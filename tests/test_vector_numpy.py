import sqlite3
from contextlib import contextmanager
from dataclasses import fields
from pathlib import Path

import numpy as np
import pytest

from engraphis.backends import DeterministicEmbedder, NumpyVectorIndex
from engraphis.backends.vector_numpy import _filter_key, _top_k_indices
from engraphis.backends.vector_sqlitevec import _cosine_from_l2
from engraphis.core import relocation, store as core_store
from engraphis.core.engine import MemoryEngine
from engraphis.core.interfaces import (
    MemoryRecord,
    MemoryType,
    Scope,
    SearchFilter,
    vector_index_requires_sync,
)
from engraphis.core.store import Store
from scripts.repair_embed_dim import repair


def test_search_ranks_relevant_memory_first():
    store = Store(":memory:")
    wid = store.get_or_create_workspace("w")
    rid = store.get_or_create_repo(wid, "r")
    emb = DeterministicEmbedder(dim=256)
    index = NumpyVectorIndex(store)

    texts = {
        "pm": "We standardized on pnpm as the package manager for all frontend repos.",
        "sky": "The afternoon sky over the harbor was a pale shade of blue.",
    }
    ids = {}
    for tag, text in texts.items():
        vec = emb.embed([text])[0]
        ids[tag] = store.add_memory(MemoryRecord(id="", content=text, scope=Scope.REPO,
                                                 workspace_id=wid, repo_id=rid, embedding=vec))

    hits = index.search(emb.embed(["which package manager do we use?"])[0], k=2)
    assert hits[0][0] == ids["pm"]
    assert hits[0][1] >= hits[1][1]
    store.close()


def test_store_backed_index_sync_capability_requires_the_same_store():
    canonical = Store(":memory:")
    other = Store(":memory:")
    try:
        assert vector_index_requires_sync(NumpyVectorIndex(canonical), canonical) is False
        assert vector_index_requires_sync(NumpyVectorIndex(other), canonical) is True
        assert vector_index_requires_sync(object(), canonical) is True
        assert vector_index_requires_sync(None, canonical) is False
    finally:
        canonical.close()
        other.close()


def test_upsert_without_metadata_uses_active_embedding_space():
    store = Store(":memory:")
    try:
        fingerprint = "deterministic:test:v1"
        store.begin_embedding_rebuild(fingerprint)
        store.finish_embedding_rebuild(
            fingerprint, identity="deterministic", version="v1"
        )
        workspace_id = store.get_or_create_workspace("w")
        memory_id = store.add_memory(MemoryRecord(
            id="", content="metadata-free vector", workspace_id=workspace_id,
        ))
        NumpyVectorIndex(store, dim=3).upsert(
            [memory_id], np.array([[1.0, 0.0, 0.0]], dtype=np.float32)
        )
        row = store.conn.execute(
            "SELECT model FROM mem_vectors WHERE id=?", (memory_id,)
        ).fetchone()
        assert row["model"] == fingerprint
    finally:
        store.close()


def test_upsert_failure_rolls_back_the_whole_owned_batch():
    store = Store(":memory:")
    try:
        fingerprint = "deterministic:test:v1"
        store.begin_embedding_rebuild(fingerprint)
        store.finish_embedding_rebuild(
            fingerprint, identity="deterministic", version="v1"
        )
        workspace_id = store.get_or_create_workspace("w")
        ids = [
            store.add_memory(MemoryRecord(
                id="", content=f"vector {index}", workspace_id=workspace_id,
            ))
            for index in range(2)
        ]
        index = NumpyVectorIndex(store, dim=3)

        with pytest.raises(RuntimeError, match="embedding-space contract"):
            index.upsert(
                ids,
                np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]], dtype=np.float32),
                [{"model": fingerprint}, {"model": "wrong-space"}],
            )

        assert store.conn.in_transaction is False
        assert store.conn.execute(
            "SELECT COUNT(*) FROM mem_vectors WHERE id IN (?, ?)", ids,
        ).fetchone()[0] == 0
    finally:
        store.close()


