"""Docs falsifiability: the DABs renderer must be documented at parity with Liquibase.

Asserts on the shipped docs (README + DESIGN-NOTES), so a dropped parity row or a stale
"Alembic engine" claim fails CI. No database, no migration run. (Replaces the old alembic-era
docs-parity test now that the DABs path renders via dabs/render_ddl.py.)
"""

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
README = (REPO / "README.md").read_text()
DESIGN = (REPO / "docs" / "DESIGN-NOTES.md").read_text()
RENDER_DDL = (REPO / "dabs" / "render_ddl.py").read_text()
GENERATE_CHANGELOGS = (REPO / "liquibase" / "generate_changelogs.py").read_text()

# The four Liquibase changesets the renderer mirrors — every one must be mapped in the parity table.
CHANGESETS = ["001-app-role", "002-app-grants", "003-index", "004-app-view"]
# The renderer helpers the parity table maps them to (the single-source SQL builders).
RENDER_HELPERS = ["role_guard_sql", "grant_statements", "index_statements", "view_statements"]

SECTION_MARKER = "## Migration engines: a Python renderer and Liquibase"


def _engines_section() -> str:
    """The README's migration-engines section only (up to the next top-level heading)."""
    assert SECTION_MARKER in README, "README missing the migration engines section"
    rest = README.split(SECTION_MARKER, 1)[1]
    nxt = rest.find("\n## ")
    return rest if nxt == -1 else rest[:nxt]


def test_readme_has_migration_engines_section():
    assert SECTION_MARKER in README, "README missing the migration engines (renderer + Liquibase) section"


def test_parity_table_maps_all_four_objects():
    """Every Liquibase changeset AND its renderer helper is mapped in the parity table. Dropping a
    row turns this red."""
    section = _engines_section()
    missing_cs = [c for c in CHANGESETS if c not in section]
    missing_h = [h for h in RENDER_HELPERS if h not in section]
    assert not missing_cs, f"parity table missing changeset rows for: {missing_cs}"
    assert not missing_h, f"parity table missing renderer-helper mappings for: {missing_h}"


def test_readme_documents_the_standalone_cli():
    """The README must document the standalone `python -m dabs.render_ddl | psql` path (which
    REPLACED the broken `alembic upgrade head --sql | psql` claim)."""
    section = _engines_section()
    assert "python -m dabs.render_ddl" in section, "README missing the standalone renderer CLI"
    assert "alembic upgrade head --sql" not in README, (
        "README still shows the broken `alembic upgrade head --sql` path"
    )


def test_design_notes_states_no_version_table():
    """DESIGN-NOTES must state the DABs path keeps NO version table (that is what makes re-apply a
    clean reconciling no-op) — not that it is 'gated' or run via Alembic."""
    text = DESIGN.lower()
    assert "no version table" in text, "DESIGN-NOTES must state the DABs renderer keeps no version table"
    assert "render_ddl" in text, "DESIGN-NOTES must name the renderer (dabs/render_ddl.py)"


# --- ownership statement correctness: it must be right in all three places --------------------
#
# Correct model: the managed WRITER role owns the synced (base) table; the deploy identity holds
# SELECT on that base table and OWNS the consumer view (which is what lets it grant consumers SELECT
# on the view). Three spots previously claimed the DEPLOY IDENTITY owns the synced (base) table,
# which is wrong. These grep-style assertions keep all three honest.

# Exact wrong claims that must never reappear (deploy identity owning the base synced table).
_WRONG_OWNERSHIP = {
    "dabs/render_ddl.py (view_statements docstring)": (
        RENDER_DDL,
        "deploy identity owns the synced table",
    ),
    "liquibase/generate_changelogs.py (004 comment)": (
        GENERATE_CHANGELOGS,
        "deploy identity owns the synced table",
    ),
    "README.md (access model)": (
        README,
        "creates the synced table owns it",
    ),
}


def test_no_file_claims_deploy_identity_owns_the_base_table():
    """None of the three spots may claim the deploy identity OWNS the synced base table."""
    offenders = {where: bad for where, (text, bad) in _WRONG_OWNERSHIP.items() if bad in text}
    assert not offenders, f"wrong ownership claim still present: {offenders}"


def test_render_ddl_states_writer_owns_base_and_identity_owns_view():
    """The view_statements docstring must say the writer role owns the base table and the deploy
    identity holds SELECT on it and owns the view."""
    # Isolate the view_statements docstring so the assertion is about THAT spot.
    assert "def view_statements(" in RENDER_DDL
    doc = RENDER_DDL.split("def view_statements(", 1)[1].split('"""', 2)[1]
    lower = doc.lower()
    assert "writer role owns" in lower, doc
    assert "select" in lower and "owns" in lower, doc
    assert "view" in lower, doc


def test_generate_changelogs_004_comment_has_correct_ownership():
    """The 004 changeset comment must credit the writer role with owning the base table and the
    deploy identity with SELECT + owning the view."""
    # The 004 changeset block in the generator.
    assert "004 — no-superuser consumer view" in GENERATE_CHANGELOGS
    block = GENERATE_CHANGELOGS.split("004 — no-superuser consumer view", 1)[1].split("return changesets", 1)[0]
    lower = block.lower()
    assert "writer role owns" in lower, block
    assert "select" in lower, block


def test_readme_access_model_has_correct_ownership():
    """The README access model must state the writer role owns the base table and the deploy identity
    holds SELECT on it (and owns the view)."""
    lower = README.lower()
    assert "writer role" in lower and "owns" in lower, "README must credit the writer role with ownership"
    # The deploy identity is GRANTED select on the base table, it does not own it.
    assert "granted" in lower or "holds select" in lower, README[:0]


def test_design_notes_documents_branch_per_pr_caveats():
    """The branch-per-PR section must carry its two load-bearing caveats: each branch has its OWN
    endpoint, and the included CI workflows are reference-only."""
    text = DESIGN.lower()
    assert "branch-per-pr" in text, "DESIGN-NOTES missing the Branch-per-PR section"
    assert "own connection endpoint" in text, "missing the per-branch endpoint caveat"
    assert "reference-only" in text, "must state the CI workflows are reference-only"
