"""Database writer boundaries, durable external repair, and bounded exact search."""
import hashlib
import multiprocessing
import shutil
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError

import numpy as np
import pytest

from engraphis.core.interfaces import MemoryRecord, Scope, SearchFilter
from engraphis.core.vector_repair import canonical_search_required, index_repair_identity
from engraphis.factory import create_memory_engine
from engraphis.service import MemoryService


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


def test_title_publication_acknowledges_repair_and_keeps_configured_search():
    engine = create_memory_engine(auto_evolve=False)
    index = ExternalIndex()
    _use_external(engine, index)
    service = MemoryService(engine)
    workspace = engine.store.get_or_create_workspace("title-repair")
    try:
        memory = engine.remember("Atlas stores memory in SQLite.", workspace_id=workspace)
        service.update_memory(memory, workspace="title-repair", title="Current title")
        np.testing.assert_allclose(index.rows[memory], engine.store.get_vectors([memory])[memory])
        assert engine.store.vector_index_pending(index_repair_identity(index, engine.store)) == 0
        assert not canonical_search_required(index, engine.store)
    finally:
        engine.close()


@pytest.mark.parametrize("erase", [False, True])
def test_delayed_title_publication_never_replays_old_payload(monkeypatch, erase):
    engine = create_memory_engine(auto_evolve=False)
    index = ExternalIndex()
    _use_external(engine, index)
    service = MemoryService(engine)
    workspace = engine.store.get_or_create_workspace("delayed-title")
    publish = service._publish_memory_index_action
    actions = []
    monkeypatch.setattr(service, "_publish_memory_index_action", actions.append)
    try:
        memory = engine.remember("Atlas stores memory in SQLite.", workspace_id=workspace)
        service.update_memory(memory, workspace="delayed-title", title="Old delayed title")
        service.update_memory(memory, workspace="delayed-title", title="Latest canonical title")
        if erase:
            engine.secure_erase(memory)
        else:
            publish(actions[1])
        publications = len(index.published)
        publish(actions[0])
        assert len(index.published) == publications
        if erase:
            assert memory not in index.rows
        else:
            np.testing.assert_allclose(index.rows[memory], engine.store.get_vectors([memory])[memory])
        assert engine.store.vector_index_pending(index_repair_identity(index, engine.store)) == 0
    finally:
        engine.close()


def test_secret_title_cleanup_queues_absent_canonical_vector_until_external_retry():
    engine = create_memory_engine(auto_evolve=False)
    index = ExternalIndex()
    _use_external(engine, index)
    service = MemoryService(engine)
    workspace = engine.store.get_or_create_workspace("secret-title")
    try:
        memory = engine.store.add_memory(MemoryRecord(
            id="", content="Private local record.", sensitivity="secret",
            scope=Scope.WORKSPACE, workspace_id=workspace,
        ))
        # An old external copy can survive even after its canonical vector is gone.
        index.rows[memory] = np.zeros(engine.embedder.dim, dtype=np.float32)
        index.fail = True
        service.update_memory(memory, workspace="secret-title", title="Review label")
        target = index_repair_identity(index, engine.store)
        assert engine.store.vector_index_pending(target) == 1
        assert memory in index.rows
        index.fail = False
        assert engine.repair_vector_index()["repaired"] == 1
        assert memory not in index.rows
        assert engine.store.vector_index_pending(target) == 0
    finally:
        engine.close()


@pytest.mark.parametrize("fail", [False, True])
def test_secure_erase_acknowledges_only_confirmed_external_cleanup(fail):
    engine = create_memory_engine(auto_evolve=False)
    index = ExternalIndex()
    _use_external(engine, index)
    workspace = engine.store.get_or_create_workspace("erase-repair")
    try:
        memory = engine.remember("Atlas stores memory in SQLite.", workspace_id=workspace)
        index.fail = fail
        result = engine.secure_erase(memory)
        assert result["vector_index_cleanup"] == ("failed" if fail else "deleted")
        assert engine.store.get_memory(memory) is None
        target = index_repair_identity(index, engine.store)
        assert engine.store.vector_index_pending(target) == int(fail)
        assert canonical_search_required(index, engine.store) is fail
        index.fail = False
        engine.repair_vector_index()
        assert memory not in index.rows
        assert engine.store.vector_index_pending(target) == 0
    finally:
        engine.close()


