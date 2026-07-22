"""Commercial boundary invariants shared by runtime defaults and public documentation."""
from __future__ import annotations

import json
from pathlib import Path

from engraphis.licensing import TRIAL_DAYS


ROOT = Path(__file__).resolve().parents[1]


def _text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_example_configuration_defaults_to_customer_only():
    assignments = [
        line.strip()
        for line in _text(".env.example").splitlines()
        if line.strip().startswith("ENGRAPHIS_SERVICE_MODE=")
    ]

    assert assignments == ["ENGRAPHIS_SERVICE_MODE=customer"]


def test_manifest_keeps_trial_and_grace_as_separate_clocks():
    manifest = json.loads(_text("engraphis/commercial_manifest.json"))
    trial = manifest["trial"]
    lifecycle = manifest["entitlement_lifecycle"]

    assert TRIAL_DAYS == trial["days"] == 3
    assert "max_grace_hours" not in trial
    assert lifecycle["max_grace_hours"] == 24
    assert lifecycle["grace_mode"] == "workspace_write_grace"
    assert lifecycle["grace_allows"] == [
        "authenticated_existing_user_local_core_workspace_writes"
    ]
    assert set(lifecycle["live_lease_still_required_for"]) == {
        "paid_or_cost_bearing_features",
        "mcp_or_agent_writes",
    }
    assert lifecycle["grace_blocks_account_growth"] is True
    assert lifecycle["trial_expiry_extended_by_grace"] is False
    assert lifecycle["recovery"]["mode"] == "recovery_read_only"
    assert lifecycle["recovery"]["blocks_normal_mutations"] is True
    assert {
        "login",
        "password_recovery",
        "authenticated_reads",
        "data_export",
        "relicensing",
    } <= set(lifecycle["recovery"]["allows"])


def test_provider_trial_fallback_uses_the_same_three_day_contract(monkeypatch):
    from engraphis.inspector.webhooks import _trial_days

    monkeypatch.delenv("ENGRAPHIS_TRIAL_DAYS", raising=False)
    assert _trial_days(None, now=1_000) == TRIAL_DAYS == 3
    monkeypatch.setenv("ENGRAPHIS_TRIAL_DAYS", "not-a-number")
    assert _trial_days(None, now=1_000) == TRIAL_DAYS
    monkeypatch.setenv("ENGRAPHIS_TRIAL_DAYS", "30")
    assert _trial_days(None, now=1_000) == TRIAL_DAYS


def test_public_docs_state_the_license_and_lapse_boundaries():
    readme = _text("README.md")
    licensing = _text("docs/LICENSING.md")
    combined = readme + "\n" + licensing
    plain_readme = " ".join(readme.replace("**", "").split())
    plain_licensing = " ".join(licensing.replace("**", "").split())

    assert "exactly 3 active days" in combined
    assert "up to 24 hours" in plain_readme
    assert "up to 24 hours" in plain_licensing
    assert "workspace_write_grace" in readme and "workspace_write_grace" in licensing
    assert "recovery_read_only" in readme and "recovery_read_only" in licensing
    assert "authenticated reads" in combined and "data export" in combined
    assert "never extends trial expiry" in readme
    assert "enable a new installation or activation" in licensing
    assert "add users, seats, invitations, or tokens" in licensing
    assert "cannot retroactively withdraw" in licensing
    assert "Everything released in this public repository is licensed under Apache-2.0" in (
        licensing
    )
    assert "runtime mode or local license check is a deployment safeguard, not DRM" in (
        licensing
    )
