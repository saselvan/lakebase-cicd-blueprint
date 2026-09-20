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
