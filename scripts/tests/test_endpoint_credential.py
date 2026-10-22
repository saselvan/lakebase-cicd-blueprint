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

import pytest

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


# Redacted live `databricks postgres list-endpoints` response from `projects/lakebase-cicd-ref`.
_LIVE_REDACTED_ENDPOINTS = [
    {
        "create_time": "2026-09-16T16:16:56Z",
        "endpoint_id": "primary",
        "name": "projects/lakebase-cicd-ref/branches/production/endpoints/primary",
        "parent": "projects/lakebase-cicd-ref/branches/production",
        "status": {
            "autoscaling_limit_max_cu": 8,
            "autoscaling_limit_min_cu": 4,
            "current_state": "ACTIVE",
            "disabled": False,
            "endpoint_id": "primary",
            "endpoint_type": "ENDPOINT_TYPE_READ_WRITE",
            "group": {"enable_readable_secondaries": False, "max": 1, "min": 1},
            "hosts": {
                "host": "ep-live-fixture-rw.database.cloud.databricks.com",
                "read_write_pooled_host": "ep-live-fixture-pooler.database.us-east-1.cloud.databricks.com",
            },
            "last_active_time": "2026-10-21T11:33:21Z",
            "settings": {},
        },
        "uid": "00000000-0000-0000-0000-000000000001",
        "update_time": "2026-10-21T11:33:33Z",
    }
]

_ENDPOINT_ATTACKER_CASES = [
    (
        "1_only_read_only_with_hosts",
        [
            {
                "name": "projects/p/branches/b/endpoints/ro-1",
                "status": {"endpoint_type": "ENDPOINT_TYPE_READ_ONLY", "hosts": {"host": "ro1.example"}},
            },
            {
                "name": "projects/p/branches/b/endpoints/ro-2",
                "status": {"endpoint_type": "READ_ONLY", "hosts": {"host": "ro2.example"}},
            },
        ],
        None,
        "no compute endpoint",
    ),
    (
        "2_no_endpoint_has_host",
        [
            {
                "name": "projects/p/branches/b/endpoints/rw",
                "status": {"endpoint_type": "ENDPOINT_TYPE_READ_WRITE", "hosts": {"host": None}},
            },
            {
                "name": "projects/p/branches/b/endpoints/ro",
                "status": {"endpoint_type": "ENDPOINT_TYPE_READ_ONLY", "hosts": {"host": ""}},
            },
        ],
        None,
        "no compute endpoint",
    ),
    (
        "3_read_write_no_host_plus_read_only_with_host",
        [
            {
                "name": "projects/p/branches/b/endpoints/rw",
                "status": {"endpoint_type": "ENDPOINT_TYPE_READ_WRITE", "hosts": {"host": None}},
            },
            {
                "name": "projects/p/branches/b/endpoints/ro",
                "status": {"endpoint_type": "ENDPOINT_TYPE_READ_ONLY", "hosts": {"host": "ro-ready.example"}},
            },
        ],
        None,
        "no compute endpoint",
    ),
    (
        "4a_read_write_with_host_and_empty_name",
        [
            {
                "name": "",
                "status": {"endpoint_type": "ENDPOINT_TYPE_READ_WRITE", "hosts": {"host": "rw.example"}},
            },
        ],
        None,
        "no resource name",
    ),
    (
        "4b_read_write_with_host_and_missing_name",
        [
            {
                "name": None,
                "status": {"endpoint_type": "ENDPOINT_TYPE_READ_WRITE", "hosts": {"host": "rw.example"}},
            },
        ],
        None,
        "no resource name",
    ),
    (
        "5_read_only_first_read_write_second",
        [
            {
                "name": "projects/p/branches/b/endpoints/ro",
                "status": {"endpoint_type": "ENDPOINT_TYPE_READ_ONLY", "hosts": {"host": "ro.example"}},
            },
            {
                "name": "projects/p/branches/b/endpoints/rw",
                "status": {"endpoint_type": "ENDPOINT_TYPE_READ_WRITE", "hosts": {"host": "rw.example"}},
            },
        ],
        ("rw.example", "projects/p/branches/b/endpoints/rw"),
        None,
    ),
    (
        "6_empty_endpoint_list",
        [],
        None,
        "no compute endpoint",
    ),
    (
        "7_live_redacted_lakebase_cicd_ref",
        _LIVE_REDACTED_ENDPOINTS,
        (
            "ep-live-fixture-rw.database.cloud.databricks.com",
            "projects/lakebase-cicd-ref/branches/production/endpoints/primary",
        ),
        None,
    ),
    (
        "8_whitespace_only_host_and_name",
        [
            {
                "name": "   ",
                "status": {"endpoint_type": "ENDPOINT_TYPE_READ_WRITE", "hosts": {"host": "rw.example"}},
            },
        ],
        None,
        "no resource name",
    ),
    (
        "9_unspecified_endpoint_type_with_host",
        [
            {
                "name": "projects/p/branches/b/endpoints/unspec",
                "status": {"endpoint_type": "ENDPOINT_TYPE_UNSPECIFIED", "hosts": {"host": "unspec.example"}},
            },
        ],
        None,
        "no compute endpoint",
    ),
]


