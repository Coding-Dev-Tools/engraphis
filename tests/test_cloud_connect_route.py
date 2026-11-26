"""Tests for the dashboard's POST /api/cloud/connect route."""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi", reason="full-stack extra not installed")
pytest.importorskip("httpx", reason="httpx not installed")

from fastapi.testclient import TestClient

from engraphis import device_connect
from engraphis.config import settings
from engraphis.routes import v2_api


def _app(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "embed_model", "")
    monkeypatch.setattr(settings, "embed_dim", 384)
    monkeypatch.setattr(settings, "allowed_workspaces", [])
    monkeypatch.setattr(settings, "api_token", "")
    from engraphis.dashboard_app import create_app

    return create_app()


def test_cloud_connect_missing_token_fails(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path)
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        resp = client.post("/api/cloud/connect", json={})
        assert resp.status_code == 422


@pytest.mark.parametrize("status", [400, 401, 402, 422, 503])
def test_cloud_connect_error_propagation(monkeypatch, tmp_path, status):
    app = _app(monkeypatch, tmp_path)

    def fake_connect(*args, **kwargs):
        raise device_connect.DeviceConnectError(
            f"Simulated connect error ({status}).",
            status=status,
        )

    monkeypatch.setattr(device_connect, "connect", fake_connect)

    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        resp = client.post(
            "/api/cloud/connect",
            json={"token": "engr_ct_expiredtoken1234567890"},
        )
        assert resp.status_code == status
        assert resp.headers.get("cache-control") == "no-store"
        assert str(status) in resp.json()["detail"]["error"]


def test_cloud_connect_409_sanitizes_filesystem_paths(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path)

    def fake_connect(*args, **kwargs):
        raise device_connect.DeviceConnectError(
            r"The saved client identity has unsafe filesystem permissions. Remove C:\Users\secret\client_identity.json.",
            status=409,
        )

    monkeypatch.setattr(device_connect, "connect", fake_connect)

    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        resp = client.post(
            "/api/cloud/connect",
            json={"token": "engr_ct_validtoken1234567890"},
        )
        assert resp.status_code == 409
        assert "client_identity.json" not in resp.text
        assert "secret" not in resp.text


def test_cloud_connect_overlong_token_not_echoed_in_422(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path)
    secret_token = "engr_ct_" + ("S" * 520)
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        resp = client.post("/api/cloud/connect", json={"token": secret_token})
        assert resp.status_code == 422
        assert resp.headers.get("cache-control") == "no-store"
        assert secret_token not in resp.text
        assert "SSSSSSSSSS" not in resp.text


def test_cloud_connect_malformed_validation_errors_never_echo_token(monkeypatch, tmp_path):
    """List/object/raw malformed payloads must return a sanitized 422 without echoing token input."""
    app = _app(monkeypatch, tmp_path)
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        for payload, needle in (
            ({"token": ["engr_ct_secret_list_token_123"]}, "engr_ct_secret_list_token_123"),
            ({"token": {"nested": "engr_ct_secret_obj_token_456"}}, "engr_ct_secret_obj_token_456"),
            (["engr_ct_secret_root_list_789"], "engr_ct_secret_root_list_789"),
        ):
            resp = client.post("/api/cloud/connect", json=payload)
            assert resp.status_code == 422
            assert resp.headers.get("cache-control") == "no-store"
            assert needle not in resp.text
        raw_resp = client.post(
            "/api/cloud/connect",
            content=b'{"token": "engr_ct_secret_broken_json',
            headers={"Content-Type": "application/json"},
        )
        assert raw_resp.status_code == 422
        assert raw_resp.headers.get("cache-control") == "no-store"
        assert "engr_ct_secret_broken_json" not in raw_resp.text


def test_cloud_connect_success_updates_license_and_clears_denial(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path)

    v2_api._AUTHORITATIVE_DENIAL_PENDING.set()
    v2_api._authoritative_denial_at = 12345.0
    v2_api._denied_state_digests = {"session": "old", "cloud": "old"}
    v2_api._entitlement_refresh_failures = 4
    v2_api._entitlement_retry_after = 999999.0

    fake_cache = tmp_path / "cloud_entitlement.json"
    fake_cache.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(v2_api, "_entitlement_cache_path", lambda: fake_cache)

    summary = {
        "organization_id": "org_pro_trial",
        "plan": "pro",
        "cloud_access_active": True,
        "cloud_features": ["sync", "automation", "analytics"],
        "control_url": "https://control.example.test",
        "compute_url": "https://compute.example.test",
        "session_path": str(tmp_path / "cloud_session.json"),
    }
    captured = {}

    def fake_connect(token, *, installation_label=None, workspace_id=None, control_url=None):
        captured["token"] = token
        captured["installation_label"] = installation_label
        captured["workspace_id"] = workspace_id
        captured["control_url"] = control_url
        return summary

    monkeypatch.setattr(device_connect, "connect", fake_connect)

    fake_license = {
        "plan": "pro",
        "features": ["sync", "automation", "analytics"],
        "access_state": "trial",
        "is_trial": True,
    }
    monkeypatch.setattr(v2_api, "get_license", lambda: fake_license)

    try:
        with TestClient(app, client=("127.0.0.1", 50000)) as client:
            resp = client.post(
                "/api/cloud/connect",
                json={
                    "token": "engr_ct_validtoken1234567890",
                    "label": "My Test Workstation",
                    "workspace": "acme",
                },
            )
            assert resp.status_code == 200
            assert resp.headers.get("cache-control") == "no-store"
            data = resp.json()
            assert data["ok"] is True
            assert data["summary"]["plan"] == "pro"
            assert data["summary"]["organization_id"] == "org_pro_trial"
            assert "session_path" not in data["summary"]
            assert data["license"]["access_state"] == "trial"

        assert captured == {
            "token": "engr_ct_validtoken1234567890",
            "installation_label": "My Test Workstation",
            "workspace_id": "acme",
            "control_url": None,
        }
        assert not v2_api._AUTHORITATIVE_DENIAL_PENDING.is_set()
        assert v2_api._authoritative_denial_at == 0.0
        assert v2_api._denied_state_digests == {}
        assert v2_api._entitlement_refresh_failures == 0
        assert v2_api._entitlement_retry_after == 0.0
        assert not fake_cache.exists()
    finally:
        v2_api._AUTHORITATIVE_DENIAL_PENDING.clear()
        v2_api._authoritative_denial_at = 0.0
        v2_api._denied_state_digests = {}
        v2_api._entitlement_refresh_failures = 0
        v2_api._entitlement_retry_after = 0.0


