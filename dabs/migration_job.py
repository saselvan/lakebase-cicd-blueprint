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

def sdk_status_source() -> Callable[[str], str]:
    """Return a `get_status(table_id)` backed by the Databricks SDK — the live wait-gate source.

    Reads the synced table's status from the WORKING postgres synced-tables REST endpoint,
    ``GET /api/2.0/postgres/synced_tables/{synced_table_id}`` (the 3-part catalog.schema.table id),
    and returns its ``status.detailed_state`` (which carries the ONLINE / FAILED substrings the gate
    matches on), defaulting to "UNKNOWN" when the field is absent.

    WHY the raw REST call and not the SDK method: ``workspace.postgres.get_synced_table(name=...)``
    maps at RUNTIME to ``GET /postgres/{id}`` — a path the workspace has NO API for (a live
    serverless run raised ``NotFound: No API found for 'GET /postgres/<id>'``), and because the
    wait-gate runs BEFORE the apply the whole job died there. The path used below is the one verified
    live (``databricks api get /api/2.0/postgres/synced_tables/<id>``). We stay on the postgres
    surface (consistent with the branch-endpoint + credential path) via the SDK's generic REST
    client: ``workspace.api_client.do("GET", ...)`` returns a dict (confirmed against
    databricks-sdk 0.140: ``ApiClient.do(method, path=None, ...) -> Union[dict, list, BinaryIO]``).

    Imported lazily so offline unit tests need no databricks-sdk. The real call is verified LIVE.
    """
    from databricks.sdk import WorkspaceClient  # lazy: not needed for offline unit tests

    workspace = WorkspaceClient()

    def get_status(table_id: str) -> str:
        resp = workspace.api_client.do("GET", f"/api/2.0/postgres/synced_tables/{table_id}")
        status = resp.get("status") if isinstance(resp, dict) else None
        detailed = status.get("detailed_state") if isinstance(status, dict) else None
        return detailed if detailed is not None else "UNKNOWN"

    return get_status


def _select_endpoint(endpoints: object) -> object:
    """Pick a branch's READ_WRITE compute endpoint OBJECT (pure, no SDK/network).

    A migration WRITES, so it must target the branch's READ_WRITE endpoint; a branch has exactly
    one. We prefer the READ_WRITE endpoint (identified by its `status.endpoint_type`) that exposes a
    `status.hosts.host`, and only fall back to the first endpoint with a host — stricter than the
    proven-live `JSON[0]` pick in scripts/branch_test.sh (correct only because a fresh branch has a
    single endpoint), so it stays correct even if a read-only endpoint is listed first. Returns the
    endpoint object so callers can derive BOTH its host (for the conninfo) and its resource name
    (for credential minting) from the SAME chosen endpoint. Duck-typed so a mock needs no SDK types.
    """
    eps = list(endpoints)

    def _has_host(ep: object) -> bool:
        return bool(_endpoint_host_or_none(ep))

    def _is_read_write(ep: object) -> bool:
        etype = getattr(getattr(ep, "status", None), "endpoint_type", None)
        # endpoint_type is an enum on the SDK object; compare by string so a plain string works too.
        return "READ_WRITE" in str(getattr(etype, "value", etype) or "").upper()

    for ep in eps:
        if _is_read_write(ep) and _has_host(ep):
            return ep
    for ep in eps:
        if _has_host(ep):
            return ep
    raise RuntimeError("no compute endpoint with a connection host found for the branch")


def _endpoint_host_or_none(ep: object) -> str | None:
    """The endpoint's connection host (`status.hosts.host`) or None — the SAME field the proven-live
    script reads. Duck-typed."""
    status = getattr(ep, "status", None)
    hosts = getattr(status, "hosts", None)
    return getattr(hosts, "host", None)


def _endpoint_host(ep: object) -> str:
    """The chosen endpoint's connection host, or raise if it has none."""
    host = _endpoint_host_or_none(ep)
    if not host:
        raise RuntimeError("resolved compute endpoint exposes no connection host")
    return host


