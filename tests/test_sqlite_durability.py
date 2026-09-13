"""SQLite policy and disposable-file failures; never hardware power-failure proof."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import textwrap

import pytest

from engraphis import config
from engraphis.core.engine import MemoryEngine
from engraphis.core.store import Store
from engraphis.factory import backend_health, create_memory_engine
from engraphis.service import MemoryService


@pytest.mark.parametrize("mode,expected", [("durable", "FULL"), ("balanced", "NORMAL")])
@pytest.mark.parametrize("factory", [Store, create_memory_engine, MemoryEngine.create,
                                     MemoryService.create])
def test_public_builders_apply_and_report_writable_durability(tmp_path, factory, mode, expected):
    resource = factory(str(tmp_path / "policy.db"), sqlite_durability=mode)
    store = resource if isinstance(resource, Store) else resource.store
    try:
        health = store.durability_health()
        assert health == {
            "configured": mode, "effective": mode, "file_backed": True,
            "read_only": False, "journal_mode": "wal", "synchronous": expected,
            "matches_requested": True,
        }
        if isinstance(resource, MemoryService):
            assert resource.stats()["sqlite_durability"] == health
        elif isinstance(resource, MemoryEngine):
            assert backend_health(resource)["sqlite_durability"] == health
    finally:
        resource.close()


def test_service_uses_settings_and_explicit_override(tmp_path, monkeypatch):
    monkeypatch.setenv("ENGRAPHIS_SQLITE_DURABILITY", "balanced")
    settings = config.Settings()
    assert settings.sqlite_durability == "balanced"
    monkeypatch.setattr(config, "settings", settings)
    configured = MemoryService.create(str(tmp_path / "configured.db"))
    explicit = MemoryService.create(str(tmp_path / "explicit.db"), sqlite_durability="durable")
    try:
        assert configured.store.durability_health()["effective"] == "balanced"
        assert explicit.store.durability_health()["effective"] == "durable"
    finally:
        configured.close()
        explicit.close()


def test_default_policy_is_durable(tmp_path, monkeypatch):
    monkeypatch.delenv("ENGRAPHIS_SQLITE_DURABILITY", raising=False)
    assert config.Settings().sqlite_durability == "durable"
    with Store(str(tmp_path / "default.db")) as store:
        assert store.durability_health()["effective"] == "durable"


@pytest.mark.parametrize("invalid", ["", "normal", "fast", "off", None, True])
def test_invalid_policy_rejected_before_open_or_service_migration(tmp_path, monkeypatch, invalid):
    opened = []
    monkeypatch.setattr("engraphis.service._auto_migrate_v1_if_needed", opened.append)
    path = tmp_path / "not-created" / "invalid.db"
    with pytest.raises(ValueError, match="sqlite_durability"):
        Store(str(path), sqlite_durability=invalid, connect=opened.append)
    if invalid is not None:  # None is the service's documented Settings selection.
        with pytest.raises(ValueError, match="sqlite_durability"):
            MemoryService.create(str(path), sqlite_durability=invalid)
    with pytest.raises(ValueError, match="ENGRAPHIS_SQLITE_DURABILITY"):
        config.Settings(sqlite_durability=invalid)
    assert opened == []
    assert not path.parent.exists()


@pytest.mark.parametrize("mode", ["durable", "balanced"])
def test_memory_storage_never_claims_persistence(mode):
    with Store(sqlite_durability=mode) as store:
        health = store.durability_health()
        assert health["effective"] == "memory"
        assert health["file_backed"] is False
        assert health["matches_requested"] is None


class RecordingConnector:
    def __init__(self):
        self.statements = []
        self.closed = False

    def __call__(self, path):
        conn = sqlite3.connect(path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.set_trace_callback(self.statements.append)
        return conn

    def open_read_only(self, path):
        conn = sqlite3.connect(Path(path).as_uri() + "?mode=ro&immutable=1", uri=True)
        conn.row_factory = sqlite3.Row
        conn.set_trace_callback(self.statements.append)
        return conn

    def close(self):
        self.closed = True


@pytest.mark.parametrize("injected", [False, True])
def test_read_only_inspection_preserves_file_and_durability_pragmas(tmp_path, injected):
    path = tmp_path / "inspect.db"
    connector = RecordingConnector() if injected else None
    with Store(str(path), connect=connector, sqlite_durability="balanced") as store:
        store.get_or_create_workspace("kept")
        store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    paths = [path, Path(str(path) + "-wal"), Path(str(path) + "-shm")]

    def state():
        return {p.name: (p.read_bytes(), p.stat().st_mtime_ns) if p.exists() else None
                for p in paths}

    before = state()
    if connector is not None:
        connector.statements.clear()
    with Store(str(path), connect=connector, read_only=True) as store:
        assert store.durability_health()["effective"] == "read_only"
        assert store.durability_health()["matches_requested"] is None
        assert store.conn.execute("SELECT name FROM workspaces").fetchone()[0] == "kept"
    assert state() == before
    if connector is not None:
        assert not connector.closed
        assert not any("synchronous=" in statement.lower() or "journal_mode=" in statement.lower()
                       for statement in connector.statements)


def test_injected_writable_connector_keeps_ownership_and_reports_effective_drift(tmp_path):
    connector = RecordingConnector()
    with Store(str(tmp_path / "injected.db"), connect=connector) as store:
        assert store.durability_health()["effective"] == "durable"
        store.conn.execute("PRAGMA synchronous=NORMAL")
        health = store.durability_health()
        assert health["configured"] == "durable"
        assert health["effective"] == "balanced"
        assert health["matches_requested"] is False
        assert str(tmp_path) not in json.dumps(health)
    assert connector.closed is False


def test_unapplied_writer_policy_fails_startup_and_closes_connection(tmp_path):
    class IgnoredSynchronization(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if sql == "PRAGMA synchronous=FULL":
                sql = "PRAGMA synchronous=NORMAL"
            return super().execute(sql, parameters)

    connections = []

    def connect(path):
        conn = sqlite3.connect(path, factory=IgnoredSynchronization)
        conn.row_factory = sqlite3.Row
        connections.append(conn)
        return conn

    with pytest.raises(RuntimeError, match="did not apply the requested WAL durability"):
        Store(str(tmp_path / "refused.db"), connect=connect)
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connections[0].execute("SELECT 1")


def test_diagnostics_do_not_commit_a_caller_owned_write(tmp_path):
    engine = create_memory_engine(str(tmp_path / "transaction.db"), auto_evolve=False)
    workspace = engine.store.get_or_create_workspace("transaction")
    observer = sqlite3.connect(str(tmp_path / "transaction.db"))
    try:
        engine.store.conn.execute("BEGIN IMMEDIATE")
        memory_id = engine.remember("A staged fact", workspace_id=workspace)
        assert engine.store.durability_health()["effective"] == "durable"
        assert backend_health(engine)["sqlite_durability"]["matches_requested"] is True
        assert engine.store.conn.transaction_owned_by_current_thread()
        assert observer.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
        engine.store.conn.rollback()
        assert engine.store.get_memory(memory_id) is None
    finally:
        observer.close()
        engine.close()


def test_abrupt_process_exit_preserves_committed_bundle_only(tmp_path):
    """os._exit skips all teardown; it does not simulate a power failure."""
    path = tmp_path / "crash.db"
    script = textwrap.dedent("""
        import json, os, sys
        from engraphis.factory import create_memory_engine
        engine = create_memory_engine(sys.argv[1], auto_evolve=False)
        store = engine.store
        workspace = store.get_or_create_workspace("crash")
        with store.write_transaction():
            committed = engine.remember("Acknowledged durable fact", workspace_id=workspace)
            store.record_receipt("remember", workspace_id=workspace, target_count=1)
        store.conn.execute("BEGIN IMMEDIATE")
        staged = engine.remember("Unacknowledged staged fact", workspace_id=workspace)
        store.record_receipt("remember", workspace_id=workspace, target_count=1)
        print(json.dumps({"committed": committed, "staged": staged}), flush=True)
        os._exit(93)
    """)
    env = dict(os.environ)
    env.update(ENGRAPHIS_ENV="test", ENGRAPHIS_DB_PATH=str(path),
               ENGRAPHIS_REQUIRE_EXACT_BACKENDS="false")
    run = subprocess.run([sys.executable, "-c", script, str(path)], capture_output=True,
                         text=True, timeout=30, env=env,
                         cwd=str(Path(__file__).resolve().parents[1]))
    assert run.returncode == 93, run.stderr
    written = json.loads(run.stdout)
    with Store(str(path)) as store:
        assert store.get_memory(written["committed"]).content == "Acknowledged durable fact"
        assert store.get_memory(written["staged"]) is None
        assert [row[0] for row in store.conn.execute("SELECT id FROM mem_vectors")] == [
            written["committed"]]
        assert store.conn.execute("SELECT COUNT(*) FROM operation_receipts").fetchone()[0] == 1
        assert {mid for mid, _ in store.fts_search("Acknowledged durable fact")} == {
            written["committed"]}
        assert store.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert store.conn.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("mode", ["durable", "balanced"])
def test_sqlite_full_rolls_back_failed_bundle_and_allows_later_writes(tmp_path, mode):
    """A database page limit raises SQLITE_FULL without filling the host disk."""
    engine = create_memory_engine(str(tmp_path / "full.db"), auto_evolve=False,
                                  sqlite_durability=mode)
    store = engine.store
    try:
        workspace = store.get_or_create_workspace("full")
        kept = engine.remember("Existing acknowledged fact", workspace_id=workspace)
        original_max = store.conn.execute("PRAGMA max_page_count").fetchone()[0]
        pages = store.conn.execute("PRAGMA page_count").fetchone()[0]
        store.conn.execute(f"PRAGMA max_page_count={pages}")
        with pytest.raises(sqlite3.OperationalError, match="full"):
            with store.write_transaction():
                store.record_receipt("remember", workspace_id=workspace, target_count=1)
                engine.remember("Uncommitted large record " + "capacity " * 100_000,
                                workspace_id=workspace, resolve_conflicts=False)
        assert not store.conn.transaction_owned_by_current_thread()
        assert store.count_memories() == 1
        assert store.get_memory(kept).content == "Existing acknowledged fact"
        assert store.conn.execute("SELECT COUNT(*) FROM mem_vectors").fetchone()[0] == 1
        assert store.conn.execute("SELECT COUNT(*) FROM operation_receipts").fetchone()[0] == 0
        assert store.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        store.conn.execute(f"PRAGMA max_page_count={original_max}")
        assert engine.remember("Successful retry after capacity recovery", workspace_id=workspace)
        assert store.count_memories() == 2
    finally:
        engine.close()
