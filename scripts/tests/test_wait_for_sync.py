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
# stub: records its args, prints STUB_OUT (default: a fixed detailed_state), exits STUB_RC
printf '%s\\n' "$*" >> "$STUB_ARGS"
if [ -n "${STUB_OUT:-}" ]; then printf '%s\\n' "$STUB_OUT"; else printf '{"status":{"detailed_state":"%s"}}\\n' "$STUB_STATE"; fi
exit "${STUB_RC:-0}"
"""

def _run(tmp_path: Path, state: str = "", timeout_sec: str = "5", **extra: str) -> subprocess.CompletedProcess:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    stub = bindir / "databricks"
    stub.write_text(STUB)
    stub.chmod(0o755)
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "STUB_STATE": state,
           "STUB_ARGS": str(tmp_path / "args.txt"), **extra}
    # a state the gate does not decide on sleeps 10 s once, then times out (timeout_sec < 10);
    # the 30 s subprocess limit catches a loop that never ends
    return subprocess.run(
        ["bash", str(WAIT_SH), "cat.schema.members", "DEFAULT", timeout_sec],
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


@pytest.mark.parametrize(
    "extra",
    [
        {"STUB_STATE": "SYNCED_TABLE_PROVISIONING_INITIAL_SNAPSHOT"},  # still loading at the timeout
        {"STUB_OUT": '{"status":{}}'},                                 # no detailed_state -> '?'
    ],
)
def test_undecided_state_times_out_with_exit_1(tmp_path, extra):
    r = _run(tmp_path, **extra)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "timeout after 5s" in r.stdout
    assert "sync online" not in r.stdout


@pytest.mark.parametrize(
    "extra",
    [
        {"STUB_OUT": "Error: synced table not found", "STUB_RC": "1"},          # CLI error, non-JSON
        {"STUB_STATE": "SYNCED_TABLE_ONLINE_NO_PENDING_UPDATE", "STUB_RC": "1"},  # ONLINE text, CLI failed
    ],
)
def test_cli_failure_never_passes_the_gate(tmp_path, extra):
    r = _run(tmp_path, **extra)
    assert r.returncode != 0, r.stdout + r.stderr
    assert "sync online" not in r.stdout


def test_polls_the_synced_table_by_its_resource_name(tmp_path):
    r = _run(tmp_path, "SYNCED_TABLE_ONLINE")
    assert r.returncode == 0
    args = (tmp_path / "args.txt").read_text()
    assert "postgres get-synced-table synced_tables/cat.schema.members -p DEFAULT -o json" in args
