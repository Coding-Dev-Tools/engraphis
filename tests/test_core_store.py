import json
import threading

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
from engraphis.core.store import Store, normalize_entity_name


@pytest.fixture()
def store():
    s = Store(":memory:")
    yield s
    s.close()


def test_schema_version(store):
    assert store.schema_version == 10


# Forward-compat test: TEAM scope wiring is incomplete (see Scope.TEAM note
# in interfaces.py). This pins the storage surface so the column round-trips
# correctly when the full RBAC/read-side feature lands.
def test_team_id_persists_and_is_queryable(store):
    wid = store.get_or_create_workspace("w")
    mid = store.add_memory(MemoryRecord(
        id="", content="Team-scoped fact.", workspace_id=wid,
        team_id="team_xyz",
    ))
    rec = store.get_memory(mid)
    assert rec is not None
    assert rec.team_id == "team_xyz"
    rows = store.conn.execute(
        "SELECT id FROM memories WHERE team_id=?", ("team_xyz",)
    ).fetchall()
    assert [row["id"] for row in rows] == [mid]


def test_prompt_memory_listing_excludes_pending_rows_before_capping(store):
    wid = store.get_or_create_workspace("w")
    approved = store.add_memory(MemoryRecord(
        id="", content="Approved release history.", workspace_id=wid,
        provenance={"trusted": True, "review_state": "approved"}, ingested_at=1.0,
    ))
    store.add_memory(MemoryRecord(
        id="", content="Pending release history.", workspace_id=wid,
        provenance={"trusted": False, "review_state": "pending"}, ingested_at=2.0,
    ))

    rows = store.list_memories(
        SearchFilter(workspace_id=wid), limit=1, prompt_only=True,
    )

    assert [row.id for row in rows] == [approved]