def _endpoint_name(ep: object) -> str:
    """The chosen endpoint's RESOURCE NAME (projects/<p>/branches/<b>/endpoints/<e>) — the value
    `generate_database_credential(endpoint=...)` mints against. Raise if absent."""
    name = getattr(ep, "name", None)
    if not name:
        raise RuntimeError(
            "resolved compute endpoint has no resource name to mint a database credential from"
        )
    return name


def _host_from_endpoints(endpoints: object) -> str:
    """Connection host of a branch's READ_WRITE compute endpoint (pure). Thin wrapper over the
    shared `_select_endpoint` selector so host resolution and credential minting agree on the SAME
    endpoint. Retained as a named seam for the pure host-selection test."""
    return _endpoint_host(_select_endpoint(endpoints))


def sdk_branch_endpoint_source() -> Callable[[str], object]:
    """Return `resolve_endpoint(branch)` -> the branch's READ_WRITE compute Endpoint, backed by the
    Databricks Postgres (projects/Autoscaling) SDK surface.

    Imported lazily so offline unit tests need no databricks-sdk. `branch` is the
    ``projects/<proj>/branches/<branch>`` value carried by ${var.lakebase_branch}. Lists that
    branch's compute endpoints and returns the selected READ_WRITE endpoint — from which the caller
    derives both the connection host and the endpoint resource name to mint the credential against.
    The SDK equivalent of the proven-live CLI `databricks postgres list-endpoints <branch>`.

    SDK-over-CLI (justified): the migration runs as a spark_python_task on SERVERLESS, where the
    `databricks` CLI binary is NOT guaranteed present, but databricks-sdk>=0.133 is a declared job
    dependency. The exact call is CONFIRMED against databricks-sdk 0.140 (WorkspaceClient().postgres,
    class PostgresAPI.list_endpoints(parent=...) -> Iterator[Endpoint], Endpoint.name /
    Endpoint.status.hosts.host).
    """
    from databricks.sdk import WorkspaceClient  # lazy: not needed for offline unit tests

    workspace = WorkspaceClient()

    def resolve_endpoint(branch: str) -> object:
        return _select_endpoint(workspace.postgres.list_endpoints(parent=branch))

    return resolve_endpoint


def _sdk_credential_source(endpoint: str) -> tuple[str, str]:
    """Return (user, token) for a RUNTIME OAuth credential minted from a branch ENDPOINT — no stored
    secret, no database-instance name. Imported lazily.

    Uses the projects (Autoscaling) surface:
    ``workspace.postgres.generate_database_credential(endpoint=projects/<p>/branches/<b>/endpoints/<e>)``
    -> DatabaseCredential.token (a workspace-scoped token). CONFIRMED against databricks-sdk 0.140.
    """
    from databricks.sdk import WorkspaceClient  # lazy

    workspace = WorkspaceClient()
    cred = workspace.postgres.generate_database_credential(endpoint=endpoint)
    user = workspace.current_user.me().user_name
    return user, cred.token


