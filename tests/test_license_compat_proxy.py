"""Customer-mode compatibility proxy tests (server-extra only)."""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from engraphis.config import settings  # noqa: E402
from engraphis.inspector import license_compat_proxy as compat  # noqa: E402


def _client(monkeypatch) -> TestClient:
    monkeypatch.setattr(settings, "service_mode", "customer")
    monkeypatch.setenv("ENGRAPHIS_CLOUD_URL", "https://license.example.test")
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
            "Authorization": "Bearer ENGR1.redacted",
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
    assert captured["headers"]["authorization"] == "Bearer ENGR1.redacted"
    assert "cookie" not in captured["headers"]
    assert "x-forwarded-for" not in captured["headers"]
    assert "x-vendor-admin" not in captured["headers"]
    import json as _json
    assert _json.loads(captured["body"]) == {"key": "ENGR1.redacted"}


def test_customer_proxy_redacts_upstream_network_failures(monkeypatch):
    async def unavailable(*_args, **_kwargs):
        raise httpx.ConnectError("provider payload included a secret")

    monkeypatch.setattr(compat, "_send_upstream", unavailable)
    response = _client(monkeypatch).get("/license/v1/verify/fingerprint")
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
    assert response.status_code == 400
    assert response.json() == {"error": "invalid license compatibility path"}
    assert called is False


def test_compat_proxy_does_not_mount_in_combined_or_vendor_mode(monkeypatch):
    for mode in ("combined", "vendor"):
        monkeypatch.setattr(settings, "service_mode", mode)
        app = FastAPI()
        assert compat.mount_license_compat_proxy(app) is False
        assert not getattr(app.state, "_license_compat_proxy_mounted", False)
