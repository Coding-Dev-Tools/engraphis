"""Tests for Issue #286: Long-lived MCP server recovery from DatabaseError,
stale file handles after out-of-process replacement, and lock leak prevention."""
from __future__ import annotations

import json
import logging
import os
import sqlite3

import pytest

pytest.importorskip("mcp", reason="optional 'mcp' extra not installed")

from engraphis.core.ids import new_id
from engraphis.core.interfaces import MemoryRecord, MemoryType, Scope
from engraphis.core.store import Store, _SerializedConnection
from engraphis.mcp_server import (
    _classify_gateway_exception,
    _err,
    _safe_exception_marker,
    engraphis_get_memory,
    engraphis_recall_context,
)
from engraphis.service import MemoryService


def _make_sample_record(memory_id: str, content: str = "Test memory content", wid: str = "ws_test") -> MemoryRecord:
    return MemoryRecord(
        id=memory_id,
        content=content,
        mtype=MemoryType.SEMANTIC,
        scope=Scope.WORKSPACE,
        workspace_id=wid,
    )


def test_safe_exception_marker_extracts_sqlite_error_names():
    exc_corrupt = sqlite3.DatabaseError("database disk image is malformed")
    setattr(exc_corrupt, "sqlite_errorname", "SQLITE_CORRUPT")
    assert _safe_exception_marker(exc_corrupt) == "DatabaseError: SQLITE_CORRUPT"

    exc_busy = sqlite3.OperationalError("database is locked")
    setattr(exc_busy, "sqlite_errorname", "SQLITE_BUSY")
    assert _safe_exception_marker(exc_busy) == "OperationalError: SQLITE_BUSY"

    exc_locked = sqlite3.OperationalError("database table is locked")
    setattr(exc_locked, "sqlite_errorname", "SQLITE_LOCKED")
    assert _safe_exception_marker(exc_locked) == "OperationalError: SQLITE_LOCKED"

    exc_ioerr = sqlite3.OperationalError("disk I/O error")
    setattr(exc_ioerr, "sqlite_errorname", "SQLITE_IOERR")
    assert _safe_exception_marker(exc_ioerr) == "OperationalError: SQLITE_IOERR"

    exc_timeout = sqlite3.OperationalError("store write lock timeout — a transaction appears stuck")
    assert _safe_exception_marker(exc_timeout) == "OperationalError: write_lock_timeout"

    # Fallback to class name if no special marker
    exc_value = ValueError("some generic invalid value")
    assert _safe_exception_marker(exc_value) == "ValueError"


def test_safe_exception_marker_and_err_do_not_leak_queries_or_paths(caplog):
    sensitive_path = "C:/Secret/Path/database.sqlite"
    sensitive_query = "SELECT secret_token FROM users WHERE id = 'super_secret'"
    sensitive_note = "User API token is super_secret_12345"

    exc = sqlite3.DatabaseError(
        f"malformed database file at {sensitive_path} while executing {sensitive_query}: {sensitive_note}"
    )
    setattr(exc, "sqlite_errorname", "SQLITE_CORRUPT")

    marker = _safe_exception_marker(exc)
    assert marker == "DatabaseError: SQLITE_CORRUPT"
    assert sensitive_path not in marker
    assert sensitive_query not in marker
    assert sensitive_note not in marker

    with caplog.at_level(logging.ERROR):
        result = _err(exc)

    assert isinstance(result, str)
    assert result.startswith("Error:")
    # Error message must be sanitized
    assert sensitive_path not in result
    assert sensitive_query not in result
    assert sensitive_note not in result

    # Log record should include the marker and type without leaking raw message
    log_text = caplog.text
    assert "DatabaseError: SQLITE_CORRUPT" in log_text
    assert sensitive_path not in log_text
    assert sensitive_query not in log_text
    assert sensitive_note not in log_text


def test_serialized_connection_rollback_failure_resets_pin(tmp_path):
    db_path = str(tmp_path / "rollback_failure.db")
    raw = sqlite3.connect(db_path, check_same_thread=False)

    class RawProxy:
        def __init__(self, target):
            self._target = target
            self.should_fail_rollback = False

        def __getattr__(self, name):
            return getattr(self._target, name)

        def rollback(self):
            if self.should_fail_rollback:
                raise sqlite3.OperationalError("disk I/O error during rollback")
            return self._target.rollback()

    proxy = RawProxy(raw)
    conn = _SerializedConnection(proxy)

    # Start a transaction to set pin
    conn.execute("CREATE TABLE test (x INTEGER)")
    conn.execute("BEGIN IMMEDIATE")
    assert conn._pinned() is True
    assert conn.in_transaction is True

    # Make rollback fail
    proxy.should_fail_rollback = True

    # Calling rollback on conn should handle the exception, reset pin, and release lock
    with pytest.raises(sqlite3.OperationalError, match="disk I/O error during rollback"):
        conn.rollback()

    # Verify pin is NOT stranded
    assert conn._pinned() is False

    # When rollback succeeds on retry, transaction settles and another write proceeds
    proxy.should_fail_rollback = False
    conn.rollback()
    assert conn.in_transaction is False
    conn.execute("INSERT INTO test VALUES (1)")
    conn.commit()
    conn.close()