def _lakebase_conninfo(
    *,
    branch: str,
    resolve_endpoint: Callable[[str], object] | None = None,
    credential_source: Callable[[str], tuple[str, str]] | None = None,
) -> str:
    """Build a psycopg conninfo string for Lakebase using a RUNTIME OAuth credential — no stored
    secret and no database-instance name. Only used on the live apply path.

    The connection MUST target the TARGET BRANCH's compute endpoint, never the instance default
    endpoint (= the production branch). `branch` (``projects/<proj>/branches/<branch>``) is therefore
    REQUIRED. It is resolved to the branch's READ_WRITE endpoint, and BOTH the connection host and
    the credential-minting endpoint name are derived from that SAME endpoint (projects API). A
    missing/empty branch RAISES rather than reconnecting to the instance default — that fallback was
    dead-but-dangerous machinery (no caller omits the branch; the bundle always passes
    ``${var.lakebase_branch}``) that would silently route a migration at the production branch.

    No instance name is threaded anywhere: the projects/Autoscaling surface mints the credential
    from the endpoint (``generate_database_credential(endpoint=...)``), so the misnamed instance var
    is gone entirely. The endpoint resolver and the credential source are INJECTABLE pure callables
    (mirroring get_status / sdk_status_source), defaulting to SDK-backed sources — so this is
    offline-unit-testable without databricks-sdk installed.
    """
    if not branch:
        raise ValueError(
            "a target branch (projects/<proj>/branches/<branch>) is required to build the "
            "Lakebase conninfo; refusing to fall back to the instance default endpoint "
            "(the production branch)"
        )
    resolve_endpoint = resolve_endpoint or sdk_branch_endpoint_source()
    credential_source = credential_source or _sdk_credential_source

    endpoint = resolve_endpoint(branch)  # the TARGET branch READ_WRITE endpoint
    host = _endpoint_host(endpoint)  # never the instance default
    user, token = credential_source(_endpoint_name(endpoint))  # minted FROM that endpoint
    return (
        f"host={host} port=5432 dbname={os.environ.get('PGDATABASE', 'databricks_postgres')} "
        f"user={user} password={token} sslmode=require"
    )


def apply_sql_to_lakebase(sql: str, *, branch: str) -> None:
    """Apply rendered DDL to Lakebase over a runtime-OAuth psycopg connection (no stored secret).

    `branch` (``projects/<proj>/branches/<branch>``) is REQUIRED — it selects the TARGET branch
    READ_WRITE endpoint (host + credential) so the migration lands on that branch, not the instance
    default endpoint (the production branch); see _lakebase_conninfo for why. A missing/empty branch
    raises there. No instance name is needed on this path.

    `sql` is the output of render_migration_sql (the shared Python renderer), which is RE-RUNNABLE
    by construction (guarded CREATE ROLE / idempotent GRANT / CREATE INDEX IF NOT EXISTS / CREATE
    OR REPLACE VIEW, and no version-tracking table), so executing this blob a second time — e.g.
    after a synced-table replace — is a clean reconciling no-op.

    Live path — exercised by the live apply against a real Lakebase branch, not by offline unit
    tests. psycopg is
    imported lazily so the unit suite imports this module without it installed.
    """
    import psycopg  # lazy: only the live apply needs a driver

    with psycopg.connect(_lakebase_conninfo(branch=branch), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(sql)


def main(argv: list[str] | None = None) -> int:
    """Job-task entrypoint: read tables -> wait for ONLINE -> render idempotent DDL -> apply.

    The live apply standardizes on the projects (Autoscaling) API: the wait-gate status, the branch
    endpoint, and the runtime OAuth credential all come from ``workspace.postgres.*``, keyed off the
    target ``--branch`` (``projects/<proj>/branches/<branch>``, a deploy-set job parameter). There is
    NO database-instance name anywhere — the credential is minted from the branch's compute endpoint.
    --dry-run stops after the render (no connection), which is what the offline gate exercises.
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
        "--branch",
        default=os.environ.get("LAKEBASE_BRANCH"),
        help="Target Lakebase branch (projects/<proj>/branches/<branch>) whose READ_WRITE "
        "compute endpoint supplies the connection host AND the runtime OAuth credential. The deploy "
        "passes ${var.lakebase_branch}. REQUIRED for a live apply: without it the job would connect "
        "to the instance DEFAULT endpoint = the production branch, so it is refused rather than "
        "defaulted. Default: LAKEBASE_BRANCH env.",
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

    if not args.branch:
        parser.error(
            "--branch / LAKEBASE_BRANCH is required for a live apply — refusing to fall back to "
            "the instance DEFAULT endpoint (the production branch)"
        )

    get_status = sdk_status_source()
    run_migration_task(
        table_ids,
        get_status,
        lambda: apply_sql_to_lakebase(sql, branch=args.branch),
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