def test_clean_v7_schema_has_temporal_code_and_memory_link_tables(store):
    tables = {row["name"] for row in store.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    link_columns = {row["name"] for row in store.conn.execute(
        "PRAGMA table_info(code_memory_links)"
    ).fetchall()}
    incidence_columns = {row["name"] for row in store.conn.execute(
        "PRAGMA table_info(memory_entities)"
    ).fetchall()}
    direct_link_columns = {row["name"] for row in store.conn.execute(
        "PRAGMA table_info(mem_links)"
    ).fetchall()}
    file_history_columns = {row["name"] for row in store.conn.execute(
        "PRAGMA table_info(code_file_history)"
    ).fetchall()}

    assert "memory_entities" in tables
    assert "code_file_history" in tables
    assert "embedding_state" in tables
    assert {"valid_from", "valid_to", "ingested_at", "expired_at"} <= link_columns
    assert {"valid_from", "valid_to", "valid_to_recorded_at", "ingested_at", "expired_at"} <= file_history_columns
    assert {"memory_id", "entity_id", "source_kind", "confidence"} <= incidence_columns
    assert {"valid_from", "valid_to", "valid_to_recorded_at", "ingested_at", "expired_at"} <= direct_link_columns


def test_entity_normalization_preserves_meaningful_punctuation():
    assert normalize_entity_name("  OpenAI\tPlatform  ") == "openai platform"
    assert normalize_entity_name("C++") != normalize_entity_name("C#")
    assert normalize_entity_name("AT&T") != normalize_entity_name("ATT")


def test_live_canonicalization_preserves_punctuation_with_shared_tokens(store):
    wid = store.get_or_create_workspace("w")
    cpp = store.upsert_entity(Node(
        id="ent_cpp_language", name="C++ language", ntype="topic", workspace_id=wid,
    ))
    csharp = store.upsert_entity(Node(
        id="ent_csharp_language", name="C# language", ntype="topic", workspace_id=wid,
    ))
    rows = store.conn.execute(
        "SELECT id, canonical_id FROM entities WHERE id IN (?, ?) ORDER BY id",
        (cpp, csharp),
    ).fetchall()
    assert [row["canonical_id"] for row in rows] == [cpp, csharp]

def test_live_entity_canonicalization_searches_beyond_arbitrary_peer_cap(store):
    wid = store.get_or_create_workspace("w")
    canonical = store.upsert_entity(Node(
        id="ent_openai", name="OpenAI", ntype="org", workspace_id=wid,
    ))
    for index in range(500):
        store.upsert_entity(Node(
            id=f"ent_filler_{index}", name=f"Filler Company {index}",
            ntype="org", workspace_id=wid,
        ))
    alias = store.upsert_entity(Node(
        id="ent_open_ai", name="Open AI", ntype="org", workspace_id=wid,
    ))
    row = store.conn.execute(
        "SELECT canonical_id, canonical_method FROM entities WHERE id=?", (alias,)
    ).fetchone()
    assert row["canonical_id"] == canonical
    assert row["canonical_method"] == "token_overlap"

def test_entity_blocking_chunks_long_token_names(store):
    wid = store.get_or_create_workspace("w")
    tokens = " ".join(f"tok{index}x" for index in range(600))
    canonical = store.upsert_entity(Node(
        id="ent_long_canonical", name=tokens, ntype="topic", workspace_id=wid,
    ))
    alias = store.upsert_entity(Node(
        id="ent_long_alias", name=tokens + " alias", ntype="topic", workspace_id=wid,
    ))
    row = store.conn.execute(
        "SELECT canonical_id, canonical_method FROM entities WHERE id=?", (alias,)
    ).fetchone()
    assert canonical != alias
    assert row["canonical_id"] == canonical
    assert row["canonical_method"] == "token_overlap"

def test_entity_blocking_skips_broad_token_buckets(store, monkeypatch):
    from engraphis.core import store as store_module

    monkeypatch.setattr(store_module, "ENTITY_BLOCK_BUCKET_LIMIT", 2)
    wid = store.get_or_create_workspace("w")
    for index in range(3):
        store.upsert_entity(Node(
            id=f"ent_shared_{index}", name=f"Shared Entity {index}",
            ntype="topic", workspace_id=wid,
        ))
    alias = store.upsert_entity(Node(
        id="ent_shared_alias", name="Shared Entity Alias",
        ntype="topic", workspace_id=wid,
    ))
    row = store.conn.execute(
        "SELECT canonical_id FROM entities WHERE id=?", (alias,)
    ).fetchone()
    assert row["canonical_id"] == alias


def test_replacing_edge_closes_removed_normalized_support(store):
    wid = store.get_or_create_workspace("w")
    first = store.add_memory(MemoryRecord(id="mem_first", content="first",
                                          workspace_id=wid))
    second = store.add_memory(MemoryRecord(id="mem_second", content="second",
                                           workspace_id=wid))
    store.upsert_edge(Edge(
        id="edge_replace", src="a", dst="b", relation="rel", workspace_id=wid,
        provenance={"memory_id": first, "memory_ids": [first]},
    ))
    store.upsert_edge(Edge(
        id="edge_replace", src="a", dst="b", relation="rel", workspace_id=wid,
        provenance={"memory_id": second, "memory_ids": [second]},
    ))

    rows = [dict(row) for row in store.conn.execute(
        "SELECT memory_id, valid_to FROM edge_supports "
        "WHERE edge_id='edge_replace' ORDER BY id"
    )]
    assert [row["memory_id"] for row in rows] == [first, second]
    assert rows[0]["valid_to"] is not None
    assert rows[1]["valid_to"] is None


def test_edge_provenance_preserves_declared_primary_memory_order(store):
    wid = store.get_or_create_workspace("w")
    store.upsert_edge(Edge(
        id="edge_order", src="a", dst="b", relation="rel", workspace_id=wid,
        provenance={"memory_id": "mem_z", "memory_ids": ["mem_z", "mem_a"]},
    ))

    provenance = json.loads(store.conn.execute(
        "SELECT provenance FROM edges WHERE id='edge_order'"
    ).fetchone()["provenance"])
    assert provenance["memory_id"] == "mem_z"
    assert provenance["memory_ids"] == ["mem_z", "mem_a"]

def test_upsert_edge_support_failure_rolls_back_edge_and_releases_lock(store, monkeypatch):
    edge = Edge(
        id="edge-support-failure", src="source", dst="target", relation="related",
        provenance={"memory_id": "mem-support"},
    )

    def fail_support(*args, **kwargs):
        raise RuntimeError("support unavailable")

    monkeypatch.setattr(store, "_write_edge_supports", fail_support)
    with pytest.raises(RuntimeError, match="support unavailable"):
        store.upsert_edge(edge)
    assert store.conn.execute(
        "SELECT 1 FROM edges WHERE id=?", (edge.id,)
    ).fetchone() is None

    monkeypatch.undo()
    assert store.upsert_edge(edge) == edge.id
    assert store.conn.execute(
        "SELECT 1 FROM edge_supports WHERE edge_id=?", (edge.id,)
    ).fetchone() is not None


def test_upsert_entity_backfill_failure_rolls_back_entity(store, monkeypatch):
    wid = store.get_or_create_workspace("w")
    node = Node(id="entity-backfill-failure", name="Failure Entity",
                ntype="person", workspace_id=wid)

    def fail_backfill(*args, **kwargs):
        raise RuntimeError("incidence unavailable")

    monkeypatch.setattr(store, "_backfill_entity_text_mentions", fail_backfill)
    with pytest.raises(RuntimeError, match="incidence unavailable"):
        store.upsert_entity(node)
    assert store.conn.execute(
        "SELECT 1 FROM entities WHERE id=?", (node.id,)
    ).fetchone() is None

    monkeypatch.undo()
    assert store.upsert_entity(node) == node.id


def test_upsert_entity_failure_after_waiting_for_other_transaction_releases_lock(
    store, monkeypatch,
):
    wid = store.get_or_create_workspace("w")
    node = Node(
        id="entity-waiting-failure", name="Waiting Failure",
        ntype="person", workspace_id=wid,
    )
    entered = threading.Event()
    release = threading.Event()
    outcome = []

    def hold_transaction():
        store.conn.execute("BEGIN IMMEDIATE")
        entered.set()
        release.wait(timeout=5)
        store.conn.rollback()

    holder = threading.Thread(target=hold_transaction)
    holder.start()
    assert entered.wait(timeout=5)

    def fail_backfill(*args, **kwargs):
        raise RuntimeError("incidence unavailable")

    monkeypatch.setattr(store, "_backfill_entity_text_mentions", fail_backfill)

    def attempt_upsert():
        try:
            store.upsert_entity(node)
        except BaseException as exc:  # communicate the worker failure to the test thread
            outcome.append(exc)

    worker = threading.Thread(target=attempt_upsert)
    worker.start()
    assert not release.wait(timeout=0.05)
    release.set()
    holder.join(timeout=5)
    worker.join(timeout=5)

    assert not holder.is_alive()
    assert not worker.is_alive()
    assert len(outcome) == 1
    assert isinstance(outcome[0], RuntimeError)
    assert store.conn.in_transaction is False
    assert store.conn.transaction_owned_by_current_thread() is False
    assert store.conn.execute(
        "SELECT 1 FROM entities WHERE id=?", (node.id,)
    ).fetchone() is None

@pytest.mark.parametrize("method_name", ("add_link", "add_link_version"))
def test_link_writes_release_transaction_after_waiting_for_other_thread(
    store, monkeypatch, method_name,
):
    entered = threading.Event()
    release = threading.Event()
    outcome = []

    def hold_transaction():
        store.conn.execute("BEGIN IMMEDIATE")
        entered.set()
        release.wait(timeout=5)
        store.conn.rollback()

    holder = threading.Thread(target=hold_transaction)
    holder.start()
    assert entered.wait(timeout=5)

    def fail_commit(_connection):
        raise RuntimeError("commit unavailable")

    monkeypatch.setattr(type(store.conn), "commit", fail_commit)

    def attempt_link():
        try:
            getattr(store, method_name)("link-a", "link-b", relation="related")
        except BaseException as exc:  # communicate the worker failure to the test thread
            outcome.append(exc)

    worker = threading.Thread(target=attempt_link)
    worker.start()
    assert not release.wait(timeout=0.05)
    release.set()
    holder.join(timeout=5)
    worker.join(timeout=5)
    monkeypatch.undo()

    assert not holder.is_alive()
    assert not worker.is_alive()
    assert len(outcome) == 1
    assert isinstance(outcome[0], RuntimeError)
    assert store.conn.in_transaction is False
    assert store.conn.transaction_owned_by_current_thread() is False
    assert store.conn.execute(
        "SELECT 1 FROM mem_links WHERE a=? AND b=?",
        ("link-a", "link-b"),
    ).fetchone() is None

    if method_name == "add_link_version":
        assert store.add_link_version("link-a", "link-b", relation="related") is True
    else:
        store.add_link("link-a", "link-b", relation="related")
    assert store.get_links("link-a")


def test_add_edge_support_failure_rolls_back_edge_provenance(store, monkeypatch):
    edge = Edge(id="edge-existing", src="source", dst="target", relation="related")
    store.upsert_edge(edge)

    def fail_support(*args, **kwargs):
        raise RuntimeError("support unavailable")

    monkeypatch.setattr(store, "_write_edge_supports", fail_support)
    with pytest.raises(RuntimeError, match="support unavailable"):
        store.add_edge_support(edge.id, {"memory_id": "mem-support"})
    row = store.conn.execute(
        "SELECT provenance FROM edges WHERE id=?", (edge.id,)
    ).fetchone()
    assert json.loads(row["provenance"]) == {}

    monkeypatch.undo()
    store.add_edge_support(edge.id, {"memory_id": "mem-support"})
    assert store.conn.execute(
        "SELECT 1 FROM edge_supports WHERE edge_id=? AND memory_id=?",
        (edge.id, "mem-support"),
    ).fetchone() is not None

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
    assert migrated.schema_version == 10
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


def test_memory_confidence_roundtrip_and_default(store):
    wid = store.get_or_create_workspace("w")
    # Default writes carry confidence 1.0 (existing behavior unchanged).
    default_id = store.add_memory(MemoryRecord(
        id="", content="A default-confidence memory.", workspace_id=wid,
    ))
    assert store.get_memory(default_id).confidence == 1.0
    # An explicit confidence value persists and survives a reload.
    confident_id = store.add_memory(MemoryRecord(
        id="", content="A 0.5-confidence memory.", workspace_id=wid, confidence=0.5,
    ))
    rec = store.get_memory(confident_id)
    assert rec is not None and rec.confidence == 0.5
    # Batched reads agree with the single-row read.
    assert store.get_memories([confident_id])[confident_id].confidence == 0.5
    # The raw column agrees too.
    row = store.conn.execute(
        "SELECT confidence FROM memories WHERE id=?", (confident_id,)
    ).fetchone()
    assert float(row["confidence"]) == 0.5
    # ON CONFLICT overwrite also persists the field.
    store.add_memory(MemoryRecord(
        id=confident_id, content="Rewritten memory.", workspace_id=wid, confidence=0.75,
    ))
    assert store.get_memory(confident_id).confidence == 0.75

def test_pin_transitions_record_latest_effective_marker(store, monkeypatch):
    from engraphis.core import store as store_mod

    wid = store.get_or_create_workspace("w")
    mid = store.add_memory(MemoryRecord(
        id="", content="A pin-lattice memory.", workspace_id=wid,
    ))
    clock = iter((100.0, 200.0, 300.0, 400.0))
    monkeypatch.setattr(store_mod, "now_ts", lambda: next(clock))

    store.set_pinned(mid, True)
    store.set_pinned(mid, False)
    store.set_pinned(mid, True)
    row = store.conn.execute(
        "SELECT pinned, pinned_at, unpinned_at FROM memories WHERE id=?", (mid,)
    ).fetchone()
    assert (row["pinned"], row["pinned_at"], row["unpinned_at"]) == (1, 300.0, 200.0)

    # Repeating an already-effective state is idempotent and does not move its marker.
    store.set_pinned(mid, True)
    row = store.conn.execute(
        "SELECT pinned_at, unpinned_at FROM memories WHERE id=?", (mid,)
    ).fetchone()
    assert (row["pinned_at"], row["unpinned_at"]) == (300.0, 200.0)


def test_add_memory_mirror_failure_rolls_back_row_and_releases_lock(store, monkeypatch):
    wid = store.get_or_create_workspace("w")
    rec = MemoryRecord(id="mirror-failure", content="mirror failure", workspace_id=wid)

    def fail_mirror(*args, **kwargs):
        raise RuntimeError("FTS unavailable")

    monkeypatch.setattr(store, "_fts_upsert", fail_mirror)
    with pytest.raises(RuntimeError, match="FTS unavailable"):
        store.add_memory(rec)
    assert store.get_memory(rec.id) is None

    monkeypatch.undo()
    replacement = store.add_memory(
        MemoryRecord(id=rec.id, content="retry succeeds", workspace_id=wid)
    )
    assert replacement == rec.id
    assert store.get_memory(rec.id).content == "retry succeeds"

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


def test_add_memory_rejects_inverted_validity_interval(store):
    wid = store.get_or_create_workspace("w")
    with pytest.raises(ValueError, match="validity interval would be empty"):
        store.add_memory(MemoryRecord(
            id="", content="A fact with an impossible window.",
            workspace_id=wid, valid_from=2000.0, valid_to=1000.0,
        ))
    # The invalid row must not have been persisted.
    assert store.list_memories(SearchFilter(workspace_id=wid), include_invalid=True) == []


def test_add_memory_accepts_closed_history_with_defaulted_valid_from(store):
    """A past-only ``valid_to`` with no explicit ``valid_from`` is the accepted
    closed-history/backfill convention, not an inverted interval."""
    wid = store.get_or_create_workspace("w")
    mid = store.add_memory(MemoryRecord(
        id="", content="historical fact", workspace_id=wid, valid_to=1.0,
    ))
    rec = store.get_memory(mid)
    assert rec is not None
    assert rec.valid_to == 1.0
    assert rec.valid_from is not None and rec.valid_from > rec.valid_to


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


def test_edge_visibility_requires_a_timestamp_paired_support(store):
    wid = store.get_or_create_workspace("w")
    edge_id = store.upsert_edge(Edge(
        id="edge_pair", src="a", dst="b", relation="uses", workspace_id=wid,
        valid_from=100.0, ingested_at=100.0,
        provenance={"memory_id": "mem_initial"},
    ))
    # The edge aggregates support starts for current reads. These starts are deliberately
    # crossed: neither source establishes the relation at (world=75, known=200).
    store.add_edge_support(
        edge_id, {"memory_id": "mem_later_knowledge"},
        valid_from=50.0, ingested_at=300.0,
    )
    invisible = SearchFilter(workspace_id=wid, valid_at=75.0, known_at=200.0)
    visible = SearchFilter(workspace_id=wid, valid_at=75.0, known_at=301.0)

    assert store.edges_in_scope(invisible) == []
    assert store.neighbors(["a"], flt=invisible) == []
    assert [edge.id for edge in store.edges_in_scope(visible)] == [edge_id]


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


def test_graph_neighbors_apply_workspace_and_repo_scope(store):
    w1 = store.get_or_create_workspace("w1")
    w2 = store.get_or_create_workspace("w2")
    r1 = store.get_or_create_repo(w1, "r")
    r2 = store.get_or_create_repo(w2, "r")
    store.upsert_edge(Edge(
        id="", src="shared", dst="visible", relation="uses",
        workspace_id=w1, repo_id=r1,
    ))
    store.upsert_edge(Edge(
        id="", src="shared", dst="leaked", relation="uses",
        workspace_id=w2, repo_id=r2,
    ))

    rows = store.neighbors(
        ["shared"],
        flt=SearchFilter(workspace_id=w1, repo_id=r1),
    )

    assert [(edge.src, edge.dst) for edge in rows] == [("shared", "visible")]


def test_memory_links_honor_known_at_empty_layers_and_large_id_sets(
        store, monkeypatch):
    from engraphis.core import store as store_mod

    ids = [f"mem_{index:04d}" for index in range(600)]
    store.add_link(ids[0], ids[-1], relation="causes", layer=GraphLayer.CAUSAL)
    store.conn.execute(
        "UPDATE mem_links SET created_at=100, valid_from=100, ingested_at=100"
    )
    store.conn.commit()
    monkeypatch.setattr(store_mod, "IN_CLAUSE_CHUNK", 50)

    assert store.links_among(
        ids, flt=SearchFilter(known_at=99.0)
    ) == []
    visible = store.links_among(
        ids, flt=SearchFilter(known_at=100.0)
    )
    assert [(row["a"], row["b"]) for row in visible] == [(ids[0], ids[-1])]
    assert store.links_among(ids, layers=[]) == []


def test_closed_memory_link_can_be_reactivated_without_erasing_history(store):
    store.add_link(
        "mem_a", "mem_b", relation="related",
        valid_from=10.0, valid_to=20.0, valid_to_recorded_at=20.0,
        ingested_at=10.0,
    )
    assert not store.has_link("mem_a", "mem_b", relation="related")

    store.add_link(
        "mem_b", "mem_a", relation="related",
        valid_from=40.0, ingested_at=40.0,
    )
    # Replaying the same current relation is still idempotent.
    store.add_link(
        "mem_a", "mem_b", relation="related",
        valid_from=50.0, ingested_at=50.0,
    )

    rows = store.conn.execute(
        "SELECT valid_from, valid_to, expired_at FROM mem_links "
        "WHERE relation='related' ORDER BY valid_from"
    ).fetchall()
    assert [(row["valid_from"], row["valid_to"]) for row in rows] == [
        (10.0, 20.0), (40.0, None),
    ]
    assert store.has_link("mem_a", "mem_b", relation="related")
    historical = store.links_among(
        ["mem_a", "mem_b"],
        flt=SearchFilter(valid_at=15.0, known_at=50.0),
    )
    current = store.links_among(
        ["mem_a", "mem_b"],
        flt=SearchFilter(valid_at=50.0, known_at=50.0),
    )
    assert [row["valid_from"] for row in historical] == [10.0]
    assert [row["valid_from"] for row in current] == [40.0]


def test_expired_memory_link_does_not_block_reactivation(store):
    store.add_link(
        "mem_a", "mem_b", relation="related",
        valid_from=10.0, ingested_at=10.0, expired_at=20.0,
    )
    assert not store.has_link("mem_a", "mem_b", relation="related")

    store.add_link(
        "mem_a", "mem_b", relation="related",
        valid_from=40.0, ingested_at=40.0,
    )

    rows = store.conn.execute(
        "SELECT valid_from, expired_at FROM mem_links ORDER BY valid_from"
    ).fetchall()
    assert [(row["valid_from"], row["expired_at"]) for row in rows] == [
        (10.0, 20.0), (40.0, None),
    ]
    assert [row["valid_from"] for row in store.links_among(
        ["mem_a", "mem_b"],
        flt=SearchFilter(valid_at=15.0, known_at=15.0),
    )] == [10.0]
    assert [row["valid_from"] for row in store.links_among(
        ["mem_a", "mem_b"],
        flt=SearchFilter(valid_at=50.0, known_at=50.0),
    )] == [40.0]


def test_memory_link_metadata_change_versions_system_time_without_rewriting_history(
        store, monkeypatch):
    from engraphis.core import store as store_mod

    store.add_link(
        "mem_a", "mem_b", relation="related", layer=GraphLayer.SEMANTIC,
        reason="old evidence", valid_from=10.0, ingested_at=10.0,
    )
    monkeypatch.setattr(store_mod, "now_ts", lambda: 30.0)

    store.add_link(
        "mem_b", "mem_a", relation="related", layer=GraphLayer.CAUSAL,
        reason="new evidence",
    )
    # Replaying the converged metadata is idempotent, not another history row.
    store.add_link(
        "mem_a", "mem_b", relation="related", layer=GraphLayer.CAUSAL,
        reason="new evidence",
    )

    rows = store.conn.execute(
        "SELECT layer, reason, valid_from, ingested_at, expired_at "
        "FROM mem_links ORDER BY ingested_at"
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("semantic", "old evidence", 10.0, 10.0, 30.0),
        ("causal", "new evidence", 10.0, 30.0, None),
    ]

    past = SearchFilter(valid_at=20.0, known_at=20.0)
    current = SearchFilter(valid_at=20.0, known_at=30.0)
    assert [(row["layer"], row["reason"]) for row in store.get_links(
        "mem_a", flt=past,
    )] == [("semantic", "old evidence")]
    assert [(row["layer"], row["reason"]) for row in store.get_links(
        "mem_a", flt=current,
    )] == [("causal", "new evidence")]
    assert store.links_among(
        ["mem_a", "mem_b"], layers=[GraphLayer.CAUSAL], flt=past,
    ) == []
    assert store.links_among(
        ["mem_a", "mem_b"], layers=[GraphLayer.SEMANTIC], flt=current,
    ) == []
    assert store.has_link("mem_a", "mem_b", relation="related")


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
    assert len(store.list_code_edges(
        "repo_x", layers=[GraphLayer.ENTITY], limit=3
    )) == 3
    assert store.list_code_edges(
        "repo_x", layers=[GraphLayer.CAUSAL], limit=3
    ) == []
    assert store.list_code_edges("repo_x", layers=[], limit=3) == []
    # list_code_files was the one sibling with no SQL limit, so engine.export_code_graph
    # had to fetch the whole repo and slice it in Python.
    for i in range(5):
        store.upsert_code_file(repo_id="repo_x", file=f"m{i}.py", lang="python",
                               content_hash=f"h{i}", size_bytes=1, mtime_ns=i,
                               backend="regex")
    assert len(store.list_code_files("repo_x")) == 5            # default: unbounded
    assert len(store.list_code_files("repo_x", limit=2)) == 2
    assert store.list_code_files("repo_x", limit=0) == []       # never SQLite's -1
    # the limit composes with the language filter rather than replacing it
    assert len(store.list_code_files("repo_x", languages={"python"}, limit=3)) == 3
    assert store.list_code_files("repo_x", languages={"rust"}, limit=3) == []


def test_code_edge_endpoint_filter_applies_before_limit(store):
    for index in range(4):
        store.add_code_edge(
            repo_id="repo_x", src=f"noise_{index}", dst=f"other_{index}",
            relation="calls", file="a_noise.py", line=index,
        )
    store.add_code_edge(
        repo_id="repo_x", src="target", dst="caller", relation="calls",
        file="z_target.py", line=1,
    )

    edges = store.list_code_edges("repo_x", endpoints=["target"], limit=1)

    assert [(edge["src"], edge["dst"]) for edge in edges] == [("target", "caller")]


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


def test_zero_temporal_anchors_round_trip_without_becoming_present_time(store):
    wid = store.get_or_create_workspace("w")
    mid = store.add_memory(MemoryRecord(
        id="", content="known at the epoch", workspace_id=wid,
        valid_from=0.0, ingested_at=0.0, last_access=0.0,
    ))
    edge_id = store.upsert_edge(Edge(
        id="", src="a", dst="b", relation="uses", workspace_id=wid,
        valid_from=0.0, ingested_at=0.0,
    ))

    record = store.get_memory(mid)
    edge = store.conn.execute(
        "SELECT valid_from, ingested_at FROM edges WHERE id=?", (edge_id,)
    ).fetchone()
    assert record.valid_from == record.ingested_at == record.last_access == 0.0
    assert edge["valid_from"] == edge["ingested_at"] == 0.0


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


def test_code_history_closes_live_rows_and_supports_time_travel(store):
    """Re-indexing retires old code evidence without deleting its history."""
    wid = store.get_or_create_workspace("w")
    rid = store.get_or_create_repo(wid, "r")
    mid = store.add_memory(MemoryRecord(
        id="", content="The old implementation called helper.",
        workspace_id=wid, repo_id=rid, scope=Scope.REPO,
        provenance={"source": "test", "trusted": True, "review_state": "approved"},
    ))
    symbol_id = store.upsert_symbol(
        repo_id=rid, kind="function", name="old", fqname="old",
        file="calc.py", span="1-1",
    )
    store.add_code_edge(repo_id=rid, src="old", dst="helper", relation="calls",
                        file="calc.py", line=1)
    store.link_memory_symbol(repo_id=rid, symbol_id=symbol_id, memory_id=mid)
    # Use a stable world/system-time interval rather than relying on sub-millisecond
    # spacing between indexing and retirement on fast CI hosts.
    store.conn.execute(
        "UPDATE symbols SET valid_from=10, ingested_at=10 WHERE id=?", (symbol_id,)
    )
    store.conn.execute(
        "UPDATE memories SET valid_from=10, ingested_at=10 WHERE id=?", (mid,)
    )
    store.conn.execute(
        "UPDATE code_edges SET valid_from=10, ingested_at=10 WHERE repo_id=?", (rid,)
    )
    store.conn.execute(
        "UPDATE code_memory_links SET valid_from=10, ingested_at=10 WHERE repo_id=?", (rid,)
    )
    store.conn.commit()
    store.clear_symbols_for_file(rid, "calc.py")

    closed_at = store.conn.execute(
        "SELECT valid_to FROM symbols WHERE id=?", (symbol_id,)
    ).fetchone()["valid_to"]
    assert store.list_symbols(rid) == []
    assert store.list_code_edges(rid) == []
    assert store.list_code_memory_links(rid) == []

    history = SearchFilter(valid_at=11.0,
                           known_at=float(closed_at) + 1.0)
    assert [row["id"] for row in store.list_symbols(rid, flt=history)] == [symbol_id]
    assert [row["id"] for row in store.search_symbols(rid, "old", flt=history)] == [
        symbol_id
    ]
    assert len(store.list_code_edges(rid, flt=history)) == 1
    assert len(store.get_symbol_callers(rid, "helper", flt=history)) == 1
    assert len(store.list_code_memory_links(rid, flt=history)) == 1
    assert [row["id"] for row in store.symbols_for_memory(
        rid, mid, flt=history
    )] == [symbol_id]


def test_code_memory_link_listing_requires_visible_symbol_and_memory(store):
    wid = store.get_or_create_workspace("w")
    rid = store.get_or_create_repo(wid, "r")
    mid = store.add_memory(MemoryRecord(
        id="", content="deploy", workspace_id=wid, repo_id=rid, scope=Scope.REPO,
        provenance={"source": "test", "trusted": True, "review_state": "approved"},
    ))
    symbol_id = store.upsert_symbol(
        repo_id=rid, kind="function", name="deploy", fqname="deploy",
        file="deploy.py", span="1-1",
    )
    store.link_memory_symbol(
        repo_id=rid, symbol_id=symbol_id, memory_id=mid,
    )
    assert len(store.list_code_memory_links(rid)) == 1

    # Simulate a legacy/direct writer that retired the symbol but forgot to
    # retire its bridge. The read must still fail closed.
    store.conn.execute(
        "UPDATE symbols SET valid_to=0 WHERE id=?", (symbol_id,)
    )
    store.conn.commit()

    assert store.list_code_memory_links(rid) == []


def test_code_memory_link_limit_excludes_pending_rows_before_capping(store):
    wid = store.get_or_create_workspace("w")
    rid = store.get_or_create_repo(wid, "r")
    symbol_id = store.upsert_symbol(
        repo_id=rid, kind="function", name="deploy", fqname="deploy",
        file="deploy.py", span="1-1",
    )
    pending = store.add_memory(MemoryRecord(
        id="", content="Pending deploy note.", workspace_id=wid, repo_id=rid,
        scope=Scope.REPO,
        provenance={"source": "import", "trusted": False, "review_state": "pending"},
    ))
    approved = store.add_memory(MemoryRecord(
        id="", content="Approved deploy note.", workspace_id=wid, repo_id=rid,
        scope=Scope.REPO,
        provenance={"source": "human_review", "trusted": True, "review_state": "approved"},
    ))
    store.link_memory_symbol(repo_id=rid, symbol_id=symbol_id, memory_id=pending)
    store.link_memory_symbol(repo_id=rid, symbol_id=symbol_id, memory_id=approved)

    rows = store.list_code_memory_links(rid, limit=1)

    assert [row["memory_id"] for row in rows] == [approved]


def test_memories_mentioning_limit_excludes_pending_rows_before_capping(store):
    wid = store.get_or_create_workspace("w")
    rid = store.get_or_create_repo(wid, "r")
    approved = store.add_memory(MemoryRecord(
        id="", content="Approved deployment guidance.", workspace_id=wid, repo_id=rid,
        scope=Scope.REPO, ingested_at=1.0,
        provenance={"source": "human_review", "trusted": True, "review_state": "approved"},
    ))
    for stamp in range(2, 13):
        store.add_memory(MemoryRecord(
            id="", content="Pending deployment guidance.", workspace_id=wid, repo_id=rid,
            scope=Scope.REPO, ingested_at=float(stamp),
            provenance={"source": "import", "trusted": False, "review_state": "pending"},
        ))

    rows = store.memories_mentioning(rid, "deployment", limit=1)

    assert [row["id"] for row in rows] == [approved]


def test_memory_entity_incidence_is_scoped_and_temporal(store):
    wid = store.get_or_create_workspace("w")
    rid = store.get_or_create_repo(wid, "r")
    mid = store.add_memory(MemoryRecord(
        id="", content="Alice owns the deployment.", workspace_id=wid,
        repo_id=rid, scope=Scope.REPO,
    ))
    entity_id = store.upsert_entity(Node(
        id="", name="Alice", ntype="person", workspace_id=wid, repo_id=rid,
    ))
    store.link_memory_entity(
        memory_id=mid, entity_id=entity_id, workspace_id=wid, repo_id=rid,
        source_kind="text_mention", confidence=0.8,
    )
    rows = store.list_memory_entities(SearchFilter(workspace_id=wid, repo_id=rid))
    assert [(row["memory_id"], row["entity_id"], row["source_kind"])
            for row in rows] == [(mid, entity_id, "text_mention")]


def test_prompt_memory_entity_limit_excludes_pending_rows_before_capping(store):
    wid = store.get_or_create_workspace("w")
    rid = store.get_or_create_repo(wid, "r")
    entity_id = store.upsert_entity(Node(
        id="", name="Deploy", ntype="service", workspace_id=wid, repo_id=rid,
    ))
    pending = store.add_memory(MemoryRecord(
        id="", content="Pending deployment note.", workspace_id=wid, repo_id=rid,
        scope=Scope.REPO,
        provenance={"source": "import", "trusted": False, "review_state": "pending"},
    ))
    approved = store.add_memory(MemoryRecord(
        id="", content="Approved deployment note.", workspace_id=wid, repo_id=rid,
        scope=Scope.REPO,
        provenance={"source": "test", "trusted": True, "review_state": "approved"},
    ))
    store.link_memory_entity(
        memory_id=pending, entity_id=entity_id, workspace_id=wid, repo_id=rid,
        source_kind="test", confidence=1.0,
    )
    store.link_memory_entity(
        memory_id=approved, entity_id=entity_id, workspace_id=wid, repo_id=rid,
        source_kind="test", confidence=0.5,
    )

    rows = store.list_memory_entities(
        SearchFilter(workspace_id=wid, repo_id=rid), entity_ids=[entity_id],
        prompt_only=True, limit=1,
    )

    assert [row["memory_id"] for row in rows] == [approved]


def test_memory_entity_lookup_chunks_large_memory_id_filters(store, monkeypatch):
    from engraphis.core import store as store_mod

    wid = store.get_or_create_workspace("w")
    entity_id = store.upsert_entity(Node(
        id="", name="Alice", ntype="person", workspace_id=wid,
    ))
    memory_ids = [
        store.add_memory(MemoryRecord(id="", content=f"Memory {index}", workspace_id=wid))
        for index in range(5)
    ]
    for memory_id in memory_ids:
        store.link_memory_entity(
            memory_id=memory_id, entity_id=entity_id, workspace_id=wid, repo_id=None,
            source_kind="test", confidence=1.0,
        )
    monkeypatch.setattr(store_mod, "IN_CLAUSE_CHUNK", 2)

    rows = store.list_memory_entities(
        SearchFilter(workspace_id=wid), memory_ids=memory_ids,
    )

    assert {row["memory_id"] for row in rows} == set(memory_ids)


def test_memory_entity_incidence_keeps_valid_and_known_coordinates_paired(store):
    wid = store.get_or_create_workspace("w")
    rid = store.get_or_create_repo(wid, "r")
    mid = store.add_memory(MemoryRecord(
        id="", content="The deployment has an assigned owner.", workspace_id=wid,
        repo_id=rid, scope=Scope.REPO, valid_from=0.0, ingested_at=0.0,
    ))
    entity_id = store.upsert_entity(Node(
        id="", name="Alice", ntype="person", workspace_id=wid, repo_id=rid,
    ))
    first = store.link_memory_entity(
        memory_id=mid, entity_id=entity_id, workspace_id=wid, repo_id=rid,
        source_kind="edge_support", valid_from=100.0, ingested_at=100.0,
    )
    second = store.link_memory_entity(
        memory_id=mid, entity_id=entity_id, workspace_id=wid, repo_id=rid,
        source_kind="edge_support", valid_from=50.0, ingested_at=300.0,
    )

    assert first != second
    rows = store.conn.execute(
        "SELECT id, valid_from, ingested_at, expired_at FROM memory_entities "
        "WHERE memory_id=? ORDER BY ingested_at",
        (mid,),
    ).fetchall()
    assert [
        (row["valid_from"], row["ingested_at"], row["expired_at"])
        for row in rows
    ] == [(100.0, 100.0, 300.0), (50.0, 300.0, None)]
    assert store.list_memory_entities(SearchFilter(
        workspace_id=wid, repo_id=rid, valid_at=75.0, known_at=200.0,
    )) == []
    visible = store.list_memory_entities(SearchFilter(
        workspace_id=wid, repo_id=rid, valid_at=75.0, known_at=350.0,
    ))
    assert [row["id"] for row in visible] == [second]


def test_explicit_semantic_code_edge_preserves_its_layer(store):
    store.add_code_edge(
        repo_id="repo_x", src="deploy", dst="release", relation="related_to",
        layer=GraphLayer.SEMANTIC,
    )
    assert store.list_code_edges("repo_x")[0]["layer"] == GraphLayer.SEMANTIC.value


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


def test_prompt_neighbors_filter_unapproved_edges_before_limit(store):
    wid = store.get_or_create_workspace("w")
    for index in range(4):
        memory_id = store.add_memory(MemoryRecord(
            id=f"mem_pending_{index}", content=f"pending {index}", workspace_id=wid,
            provenance={"source": "test", "trusted": True, "review_state": "pending"},
        ))
        store.upsert_edge(Edge(
            id=f"edg_pending_{index}", src="seed", dst=f"pending_{index}",
            relation="uses", workspace_id=wid, provenance={"memory_id": memory_id},
        ))
    approved_id = store.add_memory(MemoryRecord(
        id="mem_approved", content="approved", workspace_id=wid,
        provenance={"source": "test", "trusted": True, "review_state": "approved"},
    ))
    store.upsert_edge(Edge(
        id="edg_approved", src="seed", dst="approved", relation="uses", workspace_id=wid,
        provenance={"memory_id": approved_id},
    ))

    edges = store.neighbors(["seed"], limit=1, prompt_only=True)
    assert [edge.id for edge in edges] == ["edg_approved"]


def test_prompt_links_touching_filters_unapproved_endpoints_before_limit(store):
    wid = store.get_or_create_workspace("w")
    seed = store.add_memory(MemoryRecord(
        id="mem_seed", content="approved seed", workspace_id=wid,
        provenance={"source": "test", "trusted": True, "review_state": "approved"},
    ))
    pending = store.add_memory(MemoryRecord(
        id="mem_pending", content="pending endpoint", workspace_id=wid,
        provenance={"source": "test", "trusted": False, "review_state": "pending"},
    ))
    approved = store.add_memory(MemoryRecord(
        id="mem_safe", content="approved endpoint", workspace_id=wid,
        provenance={"source": "test", "trusted": True, "review_state": "approved"},
    ))
    store.add_link(seed, pending, relation="supports")
    store.add_link(seed, approved, relation="supports")

    links = store.links_touching([seed], limit=1, prompt_only=True)
    assert [(link["a"], link["b"]) for link in links] == [(seed, approved)]


@pytest.mark.parametrize(
    ("query", "broad_match", "exact_match"),
    [
        ("C++", "C language guide", "C++ compiler guide"),
        ("v1.2", "v1 migration notes", "v1.2 compatibility notes"),
    ],
)
def test_fts_fallback_prioritizes_literal_punctuation_before_token_variants(
        store, query, broad_match, exact_match):
    wid = store.get_or_create_workspace("w")
    store.add_memory(MemoryRecord(id="mem_broad", content=broad_match, workspace_id=wid))
    store.add_memory(MemoryRecord(id="mem_exact", content=exact_match, workspace_id=wid))
    store.has_fts5 = False

    assert store.fts_search(query, 1) == [("mem_exact", 0.5)]


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