def _run_shell_resolve(tmp_path: Path, case_id: str, raw_eps: list[dict]) -> tuple[int, str, str, str]:
    """Run `lakebase_resolve_and_mint` in a subshell with STUB_ENDPOINTS_JSON set to `raw_eps`."""
    case_dir = tmp_path / case_id
    case_dir.mkdir(parents=True, exist_ok=True)
    env, call_log = _stub_env(case_dir)
    (case_dir / "endpoints.json").write_text(json.dumps(raw_eps))
    harness = (
        'set -euo pipefail; '
        f'. "{RESOLVE_HELPER}"; '
        'lakebase_resolve_and_mint "projects/p/branches/b" DEFAULT; '
        'printf "HOST=%s\\nENDPOINT=%s\\nTOKEN=%s\\n" "$LB_HOST" "$LB_ENDPOINT" "$LB_TOKEN"'
    )
    res = subprocess.run(["bash", "-c", harness], env=env, capture_output=True, text=True)
    calls = call_log.read_text() if call_log.exists() else ""
    return res.returncode, res.stdout, res.stderr, calls


@pytest.mark.parametrize(
    "case_id,raw_eps,expected,err_substr",
    _ENDPOINT_ATTACKER_CASES,
    ids=[c[0] for c in _ENDPOINT_ATTACKER_CASES],
)
def test_shell_and_dabs_endpoint_parity_matrix(tmp_path, case_id, raw_eps, expected, err_substr):
    """Parity + Attacker matrix (items 1-9 + live fixture): feed the exact same endpoint lists to
    BOTH `scripts/resolve_endpoint.sh` (`lakebase_resolve_and_mint`) and `dabs/migration_job.py`
    (`_select_endpoint` / `_lakebase_conninfo`), asserting identical outcomes and zero credential
    mints on every failure case.

    MUTATION GATES:
      - Mutation A (allow READ_ONLY fallback): cases 1, 3, 9 go RED.
      - Mutation B (drop the no-name check): cases 4a, 4b, 8 go RED.
    """
    import enum
    import sys
    import types

    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from dabs import migration_job as mj

    class _EndpointType(enum.Enum):
        ENDPOINT_TYPE_READ_ONLY = "ENDPOINT_TYPE_READ_ONLY"
        ENDPOINT_TYPE_READ_WRITE = "ENDPOINT_TYPE_READ_WRITE"
        ENDPOINT_TYPE_UNSPECIFIED = "ENDPOINT_TYPE_UNSPECIFIED"

    def to_sdk(d: dict):
        status_d = d.get("status") or {}
        hosts_d = status_d.get("hosts") or {}
        raw_etype = status_d.get("endpoint_type")
        etype = _EndpointType(raw_etype) if raw_etype in _EndpointType._value2member_map_ else raw_etype
        return types.SimpleNamespace(
            name=d.get("name"),
            status=types.SimpleNamespace(
                endpoint_type=etype,
                hosts=types.SimpleNamespace(host=hosts_d.get("host")),
            ),
        )

    rc, out, err, calls = _run_shell_resolve(tmp_path, case_id, raw_eps)
    sdk_eps = [to_sdk(ep) for ep in raw_eps]
    dabs_cred_calls: list[str] = []

    if expected is not None:
        exp_host, exp_name = expected
        assert rc == 0, f"{case_id}: shell failed (rc={rc}):\n{out}\n{err}"
        assert f"HOST={exp_host}" in out, f"{case_id}: {out}"
        assert f"ENDPOINT={exp_name}" in out, f"{case_id}: {out}"
        assert f"TOKEN={STUB_TOKEN}" in out, f"{case_id}: {out}"
        assert f"postgres generate-database-credential {exp_name}" in calls, f"{case_id}: {calls}"

        ep = mj._select_endpoint(sdk_eps)
        assert mj._endpoint_host(ep) == exp_host, case_id
        assert mj._endpoint_name(ep) == exp_name, case_id
        conninfo = mj._lakebase_conninfo(
            branch="projects/p/branches/b",
            resolve_endpoint=lambda b, _eps=sdk_eps: mj._select_endpoint(_eps),
            credential_source=lambda e: (dabs_cred_calls.append(e), ("u", STUB_TOKEN))[1],
        )
        fields = dict(kv.split("=", 1) for kv in conninfo.split())
        assert fields["host"] == exp_host, case_id
        assert dabs_cred_calls == [exp_name], case_id
    else:
        assert rc != 0, f"{case_id}: shell unexpectedly succeeded:\n{out}"
        assert err_substr in err, f"{case_id}: expected {err_substr!r} in stderr, got {err!r}"
        assert "generate-database-credential" not in calls, (
            f"{case_id}: shell minted a credential on error:\n{calls}"
        )
        with pytest.raises(RuntimeError, match=err_substr):
            mj._select_endpoint(sdk_eps)
        with pytest.raises(RuntimeError, match=err_substr):
            mj._lakebase_conninfo(
                branch="projects/p/branches/b",
                resolve_endpoint=lambda b, _eps=sdk_eps: mj._select_endpoint(_eps),
                credential_source=lambda e: (dabs_cred_calls.append(e), ("u", STUB_TOKEN))[1],
            )
        assert dabs_cred_calls == [], (
            f"{case_id}: DABs minted a credential on error: {dabs_cred_calls}"
        )


