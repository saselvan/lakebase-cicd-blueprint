"""Migration Workflow-job task: wait-for-ONLINE gate + reuse of the shared Alembic migration.

This is the entrypoint the bundle-declared Databricks Workflow job runs (see the `jobs` resource
in dabs/databricks.yml). On a run it:

  1. reads the table list from the single source of truth (config/tables.json, ADR 0004),
  2. BLOCKS until every synced table reports ONLINE (the wait-for-ONLINE gate, ADR 0003) — so
     grants/indexes/view never run against a not-yet-loaded table,
  3. renders the EXISTING alembic/ migration to idempotent DDL (`alembic upgrade head --sql`) —
     reused, never copied — and
  4. applies it to Lakebase using a RUNTIME OAuth token minted inside the workspace (no stored
     secret; consistent with the repo's no-secret posture).

Re-running is a reconciling no-op: the shared migration emits guarded CREATE ROLE, CREATE INDEX
IF NOT EXISTS, and CREATE OR REPLACE VIEW (ADR 0002), so a synced-table replace self-heals.

Design constraints that keep this OFFLINE-UNIT-TESTABLE (the live apply is ticket 04):
  * The gate (`wait_for_online`) is a pure function over an injected `get_status` callable and an
    injected `sleep` — tests mock both; nothing touches a real workspace.
  * The Databricks SDK and psycopg are imported LAZILY inside the apply/status-source helpers, so
    the unit tests (gate, single-implementation, idempotency-render) import this module without
    those packages installed.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

# A synced table is ready once its detailed_state contains ONLINE; these substrings are terminal
# failures the gate must surface instead of polling forever.
ONLINE = "ONLINE"
_TERMINAL_FAILURES = ("FAILED", "ERROR")


# --- Single-source-of-truth locators (shared with Terraform + the Alembic migration) --------

def repo_root() -> Path:
    """Repo root — parent of the dabs/ package that holds this entrypoint."""
    return Path(__file__).resolve().parent.parent


def shared_alembic_dir() -> Path:
    """The ONE alembic/ migration directory. The task reuses it; it is never copied/forked."""
    return repo_root() / "alembic"


def tables_config_path(config_path: str | Path | None = None) -> Path:
    """Path to config/tables.json: explicit arg, else LAKEBASE_TABLES_CONFIG (same env var the
    Alembic env.py reads), else the repo default — one source of truth for both."""
    if config_path:
        return Path(config_path)
    override = os.environ.get("LAKEBASE_TABLES_CONFIG")
    if override:
        return Path(override)
    return repo_root() / "config" / "tables.json"


def load_tables(config_path: str | Path | None = None) -> list[dict]:
    """Read the single-source-of-truth tables config as a list of dicts."""
    with tables_config_path(config_path).open() as fh:
        return json.load(fh)


def synced_table_ids(tables: list[dict]) -> list[str]:
    """The 3-part synced-table ids (catalog.schema.table) the gate polls for ONLINE."""
    return [t["synced_table_id"] for t in tables]


# --- Seam A: the wait-for-ONLINE gate (pure / mockable) -------------------------------------

def wait_for_online(
    table_ids: list[str],
    get_status: Callable[[str], str],
    *,
    sleep: Callable[[float], None] = time.sleep,
    poll_interval: float = 10,
    timeout: float = 1800,
) -> dict[str, str]:
    """Block until EVERY table id reports a status containing ONLINE, then return
    {table_id: final_status}. Polls `get_status(table_id)` on a `poll_interval`, sleeping between
    rounds. Raises RuntimeError on a terminal FAILED/ERROR status and TimeoutError past `timeout`.

    This is the contract that must run BEFORE any dependent DDL (ADR 0003): dependent DDL running
    against a still-PROVISIONING table is exactly the failure this prevents.
    """
    pending = list(table_ids)
    final: dict[str, str] = {}
    elapsed: float = 0
    while pending:
        still_pending: list[str] = []
        for tid in pending:
            status = get_status(tid)
            upper = str(status).upper()
            if any(marker in upper for marker in _TERMINAL_FAILURES):
                raise RuntimeError(f"synced table {tid} reached a terminal failure state: {status}")
            if ONLINE in upper:
                final[tid] = status
            else:
                still_pending.append(tid)
        pending = still_pending
        if not pending:
            break
        if elapsed >= timeout:
            raise TimeoutError(f"timed out after {timeout}s waiting for ONLINE; still pending: {pending}")
        sleep(poll_interval)
        elapsed += poll_interval
    return final


def run_migration_task(
    table_ids: list[str],
    get_status: Callable[[str], str],
    apply_migration: Callable[[], object],
    *,
    sleep: Callable[[float], None] = time.sleep,
    poll_interval: float = 10,
    timeout: float = 1800,
) -> object:
    """Orchestrate the task: GATE FIRST (block until all ONLINE), THEN apply the migration.
    `apply_migration` is a zero-arg callable so the DB apply stays injectable/mockable — the gate
    guarantees it is never invoked before every table is ONLINE."""
    wait_for_online(table_ids, get_status, sleep=sleep, poll_interval=poll_interval, timeout=timeout)
    return apply_migration()


# --- Seam C: reuse of the shared Alembic migration (rendered offline, applied at runtime) ---

def render_migration_sql(
    *,
    alembic_dir: str | Path | None = None,
    config_path: str | Path | None = None,
) -> str:
    """Render the SHARED alembic migration to idempotent DDL via `alembic upgrade head --sql`.

    Reuses the repo's alembic/ (no copy): shells out to `python -m alembic` in that directory, with
    LAKEBASE_TABLES_CONFIG pointed at the same tables config. The emitted SQL is guarded/reconciling
    (IF NOT EXISTS / OR REPLACE / role guard), so applying it twice is a safe no-op. Returns the SQL.
    """
    adir = Path(alembic_dir) if alembic_dir else shared_alembic_dir()
    ini = adir / "alembic.ini"
    env = dict(os.environ)
    if config_path:
        env["LAKEBASE_TABLES_CONFIG"] = str(config_path)
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(ini), "upgrade", "head", "--sql"],
        cwd=str(adir),
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"shared alembic render failed (exit {result.returncode}) in {adir}:\n{result.stderr}"
        )
    return result.stdout


# --- Runtime OAuth + apply (live path, exercised by ticket 04; SDK/psycopg imported lazily) --

def sdk_status_source(instance_name: str) -> Callable[[str], str]:
    """Return a `get_status(table_id)` backed by the Databricks SDK — the live wait-gate source.

    Imported lazily so unit tests need no databricks-sdk. Reports the synced table's
    `status.detailed_state` (the field scripts/wait_for_sync.sh polls), defaulting to "UNKNOWN".
    """
    from databricks.sdk import WorkspaceClient  # lazy: not needed for offline unit tests

    workspace = WorkspaceClient()

    def get_status(table_id: str) -> str:
        table = workspace.database.get_synced_database_table(name=table_id)
        status = getattr(table, "data_synchronization_status", None) or getattr(table, "status", None)
        detailed = getattr(status, "detailed_state", None)
        return str(detailed) if detailed is not None else "UNKNOWN"

    return get_status


def _lakebase_conninfo(instance_name: str) -> str:
    """Build a psycopg conninfo string for Lakebase using a RUNTIME OAuth credential — no stored
    secret. Imported lazily; only used on the live apply path (ticket 04)."""
    from databricks.sdk import WorkspaceClient  # lazy

    workspace = WorkspaceClient()
    instance = workspace.database.get_database_instance(name=instance_name)
    cred = workspace.database.generate_database_credential(
        request_id=str(time.time_ns()), instance_names=[instance_name]
    )
    user = workspace.current_user.me().user_name
    host = instance.read_write_dns
    return (
        f"host={host} port=5432 dbname={os.environ.get('PGDATABASE', 'databricks_postgres')} "
        f"user={user} password={cred.token} sslmode=require"
    )


def apply_sql_to_lakebase(sql: str, *, instance_name: str) -> None:
    """Apply rendered DDL to Lakebase over a runtime-OAuth psycopg connection (no stored secret).

    Live path — exercised by ticket 04's FEVM acceptance, not by offline unit tests. psycopg is
    imported lazily so the unit suite imports this module without it installed.
    """
    import psycopg  # lazy: only the live apply needs a driver

    with psycopg.connect(_lakebase_conninfo(instance_name), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(sql)


def main(argv: list[str] | None = None) -> int:
    """Job-task entrypoint: read tables -> wait for ONLINE -> render shared migration -> apply.

    The Lakebase instance name comes from --instance or LAKEBASE_INSTANCE_NAME (a job parameter set
    by the deploy, not a committed value). --dry-run stops after the render (no connection), which
    is what the offline gate exercises.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", default=None, help="Path to config/tables.json (default: repo/env).")
    parser.add_argument(
        "--instance",
        default=os.environ.get("LAKEBASE_INSTANCE_NAME"),
        help="Lakebase database instance name (default: LAKEBASE_INSTANCE_NAME).",
    )
    parser.add_argument("--timeout", type=float, default=1800, help="Wait-for-ONLINE timeout (s).")
    parser.add_argument("--poll-interval", type=float, default=10, help="Wait-for-ONLINE poll interval (s).")
    parser.add_argument("--dry-run", action="store_true", help="Render only; do not connect/apply (offline).")
    args = parser.parse_args(argv)

    tables = load_tables(args.config)
    table_ids = synced_table_ids(tables)
    print(f"migration job: {len(table_ids)} synced table(s) to gate on ONLINE: {table_ids}")

    sql = render_migration_sql(config_path=args.config)

    if args.dry_run:
        print("dry-run: rendered shared alembic migration; skipping wait-gate + apply.")
        print(sql)
        return 0

    if not args.instance:
        parser.error("--instance / LAKEBASE_INSTANCE_NAME is required for a live apply")

    get_status = sdk_status_source(args.instance)
    run_migration_task(
        table_ids,
        get_status,
        lambda: apply_sql_to_lakebase(sql, instance_name=args.instance),
        poll_interval=args.poll_interval,
        timeout=args.timeout,
    )
    print("migration applied: all synced tables ONLINE, grants/indexes/view reconciled.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
