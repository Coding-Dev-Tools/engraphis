"""Baseline response headers must cover success and short-circuit responses."""
import pytest

pytest.importorskip("fastapi")
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
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

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard():
        # The real dashboard is an inline single-file app: inline <script>/<style> and
        # on* / style="" attributes. This route stands in for it.
        return "<!DOCTYPE html><html><body onload='boot()'>hi</body></html>"

    http_security.install(app)
    http_security.install(app)  # idempotent
    return TestClient(app)


def test_baseline_headers_apply_without_hsts_on_plain_http(monkeypatch):
    response = _client(monkeypatch).get("/")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    csp = response.headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in csp
    assert "unsafe-inline" not in csp
    assert "script-src-attr 'none'" in csp
    assert "worker-src 'self'" in csp
    assert "style-src-attr 'none'" in csp
    assert "Strict-Transport-Security" not in response.headers


def test_inline_dashboard_html_gets_a_policy_that_permits_inline(monkeypatch):
    """The single-file dashboard's inline scripts/styles/handlers must be allowed, or the
    strict API policy renders it a blank, unstyled page. text/html only."""
    csp = _client(monkeypatch).get("/dashboard").headers["Content-Security-Policy"]
    assert "script-src 'self' 'unsafe-inline'" in csp
    assert "script-src-attr 'unsafe-inline'" in csp
    assert "style-src 'self' 'unsafe-inline'" in csp
    assert "style-src-attr 'unsafe-inline'" in csp
    # The high-value directives survive the relaxation.
    assert "frame-ancestors 'none'" in csp
    assert "object-src 'none'" in csp
    assert "base-uri 'self'" in csp
    assert "form-action 'self'" in csp


def test_json_api_keeps_the_strict_policy(monkeypatch):
    """Only text/html is relaxed; JSON responses execute no markup and stay locked down."""
    csp = _client(monkeypatch).get("/").headers["Content-Security-Policy"]
    assert "unsafe-inline" not in csp
    assert "script-src-attr 'none'" in csp
    assert "style-src-attr 'none'" in csp


def test_explicit_csp_override_wins_wholesale_including_html(monkeypatch):
    """An operator-supplied ENGRAPHIS_CSP applies to every response, HTML included."""
    client = _client(monkeypatch, csp="default-src 'self'")
    assert client.get("/").headers["Content-Security-Policy"] == "default-src 'self'"
    assert client.get("/dashboard").headers[
        "Content-Security-Policy"] == "default-src 'self'"


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
