"""Falsifiability tests — the prod target's deploy identity and root path.

Production deploys must be deterministic and identity-stable:

  * `run_as` a fixed SERVICE PRINCIPAL (via a bundle variable) — not the user who happens to run
    the deploy — so the same identity creates the synced table and runs the migration job every time.
  * `root_path` a FIXED, non-user-derived workspace path — not `/Workspace/Users/<current_user>/...`
    — so prod always deploys to one place regardless of who runs it.

These assert on the shipped `dabs/databricks.yml` (parsed as YAML), so a regression that reverts
prod to a user-derived root path, or drops `run_as`, turns CI red without needing a workspace. The
end-to-end offline proof that the bundle still strict-validates with these set lives in
`dabs/scripts/offline_validate.sh` / `test_bundle_validate.py`.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DATABRICKS_YML = REPO_ROOT / "dabs" / "databricks.yml"


def _doc() -> dict:
    return yaml.safe_load(DATABRICKS_YML.read_text())


def _prod_target() -> dict:
    doc = _doc()
    targets = doc.get("targets") or {}
    assert "prod" in targets, f"databricks.yml has no prod target: {sorted(targets)}"
    return targets["prod"]


def test_prod_runs_as_a_service_principal_variable():
    """The prod target sets `run_as` to a service principal, and the SP is a bundle VARIABLE
    reference (`${var...}`) — never a committed real SP id.

    MUTATION GATE: drop `run_as` from prod (or point it at `user_name`/a literal) and this goes RED.
    """
    prod = _prod_target()
    run_as = prod.get("run_as")
    assert run_as, f"prod target has no run_as: {sorted(prod)}"
    sp = run_as.get("service_principal_name")
    assert sp, f"prod run_as does not set service_principal_name: {run_as}"
    assert sp.startswith("${var."), (
        f"prod run_as service_principal_name must be a bundle variable reference, got {sp!r} "
        "(a real SP id must never be committed to a public reference)"
    )


def test_prod_run_as_variable_is_declared_with_a_placeholder_default():
    """The variable `run_as` references is declared in `variables:` with a non-real placeholder
    default, so the public reference commits no real service principal id."""
    doc = _doc()
    sp_ref = _prod_target()["run_as"]["service_principal_name"]
    var_name = sp_ref[len("${var."):].rstrip("}")
    variables = doc.get("variables") or {}
    assert var_name in variables, f"run_as references undeclared variable {var_name!r}: {sorted(variables)}"
    default = str(variables[var_name].get("default", ""))
    assert "PLACEHOLDER" in default.upper() or "REPLACE" in default.upper(), (
        f"variable {var_name!r} default {default!r} does not look like a placeholder — a real "
        "service principal id must not be committed"
    )


def test_prod_root_path_is_fixed_not_user_derived():
    """The prod target's workspace root_path is a FIXED path — it must NOT interpolate the deploying
    user's name (`current_user`), so prod always deploys to the same place.

    MUTATION GATE: revert root_path to `/Workspace/Users/${workspace.current_user.userName}/...` and
    this goes RED.
    """
    prod = _prod_target()
    workspace = prod.get("workspace") or {}
    root_path = workspace.get("root_path")
    assert root_path, f"prod target has no workspace.root_path: {workspace}"
    assert "current_user" not in root_path, (
        f"prod root_path is user-derived ({root_path!r}); it must be a fixed, non-user path so "
        "production always deploys to one location regardless of who runs the deploy"
    )
    assert root_path.startswith("/Workspace/"), f"unexpected prod root_path: {root_path!r}"
