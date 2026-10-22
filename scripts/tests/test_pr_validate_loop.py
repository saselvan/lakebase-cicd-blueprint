"""Offline falsifiability tests for multi-table loops and database validation in:
  * .github/workflows/pr-validate.yml
  * scripts/deploy.sh
  * scripts/seed_source.sh

What must never happen:
  1. Table 1 (members) fails in `liquibase update`, table 2 (providers) succeeds, and the script
     or workflow step exits 0.
  2. A child command inside the loop (`liquibase` or `databricks`) reads stdin and drains the
     remaining table/statement lines so table 2 never runs.
  3. A hostile database name (`db?sslmode=disable` or `db sslmode=disable`) reaches the JDBC URL
     or conninfo in the workflow or deploy script.
  4. `scripts/seed_source.sh` exits 0 when `config/tables.json` is broken or when a SQL statement
     returns `state=FAILED`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from test_endpoint_credential import ENDPOINTS_JSON, RW_HOST, STUB_DATABRICKS, STUB_TOKEN
from test_verify_step import _STUB_PSQL

REPO_ROOT = Path(__file__).resolve().parents[2]
PR_VALIDATE_YML = REPO_ROOT / ".github" / "workflows" / "pr-validate.yml"
DEPLOY_SH = REPO_ROOT / "scripts" / "deploy.sh"
SEED_SH = REPO_ROOT / "scripts" / "seed_source.sh"


def _stage_repo(tmp_path: Path) -> Path:
    """Create a lightweight working copy of the repo in tmp_path so scripts can run isolated."""
    work = tmp_path / "repo"
    work.mkdir(parents=True, exist_ok=True)
    for rel in ("config", "dabs", "liquibase", "scripts", "terraform"):
        shutil.copytree(REPO_ROOT / rel, work / rel)
    return work


def _extract_pr_validate_run_script() -> str:
    doc = yaml.safe_load(PR_VALIDATE_YML.read_text())
    steps = doc["jobs"]["validate"]["steps"]
    for step in steps:
        if "Create ephemeral CoW branch" in (step.get("name") or ""):
            return step["run"].replace("${{ secrets.DATABRICKS_CLIENT_ID }}", "stub-client-id")
    raise AssertionError("Create ephemeral CoW branch step not found in pr-validate.yml")


def _setup_stubs(tmp_path: Path, liquibase_body: str) -> tuple[dict[str, str], Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)

    db_stub = bin_dir / "databricks"
    db_stub.write_text(STUB_DATABRICKS)
    db_stub.chmod(0o755)

    lb_log = tmp_path / "liquibase.log"
    lb_stub = bin_dir / "liquibase"
    lb_stub.write_text(liquibase_body)
    lb_stub.chmod(0o755)

    tf_stub = bin_dir / "terraform"
    tf_stub.write_text(
        r"""#!/usr/bin/env bash
dir="."
for a in "$@"; do
  case "$a" in
    -chdir=*) dir="${a#-chdir=}" ;;
  esac
done
resolve_stub_db() {
  local d="$1"
  if [ -f "$d/terraform.tfvars" ]; then
    python3 -c 'import pathlib,re,sys; m=re.search(r"logical_database\s*=\s*\"([^\"]*)\"", pathlib.Path(sys.argv[1]).read_text()); print(m.group(1) if m else "databricks_postgres", end="")' "$d/terraform.tfvars"
  else
    printf '%s' "${TF_VAR_logical_database:-databricks_postgres}"
  fi
}
case "$*" in
  *"output -raw logical_database"*)
    resolve_stub_db "$dir"
    exit 0
    ;;
  *console*)
    printf '"%s"\n' "$(resolve_stub_db "$dir")"
    exit 0
    ;;
esac
exit 0
"""
    )
    tf_stub.chmod(0o755)

    psql_log = tmp_path / "psql.log"
    psql_stub = bin_dir / "psql"
    psql_stub.write_text(
        r"""#!/usr/bin/env bash
if [ "${STUB_PSQL_DRAIN_STDIN:-0}" = "1" ]; then
  cat >/dev/null
fi
sql="${@: -1}"
if [ -n "${PSQL_LOG:-}" ]; then
  printf 'PGHOST=%s\tPGPORT=%s\tPGDATABASE=%s\tPGUSER=%s\tPGPASSWORD=%s\tPGSSLMODE=%s\tSQL=%s\n' \
    "${PGHOST-}" "${PGPORT-}" "${PGDATABASE-}" "${PGUSER-}" "${PGPASSWORD-}" "${PGSSLMODE-}" "$sql" >> "$PSQL_LOG"
