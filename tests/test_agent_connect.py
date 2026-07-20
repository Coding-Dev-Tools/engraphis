"""Agent connect: per-user API tokens + the Team-gated ``/api/remember`` write path.

A Team member mints a long-lived bearer token from the dashboard and uses it to store
memories on the cloud instance (no local Engraphis install). The write endpoint requires
the instance to hold an active Team license — a free / lapsed instance returns 402, so
"a Team license is required to connect" is enforced at the agent endpoint, not at login.
"""
import time

import pytest

pytest.importorskip("fastapi", reason="full-stack extra not installed")
pytest.importorskip("httpx", reason="httpx not installed")

from tests.team_client import InvitationTestClient as TestClient  # noqa: E402

from engraphis import licensing as lic  # noqa: E402
from engraphis.config import settings  # noqa: E402
from engraphis.licensing import compose_key, ed25519_public_key  # noqa: E402
from engraphis.service import MemoryService  # noqa: E402

_SECRET = bytes(range(32))


def _team_key(seats: int = 5) -> str:
    return compose_key({"v": 1, "plan": "team", "email": "w@x.co", "seats": seats,
                        "issued": int(time.time()),
                        "expires": int(time.time() + 365 * 86400)}, _SECRET)


def _seed(db_path: str) -> None:
    svc = MemoryService.create(db_path)
    svc.remember("The team uses Postgres 16 for the main database.", workspace="demo",
                 scope="workspace", title="DB choice")


def _client(monkeypatch, tmp_path, *, key=None, team_mode="1"):
    db = str(tmp_path / "agent.db")
    monkeypatch.setattr(settings, "db_path", db)
    monkeypatch.setattr(settings, "embed_model", "")
    monkeypatch.setattr(settings, "allowed_workspaces", [])
    monkeypatch.setenv("ENGRAPHIS_EMBED_MODEL", "")
    # The mounted relay persists opaque bundles separately from the app database. Keep
    # it inside this test's directory so repeated/parallel runs cannot leak bundles across
    # accounts that intentionally use the same deterministic test license.
    monkeypatch.setenv("ENGRAPHIS_RELAY_DB", str(tmp_path / "relay.db"))
    if team_mode is None:
        monkeypatch.delenv("ENGRAPHIS_TEAM_MODE", raising=False)
    else:
        monkeypatch.setenv("ENGRAPHIS_TEAM_MODE", team_mode)
    monkeypatch.setattr(lic, "_LICENSE_FILE", tmp_path / "license.key")
    if key:
        monkeypatch.setenv("ENGRAPHIS_LICENSE_KEY", key)
        monkeypatch.setenv("ENGRAPHIS_LICENSE_PUBKEY", ed25519_public_key(_SECRET).hex())
    else:
        monkeypatch.delenv("ENGRAPHIS_LICENSE_KEY", raising=False)
    lic.current_license(refresh=True)
    _seed(db)
    from engraphis.dashboard_app import create_app
    return TestClient(create_app())


def _setup_admin(c, email="admin@x.co", password="supersecret1") -> dict:
    r = c.post("/api/auth/setup", json={"email": email, "name": "Admin",
                                        "password": password})
    assert r.status_code == 200, r.text
    return r.json()["user"]


def _mint(c, label="claude-code") -> dict:
    r = c.post("/api/auth/token", json={"label": label})
    assert r.status_code == 200, r.text
    return r.json()


# ── token lifecycle ────────────────────────────────────────────────────────────
def test_token_lifecycle_and_connect_info(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path, key=_team_key()) as c:
        _setup_admin(c)
        tok = _mint(c)
        assert tok["token"].startswith("engr_ut_") and tok["id"].startswith("tok_")

        # listing never returns the raw token
        lst = c.get("/api/auth/tokens").json()["tokens"]
        assert len(lst) == 1 and lst[0]["id"] == tok["id"] and "token" not in lst[0]

        # connect-info describes the caller + the agent config
        ci = c.get("/api/auth/connect-info").json()
        assert ci["user"]["email"] == "admin@x.co"
        assert ci["api_base"].endswith("/api")
        assert "/api/remember" in ci["snippet"]

        # revoke, then it's gone (404 on a second revoke)
        assert c.delete(f"/api/auth/token/{tok['id']}").status_code == 200
        assert c.delete(f"/api/auth/token/{tok['id']}").status_code == 404


