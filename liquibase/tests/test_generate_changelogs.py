"""Falsifiability tests — codegen: config/tables.json -> one Liquibase changelog PER table.

The bug this generator fixes (proven live on FEVM, see .scratch repro): the old path ran ONE
shared changelog (`db.changelog-master.xml`) once per table via property substitution
(`-Dsynced_table=…`). Liquibase keys a changeset by (FILENAME, id, author) and folds the
substituted values into the checksum, so two tables in the SAME app_schema shared one
DATABASECHANGELOG and the second table tripped ValidationFailedException on `001-app-role` — its
role/grants were never created. It also capped index columns at 2 (the deploy.sh two-slot hack).

The fix: emit one changelog file per config row. A per-table FILE gives each changeset a UNIQUE
identity (distinct FILENAME) even when the tables share a schema and one DATABASECHANGELOG, so
there is no cross-table checksum collision — and index changesets come straight from
`index_columns` (0/1/N, no cap).

We assert on the EMITTED changelog text (files written to a tmp dir, then re-parsed) and on the
generator's public surface — never on private helper names — so the tests survive refactors and
catch real regressions.

The fixture (`fixtures/tables.json`) is HOSTILE by construction:
  - `members` (2 index cols) and `claims` (3 index cols) SHARE app_schema "shared_schema" with
    DISTINCT roles — this is the exact live-broken case; it must yield DISTINCT changeset
    identities (the regression guard).
  - `claims` carries THREE index columns — a 2-slot cap silently drops the third.
  - `providers` has ONE index column; `events` has ZERO — 0/1/N all exercised.
"""

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIXTURE = HERE / "fixtures" / "tables.json"
GEN_PATH = HERE.parent / "generate_changelogs.py"
REPO_ROOT = HERE.parent.parent
REAL_CONFIG = REPO_ROOT / "config" / "tables.json"
COMMITTED_GENERATED = REPO_ROOT / "liquibase" / "generated"


def _load_gen():
    """Import the generator from its file path (no `liquibase` package import — the repo dir is a
    namespace-package name and we refuse to depend on that ambiguity)."""
    spec = importlib.util.spec_from_file_location("lb_generate_changelogs", GEN_PATH)
    module = importlib.util.module_from_spec(spec)
    # Register before exec so the module's @dataclass can resolve its own annotations
    # (dataclasses looks the defining module up in sys.modules).
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gen = _load_gen()


def _fixture_rows() -> list:
    return json.loads(FIXTURE.read_text())


# --- a tiny, output-only parser for a Liquibase formatted-SQL changelog ------------------------

_CHANGESET_RE = re.compile(r"^--changeset\s+(?P<author>[^:\s]+):(?P<id>\S+)(?P<attrs>[^\n]*)$", re.M)


def _parse_changesets(text: str) -> list[dict]:
    """Return [{author, id, attrs, body}] for a formatted-SQL changelog, splitting on --changeset."""
    matches = list(_CHANGESET_RE.finditer(text))
    out = []
    for i, m in enumerate(matches):
        body_start = m.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        out.append(
            {
                "author": m.group("author"),
                "id": m.group("id"),
                "attrs": m.group("attrs").strip(),
                "body": text[body_start:body_end],
            }
        )
    return out


def _generate(tmp_path) -> dict:
    """Generate from the hostile fixture into tmp_path; re-read each changelog file.

    Returns {by_name: {name: {file, text, changesets}}, files: [Path]}.
    """
    tables = gen.load_tables(FIXTURE)
    written = gen.write_changelogs(tables, tmp_path)
    by_name = {}
    for row in tables:
        fname = gen.changelog_filename(row)
        path = Path(tmp_path) / fname
        assert path.exists(), f"expected changelog {fname} for {row['name']} was not written"
        text = path.read_text()
        by_name[row["name"]] = {"file": fname, "text": text, "changesets": _parse_changesets(text)}
    return {"by_name": by_name, "files": [Path(p) for p in written]}


def _identity_set(entry: dict) -> set:
    """The (FILENAME, author, id) identity tuples for one table's changelog — mirrors how Liquibase
    keys DATABASECHANGELOG rows. The FILENAME (the per-table changelog file) is what makes shared
    (author, id) pairs distinct across tables."""
    return {(entry["file"], cs["author"], cs["id"]) for cs in entry["changesets"]}


