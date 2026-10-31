"""Automatic browser access stays inside an explicit local-machine/origin boundary."""
from types import SimpleNamespace

import pytest

from engraphis.local_auth import (
    BROWSER_SESSION_COOKIE, browser_bootstrap_request, browser_session,
    local_browser_request,
)

TOKEN = "local-browser-fixture-credential"


def _request(*, peer="127.0.0.1", host="127.0.0.1:8700", origin="http://127.0.0.1:8700",
             extra=(), marker="1", site="same-origin", scheme="http"):
    headers = [("host", host)]
    if origin is not None:
        headers.append(("origin", origin))
    if marker is not None:
        headers.append(("x-engraphis-browser-session", marker))
    if site is not None:
        headers.append(("sec-fetch-site", site))
    headers.extend(extra)
    return SimpleNamespace(scope={"client": (peer, 50000), "scheme": scheme,
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
def test_trusted_peers_require_the_explicit_nat_opt_in(monkeypatch, peer):
    monkeypatch.setenv("ENGRAPHIS_LOCAL_TRUSTED_PEERS", "*")
    assert not local_browser_request(_request(peer=peer))


def test_container_nat_peer_is_accepted_only_for_a_loopback_url(monkeypatch):
    monkeypatch.setenv("ENGRAPHIS_LOCAL_TRUSTED_PEERS", "172.16.0.0/12")

    # A browser on the Docker host that opened http://127.0.0.1:<port> reaches the
    # container through the bridge gateway. It keeps the zero-config entry experience.
    assert local_browser_request(_request(peer="172.17.0.1"), allow_trusted_peers=True)
    # Every other machine keeps the loopback-Host requirement: a LAN URL can never
    # mint a session, and peers outside the operator's list never qualify at all.
    assert not local_browser_request(
        _request(peer="172.17.0.1", host="192.168.1.5:8700",
                 origin="http://192.168.1.5:8700"),
        allow_trusted_peers=True,
    )
    assert not local_browser_request(_request(peer="203.0.113.9"), allow_trusted_peers=True)


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


# ── automatic browser access (the default) ───────────────────────────────────
# The dashboard's own origin is the boundary: whoever can reach it opens it. These
# cases pin that a *browser* cannot be tricked into it by another site.
@pytest.mark.parametrize("host", [
    "127.0.0.1:8700", "localhost:8700", "192.168.1.5:8700", "[::1]:8700",
    "engraphis.local:8700", "mybox:8700", "203.0.113.5:8700",
])
def test_automatic_browser_access_accepts_published_address_forms(host):
    request = _request(peer="203.0.113.9", host=host, origin="http://" + host)
    assert browser_bootstrap_request(request)


@pytest.mark.parametrize("host", ["attacker.example:8700", "localhost.attacker.example"])
def test_automatic_browser_access_rejects_an_unpublished_public_name(monkeypatch, host):
    monkeypatch.delenv("ENGRAPHIS_DASHBOARD_URL", raising=False)
    monkeypatch.delenv("ENGRAPHIS_HOST", raising=False)
    request = _request(peer="203.0.113.9", host=host, origin="http://" + host)
    assert not browser_bootstrap_request(request)


def test_configured_dashboard_url_publishes_its_public_name(monkeypatch):
    monkeypatch.setenv("ENGRAPHIS_DASHBOARD_URL", "https://memory.example.com")
    request = _request(peer="203.0.113.9", scheme="https",
                       host="memory.example.com", origin="https://memory.example.com")
    assert browser_bootstrap_request(request)


def test_automatic_browser_access_trusts_only_declared_forwarding_proxies(monkeypatch):
    monkeypatch.setenv("ENGRAPHIS_DASHBOARD_URL", "https://memory.example.com")
    request = _request(
        peer="10.0.0.2", scheme="http", host="memory.example.com",
        origin="https://memory.example.com",
        extra=[("x-forwarded-proto", "https"), ("x-forwarded-for", "203.0.113.9")],
    )
    assert not browser_bootstrap_request(request)
    monkeypatch.setenv("ENGRAPHIS_FORWARDED_ALLOW_IPS", "10.0.0.2")
    assert browser_bootstrap_request(request)
    # A trusted proxy still cannot launder a rebinding host.
    rebound = _request(
        peer="10.0.0.2", scheme="http", host="evil.example",
        origin="https://evil.example",
        extra=[("x-forwarded-proto", "https")],
    )
    assert not browser_bootstrap_request(rebound)


@pytest.mark.parametrize("kwargs", [
    {"marker": None}, {"marker": "0"}, {"site": "cross-site"}, {"site": "same-site"},
    {"origin": "http://attacker.example"}, {"origin": None},
])
def test_automatic_browser_access_rejects_cross_site_or_unproven_requests(kwargs):
    request = _request(peer="203.0.113.9", **kwargs)
    assert not browser_bootstrap_request(request)


def test_automatic_access_eligibility_probe_does_not_need_an_origin():
    request = _request(peer="203.0.113.9", origin=None)
    assert browser_bootstrap_request(request, require_origin=False)
    assert not browser_bootstrap_request(request)


@pytest.fixture
def client_factory(monkeypatch, tmp_path):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from engraphis.config import settings
    from engraphis.dashboard_app import create_app

    monkeypatch.setattr(settings, "api_token", TOKEN)
    monkeypatch.setattr(settings, "browser_auto_session", True)
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "local-session.db"))
    monkeypatch.setattr(settings, "embed_model", "")
    monkeypatch.setattr(settings, "allowed_workspaces", [])

    def create(peer="127.0.0.1"):
        return TestClient(create_app(), base_url="http://127.0.0.1:8700", client=(peer, 50000))
    return create


