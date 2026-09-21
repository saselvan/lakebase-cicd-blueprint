"""Offline, Docker-free unit test of the extracted verify step (scripts/verify_table.sh).

scripts/deploy.sh step 4 (verify) asserts the real post-conditions and fails the pipeline when any
is unmet: the app role can SELECT its consumer view, and every expected index exists. This test
drives `verify_table` against stubbed psql results and confirms it exits non-zero on a false
post-condition — the app role NOT granted SELECT on its consumer view, or a missing index.

The verify logic now lives in scripts/verify_table.sh's `verify_table` function, which ASSERTS
each post-condition and returns NON-ZERO on the first table whose post-conditions are not met:
  (c) the app role and the consumer view exist,
  (a) the app role can SELECT the consumer VIEW (the no-superuser read path),
  (b) every expected index idx_<table>_<col> exists.

These tests drive `verify_table` with a stub `psql` on PATH (no database) so the assertion/exit
behavior is provable offline and fast — the exact behavior a stub can prove is: a false scalar
result flips the exit code. The end-to-end proof against a real postgres:16 (real GRANT/REVOKE,
real indexes) lives in scripts/tests/docker_verify_step.sh.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
VERIFY_SH = REPO_ROOT / "scripts" / "verify_table.sh"
DEPLOY_SH = REPO_ROOT / "scripts" / "deploy.sh"

# A stub `psql` that ignores the database and returns a controlled scalar per query kind, driven by
# STUB_* env vars (default 't'). It inspects the SQL (the last positional arg, i.e. the `-c` text).
_STUB_PSQL = r"""#!/usr/bin/env bash
# Default to 't' only when the STUB_* var is UNSET (`-`, not `:-`), so a test can set it to the
# empty string to simulate a psql error whose output is empty.
sql="${@: -1}"
case "$sql" in
  *pg_roles*)             printf '%s\n' "${STUB_ROLE_EXISTS-t}" ;;
  *pg_views*)             printf '%s\n' "${STUB_VIEW_EXISTS-t}" ;;
  *has_table_privilege*)  printf '%s\n' "${STUB_CAN_SELECT-t}" ;;
  *pg_indexes*)           printf '%s\n' "${STUB_INDEX_EXISTS-t}" ;;
  *)                      printf '\n' ;;
