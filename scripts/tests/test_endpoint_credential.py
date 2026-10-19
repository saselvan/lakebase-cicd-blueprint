"""Credential minting must use the projects (Autoscaling) endpoint API, not the old provisioned
`database generate-database-credential --json instance_names` form.

A migration WRITES, so it must target the branch's READ_WRITE compute endpoint. The shared helper
(scripts/resolve_endpoint.sh) resolves that endpoint from `postgres list-endpoints`, then derives
BOTH the connection host AND the endpoint resource name from the SAME chosen endpoint, and mints a
runtime OAuth token from that endpoint via `postgres generate-database-credential <endpoint>`.
This mirrors dabs/migration_job.py::_select_endpoint.

Offline proof (no cloud): a fake `databricks` early on PATH answers canned `list-endpoints`
(a read-only endpoint listed FIRST, a READ_WRITE second — to prove selection is not a blind [0]
pick) and canned `generate-database-credential` JSON, and refuses the old `database ... instance_names`
call. We then run the resolve+mint code the real scripts use and assert:
  (a) the READ_WRITE endpoint's host is chosen (not the read-only one listed first),
  (b) `postgres generate-database-credential` is called with that endpoint's `.name`,
  (c) the old `database generate-database-credential ... instance_names` is NEVER called,
  (d) the minted token is exported.

MUTATION GATE: make the selector pick endpoints[0] blindly -> assertion (a) goes RED (the host
becomes the read-only host listed first). Restore -> green.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RESOLVE_HELPER = REPO_ROOT / "scripts" / "resolve_endpoint.sh"
BRANCH_TEST_SH = REPO_ROOT / "scripts" / "branch_test.sh"
DEPLOY_SH = REPO_ROOT / "scripts" / "deploy.sh"
PR_VALIDATE = REPO_ROOT / ".github" / "workflows" / "pr-validate.yml"
READ_VIA_VIEW = REPO_ROOT / "examples" / "read_via_view.py"

RO_HOST = "ro-host.example.lakebase"
RW_HOST = "rw-host.example.lakebase"
RW_NAME = "projects/my-lakebase-project/branches/pr-123/endpoints/rw-endpoint"
RO_NAME = "projects/my-lakebase-project/branches/pr-123/endpoints/ro-endpoint"
STUB_TOKEN = "stub-oauth-token-123"

# Read-only endpoint listed FIRST, READ_WRITE second — a blind [0] pick would choose the wrong one.
ENDPOINTS_JSON = json.dumps(
    [
        {"name": RO_NAME, "status": {"endpoint_type": "READ_ONLY", "hosts": {"host": RO_HOST}}},
        {"name": RW_NAME, "status": {"endpoint_type": "READ_WRITE", "hosts": {"host": RW_HOST}}},
    ]
)

STUB_DATABRICKS = f"""#!/usr/bin/env bash
# Fake `databricks` for offline resolve+mint proof. Logs every invocation; answers canned JSON.
printf '%s\\n' "$*" >> "$STUB_LOG"
group="${{1:-}}"; sub="${{2:-}}"
case "$group $sub" in
  "postgres list-endpoints")            cat "$STUB_ENDPOINTS_JSON" ;;
  "postgres generate-database-credential") printf '%s\\n' '{{"token":"{STUB_TOKEN}"}}' ;;
  "postgres create-branch")             printf '%s\\n' '{{}}' ;;
  "auth describe")                      printf '%s\\n' '{{"username":"stub-user@example.com"}}' ;;
  "database generate-database-credential")
      printf '%s\\n' "FORBIDDEN old provisioned cred path used" >> "$STUB_LOG"
      printf '%s\\n' '{{"token":"WRONG-do-not-use"}}'; exit 3 ;;
  *)                                    printf '%s\\n' '{{}}' ;;