def test_upsert_does_not_commit_a_caller_owned_transaction():
    store = Store(":memory:")
    try:
        workspace_id = store.get_or_create_workspace("w")
        memory_id = store.add_memory(MemoryRecord(
            id="", content="caller-owned vector", workspace_id=workspace_id,
        ))
        index = NumpyVectorIndex(store, dim=3)
        store.conn.execute("BEGIN IMMEDIATE")

        index.upsert(
            [memory_id], np.array([[1.0, 0.0, 0.0]], dtype=np.float32)
        )

        assert store.conn.in_transaction is True
        assert store.conn.transaction_owned_by_current_thread() is True
        store.conn.rollback()
        assert store.conn.execute(
            "SELECT 1 FROM mem_vectors WHERE id=?", (memory_id,)
        ).fetchone() is None
    finally:
        store.close()


def test_engine_numpy_write_persists_the_canonical_vector_once(monkeypatch):
    engine = MemoryEngine.create(":memory:", vector_backend="numpy")
    workspace_id = engine.store.get_or_create_workspace("write-count")
    calls = []
    original = engine.store.put_vector

    def traced_put_vector(memory_id, vector, *, model=""):
        calls.append(memory_id)
        return original(memory_id, vector, model=model)

    monkeypatch.setattr(engine.store, "put_vector", traced_put_vector)
    memory_id = engine.remember(
        "A single canonical vector write remains recallable.",
        workspace_id=workspace_id,
        resolve_conflicts=False,
    )

    assert calls == [memory_id]
    query = engine.embedder.embed(["canonical vector write"])[0]
    assert memory_id in {mid for mid, _score in engine.index.search(query, 3)}
    engine.store.close()


def test_search_skips_vectors_from_other_embedding_dimensions():
    store = Store(":memory:")
    wid = store.get_or_create_workspace("w")
    rid = store.get_or_create_repo(wid, "r")
    emb = DeterministicEmbedder(dim=384)
    index = NumpyVectorIndex(store)
    matching = store.add_memory(MemoryRecord(
        id="", content="matching vector", workspace_id=wid, repo_id=rid,
        embedding=emb.embed(["matching vector"])[0]))
    store.add_memory(MemoryRecord(
        id="", content="legacy vector", workspace_id=wid, repo_id=rid,
        embedding=DeterministicEmbedder(dim=256).embed(["legacy vector"])[0]))

    hits = index.search(emb.embed(["matching vector"])[0], k=5)

    assert [memory_id for memory_id, _score in hits] == [matching]
    store.close()


def test_search_uses_fresh_filtered_vector_matrix(monkeypatch):
    store = Store(":memory:")
    wid = store.get_or_create_workspace("w")
    allowed_repo = store.get_or_create_repo(wid, "allowed")
    other_repo = store.get_or_create_repo(wid, "other")
    index = NumpyVectorIndex(store, dim=3)

    allowed = store.add_memory(MemoryRecord(
        id="", content="allowed", scope=Scope.REPO,
        workspace_id=wid, repo_id=allowed_repo,
        embedding=np.array([1.0, 0.0, 0.0], dtype=np.float32),
    ))
    store.add_memory(MemoryRecord(
        id="", content="other", scope=Scope.REPO,
        workspace_id=wid, repo_id=other_repo,
        embedding=np.array([1.0, 0.0, 0.0], dtype=np.float32),
    ))

    def unexpected_iter(*args, **kwargs):
        raise AssertionError("search must not hydrate vectors row by row")

    calls = []
    original_matrix = store.iter_vector_matrices

    def traced_matrix(*args, **kwargs):
        calls.append((args, kwargs))
        return original_matrix(*args, **kwargs)

    monkeypatch.setattr(store, "iter_vectors", unexpected_iter)
    monkeypatch.setattr(store, "iter_vector_matrices", traced_matrix)
    flt = SearchFilter(workspace_id=wid, repo_id=allowed_repo)
    query = np.array([1.0, 0.0, 0.0], dtype=np.float32)

    assert [mid for mid, _score in index.search(query, 3, filter=flt)] == [allowed]

    added = store.add_memory(MemoryRecord(
        id="", content="added", scope=Scope.REPO,
        workspace_id=wid, repo_id=allowed_repo,
        embedding=np.array([0.8, 0.6, 0.0], dtype=np.float32),
    ))
    assert {mid for mid, _score in index.search(query, 3, filter=flt)} == {allowed, added}
    assert len(calls) == 2
    store.close()


