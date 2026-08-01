"""Regression tests for the licensing, plan-gating, payments and credential fixes.

Each test here pins a behaviour that was previously unasserted, and every one of them
fails against the code as it stood before the accompanying change. They are grouped by
the surface they protect rather than by module, because the defects they cover were all
"two surfaces disagreed and nothing compared them".
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from engraphis import commercial
from engraphis.hosted_client import DEFAULT_CLOUD_URL, account_url, required_plan, upgrade_url

ROOT = Path(__file__).resolve().parents[1]


def test_website_claim_gate_allows_current_cloud_sync_encryption() -> None:
    """The marketing gate must not reject the shipped Cloud Sync E2EE claim."""

    gate = (ROOT / "scripts" / "check_commercial_manifest.py").read_text(encoding="utf-8")
    assert '"end-to-end encrypted",' not in gate


# --------------------------------------------------------------------------- plan gating

#: Manifest capability keys README's plan matrix marks Team-only (rows 423-426:
#: hosted multi-user dashboard/roles/seats, team audit export, scoped agent tokens).
TEAM_ONLY_FEATURES = (
    "hosted_multi_user_roles",
    "hosted_team_audit_export",
    "hosted_scoped_agent_tokens",
)


@pytest.mark.parametrize("feature", TEAM_ONLY_FEATURES)
def test_team_only_capabilities_do_not_advertise_the_pro_plan(feature: str) -> None:
    """A Team capability must never tell a customer who already holds Pro to buy Pro.

    ``_REQUIRED_PLAN`` held six keys and defaulted everything else to ``"pro"``, so these
    three -- all declared in the manifest and all Team-only in README's plan matrix --
    resolved to a plan that does not sell them.
    """

    assert commercial.manifest()["features"].get(feature) is True, (
        "%s is no longer declared in the manifest; update this test" % feature
    )
    assert required_plan(feature) == "team"


def test_the_required_plan_table_covers_every_hosted_capability_the_manifest_sells() -> None:
    """Any hosted_* capability the manifest turns on must resolve deliberately, not by
    falling through to the default."""

    from engraphis.hosted_client import _REQUIRED_PLAN

    declared = {
        name for name, enabled in commercial.manifest()["features"].items()
        if enabled and name.startswith("hosted_")
    }
    # ``hosted_authorization`` describes the architecture rather than a sold capability.
    sold = declared - {"hosted_authorization"}
    missing = sorted(
        name for name in sold
        if name not in _REQUIRED_PLAN and name.replace("hosted_opt_in_cloud_", "") not in _REQUIRED_PLAN
    )
    assert not missing, "no explicit required_plan entry for: %s" % missing


# ------------------------------------------------------------------- upgrade destinations

@pytest.mark.parametrize(
    "hostile",
    [
        "not a url at all",
        "/relative/path",
        "api.engraphis.com/account",       # no scheme: resolved against the local origin
        "//evil.tld/pay",
        "javascript:alert(1)",
        "data:text/html,<script>x</script>",
        "ftp://example.com/pay",
        "https://",                        # no host
    ],
)
def test_a_malformed_upgrade_url_falls_back_instead_of_shipping_a_dead_link(
    monkeypatch, hostile: str
) -> None:
    """These values reach customer-facing copy and an anchor href.

    A scheme-less value silently resolved against the dashboard origin and produced a
    404 checkout button with no error toast; a ``javascript:`` value had only the
    dashboard's own scheme filter between it and an href.
    """

    for name in ("ENGRAPHIS_PRO_UPGRADE_URL", "ENGRAPHIS_TEAM_UPGRADE_URL",
                 "ENGRAPHIS_UPGRADE_URL"):
        monkeypatch.setenv(name, hostile)
    assert upgrade_url() == (
        "https://api.engraphis.com/account?plan=pro&interval=monthly#billing"
    )
    assert upgrade_url("pro", "annual") == (
        "https://api.engraphis.com/account?plan=pro&interval=annual#billing"
    )
    assert upgrade_url("team") == (
        "https://api.engraphis.com/account?plan=team&interval=monthly#billing"
    )
    assert account_url() == DEFAULT_CLOUD_URL


def test_a_well_formed_override_is_still_honoured(monkeypatch) -> None:
    monkeypatch.setenv("ENGRAPHIS_PRO_UPGRADE_URL", "https://pay.example/pro")
    monkeypatch.setenv("ENGRAPHIS_TEAM_UPGRADE_URL", "https://pay.example/team")
    monkeypatch.delenv("ENGRAPHIS_UPGRADE_URL", raising=False)
    assert upgrade_url("pro") == "https://pay.example/pro"
    assert upgrade_url("team") == "https://pay.example/team"
    # A self-hosted control plane on loopback stays usable.
    monkeypatch.setenv("ENGRAPHIS_UPGRADE_URL", "http://127.0.0.1:9000/account")
    assert account_url() == "http://127.0.0.1:9000/account"


def test_the_lapsed_subscription_message_is_plan_neutral(monkeypatch) -> None:
    """A lapsed Team subscriber must not be pointed at the Pro checkout."""

    from engraphis.cloud_features import _public_http_error, _public_session_error

    monkeypatch.setenv("ENGRAPHIS_PRO_UPGRADE_URL", "https://pay.example/pro")
    monkeypatch.setenv("ENGRAPHIS_UPGRADE_URL", "https://pay.example/account")
    for message, _ in (_public_http_error(402), _public_session_error(402)):
        assert "https://pay.example/account" in message
        assert "https://pay.example/pro" not in message


# ----------------------------------------------------------------------- entitlement math

@pytest.mark.parametrize("status", ["canceled", "cancelled", "expired", "revoked", "inactive"])
def test_a_declared_terminal_status_defeats_the_optimistic_active_default(status: str) -> None:
    """``{"plan": "team", "status": "revoked"}`` must not read back as active access."""

    from engraphis.cloud_session import _declared_entitlement

    declared = _declared_entitlement({"plan": "team", "status": status})
    assert declared["plan"] == "team"
    assert declared["cloud_access_active"] is False


def test_an_explicit_active_flag_still_outranks_the_status_string() -> None:
    from engraphis.cloud_session import _declared_entitlement

    declared = _declared_entitlement(
        {"plan": "team", "status": "past_due", "cloud_access_active": True}
    )
    assert declared["cloud_access_active"] is True


def test_the_published_trial_and_grace_seconds_track_their_constants() -> None:
    """Three surfaces published these as literals; nothing bound them to the constants."""

    from engraphis import licensing

    sources = [
        (ROOT / "engraphis" / "routes" / "v2_api.py").read_text(encoding="utf-8"),
        (ROOT / "engraphis" / "routes" / "memory.py").read_text(encoding="utf-8"),
    ]
    for text in sources:
        assert '"trial_seconds": 259_200' not in text
        assert '"grace_seconds": 86_400' not in text
    assert licensing.TRIAL_SECONDS == 3 * 24 * 60 * 60
    assert licensing.MAX_HOSTED_ACCOUNT_GRACE_SECONDS == 24 * 60 * 60
    assert (
        licensing.MAX_LOCAL_WRITE_GRACE_SECONDS
        == licensing.MAX_HOSTED_ACCOUNT_GRACE_SECONDS
    )
    assert commercial.manifest()["trial"]["days"] * 24 * 60 * 60 == licensing.TRIAL_SECONDS
    grace_hours = commercial.manifest()["entitlement_lifecycle"]["max_grace_hours"]
    assert grace_hours * 60 * 60 == licensing.MAX_HOSTED_ACCOUNT_GRACE_SECONDS


# ------------------------------------------------------------------------------- payments

@pytest.mark.parametrize(
    "plan,field,value",
    [
        ("pro", "monthly_usd", 99999),
        ("pro", "annual_usd", 1),
        ("team", "monthly_usd", 2000),
        ("team", "annual_usd", 20),
        ("free", "monthly_usd", 49),
    ],
)
def test_a_drifted_price_fails_the_release_gate(plan: str, field: str, value: int) -> None:
    """Prices were read only inside ``_check_website``, which is reachable solely via
    --website-root and is never passed in CI -- so every published price was unguarded."""

    from scripts.check_commercial_manifest import _check_repository

    manifest = json.loads(
        (ROOT / "engraphis" / "commercial_manifest.json").read_text(encoding="utf-8")
    )
    manifest["plans"][plan][field] = value
    errors: list[str] = []
    try:
        _check_repository(manifest, errors)
    except Exception:  # noqa: BLE001 - a later structural check may raise; the price ran first
        pass
    assert any("price" in error for error in errors), errors


@pytest.mark.parametrize("value", ["ten dollars", None, 10.5, True])
def test_a_non_integer_price_fails_the_release_gate(value) -> None:
    from scripts.check_commercial_manifest import _check_repository

    manifest = json.loads(
        (ROOT / "engraphis" / "commercial_manifest.json").read_text(encoding="utf-8")
    )
    manifest["plans"]["pro"]["monthly_usd"] = value
    errors: list[str] = []
    try:
        _check_repository(manifest, errors)
    except Exception:  # noqa: BLE001
        pass
    assert any("price" in error for error in errors), errors


@pytest.mark.parametrize(
    "key,value",
    [("card_required", True), ("plans", ["pro"]), ("days", 14)],
)
def test_a_drifted_trial_promise_fails_the_release_gate(key, value) -> None:
    """"3 days, no card, both plans" is hardcoded across the UI, README and docs."""

    from scripts.check_commercial_manifest import _check_repository

    manifest = json.loads(
        (ROOT / "engraphis" / "commercial_manifest.json").read_text(encoding="utf-8")
    )
    manifest["trial"][key] = value
    errors: list[str] = []
    try:
        _check_repository(manifest, errors)
    except Exception:  # noqa: BLE001
        pass
    assert any("trial." in error for error in errors), errors


def test_the_checkout_catalog_reads_a_broken_manifest_without_raising(monkeypatch) -> None:
    """A KeyError here aborted the release check before it printed its own diagnostics."""

    broken = json.loads(
        (ROOT / "engraphis" / "commercial_manifest.json").read_text(encoding="utf-8")
    )
    broken["plans"]["pro"].pop("products")
    monkeypatch.setattr(commercial, "manifest", lambda: broken)
    targets = commercial.expected_checkout_targets()
    assert ("pro", "monthly") not in targets
    assert ("team", "monthly") in targets


def test_the_published_prices_match_the_manifest_everywhere_they_appear() -> None:
    """README, hosted-plans, and upgrade panels restate manifest prices."""

    manifest = commercial.manifest()
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    hosted_plans = (ROOT / "docs" / "HOSTED_PLANS.md").read_text(encoding="utf-8")
    dashboard = (ROOT / "engraphis" / "static" / "dashboard.js").read_text(encoding="utf-8")
    ledger = (ROOT / "engraphis" / "dashboard_assets" / "ledger.js").read_text(
        encoding="utf-8"
    )
    for plan in ("pro", "team"):
        monthly = "$%d" % manifest["plans"][plan]["monthly_usd"]
        annual = "$%d" % manifest["plans"][plan]["annual_usd"]
        assert monthly in readme and annual in readme, plan
        assert monthly in hosted_plans and annual in hosted_plans, plan
        assert monthly in dashboard and annual in dashboard, plan
        assert monthly in ledger and annual in ledger, plan
        unit = manifest["plans"][plan]["billing_unit"]
        assert "/ %s / month" % unit in ledger, plan
        assert "/ %s / year" % unit in ledger, plan
    assert "/ machine /" not in ledger
    assert "Minimum 2 seats" not in ledger


def test_default_checkout_urls_are_the_exact_manifest_targets(monkeypatch) -> None:
    for name in (
        "ENGRAPHIS_UPGRADE_URL",
        "ENGRAPHIS_PRO_UPGRADE_URL",
        "ENGRAPHIS_TEAM_UPGRADE_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    targets = commercial.expected_checkout_targets()
    for (plan, interval), target in targets.items():
        assert upgrade_url(plan, interval) == target["checkout_url"]


# ------------------------------------------------------------------ credentials and logs

def test_the_credential_state_directory_is_owner_only() -> None:
    """The leaves were 0600 but their directory was 0755 under the default umask."""

    if os.name == "nt":
        pytest.skip("POSIX permission semantics")
    from engraphis.private_state import atomic_private_text, ensure_private_dir

    previous = os.umask(0o022)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            nested = Path(tmp) / "state" / "nested"
            ensure_private_dir(nested)
            assert nested.stat().st_mode & 0o777 == 0o700
            leaf = nested / "cloud_session.json"
            atomic_private_text(leaf, "{}", harden_parent=True)
            assert leaf.stat().st_mode & 0o777 == 0o600
    finally:
        os.umask(previous)


@pytest.mark.parametrize(
    "line",
    [
        "refresh_credential=engr_rt_SECRETVALUE123456",
        'refresh_credential: "engr_rt_SECRETVALUE123456"',
        "connect with engr_ct_ABCDEFGHIJKLMNOPQRSTUV now",
        "engr_access_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345",
    ],
)
def test_a_credential_never_survives_redaction_in_any_rendering(line: str) -> None:
    """The assignment form used a shorter vocabulary than the colon form, so one field
    was redacted or not depending on how the caller happened to render it."""

    from engraphis.observability import redact

    cleaned = redact(line)
    assert "SECRETVALUE123456" not in cleaned
    assert "ABCDEFGHIJKLMNOPQRSTUV" not in cleaned
    assert "ABCDEFGHIJKLMNOPQRSTUVWXYZ012345" not in cleaned


# ------------------------------------------------------------------------ dashboard href

def test_both_dashboard_bundles_strip_control_characters_before_the_scheme_test() -> None:
    """``java\\tscript:`` defeated the scheme match and passed through unmodified; the
    URL parser then removed the tab and re-formed the ``javascript:`` scheme."""

    static = (ROOT / "engraphis" / "static" / "dashboard.js").read_text(encoding="utf-8")
    classic = (ROOT / "engraphis" / "classic_assets" / "dashboard.js").read_text(
        encoding="utf-8"
    )
    assert static == classic, "the two bundles must stay byte-identical"
    assert "u0000-\\u001F" in static or "u0000-\\u001f" in static.lower()
    # A scheme-less string that still carries a colon must fail closed rather than pass.
    assert "return /:/.test(s)?'#':s" in static


def test_classic_dashboard_selector_has_a_unique_registered_handler() -> None:
    """Duplicate object keys silently replaced the selector with the trial action."""

    script = (ROOT / "engraphis" / "classic_assets" / "dashboard.js").read_text(
        encoding="utf-8"
    )
    page = (ROOT / "engraphis" / "classic_assets" / "index.html").read_text(
        encoding="utf-8"
    )
    assert page.count('data-onchange="h146"') == 1
    assert script.count("h146:function(event){selectDashboard(this.value)}") == 1
    assert script.count("h84:function(event){startTrial()}") == 1


def test_dashboard_bundles_use_the_httponly_browser_session_exchange() -> None:
    """A configured API bearer must not make the browser dashboard unusable."""

    for path in (
        ROOT / "engraphis" / "static" / "dashboard.js",
        ROOT / "engraphis" / "classic_assets" / "dashboard.js",
        ROOT / "engraphis" / "dashboard_assets" / "ledger.js",
    ):
        script = path.read_text(encoding="utf-8")
        assert "/auth/session" in script
        assert "X-Engraphis-Browser-Session" in script
        assert "localstorage.setitem('engraphis-api-token" not in script.lower()
        assert "sessionStorage" not in script