esac
"""


def _stub_env(tmp_path: Path) -> tuple[dict, Path]:
    """Write a fake `databricks` early on PATH + canned endpoints JSON; return (env, call_log)."""
    stub = tmp_path / "databricks"
    stub.write_text(STUB_DATABRICKS)
    stub.chmod(0o755)
    endpoints = tmp_path / "endpoints.json"
    endpoints.write_text(ENDPOINTS_JSON)
    call_log = tmp_path / "calls.log"
    env = dict(os.environ)
    env["PATH"] = f"{tmp_path}:{env['PATH']}"
    env["STUB_LOG"] = str(call_log)
    env["STUB_ENDPOINTS_JSON"] = str(endpoints)
    return env, call_log


def test_helper_selects_read_write_and_mints_from_endpoint(tmp_path):
    """The shared resolve+mint helper picks the READ_WRITE endpoint, derives host + name from that
    SAME endpoint, mints the token from the endpoint name, and never touches the instance path."""
    env, call_log = _stub_env(tmp_path)
    harness = (
        f'. "{RESOLVE_HELPER}"; '
        'lakebase_resolve_and_mint "projects/my-lakebase-project/branches/pr-123" DEFAULT; '
        'printf "HOST=%s\\nENDPOINT=%s\\nTOKEN=%s\\n" "$LB_HOST" "$LB_ENDPOINT" "$LB_TOKEN"'
    )
    result = subprocess.run(
        ["bash", "-c", harness], env=env, capture_output=True, text=True
    )
    assert result.returncode == 0, f"helper failed:\n{result.stdout}\n{result.stderr}"
    out = result.stdout

    # (a) READ_WRITE host chosen, not the read-only host listed first.
    assert f"HOST={RW_HOST}" in out, out
    assert RO_HOST not in out, f"read-only host leaked (blind [0] pick?):\n{out}"
    # host + name come from the SAME chosen endpoint.
    assert f"ENDPOINT={RW_NAME}" in out, out
    # (d) token minted + exported.
    assert f"TOKEN={STUB_TOKEN}" in out, out

    calls = call_log.read_text()
    # (b) credential minted from the READ_WRITE endpoint NAME via the projects API.
    assert f"postgres generate-database-credential {RW_NAME}" in calls, calls
    # (c) the old provisioned instance-name path is never used.
    assert "database generate-database-credential" not in calls, calls
    assert "instance_names" not in calls, calls
    assert "FORBIDDEN" not in calls, calls


def test_branch_test_script_resolves_via_endpoint(tmp_path):
    """branch_test.sh (no terraform/liquibase — safe to run fully) mints via the endpoint form."""
    env, call_log = _stub_env(tmp_path)
    result = subprocess.run(
        ["bash", str(BRANCH_TEST_SH), "my-lakebase-project", "pr-123", "DEFAULT"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"branch_test.sh failed:\n{result.stdout}\n{result.stderr}"
    calls = call_log.read_text()
    assert f"postgres generate-database-credential {RW_NAME}" in calls, calls
    assert "database generate-database-credential" not in calls, calls
    assert "instance_names" not in calls, calls
    # It resolved the READ_WRITE endpoint's host, not the read-only one listed first.
    assert RW_HOST in result.stdout, result.stdout
    assert RO_HOST not in result.stdout, result.stdout


def test_all_four_sites_use_projects_endpoint_cred_form():
    """Static regression across all four sites: none may mint via the old provisioned form, and
    each must reach the projects endpoint form (directly or via the shared helper)."""
    for path in (DEPLOY_SH, BRANCH_TEST_SH, PR_VALIDATE, READ_VIA_VIEW):
        text = path.read_text()
        assert "instance_names" not in text, f"{path.name} still references instance_names"
        assert "database generate-database-credential" not in text, (
            f"{path.name} still uses the old provisioned cred form"
        )
        assert "database\", \"generate-database-credential\"" not in text, (
            f"{path.name} still uses the old provisioned cred form (python subprocess)"
        )

    # deploy.sh + branch_test.sh reach the endpoint form through the shared helper.
    for path in (DEPLOY_SH, BRANCH_TEST_SH):
        assert "resolve_endpoint.sh" in path.read_text(), (
            f"{path.name} does not source the shared endpoint-resolution helper"
        )
    # deploy.sh no longer requires a manual HOST or an INSTANCE var.
    deploy = DEPLOY_SH.read_text()
    assert "INSTANCE=" not in deploy, "deploy.sh still defines an INSTANCE var"

    # pr-validate + read_via_view reach the projects endpoint cred form.
    assert "postgres generate-database-credential" in PR_VALIDATE.read_text() or (
        "resolve_endpoint.sh" in PR_VALIDATE.read_text()
    ), "pr-validate.yml does not use the projects endpoint cred form"
    assert "postgres" in READ_VIA_VIEW.read_text() and "generate-database-credential" in (
        READ_VIA_VIEW.read_text()
    ), "read_via_view.py does not use the projects endpoint cred form"


def _make_tf_repo(tmp_path: Path, tfvars_content: str | None = None) -> Path:
    """Create a minimal repo layout with terraform/variables.tf, terraform/outputs.tf, and optional
    terraform/terraform.tfvars for testing lakebase_resolve_database."""
    tf_dir = tmp_path / "terraform"
    tf_dir.mkdir(parents=True, exist_ok=True)
    (tf_dir / "variables.tf").write_text((REPO_ROOT / "terraform" / "variables.tf").read_text())
    (tf_dir / "outputs.tf").write_text((REPO_ROOT / "terraform" / "outputs.tf").read_text())
    if tfvars_content is not None:
        (tf_dir / "terraform.tfvars").write_text(tfvars_content)
    return tmp_path


def test_resolve_database_precedence_and_pgdatabase_does_not_override_tfvars(tmp_path):
    """Resolution order is strictly:
      1. TF_VAR_logical_database
      2. terraform/terraform.tfvars
      3. terraform/variables.tf default (databricks_postgres)
    PGDATABASE and LOGICAL_DATABASE must NEVER override terraform.tfvars or variables.tf."""
    # Case 3: default from variables.tf when neither TF_VAR_logical_database nor tfvars is set.
    repo_default = _make_tf_repo(tmp_path / "c_default")
    env = dict(os.environ)
    env.pop("TF_VAR_logical_database", None)
    env["PGDATABASE"] = "pgdatabase_must_not_win"
    env["LOGICAL_DATABASE"] = "invented_var_must_not_win"
    r_def = subprocess.run(
        ["bash", "-c", f'. "{RESOLVE_HELPER}" && lakebase_resolve_database "{repo_default}"'],
        env=env, capture_output=True, text=True,
    )
    assert r_def.returncode == 0, r_def.stderr
    assert r_def.stdout.strip() == "databricks_postgres"

    # Case 2: terraform.tfvars overrides variables.tf default, and PGDATABASE does NOT override tfvars.
    # Include HCL #, // (with apostrophe), and /* ... */ comments to prove they are ignored.
    repo_tfvars = _make_tf_repo(
        tmp_path / "c_tfvars",
        '# comment with logical_database = "ignored"\n'
        '// don\'t use logical_database = "slash_comment_ignored"\n'
        '/* logical_database = "block_comment_ignored" */\n'
        'logical_database = "tfvars_custom_db"\n',
    )
    r_tfv = subprocess.run(
        ["bash", "-c", f'. "{RESOLVE_HELPER}" && lakebase_resolve_database "{repo_tfvars}"'],
        env=env, capture_output=True, text=True,
    )
    assert r_tfv.returncode == 0, r_tfv.stderr
    assert r_tfv.stdout.strip() == "tfvars_custom_db"

    # Case 1: TF_VAR_logical_database overrides terraform.tfvars.
    env_tfvar = dict(env)
    env_tfvar["TF_VAR_logical_database"] = "env_tfvar_db"
    r_env = subprocess.run(
        ["bash", "-c", f'. "{RESOLVE_HELPER}" && lakebase_resolve_database "{repo_tfvars}"'],
        env=env_tfvar, capture_output=True, text=True,
    )
    assert r_env.returncode == 0, r_env.stderr
    assert r_env.stdout.strip() == "env_tfvar_db"


def test_resolve_database_rejects_hostile_database_names_in_bash(tmp_path):
    """Hostile database names with URL or conninfo injection characters (e.g. db?sslmode=disable
    or db sslmode=disable) must fail closed in bash before building any URL or conninfo."""
    repo = _make_tf_repo(tmp_path / "repo")
    for bad in ("db?sslmode=disable", "db sslmode=disable", "1bad", "db;drop"):
        env = dict(os.environ)
        env["TF_VAR_logical_database"] = bad
        r = subprocess.run(
            ["bash", "-c", f'. "{RESOLVE_HELPER}" && lakebase_resolve_database "{repo}"'],
            env=env, capture_output=True, text=True,
        )
        assert r.returncode != 0 and r.returncode != 127, (
            f"accepted hostile database name {bad!r}: {r.stdout}"
        )
        assert "invalid database name" in r.stderr.lower(), r.stderr


def test_branch_test_fails_when_auth_describe_fails(tmp_path):
    """In scripts/branch_test.sh, a failing `databricks auth describe` must NOT be masked by
    `export PGUSER="$(...)"`."""
    env, _ = _stub_env(tmp_path)
    # Replace stub databricks so `auth describe` fails non-zero
    stub = tmp_path / "databricks"
    stub.write_text(
        STUB_DATABRICKS.replace(
            '"auth describe")                      printf \'%s\\n\' \'{"username":"stub-user@example.com"}\' ;;',
            '"auth describe")                      echo "auth expired" >&2; exit 1 ;;',
        )
    )
    stub.chmod(0o755)
    result = subprocess.run(
        ["bash", str(BRANCH_TEST_SH), "my-lakebase-project", "pr-123", "DEFAULT"],
        env=env, capture_output=True, text=True,
    )
    assert result.returncode != 0, (
        f"branch_test.sh exited 0 when `databricks auth describe` failed:\n{result.stdout}\n{result.stderr}"
    )


def test_read_via_view_uses_pgdatabase_and_rejects_hostile_database_name(monkeypatch):
    """examples/read_via_view.py reads PGDATABASE (default databricks_postgres) and validates it
    against ^[A-Za-z_][A-Za-z0-9_$-]{0,62}$ before calling psycopg.connect."""
    import importlib.util
    import sys
    import types
    import pytest

    fake_psycopg = types.ModuleType("psycopg")
    connected: list[dict] = []

    class _FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def cursor(self):
            return self

        def execute(self, sql):
            pass

        def fetchone(self):
            return (3,)

    def fake_connect(**kwargs):
        connected.append(kwargs)
        return _FakeConn()

    fake_psycopg.connect = fake_connect
    monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)

    spec = importlib.util.spec_from_file_location("read_via_view", READ_VIA_VIEW)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    monkeypatch.setattr(mod, "resolve_endpoint", lambda b, p: ("ep.host", "projects/p/branches/b/endpoints/e"))
    monkeypatch.setattr(mod, "mint_token", lambda e, p: "tok")
    monkeypatch.setenv("BRANCH", "projects/p/branches/b")
    monkeypatch.setenv("PGUSER", "u@example.com")
    monkeypatch.setenv("PGDATABASE", "custom_view_db")

    mod.main()
    assert connected and connected[-1]["dbname"] == "custom_view_db"

    for bad in ("db?sslmode=disable", "db sslmode=disable"):
        monkeypatch.setenv("PGDATABASE", bad)
        with pytest.raises(ValueError):
            mod.main()

