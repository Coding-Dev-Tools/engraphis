"""The real sqlite-vec ANN backend: KNN, widening, resolution, and concurrency.

The 0.9.7 batch changed the KNN query from ``LIMIT ?`` to vec0's ``k = ?`` constraint
(SQLite < 3.41 never passes LIMIT to xBestIndex, and the resolve path SWALLOWS the
resulting error — silently degrading every near-duplicate write to ADD) and capped the
filtered-search geometric widening with a single full scan. Neither path had CI
coverage because sqlite-vec wasn't a test dependency; now it is.
"""
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

pytest.importorskip("sqlite_vec", reason="sqlite-vec extra not installed")

from engraphis.backends import DeterministicEmbedder
from engraphis.backends.vector_sqlitevec import SqliteVecVectorIndex
from engraphis.core.engine import MemoryEngine
from engraphis.core.interfaces import MemoryRecord, Scope, SearchFilter
from engraphis.core.store import Store

DIM = 64


def _make(store, index, emb, wid, rid, text):
    """Insert a memory AND its ANN row — unlike the store-backed numpy index, the
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


def test_filtered_search_widens_past_invisible_rows_to_full_scan():
    """A workspace dense with rows the filter hides forces the widening loop all the
    way to its full-scan cap — the k visible hits must still all be found."""
    store, wid, rid, emb, index = _fixture()
    other_wid = store.get_or_create_workspace("other")
    other_rid = store.get_or_create_repo(other_wid, "r2")
    # 40 invisible (other workspace) rows crowd the ANN neighborhood…
    for i in range(40):
        _make(store, index, emb, other_wid, other_rid, f"decoy fact number {i} about deploys")
    # …and 3 visible rows sit behind them.
    visible = {_make(store, index, emb, wid, rid, f"visible fact {i} about deploys")
               for i in range(3)}
    flt = SearchFilter(workspace_id=wid)
    hits = index.search(emb.embed(["facts about deploys"])[0], k=3, filter=flt)
    assert {i for i, _ in hits} == visible
    store.close()


def test_empty_index_returns_empty():
    store, _, _, emb, index = _fixture()
    assert index.search(emb.embed(["anything"])[0], k=5) == []
    store.close()


def test_engine_resolution_writes_and_deletes_real_sqlitevec_rows():
    """The engine's ADD/NOOP/INVALIDATE path must exercise the native index, not merely
    direct index calls: an old superseded vector must be removed and the new one found."""
    eng = MemoryEngine.create(
        ":memory:", embed_dim=DIM, vector_backend="sqlite-vec", auto_evolve=False
    )
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    old = eng.remember_with_resolution(
        "Until 2026-01 the rate limit was 100 requests per minute per API key.",
        workspace_id=wid,
        repo_id=rid,
    )
    duplicate = eng.remember_with_resolution(
        "Until 2026-01 the rate limit was 100 requests per minute per API key.",
        workspace_id=wid,
        repo_id=rid,
    )
    new = eng.remember_with_resolution(
        "As of 2026-02 the rate limit was raised to 500 requests per minute per API key.",
        workspace_id=wid,
        repo_id=rid,
    )
    assert (old["op"], duplicate["op"], new["op"]) == ("add", "noop", "invalidate")
    assert duplicate["id"] == old["id"] and new["superseded"] == [old["id"]]
    indexed = {
        row["id"] for row in eng.store.conn.execute("SELECT id FROM mem_vec_ann").fetchall()
    }
    assert indexed == {new["id"]}
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
