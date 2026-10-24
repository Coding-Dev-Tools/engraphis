"""Automatic browser access stays inside an explicit local-machine/origin boundary."""
from types import SimpleNamespace

import pytest

from engraphis.local_auth import (
    BROWSER_SESSION_COOKIE, browser_session, browser_session_ok, local_browser_request,
)

TOKEN = "local-browser-fixture-credential"


def _request(*, peer="127.0.0.1", host="127.0.0.1:8700", origin="http://127.0.0.1:8700",
             extra=(), marker="1", site="same-origin"):
    headers = [("host", host)]
    if origin is not None:
        headers.append(("origin", origin))
    if marker is not None:
        headers.append(("x-engraphis-browser-session", marker))
    if site is not None:
        headers.append(("sec-fetch-site", site))
    headers.extend(extra)
    return SimpleNamespace(scope={"client": (peer, 50000), "scheme": "http",
        "headers": [(name.encode("ascii"), value.encode("ascii")) for name, value in headers]})


@pytest.mark.parametrize("peer,host,origin", [
    ("127.0.0.1", "127.0.0.1:8700", "http://127.0.0.1:8700"),
    ("127.0.0.2", "localhost:8700", "http://localhost:8700"),
    ("::1", "[::1]:8700", "http://[::1]:8700"),
    ("::ffff:127.0.0.1", "127.0.0.1:8700", "http://127.0.0.1:8700"),
    ("127.0.0.1", "localhost", "http://localhost:80"),
])
def test_local_origin_and_literal_loopback_are_accepted(peer, host, origin):
    assert local_browser_request(_request(peer=peer, host=host, origin=origin))


@pytest.mark.parametrize("peer", ["203.0.113.9", "192.168.1.12", "::", "", "testclient"])
def test_configured_trusted_peers_cannot_mint_browser_sessions(monkeypatch, peer):
    monkeypatch.setenv("ENGRAPHIS_LOCAL_TRUSTED_PEERS", "*")
    assert not local_browser_request(_request(peer=peer))


@pytest.mark.parametrize("host", [
    "attacker.example:8700", "localhost.attacker.example", "127.0.0.1.attacker.example",
    "127.0.0.1:bad", "127.0.0.1:65536", "localhost:0", "user@localhost:8700",
    "localhost:8700/extra", "localhost:8700?query", "localhost:8700#fragment",
    "localhost:8700,attacker.example", " localhost:8700", "localhost:8700\\evil",
])
def test_host_cannot_be_used_for_dns_rebinding_or_authority_confusion(host):
    assert not local_browser_request(_request(host=host))


@pytest.mark.parametrize("origin", [
    None, "null", "http://attacker.example", "http://localhost:8700",
    "http://127.0.0.1:8701", "https://127.0.0.1:8700",
    "http://user@127.0.0.1:8700", "http://127.0.0.1:8700/",
    "http://127.0.0.1:8700?query", "http://127.0.0.1:8700#fragment",
    "http://127.0.0.1:8700\\evil",
])
def test_origin_is_required_and_must_match_exact_browser_origin(origin):
    assert not local_browser_request(_request(origin=origin))


@pytest.mark.parametrize("header", [
    "forwarded", "x-real-ip", "x-forwarded-for", "x-forwarded-host",
    "x-forwarded-proto", "x-forwarded-port", "x-forwarded-prefix", "via",
])
@pytest.mark.parametrize("value", ["", "127.0.0.1"])
def test_forwarding_header_presence_disqualifies_automatic_access(header, value):
    assert not local_browser_request(_request(extra=[(header, value)]))


@pytest.mark.parametrize("extra", [
    [("host", "127.0.0.1:8700")],
    [("origin", "http://127.0.0.1:8700")],
    [("x-engraphis-browser-session", "1")],
    [("sec-fetch-site", "same-origin")],
])
def test_duplicate_boundary_headers_are_rejected(extra):
    assert not local_browser_request(_request(extra=extra))


@pytest.mark.parametrize("kwargs", [
    {"marker": None}, {"marker": "0"}, {"site": "cross-site"}, {"site": "same-site"},
])
def test_cross_site_or_non_browser_requests_are_rejected(kwargs):
    assert not local_browser_request(_request(**kwargs))


def test_get_eligibility_does_not_mint_a_session_without_post_origin():
    request = _request(origin=None)
    assert local_browser_request(request, require_origin=False)
    assert not local_browser_request(request)


@pytest.fixture
def client_factory(monkeypatch, tmp_path):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from engraphis.config import settings
    from engraphis.dashboard_app import create_app

    monkeypatch.setattr(settings, "api_token", TOKEN)
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "local-session.db"))
    monkeypatch.setattr(settings, "embed_model", "")
    monkeypatch.setattr(settings, "allowed_workspaces", [])

    def create(peer="127.0.0.1", token=TOKEN):
        monkeypatch.setattr(settings, "api_token", token)
        return TestClient(create_app(), base_url="http://127.0.0.1:8700", client=(peer, 50000))
    return create


HEADERS = {"Origin": "http://127.0.0.1:8700", "X-Engraphis-Browser-Session": "1"}
BROWSER = {"X-Engraphis-Browser-Session": "1"}


