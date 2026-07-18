"""Commercial layer of the Inspector — license gating (402), team auth, roles, seats.
Skips cleanly on the numpy-only CI gate, like test_inspector.py."""
import time

import pytest

pytest.importorskip("fastapi", reason="full-stack extra not installed")
from fastapi.testclient import TestClient  # noqa: E402

from engraphis import licensing  # noqa: E402
from engraphis.config import settings  # noqa: E402
from engraphis.inspector.app import create_app  # noqa: E402
from engraphis.inspector.auth import AuthStore  # noqa: E402
from engraphis.licensing import compose_key, ed25519_public_key  # noqa: E402
from engraphis.service import MemoryService, set_current_user  # noqa: E402

SECRET = bytes(range(32))
PW = "hunter2hunter2"  # ≥ 10 chars


def _key(plan="pro", seats=10, days=365, **kw):
    payload = {"v": 1, "plan": plan, "email": "buyer@x.co", "seats": seats,
               "issued": int(time.time()),
               "expires": int(time.time() + days * 86400) if days else None}
    payload.update(kw)
    return compose_key(payload, SECRET)


@pytest.fixture()
def make_client(monkeypatch):
    """Factory: build an Inspector TestClient in a given commercial configuration."""
    def _make(*, key=None, team_mode=False, token=""):
        monkeypatch.setenv("ENGRAPHIS_LICENSE_PUBKEY", ed25519_public_key(SECRET).hex())
        if key:
            monkeypatch.setenv("ENGRAPHIS_LICENSE_KEY", key)
        else:
            monkeypatch.delenv("ENGRAPHIS_LICENSE_KEY", raising=False)
        licensing.current_license(refresh=True)
        monkeypatch.setattr(settings, "api_token", token)
        monkeypatch.setattr(settings, "team_mode", team_mode)
        svc = MemoryService.create(":memory:")
        out = svc.remember("The rate limit is 500 rpm.", workspace="acme", repo="api")
        app = create_app(svc, AuthStore(":memory:", iterations=1_000))
        return app, TestClient(app), out["id"]
    yield _make
    licensing.current_license(refresh=True)


# ── free tier: everything existing works; paid surfaces upsell, never break ──────────

def test_free_tier_gates_pro_endpoints_with_402(make_client):
    _, c, _ = make_client()
    r = c.get("/api/analytics", params={"workspace": "acme"})
    assert r.status_code == 402
    body = r.json()
    assert body["upgrade"] is True and "purchase_url" in body
    # structured upgrade payload — the client renders a banner, not a bare error
    assert body["feature"] == "analytics" and body["tier_required"] == "pro"
    assert body["upgrade_url"].startswith("https://")
    assert c.get("/api/export", params={"workspace": "acme"}).status_code == 402
    # the free product is untouched
    assert c.get("/api/recall", params={"q": "rate", "workspace": "acme"}).status_code == 200
    st = c.get("/api/auth/state").json()
    assert st["mode"] == "open" and st["license"]["plan"] == "free"


def test_402_upgrade_url_respects_env_override(make_client, monkeypatch):
    monkeypatch.setenv("ENGRAPHIS_UPGRADE_URL", "https://example.com/pricing")
    _, c, _ = make_client()
    body = c.get("/api/export", params={"workspace": "acme"}).json()
    assert body["upgrade_url"] == "https://example.com/pricing"
    assert body["feature"] == "export" and body["tier_required"] == "pro"
    # the license dialog's link follows the same knob
    st = c.get("/api/auth/state").json()
    assert st["license"]["upgrade_url"] == "https://example.com/pricing"


def test_team_mode_without_team_license_reports_locked_not_broken(make_client):
    _, c, _ = make_client(key=_key("pro"), team_mode=True)
    st = c.get("/api/auth/state").json()
    assert st["mode"] == "open"          # gracefully NOT team
    assert st["team_locked"] is True     # …and the UI knows to show the unlock path
    assert c.get("/api/recall", params={"q": "rate", "workspace": "acme"}).status_code == 200


# ── pro tier ──────────────────────────────────────────────────────────────────────────

def test_pro_key_unlocks_analytics_and_export(make_client):
    _, c, _ = make_client(key=_key("pro"))
    a = c.get("/api/analytics", params={"workspace": "acme"})
    assert a.status_code == 200
    data = a.json()
    assert data["totals"]["live"] == 1 and len(data["growth_weekly"]) == 12
    e = c.get("/api/export", params={"workspace": "acme"})
    assert e.status_code == 200
    assert "attachment" in e.headers.get("content-disposition", "")
    dump = e.json()
    assert dump["format"] == "engraphis-export/1" and dump["counts"]["memories"] == 1


