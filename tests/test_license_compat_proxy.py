"""Managed-relay compatibility proxy tests (server-extra only)."""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from engraphis.config import settings  # noqa: E402
from engraphis.inspector import license_compat_proxy as compat  # noqa: E402


def _client(monkeypatch) -> TestClient:
    monkeypatch.setattr(settings, "service_mode", "relay")
    monkeypatch.setenv("ENGRAPHIS_CLOUD_URL", "https://license.example.test")
    # Keep the suite deterministic after the real compatibility deadline passes.
    monkeypatch.setattr(compat.time, "time", lambda: compat.COMPAT_SUNSET_EPOCH - 1)
    app = FastAPI()
    assert compat.mount_license_compat_proxy(app) is True
    return TestClient(app)


def test_customer_proxy_preserves_protocol_and_strips_ambient_secrets(monkeypatch):
    captured = {}

    async def fake_send(method, url, headers, body):
        captured.update(method=method, url=url, headers=headers, body=body)
        return httpx.Response(
            402, json={"error": "seat limit"},
            headers={"content-type": "application/json", "retry-after": "60",
                     "x-provider-debug": "must-not-leak"})

    monkeypatch.setattr(compat, "_send_upstream", fake_send)
    client = _client(monkeypatch)
    response = client.post(
        "/license/v1/register?source=legacy", json={"key": "ENGR1.redacted"},
        headers={
            "Authorization": "Bearer customer-api-token",
            "Cookie": "engr_dash_session=customer-secret",
            "X-Forwarded-For": "198.51.100.9",
            "X-Vendor-Admin": "vendor-secret",
        })

    assert response.status_code == 402
    assert response.json() == {"error": "seat limit"}
    assert response.headers["deprecation"] == "true"
    assert response.headers["sunset"] == compat.COMPAT_SUNSET
    assert response.headers["retry-after"] == "60"
    assert "x-provider-debug" not in response.headers
    assert captured["method"] == "POST"
    assert captured["url"] == \
        "https://license.example.test/license/v1/register?source=legacy"
    assert "authorization" not in captured["headers"]
    assert "cookie" not in captured["headers"]
    assert "x-forwarded-for" not in captured["headers"]
    assert "x-vendor-admin" not in captured["headers"]
    import json as _json
    assert _json.loads(captured["body"]) == {"key": "ENGR1.redacted"}


def test_customer_proxy_redacts_upstream_network_failures(monkeypatch):
    async def unavailable(*_args, **_kwargs):
        raise httpx.ConnectError("provider payload included a secret")

    monkeypatch.setattr(compat, "_send_upstream", unavailable)
    response = _client(monkeypatch).get("/license/v1/verify/deadbeefcafe")
    assert response.status_code == 503
    assert response.json() == {"error": "license service is temporarily unavailable"}
    assert "provider payload" not in response.text


def test_customer_proxy_rejects_dot_segment_path_traversal(monkeypatch):
    called = False

    async def should_not_send(*_args, **_kwargs):
        nonlocal called
        called = True
        return httpx.Response(200)

    monkeypatch.setattr(compat, "_send_upstream", should_not_send)
    response = _client(monkeypatch).get("/license/v1/%2E%2E/ops/ready")
    assert response.status_code == 404
    assert called is False


def test_customer_proxy_allows_only_legacy_routes_and_methods(monkeypatch):
    called = False

    async def should_not_send(*_args, **_kwargs):
        nonlocal called
        called = True
        return httpx.Response(200)

    monkeypatch.setattr(compat, "_send_upstream", should_not_send)
    client = _client(monkeypatch)

    unknown = client.post(
        "/license/v1/revoke/deadbeefcafe",
        headers={"Authorization": "Bearer vendor-secret"},
    )
    assert unknown.status_code == 404

    wrong_method = client.put("/license/v1/register", json={"key": "ENGR1.redacted"})
    assert wrong_method.status_code == 405
    assert wrong_method.headers["allow"] == "POST"
    assert called is False


