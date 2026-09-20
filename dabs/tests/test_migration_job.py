"""Falsifiability tests for the migration Workflow-job entrypoint.

The migration job is a bundle-declared Databricks Workflow job whose task, when run: (a) reads the
table list, (b) BLOCKS until each synced table reports ONLINE before any dependent DDL, then
(c) renders the idempotent reconciling DDL via the shared Python renderer (`dabs/render_ddl.py`)
and applies it against Lakebase via a runtime OAuth token. The live apply runs against a real
Lakebase branch; here we prove the offline-unit seams.

The offline seams, each with a hostile fixture:

  Seam A — the wait-for-ONLINE gate (a pure/mockable function).
    Hostile status source: a table reports PROVISIONING then ONLINE, alongside a table that is
    ONLINE immediately. The gate must BLOCK until ONLINE and the migration must NOT run early.

  Seam C — the invoked render is idempotent/reconciling (re-run == safe no-op).
    The renderer yields IF NOT EXISTS / CREATE OR REPLACE / role-guard DDL, two renders are
    byte-identical, and there is NO `alembic_version` (or any version table) to roll back on the
    2nd apply — the exact class of bug the old alembic-rendered path had.

  Seam E — the migration connects to the TARGET BRANCH endpoint, not the instance default.
  Seam F — the CLI wrapper does not raise SystemExit on the success path.

We assert on OBSERVABLE behavior (poll ordering, emitted SQL, real double-apply state) — never on
internal function names. The status source / SDK are MOCKED; nothing hits a real workspace.
"""

from __future__ import annotations

import enum
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import types
from pathlib import Path

import pytest
import yaml

from dabs import migration_job as mj

REPO_ROOT = Path(__file__).resolve().parents[2]
# A hostile render fixture: alpha (1 index col, schema sch_alpha) + beta (0 index cols, sch_beta).
RENDER_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "render_fixture.json"


# --- hermetic throwaway Postgres (for the REAL double-apply proof; skips in the no-docker CI) ---

def _pg_binaries() -> dict[str, str] | None:
    """Locate initdb/pg_ctl/psql for a hermetic throwaway cluster, or None if absent.

    Present on a dev box with Postgres installed; ABSENT in the credential-free / no-docker
    offline CI (where the double-apply test skips). The Homebrew Postgres CLIs are keg-only (not on
    PATH), so we also probe the common keg bins.
    """
    search_dirs = [
        "",  # PATH
        "/opt/homebrew/opt/postgresql@17/bin",
        "/opt/homebrew/opt/postgresql@16/bin",
        "/usr/local/opt/postgresql@17/bin",
        "/usr/local/opt/postgresql@16/bin",
        "/usr/lib/postgresql/16/bin",
        "/usr/lib/postgresql/15/bin",
    ]
    for base in search_dirs:
        found: dict[str, str] = {}
        for name in ("initdb", "pg_ctl", "psql"):
            path = os.path.join(base, name) if base else shutil.which(name)
            if path and os.path.exists(path):
                found[name] = path
        if len(found) == 3:
            return found
    return None


_PG_BINS = _pg_binaries()
_needs_pg = pytest.mark.skipif(
    _PG_BINS is None,
    reason="no local Postgres (initdb/pg_ctl/psql) — the hermetic double-apply skips; the true "
    "live double-apply runs against a real Lakebase branch (and the Docker script under tests/)",
)


