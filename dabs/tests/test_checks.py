"""Falsifiability tests for the job's two check tasks: `verify` and `verify_identity_policy`.

What this protects: the app role's access to the synced tables (exactly the consumer view, nothing
on the base table), and the deploy identity's rights in the prod target (it must not be able to read
or write every table, or create roles).

What must never happen:
  * verify passes while the app role can read or write the base synced table by ANY route
    (table grant, column grant, PUBLIC, an inherited role) — the view would no longer be the only
    access path;
  * the policy guard passes while the job's identity can read or write every table or create roles
    by ANY route (direct membership, inherited membership, a NOINHERIT chain it can SET ROLE
    through, a role attribute on itself or on a role it belongs to), or while the job runs as an
    identity other than the expected deploy identity;
  * a check that errors, or returns no row, counts as a pass;
  * a failed check leaves the task green.

Attacker list (each item has a test below that goes red under the named mutation):
  V-table   GRANT SELECT on the base table to the app role
  V-column  GRANT SELECT (one column) on the base table           (has_table_privilege misses it)
  V-public  GRANT SELECT on the base table to PUBLIC
  V-inherit GRANT SELECT to a helper role, helper granted to the app role
  V-write   GRANT UPDATE / DELETE on the base table to the app role
  P-direct  deploy identity is a direct member of databricks_superuser
  P-chain   ... a member through an intermediate role               (pg_auth_members misses it)
  P-noinh   ... through a NOINHERIT intermediate role (can still SET ROLE)
  P-read    ... a member of pg_read_all_data
  P-write   ... a member of pg_write_all_data
  P-cr      deploy identity has CREATEROLE
  P-cr-via  deploy identity belongs to a role that has CREATEROLE
  P-super   deploy identity has SUPERUSER
  P-who     the job runs as another identity
  P-ph      expected identity is empty, a placeholder, or SQL-shaped
  H-none    a check returning no row / NULL passes
  H-err     a check that raises passes
  H-first   checking stops at the first failure (the operator loses the full picture)

Real-Postgres tests use the hermetic throwaway cluster from test_migration_job (skips where no
local Postgres exists). A stand-in role named `databricks_superuser` plays the Lakebase role;
pg_read_all_data / pg_write_all_data are real Postgres 14+ roles.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from dabs import checks
from dabs import migration_job as mj
from dabs.tests.test_migration_job import _needs_pg, hermetic_pg  # noqa: F401  (fixture import)

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG = REPO_ROOT / "config" / "tables.json"
TABLES = json.loads(CONFIG.read_text())

DEPLOY = "deploy_sp"  # stands in for the deploy service principal's application id


# --- pure harness: run_checks -------------------------------------------------------------------

def test_run_checks_passes_only_on_true():
    """H-none: a check passes only when it returns exactly True. False, None (no row / NULL) and a
    't' string all fail."""
    results = {"a": True, "b": False, "c": None, "d": "t"}
    lines: list[str] = []
    failures = checks.run_checks(
        [(k, k) for k in results], lambda sql: results[sql], out=lines.append
    )
    assert failures == 3, lines
    assert [ln.split()[0] for ln in lines if ln.strip().startswith(("PASS", "FAIL"))] == [
        "PASS", "FAIL", "FAIL", "FAIL"
    ], lines


def test_run_checks_counts_an_error_as_a_failure_and_names_the_cause():
    """H-err: a check whose query raises is a FAIL, and the FAIL line carries the error text."""
    def execute(sql):
        if sql == "boom":
            raise RuntimeError("relation cicd_proj.members does not exist")
        return True

    lines: list[str] = []
    failures = checks.run_checks([("ok", "ok"), ("bad", "boom")], execute, out=lines.append)
    assert failures == 1
    assert any("FAIL bad" in ln and "does not exist" in ln for ln in lines), lines


def test_run_checks_runs_every_check_after_a_failure():
    """H-first: all checks run so the operator sees the full picture."""
    seen: list[str] = []

    def execute(sql):
        seen.append(sql)
        return False

    failures = checks.run_checks([("1", "q1"), ("2", "q2"), ("3", "q3")], execute, out=lambda _: None)
    assert failures == 3
    assert seen == ["q1", "q2", "q3"]


# --- pure: which checks exist --------------------------------------------------------------------

def test_verify_checks_cover_every_table_and_index():
    """Every configured table gets the access checks, the denied-on-table check and one check per
    index column (hostile: two tables, two indexes each — a hardcoded first table fails)."""
    labels = [label for label, _ in checks.verify_checks(TABLES)]
    for t in TABLES:
        tbl = t["synced_table_id"].split(".")[-1]
        role = t["app_role"]
        assert any(role in lb and "denied" in lb and tbl in lb for lb in labels), (role, labels)
        for col in t["index_columns"]:
            assert any(f"idx_{tbl}_{col}" in lb for lb in labels), (col, labels)


@pytest.mark.parametrize("bad", ["", "  ", "PLACEHOLDER_SERVICE_PRINCIPAL_APPLICATION_ID",
                                 "x' OR '1'='1", "a;b", "ab\ncd"])
def test_policy_refuses_a_missing_placeholder_or_unsafe_expected_identity(bad):
    """P-ph: the guard never runs with an unusable expected identity — it raises, so the task fails
    closed instead of comparing current_user to a placeholder or injecting SQL."""
    with pytest.raises(ValueError):
        checks.policy_checks(bad)


def test_main_has_no_switch_that_skips_the_guard():
    """The guard has no run-time off switch: the entrypoint's only steps are migrate, verify and
    policy, and no option name suggests skipping."""
    parser_opts = mj.build_parser()._option_string_actions
    assert not [o for o in parser_opts if "skip" in o or "no-policy" in o or "disable" in o], parser_opts
    step = mj.build_parser()._option_string_actions["--step"]
    assert sorted(step.choices) == ["migrate", "policy", "verify"]


def test_main_returns_nonzero_when_a_check_fails(monkeypatch):
    """A failed check makes the task fail: main() returns non-zero for verify and for policy."""
    monkeypatch.setattr(mj, "_live_execute", lambda branch: (lambda sql: False))
    assert mj.main(["--step", "verify", "--config", str(CONFIG), "--branch", "projects/p/branches/b"]) != 0
    assert mj.main(["--step", "policy", "--expected-identity", DEPLOY,
                    "--branch", "projects/p/branches/b"]) != 0


def test_main_returns_zero_when_every_check_passes(monkeypatch):
    monkeypatch.setattr(mj, "_live_execute", lambda branch: (lambda sql: True))
    assert mj.main(["--step", "verify", "--config", str(CONFIG), "--branch", "projects/p/branches/b"]) == 0
    assert mj.main(["--step", "policy", "--expected-identity", DEPLOY,
                    "--branch", "projects/p/branches/b"]) == 0


# --- real Postgres: the SQL semantics -------------------------------------------------------------

_SETUP = f"""
CREATE ROLE databricks_superuser NOLOGIN;
CREATE ROLE writer NOLOGIN;
CREATE ROLE {DEPLOY} LOGIN;
CREATE ROLE members_app_ro NOLOGIN;
CREATE ROLE providers_app_ro NOLOGIN;
CREATE SCHEMA cicd_proj AUTHORIZATION {DEPLOY};
GRANT USAGE, CREATE ON SCHEMA cicd_proj TO writer;
SET ROLE writer;
CREATE TABLE cicd_proj.members (id int PRIMARY KEY, member_id text, plan_code text);
CREATE TABLE cicd_proj.providers (id int PRIMARY KEY, provider_id text, specialty_code text);
INSERT INTO cicd_proj.members VALUES (1, 'm1', 'p1');
GRANT SELECT ON cicd_proj.members, cicd_proj.providers TO {DEPLOY};
RESET ROLE;
"""


def _executor(run, as_role: str = DEPLOY):
    """Run one check query as `as_role` (SET ROLE in the same psql session) and return a Python
    bool, or None for no row. A SQL error raises, so run_checks reports it as a FAIL."""
    def execute(sql: str):
        res = run(f"SET ROLE {as_role};\n{sql};")
        if res.returncode != 0:
            raise RuntimeError(res.stderr.strip())
        out = [ln for ln in res.stdout.splitlines() if ln and ln != "SET"]
        if not out:
            return None
        return {"t": True, "f": False}.get(out[-1], out[-1])
    return execute


@pytest.fixture()
def deployed(hermetic_pg):  # noqa: F811
    """A cluster in the deployed state: synced-table stand-ins owned by a writer role, the deploy
    identity owning the schema, admin-made app roles, and the rendered DDL applied as the deploy
    identity. verify and the policy guard both pass here (asserted by the baseline tests)."""
    run = hermetic_pg
    res = run(_SETUP)
    assert res.returncode == 0, res.stderr
    from dabs import render_ddl
    # Plain Postgres lets only a table's OWNER create an index; Lakebase also lets the synced
    # table's creator do it. So apply the DDL as the admin, then hand the views to the deploy
    # identity: the same end state the job leaves (deploy identity owns the views and holds SELECT
    # on the base tables; the app roles hold SELECT on the views only).
    res = run(render_ddl.render_ddl(TABLES)
              + f"ALTER VIEW cicd_proj.members_v OWNER TO {DEPLOY};\n"
              + f"ALTER VIEW cicd_proj.providers_v OWNER TO {DEPLOY};\n")
    assert res.returncode == 0, res.stderr
    return run


def _verify_failures(run) -> list[str]:
    lines: list[str] = []
    checks.run_checks(checks.verify_checks(TABLES), _executor(run), out=lines.append)
    return [ln for ln in lines if ln.strip().startswith("FAIL")]


def _policy_failures(run, expected: str = DEPLOY, as_role: str = DEPLOY) -> list[str]:
    lines: list[str] = []
    checks.run_checks(checks.policy_checks(expected), _executor(run, as_role), out=lines.append)
    return [ln for ln in lines if ln.strip().startswith("FAIL")]


@_needs_pg
def test_verify_passes_on_a_clean_deploy(deployed):
    assert _verify_failures(deployed) == []


@_needs_pg
@pytest.mark.parametrize("fault", [
    "GRANT SELECT ON cicd_proj.members TO members_app_ro",                  # V-table
    "GRANT SELECT (member_id) ON cicd_proj.members TO members_app_ro",       # V-column
    "GRANT SELECT ON cicd_proj.members TO PUBLIC",                          # V-public
    "CREATE ROLE helper NOLOGIN; GRANT SELECT ON cicd_proj.members TO helper; "
    "GRANT helper TO members_app_ro",                                       # V-inherit
    "ALTER ROLE members_app_ro NOINHERIT; CREATE ROLE helper_noinh NOLOGIN; "
    "GRANT SELECT ON cicd_proj.members TO helper_noinh; "
    "GRANT helper_noinh TO members_app_ro",                                 # V-noinh (SET ROLE)
    "GRANT UPDATE ON cicd_proj.members TO members_app_ro",                  # V-write
    "GRANT DELETE ON cicd_proj.members TO members_app_ro",                  # V-write
])
def test_verify_fails_when_the_app_role_can_reach_the_synced_table(deployed, fault):
    """Each route that gives the app role access to the base table fails exactly the members
    denied-on-table check. Faults are applied as the admin (a direct grant migrate never revokes)."""
    res = deployed(f"{fault};")
    assert res.returncode == 0, res.stderr
    fails = _verify_failures(deployed)
    assert len(fails) == 1, fails
    assert "members_app_ro" in fails[0] and "denied" in fails[0] and "members" in fails[0], fails


@_needs_pg
@pytest.mark.parametrize("fault", [
    "GRANT SELECT ON cicd_proj.members TO members_app_ro",                  # V-table
    "GRANT SELECT (member_id) ON cicd_proj.members TO members_app_ro",       # V-column
    "GRANT SELECT ON cicd_proj.members TO PUBLIC",                          # V-public
    "CREATE ROLE helper NOLOGIN; GRANT SELECT ON cicd_proj.members TO helper; "
    "GRANT helper TO members_app_ro",                                       # V-inherit
    "ALTER ROLE members_app_ro NOINHERIT; CREATE ROLE helper_noinh NOLOGIN; "
    "GRANT SELECT ON cicd_proj.members TO helper_noinh; "
    "GRANT helper_noinh TO members_app_ro",                                 # V-noinh (SET ROLE)
    "GRANT UPDATE ON cicd_proj.members TO members_app_ro",                  # V-write
    "GRANT DELETE ON cicd_proj.members TO members_app_ro",                  # V-write
])
def test_terraform_verify_table_fails_when_app_role_can_reach_synced_table(deployed, fault):
    """The Terraform-path verify (scripts/verify_table.sh) shares the same check list as DABs verify
    and fails on every route that gives the app role access to the base synced table."""
    import subprocess
    res = deployed(f"{fault};")
    assert res.returncode == 0, res.stderr
    verify_sh = REPO_ROOT / "scripts" / "verify_table.sh"
    r = subprocess.run(
        ["bash", str(verify_sh), "members_app_ro", "cicd_proj", "members", "members_v",
         "member_id", "plan_code"],
        env=deployed.env,
        capture_output=True,
        text=True,
    )
    assert r.returncode != 0, r.stdout + r.stderr
    assert "FAIL role members_app_ro is denied on synced table cicd_proj.members" in r.stdout, (
        r.stdout + r.stderr
    )


@_needs_pg
def test_verify_fails_when_the_view_is_gone(deployed):
    """A replace leaves no view: verify fails (the view checks), and a missing base table does not
    crash the run."""
    res = deployed("DROP VIEW cicd_proj.members_v;")
    assert res.returncode == 0, res.stderr
    assert _verify_failures(deployed), "verify passed with the consumer view dropped"


@_needs_pg
def test_policy_passes_for_the_deploy_identity(deployed):
    assert _policy_failures(deployed) == []


@_needs_pg
@pytest.mark.parametrize("fault,named", [
    (f"GRANT databricks_superuser TO {DEPLOY}", "databricks_superuser"),                      # P-direct
    (f"CREATE ROLE mid NOLOGIN; GRANT databricks_superuser TO mid; GRANT mid TO {DEPLOY}",
     "databricks_superuser"),                                                               # P-chain
    (f"CREATE ROLE mid NOLOGIN NOINHERIT; GRANT databricks_superuser TO mid; GRANT mid TO {DEPLOY}",
     "databricks_superuser"),                                                               # P-noinh
    (f"GRANT pg_read_all_data TO {DEPLOY}", "pg_read_all_data"),                              # P-read
    (f"GRANT pg_write_all_data TO {DEPLOY}", "pg_write_all_data"),                            # P-write
    (f"ALTER ROLE {DEPLOY} CREATEROLE", "CREATEROLE"),                                        # P-cr
    (f"CREATE ROLE roler NOLOGIN CREATEROLE; GRANT roler TO {DEPLOY}", "CREATEROLE"),         # P-cr-via
    (f"ALTER ROLE {DEPLOY} SUPERUSER", "SUPERUSER"),                                          # P-super
])
def test_policy_fails_on_every_route_to_wide_rights(deployed, fault, named):
    """Each route to read-all, write-all or create-role rights fails the guard, on the check that
    names that right (a superuser also fails the membership checks, so 'any failure' would let the
    SUPERUSER check be deleted unnoticed)."""
    res = deployed(fault + ";")
    assert res.returncode == 0, res.stderr
    fails = _policy_failures(deployed)
    assert any(named in f for f in fails), f"no failing check names {named} after: {fault}\n{fails}"


@_needs_pg
def test_policy_fails_when_the_job_runs_as_another_identity(deployed):
    """P-who: the guard reads current_user, so it fails when the job is not the expected identity."""
    fails = _policy_failures(deployed, expected=DEPLOY, as_role="members_app_ro")
    assert any("expected" in f for f in fails), fails


@_needs_pg
def test_policy_does_not_error_where_a_named_role_is_absent(hermetic_pg):  # noqa: F811
    """A Postgres without databricks_superuser (any non-Lakebase test box) reads as 'not a member',
    never as a SQL error."""
    run = hermetic_pg
    assert run(f"CREATE ROLE {DEPLOY} LOGIN;").returncode == 0
    assert _policy_failures(run) == []


def test_checks_loads_render_ddl_by_path_without_package(tmp_path):
    """Attacker 5: when `dabs/checks.py` is loaded by path in an isolated interpreter (`python3 -I`)
    where `dabs` is not on `sys.path`, its fallback `spec_from_file_location` block must load
    sibling `render_ddl.py` and build `verify_checks(TABLES)`.

    MUTATION GATE — break the `except Exception:` fallback in `dabs/checks.py` and this goes RED.
    """
    isolated = tmp_path / "isolated_dabs"
    isolated.mkdir(parents=True)
    for mod in ("checks.py", "render_ddl.py"):
        (isolated / mod).write_bytes((REPO_ROOT / "dabs" / mod).read_bytes())
    cfg = tmp_path / "tables.json"
    cfg.write_bytes((REPO_ROOT / "config" / "tables.json").read_bytes())

    script = r"""
import importlib.util, json, sys
from pathlib import Path

checks_path = Path(sys.argv[1])
cfg_path = Path(sys.argv[2])

spec = importlib.util.spec_from_file_location("isolated_checks", checks_path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

tables = json.loads(cfg_path.read_text())
v_checks = mod.verify_checks(tables)
print(json.dumps({"count": len(v_checks), "labels": [c[0] for c in v_checks]}))
"""
    proc = subprocess.run(
        [sys.executable, "-I", "-c", script, str(isolated / "checks.py"), str(cfg)],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (
        f"checks.py failed to load render_ddl by path under python -I:\n"
        f"STDOUT:{proc.stdout}\nSTDERR:{proc.stderr}"
    )
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["count"] == 16, out
    assert any("is denied on synced table" in lbl for lbl in out["labels"]), out

