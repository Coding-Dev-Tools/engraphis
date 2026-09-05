"""Database writer boundaries, durable external repair, and bounded exact search."""
import hashlib
import multiprocessing
import shutil
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest

from engraphis.core.interfaces import MemoryRecord, Scope, SearchFilter
from engraphis.core.vector_repair import canonical_search_required, index_repair_identity
from engraphis.factory import create_memory_engine


class ExternalIndex:
    index_identity = "test-storage-concurrency-repair"

    def __init__(self):
        self.rows = {}
        self.fail = False
        self.published = []

    def search(self, vector, k, *, filter=None):
        query = vector / max(float(np.linalg.norm(vector)), 1e-12)
        return sorted(((mid, float(value @ query)) for mid, value in self.rows.items()),
                      key=lambda row: (-row[1], row[0]))[:k]

    def upsert(self, ids, vectors, meta=None, *, commit=True):
        if self.fail:
            raise RuntimeError("injected external failure")
        self.published.extend(ids)
        for mid, vector in zip(ids, vectors):
            self.rows[mid] = vector / max(float(np.linalg.norm(vector)), 1e-12)

    def delete(self, ids, *, commit=True):
        if self.fail:
            raise RuntimeError("injected external failure")
        for mid in ids:
            self.rows.pop(mid, None)


def _use_external(engine, index):
    engine.index = index
    engine.recall_engine.index = index


def _process_remember(path, workspace, ready, proceed, results):
    engine = create_memory_engine(path, auto_evolve=False)
    try:
        ready.put(True)
        if not proceed.wait(20):
            raise RuntimeError("writer synchronization timed out")
        result = engine.remember_with_resolution(
            "Atlas stores memory in SQLite.", workspace_id=workspace,
        )
        results.put((result["op"], result["id"]))
    finally:
        engine.close()


def _process_claim(path, workspace, seconds, ready, proceed, results):
    engine = create_memory_engine(path, auto_evolve=False)
    try:
        ready.put(True)
        if not proceed.wait(20):
            raise RuntimeError("writer synchronization timed out")
        result = engine.remember_with_resolution(
            f"The cache TTL is {seconds} seconds.", workspace_id=workspace,
            subject_key="cache", claim_kind="ttl",
        )
        results.put((result["op"], result["id"]))
    finally:
        engine.close()


def test_independent_engine_writes_resolve_after_writer_reservation(tmp_path, monkeypatch):
    path = str(tmp_path / "concurrent.db")
    first = create_memory_engine(path, auto_evolve=False)
    workspace = first.store.get_or_create_workspace("concurrent")
    second = create_memory_engine(path, auto_evolve=False)
    barrier = threading.Barrier(2, timeout=10)
    for engine in (first, second):
        original = engine.embedder.embed

        def embed(texts, *, kind="text", original=original, engine=engine):
            # Expensive embedding must remain outside the database reservation.
            assert not engine.store.conn.transaction_owned_by_current_thread()
            vectors = original(texts, kind=kind)
            barrier.wait()
            return vectors

        monkeypatch.setattr(engine.embedder, "embed", embed)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda engine: engine.remember_with_resolution(
                "Atlas stores memory in SQLite.", workspace_id=workspace,
            ), (first, second)))
        assert sorted(item["op"] for item in results) == ["add", "noop"]
        assert len({item["id"] for item in results}) == 1
        assert first.store.count_memories() == 1
    finally:
        first.close()
        second.close()


def test_separate_process_writers_do_not_duplicate(tmp_path):
    path = str(tmp_path / "processes.db")
    engine = create_memory_engine(path, auto_evolve=False)
    workspace = engine.store.get_or_create_workspace("processes")
    engine.close()
    context = multiprocessing.get_context("spawn")
    ready, results = context.Queue(), context.Queue()
    proceed = context.Event()
    workers = [context.Process(target=_process_remember,
                               args=(path, workspace, ready, proceed, results))
               for _ in range(2)]
    try:
        for worker in workers:
            worker.start()
        for _ in workers:
            assert ready.get(timeout=20)
        proceed.set()
        observed = [results.get(timeout=20) for _ in workers]
        assert sorted(item[0] for item in observed) == ["add", "noop"]
        assert len({item[1] for item in observed}) == 1
        for worker in workers:
            worker.join(timeout=20)
            assert worker.exitcode == 0
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=10)
        ready.close()
        results.close()


