"""The real sqlite-vec native KNN backend: widening, resolution, and concurrency.

The 0.9.7 batch changed the KNN query from ``LIMIT ?`` to vec0's ``k = ?`` constraint
(SQLite < 3.41 never passes LIMIT to xBestIndex, and the resolve path SWALLOWS the
resulting error — silently degrading every near-duplicate write to ADD) and capped the
filtered-search geometric widening with a single full scan. Neither path had CI
coverage because sqlite-vec wasn't a test dependency; now it is.
"""
import threading
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest

from engraphis.backends import DeterministicEmbedder
from engraphis.backends.vector_sqlitevec import SqliteVecVectorIndex
from engraphis.core.engine import MemoryEngine
from engraphis.core.interfaces import MemoryRecord, Scope, SearchFilter
from engraphis.core.store import Store


pytestmark = pytest.mark.native_sqlitevec

pytest.importorskip("sqlite_vec", reason="sqlite-vec extra not installed")

DIM = 64


def _make(store, index, emb, wid, rid, text):
    """Insert a memory and its native vector-index row — unlike the store-backed NumPy index, the
    sqlite-vec backend only sees vectors explicitly upserted (as the engine does)."""
    vec = emb.embed([text])[0]
    mid = store.add_memory(MemoryRecord(id="", content=text, scope=Scope.REPO,
                                        workspace_id=wid, repo_id=rid, embedding=vec))
    index.upsert([mid], vec.reshape(1, -1))
    return mid


def _fixture():
    store = Store(":memory:")
    wid = store.get_or_create_workspace("w")
    rid = store.get_or_create_repo(wid, "r")
    emb = DeterministicEmbedder(dim=DIM)
    index = SqliteVecVectorIndex(store, DIM)
    return store, wid, rid, emb, index


def test_knn_search_returns_ranked_hits():
    """Basic ``k = ?`` KNN — on SQLite < 3.41 the old LIMIT form raised instead."""
    store, wid, rid, emb, index = _fixture()
    pm = _make(store, index, emb, wid, rid,
               "We standardized on pnpm as the package manager for all frontend repos.")
    _make(store, index, emb, wid, rid, "The afternoon sky over the harbor was pale blue.")
    ids = [i for i, _ in index.search(emb.embed(["which package manager do we use?"])[0], k=2)]
    assert ids and ids[0] == pm
    store.close()


def test_k_larger_than_index_is_capped_not_an_error():
    store, wid, rid, emb, index = _fixture()
    only = _make(store, index, emb, wid, rid, "single resident vector")
    hits = index.search(emb.embed(["single resident vector"])[0], k=50)
    assert [i for i, _ in hits] == [only]
    store.close()


def test_equal_distance_boundary_uses_memory_id_as_stable_secondary_order():
    store, wid, rid, emb, index = _fixture()
    vector = emb.embed(["identical vector for deterministic tie ordering"])[0]
    ids = ["mem_tie_z", "mem_tie_a", "mem_tie_m"]
    for memory_id in ids:
        store.add_memory(MemoryRecord(
            id=memory_id,
            content="identical vector for deterministic tie ordering",
            workspace_id=wid,
            repo_id=rid,
            scope=Scope.REPO,
            embedding=vector,
        ))
    index.upsert(ids, np.vstack([vector, vector, vector]))

    hits = index.search(vector, k=2)

    assert [memory_id for memory_id, _ in hits] == ["mem_tie_a", "mem_tie_m"]
    store.close()


def test_native_upsert_failure_rolls_back_the_whole_owned_batch(monkeypatch):
    store, wid, rid, emb, index = _fixture()
    vector = emb.embed(["atomic native batch"])[0]
    ids = [
        store.add_memory(MemoryRecord(
            id="", content=f"native {position}", workspace_id=wid, repo_id=rid,
            scope=Scope.REPO, embedding=vector,
        ))
        for position in range(2)
    ]
    connection_type = type(store.conn)
    original_execute = connection_type.execute
    inserts = 0

    def fail_second_insert(connection, statement, *args, **kwargs):
        nonlocal inserts
        if "INSERT INTO mem_vec_ann" in str(statement):
            inserts += 1
            if inserts == 2:
                raise RuntimeError("native index unavailable")
        return original_execute(connection, statement, *args, **kwargs)

    monkeypatch.setattr(connection_type, "execute", fail_second_insert)

    with pytest.raises(RuntimeError, match="native index unavailable"):
        index.upsert(ids, np.vstack([vector, vector]))

    assert store.conn.in_transaction is False
    assert store.conn.execute(
        "SELECT COUNT(*) FROM mem_vec_ann WHERE id IN (?, ?)", ids,
    ).fetchone()[0] == 0
    store.close()


def test_native_upsert_replaces_existing_rows_after_reopen():
    """Persistent vec0 rows must be safely rehydrated on the next process start."""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as temp:
        db_path = str(Path(temp) / "restart.db")
        first = MemoryEngine.create(
            db_path, embed_dim=DIM, vector_backend="sqlite-vec", auto_evolve=False
        )
        workspace_id = first.store.get_or_create_workspace("restart")
        first.remember("A persisted restart marker.", workspace_id=workspace_id)
        first.store.close()

        second = MemoryEngine.create(
            db_path, embed_dim=DIM, vector_backend="sqlite-vec", auto_evolve=False
        )
        assert second.store.conn.execute(
            "SELECT COUNT(*) FROM mem_vec_ann"
        ).fetchone()[0] == 1
        second.store.close()