def test_top_k_matches_full_stable_order_at_cutoff_ties():
    ids = ["mem_z", "mem_b", "mem_c", "mem_a", "mem_tail"]
    scores = np.array([1.0, 0.75, 0.75, 0.75, 0.1], dtype=np.float32)

    expected = sorted(
        range(len(ids)), key=lambda index: (-float(scores[index]), ids[index])
    )[:3]

    assert _top_k_indices(scores, ids, 3) == expected
    assert _top_k_indices(scores, ids, 0) == []


def test_top_k_matches_full_stable_order_for_deterministic_10k_corpus():
    rng = np.random.default_rng(20260804)
    ids = [f"mem_{index:05d}" for index in range(10_000)]
    scores = rng.standard_normal(len(ids)).astype(np.float32) * 0.01
    scores[:8] = np.arange(10, 2, -1, dtype=np.float32)
    # Deliberately put three ids at the selected boundary: only the two
    # lexicographically first ones may survive at k=10.
    scores[[8, 100, 101]] = 1.0

    expected = sorted(
        range(len(ids)), key=lambda index: (-float(scores[index]), ids[index])
    )[:10]

    assert _top_k_indices(scores, ids, 10) == expected


def test_timeline_skips_legacy_dimension_without_losing_lexical_results():
    engine = MemoryEngine.create(":memory:", embed_model=None, embed_dim=384)
    wid = engine.store.get_or_create_workspace("w")
    rid = engine.store.get_or_create_repo(wid, "r")
    mid = engine.remember(
        "durable migration fact", workspace_id=wid, repo_id=rid,
        resolve_conflicts=False)
    engine.store.put_vector(
        mid,
        DeterministicEmbedder(dim=256).embed(["durable migration fact"])[0],
        model=engine.embedding_space,
    )
    engine.store.conn.commit()

    results = engine.timeline("durable migration", workspace_id=wid, repo_id=rid)

    assert [record.id for record in results] == [mid]
    engine.store.close()


def test_repair_uses_active_dimension_and_creates_backup(tmp_path):
    db_path = tmp_path / "mixed.db"
    store = Store(str(db_path))
    wid = store.get_or_create_workspace("w")
    rid = store.get_or_create_repo(wid, "r")
    mid = store.add_memory(MemoryRecord(
        id="", content="legacy vector", workspace_id=wid, repo_id=rid,
        embedding=DeterministicEmbedder(dim=256).embed(["legacy vector"])[0]))
    store.close()

    result = repair(str(db_path), model_name="", dim=384)

    assert result["repaired"] == 1
    assert result["by_dim"] == {384: 1}
    assert Path(result["backup"]).is_file()
    repaired = Store(str(db_path))
    row = repaired.conn.execute(
        "SELECT dim, model FROM mem_vectors WHERE id=?", (mid,)
    ).fetchone()
    active = repaired.conn.execute(
        "SELECT version FROM embedding_state WHERE identity='__active__'"
    ).fetchone()
    rebuilding = repaired.conn.execute(
        "SELECT 1 FROM embedding_state WHERE identity='__rebuilding__'"
    ).fetchone()
    assert row["dim"] == 384
    assert active is not None and row["model"] == active["version"]
    assert str(row["model"]).startswith("emb:v1:")
    assert rebuilding is None
    repaired.close()


def test_delete_removes_from_index():
    store = Store(":memory:")
    wid = store.get_or_create_workspace("w")
    rid = store.get_or_create_repo(wid, "r")
    emb = DeterministicEmbedder(dim=128)
    index = NumpyVectorIndex(store)
    vec = emb.embed(["hello world"])[0]
    mid = store.add_memory(MemoryRecord(id="", content="hello world", workspace_id=wid,
                                        repo_id=rid, embedding=vec))
    assert index.search(vec, k=1)[0][0] == mid
    index.delete([mid])
    assert index.search(vec, k=1) == []
    store.close()


