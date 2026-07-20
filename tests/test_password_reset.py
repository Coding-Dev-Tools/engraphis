"""Password reset ("forgot password") — the escape hatch for the team-mode sign-in
overlay: /api/auth/forgot issues a single-use emailed link, /api/auth/reset consumes it.

Covers: anti-enumeration (identical response for known/unknown/disabled/throttled
email, token never in the HTTP response body), the reset actually changes the
password, old sessions and the old password die on reset, tokens are single-use and
expire, and the per-email request throttle. Team mode requires a Team license
(honored here via the pytest-only ENGRAPHIS_LICENSE_PUBKEY override in tests/conftest.py) —
same fixture shape as tests/test_team_audit.py.
"""
import time

import pytest

pytest.importorskip("fastapi", reason="full-stack extra not installed")
from fastapi.testclient import TestClient  # noqa: E402

from engraphis import licensing as lic  # noqa: E402
from engraphis.config import settings  # noqa: E402
from engraphis.licensing import compose_key, ed25519_public_key  # noqa: E402
from engraphis.service import MemoryService  # noqa: E402

_SECRET = bytes(range(32))


def _team_key(seats=3):
    return compose_key({"v": 1, "plan": "team", "email": "w@x.co", "seats": seats,
                        "issued": int(time.time()),
                        "expires": int(time.time() + 365 * 86400)}, _SECRET)


def _client(monkeypatch, tmp_path, *, seats=3):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "dash.db"))
    monkeypatch.setattr(settings, "embed_model", "")
    monkeypatch.setenv("ENGRAPHIS_EMBED_MODEL", "")
    monkeypatch.setenv("ENGRAPHIS_TEAM_MODE", "1")
    monkeypatch.setattr(lic, "_LICENSE_FILE", tmp_path / "license.key")
    monkeypatch.setenv("ENGRAPHIS_LICENSE_KEY", _team_key(seats))
    monkeypatch.setenv("ENGRAPHIS_LICENSE_PUBKEY", ed25519_public_key(_SECRET).hex())
    lic.current_license(refresh=True)
    svc = MemoryService.create(str(tmp_path / "dash.db"))
    from engraphis.routes import v2_api
    v2_api.set_service(svc)
    from engraphis.dashboard_app import create_app
    return TestClient(create_app())


def _admin(c, email="admin@x.co", password="supersecret1"):
    assert c.post("/api/auth/setup", json={"email": email, "name": "Ada",
                  "password": password}).status_code == 200


def _invite_and_accept(c, *, email="m@x.co", name="Mo", role="member",
                       password="anotherpass1"):
    response = c.post("/api/auth/invitations", json={
        "email": email, "name": name, "role": role})
    assert response.status_code == 200, response.text
    invite_id = response.json()["invitation"]["id"]
    invitation = c.app.state.auth_store.resend_invitation(invite_id)
    accepted = TestClient(c.app).post("/api/auth/invitations/accept", json={
        "token": invitation["token"], "password": password})
    assert accepted.status_code == 200, accepted.text
    return accepted.json()["user"]


def _capture_reset_email(monkeypatch):
    """Stub send_password_reset_email and return the list it appends to, so a test
    can grab the token out of the emailed reset_url without the HTTP layer ever
    exposing it (the whole point of the anti-enumeration design)."""
    from engraphis.inspector import webhooks as WH
    sent = []
    monkeypatch.setattr(WH, "email_configured", lambda: True)
    monkeypatch.setattr(
        WH, "send_password_reset_email",
        lambda to, name, reset_url: sent.append(
            {"to": to, "name": name, "reset_url": reset_url}))
    return sent


def _token_from_url(reset_url):
    return reset_url.rsplit("reset_token=", 1)[-1]


