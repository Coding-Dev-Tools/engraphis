"""Engraphis v2 store — SQLite implementation of the memory/graph/event layer.

A thin, dependency-light persistence layer over the §12 schema. It deliberately
does *not* own retrieval scoring (that is the recall engine, Phase 1) — it owns
durable state and the primitives the engines need: scoped + bi-temporal reads,
vector storage, full-text, the knowledge graph, sessions, and an audit trail.

Connections use WAL + foreign keys. Vectors are stored L2-normalized so the
NumPy reference index can use a dot product as cosine similarity.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

import numpy as np

from engraphis.core import ids
from engraphis.core.graph_layers import infer_graph_layer, normalize_graph_layer
from engraphis.core.interfaces import (
    Edge,
    GraphLayer,
    MemoryRecord,
    MemoryType,
    Node,
    Scope,
    SearchFilter,
)
from engraphis.core.schema import (
    FTS_SQL_FALLBACK,
    FTS_SQL_FTS5,
    SCHEMA_SQL,
    SCHEMA_VERSION,
)


# Rows materialized per locked batch when streaming the vector table (see iter_vectors).
VECTOR_SCAN_BATCH = 2000
# Bound placeholders per ``IN (...)`` so a batched lookup stays under SQLite's
# SQLITE_MAX_VARIABLE_NUMBER (999 before 3.32, 32766 after) on every build.
IN_CLAUSE_CHUNK = 500


def now_ts() -> float:
    return time.time()


def _escape_like(value: str) -> str:
    """Escape LIKE wildcards so ``%``/``_``/``\\`` in user input match literally.

    Mirrors ``MemoryService._successor_of``; every call site must pair it with
    ``ESCAPE '\\'``. The escape character itself is escaped first, which the service
    helper omits (harmless there — it matches ULIDs — but wrong in general)."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _dumps(obj: Any) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    except RecursionError:
        return "{}"


def _loads(raw: Any, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError, RecursionError):
        return default


def _provenance_memory_ids(provenance: Any) -> list[str]:
    if not isinstance(provenance, dict):
        return []
    values = [provenance.get("memory_id")]
    many = provenance.get("memory_ids")
    if isinstance(many, set):
        # Sets are tolerated for compatibility but have no declared order. Sort them
        # so they cannot make persisted provenance vary across interpreter processes.
        values.extend(sorted(many, key=lambda value: str(value)))
    elif isinstance(many, (list, tuple)):
        values.extend(many)
    out: list[str] = []
    for value in values:
        mid = str(value or "")
        if mid and mid not in out:
            out.append(mid)
    return out


def _merge_edge_provenance(values: Iterable[Any], *, merged_ids: Iterable[str] = ()) -> dict:
    """Merge compatibility provenance while normalized supports remain authoritative."""
    documents = [value for value in values if isinstance(value, dict)]
    merged = dict(documents[0]) if documents else {}
    memory_ids: list[str] = []
    sources: set[str] = set()
    confidences: list[float] = []
    for document in documents:
        for key, value in document.items():
            merged.setdefault(key, value)
        for memory_id in _provenance_memory_ids(document):
            if memory_id not in memory_ids:
                memory_ids.append(memory_id)
        source = str(document.get("source") or "")
        if source:
            sources.add(source)
        try:
            if document.get("confidence") is not None:
                confidences.append(float(document["confidence"]))
        except (TypeError, ValueError):
            pass
    if memory_ids:
        # ``memory_id`` is the declared primary source, not the lexicographically
        # smallest ULID. ULIDs created in one millisecond do not have a meaningful
        # random-suffix order, so sorting here could silently change provenance.
        merged["memory_id"] = memory_ids[0]
        merged["memory_ids"] = memory_ids
    if sources:
        merged.setdefault("source", sorted(sources)[0])
        if len(sources) > 1:
            merged["sources"] = sorted(sources)
    if confidences:
        merged["confidence"] = max(confidences)
    merged_from = sorted({str(value) for value in merged_ids if value})
    if merged_from:
        merged["canonical_deduplicated_from"] = merged_from
    return merged


def normalize_entity_name(value: str) -> str:
    """Conservative canonicalization key used by schema v4.

    It deliberately performs no fuzzy or semantic matching: exact Unicode NFKC,
    case-folded, whitespace-normalized variants may share a canonical entity, while
    punctuation, type, and workspace remain hard boundaries.  Preserving punctuation is
    important for names such as ``C++``/``C#`` and ``AT&T``/``ATT``; deleting it would
    silently conflate distinct entities.
    """
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"\s+", " ", text).strip()


_SUPPORT_CONFIDENCE = {
    "manual": 1.0,
    "schema": 1.0,
    "structured": 0.80,
    "regex_proximity": 0.55,
    "legacy_unknown": 0.50,
    "co_occurrence": 0.25,
}


def _edge_source_kind(provenance: Any, relation: str = "") -> str:
    if relation == "co_occurs":
        return "co_occurrence"
    if not isinstance(provenance, dict):
        return "legacy_unknown"
    raw = str(
        provenance.get("source_kind") or provenance.get("source") or ""
    ).casefold()
    if "manual" in raw:
        return "manual"
    if "schema" in raw:
        return "schema"
    if "structured" in raw:
        return "structured"
    if "regex" in raw or "proximity" in raw or "backfill" in raw:
        return "regex_proximity"
    return "legacy_unknown"


def _edge_support_confidence(provenance: Any, source_kind: str) -> float:
    raw = provenance.get("confidence") if isinstance(provenance, dict) else None
    try:
        if raw is not None:
            return max(0.0, min(1.0, float(raw)))
    except (TypeError, ValueError):
        pass
    return _SUPPORT_CONFIDENCE.get(source_kind, 0.50)


def _receipt_metadata(metadata: dict) -> dict:
    """Keep receipt metadata useful but content-free and bounded."""
    allowed = {
        "mtype", "scope", "resolution", "retention", "extracted", "intent", "k",
        "result_count", "grounded", "citations", "relation", "layer", "graph_layers",
        "files_scanned", "files_indexed", "files_removed", "symbols", "edges",
        "entities", "relations", "tables", "dry_run", "error_count",
        "entities_added", "relations_added",
    }
    out: dict[str, Any] = {}
    for key in sorted(metadata, key=lambda item: str(item))[:24]:
        safe_key = str(key)[:64]
        if safe_key not in allowed:
            continue
        value = metadata[key]
        if isinstance(value, bool) or value is None:
            out[safe_key] = value
        elif isinstance(value, (int, float)):
            out[safe_key] = value
        elif isinstance(value, str):
            out[safe_key] = (
                value[:80] if len(value) <= 80
                else "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()
            )
        elif isinstance(value, (list, tuple)):
            out[safe_key] = len(value)
    return out