def test_zero_vectors_are_non_searchable():
    store = Store(":memory:")
    workspace_id = store.get_or_create_workspace("zero-contract")
    index = NumpyVectorIndex(store, dim=2)
    zero_id = store.add_memory(MemoryRecord(
        id="",
        content="zero vector",
        workspace_id=workspace_id,
        embedding=np.zeros(2, dtype=np.float32),
    ))
    directed_id = store.add_memory(MemoryRecord(
        id="",
        content="directed vector",
        workspace_id=workspace_id,
        embedding=np.array([1.0, 0.0], dtype=np.float32),
    ))

    assert index.search(np.zeros(2, dtype=np.float32), k=5) == []
    assert [memory_id for memory_id, _score in index.search(
        np.array([1.0, 0.0], dtype=np.float32), k=5
    )] == [directed_id]
    assert zero_id != directed_id
    store.close()


_UNIT = np.array([1.0, 0.0, 0.0], dtype=np.float32)
_VIS_TRIGGERS = (
    "trg_vector_vis_mem_insert", "trg_vector_vis_mem_delete", "trg_vector_vis_mem_update",
    "trg_vector_vis_ws_insert", "trg_vector_vis_ws_update", "trg_vector_vis_ws_delete",
    "trg_vector_vis_closure_insert", "trg_vector_vis_closure_update", "trg_vector_vis_closure_delete",
)


def _drop_visibility_schema(store) -> None:
    for name in _VIS_TRIGGERS:
        store.conn.execute(f"DROP TRIGGER IF EXISTS {name}")
    store.conn.execute("DROP TABLE IF EXISTS vector_visibility_state")


@contextmanager
def _seeded(workspace: str = "w", **index_kwargs):
    store = Store(":memory:")
    try:
        yield store, store.get_or_create_workspace(workspace), NumpyVectorIndex(store, dim=3, **index_kwargs)
    finally:
        store.close()


def _indexed_memory(store, workspace_id, vector=_UNIT, **fields) -> str:
    return store.add_memory(MemoryRecord(
        id="", content=fields.pop("content", "visibility fact"),
        workspace_id=workspace_id, scope=Scope.WORKSPACE, embedding=vector.copy(), **fields))


def _flt(workspace_id, **kwargs) -> SearchFilter:
    return SearchFilter(workspace_id=workspace_id, **kwargs)


def _ids(index, flt=None) -> list[str]:
    return [mid for mid, _score in index.search(_UNIT, 5, filter=flt)]


def _traced_matrix_stream(store, monkeypatch) -> list[int]:
    calls, original = [], store.iter_vector_matrices
    monkeypatch.setattr(store, "iter_vector_matrices", lambda *a, **kw: (calls.append(1), original(*a, **kw))[1])
    return calls


def test_cached_search_honors_close_validity():
    with _seeded("visibility-close") as (store, wid, index):
        mid = _indexed_memory(store, wid)
        assert _ids(index, _flt(wid)) == [mid]
        store.close_validity(mid, actor="test")
        assert _ids(index, _flt(wid)) == []


def test_cached_search_honors_memory_type_update():
    with _seeded("visibility-type") as (store, wid, index):
        mid = _indexed_memory(store, wid, mtype=MemoryType.SEMANTIC)
        semantic, episodic = _flt(wid, mtypes=[MemoryType.SEMANTIC]), _flt(wid, mtypes=[MemoryType.EPISODIC])
        assert _ids(index, semantic) == [mid]
        with store.write_transaction():
            store.conn.execute("UPDATE memories SET mtype=? WHERE id=?", (MemoryType.EPISODIC.value, mid))
        assert (_ids(index, semantic), _ids(index, episodic)) == ([], [mid])


def test_cached_search_honors_workspace_reassignment():
    with _seeded("visibility-source") as (store, source, index):
        target = store.get_or_create_workspace("visibility-target")
        mid = _indexed_memory(store, source)
        assert (_ids(index, _flt(source)), _ids(index, _flt(target))) == ([mid], [])
        plan = relocation.prepare_move(store, source, target, [mid])
        assert plan.blockers == []
        with store.write_transaction():
            store.apply_memory_move(plan, actor="test")
        assert (_ids(index, _flt(source)), _ids(index, _flt(target))) == ([], [mid])


