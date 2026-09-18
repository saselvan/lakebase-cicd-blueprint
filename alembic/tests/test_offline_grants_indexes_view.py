"""Offline-SQL emission tests for ticket 02 — grants, indexes, and the consumer view.

Same single seam as ticket 01: `alembic upgrade head --sql` runs with NO database
connection, driven by the HOSTILE fixture `tables.json` (LAKEBASE_TABLES_CONFIG). We
assert only on the emitted SQL string, never on internal function names.

The fixture is deliberately hostile (mirrors tdd-falsifiability):
  * alpha  — schema `sch_alpha`, role `alpha_reader`, ONE index column (`region_key`)
  * beta   — schema `sch_beta`,  role `beta_reporting_ro`, ZERO index columns
Two DISTINCT tables with distinct schemas/roles/view names — anything hardcoded to
`members` (the repo default) cannot pass. `region_key` also differs from any schema
name, so an index column can't be confused with a schema.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

ALEMBIC_DIR = Path(__file__).resolve().parents[1]
ALEMBIC_INI = ALEMBIC_DIR / "alembic.ini"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "tables.json"

# Per-entry expectations pulled straight from the hostile fixture.
ALPHA = {"schema": "sch_alpha", "role": "alpha_reader", "table": "alpha", "index_cols": ["region_key"]}
BETA = {"schema": "sch_beta", "role": "beta_reporting_ro", "table": "beta", "index_cols": []}
TABLES = [ALPHA, BETA]


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


def _norm(sql: str) -> str:
    """Collapse all runs of whitespace to a single space for whitespace-robust matching."""
    return re.sub(r"\s+", " ", sql)


# --- Grants -----------------------------------------------------------------

def test_schema_and_table_grant_per_table():
    """Each entry emits GRANT USAGE ON SCHEMA and GRANT SELECT ON TABLE for its own
    schema/table/role (config-driven, not hardcoded to `members`).

    Guards mutation: hardcoding `members` — the second (beta) table's grants vanish.
    """
    sql = _norm(_generate_offline_sql())
    for t in TABLES:
        assert f"GRANT USAGE ON SCHEMA {t['schema']} TO {t['role']}" in sql, (
            f"missing GRANT USAGE for {t['schema']}/{t['role']} in:\n{sql}"
        )
        assert f"GRANT SELECT ON TABLE {t['schema']}.{t['table']} TO {t['role']}" in sql, (
            f"missing GRANT SELECT ON TABLE for {t['schema']}.{t['table']} in:\n{sql}"
        )


# --- Indexes ----------------------------------------------------------------

def test_index_for_one_index_column_table():
    """The 1-index table emits exactly one CREATE INDEX IF NOT EXISTS for its column.

    Guards mutation: deleting `IF NOT EXISTS` from the index DDL turns this red.
    """
    sql = _norm(_generate_offline_sql())
    col = ALPHA["index_cols"][0]
    expected = (
        f"CREATE INDEX IF NOT EXISTS idx_{ALPHA['table']}_{col} "
        f"ON {ALPHA['schema']}.{ALPHA['table']} ({col})"
    )
    assert expected in sql, f"missing idempotent index for alpha.{col} in:\n{sql}"


def test_zero_index_column_table_emits_no_index():
    """The 0-index table emits NO index statement, and there is exactly one index
    across the whole emit (alpha's only).

    Guards mutation: emitting an index when index_columns is empty turns this red.
    """
    sql = _norm(_generate_offline_sql())
    assert sql.count("CREATE INDEX") == 1, (
        f"expected exactly one CREATE INDEX (alpha only), got {sql.count('CREATE INDEX')}:\n{sql}"
    )
    assert f"idx_{BETA['table']}_" not in sql, f"beta must not get an index in:\n{sql}"
    assert f"ON {BETA['schema']}.{BETA['table']} (" not in sql, (
        f"no index may target the zero-index table beta in:\n{sql}"
    )


# --- Consumer view ----------------------------------------------------------

def test_create_or_replace_view_per_table():
    """Each entry emits CREATE OR REPLACE VIEW <schema>.<table>_v AS SELECT * FROM
    <schema>.<table> — the replace-safe form.

    Guards mutation: CREATE VIEW instead of CREATE OR REPLACE VIEW turns this red.
    """
    sql = _norm(_generate_offline_sql())
    for t in TABLES:
        expected = (
            f"CREATE OR REPLACE VIEW {t['schema']}.{t['table']}_v AS "
            f"SELECT * FROM {t['schema']}.{t['table']}"
        )
        assert expected in sql, f"missing CREATE OR REPLACE VIEW for {t['table']} in:\n{sql}"


def test_grant_select_on_view_per_table():
    """Each entry grants SELECT on its view to its app role.

    Guards mutation: dropping the GRANT SELECT ... ON <view> line turns this red.
    """
    sql = _norm(_generate_offline_sql())
    for t in TABLES:
        assert f"GRANT SELECT ON {t['schema']}.{t['table']}_v TO {t['role']}" in sql, (
            f"missing GRANT SELECT on view {t['table']}_v to {t['role']} in:\n{sql}"
        )


# --- Config-driven object names (nothing hardcoded to `members`) ------------

def test_object_names_come_from_config_not_hardcoded():
    """Distinct per-table object names (schemas, roles, view names) all appear, and
    the repo default `members` never does.

    Guards mutation: hardcoding `members` instead of looping config turns this red.
    """
    sql = _norm(_generate_offline_sql())
    for token in ("sch_alpha", "alpha_reader", "alpha_v",
                  "sch_beta", "beta_reporting_ro", "beta_v"):
        assert token in sql, f"expected config-derived token {token!r} in:\n{sql}"
    assert "members" not in sql, f"nothing may be hardcoded to `members`:\n{sql}"


def test_role_creation_still_emitted_from_ticket_01():
    """Ticket 01's idempotent app-role creation stays intact alongside the new groups."""
    sql = _generate_offline_sql()
    for t in TABLES:
        guard = re.compile(
            r"IF\s+NOT\s+EXISTS\s*\(\s*SELECT.*?FROM\s+pg_roles.*?"
            + re.escape(t["role"])
            + r".*?\)\s*THEN\s*CREATE\s+ROLE\s+"
            + re.escape(t["role"]),
            re.IGNORECASE | re.DOTALL,
        )
        assert guard.search(sql), f"ticket-01 role guard for {t['role']} was lost in:\n{sql}"


def test_emission_is_idempotent_across_two_runs():
    """Two offline emissions are byte-identical (runAlways-equivalent, no per-run state)."""
    assert _generate_offline_sql() == _generate_offline_sql()
