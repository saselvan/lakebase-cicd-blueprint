"""deploy.sh must mint the Lakebase OAuth token INSIDE the per-table loop, not once before it.

Each table's `wait_for_sync` can block for ~30 min, so a single token minted before the loop can
expire mid-run (the app-role/grant/index/view migration then fails on a later table with an auth
error). The fix moves `lakebase_resolve_and_mint` inside the loop, AFTER `wait_for_sync` and BEFORE
the `liquibase update` / verify use, re-exporting PG*/URL from the fresh credential each iteration.

MUTATION GATE: move `lakebase_resolve_and_mint` back above the `for (( … ))` loop header (mint once)
and this goes RED. Minting after the wait but before liquibase is what keeps the token fresh.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_SH = REPO_ROOT / "scripts" / "deploy.sh"


def _first_code_line_index(needle: str) -> int:
    """Index of the first non-comment deploy.sh line containing `needle` (or -1)."""
    for i, raw in enumerate(DEPLOY_SH.read_text().splitlines()):
        stripped = raw.strip()
        if stripped.startswith("#"):
            continue
        if needle in stripped:
            return i
    return -1


def test_token_minted_inside_loop_after_wait_before_liquibase():
    """`lakebase_resolve_and_mint` runs inside the per-table loop, after the sync wait and before
    the liquibase update — so the token is fresh for every table however long the wait ran."""
    loop_header = _first_code_line_index("for (( i = 0")
    mint = _first_code_line_index("lakebase_resolve_and_mint")
    wait = _first_code_line_index("wait_for_sync.sh")
    liquibase = _first_code_line_index("liquibase update")

    assert loop_header != -1, "deploy.sh no longer has the per-table `for (( i = 0 … ))` loop"
    assert mint != -1, "deploy.sh no longer calls lakebase_resolve_and_mint"
    assert wait != -1 and liquibase != -1, "deploy.sh loop body changed shape unexpectedly"

    assert mint > loop_header, (
        "lakebase_resolve_and_mint is minted BEFORE the per-table loop — a token minted once can "
        "expire during a later table's ~30-min wait_for_sync"
    )
    assert wait < mint < liquibase, (
        "the credential must be minted AFTER wait_for_sync and BEFORE liquibase update so it is "
        f"fresh for the migration (wait@{wait}, mint@{mint}, liquibase@{liquibase})"
    )
