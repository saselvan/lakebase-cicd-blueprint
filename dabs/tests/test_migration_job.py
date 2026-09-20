"""Falsifiability tests for the Alembic-in-a-Workflow-job migration task.

The migration job is a bundle-declared Databricks Workflow job whose task, when run: (a) reads the
table list, (b) BLOCKS until each synced table reports ONLINE before any dependent DDL, then
(c) runs the EXISTING `alembic/` migration (reused, not copied) against Lakebase via a runtime
OAuth token. The live apply runs against a real Lakebase branch; here we prove the offline-unit seams.

The three offline seams, each with a hostile fixture, mapped to the reviewer's named mutations:

  Seam A — the wait-for-ONLINE gate (highest seam, a pure/mockable function).
    Hostile status source: a table reports PROVISIONING then ONLINE, alongside a table that is
    ONLINE immediately. The gate must BLOCK until ONLINE and the migration must NOT run early.
    Mutation 1 (delete the gate) => `test_ddl_never_runs_before_all_tables_online` red.

  Seam B — single migration implementation (reuse, not copy).
    The task points at the shared `alembic/` dir; there is exactly one migration on disk.
    Mutation 2 (point the task at a COPIED migration) => `test_single_migration_implementation` red.

  Seam C — the invoked path is idempotent/reconciling (re-run == safe no-op).
    Rendering the shared migration yields IF NOT EXISTS / CREATE OR REPLACE / role-guard DDL, and
    two renders are byte-identical.
    Mutation 3 (break idempotency in the invoked path) => `test_invoked_migration_is_idempotent...` red.

We assert on OBSERVABLE behavior (poll ordering, on-disk layout, emitted SQL) — never on internal
function names. The status source / SDK are MOCKED; nothing hits a real workspace or Lakebase.
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
# Reuse the SHARED alembic hostile fixture (alpha: 1 index col; beta: 0 index cols) — no dup fixture.
ALEMBIC_FIXTURE = REPO_ROOT / "alembic" / "tests" / "fixtures" / "tables.json"


def _alembic_runtime_available() -> bool:
    """True only if the INSTALLED alembic package is runnable via sys.executable — the exact thing
    render_migration_sql shells out to. NOTE: importlib.util.find_spec('alembic') is NOT usable here
    because the repo's local `alembic/` DIRECTORY matches as a namespace package even with no alembic
    installed; this probe imports `alembic.config`, which the local dir does not provide.
    """
    probe = subprocess.run(
        [sys.executable, "-c", "import alembic.config, sqlalchemy"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    return probe.returncode == 0


_HAS_ALEMBIC = _alembic_runtime_available()
_needs_alembic = pytest.mark.skipif(
    not _HAS_ALEMBIC,
    reason="alembic not installed; the render seam shells out to it (the CI 'alembic' job has it)",
)


# --- hermetic throwaway Postgres (for the REAL double-apply proof; skips in the no-docker CI) ---

def _pg_binaries() -> dict[str, str] | None:
    """Locate initdb/pg_ctl/psql for a hermetic throwaway cluster, or None if absent.

    Present on a dev box with Postgres installed; ABSENT in the credential-free / no-docker
    offline CI (where the double-apply test skips exactly like the alembic-render tests do). The
    Homebrew Postgres CLIs are keg-only (not on PATH), so we also probe the common keg bins.
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
    "live double-apply runs against a real Lakebase branch",
)


@pytest.fixture()
def hermetic_pg():
    """Spin up a throwaway single-use Postgres cluster on a unix socket (no TCP, no password, no
    docker), yield a `run(sql)` that pipes SQL through psql with ON_ERROR_STOP, and tear it down.

    A nonzero return code from `run` means psql hit a SQL error — which is exactly how the
    unguarded 2nd apply surfaces (DuplicateTable on `CREATE TABLE alembic_version`).
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
    # open for its whole lifetime, so `subprocess.run` would block waiting for an EOF that never
    # comes. `-w` makes pg_ctl wait until the server accepts connections before returning.
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


# A representative `alembic upgrade head --sql` blob (offline, from base). The version bookkeeping
# is verbatim what alembic emits and is UNGUARDED; the object DDL between BEGIN/COMMIT is the
# reconciling payload that must survive a re-run. Hostile element: the version INSERT carries a
# RETURNING clause (alembic emits one) — so a naive "append before the trailing ';'" guard would
# land AFTER RETURNING and be wrong; the conflict guard must inject BEFORE RETURNING.
_ALEMBIC_SAMPLE = """BEGIN;

