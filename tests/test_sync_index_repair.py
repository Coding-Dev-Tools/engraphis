"""Sync publication shares canonical truth, repair generations, and native rollback."""
from __future__ import annotations

import numpy as np
import pytest

from engraphis.core.interfaces import MemoryRecord, Scope
from engraphis.core.sync import SYNC_FORMAT, SyncEngine
from engraphis.core.vector_repair import canonical_search_required, index_repair_identity
from engraphis.factory import create_memory_engine


class ExternalIndex:
    index_identity = "sync-repair-regression"

    def __init__(self):
        self.rows = {}
        self.published = []
        self.fail = False

    def search(self, _vector, _k, *, filter=None):
        return []

    def upsert(self, ids, vectors, meta=None, *, commit=True):
        if self.fail:
            raise RuntimeError("injected external outage")
        for memory, vector in zip(ids, vectors):
            self.rows[memory] = vector.copy()
            self.published.append(memory)

    def delete(self, ids, *, commit=True):
        if self.fail:
            raise RuntimeError("injected external outage")
        for memory in ids:
            self.rows.pop(memory, None)


def _bundle(content="Original synced evidence.", *, stamp=1.0, count=1):
    return {
        "format": SYNC_FORMAT, "version": 1, "workspace_name": "sync-index",
        "repos": {}, "mem_links": [],
        "memories": [{
            "id": f"mem_sync_{number}", "content": content + f" {number}",
            "valid_from": 1.0, "ingested_at": stamp, "last_access": stamp,
        } for number in range(count)],
    }


@pytest.fixture
def external_sync():
    engine = create_memory_engine(auto_evolve=False)
    index = ExternalIndex()
    engine.index = engine.recall_engine.index = index
    sync = SyncEngine(engine.store, embedder=engine.embedder, vector_index=index)
    try:
        yield engine, index, sync
    finally:
        engine.close()


def test_sync_registers_and_acknowledges_its_external_repair(external_sync):
    engine, index, sync = external_sync
    target = index_repair_identity(index, engine.store)
    assert engine.store.vector_index_pending(target) is None
    assert sync.apply_bundle(_bundle())["added"] == 1
    np.testing.assert_array_equal(index.rows["mem_sync_0"], engine.store.get_vectors(["mem_sync_0"])["mem_sync_0"])
    assert engine.store.vector_index_pending(target) == 0
    assert not canonical_search_required(index, engine.store)


@pytest.mark.parametrize("next_operation", ["erase", "newer_write"])
def test_delayed_sync_publication_replays_current_canonical_state(external_sync, monkeypatch, next_operation):
    engine, index, sync = external_sync
    publish = sync._publish_index_actions
    actions = []
    monkeypatch.setattr(sync, "_publish_index_actions", lambda pending: actions.extend(pending))
    sync.apply_bundle(_bundle())
    delayed = actions[0]
    if next_operation == "erase":
        engine.secure_erase("mem_sync_0")
    else:
        sync.apply_bundle(_bundle("Latest replacement evidence.", stamp=2.0))
    publish([delayed])
    if next_operation == "erase":
        assert engine.store.get_memory("mem_sync_0") is None
        assert "mem_sync_0" not in index.rows
    else:
        assert engine.store.get_memory("mem_sync_0").content.startswith("Latest replacement")
        np.testing.assert_array_equal(index.rows["mem_sync_0"], engine.store.get_vectors(["mem_sync_0"])["mem_sync_0"])
        count = len(index.published)
        publish(actions)
        assert len(index.published) == count
    assert engine.store.vector_index_pending(index_repair_identity(index, engine.store)) == 0


def test_sync_outage_retains_durable_work_until_canonical_retry(external_sync):
    engine, index, sync = external_sync
    index.fail = True
    assert sync.apply_bundle(_bundle())["added"] == 1
    target = index_repair_identity(index, engine.store)
    assert engine.store.vector_index_pending(target) == 1
    assert canonical_search_required(index, engine.store)
    index.fail = False
    assert engine.repair_vector_index()["repaired"] == 1
    np.testing.assert_array_equal(index.rows["mem_sync_0"], engine.store.get_vectors(["mem_sync_0"])["mem_sync_0"])
    assert engine.store.vector_index_pending(target) == 0


def test_sync_acknowledgement_cannot_drop_a_newer_generation(external_sync, monkeypatch):
    engine, index, sync = external_sync
    original = index.upsert
    replacement = engine.embedder.embed(["A later canonical vector."])[0]

    def mutate_after_publication(ids, vectors, meta=None, *, commit=True):
        original(ids, vectors, meta, commit=commit)
        engine.store.put_vector(ids[0], replacement, model=engine.embedding_space)

    monkeypatch.setattr(index, "upsert", mutate_after_publication)
    sync.apply_bundle(_bundle())
    target = index_repair_identity(index, engine.store)
    assert engine.store.vector_index_pending(target) == 1
    monkeypatch.setattr(index, "upsert", original)
    engine.repair_vector_index()
    np.testing.assert_array_equal(index.rows["mem_sync_0"], engine.store.get_vectors(["mem_sync_0"])["mem_sync_0"])
    assert engine.store.vector_index_pending(target) == 0