def test_secure_erase_serializes_external_delete_against_concurrent_repair(tmp_path, monkeypatch):
    path = str(tmp_path / "erase-race.db")
    first = create_memory_engine(path, auto_evolve=False)
    second = create_memory_engine(path, auto_evolve=False)
    index = ExternalIndex()
    _use_external(first, index)
    _use_external(second, index)
    deleted = threading.Event()
    release = threading.Event()
    try:
        workspace = first.store.get_or_create_workspace("erase-race")
        memory = first.remember("Atlas stores memory in SQLite.", workspace_id=workspace)
        first.store.put_vector(memory, first.store.get_vectors([memory])[memory], model=first.embedding_space)
        first.store.conn.commit()
        delete = index.delete

        def pause_after_delete(ids, *, commit=True):
            delete(ids, commit=commit)
            deleted.set()
            assert release.wait(10), "erasure synchronization timed out"

        monkeypatch.setattr(index, "delete", pause_after_delete)
        with ThreadPoolExecutor(max_workers=2) as pool:
            erasure = pool.submit(first.secure_erase, memory)
            try:
                assert deleted.wait(10), "external deletion did not run"
                replay = pool.submit(second.repair_vector_index, memory_id=memory)
                with pytest.raises(TimeoutError):
                    replay.result(timeout=0.1)
            finally:
                release.set()
            assert erasure.result(timeout=10)["vector_index_cleanup"] == "deleted"
            assert replay.result(timeout=10)["pending"] == 0
        assert memory not in index.rows
        assert first.store.get_memory(memory) is None
    finally:
        release.set()
        first.close()
        second.close()


def test_rebuild_publication_acknowledges_its_confirmed_generation(tmp_path):
    from tests.test_recall_recovery import _engine_for, _VersionedSemanticEmbedder

    path = tmp_path / "rebuild-publication.db"
    index = ExternalIndex()
    first = _engine_for(path, _VersionedSemanticEmbedder("A"))
    _use_external(first, index)
    try:
        first._rebuild_versioned_embeddings()
        workspace = first.store.get_or_create_workspace("rebuild-publication")
        memory = first.remember("Atlas stores memory in SQLite.", workspace_id=workspace)
    finally:
        first.close()
    second = _engine_for(path, _VersionedSemanticEmbedder("B"))
    _use_external(second, index)
    try:
        second._rebuild_versioned_embeddings()
        np.testing.assert_allclose(index.rows[memory], second.store.get_vectors([memory])[memory])
        assert second.store.vector_index_pending(index_repair_identity(index, second.store)) == 0
    finally:
        second.close()


def test_publication_acknowledgement_preserves_newer_canonical_generation():
    engine = create_memory_engine(auto_evolve=False)
    index = ExternalIndex()
    _use_external(engine, index)
    workspace = engine.store.get_or_create_workspace("newer-generation")
    try:
        memory = engine.remember("Atlas stores memory in SQLite.", workspace_id=workspace)
        target = index_repair_identity(index, engine.store)
        for text in ("Earlier title", "Later canonical title"):
            with engine.store.write_transaction():
                engine.store.put_vector(memory, engine.embedder.embed([text])[0], model=engine.embedding_space)
            if text == "Earlier title":
                captured = engine.store.vector_index_repair_generations(target, [memory])
        engine.store.acknowledge_vector_index_repairs(target, captured)
        assert engine.store.vector_index_pending(target) == 1
        assert engine.store.vector_index_repair_generations(target, [memory])[memory] > captured[memory]
        assert canonical_search_required(index, engine.store)
        assert engine.repair_vector_index()["repaired"] == 1
        np.testing.assert_allclose(index.rows[memory], engine.store.get_vectors([memory])[memory])
        assert engine.store.vector_index_pending(target) == 0
    finally:
        engine.close()


@pytest.mark.parametrize("repair_fails", [False, True])
def test_secure_erase_rollback_restores_repair_debt_or_reports_compensation_failure(monkeypatch, repair_fails):
    engine = create_memory_engine(auto_evolve=False)
    index = ExternalIndex()
    _use_external(engine, index)
    workspace = engine.store.get_or_create_workspace("erase-rollback")
    try:
        memory = engine.remember("Atlas stores memory in SQLite.", workspace_id=workspace)
        erase = engine.store.secure_erase_memory
        queue = engine.store.queue_vector_index_repairs

        def fail_after_erase(*args, **kwargs):
            erase(*args, **kwargs)
            assert engine.store.get_memory(memory) is None
            raise RuntimeError("injected canonical erase failure")

        def fail_compensation(*args, **kwargs):
            if not engine.store.conn.transaction_owned_by_current_thread():
                raise RuntimeError("injected repair persistence failure")
            return queue(*args, **kwargs)

        monkeypatch.setattr(engine.store, "secure_erase_memory", fail_after_erase)
        if repair_fails:
            monkeypatch.setattr(engine.store, "queue_vector_index_repairs", fail_compensation)
        expected = "injected repair persistence failure" if repair_fails else "injected canonical erase failure"
        with pytest.raises(RuntimeError, match=expected) as failure:
            engine.secure_erase(memory)
        assert engine.store.get_memory(memory) is not None
        assert memory not in index.rows
        target = index_repair_identity(index, engine.store)
        if repair_fails:
            assert isinstance(failure.value.__cause__, RuntimeError)
            assert str(failure.value.__cause__) == "injected canonical erase failure"
        else:
            assert engine.store.vector_index_pending(target) == 1
            assert canonical_search_required(index, engine.store)
            assert engine.repair_vector_index()["repaired"] == 1
            np.testing.assert_allclose(index.rows[memory], engine.store.get_vectors([memory])[memory])
            assert engine.store.vector_index_pending(target) == 0
    finally:
        engine.close()


