import gc
import json
import math
import sqlite3
import threading
import weakref

import pytest

from engraphis.core.interfaces import (
    Edge,
    GraphLayer,
    GraphReader,
    GraphWriter,
    MemoryRecord,
    MemoryType,
    Node,
    Scope,
    SearchFilter,
    format_modified_hlc,
    parse_modified_hlc,
)
from engraphis.core import scoring
from engraphis.core.retention_policy import MAX_STABILITY_DAYS, reinforced_stability
from engraphis.core.schema import SCHEMA_VERSION
from engraphis.core.store import Store, memory_matches_filter, normalize_entity_name


@pytest.fixture()
def store():
    s = Store(":memory:")
    yield s
    s.close()


def test_append_event_does_not_commit_a_caller_owned_transaction(store):
    workspace_id = store.get_or_create_workspace("events")
    store.conn.execute(
        "INSERT INTO audit(id, ts, actor, action, target, detail) VALUES (?,?,?,?,?,?)",
        ("aud_pending", 1.0, "test", "pending", "target", "detail"),
    )
    assert store.conn.transaction_owned_by_current_thread()

    event_id = store.append_event(
        kind="test", content="event", workspace_id=workspace_id,
    )

    assert store.conn.transaction_owned_by_current_thread()
    store.conn.rollback()
    assert store.conn.execute("SELECT 1 FROM events WHERE id=?", (event_id,)).fetchone() is None
    assert store.conn.execute("SELECT 1 FROM audit WHERE id='aud_pending'").fetchone() is None


def test_schema_version(store):
    assert store.schema_version == SCHEMA_VERSION


def test_temporal_mutations_reject_inverted_intervals(store):
    workspace_id = store.get_or_create_workspace("intervals")
    memory_id = store.add_memory(MemoryRecord(
        id="", content="future fact", workspace_id=workspace_id,
        scope=Scope.WORKSPACE, valid_from=100.0,
    ))
    with pytest.raises(ValueError, match="valid_to cannot predate"):
        store.close_validity(memory_id, at=99.0)

    with pytest.raises(ValueError, match="link valid_to cannot predate"):
        store.add_link_version(
            memory_id, "mem_other", valid_from=100.0, valid_to=99.0
        )

    with pytest.raises(ValueError, match="edge valid_to cannot predate"):
        store.upsert_edge(Edge(
            id="", workspace_id=workspace_id, src="ent_a", dst="ent_b",
            relation="related", valid_from=100.0, valid_to=99.0,
        ))


class _TrackedConnection(sqlite3.Connection):
    close_calls = 0

    def close(self):
        self.close_calls += 1
        return super().close()


def _tracked_store():
    opened = []

    def connect(_path):
        connection = sqlite3.connect(
            ":memory:", check_same_thread=False, factory=_TrackedConnection
        )
        connection.row_factory = sqlite3.Row
        opened.append(connection)
        return connection

    return Store(":memory:", connect=connect), opened


def test_store_close_is_idempotent_and_context_managed():
    store, opened = _tracked_store()

    with store:
        assert store.schema_version == SCHEMA_VERSION

    store.close()
    assert opened[0].close_calls == 1


def test_store_finalizer_closes_an_abandoned_connection():
    store, opened = _tracked_store()
    store_ref = weakref.ref(store)

    del store
    gc.collect()

    assert store_ref() is None
    assert opened[0].close_calls == 1


def test_store_close_is_atomic_across_threads():
    entered = threading.Event()
    release = threading.Event()
    opened = []
    errors = []

    class BlockingCloseConnection(_TrackedConnection):
        def close(self):
            self.close_calls += 1
            entered.set()
            assert release.wait(timeout=5)
            return sqlite3.Connection.close(self)

    def connect(_path):
        connection = sqlite3.connect(
            ":memory:", check_same_thread=False, factory=BlockingCloseConnection
        )
        connection.row_factory = sqlite3.Row
        opened.append(connection)
        return connection

    store = Store(":memory:", connect=connect)

    def close_store():
        try:
            store.close()
        except BaseException as exc:
            errors.append(exc)

    first = threading.Thread(target=close_store)
    second = threading.Thread(target=close_store)
    first.start()
    assert entered.wait(timeout=5)
    second.start()
    assert second.is_alive()
    release.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not first.is_alive()
    assert not second.is_alive()
    assert not errors
    assert opened[0].close_calls == 1


def test_store_closes_immediately_when_first_connection_setup_fails():
    opened = []

    class FailingSetupConnection(_TrackedConnection):
        def execute(self, sql, *args, **kwargs):
            if str(sql).strip().casefold() == "pragma foreign_keys=on":
                raise RuntimeError("foreign-key setup unavailable")
            return super().execute(sql, *args, **kwargs)

    def connect(_path):
        connection = sqlite3.connect(
            ":memory:", check_same_thread=False, factory=FailingSetupConnection
        )
        connection.row_factory = sqlite3.Row
        opened.append(connection)
        return connection

    with pytest.raises(RuntimeError, match="foreign-key setup unavailable"):
        Store(":memory:", connect=connect)

    assert opened[0].close_calls == 1


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


def test_close_validity_rolls_back_fact_and_audit_on_graph_failure(store, monkeypatch):
    wid = store.get_or_create_workspace("close-rollback")
    memory_id = store.add_memory(MemoryRecord(
        id="mem_close_rollback",
        content="live",
        workspace_id=wid,
    ))
    created = store.get_memory(memory_id)
    assert created is not None and created.valid_from is not None
    audit_before = store.conn.execute(
        "SELECT COUNT(*) FROM audit WHERE target=?", (memory_id,)
    ).fetchone()[0]

    def fail_graph_retirement(*args, **kwargs):
        raise RuntimeError("graph retirement unavailable")

    monkeypatch.setattr(
        store, "invalidate_edges_for_memory", fail_graph_retirement
    )
    with pytest.raises(RuntimeError, match="graph retirement unavailable"):
        store.close_validity(memory_id, at=created.valid_from + 1.0)

    record = store.get_memory(memory_id)
    assert record is not None and record.valid_to is None
    assert store.conn.execute(
        "SELECT COUNT(*) FROM audit WHERE target=?", (memory_id,)
    ).fetchone()[0] == audit_before
    assert store.conn.in_transaction is False


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

def _add_link_test_memories(store, *memory_ids):
    wid = store.get_or_create_workspace("memory-link-tests")
    for memory_id in memory_ids:
        store.add_memory(MemoryRecord(
            id=memory_id,
            content=f"endpoint {memory_id}",
            workspace_id=wid,
            scope=Scope.WORKSPACE,
            valid_from=1.0,
            ingested_at=1.0,
        ), commit=False)
    store.conn.commit()


@pytest.mark.parametrize("method_name", ("add_link", "add_link_version"))
def test_link_writes_release_transaction_after_waiting_for_other_thread(
    store, monkeypatch, method_name,
):
    _add_link_test_memories(store, "link-a", "link-b")
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


