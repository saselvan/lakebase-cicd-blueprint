"""Falsifiability tests for ticket 03 — the Alembic-in-a-Workflow-job migration task.

Ticket 03 adds a bundle-declared Databricks Workflow job whose task, when run: (a) reads the
table list, (b) BLOCKS until each synced table reports ONLINE before any dependent DDL, then
(c) runs the EXISTING `alembic/` migration (reused, not copied) against Lakebase via a runtime
OAuth token. The live apply is ticket 04; here we prove the offline-unit seams.

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

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
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
    "live double-apply is ticket 04's gate",
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

    Guards mutation 2 / ticket checkbox 1: the job must reuse the entrypoint, so the migration has
    a single implementation on Databricks compute too.
    """
    bundle = yaml.safe_load((mj.repo_root() / "dabs" / "databricks.yml").read_text())
    jobs = (bundle.get("resources") or {}).get("jobs") or {}
    assert jobs, "no Workflow job declared in the bundle"
    blob = json.dumps(jobs)
    assert "migration_job.py" in blob, "job task does not run the migration_job.py entrypoint"


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
    `test_rendered_migration_double_apply_reconciles` (hermetic) and ticket 04's FEVM gate.
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
    exactly ticket 03 checkbox 3 ("re-running is a reconciling no-op") and ticket 04's replace-gate.
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

    # Simulate a synced-table replace wiping the reconciled objects (ticket 04's live scenario).
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