def test_local_dashboard_opens_without_token_and_reuses_cookie_and_confirmation(client_factory, monkeypatch):
    # Paid cloud connectivity must not change the zero-token local dashboard's entry
    # experience. A token-gated instance is covered by its own tests below.
    monkeypatch.setenv("ENGRAPHIS_HOSTED_MODE", "true")
    with client_factory(token="") as client:
        # A zero-token instance is open for ordinary reads ...
        assert client.get("/api/memories", headers=BROWSER).status_code == 200

        # ... and the automatic session supplies the review CSRF confirmation.
        opened = client.post("/api/auth/local-session", headers=HEADERS)
        assert opened.status_code == 200
        assert opened.json()["authenticated"] is True
        assert TOKEN not in opened.text
        assert TOKEN not in opened.headers["set-cookie"]
        assert "HttpOnly" in opened.headers["set-cookie"]
        assert "SameSite=strict" in opened.headers["set-cookie"]
        assert opened.headers["cache-control"] == "no-store"
        cookie = client.cookies.get(BROWSER_SESSION_COOKIE)
        csrf = opened.json()["review_csrf_token"]
        assert client.get("/dashboard/review/csrf", headers=BROWSER).json()[
            "review_csrf_token"] == csrf

        repeated = client.post("/api/auth/local-session", headers=HEADERS)
        assert repeated.json()["review_csrf_token"] == csrf
        assert client.cookies.get(BROWSER_SESSION_COOKIE) == cookie
        assert "set-cookie" not in repeated.headers
        assert browser_session_ok(cookie, client.app.state.local_browser_secret)


@pytest.mark.parametrize("peer", ["203.0.113.9", "192.168.1.12"])
def test_remote_browser_cannot_bootstrap_even_with_forged_headers(client_factory, monkeypatch, peer):
    monkeypatch.setenv("ENGRAPHIS_LOCAL_TRUSTED_PEERS", "*")
    with client_factory(peer) as client:
        denied = client.post("/api/auth/local-session", headers=HEADERS)
        assert denied.status_code == 403
        assert "set-cookie" not in denied.headers
        assert client.get("/api/memories").status_code == 401
        assert client.get("/api/memories", headers={"Authorization": "Bearer " + TOKEN}).status_code == 200


def test_expired_local_session_renews_without_entering_a_credential(client_factory):
    import time
    with client_factory(token="") as client:
        key = client.app.state.local_browser_secret
        expired = browser_session(key, now=time.time() - 13 * 60 * 60)
        client.cookies.set(BROWSER_SESSION_COOKIE, expired, domain="127.0.0.1", path="/")
        assert client.get("/dashboard/review/csrf", headers=BROWSER).status_code == 401
        renewed = client.post("/api/auth/local-session", headers=HEADERS)
        assert renewed.status_code == 200
        assert "set-cookie" in renewed.headers
        assert expired not in renewed.headers["set-cookie"]
        assert client.cookies.get(BROWSER_SESSION_COOKIE) != expired
        assert browser_session_ok(client.cookies.get(BROWSER_SESSION_COOKIE), key)
        assert client.get("/dashboard/review/csrf", headers=BROWSER).status_code == 200


@pytest.mark.parametrize("cookie_kind", ["malformed", "forged"])
def test_invalid_local_cookie_is_replaced_without_echo(client_factory, cookie_kind):
    with client_factory(token="") as client:
        key = client.app.state.local_browser_secret
        supplied = ("not-a-signed-session" if cookie_kind == "malformed"
                    else browser_session(TOKEN + "-wrong-signing-key"))
        assert not browser_session_ok(supplied, key)
        client.cookies.set(BROWSER_SESSION_COOKIE, supplied, domain="127.0.0.1", path="/")
        assert client.get("/dashboard/review/csrf", headers=BROWSER).status_code == 401
        opened = client.post("/api/auth/local-session", headers=HEADERS)
        assert opened.status_code == 200
        assert "set-cookie" in opened.headers
        assert supplied not in opened.headers["set-cookie"]
        assert browser_session_ok(client.cookies.get(BROWSER_SESSION_COOKIE), key)
        assert client.get("/dashboard/review/csrf", headers=BROWSER).status_code == 200


def test_explicit_token_exchange_emits_cookie_and_rotates_confirmation(client_factory):
    with client_factory() as client:
        first = client.post("/api/auth/session", json={"token": TOKEN}, headers=HEADERS)
        assert first.status_code == 200
        assert "set-cookie" in first.headers
        assert "HttpOnly" in first.headers["set-cookie"]
        assert "SameSite=strict" in first.headers["set-cookie"]
        assert TOKEN not in first.headers["set-cookie"]
        csrf = first.json()["review_csrf_token"]
        exchanged = client.post("/api/auth/session", json={"token": TOKEN}, headers=HEADERS)
        assert exchanged.status_code == 200
        assert "set-cookie" in exchanged.headers
        assert exchanged.json()["review_csrf_token"] != csrf
        assert browser_session_ok(client.cookies.get(BROWSER_SESSION_COOKIE), TOKEN)
        assert client.get("/api/memories", headers=BROWSER).status_code == 200


