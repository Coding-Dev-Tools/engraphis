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

from fastapi.testclient import TestClient  # noqa: E402

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


def _client(monkeypatch, tmp_path, *, key=None):
    db = str(tmp_path / "agent.db")
    monkeypatch.setattr(settings, "db_path", db)
    monkeypatch.setattr(settings, "embed_model", "")
    monkeypatch.setenv("ENGRAPHIS_EMBED_MODEL", "")
    monkeypatch.setenv("ENGRAPHIS_TEAM_MODE", "1")
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
        assert len(tok["token"]) > 30 and tok["id"].startswith("tok_")

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


def test_remember_requires_team_license_402(monkeypatch, tmp_path):
    # team mode ON but no Team license -> agent write is 402 ("need a team license")
    with _client(monkeypatch, tmp_path, key=None) as c:
        _setup_admin(c)  # bootstrap admin is exempt from the license gate
        token = _mint(c)["token"]
        c.cookies.clear()
        r = c.post("/api/remember", json={"content": "x", "workspace": "demo"},
                   headers={"Authorization": f"Bearer {token}"})
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