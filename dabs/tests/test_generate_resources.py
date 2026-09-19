"""Falsifiability tests for ticket 01 — codegen: config/tables.json -> dabs/resources YAML.

The single seam is the pure codegen `tables.json (list) -> bundle resource YAML`. We assert
on the EMITTED YAML (files written to a tmp dir, then re-parsed) — never on internal function
names — so the tests survive refactors and catch real regressions.

The fixture (`fixtures/tables.json`) is HOSTILE by construction. Each element exists only to
break a lazy implementation:
  - THREE tables (a one-table hardcode fails the count assertion).
  - `claims` carries 3 `index_columns` [member_id, provider_id, service_date] — a 2-column
    truncation silently drops `service_date`.
  - `claims` and `members` SHARE `app_schema` "shared_schema" but have DISTINCT `app_role`s —
    proves per-table role, not per-schema.
  - `claims.synced_table_id` (cat_a.shared_schema.claims_synced) differs non-trivially from
    `claims.source_table_full_name` (cat_b.raw_landing.claims_delta_source) — different catalog,
    schema, AND table — so a swap/echo of the two fields cannot pass.

Bundle-schema note: `postgres_synced_tables` and `postgres_roles` both set
`additionalProperties: false` (verified against `databricks bundle schema`, CLI v1.14.1), so the
table-specific migration metadata (`app_schema`, `index_columns`) has no native resource field
and is carried in a bundle `variables` complex default (`migration_targets`). That variable is
where index columns are asserted "carried through".
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
FIXTURE = HERE / "fixtures" / "tables.json"
GEN_MODULE_DIR = HERE.parent.parent  # repo root, so `import dabs.generate_resources` works
REPO_ROOT = GEN_MODULE_DIR
REAL_CONFIG = REPO_ROOT / "config" / "tables.json"
COMMITTED_RESOURCES = REPO_ROOT / "dabs" / "resources"


def _fixture_rows() -> list:
    return json.loads(FIXTURE.read_text())


def _generate(tmp_path) -> dict:
    """Run the generator against the hostile fixture, writing to tmp_path, then re-parse every
    emitted .yml file and merge it into a single observable view.

    Returns a dict with:
      synced:   {resource_key: synced_table_dict}
      roles:    {resource_key: role_dict}
      targets:  the migration_targets variable default (list of per-table metadata dicts)
      files:    list of Path written
    """
    from dabs.generate_resources import load_tables, write_resources

    tables = load_tables(FIXTURE)
    written = write_resources(tables, tmp_path)

    synced: dict = {}
    roles: dict = {}
    targets = None
    for f in sorted(tmp_path.glob("*.yml")):
        doc = yaml.safe_load(f.read_text())  # raises on invalid YAML -> covers the parse assertion
        assert isinstance(doc, dict), f"{f.name} did not parse to a mapping"
        res = doc.get("resources", {})
        synced.update(res.get("postgres_synced_tables", {}) or {})
        roles.update(res.get("postgres_roles", {}) or {})
        variables = doc.get("variables", {})
        if "migration_targets" in variables:
            targets = variables["migration_targets"]["default"]

    return {"synced": synced, "roles": roles, "targets": targets, "files": written}


def test_one_synced_table_and_one_role_per_row(tmp_path):
    """N rows -> exactly N postgres_synced_tables and N postgres_roles.

    Guards mutation 1: replacing the loop with "emit first table only" drops the count to 1 and
    turns this red.
    """
    out = _generate(tmp_path)
    n = len(_fixture_rows())
    assert len(out["synced"]) == n, f"expected {n} synced tables, got {list(out['synced'])}"
    assert len(out["roles"]) == n, f"expected {n} roles, got {list(out['roles'])}"


def test_each_synced_table_carries_source_pk_and_id_unswapped(tmp_path):
    """Every row's synced_table_id, source_table_full_name and primary_key_columns appear on ITS
    own synced-table resource, and the two names are not swapped/echoed.

    Guards mutation 4: swapping synced_table_id and source_table_full_name in the emit turns this
    red (claims' two names differ in catalog, schema and table).
    """
    out = _generate(tmp_path)
    by_id = {st["synced_table_id"]: st for st in out["synced"].values()}
    for row in _fixture_rows():
        st = by_id.get(row["synced_table_id"])
        assert st is not None, (
            f"no synced table with synced_table_id={row['synced_table_id']}; "
            f"got {list(by_id)}"
        )
        assert st["source_table_full_name"] == row["source_table_full_name"], (
            f"source_table_full_name wrong for {row['name']}: "
            f"{st['source_table_full_name']!r} != {row['source_table_full_name']!r} "
            "(fields swapped/echoed?)"
        )
        assert st["primary_key_columns"] == row["primary_key_columns"], (
            f"primary_key_columns wrong for {row['name']}"
        )


def test_each_synced_table_carries_branch_var_reference(tmp_path):
    """Every emitted postgres_synced_tables resource carries `branch` == ${var.lakebase_branch}.

    The synced table must place on the SAME Lakebase branch the target selects (the var the role's
    `parent` already uses), or dev/prod targets can't separate synced-table placement and an
    ephemeral-branch test can't isolate. This is the load-bearing ticket-04 pre-flight finding.

    Guards the mutation: dropping the `branch` emission from build_synced_table_resource() makes
    `branch` missing -> red. A literal branch path (e.g. "projects/…/branches/…") instead of the
    var reference is ALSO rejected (a committed literal would leak a workspace-specific value).
    """
    from dabs.generate_resources import BRANCH_VAR

    out = _generate(tmp_path)
    assert out["synced"], "generator emitted no synced tables"
    for key, st in out["synced"].items():
        assert "branch" in st, f"synced table {key!r} has no `branch` field: {sorted(st)}"
        assert st["branch"] == BRANCH_VAR, (
            f"synced table {key!r} branch is {st['branch']!r}, expected the var reference "
            f"{BRANCH_VAR!r} (not a literal, not missing)"
        )
        # Hostile: reject a hardcoded literal branch path masquerading as placement.
        assert st["branch"].startswith("${var."), (
            f"synced table {key!r} branch {st['branch']!r} is not a bundle variable reference — "
            "a literal branch path must never be committed"
        )
        assert "projects/" not in st["branch"], (
            f"synced table {key!r} branch {st['branch']!r} looks like a literal branch resource path"
        )


def test_synced_table_and_role_share_the_same_branch_var(tmp_path):
    """The synced table's `branch` and its role's `parent` reference the SAME var, so a target that
    sets ${var.lakebase_branch} moves BOTH the table and its role to that branch in lockstep.

    Guards a mutation that points the synced table at a different/new var than the role's parent.
    """
    out = _generate(tmp_path)
    role_parents = {r["parent"] for r in out["roles"].values()}
    table_branches = {st["branch"] for st in out["synced"].values()}
    assert role_parents == table_branches, (
        f"synced-table branch {table_branches} and role parent {role_parents} must be the same var"
    )


def test_each_role_carries_its_rows_app_role(tmp_path):
    """Every row's app_role is carried on a role resource as postgres_role.

    Guards mutation 2: hardcoding the role name means not every row's app_role is present -> red.
    """
    out = _generate(tmp_path)
    emitted_roles = {r["postgres_role"] for r in out["roles"].values()}
    for row in _fixture_rows():
        assert row["app_role"] in emitted_roles, (
            f"app_role {row['app_role']!r} for {row['name']} not carried; got {emitted_roles}"
        )


def test_shared_schema_still_gets_distinct_roles(tmp_path):
    """The two rows sharing app_schema 'shared_schema' still get DISTINCT per-table roles.

    Guards mutation 2: a per-schema (or hardcoded) role would collapse these to one -> red.
    """
    out = _generate(tmp_path)
    rows = _fixture_rows()
    shared = [r for r in rows if r["app_schema"] == "shared_schema"]
    assert len(shared) >= 2, "fixture regression: need >=2 rows sharing a schema"
    emitted_roles = {r["postgres_role"] for r in out["roles"].values()}
    expected = {r["app_role"] for r in shared}
    assert expected.issubset(emitted_roles), (
        f"shared-schema rows collapsed to one role; expected all of {expected}, got {emitted_roles}"
    )
    assert len(emitted_roles) == len(rows), (
        f"expected {len(rows)} distinct roles, got {len(emitted_roles)}: {emitted_roles}"
    )


def test_all_index_columns_carried_not_truncated(tmp_path):
    """Every index column of every row is carried in full — including the 3-column table.

    Guards mutation 3: truncating index handling to 2 columns drops 'service_date' -> red.
    """
    out = _generate(tmp_path)
    assert out["targets"] is not None, "migration_targets variable not emitted"
    by_name = {t["name"]: t for t in out["targets"]}
    for row in _fixture_rows():
        carried = by_name[row["name"]]["index_columns"]
        assert carried == row["index_columns"], (
            f"index_columns for {row['name']} not carried in full: "
            f"{carried} != {row['index_columns']}"
        )
    # Explicit guard on the 3-column table: all three, in order, present.
    assert by_name["claims"]["index_columns"] == ["member_id", "provider_id", "service_date"]


def test_every_emitted_file_is_valid_yaml(tmp_path):
    """Each written file parses as a YAML mapping (the generator emits real, loadable YAML).

    _generate already yaml.safe_load's each file; here we assert at least one file was written
    and re-confirm each parses to a mapping so a non-YAML emit turns red.
    """
    out = _generate(tmp_path)
    assert out["files"], "generator wrote no files"
    for f in out["files"]:
        doc = yaml.safe_load(Path(f).read_text())
        assert isinstance(doc, dict), f"{Path(f).name} is not a YAML mapping"


# --------------------------------------------------------------------------------------------
# Drift check (ticket 02 fix round): the COMMITTED dabs/resources/*.yml must byte-match a fresh
# generation from config/tables.json. Without this, committed files can silently diverge from the
# single source of truth and neither the gate nor CI would notice.
# --------------------------------------------------------------------------------------------


def _copy_committed(dst: Path) -> Path:
    """Copy the committed dabs/resources/*.yml into dst (a fresh dir) — the drift-check subject."""
    dst.mkdir(parents=True, exist_ok=True)
    for f in COMMITTED_RESOURCES.glob("*.yml"):
        shutil.copy(f, dst / f.name)
    return dst


def test_committed_resources_match_fresh_generation():
    """The real committed dabs/resources/*.yml byte-match a fresh generation from the real config.

    This is the guard itself: if someone edits config/tables.json without re-running codegen (or
    hand-edits a committed resource), this goes red. GREEN state == committed files are in sync.
    """
    from dabs.generate_resources import check_drift

    problems = check_drift(REAL_CONFIG, COMMITTED_RESOURCES)
    assert problems == [], f"committed dabs/resources drifted from config/tables.json: {problems}"


def test_drift_check_detects_stale_committed_file(tmp_path):
    """HOSTILE: a committed resource with ONE edited field must make the drift check go RED.

    A no-op check (always returns []) cannot pass this — the mutated byte content must be caught.
    """
    from dabs.generate_resources import check_drift

    subject = _copy_committed(tmp_path / "resources")
    victim = subject / "members.yml"
    text = victim.read_text()
    assert "members_app_ro" in text, "fixture regression: expected role name in committed members.yml"
    victim.write_text(text.replace("members_app_ro", "members_app_TAMPERED"))

    problems = check_drift(REAL_CONFIG, subject)
    assert problems, "drift check did not flag a stale (edited) committed resource file"
    assert any("members.yml" in p for p in problems), problems


def test_drift_check_detects_missing_committed_file(tmp_path):
    """HOSTILE: a deleted committed resource must make the drift check go RED (not silently pass)."""
    from dabs.generate_resources import check_drift

    subject = _copy_committed(tmp_path / "resources")
    (subject / "members.yml").unlink()

    problems = check_drift(REAL_CONFIG, subject)
    assert problems, "drift check did not flag a MISSING committed resource file"
    assert any("members.yml" in p for p in problems), problems


def test_drift_check_flags_unexpected_committed_file(tmp_path):
    """HOSTILE: a committed .yml that no config row generates must be flagged (stale leftover)."""
    from dabs.generate_resources import check_drift

    subject = _copy_committed(tmp_path / "resources")
    (subject / "orphan_table.yml").write_text("resources: {}\n")

    problems = check_drift(REAL_CONFIG, subject)
    assert problems, "drift check did not flag an unexpected/orphan committed resource file"
    assert any("orphan_table.yml" in p for p in problems), problems


def test_check_drift_cli_flag_exits_nonzero_on_drift(tmp_path):
    """The `--check` CLI flag exits non-zero when committed --out resources drift from --config,
    and does NOT write (verify-only). Guards the CI/gate wiring that replaces the overwrite step.
    """
    subject = _copy_committed(tmp_path / "resources")
    (subject / "members.yml").unlink()
    before = sorted(p.name for p in subject.glob("*.yml"))

    result = subprocess.run(
        [sys.executable, "-m", "dabs.generate_resources",
         "--check", "--config", str(REAL_CONFIG), "--out", str(subject)],
        cwd=str(REPO_ROOT), capture_output=True, text=True,
    )
    assert result.returncode != 0, f"--check exited 0 despite drift:\n{result.stdout}\n{result.stderr}"
    after = sorted(p.name for p in subject.glob("*.yml"))
    assert before == after, f"--check wrote/regenerated files (should be verify-only): {before} -> {after}"


def test_check_drift_cli_flag_exits_zero_when_in_sync():
    """`--check` against the real committed resources exits 0 (in-sync happy path)."""
    result = subprocess.run(
        [sys.executable, "-m", "dabs.generate_resources", "--check"],
        cwd=str(REPO_ROOT), capture_output=True, text=True,
    )
    assert result.returncode == 0, f"--check red on in-sync repo:\n{result.stdout}\n{result.stderr}"


def test_module_runs_as_cli_against_repo_config(tmp_path):
    """The module runs as a CLI (`python -m dabs.generate_resources --out DIR`) against the repo's
    real config/tables.json and writes valid YAML — no per-table hand-editing.
    """
    repo_root = GEN_MODULE_DIR
    real_config = repo_root / "config" / "tables.json"
    result = subprocess.run(
        [sys.executable, "-m", "dabs.generate_resources",
         "--config", str(real_config), "--out", str(tmp_path)],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"CLI failed:\nSTDOUT:{result.stdout}\nSTDERR:{result.stderr}"
    written = list(tmp_path.glob("*.yml"))
    assert written, "CLI wrote no .yml files"
    n = len(json.loads(real_config.read_text()))
    synced = 0
    for f in written:
        doc = yaml.safe_load(f.read_text())
        synced += len((doc.get("resources", {}) or {}).get("postgres_synced_tables", {}) or {})
    assert synced == n, f"expected {n} synced tables from repo config, got {synced}"
