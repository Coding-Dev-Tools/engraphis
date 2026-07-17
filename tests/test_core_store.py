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