def test_separate_process_contradictions_preserve_one_current_claim_and_history(tmp_path):
    path = str(tmp_path / "contradictions.db")
    engine = create_memory_engine(path, auto_evolve=False)
    workspace = engine.store.get_or_create_workspace("processes")
    original = engine.remember(
        "The cache TTL is 30 seconds.", workspace_id=workspace,
        subject_key="cache", claim_kind="ttl",
    )
    engine.close()
    context = multiprocessing.get_context("spawn")
    ready, results = context.Queue(), context.Queue()
    proceed = context.Event()
    workers = [context.Process(target=_process_claim,
                               args=(path, workspace, seconds, ready, proceed, results))
               for seconds in (90, 180)]
    try:
        for worker in workers:
            worker.start()
        for _ in workers:
            assert ready.get(timeout=20)
        proceed.set()
        observed = [results.get(timeout=20) for _ in workers]
        assert [item[0] for item in observed] == ["invalidate", "invalidate"]
        assert len({item[1] for item in observed}) == 2
        for worker in workers:
            worker.join(timeout=20)
            assert worker.exitcode == 0
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=10)
        ready.close()
        results.close()
    reopened = create_memory_engine(path, auto_evolve=False)
    try:
        ids = [original, *(item[1] for item in observed)]
        records = [reopened.store.get_memory(mid) for mid in ids]
        assert {record.content for record in records} == {
            "The cache TTL is 30 seconds.", "The cache TTL is 90 seconds.",
            "The cache TTL is 180 seconds.",
        }
        live = [record for record in records if record.valid_to is None]
        assert len(live) == 1 and live[0].id != original
        predecessor = reopened.store.get_memory(live[0].metadata["supersedes"][0])
        assert predecessor.id != original
        assert predecessor.metadata["supersedes"] == [original]
        assert predecessor.valid_to is not None
        assert reopened.store.conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        reopened.close()


def test_partial_external_failure_preserves_resolution_and_repairs_live():
    engine = create_memory_engine(auto_evolve=False)
    index = ExternalIndex()
    _use_external(engine, index)
    workspace = engine.store.get_or_create_workspace("repair")
    try:
        engine.remember("Humpback whales sing underwater.", workspace_id=workspace)
        index.fail = True
        first = engine.remember_with_resolution(
            "Atlas stores memory in SQLite.", workspace_id=workspace,
        )
        assert canonical_search_required(index, engine.store)
        index.fail = False
        repeated = engine.remember_with_resolution(
            "Atlas stores memory in SQLite.", workspace_id=workspace,
        )
        assert repeated["op"] == "noop"
        assert repeated["id"] == first["id"]
        repaired = engine.repair_vector_index()
        assert repaired == {"attempted": 1, "repaired": 1, "pending": 0}
        assert first["id"] in index.rows
        assert not canonical_search_required(index, engine.store)
    finally:
        engine.close()


def test_repair_survives_restart_and_never_republishes_erased_memory(tmp_path):
    path = str(tmp_path / "repair.db")
    engine = create_memory_engine(path, auto_evolve=False)
    index = ExternalIndex()
    _use_external(engine, index)
    workspace = engine.store.get_or_create_workspace("repair")
    first = engine.remember("Atlas stores memory in SQLite.", workspace_id=workspace)
    index.fail = True
    second = engine.remember("Whales sing underwater.", workspace_id=workspace)
    # The canonical erasure transaction queues an external DELETE even when the
    # backend is unavailable. No memory content is retained in that work item.
    engine.store.secure_erase_memory(first)
    engine.close()
    reopened = create_memory_engine(path, auto_evolve=False)
    _use_external(reopened, index)
    index.fail = False
    prior_publications = len(index.published)
    try:
        rows = reopened.store.conn.execute("SELECT * FROM vector_index_repairs").fetchall()
        assert len(rows) == 2
        assert set(rows[0].keys()) == {"identity", "memory_id", "generation"}
        outcome = reopened.repair_vector_index()
        assert outcome == {"attempted": 2, "repaired": 2, "pending": 0}
        assert first not in index.rows
        assert first not in index.published[prior_publications:]
        assert second in index.rows
    finally:
        reopened.close()


