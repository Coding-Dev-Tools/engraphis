import pytest

from engraphis.core.interfaces import (
    Edge,
    GraphLayer,
    MemoryRecord,
    MemoryType,
    Node,
    Scope,
    SearchFilter,
)
from engraphis.core.store import Store


@pytest.fixture()
def store():
    s = Store(":memory:")
    yield s
    s.close()


def test_schema_version(store):
    assert store.schema_version == 3


def test_concurrent_writes_do_not_corrupt_or_lose_data(tmp_path):
    # The shared connection is serialized (_SerializedConnection): concurrent threadpool
    # writers must not interleave transactions on it. Every write from every thread must
    # land, with no "database is locked"/cursor-corruption errors.
    import threading

    store = Store(str(tmp_path / "concurrent.db"))
    errors: list = []
    n_threads, per = 8, 25

    def worker(t: int) -> None:
        try:
            for i in range(per):
                store.create_workspace("ws-%d-%d" % (t, i))
        except Exception as exc:  # noqa: BLE001 — surface for the assertion
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    count = store.conn.execute("SELECT COUNT(*) AS n FROM workspaces").fetchone()["n"]
    store.close()
    assert not errors, errors
    assert count == n_threads * per


def test_wrapper_releases_lock_after_a_failing_statement(tmp_path):
    # A statement that raises mid-transaction must roll back and free the write lock, or
    # the next writer would deadlock on the shared connection.
    store = Store(str(tmp_path / "recover.db"))
    with pytest.raises(Exception):
        store.conn.execute("INSERT INTO does_not_exist(x) VALUES (1)")
    # The lock is free again: a normal write still succeeds.
    assert store.create_workspace("after-error")
    store.close()


def test_wrapper_rolls_back_and_releases_on_constraint_violation(tmp_path):
    # A failed single write that OPENED a transaction (e.g. a PK/UNIQUE violation) must roll
    # back and release the lock — otherwise the pin leaks: other threads stall and this
    # thread's next request inherits a stale open transaction that could commit the reject.
    import sqlite3
    store = Store(str(tmp_path / "constraint.db"))
    store.create_workspace("ws1")
    wid = store.conn.execute(
        "SELECT id FROM workspaces WHERE name='ws1'").fetchone()["id"]
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute(
            "INSERT INTO workspaces(id, name, created_at, settings) VALUES (?,?,?,?)",
            (wid, "dup", 0.0, "{}"))          # duplicate primary key
    # Lock released + failed row rolled back: the next write works and 'dup' never landed.
    assert store.create_workspace("ws2")
    names = {r["name"] for r in store.conn.execute("SELECT name FROM workspaces")}
    assert names == {"ws1", "ws2"}
    store.close()


def test_v3_migration_classifies_existing_graph_layers_once(tmp_path):
    db = tmp_path / "v2.db"
    original = Store(str(db))
    wid = original.get_or_create_workspace("acme")
    original.conn.execute(
        "INSERT INTO edges(id, workspace_id, src, dst, relation, layer) "
        "VALUES ('edge_old', ?, 'a', 'b', 'works_at', 'semantic')",
        (wid,),
    )
    original.conn.execute("DELETE FROM schema_migrations")
    original.conn.execute(
        "INSERT INTO schema_migrations(version, applied_at) VALUES (2, 0)"
    )
    original.conn.commit()
    original.close()

    migrated = Store(str(db))
    row = migrated.conn.execute(
        "SELECT layer FROM edges WHERE id='edge_old'"
    ).fetchone()
    assert migrated.schema_version == 3
    assert row["layer"] == "entity"
    migrated.conn.execute(
        "UPDATE edges SET layer='causal' WHERE id='edge_old'"
    )
    migrated.conn.commit()
    migrated.close()

    reopened = Store(str(db))
    assert reopened.conn.execute(
        "SELECT layer FROM edges WHERE id='edge_old'"
    ).fetchone()["layer"] == "causal"


def test_workspace_repo_session(store):
    wid = store.get_or_create_workspace("acme")
    assert store.get_or_create_workspace("acme") == wid  # idempotent
    rid = store.get_or_create_repo(wid, "web-app")
    sid = store.start_session(wid, rid, agent="claude-code", goal="refactor auth")
    store.end_session(sid, summary="did the refactor", open_threads=["tests 3-5 failing"])
    sess = store.get_session(sid)
    assert sess["status"] == "summarized"
    assert sess["open_threads"] == ["tests 3-5 failing"]