# --- 1. one changelog per config row -----------------------------------------------------------


def test_one_changelog_file_per_config_row(tmp_path):
    """N rows -> exactly N changelog files, one per table.

    A lazy impl that writes a single shared changelog for all tables fails the count.
    """
    out = _generate(tmp_path)
    n = len(_fixture_rows())
    files = sorted(p.name for p in Path(tmp_path).glob("*.changelog.sql"))
    assert len(files) == n, f"expected {n} changelog files, got {files}"
    assert len(out["by_name"]) == n


# --- 2. THE regression guard: shared schema -> DISTINCT changeset identities -------------------


def test_shared_schema_tables_get_distinct_changeset_identities(tmp_path):
    """`members` and `claims` share app_schema 'shared_schema' but must have DISTINCT changeset
    identities so they never collide in one DATABASECHANGELOG (the live FEVM bug).

    A lazy impl that writes both tables into ONE shared changelog file gives them the SAME FILENAME,
    so their identical (author, id) pairs (e.g. cicd:001-app-role) collide -> this goes red.
    """
    out = _generate(tmp_path)
    rows = {r["name"]: r for r in _fixture_rows()}
    assert rows["members"]["app_schema"] == rows["claims"]["app_schema"] == "shared_schema", (
        "fixture regression: members and claims must share a schema"
    )

    members_ids = _identity_set(out["by_name"]["members"])
    claims_ids = _identity_set(out["by_name"]["claims"])

    # No identity is shared between the two same-schema tables.
    assert members_ids.isdisjoint(claims_ids), (
        f"shared-schema tables collided on changeset identities: "
        f"{members_ids & claims_ids} (are both tables in one changelog file?)"
    )

    # And specifically: both DO carry a `001-app-role` changeset with the SAME (author, id) —
    # the distinctness comes ONLY from the per-table FILENAME (exactly the fix).
    def role_cs(entry):
        return next(cs for cs in entry["changesets"] if cs["id"] == "001-app-role")

    m_role, c_role = role_cs(out["by_name"]["members"]), role_cs(out["by_name"]["claims"])
    assert (m_role["author"], m_role["id"]) == (c_role["author"], c_role["id"]), (
        "fixture/impl regression: both tables should reuse the same changeset id 001-app-role"
    )
    assert out["by_name"]["members"]["file"] != out["by_name"]["claims"]["file"], (
        "the two shared-schema tables MUST live in different changelog files (that is the fix)"
    )


def test_public_changeset_identities_match_written_files(tmp_path):
    """The generator's `changeset_identities(table)` matches the identities parsed from the file it
    writes for that table — so downstream tooling can reason about identity without parsing SQL.
    """
    tables = gen.load_tables(FIXTURE)
    gen.write_changelogs(tables, tmp_path)
    for row in tables:
        text = (Path(tmp_path) / gen.changelog_filename(row)).read_text()
        parsed = {(gen.changelog_filename(row), cs["author"], cs["id"]) for cs in _parse_changesets(text)}
        assert set(gen.changeset_identities(row)) == parsed, (
            f"changeset_identities({row['name']}) disagrees with the written changelog"
        )


# --- 3. index changesets = N from index_columns (kills the 2-slot cap) -------------------------


def _index_changesets(entry: dict) -> list[dict]:
    return [cs for cs in entry["changesets"] if cs["id"].startswith("003-index")]


def test_index_changeset_count_matches_index_columns(tmp_path):
    """Each table emits exactly len(index_columns) index changesets: 0/1/2/3 all exercised.

    The 3-column `claims` table is the cap-killer: a 2-slot workaround emits only 2 -> red.
    """
    out = _generate(tmp_path)
    expected = {"members": 2, "claims": 3, "providers": 1, "events": 0}
    for row in _fixture_rows():
        n = len(_index_changesets(out["by_name"][row["name"]]))
        assert n == expected[row["name"]] == len(row["index_columns"]), (
            f"{row['name']}: expected {len(row['index_columns'])} index changesets, got {n}"
        )


