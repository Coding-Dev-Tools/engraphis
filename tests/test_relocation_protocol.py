"""Relocation policy runs without SQL; canonical persistence retains its bounds."""
import ast
import copy
import inspect
import json

import pytest

from engraphis.core import relocation
from engraphis.core.interfaces import (
    MemoryRecord,
    MovePlan,
    RelocationDependencies,
    RelocationHistory,
    RelocationSessionHistory,
    RelocationStore,
    Scope,
)
from engraphis.core.store import Store


class MemoryRelocationStore:
    """Independent domain adapter: dictionaries only, deliberately no connection."""

    def __init__(self):
        self.records = {
            "mem_old": MemoryRecord(
                id="mem_old", content="Earlier deployment guidance.", workspace_id="source",
                scope=Scope.WORKSPACE, session_id="session", valid_to=1.0,
            ),
            "mem_new": MemoryRecord(
                id="mem_new", content="Current deployment guidance.", workspace_id="source",
                scope=Scope.WORKSPACE, session_id="session", metadata={"supersedes": ["mem_old"]},
            ),
        }
        self.session = {
            "id": "session", "workspace_id": "source", "repo_id": None,
            "status": "summarized", "handoff": json.dumps({"refs": ["mem_old", "mem_new"]}),
        }
        self.events = [{
            "id": "event", "workspace_id": "source", "repo_id": None,
            "session_id": "session", "refs": json.dumps(["mem_new"]),
        }]
        self.ownership = [{"id": "source", "settings": "{}"}, {"id": "target", "settings": "{}"}]
        self.applied = []

    @staticmethod
    def _bounded(rows, limit):
        if len(rows) > limit:
            raise ValueError("review limit")
        return copy.deepcopy(rows)

    def get_memory(self, memory_id):
        return copy.deepcopy(self.records.get(memory_id))

    def relocation_dependencies(self, workspace_id, *, limit):
        return RelocationDependencies(memories=self._bounded([
            {"id": record.id, "repo_id": record.repo_id, "session_id": record.session_id,
             "metadata": json.dumps(record.metadata), "provenance": json.dumps(record.provenance)}
            for record in self.records.values() if record.workspace_id == workspace_id
        ], limit))

    def relocation_history(self, memory_ids, *, limit):
        return RelocationHistory(attachments={
            "memory_sync_exports": [], "memory_tombstones": [],
            "source_imports": [], "code_memory_links": [],
        })

    def relocation_session_history(self, session_id, *, limit):
        return RelocationSessionHistory(
            session=copy.deepcopy(self.session) if session_id == self.session["id"] else None,
            events=self._bounded([event for event in self.events if event["session_id"] == session_id], limit),
        )

    def relocation_workspace_events(self, workspace_id, *, limit):
        return self._bounded([event for event in self.events if event["workspace_id"] == workspace_id], limit)

    def relocation_entity(self, entity_id):
        return None

    def relocation_repo(self, repo_id):
        return None

    def relocation_repo_named(self, workspace_id, name):
        return None

    def relocation_entity_named(self, workspace_id, repo_id, name, etype):
        return None

    def relocation_canonical_entity(self, workspace_id, repo_id, normalized_name, etype):
        return None

    def relocation_edge_conflict(self, workspace_id, repo_id, src, dst, relation, layer):
        return None

    def relocation_claim_conflict(self, workspace_id, repo_id, record):
        return None

    def relocation_operation_exists(self, workspace_id, operation_id):
        return False

    def relocation_ownership(self, source_id, target_id, *, limit):
        return self._bounded(self.ownership, limit)

    def apply_memory_move(self, plan, *, actor):
        self.applied.append((plan, actor))
        for record in plan.records:
            self.records[record.id].workspace_id = plan.target_id
        self.session["workspace_id"] = plan.target_id
        for event in self.events:
            if event["id"] in {row["id"] for row in plan.events}:
                event["workspace_id"] = plan.target_id


def test_planning_and_apply_use_domain_operations_without_a_connection():
    store = MemoryRelocationStore()
    assert isinstance(store, RelocationStore)
    assert not hasattr(store, "conn")
    before = copy.deepcopy(store.records)
    plan = relocation.prepare_move(store, "source", "target", ["mem_old"])
    assert not plan.blockers
    assert {record.id for record in plan.records} == {"mem_old", "mem_new"}
    assert plan.sessions == [store.session] and plan.events == store.events
    assert plan.public("Source", "Target")["related_count"] == 1
    assert store.records == before and store.applied == []
    assert relocation.prepare_move(store, "source", "target", ["mem_old"]).preview_token == plan.preview_token
    relocation.apply_move(store, plan, actor="reviewer")
    assert store.applied == [(plan, "reviewer")]
    assert {record.workspace_id for record in store.records.values()} == {"target"}
    assert store.records["mem_old"].valid_to == before["mem_old"].valid_to
    assert store.records["mem_new"].metadata == before["mem_new"].metadata


