"""Team audit log + admin overview — the Team-tier accountability features.

Covers: login/user-management events are recorded; the audit + overview endpoints are
admin-only; seat usage is reported; CSV export works. Team mode requires a Team license
(honored here via the pytest-only ENGRAPHIS_LICENSE_PUBKEY override in tests/conftest.py).
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


def _admin(c):
    assert c.post("/api/auth/setup", json={"email": "admin@x.co", "name": "Ada",
                  "password": "supersecret1"}).status_code == 200


def test_login_and_user_events_are_recorded(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _admin(c)                                   # -> team.setup + login.success
    # a member is created by the admin
    assert c.post("/api/auth/users", json={"email": "m@x.co", "name": "Mo",
                  "password": "anotherpass1", "role": "member"}).status_code == 200
    # a failed login attempt
    fresh = TestClient(c.app)
    assert fresh.post("/api/auth/login", json={"email": "m@x.co",
                      "password": "wrongwrong1"}).status_code == 401

    events = c.get("/api/auth/audit").json()["events"]
    actions = [e["action"] for e in events]
    assert "team.setup" in actions
    assert "login.success" in actions
    assert "user.created" in actions
    assert "login.failed" in actions
    # the user.created event names the admin as actor and the new user as target
    created = next(e for e in events if e["action"] == "user.created")
    assert created["actor_email"] == "admin@x.co" and created["target"] == "m@x.co"


def test_add_user_sends_invite_email_and_reports_invited_true(monkeypatch, tmp_path):
    # local delivery configured -> add_user prefers it over the vendor relay
    monkeypatch.setenv("ENGRAPHIS_RESEND_API_KEY", "re_test")
    from engraphis.inspector import webhooks as WH
    captured = {}
    monkeypatch.setattr(
        WH, "send_team_invite_email",
        lambda to, name, role, invited_by="", key="", dashboard_url=None:
            captured.update(to=to, name=name, role=role, invited_by=invited_by))

    c = _client(monkeypatch, tmp_path)
    _admin(c)
    r = c.post("/api/auth/users", json={"email": "m@x.co", "name": "Mo",
              "password": "anotherpass1", "role": "member"})
    assert r.status_code == 200 and r.json()["invited"] is True
    assert captured["to"] == "m@x.co" and captured["name"] == "Mo"
    assert captured["role"] == "member" and captured["invited_by"] == "admin@x.co"
    actions = [e["action"] for e in c.get("/api/auth/audit").json()["events"]]
    assert "user.invite_email_failed" not in actions


def test_add_user_invite_email_failure_does_not_block_account_creation(monkeypatch, tmp_path):
    monkeypatch.setenv("ENGRAPHIS_RESEND_API_KEY", "re_test")
    from engraphis.inspector import webhooks as WH

    def boom(to, name, role, invited_by="", key="", dashboard_url=None):
        raise RuntimeError("simulated Resend outage")

    monkeypatch.setattr(WH, "send_team_invite_email", boom)

    c = _client(monkeypatch, tmp_path)
    _admin(c)
    r = c.post("/api/auth/users", json={"email": "m@x.co", "name": "Mo",
              "password": "anotherpass1", "role": "member"})
    # the account still gets created — a delivery failure must never lose the user
    assert r.status_code == 200 and r.json()["invited"] is False
    assert any(u["email"] == "m@x.co" for u in c.get("/api/auth/users").json()["users"])
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

    def fake_send(base_url, key, to, name, role, invited_by, dashboard_url=""):
        captured.update(base_url=base_url, key=key, to=to, name=name,
                        role=role, invited_by=invited_by, dashboard_url=dashboard_url)
        return True, ""

    monkeypatch.setattr(cloud_license, "send_team_invite", fake_send)
    monkeypatch.setattr(settings, "relay_url", "https://customer.example")
    monkeypatch.setenv("ENGRAPHIS_CLOUD_URL", "https://team.engraphis.com")

    c = _client(monkeypatch, tmp_path)
    _admin(c)
    r = c.post("/api/auth/users", json={"email": "m@x.co", "name": "Mo",
              "password": "anotherpass1", "role": "member"})
    assert r.status_code == 200 and r.json()["invited"] is True
    assert captured["to"] == "m@x.co" and captured["invited_by"] == "admin@x.co"
    assert captured["key"]                       # the raw active license key was forwarded
    assert captured["base_url"] == "https://team.engraphis.com"
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
    _admin(c)
    r = c.post("/api/auth/users", json={"email": "m@x.co", "name": "Mo",
              "password": "anotherpass1", "role": "member"})
    assert r.status_code == 200 and r.json()["invited"] is False
    assert any(u["email"] == "m@x.co" for u in c.get("/api/auth/users").json()["users"])
    events = c.get("/api/auth/audit").json()["events"]
    failed = next(e for e in events if e["action"] == "user.invite_email_failed")
    assert "limit" in failed["detail"]


def test_role_change_and_disable_are_recorded(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _admin(c)
    c.post("/api/auth/users", json={"email": "m@x.co", "name": "Mo",
           "password": "anotherpass1", "role": "member"})
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
    c.post("/api/auth/users", json={"email": "m@x.co", "name": "Mo",
           "password": "anotherpass1", "role": "member"})
    mid = [u["id"] for u in c.get("/api/auth/users").json()["users"]
           if u["email"] == "m@x.co"][0]
    assert c.post("/api/auth/users/delete", json={"user_id": mid}).status_code == 200
    events = c.get("/api/auth/audit").json()["events"]
    deleted = next(e for e in events if e["action"] == "user.deleted")
    assert deleted["actor_email"] == "admin@x.co" and deleted["target"] == "m@x.co"


def test_audit_and_overview_are_admin_only(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _admin(c)
    c.post("/api/auth/users", json={"email": "v@x.co", "name": "Vi",
           "password": "viewerpass12", "role": "viewer"})
    viewer = TestClient(c.app)
    assert viewer.post("/api/auth/login", json={"email": "v@x.co",
                       "password": "viewerpass12"}).status_code == 200
    # a viewer cannot read the audit log or the overview
    assert viewer.get("/api/auth/audit").status_code == 403
    assert viewer.get("/api/auth/overview").status_code == 403


def test_overview_reports_seat_usage(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path, seats=3)
    _admin(c)
    c.post("/api/auth/users", json={"email": "m@x.co", "name": "Mo",
           "password": "anotherpass1", "role": "member"})
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
