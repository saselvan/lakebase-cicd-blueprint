"""Docs falsifiability: the Alembic path must be documented at parity.

Asserts on the shipped docs (README + DESIGN-NOTES), so a dropped parity row or a
contradicting runAlways claim fails CI. No database, no alembic run.
"""

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
README = (REPO / "README.md").read_text()
DESIGN = (REPO / "docs" / "DESIGN-NOTES.md").read_text()

# The four Liquibase changesets that the Alembic variant mirrors — every one must be
# mapped in the README parity table.
CHANGESETS = ["001-app-role", "002-app-grants", "003-indexes", "004-app-view"]

SECTION_MARKER = "## Migration engines: Alembic and Liquibase"


def _alembic_section() -> str:
    """The README's Alembic section only (up to the next top-level heading), so parity
    assertions are scoped to the parity table — not to changeset names that also appear
    elsewhere in the README (e.g. the pattern section)."""
    assert SECTION_MARKER in README, "README missing the migration engines section"
    rest = README.split(SECTION_MARKER, 1)[1]
    nxt = rest.find("\n## ")
    return rest if nxt == -1 else rest[:nxt]


def test_readme_has_alembic_section():
    assert SECTION_MARKER in README, "README missing the migration engines (Alembic + Liquibase) section"


def test_parity_table_maps_all_four_changesets():
    """Every Liquibase changeset is mapped IN THE ALEMBIC PARITY TABLE. Dropping a row
    (mutation 1) turns this red."""
    section = _alembic_section()
    missing = [c for c in CHANGESETS if c not in section]
    assert not missing, f"Alembic parity table missing rows for: {missing}"


def test_design_notes_states_runalways_equivalent_correctly():
    """The runAlways-equivalent must be stated as idempotent-every-deploy, NOT gated by
    Alembic's version table (mutation 2 = the contradicting claim)."""
    text = DESIGN.lower()
    assert "runalways" in text and "version table" in text, (
        "DESIGN-NOTES missing the Alembic runAlways-equivalent note"
    )
    # The correct claim: NOT gated by the version table.
    assert "not gated" in text, (
        "DESIGN-NOTES must say the Alembic path is NOT gated by the version table"
    )


def test_design_notes_documents_branch_per_pr_caveats():
    """The branch-per-PR section must carry the two load-bearing caveats: each branch has
    its OWN endpoint, and the included CI workflows are reference-only."""
    text = DESIGN.lower()
    assert "branch-per-pr" in text, "DESIGN-NOTES missing the Branch-per-PR section"
    assert "own connection endpoint" in text, "missing the per-branch endpoint caveat"
    assert "reference-only" in text, "must state the CI workflows are reference-only"
