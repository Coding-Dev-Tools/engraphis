"""First-class code-symbol recall arm."""
from __future__ import annotations

import pytest

from engraphis.core.engine import MemoryEngine
from engraphis.core.interfaces import SearchFilter


def test_code_profile_bridges_symbols_to_scoped_memories(tmp_path):
    (tmp_path / "deploy.py").write_text(
        "def deploy_release():\n    return 'ok'\n",
        encoding="utf-8",
    )
    engine = MemoryEngine.create(":memory:")
    workspace_id = engine.store.get_or_create_workspace("acme")
    repo_id = engine.store.get_or_create_repo(workspace_id, "api")
    engine.index_repo(repo_id, str(tmp_path), prefer="regex")
    memory_id = engine.remember(
        "deploy_release requires a signed tag and a successful backup.",
        workspace_id=workspace_id,
        repo_id=repo_id,
    )

    result = engine.recall_engine.recall(
        "What calls deploy_release()?",
        SearchFilter(
            workspace_id=workspace_id,
            repo_id=repo_id,
            include_ancestors=True,
        ),
        k=5,
        reinforce=False,
        retrieval_profile="code",
        diagnostics=True,
    )

    assert memory_id in {chunk["id"] for chunk in result.chunks}
    detail = next(item for item in result.retrieval_trace if item["id"] == memory_id)
    assert "code" in detail["arms"]
    assert detail["raw"]["code"] > 0


def test_auto_profile_selects_code_without_changing_balanced_default(tmp_path):
    (tmp_path / "worker.py").write_text(
        "def process_queue():\n    return None\n",
        encoding="utf-8",
    )
    engine = MemoryEngine.create(":memory:")
    workspace_id = engine.store.get_or_create_workspace("acme")
    repo_id = engine.store.get_or_create_repo(workspace_id, "worker")
    engine.index_repo(repo_id, str(tmp_path), prefer="regex")
    engine.remember(
        "process_queue drains the durable retry queue.",
        workspace_id=workspace_id,
        repo_id=repo_id,
    )
    flt = SearchFilter(
        workspace_id=workspace_id,
        repo_id=repo_id,
        include_ancestors=True,
    )

    balanced = engine.recall_engine.recall(
        "process_queue()", flt, reinforce=False
    )
    automatic = engine.recall_engine.recall(
        "process_queue()", flt, reinforce=False, retrieval_profile="auto"
    )

    assert balanced.retrieval_profile == "balanced"
    assert automatic.retrieval_profile == "code"


def test_historical_code_arm_fails_closed_for_legacy_store_methods(monkeypatch):
    engine = MemoryEngine.create(":memory:")
    workspace_id = engine.store.get_or_create_workspace("acme")
    repo_id = engine.store.get_or_create_repo(workspace_id, "api")
    calls = []

    def legacy_search_symbols(repo, query, *, limit=20):
        calls.append((repo, query, limit))
        return [{"id": "sym_current", "name": "deploy", "fqname": "deploy"}]

    monkeypatch.setattr(engine.store, "search_symbols", legacy_search_symbols)
    scores = engine.recall_engine._code_arm(
        "deploy()",
        SearchFilter(
            workspace_id=workspace_id,
            repo_id=repo_id,
            valid_at=10.0,
            known_at=10.0,
        ),
        10,
    )

    assert scores == {}
    assert calls == [], "historical reads must never retry without the temporal filter"


def test_code_arm_does_not_mask_type_errors_from_temporal_store(monkeypatch):
    engine = MemoryEngine.create(":memory:")
    workspace_id = engine.store.get_or_create_workspace("acme")
    repo_id = engine.store.get_or_create_repo(workspace_id, "api")

    def broken_search_symbols(repo, query, *, limit=20, flt=None):
        raise TypeError("implementation bug")

    monkeypatch.setattr(engine.store, "search_symbols", broken_search_symbols)
    with pytest.raises(TypeError, match="implementation bug"):
        engine.recall_engine._code_arm(
            "deploy()",
            SearchFilter(workspace_id=workspace_id, repo_id=repo_id),
            10,
        )


def test_code_arm_batches_memory_lookup_for_many_symbols(monkeypatch):
    engine = MemoryEngine.create(":memory:")
    workspace_id = engine.store.get_or_create_workspace("acme")
    repo_id = engine.store.get_or_create_repo(workspace_id, "api")
    symbols = [
        {"id": f"sym_{index:03d}", "name": f"DeployTarget{index}",
         "fqname": f"api.DeployTarget{index}"}
        for index in range(100)
    ]
    batch_calls = []

    monkeypatch.setattr(
        engine.store,
        "search_symbols",
        lambda repo, query, *, limit=20, flt=None: symbols,
    )
    monkeypatch.setattr(
        engine.store,
        "list_code_edges",
        lambda repo, *, limit=None, layers=None, flt=None: [],
    )
    monkeypatch.setattr(
        engine.store,
        "memories_for_symbol",
        lambda *args, **kwargs: pytest.fail("per-symbol lookup must not run"),
    )

    def batched(repo, symbol_ids, *, flt=None, limit=20):
        batch_calls.append((repo, list(symbol_ids), limit))
        return {
            symbol_id: [{"id": f"mem_{symbol_id}", "confidence": 1.0}]
            for symbol_id in symbol_ids
        }

    monkeypatch.setattr(engine.store, "memories_for_symbols", batched)
    scores = engine.recall_engine._code_arm(
        "DeployTarget()", SearchFilter(workspace_id=workspace_id, repo_id=repo_id), 50
    )

    assert len(batch_calls) == 1
    assert batch_calls[0][0] == repo_id
    assert len(batch_calls[0][1]) == 100
    assert len(scores) == 50


def test_code_arm_resolves_incident_symbols_before_global_symbol_cap():
    engine = MemoryEngine.create(":memory:")
    workspace_id = engine.store.get_or_create_workspace("acme")
    repo_id = engine.store.get_or_create_repo(workspace_id, "api")
    entry_id = engine.store.upsert_symbol(
        repo_id=repo_id, kind="function", name="entry", fqname="entry",
        file="a/entry.py", span="1-1",
    )
    for index in range(1_000):
        engine.store.upsert_symbol(
            repo_id=repo_id, kind="function", name=f"noise_{index:04d}",
            fqname=f"noise_{index:04d}", file=f"a/{index:04d}.py", span="1-1",
        )
    late_id = engine.store.upsert_symbol(
        repo_id=repo_id, kind="function", name="late_callee",
        fqname="worker.late_callee", file="z/worker.py", span="1-1",
    )
    engine.store.add_code_edge(
        repo_id=repo_id, src="entry", dst="worker.late_callee",
        relation="calls", file="a/entry.py", line=1,
    )
    memory_id = engine.remember(
        "The remote deployment worker requires a sealed credential.",
        workspace_id=workspace_id, repo_id=repo_id, resolve_conflicts=False,
    )
    engine.store.link_memory_symbol(
        repo_id=repo_id, symbol_id=late_id, memory_id=memory_id,
    )

    # The ordinary sorted prefix contains the caller and unrelated symbols but
    # not the edge's callee in z/worker.py.
    assert late_id not in {
        row["id"] for row in engine.store.list_symbols(repo_id, limit=1_000)
    }
    scores = engine.recall_engine._code_arm(
        "entry()", SearchFilter(workspace_id=workspace_id, repo_id=repo_id), 50
    )

    assert entry_id != late_id
    assert memory_id in scores