def test_external_secure_erase_rejects_caller_transaction_before_provider_deletion(monkeypatch):
    engine = create_memory_engine(auto_evolve=False)
    index = ExternalIndex()
    _use_external(engine, index)
    workspace = engine.store.get_or_create_workspace("caller-erase")
    try:
        memory = engine.remember("Atlas stores memory in SQLite.", workspace_id=workspace)
        deleted = []
        monkeypatch.setattr(index, "delete", lambda ids: deleted.extend(ids))
        engine.store.conn.execute("BEGIN IMMEDIATE")
        with pytest.raises(RuntimeError, match="caller-owned transactions cannot erase"):
            engine.secure_erase(memory)
        assert engine.store.conn.transaction_owned_by_current_thread()
        assert not deleted
        assert memory in index.rows
        engine.store.conn.rollback()
        assert engine.store.get_memory(memory) is not None
        assert engine.store.vector_index_pending(index_repair_identity(index, engine.store)) == 0
    finally:
        engine.close()


def test_existing_v17_database_gains_repair_queue_index_without_losing_debt(tmp_path):
    from engraphis.core.store import Store

    path = tmp_path / "v17-queue-index.db"
    with Store(str(path)) as store:
        store.queue_vector_index_repairs("upgrade-target", ["mem_cleanup"])
    with sqlite3.connect(str(path)) as previous:
        previous.execute("DROP INDEX idx_vector_index_repairs_queue")
    with Store(str(path)) as upgraded:
        assert upgraded.schema_version == 17
        assert upgraded.vector_index_pending("upgrade-target") == 1
        plan = " ".join(str(row[3]) for row in upgraded.conn.execute(
            "EXPLAIN QUERY PLAN SELECT memory_id,generation FROM vector_index_repairs "
            "WHERE identity=? ORDER BY generation,memory_id LIMIT 1", ("upgrade-target",),
        ).fetchall()).upper()
        assert "USE TEMP B-TREE" not in plan
        assert "COVERING INDEX IDX_VECTOR_INDEX_REPAIRS_QUEUE" in plan


@pytest.mark.parametrize("startup_state", ["healthy", "recreated", "failed_rebuild"])
def test_external_startup_honors_rebuild_signal_with_existing_target(tmp_path, startup_state):
    from tests.test_recall_recovery import _engine_for, _VersionedSemanticEmbedder

    class RestartableIndex(ExternalIndex):
        requires_rebuild = False

        def __init__(self):
            super().__init__()
            self.completed = 0

        def mark_rebuild_complete(self):
            self.completed += 1
            self.requires_rebuild = False

    path = tmp_path / "external-startup.db"
    index = RestartableIndex()
    first = _engine_for(path, _VersionedSemanticEmbedder("A"))
    _use_external(first, index)
    try:
        first._rebuild_versioned_embeddings()
        workspace = first.store.get_or_create_workspace("external-startup")
        historical = first.remember("Atlas stores memory in SQLite.", workspace_id=workspace)
        current = first.remember("Whales sing underwater.", workspace_id=workspace)
        first.store.close_validity(historical)
        target = index_repair_identity(index, first.store)
        assert first.store.vector_index_pending(target) == 0
        assert set(index.rows) == {historical, current}
    finally:
        first.close()

    if startup_state != "healthy":
        index = RestartableIndex()
        index.requires_rebuild = True
        assert not index.rows
    index.published.clear()
    second = _engine_for(path, _VersionedSemanticEmbedder("A"))
    _use_external(second, index)
    try:
        assert index_repair_identity(index, second.store) == target
        assert second.store.vector_index_pending(target) == 0
        assert canonical_search_required(index, second.store) is index.requires_rebuild
        if startup_state == "failed_rebuild":
            index.fail = True
            with pytest.raises(RuntimeError, match="external vector index repair is incomplete"):
                second._rebuild_versioned_embeddings()
            assert second.store.vector_index_pending(target) == 2
            assert canonical_search_required(index, second.store)
            assert index.requires_rebuild
            assert index.completed == 0
            index.fail = False
        second._rebuild_versioned_embeddings()
        assert set(index.rows) == {historical, current}
        for memory in (historical, current):
            np.testing.assert_allclose(index.rows[memory], second.store.get_vectors([memory])[memory])
        assert second.store.vector_index_pending(target) == 0
        assert not index.requires_rebuild
        assert not canonical_search_required(index, second.store)
        if startup_state == "healthy":
            assert not index.published
            assert index.completed == 0
        else:
            assert set(index.published) == {historical, current}
            assert index.completed == 1
    finally:
        second.close()