@pytest.mark.parametrize("state", ["present", "provider_outage", "orphan"])
def test_peer_tombstone_publishes_durable_external_cleanup(external_sync, state):
    engine, index, sync = external_sync
    workspace = engine.store.get_or_create_workspace("sync-index")
    memory_id = "mem_peer_erased"
    if state == "orphan":
        index.rows[memory_id] = engine.embedder.embed(["Orphaned evidence."])[0]
    else:
        # Legacy peer data without a pending-review hold is eligible for erasure.
        sync._write(MemoryRecord(
            id=memory_id, content="Previously synced evidence.",
            workspace_id=workspace, scope=Scope.WORKSPACE,
            provenance={"source": "sync", "trusted": False, "review_state": ""},
        ))
    assert memory_id in index.rows
    index.fail = state == "provider_outage"
    bundle = {
        **_bundle(count=0), "device_id": "peer", "tombstones": [{
            "id": memory_id, "deleted_at": 10.0, "device": "peer",
            "export_class": "remote_erasure",
        }],
    }
    assert sync.apply_bundle(bundle)["tombstones_applied"] == 1
    assert engine.store.get_memory(memory_id) is None
    target = index_repair_identity(index, engine.store)
    if state == "provider_outage":
        assert engine.store.vector_index_pending(target) == 1
        index.fail = False
        # Re-offering an unchanged marker retries cleanup without requiring a row.
        assert sync.apply_bundle(bundle)["tombstones_applied"] == 0
    assert memory_id not in index.rows
    assert engine.store.vector_index_pending(target) == 0


def test_file_backed_peer_erasure_does_not_require_an_embedder(tmp_path):
    engine = create_memory_engine(str(tmp_path / "erasure.db"), auto_evolve=False)
    index = ExternalIndex()
    workspace = engine.store.get_or_create_workspace("sync-index")
    try:
        SyncEngine(engine.store, embedder=engine.embedder, vector_index=index)._write(
            MemoryRecord(
                id="mem_erased", content="Legacy evidence.", workspace_id=workspace,
                scope=Scope.WORKSPACE,
                provenance={"source": "sync", "trusted": False, "review_state": ""},
            ),
        )
        assert "mem_erased" in index.rows
        sync = SyncEngine(engine.store, vector_index=index)
        bundle = {
            **_bundle(count=0), "device_id": "peer", "tombstones": [{
                "id": "mem_erased", "deleted_at": 10.0, "device": "peer",
                "export_class": "remote_erasure",
            }],
        }
        assert sync.apply_bundle(bundle)["tombstones_applied"] == 1
        assert engine.store.get_memory("mem_erased") is None
        assert "mem_erased" not in index.rows
        assert engine.store.vector_index_pending(index_repair_identity(index, engine.store)) == 0
    finally:
        engine.close()


@pytest.mark.parametrize("failure_at", [1, 2])
def test_sync_native_upsert_failure_rolls_back_current_batch(monkeypatch, failure_at):
    pytest.importorskip("sqlite_vec", reason="sqlite-vec extra not installed")
    engine = create_memory_engine(vector_backend="sqlite-vec", embed_dim=64, auto_evolve=False)
    sync = SyncEngine(engine.store, embedder=engine.embedder, vector_index=engine.index)
    original = engine.index.upsert
    calls = 0

    def fail_after_native_write(*args, **kwargs):
        nonlocal calls
        calls += 1
        original(*args, **kwargs)
        if calls == failure_at:
            raise RuntimeError("injected native sync failure")

    monkeypatch.setattr(engine.index, "upsert", fail_after_native_write)
    try:
        with pytest.raises(RuntimeError, match="injected native sync failure"):
            sync.apply_bundle(_bundle(count=2))
        for table in ("memories", "mem_vectors", "mem_fts", "mem_vec_ann"):
            assert engine.store.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        assert not engine.store.conn.in_transaction
    finally:
        engine.close()


def test_sync_native_delete_failure_restores_canonical_and_native_state(monkeypatch):
    pytest.importorskip("sqlite_vec", reason="sqlite-vec extra not installed")
    engine = create_memory_engine(vector_backend="sqlite-vec", embed_dim=64, auto_evolve=False)
    sync = SyncEngine(engine.store, embedder=engine.embedder, vector_index=engine.index)
    try:
        sync.apply_bundle(_bundle())
        record = engine.store.get_memory("mem_sync_0")
        vector = engine.store.get_vectors([record.id])[record.id].copy()
        record.metadata["provenance"] = {**record.provenance, "quarantined": True}
        original = engine.index.delete

        def fail_after_native_delete(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("injected native delete failure")

        monkeypatch.setattr(engine.index, "delete", fail_after_native_delete)
        with pytest.raises(RuntimeError, match="injected native delete failure"):
            sync._write(record)
        assert not engine.store.get_memory(record.id).provenance.get("quarantined")
        np.testing.assert_array_equal(engine.store.get_vectors([record.id])[record.id], vector)
        assert engine.store.conn.execute("SELECT COUNT(*) FROM mem_vec_ann").fetchone()[0] == 1
        assert not engine.store.conn.in_transaction
    finally:
        engine.close()


def test_sync_native_batch_has_one_visibility_boundary(tmp_path, monkeypatch):
    pytest.importorskip("sqlite_vec", reason="sqlite-vec extra not installed")
    path = str(tmp_path / "sync-native.db")
    engine = create_memory_engine(path, vector_backend="sqlite-vec", embed_dim=64, auto_evolve=False)
    observer = create_memory_engine(path, vector_backend="sqlite-vec", embed_dim=64, auto_evolve=False)
    sync = SyncEngine(engine.store, embedder=engine.embedder, vector_index=engine.index)
    original = engine.index.upsert

    def observe(*args, **kwargs):
        assert engine.store.conn.transaction_owned_by_current_thread()
        original(*args, **kwargs)
        assert observer.store.count_memories() == 0
        assert observer.store.conn.execute("SELECT COUNT(*) FROM mem_vec_ann").fetchone()[0] == 0

    monkeypatch.setattr(engine.index, "upsert", observe)
    try:
        assert sync.apply_bundle(_bundle(count=2))["added"] == 2
        for table in ("memories", "mem_vectors", "mem_vec_ann"):
            assert observer.store.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 2
    finally:
        observer.close()
        engine.close()
