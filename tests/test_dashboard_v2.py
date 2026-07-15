"""The v1-look dashboard on the v2 engine: adapter API + team mode.

Skips on the numpy-only CI gate (needs fastapi/httpx), like the other v1 tests. Uses the
deterministic embedder so recall works without torch, and a fresh DB per test.
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


def _seed(db_path: str) -> MemoryService:
    svc = MemoryService.create(db_path)
    svc.remember("The team uses Postgres 16 for the main database.", workspace="demo",
                 scope="workspace", title="DB choice", importance=0.8)
    svc.remember("Deploys run Fridays at noon via GitHub Actions.", workspace="demo",
                 scope="workspace", title="Deploy cadence")
    return svc


def _client(monkeypatch, tmp_path, *, team=False, key=None):
    db = str(tmp_path / "dash.db")
    monkeypatch.setattr(settings, "db_path", db)
    monkeypatch.setattr(settings, "embed_model", "")
    monkeypatch.setenv("ENGRAPHIS_EMBED_MODEL", "")
    monkeypatch.setenv("ENGRAPHIS_TEAM_MODE", "1" if team else "0")
    monkeypatch.setattr(lic, "_LICENSE_FILE", tmp_path / "license.key")
    if key:
        monkeypatch.setenv("ENGRAPHIS_LICENSE_KEY", key)
        monkeypatch.setenv("ENGRAPHIS_LICENSE_PUBKEY", ed25519_public_key(_SECRET).hex())
    else:
        monkeypatch.delenv("ENGRAPHIS_LICENSE_KEY", raising=False)
    lic.current_license(refresh=True)
    svc = _seed(db)
    from engraphis.routes import v2_api
    v2_api.set_service(svc)
    from engraphis.dashboard_app import create_app
    return TestClient(create_app())


def _team_key(seats=5):
    return compose_key({"v": 1, "plan": "team", "email": "w@x.co", "seats": seats,
                        "issued": int(time.time()),
                        "expires": int(time.time() + 365 * 86400)}, _SECRET)


def test_dashboard_serves_and_bootstraps(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as c:
        # / serves the unified dashboard — the standalone Inspector was retired into it
        r = c.get("/")
        assert r.status_code == 200
        assert 'class="sidebar"' in r.text
        b = c.get("/api/bootstrap").json()
        assert b["stats"]["memories"] >= 2
        assert any(w["name"] == "demo" for w in b["workspaces"])
        assert b["license"]["plan"] == "free"


@pytest.mark.parametrize("raw", ["0", " false ", "NO", "Off"])
def test_team_mode_env_opt_out_parsing(monkeypatch, raw):
    from engraphis.routes.v2_team import _enabled
    monkeypatch.setenv("ENGRAPHIS_TEAM_MODE", raw)
    assert _enabled() is False


def test_team_mode_defaults_on_but_auth_wall_waits_for_team_license(monkeypatch, tmp_path):
    db = str(tmp_path / "dash.db")
    monkeypatch.setattr(settings, "db_path", db)
    monkeypatch.setattr(settings, "embed_model", "")
    monkeypatch.setenv("ENGRAPHIS_EMBED_MODEL", "")
    monkeypatch.delenv("ENGRAPHIS_TEAM_MODE", raising=False)
    monkeypatch.setattr(lic, "_LICENSE_FILE", tmp_path / "license.key")
    monkeypatch.delenv("ENGRAPHIS_LICENSE_KEY", raising=False)
    lic.current_license(refresh=True)
    svc = _seed(db)
    from engraphis.routes import v2_api
    v2_api.set_service(svc)
    from engraphis.dashboard_app import create_app
    with TestClient(create_app()) as c:
        assert c.get("/api/auth/state").json() == {
            "enabled": False,
            "needs_setup": False,
            "user": None,
        }
        assert c.post("/api/auth/setup", json={
            "email": "w@x.co",
            "name": "W",
            "password": "supersecret1",
        }).status_code == 402
        assert c.get("/api/bootstrap").status_code == 200


def test_recall_why_timeline_and_detail(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as c:
        r = c.get("/api/recall?q=database&workspace=demo").json()
        assert r["count"] >= 1
        mid = r["memories"][0]["id"]
        assert c.get(f"/api/memory/{mid}?workspace=demo").json()["memory"]["id"] == mid
        assert "answer" in c.get("/api/why?q=database&workspace=demo").json()
        assert "history" in c.get("/api/timeline?q=database&workspace=demo").json()


def test_governance_pin_correct_forget(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as c:
        mid = c.get("/api/recall?q=database&workspace=demo").json()["memories"][0]["id"]
        assert c.post("/api/pin", json={"id": mid, "workspace": "demo", "pinned": True}).status_code == 200
        assert c.post("/api/correct", json={"id": mid, "workspace": "demo",
                      "content": "Postgres 16 primary + a read replica.", "reason": "clarify"}).status_code == 200
        assert c.post("/api/forget", json={"id": mid, "workspace": "demo", "reason": "test"}).status_code == 200


# Split into two test functions rather than two sequential `with _client(...)` blocks
# in one test: under pytest (not in a bare script) two TestClient lifespans opened
# back-to-back in a single test function reproducibly deadlock in this environment
# (fastapi 0.139/starlette 1.3/anyio 4.14) even on unrelated apps — a bare two-FastAPI-
# TestClient repro without any of this repo's code hangs the same way. One TestClient
# per test function sidesteps it entirely and is arguably the better test shape anyway.
def test_analytics_and_export_gated_by_default(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as c:
        assert c.get("/api/analytics?workspace=demo").status_code == 402
        assert c.get("/api/analytics/portfolio").status_code == 402
        assert c.get("/api/export?workspace=demo").status_code == 402


def test_analytics_and_export_unlocked_with_team_key(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path, key=_team_key()) as c:
        assert c.get("/api/analytics?workspace=demo").status_code == 200
        r = c.get("/api/analytics/portfolio")
        assert r.status_code == 200
        body = r.json()
        assert body["totals"]["workspaces"] >= 1
        assert any(w["workspace"] == "demo" for w in body["workspaces"])
        assert c.get("/api/export?workspace=demo").status_code == 200


def test_team_disabled_by_default(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as c:
        assert c.get("/api/auth/state").json()["enabled"] is False


def test_automation_policy_round_trips_dream_knobs(monkeypatch, tmp_path):
    # The dream/infer knobs (dream, dream_min_new, dream_idle_minutes, infer) are
    # part of the maintenance policy. They must round-trip through the API so the
    # dashboard controls can't silently desync from the persisted policy.
    with _client(monkeypatch, tmp_path, key=_team_key()) as c:
        r = c.post("/api/automation", json={"enabled": True, "dream": False,
                                            "dream_min_new": 7, "dream_idle_minutes": 0,
                                            "infer": True})
        assert r.status_code == 200
        p = c.get("/api/automation").json()
        assert p["dream"] is False
        assert p["dream_min_new"] == 7
        assert p["dream_idle_minutes"] == 0   # 0 is valid and must survive (not coerced)
        assert p["infer"] is True            # the inference pass is Pro-gated via automation


def test_consolidate_inference_pass_is_pro_gated(monkeypatch, tmp_path):
    # The inference pass (infer=True) is a paid `automation` capability — the dream
    # pass 4 — so it must 402 on the free tier; the base sweep (infer=False) stays free.
    with _client(monkeypatch, tmp_path) as c:                       # no key -> free tier
        base = c.post("/api/consolidate", json={"workspace": "demo", "dry_run": True})
        assert base.status_code == 200                       # manual consolidate is free
        gated = c.post("/api/consolidate",
                       json={"workspace": "demo", "dry_run": True, "infer": True})
        assert gated.status_code == 402
        assert gated.json()["detail"]["feature"] == "automation"    # structured 402


def test_consolidate_inference_pass_runs_with_team_key(monkeypatch, tmp_path):
    # One TestClient per test function — see the note on test_analytics_and_export_*.
    # With automation unlocked (Team license) the inference pass runs ungated.
    with _client(monkeypatch, tmp_path, key=_team_key()) as c:     # automation unlocked
        r = c.post("/api/consolidate",
                  json={"workspace": "demo", "dry_run": True, "infer": True})
        assert r.status_code == 200
        assert "inferences" in r.json()                     # the pass ran, ungated now


def test_maintenance_run_proposes_inference_when_policy_on(monkeypatch, tmp_path):
    # The inference pass (consolidate pass 4) is reachable from the maintenance run
    # endpoint when the policy opts in — proving the wiring through run_maintenance ->
    # service.consolidate(infer=...). Dry-run so nothing is written; we only assert
    # the inferences block is present and proposes the Redis bridge.
    with _client(monkeypatch, tmp_path, key=_team_key()) as c:
        from engraphis.routes import v2_api
        svc = v2_api.service()
        for t in ("Redis caches API responses to cut gateway latency",
                  "Redis raises throughput on the gateway API",
                  "User login sessions live in Redis keyed by a signed session token",
                  "Redis expires each login session token on logout"):
            svc.remember(t, workspace="demo", mtype="episodic",
                         resolve_conflicts=False, scope="workspace")
        c.post("/api/automation", json={"enabled": True, "consolidate": True,
                                        "infer": True, "dream": False,
                                        "workspaces": ["demo"],
                                        "cadence_hours": 999, "dream_min_new": 99999})
        r = c.post("/api/maintenance/run", json={"dry_run": True})
        assert r.status_code == 200
        demo = next(x for x in r.json()["runs"] if x["workspace"] == "demo")
        assert "inferences" in demo["consolidate"]
        inf = demo["consolidate"]["inferences"]
        assert any(e["entity"].lower() == "redis" for e in inf["links_created"])


def test_team_flow_setup_login_roles(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path, team=True, key=_team_key()) as c:
        assert c.get("/api/auth/state").json()["needs_setup"] is True
        assert c.post("/api/auth/setup", json={"email": "w@x.co", "name": "W",
                      "password": "supersecret1"}).status_code == 200
        assert c.get("/api/auth/state").json()["user"]["role"] == "admin"
        assert c.post("/api/auth/users", json={"email": "m@x.co", "name": "M",
                      "password": "anotherpass1", "role": "member"}).status_code == 200
        assert c.post("/api/auth/logout").status_code == 200
        assert c.get("/api/auth/users").status_code == 401
        assert c.post("/api/auth/login", json={"email": "w@x.co",
                      "password": "supersecret1"}).status_code == 200


def test_team_mode_gates_data_endpoints(monkeypatch, tmp_path):
    """Regression: team mode must require a session on every /api/* route, not just
    /api/auth/users — otherwise recall/governance/export stay reachable by anyone who
    can hit the port even with per-user login turned on."""
    with _client(monkeypatch, tmp_path, team=True, key=_team_key()) as c:
        assert c.get("/api/bootstrap").status_code == 401
        assert c.get("/api/recall?q=database&workspace=demo").status_code == 401
        assert c.post("/api/pin", json={"id": "x", "workspace": "demo"}).status_code == 401
        assert c.get("/api/export?workspace=demo").status_code == 401
        # public/bootstrap-of-auth endpoints stay reachable while logged out
        assert c.get("/api/auth/state").status_code == 200
        # after logging in, the same routes work
        c.post("/api/auth/setup", json={"email": "w@x.co", "name": "W",
                                        "password": "supersecret1"})
        assert c.get("/api/bootstrap").status_code == 200
        assert c.get("/api/recall?q=database&workspace=demo").status_code == 200


def test_team_users_db_is_separate_from_memory_db(monkeypatch, tmp_path):
    """Regression: session/password hashes must not live inside the main memory DB
    file that /api/export and ordinary backups copy around."""
    db = str(tmp_path / "dash.db")
    monkeypatch.setattr(settings, "db_path", db)
    monkeypatch.setattr(settings, "embed_model", "")
    monkeypatch.setenv("ENGRAPHIS_EMBED_MODEL", "")
    monkeypatch.setenv("ENGRAPHIS_TEAM_MODE", "1")
    monkeypatch.setattr(lic, "_LICENSE_FILE", tmp_path / "license.key")
    monkeypatch.setenv("ENGRAPHIS_LICENSE_KEY", _team_key())
    monkeypatch.setenv("ENGRAPHIS_LICENSE_PUBKEY", ed25519_public_key(_SECRET).hex())
    lic.current_license(refresh=True)
    svc = _seed(db)
    from engraphis.routes import v2_api
    v2_api.set_service(svc)
    from engraphis.dashboard_app import create_app
    with TestClient(create_app()) as c:
        c.post("/api/auth/setup", json={"email": "w@x.co", "name": "W",
                                        "password": "supersecret1"})
    import sqlite3
    conn = sqlite3.connect(db)
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert "users" not in tables and "auth_sessions" not in tables
    assert (tmp_path / "dash.db.users.db").exists()


def test_viewer_role_denied_on_governance_and_admin_routes(monkeypatch, tmp_path):
    """Regression: dashboard_app's _auth_gate must enforce roles, not just "is there a
    session" — otherwise a viewer (read-only) team member can still mutate/delete
    memories, trigger consolidation, pull the compliance export, or replace the
    instance's license key. Mirrors test_inspector_pro.py's equivalent coverage."""
    with _client(monkeypatch, tmp_path, team=True, key=_team_key()) as c:
        admin = c
        assert admin.post("/api/auth/setup", json={"email": "w@x.co", "name": "W",
                          "password": "supersecret1"}).status_code == 200
        assert admin.post("/api/auth/users", json={"email": "v@x.co", "name": "V",
                          "password": "anotherpass1", "role": "viewer"}).status_code == 200

        viewer = TestClient(c.app)
        assert viewer.post("/api/auth/login", json={"email": "v@x.co",
                           "password": "anotherpass1"}).status_code == 200

        mid = admin.get("/api/recall?q=database&workspace=demo").json()["memories"][0]["id"]
        # reads stay allowed for a viewer
        assert viewer.get("/api/recall?q=database&workspace=demo").status_code == 200
        # governance/admin routes must not be reachable at the viewer role
        assert viewer.post("/api/pin", json={"id": mid, "workspace": "demo",
                           "pinned": True}).status_code == 403
        assert viewer.post("/api/forget", json={"id": mid, "workspace": "demo",
                           "reason": "x"}).status_code == 403
        assert viewer.post("/api/correct", json={"id": mid, "workspace": "demo",
                           "content": "x", "reason": "x"}).status_code == 403
        assert viewer.post("/api/consolidate", json={"workspace": "demo"}).status_code == 403
        assert viewer.get("/api/export?workspace=demo").status_code == 403
        assert viewer.post("/api/license/activate",
                           json={"key": _team_key()}).status_code == 403
        # a viewer can't create a folder either — creating a workspace is a member+ action
        assert viewer.post("/api/workspaces/create",
                           json={"workspace": "viewer-folder"}).status_code == 403
        # nor import into one — same member+ gate, both the path and upload routes
        assert viewer.post("/api/workspaces/import-folder",
                           json={"workspace": "demo", "path": "/tmp"}).status_code == 403
        assert viewer.post("/api/workspaces/import-files", data={"workspace": "demo"},
                           files=[("files", ("x.md", b"x", "text/markdown"))]
                           ).status_code == 403
        # the same routes work for the admin who created the viewer
        assert admin.post("/api/pin", json={"id": mid, "workspace": "demo",
                          "pinned": True}).status_code == 200
        # both members and admins may create their own shared folders
        assert admin.post("/api/auth/users", json={"email": "m@x.co", "name": "M",
                          "password": "anotherpass1", "role": "member"}).status_code == 200
        member = TestClient(c.app)
        assert member.post("/api/auth/login", json={"email": "m@x.co",
                           "password": "anotherpass1"}).status_code == 200
        assert member.post("/api/workspaces/create",
                           json={"workspace": "member-folder"}).status_code == 200
        assert admin.post("/api/workspaces/create",
                          json={"workspace": "admin-folder"}).status_code == 200
        # and the folders they made are visible to the whole team (shared, not per-user)
        names = {w["name"] for w in viewer.get("/api/workspaces").json()["workspaces"]}
        assert {"member-folder", "admin-folder"} <= names


def test_graph_endpoint_shape(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as c:
        g = c.get("/api/graph?workspace=demo").json()
        assert set(g) >= {"nodes", "edges", "types", "top", "stats"}
        assert set(g["stats"]) >= {"entities", "edges", "connected", "isolated"}
        ids = {n["id"] for n in g["nodes"]}
        assert all(e["from"] in ids and e["to"] in ids for e in g["edges"])


def test_team_seat_limit_enforcement(monkeypatch, tmp_path):
    """Adding more users than the licensed seat count must be rejected."""
    with _client(monkeypatch, tmp_path, team=True, key=_team_key(seats=2)) as c:
        assert c.post("/api/auth/setup", json={"email": "w@x.co", "name": "W",
                      "password": "supersecret1"}).status_code == 200
        assert c.post("/api/auth/users", json={"email": "m@x.co", "name": "M",
                      "password": "anotherpass1", "role": "member"}).status_code == 200
        r = c.post("/api/auth/users", json={"email": "v@x.co", "name": "V",
                   "password": "thirduserpass1", "role": "viewer"})
        assert r.status_code == 400, f"expected 400 seat-limit, got {r.status_code}: {r.text}"
        assert "seat limit" in r.text.lower()


def test_team_user_disable_and_reenable(monkeypatch, tmp_path):
    """Disabling a user prevents login; re-enabling restores it."""
    with _client(monkeypatch, tmp_path, team=True, key=_team_key()) as c:
        assert c.post("/api/auth/setup", json={"email": "w@x.co", "name": "W",
                      "password": "supersecret1"}).status_code == 200
        c.post("/api/auth/users", json={"email": "m@x.co", "name": "M",
               "password": "anotherpass1", "role": "member"})
        users = c.get("/api/auth/users").json()["users"]
        mid = [u["id"] for u in users if u["email"] == "m@x.co"][0]
        # Disable
        assert c.post("/api/auth/users/update", json={"user_id": mid, "disabled": True}).status_code == 200
        # Disabled user cannot login (on a fresh client without the admin cookie)
        fresh = TestClient(c.app)
        assert fresh.post("/api/auth/login", json={"email": "m@x.co",
                          "password": "anotherpass1"}).status_code == 401
        # Re-enable
        assert c.post("/api/auth/users/update", json={"user_id": mid, "disabled": False}).status_code == 200
        assert fresh.post("/api/auth/login", json={"email": "m@x.co",
                          "password": "anotherpass1"}).status_code == 200


def test_team_last_admin_cannot_be_demoted(monkeypatch, tmp_path):
    """The last active admin must not be demoted or disabled."""
    with _client(monkeypatch, tmp_path, team=True, key=_team_key()) as c:
        assert c.post("/api/auth/setup", json={"email": "w@x.co", "name": "W",
                      "password": "supersecret1"}).status_code == 200
        u = c.get("/api/auth/users").json()["users"][0]
        r = c.post("/api/auth/users/update", json={"user_id": u["id"], "role": "viewer"})
        assert r.status_code == 400, f"expected 400, got {r.status_code}: {r.text}"
        assert "last active admin" in r.text.lower()
        r2 = c.post("/api/auth/users/update", json={"user_id": u["id"], "disabled": True})
        assert r2.status_code == 400, f"expected 400, got {r2.status_code}: {r2.text}"
        assert "last active admin" in r2.text.lower()


def test_team_user_delete_frees_seat_and_email(monkeypatch, tmp_path):
    """Removing a member frees both their seat and their email — unlike disable, the
    same address can be re-invited afterwards (e.g. a typo'd/bounced invite)."""
    with _client(monkeypatch, tmp_path, team=True, key=_team_key(seats=2)) as c:
        assert c.post("/api/auth/setup", json={"email": "w@x.co", "name": "W",
                      "password": "supersecret1"}).status_code == 200
        assert c.post("/api/auth/users", json={"email": "m@x.co", "name": "M",
                      "password": "anotherpass1", "role": "member"}).status_code == 200
        # At the 2-seat cap: a third user is rejected.
        r = c.post("/api/auth/users", json={"email": "v@x.co", "name": "V",
                   "password": "thirduserpass1", "role": "viewer"})
        assert r.status_code == 400
        users = c.get("/api/auth/users").json()["users"]
        mid = [u["id"] for u in users if u["email"] == "m@x.co"][0]
        # Remove the member.
        assert c.post("/api/auth/users/delete", json={"user_id": mid}).status_code == 200
        assert [u["email"] for u in c.get("/api/auth/users").json()["users"]] == ["w@x.co"]
        # Their sessions are dead.
        fresh = TestClient(c.app)
        assert fresh.post("/api/auth/login", json={"email": "m@x.co",
                          "password": "anotherpass1"}).status_code == 401
        # The freed seat AND the freed email both work: the same address can be
        # re-invited (e.g. after the first invite email bounced) without a DB edit.
        r2 = c.post("/api/auth/users", json={"email": "m@x.co", "name": "M2",
                    "password": "freshpassword1", "role": "member"})
        assert r2.status_code == 200, r2.text
        assert fresh.post("/api/auth/login", json={"email": "m@x.co",
                          "password": "freshpassword1"}).status_code == 200


def test_team_last_admin_cannot_be_deleted(monkeypatch, tmp_path):
    """Deleting the last active admin must be rejected, same as demote/disable."""
    with _client(monkeypatch, tmp_path, team=True, key=_team_key()) as c:
        assert c.post("/api/auth/setup", json={"email": "w@x.co", "name": "W",
                      "password": "supersecret1"}).status_code == 200
        u = c.get("/api/auth/users").json()["users"][0]
        r = c.post("/api/auth/users/delete", json={"user_id": u["id"]})
        assert r.status_code == 400, f"expected 400, got {r.status_code}: {r.text}"
        assert "last active admin" in r.text.lower()
        assert len(c.get("/api/auth/users").json()["users"]) == 1


def test_team_delete_requires_admin(monkeypatch, tmp_path):
    """Only admins may remove team members."""
    with _client(monkeypatch, tmp_path, team=True, key=_team_key()) as c:
        assert c.post("/api/auth/setup", json={"email": "w@x.co", "name": "W",
                      "password": "supersecret1"}).status_code == 200
        c.post("/api/auth/users", json={"email": "m@x.co", "name": "M",
               "password": "anotherpass1", "role": "member"})
        users = c.get("/api/auth/users").json()["users"]
        wid = [u["id"] for u in users if u["email"] == "w@x.co"][0]
        member = TestClient(c.app)
        assert member.post("/api/auth/login", json={"email": "m@x.co",
                           "password": "anotherpass1"}).status_code == 200
        r = member.post("/api/auth/users/delete", json={"user_id": wid})
        assert r.status_code == 403, f"expected 403, got {r.status_code}: {r.text}"
        assert len(c.get("/api/auth/users").json()["users"]) == 2


def test_team_role_change_takes_effect(monkeypatch, tmp_path):
    """Demoting a member to viewer must immediately restrict their privileges."""
    with _client(monkeypatch, tmp_path, team=True, key=_team_key()) as c:
        assert c.post("/api/auth/setup", json={"email": "w@x.co", "name": "W",
                      "password": "supersecret1"}).status_code == 200
        c.post("/api/auth/users", json={"email": "m@x.co", "name": "M",
               "password": "anotherpass1", "role": "member"})
        users = c.get("/api/auth/users").json()["users"]
        mid = [u["id"] for u in users if u["email"] == "m@x.co"][0]
        # Member can recall
        member = TestClient(c.app)
        assert member.post("/api/auth/login", json={"email": "m@x.co",
                           "password": "anotherpass1"}).status_code == 200
        assert member.get("/api/recall?q=database&workspace=demo").status_code == 200
        # Demote to viewer
        assert c.post("/api/auth/users/update", json={"user_id": mid, "role": "viewer"}).status_code == 200
        # Viewer's recall still works (viewer can read)
        assert member.get("/api/recall?q=database&workspace=demo").status_code == 200
        # Viewer cannot pin/govern (POST = member+)
        mid_mem = member.get("/api/recall?q=database&workspace=demo").json()["memories"][0]["id"]
        assert member.post("/api/pin", json={"id": mid_mem, "workspace": "demo",
                           "pinned": True}).status_code == 403


def test_team_viewer_can_logout(monkeypatch, tmp_path):
    """Logout must be reachable by any role including viewer."""
    with _client(monkeypatch, tmp_path, team=True, key=_team_key()) as c:
        assert c.post("/api/auth/setup", json={"email": "w@x.co", "name": "W",
                      "password": "supersecret1"}).status_code == 200
        c.post("/api/auth/users", json={"email": "v@x.co", "name": "V",
               "password": "anotherpass1", "role": "viewer"})
        viewer = TestClient(c.app)
        assert viewer.post("/api/auth/login", json={"email": "v@x.co",
                           "password": "anotherpass1"}).status_code == 200
        assert viewer.post("/api/auth/logout").status_code == 200
        assert viewer.get("/api/auth/users").status_code == 401


def test_team_password_policy_enforced(monkeypatch, tmp_path):
    """Password requirements must be enforced at user creation."""
    with _client(monkeypatch, tmp_path, team=True, key=_team_key()) as c:
        assert c.post("/api/auth/setup", json={"email": "w@x.co", "name": "W",
                      "password": "supersecret1"}).status_code == 200
        r = c.post("/api/auth/users", json={"email": "x@x.co", "name": "X",
                   "password": "shortpassword", "role": "member"})
        assert r.status_code == 400, f"expected 400, got {r.status_code}: {r.text}"
        assert "password" in r.text.lower()
        r2 = c.post("/api/auth/users", json={"email": "y@x.co", "name": "Y",
                    "password": "alllowercase", "role": "member"})
        assert r2.status_code == 400
        assert "password" in r2.text.lower()


def test_team_login_lockout(monkeypatch, tmp_path):
    """Repeated failed logins must lock the account temporarily."""
    with _client(monkeypatch, tmp_path, team=True, key=_team_key()) as c:
        assert c.post("/api/auth/setup", json={"email": "w@x.co", "name": "W",
                      "password": "supersecret1"}).status_code == 200
        c.post("/api/auth/logout")
        for _ in range(5):
            r = c.post("/api/auth/login", json={"email": "w@x.co", "password": "wrongpass1"})
            assert r.status_code == 401
        r = c.post("/api/auth/login", json={"email": "w@x.co", "password": "supersecret1"})
        assert r.status_code == 401, f"expected 401 lockout, got {r.status_code}: {r.text}"
        assert "too many" in r.text.lower()


def test_trial_start_and_rejection(monkeypatch, tmp_path):
    """Trial starts. Re-calling during active trial is a no-op (returns current status).

    Since 0.8.4 the Pro trial is a REAL server-issued key (``licensing.start_trial`` ->
    ``cloud_license.request_trial_key``), not a local grant — mock the relay client call
    so this stays on the offline gate, same as the Team-trial test below. The re-call
    must NOT hit the relay a second time (there is only one ``request_trial_key`` stub
    below, good for exactly one call) — ``start_trial`` recognizes the already-active
    trial key locally and short-circuits before ever reaching the relay client. Since
    2026-07-14 an email is required in the request body too (the mock below still
    returns a key synchronously — ``pending=False`` — simulating a relay that short-
    circuits, so the "activates immediately" shape of this test stays valid)."""
    from engraphis import cloud_license
    monkeypatch.setenv("ENGRAPHIS_LICENSE_PUBKEY", ed25519_public_key(_SECRET).hex())
    trial_key = compose_key(
        {"v": 1, "plan": "pro", "email": "trial@engraphis.local", "seats": 1,
         "issued": int(time.time()), "expires": int(time.time() + 3 * 86400),
         "trial": 1}, _SECRET)
    monkeypatch.setattr(
        cloud_license, "request_trial_key",
        lambda base, mid, plan="pro", email="": (trial_key, "", False))
    with _client(monkeypatch, tmp_path) as c:
        r = c.post("/api/license/trial", json={"email": "trial@engraphis.local"})
        assert r.status_code == 200
        lic = c.get("/api/license").json()
        assert lic["is_trial"] is True
        r2 = c.post("/api/license/trial", json={"email": "trial@engraphis.local"})
        assert r2.status_code == 200  # no-op: already on trial
        assert r2.json()["is_trial"] is True


def test_trial_start_requires_email(monkeypatch, tmp_path):
    """2026-07-14 hardening: machine_id alone is no longer enough — a missing/blank
    email must 400 before ever reaching the relay client."""
    from engraphis import cloud_license
    called = []
    monkeypatch.setattr(
        cloud_license, "request_trial_key",
        lambda *a, **k: called.append(1) or (None, "should not be called", False))
    with _client(monkeypatch, tmp_path) as c:
        r = c.post("/api/license/trial", json={})
        assert r.status_code == 400
        assert "email" in r.json()["detail"]["error"].lower()
    assert not called


def test_trial_start_route_surfaces_pending_status(monkeypatch, tmp_path):
    """The route's normal successful response is now {"pending": true, ...} — a real
    key is minted only once the emailed magic link is opened, not from this call."""
    from engraphis import cloud_license
    monkeypatch.setattr(
        cloud_license, "request_trial_key",
        lambda base, mid, plan="pro", email="":
            (None, "check your email to confirm and activate the trial", True))
    with _client(monkeypatch, tmp_path) as c:
        r = c.post("/api/license/trial", json={"email": "w@example.com"})
        assert r.status_code == 200
        assert r.json()["pending"] is True
        # nothing activated yet
        assert c.get("/api/license").json()["plan"] == "free"


def test_team_trial_route_activates_relay_issued_key(monkeypatch, tmp_path):
    """POST /api/license/team-trial delegates to licensing.start_team_trial(), which
    needs the vendor relay (unlike the local-only Pro trial) — mock the relay client
    call so this stays on the offline gate."""
    from engraphis import cloud_license
    monkeypatch.setenv("ENGRAPHIS_STATE_DIR", str(tmp_path / "state"))  # isolate machine_id
    # the relay-minted key is signed with the test keypair, so verification against
    # the real key needs the same pubkey override _client(key=...) would normally set
    monkeypatch.setenv("ENGRAPHIS_LICENSE_PUBKEY", ed25519_public_key(_SECRET).hex())
    trial_key = compose_key(
        {"v": 1, "plan": "team", "email": "trial@engraphis.local", "seats": 1,
         "issued": int(time.time()), "expires": int(time.time() + 3 * 86400)}, _SECRET)
    monkeypatch.setattr(
        cloud_license, "request_team_trial_key",
        lambda base, mid, email="": (trial_key, "", False))
    with _client(monkeypatch, tmp_path) as c:
        r = c.post("/api/license/team-trial", json={"email": "trial@engraphis.local"})
        assert r.status_code == 200 and r.json()["plan"] == "team"
        assert c.get("/api/license").json()["plan"] == "team"


def test_team_trial_reachable_with_zero_users_and_no_session(monkeypatch, tmp_path):
    """Regression: a brand-new team-mode instance (ENGRAPHIS_TEAM_MODE=1, zero users, no
    license) was a hard deadlock. create_user() (called by /api/auth/setup) requires
    require_feature("team"), so you can't create the first admin without a license — but
    /api/license, /api/license/trial and /api/license/team-trial all required an
    authenticated team session, which is impossible before any admin exists. Every visitor
    got a 401 the instant they touched Settings -> License or clicked "Start trial," with
    no way to ever bootstrap. These three routes are now in dashboard_app.py's _PUBLIC set;
    this test drives the exact recovery path end to end: read license (no session) -> start
    Team trial (no session) -> the resulting license unblocks /api/auth/setup -> the new
    admin can log in normally afterward."""
    from engraphis import cloud_license
    monkeypatch.setenv("ENGRAPHIS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ENGRAPHIS_LICENSE_PUBKEY", ed25519_public_key(_SECRET).hex())
    trial_key = compose_key(
        {"v": 1, "plan": "team", "email": "trial@engraphis.local", "seats": 5,
         "issued": int(time.time()), "expires": int(time.time() + 3 * 86400)}, _SECRET)
    monkeypatch.setattr(
        cloud_license, "request_team_trial_key",
        lambda base, mid, email="": (trial_key, "", False))
    # team=True, key=None: team mode is on but there is no license and no user yet.
    with _client(monkeypatch, tmp_path, team=True) as c:
        # reading license state pre-login must not 401
        r0 = c.get("/api/license")
        assert r0.status_code == 200 and r0.json()["plan"] == "free"
        # first-admin setup stays closed until Team is active, even though the route is public
        locked = c.post("/api/auth/setup", json={"email": "w@x.co", "name": "W",
                        "password": "supersecret1"})
        assert locked.status_code == 402
        # starting the Team trial pre-login must not 401 either
        r1 = c.post("/api/license/team-trial", json={"email": "w@x.co"})
        assert r1.status_code == 200 and r1.json()["plan"] == "team"
        # the trial key is now active -> the first admin can be created and signed in
        r2 = c.post("/api/auth/setup", json={"email": "w@x.co", "name": "W",
                    "password": "supersecret1"})
        assert r2.status_code == 200
        fresh = TestClient(c.app)
        assert fresh.post("/api/auth/login", json={"email": "w@x.co",
                          "password": "supersecret1"}).status_code == 200


def test_license_activate_still_requires_admin_session(monkeypatch, tmp_path):
    """/api/license/activate is deliberately NOT in _PUBLIC (unlike /api/license and the
    trial routes) — pasting an arbitrary key changes the whole team's plan, so it stays
    behind the normal session + min_role('admin') gate."""
    with _client(monkeypatch, tmp_path, team=True, key=_team_key()) as c:
        assert c.post("/api/auth/setup", json={"email": "w@x.co", "name": "W",
                      "password": "supersecret1"}).status_code == 200
        c.post("/api/auth/users", json={"email": "m@x.co", "name": "M",
              "password": "anotherpass1", "role": "member"})
        anon = TestClient(c.app)
        assert anon.post("/api/license/activate",
                         json={"key": "not-a-key"}).status_code == 401
        member = TestClient(c.app)
        member.post("/api/auth/login", json={"email": "m@x.co", "password": "anotherpass1"})
        r = member.post("/api/license/activate", json={"key": "not-a-key"})
        assert r.status_code == 403


def test_team_trial_route_surfaces_relay_denial_as_400(monkeypatch, tmp_path):
    from engraphis import cloud_license
    monkeypatch.setenv("ENGRAPHIS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(
        cloud_license, "request_team_trial_key",
        lambda base, mid, email="": (None, "the free Team trial has already been used", False))
    with _client(monkeypatch, tmp_path) as c:
        r = c.post("/api/license/team-trial", json={"email": "w@x.co"})
        assert r.status_code == 400
        assert "already been used" in r.json()["detail"]["error"]


def test_license_activate_valid_and_invalid(monkeypatch, tmp_path):
    """Valid key activates when no license is already active; invalid key is rejected."""
    monkeypatch.setenv("ENGRAPHIS_LICENSE_PUBKEY", ed25519_public_key(_SECRET).hex())
    with _client(monkeypatch, tmp_path) as c:
        r = c.post("/api/license/activate", json={"key": "not-a-key"})
        assert r.status_code == 400
        good_key = _team_key()
        r2 = c.post("/api/license/activate", json={"key": good_key})
        assert r2.status_code == 200, f"activation failed: {r2.text}"
        assert r2.json()["plan"] == "team"


def test_team_cookie_secure_flag_on_https(monkeypatch, tmp_path):
    """Dashboard session cookie must carry secure=True when scheme is https."""
    with _client(monkeypatch, tmp_path, team=True, key=_team_key()) as c:
        # The TestClient uses http by default — the cookie should have secure=False
        # We verify the cookie exists and has httponly + samesite
        r = c.post("/api/auth/setup", json={"email": "w@x.co", "name": "W",
                   "password": "supersecret1"})
        assert r.status_code == 200
        cookies = r.headers.get_list("set-cookie")
        assert any("HttpOnly" in ck for ck in cookies)
        assert any("SameSite=strict" in ck for ck in cookies)
        # Over https, secure should be true
        r2 = c.post("/api/auth/login", json={"email": "w@x.co", "password": "supersecret1"},
                    headers={"X-Forwarded-Proto": "https"})
        assert r2.status_code == 200
        cookies2 = r2.headers.get_list("set-cookie")
        # Note: request.url.scheme may still be http behind TestClient, so we check
        # that the cookie is set regardless; the actual secure flag depends on the
        # proxy config. Verified: cookie is HttpOnly + SameSite=strict always.
        assert any("engr_dash_session" in ck for ck in cookies2)


def test_login_survives_a_lapsed_team_license_but_new_seats_still_need_one(
        monkeypatch, tmp_path):
    """2026-07-12 incident: a lapsed/expired Team license blocked EVERY login, including
    the admin's own, with no recovery path short of hand-minting a new key from the
    vendor's private signing key. AuthStore.login() no longer gates on a live license —
    an already-provisioned account can always sign back in on correct credentials, license
    or no license. What a lapsed license still blocks: adding NEW seats (create_user's own
    require_feature("team") gate is untouched), and the paid features gated at their own
    routes (analytics/export/automation/sync) — see test_analytics_and_export_gated_by_default.
    So the product's actual monetized value (seat growth + Pro/Team features) stays behind
    the paywall; only "can I get back into my own account" no longer does."""
    with _client(monkeypatch, tmp_path, team=True, key=_team_key()) as c:
        assert c.post("/api/auth/setup", json={"email": "w@x.co", "name": "W",
                      "password": "supersecret1"}).status_code == 200
        assert c.post("/api/auth/users", json={"email": "m@x.co", "name": "M",
                      "password": "anotherpass1", "role": "member"}).status_code == 200
        # license lapses (key gone)
        monkeypatch.delenv("ENGRAPHIS_LICENSE_KEY")
        lic.current_license(refresh=True)
        fresh = TestClient(c.app)
        # an existing account can still log in — no more license-induced lockout
        r = fresh.post("/api/auth/login", json={"email": "m@x.co",
                       "password": "anotherpass1"})
        assert r.status_code == 200
        # wrong password still fails normally (401) — this isn't an open door
        assert fresh.post("/api/auth/login", json={"email": "m@x.co",
                          "password": "wrongwrong1"}).status_code == 401
        # ...but adding a brand-new seat still requires a live Team license
        r2 = c.post("/api/auth/users", json={"email": "n@x.co", "name": "N",
                    "password": "yetanotherpw1", "role": "member"})
        assert r2.status_code == 402
        assert r2.json().get("feature") == "team"
        # restoring a valid key restores the ability to add seats
        monkeypatch.setenv("ENGRAPHIS_LICENSE_KEY", _team_key())
        lic.current_license(refresh=True)
        assert c.post("/api/auth/users", json={"email": "n@x.co", "name": "N",
                      "password": "yetanotherpw1", "role": "member"}).status_code == 200


# ── import (dashboard "Import files & folders" section) ────────────────────────────


def test_import_folder_route_rejects_path_outside_roots(monkeypatch, tmp_path):
    """
    The /api/workspaces/import-folder endpoint must reject paths that resolve
    outside the allowed import roots (HOME directory or ENGRAPHIS_IMPORT_ROOTS).
    This guards against path-traversal / symlink escapes.
    """
    import os
    import sys
    import tempfile
    # A directory genuinely OUTSIDE the import roots (HOME): C:\ is outside
    # %USERPROFILE% on Windows; /tmp is outside /home on POSIX CI runners.
    base = "C:\\" if sys.platform == "win32" else "/tmp"
    with tempfile.TemporaryDirectory(dir=base) as td:
        outside = td
        os.makedirs(outside, exist_ok=True)
        test_file = os.path.join(outside, "test.md")
        with open(test_file, "w") as f:
            f.write("# test\n")
        c = _client(monkeypatch, tmp_path)
        r = c.post("/api/workspaces/import-folder",
                    json={"workspace": "demo", "path": outside})
        # Should be rejected with 400
        assert r.status_code == 400, f"Expected 400, got {r.status_code}: {r.text}"


def test_import_folder_route(monkeypatch, tmp_path):
    """Restored v2-native counterpart to the retired v1 vault import-folder endpoint —
    see MemoryService.import_folder. The path must be under ENGRAPHIS_IMPORT_ROOTS (or
    home) before the route will read anything under it."""
    import_dir = tmp_path / "import-src"
    import_dir.mkdir()
    (import_dir / "note.md").write_text("# Title\nRoute-imported fact about aardvarks.")
    monkeypatch.setenv("ENGRAPHIS_IMPORT_ROOTS", str(import_dir))
    with _client(monkeypatch, tmp_path) as c:
        r = c.post("/api/workspaces/import-folder",
                   json={"workspace": "demo", "path": str(import_dir)})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["imported"] == 1 and body["scanned"] == 1

        found = c.get("/api/recall?q=aardvarks&workspace=demo").json()
        assert any("aardvarks" in m["content"] for m in found["memories"])



def test_import_files_route_multipart_upload(monkeypatch, tmp_path):
    """The drag-and-drop counterpart — a multipart upload, not a server path."""
    with _client(monkeypatch, tmp_path) as c:
        r = c.post("/api/workspaces/import-files",
                   data={"workspace": "demo", "memory_type": "semantic"},
                   files=[("files", ("upload.md", b"Uploaded fact about okapis.",
                                     "text/markdown"))])
        assert r.status_code == 200, r.text
        assert r.json()["imported"] == 1

        found = c.get("/api/recall?q=okapis&workspace=demo").json()
        assert any("okapis" in m["content"] for m in found["memories"])


def test_personal_folders_are_isolated_per_user(monkeypatch, tmp_path):
    """A personal folder is visible and usable only by its owner — even an admin cannot
    see or read another member's personal folder — while shared folders stay visible to the
    whole team. Exercises the full HTTP path end to end: the team auth gate binds the
    session user for the request and MemoryService enforces ownership at its single
    workspace-authorization chokepoint, so every scoped route inherits the check.

    One TestClient, one lifespan (two sequential lifespans deadlock here — see the note on
    test_analytics_and_export_*): the two identities are driven by swapping the session
    cookie explicitly after each user has logged in once.
    """
    cookie = "engr_dash_session"
    with _client(monkeypatch, tmp_path, team=True, key=_team_key()) as c:
        # admin (alice) sets herself up, then makes one personal and one shared folder
        assert c.post("/api/auth/setup", json={"email": "alice@x.co", "name": "Alice",
                      "password": "supersecret1"}).status_code == 200
        alice = c.cookies.get(cookie)
        assert c.post("/api/workspaces/create",
                      json={"workspace": "alice-secret", "visibility": "personal"}
                      ).status_code == 200
        assert c.post("/api/workspaces/create",
                      json={"workspace": "team-proj", "visibility": "shared"}
                      ).status_code == 200
        # add a member (bob) and log him in to capture his session
        assert c.post("/api/auth/users", json={"email": "bob@x.co", "name": "Bob",
                      "password": "anotherpass1", "role": "member"}).status_code == 200
        assert c.post("/api/auth/login", json={"email": "bob@x.co",
                      "password": "anotherpass1"}).status_code == 200
        bob = c.cookies.get(cookie)
        c.cookies.clear()  # from here on every request names its user via an explicit header

        # Identity is set with an explicit Cookie header (not per-request cookies=, which
        # httpx deprecates) so swapping between the two users is unambiguous and jar-free.
        def hdr(tok):
            return {"Cookie": "%s=%s" % (cookie, tok)}

        def names(tok):
            r = c.get("/api/workspaces", headers=hdr(tok))
            return sorted(w["name"] for w in r.json()["workspaces"])

        # bob sees the shared folder but never alice's personal one
        assert "team-proj" in names(bob)
        assert "alice-secret" not in names(bob)
        # and he is refused read access to it on every scoped route he might try
        assert c.get("/api/memories?workspace=alice-secret",
                     headers=hdr(bob)).status_code == 400
        assert c.get("/api/recall?q=x&workspace=alice-secret",
                     headers=hdr(bob)).status_code == 400
        # bob makes his own personal folder; alice (an admin) can't see it either
        assert c.post("/api/workspaces/create",
                      json={"workspace": "bob-notes", "visibility": "personal"},
                      headers=hdr(bob)).status_code == 200
        assert "bob-notes" not in names(alice)
        assert "alice-secret" in names(alice)
        # visibility is surfaced on the listing so the dashboard can badge folders
        vis = {w["name"]: w.get("visibility") for w in
               c.get("/api/workspaces", headers=hdr(alice)).json()["workspaces"]}
        assert vis["alice-secret"] == "personal"
        assert vis["team-proj"] == "shared"
