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


def test_cloud_connect_error_propagation(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path)

    def fake_connect(*args, **kwargs):
        raise device_connect.DeviceConnectError(
            "That connect token has expired, was already used, or is not valid.",
            status=401,
        )

    monkeypatch.setattr(device_connect, "connect", fake_connect)

    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        resp = client.post(
            "/api/cloud/connect",
            json={"token": "engr_ct_expiredtoken1234567890"},
        )
        assert resp.status_code == 401
        assert "expired" in resp.json()["detail"]["error"]


def test_cloud_connect_success_updates_license_and_clears_denial(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path)

    v2_api._AUTHORITATIVE_DENIAL_PENDING.set()
    v2_api._authoritative_denial_at = 12345.0

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

    def fake_connect(token, *, installation_label=None, workspace_id=None, control_url=None):
        assert token == "engr_ct_validtoken1234567890"
        return summary

    monkeypatch.setattr(device_connect, "connect", fake_connect)

    fake_license = {
        "plan": "pro",
        "features": ["sync", "automation", "analytics"],
        "access_state": "trial",
        "is_trial": True,
    }
    monkeypatch.setattr(v2_api, "get_license", lambda: fake_license)

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
        data = resp.json()
        assert data["ok"] is True
        assert data["summary"]["plan"] == "pro"
        assert data["license"]["access_state"] == "trial"

    assert not v2_api._AUTHORITATIVE_DENIAL_PENDING.is_set()
    assert v2_api._authoritative_denial_at == 0.0
    assert not fake_cache.exists()


def test_ledger_js_rejects_query_string_connect_token():
    from pathlib import Path

    ledger_path = Path(__file__).resolve().parents[1] / "engraphis" / "dashboard_assets" / "ledger.js"
    js = ledger_path.read_text(encoding="utf-8")
    # Fragment parsing remains supported for local dashboard handoff.
    assert "fragment.has('connect_token')" in js
    # Query-string parsing must never assign pendingConnectToken from location.search.
    assert "searchParams.get('connect_token')" not in js
    assert "pendingConnectToken = pendingConnectToken || (searchParams.get('token')" not in js