HEADERS = {"Origin": "http://127.0.0.1:8700", "X-Engraphis-Browser-Session": "1"}
BROWSER = {"X-Engraphis-Browser-Session": "1"}


def test_local_dashboard_opens_without_token_and_reuses_cookie_and_confirmation(client_factory, monkeypatch):
    # Paid cloud connectivity must not change the local dashboard's entry experience.
    monkeypatch.setenv("ENGRAPHIS_HOSTED_MODE", "true")
    with client_factory() as client:
        rejected = client.get("/api/memories", headers=BROWSER)
        assert rejected.status_code == 401
        assert rejected.json()["code"] == "local_browser_session_required"
        assert rejected.json()["local_browser"] is True

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
        assert client.get("/api/memories", headers=BROWSER).status_code == 200
        assert client.get("/api/memories").status_code == 403

        repeated = client.post("/api/auth/local-session", headers=HEADERS)
        assert repeated.json()["review_csrf_token"] == csrf
        assert client.cookies.get(BROWSER_SESSION_COOKIE) == cookie


@pytest.mark.parametrize("peer", ["203.0.113.9", "192.168.1.12"])
def test_remote_browser_opens_automatically(client_factory, peer):
    """Any browser that reaches the dashboard opens it; no credential is involved."""

    with client_factory(peer) as client:
        rejected = client.get("/api/memories", headers=BROWSER)
        assert rejected.status_code == 401
        assert rejected.json()["local_browser"] is True
        opened = client.post("/api/auth/local-session", headers=HEADERS)
        assert opened.status_code == 200
        assert TOKEN not in opened.text
        assert client.get("/api/memories", headers=BROWSER).status_code == 200
        # The bearer still works, but nothing required the person to supply it.
        client.cookies.clear()
        assert client.get("/api/memories").status_code == 401
        assert client.get("/api/memories",
                          headers={"Authorization": "Bearer " + TOKEN}).status_code == 200


def test_hardened_mode_keeps_remote_browsers_out(client_factory, monkeypatch):
    """``ENGRAPHIS_BROWSER_AUTO_SESSION=0`` restores credential-gated remote entry."""

    from engraphis.config import settings

    monkeypatch.setattr(settings, "browser_auto_session", False)
    with client_factory("203.0.113.9") as client:
        denied = client.post("/api/auth/local-session", headers=HEADERS)
        assert denied.status_code == 403
        assert "set-cookie" not in denied.headers
        assert client.get("/api/memories").status_code == 401
        assert client.post("/api/auth/link").status_code == 401
    with client_factory() as client:
        assert client.post("/api/auth/local-session", headers=HEADERS).status_code == 200


def test_container_nat_host_browser_opens_without_a_token(client_factory, monkeypatch):
    monkeypatch.setenv("ENGRAPHIS_LOCAL_TRUSTED_PEERS", "172.16.0.0/12")
    with client_factory("172.17.0.1") as client:
        rejected = client.get("/api/memories", headers=BROWSER)
        assert rejected.status_code == 401
        assert rejected.json()["local_browser"] is True
        assert client.post("/api/auth/local-session", headers=HEADERS).status_code == 200
        assert client.get("/api/memories", headers=BROWSER).status_code == 200


def test_one_time_sign_in_link_opens_the_dashboard_without_typing_a_token(client_factory):
    with client_factory() as client:
        assert client.post("/api/auth/link").status_code == 401

        issued = client.post("/api/auth/link", headers={"Authorization": "Bearer " + TOKEN})
        assert issued.status_code == 200
        assert issued.headers["cache-control"] == "no-store"
        assert TOKEN not in issued.text
        assert TOKEN not in issued.headers.get("set-cookie", "")
        url = issued.json()["url"]
        assert url.startswith("http://127.0.0.1:8700/#token=")
        code = url.split("#token=", 1)[1]

        assert client.post("/api/auth/handoff", json={"code": "egh1.9999999999.x.y"}).status_code == 401
        redeemed = client.post("/api/auth/handoff", json={"code": code})
        assert redeemed.status_code == 200
        assert redeemed.json()["authenticated"] is True
        assert client.get("/api/memories", headers=BROWSER).status_code == 200

        # A link buys one browser session; it is not an API bearer and cannot be replayed.
        client.cookies.clear()
        assert client.get("/api/memories",
                          headers={"Authorization": "Bearer " + code}).status_code == 401
        assert client.post("/api/auth/handoff", json={"code": code}).status_code == 401
        assert client.get("/api/memories", headers=BROWSER).status_code == 401


def test_expired_local_session_renews_without_entering_a_credential(client_factory):
    import time
    expired = browser_session(TOKEN, now=time.time() - 13 * 60 * 60)
    with client_factory() as client:
        client.cookies.set(BROWSER_SESSION_COOKIE, expired, domain="127.0.0.1", path="/")
        assert client.get("/api/memories", headers=BROWSER).status_code == 401
        assert client.post("/api/auth/local-session", headers=HEADERS).status_code == 200
        assert client.cookies.get(BROWSER_SESSION_COOKIE) != expired
        assert client.get("/api/memories", headers=BROWSER).status_code == 200


def test_automatic_session_retains_human_review_csrf_boundary(client_factory):
    with client_factory() as client:
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
    with client_factory() as client:
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
    with client_factory() as client:
        calls = []

        @client.app.post("/api/local-test-effect")
        def effect():
            calls.append(True)
            return JSONResponse({"error": "cloud account expired"}, status_code=401)

        rejected = client.post("/api/local-test-effect", headers=BROWSER)
        assert rejected.json()["code"] == "local_browser_session_required"
        assert calls == []
        client.post("/api/auth/local-session", headers=HEADERS)
        application_error = client.post("/api/local-test-effect", headers=BROWSER)
        assert application_error.status_code == 401
        assert "code" not in application_error.json()
        assert calls == [True]