CREATE TABLE alembic_version (
    version_num VARCHAR(32) NOT NULL,
    CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)
);

-- Running upgrade  -> 0001_app_role

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'alpha_reader') THEN
    CREATE ROLE alpha_reader NOLOGIN;
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_alpha_region_key ON sch_alpha.alpha (region_key);

CREATE OR REPLACE VIEW sch_alpha.alpha_v AS SELECT * FROM sch_alpha.alpha;

INSERT INTO alembic_version (version_num) VALUES ('0001_app_role') RETURNING alembic_version.version_num;

COMMIT;
"""


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

    Guards mutation 1: a gate that returns on the first poll leaves `slow` at PROVISIONING.
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

    Guards mutation 1: delete the wait-for-ONLINE gate => this goes red.
    """
    seqs = {
        "cat.sch.slow": ["PROVISIONING", "ONLINE"],
        "cat.sch.fast": ["ONLINE"],
    }
    fake = SequenceStatus(seqs)
    snapshots: list[dict[str, str]] = []

    def apply() -> str:
        # Latest status each table has reported at the instant DDL executes.
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


# --- Seam B: single migration implementation (reuse, not copy) ------------------------------

def test_single_migration_implementation():
    """The task reuses the ONE shared `alembic/` migration; there is no copied migration anywhere
    in the repo — under ANY filename or directory — and the task's locator points at that single
    directory.

    Guards mutation 2: point the task at a COPIED migration => this goes red. The copy is detected
    by its alembic-migration SHAPE (a module-level `down_revision = ` + a `def upgrade(`), not by a
    hard-coded filename, so `cp 0001_app_role.py dabs/whatever_9999.py` is caught too.
    """
    root = mj.repo_root()
    excluded = {".venv", "site-packages", "__pycache__", ".git", "node_modules"}
    shared_alembic = root / "alembic"
    shared_migration = shared_alembic / "versions" / "0001_app_role.py"

    def _tracked(p: Path) -> bool:
        return not (excluded & set(p.parts))

    _rev = re.compile(r"^\s*down_revision\s*=", re.MULTILINE)
    _upg = re.compile(r"def\s+upgrade\s*\(")

    # (a) Exactly ONE alembic version-migration on disk (any name/dir), and it is the shared one.
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
    assert version_migrations == [shared_migration], (
        f"expected exactly one alembic version-migration (the shared one); found {version_migrations}"
    )

    # (b) The task's shared-alembic locator points at the single alembic/ dir.
    assert mj.shared_alembic_dir() == shared_alembic, (
        f"task's shared-alembic locator does not point at alembic/: {mj.shared_alembic_dir()}"
    )

    # (c) No copied alembic env.py anywhere OUTSIDE the shared alembic/ tree (a self-contained fork
    # would ship its own env.py); the one under alembic/ is the shared original.
    env_copies = [
        p
        for p in root.rglob("env.py")
        if _tracked(p) and shared_alembic not in p.parents and p.parent != shared_alembic
    ]
    assert env_copies == [], f"copied alembic env outside the shared alembic/ tree: {env_copies}"


def test_bundle_declares_migration_job_running_the_entrypoint():
    """The bundle declares a Workflow job whose task runs the shared migration_job.py entrypoint
    (not inline-copied migration logic).

    Guards mutation 2: the job must reuse the entrypoint, so the migration has
    a single implementation on Databricks compute too.
    """
    bundle = yaml.safe_load((mj.repo_root() / "dabs" / "databricks.yml").read_text())
    jobs = (bundle.get("resources") or {}).get("jobs") or {}
    assert jobs, "no Workflow job declared in the bundle"
    blob = json.dumps(jobs)
    assert "migration_job.py" in blob, "job task does not run the migration_job.py entrypoint"


