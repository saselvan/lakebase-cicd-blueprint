"""Docs falsifiability: the DABs renderer must be documented at parity with Liquibase.

Asserts on the shipped docs (README + DESIGN-NOTES), so a dropped parity row or a stale claim that
the DABs path runs an external, version-tracking migration engine fails CI. No database, no
migration run — the DABs path renders via dabs/render_ddl.py.
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
    """The README must document the standalone `python -m dabs.render_ddl | psql` path, and must
    not present an external migration-engine invocation in its place."""
    section = _engines_section()
    assert "python -m dabs.render_ddl" in section, "README missing the standalone renderer CLI"
    assert "alembic upgrade head --sql" not in README, (
        "README shows an external migration-engine path instead of the renderer CLI"
    )


def test_design_notes_states_no_version_table():
    """DESIGN-NOTES must state the DABs path keeps NO version table (that is what makes re-apply a
    clean reconciling no-op) — not that it is 'gated' or tracked by a version table."""
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


# --- Ticket 08: Docs tell the truth invariants --------------------------------------------------

def _load_doc_texts() -> dict[str, str]:
    """Read the customer-facing documentation files fresh from disk."""
    files = [
        "README.md",
        "RUNBOOK.md",
        "CONTRIBUTING.md",
        "Makefile",
        "config/README.md",
        "docs/DESIGN-NOTES.md",
        "docs/TROUBLESHOOTING.md",
        "docs/adr/0001-view-based-no-superuser-access.md",
        ".github/workflows/ci.yml",
    ]
    return {rel: (REPO / rel).read_text() for rel in files}


def test_no_doc_instructs_hand_editing_generated_changelogs():
    """D03: No doc may instruct users to hand-edit generated changelogs (deploy.sh and CI run
    generate_changelogs.py --check and abort on any hand-edit drift).

    Hostile check: inspects README.md, config/README.md, docs/DESIGN-NOTES.md, docs/TROUBLESHOOTING.md,
    and CONTRIBUTING.md for any positive instruction to edit generated changelog files.
    """
    docs = _load_doc_texts()
    banned_phrases = (
        "edits its generated changelog",
        "edit its generated changelog",
        "edit the generated changelog",
    )
    offenders = {}
    for rel, text in docs.items():
        lower = text.lower()
        for phrase in banned_phrases:
            if phrase in lower:
                offenders[rel] = phrase
    assert not offenders, f"D03 contradiction: docs instruct hand-editing generated changelogs: {offenders}"


def test_troubleshooting_does_not_make_false_ownership_or_seed_claims():
    """B23 (Trap 1) & D04: TROUBLESHOOTING.md must not claim 'you can't grant on what you don't own'
    (the creator holds WITH GRANT OPTION) or that seed_source.sh 'seeds a single demo source table'
    / 'does not read config/tables.json'."""
    ts = _load_doc_texts()["docs/TROUBLESHOOTING.md"]
    lower = ts.lower()
    assert "can't grant on what you don't own" not in lower, (
        "Trap 1 / B23: synced table creator holds SELECT WITH GRANT OPTION"
    )
    assert "cannot grant on what you do not own" not in lower
    assert "with grant option" in lower, (
        "TROUBLESHOOTING.md must explain that the synced table creator holds SELECT WITH GRANT OPTION"
    )
    assert "does not read `config/tables.json`" not in ts and "does not read config/tables.json" not in lower, (
        "D04: seed_source.sh reads config/tables.json"
    )
    assert "seeds a single demo source table" not in lower, (
        "D04: seed_source.sh seeds one demo source table per entry in config/tables.json"
    )


def test_readme_and_design_notes_warn_against_security_invoker():
    """Trap 9: Both README.md and docs/DESIGN-NOTES.md must explicitly warn not to set
    security_invoker on the consumer view."""
    docs = _load_doc_texts()
    for rel in ("README.md", "docs/DESIGN-NOTES.md"):
        text = docs[rel].lower()
        assert "security_invoker" in text, f"Trap 9: {rel} must warn against setting security_invoker on the consumer view"


def test_role_guard_sql_described_as_existence_check_not_create_role():
    """Ticket 09 + 08: role_guard_sql / 001-app-role checks pg_roles and raises an exception if the
    admin-created role is missing; it never runs CREATE ROLE."""
    docs = _load_doc_texts()
    for rel in (
        "README.md",
        "RUNBOOK.md",
        "docs/DESIGN-NOTES.md",
        "docs/adr/0001-view-based-no-superuser-access.md",
    ):
        text = docs[rel]
        lower = text.lower()
        assert "creates it with a guarded `create role`" not in lower, f"{rel} still claims job creates role"
        assert "idempotent `create role` via a `pg_roles` existence guard" not in lower, (
            f"{rel} parity table still claims role_guard_sql runs CREATE ROLE"
        )
        assert "guarded `create role`" not in lower, f"{rel} still claims a guarded CREATE ROLE"
        assert "migrate fails at `create role`" not in lower, f"{rel} still claims migrate fails at CREATE ROLE"
    # Parity table in README must describe role_guard_sql as an existence check
    section = _engines_section().lower()
    assert "role_guard_sql" in section and "does not exist" in section, (
        "README parity table must state role_guard_sql checks pg_roles and fails if the role does not exist"
    )


def test_no_doc_claims_not_even_as_a_superuser_or_unqualified_single_transaction():
    """B26, B18, B12, B24: No doc may overclaim 'not even as a superuser' / 'even as a superuser' /
    'not even by a superuser', 'This is automatic and cannot be turned off', 'Every claim the README
    makes is anchored to a test or a live run', or 'the single-transaction reconcile both paths use'."""
    docs = _load_doc_texts()
    banned = (
        "not even as a superuser",
        "even as a superuser",
        "not even by a superuser",
        "this is automatic and cannot be turned off",
        "every claim the readme makes is anchored to a test or a live run",
        "the single-transaction reconcile both paths use",
    )
    offenders = {}
    for rel, text in docs.items():
        lower = text.lower()
        for phrase in banned:
            if phrase in lower:
                offenders[(rel, phrase)] = True
    assert not offenders, f"Overclaims still present in docs: {list(offenders.keys())}"


