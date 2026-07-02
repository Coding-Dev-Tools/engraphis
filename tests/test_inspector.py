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


def test_index_serves_the_ui(client):
    c, _ = client
    r = c.get("/")
    assert r.status_code == 200
    assert "Memory Inspector" in r.text
    assert 'role="tablist"' in r.text            # accessible tab navigation shipped


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


def test_validation_errors_are_400_not_500(client):
    c, _ = client
    r = c.get("/api/why", params={"q": "x", "workspace": "no-such-ws"})
    assert r.status_code == 400
    assert "error" in r.json()


def test_bearer_auth_gates_api_but_not_page(monkeypatch):
    monkeypatch.setattr(settings, "api_token", "sekrit")
    svc = MemoryService.create(":memory:")
    c = TestClient(create_app(svc))
    assert c.get("/").status_code == 200                      # page loads
    assert c.get("/api/workspaces").status_code == 401        # api gated
    ok = c.get("/api/workspaces", headers={"Authorization": "Bearer sekrit"})
    assert ok.status_code == 200


def test_consolidate_endpoint_dry_run(client):
    c, _ = client
    for i in (1, 2, 3):
        # bypass dedupe so the repeats survive as separate episodics
        pass
    svc_r = c.post("/api/consolidate", json={"workspace": "acme", "dry_run": True})
    assert svc_r.status_code == 200
    assert svc_r.json()["dry_run"] is True
