"""Team audit log + admin overview — the Team-tier accountability features.

Covers: login/user-management events are recorded; the audit + overview endpoints are
admin-only; seat usage is reported; CSV export works. Team mode requires a Team license
(honored here via the pytest-only ENGRAPHIS_LICENSE_PUBKEY override in tests/conftest.py).
"""
import time
from urllib.parse import parse_qs, urlsplit

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
    monkeypatch.setenv("ENGRAPHIS_TEAM_INVITES", "0")
    monkeypatch.setattr(lic, "_LICENSE_FILE", tmp_path / "license.key")
    key = _team_key(seats)
    monkeypatch.setenv("ENGRAPHIS_LICENSE_KEY", key)
    monkeypatch.setenv("ENGRAPHIS_LICENSE_PUBKEY", ed25519_public_key(_SECRET).hex())
    monkeypatch.setenv("ENGRAPHIS_RELAY_DB", str(tmp_path / "vendor-relay.db"))
    from engraphis.inspector import license_registry
    license_registry.record_issued(key)
    lic.current_license(refresh=True)
    svc = MemoryService.create(str(tmp_path / "dash.db"))
    from engraphis.routes import v2_api
    v2_api.set_service(svc)
    from engraphis.dashboard_app import create_app
    return TestClient(create_app())


def _admin(c):
    assert c.post("/api/auth/setup", json={"email": "admin@x.co", "name": "Ada",
                  "password": "supersecret1"}).status_code == 200


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


def _invitation_token(url: str) -> str:
    parts = urlsplit(url)
    assert parts.query == ""
    return parse_qs(parts.fragment)["invite_token"][0]


def test_invitation_http_contract_is_public_only_for_acceptance(monkeypatch, tmp_path):
    """Exercise every real endpoint without the legacy fixture adapter.

    Admins can create/list/resend/revoke, invitation secrets never appear in list/create
    responses, and only the one-time acceptance route is reachable while signed out.
    """
    from engraphis.inspector import webhooks as WH

    sent = []
    monkeypatch.setattr(WH, "email_configured", lambda: True)
    monkeypatch.setattr(
        WH, "send_team_invite_email",
        lambda to, name, role, invite_url="", **kwargs: sent.append(invite_url),
    )
    c = _client(monkeypatch, tmp_path, seats=3)
    monkeypatch.setenv("ENGRAPHIS_TEAM_INVITES", "1")
    _admin(c)

    anonymous = TestClient(c.app)
    assert anonymous.post("/api/auth/invitations", json={
        "email": "x@x.co", "name": "X", "role": "member"}).status_code == 401
    assert anonymous.get("/api/auth/invitations").status_code == 401
    assert anonymous.post("/api/auth/invitations/missing/resend").status_code == 401
    assert anonymous.delete("/api/auth/invitations/missing").status_code == 401
    assert anonymous.post("/api/auth/invitations/accept", json={
        "token": "not-a-real-invitation", "password": "recipient-pass-1",
    }).status_code == 400

    created = c.post("/api/auth/invitations", json={
        "email": "member@x.co", "name": "Member", "role": "member"})
    assert created.status_code == 200, created.text
    invite_id = created.json()["invitation"]["id"]
    assert "token" not in created.text and "token_hash" not in created.text
    assert "/#invite_token=" in sent[-1] and "?invite_token=" not in sent[-1]
    first_token = _invitation_token(sent[-1])

    resent = c.post(f"/api/auth/invitations/{invite_id}/resend")
    assert resent.status_code == 200 and resent.json()["ok"] is True
    second_token = _invitation_token(sent[-1])
    assert second_token != first_token
    assert anonymous.post("/api/auth/invitations/accept", json={
        "token": first_token, "password": "recipient-pass-1",
    }).status_code == 400
    accepted = anonymous.post("/api/auth/invitations/accept", json={
        "token": second_token, "password": "recipient-pass-1",
    })
    assert accepted.status_code == 200 and accepted.json()["user"]["role"] == "member"

    pending = c.post("/api/auth/invitations", json={
        "email": "pending@x.co", "name": "Pending", "role": "viewer"})
    pending_id = pending.json()["invitation"]["id"]
    pending_token = _invitation_token(sent[-1])
    listed = c.get("/api/auth/invitations")
    assert listed.status_code == 200
    assert "token" not in listed.text and "token_hash" not in listed.text

    # The accepted recipient now has a member session, but governance remains admin-only.
    assert anonymous.get("/api/auth/invitations").status_code == 403
    assert anonymous.post("/api/auth/invitations", json={
        "email": "nope@x.co", "name": "Nope", "role": "member"}).status_code == 403
    assert anonymous.post(
        f"/api/auth/invitations/{pending_id}/resend").status_code == 403
    assert anonymous.delete(f"/api/auth/invitations/{pending_id}").status_code == 403

    assert c.delete(f"/api/auth/invitations/{pending_id}").status_code == 200
    assert TestClient(c.app).post("/api/auth/invitations/accept", json={
        "token": pending_token, "password": "recipient-pass-1",
    }).status_code == 400

    # The compatibility alias must not let an admin choose another user's password.
    legacy = c.post("/api/auth/users", json={
        "email": "legacy@x.co", "name": "Legacy", "role": "member",
        "password": "admin-chosen-1",
    })
    assert legacy.status_code == 400 and "temporary passwords" in legacy.text