def test_ci_and_org_repo_disclosures_are_accurate():
    """C01-C05, C10, M1, M2: README, CONTRIBUTING, RUNBOOK, and ci.yml must state the org-repo
    GitHub Actions status, reference-workflow status, and accurate CLI version history."""
    docs = _load_doc_texts()
    for rel in ("README.md", ".github/workflows/ci.yml"):
        assert "drop the resource `role_id`" not in docs[rel] and "drop the resource role_id" not in docs[rel], (
            f"C10: unproven role_id claim still in {rel}"
        )
        assert "Older CLIs do not\nsupport the `postgres_synced_tables` resource" not in docs[rel], (
            f"C10: inaccurate CLI claim still in {rel}"
        )
    assert "databricks-solutions" in docs["README.md"] and "databricks-solutions" in docs["CONTRIBUTING.md"], (
        "C01/C03/M1: README and CONTRIBUTING must disclose org repo Actions/scanning defaults"
    )
    assert "GitHub Actions | run that same sequence on push / PR" not in docs["RUNBOOK.md"], (
        "C05: RUNBOOK must not claim GitHub Actions runs deploy.sh on push/PR"
    )


def test_readme_make_targets_match_makefile_help():
    """README's documented make targets must match the actual targets in Makefile."""
    import re
    import subprocess

    docs = _load_doc_texts()
    makefile_targets = set(
        re.findall(r"^([a-zA-Z_-]+):.*?## ", docs["Makefile"], flags=re.MULTILINE)
    )
    # Hostile check: verify against real `make help` output (strip ANSI escapes) as well as static regex
    raw_out = subprocess.check_output(["make", "-s", "help"], cwd=REPO, text=True)
    clean_out = re.sub(r"\x1b\[[0-9;]*m", "", raw_out)
    help_targets = set(re.findall(r"^\s+([a-zA-Z_-]+)\s+", clean_out, flags=re.MULTILINE)) - {"Config", "Env"}
    assert makefile_targets == help_targets == {"help", "validate", "fmt", "seed", "deploy", "branch"}
    # README repo layout line must list all make targets
    assert "make help | validate | fmt | seed | deploy | branch" in docs["README.md"], (
        "README must list all 6 Makefile targets (help | validate | fmt | seed | deploy | branch)"
    )
    # D11: plural demo source tables in Makefile, README, RUNBOOK
    assert "demo Delta source tables" in docs["Makefile"], "D11: Makefile seed description must be plural"
    assert "demo Delta source table " not in docs["README.md"], "D11: README must use plural demo Delta source tables"
    assert "demo Delta source table " not in docs["RUNBOOK.md"], "D11: RUNBOOK must use plural demo Delta source tables"

