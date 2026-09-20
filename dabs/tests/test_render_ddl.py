"""Falsifiability tests for the DABs migration RENDERER (fix B).

The DABs migration reconcile no longer uses Alembic or an `alembic_version` table. A single
Python renderer module (`dabs/render_ddl.py`) emits the idempotent reconciling DDL directly from
`config/tables.json` by calling the four shared SQL helpers (role guard, grants, N indexes, view)
— the SAME helpers the Liquibase generator consumes (single source of the idempotent SQL). This
module is exposed two ways: the DABs Workflow-job entrypoint renders through it, and
`python -m dabs.render_ddl [--config …]` prints the SQL to stdout for `… | psql`.

Why this replaces the old alembic path (the bug it fixes): `alembic upgrade head --sql` prepended
UNGUARDED version bookkeeping (`CREATE TABLE alembic_version` + an `INSERT` with no conflict
guard). With a 2nd Alembic revision the reconcile's regex-patch (`make_rerunnable`) created a
duplicate `alembic_version` row on the 2nd apply → `duplicate key … alembic_version_pkc` → the
whole transaction rolled back and the object DDL never reconciled. The renderer emits NO version
bookkeeping at all, so there is nothing to roll back — reconcile is a clean no-op on every run.

We assert on the EMITTED SQL and the CLI's observable output — never on private helper names — so
these survive refactors and catch real regressions. No database, no alembic, no docker: this lands
in the no-cloud `dabs-validate` CI job.

The fixture (`dabs/tests/fixtures/tables.json`) is HOSTILE by construction:
  - `claims` and `members` SHARE app_schema "shared_schema" with DISTINCT roles.
  - `claims` carries THREE index columns — a 2-slot cap would silently drop the third.
  - `providers` has ZERO index columns — an empty list must emit no CREATE INDEX.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from dabs import render_ddl as rd

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "tables.json"


def _fixture_rows() -> list[dict]:
    return json.loads(FIXTURE.read_text())


def _render() -> str:
    return rd.render_ddl(rd.load_tables(FIXTURE))


# --- 1. NO alembic version bookkeeping anywhere (the fix) --------------------------------------

def test_render_has_no_alembic_version_bookkeeping():
    """The rendered SQL carries NO `alembic_version` table and NO `CREATE TABLE alembic_version`
    — the whole class of "duplicate key on the 2nd apply → rollback" is structurally impossible.

    MUTATION GATE: reintroduce any `alembic_version` write (or the old make_rerunnable path) and
    this goes RED.
    """
    sql = _render()
    assert "alembic_version" not in sql, f"renderer emitted alembic version bookkeeping:\n{sql}"
    assert not re.search(r"CREATE\s+TABLE\s+(IF\s+NOT\s+EXISTS\s+)?alembic_version", sql, re.I), (
        f"renderer emitted a CREATE TABLE alembic_version:\n{sql}"
    )
    # No migration-version table of ANY name (the renderer reconciles; it does not track revisions).
    assert "version_num" not in sql, f"renderer emitted a version-tracking column:\n{sql}"


# --- 2. the four idempotent object groups, per table -------------------------------------------

def test_render_emits_guarded_role_per_table():
    """Each app role is created via a `pg_roles` existence guard (idempotent CREATE ROLE)."""
    sql = _render()
    for row in _fixture_rows():
        role = row["app_role"]
        assert re.search(
            rf"IF NOT EXISTS \(SELECT 1 FROM pg_roles WHERE rolname = '{role}'\)", sql
        ), f"role {role} not guarded by a pg_roles existence check:\n{sql}"
        assert f"CREATE ROLE {role} NOLOGIN" in sql, f"role {role} not created NOLOGIN:\n{sql}"


def test_render_emits_grants_per_table():
    """Explicit GRANT USAGE on schema + GRANT SELECT on the synced table, per table."""
    sql = _render()
    for row in _fixture_rows():
        schema, role = row["app_schema"], row["app_role"]
        tbl = row["synced_table_id"].split(".")[-1]
        assert re.search(rf"GRANT USAGE\s+ON SCHEMA {schema}\s+TO {role}", sql), (
            f"missing GRANT USAGE for {row['name']}:\n{sql}"
        )
        assert re.search(rf"GRANT SELECT ON TABLE\s+{schema}\.{tbl}\s+TO {role}", sql), (
            f"missing GRANT SELECT on table for {row['name']}:\n{sql}"
        )


def test_render_emits_one_index_per_column_no_cap():
    """N index columns -> N `CREATE INDEX IF NOT EXISTS`. `claims` (3 cols) is the cap-killer."""
    sql = _render()
    expected = {"claims": ["member_id", "provider_id", "service_date"], "members": ["plan_code"], "providers": []}
    for row in _fixture_rows():
        tbl = row["synced_table_id"].split(".")[-1]
        schema = row["app_schema"]
        for col in expected[row["name"]]:
            assert re.search(
                rf"CREATE INDEX IF NOT EXISTS idx_{tbl}_{col} ON {schema}\.{tbl} \({col}\)", sql
            ), f"{row['name']}: missing idempotent index on {col}:\n{sql}"


def test_zero_index_table_emits_no_index():
    """`providers` has index_columns == [] -> no CREATE INDEX mentions its table."""
    sql = _render()
    assert "idx_providers_synced" not in sql, f"providers emitted an index despite 0 columns:\n{sql}"


def test_render_emits_consumer_view_per_table():
    """A `CREATE OR REPLACE VIEW <schema>.<tbl>_v` + a grant on the view, per table."""
    sql = _render()
    for row in _fixture_rows():
        schema, role = row["app_schema"], row["app_role"]
        tbl = row["synced_table_id"].split(".")[-1]
        assert f"CREATE OR REPLACE VIEW {schema}.{tbl}_v" in sql, (
            f"missing consumer view for {row['name']}:\n{sql}"
        )
        assert re.search(rf"GRANT SELECT ON {schema}\.{tbl}_v\s+TO {role}", sql), (
            f"missing GRANT SELECT on the view for {row['name']}:\n{sql}"
        )


# --- 3. re-runnable shape + determinism --------------------------------------------------------

def test_render_is_deterministic():
    """Two renders are byte-identical — no timestamps / per-run state (a clean reconciling no-op)."""
    assert _render() == _render()


def test_every_statement_is_terminated():
    """Every emitted statement ends with ';' so the blob is executable as one multi-statement apply.
    (Comment lines and blanks aside.)"""
    sql = _render()
    # Strip comment lines/blanks, collapse the DO-block bodies, then check each statement terminator.
    non_comment = [ln for ln in sql.splitlines() if ln.strip() and not ln.lstrip().startswith("--")]
    joined = "\n".join(non_comment).strip()
    assert joined.endswith(";"), f"rendered SQL does not end on a terminated statement:\n{sql}"
    assert "CREATE INDEX IF NOT EXISTS" in joined and "CREATE OR REPLACE VIEW" in joined


# --- 4. identifier validation goes through the SAME seam ---------------------------------------

def test_identifier_validation_seam_rejects_empty():
    """The renderer exposes validate_identifier (shared with the Liquibase path) and it rejects an
    empty / non-string identifier so malformed config fails loudly instead of emitting broken DDL."""
    assert hasattr(rd, "validate_identifier")
    for bad in ["", None]:
        with pytest.raises((ValueError, TypeError)):
            rd.validate_identifier(bad, "app_role")
    assert rd.validate_identifier("members_app_ro", "app_role") == "members_app_ro"


def test_render_rejects_malformed_config(tmp_path):
    """A row with an empty app_role must fail rendering via the validation seam (hostile input)."""
    rows = _fixture_rows()
    rows[0]["app_role"] = ""
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(rows))
    with pytest.raises((ValueError, TypeError)):
        rd.render_ddl(rd.load_tables(bad))


# --- 4b. fix D: reject UNSAFE identifiers (not just empty) at the shared seam -------------------
#
# Every identifier baked into DDL is interpolated straight into SQL, so a hyphenated / reserved /
# oversized / uppercase value would break the SQL or be an injection vector. The seam accepts ONLY
# safe unquoted Postgres identifiers (lowercase snake_case, <=63 chars) and rejects a curated set of
# reserved words. These fixtures are the reviewer's mutations for fix D.

HOSTILE_HYPHEN = "Plan-Code"          # hyphen — not a legal unquoted identifier / injection shape
HOSTILE_RESERVED = "user"             # a reserved word that MATCHES the safe regex
HOSTILE_RESERVED_2 = "table"          # a second reserved word
HOSTILE_UPPER = "PlanCode"            # uppercase — violates the lowercase snake_case rule
HOSTILE_TOOLONG = "a" * 64            # 64 chars — over the Postgres 63-char identifier limit

HOSTILE_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "tables_hostile_identifiers.json"


@pytest.mark.parametrize(
    "bad", [HOSTILE_HYPHEN, HOSTILE_RESERVED, HOSTILE_RESERVED_2, HOSTILE_UPPER, HOSTILE_TOOLONG]
)
def test_validate_identifier_rejects_unsafe(bad):
    """validate_identifier rejects a hyphenated, reserved-word, uppercase, or over-63-char
    identifier, and the error names the offending value + the kind. MUTATION GATE: loosen the seam
    back to empty-only and every case here goes RED.
    """
    with pytest.raises(ValueError) as exc:
        rd.validate_identifier(bad, "app_role")
    msg = str(exc.value)
    assert bad in msg, f"error must name the offending value {bad!r}: {msg}"
    assert "app_role" in msg, f"error must name the kind: {msg}"


def test_validate_identifier_accepts_snake_case():
    """A normal lowercase snake_case identifier (incl. one at the 63-char boundary) passes."""
    assert rd.validate_identifier("members_app_ro", "app_role") == "members_app_ro"
    assert rd.validate_identifier("a" * 63, "app_role") == "a" * 63


@pytest.mark.parametrize("field,bad", [
    ("app_role", HOSTILE_RESERVED),
    ("app_schema", HOSTILE_HYPHEN),
    ("app_schema", HOSTILE_UPPER),
    ("app_role", HOSTILE_TOOLONG),
])
def test_render_rejects_hostile_scalar_identifier(field, bad, tmp_path):
    """A hostile app_role / app_schema must fail rendering (identifier baked into the DDL)."""
    rows = _fixture_rows()
    rows[0][field] = bad
    bad_file = tmp_path / "hostile.json"
    bad_file.write_text(json.dumps(rows))
    with pytest.raises(ValueError):
        rd.render_ddl(rd.load_tables(bad_file))


def test_render_rejects_hostile_index_column(tmp_path):
    """A hostile index column (`Plan-Code`) must be rejected — EACH index column is baked into a
    CREATE INDEX and must pass through the seam."""
    rows = _fixture_rows()
    rows[0]["index_columns"] = ["member_id", HOSTILE_HYPHEN]
    bad_file = tmp_path / "hostile_idx.json"
    bad_file.write_text(json.dumps(rows))
    with pytest.raises(ValueError):
        rd.render_ddl(rd.load_tables(bad_file))


def test_render_rejects_hostile_pg_table_name(tmp_path):
    """A hostile pg table name (last dotted part of synced_table_id) must be rejected."""
    rows = _fixture_rows()
    rows[0]["synced_table_id"] = "cat_a.shared_schema.Bad-Table"
    bad_file = tmp_path / "hostile_tbl.json"
    bad_file.write_text(json.dumps(rows))
    with pytest.raises(ValueError):
        rd.render_ddl(rd.load_tables(bad_file))


def test_committed_hostile_fixture_is_rejected():
    """The committed hostile fixture (a hyphenated `Plan-Code` index column) must be rejected by the
    renderer — a durable hostile artifact for the fix D seam."""
    with pytest.raises(ValueError):
        rd.render_ddl(rd.load_tables(HOSTILE_FIXTURE))


def test_valid_config_still_renders():
    """The shipped valid config still renders full object DDL (the strict seam does not reject any
    real lowercase snake_case identifier)."""
    sql = rd.render_ddl(rd.load_tables(REPO_ROOT / "config" / "tables.json"))
    assert "CREATE OR REPLACE VIEW" in sql and "CREATE INDEX IF NOT EXISTS" in sql


# --- 5. the standalone CLI (`python -m dabs.render_ddl | psql`) --------------------------------

def test_cli_prints_the_rendered_sql(tmp_path):
    """`python -m dabs.render_ddl --config <fixture>` prints exactly the SQL render_ddl() produces,
    so `python -m dabs.render_ddl | psql` applies the same idempotent DDL — this REPLACES the old,
    broken `alembic upgrade head --sql | psql` claim.
    """
    result = subprocess.run(
        [sys.executable, "-m", "dabs.render_ddl", "--config", str(FIXTURE)],
        cwd=str(REPO_ROOT), capture_output=True, text=True,
    )
    assert result.returncode == 0, f"CLI failed:\nSTDOUT:{result.stdout}\nSTDERR:{result.stderr}"
    assert result.stdout.strip() == _render().strip(), "CLI output diverged from render_ddl()"
    assert "alembic_version" not in result.stdout, "CLI emitted alembic version bookkeeping"


def test_cli_defaults_to_repo_config():
    """With no --config the CLI renders the repo's config/tables.json (the single source of truth)."""
    result = subprocess.run(
        [sys.executable, "-m", "dabs.render_ddl"],
        cwd=str(REPO_ROOT), capture_output=True, text=True,
    )
    assert result.returncode == 0, f"CLI failed:\nSTDOUT:{result.stdout}\nSTDERR:{result.stderr}"
    assert "CREATE OR REPLACE VIEW" in result.stdout, "default CLI render produced no object DDL"
    assert "alembic_version" not in result.stdout