def test_native_upsert_does_not_commit_a_caller_owned_transaction():
    store, wid, rid, emb, index = _fixture()
    vector = emb.embed(["caller-owned native batch"])[0]
    memory_id = store.add_memory(MemoryRecord(
        id="", content="caller-owned native vector", workspace_id=wid, repo_id=rid,
        scope=Scope.REPO, embedding=vector,
    ))
    store.conn.execute("BEGIN IMMEDIATE")

    index.upsert([memory_id], vector.reshape(1, -1))

    assert store.conn.in_transaction is True
    assert store.conn.transaction_owned_by_current_thread() is True
    store.conn.rollback()
    assert store.conn.execute(
        "SELECT 1 FROM mem_vec_ann WHERE id=?", (memory_id,)
    ).fetchone() is None
    store.close()


def test_filtered_search_widens_past_invisible_rows_to_full_scan():
    """A workspace dense with rows the filter hides forces the widening loop all the
    way to its full-scan cap — the k visible hits must still all be found."""
    store, wid, rid, emb, index = _fixture()
    other_wid = store.get_or_create_workspace("other")
    other_rid = store.get_or_create_repo(other_wid, "r2")
    # 40 invisible (other workspace) rows crowd the exact-KNN neighborhood…
    for i in range(40):
        _make(store, index, emb, other_wid, other_rid, f"decoy fact number {i} about deploys")
    # …and 3 visible rows sit behind them.
    visible = {_make(store, index, emb, wid, rid, f"visible fact {i} about deploys")
               for i in range(3)}
    flt = SearchFilter(workspace_id=wid)
    hits = index.search(emb.embed(["facts about deploys"])[0], k=3, filter=flt)
    assert {i for i, _ in hits} == visible
    store.close()


def test_filtered_search_batches_visibility_lookups(monkeypatch):
    store, wid, rid, emb, index = _fixture()
    for i in range(8):
        _make(store, index, emb, wid, rid, f"visible batch fact {i}")
    calls = 0
    original = store.get_memories

    def batched(memory_ids):
        nonlocal calls
        calls += 1
        return original(memory_ids)

    monkeypatch.setattr(store, "get_memories", batched)
    monkeypatch.setattr(
        store,
        "get_memory",
        lambda _memory_id: (_ for _ in ()).throw(AssertionError("N+1 lookup")),
    )

    hits = index.search(
        emb.embed(["visible batch fact"])[0],
        k=5,
        filter=SearchFilter(workspace_id=wid),
    )

    assert len(hits) == 5
    # One query is typical; an equal-distance kth boundary may require one
    # deterministic tie-expansion query. Either way visibility remains batched,
    # never an N+1 get_memory loop.
    assert 1 <= calls <= 2
    store.close()


def test_empty_index_returns_empty():
    store, _, _, emb, index = _fixture()
    assert index.search(emb.embed(["anything"])[0], k=5) == []
    store.close()


def test_engine_resolution_preserves_historical_sqlitevec_rows_and_filters_them():
    """INVALIDATE retains the historical index row without leaking it into live recall."""
    eng = MemoryEngine.create(
        ":memory:", embed_dim=DIM, vector_backend="sqlite-vec", auto_evolve=False
    )
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    old_content = "Until 2026-01 the rate limit was 100 requests per minute per API key."
    new_content = (
        "As of 2026-02 the rate limit was raised to 500 requests per minute per API key."
    )
    old = eng.remember_with_resolution(
        old_content,
        workspace_id=wid,
        repo_id=rid,
        valid_from=100.0,
    )
    duplicate = eng.remember_with_resolution(
        old_content,
        workspace_id=wid,
        repo_id=rid,
        valid_from=100.0,
    )
    new = eng.remember_with_resolution(
        new_content,
        workspace_id=wid,
        repo_id=rid,
        valid_from=200.0,
    )
    assert (old["op"], duplicate["op"], new["op"]) == ("add", "noop", "invalidate")
    assert duplicate["id"] == old["id"] and new["superseded"] == [old["id"]]
    indexed = {
        row["id"] for row in eng.store.conn.execute("SELECT id FROM mem_vec_ann").fetchall()
    }
    assert indexed == {old["id"], new["id"]}

    query_vector = eng.embedder.embed(["What is the API rate limit?"])[0]
    historical = {
        memory_id for memory_id, _ in eng.index.search(
            query_vector,
            k=10,
            filter=SearchFilter(
                workspace_id=wid,
                repo_id=rid,
                valid_at=150.0,
            ),
        )
    }
    current = {
        memory_id for memory_id, _ in eng.index.search(
            query_vector,
            k=10,
            filter=SearchFilter(
                workspace_id=wid,
                repo_id=rid,
                valid_at=250.0,
            ),
        )
    }
    assert historical == {old["id"]}
    assert current == {new["id"]}
    eng.store.close()


def test_concurrent_identical_writes_use_one_real_sqlitevec_row():
    """Regression for resolve/read/write atomicity with the production native backend."""
    threads = 8
    eng = MemoryEngine.create(
        ":memory:", embed_dim=DIM, vector_backend="sqlite-vec", auto_evolve=False
    )
    wid = eng.store.get_or_create_workspace("w")
    barrier = threading.Barrier(threads)

    def write(_):
        barrier.wait()
        return eng.remember_with_resolution(
            "The deploy pipeline uses GitHub Actions and pushes to AWS ECS.",
            workspace_id=wid,
            title="deploy",
        )

    with ThreadPoolExecutor(max_workers=threads) as pool:
        results = list(pool.map(write, range(threads)))
    assert [r["op"] for r in results].count("add") == 1
    assert [r["op"] for r in results].count("noop") == threads - 1
    assert eng.store.conn.execute("SELECT COUNT(*) FROM mem_vec_ann").fetchone()[0] == 1
    assert len(eng.store.list_memories(SearchFilter(workspace_id=wid), limit=50)) == 1
    eng.store.close()
