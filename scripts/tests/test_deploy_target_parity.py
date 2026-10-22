"""Falsifiability tests for target parity across Terraform, wait_for_sync, Liquibase, and verify:
  * scripts/deploy.sh
  * terraform/outputs.tf & terraform/variables.tf
  * Makefile & scripts/branch_test.sh

What this protects:
  1. One target: the synced tables (Terraform), wait_for_sync, Liquibase migration, and verify step
     act on ONE workspace/profile, ONE project, and ONE branch (projects/<project>/branches/<branch>).
  2. Workflows and Make targets run against the requested profile, never silently falling back to
     DEFAULT or a mismatched branch/project.

Attacker list:
  1. Defaults only (no PROJECT, BRANCH, PROFILE, or TF_VAR_* set in env; no tfvars file):
     Terraform's default project, branch, and profile equal what wait_for_sync,
     lakebase_resolve_and_mint, and Liquibase use (projects/my-lakebase-project/branches/production,
     profile DEFAULT).
  2. PROJECT, BRANCH, PROFILE set in env, no tfvars file:
     deploy.sh exports TF_VAR_project_id, TF_VAR_branch, TF_VAR_databricks_profile to Terraform,
     and wait_for_sync and lakebase_resolve_and_mint receive the exact same resolved project,
     branch, and profile.
  3. terraform.tfvars sets `branch` (or `project_id` / `databricks_profile`), and env sets a
     DIFFERENT `BRANCH` (or `PROJECT` / `PROFILE`):
     Because Terraform's tfvars file beats TF_VAR_*, Terraform resolves the tfvars value.
     deploy.sh reads back the resolved outputs after `terraform apply` and FAILS LOUDLY (non-zero,
     before wait_for_sync or Liquibase runs) naming the mismatch.
     Conversely, when env vars are UNSET and terraform.tfvars sets `branch` / `project_id` /
     `databricks_profile`, deploy.sh uses the values Terraform resolved so everything targets one
     place.
  4. An `*.auto.tfvars` file sets `project_id` (and/or `branch` / `databricks_profile`):
     When env vars are unset, deploy.sh uses the values Terraform resolved (with `branch` coherent
     with `project_id` or read back from Terraform). When env vars are explicitly set to a
     conflicting value, deploy.sh fails loudly naming the mismatch.
     Also checks that if Terraform's resolved `branch` (`projects/<p>/branches/<b>`) has a
     `<p>` that disagrees with Terraform's resolved `project_id`, deploy.sh fails closed.
  5. `make branch PROFILE=x` passes profile `x` as arg 3 to `scripts/branch_test.sh`;
     `make branch` with no PROFILE passes `DEFAULT`. And `scripts/branch_test.sh` when invoked
     directly with only 2 args respects `PROFILE` from the environment (falling back to `DEFAULT`).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from test_endpoint_credential import ENDPOINTS_JSON, RW_HOST, STUB_DATABRICKS
from test_verify_step import _STUB_PSQL

REPO_ROOT = Path(__file__).resolve().parents[2]


def _stage_repo(tmp_path: Path) -> Path:
    work = tmp_path / "repo"
    work.mkdir(parents=True, exist_ok=True)
    for rel in ("config", "dabs", "liquibase", "scripts", "terraform"):
        shutil.copytree(REPO_ROOT / rel, work / rel)
    shutil.copy(REPO_ROOT / "Makefile", work / "Makefile")
    return work


# A realistic stub `terraform` that evaluates Terraform's actual variable precedence
# (variables.tf defaults < TF_VAR_* < terraform.tfvars < *.auto.tfvars) when reading
# `output -raw <name>`, and records the TF_VAR_* environment it saw during `apply`.
_PRECEDENCE_TF_STUB = r"""#!/usr/bin/env bash
set -euo pipefail
dir="."
for a in "$@"; do
  case "$a" in
    -chdir=*) dir="${a#-chdir=}" ;;
  esac
done

