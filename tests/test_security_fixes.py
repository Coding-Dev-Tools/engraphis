"""Regressions for the public redirector's fixed-origin security boundary."""
import pytest

pytest.importorskip("fastapi", reason="full-stack extra not installed")


def test_redirector_ignores_spoofed_host(monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.delenv("ENGRAPHIS_DASHBOARD_URL", raising=False)
    monkeypatch.setenv("ENGRAPHIS_HOST", "127.0.0.1")
    from engraphis.redirector import create_app

    client = TestClient(create_app())
    response = client.get(
        "/memories?q=1",
        headers={"X-Forwarded-Host": "evil.com", "Host": "evil.com"},
        follow_redirects=False,
    )
    assert response.status_code == 301
    assert "evil.com" not in response.headers["location"]
    assert response.headers["location"].startswith("http://127.0.0.1:8700/memories")


def test_redirector_uses_configured_dashboard_url(monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ENGRAPHIS_DASHBOARD_URL", "https://dash.example.com")
    from engraphis.redirector import create_app

    response = TestClient(create_app()).get(
        "/x", headers={"X-Forwarded-Host": "evil.com"}, follow_redirects=False
    )
    assert response.headers["location"] == "https://dash.example.com/x"
    assert response.headers["x-frame-options"] == "DENY"