# --- Seam D: config/alembic resolution survives the exec-without-__file__ runtime -------------
#
# THE runtime bug: the bundle spark_python_task runs the entrypoint via
# exec(compile(src, filename, "exec")) into a namespace where __file__ is NOT bound. repo_root()
# used Path(__file__), so a live serverless run died with
#   NameError: name '__file__' is not defined
# at load_tables -> tables_config_path -> repo_root(), BEFORE the config was ever read. Only the
# unit suite (which imports the module, where __file__ IS bound) ever ran it, so it never surfaced.
#
# Two layered guarantees, each with a falsifiable test:
#   (1) repo_root() no longer needs __file__  -> test_config_resolves_when_dunder_file_undefined.
#   (2) the deploy passes the DEPLOYED config/alembic paths explicitly (belt to (1)'s suspenders)
#       -> test_bundle_task_passes_deployed_config_and_alembic_paths.

# A child that reproduces the bundle runtime EXACTLY: read the entrypoint source and run it via
# exec(compile(...)) into a globals dict with NO "__file__" key, with sys.argv[0] set to the
# entrypoint path (as a spark_python_task sets it). Then call load_tables() — which reaches
# repo_root(). If repo_root() still needs __file__, this raises NameError and the child exits != 0.
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

    Reproduces the runtime faithfully: a deployed-like tree (<root>/dabs/migration_job.py with
    <root>/config/tables.json as a sibling-of-dabs, matching the deploy's sync manifest) whose
    entrypoint is run via exec(compile(...)) into a namespace with no __file__.

    MUTATION GATE — restore the __file__-only resolution
    (`def repo_root(): return Path(__file__).resolve().parent.parent`) and this goes RED: the
    exec namespace has no __file__, so repo_root() raises NameError, load_tables() never returns,
    the child exits non-zero, and the returncode/JSON assertions below fail.
    """
    # Mirror the DEPLOYED layout: files/{dabs/migration_job.py, config/tables.json}.
    root = tmp_path / "files"
    (root / "dabs").mkdir(parents=True)
    (root / "config").mkdir(parents=True)
    entry = root / "dabs" / "migration_job.py"
    entry.write_bytes((REPO_ROOT / "dabs" / "migration_job.py").read_bytes())
    # A hostile config: a KNOWN, distinctive row that a mirror/hardcoded resolver could not fake,
    # and an extra row so a single-row assumption is caught too.
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
    # And it resolved to the config that sits as a sibling-of-dabs (the deployed layout), proving
    # repo_root() walked dabs/ -> files/ -> files/config, not some unrelated path.
    assert Path(out["resolved"]).resolve() == (root / "config" / "tables.json").resolve(), (
        f"resolved config path is not the deployed sibling-of-dabs location: {out['resolved']}"
    )


def test_bundle_task_passes_deployed_config_and_alembic_paths():
    """The deploy passes the DEPLOYED config/ and alembic/ locations to the task explicitly, via
    bundle interpolation — so the live task never depends on the __file__/argv fallback at all, and
    no literal workspace path is committed.

    Guards a regression that would re-expose the __file__ failure: drop the explicit --config /
    --alembic-dir params (or hardcode a literal path) and this goes RED.
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
    assert _value_after("--alembic-dir") == "${workspace.file_path}/alembic", (
        f"--alembic-dir is not the interpolated DEPLOYED alembic dir: {params}"
    )
    # Credential-free / no committed literal: the deployed paths are interpolated, never hardcoded.
    blob = json.dumps(params)
    assert "/Workspace/" not in blob, f"a literal workspace path leaked into task params: {params}"


@_needs_alembic
def test_task_render_reuses_shared_alembic():
    """The task's default render (no alembic_dir) is byte-identical to an explicit render from the
    shared alembic/ dir — proving the task renders THROUGH the shared code, not a divergent copy.
    """
    default = mj.render_migration_sql(config_path=ALEMBIC_FIXTURE)
    explicit = mj.render_migration_sql(alembic_dir=mj.repo_root() / "alembic", config_path=ALEMBIC_FIXTURE)
    assert default.strip(), "shared alembic render produced no SQL"
    assert default == explicit


# --- Seam C: the invoked path is idempotent / reconciling -----------------------------------

def _assert_rerunnable_shape(n: str) -> None:
    """Assert the whitespace-normalized SQL has the RE-RUNNABLE shape a 2nd apply needs:
      * the version-table create is guarded (`CREATE TABLE IF NOT EXISTS alembic_version`), and the
        UNGUARDED `CREATE TABLE alembic_version` form is gone — no DuplicateTable on re-apply;
      * the version INSERT is conflict-safe (`ON CONFLICT`), and the guard lands BEFORE any
        RETURNING clause — no PK unique-violation on re-apply;
      * the reconciling object DDL is preserved untouched (role guard / IF NOT EXISTS / OR REPLACE).
    """
    assert "CREATE TABLE IF NOT EXISTS alembic_version" in n, (
        f"version-table create is not guarded — 2nd apply raises DuplicateTable:\n{n}"
    )
    assert re.search(r"CREATE TABLE\s+alembic_version", n) is None, (
        f"an UNGUARDED `CREATE TABLE alembic_version` remains — 2nd apply raises DuplicateTable:\n{n}"
    )
    ins = re.search(r"INSERT INTO alembic_version[^;]*;", n)
    assert ins, f"no alembic_version INSERT found in rendered SQL:\n{n}"
    stmt = ins.group(0)
    assert "ON CONFLICT" in stmt, (
        f"version INSERT is not conflict-safe — 2nd apply raises a PK unique violation:\n{stmt}"
    )
    if "RETURNING" in stmt:
        assert stmt.index("ON CONFLICT") < stmt.index("RETURNING"), (
            f"ON CONFLICT must precede RETURNING to be valid Postgres:\n{stmt}"
        )
    # Object DDL still reconciling (the whole point — must re-apply on every run).
    assert "CREATE INDEX IF NOT EXISTS" in n, f"index DDL not re-runnable (no IF NOT EXISTS):\n{n}"
    assert "CREATE OR REPLACE VIEW" in n, f"view DDL not re-runnable (no OR REPLACE):\n{n}"
    assert "pg_roles" in n, f"role creation not guarded by a pg_roles existence check:\n{n}"


def test_make_rerunnable_guards_version_bookkeeping():
    """`make_rerunnable` is the pure transform that turns alembic's UNGUARDED version bookkeeping
    into re-runnable SQL, WITHOUT touching the reconciling object DDL. This runs with no alembic
    and no Postgres — so it lands in the no-cloud `dabs-validate` CI job that guards the fix.

    Guards the new mutation: remove the alembic_version IF-NOT-EXISTS / conflict guard => red.
    """
    out = mj.make_rerunnable(_ALEMBIC_SAMPLE)
    _assert_rerunnable_shape(_norm(out))
    # The guard is confined to the bookkeeping: object DDL bytes are unchanged.
    assert "CREATE INDEX IF NOT EXISTS idx_alpha_region_key ON sch_alpha.alpha (region_key)" in out
    assert "CREATE OR REPLACE VIEW sch_alpha.alpha_v AS SELECT * FROM sch_alpha.alpha" in out
    # Idempotent transform: applying it to already-guarded SQL is a fixpoint (no double guard).
    assert mj.make_rerunnable(out) == out


@_needs_alembic
def test_invoked_migration_is_idempotent_reconciling():
    """The SQL the job task invokes (what `apply_sql_to_lakebase` executes) is fully re-runnable:
    the version bookkeeping is guarded AND the object DDL is reconciling — and two renders are
    byte-identical (no per-run state). Proves the transform holds on alembic's REAL output, so the
    hard-coded sample above cannot silently drift from what alembic actually emits.

    Guards mutation 3 (drop object-DDL IF NOT EXISTS / OR REPLACE) AND the new mutation (drop the
    version-bookkeeping guard) => this goes red. The TRUE live double-apply is
    `test_rendered_migration_double_apply_reconciles` (hermetic) and the live apply against a real
    Lakebase branch.
    """
    sql = mj.render_migration_sql(config_path=ALEMBIC_FIXTURE)
    _assert_rerunnable_shape(_norm(sql))
    assert mj.render_migration_sql(config_path=ALEMBIC_FIXTURE) == sql, (
        "two renders differ — the invoked migration carries per-run state (not a clean no-op)"
    )


@_needs_alembic
@_needs_pg
def test_rendered_migration_double_apply_reconciles(hermetic_pg):
    """THE bug, proven live: apply the rendered SQL TWICE against a hermetic throwaway Postgres and
    assert the 2nd apply does not error AND the reconciling object DDL re-took effect after a
    (simulated) synced-table replace wiped it.

    Without the version-bookkeeping guard, the unguarded `CREATE TABLE alembic_version` raises
    DuplicateTable, the `BEGIN;…COMMIT;` rolls back, and the object DDL never re-applies — which is
    exactly the "re-running is a reconciling no-op" contract and the live replace scenario.
    """
    run = hermetic_pg
    # The synced tables the migration grants on / builds a view over exist at run time.
    setup = run(
        "CREATE SCHEMA sch_alpha; CREATE SCHEMA sch_beta;"
        " CREATE TABLE sch_alpha.alpha (id int, region_key text);"
        " CREATE TABLE sch_beta.beta (id int);"
    )
    assert setup.returncode == 0, f"fixture setup failed:\n{setup.stderr}"

    sql = mj.render_migration_sql(config_path=ALEMBIC_FIXTURE)  # exactly what apply executes

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
        " (SELECT count(*) FROM alembic_version);"
    )
    assert check.returncode == 0, check.stderr
    # to_regclass NOT NULL for both (view + index reconciled back), version row count exactly 1.
    assert check.stdout.strip() == "t|t|1", (
        f"objects not reconciled or version row duplicated after 2nd apply: {check.stdout!r}"
    )