@pytest.mark.parametrize("method_name", ("add_link", "add_link_version"))
def test_link_writes_preserve_caller_owned_transaction(store, method_name):
    _add_link_test_memories(store, "link-outer-a", "link-outer-b")
    store.conn.execute("BEGIN IMMEDIATE")

    with pytest.raises(ValueError, match="endpoints must exist"):
        getattr(store, method_name)(
            "link-outer-a", "link-missing", relation="related"
        )
    assert store.conn.in_transaction

    getattr(store, method_name)(
        "link-outer-a", "link-outer-b", relation="related"
    )
    assert store.conn.in_transaction
    assert store.has_link("link-outer-a", "link-outer-b")
    store.conn.rollback()
    assert not store.has_link("link-outer-a", "link-outer-b")


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


def test_failed_deferred_commit_retains_transaction_ownership_until_rollback(tmp_path):
    store = Store(str(tmp_path / "deferred-commit.db"))
    conn = store.conn
    conn.execute("CREATE TABLE deferred_parent(id INTEGER PRIMARY KEY)")
    conn.execute(
        "CREATE TABLE deferred_child(parent_id INTEGER REFERENCES deferred_parent(id) "
        "DEFERRABLE INITIALLY DEFERRED)"
    )
    conn.commit()
    conn.execute("BEGIN")
    conn.execute("INSERT INTO deferred_child(parent_id) VALUES (1)")

    with pytest.raises(sqlite3.IntegrityError):
        conn.commit()

    assert conn.in_transaction is True
    assert conn.transaction_owned_by_current_thread() is True

    started = threading.Event()
    finished = threading.Event()
    rows = []
    errors = []

    def wait_for_connection():
        started.set()
        try:
            rows.append(conn.execute("SELECT 1").fetchone()[0])
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()

    waiter = threading.Thread(target=wait_for_connection)
    waiter.start()
    assert started.wait(timeout=5)
    assert not finished.wait(timeout=0.05)

    conn.rollback()
    waiter.join(timeout=5)

    assert not waiter.is_alive()
    assert not errors
    assert rows == [1]
    assert conn.in_transaction is False
    store.close()


def test_query_cursor_is_materialized_before_the_connection_lock_is_released(tmp_path):
    store = Store(str(tmp_path / "query-snapshot.db"))
    conn = store.conn
    conn.execute("CREATE TABLE snapshot_rows(value INTEGER NOT NULL)")
    conn.executemany("INSERT INTO snapshot_rows(value) VALUES (?)", [(0,), (1,)])
    conn.commit()

    reader_entered = threading.Event()
    release_reader = threading.Event()
    writer_finished = threading.Event()
    reader_rows = []
    errors = []

    def gate(value):
        if value == 1:
            reader_entered.set()
            assert release_reader.wait(timeout=5)
        return value

    conn.create_function("gate_snapshot", 1, gate)

    def read_rows():
        try:
            reader_rows.extend(
                row[0] for row in conn.execute(
                    "SELECT gate_snapshot(value) FROM snapshot_rows ORDER BY value"
                )
            )
        except BaseException as exc:
            errors.append(exc)

    def write_row():
        try:
            conn.execute("INSERT INTO snapshot_rows(value) VALUES (2)")
            conn.commit()
        except BaseException as exc:
            errors.append(exc)
        finally:
            writer_finished.set()

    reader = threading.Thread(target=read_rows)
    writer = threading.Thread(target=write_row)
    reader.start()
    assert reader_entered.wait(timeout=5)
    writer.start()
    assert not writer_finished.wait(timeout=0.05)
    release_reader.set()
    reader.join(timeout=5)
    writer.join(timeout=5)

    assert not reader.is_alive()
    assert not writer.is_alive()
    assert not errors
    assert reader_rows == [0, 1]
    assert [row[0] for row in conn.execute(
        "SELECT value FROM snapshot_rows ORDER BY value"
    )] == [0, 1, 2]
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
    assert migrated.schema_version == SCHEMA_VERSION
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


def test_empty_scope_and_type_filters_match_nothing(store):
    wid = store.get_or_create_workspace("w")
    mid = store.add_memory(MemoryRecord(
        id="", content="scoped fact", workspace_id=wid,
        scope=Scope.WORKSPACE, mtype=MemoryType.SEMANTIC,
    ))
    record = store.get_memory(mid)
    assert record is not None

    # ``None`` means the caller omitted the filter; an explicit empty allow-list
    # must not widen a read to every scope/type.
    assert store.list_memories(SearchFilter(workspace_id=wid, scopes=[])) == []
    assert store.list_memories(SearchFilter(workspace_id=wid, mtypes=[])) == []
    assert not memory_matches_filter(record, SearchFilter(workspace_id=wid, scopes=[]))
    assert not memory_matches_filter(record, SearchFilter(workspace_id=wid, mtypes=[]))


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
    _add_link_test_memories(store, *ids)
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
    _add_link_test_memories(store, "mem_a", "mem_b")
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
    _add_link_test_memories(store, "mem_a", "mem_b")
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
    _add_link_test_memories(store, "mem_a", "mem_b")

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


def test_reinforce_is_finite_bounded_and_logarithmic(store):
    wid = store.get_or_create_workspace("w")
    rid = store.get_or_create_repo(wid, "r")
    mid = store.add_memory(MemoryRecord(id="", content="reinforce me", workspace_id=wid, repo_id=rid))
    for _ in range(1000):
        store.reinforce(mid, boost=scoring.INTERACTION_BOOST["recall"])
    after = store.get_memory(mid)
    assert after.access_count == 1000
    assert after.stability == pytest.approx(1.0 + 0.45 * math.log1p(1000))
    assert math.isfinite(after.stability)
    assert after.stability <= MAX_STABILITY_DAYS


def test_add_memory_canonicalizes_retention_state(store):
    from engraphis.core.retention_policy import MAX_ACCESS_COUNT

    wid = store.get_or_create_workspace("w")
    mid = store.add_memory(MemoryRecord(
        id="", content="bounded state", workspace_id=wid,
        stability=float("inf"), access_count=MAX_ACCESS_COUNT + 5,
    ))

    stored = store.get_memory(mid)
    assert stored.stability == 1.0
    assert stored.access_count == MAX_ACCESS_COUNT


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


def test_edge_supports_scoped_lookup_drives_from_requested_edge_ids(store, monkeypatch):
    """Large scene evidence batches must not rescan the workspace per IN chunk."""
    from engraphis.core import store as store_mod

    workspace_id = store.get_or_create_workspace("support-lookup")
    other_workspace = store.get_or_create_workspace("other-support-lookup")
    for index in range(5):
        store.upsert_edge(Edge(
            id=f"edge_{index}", src=f"source_{index}", dst=f"target_{index}",
            relation="related", workspace_id=workspace_id,
            provenance={"memory_id": f"mem_{index}"},
        ))
    store.upsert_edge(Edge(
        id="other_edge", src="other_source", dst="other_target",
        relation="related", workspace_id=other_workspace,
        provenance={"memory_id": "mem_other"},
    ))
    monkeypatch.setattr(store_mod, "IN_CLAUSE_CHUNK", 2)
    calls: list[str] = []
    original = store_mod._SerializedConnection.execute

    def capture_execute(connection, statement, *args, **kwargs):
        if "FROM edge_supports s" in statement:
            calls.append(statement)
        return original(connection, statement, *args, **kwargs)

    monkeypatch.setattr(store_mod._SerializedConnection, "execute", capture_execute)

    supports = store.edge_supports_in_scope(
        ["edge_0", "edge_1", "edge_2", "edge_3", "edge_4", "other_edge"],
        flt=SearchFilter(workspace_id=workspace_id),
    )

    assert [row["edge_id"] for row in supports] == [
        "edge_0", "edge_1", "edge_2", "edge_3", "edge_4",
    ]
    assert len(calls) == 3
    assert all("CROSS JOIN edges e ON e.id=s.edge_id" in statement for statement in calls)


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


