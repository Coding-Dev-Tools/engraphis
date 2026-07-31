import pytest

pytest.importorskip("fastapi", reason="full-stack extra not installed")

from fastapi.testclient import TestClient

from engraphis.read_only_api import create_read_only_app
from engraphis.service import MemoryService
from engraphis.backends.graph_extractor import RegexGraphExtractor


def test_read_only_api_requires_token_and_does_not_reinforce():
    svc = MemoryService.create(":memory:", graph_extractor="none")
    memory = svc.remember("The database is SQLite.", workspace="w", scope="workspace")
    before = svc.store.get_memory(memory["id"]).access_count
    receipts_before = svc.store.conn.execute(
        "SELECT COUNT(*) AS n FROM operation_receipts"
    ).fetchone()["n"]
    client = TestClient(create_read_only_app(svc, token="secret"))
    assert client.get("/recall", params={"query": "database", "workspace": "w"}).status_code == 401
    # Receipt-derived savings are still scoped usage information, so the new
    # endpoint must stay behind the same bearer gate as recall.
    assert client.get("/context-savings", params={"workspace": "w"}).status_code == 401
    response = client.get(
        "/recall",
        params={"query": "database", "workspace": "w", "candidate_depth": "adaptive"},
        headers={"Authorization": "Bearer secret"},
    )
    assert response.status_code == 200 and response.json()["count"] == 1
    assert response.json()["candidate_depth"] == "adaptive"
    assert response.json()["candidate_k_used"] < response.json()["candidate_k_requested"]
    lowercase = client.get(
        "/recall", params={"query": "database", "workspace": "w"},
        headers={"Authorization": "bearer secret"},
    )
    assert lowercase.status_code == 200
    savings = client.get(
        "/context-savings", params={"workspace": "w"},
        headers={"Authorization": "Bearer secret"},
    )
    assert savings.status_code == 200
    assert savings.json()["format"] == "engraphis-context-savings/1"
    assert response.headers["x-frame-options"] == "DENY"
    assert svc.store.get_memory(memory["id"]).access_count == before
    assert svc.store.conn.execute(
        "SELECT COUNT(*) AS n FROM operation_receipts"
    ).fetchone()["n"] == receipts_before
    assert client.post(
        "/remember", json={}, headers={"Authorization": "Bearer secret"}
    ).status_code == 404


def test_read_only_api_serves_graph_and_intent_recall():
    svc = MemoryService.create(":memory:", graph_extractor="regex")
    svc.remember(
        "Alice Johnson works at Acme Corporation.",
        workspace="w", scope="workspace",
    )
    client = TestClient(create_read_only_app(svc))
    omitted = client.get("/graph", params={"workspace": "w"}).json()
    assert omitted["nodes"] and omitted["edges"]
    assert client.get("/graph?workspace=w&layers=").json()["edges"] == []
    response = client.post(
        "/intent/recall",
        json={
            "query": "Alice", "intent": "explain", "workspace": "w",
            "candidate_depth": "adaptive",
        },
    )
    assert response.status_code == 200
    assert response.json()["operation"] == "recall"
    assert response.json()["candidate_depth"] == "adaptive"


def test_read_only_api_serves_content_free_context_savings():
    svc = MemoryService.create(":memory:", graph_extractor="none")
    svc.remember("Context savings test.", workspace="w", scope="workspace")
    svc.recall("context savings", workspace="w", token_budget=64)

    response = TestClient(create_read_only_app(svc)).get(
        "/context-savings", params={"workspace": "w"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["format"] == "engraphis-context-savings/1"
    assert body["savings_receipt_count"] == 1
    assert body["by_token_counter"][0]["source_tokens"] >= body["by_token_counter"][0]["saved_tokens"]


def test_read_only_code_search_forwards_bitemporal_anchors():
    svc = MemoryService.create(":memory:", graph_extractor="none")
    svc.remember("Code search anchor.", workspace="w", repo="repo")
    observed = {}
    original = svc.search_code

    def observe(*args, **kwargs):
        observed.update(kwargs)
        return original(*args, **kwargs)

    svc.search_code = observe
    response = TestClient(create_read_only_app(svc)).get(
        "/code/search",
        params={
            "query": "missing", "workspace": "w", "repo": "repo",
            "as_of": 10.0, "valid_at": 10.0, "known_at": 20.0,
        },
    )

    assert response.status_code == 200
    assert observed["as_of"] == observed["valid_at"] == 10.0
    assert observed["known_at"] == 20.0


@pytest.mark.parametrize("path", ["/graph", "/code/export"])
def test_read_only_graph_surfaces_forward_bitemporal_anchors(path):
    svc = MemoryService.create(":memory:", graph_extractor="none")
    svc.remember("Temporal adapter anchor.", workspace="w", repo="repo")
    observed = {}
    method_name = "graph" if path == "/graph" else "export_code_graph"
    original = getattr(svc, method_name)

    def observe(*args, **kwargs):
        observed.update(kwargs)
        return original(*args, **kwargs)

    setattr(svc, method_name, observe)
    response = TestClient(create_read_only_app(svc)).get(
        path,
        params={
            "workspace": "w", "repo": "repo",
            "as_of": 10.0, "valid_at": 10.0, "known_at": 20.0,
        },
    )

    assert response.status_code == 200
    assert observed["as_of"] == observed["valid_at"] == 10.0
    assert observed["known_at"] == 20.0


def test_read_only_graph_does_not_lazy_backfill():
    svc = MemoryService.create(":memory:", graph_extractor="none")
    svc.remember(
        "Alice Johnson works at Acme Corporation.",
        workspace="w", scope="workspace",
    )
    svc.engine.graph_extractor = RegexGraphExtractor()
    before = svc.store.conn.execute(
        "SELECT COUNT(*) AS n FROM entities"
    ).fetchone()["n"]

    response = TestClient(create_read_only_app(svc)).get("/graph", params={"workspace": "w"})

    assert response.status_code == 200
    assert response.json()["nodes"] == []
    assert svc.store.conn.execute(
        "SELECT COUNT(*) AS n FROM entities"
    ).fetchone()["n"] == before