# --- Seam E: the migration connects to the TARGET BRANCH endpoint, not the instance default ----
#
# The branch-endpoint requirement: an earlier version built the psycopg conninfo from the Lakebase
# INSTANCE `read_write_dns` — which is the instance DEFAULT endpoint = the PRODUCTION branch. So the
# migration job connected to production, not the ephemeral target branch, and raised
#   InvalidSchemaName: schema "cicd_dabs" does not exist
# because the intended target branch was never migrated. The fix: resolve the host from the
# ${var.lakebase_branch} value ("projects/<proj>/branches/<branch>") via that branch's compute
# endpoint (SDK postgres.list_endpoints -> status.hosts.host — the SAME field the proven-live
# scripts/branch_test.sh reads as JSON[0]['status']['hosts']['host']), NOT the instance read_write_dns.
#
# All seams are injected as pure callables (mirroring the get_status / sdk_status_source pattern),
# so these run fully offline with NO databricks-sdk and NO psycopg installed.

_PROD_DNS = "instance-abc123.database.cloud.databricks.com"     # hostile decoy: the INSTANCE default
_BRANCH_HOST = "ep-branch-pr42.database.cloud.databricks.com"   # the TARGET branch endpoint host
_BRANCH = "projects/lakebase-cicd/branches/pr-42"