def test_prompt_neighbors_reject_explicitly_untrusted_direct_edges(store):
    wid = store.get_or_create_workspace("w")
    cases = {
        "edg_legacy": {},
        "edg_approved": {"trusted": True, "review_state": "approved"},
        "edg_untrusted": {"trusted": False},
        "edg_pending": {"trusted": True, "review_state": "pending"},
        "edg_quarantined": {"quarantined": True},
        "edg_nested_quarantine": {"quarantine": {"state": "quarantined"}},
    }
    for edge_id, provenance in cases.items():
        store.upsert_edge(Edge(
            id=edge_id,
            src="seed",
            dst=edge_id,
            relation="uses",
            workspace_id=wid,
            provenance=provenance,
        ))

    edges = store.neighbors(["seed"], prompt_only=True)

    assert {edge.id for edge in edges} == {"edg_legacy", "edg_approved"}


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


# ── owner-01 persistence hardening regressions ───────────────────────────────

BAD_TEMPORAL_VALUES = [
    pytest.param(float("nan"), id="nan"),
    pytest.param(float("inf"), id="positive-infinity"),
    pytest.param(float("-inf"), id="negative-infinity"),
    pytest.param(True, id="boolean"),
    pytest.param("not-a-time", id="text"),
    pytest.param(10**10000, id="overflowing-integer"),
]


@pytest.mark.parametrize("bad_value", BAD_TEMPORAL_VALUES)
def test_record_temporal_fields_reject_non_finite_values(bad_value):
    for field in (
        "last_access", "valid_from", "valid_to", "ingested_at", "expired_at",
        "valid_to_recorded_at", "pinned_at", "unpinned_at",
    ):
        with pytest.raises(ValueError, match="finite timestamp"):
            MemoryRecord(id="", content="invalid time", **{field: bad_value})


@pytest.mark.parametrize("bad_value", BAD_TEMPORAL_VALUES)
def test_edge_temporal_fields_and_weight_reject_non_finite_values(bad_value):
    for field in (
        "valid_from", "valid_to", "ingested_at", "expired_at",
        "valid_to_recorded_at",
    ):
        with pytest.raises(ValueError, match="finite timestamp"):
            Edge(id="", src="a", dst="b", relation="related", **{field: bad_value})
    with pytest.raises(ValueError, match="finite number"):
        Edge(id="", src="a", dst="b", relation="related", weight=bad_value)


@pytest.mark.parametrize("bad_value", BAD_TEMPORAL_VALUES)
@pytest.mark.parametrize(
    "operation",
    [
        "add_memory",
        "upsert_edge",
        "add_edge_support",
        "close_validity",
        "invalidate_edge",
        "invalidate_edges_for_memory",
        "retire_memory_graph_state",
        "add_link",
        "add_link_version",
        "link_memory_entity",
        "add_memory_tombstone",
    ],
)
def test_temporal_mutators_reject_invalid_values_before_persisting(
        store, operation, bad_value):
    wid = store.get_or_create_workspace("temporal-domain")
    first = store.add_memory(MemoryRecord(
        id="mem_time_a", content="a", workspace_id=wid, scope=Scope.WORKSPACE,
    ))
    second = store.add_memory(MemoryRecord(
        id="mem_time_b", content="b", workspace_id=wid, scope=Scope.WORKSPACE,
    ))
    entity_id = store.upsert_entity(Node(
        id="ent_time", name="Time", workspace_id=wid,
    ))
    edge_id = store.upsert_edge(Edge(
        id="edge_time", src="ent_time", dst="ent_other", relation="related",
        workspace_id=wid,
    ))
    rows_before = store.conn.execute(
        "SELECT COUNT(*) AS n FROM audit"
    ).fetchone()["n"]

    with pytest.raises(ValueError, match="finite"):
        if operation == "add_memory":
            record = MemoryRecord(
                id="mem_invalid_time", content="bad", workspace_id=wid,
            )
            record.valid_from = bad_value
            store.add_memory(record)
        elif operation == "upsert_edge":
            edge = Edge(
                id="edge_invalid_time", src="a", dst="b", relation="related",
                workspace_id=wid,
            )
            edge.valid_from = bad_value
            store.upsert_edge(edge)
        elif operation == "add_edge_support":
            store.add_edge_support(
                edge_id, {"memory_id": first}, valid_from=bad_value,
            )
        elif operation == "close_validity":
            store.close_validity(first, at=bad_value)
        elif operation == "invalidate_edge":
            store.invalidate_edge(edge_id, at=bad_value)
        elif operation == "invalidate_edges_for_memory":
            store.invalidate_edges_for_memory(first, at=bad_value)
        elif operation == "retire_memory_graph_state":
            store.retire_memory_graph_state(first, at=bad_value)
        elif operation == "add_link":
            store.add_link(first, second, valid_from=bad_value)
        elif operation == "add_link_version":
            store.add_link_version(first, second, valid_from=bad_value)
        elif operation == "link_memory_entity":
            store.link_memory_entity(
                memory_id=first,
                entity_id=entity_id,
                workspace_id=wid,
                repo_id=None,
                valid_from=bad_value,
            )
        else:
            store.add_memory_tombstone(first, deleted_at=bad_value)

    assert store.conn.execute(
        "SELECT COUNT(*) AS n FROM audit"
    ).fetchone()["n"] == rows_before
    assert store.conn.in_transaction is False


@pytest.mark.parametrize("bad_value", BAD_TEMPORAL_VALUES)
@pytest.mark.parametrize(
    "operation",
    [
        "memory_matches_filter",
        "edges_in_scope",
        "edge_supports_in_scope",
        "neighbors",
        "mutated_filter",
    ],
)
def test_temporal_read_overrides_reject_invalid_values(store, operation, bad_value):
    with pytest.raises(ValueError, match="finite timestamp"):
        if operation == "memory_matches_filter":
            memory_matches_filter(
                MemoryRecord(id="mem_read_time", content="read", valid_from=1.0),
                None,
                at=bad_value,
            )
        elif operation == "edges_in_scope":
            store.edges_in_scope(at=bad_value)
        elif operation == "edge_supports_in_scope":
            store.edge_supports_in_scope(at=bad_value)
        elif operation == "neighbors":
            store.neighbors(["ent_read_time"], at=bad_value)
        else:
            flt = SearchFilter()
            flt.valid_at = bad_value
            store.list_memories(flt)