def test_memory_roundtrip(store):
    wid = store.get_or_create_workspace("w")
    rid = store.get_or_create_repo(wid, "r")
    mid = store.add_memory(MemoryRecord(
        id="", content="Auth uses PASETO v4 tokens.", mtype=MemoryType.SEMANTIC,
        scope=Scope.REPO, workspace_id=wid, repo_id=rid, title="auth", keywords=["auth", "paseto"],
    ))
    rec = store.get_memory(mid)
    assert rec is not None
    assert rec.mtype == MemoryType.SEMANTIC and rec.scope == Scope.REPO
    assert rec.keywords == ["auth", "paseto"]
    assert rec.ingested_at is not None and rec.valid_from is not None


def test_bitemporal_visibility(store):
    wid = store.get_or_create_workspace("w")
    rid = store.get_or_create_repo(wid, "r")
    # A fact that was true only between t=1000 and t=2000 (already expired in world-time).
    mid = store.add_memory(MemoryRecord(
        id="", content="We were on JWT.", workspace_id=wid, repo_id=rid,
        valid_from=1000.0, valid_to=2000.0,
    ))
    flt = SearchFilter(workspace_id=wid)
    # Default (as_of=now): the closed fact is not visible.
    assert mid not in [m.id for m in store.list_memories(flt)]
    # include_invalid: visible.
    assert mid in [m.id for m in store.list_memories(flt, include_invalid=True)]
    # Time-travel to when it was valid: visible.
    assert mid in [m.id for m in store.list_memories(SearchFilter(workspace_id=wid, as_of=1500.0))]


def test_close_validity(store):
    wid = store.get_or_create_workspace("w")
    rid = store.get_or_create_repo(wid, "r")
    mid = store.add_memory(MemoryRecord(id="", content="current fact", workspace_id=wid, repo_id=rid))
    assert mid in [m.id for m in store.list_memories(SearchFilter(workspace_id=wid))]
    store.close_validity(mid, reason="contradicted by new info")
    assert mid not in [m.id for m in store.list_memories(SearchFilter(workspace_id=wid))]


def test_fts_search(store):
    wid = store.get_or_create_workspace("w")
    rid = store.get_or_create_repo(wid, "r")
    store.add_memory(MemoryRecord(id="", content="The staging database runs PostgreSQL 16.",
                                  workspace_id=wid, repo_id=rid))
    store.add_memory(MemoryRecord(id="", content="The user prefers dark mode.",
                                  workspace_id=wid, repo_id=rid))
    hits = store.fts_search("PostgreSQL", k=5)
    assert hits and "postgres" in store.get_memory(hits[0][0]).content.lower()


def test_graph_neighbors(store):
    wid = store.get_or_create_workspace("w")
    rid = store.get_or_create_repo(wid, "r")
    store.upsert_entity(Node(id="", name="auth.py", ntype="file", workspace_id=wid, repo_id=rid))
    store.upsert_entity(Node(id="", name="PASETO", ntype="lib", workspace_id=wid, repo_id=rid))
    store.upsert_edge(Edge(id="", src="auth.py", dst="PASETO", relation="uses",
                           workspace_id=wid, repo_id=rid))
    nbrs = store.neighbors(["auth.py"])
    assert any(e.dst == "PASETO" and e.relation == "uses" for e in nbrs)
    assert nbrs[0].layer == GraphLayer.ENTITY


def test_graph_neighbors_filters_by_layer(store):
    """1-hop expansion honors the logical-overlay selection, same as
    edges_in_scope/links_among — a `timeline` intent must not traverse
    entity/causal edges (PR #19 review follow-up)."""
    wid = store.get_or_create_workspace("w")
    store.upsert_entity(Node(id="", name="deploy", ntype="event", workspace_id=wid))
    store.upsert_entity(Node(id="", name="outage", ntype="event", workspace_id=wid))
    store.upsert_entity(Node(id="", name="oncall", ntype="person", workspace_id=wid))
    store.upsert_edge(Edge(id="", src="deploy", dst="outage", relation="causes",
                           workspace_id=wid))
    store.upsert_edge(Edge(id="", src="deploy", dst="oncall", relation="owned_by",
                           workspace_id=wid))
    assert len(store.neighbors(["deploy"])) == 2
    causal = store.neighbors(["deploy"], layers=[GraphLayer.CAUSAL])
    assert [e.dst for e in causal] == ["outage"]
    assert store.neighbors(["deploy"], layers=[GraphLayer.TEMPORAL]) == []