def test_three_column_table_indexes_all_three_columns(tmp_path):
    """`claims` (3 index cols) creates an index on EACH of member_id, provider_id, service_date.

    Guards the 2-slot cap directly: service_date (the 3rd) must appear.
    """
    out = _generate(tmp_path)
    claims = out["by_name"]["claims"]
    pg_table = "claims_synced"  # last dotted part of cat_a.shared_schema.claims_synced
    for col in ["member_id", "provider_id", "service_date"]:
        assert re.search(
            rf"CREATE INDEX IF NOT EXISTS\s+idx_{pg_table}_{col}\b.*\({col}\)",
            claims["text"],
        ), f"claims changelog missing an index on {col}:\n{claims['text']}"


def test_zero_index_column_table_emits_no_index_changeset(tmp_path):
    """`events` has index_columns == [] -> zero index changesets (an empty list must not emit one)."""
    out = _generate(tmp_path)
    assert _index_changesets(out["by_name"]["events"]) == []
    assert "CREATE INDEX" not in out["by_name"]["events"]["text"]


# --- 4. idempotency / runAlways / baked values -------------------------------------------------


def test_changesets_are_idempotent_and_runalways(tmp_path):
    """Grants, indexes and the view are runAlways (reapplied after a synced-table replace); the role
    is guarded; the view is CREATE OR REPLACE; indexes are CREATE INDEX IF NOT EXISTS."""
    out = _generate(tmp_path)
    m = out["by_name"]["members"]
    cs = {c["id"]: c for c in m["changesets"]}

    assert "runAlways:true" in cs["002-app-grants"]["attrs"]
    assert "runAlways:true" in cs["004-app-view"]["attrs"]
    for c in _index_changesets(m):
        assert "runAlways:true" in c["attrs"], f"index changeset {c['id']} must be runAlways"

    assert "IF NOT EXISTS" in cs["001-app-role"]["body"] and "CREATE ROLE" in cs["001-app-role"]["body"]
    assert "CREATE OR REPLACE VIEW" in cs["004-app-view"]["body"]
    assert "CREATE INDEX IF NOT EXISTS" in m["text"]


def test_generated_changelog_has_no_property_placeholders(tmp_path):
    """Values are BAKED per table — the generated changelog carries NO `${...}` Liquibase property
    placeholders. Property substitution is exactly the checksum-folding mechanism that caused the
    shared-schema collision; its absence is the structural proof the fix removed that path.
    """
    out = _generate(tmp_path)
    for name, entry in out["by_name"].items():
        assert "${" not in entry["text"], f"{name} changelog still contains a ${{...}} placeholder"


def test_role_schema_table_values_are_baked_per_table(tmp_path):
    """Each table's own role / schema / pg-table name appear literally in ITS changelog."""
    out = _generate(tmp_path)
    for row in _fixture_rows():
        text = out["by_name"][row["name"]]["text"]
        pg_table = row["synced_table_id"].split(".")[-1]
        assert row["app_role"] in text, f"{row['name']}: app_role not baked"
        assert row["app_schema"] in text, f"{row['name']}: app_schema not baked"
        assert pg_table in text, f"{row['name']}: pg table name {pg_table} not baked"
        # consumer view over the base synced table, granted to the app role
        assert f"{row['app_schema']}.{pg_table}_v" in text, f"{row['name']}: consumer view missing"


# --- 5. identifier-validation seam (fix D's future home — structural only for now) -------------


def test_identifier_validation_seam_exists_and_rejects_empty():
    """A single identifier-validation home exists in the generator (the future home for fix D's
    strict rules). For fix A it need only be a real, called seam that rejects an empty/None
    identifier so malformed config fails loudly rather than emitting broken DDL."""
    assert hasattr(gen, "validate_identifier"), "generator must expose validate_identifier (fix D seam)"
    for bad in ["", None]:
        try:
            gen.validate_identifier(bad, "app_role")
        except (ValueError, TypeError):
            continue
        raise AssertionError(f"validate_identifier accepted a bad identifier: {bad!r}")
    # a normal identifier passes through
    assert gen.validate_identifier("members_app_ro", "app_role") == "members_app_ro"