def test_branch_test_script_resolves_via_endpoint(tmp_path):
    """branch_test.sh (no terraform/liquibase — safe to run fully) mints via the endpoint form."""
    env, call_log = _stub_env(tmp_path)
    # This test is about the endpoint, not the database name: pin the name so the result does
    # not depend on a terraform binary on PATH (CI's dabs-validate job had none).
    env["TF_VAR_logical_database"] = "databricks_postgres"
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


def _terraform_console_logical_db(tf_dir: Path, env: dict[str, str]) -> str:
    """Ask the real terraform binary in an isolated scratch dir (variables.tf + all *.tfvars*) for
    var.logical_database."""
    import shutil
    import tempfile

    with tempfile.TemporaryDirectory(prefix="tf-oracle-") as scratch_str:
        scratch = Path(scratch_str)
        shutil.copy(tf_dir / "variables.tf", scratch / "variables.tf")
        for f in tf_dir.iterdir():
            if f.name in ("terraform.tfvars", "terraform.tfvars.json") or f.name.endswith(
                (".auto.tfvars", ".auto.tfvars.json")
            ):
                shutil.copy(f, scratch / f.name)
        res = subprocess.run(
            ["terraform", f"-chdir={scratch}", "console"],
            input="var.logical_database\n",
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(res.stdout.strip().splitlines()[-1])


def test_resolve_database_matches_real_terraform_oracle(tmp_path):
    """Differential test (Attackers 1, 2, 4): lakebase_resolve_database must match the real
    `terraform console` evaluation across:
      - variables.tf default (and custom default with HCL comments)
      - Attacker 1: prod.auto.tfvars, terraform.tfvars.json, x.auto.tfvars.json
      - Attacker 2: two *.auto.tfvars files that disagree (lexical order decides in Terraform)
      - Attacker 4: TF_VAR_logical_database AND a tfvars value (tfvars wins in Terraform)
      - PGDATABASE and LOGICAL_DATABASE never override Terraform's resolution."""
    import shutil
    import pytest

    if shutil.which("terraform") is None:
        if os.environ.get("CI"):
            pytest.fail("no terraform binary on PATH in CI; the real-Terraform differential test must run")
        pytest.skip("no terraform binary on PATH; cannot run real-Terraform differential test")

    base_env = dict(os.environ)
    base_env.pop("TF_VAR_logical_database", None)
    base_env["PGDATABASE"] = "pgdatabase_must_not_win"
    base_env["LOGICAL_DATABASE"] = "invented_var_must_not_win"

    cases = [
        # (name, files_dict, tf_var_env, expected)
        ("default", {}, None, "databricks_postgres"),
        (
            "custom_variables_tf_with_comments",
            {
                "variables.tf": (
                    'variable "logical_database" {\n'
                    '  // default = "decoy_comment_db"\n'
                    '  # default = "hash_decoy_db"\n'
                    '  /* default = "block_decoy_db" */\n'
                    '  type    = string\n'
                    '  default = "vars_default_db"\n'
                    '}\n'
                )
            },
            None,
            "vars_default_db",
        ),
        ("tf_var_only", {}, "env_only_db", "env_only_db"),
        # Attacker 1a: prod.auto.tfvars
        ("prod_auto_tfvars", {"prod.auto.tfvars": 'logical_database = "prod_auto_db"\n'}, None, "prod_auto_db"),
        # Attacker 1b: terraform.tfvars.json
        (
            "terraform_tfvars_json",
            {"terraform.tfvars.json": '{"logical_database": "tfvars_json_db"}\n'},
            None,
            "tfvars_json_db",
        ),
        # Attacker 1c: x.auto.tfvars.json overrides terraform.tfvars
        (
            "x_auto_tfvars_json",
            {
                "terraform.tfvars": 'logical_database = "base_tfvars_db"\n',
                "x.auto.tfvars.json": '{"logical_database": "x_auto_json_db"}\n',
            },
            None,
            "x_auto_json_db",
        ),
        # Attacker 2: two auto.tfvars files that disagree (lexical order: b_second wins over a_first)
        (
            "two_disagreeing_auto_tfvars",
            {
                "a_first.auto.tfvars": 'logical_database = "first_auto_db"\n',
                "b_second.auto.tfvars": 'logical_database = "second_auto_db"\n',
            },
            None,
            "second_auto_db",
        ),
        # Attacker 4: TF_VAR_logical_database set AND a tfvars value (tfvars wins in Terraform!)
        (
            "tf_var_and_tfvars_disagree",
            {"terraform.tfvars": 'logical_database = "tfvars_wins_over_env_db"\n'},
            "env_loses_to_tfvars_db",
            "tfvars_wins_over_env_db",
        ),
    ]

    for name, files, tf_var, expected in cases:
        repo = _make_tf_repo(tmp_path / name)
        for fname, content in files.items():
            (repo / "terraform" / fname).write_text(content)
        env = dict(base_env)
        if tf_var is not None:
            env["TF_VAR_logical_database"] = tf_var

        oracle = _terraform_console_logical_db(repo / "terraform", env)
        assert oracle == expected, f"case {name}: terraform oracle returned {oracle!r}, expected {expected!r}"

        r = subprocess.run(
            ["bash", "-c", f'. "{RESOLVE_HELPER}" && lakebase_resolve_database "{repo}"'],
            env=env,
            capture_output=True,
            text=True,
        )
        assert r.returncode == 0, f"case {name}: resolver failed:\n{r.stderr}"
        resolved = r.stdout.strip()
        assert resolved == oracle, (
            f"case {name}: lakebase_resolve_database returned {resolved!r} != terraform oracle {oracle!r}"
        )


def test_resolve_database_rejects_hostile_database_names_in_bash(tmp_path):
    """Hostile database names with URL or conninfo injection characters (e.g. db?sslmode=disable,
    db sslmode=disable, trailing newline, non-ASCII, 64 chars, empty) must fail closed in bash
    before building any URL or conninfo."""
    repo = _make_tf_repo(tmp_path / "repo")
    for bad in ("db?sslmode=disable", "db sslmode=disable", "1bad", "db;drop", r"db\x", "a" * 64, "db_é", ""):
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

    # Direct validator check for trailing newline ("db_ok\n") which bash command substitution strips
    r_nl = subprocess.run(
        ["bash", "-c", f'. "{RESOLVE_HELPER}" && lakebase_validate_database $\'db_ok\\n\''],
        capture_output=True, text=True,
    )
    assert r_nl.returncode != 0, "lakebase_validate_database accepted trailing newline"


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

    for bad in ("db?sslmode=disable", "db sslmode=disable", "db_ok\n", "db_é"):
        monkeypatch.setenv("PGDATABASE", bad)
        with pytest.raises(ValueError):
            mod.main()


def test_resolve_database_no_terraform_binary_and_failing_terraform_console(tmp_path):
    """When terraform is not on PATH:
      - fails closed if any terraform.tfvars, terraform.tfvars.json, or *.auto.tfvars(.json) exists;
      - returns TF_VAR_logical_database (or 'databricks_postgres' if unset) when no var files exist.
    When terraform IS on PATH and `terraform console` fails (or outputs non-JSON), exits non-zero
    immediately instead of silently falling back."""
    import shutil

    # Build a PATH that has bash + python3 (and basic utils) but NO `terraform` binary.
    no_tf_bin = tmp_path / "no_tf_bin"
    no_tf_bin.mkdir()
    for tool in ("bash", "python3", "dirname", "pwd"):
        real = shutil.which(tool)
        if real:
            (no_tf_bin / tool).symlink_to(real)

    # 1) No terraform binary + no var files -> fails closed if TF_VAR_logical_database is unset,
    #    succeeds with TF_VAR_logical_database when explicitly set.
    clean_root = tmp_path / "clean_root"
    clean_tf = clean_root / "terraform"
    clean_tf.mkdir(parents=True)
    (clean_tf / "variables.tf").write_text((REPO_ROOT / "terraform" / "variables.tf").read_text())

    env_no_tf = dict(os.environ, PATH=str(no_tf_bin))
    env_no_tf.pop("TF_VAR_logical_database", None)
    r_def = subprocess.run(
        [str(no_tf_bin / "bash"), "-c", f'set -euo pipefail; . "{RESOLVE_HELPER}"; lakebase_resolve_database "{clean_root}"'],
        env=env_no_tf,
        capture_output=True,
        text=True,
    )
    assert r_def.returncode != 0
    assert "terraform binary not found; install terraform or set TF_VAR_logical_database" in r_def.stderr

    env_with_var = dict(env_no_tf, TF_VAR_logical_database="env_db")
    r_env = subprocess.run(
        [str(no_tf_bin / "bash"), "-c", f'set -euo pipefail; . "{RESOLVE_HELPER}"; lakebase_resolve_database "{clean_root}"'],
        env=env_with_var,
        capture_output=True,
        text=True,
    )
    assert r_env.returncode == 0, r_env.stderr
    assert r_env.stdout.strip() == "env_db"

    # 2) No terraform binary + any var file -> fails closed with "unsupported var file: <name>"
    for var_file, content in [
        ("prod.auto.tfvars", 'logical_database = "prod_db"\n'),
        ("terraform.tfvars.json", '{"logical_database": "json_db"}\n'),
        ("x.auto.tfvars.json", '{"logical_database": "x_db"}\n'),
        ("terraform.tfvars", 'logical_database = "tf_db"\n'),
    ]:
        root = tmp_path / f"case_{var_file}"
        tf_dir = root / "terraform"
        tf_dir.mkdir(parents=True)
        (tf_dir / "variables.tf").write_text((REPO_ROOT / "terraform" / "variables.tf").read_text())
        (tf_dir / var_file).write_text(content)
        res = subprocess.run(
            [str(no_tf_bin / "bash"), "-c", f'set -euo pipefail; . "{RESOLVE_HELPER}"; lakebase_resolve_database "{root}"'],
            env=env_no_tf,
            capture_output=True,
            text=True,
        )
        assert res.returncode != 0, f"expected fail-closed on {var_file}"
        assert f"unsupported var file: {var_file}; set TF_VAR_logical_database" in res.stderr

    # 3) Terraform IS on PATH, no var files, but `terraform console` fails or emits junk -> exits non-zero
    for case_name, stub_body, expected_stderr in [
        ("nonzero_exit", "#!/usr/bin/env bash\necho 'console boom' >&2\nexit 1\n", "console boom"),
        ("empty_output", "#!/usr/bin/env bash\nexit 0\n", "empty output"),
        ("invalid_json", "#!/usr/bin/env bash\necho 'not-json'\nexit 0\n", "invalid JSON"),
        ("non_string", "#!/usr/bin/env bash\necho '42'\nexit 0\n", "non-string"),
    ]:
        failing_tf_bin = tmp_path / f"failing_tf_{case_name}"
        failing_tf_bin.mkdir()
        tf_stub = failing_tf_bin / "terraform"
        tf_stub.write_text(stub_body)
        tf_stub.chmod(0o755)
        env_fail_tf = dict(os.environ, PATH=f"{failing_tf_bin}{os.pathsep}{os.environ.get('PATH', '')}")
        env_fail_tf.pop("TF_VAR_logical_database", None)
        r_fail = subprocess.run(
            ["bash", "-c", f'set -euo pipefail; . "{RESOLVE_HELPER}"; lakebase_resolve_database "{clean_root}"'],
            env=env_fail_tf,
            capture_output=True,
            text=True,
        )
        assert r_fail.returncode != 0, f"expected non-zero exit for {case_name}"
        assert expected_stderr in r_fail.stderr, f"{case_name}: {r_fail.stderr!r}"