def test_conninfo_host_is_branch_endpoint_not_instance_default():
    """When a branch is given, the conninfo host is the BRANCH endpoint host — resolved from the
    ${var.lakebase_branch} value via the injected resolver, never the instance default (production).

    MUTATION GATE — goes RED here:
      revert host resolution to a silent instance-default fallback / a hardcoded prod host
      -> host is not the branch endpoint.
    """
    seen_branches: list[str] = []

    def resolve_host(branch: str) -> str:
        seen_branches.append(branch)
        return _BRANCH_HOST

    def credential_source(instance_name: str) -> tuple[str, str]:
        return ("svc@databricks.com", "tok-runtime-oauth")

    conninfo = mj._lakebase_conninfo(
        "my-instance",
        branch=_BRANCH,
        resolve_host=resolve_host,
        credential_source=credential_source,
    )

    fields = dict(kv.split("=", 1) for kv in conninfo.split())
    assert fields["host"] == _BRANCH_HOST, (
        f"conninfo host must be the branch endpoint, got {fields['host']!r} "
        f"(prod decoy is {_PROD_DNS!r})"
    )
    assert fields["host"] != _PROD_DNS, (
        "conninfo used the INSTANCE DEFAULT (production) host instead of the target branch endpoint"
    )
    assert seen_branches == [_BRANCH], f"branch not passed to the host resolver: {seen_branches}"
    # The runtime-OAuth credential path still populates user/password (no stored secret).
    assert fields["user"] == "svc@databricks.com"
    assert fields["password"] == "tok-runtime-oauth"


