"""Inspector API tests — skip cleanly on the numpy-only CI gate (like test_app_auth)."""
import pytest

pytest.importorskip("fastapi", reason="full-stack extra not installed")
from fastapi.testclient import TestClient  # noqa: E402

from engraphis.config import settings  # noqa: E402
from engraphis.inspector import create_app  # noqa: E402
from engraphis.service import MemoryService  # noqa: E402


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(settings, "api_token", "")
    svc = MemoryService.create(":memory:")
    svc.remember("Until 2026-01 the rate limit was 100 requests per minute per API key.",
                 workspace="acme", repo="backend")
    out = svc.remember("As of 2026-02 the rate limit was raised to 500 requests per minute "
                       "per API key.", workspace="acme", repo="backend")
    assert out["op"] == "invalidate"
    return TestClient(create_app(svc)), out


def test_index_ui_is_retired(client):
    # The standalone Inspector HTML UI was retired (folded into the :8700 dashboard);
    # the page route is intentionally gone, so GET "/" now 404s. The JSON API remains.
    c, _ = client
    assert c.get("/").status_code == 404


def test_ready_probe_reports_db_and_embedder(client):
    c, _ = client
    r = c.get("/api/ready")
    assert r.status_code == 200
    body = r.json()
    assert body["ready"] is True
    assert body["checks"] == {"db": True, "embedder": True}
    assert body["version"]


def test_workspaces_and_stats(client):
    c, _ = client
    ws = c.get("/api/workspaces").json()
    assert ws["workspaces"][0]["name"] == "acme"
    assert "backend" in ws["workspaces"][0]["repos"]
    s = c.get("/api/stats", params={"workspace": "acme"}).json()
    assert s["memories"] == 1                    # superseded fact left the live view


def test_recall_endpoint_round_trip(client):
    c, _ = client
    r = c.get("/api/recall", params={"q": "rate limit", "workspace": "acme"}).json()
    assert r["count"] >= 1
    assert any("500" in m["content"] for m in r["memories"])


def test_inspect_returns_full_supersession_chain(client):
    c, out = client
    new_id = out["id"]
    r = c.get(f"/api/memory/{new_id}", params={"workspace": "acme", "repo": "backend"})
    data = r.json()
    assert r.status_code == 200
    chain = data["chain"]
    assert len(chain) == 2                       # old version + live version
    assert chain[0]["valid_to"] is not None      # oldest is closed…
    assert chain[-1]["id"] == new_id and chain[-1]["valid_to"] is None  # …newest is live
    assert "100" in chain[0]["content"] and "500" in chain[1]["content"]


def test_graph_endpoint_serves_the_same_data_as_the_dashboard():
    """Inspector's /api/graph is the ported Graph tab -- same MemoryService.graph()
    the dashboard's v2_api.py calls (engraphis/graphdata.py), so both UIs render
    identical graphs from the same entities/edges tables."""
    svc = MemoryService.create(":memory:")
    svc.remember("seed memory so the workspace exists", workspace="acme", repo="backend")
    wid = svc.store.get_or_create_workspace("acme")
    conn = svc.store.conn
    conn.execute("INSERT INTO entities(id, workspace_id, repo_id, name, etype, created_at) "
                 "VALUES ('e1', ?, NULL, 'Alice', 'person_or_concept', 0)", (wid,))
    conn.execute("INSERT INTO entities(id, workspace_id, repo_id, name, etype, created_at) "
                 "VALUES ('e2', ?, NULL, 'Acme Corp', 'organization', 0)", (wid,))
    conn.execute("INSERT INTO edges(id, workspace_id, repo_id, src, dst, relation, layer) "
                 # src/dst are entity ids ('e1'/'e2'), never the display name — that's
                 # what backends.graph_extractor.feed actually writes
                 "VALUES ('g1', ?, NULL, 'e1', 'e2', 'works_at', 'entity')", (wid,))
    conn.commit()
    c = TestClient(create_app(svc))
    r = c.get("/api/graph", params={"workspace": "acme"})
    assert r.status_code == 200
    g = r.json()
    assert {n["id"] for n in g["nodes"]} == {"e1", "e2"}
    assert {n["label"] for n in g["nodes"]} == {"Alice", "Acme Corp"}
    assert g["edges"] == [
        {"from": "e1", "to": "e2", "label": "works_at", "layer": "entity"}
    ]
    assert g["stats"] == {"entities": 2, "edges": 1, "connected": 2, "isolated": 0}