def test_expired_tokens_do_not_exhaust_the_active_token_limit(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path, key=_team_key()) as c:
        user = _setup_admin(c)
        store = c.app.state.auth_store
        assert c.post("/api/auth/token", json={
            "label": "empty", "scopes": [],
        }).status_code == 400
        for index in range(100):
            store.create_api_token(user["id"], label=f"expired-{index}")
        limited = c.post("/api/auth/token", json={"label": "one-too-many"})
        assert limited.status_code == 400 and "token limit" in limited.text
        store.conn.execute(
            "UPDATE api_tokens SET expires_at=0 WHERE user_id=?", (user["id"],))
        store.conn.commit()

        fresh = _mint(c, label="replacement")
        assert fresh["token"].startswith("engr_ut_")


def test_legacy_api_tokens_receive_the_v1_expiry_during_migration(monkeypatch, tmp_path):
    from engraphis.inspector.auth import API_TOKEN_TTL_SECONDS

    with _client(monkeypatch, tmp_path, key=_team_key()) as c:
        user = _setup_admin(c)
        store = c.app.state.auth_store
        created_at = time.time() - 123
        store.conn.execute(
            "INSERT INTO api_tokens(id,user_id,label,token_hash,created_at,expires_at,scopes) "
            "VALUES (?,?,?,?,?,NULL,'agent')",
            ("tok_legacy", user["id"], "legacy", "legacy-hash", created_at),
        )
        store.conn.commit()

        store._migrate_schema()
        migrated = store.conn.execute(
            "SELECT expires_at FROM api_tokens WHERE id='tok_legacy'").fetchone()[0]
        assert migrated == pytest.approx(created_at + API_TOKEN_TTL_SECONDS)


def test_scoped_team_sync_tokens_enforce_read_and_write(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path, key=_team_key()) as c:
        _setup_admin(c)
        admin_token = _mint(c, label="admin-sync")["token"]
        c.cookies.clear()
        admin_headers = {"Authorization": f"Bearer {admin_token}"}
        assert c.post(
            "/relay/v1/demo/bundles/bundle-admin.json",
            content=b"{}",
            headers=admin_headers,
        ).status_code == 200
        assert c.get("/relay/v1/demo/names", headers=admin_headers).json()["names"] == [
            "bundle-admin.json"
        ]

        assert c.post("/api/auth/login", json={
            "email": "admin@x.co", "password": "supersecret1",
        }).status_code == 200
        member = c.post("/api/auth/users", json={
            "email": "member-sync@x.co",
            "name": "Member",
            "role": "member",
            "password": "memberpass12",
        }).json()["user"]
        assert member["role"] == "member"
        c.post("/api/auth/logout")
        assert c.post("/api/auth/login", json={
            "email": "member-sync@x.co", "password": "memberpass12",
        }).status_code == 200
        member_token = _mint(c, label="member-sync")["token"]
        c.cookies.clear()
        member_headers = {"Authorization": f"Bearer {member_token}"}
        assert c.post(
            "/relay/v1/demo/bundles/bundle-member.json",
            content=b"{}",
            headers=member_headers,
        ).status_code == 200

        # Scope alone is not durable authority: a role downgrade takes effect on the
        # next relay request even though this older token still says sync:write.
        assert c.post("/api/auth/login", json={
            "email": "admin@x.co", "password": "supersecret1",
        }).status_code == 200
        assert c.post("/api/auth/users/update", json={
            "user_id": member["id"], "role": "viewer",
        }).status_code == 200
        c.cookies.clear()
        assert c.get("/relay/v1/demo/names", headers=member_headers).status_code == 200
        assert c.post(
            "/relay/v1/demo/bundles/bundle-viewer.json",
            content=b"{}",
            headers=member_headers,
        ).status_code == 403