def test_cached_search_honors_a_new_closure_observation():
    with _seeded("visibility-closure") as (store, wid, index):
        mid = _indexed_memory(store, wid)
        assert _ids(index, _flt(wid)) == [mid]
        with store.write_transaction():
            store.conn.execute("INSERT INTO memory_closures(memory_id,valid_to,recorded_at) VALUES (?,?,?)", (mid, 10.0, 20.0))
        assert _ids(index, _flt(wid)) == []


@pytest.mark.parametrize("fields,visible_before,visible_after", [
    ({"valid_from": 10.0, "ingested_at": 0.0}, False, True),
    ({"valid_from": -100.0, "valid_to": 10.0, "valid_to_recorded_at": -100.0, "ingested_at": -100.0}, True, False),
    ({"valid_from": -100.0, "valid_to": -50.0, "valid_to_recorded_at": 10.0, "ingested_at": -100.0}, True, False),
], ids=["future-valid-from", "future-valid-to", "future-valid-to-recorded-world"])
def test_cached_search_crosses_a_future_temporal_boundary(monkeypatch, fields, visible_before, visible_after):
    base, clock = 1_700_000_000.0, {"now": 1_700_000_000.0}
    monkeypatch.setattr(core_store, "now_ts", lambda: clock["now"])
    with _seeded("visibility-future") as (store, wid, index):
        mid = _indexed_memory(store, wid, **{key: base + value for key, value in fields.items()})
        assert _ids(index, _flt(wid)) == ([mid] if visible_before else [])
        assert index._cache_valid_until == base + 10.0
        clock["now"] = base + 20.0
        assert _ids(index, _flt(wid)) == ([mid] if visible_after else [])


def test_reinforcement_keeps_the_cached_matrix_warm(monkeypatch):
    with _seeded("visibility-reinforce") as (store, wid, index):
        mid = _indexed_memory(store, wid)
        assert _ids(index, _flt(wid)) == [mid]
        calls = _traced_matrix_stream(store, monkeypatch)
        store.reinforce(mid, boost=0.5)
        assert (_ids(index, _flt(wid)), calls) == ([mid], [])


@pytest.mark.parametrize("budget", [1, 0], ids=["tiny-budget", "disabled"])
def test_cache_budget_bounds_or_disables_retention(monkeypatch, budget):
    with _seeded("visibility-budget", max_cache_bytes=4096) as (store, wid, index):
        mid = _indexed_memory(store, wid)
        assert _ids(index, _flt(wid)) == [mid] and index._cache_key is not None
        target = store.get_or_create_workspace("other") if budget else wid
        tmid = _indexed_memory(store, target) if budget else mid
        index.max_cache_bytes = budget
        calls = _traced_matrix_stream(store, monkeypatch)
        assert [_ids(index, _flt(target)) for _ in range(2)] == [[tmid]] * 2
        assert (len(calls), index._cache_key, index._cache_batches) == (2, None, ())


class _StreamingStore:
    def __init__(self, batches, generation=1):
        self.batches, self.generation, self.calls = batches, generation, 0

    def iter_vector_matrices(self, flt=None, *, dim):
        self.calls += 1
        return list(self.batches)


class _GenerationStore(_StreamingStore):
    def vector_generation(self) -> int:
        return self.generation


def test_search_double_falls_back_to_streaming_or_the_generation_hook():
    batch = [(["mem_1"], np.array([[1.0, 0.0, 0.0]], dtype=np.float32))]
    plain = NumpyVectorIndex(_StreamingStore(batch), dim=3)
    assert [plain.search(_UNIT, 2) for _ in range(2)] == [[("mem_1", 1.0)]] * 2
    assert (plain._cache_key, plain.store.calls) == (None, 2)
    legacy = NumpyVectorIndex(_GenerationStore(batch), dim=3)
    assert [legacy.search(_UNIT, 2) for _ in range(2)] == [[("mem_1", 1.0)]] * 2
    assert legacy.store.calls == 1
    legacy.store.generation += 1
    assert (legacy.search(_UNIT, 2), legacy.store.calls) == ([("mem_1", 1.0)], 2)


