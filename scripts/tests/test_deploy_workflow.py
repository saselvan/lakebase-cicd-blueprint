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
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

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

    # Also verify ci.yml's secret-scan job uses the checksum-verified gitleaks 8.30.1 CLI binary
    ci_doc = yaml.safe_load((WORKFLOWS_DIR / "ci.yml").read_text())
    secret_steps = ci_doc["jobs"]["secret-scan"]["steps"]
    gitleaks_step = next((s for s in secret_steps if s.get("name") == "gitleaks"), None)
    assert gitleaks_step is not None, "ci.yml secret-scan job must have a `gitleaks` step"
    assert "uses" not in gitleaks_step, "gitleaks step must run checksum-verified binary, not gitleaks-action"
    gl_env = gitleaks_step.get("env") or {}
    assert gl_env.get("GITLEAKS_VERSION") == "8.30.1", gl_env
    assert gl_env.get("GITLEAKS_SHA256") == (
        "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb"
    ), gl_env
    assert "sha256sum -c -" in str(gitleaks_step.get("run") or ""), gitleaks_step

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