def test_ledger_js_hardens_connect_token_handoff_and_views():
    from pathlib import Path

    ledger_path = Path(__file__).resolve().parents[1] / "engraphis" / "dashboard_assets" / "ledger.js"
    js = ledger_path.read_text(encoding="utf-8")
    # Fragment parsing remains supported for local dashboard handoff.
    assert "fragment.has('connect_token')" in js
    # Query-string parsing must never assign pendingConnectToken from location.search.
    assert "searchParams.get('connect_token')" not in js
    assert "pendingConnectToken = pendingConnectToken || (searchParams.get('token')" not in js
    # Fragment handoff must require explicit user confirmation and stay out of browser storage.
    assert "sessionStorage" not in js
    assert "return runBoot(true)" in js
    assert "Connect this installation to Engraphis Cloud?" in js
    # Manual prompt masks token input and view checks use state.view (not state.activeView).
    assert "type: 'password'" in js
    assert "state.activeView" not in js


def test_cloud_connect_refuses_cross_site_and_host_rebinding_requests(monkeypatch, tmp_path):
    """A web page on another site or DNS-rebound Host must never redeem a connect token."""
    app = _app(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(device_connect, "connect", lambda *a, **k: calls.append((a, k)) or {})

    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        for headers in (
            {"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"},
            {"Origin": "null"},
            {"Sec-Fetch-Site": "cross-site"},
            {"Sec-Fetch-Site": "same-site"},
            {"Host": "evil.example:8700"},
        ):
            resp = client.post(
                "/api/cloud/connect",
                json={"token": "engr_ct_attackertoken1234567890"},
                headers=headers,
            )
            assert resp.status_code in {401, 403}, headers
    assert calls == []


def test_cloud_connect_enforces_api_token_and_non_loopback_isolation(monkeypatch, tmp_path):
    """Remote clients without ENGRAPHIS_API_TOKEN are refused; configured API token is enforced."""
    app = _app(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(device_connect, "connect", lambda *a, **k: calls.append((a, k)) or {"plan": "pro"})
    monkeypatch.setattr(v2_api, "get_license", lambda: {"plan": "pro", "access_state": "active"})

    with TestClient(app, client=("203.0.113.10", 50000)) as remote_client:
        resp = remote_client.post(
            "/api/cloud/connect",
            json={"token": "engr_ct_validtoken1234567890"},
        )
        assert resp.status_code == 403
    assert calls == []

    monkeypatch.setattr(settings, "api_token", "test-dashboard-secret-token")
    local_app = _app(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "api_token", "test-dashboard-secret-token")
    with TestClient(local_app, client=("127.0.0.1", 50000)) as local_client:
        unauth = local_client.post(
            "/api/cloud/connect",
            json={"token": "engr_ct_validtoken1234567890"},
        )
        assert unauth.status_code == 401
        auth = local_client.post(
            "/api/cloud/connect",
            json={"token": "engr_ct_validtoken1234567890"},
            headers={"Authorization": "Bearer test-dashboard-secret-token"},
        )
        assert auth.status_code == 200
    assert len(calls) == 1


def test_cloud_connect_rejects_request_supplied_control_url(monkeypatch, tmp_path):
    """The browser cannot choose which control plane receives the connect token."""
    app = _app(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(device_connect, "connect", lambda *a, **k: calls.append((a, k)) or {})

    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        resp = client.post(
            "/api/cloud/connect",
            json={
                "token": "engr_ct_validtoken1234567890",
                "control_url": "https://attacker.example",
            },
        )
        assert resp.status_code == 422
    assert calls == []


def test_cloud_connect_unexpected_failure_does_not_echo_exception_text(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path)

    def boom(*args, **kwargs):
        raise RuntimeError(r"leaked C:\Users\secret\path and upstream-body-fragment")

    monkeypatch.setattr(device_connect, "connect", boom)

    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        resp = client.post("/api/cloud/connect", json={"token": "engr_ct_validtoken1234567890"})
        assert resp.status_code == 500
        assert "secret" not in resp.text
        assert "upstream-body-fragment" not in resp.text
        assert "Could not connect this device" in resp.json()["detail"]["error"]