def test_code_listing_helpers_honor_limit(store):
    """service.graph() bounds its per-repo code fetches; the SQL layer must
    actually enforce the cap rather than materializing the whole repo."""
    for i in range(5):
        store.upsert_symbol(repo_id="repo_x", kind="function", name=f"f{i}",
                            fqname=f"f{i}", file="mod.py", span="1-1")
    assert len(store.list_symbols("repo_x")) == 5
    assert len(store.list_symbols("repo_x", limit=2)) == 2
    for i in range(4):
        store.add_code_edge(repo_id="repo_x", src=f"f{i}", dst=f"f{i + 1}",
                            relation="calls", file="mod.py", line=i + 1)
    assert len(store.list_code_edges("repo_x")) == 4
    assert len(store.list_code_edges("repo_x", limit=3)) == 3


def test_memory_links_infer_and_filter_graph_layers(store):
    wid = store.get_or_create_workspace("w")
    a = store.add_memory(MemoryRecord(id="", content="cause", workspace_id=wid))
    b = store.add_memory(MemoryRecord(id="", content="effect", workspace_id=wid))
    store.add_link(a, b, relation="causes")
    assert store.get_links(a)[0]["layer"] == "causal"
    assert store.links_among([a, b], layers=[GraphLayer.CAUSAL])
    assert store.links_among([a, b], layers=[GraphLayer.TEMPORAL]) == []


def test_reinforce_increases_stability_and_count(store):
    wid = store.get_or_create_workspace("w")
    rid = store.get_or_create_repo(wid, "r")
    mid = store.add_memory(MemoryRecord(id="", content="reinforce me", workspace_id=wid, repo_id=rid))
    before = store.get_memory(mid)
    store.reinforce(mid)
    after = store.get_memory(mid)
    assert after.access_count == before.access_count + 1
    assert after.stability > before.stability


def test_symbol_roundtrip_and_search(store):
    sid = store.upsert_symbol(repo_id="repo_x", kind="function", name="add", fqname="add",
                              file="calc.py", span="1-2", signature="def add(a, b):",
                              lang="python", exported=True, content_hash="abc123")
    assert sid.startswith("sym_")
    hits = store.search_symbols("repo_x", "add")
    assert any(h["name"] == "add" for h in hits)
    assert store.count_symbols("repo_x") == 1


def test_clear_symbols_for_file_replaces_not_accumulates(store):
    store.upsert_symbol(repo_id="repo_x", kind="function", name="old", fqname="old",
                        file="calc.py", span="1-1")
    store.clear_symbols_for_file("repo_x", "calc.py")
    store.upsert_symbol(repo_id="repo_x", kind="function", name="new", fqname="new",
                        file="calc.py", span="1-1")
    names = {h["name"] for h in store.search_symbols("repo_x", "")}
    assert names == {"new"}


def test_code_edge_callers(store):
    store.add_code_edge(repo_id="repo_x", src="Calculator", dst="add", relation="calls",
                        file="calc.py", line=9)
    callers = store.get_symbol_callers("repo_x", "add")
    assert any(c["src"] == "Calculator" for c in callers)


# ── regression: iter_vectors must not hand out a live cursor (see store.py) ───

def _spy_on_fetchall(store_mod, monkeypatch):
    """Record every locked/materialized read. Patched on the class, not the instance:
    _SerializedConnection.__setattr__ forwards to the wrapped sqlite3 connection."""
    calls: list = []
    real = store_mod._SerializedConnection.fetchall

    def spy(self, *a, **k):
        calls.append(a[0])
        return real(self, *a, **k)

    monkeypatch.setattr(store_mod._SerializedConnection, "fetchall", spy)
    return calls