@pytest.mark.parametrize("missing", [None, ""])
def test_conninfo_missing_branch_raises(missing):
    """A missing/empty branch must RAISE — never silently fall back to the instance default
    endpoint (production). That fallback was the defect; the branch is now REQUIRED, so
    the dead-but-dangerous fallback machinery is gone. Observable behavior: a ValueError is raised
    and NEITHER the host resolver NOR the credential source is ever consulted (so no accidental
    instance-default connection is even attempted).

    MUTATION GATE: re-add a silent instance-default fallback instead of raising -> this goes RED.
    """
    resolver_calls: list[str] = []
    cred_calls: list[str] = []

    def resolve_host(branch: str) -> str:
        resolver_calls.append(branch)
        return "should-not-be-used.example"

    def credential_source(instance_name: str) -> tuple[str, str]:
        cred_calls.append(instance_name)
        return ("u", "t")

    with pytest.raises(ValueError):
        mj._lakebase_conninfo(
            "my-instance",
            branch=missing,
            resolve_host=resolve_host,
            credential_source=credential_source,
        )
    assert resolver_calls == [], "host resolver must not be called when the branch is missing"
    assert cred_calls == [], "credential source must not be called when the branch is missing"


def test_main_requires_branch_for_live_apply(monkeypatch, tmp_path):
    """A live apply (no --dry-run) must REQUIRE --branch / LAKEBASE_BRANCH, exactly as it already
    requires --instance — so it can never fall back to the instance default endpoint (production).
    argparse's parser.error exits with SystemExit(2). Guards the required-branch check at the CLI layer.
    """
    # Minimal one-row tables config so load_tables/render succeed before the branch check.
    cfg = tmp_path / "tables.json"
    cfg.write_text('[{"synced_table_id": "cat.sch.tbl"}]')
    monkeypatch.setattr(mj, "render_migration_sql", lambda **kw: "SELECT 1;")
    monkeypatch.delenv("LAKEBASE_BRANCH", raising=False)

    with pytest.raises(SystemExit) as exc:
        mj.main(["--config", str(cfg), "--instance", "my-instance"])  # instance given, branch absent
    assert exc.value.code == 2, "missing --branch on a live apply must exit non-zero (parser.error)"