esac
"""

# A stub `psql` that FAILS the way a real psql does on a connection/relation error: it writes a
# diagnostic to STDERR and exits non-zero, printing nothing to stdout. This is the case this fix
# addresses — the old `_scalar` ran `2>/dev/null`, so a FAIL line showed only the empty value and
# never the CAUSE. The exact cause string below must survive to the operator's output.
_STUB_PSQL_STDERR_CAUSE = "psql: error: connection to server on socket failed: No such file"
_STUB_PSQL_STDERR = (
    "#!/usr/bin/env bash\n"
    f'printf "%s\\n" "{_STUB_PSQL_STDERR_CAUSE}" >&2\n'
    "exit 2\n"
)


def _run_verify(tmp_path: Path, stub_env: dict, args: list[str]) -> subprocess.CompletedProcess:
    """Run scripts/verify_table.sh with a stub psql on PATH and the given STUB_* env."""
    stub = tmp_path / "psql"
    stub.write_text(_STUB_PSQL)
    stub.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{tmp_path}{os.pathsep}{env.get('PATH', '')}"
    env.update(stub_env)
    return subprocess.run(
        ["bash", str(VERIFY_SH), *args],
        capture_output=True,
        text=True,
        env=env,
    )


# The canonical positive call: two index columns, all post-conditions hold.
_OK_ARGS = ["members_app_ro", "cicd_proj", "members", "members_v", "member_id", "plan_code"]


def test_all_postconditions_met_exits_zero(tmp_path):
    r = _run_verify(tmp_path, {}, _OK_ARGS)  # all STUB_* default to 't'
    assert r.returncode == 0, r.stdout + r.stderr
    assert "VERIFY OK" in r.stdout
    assert "FAIL" not in r.stdout


def test_missing_grant_on_view_exits_nonzero(tmp_path):
    # The hostile row: role and view exist, but the app role is NOT granted SELECT on the view.
    # This is the exact case the old `|| true` verify silently passed.
    r = _run_verify(tmp_path, {"STUB_CAN_SELECT": "f"}, _OK_ARGS)
    assert r.returncode != 0, r.stdout + r.stderr
    assert "FAIL" in r.stdout
    assert "can SELECT" in r.stdout


def test_missing_index_exits_nonzero(tmp_path):
    r = _run_verify(tmp_path, {"STUB_INDEX_EXISTS": "f"}, _OK_ARGS)
    assert r.returncode != 0, r.stdout + r.stderr
    assert "FAIL" in r.stdout
    assert "index idx_members_" in r.stdout


def test_missing_role_exits_nonzero(tmp_path):
    r = _run_verify(tmp_path, {"STUB_ROLE_EXISTS": "f"}, _OK_ARGS)
    assert r.returncode != 0, r.stdout + r.stderr
    assert "FAIL" in r.stdout


def test_missing_view_exits_nonzero(tmp_path):
    r = _run_verify(tmp_path, {"STUB_VIEW_EXISTS": "f"}, _OK_ARGS)
    assert r.returncode != 0, r.stdout + r.stderr
    assert "FAIL" in r.stdout


def test_psql_error_empty_result_is_not_a_silent_pass(tmp_path):
    # The `*)` stub branch returns empty (as a psql error would after 2>/dev/null). An empty
    # scalar must FAIL the assertion, never pass it — verify one index whose query returns ''.
    r = _run_verify(tmp_path, {"STUB_INDEX_EXISTS": ""}, _OK_ARGS)
    assert r.returncode != 0, r.stdout + r.stderr
    assert "FAIL" in r.stdout


def test_psql_stderr_cause_is_surfaced_on_failure(tmp_path):
    # When psql itself errors (connection refused, missing relation), _scalar used to
    # discard stderr (`2>/dev/null`), so the FAIL line showed the empty value but not WHY. The stub
    # psql writes a real diagnostic to stderr and exits non-zero; the surfaced output must contain
    # that cause so the operator can act on it (and the run must still FAIL, never silently pass).
    stub = tmp_path / "psql"
    stub.write_text(_STUB_PSQL_STDERR)
    stub.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{tmp_path}{os.pathsep}{env.get('PATH', '')}"
    r = subprocess.run(
        ["bash", str(VERIFY_SH), *_OK_ARGS],
        capture_output=True,
        text=True,
        env=env,
    )
    assert r.returncode != 0, r.stdout + r.stderr
    combined = r.stdout + r.stderr
    assert _STUB_PSQL_STDERR_CAUSE in combined, (
        "the psql stderr cause must be surfaced on failure, got:\n" + combined
    )


def test_happy_path_stays_quiet_no_spurious_psql_error(tmp_path):
    # The fix must not make the happy path noisy: when every query succeeds, no psql error diagnostic
    # is printed (the stub exits 0, so _scalar must stay silent on stderr).
    r = _run_verify(tmp_path, {}, _OK_ARGS)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "psql error" not in (r.stdout + r.stderr).lower()


def test_no_index_columns_still_checks_role_and_view(tmp_path):
    # Zero index columns (index_statements supports 0): still asserts role + view, exits zero.
    r = _run_verify(tmp_path, {}, ["members_app_ro", "cicd_proj", "members", "members_v"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert "VERIFY OK" in r.stdout


def test_verify_script_is_syntactically_valid():
    # Offline gate: `bash -n` must stay clean (CI also runs this over scripts/*.sh).
    r = subprocess.run(["bash", "-n", str(VERIFY_SH)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_deploy_sources_verify_and_dropped_the_swallowing_check():
    # Regression guard for the fix: deploy.sh must delegate to verify_table.sh and must NOT carry
    # the old un-inspectable `has_table_privilege ... || true` inline check.
    deploy = DEPLOY_SH.read_text()
    assert "verify_table.sh" in deploy, "deploy.sh must source the extracted verify step"
    assert "verify_table " in deploy, "deploy.sh must call verify_table in the per-table loop"
    assert "has_table_privilege" not in deploy, (
        "the swallowing inline verify must be gone from deploy.sh (moved into verify_table.sh)"
    )
    assert "2>&1 || true" not in deploy, (
        "step 4 must no longer swallow the verify result with the old `2>&1 || true` pattern"
    )
