"""Baseline response headers must cover success and short-circuit responses."""
import pytest

pytest.importorskip("fastapi")
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from engraphis import http_security


def _client(monkeypatch, *, csp=None, hsts=None):
    if csp is None:
        monkeypatch.delenv("ENGRAPHIS_CSP", raising=False)
    else:
        monkeypatch.setenv("ENGRAPHIS_CSP", csp)
    if hsts is None:
        monkeypatch.delenv("ENGRAPHIS_HSTS", raising=False)
    else:
        monkeypatch.setenv("ENGRAPHIS_HSTS", hsts)

    app = FastAPI()

    @app.get("/")
    def root():
        return {"ok": True}

    @app.get("/custom")
    def custom():
        return JSONResponse({"ok": True}, headers={"Referrer-Policy": "no-referrer"})

    http_security.install(app)
    http_security.install(app)  # idempotent
    return TestClient(app)


def test_baseline_headers_apply_without_hsts_on_plain_http(monkeypatch):
    response = _client(monkeypatch).get("/")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert "Strict-Transport-Security" not in response.headers


def test_https_proxy_response_gets_hsts_and_route_override_wins(monkeypatch):
    monkeypatch.setenv("ENGRAPHIS_FORWARDED_ALLOW_IPS", "*")
    response = _client(monkeypatch).get(
        "/custom", headers={"X-Forwarded-Proto": "https"}
    )
    assert response.headers["Strict-Transport-Security"] == http_security.DEFAULT_HSTS
    assert response.headers["Referrer-Policy"] == "no-referrer"


def test_empty_environment_overrides_disable_csp_and_hsts(monkeypatch):
    response = _client(monkeypatch, csp="", hsts="").get(
        "/", headers={"X-Forwarded-Proto": "https"}
    )
    assert "Content-Security-Policy" not in response.headers
    assert "Strict-Transport-Security" not in response.headers
