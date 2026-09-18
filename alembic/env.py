"""Alembic environment for the Lakebase CI/CD reference (Python variant).

Configured for OFFLINE SQL emission only (no ORM autogenerate, no live connection).
`alembic upgrade head --sql` renders idempotent DDL to stdout; a deploy pipes it to psql.

Single seam: the tables config is read here, from the LAKEBASE_TABLES_CONFIG env var,
defaulting to the repo's ../config/tables.json — the SAME single source of truth that
Terraform (`for_each`) and scripts/deploy.sh consume (ADR 0004). Tests point that env var
at a fixture. The loaded list is stashed on `config.attributes["tables"]` so the migration
stays a pure emitter that loops whatever config it is handed.
"""

import json
import os
from logging.config import fileConfig
from pathlib import Path

from alembic import context

# Alembic Config object, providing access to alembic.ini values.
config = context.config

# Interpret the config file for Python logging (guarded — file may be absent under -c).
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Raw-DDL migration, not ORM schema management: no target metadata / autogenerate.
target_metadata = None


def _tables_config_path() -> Path:
    """Path to the tables config: env override, else repo default ../config/tables.json."""
    override = os.environ.get("LAKEBASE_TABLES_CONFIG")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent / "config" / "tables.json"


def load_tables_config() -> list:
    """Read the single-source-of-truth tables config as a list of dicts."""
    with _tables_config_path().open() as fh:
        return json.load(fh)


def run_migrations_offline() -> None:
    """Render migrations as SQL to stdout — no DBAPI connection is created."""
    config.attributes["tables"] = load_tables_config()
    # Dialect-only URL: enough for SQLAlchemy to render Postgres SQL, with no host and
    # no credentials, so nothing connects and no connection string is ever stored.
    url = config.get_main_option("sqlalchemy.url") or "postgresql://"
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    # This reference is offline-only: it renders idempotent DDL for a deploy to pipe to psql.
    raise SystemExit(
        "Offline-only reference: run `alembic upgrade head --sql` (online apply is intentionally not wired)."
    )
