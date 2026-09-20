"""Falsifiability tests — the offline `bundle validate` seam.

The seam is: config/tables.json --(codegen)--> dabs/resources/*.yml --(bundle validate)--> exit code.
These tests drive the REAL `databricks` CLI (no mock of the CLI itself) against a localhost
workspace stub (dabs/tests/mock_workspace.py), so validation runs fully offline — no cloud, no
secrets. Each test asserts on an OBSERVABLE exit code / resource count, never on internals.

They encode the reviewer's three named mutations as behavior contracts:
  1. A malformed config/tables.json must make the pipeline FAIL (not pass silently).
  2. An invalid generated resource field must make `bundle validate --strict` FAIL.
  3. A good config must validate AND carry one synced table + one role per config row — so a
     missing codegen step (absent resources) turns the count assertion red.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
REAL_CONFIG = REPO_ROOT / "config" / "tables.json"
MALFORMED_CONFIG = HERE / "fixtures" / "tables_malformed.json"
DATABRICKS = shutil.which("databricks")

pytestmark = pytest.mark.skipif(
    DATABRICKS is None,
    reason="databricks CLI not on PATH; bundle validate seam requires it (CI installs >= 1.5.0)",
)


def _codegen(config: Path, out_dir: Path) -> subprocess.CompletedProcess:
    """Run the codegen. Returns the completed process (non-zero on bad config)."""
    return subprocess.run(
        [sys.executable, "-m", "dabs.generate_resources", "--config", str(config), "--out", str(out_dir)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )


class _Mock:
    """Localhost workspace stub for offline auth; yields the base http URL."""

    def __enter__(self) -> str:
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "dabs.tests.mock_workspace", "--port", "0"],
            cwd=str(REPO_ROOT),
            stdout=subprocess.PIPE,
            text=True,
        )
        deadline = time.time() + 10
        port = None
        while time.time() < deadline:
            line = self.proc.stdout.readline()
            if line.startswith("PORT="):
                port = line.strip().split("=", 1)[1]
                break
        assert port, "mock workspace did not report a port"
        return f"http://127.0.0.1:{port}"

    def __exit__(self, *_exc) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def _validate(bundle_dir: Path, host: str, target: str = "dev", strict: bool = True,
              json_out: bool = False) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["DATABRICKS_HOST"] = host
    env["DATABRICKS_TOKEN"] = "offline-dummy-not-a-secret"
    env.pop("DATABRICKS_CONFIG_PROFILE", None)
    env.pop("DATABRICKS_CONFIG_FILE", None)
    cmd = [DATABRICKS, "bundle", "validate", "--target", target]
    if strict:
        cmd.append("--strict")
    if json_out:
        cmd += ["-o", "json"]
    return subprocess.run(cmd, cwd=str(bundle_dir), capture_output=True, text=True, env=env)


def _bundle_dir(tmp_path: Path) -> Path:
    """Mirror the repo layout: bundle root at tmp_path/dabs, with config/ as a sibling.
    databricks.yml's `sync.paths` reference ../config (the single-source config the job task reads),
    and `bundle validate` stats that path — so the temp bundle needs it.
    """
    bundle = tmp_path / "dabs"
    bundle.mkdir(parents=True, exist_ok=True)
    shutil.copy(REPO_ROOT / "dabs" / "databricks.yml", bundle / "databricks.yml")
    # The migration job task's python_file (./migration_job.py) must resolve for validate.
    shutil.copy(REPO_ROOT / "dabs" / "migration_job.py", bundle / "migration_job.py")
    shutil.copy(REPO_ROOT / "dabs" / "render_ddl.py", bundle / "render_ddl.py")
    (tmp_path / "config").symlink_to(REPO_ROOT / "config")
    return bundle


def test_good_config_validates_and_emits_all_resources(tmp_path):
    """Good config -> codegen -> `bundle validate --strict` exit 0 for BOTH targets, and the
    bundle carries exactly one postgres_synced_tables + one postgres_roles per config row.

    Guards mutation 3: if codegen is skipped/broken, resources are absent -> the count is 0 -> red.
    """
    bundle = _bundle_dir(tmp_path)
    cg = _codegen(REAL_CONFIG, bundle / "resources")
    assert cg.returncode == 0, f"codegen failed on good config:\n{cg.stderr}"

    expected = len(json.loads(REAL_CONFIG.read_text()))
    with _Mock() as host:
        for target in ("dev", "prod"):
            res = _validate(bundle, host, target=target)
            assert res.returncode == 0, (
                f"strict validate failed for {target}:\nSTDOUT:{res.stdout}\nSTDERR:{res.stderr}"
            )
        js = _validate(bundle, host, target="dev", json_out=True)
        assert js.returncode == 0, js.stderr
        doc = json.loads(js.stdout)
    resources = doc.get("resources", {}) or {}
    n_synced = len(resources.get("postgres_synced_tables") or {})
    n_roles = len(resources.get("postgres_roles") or {})
    assert n_synced == expected, f"expected {expected} synced tables, bundle has {n_synced}"
    assert n_roles == expected, f"expected {expected} roles, bundle has {n_roles}"


def test_malformed_config_fails_the_pipeline(tmp_path):
    """A malformed config/tables.json must make the codegen step FAIL, so the pipeline never
    reaches a green validate.

    Guards mutation 1: corrupting tables.json must not pass silently.
    """
    bundle = _bundle_dir(tmp_path)
    cg = _codegen(MALFORMED_CONFIG, bundle / "resources")
    assert cg.returncode != 0, (
        "codegen exited 0 on MALFORMED tables.json — a corrupt config passed silently.\n"
        f"STDOUT:{cg.stdout}"
    )
    # And codegen wrote NO resource files, so there is nothing for validate to accept.
    resources_dir = bundle / "resources"
    produced = list(resources_dir.glob("*.yml")) if resources_dir.exists() else []
    assert produced == [], f"codegen wrote resources despite a malformed config: {produced}"


def test_injected_bad_resource_field_fails_strict_validate(tmp_path):
    """A well-formed but SCHEMA-INVALID resource field (unknown field on a postgres_synced_table)
    must make `bundle validate --strict` exit non-zero.

    Guards mutation 2: an invalid generated resource field must fail the job.
    """
    bundle = _bundle_dir(tmp_path)
    cg = _codegen(REAL_CONFIG, bundle / "resources")
    assert cg.returncode == 0, cg.stderr

    members = bundle / "resources" / "members.yml"
    doc = yaml.safe_load(members.read_text())
    first = next(iter(doc["resources"]["postgres_synced_tables"].values()))
    first["totally_bogus_field"] = True  # unknown field; additionalProperties: false
    members.write_text(yaml.safe_dump(doc, sort_keys=False))

    with _Mock() as host:
        res = _validate(bundle, host, target="dev", strict=True)
    assert res.returncode != 0, (
        "strict validate accepted an unknown resource field:\n"
        f"STDOUT:{res.stdout}\nSTDERR:{res.stderr}"
    )
