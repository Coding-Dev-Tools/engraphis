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

from engraphis.backends import DeterministicEmbedder, NumpyVectorIndex
from engraphis.backends.vector_sqlitevec import (
    SqliteVecVectorIndex,
    get_vector_index,
)
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


def test_zero_vector_score_contract_matches_numpy():
    store = Store(":memory:")
    workspace_id = store.get_or_create_workspace("zero-contract")
    native = SqliteVecVectorIndex(store, 2)
    numpy_index = NumpyVectorIndex(store, dim=2)
    rows = {
        "mem_positive": np.array([1.0, 0.0], dtype=np.float32),
        "mem_orthogonal": np.array([0.0, 1.0], dtype=np.float32),
        "mem_negative": np.array([-1.0, 0.0], dtype=np.float32),
        "mem_zero": np.zeros(2, dtype=np.float32),
    }
    for memory_id, vector in rows.items():
        store.add_memory(MemoryRecord(
            id=memory_id,
            content=memory_id,
            workspace_id=workspace_id,
            scope=Scope.WORKSPACE,
            embedding=vector,
        ))
    native.upsert(list(rows), np.vstack(list(rows.values())))

    query = np.array([1.0, 0.0], dtype=np.float32)
    numpy_hits = numpy_index.search(query, k=10)
    native_hits = native.search(query, k=10)

    assert [memory_id for memory_id, _score in native_hits] == [
        memory_id for memory_id, _score in numpy_hits
    ]
    np.testing.assert_allclose(
        [score for _memory_id, score in native_hits],
        [score for _memory_id, score in numpy_hits],
        atol=1e-6,
    )
    assert native.search(np.zeros(2, dtype=np.float32), k=10) == []
    assert numpy_index.search(np.zeros(2, dtype=np.float32), k=10) == []
    assert store.conn.execute(
        "SELECT 1 FROM mem_vec_ann WHERE id='mem_zero'"
    ).fetchone() is None
    store.close()


def test_native_index_recreates_disposable_state_for_new_dimension(tmp_path):
    db_path = tmp_path / "dimension-change.db"
    first_store = Store(str(db_path))
    workspace_id = first_store.get_or_create_workspace("dimension-change")
    vector = np.zeros(DIM, dtype=np.float32)
    vector[0] = 1.0
    memory_id = first_store.add_memory(MemoryRecord(
        id="",
        content="old vector dimension",
        workspace_id=workspace_id,
        scope=Scope.WORKSPACE,
        embedding=vector,
    ))
    first_index = SqliteVecVectorIndex(first_store, DIM)
    first_index.upsert([memory_id], vector.reshape(1, -1))
    first_index.mark_rebuild_complete()
    first_store.close()

    second_store = Store(str(db_path))
    second_index = SqliteVecVectorIndex(second_store, 32)

    sql = second_store.conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='mem_vec_ann'"
    ).fetchone()["sql"]
    state = second_store.conn.execute(
        "SELECT format_version, dimension FROM mem_vec_ann_state WHERE singleton=1"
    ).fetchone()
    assert "FLOAT[32]" in sql
    assert (state["format_version"], state["dimension"]) == (0, 32)
    assert second_store.conn.execute(
        "SELECT COUNT(*) FROM mem_vec_ann"
    ).fetchone()[0] == 0
    assert second_index.requires_rebuild is True
    second_index.mark_rebuild_complete()
    second_store.close()

    third_store = Store(str(db_path))
    assert SqliteVecVectorIndex(third_store, 32).requires_rebuild is False
    third_store.close()


