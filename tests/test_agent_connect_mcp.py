"""Agent connect — MCP-over-HTTP at /mcp (stacked on the agent-connect PR).

An MCP-capable agent (Claude Code, Cursor, ...) points one URL at the cloud instance:
``https://team.engraphis.com/mcp`` with a per-user bearer token. The MCP tools reuse the
dashboard's single MemoryService (one writer — no second SQLite connection), so a memory
written via MCP immediately appears in the dashboard. ``/mcp`` is Team-gated (402 without a
Team license) and member-authenticated (401 without a token), matching ``/api/remember``.

These tests speak the streamable-http JSON-RPC protocol directly over the TestClient (no
real socket needed). The dashboard app's lifespan must run (TestClient used as a context
manager) so the MCP session manager's task group initializes.
"""
import json
import time

import pytest

pytest.importorskip("fastapi", reason="full-stack extra not installed")
pytest.importorskip("httpx", reason="httpx not installed")
pytest.importorskip("mcp", reason="mcp extra not installed")

from tests.team_client import InvitationTestClient as TestClient  # noqa: E402

from engraphis import licensing as lic  # noqa: E402
from engraphis.config import settings  # noqa: E402
from engraphis.licensing import compose_key, ed25519_public_key  # noqa: E402
from engraphis.service import MemoryService  # noqa: E402

_SECRET = bytes(range(32))
_PROTO = "2024-11-05"


def _team_key(seats: int = 5) -> str:
    return compose_key({"v": 1, "plan": "team", "email": "w@x.co", "seats": seats,
                        "issued": int(time.time()),
                        "expires": int(time.time() + 365 * 86400)}, _SECRET)


def _seed(db_path: str) -> None:
    MemoryService.create(db_path).remember(
        "The team uses Postgres 16 for the main database.", workspace="demo",
        scope="workspace", title="DB choice")


def _client(monkeypatch, tmp_path, *, key=None):
    db = str(tmp_path / "mcp.db")
    monkeypatch.setattr(settings, "db_path", db)
    monkeypatch.setattr(settings, "embed_model", "")
    monkeypatch.setenv("ENGRAPHIS_EMBED_MODEL", "")
    monkeypatch.setenv("ENGRAPHIS_TEAM_MODE", "1")
    monkeypatch.setenv("ENGRAPHIS_DASHBOARD_URL", "http://testserver")
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


def _mint(c, label="mcp-agent") -> str:
    r = c.post("/api/auth/token", json={"label": label})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _h(token):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream"}


def _init(c, token):
    """Run the MCP initialize handshake; return headers carrying the session id."""
    r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": _PROTO, "capabilities": {},
                         "clientInfo": {"name": "engraphis-test", "version": "1"}}},
              headers=_h(token))
    assert r.status_code == 200, r.text
    sid = r.headers.get("mcp-session-id")
    assert sid, "no mcp-session-id on initialize"
    h = {**_h(token), "Mcp-Session-Id": sid}
    c.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"},
           headers=h)
    return h


def _rpc(c, h, method, params=None, id=2, status_code=200):
    r = c.post("/mcp", json={"jsonrpc": "2.0", "id": id, "method": method,
                            "params": params or {}}, headers=h)
    assert r.status_code == status_code, r.text
    return r


def test_mcp_requires_auth_401(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path, key=_team_key()) as c:
        _setup_admin(c)
        c.cookies.clear()  # no cookie, no token -> the /mcp gate refuses before MCP
        r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": _PROTO, "capabilities": {},
                             "clientInfo": {"name": "t", "version": "1"}}})
        assert r.status_code == 401  # no token, no cookie -> refused


def test_per_user_bearer_scheme_is_case_insensitive(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path, key=_team_key()) as c:
        _setup_admin(c)
        token = _mint(c)
        c.cookies.clear()
        response = c.get(
            "/api/memories", headers={"Authorization": "bearer %s" % token})
        assert response.status_code == 200


def test_mcp_rejects_browser_cookie_without_bearer(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path, key=_team_key()) as c:
        _setup_admin(c)  # leaves a valid dashboard session cookie in the client
        r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": _PROTO, "capabilities": {},
                             "clientInfo": {"name": "t", "version": "1"}}})
        assert r.status_code == 401