def test_store_is_stale_and_reconnect_on_file_replacement(tmp_path):
    db_path = str(tmp_path / "stale_test.db")

    # Initial store with memory 1
    store1 = Store(db_path)
    wid1 = store1.get_or_create_workspace("ws_test")
    mem1 = _make_sample_record(new_id("memory"), "Initial memory", wid=wid1)
    store1.add_memory(mem1)
    assert store1.get_memory(mem1.id) is not None
    assert store1.is_stale() is False

    # Simulate out-of-process replacement:
    # Build a new database at tmp file and replace the original
    new_db_path = str(tmp_path / "replacement.db")
    store2 = Store(new_db_path)
    wid2 = store2.get_or_create_workspace("ws_test")
    mem2 = _make_sample_record(new_id("memory"), "Replaced memory", wid=wid2)
    store2.add_memory(mem2)
    store2.close()

    # Simulate file replacement: close store1 to allow atomic replacement on Windows
    store1.close()
    os.replace(new_db_path, db_path)

    # Re-open store1 with the old opened identity pointing to old file
    store1 = Store(db_path)
    # Simulate a stale identity
    fake_identity = (store1._opened_identity[0], (store1._opened_identity[1] or 0) + 9999)
    store1._opened_identity = fake_identity
    assert store1.is_stale() is True

    # Calling reconnect should refresh identity and clear staleness
    store1.reconnect()
    assert store1.is_stale() is False
    # Memory from replacement should now be present
    found = store1.get_memory(mem2.id)
    assert found is not None
    assert found.content == "Replaced memory"
    store1.close()


def test_store_get_memory_auto_reconnects_when_stale(tmp_path):
    db_path = str(tmp_path / "auto_reconnect_stale.db")
    store = Store(db_path)
    wid = store.get_or_create_workspace("ws_test")
    mem1 = _make_sample_record(new_id("memory"), "First memory", wid=wid)
    store.add_memory(mem1)

    # Artificially mark store as stale
    store._opened_identity = (0, 0)
    assert store.is_stale() is True

    # get_memory should detect staleness, reconnect, and find mem1
    res = store.get_memory(mem1.id)
    assert res is not None
    assert res.id == mem1.id
    assert store.is_stale() is False
    store.close()


def test_store_check_health_and_service_check_health(tmp_path):
    db_path = str(tmp_path / "health_check.db")
    service = MemoryService.create(db_path)

    store_health = service.store.check_health()
    assert store_health["status"] == "ok"
    assert store_health["responsive"] is True

    service_health = service.check_health()
    assert service_health["status"] == "ok"
    assert service_health["responsive"] is True

    # If made stale, check_health should reconnect and return ok
    service.store._opened_identity = (0, 0)
    assert service.store.is_stale() is True
    reconnected_health = service.check_health()
    assert reconnected_health["status"] == "ok"
    assert service.store.is_stale() is False

    service.close()


def test_mcp_recall_reconnects_and_retries_on_database_error(tmp_path, monkeypatch):
    db_path = str(tmp_path / "mcp_db_err.db")
    service = MemoryService.create(db_path)
    wid = service.store.get_or_create_workspace("ws_test")
    mem = _make_sample_record(new_id("memory"), "Persistent recallable memory", wid=wid)
    service.store.add_memory(mem)

    import engraphis.mcp_server as mcp_mod
    monkeypatch.setattr(mcp_mod, "service", lambda: service)

    call_count = 0
    orig_recall = service.recall

    def flaky_recall(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            err = sqlite3.DatabaseError("database disk image is malformed")
            setattr(err, "sqlite_errorname", "SQLITE_CORRUPT")
            raise err
        return orig_recall(*args, **kwargs)

    monkeypatch.setattr(service, "recall", flaky_recall)

    # First call fails with SQLITE_CORRUPT, reconnects, retries, and succeeds
    res_raw = engraphis_recall_context("recallable", workspace="ws_test")
    res = json.loads(res_raw)
    assert res.get("is_error") is not True
    assert call_count == 2
    service.close()


def test_mcp_get_memory_reconnects_and_retries_on_database_error(tmp_path, monkeypatch):
    db_path = str(tmp_path / "mcp_get_mem_err.db")
    service = MemoryService.create(db_path)
    wid = service.store.get_or_create_workspace("ws_test")
    mem = _make_sample_record(new_id("memory"), "Exact memory fetch", wid=wid)
    service.store.add_memory(mem)

    import engraphis.mcp_server as mcp_mod
    monkeypatch.setattr(mcp_mod, "service", lambda: service)

    call_count = 0
    orig_inspect = service.inspect

    def flaky_inspect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            err = sqlite3.DatabaseError("database disk image is malformed")
            setattr(err, "sqlite_errorname", "SQLITE_CORRUPT")
            raise err
        return orig_inspect(*args, **kwargs)

    monkeypatch.setattr(service, "inspect", flaky_inspect)

    res_raw = engraphis_get_memory(mem.id, workspace="ws_test")
    res = json.loads(res_raw)
    assert res.get("id") == mem.id
    assert call_count == 2
    service.close()


def test_classify_gateway_exception_maps_busy_and_locked_to_retryable():
    exc_busy = sqlite3.OperationalError("database is locked")
    setattr(exc_busy, "sqlite_errorname", "SQLITE_BUSY")
    res_busy = _classify_gateway_exception(exc_busy)
    assert res_busy.isError is True
    parsed_busy = json.loads(res_busy.content[0].text)
    assert parsed_busy["error"]["code"] == "E_RETRYABLE"
    assert parsed_busy["error"]["retryable"] is True

    exc_locked = sqlite3.OperationalError("database table is locked")
    setattr(exc_locked, "sqlite_errorname", "SQLITE_LOCKED")
    res_locked = _classify_gateway_exception(exc_locked)
    assert res_locked.isError is True
    parsed_locked = json.loads(res_locked.content[0].text)
    assert parsed_locked["error"]["code"] == "E_RETRYABLE"
    assert parsed_locked["error"]["retryable"] is True
