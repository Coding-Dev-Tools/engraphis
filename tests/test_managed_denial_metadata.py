"""Denial metadata that turns a paid-feature rejection into a recovery action.

The dashboard can only offer the right button if the client tells it *why* the call was
denied. These tests pin the fixed-vocabulary reason, the plan that actually sells the
feature, the account destination, and the promise that provider text never crosses.
"""
from __future__ import annotations

import io
import urllib.error

import pytest

pytest.importorskip("fastapi", reason="full-stack extra not installed")

from fastapi import HTTPException  # noqa: E402

from engraphis import cloud_authz, licensing  # noqa: E402
from engraphis import cloud_features  # noqa: E402
from engraphis.cloud_features import CloudFeatureClient, CloudFeatureError  # noqa: E402
from engraphis.routes import v2_api  # noqa: E402


class _Opener:
    def __init__(self, status: int, body: bytes):
        self.status = status
        self.body = body

    def open(self, request, timeout=None):
        raise urllib.error.HTTPError(
            "https://compute.example.test/private", self.status, "failure", {},
            io.BytesIO(self.body),
        )


def _client_response(monkeypatch, status: int, body: bytes) -> CloudFeatureError:
    monkeypatch.setattr(
        cloud_features, "build_pinned_https_opener",
        lambda *handlers: _Opener(status, body),
    )
    client = CloudFeatureClient("https://compute.example.test", "org_1", "token")
    with pytest.raises(CloudFeatureError) as caught:
        client._request("GET", "/private")
    return caught.value


def test_a_known_reason_is_extracted_without_reflecting_the_body(monkeypatch):
    error = _client_response(
        monkeypatch,
        402,
        b'{"reason":"entitlement_expired","detail":"internal https://backend.invalid"}',
    )

    assert error.status == 402
    assert error.reason == "entitlement_expired"
    assert "internal" not in str(error)
    assert "backend.invalid" not in str(error)


@pytest.mark.parametrize(
    "body",
    (
        b"not json",
        b'{"reason":"not_a_real_reason"}',
        b'{"detail":"missing reason"}',
        b'{"reason":"entitlement_expired"}' + b"x" * (cloud_features.MAX_ERROR_BODY_BYTES + 1),
    ),
)
def test_unknown_or_oversized_reason_bodies_degrade_to_none(monkeypatch, body):
    error = _client_response(monkeypatch, 402, body)

    assert error.reason is None


def test_a_402_names_the_plan_that_sells_the_feature(monkeypatch):
    def fail():
        raise CloudFeatureError("denied", status=402, reason="entitlement_expired")

    with pytest.raises(HTTPException) as caught:
        v2_api._managed_call(fail, feature="sync")

    detail = caught.value.detail
    assert detail["reason"] == "entitlement_expired"
    assert detail["tier_required"] == "pro"
    assert detail["upgrade_url"] == licensing.upgrade_url("pro")
    assert detail["account_url"] == licensing.account_url()
    assert detail["transient"] is False


def test_a_full_seat_list_routes_to_the_account_portal_not_the_pro_plan(monkeypatch):
    def fail():
        raise CloudFeatureError("denied", status=402, reason="seat_limit_exceeded")

    with pytest.raises(HTTPException) as caught:
        v2_api._managed_call(fail, feature="automation")

    detail = caught.value.detail
    assert detail["reason"] == "seat_limit_exceeded"
    assert detail["tier_required"] == "team"
    assert detail["upgrade_url"] == licensing.upgrade_url("team")


def test_a_session_denial_reconnects_at_the_account_portal(monkeypatch):
    def fail():
        raise CloudFeatureError("denied", status=401)

    with pytest.raises(HTTPException) as caught:
        v2_api._managed_call(fail, feature="analytics")

    detail = caught.value.detail
    assert detail["reason"] == "token_stale"
    assert detail["upgrade_url"] == licensing.account_url()
    assert detail["account_url"] == licensing.account_url()


def test_a_role_denial_does_not_advertise_a_plan(monkeypatch):
    def fail():
        raise CloudFeatureError("denied", status=403, reason="insufficient_role")

    with pytest.raises(HTTPException) as caught:
        v2_api._managed_call(fail, feature="team")

    detail = caught.value.detail
    assert detail["reason"] == "insufficient_role"
    assert detail["tier_required"] == "team"
    assert detail["upgrade_url"] == licensing.account_url()


def test_cloud_authz_fallback_is_the_real_account_portal():
    result = cloud_authz.interpret_denial(402, {"reason": "entitlement_expired"})

    assert result["upgrade_url"] == licensing.account_url()
    assert "/billing/upgrade" not in result["upgrade_url"]


def test_the_seat_message_tells_a_team_owner_to_add_a_seat():
    result = cloud_authz.interpret_denial(402, {"reason": "seat_limit_exceeded"})

    assert "Add a seat" in result["message"]
    assert "Upgrade your plan" not in result["message"]