def test_malformed_config_is_rejected(tmp_path):
    """A row with an empty app_role must fail generation via the validation seam (hostile input)."""
    rows = _fixture_rows()
    rows[0]["app_role"] = ""
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(rows))
    try:
        gen.write_changelogs(gen.load_tables(bad), tmp_path / "out")
    except (ValueError, TypeError):
        return
    raise AssertionError("generation accepted a row with an empty app_role")


# --- 6. drift check: committed generated changelogs must match a fresh generation --------------


def test_committed_changelogs_match_fresh_generation():
    """The real committed liquibase/generated/*.changelog.sql byte-match a fresh generation from the
    real config. GREEN == in sync; editing config without regenerating turns this red."""
    problems = gen.check_drift(REAL_CONFIG, COMMITTED_GENERATED)
    assert problems == [], f"committed liquibase/generated drifted from config/tables.json: {problems}"


def _copy_committed(dst: Path) -> Path:
    dst.mkdir(parents=True, exist_ok=True)
    for f in COMMITTED_GENERATED.glob("*.changelog.sql"):
        (dst / f.name).write_text(f.read_text())
    return dst


def test_drift_check_detects_stale_committed_file(tmp_path):
    """HOSTILE: a committed changelog with one edited value must make the drift check go RED."""
    subject = _copy_committed(tmp_path / "generated")
    victim = subject / "members.changelog.sql"
    text = victim.read_text()
    assert "members_app_ro" in text, "fixture regression: expected role name in committed members changelog"
    victim.write_text(text.replace("members_app_ro", "members_app_TAMPERED"))
    problems = gen.check_drift(REAL_CONFIG, subject)
    assert problems and any("members.changelog.sql" in p for p in problems), problems


def test_drift_check_detects_missing_committed_file(tmp_path):
    """HOSTILE: a deleted committed changelog must make the drift check go RED."""
    subject = _copy_committed(tmp_path / "generated")
    (subject / "members.changelog.sql").unlink()
    problems = gen.check_drift(REAL_CONFIG, subject)
    assert problems and any("members.changelog.sql" in p for p in problems), problems


def test_drift_check_flags_unexpected_committed_file(tmp_path):
    """HOSTILE: a committed changelog no config row generates must be flagged (stale leftover)."""
    subject = _copy_committed(tmp_path / "generated")
    (subject / "orphan.changelog.sql").write_text("--liquibase formatted sql\n")
    problems = gen.check_drift(REAL_CONFIG, subject)
    assert problems and any("orphan.changelog.sql" in p for p in problems), problems


def test_check_cli_exits_nonzero_on_drift_and_writes_nothing(tmp_path):
    """`--check` exits non-zero on drift and is verify-only (writes nothing)."""
    subject = _copy_committed(tmp_path / "generated")
    (subject / "members.changelog.sql").unlink()
    before = sorted(p.name for p in subject.glob("*.changelog.sql"))
    result = subprocess.run(
        [sys.executable, str(GEN_PATH), "--check", "--config", str(REAL_CONFIG), "--out", str(subject)],
        capture_output=True, text=True,
    )
    assert result.returncode != 0, f"--check exited 0 despite drift:\n{result.stdout}\n{result.stderr}"
    after = sorted(p.name for p in subject.glob("*.changelog.sql"))
    assert before == after, f"--check wrote/regenerated files (should be verify-only): {before} -> {after}"


def test_check_cli_exits_zero_when_in_sync():
    """`--check` against the committed generated dir exits 0 (in-sync happy path)."""
    result = subprocess.run(
        [sys.executable, str(GEN_PATH), "--check"],
        cwd=str(REPO_ROOT), capture_output=True, text=True,
    )
    assert result.returncode == 0, f"--check red on in-sync repo:\n{result.stdout}\n{result.stderr}"


def test_module_runs_as_cli_and_writes_one_changelog_per_row(tmp_path):
    """`python generate_changelogs.py --config … --out …` writes one changelog per config row."""
    result = subprocess.run(
        [sys.executable, str(GEN_PATH), "--config", str(REAL_CONFIG), "--out", str(tmp_path)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, f"CLI failed:\nSTDOUT:{result.stdout}\nSTDERR:{result.stderr}"
    written = sorted(p.name for p in tmp_path.glob("*.changelog.sql"))
    n = len(json.loads(REAL_CONFIG.read_text()))
    assert len(written) == n, f"expected {n} changelogs from repo config, got {written}"