fi
if [ -n "${STUB_PSQL_FAIL_TABLE:-}" ]; then
  case "$sql" in
    *"${STUB_PSQL_FAIL_TABLE}"*)
      printf 'f\n'
      exit 0
      ;;
  esac
fi
"""
        + "\n".join(_STUB_PSQL.splitlines()[1:])
        + "\n"
    )
    psql_stub.chmod(0o755)

    endpoints = tmp_path / "endpoints.json"
    endpoints.write_text(ENDPOINTS_JSON)

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["STUB_LOG"] = str(tmp_path / "databricks.log")
    env["STUB_ENDPOINTS_JSON"] = str(endpoints)
    env["LB_LOG"] = str(lb_log)
    env["PSQL_LOG"] = str(psql_log)
    env["PROJECT"] = "my-lakebase-project"
    env["BRANCH"] = "pr-42"
    env["PGUSER"] = "stub-user@example.com"
    env.pop("TF_VAR_logical_database", None)
    env.pop("PGDATABASE", None)
    return env, lb_log


def test_pr_validate_fails_when_first_table_fails_and_second_succeeds(tmp_path):
    """Finding 5 / Attacker 3: when table 1 (members) fails in liquibase update and table 2
    (providers) succeeds, pr-validate.yml must exit non-zero (even if invoked without external -e,
    because the step script itself sets -euo pipefail and || exit 1)."""
    work = _stage_repo(tmp_path)
    env, lb_log = _setup_stubs(
        tmp_path,
        """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$LB_LOG"
case "$*" in
  *members.changelog.sql*) exit 1 ;;
  *) exit 0 ;;
esac
""",
    )
    script = _extract_pr_validate_run_script()
    r = subprocess.run(["bash", "-c", script], cwd=str(work), env=env, capture_output=True, text=True)
    assert r.returncode != 0, (
        f"pr-validate.yml exited 0 despite table 1 failing:\nSTDOUT:{r.stdout}\nSTDERR:{r.stderr}"
    )


def test_pr_validate_isolates_stdin_across_tables(tmp_path):
    """Finding 5 / Attacker 3: if liquibase drains stdin on table 1 (members), table 2 (providers)
    must still run."""
    work = _stage_repo(tmp_path)
    env, lb_log = _setup_stubs(
        tmp_path,
        """#!/usr/bin/env bash
cat >/dev/null
printf '%s\\n' "$*" >> "$LB_LOG"
exit 0
""",
    )
    script = _extract_pr_validate_run_script()
    r = subprocess.run(["bash", "-e", "-c", script], cwd=str(work), env=env, capture_output=True, text=True)
    assert r.returncode == 0, f"pr-validate.yml failed:\n{r.stdout}\n{r.stderr}"
    calls = lb_log.read_text()
    assert "generated/members.changelog.sql" in calls, calls
    assert "generated/providers.changelog.sql" in calls, (
        f"table 2 (providers) was skipped because table 1 drained stdin:\n{calls}"
    )


@pytest.mark.parametrize("bad_db", ["db?sslmode=disable", "db sslmode=disable"])
def test_pr_validate_rejects_hostile_database_name_and_threads_custom_db(tmp_path, bad_db):
    """Attacker 2 (workflow path): pr-validate.yml threads the resolved database into the JDBC URL
    and rejects hostile database names before running liquibase."""
    work = _stage_repo(tmp_path)
    env, lb_log = _setup_stubs(
        tmp_path,
        """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$LB_LOG"