@pytest.fixture()
def hermetic_pg():
    """Spin up a throwaway single-use Postgres cluster on a unix socket (no TCP, no password, no
    docker), yield a `run(sql)` that pipes SQL through psql with ON_ERROR_STOP, and tear it down.

    A nonzero return code from `run` means psql hit a SQL error — which is exactly how a broken
    re-apply would surface.
    """
    bins = _PG_BINS
    assert bins is not None  # guarded by _needs_pg
    tmp = tempfile.mkdtemp(prefix="lkb-pg-")
    datadir = os.path.join(tmp, "data")
    sock = os.path.join(tmp, "sock")
    logfile = os.path.join(tmp, "server.log")
    os.makedirs(sock)
    subprocess.run(
        [bins["initdb"], "-D", datadir, "-A", "trust", "-U", "postgres", "--no-sync"],
        check=True, capture_output=True, text=True,
    )
    # `-l logfile` sends the SERVER's stdout/stderr to a file. This is not cosmetic: without it,
    # capturing pg_ctl's pipes would deadlock — the postgres daemon inherits and holds the pipe
    # open for its whole lifetime. `-w` makes pg_ctl wait until the server accepts connections.
    subprocess.run(
        [bins["pg_ctl"], "-D", datadir, "-l", logfile,
         "-o", f"-k {sock} -c listen_addresses=''", "-w", "start"],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        def run(sql: str) -> subprocess.CompletedProcess:
            return subprocess.run(
                [bins["psql"], "-v", "ON_ERROR_STOP=1", "-h", sock, "-U", "postgres", "-d",
                 "postgres", "-tA"],
                input=sql, capture_output=True, text=True,
            )
        yield run
    finally:
        subprocess.run(
            [bins["pg_ctl"], "-D", datadir, "-w", "-m", "immediate", "stop"],
            capture_output=True, text=True,
        )
        shutil.rmtree(tmp, ignore_errors=True)


def _norm(sql: str) -> str:
    """Collapse whitespace runs to one space for whitespace-robust substring matching."""
    return re.sub(r"\s+", " ", sql)


class SequenceStatus:
    """Stateful fake status source: each synced-table id has a status sequence and the LAST
    status sticks (an ONLINE table stays ONLINE). Records every (table_id, status) poll in order,
    so a test can prove the gate blocked through PROVISIONING rather than returning early.
    """

    def __init__(self, sequences: dict[str, list[str]]):
        self._seq = {k: list(v) for k, v in sequences.items()}
        self.calls: list[tuple[str, str]] = []

    def __call__(self, table_id: str) -> str:
        seq = self._seq[table_id]
        status = seq.pop(0) if len(seq) > 1 else seq[0]
        self.calls.append((table_id, status))
        return status


# --- Seam A: the wait-for-ONLINE gate -------------------------------------------------------

def test_gate_blocks_until_every_table_online():
    """A table that reports PROVISIONING twice then ONLINE must be re-polled until ONLINE; a table
    that is ONLINE immediately resolves at once. The gate only returns once ALL are ONLINE.
    """
    seqs = {
        "cat.sch.slow": ["PROVISIONING", "PROVISIONING", "ONLINE"],  # hostile: not ready twice
        "cat.sch.fast": ["ONLINE"],                                  # ready immediately
    }
    fake = SequenceStatus(seqs)
    slept: list[int] = []

    final = mj.wait_for_online(
        list(seqs), fake, sleep=lambda s: slept.append(s), poll_interval=5, timeout=100
    )

    slow_seen = [st for tid, st in fake.calls if tid == "cat.sch.slow"]
    assert slow_seen[-1] == "ONLINE", f"gate resolved on non-ONLINE status: {slow_seen}"
    assert slow_seen.count("PROVISIONING") >= 2, (
        f"gate did not block through PROVISIONING (returned early?): {slow_seen}"
    )
    assert slept, "gate never slept between polls — it returned on the first PROVISIONING"
    assert final == {"cat.sch.slow": "ONLINE", "cat.sch.fast": "ONLINE"}


def test_ddl_never_runs_before_all_tables_online():
    """The task GATES first, then applies DDL. At the moment DDL runs, every table must have most
    recently reported ONLINE. A deleted/early gate applies while `slow` is still PROVISIONING.
    """
    seqs = {
        "cat.sch.slow": ["PROVISIONING", "ONLINE"],
        "cat.sch.fast": ["ONLINE"],
    }
    fake = SequenceStatus(seqs)
    snapshots: list[dict[str, str]] = []

    def apply() -> str:
        latest: dict[str, str] = {}
        for tid, st in fake.calls:
            latest[tid] = st
        snapshots.append(latest)
        return "migrated"

    result = mj.run_migration_task(
        list(seqs), fake, apply, sleep=lambda s: None, poll_interval=1, timeout=50
    )

    assert result == "migrated"
    assert len(snapshots) == 1, f"migration applied {len(snapshots)} times, expected once"
    latest = snapshots[0]
    assert latest.get("cat.sch.slow") == "ONLINE", f"DDL ran before slow table ONLINE: {latest}"
    assert latest.get("cat.sch.fast") == "ONLINE", f"DDL ran before fast table ONLINE: {latest}"


def test_gate_raises_on_failed_status():
    """A FAILED/ERROR status is terminal — the gate raises rather than polling forever."""
    fake = SequenceStatus({"cat.sch.x": ["PROVISIONING", "FAILED"]})
    with pytest.raises(RuntimeError):
        mj.wait_for_online(["cat.sch.x"], fake, sleep=lambda s: None, poll_interval=1, timeout=50)


def test_gate_times_out_if_never_online():
    """The gate raises TimeoutError if a table never reaches ONLINE within the budget."""
    fake = SequenceStatus({"cat.sch.stuck": ["PROVISIONING"]})  # never advances
    with pytest.raises(TimeoutError):
        mj.wait_for_online(["cat.sch.stuck"], fake, sleep=lambda s: None, poll_interval=10, timeout=20)


# --- Seam B: a single migration implementation (the shared renderer, no alembic left) -----------

def test_bundle_declares_migration_job_running_the_entrypoint():
    """The bundle declares a Workflow job whose task runs the shared migration_job.py entrypoint
    (not inline-copied migration logic)."""
    bundle = yaml.safe_load((mj.repo_root() / "dabs" / "databricks.yml").read_text())
    jobs = (bundle.get("resources") or {}).get("jobs") or {}
    assert jobs, "no Workflow job declared in the bundle"
    blob = json.dumps(jobs)
    assert "migration_job.py" in blob, "job task does not run the migration_job.py entrypoint"


def test_no_alembic_machinery_remains():
    """The Alembic engine is GONE — no `alembic/` directory, no alembic version-migration on disk,
    and the entrypoint carries no live `alembic`/`alembic_version`/`upgrade head` dependency. The
    reconcile now runs the pure Python renderer.

    MUTATION GATE: reintroduce an `alembic upgrade head --sql` render or an `alembic_version` write
    and this goes RED.
    """
    root = mj.repo_root()
    assert not (root / "alembic").exists(), "an alembic/ directory still exists (engine not removed)"

    excluded = {".venv", ".venv-dev", "site-packages", "__pycache__", ".git", "node_modules", ".scratch"}

    def _tracked(p: Path) -> bool:
        return not (excluded & set(p.parts))

    _rev = re.compile(r"^\s*down_revision\s*=", re.MULTILINE)
    _upg = re.compile(r"def\s+upgrade\s*\(")
    version_migrations = []
    for p in root.rglob("*.py"):
        if not _tracked(p):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if _rev.search(text) and _upg.search(text):
            version_migrations.append(p)
    assert version_migrations == [], f"alembic version-migration still present: {version_migrations}"

    # The entrypoint's CODE (not its explanatory prose) must not run alembic or write a version row.
    # Tokenize and drop STRING/COMMENT tokens so the docstring's honest "why we removed alembic"
    # narrative does not trip this — only real identifiers (imports, calls) are inspected.
    import io
    import tokenize

    entry_src = (root / "dabs" / "migration_job.py").read_text()
    code_names = {
        tok.string
        for tok in tokenize.generate_tokens(io.StringIO(entry_src).readline)
        if tok.type == tokenize.NAME
    }
    for banned in ("alembic", "alembic_version", "make_rerunnable", "shared_alembic_dir"):
        assert banned not in code_names, f"entrypoint code still references the identifier {banned!r}"


# --- Seam D: config resolution survives the exec-without-__file__ runtime ---------------------
#
# THE runtime bug: the bundle spark_python_task runs the entrypoint via
# exec(compile(src, filename, "exec")) into a namespace where __file__ is NOT bound. repo_root()
# used Path(__file__), so a live serverless run died with NameError before the config was read.

_EXEC_WITHOUT_DUNDER_FILE = r'''
import json, sys
from pathlib import Path

entry = sys.argv[1]
config_root = sys.argv[2]  # <root>/dabs/migration_job.py has <root>/config/tables.json as sibling-of-dabs

src = Path(entry).read_text()
g = {"__name__": "not_dunder_main"}          # deliberately NOT "__main__", and crucially NO __file__
assert "__file__" not in g, "test harness leaked __file__ into the exec namespace"
sys.argv = [entry]                            # emulate the task's argv[0]; NO --config passed here
exec(compile(src, entry, "exec"), g)          # define the module's functions in a __file__-less ns

# repo_root() must resolve WITHOUT __file__ (via sys.argv[0]) and find config/tables.json.
rows = g["load_tables"]()                      # load_tables -> tables_config_path -> repo_root()
resolved = g["tables_config_path"]()
print(json.dumps({"rows": rows, "resolved": str(resolved)}))
'''


def test_config_resolves_when_dunder_file_undefined(tmp_path):
    """The config resolver locates config/tables.json in the deployed bundle tree even when
    __file__ is UNAVAILABLE — the exact serverless exec runtime that killed the live job.

    MUTATION GATE — restore the __file__-only resolution
    (`def repo_root(): return Path(__file__).resolve().parent.parent`) and this goes RED.
    """
    root = tmp_path / "files"
    (root / "dabs").mkdir(parents=True)
    (root / "config").mkdir(parents=True)
    entry = root / "dabs" / "migration_job.py"
    entry.write_bytes((REPO_ROOT / "dabs" / "migration_job.py").read_bytes())
    cfg_rows = [
        {"name": "sentinel_only_here", "synced_table_id": "c.s.sentinel"},
        {"name": "second_row", "synced_table_id": "c.s.second"},
    ]
    (root / "config" / "tables.json").write_text(json.dumps(cfg_rows))

    proc = subprocess.run(
        [sys.executable, "-c", _EXEC_WITHOUT_DUNDER_FILE, str(entry), str(root)],
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 0, (
        "resolver failed when __file__ was undefined (the live serverless exec runtime):\n"
        f"STDOUT:{proc.stdout}\nSTDERR:{proc.stderr}"
    )
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["rows"] == cfg_rows, f"resolved the wrong config rows: {out['rows']}"
    assert Path(out["resolved"]).resolve() == (root / "config" / "tables.json").resolve(), (
        f"resolved config path is not the deployed sibling-of-dabs location: {out['resolved']}"
    )


def test_bundle_task_passes_deployed_config_path():
    """The deploy passes the DEPLOYED config/ location to the task explicitly, via bundle
    interpolation — so the live task never depends on the __file__/argv fallback, and no literal
    workspace path is committed.

    Guards a regression that would re-expose the __file__ failure: drop the explicit --config param
    (or hardcode a literal path) and this goes RED.
    """
    bundle = yaml.safe_load((mj.repo_root() / "dabs" / "databricks.yml").read_text())
    tasks = bundle["resources"]["jobs"]["lakebase_migration"]["tasks"]
    params = tasks[0]["spark_python_task"]["parameters"]

    def _value_after(flag: str) -> str:
        assert flag in params, f"task parameters missing {flag}: {params}"
        return params[params.index(flag) + 1]

    assert _value_after("--config") == "${workspace.file_path}/config/tables.json", (
        f"--config is not the interpolated DEPLOYED config path: {params}"
    )
    # The alembic engine is gone — the task must NOT pass an --alembic-dir any more.
    assert "--alembic-dir" not in params, f"stale --alembic-dir still passed to the task: {params}"
    # Credential-free / no committed literal.
    blob = json.dumps(params)
    assert "/Workspace/" not in blob, f"a literal workspace path leaked into task params: {params}"


# --- Seam C: the invoked render is idempotent / reconciling (no version table) ------------------

def _assert_reconciling_shape(n: str) -> None:
    """The whitespace-normalized SQL has the RE-RUNNABLE reconciling shape a 2nd apply needs, and
    NO migration version table of any kind (nothing to roll back on the 2nd apply)."""
    assert "CREATE INDEX IF NOT EXISTS" in n, f"index DDL not re-runnable (no IF NOT EXISTS):\n{n}"
    assert "CREATE OR REPLACE VIEW" in n, f"view DDL not re-runnable (no OR REPLACE):\n{n}"
    assert "pg_roles" in n, f"role creation not guarded by a pg_roles existence check:\n{n}"
    assert "alembic_version" not in n, f"a version table leaked into the reconcile SQL:\n{n}"
    assert "version_num" not in n, f"a version-tracking column leaked into the reconcile SQL:\n{n}"


def test_invoked_migration_is_idempotent_reconciling():
    """The SQL the job task invokes (what `apply_sql_to_lakebase` executes) is fully re-runnable
    (guarded role / IF NOT EXISTS / OR REPLACE), two renders are byte-identical (no per-run state),
    and it carries NO version bookkeeping to collide on a 2nd apply.

    MUTATION GATE: drop the object-DDL IF NOT EXISTS / OR REPLACE, or reintroduce a version table,
    and this goes RED. The TRUE live double-apply is `test_rendered_migration_double_apply...`
    (hermetic) plus the Docker script under tests/ and the live apply against a Lakebase branch.
    """
    sql = mj.render_migration_sql(config_path=RENDER_FIXTURE)
    _assert_reconciling_shape(_norm(sql))
    assert mj.render_migration_sql(config_path=RENDER_FIXTURE) == sql, (
        "two renders differ — the invoked migration carries per-run state (not a clean no-op)"
    )


@_needs_pg
def test_rendered_migration_double_apply_reconciles(hermetic_pg):
    """THE bug, proven on a real Postgres: apply the rendered SQL TWICE against a throwaway cluster
    and assert the 2nd apply does NOT error AND the reconciling object DDL re-took effect after a
    (simulated) synced-table replace wiped it.

    The old alembic-rendered path rolled back on the 2nd apply (duplicate key on `alembic_version`).
    The renderer has no version table, so the 2nd apply is a clean reconciling no-op.
    """
    run = hermetic_pg
    # The synced tables the migration grants on / builds a view over exist at run time.
    setup = run(
        "CREATE SCHEMA sch_alpha; CREATE SCHEMA sch_beta;"
        " CREATE TABLE sch_alpha.alpha (id int, region_key text);"
        " CREATE TABLE sch_beta.beta (id int);"
    )
    assert setup.returncode == 0, f"fixture setup failed:\n{setup.stderr}"

    sql = mj.render_migration_sql(config_path=RENDER_FIXTURE)  # exactly what apply executes

    first = run(sql)
    assert first.returncode == 0, f"1st apply failed:\n{first.stderr}"

    # Simulate a synced-table replace wiping the reconciled objects (the live scenario).
    wipe = run("DROP VIEW sch_alpha.alpha_v; DROP INDEX sch_alpha.idx_alpha_region_key;")
    assert wipe.returncode == 0, f"could not wipe objects for the reconcile test:\n{wipe.stderr}"

    second = run(sql)
    assert second.returncode == 0, f"2nd apply errored (not re-runnable):\n{second.stderr}"

    check = run(
        "SELECT to_regclass('sch_alpha.alpha_v') IS NOT NULL,"
        " to_regclass('sch_alpha.idx_alpha_region_key') IS NOT NULL,"
        " EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'alpha_reader');"
    )
    assert check.returncode == 0, check.stderr
    # view + index reconciled back, and the role exists — a clean reconcile with no rollback.
    assert check.stdout.strip() == "t|t|t", (
        f"objects/role not reconciled after the 2nd apply: {check.stdout!r}"
    )


# --- Seam E: the migration connects to the TARGET BRANCH endpoint, not the instance default ----

_PROD_DNS = "instance-abc123.database.cloud.databricks.com"     # hostile decoy: the INSTANCE default
_BRANCH_HOST = "ep-branch-pr42.database.cloud.databricks.com"   # the TARGET branch endpoint host
_BRANCH = "projects/lakebase-cicd/branches/pr-42"
# The projects-API endpoint resource name the credential is minted from — NOT an instance name.
_ENDPOINT_NAME = "projects/lakebase-cicd/branches/pr-42/endpoints/ep-rw"


def _fake_endpoint(name: str, host: str):
    """A duck-typed compute Endpoint: carries the resource `name` (for credential minting) and
    `status.hosts.host` (for the conninfo), mirroring databricks.sdk...postgres.Endpoint."""
    return types.SimpleNamespace(
        name=name,
        status=types.SimpleNamespace(hosts=types.SimpleNamespace(host=host)),
    )


def test_conninfo_host_is_branch_endpoint_not_instance_default():
    """When a branch is given, the conninfo host is the BRANCH endpoint host — resolved from the
    ${var.lakebase_branch} value via the injected resolver, never the instance default (production).
    The credential is minted from that resolved ENDPOINT (projects API), NOT from any instance name.
    """
    seen_branches: list[str] = []

    def resolve_endpoint(branch: str):
        seen_branches.append(branch)
        return _fake_endpoint(_ENDPOINT_NAME, _BRANCH_HOST)

    cred_endpoints: list[str] = []

    def credential_source(endpoint: str) -> tuple[str, str]:
        cred_endpoints.append(endpoint)
        return ("svc@databricks.com", "tok-runtime-oauth")

    conninfo = mj._lakebase_conninfo(
        branch=_BRANCH,
        resolve_endpoint=resolve_endpoint,
        credential_source=credential_source,
    )

    fields = dict(kv.split("=", 1) for kv in conninfo.split())
    assert fields["host"] == _BRANCH_HOST, (
        f"conninfo host must be the branch endpoint, got {fields['host']!r} (prod decoy is {_PROD_DNS!r})"
    )
    assert fields["host"] != _PROD_DNS
    assert seen_branches == [_BRANCH], f"branch not passed to the endpoint resolver: {seen_branches}"
    assert cred_endpoints == [_ENDPOINT_NAME], (
        f"credential must be minted from the resolved ENDPOINT, not an instance name: {cred_endpoints}"
    )
    assert fields["user"] == "svc@databricks.com"
    assert fields["password"] == "tok-runtime-oauth"


@pytest.mark.parametrize("missing", [None, ""])
def test_conninfo_missing_branch_raises(missing):
    """A missing/empty branch must RAISE — never silently fall back to the instance default
    endpoint (production). NEITHER the endpoint resolver NOR the credential source is consulted."""
    resolver_calls: list[str] = []
    cred_calls: list[str] = []

    def resolve_endpoint(branch: str):
        resolver_calls.append(branch)
        return _fake_endpoint(_ENDPOINT_NAME, "should-not-be-used.example")

    def credential_source(endpoint: str) -> tuple[str, str]:
        cred_calls.append(endpoint)
        return ("u", "t")

    with pytest.raises(ValueError):
        mj._lakebase_conninfo(
            branch=missing,
            resolve_endpoint=resolve_endpoint,
            credential_source=credential_source,
        )
    assert resolver_calls == [], "endpoint resolver must not be called when the branch is missing"
    assert cred_calls == [], "credential source must not be called when the branch is missing"


def test_main_requires_branch_for_live_apply(monkeypatch, tmp_path):
    """A live apply (no --dry-run) must REQUIRE --branch / LAKEBASE_BRANCH. With no branch at all
    (and no instance flag to give either), argparse's parser.error exits with SystemExit(2)."""
    cfg = tmp_path / "tables.json"
    cfg.write_text('[{"synced_table_id": "cat.sch.tbl"}]')
    monkeypatch.setattr(mj, "render_migration_sql", lambda **kw: "SELECT 1;")
    monkeypatch.delenv("LAKEBASE_BRANCH", raising=False)

    with pytest.raises(SystemExit) as exc:
        mj.main(["--config", str(cfg)])  # branch absent -> refused
    assert exc.value.code == 2, "missing --branch on a live apply must exit non-zero (parser.error)"


def test_main_no_instance_needed_proceeds_to_apply(monkeypatch, tmp_path):
    """A live apply needs ONLY --branch — no instance name is threaded anywhere. The projects API
    mints the credential from the branch endpoint (workspace-scoped token), so main proceeds to the
    (mocked) gate + apply with a branch and no instance.

    MUTATION GATE: re-add a required --instance / LAKEBASE_INSTANCE_NAME check to main and this goes
    RED (main would refuse to proceed without an instance).
    """
    cfg = tmp_path / "tables.json"
    cfg.write_text('[{"synced_table_id": "cat.sch.tbl"}]')
    monkeypatch.setattr(mj, "render_migration_sql", lambda **kw: "SELECT 1;")
    monkeypatch.setattr(mj, "sdk_status_source", lambda: (lambda tid: "ONLINE"))
    monkeypatch.delenv("LAKEBASE_INSTANCE_NAME", raising=False)

    applied: dict[str, object] = {}

    def fake_apply(sql, *, branch):
        applied["sql"] = sql
        applied["branch"] = branch

    monkeypatch.setattr(mj, "apply_sql_to_lakebase", fake_apply)

    rc = mj.main(["--config", str(cfg), "--branch", "projects/p/branches/b"])
    assert rc == 0, "a live apply with a branch and no instance must succeed"
    assert applied["branch"] == "projects/p/branches/b"
    assert applied["sql"] == "SELECT 1;"


def test_selected_endpoint_is_read_write_for_host_and_credential():
    """The SAME branch READ_WRITE compute endpoint supplies BOTH the conninfo host AND the
    credential-minting endpoint name. Hostile fixture: the READ_ONLY endpoint is listed FIRST (with
    its own name + host) and the read-write marker sits behind an enum `.value`, so a blind `[0]`
    pick would connect to the wrong host and mint the token against the read-only endpoint."""
    class _EndpointType(enum.Enum):  # mirrors databricks.sdk.service.postgres.EndpointType
        ENDPOINT_TYPE_READ_ONLY = "ENDPOINT_TYPE_READ_ONLY"
        ENDPOINT_TYPE_READ_WRITE = "ENDPOINT_TYPE_READ_WRITE"

    def _ep(name: str, host: str, etype: _EndpointType):
        return types.SimpleNamespace(
            name=name,
            status=types.SimpleNamespace(
                hosts=types.SimpleNamespace(host=host),
                endpoint_type=etype,
            ),
        )

    eps = [
        _ep("projects/p/branches/b/endpoints/ro", "ro-host.example", _EndpointType.ENDPOINT_TYPE_READ_ONLY),
        _ep("projects/p/branches/b/endpoints/rw", "rw-host.example", _EndpointType.ENDPOINT_TYPE_READ_WRITE),
    ]
    ep = mj._select_endpoint(eps)
    assert mj._endpoint_host(ep) == "rw-host.example", (
        "selector did not prefer the READ_WRITE endpoint host (a migration writes)"
    )
    assert mj._endpoint_name(ep) == "projects/p/branches/b/endpoints/rw", (
        "selector did not carry the READ_WRITE endpoint's resource name for credential minting"
    )


def test_host_from_endpoints_prefers_read_write():
    """The pure host picker returns the READ_WRITE endpoint's connection host (a migration WRITES).
    Hostile fixture: the READ_ONLY endpoint is listed FIRST, so a blind `[0]` pick grabs the wrong
    host, and the read-write marker sits behind an enum `.value`."""
    class _EndpointType(enum.Enum):  # mirrors databricks.sdk.service.postgres.EndpointType
        ENDPOINT_TYPE_READ_ONLY = "ENDPOINT_TYPE_READ_ONLY"
        ENDPOINT_TYPE_READ_WRITE = "ENDPOINT_TYPE_READ_WRITE"

    def _ep(host: str, etype: _EndpointType):
        return types.SimpleNamespace(
            name="projects/p/branches/b/endpoints/e",
            status=types.SimpleNamespace(
                hosts=types.SimpleNamespace(host=host),
                endpoint_type=etype,
            )
        )

    eps = [
        _ep("ro-host.example", _EndpointType.ENDPOINT_TYPE_READ_ONLY),
        _ep("rw-host.example", _EndpointType.ENDPOINT_TYPE_READ_WRITE),
    ]
    assert mj._host_from_endpoints(eps) == "rw-host.example", (
        "host picker did not prefer the READ_WRITE endpoint (a migration writes)"
    )


def test_apply_threads_branch_into_conninfo(monkeypatch):
    """apply_sql_to_lakebase threads the branch through to the conninfo builder, so the live apply
    connects to the branch endpoint. It passes NO instance name (the conninfo builder takes only a
    branch now) — the fake conninfo has no **kw, so a leftover instance positional/kwarg raises."""
    captured: dict[str, object] = {}

    def fake_conninfo(*, branch):
        captured["branch"] = branch
        return "host=x port=5432 dbname=d user=u password=p sslmode=require"

    monkeypatch.setattr(mj, "_lakebase_conninfo", fake_conninfo)

    fake_psycopg = types.ModuleType("psycopg")

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql):
            captured["sql"] = sql

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def cursor(self):
            return _Cur()

    fake_psycopg.connect = lambda conninfo, autocommit=False: _Conn()
    monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)

    mj.apply_sql_to_lakebase("SELECT 1;", branch="projects/p/branches/b")

    assert captured["branch"] == "projects/p/branches/b", "branch not threaded into the conninfo builder"
    assert captured["sql"] == "SELECT 1;"


