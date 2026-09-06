"""Live readers preserve WAL snapshots while writers continue independently."""
from __future__ import annotations

import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from engraphis.backends import encrypted_db
from engraphis.core import browsing
from engraphis.core.interfaces import MemoryRecord, Scope, SearchFilter
from engraphis.core.read_snapshots import ReadSnapshotBusy, ReadSnapshotTimeout
from engraphis.core.store import Store


def _seed(store, count=2):
    workspace = store.get_or_create_workspace("readers")
    for number in range(count):
        store.add_memory(MemoryRecord(
            id=f"mem_reader_{number}", workspace_id=workspace, scope=Scope.WORKSPACE,
            content=f"Evidence {number}", valid_from=1, ingested_at=1,
        ))
    return SearchFilter(workspace_id=workspace)


def test_browsing_holds_one_snapshot_without_blocking_the_store_writer(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "concurrent.db"))
    flt = _seed(store)
    pinned = threading.Event()
    resume = threading.Event()
    original = browsing._revision

    def hold(reader, selected_filter):
        result = original(reader, selected_filter)
        pinned.set()
        assert resume.wait(5)
        return result

    monkeypatch.setattr(browsing, "_revision", hold)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            future = pool.submit(browsing.browse_memories, store, flt)
            assert pinned.wait(5)
            writer = pool.submit(store.add_memory, MemoryRecord(
                id="mem_new_reader", workspace_id=flt.workspace_id,
                scope=Scope.WORKSPACE, content="Committed during browsing.",
                valid_from=1, ingested_at=1,
            ))
            try:
                assert writer.result(timeout=1) == "mem_new_reader"
            finally:
                resume.set()
            page = future.result(timeout=5)
        assert page["total_count"] == 2
        assert "mem_new_reader" not in {row["id"] for row in page["rows"]}
        monkeypatch.setattr(browsing, "_revision", original)
        assert browsing.browse_memories(store, flt)["total_count"] == 3
    finally:
        resume.set()
        store.close()


def test_live_reader_sees_wal_and_freezes_its_snapshot_without_migrating(tmp_path, monkeypatch):
    path = tmp_path / "wal.db"
    store = Store(str(path))
    _seed(store)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("read lease must not run migrations or backups")

    monkeypatch.setattr(Store, "init_schema", forbidden)
    monkeypatch.setattr(Store, "_backup_before_v4_migration", forbidden)
    try:
        assert Path(str(path) + "-wal").stat().st_size > 0
        with store.borrow_read_snapshot() as reader:
            assert reader.conn is not store.conn
            assert reader.conn.execute("PRAGMA query_only").fetchone()[0] == 1
            assert reader.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 2
            store.conn.execute("UPDATE memories SET title='new title'")
            store.conn.commit()
            assert reader.conn.execute("SELECT title FROM memories LIMIT 1").fetchone()[0] == ""
            # URI mode=ro remains the authority even if an internal caller changes
            # its connection-local query_only flag.
            reader.conn.execute("PRAGMA query_only=OFF")
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                reader.conn.execute("UPDATE memories SET title='forbidden'")
        with store.borrow_read_snapshot() as fresh:
            assert fresh.conn.execute("SELECT title FROM memories LIMIT 1").fetchone()[0] == "new title"
    finally:
        store.close()


def test_reader_cap_bounds_cross_thread_wait_and_recovers_after_release(tmp_path):
    store = Store(str(tmp_path / "capacity.db"), read_snapshot_limit=1)
    _seed(store)

    def acquire():
        with store.borrow_read_snapshot(timeout=0.05):
            raise AssertionError("reader capacity was exceeded")

    try:
        with store.borrow_read_snapshot(timeout=2):
            started = time.monotonic()
            with ThreadPoolExecutor(max_workers=1) as pool:
                with pytest.raises(ReadSnapshotBusy):
                    pool.submit(acquire).result(timeout=1)
            assert time.monotonic() - started < 1
        with store.borrow_read_snapshot(timeout=1) as reader:
            assert reader.conn.execute("SELECT 1").fetchone()[0] == 1
    finally:
        store.close()


def test_idle_lease_expires_and_releases_capacity_before_its_context_exits(tmp_path):
    store = Store(str(tmp_path / "expiry.db"), read_snapshot_limit=1)
    _seed(store)
    try:
        with pytest.raises(ReadSnapshotTimeout):
            with store.borrow_read_snapshot(timeout=0.05) as expired:
                # The second lease can start when the timer expires the first,
                # even though the first caller is still inside its context.
                with store.borrow_read_snapshot(timeout=1) as current:
                    assert current.conn.execute("SELECT 1").fetchone()[0] == 1
                expired.conn.execute("SELECT 1")
    finally:
        store.close()


def test_query_deadline_interrupts_sql_and_returns_reader_capacity(tmp_path):
    store = Store(str(tmp_path / "deadline.db"), read_snapshot_limit=1)
    _seed(store)
    try:
        started = time.monotonic()
        with pytest.raises(ReadSnapshotTimeout):
            with store.borrow_read_snapshot(timeout=0.05) as reader:
                reader.conn.execute(
                    "WITH RECURSIVE counter(x) AS (VALUES(0) UNION ALL "
                    "SELECT x+1 FROM counter WHERE x<1000000000) SELECT sum(x) FROM counter"
                ).fetchone()
        assert time.monotonic() - started < 1
        with store.borrow_read_snapshot(timeout=1) as reader:
            assert reader.conn.execute("SELECT 1").fetchone()[0] == 1
    finally:
        store.close()