exit 0
""",
    )
    script = _extract_pr_validate_run_script()

    # Negative: hostile database name fails closed before liquibase runs
    env_bad = dict(env, TF_VAR_logical_database=bad_db)
    r_bad = subprocess.run(
        ["bash", "-e", "-c", script], cwd=str(work), env=env_bad, capture_output=True, text=True
    )
    assert r_bad.returncode != 0, f"pr-validate accepted hostile DB {bad_db!r}"
    assert not lb_log.exists() or lb_log.read_text() == "", (
        f"liquibase was invoked despite hostile DB {bad_db!r}: {lb_log.read_text()}"
    )

    # Positive: custom database name in terraform.tfvars reaches the JDBC URL
    (work / "terraform" / "terraform.tfvars").write_text('logical_database = "wf_custom_db"\n')
    r_ok = subprocess.run(
        ["bash", "-e", "-c", script], cwd=str(work), env=env, capture_output=True, text=True
    )
    assert r_ok.returncode == 0, f"pr-validate failed on valid custom DB:\n{r_ok.stdout}\n{r_ok.stderr}"
    assert f"jdbc:postgresql://{RW_HOST}:5432/wf_custom_db?sslmode=require" in lb_log.read_text()


def test_deploy_sh_fails_on_first_table_and_isolates_stdin_and_threads_db(tmp_path):
    """Item 5 & Item 2: scripts/deploy.sh must fail non-zero if table 1 fails in liquibase update,
    isolate stdin so table 1 cannot drain stdin, and thread the resolved database name (while
    rejecting hostile names)."""
    work = _stage_repo(tmp_path)
    # Stub wait_for_sync.sh so it exits 0 immediately
    (work / "scripts" / "wait_for_sync.sh").write_text("#!/usr/bin/env bash\nexit 0\n")

    # 1) Table 1 fails -> deploy.sh exits non-zero
    env_fail, lb_log_fail = _setup_stubs(
        tmp_path / "fail_case",
        """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$LB_LOG"
case "$*" in
  *members.changelog.sql*) exit 1 ;;
  *) exit 0 ;;
esac
""",
    )
    r_fail = subprocess.run(
        ["bash", str(work / "scripts" / "deploy.sh")],
        cwd=str(work), env=env_fail, capture_output=True, text=True,
    )
    assert r_fail.returncode != 0, "deploy.sh exited 0 when table 1 failed in liquibase update"

    # 2) Table 1 drains stdin -> table 2 still runs, and custom DB from tfvars is used
    (work / "terraform" / "terraform.tfvars").write_text('logical_database = "deploy_custom_db"\n')
    env_ok, lb_log_ok = _setup_stubs(
        tmp_path / "ok_case",
        """#!/usr/bin/env bash
cat >/dev/null
printf '%s\\n' "$*" >> "$LB_LOG"
exit 0
""",
    )
    r_ok = subprocess.run(
        ["bash", str(work / "scripts" / "deploy.sh")],
        cwd=str(work), env=env_ok, capture_output=True, text=True,
    )
    assert r_ok.returncode == 0, f"deploy.sh failed:\n{r_ok.stdout}\n{r_ok.stderr}"
    calls = lb_log_ok.read_text()
    assert "generated/members.changelog.sql" in calls, calls
    assert "generated/providers.changelog.sql" in calls, calls
    assert f"jdbc:postgresql://{RW_HOST}:5432/deploy_custom_db?sslmode=require" in calls, calls

    # 3) Hostile DB name in TF_VAR_logical_database (no tfvars override) -> deploy.sh exits non-zero
    (work / "terraform" / "terraform.tfvars").unlink(missing_ok=True)
    env_bad = dict(env_ok, TF_VAR_logical_database="db?sslmode=disable")
    r_bad = subprocess.run(
        ["bash", str(work / "scripts" / "deploy.sh")],
        cwd=str(work), env=env_bad, capture_output=True, text=True,
    )
    assert r_bad.returncode != 0, "deploy.sh accepted hostile database name db?sslmode=disable"


def test_deploy_sh_reads_database_from_terraform_output_after_apply(tmp_path):
    """Attacker 3: if Terraform applies with a value from -var-file / -var / state (which only
    `terraform -chdir=terraform output -raw logical_database` after apply knows), deploy.sh must
    read `DB` from `terraform output -raw logical_database` after `terraform apply` and validate it."""
    work = _stage_repo(tmp_path)
    (work / "scripts" / "wait_for_sync.sh").write_text("#!/usr/bin/env bash\nexit 0\n")
    env, lb_log = _setup_stubs(
        tmp_path / "tf_out_case",
        """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$LB_LOG"
exit 0
""",
    )
    # Stub terraform so `output -raw logical_database` prints `applied_via_var_file_db`
    tf_stub = Path(env["PATH"].split(os.pathsep)[0]) / "terraform"
    tf_stub.write_text(
        """#!/usr/bin/env bash
case "$*" in
  *"output -raw logical_database"*) printf 'applied_via_var_file_db\\n' ;;
  *) exit 0 ;;
