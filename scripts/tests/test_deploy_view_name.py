"""deploy.sh step-4 verify must honor a config `view_name` override.

The bug: deploy.sh passed the DERIVED ``${PG_TABLE}_v`` to ``verify_table``. A table that sets a
config ``view_name`` override gets THAT name created by its changelog (both generators resolve the
name through ``dabs.render_ddl.resolve_view_name``), so verify then looked for a view that does not
exist and FAILED a deploy that actually succeeded.

The fix extracts a ``T_VIEW`` field per table via the ONE shared seam ``resolve_view_name`` (the
exact function both generators use) and passes ``"$T_VIEW"`` to ``verify_table`` in both branches.

Two assertions (both offline, no cloud):
  1. deploy.sh's per-table field-extraction block emits ``T_VIEW`` resolved via resolve_view_name —
     for a fixture with a ``view_name`` override it emits the OVERRIDE, not the derived ``<tbl>_v``.
  2. Both ``verify_table`` calls in deploy.sh pass ``"$T_VIEW"`` and none pass the derived
     ``${PG_TABLE}_v``.

MUTATION GATE: revert deploy.sh's verify_table calls to ``"${PG_TABLE}_v"`` -> assertion 2 goes RED;
revert the T_VIEW emit (or its resolve_view_name import) -> assertion 1 goes RED.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_SH = REPO_ROOT / "scripts" / "deploy.sh"


def _field_extraction_heredoc() -> str:
    """The Python block deploy.sh runs per table to emit shell-safe field assignments.

    Extracted verbatim from deploy.sh (the `<<'PY' … PY` heredoc) so this tests the ACTUAL deploy
    code, not a reimplementation of it."""
    text = DEPLOY_SH.read_text()
    m = re.search(r"<<'PY'\n(.*?)^PY$", text, re.S | re.M)
    assert m, "could not find the per-table field-extraction heredoc in deploy.sh"
    return m.group(1)


def test_deploy_emits_t_view_override_via_resolve_view_name(tmp_path):
    """Run deploy.sh's own extraction block over a fixture with a `view_name` override and assert
    it emits ``T_VIEW=<override>`` (resolved through the shared seam), not the derived ``<tbl>_v``."""
    cfg = tmp_path / "tables.json"
    cfg.write_text(
        json.dumps(
            [
                {
                    "name": "members",
                    "synced_table_id": "my_catalog.cicd_proj.members",
                    "app_schema": "cicd_proj",
                    "app_role": "members_app_ro",
                    # override: a STABLE consumer-view name distinct from the derived members_v
                    "view_name": "members_stable_v",
                    "index_columns": [],
                }
            ]
        )
    )
    code = _field_extraction_heredoc()
    # deploy.sh invokes `python3 - "$CONFIG" "$i" "$ROOT"` => argv[1]=config, [2]=index, [3]=repo root
    result = subprocess.run(
        [sys.executable, "-", str(cfg), "0", str(REPO_ROOT)],
        input=code,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"extraction block failed:\n{result.stdout}\n{result.stderr}"
    assert "T_VIEW=members_stable_v" in result.stdout, (
        f"deploy.sh did not resolve the view_name override into T_VIEW:\n{result.stdout}"
    )
    assert "T_VIEW=members_v" not in result.stdout, (
        f"deploy.sh emitted the DERIVED view name, ignoring the override:\n{result.stdout}"
    )


def test_deploy_emits_derived_t_view_when_no_override(tmp_path):
    """With no override, T_VIEW is the derived ``<pg_table>_v`` (unchanged default behavior)."""
    cfg = tmp_path / "tables.json"
    cfg.write_text(
        json.dumps(
            [
                {
                    "name": "providers",
                    "synced_table_id": "my_catalog.cicd_proj.providers",
                    "app_schema": "cicd_proj",
                    "app_role": "providers_app_ro",
                    "index_columns": [],
                }
            ]
        )
    )
    code = _field_extraction_heredoc()
    result = subprocess.run(
        [sys.executable, "-", str(cfg), "0", str(REPO_ROOT)],
        input=code,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"extraction block failed:\n{result.stdout}\n{result.stderr}"
    assert "T_VIEW=providers_v" in result.stdout, result.stdout


def _verify_table_call_lines() -> list[str]:
    """Non-comment deploy.sh lines that invoke verify_table."""
    lines = []
    for raw in DEPLOY_SH.read_text().splitlines():
        stripped = raw.strip()
        if stripped.startswith("#"):
            continue
        if re.match(r"verify_table\b", stripped):
            lines.append(stripped)
    return lines


def test_deploy_passes_t_view_to_verify_table():
    """Both verify_table calls pass the RESOLVED view name and never the derived ``${PG_TABLE}_v``.

    MUTATION GATE: revert either call to ``"${PG_TABLE}_v"`` and this goes RED.
    """
    calls = _verify_table_call_lines()
    assert len(calls) >= 2, f"expected the two verify_table calls in deploy.sh, found: {calls}"
    for line in calls:
        assert '"$T_VIEW"' in line, f"verify_table call does not pass the resolved view: {line!r}"
        assert "${PG_TABLE}_v" not in line, (
            f"verify_table still passes the derived view name, ignoring view_name overrides: {line!r}"
        )
