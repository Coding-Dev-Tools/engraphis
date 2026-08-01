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
import math
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


_PUBLIC_RECEIPT_LABELS_BY_KEY = {
    "mtype": {"working", "episodic", "semantic", "procedural"},
    "scope": {"session", "repo", "workspace", "user"},
    "resolution": {"add", "noop", "invalidate", "relate"},
    "retention": {
        "ephemeral", "normal", "critical", "short", "standard", "long", "permanent",
    },
    "intent": {
        "recall", "recall_context", "grounded", "http_read_only",
        "explain", "timeline", "code", "locate_code",
    },
    "relation": {
        "related", "mentions", "supports", "supersedes", "consolidates",
        "promotes", "causes", "depends_on", "calls", "imports", "references",
        "implements", "tests", "uses", "owned_by", "co_occurs",
    },
    "layer": {"temporal", "entity", "causal", "semantic"},
    "retrieval_profile": {"balanced", "auto", "lexical", "graph", "code"},
    "candidate_depth": {"fixed", "adaptive"},
    "response_mode": {"full", "compact"},
    "adaptive_mode": {
        "history_bypass", "retrieval", "history_fallback", "low_confidence_abstain",
    },
}


def _receipt_metadata(metadata: dict) -> dict:
    """Keep receipt metadata useful but content-free and bounded."""
    allowed = {
        "mtype", "scope", "resolution", "retention", "extracted", "intent", "k",
        "result_count", "grounded", "citations", "relation", "layer", "graph_layers",
        "files_scanned", "files_indexed", "files_removed", "symbols", "edges",
        "entities", "relations", "tables", "dry_run", "error_count",
        "entities_added", "relations_added",
        "retrieval_profile", "candidate_depth", "candidate_k_requested",
        "candidate_k_used", "response_mode", "historical", "token_usage",
        "adaptive_mode",
    }
    def content_free_label(key: str, value: str) -> str:
        normalized = value.strip().casefold().replace(" ", "_")
        if normalized in _PUBLIC_RECEIPT_LABELS_BY_KEY.get(key, set()):
            return normalized
        return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()

    out: dict[str, Any] = {}
    for key in sorted(metadata, key=lambda item: str(item))[:24]:
        safe_key = str(key)[:64]
        if safe_key not in allowed:
            continue
        value = metadata[key]
        if safe_key == "token_usage":
            if not isinstance(value, dict):
                continue
            numeric = {
                name: value[name]
                for name in (
                    "budget_tokens", "context_tokens", "source_tokens", "saved_tokens",
                    "savings_ratio", "packed_count", "omitted_count",
                )
                if type(value.get(name)) in (int, float)
                and math.isfinite(float(value[name]))
            }
            counter = value.get("token_counter")
            if isinstance(counter, str):
                if counter in {"engraphis.regex.v1", "estimate_tokens"}:
                    numeric["token_counter"] = counter
                else:
                    numeric["token_counter"] = (
                        "sha256:" + hashlib.sha256(counter.encode("utf-8")).hexdigest()
                    )
            out[safe_key] = numeric
        elif isinstance(value, bool) or value is None:
            out[safe_key] = value
        elif isinstance(value, (int, float)):
            if math.isfinite(float(value)):
                out[safe_key] = value
        elif isinstance(value, str):
            out[safe_key] = content_free_label(safe_key, value)
        elif isinstance(value, (list, tuple)):
            out[safe_key] = len(value)
    return out


_PUBLIC_RECEIPT_ID = re.compile(r"^rcpt_[0-9ABCDEFGHJKMNPQRSTVWXYZ]{26}$")
_PUBLIC_RECEIPT_HASH = re.compile(r"^[0-9a-f]{64}$")
_PUBLIC_RECEIPT_HASHED_LABEL = re.compile(r"^sha256:[0-9a-f]{64}$")
_PUBLIC_RECEIPT_KEYS = {
    "version", "id", "ts_ms", "operation", "scope_digest", "actor_digest",
    "target_count", "status", "metadata", "prev_hash",
}
_PUBLIC_RECEIPT_METADATA_KEYS = {
    "mtype", "scope", "resolution", "retention", "extracted", "intent", "k",
    "result_count", "grounded", "citations", "relation", "layer", "graph_layers",
    "files_scanned", "files_indexed", "files_removed", "symbols", "edges",
    "entities", "relations", "tables", "dry_run", "error_count",
    "entities_added", "relations_added", "retrieval_profile", "candidate_depth",
    "candidate_k_requested", "candidate_k_used", "response_mode", "historical",
    "token_usage", "adaptive_mode",
}
_PUBLIC_RECEIPT_OPERATIONS = {
    "remember", "recall", "promote", "link", "index_repo",
    "graph_index", "grounded_recall", "adaptive_context", "consolidate", "sync",
}
_PUBLIC_RECEIPT_STATUSES = {
    "ok", "add", "noop", "invalidate", "relate", "ingested",
    "postgres_schema", "grounded", "abstained", "promoted",
    "indexed", "skipped", "error", "failed", "cancelled", "partial",
}


def _receipt_scope_digest(workspace_id: str, repo_id: Optional[str]) -> str:
    """Return the signed scope binding for an operation receipt."""
    return hashlib.sha256(
        f"{workspace_id}\0{repo_id or ''}".encode("utf-8")
    ).hexdigest()[:24]


def _redacted_receipt_value(value: Any) -> str:
    raw = value if isinstance(value, str) else str(value or "")
    return "redacted_sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _public_receipt_row(row: dict) -> dict:
    """Return one validated content-free receipt or a hash-only corruption marker."""
    raw = row.get("payload")
    raw = raw if isinstance(raw, str) else str(raw or "")
    raw_id = row.get("id")
    raw_prev = row.get("prev_hash")
    raw_hash = row.get("receipt_hash")

    def safe_id() -> str:
        value = raw_id if isinstance(raw_id, str) else str(raw_id or "")
        return value if _PUBLIC_RECEIPT_ID.fullmatch(value) else _redacted_receipt_value(value)

    def safe_hash(value: Any, *, allow_empty: bool = False) -> str:
        text = value if isinstance(value, str) else str(value or "")
        if allow_empty and not text:
            return ""
        return (
            text if _PUBLIC_RECEIPT_HASH.fullmatch(text)
            else _redacted_receipt_value(text)
        )

    invalid = {
        "id": safe_id(),
        "prev_hash": safe_hash(raw_prev, allow_empty=True),
        "hash": safe_hash(raw_hash),
        "invalid_payload": True,
        "payload_bytes": len(raw.encode("utf-8")),
        "payload_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
    }
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, RecursionError):
        return invalid
    if (
        not isinstance(payload, dict)
        or set(payload) != _PUBLIC_RECEIPT_KEYS
        or payload.get("version") != 1
        or payload.get("id") != raw_id
        or payload.get("prev_hash") != raw_prev
        or not isinstance(raw_id, str)
        or _PUBLIC_RECEIPT_ID.fullmatch(raw_id) is None
        or (
            raw_prev != ""
            and (
                not isinstance(raw_prev, str)
                or _PUBLIC_RECEIPT_HASH.fullmatch(raw_prev) is None
            )
        )
        or not isinstance(raw_hash, str)
        or _PUBLIC_RECEIPT_HASH.fullmatch(raw_hash) is None
        or hashlib.sha256(raw.encode("utf-8")).hexdigest() != raw_hash
    ):
        return invalid
    if type(payload.get("ts_ms")) is not int or payload["ts_ms"] < 0:
        return invalid
    if type(payload.get("target_count")) is not int or payload["target_count"] < 0:
        return invalid
    operation = payload.get("operation")
    if not (
        operation in _PUBLIC_RECEIPT_OPERATIONS
        or (
            isinstance(operation, str)
            and _PUBLIC_RECEIPT_HASHED_LABEL.fullmatch(operation)
        )
    ):
        return invalid
    status = payload.get("status")
    if not (
        status in _PUBLIC_RECEIPT_STATUSES
        or (
            isinstance(status, str)
            and _PUBLIC_RECEIPT_HASHED_LABEL.fullmatch(status)
        )
    ):
        return invalid
    if not (
        isinstance(payload.get("scope_digest"), str)
        and re.fullmatch(r"[0-9a-f]{24}", payload["scope_digest"])
        and isinstance(payload.get("actor_digest"), str)
        and re.fullmatch(r"[0-9a-f]{16}", payload["actor_digest"])
    ):
        return invalid
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict) or not set(metadata).issubset(
        _PUBLIC_RECEIPT_METADATA_KEYS
    ):
        return invalid
    for key, value in metadata.items():
        if key == "token_usage":
            if not isinstance(value, dict):
                return invalid
            allowed_usage = {
                "budget_tokens", "context_tokens", "source_tokens", "saved_tokens",
                "savings_ratio", "packed_count", "omitted_count", "token_counter",
            }
            if not set(value).issubset(allowed_usage):
                return invalid
            for usage_key, usage_value in value.items():
                if usage_key == "token_counter":
                    if not (
                        usage_value in {"engraphis.regex.v1", "estimate_tokens"}
                        or (
                            isinstance(usage_value, str)
                            and _PUBLIC_RECEIPT_HASHED_LABEL.fullmatch(usage_value)
                        )
                    ):
                        return invalid
                elif (
                    type(usage_value) not in (int, float)
                    or not math.isfinite(float(usage_value))
                ):
                    return invalid
        elif isinstance(value, str):
            public_labels = _PUBLIC_RECEIPT_LABELS_BY_KEY.get(key, set())
            if not (
                value in public_labels
                or _PUBLIC_RECEIPT_HASHED_LABEL.fullmatch(value)
            ):
                return invalid
        elif isinstance(value, bool) or value is None:
            continue
        elif type(value) not in (int, float) or not math.isfinite(float(value)):
            return invalid
    return {**payload, "hash": raw_hash}


