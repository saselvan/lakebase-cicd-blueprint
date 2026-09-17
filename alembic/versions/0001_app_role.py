"""app role per configured table (idempotent)

Ticket 01 — the tracer-bullet slice of the Alembic variant. Mirrors Liquibase changeset
001-app-role.sql: for every table in config/tables.json, create its app role idempotently.
Grants, indexes, and the consumer view are ticket 02 (deliberately NOT emitted here).

Idempotency = runAlways-equivalent (ADR 0002): Postgres has no CREATE ROLE IF NOT EXISTS,
so we wrap it in a DO block guarded by a pg_roles existence check. Re-emitting and
re-applying every deploy is safe, so app access self-heals after a synced-table replace.

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


def _role_guard_sql(app_role: str) -> str:
    """Idempotent CREATE ROLE, guarded by a pg_roles existence check.

    Mirrors liquibase/changelog/001-app-role.sql. NOLOGIN: a read-only app role that
    consumers are GRANTed into (grants are ticket 02).
    """
    return (
        "DO $$\n"
        "BEGIN\n"
        f"  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{app_role}') THEN\n"
        f"    CREATE ROLE {app_role} NOLOGIN;\n"
        "  END IF;\n"
        "END $$"  # op.execute appends the single statement terminator
    )


def _tables() -> list:
    """Tables handed in by env.py (the single config seam)."""
    return context.config.attributes.get("tables", [])


def upgrade() -> None:
    for table in _tables():
        op.execute(_role_guard_sql(table["app_role"]))


def downgrade() -> None:
    for table in _tables():
        op.execute(f"DROP ROLE IF EXISTS {table['app_role']};")
