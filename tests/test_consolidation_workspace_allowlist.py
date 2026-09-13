"""Batched citation visibility must preserve the Store's workspace authority."""
from contextlib import closing

import pytest

from engraphis.backends import DeterministicEmbedder
from engraphis.backends.vector_sqlitevec import get_vector_index
from engraphis.core.engine import MemoryEngine
from engraphis.core.interfaces import MemoryRecord, Scope, SearchFilter
from engraphis.core.store import Store


@pytest.fixture
def restricted_store(tmp_path):
    path = str(tmp_path / "scoped.db")
    with closing(Store(path)) as seed:
        allowed = seed.get_or_create_workspace("allowed")
        hidden = seed.get_or_create_workspace("hidden")
        approved = {"source": "test", "trusted": True, "review_state": "approved"}

        def add(workspace, content, **fields):
            return seed.add_memory(MemoryRecord(
                id="", content=content, workspace_id=workspace, scope=Scope.WORKSPACE,
                valid_from=10, ingested_at=10, provenance=approved, **fields,
            ))

        public_id = add(allowed, "Visible source")
        closed_id = add(allowed, "Earlier source", valid_to=15, valid_to_recorded_at=15)
        hidden_id = add(hidden, "Private source")
        provenance = {**approved, "source": "consolidation",
                      "consolidates": [hidden_id, public_id, closed_id]}
        digest_id = seed.add_memory(MemoryRecord(
            id="", content="Build reliability summary", workspace_id=allowed,
            scope=Scope.WORKSPACE, valid_from=10, ingested_at=10,
            provenance=provenance, metadata={"provenance": provenance},
        ))
    with closing(Store(path, allowed_workspaces={"allowed"})) as store:
        yield store, hidden, public_id, closed_id, hidden_id, digest_id


@pytest.mark.parametrize("filter_kind", ["none", "unscoped", "foreign"])
@pytest.mark.parametrize("include_invalid", [False, True])
def test_batched_visibility_applies_instance_allowlist(restricted_store, filter_kind, include_invalid):
    store, hidden, public_id, closed_id, hidden_id, _ = restricted_store
    flt = {"none": None, "unscoped": SearchFilter(),
           "foreign": SearchFilter(workspace_id=hidden)}[filter_kind]
    assert store.get_memory(hidden_id) is None
    statements = []
    store.conn.set_trace_callback(statements.append)
    try:
        visible = store.visible_memory_ids(
            [public_id, closed_id, hidden_id, public_id, "mem_missing"],
            flt, include_invalid=include_invalid,
        )
    finally:
        store.conn.set_trace_callback(None)
    expected = set() if filter_kind == "foreign" else {public_id}
    if include_invalid and filter_kind != "foreign":
        expected.add(closed_id)
    assert visible == expected
    assert len([sql for sql in statements if sql.lstrip().upper().startswith("SELECT")]) == 1


def test_direct_recall_omits_disallowed_consolidation_sources(restricted_store):
    store, _, public_id, _, hidden_id, digest_id = restricted_store
    with closing(MemoryEngine(
        store, DeterministicEmbedder(32), get_vector_index(store, dim=32, prefer="numpy"),
        auto_evolve=False,
    )) as engine:
        result = engine.recall("Build reliability summary", reinforce=False)
    chunk = next(item for item in result.chunks if item["id"] == digest_id)
    assert chunk["consolidation_source_ids"] == [public_id]
    assert result.source_metadata[digest_id]["consolidation_source_ids"] == [public_id]
    assert hidden_id not in {item["id"] for item in result.chunks}