def test_read_only_native_index_opens_current_state_without_writes(tmp_path):
    db_path = tmp_path / "read-only-current.db"
    writable = Store(str(db_path))
    workspace_id = writable.get_or_create_workspace("read-only-current")
    vector = np.zeros(DIM, dtype=np.float32)
    vector[0] = 1.0
    memory_id = writable.add_memory(MemoryRecord(
        id="",
        content="read-only native vector",
        workspace_id=workspace_id,
        scope=Scope.WORKSPACE,
        embedding=vector,
    ))
    index = SqliteVecVectorIndex(writable, DIM)
    index.upsert([memory_id], vector.reshape(1, -1))
    index.mark_rebuild_complete()
    writable.close()

    read_only = Store(str(db_path), read_only=True)
    read_only_index = SqliteVecVectorIndex(read_only, DIM)

    assert read_only_index.requires_rebuild is False
    assert read_only_index.search(vector, k=1) == [(memory_id, 1.0)]
    read_only.close()


def test_read_only_engine_reuses_current_native_index(tmp_path):
    db_path = tmp_path / "read-only-engine.db"
    writable = MemoryEngine.create(
        str(db_path),
        embed_dim=DIM,
        vector_backend="sqlite-vec",
        auto_evolve=False,
    )
    workspace_id = writable.store.get_or_create_workspace("read-only-engine")
    memory_id = writable.remember(
        "native read-only recall remains available",
        workspace_id=workspace_id,
    )
    writable.store.close()

    read_only = MemoryEngine.create(
        str(db_path),
        embed_dim=DIM,
        vector_backend="sqlite-vec",
        auto_evolve=False,
        read_only=True,
    )
    query = read_only.embedder.embed(["native read-only recall remains available"])[0]
    hits = read_only.index.search(
        query,
        k=1,
        filter=SearchFilter(workspace_id=workspace_id),
    )

    assert [hit_id for hit_id, _score in hits] == [memory_id]
    read_only.store.close()


def test_mark_rebuild_complete_requires_exact_canonical_coverage(tmp_path):
    store = Store(str(tmp_path / "incomplete-native-coverage.db"))
    workspace_id = store.get_or_create_workspace("incomplete-native-coverage")
    vectors = np.zeros((2, DIM), dtype=np.float32)
    vectors[0, 0] = 1.0
    vectors[1, 1] = 1.0
    memory_ids = [
        store.add_memory(MemoryRecord(
            id="",
            content=f"canonical row {index}",
            workspace_id=workspace_id,
            scope=Scope.WORKSPACE,
            embedding=vector,
        ))
        for index, vector in enumerate(vectors)
    ]
    native = SqliteVecVectorIndex(store, DIM)
    native.upsert(memory_ids[:1], vectors[:1])

    with pytest.raises(RuntimeError, match="native mirror coverage differs"):
        native.mark_rebuild_complete()
    state = store.conn.execute(
        "SELECT format_version FROM mem_vec_ann_state WHERE singleton=1"
    ).fetchone()
    assert state["format_version"] == 0

    native.upsert(memory_ids[1:], vectors[1:])
    native.mark_rebuild_complete()
    assert SqliteVecVectorIndex(store, DIM).requires_rebuild is False
    store.close()


def test_native_coverage_tolerates_roundoff_but_rejects_same_id_change(tmp_path):
    db_path = tmp_path / "native-vector-content.db"
    store = Store(str(db_path))
    workspace_id = store.get_or_create_workspace("native-vector-content")
    vector = np.linspace(-7.25, 11.5, DIM, dtype=np.float32)
    memory_id = store.add_memory(MemoryRecord(
        id="",
        content="unnormalized vector",
        workspace_id=workspace_id,
        scope=Scope.WORKSPACE,
        embedding=vector,
    ))
    native = SqliteVecVectorIndex(store, DIM)
    native.upsert([memory_id], vector.reshape(1, -1))
    canonical_blob = store.conn.execute(
        "SELECT vector FROM mem_vectors WHERE id=?", (memory_id,),
    ).fetchone()["vector"]
    native_blob = store.conn.execute(
        "SELECT embedding FROM mem_vec_ann WHERE id=?", (memory_id,),
    ).fetchone()["embedding"]
    assert canonical_blob != native_blob  # independent normalization differs by one ULP
    native.mark_rebuild_complete()
    store.close()

    unchanged = Store(str(db_path))
    assert SqliteVecVectorIndex(unchanged, DIM).requires_rebuild is False
    changed = np.zeros(DIM, dtype=np.float32)
    changed[-1] = 1.0
    unchanged.put_vector(memory_id, changed)
    unchanged.conn.commit()
    unchanged.close()

    read_only = Store(str(db_path), read_only=True)
    with pytest.raises(
        RuntimeError, match="read-only sqlite-vec index is unavailable or stale",
    ):
        SqliteVecVectorIndex(read_only, DIM)
    read_only.close()


