"""One plan→feature mirror, and a loud failure when it drifts.

The client used to keep three hand-maintained copies of the private control plane's hosted
entitlement table (the runtime, plus two test mirrors), and each copy was only ever
compared to itself. These tests pin the replacement: one checked-in contract, one loader,
and a runtime surface that reports a server grant it cannot render instead of silently
hiding it behind a lock.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from engraphis import commercial, entitlements
from engraphis.hosted_client import required_plan


ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "engraphis" / "entitlement_contract.json"


def test_the_contract_is_valid_json_and_declares_every_plan():
    data = json.loads(CONTRACT.read_text(encoding="utf-8"))

    assert data["schema"] == entitlements.SCHEMA
    assert set(data["plans"]) == {"free", "pro", "team"}
    assert set(data["features"]) >= set(data["plans"]["pro"])


def test_a_malformed_contract_is_rejected_instead_of_misread():
    with pytest.raises(ValueError):
        entitlements._validate({"schema": "not-the-contract"})
    with pytest.raises(ValueError):
        entitlements._validate(
            {
                "schema": entitlements.SCHEMA,
                "plans": {"free": [], "pro": ["sync"], "team": []},
                "features": {
                    "sync": {"plan": "pro", "label": "Cloud Sync", "status": "available"}
                },
                "limits": {"trial_days_by_plan": {"pro": 7, "team": 14}, "grace_hours": 24},
            }
        )


def test_features_for_is_case_and_whitespace_insensitive_and_treats_local_as_free():
    assert entitlements.features_for(" PRO ") == entitlements.features_for("pro")
    assert entitlements.features_for("local") == []
    assert entitlements.features_for("free") == []
    assert entitlements.features_for("enterprise") == []
    assert entitlements.features_for(None) == []


def test_team_grants_every_pro_feature_and_one_more():
    pro = set(entitlements.features_for("pro"))
    team = set(entitlements.features_for("team"))

    assert pro and pro < team
    assert team - pro == {"team"}


def test_the_contract_covers_every_hosted_capability_the_manifest_sells():
    manifest = commercial.manifest()

    for name, enabled in manifest["features"].items():
        if not enabled or not name.startswith("hosted_") or name == "hosted_authorization":
            continue
        key = name.replace("hosted_opt_in_cloud_", "")
        spec = entitlements.feature_spec(key)
        assert isinstance(spec, dict), key
        expected = "team" if name in {
            "hosted_multi_user_roles",
            "hosted_team_audit_export",
            "hosted_scoped_agent_tokens",
        } else "pro"
        assert spec["plan"] == expected, name
        assert required_plan(key) == expected, name


def test_local_export_is_free_and_never_advertised_as_a_paid_grant():
    assert entitlements.feature_spec("export")["plan"] == "free"
    assert required_plan("export") == "free"
    assert "export" not in entitlements.features_for("pro")
    assert "export" not in entitlements.features_for("team")


def test_unknown_features_keep_the_conservative_pro_fallback():
    assert entitlements.feature_spec("not-a-real-feature") is None
    assert required_plan("not-a-real-feature") == "pro"
    assert entitlements.unrecognized(["not-a-real-feature"], "pro") == ["not-a-real-feature"]


def test_unknown_grants_are_reported_but_never_become_feature_rows():
    granted = ["analytics", "future_capability", "future_capability"]

    assert entitlements.unrecognized(granted, "pro") == ["future_capability"]
    assert entitlements.unrecognized(["analytics", "automation"], "pro") == []


def test_managed_jev_is_an_included_paid_benefit_with_a_bounded_allowance():
    service = entitlements.included_services()["managed_jev"]

    assert service["plan"] == "pro"
    assert service["calls_per_person_per_day"] == 500
    assert entitlements.required_plan("managed_jev") == "pro"
    # It is informational, not a hosted feature row: no lock badge, no tick box.
    assert "managed_jev" not in entitlements.labels()


def test_license_exposes_the_included_paid_benefit(monkeypatch):
    pytest.importorskip("fastapi", reason="full-stack extra not installed")
    from engraphis.routes import v2_api

    monkeypatch.setenv("ENGRAPHIS_CLOUD_PLAN", "pro")
    payload = v2_api.get_license()

    assert payload["included_services"]["managed_jev"]["calls_per_person_per_day"] == 500
    assert "managed_jev" not in payload["features"]


def test_trial_and_grace_limits_match_the_commercial_manifest():
    manifest = commercial.manifest()

    assert entitlements.trial_days_by_plan() == manifest["trial"]["days_by_plan"]
    assert entitlements.grace_hours() == manifest["entitlement_lifecycle"]["max_grace_hours"]


def test_the_release_gate_accepts_the_checked_in_contract():
    from scripts.check_commercial_manifest import _check_repository

    errors: list[str] = []
    _check_repository(commercial.manifest(), errors)

    assert errors == []


def test_the_distribution_ships_the_contract():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")

    assert "entitlement_contract.json" in pyproject
    assert "include engraphis/entitlement_contract.json" in manifest


def test_license_reports_an_unknown_server_grant_without_unlocking_a_row(monkeypatch):
    pytest.importorskip("fastapi", reason="full-stack extra not installed")
    from engraphis.routes import v2_api

    monkeypatch.setattr(
        v2_api,
        "_plan_entitlement",
        lambda: {
            "plan": "pro",
            "features": v2_api.entitled_features("pro"),
            "unrecognized_features": ["future_capability"],
            "source": "session",
            "cloud_access_active": True,
            "checked_at": 0.0,
            "status": "",
            "is_trial": False,
            "trial_consumed": True,
            "trial_ends_at": 0.0,
        },
    )

    payload = v2_api.get_license()

    assert payload["unrecognized_features"] == ["future_capability"]
    assert "future_capability" not in payload["features"]
    assert set(payload["features"]) <= set(payload["known_features"])


def test_normalization_drops_an_unknown_grant_from_the_feature_list(monkeypatch):
    pytest.importorskip("fastapi", reason="full-stack extra not installed")
    from engraphis.routes import v2_api

    assert v2_api._normalized_features(["analytics", "future_capability"], "pro") == [
        "analytics"
    ]
    assert v2_api._unrecognized_features(["analytics", "future_capability"], "pro") == [
        "future_capability"
    ]