def test_visibility_transition_skips_absent_closure_storage(monkeypatch):
    base = 1_700_000_000.0
    monkeypatch.setattr(core_store, "now_ts", lambda: base)
    with _seeded("legacy-boundary") as (store, wid, _index):
        mid = _indexed_memory(store, wid, valid_from=base - 100.0, valid_to=base + 10.0,
                              valid_to_recorded_at=base - 100.0, ingested_at=base - 100.0)
        with store.write_transaction():
            store.conn.execute("INSERT INTO memory_closures(memory_id,valid_to,recorded_at) VALUES (?,?,?)", (mid, base + 5.0, base - 100.0))
        assert store.next_vector_visibility_transition(_flt(wid), base, base) == base + 5.0
        with store.write_transaction():
            store.conn.execute("DROP TABLE memory_closures")
        assert store.next_vector_visibility_transition(_flt(wid), base, base) == base + 10.0


def test_read_only_snapshot_and_backup_preserving_visibility_repair(tmp_path):
    ro_db, repair_db = tmp_path / "legacy-v19.db", tmp_path / "migrated-v19.db"
    for db, name in ((ro_db, "legacy-ro"), (repair_db, "repair-vis")):
        with Store(str(db)) as writable:
            wid = writable.get_or_create_workspace(name)
            mid = _indexed_memory(writable, wid)
            with writable.write_transaction():
                _drop_visibility_schema(writable)
            writable.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        if db == ro_db:
            with Store(str(db), read_only=True) as read_only:
                assert read_only.vector_visibility_token() == (read_only.vector_generation(), 0)
                assert [_ids(NumpyVectorIndex(read_only, dim=3), _flt(wid))] * 2 == [[mid]] * 2
        else:
            backup = Path(f"{db}.pre-migration-v19.bak")
            backup.write_bytes(b"an earlier snapshot")
            with Store(str(db)) as repaired:
                triggers = {str(r[0]) for r in repaired.conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
                assert set(_VIS_TRIGGERS) <= triggers and repaired._has_table(repaired.conn, "vector_visibility_state")
                assert _ids(NumpyVectorIndex(repaired, dim=3), _flt(wid)) == [mid]
                before = repaired.vector_visibility_token()[1]
                repaired.reinforce(mid, boost=0.5)
                assert repaired.vector_visibility_token()[1] == before
                repaired.close_validity(mid, actor="test")
                assert repaired.vector_visibility_token()[1] > before
            assert backup.read_bytes() == b"an earlier snapshot"


def test_raw_connection_replacement_invalidates_the_matrix_cache(tmp_path):
    with Store(str(tmp_path / "swap.db")) as store, Store(str(tmp_path / "swap-other.db")) as other:
        first_mid = _indexed_memory(store, store.get_or_create_workspace("swap"))
        other_mid = _indexed_memory(other, other.get_or_create_workspace("swap"))
        assert other.vector_visibility_token() == store.vector_visibility_token()
        index = NumpyVectorIndex(store, dim=3)
        assert _ids(index) == [first_mid]
        warm, epoch_before = index._cache_key, store._connection_epoch
        store.path = str(tmp_path / "swap-other.db")
        store.reconnect()
        assert store._connection_epoch == epoch_before + 1 and other_mid != first_mid
        assert _ids(index) == [other_mid] and index._cache_key != warm


def test_uncommitted_transaction_and_narrowing_allowed_workspaces():
    with _seeded("tx-rollback") as (store, wid, index):
        mid = _indexed_memory(store, wid)
        hidden_mid = _indexed_memory(store, store.get_or_create_workspace("hidden"))
        assert _ids(index, _flt(wid)) == [mid]
        warm = index._cache_key
        store.conn.execute("BEGIN IMMEDIATE")
        try:
            uncommitted = _indexed_memory(store, wid)
            assert set(_ids(index, _flt(wid))) == {mid, uncommitted} and index._cache_key == warm
        finally:
            store.conn.rollback()
        assert _ids(index, _flt(wid)) == [mid]
        assert set(_ids(index)) == {mid, hidden_mid}
        store.allowed_workspaces = frozenset({"tx-rollback"})
        assert _ids(index) == [mid]


def test_mid_batch_failure_after_an_in_transaction_search_leaves_no_dead_vectors():
    engine = MemoryEngine.create(":memory:", vector_backend="numpy")
    try:
        wid = engine.store.get_or_create_workspace("batch-rollback")
        index = engine.index
        assert isinstance(index, NumpyVectorIndex)
        engine.remember("surviving batch fact", workspace_id=wid, resolve_conflicts=False)
        query = engine.embedder.embed(["surviving batch fact"])[0]
        assert len(index.search(query, 5)) == 1
        index._invalidate_cache()
        original, calls = engine._resolve_and_store, []

        def failing_resolve(*args, **kwargs):
            result = original(*args, **kwargs)
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("mid-batch failure")
            return result

        engine._resolve_and_store = failing_resolve  # type: ignore[method-assign]
        try:
            with pytest.raises(RuntimeError, match="mid-batch failure"):
                engine.remember_many(["first rolled back fact", "second rolled back fact"], workspace_id=wid)
        finally:
            engine._resolve_and_store = original  # type: ignore[method-assign]
        assert index._cache_key is None and len(index.search(query, 5)) == 1
        assert [engine.store.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("memories", "mem_vectors")] == [1, 1]
    finally:
        engine.store.close()


class _MmapConnector:
    def __init__(self, supports_mmap_pragma) -> None:
        if supports_mmap_pragma is not None:
            self.supports_mmap_pragma = supports_mmap_pragma

    def __call__(self, path):
        conn = sqlite3.connect(path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn


@pytest.mark.parametrize("cap,env_val,want_enabled", [(True, "0", True), (None, "1", True), (False, "1", False), (None, "0", False)])
def test_configure_connection_honors_the_connector_mmap_capability(tmp_path, monkeypatch, cap, env_val, want_enabled):
    monkeypatch.setenv("ENGRAPHIS_SQLITE_MMAP", env_val)
    with sqlite3.connect(str(tmp_path / "probe.db")) as probe:
        host_supports_mmap = (probe.execute("PRAGMA mmap_size=67108864").fetchone() or [0])[0] > 0
    with Store(str(tmp_path / "mmap.db"), connect=_MmapConnector(cap)) as store:
        mmap_size = store.conn.execute("PRAGMA mmap_size").fetchone()[0] or 0
        assert mmap_size == (67108864 if (want_enabled and host_supports_mmap) else 0)
        assert store.conn.execute("PRAGMA cache_size").fetchone()[0] == -16384


def test_filter_key_covers_every_where_filter_field():
    assert {f.name for f in fields(SearchFilter)} - {"as_of", "graph_layers"} == {
        "workspace_id", "repo_id", "session_id", "scopes", "mtypes",
        "include_ancestors", "valid_at", "known_at", "modified_since",
    }
    assert _filter_key(None, SearchFilter(repo_id="r1")) != _filter_key(None, SearchFilter(repo_id="r2"))


@pytest.mark.parametrize("raw,expected", [(None, 64 * 1024 * 1024), ("0", 0), ("1024", 1024), ("invalid_bytes", 64 * 1024 * 1024), ("-1", 64 * 1024 * 1024)])
def test_env_and_explicit_cache_budget_validation(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("ENGRAPHIS_VECTOR_CACHE_BYTES", raising=False)
    else:
        monkeypatch.setenv("ENGRAPHIS_VECTOR_CACHE_BYTES", raw)
    with Store(":memory:") as store:
        assert NumpyVectorIndex(store).max_cache_bytes == expected
        for invalid in (-1, 1.5, True):
            with pytest.raises(ValueError, match="non-negative integer"):
                NumpyVectorIndex(store, max_cache_bytes=invalid)


def test_sqlitevec_l2_distance_converts_to_cosine_similarity():
    assert _cosine_from_l2(0.0) == 1.0
    assert abs(_cosine_from_l2(2 ** 0.5)) < 1e-12
    assert _cosine_from_l2(2.0) == -1.0