resolve_var() {
  local var_name="$1"
  local tf_dir="$2"
  python3 - "$var_name" "$tf_dir" <<'PY'
import json, os, pathlib, re, sys

var_name = sys.argv[1]
tf_dir = pathlib.Path(sys.argv[2])

defaults = {
    "logical_database": "databricks_postgres",
    "project_id": "my-lakebase-project",
    "branch": "projects/my-lakebase-project/branches/production",
    "databricks_profile": "DEFAULT",
}
val = defaults.get(var_name, "")

# 1. TF_VAR_<name>
env_key = f"TF_VAR_{var_name}"
if env_key in os.environ:
    val = os.environ[env_key]

def parse_file(p: pathlib.Path, cur: str) -> str:
    text = p.read_text()
    if p.name.endswith(".json"):
        data = json.loads(text)
        if var_name in data and data[var_name] is not None:
            return str(data[var_name])
        return cur
    m = re.search(rf'^\s*{re.escape(var_name)}\s*=\s*"([^"]*)"', text, re.M)
    if m:
        return m.group(1)
    return cur

# 2. terraform.tfvars / terraform.tfvars.json
for name in ("terraform.tfvars", "terraform.tfvars.json"):
    p = tf_dir / name
    if p.is_file():
        val = parse_file(p, val)

# 3. *.auto.tfvars / *.auto.tfvars.json in lexical order
if tf_dir.is_dir():
    for p in sorted(tf_dir.iterdir()):
        if p.is_file() and p.name.endswith((".auto.tfvars", ".auto.tfvars.json")):
            val = parse_file(p, val)

sys.stdout.write(val)
PY
}

case "$*" in
  *apply*)
    if [ -n "${TF_APPLY_ENV_LOG:-}" ]; then
      python3 - "$dir" "$TF_APPLY_ENV_LOG" <<'PY'
import json, os, sys
data = {
    "cwd": os.getcwd(),
    "TF_VAR_project_id": os.environ.get("TF_VAR_project_id"),
    "TF_VAR_branch": os.environ.get("TF_VAR_branch"),
    "TF_VAR_databricks_profile": os.environ.get("TF_VAR_databricks_profile"),
    "TF_VAR_logical_database": os.environ.get("TF_VAR_logical_database"),
}
with open(sys.argv[2], "w") as f:
    json.dump(data, f)
PY
    fi
    exit 0
    ;;
  *"output -raw "*)
    out_name="${@: -1}"
    # Verify the output is actually declared in terraform/outputs.tf!
    if ! grep -Eq "output[[:space:]]+\"${out_name}\"" "$dir/outputs.tf"; then
      echo "Error: Output \"${out_name}\" not found in $dir/outputs.tf" >&2
      exit 1
    fi
    if [ -n "${TF_STUB_FORCE_OUTPUTS_JSON:-}" ]; then
      python3 - "$out_name" "$TF_STUB_FORCE_OUTPUTS_JSON" <<'PY'
import json, sys
name = sys.argv[1]
data = json.loads(sys.argv[2])
if name in data:
    sys.stdout.write(str(data[name]))
    sys.exit(0)
sys.exit(2)
PY
      rc=$?
      [ "$rc" -eq 0 ] && exit 0
    fi
    resolve_var "$out_name" "$dir"
    exit 0
    ;;
  *console*)
    printf '"%s"\n' "$(resolve_var "logical_database" "$dir")"
    exit 0
    ;;