def test_iter_vectors_materializes_in_bounded_batches(store, monkeypatch):
    """The read is drained inside the connection lock, one bounded batch at a time.

    Regression: iter_vectors used to yield straight off a cursor returned by
    conn.execute(), which releases the lock before the caller fetches — so another
    thread's write could interleave with an in-flight read on the shared connection.
    """
    import numpy as np

    from engraphis.core import store as store_mod

    wid = store.get_or_create_workspace("w")
    for i in range(10):
        store.add_memory(MemoryRecord(id="mem_%02d" % i, content="c%d" % i,
                                      workspace_id=wid,
                                      embedding=np.ones(4, dtype=np.float32)))

    monkeypatch.setattr(store_mod, "VECTOR_SCAN_BATCH", 3)
    calls = _spy_on_fetchall(store_mod, monkeypatch)

    got = [mid for mid, _ in store.iter_vectors()]

    assert got == sorted(got)                      # keyset pagination => stable order
    assert len(got) == len(set(got)) == 10         # every row exactly once
    # 10 rows / batch of 3 => 4 fetches (the last is short and terminates the loop).
    assert len(calls) == 4
    assert all("LIMIT ?" in sql for sql in calls)


def test_iter_vectors_tolerates_concurrent_writes(tmp_path):
    """A writer on another thread must not corrupt or truncate an in-flight scan."""
    import threading

    import numpy as np

    s = Store(str(tmp_path / "vec.db"))
    wid = s.get_or_create_workspace("w")
    original = {"mem_%03d" % i for i in range(40)}
    for mid in sorted(original):
        s.add_memory(MemoryRecord(id=mid, content=mid, workspace_id=wid,
                                  embedding=np.ones(4, dtype=np.float32)))

    errors: list = []
    stop = threading.Event()

    def writer():
        try:
            i = 0
            while not stop.is_set():
                s.add_memory(MemoryRecord(id="zzz_%03d" % i, content="new",
                                          workspace_id=wid,
                                          embedding=np.ones(4, dtype=np.float32)))
                i += 1
        except Exception as exc:  # noqa: BLE001 — surface for the assertion
            errors.append(exc)

    th = threading.Thread(target=writer)
    th.start()
    try:
        seen = [mid for mid, _ in s.iter_vectors()]
    finally:
        stop.set()
        th.join()
    s.close()

    assert not errors, errors
    assert len(seen) == len(set(seen))             # no row yielded twice
    assert original <= set(seen)                   # nothing pre-existing was skipped


# ── regression: invalidate_edges_for_memory must not scan every tenant ────────

def _edge_with_support(store, *, eid, workspace_id, memory_id):
    store.upsert_edge(Edge(id=eid, src="a", dst="b", relation="rel",
                           workspace_id=workspace_id,
                           provenance={"memory_id": memory_id,
                                       "memory_ids": [memory_id]}))


def test_invalidate_edges_is_scoped_to_the_owning_workspace(store):
    w1 = store.get_or_create_workspace("w1")
    w2 = store.get_or_create_workspace("w2")
    mid = "mem_shared_id"
    store.add_memory(MemoryRecord(id=mid, content="x", workspace_id=w1))
    _edge_with_support(store, eid="edge_w1", workspace_id=w1, memory_id=mid)
    _edge_with_support(store, eid="edge_w2", workspace_id=w2, memory_id=mid)
    _edge_with_support(store, eid="edge_global", workspace_id=None, memory_id=mid)

    store.invalidate_edges_for_memory(mid)

    closed = {r["id"] for r in store.conn.execute(
        "SELECT id FROM edges WHERE valid_to IS NOT NULL").fetchall()}
    assert "edge_w1" in closed          # the owning workspace's edge is closed
    assert "edge_global" in closed      # unscoped edges stay in scope (unchanged behaviour)
    assert "edge_w2" not in closed      # another tenant's edge is never touched


def test_invalidate_edges_escapes_like_wildcards(store):
    wid = store.get_or_create_workspace("w")
    wild = "mem_%"
    other = "mem_other"
    store.add_memory(MemoryRecord(id=wild, content="x", workspace_id=wid))
    store.add_memory(MemoryRecord(id=other, content="x", workspace_id=wid))
    _edge_with_support(store, eid="edge_other", workspace_id=wid, memory_id=other)

    store.invalidate_edges_for_memory(wild)

    # 'mem_%' must not behave as a LIKE pattern matching every mem_* id.
    row = store.conn.execute(
        "SELECT valid_to FROM edges WHERE id='edge_other'").fetchone()
    assert row["valid_to"] is None