def _fts5_available(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS _fts_probe USING fts5(x)")
        conn.execute("DROP TABLE IF EXISTS _fts_probe")
        return True
    except sqlite3.OperationalError:
        return False


def _temporal_anchors(flt: Optional[SearchFilter], *, valid_at: Optional[float] = None
                      ) -> tuple[float, float]:
    """Return world-time and system-time anchors for one read.

    ``valid_at`` is an explicit per-operation override used by graph traversal;
    otherwise the filter's normalized ``valid_at``/legacy ``as_of`` value applies.
    System-time defaults to the present, which preserves ordinary current reads.
    """
    world = valid_at
    if world is None and flt is not None:
        world = flt.valid_at
    known = flt.known_at if flt is not None else None
    present = now_ts()
    return (present if world is None else world,
            present if known is None else known)


def _temporal_visibility_sql(alias: str, flt: Optional[SearchFilter], *,
                             valid_at: Optional[float] = None) -> tuple[str, list[Any]]:
    """SQL predicate shared by temporal code-history reads."""
    world, known = _temporal_anchors(flt, valid_at=valid_at)
    p = f"{alias}." if alias else ""
    return (
        f"({p}valid_from IS NULL OR {p}valid_from<=?) "
        f"AND ({p}valid_to IS NULL OR ?<{p}valid_to "
        f"OR ({p}valid_to_recorded_at IS NOT NULL "
        f"AND ?<{p}valid_to_recorded_at)) "
        f"AND ({p}ingested_at IS NULL OR {p}ingested_at<=?) "
        f"AND ({p}expired_at IS NULL OR ?<{p}expired_at)",
        [world, world, known, known, known],
    )


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
    valid_at, known_at = _temporal_anchors(flt, valid_at=at)
    if rec.ingested_at is not None and rec.ingested_at > known_at:
        return False
    if rec.expired_at is not None and known_at >= rec.expired_at:
        return False
    if rec.valid_from is not None and rec.valid_from > valid_at:
        return False
    if (rec.valid_to is not None and valid_at >= rec.valid_to
            and not (
                rec.valid_to_recorded_at is not None
                and known_at < rec.valid_to_recorded_at
            )):
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

    def _backup_before_v4_migration(self, *, previous_version: int = 0) -> str:
        """Create and verify the mandatory pre-migration backup without mutating data.

        Source and destination both use the injected connector, so SQLCipher databases
        remain keyed throughout. The caller holds ``BEGIN IMMEDIATE`` on the primary
        connection, preventing another writer from changing the source between this
        snapshot and the migration commit. Only a quick-checked temporary backup may
        atomically replace the stable backup path; every failure aborts the migration.

        Each migration target needs its own durable recovery artifact.  For example, a
        v5 database can legitimately retain the immutable ``.pre-migration-v5.bak``
        created during its v4→v5 upgrade.  Reusing that name for a v5→v6 upgrade would
        compare the older v4 snapshot with the later v5 source and abort the upgrade.
        Preserve the legacy v4/v5 names and use the target schema version for newer
        backups.
        """
        if self.path in (":memory:", "") or self.path.startswith("file::memory:"):
            raise RuntimeError("schema migration requires a durable pre-migration backup")
        backup_version = max(4, min(SCHEMA_VERSION, previous_version + 1))
        backup_path = f"{self.path}.pre-migration-v{backup_version}.bak"
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
                f"schema v{backup_version} migration aborted: could not create and verify the "
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
        # Early v5 databases recorded direct memory links with only ``created_at``.
        # Track that shape independently of the version row so they receive the
        # missing bi-temporal fields and backfill on their next safe open.
        mem_link_columns: set[str] = set()
        if "mem_links" in object_names:
            mem_link_columns = {
                str(row["name"])
                for row in self.conn.execute("PRAGMA table_info(mem_links)").fetchall()
            }
        mem_links_need_temporal_backfill = not {
            "valid_from", "valid_to", "valid_to_recorded_at", "ingested_at", "expired_at",
        }.issubset(mem_link_columns)
        self._mem_links_need_temporal_backfill = mem_links_need_temporal_backfill
        if previous_version > SCHEMA_VERSION:
            raise RuntimeError(
                f"database schema {previous_version} is newer than supported "
                f"schema {SCHEMA_VERSION}"
            )
        needs_backup = bool(object_names) and (
            previous_version < SCHEMA_VERSION or mem_links_need_temporal_backfill
        )
        try:
            # Reserve the writer before the snapshot. This is read/locking state only;
            # every schema/data transform remains inside the transaction below.
            self.conn.execute("BEGIN IMMEDIATE")
            if needs_backup:
                self._backup_before_v4_migration(previous_version=previous_version)
            self._apply_schema(previous_version)
            self.conn.commit()
        except BaseException:
            if self.conn.in_transaction:
                self.conn.rollback()
            raise

    def _apply_schema(self, previous_version: int) -> None:
        mem_links_need_temporal_backfill = bool(
            getattr(self, "_mem_links_need_temporal_backfill", False)
        )
        receipt_sequence_existed = any(
            str(row["name"]) == "sequence"
            for row in self.conn.execute(
                "PRAGMA table_info(operation_receipts)"
            ).fetchall()
        )
        self._execute_script_transactional(SCHEMA_SQL)
        self.has_fts5 = _fts5_available(self.conn)
        self.conn.execute(FTS_SQL_FTS5 if self.has_fts5 else FTS_SQL_FALLBACK)
        # Additive columns for DBs created before they existed — CREATE TABLE IF NOT
        # EXISTS above is a no-op on an already-existing table, so new columns need an
        # explicit, idempotent ALTER TABLE here (SQLite has no "ADD COLUMN IF NOT EXISTS").
        for stmt in (
            "ALTER TABLE memories ADD COLUMN sort_order REAL",
            "ALTER TABLE memories ADD COLUMN subject_key TEXT DEFAULT ''",
            "ALTER TABLE memories ADD COLUMN claim_kind TEXT DEFAULT ''",
            "ALTER TABLE memories ADD COLUMN valid_to_recorded_at REAL",
            "ALTER TABLE edges ADD COLUMN layer TEXT DEFAULT 'semantic'",
            "ALTER TABLE entities ADD COLUMN normalized_name TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE entities ADD COLUMN canonical_method TEXT NOT NULL DEFAULT 'exact'",
            "ALTER TABLE entities ADD COLUMN canonical_confidence REAL NOT NULL DEFAULT 1.0",
            "ALTER TABLE mem_links ADD COLUMN layer TEXT DEFAULT 'semantic'",
            "ALTER TABLE mem_links ADD COLUMN reason TEXT DEFAULT ''",
            "ALTER TABLE mem_links ADD COLUMN valid_from REAL",
            "ALTER TABLE mem_links ADD COLUMN valid_to REAL",
            "ALTER TABLE mem_links ADD COLUMN valid_to_recorded_at REAL",
            "ALTER TABLE mem_links ADD COLUMN ingested_at REAL",
            "ALTER TABLE mem_links ADD COLUMN expired_at REAL",
            "ALTER TABLE code_edges ADD COLUMN layer TEXT DEFAULT 'entity'",
            "ALTER TABLE symbols ADD COLUMN docstring TEXT DEFAULT ''",
            "ALTER TABLE symbols ADD COLUMN valid_from REAL",
            "ALTER TABLE symbols ADD COLUMN valid_to REAL",
            "ALTER TABLE symbols ADD COLUMN valid_to_recorded_at REAL",
            "ALTER TABLE symbols ADD COLUMN ingested_at REAL",
            "ALTER TABLE symbols ADD COLUMN expired_at REAL",
            "ALTER TABLE code_edges ADD COLUMN valid_from REAL",
            "ALTER TABLE code_edges ADD COLUMN valid_to REAL",
            "ALTER TABLE code_edges ADD COLUMN valid_to_recorded_at REAL",
            "ALTER TABLE code_edges ADD COLUMN ingested_at REAL",
            "ALTER TABLE code_edges ADD COLUMN expired_at REAL",
            "ALTER TABLE edges ADD COLUMN valid_to_recorded_at REAL",
            "ALTER TABLE edge_supports ADD COLUMN valid_to_recorded_at REAL",
            "ALTER TABLE memory_entities ADD COLUMN valid_to_recorded_at REAL",
            "ALTER TABLE code_memory_links ADD COLUMN valid_to_recorded_at REAL",
            "ALTER TABLE receipt_chain_heads ADD COLUMN integrity_error TEXT DEFAULT ''",
            "ALTER TABLE operation_receipts ADD COLUMN sequence INTEGER",
            "ALTER TABLE jobs ADD COLUMN runner_id TEXT",
            "ALTER TABLE jobs ADD COLUMN heartbeat_at REAL",
        ):
            try:
                self.conn.execute(stmt)
            except sqlite3.OperationalError:
                pass  # column already exists
        # This cannot live in SCHEMA_SQL: CREATE TABLE IF NOT EXISTS leaves an
        # early-v5 ``mem_links`` table untouched, so the index would reference
        # temporal columns before the additive ALTERs above install them.
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_mem_links_temporal "
            "ON mem_links(a, valid_to, expired_at)"
        )
        self.conn.execute(
            "UPDATE operation_receipts SET workspace_id='' WHERE workspace_id IS NULL"
        )
        self.conn.execute(
            "UPDATE operation_receipts SET repo_id='' WHERE repo_id IS NULL"
        )
        if not receipt_sequence_existed:
            self._backfill_receipt_sequences()
        self._execute_script_transactional(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_receipt_sequence "
            "ON operation_receipts(workspace_id, sequence) "
            "WHERE sequence IS NOT NULL;"
            "DROP TRIGGER IF EXISTS trg_receipt_sequence_required;"
            "CREATE TRIGGER trg_receipt_sequence_required "
            "BEFORE INSERT ON operation_receipts "
            "WHEN NEW.sequence IS NULL OR typeof(NEW.sequence)!='integer' "
            "OR NEW.sequence<1 BEGIN "
            "SELECT RAISE(ABORT, 'receipt sequence is required'); END;"
            "DROP TRIGGER IF EXISTS trg_receipt_sequence_immutable;"
            "CREATE TRIGGER trg_receipt_sequence_immutable "
            "BEFORE UPDATE OF sequence ON operation_receipts "
            "WHEN NEW.sequence IS NOT OLD.sequence BEGIN "
            "SELECT RAISE(ABORT, 'receipt sequence is immutable'); END;"
        )
        # These are migration transforms, not startup maintenance. Re-running the
        # incidence backfill on every open scans the entire evidence graph and turns
        # otherwise constant-time startup into O(workspace history). The schema-version
        # row is written in the same transaction below, so an interrupted migration
        # remains < v5 and safely retries all three transforms.
        if previous_version < 5:
            self._migrate_code_history_v5()
            self._backfill_claim_identity_v5()
            self._backfill_memory_entities_v5()
        if previous_version < 5 or mem_links_need_temporal_backfill:
            self._migrate_mem_link_history_v5()
        if previous_version < 6:
            self._migrate_code_file_history_v6()
        if previous_version < 7:
            # v6 deterministic vectors predate aliases and measurement features.
            # ``MemoryEngine.create`` owns the actual re-embed because only it has
            # the configured Embedder and VectorIndex; this durable marker keeps a
            # failed/interrupted rebuild retryable on the next startup.
            self.conn.execute(
                "INSERT OR IGNORE INTO embedding_state(identity, version, updated_at) "
                "VALUES (?,?,?)",
                ("deterministic_hashing", "v1_legacy", now_ts()),
            )
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
        self._execute_script_transactional(
            "CREATE INDEX IF NOT EXISTS idx_mem_claim_live "
            "ON memories(workspace_id, repo_id, scope, mtype, subject_key, claim_kind) "
            "WHERE subject_key<>'' AND valid_to IS NULL AND expired_at IS NULL;"
            "CREATE INDEX IF NOT EXISTS idx_sym_repo_live "
            "ON symbols(repo_id, file, fqname, valid_to, expired_at);"
            "CREATE INDEX IF NOT EXISTS idx_code_edge_live "
            "ON code_edges(repo_id, file, valid_to, expired_at);"
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_code_mem_live_unique "
            "ON code_memory_links(repo_id, symbol_id, memory_id, relation) "
            "WHERE valid_to IS NULL AND expired_at IS NULL;"
            "CREATE INDEX IF NOT EXISTS idx_code_mem_symbol "
            "ON code_memory_links(repo_id, symbol_id);"
            "CREATE INDEX IF NOT EXISTS idx_code_mem_memory "
            "ON code_memory_links(repo_id, memory_id);"
            "CREATE INDEX IF NOT EXISTS idx_code_mem_live_symbol "
            "ON code_memory_links(repo_id, symbol_id, valid_to, expired_at);"
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
        if previous_version < 5:
            receipt_scopes = self.conn.execute(
                "SELECT r.workspace_id, COALESCE(MAX(r.ts), 0) AS updated_at "
                "FROM operation_receipts r "
                "LEFT JOIN receipt_chain_heads h ON h.workspace_id=r.workspace_id "
                "WHERE h.workspace_id IS NULL "
                "GROUP BY r.workspace_id"
            ).fetchall()
            for receipt_scope in receipt_scopes:
                workspace_id = str(receipt_scope["workspace_id"] or "")
                chain = self._receipt_chain_state(workspace_id)
                self.conn.execute(
                    "INSERT OR IGNORE INTO receipt_chain_heads "
                    "(workspace_id, receipt_count, head_hash, integrity_error, updated_at) "
                    "VALUES (?,?,?,?,?)",
                    (
                        workspace_id,
                        len(chain["rows"]),
                        chain["head"],
                        "" if not chain["errors"] else "migration_chain_invalid",
                        receipt_scope["updated_at"],
                    ),
                )
        self.conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?,?)",
            (SCHEMA_VERSION, now_ts()),
        )

    def _migrate_code_history_v5(self) -> None:
        """Give pre-v5 code graph rows open bi-temporal intervals.

        ``code_memory_links`` formerly had a table-level uniqueness constraint, which
        made it impossible to retain a closed link and later create the same live link.
        SQLite cannot drop that constraint in place, so rebuild that one narrow table
        transactionally before installing the partial live-uniqueness index.
        """
        stamp = now_ts()
        self.conn.execute(
            "UPDATE symbols SET valid_from=COALESCE(valid_from, updated_at, ?), "
            "ingested_at=COALESCE(ingested_at, updated_at, ?) "
            "WHERE valid_from IS NULL OR ingested_at IS NULL",
            (stamp, stamp),
        )
        self.conn.execute(
            "UPDATE code_edges SET valid_from=COALESCE(valid_from, ?), "
            "ingested_at=COALESCE(ingested_at, ?) "
            "WHERE valid_from IS NULL OR ingested_at IS NULL",
            (stamp, stamp),
        )
        columns = {
            row["name"] for row in self.conn.execute(
                "PRAGMA table_info(code_memory_links)"
            ).fetchall()
        }
        if "valid_from" not in columns:
            self.conn.execute(
                "CREATE TABLE code_memory_links_v5 ("
                "id TEXT PRIMARY KEY, repo_id TEXT NOT NULL, symbol_id TEXT NOT NULL, "
                "memory_id TEXT NOT NULL, relation TEXT DEFAULT 'mentions', "
                "confidence REAL DEFAULT 1.0, created_at REAL, valid_from REAL, "
                "valid_to REAL, valid_to_recorded_at REAL, "
                "ingested_at REAL, expired_at REAL)"
            )
            self.conn.execute(
                "INSERT INTO code_memory_links_v5("
                "id, repo_id, symbol_id, memory_id, relation, confidence, created_at, "
                "valid_from, ingested_at) "
                "SELECT id, repo_id, symbol_id, memory_id, relation, confidence, "
                "created_at, COALESCE(created_at, ?), COALESCE(created_at, ?) "
                "FROM code_memory_links",
                (stamp, stamp),
            )
            self.conn.execute("DROP TABLE code_memory_links")
            self.conn.execute("ALTER TABLE code_memory_links_v5 RENAME TO code_memory_links")
        else:
            self.conn.execute(
                "UPDATE code_memory_links SET valid_from=COALESCE(valid_from, created_at, ?), "
                "ingested_at=COALESCE(ingested_at, created_at, ?) "
                "WHERE valid_from IS NULL OR ingested_at IS NULL",
                (stamp, stamp),
            )

    def _migrate_mem_link_history_v5(self) -> None:
        """Give legacy direct memory links an open bi-temporal interval.

        ``created_at`` was the only historical signal on old rows, so it is both
        the best available world-time and system-time start. Rows without a clock
        start at migration time rather than being projected into every past view.
        """
        stamp = now_ts()
        self.conn.execute(
            "UPDATE mem_links SET valid_from=COALESCE(valid_from, created_at, ?), "
            "ingested_at=COALESCE(ingested_at, created_at, ?) "
            "WHERE valid_from IS NULL OR ingested_at IS NULL",
            (stamp, stamp),
        )

    def _migrate_code_file_history_v6(self) -> None:
        """Seed temporal file manifests from the v5 current-file snapshot."""
        stamp = now_ts()
        rows = self.conn.execute("SELECT * FROM code_files").fetchall()
        for row in rows:
            existing = self.conn.execute(
                "SELECT 1 FROM code_file_history WHERE repo_id=? AND file=? "
                "AND valid_to IS NULL AND expired_at IS NULL",
                (row["repo_id"], row["file"]),
            ).fetchone()
            if existing is None:
                started = row["indexed_at"] if row["indexed_at"] is not None else stamp
                self.conn.execute(
                    "INSERT INTO code_file_history("
                    "repo_id, file, lang, content_hash, size_bytes, mtime_ns, backend, "
                    "indexed_at, valid_from, ingested_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        row["repo_id"], row["file"], row["lang"], row["content_hash"],
                        row["size_bytes"], row["mtime_ns"], row["backend"],
                        row["indexed_at"], started, started,
                    ),
                )

    def _backfill_claim_identity_v5(self) -> None:
        """Lift already-present metadata hints into indexed, optional claim columns."""
        rows = self.conn.execute(
            "SELECT id, metadata, subject_key, claim_kind FROM memories"
        ).fetchall()
        for row in rows:
            metadata = _loads(row["metadata"], {})
            if not isinstance(metadata, dict):
                metadata = {}
            subject_key = str(row["subject_key"] or metadata.get("subject_key") or "").strip()
            claim_kind = str(row["claim_kind"] or metadata.get("claim_kind") or "").strip()
            if subject_key != (row["subject_key"] or "") or claim_kind != (row["claim_kind"] or ""):
                self.conn.execute(
                    "UPDATE memories SET subject_key=?, claim_kind=? WHERE id=?",
                    (subject_key, claim_kind, row["id"]),
                )

    def _backfill_memory_entities_v5(self, memory_id: Optional[str] = None) -> None:
        """Materialize deterministic incidence already evidenced by graph supports."""
        sql = (
            "SELECT s.memory_id, endpoint.entity_id, e.workspace_id, e.repo_id, "
            "s.confidence, s.valid_from, s.valid_to, s.valid_to_recorded_at, "
            "s.ingested_at, s.expired_at, "
            "e.valid_from AS edge_valid_from, e.valid_to AS edge_valid_to, "
            "e.valid_to_recorded_at AS edge_valid_to_recorded_at, "
            "e.ingested_at AS edge_ingested_at, e.expired_at AS edge_expired_at, "
            "s.provenance "
            "FROM edge_supports s JOIN edges e ON e.id=s.edge_id "
            "JOIN (SELECT id AS edge_id, src AS entity_id FROM edges "
            "UNION ALL SELECT id, dst FROM edges) endpoint ON endpoint.edge_id=e.id "
            "WHERE 1=1"
        )
        params: list[Any] = []
        if memory_id is not None:
            sql += " AND s.memory_id=?"
            params.append(memory_id)
        rows = self.conn.execute(sql, params).fetchall()
        for row in rows:
            valid_starts = [
                value for value in (row["valid_from"], row["edge_valid_from"])
                if value is not None
            ]
            valid_ends = [
                value for value in (row["valid_to"], row["edge_valid_to"])
                if value is not None
            ]
            known_starts = [
                value for value in (row["ingested_at"], row["edge_ingested_at"])
                if value is not None
            ]
            known_ends = [
                value for value in (row["expired_at"], row["edge_expired_at"])
                if value is not None
            ]
            valid_from = max(valid_starts) if valid_starts else None
            valid_to = min(valid_ends) if valid_ends else None
            closure_candidates = [
                (row["valid_to"], row["valid_to_recorded_at"]),
                (row["edge_valid_to"], row["edge_valid_to_recorded_at"]),
            ]
            controlling_closures = [
                recorded for end, recorded in closure_candidates
                if end is not None and end == valid_to
            ]
            valid_to_recorded_at = (
                None
                if not controlling_closures or any(
                    recorded is None for recorded in controlling_closures
                )
                else min(controlling_closures)
            )
            ingested_at = max(known_starts) if known_starts else None
            expired_at = min(known_ends) if known_ends else None
            if (valid_from is not None and valid_to is not None
                    and valid_from >= valid_to):
                continue
            if (ingested_at is not None and expired_at is not None
                    and ingested_at >= expired_at):
                continue
            self.link_memory_entity(
                memory_id=row["memory_id"], entity_id=row["entity_id"],
                workspace_id=row["workspace_id"], repo_id=row["repo_id"],
                source_kind="edge_support", confidence=row["confidence"],
                valid_from=valid_from, valid_to=valid_to,
                valid_to_recorded_at=valid_to_recorded_at,
                ingested_at=ingested_at, expired_at=expired_at,
                provenance=_loads(row["provenance"], {}), commit=False,
            )

    def backfill_memory_entities_for_memory(self, memory_id: str) -> None:
        """Materialize the evidence incidence for one freshly written memory."""
        self._backfill_memory_entities_v5(memory_id)

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
                    "UPDATE edges SET valid_to=?, valid_to_recorded_at=?, "
                    "provenance=? WHERE id=?",
                    (closed_at, closed_at, _dumps(provenance), row["id"]),
                )
            retired_marks = ",".join("?" for _ in retired_ids)
            self.conn.execute(
                "UPDATE edge_supports SET valid_to=?, valid_to_recorded_at=? "
                "WHERE edge_id IN ("
                + retired_marks + ") AND valid_to IS NULL AND expired_at IS NULL",
                (closed_at, closed_at, *retired_ids),
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
        # ``Store`` is a local-programmatic capability.  Stamp direct new writes
        # explicitly so prompt-facing recall can fail closed for genuinely legacy
        # rows without making current low-level integrations silently disappear.
        # External ingress (service/sync) provides its own stricter provenance.
        provenance = dict(rec.provenance or {})
        if "trusted" not in provenance:
            provenance.update({"source": provenance.get("source", "local_store"),
                               "trusted": True,
                               "trust_origin": provenance.get(
                                   "trust_origin", "local_store"
                               )})
        rec.provenance = provenance
        metadata = dict(rec.metadata or {})
        if not isinstance(metadata.get("provenance"), dict):
            metadata["provenance"] = dict(provenance)
        rec.metadata = metadata
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
        rec.ingested_at = rec.ingested_at if rec.ingested_at is not None else ts
        rec.valid_from = rec.valid_from if rec.valid_from is not None else ts
        rec.last_access = rec.last_access if rec.last_access is not None else ts
        self.conn.execute(
            """INSERT INTO memories
               (id, workspace_id, repo_id, session_id, scope, mtype, title, content, summary,
                keywords, metadata, importance, surprise, stability, access_count, last_access,
                valid_from, valid_to, valid_to_recorded_at, ingested_at, expired_at,
                subject_key, claim_kind,
                pinned, sensitivity, provenance)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                workspace_id=excluded.workspace_id, repo_id=excluded.repo_id,
                session_id=excluded.session_id, scope=excluded.scope, mtype=excluded.mtype,
                title=excluded.title, content=excluded.content, summary=excluded.summary,
                keywords=excluded.keywords, metadata=excluded.metadata,
                importance=excluded.importance, surprise=excluded.surprise,
                stability=excluded.stability, access_count=excluded.access_count,
                last_access=excluded.last_access, valid_from=excluded.valid_from,
                valid_to=excluded.valid_to,
                valid_to_recorded_at=excluded.valid_to_recorded_at,
                ingested_at=excluded.ingested_at,
                expired_at=excluded.expired_at, subject_key=excluded.subject_key,
                claim_kind=excluded.claim_kind, pinned=excluded.pinned,
                sensitivity=excluded.sensitivity, provenance=excluded.provenance""",
            (rec.id, rec.workspace_id, rec.repo_id, rec.session_id,
             _enum(rec.scope), _enum(rec.mtype), rec.title, rec.content, rec.summary,
             _dumps(rec.keywords), _dumps(rec.metadata), rec.importance, rec.surprise,
             rec.stability, rec.access_count, rec.last_access, rec.valid_from, rec.valid_to,
             rec.valid_to_recorded_at, rec.ingested_at, rec.expired_at,
             rec.subject_key, rec.claim_kind,
             int(rec.pinned), rec.sensitivity,
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

    def count_memories(self, flt: Optional[SearchFilter] = None,
                       *, include_invalid: bool = False) -> int:
        """Count records visible to a search filter without materializing them."""
        sql = "SELECT COUNT(*) AS count FROM memories"
        where, params = self._where(flt, include_invalid)
        if where:
            sql += " WHERE " + " AND ".join(where)
        row = self.conn.execute(sql, params).fetchone()
        return int(row["count"] if row is not None else 0)

    def list_live_claims(self, *, workspace_id: str, repo_id: Optional[str],
                         session_id: Optional[str], scope: Scope, mtype: MemoryType,
                         subject_key: str, claim_kind: str) -> list[MemoryRecord]:
        """Return the current instances of one exact claim identity.

        Conflict resolution normally looks at a candidate's valid-time neighbourhood.  A
        backdated candidate still needs to see a later, live instance of its *own* durable
        claim key so it cannot create an overlapping history merely because an unrelated
        anchored hit filled the vector candidate budget.
        """
        subject_key = str(subject_key or "").strip()
        if not subject_key:
            return []
        sql = (
            "SELECT * FROM memories WHERE workspace_id=? AND repo_id IS ? "
            "AND scope=? AND mtype=? AND subject_key=? AND claim_kind=? "
            "AND valid_to IS NULL AND expired_at IS NULL"
        )
        params: list[Any] = [
            workspace_id, repo_id, _enum(scope), _enum(mtype), subject_key,
            str(claim_kind or "").strip(),
        ]
        if scope == Scope.SESSION:
            sql += " AND session_id=?"
            params.append(session_id)
        sql += " ORDER BY ingested_at DESC, id"
        rows = self.conn.execute(sql, params).fetchall()
        return [_row_to_record(row) for row in rows]

    def list_claim_history(self, *, workspace_id: str, repo_id: Optional[str],
                           session_id: Optional[str], scope: Scope, mtype: MemoryType,
                           subject_key: str, claim_kind: str) -> list[MemoryRecord]:
        """Return every recorded interval for one exact durable claim identity.

        Resolution uses this only to bound a newly inserted, backfilled keyed claim at
        the next known successor. Closed rows are deliberately included: they are the
        authoritative temporal chain and must not disappear merely because they are no
        longer visible to present-day recall.
        """
        subject_key = str(subject_key or "").strip()
        if not subject_key:
            return []
        sql = (
            "SELECT * FROM memories WHERE workspace_id=? AND repo_id IS ? "
            "AND scope=? AND mtype=? AND subject_key=? AND claim_kind=?"
        )
        params: list[Any] = [
            workspace_id, repo_id, _enum(scope), _enum(mtype), subject_key,
            str(claim_kind or "").strip(),
        ]
        if scope == Scope.SESSION:
            sql += " AND session_id=?"
            params.append(session_id)
        sql += " ORDER BY valid_from, ingested_at, id"
        rows = self.conn.execute(sql, params).fetchall()
        return [_row_to_record(row) for row in rows]

    def list_memories_page(self, flt: Optional[SearchFilter] = None, *,
                           after_id: str = "", limit: int = 500,
                           include_invalid: bool = False) -> list[MemoryRecord]:
        """Return one deterministic keyset page without materializing the full scope."""
        sql = "SELECT * FROM memories"
        where, params = self._where(flt, include_invalid=include_invalid)
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
        """Bi-temporal invalidation (§8.3): shorten a fact's validity without deleting."""
        recorded_at = now_ts()
        at = at if at is not None else recorded_at
        updated = self.conn.execute(
            "UPDATE memories SET valid_to=?, valid_to_recorded_at=? "
            "WHERE id=? AND (valid_to IS NULL OR valid_to>?)",
            (at, recorded_at, memory_id, at),
        ).rowcount
        if updated:
            self.invalidate_edges_for_memory(memory_id, at=at, commit=False)
        # Governance attempts are audit-worthy even when the interval was already
        # closed.  MCP callers deliberately expose forget as non-idempotent so a
        # repeated request keeps its own audit evidence while avoiding a second edge
        # invalidation or widening a closed interval.
        self.audit(actor, "invalidate", memory_id, reason, commit=False)
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

    def embedding_version(self, identity: str) -> Optional[str]:
        row = self.conn.execute(
            "SELECT version FROM embedding_state WHERE identity=?", (identity,)
        ).fetchone()
        return str(row["version"]) if row is not None else None

    def set_embedding_version(self, identity: str, version: str) -> None:
        self.conn.execute(
            "INSERT INTO embedding_state(identity, version, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(identity) DO UPDATE SET "
            "version=excluded.version, updated_at=excluded.updated_at",
            (identity, version, now_ts()),
        )
        self.conn.commit()

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
            nid = existing["id"]
        else:
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
        self._backfill_entity_text_mentions(
            nid, name=node.name, workspace_id=node.workspace_id, repo_id=node.repo_id,
        )
        if commit:
            self.conn.commit()
        return nid

    def _backfill_entity_text_mentions(self, entity_id: str, *, name: str,
                                       workspace_id: Optional[str],
                                       repo_id: Optional[str]) -> None:
        """Attach an entity added after its matching prose memories already existed.

        New writes are linked by ``MemoryEngine._link_memory_entities``.  This bounded,
        exact-word backfill preserves the same graph reachability for imported or legacy
        memories when their entity is introduced later, without a recall-time prose scan.
        """
        name = (name or "").strip()
        if len(name) < 2:
            return
        scope_sql = "repo_id IS NULL"
        scope_params: list[Any] = []
        if repo_id is not None:
            # A contextual repo read includes its workspace/user ancestors.  Do not
            # lose a legacy workspace mention merely because the matching entity was
            # introduced later in a repository; sibling repositories stay isolated.
            scope_sql = "(repo_id=? OR repo_id IS NULL)"
            scope_params.append(repo_id)
        rows = self.conn.execute(
            "SELECT id, title, content, workspace_id, repo_id, valid_from, valid_to, "
            "valid_to_recorded_at, ingested_at, expired_at FROM memories "
            "WHERE workspace_id IS ? AND scope<>'session' AND " + scope_sql + " "
            "AND (lower(title) LIKE ? ESCAPE '\\' OR lower(content) LIKE ? ESCAPE '\\') "
            "ORDER BY id LIMIT 12000",
            (workspace_id, *scope_params,
             "%" + _escape_like(name.casefold()) + "%",
             "%" + _escape_like(name.casefold()) + "%"),
        ).fetchall()
        pattern = re.compile(r"(?<!\w)" + re.escape(name) + r"(?!\w)", re.IGNORECASE)
        for row in rows:
            if not pattern.search(f"{row['title'] or ''}\n{row['content'] or ''}"):
                continue
            self.link_memory_entity(
                memory_id=row["id"], entity_id=entity_id,
                workspace_id=row["workspace_id"], repo_id=row["repo_id"],
                source_kind="text_mention", confidence=0.8,
                valid_from=row["valid_from"], valid_to=row["valid_to"],
                valid_to_recorded_at=row["valid_to_recorded_at"],
                ingested_at=row["ingested_at"], expired_at=row["expired_at"],
                provenance={"source": "exact_text_backfill"}, commit=False,
            )

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
            if flt.include_ancestors:
                where.append("(repo_id=? OR repo_id IS NULL)")
            else:
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

    def link_memory_entity(self, *, memory_id: str, entity_id: str,
                           workspace_id: Optional[str], repo_id: Optional[str],
                           source_kind: str = "explicit", confidence: float = 1.0,
                           valid_from: Optional[float] = None,
                           valid_to: Optional[float] = None,
                           valid_to_recorded_at: Optional[float] = None,
                           ingested_at: Optional[float] = None,
                           expired_at: Optional[float] = None,
                           provenance: Optional[dict] = None,
                           commit: bool = True) -> str:
        """Create one idempotent, bi-temporal memory↔entity incidence record."""
        stamp = now_ts()
        if valid_to is None and expired_at is None:
            existing = self.conn.execute(
                "SELECT id, confidence, valid_from, ingested_at "
                "FROM memory_entities WHERE memory_id=? AND entity_id=? "
                "AND source_kind=? AND valid_to IS NULL AND expired_at IS NULL",
                (memory_id, entity_id, source_kind),
            ).fetchone()
            requested_valid = (
                valid_from if valid_from is not None
                else (existing["valid_from"] if existing is not None else stamp)
            )
            requested_known = (
                ingested_at if ingested_at is not None
                else (existing["ingested_at"] if existing is not None else stamp)
            )
        else:
            requested_valid = valid_from if valid_from is not None else stamp
            requested_known = ingested_at if ingested_at is not None else stamp
            existing = self.conn.execute(
                "SELECT id FROM memory_entities WHERE memory_id=? AND entity_id=? "
                "AND source_kind=? AND valid_from IS ? AND valid_to IS ? "
                "AND valid_to_recorded_at IS ? "
                "AND ingested_at IS ? AND expired_at IS ?",
                (
                    memory_id, entity_id, source_kind, requested_valid, valid_to,
                    valid_to_recorded_at, requested_known, expired_at,
                ),
            ).fetchone()
        if existing is not None:
            if valid_to is None and expired_at is None:
                desired_confidence = max(
                    float(existing["confidence"] or 0.0),
                    max(0.0, min(1.0, float(confidence))),
                )
                if (requested_valid == existing["valid_from"]
                        and requested_known == existing["ingested_at"]):
                    if desired_confidence != float(existing["confidence"] or 0.0):
                        self.conn.execute(
                            "UPDATE memory_entities SET confidence=? WHERE id=?",
                            (desired_confidence, existing["id"]),
                        )
                        if commit:
                            self.conn.commit()
                    return existing["id"]

                # A later observation can describe the same incidence with a different
                # valid/known pair.  Version it instead of independently minimising the
                # coordinates, which would fabricate a historical interval no source ever
                # asserted (for example valid_from=50 paired with ingested_at=100).
                retire_at = max(
                    (value for value in (existing["ingested_at"], requested_known)
                     if value is not None),
                    default=stamp,
                )
                self.conn.execute(
                    "UPDATE memory_entities SET expired_at=? WHERE id=?",
                    (retire_at, existing["id"]),
                )
            else:
                return existing["id"]
        link_id = ids.new_id("edge")
        self.conn.execute(
            "INSERT INTO memory_entities("
            "id, memory_id, entity_id, workspace_id, repo_id, source_kind, confidence, "
            "valid_from, valid_to, valid_to_recorded_at, ingested_at, expired_at, "
            "provenance) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (link_id, memory_id, entity_id, workspace_id, repo_id, source_kind,
             max(0.0, min(1.0, float(confidence))),
             requested_valid, valid_to, valid_to_recorded_at, requested_known, expired_at,
             _dumps(provenance or {})),
        )
        if commit:
            self.conn.commit()
        return link_id

    def list_memory_entities(self, flt: Optional[SearchFilter] = None, *,
                             entity_ids: Optional[list[str]] = None,
                             memory_ids: Optional[list[str]] = None,
                             limit: Optional[int] = None) -> list[dict]:
        """Return bounded scoped/temporal incidence rows for graph retrieval."""
        # Consolidation scans up to 2,000 memories, while portable SQLite builds may
        # allow only 999 bind variables. Partition ID filters before building the SQL
        # predicate; each pair of chunks is disjoint, so merging preserves results.
        entity_chunks = (
            [entity_ids[start:start + IN_CLAUSE_CHUNK]
             for start in range(0, len(entity_ids), IN_CLAUSE_CHUNK)]
            if entity_ids is not None else [None]
        )
        memory_chunks = (
            [memory_ids[start:start + IN_CLAUSE_CHUNK]
             for start in range(0, len(memory_ids), IN_CLAUSE_CHUNK)]
            if memory_ids is not None else [None]
        )
        if not entity_chunks or not memory_chunks:
            return []
        if len(entity_chunks) > 1 or len(memory_chunks) > 1:
            rows = [
                row
                for entity_chunk in entity_chunks
                for memory_chunk in memory_chunks
                for row in self.list_memory_entities(
                    flt, entity_ids=entity_chunk, memory_ids=memory_chunk,
                )
            ]
            rows.sort(key=lambda row: (-float(row.get("confidence") or 0.0), row["id"]))
            return rows if limit is None else rows[:max(0, int(limit))]
        valid_at, known_at = _temporal_anchors(flt)
        sql = (
            "SELECT me.* FROM memory_entities me "
            "JOIN memories m ON m.id=me.memory_id WHERE "
            "(me.valid_from IS NULL OR me.valid_from<=?) "
            "AND (me.valid_to IS NULL OR ?<me.valid_to "
            "OR (me.valid_to_recorded_at IS NOT NULL "
            "AND ?<me.valid_to_recorded_at)) "
            "AND (me.ingested_at IS NULL OR me.ingested_at<=?) "
            "AND (me.expired_at IS NULL OR ?<me.expired_at)"
        )
        params: list[Any] = [
            valid_at, valid_at, known_at, known_at, known_at,
        ]
        if flt and flt.workspace_id:
            sql += " AND me.workspace_id=?"
            params.append(flt.workspace_id)
        if flt and flt.repo_id:
            if flt.include_ancestors:
                sql += " AND (me.repo_id=? OR me.repo_id IS NULL)"
            else:
                sql += " AND me.repo_id=?"
            params.append(flt.repo_id)
        memory_where, memory_params = self._where(
            flt, include_invalid=False, alias="m"
        )
        if memory_where:
            sql += " AND " + " AND ".join(memory_where)
            params.extend(memory_params)
        if entity_ids is not None:
            if not entity_ids:
                return []
            marks = ",".join("?" for _ in entity_ids)
            sql += f" AND me.entity_id IN ({marks})"
            params.extend(entity_ids)
        if memory_ids is not None:
            if not memory_ids:
                return []
            marks = ",".join("?" for _ in memory_ids)
            sql += f" AND me.memory_id IN ({marks})"
            params.extend(memory_ids)
        sql += " ORDER BY me.confidence DESC, me.id"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(max(0, int(limit)))
        return [dict(row) for row in self.conn.execute(sql, params).fetchall()]

    def upsert_edge(self, edge: Edge, *, commit: bool = True) -> str:
        eid = edge.id or ids.new_id("edge")
        layer = normalize_graph_layer(edge.layer, edge.relation).value
        source, target = edge.src, edge.dst
        if edge.relation in {"co_occurs", "related", "associated_with"} and target < source:
            source, target = target, source
        incoming_provenance = _merge_edge_provenance([edge.provenance])
        existing = self.conn.execute(
            "SELECT id, workspace_id, repo_id, src, dst, relation, layer, weight, "
            "valid_from, valid_to, valid_to_recorded_at, ingested_at, expired_at, provenance "
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
            desired_ingested_at = existing["ingested_at"]
            if edge.ingested_at is not None:
                desired_ingested_at = min(
                    value for value in (existing["ingested_at"], edge.ingested_at)
                    if value is not None
                )
            serialized_provenance = _dumps(merged_provenance)
            if desired_weight != float(existing["weight"] or 0.0) \
                    or desired_valid_from != existing["valid_from"] \
                    or desired_ingested_at != existing["ingested_at"] \
                    or serialized_provenance != (existing["provenance"] or "{}"):
                self.conn.execute(
                    "UPDATE edges SET weight=?, valid_from=?, ingested_at=?, "
                    "provenance=? WHERE id=?",
                    (
                        desired_weight, desired_valid_from, desired_ingested_at,
                        serialized_provenance, eid,
                    ),
                )
            self._write_edge_supports(
                eid, edge.relation, incoming_provenance,
                valid_from=edge.valid_from, valid_to=edge.valid_to,
                valid_to_recorded_at=edge.valid_to_recorded_at,
                ingested_at=edge.ingested_at, expired_at=edge.expired_at,
            )
            if commit:
                self.conn.commit()
            return eid
        equivalent = None
        if edge.valid_to is None and edge.expired_at is None:
            equivalent = self.conn.execute(
                "SELECT id, weight, valid_from, ingested_at, provenance FROM edges "
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
                    "UPDATE edges SET valid_to=?, valid_to_recorded_at=? "
                    "WHERE id=? AND valid_to IS NULL",
                    (closed_at, closed_at, eid),
                )
                self.conn.execute(
                    "UPDATE edge_supports SET valid_to=?, valid_to_recorded_at=? "
                    "WHERE edge_id=? "
                    "AND valid_to IS NULL AND expired_at IS NULL",
                    (closed_at, closed_at, eid),
                )
            existing_provenance = _loads(equivalent["provenance"], {})
            merged_provenance = _merge_edge_provenance(
                [existing_provenance, incoming_provenance],
                merged_ids=[eid] if replacing else [],
            )
            valid_values = [value for value in (
                equivalent["valid_from"], edge.valid_from
            ) if value is not None]
            known_values = [
                value for value in (
                    equivalent["ingested_at"], edge.ingested_at
                ) if value is not None
            ]
            self.conn.execute(
                "UPDATE edges SET weight=?, valid_from=?, ingested_at=?, provenance=? "
                "WHERE id=?",
                (
                    max(float(equivalent["weight"] or 0.0), float(edge.weight or 0.0)),
                    min(valid_values) if valid_values else now_ts(),
                    min(known_values) if known_values else now_ts(),
                    _dumps(merged_provenance), equivalent["id"],
                ),
            )
            self._write_edge_supports(
                equivalent["id"], edge.relation, incoming_provenance,
                valid_from=edge.valid_from, valid_to=edge.valid_to,
                valid_to_recorded_at=edge.valid_to_recorded_at,
                ingested_at=edge.ingested_at, expired_at=edge.expired_at,
            )
            if commit:
                self.conn.commit()
            return str(equivalent["id"])
        if replacing:
            # ``upsert_edge`` replaces the supplied edge record. Close its previous
            # normalized evidence before writing the replacement so sources removed
            # from the new provenance cannot remain live invisibly.
            closed_at = now_ts()
            self.conn.execute(
                "UPDATE edge_supports SET valid_to=?, valid_to_recorded_at=? "
                "WHERE edge_id=? "
                "AND valid_to IS NULL AND expired_at IS NULL",
                (closed_at, closed_at, eid),
            )
        self.conn.execute(
            "INSERT INTO edges(id, workspace_id, repo_id, src, dst, relation, layer, "
            "weight, valid_from, valid_to, valid_to_recorded_at, ingested_at, "
            "expired_at, provenance) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET workspace_id=excluded.workspace_id, "
            "repo_id=excluded.repo_id, src=excluded.src, dst=excluded.dst, "
            "relation=excluded.relation, layer=excluded.layer, weight=excluded.weight, "
            "valid_from=excluded.valid_from, valid_to=excluded.valid_to, "
            "valid_to_recorded_at=excluded.valid_to_recorded_at, "
            "ingested_at=excluded.ingested_at, expired_at=excluded.expired_at, "
            "provenance=excluded.provenance",
            (eid, edge.workspace_id, edge.repo_id, source, target, edge.relation, layer,
             edge.weight, edge.valid_from if edge.valid_from is not None else now_ts(),
             edge.valid_to, edge.valid_to_recorded_at,
             edge.ingested_at if edge.ingested_at is not None else now_ts(),
             edge.expired_at,
             _dumps(incoming_provenance)),
        )
        self._write_edge_supports(
            eid, edge.relation, incoming_provenance,
            valid_from=edge.valid_from, valid_to=edge.valid_to,
            valid_to_recorded_at=edge.valid_to_recorded_at,
            ingested_at=edge.ingested_at, expired_at=edge.expired_at,
        )
        if commit:
            self.conn.commit()
        return eid

    def invalidate_edge(self, edge_id: str, at: Optional[float] = None) -> None:
        recorded_at = now_ts()
        ts = recorded_at if at is None else at
        self.conn.execute(
            "UPDATE edges SET valid_to=?, valid_to_recorded_at=? "
            "WHERE id=? AND valid_to IS NULL",
            (ts, recorded_at, edge_id),
        )
        self.conn.execute(
            "UPDATE edge_supports SET valid_to=?, valid_to_recorded_at=? "
            "WHERE edge_id=? AND valid_to IS NULL AND expired_at IS NULL",
            (ts, recorded_at, edge_id),
        )
        self.conn.commit()

    def _write_edge_supports(self, edge_id: str, relation: str, provenance: dict,
                             *, valid_from: Optional[float] = None,
                             valid_to: Optional[float] = None,
                             valid_to_recorded_at: Optional[float] = None,
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
                "valid_to_recorded_at, ingested_at, expired_at, provenance) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (edge_id, memory_id, source_kind, confidence,
                 support_valid_from, valid_to, valid_to_recorded_at,
                 support_ingested_at, expired_at,
                 _dumps(support_provenance)),
            )

    def add_edge_support(self, edge_id: str, provenance: dict, *,
                         valid_from: Optional[float] = None,
                         ingested_at: Optional[float] = None,
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
            "SELECT relation, valid_from, valid_to, valid_to_recorded_at, "
            "ingested_at, expired_at "
            "FROM edges WHERE id=?", (edge_id,)
        ).fetchone()
        if edge_row:
            support_valid_from = (
                valid_from if valid_from is not None else edge_row["valid_from"]
            )
            support_ingested_at = (
                ingested_at if ingested_at is not None else edge_row["ingested_at"]
            )
            self._write_edge_supports(
                edge_id, edge_row["relation"] or "", provenance,
                valid_from=support_valid_from, valid_to=edge_row["valid_to"],
                valid_to_recorded_at=edge_row["valid_to_recorded_at"],
                ingested_at=support_ingested_at, expired_at=edge_row["expired_at"],
            )
            # The edge is the union of its supporting evidence intervals. A
            # backdated support must make the relation visible at that earlier
            # world time, and a historically imported support may likewise be
            # known before the edge's previous system-time anchor.
            valid_values = [
                value for value in (edge_row["valid_from"], support_valid_from)
                if value is not None
            ]
            ingested_values = [
                value for value in (edge_row["ingested_at"], support_ingested_at)
                if value is not None
            ]
            earlier_valid = min(valid_values) if valid_values else None
            earlier_ingested = min(ingested_values) if ingested_values else None
            if (earlier_valid != edge_row["valid_from"]
                    or earlier_ingested != edge_row["ingested_at"]):
                self.conn.execute(
                    "UPDATE edges SET valid_from=?, ingested_at=? WHERE id=?",
                    (earlier_valid, earlier_ingested, edge_id),
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
        recorded_at = now_ts()
        ts = at if at is not None else recorded_at
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
        # Compatibility fallback for a direct legacy SQL writer. Canonical write
        # paths populate edge_supports, but a workspace can hold both normalized and
        # older direct-provenance edges. Query both sources: using the fallback only
        # when the indexed arm is empty leaves those old edges live after a downgrade.
        sql = ("SELECT id, provenance FROM edges "
               "WHERE valid_to IS NULL AND provenance LIKE ? ESCAPE '\\'")
        params: list[Any] = [f"%{_escape_like(memory_id)}%"]
        if workspace_id is not None:
            sql += " AND (workspace_id=? OR workspace_id IS NULL)"
            params.append(workspace_id)
        seen = {row["id"] for row in rows}
        rows.extend(
            row for row in self.conn.fetchall(sql, params) if row["id"] not in seen
        )
        ids_to_close: list[str] = []
        for row in rows:
            prov = _loads(row["provenance"], {})
            supports = _provenance_memory_ids(prov)
            if memory_id not in supports:
                continue
            self.conn.execute(
                "UPDATE edge_supports SET valid_to=?, valid_to_recorded_at=? "
                "WHERE edge_id=? AND memory_id=? "
                "AND valid_to IS NULL AND expired_at IS NULL",
                (ts, recorded_at, row["id"], memory_id),
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
            self.conn.execute(
                f"UPDATE edges SET valid_to=?, valid_to_recorded_at=? "
                f"WHERE id IN ({marks})",
                (ts, recorded_at, *ids_to_close),
            )
            self.conn.execute(
                f"UPDATE edge_supports SET valid_to=?, valid_to_recorded_at=? "
                f"WHERE edge_id IN ({marks}) "
                "AND valid_to IS NULL AND expired_at IS NULL",
                (ts, recorded_at, *ids_to_close),
            )
        if commit:
            self.conn.commit()

    def retire_memory_graph_state(self, memory_id: str, *, at: Optional[float] = None,
                                  commit: bool = True) -> None:
        """Close live graph derivatives of one memory without deleting their history.

        A trust downgrade can leave the memory itself valid for inspection while making
        its previously trusted graph evidence unsafe to traverse. Retire every current
        support, incidence, and memory/code link at one scan-time boundary so historical
        reads remain explainable but current graph recall cannot route through it.
        """
        recorded_at = now_ts()
        ts = at if at is not None else recorded_at
        self.invalidate_edges_for_memory(memory_id, at=ts, commit=False)
        self.conn.execute(
            "UPDATE memory_entities SET valid_to=?, valid_to_recorded_at=? "
            "WHERE memory_id=? AND valid_to IS NULL AND expired_at IS NULL",
            (ts, recorded_at, memory_id),
        )
        self.conn.execute(
            "UPDATE mem_links SET valid_to=?, valid_to_recorded_at=? "
            "WHERE (a=? OR b=?) AND valid_to IS NULL AND expired_at IS NULL",
            (ts, recorded_at, memory_id, memory_id),
        )
        self.conn.execute(
            "UPDATE code_memory_links SET valid_to=?, valid_to_recorded_at=? "
            "WHERE memory_id=? AND valid_to IS NULL AND expired_at IS NULL",
            (ts, recorded_at, memory_id),
        )
        if commit:
            self.conn.commit()

    # ── memory-to-memory links (A-MEM style) ────────────────────────────────────
    def edge_supports_in_scope(self, edge_ids: Optional[list[str]] = None, *,
                               at: Optional[float] = None,
                               flt: Optional[SearchFilter] = None,
                               limit: Optional[int] = None) -> list[dict]:
        """Return evidence visible at the supplied world/system-time anchors."""
        valid_at, known_at = _temporal_anchors(flt, valid_at=at)
        row_cap = None if limit is None else max(0, int(limit))
        if row_cap == 0:
            return []
        sql = (
            "SELECT s.id, s.edge_id, s.memory_id, s.source_kind, s.confidence, "
            "s.valid_from, s.valid_to, s.valid_to_recorded_at, "
            "s.ingested_at, s.expired_at, s.provenance "
            "FROM edge_supports s JOIN edges e ON e.id=s.edge_id "
            "WHERE (s.valid_from IS NULL OR s.valid_from<=?) "
            "AND (s.valid_to IS NULL OR ?<s.valid_to "
            "OR (s.valid_to_recorded_at IS NOT NULL "
            "AND ?<s.valid_to_recorded_at)) "
            "AND (s.ingested_at IS NULL OR s.ingested_at<=?) "
            "AND (s.expired_at IS NULL OR ?<s.expired_at) "
            "AND (e.valid_from IS NULL OR e.valid_from<=?) "
            "AND (e.valid_to IS NULL OR ?<e.valid_to "
            "OR (e.valid_to_recorded_at IS NOT NULL "
            "AND ?<e.valid_to_recorded_at)) "
            "AND (e.ingested_at IS NULL OR e.ingested_at<=?) "
            "AND (e.expired_at IS NULL OR ?<e.expired_at)"
        )
        params: list[Any] = [
            valid_at, valid_at, known_at, known_at, known_at,
            valid_at, valid_at, known_at, known_at, known_at,
        ]
        if flt and flt.workspace_id:
            sql += " AND e.workspace_id=?"
            params.append(flt.workspace_id)
        if flt and flt.repo_id:
            if flt.include_ancestors:
                sql += " AND (e.repo_id=? OR e.repo_id IS NULL)"
            else:
                sql += " AND e.repo_id=?"
            params.append(flt.repo_id)
        if edge_ids is not None:
            if not edge_ids:
                return []
            rows: list[dict] = []
            for start in range(0, len(edge_ids), IN_CLAUSE_CHUNK):
                if row_cap is not None and len(rows) >= row_cap:
                    break
                chunk = edge_ids[start:start + IN_CLAUSE_CHUNK]
                marks = ",".join("?" for _ in chunk)
                statement = (
                    sql + f" AND s.edge_id IN ({marks}) "
                    "ORDER BY s.edge_id, s.memory_id, s.id"
                )
                statement_params: tuple[Any, ...] = (*params, *chunk)
                if row_cap is not None:
                    statement += " LIMIT ?"
                    statement_params = (*statement_params, row_cap - len(rows))
                found = self.conn.execute(
                    statement, statement_params,
                ).fetchall()
                rows.extend(dict(row) for row in found)
            return rows
        statement = sql + " ORDER BY s.edge_id, s.memory_id, s.id"
        statement_params: tuple[Any, ...] = tuple(params)
        if row_cap is not None:
            statement += " LIMIT ?"
            statement_params = (*statement_params, row_cap)
        return [dict(row) for row in self.conn.execute(
            statement, statement_params
        ).fetchall()]

    def add_link(self, a: str, b: str, relation: str = "related",
                 layer: Optional[GraphLayer] = None, reason: str = "",
                 *, valid_from: Optional[float] = None,
                 valid_to: Optional[float] = None,
                 valid_to_recorded_at: Optional[float] = None,
                 ingested_at: Optional[float] = None,
                 expired_at: Optional[float] = None,
                 commit: bool = True) -> None:
        """Idempotent per (pair, relation): re-linking the same two memories with the
        same relation is a no-op in either direction, so auto-evolution and explicit
        ``engraphis_link`` calls can't accrete duplicate rows."""
        requested_layer = (
            normalize_graph_layer(layer, relation).value
            if layer is not None else None
        )
        graph_layer = requested_layer or normalize_graph_layer(None, relation).value
        started_transaction = not self.conn.in_transaction
        if started_transaction:
            self.conn.execute("BEGIN IMMEDIATE")
        try:
            # A sync bundle may carry a closed link interval.  It has no live row to
            # match below, so recognize an exact historical version before inserting
            # it again on every replay. ``IS`` deliberately gives NULL-safe equality.
            exact = self.conn.execute(
                "SELECT 1 FROM mem_links "
                "WHERE ((a=? AND b=?) OR (a=? AND b=?)) AND relation=? "
                "AND layer=? AND reason=? AND valid_from IS ? AND valid_to IS ? "
                "AND valid_to_recorded_at IS ? AND ingested_at IS ? AND expired_at IS ? "
                "LIMIT 1",
                (
                    a, b, b, a, relation, graph_layer, reason,
                    valid_from, valid_to, valid_to_recorded_at, ingested_at, expired_at,
                ),
            ).fetchone()
            if exact is not None:
                if started_transaction:
                    self.conn.commit()
                return
            existing = self.conn.execute(
                "SELECT rowid, a, b, relation, layer, reason, created_at, "
                "valid_from, valid_to, valid_to_recorded_at, ingested_at, expired_at "
                "FROM mem_links "
                "WHERE ((a=? AND b=?) OR (a=? AND b=?)) AND relation=? "
                "AND valid_to IS NULL AND expired_at IS NULL "
                "ORDER BY rowid DESC LIMIT 1",
                (a, b, b, a, relation),
            ).fetchone()
            if existing:
                graph_layer = (
                    requested_layer
                    if requested_layer is not None else existing["layer"]
                )
                replacement_reason = reason if reason else existing["reason"]
                if (
                    graph_layer != existing["layer"]
                    or replacement_reason != existing["reason"]
                ):
                    # Metadata is part of what the system knew about this link. Updating
                    # it in place would rewrite a historical ``known_at`` view. Retire the
                    # system-time version and open a replacement over the same world-time
                    # interval so past reads remain immutable while current reads converge.
                    stamp = max(
                        now_ts(),
                        (
                            float(existing["ingested_at"])
                            if existing["ingested_at"] is not None
                            else float("-inf")
                        ),
                    )
                    self.conn.execute(
                        "UPDATE mem_links SET expired_at=? "
                        "WHERE rowid=? AND expired_at IS NULL",
                        (stamp, existing["rowid"]),
                    )
                    self.conn.execute(
                        "INSERT INTO mem_links("
                        "a, b, relation, layer, reason, created_at, valid_from, valid_to, "
                        "valid_to_recorded_at, ingested_at, expired_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,NULL)",
                        (
                            existing["a"], existing["b"], existing["relation"],
                            graph_layer, replacement_reason, stamp,
                            existing["valid_from"], existing["valid_to"],
                            existing["valid_to_recorded_at"], stamp,
                        ),
                    )
                    if commit:
                        self.conn.commit()
                elif started_transaction:
                    # The pre-read reservation has no write to batch. Release it even
                    # for ``commit=False``; the old no-op path never opened a transaction.
                    self.conn.commit()
                return
            stamp = now_ts()
            world_start = stamp if valid_from is None else valid_from
            system_start = stamp if ingested_at is None else ingested_at
            self.conn.execute(
                "INSERT INTO mem_links("
                "a, b, relation, layer, reason, created_at, valid_from, valid_to, "
                "valid_to_recorded_at, ingested_at, expired_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (a, b, relation, graph_layer, reason, stamp, world_start, valid_to,
                 valid_to_recorded_at, system_start, expired_at),
            )
            if commit:
                self.conn.commit()
        except BaseException:
            if started_transaction and self.conn.in_transaction:
                self.conn.rollback()
            raise

    def add_link_version(self, a: str, b: str, relation: str = "related",
                         layer: Optional[GraphLayer] = None, reason: str = "", *,
                         valid_from: Optional[float] = None,
                         valid_to: Optional[float] = None,
                         valid_to_recorded_at: Optional[float] = None,
                         ingested_at: Optional[float] = None,
                         expired_at: Optional[float] = None,
                         commit: bool = True) -> bool:
        """Persist one exact temporal link version without collapsing live evidence.

        Normal :meth:`add_link` intentionally de-duplicates active relationships for
        interactive callers. Sync is different: two peers can independently observe the
        same relation with distinct valid/known intervals, and both intervals are needed
        for a convergent historical graph. This method appends that exact observation and
        returns whether it was new, while replaying the same version remains a no-op.
        """
        graph_layer = normalize_graph_layer(layer, relation).value
        stamp = now_ts()
        world_start = stamp if valid_from is None else valid_from
        system_start = stamp if ingested_at is None else ingested_at
        started_transaction = not self.conn.in_transaction
        if started_transaction:
            self.conn.execute("BEGIN IMMEDIATE")
        try:
            exact = self.conn.execute(
                "SELECT 1 FROM mem_links "
                "WHERE ((a=? AND b=?) OR (a=? AND b=?)) AND relation=? "
                "AND layer=? AND reason=? AND valid_from IS ? AND valid_to IS ? "
                "AND valid_to_recorded_at IS ? AND ingested_at IS ? AND expired_at IS ? "
                "LIMIT 1",
                (
                    a, b, b, a, relation, graph_layer, reason,
                    world_start, valid_to, valid_to_recorded_at, system_start, expired_at,
                ),
            ).fetchone()
            if exact is not None:
                if started_transaction:
                    self.conn.commit()
                return False
            self.conn.execute(
                "INSERT INTO mem_links("
                "a, b, relation, layer, reason, created_at, valid_from, valid_to, "
                "valid_to_recorded_at, ingested_at, expired_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (a, b, relation, graph_layer, reason, stamp, world_start, valid_to,
                 valid_to_recorded_at, system_start, expired_at),
            )
            if commit:
                self.conn.commit()
            return True
        except BaseException:
            if started_transaction and self.conn.in_transaction:
                self.conn.rollback()
            raise

    def has_link(self, a: str, b: str, *, relation: Optional[str] = None) -> bool:
        """Return whether the pair has a current open link interval.

        Closed history must not block a later reactivation of the same relationship.
        Historical visibility remains available through ``get_links``/``links_among``.
        """
        sql = (
            "SELECT 1 FROM mem_links WHERE ((a=? AND b=?) OR (a=? AND b=?)) "
            "AND valid_to IS NULL AND expired_at IS NULL"
        )
        params: list[Any] = [a, b, b, a]
        if relation is not None:
            sql += " AND relation=?"
            params.append(relation)
        return self.conn.execute(sql + " LIMIT 1", params).fetchone() is not None

    def get_links(self, memory_id: str, *,
                  flt: Optional[SearchFilter] = None) -> list[dict]:
        """Return direct links visible at the filter's bi-temporal anchors."""
        visible_sql, params = _temporal_visibility_sql("", flt)
        rows = self.conn.execute(
            "SELECT a, b, relation, layer, reason, created_at, valid_from, valid_to, "
            "valid_to_recorded_at, ingested_at, expired_at FROM mem_links "
            f"WHERE (a=? OR b=?) AND {visible_sql} ORDER BY a, b, relation",
            (memory_id, memory_id, *params),
        ).fetchall()
        return [dict(r) for r in rows]

    def edges_in_scope(self, flt: Optional[SearchFilter] = None,
                       *, at: Optional[float] = None,
                       limit: Optional[int] = None) -> list[Edge]:
        """Edges visible at ``at``/``filter.valid_at`` and ``filter.known_at``.

        Normalized supports are authoritative for edges that have them.  The edge row
        aggregates its support starts for current-read efficiency, but independently
        minimizing world and system time can fabricate a pair no source established.
        A historical read must therefore see at least one individually visible support.
        Legacy direct edges with no normalized support retain the edge-row fallback.
        """
        valid_at, known_at = _temporal_anchors(flt, valid_at=at)
        sql = ("SELECT * FROM edges WHERE (valid_from IS NULL OR valid_from<=?) "
               "AND (valid_to IS NULL OR ?<valid_to "
               "OR (valid_to_recorded_at IS NOT NULL "
               "AND ?<valid_to_recorded_at)) "
               "AND (ingested_at IS NULL OR ingested_at<=?) "
               "AND (expired_at IS NULL OR ?<expired_at)")
        params: list[Any] = [
            valid_at, valid_at, known_at, known_at, known_at,
        ]
        support_visibility, support_params = _temporal_visibility_sql(
            "s", flt, valid_at=valid_at
        )
        sql += (
            " AND (NOT EXISTS (SELECT 1 FROM edge_supports any_support "
            "WHERE any_support.edge_id=edges.id) OR EXISTS (SELECT 1 FROM "
            "edge_supports s WHERE s.edge_id=edges.id AND "
            + support_visibility + "))"
        )
        params.extend(support_params)
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
        sql += " ORDER BY id"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(max(0, int(limit)))
        rows = self.conn.execute(sql, params).fetchall()
        return [_row_to_edge(r) for r in rows]

    def links_among(self, ids: list[str], *,
                    layers: Optional[list[GraphLayer]] = None,
                    flt: Optional[SearchFilter] = None,
                    include_invalid: bool = False,
                    limit: Optional[int] = None) -> list[dict]:
        """Return memory links visible under both temporal anchors.

        ``include_invalid`` is for full-state replication only: a closed interval is
        state that must synchronize even though normal graph reads do not expose it.

        Chunk only the indexed ``a`` side and filter ``b`` against an in-memory set.
        This keeps every statement below SQLite's portable variable limit while
        preserving exact pair semantics for graphs containing thousands of memories.
        """
        if not ids:
            return []
        if layers is not None and not layers:
            return []
        row_cap = None if limit is None else max(0, int(limit))
        if row_cap == 0:
            return []
        wanted = set(ids)
        ordered_ids = sorted(wanted)
        visibility_sql, visibility_params = _temporal_visibility_sql("", flt)
        rows: list[dict] = []
        # Leave headroom for the time anchor and optional layer parameters.
        chunk_size = max(1, IN_CLAUSE_CHUNK - 16)
        for start in range(0, len(ordered_ids), chunk_size):
            if row_cap is not None and len(rows) >= row_cap:
                break
            chunk = ordered_ids[start:start + chunk_size]
            marks = ",".join("?" for _ in chunk)
            sql = (
                "SELECT a, b, relation, layer, reason, created_at, valid_from, valid_to, "
                "valid_to_recorded_at, ingested_at, expired_at FROM mem_links "
                f"WHERE a IN ({marks})"
            )
            params: list[Any] = [*chunk]
            if not include_invalid:
                sql += f" AND {visibility_sql}"
                params.extend(visibility_params)
            if layers is not None:
                layer_marks = ",".join("?" for _ in layers)
                sql += f" AND layer IN ({layer_marks})"
                params.extend(_enum(layer) for layer in layers)
            sql += " ORDER BY a, b, relation, valid_from, ingested_at"
            found = self.conn.execute(sql, params).fetchall()
            for row in found:
                if row["b"] not in wanted:
                    continue
                rows.append(dict(row))
                if row_cap is not None and len(rows) >= row_cap:
                    break
        return rows

    def links_touching(self, ids: list[str], *,
                       layers: Optional[list[GraphLayer]] = None,
                       flt: Optional[SearchFilter] = None,
                       include_invalid: bool = False,
                       limit: Optional[int] = None) -> list[dict]:
        """Return visible links with at least one endpoint in ``ids``.

        This bounded frontier expansion is distinct from :meth:`links_among`: graph
        recall uses it to retain an unmentioned endpoint linked to an entity-attached
        memory, without first materializing every memory in a large scope.
        """
        if not ids:
            return []
        if layers is not None and not layers:
            return []
        row_cap = None if limit is None else max(0, int(limit))
        if row_cap == 0:
            return []
        ordered_ids = sorted(set(ids))
        visibility_sql, visibility_params = _temporal_visibility_sql("", flt)
        rows: list[dict] = []
        seen: set[tuple] = set()
        # Each id appears once for each endpoint predicate; reserve parameters for
        # time/layer filters so this remains under SQLite's portable bind limit.
        chunk_size = max(1, (IN_CLAUSE_CHUNK - 16) // 2)
        for start in range(0, len(ordered_ids), chunk_size):
            if row_cap is not None and len(rows) >= row_cap:
                break
            chunk = ordered_ids[start:start + chunk_size]
            marks = ",".join("?" for _ in chunk)
            sql = (
                "SELECT a, b, relation, layer, reason, created_at, valid_from, valid_to, "
                "valid_to_recorded_at, ingested_at, expired_at FROM mem_links "
                f"WHERE (a IN ({marks}) OR b IN ({marks}))"
            )
            params: list[Any] = [*chunk, *chunk]
            if not include_invalid:
                sql += f" AND {visibility_sql}"
                params.extend(visibility_params)
            if layers is not None:
                layer_marks = ",".join("?" for _ in layers)
                sql += f" AND layer IN ({layer_marks})"
                params.extend(_enum(layer) for layer in layers)
            sql += " ORDER BY a, b, relation, valid_from, ingested_at"
            for row in self.conn.execute(sql, params).fetchall():
                item = dict(row)
                key = (
                    item["a"], item["b"], item["relation"], item["layer"],
                    item["valid_from"], item["valid_to"], item["ingested_at"],
                )
                if key in seen:
                    continue
                seen.add(key)
                rows.append(item)
                if row_cap is not None and len(rows) >= row_cap:
                    break
        return rows

    def neighbors(self, node_ids: list[str], *, at: Optional[float] = None,
                  layers: Optional[list[GraphLayer]] = None,
                  flt: Optional[SearchFilter] = None,
                  limit: Optional[int] = None) -> list[Edge]:
        if not node_ids:
            return []
        valid_at, known_at = _temporal_anchors(flt, valid_at=at)
        marks = ",".join("?" for _ in node_ids)
        sql = (
            f"SELECT * FROM edges WHERE (src IN ({marks}) OR dst IN ({marks})) "
            f"AND (valid_from IS NULL OR valid_from<=?) "
            f"AND (valid_to IS NULL OR ?<valid_to "
            f"OR (valid_to_recorded_at IS NOT NULL "
            f"AND ?<valid_to_recorded_at)) "
            f"AND (ingested_at IS NULL OR ingested_at<=?) "
            f"AND (expired_at IS NULL OR ?<expired_at)"
        )
        params: list[Any] = [
            *node_ids, *node_ids,
            valid_at, valid_at, known_at, known_at, known_at,
        ]
        support_visibility, support_params = _temporal_visibility_sql(
            "s", flt, valid_at=valid_at
        )
        sql += (
            " AND (NOT EXISTS (SELECT 1 FROM edge_supports any_support "
            "WHERE any_support.edge_id=edges.id) OR EXISTS (SELECT 1 FROM "
            "edge_supports s WHERE s.edge_id=edges.id AND "
            + support_visibility + "))"
        )
        params.extend(support_params)
        if layers is not None:
            if not layers:
                return []
            layer_marks = ",".join("?" for _ in layers)
            sql += f" AND layer IN ({layer_marks})"
            params.extend(_enum(layer) for layer in layers)
        if flt and flt.workspace_id:
            sql += " AND workspace_id=?"
            params.append(flt.workspace_id)
        if flt and flt.repo_id:
            if flt.include_ancestors:
                sql += " AND (repo_id=? OR repo_id IS NULL)"
            else:
                sql += " AND repo_id=?"
            params.append(flt.repo_id)
        sql += " ORDER BY id"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(max(0, int(limit)))
        rows = self.conn.execute(sql, params).fetchall()
        return [_row_to_edge(r) for r in rows]

    # ── code symbol graph ────────────────────────────────────────────────────────
    def clear_symbols_for_file(self, repo_id: str, file: str, *,
                               commit: bool = True) -> None:
        """Retire a file's live code graph rows before an incremental re-index."""
        stamp = now_ts()
        symbol_rows = self.conn.execute(
            "SELECT id FROM symbols WHERE repo_id=? AND file=? "
            "AND valid_to IS NULL AND expired_at IS NULL", (repo_id, file)
        ).fetchall()
        symbol_ids = [row["id"] for row in symbol_rows]
        if symbol_ids:
            marks = ",".join("?" for _ in symbol_ids)
            self.conn.execute(
                f"UPDATE code_memory_links SET valid_to=?, valid_to_recorded_at=? "
                f"WHERE repo_id=? "
                f"AND symbol_id IN ({marks}) AND valid_to IS NULL AND expired_at IS NULL",
                (stamp, stamp, repo_id, *symbol_ids),
            )
        self.conn.execute(
            "UPDATE symbols SET valid_to=?, valid_to_recorded_at=? "
            "WHERE repo_id=? AND file=? "
            "AND valid_to IS NULL AND expired_at IS NULL",
            (stamp, stamp, repo_id, file),
        )
        self.conn.execute(
            "UPDATE code_edges SET valid_to=?, valid_to_recorded_at=? "
            "WHERE repo_id=? AND file=? "
            "AND valid_to IS NULL AND expired_at IS NULL",
            (stamp, stamp, repo_id, file),
        )
        if commit:
            self.conn.commit()

    def upsert_symbol(self, *, repo_id: str, kind: str, name: str, fqname: str, file: str,
                      span: str, signature: str = "", docstring: str = "",
                      lang: str = "", exported: bool = False,
                      content_hash: str = "", commit: bool = True) -> str:
        sid = ids.new_id("symbol")
        self.conn.execute(
            "INSERT INTO symbols(id, repo_id, kind, name, fqname, file, span, signature, "
            "docstring, lang, exported, content_hash, updated_at, valid_from, ingested_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, repo_id, kind, name, fqname, file, span, signature, docstring,
             lang, int(exported), content_hash, now_ts(), now_ts(), now_ts()),
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
            "INSERT INTO code_edges(id, repo_id, src, dst, relation, layer, file, line, "
            "valid_from, ingested_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (eid, repo_id, src, dst, relation, graph_layer.value, file, line,
             now_ts(), now_ts()),
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
                        flt: Optional[SearchFilter] = None,
                        limit: Optional[int] = None) -> list[dict]:
        """Return the current manifest, or its bi-temporal history when anchored."""
        historical = bool(flt and flt.historical)
        table = "code_file_history" if historical else "code_files"
        sql = f"SELECT * FROM {table} WHERE repo_id=?"
        params: list[Any] = [repo_id]
        if historical:
            temporal, temporal_params = _temporal_visibility_sql("", flt)
            sql += " AND " + temporal
            params.extend(temporal_params)
        if languages:
            marks = ",".join("?" for _ in languages)
            sql += f" AND lang IN ({marks})"
            params.extend(sorted(languages))
        sql += " ORDER BY file" + (", version" if historical else "")
        if limit is not None:
            sql += " LIMIT ?"
            params.append(max(0, int(limit)))  # never -1 == SQLite "unlimited"
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def upsert_code_file(self, *, repo_id: str, file: str, lang: str,
                         content_hash: str, size_bytes: int, mtime_ns: int,
                         backend: str, commit: bool = True) -> None:
        stamp = now_ts()
        current_history = self.conn.execute(
            "SELECT version, lang, content_hash, size_bytes, mtime_ns, backend "
            "FROM code_file_history WHERE repo_id=? AND file=? "
            "AND valid_to IS NULL AND expired_at IS NULL",
            (repo_id, file),
        ).fetchone()
        unchanged = current_history is not None and (
            current_history["lang"], current_history["content_hash"],
            int(current_history["size_bytes"] or 0), int(current_history["mtime_ns"] or 0),
            current_history["backend"] or "",
        ) == (lang, content_hash, int(size_bytes), int(mtime_ns), backend)
        if not unchanged:
            if current_history is not None:
                self.conn.execute(
                    "UPDATE code_file_history SET valid_to=?, valid_to_recorded_at=? "
                    "WHERE version=?",
                    (stamp, stamp, current_history["version"]),
                )
            self.conn.execute(
                "INSERT INTO code_file_history("
                "repo_id, file, lang, content_hash, size_bytes, mtime_ns, backend, "
                "indexed_at, valid_from, ingested_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    repo_id, file, lang, content_hash, int(size_bytes), int(mtime_ns),
                    backend, stamp, stamp, stamp,
                ),
            )
        self.conn.execute(
            "INSERT INTO code_files(repo_id, file, lang, content_hash, size_bytes, "
            "mtime_ns, backend, indexed_at) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(repo_id, file) DO UPDATE SET "
            "lang=excluded.lang, content_hash=excluded.content_hash, "
            "size_bytes=excluded.size_bytes, mtime_ns=excluded.mtime_ns, "
            "backend=excluded.backend, indexed_at=excluded.indexed_at",
            (repo_id, file, lang, content_hash, int(size_bytes), int(mtime_ns),
             backend, stamp),
        )
        if commit:
            self.conn.commit()

    def remove_code_file(self, repo_id: str, file: str, *, commit: bool = True) -> None:
        self.clear_symbols_for_file(repo_id, file, commit=False)
        stamp = now_ts()
        self.conn.execute(
            "UPDATE code_file_history SET valid_to=?, valid_to_recorded_at=? "
            "WHERE repo_id=? AND file=? AND valid_to IS NULL AND expired_at IS NULL",
            (stamp, stamp, repo_id, file),
        )
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

    def list_symbols(self, repo_id: str, *, limit: Optional[int] = None,
                     identifiers: Optional[list[str]] = None,
                     flt: Optional[SearchFilter] = None) -> list[dict]:
        """List visible symbols, optionally resolving exact identifiers first.

        ``identifiers`` matches a symbol's ID, short name, or fully-qualified
        name.  The predicate deliberately precedes ``LIMIT``: callers that
        follow a code edge must not lose its endpoint merely because unrelated
        files sort earlier in a large repository.
        """
        if identifiers is not None:
            identifiers = list(dict.fromkeys(value for value in identifiers if value))
            if not identifiers:
                return []
            # Three IN predicates consume three bindings per identifier.  Keep
            # each recursive query below SQLite's conservative parameter limit,
            # then apply the requested cap to the merged, ordered result.
            chunk_size = max(1, IN_CLAUSE_CHUNK // 3)
            if len(identifiers) > chunk_size:
                rows_by_id = {
                    row["id"]: row
                    for start in range(0, len(identifiers), chunk_size)
                    for row in self.list_symbols(
                        repo_id,
                        identifiers=identifiers[start:start + chunk_size],
                        flt=flt,
                    )
                }
                rows = sorted(rows_by_id.values(), key=lambda row: (
                    row.get("file") or "", row.get("fqname") or "", row.get("id") or "",
                ))
                return rows if limit is None else rows[:max(0, int(limit))]
        temporal, params = _temporal_visibility_sql("", flt)
        sql = "SELECT * FROM symbols WHERE repo_id=? AND " + temporal
        params = [repo_id, *params]
        if identifiers is not None:
            marks = ",".join("?" for _ in identifiers)
            sql += f" AND (id IN ({marks}) OR name IN ({marks}) OR fqname IN ({marks}))"
            params.extend(identifiers)
            params.extend(identifiers)
            params.extend(identifiers)
        sql += " ORDER BY file, fqname"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(max(0, int(limit)))  # never -1 == SQLite "unlimited"
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def list_symbols_page(self, repo_id: str, *,
                          after: Optional[tuple[str, str, str]] = None,
                          limit: int = 500,
                          flt: Optional[SearchFilter] = None) -> list[dict]:
        temporal, params = _temporal_visibility_sql("", flt)
        sql = "SELECT * FROM symbols WHERE repo_id=? AND " + temporal
        params = [repo_id, *params]
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
                        layers: Optional[list[GraphLayer]] = None,
                        endpoints: Optional[list[str]] = None,
                        flt: Optional[SearchFilter] = None) -> list[dict]:
        temporal, params = _temporal_visibility_sql("", flt)
        sql = "SELECT * FROM code_edges WHERE repo_id=? AND " + temporal
        params = [repo_id, *params]
        if layers is not None:
            if not layers:
                return []
            marks = ",".join("?" for _ in layers)
            sql += f" AND layer IN ({marks})"
            params.extend(_enum(layer) for layer in layers)
        if endpoints is not None:
            if not endpoints:
                return []
            marks = ",".join("?" for _ in endpoints)
            sql += f" AND (src IN ({marks}) OR dst IN ({marks}))"
            params.extend(endpoints)
            params.extend(endpoints)
        sql += " ORDER BY file, line, id"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(max(0, int(limit)))  # never -1 == SQLite "unlimited"
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def symbols_for_files(self, repo_id: str, files: list[str], *,
                          flt: Optional[SearchFilter] = None) -> list[dict]:
        if not files:
            return []
        marks = ",".join("?" for _ in files)
        temporal, params = _temporal_visibility_sql("", flt)
        rows = self.conn.execute(
            f"SELECT * FROM symbols WHERE repo_id=? AND file IN ({marks}) "
            f"AND {temporal} ORDER BY file, fqname",
            (repo_id, *files, *params),
        ).fetchall()
        return [dict(r) for r in rows]

    def count_code_edges(self, repo_id: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM code_edges WHERE repo_id=? "
            "AND valid_to IS NULL AND expired_at IS NULL", (repo_id,)
        ).fetchone()
        return int(row["n"]) if row else 0

    def search_symbols(self, repo_id: str, query: str, *, limit: int = 20,
                       flt: Optional[SearchFilter] = None) -> list[dict]:
        """Substring match on name/fqname (no embedding yet — v1 is lexical)."""
        like = f"%{_escape_like(query)}%"
        temporal, temporal_params = _temporal_visibility_sql("", flt)
        rows = self.conn.execute(
            f"SELECT * FROM symbols WHERE repo_id=? AND {temporal} "
            "AND (name LIKE ? ESCAPE '\\' OR fqname LIKE ? ESCAPE '\\') "
            "ORDER BY name LIMIT ?",
            (repo_id, *temporal_params, like, like, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_symbol_callers(self, repo_id: str, name: str, *, limit: int = 50,
                           flt: Optional[SearchFilter] = None) -> list[dict]:
        temporal, temporal_params = _temporal_visibility_sql("", flt)
        rows = self.conn.execute(
            "SELECT * FROM code_edges WHERE repo_id=? AND dst=? AND relation='calls' "
            f"AND {temporal} LIMIT ?",
            (repo_id, name, *temporal_params, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def count_symbols(self, repo_id: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM symbols WHERE repo_id=? "
            "AND valid_to IS NULL AND expired_at IS NULL", (repo_id,)
        ).fetchone()
        return int(row["n"]) if row else 0

    def link_memory_symbol(self, *, repo_id: str, symbol_id: str, memory_id: str,
                           relation: str = "mentions", confidence: float = 1.0,
                           commit: bool = True) -> str:
        existing = self.conn.execute(
            "SELECT id FROM code_memory_links WHERE repo_id=? AND symbol_id=? "
            "AND memory_id=? AND relation=? AND valid_to IS NULL AND expired_at IS NULL",
            (repo_id, symbol_id, memory_id, relation),
        ).fetchone()
        if existing is not None:
            return existing["id"]
        link_id = ids.new_id("edge")
        stamp = now_ts()
        self.conn.execute(
            "INSERT OR IGNORE INTO code_memory_links("
            "id, repo_id, symbol_id, memory_id, relation, confidence, created_at, "
            "valid_from, ingested_at"
            ") VALUES (?,?,?,?,?,?,?,?,?)",
            (link_id, repo_id, symbol_id, memory_id, relation,
             max(0.0, min(1.0, float(confidence))), stamp, stamp, stamp),
        )
        if commit:
            self.conn.commit()
        return link_id

    def clear_code_memory_links(self, repo_id: str, *, commit: bool = True) -> None:
        stamp = now_ts()
        self.conn.execute(
            "UPDATE code_memory_links SET valid_to=?, valid_to_recorded_at=? "
            "WHERE repo_id=? "
            "AND valid_to IS NULL AND expired_at IS NULL",
            (stamp, stamp, repo_id),
        )
        if commit:
            self.conn.commit()

    def clear_code_memory_links_for_memories(self, repo_id: str, memory_ids: list[str],
                                             *, commit: bool = True) -> None:
        if not memory_ids:
            return
        marks = ",".join("?" for _ in memory_ids)
        stamp = now_ts()
        self.conn.execute(
            f"UPDATE code_memory_links SET valid_to=?, valid_to_recorded_at=? "
            f"WHERE repo_id=? "
            f"AND memory_id IN ({marks}) AND valid_to IS NULL AND expired_at IS NULL",
            (stamp, stamp, repo_id, *memory_ids),
        )
        if commit:
            self.conn.commit()

    def prune_code_memory_links(self, repo_id: str, *, commit: bool = True) -> None:
        """Remove bridges whose repo-associated memory is no longer live."""
        t = now_ts()
        self.conn.execute(
            "UPDATE code_memory_links SET valid_to=?, valid_to_recorded_at=? "
            "WHERE repo_id=? "
            "AND valid_to IS NULL AND expired_at IS NULL AND NOT EXISTS ("
            "SELECT 1 FROM memories AS m WHERE m.id=code_memory_links.memory_id AND m.repo_id=? "
            "AND (m.valid_from IS NULL OR m.valid_from<=?) "
            "AND (m.valid_to IS NULL OR ?<m.valid_to) AND m.expired_at IS NULL"
            ")",
            (t, t, repo_id, repo_id, t, t),
        )
        if commit:
            self.conn.commit()


    def list_code_memory_links(self, repo_id: str, *,
                               flt: Optional[SearchFilter] = None,
                               limit: Optional[int] = None) -> list[dict]:
        sql = (
            "SELECT l.*, s.name, s.fqname, s.file, s.kind AS symbol_kind, "
            "m.title, m.mtype, m.valid_to AS memory_valid_to, "
            "m.expired_at AS memory_expired_at "
            "FROM code_memory_links l "
            "JOIN symbols s ON s.id=l.symbol_id "
            "JOIN memories m ON m.id=l.memory_id "
            "WHERE l.repo_id=?"
        )
        params: list[Any] = [repo_id]
        link_visibility, link_params = _temporal_visibility_sql("l", flt)
        sql += " AND " + link_visibility
        params.extend(link_params)
        symbol_visibility, symbol_params = _temporal_visibility_sql("s", flt)
        sql += " AND " + symbol_visibility
        params.extend(symbol_params)
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
        link_visibility, link_params = _temporal_visibility_sql("l", flt)
        sql += " AND " + link_visibility
        params.extend(link_params)
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

    def memories_for_symbols(self, repo_id: str, symbol_ids: list[str], *,
                             flt: Optional[SearchFilter] = None,
                             limit: int = 20) -> dict[str, list[dict]]:
        """Return a bounded memory ranking for many symbols in one SQL query."""
        unique_ids = list(dict.fromkeys(
            str(symbol_id) for symbol_id in symbol_ids if str(symbol_id)
        ))[:500]
        if not unique_ids:
            return {}
        per_symbol_limit = max(1, min(100, int(limit)))
        placeholders = ",".join("?" for _ in unique_ids)
        sql = (
            "WITH ranked AS ("
            "SELECT l.symbol_id, m.id, m.title, m.content, m.mtype, m.scope, "
            "m.importance, m.provenance, l.relation, l.confidence, "
            "ROW_NUMBER() OVER (PARTITION BY l.symbol_id "
            "ORDER BY l.confidence DESC, m.importance DESC, "
            "m.ingested_at DESC, l.id, m.id) AS row_rank "
            "FROM code_memory_links l JOIN memories m ON m.id=l.memory_id "
            f"WHERE l.repo_id=? AND l.symbol_id IN ({placeholders})"
        )
        params: list[Any] = [repo_id, *unique_ids]
        link_visibility, link_params = _temporal_visibility_sql("l", flt)
        sql += " AND " + link_visibility
        params.extend(link_params)
        where, visibility_params = self._where(flt, include_invalid=False, alias="m")
        if where:
            sql += " AND " + " AND ".join(where)
            params.extend(visibility_params)
        sql += (
            ") SELECT symbol_id, id, title, content, mtype, scope, importance, "
            "provenance, relation, confidence FROM ranked WHERE row_rank<=? "
            "ORDER BY symbol_id, row_rank"
        )
        params.append(per_symbol_limit)
        grouped: dict[str, list[dict]] = {}
        for row in self.conn.execute(sql, params).fetchall():
            item = dict(row)
            symbol_id = str(item.pop("symbol_id"))
            item["provenance"] = _loads(item.get("provenance"), {})
            grouped.setdefault(symbol_id, []).append(item)
        return grouped

    def symbols_for_memory(self, repo_id: str, memory_id: str, *,
                           flt: Optional[SearchFilter] = None) -> list[dict]:
        link_visibility, link_params = _temporal_visibility_sql("l", flt)
        symbol_visibility, symbol_params = _temporal_visibility_sql("s", flt)
        rows = self.conn.execute(
            "SELECT s.*, l.relation, l.confidence FROM code_memory_links l "
            "JOIN symbols s ON s.id=l.symbol_id "
            f"WHERE l.repo_id=? AND l.memory_id=? AND {link_visibility} "
            f"AND {symbol_visibility} "
            "ORDER BY l.confidence DESC, s.fqname",
            (repo_id, memory_id, *link_params, *symbol_params),
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

    def _backfill_receipt_sequences(self) -> None:
        """Assign durable logical ordinals once when the sequence column is introduced."""
        scopes = self.conn.execute(
            "SELECT DISTINCT workspace_id FROM operation_receipts"
        ).fetchall()
        for scope in scopes:
            workspace_id = str(scope["workspace_id"] or "")
            chain = self._receipt_chain_state(workspace_id)
            for sequence, row in enumerate(chain["rows"], 1):
                self.conn.execute(
                    "UPDATE operation_receipts SET sequence=? WHERE id=?",
                    (sequence, row["id"]),
                )

    def _receipt_chain_state(self, workspace_id: str) -> dict:
        """Reconstruct one receipt chain from immutable predecessor hashes.

        SQLite ``rowid`` is physical placement, not durable ordering: VACUUM and table
        rewrites may renumber it. The receipt payload already carries the true linked-list
        order, while ``receipt_chain_heads`` anchors the expected tail. This helper keeps
        traversal independent of storage layout and returns a deterministic fallback order
        when corruption makes a single chain impossible.
        """
        rows = [dict(row) for row in self.conn.execute(
            "SELECT id, sequence, payload, prev_hash, receipt_hash "
            "FROM operation_receipts "
            "WHERE workspace_id=?",
            (workspace_id,),
        ).fetchall()]

        def text(value: Any) -> str:
            return value if isinstance(value, str) else str(value or "")

        def stable_key(row: dict) -> tuple[str, str]:
            material = "\0".join((
                text(row.get("receipt_hash")),
                text(row.get("prev_hash")),
                text(row.get("id")),
                hashlib.sha256(text(row.get("payload")).encode("utf-8")).hexdigest(),
            ))
            return hashlib.sha256(material.encode("utf-8")).hexdigest(), material

        children: dict[str, list[dict]] = {}
        for row in rows:
            children.setdefault(text(row.get("prev_hash")), []).append(row)
        for candidates in children.values():
            candidates.sort(key=stable_key)

        structure_errors: list[dict] = []
        ordered: list[dict] = []
        roots = children.get("", [])
        if rows and len(roots) != 1:
            structure_errors.append({
                "index": 0,
                "id": "",
                "error": "chain_root_count",
            })
        if len(roots) == 1:
            current = roots[0]
            visited_hashes: set[str] = set()
            while current is not None:
                receipt_hash = text(current.get("receipt_hash"))
                if receipt_hash in visited_hashes:
                    structure_errors.append({
                        "index": len(ordered),
                        "id": text(current.get("id")),
                        "error": "chain_cycle",
                    })
                    break
                visited_hashes.add(receipt_hash)
                ordered.append(current)
                successors = children.get(receipt_hash, [])
                if len(successors) > 1:
                    structure_errors.append({
                        "index": len(ordered) - 1,
                        "id": text(current.get("id")),
                        "error": "chain_fork",
                    })
                    break
                current = successors[0] if successors else None

        ordered_identity = {id(row) for row in ordered}
        if len(ordered) != len(rows):
            structure_errors.append({
                "index": len(ordered),
                "id": "",
                "error": "chain_disconnected",
            })
            ordered.extend(sorted(
                (row for row in rows if id(row) not in ordered_identity),
                key=stable_key,
            ))

        row_errors: list[dict] = []
        for index, row in enumerate(ordered):
            if type(row.get("sequence")) is not int or row["sequence"] != index + 1:
                row_errors.append({
                    "index": index,
                    "id": text(row.get("id")),
                    "error": "sequence_mismatch",
                })
            raw = text(row.get("payload"))
            stored_hash = text(row.get("receipt_hash"))
            if hashlib.sha256(raw.encode("utf-8")).hexdigest() != stored_hash:
                row_errors.append({
                    "index": index,
                    "id": text(row.get("id")),
                    "error": "hash_mismatch",
                })
            try:
                payload = json.loads(raw)
            except (TypeError, ValueError, RecursionError):
                payload = None
            if (
                not isinstance(payload, dict)
                or payload.get("id") != row.get("id")
                or payload.get("prev_hash") != row.get("prev_hash")
            ):
                row_errors.append({
                    "index": index,
                    "id": text(row.get("id")),
                    "error": "payload_mismatch",
                })
            if _public_receipt_row(row).get("invalid_payload") is True:
                row_errors.append({
                    "index": index,
                    "id": text(row.get("id")),
                    "error": "payload_schema_invalid",
                })

        structurally_valid = not structure_errors and len(ordered) == len(rows)
        head = (
            text(ordered[-1].get("receipt_hash"))
            if ordered and structurally_valid else ""
        )
        return {
            "rows": ordered,
            "head": head,
            "structure_errors": structure_errors,
            "row_errors": row_errors,
            "errors": [*row_errors, *structure_errors],
        }

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
        operation = str(operation or "unknown")
        operation_normalized = operation.strip().casefold()
        operation = (
            operation_normalized
            if operation_normalized in _PUBLIC_RECEIPT_OPERATIONS
            else "sha256:" + hashlib.sha256(operation.encode("utf-8")).hexdigest()
        )
        raw_status = str(status or "ok")
        status_normalized = raw_status.strip().casefold()
        safe_status = (
            status_normalized
            if status_normalized in _PUBLIC_RECEIPT_STATUSES
            else "sha256:" + hashlib.sha256(raw_status.encode("utf-8")).hexdigest()
        )
        try:
            safe_target_count = max(0, int(target_count))
        except (TypeError, ValueError, OverflowError):
            safe_target_count = 0
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
                scope_digest = _receipt_scope_digest(workspace_id, repo_id)
                actor_digest = hashlib.sha256(actor.encode("utf-8")).hexdigest()[:16]
                anchor = self.conn.execute(
                    "SELECT receipt_count, head_hash, integrity_error "
                    "FROM receipt_chain_heads "
                    "WHERE workspace_id=?",
                    (workspace_id,),
                ).fetchone()
                anchor_error = str(anchor["integrity_error"] or "") if anchor else ""
                latest = self.conn.execute(
                    "SELECT sequence FROM operation_receipts "
                    "WHERE workspace_id=? ORDER BY sequence DESC LIMIT 1",
                    (workspace_id,),
                ).fetchone()
                current_count: Optional[int] = None
                prev_hash = ""
                if anchor is None and latest is None:
                    # First receipt for a workspace: no scan and no anchor are expected.
                    current_count = 0
                elif anchor is not None:
                    anchor_count = anchor["receipt_count"]
                    anchor_head = anchor["head_hash"]
                    if (
                        type(anchor_count) is int
                        and anchor_count == 0
                        and anchor_head == ""
                        and latest is None
                    ):
                        current_count = 0
                    elif (
                        type(anchor_count) is int
                        and anchor_count > 0
                        and latest is not None
                        and latest["sequence"] == anchor_count
                    ):
                        head_row = self.conn.execute(
                            "SELECT id, sequence, payload, prev_hash, receipt_hash "
                            "FROM operation_receipts "
                            "WHERE workspace_id=? AND sequence=?",
                            (workspace_id, anchor_count),
                        ).fetchone()
                        if (
                            head_row is not None
                            and head_row["receipt_hash"] == anchor_head
                            and not _public_receipt_row(dict(head_row)).get(
                                "invalid_payload", False
                            )
                        ):
                            current_count = anchor_count
                            prev_hash = str(anchor_head)

                if current_count is None:
                    # The independently stored anchor/ordinal did not describe a healthy
                    # head. Reconstruct only on this exceptional path so a safe unique
                    # predecessor can still be extended without retrying the memory action.
                    chain = self._receipt_chain_state(workspace_id)
                    if chain["structure_errors"]:
                        raise sqlite3.IntegrityError(
                            "receipt chain has no unique structural head; append refused"
                        )
                    current_count = len(chain["rows"])
                    prev_hash = str(chain["head"] or "")
                    if chain["row_errors"]:
                        anchor_error = anchor_error or "pre_append_chain_corruption"
                    if anchor is None and current_count:
                        anchor_error = anchor_error or "pre_append_anchor_missing"
                    elif anchor is not None and (
                        type(anchor["receipt_count"]) is not int
                        or anchor["receipt_count"] != current_count
                        or str(anchor["head_hash"]) != prev_hash
                    ):
                        # Keep evidence of deletion or anchor damage while extending the
                        # unique chain that actually remains.
                        anchor_error = anchor_error or "pre_append_anchor_mismatch"
                next_sequence = current_count + 1
                safe_meta = _receipt_metadata(metadata or {})
                payload_obj = {
                    "version": 1,
                    "id": receipt_id,
                    "ts_ms": int(ts * 1000),
                    "operation": operation,
                    "scope_digest": scope_digest,
                    "actor_digest": actor_digest,
                    "target_count": safe_target_count,
                    "status": safe_status,
                    "metadata": safe_meta,
                    "prev_hash": prev_hash,
                }
                payload = json.dumps(
                    payload_obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                )
                receipt_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
                self.conn.execute(
                    "INSERT INTO operation_receipts(id, ts, operation, workspace_id, repo_id, "
                    "sequence, scope_digest, actor, target_count, status, payload, prev_hash, "
                    "receipt_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        receipt_id, ts, operation, workspace_id, repo_id, next_sequence,
                        scope_digest,
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
        safe_limit = max(1, min(10_000, int(limit)))
        rows = self.conn.execute(
            "SELECT id, sequence, payload, prev_hash, receipt_hash "
            "FROM operation_receipts WHERE workspace_id=? "
            "ORDER BY sequence DESC LIMIT ?",
            (workspace_id, safe_limit),
        ).fetchall()
        return [_public_receipt_row(dict(row)) for row in rows]

    def context_savings(self, *, workspace_id: str, repo_id: Optional[str] = None) -> dict:
        """Aggregate validated, content-free context usage from scoped receipts.

        Token counts are kept separate by counter identity: a tokenizer change must not turn
        into a misleading cumulative total. Invalid, missing, and incomplete receipts remain
        visible only as counts; their payload is never reflected into this summary. The
        workspace-wide receipt-chain validity is returned alongside any repo-scoped aggregate
        so callers can distinguish useful local accounting from evidence eligible for audit.
        """
        verification = self.verify_receipts(workspace_id=workspace_id)
        where = "workspace_id=?"
        params: list[str] = [workspace_id]
        if repo_id is not None:
            where += " AND repo_id=?"
            params.append(repo_id)
        rows = self.conn.execute(
            "SELECT id, repo_id, payload, prev_hash, receipt_hash FROM operation_receipts WHERE " + where,
            params,
        ).fetchall()
        totals = {
            "receipt_count": len(rows),
            "usage_receipt_count": 0,
            "savings_receipt_count": 0,
            "invalid_receipt_count": 0,
            "incomplete_usage_receipt_count": 0,
        }
        buckets: dict[str, dict] = {}

        def bucket(counter: str) -> dict:
            return buckets.setdefault(counter, {
                "token_counter": counter,
                "receipt_count": 0,
                "source_tokens": 0,
                "context_tokens": 0,
                "saved_tokens": 0,
                "budget_tokens": 0,
                "packed_count": 0,
                "omitted_count": 0,
                "_operations": {},
            })

        def add(target: dict, usage: dict, operation: str) -> None:
            target["receipt_count"] += 1
            for key in (
                "source_tokens", "context_tokens", "saved_tokens", "budget_tokens",
                "packed_count", "omitted_count",
            ):
                value = usage.get(key)
                if type(value) in (int, float) and value >= 0:
                    target[key] += value
            operation_totals = target["_operations"].setdefault(operation, {
                "operation": operation,
                "receipt_count": 0,
                "source_tokens": 0,
                "context_tokens": 0,
                "saved_tokens": 0,
                "budget_tokens": 0,
                "packed_count": 0,
                "omitted_count": 0,
            })
            operation_totals["receipt_count"] += 1
            for key in (
                "source_tokens", "context_tokens", "saved_tokens", "budget_tokens",
                "packed_count", "omitted_count",
            ):
                value = usage.get(key)
                if type(value) in (int, float) and value >= 0:
                    operation_totals[key] += value

        def finished(target: dict) -> dict:
            operations = target.pop("_operations")
            target["savings_ratio"] = (
                target["saved_tokens"] / target["source_tokens"]
                if target["source_tokens"] else 0.0
            )
            target["by_operation"] = [
                {**value, "savings_ratio": (
                    value["saved_tokens"] / value["source_tokens"]
                    if value["source_tokens"] else 0.0
                )}
                for _, value in sorted(operations.items())
            ]
            return target

        for raw_row in rows:
            receipt = _public_receipt_row(dict(raw_row))
            if (
                receipt.get("invalid_payload")
                or receipt.get("scope_digest")
                != _receipt_scope_digest(workspace_id, raw_row["repo_id"])
            ):
                totals["invalid_receipt_count"] += 1
                continue
            metadata = receipt.get("metadata")
            usage = metadata.get("token_usage") if isinstance(metadata, dict) else None
            if not isinstance(usage, dict):
                continue
            totals["usage_receipt_count"] += 1
            required = ("source_tokens", "context_tokens", "saved_tokens")
            if not all(
                type(usage.get(key)) in (int, float) and usage[key] >= 0
                for key in required
            ):
                totals["incomplete_usage_receipt_count"] += 1
                continue
            expected_saved = max(
                0.0, float(usage["source_tokens"]) - float(usage["context_tokens"])
            )
            if not math.isclose(
                float(usage["saved_tokens"]), expected_saved, rel_tol=0.0, abs_tol=1e-9
            ):
                totals["incomplete_usage_receipt_count"] += 1
                continue
            totals["savings_receipt_count"] += 1
            add(
                bucket(str(usage.get("token_counter") or "unknown")),
                usage,
                str(receipt["operation"]),
            )
        return {
            **totals,
            "receipt_chain_valid": bool(verification["valid"]),
            "receipt_chain_error_count": len(verification["errors"]),
            "by_token_counter": [finished(value) for _, value in sorted(buckets.items())],
        }

    def verify_receipts(self, *, workspace_id: str, expected_head: str = "",
                        expected_count: Optional[int] = None) -> dict:
        chain = self._receipt_chain_state(workspace_id)
        rows = chain["rows"]
        errors: list[dict] = list(chain["errors"])
        head = str(chain["head"] or "")
        anchor = self.conn.execute(
            "SELECT receipt_count, head_hash, integrity_error "
            "FROM receipt_chain_heads WHERE workspace_id=?",
            (workspace_id,),
        ).fetchone()
        if rows and anchor is None:
            errors.append({"index": len(rows), "id": "", "error": "missing_anchor"})
        elif anchor is not None:
            anchor_count = anchor["receipt_count"]
            if type(anchor_count) is not int or anchor_count < 0 or anchor_count != len(rows):
                errors.append({
                    "index": len(rows), "id": "", "error": "anchor_count_mismatch",
                })
            if str(anchor["head_hash"]) != head:
                errors.append({
                    "index": len(rows), "id": "", "error": "anchor_head_mismatch",
                })
            if str(anchor["integrity_error"] or ""):
                errors.append({
                    "index": len(rows), "id": "", "error": "anchor_integrity_error",
                })
        expected_head = str(expected_head or "").strip()
        if expected_head and head != expected_head:
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
            "head": head,
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
            valid_at, known_at = _temporal_anchors(flt)
            where.append(f"({p}valid_from IS NULL OR {p}valid_from<=?)")
            params.append(valid_at)
            where.append(
                f"({p}valid_to IS NULL OR ?<{p}valid_to OR "
                f"({p}valid_to_recorded_at IS NOT NULL "
                f"AND ?<{p}valid_to_recorded_at))"
            )
            params.extend((valid_at, known_at))
            where.append(f"({p}ingested_at IS NULL OR {p}ingested_at<=?)")
            params.append(known_at)
            where.append(f"({p}expired_at IS NULL OR ?<{p}expired_at)")
            params.append(known_at)
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
        valid_to_recorded_at=(
            row["valid_to_recorded_at"]
            if "valid_to_recorded_at" in row.keys() else None
        ),
        ingested_at=row["ingested_at"], expired_at=row["expired_at"],
        subject_key=row["subject_key"] if "subject_key" in row.keys() else "",
        claim_kind=row["claim_kind"] if "claim_kind" in row.keys() else "",
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
        valid_to_recorded_at=(
            row["valid_to_recorded_at"]
            if "valid_to_recorded_at" in row.keys() else None
        ),
        ingested_at=row["ingested_at"], expired_at=row["expired_at"],
        provenance=_loads(row["provenance"], {}),
    )


def _fts_query(q: str) -> str:
    """Make a safe FTS5 MATCH query: OR the alphanumeric terms as prefixes."""
    terms = [t for t in "".join(c if c.isalnum() else " " for c in q).split() if t]
    return " OR ".join(f'{t}*' for t in terms) if terms else '""'
