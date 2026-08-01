"""Commercial boundary invariants shared by runtime defaults and public documentation."""
from __future__ import annotations

import json
from pathlib import Path

from engraphis.commercial import BILLING_AUTHORITY, expected_checkout_targets
from engraphis.hosted_client import TRIAL_DAYS


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
    assert lifecycle["enforced_by"] == "private_control_plane"
    assert lifecycle["grace_allows"] == [
        "authenticated_existing_user_hosted_account_continuity"
    ]
    assert set(lifecycle["live_authorization_still_required_for"]) == {
        "paid_or_cost_bearing_features",
        "hosted_mcp_or_agent_writes",
    }
    assert lifecycle["grace_blocks_account_growth"] is True
    assert lifecycle["trial_expiry_extended_by_grace"] is False
    assert lifecycle["recovery"]["mode"] == "recovery_read_only"
    assert lifecycle["recovery"]["enforced_by"] == "private_control_plane"
    assert lifecycle["recovery"]["blocks_normal_mutations"] is True
    assert {
        "login",
        "password_recovery",
        "authenticated_reads",
        "data_export",
        "relicensing",
    } <= set(lifecycle["recovery"]["allows"])


def test_manifest_uses_stripe_as_the_only_launch_billing_authority():
    manifest = json.loads(_text("engraphis/commercial_manifest.json"))
    billing = manifest["billing"]
    targets = expected_checkout_targets()

    assert BILLING_AUTHORITY == billing["authority"] == billing["new_subscriptions"] == "stripe"
    assert billing["legacy_providers"] == []
    assert billing["checkout_mode"] == "authenticated_server_session"
    assert billing["provider_price_ids_public"] is False
    assert manifest["account_portal"] == "https://api.engraphis.com/account"
    assert "managed_dashboard" not in manifest
    assert set(targets) == {
        ("pro", "monthly"),
        ("pro", "annual"),
        ("team", "monthly"),
        ("team", "annual"),
    }
    for (plan, interval), target in targets.items():
        assert target["provider"] == "stripe"
        assert target["checkout_url"] == (
            f"https://api.engraphis.com/account?plan={plan}&interval={interval}#billing"
        )

def test_public_docs_state_the_license_and_lapse_boundaries():
    readme = _text("README.md")
    hosted_plans = _text("docs/HOSTED_PLANS.md")
    licensing = _text("docs/LICENSING.md")
    combined = readme + "\n" + hosted_plans + "\n" + licensing
    plain_hosted_plans = " ".join(hosted_plans.replace("**", "").split())
    plain_licensing = " ".join(licensing.replace("**", "").split())

    assert "exactly 3 active days" in combined
    assert "at most 24 hours" in plain_hosted_plans or "up to 24 hours" in plain_hosted_plans
    assert "up to 24 hours" in plain_licensing
    assert "workspace_write_grace" in hosted_plans and "workspace_write_grace" in licensing
    assert "recovery_read_only" in hosted_plans and "recovery_read_only" in licensing
    assert "private control plane" in plain_hosted_plans.lower()
    assert "local dashboard, MCP server, local writes" in licensing
    assert "not controlled by either hosted lifecycle state" in licensing
    assert "data export" in combined
    assert "does not extend a trial or subscription" in hosted_plans
    assert "enable a new installation or activation" in licensing
    assert "add hosted users, seats, invitations, devices, or credentials" in licensing
    assert "cannot retroactively withdraw" in licensing
    assert "Everything released in this public repository is licensed under Apache-2.0" in (
        licensing
    )
    assert "runtime mode or local license check is a deployment safeguard, not DRM" in (
        licensing
    )


def test_vendor_authority_is_not_shipped_in_the_public_tree():
    private_paths = (
        "engraphis/billing.py",
        "engraphis/vendor_app.py",
        "engraphis/relay_app.py",
        "engraphis/inspector/license_cloud.py",
        "engraphis/inspector/license_registry.py",
        "engraphis/inspector/sync_relay.py",
        "engraphis/cloud_license.py",
        "scripts/license_admin.py",
        "scripts/smoke_cloud.py",
    )
    assert all(not (ROOT / path).exists() for path in private_paths)
    licensing = _text("engraphis/licensing.py")
    for forbidden in (
        "ed25519_sign",
        "ed25519_verify",
        "compose_key",
        "parse_key",
        "start_trial",
        "require_feature",
        "ENGRAPHIS_LICENSE_KEY",
    ):
        assert forbidden not in licensing
    assert (ROOT / "engraphis/hosted_client.py").is_file()
    assert (ROOT / "engraphis/cloud_session.py").is_file()
    assert (ROOT / "engraphis/backends/sync_relay.py").is_file()


def test_container_examples_do_not_describe_private_license_or_relay_state_as_local():
    """Compose must not imply that this customer image runs the private authority."""

    dockerfile = _text("Dockerfile")
    compose = _text("docker-compose.yml")
    combined = dockerfile + "\n" + compose

    assert "license/trial/machine-id/lease" not in combined
    assert "revocation registry" not in combined
    assert "ENGRAPHIS_RELAY_DB" not in combined
    assert "cloud session" in combined
    assert "Issuance, trial state, leases, and revocations stay private." in compose


def test_readme_describes_only_customer_side_cloud_state_as_persisted():
    """The Docker quickstart must not imply that the public image owns licenses.

    The mounted state directory holds a customer-side connection plus a display cache;
    issuance and entitlement authority stay in the private control plane.  Calling that
    state "license state" made the open-core boundary ambiguous for self-hosters.
    """

    readme = _text("README.md")

    assert "database plus license state" not in readme
    assert "customer-side cloud session and non-authoritative entitlement display" in readme
    assert (
        "License issuance, trials, leases, and revocations remain on the private control plane."
        in readme
    )