def test_graph_endpoint_rejects_workspace_outside_the_binding():
    """Same isolation boundary as every other Inspector read (test_service_isolation.py):
    a bound instance must not leak another tenant's graph."""
    svc = MemoryService.create(":memory:", allowed_workspaces=["alpha"])
    svc.store.get_or_create_workspace("alpha")
    c = TestClient(create_app(svc))
    r = c.get("/api/graph", params={"workspace": "beta"})
    assert r.status_code == 400


def test_why_supersedes_and_timeline_endpoints(client):
    c, _ = client
    why = c.get("/api/why", params={"q": "rate limit", "workspace": "acme"}).json()
    assert any("500" in m["content"] for m in why["answer"])
    assert any("100" in m["content"] for m in why["supersedes"])
    tl = c.get("/api/timeline", params={"q": "rate limit", "workspace": "acme"}).json()
    assert len(tl["history"]) == 2


def test_governance_endpoints_pin_and_forget(client):
    c, out = client
    body = {"memory_id": out["id"], "workspace": "acme", "repo": "backend"}
    assert c.post("/api/pin", json=body).json()["pinned"] is True
    r = c.post("/api/forget", json={**body, "reason": "test"}).json()
    assert r["status"] == "forgotten"
    assert c.get("/api/stats", params={"workspace": "acme"}).json()["memories"] == 0


def test_promote_endpoint_widens_scope(client):
    c, out = client
    response = c.post("/api/promote", json={
        "memory_id": out["id"],
        "target_scope": "workspace",
        "workspace": "acme",
        "repo": "backend",
        "reason": "shared convention",
    })

    assert response.status_code == 200
    promoted = response.json()
    assert promoted["scope"] == "workspace"
    assert promoted["promoted_from"] == out["id"]


def test_validation_errors_are_400_not_500(client):
    c, _ = client
    r = c.get("/api/why", params={"q": "x", "workspace": "no-such-ws"})
    assert r.status_code == 400
    assert "error" in r.json()


def test_unhandled_exception_returns_json_not_plaintext(client, monkeypatch):
    """Regression: an unhandled exception used to fall through to Starlette's default
    handler, a bare text/plain "Internal Server Error" body. The frontend's api()
    helper always does res.json() on the response, so that plaintext body failed to
    parse and surfaced as an opaque "Error: bad response" with no clue what broke.
    The catch-all handler must turn this into a real 500 JSON body instead."""
    c, _ = client

    def _boom(self, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(MemoryService, "stats", _boom)
    # raise_server_exceptions=False: exercise the ASGI response Starlette actually
    # sends, the same thing a real browser's fetch() sees -- not pytest re-raising
    # the exception in-process. Same app/service instance as `client`; only the
    # TestClient's exception-propagation behavior differs.
    c2 = TestClient(c.app, raise_server_exceptions=False)
    r = c2.get("/api/stats", params={"workspace": "acme"})
    assert r.status_code == 500
    assert r.headers["content-type"].startswith("application/json")
    assert r.json()["error"]


def test_bearer_auth_gates_api_but_not_page(monkeypatch):
    monkeypatch.setattr(settings, "api_token", "sekrit")
    svc = MemoryService.create(":memory:")
    c = TestClient(create_app(svc))
    assert c.get("/").status_code == 404                      # page retired (unrouted → 404, not a 401 auth gate)
    assert c.get("/api/workspaces").status_code == 401        # api gated
    ok = c.get("/api/workspaces", headers={"Authorization": "Bearer sekrit"})
    assert ok.status_code == 200


def test_consolidate_endpoint_dry_run(client):
    c, _ = client
    # Seed episodic memories via the API so they survive deduplication.
    for i in (1, 2, 3):
        c.post("/api/remember", json={
            "content": f"We discussed the release plan and blockers in standup #{i}.",
            "workspace": "acme", "repo": "backend",
        })
    svc_r = c.post("/api/consolidate", json={"workspace": "acme", "dry_run": True})
    assert svc_r.status_code == 200
    assert svc_r.json()["dry_run"] is True
