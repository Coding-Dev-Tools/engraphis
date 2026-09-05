"""Atomic new-row insertion avoids an unindexed FTS scan; repair still replaces."""
import pytest

from engraphis.core.interfaces import MemoryRecord
from engraphis.core.store import Store


@pytest.fixture(params=["fts5", "fallback"])
def store(request):
    store = Store(":memory:")
    if request.param == "fts5" and not store.has_fts5:
        store.close()
        pytest.skip("SQLite FTS5 unavailable")
    if request.param == "fallback":
        store.conn.execute("DROP TABLE mem_fts")
        store.conn.execute(
            "CREATE TABLE mem_fts(id TEXT PRIMARY KEY, title TEXT, content TEXT, keywords TEXT)"
        )
        store.has_fts5 = False
        store.conn.commit()
    yield store
    store.close()


def _record(store, mid="mem_fts_insert", content="original searchable evidence"):
    return MemoryRecord(id=mid, content=content,
                        workspace_id=store.get_or_create_workspace("fts-insert"))


def _mirrors(store, mid):
    return [row["content"] for row in store.conn.execute(
        "SELECT content FROM mem_fts WHERE id=?", (mid,),
    ).fetchall()]


def test_new_rows_do_not_scan_fts_and_updates_replace_searchable_content(store):
    calls = []

    def observe(sql):
        if "DELETE FROM mem_fts" in sql:
            calls.append(sql)

    store.conn.set_trace_callback(observe)
    record = _record(store)
    store.add_memory(record)
    store.add_memory(_record(store, mid="mem_fts_second"))
    assert calls == []
    assert _mirrors(store, record.id) == [record.content]
    record.content = "replacement searchable evidence"
    store.add_memory(record)
    assert len(calls) == 1
    assert _mirrors(store, record.id) == [record.content]
    assert store.fts_search("replacement", k=5)[0][0] == record.id
    assert record.id not in {mid for mid, _ in store.fts_search("original", k=5)}


def test_update_cleans_existing_duplicate_mirrors(store):
    if not store.has_fts5:
        pytest.skip("plain-table primary key already prevents duplicates")
    record = _record(store)
    store.add_memory(record)
    store.conn.execute(
        "INSERT INTO mem_fts(id,title,content,keywords) VALUES (?,?,?,?)",
        (record.id, "", "stale duplicate", ""),
    )
    store.conn.commit()
    assert len(_mirrors(store, record.id)) == 2
    record.content = "correct replacement"
    store.add_memory(record)
    assert _mirrors(store, record.id) == [record.content]


def test_explicit_fts_repair_replaces_orphan_mirror(store):
    store.conn.execute(
        "INSERT INTO mem_fts(id,title,content,keywords) VALUES (?,?,?,?)",
        ("mem_orphan", "", "stale orphan", ""),
    )
    store._fts_upsert("mem_orphan", "", "repaired orphan", "")
    assert _mirrors(store, "mem_orphan") == ["repaired orphan"]


@pytest.mark.parametrize("first_insert", [True, False])
def test_new_canonical_insert_replaces_preexisting_orphan(store, first_insert):
    record = _record(store, mid="mem_legacy_orphan", content="correct canonical evidence")
    store.conn.execute(
        "INSERT INTO mem_fts(id,title,content,keywords) VALUES (?,?,?,?)",
        (record.id, "", "stale orphan", ""),
    )
    if store.has_fts5:
        store.conn.execute(
            "INSERT INTO mem_fts(id,title,content,keywords) VALUES (?,?,?,?)",
            (record.id, "", "second stale orphan", ""),
        )
    store.conn.commit()
    if not first_insert:
        store.add_memory(_record(store, mid="mem_unrelated"))
    store.add_memory(record)
    assert _mirrors(store, record.id) == [record.content]
    assert store.fts_search("correct", k=5)[0][0] == record.id


def test_orphan_repair_inventory_remains_safe_after_outer_rollback(store):
    record = _record(store, mid="mem_legacy_orphan")
    store.conn.execute(
        "INSERT INTO mem_fts(id,title,content,keywords) VALUES (?,?,?,?)",
        (record.id, "", "legacy orphan", ""),
    )
    store.conn.commit()
    store.conn.execute("BEGIN IMMEDIATE")
    store.add_memory(record, commit=False)
    store.conn.rollback()
    assert _mirrors(store, record.id) == ["legacy orphan"]
    store.add_memory(record)
    assert _mirrors(store, record.id) == [record.content]


def test_direct_fts_repair_after_inventory_is_safe_for_later_canonical_insert(store):
    store.add_memory(_record(store))
    store._fts_upsert("mem_later_orphan", "", "orphan created by repair", "")
    store.conn.commit()
    record = _record(store, mid="mem_later_orphan", content="canonical replacement")
    store.add_memory(record)
    assert _mirrors(store, record.id) == [record.content]


def test_failed_mirror_insert_rolls_back_canonical_and_fts_rows(store, monkeypatch):
    record = _record(store)
    original = store._fts_upsert

    def fail_after_mirror(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("failure after mirror insertion")

    monkeypatch.setattr(store, "_fts_upsert", fail_after_mirror)
    with pytest.raises(RuntimeError, match="failure after mirror"):
        store.add_memory(record)
    assert store.get_memory(record.id) is None
    assert _mirrors(store, record.id) == []
    monkeypatch.undo()
    store.add_memory(record)
    assert _mirrors(store, record.id) == [record.content]


def test_outer_transaction_rollback_removes_both_new_rows(store):
    record = _record(store)
    store.conn.execute("BEGIN IMMEDIATE")
    store.add_memory(record, commit=False)
    assert _mirrors(store, record.id) == [record.content]
    store.conn.rollback()
    assert store.get_memory(record.id) is None
    assert _mirrors(store, record.id) == []


def test_erase_removes_all_mirrors_before_same_id_is_reinserted(store):
    record = _record(store)
    store.add_memory(record)
    if store.has_fts5:
        store.conn.execute(
            "INSERT INTO mem_fts(id,title,content,keywords) VALUES (?,?,?,?)",
            (record.id, "", "stale duplicate", ""),
        )
        store.conn.commit()
    store.secure_erase_memory(record.id)
    assert store.get_memory(record.id) is None
    assert _mirrors(store, record.id) == []
    store.add_memory(record)
    assert _mirrors(store, record.id) == [record.content]