esac
"""
    )
    tf_stub.chmod(0o755)

    r = subprocess.run(
        ["bash", str(work / "scripts" / "deploy.sh")],
        cwd=str(work), env=env, capture_output=True, text=True,
    )
    assert r.returncode == 0, f"deploy.sh failed:\n{r.stdout}\n{r.stderr}"
    calls = lb_log.read_text()
    assert f"jdbc:postgresql://{RW_HOST}:5432/applied_via_var_file_db?sslmode=require" in calls, (
        f"deploy.sh did not use `terraform output -raw logical_database` after apply:\n{calls}"
    )

    # And if `terraform output -raw logical_database` returns a hostile DB name, deploy.sh fails closed
    lb_log.write_text("")
    tf_stub.write_text(
        """#!/usr/bin/env bash
case "$*" in
  *"output -raw logical_database"*) printf 'db?sslmode=disable\\n' ;;
  *) exit 0 ;;
esac
"""
    )
    tf_stub.chmod(0o755)
    r_bad = subprocess.run(
        ["bash", str(work / "scripts" / "deploy.sh")],
        cwd=str(work), env=env, capture_output=True, text=True,
    )
    assert r_bad.returncode != 0, "deploy.sh accepted hostile database name from terraform output"
    assert lb_log.read_text() == "", "liquibase ran despite hostile terraform output database name"


def test_seed_source_sh_fails_on_bad_config_or_failed_statement_and_isolates_stdin(tmp_path):
    """Attacker 3: scripts/seed_source.sh must fail non-zero when CONFIG is invalid or when a SQL
    statement returns state=FAILED, and must isolate stdin so `databricks api post` cannot drain
    remaining statements."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    stmt_log = tmp_path / "stmts.log"

    # 1) Bad config path -> non-zero exit
    db_stub = bin_dir / "databricks"
    db_stub.write_text('#!/usr/bin/env bash\nprintf \'{"status":{"state":"SUCCEEDED"}}\\n\'\n')
    db_stub.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["WAREHOUSE_ID"] = "wh-stub"
    env["CONFIG"] = str(tmp_path / "nonexistent.json")
    r_bad_cfg = subprocess.run(
        ["bash", str(SEED_SH)], env=env, capture_output=True, text=True
    )
    assert r_bad_cfg.returncode != 0, (
        f"seed_source.sh exited 0 when CONFIG was missing:\n{r_bad_cfg.stdout}\n{r_bad_cfg.stderr}"
    )

    # 2) Statement 1 drains stdin -> all statements still run
    env["CONFIG"] = str(REPO_ROOT / "config" / "tables.json")
    env["STMT_LOG"] = str(stmt_log)
    db_stub.write_text(
        """#!/usr/bin/env bash
cat >/dev/null
printf '%s\\n' "$*" >> "$STMT_LOG"
printf '{"status":{"state":"SUCCEEDED"}}\\n'
"""
    )
    db_stub.chmod(0o755)
    r_stdin = subprocess.run(["bash", str(SEED_SH)], env=env, capture_output=True, text=True)
    assert r_stdin.returncode == 0, f"seed_source.sh failed:\n{r_stdin.stdout}\n{r_stdin.stderr}"
    lines = stmt_log.read_text().strip().splitlines()
    assert len(lines) == 7, f"expected 7 SQL statements across 2 tables, got {len(lines)}:\n{lines}"

    # 3) Statement returns FAILED -> seed_source.sh exits non-zero
    db_stub.write_text(
        """#!/usr/bin/env bash
printf '{"status":{"state":"FAILED","error":{"message":"simulated warehouse failure"}}}\\n'
"""
    )
    db_stub.chmod(0o755)
    r_stmt_fail = subprocess.run(["bash", str(SEED_SH)], env=env, capture_output=True, text=True)
    assert r_stmt_fail.returncode != 0, (
        f"seed_source.sh exited 0 when statement returned FAILED:\n{r_stmt_fail.stdout}"
    )