def test_store_close_cancels_active_readers_and_rejects_new_leases(tmp_path):
    store = Store(str(tmp_path / "closing.db"))
    _seed(store)
    with pytest.raises(RuntimeError, match="closed"):
        with store.borrow_read_snapshot() as reader:
            store.close()
            reader.conn.execute("SELECT 1")
    with pytest.raises(RuntimeError, match="closed"):
        with store.borrow_read_snapshot():
            pass
    store.close()


@pytest.mark.parametrize("path", [":memory:", "file:reader_shared?mode=memory&cache=shared"])
def test_memory_databases_retain_the_shared_connection_contract(path):
    store = Store(path)
    try:
        _seed(store)
        with store.borrow_read_snapshot() as reader:
            assert reader is store
        with store.read_snapshot():
            assert store.conn.transaction_owned_by_current_thread()
        assert not store.conn.transaction_owned_by_current_thread()
    finally:
        store.close()


def test_caller_owned_transaction_remains_visible_and_unsettled(tmp_path):
    store = Store(str(tmp_path / "caller.db"))
    flt = _seed(store)
    try:
        store.conn.execute("UPDATE memories SET title='uncommitted'")
        with store.borrow_read_snapshot() as reader:
            assert reader is store
            assert browsing.browse_memories(reader, flt)["rows"][0]["title"] == "uncommitted"
        assert store.conn.transaction_owned_by_current_thread()
        store.conn.rollback()
        assert browsing.browse_memories(store, flt)["rows"][0]["title"] == ""
    finally:
        store.close()


def test_unsupported_custom_connector_never_reuses_immutable_open(tmp_path):
    calls = []

    def connector(path):
        calls.append(path)
        connection = sqlite3.connect(path, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        return connection

    def forbidden(_path):
        raise AssertionError("immutable inspection cannot serve a live database")

    connector.open_read_only = forbidden
    store = Store(str(tmp_path / "custom.db"), connect=connector)
    try:
        flt = _seed(store)
        with store.borrow_read_snapshot() as reader:
            assert reader is store
        assert browsing.browse_memories(store, flt)["total_count"] == 2
        assert len(calls) == 1
    finally:
        store.close()


def test_immutable_store_keeps_its_original_connection_and_sidecars_unchanged(tmp_path):
    path = tmp_path / "immutable.db"
    store = Store(str(path))
    flt = _seed(store)
    store.close()
    before = {item.name: item.read_bytes() for item in tmp_path.iterdir()}
    inspector = Store(str(path), read_only=True)
    try:
        with inspector.borrow_read_snapshot() as reader:
            assert reader is inspector
        assert browsing.browse_memories(inspector, flt)["total_count"] == 2
    finally:
        inspector.close()
    assert {item.name: item.read_bytes() for item in tmp_path.iterdir()} == before


def test_failed_opt_in_connector_releases_capacity_without_plaintext_fallback(tmp_path):
    class Connector:
        failed = True

        def __call__(self, path):
            connection = sqlite3.connect(path, check_same_thread=False)
            connection.row_factory = sqlite3.Row
            return connection

        def open_read_snapshot(self, path, *, timeout):
            if self.failed:
                raise encrypted_db.EncryptionError("injected key failure")
            connection = sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True,
                                         timeout=timeout, check_same_thread=False)
            connection.row_factory = sqlite3.Row
            return connection

    connector = Connector()
    store = Store(str(tmp_path / "opt-in.db"), connect=connector, read_snapshot_limit=1)
    try:
        _seed(store)
        with pytest.raises(encrypted_db.EncryptionError):
            with store.borrow_read_snapshot():
                pass
        connector.failed = False
        with store.borrow_read_snapshot() as reader:
            assert reader.conn.execute("SELECT 1").fetchone()[0] == 1
    finally:
        store.close()


def test_reader_keeps_the_original_database_after_working_directory_changes(tmp_path, monkeypatch):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    monkeypatch.chdir(first)
    store = Store("relative.db")
    flt = _seed(store)
    monkeypatch.chdir(second)
    try:
        assert browsing.browse_memories(store, flt)["total_count"] == 2
        assert not (second / "relative.db").exists()
    finally:
        store.close()


def test_sqlcipher_live_connector_uses_readonly_wal_uri_and_keys_first(tmp_path):
    calls = []
    statements = []
    path = tmp_path / "keyed.db"
    path.touch()

    class Raw:
        def execute(self, sql):
            statements.append(sql)
            return self

        def fetchone(self):
            return (1,)

    class Driver:
        Row = sqlite3.Row

        @staticmethod
        def connect(target, **options):
            calls.append((target, options))
            return Raw()

    connector = encrypted_db._EncryptedConnector(Driver, "PRAGMA key = 'fixture'")
    connector.open_read_snapshot(str(path), timeout=0.5)
    target, options = calls[0]
    assert target.endswith("?mode=ro")
    assert "immutable" not in target
    assert options == {"timeout": 0.5, "check_same_thread": False, "uri": True}
    assert statements == ["PRAGMA key = 'fixture'", "PRAGMA query_only=ON",
                          "SELECT count(*) FROM sqlite_master"]
