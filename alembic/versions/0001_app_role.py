"""app role, grants, indexes, and consumer view per configured table (idempotent)

The Alembic variant of the reference migration. For every table in config/tables.json it
emits the SAME four object groups as the Liquibase changelog, mirroring one changeset per
helper inside a single upgrade():

  1. app role      — Liquibase 001-app-role.sql   (ticket 01)
  2. grants        — Liquibase 002-app-grants.sql  (ticket 02)
  3. indexes       — Liquibase 003-indexes.sql     (ticket 02)
  4. consumer view — Liquibase 004-app-view.sql    (ticket 02)

Idempotency = runAlways-equivalent (ADR 0002). Postgres has no CREATE ROLE / CREATE INDEX
"IF NOT EXISTS ROLE", so the role is guarded by a pg_roles existence check; indexes use
CREATE INDEX IF NOT EXISTS; grants are no-ops when already held; the view is CREATE OR
REPLACE. Re-emitting and re-applying every deploy is therefore safe, so app access
self-heals after a synced-table replace (ADR 0001 = the no-superuser view path).

Revision ID: 0001_app_role
Revises:
Create Date: offline / config-driven
"""

from alembic import context, op

# revision identifiers, used by Alembic.
revision = "0001_app_role"
down_revision = None
branch_labels = None
depends_on = None


def _synced_table(table: dict) -> str:
    """Postgres table name = last dotted segment of `synced_table_id` (ADR 0004)."""
    return table["synced_table_id"].rsplit(".", 1)[-1]


def _role_guard_sql(app_role: str) -> str:
    """Idempotent CREATE ROLE, guarded by a pg_roles existence check.

    Mirrors liquibase/changelog/001-app-role.sql. NOLOGIN: a read-only app role that
    consumers are GRANTed into.
    """
    return (
        "DO $$\n"
        "BEGIN\n"
        f"  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{app_role}') THEN\n"
        f"    CREATE ROLE {app_role} NOLOGIN;\n"
        "  END IF;\n"
        "END $$"  # op.execute appends the single statement terminator
    )


def _grant_statements(app_schema: str, app_role: str, synced_table: str) -> list:
    """Explicit native grants on the writer-owned synced table.

    Mirrors liquibase/changelog/002-app-grants.sql: GRANT is idempotent (no-op when
    already held) and reapplied every deploy so access survives a table replace.
    """
    return [
        f"GRANT USAGE ON SCHEMA {app_schema} TO {app_role}",
        f"GRANT SELECT ON TABLE {app_schema}.{synced_table} TO {app_role}",
    ]


def _index_statements(app_schema: str, synced_table: str, index_columns: list) -> list:
    """One idempotent index per configured column — zero columns emit zero statements.

    Mirrors liquibase/changelog/003-indexes.sql, but parametrized over the full
    index_columns list (0/1/N) instead of the Liquibase two-slot workaround.
    CREATE INDEX IF NOT EXISTS => no-op on a normal deploy, rebuilt after a replace.
    """
    return [
        f"CREATE INDEX IF NOT EXISTS idx_{synced_table}_{col} "
        f"ON {app_schema}.{synced_table} ({col})"
        for col in index_columns
    ]


def _view_statements(app_schema: str, app_role: str, synced_table: str) -> list:
    """Consumer view + grant — the no-superuser access path (ADR 0001).

    Mirrors liquibase/changelog/004-app-view.sql: the deploy identity owns the view, so
    it can grant consumers SELECT on the VIEW without holding the un-grantable writer
    role. CREATE OR REPLACE so the view self-heals after a synced-table replace.
    """
    view = f"{synced_table}_v"
    return [
        f"CREATE OR REPLACE VIEW {app_schema}.{view} AS "
        f"SELECT * FROM {app_schema}.{synced_table}",
        f"GRANT SELECT ON {app_schema}.{view} TO {app_role}",
    ]


def _tables() -> list:
    """Tables handed in by env.py (the single config seam)."""
    return context.config.attributes.get("tables", [])


def upgrade() -> None:
    for table in _tables():
        app_schema = table["app_schema"]
        app_role = table["app_role"]
        synced_table = _synced_table(table)
        index_columns = table.get("index_columns", [])

        op.execute(_role_guard_sql(app_role))
        for stmt in _grant_statements(app_schema, app_role, synced_table):
            op.execute(stmt)
        for stmt in _index_statements(app_schema, synced_table, index_columns):
            op.execute(stmt)
        for stmt in _view_statements(app_schema, app_role, synced_table):
            op.execute(stmt)


def downgrade() -> None:
    for table in _tables():
        app_schema = table["app_schema"]
        app_role = table["app_role"]
        synced_table = _synced_table(table)
        op.execute(f"DROP VIEW IF EXISTS {app_schema}.{synced_table}_v;")
        for col in table.get("index_columns", []):
            op.execute(f"DROP INDEX IF EXISTS {app_schema}.idx_{synced_table}_{col};")
        op.execute(f"DROP ROLE IF EXISTS {app_role};")