def test_bundle_task_passes_branch_variable():
    """The deploy passes the target branch to the task via ${var.lakebase_branch}, so the job
    migrates the TARGET branch endpoint, never the instance default."""
    bundle = yaml.safe_load((mj.repo_root() / "dabs" / "databricks.yml").read_text())
    params = bundle["resources"]["jobs"]["lakebase_migration"]["tasks"][0]["spark_python_task"]["parameters"]
    assert "--branch" in params, f"task parameters missing --branch: {params}"
    assert params[params.index("--branch") + 1] == "${var.lakebase_branch}", (
        f"--branch is not the interpolated lakebase_branch var: {params}"
    )


# --- Seam F: the entrypoint wrapper must NOT raise SystemExit on the success path -------------

def test_run_cli_success_does_not_raise_systemexit(monkeypatch):
    """SUCCESS path: when main() returns 0, the CLI wrapper returns normally and does NOT raise
    SystemExit — so the serverless spark_python_task (which reports ANY raised SystemExit, even
    code 0, as a task FAILURE) is marked SUCCEEDED.
    """
    monkeypatch.setattr(mj, "main", lambda argv=None: 0)
    rc = mj._run_cli([])
    assert rc == 0, f"wrapper did not return the success code; got {rc!r}"


def test_run_cli_failure_raises_systemexit_with_code(monkeypatch):
    """FAILURE path: when main() returns a NON-ZERO code, the wrapper raises SystemExit carrying
    that exact code."""
    monkeypatch.setattr(mj, "main", lambda argv=None: 3)
    with pytest.raises(SystemExit) as exc:
        mj._run_cli([])
    assert exc.value.code == 3, f"wrapper did not propagate the non-zero code; got {exc.value.code!r}"


