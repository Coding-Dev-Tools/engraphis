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
import tempfile
import threading
import time
import unicodedata
import weakref
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Protocol

import numpy as np

from engraphis.private_state import ensure_owner_private_dir
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
    _finite_number,
    _finite_timestamp,
    advance_modified_hlc,
    normalize_modified_hlc,
)
from engraphis.core.secrets import reject_secrets
from engraphis.core.poisoning import (
    REVIEW_APPROVED,
    REVIEW_PENDING,
    llm_consolidation_kind,
    pending_llm_consolidation_envelope,
    pending_llm_extraction_envelope,
)
from engraphis.core.documents import normalize_document_path
from engraphis.core.retention_policy import (
    DEFAULT_STABILITY_DAYS,
    MAX_ACCESS_COUNT,
    MAX_STABILITY_DAYS,
    MIN_STABILITY_DAYS,
    effective_access_count,
    effective_stability,
    reinforced_stability,
)
from engraphis.core.savings import normalize_release_version
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
# Keep dynamic blocking predicates well below SQLite's conservative 999-variable
# and expression-depth limits. Each token contributes two LIKE parameters.
ENTITY_BLOCK_TOKEN_CHUNK = 200
# Do not materialize unbounded common-token buckets during migration/live writes.
ENTITY_BLOCK_BUCKET_LIMIT = 1024
_LLM_CONSOLIDATION_REPAIR_STATE_KEY = "__schema_v11_llm_consolidation_trust_repair"
_LLM_CONSOLIDATION_REPAIR_STATE_VALUE = "complete"
_LLM_EXTRACTION_REPAIR_STATE_KEY = "__schema_v12_llm_extraction_trust_repair"
_LLM_EXTRACTION_REPAIR_STATE_VALUE = "complete"
TOMBSTONE_NEVER_EXPORT = "never_export"
TOMBSTONE_REMOTE_ERASURE = "remote_erasure"
TOMBSTONE_EXPORT_CLASSES = frozenset({
    TOMBSTONE_NEVER_EXPORT,
    TOMBSTONE_REMOTE_ERASURE,
})
_SOURCE_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_IMPORT_RECEIPT_COUNT_KEYS = frozenset({
    "files_scanned", "files_imported", "files_updated", "files_skipped",
    "files_renamed", "files_rejected", "files_missing", "files_errored",
    "conflicts", "warnings", "attachments", "wikilinks", "aliases", "tags",
})
_MAX_RECEIPT_COUNT = 1_000_000_000
USER_SCOPE_UNSUPPORTED = (
    "user scope is not supported until owner-aware memories are implemented; "
    "use workspace, repo, or session"
)




def now_ts() -> float:
    return time.time()


def _content_free_source_error(value: Any) -> str:
    """Persist an error code or one-way digest, never a source-derived message."""
    raw = str(value or "")
    normalized = raw.strip().casefold().replace(" ", "_")
    if not normalized:
        return ""
    if re.fullmatch(r"[a-z0-9_.:-]{1,100}", normalized):
        return normalized
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


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


def _close_connection_quietly(conn: Any) -> None:
    """Best-effort cleanup for a Store abandoned without an explicit close."""
    try:
        conn.close()
    except Exception:
        pass


class ReadOnlyConnector(Protocol):
    """Explicit contract for injected connectors used by a read-only ``Store``.

    Writable compatibility remains the ordinary ``connector(path)`` call.  A
    connector that also supports inspection must expose ``open_read_only(path)``
    and open the already-existing regular file without creating, recovering, or
    mutating the database or any sidecar.  Implementations should use their
    driver's equivalent of SQLite ``mode=ro&immutable=1``.  Store rejects a bare
    callable in read-only mode rather than guessing that it is safe.
    """

    def __call__(self, path: str) -> Any: ...

    def open_read_only(self, path: str) -> Any: ...


def _row_is_prompt_eligible(provenance: Any, metadata: Any) -> bool:
    """Use the one trust predicate before exposing a derived bridge.

    Store normally stays independent of policy, but code-memory links are a derived
    index that otherwise outlives a source's review state.  Keep this tiny adapter
    here so every store-level bridge read and prune operation applies exactly the
    same predicate as prompt packing and write-time derivation.
    """
    from engraphis.core.poisoning import prompt_eligible

    prov = provenance if isinstance(provenance, dict) else _loads(provenance, {})
    meta = metadata if isinstance(metadata, dict) else _loads(metadata, {})
    return prompt_eligible(prov, meta)


def _merge_provenance_envelopes(dedicated: dict, nested: dict) -> dict:
    """Merge trust envelopes without losing a restrictive assertion."""
    provenance = {**dedicated, **nested}
    envelopes = (dedicated, nested)
    if any(item.get("trusted") is False for item in envelopes):
        provenance["trusted"] = False
    if any(item.get("quarantined") is True for item in envelopes):
        provenance["quarantined"] = True
    for item in envelopes:
        state = item.get("review_state")
        if state and state != REVIEW_APPROVED:
            provenance["review_state"] = state
            break
    return provenance


def _edge_is_prompt_eligible(provenance: Any) -> bool:
    """Apply the canonical direct-edge trust predicate at the store boundary."""
    from engraphis.core.poisoning import edge_provenance_prompt_eligible

    prov = provenance if isinstance(provenance, dict) else _loads(provenance, {})
    return edge_provenance_prompt_eligible(prov)


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


def _entity_token_set(name: Any) -> set[str]:
    """Return conservative blocking tokens for one entity spelling."""
    return {
        token
        for token in re.split(r"[^a-z0-9]+", str(name or "").casefold())
        if len(token) >= 2
    }


def _entity_compact_name(name: Any) -> str:
    """Return the punctuation-preserving, whitespace-insensitive spelling."""
    return re.sub(r"\s+", "", normalize_entity_name(str(name or "")))


def _entity_punctuation_signature(name: Any) -> str:
    """Return meaningful punctuation so token blocking cannot cross its boundary."""
    normalized = normalize_entity_name(str(name or ""))
    return "".join(
        character for character in normalized
        if not character.isalnum() and not character.isspace()
    )


def _entity_overlap(left: Any, right: Any) -> Optional[float]:
    """Return the token-blocking score, or ``None`` when no safe match exists."""
    left_compact = _entity_compact_name(left)
    right_compact = _entity_compact_name(right)
    if left_compact and left_compact == right_compact:
        return 1.0
    if _entity_punctuation_signature(left) != _entity_punctuation_signature(right):
        return None
    left_tokens = _entity_token_set(left)
    right_tokens = _entity_token_set(right)
    if not left_tokens or not right_tokens:
        return None
    return len(left_tokens & right_tokens) / max(len(left_tokens), len(right_tokens))


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
    "savings_basis": {
        "history_retrieval", "history_fallback", "history_bypass",
        "low_confidence_abstain", "packed_context", "unclassified",
    },
    "savings_confidence": {"high", "medium", "none", "unknown"},
}


