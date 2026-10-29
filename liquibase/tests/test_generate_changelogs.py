"""Falsifiability tests — codegen: config/tables.json -> one Liquibase changelog PER table.

The bug this generator fixes (reproduced against a live Lakebase instance): the old path ran ONE
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

import pytest

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
    identities so they never collide in one DATABASECHANGELOG (the bug reproduced against a live Lakebase instance).

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
    """Role check, grants, indexes and the view are runAlways (reapplied on every deploy and after
    a synced-table replace); the role changeset checks `pg_roles` and raises the clear error
    `app role <name> does not exist; an admin must create it` (never `CREATE ROLE`); the view is
    CREATE OR REPLACE; indexes are CREATE INDEX IF NOT EXISTS."""
    out = _generate(tmp_path)
    m = out["by_name"]["members"]
    cs = {c["id"]: c for c in m["changesets"]}

    assert "runAlways:true" in cs["001-app-role"]["attrs"], cs["001-app-role"]["attrs"]
    assert "runAlways:true" in cs["002-app-grants"]["attrs"]
    assert "runAlways:true" in cs["004-app-view"]["attrs"]
    for c in _index_changesets(m):
        assert "runAlways:true" in c["attrs"], f"index changeset {c['id']} must be runAlways"

    role_body = cs["001-app-role"]["body"]
    active_role_sql = "\n".join(ln for ln in role_body.splitlines() if not ln.lstrip().startswith("--"))
    assert "IF NOT EXISTS" in role_body, role_body
    assert "RAISE EXCEPTION 'app role members_app_ro does not exist; an admin must create it';" in role_body, role_body
    assert not re.search(r"\bCREATE\s+ROLE\b", active_role_sql, re.IGNORECASE), (
        f"001-app-role must check the role, never CREATE ROLE:\n{role_body}"
    )
    assert not re.search(r"--rollback\s+DROP\s+ROLE\b", role_body, re.IGNORECASE), (
        f"001-app-role rollback must not DROP the admin-created role:\n{role_body}"
    )
    assert "DROP VIEW IF EXISTS" in cs["004-app-view"]["body"], (
        f"004-app-view must DROP VIEW IF EXISTS before CREATE so column changes succeed:\n{cs['004-app-view']['body']}"
    )
    assert "CREATE OR REPLACE VIEW" in cs["004-app-view"]["body"]
    assert cs["004-app-view"]["body"].index("DROP VIEW IF EXISTS") < cs["004-app-view"]["body"].index("CREATE OR REPLACE VIEW")
    assert "CREATE INDEX IF NOT EXISTS" in m["text"]


def _strip_quotes(ident: str) -> str:
    return ".".join(part.strip().strip('"').lower() for part in ident.split("."))


_GRANT_RE = re.compile(
    r"\bGRANT\s+(?P<priv>[A-Za-z_,\s]+?)\s+ON\s+(?:(?P<kind>SCHEMA|TABLE)\s+)?(?P<target>(?:\"[^\"]+\"|[A-Za-z_][A-Za-z0-9_]*)(?:\s*\.\s*(?:\"[^\"]+\"|[A-Za-z_][A-Za-z0-9_]*))?)\s+TO\s+(?P<role>\"[^\"]+\"|[A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE | re.DOTALL,
)

_REVOKE_RE = re.compile(
    r"\bREVOKE\s+(?P<priv>[A-Za-z_,\s]+?)\s+ON\s+(?:(?P<kind>SCHEMA|TABLE)\s+)?(?P<target>(?:\"[^\"]+\"|[A-Za-z_][A-Za-z0-9_]*)(?:\s*\.\s*(?:\"[^\"]+\"|[A-Za-z_][A-Za-z0-9_]*))?)\s+FROM\s+(?P<role>\"[^\"]+\"|[A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE | re.DOTALL,
)


def _extract_grants(sql: str) -> list[dict]:
    """Parse every GRANT statement (active SQL lines, ignoring `-- ` comments) into normalized
    {priv, kind, target, role} dicts."""
    active_lines = [
        ln for ln in sql.splitlines()
        if not ln.lstrip().startswith("--")
    ]
    active_sql = "\n".join(active_lines)
    grants = []
    for m in _GRANT_RE.finditer(active_sql):
        priv = " ".join(m.group("priv").upper().split())
        kind = (m.group("kind") or "").upper()
        target = _strip_quotes(re.sub(r"\s*\.\s*", ".", m.group("target")))
        role = _strip_quotes(m.group("role"))
        grants.append({"priv": priv, "kind": kind, "target": target, "role": role, "raw": m.group(0)})
    return grants


def _extract_revokes(sql: str, include_rollback_comments: bool = True) -> list[dict]:
    """Parse every REVOKE statement (including `--rollback REVOKE ...` lines when requested) into
    normalized {priv, kind, target, role} dicts."""
    lines = []
    for ln in sql.splitlines():
        stripped = ln.lstrip()
        if include_rollback_comments and stripped.lower().startswith("--rollback"):
            lines.append(re.sub(r"^--rollback\s+", "", stripped, flags=re.IGNORECASE))
        elif not stripped.startswith("--"):
            lines.append(ln)
    active_sql = "\n".join(lines)
    revokes = []
    for m in _REVOKE_RE.finditer(active_sql):
        priv = " ".join(m.group("priv").upper().split())
        kind = (m.group("kind") or "").upper()
        target = _strip_quotes(re.sub(r"\s*\.\s*", ".", m.group("target")))
        role = _strip_quotes(m.group("role"))
        revokes.append({"priv": priv, "kind": kind, "target": target, "role": role, "raw": m.group(0)})
    return revokes


def _assert_002_least_privilege(body: str, schema: str, role: str, name: str = "table") -> None:
    """Assert changeset 002 grants/revokes ONLY schema USAGE and zero SELECT grants/revokes."""
    grants = _extract_grants(body)
    revokes = _extract_revokes(body, include_rollback_comments=True)
    assert grants, f"{name}: 002 must contain at least one GRANT:\n{body}"
    for g in grants:
        assert "SELECT" not in g["priv"].split(), (
            f"{name}: 002 must not grant SELECT (least privilege), found {g['raw']!r} targeting {g['target']!r}"
        )
        assert g["priv"] == "USAGE" and g["kind"] == "SCHEMA" and g["target"] == schema.lower() and g["role"] == role.lower(), (
            f"{name}: 002 unexpected grant {g}"
        )
    assert revokes, f"{name}: 002 must contain at least one rollback REVOKE:\n{body}"
    for r in revokes:
        assert "SELECT" not in r["priv"].split(), (
            f"{name}: 002 rollback must not revoke SELECT on base table, found {r['raw']!r} targeting {r['target']!r}"
        )
        assert r["priv"] == "USAGE" and r["kind"] == "SCHEMA" and r["target"] == schema.lower() and r["role"] == role.lower(), (
            f"{name}: 002 unexpected rollback revoke {r}"
        )


def _assert_changelog_select_targets_only_view(text: str, expected_view_fqn: str, expected_role: str, name: str = "table") -> None:
    """Assert across the whole changelog that every GRANT SELECT or REVOKE SELECT targets ONLY
    `<app_schema>.<view_name>` (and zero base-table SELECT grants/revokes exist in any spelling)."""
    grants = _extract_grants(text)
    select_grants = [g for g in grants if "SELECT" in g["priv"].split()]
    assert len(select_grants) == 1, (
        f"{name}: expected exactly 1 GRANT SELECT (in 004 on {expected_view_fqn}), found {len(select_grants)}: {select_grants}"
    )
    for g in select_grants:
        assert g["kind"] != "TABLE", (
            f"{name}: GRANT SELECT must not use ON TABLE ({g['raw']!r})"
        )
        assert g["target"] == expected_view_fqn.lower(), (
            f"{name}: GRANT SELECT target {g['target']!r} != expected consumer view {expected_view_fqn.lower()!r} ({g['raw']!r})"
        )
        assert g["role"] == expected_role.lower(), (
            f"{name}: GRANT SELECT role {g['role']!r} != expected {expected_role.lower()!r}"
        )

    revokes = _extract_revokes(text, include_rollback_comments=True)
    select_revokes = [r for r in revokes if "SELECT" in r["priv"].split()]
    for r in select_revokes:
        assert r["kind"] != "TABLE" and r["target"] == expected_view_fqn.lower(), (
            f"{name}: REVOKE SELECT target {r['target']!r} (kind={r['kind']!r}) != consumer view {expected_view_fqn.lower()!r} ({r['raw']!r})"
        )


def test_grants_changeset_is_usage_only_no_base_table_select(tmp_path):
    """Least privilege: 002-app-grants grants schema USAGE only — NO SELECT on the base synced
    table (the app role reads only through the consumer view, whose owner holds the base-table
    privilege). Its rollback revokes USAGE only — there is no base-table grant to revoke.

    MUTATION GATE: re-add the base-table SELECT grant (and its REVOKE) in any SQL spelling and this goes RED.
    """
    out = _generate(tmp_path)
    rows_by_name = {r["name"]: r for r in _fixture_rows()}
    for name, entry in out["by_name"].items():
        row = rows_by_name[name]
        cs = {c["id"]: c for c in entry["changesets"]}
        body = cs["002-app-grants"]["body"]
        _assert_002_least_privilege(body, row["app_schema"], row["app_role"], name=name)


def test_only_view_select_is_granted_across_changelog(tmp_path):
    """Across a table's whole changelog, every `GRANT SELECT` (and any `REVOKE SELECT`) targets the
    resolved consumer view (`<app_schema>.<view_name>`) — never the base synced table in any
    spelling (with/without TABLE keyword, quoted identifiers, multiline/extra whitespace, lowercase).
    MUTATION GATE: re-add the base-table SELECT grant or rollback REVOKE and this goes RED."""
    out = _generate(tmp_path)
    rows_by_name = {r["name"]: r for r in _fixture_rows()}
    for name, entry in out["by_name"].items():
        row = rows_by_name[name]
        pg_tbl = gen.pg_table_name(row)
        view = gen.resolve_view_name(row, pg_tbl)
        expected_view_fqn = f"{row['app_schema']}.{view}"
        _assert_changelog_select_targets_only_view(
            entry["text"], expected_view_fqn, row["app_role"], name=name
        )


@pytest.mark.parametrize(
    "spelling_id,grant_sql",
    [
        ("3a_no_table_kw", "GRANT SELECT ON shared_schema.members_synced TO members_app_ro;"),
        ("3b_with_table_kw", "GRANT SELECT ON TABLE shared_schema.members_synced TO members_app_ro;"),
        ("3c_quoted_idents", 'GRANT SELECT ON "shared_schema"."members_synced" TO "members_app_ro";'),
        ("3d_multiline_ws", "GRANT   SELECT\n  ON\n  shared_schema.members_synced TO members_app_ro;"),
        ("3e_lowercase", "grant select on shared_schema.members_synced to members_app_ro;"),
    ],
)
def test_least_privilege_parser_catches_all_five_grant_select_spellings(tmp_path, spelling_id, grant_sql):
    """Attacker 3a-3e: injecting a base-table GRANT SELECT into changeset 002 in any of the 5 SQL
    spellings is caught by both _assert_002_least_privilege and _assert_changelog_select_targets_only_view."""
    out = _generate(tmp_path)
    entry = out["by_name"]["members"]
    cs_002 = next(c for c in entry["changesets"] if c["id"] == "002-app-grants")
    mutated_002 = cs_002["body"] + "\n" + grant_sql
    mutated_text = entry["text"] + "\n" + grant_sql

    with pytest.raises(AssertionError, match="SELECT"):
        _assert_002_least_privilege(mutated_002, "shared_schema", "members_app_ro", name=spelling_id)

    with pytest.raises(AssertionError, match="SELECT"):
        _assert_changelog_select_targets_only_view(
            mutated_text, "shared_schema.members_synced_v", "members_app_ro", name=spelling_id
        )


@pytest.mark.parametrize(
    "spelling_id,revoke_sql",
    [
        ("4a_no_table_kw", "--rollback REVOKE SELECT ON shared_schema.members_synced FROM members_app_ro;"),
        ("4b_with_table_kw", "--rollback REVOKE SELECT ON TABLE shared_schema.members_synced FROM members_app_ro;"),
        ("4c_quoted_idents", '--rollback REVOKE SELECT ON "shared_schema"."members_synced" FROM "members_app_ro";'),
        ("4d_multiline_ws", "REVOKE   SELECT\n  ON\n  shared_schema.members_synced FROM members_app_ro;"),
        ("4e_lowercase", "--rollback revoke select on shared_schema.members_synced from members_app_ro;"),
    ],
)
def test_least_privilege_parser_catches_all_five_revoke_select_spellings(tmp_path, spelling_id, revoke_sql):
    """Attacker 4a-4e: injecting a base-table REVOKE SELECT into changeset 002 rollback (or body)
    in any of the 5 SQL spellings is caught by both _assert_002_least_privilege and
    _assert_changelog_select_targets_only_view."""
    out = _generate(tmp_path)
    entry = out["by_name"]["members"]
    cs_002 = next(c for c in entry["changesets"] if c["id"] == "002-app-grants")
    mutated_002 = cs_002["body"] + "\n" + revoke_sql
    mutated_text = entry["text"] + "\n" + revoke_sql

    with pytest.raises(AssertionError, match="SELECT"):
        _assert_002_least_privilege(mutated_002, "shared_schema", "members_app_ro", name=spelling_id)

    with pytest.raises(AssertionError, match="SELECT"):
        _assert_changelog_select_targets_only_view(
            mutated_text, "shared_schema.members_synced_v", "members_app_ro", name=spelling_id
        )


def test_role_changeset_is_runonchange(tmp_path):
    """R3 & Attacker 4: the 001-app-role changeset MUST carry `runAlways:true runOnChange:true splitStatements:false`.

    `runAlways:true` ensures Liquibase checks `pg_roles` on every deploy (even if an admin dropped
    a role after deploy 1). `runOnChange:true` ensures a future rewording of the guard body or its
    comments re-runs cleanly instead of failing checksum validation on existing deploys.
    `splitStatements:false` sends the DO block as one statement. And its body must check the role
    via RAISE EXCEPTION (never CREATE ROLE) and its rollback must never DROP ROLE.

    MUTATION GATE: revert `001-app-role` to `CREATE ROLE` or drop `runAlways:true` / `runOnChange:true`
    and this goes RED.
    """
    out = _generate(tmp_path)
    rows_by_name = {r["name"]: r for r in _fixture_rows()}
    for name, entry in out["by_name"].items():
        row = rows_by_name[name]
        app_role = row["app_role"]
        cs = {c["id"]: c for c in entry["changesets"]}
        role = cs["001-app-role"]
        assert "runAlways:true" in role["attrs"], (
            f"{name}: 001-app-role must be runAlways:true so every deploy checks the role, got attrs={role['attrs']!r}"
        )
        assert "runOnChange:true" in role["attrs"], (
            f"{name}: 001-app-role must be runOnChange:true, got attrs={role['attrs']!r}"
        )
        assert "runOnChange:false" not in role["attrs"], (
            f"{name}: 001-app-role still carries runOnChange:false: {role['attrs']!r}"
        )
        # the DO block must still be sent as one statement
        assert "splitStatements:false" in role["attrs"], (
            f"{name}: 001-app-role must keep splitStatements:false: {role['attrs']!r}"
        )
        expected_err = f"RAISE EXCEPTION 'app role {app_role} does not exist; an admin must create it';"
        assert expected_err in role["body"], (
            f"{name}: 001-app-role missing exact error message {expected_err!r}:\n{role['body']}"
        )
        active_sql = "\n".join(ln for ln in role["body"].splitlines() if not ln.lstrip().startswith("--"))
        assert not re.search(r"\bCREATE\s+ROLE\b", active_sql, re.IGNORECASE), (
            f"{name}: 001-app-role must not CREATE ROLE:\n{role['body']}"
        )
        assert not re.search(r"--rollback\s+DROP\s+ROLE\b", role["body"], re.IGNORECASE), (
            f"{name}: 001-app-role rollback must not DROP ROLE:\n{role['body']}"
        )


def test_committed_role_changeset_is_runonchange():
    """R3 & Attacker 4/5, committed artifact: the checked-in generated changelogs carry
    `runAlways:true` and `runOnChange:true` on `001-app-role`, emit the exact `RAISE EXCEPTION`
    message, and contain no `CREATE ROLE` or `DROP ROLE`."""
    role_entries = []
    for f in sorted(COMMITTED_GENERATED.glob("*.changelog.sql")):
        for cs in _parse_changesets(f.read_text()):
            if cs["id"] == "001-app-role":
                role_entries.append((f.name, cs["attrs"], cs["body"]))
    assert role_entries, "no committed 001-app-role changeset found"
    for fname, attrs, body in role_entries:
        assert "runAlways:true" in attrs, f"{fname}: committed 001-app-role not runAlways:true ({attrs!r})"
        assert "runOnChange:true" in attrs, f"{fname}: committed 001-app-role not runOnChange:true ({attrs!r})"
        assert "does not exist; an admin must create it" in body, (
            f"{fname}: committed 001-app-role missing clear error message:\n{body}"
        )
        active_sql = "\n".join(ln for ln in body.splitlines() if not ln.lstrip().startswith("--"))
        assert not re.search(r"\bCREATE\s+ROLE\b", active_sql, re.IGNORECASE), (
            f"{fname}: committed 001-app-role still contains CREATE ROLE:\n{body}"
        )
        assert not re.search(r"--rollback\s+DROP\s+ROLE\b", body, re.IGNORECASE), (
            f"{fname}: committed 001-app-role rollback still contains DROP ROLE:\n{body}"
        )


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


# --- 5. identifier-validation seam (the home for the strict identifier rules) -----------------


def test_identifier_validation_seam_exists_and_rejects_empty():
    """A single identifier-validation home exists in the generator (the home for the strict
    identifier rules). At minimum it must be a real, called seam that rejects an empty/None
    identifier so malformed config fails loudly rather than emitting broken DDL."""
    assert hasattr(gen, "validate_identifier"), "generator must expose validate_identifier (the identifier seam)"
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


# --- 5b. the SHARED seam rejects UNSAFE identifiers in the Liquibase path too ------------------
#
# validate_identifier is imported from dabs.render_ddl (ONE home), so tightening it there tightens
# BOTH paths at once. These prove the Liquibase generator refuses to bake a hyphenated / reserved /
# uppercase / over-63-char identifier into a changelog. Same reviewer mutations as the DABs suite.

HOSTILE_HYPHEN = "Plan-Code"
HOSTILE_RESERVED = "user"
HOSTILE_UPPER = "PlanCode"
HOSTILE_TOOLONG = "a" * 64


@pytest.mark.parametrize("bad", [HOSTILE_HYPHEN, HOSTILE_RESERVED, HOSTILE_UPPER, HOSTILE_TOOLONG])
def test_shared_seam_rejects_unsafe_identifier(bad):
    """The generator's validate_identifier (imported from dabs.render_ddl) rejects unsafe values and
    names the offending value + kind. MUTATION GATE: loosen the seam to empty-only => RED."""
    with pytest.raises(ValueError) as exc:
        gen.validate_identifier(bad, "app_role")
    msg = str(exc.value)
    assert bad in msg and "app_role" in msg, msg


@pytest.mark.parametrize("field,bad", [
    ("app_role", HOSTILE_RESERVED),
    ("app_schema", HOSTILE_HYPHEN),
    ("name", HOSTILE_UPPER),
    ("app_schema", HOSTILE_TOOLONG),
])
def test_generate_rejects_hostile_scalar_identifier(field, bad, tmp_path):
    """A hostile app_role / app_schema / name must fail changelog generation."""
    rows = _fixture_rows()
    rows[0][field] = bad
    bad_file = tmp_path / "hostile.json"
    bad_file.write_text(json.dumps(rows))
    with pytest.raises(ValueError):
        gen.write_changelogs(gen.load_tables(bad_file), tmp_path / "out")


def test_generate_rejects_hostile_index_column(tmp_path):
    """A hostile index column (`Plan-Code`) must be rejected — EACH index column is baked into a
    CREATE INDEX changeset and must pass through the shared seam."""
    rows = _fixture_rows()
    rows[0]["index_columns"] = ["member_id", HOSTILE_HYPHEN]
    bad_file = tmp_path / "hostile_idx.json"
    bad_file.write_text(json.dumps(rows))
    with pytest.raises(ValueError):
        gen.write_changelogs(gen.load_tables(bad_file), tmp_path / "out")


def test_valid_config_still_generates(tmp_path):
    """The shipped valid config still generates changelogs (strict seam rejects no real identifier)."""
    written = gen.write_changelogs(gen.load_tables(REAL_CONFIG), tmp_path / "out")
    assert written and all(p.read_text() for p in written)


# --- 5c. R2: reject DERIVED names > 63 chars in the Liquibase path too (shared builders) --------
#
# The generator wraps the SAME shared builders (index_statements / view_statements). Those builders
# EMIT derived names idx_<tbl>_<col> and <tbl>_v that can exceed 63 even when the table name and
# each column are individually legal. Postgres would silently truncate them; the verify step then
# looks for the untruncated name and fails a "successful" deploy. Rejecting in the shared builders
# means this path inherits the check for free — these prove it does.

def test_generate_rejects_overlong_derived_index_name(tmp_path):
    """Individually-legal table + index column whose derived idx_<tbl>_<col> exceeds 63 -> the
    Liquibase generator must refuse to bake the (would-be-truncated) index name into a changeset."""
    rows = _fixture_rows()
    long_tbl = "t" * 40
    long_col = "c" * 40
    rows[0]["synced_table_id"] = f"cat_a.shared_schema.{long_tbl}"
    rows[0]["index_columns"] = [long_col]
    assert gen.validate_identifier(long_tbl, "synced_table") == long_tbl
    assert gen.validate_identifier(long_col, "index_column") == long_col
    assert len(f"idx_{long_tbl}_{long_col}") > 63
    bad = tmp_path / "overlong_idx.json"
    bad.write_text(json.dumps(rows))
    with pytest.raises(ValueError) as exc:
        gen.write_changelogs(gen.load_tables(bad), tmp_path / "out")
    assert "63" in str(exc.value), f"error must cite the 63-char limit: {exc.value}"


def test_generate_rejects_overlong_derived_view_name(tmp_path):
    """Legal table name whose derived consumer view name <tbl>_v exceeds 63 -> generation must
    refuse (isolate the view path with no index columns)."""
    rows = _fixture_rows()
    long_tbl = "t" * 62  # <tbl>_v == 64
    rows[0]["synced_table_id"] = f"cat_a.shared_schema.{long_tbl}"
    rows[0]["index_columns"] = []
    assert gen.validate_identifier(long_tbl, "synced_table") == long_tbl
    assert len(f"{long_tbl}_v") > 63
    bad = tmp_path / "overlong_view.json"
    bad.write_text(json.dumps(rows))
    with pytest.raises(ValueError) as exc:
        gen.write_changelogs(gen.load_tables(bad), tmp_path / "out")
    assert "63" in str(exc.value), f"error must cite the 63-char limit: {exc.value}"


# --- 5d. pin changeset identity with a cwd-independent logicalFilePath -------------------------
#
# Liquibase derives a changeset's identity partly from the changelog FILENAME, which it resolves
# relative to cwd / --search-path. deploy.sh runs liquibase from `liquibase/` (filename
# `generated/<name>.changelog.sql`) while CI runs from repo-root — so the SAME changelog would get
# DIFFERENT identities from the two cwds, and an operator running from repo-root would re-execute
# changesets already applied. Emitting `logicalFilePath:generated/<name>.changelog.sql` in the
# formatted-SQL header pins the recorded filename regardless of cwd. The end-to-end STATE proof
# (DATABASECHANGELOG.filename equal from both cwds, second run a no-op) lives in
# liquibase/tests/docker_multitable_proof.sh; these assert the directive is emitted + committed.

def _expected_logical_path(row: dict) -> str:
    return f"logicalFilePath:generated/{gen.changelog_filename(row)}"


def test_generated_changelog_declares_logical_file_path(tmp_path):
    """Each generated changelog's formatted-SQL header carries a cwd-independent
    `logicalFilePath:generated/<name>.changelog.sql`, so its changeset identity is STABLE whether
    liquibase runs from `liquibase/` (deploy.sh) or repo-root (CI).

    MUTATION GATE: drop the logicalFilePath from the header and this goes RED.
    """
    out = _generate(tmp_path)
    for row in _fixture_rows():
        text = out["by_name"][row["name"]]["text"]
        first_line = text.splitlines()[0]
        assert first_line.startswith("--liquibase formatted sql"), first_line
        assert _expected_logical_path(row) in first_line, (
            f"{row['name']}: header missing {_expected_logical_path(row)!r}; got {first_line!r}"
        )


def test_committed_changelogs_declare_logical_file_path():
    """The checked-in generated changelogs carry the logicalFilePath directive (so the drift check
    and CI reflect the fix, not just a fresh generation)."""
    rows_by_file = {gen.changelog_filename(r): r for r in gen.load_tables(REAL_CONFIG)}
    committed = sorted(COMMITTED_GENERATED.glob("*.changelog.sql"))
    assert committed, "no committed generated changelogs found"
    for f in committed:
        row = rows_by_file[f.name]
        first_line = f.read_text().splitlines()[0]
        assert _expected_logical_path(row) in first_line, (
            f"{f.name}: committed header missing {_expected_logical_path(row)!r}; got {first_line!r}"
        )


# --- 5e. view_name: optional stable consumer-view name (same threading as the DABs path) --------
#
# view_name defaults to <synced_table>_v and an optional config `view_name` overrides it. The
# generator wraps the SAME shared builder + resolve_view_name from dabs.render_ddl, so the Liquibase
# path inherits the override (and the identifier seam) for free — these prove it does.

def test_view_name_override_appears_in_changelog(tmp_path):
    """A row's explicit `view_name` is baked into the 004 consumer-view changeset (and its rollback),
    not the derived <tbl>_v. MUTATION GATE: ignore the override and this goes RED."""
    rows = _fixture_rows()
    rows[1]["view_name"] = "claims_current"  # claims row (schema shared_schema, tbl claims_synced)
    cfg = tmp_path / "override.json"
    cfg.write_text(json.dumps(rows))
    gen.write_changelogs(gen.load_tables(cfg), tmp_path / "out")
    text = (tmp_path / "out" / "claims.changelog.sql").read_text()
    assert "CREATE OR REPLACE VIEW shared_schema.claims_current" in text, text
    assert "DROP VIEW IF EXISTS shared_schema.claims_current" in text, text
    assert "claims_synced_v" not in text, f"derived view name still emitted despite override:\n{text}"


def test_omitted_view_name_defaults_to_derived_v_in_changelog(tmp_path):
    """A row WITHOUT `view_name` still gets the derived <tbl>_v consumer view in its changelog."""
    rows = _fixture_rows()
    assert "view_name" not in rows[0], "fixture regression: members must omit view_name"
    cfg = tmp_path / "default.json"
    cfg.write_text(json.dumps(rows))
    gen.write_changelogs(gen.load_tables(cfg), tmp_path / "out")
    text = (tmp_path / "out" / "members.changelog.sql").read_text()
    assert "CREATE OR REPLACE VIEW shared_schema.members_synced_v" in text, text


def test_generate_rejects_overlong_view_name_override(tmp_path):
    """An overridden `view_name` over 63 chars is rejected by the Liquibase generator too."""
    rows = _fixture_rows()
    rows[0]["view_name"] = "v" * 64
    cfg = tmp_path / "overlong_view_name.json"
    cfg.write_text(json.dumps(rows))
    with pytest.raises(ValueError) as exc:
        gen.write_changelogs(gen.load_tables(cfg), tmp_path / "out")
    assert "63" in str(exc.value), f"error must cite the 63-char limit: {exc.value}"


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


# --- 5f. cross-entry view-name uniqueness (shared seam, inherited by the Liquibase path) ---------
#
# The Liquibase generator shares resolve_view_name + validate_view_names_unique from dabs.render_ddl,
# so two entries resolving to the SAME (app_schema, view_name) must fail changelog generation too —
# otherwise both changelogs would CREATE OR REPLACE VIEW the same schema.view and last-write-wins.

def test_duplicate_resolved_view_names_are_rejected(tmp_path):
    """Two entries resolving to the same (app_schema, view_name) fail changelog generation, naming
    BOTH entries + the colliding view. `alpha` derives members_v; `beta` overrides to members_v over
    a different base table in the same schema.

    MUTATION GATE: remove validate_view_names_unique (or its call) and this goes RED.
    """
    rows = [
        {"name": "alpha", "synced_table_id": "cat_a.shared_schema.members",
         "source_table_full_name": "cat_a.raw.members_src", "primary_key_columns": ["id"],
         "app_schema": "shared_schema", "app_role": "alpha_ro", "index_columns": []},
        {"name": "beta", "synced_table_id": "cat_a.shared_schema.orders",
         "source_table_full_name": "cat_a.raw.orders_src", "primary_key_columns": ["id"],
         "app_schema": "shared_schema", "app_role": "beta_ro", "index_columns": [],
         "view_name": "members_v"},
    ]
    cfg = tmp_path / "dup_view.json"
    cfg.write_text(json.dumps(rows))
    with pytest.raises(ValueError) as exc:
        gen.write_changelogs(gen.load_tables(cfg), tmp_path / "out")
    msg = str(exc.value)
    assert "alpha" in msg and "beta" in msg, f"error must name both colliding entries: {msg}"
    assert "members_v" in msg, f"error must name the colliding view: {msg}"


# --- 5g. cross-entry table-name uniqueness (exact + case-insensitive) ---------------------------
#
# Each table row's `name` drives the changelog filename (`<name>.changelog.sql`) and the DABs
# resource key (`<name>.yml` / `postgres_synced_tables.<name>`). Two rows with the same `name`
# (even across different schemas, or differing only in case) would silently overwrite the first
# changelog and collapse in `check_drift`'s dict keyed by filename. Both generators + `load_tables`
# must reject duplicate `name` values and name the duplicate.

def _dup_name_rows(second_name: str = "members") -> list[dict]:
    """Two config rows with the same (or case-colliding) `name` in different schemas."""
    return [
        {
            "name": "members",
            "synced_table_id": "cat_a.s1.members_a",
            "source_table_full_name": "cat_a.raw.members_a_src",
            "primary_key_columns": ["id"],
            "app_schema": "s1",
            "app_role": "members_s1_ro",
            "index_columns": [],
        },
        {
            "name": second_name,
            "synced_table_id": "cat_a.s2.members_b",
            "source_table_full_name": "cat_a.raw.members_b_src",
            "primary_key_columns": ["id"],
            "app_schema": "s2",
            "app_role": "members_s2_ro",
            "index_columns": [],
        },
    ]


@pytest.mark.parametrize("second_name", ["members", "MEMBERS", "Members"])
def test_duplicate_table_names_rejected_in_write_changelogs_and_load_tables(tmp_path, second_name):
    """Attacker 1 & 2: Two rows with the same `name` (exact or case-insensitive) and different
    schemas are rejected by `write_changelogs` and `load_tables`, naming the duplicate as a
    duplicate table name."""
    rows = _dup_name_rows(second_name)
    with pytest.raises(ValueError) as exc_write:
        gen.write_changelogs(rows, tmp_path / "out")
    msg_write = str(exc_write.value).lower()
    assert "members" in msg_write and "duplicate" in msg_write, (
        f"write_changelogs error must name the duplicate 'members': {exc_write.value}"
    )

    cfg = tmp_path / "dup_name.json"
    cfg.write_text(json.dumps(rows))
    with pytest.raises(ValueError) as exc_load:
        gen.load_tables(cfg)
    msg_load = str(exc_load.value).lower()
    assert "members" in msg_load and "duplicate" in msg_load, (
        f"load_tables error must name the duplicate 'members': {exc_load.value}"
    )


@pytest.mark.parametrize("second_name", ["members", "MEMBERS", "Members"])
def test_duplicate_table_names_rejected_in_check_drift_and_cli(tmp_path, second_name):
    """Attacker 1 & 2: `check_drift` and `generate_changelogs.py --check` reject a config with
    duplicate `name` (exact or case-insensitive) instead of collapsing the dict by filename."""
    rows = _dup_name_rows(second_name)
    cfg = tmp_path / "dup_name.json"
    cfg.write_text(json.dumps(rows))

    with pytest.raises(ValueError) as exc_drift:
        gen.check_drift(cfg, COMMITTED_GENERATED)
    msg_drift = str(exc_drift.value).lower()
    assert "members" in msg_drift and "duplicate" in msg_drift, (
        f"check_drift error must name the duplicate 'members': {exc_drift.value}"
    )

    res_check = subprocess.run(
        [sys.executable, str(GEN_PATH), "--check", "--config", str(cfg), "--out", str(COMMITTED_GENERATED)],
        capture_output=True, text=True,
    )
    assert res_check.returncode != 0, f"--check exited 0 on duplicate name {second_name!r}"
    out_check = (res_check.stderr + res_check.stdout).lower()
    assert "members" in out_check and "duplicate" in out_check, (
        f"--check output did not name duplicate 'members':\n{res_check.stderr}\n{res_check.stdout}"
    )

    res_gen = subprocess.run(
        [sys.executable, str(GEN_PATH), "--config", str(cfg), "--out", str(tmp_path / "gen_out")],
        capture_output=True, text=True,
    )
    assert res_gen.returncode != 0, f"generate CLI exited 0 on duplicate name {second_name!r}"
    out_gen = (res_gen.stderr + res_gen.stdout).lower()
    assert "members" in out_gen and "duplicate" in out_gen, (
        f"generate CLI output did not name duplicate 'members':\n{res_gen.stderr}\n{res_gen.stdout}"
    )



# --- Write mode removes the generated changelogs of entries that are no longer in the config -------
#
# What this protects: a customer who renames or removes the example tables and then runs the
# Terraform + Liquibase deploy. What must never happen: the old entry's changelog stays in
# liquibase/generated/ and a stale changelog runs against the database, or a hand-written file
# in that folder is deleted by the cleanup.
# Attacker list (minimum): 1. a renamed entry's old changelog is gone and the folder passes --check;
# 2. a hand-written *.changelog.sql is kept and reported; 3. a symlink is not followed or deleted;
# 4. a file that only quotes the marker later is not "generated"; 5. --check deletes nothing.


def _renamed_config(tmp_path: Path) -> Path:
    rows = json.loads(REAL_CONFIG.read_text())
    for r in rows:
        r["name"] = r["name"] + "_indep"
    cfg = tmp_path / "renamed_tables.json"
    cfg.write_text(json.dumps(rows))
    return cfg


def _run_gen(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(GEN_PATH), *args], capture_output=True, text=True)


def test_renamed_entries_leave_no_stale_changelogs_and_pass_check(tmp_path):
    out = _copy_committed(tmp_path / "generated")
    assert (out / "members.changelog.sql").is_file()
    cfg = _renamed_config(tmp_path)

    r = _run_gen("--config", str(cfg), "--out", str(out))
    assert r.returncode == 0, r.stderr

    names = sorted(p.name for p in out.glob("*.changelog.sql"))
    assert "members.changelog.sql" not in names and "providers.changelog.sql" not in names, names
    assert "members_indep.changelog.sql" in names and "providers_indep.changelog.sql" in names, names
    assert "members.changelog.sql" in r.stdout, "the removal must be reported, not silent"
    chk = _run_gen("--check", "--config", str(cfg), "--out", str(out))
    assert chk.returncode == 0, chk.stderr


def test_write_keeps_a_hand_written_changelog_and_check_still_flags_it(tmp_path):
    out = _copy_committed(tmp_path / "generated")
    mine = out / "notes.changelog.sql"
    mine.write_text("--liquibase formatted sql\n-- my own changelog\n")

    r = _run_gen("--config", str(REAL_CONFIG), "--out", str(out))
    assert r.returncode == 0, r.stderr
    assert mine.read_text() == "--liquibase formatted sql\n-- my own changelog\n", "a hand-written file was deleted"
    assert "notes.changelog.sql" in (r.stdout + r.stderr), "the kept file must be reported"
    chk = _run_gen("--check", "--config", str(REAL_CONFIG), "--out", str(out))
    assert chk.returncode != 0 and "notes.changelog.sql" in chk.stderr, chk.stderr


def test_write_never_follows_or_deletes_a_changelog_symlink(tmp_path):
    out = _copy_committed(tmp_path / "generated")
    outside = tmp_path / "outside.changelog.sql"
    outside.write_text(gen._generated_header("outside"))
    link = out / "ghost.changelog.sql"
    link.symlink_to(outside)

    r = _run_gen("--config", str(REAL_CONFIG), "--out", str(out))
    assert r.returncode == 0, r.stderr
    assert outside.is_file(), "the file a symlink pointed at was deleted"
    assert link.is_symlink(), "a symlink was removed"


def test_a_changelog_that_only_quotes_the_marker_later_is_not_generated(tmp_path):
    out = _copy_committed(tmp_path / "generated")
    quoted = out / "quoted.changelog.sql"
    quoted.write_text("-- I copied the header below to explain it\n" + gen._generated_header("quoted"))

    r = _run_gen("--config", str(REAL_CONFIG), "--out", str(out))
    assert r.returncode == 0, r.stderr
    assert quoted.is_file(), "a file with the marker mid-file was deleted"


def test_check_mode_deletes_no_changelog(tmp_path):
    out = _copy_committed(tmp_path / "generated")
    stale = out / "old_entry.changelog.sql"
    stale.write_text(gen._generated_header("old_entry"))

    chk = _run_gen("--check", "--config", str(REAL_CONFIG), "--out", str(out))
    assert chk.returncode != 0 and "old_entry.changelog.sql" in chk.stderr, chk.stderr
    assert stale.is_file(), "--check removed a file; it must be verify-only"