def test_forgot_unknown_email_returns_ok_and_sends_nothing(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _admin(c)
    sent = _capture_reset_email(monkeypatch)
    r = c.post("/api/auth/forgot", json={"email": "nobody@x.co"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert sent == []


def test_forgot_known_email_sends_link_never_exposed_in_response(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _admin(c)
    sent = _capture_reset_email(monkeypatch)
    r = c.post("/api/auth/forgot", json={"email": "admin@x.co"})
    body = r.json()
    assert r.status_code == 200 and body == {"ok": True}
    assert len(sent) == 1 and sent[0]["to"] == "admin@x.co"
    assert "reset_token=" in sent[0]["reset_url"]
    assert "/#reset_token=" in sent[0]["reset_url"]
    assert "?reset_token=" not in sent[0]["reset_url"]
    # the raw response body must never carry the token itself
    assert _token_from_url(sent[0]["reset_url"]) not in r.text


def test_hosted_forgot_relays_server_to_server_when_local_email_is_absent(
        monkeypatch, tmp_path):
    from engraphis.inspector import webhooks as WH
    from tests.vendor_relay import wire_vendor_relay

    queued = []
    monkeypatch.setattr(WH, "email_configured", lambda: False)
    monkeypatch.setenv("ENGRAPHIS_DASHBOARD_URL", "https://customer.example")
    monkeypatch.setattr(
        WH, "queue_password_reset_email",
        lambda to, name, reset_url, **kwargs:
            queued.append({"to": to, "name": name, "reset_url": reset_url,
                           **kwargs}) or "eml_reset",
    )
    wire_vendor_relay(monkeypatch)
    c = _client(monkeypatch, tmp_path)
    _admin(c)
    r = c.post("/api/auth/forgot", json={"email": "admin@x.co"})

    assert r.status_code == 200 and r.json() == {"ok": True}
    assert len(queued) == 1 and queued[0]["to"] == "admin@x.co"
    assert queued[0]["reset_url"].startswith(
        "https://customer.example/#reset_token=")
    assert "?reset_token=" not in queued[0]["reset_url"]
    assert _token_from_url(queued[0]["reset_url"]) not in r.text


def test_remote_forgot_never_builds_a_reset_link_from_the_host_header(
        monkeypatch, tmp_path):
    """A forged Host must not turn the reset-email endpoint into account takeover."""
    from engraphis.inspector import webhooks as WH

    sent = []
    monkeypatch.delenv("ENGRAPHIS_DASHBOARD_URL", raising=False)
    monkeypatch.setattr(WH, "email_configured", lambda: True)
    monkeypatch.setattr(
        WH, "send_password_reset_email",
        lambda to, name, reset_url: sent.append(reset_url),
    )
    c = _client(monkeypatch, tmp_path)
    _admin(c)
    before = c.app.state.auth_store.conn.execute(
        "SELECT COUNT(*) FROM password_resets").fetchone()[0]

    remote = TestClient(c.app, client=("203.0.113.9", 50000))
    response = remote.post(
        "/api/auth/forgot", json={"email": "admin@x.co"},
        headers={"Host": "reset-token.attacker.example"},
    )
    assert response.status_code == 200 and response.json() == {"ok": True}
    assert sent == []
    # No undeliverable token was issued, so a forged request cannot invalidate a valid
    # link the account owner requested moments earlier.
    after = c.app.state.auth_store.conn.execute(
        "SELECT COUNT(*) FROM password_resets").fetchone()[0]
    assert after == before


def test_loopback_peer_forged_host_builds_only_a_loopback_reset_link(monkeypatch, tmp_path):
    """A reverse proxy forwarding from a loopback socket without X-Forwarded-* makes a remote
    visitor look local; the Host it supplies must NOT become the reset-link target. The local
    fallback emits only a localhost origin, so a forged Host is defused."""
    from engraphis.inspector import webhooks as WH

    sent = []
    monkeypatch.delenv("ENGRAPHIS_DASHBOARD_URL", raising=False)
    monkeypatch.setattr(WH, "email_configured", lambda: True)
    monkeypatch.setattr(
        WH, "send_password_reset_email",
        lambda to, name, reset_url: sent.append(reset_url),
    )
    c = _client(monkeypatch, tmp_path)
    _admin(c)

    local = TestClient(c.app, client=("127.0.0.1", 50000))
    response = local.post(
        "/api/auth/forgot", json={"email": "admin@x.co"},
        headers={"Host": "reset-token.attacker.example"},
    )
    assert response.status_code == 200 and response.json() == {"ok": True}
    assert len(sent) == 1
    assert "attacker.example" not in sent[0]
    assert sent[0].startswith("http://localhost")
    assert "#reset_token=" in sent[0]


def test_forgot_disabled_account_responds_identically_and_sends_nothing(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _admin(c)
    mid = _invite_and_accept(c)["id"]
    c.post("/api/auth/users/update", json={"user_id": mid, "disabled": True})
    sent = _capture_reset_email(monkeypatch)
    r = c.post("/api/auth/forgot", json={"email": "m@x.co"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert sent == []


def test_forgot_malformed_email_still_returns_ok(monkeypatch, tmp_path):
    """A garbage address must fail exactly like an unknown one — no 400 that would
    distinguish "not an email" from "not an account" for an attacker."""
    c = _client(monkeypatch, tmp_path)
    _admin(c)
    r = c.post("/api/auth/forgot", json={"email": "not-an-email"})
    assert r.status_code == 200 and r.json() == {"ok": True}


def test_reset_succeeds_signs_in_and_changes_password(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _admin(c, password="oldpassword1")
    sent = _capture_reset_email(monkeypatch)
    anon = TestClient(c.app)
    anon.post("/api/auth/forgot", json={"email": "admin@x.co"})
    token = _token_from_url(sent[0]["reset_url"])

    r = anon.post("/api/auth/reset", json={"token": token, "password": "brandnewpass1"})
    assert r.status_code == 200
    assert r.json()["user"]["email"] == "admin@x.co"
    assert "token" not in r.json()["user"]   # never echoed in the body, only the cookie
    # the reset response itself signs the browser in (cookie set on `anon`)
    assert anon.get("/api/auth/users").status_code == 200

    # old password is dead, new one works
    fresh = TestClient(c.app)
    assert fresh.post("/api/auth/login", json={"email": "admin@x.co",
                      "password": "oldpassword1"}).status_code == 401
    assert fresh.post("/api/auth/login", json={"email": "admin@x.co",
                      "password": "brandnewpass1"}).status_code == 200


def test_reset_revokes_every_existing_session(monkeypatch, tmp_path):
    """A session open before the reset (e.g. on another device) must die too — not
    just the browser that requested the reset."""
    c = _client(monkeypatch, tmp_path)
    _admin(c, password="oldpassword1")
    assert c.get("/api/auth/users").status_code == 200   # `c`'s cookie is live

    sent = _capture_reset_email(monkeypatch)
    anon = TestClient(c.app)
    anon.post("/api/auth/forgot", json={"email": "admin@x.co"})
    token = _token_from_url(sent[0]["reset_url"])
    assert anon.post("/api/auth/reset",
                     json={"token": token, "password": "brandnewpass1"}).status_code == 200

    assert c.get("/api/auth/users").status_code == 401


def test_reset_token_is_single_use(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _admin(c, password="oldpassword1")
    sent = _capture_reset_email(monkeypatch)
    anon = TestClient(c.app)
    anon.post("/api/auth/forgot", json={"email": "admin@x.co"})
    token = _token_from_url(sent[0]["reset_url"])

    assert anon.post("/api/auth/reset",
                     json={"token": token, "password": "brandnewpass1"}).status_code == 200
    r2 = anon.post("/api/auth/reset", json={"token": token, "password": "anotherpass2"})
    assert r2.status_code == 400
    assert "invalid or expired" in r2.json()["detail"]["error"]


def test_reset_rejects_bogus_token(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _admin(c)
    import engraphis.inspector.auth as auth_mod

    def expensive_hash_must_not_run(*args, **kwargs):
        pytest.fail("an invalid public reset token reached PBKDF2")

    monkeypatch.setattr(auth_mod, "_hash_password", expensive_hash_must_not_run)
    r = c.post("/api/auth/reset", json={"token": "not-a-real-token-xxxxxxxxxx",
                                        "password": "brandnewpass1"})
    assert r.status_code == 400
    assert "invalid or expired" in r.json()["detail"]["error"]


def test_reset_rejects_expired_token(monkeypatch, tmp_path):
    import engraphis.inspector.auth as auth_mod
    monkeypatch.setattr(auth_mod, "RESET_TOKEN_TTL_SECONDS", -1)   # expires instantly

    c = _client(monkeypatch, tmp_path)
    _admin(c)
    sent = _capture_reset_email(monkeypatch)
    c.post("/api/auth/forgot", json={"email": "admin@x.co"})
    token = _token_from_url(sent[0]["reset_url"])

    r = c.post("/api/auth/reset", json={"token": token, "password": "brandnewpass1"})
    assert r.status_code == 400
    assert "invalid or expired" in r.json()["detail"]["error"]


def test_reset_rejects_password_too_short_at_the_schema_level(monkeypatch, tmp_path):
    """Same convention as SetupReq/NewUserReq: min_length=10 is enforced by the
    pydantic model before the request body even reaches AuthStore, so a too-short
    password is a 422, not a 400."""
    c = _client(monkeypatch, tmp_path)
    _admin(c)
    sent = _capture_reset_email(monkeypatch)
    c.post("/api/auth/forgot", json={"email": "admin@x.co"})
    token = _token_from_url(sent[0]["reset_url"])
    r = c.post("/api/auth/reset", json={"token": token, "password": "short"})
    assert r.status_code == 422


def test_reset_enforces_character_class_password_policy(monkeypatch, tmp_path):
    """Long enough to pass the schema's min_length, but no uppercase/digit/special —
    AuthStore._validate_password rejects it and the route surfaces that as a 400."""
    c = _client(monkeypatch, tmp_path)
    _admin(c)
    sent = _capture_reset_email(monkeypatch)
    c.post("/api/auth/forgot", json={"email": "admin@x.co"})
    token = _token_from_url(sent[0]["reset_url"])
    r = c.post("/api/auth/reset", json={"token": token, "password": "alllowercaseletters"})
    assert r.status_code == 400
    assert "uppercase" in r.json()["detail"]["error"]


def test_forgot_throttles_after_max_requests_per_email(monkeypatch, tmp_path):
    import engraphis.inspector.auth as auth_mod
    c = _client(monkeypatch, tmp_path)
    _admin(c)
    sent = _capture_reset_email(monkeypatch)
    for _ in range(auth_mod.RESET_REQUEST_MAX):
        r = c.post("/api/auth/forgot", json={"email": "admin@x.co"})
        assert r.status_code == 200 and r.json() == {"ok": True}
    assert len(sent) == auth_mod.RESET_REQUEST_MAX

    # one more, over the limit: still {"ok": true}, but nothing is sent
    r = c.post("/api/auth/forgot", json={"email": "admin@x.co"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert len(sent) == auth_mod.RESET_REQUEST_MAX


def test_forgot_email_delivery_failure_does_not_change_the_response(monkeypatch, tmp_path):
    """Delivery failing must look identical to delivery succeeding — otherwise a
    down mail provider becomes an oracle for "this email exists"."""
    from engraphis.inspector import webhooks as WH

    def boom(to, name, reset_url):
        raise RuntimeError("simulated Resend outage")

    monkeypatch.setattr(WH, "email_configured", lambda: True)
    monkeypatch.setattr(WH, "send_password_reset_email", boom)
    c = _client(monkeypatch, tmp_path)
    _admin(c)
    r = c.post("/api/auth/forgot", json={"email": "admin@x.co"})
    assert r.status_code == 200 and r.json() == {"ok": True}


def test_reset_route_is_reachable_while_signed_out(monkeypatch, tmp_path):
    """/forgot and /reset must be public (no session cookie required) — that's the
    whole point: they're the way back in when you're locked out."""
    c = _client(monkeypatch, tmp_path)
    _admin(c)
    anon = TestClient(c.app)
    assert anon.post("/api/auth/forgot", json={"email": "admin@x.co"}).status_code == 200
    r = anon.post("/api/auth/reset", json={"token": "whatever-invalid-token-1234",
                                           "password": "brandnewpass1"})
    assert r.status_code == 400   # rejected for being a bad token, NOT for being unauthenticated