def _receipt_metadata(metadata: dict) -> dict:
    """Keep receipt metadata useful but content-free and bounded."""
    allowed = {
        "mtype", "scope", "resolution", "retention", "extracted", "intent", "k",
        "result_count", "grounded", "citations", "relation", "layer", "graph_layers",
        "files_scanned", "files_indexed", "files_removed", "files_imported",
        "files_updated", "files_renamed", "files_skipped", "files_rejected",
        "files_missing", "files_errored", "conflicts", "warnings",
        "attachments", "wikilinks", "aliases", "tags", "symbols", "edges",
        "entities", "relations", "tables", "dry_run", "error_count",
        "entities_added", "relations_added",
        "retrieval_profile", "candidate_depth", "candidate_k_requested",
        "candidate_k_used", "response_mode", "historical", "token_usage",
        "adaptive_mode", "action_id", "schema_version", "result_mode",
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
        if safe_key in _IMPORT_RECEIPT_COUNT_KEYS:
            # Import summaries are counts, never arbitrary floats/labels.  Reject
            # booleans and clamp adversarially large values so the durable public
            # receipt stays predictable and bounded.
            if type(value) is int:
                out[safe_key] = max(0, min(_MAX_RECEIPT_COUNT, value))
        elif safe_key == "token_usage":
            if not isinstance(value, dict):
                continue
            numeric = {
                name: value[name]
                for name in (
                    "budget_tokens", "context_tokens", "source_tokens", "saved_tokens",
                    "savings_ratio", "packed_count", "omitted_count", "baseline_tokens",
                    "emitted_tokens", "estimated_saved_tokens", "estimated_savings_ratio",
                )
                if type(value.get(name)) in (int, float)
                and math.isfinite(float(value[name]))
            }
            if type(value.get("savings_eligible")) is bool:
                numeric["savings_eligible"] = value["savings_eligible"]
            counter = value.get("token_counter")
            if isinstance(counter, str):
                if counter in {"engraphis.regex.v1", "estimate_tokens"}:
                    numeric["token_counter"] = counter
                else:
                    numeric["token_counter"] = (
                        "sha256:" + hashlib.sha256(counter.encode("utf-8")).hexdigest()
                    )
            for key in ("savings_basis", "savings_confidence"):
                label = value.get(key)
                if isinstance(label, str):
                    numeric[key] = content_free_label(key, label)
            release_version = normalize_release_version(value.get("release_version"))
            if release_version:
                numeric["release_version"] = release_version
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
    "files_imported", "files_updated", "files_renamed", "files_skipped",
    "files_rejected", "files_missing", "files_errored", "conflicts",
    "warnings", "attachments", "wikilinks", "aliases", "tags",
    "entities", "relations", "tables", "dry_run", "error_count",
    "entities_added", "relations_added", "retrieval_profile", "candidate_depth",
    "candidate_k_requested", "candidate_k_used", "response_mode", "historical",
    "token_usage", "adaptive_mode", "action_id", "schema_version", "result_mode",
}
_PUBLIC_RECEIPT_OPERATIONS = {
    "remember", "recall", "promote", "link", "index_repo",
    "graph_index", "grounded_recall", "adaptive_context", "proactive_context", "smart_gateway",
    "consolidate", "sync", "document_import", "obsidian_import",
}
_PUBLIC_RECEIPT_STATUSES = {
    "ok", "add", "noop", "invalidate", "relate", "ingested",
    "postgres_schema", "grounded", "abstained", "promoted",
    "indexed", "skipped", "error", "failed", "completed", "cancelled", "partial",
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
        if key in _IMPORT_RECEIPT_COUNT_KEYS:
            if type(value) is not int or not 0 <= value <= _MAX_RECEIPT_COUNT:
                return invalid
        elif key == "token_usage":
            if not isinstance(value, dict):
                return invalid
            allowed_usage = {
                "budget_tokens", "context_tokens", "source_tokens", "saved_tokens",
                "savings_ratio", "packed_count", "omitted_count", "token_counter",
                "baseline_tokens", "emitted_tokens", "estimated_saved_tokens",
                "estimated_savings_ratio", "savings_basis", "savings_confidence",
                "savings_eligible", "release_version",
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
                elif usage_key == "savings_basis":
                    if not (
                        usage_value in _PUBLIC_RECEIPT_LABELS_BY_KEY["savings_basis"]
                        or (
                            isinstance(usage_value, str)
                            and _PUBLIC_RECEIPT_HASHED_LABEL.fullmatch(usage_value)
                        )
                    ):
                        return invalid
                elif usage_key == "savings_confidence":
                    if usage_value not in _PUBLIC_RECEIPT_LABELS_BY_KEY["savings_confidence"]:
                        return invalid
                elif usage_key == "savings_eligible":
                    if type(usage_value) is not bool:
                        return invalid
                elif usage_key == "release_version":
                    if normalize_release_version(usage_value) != usage_value:
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


def _fts5_available(conn: sqlite3.Connection | _SerializedConnection) -> bool:
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
    world = _finite_timestamp(valid_at, "valid_at")
    if world is None and flt is not None:
        world = _finite_timestamp(flt.valid_at, "valid_at")
    known = _finite_timestamp(
        flt.known_at if flt is not None else None, "known_at"
    )
    present = _finite_timestamp(now_ts(), "current timestamp")
    if present is None:
        raise AssertionError("current timestamp unexpectedly became null")
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
        if flt.scopes is not None and rec.scope not in flt.scopes:
            return False
        if flt.mtypes is not None and rec.mtype not in flt.mtypes:
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


class _MaterializedCursor:
    """Cursor-compatible snapshot whose rows were drained under the connection lock.

    A live sqlite cursor is tied to its connection's current statement state. Returning
    one after releasing the shared-connection lock lets another thread mutate that state
    before ``fetchone()``, ``fetchall()``, or iteration completes. Query results are
    therefore materialized while serialized, then exposed through this small cursor
    facade. DML cursors remain native so ``rowcount`` and ``lastrowid`` keep their exact
    sqlite semantics.
    """

    def __init__(self, connection: "_SerializedConnection", raw, rows: list[Any]) -> None:
        self._connection = connection
        self._raw = raw
        self._rows = rows
        self._index = 0
        self.arraysize = raw.arraysize

    def __getattr__(self, name):
        return getattr(self._raw, name)

    def fetchone(self):
        if self._index >= len(self._rows):
            return None
        row = self._rows[self._index]
        self._index += 1
        return row

    def fetchmany(self, size: Optional[int] = None) -> list[Any]:
        count = self.arraysize if size is None else int(size)
        if count < 0:
            raise ValueError("fetchmany size must be non-negative")
        end = min(len(self._rows), self._index + count)
        rows = self._rows[self._index:end]
        self._index = end
        return rows

    def fetchall(self) -> list[Any]:
        rows = self._rows[self._index:]
        self._index = len(self._rows)
        return rows

    def execute(self, *a, **k):
        return self._connection.execute(*a, **k)

    def executemany(self, *a, **k):
        return self._connection.executemany(*a, **k)

    def executescript(self, *a, **k):
        return self._connection.executescript(*a, **k)

    def close(self) -> None:
        self._rows = []
        self._index = 0
        self._connection._run(self._raw.close)

    def __iter__(self):
        return self

    def __next__(self):
        row = self.fetchone()
        if row is None:
            raise StopIteration
        return row


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
    released on commit/rollback — so transactions never interleave. Query cursors are
    drained into immutable snapshots before the per-statement lock is released, preventing
    a later fetch from racing another thread's write. Two safety nets keep a stuck
    transaction from deadlocking the process: a statement that raises while a transaction
    is open rolls it back and frees the pin, and lock acquisition times out (raising, not
    blocking forever). Non-statement attributes/methods (``in_transaction``,
    ``enable_load_extension`` at setup, ...) pass straight through.
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

    @contextmanager
    def defer_commits(self):
        """Keep nested Store helpers inside the caller's transaction boundary.

        Many Store methods preserve their standalone API by committing their own write.
        A service operation that composes several such helpers needs one atomic boundary,
        and a service invoked inside a caller-owned transaction must not commit that
        caller's work. This thread-local barrier turns nested ``commit()`` calls into
        no-ops. A savepoint also redirects nested ``rollback()`` calls so a failed helper
        can discard this service operation without settling work the caller wrote before
        entering it. The outer owner commits or rolls back after leaving the scope.
        """
        depth = int(getattr(self._pin, "defer_commits", 0))
        if depth:
            self._pin.defer_commits = depth + 1
            try:
                yield
            finally:
                self._pin.defer_commits = depth
            return
        if not self.transaction_owned_by_current_thread():
            raise RuntimeError("commit deferral requires a caller-owned transaction")
        savepoint = f"engraphis_service_{threading.get_ident()}_{time.monotonic_ns()}"
        self.execute(f"SAVEPOINT {savepoint}")
        self._pin.defer_savepoint = savepoint
        self._pin.defer_commits = depth + 1
        try:
            try:
                yield
            except BaseException:
                self.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                self.execute(f"RELEASE SAVEPOINT {savepoint}")
                raise
            else:
                self.execute(f"RELEASE SAVEPOINT {savepoint}")
        finally:
            for attribute in ("defer_commits", "defer_savepoint"):
                try:
                    delattr(self._pin, attribute)
                except AttributeError:
                    pass

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
        # Finalizers may run while a test or embedding application temporarily
        # instruments the acquire hook.  Teardown must use the primitive lock directly;
        # dispatching through ``self._acquire`` can invoke an observer after its owning
        # Store has become unreachable and can crash CPython while closing SQLite on
        # Windows.
        if not self._lock.acquire(timeout=self._ACQUIRE_TIMEOUT):
            raise sqlite3.OperationalError(
                "store write lock timeout — a transaction appears stuck"
            )
        succeeded = False
        try:
            fn()
            succeeded = True
        finally:
            # A deferred constraint can make commit() raise while SQLite deliberately
            # leaves the transaction open. Preserve this thread's pin in that case so a
            # waiter cannot adopt the failed transaction; the owner can still roll back.
            keep_pin = False
            if self._pinned() and not succeeded:
                try:
                    keep_pin = bool(self._raw.in_transaction)
                except Exception:  # noqa: BLE001 - a failed/closed connector cannot be kept
                    keep_pin = False
            if self._pinned() and not keep_pin:
                self._pin.held = False
                self._lock.release()          # release the transaction pin
            self._lock.release()              # release this call's acquire

    def execute(self, *a, **k):
        def execute_and_snapshot(*aa, **kk):
            cursor = self._raw.execute(*aa, **kk)
            if cursor.description is None:
                return cursor
            return _MaterializedCursor(self, cursor, cursor.fetchall())

        return self._run(execute_and_snapshot, *a, **k)

    def fetchone(self, *a, **k):
        """Execute and drain a one-row read in one locked section."""
        return self._run(lambda *aa, **kk: self._raw.execute(*aa, **kk).fetchone(), *a, **k)

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
        if getattr(self._pin, "defer_commits", 0):
            return
        self._finish(self._raw.commit)

    def rollback(self):
        savepoint = getattr(self._pin, "defer_savepoint", "")
        if getattr(self._pin, "defer_commits", 0) and savepoint:
            self.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            return
        self._finish(self._raw.rollback)

    def close(self):
        # Closing participates in the same lock as statements and transaction
        # settlement. This prevents shutdown from racing a thread that still owns the
        # shared connection's write transaction.
        self._finish(self._raw.close)

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
                 connect: Optional[Callable[[str], Any]] = None,
                 read_only: bool = False) -> None:
        """Open a store.

        ``read_only`` is deliberately stronger than merely promising not to call a
        writer: it opens a checkpointed SQLite file with ``mode=ro&immutable=1`` and
        skips schema setup, migrations, backups, and the persistent WAL-mode pragma.
        It is for inspection tools (notably security dry-runs) whose safety contract
        includes leaving a database and its sidecar files untouched. Non-empty WAL and
        rollback-journal sidecars are rejected rather than silently scanning an
        incomplete immutable snapshot. An injected connector must implement the
        :class:`ReadOnlyConnector` ``open_read_only(path)`` contract; a bare writable
        callable is rejected before it can be invoked.
        """
        self.path = path
        self._connect = connect
        self.read_only = bool(read_only)
        if self.read_only and path == ":memory:":
            raise ValueError("read-only Store requires an existing database file")
        read_only_path: Optional[str] = None
        if self.read_only:
            if self._connect is not None and not callable(
                getattr(self._connect, "open_read_only", None)
            ):
                raise TypeError(
                    "read-only Store requires an injected connector with an "
                    "open_read_only(path) method"
                )
            read_only_path = self._preflight_read_only_path(path)
        if path != ":memory:" and not self.read_only:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        raw_conn = self._open_connection(read_only_path or path)
        # Serialize the shared connection so concurrent threadpool handlers can't interleave
        # transactions on it (see _SerializedConnection). All Store/service/backend access
        # goes through self.conn, so wrapping here covers every writer.
        self.conn = _SerializedConnection(raw_conn)
        self._close_lock = threading.Lock()
        self._connection_finalizer = weakref.finalize(
            self, _close_connection_quietly, self.conn
        )
        self.has_fts5 = False
        self._receipt_lock = threading.Lock()
        self.allowed_workspaces: Optional[frozenset] = (
            frozenset(allowed_workspaces) if allowed_workspaces else None
        )
        try:
            self.conn.execute("PRAGMA foreign_keys=ON")
            if self.read_only:
                # Defense in depth after the stdlib immutable URI or the injected
                # connector's explicit immutable-open contract. Do not probe FTS5 by
                # creating a temporary table here: an inspection must not write anything.
                self.conn.execute("PRAGMA query_only=ON")
                self._validate_read_only_ready()
                row = self.conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='mem_fts'"
                ).fetchone()
                self.has_fts5 = bool(
                    row and "virtual table" in str(row["sql"] or "").casefold()
                    and "fts5" in str(row["sql"] or "").casefold()
                )
            else:
                # Keep deleted pages scrubbed even when an emergency erase cannot run a
                # final VACUUM because another connection has the database busy.  The
                # per-erase helper sets this too for legacy connections and backups;
                # setting it at writable-store startup makes the protection durable for
                # every normal v2 connection without changing the schema or data model.
                self.conn.execute("PRAGMA secure_delete=ON")
                self.conn.execute("PRAGMA synchronous=NORMAL")
                self.init_schema()
                # journal_mode is persistent state, so set it only after a required backup
                # and the transactional migration have completed successfully.
                self.conn.execute("PRAGMA journal_mode=WAL")
        except BaseException:
            try:
                if self.conn.transaction_owned_by_current_thread():
                    self.conn.rollback()
            finally:
                self.close()
            raise

    def _open_connection(self, path: str):
        """Open *path* with the primary database's connection semantics."""
        if self._connect is not None:
            # Injected factories own opening, keying, row_factory, and exception
            # translation (notably the SQLCipher backend).
            if self.read_only:
                return self._connect.open_read_only(path)  # type: ignore[attr-defined]
            return self._connect(path)
        if self.read_only:
            uri = Path(path).resolve().as_uri() + "?mode=ro&immutable=1"
            conn = sqlite3.connect(uri, uri=True, timeout=30, check_same_thread=False)
        else:
            conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _preflight_read_only_path(path: str) -> str:
        """Validate one immutable snapshot path without creating or opening it.

        The resolved regular file is returned so a connector cannot reinterpret a
        relative path after validation.  Active WAL/rollback-journal state is
        refused because an immutable connection would skip recovery and silently
        expose an incomplete snapshot.
        """
        candidate = Path(path)
        try:
            info = os.lstat(candidate)
        except OSError:
            raise RuntimeError(
                "read-only Store requires an existing regular database file"
            ) from None
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        file_attributes = int(getattr(info, "st_file_attributes", 0))
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISREG(info.st_mode)
            or bool(reparse_flag and file_attributes & reparse_flag)
        ):
            raise RuntimeError(
                "read-only Store requires an existing regular database file"
            )
        for suffix, label in (("-wal", "WAL"), ("-journal", "rollback journal")):
            sidecar = Path(f"{candidate}{suffix}")
            try:
                sidecar_info = os.lstat(sidecar)
            except FileNotFoundError:
                continue
            except OSError:
                raise RuntimeError(
                    f"read-only Store could not validate the {label} sidecar"
                ) from None
            if sidecar_info.st_size:
                raise RuntimeError(
                    "read-only Store requires a checkpointed database; "
                    f"active {label} found"
                )
        try:
            return str(candidate.resolve(strict=True))
        except OSError:
            raise RuntimeError(
                "read-only Store requires an existing regular database file"
            ) from None

    def _validate_read_only_ready(self) -> None:
        """Fail closed unless the immutable snapshot can serve the current schema."""
        required = {
            "workspaces",
            "repos",
            "sessions",
            "memories",
            "mem_vectors",
            "entities",
            "edges",
            "mem_links",
            "memory_tombstones",
            "memory_sync_exports",
            "operation_receipts",
            "schema_migrations",
            "source_vaults",
            "source_imports",
            "source_import_items",
        }
        rows = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        present = {str(row["name"]) for row in rows}
        missing = sorted(required - present)
        if missing:
            raise RuntimeError(
                "read-only Store requires a complete current schema; missing "
                + ", ".join(missing)
            )
        source_security_objects = {
            str(row["name"])
            for row in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('index','trigger')"
            ).fetchall()
        }
        required_source_security_objects = {
            "idx_source_vaults_identity",
            "trg_source_vault_scope_insert",
            "trg_source_vault_scope_update",
            "trg_source_import_scope_insert",
            "trg_source_import_scope_update",
            "trg_source_import_seen_job_insert",
            "trg_source_import_seen_job_update",
            "trg_source_import_job_insert",
            "trg_source_import_job_update",
        }
        missing_source_security = sorted(
            required_source_security_objects - source_security_objects
        )
        if missing_source_security:
            raise RuntimeError(
                "read-only Store requires a complete current schema; missing "
                + ", ".join(missing_source_security)
            )
        source_vault_foreign_keys = {
            (str(row["from"]), str(row["table"]), str(row["to"]))
            for row in self.conn.execute(
                "PRAGMA foreign_key_list(source_vaults)"
            ).fetchall()
        }
        if not {
            ("workspace_id", "workspaces", "id"),
            ("repo_id", "repos", "id"),
            ("session_id", "sessions", "id"),
        }.issubset(source_vault_foreign_keys):
            raise RuntimeError(
                "read-only Store requires source_vaults scope foreign keys"
            )
        session_columns = {
            str(item["name"]) for item in self.conn.execute(
                "PRAGMA table_info(sessions)"
            ).fetchall()
        }
        if "handoff" not in session_columns:
            raise RuntimeError(
                "read-only Store requires a complete current schema; missing "
                "sessions.handoff"
            )
        memory_columns = {
            str(item["name"]) for item in self.conn.execute(
                "PRAGMA table_info(memories)"
            ).fetchall()
        }
        if "modified_hlc" not in memory_columns:
            raise RuntimeError(
                "read-only Store requires a complete current schema; missing "
                "memories.modified_hlc"
            )
        sync_export_columns = {
            str(item["name"]) for item in self.conn.execute(
                "PRAGMA table_info(memory_sync_exports)"
            ).fetchall()
        }
        required_sync_export_columns = {
            "memory_id", "workspace_id", "repo_id",
            "first_exported_at", "last_exported_at",
        }
        missing_sync_export_columns = sorted(
            required_sync_export_columns - sync_export_columns
        )
        if missing_sync_export_columns:
            raise RuntimeError(
                "read-only Store requires a complete current schema; missing "
                + ", ".join(
                    f"memory_sync_exports.{name}"
                    for name in missing_sync_export_columns
                )
            )
        tombstone_columns = {
            str(item["name"]) for item in self.conn.execute(
                "PRAGMA table_info(memory_tombstones)"
            ).fetchall()
        }
        if "export_class" not in tombstone_columns:
            raise RuntimeError(
                "read-only Store requires a complete current schema; missing "
                "memory_tombstones.export_class"
            )
        source_import_columns = {
            str(item["name"]) for item in self.conn.execute(
                "PRAGMA table_info(source_imports)"
            ).fetchall()
        }
        required_source_import_columns = {
            "vault_id", "source_key", "relative_path", "memory_id",
            "subject_key", "content_sha256", "canonical_sha256",
            "last_seen_job_id", "state", "last_seen_at",
        }
        missing_source_import_columns = sorted(
            required_source_import_columns - source_import_columns
        )
        if missing_source_import_columns:
            raise RuntimeError(
                "read-only Store requires a complete current schema; missing "
                + ", ".join(
                    f"source_imports.{name}" for name in missing_source_import_columns
                )
            )
        source_item_columns = {
            str(item["name"]) for item in self.conn.execute(
                "PRAGMA table_info(source_import_items)"
            ).fetchall()
        }
        required_source_item_columns = {
            "job_id", "source_id", "relative_path", "planned_action",
            "source_format", "result_state", "warning_count", "error_code",
        }
        missing_source_item_columns = sorted(
            required_source_item_columns - source_item_columns
        )
        if missing_source_item_columns:
            raise RuntimeError(
                "read-only Store requires a complete current schema; missing "
                + ", ".join(
                    f"source_import_items.{name}" for name in missing_source_item_columns
                )
            )
        row = self.conn.execute(
            "SELECT MAX(version) AS version FROM schema_migrations"
        ).fetchone()
        version = int(row["version"]) if row and row["version"] is not None else 0
        if version != SCHEMA_VERSION:
            raise RuntimeError(
                f"read-only Store schema {version} is not current "
                f"(expected {SCHEMA_VERSION}); open it once with a writable Store"
            )
        if not self._quick_check(self.conn):
            raise sqlite3.DatabaseError("read-only Store integrity check failed")

    @classmethod
    def snapshot_source_import_manifest(
        cls, path: str, *, connect: Optional[ReadOnlyConnector] = None,
    ) -> dict:
        """Read an importer manifest without migrating or changing a database.

        This is deliberately usable by previews against an older v13 database (and
        against a not-yet-created database). Active WAL and rollback-journal state is
        refused: immutable reads would otherwise silently miss recovered manifest
        state. Injected connectors must opt in through ``open_read_only(path)``;
        ordinary writable callables are never invoked here.
        """
        empty = {"schema_version": 0, "vaults": [], "items": []}
        if path in (":memory:", ""):
            return empty
        db_path = Path(path)
        try:
            os.lstat(db_path)
        except FileNotFoundError:
            return empty
        except OSError:
            raise RuntimeError(
                "import manifest database could not be inspected"
            ) from None
        if connect is not None and not callable(
            getattr(connect, "open_read_only", None)
        ):
            raise TypeError(
                "import manifest snapshot requires an injected connector with an "
                "open_read_only(path) method"
            )
        resolved_path = cls._preflight_read_only_path(path)
        db_path = Path(resolved_path)

        def sidecar_state() -> dict[str, Optional[tuple[int, int, int, int]]]:
            state: dict[str, Optional[tuple[int, int, int, int]]] = {}
            for suffix in ("-wal", "-journal", "-shm"):
                sidecar = Path(f"{db_path}{suffix}")
                try:
                    info = os.lstat(sidecar)
                except FileNotFoundError:
                    state[suffix] = None
                except OSError:
                    raise RuntimeError(
                        "import manifest database sidecars could not be inspected"
                    ) from None
                else:
                    state[suffix] = (
                        int(info.st_dev), int(info.st_ino), int(info.st_size),
                        int(info.st_mtime_ns),
                    )
            return state

        version_fields = (
            ("st_size", "st_mtime_ns")
            if os.name == "nt"
            else ("st_size", "st_mtime_ns", "st_ctime_ns")
        )

        def same_version(left, right) -> bool:
            return cls._same_file(left, right) and all(
                getattr(left, name, None) == getattr(right, name, None)
                for name in version_fields
            )

        try:
            before = os.lstat(db_path)
            before_sidecars = sidecar_state()
        except OSError:
            raise RuntimeError(
                "import manifest database could not be inspected"
            ) from None

        source_flags = (
            os.O_RDONLY | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            source_descriptor = os.open(str(db_path), source_flags)
        except OSError:
            raise RuntimeError(
                "import manifest database could not be opened safely"
            ) from None
        try:
            opened = os.fstat(source_descriptor)
            attributes = int(getattr(opened, "st_file_attributes", 0))
            reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            if (
                not stat.S_ISREG(opened.st_mode)
                or bool(reparse_flag and attributes & reparse_flag)
                or not same_version(before, opened)
            ):
                raise RuntimeError(
                    "import manifest database changed while it was opened"
                )

            with tempfile.TemporaryDirectory(
                prefix="engraphis-manifest-snapshot-",
            ) as temp_root:
                temp_directory = Path(temp_root)
                ensure_owner_private_dir(temp_directory)
                temp_path = temp_directory / "manifest.db"
                output_flags = (
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL
                    | getattr(os, "O_BINARY", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                )
                output_descriptor = os.open(str(temp_path), output_flags, 0o600)
                try:
                    fchmod = getattr(os, "fchmod", None)
                    if fchmod is not None:
                        fchmod(output_descriptor, 0o600)
                    while True:
                        chunk = os.read(source_descriptor, 1024 * 1024)
                        if not chunk:
                            break
                        view = memoryview(chunk)
                        while view:
                            written = os.write(output_descriptor, view)
                            if written <= 0:
                                raise OSError("private snapshot copy made no progress")
                            view = view[written:]
                finally:
                    os.close(output_descriptor)

                try:
                    after_copy = os.fstat(source_descriptor)
                    current = os.lstat(db_path)
                    current_sidecars = sidecar_state()
                except OSError:
                    raise RuntimeError(
                        "import manifest database changed during snapshot"
                    ) from None
                if (
                    not same_version(opened, after_copy)
                    or not same_version(after_copy, current)
                    or before_sidecars != current_sidecars
                ):
                    raise RuntimeError(
                        "import manifest database changed during snapshot"
                    )

                if connect is not None:
                    conn = connect.open_read_only(str(temp_path))
                else:
                    uri = temp_path.as_uri() + "?mode=ro&immutable=1"
                    conn = sqlite3.connect(uri, uri=True)
                    conn.row_factory = sqlite3.Row
                try:
                    conn.execute("PRAGMA query_only=ON")
                    tables = {str(row[0]) for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()}
                    version = 0
                    if "schema_migrations" in tables:
                        row = conn.execute(
                            "SELECT MAX(version) FROM schema_migrations"
                        ).fetchone()
                        version = int(row[0]) if row and row[0] is not None else 0
                    if not {"source_vaults", "source_imports"}.issubset(tables):
                        result = {
                            "schema_version": version, "vaults": [], "items": [],
                        }
                    else:
                        vaults = [dict(row) for row in conn.execute(
                            "SELECT v.id, v.kind, v.root_digest, v.display_name, "
                            "v.workspace_id, v.repo_id, v.session_id, v.scope, "
                            "v.memory_type, v.importer_version, v.created_at, "
                            "v.updated_at, w.name AS workspace_name, "
                            "r.name AS repo_name FROM source_vaults v "
                            "JOIN workspaces w ON w.id=v.workspace_id "
                            "LEFT JOIN repos r ON r.id=v.repo_id ORDER BY v.id"
                        ).fetchall()]
                        items = [dict(row) for row in conn.execute(
                            "SELECT id, vault_id, source_key, relative_path, "
                            "memory_id, subject_key, content_sha256, "
                            "canonical_sha256, file_mtime_ns, file_size, "
                            "importer_version, last_seen_job_id, state, "
                            "first_imported_at, last_imported_at, last_seen_at, "
                            "missing_at, last_error FROM source_imports "
                            "ORDER BY vault_id, relative_path"
                        ).fetchall()]
                        result = {
                            "schema_version": version, "vaults": vaults,
                            "items": items,
                        }
                finally:
                    conn.close()

                try:
                    after = os.lstat(db_path)
                    after_handle = os.fstat(source_descriptor)
                    after_sidecars = sidecar_state()
                except OSError:
                    raise RuntimeError(
                        "import manifest database changed during snapshot"
                    ) from None
                if (
                    not same_version(opened, after_handle)
                    or not same_version(after_handle, after)
                    or before_sidecars != after_sidecars
                ):
                    raise RuntimeError(
                        "import manifest database changed during snapshot"
                    )
                return result
        finally:
            os.close(source_descriptor)

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

    def _prepare_source_manifest_v15(self, previous_version: int) -> bool:
        """Stage the v14 Obsidian-only manifest for a source-neutral rebuild.

        SQLite cannot widen a table ``CHECK`` constraint in place.  Keep the
        complete content-free manifest in temporary tables, drop children before
        parents, let :data:`SCHEMA_SQL` create the v15 shape, then restore it in
        the same migration transaction.  A failure rolls the primary database
        back to v14; the durable pre-migration backup remains the final fallback.
        """
        if previous_version >= 15:
            return False
        row = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='source_vaults'"
        ).fetchone()
        if row is None:
            return False
        definition = str(row["sql"] or "")
        if "'documents'" in definition:
            return False
        for name in (
            "_source_vaults_v15", "_source_imports_v15", "_source_import_items_v15",
        ):
            self.conn.execute(f"DROP TABLE IF EXISTS temp.{name}")
        self.conn.execute(
            "CREATE TEMP TABLE _source_vaults_v15 AS SELECT * FROM source_vaults"
        )
        self.conn.execute(
            "CREATE TEMP TABLE _source_imports_v15 AS SELECT * FROM source_imports"
        )
        self.conn.execute(
            "CREATE TEMP TABLE _source_import_items_v15 AS SELECT * FROM source_import_items"
        )
        self.conn.execute("DROP TABLE source_import_items")
        self.conn.execute("DROP TABLE source_imports")
        self.conn.execute("DROP TABLE source_vaults")
        return True

    def _restore_source_manifest_v15(self) -> None:
        """Restore source identities staged by :meth:`_prepare_source_manifest_v15`."""
        self.conn.execute(
            "INSERT INTO source_vaults SELECT * FROM temp._source_vaults_v15"
        )
        self.conn.execute(
            "INSERT INTO source_imports SELECT * FROM temp._source_imports_v15"
        )
        self.conn.execute(
            "INSERT INTO source_import_items("
            "id,job_id,source_id,relative_path,planned_action,result_state,"
            "warning_count,error_code,created_at,finished_at) "
            "SELECT id,job_id,source_id,relative_path,planned_action,result_state,"
            "warning_count,error_code,created_at,finished_at "
            "FROM temp._source_import_items_v15"
        )
        for name in (
            "_source_import_items_v15", "_source_imports_v15", "_source_vaults_v15",
        ):
            self.conn.execute(f"DROP TABLE temp.{name}")

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
        memory_columns: set[str] = set()
        if "memories" in object_names:
            memory_columns = {
                str(row["name"])
                for row in self.conn.execute("PRAGMA table_info(memories)").fetchall()
            }
        memories_need_modified_hlc = (
            "memories" in object_names and "modified_hlc" not in memory_columns
        )
        self._memories_need_modified_hlc = memories_need_modified_hlc
        session_columns: set[str] = set()
        if "sessions" in object_names:
            session_columns = {
                str(row["name"])
                for row in self.conn.execute("PRAGMA table_info(sessions)").fetchall()
            }
        sessions_need_handoff = "handoff" not in session_columns
        self._sessions_need_handoff = sessions_need_handoff
        tombstone_columns: set[str] = set()
        if "memory_tombstones" in object_names:
            tombstone_columns = {
                str(row["name"])
                for row in self.conn.execute(
                    "PRAGMA table_info(memory_tombstones)"
                ).fetchall()
            }
        tombstones_need_export_class = (
            "memory_tombstones" in object_names
            and "export_class" not in tombstone_columns
        )
        self._tombstones_need_export_class = tombstones_need_export_class
        sync_exports_need_table = (
            bool(object_names) and "memory_sync_exports" not in object_names
        )
        self._sync_exports_need_table = sync_exports_need_table
        if previous_version > SCHEMA_VERSION:
            raise RuntimeError(
                f"database schema {previous_version} is newer than supported "
                f"schema {SCHEMA_VERSION}"
            )
        needs_backup = bool(object_names) and (
            previous_version < SCHEMA_VERSION
            or mem_links_need_temporal_backfill
            or memories_need_modified_hlc
            or sessions_need_handoff
            or tombstones_need_export_class
            or sync_exports_need_table
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
            if self.conn.transaction_owned_by_current_thread():
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
        restore_source_manifest = self._prepare_source_manifest_v15(previous_version)
        self._execute_script_transactional(SCHEMA_SQL)
        if restore_source_manifest:
            self._restore_source_manifest_v15()
        self.has_fts5 = _fts5_available(self.conn)
        self.conn.execute(FTS_SQL_FTS5 if self.has_fts5 else FTS_SQL_FALLBACK)
        # Additive columns for DBs created before they existed — CREATE TABLE IF NOT
        # EXISTS above is a no-op on an already-existing table, so new columns need an
        # explicit, idempotent ALTER TABLE here (SQLite has no "ADD COLUMN IF NOT EXISTS").
        for stmt in (
            "ALTER TABLE memories ADD COLUMN sort_order REAL",
            "ALTER TABLE memories ADD COLUMN pinned_at REAL",
            "ALTER TABLE memories ADD COLUMN unpinned_at REAL",
            "ALTER TABLE memories ADD COLUMN confidence REAL NOT NULL DEFAULT 1.0",
            "ALTER TABLE memories ADD COLUMN subject_key TEXT DEFAULT ''",
            "ALTER TABLE memories ADD COLUMN claim_kind TEXT DEFAULT ''",
            "ALTER TABLE memories ADD COLUMN valid_to_recorded_at REAL",
            "ALTER TABLE memories ADD COLUMN modified_hlc TEXT NOT NULL DEFAULT ''",
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
            "ALTER TABLE memory_tombstones ADD COLUMN repo_id TEXT",
            "ALTER TABLE memory_tombstones ADD COLUMN export_class TEXT NOT NULL "
            "DEFAULT 'never_export' CHECK("
            "export_class IN ('never_export','remote_erasure'))",
            "ALTER TABLE sessions ADD COLUMN handoff TEXT DEFAULT '{}'",
            "ALTER TABLE source_import_items ADD COLUMN source_format TEXT NOT NULL "
            "DEFAULT '' CHECK(length(source_format)<=64 AND "
            "source_format NOT GLOB '*[^A-Za-z0-9_.+-]*')",
        ):
            try:
                self.conn.execute(stmt)
            except sqlite3.OperationalError:
                pass  # column already exists
        session_columns = {
            str(row["name"])
            for row in self.conn.execute("PRAGMA table_info(sessions)").fetchall()
        }
        if "handoff" not in session_columns:
            # Unlike the legacy additive loop above, do not swallow an arbitrary
            # OperationalError: this current-version shape repair is load-bearing.
            self.conn.execute(
                "ALTER TABLE sessions ADD COLUMN handoff TEXT DEFAULT '{}'"
            )
        memory_columns = {
            str(row["name"])
            for row in self.conn.execute("PRAGMA table_info(memories)").fetchall()
        }
        if "modified_hlc" not in memory_columns:
            self.conn.execute(
                "ALTER TABLE memories ADD COLUMN modified_hlc TEXT NOT NULL DEFAULT ''"
            )
        sync_export_table = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='memory_sync_exports'"
        ).fetchone()
        if sync_export_table is None:
            raise RuntimeError("memory_sync_exports table is missing after schema repair")
        sync_export_columns = {
            str(row["name"])
            for row in self.conn.execute(
                "PRAGMA table_info(memory_sync_exports)"
            ).fetchall()
        }
        if not {
            "memory_id", "workspace_id", "repo_id",
            "first_exported_at", "last_exported_at",
        }.issubset(sync_export_columns):
            raise RuntimeError("memory_sync_exports table has an incomplete schema")
        tombstone_columns = {
            str(row["name"])
            for row in self.conn.execute(
                "PRAGMA table_info(memory_tombstones)"
            ).fetchall()
        }
        if "export_class" not in tombstone_columns:
            self.conn.execute(
                "ALTER TABLE memory_tombstones ADD COLUMN export_class TEXT NOT NULL "
                "DEFAULT 'never_export' CHECK("
                "export_class IN ('never_export','remote_erasure'))"
            )
        if previous_version < 12:
            self.conn.execute(
                "UPDATE memory_tombstones SET export_class=?",
                (TOMBSTONE_NEVER_EXPORT,),
            )
        invalid_export_class = self.conn.execute(
            "SELECT export_class FROM memory_tombstones "
            "WHERE export_class NOT IN (?,?) LIMIT 1",
            (TOMBSTONE_NEVER_EXPORT, TOMBSTONE_REMOTE_ERASURE),
        ).fetchone()
        if invalid_export_class is not None:
            raise RuntimeError("memory tombstone export_class is invalid")
        tombstone_index_columns = [
            str(row["name"])
            for row in self.conn.execute(
                "PRAGMA index_info('idx_memory_tombstones_workspace')"
            ).fetchall()
        ]
        if tombstone_index_columns != ["workspace_id", "repo_id", "memory_id"]:
            self.conn.execute(
                "DROP INDEX IF EXISTS idx_memory_tombstones_workspace"
            )
            self.conn.execute(
                "CREATE INDEX idx_memory_tombstones_workspace "
                "ON memory_tombstones(workspace_id, repo_id, memory_id)"
            )
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
        if previous_version < 8:
            # v7 memories predate first-class confidence. ``confidence`` is a
            # scoring multiplier with a 1.0 default, so existing rows need no
            # backfill — the NOT NULL DEFAULT 1.0 column already covers them
            # (the additive ALTER above is one-shot on reopens).
            # v7 pin state has no clock. Synthesize earliest-wins markers so a
            # legacy pinned row still participates in the new pin lattice: a pinned
            # row without ``pinned_at`` is treated as pinned since the epoch (it
            # can never be beaten by a peer's unpin, which matches the old
            # OR-semantics), and a legacy unpinned row carries no marker at all
            # (a peer's pin simply applies). Rows with real clocks are untouched.
            self.conn.execute(
                "UPDATE memories SET pinned_at=0.0 "
                "WHERE pinned=1 AND pinned_at IS NULL"
            )
        if previous_version < 10:
            # v9 and earlier compounded the already-grown stability by a larger
            # multiplier on every reinforcement. Repair unsafe values and establish
            # the same finite domain used by live scoring and sync.
            self.conn.execute(
                "UPDATE memories SET stability=CASE "
                "WHEN stability IS NULL OR typeof(stability) NOT IN ('integer','real') "
                "OR stability<=0 THEN ? "
                "WHEN stability<? THEN ? "
                "WHEN stability>? THEN ? "
                "ELSE stability END, "
                "access_count=CASE "
                "WHEN access_count IS NULL OR typeof(access_count)!='integer' "
                "OR access_count<0 THEN 0 "
                "WHEN access_count>? THEN ? "
                "ELSE access_count END",
                (
                    DEFAULT_STABILITY_DAYS,
                    MIN_STABILITY_DAYS, MIN_STABILITY_DAYS,
                    MAX_STABILITY_DAYS, MAX_STABILITY_DAYS,
                    MAX_ACCESS_COUNT, MAX_ACCESS_COUNT,
                ),
            )
        if previous_version < 11:
            # v10 made prompt approval and backend version markers authoritative but
            # did not classify rows written under the preceding contracts. Preserve
            # explicit legacy trust, recover the exact local-agent downgrade emitted
            # by the pre-1.4.5 service gate, and force one verified vector rebuild.
            self._migrate_prompt_review_state_v11()
            if self.conn.execute(
                "SELECT 1 FROM mem_vectors LIMIT 1"
            ).fetchone() is not None:
                self.conn.execute(
                    "INSERT OR REPLACE INTO embedding_state(identity, version, updated_at) "
                    "VALUES (?,?,?)",
                    ("__active__", "legacy-unverified", now_ts()),
                )
            self.conn.execute(
                "DELETE FROM embedding_state WHERE identity='__rebuilding__'"
            )
        # Schema 11 was still pre-release when model-derived consolidation stopped
        # inheriting source approval. Databases already opened by an earlier v11 build
        # have no version transition left to trigger the backfill, so use one durable
        # transactional marker to repair them exactly once. Pre-v11 upgrades were fully
        # classified above and only need the marker written.
        self._ensure_llm_consolidation_trust_repair_v11(
            scan_legacy=previous_version >= 11,
        )
        # Earlier v11/v12 builds let model-extracted facts inherit ingress approval.
        # Repair both version transitions and already-opened same-schema databases once.
        self._ensure_llm_extraction_trust_repair_v12()
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
        # backfill only when the database crosses the migration that introduced the
        # canonical fields. Running the all-pairs token pass on every fresh/opened
        # database turns startup into an O(n²) scan of the entire entity table.
        if previous_version < 4:
            self._backfill_entity_canonicalization()
        elif previous_version < 9:
            # v8 databases may have canonical fields but never received the token
            # overlap pass; v9 is the one-time repair for that gap.
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

    def _migrate_prompt_review_state_v11(self) -> None:
        """Classify memories created before explicit prompt review existed.

        A trusted deterministic row was prompt-visible under the old contract, so adding
        the equivalent approval stamp preserves upgrade behavior rather than granting a
        new capability. Model-authored consolidation is the exception: valid source IDs
        prove lineage, not entailment, so those rows become reviewable pending records and
        any materialized graph derivatives are retired. The second approved shape is the
        exact local-agent downgrade emitted by the short-lived service gate before local
        agent writes were restored. Everything else is labelled pending and remains
        outside prompt context.
        """
        rows = self.conn.execute(
            "SELECT id, content, metadata, provenance FROM memories ORDER BY id"
        ).fetchall()
        counts = {"approved": 0, "agent_recovered": 0, "pending": 0,
                  "llm_pending": 0}
        for row in rows:
            metadata = _loads(row["metadata"], {})
            metadata = metadata if isinstance(metadata, dict) else {}
            dedicated = _loads(row["provenance"], {})
            dedicated = dedicated if isinstance(dedicated, dict) else {}
            nested = metadata.get("provenance")
            nested = dict(nested) if isinstance(nested, dict) else {}
            dedicated_restrictive = bool(
                dedicated.get("trusted") is False
                or (
                    "review_state" in dedicated
                    and dedicated.get("review_state") != REVIEW_APPROVED
                )
                or dedicated.get("quarantined") is True
            )
            nested_restrictive = bool(
                nested.get("trusted") is False
                or (
                    "review_state" in nested
                    and nested.get("review_state") != REVIEW_APPROVED
                )
                or nested.get("quarantined") is True
            )
            # Contradictory legacy envelopes resolve to the stricter assertion so
            # migration cannot turn a nested distrust marker into prompt approval.
            provenance = _merge_provenance_envelopes(dedicated, nested)
            review_state = str(provenance.get("review_state") or "").strip().casefold()
            quarantine = metadata.get("quarantine")
            quarantined = bool(
                provenance.get("quarantined") is True
                or isinstance(quarantine, dict)
                and quarantine.get("state") == "quarantined"
            )
            legacy_agent_gate = bool(
                review_state == "pending"
                and provenance.get("trusted") is False
                and str(provenance.get("source") or "").strip().casefold()
                in {"agent", "intent_api"}
                and provenance.get("trust_origin") == "service_review_gate"
                and provenance.get("trust_downgraded") is True
            )
            legacy_llm_kind = llm_consolidation_kind(provenance, row["content"])
            basis = ""
            if legacy_llm_kind is not None:
                # A valid source ID establishes lineage, not entailment.  Historical
                # structured facts and optional prose summaries were model-authored but
                # predated that explicit marker, so never auto-approve them during the
                # review-state upgrade.  Retire graph/code derivatives while preserving
                # the source links an owner needs for governed review.
                provenance, metadata, _ = pending_llm_consolidation_envelope(
                    provenance, metadata, row["content"],
                )
                self.retire_memory_graph_state(
                    row["id"],
                    preserve_link_relations=("consolidates", "profiles"),
                    commit=False,
                )
                provenance["derived_graph_inert"] = True
                review_state = REVIEW_PENDING
                basis = "legacy_llm_consolidation"
                counts["pending"] += 1
                counts["llm_pending"] += 1
            elif not quarantined and nested_restrictive and not dedicated_restrictive:
                # A nested distrust marker is a stricter legacy assertion than
                # a contradictory dedicated approval; never recover it implicitly.
                provenance["trusted"] = False
                review_state = REVIEW_PENDING
                basis = "legacy_unreviewed"
                counts["pending"] += 1
            elif not quarantined and not review_state and provenance.get("trusted") is True:
                review_state = "approved"
                basis = "legacy_explicit_trust"
                counts["approved"] += 1
            elif not quarantined and legacy_agent_gate:
                provenance["trusted"] = True
                review_state = "approved"
                basis = "legacy_local_agent_gate"
                counts["approved"] += 1
                counts["agent_recovered"] += 1
                provenance["trust_origin"] = "legacy_local_agent_upgrade"
                provenance["trust_recovered"] = True
            elif not review_state:
                provenance["trusted"] = False
                review_state = "pending"
                basis = "legacy_unreviewed"
                counts["pending"] += 1
                provenance.setdefault("trust_origin", "legacy_review_upgrade")
            else:
                continue

            provenance["review_state"] = review_state
            provenance["review_basis"] = basis
            provenance["review_policy_version"] = 11
            metadata["provenance"] = dict(provenance)
            self.advance_memory_modified_hlc(row["id"], commit=False)
            self.conn.execute(
                "UPDATE memories SET provenance=?, metadata=? WHERE id=?",
                (_dumps(provenance), _dumps(metadata), row["id"]),
            )
            self.audit(
                "schema_migration",
                "prompt_review_backfill",
                row["id"],
                f"schema=11; state={review_state}; basis={basis}",
                commit=False,
            )
        if rows:
            self.audit(
                "schema_migration",
                "prompt_review_backfill_summary",
                "schema_v11",
                "approved=%d; agent_recovered=%d; pending=%d; llm_pending=%d"
                % (counts["approved"], counts["agent_recovered"], counts["pending"],
                   counts["llm_pending"]),
                commit=False,
            )

    def _ensure_llm_consolidation_trust_repair_v11(
        self, *, scan_legacy: bool,
    ) -> None:
        """Repair same-schema v11 LLM output once, then atomically mark completion.

        The outer ``init_schema`` transaction owns both graph retirement and this local
        state marker. Any exception therefore rolls back the entire scan and leaves no
        marker, so the next open retries from a coherent pre-repair state. New databases
        and pre-v11 upgrades already ran the full review-state migration and only write
        the marker; an older v11 database performs the compatibility scan first.
        """
        marker = self.conn.execute(
            "SELECT value FROM sync_state WHERE key=?",
            (_LLM_CONSOLIDATION_REPAIR_STATE_KEY,),
        ).fetchone()
        if (
            marker is not None
            and marker["value"] == _LLM_CONSOLIDATION_REPAIR_STATE_VALUE
        ):
            return

        if scan_legacy:
            rows = self.conn.execute(
                "SELECT id, content, metadata, provenance FROM memories ORDER BY id"
            ).fetchall()
            for row in rows:
                metadata = _loads(row["metadata"], {})
                metadata = metadata if isinstance(metadata, dict) else {}
                dedicated = _loads(row["provenance"], {})
                dedicated = dedicated if isinstance(dedicated, dict) else {}
                nested = metadata.get("provenance")
                nested = dict(nested) if isinstance(nested, dict) else {}
                provenance = _merge_provenance_envelopes(dedicated, nested)
                kind = llm_consolidation_kind(provenance, row["content"])
                if kind is None:
                    continue

                provenance, metadata, _ = pending_llm_consolidation_envelope(
                    provenance, metadata, row["content"],
                )
                self.retire_memory_graph_state(
                    row["id"],
                    preserve_link_relations=("consolidates", "profiles"),
                    commit=False,
                )
                provenance["derived_graph_inert"] = True
                provenance["review_basis"] = "legacy_llm_consolidation"
                provenance["review_policy_version"] = 11
                metadata["provenance"] = dict(provenance)
                self.advance_memory_modified_hlc(row["id"], commit=False)
                self.conn.execute(
                    "UPDATE memories SET provenance=?, metadata=? WHERE id=?",
                    (_dumps(provenance), _dumps(metadata), row["id"]),
                )
                self.audit(
                    "schema_migration",
                    "llm_consolidation_trust_repair",
                    row["id"],
                    f"schema=11; state={REVIEW_PENDING}; kind={kind}",
                    commit=False,
                )

        # ``sync_state`` is local-only bookkeeping and never enters user audit or sync
        # bundles. This completion marker must remain the final repair write; deferring
        # its commit to ``init_schema`` keeps it atomic with every graph/provenance edit.
        self.set_sync_state(
            _LLM_CONSOLIDATION_REPAIR_STATE_KEY,
            _LLM_CONSOLIDATION_REPAIR_STATE_VALUE,
            commit=False,
        )

    def _ensure_llm_extraction_trust_repair_v12(self) -> None:
        """Demote legacy model-extracted facts and retire their derived graph state."""
        marker = self.conn.execute(
            "SELECT value FROM sync_state WHERE key=?",
            (_LLM_EXTRACTION_REPAIR_STATE_KEY,),
        ).fetchone()
        if (
            marker is not None
            and marker["value"] == _LLM_EXTRACTION_REPAIR_STATE_VALUE
        ):
            return

        rows = self.conn.execute(
            "SELECT id, metadata, provenance FROM memories ORDER BY id"
        ).fetchall()
        repaired = 0
        for row in rows:
            metadata = _loads(row["metadata"], {})
            metadata = metadata if isinstance(metadata, dict) else {}
            if not isinstance(metadata.get("llm_extraction"), dict):
                continue
            dedicated = _loads(row["provenance"], {})
            dedicated = dedicated if isinstance(dedicated, dict) else {}
            nested = metadata.get("provenance")
            nested = dict(nested) if isinstance(nested, dict) else {}
            provenance = _merge_provenance_envelopes(dedicated, nested)
            provenance, metadata, detected = pending_llm_extraction_envelope(
                provenance, metadata,
            )
            if not detected:
                continue
            self.retire_memory_graph_state(row["id"], commit=False)
            provenance["review_basis"] = "legacy_llm_extraction"
            provenance["review_policy_version"] = 12
            metadata["provenance"] = dict(provenance)
            self.advance_memory_modified_hlc(row["id"], commit=False)
            self.conn.execute(
                "UPDATE memories SET provenance=?, metadata=? WHERE id=?",
                (_dumps(provenance), _dumps(metadata), row["id"]),
            )
            self.audit(
                "schema_migration",
                "llm_extraction_trust_repair",
                row["id"],
                f"schema=12; state={REVIEW_PENDING}",
                commit=False,
            )
            repaired += 1

        if repaired:
            self.audit(
                "schema_migration",
                "llm_extraction_trust_repair_summary",
                "schema_v12",
                f"pending={repaired}",
                commit=False,
            )
        self.set_sync_state(
            _LLM_EXTRACTION_REPAIR_STATE_KEY,
            _LLM_EXTRACTION_REPAIR_STATE_VALUE,
            commit=False,
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
                self.advance_memory_modified_hlc(row["id"], commit=False)
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

    def _entity_blocking_candidates(self, *, entity_id: Optional[str],
                                     workspace_id: Optional[str],
                                     etype: Optional[str], name: Any) -> list[sqlite3.Row]:
        """Select lexical peers without making one unbounded SQL expression.
        Ordinary token blocks return every matching peer; unusually broad blocks are
        deliberately discarded rather than materialized. The compact-alias query always
        runs. The Python score below then applies the exact compact/Jaccard rule.
        Matching both normalized_name and the legacy name column lets a partially
        upgraded database participate before its next migration completes.
        """
        tokens = sorted(_entity_token_set(name))
        if not tokens:
            return []
        base_sql = (
            "SELECT id, workspace_id, repo_id, name, etype, canonical_id, "
            "normalized_name, canonical_method, canonical_confidence "
            "FROM entities WHERE workspace_id IS ? AND etype IS ? AND ("
        )
        found: dict[str, sqlite3.Row] = {}

        def collect(clauses: list[str], patterns: list[str], *,
                    guard_broad: bool) -> None:
            params: list[Any] = [workspace_id, etype, *patterns]
            sql = base_sql + " OR ".join(clauses) + ")"
            if entity_id is not None:
                sql += " AND id<>?"
                params.append(entity_id)
            if guard_broad:
                sql += " LIMIT ?"
                params.append(ENTITY_BLOCK_BUCKET_LIMIT + 1)
            rows = self.conn.execute(sql, params).fetchall()
            if guard_broad and len(rows) > ENTITY_BLOCK_BUCKET_LIMIT:
                # A common token is not useful as a blocking key. Do not retain
                # an arbitrarily large bucket; the exact compact query still runs.
                return
            for row in rows:
                found[str(row["id"])] = row

        for start in range(0, len(tokens), ENTITY_BLOCK_TOKEN_CHUNK):
            clauses: list[str] = []
            patterns: list[str] = []
            for token in tokens[start:start + ENTITY_BLOCK_TOKEN_CHUNK]:
                pattern = "%" + _escape_like(token) + "%"
                clauses.append(
                    "(normalized_name LIKE ? ESCAPE '\\' OR lower(name) LIKE ? ESCAPE '\\')"
                )
                patterns.extend((pattern, pattern))
            collect(clauses, patterns, guard_broad=True)

        # Whitespace-separated aliases such as OpenAI/Open AI have no shared token,
        # but their compact spellings are still an exact canonical match.
        compact = _entity_compact_name(name)
        if compact:
            compact_pattern = "%" + _escape_like(compact) + "%"
            collect(
                [
                    "(replace(lower(normalized_name), ' ', '') LIKE ? ESCAPE '\\' "
                    "OR replace(lower(name), ' ', '') LIKE ? ESCAPE '\\')"
                ],
                [compact_pattern, compact_pattern], guard_broad=False,
            )
        return [found[key] for key in sorted(found)]

    def _backfill_entity_canonicalization(self) -> None:
        rows = [dict(row) for row in self.conn.execute(
            "SELECT id, workspace_id, name, etype, canonical_id, normalized_name, "
            "canonical_method, canonical_confidence FROM entities "
            "ORDER BY workspace_id, etype, id"
        ).fetchall()]
        # Close canonical chains to their root FIRST. A legacy database can carry a
        # two-hop chain (A→B, B→C) when an earlier pass merged B into C after A had
        # already pointed at B; the group pass below keeps "any existing canonical
        # wins", so A would otherwise dangle at B while B points at C. Resolve every
        # id to its transitive root (an id whose canonical is itself, or a
        # non-existent id — caller-provided roots are authoritative) and persist one
        # hop, so the group pass and the singleton-reset logic below see roots only.
        # Deterministic and idempotent.
        root_of: dict[str, str] = {row["id"]: row["id"] for row in rows}
        for row in rows:
            cid = str(row.get("canonical_id") or "")
            if cid:
                root_of[row["id"]] = cid
        for mid in root_of:
            seen: set[str] = set()
            cursor = root_of[mid]
            while cursor in root_of and root_of[cursor] != cursor:
                if cursor in seen:  # cycle safety (should not happen)
                    break
                seen.add(cursor)
                cursor = root_of[cursor]
            root_of[mid] = cursor
        for row in rows:
            root = root_of.get(row["id"])
            cid = str(row.get("canonical_id") or "")
            if cid and root and root != cid:
                self.conn.execute(
                    "UPDATE entities SET canonical_id=? WHERE id=?",
                    (root, row["id"]),
                )
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

        # Token-overlap blocking is deliberately query-backed rather than an in-memory
        # all-pairs pass.  It is still a one-time migration transform, but a workspace
        # with many unrelated entities should not turn an upgrade into quadratic work.
        rows = [dict(row) for row in self.conn.execute(
            "SELECT id, workspace_id, repo_id, name, etype, canonical_id, normalized_name, "
            "canonical_method, canonical_confidence FROM entities "
            "ORDER BY workspace_id, etype, id"
        ).fetchall()]
        row_by_id = {str(row["id"]): row for row in rows}
        seen_pairs: set[tuple[str, str]] = set()
        for row in rows:
            if not _entity_token_set(row.get("name")):
                continue
            candidates = self._entity_blocking_candidates(
                entity_id=row["id"], workspace_id=row.get("workspace_id"),
                etype=row.get("etype"), name=row.get("name"),
            )
            for candidate in candidates:
                other = dict(candidate)
                row_id, other_id = str(row["id"]), str(other["id"])
                pair = (row_id, other_id) if row_id <= other_id else (other_id, row_id)
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                overlap = _entity_overlap(row.get("name"), other.get("name"))
                if overlap is None or overlap < 0.6:
                    continue
                # Existing canonical ids win when either side has one; otherwise the
                # lexicographically oldest typed id is deterministic.
                other_state = row_by_id.get(str(other["id"]))
                if other_state is not None:
                    other["canonical_id"] = other_state.get("canonical_id")
                    other["canonical_method"] = other_state.get("canonical_method")
                existing = sorted({
                    str(row.get("canonical_id") or ""),
                    str(other.get("canonical_id") or ""),
                })
                existing = [value for value in existing if value]
                canonical = existing[0] if existing else min(pair)
                for member in (row, other):
                    state = row_by_id.get(str(member["id"]), member)
                    if state.get("canonical_id") != canonical or \
                            state.get("canonical_method") != "token_overlap":
                        self.conn.execute(
                            "UPDATE entities SET canonical_id=?, canonical_method=? "
                            "WHERE id=?",
                            (canonical, "token_overlap", member["id"]),
                        )
                    state["canonical_id"] = canonical
                    state["canonical_method"] = "token_overlap"
                    member["canonical_id"] = canonical
                    member["canonical_method"] = "token_overlap"

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
        with self._close_lock:
            finalizer = getattr(self, "_connection_finalizer", None)
            if finalizer is None:
                self.conn.close()
                return
            if not finalizer.alive:
                return
            # Explicit shutdown retains the historical error contract. Detach only after
            # close succeeds so a failed close still gets one best-effort finalizer attempt.
            self.conn.close()
            finalizer.detach()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    @contextmanager
    def _write_operation(self, name: str, *, commit: bool):
        """Isolate one compound write without settling a caller-owned transaction."""
        owns_transaction = not self.conn.transaction_owned_by_current_thread()
        savepoint = ""
        try:
            if owns_transaction:
                self.conn.execute("BEGIN IMMEDIATE")
            else:
                savepoint = (
                    f"engraphis_{name}_{threading.get_ident()}_{time.monotonic_ns()}"
                )
                self.conn.execute(f"SAVEPOINT {savepoint}")
            yield
            if savepoint:
                self.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            elif commit:
                self.conn.commit()
        except BaseException:
            if owns_transaction:
                if self.conn.transaction_owned_by_current_thread():
                    self.conn.rollback()
            elif savepoint and self.conn.transaction_owned_by_current_thread():
                self.conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                self.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise

    # ── local source-import manifest ─────────────────────────────────────────
    def _authorize_source_workspace_id(self, workspace_id: str) -> str:
        row = self.conn.execute(
            "SELECT name FROM workspaces WHERE id=?", (str(workspace_id),)
        ).fetchone()
        if row is None:
            raise ValueError("source import workspace was not found")
        self._authorize_workspace(str(row["name"]))
        return str(workspace_id)

    def _source_vault_row(self, vault_id: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM source_vaults WHERE id=?", (str(vault_id),)
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        self._authorize_source_workspace_id(str(result["workspace_id"]))
        return result

    def _authorize_source_job(self, job_id: str) -> dict:
        row = self.conn.execute(
            "SELECT id, workspace_id, repo_id, kind FROM jobs WHERE id=?",
            (str(job_id),),
        ).fetchone()
        if row is None or str(row["kind"]) not in {
            "document_import", "obsidian_import",
        }:
            raise ValueError("source import job was not found")
        result = dict(row)
        self._authorize_source_workspace_id(str(result["workspace_id"]))
        return result

    def get_source_vault(self, vault_id: str) -> Optional[dict]:
        return self._source_vault_row(vault_id)

    def get_source_vault_by_root_digest(self, *, kind: str, root_digest: str,
                                        workspace_id: str, repo_id: Optional[str] = None,
                                        session_id: Optional[str] = None) -> Optional[dict]:
        self._authorize_source_workspace_id(workspace_id)
        row = self.conn.execute(
            "SELECT * FROM source_vaults WHERE kind=? AND root_digest=? AND workspace_id=? "
            "AND repo_id IS ? AND session_id IS ?",
            (kind, root_digest, workspace_id, repo_id, session_id),
        ).fetchone()
        return dict(row) if row is not None else None

    def list_source_vaults(self, *, workspace_id: Optional[str] = None,
                           kind: Optional[str] = None, limit: int = 100) -> list[dict]:
        clauses, params = [], []
        if workspace_id is not None:
            self._authorize_source_workspace_id(workspace_id)
            clauses.append("workspace_id=?")
            params.append(workspace_id)
        if self.allowed_workspaces is not None:
            names = sorted(str(name) for name in self.allowed_workspaces)
            clauses.append(
                "workspace_id IN (SELECT id FROM workspaces WHERE name IN ("
                + ",".join("?" for _ in names) + "))"
            )
            params.extend(names)
        if kind is not None:
            clauses.append("kind=?")
            params.append(kind)
        sql = "SELECT * FROM source_vaults"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY updated_at DESC, id LIMIT ?"
        params.append(max(1, min(10_000, int(limit))))
        return [dict(row) for row in self.conn.execute(sql, params).fetchall()]

    def register_source_vault(self, *, kind: str, root_digest: str, workspace_id: str,
                              repo_id: Optional[str] = None, session_id: Optional[str] = None,
                              display_name: str = "", scope: str = "workspace",
                              memory_type: str = "semantic", importer_version: str = "",
                              commit: bool = True) -> str:
        """Create or refresh a local source-vault identity without storing its root."""
        kind, root_digest = str(kind).strip(), str(root_digest).strip().casefold()
        if kind not in {"documents", "obsidian"} or _SOURCE_DIGEST_RE.fullmatch(root_digest) is None:
            raise ValueError(
                "source collection requires kind='documents' or 'obsidian' and a root digest"
            )
        self._authorize_source_workspace_id(workspace_id)
        try:
            selected_scope = Scope(str(scope))
        except ValueError as exc:
            raise ValueError("source vault scope must be workspace, repo, or session") from exc
        try:
            selected_memory_type = MemoryType(str(memory_type))
        except ValueError as exc:
            raise ValueError("source vault memory_type is invalid") from exc
        if selected_scope == Scope.WORKSPACE and (repo_id is not None or session_id is not None):
            raise ValueError("workspace source vault scope cannot include a repo or session")
        if selected_scope == Scope.REPO and (repo_id is None or session_id is not None):
            raise ValueError("repo source vault scope requires repo_id and no session_id")
        if repo_id is not None:
            repo = self.conn.execute(
                "SELECT workspace_id FROM repos WHERE id=?", (repo_id,)
            ).fetchone()
            if repo is None or str(repo["workspace_id"]) != str(workspace_id):
                raise ValueError("repo_id does not belong to the source vault workspace")
        if selected_scope == Scope.SESSION:
            if session_id is None:
                raise ValueError("session source vault scope requires session_id")
            session = self.get_session(session_id)
            if session is None or session["workspace_id"] != workspace_id:
                raise ValueError("session_id does not belong to the source vault workspace")
            if repo_id is not None and session.get("repo_id") != repo_id:
                raise ValueError("session_id does not belong to the source vault repo")
            repo_id = repo_id or session.get("repo_id")
        safe_display_name = str(display_name or "")[:200]
        safe_importer_version = str(importer_version or "")[:64]
        stamp = now_ts()
        with self._write_operation("source_vault", commit=commit):
            existing = self.get_source_vault_by_root_digest(
                kind=kind, root_digest=root_digest, workspace_id=workspace_id,
                repo_id=repo_id, session_id=session_id,
            )
            if existing is not None:
                self.conn.execute(
                    "UPDATE source_vaults SET display_name=?, scope=?, memory_type=?, "
                    "importer_version=?, updated_at=? WHERE id=?",
                    (safe_display_name, selected_scope.value, selected_memory_type.value,
                     safe_importer_version, stamp, existing["id"]),
                )
                return str(existing["id"])
            vault_id = ids.new_id("vault")
            self.conn.execute(
                "INSERT INTO source_vaults(id, kind, root_digest, display_name, workspace_id, "
                "repo_id, session_id, scope, memory_type, importer_version, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (vault_id, kind, root_digest, safe_display_name, workspace_id,
                 repo_id, session_id, selected_scope.value, selected_memory_type.value,
                 safe_importer_version, stamp, stamp),
            )
            return vault_id

    def get_source_import_item(self, *, vault_id: str, source_key: str) -> Optional[dict]:
        if self._source_vault_row(vault_id) is None:
            return None
        row = self.conn.execute(
            "SELECT * FROM source_imports WHERE vault_id=? AND source_key=?",
            (vault_id, source_key),
        ).fetchone()
        return dict(row) if row is not None else None

    def list_source_import_items(self, *, vault_id: str, states: Optional[list[str]] = None,
                                 limit: int = 10_000) -> list[dict]:
        if self._source_vault_row(vault_id) is None:
            return []
        params: list[Any] = [vault_id]
        sql = "SELECT * FROM source_imports WHERE vault_id=?"
        if states is not None:
            if not states:
                return []
            sql += " AND state IN (" + ",".join("?" for _ in states) + ")"
            params.extend(str(state) for state in states)
        sql += " ORDER BY relative_path LIMIT ?"
        params.append(max(1, min(100_000, int(limit))))
        return [dict(row) for row in self.conn.execute(sql, params).fetchall()]

    def upsert_source_import_item(self, *, vault_id: str, source_key: str, relative_path: str,
                                  source_id: Optional[str] = None,
                                  memory_id: Optional[str] = None, subject_key: str = "",
                                  content_sha256: str = "", canonical_sha256: str = "",
                                  file_size: int = 0,
                                  file_mtime_ns: Optional[int] = None,
                                  importer_version: str = "", state: str = "imported",
                                  import_id: Optional[str] = None,
                                  last_error: str = "", seen_at: Optional[float] = None,
                                  commit: bool = True) -> str:
        """Atomically create/update one source identity; callers may join their memory write."""
        if self._source_vault_row(vault_id) is None:
            raise ValueError("source vault was not found")
        try:
            relative_path = normalize_document_path(relative_path)
        except ValueError as exc:
            raise ValueError(
                "source import item requires a safe relative path and source key"
            ) from exc
        source_key = str(source_key).strip().casefold()
        if _SOURCE_DIGEST_RE.fullmatch(source_key) is None:
            raise ValueError("source import item requires a safe relative path and source key")
        for label, digest in (
            ("content_sha256", content_sha256),
            ("canonical_sha256", canonical_sha256),
        ):
            value = str(digest or "").strip().casefold()
            if value and _SOURCE_DIGEST_RE.fullmatch(value) is None:
                raise ValueError(f"source import item {label} is invalid")
        content_sha256 = str(content_sha256 or "").strip().casefold()
        canonical_sha256 = str(canonical_sha256 or "").strip().casefold()
        if state not in {"imported", "unchanged", "skipped", "rejected", "error", "conflict", "missing", "renamed"}:
            raise ValueError("invalid source import item state")
        stamp = now_ts() if seen_at is None else float(seen_at)
        with self._write_operation("source_item", commit=commit):
            existing = self.get_source_import_item(vault_id=vault_id, source_key=source_key)
            item_id = (
                str(existing["id"])
                if existing is not None
                else str(source_id or ids.new_id("source"))
            )
            if not item_id.startswith("src_"):
                raise ValueError("source import item requires a typed source id")
            self.conn.execute(
                "INSERT INTO source_imports(id, vault_id, source_key, relative_path, memory_id, "
                "subject_key, content_sha256, canonical_sha256, file_size, file_mtime_ns, importer_version, "
                "last_seen_job_id, state, first_imported_at, last_imported_at, last_seen_at, "
                "missing_at, last_error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?) "
                "ON CONFLICT(vault_id, source_key) DO UPDATE SET relative_path=excluded.relative_path, "
                "memory_id=COALESCE(excluded.memory_id, source_imports.memory_id), "
                "subject_key=excluded.subject_key, content_sha256=excluded.content_sha256, "
                "canonical_sha256=excluded.canonical_sha256, file_size=excluded.file_size, "
                "file_mtime_ns=excluded.file_mtime_ns, "
                "importer_version=excluded.importer_version, "
                "last_seen_job_id=excluded.last_seen_job_id, state=excluded.state, "
                "last_imported_at=excluded.last_imported_at, last_seen_at=excluded.last_seen_at, "
                "missing_at=NULL, last_error=excluded.last_error",
                (item_id, vault_id, source_key, relative_path, memory_id,
                 str(subject_key or ""), content_sha256,
                 canonical_sha256, max(0, int(file_size)), file_mtime_ns,
                 str(importer_version or "")[:64], import_id, state, stamp, stamp, stamp,
                 _content_free_source_error(last_error)),
            )
            return item_id

    def rename_source_import_item(self, *, vault_id: str, source_key: str,
                                  relative_path: str, commit: bool = True) -> bool:
        if self._source_vault_row(vault_id) is None:
            return False
        try:
            relative_path = normalize_document_path(relative_path)
        except ValueError as exc:
            raise ValueError("source import item requires a safe relative path") from exc
        with self._write_operation("source_rename", commit=commit):
            return bool(self.conn.execute(
                "UPDATE source_imports SET relative_path=?, state='renamed', last_seen_at=?, "
                "missing_at=NULL WHERE vault_id=? AND source_key=?",
                (relative_path, now_ts(), vault_id, source_key),
            ).rowcount)

    def mark_source_import_items_missing(self, *, vault_id: str, seen_before: float,
                                         commit: bool = True) -> int:
        if self._source_vault_row(vault_id) is None:
            return 0
        with self._write_operation("source_missing", commit=commit):
            return int(self.conn.execute(
                "UPDATE source_imports SET state='missing', missing_at=? WHERE vault_id=? "
                "AND (last_seen_at IS NULL OR last_seen_at<?) "
                "AND state NOT IN ('missing','conflict')",
                (now_ts(), vault_id, float(seen_before)),
            ).rowcount)

    def get_source_import(self, import_id: str) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM source_imports WHERE id=?", (import_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        if self._source_vault_row(str(result["vault_id"])) is None:
            return None
        return result

    def list_source_imports(self, *, vault_id: str, limit: int = 100) -> list[dict]:
        if self._source_vault_row(vault_id) is None:
            return []
        return [dict(row) for row in self.conn.execute(
            "SELECT * FROM source_imports WHERE vault_id=? ORDER BY last_seen_at DESC, id DESC LIMIT ?",
            (vault_id, max(1, min(10_000, int(limit)))),
        ).fetchall()]

    def record_source_import_job_item(self, *, job_id: str,
                                      relative_path: str, planned_action: str,
                                      result_state: str = "pending",
                                      source_id: Optional[str] = None,
                                      source_format: str = "",
                                      warning_count: int = 0, error_code: str = "",
                                      commit: bool = True) -> str:
        """Upsert one content-free per-job plan/result row."""
        self._authorize_source_job(job_id)
        try:
            relative_path = normalize_document_path(relative_path)
        except ValueError as exc:
            raise ValueError("source import job item requires a safe relative path") from exc
        planned = str(planned_action)
        result = str(result_state)
        source_format = str(source_format or "").strip()
        if len(source_format) > 64 or re.fullmatch(r"[A-Za-z0-9_.+-]*", source_format) is None:
            raise ValueError("invalid source import format")
        if planned not in {"imported", "updated", "skipped", "rejected", "renamed", "missing", "conflict"}:
            raise ValueError("invalid planned source import action")
        if result not in {"pending", "imported", "updated", "skipped", "rejected", "renamed", "missing", "conflict", "error", "warning"}:
            raise ValueError("invalid source import result")
        stamp = now_ts()
        with self._write_operation("source_job_item", commit=commit):
            row = self.conn.execute(
                "SELECT id FROM source_import_items WHERE job_id=? AND relative_path=? "
                "AND planned_action=?",
                (job_id, relative_path, planned),
            ).fetchone()
            item_id = str(row["id"]) if row is not None else ids.new_id("source")
            self.conn.execute(
                "INSERT INTO source_import_items(id, job_id, source_id, relative_path, "
                "source_format, planned_action, result_state, warning_count, error_code, "
                "created_at, finished_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(job_id, relative_path, planned_action) "
                "DO UPDATE SET source_id=COALESCE(excluded.source_id, source_import_items.source_id), "
                "source_format=excluded.source_format, result_state=excluded.result_state, "
                "warning_count=excluded.warning_count, "
                "error_code=excluded.error_code, finished_at=excluded.finished_at",
                (item_id, job_id, source_id, relative_path, source_format, planned, result,
                 max(0, int(warning_count)), str(error_code or "")[:100], stamp,
                 stamp if result != "pending" else None),
            )
            return item_id

    def list_source_import_job_items(self, *, job_id: str, limit: int = 100_000) -> list[dict]:
        self._authorize_source_job(job_id)
        return [dict(row) for row in self.conn.execute(
            "SELECT * FROM source_import_items WHERE job_id=? ORDER BY relative_path, id LIMIT ?",
            (job_id, max(1, min(100_000, int(limit)))),
        ).fetchall()]

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

    def get_or_create_workspace(
        self, name: str, *, settings: Optional[dict] = None,
    ) -> str:
        """Atomically return/create a workspace; the winning creator's settings persist."""
        self._authorize_workspace(name)
        row = self.conn.execute(
            "SELECT id FROM workspaces WHERE name=?", (name,)
        ).fetchone()
        if row is not None:
            return str(row["id"])
        owns_transaction = not self.conn.transaction_owned_by_current_thread()
        try:
            if owns_transaction:
                self.conn.execute("BEGIN IMMEDIATE")
            candidate = ids.new_id("workspace")
            self.conn.execute(
                "INSERT INTO workspaces(id, name, created_at, settings) VALUES (?,?,?,?) "
                "ON CONFLICT(name) DO NOTHING",
                (candidate, name, now_ts(), _dumps(settings or {})),
            )
            row = self.conn.execute(
                "SELECT id FROM workspaces WHERE name=?", (name,)
            ).fetchone()
            if row is None:
                raise RuntimeError("workspace creation did not produce a durable row")
            if owns_transaction:
                self.conn.commit()
            return str(row["id"])
        except BaseException:
            if owns_transaction and self.conn.transaction_owned_by_current_thread():
                self.conn.rollback()
            raise

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
        """Return one scoped repository id, creating it atomically when absent."""
        row = self.conn.execute(
            "SELECT id FROM repos WHERE workspace_id=? AND name=?", (workspace_id, name)
        ).fetchone()
        if row is not None:
            return str(row["id"])
        owns_transaction = not self.conn.transaction_owned_by_current_thread()
        try:
            if owns_transaction:
                self.conn.execute("BEGIN IMMEDIATE")
            candidate = ids.new_id("repo")
            self.conn.execute(
                "INSERT INTO repos(id, workspace_id, name, root_path, vcs_remote, "
                "primary_lang, created_at, settings) VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(workspace_id, name) DO NOTHING",
                (
                    candidate,
                    workspace_id,
                    name,
                    kw.get("root_path"),
                    kw.get("vcs_remote"),
                    kw.get("primary_lang"),
                    now_ts(),
                    _dumps(kw.get("settings") or {}),
                ),
            )
            row = self.conn.execute(
                "SELECT id FROM repos WHERE workspace_id=? AND name=?",
                (workspace_id, name),
            ).fetchone()
            if row is None:
                raise RuntimeError("repository creation did not produce a durable row")
            if owns_transaction:
                self.conn.commit()
            return str(row["id"])
        except BaseException:
            if owns_transaction and self.conn.transaction_owned_by_current_thread():
                self.conn.rollback()
            raise

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
                   commit: bool = True,
                   _allow_legacy_user_scope: bool = False,
                   _preserve_legacy_modified_hlc: bool = False) -> str:
        """Persist a memory and every derived mirror as one failure boundary.

        New USER-scoped rows are unsafe until records carry an owner identity. Sync and
        migration code preserving already-existing USER history may opt into that
        internal compatibility path. Sync may separately preserve the empty pre-v13
        descriptive clock; ordinary local writes always mint a real HLC.
        """
        if (
            _enum(rec.scope) == Scope.USER.value
            and not _allow_legacy_user_scope
        ):
            raise ValueError(USER_SCOPE_UNSUPPORTED)
        with self._write_operation("add_memory", commit=commit):
            return self._add_memory_impl(
                rec,
                audit=audit,
                preserve_legacy_modified_hlc=_preserve_legacy_modified_hlc,
            )

    def advance_memory_modified_hlc(
        self,
        memory_id: str,
        *,
        observed_hlc: str = "",
        commit: bool = True,
    ) -> str:
        """Atomically advance one memory's descriptive-state hybrid logical clock.

        ``commit=False`` deliberately leaves a newly opened transaction to the caller,
        allowing the clock update and a following direct descriptive update to share one
        commit/rollback boundary.  Inside an existing transaction this method uses a
        savepoint and never settles the caller's transaction, regardless of ``commit``.
        """
        if not isinstance(memory_id, str) or not memory_id:
            raise ValueError("memory_id must be a non-empty string")
        observed_hlc = normalize_modified_hlc(observed_hlc, allow_empty=True)
        with self._write_operation("advance_memory_modified_hlc", commit=commit):
            row = self.conn.execute(
                "SELECT modified_hlc FROM memories WHERE id=?", (memory_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"no memory with id '{memory_id}'")
            current = normalize_modified_hlc(
                str(row["modified_hlc"] or ""), allow_empty=True
            )
            advanced = advance_modified_hlc(
                current,
                observed=observed_hlc,
                node_id=self.device_id(),
                now_ms=int(now_ts() * 1000),
            )
            updated = self.conn.execute(
                "UPDATE memories SET modified_hlc=? WHERE id=?",
                (advanced, memory_id),
            ).rowcount
            if updated != 1:
                raise RuntimeError("memory descriptive clock update lost its target")
            return advanced

    def _add_memory_impl(
        self,
        rec: MemoryRecord,
        *,
        audit: bool = True,
        preserve_legacy_modified_hlc: bool = False,
    ) -> str:
        # Callers can mutate a dataclass after construction; validate again at the
        # persistence boundary so SQLite never receives NaN or infinity.
        for name in (
            "last_access",
            "valid_from",
            "valid_to",
            "ingested_at",
            "expired_at",
            "valid_to_recorded_at",
            "pinned_at",
            "unpinned_at",
        ):
            setattr(rec, name, _finite_timestamp(getattr(rec, name), name))
        rec.modified_hlc = normalize_modified_hlc(
            rec.modified_hlc, allow_empty=True
        )
        if (
            rec.valid_from is not None
            and rec.valid_to is not None
            and rec.valid_to < rec.valid_from
        ):
            raise ValueError(
                "valid_to cannot predate valid_from; the validity interval would be empty"
            )
        # This is the last common write boundary.  Check every persisted text-bearing
        # field *before* the main row, FTS mirror, or vector are written, including
        # direct Store callers that do not go through MemoryEngine/MemoryService.
        reject_secrets((
            ("title", rec.title), ("content", rec.content), ("summary", rec.summary),
            ("keywords", rec.keywords), ("metadata", rec.metadata),
            ("provenance", rec.provenance), ("subject_key", rec.subject_key),
            ("claim_kind", rec.claim_kind),
        ))
        # ``Store`` is a local-programmatic capability.  Stamp direct new writes
        # explicitly so prompt-facing recall can fail closed for genuinely legacy
        # rows without making current low-level integrations silently disappear.
        # External ingress (service/sync) provides its own stricter provenance.
        metadata = dict(rec.metadata or {})
        nested_provenance = metadata.get("provenance")
        dedicated = dict(rec.provenance or {})
        nested = (
            dict(nested_provenance)
            if isinstance(nested_provenance, dict) else {}
        )
        # Contradictory trust envelopes resolve to the stricter assertion. This
        # preserves fail-closed behavior for direct/sync callers while serializing one
        # canonical value into both storage locations for all subsequent reads.
        provenance = _merge_provenance_envelopes(dedicated, nested)
        if "trusted" not in provenance:
            provenance.update({"source": provenance.get("source", "local_store"),
                               "trusted": True,
                               "trust_origin": provenance.get(
                                   "trust_origin", "local_store"
                               )})
        if provenance.get("trusted") is True:
            provenance.setdefault("review_state", REVIEW_APPROVED)
        else:
            provenance.setdefault("review_state", REVIEW_PENDING)
        rec.provenance = provenance
        metadata["provenance"] = dict(provenance)
        rec.metadata = metadata
        # Canonicalize retention state at the common persistence boundary. Direct
        # Store writes and sync imports must serialize identically or replicas can
        # diverge after an oversized/invalid value makes a round trip.
        rec.stability = effective_stability(rec.stability)
        rec.access_count = effective_access_count(rec.access_count)
        if not rec.id:
            rec.id = ids.new_id("memory")
        existing_record: Optional[MemoryRecord] = None
        existing = self.conn.execute(
            "SELECT * FROM memories WHERE id=?",
            (rec.id,),
        ).fetchone()
        if existing is not None:
            if existing["workspace_id"] != rec.workspace_id:
                self.audit("system", "cross_workspace_overwrite_blocked", rec.id,
                           f"existing workspace={existing['workspace_id']}, "
                           f"incoming workspace={rec.workspace_id}", commit=False)
                rec.id = ids.new_id("memory")
                existing = None
            else:
                existing_record = _row_to_record(existing)
                if audit:
                    # Generic provenance-change record for direct writes. The sync path
                    # passes audit=False and logs its own semantic 'sync_overwrite'
                    # instead, so a synced update yields exactly one audit row.
                    self.audit(
                        "system",
                        "overwrite",
                        rec.id,
                        f"existing provenance={existing['provenance']}, "
                        f"incoming provenance={_dumps(rec.provenance)}",
                        commit=False,
                    )
        ts = now_ts()
        # A "closed history" record may legitimately carry only a past ``valid_to`` with
        # ``valid_from`` defaulting to ingest time (the fixture/backfill convention). The
        # empty-interval invariant therefore applies only when the caller explicitly
        # supplied BOTH endpoints — a caller-authored inversion is always a bug, whereas
        # a defaulted ``valid_from`` with a past ``valid_to`` is an accepted closed window.
        valid_from_was_explicit = rec.valid_from is not None
        rec.ingested_at = rec.ingested_at if rec.ingested_at is not None else ts
        rec.valid_from = rec.valid_from if rec.valid_from is not None else ts
        rec.last_access = rec.last_access if rec.last_access is not None else ts
        if not preserve_legacy_modified_hlc:
            previous_hlc = (
                existing_record.modified_hlc if existing_record is not None else ""
            )
            descriptive_changed = (
                existing_record is None
                or _memory_descriptive_state(rec)
                != _memory_descriptive_state(existing_record)
            )
            if descriptive_changed and (
                not rec.modified_hlc or rec.modified_hlc <= previous_hlc
            ):
                rec.modified_hlc = advance_modified_hlc(
                    previous_hlc,
                    observed=rec.modified_hlc,
                    node_id=self.device_id(),
                    now_ms=int(ts * 1000),
                )
            elif not descriptive_changed and rec.modified_hlc < previous_hlc:
                # An idempotent local upsert must not roll back the durable clock merely
                # because the caller reconstructed an otherwise-identical record.
                rec.modified_hlc = previous_hlc
        if (valid_from_was_explicit and rec.valid_to is not None
                and rec.valid_to < rec.valid_from):
            raise ValueError(
                "valid_to cannot predate valid_from; the validity interval would be empty"
            )
        self.conn.execute(
            """INSERT INTO memories
               (id, workspace_id, repo_id, session_id, scope, mtype, title, content, summary,
                keywords, metadata, importance, surprise, stability, access_count, last_access,
                valid_from, valid_to, valid_to_recorded_at, ingested_at, modified_hlc,
                expired_at, subject_key, claim_kind,
                pinned, sensitivity, provenance, confidence, pinned_at, unpinned_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                modified_hlc=excluded.modified_hlc,
                expired_at=excluded.expired_at, subject_key=excluded.subject_key,
                claim_kind=excluded.claim_kind, pinned=excluded.pinned,
                sensitivity=excluded.sensitivity, provenance=excluded.provenance,
                confidence=excluded.confidence,
                pinned_at=excluded.pinned_at, unpinned_at=excluded.unpinned_at""",
            (rec.id, rec.workspace_id, rec.repo_id, rec.session_id,
             _enum(rec.scope), _enum(rec.mtype), rec.title, rec.content, rec.summary,
             _dumps(rec.keywords), _dumps(rec.metadata), rec.importance, rec.surprise,
             rec.stability, rec.access_count, rec.last_access, rec.valid_from, rec.valid_to,
             rec.valid_to_recorded_at, rec.ingested_at, rec.modified_hlc, rec.expired_at,
             rec.subject_key, rec.claim_kind,
             int(rec.pinned), rec.sensitivity,
             _dumps(rec.provenance), rec.confidence,
             rec.pinned_at, rec.unpinned_at),
        )
        # The method-level transaction/savepoint keeps the row, FTS mirror, and
        # vector mirror atomic without settling a caller-owned transaction.
        self._fts_upsert(rec.id, rec.title, rec.content, " ".join(rec.keywords))
        if rec.embedding is not None:
            self.put_vector(
                rec.id,
                rec.embedding,
                model=str(rec.metadata.get("embed_model", "")),
            )
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

    def visible_memory_ids(self, memory_ids: list[str],
                           flt: Optional[SearchFilter], *,
                           include_invalid: bool = False) -> set[str]:
        """Return the bounded subset visible under :func:`memory_matches_filter`.

        This is the lightweight visibility oracle for native indexes: it reads
        identity/scope/temporal columns only and never hydrates content or vectors.
        """
        if not isinstance(memory_ids, list):
            raise TypeError("memory_ids must be a list")
        if len(memory_ids) > IN_CLAUSE_CHUNK:
            raise ValueError(
                f"memory_ids may contain at most {IN_CLAUSE_CHUNK} entries"
            )
        unique = list(dict.fromkeys(memory_ids))
        if any(not isinstance(memory_id, str) or not memory_id for memory_id in unique):
            raise ValueError("memory_ids must contain non-empty strings")
        if not unique:
            return set()
        marks = ",".join("?" for _ in unique)
        rows = self.conn.execute(
            "SELECT id, workspace_id, repo_id, session_id, scope, mtype, "
            "valid_from, valid_to, valid_to_recorded_at, ingested_at, expired_at "
            f"FROM memories WHERE id IN ({marks})",
            unique,
        ).fetchall()
        visible: set[str] = set()
        for row in rows:
            record = MemoryRecord(
                id=row["id"],
                content="",
                workspace_id=row["workspace_id"],
                repo_id=row["repo_id"],
                session_id=row["session_id"],
                scope=Scope(row["scope"]),
                mtype=MemoryType(row["mtype"]),
                valid_from=row["valid_from"],
                valid_to=row["valid_to"],
                valid_to_recorded_at=row["valid_to_recorded_at"],
                ingested_at=row["ingested_at"],
                expired_at=row["expired_at"],
            )
            if memory_matches_filter(
                record, flt, include_invalid=include_invalid
            ):
                visible.add(record.id)
        return visible

    def list_memories(self, flt: Optional[SearchFilter] = None,
                      *, include_invalid: bool = False, limit: Optional[int] = None,
                      prompt_only: bool = False) -> list[MemoryRecord]:
        """List scoped records, optionally capping only prompt-eligible rows.

        Public callers can opt into ``prompt_only`` when this bounded result will enter
        model-adjacent output.  Eligibility is deliberately checked while streaming SQL
        rows, before the result cap: a large pending import must not hide an older
        approved record simply by consuming the raw ``LIMIT`` window.
        """
        if prompt_only and limit is not None and int(limit) <= 0:
            return []
        sql = "SELECT * FROM memories"
        where, params = self._where(flt, include_invalid)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY ingested_at DESC"
        if limit and not prompt_only:
            sql += f" LIMIT {int(limit)}"
        if not prompt_only:
            rows = self.conn.execute(sql, params).fetchall()
            return [_row_to_record(r) for r in rows]

        eligible_limit = None if limit is None else int(limit)
        out: list[MemoryRecord] = []
        for row in self.conn.execute(sql, params):
            if not _row_is_prompt_eligible(row["provenance"], row["metadata"]):
                continue
            out.append(_row_to_record(row))
            if eligible_limit is not None and len(out) >= eligible_limit:
                break
        return out

    def count_memories(self, flt: Optional[SearchFilter] = None,
                       *, include_invalid: bool = False) -> int:
        """Count records visible to a search filter without materializing them."""
        sql = "SELECT COUNT(*) AS count FROM memories"
        where, params = self._where(flt, include_invalid)
        if where:
            sql += " WHERE " + " AND ".join(where)
        row = self.conn.execute(sql, params).fetchone()
        return int(row["count"] if row is not None else 0)

    def prompt_eligibility_counts(
        self, flt: Optional[SearchFilter] = None, *, include_invalid: bool = False
    ) -> dict[str, int]:
        """Return content-free review diagnostics for one recall scope."""
        from engraphis.core.poisoning import inspection_eligible, prompt_eligible

        sql = "SELECT provenance, metadata FROM memories"
        where, params = self._where(flt, include_invalid)
        if where:
            sql += " WHERE " + " AND ".join(where)
        counts = {
            "total": 0,
            "prompt_eligible": 0,
            "pending": 0,
            "quarantined": 0,
            "legacy_trusted_unreviewed": 0,
            "legacy_local_agent_gate": 0,
        }
        for row in self.conn.execute(sql, params):
            provenance = _loads(row["provenance"], {})
            metadata = _loads(row["metadata"], {})
            provenance = provenance if isinstance(provenance, dict) else {}
            metadata = metadata if isinstance(metadata, dict) else {}
            counts["total"] += 1
            if prompt_eligible(provenance, metadata):
                counts["prompt_eligible"] += 1
                continue
            if not inspection_eligible(provenance, metadata):
                counts["quarantined"] += 1
                continue
            if (
                provenance.get("source") in {"agent", "intent_api"}
                and provenance.get("trusted") is False
                and provenance.get("review_state") == REVIEW_PENDING
                and provenance.get("trust_origin") == "service_review_gate"
                and provenance.get("trust_downgraded") is True
            ):
                counts["legacy_local_agent_gate"] += 1
            elif (
                provenance.get("trusted") is True
                and "review_state" not in provenance
            ):
                counts["legacy_trusted_unreviewed"] += 1
            else:
                counts["pending"] += 1
        return counts

    def list_proactive_overrides(self, flt: Optional[SearchFilter] = None,
                                 *, prompt_only: bool = False) -> list[MemoryRecord]:
        """Return pinned/``proactive=always`` rows outside the normal scan window.

        The proactive agenda intentionally bounds its ordinary scan, but explicit user
        choices are not bounded by recency.  Keep this query separate so a very old pin
        cannot disappear behind 500 newer memories without making every proactive call
        materialize the entire store.
        """
        sql = "SELECT * FROM memories"
        where, params = self._where(flt, include_invalid=False)
        where.append("(pinned=1 OR lower(metadata) LIKE ?)")
        params.append('%"proactive"%')
        sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY ingested_at DESC"
        out: list[MemoryRecord] = []
        for row in self.conn.execute(sql, params):
            rec = _row_to_record(row)
            proactive = str((rec.metadata or {}).get("proactive") or "").lower()
            if not rec.pinned and proactive != "always":
                continue
            if prompt_only and not _row_is_prompt_eligible(row["provenance"], row["metadata"]):
                continue
            out.append(rec)
        return out

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
                       actor: str = "system", reason: str = "contradicted",
                       commit: bool = True) -> None:
        """Close one fact and its graph evidence as one atomic governance write."""
        recorded_at = _finite_timestamp(now_ts(), "recorded_at")
        at = _finite_timestamp(recorded_at if at is None else at, "at")
        with self._write_operation("close_validity", commit=commit):
            row = self.conn.execute(
                "SELECT valid_from FROM memories WHERE id=?", (memory_id,)
            ).fetchone()
            if (
                row is not None
                and row["valid_from"] is not None
                and at < row["valid_from"]
            ):
                raise ValueError("valid_to cannot predate valid_from")
            updated = self.conn.execute(
                "UPDATE memories SET valid_to=?, valid_to_recorded_at=? "
                "WHERE id=? AND (valid_to IS NULL OR valid_to>?)",
                (at, recorded_at, memory_id, at),
            ).rowcount
            if updated:
                self.invalidate_edges_for_memory(memory_id, at=at, commit=False)
            # Governance attempts are audit-worthy even when the interval was already
            # closed. MCP callers expose forget as non-idempotent so every request
            # retains evidence without widening the closed interval.
            self.audit(actor, "invalidate", memory_id, reason, commit=False)

    def set_pinned(self, memory_id: str, pinned: bool) -> None:
        """Pinned memories are exempt from automatic decay/pruning (AGENTS.md §3.2);
        governance (explicit forget/correct) can still act on them.

        Every pin-state transition stamps the system time into the row so sync can
        merge the state as a latest-transition lattice instead of an OR-set:
        ``pinned_at`` records the latest pin and ``unpinned_at`` the latest unpin.
        A re-pin preserves the unpin marker, so peers converge on whichever
        transition happened last instead of allowing a stale pin to resurrect.
        """
        row = self.conn.execute(
            "SELECT pinned FROM memories WHERE id=?", (memory_id,)
        ).fetchone()
        if row is None:
            return
        now = now_ts()
        if pinned:
            self.conn.execute(
                "UPDATE memories SET pinned=1, pinned_at=? "
                "WHERE id=? AND pinned=0",
                (now, memory_id),
            )
        else:
            self.conn.execute(
                "UPDATE memories SET pinned=0, unpinned_at=? "
                "WHERE id=? AND pinned=1",
                (now, memory_id),
            )
        self.conn.commit()

    def reinforce(self, memory_id: str, *, alpha: float = 0.3, boost: float = 0.0) -> None:
        """Apply one spacing-effect transition atomically across Store instances."""
        with self._write_operation("reinforce", commit=True):
            row = self.conn.execute(
                "SELECT stability, access_count FROM memories WHERE id=?", (memory_id,)
            ).fetchone()
            if row is None:
                return
            new_stab, new_count = reinforced_stability(
                row["stability"], row["access_count"], alpha=alpha, boost=boost,
            )
            self.conn.execute(
                "UPDATE memories SET stability=?, access_count=?, last_access=? WHERE id=?",
                (new_stab, new_count, now_ts(), memory_id),
            )

    # ── vectors ───────────────────────────────────────────────────────────────
    def put_vector(self, memory_id: str, vec: np.ndarray, *, model: str = "") -> None:
        model = str(model or "")
        active = self.active_embedding_space()
        rebuilding = self.embedding_rebuild_target()
        expected = rebuilding or active
        if expected and model != expected:
            raise RuntimeError(
                "vector model does not match the active embedding-space contract"
            )
        try:
            v = np.asarray(vec, dtype=np.float32)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("vector must be a finite, non-empty 1-D array") from exc
        if v.ndim != 1 or v.size == 0 or not np.isfinite(v).all():
            raise ValueError("vector must be a finite, non-empty 1-D array")
        # Compute in float64 so large finite float32 inputs cannot overflow the
        # norm and silently turn into an all-zero vector during normalization.
        norm = float(np.linalg.norm(v.astype(np.float64, copy=False)))
        if norm > 0:
            v = v / norm
        self.conn.execute(
            "INSERT OR REPLACE INTO mem_vectors(id, dim, vector, model) VALUES (?,?,?,?)",
            (memory_id, int(v.shape[0]), v.tobytes(), model),
        )

    def get_vectors(self, memory_ids: Iterable[str]) -> dict[str, np.ndarray]:
        """Return stored, normalized vectors for a bounded set of memory ids.

        Recall uses this to calculate an original-query support score for a final
        candidate introduced by a planner query but absent from the original vector
        arm's bounded result set.  Reading the persisted vector preserves the exact
        vector-space result used by every backend without a fresh embedding call.
        """
        unique = list(dict.fromkeys(str(memory_id) for memory_id in memory_ids if memory_id))
        vectors: dict[str, np.ndarray] = {}
        for start in range(0, len(unique), IN_CLAUSE_CHUNK):
            chunk = unique[start:start + IN_CLAUSE_CHUNK]
            marks = ",".join("?" for _ in chunk)
            rows = self.conn.execute(
                f"SELECT id, vector FROM mem_vectors WHERE id IN ({marks})", chunk,
            ).fetchall()
            vectors.update({
                row["id"]: np.frombuffer(row["vector"], dtype=np.float32)
                for row in rows
            })
        return vectors

    def embedding_version(self, identity: str) -> Optional[str]:
        row = self.conn.execute(
            "SELECT version FROM embedding_state WHERE identity=?", (identity,)
        ).fetchone()
        return str(row["version"]) if row is not None else None

    def active_embedding_space(self) -> Optional[str]:
        """Return the one vector-space fingerprint represented by stored vectors."""
        return self.embedding_version("__active__")

    def embedding_rebuild_target(self) -> Optional[str]:
        """Return the target fingerprint while a rebuild is incomplete."""
        return self.embedding_version("__rebuilding__")

    def embedding_space_ready(self, fingerprint: str) -> bool:
        """Whether every stored vector is safe for queries from fingerprint."""
        if not (
            fingerprint
            and self.embedding_rebuild_target() is None
            and self.active_embedding_space() == fingerprint
        ):
            return False
        # Three indexed existence probes avoid a full vector-table scan while
        # detecting null, older, or newer model fingerprints. This catches manual
        # repairs and interrupted pre-v11 tooling even when the active marker itself
        # was incorrectly stamped current.
        for predicate, params in (
            ("model IS NULL", ()),
            ("model < ?", (fingerprint,)),
            ("model > ?", (fingerprint,)),
        ):
            if self.conn.execute(
                f"SELECT 1 FROM mem_vectors WHERE {predicate} LIMIT 1", params
            ).fetchone() is not None:
                return False
        return True

    def begin_embedding_rebuild(self, fingerprint: str) -> None:
        """Durably disable vector recall before the first replacement batch."""
        if not fingerprint:
            raise ValueError("embedding fingerprint is required")
        self.conn.execute(
            "INSERT INTO embedding_state(identity, version, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(identity) DO UPDATE SET "
            "version=excluded.version, updated_at=excluded.updated_at",
            ("__rebuilding__", fingerprint, now_ts()),
        )
        self.conn.commit()

    def finish_embedding_rebuild(
        self, fingerprint: str, *, identity: str, version: str
    ) -> None:
        """Atomically publish a complete vector space and clear its rebuild gate."""
        if not fingerprint or not identity or not version:
            raise ValueError("complete embedding identity is required")
        if self.embedding_rebuild_target() != fingerprint:
            raise RuntimeError("embedding rebuild target changed before publication")
        stamp = now_ts()
        self.conn.execute(
            "INSERT INTO embedding_state(identity, version, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(identity) DO UPDATE SET "
            "version=excluded.version, updated_at=excluded.updated_at",
            ("__active__", fingerprint, stamp),
        )
        # Retain the backend row as operator-facing history. Recall never uses it as
        # authority, which prevents an A -> B -> A switch from accepting stale A vectors.
        self.conn.execute(
            "INSERT INTO embedding_state(identity, version, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(identity) DO UPDATE SET "
            "version=excluded.version, updated_at=excluded.updated_at",
            (identity, version, stamp),
        )
        self.conn.execute(
            "DELETE FROM embedding_state WHERE identity='__rebuilding__'"
        )
        self.conn.commit()

    def embedding_space_health(self, configured_fingerprint: str) -> dict[str, Any]:
        """Return content-free vector coverage and rebuild diagnostics."""
        total_row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM mem_vectors"
        ).fetchone()
        total = 0
        if total_row is not None:
            total = int(total_row["n"])
        current = 0
        if configured_fingerprint:
            current_row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM mem_vectors WHERE model=?",
                (configured_fingerprint,),
            ).fetchone()
            if current_row is not None:
                current = int(current_row["n"])
        active = self.active_embedding_space() or ""
        rebuilding = self.embedding_rebuild_target() or ""
        return {
            "configured": configured_fingerprint,
            "active": active,
            "rebuilding": rebuilding,
            "ready": self.embedding_space_ready(configured_fingerprint),
            "vectors": total,
            "current_vectors": current,
            "stale_vectors": max(0, total - current),
        }

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

    def vector_matrix(self, flt: Optional[SearchFilter] = None,
                      *, include_invalid: bool = False, dim: int) -> tuple[list[str], np.ndarray]:
        """Materialize one filtered, fixed-width vector matrix for an exact scan.

        NumpyVectorIndex needs every candidate at once for its exact dot-product
        search. Fetching that set in one locked statement avoids repeated joins and
        avoids constructing one NumPy view per vector before vstack copies them.
        The store remains the source of truth: this is deliberately a read-through
        helper, not an index cache. The blob-length predicate retains iter_vectors'
        behaviour of ignoring malformed legacy rows whose stored dimension does not
        match their actual payload.
        """
        if dim < 1:
            raise ValueError("vector matrix dimension must be a positive integer")
        where, params = self._where(flt, include_invalid, alias="m")
        where.extend(("v.dim=?", "length(v.vector)=?"))
        params.extend((int(dim), int(dim) * np.dtype(np.float32).itemsize))
        sql = (
            "SELECT v.id AS id, v.vector AS vector FROM mem_vectors v "
            "JOIN memories m ON m.id = v.id WHERE "
            + " AND ".join(where)
            + " ORDER BY v.id"
        )
        rows = self.conn.fetchall(sql, params)
        if not rows:
            return [], np.empty((0, dim), dtype=np.float32)
        ids = [str(row["id"]) for row in rows]
        payload = b"".join(row["vector"] for row in rows)
        return ids, np.frombuffer(payload, dtype=np.float32).reshape(len(ids), dim)

    # ── full text ─────────────────────────────────────────────────────────────
    def _fts_upsert(self, mid: str, title: str, content: str, keywords: str) -> None:
        self.conn.execute("DELETE FROM mem_fts WHERE id=?", (mid,))
        self.conn.execute(
            "INSERT INTO mem_fts(id, title, content, keywords) VALUES (?,?,?,?)",
            (mid, title, content, keywords),
        )

    # ── destructive, per-memory secure erasure ──────────────────────────────
    @staticmethod
    def _has_table(conn, name: str) -> bool:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?", (name,)
        ).fetchone() is not None

    @classmethod
    def _erase_memory_rows(cls, conn, memory_id: str, *, actor: str = "user") -> dict:
        """Remove a memory and all known local derivatives from one SQLite database.

        This deliberately does *not* use temporal retirement. It is for accidentally
        captured credentials and is intentionally lossy.  The helper also supports
        recognised local SQLite recovery backups, some of which predate newer tables.
        """
        if not cls._has_table(conn, "memories"):
            return {"present": False, "removed": False}
        memory_columns = {
            item["name"] for item in conn.execute("PRAGMA table_info(memories)").fetchall()
        }
        identity_columns = [
            name for name in (
                "id", "workspace_id", "repo_id", "scope", "sensitivity",
            )
            if name in memory_columns
        ]
        row = conn.execute(
            f"SELECT {', '.join(identity_columns)} FROM memories WHERE id=?",
            (memory_id,),
        ).fetchone()
        if row is None:
            return {"present": False, "removed": False}

        # Ask SQLite to overwrite deleted cells where the active VFS supports it. A
        # later VACUUM rebuild removes free pages/FTS tombstones from the live database.
        conn.execute("PRAGMA secure_delete=ON")
        tables = {
            name for name in (
                "mem_fts", "mem_vectors", "mem_vec_ann", "code_memory_links",
                "memory_entities", "edge_supports", "edges", "entities", "mem_links",
                "audit",
            ) if cls._has_table(conn, name)
        }
        incident_entities: list[str] = []
        if "memory_entities" in tables:
            incident_entities = [str(item[0]) for item in conn.execute(
                "SELECT DISTINCT entity_id FROM memory_entities WHERE memory_id=?", (memory_id,)
            ).fetchall()]
        supported_edges: list[str] = []
        if "edge_supports" in tables:
            supported_edges = [str(item[0]) for item in conn.execute(
                "SELECT DISTINCT edge_id FROM edge_supports WHERE memory_id=?", (memory_id,)
            ).fetchall()]

        for table, column in (
            ("mem_fts", "id"), ("mem_vectors", "id"), ("mem_vec_ann", "id"),
            ("code_memory_links", "memory_id"), ("memory_entities", "memory_id"),
            ("edge_supports", "memory_id"),
        ):
            if table in tables:
                conn.execute(f"DELETE FROM {table} WHERE {column}=?", (memory_id,))
        if "mem_links" in tables:
            conn.execute("DELETE FROM mem_links WHERE a=? OR b=?", (memory_id, memory_id))

        # A graph edge whose last provenance support was the erased memory is itself a
        # derivative of that secret. Preserve shared graph facts with another support.
        if supported_edges and "edges" in tables:
            if "edge_supports" in tables:
                for edge_id in supported_edges:
                    remaining = conn.execute(
                        "SELECT id, memory_id, valid_to, expired_at, provenance "
                        "FROM edge_supports WHERE edge_id=? ORDER BY id",
                        (edge_id,),
                    ).fetchall()
                    if not remaining:
                        conn.execute("DELETE FROM edges WHERE id=?", (edge_id,))
                        continue

                    # Normalized support rows are authoritative. Rebuild every surviving
                    # compatibility blob so the erased source cannot keep a shared edge
                    # prompt-ineligible or remain falsely attributed in provenance.
                    active_provenance = []
                    active_memory_ids: list[str] = []
                    historical_provenance = []
                    historical_memory_ids: list[str] = []
                    for support in remaining:
                        support_memory_id = str(support["memory_id"] or "")
                        if support_memory_id and support_memory_id not in historical_memory_ids:
                            historical_memory_ids.append(support_memory_id)
                        provenance = _loads(support["provenance"], {})
                        provenance = dict(provenance) if isinstance(provenance, dict) else {}
                        provenance["memory_id"] = support_memory_id
                        provenance["memory_ids"] = (
                            [support_memory_id] if support_memory_id else []
                        )
                        conn.execute(
                            "UPDATE edge_supports SET provenance=? WHERE id=?",
                            (_dumps(provenance), support["id"]),
                        )
                        historical_provenance.append(provenance)
                        if support_memory_id and support["valid_to"] is None \
                                and support["expired_at"] is None:
                            if support_memory_id not in active_memory_ids:
                                active_memory_ids.append(support_memory_id)
                            active_provenance.append(provenance)
                    memory_ids = active_memory_ids or historical_memory_ids
                    if not memory_ids:
                        conn.execute("DELETE FROM edges WHERE id=?", (edge_id,))
                        continue
                    if not active_memory_ids:
                        closed_at = now_ts()
                        conn.execute(
                            "UPDATE edges SET valid_to=?, valid_to_recorded_at=? "
                            "WHERE id=? AND valid_to IS NULL",
                            (closed_at, closed_at, edge_id),
                        )
                    rebuilt = _merge_edge_provenance(
                        active_provenance or historical_provenance
                    )
                    rebuilt["memory_id"] = memory_ids[0]
                    rebuilt["memory_ids"] = memory_ids
                    conn.execute(
                        "UPDATE edges SET provenance=? WHERE id=?",
                        (_dumps(rebuilt), edge_id),
                    )
            else:
                marks = ",".join("?" for _ in supported_edges)
                conn.execute(f"DELETE FROM edges WHERE id IN ({marks})", supported_edges)

        # An entity extracted only from this memory can itself contain credential text.
        # Remove it only if it no longer has any memory or graph incidence.
        if incident_entities and "entities" in tables:
            marks = ",".join("?" for _ in incident_entities)
            clauses = []
            if "memory_entities" in tables:
                clauses.append("NOT EXISTS (SELECT 1 FROM memory_entities me "
                               "WHERE me.entity_id=entities.id)")
            if "edges" in tables:
                clauses.append("NOT EXISTS (SELECT 1 FROM edges e "
                               "WHERE e.src=entities.id OR e.dst=entities.id)")
            if clauses:
                conn.execute(
                    f"DELETE FROM entities WHERE id IN ({marks}) AND " + " AND ".join(clauses),
                    incident_entities,
                )

        # Prior audit details are caller text and could itself contain the credential.
        # Remove those entries, then add only a content-free erasure marker below.
        if "audit" in tables:
            conn.execute("DELETE FROM audit WHERE target=?", (memory_id,))
        conn.execute("DELETE FROM memories WHERE id=?", (memory_id,))
        if "audit" in tables:
            conn.execute(
                "INSERT INTO audit(id, ts, actor, action, target, detail) VALUES (?,?,?,?,?,?)",
                (ids.new_id("audit"), now_ts(), actor, "secure_erase", memory_id,
                 "per-memory secure erasure completed; content intentionally omitted"),
            )
        return {
            "present": True,
            "removed": True,
            "workspace_id": row["workspace_id"] if "workspace_id" in row.keys() else None,
            "repo_id": row["repo_id"] if "repo_id" in row.keys() else None,
            "scope": row["scope"] if "scope" in row.keys() else None,
            "sensitivity": (
                row["sensitivity"] if "sensitivity" in row.keys() else None
            ),
            "graph_edges_considered": len(supported_edges),
            "entities_considered": len(incident_entities),
        }

    @staticmethod
    def _checkpoint_and_vacuum(conn, *, durable: bool) -> dict:
        """Best-effort physical cleanup after a destructive erase, without overclaiming."""
        if not durable:
            return {"secure_delete": True, "wal": "not_applicable", "vacuum": "not_applicable"}
        result = {"secure_delete": True, "wal": "unavailable", "vacuum": "unavailable"}
        try:
            checkpoint = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            # SQLite returns (busy, log, checkpointed); never pretend busy means erased.
            result["wal"] = "truncated" if checkpoint is not None and int(checkpoint[0]) == 0 else "busy"
        except Exception:  # pragma: no cover - depends on VFS / external connection state
            result["wal"] = "failed"
        try:
            conn.execute("VACUUM")
            result["vacuum"] = "completed"
        except Exception:  # pragma: no cover - depends on disk / external connection state
            result["vacuum"] = "failed"
        try:
            checkpoint = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if checkpoint is not None and int(checkpoint[0]) == 0:
                result["wal"] = "truncated"
            elif result["wal"] != "failed":
                result["wal"] = "busy"
        except Exception:  # pragma: no cover - see initial checkpoint
            if result["wal"] != "truncated":
                result["wal"] = "failed"
        return result

    def _recognised_local_backups(self) -> list[Path]:
        """Return recovery artefacts this Store created and can safely identify.

        We cannot discover filesystem snapshots, cloud backups, copied databases, or
        another process's encrypted backup location. Those remain an explicit operator
        obligation in the secure-erasure result and documentation.
        """
        if self.path in (":memory:", "") or self.path.startswith("file::memory:"):
            return []
        primary = Path(self.path).resolve()
        parent = primary.parent
        patterns = (
            f"{primary.name}.pre-migration-v*.bak",
            f"{primary.name}.embed-repair-*.bak",
            f"{primary.stem}.v1-backup-*.db",
        )
        found: list[Path] = []
        for pattern in patterns:
            for candidate in parent.glob(pattern):
                try:
                    if candidate.is_file() and candidate.resolve() != primary:
                        found.append(candidate.resolve())
                except OSError:
                    continue
        return sorted(set(found), key=lambda value: str(value))

    def secure_erase_memory(self, memory_id: str, *, actor: str = "user") -> dict:
        """Irreversibly erase one memory plus local index copies and known backups.

        This is a breach-remediation operation, not the normal ``retire`` lifecycle.
        It clears current SQLite rows, FTS/vector-index derivatives, related graph/link
        state, audit details for that record, WAL contents when SQLite can checkpoint,
        and recognised local SQLite recovery backups. OS snapshots, copies, remote sync
        peers, and a process that already read the secret cannot be recalled or erased.
        """
        owns_transaction = not self.conn.transaction_owned_by_current_thread()
        try:
            # Mint the origin before opening the erase transaction.  ``device_id`` may
            # need to write sync metadata on a new database; keeping that write outside
            # the destructive transaction means the deletion and terminal tombstone
            # commit (or roll back) as one unit.
            device_id = self.device_id()
            current = self._erase_memory_rows(self.conn, memory_id, actor=actor)
            if not current["present"]:
                raise KeyError(f"no memory with id '{memory_id}'")
            export_marker = self.get_memory_sync_export(memory_id)
            if (
                export_marker is not None
                and export_marker["workspace_id"] == current.get("workspace_id")
            ):
                export_class = TOMBSTONE_REMOTE_ERASURE
                tombstone_workspace_id = export_marker["workspace_id"]
                tombstone_repo_id = export_marker["repo_id"]
            else:
                export_class = TOMBSTONE_NEVER_EXPORT
                tombstone_workspace_id = current.get("workspace_id")
                tombstone_repo_id = current.get("repo_id")
            # Current scope/sensitivity cannot prove that an id ever crossed a sync
            # boundary. Only the durable content-free marker can authorize a remote
            # erasure; absent or scope-conflicting evidence fails closed to local-only.
            self.add_memory_tombstone(
                memory_id, deleted_at=now_ts(),
                device_id=device_id,
                workspace_id=tombstone_workspace_id,
                repo_id=tombstone_repo_id,
                export_class=export_class,
            )
            if owns_transaction and self.conn.transaction_owned_by_current_thread():
                self.conn.commit()
        except BaseException:
            if owns_transaction and self.conn.transaction_owned_by_current_thread():
                self.conn.rollback()
            raise
        durable = self.path not in (":memory:", "") and not self.path.startswith("file::memory:")
        maintenance = self._checkpoint_and_vacuum(self.conn, durable=durable)

        backup_processed = 0
        backup_failed = 0
        for backup in self._recognised_local_backups():
            conn = None
            try:
                conn = self._open_connection(str(backup))
                erased = self._erase_memory_rows(conn, memory_id, actor="secure_erase")
                conn.commit()
                self._checkpoint_and_vacuum(conn, durable=True)
                if erased["present"]:
                    backup_processed += 1
            except Exception:  # pragma: no cover - keyed/corrupt/locked backups vary by deployment
                backup_failed += 1
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
        return {
            "id": memory_id,
            "status": "securely_erased",
            "export_class": export_class,
            "maintenance": maintenance,
            "recognised_backups_erased": backup_processed,
            "recognised_backups_failed": backup_failed,
            "backup_limitations": (
                "Only recognised local SQLite recovery backups were scanned. Erase or rotate "
                "filesystem snapshots, copied/exported databases, remote sync peers, and any "
                "other backups separately; a running agent may already have read the secret."
            ),
        }

    def fts_search(self, query: str, k: int = 20,
                   *, filter: Optional[SearchFilter] = None) -> list[tuple[str, float]]:
        """Lexical arm. Uses FTS5 BM25 when available, else a LIKE fallback."""
        q = (query or "").strip()
        if not q:
            return []
        terms = _fts_terms(q)
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
        # Use the same conservative inflection variants as FTS5 so lexical-only degraded
        # mode remains useful on SQLite builds without FTS5.
        # ``_fts_terms`` intentionally removes punctuation for FTS syntax.  In the
        # LIKE fallback, retain the literal query first: C++ and v1.2 must not be
        # reduced to broad C/v1/2 matches that consume the caller's result limit.
        def search_like(
            search_terms: list[str], limit: int, excluded: Optional[list[str]] = None
        ) -> list[str]:
            clauses = []
            query_params: list[Any] = []
            for term in search_terms:
                like = f"%{_escape_like(term)}%"
                clauses.append(
                    "(f.content LIKE ? ESCAPE '\\' OR f.title LIKE ? ESCAPE '\\' "
                    "OR f.keywords LIKE ? ESCAPE '\\')"
                )
                query_params.extend((like, like, like))
            if not clauses or limit <= 0:
                return []
            exclusions = ""
            if excluded:
                marks = ",".join("?" for _ in excluded)
                exclusions = f" AND f.id NOT IN ({marks})"
            rows = self.conn.execute(
                "SELECT f.id FROM mem_fts f JOIN memories m ON m.id = f.id "
                "WHERE (" + " OR ".join(clauses) + ")" + extra + exclusions + " LIMIT ?",
                (*query_params, *params, *(excluded or []), limit),
            ).fetchall()
            return [row["id"] for row in rows]

        literal_ids = search_like([q], k)
        if len(literal_ids) >= k:
            return [(memory_id, 0.5) for memory_id in literal_ids]
        # Add the ordinary token/inflection matches only after literal results, and
        # avoid repeating a literal term for simple punctuation-free queries.
        variants = [term for term in terms if term.casefold() != q.casefold()]
        variant_ids = search_like(variants, k - len(literal_ids), literal_ids)
        return [(memory_id, 0.5) for memory_id in [*literal_ids, *variant_ids]]

    # ── graph ─────────────────────────────────────────────────────────────────
    def upsert_entity(self, node: Node, *, commit: bool = True) -> str:
        """Persist an entity and its derived incidence atomically."""
        with self._write_operation("upsert_entity", commit=commit):
            return self._upsert_entity_impl(node)

    def _upsert_entity_impl(self, node: Node) -> str:
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
        self._live_canonicalize_entity(
            nid, name=node.name, workspace_id=node.workspace_id, repo_id=node.repo_id,
        )
        return nid

    def _live_canonicalize_entity(self, entity_id: str, *, name: str,
                                  workspace_id: Optional[str],
                                  repo_id: Optional[str]) -> None:
        """Merge a freshly-written entity into a token-overlap alias group."""
        name = (name or "").strip()
        if len(name) < 2 or not workspace_id:
            return
        entity = self.conn.execute(
            "SELECT etype FROM entities WHERE id=?", (entity_id,)
        ).fetchone()
        if entity is None:
            return
        candidates = self._entity_blocking_candidates(
            entity_id=entity_id, workspace_id=workspace_id,
            etype=entity["etype"], name=name,
        )
        best: Optional[dict] = None
        best_overlap = 0.0
        for peer in candidates:
            overlap = _entity_overlap(name, peer["name"])
            if overlap is None or overlap < 0.6 or overlap <= best_overlap:
                continue
            best_overlap = overlap
            best = dict(peer)
        if best is None:
            return
        peer_canonical = best["canonical_id"] or best["id"]
        self.conn.execute(
            "UPDATE entities SET canonical_id=?, canonical_method=? WHERE id=?",
            (peer_canonical, "token_overlap", entity_id),
        )

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
        if repo_id is None:
            # A workspace-owned entity is the shared identity across its repositories.
            # Include every repo-owned memory in this workspace, then partition profile
            # writes by the memory owner so a workspace sweep remains repo-isolated.
            scope_sql = "1=1"
            scope_params: list[Any] = []
        else:
            # A repo-owned entity may use workspace-level memories as shared evidence,
            # but must not reach a sibling repository.
            scope_sql = "(repo_id=? OR repo_id IS NULL)"
            scope_params = [repo_id]
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
    def list_entities(self, flt: Optional[SearchFilter] = None, *,
                      after_id: Optional[str] = None,
                      limit: Optional[int] = None) -> list[Node]:
        """Return scoped entities, with optional deterministic keyset paging.

        Passing ``after_id`` (including ``""`` for the first page) selects
        ascending ULID order. Omitting it preserves the legacy newest-first view.
        """
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
        if after_id:
            where.append("id>?")
            params.append(after_id)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY " + (
            "id" if after_id is not None else "created_at DESC, id DESC"
        )
        if limit is not None:
            sql += " LIMIT ?"
            params.append(max(0, int(limit)))
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
        valid_from = _finite_timestamp(valid_from, "valid_from")
        valid_to = _finite_timestamp(valid_to, "valid_to")
        valid_to_recorded_at = _finite_timestamp(
            valid_to_recorded_at, "valid_to_recorded_at"
        )
        ingested_at = _finite_timestamp(ingested_at, "ingested_at")
        expired_at = _finite_timestamp(expired_at, "expired_at")
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
        if valid_to is not None and valid_to < requested_valid:
            raise ValueError("memory-entity valid_to cannot predate valid_from")
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
                             limit: Optional[int] = None,
                             prompt_only: bool = False) -> list[dict]:
        """Return bounded scoped/temporal incidence rows for graph retrieval.

        ``prompt_only`` applies the canonical trust predicate before ``limit``.
        Derived graph bridges otherwise let pending records exhaust a raw SQL
        result window and hide lower-ranked approved evidence.
        """
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
                    prompt_only=prompt_only,
                )
            ]
            rows.sort(key=lambda row: (-float(row.get("confidence") or 0.0), row["id"]))
            return rows if limit is None else rows[:max(0, int(limit))]
        if prompt_only and limit is not None and int(limit) <= 0:
            return []
        valid_at, known_at = _temporal_anchors(flt)
        sql = (
            "SELECT me.*"
            + (", m.provenance AS memory_provenance, m.metadata AS memory_metadata"
               if prompt_only else "")
            + " FROM memory_entities me "
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
        if limit is not None and not prompt_only:
            sql += " LIMIT ?"
            params.append(max(0, int(limit)))
        if not prompt_only:
            return [dict(row) for row in self.conn.execute(sql, params).fetchall()]
        eligible_limit = None if limit is None else max(0, int(limit))
        rows: list[dict] = []
        for row in self.conn.execute(sql, params):
            item = dict(row)
            if not _row_is_prompt_eligible(
                item.pop("memory_provenance", None), item.pop("memory_metadata", None),
            ):
                continue
            rows.append(item)
            if eligible_limit is not None and len(rows) >= eligible_limit:
                break
        return rows

    def upsert_edge(self, edge: Edge, *, commit: bool = True) -> str:
        """Atomically persist an edge and its normalized support rows."""
        with self._write_operation("upsert_edge", commit=commit):
            return self._upsert_edge_impl(edge)

    def _upsert_edge_impl(self, edge: Edge) -> str:
        # Revalidate mutable dataclass fields at the persistence boundary.
        for name in (
            "valid_from",
            "valid_to",
            "ingested_at",
            "expired_at",
            "valid_to_recorded_at",
        ):
            setattr(edge, name, _finite_timestamp(getattr(edge, name), name))
        edge.weight = _finite_number(edge.weight, "weight")
        eid = edge.id or ids.new_id("edge")
        edge_valid_from = edge.valid_from if edge.valid_from is not None else now_ts()
        if edge.valid_to is not None and edge.valid_to < edge_valid_from:
            raise ValueError("edge valid_to cannot predate valid_from")
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
             edge.weight, edge_valid_from,
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
        return eid

    def invalidate_edge(self, edge_id: str, at: Optional[float] = None, *,
                        commit: bool = True) -> None:
        """Close an edge and its supports at one finite world/system-time boundary."""
        with self._write_operation("invalidate_edge", commit=commit):
            recorded_at = _finite_timestamp(now_ts(), "recorded_at")
            ts = _finite_timestamp(recorded_at if at is None else at, "at")
            row = self.conn.execute(
                "SELECT valid_from FROM edges WHERE id=?", (edge_id,)
            ).fetchone()
            if (
                row is not None
                and row["valid_from"] is not None
                and ts < row["valid_from"]
            ):
                # A caller may supply an old world-time anchor for an edge whose
                # implicit start was recorded at ingestion. Clamp it to preserve
                # a non-empty interval without admitting non-finite timestamps.
                ts = row["valid_from"]
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

    def _write_edge_supports(self, edge_id: str, relation: str, provenance: dict,
                             *, valid_from: Optional[float] = None,
                             valid_to: Optional[float] = None,
                             valid_to_recorded_at: Optional[float] = None,
                             ingested_at: Optional[float] = None,
                             expired_at: Optional[float] = None) -> None:
        valid_from = _finite_timestamp(valid_from, "valid_from")
        valid_to = _finite_timestamp(valid_to, "valid_to")
        valid_to_recorded_at = _finite_timestamp(
            valid_to_recorded_at, "valid_to_recorded_at"
        )
        ingested_at = _finite_timestamp(ingested_at, "ingested_at")
        expired_at = _finite_timestamp(expired_at, "expired_at")
        source_kind = _edge_source_kind(provenance, relation)
        confidence = _edge_support_confidence(provenance, source_kind)
        support_provenance = _merge_edge_provenance([provenance])
        support_provenance["confidence"] = confidence
        timestamp = now_ts()
        support_valid_from = valid_from if valid_from is not None else timestamp
        support_ingested_at = ingested_at if ingested_at is not None else timestamp
        if valid_to is not None and valid_to < support_valid_from:
            raise ValueError("edge support valid_to cannot predate valid_from")
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
        """Record support and edge provenance as one write unit."""
        with self._write_operation("add_edge_support", commit=commit):
            self._add_edge_support_impl(
                edge_id,
                provenance,
                valid_from=valid_from,
                ingested_at=ingested_at,
            )

    def _add_edge_support_impl(self, edge_id: str, provenance: dict, *,
                               valid_from: Optional[float] = None,
                               ingested_at: Optional[float] = None) -> None:
        """Record another source memory supporting an existing graph edge."""
        valid_from = _finite_timestamp(valid_from, "valid_from")
        ingested_at = _finite_timestamp(ingested_at, "ingested_at")
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

    def invalidate_edges_for_memory(
        self, memory_id: str, *, at: Optional[float] = None,
        commit: bool = True,
    ) -> None:
        """Atomically retire one memory's support from derived graph edges."""
        with self._write_operation("invalidate_edges_for_memory", commit=commit):
            self._invalidate_edges_for_memory_impl(
                memory_id, at=at, commit=False
            )

    def _invalidate_edges_for_memory_impl(
        self, memory_id: str, *, at: Optional[float] = None,
        commit: bool = False,
    ) -> None:
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
        recorded_at = _finite_timestamp(now_ts(), "recorded_at")
        ts = _finite_timestamp(recorded_at if at is None else at, "at")
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

    def retire_memory_graph_state(
        self,
        memory_id: str,
        *,
        at: Optional[float] = None,
        preserve_link_relations: Iterable[str] = (),
        commit: bool = True,
    ) -> None:
        """Atomically retire every derived graph surface for one memory."""
        with self._write_operation("retire_memory_graph_state", commit=commit):
            self._retire_memory_graph_state_impl(
                memory_id,
                at=at,
                preserve_link_relations=preserve_link_relations,
                commit=False,
            )

    def _retire_memory_graph_state_impl(
        self,
        memory_id: str,
        *,
        at: Optional[float] = None,
        preserve_link_relations: Iterable[str] = (),
        commit: bool = False,
    ) -> None:
        """Close live graph derivatives of one memory without deleting their history.

        A trust downgrade can leave the memory itself valid for inspection while making
        its previously trusted graph evidence unsafe to traverse. Retire every current
        support, incidence, and memory/code link at one scan-time boundary so historical
        reads remain explainable but current graph recall cannot route through it.
        ``preserve_link_relations`` keeps explicitly named audit/lineage relations live
        while retiring associative links such as automatic evolution bridges.
        """
        recorded_at = _finite_timestamp(now_ts(), "recorded_at")
        ts = _finite_timestamp(recorded_at if at is None else at, "at")
        self.invalidate_edges_for_memory(memory_id, at=ts, commit=False)
        self.conn.execute(
            "UPDATE memory_entities SET valid_to=?, valid_to_recorded_at=? "
            "WHERE memory_id=? AND valid_to IS NULL AND expired_at IS NULL",
            (ts, recorded_at, memory_id),
        )
        preserved = tuple(dict.fromkeys(
            str(relation) for relation in preserve_link_relations if str(relation)
        ))
        link_sql = (
            "UPDATE mem_links SET valid_to=?, valid_to_recorded_at=? "
            "WHERE (a=? OR b=?) AND valid_to IS NULL AND expired_at IS NULL"
        )
        link_params: tuple[Any, ...] = (ts, recorded_at, memory_id, memory_id)
        if preserved:
            marks = ",".join("?" for _ in preserved)
            link_sql += f" AND relation NOT IN ({marks})"
            link_params = (*link_params, *preserved)
        self.conn.execute(link_sql, link_params)
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

    @staticmethod
    def _validate_memory_link_owner_rows(
        first,
        second,
        relation: str,
        *,
        allow_scope_transition: bool,
    ) -> None:
        """Require shared workspace; prove cross-owner promotion/merge lineage."""
        if first["workspace_id"] != second["workspace_id"]:
            raise ValueError("memory link endpoints must share workspace ownership")
        first_owner = (
            first["repo_id"], first["session_id"], str(first["scope"] or "")
        )
        second_owner = (
            second["repo_id"], second["session_id"], str(second["scope"] or "")
        )
        if first_owner == second_owner or relation not in {"promotes", "merges"}:
            return
        if not allow_scope_transition:
            raise ValueError(
                "governed promotion or merge requires explicit scope-transition "
                "authorization"
            )
        rank = {
            Scope.SESSION.value: 0,
            Scope.REPO.value: 1,
            Scope.WORKSPACE.value: 2,
            Scope.USER.value: 3,
        }
        first_rank = rank.get(str(first["scope"]), -1)
        second_rank = rank.get(str(second["scope"]), -1)
        # Allow same-rank links within the same workspace (cross-repo is OK at workspace scope)
        if first_rank < second_rank:
            raise ValueError(
                "governed memory link must point from the wider result to its source"
            )
        if first_rank == second_rank and first["workspace_id"] != second["workspace_id"]:
            raise ValueError(
                "governed memory link must point from the wider result to its source"
            )
        metadata = _loads(first["metadata"], {})
        provenance = _loads(first["provenance"], {})
        nested = metadata.get("provenance") if isinstance(metadata, dict) else {}
        if not isinstance(nested, dict):
            nested = {}
        if relation == "promotes":
            evidence = metadata.get("promoted_from", []) if isinstance(metadata, dict) else []
        else:
            evidence = (
                provenance.get("merges")
                or nested.get("merges")
                or (metadata.get("supersedes") if isinstance(metadata, dict) else [])
                or []
            )
        if not isinstance(evidence, list) or second["id"] not in evidence:
            raise ValueError(
                f"governed {relation} link lacks persisted source evidence"
            )

    def _validate_memory_link_endpoints(
        self,
        a: str,
        b: str,
        relation: str,
        *,
        allow_scope_transition: bool,
    ) -> None:
        """Require durable endpoint ownership or a proven governed widening."""
        rows = self.conn.execute(
            "SELECT id, workspace_id, repo_id, session_id, scope, metadata, provenance "
            "FROM memories WHERE id IN (?,?)",
            (a, b),
        ).fetchall()
        records = {str(row["id"]): row for row in rows}
        if a not in records or b not in records:
            raise ValueError("memory link endpoints must exist")
        self._validate_memory_link_owner_rows(
            records[a],
            records[b],
            relation,
            allow_scope_transition=allow_scope_transition,
        )

    def _filter_memory_links_by_ownership(self, rows: list[dict]) -> list[dict]:
        """Fail closed on legacy/direct-SQL links that cross an unproven boundary."""
        if not rows:
            return []
        endpoint_ids = sorted({
            endpoint for row in rows for endpoint in (row["a"], row["b"])
        })
        records = {}
        for start in range(0, len(endpoint_ids), IN_CLAUSE_CHUNK):
            chunk = endpoint_ids[start:start + IN_CLAUSE_CHUNK]
            marks = ",".join("?" for _ in chunk)
            for record in self.conn.execute(
                "SELECT id, workspace_id, repo_id, session_id, scope, "
                f"metadata, provenance FROM memories WHERE id IN ({marks})",
                chunk,
            ):
                records[str(record["id"])] = record
        valid = []
        for row in rows:
            first = records.get(row["a"])
            second = records.get(row["b"])
            if first is None or second is None:
                continue
            try:
                self._validate_memory_link_owner_rows(
                    first,
                    second,
                    str(row["relation"]),
                    allow_scope_transition=True,
                )
            except ValueError:
                continue
            valid.append(row)
        return valid

    def _memory_link_endpoint_visibility(
        self, flt: Optional[SearchFilter], *, include_invalid: bool,
    ) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        for alias in ("ma", "mb"):
            where, values = self._where(
                flt, include_invalid=include_invalid, alias=alias
            )
            clauses.extend(where)
            params.extend(values)
        ordinary = " AND ".join(clauses)
        if include_invalid:
            return ordinary, params

        # Promotion and merge links intentionally keep retired source history
        # queryable from the live successor. They still require both endpoints
        # to satisfy every non-temporal scope/type predicate.
        historical_clauses: list[str] = []
        historical_params: list[Any] = []
        for alias in ("ma", "mb"):
            where, values = self._where(
                flt, include_invalid=True, alias=alias
            )
            historical_clauses.extend(where)
            historical_params.extend(values)
        lineage = "l.relation IN ('promotes','merges')"
        if historical_clauses:
            lineage += " AND " + " AND ".join(historical_clauses)
        return f"(({ordinary}) OR ({lineage}))", params + historical_params

    def add_link(self, a: str, b: str, relation: str = "related",
                 layer: Optional[GraphLayer] = None, reason: str = "",
                 *, valid_from: Optional[float] = None,
                 valid_to: Optional[float] = None,
                 valid_to_recorded_at: Optional[float] = None,
                 ingested_at: Optional[float] = None,
                 expired_at: Optional[float] = None,
                 commit: bool = True,
                 allow_scope_transition: bool = False) -> None:
        """Idempotent per (pair, relation): re-linking the same two memories with the
        same relation is a no-op in either direction, so auto-evolution and explicit
        ``engraphis_link`` calls can't accrete duplicate rows."""
        valid_from = _finite_timestamp(valid_from, "valid_from")
        valid_to = _finite_timestamp(valid_to, "valid_to")
        valid_to_recorded_at = _finite_timestamp(
            valid_to_recorded_at, "valid_to_recorded_at"
        )
        ingested_at = _finite_timestamp(ingested_at, "ingested_at")
        expired_at = _finite_timestamp(expired_at, "expired_at")
        reject_secrets((("link reason", reason),))
        requested_layer = (
            normalize_graph_layer(layer, relation).value
            if layer is not None else None
        )
        graph_layer = requested_layer or normalize_graph_layer(None, relation).value
        stamp = now_ts()
        world_start = stamp if valid_from is None else valid_from
        system_start = stamp if ingested_at is None else ingested_at
        if valid_to is not None and valid_to < world_start:
            raise ValueError("link valid_to cannot predate valid_from")
        owns_transaction = not self.conn.transaction_owned_by_current_thread()
        savepoint = ""
        if owns_transaction:
            self.conn.execute("BEGIN IMMEDIATE")
        else:
            savepoint = (
                f"engraphis_add_link_{threading.get_ident()}_{time.monotonic_ns()}"
            )
            self.conn.execute(f"SAVEPOINT {savepoint}")
        try:
            self._validate_memory_link_endpoints(
                a, b, relation, allow_scope_transition=allow_scope_transition
            )
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
                if savepoint:
                    self.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                elif owns_transaction:
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
                    if savepoint:
                        self.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                    elif commit:
                        self.conn.commit()
                else:
                    # The pre-read reservation has no write to batch. Release it even
                    # for ``commit=False``; the old no-op path never opened a transaction.
                    if savepoint:
                        self.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                    elif owns_transaction:
                        self.conn.commit()
                return
            self.conn.execute(
                "INSERT INTO mem_links("
                "a, b, relation, layer, reason, created_at, valid_from, valid_to, "
                "valid_to_recorded_at, ingested_at, expired_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (a, b, relation, graph_layer, reason, stamp, world_start, valid_to,
                 valid_to_recorded_at, system_start, expired_at),
            )
            if savepoint:
                self.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            elif commit:
                self.conn.commit()
        except BaseException:
            if savepoint and self.conn.transaction_owned_by_current_thread():
                self.conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                self.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            elif owns_transaction and self.conn.transaction_owned_by_current_thread():
                self.conn.rollback()
            raise

    def add_link_version(self, a: str, b: str, relation: str = "related",
                         layer: Optional[GraphLayer] = None, reason: str = "", *,
                         valid_from: Optional[float] = None,
                         valid_to: Optional[float] = None,
                         valid_to_recorded_at: Optional[float] = None,
                         ingested_at: Optional[float] = None,
                         expired_at: Optional[float] = None,
                         commit: bool = True,
                         allow_scope_transition: bool = False) -> bool:
        """Persist one exact temporal link version without collapsing live evidence.

        Normal :meth:`add_link` intentionally de-duplicates active relationships for
        interactive callers. Sync is different: two peers can independently observe the
        same relation with distinct valid/known intervals, and both intervals are needed
        for a convergent historical graph. This method appends that exact observation and
        returns whether it was new, while replaying the same version remains a no-op.
        """
        valid_from = _finite_timestamp(valid_from, "valid_from")
        valid_to = _finite_timestamp(valid_to, "valid_to")
        valid_to_recorded_at = _finite_timestamp(
            valid_to_recorded_at, "valid_to_recorded_at"
        )
        ingested_at = _finite_timestamp(ingested_at, "ingested_at")
        expired_at = _finite_timestamp(expired_at, "expired_at")
        reject_secrets((("link reason", reason),))
        graph_layer = normalize_graph_layer(layer, relation).value
        stamp = now_ts()
        world_start = stamp if valid_from is None else valid_from
        system_start = stamp if ingested_at is None else ingested_at
        if valid_to is not None and valid_to < world_start:
            raise ValueError("link valid_to cannot predate valid_from")
        owns_transaction = not self.conn.transaction_owned_by_current_thread()
        savepoint = ""
        if owns_transaction:
            self.conn.execute("BEGIN IMMEDIATE")
        else:
            savepoint = (
                f"engraphis_add_link_version_{threading.get_ident()}_"
                f"{time.monotonic_ns()}"
            )
            self.conn.execute(f"SAVEPOINT {savepoint}")
        try:
            self._validate_memory_link_endpoints(
                a, b, relation, allow_scope_transition=allow_scope_transition
            )
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
                if savepoint:
                    self.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                elif owns_transaction:
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
            if savepoint:
                self.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            elif commit:
                self.conn.commit()
            return True
        except BaseException:
            if savepoint and self.conn.transaction_owned_by_current_thread():
                self.conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                self.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            elif owns_transaction and self.conn.transaction_owned_by_current_thread():
                self.conn.rollback()
            raise

    def has_link(self, a: str, b: str, *, relation: Optional[str] = None) -> bool:
        """Return whether the pair has a current open link interval.

        Closed history must not block a later reactivation of the same relationship.
        Historical visibility remains available through ``get_links``/``links_among``.
        """
        sql = (
            "SELECT a, b, relation FROM mem_links "
            "WHERE ((a=? AND b=?) OR (a=? AND b=?)) "
            "AND valid_to IS NULL AND expired_at IS NULL"
        )
        params: list[Any] = [a, b, b, a]
        if relation is not None:
            sql += " AND relation=?"
            params.append(relation)
        rows = [dict(row) for row in self.conn.execute(sql, params).fetchall()]
        return bool(self._filter_memory_links_by_ownership(rows))

    def get_links(self, memory_id: str, *,
                  flt: Optional[SearchFilter] = None) -> list[dict]:
        """Return direct links only when both endpoints are visible to ``flt``."""
        link_sql, link_params = _temporal_visibility_sql("l", flt)
        endpoint_sql, endpoint_params = self._memory_link_endpoint_visibility(
            flt, include_invalid=False
        )
        sql = (
            "SELECT l.a, l.b, l.relation, l.layer, l.reason, l.created_at, "
            "l.valid_from, l.valid_to, l.valid_to_recorded_at, l.ingested_at, "
            "l.expired_at FROM mem_links AS l "
            "JOIN memories AS ma ON ma.id=l.a "
            "JOIN memories AS mb ON mb.id=l.b "
            f"WHERE (l.a=? OR l.b=?) AND {link_sql}"
        )
        params: list[Any] = [memory_id, memory_id, *link_params]
        if endpoint_sql:
            sql += " AND " + endpoint_sql
            params.extend(endpoint_params)
        sql += " ORDER BY l.a, l.b, l.relation"
        rows = [dict(row) for row in self.conn.execute(sql, params).fetchall()]
        return self._filter_memory_links_by_ownership(rows)

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
        """Return links whose two endpoints and interval are visible to ``flt``."""
        if not ids or (layers is not None and not layers):
            return []
        row_cap = None if limit is None else max(0, int(limit))
        if row_cap == 0:
            return []
        wanted = set(ids)
        ordered_ids = sorted(wanted)
        visibility_sql, visibility_params = _temporal_visibility_sql("l", flt)
        endpoint_sql, endpoint_params = self._memory_link_endpoint_visibility(
            flt, include_invalid=include_invalid
        )
        rows: list[dict] = []
        # Leave headroom for temporal, endpoint-scope, and layer parameters.
        chunk_size = max(1, IN_CLAUSE_CHUNK - 32)
        for start in range(0, len(ordered_ids), chunk_size):
            if row_cap is not None and len(rows) >= row_cap:
                break
            chunk = ordered_ids[start:start + chunk_size]
            marks = ",".join("?" for _ in chunk)
            sql = (
                "SELECT l.a, l.b, l.relation, l.layer, l.reason, l.created_at, "
                "l.valid_from, l.valid_to, l.valid_to_recorded_at, l.ingested_at, "
                "l.expired_at FROM mem_links AS l "
                "JOIN memories AS ma ON ma.id=l.a "
                "JOIN memories AS mb ON mb.id=l.b "
                f"WHERE l.a IN ({marks})"
            )
            params: list[Any] = [*chunk]
            if not include_invalid:
                sql += f" AND {visibility_sql}"
                params.extend(visibility_params)
            if endpoint_sql:
                sql += " AND " + endpoint_sql
                params.extend(endpoint_params)
            if layers is not None:
                layer_marks = ",".join("?" for _ in layers)
                sql += f" AND l.layer IN ({layer_marks})"
                params.extend(_enum(layer) for layer in layers)
            sql += " ORDER BY l.a, l.b, l.relation, l.valid_from, l.ingested_at"
            found = [
                dict(row) for row in self.conn.execute(sql, params).fetchall()
            ]
            for row in self._filter_memory_links_by_ownership(found):
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
                       limit: Optional[int] = None,
                       prompt_only: bool = False) -> list[dict]:
        """Return visible links touching ``ids`` without exposing a foreign endpoint."""
        if not ids or (layers is not None and not layers):
            return []
        row_cap = None if limit is None else max(0, int(limit))
        if row_cap == 0:
            return []
        ordered_ids = sorted(set(ids))
        visibility_sql, visibility_params = _temporal_visibility_sql("l", flt)
        endpoint_sql, endpoint_params = self._memory_link_endpoint_visibility(
            flt, include_invalid=include_invalid
        )
        rows: list[dict] = []
        seen: set[tuple] = set()
        # Each seed appears in both endpoint predicates; reserve bindings for filters.
        chunk_size = max(1, (IN_CLAUSE_CHUNK - 32) // 2)
        for start in range(0, len(ordered_ids), chunk_size):
            if row_cap is not None and len(rows) >= row_cap:
                break
            chunk = ordered_ids[start:start + chunk_size]
            marks = ",".join("?" for _ in chunk)
            sql = (
                "SELECT l.a, l.b, l.relation, l.layer, l.reason, l.created_at, "
                "l.valid_from, l.valid_to, l.valid_to_recorded_at, l.ingested_at, "
                "l.expired_at FROM mem_links AS l "
                "JOIN memories AS ma ON ma.id=l.a "
                "JOIN memories AS mb ON mb.id=l.b "
                f"WHERE (l.a IN ({marks}) OR l.b IN ({marks}))"
            )
            params: list[Any] = [*chunk, *chunk]
            if not include_invalid:
                sql += f" AND {visibility_sql}"
                params.extend(visibility_params)
            if endpoint_sql:
                sql += " AND " + endpoint_sql
                params.extend(endpoint_params)
            if layers is not None:
                layer_marks = ",".join("?" for _ in layers)
                sql += f" AND l.layer IN ({layer_marks})"
                params.extend(_enum(layer) for layer in layers)
            sql += " ORDER BY l.a, l.b, l.relation, l.valid_from, l.ingested_at"
            found = [dict(row) for row in self.conn.execute(sql, params).fetchall()]
            found = self._filter_memory_links_by_ownership(found)
            endpoint_ids = {
                endpoint for item in found for endpoint in (item["a"], item["b"])
            }
            endpoint_records = (
                self.get_memories(sorted(endpoint_ids)) if prompt_only else {}
            )
            for item in found:
                if prompt_only and not all(
                    (record := endpoint_records.get(endpoint))
                    and _row_is_prompt_eligible(record.provenance, record.metadata)
                    for endpoint in (item["a"], item["b"])
                ):
                    continue
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
                  limit: Optional[int] = None,
                  prompt_only: bool = False) -> list[Edge]:
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
        row_cap = None if limit is None else max(0, int(limit))
        if row_cap == 0:
            return []
        sql += " ORDER BY id"
        if not prompt_only:
            if row_cap is not None:
                sql += " LIMIT ?"
                params.append(row_cap)
            rows = self.conn.execute(sql, params).fetchall()
            return [_row_to_edge(r) for r in rows]

        # Prompt-facing graph traversal must not let unreviewed edge evidence use
        # up the frontier before eligibility is checked. Page raw rows in stable
        # order and count only prompt-safe edges toward the caller's cap.
        selected: list[Edge] = []
        offset = 0
        page_size = min(1_000, row_cap or 1_000)
        while row_cap is None or len(selected) < row_cap:
            rows = self.conn.execute(
                sql + " LIMIT ? OFFSET ?", (*params, page_size, offset)
            ).fetchall()
            if not rows:
                break
            edges = [_row_to_edge(row) for row in rows]
            source_ids = set().union(*(
                set(_provenance_memory_ids(edge.provenance)) for edge in edges
            )) if edges else set()
            memories = self.get_memories(sorted(source_ids))
            for edge in edges:
                if not _edge_is_prompt_eligible(edge.provenance):
                    continue
                sources = _provenance_memory_ids(edge.provenance)
                if sources and not all(
                    (memory := memories.get(memory_id))
                    and _row_is_prompt_eligible(memory.provenance, memory.metadata)
                    for memory_id in sources
                ):
                    continue
                selected.append(edge)
                if row_cap is not None and len(selected) >= row_cap:
                    break
            offset += len(rows)
            if len(rows) < page_size:
                break
        return selected

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
                          flt: Optional[SearchFilter] = None,
                          limit: Optional[int] = None) -> list[dict]:
        """Return visible symbols for files, honoring a hard result sentinel."""
        files = sorted(set(file for file in files if file))
        if not files or (limit is not None and int(limit) <= 0):
            return []
        result: list[dict] = []
        for start in range(0, len(files), IN_CLAUSE_CHUNK):
            chunk = files[start:start + IN_CLAUSE_CHUNK]
            marks = ",".join("?" for _ in chunk)
            temporal, params = _temporal_visibility_sql("", flt)
            sql = (
                f"SELECT * FROM symbols WHERE repo_id=? AND file IN ({marks}) "
                f"AND {temporal} ORDER BY file, fqname, id"
            )
            query_params: list[Any] = [repo_id, *chunk, *params]
            if limit is not None:
                remaining = int(limit) - len(result)
                if remaining <= 0:
                    break
                sql += " LIMIT ?"
                query_params.append(remaining)
            result.extend(
                dict(row) for row in self.conn.execute(sql, query_params).fetchall()
            )
        return result

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
        """Retire bridges whose source is not live and explicitly approved."""
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
        unapproved = self.conn.execute(
            "SELECT l.id, m.provenance, m.metadata FROM code_memory_links l "
            "JOIN memories m ON m.id=l.memory_id WHERE l.repo_id=? "
            "AND l.valid_to IS NULL AND l.expired_at IS NULL",
            (repo_id,),
        ).fetchall()
        retire_ids = [
            row["id"] for row in unapproved
            if not _row_is_prompt_eligible(row["provenance"], row["metadata"])
        ]
        if retire_ids:
            marks = ",".join("?" for _ in retire_ids)
            self.conn.execute(
                f"UPDATE code_memory_links SET valid_to=?, valid_to_recorded_at=? "
                f"WHERE id IN ({marks}) AND valid_to IS NULL AND expired_at IS NULL",
                (t, t, *retire_ids),
            )
        if commit:
            self.conn.commit()


    def list_code_memory_links(self, repo_id: str, *,
                               flt: Optional[SearchFilter] = None,
                               limit: Optional[int] = None) -> list[dict]:
        sql = (
            "SELECT l.*, s.name, s.fqname, s.file, s.kind AS symbol_kind, "
            "m.title, m.mtype, m.provenance, m.metadata, m.valid_to AS memory_valid_to, "
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
        if limit is not None and int(limit) <= 0:
            return []
        # This bridge feeds export/code-path/scene features. Filter each source before
        # counting it, so pending links cannot exhaust the public result cap.
        eligible_limit = None if limit is None else int(limit)
        out = []
        for row in self.conn.execute(sql, params):
            if not _row_is_prompt_eligible(row["provenance"], row["metadata"]):
                continue
            out.append({
                key: value for key, value in dict(row).items()
                if key not in {"metadata", "provenance"}
            })
            if eligible_limit is not None and len(out) >= eligible_limit:
                break
        return out

    def memories_for_symbol(self, repo_id: str, symbol_id: str, *,
                            flt: Optional[SearchFilter] = None,
                            limit: int = 20) -> list[dict]:
        sql = (
            "SELECT m.id, m.title, m.content, m.mtype, m.scope, m.importance, "
            "m.provenance, m.metadata, l.relation, l.confidence "
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
        sql += " ORDER BY l.confidence DESC, m.importance DESC, m.ingested_at DESC, l.id, m.id"
        row_limit = max(1, min(100, int(limit)))
        out = []
        for row in self.conn.execute(sql, params):
            item = dict(row)
            if not _row_is_prompt_eligible(item.get("provenance"), item.get("metadata")):
                continue
            item["provenance"] = _loads(item.get("provenance"), {})
            item.pop("metadata", None)
            out.append(item)
            if len(out) >= row_limit:
                break
        return out

    def memories_for_symbols(self, repo_id: str, symbol_ids: list[str], *,
                             flt: Optional[SearchFilter] = None,
                             limit: int = 20) -> dict[str, list[dict]]:
        """Return bounded prompt-safe memory rankings with indexed per-symbol lookups.

        A window-function query with an outer ``row_rank`` cap still makes SQLite
        sort every matching partition before it can apply that cap.  Issuing one
        indexed, limited lookup per requested symbol instead gives the prompt-facing
        path a real physical bound even when an untrusted import owns many links.
        """
        unique_ids = list(dict.fromkeys(
            str(symbol_id) for symbol_id in symbol_ids if str(symbol_id)
        ))[:500]
        if not unique_ids:
            return {}
        grouped: dict[str, list[dict]] = {}
        for symbol_id in unique_ids:
            rows = self.memories_for_symbol(repo_id, symbol_id, flt=flt, limit=limit)
            if rows:
                grouped[symbol_id] = rows
        return grouped

    def symbols_for_memory(self, repo_id: str, memory_id: str, *,
                           flt: Optional[SearchFilter] = None) -> list[dict]:
        memory = self.get_memory(memory_id)
        if memory is None or not _row_is_prompt_eligible(memory.provenance, memory.metadata):
            return []
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
        if limit <= 0:
            return []
        escaped = str(text).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        sql = (
            "SELECT m.id, m.title, m.mtype, m.provenance, m.metadata FROM memories AS m "
            "WHERE m.repo_id=? AND (m.title LIKE ? ESCAPE '\\' "
            "OR m.content LIKE ? ESCAPE '\\')"
        )
        pattern = f"%{escaped}%"
        params: list[Any] = [repo_id, pattern, pattern]
        where, visibility_params = self._where(flt, include_invalid=False, alias="m")
        if where:
            sql += " AND " + " AND ".join(where)
            params.extend(visibility_params)
        sql += " ORDER BY m.ingested_at DESC"
        # This derived bridge feeds impact analysis. Filter sources before counting
        # them, so a newer pending import cannot consume the bounded public window.
        out = []
        for row in self.conn.execute(sql, params):
            if not _row_is_prompt_eligible(row["provenance"], row["metadata"]):
                continue
            out.append({
                key: value for key, value in dict(row).items()
                if key not in {"provenance", "metadata"}
            })
            if len(out) >= limit:
                break
        return out

    # ── events & audit ──────────────────────────────────────────────────────
    def append_event(self, *, kind: str, content: str, workspace_id: str = "",
                     repo_id: str = "", session_id: str = "", refs: Optional[list] = None,
                     interaction_level: str = "") -> str:
        # Events are not memories, but are durable, searchable agent context too. Do
        # not create a side channel that can retain a credential after memory capture is
        # blocked.
        reject_secrets((("event content", content), ("event refs", refs)))
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
            transaction_started = not self.conn.transaction_owned_by_current_thread()
            try:
                if transaction_started:
                    self.conn.execute("BEGIN IMMEDIATE")
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
                if transaction_started:
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

    def context_savings(
        self,
        *,
        workspace_id: str,
        repo_id: Optional[str] = None,
        from_ts: Optional[float] = None,
        to_ts: Optional[float] = None,
        release_version: Optional[str] = None,
    ) -> dict:
        """Aggregate validated, content-free context usage from scoped receipts.

        Token counts are kept separate by counter identity: a tokenizer change must not turn
        into a misleading cumulative total. Invalid, missing, and incomplete receipts remain
        visible only as counts; their payload is never reflected into this summary. The
        workspace-wide receipt-chain validity is returned alongside any repo-scoped aggregate
        so callers can distinguish useful local accounting from evidence eligible for audit.
        """
        if from_ts is not None and not math.isfinite(float(from_ts)):
            raise ValueError("from_ts must be finite")
        if to_ts is not None and not math.isfinite(float(to_ts)):
            raise ValueError("to_ts must be finite")
        if from_ts is not None and to_ts is not None and from_ts > to_ts:
            raise ValueError("from_ts must be less than or equal to to_ts")
        if release_version is not None:
            normalized_release = normalize_release_version(release_version)
            if not normalized_release:
                raise ValueError("release_version must be a semantic version")
            release_version = normalized_release
        verification = self.verify_receipts(workspace_id=workspace_id)
        where = "workspace_id=?"
        params: list[Any] = [workspace_id]
        if repo_id is not None:
            where += " AND repo_id=?"
            params.append(repo_id)
        if from_ts is not None:
            where += " AND ts>=?"
            params.append(float(from_ts))
        if to_ts is not None:
            where += " AND ts<?"
            params.append(float(to_ts))
        rows = self.conn.execute(
            "SELECT id, repo_id, ts, payload, prev_hash, receipt_hash "
            "FROM operation_receipts WHERE " + where,
            params,
        ).fetchall()
        totals = {
            "receipt_count": 0,
            "usage_receipt_count": 0,
            "savings_receipt_count": 0,
            "invalid_receipt_count": 0,
            "incomplete_usage_receipt_count": 0,
        }
        buckets: dict[str, dict] = {}
        estimate_totals = {
            "eligible_receipt_count": 0,
            "excluded_receipt_count": 0,
            "unclassified_receipt_count": 0,
            "invalid_estimate_count": 0,
            "baseline_tokens": 0,
            "emitted_tokens": 0,
            "saved_tokens": 0,
            "_bases": {},
            "_counters": {},
        }

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

        def nonnegative_builtin_number(value: object) -> Optional[int | float]:
            # Metadata is untrusted persisted JSON. Use exact built-in numeric
            # types to preserve the receipt format's existing contract.
            if type(value) is int or type(value) is float:
                return value if value >= 0 else None
            return None

        def add(target: dict, usage: dict, operation: str) -> None:
            target["receipt_count"] += 1
            for key in (
                "source_tokens", "context_tokens", "saved_tokens", "budget_tokens",
                "packed_count", "omitted_count",
            ):
                value = nonnegative_builtin_number(usage.get(key))
                if value is not None:
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
                value = nonnegative_builtin_number(usage.get(key))
                if value is not None:
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

        def estimate_bucket(container: dict, key: str, confidence: str) -> dict:
            return container.setdefault(key, {
                "basis": key,
                "confidence": confidence,
                "receipt_count": 0,
                "baseline_tokens": 0,
                "emitted_tokens": 0,
                "saved_tokens": 0,
            })

        def add_estimate(usage: dict) -> None:
            required = (
                "baseline_tokens", "emitted_tokens", "estimated_saved_tokens",
                "estimated_savings_ratio", "savings_basis", "savings_confidence",
                "savings_eligible",
            )
            if not all(key in usage for key in required):
                estimate_totals["unclassified_receipt_count"] += 1
                return
            numeric = (
                "baseline_tokens", "emitted_tokens", "estimated_saved_tokens",
                "estimated_savings_ratio",
            )
            if any(
                type(usage.get(key)) not in (int, float)
                or not math.isfinite(float(usage[key]))
                or usage[key] < 0
                for key in numeric
            ):
                estimate_totals["invalid_estimate_count"] += 1
                return
            if type(usage.get("savings_eligible")) is not bool:
                estimate_totals["invalid_estimate_count"] += 1
                return
            basis = usage.get("savings_basis")
            confidence = usage.get("savings_confidence")
            if not isinstance(basis, str) or not isinstance(confidence, str):
                estimate_totals["invalid_estimate_count"] += 1
                return
            if (
                basis not in _PUBLIC_RECEIPT_LABELS_BY_KEY["savings_basis"]
                or confidence not in _PUBLIC_RECEIPT_LABELS_BY_KEY["savings_confidence"]
            ):
                estimate_totals["invalid_estimate_count"] += 1
                return
            baseline = int(usage["baseline_tokens"])
            emitted = int(usage["emitted_tokens"])
            saved = int(usage["estimated_saved_tokens"])
            expected_saved = max(0, baseline - emitted) if usage["savings_eligible"] else 0
            expected_ratio = expected_saved / baseline if baseline else 0.0
            if (
                saved != expected_saved
                or saved > baseline
                or not math.isclose(
                    float(usage["estimated_savings_ratio"]),
                    expected_ratio,
                    rel_tol=0.0,
                    abs_tol=1e-9,
                )
            ):
                estimate_totals["invalid_estimate_count"] += 1
                return
            if not usage["savings_eligible"]:
                estimate_totals["excluded_receipt_count"] += 1
                return
            counter = str(usage.get("token_counter") or "unknown")
            estimate_totals["eligible_receipt_count"] += 1
            estimate_totals["baseline_tokens"] += baseline
            estimate_totals["emitted_tokens"] += emitted
            estimate_totals["saved_tokens"] += saved
            basis_bucket = estimate_bucket(estimate_totals["_bases"], basis, confidence)
            basis_bucket["receipt_count"] += 1
            basis_bucket["baseline_tokens"] += baseline
            basis_bucket["emitted_tokens"] += emitted
            basis_bucket["saved_tokens"] += saved
            counter_bucket = estimate_bucket(
                estimate_totals["_counters"], counter, confidence
            )
            counter_bucket["receipt_count"] += 1
            counter_bucket["baseline_tokens"] += baseline
            counter_bucket["emitted_tokens"] += emitted
            counter_bucket["saved_tokens"] += saved

        def finish_estimate(target: dict, label: str) -> dict:
            target = dict(target)
            key = target.pop("basis")
            target[label] = key
            target["savings_ratio"] = (
                target["saved_tokens"] / target["baseline_tokens"]
                if target["baseline_tokens"] else 0.0
            )
            return target

        for raw_row in rows:
            receipt = _public_receipt_row(dict(raw_row))
            if (
                receipt.get("invalid_payload")
                or receipt.get("scope_digest")
                != _receipt_scope_digest(workspace_id, raw_row["repo_id"])
            ):
                if release_version is None:
                    totals["receipt_count"] += 1
                    totals["invalid_receipt_count"] += 1
                continue
            metadata = receipt.get("metadata")
            usage = metadata.get("token_usage") if isinstance(metadata, dict) else None
            operation = str(receipt["operation"])
            if release_version is not None and (
                operation == "smart_gateway"
                or not isinstance(usage, dict)
                or usage.get("release_version") != release_version
            ):
                continue
            totals["receipt_count"] += 1
            if not isinstance(usage, dict):
                continue
            # Smart gateway telemetry is supplementary to the authoritative classic
            # handler receipt. Older databases may contain copied token_usage here;
            # ignore it so those historical rows cannot double-count a delivery.
            if operation == "smart_gateway":
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
            add_estimate(usage)
        bases = [
            finish_estimate(value, "basis")
            for _, value in sorted(estimate_totals["_bases"].items())
        ]
        counters = [
            finish_estimate(value, "token_counter")
            for _, value in sorted(estimate_totals["_counters"].items())
        ]
        estimate_totals.pop("_bases")
        estimate_totals.pop("_counters")
        estimate_totals["savings_ratio"] = (
            estimate_totals["saved_tokens"] / estimate_totals["baseline_tokens"]
            if estimate_totals["baseline_tokens"] else 0.0
        )
        estimate_totals["by_basis"] = bases
        estimate_totals["by_token_counter"] = counters
        confidence_values = {row["confidence"] for row in bases}
        estimate_totals["confidence"] = (
            next(iter(confidence_values)) if len(confidence_values) == 1
            else "mixed" if confidence_values else "none"
        )
        return {
            **totals,
            "receipt_chain_valid": bool(verification["valid"]),
            "receipt_chain_error_count": len(verification["errors"]),
            "by_token_counter": [finished(value) for _, value in sorted(buckets.items())],
            "period": {"from_ts": from_ts, "to_ts": to_ts},
            "release_version": release_version,
            "estimated": estimate_totals,
        }


    def context_savings_grouped(
        self, *, workspace_id: str, repo_id: Optional[str] = None,
        group_by: str = "workspace",
        from_ts: Optional[float] = None,
        to_ts: Optional[float] = None,
        release_version: Optional[str] = None,
    ) -> list[dict]:
        """Aggregate context savings grouped by a dimension.

        Supported dimensions: ``workspace`` (single bucket), ``repo``,
        ``agent`` (actor digest), ``day`` (UTC date from receipt ts).
        Returns a list of dicts each containing the group key and the same
        token counters as :meth:`context_savings`. Each dimension bucket is
        additionally partitioned by token-counter identity so incompatible
        units are never added together. Receipts are privacy-safe: actor is a
        one-way digest, no query or memory content is exposed.
        """
        valid_dims = {"workspace", "repo", "agent", "day"}
        if group_by not in valid_dims:
            raise ValueError(f"group_by must be one of: {', '.join(sorted(valid_dims))}")
        if from_ts is not None and not math.isfinite(float(from_ts)):
            raise ValueError("from_ts must be finite")
        if to_ts is not None and not math.isfinite(float(to_ts)):
            raise ValueError("to_ts must be finite")
        if from_ts is not None and to_ts is not None and from_ts > to_ts:
            raise ValueError("from_ts must be less than or equal to to_ts")
        if release_version is not None:
            normalized_release = normalize_release_version(release_version)
            if not normalized_release:
                raise ValueError("release_version must be a semantic version")
            release_version = normalized_release
        where = "workspace_id=?"
        params: list[Any] = [workspace_id]
        if repo_id is not None:
            where += " AND repo_id=?"
            params.append(repo_id)
        if from_ts is not None:
            where += " AND ts>=?"
            params.append(float(from_ts))
        if to_ts is not None:
            where += " AND ts<?"
            params.append(float(to_ts))
        rows = self.conn.execute(
            "SELECT id, ts, repo_id, actor, payload, prev_hash, receipt_hash FROM operation_receipts WHERE " + where,
            params,
        ).fetchall()
        import time as _time
        groups: dict[tuple[str, str], dict] = {}

        def _bucket() -> dict:
            return {
                "receipt_count": 0, "source_tokens": 0, "context_tokens": 0,
                "saved_tokens": 0, "budget_tokens": 0, "packed_count": 0,
                "omitted_count": 0,
            }

        def _add(target: dict, usage: dict) -> None:
            target["receipt_count"] += 1
            for key in (
                "source_tokens", "context_tokens", "saved_tokens",
                "budget_tokens", "packed_count", "omitted_count",
            ):
                value = usage.get(key)
                if (
                    isinstance(value, (int, float))
                    and not isinstance(value, bool)
                    and value >= 0
                ):
                    target[key] += value

        for raw_row in rows:
            receipt = _public_receipt_row(dict(raw_row))
            if (
                receipt.get("invalid_payload")
                or receipt.get("scope_digest")
                != _receipt_scope_digest(workspace_id, raw_row["repo_id"])
            ):
                continue
            metadata = receipt.get("metadata")
            usage = metadata.get("token_usage") if isinstance(metadata, dict) else None
            operation = str(receipt["operation"])
            if release_version is not None and (
                operation == "smart_gateway"
                or not isinstance(usage, dict)
                or usage.get("release_version") != release_version
            ):
                continue
            if not isinstance(usage, dict):
                continue
            if operation == "smart_gateway":
                continue
            required = ("source_tokens", "context_tokens", "saved_tokens")
            if not all(
                type(usage.get(k)) in (int, float) and usage[k] >= 0
                for k in required
            ):
                continue
            expected_saved = max(
                0.0, float(usage["source_tokens"]) - float(usage["context_tokens"])
            )
            if not math.isclose(
                float(usage["saved_tokens"]),
                expected_saved,
                rel_tol=0.0,
                abs_tol=1e-9,
            ):
                continue
            if group_by == "workspace":
                key = workspace_id
            elif group_by == "repo":
                key = str(raw_row["repo_id"] or "(none)")
            elif group_by == "agent":
                key = str(raw_row["actor"] or "system")
            elif group_by == "day":
                try:
                    day = _time.strftime("%Y-%m-%d", _time.gmtime(float(raw_row["ts"])))
                except (TypeError, ValueError, OverflowError, OSError):
                    day = "unknown"
                key = day
            else:
                key = workspace_id
            token_counter = str(usage.get("token_counter") or "unknown")
            grp = groups.setdefault((key, token_counter), _bucket())
            _add(grp, usage)
        result = []
        for key, token_counter in sorted(groups):
            entry = {
                "group_key": key,
                "token_counter": token_counter,
                **groups[(key, token_counter)],
            }
            entry["savings_ratio"] = (
                entry["saved_tokens"] / entry["source_tokens"]
                if entry["source_tokens"] else 0.0
            )
            result.append(entry)
        return result


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

    def set_sync_state(self, key: str, value: str, *, commit: bool = True) -> None:
        self.conn.execute(
            "INSERT INTO sync_state(key, value, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, now_ts()),
        )
        if commit:
            self.conn.commit()


    # ── sync stats (per-device byte transfer counters) ─────────────────────────
    def add_sync_bytes(self, device_id: str, *, sent: int = 0,
                       received: int = 0, commit: bool = True) -> None:
        """Accumulate byte transfer counters for one device.

        Counters are monotonic and local-only — they never leave the device in a
        sync bundle. ``device_id`` is the origin device of the bytes (the local
        device for ``sent``, the remote device for ``received``)."""
        if sent < 0 or received < 0:
            raise ValueError("byte counters must be non-negative")
        if sent == 0 and received == 0:
            return
        now = now_ts()
        self.conn.execute(
            "INSERT INTO sync_stats(device_id, bytes_sent, bytes_received, updated_at) "
            "VALUES (?,?,?,?) "
            "ON CONFLICT(device_id) DO UPDATE SET "
            "bytes_sent=sync_stats.bytes_sent+excluded.bytes_sent, "
            "bytes_received=sync_stats.bytes_received+excluded.bytes_received, "
            "updated_at=excluded.updated_at",
            (device_id, sent, received, now),
        )
        if commit:
            self.conn.commit()

    def get_sync_stats(self) -> list[dict]:
        """Return per-device byte transfer counters (content-free telemetry).

        Returns only device_id and counters — no memory content, no PII."""
        rows = self.conn.execute(
            "SELECT device_id, bytes_sent, bytes_received, updated_at "
            "FROM sync_stats ORDER BY updated_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    # ── bounded maintenance cursors (local, never synced) ──────────────────────
    def get_maintenance_cursor(self, workspace_id: str, repo_id: Optional[str],
                                name: str) -> str:
        """Return the last keyset id visited by one scoped maintenance sweep."""
        row = self.conn.execute(
            "SELECT cursor FROM maintenance_cursors "
            "WHERE workspace_id=? AND repo_id=? AND name=?",
            (workspace_id, repo_id or "", name),
        ).fetchone()
        return str(row["cursor"]) if row else ""

    def set_maintenance_cursor(self, workspace_id: str, repo_id: Optional[str],
                               name: str, cursor: str, *, commit: bool = True) -> None:
        """Persist bounded-sweep progress without exposing it to sync peers."""
        normalized_cursor = str(cursor or "")
        scope = (workspace_id, repo_id or "", name)
        existing = self.conn.execute(
            "SELECT cursor FROM maintenance_cursors "
            "WHERE workspace_id=? AND repo_id=? AND name=?",
            scope,
        ).fetchone()
        if existing is not None and str(existing["cursor"] or "") == normalized_cursor:
            return
        if existing is None:
            self.conn.execute(
                "INSERT INTO maintenance_cursors("
                "workspace_id, repo_id, name, cursor, updated_at"
                ") VALUES (?,?,?,?,?)",
                (*scope, normalized_cursor, now_ts()),
            )
        else:
            self.conn.execute(
                "UPDATE maintenance_cursors SET cursor=?, updated_at=? "
                "WHERE workspace_id=? AND repo_id=? AND name=?",
                (normalized_cursor, now_ts(), *scope),
            )
        if commit:
            self.conn.commit()

    # ── durable sync-export proof ──────────────────────────────────────────────

    def mark_memories_sync_exported(
        self,
        memory_ids: Iterable[str],
        *,
        workspace_id: str,
        exported_at: Optional[float] = None,
        commit: bool = True,
    ) -> int:
        """Atomically record content-free proof that eligible live rows were exported.

        The bounded batch is intended to be called by the sync transaction only after
        those exact ids have been selected for transfer.  Markers survive secure erase.
        """
        if isinstance(memory_ids, (str, bytes)):
            raise TypeError("memory_ids must be an iterable of memory ids")
        if not isinstance(workspace_id, str) or not workspace_id:
            raise ValueError("workspace_id must be a non-empty string")
        unique: list[str] = []
        seen: set[str] = set()
        for memory_id in memory_ids:
            if not isinstance(memory_id, str) or not memory_id:
                raise ValueError("memory_ids must contain non-empty strings")
            if memory_id in seen:
                continue
            seen.add(memory_id)
            unique.append(memory_id)
            if len(unique) > IN_CLAUSE_CHUNK:
                raise ValueError(
                    f"memory_ids may contain at most {IN_CLAUSE_CHUNK} unique entries"
                )
        if not unique:
            return 0
        ts = _finite_timestamp(
            now_ts() if exported_at is None else exported_at, "exported_at"
        )
        if ts is None:
            raise AssertionError("normalized exported_at unexpectedly became null")
        with self._write_operation("mark_memories_sync_exported", commit=commit):
            marks = ",".join("?" for _ in unique)
            rows = self.conn.execute(
                "SELECT id, workspace_id, repo_id, scope, sensitivity "
                f"FROM memories WHERE id IN ({marks})",
                unique,
            ).fetchall()
            by_id = {str(row["id"]): row for row in rows}
            missing = [memory_id for memory_id in unique if memory_id not in by_id]
            if missing:
                raise ValueError("sync export marker targets must exist")
            existing_rows = self.conn.execute(
                "SELECT memory_id, workspace_id, repo_id, first_exported_at, "
                f"last_exported_at FROM memory_sync_exports WHERE memory_id IN ({marks})",
                unique,
            ).fetchall()
            existing_by_id = {
                str(row["memory_id"]): row for row in existing_rows
            }
            for memory_id in unique:
                row = by_id[memory_id]
                if row["workspace_id"] != workspace_id:
                    raise ValueError(
                        "sync export marker workspace does not own every memory"
                    )
                scope = str(row["scope"] or "")
                sensitivity = str(row["sensitivity"] or "secret")
                if (
                    scope not in (Scope.WORKSPACE.value, Scope.REPO.value)
                    or sensitivity not in ("normal", "sensitive")
                ):
                    raise ValueError(
                        "sync export markers require a shareable workspace/repo memory"
                    )
                if (
                    scope == Scope.REPO.value
                    and (not isinstance(row["repo_id"], str) or not row["repo_id"])
                ):
                    raise ValueError(
                        "sync export markers require a valid repository owner"
                    )
                repo_id = row["repo_id"] if scope == Scope.REPO.value else None
                existing = existing_by_id.get(memory_id)
                if existing is None:
                    first_exported_at = last_exported_at = ts
                else:
                    if existing["workspace_id"] != workspace_id:
                        raise ValueError(
                            "sync export marker conflicts with its existing workspace"
                        )
                    existing_repo = existing["repo_id"]
                    if (
                        existing_repo is not None
                        and repo_id is not None
                        and existing_repo != repo_id
                    ):
                        raise ValueError(
                            "sync export marker cannot move between repositories"
                        )
                    if existing_repo is None or repo_id is None:
                        repo_id = None
                    existing_first = _finite_timestamp(
                        existing["first_exported_at"], "first_exported_at"
                    )
                    existing_last = _finite_timestamp(
                        existing["last_exported_at"], "last_exported_at"
                    )
                    if existing_first is None or existing_last is None:
                        raise RuntimeError("stored sync export marker has null time")
                    if existing_last < existing_first:
                        raise RuntimeError("stored sync export marker has inverted time")
                    first_exported_at = min(existing_first, ts)
                    last_exported_at = max(existing_last, ts)
                self.conn.execute(
                    "INSERT INTO memory_sync_exports("
                    "memory_id, workspace_id, repo_id, first_exported_at, last_exported_at"
                    ") VALUES (?,?,?,?,?) "
                    "ON CONFLICT(memory_id) DO UPDATE SET "
                    "workspace_id=excluded.workspace_id, repo_id=excluded.repo_id, "
                    "first_exported_at=excluded.first_exported_at, "
                    "last_exported_at=excluded.last_exported_at",
                    (
                        memory_id, workspace_id, repo_id,
                        first_exported_at, last_exported_at,
                    ),
                )
        return len(unique)

    def get_memory_sync_export(self, memory_id: str) -> Optional[dict]:
        """Return one content-free prior-export marker, if local proof exists."""
        if not isinstance(memory_id, str) or not memory_id:
            raise ValueError("memory_id must be a non-empty string")
        row = self.conn.execute(
            "SELECT memory_id, workspace_id, repo_id, first_exported_at, "
            "last_exported_at FROM memory_sync_exports WHERE memory_id=?",
            (memory_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    # ── sync tombstones (durable deletion markers that propagate) ───────────────
    def add_memory_tombstone(
        self,
        memory_id: str,
        *,
        deleted_at: Optional[float] = None,
        device_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
        repo_id: Optional[str] = None,
        export_class: str = TOMBSTONE_NEVER_EXPORT,
    ) -> None:
        """Record one content-free erasure marker under a closed export policy.

        Earliest ``deleted_at`` wins, exactly like the ``valid_to`` closure lattice.
        ``never_export`` is terminal because its erased source classification can no
        longer be reconstructed. ``remote_erasure`` is likewise monotonic: a stale
        replay cannot retract a deletion already sent to peers. The caller owns the
        transaction/commit.
        """
        ts = _finite_timestamp(
            now_ts() if deleted_at is None else deleted_at, "deleted_at"
        )
        if (
            not isinstance(export_class, str)
            or export_class not in TOMBSTONE_EXPORT_CLASSES
        ):
            raise ValueError(
                "export_class must be 'never_export' or 'remote_erasure'"
            )
        did = device_id or self.device_id()
        existing = self.conn.execute(
            "SELECT deleted_at, device_id, workspace_id, repo_id, export_class "
            "FROM memory_tombstones WHERE memory_id=?",
            (memory_id,),
        ).fetchone()
        if existing is None:
            self.conn.execute(
                "INSERT INTO memory_tombstones("
                "memory_id, deleted_at, device_id, workspace_id, repo_id, "
                "export_class, created_at) VALUES (?,?,?,?,?,?,?)",
                (
                    memory_id, ts, did, workspace_id, repo_id,
                    export_class, ts,
                ),
            )
            return
        existing_workspace = existing["workspace_id"]
        if (
            existing_workspace is not None
            and workspace_id is not None
            and existing_workspace != workspace_id
        ):
            raise ValueError("tombstone workspace scope conflicts with existing marker")
        existing_repo = existing["repo_id"]
        if (
            existing_repo is not None
            and repo_id is not None
            and existing_repo != repo_id
        ):
            raise ValueError("tombstone repository scope conflicts with existing marker")
        if ts is None:
            raise AssertionError("normalized deleted_at unexpectedly became null")
        existing_deleted_at = existing["deleted_at"]
        if existing_deleted_at is None:
            raise RuntimeError("stored tombstone deleted_at is null")
        earlier = ts < float(existing_deleted_at)
        merged_workspace = (
            None
            if existing_workspace is None or workspace_id is None
            else (workspace_id if earlier else existing_workspace)
        )
        # A repo-less marker is legacy global state. Never narrow it to a repo;
        # conversely, a legacy marker arriving after a known repo marker widens
        # the terminal scope rather than allowing sibling-specific overwrite.
        merged_repo = (
            None
            if existing_repo is None or repo_id is None
            else existing_repo
        )
        if (
            existing["export_class"] == TOMBSTONE_NEVER_EXPORT
            and export_class == TOMBSTONE_REMOTE_ERASURE
        ):
            raise ValueError("never_export tombstones cannot become remotely exportable")
        merged_export_class = (
            TOMBSTONE_REMOTE_ERASURE
            if (
                existing["export_class"] == TOMBSTONE_REMOTE_ERASURE
                or export_class == TOMBSTONE_REMOTE_ERASURE
            )
            else TOMBSTONE_NEVER_EXPORT
        )
        self.conn.execute(
            "UPDATE memory_tombstones SET deleted_at=?, device_id=?, "
            "workspace_id=?, repo_id=?, export_class=? WHERE memory_id=?",
            (
                ts if earlier else existing["deleted_at"],
                did if earlier else existing["device_id"],
                merged_workspace,
                merged_repo,
                merged_export_class,
                memory_id,
            ),
        )

    def list_memory_tombstones(self, workspace_id: Optional[str] = None,
                               repo_id: Optional[str] = None) -> list[dict]:
        """Return tombstones scoped to a workspace and, when selected, one repo.

        Workspace-scoped tombstones remain visible to every repo in that workspace;
        repo-scoped tombstones never cross a repo-only export boundary.
        """
        if workspace_id is None and repo_id is not None:
            raise ValueError("repo_id requires workspace_id")
        if workspace_id is None:
            rows = self.conn.execute(
                "SELECT memory_id, deleted_at, device_id, workspace_id, repo_id, "
                "export_class FROM memory_tombstones ORDER BY memory_id"
            ).fetchall()
        elif repo_id is None:
            rows = self.conn.execute(
                "SELECT memory_id, deleted_at, device_id, workspace_id, repo_id, "
                "export_class FROM memory_tombstones WHERE workspace_id=? "
                "ORDER BY memory_id",
                (workspace_id,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT memory_id, deleted_at, device_id, workspace_id, repo_id, "
                "export_class FROM memory_tombstones "
                "WHERE workspace_id=? AND (repo_id=? OR repo_id IS NULL) "
                "ORDER BY memory_id",
                (workspace_id, repo_id),
            ).fetchall()
        return [
            {
                "id": str(row["memory_id"]), "deleted_at": float(row["deleted_at"]),
                "device": str(row["device_id"] or ""),
                "workspace_id": row["workspace_id"],
                "repo_id": row["repo_id"],
                "export_class": str(row["export_class"]),
            }
            for row in rows
        ]

    def device_id(self) -> str:
        """Return the one durable per-database sync origin, minting it atomically."""
        existing = self.get_sync_state("device_id")
        if existing:
            return str(existing)
        with self._write_operation("device_id", commit=True):
            candidate = ids.new_id("device")
            self.conn.execute(
                "INSERT INTO sync_state(key, value, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(key) DO NOTHING",
                ("device_id", candidate, now_ts()),
            )
            row = self.conn.execute(
                "SELECT value FROM sync_state WHERE key='device_id'"
            ).fetchone()
            if row is None or not row["value"]:
                raise RuntimeError("device identity initialization produced no value")
            return str(row["value"])

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
            if flt.scopes is not None:
                if not flt.scopes:
                    where.append("0")
                else:
                    marks = ",".join("?" for _ in flt.scopes)
                    where.append(f"{p}scope IN ({marks})")
                    params.extend(_enum(s) for s in flt.scopes)
            if flt.mtypes is not None:
                if not flt.mtypes:
                    where.append("0")
                else:
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


def _memory_descriptive_state(rec: MemoryRecord) -> tuple[Any, ...]:
    """Return the fields governed by a memory's descriptive LWW clock."""
    return (
        rec.title,
        rec.content,
        rec.summary,
        tuple(sorted(rec.keywords)),
        rec.metadata,
        _enum(rec.mtype),
        _enum(rec.scope),
        rec.importance,
        rec.surprise,
        rec.confidence,
        rec.sensitivity,
        rec.valid_from,
        rec.ingested_at,
        rec.session_id,
        rec.provenance,
        rec.subject_key,
        rec.claim_kind,
    )


def _row_to_record(row: sqlite3.Row) -> MemoryRecord:
    return MemoryRecord(
        id=row["id"], content=row["content"],
        mtype=MemoryType(row["mtype"]), scope=Scope(row["scope"]),
        workspace_id=row["workspace_id"], repo_id=row["repo_id"], session_id=row["session_id"],
        title=row["title"] or "", summary=row["summary"] or "",
        keywords=_loads(row["keywords"], []), metadata=_loads(row["metadata"], {}),
        importance=row["importance"], surprise=row["surprise"], stability=row["stability"],
        confidence=(
            row["confidence"]
            if "confidence" in row.keys() and row["confidence"] is not None else 1.0
        ),
        access_count=row["access_count"], last_access=row["last_access"],
        valid_from=row["valid_from"], valid_to=row["valid_to"],
        valid_to_recorded_at=(
            row["valid_to_recorded_at"]
            if "valid_to_recorded_at" in row.keys() else None
        ),
        ingested_at=row["ingested_at"], expired_at=row["expired_at"],
        modified_hlc=(
            row["modified_hlc"] if "modified_hlc" in row.keys() else ""
        ),
        subject_key=row["subject_key"] if "subject_key" in row.keys() else "",
        claim_kind=row["claim_kind"] if "claim_kind" in row.keys() else "",
        pinned=bool(row["pinned"]), sensitivity=row["sensitivity"],
        provenance=_loads(row["provenance"], {}),
        pinned_at=row["pinned_at"] if "pinned_at" in row.keys() else None,
        unpinned_at=row["unpinned_at"] if "unpinned_at" in row.keys() else None,
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


def _fts_terms(q: str) -> list[str]:
    """Return safe lexical terms plus conservative inflection variants."""
    terms = [t for t in "".join(c if c.isalnum() else " " for c in q).split() if t]
    expanded: list[str] = []
    for term in terms:
        expanded.append(term)
        if len(term) > 5 and term.endswith("ies"):
            expanded.append(term[:-3] + "y")
        elif len(term) > 6 and term.endswith("ions"):
            expanded.append(term[:-4])
        elif len(term) > 5 and term.endswith("ion"):
            expanded.append(term[:-3])
        elif len(term) > 6 and term.endswith(("ised", "ized")):
            expanded.append(term[:-1])
        elif len(term) > 6 and term.endswith("ates"):
            expanded.append(term[:-2])
        elif len(term) > 4 and term.endswith("s") and not term.endswith("ss"):
            expanded.append(term[:-1])
    # Keep the caller's term order while avoiding duplicate FTS clauses.
    return list(dict.fromkeys(expanded))


def _fts_query(q: str) -> str:
    """Make a safe FTS5 MATCH query with conservative inflection prefixes."""
    terms = _fts_terms(q)
    return " OR ".join(f'{term}*' for term in terms) if terms else '""'