def test_run_cli_threads_argv_into_main(monkeypatch):
    """The wrapper passes its argv through to main() unchanged, and still returns normally on the
    success code."""
    seen: dict[str, object] = {}

    def fake_main(argv=None):
        seen["argv"] = argv
        return 0

    monkeypatch.setattr(mj, "main", fake_main)
    mj._run_cli(["--dry-run", "--config", "x.json"])
    assert seen["argv"] == ["--dry-run", "--config", "x.json"], (
        f"wrapper did not thread argv into main: {seen.get('argv')!r}"
    )


# --- Seam G: the live wait-gate status source reads the WORKING synced-tables endpoint ---------

def _install_fake_sdk(monkeypatch, api_client) -> None:
    """Inject a fake `databricks.sdk` whose WorkspaceClient exposes the given `api_client`.

    `sdk_status_source` does `from databricks.sdk import WorkspaceClient` lazily; the offline suite
    has NO databricks-sdk installed, so we register both the `databricks` package and its `.sdk`
    submodule in sys.modules (mirroring the psycopg fake-module pattern used by the apply test)."""
    class _WorkspaceClient:
        def __init__(self, *a, **k):
            self.api_client = api_client

    fake_sdk = types.ModuleType("databricks.sdk")
    fake_sdk.WorkspaceClient = _WorkspaceClient
    fake_pkg = types.ModuleType("databricks")
    fake_pkg.sdk = fake_sdk
    monkeypatch.setitem(sys.modules, "databricks", fake_pkg)
    monkeypatch.setitem(sys.modules, "databricks.sdk", fake_sdk)