def test_read_only_native_index_rejects_numpy_write_after_publication(tmp_path):
    db_path = tmp_path / "native-then-numpy.db"
    native = MemoryEngine.create(
        str(db_path), embed_dim=DIM, vector_backend="sqlite-vec", auto_evolve=False,
    )
    workspace_id = native.store.get_or_create_workspace("native-then-numpy")
    native.remember(
        "the original native vector",
        workspace_id=workspace_id,
        scope=Scope.WORKSPACE,
    )
    native.store.close()

    portable = MemoryEngine.create(
        str(db_path), embed_dim=DIM, vector_backend="numpy", auto_evolve=False,
    )
    late_id = portable.remember(
        "the canonical row written through numpy",
        workspace_id=workspace_id,
        scope=Scope.WORKSPACE,
    )
    query = portable.embedder.embed(["the canonical row written through numpy"])[0]
    portable.store.close()

    with pytest.raises(
        RuntimeError, match="read-only sqlite-vec index is unavailable or stale",
    ):
        MemoryEngine.create(
            str(db_path), embed_dim=DIM, vector_backend="sqlite-vec",
            auto_evolve=False, read_only=True,
        )

    fallback = MemoryEngine.create(
        str(db_path), embed_dim=DIM, vector_backend="auto",
        auto_evolve=False, read_only=True,
    )
    assert isinstance(fallback.index, NumpyVectorIndex)
    hits = fallback.index.search(
        query, k=10, filter=SearchFilter(workspace_id=workspace_id),
    )
    assert late_id in {memory_id for memory_id, _score in hits}
    fallback.store.close()


def test_read_only_stale_native_index_fails_or_falls_back_without_migration(tmp_path):
    db_path = tmp_path / "read-only-stale.db"
    writable = Store(str(db_path))
    SqliteVecVectorIndex(writable, DIM)
    writable.close()

    read_only = Store(str(db_path), read_only=True)
    with pytest.raises(
        RuntimeError, match="read-only sqlite-vec index is unavailable or stale"
    ):
        SqliteVecVectorIndex(read_only, DIM)
    with pytest.raises(
        RuntimeError, match="read-only sqlite-vec index is unavailable or stale"
    ):
        SqliteVecVectorIndex(read_only, 32)

    fallback = get_vector_index(read_only, dim=32, prefer="auto")
    assert isinstance(fallback, NumpyVectorIndex)
    state = read_only.conn.execute(
        "SELECT format_version, dimension FROM mem_vec_ann_state WHERE singleton=1"
    ).fetchone()
    assert (state["format_version"], state["dimension"]) == (0, DIM)
    read_only.close()


