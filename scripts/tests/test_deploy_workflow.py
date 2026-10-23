"""Falsifiability tests for `.github/workflows/deploy.yml` and workflow-wide Terraform setup
(Issue 05 — The deploy workflow runs as shipped; Findings 6, 7, 8).

What this protects:
  1. A reader who sets the documented GitHub Actions secrets (`DATABRICKS_HOST`,
     `DATABRICKS_CLIENT_ID`, `DATABRICKS_CLIENT_SECRET`) and triggers `.github/workflows/deploy.yml`
     gets a working run or a clear failure — never an immediate crash on unbound `PGUSER`, never a
     failure looking for `[DEFAULT]` in a nonexistent `~/.databrickscfg`, and never corrupted
     `terraform output -raw` stdout from `hashicorp/setup-terraform`'s wrapper script.
  2. Every `hashicorp/setup-terraform` step across every workflow in `.github/workflows/*.yml`
     pins `terraform_version: "1.16.2"` and sets `terraform_wrapper: false`.

Attacker list:
  1. Env-replay of `deploy.yml`'s `./scripts/deploy.sh` step with `HOME` set to an empty temporary
     directory (no `~/.databrickscfg`), using stub `terraform`, `databricks`, `liquibase`, and
     `psql` binaries where `databricks` rejects `-p` / `--profile` when `$HOME/.databrickscfg` is
     missing (matching real Databricks CLI behavior on a fresh GitHub Actions runner):
     - `deploy.sh` reaches `terraform apply`,
     - runs the real `scripts/wait_for_sync.sh` and `lakebase_resolve_and_mint` with zero `-p`
       flags,
     - passes `PGUSER` equal to the service principal client ID (`DATABRICKS_CLIENT_ID`) to
       Liquibase and `psql`,
     - and exits `0`.
  2. `wait_for_sync.sh` with `PROFILE=""` (no `-p` in the CLI call) and `PROFILE="x"` (`-p x`).
     (Also covered directly in `scripts/tests/test_wait_for_sync.py` and exercised end-to-end here
     by running the real `scripts/wait_for_sync.sh` during the `deploy.yml` env-replay.)
  3. Static YAML check across all `.github/workflows/*.yml`:
     - Every `hashicorp/setup-terraform` step in every workflow (`ci.yml`, `deploy.yml`,
       `pr-validate.yml`) sets `with.terraform_version == "1.16.2"` and
       `with.terraform_wrapper is False`.
     - `deploy.yml`'s `deploy` job / `./scripts/deploy.sh` step sets `PGUSER` (referencing
       `secrets.DATABRICKS_CLIENT_ID`) and `PROFILE: ""` (empty string).
  4. Simulated `hashicorp/setup-terraform` wrapper stdout (`[command]/path/to/terraform output -raw
     logical_database\\ndatabricks_postgres\\n::debug::Terraform exited with code 0.`):
     - Capturing `$(terraform -chdir=... output -raw logical_database)` when the wrapper is active
       pollutes `DB` with wrapper lines, and `deploy.sh` fails closed (`invalid database name`)
       before running `wait_for_sync.sh` or `liquibase`.
  5. Hostile provider config check (`terraform/versions.tf`):
     - `provider "databricks"` sets `profile = var.databricks_profile != "" ? var.databricks_profile : null`
       so passing `TF_VAR_databricks_profile=""` in CI leaves `profile` null (env-var auth) instead
       of configuring an empty profile string `""`.

Named mutations the reviewer will run:
  - Mutation A: remove `PGUSER` from `.github/workflows/deploy.yml` ->
    `test_attacker_1_deploy_workflow_env_replay_reaches_apply_with_empty_home` and
    `test_attacker_3_all_workflows_pin_terraform_and_deploy_sets_pguser_and_empty_profile` go RED.
  - Mutation B: set `PROFILE: DEFAULT` in `.github/workflows/deploy.yml` (or revert `-p` omission
    in `scripts/wait_for_sync.sh`) ->
    `test_attacker_1_deploy_workflow_env_replay_reaches_apply_with_empty_home`,
    `test_attacker_3_all_workflows_pin_terraform_and_deploy_sets_pguser_and_empty_profile`, and
    `test_empty_profile_omits_p_flag_and_named_profile_passes_p_flag` go RED.
  - Mutation C: set `terraform_wrapper: true` or omit `terraform_version: "1.16.2"` on any
    `hashicorp/setup-terraform` step ->
    `test_attacker_3_all_workflows_pin_terraform_and_deploy_sets_pguser_and_empty_profile` goes RED.
  - Mutation D (Issue 13): drop `--exit-code 1` (or change to `--exit-code 0`), change subcommand
    from `git`, change target from `.`, swap `sha256sum -c -` after `tar -xzf` or after scan, add
    `|| true` / `set +e` / `continue-on-error: true`, or remove `fetch-depth: 0` from `secret-scan`
    checkout in `.github/workflows/ci.yml` ->
    `test_attacker_6_ci_supply_chain_sha_pinning_no_curl_pipe_sh_version_parity_and_permissions`
    and `test_attacker_7_gitleaks_step_hostile_mutations_go_red` go RED.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Callable

import pytest
import yaml

from test_deploy_target_parity import _PRECEDENCE_TF_STUB
from test_endpoint_credential import ENDPOINTS_JSON, RW_HOST, STUB_TOKEN
from test_verify_step import _STUB_PSQL

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
DEPLOY_YML = WORKFLOWS_DIR / "deploy.yml"


def _stage_repo(tmp_path: Path) -> Path:
    work = tmp_path / "repo"
    work.mkdir(parents=True, exist_ok=True)
    for rel in ("config", "dabs", "liquibase", "scripts", "terraform"):
        shutil.copytree(REPO_ROOT / rel, work / rel)
    return work


def _resolve_gha_expr(raw: object, secrets: dict[str, str]) -> str:
    """Resolve GitHub Actions `${{ secrets.X }}` expressions in a workflow env value."""
    if raw is None:
        return ""
    val = str(raw)

    def _sub(match: re.Match[str]) -> str:
        expr = match.group(1).strip()
        if expr.startswith("secrets."):
            key = expr.split(".", 1)[1].strip()
            if key in secrets:
                return secrets[key]
        raise AssertionError(f"Unsupported or unknown GitHub Actions expression in deploy.yml: {expr!r}")

    return re.sub(r"\$\{\{\s*([^}]+?)\s*\}\}", _sub, val)


def _extract_deploy_step_env(secrets: dict[str, str]) -> dict[str, str]:
    """Parse `.github/workflows/deploy.yml` and return the merged job + step `env` for the
    `./scripts/deploy.sh` step, exactly as GitHub Actions constructs it."""
    doc = yaml.safe_load(DEPLOY_YML.read_text())
    job = doc["jobs"]["deploy"]
    merged: dict[str, str] = {}
    for k, v in (job.get("env") or {}).items():
        merged[str(k)] = _resolve_gha_expr(v, secrets)
    for step in job.get("steps") or []:
        run_cmd = (step.get("run") or "").strip()
        if "./scripts/deploy.sh" in run_cmd:
            for k, v in (step.get("env") or {}).items():
                merged[str(k)] = _resolve_gha_expr(v, secrets)
            return merged
    raise AssertionError("Could not find `./scripts/deploy.sh` step in .github/workflows/deploy.yml")


def test_attacker_1_deploy_workflow_env_replay_reaches_apply_with_empty_home(tmp_path):
    """Attacker 1: Replay `deploy.yml`'s exact `env:` for `./scripts/deploy.sh` with `HOME` set to
    an empty temp directory (no `~/.databrickscfg`) and a hostile `databricks` CLI stub that fails
    immediately if `-p` or `--profile` is passed when `$HOME/.databrickscfg` does not exist.
    Also keeps the REAL `scripts/wait_for_sync.sh` in place so both `wait_for_sync.sh` and
    `lakebase_resolve_and_mint` are exercised end-to-end."""
    work = _stage_repo(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    empty_home = tmp_path / "empty_home"
    empty_home.mkdir(parents=True, exist_ok=True)

    tf_apply_log = tmp_path / "tf_apply.json"
    db_log = tmp_path / "databricks.log"
    lb_log = tmp_path / "liquibase.log"
    endpoints = tmp_path / "endpoints.json"
    endpoints.write_text(ENDPOINTS_JSON)

    # Stub terraform with real variable precedence
    tf_stub = bin_dir / "terraform"
    tf_stub.write_text(_PRECEDENCE_TF_STUB)
    tf_stub.chmod(0o755)

    # Hostile stub databricks: rejects -p / --profile when $HOME/.databrickscfg is missing!
    db_stub = bin_dir / "databricks"
    db_stub.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "$STUB_LOG"
for arg in "$@"; do
  if [ "$arg" = "-p" ] || [ "$arg" = "--profile" ]; then
    if [ ! -f "$HOME/.databrickscfg" ]; then
      echo "Error: cannot resolve profile: $HOME/.databrickscfg does not exist (got args: $*)" >&2
      exit 1
    fi
  fi
done
group="${{1:-}}"; sub="${{2:-}}"
case "$group $sub" in
  "postgres get-synced-table")
    printf '%s\\n' '{{"status":{{"detailed_state":"SYNCED_TABLE_ONLINE"}}}}'
    ;;
  "postgres list-endpoints")
    cat "$STUB_ENDPOINTS_JSON"
    ;;
  "postgres generate-database-credential")
    printf '%s\\n' '{{"token":"{STUB_TOKEN}"}}'
    ;;
  *)
    printf '%s\\n' '{{}}'
    ;;
esac
"""
    )
    db_stub.chmod(0o755)

    lb_stub = bin_dir / "liquibase"
    lb_stub.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "$LB_LOG"\nexit 0\n')
    lb_stub.chmod(0o755)

    psql_stub = bin_dir / "psql"
    psql_stub.write_text(_STUB_PSQL)
    psql_stub.chmod(0o755)

    secrets = {
        "DATABRICKS_HOST": "https://fake-workspace.cloud.databricks.com",
        "DATABRICKS_CLIENT_ID": "00000000-1111-2222-3333-444444444444",
        "DATABRICKS_CLIENT_SECRET": "fake-sp-oauth-secret",
    }
    wf_env = _extract_deploy_step_env(secrets)

    # Start from a clean environment that has NO ambient PGUSER, PROFILE, PROJECT, BRANCH, or TF_VAR_*
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("TF_VAR_")
        and not k.startswith("DATABRICKS_")
        and k not in ("PGUSER", "PROFILE", "PROJECT", "BRANCH", "PGDATABASE")
    }
    env["HOME"] = str(empty_home)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["TF_APPLY_ENV_LOG"] = str(tf_apply_log)
    env["STUB_LOG"] = str(db_log)
    env["STUB_ENDPOINTS_JSON"] = str(endpoints)
    env["LB_LOG"] = str(lb_log)
    env["WAIT_INTERVAL"] = "1"
    env.update(wf_env)

    r = subprocess.run(
        ["bash", str(work / "scripts" / "deploy.sh")],
        cwd=str(work),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert r.returncode == 0, (
        f"deploy.sh failed under deploy.yml env with empty HOME:\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )

    # 1. Reached terraform apply and passed TF_VAR_databricks_profile=""
    assert tf_apply_log.is_file(), "deploy.sh did not reach `terraform apply`"
    tf_env = json.loads(tf_apply_log.read_text())
    assert tf_env["TF_VAR_databricks_profile"] == "", (
        f"expected TF_VAR_databricks_profile='' in CI, got {tf_env['TF_VAR_databricks_profile']!r}"
    )

    # 2. Every databricks CLI call (get-synced-table, list-endpoints, generate-database-credential)
    #    omitted `-p`
    db_calls = db_log.read_text().strip().splitlines()
    assert len(db_calls) >= 6, f"expected >= 6 databricks CLI calls across 2 tables, got:\n{db_calls}"
    for line in db_calls:
        assert "-p" not in line.split() and "--profile" not in line.split(), (
            f"databricks CLI call unexpectedly included -p/--profile in CI mode: {line!r}"
        )

    # 3. Liquibase received --username=<SP client ID> and JDBC URL for RW_HOST
    lb_calls = lb_log.read_text()
    assert f"--username={secrets['DATABRICKS_CLIENT_ID']}" in lb_calls, lb_calls
    assert f"jdbc:postgresql://{RW_HOST}:5432/databricks_postgres?sslmode=require" in lb_calls, lb_calls


def test_attacker_3_all_workflows_pin_terraform_and_deploy_sets_pguser_and_empty_profile():
    """Attacker 3: Static YAML check across every `.github/workflows/*.yml` file:
    1. Every `hashicorp/setup-terraform` step in every workflow pins `terraform_version: "1.16.2"`
       and sets `terraform_wrapper: false`.
    2. `.github/workflows/deploy.yml` sets `PGUSER: ${{ secrets.DATABRICKS_CLIENT_ID }}` and
       `PROFILE: ""` (empty string) for `./scripts/deploy.sh`."""
    workflow_files = sorted(WORKFLOWS_DIR.glob("*.yml"))
    assert len(workflow_files) >= 4, f"expected >= 4 workflow files, found {workflow_files}"

    setup_tf_steps_found = 0
    for wf_path in workflow_files:
        doc = yaml.safe_load(wf_path.read_text())
        for job_name, job in (doc.get("jobs") or {}).items():
            for idx, step in enumerate(job.get("steps") or []):
                uses = str(step.get("uses") or "")
                if uses.startswith("hashicorp/setup-terraform"):
                    setup_tf_steps_found += 1
                    with_block = step.get("with") or {}
                    assert with_block.get("terraform_version") == "1.16.2", (
                        f"{wf_path.name} job {job_name!r} step #{idx + 1} ({uses}) must pin "
                        f'terraform_version: "1.16.2", got {with_block.get("terraform_version")!r}'
                    )
                    assert with_block.get("terraform_wrapper") is False, (
                        f"{wf_path.name} job {job_name!r} step #{idx + 1} ({uses}) must set "
                        f"terraform_wrapper: false, got {with_block.get('terraform_wrapper')!r}"
                    )

    # ci.yml (2 steps), deploy.yml (1 step), pr-validate.yml (1 step) => at least 4 steps total
    assert setup_tf_steps_found >= 4, f"expected >= 4 setup-terraform steps, found {setup_tf_steps_found}"

    # Verify deploy.yml env settings specifically
    secrets = {
        "DATABRICKS_HOST": "https://fake.cloud.databricks.com",
        "DATABRICKS_CLIENT_ID": "sp-client-id-123",
        "DATABRICKS_CLIENT_SECRET": "sp-secret-456",
    }
    deploy_env = _extract_deploy_step_env(secrets)
    assert deploy_env.get("PROFILE") == "", (
        f"deploy.yml must set PROFILE to empty string '' for CI env-var auth, got {deploy_env.get('PROFILE')!r}"
    )
    assert deploy_env.get("PGUSER") == "sp-client-id-123", (
        f"deploy.yml must set PGUSER from secrets.DATABRICKS_CLIENT_ID, got {deploy_env.get('PGUSER')!r}"
    )

    # Verify pr-validate.yml sources scripts/verify_table.sh, exports PG* env vars, and invokes
    # verify_table_from_row (or verify_table) with </dev/null and || exit 1 after liquibase update.
    pr_doc = yaml.safe_load((WORKFLOWS_DIR / "pr-validate.yml").read_text())
    pr_steps = pr_doc["jobs"]["validate"]["steps"]
    pr_run = ""
    for step in pr_steps:
        if "Create ephemeral CoW branch" in str(step.get("name") or ""):
            pr_run = str(step.get("run") or "")
            break
    assert pr_run, "Create ephemeral CoW branch step not found in pr-validate.yml"
    non_comment_lines = [
        ln.strip() for ln in pr_run.splitlines() if ln.strip() and not ln.strip().startswith("#")
    ]
    non_comment_body = "\n".join(non_comment_lines)
    assert "scripts/verify_table.sh" in non_comment_body, (
        "pr-validate.yml must source scripts/verify_table.sh"
    )
    for pg_var in ("PGHOST=", "PGPORT=5432", "PGDATABASE=", "PGUSER=", "PGPASSWORD=", "PGSSLMODE=require"):
        assert pg_var in non_comment_body, (
            f"pr-validate.yml must export {pg_var} before running verify:\n{non_comment_body}"
        )
    verify_call_lines = [
        ln for ln in non_comment_lines if re.match(r"verify_table(_from_row)?\b", ln)
    ]
    assert verify_call_lines, (
        "pr-validate.yml must invoke verify_table_from_row (or verify_table) for each table after Liquibase"
    )
    for vline in verify_call_lines:
        assert "</dev/null" in vline and "|| exit 1" in vline, (
            f"pr-validate.yml verify call must isolate stdin (</dev/null) and fail fast (|| exit 1): {vline!r}"
        )


def test_attacker_4_setup_terraform_wrapper_stdout_corrupts_database_output_and_fails_closed(tmp_path):
    """Attacker 4: Simulate `hashicorp/setup-terraform` with `terraform_wrapper: true` (the default
    when omitted). The wrapper script prints `[command]/path/to/terraform ...` and `::debug::...`
    lines to stdout around the real `terraform output -raw logical_database` output.
    Verify that `$(terraform -chdir=... output -raw logical_database)` captures those extra lines
    and `deploy.sh` fails closed (`invalid database name`) before running `wait_for_sync.sh` or
    `liquibase`."""
    work = _stage_repo(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    lb_log = tmp_path / "liquibase.log"
    wait_log = tmp_path / "wait.log"

    (work / "scripts" / "wait_for_sync.sh").write_text(
        f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "{wait_log}"\nexit 0\n'
    )
    (work / "scripts" / "wait_for_sync.sh").chmod(0o755)

    # Simulate the hashicorp/setup-terraform wrapper script on `output -raw`
    tf_wrapper_stub = bin_dir / "terraform"
    tf_wrapper_stub.write_text(
        """#!/usr/bin/env bash
case "$*" in
  *"output -raw logical_database"*)
    printf '[command]/usr/local/bin/terraform %s\\n' "$*"
    printf 'databricks_postgres\\n'
    printf '::debug::Terraform exited with code 0.\\n'
    exit 0
    ;;
  *)
    exit 0
    ;;
esac
"""
    )
    tf_wrapper_stub.chmod(0o755)

    lb_stub = bin_dir / "liquibase"
    lb_stub.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "{lb_log}"\nexit 0\n')
    lb_stub.chmod(0o755)

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["PGUSER"] = "00000000-1111-2222-3333-444444444444"
    env["PROFILE"] = ""

    r = subprocess.run(
        ["bash", str(work / "scripts" / "deploy.sh")],
        cwd=str(work),
        env=env,
        capture_output=True,
        text=True,
    )
    assert r.returncode != 0, (
        f"deploy.sh unexpectedly succeeded when terraform wrapper polluted stdout:\n{r.stdout}\n{r.stderr}"
    )
    assert "invalid database name" in r.stderr.lower(), (
        f"expected `invalid database name` from lakebase_validate_database, got:\n{r.stderr}"
    )
    assert "[command]/usr/local/bin/terraform" in r.stderr, r.stderr
    assert not wait_log.exists(), "wait_for_sync.sh must not run when terraform output is corrupted"
    assert not lb_log.exists(), "liquibase must not run when terraform output is corrupted"


def test_attacker_5_terraform_provider_maps_empty_profile_to_null():
    """When `PROFILE=""` in CI, `deploy.sh` exports `TF_VAR_databricks_profile=""`.
    `terraform/versions.tf` must map an empty `var.databricks_profile` to `null`
    (`var.databricks_profile != "" ? var.databricks_profile : null`) so the Databricks Terraform
    provider uses `DATABRICKS_*` environment variables instead of looking up an empty profile name."""
    versions_tf = (REPO_ROOT / "terraform" / "versions.tf").read_text()
    assert 'var.databricks_profile != "" ? var.databricks_profile : null' in versions_tf, (
        "terraform/versions.tf must map empty var.databricks_profile to null so CI env-var auth works:\n"
        + versions_tf
    )


def _join_continued_lines(script: str) -> list[str]:
    """Return active (non-empty, non-comment) shell statements after joining `\\`-continued lines."""
    statements: list[str] = []
    buf = ""
    for raw_line in script.splitlines():
        stripped = raw_line.strip()
        if not buf and (not stripped or stripped.startswith("#")):
            continue
        if stripped.endswith("\\"):
            buf += stripped[:-1].rstrip() + " "
        else:
            buf += stripped
            if buf.strip():
                statements.append(buf.strip())
            buf = ""
    if buf.strip():
        statements.append(buf.strip())
    return statements


def _check_ci_gitleaks_step(ci_path: Path) -> None:
    """Validate `.github/workflows/ci.yml`'s `secret-scan` job and `gitleaks` step (Issue 13):
    1. `secret-scan` job exists and does not set `continue-on-error`.
    2. `actions/checkout` step runs before `gitleaks` and sets `fetch-depth: 0` (full git history
       so `gitleaks git` scans all commits in a PR branch, not a shallow 1-commit clone).
    3. `gitleaks` step does not set `continue-on-error`, pins `GITLEAKS_VERSION: "8.30.1"` and
       `GITLEAKS_SHA256`, and does not use `set +e`, `|| true`, or `|| :`.
    4. Step ordering inside `run:` is strictly `curl` -> `sha256sum -c -` -> `tar -xzf` ->
       `"$RUNNER_TEMP/gitleaks"` scan (last statement).
    5. Scan command invokes `"$RUNNER_TEMP/gitleaks" git` with `--redact`, `--no-banner`,
       `--exit-code 1`, and repo root target `.`."""
    doc = yaml.safe_load(ci_path.read_text())
    jobs = doc.get("jobs") or {}
    secret_job = jobs.get("secret-scan")
    assert isinstance(secret_job, dict), f"{ci_path.name} must define a `secret-scan` job"

    job_coe = secret_job.get("continue-on-error")
    assert job_coe in (None, False) and not bool(job_coe), (
        f"{ci_path.name} secret-scan job must not set continue-on-error, got {job_coe!r}"
    )
    assert "if" not in secret_job, (
        f"{ci_path.name} secret-scan job must not set `if:` (must run unconditionally), got {secret_job.get('if')!r}"
    )
    assert "defaults" not in doc and "defaults" not in secret_job, (
        f"{ci_path.name} must not override `defaults.run.shell`"
    )

    steps = secret_job.get("steps") or []
    checkout_idx: int | None = None
    checkout_step: dict[str, object] | None = None
    gitleaks_idx: int | None = None
    gitleaks_step: dict[str, object] | None = None

    for idx, step in enumerate(steps):
        uses = str(step.get("uses") or "")
        if uses.startswith("actions/checkout@") and checkout_idx is None:
            checkout_idx = idx
            checkout_step = step
        if step.get("name") == "gitleaks" and gitleaks_idx is None:
            gitleaks_idx = idx
            gitleaks_step = step

    assert checkout_idx is not None and checkout_step is not None, (
        f"{ci_path.name} secret-scan job must have an `actions/checkout` step"
    )
    assert gitleaks_idx is not None and gitleaks_step is not None, (
        f"{ci_path.name} secret-scan job must have a `gitleaks` step"
    )
    assert checkout_idx < gitleaks_idx, (
        f"{ci_path.name} secret-scan `actions/checkout` must run before `gitleaks` step"
    )
    assert "if" not in checkout_step, (
        f"{ci_path.name} secret-scan checkout step must not set `if:`, got {checkout_step.get('if')!r}"
    )

    with_block = checkout_step.get("with")
    fetch_depth = with_block.get("fetch-depth") if isinstance(with_block, dict) else None
    assert fetch_depth == 0 and not isinstance(fetch_depth, bool), (
        f"{ci_path.name} secret-scan checkout step must set `fetch-depth: 0` so `gitleaks git` "
        f"scans full commit history, got {fetch_depth!r}"
    )

    step_coe = gitleaks_step.get("continue-on-error")
    assert step_coe in (None, False) and not bool(step_coe), (
        f"{ci_path.name} gitleaks step must not set continue-on-error, got {step_coe!r}"
    )
    assert "if" not in gitleaks_step, (
        f"{ci_path.name} gitleaks step must not set `if:` (must run unconditionally), got {gitleaks_step.get('if')!r}"
    )
    assert "shell" not in gitleaks_step, (
        f"{ci_path.name} gitleaks step must not override `shell:` (default `bash -e {{0}}` required), "
        f"got {gitleaks_step.get('shell')!r}"
    )
    assert "uses" not in gitleaks_step, (
        f"{ci_path.name} gitleaks step must run checksum-verified binary, not gitleaks-action"
    )

    gl_env = gitleaks_step.get("env")
    assert isinstance(gl_env, dict), f"{ci_path.name} gitleaks step must define `env:`"
    assert gl_env.get("GITLEAKS_VERSION") == "8.30.1", (
        f"{ci_path.name} gitleaks step must pin GITLEAKS_VERSION='8.30.1', got {gl_env.get('GITLEAKS_VERSION')!r}"
    )
    assert gl_env.get("GITLEAKS_SHA256") == (
        "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb"
    ), (
        f"{ci_path.name} gitleaks step must pin expected GITLEAKS_SHA256, got {gl_env.get('GITLEAKS_SHA256')!r}"
    )

    run_script = str(gitleaks_step.get("run") or "")
    statements = _join_continued_lines(run_script)
    assert statements, f"{ci_path.name} gitleaks step `run:` is empty"

    for stmt in statements:
        # Any `+` flag group holding `e` (`+e`, `+eu`, `+xe`, `+x +e`) or `+o errexit` turns errexit off.
        assert not re.search(
            r"(?:^|[;\s])set\b[^;|&\n]*?\s(?:\+[A-Za-z]*e[A-Za-z]*\b|\+o\s+errexit\b)", stmt
        ), (
            f"{ci_path.name} gitleaks step must not use `set +e` / `set +o errexit`: {stmt!r}"
        )
        assert "||" not in stmt, (
            f"{ci_path.name} gitleaks step must not use `||` to suppress failure or mask exit code: {stmt!r}"
        )

    curl_indices = [i for i, s in enumerate(statements) if re.match(r"^curl\b", s)]
    sha_indices = [
        i
        for i, s in enumerate(statements)
        if "sha256sum -c -" in s and "${GITLEAKS_SHA256}" in s and "$RUNNER_TEMP/$archive" in s
    ]
    tar_indices = [i for i, s in enumerate(statements) if re.match(r"^tar\b", s) and "-xzf" in s]
    scan_indices = [
        i for i, s in enumerate(statements) if s.startswith('"$RUNNER_TEMP/gitleaks"') or s.startswith("$RUNNER_TEMP/gitleaks")
    ]

    assert len(curl_indices) == 1, f"expected 1 curl statement in gitleaks step, got {statements}"
    assert len(sha_indices) == 1, f"expected 1 sha256sum -c - statement in gitleaks step, got {statements}"
    assert len(tar_indices) == 1, f"expected 1 tar -xzf statement in gitleaks step, got {statements}"
    assert len(scan_indices) == 1, f"expected 1 gitleaks scan statement in gitleaks step, got {statements}"

    curl_idx, sha_idx, tar_idx, scan_idx = curl_indices[0], sha_indices[0], tar_indices[0], scan_indices[0]
    assert curl_idx < sha_idx < tar_idx < scan_idx, (
        f"{ci_path.name} gitleaks step must verify sha256sum after curl and before tar extraction "
        f"and scan (got indices curl={curl_idx}, sha256sum={sha_idx}, tar={tar_idx}, scan={scan_idx})"
    )
    assert scan_idx == len(statements) - 1, (
        f"{ci_path.name} gitleaks scan must be the final command in `run:` so its exit code is not masked"
    )

    scan_stmt = statements[scan_idx]
    assert not any(op in scan_stmt for op in ("||", "&&", ";", "|")), (
        f"{ci_path.name} gitleaks scan command must not suppress failure or mask exit code: {scan_stmt!r}"
    )

    tokens = shlex.split(scan_stmt)
    assert tokens and tokens[0] == "$RUNNER_TEMP/gitleaks", (
        f"gitleaks binary path must be $RUNNER_TEMP/gitleaks, got {tokens!r}"
    )
    assert len(tokens) >= 2 and tokens[1] == "git", (
        f"gitleaks subcommand must be 'git', got {tokens[1] if len(tokens) >= 2 else None!r} in {scan_stmt!r}"
    )

    rest = tokens[2:]
    assert "--redact" in rest, f"gitleaks scan must include --redact, got {scan_stmt!r}"
    assert "--no-banner" in rest, f"gitleaks scan must include --no-banner, got {scan_stmt!r}"

    exit_code_val: str | None = None
    positional: list[str] = []
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok == "--exit-code":
            assert i + 1 < len(rest), f"--exit-code missing value in {scan_stmt!r}"
            exit_code_val = rest[i + 1]
            i += 2
        elif tok.startswith("--exit-code="):
            exit_code_val = tok.split("=", 1)[1]
            i += 1
        elif tok.startswith("-"):
            i += 1
        else:
            positional.append(tok)
            i += 1

    assert exit_code_val == "1", (
        f"gitleaks scan must pass `--exit-code 1` so leaks fail CI, got exit_code={exit_code_val!r} in {scan_stmt!r}"
    )
    assert positional == ["."] and tokens[-1] == ".", (
        f"gitleaks scan target must be repo root `.`, got positional={positional!r} in {scan_stmt!r}"
    )


def _check_workflows_supply_chain(workflow_paths: list[Path]) -> None:
    """Validate supply-chain pinning, tool version parity, least-privilege permissions, and no
    curl/wget pipe to shell across the given workflow YAML files."""
    assert len(workflow_paths) >= 4, f"expected >= 4 workflow files, got {workflow_paths}"

    uses_sha_re = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[0-9a-f]{40}$")
    raw_uses_line_re = re.compile(
        r"^\s*(?:-\s*)?uses:\s*([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[0-9a-f]{40})\s+#\s*v\d+\.\d+(?:\.\d+)?\b"
    )
    pipe_shell_re = re.compile(
        r"\b(?:curl|wget)\b[^\n|]*\|\s*(?:sudo\s+)?(?:ba|z|da|k)?sh\b"
    )
    gha_interpolation_re = re.compile(r"\$\{\{.*?\}\}")

    shas_by_action: dict[str, set[str]] = {}
    counts_by_action: dict[str, int] = {}

    for wf_path in workflow_paths:
        raw_text = wf_path.read_text()
        doc = yaml.safe_load(raw_text)

        # Attacker 4: Least-privilege top-level permissions: {contents: read} and no job-level write
        top_perms = doc.get("permissions")
        assert top_perms == {"contents": "read"}, (
            f"{wf_path.name} must set top-level `permissions: {{contents: read}}`, got {top_perms!r}"
        )

        # Raw line check for every `uses:` line to ensure `# vX.Y.Z` version comment is present
        raw_uses_lines = [
            (lineno, line)
            for lineno, line in enumerate(raw_text.splitlines(), start=1)
            if re.match(r"^\s*(?:-\s*)?uses:\s*", line)
        ]
        assert len(raw_uses_lines) >= 1, f"{wf_path.name} has no `uses:` lines"
        for lineno, line in raw_uses_lines:
            assert raw_uses_line_re.match(line), (
                f"{wf_path.name}:{lineno} `uses:` must be pinned to a 40-char SHA with `# vX.Y.Z` comment, "
                f"got: {line.strip()!r}"
            )

        jobs = doc.get("jobs") or {}
        assert jobs, f"{wf_path.name} has no jobs"
        for job_name, job in jobs.items():
            job_perms = job.get("permissions")
            if job_perms is not None:
                assert job_perms == {"contents": "read"}, (
                    f"{wf_path.name} job {job_name!r} must not broaden permissions beyond contents: read, "
                    f"got {job_perms!r}"
                )

            for idx, step in enumerate(job.get("steps") or []):
                uses = step.get("uses")
                if uses is not None:
                    uses_str = str(uses).strip()
                    assert uses_sha_re.match(uses_str), (
                        f"{wf_path.name} job {job_name!r} step #{idx + 1} `uses:` must match "
                        f"<owner>/<repo>@<40-hex-sha>, got {uses_str!r}"
                    )
                    action_repo, sha = uses_str.split("@", 1)
                    shas_by_action.setdefault(action_repo, set()).add(sha)
                    counts_by_action[action_repo] = counts_by_action.get(action_repo, 0) + 1

                    with_block = step.get("with") or {}
                    if action_repo == "hashicorp/setup-terraform":
                        assert with_block.get("terraform_version") == "1.16.2", (
                            f"{wf_path.name} job {job_name!r} step #{idx + 1} must set "
                            f'terraform_version: "1.16.2", got {with_block.get("terraform_version")!r}'
                        )
                        assert with_block.get("terraform_wrapper") is False, (
                            f"{wf_path.name} job {job_name!r} step #{idx + 1} must set "
                            f"terraform_wrapper: false, got {with_block.get('terraform_wrapper')!r}"
                        )
                    elif action_repo == "liquibase/setup-liquibase":
                        assert with_block.get("version") == "4.33.0", (
                            f"{wf_path.name} job {job_name!r} step #{idx + 1} must set "
                            f'liquibase version: "4.33.0", got {with_block.get("version")!r}'
                        )
                        assert with_block.get("edition") == "community", (
                            f"{wf_path.name} job {job_name!r} step #{idx + 1} must set "
                            f'liquibase edition: "community", got {with_block.get("edition")!r}'
                        )
                    elif action_repo == "databricks/setup-cli":
                        assert with_block.get("version") == "1.19.0", (
                            f"{wf_path.name} job {job_name!r} step #{idx + 1} must set "
                            f'databricks/setup-cli version: "1.19.0", got {with_block.get("version")!r}'
                        )
                    elif action_repo == "actions/setup-python":
                        assert with_block.get("python-version") == "3.11", (
                            f"{wf_path.name} job {job_name!r} step #{idx + 1} must set "
                            f'python-version: "3.11", got {with_block.get("python-version")!r}'
                        )

                run_script = step.get("run")
                if run_script is not None:
                    run_str = str(run_script)
                    assert not pipe_shell_re.search(run_str), (
                        f"{wf_path.name} job {job_name!r} step #{idx + 1} pipes curl/wget to shell:\n{run_str}"
                    )
                    assert not gha_interpolation_re.search(run_str), (
                        f"{wf_path.name} job {job_name!r} step #{idx + 1} interpolates `${{{{ ... }}}}` "
                        f"directly inside `run:` (move to `env:` to prevent script injection and "
                        f"semgrep PartialParsing):\n{run_str}"
                    )

        if wf_path.name == "ci.yml":
            _check_ci_gitleaks_step(wf_path)

    # Attacker 3: Every workflow uses the exact same commit SHA for each action repository
    expected_min_counts = {
        "actions/checkout": 6,
        "actions/setup-python": 2,
        "hashicorp/setup-terraform": 4,
        "liquibase/setup-liquibase": 3,
        "databricks/setup-cli": 4,
    }
    for action_repo, min_count in expected_min_counts.items():
        actual_count = counts_by_action.get(action_repo, 0)
        assert actual_count >= min_count, (
            f"expected at least {min_count} `{action_repo}` steps across workflows, found {actual_count}"
        )
        shas = shas_by_action.get(action_repo, set())
        assert len(shas) == 1, (
            f"SHA drift across workflows for `{action_repo}`: found multiple SHAs {sorted(shas)}"
        )


def test_attacker_6_ci_supply_chain_sha_pinning_no_curl_pipe_sh_version_parity_and_permissions(tmp_path):
    """Issue 11 (Attacker items 1-5):
    1. Every `uses:` in `.github/workflows/*.yml` is pinned to a 40-character lowercase hex SHA
       with a `# vX.Y.Z` version comment on the YAML line.
    2. Zero `curl ... | sh` or `wget ... | sh` anywhere in `.github/workflows/*.yml`; gitleaks in
       `ci.yml` verifies `GITLEAKS_SHA256` with `sha256sum -c -`.
    3. Tool version parity across all workflows:
       - `actions/checkout`: identical 40-char SHA everywhere
       - `actions/setup-python`: identical 40-char SHA + `python-version: "3.11"` everywhere
       - `hashicorp/setup-terraform`: identical 40-char SHA + `terraform_version: "1.16.2"` +
         `terraform_wrapper: false` everywhere
       - `liquibase/setup-liquibase`: identical 40-char SHA + `version: "4.33.0"` +
         `edition: "community"` everywhere (`ci.yml`, `deploy.yml`, `pr-validate.yml`)
       - `databricks/setup-cli`: identical 40-char SHA + `version: "1.19.0"` everywhere
         (`ci.yml`, `deploy.yml`, `pr-validate.yml`, `pr-cleanup.yml`)
    4. Every workflow has top-level `permissions: {contents: read}` and no job-level write
       permissions.
    5. Zero `${{ ... }}` template expressions interpolated inside `run:` scripts."""
    workflow_files = sorted(WORKFLOWS_DIR.glob("*.yml"))
    _check_workflows_supply_chain(workflow_files)

    # Hostile fixture check: prove _check_workflows_supply_chain rejects each class of violation
    # Hostile element 1: mutable tag (`@v4`) on a realistic workflow copy
    bad_dir = tmp_path / "hostile_mutable_tag"
    shutil.copytree(WORKFLOWS_DIR, bad_dir)
    deploy_copy = bad_dir / "deploy.yml"
    deploy_copy.write_text(
        deploy_copy.read_text().replace(
            "actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683 # v4.2.2",
            "actions/checkout@v4",
        )
    )
    with pytest.raises(AssertionError, match="40-char SHA"):
        _check_workflows_supply_chain(sorted(bad_dir.glob("*.yml")))


def _mutate_swap_sha_and_tar(text: str) -> str:
    sha_line = '          echo "${GITLEAKS_SHA256}  $RUNNER_TEMP/$archive" | sha256sum -c -'
    tar_line = '          tar -xzf "$RUNNER_TEMP/$archive" -C "$RUNNER_TEMP" gitleaks'
    assert sha_line in text and tar_line in text
    return text.replace(f"{sha_line}\n{tar_line}", f"{tar_line}\n{sha_line}", 1)


def _mutate_sha_after_scan(text: str) -> str:
    sha_line = '          echo "${GITLEAKS_SHA256}  $RUNNER_TEMP/$archive" | sha256sum -c -'
    scan_line = '          "$RUNNER_TEMP/gitleaks" git --redact --no-banner --exit-code 1 .'
    assert sha_line in text and scan_line in text
    removed = text.replace(f"{sha_line}\n", "", 1)
    return removed.replace(scan_line, f"{scan_line}\n{sha_line}", 1)


_GITLEAKS_HOSTILE_MUTATIONS: list[tuple[str, Callable[[str], str], str]] = [
    (
        "1a_remove_exit_code_1",
        lambda t: t.replace(
            '"$RUNNER_TEMP/gitleaks" git --redact --no-banner --exit-code 1 .',
            '"$RUNNER_TEMP/gitleaks" git --redact --no-banner .',
            1,
        ),
        r"--exit-code 1",
    ),
    (
        "1b_exit_code_0",
        lambda t: t.replace(
            '"$RUNNER_TEMP/gitleaks" git --redact --no-banner --exit-code 1 .',
            '"$RUNNER_TEMP/gitleaks" git --redact --no-banner --exit-code 0 .',
            1,
        ),
        r"--exit-code 1",
    ),
    (
        "2a_subcommand_version",
        lambda t: t.replace(
            '"$RUNNER_TEMP/gitleaks" git --redact --no-banner --exit-code 1 .',
            '"$RUNNER_TEMP/gitleaks" version --redact --no-banner --exit-code 1 .',
            1,
        ),
        r"subcommand.*git",
    ),
    (
        "2b_subcommand_detect_no_source",
        lambda t: t.replace(
            '"$RUNNER_TEMP/gitleaks" git --redact --no-banner --exit-code 1 .',
            '"$RUNNER_TEMP/gitleaks" detect --redact --no-banner --exit-code 1',
            1,
        ),
        r"subcommand.*git",
    ),
    (
        "3a_target_subdir",
        lambda t: t.replace(
            '"$RUNNER_TEMP/gitleaks" git --redact --no-banner --exit-code 1 .',
            '"$RUNNER_TEMP/gitleaks" git --redact --no-banner --exit-code 1 ./dabs',
            1,
        ),
        r"repo root `\.\`",
    ),
    (
        "3b_target_omitted",
        lambda t: t.replace(
            '"$RUNNER_TEMP/gitleaks" git --redact --no-banner --exit-code 1 .',
            '"$RUNNER_TEMP/gitleaks" git --redact --no-banner --exit-code 1',
            1,
        ),
        r"repo root `\.\`",
    ),
    (
        "4a_sha256_after_tar",
        _mutate_swap_sha_and_tar,
        r"sha256sum.*before.*tar",
    ),
    (
        "4b_sha256_after_scan",
        _mutate_sha_after_scan,
        r"sha256sum.*before.*tar",
    ),
    (
        "5a_or_true_on_scan",
        lambda t: t.replace(
            '"$RUNNER_TEMP/gitleaks" git --redact --no-banner --exit-code 1 .',
            '"$RUNNER_TEMP/gitleaks" git --redact --no-banner --exit-code 1 . || true',
            1,
        ),
        r"suppress failure|mask.*exit",
    ),
    (
        "5b_set_plus_e",
        lambda t: t.replace(
            '          archive="gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz"',
            '          set +e\n          archive="gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz"',
            1,
        ),
        r"set \+e",
    ),
    *[
        (
            f"5b_{case_id}",
            (lambda flags: lambda t: t.replace(
                '          archive="gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz"',
                f'          set {flags}\n          archive="gitleaks_${{GITLEAKS_VERSION}}_linux_x64.tar.gz"',
                1,
            ))(flags),
            r"set \+e",
        )
        for case_id, flags in [
            ("set_plus_eu", "+eu"),
            ("set_plus_xe", "+xe"),
            ("set_plus_x_plus_e", "+x +e"),
            ("set_minus_x_plus_o_errexit", "-x +o errexit"),
        ]
    ],
    (
        "5c_step_continue_on_error",
        lambda t: t.replace(
            "      - name: gitleaks\n",
            "      - name: gitleaks\n        continue-on-error: true\n",
            1,
        ),
        r"gitleaks.*continue-on-error",
    ),
    (
        "5d_job_continue_on_error",
        lambda t: t.replace(
            "  secret-scan:\n    runs-on: ubuntu-latest\n",
            "  secret-scan:\n    runs-on: ubuntu-latest\n    continue-on-error: true\n",
            1,
        ),
        r"secret-scan.*continue-on-error",
    ),
    (
        "5e_or_on_sha256_line",
        lambda t: t.replace(
            'echo "${GITLEAKS_SHA256}  $RUNNER_TEMP/$archive" | sha256sum -c -',
            'echo "${GITLEAKS_SHA256}  $RUNNER_TEMP/$archive" | sha256sum -c - || exit 0',
            1,
        ),
        r"suppress failure|mask exit code",
    ),
    (
        "5f_step_if_false",
        lambda t: t.replace(
            "      - name: gitleaks\n",
            "      - name: gitleaks\n        if: false\n",
            1,
        ),
        r"gitleaks step must not set `if:`",
    ),
    (
        "5g_job_if_false",
        lambda t: t.replace(
            "  secret-scan:\n    runs-on: ubuntu-latest\n",
            "  secret-scan:\n    if: false\n    runs-on: ubuntu-latest\n",
            1,
        ),
        r"secret-scan job must not set `if:`",
    ),
    (
        "5h_step_shell_override_no_errexit",
        lambda t: t.replace(
            "      - name: gitleaks\n",
            "      - name: gitleaks\n        shell: bash {0}\n",
            1,
        ),
        r"gitleaks step must not override `shell:`",
    ),
    (
        "6a_checkout_missing_fetch_depth",
        lambda t: t.replace(
            "    steps:\n      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683 # v4.2.2\n        with:\n          fetch-depth: 0\n",
            "    steps:\n      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683 # v4.2.2\n",
            1,
        ),
        r"fetch-depth:\s*0",
    ),
    (
        "6b_checkout_shallow_fetch_depth_1",
        lambda t: t.replace(
            "          fetch-depth: 0\n",
            "          fetch-depth: 1\n",
            1,
        ),
        r"fetch-depth:\s*0",
    ),
    (
        "7a_remove_redact",
        lambda t: t.replace(
            '"$RUNNER_TEMP/gitleaks" git --redact --no-banner --exit-code 1 .',
            '"$RUNNER_TEMP/gitleaks" git --no-banner --exit-code 1 .',
            1,
        ),
        r"--redact",
    ),
    (
        "7b_trailing_command_after_scan",
        lambda t: t.replace(
            '"$RUNNER_TEMP/gitleaks" git --redact --no-banner --exit-code 1 .',
            '"$RUNNER_TEMP/gitleaks" git --redact --no-banner --exit-code 1 .\n          echo "done"',
            1,
        ),
        r"final command",
    ),
]


@pytest.mark.parametrize(
    ("case_id", "mutate_fn", "expected_match"),
    _GITLEAKS_HOSTILE_MUTATIONS,
    ids=[m[0] for m in _GITLEAKS_HOSTILE_MUTATIONS],
)
def test_attacker_7_gitleaks_step_hostile_mutations_go_red(
    tmp_path: Path,
    case_id: str,
    mutate_fn: Callable[[str], str],
    expected_match: str,
) -> None:
    """Issue 13 (Attackers 1-6 + extended attacker items): Every hostile mutation to
    `.github/workflows/ci.yml`'s `secret-scan` job or `gitleaks` step must fail both
    `_check_ci_gitleaks_step` and `_check_workflows_supply_chain`."""
    bad_dir = tmp_path / f"hostile_{case_id}"
    shutil.copytree(WORKFLOWS_DIR, bad_dir)
    ci_copy = bad_dir / "ci.yml"
    orig_text = ci_copy.read_text()
    mutated_text = mutate_fn(orig_text)
    assert mutated_text != orig_text, f"Mutation {case_id!r} produced no change in ci.yml"
    ci_copy.write_text(mutated_text)

    with pytest.raises(AssertionError, match=expected_match):
        _check_ci_gitleaks_step(ci_copy)
    with pytest.raises(AssertionError, match=expected_match):
        _check_workflows_supply_chain(sorted(bad_dir.glob("*.yml")))


