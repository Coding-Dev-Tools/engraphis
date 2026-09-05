"""Cross-surface temporal and pagination behavior, independent of embeddings."""
import base64
import json
import time

import pytest

from engraphis.core.browsing import BrowseCursorStale
from engraphis.core.interfaces import MemoryRecord, MemoryType, Scope
from engraphis.service import MemoryService, WorkspaceBindingError


@pytest.fixture
def svc():
    service = MemoryService.create(":memory:", graph_extractor="none")
    yield service
    service.store.close()


def seed(svc, *, count=1, workspace="w"):
    wid = svc.store.get_or_create_workspace(workspace)
    for i in range(count):
        svc.store.add_memory(MemoryRecord(
            id=f"mem_{workspace}_{i:06d}", workspace_id=wid, scope=Scope.WORKSPACE,
            mtype=MemoryType.SEMANTIC if i % 2 == 0 else MemoryType.PROCEDURAL,
            content=f"distinct browse record {i}", title=f"record {i}",
            valid_from=10, ingested_at=10,
        ))
    svc.store.conn.commit()
    return wid


def test_browse_all_1201_records_and_search_oldest(svc):
    seed(svc, count=1201)
    cursor = ""
    ids = []
    while True:
        page = svc.list_memories(workspace="w", limit=100, cursor=cursor)
        assert page["total_count"] == 1201
        assert page["count"] <= 100
        ids.extend(row["id"] for row in page["memories"])
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert len(ids) == len(set(ids)) == 1201
    match = svc.list_memories(workspace="w", q="record 1200")
    assert match["total_count"] == 1
    assert match["memories"][0]["id"] == "mem_w_001200"
    assert svc.list_memories(workspace="w", mtype="procedural")["total_count"] == 600


def test_temporal_browsing_matches_canonical_history(svc):
    wid = seed(svc, count=6)
    now = time.time()
    updates = [("valid_from", now + 100, 0), ("valid_to", now + 100, 1),
               ("ingested_at", now + 100, 2), ("expired_at", now - 1, 3)]
    for field, timestamp, index in updates:
        svc.store.conn.execute(f"UPDATE memories SET {field}=? WHERE id=?",
                               (timestamp, f"mem_w_{index:06d}"))
    svc.store.conn.execute("UPDATE memories SET scope='session' WHERE id='mem_w_000004'")
    svc.store.conn.execute(
        "UPDATE memories SET valid_to=?,valid_to_recorded_at=? WHERE id='mem_w_000005'",
        (now - 10, now + 10),
    )
    svc.store.conn.commit()
    live = svc.list_memories(workspace="w", valid_at=now, known_at=now)
    assert {r["id"] for r in live["memories"]} == {"mem_w_000001", "mem_w_000005"}
    assert svc.list_memories(workspace="w", valid_at=now, known_at=now + 20)["count"] == 1
    assert wid


def test_cursors_bind_filters_and_invalidate_on_changes(svc):
    seed(svc, count=3)
    page = svc.list_memories(workspace="w", limit=1)
    with pytest.raises(ValueError, match="match the query"):
        svc.list_memories(workspace="w", q="different", cursor=page["next_cursor"])
    svc.store.conn.execute("UPDATE memories SET sort_order=1 WHERE id='mem_w_000002'")
    svc.store.conn.commit()
    with pytest.raises(BrowseCursorStale):
        svc.list_memories(workspace="w", limit=1, cursor=page["next_cursor"])


def test_browse_preserves_caller_transaction_and_workspace_binding(svc):
    seed(svc)
    svc.store.conn.execute("UPDATE memories SET title='Uncommitted' WHERE id='mem_w_000000'")
    assert svc.list_memories(workspace="w")["memories"][0]["title"] == "Uncommitted"
    assert svc.store.conn.transaction_owned_by_current_thread()
    svc.store.conn.rollback()
    assert svc.list_memories(workspace="w")["memories"][0]["title"] == "record 0"
    svc.allowed_workspaces = frozenset({"allowed"})
    svc.store.allowed_workspaces = svc.allowed_workspaces
    with pytest.raises(WorkspaceBindingError):
        svc.list_memories(workspace="w")


def test_browse_escapes_search_metacharacters(svc):
    seed(svc, count=2)
    assert svc.list_memories(workspace="w", q="%")["count"] == 0
    assert svc.list_memories(workspace="w", q="_")["count"] == 0
    assert svc.list_memories(workspace="w", q="\\")["count"] == 0


@pytest.mark.parametrize("field", ["anchors", "position"])
@pytest.mark.parametrize("magnitude", [30, 500])
def test_cursor_rejects_oversized_numeric_values_as_validation_errors(svc, field, magnitude):
    seed(svc, count=2)
    first = svc.list_memories(workspace="w", limit=1)
    payload = json.loads(base64.urlsafe_b64decode(first["next_cursor"]))
    payload[field][1] = 10 ** magnitude
    cursor = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    with pytest.raises(ValueError, match="invalid memory cursor"):
        svc.list_memories(workspace="w", limit=1, cursor=cursor)


def test_browse_http_pagination_and_stale_cursor(svc, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from engraphis.routes import v2_api

    seed(svc, count=3)
    monkeypatch.setattr(v2_api, "service", lambda: svc)
    app = FastAPI()
    app.include_router(v2_api.router)
    with TestClient(app) as client:
        first = client.get("/api/memories", params={"workspace": "w", "limit": 1})
        assert first.status_code == 200
        svc.store.conn.execute("UPDATE memories SET title='changed' WHERE id='mem_w_000000'")
        svc.store.conn.commit()
        stale = client.get("/api/memories", params={
            "workspace": "w", "limit": 1, "cursor": first.json()["next_cursor"],
        })
        assert stale.status_code == 409
        assert stale.json()["detail"]["code"] == "cursor_stale"