def test_read_only_sync_policy_survives_process_environment_reset(monkeypatch, tmp_path):
    from engraphis.backends.sync_relay import has_sync_token, sync_read_only

    monkeypatch.setenv("ENGRAPHIS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("ENGRAPHIS_SYNC_TOKEN", raising=False)
    monkeypatch.delenv("ENGRAPHIS_SYNC_READ_ONLY", raising=False)
    with _client(monkeypatch, tmp_path, key=_team_key()) as c:
        _setup_admin(c)
        configured = c.post("/api/sync/token", json={
            "token": "engr_ut_" + "s" * 32, "read_only": True,
        })
        assert configured.status_code == 200 and configured.json()["read_only"] is True
        assert has_sync_token() is True and sync_read_only() is True

        # Simulate a fresh process with no environment override; the owner-only state
        # files remain on the persistent volume.
        monkeypatch.delenv("ENGRAPHIS_SYNC_READ_ONLY", raising=False)
        status = c.get("/api/sync/status")
        assert status.status_code == 200 and status.json()["read_only"] is True
        assert sync_read_only() is True

        removed = c.delete("/api/sync/token")
        assert removed.status_code == 200
        assert removed.json() == {"configured": False, "read_only": False}


def test_concurrent_sync_token_updates_do_not_interleave(monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from engraphis.backends import sync_relay
    from engraphis.routes import v2_api

    monkeypatch.delenv("ENGRAPHIS_SYNC_TOKEN", raising=False)
    monkeypatch.delenv("ENGRAPHIS_SYNC_READ_ONLY", raising=False)
    calls = []
    calls_lock = threading.Lock()
    start = threading.Barrier(2)

    def record(kind, value):
        with calls_lock:
            calls.append((threading.get_ident(), kind, value))
        time.sleep(0.01)  # encourage a context switch inside each multi-file update

    monkeypatch.setattr(sync_relay, "save_sync_read_only",
                        lambda enabled: record("policy", enabled))
    monkeypatch.setattr(sync_relay, "save_sync_token",
                        lambda token: record("token", token))

    def configure(token, read_only):
        start.wait()
        return v2_api.configure_sync_token(
            v2_api._SyncTokenReq(token=token, read_only=read_only))

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(configure, "engr_ut_" + "a" * 32, True),
            pool.submit(configure, "engr_ut_" + "b" * 32, False),
        ]
        assert all(future.result()["configured"] for future in futures)

    # Every operation for one request is contiguous: another request cannot overwrite
    # its policy between the restrictive sentinel and token replacement.
    owners = [entry[0] for entry in calls]
    transitions = sum(left != right for left, right in zip(owners, owners[1:]))
    assert transitions == 1


# ── agent write path ────────────────────────────────────────────────────────────
def test_remember_with_bearer_writes_to_cloud(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path, key=_team_key()) as c:
        _setup_admin(c)
        token = _mint(c)["token"]
        c.cookies.clear()  # bearer-only, like a headless agent with no browser session
        h = {"Authorization": f"Bearer {token}"}

        r = c.post("/api/remember", json={"content": "Redis caches the gateway.",
                                          "workspace": "demo"}, headers=h)
        assert r.status_code == 200, r.text

        # the write landed in the cloud store and is recallable with the same token
        rec = c.get("/api/recall?q=Redis&workspace=demo", headers=h)
        assert rec.status_code == 200
        assert any("Redis" in (m.get("content") or "")
                   for m in rec.json()["memories"])


def test_team_mode_defaults_on_when_environment_is_unset(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path, key=_team_key(), team_mode=None) as c:
        assert c.get("/api/auth/state").json()["enabled"] is True
        assert c.post(
            "/api/remember",
            json={"content": "x", "workspace": "demo"}).status_code == 401


def test_viewer_can_mint_read_only_api_token(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path, key=_team_key()) as c:
        _setup_admin(c)
        assert c.post("/api/auth/users", json={
            "email": "viewer@x.co", "name": "Viewer", "role": "viewer",
            "password": "viewerpass12",
        }).status_code == 200
        c.cookies.clear()
        assert c.post("/api/auth/login", json={
            "email": "viewer@x.co", "password": "viewerpass12",
        }).status_code == 200
        token = _mint(c, label="viewer-agent")["token"]
        headers = {"Authorization": f"Bearer {token}"}
        c.cookies.clear()

        assert c.get(
            "/api/recall?q=database&workspace=demo", headers=headers).status_code == 200
        assert c.post(
            "/api/remember", json={"content": "x", "workspace": "demo"},
            headers=headers).status_code == 403


def test_remember_requires_team_license_402(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "api_token", "service-token")
    with _client(monkeypatch, tmp_path, key=None) as c:
        r = c.post("/api/remember", json={"content": "x", "workspace": "demo"},
                   headers={"Authorization": "Bearer service-token"})
        assert r.status_code == 402
        assert r.json()["detail"]["feature"] == "team"


def test_remember_without_auth_401(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path, key=_team_key()) as c:
        _setup_admin(c)
        c.cookies.clear()  # no cookie, no bearer -> the team auth wall refuses it
        assert c.post("/api/remember",
                      json={"content": "x", "workspace": "demo"}).status_code == 401


def test_connect_info_verifies_a_bearer_token(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path, key=_team_key()) as c:
        _setup_admin(c)
        token = _mint(c)["token"]
        c.cookies.clear()
        # an agent can hit connect-info with its bearer to verify the token + discover base
        ci = c.get("/api/auth/connect-info",
                   headers={"Authorization": f"Bearer {token}"}).json()
        assert ci["user"]["email"] == "admin@x.co"


# ── disabled user's token is refused ────────────────────────────────────────────
def test_disabled_user_token_rejected(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path, key=_team_key(seats=5)) as c:
        _setup_admin(c)
        mem = c.post("/api/auth/users", json={"email": "mem@x.co", "name": "Mem",
                                              "password": "memberpass1",
                                              "role": "member"}).json()["user"]

        c.post("/api/auth/logout")
        assert c.post("/api/auth/login", json={"email": "mem@x.co",
                                               "password": "memberpass1"}).status_code == 200
        token = _mint(c, label="member-agent")["token"]
        h = {"Authorization": f"Bearer {token}"}
        assert c.post("/api/remember", json={"content": "m1", "workspace": "demo"},
                      headers=h).status_code == 200

        # admin disables the member
        c.post("/api/auth/logout")
        c.post("/api/auth/login", json={"email": "admin@x.co",
                                        "password": "supersecret1"})
        c.post("/api/auth/users/update", json={"user_id": mem["id"], "disabled": True})

        # the member's token is now inert (resolve_api_token rejects disabled owners).
        # Clear the admin's browser cookie so the bearer is the sole credential.
        c.cookies.clear()
        assert c.post("/api/remember", json={"content": "m2", "workspace": "demo"},
                      headers=h).status_code == 401


# ── instance-wide service token still bypasses (unchanged) ─────────────────────
def test_instance_bearer_bypass_still_works(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "api_token", "inst-secret")
    with _client(monkeypatch, tmp_path, key=_team_key()) as c:
        _setup_admin(c)
        c.cookies.clear()  # the instance service token is the sole credential here
        r = c.post("/api/remember", json={"content": "via instance token",
                                          "workspace": "demo"},
                   headers={"Authorization": "Bearer inst-secret"})
        assert r.status_code == 200, r.text