@pytest.mark.parametrize("change", ["content", "ownership", "incoming_event"])
def test_portable_preview_binds_record_authority_and_incoming_history(change):
    store = MemoryRelocationStore()
    token = relocation.prepare_move(store, "source", "target", ["mem_old"]).preview_token
    if change == "content":
        store.records["mem_new"].content = "A changed instruction."
    elif change == "ownership":
        store.ownership[1]["settings"] = json.dumps({"visibility": "personal"})
    else:
        store.events.append({
            "id": "outside", "workspace_id": "source", "repo_id": None,
            "session_id": None, "refs": json.dumps(["mem_old"]),
        })
    refreshed = relocation.prepare_move(store, "source", "target", ["mem_old"])
    assert refreshed.preview_token != token
    if change == "incoming_event":
        assert "external_events" in {blocker["code"] for blocker in refreshed.blockers}
    assert store.applied == []


@pytest.mark.parametrize("blocker", ["active_session", "synced_memory", "external_history"])
def test_portable_blockers_refuse_before_the_writer(blocker):
    store = MemoryRelocationStore()
    if blocker == "active_session":
        store.session["status"] = "active"
    elif blocker == "synced_memory":
        store.records["mem_old"].metadata = {"synced_from_device": "peer"}
    else:
        store.records["mem_new"].metadata["corrects"] = "mem_external"
    plan = relocation.prepare_move(store, "source", "target", ["mem_old"])
    assert blocker in {item["code"] for item in plan.blockers}
    with pytest.raises(ValueError, match="preview blockers"):
        relocation.apply_move(store, plan, actor="reviewer")
    assert store.applied == []
    assert {record.workspace_id for record in store.records.values()} == {"source"}


def test_relocation_module_keeps_storage_api_and_sql_out_of_policy():
    tree = ast.parse(inspect.getsource(relocation))
    assert relocation.RelocationStore is RelocationStore
    assert "conn" not in RelocationStore.__annotations__
    assert not [node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
                and node.attr in {"conn", "execute", "executemany", "fetchone", "fetchall", "fetchmany"}]
    assert not [node.value for node in ast.walk(tree) if isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value.lstrip().upper().startswith(("SELECT ", "INSERT ", "UPDATE ", "DELETE "))]


@pytest.fixture
def sqlite_store():
    store = Store(":memory:")
    assert isinstance(store, RelocationStore)
    source = store.get_or_create_workspace("source")
    target = store.get_or_create_workspace("target")
    yield store, source, target
    store.close()


def test_canonical_scan_refuses_overflow_before_materializing_the_whole_result(sqlite_store, monkeypatch):
    store, source, _ = sqlite_store
    for index in range(10):
        store.add_memory(MemoryRecord(id=f"mem_{index}", content=f"Rule {index}", workspace_id=source))
    materialized_counts = []
    original = type(store.conn).execute

    def measured_execute(connection, *args, **kwargs):
        cursor = original(connection, *args, **kwargs)
        materialized_counts.append(len(cursor._rows))
        return cursor

    monkeypatch.setattr(type(store.conn), "execute", measured_execute)
    with pytest.raises(ValueError, match="review limit"):
        store.relocation_dependencies(source, limit=2)
    assert materialized_counts == [3]
    assert len(store.relocation_dependencies(source, limit=10).memories) == 10


def test_canonical_apply_requires_and_does_not_commit_the_callers_transaction(sqlite_store):
    store, source, target = sqlite_store
    mid = store.add_memory(MemoryRecord(id="mem_local", content="Keep history", workspace_id=source))
    plan = relocation.prepare_move(store, source, target, [mid])
    before = store.get_memory(mid)
    with pytest.raises(RuntimeError, match="caller-owned write transaction"):
        store.apply_memory_move(plan, actor="reviewer")
    assert store.get_memory(mid) == before
    store.conn.execute("BEGIN IMMEDIATE")
    store.apply_memory_move(plan, actor="reviewer")
    assert store.conn.transaction_owned_by_current_thread()
    assert store.get_memory(mid).workspace_id == target
    store.conn.rollback()
    assert store.get_memory(mid) == before
    assert store.conn.execute("SELECT 1 FROM audit WHERE action='workspace_move'").fetchone() is None
    assert store.conn.execute("SELECT 1 FROM graph_index_state").fetchone() is None


def test_canonical_writer_also_refuses_blocked_plans(sqlite_store):
    store, source, target = sqlite_store
    plan = MovePlan(source, target, [])
    plan.block("external_history", "The related history is incomplete.")
    with store.write_transaction(), pytest.raises(ValueError, match="preview blockers"):
        store.apply_memory_move(plan, actor="reviewer")