def test_customer_proxy_hard_sunsets_without_contacting_upstream(monkeypatch):
    called = False

    async def should_not_send(*_args, **_kwargs):
        nonlocal called
        called = True
        return httpx.Response(200)

    monkeypatch.setattr(compat, "_send_upstream", should_not_send)
    client = _client(monkeypatch)
    monkeypatch.setattr(compat.time, "time", lambda: compat.COMPAT_SUNSET_EPOCH)

    response = client.post(
        "/license/v1/register", json={"key": "ENGR1.redacted", "machine_id": "old"})

    assert response.status_code == 410
    assert response.headers["sunset"] == compat.COMPAT_SUNSET
    assert response.json()["replacement"].startswith("https://license.engraphis.com/")
    assert called is False


def test_compat_proxy_does_not_mount_outside_managed_relay_mode(monkeypatch):
    for mode in ("customer", "combined", "vendor"):
        monkeypatch.setattr(settings, "service_mode", mode)
        app = FastAPI()
        assert compat.mount_license_compat_proxy(app) is False
        assert not getattr(app.state, "_license_compat_proxy_mounted", False)


@pytest.mark.parametrize("method,path", [
    ("GET", "/license/v1/keys"),
    ("GET", "/license/v1/keys/key-1/devices"),
    ("POST", "/license/v1/revoke/key-1"),
    ("POST", "/license/v1/revoke-by-email"),
    ("POST", "/license/v1/deactivate"),
    ("GET", "/license/v1/not-a-client-call"),
])
def test_customer_proxy_denies_vendor_and_unknown_routes(monkeypatch, method, path):
    called = False

    async def should_not_send(*_args, **_kwargs):
        nonlocal called
        called = True
        return httpx.Response(200)

    monkeypatch.setattr(compat, "_send_upstream", should_not_send)
    response = _client(monkeypatch).request(method, path)
    assert response.status_code == 404
    assert called is False


def test_customer_proxy_enforces_exact_legacy_methods(monkeypatch):
    called = False

    async def should_not_send(*_args, **_kwargs):
        nonlocal called
        called = True
        return httpx.Response(200)

    monkeypatch.setattr(compat, "_send_upstream", should_not_send)
    response = _client(monkeypatch).get("/license/v1/register")
    assert response.status_code == 405
    assert called is False


def test_real_customer_app_never_mounts_the_compatibility_proxy(
        monkeypatch, tmp_path):
    """Ordinary customer dashboards are not forwarding trust boundaries."""
    calls = []

    async def fake_send(method, url, headers, body):
        calls.append((method, url, headers, body))
        return httpx.Response(200, json={"lease": "redacted"})

    api_token = "customer-api-token-at-least-32-characters"
    deployment = "deployment-token-at-least-32-characters"
    monkeypatch.setattr(compat, "_send_upstream", fake_send)
    monkeypatch.setattr(settings, "service_mode", "customer")
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "customer.db"))
    monkeypatch.setattr(settings, "embed_model", "")
    monkeypatch.setattr(settings, "allowed_workspaces", [])
    monkeypatch.setattr(settings, "api_token", api_token)
    monkeypatch.setenv("ENGRAPHIS_TEAM_MODE", "0")
    monkeypatch.setenv("ENGRAPHIS_EMBED_MODEL", "")
    monkeypatch.setenv("ENGRAPHIS_CLOUD_URL", "https://license.example.test")
    monkeypatch.setenv("ENGRAPHIS_DEPLOYMENT_TOKEN", deployment)

    from engraphis.dashboard_app import create_app

    with TestClient(create_app()) as client:
        allowed = client.post(
            "/license/v1/register", json={"key": "ENGR1.redacted"},
            headers={"Authorization": "Bearer " + api_token})
        denied_api = client.post(
            "/license/v1/revoke/key-1",
            headers={"Authorization": "Bearer " + api_token})
        denied_deployment = client.get(
            "/license/v1/keys",
            headers={"Authorization": "Bearer " + deployment})

    assert allowed.status_code == 404
    assert denied_api.status_code == 404
    assert denied_deployment.status_code == 404
    assert calls == []
