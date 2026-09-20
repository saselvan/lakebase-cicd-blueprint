"""Single source of the idempotent reconciling DDL — the DABs migration renderer (fix B).

`config/tables.json` in, idempotent SQL out. For every configured table this emits the SAME four
object groups the Liquibase changelog does — a guarded app role, explicit grants, one index per
`index_columns` entry, and a no-superuser consumer view — as plain, re-runnable Postgres DDL.

This module is the ONE home for the SQL-building helpers. Both migration paths consume it:

  * the DABs Workflow-job entrypoint (`dabs/migration_job.py`) renders through `render_ddl()`
    instead of shelling out to `alembic upgrade head --sql`, and
  * the Liquibase generator (`liquibase/generate_changelogs.py`) imports the same four helpers
    (`role_guard_sql` / `grant_statements` / `index_statements` / `view_statements`) and
    `validate_identifier`, so "config in, idempotent SQL out" is structurally identical for both.

It is also runnable standalone:

    python -m dabs.render_ddl [--config config/tables.json] | psql "$LAKEBASE_CONNINFO"

which REPLACES the old (broken) `alembic upgrade head --sql | psql` path.

Why there is no version table. The earlier DABs reconcile rendered from Alembic, which prepends an
UNGUARDED `alembic_version` create + INSERT and wraps everything in one transaction. With a 2nd
Alembic revision the reconcile's regex bookkeeping-patch produced a duplicate `alembic_version`
row on the 2nd apply → `duplicate key value violates unique constraint "alembic_version_pkc"` → the
whole transaction rolled back and the object DDL never reconciled. This renderer emits NO version
bookkeeping at all: every statement is idempotent (guarded CREATE ROLE, idempotent GRANT,
CREATE INDEX IF NOT EXISTS, CREATE OR REPLACE VIEW), so re-running is a clean reconciling no-op —
which is exactly what a synced-table replace needs. There is no revision state to collide.

stdlib-only and dependency-free, so the serverless job entrypoint can import it with nothing beyond
the standard library, and the offline unit suite runs it with no database and no alembic.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

_HEADER = (
    "-- GENERATED idempotent reconciling DDL for the Lakebase CI/CD reference (DABs path).\n"
    "-- Source of truth: config/tables.json. Emitted by dabs/render_ddl.py — DO NOT EDIT BY HAND.\n"
    "-- Re-runnable by construction: a pg_roles-guarded CREATE ROLE, idempotent GRANT,\n"
    "-- CREATE INDEX IF NOT EXISTS, and CREATE OR REPLACE VIEW. There is NO migration version\n"
    "-- table — the whole point is to reconcile the object layer on every deploy (a synced-table\n"
    "-- replace drops grants/indexes/view; re-applying this DDL restores them).\n"
)


# --- config loading -----------------------------------------------------------------------------

def load_tables(config_path: str | Path) -> list[dict]:
    """Read the single-source-of-truth tables config as a list of dicts."""
    with Path(config_path).open() as fh:
        tables = json.load(fh)
    if not isinstance(tables, list):
        raise ValueError(f"{config_path}: expected a JSON array of table objects")
    return tables


# --- identifier validation (the SAME seam the Liquibase path uses) ------------------------------
#
# Every identifier baked into the emitted DDL (app_role, app_schema, the pg table name, and each
# index column) is interpolated straight into SQL text, so an unsafe value — a hyphen, a space, a
# quote, a reserved word — would either break the SQL or be an injection vector. This single seam
# (imported by liquibase/generate_changelogs.py too) is where BOTH paths reject unsafe identifiers
# at generation time.
#
# The rule is strict-reject, not quote. Config identifiers in this reference are lowercase
# snake_case; a generated PUBLIC teaching reference should fail LOUD on a weird identifier rather
# than silently double-quote arbitrary input (quoting would have to be threaded through every
# emitted statement and every downstream consumer, and case-folding surprises are exactly the kind
# of footgun a reference should not model). If a future need arises, quoting can be added behind
# this one function.

_SAFE_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")
_MAX_IDENTIFIER_LEN = 63  # Postgres NAMEDATALEN - 1; longer names are silently truncated by PG.

# Curated set of Postgres reserved words rejected even when they match the safe-identifier regex.
# Not exhaustive — a strict, predictable guard, not a full parser. An identifier that is also a
# reserved word (an app_role named `user`, a column named `order`) would need double-quoting
# everywhere it appears in the emitted DDL; we fail loud at generation instead.
_RESERVED_WORDS = frozenset({
    "all", "analyse", "analyze", "and", "any", "array", "as", "asc", "authorization",
    "between", "both", "by", "case", "cast", "check", "collate", "column", "constraint",
    "create", "cross", "current_catalog", "current_date", "current_role", "current_schema",
    "current_time", "current_timestamp", "current_user", "database", "default", "deferrable",
    "delete", "desc", "distinct", "do", "drop", "else", "end", "except", "exists", "false",
    "fetch", "for", "foreign", "from", "full", "grant", "group", "having", "in", "index",
    "inner", "insert", "intersect", "into", "is", "join", "key", "leading", "left", "like",
    "limit", "localtime", "localtimestamp", "natural", "not", "null", "offset", "on", "only",
    "or", "order", "outer", "primary", "references", "returning", "revoke", "right", "role",
    "schema", "select", "session_user", "similar", "some", "table", "then", "to", "trailing",
    "true", "union", "unique", "update", "user", "using", "values", "view", "when", "where",
    "with",
})


def validate_identifier(name, kind: str = "identifier") -> str:
    """Single identifier-validation home for BOTH migration paths (fix D).

    Accepts ONLY a safe unquoted Postgres identifier: a non-empty string matching
    ``^[a-z_][a-z0-9_]*`` of at most 63 characters, and NOT a reserved SQL word. Every identifier
    baked into the rendered DDL (app_role, app_schema, the pg table name, and each index column)
    passes through here, and so does the Liquibase generator (it imports this function), so a
    malformed / hostile `config/tables.json` fails loudly at generation time in BOTH paths instead
    of emitting broken or injectable DDL.

    On rejection the error names the offending value, the `kind`, and the rule it broke (nothing
    sensitive — an identifier is a table/role/column name). Returns the identifier unchanged when
    valid, so callers can use it inline.
    """
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"{kind} must be a non-empty string, got {name!r}")
    if len(name) > _MAX_IDENTIFIER_LEN:
        raise ValueError(
            f"{kind} {name!r} is {len(name)} characters; exceeds the Postgres identifier "
            f"limit of {_MAX_IDENTIFIER_LEN}. Use a shorter lowercase snake_case name."
        )
    if not _SAFE_IDENTIFIER.match(name):
        raise ValueError(
            f"{kind} {name!r} is not a safe unquoted Postgres identifier: expected lowercase "
            f"snake_case matching ^[a-z_][a-z0-9_]* (no hyphens, spaces, quotes, dots, or "
            f"uppercase). Rename it in config/tables.json."
        )
    if name in _RESERVED_WORDS:
        raise ValueError(
            f"{kind} {name!r} is a reserved SQL word and cannot be used as an unquoted "
            f"identifier. Rename it in config/tables.json."
        )
    return name


def validate_derived_name(name: str, kind: str, source_hint: str = "") -> str:
    """Length-check a name DERIVED from already-validated parts (fix R2).

    ``validate_identifier`` caps each INPUT part (app_role, app_schema, pg-table name, index column)
    at 63 chars, but the builders EMIT derived names — the index name ``idx_<tbl>_<col>`` and the
    consumer view name ``<tbl>_v`` — whose concatenation can exceed 63 even when every input part is
    individually legal. Postgres would then silently TRUNCATE the name (a NOTICE; the object is still
    created), and the later verify step / ``verify_table``, which looks for the UN-truncated name,
    would FAIL a deploy that "succeeded". So we reject an over-limit derived name at generation time.

    The derived name is a concatenation of parts that already passed ``validate_identifier``, so it
    is guaranteed to match the safe-identifier shape; only the length can newly violate the rule —
    hence a length-only check with a message that points at the source part to shorten.
    """
    if len(name) > _MAX_IDENTIFIER_LEN:
        raise ValueError(
            f"derived {kind} {name!r} is {len(name)} characters; exceeds the Postgres identifier "
            f"limit of {_MAX_IDENTIFIER_LEN}. Postgres would silently truncate it and the verify "
            f"step, which looks for the untruncated name, would then fail a deploy that appeared to "
            f"succeed. Shorten {source_hint or 'the source table/column name'} in config/tables.json."
        )
    return name


def pg_table_name(table: dict) -> str:
    """Postgres table name = the last dotted part of the 3-part `synced_table_id`.

    (The synced-table create lands the table under this name in `app_schema`; Terraform's
    `for_each` and the deploy loop derive the same value as `${synced_table_id##*.}`.)"""
    parts = str(table["synced_table_id"]).split(".")
    if len(parts) < 3:
        raise ValueError(
            f"synced_table_id {table['synced_table_id']!r} for table {table.get('name')!r} "
            "must be a 3-part catalog.schema.table identifier"
        )
    return parts[-1]


# --- the four idempotent SQL builders (shared with the Liquibase generator) ---------------------
#
# Each returns SQL statement text WITHOUT a trailing ';' — every consumer adds its own terminator
# (the DABs renderer terminates each statement; the Liquibase generator wraps them in changesets).
# This keeps ONE definition of each statement's shape for both paths.

def role_guard_sql(app_role: str) -> str:
    """Idempotent CREATE ROLE, guarded by a `pg_roles` existence check (Postgres has no
    `CREATE ROLE IF NOT EXISTS`). NOLOGIN: a read-only app role consumers are GRANTed into."""
    return (
        "DO $$\n"
        "BEGIN\n"
        f"  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{app_role}') THEN\n"
        f"    CREATE ROLE {app_role} NOLOGIN;\n"
        "  END IF;\n"
        "END $$"
    )


def grant_statements(app_schema: str, app_role: str, synced_table: str) -> list[str]:
    """Explicit native grants on the writer-owned synced table. GRANT is idempotent (a no-op when
    already held) so it is safe to reapply after a synced-table create/replace."""
    return [
        f"GRANT USAGE  ON SCHEMA {app_schema}                 TO {app_role}",
        f"GRANT SELECT ON TABLE  {app_schema}.{synced_table}           TO {app_role}",
    ]


def index_statements(app_schema: str, synced_table: str, index_columns: list) -> list[str]:
    """One idempotent index per configured column — zero columns emit zero statements (no cap).
    CREATE INDEX IF NOT EXISTS => a no-op on a normal deploy, rebuilt after a synced-table replace.

    The derived index name ``idx_<tbl>_<col>`` is length-checked (fix R2): both parts are
    individually legal, but their concatenation can exceed 63 chars and would be silently truncated
    by Postgres, breaking the later verify step — so we reject it here at generation time."""
    statements = []
    for col in index_columns:
        idx_name = validate_derived_name(
            f"idx_{synced_table}_{col}", "index name", f"the table name or the index column {col!r}"
        )
        statements.append(
            f"CREATE INDEX IF NOT EXISTS {idx_name} ON {app_schema}.{synced_table} ({col})"
        )
    return statements


def resolve_view_name(table: dict, synced_table: str) -> str:
    """The consumer view name for a table: the OPTIONAL config `view_name`, else the derived
    ``<synced_table>_v``.

    Why an override exists: blue/green cutover wants a STABLE consumer-view name that a
    ``CREATE OR REPLACE VIEW`` can re-point onto a NEW base synced table, so the app config never
    changes. Without an override the view name is derived from the synced-table name, which changes
    when the base table is recreated under a new id — forcing an app-config change and defeating the
    cutover. The same column set (a same-source synced table) satisfies ``CREATE OR REPLACE``.

    An override is fresh user input baked straight into the CREATE VIEW DDL, so it passes the SAME
    identifier seam every other emitted identifier does — shape (lowercase snake_case), reserved
    word, and the 63-char limit (Postgres would silently truncate a longer name and the verify step,
    which looks for the untruncated name, would then fail a deploy that appeared to succeed). The
    DERIVED default only needs the length check (its parts already passed ``validate_identifier``)."""
    override = table.get("view_name")
    if override:
        return validate_identifier(override, "view_name")
    return validate_derived_name(f"{synced_table}_v", "view name", "the synced table name")


def view_statements(app_schema: str, app_role: str, synced_table: str, view_name: str) -> list[str]:
    """Consumer view + grant — the no-superuser access path. The managed writer role owns the
    synced base table; the deploy identity holds SELECT on that base table and OWNS this view over
    it, so it can grant consumers SELECT on the VIEW. CREATE OR REPLACE so the view self-heals after
    a synced-table replace.

    `view_name` is the ALREADY-RESOLVED, already-validated consumer-view name (from
    `resolve_view_name`: the config override or the derived ``<tbl>_v``). Passing it in keeps ONE
    definition of the view SQL shape for both migration paths while letting each resolve the name
    from the same seam.

    The `GRANT USAGE ON SCHEMA` here mirrors the Liquibase 004 changeset (each runAlways changeset
    is an independent unit, so it self-contains its schema grant). In this single linear render it
    repeats the USAGE grant emitted by `grant_statements`, which is harmless — GRANT is idempotent."""
    return [
        f"CREATE OR REPLACE VIEW {app_schema}.{view_name} AS\n  SELECT * FROM {app_schema}.{synced_table}",
        f"GRANT USAGE  ON SCHEMA {app_schema}                 TO {app_role}",
        f"GRANT SELECT ON {app_schema}.{view_name}                TO {app_role}",
    ]


# --- render one table, then the whole config ----------------------------------------------------

def build_statements(table: dict) -> list[str]:
    """The ordered, validated statements for one table: role -> grants -> N indexes -> view.

    Every identifier (role, schema, pg-table name, each index column) passes through
    `validate_identifier`, so a malformed row fails here rather than emitting broken DDL."""
    role = validate_identifier(table["app_role"], "app_role")
    schema = validate_identifier(table["app_schema"], "app_schema")
    tbl = validate_identifier(pg_table_name(table), "synced_table")
    index_columns = [validate_identifier(c, "index_column") for c in (table.get("index_columns") or [])]

    view = resolve_view_name(table, tbl)
    statements: list[str] = [role_guard_sql(role)]
    statements += grant_statements(schema, role, tbl)
    statements += index_statements(schema, tbl, index_columns)
    statements += view_statements(schema, role, tbl, view)
    return statements


def render_ddl(tables: list[dict]) -> str:
    """Render the full idempotent reconciling DDL for every configured table.

    Deterministic (no timestamps / ids), so two renders are byte-identical and re-applying is a
    clean no-op. Each statement is terminated with ';' so the result executes as one multi-statement
    apply (or pipes to psql). Contains NO `alembic_version` / no migration-version table of any kind.
    """
    chunks: list[str] = [_HEADER]
    for table in tables:
        role = validate_identifier(table["app_role"], "app_role")
        schema = validate_identifier(table["app_schema"], "app_schema")
        tbl = validate_identifier(pg_table_name(table), "synced_table")
        chunks.append(f"\n-- table: {table.get('name', tbl)} (schema {schema}, role {role})")
        for stmt in build_statements(table):
            chunks.append(stmt + ";")
    return "\n".join(chunks) + "\n"


# --- standalone CLI: `python -m dabs.render_ddl [--config …] | psql` ----------------------------

def _default_config() -> Path:
    """Repo default config/tables.json, or LAKEBASE_TABLES_CONFIG if set (the same env var the
    Workflow-job entrypoint honors) — one source of truth for both."""
    override = os.environ.get("LAKEBASE_TABLES_CONFIG")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent / "config" / "tables.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--config",
        default=None,
        help="Path to config/tables.json (default: LAKEBASE_TABLES_CONFIG env / repo default). "
        "Prints idempotent reconciling DDL to stdout for `python -m dabs.render_ddl | psql`.",
    )
    args = parser.parse_args(argv)
    config = Path(args.config) if args.config else _default_config()
    sys.stdout.write(render_ddl(load_tables(config)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