def test_invitation_acceptance_rechecks_a_reduced_seat_limit(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path, seats=2)
    _admin(c)
    invitation = c.app.state.auth_store.create_invitation(
        "member@x.co", "Member", "member",
        created_by=c.app.state.auth_store.list_users()[0]["id"], seat_limit=2)

    # The invite reserved seat two, then the account was downgraded to one seat.
    monkeypatch.setenv("ENGRAPHIS_LICENSE_KEY", _team_key(1))
    lic.current_license(refresh=True)
    recipient = TestClient(c.app)
    denied = recipient.post("/api/auth/invitations/accept", json={
        "token": invitation["token"], "password": "recipient-pass-1"})
    assert denied.status_code == 400 and "seat limit" in denied.text
    assert c.app.state.auth_store.count_active_users() == 1

    # The failed transaction did not consume the one-time token.
    monkeypatch.setenv("ENGRAPHIS_LICENSE_KEY", _team_key(2))
    lic.current_license(refresh=True)
    assert recipient.post("/api/auth/invitations/accept", json={
        "token": invitation["token"], "password": "recipient-pass-1"}).status_code == 200


def test_invalid_invitation_token_is_rejected_before_password_hash(monkeypatch, tmp_path):
    """The public invitation route must not expose PBKDF2 as a CPU-amplification primitive."""
    c = _client(monkeypatch, tmp_path)
    _admin(c)
    import engraphis.inspector.auth as auth_mod

    def expensive_hash_must_not_run(*args, **kwargs):
        pytest.fail("an invalid public invitation token reached PBKDF2")

    monkeypatch.setattr(auth_mod, "_hash_password", expensive_hash_must_not_run)
    denied = TestClient(c.app).post("/api/auth/invitations/accept", json={
        "token": "not-a-real-invitation-token", "password": "recipient-pass-1",
    })
    assert denied.status_code == 400
    assert "invalid or expired" in denied.json()["detail"]["error"]