def test_invalid_memory_overwrite_rolls_back_audit_and_releases_writer(store):
    wid = store.get_or_create_workspace("overwrite-rollback")
    memory_id = store.add_memory(MemoryRecord(
        id="mem_overwrite", content="original", workspace_id=wid,
        valid_from=10.0,
    ))
    audit_before = store.conn.execute(
        "SELECT COUNT(*) AS n FROM audit"
    ).fetchone()["n"]
    with pytest.raises(ValueError, match="valid_to cannot predate"):
        store.add_memory(MemoryRecord(
            id=memory_id, content="rejected", workspace_id=wid,
            valid_from=10.0, valid_to=9.0,
        ))

    assert store.get_memory(memory_id).content == "original"
    assert store.conn.execute(
        "SELECT COUNT(*) AS n FROM audit"
    ).fetchone()["n"] == audit_before
    assert store.conn.in_transaction is False

    errors = []

    def write_after_failure():
        try:
            store.add_memory(MemoryRecord(
                id="", content="accepted", workspace_id=wid,
            ))
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    thread = threading.Thread(target=write_after_failure)
    thread.start()
    thread.join(timeout=3)
    assert not thread.is_alive()
    assert errors == []


def test_invalid_memory_overwrite_preserves_caller_owned_transaction(store):
    wid = store.get_or_create_workspace("overwrite-savepoint")
    memory_id = store.add_memory(MemoryRecord(
        id="mem_overwrite_savepoint", content="original", workspace_id=wid,
        valid_from=10.0,
    ))
    audit_before = store.conn.execute(
        "SELECT COUNT(*) AS n FROM audit"
    ).fetchone()["n"]

    store.conn.execute("BEGIN IMMEDIATE")
    with pytest.raises(ValueError, match="valid_to cannot predate"):
        store.add_memory(MemoryRecord(
            id=memory_id, content="rejected", workspace_id=wid,
            valid_from=10.0, valid_to=9.0,
        ), commit=False)

    assert store.conn.in_transaction
    assert store.get_memory(memory_id).content == "original"
    assert store.conn.execute(
        "SELECT COUNT(*) AS n FROM audit"
    ).fetchone()["n"] == audit_before
    accepted = store.add_memory(MemoryRecord(
        id="", content="outer transaction survives", workspace_id=wid,
    ), commit=False)
    assert store.get_memory(accepted) is not None
    store.conn.rollback()
    assert store.get_memory(accepted) is None
    assert store.get_memory(memory_id).content == "original"


def test_memory_link_writes_and_reads_enforce_endpoint_ownership(store):
    workspace_a = store.get_or_create_workspace("link-a")
    workspace_b = store.get_or_create_workspace("link-b")
    a = store.add_memory(MemoryRecord(
        id="mem_link_a", content="a", workspace_id=workspace_a,
        scope=Scope.WORKSPACE,
    ))
    same = store.add_memory(MemoryRecord(
        id="mem_link_same", content="same", workspace_id=workspace_a,
        scope=Scope.WORKSPACE,
    ))
    foreign = store.add_memory(MemoryRecord(
        id="mem_link_foreign", content="foreign", workspace_id=workspace_b,
        scope=Scope.WORKSPACE,
    ))

    store.add_link(a, same, relation="related")
    with pytest.raises(ValueError, match="share workspace ownership"):
        store.add_link(a, foreign, relation="related")
    with pytest.raises(ValueError, match="must exist"):
        store.add_link(a, "mem_missing", relation="related")
    assert store.conn.in_transaction is False

    # Simulate one legacy/direct-SQL row that predates the governed writer.
    store.conn.execute(
        "INSERT INTO mem_links(a,b,relation,layer,reason,created_at,valid_from,"
        "ingested_at) VALUES (?,?,?,?,?,?,?,?)",
        (a, foreign, "legacy", "semantic", "", 1.0, 1.0, 1.0),
    )
    store.conn.commit()
    flt = SearchFilter(workspace_id=workspace_a)
    assert [row["b"] for row in store.get_links(a, flt=flt)] == [same]
    assert [row["b"] for row in store.get_links(a)] == [same]
    assert not store.has_link(a, foreign, relation="legacy")
    assert store.links_among(
        [a, foreign], include_invalid=True
    ) == []
    assert all(
        foreign not in (row["a"], row["b"])
        for row in store.links_touching([a], flt=flt)
    )


def test_same_workspace_links_preserve_ancestor_and_cross_repo_relationships(store):
    wid = store.get_or_create_workspace("link-same-workspace")
    first_repo = store.get_or_create_repo(wid, "first")
    second_repo = store.get_or_create_repo(wid, "second")
    first = store.add_memory(MemoryRecord(
        id="mem_link_first_repo", content="first", workspace_id=wid,
        repo_id=first_repo, scope=Scope.REPO,
    ))
    second = store.add_memory(MemoryRecord(
        id="mem_link_second_repo", content="second", workspace_id=wid,
        repo_id=second_repo, scope=Scope.REPO,
    ))
    ancestor = store.add_memory(MemoryRecord(
        id="mem_link_workspace_ancestor", content="ancestor", workspace_id=wid,
        scope=Scope.WORKSPACE,
    ))

    store.add_link(first, second, relation="related")
    store.add_link(first, ancestor, relation="supports")
    assert store.has_link(first, second, relation="related")
    assert store.has_link(first, ancestor, relation="supports")
    assert {
        row["b"] for row in store.get_links(first)
    } == {second, ancestor}

    scoped = store.get_links(first, flt=SearchFilter(
        workspace_id=wid, repo_id=first_repo, include_ancestors=True,
    ))
    assert [(row["b"], row["relation"]) for row in scoped] == [
        (ancestor, "supports"),
    ]


def test_governed_scope_transition_requires_persisted_widening_evidence(store):
    wid = store.get_or_create_workspace("governed-link")
    rid = store.get_or_create_repo(wid, "repo")
    source = store.add_memory(MemoryRecord(
        id="mem_scope_source", content="source", workspace_id=wid,
        repo_id=rid, scope=Scope.REPO,
    ))
    promoted = store.add_memory(MemoryRecord(
        id="mem_scope_promoted", content="promoted", workspace_id=wid,
        scope=Scope.WORKSPACE, metadata={"promoted_from": [source]},
    ))
    unproven = store.add_memory(MemoryRecord(
        id="mem_scope_unproven", content="unproven", workspace_id=wid,
        scope=Scope.WORKSPACE,
    ))

    with pytest.raises(ValueError, match="governed promotion"):
        store.add_link(promoted, source, relation="promotes")
    with pytest.raises(ValueError, match="lacks persisted source evidence"):
        store.add_link(
            unproven, source, relation="promotes", allow_scope_transition=True,
        )
    store.add_link(
        promoted, source, relation="promotes", allow_scope_transition=True,
    )
    assert store.has_link(promoted, source, relation="promotes")
    store.conn.execute(
        "INSERT INTO mem_links(a,b,relation,layer,reason,created_at,valid_from,"
        "ingested_at) VALUES (?,?,?,?,?,?,?,?)",
        (unproven, source, "promotes", "semantic", "", 1.0, 1.0, 1.0),
    )
    store.conn.commit()
    assert not store.has_link(unproven, source, relation="promotes")
    assert store.get_links(unproven) == []


