"""Trust-boundary coverage for graph state reconstructed after a write."""

from __future__ import annotations

import sqlite3
import time

import pytest

from engraphis.core.engine import MemoryEngine
from engraphis.core.interfaces import MemoryRecord, Scope
from engraphis.core.store import Store
from engraphis.service import MemoryService
from scripts import backfill_graph


def test_cli_graph_backfill_uses_only_live_explicitly_approved_memories(tmp_path, monkeypatch):
    path = tmp_path / "graph-backfill.db"
    store = Store(str(path))
    workspace_id = store.get_or_create_workspace("acme")
    repo_id = store.get_or_create_repo(workspace_id, "api")
    approved_id = store.add_memory(MemoryRecord(
        id="mem_approved", content="Approved graph evidence.",
        workspace_id=workspace_id, repo_id=repo_id, scope=Scope.REPO,
        provenance={"source": "human_review", "trusted": True, "review_state": "approved"},
    ))
    store.add_memory(MemoryRecord(
        id="mem_pending", content="Pending imported graph evidence.",
        workspace_id=workspace_id, repo_id=repo_id, scope=Scope.REPO,
        provenance={"source": "import", "trusted": False, "review_state": "pending"},
    ))
    store.add_memory(MemoryRecord(
        id="mem_future", content="Future graph evidence.",
        workspace_id=workspace_id, repo_id=repo_id, scope=Scope.REPO,
        valid_from=time.time() + 3600,
        provenance={"source": "human_review", "trusted": True, "review_state": "approved"},
    ))
    store.close()

    seen: list[tuple[str, dict]] = []

    def fake_feed(_store, _content, **kwargs):
        seen.append((kwargs["provenance"]["memory_id"], kwargs))
        return {"entities": 0, "relations": 0}

    monkeypatch.setattr(backfill_graph, "feed", fake_feed)
    report = backfill_graph.backfill(str(path))

    assert [memory_id for memory_id, _ in seen] == [approved_id]
    assert seen[0][1]["provenance"]["source"] == "backfill_graph"
    assert report["workspaces"][0]["memories_scanned"] == 1


def test_cli_graph_backfill_dry_run_leaves_database_bytes_and_mode_unchanged(tmp_path):
    path = tmp_path / "graph-backfill.db"
    store = Store(str(path))
    store.close()

    conn = sqlite3.connect(path)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    assert conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"
    conn.close()
    before = path.read_bytes()

    report = backfill_graph.backfill(str(path), dry_run=True)

    assert report["dry_run"] is True
    assert path.read_bytes() == before
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    finally:
        conn.close()


def test_lazy_graph_backfill_never_feeds_pending_records(monkeypatch):
    service = MemoryService.create(":memory:", graph_extractor="regex", extractor="none")
    workspace_id = service.store.get_or_create_workspace("acme")
    repo_id = service.store.get_or_create_repo(workspace_id, "api")
    approved_id = service.engine.remember(
        "Approved graph evidence.", workspace_id=workspace_id, repo_id=repo_id,
    )
    pending_id = service.engine.remember(
        "Pending graph evidence.", workspace_id=workspace_id, repo_id=repo_id,
        metadata={"provenance": {
            "source": "import", "trusted": False, "review_state": "pending",
        }},
    )

    from engraphis.backends import graph_extractor

    seen: list[str] = []

    def fake_feed(_store, _content, **kwargs):
        seen.append(kwargs["provenance"]["memory_id"])
        return {"entities": 0, "relations": 0}

    monkeypatch.setattr(graph_extractor, "feed", fake_feed)
    service._lazy_backfill_graph(workspace_id)

    assert seen == [approved_id]
    assert pending_id not in seen


def test_rebuild_code_memory_links_does_not_resurrect_pending_bridge():
    engine = MemoryEngine.create(":memory:")
    workspace_id = engine.store.get_or_create_workspace("acme")
    repo_id = engine.store.get_or_create_repo(workspace_id, "api")
    symbol_id = engine.store.upsert_symbol(
        repo_id=repo_id, kind="function", name="deploy", fqname="deploy",
        file="deploy.py", span="1-1",
    )
    approved_id = engine.remember(
        "deploy publishes the approved release.",
        workspace_id=workspace_id, repo_id=repo_id,
    )
    pending_id = engine.remember(
        "deploy publishes imported review evidence.",
        workspace_id=workspace_id, repo_id=repo_id,
        metadata={"provenance": {
            "source": "import", "trusted": False, "review_state": "pending",
        }},
    )
    engine.store.link_memory_symbol(repo_id=repo_id, symbol_id=symbol_id, memory_id=pending_id)

    engine.rebuild_code_memory_links(repo_id=repo_id)

    assert {
        row["memory_id"] for row in engine.store.list_code_memory_links(repo_id)
    } == {approved_id}

def test_backfill_closes_store_even_when_feed_raises(tmp_path, monkeypatch):
    """Round 1 wrapped the processing loop in try/finally: store.close().

    An extractor failure, JSON decode error, or any other mid-loop exception
    must not strand the SQLite connection. This test pins that contract.
    """
    path = tmp_path / "graph-backfill-lifecycle.db"
    store = Store(str(path))
    workspace_id = store.get_or_create_workspace("lifecycle")
    store.add_memory(MemoryRecord(
        id="", content="test memory", workspace_id=workspace_id,
        provenance={"trusted": True, "review_state": "approved"},
    ))
    store.close()

    def _exploding_feed(*args, **kwargs):
        raise RuntimeError("extractor exploded")

    monkeypatch.setattr(backfill_graph, "feed", _exploding_feed)
    with pytest.raises(RuntimeError, match="extractor exploded"):
        backfill_graph.backfill(str(path))
    # If the store leaked, reopening would fail on Windows (file locked).
    reopened = Store(str(path))
    reopened.close()