def test_whitespace_only_token_still_gates_the_automatic_session(client_factory):
    """A present-but-blank token is configured, not absent.

    The API middleware treats any non-empty value as a configured credential, so
    the automatic session must not strip it into the zero-token path.
    """
    with client_factory(token="   ") as client:
        refused = client.post("/api/auth/local-session", headers=HEADERS)
        assert refused.status_code == 403
        assert refused.json()["code"] == "local_token_required"
        # The literal value still exchanges for a session, so the instance stays
        # recoverable by a client that can send it.
        exchanged = client.post(
            "/api/auth/session", json={"token": "   "}, headers=HEADERS,
        )
        assert exchanged.status_code == 200


def test_token_gated_instance_refuses_the_automatic_browser_session(client_factory):
    """An operator who configured a token keeps that boundary on every browser session.

    The automatic session reads request headers only, so a loopback port-forward
    (``ssh -L``, ``netsh portproxy``) can forge every one of them. No server-side
    signal separates that caller from the real local browser.
    """
    with client_factory() as client:
        refused = client.post("/api/auth/local-session", headers=HEADERS)
        assert refused.status_code == 403
        assert refused.json()["code"] == "local_token_required"
        assert refused.json()["token_required"] is True
        assert "set-cookie" not in refused.headers
        hint = client.get("/api/memories", headers=BROWSER)
        assert hint.status_code == 401
        assert hint.json()["local_browser"] is False
        assert hint.json()["token_required"] is True
        exchanged = client.post("/api/auth/session", json={"token": TOKEN}, headers=HEADERS)
        assert exchanged.status_code == 200
        assert client.get("/api/memories", headers=BROWSER).status_code == 200


def test_forged_loopback_bootstrap_cannot_approve_without_the_token(client_factory):
    with client_factory() as client:
        pending = client.app.state.service.remember(
            "The owner verified this local test fact.", workspace="local-test",
            source="web", trusted=True,
        )
        assert client.post("/api/auth/local-session", headers=HEADERS).status_code == 403
        body = {"memory_id": pending["id"], "reason": "Reviewed synthetic test evidence."}
        denied = client.post("/dashboard/review/approve", json=body,
            headers={**BROWSER, "X-Engraphis-Review-CSRF": "forged"})
        assert denied.status_code == 401


def test_automatic_session_retains_human_review_csrf_boundary(client_factory):
    with client_factory(token="") as client:
        pending = client.app.state.service.remember(
            "The owner verified this local test fact.", workspace="local-test",
            source="web", trusted=True,
        )
        csrf = client.post("/api/auth/local-session", headers=HEADERS).json()["review_csrf_token"]
        body = {"memory_id": pending["id"], "reason": "Reviewed synthetic test evidence."}
        assert client.post("/dashboard/review/approve", json=body, headers=BROWSER).status_code == 403
        approved = client.post("/dashboard/review/approve", json=body,
            headers={**BROWSER, "X-Engraphis-Review-CSRF": csrf})
        assert approved.status_code == 200


def test_zero_config_free_dashboard_needs_no_user_entered_credential(client_factory, monkeypatch):
    from engraphis.config import settings
    monkeypatch.setattr(settings, "api_token", "")
    with client_factory(token="") as client:
        assert client.get("/api/memories").status_code == 200
        opened = client.post("/api/auth/local-session", headers=HEADERS)
        assert opened.status_code == 200
        csrf = opened.json()["review_csrf_token"]
        assert client.get("/dashboard/review/csrf", headers=BROWSER).json()["review_csrf_token"] == csrf
        pending = client.app.state.service.remember(
            "A free local user can review an imported fact.", workspace="local-free",
            source="web", trusted=True,
        )
        approved = client.post("/dashboard/review/approve",
            json={"memory_id": pending["id"], "reason": "Checked the synthetic fact."},
            headers={**BROWSER, "X-Engraphis-Review-CSRF": csrf})
        assert approved.status_code == 200
        client.cookies.clear()
        assert client.post("/dashboard/review/approve",
            json={"memory_id": pending["id"], "reason": "Missing owner session."},
            headers={**BROWSER, "X-Engraphis-Review-CSRF": csrf}).status_code == 401


def test_only_middleware_rejections_carry_the_safe_retry_code(client_factory):
    from fastapi.responses import JSONResponse
    # Only a token-gated instance rejects at the middleware, so it must hold a session.
    with client_factory() as client:
        calls = []

        @client.app.post("/api/local-test-effect")
        def effect():
            calls.append(True)
            return JSONResponse({"error": "cloud account expired"}, status_code=401)

        rejected = client.post("/api/local-test-effect", headers=BROWSER)
        assert rejected.json()["code"] == "local_browser_session_required"
        assert rejected.json()["token_required"] is True
        assert calls == []
        client.post("/api/auth/session", json={"token": TOKEN}, headers=HEADERS)
        application_error = client.post("/api/local-test-effect", headers=BROWSER)
        assert application_error.status_code == 401
        assert "code" not in application_error.json()
        assert calls == [True]