def test_visible_memory_ids_matches_canonical_filter_and_is_bounded(store):
    wid = store.get_or_create_workspace("visibility")
    rid = store.get_or_create_repo(wid, "repo")
    sid = store.start_session(wid, rid, agent="test")
    ids_in_scope = [
        store.add_memory(MemoryRecord(
            id="mem_vis_workspace", content="workspace", workspace_id=wid,
            scope=Scope.WORKSPACE,
        )),
        store.add_memory(MemoryRecord(
            id="mem_vis_repo", content="repo", workspace_id=wid,
            repo_id=rid, scope=Scope.REPO,
        )),
        store.add_memory(MemoryRecord(
            id="mem_vis_session", content="session", workspace_id=wid,
            repo_id=rid, session_id=sid, scope=Scope.SESSION,
        )),
    ]
    closed = store.add_memory(MemoryRecord(
        id="mem_vis_closed", content="closed", workspace_id=wid,
        scope=Scope.WORKSPACE,
    ))
    stored_closed = store.get_memory(closed)
    store.close_validity(closed, at=stored_closed.valid_from)
    candidates = [*ids_in_scope, closed, "mem_missing"]
    flt = SearchFilter(
        workspace_id=wid,
        repo_id=rid,
        session_id=sid,
        include_ancestors=True,
    )
    records = store.get_memories(candidates)
    expected = {
        memory_id for memory_id, record in records.items()
        if memory_matches_filter(record, flt)
    }

    assert store.visible_memory_ids(candidates, flt) == expected
    assert closed in store.visible_memory_ids(
        candidates, flt, include_invalid=True
    )
    with pytest.raises(ValueError, match="at most"):
        store.visible_memory_ids(
            [f"mem_{index}" for index in range(501)], flt
        )


def test_entity_and_code_graph_reads_honor_keyset_and_sentinel_limits(store):
    wid = store.get_or_create_workspace("bounded-graph")
    rid = store.get_or_create_repo(wid, "repo")
    for entity_id, name in (
        ("ent_03", "Three"), ("ent_01", "One"), ("ent_02", "Two"),
    ):
        store.upsert_entity(Node(
            id=entity_id, name=name, workspace_id=wid, repo_id=rid,
        ))
    flt = SearchFilter(workspace_id=wid, repo_id=rid)
    first_page = store.list_entities(flt, after_id="", limit=2)
    second_page = store.list_entities(
        flt, after_id=first_page[-1].id, limit=2
    )
    assert [node.id for node in first_page] == ["ent_01", "ent_02"]
    assert [node.id for node in second_page] == ["ent_03"]

    symbol_ids = []
    for index in range(4):
        symbol_ids.append(store.upsert_symbol(
            repo_id=rid,
            kind="function",
            name=f"symbol_{index}",
            fqname=f"pkg.symbol_{index}",
            file=f"file_{index // 2}.py",
            span=f"{index + 1}:{index + 1}",
        ))
        store.add_code_edge(
            repo_id=rid,
            src=f"symbol_{index}",
            dst=f"symbol_{(index + 1) % 4}",
            relation="calls",
        )
    assert len(store.symbols_for_files(
        rid, ["file_0.py", "file_1.py"], flt=flt, limit=3
    )) == 3
    assert len(store.list_symbols(rid, flt=flt, limit=3)) == 3
    assert len(store.list_code_edges(rid, flt=flt, limit=3)) == 3


def test_store_satisfies_narrow_graph_protocols(store):
    assert isinstance(store, GraphReader)
    assert isinstance(store, GraphWriter)


