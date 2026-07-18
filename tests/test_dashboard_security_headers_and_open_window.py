"""Dashboard-side half of the 2026-07-18 security batch: H3 (no unauthenticated remote
/api window on a fresh deploy), M1 (baseline security response headers), L4 (/api/docs
is no longer unconditionally public).

Skips on the numpy-only CI gate (needs fastapi/httpx), like the other dashboard tests.
"""
import time

import pytest

pytest.importorskip("fastapi", reason="full-stack extra not installed")
pytest.importorskip("httpx", reason="httpx not installed")

from fastapi.testclient import TestClient  # noqa: E402

from engraphis import licensing as lic  # noqa: E402
from engraphis.config import settings  # noqa: E402
from engraphis.licensing import compose_key, ed25519_public_key  # noqa: E402
from engraphis.service import MemoryService  # noqa: E402

_SECRET = bytes(range(32))


def _client(monkeypatch, tmp_path, *, team=False, key=None, api_token="",
            client_addr=None):
    db = str(tmp_path / "dash.db")
    monkeypatch.setattr(settings, "db_path", db)
    monkeypatch.setattr(settings, "embed_model", "")
    monkeypatch.setattr(settings, "api_token", api_token)
    monkeypatch.setenv("ENGRAPHIS_EMBED_MODEL", "")
    monkeypatch.setenv("ENGRAPHIS_TEAM_MODE", "1" if team else "0")
    monkeypatch.setenv("ENGRAPHIS_TEAM_INVITES", "0")
    monkeypatch.setenv("ENGRAPHIS_TEST_AUTH_ITERATIONS", "1000")
    monkeypatch.setattr(lic, "_LICENSE_FILE", tmp_path / "license.key")
    if key:
        monkeypatch.setenv("ENGRAPHIS_LICENSE_KEY", key)
        monkeypatch.setenv("ENGRAPHIS_LICENSE_PUBKEY", ed25519_public_key(_SECRET).hex())
    else:
        monkeypatch.delenv("ENGRAPHIS_LICENSE_KEY", raising=False)
    lic.current_license(refresh=True)
    from engraphis.routes import v2_api
    v2_api.set_service(MemoryService.create(db))
    from engraphis.dashboard_app import create_app
    app = create_app()
    if client_addr is None:
        return TestClient(app)
    return TestClient(app, client=client_addr)


def _team_key(seats=5):
    return compose_key({"v": 1, "plan": "team", "email": "w@x.co", "seats": seats,
                        "issued": int(time.time()),
                        "expires": int(time.time() + 365 * 86400)}, _SECRET)


# ── H3: an unconfigured instance must not answer /api/* to the network ───────────────

def test_h3_remote_caller_refused_when_nothing_is_configured(monkeypatch, tmp_path):
    """The Railway window: deploy finished, no admin created, no license, no token."""
    client = _client(monkeypatch, tmp_path, client_addr=("203.0.113.9", 51234))
    response = client.get("/api/memories")
    assert response.status_code == 403
    assert response.json()["auth"] == "unconfigured"


def test_h3_loopback_caller_still_works_unconfigured(monkeypatch, tmp_path):
    """The local zero-config experience must be completely unchanged."""
    client = _client(monkeypatch, tmp_path, client_addr=("127.0.0.1", 51234))
    assert client.get("/api/memories").status_code == 200


def test_h3_forwarded_for_cannot_forge_loopback(monkeypatch, tmp_path):
    """With FORWARDED_ALLOW_IPS=* the ASGI server rewrites scope['client'] from
    X-Forwarded-For, so 'the client says it is 127.0.0.1' must not be believed."""
    monkeypatch.setenv("ENGRAPHIS_FORWARDED_ALLOW_IPS", "*")
    client = _client(monkeypatch, tmp_path, client_addr=("127.0.0.1", 51234))
    for header in ("x-forwarded-for", "forwarded", "x-real-ip", "x-forwarded-host"):
        response = client.get("/api/memories", headers={header: "127.0.0.1"})
        assert response.status_code == 403, header


