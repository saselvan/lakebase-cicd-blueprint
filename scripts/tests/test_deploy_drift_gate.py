"""deploy.sh must VERIFY committed changelogs, not REGENERATE them at deploy time.

deploy.sh step 1b used to run `python3 liquibase/generate_changelogs.py` (a full regenerate) right
before applying migrations, so a deploy could apply something other than what was reviewed and
committed (a config edit not yet regenerated-and-committed, or a hand-edited generated file, would
be silently overwritten and deployed). The fix runs `generate_changelogs.py --check` there instead
and aborts (non-zero) on any drift, so the deploy uses exactly the committed artifacts.

Two assertions:
  1. deploy.sh's generate_changelogs invocation carries `--check` and never runs a bare regenerate.
  2. The `--check` gate the deploy now depends on actually exits non-zero on a drifted committed
     changelog and zero when in sync (the mechanism deploy.sh relies on to abort).
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_SH = REPO_ROOT / "scripts" / "deploy.sh"
GEN_PATH = REPO_ROOT / "liquibase" / "generate_changelogs.py"
REAL_CONFIG = REPO_ROOT / "config" / "tables.json"
COMMITTED_GENERATED = REPO_ROOT / "liquibase" / "generated"


def _generate_changelogs_invocations() -> list[str]:
    """Every non-comment deploy.sh line that runs generate_changelogs.py."""
    lines = []
    for raw in DEPLOY_SH.read_text().splitlines():
        stripped = raw.strip()
        if stripped.startswith("#"):
            continue
        if "generate_changelogs.py" in stripped:
            lines.append(stripped)
    return lines


def test_deploy_runs_generate_changelogs_at_step_1b():
    """Regression guard: deploy.sh must still invoke the generator at deploy time (as --check)."""
    assert _generate_changelogs_invocations(), (
        "deploy.sh no longer references generate_changelogs.py at all"
    )


def test_deploy_step1b_checks_drift_and_does_not_regenerate():
    """Every generate_changelogs.py invocation in deploy.sh must pass --check (verify-only) and
    none may run a bare regenerate that could deploy something other than the committed artifacts.

    MUTATION GATE: revert deploy.sh step 1b to a bare `generate_changelogs.py` (regenerate) and this
    goes RED.
    """
    invocations = _generate_changelogs_invocations()
    assert invocations, "deploy.sh must invoke generate_changelogs.py --check at deploy time"
    for line in invocations:
        assert re.search(r"generate_changelogs\.py\b", line), line
        assert "--check" in line, (
            f"deploy.sh regenerates changelogs at deploy time instead of checking drift: {line!r}"
        )


def test_check_gate_exits_zero_when_committed_in_sync():
    """The gate deploy.sh depends on: --check exits 0 against the in-sync committed tree."""
    result = subprocess.run(
        [sys.executable, str(GEN_PATH), "--check"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"--check red on in-sync repo:\n{result.stdout}\n{result.stderr}"


def test_check_gate_exits_nonzero_on_drift(tmp_path):
    """The gate aborts the deploy on drift: mutate a committed changelog copy, --check exits non-zero
    and writes nothing (so deploy.sh's `set -e` aborts before applying migrations)."""
    subject = tmp_path / "generated"
    subject.mkdir(parents=True)
    for f in COMMITTED_GENERATED.glob("*.changelog.sql"):
        (subject / f.name).write_text(f.read_text())
    victim = subject / "members.changelog.sql"
    text = victim.read_text()
    assert "members_app_ro" in text
    victim.write_text(text.replace("members_app_ro", "members_app_DRIFTED"))
    before = sorted(p.name for p in subject.glob("*.changelog.sql"))
    result = subprocess.run(
        [sys.executable, str(GEN_PATH), "--check", "--config", str(REAL_CONFIG), "--out", str(subject)],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, f"--check passed despite drift:\n{result.stdout}\n{result.stderr}"
    after = sorted(p.name for p in subject.glob("*.changelog.sql"))
    assert before == after, "--check must be verify-only (write nothing) so deploy uses committed artifacts"