def test_mcp_requires_team_license_402(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path, key=None) as c:
        # Entitlement is checked before authentication, so this test needs no licensed
        # bootstrap user or valid token (and must not depend on a developer's local key).
        r = c.post("/mcp/", json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": _PROTO, "capabilities": {},
                             "clientInfo": {"name": "t", "version": "1"}}})
        assert r.status_code == 402
        assert r.json()["feature"] == "team"



def test_mcp_viewer_token_can_initialize(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path, key=_team_key()) as c:
        _setup_admin(c)
        member = c.post("/api/auth/users", json={"email": "viewer@x.co", "name": "Viewer",
                         "password": "viewerpass1", "role": "member"}).json()["user"]
        c.post("/api/auth/logout")
        assert c.post("/api/auth/login", json={"email": "viewer@x.co",
                      "password": "viewerpass1"}).status_code == 200
        token = _mint(c, label="viewer-agent")
        c.post("/api/auth/logout")
        c.post("/api/auth/login", json={"email": "admin@x.co",
                                        "password": "supersecret1"})
        assert c.post("/api/auth/users/update",
                      json={"user_id": member["id"], "role": "viewer"}).status_code == 200
        c.cookies.clear()
        r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": _PROTO, "capabilities": {},
                             "clientInfo": {"name": "t", "version": "1"}}},
                   headers=_h(token))
        assert r.status_code == 200

def test_mcp_rejects_unconfigured_host(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path, key=_team_key()) as c:
        _setup_admin(c)
        token = _mint(c)
        c.cookies.clear()
        r = c.post("/mcp/", json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": _PROTO, "capabilities": {},
                             "clientInfo": {"name": "t", "version": "1"}}},
                   headers={**_h(token), "Host": "attacker.invalid"})
        assert r.status_code == 421


def test_dashboard_url_rejects_embedded_credentials(monkeypatch):
    from engraphis.dashboard_app import _mcp_transport_security
    from engraphis.mcp_server import mcp

    monkeypatch.setenv("ENGRAPHIS_DASHBOARD_URL", "https://:secret@dash.example.com")
    with pytest.raises(ValueError, match="without userinfo"):
        _mcp_transport_security(mcp)


def test_mcp_handshake_lists_engraphis_tools(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path, key=_team_key()) as c:
        _setup_admin(c)
        token = _mint(c)
        c.cookies.clear()
        h = _init(c, token)
        r = _rpc(c, h, "tools/list")
        assert "engraphis_remember" in r.text
        assert "engraphis_recall" in r.text


