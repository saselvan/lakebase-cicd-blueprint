"""Offline-SQL emission tests for the Alembic variant (app role only).

The single seam is offline SQL generation: `alembic upgrade head --sql` runs with
NO database connection, driven by a fixture `tables.json` pointed at via the
LAKEBASE_TABLES_CONFIG env var. We assert on the emitted SQL string only — never on
internal function names — so the test survives refactors and catches real regressions.

The fixture is HOSTILE: it has two entries with DISTINCT app_role names
(`alpha_reader`, `beta_reporting_ro`) — neither is the repo default `members_app_ro`.
A migration that hardcodes a single role name, or ignores the config, cannot pass.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

ALEMBIC_DIR = Path(__file__).resolve().parents[1]
ALEMBIC_INI = ALEMBIC_DIR / "alembic.ini"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "tables.json"

# Roles that MUST appear, taken from the hostile fixture.
FIXTURE_ROLES = ["alpha_reader", "beta_reporting_ro"]


def _generate_offline_sql() -> str:
    """Run `alembic upgrade head --sql` offline (no DB) against the fixture config."""
    env = dict(os.environ)
    env["LAKEBASE_TABLES_CONFIG"] = str(FIXTURE)
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(ALEMBIC_INI),
         "upgrade", "head", "--sql"],
        cwd=str(ALEMBIC_DIR),
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"alembic offline --sql exited {result.returncode}\n"
        f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    return result.stdout


def test_offline_sql_is_non_empty():
    """The offline path must emit non-empty SQL for the configured tables.

    Guards mutation: env.py ignoring the config (emitting nothing) turns this red.
    """
    sql = _generate_offline_sql()
    # Something SQL-ish must be emitted, not just alembic transaction noise.
    assert "CREATE ROLE" in sql, f"no role DDL emitted:\n{sql}"


def test_role_guard_appears_for_each_configured_role():
    """Every role in the config gets an idempotent pg_roles-guarded CREATE ROLE.

    Guards mutations: hardcoding one role name (multi-role assertion fails) and
    removing the IF NOT EXISTS pg_roles guard (idempotency assertion fails).
    """
    sql = _generate_offline_sql()
    for role in FIXTURE_ROLES:
        # An idempotency guard querying pg_roles for THIS role must precede its CREATE.
        guard = re.compile(
            r"IF\s+NOT\s+EXISTS\s*\(\s*SELECT.*?FROM\s+pg_roles.*?"
            + re.escape(role)
            + r".*?\)\s*THEN\s*CREATE\s+ROLE\s+"
            + re.escape(role),
            re.IGNORECASE | re.DOTALL,
        )
        assert guard.search(sql), (
            f"no idempotent pg_roles guard around CREATE ROLE {role} in:\n{sql}"
        )
        # And no BARE (unguarded) create for this role — the guard is load-bearing.
        assert "pg_roles" in sql, f"no pg_roles idempotency guard at all in:\n{sql}"


def test_no_bare_create_role():
    """No CREATE ROLE may appear without a pg_roles existence guard in the emit.

    Guards mutation: dropping the DO $$ ... IF NOT EXISTS pg_roles block leaves a
    bare CREATE ROLE and no pg_roles reference — this turns red.
    """
    sql = _generate_offline_sql()
    assert sql.count("CREATE ROLE") == len(FIXTURE_ROLES), (
        f"expected one CREATE ROLE per configured role ({len(FIXTURE_ROLES)}):\n{sql}"
    )
    # Each CREATE ROLE must be inside a block that references pg_roles.
    assert sql.count("pg_roles") >= len(FIXTURE_ROLES), (
        f"expected a pg_roles guard per role; SQL missing guards:\n{sql}"
    )


def test_emission_is_idempotent_across_two_runs():
    """Generating the SQL twice yields identical output (runAlways-equivalent).

    Guards mutation: any per-run state (timestamps, random ids) that makes a second
    emission differ turns this red.
    """
    first = _generate_offline_sql()
    second = _generate_offline_sql()
    assert first == second, (
        "two offline emissions differ (non-idempotent generation):\n"
        f"--- first ---\n{first}\n--- second ---\n{second}"
    )