def test_h3_unknown_peer_is_treated_as_remote(monkeypatch, tmp_path):
    """uvicorn sets scope['client'] = None on a UNIX-domain socket, and nginx adds no
    X-Forwarded-For unless configured to. Treating 'unknown' as local would hand the
    internet an open /api on any --uds deployment. Found by adversarial re-review."""
    client = _client(monkeypatch, tmp_path, client_addr=("127.0.0.1", 1))
    app = client.app

    async def _call(scope_client):
        received = {}
        scope = {"type": "http", "http_version": "1.1", "method": "GET",
                 "path": "/api/memories", "raw_path": b"/api/memories",
                 "query_string": b"", "root_path": "", "scheme": "http",
                 "headers": [(b"host", b"x")], "client": scope_client,
                 "server": ("x", 80), "app": app}

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            if message["type"] == "http.response.start":
                received["status"] = message["status"]

        await app(scope, receive, send)
        return received["status"]

    import asyncio
    assert asyncio.run(_call(None)) == 403


def test_h3_ipv4_mapped_loopback_is_local(monkeypatch, tmp_path):
    """The shipped Docker image binds `::` (dual-stack), so an IPv4 browser on
    127.0.0.1 is reported as ::ffff:127.0.0.1 — which ipaddress does NOT call loopback.
    Without unwrapping, a zero-config local install 403s its own dashboard."""
    client = _client(monkeypatch, tmp_path, client_addr=("::ffff:127.0.0.1", 1))
    assert client.get("/api/memories").status_code == 200


def test_h3_ipv6_loopback_is_local(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path, client_addr=("::1", 1))
    assert client.get("/api/memories").status_code == 200


def test_h3_ipv4_mapped_public_address_is_remote(monkeypatch, tmp_path):
    """Unwrapping must not accidentally admit a mapped PUBLIC address."""
    client = _client(monkeypatch, tmp_path, client_addr=("::ffff:203.0.113.9", 1))
    assert client.get("/api/memories").status_code == 403


def test_compose_bridge_peer_keeps_zero_config_local_quickstart(monkeypatch, tmp_path):
    """The host browser reaches a container through a private bridge address."""
    monkeypatch.setenv("ENGRAPHIS_LOCAL_TRUSTED_PEERS", "172.16.0.0/12")
    client = _client(
        monkeypatch, tmp_path, team=True, client_addr=("172.18.0.1", 51234)
    )
    assert client.get("/api/bootstrap").status_code == 200


def test_compose_publishes_unauthenticated_quickstart_on_loopback_only():
    from pathlib import Path

    compose = (Path(__file__).parents[1] / "docker-compose.yml").read_text(
        encoding="utf-8"
    )
    assert '      - "127.0.0.1:8700:8700"' in compose
    assert '      - "127.0.0.1:8701:8700"' in compose
    assert "ENGRAPHIS_LOCAL_TRUSTED_PEERS:" in compose


def test_h3_health_stays_public_for_the_platform_probe(monkeypatch, tmp_path):
    """Railway's healthcheck arrives from the network — breaking it would turn this
    fix into an outage."""
    client = _client(monkeypatch, tmp_path, client_addr=("203.0.113.9", 51234))
    assert client.get("/api/health").status_code == 200


def test_h3_team_bootstrap_stays_reachable_remotely(monkeypatch, tmp_path):
    """Remote first-run setup must still work, or the fix locks the operator out of
    their own fresh deploy."""
    client = _client(monkeypatch, tmp_path, team=True,
                     client_addr=("203.0.113.9", 51234))
    assert client.get("/api/auth/state").status_code == 200
    assert client.get("/api/license").status_code == 200


def test_remote_caller_cannot_consume_fresh_deployment_trial(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path, team=True,
                     client_addr=("203.0.113.9", 51234))
    response = client.post(
        "/api/license/team-trial", json={"email": "attacker@example.com"})
    assert response.status_code == 403
    assert response.json()["auth"] == "unconfigured"


