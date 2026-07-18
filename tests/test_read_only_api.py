import pytest

pytest.importorskip("fastapi", reason="full-stack extra not installed")

from fastapi.testclient import TestClient

from engraphis.read_only_api import create_read_only_app
from engraphis.service import MemoryService


def test_read_only_api_requires_token_and_does_not_reinforce():
    svc = MemoryService.create(":memory:", graph_extractor="none")
    memory = svc.remember("The database is SQLite.", workspace="w", scope="workspace")
    before = svc.store.get_memory(memory["id"]).access_count
    receipts_before = svc.store.conn.execute(
        "SELECT COUNT(*) AS n FROM operation_receipts"
    ).fetchone()["n"]
    client = TestClient(create_read_only_app(svc, token="secret"))
    assert client.get("/recall", params={"query": "database", "workspace": "w"}).status_code == 401
    response = client.get(
        "/recall",
        params={"query": "database", "workspace": "w"},
        headers={"Authorization": "Bearer secret"},
    )
    assert response.status_code == 200 and response.json()["count"] == 1
    lowercase = client.get(
        "/recall", params={"query": "database", "workspace": "w"},
        headers={"Authorization": "bearer secret"},
    )
    assert lowercase.status_code == 200
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
    assert client.get("/graph", params={"workspace": "w"}).json()["nodes"]
    response = client.post(
        "/intent/recall",
        json={"query": "Alice", "intent": "explain", "workspace": "w"},
    )
    assert response.status_code == 200
    assert response.json()["operation"] == "recall"
