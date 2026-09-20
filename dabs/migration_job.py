"""Migration Workflow-job task: wait-for-ONLINE gate + the shared Python DDL renderer.

This is the entrypoint the bundle-declared Databricks Workflow job runs (see the `jobs` resource
in dabs/databricks.yml). On a run it:

  1. reads the table list from the single source of truth (config/tables.json, ADR 0004),
  2. BLOCKS until every synced table reports ONLINE (the wait-for-ONLINE gate, ADR 0003) — so
     grants/indexes/view never run against a not-yet-loaded table,
  3. renders the idempotent reconciling DDL via the shared renderer (`dabs/render_ddl.py`) — the
     SAME "config in, idempotent SQL out" the Liquibase generator uses — and
  4. applies it to Lakebase using a RUNTIME OAuth token minted inside the workspace (no stored
     secret; consistent with the repo's no-secret posture).

Re-running is a reconciling no-op: the renderer emits a pg_roles-guarded CREATE ROLE, idempotent
GRANT, CREATE INDEX IF NOT EXISTS, and CREATE OR REPLACE VIEW (ADR 0002). There is NO Alembic and
NO `alembic_version` table — so there is no version bookkeeping to collide with, and nothing to
roll back on a 2nd apply. A synced-table replace therefore self-heals: every run re-applies the
object DDL cleanly. (An earlier Alembic-rendered variant rolled back on the 2nd apply once a second
revision existed, because its version-table INSERT tripped a duplicate-key on `alembic_version`;
the renderer removes that class of bug entirely.)

Design constraints that keep this OFFLINE-UNIT-TESTABLE (the live apply runs against a real
Lakebase branch):
  * The gate (`wait_for_online`) is a pure function over an injected `get_status` callable and an
    injected `sleep` — tests mock both; nothing touches a real workspace.
  * The render is a pure, stdlib-only function (no database, no external tools).
  * The Databricks SDK and psycopg are imported LAZILY inside the apply/status-source helpers, so
    the unit tests (gate, render, conninfo) import this module without those packages installed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Callable

# A synced table is ready once its detailed_state contains ONLINE; these substrings are terminal
# failures the gate must surface instead of polling forever.
ONLINE = "ONLINE"
_TERMINAL_FAILURES = ("FAILED", "ERROR")


# --- Single-source-of-truth locators (shared with Terraform + the DDL renderer) --------------

def _entrypoint_file() -> Path:
    """Locate THIS entrypoint file WITHOUT depending on ``__file__``.

    The bundle ``spark_python_task`` runs the entrypoint via ``exec(compile(src, filename,
    "exec"))`` into a namespace where ``__file__`` is NOT bound — a live serverless run therefore
    raised ``NameError: name '__file__' is not defined`` the moment ``repo_root()`` was reached
    (before the config was ever read). The unit suite only ever imported this module (where
    ``__file__`` IS bound), so the failure never surfaced offline.

    Resolution order, from most to least reliable:
      1. ``__file__`` when it is bound — local import, pytest, ``python migration_job.py``.
      2. ``sys.argv[0]`` — the path the runtime invoked; a ``spark_python_task`` sets it to the
         entrypoint. Guarded against the interactive/exec sentinels ('' and '-c').
      3. ``cwd``/migration_job.py — a coarse last resort so a call still returns a Path, not raise.
    """
    try:
        return Path(__file__)
    except NameError:
        argv0 = sys.argv[0] if sys.argv else ""
        if argv0 and not argv0.startswith("-"):
            return Path(argv0)
        return Path.cwd() / "migration_job.py"


def repo_root() -> Path:
    """Repo root — parent of the dabs/ package that holds this entrypoint.

    Derived via ``_entrypoint_file()`` so it never depends on ``__file__`` (the deployed bundle
    runs the task under ``exec`` where ``__file__`` is undefined). In BOTH the local checkout and
    the deployed bundle tree the entrypoint lives at ``<root>/dabs/migration_job.py`` with
    ``config/`` as a sibling of ``dabs/`` (verified against the deploy's sync manifest: files land
    under ``${workspace.file_path}/{dabs,config}``), so parent-of-dabs is the correct root in both.
    The live path ALSO passes ``--config`` explicitly (see ``main`` / ``databricks.yml``), so this
    is a robust fallback, not the sole locator.
    """
    return _entrypoint_file().resolve().parent.parent


def tables_config_path(config_path: str | Path | None = None) -> Path:
    """Path to config/tables.json: explicit arg, else LAKEBASE_TABLES_CONFIG (the same env var the
    standalone renderer honors), else the repo default — one source of truth for all."""
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


# --- Seam C: the shared Python DDL renderer (pure; applied at runtime) ----------------------

def _load_render_ddl():
    """Import the shared renderer module (``dabs/render_ddl.py``), robustly.

    Normal import works locally, under pytest, and via ``python migration_job.py``. On the
    serverless ``spark_python_task`` the entrypoint is run via ``exec(compile(...))`` where
    ``__file__`` is unbound and the repo root may not be on ``sys.path`` — so we fall back to
    loading the module BY PATH, derived from ``repo_root()`` (which does not depend on ``__file__``).
    ``render_ddl`` is stdlib-only, so importing it needs nothing beyond the standard library.
    """
    try:
        from dabs import render_ddl  # normal import when the package is importable
        return render_ddl
    except Exception:
        import importlib.util

        path = repo_root() / "dabs" / "render_ddl.py"
        spec = importlib.util.spec_from_file_location("dabs_render_ddl", path)
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)
        return module


def render_migration_sql(*, config_path: str | Path | None = None) -> str:
    """Render the idempotent reconciling DDL for the configured tables via the shared renderer.

    Delegates to ``dabs/render_ddl.py`` — the SAME "config in, idempotent SQL out" the Liquibase
    generator consumes. The result (what ``--dry-run`` prints AND what ``apply_sql_to_lakebase``
    executes) is a pg_roles-guarded CREATE ROLE + idempotent GRANT + CREATE INDEX IF NOT EXISTS +
    CREATE OR REPLACE VIEW, per table. There is NO Alembic and NO ``alembic_version`` table, so
    applying this SQL a second time — e.g. after a synced-table replace — is a clean reconciling
    no-op, never a version-bookkeeping rollback. Returns the SQL.
    """
    render = _load_render_ddl()
    path = config_path if config_path else tables_config_path()
    return render.render_ddl(render.load_tables(path))


# --- Runtime OAuth + apply (live path against a real Lakebase branch; SDK/psycopg imported lazily) --

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


def _host_from_endpoints(endpoints: object) -> str:
    """Pick the connection host from a branch's compute endpoints (pure, no SDK/network).

    A migration WRITES, so it must target the branch's READ_WRITE endpoint; a branch has exactly
    one. We therefore prefer the READ_WRITE endpoint's `status.hosts.host` and only fall back to the
    first endpoint that exposes a host — this is stricter than the proven-live `JSON[0]` pick in
    scripts/branch_test.sh (which is correct only because a fresh branch has a single endpoint), so
    it stays correct even if a read-only endpoint is listed first. Reads the SAME field the live
    script reads: `status.hosts.host`. Duck-typed so a mocked endpoint needs no SDK types.
    """
    eps = list(endpoints)

    def _host(ep: object) -> str | None:
        status = getattr(ep, "status", None)
        hosts = getattr(status, "hosts", None)
        return getattr(hosts, "host", None)

    def _is_read_write(ep: object) -> bool:
        etype = getattr(getattr(ep, "status", None), "endpoint_type", None)
        # endpoint_type is an enum on the SDK object; compare by string so a plain string works too.
        return "READ_WRITE" in str(getattr(etype, "value", etype) or "").upper()

    for ep in eps:
        if _is_read_write(ep) and _host(ep):
            return _host(ep)  # type: ignore[return-value]
    for ep in eps:
        if _host(ep):
            return _host(ep)  # type: ignore[return-value]
    raise RuntimeError("no compute endpoint with a connection host found for the branch")


def sdk_branch_host_source() -> Callable[[str], str]:
    """Return `resolve_host(branch)` -> connection host, backed by the Databricks Postgres SDK.

    Imported lazily so offline unit tests need no databricks-sdk. `branch` is the
    ``projects/<proj>/branches/<branch>`` value carried by ${var.lakebase_branch}. Lists that
    branch's compute endpoints and returns the READ_WRITE endpoint's host — the SDK equivalent of
    the proven-live CLI `databricks postgres list-endpoints <branch>` -> JSON[0].status.hosts.host.

    SDK-over-CLI (justified): the migration runs as a spark_python_task on SERVERLESS, where the
    `databricks` CLI binary is NOT guaranteed present, but databricks-sdk>=0.133 is a declared job
    dependency. The exact call is CONFIRMED against databricks-sdk (WorkspaceClient().postgres,
    class PostgresAPI.list_endpoints(parent=...) -> Iterator[Endpoint], Endpoint.status.hosts.host).
    """
    from databricks.sdk import WorkspaceClient  # lazy: not needed for offline unit tests

    workspace = WorkspaceClient()

    def resolve_host(branch: str) -> str:
        return _host_from_endpoints(workspace.postgres.list_endpoints(parent=branch))

    return resolve_host


def _sdk_credential_source(instance_name: str) -> tuple[str, str]:
    """Return (user, token) for a RUNTIME OAuth credential — no stored secret. Imported lazily."""
    from databricks.sdk import WorkspaceClient  # lazy

    workspace = WorkspaceClient()
    cred = workspace.database.generate_database_credential(
        request_id=str(time.time_ns()), instance_names=[instance_name]
    )
    user = workspace.current_user.me().user_name
    return user, cred.token


def _lakebase_conninfo(
    instance_name: str,
    *,
    branch: str,
    resolve_host: Callable[[str], str] | None = None,
    credential_source: Callable[[str], tuple[str, str]] | None = None,
) -> str:
    """Build a psycopg conninfo string for Lakebase using a RUNTIME OAuth credential — no stored
    secret. Only used on the live apply path.

    The host MUST be the TARGET BRANCH's endpoint, never the instance `read_write_dns` (which is
    the instance DEFAULT endpoint = the production branch). `branch`
    (``projects/<proj>/branches/<branch>``) is therefore REQUIRED and is resolved to a host via the
    branch's compute endpoint. A missing/empty branch RAISES rather than reconnecting to the
    instance default — that fallback was dead-but-dangerous machinery (no caller omits the branch;
    the bundle always passes ``${var.lakebase_branch}``) that would silently route a migration at
    the production branch, so it was removed along with the instance-DNS seam. This is the single
    authoritative explanation of why the branch is required; the callers below just enforce it.

    The host resolver and the credential source are INJECTABLE pure callables (mirroring
    get_status / sdk_status_source), defaulting to SDK-backed sources — so this is
    offline-unit-testable without databricks-sdk installed.
    """
    if not branch:
        raise ValueError(
            "a target branch (projects/<proj>/branches/<branch>) is required to build the "
            "Lakebase conninfo; refusing to fall back to the instance default endpoint "
            "(the production branch)"
        )
    resolve_host = resolve_host or sdk_branch_host_source()
    credential_source = credential_source or _sdk_credential_source

    host = resolve_host(branch)  # the TARGET branch endpoint — never the instance default
    user, token = credential_source(instance_name)
    return (
        f"host={host} port=5432 dbname={os.environ.get('PGDATABASE', 'databricks_postgres')} "
        f"user={user} password={token} sslmode=require"
    )


def apply_sql_to_lakebase(sql: str, *, instance_name: str, branch: str) -> None:
    """Apply rendered DDL to Lakebase over a runtime-OAuth psycopg connection (no stored secret).

    `branch` (``projects/<proj>/branches/<branch>``) is REQUIRED — it selects the TARGET branch
    endpoint host so the migration lands on that branch, not the instance default endpoint (the
    production branch); see _lakebase_conninfo for why. A missing/empty branch raises there.

    `sql` is the output of render_migration_sql (the shared Python renderer), which is RE-RUNNABLE
    by construction (guarded CREATE ROLE / idempotent GRANT / CREATE INDEX IF NOT EXISTS / CREATE
    OR REPLACE VIEW, and no version-tracking table), so executing this blob a second time — e.g.
    after a synced-table replace — is a clean reconciling no-op.

    Live path — exercised by the live apply against a real Lakebase branch, not by offline unit
    tests. psycopg is
    imported lazily so the unit suite imports this module without it installed.
    """
    import psycopg  # lazy: only the live apply needs a driver

    with psycopg.connect(_lakebase_conninfo(instance_name, branch=branch), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(sql)


def main(argv: list[str] | None = None) -> int:
    """Job-task entrypoint: read tables -> wait for ONLINE -> render idempotent DDL -> apply.

    The Lakebase instance name comes from --instance or LAKEBASE_INSTANCE_NAME (a job parameter set
    by the deploy, not a committed value). --dry-run stops after the render (no connection), which
    is what the offline gate exercises.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--config",
        default=None,
        help="Path to config/tables.json. The deploy passes the DEPLOYED location "
        "(${workspace.file_path}/config/tables.json) so runtime config resolution is explicit "
        "and never depends on __file__. Default: LAKEBASE_TABLES_CONFIG env / repo default.",
    )
    parser.add_argument(
        "--instance",
        default=os.environ.get("LAKEBASE_INSTANCE_NAME"),
        help="Lakebase database instance name (default: LAKEBASE_INSTANCE_NAME).",
    )
    parser.add_argument(
        "--branch",
        default=os.environ.get("LAKEBASE_BRANCH"),
        help="Target Lakebase branch (projects/<proj>/branches/<branch>) whose compute-endpoint "
        "host the migration connects to. The deploy passes ${var.lakebase_branch}. REQUIRED for a "
        "live apply (like --instance): without it the job would connect to the instance DEFAULT "
        "endpoint = the production branch, so it is refused rather than defaulted. "
        "Default: LAKEBASE_BRANCH env.",
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
        print("dry-run: rendered idempotent reconciling DDL; skipping wait-gate + apply.")
        print(sql)
        return 0

    if not args.instance:
        parser.error("--instance / LAKEBASE_INSTANCE_NAME is required for a live apply")
    if not args.branch:
        parser.error(
            "--branch / LAKEBASE_BRANCH is required for a live apply — refusing to fall back to "
            "the instance DEFAULT endpoint (the production branch)"
        )

    get_status = sdk_status_source(args.instance)
    run_migration_task(
        table_ids,
        get_status,
        lambda: apply_sql_to_lakebase(sql, instance_name=args.instance, branch=args.branch),
        poll_interval=args.poll_interval,
        timeout=args.timeout,
    )
    print("migration applied: all synced tables ONLINE, grants/indexes/view reconciled.")
    return 0


def _run_cli(argv: list[str] | None = None) -> int:
    """Entrypoint wrapper: run main() and raise SystemExit ONLY on a non-zero return code.

    WHY: a bundle spark_python_task runs this file via exec(compile(src, ..., "exec")), and the
    serverless runtime catches ANY raised SystemExit — even code 0 — and reports the task as a
    FAILURE. A plain `sys.exit(main())` therefore turns a fully successful migration
    (schema/view/role/grants all applied) into a FAILED job run (`INTERNAL_ERROR / SystemExit: 0`),
    which would fail the CI pipeline that runs it. So on the SUCCESS path (rc == 0) we
    return normally WITHOUT raising, and the task is marked succeeded. A real failure (non-zero rc)
    still raises SystemExit(rc); and any uncaught exception under spark_python_task is itself a
    failure — the correct signal. main()'s return-int contract and its argparse validation are
    unchanged, so CLI/local `python migration_job.py` behaves as before.
    """
    rc = main(argv)
    if rc != 0:
        raise SystemExit(rc)
    return rc


if __name__ == "__main__":
    _run_cli()