def test_host_from_endpoints_prefers_read_write():
    """The pure host picker returns the READ_WRITE endpoint's connection host (a migration must
    WRITE). Pinned against the REAL databricks-sdk shape (service/postgres.py, sdk >= 0.133):
      * Endpoint.status.endpoint_type  -> EndpointStatus.endpoint_type, an EndpointType enum
      * Endpoint.status.hosts.host     -> EndpointHosts.host
      * enum VALUES ENDPOINT_TYPE_READ_WRITE / ENDPOINT_TYPE_READ_ONLY (confirmed in the installed
        SDK) — NOT a fabricated 'READ_WRITE' short form.
    Hostile fixture: the READ_ONLY endpoint is listed FIRST with a different host, so a blind `[0]`
    pick grabs the read-only host, and the read-write marker sits behind an enum `.value` — so a
    picker that reads the WRONG attribute name degrades to first-pick and this goes RED.
    """
    class _EndpointType(enum.Enum):  # mirrors databricks.sdk.service.postgres.EndpointType
        ENDPOINT_TYPE_READ_ONLY = "ENDPOINT_TYPE_READ_ONLY"
        ENDPOINT_TYPE_READ_WRITE = "ENDPOINT_TYPE_READ_WRITE"

    def _ep(host: str, etype: _EndpointType):
        return types.SimpleNamespace(
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
    connects to the branch endpoint. Guards the threading half of mutation (b): if apply drops the
    branch before building the conninfo, this goes red.
    """
    captured: dict[str, object] = {}

    def fake_conninfo(instance_name, *, branch=None, **kw):
        captured["instance"] = instance_name
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

    mj.apply_sql_to_lakebase("SELECT 1;", instance_name="inst", branch="projects/p/branches/b")

    assert captured["branch"] == "projects/p/branches/b", "branch not threaded into the conninfo builder"
    assert captured["instance"] == "inst"
    assert captured["sql"] == "SELECT 1;"


def test_bundle_task_passes_branch_variable():
    """The deploy passes the target branch to the task via ${var.lakebase_branch}, mirroring how
    --instance / ${var.lakebase_instance} is passed — so the job migrates the TARGET branch endpoint,
    never the instance default. Guards a regression that drops the --branch param.
    """
    bundle = yaml.safe_load((mj.repo_root() / "dabs" / "databricks.yml").read_text())
    params = bundle["resources"]["jobs"]["lakebase_migration"]["tasks"][0]["spark_python_task"]["parameters"]
    assert "--branch" in params, f"task parameters missing --branch: {params}"
    assert params[params.index("--branch") + 1] == "${var.lakebase_branch}", (
        f"--branch is not the interpolated lakebase_branch var: {params}"
    )


# --- Seam F: the entrypoint wrapper must NOT raise SystemExit on the success path -------------
#
# See migration_job._run_cli for WHY a raised SystemExit(0) under a serverless spark_python_task is
# reported as a task FAILURE (the authoritative explanation of the mechanism lives there). These
# tests pin the wrapper's observable contract: on rc == 0 it returns normally (no SystemExit) so the
# task is marked succeeded; on a non-zero rc it raises SystemExit carrying that code so a real
# failure still surfaces. We test the WRAPPER, never the un-observable `if __name__ == "__main__"`.


def test_run_cli_success_does_not_raise_systemexit(monkeypatch):
    """SUCCESS path: when main() returns 0, the CLI wrapper returns normally and does NOT raise
    SystemExit — so the serverless spark_python_task (which reports ANY raised SystemExit, even
    code 0, as a task FAILURE) is marked SUCCEEDED.

    MUTATION GATE — revert the wrapper to always `sys.exit(rc)` (raise even on rc == 0) and this
    goes RED: sys.exit(0) raises SystemExit(0), so the `does not raise` assertion fails.
    """
    monkeypatch.setattr(mj, "main", lambda argv=None: 0)
    # Must complete without raising SystemExit — assert by simply calling it inside the test body.
    rc = mj._run_cli([])
    assert rc == 0, f"wrapper did not return the success code; got {rc!r}"


def test_run_cli_failure_raises_systemexit_with_code(monkeypatch):
    """FAILURE path: when main() returns a NON-ZERO code, the wrapper raises SystemExit carrying
    that exact code — a real failure still surfaces as a failure to the task runner.
    """
    monkeypatch.setattr(mj, "main", lambda argv=None: 3)
    with pytest.raises(SystemExit) as exc:
        mj._run_cli([])
    assert exc.value.code == 3, f"wrapper did not propagate the non-zero code; got {exc.value.code!r}"


def test_run_cli_threads_argv_into_main(monkeypatch):
    """The wrapper passes its argv through to main() unchanged (so CLI/local use is preserved),
    and still returns normally on the success code.
    """
    seen: dict[str, object] = {}

    def fake_main(argv=None):
        seen["argv"] = argv
        return 0

    monkeypatch.setattr(mj, "main", fake_main)
    mj._run_cli(["--dry-run", "--config", "x.json"])
    assert seen["argv"] == ["--dry-run", "--config", "x.json"], (
        f"wrapper did not thread argv into main: {seen.get('argv')!r}"
    )
