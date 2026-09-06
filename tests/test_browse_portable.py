"""Portable browsing survives unrelated activity and retains its temporal snapshot."""
from __future__ import annotations

import base64
import json
import multiprocessing

import pytest

from engraphis.core import browsing
from engraphis.core.browsing import BrowseCursorStale, browse_memories
from engraphis.core.interfaces import MemoryRecord, MemoryType, Scope, SearchFilter
from engraphis.core.store import Store


def _seed(store, *, count=3, workspace="w", repo=None, scope=Scope.REPO, session=None,
          mtype=MemoryType.SEMANTIC, prefix="mem", start=1):
    workspace_id = store.get_or_create_workspace(workspace)
    repo_id = store.get_or_create_repo(workspace_id, repo) if repo else None
    with store.write_transaction():
        for number in range(count):
            store.add_memory(MemoryRecord(
                id=f"{prefix}_{number:05d}", workspace_id=workspace_id, repo_id=repo_id,
                session_id=session, scope=scope, mtype=mtype,
                content=f"Evidence {number}", valid_from=float(start + number),
                ingested_at=0,
            ), commit=False)
    return SearchFilter(workspace_id=workspace_id, repo_id=repo_id,
                        include_ancestors=True)


def _browse_in_process(path, flt, connection):
    store = Store(path)
    try:
        connection.send("ready")
        while True:
            cursor = connection.recv()
            if cursor is None:
                break
            try:
                page = browse_memories(store, flt, limit=100, cursor=cursor)
                connection.send({**page, "rows": [row["id"] for row in page["rows"]]})
            except Exception as exc:
                connection.send({"error": f"{type(exc).__name__}: {exc}"})
    finally:
        store.close()
        connection.close()


def test_1201_pages_cross_processes_during_reinforcement_and_audit(tmp_path):
    path = str(tmp_path / "portable.db")
    store = Store(path)
    flt = _seed(store, count=1201, repo="main")
    _seed(store, workspace="unrelated", prefix="mem_other")
    ctx = multiprocessing.get_context("spawn")
    parent, child = ctx.Pipe()
    process = ctx.Process(target=_browse_in_process, args=(path, flt, child))
    process.start()
    child.close()
    try:
        assert parent.poll(20)
        assert parent.recv() == "ready"
        cursor = ""
        ids = []
        anchors = None
        page_number = 0
        while True:
            if page_number % 2:
                parent.send(cursor)
                assert parent.poll(20)
                page = parent.recv()
                assert "error" not in page, page
            else:
                page = browse_memories(store, flt, limit=100, cursor=cursor)
                page["rows"] = [row["id"] for row in page["rows"]]
            current_anchors = page["valid_at"], page["known_at"]
            anchors = anchors or current_anchors
            assert anchors == current_anchors
            assert page["total_count"] == 1201
            ids.extend(page["rows"])
            cursor = page["next_cursor"]
            if not cursor:
                break
            store.reinforce("mem_00000")
            store.audit("test", "background_activity", "", "content-free")
            store.conn.execute("UPDATE memories SET title=? WHERE id='mem_other_00000'",
                               (str(page_number),))
            store.conn.commit()
            page_number += 1
        assert ids == [f"mem_{number:05d}" for number in range(1200, -1, -1)]
        assert len(ids) == len(set(ids)) == 1201
    finally:
        if process.is_alive():
            parent.send(None)
        process.join(20)
        if process.is_alive():
            process.terminate()
            process.join(5)
        parent.close()
        store.close()


@pytest.mark.parametrize("other", ["workspace", "repo", "session", "mtype"])
def test_unrelated_scope_changes_do_not_invalidate_cursor(other):
    store = Store(":memory:")
    try:
        flt = _seed(store, repo="main")
        flt.mtypes = [MemoryType.SEMANTIC]
        _seed(store, workspace="other" if other == "workspace" else "w",
              repo="other" if other == "repo" else "main",
              scope=Scope.SESSION if other == "session" else Scope.REPO,
              session="ses_other" if other == "session" else None,
              mtype=MemoryType.PROCEDURAL if other == "mtype" else MemoryType.SEMANTIC,
              prefix="mem_other")
        first = browse_memories(store, flt, limit=1)
        store.conn.execute("UPDATE memories SET title='changed' WHERE id='mem_other_00000'")
        store.conn.commit()
        assert browse_memories(store, flt, cursor=first["next_cursor"])["total_count"] == 3
    finally:
        store.close()


@pytest.mark.parametrize("field,value", [
    ("title", "edited"), ("content", "edited"), ("summary", "edited"),
    ("valid_from", 2000000000), ("valid_to", 0), ("valid_to_recorded_at", 0),
    ("expired_at", 0), ("ingested_at", 2000000000), ("sort_order", 1),
    ("repo_id", "other"), ("session_id", "ses_other"), ("scope", "session"),
    ("mtype", "procedural"), ("pinned", 1), ("provenance", "{}"),
])
def test_relevant_mutation_requires_typed_refresh(field, value):
    store = Store(":memory:")
    try:
        flt = _seed(store, repo="main")
        first = browse_memories(store, flt, limit=1)
        store.conn.execute(f"UPDATE memories SET {field}=? WHERE id='mem_00000'", (value,))
        store.conn.commit()
        with pytest.raises(BrowseCursorStale, match="restart from the first page"):
            browse_memories(store, flt, cursor=first["next_cursor"])
    finally:
        store.close()