def test_pr_validate_fails_when_verify_fails_on_first_table_and_second_succeeds(tmp_path):
    """Attacker 1 & Attacker 2 (Issue 06 / Finding 9):
    When `liquibase update` succeeds on both tables (`members` and `providers`), but `verify_table`
    (`psql`) fails on table 1 (`members`) and would pass on table 2 (`providers`),
    `.github/workflows/pr-validate.yml` must exit non-zero — both under `set -euo pipefail` AND when
    `set -e` is stripped (`set -uo pipefail`), proving the verify call inside the loop carries
    `|| exit 1` so table 2's pass cannot hide table 1's failed verify."""
    work = _stage_repo(tmp_path)
    env, _lb_log = _setup_stubs(
        tmp_path,
        """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$LB_LOG"
exit 0
""",
    )
    env["STUB_PSQL_FAIL_TABLE"] = "members"
    script = _extract_pr_validate_run_script()

    # 1) Standard execution of the workflow step script
    r = subprocess.run(["bash", "-c", script], cwd=str(work), env=env, capture_output=True, text=True)
    assert r.returncode != 0, (
        f"pr-validate.yml exited 0 when verify_table failed on table 1 (members):\n"
        f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert "VERIFY FAILED for 'members'" in (r.stdout + r.stderr), (
        f"expected VERIFY FAILED for 'members' diagnostic, got:\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )

    # 2) Without `set -e` (`set -uo pipefail`), the loop's own `|| exit 1` on verify must still
    #    prevent table 2 (providers) from masking table 1's failure and exiting 0.
    script_no_errexit = script.replace("set -euo pipefail", "set -uo pipefail")
    r_no_e = subprocess.run(
        ["bash", "-c", script_no_errexit], cwd=str(work), env=env, capture_output=True, text=True
    )
    assert r_no_e.returncode != 0, (
        "pr-validate.yml verify call is missing `|| exit 1`: when `set -e` is inactive, table 2 "
        f"(providers) succeeded and hid table 1 (members) verify failure:\nSTDOUT:\n{r_no_e.stdout}\nSTDERR:\n{r_no_e.stderr}"
    )


def test_pr_validate_verify_isolates_stdin_across_tables(tmp_path):
    """Attacker 4 (Issue 06 / Finding 9):
    When `psql` (inside `verify_table`) greedily reads stdin (`cat >/dev/null`), `</dev/null`
    isolation on the verify call in `.github/workflows/pr-validate.yml` ensures both tables
    (`members` and `providers`) still run both `liquibase update` and `verify_table`."""
    work = _stage_repo(tmp_path)
    env, lb_log = _setup_stubs(
        tmp_path,
        """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$LB_LOG"
exit 0
""",
    )
    env["STUB_PSQL_DRAIN_STDIN"] = "1"
    script = _extract_pr_validate_run_script()

    r = subprocess.run(["bash", "-e", "-c", script], cwd=str(work), env=env, capture_output=True, text=True)
    assert r.returncode == 0, f"pr-validate.yml failed:\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"

    lb_calls = lb_log.read_text()
    assert "generated/members.changelog.sql" in lb_calls, lb_calls
    assert "generated/providers.changelog.sql" in lb_calls, (
        f"table 2 (providers) was skipped because verify/psql on table 1 drained stdin:\n{lb_calls}"
    )
    assert "VERIFY OK for 'members'" in r.stdout, r.stdout
    assert "VERIFY OK for 'providers'" in r.stdout, (
        f"verify did not run for table 2 (providers) after table 1 drained stdin:\n{r.stdout}"
    )


def test_pr_validate_and_deploy_invoke_verify_with_identical_args_and_pg_env(tmp_path):
    """Attacker 3, 5, 6 (Issue 06 / Finding 9):
    For a hostile multi-table `config/tables.json` fixture containing:
      - `members` with a custom `view_name` override (`members_custom_v` != derived `members_v`)
        and 3 `index_columns` (`member_id`, `plan_code`, `region_code`),
      - `providers` with no `view_name` override (derived `providers_v`) and 0 `index_columns` (`[]`),
      - and a custom `logical_database = "parity_custom_db"` in `terraform/terraform.tfvars`,
    `scripts/deploy.sh` and `.github/workflows/pr-validate.yml` must execute the exact same
    `verify_table` checks (`psql` queries) with the exact same `PG*` environment (`PGHOST`,
    `PGPORT=5432`, `PGDATABASE=parity_custom_db`, `PGUSER`, `PGPASSWORD`, `PGSSLMODE=require`)."""
    hostile_tables = [
        {
            "name": "members",
            "synced_table_id": "my_catalog.cicd_proj.members",
            "source_table_full_name": "my_catalog.cicd_proj.members_src",
            "primary_key_columns": ["id"],
            "app_schema": "cicd_proj",
            "app_role": "members_app_ro",
            # Hostile element 1: explicit view_name override distinct from derived members_v
            "view_name": "members_custom_v",
            # Hostile element 2: 3 index columns (tests >2 index columns)
            "index_columns": ["member_id", "plan_code", "region_code"],
        },
        {
            "name": "providers",
            "synced_table_id": "my_catalog.cicd_proj.providers",
            "source_table_full_name": "my_catalog.cicd_proj.providers_src",
            "primary_key_columns": ["id"],
            "app_schema": "cicd_proj",
            "app_role": "providers_app_ro",
            # Hostile element 3: 0 index columns + default derived view name (providers_v)
            "index_columns": [],
        },
    ]

    # --- Run 1: scripts/deploy.sh ---
    work_deploy = _stage_repo(tmp_path / "deploy_run")
    (work_deploy / "scripts" / "wait_for_sync.sh").write_text("#!/usr/bin/env bash\nexit 0\n")
    (work_deploy / "config" / "tables.json").write_text(json.dumps(hostile_tables, indent=2) + "\n")
    (work_deploy / "terraform" / "terraform.tfvars").write_text('logical_database = "parity_custom_db"\n')
    subprocess.run(
        [sys.executable, str(work_deploy / "liquibase" / "generate_changelogs.py")],
        cwd=str(work_deploy),
        check=True,
    )
    env_deploy, _ = _setup_stubs(
        tmp_path / "deploy_stubs",
        """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$LB_LOG"
exit 0
""",
    )
    env_deploy["PGUSER"] = "sp-client-id-parity"
    r_deploy = subprocess.run(
        ["bash", str(work_deploy / "scripts" / "deploy.sh")],
        cwd=str(work_deploy),
        env=env_deploy,
        capture_output=True,
        text=True,
    )
    assert r_deploy.returncode == 0, f"deploy.sh failed:\n{r_deploy.stdout}\n{r_deploy.stderr}"
    deploy_psql_lines = Path(env_deploy["PSQL_LOG"]).read_text().strip().splitlines()
    assert len(deploy_psql_lines) > 0, "deploy.sh produced 0 psql verify calls"

    # --- Run 2: .github/workflows/pr-validate.yml ---
    work_pr = _stage_repo(tmp_path / "pr_run")
    (work_pr / "config" / "tables.json").write_text(json.dumps(hostile_tables, indent=2) + "\n")
    (work_pr / "terraform" / "terraform.tfvars").write_text('logical_database = "parity_custom_db"\n')
    env_pr, _ = _setup_stubs(
        tmp_path / "pr_stubs",
        """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$LB_LOG"
exit 0
""",
    )
    # Hostile element 4: unset PGUSER and set DATABRICKS_CLIENT_ID as GitHub Actions does
    env_pr.pop("PGUSER", None)
    env_pr["DATABRICKS_CLIENT_ID"] = "sp-client-id-parity"
    script_pr = _extract_pr_validate_run_script().replace("stub-client-id", "sp-client-id-parity")
    r_pr = subprocess.run(
        ["bash", "-e", "-c", script_pr],
        cwd=str(work_pr),
        env=env_pr,
        capture_output=True,
        text=True,
    )
    assert r_pr.returncode == 0, f"pr-validate.yml failed:\n{r_pr.stdout}\n{r_pr.stderr}"
    assert Path(env_pr["PSQL_LOG"]).is_file(), "pr-validate.yml did not invoke psql (verify_table never ran)"
    pr_psql_lines = Path(env_pr["PSQL_LOG"]).read_text().strip().splitlines()

    # Exact parity: every single psql invocation and its PG* env in pr-validate.yml must equal deploy.sh
    assert pr_psql_lines == deploy_psql_lines, (
        "pr-validate.yml and deploy.sh invoked verify_table/psql with different args or PG* env!\n"
        f"pr-validate ({len(pr_psql_lines)} lines):\n"
        + "\n".join(pr_psql_lines)
        + f"\n\ndeploy.sh ({len(deploy_psql_lines)} lines):\n"
        + "\n".join(deploy_psql_lines)
    )
    # Spot-check that the custom view override, 3rd index column, and custom DB are actually in the log
    joined = "\n".join(pr_psql_lines)
    assert f"PGHOST={RW_HOST}\tPGPORT=5432\tPGDATABASE=parity_custom_db\tPGUSER=sp-client-id-parity\tPGPASSWORD={STUB_TOKEN}\tPGSSLMODE=require" in joined
    assert "members_custom_v" in joined
    assert "members_v" not in joined
    assert "idx_members_region_code" in joined
    assert "providers_v" in joined

