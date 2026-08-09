from pathlib import Path

import numpy as np
import pytest

from engraphis.backends import DeterministicEmbedder, NumpyVectorIndex
from engraphis.backends.vector_numpy import _top_k_indices
from engraphis.backends.vector_sqlitevec import _cosine_from_l2
from engraphis.core.engine import MemoryEngine
from engraphis.core.interfaces import (
    MemoryRecord,
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
    original_matrix = store.vector_matrix

    def traced_matrix(*args, **kwargs):
        calls.append((args, kwargs))
        return original_matrix(*args, **kwargs)

    monkeypatch.setattr(store, "iter_vectors", unexpected_iter)
    monkeypatch.setattr(store, "vector_matrix", traced_matrix)
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


def test_sqlitevec_l2_distance_converts_to_cosine_similarity():
    assert _cosine_from_l2(0.0) == 1.0
    assert abs(_cosine_from_l2(2 ** 0.5)) < 1e-12
    assert _cosine_from_l2(2.0) == -1.0
