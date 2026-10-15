"""Offline test of scripts/wait_for_sync.sh with a stub `databricks` on PATH (no cloud).

The gate must fail on any failed state, including one whose name also contains ONLINE
(SYNCED_TABLE_ONLINE_PIPELINE_FAILED: the table is readable but its last pipeline run failed).
Before the fix the case statement matched *ONLINE* first, so that state printed "sync online"
and exited 0, and the deploy went on to Liquibase and verify over a failed sync.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WAIT_SH = REPO_ROOT / "scripts" / "wait_for_sync.sh"

STUB = """#!/usr/bin/env bash
# stub: databricks postgres get-synced-table ... -o json -> a fixed detailed_state
printf '{"status":{"detailed_state":"%s"}}\\n' "$STUB_STATE"
"""


def _run(tmp_path: Path, state: str) -> subprocess.CompletedProcess:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    stub = bindir / "databricks"
    stub.write_text(STUB)
    stub.chmod(0o755)
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "STUB_STATE": state}
    # timeout 5 s: a state the gate does not decide on would sleep 10 s, so the test fails fast
    return subprocess.run(
        ["bash", str(WAIT_SH), "cat.schema.members", "DEFAULT", "5"],
        env=env, capture_output=True, text=True, timeout=30,
    )


@pytest.mark.parametrize(
    "state",
    [
        "SYNCED_TABLE_ONLINE_PIPELINE_FAILED",  # hostile: contains ONLINE and FAILED
        "SYNCED_TABLE_OFFLINE_FAILED",
        "SYNCED_TABLE_PROVISIONING_PIPELINE_ERROR",
    ],
)
def test_failed_states_exit_1(tmp_path, state):
    r = _run(tmp_path, state)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "sync FAILED" in r.stdout
    assert "sync online" not in r.stdout


@pytest.mark.parametrize(
    "state",
    [
        "SYNCED_TABLE_ONLINE_TRIGGERED_UPDATE",  # the state the live run reached on a SNAPSHOT table
        "SYNCED_TABLE_ONLINE_NO_PENDING_UPDATE",
        "SYNCED_TABLE_ONLINE",
    ],
)
def test_online_states_exit_0(tmp_path, state):
    r = _run(tmp_path, state)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "sync online" in r.stdout