def test_scope_move_invalidates_both_the_old_and_new_listing():
    store = Store(":memory:")
    try:
        first_filter = _seed(store, repo="first")
        second_filter = _seed(store, repo="second", prefix="mem_second")
        first = browse_memories(store, first_filter, limit=1)
        second = browse_memories(store, second_filter, limit=1)
        store.conn.execute("UPDATE memories SET repo_id=? WHERE id='mem_00000'",
                           (second_filter.repo_id,))
        store.conn.commit()
        for flt, page in ((first_filter, first), (second_filter, second)):
            with pytest.raises(BrowseCursorStale):
                browse_memories(store, flt, cursor=page["next_cursor"])
    finally:
        store.close()


def test_legacy_v1_cursor_has_actionable_refresh():
    store = Store(":memory:")
    try:
        flt = _seed(store)
        page = browse_memories(store, flt, limit=1)
        payload = json.loads(base64.urlsafe_b64decode(page["next_cursor"]))
        payload.update(v=1, revision=[123, 1, 10])
        cursor = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
        with pytest.raises(BrowseCursorStale, match="upgraded.*restart from the first page"):
            browse_memories(store, flt, cursor=cursor)
    finally:
        store.close()


def test_cursor_freezes_temporal_anchors_and_ignores_last_access(monkeypatch):
    store = Store(":memory:")
    try:
        flt = _seed(store, count=3)
        store.conn.execute("UPDATE memories SET valid_to=150 WHERE id='mem_00000'")
        store.conn.execute("UPDATE memories SET valid_from=150 WHERE id='mem_00002'")
        store.conn.commit()
        monkeypatch.setattr(browsing.time, "time", lambda: 100.0)
        first = browse_memories(store, flt, limit=1)
        assert first["rows"][0]["id"] == "mem_00001"
        store.reinforce("mem_00000")
        monkeypatch.setattr(browsing.time, "time", lambda: 200.0)
        second = browse_memories(store, flt, cursor=first["next_cursor"])
        assert second["valid_at"] == second["known_at"] == 100.0
        assert [row["id"] for row in second["rows"]] == ["mem_00000"]
        assert second["total_count"] == 2
    finally:
        store.close()


def test_rolled_back_mutation_does_not_invalidate_committed_cursor():
    store = Store(":memory:")
    try:
        flt = _seed(store)
        first = browse_memories(store, flt, limit=1)
        store.conn.execute("UPDATE memories SET title='temporary' WHERE id='mem_00000'")
        store.conn.rollback()
        assert browse_memories(store, flt, cursor=first["next_cursor"])["total_count"] == 3
    finally:
        store.close()


def test_cursor_from_rolled_back_snapshot_cannot_match_a_different_write():
    store = Store(":memory:")
    try:
        flt = _seed(store)
        store.conn.execute("UPDATE memories SET title='temporary' WHERE id='mem_00000'")
        temporary = browse_memories(store, flt, limit=1)
        store.conn.rollback()
        store.conn.execute("UPDATE memories SET title='different' WHERE id='mem_00001'")
        store.conn.commit()
        with pytest.raises(BrowseCursorStale):
            browse_memories(store, flt, cursor=temporary["next_cursor"])
    finally:
        store.close()


def test_repaired_revision_trigger_invalidates_pre_repair_cursors(tmp_path):
    path = str(tmp_path / "repaired.db")
    store = Store(path)
    flt = _seed(store)
    first = browse_memories(store, flt, limit=1)
    store.conn.execute("DROP TRIGGER trg_browse_memory_update")
    store.conn.execute("UPDATE memories SET valid_from=0 WHERE id='mem_00002'")
    store.conn.commit()
    store.close()
    reopened = Store(path)
    try:
        with pytest.raises(BrowseCursorStale):
            browse_memories(reopened, flt, cursor=first["next_cursor"])
    finally:
        reopened.close()


def test_scope_revision_scan_uses_its_scope_index():
    store = Store(":memory:")
    try:
        flt = _seed(store, repo="main")
        plan = store.conn.execute(
            "EXPLAIN QUERY PLAN SELECT revision FROM browse_scope_revisions "
            "WHERE workspace_id=? AND repo_id=? "
            "ORDER BY workspace_id,repo_id,session_id,scope,mtype",
            (flt.workspace_id, flt.repo_id),
        ).fetchall()
        detail = " ".join(str(row[3]) for row in plan).upper()
        assert "SEARCH BROWSE_SCOPE_REVISIONS USING INDEX" in detail
        assert "TEMP B-TREE" not in detail
    finally:
        store.close()
