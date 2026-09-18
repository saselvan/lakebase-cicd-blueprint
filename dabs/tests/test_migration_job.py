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

import importlib.util
import json
import re
from pathlib import Path

import pytest
import yaml

from dabs import migration_job as mj

REPO_ROOT = Path(__file__).resolve().parents[2]
# Reuse the SHARED alembic hostile fixture (alpha: 1 index col; beta: 0 index cols) — no dup fixture.
ALEMBIC_FIXTURE = REPO_ROOT / "alembic" / "tests" / "fixtures" / "tables.json"
_HAS_ALEMBIC = importlib.util.find_spec("alembic") is not None
_needs_alembic = pytest.mark.skipif(
    not _HAS_ALEMBIC,
    reason="alembic not installed; the render seam shells out to it (the CI 'alembic' job has it)",
)


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
    in the repo, and the task's shared-alembic locator points at that single directory.

    Guards mutation 2: point the task at a COPIED migration => this goes red (a second migration
    file appears, or the locator stops pointing at alembic/).
    """
    root = mj.repo_root()
    found = [
        p
        for p in root.glob("**/versions/0001_app_role.py")
        if ".venv" not in p.parts and "site-packages" not in p.parts
    ]
    assert found == [root / "alembic" / "versions" / "0001_app_role.py"], (
        f"expected exactly one shared migration implementation; found {found}"
    )
    assert mj.shared_alembic_dir() == root / "alembic", (
        f"task's shared-alembic locator does not point at alembic/: {mj.shared_alembic_dir()}"
    )
    # No copied alembic env / migration under dabs/ (a self-contained fork would live here).
    dabs_env_copies = list((root / "dabs").rglob("env.py"))
    assert dabs_env_copies == [], f"copied alembic env under dabs/: {dabs_env_copies}"


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

@_needs_alembic
def test_invoked_migration_is_idempotent_reconciling():
    """The SQL the job task invokes is written in reconciling forms — re-running is a safe no-op:
    guarded role creation, CREATE INDEX IF NOT EXISTS, CREATE OR REPLACE VIEW — and two renders are
    byte-identical (no per-run state).

    Guards mutation 3: break idempotency in the invoked path (drop IF NOT EXISTS / OR REPLACE) =>
    this goes red.
    """
    sql = mj.render_migration_sql(config_path=ALEMBIC_FIXTURE)
    n = _norm(sql)
    assert "CREATE INDEX IF NOT EXISTS" in n, f"index DDL is not re-runnable (no IF NOT EXISTS):\n{n}"
    assert "CREATE OR REPLACE VIEW" in n, f"view DDL is not re-runnable (no OR REPLACE):\n{n}"
    assert "pg_roles" in n, f"role creation is not guarded by a pg_roles existence check:\n{n}"
    assert mj.render_migration_sql(config_path=ALEMBIC_FIXTURE) == sql, (
        "two renders differ — the invoked migration carries per-run state (not a clean no-op)"
    )