def test_remote_first_admin_setup_requires_deployment_token(monkeypatch, tmp_path):
    """A public deployment must not let the first internet caller claim admin."""
    body = {"email": "admin@example.com", "name": "Admin",
            "password": "StrongPassword9!"}
    client = _client(monkeypatch, tmp_path, team=True, key=_team_key(),
                     client_addr=("203.0.113.9", 51234))
    assert client.post("/api/auth/setup", json=body).status_code == 401

    protected = _client(monkeypatch, tmp_path, team=True, key=_team_key(),
                        api_token="bootstrap-secret",
                        client_addr=("203.0.113.9", 51234))
    allowed = protected.post(
        "/api/auth/setup", json=body,
        headers={"Authorization": "Bearer bootstrap-secret"},
    )
    assert allowed.status_code == 200


def test_retired_inspector_surface_also_blocks_remote_admin_takeover(
        monkeypatch, tmp_path):
    """Direct ASGI imports of the retired JSON inspector must retain the same wall."""
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "inspector.db"))
    monkeypatch.setattr(settings, "team_mode", True)
    monkeypatch.setattr(settings, "api_token", "bootstrap-secret")
    monkeypatch.setenv("ENGRAPHIS_LICENSE_KEY", _team_key())
    monkeypatch.setenv("ENGRAPHIS_LICENSE_PUBKEY", ed25519_public_key(_SECRET).hex())
    lic.current_license(refresh=True)
    from engraphis.inspector.app import create_app as create_inspector_app

    client = TestClient(
        create_inspector_app(MemoryService.create(settings.db_path)),
        client=("203.0.113.9", 51234),
    )
    body = {"email": "admin@example.com", "name": "Admin",
            "password": "StrongPassword9!"}
    assert client.post("/api/auth/setup", json=body).status_code == 401
    allowed = client.post(
        "/api/auth/setup", json=body,
        headers={"Authorization": "Bearer bootstrap-secret"},
    )
    assert allowed.status_code == 200


