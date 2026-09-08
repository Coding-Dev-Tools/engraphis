"""Startup transforms finish with transactional markers and verified recovery data."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from engraphis.core.interfaces import MemoryRecord, Scope
from engraphis.core.schema import SCHEMA_VERSION
from engraphis.core.store import Store


TRANSFORMS = {("edge_supports", 1), ("live_edge_deduplication", 1)}


def _executions(connection):
    return {tuple(row) for row in connection.execute(
        "SELECT name,version FROM migration_executions"
    ).fetchall()}


def _prepare_upgrade(path, *, previous_version):
    store = Store(str(path))
    try:
        workspace = store.get_or_create_workspace("startup")
        store.conn.execute("DROP INDEX idx_edge_workspace_live_unique")
        store.conn.execute("DROP INDEX idx_edge_repo_live_unique")
        for number in (1, 2):
            memory_id = f"mem_source_{number}"
            store.add_memory(MemoryRecord(
                id=memory_id, workspace_id=workspace, scope=Scope.WORKSPACE,
                content=f"Supported fact {number}", valid_from=10, ingested_at=10,
            ), commit=False)
            store.conn.execute(
                "INSERT INTO edges(id,workspace_id,src,dst,relation,layer,"
                "valid_from,ingested_at,provenance) VALUES (?,?,?,?,?,?,?,?,?)",
                (f"edg_old_{number}", workspace, "a", "b", "related", "semantic",
                 10, 10, json.dumps({"source": "structured", "memory_id": memory_id})),
            )
        store.conn.execute("DELETE FROM migration_executions")
        store.conn.execute("DELETE FROM schema_migrations WHERE version>?", (previous_version,))
        store.conn.commit()
    finally:
        store.close()


def test_reopen_does_not_repeat_completed_graph_transforms(tmp_path, monkeypatch):
    path = str(tmp_path / "reopen.db")
    Store(path).close()

    def unexpected(_self):
        raise AssertionError("completed startup transform repeated")

    monkeypatch.setattr(Store, "_backfill_edge_supports", unexpected)
    monkeypatch.setattr(Store, "_deduplicate_live_edges", unexpected)
    reopened = Store(path)
    try:
        assert TRANSFORMS <= _executions(reopened.conn)
    finally:
        reopened.close()
    assert not list(tmp_path.glob("*.bak"))


@pytest.mark.parametrize("previous_version", [17, SCHEMA_VERSION])
@pytest.mark.parametrize("failure_type", [RuntimeError, KeyboardInterrupt])
def test_interrupted_transform_restores_state_and_retries_once(
        tmp_path, monkeypatch, previous_version, failure_type):
    path = tmp_path / "restart.db"
    _prepare_upgrade(path, previous_version=previous_version)
    original = Store._deduplicate_live_edges

    def interrupted(self):
        original(self)
        raise failure_type("injected startup interruption")

    monkeypatch.setattr(Store, "_deduplicate_live_edges", interrupted)
    with pytest.raises(failure_type, match="injected startup interruption"):
        Store(str(path))
    with sqlite3.connect(path) as raw:
        assert raw.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert raw.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == previous_version
        assert not _executions(raw)
        assert raw.execute("SELECT COUNT(*) FROM edge_supports").fetchone()[0] == 0
        assert raw.execute("SELECT COUNT(*) FROM edges WHERE valid_to IS NULL").fetchone()[0] == 2
    backup = Path(f"{path}.pre-migration-v{SCHEMA_VERSION}.bak")
    assert backup.is_file()
    with sqlite3.connect(backup) as raw:
        assert raw.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert raw.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == previous_version
        assert raw.execute("SELECT COUNT(*) FROM edge_supports").fetchone()[0] == 0
    digest = hashlib.sha256(backup.read_bytes()).hexdigest()

    monkeypatch.setattr(Store, "_deduplicate_live_edges", original)
    restarted = Store(str(path))
    try:
        assert TRANSFORMS <= _executions(restarted.conn)
        assert restarted.schema_version == SCHEMA_VERSION
        live = restarted.conn.execute("SELECT id FROM edges WHERE valid_to IS NULL").fetchall()
        assert len(live) == 1
        assert {row[0] for row in restarted.conn.execute(
            "SELECT memory_id FROM edge_supports WHERE edge_id=? AND valid_to IS NULL",
            (live[0][0],),
        ).fetchall()} == {"mem_source_1", "mem_source_2"}
    finally:
        restarted.close()

    def unexpected(_self):
        raise AssertionError("completed transform repeated after recovery")

    monkeypatch.setattr(Store, "_backfill_edge_supports", unexpected)
    monkeypatch.setattr(Store, "_deduplicate_live_edges", unexpected)
    Store(str(path)).close()
    assert hashlib.sha256(backup.read_bytes()).hexdigest() == digest