def test_analytics_html_export_is_pro_gated_and_self_contained(make_client):
    _, free, _ = make_client()
    r = free.get("/api/analytics/export", params={"workspace": "acme"})
    assert r.status_code == 402 and r.json()["tier_required"] == "pro"

    _, c, _ = make_client(key=_key("pro"))
    r = c.get("/api/analytics/export", params={"workspace": "acme"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert 'attachment; filename="engraphis-analytics-acme-' in \
        r.headers.get("content-disposition", "")
    page = r.text
    assert page.startswith("<!doctype html>")
    assert "Engraphis analytics report" in page and "acme" in page
    from engraphis import __version__
    assert __version__ in page                      # version stamped
    assert "generated" in page.lower()              # generated-at timestamp
    # self-contained: inline CSS only — no scripts, no CDN/external fetches
    assert "<style>" in page and "<script" not in page
    assert "src=" not in page and "@import" not in page and "href=" not in page


def test_expired_key_degrades_to_free_with_reason(make_client):
    _, c, _ = make_client(key=_key("pro", days=-1))
    st = c.get("/api/auth/state").json()
    assert st["license"]["plan"] == "free"
    assert "expired" in st["license_error"]
    assert c.get("/api/analytics", params={"workspace": "acme"}).status_code == 402


# ── team tier: sessions, roles, seats ─────────────────────────────────────────────────

def _setup_admin(client):
    r = client.post("/api/auth/setup", json={
        "email": "admin@x.co", "name": "Admin", "password": PW})
    assert r.status_code == 200
    return r


def test_team_flow_setup_login_roles_and_seats(make_client):
    app, admin, mem_id = make_client(key=_key("team", seats=3), team_mode=True)

    st = admin.get("/api/auth/state").json()
    assert st["mode"] == "team" and st["setup_required"] is True
    # locked out before setup/login
    assert admin.get("/api/recall", params={"q": "x", "workspace": "acme"}).status_code == 401

    _setup_admin(admin)                                   # first user = admin + cookie
    assert admin.get("/api/auth/state").json()["user"]["role"] == "admin"
    assert admin.get("/api/recall",
                     params={"q": "rate", "workspace": "acme"}).status_code == 200
    # setup is one-shot
    r = admin.post("/api/auth/setup", json={"email": "e@x.co", "password": PW})
    assert r.status_code == 409

    # admin provisions a member and a viewer (3 seats total — at the limit now)
    for email, role in [("m@x.co", "member"), ("v@x.co", "viewer")]:
        r = admin.post("/api/auth/users", json={
            "email": email, "name": email, "password": PW, "role": role})
        assert r.status_code == 200, r.text
    # seat 4 exceeds the licensed 3
    r = admin.post("/api/auth/users", json={
        "email": "extra@x.co", "name": "x", "password": PW, "role": "viewer"})
    assert r.status_code == 400 and "seat limit" in r.json()["error"]

    member, viewer = TestClient(app), TestClient(app)
    assert member.post("/api/auth/login",
                       json={"email": "m@x.co", "password": PW}).status_code == 200
    assert viewer.post("/api/auth/login",
                       json={"email": "v@x.co", "password": PW}).status_code == 200

    govern = {"memory_id": mem_id, "workspace": "acme", "repo": "api"}
    assert viewer.get("/api/recall",
                      params={"q": "rate", "workspace": "acme"}).status_code == 200
    assert viewer.post("/api/pin", json=govern).status_code == 403       # read-only
    assert member.post("/api/pin", json=govern).status_code == 200       # governance ok
    assert member.get("/api/auth/users").status_code == 403              # not admin
    assert member.post("/api/consolidate",
                       json={"workspace": "acme"}).status_code == 403
    assert member.get("/api/export", params={"workspace": "acme"}).status_code == 403
    assert admin.get("/api/export", params={"workspace": "acme"}).status_code == 200

    # last-active-admin protection
    users = admin.get("/api/auth/users").json()["users"]
    admin_id = next(u["id"] for u in users if u["role"] == "admin")
    r = admin.post("/api/auth/users/update", json={"user_id": admin_id, "role": "member"})
    assert r.status_code == 400 and "last active admin" in r.json()["error"]

    # disable the viewer → their session dies with them
    viewer_id = next(u["id"] for u in users if u["email"] == "v@x.co")
    assert admin.post("/api/auth/users/update",
                      json={"user_id": viewer_id, "disabled": True}).status_code == 200
    assert viewer.get("/api/recall",
                      params={"q": "rate", "workspace": "acme"}).status_code == 401

    # logout revokes the admin session
    assert admin.post("/api/auth/logout").status_code == 200
    assert admin.get("/api/recall",
                     params={"q": "rate", "workspace": "acme"}).status_code == 401


def test_seat_limit_enforced_on_reenable(make_client):
    """A disabled seat is not a free pass around the licensed cap: disable a user, spend
    the freed seat on a replacement, then re-enabling the original must be refused —
    otherwise disable→add→re-enable lets a team exceed the seats it paid for."""
    app, admin, _ = make_client(key=_key("team", seats=2), team_mode=True)
    _setup_admin(admin)                                    # admin = seat 1 of 2

    r = admin.post("/api/auth/users", json={
        "email": "m@x.co", "name": "M", "password": PW, "role": "member"})
    assert r.status_code == 200, r.text                    # seat 2 of 2 (at the cap)
    member_id = next(u["id"] for u in admin.get("/api/auth/users").json()["users"]
                     if u["email"] == "m@x.co")

    assert admin.post("/api/auth/users/update",
                      json={"user_id": member_id, "disabled": True}).status_code == 200
    r = admin.post("/api/auth/users", json={
        "email": "m2@x.co", "name": "M2", "password": PW, "role": "member"})
    assert r.status_code == 200, r.text                    # freed seat spent → 2 active

    r = admin.post("/api/auth/users/update",
                   json={"user_id": member_id, "disabled": False})
    assert r.status_code == 400 and "seat limit" in r.json()["error"]


def test_login_throttle_locks_after_repeated_failures(make_client):
    app, admin, _ = make_client(key=_key("team"), team_mode=True)
    _setup_admin(admin)
    attacker = TestClient(app)
    for _ in range(5):
        r = attacker.post("/api/auth/login",
                          json={"email": "admin@x.co", "password": "wrong-password"})
        assert r.status_code == 401
    r = attacker.post("/api/auth/login",
                      json={"email": "admin@x.co", "password": "wrong-password"})
    assert r.status_code == 429
    # correct password is ALSO locked out during the window (no oracle)
    r = attacker.post("/api/auth/login", json={"email": "admin@x.co", "password": PW})
    assert r.status_code == 429


def test_bearer_token_still_works_as_service_account_in_team_mode(make_client):
    _, c, _ = make_client(key=_key("team"), team_mode=True, token="s3cret-token")
    _setup_admin(c)
    c.post("/api/auth/logout")
    r = c.get("/api/recall", params={"q": "rate", "workspace": "acme"},
              headers={"Authorization": "Bearer s3cret-token"})
    assert r.status_code == 200                            # scripts keep working
    r = c.get("/api/recall", params={"q": "rate", "workspace": "acme"},
              headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_personal_receipts_are_scoped_to_signed_in_user(make_client):
    app, alice, _ = make_client(key=_key("team", seats=2), team_mode=True)
    _setup_admin(alice)
    store = app.state.auth_store
    service = app.state.service
    alice_user = next(user for user in store.list_users()
                      if user["email"] == "admin@x.co")

    # Seed a receipt in Alice's personal folder without going around the same service
    # authorization chokepoint exercised by the HTTP receipt handler.
    set_current_user(alice_user)
    try:
        service.create_workspace("alice-private", visibility="personal")
        service.remember("Alice's private receipt", workspace="alice-private")
    finally:
        set_current_user(None)

    assert alice.get("/api/receipts",
                     params={"workspace": "alice-private"}).json()["entries"]

    assert alice.post("/api/auth/users", json={
        "email": "bob@x.co", "name": "Bob", "password": PW,
        "role": "viewer"}).status_code == 200
    bob_user = next(user for user in store.list_users()
                    if user["email"] == "bob@x.co")
    bob = TestClient(app)
    assert bob.post("/api/auth/login", json={
        "email": "bob@x.co", "password": PW}).status_code == 200

    denied = bob.get("/api/receipts", params={"workspace": "alice-private"})
    assert denied.status_code == 400
    assert denied.json()["error"] == \
        "workspace 'alice-private' is a personal folder of another user"

    token = store.create_api_token(
        bob_user["id"], label="receipt regression")["token"]
    denied = TestClient(app).get(
        "/api/receipts", params={"workspace": "alice-private"},
        headers={"Authorization": "Bearer " + token})
    assert denied.status_code == 400

    # A request without either credential remains anonymous after authenticated requests.
    anonymous = TestClient(app).get(
        "/api/receipts", params={"workspace": "alice-private"})
    assert anonymous.status_code == 401


def test_activate_endpoint_persists_and_unlocks(make_client, monkeypatch, tmp_path):
    monkeypatch.setattr(licensing, "_LICENSE_FILE", tmp_path / "license.key")
    _, c, _ = make_client()                                # free tier
    assert c.get("/api/analytics", params={"workspace": "acme"}).status_code == 402
    r = c.post("/api/license/activate", json={"key": "ENGR1.garbage.key"})
    assert r.status_code == 402                            # bad key → upgrade payload
    r = c.post("/api/license/activate", json={"key": _key("pro")})
    assert r.status_code == 200 and r.json()["license"]["plan"] == "pro"
    assert c.get("/api/analytics", params={"workspace": "acme"}).status_code == 200


def test_audit_attributes_governance_to_the_signed_in_user(make_client):
    """Team tier's compliance story: the audit trail answers WHO, not just what."""
    app, admin, mem_id = make_client(key=_key("team"), team_mode=True)
    _setup_admin(admin)
    admin.post("/api/auth/users", json={
        "email": "m@x.co", "name": "M", "password": PW, "role": "member"})
    member = TestClient(app)
    member.post("/api/auth/login", json={"email": "m@x.co", "password": PW})
    r = member.post("/api/pin", json={
        "memory_id": mem_id, "workspace": "acme", "repo": "api"})
    assert r.status_code == 200
    entries = admin.get("/api/audit", params={"workspace": "acme"}).json()["entries"]
    pin_entries = [e for e in entries if e["action"] == "pin"]
    assert pin_entries and pin_entries[0]["actor"] == "m@x.co"