def test_repair_work_rolls_back_with_failed_authoritative_write(monkeypatch):
    engine = create_memory_engine(auto_evolve=False)
    index = ExternalIndex()
    _use_external(engine, index)
    workspace = engine.store.get_or_create_workspace("rollback")

    def fail(*args, **kwargs):
        raise RuntimeError("injected late failure")

    monkeypatch.setattr(engine, "_evolve", fail)
    try:
        with pytest.raises(RuntimeError, match="injected late failure"):
            engine.remember("Atlas stores memory in SQLite.", workspace_id=workspace)
        assert engine.store.count_memories() == 0
        assert engine.store.conn.execute("SELECT COUNT(*) FROM vector_index_repairs").fetchone()[0] == 0
        assert not index.rows
    finally:
        engine.close()


def test_bounded_vector_batches_preserve_exact_order_and_scope(monkeypatch):
    import engraphis.core.store as store_module

    monkeypatch.setattr(store_module, "VECTOR_SCAN_BATCH", 7)
    engine = create_memory_engine(embed_dim=3, auto_evolve=False)
    workspace = engine.store.get_or_create_workspace("vectors")
    other = engine.store.get_or_create_workspace("other")
    try:
        for number in range(40):
            engine.store.add_memory(MemoryRecord(
                id=f"mem_{number:04d}", content=str(number), scope=Scope.WORKSPACE,
                workspace_id=workspace, embedding=np.array([1.0, number % 4, 0.0]),
                metadata={"embed_model": engine.embedding_space},
            ))
        engine.store.add_memory(MemoryRecord(
            id="mem_other", content="other", scope=Scope.WORKSPACE, workspace_id=other,
            embedding=np.array([1.0, 0.0, 0.0]),
            metadata={"embed_model": engine.embedding_space},
        ))
        flt = SearchFilter(workspace_id=workspace)
        query = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        ids, matrix = engine.store.vector_matrix(flt, dim=3)
        expected = sorted(zip(ids, (matrix @ query).tolist()), key=lambda item: (-item[1], item[0]))[:11]
        original = engine.store.iter_vector_matrices
        sizes = []

        def batches(*args, **kwargs):
            for batch_ids, batch_matrix in original(*args, **kwargs):
                sizes.append(len(batch_ids))
                yield batch_ids, batch_matrix

        monkeypatch.setattr(engine.store, "iter_vector_matrices", batches)
        actual = engine.index.search(query, 11, filter=flt)
        assert actual == expected
        assert len(sizes) > 1 and max(sizes) <= 7
        assert not engine.store.conn.in_transaction
    finally:
        engine.close()


def test_external_target_identity_does_not_persist_connection_secrets():
    engine = create_memory_engine(auto_evolve=False)
    index = ExternalIndex()
    index.index_identity = "https://user:credential@example.invalid/private"
    try:
        target = index_repair_identity(index, engine.store)
        assert target.startswith("index:v1:")
        assert "credential" not in target
    finally:
        engine.close()


def test_recall_uses_canonical_vectors_and_reports_pending_repairs(monkeypatch):
    from engraphis import factory
    from engraphis.backends import DeterministicEmbedder
    from engraphis.core.retrieval_policy import ProfileConfig

    class SemanticEmbedder(DeterministicEmbedder):
        supports_semantic_search = True
        embedding_mode = "semantic"

    monkeypatch.setattr(factory, "get_embedder", lambda *args, **kwargs: SemanticEmbedder(384))
    engine = create_memory_engine(auto_evolve=False)
    index = ExternalIndex()
    _use_external(engine, index)
    workspace = engine.store.get_or_create_workspace("semantic-repair")
    try:
        engine.remember("Whales sing underwater.", workspace_id=workspace)
        index.fail = True
        memory = engine.remember("Atlas stores memory in SQLite.", workspace_id=workspace)
        result = engine.recall_engine.recall(
            "Atlas stores memory in SQLite.", SearchFilter(workspace_id=workspace), k=1,
            arm_config=ProfileConfig("vector_only", True, False, False, False),
        )
        assert result.chunks[0]["id"] == memory
        assert result.vector_search_source == "canonical"
        assert result.vector_index_repairs_pending == 1
        assert result.vector_search_ready
    finally:
        engine.close()