def test_sdk_status_source_reads_working_synced_tables_endpoint(monkeypatch):
    """`sdk_status_source().get_status(id)` must read the status from the WORKING postgres
    synced-tables REST endpoint — `GET /api/2.0/postgres/synced_tables/{id}` — and return
    `status.detailed_state`.

    LIVE REGRESSION this locks down: the SDK method `workspace.postgres.get_synced_table(name=<3-part
    id>)` mapped at RUNTIME to `GET /postgres/{id}`, a path the workspace has NO API for
    (`NotFound: No API found for 'GET /postgres/...'`). The wait-gate runs BEFORE the apply, so the
    whole job died there. The generic REST call below is the path proven live via `databricks api get`.

    Hostile fixture: detailed_state is a NON-online marker (`SYNCED_TABLE_PROVISIONING`) returned
    verbatim — so the test cannot pass by accidentally matching the gate's ONLINE substring; it must
    return the field the endpoint actually carries. We assert BOTH the HTTP method+path AND the state.
    """
    calls: list[tuple[str, str]] = []

    class _ApiClient:
        def do(self, method, path, *args, **kwargs):
            calls.append((method, path))
            return {"status": {"detailed_state": "SYNCED_TABLE_PROVISIONING"}}

    _install_fake_sdk(monkeypatch, _ApiClient())

    get_status = mj.sdk_status_source()
    state = get_status("cat.sch.tbl")

    assert calls == [("GET", "/api/2.0/postgres/synced_tables/cat.sch.tbl")], (
        f"status source did not GET the working synced-tables endpoint (3-part id in the path): {calls}"
    )
    assert state == "SYNCED_TABLE_PROVISIONING", (
        f"status source did not return status.detailed_state verbatim; got {state!r}"
    )


def test_sdk_status_source_defaults_unknown_when_state_absent(monkeypatch):
    """When the endpoint response carries no `status.detailed_state`, get_status returns 'UNKNOWN'
    (so the gate keeps polling rather than crashing on a missing field)."""
    class _ApiClient:
        def do(self, method, path, *args, **kwargs):
            return {"status": {}}  # no detailed_state present

    _install_fake_sdk(monkeypatch, _ApiClient())

    assert mj.sdk_status_source()("cat.sch.tbl") == "UNKNOWN"