esac
exit 0
"""


def _setup_deploy_harness(tmp_path: Path) -> tuple[Path, dict[str, str], dict[str, Path]]:
    work = _stage_repo(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)

    tf_apply_log = tmp_path / "tf_apply_env.json"
    wait_log = tmp_path / "wait_for_sync.log"
    db_log = tmp_path / "databricks.log"
    lb_log = tmp_path / "liquibase.log"

    # Replace wait_for_sync.sh with a recording stub so we see exact args (table + profile)
    (work / "scripts" / "wait_for_sync.sh").write_text(
        f'#!/usr/bin/env bash\nprintf "%s\\t%s\\n" "${{1-}}" "${{2-<unset>}}" >> "{wait_log}"\nexit 0\n'
    )
    (work / "scripts" / "wait_for_sync.sh").chmod(0o755)

    tf_stub = bin_dir / "terraform"
    tf_stub.write_text(_PRECEDENCE_TF_STUB)
    tf_stub.chmod(0o755)

    db_stub = bin_dir / "databricks"
    db_stub.write_text(STUB_DATABRICKS)
    db_stub.chmod(0o755)

    lb_stub = bin_dir / "liquibase"
    lb_stub.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "$LB_LOG"\nexit 0\n')
    lb_stub.chmod(0o755)

    psql_stub = bin_dir / "psql"
    psql_stub.write_text(_STUB_PSQL)
    psql_stub.chmod(0o755)

    endpoints = tmp_path / "endpoints.json"
    endpoints.write_text(ENDPOINTS_JSON)

    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("TF_VAR_")
        and k not in ("PROJECT", "BRANCH", "PROFILE", "PGDATABASE")
    }
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["TF_APPLY_ENV_LOG"] = str(tf_apply_log)
    env["STUB_LOG"] = str(db_log)
    env["STUB_ENDPOINTS_JSON"] = str(endpoints)
    env["LB_LOG"] = str(lb_log)
    env["PGUSER"] = "stub-user@example.com"

    logs = {
        "tf_apply": tf_apply_log,
        "wait": wait_log,
        "databricks": db_log,
        "liquibase": lb_log,
    }
    return work, env, logs


def _real_terraform_outputs(tf_dir: Path, env: dict[str, str]) -> dict[str, str]:
    """Evaluate var.project_id, var.branch, var.databricks_profile, var.logical_database using the
    real `terraform console` binary in an isolated provider-free scratch directory."""
    if shutil.which("terraform", path=os.environ.get("PATH", "")) is None:
        if os.environ.get("CI"):
            pytest.fail("no terraform binary on PATH in CI; the real-Terraform differential test must run")
        pytest.skip("real terraform binary not on PATH")
    real_tf = shutil.which("terraform", path=os.environ.get("PATH", ""))
    assert real_tf is not None
    with tempfile.TemporaryDirectory(prefix="tf-oracle-parity-") as scratch_str:
        scratch = Path(scratch_str)
        shutil.copy(tf_dir / "variables.tf", scratch / "variables.tf")
        shutil.copy(tf_dir / "outputs.tf", scratch / "outputs.tf")
        for f in tf_dir.iterdir():
            if f.is_file() and (
                f.name in ("terraform.tfvars", "terraform.tfvars.json")
                or f.name.endswith((".auto.tfvars", ".auto.tfvars.json"))
            ):
                shutil.copy(f, scratch / f.name)
        out: dict[str, str] = {}
        for var in ("project_id", "branch", "databricks_profile", "logical_database"):
            res = subprocess.run(
                [real_tf, f"-chdir={scratch}", "console"],
                input=f"var.{var}\n",
                env=env,
                capture_output=True,
                text=True,
                check=True,
            )
            out[var] = json.loads(res.stdout.strip().splitlines()[-1])
        return out


def test_attacker_1_defaults_agree_across_terraform_wait_liquibase_and_verify(tmp_path):
    """Attacker 1 (defaults only): with PROJECT, BRANCH, PROFILE unset and no tfvars file,
    Terraform's defaults (`projects/my-lakebase-project/branches/production`, `my-lakebase-project`,
    `DEFAULT`) must equal what wait_for_sync and lakebase_resolve_and_mint receive.
    Previously deploy.sh defaulted BRANCH=main while Terraform defaulted branch=.../production."""
    work, env, logs = _setup_deploy_harness(tmp_path)

    # Check against real terraform oracle too
    oracle = _real_terraform_outputs(work / "terraform", env)
    assert oracle["project_id"] == "my-lakebase-project"
    assert oracle["branch"] == "projects/my-lakebase-project/branches/production"
    assert oracle["databricks_profile"] == "DEFAULT"

    r = subprocess.run(
        ["bash", str(work / "scripts" / "deploy.sh")],
        cwd=str(work),
        env=env,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, f"deploy.sh failed on defaults:\n{r.stdout}\n{r.stderr}"

    # Verify wait_for_sync got DEFAULT
    wait_calls = logs["wait"].read_text().strip().splitlines()
    assert len(wait_calls) == 2
    for line in wait_calls:
        assert line.endswith("\tDEFAULT"), f"expected DEFAULT profile in wait_for_sync: {line!r}"

    # Verify databricks list-endpoints was called for projects/my-lakebase-project/branches/production
    # and NOT projects/my-lakebase-project/branches/main!
    db_calls = logs["databricks"].read_text()
    assert (
        "postgres list-endpoints projects/my-lakebase-project/branches/production -p DEFAULT -o json"
        in db_calls
    ), f"expected production branch in list-endpoints:\n{db_calls}"
    assert "branches/main" not in db_calls, (
        f"deploy.sh targeted branches/main while Terraform targeted branches/production:\n{db_calls}"
    )


def test_attacker_2_env_vars_passed_to_terraform_and_used_everywhere(tmp_path):
    """Attacker 2: when PROJECT, BRANCH, PROFILE are set in the environment (no tfvars file),
    deploy.sh passes them to Terraform (via TF_VAR_project_id, TF_VAR_branch,
    TF_VAR_databricks_profile) and uses the exact same values in wait_for_sync and
    lakebase_resolve_and_mint. Also supports PROFILE="" (CI mode: empty profile)."""
    work, env, logs = _setup_deploy_harness(tmp_path)
    env["PROJECT"] = "env-proj-99"
    env["BRANCH"] = "staging"
    env["PROFILE"] = "ws-staging"

    r = subprocess.run(
        ["bash", str(work / "scripts" / "deploy.sh")],
        cwd=str(work),
        env=env,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, f"deploy.sh failed with env vars:\n{r.stdout}\n{r.stderr}"

    # 1. Terraform apply received TF_VAR_project_id, TF_VAR_branch, TF_VAR_databricks_profile
    tf_env = json.loads(logs["tf_apply"].read_text())
    assert tf_env["TF_VAR_project_id"] == "env-proj-99", tf_env
    assert tf_env["TF_VAR_branch"] == "projects/env-proj-99/branches/staging", tf_env
    assert tf_env["TF_VAR_databricks_profile"] == "ws-staging", tf_env

    # 2. wait_for_sync received profile ws-staging
    wait_calls = logs["wait"].read_text().strip().splitlines()
    assert all(line.endswith("\tws-staging") for line in wait_calls), wait_calls

    # 3. databricks list-endpoints received projects/env-proj-99/branches/staging and -p ws-staging
    db_calls = logs["databricks"].read_text()
    assert (
        "postgres list-endpoints projects/env-proj-99/branches/staging -p ws-staging -o json"
        in db_calls
    ), db_calls


def test_attacker_3_tfvars_overrides_env_fails_loudly_on_conflict_and_works_when_env_unset(tmp_path):
    """Attacker 3:
    (a) terraform.tfvars sets `branch` (or `project_id` / `databricks_profile`) and the environment
        sets a DIFFERENT `BRANCH` (or `PROJECT` / `PROFILE`). Because Terraform's tfvars beats
        TF_VAR_*, Terraform applies to the tfvars target. deploy.sh must detect the mismatch after
        reading Terraform outputs and FAIL LOUDLY before wait_for_sync / Liquibase run.
    (b) When `BRANCH` / `PROJECT` / `PROFILE` are NOT set in the environment and `terraform.tfvars`
        sets them, deploy.sh adopts the values resolved by Terraform so all steps target the tfvars
        project, branch, and profile."""
    # Part (a1): terraform.tfvars sets branch = .../tfvars-branch, env sets BRANCH=env-branch
    work_a, env_a, logs_a = _setup_deploy_harness(tmp_path / "conflict_branch")
    (work_a / "terraform" / "terraform.tfvars").write_text(
        'project_id = "my-lakebase-project"\n'
        'branch = "projects/my-lakebase-project/branches/tfvars-branch"\n'
    )
    env_a["BRANCH"] = "env-branch"

    r_a = subprocess.run(
        ["bash", str(work_a / "scripts" / "deploy.sh")],
        cwd=str(work_a),
        env=env_a,
        capture_output=True,
        text=True,
    )
    assert r_a.returncode != 0, (
        f"deploy.sh exited 0 despite BRANCH={env_a['BRANCH']!r} conflicting with terraform.tfvars:\n"
        f"{r_a.stdout}\n{r_a.stderr}"
    )
    combined_a = (r_a.stdout + r_a.stderr).lower()
    assert "mismatch" in combined_a or "disagree" in combined_a or "conflict" in combined_a, (
        f"expected clear mismatch error message, got:\n{r_a.stdout}\n{r_a.stderr}"
    )
    assert not logs_a["liquibase"].exists() or logs_a["liquibase"].read_text() == "", (
        "Liquibase must not run when Terraform target mismatches caller's env"
    )

    # Part (a2): terraform.tfvars sets databricks_profile = "tfvars-prof", env sets PROFILE="env-prof"
    work_p, env_p, logs_p = _setup_deploy_harness(tmp_path / "conflict_profile")
    (work_p / "terraform" / "terraform.tfvars").write_text(
        'databricks_profile = "tfvars-prof"\n'
    )
    env_p["PROFILE"] = "env-prof"
    r_p = subprocess.run(
        ["bash", str(work_p / "scripts" / "deploy.sh")],
        cwd=str(work_p),
        env=env_p,
        capture_output=True,
        text=True,
    )
    assert r_p.returncode != 0, "deploy.sh exited 0 despite PROFILE conflicting with terraform.tfvars"
    assert not logs_p["liquibase"].exists() or logs_p["liquibase"].read_text() == ""

    # Part (b): env vars unset, terraform.tfvars sets project_id, branch, databricks_profile
    work_b, env_b, logs_b = _setup_deploy_harness(tmp_path / "tfvars_only")
    (work_b / "terraform" / "terraform.tfvars").write_text(
        'project_id = "tfvars-proj"\n'
        'branch = "projects/tfvars-proj/branches/tfvars-br"\n'
        'databricks_profile = "tfvars-prof"\n'
    )
    r_b = subprocess.run(
        ["bash", str(work_b / "scripts" / "deploy.sh")],
        cwd=str(work_b),
        env=env_b,
        capture_output=True,
        text=True,
    )
    assert r_b.returncode == 0, f"deploy.sh failed when configured via terraform.tfvars:\n{r_b.stdout}\n{r_b.stderr}"
    wait_calls_b = logs_b["wait"].read_text().strip().splitlines()
    assert all(line.endswith("\ttfvars-prof") for line in wait_calls_b), wait_calls_b
    db_calls_b = logs_b["databricks"].read_text()
    assert (
        "postgres list-endpoints projects/tfvars-proj/branches/tfvars-br -p tfvars-prof -o json"
        in db_calls_b
    ), db_calls_b


def test_attacker_4_auto_tfvars_project_id_and_internal_branch_consistency(tmp_path):
    """Attacker 4:
    (a) An `*.auto.tfvars` file sets `project_id` and `branch` (and `databricks_profile`): when env
        vars are unset, deploy.sh uses the values Terraform resolved.
    (b) When `*.auto.tfvars` sets `project_id = "auto-proj"` and `PROJECT="other-proj"` is set in
        env, deploy.sh fails loudly naming the mismatch.
    (c) Hostile fixture: `*.auto.tfvars` sets `project_id = "auto-proj"` ONLY, leaving `var.branch`
        at its default `projects/my-lakebase-project/branches/production` (so Terraform's own
        `project_id` and `branch` outputs disagree on the project). deploy.sh must fail closed
        instead of silently migrating one project while synced tables are on another."""
    # (a) Coherent *.auto.tfvars with env unset -> succeeds and targets auto-proj / auto-br / auto-prof
    work_a, env_a, logs_a = _setup_deploy_harness(tmp_path / "auto_ok")
    (work_a / "terraform" / "prod.auto.tfvars").write_text(
        'project_id = "auto-proj"\n'
        'branch = "projects/auto-proj/branches/auto-br"\n'
        'databricks_profile = "auto-prof"\n'
    )
    r_a = subprocess.run(
        ["bash", str(work_a / "scripts" / "deploy.sh")],
        cwd=str(work_a),
        env=env_a,
        capture_output=True,
        text=True,
    )
    assert r_a.returncode == 0, f"deploy.sh failed on coherent prod.auto.tfvars:\n{r_a.stdout}\n{r_a.stderr}"
    db_calls_a = logs_a["databricks"].read_text()
    assert (
        "postgres list-endpoints projects/auto-proj/branches/auto-br -p auto-prof -o json"
        in db_calls_a
    ), db_calls_a

    # (b) prod.auto.tfvars sets project_id="auto-proj" and branch="projects/auto-proj/branches/production",
    #     but env sets PROJECT="env-proj" -> fails loudly
    work_b, env_b, logs_b = _setup_deploy_harness(tmp_path / "auto_conflict")
    (work_b / "terraform" / "prod.auto.tfvars").write_text(
        'project_id = "auto-proj"\n'
        'branch = "projects/auto-proj/branches/production"\n'
    )
    env_b["PROJECT"] = "env-proj"
    r_b = subprocess.run(
        ["bash", str(work_b / "scripts" / "deploy.sh")],
        cwd=str(work_b),
        env=env_b,
        capture_output=True,
        text=True,
    )
    assert r_b.returncode != 0, "deploy.sh exited 0 when PROJECT in env conflicted with prod.auto.tfvars"
    assert not logs_b["liquibase"].exists() or logs_b["liquibase"].read_text() == ""

    # (c) Hostile: prod.auto.tfvars sets ONLY `project_id = "auto-proj"`, leaving `branch` at
    #     `projects/my-lakebase-project/branches/production`. Terraform's `project_id` and `branch`
    #     disagree! deploy.sh must fail closed.
    work_c, env_c, logs_c = _setup_deploy_harness(tmp_path / "auto_inconsistent")
    (work_c / "terraform" / "prod.auto.tfvars").write_text('project_id = "auto-proj"\n')
    r_c = subprocess.run(
        ["bash", str(work_c / "scripts" / "deploy.sh")],
        cwd=str(work_c),
        env=env_c,
        capture_output=True,
        text=True,
    )
    assert r_c.returncode != 0, (
        "deploy.sh exited 0 when prod.auto.tfvars set project_id='auto-proj' but left var.branch on "
        "projects/my-lakebase-project/branches/production"
    )
    assert not logs_c["liquibase"].exists() or logs_c["liquibase"].read_text() == ""


def test_attacker_5_make_branch_and_branch_test_sh_thread_profile(tmp_path):
    """Attacker 5:
    (a) `make -n branch PROJECT=my-proj PROFILE=custom-prof` passes `custom-prof` as arg 3 to
        `./scripts/branch_test.sh`.
    (b) `make -n branch PROJECT=my-proj` (no PROFILE) passes `DEFAULT` as arg 3 to
        `./scripts/branch_test.sh`.
    (c) Running `branch_test.sh <proj> <branch>` directly (2 args) with `PROFILE=custom-prof` in
        the environment uses `custom-prof` (and defaults to `DEFAULT` when neither arg 3 nor env
        `PROFILE` is set)."""
    work, env, logs = _setup_deploy_harness(tmp_path)

    # (a) make -n branch with PROFILE=custom-prof
    r_make_prof = subprocess.run(
        ["make", "-n", "branch", "PROJECT=my-proj", "PROFILE=custom-prof"],
        cwd=str(work),
        env=env,
        capture_output=True,
        text=True,
    )
    assert r_make_prof.returncode == 0, r_make_prof.stderr
    assert "./scripts/branch_test.sh my-proj pr-test custom-prof" in r_make_prof.stdout.strip(), (
        f"make branch did not pass PROFILE=custom-prof as arg 3:\n{r_make_prof.stdout}"
    )

    # (b) make -n branch with no PROFILE
    r_make_def = subprocess.run(
        ["make", "-n", "branch", "PROJECT=my-proj"],
        cwd=str(work),
        env=env,
        capture_output=True,
        text=True,
    )
    assert r_make_def.returncode == 0, r_make_def.stderr
    assert "./scripts/branch_test.sh my-proj pr-test DEFAULT" in r_make_def.stdout.strip(), (
        f"make branch without PROFILE did not pass DEFAULT as arg 3:\n{r_make_def.stdout}"
    )

    # (c) Direct invocation of branch_test.sh with 2 args + PROFILE=env-prof in env
    env_direct = dict(env, PROFILE="env-prof", TF_VAR_logical_database="databricks_postgres")
    r_direct = subprocess.run(
        ["bash", str(work / "scripts" / "branch_test.sh"), "my-lakebase-project", "pr-123"],
        cwd=str(work),
        env=env_direct,
        capture_output=True,
        text=True,
    )
    assert r_direct.returncode == 0, f"branch_test.sh failed:\n{r_direct.stdout}\n{r_direct.stderr}"
    db_calls = logs["databricks"].read_text()
    assert "postgres create-branch projects/my-lakebase-project pr-123" in db_calls
    assert "-p env-prof" in db_calls, (
        f"branch_test.sh invoked with 2 args ignored PROFILE=env-prof from env:\n{db_calls}"
    )
    assert "-p DEFAULT" not in db_calls, db_calls