def _fts5_available(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS _fts_probe USING fts5(x)")
        conn.execute("DROP TABLE IF EXISTS _fts_probe")
        return True
    except sqlite3.OperationalError:
        return False


def memory_matches_filter(rec: MemoryRecord, flt: Optional[SearchFilter], *,
                          at: Optional[float] = None,
                          include_invalid: bool = False) -> bool:
    """Return whether ``rec`` is visible under the same rules as :meth:`Store._where`.

    This is shared by the defensive recall check and sqlite-vec's post-filter so the
    accelerated and NumPy retrieval paths cannot drift on hierarchy semantics.
    """
    if flt:
        if flt.workspace_id and rec.workspace_id != flt.workspace_id:
            return False
        if flt.include_ancestors:
            if flt.session_id:
                if rec.scope == Scope.SESSION:
                    if rec.session_id != flt.session_id:
                        return False
                elif rec.scope == Scope.REPO:
                    if not flt.repo_id or rec.repo_id != flt.repo_id:
                        return False
                elif rec.scope not in (Scope.WORKSPACE, Scope.USER):
                    return False
            elif flt.repo_id:
                if rec.scope == Scope.SESSION:
                    return False
                if rec.scope == Scope.REPO and rec.repo_id != flt.repo_id:
                    return False
                if rec.scope not in (Scope.REPO, Scope.WORKSPACE, Scope.USER):
                    return False
            elif rec.scope == Scope.SESSION:
                # A workspace/global recall has no session context and must not leak
                # transient working state from every session in that container.
                return False
        else:
            if flt.repo_id and rec.repo_id != flt.repo_id:
                return False
            if flt.session_id and rec.session_id != flt.session_id:
                return False
        if flt.scopes and rec.scope not in flt.scopes:
            return False
        if flt.mtypes and rec.mtype not in flt.mtypes:
            return False
    if include_invalid:
        return True
    t = at if at is not None else (
        flt.as_of if flt and flt.as_of is not None else now_ts()
    )
    if rec.expired_at is not None:
        return False
    if rec.valid_from is not None and rec.valid_from > t:
        return False
    if rec.valid_to is not None and t >= rec.valid_to:
        return False
    return True


class _SerializedConnection:
    """Serializes access to one sqlite3 connection shared across threads.

    The Store opens a SINGLE connection with ``check_same_thread=False`` and shares it
    across the threadpool FastAPI runs sync handlers on. A bare sqlite3 connection is not
    safe for concurrent multi-thread use: interleaved statements corrupt cursors, and —
    because a connection has ONE transaction — one thread's ``commit()``/``rollback()``
    lands on another thread's uncommitted writes, so a rollback can silently discard them.
    (Per-thread connections are not an option: the sqlite-vec extension and FTS state are
    loaded into THIS connection, and a ``:memory:`` DB can't be shared across connections
    at all.)

    This wrapper holds a reentrant lock for the DURATION of each write transaction —
    pinned on the first statement that opens one (detected via ``in_transaction``) and
    released on commit/rollback — so transactions never interleave. Read-only statements
    lock only for the individual call. Two safety nets keep a stuck transaction from
    deadlocking the process: a statement that raises while a transaction is open rolls it
    back and frees the pin, and lock acquisition times out (raising, not blocking forever).
    Non-statement attributes/methods (``in_transaction``, ``enable_load_extension`` at
    setup, ...) pass straight through.
    """

    _ACQUIRE_TIMEOUT = 60.0

    def __init__(self, raw) -> None:
        object.__setattr__(self, "_raw", raw)
        object.__setattr__(self, "_lock", threading.RLock())
        object.__setattr__(self, "_pin", threading.local())

    def __getattr__(self, name):
        return getattr(self._raw, name)

    def __setattr__(self, name, value):
        setattr(self._raw, name, value)

    def _pinned(self) -> bool:
        return getattr(self._pin, "held", False)

    def transaction_owned_by_current_thread(self) -> bool:
        """Whether this thread owns the connection's currently pinned transaction.

        ``sqlite3.Connection.in_transaction`` is connection-global: it is also true when
        a *different* thread owns the transaction and this thread is waiting on ``_lock``.
        Multi-statement Store operations use this thread-local view to decide whether they
        must open and settle their own transaction after that waiter is released.
        """
        return self._pinned()

    def _acquire(self) -> None:
        if not self._lock.acquire(timeout=self._ACQUIRE_TIMEOUT):
            raise sqlite3.OperationalError(
                "store write lock timeout — a transaction appears stuck")

    def _run(self, fn, *a, **k):
        was_pinned = self._pinned()           # already inside an ongoing transaction?
        self._acquire()
        try:
            result = fn(*a, **k)
        except BaseException:
            if not was_pinned and self._raw.in_transaction:
                # This statement OPENED a transaction and then failed (e.g. a single write
                # that hit a UNIQUE violation). Nothing else is in that transaction, so roll
                # it back and release cleanly. Leaving it open would pin the lock forever —
                # stalling every other thread and handing this thread's NEXT request a stale
                # open transaction.
                try:
                    self._raw.rollback()
                except Exception:  # noqa: BLE001 — best-effort cleanup
                    pass
                self._lock.release()          # this call's acquire; no pin was established
            else:
                # A transaction was already open before this call (multi-statement: the
                # caller may catch this and continue — e.g. probing an optional table).
                # Preserve it; sqlite keeps a failed statement's transaction intact.
                self._settle()
            raise
        self._settle()
        return result

    def _settle(self) -> None:
        """After a statement, hold exactly one pinned lock acquire for this thread while a
        write transaction is open (released on commit/rollback); otherwise release this
        call's acquire so read-only statements don't hold the lock."""
        if self._raw.in_transaction:
            if self._pinned():
                self._lock.release()          # already pinned; drop this call's acquire
            else:
                self._pin.held = True         # keep this acquire as the transaction pin
        elif self._pinned():
            # A statement closed the pinned transaction WITHOUT going through commit()/
            # rollback() — e.g. executescript's implicit commit, or a raw COMMIT/END. Clear
            # the pin and release both its acquire and this call's, so it can't leak.
            self._pin.held = False
            self._lock.release()              # release the pin's acquire
            self._lock.release()              # release this call's acquire
        else:
            self._lock.release()              # no open transaction; release now

    def _finish(self, fn):
        self._acquire()
        try:
            fn()
        finally:
            if self._pinned():
                self._pin.held = False
                self._lock.release()          # release the transaction pin
            self._lock.release()              # release this call's acquire

    def execute(self, *a, **k):
        return self._run(self._raw.execute, *a, **k)

    def fetchall(self, *a, **k):
        """Execute and drain a read in ONE locked section.

        ``execute()`` returns a live cursor and releases the lock before the caller
        fetches, so anything that holds that cursor open across other work (a generator
        yielding row-by-row, e.g. ``Store.iter_vectors``) lets another thread's write
        interleave with an in-flight read on the shared connection — exactly what this
        wrapper exists to prevent. Reads that must be atomic use this instead."""
        return self._run(lambda *aa, **kk: self._raw.execute(*aa, **kk).fetchall(), *a, **k)

    def executemany(self, *a, **k):
        return self._run(self._raw.executemany, *a, **k)

    def executescript(self, *a, **k):
        return self._run(self._raw.executescript, *a, **k)

    def commit(self):
        self._finish(self._raw.commit)

    def rollback(self):
        self._finish(self._raw.rollback)

    def close(self):
        self._raw.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        return False


class Store:
    """A connection to one Engraphis v2 database (one file, or ``:memory:``)."""

    def __init__(self, path: str = ":memory:", *,
                 allowed_workspaces: Optional[set] = None,
                 connect: Optional[Callable[[str], Any]] = None) -> None:
        self.path = path
        self._connect = connect
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        raw_conn = self._open_connection(path)
        # Serialize the shared connection so concurrent threadpool handlers can't interleave
        # transactions on it (see _SerializedConnection). All Store/service/backend access
        # goes through self.conn, so wrapping here covers every writer.
        self.conn = _SerializedConnection(raw_conn)
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.has_fts5 = False
        self._receipt_lock = threading.Lock()
        self.allowed_workspaces: Optional[frozenset] = (
            frozenset(allowed_workspaces) if allowed_workspaces else None
        )
        try:
            self.init_schema()
            # journal_mode is persistent state, so set it only after a required backup
            # and the transactional migration have completed successfully.
            self.conn.execute("PRAGMA journal_mode=WAL")
        except BaseException:
            try:
                if self.conn.in_transaction:
                    self.conn.rollback()
            finally:
                self.conn.close()
            raise

    def _open_connection(self, path: str):
        """Open *path* with the primary database's connection semantics."""
        if self._connect is not None:
            # Injected factories own opening, keying, row_factory, and exception
            # translation (notably the SQLCipher backend).
            return self._connect(path)
        conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _raw_connection(conn):
        """Unwrap core/backend adapters for sqlite3's type-checked backup API."""
        seen: set[int] = set()
        while hasattr(conn, "_raw") and id(conn) not in seen:
            seen.add(id(conn))
            conn = getattr(conn, "_raw")
        return conn

    @staticmethod
    def _quick_check(conn) -> bool:
        rows = conn.execute("PRAGMA quick_check").fetchall()
        return len(rows) == 1 and str(rows[0][0]).casefold() == "ok"

    @staticmethod
    def _same_file(left, right) -> bool:
        return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)

    @staticmethod
    def _checked_backup_file(path: str, *, allow_missing: bool = False):
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            if allow_missing:
                return None
            raise
        attributes = getattr(info, "st_file_attributes", 0)
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        if (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode)
                or (reparse and attributes & reparse)
                or getattr(info, "st_nlink", 1) != 1):
            raise RuntimeError("schema backup path is not a private regular file")
        return info

    @staticmethod
    def _fsync_backup_parent(path: str) -> None:
        if os.name == "nt":
            return
        descriptor = os.open(
            str(Path(path).parent), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _logical_digest(conn) -> str:
        digest = hashlib.sha256()
        for statement in conn.iterdump():
            digest.update(statement.encode("utf-8"))
            digest.update(b"\n")
        return digest.hexdigest()

    def _cleanup_v4_backup_temps(self, backup_path: str) -> None:
        stable = Path(backup_path)
        pattern = re.compile(
            r"^%s\.tmp-[0-9]+-[0-9]+-[0-9]+$" % re.escape(stable.name))
        try:
            entries = tuple(stable.parent.iterdir())
        except OSError:
            return
        changed = False
        for entry in entries:
            if not pattern.fullmatch(entry.name):
                continue
            try:
                info = os.lstat(str(entry))
                if not stat.S_ISREG(info.st_mode):
                    continue
                if getattr(info, "st_nlink", 1) == 1:
                    entry.unlink()
                    changed = True
                    continue
                try:
                    published = os.lstat(str(stable))
                except FileNotFoundError:
                    continue
                if self._same_file(info, published):
                    entry.unlink()
                    changed = True
            except OSError:
                pass
        if changed:
            self._fsync_backup_parent(backup_path)

    def _backup_before_v4_migration(self) -> str:
        """Create and verify the mandatory pre-v4 backup without mutating source data.

        Source and destination both use the injected connector, so SQLCipher databases
        remain keyed throughout. The caller holds ``BEGIN IMMEDIATE`` on the primary
        connection, preventing another writer from changing the source between this
        snapshot and the migration commit. Only a quick-checked temporary backup may
        atomically replace the stable backup path; every failure aborts the migration.
        """
        if self.path in (":memory:", "") or self.path.startswith("file::memory:"):
            raise RuntimeError("schema v4 migration requires a durable pre-migration backup")
        backup_path = f"{self.path}.pre-migration-v4.bak"
        self._cleanup_v4_backup_temps(backup_path)
        temp_path = (
            f"{backup_path}.tmp-{os.getpid()}-{threading.get_ident()}-{time.time_ns()}"
        )
        source = destination = None
        try:
            flags = (
                os.O_RDWR | os.O_CREAT | os.O_EXCL
                | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            )
            descriptor = os.open(temp_path, flags, 0o600)
            created = os.fstat(descriptor)
            os.close(descriptor)
            source = self._open_connection(self.path)
            destination = self._open_connection(temp_path)
            current = self._checked_backup_file(temp_path)
            if not self._same_file(created, current):
                raise RuntimeError("schema backup path changed while opening")
            self._raw_connection(source).backup(self._raw_connection(destination))
            destination.commit()
            if not self._quick_check(destination):
                raise RuntimeError("backup quick_check did not return ok")
            source_digest = self._logical_digest(source)
            backup_digest = self._logical_digest(destination)
            if source_digest != backup_digest:
                raise RuntimeError("backup logical digest did not match source")
            destination.close()
            destination = None
            source.close()
            source = None
            current = self._checked_backup_file(temp_path)
            if not self._same_file(created, current):
                raise RuntimeError("schema backup path changed while writing")
            descriptor = os.open(
                temp_path, os.O_RDWR | getattr(os, "O_BINARY", 0)
                | getattr(os, "O_NOFOLLOW", 0))
            try:
                opened = os.fstat(descriptor)
                if not self._same_file(current, opened):
                    raise RuntimeError("schema backup path changed before flush")
                fchmod = getattr(os, "fchmod", None)
                if fchmod is not None:
                    fchmod(descriptor, 0o600)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            try:
                os.link(temp_path, backup_path)
            except FileExistsError:
                stable_info = self._checked_backup_file(backup_path)
                stable = self._open_connection(backup_path)
                try:
                    if not self._quick_check(stable):
                        raise RuntimeError("existing schema backup failed quick_check")
                    if self._logical_digest(stable) != backup_digest:
                        raise RuntimeError("existing schema backup does not match source")
                finally:
                    stable.close()
                if not self._same_file(
                        stable_info, self._checked_backup_file(backup_path)):
                    raise RuntimeError("existing schema backup changed while validating")
                os.unlink(temp_path)
                self._fsync_backup_parent(backup_path)
                return backup_path
            published = os.lstat(backup_path)
            if not self._same_file(current, published):
                raise RuntimeError("schema backup publication changed")
            os.unlink(temp_path)
            stable_info = self._checked_backup_file(backup_path)
            if not self._same_file(current, stable_info):
                raise RuntimeError("schema backup publication was replaced")
            self._fsync_backup_parent(backup_path)
            return backup_path
        except BaseException as exc:
            for conn in (destination, source):
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
            try:
                if os.path.exists(temp_path):
                    os.unlink(temp_path)
            except OSError:
                pass
            raise RuntimeError(
                "schema v4 migration aborted: could not create and verify the "
                "pre-migration backup"
            ) from exc

    def _execute_script_transactional(self, script: str) -> None:
        """Execute a SQLite script without ``executescript``'s implicit COMMIT."""
        statement = ""
        # Some callers compose adjacent string literals with no newline between their
        # semicolon-terminated statements, so split at complete semicolon boundaries
        # rather than assuming one statement per source line. ``complete_statement``
        # correctly keeps trigger ``BEGIN ...; ...; END;`` bodies together.
        for character in script:
            statement += character
            if character == ";" and sqlite3.complete_statement(statement):
                sql = statement.strip()
                if sql:
                    self.conn.execute(sql)
                statement = ""
        if statement.strip():
            raise sqlite3.OperationalError("incomplete schema statement")

    # ── schema ──────────────────────────────────────────────────────────────
    def init_schema(self) -> None:
        objects = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view','index','trigger') "
            "AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        object_names = {str(row[0]) for row in objects}
        previous_version = 0
        if "schema_migrations" in object_names:
            row = self.conn.execute(
                "SELECT MAX(version) AS v FROM schema_migrations"
            ).fetchone()
            value = row[0] if row is not None else None
            previous_version = int(value) if value is not None else 0
        if previous_version > SCHEMA_VERSION:
            raise RuntimeError(
                f"database schema {previous_version} is newer than supported "
                f"schema {SCHEMA_VERSION}"
            )
        needs_backup = bool(object_names) and previous_version < SCHEMA_VERSION
        try:
            # Reserve the writer before the snapshot. This is read/locking state only;
            # every schema/data transform remains inside the transaction below.
            self.conn.execute("BEGIN IMMEDIATE")
            if needs_backup:
                self._backup_before_v4_migration()
            self._apply_schema(previous_version)
            self.conn.commit()
        except BaseException:
            if self.conn.in_transaction:
                self.conn.rollback()
            raise

    def _apply_schema(self, previous_version: int) -> None:
        self._execute_script_transactional(SCHEMA_SQL)
        self.has_fts5 = _fts5_available(self.conn)
        self.conn.execute(FTS_SQL_FTS5 if self.has_fts5 else FTS_SQL_FALLBACK)
        # Additive columns for DBs created before they existed — CREATE TABLE IF NOT
        # EXISTS above is a no-op on an already-existing table, so new columns need an
        # explicit, idempotent ALTER TABLE here (SQLite has no "ADD COLUMN IF NOT EXISTS").
        for stmt in (
            "ALTER TABLE memories ADD COLUMN sort_order REAL",
            "ALTER TABLE edges ADD COLUMN layer TEXT DEFAULT 'semantic'",
            "ALTER TABLE entities ADD COLUMN normalized_name TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE entities ADD COLUMN canonical_method TEXT NOT NULL DEFAULT 'exact'",
            "ALTER TABLE entities ADD COLUMN canonical_confidence REAL NOT NULL DEFAULT 1.0",
            "ALTER TABLE mem_links ADD COLUMN layer TEXT DEFAULT 'semantic'",
            "ALTER TABLE mem_links ADD COLUMN reason TEXT DEFAULT ''",
            "ALTER TABLE code_edges ADD COLUMN layer TEXT DEFAULT 'entity'",
            "ALTER TABLE symbols ADD COLUMN docstring TEXT DEFAULT ''",
            "ALTER TABLE receipt_chain_heads ADD COLUMN integrity_error TEXT DEFAULT ''",
            "ALTER TABLE jobs ADD COLUMN runner_id TEXT",
            "ALTER TABLE jobs ADD COLUMN heartbeat_at REAL",
        ):
            try:
                self.conn.execute(stmt)
            except sqlite3.OperationalError:
                pass  # column already exists
        # Classify pre-v3 edges. Existing rows defaulted to semantic during ALTER TABLE;
        # infer their more specific logical layer from the relationship label.
        if previous_version < 3:
            for table in ("edges", "mem_links", "code_edges"):
                rows = self.conn.execute(
                    f"SELECT rowid, relation, layer FROM {table}"
                ).fetchall()
                for row in rows:
                    inferred = infer_graph_layer(row["relation"]).value
                    if table == "code_edges" and inferred == GraphLayer.SEMANTIC.value:
                        inferred = GraphLayer.ENTITY.value
                    if row["layer"] != inferred:
                        self.conn.execute(
                            f"UPDATE {table} SET layer=? WHERE rowid=?",
                            (inferred, row["rowid"]),
                        )
        # v4 makes canonical identity and edge evidence explicit and indexed. Run the
        # backfills before creating representative-only uniqueness indexes so exact
        # normalized aliases can safely converge onto one deterministic canonical id.
        self._backfill_entity_canonicalization()
        self._execute_script_transactional(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_entity_workspace_canonical "
            "ON entities(workspace_id, normalized_name, etype) "
            "WHERE repo_id IS NULL AND canonical_id=id AND normalized_name<>'';"
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_entity_repo_canonical "
            "ON entities(workspace_id, repo_id, normalized_name, etype) "
            "WHERE repo_id IS NOT NULL AND canonical_id=id AND normalized_name<>'';"
            "CREATE INDEX IF NOT EXISTS idx_entity_canonical "
            "ON entities(workspace_id, canonical_id);"
            "CREATE INDEX IF NOT EXISTS idx_entity_normalized "
            "ON entities(workspace_id, normalized_name, etype);"
        )
        self._backfill_edge_supports()
        self._deduplicate_live_edges()
        self._execute_script_transactional(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_edge_workspace_live_unique "
            "ON edges(workspace_id, src, dst, relation, layer) "
            "WHERE workspace_id IS NOT NULL AND repo_id IS NULL "
            "AND valid_to IS NULL AND expired_at IS NULL;"
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_edge_repo_live_unique "
            "ON edges(workspace_id, repo_id, src, dst, relation, layer) "
            "WHERE workspace_id IS NOT NULL AND repo_id IS NOT NULL "
            "AND valid_to IS NULL AND expired_at IS NULL;"
        )
        # Every workspace has a cheap graph generation/state row, including databases
        # that already contained graph data before the v4 explorer tables were added.
        # Triggers in SCHEMA_SQL advance the generation on subsequent graph mutations.
        self.conn.execute(
            "INSERT OR IGNORE INTO graph_index_state "
            "(workspace_id, generation, state, active_job_id, updated_at, last_error) "
            "SELECT id, 1, 'ready', NULL, ?, '' FROM workspaces",
            (now_ts(),),
        )
        # Backfill the independent receipt anchor for databases created before the
        # anchor table existed. From this point onward every append updates it atomically,
        # allowing verification to detect deletion of the newest receipt as well as an
        # interior chain break.
        self.conn.execute(
            "UPDATE operation_receipts SET workspace_id='' WHERE workspace_id IS NULL"
        )
        self.conn.execute(
            "UPDATE operation_receipts SET repo_id='' WHERE repo_id IS NULL"
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO receipt_chain_heads "
            "(workspace_id, receipt_count, head_hash, integrity_error, updated_at) "
            "SELECT COALESCE(r.workspace_id, ''), COUNT(*), "
            "  (SELECT r2.receipt_hash FROM operation_receipts r2 "
            "   WHERE COALESCE(r2.workspace_id, '')=COALESCE(r.workspace_id, '') "
            "   ORDER BY r2.rowid DESC LIMIT 1), "
            "  '', "
            "  COALESCE(MAX(r.ts), 0) "
            "FROM operation_receipts r GROUP BY COALESCE(r.workspace_id, '')"
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?,?)",
            (SCHEMA_VERSION, now_ts()),
        )

    def _backfill_entity_canonicalization(self) -> None:
        rows = [dict(row) for row in self.conn.execute(
            "SELECT id, workspace_id, name, etype, canonical_id, normalized_name, "
            "canonical_method, canonical_confidence FROM entities "
            "ORDER BY workspace_id, etype, id"
        ).fetchall()]
        groups: dict[tuple[str, str, str], list[dict]] = {}
        for row in rows:
            normalized = normalize_entity_name(row.get("name") or "")
            row["_normalized"] = normalized
            key = (str(row.get("workspace_id") or ""), str(row.get("etype") or ""), normalized)
            groups.setdefault(key, []).append(row)
        for members in groups.values():
            # Existing canonical ids win when present; otherwise the oldest typed id
            # is the deterministic representative. Exact variants never cross a
            # workspace or entity-type boundary.
            existing = sorted({str(row.get("canonical_id") or "") for row in members
                               if row.get("canonical_id")})
            canonical_id = existing[0] if existing else min(row["id"] for row in members)
            merged = len(members) > 1
            for row in members:
                method = row.get("canonical_method") or (
                    "exact_normalized" if merged else "identity"
                )
                if not row.get("canonical_id"):
                    method = "exact_normalized" if merged else "identity"
                # A pre-release v4 build briefly stripped all punctuation. Reopening
                # such a database with the conservative normalizer can split a false
                # merge (for example C++ vs C#). A singleton that was joined only by
                # that automatic method must become its own representative again;
                # caller-provided canonical ids remain authoritative.
                if not merged and method == "exact_normalized" \
                        and row.get("canonical_id") != row["id"]:
                    canonical_id = row["id"]
                    method = "identity"
                confidence = float(row.get("canonical_confidence") or 1.0)
                if (
                    row.get("normalized_name") == row["_normalized"]
                    and row.get("canonical_id") == canonical_id
                    and row.get("canonical_method") == method
                    and float(row.get("canonical_confidence") or 0.0) == confidence
                ):
                    continue
                self.conn.execute(
                    "UPDATE entities SET normalized_name=?, canonical_id=?, "
                    "canonical_method=?, canonical_confidence=? WHERE id=?",
                    (row["_normalized"], canonical_id, method, confidence, row["id"]),
                )

    def _backfill_edge_supports(self) -> None:
        rows = self.conn.execute(
            "SELECT id, relation, valid_from, valid_to, ingested_at, expired_at, provenance "
            "FROM edges"
        ).fetchall()
        for row in rows:
            provenance = _loads(row["provenance"], {})
            source_kind = _edge_source_kind(provenance, row["relation"] or "")
            confidence = _edge_support_confidence(provenance, source_kind)
            for memory_id in _provenance_memory_ids(provenance):
                # This migration backfill is intentionally append-once.  The live-row
                # uniqueness index cannot make an ``INSERT OR IGNORE`` idempotent for
                # historical supports because partial indexes exclude closed rows.  In
                # addition to inflating the graph generation on every process start,
                # blindly inserting here would resurrect evidence that was explicitly
                # invalidated.  Any row for this legacy edge/memory/source triple proves
                # that its provenance has already been normalized; later lifecycle
                # changes remain authoritative.
                existing = self.conn.execute(
                    "SELECT 1 FROM edge_supports WHERE edge_id=? AND memory_id=? "
                    "AND source_kind=? LIMIT 1",
                    (row["id"], memory_id, source_kind),
                ).fetchone()
                if existing is not None:
                    continue
                self.conn.execute(
                    "INSERT INTO edge_supports "
                    "(edge_id, memory_id, source_kind, confidence, valid_from, valid_to, "
                    "ingested_at, expired_at, provenance) VALUES (?,?,?,?,?,?,?,?,?)",
                    (row["id"], memory_id, source_kind, confidence,
                     row["valid_from"], row["valid_to"], row["ingested_at"],
                     row["expired_at"], _dumps(provenance)),
                )

    def _deduplicate_live_edges(self) -> None:
        """Converge equivalent live relations without discarding temporal history."""
        rows = [dict(row) for row in self.conn.execute(
            "SELECT id, workspace_id, repo_id, src, dst, relation, layer, weight, "
            "valid_from, ingested_at, provenance FROM edges "
            "WHERE workspace_id IS NOT NULL AND valid_to IS NULL AND expired_at IS NULL "
            "ORDER BY workspace_id, repo_id, src, dst, relation, layer, "
            "COALESCE(valid_from, ingested_at), id"
        ).fetchall()]
        groups: dict[tuple, list[dict]] = {}
        for row in rows:
            source, target = row["src"], row["dst"]
            if row["relation"] in {"co_occurs", "related", "associated_with"} \
                    and target < source:
                source, target = target, source
            row["_normalized_src"] = source
            row["_normalized_dst"] = target
            key = (
                row["workspace_id"], row["repo_id"], source, target,
                row["relation"], row["layer"],
            )
            groups.setdefault(key, []).append(row)
        closed_at = now_ts()
        workspace_counts: dict[str, int] = {}
        for duplicates in groups.values():
            if len(duplicates) < 2:
                row = duplicates[0]
                if (row["src"], row["dst"]) != (
                        row["_normalized_src"], row["_normalized_dst"]):
                    self.conn.execute(
                        "UPDATE edges SET src=?, dst=? WHERE id=?",
                        (row["_normalized_src"], row["_normalized_dst"], row["id"]),
                    )
                continue
            duplicates.sort(key=lambda row: (
                row["valid_from"] if row["valid_from"] is not None
                else row["ingested_at"] if row["ingested_at"] is not None
                else float("inf"),
                row["id"],
            ))
            survivor, retired = duplicates[0], duplicates[1:]
            retired_ids = [row["id"] for row in retired]
            all_ids = [survivor["id"], *retired_ids]
            marks = ",".join("?" for _ in all_ids)
            support_rows = self.conn.execute(
                "SELECT memory_id, source_kind, confidence, valid_from, ingested_at, "
                "provenance FROM edge_supports WHERE edge_id IN (" + marks + ") "
                "AND valid_to IS NULL AND expired_at IS NULL ORDER BY id",
                all_ids,
            ).fetchall()
            for support in support_rows:
                current = self.conn.execute(
                    "SELECT id, confidence, valid_from, ingested_at, provenance "
                    "FROM edge_supports WHERE edge_id=? "
                    "AND memory_id=? AND source_kind=? AND valid_to IS NULL "
                    "AND expired_at IS NULL",
                    (survivor["id"], support["memory_id"], support["source_kind"]),
                ).fetchone()
                if current is None:
                    self.conn.execute(
                        "INSERT INTO edge_supports "
                        "(edge_id, memory_id, source_kind, confidence, valid_from, "
                        "ingested_at, provenance) VALUES (?,?,?,?,?,?,?)",
                        (
                            survivor["id"], support["memory_id"],
                            support["source_kind"], support["confidence"],
                            support["valid_from"], support["ingested_at"],
                            support["provenance"],
                        ),
                    )
                else:
                    confidence = max(
                        float(support["confidence"] or 0.0),
                        float(current["confidence"] or 0.0),
                    )
                    provenance = _merge_edge_provenance([
                        _loads(current["provenance"], {}),
                        _loads(support["provenance"], {}),
                    ])
                    provenance["confidence"] = confidence
                    support_valid = [value for value in (
                        current["valid_from"], support["valid_from"]
                    ) if value is not None]
                    support_ingested = [value for value in (
                        current["ingested_at"], support["ingested_at"]
                    ) if value is not None]
                    self.conn.execute(
                        "UPDATE edge_supports SET confidence=?, valid_from=?, "
                        "ingested_at=?, provenance=? WHERE id=?",
                        (
                            confidence, min(support_valid) if support_valid else None,
                            min(support_ingested) if support_ingested else None,
                            _dumps(provenance), current["id"],
                        ),
                    )
            provenances = [_loads(row["provenance"], {}) for row in duplicates]
            merged_provenance = _merge_edge_provenance(
                provenances, merged_ids=retired_ids
            )
            valid_values = [float(row["valid_from"]) for row in duplicates
                            if row["valid_from"] is not None]
            ingested_values = [float(row["ingested_at"]) for row in duplicates
                               if row["ingested_at"] is not None]
            for row in retired:
                provenance = _loads(row["provenance"], {})
                if not isinstance(provenance, dict):
                    provenance = {}
                provenance["canonical_deduplicated_into"] = survivor["id"]
                self.conn.execute(
                    "UPDATE edges SET valid_to=?, provenance=? WHERE id=?",
                    (closed_at, _dumps(provenance), row["id"]),
                )
            retired_marks = ",".join("?" for _ in retired_ids)
            self.conn.execute(
                "UPDATE edge_supports SET valid_to=? WHERE edge_id IN ("
                + retired_marks + ") AND valid_to IS NULL AND expired_at IS NULL",
                (closed_at, *retired_ids),
            )
            # Retire duplicates before normalizing the survivor endpoints. A pre-release
            # v4 database may already have the partial unique index; reversing the
            # survivor first would temporarily collide with its still-live twin.
            self.conn.execute(
                "UPDATE edges SET src=?, dst=?, weight=?, valid_from=?, ingested_at=?, "
                "provenance=? WHERE id=?",
                (
                    survivor["_normalized_src"], survivor["_normalized_dst"],
                    max(float(row["weight"] or 0.0) for row in duplicates),
                    min(valid_values) if valid_values else None,
                    min(ingested_values) if ingested_values else None,
                    _dumps(merged_provenance), survivor["id"],
                ),
            )
            workspace_counts[survivor["workspace_id"]] = (
                workspace_counts.get(survivor["workspace_id"], 0) + len(retired)
            )
        for workspace_id, count in workspace_counts.items():
            self.audit(
                "system", "graph_relation_deduplicate", workspace_id,
                f"closed {count} duplicate live relations", commit=False,
            )

    @property
    def schema_version(self) -> int:
        row = self.conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()
        return int(row["v"]) if row and row["v"] is not None else 0

    def close(self) -> None:
        self.conn.close()

    # ── tenancy ───────────────────────────────────────────────────────────────
    def _authorize_workspace(self, name: str) -> str:
        """When this Store is bound to a workspace allow-list, refuse to create or
        retrieve a workspace outside it. This is the hard isolation boundary applied
        at the persistence layer so no caller (including a future sync path) can
        bypass ENGRAPHIS_WORKSPACES by going directly to Store instead of through
        MemoryService."""
        if self.allowed_workspaces is not None and name not in self.allowed_workspaces:
            raise ValueError(f"workspace '{name}' is not permitted on this instance")
        return name

    def create_workspace(self, name: str, *, settings: Optional[dict] = None) -> str:
        self._authorize_workspace(name)
        wid = ids.new_id("workspace")
        self.conn.execute(
            "INSERT INTO workspaces(id, name, created_at, settings) VALUES (?,?,?,?)",
            (wid, name, now_ts(), _dumps(settings or {})),
        )
        self.conn.commit()
        return wid

    def get_or_create_workspace(self, name: str) -> str:
        # Authorize on the RETRIEVE path too, not just create — otherwise a workspace
        # outside ENGRAPHIS_WORKSPACES that already exists in the DB (e.g. predating the
        # allow-list, or arriving via sync) could be handed back, silently bypassing the
        # isolation boundary _authorize_workspace is meant to enforce ("create or retrieve").
        self._authorize_workspace(name)
        row = self.conn.execute("SELECT id FROM workspaces WHERE name=?", (name,)).fetchone()
        if row:
            return row["id"]
        return self.create_workspace(name)

    def create_repo(self, workspace_id: str, name: str, **kw: Any) -> str:
        rid = ids.new_id("repo")
        self.conn.execute(
            "INSERT INTO repos(id, workspace_id, name, root_path, vcs_remote, primary_lang, "
            "created_at, settings) VALUES (?,?,?,?,?,?,?,?)",
            (rid, workspace_id, name, kw.get("root_path"), kw.get("vcs_remote"),
             kw.get("primary_lang"), now_ts(), _dumps(kw.get("settings") or {})),
        )
        self.conn.commit()
        return rid

    def get_or_create_repo(self, workspace_id: str, name: str, **kw: Any) -> str:
        row = self.conn.execute(
            "SELECT id FROM repos WHERE workspace_id=? AND name=?", (workspace_id, name)
        ).fetchone()
        return row["id"] if row else self.create_repo(workspace_id, name, **kw)

    # ── sessions ──────────────────────────────────────────────────────────────
    def start_session(self, workspace_id: str, repo_id: Optional[str] = None,
                      *, agent: str = "", user_id: str = "", goal: str = "",
                      commit: bool = True) -> str:
        sid = ids.new_id("session")
        self.conn.execute(
            "INSERT INTO sessions(id, workspace_id, repo_id, agent, user_id, goal, status, "
            "started_at) VALUES (?,?,?,?,?,?,?,?)",
            (sid, workspace_id, repo_id, agent, user_id, goal, "active", now_ts()),
        )
        if commit:
            self.conn.commit()
        return sid

    def end_session(self, session_id: str, *, summary: str = "",
                    open_threads: Optional[list] = None, outcome: str = "") -> str:
        """Close one active session exactly once.

        An identical retry is a no-op, while a conflicting retry cannot overwrite the
        durable handoff left by the first caller. ``BEGIN IMMEDIATE`` makes the state
        check and transition atomic across threads, processes, and Store instances.

        Returns ``"ended"``, ``"unchanged"``, ``"conflict"``, or ``"missing"``.
        """
        threads = list(open_threads or [])
        encoded_threads = _dumps(threads)
        owns_transaction = not self.conn.transaction_owned_by_current_thread()
        try:
            if owns_transaction:
                self.conn.execute("BEGIN IMMEDIATE")
            row = self.conn.execute(
                "SELECT status, summary, open_threads, outcome FROM sessions WHERE id=?",
                (session_id,),
            ).fetchone()
            if row is None:
                result = "missing"
            elif row["status"] == "active":
                self.conn.execute(
                    "UPDATE sessions SET status='summarized', ended_at=?, summary=?, "
                    "open_threads=?, outcome=? WHERE id=? AND status='active'",
                    (now_ts(), summary, encoded_threads, outcome, session_id),
                )
                result = "ended"
            elif (
                row["status"] == "summarized"
                and (row["summary"] or "") == summary
                and _loads(row["open_threads"], []) == threads
                and (row["outcome"] or "") == outcome
            ):
                result = "unchanged"
            else:
                result = "conflict"
            if owns_transaction:
                self.conn.commit()
            return result
        except BaseException:
            if owns_transaction and self.conn.transaction_owned_by_current_thread():
                self.conn.rollback()
            raise

    def get_session(self, session_id: str) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["open_threads"] = _loads(d.get("open_threads"), [])
        return d

    def begin_session_write(self, session_id: str, *, workspace_id: str,
                            repo_id: Optional[str] = None) -> bool:
        """Reserve an active session for one write transaction.

        The service performs an early ownership/status check for useful public errors, but
        that check cannot serialize with a concurrent ``end_session``.  Re-reading under
        ``BEGIN IMMEDIATE`` makes the write and close operations linearizable: whichever
        transaction wins first either commits the write before closure or observes the
        closed session and rejects it.

        Return whether this call opened the transaction so the caller can roll it back if
        a later step fails.  A caller already inside a transaction retains ownership.
        """
        owns_transaction = not self.conn.transaction_owned_by_current_thread()
        try:
            if owns_transaction:
                self.conn.execute("BEGIN IMMEDIATE")
            row = self.conn.execute(
                "SELECT workspace_id, repo_id, status FROM sessions WHERE id=?",
                (session_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"no session with id '{session_id}'")
            if row["workspace_id"] != workspace_id or (
                    repo_id is not None and row["repo_id"] != repo_id):
                raise ValueError("session_id does not belong to that workspace/repo")
            if row["status"] != "active":
                raise ValueError("session_id is not active")
            return owns_transaction
        except BaseException:
            if owns_transaction and self.conn.transaction_owned_by_current_thread():
                self.conn.rollback()
            raise

    def get_active_session(self, workspace_id: str, repo_id: Optional[str],
                           *, agent: str = "", user_id: str = "",
                           goal: str = "") -> Optional[dict]:
        """Return the active session for one exact task identity.

        Empty values are values, not wildcards. This prevents an unnamed client, a
        different authenticated user, or a new goal from inheriting unrelated work.
        ``COALESCE`` keeps legacy rows with NULL identity fields compatible with the
        empty-string values written by current clients.
        """
        sql = ("SELECT * FROM sessions WHERE workspace_id=? AND repo_id IS ? "
               "AND status='active' AND COALESCE(agent, '')=? "
               "AND COALESCE(user_id, '')=? AND COALESCE(goal, '')=?")
        params: list[Any] = [workspace_id, repo_id, agent, user_id, goal]
        sql += " ORDER BY started_at DESC LIMIT 1"
        row = self.conn.execute(sql, params).fetchone()
        if not row:
            return None
        d = dict(row)
        d["open_threads"] = _loads(d.get("open_threads"), [])
        return d

    def get_or_start_session(self, workspace_id: str, repo_id: Optional[str] = None,
                             *, agent: str = "", user_id: str = "", goal: str = "",
                             force_new: bool = False) -> tuple[str, bool]:
        """Atomically reuse an exact active task or create a new session.

        The write reservation precedes the lookup, so two concurrent callers cannot both
        observe "no session" and insert duplicates. ``force_new`` deliberately skips the
        lookup while retaining the same transaction boundary.
        """
        owns_transaction = not self.conn.transaction_owned_by_current_thread()
        try:
            if owns_transaction:
                self.conn.execute("BEGIN IMMEDIATE")
            if not force_new:
                existing = self.get_active_session(
                    workspace_id, repo_id, agent=agent, user_id=user_id, goal=goal,
                )
                if existing is not None:
                    if owns_transaction:
                        self.conn.commit()
                    return existing["id"], True
            sid = self.start_session(
                workspace_id, repo_id, agent=agent, user_id=user_id, goal=goal,
                commit=False,
            )
            if owns_transaction:
                self.conn.commit()
            return sid, False
        except BaseException:
            if owns_transaction and self.conn.transaction_owned_by_current_thread():
                self.conn.rollback()
            raise

    def get_last_session(self, workspace_id: str, repo_id: Optional[str],
                         *, exclude: Optional[str] = None,
                         user_id: Optional[str] = None,
                         agent: Optional[str] = None) -> Optional[dict]:
        """Return the most recent ended session matching the requested identity.

        ``None`` leaves an identity dimension unfiltered for legacy/core callers. Passing
        an empty string is an exact match for legacy unowned/unnamed sessions; it is never
        a wildcard.
        """
        sql = ("SELECT * FROM sessions WHERE workspace_id=? AND repo_id IS ? "
               "AND ended_at IS NOT NULL")
        params: list[Any] = [workspace_id, repo_id]
        if exclude:
            sql += " AND id != ?"
            params.append(exclude)
        if user_id is not None:
            sql += " AND COALESCE(user_id, '') = ?"
            params.append(user_id)
        if agent is not None:
            sql += " AND COALESCE(agent, '') = ?"
            params.append(agent)
        sql += " ORDER BY ended_at DESC LIMIT 1"
        row = self.conn.execute(sql, params).fetchone()
        if not row:
            return None
        d = dict(row)
        d["open_threads"] = _loads(d.get("open_threads"), [])
        return d

    # ── memories ──────────────────────────────────────────────────────────────
    def add_memory(self, rec: MemoryRecord, *, audit: bool = True,
                   commit: bool = True) -> str:
        if not rec.id:
            rec.id = ids.new_id("memory")
        existing = self.conn.execute(
            "SELECT provenance, workspace_id FROM memories WHERE id=?", (rec.id,)
        ).fetchone()
        if existing is not None:
            if existing["workspace_id"] != rec.workspace_id:
                self.audit("system", "cross_workspace_overwrite_blocked", rec.id,
                           f"existing workspace={existing['workspace_id']}, "
                           f"incoming workspace={rec.workspace_id}", commit=False)
                rec.id = ids.new_id("memory")
            elif audit:
                # Generic provenance-change record for direct writes. The sync path
                # passes audit=False and logs its own semantic 'sync_overwrite' instead,
                # so a synced update yields exactly one audit row rather than a duplicate.
                self.audit("system", "overwrite", rec.id,
                           f"existing provenance={existing['provenance']}, "
                           f"incoming provenance={_dumps(rec.provenance)}", commit=False)
        ts = now_ts()
        rec.ingested_at = rec.ingested_at or ts
        rec.valid_from = rec.valid_from if rec.valid_from is not None else ts
        rec.last_access = rec.last_access or ts
        self.conn.execute(
            """INSERT INTO memories
               (id, workspace_id, repo_id, session_id, scope, mtype, title, content, summary,
                keywords, metadata, importance, surprise, stability, access_count, last_access,
                valid_from, valid_to, ingested_at, expired_at, pinned, sensitivity, provenance)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                workspace_id=excluded.workspace_id, repo_id=excluded.repo_id,
                session_id=excluded.session_id, scope=excluded.scope, mtype=excluded.mtype,
                title=excluded.title, content=excluded.content, summary=excluded.summary,
                keywords=excluded.keywords, metadata=excluded.metadata,
                importance=excluded.importance, surprise=excluded.surprise,
                stability=excluded.stability, access_count=excluded.access_count,
                last_access=excluded.last_access, valid_from=excluded.valid_from,
                valid_to=excluded.valid_to, ingested_at=excluded.ingested_at,
                expired_at=excluded.expired_at, pinned=excluded.pinned,
                sensitivity=excluded.sensitivity, provenance=excluded.provenance""",
            (rec.id, rec.workspace_id, rec.repo_id, rec.session_id,
             _enum(rec.scope), _enum(rec.mtype), rec.title, rec.content, rec.summary,
             _dumps(rec.keywords), _dumps(rec.metadata), rec.importance, rec.surprise,
             rec.stability, rec.access_count, rec.last_access, rec.valid_from, rec.valid_to,
             rec.ingested_at, rec.expired_at, int(rec.pinned), rec.sensitivity,
             _dumps(rec.provenance)),
        )
        # full-text mirror
        self._fts_upsert(rec.id, rec.title, rec.content, " ".join(rec.keywords))
        # vector mirror (L2-normalized for cosine-as-dot)
        if rec.embedding is not None:
            self.put_vector(rec.id, rec.embedding, model=str(rec.metadata.get("embed_model", "")))
        # ``commit=False`` lets a bulk writer (sync's bundle apply) amortize one commit over
        # a batch of rows instead of paying a durability fsync per memory. The caller then
        # owns the transaction and MUST commit or roll back — see SyncEngine.apply_bundle.
        if commit:
            self.conn.commit()
        return rec.id

    def get_memory(self, memory_id: str) -> Optional[MemoryRecord]:
        row = self.conn.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
        return _row_to_record(row) if row else None

    def get_memories(self, memory_ids: Iterable[str]) -> dict[str, MemoryRecord]:
        """Batched :meth:`get_memory` — one ``IN (...)`` query per chunk.

        Recall resolves the union of the vector/lexical/graph arms (~150 ids) and sync
        resolves a whole bundle; doing that one ``SELECT`` at a time is the dominant cost
        on both paths. Ids that do not exist are simply absent from the result, mirroring
        ``get_memory`` returning ``None``."""
        unique: list[str] = []
        seen: set = set()
        for mid in memory_ids:
            if mid and mid not in seen:
                seen.add(mid)
                unique.append(mid)
        out: dict[str, MemoryRecord] = {}
        for start in range(0, len(unique), IN_CLAUSE_CHUNK):
            chunk = unique[start:start + IN_CLAUSE_CHUNK]
            marks = ",".join("?" for _ in chunk)
            rows = self.conn.fetchall(
                f"SELECT * FROM memories WHERE id IN ({marks})", chunk)
            for row in rows:
                out[row["id"]] = _row_to_record(row)
        return out

    def list_memories(self, flt: Optional[SearchFilter] = None,
                      *, include_invalid: bool = False, limit: Optional[int] = None) -> list[MemoryRecord]:
        sql = "SELECT * FROM memories"
        where, params = self._where(flt, include_invalid)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY ingested_at DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        rows = self.conn.execute(sql, params).fetchall()
        return [_row_to_record(r) for r in rows]

    def list_memories_page(self, flt: Optional[SearchFilter] = None, *,
                           after_id: str = "", limit: int = 500) -> list[MemoryRecord]:
        """Return one deterministic keyset page without materializing the full scope."""
        sql = "SELECT * FROM memories"
        where, params = self._where(flt, include_invalid=False)
        if after_id:
            where.append("id>?")
            params.append(after_id)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id LIMIT ?"
        params.append(max(1, int(limit)))
        rows = self.conn.execute(sql, params).fetchall()
        return [_row_to_record(row) for row in rows]


    def close_validity(self, memory_id: str, *, at: Optional[float] = None,
                       actor: str = "system", reason: str = "contradicted") -> None:
        """Bi-temporal invalidation (§8.3): close a fact's validity window without deleting."""
        at = at if at is not None else now_ts()
        self.conn.execute("UPDATE memories SET valid_to=? WHERE id=? AND valid_to IS NULL",
                          (at, memory_id))
        self.audit(actor, "invalidate", memory_id, reason, commit=False)
        self.invalidate_edges_for_memory(memory_id, at=at, commit=False)
        self.conn.commit()

    def set_pinned(self, memory_id: str, pinned: bool) -> None:
        """Pinned memories are exempt from automatic decay/pruning (AGENTS.md §3.2);
        governance (explicit forget/correct) can still act on them."""
        self.conn.execute("UPDATE memories SET pinned=? WHERE id=?", (int(pinned), memory_id))
        self.conn.commit()

    def reinforce(self, memory_id: str, *, alpha: float = 0.3, boost: float = 0.0) -> None:
        """Spacing-effect reinforcement (§13.2): stability grows sub-linearly with use."""
        row = self.conn.execute(
            "SELECT stability, access_count FROM memories WHERE id=?", (memory_id,)
        ).fetchone()
        if not row:
            return
        n = row["access_count"] + 1
        new_stab = row["stability"] * (1 + alpha * np.log(1 + n)) + boost
        self.conn.execute(
            "UPDATE memories SET stability=?, access_count=?, last_access=? WHERE id=?",
            (float(new_stab), n, now_ts(), memory_id),
        )
        self.conn.commit()

    # ── vectors ───────────────────────────────────────────────────────────────
    def put_vector(self, memory_id: str, vec: np.ndarray, *, model: str = "") -> None:
        v = np.asarray(vec, dtype=np.float32)
        norm = float(np.linalg.norm(v))
        if norm > 0:
            v = v / norm
        self.conn.execute(
            "INSERT OR REPLACE INTO mem_vectors(id, dim, vector, model) VALUES (?,?,?,?)",
            (memory_id, int(v.shape[0]), v.tobytes(), model),
        )

    def iter_vectors(self, flt: Optional[SearchFilter] = None,
                     *, include_invalid: bool = False,
                     dim: Optional[int] = None) -> Iterable[tuple[str, np.ndarray]]:
        """Yield normalized vectors matching the memory filter and optional dimension.

        Rows are materialized *inside* the connection lock in bounded batches rather than
        streamed off a live cursor. ``_SerializedConnection`` serializes one statement at a
        time, so a generator that held an open cursor across its yields would let another
        thread's write interleave with this read on the shared connection — and this is the
        hot recall path (``NumpyVectorIndex.search`` drains it with ``list(...)``). Keyset
        pagination on the primary key keeps peak memory at one batch no matter how large
        ``mem_vectors`` grows, and is stable under concurrent inserts (unlike OFFSET)."""
        where, params = self._where(flt, include_invalid, alias="m")
        if dim is not None:
            where.append("v.dim=?")
            params.append(int(dim))
        sql = ("SELECT v.id AS id, v.vector AS vector FROM mem_vectors v "
               "JOIN memories m ON m.id = v.id WHERE "
               + " AND ".join([*where, "v.id > ?"])
               + " ORDER BY v.id LIMIT ?")
        cursor_id = ""
        while True:
            rows = self.conn.fetchall(sql, (*params, cursor_id, VECTOR_SCAN_BATCH))
            if not rows:
                return
            for r in rows:
                yield r["id"], np.frombuffer(r["vector"], dtype=np.float32)
            if len(rows) < VECTOR_SCAN_BATCH:
                return
            cursor_id = rows[-1]["id"]

    # ── full text ─────────────────────────────────────────────────────────────
    def _fts_upsert(self, mid: str, title: str, content: str, keywords: str) -> None:
        self.conn.execute("DELETE FROM mem_fts WHERE id=?", (mid,))
        self.conn.execute(
            "INSERT INTO mem_fts(id, title, content, keywords) VALUES (?,?,?,?)",
            (mid, title, content, keywords),
        )

    def fts_search(self, query: str, k: int = 20,
                   *, filter: Optional[SearchFilter] = None) -> list[tuple[str, float]]:
        """Lexical arm. Uses FTS5 BM25 when available, else a LIKE fallback."""
        q = (query or "").strip()
        if not q:
            return []
        where, params = self._where(filter, include_invalid=False, alias="m")
        extra = (" AND " + " AND ".join(where)) if where else ""
        if self.has_fts5:
            try:
                rows = self.conn.execute(
                    "SELECT f.id, bm25(mem_fts) AS rank FROM mem_fts f "
                    "JOIN memories m ON m.id = f.id "
                    "WHERE mem_fts MATCH ?" + extra + " ORDER BY rank LIMIT ?",
                    (_fts_query(q), *params, k),
                ).fetchall()
                # FTS5 BM25 scores are negative; lower is better, so negate them.
                return [(r["id"], -float(r["rank"])) for r in rows]
            except sqlite3.OperationalError:
                pass
        # Escape LIKE wildcards: on a non-FTS5 build an unescaped '%'/'_' in the query
        # would be treated as a pattern and over-match (a bare "%" matching everything).
        like = f"%{_escape_like(q)}%"
        rows = self.conn.execute(
            "SELECT f.id FROM mem_fts f JOIN memories m ON m.id = f.id "
            "WHERE (f.content LIKE ? ESCAPE '\\' OR f.title LIKE ? ESCAPE '\\')"
            + extra + " LIMIT ?",
            (like, like, *params, k),
        ).fetchall()
        return [(r["id"], 0.5) for r in rows]

    # ── graph ─────────────────────────────────────────────────────────────────
    def upsert_entity(self, node: Node, *, commit: bool = True) -> str:
        normalized = normalize_entity_name(node.name)
        existing = self.conn.execute(
            "SELECT id FROM entities WHERE workspace_id=? AND repo_id IS ? "
            "AND normalized_name=? AND etype IS ? ORDER BY id LIMIT 1",
            (node.workspace_id, node.repo_id, normalized, node.ntype),
        ).fetchone()
        if existing:
            return existing["id"]
        nid = node.id or ids.new_id("entity")
        canonical_id = node.canonical_id
        method = "provided" if canonical_id else "identity"
        if not canonical_id:
            canonical = self.conn.execute(
                "SELECT COALESCE(canonical_id, id) AS canonical_id FROM entities "
                "WHERE workspace_id=? AND normalized_name=? AND etype IS ? "
                "ORDER BY id LIMIT 1",
                (node.workspace_id, normalized, node.ntype),
            ).fetchone()
            if canonical:
                canonical_id = canonical["canonical_id"]
                method = "exact_normalized"
        canonical_id = canonical_id or nid
        self.conn.execute(
            "INSERT INTO entities(id, workspace_id, repo_id, name, etype, canonical_id, "
            "normalized_name, canonical_method, canonical_confidence, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (nid, node.workspace_id, node.repo_id, node.name, node.ntype,
             canonical_id, normalized, method, 1.0, now_ts()),
        )
        if commit:
            self.conn.commit()
        return nid

    def list_entities(self, flt: Optional[SearchFilter] = None,
                      *, limit: Optional[int] = None) -> list[Node]:
        """Entities in scope, newest first — the seed set the profile-consolidation
        pass rolls up (``core.consolidate.consolidate_profiles``). Scoped to the
        filter's workspace/repo so it can't cross the isolation boundary."""
        sql = "SELECT * FROM entities"
        where: list[str] = []
        params: list[Any] = []
        if flt and flt.workspace_id:
            where.append("workspace_id=?")
            params.append(flt.workspace_id)
        if flt and flt.repo_id:
            where.append("repo_id=?")
            params.append(flt.repo_id)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        rows = self.conn.execute(sql, params).fetchall()
        return [Node(id=r["id"], name=r["name"], ntype=r["etype"] or "",
                     workspace_id=r["workspace_id"], repo_id=r["repo_id"],
                     canonical_id=r["canonical_id"]) for r in rows]

    def upsert_edge(self, edge: Edge, *, commit: bool = True) -> str:
        eid = edge.id or ids.new_id("edge")
        layer = normalize_graph_layer(edge.layer, edge.relation).value
        source, target = edge.src, edge.dst
        if edge.relation in {"co_occurs", "related", "associated_with"} and target < source:
            source, target = target, source
        incoming_provenance = _merge_edge_provenance([edge.provenance])
        existing = self.conn.execute(
            "SELECT id, workspace_id, repo_id, src, dst, relation, layer, weight, "
            "valid_from, valid_to, ingested_at, expired_at, provenance "
            "FROM edges WHERE id=?", (eid,)
        ).fetchone()
        replacing = existing is not None
        stored_provenance = _loads(existing["provenance"], {}) if existing else {}
        incoming_supports = {
            (memory_id, _edge_source_kind(incoming_provenance, edge.relation))
            for memory_id in _provenance_memory_ids(incoming_provenance)
        }
        stored_supports = {
            (memory_id, _edge_source_kind(stored_provenance, edge.relation))
            for memory_id in _provenance_memory_ids(stored_provenance)
        }
        if existing is not None and edge.valid_to is None and edge.expired_at is None \
                and existing["valid_to"] is None and existing["expired_at"] is None \
                and incoming_supports == stored_supports \
                and (
                    existing["workspace_id"], existing["repo_id"],
                    existing["src"], existing["dst"], existing["relation"], existing["layer"],
                ) == (
                    edge.workspace_id, edge.repo_id, source, target, edge.relation, layer,
                ):
            merged_provenance = _merge_edge_provenance(
                [stored_provenance, incoming_provenance]
            )
            desired_weight = max(
                float(existing["weight"] or 0.0), float(edge.weight or 0.0)
            )
            desired_valid_from = existing["valid_from"]
            if edge.valid_from is not None:
                desired_valid_from = min(
                    value for value in (existing["valid_from"], edge.valid_from)
                    if value is not None
                )
            serialized_provenance = _dumps(merged_provenance)
            if desired_weight != float(existing["weight"] or 0.0) \
                    or desired_valid_from != existing["valid_from"] \
                    or serialized_provenance != (existing["provenance"] or "{}"):
                self.conn.execute(
                    "UPDATE edges SET weight=?, valid_from=?, provenance=? WHERE id=?",
                    (desired_weight, desired_valid_from, serialized_provenance, eid),
                )
            self._write_edge_supports(
                eid, edge.relation, incoming_provenance,
                valid_from=edge.valid_from, valid_to=edge.valid_to,
                ingested_at=edge.ingested_at, expired_at=edge.expired_at,
            )
            if commit:
                self.conn.commit()
            return eid
        equivalent = None
        if edge.valid_to is None and edge.expired_at is None:
            equivalent = self.conn.execute(
                "SELECT id, weight, valid_from, provenance FROM edges "
                "WHERE workspace_id IS ? AND repo_id IS ? AND src=? AND dst=? "
                "AND relation=? AND layer=? AND valid_to IS NULL AND expired_at IS NULL "
                "AND id<>? ORDER BY id LIMIT 1",
                (
                    edge.workspace_id, edge.repo_id, source, target,
                    edge.relation, layer, eid,
                ),
            ).fetchone()
        if equivalent is not None:
            if replacing:
                closed_at = now_ts()
                self.conn.execute(
                    "UPDATE edges SET valid_to=? WHERE id=? AND valid_to IS NULL",
                    (closed_at, eid),
                )
                self.conn.execute(
                    "UPDATE edge_supports SET valid_to=? WHERE edge_id=? "
                    "AND valid_to IS NULL AND expired_at IS NULL",
                    (closed_at, eid),
                )
            existing_provenance = _loads(equivalent["provenance"], {})
            merged_provenance = _merge_edge_provenance(
                [existing_provenance, incoming_provenance],
                merged_ids=[eid] if replacing else [],
            )
            valid_values = [value for value in (
                equivalent["valid_from"], edge.valid_from
            ) if value is not None]
            self.conn.execute(
                "UPDATE edges SET weight=?, valid_from=?, provenance=? WHERE id=?",
                (
                    max(float(equivalent["weight"] or 0.0), float(edge.weight or 0.0)),
                    min(valid_values) if valid_values else now_ts(),
                    _dumps(merged_provenance), equivalent["id"],
                ),
            )
            self._write_edge_supports(
                equivalent["id"], edge.relation, incoming_provenance,
                valid_from=edge.valid_from, valid_to=edge.valid_to,
                ingested_at=edge.ingested_at, expired_at=edge.expired_at,
            )
            if commit:
                self.conn.commit()
            return str(equivalent["id"])
        if replacing:
            # ``upsert_edge`` replaces the supplied edge record. Close its previous
            # normalized evidence before writing the replacement so sources removed
            # from the new provenance cannot remain live invisibly.
            self.conn.execute(
                "UPDATE edge_supports SET valid_to=? WHERE edge_id=? "
                "AND valid_to IS NULL AND expired_at IS NULL",
                (now_ts(), eid),
            )
        self.conn.execute(
            "INSERT INTO edges(id, workspace_id, repo_id, src, dst, relation, layer, "
            "weight, valid_from, valid_to, ingested_at, expired_at, provenance) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET workspace_id=excluded.workspace_id, "
            "repo_id=excluded.repo_id, src=excluded.src, dst=excluded.dst, "
            "relation=excluded.relation, layer=excluded.layer, weight=excluded.weight, "
            "valid_from=excluded.valid_from, valid_to=excluded.valid_to, "
            "ingested_at=excluded.ingested_at, expired_at=excluded.expired_at, "
            "provenance=excluded.provenance",
            (eid, edge.workspace_id, edge.repo_id, source, target, edge.relation, layer,
             edge.weight, edge.valid_from if edge.valid_from is not None else now_ts(),
             edge.valid_to, edge.ingested_at or now_ts(), edge.expired_at,
             _dumps(incoming_provenance)),
        )
        self._write_edge_supports(
            eid, edge.relation, incoming_provenance,
            valid_from=edge.valid_from, valid_to=edge.valid_to,
            ingested_at=edge.ingested_at, expired_at=edge.expired_at,
        )
        if commit:
            self.conn.commit()
        return eid

    def invalidate_edge(self, edge_id: str, at: Optional[float] = None) -> None:
        ts = now_ts() if at is None else at
        self.conn.execute("UPDATE edges SET valid_to=? WHERE id=? AND valid_to IS NULL",
                          (ts, edge_id))
        self.conn.execute(
            "UPDATE edge_supports SET valid_to=? WHERE edge_id=? "
            "AND valid_to IS NULL AND expired_at IS NULL", (ts, edge_id)
        )
        self.conn.commit()

    def _write_edge_supports(self, edge_id: str, relation: str, provenance: dict,
                             *, valid_from: Optional[float] = None,
                             valid_to: Optional[float] = None,
                             ingested_at: Optional[float] = None,
                             expired_at: Optional[float] = None) -> None:
        source_kind = _edge_source_kind(provenance, relation)
        confidence = _edge_support_confidence(provenance, source_kind)
        support_provenance = _merge_edge_provenance([provenance])
        support_provenance["confidence"] = confidence
        timestamp = now_ts()
        support_valid_from = valid_from if valid_from is not None else timestamp
        support_ingested_at = ingested_at if ingested_at is not None else timestamp
        for memory_id in _provenance_memory_ids(provenance):
            if valid_to is None and expired_at is None:
                current = self.conn.execute(
                    "SELECT id, confidence, valid_from, ingested_at, provenance "
                    "FROM edge_supports WHERE edge_id=? AND memory_id=? AND source_kind=? "
                    "AND valid_to IS NULL AND expired_at IS NULL",
                    (edge_id, memory_id, source_kind),
                ).fetchone()
                if current is not None:
                    current_provenance = _loads(current["provenance"], {})
                    merged_provenance = _merge_edge_provenance(
                        [current_provenance, support_provenance]
                    )
                    desired_confidence = max(
                        float(current["confidence"] or 0.0), confidence
                    )
                    merged_provenance["confidence"] = desired_confidence
                    desired_valid_from = min(
                        value for value in (current["valid_from"], support_valid_from)
                        if value is not None
                    )
                    desired_ingested_at = min(
                        value for value in (current["ingested_at"], support_ingested_at)
                        if value is not None
                    )
                    serialized = _dumps(merged_provenance)
                    if desired_confidence != float(current["confidence"] or 0.0) \
                            or desired_valid_from != current["valid_from"] \
                            or desired_ingested_at != current["ingested_at"] \
                            or serialized != (current["provenance"] or "{}"):
                        self.conn.execute(
                            "UPDATE edge_supports SET confidence=?, valid_from=?, "
                            "ingested_at=?, provenance=? WHERE id=?",
                            (desired_confidence, desired_valid_from,
                             desired_ingested_at, serialized, current["id"]),
                        )
                    continue
            self.conn.execute(
                "INSERT OR IGNORE INTO edge_supports "
                "(edge_id, memory_id, source_kind, confidence, valid_from, valid_to, "
                "ingested_at, expired_at, provenance) VALUES (?,?,?,?,?,?,?,?,?)",
                (edge_id, memory_id, source_kind, confidence,
                 support_valid_from, valid_to, support_ingested_at, expired_at,
                 _dumps(support_provenance)),
            )

    def add_edge_support(self, edge_id: str, provenance: dict, *,
                         commit: bool = True) -> None:
        """Record another source memory supporting an existing graph edge."""
        incoming = _provenance_memory_ids(provenance)
        if not incoming:
            return
        row = self.conn.execute("SELECT provenance FROM edges WHERE id=?", (edge_id,)).fetchone()
        if row is None:
            return
        stored = _loads(row["provenance"], {})
        if not isinstance(stored, dict):
            stored = {}
        merged_provenance = _merge_edge_provenance([stored, provenance])
        if _dumps(merged_provenance) != _dumps(stored):
            self.conn.execute("UPDATE edges SET provenance=? WHERE id=?",
                              (_dumps(merged_provenance), edge_id))
        edge_row = self.conn.execute(
            "SELECT relation, valid_from, valid_to, ingested_at, expired_at "
            "FROM edges WHERE id=?", (edge_id,)
        ).fetchone()
        if edge_row:
            self._write_edge_supports(
                edge_id, edge_row["relation"] or "", provenance,
                valid_from=edge_row["valid_from"], valid_to=edge_row["valid_to"],
                ingested_at=edge_row["ingested_at"], expired_at=edge_row["expired_at"],
            )
        if commit:
            self.conn.commit()

    def invalidate_edges_for_memory(self, memory_id: str, *, at: Optional[float] = None,
                                    commit: bool = True) -> None:
        """Remove one memory's support and close edges with no remaining sources.

        Called on every INVALIDATE resolution, ``forget`` and ``correct`` — routine write
        traffic — so the candidate scan is bounded to the owning memory's workspace. Without
        it this was a leading-wildcard ``LIKE`` with no scope predicate at all: a full scan
        of every edge in the database, across every tenant, on each call.

        Residual (deliberate, bounded fix): support is still matched by substring against the
        JSON ``provenance`` blob, so the scan is O(edges in this workspace) rather than an
        indexed O(edges supported by this memory). Substring matching cannot cause a *false*
        invalidation — every candidate row is re-checked with an exact
        ``memory_id in _provenance_memory_ids(...)`` test below — it only over-fetches
        candidates. The indexed fix is an ``(edge_id, memory_id)`` join table, which is NOT
        safe to land while ``MemoryService.clone_workspace`` writes ``INSERT INTO edges``
        directly (service.py): those edges would carry provenance but no support rows, and
        would then silently never be invalidated. Normalize the edge writes first.
        """
        ts = at if at is not None else now_ts()
        owner = self.conn.fetchall(
            "SELECT workspace_id FROM memories WHERE id=?", (memory_id,))
        workspace_id = owner[0]["workspace_id"] if owner else None
        indexed_sql = (
            "SELECT DISTINCT e.id, e.provenance FROM edge_supports s "
            "JOIN edges e ON e.id=s.edge_id WHERE s.memory_id=? "
            "AND s.valid_to IS NULL AND s.expired_at IS NULL AND e.valid_to IS NULL"
        )
        indexed_params: list[Any] = [memory_id]
        if workspace_id is not None:
            indexed_sql += " AND (e.workspace_id=? OR e.workspace_id IS NULL)"
            indexed_params.append(workspace_id)
        rows = self.conn.fetchall(indexed_sql, indexed_params)
        if not rows:
            # Compatibility fallback for a direct legacy SQL writer. Canonical write
            # paths populate edge_supports, so normal invalidation is indexed.
            sql = ("SELECT id, provenance FROM edges "
                   "WHERE valid_to IS NULL AND provenance LIKE ? ESCAPE '\\'")
            params: list[Any] = [f"%{_escape_like(memory_id)}%"]
            if workspace_id is not None:
                sql += " AND (workspace_id=? OR workspace_id IS NULL)"
                params.append(workspace_id)
            rows = self.conn.fetchall(sql, params)
        ids_to_close: list[str] = []
        for row in rows:
            prov = _loads(row["provenance"], {})
            supports = _provenance_memory_ids(prov)
            if memory_id not in supports:
                continue
            self.conn.execute(
                "UPDATE edge_supports SET valid_to=? WHERE edge_id=? AND memory_id=? "
                "AND valid_to IS NULL AND expired_at IS NULL",
                (ts, row["id"], memory_id),
            )
            normalized_remaining = [r["memory_id"] for r in self.conn.execute(
                "SELECT DISTINCT memory_id FROM edge_supports WHERE edge_id=? "
                "AND valid_to IS NULL AND expired_at IS NULL ORDER BY memory_id",
                (row["id"],),
            ).fetchall()]
            remaining = normalized_remaining or [mid for mid in supports if mid != memory_id]
            if not remaining:
                ids_to_close.append(row["id"])
                continue
            prov["memory_id"] = remaining[0]
            prov["memory_ids"] = remaining
            self.conn.execute("UPDATE edges SET provenance=? WHERE id=?",
                              (_dumps(prov), row["id"]))
        if ids_to_close:
            marks = ",".join("?" for _ in ids_to_close)
            self.conn.execute(f"UPDATE edges SET valid_to=? WHERE id IN ({marks})",
                              (ts, *ids_to_close))
            self.conn.execute(
                f"UPDATE edge_supports SET valid_to=? WHERE edge_id IN ({marks}) "
                "AND valid_to IS NULL AND expired_at IS NULL",
                (ts, *ids_to_close),
            )
        if commit:
            self.conn.commit()

    # ── memory-to-memory links (A-MEM style) ────────────────────────────────────
    def edge_supports_in_scope(self, edge_ids: Optional[list[str]] = None, *,
                               at: Optional[float] = None,
                               limit: Optional[int] = None) -> list[dict]:
        """Return live normalized evidence rows for graph inspection/scene scoring."""
        t = at if at is not None else now_ts()
        row_cap = None if limit is None else max(0, int(limit))
        if row_cap == 0:
            return []
        sql = (
            "SELECT id, edge_id, memory_id, source_kind, confidence, valid_from, "
            "valid_to, ingested_at, expired_at, provenance FROM edge_supports "
            "WHERE (valid_from IS NULL OR valid_from<=?) "
            "AND (valid_to IS NULL OR ?<valid_to) AND expired_at IS NULL"
        )
        params: list[Any] = [t, t]
        if edge_ids is not None:
            if not edge_ids:
                return []
            rows: list[dict] = []
            for start in range(0, len(edge_ids), IN_CLAUSE_CHUNK):
                if row_cap is not None and len(rows) >= row_cap:
                    break
                chunk = edge_ids[start:start + IN_CLAUSE_CHUNK]
                marks = ",".join("?" for _ in chunk)
                statement = sql + f" AND edge_id IN ({marks}) ORDER BY edge_id, memory_id, id"
                statement_params: tuple[Any, ...] = (*params, *chunk)
                if row_cap is not None:
                    statement += " LIMIT ?"
                    statement_params = (*statement_params, row_cap - len(rows))
                found = self.conn.execute(
                    statement, statement_params,
                ).fetchall()
                rows.extend(dict(row) for row in found)
            return rows
        statement = sql + " ORDER BY edge_id, memory_id, id"
        statement_params: tuple[Any, ...] = tuple(params)
        if row_cap is not None:
            statement += " LIMIT ?"
            statement_params = (*statement_params, row_cap)
        return [dict(row) for row in self.conn.execute(
            statement, statement_params
        ).fetchall()]

    def add_link(self, a: str, b: str, relation: str = "related",
                 layer: Optional[GraphLayer] = None, reason: str = "",
                 *, commit: bool = True) -> None:
        """Idempotent per (pair, relation): re-linking the same two memories with the
        same relation is a no-op in either direction, so auto-evolution and explicit
        ``engraphis_link`` calls can't accrete duplicate rows."""
        existing = self.conn.execute(
            "SELECT rowid, layer, reason FROM mem_links "
            "WHERE ((a=? AND b=?) OR (a=? AND b=?)) AND relation=? LIMIT 1",
            (a, b, b, a, relation),
        ).fetchone()
        if existing:
            updates: list[str] = []
            params: list[Any] = []
            if layer is not None:
                graph_layer = normalize_graph_layer(layer, relation).value
                if existing["layer"] != graph_layer:
                    updates.append("layer=?")
                    params.append(graph_layer)
            if reason and existing["reason"] != reason:
                updates.append("reason=?")
                params.append(reason)
            if updates:
                params.append(existing["rowid"])
                self.conn.execute(
                    f"UPDATE mem_links SET {', '.join(updates)} WHERE rowid=?",
                    params,
                )
                if commit:
                    self.conn.commit()
            return
        graph_layer = normalize_graph_layer(layer, relation).value
        self.conn.execute(
            "INSERT INTO mem_links(a, b, relation, layer, reason, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (a, b, relation, graph_layer, reason, now_ts()),
        )
        if commit:
            self.conn.commit()

    def has_link(self, a: str, b: str, *, relation: Optional[str] = None) -> bool:
        sql = "SELECT 1 FROM mem_links WHERE ((a=? AND b=?) OR (a=? AND b=?))"
        params: list[Any] = [a, b, b, a]
        if relation is not None:
            sql += " AND relation=?"
            params.append(relation)
        return self.conn.execute(sql + " LIMIT 1", params).fetchone() is not None

    def get_links(self, memory_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT a, b, relation, layer, reason, created_at "
            "FROM mem_links WHERE a=? OR b=?",
            (memory_id, memory_id),
        ).fetchall()
        return [dict(r) for r in rows]

    def edges_in_scope(self, flt: Optional[SearchFilter] = None,
                       *, at: Optional[float] = None,
                       limit: Optional[int] = None) -> list[Edge]:
        """Every edge valid at ``at`` within the filter's workspace/repo — the graph
        the PPR retrieval arm walks (edges outside their validity window are invisible,
        same bi-temporal rule as memories)."""
        t = at if at is not None else now_ts()
        sql = ("SELECT * FROM edges WHERE (valid_from IS NULL OR valid_from<=?) "
               "AND (valid_to IS NULL OR ?<valid_to) AND expired_at IS NULL")
        params: list[Any] = [t, t]
        if flt and flt.workspace_id:
            sql += " AND workspace_id=?"
            params.append(flt.workspace_id)
        if flt and flt.repo_id:
            sql += " AND (repo_id=? OR repo_id IS NULL)" if flt.include_ancestors else " AND repo_id=?"
            params.append(flt.repo_id)
        if flt and flt.graph_layers is not None:
            if not flt.graph_layers:
                return []
            marks = ",".join("?" for _ in flt.graph_layers)
            sql += f" AND layer IN ({marks})"
            params.extend(_enum(layer) for layer in flt.graph_layers)
        if limit is not None:
            sql += " LIMIT ?"
            params.append(max(0, int(limit)))
        rows = self.conn.execute(sql, params).fetchall()
        return [_row_to_edge(r) for r in rows]

    def links_among(self, ids: list[str], *,
                    layers: Optional[list[GraphLayer]] = None) -> list[dict]:
        """mem_links rows where *both* endpoints are in ``ids`` (for graph retrieval)."""
        if not ids:
            return []
        marks = ",".join("?" for _ in ids)
        sql = (
            f"SELECT a, b, relation, layer, reason FROM mem_links "
            f"WHERE a IN ({marks}) AND b IN ({marks})"
        )
        params: list[Any] = [*ids, *ids]
        if layers:
            layer_marks = ",".join("?" for _ in layers)
            sql += f" AND layer IN ({layer_marks})"
            params.extend(_enum(layer) for layer in layers)
        rows = self.conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def neighbors(self, node_ids: list[str], *, at: Optional[float] = None,
                  layers: Optional[list[GraphLayer]] = None) -> list[Edge]:
        if not node_ids:
            return []
        t = at if at is not None else now_ts()
        marks = ",".join("?" for _ in node_ids)
        sql = (
            f"SELECT * FROM edges WHERE (src IN ({marks}) OR dst IN ({marks})) "
            f"AND (valid_from IS NULL OR valid_from<=?) AND (valid_to IS NULL OR ?<valid_to) "
            f"AND expired_at IS NULL"
        )
        params: list[Any] = [*node_ids, *node_ids, t, t]
        if layers:
            layer_marks = ",".join("?" for _ in layers)
            sql += f" AND layer IN ({layer_marks})"
            params.extend(_enum(layer) for layer in layers)
        rows = self.conn.execute(sql, params).fetchall()
        return [_row_to_edge(r) for r in rows]

    # ── code symbol graph ────────────────────────────────────────────────────────
    def clear_symbols_for_file(self, repo_id: str, file: str, *,
                               commit: bool = True) -> None:
        """Re-indexing a file replaces its symbols/edges — incremental indexing is
        idempotent per file, not additive."""
        symbol_rows = self.conn.execute(
            "SELECT id FROM symbols WHERE repo_id=? AND file=?", (repo_id, file)
        ).fetchall()
        symbol_ids = [row["id"] for row in symbol_rows]
        if symbol_ids:
            marks = ",".join("?" for _ in symbol_ids)
            self.conn.execute(
                f"DELETE FROM code_memory_links WHERE repo_id=? "
                f"AND symbol_id IN ({marks})",
                (repo_id, *symbol_ids),
            )
        self.conn.execute("DELETE FROM symbols WHERE repo_id=? AND file=?", (repo_id, file))
        self.conn.execute("DELETE FROM code_edges WHERE repo_id=? AND file=?", (repo_id, file))
        if commit:
            self.conn.commit()

    def upsert_symbol(self, *, repo_id: str, kind: str, name: str, fqname: str, file: str,
                      span: str, signature: str = "", docstring: str = "",
                      lang: str = "", exported: bool = False,
                      content_hash: str = "", commit: bool = True) -> str:
        sid = ids.new_id("symbol")
        self.conn.execute(
            "INSERT INTO symbols(id, repo_id, kind, name, fqname, file, span, signature, "
            "docstring, lang, exported, content_hash, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, repo_id, kind, name, fqname, file, span, signature, docstring,
             lang, int(exported), content_hash, now_ts()),
        )
        if commit:
            self.conn.commit()
        return sid

    def add_code_edge(self, *, repo_id: str, src: str, dst: str, relation: str,
                      file: str = "", line: int = 0, layer: Optional[GraphLayer] = None,
                      commit: bool = True) -> str:
        eid = ids.new_id("edge")
        graph_layer = normalize_graph_layer(layer, relation)
        if layer is None and graph_layer == GraphLayer.SEMANTIC:
            graph_layer = GraphLayer.ENTITY
        self.conn.execute(
            "INSERT INTO code_edges(id, repo_id, src, dst, relation, layer, file, line) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (eid, repo_id, src, dst, relation, graph_layer.value, file, line),
        )
        if commit:
            self.conn.commit()
        return eid

    def get_code_file(self, repo_id: str, file: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM code_files WHERE repo_id=? AND file=?", (repo_id, file)
        ).fetchone()
        return dict(row) if row else None

    def list_code_files(self, repo_id: str, *,
                        languages: Optional[set] = None,
                        limit: Optional[int] = None) -> list[dict]:
        sql = "SELECT * FROM code_files WHERE repo_id=?"
        params: list[Any] = [repo_id]
        if languages:
            marks = ",".join("?" for _ in languages)
            sql += f" AND lang IN ({marks})"
            params.extend(sorted(languages))
        sql += " ORDER BY file"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(max(0, int(limit)))  # never -1 == SQLite "unlimited"
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def upsert_code_file(self, *, repo_id: str, file: str, lang: str,
                         content_hash: str, size_bytes: int, mtime_ns: int,
                         backend: str, commit: bool = True) -> None:
        self.conn.execute(
            "INSERT INTO code_files(repo_id, file, lang, content_hash, size_bytes, "
            "mtime_ns, backend, indexed_at) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(repo_id, file) DO UPDATE SET "
            "lang=excluded.lang, content_hash=excluded.content_hash, "
            "size_bytes=excluded.size_bytes, mtime_ns=excluded.mtime_ns, "
            "backend=excluded.backend, indexed_at=excluded.indexed_at",
            (repo_id, file, lang, content_hash, int(size_bytes), int(mtime_ns),
             backend, now_ts()),
        )
        if commit:
            self.conn.commit()

    def remove_code_file(self, repo_id: str, file: str, *, commit: bool = True) -> None:
        self.clear_symbols_for_file(repo_id, file, commit=False)
        self.conn.execute("DELETE FROM code_files WHERE repo_id=? AND file=?", (repo_id, file))
        if commit:
            self.conn.commit()

    def update_repo_index(self, repo_id: str, *, root_path: str,
                          primary_lang: str = "", settings: Optional[dict] = None) -> None:
        row = self.conn.execute("SELECT settings FROM repos WHERE id=?", (repo_id,)).fetchone()
        current = _loads(row["settings"], {}) if row else {}
        if settings:
            current.update(settings)
        self.conn.execute(
            "UPDATE repos SET root_path=?, primary_lang=?, indexed_at=?, settings=? WHERE id=?",
            (root_path, primary_lang or None, now_ts(), _dumps(current), repo_id),
        )
        self.conn.commit()

    def list_symbols(self, repo_id: str, *, limit: Optional[int] = None) -> list[dict]:
        sql = "SELECT * FROM symbols WHERE repo_id=? ORDER BY file, fqname"
        params: list[Any] = [repo_id]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(max(0, int(limit)))  # never -1 == SQLite "unlimited"
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def list_symbols_page(self, repo_id: str, *,
                          after: Optional[tuple[str, str, str]] = None,
                          limit: int = 500) -> list[dict]:
        sql = "SELECT * FROM symbols WHERE repo_id=?"
        params: list[Any] = [repo_id]
        if after is not None:
            file, fqname, symbol_id = after
            sql += (
                " AND (file>? OR (file=? AND fqname>?) "
                "OR (file=? AND fqname=? AND id>?))"
            )
            params.extend((file, file, fqname, file, fqname, symbol_id))
        sql += " ORDER BY file, fqname, id LIMIT ?"
        params.append(max(1, int(limit)))
        return [dict(row) for row in self.conn.execute(sql, params).fetchall()]

    def list_code_edges(self, repo_id: str, *, limit: Optional[int] = None,
                        layers: Optional[list[GraphLayer]] = None) -> list[dict]:
        sql = "SELECT * FROM code_edges WHERE repo_id=?"
        params: list[Any] = [repo_id]
        if layers is not None:
            if not layers:
                return []
            marks = ",".join("?" for _ in layers)
            sql += f" AND layer IN ({marks})"
            params.extend(_enum(layer) for layer in layers)
        sql += " ORDER BY file, line, id"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(max(0, int(limit)))  # never -1 == SQLite "unlimited"
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def symbols_for_files(self, repo_id: str, files: list[str]) -> list[dict]:
        if not files:
            return []
        marks = ",".join("?" for _ in files)
        rows = self.conn.execute(
            f"SELECT * FROM symbols WHERE repo_id=? AND file IN ({marks}) "
            "ORDER BY file, fqname",
            (repo_id, *files),
        ).fetchall()
        return [dict(r) for r in rows]

    def count_code_edges(self, repo_id: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM code_edges WHERE repo_id=?", (repo_id,)
        ).fetchone()
        return int(row["n"]) if row else 0

    def search_symbols(self, repo_id: str, query: str, *, limit: int = 20) -> list[dict]:
        """Substring match on name/fqname (no embedding yet — v1 is lexical)."""
        like = f"%{_escape_like(query)}%"
        rows = self.conn.execute(
            "SELECT * FROM symbols WHERE repo_id=? AND (name LIKE ? ESCAPE '\\' OR fqname LIKE ? ESCAPE '\\') "
            "ORDER BY name LIMIT ?",
            (repo_id, like, like, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_symbol_callers(self, repo_id: str, name: str, *, limit: int = 50) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM code_edges WHERE repo_id=? AND dst=? AND relation='calls' LIMIT ?",
            (repo_id, name, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def count_symbols(self, repo_id: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM symbols WHERE repo_id=?", (repo_id,)
        ).fetchone()
        return int(row["n"]) if row else 0

    def link_memory_symbol(self, *, repo_id: str, symbol_id: str, memory_id: str,
                           relation: str = "mentions", confidence: float = 1.0,
                           commit: bool = True) -> str:
        link_id = ids.new_id("edge")
        self.conn.execute(
            "INSERT OR IGNORE INTO code_memory_links("
            "id, repo_id, symbol_id, memory_id, relation, confidence, created_at"
            ") VALUES (?,?,?,?,?,?,?)",
            (link_id, repo_id, symbol_id, memory_id, relation,
             max(0.0, min(1.0, float(confidence))), now_ts()),
        )
        row = self.conn.execute(
            "SELECT id FROM code_memory_links WHERE repo_id=? AND symbol_id=? "
            "AND memory_id=? AND relation=?",
            (repo_id, symbol_id, memory_id, relation),
        ).fetchone()
        if commit:
            self.conn.commit()
        return row["id"] if row else link_id

    def clear_code_memory_links(self, repo_id: str, *, commit: bool = True) -> None:
        self.conn.execute("DELETE FROM code_memory_links WHERE repo_id=?", (repo_id,))
        if commit:
            self.conn.commit()

    def clear_code_memory_links_for_memories(self, repo_id: str, memory_ids: list[str],
                                             *, commit: bool = True) -> None:
        if not memory_ids:
            return
        marks = ",".join("?" for _ in memory_ids)
        self.conn.execute(
            f"DELETE FROM code_memory_links WHERE repo_id=? AND memory_id IN ({marks})",
            (repo_id, *memory_ids),
        )
        if commit:
            self.conn.commit()

    def prune_code_memory_links(self, repo_id: str, *, commit: bool = True) -> None:
        """Remove bridges whose repo-associated memory is no longer live."""
        t = now_ts()
        self.conn.execute(
            "DELETE FROM code_memory_links WHERE repo_id=? AND NOT EXISTS ("
            "SELECT 1 FROM memories AS m WHERE m.id=code_memory_links.memory_id AND m.repo_id=? "
            "AND (m.valid_from IS NULL OR m.valid_from<=?) "
            "AND (m.valid_to IS NULL OR ?<m.valid_to) AND m.expired_at IS NULL"
            ")",
            (repo_id, repo_id, t, t),
        )
        if commit:
            self.conn.commit()


    def list_code_memory_links(self, repo_id: str, *,
                               flt: Optional[SearchFilter] = None,
                               limit: Optional[int] = None) -> list[dict]:
        sql = (
            "SELECT l.*, s.name, s.fqname, s.file, s.kind AS symbol_kind, "
            "m.title, m.mtype, m.valid_to, m.expired_at "
            "FROM code_memory_links l "
            "LEFT JOIN symbols s ON s.id=l.symbol_id "
            "LEFT JOIN memories m ON m.id=l.memory_id "
            "WHERE l.repo_id=?"
        )
        params: list[Any] = [repo_id]
        if flt is not None:
            where, visibility_params = self._where(flt, include_invalid=False, alias="m")
            if where:
                sql += " AND " + " AND ".join(where)
                params.extend(visibility_params)
        sql += " ORDER BY l.created_at, l.id"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(max(0, int(limit)))  # never -1 == SQLite "unlimited"
        rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def memories_for_symbol(self, repo_id: str, symbol_id: str, *,
                            flt: Optional[SearchFilter] = None,
                            limit: int = 20) -> list[dict]:
        sql = (
            "SELECT m.id, m.title, m.content, m.mtype, m.scope, m.importance, "
            "m.provenance, l.relation, l.confidence "
            "FROM code_memory_links l JOIN memories m ON m.id=l.memory_id "
            "WHERE l.repo_id=? AND l.symbol_id=?"
        )
        params: list[Any] = [repo_id, symbol_id]
        where, visibility_params = self._where(flt, include_invalid=False, alias="m")
        if where:
            sql += " AND " + " AND ".join(where)
            params.extend(visibility_params)
        sql += (
            " ORDER BY l.confidence DESC, m.importance DESC, m.ingested_at DESC LIMIT ?"
        )
        params.append(max(1, min(100, int(limit))))
        rows = self.conn.execute(sql, params).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["provenance"] = _loads(item.get("provenance"), {})
            out.append(item)
        return out

    def symbols_for_memory(self, repo_id: str, memory_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT s.*, l.relation, l.confidence FROM code_memory_links l "
            "JOIN symbols s ON s.id=l.symbol_id "
            "WHERE l.repo_id=? AND l.memory_id=? ORDER BY l.confidence DESC, s.fqname",
            (repo_id, memory_id),
        ).fetchall()
        return [dict(row) for row in rows]

    def memories_mentioning(self, repo_id: str, text: str, *,
                            flt: Optional[SearchFilter] = None,
                            limit: int = 10) -> list[dict]:
        escaped = str(text).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        sql = (
            "SELECT m.id, m.title, m.mtype FROM memories AS m "
            "WHERE m.repo_id=? AND (m.title LIKE ? ESCAPE '\\' "
            "OR m.content LIKE ? ESCAPE '\\')"
        )
        pattern = f"%{escaped}%"
        params: list[Any] = [repo_id, pattern, pattern]
        where, visibility_params = self._where(flt, include_invalid=False, alias="m")
        if where:
            sql += " AND " + " AND ".join(where)
            params.extend(visibility_params)
        sql += " ORDER BY m.ingested_at DESC LIMIT ?"
        params.append(max(0, int(limit)))
        return [dict(row) for row in self.conn.execute(sql, params).fetchall()]

    # ── events & audit ──────────────────────────────────────────────────────
    def append_event(self, *, kind: str, content: str, workspace_id: str = "",
                     repo_id: str = "", session_id: str = "", refs: Optional[list] = None,
                     interaction_level: str = "") -> str:
        eid = ids.new_id("event")
        owns_session_transaction = False
        try:
            if session_id:
                owns_session_transaction = self.begin_session_write(
                    session_id, workspace_id=workspace_id, repo_id=repo_id or None
                )
            self.conn.execute(
                "INSERT INTO events(id, workspace_id, repo_id, session_id, kind, content, refs, "
                "interaction_level, ts) VALUES (?,?,?,?,?,?,?,?,?)",
                (eid, workspace_id, repo_id, session_id, kind, content, _dumps(refs or []),
                 interaction_level, now_ts()),
            )
            self.conn.commit()
            return eid
        except BaseException:
            if (owns_session_transaction
                    and self.conn.transaction_owned_by_current_thread()):
                self.conn.rollback()
            raise

    def audit(self, actor: str, action: str, target: str, detail: str = "",
              *, commit: bool = True) -> None:
        self.conn.execute(
            "INSERT INTO audit(id, ts, actor, action, target, detail) VALUES (?,?,?,?,?,?)",
            (ids.new_id("audit"), now_ts(), actor, action, target, detail),
        )
        if commit:
            self.conn.commit()

    def record_receipt(self, operation: str, *, workspace_id: str = "",
                       repo_id: str = "", actor: str = "system",
                       target_count: int = 0, status: str = "ok",
                       metadata: Optional[dict] = None) -> dict:
        """Append a privacy-safe, tamper-evident operation receipt.

        The public payload intentionally excludes raw content, query text, titles,
        workspace/repo names, raw ids, and actor identity. Scope and actor are represented
        by one-way digests. Receipts are chained per workspace and the current count/head
        is anchored independently, so modification, reordering, interior deletion, and
        tail truncation are detectable during verification.
        """
        operation = str(operation or "unknown")[:80]
        actor = str(actor or "system")[:200]
        workspace_id = str(workspace_id or "")
        repo_id = str(repo_id or "")
        with self._receipt_lock:
            # The Python lock serializes threads sharing this Store. BEGIN IMMEDIATE also
            # serializes separate Store/process connections before predecessor selection,
            # preventing two Team workers from forking the same workspace chain.
            transaction_started = False
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                transaction_started = True
                ts = now_ts()
                receipt_id = ids.new_id("receipt")
                scope_digest = hashlib.sha256(
                    f"{workspace_id}\0{repo_id}".encode("utf-8")
                ).hexdigest()[:24]
                actor_digest = hashlib.sha256(actor.encode("utf-8")).hexdigest()[:16]
                chain = self.conn.execute(
                    "SELECT COUNT(*) AS n, "
                    "COALESCE((SELECT receipt_hash FROM operation_receipts "
                    "WHERE workspace_id=? ORDER BY rowid DESC LIMIT 1), '') AS head "
                    "FROM operation_receipts WHERE workspace_id=?",
                    (workspace_id, workspace_id),
                ).fetchone()
                current_count = int(chain["n"] or 0)
                prev_hash = str(chain["head"] or "")
                anchor = self.conn.execute(
                    "SELECT receipt_count, head_hash, integrity_error "
                    "FROM receipt_chain_heads "
                    "WHERE workspace_id=?",
                    (workspace_id,),
                ).fetchone()
                anchor_error = str(anchor["integrity_error"] or "") if anchor else ""
                if anchor is not None and (
                    int(anchor["receipt_count"]) != current_count
                    or str(anchor["head_hash"]) != prev_hash
                ):
                    # Preserve evidence of the mismatch without bricking the operation that
                    # requested this receipt. The new receipt continues from the rows that
                    # actually remain, while verification stays invalid until an explicit
                    # repair/export decision clears the persistent integrity marker.
                    anchor_error = anchor_error or "pre_append_anchor_mismatch"
                safe_meta = _receipt_metadata(metadata or {})
                payload_obj = {
                    "version": 1,
                    "id": receipt_id,
                    "ts_ms": int(ts * 1000),
                    "operation": operation,
                    "scope_digest": scope_digest,
                    "actor_digest": actor_digest,
                    "target_count": max(0, int(target_count)),
                    "status": str(status or "ok")[:40],
                    "metadata": safe_meta,
                    "prev_hash": prev_hash,
                }
                payload = json.dumps(
                    payload_obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                )
                receipt_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
                self.conn.execute(
                    "INSERT INTO operation_receipts(id, ts, operation, workspace_id, repo_id, "
                    "scope_digest, actor, target_count, status, payload, prev_hash, "
                    "receipt_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        receipt_id, ts, operation, workspace_id, repo_id, scope_digest,
                        actor_digest, payload_obj["target_count"], payload_obj["status"],
                        payload, prev_hash, receipt_hash,
                    ),
                )
                self.conn.execute(
                    "INSERT INTO receipt_chain_heads "
                    "(workspace_id, receipt_count, head_hash, integrity_error, updated_at) "
                    "VALUES (?,?,?,?,?) "
                    "ON CONFLICT(workspace_id) DO UPDATE SET "
                    "receipt_count=excluded.receipt_count, "
                    "head_hash=excluded.head_hash, "
                    "integrity_error=CASE "
                    "WHEN receipt_chain_heads.integrity_error!='' "
                    "THEN receipt_chain_heads.integrity_error "
                    "ELSE excluded.integrity_error END, "
                    "updated_at=excluded.updated_at",
                    (workspace_id, current_count + 1, receipt_hash, anchor_error, ts),
                )
                self.conn.commit()
                return {**payload_obj, "hash": receipt_hash}
            except Exception:
                if transaction_started:
                    self.conn.rollback()
                raise

    def list_receipts(self, *, workspace_id: str, limit: int = 100) -> list[dict]:
        rows = self.conn.execute(
            "SELECT payload, receipt_hash FROM operation_receipts WHERE workspace_id=? "
            "ORDER BY rowid DESC LIMIT ?",
            (workspace_id, max(1, min(10_000, int(limit)))),
        ).fetchall()
        out = []
        for row in rows:
            payload = _loads(row["payload"], {})
            if isinstance(payload, dict):
                payload["hash"] = row["receipt_hash"]
                out.append(payload)
        return out

    def verify_receipts(self, *, workspace_id: str, expected_head: str = "",
                        expected_count: Optional[int] = None) -> dict:
        rows = self.conn.execute(
            "SELECT id, payload, prev_hash, receipt_hash FROM operation_receipts "
            "WHERE workspace_id=? ORDER BY rowid ASC",
            (workspace_id,),
        ).fetchall()
        previous = ""
        errors: list[dict] = []
        for index, row in enumerate(rows):
            actual = hashlib.sha256(row["payload"].encode("utf-8")).hexdigest()
            if actual != row["receipt_hash"]:
                errors.append({"index": index, "id": row["id"], "error": "hash_mismatch"})
            payload = _loads(row["payload"], {})
            if not isinstance(payload, dict) or payload.get("id") != row["id"] \
                    or payload.get("prev_hash") != row["prev_hash"]:
                errors.append({"index": index, "id": row["id"],
                               "error": "payload_mismatch"})
            if row["prev_hash"] != previous:
                errors.append({"index": index, "id": row["id"], "error": "chain_break"})
            previous = row["receipt_hash"]
        anchor = self.conn.execute(
            "SELECT receipt_count, head_hash, integrity_error "
            "FROM receipt_chain_heads WHERE workspace_id=?",
            (workspace_id,),
        ).fetchone()
        if rows and anchor is None:
            errors.append({"index": len(rows), "id": "", "error": "missing_anchor"})
        elif anchor is not None:
            if int(anchor["receipt_count"]) != len(rows):
                errors.append({
                    "index": len(rows), "id": "", "error": "anchor_count_mismatch",
                })
            if str(anchor["head_hash"]) != previous:
                errors.append({
                    "index": len(rows), "id": "", "error": "anchor_head_mismatch",
                })
            if str(anchor["integrity_error"] or ""):
                errors.append({
                    "index": len(rows), "id": "", "error": "anchor_integrity_error",
                })
        expected_head = str(expected_head or "").strip()
        if expected_head and previous != expected_head:
            errors.append({
                "index": len(rows), "id": "", "error": "expected_head_mismatch",
            })
        if expected_count is not None:
            try:
                external_count = max(0, int(expected_count))
            except (TypeError, ValueError, OverflowError):
                external_count = -1
            if external_count != len(rows):
                errors.append({
                    "index": len(rows), "id": "", "error": "expected_count_mismatch",
                })
        return {
            "valid": not errors,
            "count": len(rows),
            "head": previous,
            "anchored": anchor is not None,
            "errors": errors,
        }

    # ── sync state (device identity + per-peer cursors) ─────────────────────────
    def get_sync_state(self, key: str) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM sync_state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_sync_state(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO sync_state(key, value, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, now_ts()),
        )
        self.conn.commit()

    def device_id(self) -> str:
        """Stable per-database device id (minted once, then persistent). Attributes
        sync bundles to their origin device so a store never re-applies its own
        writes; it is local metadata, never memory, and only ever leaves the machine
        inside a bundle header."""
        did = self.get_sync_state("device_id")
        if not did:
            did = ids.new_id("device")
            self.set_sync_state("device_id", did)
        return did

    # ── helpers ───────────────────────────────────────────────────────────────
    def _where(self, flt: Optional[SearchFilter], include_invalid: bool,
               alias: str = "") -> tuple[list[str], list[Any]]:
        p = f"{alias}." if alias else ""
        where: list[str] = []
        params: list[Any] = []
        if flt:
            if flt.workspace_id:
                where.append(f"{p}workspace_id=?")
                params.append(flt.workspace_id)
            if flt.include_ancestors:
                if flt.session_id:
                    if flt.repo_id:
                        where.append(
                            f"(({p}scope='session' AND {p}session_id=?) OR "
                            f"({p}scope='repo' AND {p}repo_id=?) OR "
                            f"{p}scope IN ('workspace','user'))"
                        )
                        params.extend((flt.session_id, flt.repo_id))
                    else:
                        where.append(
                            f"(({p}scope='session' AND {p}session_id=?) OR "
                            f"{p}scope IN ('workspace','user'))"
                        )
                        params.append(flt.session_id)
                elif flt.repo_id:
                    where.append(
                        f"(({p}scope='repo' AND {p}repo_id=?) OR "
                        f"{p}scope IN ('workspace','user'))"
                    )
                    params.append(flt.repo_id)
                else:
                    where.append(f"{p}scope<>'session'")
            else:
                if flt.repo_id:
                    where.append(f"{p}repo_id=?")
                    params.append(flt.repo_id)
                if flt.session_id:
                    where.append(f"{p}session_id=?")
                    params.append(flt.session_id)
            if flt.scopes:
                marks = ",".join("?" for _ in flt.scopes)
                where.append(f"{p}scope IN ({marks})")
                params.extend(_enum(s) for s in flt.scopes)
            if flt.mtypes:
                marks = ",".join("?" for _ in flt.mtypes)
                where.append(f"{p}mtype IN ({marks})")
                params.extend(_enum(m) for m in flt.mtypes)
        if not include_invalid:
            t = (flt.as_of if flt and flt.as_of is not None else now_ts())
            where.append(f"({p}valid_from IS NULL OR {p}valid_from<=?)")
            params.append(t)
            where.append(f"({p}valid_to IS NULL OR ?<{p}valid_to)")
            params.append(t)
            where.append(f"{p}expired_at IS NULL")
        return where, params


# ── row mapping ──────────────────────────────────────────────────────────────

def _enum(v: Any) -> str:
    return v.value if hasattr(v, "value") else str(v)


def _row_to_record(row: sqlite3.Row) -> MemoryRecord:
    return MemoryRecord(
        id=row["id"], content=row["content"],
        mtype=MemoryType(row["mtype"]), scope=Scope(row["scope"]),
        workspace_id=row["workspace_id"], repo_id=row["repo_id"], session_id=row["session_id"],
        title=row["title"] or "", summary=row["summary"] or "",
        keywords=_loads(row["keywords"], []), metadata=_loads(row["metadata"], {}),
        importance=row["importance"], surprise=row["surprise"], stability=row["stability"],
        access_count=row["access_count"], last_access=row["last_access"],
        valid_from=row["valid_from"], valid_to=row["valid_to"],
        ingested_at=row["ingested_at"], expired_at=row["expired_at"],
        pinned=bool(row["pinned"]), sensitivity=row["sensitivity"],
        provenance=_loads(row["provenance"], {}),
    )


def _row_to_edge(row: sqlite3.Row) -> Edge:
    return Edge(
        id=row["id"], src=row["src"], dst=row["dst"], relation=row["relation"],
        layer=normalize_graph_layer(
            row["layer"] if "layer" in row.keys() else None, row["relation"]
        ),
        weight=row["weight"], workspace_id=row["workspace_id"] if "workspace_id" in row.keys() else None,
        repo_id=row["repo_id"] if "repo_id" in row.keys() else None,
        valid_from=row["valid_from"], valid_to=row["valid_to"],
        ingested_at=row["ingested_at"], expired_at=row["expired_at"],
        provenance=_loads(row["provenance"], {}),
    )


def _fts_query(q: str) -> str:
    """Make a safe FTS5 MATCH query: OR the alphanumeric terms as prefixes."""
    terms = [t for t in "".join(c if c.isalnum() else " " for c in q).split() if t]
    return " OR ".join(f'{t}*' for t in terms) if terms else '""'
