"""Smoke test — exercises the full API surface against a running server.

Usage:
    # Start server in one terminal:
    python -m scripts.start_server
    # Run tests in another:
    python -m scripts.test_routes
"""
from __future__ import annotations

import sys
import time

import httpx

from engraphis.config import settings

BASE = settings.base_url
PASS = 0
FAIL = 0


def _ok(name: str) -> None:
    global PASS
    PASS += 1
    print(f"  [ok] {name}")


def _fail(name: str, err: str) -> None:
    global FAIL
    FAIL += 1
    print(f"  [FAIL] {name}: {err}")


def run() -> None:
    print(f"Testing Engraphis at {BASE}")
    print()

    with httpx.Client(base_url=BASE, timeout=60) as c:
        # Health
        try:
            r = c.get("/memory/health")
            assert r.status_code == 200
            _ok("health")
        except Exception as e:
            _fail("health", e)
            return

        ns = f"test-{int(time.time())}"

        # Insert memory (legacy route)
        try:
            r = c.post("/memory/insert", json={
                "key": "pref-theme",
                "content": "User prefers dark mode and high contrast UI",
                "namespace": ns,
                "metadata": {"source": "test"},
            })
            assert r.status_code == 200, r.text
            _ok("insert_memory (legacy)")
        except Exception as e:
            _fail("insert_memory", e)

        # Insert document
        try:
            r = c.post("/memory/documents", json={
                "title": "Meeting Notes",
                "content": "Discussed the Q3 roadmap. Alice will lead the backend refactor. "
                           "Bob is responsible for the frontend migration to React 19.",
                "namespace": ns,
                "document_id": "meeting-q3",
                "source_type": "doc",
            })
            assert r.status_code == 200, r.text
            _ok("insert_document")
        except Exception as e:
            _fail("insert_document", e)

        # Batch insert
        try:
            r = c.post("/memory/documents/batch", json={"items": [
                {"title": "Doc A", "content": "Alice prefers Python over JavaScript.", "namespace": ns, "document_id": "doc-a"},
                {"title": "Doc B", "content": "Bob works remotely from Seattle.", "namespace": ns, "document_id": "doc-b"},
            ]})
            assert r.status_code == 200, r.text
            _ok("insert_documents_batch")
        except Exception as e:
            _fail("insert_documents_batch", e)

        # Wait a moment for indexing
        time.sleep(1)

        # Query memory
        try:
            r = c.post("/memory/query", json={
                "namespace": ns,
                "query": "What does the user prefer?",
                "maxChunks": 5,
            })
            assert r.status_code == 200, r.text
            data = r.json()["data"]
            assert data["count"] > 0, "expected at least 1 chunk"
            _ok(f"query_memory (count={data['count']})")
        except Exception as e:
            _fail("query_memory", e)

        # List documents
        try:
            r = c.get("/memory/documents", params={"namespace": ns, "limit": 10})
            assert r.status_code == 200, r.text
            data = r.json()["data"]
            assert data["count"] >= 3, f"expected >=3 docs, got {data['count']}"
            _ok(f"list_documents (count={data['count']})")
        except Exception as e:
            _fail("list_documents", e)

        # Get single document
        try:
            r = c.get("/memory/documents/meeting-q3", params={"namespace": ns})
            assert r.status_code == 200, r.text
            _ok("get_document")
        except Exception as e:
            _fail("get_document", e)

        # Recall master
        try:
            r = c.post("/memory/recall", json={"namespace": ns, "maxChunks": 5})
            assert r.status_code == 200, r.text
            _ok("recall_master")
        except Exception as e:
            _fail("recall_master", e)

        # Recall memories (Ebbinghaus)
        try:
            r = c.post("/memory/memories/recall", json={"namespace": ns, "topK": 5})
            assert r.status_code == 200, r.text
            _ok("recall_memories")
        except Exception as e:
            _fail("recall_memories", e)

        # Record interactions
        try:
            r = c.post("/memory/interactions", json={
                "namespace": ns,
                "entityNames": ["Alice", "Bob"],
                "interactionLevel": "engage",
            })
            assert r.status_code == 200, r.text
            _ok("record_interactions")
        except Exception as e:
            _fail("record_interactions", e)

        # Graph snapshot
        try:
            r = c.get("/memory/admin/graph-snapshot", params={"namespace": ns})
            assert r.status_code == 200, r.text
            data = r.json()["data"]
            _ok(f"graph_snapshot (entities={data['entity_count']}, edges={data['edge_count']})")
        except Exception as e:
            _fail("graph_snapshot", e)

        # Queries endpoint
        try:
            r = c.post("/memory/queries", json={
                "query": "Who works on the backend?",
                "namespace": ns,
                "maxChunks": 3,
                "recallOnly": True,
            })
            assert r.status_code == 200, r.text
            _ok("query_memory_context")
        except Exception as e:
            _fail("query_memory_context", e)

        # Delete document
        try:
            r = c.delete(f"/memory/documents/doc-a", params={"namespace": ns})
            assert r.status_code == 200, r.text
            _ok("delete_document")
        except Exception as e:
            _fail("delete_document", e)

        # Delete namespace
        try:
            r = c.post("/memory/admin/delete", json={"namespace": ns, "delete_all": True})
            assert r.status_code == 200, r.text
            _ok("delete_namespace")
        except Exception as e:
            _fail("delete_namespace", e)

    print()
    print(f"Results: {PASS} passed, {FAIL} failed")
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    run()
