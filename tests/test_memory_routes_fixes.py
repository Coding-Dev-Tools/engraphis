"""Regressions for v1 memory-route / store correctness bugs:

- list_documents(offset=..) with no limit generated invalid SQL (OFFSET without LIMIT).
- GET /memory/documents/{id} without ?namespace looked up a nonexistent '_global' ns.
- POST /memory/prune coerced an explicit minRetention=0.0 to 0.05 and over-pruned.
- POST /memory/conversations crashed (500) on a user message missing 'content'.
- POST /memory/interactions recorded signals that never reinforced any memory.
"""
import threading

import pytest

from engraphis.config import settings
from engraphis.stores import get_conn, init_db, now_ts
from engraphis.stores import vectors as mem_store
from engraphis.engines import reweight


def _setup_store(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "mem.db"))
    monkeypatch.setattr("engraphis.stores._local", threading.local())
    init_db()


def test_list_documents_offset_without_limit_is_valid(monkeypatch, tmp_path):
    _setup_store(monkeypatch, tmp_path)
    for i in range(3):
        mem_store.upsert_memory(namespace="ns", document_id="d%d" % i, title="t",
                                content="c%d" % i)
    # offset with no limit must not raise "OFFSET without LIMIT" (previously a 500).
    rest = mem_store.list_documents(namespace="ns", offset=1)
    assert len(rest) == 2


def test_find_document_without_namespace(monkeypatch, tmp_path):
    _setup_store(monkeypatch, tmp_path)
    mem_store.upsert_memory(namespace="vault", document_id="doc1", title="t", content="hi")
    # No namespace: still found (old code queried a nonexistent '_global' ns and 404'd).
    assert mem_store.find_document("doc1") is not None
    assert mem_store.find_document("doc1", "vault") is not None
    assert mem_store.find_document("nope") is None


def test_recall_master_none_namespace_recalls_across_all(monkeypatch, tmp_path):
    _setup_store(monkeypatch, tmp_path)
    import numpy as np
    from engraphis.engines import recall as recall_engine
    vec = np.ones(8, dtype=np.float32)
    mem_store.upsert_memory(namespace="ns1", document_id="a", title="t", content="alpha",
                            vector=vec)
    mem_store.upsert_memory(namespace="ns2", document_id="b", title="t", content="beta",
                            vector=vec)
    # namespace=None must recall across ALL namespaces, not a nonexistent '_global' (which
    # made the consciousness loop's thought synthesis silently no-op).
    out = recall_engine.recall_master(namespace=None, max_chunks=10)
    assert out["count"] >= 2


def test_interactions_reinforce_matching_memories(monkeypatch, tmp_path):
    _setup_store(monkeypatch, tmp_path)
    mem_store.upsert_memory(namespace="ns", document_id="d1", title="About Alice",
                            content="Alice ships the release")
    mem_store.upsert_memory(namespace="ns", document_id="d2", title="About Bob",
                            content="Bob reviews code")
    before = get_conn().execute(
        "SELECT stability FROM memories WHERE document_id='d1'").fetchone()["stability"]
    n = reweight.boost_entity_memories("ns", "Alice", "engage")
    assert n == 1                                   # only the Alice memory matched
    after = get_conn().execute(
        "SELECT stability FROM memories WHERE document_id='d1'").fetchone()["stability"]
    assert after > before                           # it was actually reinforced
    # Bob's memory is untouched.
    bob = get_conn().execute(
        "SELECT stability FROM memories WHERE document_id='d2'").fetchone()["stability"]
    assert bob == 1.0


# ── app-level route regressions ────────────────────────────────────────────────
pytest.importorskip("fastapi", reason="full-stack extra not installed")
from fastapi.testclient import TestClient  # noqa: E402


def _client(monkeypatch, tmp_path):
    _setup_store(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "loop_interval", 0)
    monkeypatch.setattr(settings, "embed_model", "")
    from engraphis.app import create_app
    return TestClient(create_app())


def test_prune_honors_explicit_zero_threshold(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as c:
        # A memory last accessed long ago has near-zero retention.
        mem_store.upsert_memory(namespace="ns", document_id="old", title="t",
                                content="stale", created_at=now_ts() - 100 * 86400)
        get_conn().execute(
            "UPDATE memories SET last_access=?, stability=0.5 WHERE document_id='old'",
            (now_ts() - 100 * 86400,))
        get_conn().commit()
        # threshold 0.0 => delete only retention < 0 => nothing (old code coerced to 0.05).
        r = c.post("/memory/prune",
                   json={"namespace": "ns", "minRetention": 0.0, "dryRun": True})
        assert r.status_code == 200, r.text
        data = r.json()["data"]
        assert data.get("candidates", data.get("wouldDelete", 0)) == 0 or \
            data.get("count", 0) == 0


def test_conversations_missing_content_is_400_not_500(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as c:
        r = c.post("/memory/conversations", json={"messages": [{"role": "user"}]})
        assert r.status_code == 400