def test_native_dimension_migration_rolls_back_on_create_failure(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "dimension-migration-failure.db"))
    workspace_id = store.get_or_create_workspace("dimension-migration-failure")
    vector = np.zeros(DIM, dtype=np.float32)
    vector[0] = 1.0
    memory_id = store.add_memory(MemoryRecord(
        id="",
        content="preserved native vector",
        workspace_id=workspace_id,
        scope=Scope.WORKSPACE,
        embedding=vector,
    ))
    index = SqliteVecVectorIndex(store, DIM)
    index.upsert([memory_id], vector.reshape(1, -1))
    index.mark_rebuild_complete()

    connection_type = type(store.conn)
    original_execute = connection_type.execute

    def fail_replacement(connection, sql, params=()):
        normalized = " ".join(sql.split())
        if (
            normalized.startswith("CREATE VIRTUAL TABLE")
            and "mem_vec_ann" in normalized
            and "FLOAT[32]" in normalized
        ):
            raise RuntimeError("simulated native table creation failure")
        return original_execute(connection, sql, params)

    monkeypatch.setattr(connection_type, "execute", fail_replacement)
    with pytest.raises(RuntimeError, match="simulated native table creation failure"):
        SqliteVecVectorIndex(store, 32)

    sql = store.conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='mem_vec_ann'"
    ).fetchone()["sql"]
    state = store.conn.execute(
        "SELECT format_version, dimension FROM mem_vec_ann_state WHERE singleton=1"
    ).fetchone()
    assert "FLOAT[64]" in sql
    assert (state["format_version"], state["dimension"]) == (3, DIM)
    assert store.conn.execute(
        "SELECT 1 FROM mem_vec_ann WHERE id=?", (memory_id,)
    ).fetchone() is not None
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


def test_session_failure_rolls_back_native_row_in_store_transaction(monkeypatch):
    eng = MemoryEngine.create(
        ":memory:", embed_dim=DIM, vector_backend="sqlite-vec", auto_evolve=False
    )
    workspace_id = eng.store.get_or_create_workspace("native-session-rollback")
    repo_id = eng.store.get_or_create_repo(workspace_id, "repo")
    session_id = eng.start_session(workspace_id, repo_id)
    upsert_transactions = []
    original_upsert = eng.index.upsert

    def record_upsert_transaction(*args, **kwargs):
        upsert_transactions.append((
            eng.store.conn.in_transaction,
            eng.store.conn.transaction_owned_by_current_thread(),
        ))
        return original_upsert(*args, **kwargs)

    def fail_after_native_upsert(*_args, **_kwargs):
        raise RuntimeError("late native session failure")

    monkeypatch.setattr(eng.index, "upsert", record_upsert_transaction)
    monkeypatch.setattr(eng, "_evolve", fail_after_native_upsert)

    with pytest.raises(RuntimeError, match="late native session failure"):
        eng.remember(
            "native session write that must roll back",
            workspace_id=workspace_id,
            repo_id=repo_id,
            session_id=session_id,
            scope=Scope.SESSION,
            resolve_conflicts=False,
        )

    assert eng.index.shares_store_transaction is True
    assert upsert_transactions == [(True, True)]
    assert eng.store.conn.execute("SELECT COUNT(*) FROM mem_vec_ann").fetchone()[0] == 0
    assert eng.store.list_memories(
        SearchFilter(workspace_id=workspace_id, session_id=session_id),
        include_invalid=True,
    ) == []
    assert eng.store.conn.in_transaction is False
    eng.store.close()


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


def test_filtered_search_uses_minimal_cached_visibility_lookups(monkeypatch):
    store, wid, rid, emb, index = _fixture()
    for i in range(8):
        _make(store, index, emb, wid, rid, f"visible batch fact {i}")
    calls = 0
    checked = set()
    original = store.visible_memory_ids

    def visible(memory_ids, flt, *, include_invalid=False):
        nonlocal calls
        batch = list(memory_ids)
        calls += 1
        assert len(batch) <= 8
        assert checked.isdisjoint(batch)
        checked.update(batch)
        return original(batch, flt, include_invalid=include_invalid)

    monkeypatch.setattr(store, "visible_memory_ids", visible)
    monkeypatch.setattr(
        store,
        "get_memories",
        lambda _memory_ids: (_ for _ in ()).throw(
            AssertionError("full-record hydration")
        ),
    )

    hits = index.search(
        emb.embed(["visible batch fact"])[0],
        k=5,
        filter=SearchFilter(workspace_id=wid),
    )

    assert len(hits) == 5
    assert calls >= 1
    assert len(checked) <= 8
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