def test_mcp_tools_enforce_team_roles(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path, key=_team_key()) as c:
        _setup_admin(c)
        admin_token = _mint(c, label="admin-agent")
        member = c.post("/api/auth/users", json={
            "email": "agent@x.co", "name": "Agent", "password": "memberpass1",
            "role": "member",
        }).json()["user"]

        c.post("/api/auth/logout")
        assert c.post("/api/auth/login", json={
            "email": "agent@x.co", "password": "memberpass1",
        }).status_code == 200
        token = _mint(c, label="role-agent")

        # Existing tokens immediately inherit role changes. Demote this token's owner to
        # viewer, then prove reads still work while a mutating MCP tool is refused.
        c.post("/api/auth/logout")
        assert c.post("/api/auth/login", json={
            "email": "admin@x.co", "password": "supersecret1",
        }).status_code == 200
        assert c.post("/api/auth/users/update", json={
            "user_id": member["id"], "role": "viewer",
        }).status_code == 200
        c.cookies.clear()
        h = _init(c, token)
        assert "Postgres 16" in _rpc(
            c, h, "tools/call",
            {"name": "engraphis_recall",
             "arguments": {"query": "database", "workspace": "demo"}},
            id=20,
        ).text
        for rpc_id, tool_name, arguments in (
            (25, "engraphis_answer", {"query": "database", "workspace": "demo"}),
            (26, "engraphis_proactive_context", {"workspace": "demo"}),
        ):
            assert _rpc(
                c, h, "tools/call",
                {"name": tool_name, "arguments": arguments},
                id=rpc_id,
            ).status_code == 200
        denied = _rpc(
            c, h, "tools/call",
            {"name": "engraphis_remember",
             "arguments": {"content": "viewer must not write", "workspace": "demo"}},
            id=21,
            status_code=403,
        )
        assert "requires the member role" in denied.text
        hijack = _rpc(
            c, {**h, "Authorization": f"Bearer {admin_token}"}, "tools/list",
            id=24,
            status_code=403,
        )
        assert "belongs to another user" in hijack.text

        # Promote the same token owner back to member. Writes are now allowed, but the
        # admin-only consolidation sweep remains unavailable.
        assert c.post("/api/auth/login", json={
            "email": "admin@x.co", "password": "supersecret1",
        }).status_code == 200
        assert c.post("/api/auth/users/update", json={
            "user_id": member["id"], "role": "member",
        }).status_code == 200
        c.cookies.clear()
        allowed = _rpc(
            c, h, "tools/call",
            {"name": "engraphis_remember",
             "arguments": {"content": "member may write", "workspace": "demo"}},
            id=22,
        )
        assert "stored" in allowed.text
        admin_only = _rpc(
            c, h, "tools/call",
            {"name": "engraphis_consolidate",
             "arguments": {"workspace": "demo", "dry_run": True}},
            id=23,
            status_code=403,
        )
        assert "requires the admin role" in admin_only.text
        index_admin_only = _rpc(
            c, h, "tools/call",
            {"name": "engraphis_index_repo",
             "arguments": {"workspace": "demo", "repo": "sample",
                           "root_path": str(tmp_path)}},
            id=27,
            status_code=403,
        )
        assert "requires the admin role" in index_admin_only.text
        postgres_admin_only = _rpc(
            c, h, "tools/call",
            {"name": "engraphis_ingest_postgres_schema",
             "arguments": {"dsn": "postgresql://db.invalid/app",
                           "workspace": "demo"}},
            id=28,
            status_code=403,
        )
        assert "requires the admin role" in postgres_admin_only.text


def test_mcp_write_shares_the_dashboard_store(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path, key=_team_key()) as c:
        _setup_admin(c)
        token = _mint(c)
        c.cookies.clear()
        h = _init(c, token)
        # write a memory via the MCP tool ...
        r = _rpc(c, h, "tools/call",
                 {"name": "engraphis_remember",
                  "arguments": {"content": "MCP wrote this cloud memory",
                                "workspace": "demo"}}, id=10)
        assert "stored" in r.text
        # ... and it is immediately recallable through the dashboard's HTTP API
        rec = c.get("/api/recall?q=MCP&workspace=demo", headers=_h(token))
        assert rec.status_code == 200
        assert any("MCP" in (m.get("content") or "")
                   for m in rec.json()["memories"])


def test_mcp_answer_returns_grounded_result(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path, key=_team_key()) as c:
        _setup_admin(c)
        token = _mint(c)
        c.cookies.clear()
        h = _init(c, token)
        r = _rpc(c, h, "tools/call",
                 {"name": "engraphis_answer",
                  "arguments": {"query": "Which database does the team use?",
                                "workspace": "demo"}}, id=11)
        assert "Postgres" in r.text
        event = json.loads(r.text.split("data: ", 1)[1])
        payload = json.loads(event["result"]["content"][0]["text"])
        assert payload["grounded"] is True


def test_connect_info_reports_mcp_available(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path, key=_team_key()) as c:
        _setup_admin(c)
        token = _mint(c)
        c.cookies.clear()
        ci = c.get("/api/auth/connect-info", headers=_h(token)).json()
        assert ci["mcp_over_http"] is True
        assert ci["mcp_url"].endswith("/mcp")


def test_connect_info_reports_actual_mount_state(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path, key=_team_key()) as c:
        _setup_admin(c)
        token = _mint(c)
        c.cookies.clear()
        c.app.state.mcp_over_http = False
        ci = c.get("/api/auth/connect-info", headers=_h(token)).json()
        assert ci["mcp_over_http"] is False
        assert ci["mcp_url"] is None
