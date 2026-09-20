"""Falsifiability test — Terraform must honor per-table scheduling_policy from config.

The DABs generator resolves each synced table's `scheduling_policy` from `config/tables.json`
(defaulting to SNAPSHOT when the field is absent). The Terraform path is driven by the SAME single
source of truth (ADR 0004), so it must NOT hardcode the policy — otherwise a config row that sets
`scheduling_policy: "TRIGGERED"` would be silently forced to SNAPSHOT on the Terraform path while
the DABs path honored it, and the two paths would diverge.

This is HCL, so there is no Python execution harness; this is a source-level guard. The end-to-end
`terraform fmt -check` + `terraform validate` (offline, -backend=false) run separately. Here we
assert that main.tf reads the per-table value (`each.value`) with a SNAPSHOT default, not a
hardcoded literal.

MUTATION GATE: revert main.tf's scheduling_policy to a hardcoded `"SNAPSHOT"` literal and this
goes RED.
"""

import re
from pathlib import Path

MAIN_TF = Path(__file__).resolve().parents[2] / "terraform" / "main.tf"


def _scheduling_policy_rhs() -> str:
    """The right-hand side of the `scheduling_policy = ...` assignment in main.tf."""
    text = MAIN_TF.read_text()
    m = re.search(r"^\s*scheduling_policy\s*=\s*(?P<rhs>.+?)\s*$", text, re.M)
    assert m, f"no `scheduling_policy = ...` assignment found in {MAIN_TF}"
    return m.group("rhs").strip()


def test_scheduling_policy_reads_per_table_config_not_hardcoded():
    """main.tf resolves scheduling_policy from the per-table config (`each.value`), not a literal."""
    rhs = _scheduling_policy_rhs()
    assert "each.value" in rhs, (
        f"scheduling_policy must read the per-table config value (each.value), got: {rhs!r}"
    )
    # A bare literal ("SNAPSHOT" / "TRIGGERED" / "CONTINUOUS") means the config field is ignored.
    assert not re.fullmatch(r'"(SNAPSHOT|TRIGGERED|CONTINUOUS)"', rhs), (
        f"scheduling_policy is hardcoded to a literal ({rhs!r}); it must come from config/tables.json"
    )


def test_scheduling_policy_defaults_to_snapshot_when_absent():
    """The resolution keeps SNAPSHOT as the default for rows that omit scheduling_policy (matching
    the DABs generator's DEFAULT_SCHEDULING_POLICY), so the field stays optional in config."""
    rhs = _scheduling_policy_rhs()
    assert "SNAPSHOT" in rhs, (
        f"scheduling_policy resolution must default to SNAPSHOT when the config field is absent, "
        f"got: {rhs!r}"
    )
    # Expressed via a lookup/try default form, not a bare literal (guarded above).
    assert re.search(r"\b(try|lookup|coalesce)\b", rhs), (
        f"expected a try()/lookup()/coalesce() default for the optional field, got: {rhs!r}"
    )