def test_cors_preflight_reaches_cors_middleware_before_auth(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "cors_origins", ["https://client.example"])
    client = _client(monkeypatch, tmp_path, api_token="secret",
                     client_addr=("203.0.113.9", 51234))
    response = client.options(
        "/api/memories",
        headers={
            "Origin": "https://client.example",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "https://client.example"


def test_h3_api_token_restores_remote_access(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path, api_token="s3cret",
                     client_addr=("203.0.113.9", 51234))
    assert client.get("/api/memories").status_code == 401
    ok = client.get("/api/memories", headers={"Authorization": "Bearer s3cret"})
    assert ok.status_code == 200


def test_h3_team_wall_supersedes_the_restriction(monkeypatch, tmp_path):
    """Once a paid license is active the normal 401 wall applies — not the 403."""
    client = _client(monkeypatch, tmp_path, team=True, key=_team_key(),
                     client_addr=("203.0.113.9", 51234))
    assert client.get("/api/memories").status_code == 401


# ── M1: baseline security response headers ──────────────────────────────────────────

def test_m1_headers_present_on_a_normal_response(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path, client_addr=("127.0.0.1", 1))
    headers = client.get("/api/health").headers
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    assert headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert "camera=()" in headers["permissions-policy"]


def test_m1_headers_present_on_a_refused_response(monkeypatch, tmp_path):
    """Headers must wrap the auth gate's short-circuits too, not just call_next."""
    client = _client(monkeypatch, tmp_path, client_addr=("203.0.113.9", 51234))
    response = client.get("/api/memories")
    assert response.status_code == 403
    assert response.headers["x-frame-options"] == "DENY"
    assert "content-security-policy" in response.headers


def test_m1_hsts_only_over_https(monkeypatch, tmp_path):
    """Pinning HSTS on a plain-HTTP localhost dashboard would break every other
    project on 127.0.0.1 for a year."""
    client = _client(monkeypatch, tmp_path, client_addr=("127.0.0.1", 1))
    assert "strict-transport-security" not in client.get("/api/health").headers
    spoofed = client.get("/api/health", headers={"x-forwarded-proto": "https"})
    assert "strict-transport-security" not in spoofed.headers
    monkeypatch.setenv("ENGRAPHIS_FORWARDED_ALLOW_IPS", "*")
    forwarded = client.get(
        "/api/health", headers={"x-forwarded-proto": "http, https"})
    assert "max-age=" in forwarded.headers["strict-transport-security"]


def test_m1_csp_can_be_disabled_for_a_fronting_proxy(monkeypatch, tmp_path):
    monkeypatch.setenv("ENGRAPHIS_CSP", "")
    client = _client(monkeypatch, tmp_path, client_addr=("127.0.0.1", 1))
    headers = client.get("/api/health").headers
    assert "content-security-policy" not in headers
    assert headers["x-frame-options"] == "DENY"      # the others still apply


def test_m1_route_set_headers_win(monkeypatch, tmp_path):
    """setdefault, not assignment — a route that set a header on purpose keeps it."""
    from engraphis import http_security
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse

    app = FastAPI()

    @app.get("/x")
    def _x():
        return JSONResponse({"ok": True}, headers={"Referrer-Policy": "no-referrer"})

    http_security.install(app)
    assert TestClient(app).get("/x").headers["referrer-policy"] == "no-referrer"


# ── L4: the OpenAPI schema is not public on a team instance ─────────────────────────

def test_l4_docs_and_openapi_are_gated(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path, team=True, key=_team_key(),
                     client_addr=("203.0.113.9", 51234))
    assert client.get("/api/docs").status_code == 401
    assert client.get("/api/openapi.json").status_code == 401


def test_l4_cdn_backed_interactive_docs_are_disabled(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path, client_addr=("127.0.0.1", 1))
    response = client.get("/api/docs")
    assert response.status_code == 404
    assert "cdn.jsdelivr.net" not in response.text


def test_l4_redoc_route_does_not_exist(monkeypatch, tmp_path):
    """ReDoc sat outside /api, so the gate's first clause let it through
    unauthenticated. It duplicates /api/docs — removed rather than guarded."""
    client = _client(monkeypatch, tmp_path, team=True, key=_team_key(),
                     client_addr=("203.0.113.9", 51234))
    assert client.get("/redoc").status_code == 404


def test_m1_disabled_docs_response_still_gets_csp(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path, client_addr=("127.0.0.1", 1))
    docs = client.get("/api/docs")
    assert docs.status_code == 404
    assert "content-security-policy" in docs.headers
    assert docs.headers["x-frame-options"] == "DENY"


def test_dashboard_installs_configured_cors(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "cors_origins", ["https://client.example"])
    client = _client(monkeypatch, tmp_path, client_addr=("127.0.0.1", 1))
    response = client.options(
        "/api/memories",
        headers={"Origin": "https://client.example",
                 "Access-Control-Request-Method": "GET"},
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "https://client.example"


def test_headers_cover_unhandled_exceptions_without_leaking():
    from engraphis import http_security
    from fastapi import FastAPI

    app = FastAPI()

    @app.get("/boom")
    def boom():
        raise RuntimeError("password=hunter2 C:/private/customer.db")

    http_security.install(app)
    response = TestClient(app, raise_server_exceptions=False).get("/boom")
    assert response.status_code == 500
    assert response.json() == {"error": "internal server error"}
    assert "hunter2" not in response.text and "private" not in response.text
    assert response.headers["x-frame-options"] == "DENY"


def test_dashboard_operation_error_is_sanitized():
    from fastapi import HTTPException
    from engraphis.routes import v2_api

    def fail():
        raise RuntimeError("password=hunter2 C:/private/customer.db")

    with pytest.raises(HTTPException) as caught:
        v2_api._run(fail)
    assert caught.value.status_code == 500
    rendered = str(caught.value.detail)
    assert "hunter2" not in rendered and "private" not in rendered