def test_concurrent_identity_initializers_and_reinforcement_converge(tmp_path):
    db_path = str(tmp_path / "identity.db")
    Store(db_path).close()
    stores = [Store(db_path), Store(db_path)]
    try:
        def race(call):
            barrier = threading.Barrier(2)
            results = []
            errors = []

            def worker(index):
                try:
                    barrier.wait()
                    results.append(call(stores[index]))
                except BaseException as exc:  # pragma: no cover - surfaced below
                    errors.append(exc)

            threads = [threading.Thread(target=worker, args=(index,)) for index in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
            assert all(not thread.is_alive() for thread in threads)
            assert errors == []
            return results

        workspace_ids = race(
            lambda current: current.get_or_create_workspace("shared")
        )
        assert len(set(workspace_ids)) == 1
        personal_ids = race(
            lambda current: current.get_or_create_workspace(
                "personal-race",
                settings={
                    "visibility": "personal",
                    "owner": (
                        "alice@example.test"
                        if current is stores[0] else "bob@example.test"
                    ),
                },
            )
        )
        assert len(set(personal_ids)) == 1
        persisted_row = stores[0].conn.execute(
            "SELECT settings FROM workspaces WHERE id=?", (personal_ids[0],)
        ).fetchone()
        assert persisted_row is not None
        persisted_settings = json.loads(persisted_row["settings"])
        assert persisted_settings in (
            {"visibility": "personal", "owner": "alice@example.test"},
            {"visibility": "personal", "owner": "bob@example.test"},
        )
        assert stores[1].get_or_create_workspace(
            "personal-race",
            settings={"visibility": "personal", "owner": "mallory@example.test"},
        ) == personal_ids[0]
        reread_row = stores[0].conn.execute(
            "SELECT settings FROM workspaces WHERE id=?", (personal_ids[0],)
        ).fetchone()
        assert reread_row is not None
        assert json.loads(reread_row["settings"]) == persisted_settings
        repo_ids = race(
            lambda current: current.get_or_create_repo(workspace_ids[0], "repo")
        )
        assert len(set(repo_ids)) == 1
        device_ids = race(lambda current: current.device_id())
        assert len(set(device_ids)) == 1

        memory_id = stores[0].add_memory(MemoryRecord(
            id="mem_reinforce_concurrent",
            content="reinforce",
            workspace_id=workspace_ids[0],
        ))
        initial = stores[0].get_memory(memory_id)
        assert initial is not None
        barrier = threading.Barrier(8)
        errors = []

        def reinforce(index):
            try:
                barrier.wait()
                for _ in range(10):
                    stores[index % 2].reinforce(memory_id)
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        threads = [threading.Thread(target=reinforce, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        assert all(not thread.is_alive() for thread in threads)
        assert errors == []
        record = stores[0].get_memory(memory_id)
        assert record is not None
        assert record.access_count == 80
        expected_stability = initial.stability
        expected_count = initial.access_count
        for _ in range(80):
            expected_stability, expected_count = reinforced_stability(
                expected_stability, expected_count
            )
        assert record.stability == pytest.approx(expected_stability)
        assert record.access_count == expected_count
        assert math.isfinite(record.stability)
    finally:
        for current in stores:
            current.close()


def test_reinforce_preserves_caller_owned_transaction(store):
    wid = store.get_or_create_workspace("reinforce-transaction")
    memory_id = store.add_memory(MemoryRecord(
        id="mem_reinforce_transaction",
        content="reinforce",
        workspace_id=wid,
    ))
    before = store.get_memory(memory_id)
    assert before is not None

    store.conn.execute("BEGIN IMMEDIATE")
    store.reinforce(memory_id)
    during = store.get_memory(memory_id)
    assert during is not None
    assert during.access_count == before.access_count + 1
    assert store.conn.in_transaction
    store.conn.rollback()

    after = store.get_memory(memory_id)
    assert after is not None
    assert after.access_count == before.access_count
    assert after.stability == before.stability


def test_read_only_store_is_query_only_and_leaves_database_files_unchanged(tmp_path):
    db_path = tmp_path / "read-only.db"
    writable = Store(str(db_path))
    wid = writable.get_or_create_workspace("read-only")
    memory_id = writable.add_memory(MemoryRecord(
        id="", content="immutable evidence", workspace_id=wid,
    ))
    writable.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    writable.close()

    tracked = [db_path, tmp_path / "read-only.db-wal", tmp_path / "read-only.db-shm"]

    def file_state(path):
        return (
            path.exists(),
            path.stat().st_size if path.exists() else None,
            path.stat().st_mtime_ns if path.exists() else None,
        )

    before = {path.name: file_state(path) for path in tracked}
    read_only = Store(str(db_path), read_only=True)
    try:
        record = read_only.get_memory(memory_id)
        assert record is not None and record.content == "immutable evidence"
        query_only = read_only.conn.execute("PRAGMA query_only").fetchone()
        assert query_only is not None and query_only[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            read_only.conn.execute(
                "UPDATE memories SET content='changed' WHERE id=?", (memory_id,)
            )
    finally:
        read_only.close()
    after = {path.name: file_state(path) for path in tracked}
    assert after == before


def test_read_only_store_rejects_active_wal_without_touching_sidecars(tmp_path):
    db_path = tmp_path / "active-wal.db"
    writable = Store(str(db_path))
    try:
        writable.conn.execute("PRAGMA wal_autocheckpoint=0")
        writable.get_or_create_workspace("active-wal")
        wal_path = tmp_path / "active-wal.db-wal"
        assert wal_path.is_file() and wal_path.stat().st_size > 0
        before = (
            wal_path.stat().st_size,
            wal_path.stat().st_mtime_ns,
        )

        with pytest.raises(RuntimeError, match="active WAL found"):
            Store(str(db_path), read_only=True)

        assert (
            wal_path.stat().st_size,
            wal_path.stat().st_mtime_ns,
        ) == before
    finally:
        writable.close()


def test_read_only_store_rejects_incomplete_current_version_marker(tmp_path):
    db_path = tmp_path / "incomplete.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at REAL)"
    )
    conn.execute(
        "INSERT INTO schema_migrations(version, applied_at) VALUES (?, 0)",
        (SCHEMA_VERSION,),
    )
    conn.commit()
    conn.close()

    with pytest.raises(RuntimeError, match="complete current schema"):
        Store(str(db_path), read_only=True)


def test_public_store_rejects_user_scope_before_mutation_but_legacy_import_reads(store):
    wid = store.get_or_create_workspace("user-scope")
    record = MemoryRecord(
        id="mem_user_legacy",
        content="historical user preference",
        workspace_id=wid,
        scope=Scope.USER,
    )
    audit_before = store.conn.execute(
        "SELECT COUNT(*) AS n FROM audit"
    ).fetchone()["n"]
    message = (
        "user scope is not supported until owner-aware memories are implemented; "
        "use workspace, repo, or session"
    )

    with pytest.raises(ValueError, match=message):
        store.add_memory(record)
    assert store.get_memory(record.id) is None
    assert store.conn.execute(
        "SELECT COUNT(*) AS n FROM audit"
    ).fetchone()["n"] == audit_before
    assert store.conn.in_transaction is False

    store.add_memory(record, _allow_legacy_user_scope=True)
    historical = store.get_memory(record.id)
    assert historical is not None and historical.scope == Scope.USER


@pytest.mark.parametrize(
    ("scope", "sensitivity", "mark_exported", "expected"),
    [
        (Scope.WORKSPACE, "normal", True, "remote_erasure"),
        (Scope.REPO, "sensitive", True, "remote_erasure"),
        (Scope.WORKSPACE, "normal", False, "never_export"),
        (Scope.SESSION, "normal", False, "never_export"),
        (Scope.WORKSPACE, "secret", False, "never_export"),
        (Scope.USER, "normal", False, "never_export"),
    ],
)
def test_secure_erase_classifies_content_free_tombstone_export(
        store, scope, sensitivity, mark_exported, expected):
    wid = store.get_or_create_workspace("erase-class")
    rid = store.get_or_create_repo(wid, "repo")
    session_id = store.start_session(wid, rid, agent="test")
    record = MemoryRecord(
        id=f"mem_erase_{scope.value}_{sensitivity}",
        content="erasable record",
        workspace_id=wid,
        repo_id=rid if scope in (Scope.REPO, Scope.SESSION) else None,
        session_id=session_id if scope == Scope.SESSION else None,
        scope=scope,
        sensitivity=sensitivity,
    )
    memory_id = store.add_memory(
        record,
        _allow_legacy_user_scope=scope == Scope.USER,
    )
    if mark_exported:
        assert store.mark_memories_sync_exported(
            [memory_id], workspace_id=wid
        ) == 1

    result = store.secure_erase_memory(memory_id)

    assert result["export_class"] == expected
    tombstone = next(
        row for row in store.list_memory_tombstones()
        if row["id"] == memory_id
    )
    assert tombstone["export_class"] == expected
    assert set(tombstone) == {
        "id", "deleted_at", "device", "workspace_id", "repo_id",
        "export_class",
    }


def test_secure_erase_defers_maintenance_for_caller_owned_transaction(store):
    wid = store.get_or_create_workspace("erase-outer-transaction")
    memory_id = store.add_memory(MemoryRecord(
        id="mem_erase_outer_transaction", content="erasable record",
        workspace_id=wid, scope=Scope.WORKSPACE,
    ))
    store.conn.execute("BEGIN IMMEDIATE")

    result = store.secure_erase_memory(memory_id)

    assert result["maintenance"] == {
        "secure_delete": True, "wal": "deferred", "vacuum": "deferred",
    }
    assert store.conn.in_transaction is True
    store.conn.commit()
    assert store.get_memory(memory_id) is None


def test_secure_erase_rescans_successors_instead_of_trusting_stale_targets(store):
    wid = store.get_or_create_workspace("erase-successor-race")
    parent_id = store.add_memory(MemoryRecord(
        id="mem_erase_parent", content="parent secret", workspace_id=wid,
        scope=Scope.WORKSPACE,
    ))
    successor_id = store.add_memory(MemoryRecord(
        id="mem_erase_successor", content="successor secret", workspace_id=wid,
        scope=Scope.WORKSPACE,
        provenance={"conflict_of": parent_id},
    ))

    store.secure_erase_memory(parent_id, _target_ids=[parent_id])

    assert store.get_memory(parent_id) is None
    assert store.get_memory(successor_id) is None
    assert {row["id"] for row in store.list_memory_tombstones()} >= {
        parent_id, successor_id,
    }


def test_tombstone_export_class_is_strict_and_monotonic(store):
    with pytest.raises(ValueError, match="export_class must be"):
        store.add_memory_tombstone(
            "mem_invalid_export",
            device_id="device",
            export_class="local_only",
        )
    assert store.list_memory_tombstones() == []

    store.add_memory_tombstone(
        "mem_terminal",
        deleted_at=20.0,
        device_id="remote",
        export_class="remote_erasure",
    )
    store.add_memory_tombstone(
        "mem_terminal",
        deleted_at=10.0,
        device_id="local",
        export_class="never_export",
    )
    tombstone = store.list_memory_tombstones()[0]
    assert tombstone["deleted_at"] == 10.0
    assert tombstone["device"] == "local"
    assert tombstone["export_class"] == "remote_erasure"

    store.add_memory_tombstone(
        "mem_private_terminal",
        deleted_at=30.0,
        device_id="local",
        export_class="never_export",
    )
    store.conn.commit()
    with pytest.raises(ValueError, match="cannot become remotely exportable"):
        store.add_memory_tombstone(
            "mem_private_terminal",
            deleted_at=5.0,
            device_id="remote",
            export_class="remote_erasure",
        )
    private = next(
        item for item in store.list_memory_tombstones()
        if item["id"] == "mem_private_terminal"
    )
    assert private["deleted_at"] == 30.0
    assert private["export_class"] == "never_export"



def test_modified_hlc_advances_across_clock_rollback_and_caller_rollback(
        store, monkeypatch):
    monkeypatch.setattr("engraphis.core.store.now_ts", lambda: 100.0)
    wid = store.get_or_create_workspace("modified-hlc")
    memory_id = store.add_memory(MemoryRecord(
        id="mem_modified_hlc",
        content="versioned",
        workspace_id=wid,
        scope=Scope.WORKSPACE,
    ))
    initial = store.get_memory(memory_id)
    assert initial is not None
    physical, logical, _ = parse_modified_hlc(initial.modified_hlc)
    assert (physical, logical) == (100_000, 0)

    monkeypatch.setattr("engraphis.core.store.now_ts", lambda: 90.0)
    rolled_back_clock = store.advance_memory_modified_hlc(memory_id)
    next_physical, next_logical, _ = parse_modified_hlc(rolled_back_clock)
    assert (next_physical, next_logical) == (physical, logical + 1)

    observed = format_modified_hlc(
        physical + 10, 7, "dev_" + ("0" * 26)
    )
    advanced = store.advance_memory_modified_hlc(
        memory_id, observed_hlc=observed
    )
    assert advanced > observed
    persisted = store.get_memory(memory_id)
    assert persisted is not None and persisted.modified_hlc == advanced

    store.conn.execute("BEGIN IMMEDIATE")
    nested = store.advance_memory_modified_hlc(memory_id, commit=True)
    assert nested > advanced
    assert store.conn.in_transaction
    store.conn.rollback()
    restored = store.get_memory(memory_id)
    assert restored is not None and restored.modified_hlc == advanced

    uncommitted = store.advance_memory_modified_hlc(memory_id, commit=False)
    assert uncommitted > advanced
    assert store.conn.in_transaction
    store.conn.rollback()
    restored = store.get_memory(memory_id)
    assert restored is not None and restored.modified_hlc == advanced


def test_modified_hlc_advances_atomically_across_store_connections(
        tmp_path, monkeypatch):
    monkeypatch.setattr("engraphis.core.store.now_ts", lambda: 200.0)
    db_path = str(tmp_path / "modified-hlc-race.db")
    stores = [Store(db_path), Store(db_path)]
    try:
        wid = stores[0].get_or_create_workspace("modified-hlc-race")
        memory_id = stores[0].add_memory(MemoryRecord(
            id="mem_modified_hlc_race",
            content="versioned",
            workspace_id=wid,
            scope=Scope.WORKSPACE,
        ))
        barrier = threading.Barrier(8)
        results: list[str] = []
        errors: list[BaseException] = []
        result_lock = threading.Lock()

        def advance(index):
            try:
                barrier.wait()
                value = stores[index % 2].advance_memory_modified_hlc(memory_id)
                with result_lock:
                    results.append(value)
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        threads = [
            threading.Thread(target=advance, args=(index,))
            for index in range(8)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        assert all(not thread.is_alive() for thread in threads)
        assert errors == []
        assert len(set(results)) == len(results) == 8
        persisted = stores[0].get_memory(memory_id)
        assert persisted is not None
        assert persisted.modified_hlc == max(results)
    finally:
        for current in stores:
            current.close()


def test_sync_export_markers_are_bounded_atomic_and_content_free(store):
    wid = store.get_or_create_workspace("sync-export-proof")
    shared_id = store.add_memory(MemoryRecord(
        id="mem_sync_export_shared",
        content="shareable",
        workspace_id=wid,
        scope=Scope.WORKSPACE,
    ))
    secret_id = store.add_memory(MemoryRecord(
        id="mem_sync_export_secret",
        content="private",
        workspace_id=wid,
        scope=Scope.WORKSPACE,
        sensitivity="secret",
    ))

    with pytest.raises(ValueError, match="shareable workspace/repo"):
        store.mark_memories_sync_exported(
            [shared_id, secret_id], workspace_id=wid
        )
    assert store.get_memory_sync_export(shared_id) is None
    assert store.get_memory_sync_export(secret_id) is None
    assert store.conn.in_transaction is False

    with pytest.raises(ValueError, match="finite timestamp"):
        store.mark_memories_sync_exported(
            [shared_id], workspace_id=wid, exported_at=float("inf")
        )

    store.conn.execute("BEGIN IMMEDIATE")
    assert store.mark_memories_sync_exported(
        [shared_id, shared_id], workspace_id=wid,
        exported_at=10.0, commit=True,
    ) == 1
    marker = store.get_memory_sync_export(shared_id)
    assert marker == {
        "memory_id": shared_id,
        "workspace_id": wid,
        "repo_id": None,
        "first_exported_at": 10.0,
        "last_exported_at": 10.0,
    }
    assert store.conn.in_transaction
    store.conn.rollback()
    assert store.get_memory_sync_export(shared_id) is None


def test_prior_export_marker_survives_private_transition_and_secure_erase(store):
    wid = store.get_or_create_workspace("sync-export-transition")
    rid = store.get_or_create_repo(wid, "repo")
    session_id = store.start_session(wid, rid, agent="test")
    memory_id = store.add_memory(MemoryRecord(
        id="mem_sync_export_transition",
        content="shared first",
        workspace_id=wid,
        repo_id=rid,
        scope=Scope.REPO,
    ))
    store.mark_memories_sync_exported(
        [memory_id], workspace_id=wid, exported_at=10.0
    )

    store.advance_memory_modified_hlc(memory_id, commit=False)
    store.conn.execute(
        "UPDATE memories SET scope='session', session_id=?, sensitivity='secret' "
        "WHERE id=?",
        (session_id, memory_id),
    )
    store.conn.commit()
    result = store.secure_erase_memory(memory_id)

    assert result["export_class"] == "remote_erasure"
    tombstone = next(
        row for row in store.list_memory_tombstones(wid, rid)
        if row["id"] == memory_id
    )
    assert tombstone["export_class"] == "remote_erasure"
    assert tombstone["repo_id"] == rid
    marker = store.get_memory_sync_export(memory_id)
    assert marker is not None
    assert set(marker) == {
        "memory_id", "workspace_id", "repo_id",
        "first_exported_at", "last_exported_at",
    }


@pytest.mark.parametrize(
    "modified_hlc",
    [
        "not-an-hlc",
        "000000000001:00000000:legacy-device",
        "000000000001:000000000:dev_" + ("0" * 26),
        "000000000001:00000000:dev_" + ("I" * 26),
    ],
)
def test_modified_hlc_rejects_noncanonical_values_before_persisting(
        store, modified_hlc):
    wid = store.get_or_create_workspace("invalid-modified-hlc")
    record = MemoryRecord(
        id="mem_invalid_modified_hlc",
        content="invalid version",
        workspace_id=wid,
    )
    record.modified_hlc = modified_hlc

    with pytest.raises(ValueError, match="canonical HLC"):
        store.add_memory(record)

    assert store.get_memory(record.id) is None
    assert store.conn.in_transaction is False


def test_add_memory_preserves_blank_legacy_hlc_only_by_explicit_opt_in(store):
    wid = store.get_or_create_workspace("legacy-hlc-import")
    local_id = store.add_memory(MemoryRecord(
        id="mem_local_hlc",
        content="local",
        workspace_id=wid,
    ))
    local = store.get_memory(local_id)
    assert local is not None and local.modified_hlc

    legacy = MemoryRecord(
        id="mem_legacy_hlc",
        content="legacy one",
        workspace_id=wid,
        ingested_at=10.0,
    )
    store.add_memory(legacy, _preserve_legacy_modified_hlc=True)
    persisted = store.get_memory(legacy.id)
    assert persisted is not None and persisted.modified_hlc == ""

    store.add_memory(
        MemoryRecord(
            id=legacy.id,
            content="legacy two",
            workspace_id=wid,
            ingested_at=20.0,
        ),
        audit=False,
        _preserve_legacy_modified_hlc=True,
    )
    persisted = store.get_memory(legacy.id)
    assert persisted is not None
    assert persisted.content == "legacy two"
    assert persisted.modified_hlc == ""

    store.conn.execute("BEGIN IMMEDIATE")
    store.add_memory(
        MemoryRecord(
            id="mem_legacy_hlc_rollback",
            content="rolled back",
            workspace_id=wid,
        ),
        _preserve_legacy_modified_hlc=True,
    )
    assert store.conn.in_transaction
    store.conn.rollback()
    assert store.get_memory("mem_legacy_hlc_rollback") is None

    advanced = store.advance_memory_modified_hlc(legacy.id)
    assert advanced
    persisted = store.get_memory(legacy.id)
    assert persisted is not None and persisted.modified_hlc == advanced


def test_add_memory_advances_hlc_only_for_real_local_descriptive_overwrite(store):
    wid = store.get_or_create_workspace("local-hlc-overwrite")
    memory_id = store.add_memory(MemoryRecord(
        id="mem_local_hlc_overwrite",
        content="one",
        workspace_id=wid,
    ))
    original = store.get_memory(memory_id)
    assert original is not None and original.modified_hlc
    original_clock = original.modified_hlc

    original.content = "two"
    store.add_memory(original)
    changed = store.get_memory(memory_id)
    assert changed is not None
    assert changed.modified_hlc > original_clock

    changed_clock = changed.modified_hlc
    changed.modified_hlc = ""
    store.add_memory(changed)
    idempotent = store.get_memory(memory_id)
    assert idempotent is not None
    assert idempotent.modified_hlc == changed_clock

    idempotent.stability += 1.0
    store.add_memory(idempotent)
    lattice_only = store.get_memory(memory_id)
    assert lattice_only is not None
    assert lattice_only.stability == idempotent.stability
    assert lattice_only.modified_hlc == changed_clock

def test_context_savings_handles_workspace_ids_above_sqlite_variable_limit(
    store, monkeypatch
):
    """Large authorization scopes use bounded queries without leaking other workspaces."""
    from engraphis.core import store as store_mod

    def usage(source, context):
        saved = source - context
        return {
            "source_tokens": source,
            "context_tokens": context,
            "saved_tokens": saved,
            "budget_tokens": source,
            "packed_count": 1,
            "omitted_count": 0,
            "token_counter": "engraphis.regex.v1",
            "baseline_tokens": source,
            "emitted_tokens": context,
            "estimated_saved_tokens": saved,
            "estimated_savings_ratio": saved / source,
            "savings_basis": "packed_context",
            "savings_confidence": "medium",
            "savings_eligible": True,
            "release_version": "1.5",
        }

    included_ids = []
    for index in range(3):
        workspace_id = store.get_or_create_workspace(f"authorized-{index}")
        included_ids.append(workspace_id)
        store.record_receipt(
            "recall",
            workspace_id=workspace_id,
            metadata={"intent": "recall_context", "token_usage": usage(500, 300)},
        )
    unauthorized_id = store.get_or_create_workspace("unauthorized")
    store.record_receipt(
        "recall",
        workspace_id=unauthorized_id,
        metadata={"intent": "recall_context", "token_usage": usage(9_999, 1_111)},
    )
    authorized_ids = included_ids + [
        f"authorized-empty-{index}" for index in range(1_097)
    ]

    original_execute = store_mod._SerializedConnection.execute

    def bounded_execute(connection, *args, **kwargs):
        params = args[1] if len(args) > 1 else kwargs.get("parameters", ())
        assert len(params) <= 100, "receipt query exceeded the simulated SQLite limit"
        return original_execute(connection, *args, **kwargs)

    monkeypatch.setattr(store_mod, "IN_CLAUSE_CHUNK", 96)
    monkeypatch.setattr(store_mod._SerializedConnection, "execute", bounded_execute)

    result = store.context_savings(
        workspace_ids=authorized_ids,
        from_ts=0,
        to_ts=9_999_999_999,
        release_version="1.5",
    )
    assert result["receipt_count"] == len(included_ids)
    assert result["savings_receipt_count"] == len(included_ids)
    counter = result["by_token_counter"][0]
    assert counter["source_tokens"] == 500 * len(included_ids)
    assert counter["context_tokens"] == 300 * len(included_ids)
    assert counter["saved_tokens"] == 200 * len(included_ids)

    grouped = store.context_savings_grouped(
        workspace_ids=authorized_ids,
        group_by="workspace",
        from_ts=0,
        to_ts=9_999_999_999,
        release_version="1.5",
    )
    assert {row["group_key"] for row in grouped} == set(included_ids)
    assert unauthorized_id not in {row["group_key"] for row in grouped}

    assert store.context_savings(workspace_ids=[])["receipt_count"] == 0
    deduped = store.context_savings(workspace_ids=included_ids + included_ids)
    assert deduped["receipt_count"] == len(included_ids)