def test_v16_upgrade_adds_durable_repair_without_changing_memories(tmp_path):
    from engraphis.core.store import Store

    path = str(tmp_path / "v16.db")
    engine = create_memory_engine(path, auto_evolve=False)
    workspace = engine.store.get_or_create_workspace("migration")
    memory = engine.remember("Atlas stores memory in SQLite.", workspace_id=workspace)
    engine.close()
    # Model the v16 shape for the only tables/triggers this additive migration owns.
    with sqlite3.connect(path) as previous:
        for action in ("insert", "update", "delete"):
            previous.execute(f"DROP TRIGGER trg_vector_repair_{action}")
        for table in ("vector_index_repairs", "vector_index_targets", "vector_store_state"):
            previous.execute(f"DROP TABLE {table}")
        previous.execute("DELETE FROM schema_migrations WHERE version=17")
    with Store(path) as upgraded:
        assert upgraded.schema_version == 17
        assert upgraded.get_memory(memory).content == "Atlas stores memory in SQLite."
        assert upgraded.vector_generation() == 0
        assert upgraded.conn.execute("PRAGMA foreign_key_check").fetchall() == []
    backup = tmp_path / "v16.db.pre-migration-v17.bak"
    assert backup.exists()
    with sqlite3.connect(str(backup)) as recovery:
        assert recovery.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 16
        assert recovery.execute("SELECT content FROM memories WHERE id=?", (memory,)).fetchone()[0] == (
            "Atlas stores memory in SQLite."
        )
    # Restore into a disposable copy, start the real engine, and exercise retrieval
    # plus a governed correction. The immutable backup and original stay untouched.
    backup_digest = hashlib.sha256(backup.read_bytes()).digest()
    restored_path = tmp_path / "restored.db"
    shutil.copyfile(backup, restored_path)
    restored = create_memory_engine(str(restored_path), auto_evolve=False)
    try:
        recalled = restored.recall("Atlas stores memory in SQLite.", workspace_id=workspace)
        assert recalled.count == 1 and "SQLite" in recalled.context
        corrected = restored.correct(memory, "Atlas stores restored memory in SQLite.", reason="recovery drill")
        assert restored.store.get_memory(memory).valid_to is not None
        assert restored.store.get_memory(corrected["id"]).metadata["supersedes"] == [memory]
        assert restored.store.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert restored.store.conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        restored.close()
    assert hashlib.sha256(backup.read_bytes()).digest() == backup_digest
    with sqlite3.connect(path) as original:
        assert original.execute("SELECT valid_to FROM memories WHERE id=?", (memory,)).fetchone()[0] is None
        assert original.execute("SELECT count(*) FROM memories").fetchone()[0] == 1


def test_repair_dequeue_uses_covering_order_index_without_sorting():
    engine = create_memory_engine(auto_evolve=False)
    target = "query-plan-review"
    try:
        engine.store.register_vector_index(target)
        engine.store.conn.executemany(
            "INSERT INTO vector_index_repairs(identity,memory_id,generation) VALUES (?,?,?)",
            [(target, f"mem_{index:05d}", (10_000 - index) // 3) for index in range(10_000)],
        )
        engine.store.conn.commit()
        sql = ("SELECT memory_id,generation FROM vector_index_repairs WHERE identity=? "
               "ORDER BY generation,memory_id LIMIT 1")
        plan = " ".join(str(row[3]) for row in engine.store.conn.execute(
            "EXPLAIN QUERY PLAN " + sql, (target,),
        ).fetchall()).upper()
        assert "USE TEMP B-TREE" not in plan
        assert "COVERING INDEX IDX_VECTOR_INDEX_REPAIRS_QUEUE" in plan
        assert tuple(engine.store.conn.execute(sql, (target,)).fetchone()) == ("mem_09998", 0)
    finally:
        engine.close()
