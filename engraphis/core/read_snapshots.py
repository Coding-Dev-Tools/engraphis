"""Bounded, disposable live SQLite read snapshots without a shared writer lock."""
from __future__ import annotations

import logging
import math
import threading
import time
from contextlib import contextmanager
from typing import Any, Callable


logger = logging.getLogger("engraphis.core.read_snapshots")


class ReadSnapshotBusy(TimeoutError):
    """The bounded reader capacity was unavailable before the request deadline."""


class ReadSnapshotTimeout(TimeoutError):
    """The read snapshot lease expired."""


def validate_read_timeout(timeout: float) -> float:
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not 0 < timeout <= 30):
        raise ValueError("read snapshot timeout must be between 0 and 30 seconds")
    return float(timeout)


class _ReadCursor:
    def __init__(self, connection, raw):
        self._connection = connection
        self._raw = raw

    def fetchone(self):
        return self._connection._run(self._raw.fetchone)

    def fetchall(self):
        return self._connection._run(self._raw.fetchall)

    def fetchmany(self, size=1000):
        return self._connection._run(self._raw.fetchmany, size)

    def __iter__(self):
        while True:
            rows = self.fetchmany()
            if not rows:
                return
            yield from rows


class ReadSnapshotConnection:
    """Serialize individual reads and close after interrupting any active query.

    Unlike the primary Store connection, a read transaction does not hold this
    lock between statements. A lease timer can therefore release even an idle
    snapshot without closing a connection concurrently used by SQLite.
    """

    def __init__(self, raw, deadline: float, on_close: Callable[[], None]):
        self._raw = raw
        self._deadline = deadline
        self._on_close = on_close
        self._lock = threading.RLock()
        self._control_lock = threading.Lock()
        self._closed = False
        self._cancelled = False
        self._expired = False
        self._timer = threading.Timer(max(0, deadline - time.monotonic()), self._expire)
        self._timer.daemon = True

    def start(self):
        self._timer.start()
        self._run(self._raw.set_progress_handler,
                  lambda: int(time.monotonic() >= self._deadline), 1000)
        self.execute(f"PRAGMA busy_timeout={max(1, math.ceil((self._deadline - time.monotonic()) * 1000))}")
        self.execute("PRAGMA query_only=ON")
        if self.execute("PRAGMA query_only").fetchone()[0] != 1:
            raise RuntimeError("read snapshot connector did not enable query-only mode")
        self.execute("BEGIN")
        # BEGIN alone is deferred. Pin the WAL read point before handing out the view.
        self.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()

    def _check(self):
        if self._expired or time.monotonic() >= self._deadline:
            raise ReadSnapshotTimeout("memory read deadline exceeded; retry the request")
        if self._closed or self._cancelled:
            raise RuntimeError("read snapshot is closed")

    def _run(self, operation, *args):
        with self._lock:
            self._check()
            try:
                result = operation(*args)
            except Exception:
                self._check()
                raise
            self._check()
            return result

    def execute(self, *args):
        return _ReadCursor(self, self._run(self._raw.execute, *args))

    def transaction_owned_by_current_thread(self):
        return self._run(lambda: self._raw.in_transaction)

    def _expire(self):
        self._expired = True
        self.close()

    def close(self):
        self._cancelled = True
        self._timer.cancel()
        # SQLite explicitly permits interrupt from another thread. Closing itself
        # waits for the statement/fetch lock, including translated SQLCipher calls.
        with self._control_lock:
            if self._closed:
                return
            try:
                self._raw.interrupt()
            except Exception:  # noqa: BLE001 - failing injected connection
                pass
        with self._lock:
            with self._control_lock:
                if self._closed:
                    return
                self._closed = True
                try:
                    try:
                        self._raw.set_progress_handler(None, 0)
                        self._raw.rollback()
                    finally:
                        self._raw.close()
                except Exception as exc:  # noqa: BLE001 - release capacity after failed cleanup
                    logger.warning("read snapshot cleanup failed (%s)", type(exc).__name__)
                finally:
                    self._on_close()


class ReadSnapshotView:
    """Only the connection and canonical scope predicate required by browsing."""

    def __init__(self, connection: ReadSnapshotConnection, where: Callable[..., Any]):
        self.conn = connection
        self._where = where


class ReadSnapshotPool:
    def __init__(self, limit: int = 4):
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 64:
            raise ValueError("read snapshot limit must be between 1 and 64")
        self.limit = limit
        self._condition = threading.Condition()
        self._leases: dict[object, Any] = {}
        self._closed = False

    def _release(self, token):
        with self._condition:
            self._leases.pop(token, None)
            self._condition.notify_all()

    @contextmanager
    def borrow(self, opener: Callable[..., Any], *, timeout: float = 5.0):
        deadline = time.monotonic() + validate_read_timeout(timeout)
        token = object()
        with self._condition:
            while len(self._leases) >= self.limit and not self._closed:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ReadSnapshotBusy("memory readers are busy; retry the request")
                self._condition.wait(remaining)
            if self._closed:
                raise RuntimeError("read snapshot pool is closed")
            self._leases[token] = None
        reader = None
        raw = None
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ReadSnapshotTimeout("memory read deadline exceeded; retry the request")
            raw = opener(timeout=remaining)
            reader = ReadSnapshotConnection(raw, deadline, lambda: self._release(token))
            with self._condition:
                if self._closed:
                    raise RuntimeError("read snapshot pool is closed")
                self._leases[token] = reader
            reader.start()
            yield reader
            reader._check()
        finally:
            if reader is not None:
                reader.close()
            elif raw is not None:
                try:
                    raw.close()
                except Exception as exc:  # noqa: BLE001 - retain the original setup error
                    logger.warning("read snapshot setup cleanup failed (%s)", type(exc).__name__)
            self._release(token)

    def close(self):
        with self._condition:
            self._closed = True
            readers = [reader for reader in self._leases.values() if reader is not None]
            self._condition.notify_all()
        for reader in readers:
            reader.close()