def test_invalidate_edges_keeps_edges_with_remaining_support(store):
    wid = store.get_or_create_workspace("w")
    a = store.add_memory(MemoryRecord(id="mem_a", content="a", workspace_id=wid))
    b = store.add_memory(MemoryRecord(id="mem_b", content="b", workspace_id=wid))
    store.upsert_edge(Edge(id="edge_two", src="s", dst="d", relation="rel",
                           workspace_id=wid,
                           provenance={"memory_id": a, "memory_ids": [a, b]}))

    store.invalidate_edges_for_memory(a)

    row = store.conn.execute(
        "SELECT valid_to, provenance FROM edges WHERE id='edge_two'").fetchone()
    assert row["valid_to"] is None
    assert b in row["provenance"] and a not in row["provenance"]


# ── regression: batched get_memories ──────────────────────────────────────────

def test_get_memories_batches_and_matches_get_memory(store):
    from engraphis.core import store as store_mod

    wid = store.get_or_create_workspace("w")
    ids = [store.add_memory(MemoryRecord(id="", content="c%d" % i, workspace_id=wid))
           for i in range(12)]

    got = store.get_memories(ids + ids + ["mem_missing", ""])

    assert set(got) == set(ids)                    # missing/empty ids are simply absent
    for mid in ids:
        assert got[mid].content == store.get_memory(mid).content
    assert store.get_memories([]) == {}
    assert store_mod.IN_CLAUSE_CHUNK <= 999        # stays under SQLITE_MAX_VARIABLE_NUMBER


def test_get_memories_chunks_past_the_variable_limit(store, monkeypatch):
    from engraphis.core import store as store_mod

    wid = store.get_or_create_workspace("w")
    ids = [store.add_memory(MemoryRecord(id="", content="c%d" % i, workspace_id=wid))
           for i in range(7)]
    monkeypatch.setattr(store_mod, "IN_CLAUSE_CHUNK", 2)
    calls = _spy_on_fetchall(store_mod, monkeypatch)

    got = store.get_memories(ids)

    assert set(got) == set(ids)
    assert len(calls) == 4                         # ceil(7 / 2)


# ── regression: LIKE wildcards in the non-FTS5 lexical fallback ───────────────

def test_fts_fallback_escapes_like_wildcards(store):
    wid = store.get_or_create_workspace("w")
    store.add_memory(MemoryRecord(id="mem_pct", content="deploys are 100% green",
                                  workspace_id=wid))
    store.add_memory(MemoryRecord(id="mem_plain", content="nothing special here",
                                  workspace_id=wid))
    store.has_fts5 = False                          # force the LIKE fallback

    hits = {mid for mid, _ in store.fts_search("100%", 10)}
    assert hits == {"mem_pct"}                      # '%' is literal, not "match everything"

    assert {mid for mid, _ in store.fts_search("%", 10)} == {"mem_pct"}
    assert store.fts_search("_", 10) == []           # '_' is literal, not "any character"


# ── regression: indexes exist, and are added to pre-existing databases ────────

def _index_names(conn):
    return {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'").fetchall()}


NEW_INDEXES = {"idx_audit_target", "idx_edge_workspace_repo", "idx_mem_links_b"}


def test_new_indexes_exist_on_a_fresh_database(store):
    assert NEW_INDEXES <= _index_names(store.conn)


def test_new_indexes_are_added_to_an_existing_database(tmp_path):
    path = str(tmp_path / "legacy.db")
    s = Store(path)
    for name in NEW_INDEXES:
        s.conn.execute("DROP INDEX IF EXISTS %s" % name)
    s.conn.commit()
    assert not (NEW_INDEXES & _index_names(s.conn))
    s.close()

    s2 = Store(path)                                # re-open runs the schema script again
    try:
        assert NEW_INDEXES <= _index_names(s2.conn)
    finally:
        s2.close()


def test_audit_index_is_used_by_the_inspect_query(store):
    plan = " ".join(str(r[3]) for r in store.conn.execute(
        "EXPLAIN QUERY PLAN SELECT ts, actor, action, detail FROM audit "
        "WHERE target=? ORDER BY ts", ("mem_x",)).fetchall())
    assert "idx_audit_target" in plan


def test_mem_links_b_index_is_used(store):
    plan = " ".join(str(r[3]) for r in store.conn.execute(
        "EXPLAIN QUERY PLAN SELECT a, b FROM mem_links WHERE b=?", ("mem_x",)).fetchall())
    assert "idx_mem_links_b" in plan