def test_login_and_user_events_are_recorded(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _admin(c)                                   # -> team.setup + login.success
    _invite_and_accept(c)
    # a failed login attempt
    fresh = TestClient(c.app)
    assert fresh.post("/api/auth/login", json={"email": "m@x.co",
                      "password": "wrongwrong1"}).status_code == 401

    events = c.get("/api/auth/audit").json()["events"]
    actions = [e["action"] for e in events]
    assert "team.setup" in actions
    assert "login.success" in actions
    assert "user.invited" in actions
    assert "user.invitation_accepted" in actions
    assert "login.failed" in actions
    created = next(e for e in events if e["action"] == "user.invited")
    assert created["actor_email"] == "admin@x.co" and created["target"] == "m@x.co"


def test_add_user_sends_invite_email_and_reports_invited_true(monkeypatch, tmp_path):
    # local delivery configured -> add_user prefers it over the vendor relay
    monkeypatch.setenv("ENGRAPHIS_RESEND_API_KEY", "re_test")
    from engraphis.inspector import webhooks as WH
    captured = {}
    monkeypatch.setattr(
        WH, "send_team_invite_email",
        lambda to, name, role, invited_by="", invite_url="", **kwargs:
            captured.update(to=to, name=name, role=role, invited_by=invited_by,
                            invite_url=invite_url))

    c = _client(monkeypatch, tmp_path)
    monkeypatch.setenv("ENGRAPHIS_TEAM_INVITES", "1")
    _admin(c)
    r = c.post("/api/auth/invitations", json={
        "email": "m@x.co", "name": "Mo", "role": "member"})
    assert r.status_code == 200 and r.json()["invited"] is True
    assert captured["to"] == "m@x.co" and captured["name"] == "Mo"
    assert captured["role"] == "member" and captured["invited_by"] == "admin@x.co"
    assert "invite_token=" in captured["invite_url"]
    actions = [e["action"] for e in c.get("/api/auth/audit").json()["events"]]
    assert "user.invite_email_failed" not in actions


def test_add_user_invite_email_failure_does_not_block_account_creation(monkeypatch, tmp_path):
    monkeypatch.setenv("ENGRAPHIS_RESEND_API_KEY", "re_test")
    from engraphis.inspector import webhooks as WH

    def boom(to, name, role, **kwargs):
        raise RuntimeError("simulated Resend outage")

    monkeypatch.setattr(WH, "send_team_invite_email", boom)

    c = _client(monkeypatch, tmp_path)
    monkeypatch.setenv("ENGRAPHIS_TEAM_INVITES", "1")
    _admin(c)
    r = c.post("/api/auth/invitations", json={
        "email": "m@x.co", "name": "Mo", "role": "member"})
    # The seat stays recoverably pending; no inaccessible active account is created.
    assert r.status_code == 200 and r.json()["invited"] is False
    assert all(u["email"] != "m@x.co" for u in c.get("/api/auth/users").json()["users"])
    pending = c.get("/api/auth/invitations").json()["invitations"]
    assert pending[0]["email"] == "m@x.co" and pending[0]["delivery_state"] == "failed"
    events = c.get("/api/auth/audit").json()["events"]
    failed = next(e for e in events if e["action"] == "user.invite_email_failed")
    assert failed["actor_email"] == "admin@x.co" and failed["target"] == "m@x.co"


def test_add_user_falls_back_to_vendor_relay_when_no_local_email_configured(monkeypatch, tmp_path):
    # no ENGRAPHIS_RESEND_API_KEY / SMTP_* set -> add_user must fall back to relaying
    # the invite through the vendor's mail provider using THIS instance's own license key
    for var in ("ENGRAPHIS_RESEND_API_KEY", "ENGRAPHIS_SMTP_HOST",
               "ENGRAPHIS_SMTP_USER", "ENGRAPHIS_SMTP_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    from engraphis import cloud_license
    captured = {}

    def fake_send(base_url, key, to, name, role, invited_by, **kwargs):
        captured.update(base_url=base_url, key=key, to=to, name=name,
                        role=role, invited_by=invited_by, **kwargs)
        return True, ""

    monkeypatch.setattr(cloud_license, "send_team_invite", fake_send)
    monkeypatch.setattr(settings, "relay_url", "https://customer.example")
    monkeypatch.setenv("ENGRAPHIS_CLOUD_URL", "https://team.engraphis.com")

    c = _client(monkeypatch, tmp_path)
    monkeypatch.setenv("ENGRAPHIS_TEAM_INVITES", "1")
    _admin(c)
    r = c.post("/api/auth/invitations", json={
        "email": "m@x.co", "name": "Mo", "role": "member"})
    assert r.status_code == 200 and r.json()["invited"] is True
    assert captured["to"] == "m@x.co" and captured["invited_by"] == "admin@x.co"
    assert captured["key"]                       # the raw active license key was forwarded
    assert captured["base_url"] == "https://license.engraphis.com"
    assert "invite_token=" in captured["invite_url"]
    actions = [e["action"] for e in c.get("/api/auth/audit").json()["events"]]
    assert "user.invite_email_failed" not in actions


def test_add_user_relay_failure_is_recorded_but_does_not_block_account_creation(
        monkeypatch, tmp_path):
    for var in ("ENGRAPHIS_RESEND_API_KEY", "ENGRAPHIS_SMTP_HOST",
               "ENGRAPHIS_SMTP_USER", "ENGRAPHIS_SMTP_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    from engraphis import cloud_license
    monkeypatch.setattr(
        cloud_license, "send_team_invite",
        lambda *a, **k: (False, "daily invite-email limit reached for this license"))

    c = _client(monkeypatch, tmp_path)
    monkeypatch.setenv("ENGRAPHIS_TEAM_INVITES", "1")
    _admin(c)
    r = c.post("/api/auth/invitations", json={
        "email": "m@x.co", "name": "Mo", "role": "member"})
    assert r.status_code == 200 and r.json()["invited"] is False
    assert all(u["email"] != "m@x.co" for u in c.get("/api/auth/users").json()["users"])
    events = c.get("/api/auth/audit").json()["events"]
    failed = next(e for e in events if e["action"] == "user.invite_email_failed")
    assert "limit" in failed["detail"]


# ── invitations never carry the account-wide Team key ────────────────────────────────

def test_viewer_invite_never_carries_the_shared_team_license_key(monkeypatch, tmp_path):
    monkeypatch.setenv("ENGRAPHIS_RESEND_API_KEY", "re_test")
    from engraphis.inspector import webhooks as WH
    sent = []
    monkeypatch.setattr(
        WH, "send_team_invite_email",
        lambda to, name, role, invite_url="", **kwargs:
            sent.append({"to": to, "role": role, "invite_url": invite_url,
                         "kwargs": kwargs}))

    c = _client(monkeypatch, tmp_path)
    monkeypatch.setenv("ENGRAPHIS_TEAM_INVITES", "1")
    _admin(c)
    viewer = c.post("/api/auth/invitations", json={
        "email": "v@x.co", "name": "Vi", "role": "viewer"})
    member = c.post("/api/auth/invitations", json={
        "email": "m@x.co", "name": "Mo", "role": "member"})
    assert viewer.status_code == 200 and member.status_code == 200

    by_role = {entry["role"]: entry for entry in sent}
    assert "invite_token=" in by_role["viewer"]["invite_url"]
    assert "invite_token=" in by_role["member"]["invite_url"]
    assert "key" not in by_role["viewer"]["kwargs"]
    assert "key" not in by_role["member"]["kwargs"]
    actions = [e["action"] for e in c.get("/api/auth/audit").json()["events"]]
    assert "user.invite_no_license" not in actions


def test_viewer_invite_through_vendor_relay_reports_delivery(monkeypatch, tmp_path):
    """The customer fallback and real vendor endpoint agree on fragment-only links."""
    for var in ("ENGRAPHIS_RESEND_API_KEY", "ENGRAPHIS_SMTP_HOST",
                "ENGRAPHIS_SMTP_USER", "ENGRAPHIS_SMTP_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ENGRAPHIS_DASHBOARD_URL", "https://customer.example")
    from engraphis.inspector import webhooks as WH
    from tests.vendor_relay import wire_vendor_relay

    queued = []
    monkeypatch.setattr(
        WH, "queue_team_invite_email",
        lambda to, name, role, invited_by="", invite_url="", **kwargs:
            queued.append({"to": to, "role": role, "invited_by": invited_by,
                           "invite_url": invite_url, **kwargs}) or "eml_invite",
    )
    wire_vendor_relay(monkeypatch, tmp_path)

    c = _client(monkeypatch, tmp_path)
    monkeypatch.setenv("ENGRAPHIS_TEAM_INVITES", "1")
    _admin(c)
    r = c.post("/api/auth/invitations", json={
        "email": "v@x.co", "name": "Vi", "role": "viewer"})
    assert r.status_code == 200 and r.json()["invited"] is True
    assert len(queued) == 1 and queued[0]["to"] == "v@x.co"
    assert queued[0]["invite_url"].startswith(
        "https://customer.example/#invite_token=")
    assert "?invite_token=" not in queued[0]["invite_url"]
    assert "key" not in queued[0]


def test_role_change_and_disable_are_recorded(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _admin(c)
    _invite_and_accept(c)
    mid = [u["id"] for u in c.get("/api/auth/users").json()["users"]
           if u["email"] == "m@x.co"][0]
    assert c.post("/api/auth/users/update",
                  json={"user_id": mid, "role": "viewer"}).status_code == 200
    assert c.post("/api/auth/users/update",
                  json={"user_id": mid, "disabled": True}).status_code == 200
    actions = [e["action"] for e in c.get("/api/auth/audit").json()["events"]]
    assert "user.role_changed" in actions and "user.disabled" in actions
    # filter works
    only = c.get("/api/auth/audit?action=user.disabled").json()["events"]
    assert only and all(e["action"] == "user.disabled" for e in only)


def test_delete_is_recorded_and_target_survives_the_row(monkeypatch, tmp_path):
    """The audit trail must still show who was removed even though the row itself
    (and its email) is gone — the row's email is captured into `target` beforehand."""
    c = _client(monkeypatch, tmp_path)
    _admin(c)
    _invite_and_accept(c)
    mid = [u["id"] for u in c.get("/api/auth/users").json()["users"]
           if u["email"] == "m@x.co"][0]
    assert c.post("/api/auth/users/delete", json={"user_id": mid}).status_code == 200
    events = c.get("/api/auth/audit").json()["events"]
    deleted = next(e for e in events if e["action"] == "user.deleted")
    assert deleted["actor_email"] == "admin@x.co" and deleted["target"] == "m@x.co"


def test_audit_and_overview_are_admin_only(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _admin(c)
    _invite_and_accept(c, email="v@x.co", name="Vi", role="viewer",
                       password="viewerpass12")
    viewer = TestClient(c.app)
    assert viewer.post("/api/auth/login", json={"email": "v@x.co",
                       "password": "viewerpass12"}).status_code == 200
    # a viewer cannot read the audit log or the overview
    assert viewer.get("/api/auth/audit").status_code == 403
    assert viewer.get("/api/auth/overview").status_code == 403


def test_overview_reports_seat_usage(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path, seats=3)
    _admin(c)
    _invite_and_accept(c)
    ov = c.get("/api/auth/overview").json()
    assert ov["seats"]["limit"] == 3
    assert ov["seats"]["used"] == 2          # admin + member
    assert ov["seats"]["available"] == 1
    assert {m["email"] for m in ov["members"]} == {"admin@x.co", "m@x.co"}
    # admin has a last_active (they logged in at setup); activity mix is populated
    admin_row = next(m for m in ov["members"] if m["email"] == "admin@x.co")
    assert admin_row["last_active"] is not None
    assert ov["activity"].get("login.success", 0) >= 1


def test_audit_csv_export(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _admin(c)
    r = c.get("/api/auth/audit/export")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    body = r.text.splitlines()
    assert body[0] == "ts,iso_utc,actor_id,actor_email,action,target,detail,ip"
    assert any("team.setup" in line for line in body[1:])


def test_audit_export_neutralizes_csv_formula_injection(monkeypatch, tmp_path):
    """An UNauthenticated failed-login attempt seeds actor_email into the audit log; the
    CSV export must defuse spreadsheet-formula injection (CWE-1236)."""
    c = _client(monkeypatch, tmp_path)
    _admin(c)
    evil = "=1+cmd@evil.co"   # space-free so it passes the email regex; a formula in Excel
    anon = TestClient(c.app)
    r = anon.post("/api/auth/login", json={"email": evil, "password": "whatever12"})
    assert r.status_code == 401   # recorded as login.failed with actor_email=evil
    csv_text = c.get("/api/auth/audit/export").text
    # the cell must be quote-prefixed; no bare cell may start with the formula
    assert "'" + evil in csv_text
    assert ("," + evil) not in csv_text and not csv_text.startswith(evil)
