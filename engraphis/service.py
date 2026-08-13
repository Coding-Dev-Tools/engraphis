"""MemoryService — transport-agnostic facade over :class:`MemoryEngine`.

This is the layer the MCP server (and any other front end) calls. It deliberately
has **no MCP dependency**, so it runs and unit-tests offline on ``numpy`` alone
(per AGENTS.md §3). Responsibilities:

* resolve human-friendly ``workspace`` / ``repo`` names to scoped IDs;
* **validate and sanitize all untrusted input** before it reaches the store —
  ingested content is untrusted and memory poisoning is an explicit threat.
  Validation lives here so every front end inherits it;
* return plain JSON-serializable dicts.

The companion :mod:`engraphis.mcp_server` is a thin binding of these methods to
MCP tools; nothing in this module imports ``mcp``.
"""
from __future__ import annotations

import os
import re
import sys
import json
import hashlib
import contextvars
import logging
import math
import copy
import time
import threading
import unicodedata
import numpy as np
from collections import Counter, OrderedDict
from dataclasses import asdict
from functools import wraps
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, unquote, urlsplit
from urllib.request import url2pathname

from engraphis import __version__
from engraphis.backends.extractor import ChunkingExtractor
from engraphis.core.engine import MemoryEngine
from engraphis.core.graph_scene import (
    ALGORITHM_VERSION as GRAPH_SCENE_ALGORITHM_VERSION,
    build_canonical_graph,
    build_graph_scene,
    is_broad_search_fragment,
    strongest_path,
)
from engraphis.core.graph_layers import normalize_graph_layer
from engraphis.core.context import RegexTokenCounter
from engraphis.core.ids import new_id as make_id
from engraphis.core.savings import annotate_usage, normalize_release_version
from engraphis.core.interfaces import (
    Edge, GraphLayer, MemoryType, Node, Scope, SearchFilter,
    embedder_capabilities, embedding_space_fingerprint,
    vector_index_requires_sync,
    vector_index_shares_store_transaction,
)
from engraphis.core.poisoning import (
    REVIEW_APPROVED,
    REVIEW_PENDING,
    inspection_eligible,
    prompt_eligible,
    source_is_external,
)
from engraphis.core.query_planner import PLANNING_MODES
from engraphis.core.retrieval_policy import CANDIDATE_DEPTH_MODES, RETRIEVAL_PROFILES
from engraphis.core.secrets import SecretDetectedError, reject_secrets
from engraphis.core.store import (
    _loads,
    _merge_edge_provenance,
    _public_receipt_row,
    normalize_entity_name,
)
from engraphis.graphdata import build_graph_payload, empty_graph

logger = logging.getLogger("engraphis.service")


def _is_memory_database_path(db_path: str) -> bool:
    """Detect SQLite memory databases including named shared-memory URIs.

    Handles ``:memory:``, ``file::memory:``, and ``file:name?mode=memory``
    (with any query parameter order or case).
    """
    text = str(db_path or "")
    if not text or text == ":memory:":
        return True
    if not text.startswith("file:"):
        return False
    from urllib.parse import parse_qs, unquote, urlsplit
    parsed = urlsplit(text.replace("\\", "/"))
    uri_path = unquote(parsed.path)
    if uri_path == ":memory:":
        return True
    query = parse_qs(parsed.query)
    return "memory" in query.get("mode", [])


def _is_read_only_database_uri(db_path: str) -> bool:
    """Detect URI options that prohibit writes to the SQLite target."""
    text = str(db_path or "")
    if not text.startswith("file:"):
        return False
    query = parse_qs(urlsplit(text.replace("\\", "/")).query)
    modes = {value.casefold() for value in query.get("mode", [])}
    if "ro" in modes:
        return True
    return any(
        value.casefold() not in {"", "0", "false", "no", "off"}
        for value in query.get("immutable", [])
    )


def _physical_database_path(db_path: str) -> str:
    """Convert a SQLite file URI to the filesystem path used by Store.

    Store opens paths with ``uri=False``, so passing a ``file:`` URI through to
    pathlib or sqlite would treat the URI text as a literal filename.  Strip
    URI-only query options here and decode the path before migration locking and
    database opening both see it.  Named shared-memory URIs (``mode=memory``)
    are returned verbatim so SQLite keeps them in-memory.
    """
    text = str(db_path)
    if _is_memory_database_path(text) or not text.startswith("file:"):
        return text
    parsed = urlsplit(text)
    if parsed.scheme != "file" or not parsed.path:
        raise ValueError("file database URI must include a path")
    uri_path = unquote(parsed.path)
    if parsed.netloc and parsed.netloc != "localhost":
        uri_path = f"//{parsed.netloc}{uri_path}"
    return str(Path(url2pathname(uri_path)).expanduser())


def _annotate_context_usage(
    usage: dict[str, Any],
    *,
    operation: str,
    intent: Optional[str] = None,
    adaptive_mode: Optional[str] = None,
    baseline_tokens: Any = None,
    emitted_tokens: Any = None,
) -> dict[str, Any]:
    """Attach release-stamped, privacy-safe runtime savings telemetry."""
    return annotate_usage(
        usage,
        operation=operation,
        intent=intent,
        adaptive_mode=adaptive_mode,
        baseline_tokens=baseline_tokens,
        emitted_tokens=emitted_tokens,
        release_version=__version__,
    )

# ── validation limits (memory-poisoning / resource-exhaustion guards) ──────────
MAX_CONTENT_CHARS = 100_000
MAX_TITLE_CHARS = 1_000
MAX_NAME_CHARS = 200
MAX_KEYWORDS = 64
MAX_KEYWORD_CHARS = 128
MAX_METADATA_BYTES = 16_384
MAX_K = 50
MAX_TOKEN_BUDGET = 32_768
RESPONSE_MODES = frozenset({"full", "compact"})
# These are the transport-neutral producers used by the local agent protocol.  They
# are allowed to create prompt-visible memories immediately; external source labels
# remain review-gated below.  Keep this allow-list narrow so arbitrary caller-supplied
# provenance cannot self-approve a memory.
LOCAL_AGENT_SOURCES = frozenset({"agent", "intent_api"})
# Recall's fused rank is min-max normalized inside each query.  Keep this contract in
# every response mode so API/MCP clients do not treat a high rank as calibrated truth.
RECALL_SCORE_SEMANTICS = {
    "version": "retrieval-support-v1",
    "relative_score": (
        "Query-relative fused ranking score; compare only among memories returned by "
        "this response. It is not a confidence value or threshold."
    ),
    "absolute_support": (
        "Absolute query-to-memory support in [0, 1]: the maximum of semantic cosine "
        "(when semantic support is enabled) and lexical Jaccard. It is not min-max "
        "normalized. Grounded recall applies its stricter, separately calibrated "
        "evidence gate."
    ),
    "semantic_support": (
        "Whether this response used a declared semantic embedder. When false, vector "
        "retrieval and semantic cosine support are disabled."
    ),
}


def _recall_score_semantics(capabilities: dict) -> dict:
    """Describe the support calculation actually used by this response."""
    semantics = dict(RECALL_SCORE_SEMANTICS)
    semantics["semantic_support"] = bool(capabilities.get("semantic_support"))
    if not capabilities.get("semantic_support"):
        semantics["absolute_support"] = (
            "Absolute lexical query-to-memory support in [0, 1] (Jaccard only). "
            "Semantic cosine is disabled because the active embedder is not declared "
            "semantic."
        )
    return semantics

def _finite_float(value: Any, default: float = 0.0) -> float:
    """Coerce persisted numeric fields without exposing NaN/Infinity downstream."""
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if math.isfinite(number) else default



def _with_retrieval_capabilities(payload: dict, embedder, store=None) -> dict:
    """Add the stable degraded-mode contract to a public recall-shaped payload."""
    capabilities = embedder_capabilities(embedder)
    persistent_store = store is not None and not _is_memory_database_path(store.path)
    if capabilities["semantic_support"] and persistent_store:
        fingerprint = embedding_space_fingerprint(embedder)
        if not fingerprint or not store.embedding_space_ready(fingerprint):
            capabilities.update({
                "degraded_mode": True,
                "semantic_support": False,
                "degraded_reason": (
                    "semantic vector retrieval is disabled because stored vectors "
                    "do not match the configured embedding space"
                ),
                "vector_search_ready": False,
            })
    payload.update(capabilities)
    payload["score_semantics"] = _recall_score_semantics(capabilities)
    return payload
MAX_CONTEXT_TASK_CHARS = 10_000
MAX_AGENT_STATE_CHARS = 20_000
# import_folder/import_files (SECURITY.md §5 — reads/accepts local-content by path or
# upload; these bound resource use, not access scope, same framing as index_repo's
# max_files/max_file_bytes).
MAX_IMPORT_FILES = 500
MAX_IMPORT_FILE_BYTES = 2_000_000
MAX_IMPORT_RESOURCE_BYTES = 100_000_000
MAX_IMPORT_TOTAL_BYTES = 250_000_000
# Analytical graph scenes rank the candidate graph before applying the much smaller
# browser scene budget. Keep that server-side candidate set finite as well: graph rows
# are user/sync writable, and an unbounded Louvain/PageRank request would otherwise be a
# straightforward authenticated resource-exhaustion path.
MAX_GRAPH_ANALYSIS_ENTITIES = 40_000
MAX_GRAPH_ANALYSIS_EDGES = 200_000
MAX_GRAPH_ANALYSIS_SUPPORTS = 500_000
# Complete scenes are intentionally not representative samples.  These are hard
# refusal ceilings, not render caps: callers receive an explicit capacity error rather
# than a silently incomplete chart.
MAX_GRAPH_COMPLETE_MEMORIES = 100_000
MAX_GRAPH_COMPLETE_MEMORY_LINKS = 300_000
MAX_GRAPH_COMPLETE_CODE_MEMORY_LINKS = 300_000
MAX_GRAPH_COMPLETE_PAYLOAD_BYTES = 128 * 1024 * 1024
MAX_GRAPH_INDEX_MEMORIES = 20_000
MAX_GRAPH_INDEX_WORKERS = 2
GRAPH_INDEX_BATCH_SIZE = 100
GRAPH_INDEX_LEASE_SECONDS = 60.0
GRAPH_INDEX_JOB_HISTORY = 100
GRAPH_INDEX_SHUTDOWN_SECONDS = 10.0
DEFAULT_CODE_QUERY_CAPACITY = 10_000
MAX_CODE_QUERY_CAPACITY = 50_000
# Inspector payloads are deliberately smaller than analysis payloads. The endpoint
# reports complete counts, but bounds the returned detail so selecting a hub cannot
# produce a multi-megabyte response or lock the inspector's DOM.
GRAPH_ENTITY_RELATION_LIMIT = 200
GRAPH_ENTITY_EVIDENCE_LIMIT = 100
GRAPH_ENTITY_EVIDENCE_CANDIDATE_LIMIT = 400
GRAPH_ENTITY_HISTORY_LIMIT = 50
CONFLICT_REVIEW_SCAN_LIMIT = 10_000


def _graph_edge_visibility_sql(edge_alias: str, *, at: Optional[float] = None) -> str:
    """SQL predicate: support-less legacy edge or evidence from a non-session memory."""
    anchor = (
        repr(float(at)) if at is not None
        else repr(time.time())
    )
    return (
        "(NOT EXISTS (SELECT 1 FROM edge_supports visibility_support "
        f"WHERE visibility_support.edge_id={edge_alias}.id) OR EXISTS ("
        "SELECT 1 FROM edge_supports visibility_support "
        "JOIN memories visibility_memory "
        "ON visibility_memory.id=visibility_support.memory_id "
        f"WHERE visibility_support.edge_id={edge_alias}.id "
        "AND (visibility_support.valid_from IS NULL "
        f"OR visibility_support.valid_from<={anchor}) "
        "AND (visibility_support.valid_to IS NULL "
        f"OR {anchor}<visibility_support.valid_to) "
        "AND visibility_support.expired_at IS NULL "
        "AND (visibility_memory.valid_from IS NULL "
        f"OR visibility_memory.valid_from<={anchor}) "
        "AND (visibility_memory.valid_to IS NULL "
        f"OR {anchor}<visibility_memory.valid_to) "
        "AND visibility_memory.expired_at IS NULL "
        "AND COALESCE(visibility_memory.scope, 'workspace')!='session'))"
    )


def _graph_edge_history_visibility_sql(
    edge_alias: str, *, at: float, known_at: Optional[float] = None,
) -> str:
    """Visibility predicate for a time-travel graph payload.

    The ordinary graph reader asks whether evidence is public *at now*.  The Time
    view instead deliberately includes a relation's public history so the browser
    can distinguish live relations from superseded ghosts at the chosen anchor. Public
    support must have begun by that anchor, but it need not still be live: an invalidated
    public relation is intentionally retained as a ghost. Hard-expired evidence remains
    hidden in either case.
    """
    anchor = repr(float(at))
    known_anchor = repr(float(known_at)) if known_at is not None else repr(time.time())
    return (
        "(NOT EXISTS (SELECT 1 FROM edge_supports history_support "
        f"WHERE history_support.edge_id={edge_alias}.id) OR EXISTS ("
        "SELECT 1 FROM edge_supports history_support "
        "JOIN memories history_memory ON history_memory.id=history_support.memory_id "
        f"WHERE history_support.edge_id={edge_alias}.id "
        "AND (history_support.valid_from IS NULL "
        f"OR history_support.valid_from<={anchor}) "
        "AND (history_support.ingested_at IS NULL "
        f"OR history_support.ingested_at<={known_anchor}) "
        "AND (history_support.expired_at IS NULL "
        f"OR {known_anchor}<history_support.expired_at) "
        "AND (history_memory.valid_from IS NULL "
        f"OR history_memory.valid_from<={anchor}) "
         "AND (history_memory.ingested_at IS NULL "
         f"OR history_memory.ingested_at<={known_anchor}) "
         "AND (history_memory.expired_at IS NULL "
         f"OR {known_anchor}<history_memory.expired_at) "
         f"AND history_memory.workspace_id={edge_alias}.workspace_id "
         "AND COALESCE(history_memory.scope, 'workspace')!='session'))"
     )


def _graph_entity_visibility_sql(entity_alias: str, *, at: Optional[float] = None) -> str:
    """Hide entities whose entire evidence-bearing history is session-private.

    Entity classification deliberately considers all historical touching edges, not only
    the edges rendered at ``at``.  Otherwise forgetting the last session memory closes its
    edge/support and turns the now-isolated entity into an apparently support-less public
    label.  Truly manual/legacy entities remain visible when they have no edge history or a
    touching edge that never had a support row.  ``at`` remains accepted for call-site
    symmetry; edge-at-anchor visibility is applied separately when edges are rendered.
    """
    del at
    touching = (
        f"visibility_edge.workspace_id={entity_alias}.workspace_id AND "
        f"(visibility_edge.src={entity_alias}.id OR visibility_edge.dst={entity_alias}.id)"
    )
    return (
        "(NOT EXISTS (SELECT 1 FROM edges visibility_edge WHERE " + touching + ") "
        "OR EXISTS (SELECT 1 FROM edges visibility_edge WHERE " + touching + " AND "
        "NOT EXISTS (SELECT 1 FROM edge_supports visibility_support "
        "WHERE visibility_support.edge_id=visibility_edge.id)) "
        "OR EXISTS (SELECT 1 FROM edges visibility_edge "
        "JOIN edge_supports visibility_support "
        "ON visibility_support.edge_id=visibility_edge.id "
        "JOIN memories visibility_memory "
        "ON visibility_memory.id=visibility_support.memory_id WHERE " + touching + " AND "
        "COALESCE(visibility_memory.scope, 'workspace')!='session'))"
    )

# control characters except tab/newline/carriage-return
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_NAME_RE = re.compile(r"^[A-Za-z0-9._\-/ ]{1,%d}$" % MAX_NAME_CHARS)
_PRINCIPAL_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,%d}$" % MAX_NAME_CHARS)
_PRINCIPAL_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+$")
_RECEIPT_ID_RE = re.compile(r"^rcpt_[0-9ABCDEFGHJKMNPQRSTVWXYZ]{26}$")
_RECEIPT_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_RECEIPT_VERIFICATION_ERRORS = frozenset({
    "hash_mismatch",
    "payload_mismatch",
    "payload_schema_invalid",
    "sequence_mismatch",
    "chain_break",
    "chain_root_count",
    "chain_cycle",
    "chain_fork",
    "chain_disconnected",
    "missing_anchor",
    "anchor_count_mismatch",
    "anchor_head_mismatch",
    "anchor_integrity_error",
    "expected_head_mismatch",
    "expected_count_mismatch",
})


class ValidationError(ValueError):
    """Raised when untrusted input fails a guard. Message is safe to surface."""


class WorkspaceBindingError(ValidationError):
    """Raised when a request crosses the configured workspace boundary.

    This remains separate from ordinary input validation so API surfaces can return a
    fixed configuration error without echoing the requested workspace or allow-list.
    """

    def __init__(self) -> None:
        super().__init__("workspace is not permitted by this instance's configuration")


def _reject_secret_capture(fields) -> None:
    """Map the core content-free secret rejection into this facade's error type."""
    try:
        reject_secrets(fields)
    except SecretDetectedError as exc:
        raise ValidationError(str(exc)) from None


def _code_query_capacity(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError("capacity must be an integer")
    if not 1 <= value <= MAX_CODE_QUERY_CAPACITY:
        raise ValidationError(
            f"capacity must be between 1 and {MAX_CODE_QUERY_CAPACITY}"
        )
    return value


class GraphSceneCapacityExceeded(ValidationError):
    """A complete scene crossed a hard safety ceiling and was not sampled."""

    def __init__(self, *, resource: str, count: int, limit: int) -> None:
        self.resource = resource
        self.count = int(count)
        self.limit = int(limit)
        super().__init__(
            f"complete graph exceeds the {resource} safety limit "
            f"({self.count} > {self.limit}); narrow the workspace filters"
        )


def _rollback_service_transaction(method):
    """Run a service mutation in an owned transaction and release it on failure.

    ``sqlite3.Connection.in_transaction`` is connection-global, while the store
    serializes transactions with thread-local ownership.  Checking the former can
    make a request roll back another thread's transaction or leave its own
    transaction open after waiting for that thread.  Use the store's ownership
    primitive so lifecycle mutations are atomic without stealing concurrent work.
    """
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        conn = self.store.conn
        owns_transaction = not conn.transaction_owned_by_current_thread()
        try:
            if owns_transaction:
                conn.execute("BEGIN IMMEDIATE")
            with conn.defer_commits():
                result = method(self, *args, **kwargs)
            if owns_transaction and conn.transaction_owned_by_current_thread():
                conn.commit()
            return result
        except BaseException:
            if owns_transaction and conn.transaction_owned_by_current_thread():
                try:
                    conn.rollback()
                except Exception:  # noqa: BLE001 - preserve the original failure
                    pass
            raise
    return wrapped


class GraphIndexRebuilding(ValidationError):
    """Raised when a graph read would observe a partially rebuilt derived index."""

    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        super().__init__(f"graph index rebuilding (job {job_id})")


# ── current dashboard user (request-scoped, team mode only) ────────────────────
# Set by the dashboard's team auth gate (engraphis/dashboard_app.py::_auth_gate) for the
# duration of a request, and read at the workspace-authorization chokepoint below so a
# *personal* folder is visible and usable only by its owner. Every other entry point —
# standalone MCP server, the CLI, the sync loop, and the offline test/eval harnesses —
# leaves this at its ``None`` default, so per-user enforcement is a no-op outside the
# multi-user dashboard (including its mounted MCP endpoint) and single-tenant behaviour is
# completely unchanged. It lives here (not in a
# route module) so the service stays the single place workspace access is decided.
_CURRENT_USER: "contextvars.ContextVar[Optional[dict]]" = contextvars.ContextVar(
    "engraphis_dashboard_user", default=None)


def set_current_user(user: Optional[dict]) -> None:
    """Bind (or clear, with ``None``) the current dashboard user for this request context.

    ``None`` is the only anonymous/single-user value. Every authenticated principal must
    supply a stable ``id`` and ownership ``email``; malformed identity fails closed and
    clears any inherited binding before raising. A normalized copy is stored so callers
    cannot mutate their input dict after authentication. ``role`` defaults to member-safe.
    Called once per request by the team auth gate; contextvars are per-context so concurrent
    requests never see each other's user."""
    if user is None:
        _CURRENT_USER.set(None)
        return
    try:
        principal = _validate_authenticated_principal(user)
    except ValidationError:
        _CURRENT_USER.set(None)
        raise
    _CURRENT_USER.set(principal)


def current_user() -> Optional[dict]:
    """A copy of the validated dashboard principal, or ``None`` outside team mode."""
    user = _CURRENT_USER.get()
    return dict(user) if user is not None else None


def _clean_text(value: Any, *, field: str, max_chars: int, required: bool = True) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be a string")
    # strip control chars (defangs hidden-instruction / terminal-escape payloads)
    cleaned = _CONTROL_RE.sub("", value).strip()
    if required and not cleaned:
        raise ValidationError(f"{field} must not be empty")
    if len(cleaned) > max_chars:
        raise ValidationError(f"{field} exceeds {max_chars} characters (got {len(cleaned)})")
    return cleaned


def _fit_context_tokens(text: str, budget: int, counter) -> str:
    """Return a deterministic prefix that satisfies the active token counter.

    Proactive context predates the recall packer, so its compact projection must
    enforce the same hard budget itself. Keep source text intact when it fits;
    otherwise trim at the regex-token boundary used by the offline default.
    """
    text = str(text or "")
    if budget <= 0 or not text:
        return ""
    if int(counter(text)) <= budget:
        return text
    tokens = list(re.finditer(r"\w+|[^\w\s]", text, re.UNICODE))
    if not tokens:
        return ""
    end = tokens[min(budget, len(tokens)) - 1].end()
    fitted = text[:end].rstrip()
    # Custom counters are allowed at composition time. Be conservative if one
    # tokenizes differently from the deterministic boundary above.
    while fitted and int(counter(fitted)) > budget:
        tokens = list(re.finditer(r"\w+|[^\w\s]", fitted, re.UNICODE))
        if not tokens:
            return ""
        fitted = fitted[:tokens[-1].start()].rstrip()
    return fitted


def _fit_context_lines(text: str, budget: int, counter) -> str:
    """Pack a whole-line prefix without splitting citation markers or bodies.

    Proactive summaries use one source per line.  A token-level prefix can end
    in ``[`` or ``[1``, falsely making a truncated source appear grounded.
    Compact responses therefore trade a partial final line for a complete,
    independently verifiable cited line.
    """
    text = str(text or "")
    if budget <= 0 or not text:
        return ""
    if int(counter(text)) <= budget:
        return text
    packed: list[str] = []
    for line in text.splitlines():
        candidate = "\n".join([*packed, line])
        if int(counter(candidate)) > budget:
            break
        packed.append(line)
    return "\n".join(packed)


def _strict_bool(value: Any, *, field: str) -> bool:
    """Accept only real booleans for authority-affecting flags.

    Python's ``bool(\"false\")`` is true. Coercing a caller-supplied provenance flag
    that way would let an untrusted payload bypass the quarantine policy merely by
    arriving through a loosely typed integration.
    """
    if not isinstance(value, bool):
        raise ValidationError(f"{field} must be a boolean")
    return value


def _canonical_write_provenance(
    source: Any, trusted: Any, *, raw_ingest: bool, ingress: str = "service"
) -> dict:
    """Create provenance at the service boundary, never from caller metadata.

    Normal local-agent memory creation is intentionally immediate: agents should not
    need an owner ceremony for every fact they learn.  The service still owns the
    approval decision, and only the narrow local-agent source allow-list receives
    prompt eligibility here.  External/imported sources remain pending, while the
    deterministic poisoning guard can quarantine any payload before it is surfaced.
    In-process callers that are intentionally trusted may still use ``MemoryEngine``
    directly; that is an explicit local-code capability.
    """
    source_name = _clean_text(
        source, field="source", max_chars=MAX_NAME_CHARS, required=False
    ) or "agent"
    requested = _strict_bool(trusted, field="trusted")
    ingress_name = _clean_text(
        ingress, field="ingress", max_chars=MAX_NAME_CHARS, required=False
    ) or "service"
    external = source_is_external(source_name)
    # Transport labels are not capabilities.  HTTP and MCP callers must use the
    # explicit loopback attestation below; otherwise a remote caller could simply
    # submit source="agent" and self-approve prompt-visible content.
    local_agent = (
        source_name.casefold() in LOCAL_AGENT_SOURCES
        and ingress_name.casefold() not in {"http", "mcp", "remote"}
    )
    provenance = {
        "source": source_name,
        "trusted": local_agent,
        "review_state": REVIEW_APPROVED if local_agent else REVIEW_PENDING,
        "trust_origin": (
            "local_agent"
            if local_agent else
            "external_ingress" if (external or raw_ingest) else "service_review_gate"
        ),
        "writer_policy": "service-v11",
        "ingress": ingress_name,
    }
    if requested and not local_agent:
        # An auditable code, not a copy of source content or a caller-controlled
        # trust assertion.  Operators can see that a downgrade happened without
        # turning it into prompt-visible metadata.
        provenance["trust_downgraded"] = True
    return provenance


def _local_cli_provenance() -> dict:
    """Return the explicit local-owner provenance reserved for ``engraphis-cli``.

    A terminal command entered on the device that owns the database is an intentional
    local capability, like a direct ``MemoryEngine`` call. It is not a transport
    assertion and no HTTP, dashboard, import, or MCP caller can select it. Those
    boundaries use canonical ingress policy or their separate, binding-attested
    local-agent capability.
    """
    return {
        "source": "cli",
        "trusted": True,
        "review_state": REVIEW_APPROVED,
        "trust_origin": "local_cli_operator",
        "writer_policy": "service-v11",
        "ingress": "cli",
    }


def _local_agent_provenance(source: Any, *, ingress: str) -> Optional[dict]:
    """Return approved provenance only for an operator-attested local binding.

    ``_local_agent_operator`` is a private capability supplied by the HTTP or MCP
    binding after that binding's own authorization check.  Keep the accepted ingress
    names explicit so an accidental call-site cannot turn an arbitrary transport label
    into approval authority.
    """
    source_name = _clean_text(
        source, field="source", max_chars=MAX_NAME_CHARS, required=False
    ) or "agent"
    if source_name.casefold() not in LOCAL_AGENT_SOURCES:
        return None
    ingress_name = _clean_text(
        ingress, field="ingress", max_chars=MAX_NAME_CHARS, required=False
    ).casefold()
    attested_boundary = {
        "http": ("local_loopback_agent", "http_loopback"),
        "mcp": ("local_mcp_agent", "mcp_operator"),
    }.get(ingress_name)
    if attested_boundary is None:
        return None
    trust_origin, recorded_ingress = attested_boundary
    return {
        "source": source_name,
        "trusted": True,
        "review_state": REVIEW_APPROVED,
        "trust_origin": trust_origin,
        "writer_policy": "service-v11",
        "ingress": recorded_ingress,
    }


def _clean_name(value: Any, *, field: str) -> str:
    name = _clean_text(value, field=field, max_chars=MAX_NAME_CHARS)
    if not _NAME_RE.match(name):
        raise ValidationError(
            f"{field} may only contain letters, digits, space and . _ - / characters"
        )
    return name


def _validate_authenticated_principal(user: Any) -> dict[str, str]:
    """Return a normalized, caller-independent identity or fail closed.

    A non-``None`` current-user value is an authenticated boundary, never a hint. Both
    the stable user id (session ownership) and email (workspace ownership) are mandatory;
    silently replacing either with ``''`` collapses distinct users into one principal.
    """
    if not isinstance(user, dict):
        raise ValidationError("authenticated principal must be an object")
    raw_id = user.get("id")
    if isinstance(raw_id, str) and _CONTROL_RE.search(raw_id):
        raise ValidationError("authenticated principal id contains control characters")
    user_id = _clean_text(
        raw_id, field="authenticated principal id", max_chars=MAX_NAME_CHARS
    )
    if not _PRINCIPAL_ID_RE.fullmatch(user_id):
        raise ValidationError("authenticated principal id is invalid")
    raw_email = user.get("email")
    if isinstance(raw_email, str) and _CONTROL_RE.search(raw_email):
        raise ValidationError("authenticated principal email contains control characters")
    email = _clean_text(
        raw_email, field="authenticated principal email", max_chars=320
    ).casefold()
    if not _PRINCIPAL_EMAIL_RE.fullmatch(email):
        raise ValidationError("authenticated principal email is invalid")
    role = user.get("role")
    if role not in {"viewer", "member", "admin"}:
        role = "member"
    return {"id": user_id, "email": email, "role": role}


def _authenticated_principal() -> Optional[dict[str, str]]:
    """Validated request principal; ``None`` alone selects trusted local-owner mode."""
    user = current_user()
    return _validate_authenticated_principal(user) if user is not None else None


def _clean_string_list(value: Any, *, field: str, max_items: int, max_chars: int) -> list[str]:
    if not value:
        return []
    if not isinstance(value, (list, tuple)):
        raise ValidationError(f"{field} must be a list of strings")
    if len(value) > max_items:
        raise ValidationError(f"too many {field} (max {max_items})")
    return [_clean_text(v, field=field.rstrip("s") or field, max_chars=max_chars) for v in value]


def _clean_keywords(value: Any) -> list[str]:
    return _clean_string_list(value, field="keywords", max_items=MAX_KEYWORDS,
                              max_chars=MAX_KEYWORD_CHARS)


# Keys MemoryEngine treats as trusted structured-extraction output
# (core/engine.py::_has_structured_graph_metadata) and feeds straight into the
# entity/edge graph tagged provenance.source="structured_extractor" — i.e.
# indistinguishable from what backends.extractor.StructuredLLMExtractor actually
# produced. The engine cannot tell a caller-supplied value here from its own
# extractor's output (both arrive in the same ``metadata`` dict), so that check has to
# happen before the caller's value ever reaches the engine — see _clean_metadata below.
_GRAPH_HINT_KEYS = ("entities", "relations", "structured_extraction")
# Internal review envelope produced only after the extractor boundary. A caller-provided
# value under this name could otherwise be relabelled as model-derived evidence when a
# genuine extractor emits activity metadata but no graph hints.
_INTERNAL_GRAPH_HINT_KEYS = ("unverified_derived_graph",)
_CALLER_GRAPH_HINT_KEYS = (*_GRAPH_HINT_KEYS, *_INTERNAL_GRAPH_HINT_KEYS)

# Keys the /llm/activity audit view (routes/v2_api.py) trusts as authentic evidence that
# a memory's content was sent to an LLM provider (``llm_extraction``) or consolidated
# (``structured_consolidation``). Both are produced ONLY inside the engine/consolidator
# from a real extractor's output (backends/extractor.py, core/consolidate.py), never from
# a caller-supplied metadata dict — so, exactly like _GRAPH_HINT_KEYS above, a direct
# remember()/ingest() caller could otherwise set them itself and forge that audit trail.
_ACTIVITY_HINT_KEYS = ("llm_extraction", "structured_consolidation")


def _clean_metadata(value: Any) -> dict:
    if not value:
        return {}
    if not isinstance(value, dict):
        raise ValidationError("metadata must be an object")
    import json
    try:
        encoded = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError, RecursionError):
        raise ValidationError("metadata must be JSON-serializable")
    if len(encoded.encode("utf-8")) > MAX_METADATA_BYTES:
        raise ValidationError(f"metadata exceeds {MAX_METADATA_BYTES} bytes")
    if "retention_supervision" in value:
        # Reserved service-internal channel: the engine trusts this key as a host
        # retention decision (raw importance/stability, far past the bounded
        # ``retention_class`` presets). Only ``remember()`` may set it, after
        # validating ``retention_class`` — never a caller-supplied metadata dict.
        value = {k: v for k, v in value.items() if k != "retention_supervision"}
    if any(k in value for k in _CALLER_GRAPH_HINT_KEYS):
        # Graph poisoning with forged provenance (SECURITY.md): remember()/ingest() are
        # reachable directly (MCP tool, HTTP route, dashboard) with caller-chosen
        # metadata, so a caller could set these same keys itself and inherit the
        # trusted extractor's label for content the extractor never saw. The genuine
        # path is unaffected: a configured Extractor's own ExtractedFact.metadata is
        # computed fresh inside MemoryEngine.ingest() from the extractor's real output,
        # never from this argument. Re-home the caller's values — preserved, not
        # dropped — under a key the engine's structured-graph check does not recognize,
        # tagged with an honest source, so they can never masquerade as trusted
        # extraction. Existing defanging/caps (backends/graph_extractor.py) are
        # untouched by this; only the label was the defect.
        hints = {k: value[k] for k in _CALLER_GRAPH_HINT_KEYS if k in value}
        value = {
            k: v for k, v in value.items() if k not in _CALLER_GRAPH_HINT_KEYS
        }
        value = {**value, "client_supplied_graph": {**hints, "source": "client_supplied"}}
    if any(k in value for k in _ACTIVITY_HINT_KEYS):
        # Forged LLM-activity provenance (same class as the graph keys above): re-home the
        # caller's values — preserved, not dropped — under an honest client-supplied label
        # so they can never masquerade as trusted extraction/consolidation activity in
        # /llm/activity. The genuine path is unaffected: real llm_extraction /
        # structured_consolidation metadata is computed inside the engine/consolidator
        # after this validation runs, never from this caller-supplied argument.
        acts = {k: value[k] for k in _ACTIVITY_HINT_KEYS if k in value}
        value = {k: v for k, v in value.items() if k not in _ACTIVITY_HINT_KEYS}
        value = {**value, "client_supplied_activity": {**acts, "source": "client_supplied"}}
    return value


def _enum(value: Any, enum_cls, field: str):
    if value is None:
        raise ValidationError(f"{field} is required")
    if isinstance(value, enum_cls):
        return value
    try:
        return enum_cls(str(value).strip().lower())
    except ValueError:
        allowed = ", ".join(e.value for e in enum_cls)
        raise ValidationError(f"{field} must be one of: {allowed}")


def _optional_timestamp(value: Any, *, field: str) -> Optional[float]:
    """Validate an optional Unix timestamp at the shared transport boundary."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValidationError(f"{field} must be a finite timestamp")
    try:
        timestamp = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValidationError(f"{field} must be a finite timestamp") from exc
    if not math.isfinite(timestamp):
        raise ValidationError(f"{field} must be a finite timestamp")
    return timestamp


def _temporal_anchors(*, as_of: Any = None, valid_at: Any = None,
                      known_at: Any = None) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """Normalize the public bi-temporal aliases once for non-recall reads.

    Recall performs the same validation inline for backwards-compatible error
    ordering.  Direct code-graph reads use this helper so they cannot silently
    diverge from recall's ``as_of``/``valid_at`` contract.
    """
    as_of_value = _optional_timestamp(as_of, field="as_of")
    valid_value = _optional_timestamp(valid_at, field="valid_at")
    known_value = _optional_timestamp(known_at, field="known_at")
    if as_of_value is not None and valid_value is not None and as_of_value != valid_value:
        raise ValidationError("as_of and valid_at must match when both are supplied")
    valid_value = valid_value if valid_value is not None else as_of_value
    return as_of_value, valid_value, known_value


def _write_scope(value: Any, *, repo: Optional[str], session_id: Optional[str]) -> Scope:
    """Resolve and validate the structural scope of a write.

    Omitted scope follows the supplied context (session -> repo -> workspace). Explicit
    scopes must name the parent they require; this prevents records whose scope says
    ``repo`` but whose ``repo_id`` is NULL, the inconsistency that previously made the
    hierarchy advisory rather than enforceable.
    """
    if value is None:
        return Scope.REPO if (repo or session_id) else Scope.WORKSPACE
    scope = _enum(value, Scope, "scope")
    if scope == Scope.SESSION and not session_id:
        raise ValidationError("session scope requires session_id")
    if scope == Scope.REPO and not repo and not session_id:
        raise ValidationError("repo scope requires repo (or a repo-backed session_id)")
    if scope in (Scope.WORKSPACE, Scope.USER) and repo:
        raise ValidationError(f"{scope.value} scope requires repo to be omitted")
    return scope


def _resolve_import_root(raw_path: str) -> Path:
    """Path-traversal guard for ``import_folder`` (SECURITY.md §5): the path is
    attacker-controlled if whatever calls this endpoint is (e.g. a prompt-injected
    agent, or any team member who can reach the dashboard), so it must resolve inside
    an allowlisted root before anything under it is read. Mirrors the retired v1 vault
    ``/memory/vaults/import-folder`` endpoint's convention — home directory by default,
    widened via ``ENGRAPHIS_IMPORT_ROOTS`` (``os.pathsep``-separated) for server
    deployments that keep content outside ``$HOME``."""
    home = os.path.realpath(str(Path.home().expanduser()))
    allowed_roots = [home]
    env_roots = os.environ.get("ENGRAPHIS_IMPORT_ROOTS", "")
    if env_roots:
        allowed_roots.extend(
            os.path.realpath(os.path.expanduser(root))
            for root in env_roots.split(os.pathsep)
            if root
        )
    real_path = os.path.realpath(os.path.expanduser(raw_path))
    comparable_path = os.path.normcase(real_path)
    safe_path = None
    for root in allowed_roots:
        comparable_root = os.path.normcase(root)
        if comparable_path == comparable_root:
            safe_path = comparable_root
            break
        root_prefix = comparable_root.rstrip(os.sep) + os.sep
        if comparable_path.startswith(root_prefix):
            safe_path = comparable_path
            break
    if safe_path is None:
        raise ValidationError(
            "import path must be under an allowed root (your home directory, or "
            "ENGRAPHIS_IMPORT_ROOTS)")
    folder = Path(safe_path)
    if not folder.exists():
        raise ValidationError(f"path not found: {raw_path}")
    if not folder.is_dir():
        raise ValidationError(f"not a directory: {raw_path}")
    return folder


def _iter_import_files(folder: Path, pattern: str, max_files: int) -> list:
    """Files under ``folder`` matching the glob ``pattern`` (default ``*.md``), skipping
    VCS/dependency directories and capped at ``max_files`` — a resource bound, not a
    security boundary (the boundary is ``_resolve_import_root``).

    Symlink escape guard: ``rglob`` follows symlinked directories, so a symlink placed
    somewhere under an allowed root (by anything that ever had write access there) could
    point outside the allowed root entirely and defeat ``_resolve_import_root`` — every
    candidate is re-resolved and re-contained here, the same check the root itself got."""
    import fnmatch
    files: list = []
    for f in sorted(folder.rglob("*")):
        if len(files) >= max_files:
            break
        if not f.is_file() or not fnmatch.fnmatch(f.name, pattern):
            continue
        try:
            # ``f`` came from a user-selected tree. Resolve and contain it before both
            # deriving metadata and returning the path that ``import_folder`` will read.
            real = f.resolve(strict=True)
            rel = real.relative_to(folder)
        except (OSError, ValueError):
            continue
        parts = rel.parts
        if any(p == "node_modules" or p == ".git" or p.startswith(".") for p in parts[:-1]):
            continue
        files.append(real)
    return files


def _title_from_content(content: str, fallback: str) -> str:
    """First Markdown H1 if present, else the caller-supplied fallback (usually the
    filename stem) — matches the retired v1 import-folder endpoint's title heuristic."""
    match = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
    return match.group(1).strip() if match else fallback


def _warn_if_db_empty_with_populated_sibling(db_path: str) -> None:
    """Warn, without blocking startup, when a sibling database holds the data."""
    import sqlite3

    configured = Path(db_path)
    if not configured.is_file():
        return
    try:
        probe = sqlite3.connect(str(configured))
        try:
            row = probe.execute("SELECT COUNT(*) FROM memories").fetchone()
            configured_count = int(row[0]) if row else 0
        except sqlite3.Error:
            return
        finally:
            probe.close()
    except sqlite3.Error:
        return
    if configured_count > 0:
        return

    home = Path.home()
    candidates: list[Path] = [home / ".engraphis" / "engraphis.db"]
    if os.name == "nt":
        local_appdata = os.environ.get("LOCALAPPDATA")
        if local_appdata:
            candidates.append(Path(local_appdata) / "engraphis" / "engraphis.db")
    elif sys.platform == "darwin":
        candidates.append(home / "Library" / "Application Support" / "engraphis" / "engraphis.db")
    else:
        xdg = os.environ.get("XDG_DATA_HOME", str(home / ".local" / "share"))
        candidates.append(Path(xdg) / "engraphis" / "engraphis.db")

    configured_resolved = configured.resolve()
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            resolved = str(candidate.resolve())
        except OSError:
            continue
        if resolved == str(configured_resolved) or resolved in seen:
            continue
        seen.add(resolved)
        try:
            probe = sqlite3.connect(str(candidate))
            try:
                row = probe.execute("SELECT COUNT(*) FROM memories").fetchone()
                count = int(row[0]) if row else 0
            finally:
                probe.close()
        except sqlite3.Error:
            continue
        if count > 0:
            print(
                "[engraphis] WARNING: configured database %s has 0 memories, but "
                "%s has %d memories. If this is unintentional, update "
                "ENGRAPHIS_DB_PATH in ~/.engraphis/config.env to point to the "
                "populated database." % (configured, candidate, count),
                file=sys.stderr,
            )
            return


def _auto_migrate_v1_if_needed(db_path: str) -> None:
    """Serialize legacy-database recovery and migration across processes."""
    from engraphis.config import _migration_lock

    physical_path = _physical_database_path(db_path)
    if _is_memory_database_path(physical_path):
        return
    with _migration_lock(Path(physical_path).expanduser()):
        _auto_migrate_v1_if_needed_unlocked(physical_path)


def _auto_migrate_v1_if_needed_unlocked(db_path: str) -> None:
    """If *db_path* is an existing v1-shaped SQLite file, migrate it to the v2 schema
    in place before :class:`~engraphis.core.engine.MemoryEngine` (via ``Store``) ever
    touches it.

    v1 (``engraphis/stores/__init__.py``) and v2 (``engraphis/core/schema.py``) both
    happen to name a table ``memories``, but the column sets are unrelated — v1 has no
    ``workspace_id``. ``Store.init_schema()`` runs ``CREATE INDEX ... ON
    memories(workspace_id, ...)`` unconditionally, which is a no-op-safe ``CREATE TABLE
    IF NOT EXISTS`` for a *fresh* db, but crashes with ``sqlite3.OperationalError: no
    such column: workspace_id`` the instant it runs against a pre-existing v1 file —
    e.g. any self-host that ran ``engraphis-server`` (v1) against ``ENGRAPHIS_DB_PATH``
    before ever running ``engraphis-dashboard`` (v2) against that same path. This bit a
    real production deployment on 2026-07-13: switching the default entrypoint to the
    v2 dashboard crash-looped the container against its own pre-existing v1 data.

    Detection: read-only sniff of ``PRAGMA table_info(memories)`` — no ``workspace_id``
    column means v1-shaped (or a table from some other, unrelated database entirely, in
    which case there's nothing safe to do and we leave it for ``Store`` to error on
    normally). A missing file or an unreadable one (encrypted via
    ``ENGRAPHIS_DB_KEY``, corrupt, no ``memories`` table at all) is left alone — the
    normal ``Store()`` path handles a fresh install or surfaces the real error.

    Migration is non-destructive: the original file is copied aside to
    ``<name>.v1-backup-<unix-ts><ext>`` *before* anything else happens, the actual
    migration (:func:`scripts.migrate_to_v2.migrate`) reads the untouched original and
    writes a brand-new file, and only a fully-successful migration is atomically
    swapped into ``db_path`` (:func:`os.replace`). Any failure along the way leaves the
    original file exactly as it was; ``Store`` then raises its normal (now unmasked)
    error instead of silently losing data."""
    p = Path(db_path)
    # Recover a crash from the legacy two-step Windows swap before inspecting the
    # database. A completed migration output is preferred to the stale v1 backup.
    staging = p.with_suffix(".v2_swap")
    if not p.exists() and staging.exists():
        migrating = sorted(
            p.parent.glob(p.stem + ".v2-migrating-*" + p.suffix),
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )
        if migrating:
            os.replace(str(migrating[0]), str(p))
            for leftover in migrating[1:]:
                try:
                    leftover.unlink(missing_ok=True)
                except OSError:
                    pass
            try:
                staging.unlink(missing_ok=True)
            except OSError:
                pass
        else:
            os.replace(str(staging), str(p))
    elif not p.exists():
        migrating = sorted(
            p.parent.glob(p.stem + ".v2-migrating-*" + p.suffix),
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )
        if migrating:
            os.replace(str(migrating[0]), str(p))
            for leftover in migrating[1:]:
                try:
                    leftover.unlink(missing_ok=True)
                except OSError:
                    pass
    if not p.exists() or not p.is_file():
        return  # fresh install, ":memory:", or nothing there yet — Store() creates v2 cleanly
    import sqlite3
    try:
        probe = sqlite3.connect(str(p))
        try:
            cols = {r[1] for r in probe.execute("PRAGMA table_info(memories)").fetchall()}
        finally:
            probe.close()
    except sqlite3.Error:
        return  # not a plain-sqlite file we can safely inspect (e.g. SQLCipher-encrypted)
    if not cols or "workspace_id" in cols:
        return  # no memories table yet, or already v2-shaped — nothing to migrate

    import shutil
    import sys
    import time
    ts = int(time.time())
    backup = p.with_name(p.stem + (".v1-backup-%d" % ts) + p.suffix)
    tmp_new = p.with_name(p.stem + (".v2-migrating-%d" % ts) + p.suffix)
    print("[engraphis] detected a v1-shaped database at %s — auto-migrating to the v2 "
          "schema (original preserved at %s)" % (p, backup), file=sys.stderr)
    try:
        shutil.copy2(str(p), str(backup))          # preserve the untouched original first
        from scripts.migrate_to_v2 import migrate
        counts = migrate(str(p), str(tmp_new))      # reads p (untouched), writes tmp_new
        # On Windows os.replace is not atomic; use a two-step rename with a staging
        # file so a crash mid-swap leaves either the original or the migrated DB intact.
        staging = p.with_suffix(".v2_swap")
        try:
            if staging.exists():
                staging.unlink()
            os.rename(str(p), str(staging))
            os.rename(str(tmp_new), str(p))
            try:
                staging.unlink()
            except OSError:
                pass  # best-effort cleanup; backup still exists
        except Exception:
            # Rollback: restore the original if the swap failed partway through.
            if staging.exists() and not p.exists():
                os.rename(str(staging), str(p))
            raise
        print("[engraphis] v1->v2 auto-migration complete: %s" % counts, file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 — must never brick startup worse than before
        print("[engraphis] v1->v2 auto-migration failed (%s) — leaving %s untouched; "
              "the original v1 data is safe at %s. Store() will now raise its normal "
              "schema error." % (exc, p, backup), file=sys.stderr)
        try:
            tmp_new.unlink(missing_ok=True)
        except Exception:
            pass

class MemoryService:
    """High-level, validated operations over a single Engraphis database."""

    def __init__(self, engine: MemoryEngine, *,
                 allowed_workspaces: Optional[list] = None) -> None:
        self.engine = engine
        self.store = engine.store
        # Server-side workspace binding (the hard isolation boundary). None means
        # unrestricted (single-tenant local default); a non-empty set means every scoped
        # read/write must target one of these workspaces — see ``_authorize_workspace``.
        self.allowed_workspaces: Optional[frozenset] = (
            frozenset(allowed_workspaces) if allowed_workspaces else None
        )
        # Replicate an explicit service binding on the Store itself so no caller
        # (including a future sync path) can bypass it by calling Store directly.
        self.store.allowed_workspaces = self.allowed_workspaces
        # Workspaces whose graph has been lazily backfilled this process — see
        # ``graph()``. Guards against rescanning a workspace whose memories genuinely
        # yield no entities on every Graph-tab open.
        self._graph_backfilled: set = set()
        # Graph scenes are expensive derived views over the full canonical graph. Cache
        # a small number per service instance, keyed by both request parameters and the
        # SQLite connection revision. ``total_changes`` catches writes performed through
        # this connection; ``data_version`` catches commits from another connection.
        # Consequently a memory/entity/edge mutation invalidates cached scenes without a
        # stale TTL window, while repeated pan/filter/navigation reads stay comfortably
        # inside the dashboard's warm-response budget. Each value also carries the next
        # bi-temporal validity boundary: time passing can change a current-time scene even
        # when no connection writes, so such an entry expires exactly at that boundary.
        self._graph_scene_cache: "OrderedDict[tuple, tuple[float, dict]]" = OrderedDict()
        self._graph_job_lock = threading.RLock()
        self._graph_job_threads: dict[str, threading.Thread] = {}
        self._obsidian_job_threads: dict[str, threading.Thread] = {}
        self._graph_runner_id = make_id("device")
        self._service_close_lock = threading.Lock()
        self._closing = False
        self._closed = False

    def close(self, *, timeout: float = GRAPH_INDEX_SHUTDOWN_SECONDS) -> None:
        """Cancel owned graph workers before closing the shared Store.

        A provider-backed extractor can remain inside an in-flight call longer than the
        shutdown budget. In that case the Store deliberately stays open and this method
        raises: closing SQLite beneath a live worker would turn orderly shutdown into
        use-after-close races and partial terminal job records. The persisted runner lease
        lets the next process recover a worker that outlives process shutdown.
        """
        try:
            timeout_value = float(timeout)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("timeout must be a finite non-negative number") from None
        if not math.isfinite(timeout_value) or timeout_value < 0:
            raise ValueError("timeout must be a finite non-negative number")

        with self._service_close_lock:
            if self._closed:
                return
            self._closing = True
            with self._graph_job_lock:
                workers = list(self._graph_job_threads.items())

            with self._graph_job_lock:
                import_workers = list(self._obsidian_job_threads.items())
            for job_id, _thread in import_workers:
                self.store.conn.execute(
                    "UPDATE jobs SET cancel_requested=1 WHERE id=? AND state='running'",
                    (job_id,),
                )
            if import_workers:
                self.store.conn.commit()

            deadline = time.monotonic() + timeout_value
            for _job_id, thread in [*workers, *import_workers]:
                remaining = max(0.0, deadline - time.monotonic())
                thread.join(remaining)
            alive = [
                job_id for job_id, thread in [*workers, *import_workers]
                if thread.is_alive()
            ]
            if alive:
                raise RuntimeError(
                    f"{len(alive)} graph index worker(s) did not stop before shutdown"
                )

            close_engine = getattr(self.engine, "close", None)
            if callable(close_engine):
                close_engine()
            else:
                self.store.close()
            self._closed = True

    def _graph_scene_revision(self) -> tuple[int, int, int]:
        row = self.store.conn.execute("PRAGMA data_version").fetchone()
        data_version = int(row[0]) if row is not None else 0
        return (int(self.store.conn.total_changes), data_version,
                int(self.store.schema_version))

    def _graph_scene_valid_until(
        self, workspace_id: str, at: float, *, known_at: Optional[float] = None,
        world_time_floating: bool = True, system_time_floating: bool = True,
    ) -> float:
        """Earliest future boundary on either unanchored graph-scene time axis."""
        known = at if known_at is None else known_at
        sources = [
            ("edges edge", "edge.workspace_id=?", (workspace_id,), "edge"),
            (
                "edge_supports support JOIN edges parent_edge "
                "ON parent_edge.id=support.edge_id",
                "parent_edge.workspace_id=?",
                (workspace_id,),
                "support",
            ),
            ("memories memory", "memory.workspace_id=?", (workspace_id,), "memory"),
            (
                "mem_links link "
                "JOIN memories left_memory ON left_memory.id=link.a "
                "JOIN memories right_memory ON right_memory.id=link.b",
                "left_memory.workspace_id=? AND right_memory.workspace_id=?",
                (workspace_id, workspace_id),
                "link",
            ),
            (
                "code_memory_links link "
                "JOIN memories linked_memory ON linked_memory.id=link.memory_id "
                "JOIN repos linked_repo ON linked_repo.id=link.repo_id",
                "linked_memory.workspace_id=? AND linked_repo.workspace_id=?",
                (workspace_id, workspace_id),
                "link",
            ),
            (
                "symbols symbol "
                "JOIN repos symbol_repo ON symbol_repo.id=symbol.repo_id",
                "symbol_repo.workspace_id=?",
                (workspace_id,),
                "symbol",
            ),
            (
                "code_edges code_edge "
                "JOIN repos code_edge_repo ON code_edge_repo.id=code_edge.repo_id",
                "code_edge_repo.workspace_id=?",
                (workspace_id,),
                "code_edge",
            ),
        ]
        boundary_sql: list[str] = []
        boundary_params: list[Any] = []
        for source, scope, source_params, alias in sources:
            columns = []
            if world_time_floating:
                columns.extend(("valid_from", "valid_to"))
            if system_time_floating:
                columns.extend(("valid_to_recorded_at", "ingested_at", "expired_at"))
            for column in columns:
                # For start-boundary columns (valid_from, ingested_at), include rows
                # whose expired_at is still in the future — they become visible when
                # the start boundary passes and remain so until expiration. End-boundary
                # columns only matter on currently-active (non-expired) rows.
                extra_params: list[Any] = []
                reference = at if column in ("valid_from", "valid_to") else known
                if column in ("valid_from", "ingested_at"):
                    active = f" AND ({alias}.expired_at IS NULL OR {alias}.expired_at>?)"
                    extra_params = [known]
                elif column == "expired_at":
                    active = ""
                else:
                    active = f" AND ({alias}.expired_at IS NULL OR {alias}.expired_at>?)"
                    extra_params = [known]
                boundary_sql.append(
                    f"SELECT {alias}.{column} AS boundary FROM {source} "
                    f"WHERE {scope} AND {alias}.{column}>?{active}"
                )
                boundary_params.extend((*source_params, reference, *extra_params))
        # Entity rows use ``created_at`` as their temporal visibility boundary;
        # a clock-skewed sync can make an otherwise isolated entity appear later
        # without changing the graph revision.
        if system_time_floating:
            boundary_sql.append(
                "SELECT entity.created_at AS boundary FROM entities entity "
                "WHERE entity.workspace_id=? AND entity.created_at>?"
            )
            boundary_params.extend((workspace_id, known))
        row = self.store.conn.execute(
            "SELECT MIN(boundary) FROM (" + " UNION ALL ".join(boundary_sql) + ")",
            boundary_params,
        ).fetchone()
        return float(row[0]) if row is not None and row[0] is not None else math.inf

    @classmethod
    def create(cls, db_path: str = ":memory:", *, embed_model: Optional[str] = None,
               embed_revision: Optional[str] = None,
               require_immutable_models: bool = False,
               embed_dim: int = 384, vector_backend: str = "numpy",
               rerank_model: Optional[str] = None,
               rerank_revision: Optional[str] = None,
               allowed_workspaces: Optional[list] = None,
               extractor: Optional[str] = None,
               graph_extractor: Optional[str] = None,
               retention_supervisor: Optional[str] = None,
               allow_automatic_critical_retention: Optional[bool] = None,
               query_planner=None, read_only: bool = False) -> "MemoryService":
        database_path = str(db_path)
        physical_db_path = _physical_database_path(database_path)
        migration_allowed = (
            not _is_memory_database_path(database_path)
            and not read_only
            and not _is_read_only_database_uri(database_path)
        )
        # extractor / graph_extractor default to the configured backends
        # (ENGRAPHIS_EXTRACTOR — "none" | "chunk" | "llm" | "llm_structured";
        # ENGRAPHIS_GRAPH_EXTRACTOR — "regex" by default) so the dashboard,
        # auto-maintenance, MCP server, and CLI all honor the same config knob. An
        # explicit value (e.g. extractor="none") still overrides the environment.
        if (extractor is None or graph_extractor is None or retention_supervisor is None
                or allow_automatic_critical_retention is None):
            from engraphis.config import settings
            if extractor is None:
                extractor = settings.extractor
            if graph_extractor is None:
                graph_extractor = settings.graph_extractor
            if retention_supervisor is None:
                retention_supervisor = settings.retention_supervisor
            if allow_automatic_critical_retention is None:
                allow_automatic_critical_retention = settings.allow_automatic_critical_retention
        # One-time, safe upgrade path for a self-host whose ENGRAPHIS_DB_PATH already
        # holds a v1-shaped database (see docstring) — must run before Store() ever
        # touches the file. No-ops instantly for a fresh install or an already-v2 db.
        if migration_allowed:
            _auto_migrate_v1_if_needed(physical_db_path)
        # Optional encryption at rest: if ENGRAPHIS_DB_KEY[_FILE] is set, memories are
        # stored in a SQLCipher-encrypted database. Off by default (returns None).
        from engraphis.backends.encrypted_db import connector_from_env
        connect = connector_from_env()
        engine = MemoryEngine.create(
            database_path, embed_model=embed_model, embed_revision=embed_revision,
            require_immutable_models=require_immutable_models,
            embed_dim=embed_dim,
            vector_backend=vector_backend, rerank_model=rerank_model,
            rerank_revision=rerank_revision,
            extractor=extractor, graph_extractor=graph_extractor,
            retention_supervisor=retention_supervisor, connect=connect,
            allow_automatic_critical_retention=bool(allow_automatic_critical_retention),
            query_planner=query_planner, read_only=read_only,
        )
        if migration_allowed:
            try:
                _warn_if_db_empty_with_populated_sibling(physical_db_path)
            except Exception:  # noqa: BLE001 — diagnostics never block startup
                pass
        return cls(engine, allowed_workspaces=allowed_workspaces)

    # ── name → id resolution ───────────────────────────────────────────────────
    def _lookup_workspace(self, name: str) -> Optional[str]:
        row = self.store.conn.execute(
            "SELECT id FROM workspaces WHERE name=?", (name,)
        ).fetchone()
        return row["id"] if row else None

    def _lookup_repo(self, workspace_id: str, name: str) -> Optional[str]:
        row = self.store.conn.execute(
            "SELECT id FROM repos WHERE workspace_id=? AND (name=? OR id=?) "
            "ORDER BY CASE WHEN id=? THEN 0 ELSE 1 END LIMIT 1",
            (workspace_id, name, name, name),
        ).fetchone()
        return row["id"] if row else None

    def _require_scope(self, workspace: str, repo: Optional[str]) -> tuple[str, Optional[str]]:
        """Resolve workspace/repo names to ids for tools where "not found yet" is a
        user error, not a quiet empty result (unlike ``recall``'s gentler UX)."""
        ws = self._clean_ws(workspace)
        wid = self._lookup_workspace(ws)
        if wid is None:
            raise ValidationError(f"no workspace named '{ws}' yet")
        rid = None
        if repo:
            rp = _clean_name(repo, field="repo")
            rid = self._lookup_repo(wid, rp)
            if rid is None:
                raise ValidationError(f"no repo named '{rp}' in workspace '{ws}' yet")
        return wid, rid

    def _visible_workspace_ids(self) -> list[str]:
        """Return workspace ids visible to the current caller for global accounting."""
        rows = self.store.conn.execute(
            "SELECT id, name FROM workspaces ORDER BY name"
        ).fetchall()
        visible: list[str] = []
        for row in rows:
            try:
                self._clean_ws(row["name"])
            except ValidationError:
                continue
            visible.append(str(row["id"]))
        return visible

    def _authorize_workspace(self, ws: str) -> str:
        """Enforce the server-side workspace binding. When this instance is bound to a set
        of workspaces, no caller may read or write a workspace outside it — knowing or
        guessing the name is not enough. This is what makes
        ``workspace`` a *hard* isolation boundary rather than an advisory label the client
        asserts and the server trusts (scope is enforced server-side on
        every read/write — never trust client-supplied scope alone). An empty binding — the
        single-tenant local default — is unrestricted, so existing setups are unaffected.

        In team mode it *also* enforces per-user ownership of **personal** folders: a
        folder created ``visibility='personal'`` is readable and writable only by the user
        who owns it, even by an admin. Because every workspace-scoped read/write routes
        through ``_clean_ws`` → here, that ownership check can never be skipped at an
        individual call site (same reasoning the binding check relies on). Outside team
        mode there is no current user, so this is a no-op and shared/single-tenant
        behaviour is unchanged."""
        if self.allowed_workspaces is not None and ws not in self.allowed_workspaces:
            raise WorkspaceBindingError()
        self._enforce_personal_access(ws)
        return ws

    def _workspace_visibility(self, ws: str) -> tuple[str, str]:
        """Return ``(visibility, owner)`` for an existing workspace.

        Access-control metadata is part of the authorization boundary, so lookup and
        parsing failures must not be treated as a shared workspace.  A missing settings
        value remains the legacy shared default; an explicitly malformed or incomplete
        personal declaration fails closed instead of allowing every authenticated user in.
        """
        row = self.store.conn.execute(
            "SELECT settings FROM workspaces WHERE name=?", (ws,)).fetchone()
        if row is None or not row["settings"]:
            return ("shared", "")
        try:
            settings = json.loads(row["settings"])
        except (TypeError, ValueError, RecursionError) as exc:
            raise ValidationError("workspace access settings are invalid") from exc
        if not isinstance(settings, dict):
            raise ValidationError("workspace access settings are invalid")
        visibility = settings.get("visibility")
        if visibility is None:
            return ("shared", "")
        if visibility == "shared":
            # A shared workspace's owner is its controller/original sharer, not an
            # access restriction. Preserve a valid controller so they can reverse
            # their own sharing decision; malformed legacy values grant no control.
            owner = settings.get("owner")
            return ("shared", owner.strip() if isinstance(owner, str) else "")
        if visibility != "personal":
            raise ValidationError("workspace access settings are invalid")
        owner = settings.get("owner")
        if not isinstance(owner, str) or not owner.strip():
            raise ValidationError("personal workspace has no valid owner")
        return ("personal", owner.strip())

    def _authorize_workspace_control(self, ws: str) -> None:
        """Require the original sharer or an admin for whole-workspace mutations."""
        user = _authenticated_principal()
        if user is None:
            return
        _visibility, owner = self._workspace_visibility(ws)
        if user.get("role") == "admin" or (
                owner and str(owner).casefold() == user["email"]):
            return
        raise ValidationError(
            "only the original sharer or an admin can modify the whole workspace"
        )

    def _get_or_create_workspace(self, ws: str) -> str:
        """Get ``ws`` or create it private to the authenticated team user.

        Writes can create a workspace without first using the dashboard's explicit
        Create-folder dialog. That path must obey the same safe default, otherwise an
        agent or import could silently create a team-visible folder. Non-team callers do
        not have an identity and retain the established single-tenant behaviour.
        """
        user = _authenticated_principal()
        owner = user["email"] if user is not None else ""
        workspace_settings = {"visibility": "personal", "owner": owner} if owner else None
        # The read-then-create sequence used to race when two first-use requests
        # arrived together.  The Store helper serializes the insert and re-reads the
        # winner, so retries converge on one workspace instead of surfacing a UNIQUE
        # constraint error to the caller.
        workspace_id = self.store.get_or_create_workspace(ws, settings=workspace_settings)
        # The insert may have raced with another authenticated creator.  Re-run the
        # ownership check against the durable winner before any caller can use its id;
        # otherwise the loser could write into the winner's personal workspace.
        self._enforce_personal_access(ws)
        return workspace_id

    def _enforce_personal_access(self, ws: str) -> None:
        """Block access to another user's personal folder. No current user (single-tenant,
        standalone MCP, CLI, sync, tests) → no restriction. A shared folder, or a personal folder the
        current user owns → allowed. A personal folder owned by someone else → refused,
        with a message that neither confirms nor denies the folder's contents beyond the
        fact that it's private (the name is already known to the caller who supplied it)."""
        user = _authenticated_principal()
        if user is None:
            return
        vis, owner = self._workspace_visibility(ws)
        if (vis == "personal" and owner
                and str(owner).casefold() != user["email"]):
            raise ValidationError(f"workspace '{ws}' is a personal folder of another user")

    def _clean_ws(self, workspace: Any) -> str:
        """Validate a workspace name *and* enforce the binding in one step. Every entry point
        that accepts a client-supplied workspace routes through here, so the isolation check
        can never be skipped at an individual call site."""
        return self._authorize_workspace(_clean_name(workspace, field="workspace"))

    def _check_owns(self, memory_id: str, wid: str, rid: Optional[str]) -> None:
        """Governance tools (forget/pin/correct/link) act on a bare memory_id; require the
        caller to also name the workspace (and optionally repo) it believes owns the memory,
        and verify that before mutating anything. Without this check, any caller who has
        seen an id — e.g. from a recall, why, or timeline result — could forget, pin, correct,
        or link a memory that belongs to a workspace it has no other access to. The
        memory-poisoning threat model (SECURITY.md) cuts both ways: governance tools are
        an attack/mistake surface too if they aren't scope-checked like every read tool is."""
        rec = self.store.get_memory(memory_id)
        if rec is None:
            raise ValidationError(f"no memory with id '{memory_id}'")
        if rec.workspace_id != wid or (rid is not None and rec.repo_id != rid):
            raise ValidationError(f"memory '{memory_id}' does not belong to that workspace/repo")
        self._authorize_memory_session(rec)

    def _authorize_memory_session(self, rec: Any) -> None:
        """Authorize the owner of a session-private memory reached by bare id.

        Workspace/repo equality is insufficient for session scope in a shared Team
        workspace. Bare-id governance and inspector paths must resolve the associated
        session before they may read or mutate the record.
        """
        if rec.scope != Scope.SESSION:
            return
        if not rec.session_id:
            raise ValidationError("session-scoped memory has no owning session")
        session = self.store.get_session(rec.session_id)
        if session is None:
            raise ValidationError("session-scoped memory has no owning session")
        self._authorize_session(session)

    def _memory_visible_to_caller(self, rec: Any) -> bool:
        try:
            self._authorize_memory_session(rec)
        except ValidationError:
            return False
        return True

    def _authorize_session(self, session: dict) -> None:
        """Enforce workspace and authenticated-user ownership for a session row.

        Session tools accept a bare typed id, so they cannot rely on a caller-supplied
        workspace passing through ``_clean_ws``. Resolve the owning workspace here and
        apply the same server binding/personal-folder boundary as every named operation.
        Team sessions carry a stable ``user_id``; another authenticated user may neither
        receive their handoff nor read, write, or close that user's session. Legacy rows
        without an owner remain available only in trusted local-owner mode, never through
        an authenticated principal.
        """
        row = self.store.conn.execute(
            "SELECT name FROM workspaces WHERE id=?", (session["workspace_id"],)
        ).fetchone()
        if row is None:
            raise ValidationError("session belongs to an unknown workspace")
        self._authorize_workspace(row["name"])
        user = _authenticated_principal()
        owner_id = str(session.get("user_id") or "")
        if user is not None:
            if not owner_id:
                raise ValidationError("session has no authenticated owner")
            if owner_id != user["id"]:
                raise ValidationError("session belongs to another user")

    def _session_for_write(self, session_id: Optional[str], wid: str,
                           rid: Optional[str]) -> Optional[dict]:
        if not session_id:
            return None
        sid = _clean_text(session_id, field="session_id", max_chars=MAX_NAME_CHARS)
        session = self.store.get_session(sid)
        if session is None:
            raise ValidationError(f"no session with id '{sid}'")
        if session["workspace_id"] != wid or (
                rid is not None and session.get("repo_id") != rid):
            raise ValidationError("session_id does not belong to that workspace/repo")
        self._authorize_session(session)
        if session.get("status") != "active":
            raise ValidationError("session_id is not active")
        return session

    # ── write ──────────────────────────────────────────────────────────────────
    def remember(self, content: str, *, workspace: str, repo: Optional[str] = None,
                 session_id: Optional[str] = None, mtype: str = "semantic",
                 scope: Optional[str] = None, title: str = "", importance: float = 0.0,
                 keywords: Optional[list] = None, metadata: Optional[dict] = None,
                 source: str = "agent", trusted: bool = False,
                 kind: Optional[str] = None, resolve_conflicts: bool = True,
                 retention_class: Optional[str] = None,
                 retention_reason: str = "",
                 valid_from: Optional[float] = None,
                 subject_key: str = "", claim_kind: str = "",
                 _local_cli_operator: bool = False,
                 _local_agent_operator: bool = False,
                 _ingress: str = "service") -> dict:
        """Store one memory. Returns its id, resolved scope, and the resolution
        outcome (``op``: add/noop/invalidate/relate — see
        ``MemoryEngine.remember_with_resolution``).
        """
        content = _clean_text(content, field="content", max_chars=MAX_CONTENT_CHARS)
        title = _clean_text(title, field="title", max_chars=MAX_TITLE_CHARS, required=False)
        _reject_secret_capture((
            ("content", content), ("title", title), ("keywords", keywords),
            ("metadata", metadata), ("subject_key", subject_key), ("claim_kind", claim_kind),
        ))
        local_agent_provenance = (
            _local_agent_provenance(source, ingress=_ingress)
            if _local_agent_operator else None
        )
        provenance = (
            _local_cli_provenance()
            if _local_cli_operator else
            local_agent_provenance
            if local_agent_provenance is not None else
            _canonical_write_provenance(
                source, trusted, raw_ingest=False, ingress=_ingress
            )
        )
        ws = self._clean_ws(workspace)
        rp = _clean_name(repo, field="repo") if repo else None
        mt = _enum(mtype, MemoryType, "mtype")
        scope_was_omitted = scope is None
        sc = _write_scope(scope, repo=rp, session_id=session_id)
        kws = _clean_keywords(keywords)
        meta = _clean_metadata(metadata)
        valid_from = _optional_timestamp(valid_from, field="valid_from")
        subject_key = _clean_text(
            subject_key, field="subject_key", max_chars=MAX_TITLE_CHARS, required=False
        )
        claim_kind = _clean_text(
            claim_kind, field="claim_kind", max_chars=MAX_NAME_CHARS, required=False
        )
        retention = None
        if retention_class:
            label = _clean_text(
                retention_class, field="retention_class", max_chars=40
            ).lower()
            if label not in {"ephemeral", "normal", "critical"}:
                raise ValidationError(
                    "retention_class must be one of: ephemeral, normal, critical"
                )
            reason = _clean_text(
                retention_reason, field="retention_reason",
                max_chars=MAX_TITLE_CHARS, required=False,
            )
            retention = {"source": "host", "label": label, "retain": True,
                         "reason": reason}
            meta = {**meta, "retention_supervision": retention}
        try:
            importance = float(importance)
        except (TypeError, ValueError, OverflowError):
            raise ValidationError("importance must be a number")
        if not math.isfinite(importance):
            raise ValidationError("importance must be finite")
        importance = max(0.0, min(1.0, importance))

        wid = self._get_or_create_workspace(ws)
        rid = self.store.get_or_create_repo(wid, rp) if rp else None
        session = self._session_for_write(session_id, wid, rid)
        if sc in (Scope.SESSION, Scope.REPO) and rid is None and session:
            rid = session.get("repo_id")
            if rid:
                row = self.store.conn.execute(
                    "SELECT name FROM repos WHERE id=?", (rid,)
                ).fetchone()
                rp = row["name"] if row else None
        if sc == Scope.REPO and rid is None:
            if scope_was_omitted:
                sc = Scope.WORKSPACE
            else:
                raise ValidationError("repo scope requires a repo-backed session_id")
        if kind:
            provenance["kind"] = _clean_name(kind, field="kind")
        try:
            result = self.engine.remember_with_resolution(
                content, workspace_id=wid, repo_id=rid, session_id=session_id,
                mtype=mt, scope=sc, title=title, importance=importance,
                keywords=kws, metadata={**meta, "provenance": provenance},
                valid_from=valid_from,
                subject_key=subject_key, claim_kind=claim_kind,
                resolve_conflicts=bool(resolve_conflicts),
            )
        except ValueError as exc:
            if str(exc).startswith("valid_from "):
                raise ValidationError(str(exc)) from exc
            if session_id and str(exc) in {
                f"no session with id '{session_id}'",
                "session_id does not belong to that workspace/repo",
                "session_id is not active",
            }:
                raise ValidationError(str(exc)) from exc
            raise
        out = {
            "id": result["id"], "workspace": ws, "repo": rp,
            "scope": sc.value, "mtype": mt.value, "stored": True, "op": result["op"],
        }
        if result["op"] in ("noop", "invalidate", "relate"):
            out["resolution"] = result.get("reason", "")
        if result["op"] == "invalidate":
            out["superseded"] = result["superseded"]
        if result["op"] == "relate":
            out["related_to"] = result.get("related_to")
        if result["op"] == "quarantined":
            # These are policy/reason codes only — never copy hostile payload text into
            # a caller response or receipt merely to explain why it was quarantined.
            out.update({
                "quarantined": True,
                "policy": result.get("policy", ""),
                "reasons": list(result.get("reasons") or []),
            })
        out["receipt"] = self.store.record_receipt(
            "remember", workspace_id=wid, repo_id=rid or "", actor=provenance["source"],
            target_count=1, status=result["op"],
            metadata={"mtype": mt.value, "scope": sc.value, "resolution": result["op"],
                      "retention": (retention or {}).get("label", ""),
                      "quarantined": bool(result.get("quarantined")),
                      "quarantine_policy": result.get("policy", ""),
                      "quarantine_reasons": list(result.get("reasons") or [])},
        )
        return out

    def remember_local_cli(self, content: str, *, workspace: str, title: str = "",
                           metadata: Optional[dict] = None,
                           resolve_conflicts: bool = True) -> dict:
        """Store an explicit local operator command as prompt-eligible memory.

        This is intentionally a narrow in-process capability for ``engraphis-cli``.
        It reuses the normal service validation, workspace authorization, resolution,
        receipts, and storage path, but records an approved local-owner provenance.
        Do not expose it through HTTP, MCP, dashboard, or import routes: those are
        transport boundaries and must continue through the pending-review write path.
        """
        return self.remember(
            content,
            workspace=workspace,
            title=title,
            metadata=metadata,
            source="cli",
            trusted=True,
            resolve_conflicts=resolve_conflicts,
            _local_cli_operator=True,
        )

    @_rollback_service_transaction
    def remember_batch(self, memories: list[dict], *, workspace: str) -> dict:
        """Store multiple memories in a single atomic transaction.

        Each item in *memories* accepts the same keyword arguments as
        :meth:`remember` (``content`` is the only required key).  The entire
        batch runs inside one ``BEGIN IMMEDIATE`` transaction via the
        ``@_rollback_service_transaction`` decorator; unexpected engine errors
        roll back every write, while per-item *validation* failures are caught
        and reported without aborting the rest of the batch.

        Returns a dict with ``total``, ``succeeded``, ``failed``, and a
        ``results`` list carrying per-item resolution (``op``: add / noop /
        invalidate / relate / quarantined) or an ``error`` string.
        """
        if not isinstance(memories, list):
            raise ValidationError("memories must be a list")
        if not memories:
            raise ValidationError("memories list must not be empty")
        if len(memories) > 50:
            raise ValidationError("memories list must not exceed 50 items")

        ws = self._clean_ws(workspace)
        results: list[dict] = []
        failed_indices: list[int] = []

        for idx, mem in enumerate(memories):
            if not isinstance(mem, dict):
                results.append({"index": idx, "status": "error",
                                "error": "each memory must be a dict"})
                failed_indices.append(idx)
                continue
            content = mem.get("content")
            if not content or not isinstance(content, str) or not content.strip():
                results.append({"index": idx, "status": "error",
                                "error": "content is required and must be a non-empty string"})
                failed_indices.append(idx)
                continue
            try:
                result = self.remember(
                    content,
                    workspace=ws,
                    repo=mem.get("repo"),
                    session_id=mem.get("session_id"),
                    mtype=mem.get("mtype", "semantic"),
                    scope=mem.get("scope"),
                    title=mem.get("title", ""),
                    importance=mem.get("importance", 0.0),
                    keywords=mem.get("keywords"),
                    metadata=mem.get("metadata"),
                    source=mem.get("source", "agent"),
                    trusted=mem.get("trusted", False),
                    kind=mem.get("kind"),
                    resolve_conflicts=mem.get("resolve_conflicts", mem.get("dedupe", True)),
                    retention_class=mem.get("retention_class"),
                    retention_reason=mem.get("retention_reason", ""),
                    valid_from=mem.get("valid_from"),
                    subject_key=mem.get("subject_key", ""),
                    claim_kind=mem.get("claim_kind", ""),
                )
                results.append({"index": idx, "status": "ok", **result})
            except (ValidationError, ValueError) as exc:
                results.append({"index": idx, "status": "error",
                                "error": str(exc)})
                failed_indices.append(idx)

        return {
            "workspace": ws,
            "total": len(memories),
            "succeeded": len(memories) - len(failed_indices),
            "failed": len(failed_indices),
            "results": results,
        }

    def ingest(self, content: str, *, workspace: str, repo: Optional[str] = None,
               session_id: Optional[str] = None, mtype: str = "semantic",
               scope: Optional[str] = None, metadata: Optional[dict] = None,
               source: str = "agent", trusted: bool = False,
               kind: Optional[str] = None, resolve_conflicts: bool = True,
               _local_agent_operator: bool = False,
               _ingress: str = "service") -> dict:
        """Store raw, undistilled text. With an extractor configured (ENGRAPHIS_EXTRACTOR)
        the text is first distilled into discrete typed facts; without one this behaves
        exactly like ``remember``. Normal local-agent ingest is prompt-visible after
        validation; explicitly external sources remain pending, and detector matches
        are quarantined before they can surface."""
        content = _clean_text(content, field="content", max_chars=MAX_CONTENT_CHARS)
        _reject_secret_capture((("content", content), ("metadata", metadata)))
        local_agent_provenance = (
            _local_agent_provenance(source, ingress=_ingress)
            if _local_agent_operator else None
        )
        provenance = local_agent_provenance or _canonical_write_provenance(
            source, trusted, raw_ingest=True, ingress=_ingress
        )
        ws = self._clean_ws(workspace)
        rp = _clean_name(repo, field="repo") if repo else None
        mt = _enum(mtype, MemoryType, "mtype")
        scope_was_omitted = scope is None
        sc = _write_scope(scope, repo=rp, session_id=session_id)
        meta = _clean_metadata(metadata)
        wid = self._get_or_create_workspace(ws)
        rid = self.store.get_or_create_repo(wid, rp) if rp else None
        session = self._session_for_write(session_id, wid, rid)
        if sc in (Scope.SESSION, Scope.REPO) and rid is None and session:
            rid = session.get("repo_id")
            if rid:
                row = self.store.conn.execute(
                    "SELECT name FROM repos WHERE id=?", (rid,)
                ).fetchone()
                rp = row["name"] if row else None
        if sc == Scope.REPO and rid is None:
            if scope_was_omitted:
                sc = Scope.WORKSPACE
            else:
                raise ValidationError("repo scope requires a repo-backed session_id")
        if kind:
            provenance["kind"] = _clean_name(kind, field="kind")
        try:
            out = self.engine.ingest(
                content, workspace_id=wid, repo_id=rid, session_id=session_id, scope=sc,
                default_mtype=mt, metadata={**meta, "provenance": provenance},
                resolve_conflicts=bool(resolve_conflicts),
            )
        except ValueError as exc:
            if session_id and str(exc) in {
                f"no session with id '{session_id}'",
                "session_id does not belong to that workspace/repo",
                "session_id is not active",
            }:
                raise ValidationError(str(exc)) from exc
            raise
        result = {"workspace": ws, "repo": rp, "count": out["count"],
                  "extracted": out["extracted"],
                  "facts": [{"id": r["id"], "op": r["op"],
                             **({"superseded": r["superseded"]}
                                if "superseded" in r else {}),
                             **({"quarantined": True,
                                 "policy": r.get("policy", ""),
                                 "reasons": list(r.get("reasons") or [])}
                                if r.get("quarantined") else {})}
                            for r in out["facts"]]}
        result["receipt"] = self.store.record_receipt(
            "remember", workspace_id=wid, repo_id=rid or "", actor=provenance["source"],
            target_count=out["count"], status="ingested",
            metadata={"extracted": bool(out["extracted"]), "mtype": mt.value,
                      "scope": sc.value},
        )
        return result

    # Intent-native agent protocol. These wrappers intentionally stay transport-agnostic:
    # REST and MCP can expose the same remember/link/recall vocabulary without leaking
    # SQLite operations into agent prompts.
    def intent_remember(self, text: str, *, workspace: str,
                        repo: Optional[str] = None, title: str = "",
                        mtype: str = "semantic", scope: Optional[str] = None,
                        importance: float = 0.0,
                        metadata: Optional[dict] = None,
                        retention_class: Optional[str] = None,
                         retention_reason: str = "",
                         valid_from: Optional[float] = None,
                         subject_key: str = "", claim_kind: str = "",
                         _local_agent_operator: bool = False,
                         _ingress: str = "intent_api") -> dict:
        out = self.remember(
            text, workspace=workspace, repo=repo, title=title, mtype=mtype,
            scope=scope, importance=importance, metadata=metadata,
            retention_class=retention_class, retention_reason=retention_reason,
            # Dashboard intent is a local agent-protocol write.  It may create a
            # prompt-visible memory immediately; external/imported sources still
            # remain review-gated by the canonical service boundary.
            valid_from=valid_from, subject_key=subject_key, claim_kind=claim_kind,
            source="intent_api", trusted=False,
            _local_agent_operator=_local_agent_operator, _ingress=_ingress,
        )
        return {"operation": "remember", **out}

    def intent_link(self, source_id: str, target_id: str, *, workspace: str,
                    repo: Optional[str] = None, relation: str = "related",
                    layer: Optional[str] = None, reason: str = "") -> dict:
        return {"operation": "link", **self.link(
            source_id, target_id, workspace=workspace, repo=repo,
            relation=relation, layer=layer, reason=reason,
        )}

    def intent_recall(self, query: str, *, intent: str = "recall",
                      workspace: Optional[str] = None, repo: Optional[str] = None,
                      mtypes: Optional[list] = None, k: int = 8,
                      as_of: Optional[float] = None,
                      valid_at: Optional[float] = None,
                      known_at: Optional[float] = None,
                      token_budget: Optional[int] = None,
                      retrieval_profile: str = "balanced",
                      candidate_depth: str = "fixed",
                      response_mode: str = "full",
                      diagnostics: bool = False,
                      planning: str = "off",
                      mtype_limits: Optional[dict] = None,
                      reinforce: bool = False,
                      record_receipt: bool = True) -> dict:
        intent_clean = _clean_text(
            intent, field="intent", max_chars=80, required=False
        ) or "recall"
        normalized = intent_clean.lower().replace("-", "_").replace(" ", "_")
        layers = {
            "explain": ["causal", "entity", "semantic"],
            "why": ["causal", "entity", "semantic"],
            "causal": ["causal", "entity"],
            "summarize_history": ["temporal", "causal", "semantic"],
            "history": ["temporal", "causal", "semantic"],
            "timeline": ["temporal", "entity"],
            "locate_code": ["entity", "semantic"],
            "code": ["entity", "semantic"],
        }.get(normalized)
        out = self.recall(
            query, workspace=workspace, repo=repo, mtypes=mtypes, k=k,
            as_of=as_of, valid_at=valid_at, known_at=known_at,
            token_budget=token_budget, retrieval_profile=retrieval_profile,
            candidate_depth=candidate_depth,
            response_mode=response_mode, diagnostics=diagnostics,
            planning=planning, mtype_limits=mtype_limits,
            intent=intent_clean, graph_layers=layers,
            reinforce=reinforce, record_receipt=record_receipt,
        )
        response = {"operation": "recall", "intent": intent_clean, **out}
        if normalized in {"locate_code", "code"} and workspace and repo:
            response["code"] = self.search_code(
                query, workspace=workspace, repo=repo, limit=k,
                as_of=as_of, valid_at=valid_at, known_at=known_at,
            )
        elif normalized in {"explain", "why"} and workspace:
            response["explanation"] = self.why(
                query, workspace=workspace, repo=repo, k=min(k, 10),
                as_of=as_of, valid_at=valid_at, known_at=known_at,
            )
        elif normalized in {"summarize_history", "history", "timeline"} and workspace:
            response["history"] = self.timeline(
                query, workspace=workspace, repo=repo, limit=min(max(k * 2, 10), 50),
                as_of=as_of, valid_at=valid_at, known_at=known_at,
            )
        return response

    # ── folder / file import (dashboard "Import" section) ────────────────────────
    def _import_one(self, name: str, content: str, *, ws: str, mt: MemoryType,
                    kind: str, extra_provenance: Optional[dict] = None,
                    resource_title: str = "") -> dict:
        """Shared per-file ingest for ``import_folder``/``import_files``: one memory per
        file, workspace-scoped, always marked untrusted (SECURITY.md §5/§1 — imported
        content did not originate from an already-trusted agent write, so it must not be
        able to launder itself into a trusted fact at merge time; see
        ``core/engine.py``'s merge trust rule).

        When the configured extractor is the *offline* ``ChunkingExtractor``, a file is
        split into several retrieval-sized memories instead of one — each still untrusted,
        each stamped with ``provenance``/``metadata.chunk`` linking it to its file and
        position. An LLM/custom extractor is never applied by this base import pass.
        Callers must explicitly opt into the separate ``derive_facts`` pass, which may
        send content to the configured provider (SECURITY.md §6). With no extractor
        (the default) behaviour is byte-for-byte unchanged."""
        if not content.strip():
            return {"file": name, "skipped": True}
        fallback = Path(name).stem or name
        extractor = getattr(self.engine, "extractor", None)
        chunker = (
            extractor if isinstance(extractor, ChunkingExtractor)
            else ChunkingExtractor() if len(content) > MAX_CONTENT_CHARS
            else None
        )
        chunks = chunker.extract(content) if chunker is not None else None
        try:
            if chunks:
                total = len(chunks)
                first: Optional[dict] = None
                for i, fact in enumerate(chunks):
                    title = (
                        fact.title or resource_title
                        or _title_from_content(fact.content, fallback)
                    )
                    r = self.remember(
                        fact.content, workspace=ws,
                        mtype=(fact.mtype.value if fact.mtype else mt.value),
                        scope="workspace", title=title[:MAX_TITLE_CHARS],
                        source="import", trusted=False, kind=kind,
                        keywords=fact.keywords,
                        metadata={**(extra_provenance or {}), "import_file": name,
                                  "chunk": {"index": i, "of": total,
                                            "heading": (fact.title or "")[:200]}},
                        resolve_conflicts=False,
                    )
                    first = first or r
                return {"file": name, "id": first["id"], "op": first["op"], "chunks": total}
            title = resource_title or _title_from_content(content, fallback=fallback)
            r = self.remember(
                content, workspace=ws, mtype=mt.value, scope="workspace",
                title=title[:MAX_TITLE_CHARS], source="import", trusted=False, kind=kind,
                metadata={**(extra_provenance or {}), "import_file": name},
            )
            return {"file": name, "id": r["id"], "op": r["op"]}
        except ValidationError as exc:
            logger.info("uploaded resource import rejected (%s)", type(exc).__name__)
            return {"file": name, "error": "resource could not be imported"}

    def _derive_import_facts(self, content: str, *, ws: str, mt: MemoryType,
                             resource_name: str, resource_kind: str,
                             resource_meta: dict) -> tuple[int, str]:
        """Run the explicitly requested second-pass extractor without duplicating
        deterministic chunking already performed by ``_import_one``."""
        extractor = getattr(self.engine, "extractor", None)
        if extractor is None:
            return 0, "fact derivation requested but no extractor is configured"
        if isinstance(extractor, ChunkingExtractor):
            return 0, (
                "fact derivation skipped because the configured chunk extractor "
                "already ran during import"
            )

        inputs = [content]
        if len(content) > MAX_CONTENT_CHARS:
            inputs = [fact.content for fact in ChunkingExtractor().extract(content)]

        created = 0
        extracted = False
        for chunk in inputs:
            derived = self.ingest(
                chunk, workspace=ws, mtype=mt.value, scope="workspace",
                metadata={"derived_from_resource": resource_name, **resource_meta},
                source="resource_extractor", trusted=False,
                kind=f"{resource_kind}_facts",
            )
            extracted = extracted or bool(derived["extracted"])
            created += sum(
                1 for fact in derived["facts"] if fact.get("op") != "noop"
            )
        if not extracted or created == 0:
            return created, "configured extractor produced no new discrete facts"
        return created, ""

    @_rollback_service_transaction
    def import_folder(self, *, workspace: str, path: str, file_pattern: str = "*.md",
                      memory_type: str = "semantic", actor: str = "user",
                      derive_facts: bool = False) -> dict:
        """Import files from a directory on the machine running Engraphis into
        ``workspace``, one memory per file. Restores the retired v1 vault
        ``/memory/vaults/import-folder`` capability as a first-class v2 feature (the old
        endpoint wrote to the v1 namespace store, invisible to this — the v2 — dashboard).
        The path is resolved and checked by ``_resolve_import_root`` before anything
        under it is touched (SECURITY.md §5); every imported memory is marked
        ``trusted: false`` (SECURITY.md §1) since the content is disk-local text this
        instance did not author."""
        ws = self._clean_ws(workspace)
        mt = _enum(memory_type, MemoryType, "memory_type")
        pattern = _clean_text(file_pattern, field="file_pattern", max_chars=MAX_NAME_CHARS,
                              required=False) or "*.md"
        raw_path = _clean_text(path, field="path", max_chars=MAX_CONTENT_CHARS)
        actor = _clean_text(actor, field="actor", max_chars=MAX_NAME_CHARS,
                            required=False) or "user"

        folder = _resolve_import_root(raw_path)
        wid = self._get_or_create_workspace(ws)
        files = _iter_import_files(folder, pattern, MAX_IMPORT_FILES)
        total_bytes = 0
        for file in files:
            try:
                total_bytes += file.stat().st_size
            except OSError:
                continue
        if total_bytes > MAX_IMPORT_TOTAL_BYTES:
            raise ValidationError(
                f"import batch is too large (max {MAX_IMPORT_TOTAL_BYTES} bytes)"
            )
        from engraphis.backends.resources import get_resource_extractor
        resource_extractor = get_resource_extractor()

        imported, skipped, errors, derived_facts = 0, 0, 0, 0
        details, warnings = [], []
        for f in files:
            try:
                if f.stat().st_size > MAX_IMPORT_RESOURCE_BYTES:
                    errors += 1
                    details.append({"file": f.name, "error": "file too large"})
                    continue
                resource = resource_extractor.extract_path(str(f))
            except (OSError, ValueError) as exc:
                if "no extractable text" in str(exc):
                    skipped += 1
                    continue
                logger.warning("folder import failed for one file (%s)", type(exc).__name__)
                errors += 1
                details.append({"file": f.name, "error": "file could not be imported"})
                continue
            rel = f.relative_to(folder).as_posix()
            resource_meta = {
                **resource.metadata,
                "media_type": resource.media_type,
                "resource_kind": resource.kind,
                "warnings": resource.warnings,
            }
            result = self._import_one(
                rel, resource.text, ws=ws, mt=mt, kind="file_import",
                extra_provenance={"import_path": rel, **resource_meta},
                resource_title=resource.title,
            )
            if result.get("skipped"):
                skipped += 1
                continue
            elif result.get("error"):
                errors += 1
                details.append(result)
                continue
            else:
                imported += 1
            file_warnings = list(resource.warnings)
            if derive_facts:
                try:
                    count, note = self._derive_import_facts(
                        resource.text, ws=ws, mt=mt, resource_name=rel,
                        resource_kind=resource.kind, resource_meta=resource_meta,
                    )
                    derived_facts += count
                    if note:
                        file_warnings.append(note)
                except (OSError, ValueError) as exc:
                    logger.warning("fact derivation failed for one file (%s)",
                                   type(exc).__name__)
                    file_warnings.append("fact derivation failed")
            if file_warnings:
                warnings.append({"file": rel, "warnings": file_warnings})

        self.store.audit(actor, "import_folder", wid,
                         f"{raw_path} ({imported} imported)")
        self.store.conn.commit()
        return {"workspace": ws, "path": str(folder), "scanned": len(files),
                "imported": imported, "skipped": skipped, "errors": errors,
                "derived_facts": derived_facts, "details": details[:50],
                "warnings": warnings[:50]}

    @_rollback_service_transaction
    def import_files(self, *, workspace: str, files: list, memory_type: str = "semantic",
                     actor: str = "user", derive_facts: bool = False) -> dict:
        """Drag-and-drop / picked-file counterpart to ``import_folder``: ingest
        browser-uploaded file bytes through the local resource extractor. This method has
        no transport dependency, matching the rest of the facade, and applies the same
        untrusted-by-default marking as ``import_folder``."""
        ws = self._clean_ws(workspace)
        mt = _enum(memory_type, MemoryType, "memory_type")
        actor = _clean_text(actor, field="actor", max_chars=MAX_NAME_CHARS,
                            required=False) or "user"
        if not isinstance(files, (list, tuple)):
            raise ValidationError("files must be a list")
        if len(files) > MAX_IMPORT_FILES:
            raise ValidationError(f"too many files (max {MAX_IMPORT_FILES})")

        total_bytes = 0
        for item in files:
            if not isinstance(item, dict):
                continue
            raw = item.get("data")
            content = item.get("content")
            if raw is None and isinstance(content, str):
                raw = content.encode("utf-8")
            if isinstance(raw, (bytes, bytearray)):
                total_bytes += len(raw)
        if total_bytes > MAX_IMPORT_TOTAL_BYTES:
            raise ValidationError(
                f"import batch is too large (max {MAX_IMPORT_TOTAL_BYTES} bytes)"
            )

        wid = self._get_or_create_workspace(ws)
        from engraphis.backends.resources import get_resource_extractor
        resource_extractor = get_resource_extractor()
        imported, skipped, errors, derived_facts = 0, 0, 0, 0
        details, warnings = [], []
        for item in files:
            if not isinstance(item, dict):
                errors += 1
                continue
            name = _clean_text(item.get("name"), field="name", max_chars=MAX_NAME_CHARS,
                               required=False) or "untitled"
            raw = item.get("data")
            content = item.get("content")
            if raw is None and isinstance(content, str):
                raw = content.encode("utf-8")
            if not isinstance(raw, (bytes, bytearray)):
                errors += 1
                details.append({"file": name, "error": "content must be text or data bytes"})
                continue
            if len(raw) > MAX_IMPORT_RESOURCE_BYTES:
                errors += 1
                details.append({"file": name, "error": "file too large"})
                continue
            try:
                resource = resource_extractor.extract_bytes(name, bytes(raw))
            except ValueError as exc:
                if "no extractable text" in str(exc):
                    skipped += 1
                    continue
                logger.info("uploaded resource extraction failed (%s)", type(exc).__name__)
                errors += 1
                details.append({"file": name, "error": "resource could not be imported"})
                continue
            resource_meta = {
                **resource.metadata,
                "media_type": resource.media_type,
                "resource_kind": resource.kind,
                "warnings": resource.warnings,
            }
            result = self._import_one(
                name, resource.text, ws=ws, mt=mt, kind="file_upload",
                extra_provenance=resource_meta, resource_title=resource.title,
            )
            if result.get("skipped"):
                skipped += 1
                continue
            elif result.get("error"):
                errors += 1
                details.append(result)
                continue
            else:
                imported += 1
            file_warnings = list(resource.warnings)
            if derive_facts:
                try:
                    count, note = self._derive_import_facts(
                        resource.text, ws=ws, mt=mt, resource_name=name,
                        resource_kind=resource.kind, resource_meta=resource_meta,
                    )
                    derived_facts += count
                    if note:
                        file_warnings.append(note)
                except (OSError, ValueError) as exc:
                    logger.info("uploaded resource fact derivation failed (%s)",
                                type(exc).__name__)
                    file_warnings.append("fact derivation failed")
            if file_warnings:
                warnings.append({"file": name, "warnings": file_warnings})

        self.store.audit(actor, "import_files", wid, f"{imported} imported")
        self.store.conn.commit()
        return {"workspace": ws, "scanned": len(files), "imported": imported,
                "skipped": skipped, "errors": errors, "derived_facts": derived_facts,
                "details": details[:50], "warnings": warnings[:50]}

    # ── Universal local document import (v2 source manifest) ────────────────
    def _document_registered_target(
        self, source_id: Optional[str], *, workspace: str, repo: Optional[str],
        session_id: Optional[str], scope: Optional[str], memory_type: str,
    ) -> Optional[str]:
        """Validate a selected source collection before creating scope rows."""
        if source_id is None:
            return None
        clean_id = _clean_text(
            source_id, field="source_id", max_chars=MAX_NAME_CHARS,
        )
        if not clean_id.startswith("vlt_"):
            raise ValidationError("registered document source was not found")
        _ws, wid, rid, sid, selected_scope, selected_type = self._obsidian_target(
            workspace=workspace, repo=repo, session_id=session_id,
            scope=scope, memory_type=memory_type, create=False,
        )
        if wid is None or (repo is not None and rid is None):
            raise ValidationError("registered document source was not found")
        source = self.store.get_source_vault(clean_id)
        if (
            source is None
            or source.get("kind") != "documents"
            or source.get("workspace_id") != wid
            or source.get("repo_id") != rid
            or source.get("session_id") != sid
        ):
            raise ValidationError("registered document source does not belong to that target")
        if (
            source.get("scope") != selected_scope.value
            or source.get("memory_type") != selected_type.value
        ):
            raise ValidationError("registered document source has different import defaults")
        return clean_id

    def _document_label(self, value: str, *, source_id: Optional[str] = None) -> str:
        label = unicodedata.normalize("NFC", _clean_text(
            value, field="source_label", max_chars=MAX_NAME_CHARS, required=False,
        ))
        if not label and source_id:
            source = self.store.get_source_vault(source_id)
            label = str(source.get("display_name") or "") if source else ""
        if not label:
            raise ValidationError("source_label is required for a new browser source")
        _reject_secret_capture((("source_label", label),))
        return label

    def _require_new_browser_source_label(
        self, label: str, *, workspace: str, repo: Optional[str],
        session_id: Optional[str], scope: Optional[str], memory_type: str,
        source_kind: str, source_noun: str,
    ) -> None:
        """Keep independently selected browser uploads from sharing a label lineage.

        Browser uploads intentionally have no stable filesystem root.  A new upload
        therefore cannot safely infer that an existing same-label collection is the
        same source.  Require the owner to select that registered ``vlt_`` identity
        explicitly; disk imports retain their root-digest auto-selection path.
        """
        _ws, wid, rid, sid, _selected_scope, _selected_type = self._obsidian_target(
            workspace=workspace, repo=repo, session_id=session_id,
            scope=scope, memory_type=memory_type, create=False,
        )
        if wid is None:
            return
        # Browser source roots are deliberately derived only from the normalized
        # owner label: source bytes change on every edit. Query that exact,
        # indexed identity rather than a capped presentation list; otherwise a
        # 101st registered source could silently reuse a live lineage.
        root_digest = hashlib.sha256(
            f"{source_kind}-browser\0{label.casefold()}".encode("utf-8", "surrogatepass"),
        ).hexdigest()
        source = self.store.get_source_vault_by_root_digest(
            kind=source_kind, root_digest=root_digest, workspace_id=wid,
            repo_id=rid, session_id=sid,
        )
        if source is not None:
            raise ValidationError(
                f"a {source_noun} with this label already exists; select its source_id to resume"
            )

    def _require_new_document_label(
        self, label: str, *, workspace: str, repo: Optional[str],
        session_id: Optional[str], scope: Optional[str], memory_type: str,
    ) -> None:
        self._require_new_browser_source_label(
            label, workspace=workspace, repo=repo, session_id=session_id,
            scope=scope, memory_type=memory_type, source_kind="documents",
            source_noun="source",
        )

    def _require_new_obsidian_label(
        self, label: str, *, workspace: str, repo: Optional[str],
        session_id: Optional[str], scope: Optional[str], memory_type: str,
    ) -> None:
        self._require_new_browser_source_label(
            label, workspace=workspace, repo=repo, session_id=session_id,
            scope=scope, memory_type=memory_type, source_kind="obsidian",
            source_noun="vault",
        )

    @staticmethod
    def _document_report(report: dict) -> dict:
        """Expose the selected adapter explicitly on every generic response."""
        report.setdefault("adapter", "documents")
        report.setdefault("source_adapter", "documents")
        return report

    @staticmethod
    def _document_upload_inputs(
        files: list[tuple[str, bytes]], attachment_manifest: Optional[list[dict]],
    ) -> tuple[list[tuple[str, bytes]], list[dict]]:
        """Validate mixed document bytes and content-free attachment metadata."""
        if not isinstance(files, list) or not files or len(files) > MAX_IMPORT_FILES:
            raise ValidationError(f"files must contain 1 to {MAX_IMPORT_FILES} uploads")
        from engraphis.core.documents import normalize_document_path

        uploads: list[tuple[str, bytes]] = []
        upload_paths: set[str] = set()
        total_bytes = 0
        for entry in files:
            if not isinstance(entry, tuple) or len(entry) != 2:
                raise ValidationError("each upload must contain a path and bytes")
            relative_path, raw = entry
            if not isinstance(relative_path, str) or not isinstance(raw, bytes):
                raise ValidationError("each upload must contain a path and bytes")
            try:
                path = normalize_document_path(relative_path)
            except ValueError:
                raise ValidationError("upload contains an invalid path") from None
            path_key = path.casefold()
            if path_key in upload_paths:
                raise ValidationError("upload contains a duplicate path")
            upload_paths.add(path_key)
            if len(raw) > MAX_IMPORT_RESOURCE_BYTES:
                raise ValidationError("upload contains a file that is too large")
            total_bytes += len(raw)
            if total_bytes > MAX_IMPORT_TOTAL_BYTES:
                raise ValidationError("uploads exceed the total size limit")
            uploads.append((path, raw))

        manifest = [] if attachment_manifest is None else attachment_manifest
        if not isinstance(manifest, list) or len(manifest) > MAX_IMPORT_FILES * 20:
            raise ValidationError("attachment_manifest is invalid")
        attachments: list[dict] = []
        attachment_paths: set[str] = set()
        for entry in manifest:
            if not isinstance(entry, dict):
                raise ValidationError("attachment_manifest is invalid")
            raw_path = entry.get("path")
            if not isinstance(raw_path, str):
                raise ValidationError("attachment_manifest contains an invalid path")
            try:
                path = normalize_document_path(raw_path)
            except ValueError:
                raise ValidationError("attachment_manifest contains an invalid path") from None
            path_key = path.casefold()
            if path_key in attachment_paths:
                raise ValidationError("attachment_manifest contains a duplicate path")
            attachment_paths.add(path_key)
            size = entry.get("size")
            if (
                isinstance(size, bool) or not isinstance(size, int)
                or not 0 <= size <= MAX_IMPORT_RESOURCE_BYTES
            ):
                raise ValidationError("attachment_manifest contains an invalid size")
            attachments.append({"path": path, "size": size})
        if upload_paths.intersection(attachment_paths):
            raise ValidationError("upload and attachment paths overlap")
        return uploads, attachments

    def preview_document_tree(
        self, path: str, *, workspace: str, repo: Optional[str] = None,
        session_id: Optional[str] = None, scope: Optional[str] = None,
        memory_type: str = "semantic", source_id: Optional[str] = None,
        source_label: str = "", on_conflict: str = "error",
    ) -> dict:
        """Scan and plan a mixed local document collection without Store writes."""
        from engraphis.core.documents import scan_document_tree
        from engraphis.document_import import DocumentImporter, local_document_adapter

        ws, wid, rid, sid, sc, mt = self._obsidian_target(
            workspace=workspace, repo=repo, session_id=session_id,
            scope=scope, memory_type=memory_type, create=False,
        )
        policy = self._obsidian_conflict_policy(on_conflict)
        source_id = self._document_registered_target(
            source_id, workspace=workspace, repo=repo, session_id=session_id,
            scope=scope, memory_type=memory_type,
        )
        scan = scan_document_tree(path, adapter=local_document_adapter)
        label = self._document_label(source_label or Path(path).name)
        report = DocumentImporter(self).preview(
            scan, workspace_id=wid, repo_id=rid, session_id=sid,
            scope=sc, memory_type=mt, source_id=source_id,
            source_label=label, on_conflict=policy,
            manifest={"vaults": [], "items": []} if wid is None else None,
        )
        report["target"].update({"workspace": ws, "repo": repo})
        return self._document_report(report)

    def import_document_tree(
        self, path: str, *, workspace: str, repo: Optional[str] = None,
        session_id: Optional[str] = None, scope: Optional[str] = None,
        memory_type: str = "semantic", source_id: Optional[str] = None,
        source_label: str = "", on_conflict: str = "error",
        confirmed: bool = False, actor: str = "local_cli_operator",
        cancel_check=None, progress=None, _scan=None,
    ) -> dict:
        """Import mixed local documents synchronously and atomically per source file."""
        from engraphis.core.documents import scan_document_tree
        from engraphis.document_import import DocumentImporter, local_document_adapter

        if confirmed is not True:
            raise ValidationError("trusted-local confirmation is required")
        policy = self._obsidian_conflict_policy(on_conflict)
        source_id = self._document_registered_target(
            source_id, workspace=workspace, repo=repo, session_id=session_id,
            scope=scope, memory_type=memory_type,
        )
        # The local CLI passes its already-previewed immutable scan so confirmation
        # applies to exactly those bytes. Other callers receive the same secure scan
        # here, immediately before target creation.
        scan = _scan or scan_document_tree(path, adapter=local_document_adapter)
        label = self._document_label(source_label or Path(path).name)
        clean_actor = _clean_text(actor, field="actor", max_chars=MAX_NAME_CHARS)
        _reject_secret_capture((("actor", clean_actor),))
        ws, wid, rid, sid, sc, mt = self._obsidian_target(
            workspace=workspace, repo=repo, session_id=session_id,
            scope=scope, memory_type=memory_type, create=True,
        )
        if wid is None:
            raise RuntimeError("workspace creation failed")
        report = DocumentImporter(self).import_scan(
            scan, workspace_id=wid, repo_id=rid, session_id=sid,
            scope=sc, memory_type=mt, source_id=source_id,
            source_label=label, on_conflict=policy, confirmed=True,
            actor=clean_actor, strict_root=True,
            cancel_check=cancel_check, progress=progress,
        )
        report["target"].update({"workspace": ws, "repo": repo})
        return self._document_report(report)

    def preview_document_upload(
        self, *, files: list[tuple[str, bytes]],
        attachment_manifest: Optional[list[dict]], workspace: str,
        repo: Optional[str] = None, session_id: Optional[str] = None,
        scope: Optional[str] = None, memory_type: str = "semantic",
        source_id: Optional[str] = None, source_label: str = "",
        on_conflict: str = "error", confirmed: bool = False,
    ) -> dict:
        """Preview browser-selected mixed document bytes without persisting a copy."""
        del confirmed
        from engraphis.document_import import DocumentImporter, scan_document_upload

        policy = self._obsidian_conflict_policy(on_conflict)
        source_id = self._document_registered_target(
            source_id, workspace=workspace, repo=repo, session_id=session_id,
            scope=scope, memory_type=memory_type,
        )
        ws, wid, rid, sid, sc, mt = self._obsidian_target(
            workspace=workspace, repo=repo, session_id=session_id,
            scope=scope, memory_type=memory_type, create=False,
        )
        uploads, attachments = self._document_upload_inputs(files, attachment_manifest)
        label = self._document_label(source_label, source_id=source_id)
        if source_id is None:
            self._require_new_document_label(
                label, workspace=workspace, repo=repo, session_id=session_id,
                scope=scope, memory_type=memory_type,
            )
        scan = scan_document_upload(uploads, source_label=label)
        report = DocumentImporter(self).preview(
            scan, workspace_id=wid, repo_id=rid, session_id=sid,
            scope=sc, memory_type=mt, source_id=source_id,
            source_label=label, on_conflict=policy, strict_root=False,
            attachment_manifest=attachments,
            manifest={"vaults": [], "items": []} if wid is None else None,
        )
        report["target"].update({"workspace": ws, "repo": repo})
        return self._document_report(report)

    def import_document_upload(
        self, *, files: list[tuple[str, bytes]],
        attachment_manifest: Optional[list[dict]], workspace: str,
        repo: Optional[str] = None, session_id: Optional[str] = None,
        scope: Optional[str] = None, memory_type: str = "semantic",
        source_id: Optional[str] = None, source_label: str = "",
        on_conflict: str = "error", confirmed: bool = False,
    ) -> dict:
        """Start an owner-confirmed mixed-document import without upload persistence."""
        from engraphis.document_import import DocumentImporter, scan_document_upload

        if confirmed is not True:
            raise ValidationError("trusted-local confirmation is required")
        policy = self._obsidian_conflict_policy(on_conflict)
        source_id = self._document_registered_target(
            source_id, workspace=workspace, repo=repo, session_id=session_id,
            scope=scope, memory_type=memory_type,
        )
        uploads, attachments = self._document_upload_inputs(files, attachment_manifest)
        label = self._document_label(source_label, source_id=source_id)
        if source_id is None:
            self._require_new_document_label(
                label, workspace=workspace, repo=repo, session_id=session_id,
                scope=scope, memory_type=memory_type,
            )
        scan = scan_document_upload(uploads, source_label=label)
        ws, wid, rid, sid, sc, mt = self._obsidian_target(
            workspace=workspace, repo=repo, session_id=session_id,
            scope=scope, memory_type=memory_type, create=True,
        )
        if wid is None:
            raise RuntimeError("workspace creation failed")
        importer = DocumentImporter(self)
        prepared = importer.prepare_import(
            scan, workspace_id=wid, repo_id=rid, session_id=sid,
            scope=sc, memory_type=mt, source_id=source_id,
            source_label=label, on_conflict=policy, confirmed=True,
            strict_root=False,
        )
        job_id = str(prepared["job_id"])

        def run() -> None:
            try:
                importer.import_scan(
                    scan, workspace_id=wid, repo_id=rid, session_id=sid,
                    scope=sc, memory_type=mt, source_id=source_id,
                    source_label=label, on_conflict=policy, confirmed=True,
                    actor="dashboard_browser_session", strict_root=False,
                    attachment_manifest=attachments, prepared=prepared,
                )
            except BaseException:
                logger.exception("Document import worker failed before final reporting")
                try:
                    self.store.conn.execute(
                        "UPDATE jobs SET state='failed', finished_at=?, heartbeat_at=? WHERE id=?",
                        (time.time(), time.time(), job_id),
                    )
                    self.store.conn.commit()
                except Exception:
                    logger.exception("Document import worker finalization failed")
            finally:
                with self._graph_job_lock:
                    self._obsidian_job_threads.pop(job_id, None)

        worker = threading.Thread(
            target=run, name=f"engraphis-document-import-{job_id[-8:]}", daemon=True,
        )
        with self._graph_job_lock:
            self._obsidian_job_threads[job_id] = worker
        try:
            worker.start()
        except BaseException:
            with self._graph_job_lock:
                self._obsidian_job_threads.pop(job_id, None)
            failed_at = time.time()
            self.store.conn.execute(
                "UPDATE jobs SET state='failed', finished_at=?, heartbeat_at=? "
                "WHERE id=?",
                (failed_at, failed_at, job_id),
            )
            self.store.conn.commit()
            raise
        return {
            "job_id": job_id, "id": job_id, "state": "running", "status": "running",
            "source_id": prepared.get("source_id", prepared["vault_id"]),
            "adapter": "documents", "source_adapter": "documents",
            "workspace": ws, "repo": repo,
            "total_items": len(scan.documents) + len(scan.rejected) + len(scan.skipped),
        }

    def list_document_sources(self, workspace: str) -> list[dict]:
        """List universal and legacy Markdown source identities for one workspace."""
        ws = self._clean_ws(workspace)
        wid = self._lookup_workspace(ws)
        if wid is None:
            return []
        result = []
        for row in self.store.list_source_vaults(workspace_id=wid):
            if row.get("kind") not in {"documents", "obsidian"}:
                continue
            repo_name = None
            if row.get("repo_id"):
                repo_row = self.store.conn.execute(
                    "SELECT name FROM repos WHERE id=?", (row["repo_id"],),
                ).fetchone()
                repo_name = str(repo_row["name"]) if repo_row else None
            result.append({
                "id": row["id"],
                "label": row.get("display_name") or "Local documents",
                "kind": row.get("kind"),
                "adapter": "obsidian" if row.get("kind") == "obsidian" else "documents",
                "formats": row.get("formats") or {},
                "workspace": ws, "repo": repo_name,
                "session_id": row.get("session_id"), "scope": row.get("scope"),
                "memory_type": row.get("memory_type"),
                "importer_version": row.get("importer_version"),
            })
        return result

    def get_document_import_job(self, job_id: str, *, workspace: str) -> dict:
        """Return a content-free universal or compatibility import report."""
        ws = self._clean_ws(workspace)
        wid = self._lookup_workspace(ws)
        clean_id = _clean_text(job_id, field="job_id", max_chars=MAX_NAME_CHARS)
        if wid is None:
            raise KeyError(clean_id)
        row = self.store.conn.execute(
            "SELECT * FROM jobs WHERE id=? AND workspace_id=? "
            "AND kind IN ('document_import','obsidian_import')",
            (clean_id, wid),
        ).fetchone()
        if row is None:
            raise KeyError(clean_id)
        items = self.store.list_source_import_job_items(job_id=clean_id)
        files = [{
            "relative_path": item["relative_path"],
            "status": item["result_state"], "action": item["planned_action"],
            "reason": item.get("error_code") or "",
            "warning_count": int(item.get("warning_count") or 0),
            "format": item.get("source_format") or "",
        } for item in items]
        counts = _loads(row["counts"], {})
        return {
            "id": clean_id, "job_id": clean_id, "workspace": ws,
            "kind": row["kind"], "state": row["state"], "status": row["state"],
            "total_items": int(row["total_items"]),
            "processed_items": int(row["processed_items"]),
            "counts": counts, "files": files,
            "report": {"state": row["state"], "counts": counts, "files": files},
        }

    def cancel_document_import_job(self, job_id: str, *, workspace: str) -> dict:
        """Request cancellation at the next per-document atomic boundary."""
        ws = self._clean_ws(workspace)
        wid = self._lookup_workspace(ws)
        clean_id = _clean_text(job_id, field="job_id", max_chars=MAX_NAME_CHARS)
        if wid is None:
            raise KeyError(clean_id)
        changed = self.store.conn.execute(
            "UPDATE jobs SET cancel_requested=1 WHERE id=? AND workspace_id=? "
            "AND kind IN ('document_import','obsidian_import') "
            "AND state IN ('queued','running')",
            (clean_id, wid),
        ).rowcount
        self.store.conn.commit()
        if not changed:
            row = self.store.conn.execute(
                "SELECT state FROM jobs WHERE id=? AND workspace_id=? "
                "AND kind IN ('document_import','obsidian_import')", (clean_id, wid),
            ).fetchone()
            if row is None:
                raise KeyError(clean_id)
            return {"id": clean_id, "state": row["state"], "cancel_requested": False}
        return {"id": clean_id, "state": "running", "cancel_requested": True}

    # ── Obsidian compatibility import ────────────────────────────────────────
    def _obsidian_target(self, *, workspace: str, repo: Optional[str],
                         session_id: Optional[str], scope: Optional[str],
                         memory_type: str, create: bool) -> tuple[
                             str, Optional[str], Optional[str], Optional[str], Scope, MemoryType
                         ]:
        """Resolve an import target through the ordinary v2 hierarchy rules."""
        ws = self._clean_ws(workspace)
        rp = _clean_name(repo, field="repo") if repo else None
        mt = _enum(memory_type, MemoryType, "memory_type")
        sc = _write_scope(scope, repo=rp, session_id=session_id)
        if sc == Scope.USER:
            raise ValidationError("user scope is read-only")
        if sc == Scope.WORKSPACE and (rp or session_id):
            raise ValidationError(
                "workspace scope requires repo and session_id to be omitted"
            )
        wid = self._get_or_create_workspace(ws) if create else self._lookup_workspace(ws)
        if wid is None:
            if session_id:
                raise ValidationError("session scope requires an existing workspace")
            return ws, None, None, None, sc, mt
        rid = (
            self.store.get_or_create_repo(wid, rp) if create and rp
            else self._lookup_repo(wid, rp) if rp else None
        )
        if rp and rid is None:
            return ws, wid, None, None, sc, mt
        if session_id:
            session = self._session_for_write(session_id, wid, rid)
            if session is None:
                raise ValidationError("session_id is required")
            session_id = str(session["id"])
            if rid is None:
                rid = session.get("repo_id")
            if sc == Scope.REPO and rid is None:
                raise ValidationError("repo scope requires a repo-backed session_id")
            if sc == Scope.REPO:
                # The session is used only to infer and validate its parent repo;
                # repo-scoped source manifests must not retain a session target.
                session_id = None
        if sc == Scope.REPO and rid is None:
            raise ValidationError("repo scope requires repo")
        return ws, wid, rid, session_id, sc, mt

    def _obsidian_registered_target(
        self, vault_id: Optional[str], *, workspace: str, repo: Optional[str],
        session_id: Optional[str], scope: Optional[str], memory_type: str,
    ) -> Optional[str]:
        """Validate a selected vault before an import may create hierarchy rows.

        A new import is allowed to create its workspace/repository. Re-importing a
        registered vault is different: its target already exists, and a misspelled or
        cross-workspace request must fail without first creating attacker-controlled
        hierarchy state. The importer performs the authoritative vault-row comparison;
        this preflight makes that comparison mutation-free.
        """
        if vault_id is None:
            return None
        clean_id = _clean_text(
            vault_id, field="vault_id", max_chars=MAX_NAME_CHARS,
        )
        if not clean_id.startswith("vlt_"):
            raise ValidationError("registered vault was not found")
        _ws, wid, rid, sid, selected_scope, selected_type = self._obsidian_target(
            workspace=workspace, repo=repo, session_id=session_id,
            scope=scope, memory_type=memory_type, create=False,
        )
        if wid is None or (repo is not None and rid is None):
            raise ValidationError("registered vault was not found")
        vault = self.store.get_source_vault(clean_id)
        if (
            vault is None
            or vault.get("kind") != "obsidian"
            or vault.get("workspace_id") != wid
            or vault.get("repo_id") != rid
            or vault.get("session_id") != sid
        ):
            raise ValidationError("registered vault does not belong to that target")
        if (
            vault.get("scope") != selected_scope.value
            or vault.get("memory_type") != selected_type.value
        ):
            raise ValidationError("registered vault has different import defaults")
        return clean_id

    def _obsidian_label(self, value: str, *, vault_id: Optional[str] = None) -> str:
        label = unicodedata.normalize("NFC", _clean_text(
            value, field="vault_label", max_chars=MAX_NAME_CHARS, required=False,
        ))
        if not label and vault_id:
            vault = self.store.get_source_vault(vault_id)
            label = str(vault.get("display_name") or "") if vault else ""
        if not label:
            raise ValidationError("vault_label is required for a new browser source")
        _reject_secret_capture((("vault_label", label),))
        return label

    @staticmethod
    def _obsidian_conflict_policy(value: str) -> str:
        policy = _clean_text(
            value, field="on_conflict", max_chars=MAX_NAME_CHARS,
        ).casefold()
        policy = {"report": "error", "supersede": "replace"}.get(policy, policy)
        if policy not in {"error", "replace", "new"}:
            raise ValidationError("on_conflict must be error, replace, or new")
        return policy

    @staticmethod
    def _obsidian_upload_inputs(
        files: list[tuple[str, bytes]], attachment_manifest: Optional[list[dict]],
    ) -> tuple[list[tuple[str, bytes]], list[dict]]:
        """Apply transport-independent upload bounds and manifest validation."""
        if not isinstance(files, list) or not files or len(files) > MAX_IMPORT_FILES:
            raise ValidationError(f"files must contain 1 to {MAX_IMPORT_FILES} uploads")
        from engraphis.core.obsidian import (
            MAX_NOTE_BYTES,
            MAX_VAULT_BYTES,
            normalize_obsidian_path,
        )

        uploads: list[tuple[str, bytes]] = []
        upload_paths: set[str] = set()
        total_bytes = 0
        for entry in files:
            if not isinstance(entry, tuple) or len(entry) != 2:
                raise ValidationError("each upload must contain a path and bytes")
            relative_path, raw = entry
            if not isinstance(relative_path, str) or not isinstance(raw, bytes):
                raise ValidationError("each upload must contain a path and bytes")
            if len(raw) > MAX_NOTE_BYTES:
                raise ValidationError("upload contains a file that is too large")
            total_bytes += len(raw)
            if total_bytes > MAX_VAULT_BYTES:
                raise ValidationError("uploads exceed the total size limit")
            # Retain an unsafe path for the scanner to put in the content-free
            # per-file preview report; only valid paths participate in the
            # transport-level duplicate/overlap check.
            try:
                path = normalize_obsidian_path(relative_path)
            except ValueError:
                uploads.append((relative_path, raw))
                continue
            path_key = path.casefold()
            if path_key in upload_paths:
                raise ValidationError("upload contains a duplicate path")
            upload_paths.add(path_key)
            uploads.append((path, raw))

        manifest = [] if attachment_manifest is None else attachment_manifest
        if not isinstance(manifest, list) or len(manifest) > MAX_IMPORT_FILES * 20:
            raise ValidationError("attachment_manifest is invalid")

        attachments: list[dict] = []
        attachment_paths: set[str] = set()
        for entry in manifest:
            if not isinstance(entry, dict):
                raise ValidationError("attachment_manifest is invalid")
            raw_path = entry.get("path")
            if not isinstance(raw_path, str):
                raise ValidationError("attachment_manifest contains an invalid path")
            try:
                path = normalize_obsidian_path(raw_path)
            except ValueError:
                raise ValidationError("attachment_manifest contains an invalid path") from None
            path_key = path.casefold()
            if path_key in attachment_paths:
                raise ValidationError("attachment_manifest contains a duplicate path")
            attachment_paths.add(path_key)
            size = entry.get("size")
            if (
                isinstance(size, bool) or not isinstance(size, int)
                or not 0 <= size <= MAX_IMPORT_RESOURCE_BYTES
            ):
                raise ValidationError("attachment_manifest contains an invalid size")
            attachments.append({"path": path, "size": size})
        if upload_paths.intersection(attachment_paths):
            raise ValidationError("upload and attachment paths overlap")
        return uploads, attachments

    def preview_obsidian_vault(self, path: str, *, workspace: str,
                               repo: Optional[str] = None,
                               session_id: Optional[str] = None,
                               scope: Optional[str] = None,
                               memory_type: str = "semantic",
                               vault_id: Optional[str] = None,
                               vault_label: str = "",
                               on_conflict: str = "error") -> dict:
        """Read and plan one local vault without mutating the Store."""
        from engraphis.core.obsidian import scan_obsidian_vault
        from engraphis.obsidian_import import ObsidianImporter

        ws, wid, rid, sid, sc, mt = self._obsidian_target(
            workspace=workspace, repo=repo, session_id=session_id,
            scope=scope, memory_type=memory_type, create=False,
        )
        policy = self._obsidian_conflict_policy(on_conflict)
        scan = scan_obsidian_vault(path)
        label = self._obsidian_label(vault_label or Path(path).name)
        vault_id = self._obsidian_registered_target(
            vault_id, workspace=workspace, repo=repo, session_id=session_id,
            scope=scope, memory_type=memory_type,
        )
        report = ObsidianImporter(self).preview(
            scan, workspace_id=wid, repo_id=rid, session_id=sid,
            scope=sc, memory_type=mt, vault_id=vault_id,
            vault_label=label, on_conflict=policy,
            manifest={"vaults": [], "items": []} if wid is None else None,
        )
        report["target"].update({"workspace": ws, "repo": repo})
        return report

    def import_obsidian_vault(self, path: str, *, workspace: str,
                              repo: Optional[str] = None,
                              session_id: Optional[str] = None,
                              scope: Optional[str] = None,
                              memory_type: str = "semantic",
                              vault_id: Optional[str] = None,
                              vault_label: str = "",
                              on_conflict: str = "error",
                              confirmed: bool = False,
                              actor: str = "local_cli_operator",
                              cancel_check=None, progress=None,
                              _scan=None) -> dict:
        """Import one filesystem vault synchronously and atomically per note."""
        from engraphis.core.obsidian import scan_obsidian_vault
        from engraphis.obsidian_import import ObsidianImporter

        if confirmed is not True:
            raise ValidationError("trusted-local confirmation is required")
        policy = self._obsidian_conflict_policy(on_conflict)
        vault_id = self._obsidian_registered_target(
            vault_id, workspace=workspace, repo=repo, session_id=session_id,
            scope=scope, memory_type=memory_type,
        )
        # The local CLI passes its already-previewed immutable scan so operator
        # confirmation applies to exactly those bytes. Other callers are scanned
        # here immediately before target creation.
        scan = _scan or scan_obsidian_vault(path)
        label = self._obsidian_label(vault_label or Path(path).name)
        clean_actor = _clean_text(actor, field="actor", max_chars=MAX_NAME_CHARS)
        _reject_secret_capture((("actor", clean_actor),))
        ws, wid, rid, sid, sc, mt = self._obsidian_target(
            workspace=workspace, repo=repo, session_id=session_id,
            scope=scope, memory_type=memory_type, create=True,
        )
        if wid is None:
            raise RuntimeError("workspace creation failed")
        report = ObsidianImporter(self).import_scan(
            scan, workspace_id=wid, repo_id=rid, session_id=sid,
            scope=sc, memory_type=mt, vault_id=vault_id,
            vault_label=label, on_conflict=policy,
            confirmed=True, actor=clean_actor, strict_root=True,
            cancel_check=cancel_check, progress=progress,
        )
        report["target"].update({"workspace": ws, "repo": repo})
        return report

    def preview_obsidian_upload(self, *, files: list[tuple[str, bytes]],
                                attachment_manifest: Optional[list[dict]],
                                workspace: str, repo: Optional[str] = None,
                                session_id: Optional[str] = None,
                                scope: Optional[str] = None,
                                memory_type: str = "semantic",
                                vault_id: Optional[str] = None,
                                vault_label: str = "",
                                on_conflict: str = "error",
                                confirmed: bool = False) -> dict:
        """Preview browser-selected bytes; ``confirmed`` is intentionally ignored."""
        del confirmed
        from engraphis.obsidian_import import ObsidianImporter, scan_obsidian_upload

        policy = self._obsidian_conflict_policy(on_conflict)
        vault_id = self._obsidian_registered_target(
            vault_id, workspace=workspace, repo=repo, session_id=session_id,
            scope=scope, memory_type=memory_type,
        )
        ws, wid, rid, sid, sc, mt = self._obsidian_target(
            workspace=workspace, repo=repo, session_id=session_id,
            scope=scope, memory_type=memory_type, create=False,
        )
        uploads, attachments = self._obsidian_upload_inputs(files, attachment_manifest)
        label = self._obsidian_label(vault_label, vault_id=vault_id)
        if vault_id is None:
            self._require_new_obsidian_label(
                label, workspace=workspace, repo=repo, session_id=session_id,
                scope=scope, memory_type=memory_type,
            )
        scan = scan_obsidian_upload(uploads, vault_label=label)
        report = ObsidianImporter(self).preview(
            scan, workspace_id=wid, repo_id=rid, session_id=sid,
            scope=sc, memory_type=mt, vault_id=vault_id,
            vault_label=label, on_conflict=policy,
            strict_root=False, attachment_manifest=attachments,
            manifest={"vaults": [], "items": []} if wid is None else None,
        )
        report["target"].update({"workspace": ws, "repo": repo})
        return report

    def import_obsidian_upload(self, *, files: list[tuple[str, bytes]],
                               attachment_manifest: Optional[list[dict]],
                               workspace: str, repo: Optional[str] = None,
                               session_id: Optional[str] = None,
                               scope: Optional[str] = None,
                               memory_type: str = "semantic",
                               vault_id: Optional[str] = None,
                               vault_label: str = "",
                               on_conflict: str = "error",
                               confirmed: bool = False) -> dict:
        """Import owner-confirmed browser bytes without creating an upload copy."""
        from engraphis.obsidian_import import ObsidianImporter, scan_obsidian_upload

        if confirmed is not True:
            raise ValidationError("trusted-local confirmation is required")
        policy = self._obsidian_conflict_policy(on_conflict)
        vault_id = self._obsidian_registered_target(
            vault_id, workspace=workspace, repo=repo, session_id=session_id,
            scope=scope, memory_type=memory_type,
        )
        uploads, attachments = self._obsidian_upload_inputs(files, attachment_manifest)
        label = self._obsidian_label(vault_label, vault_id=vault_id)
        if vault_id is None:
            self._require_new_obsidian_label(
                label, workspace=workspace, repo=repo, session_id=session_id,
                scope=scope, memory_type=memory_type,
            )
        scan = scan_obsidian_upload(uploads, vault_label=label)
        ws, wid, rid, sid, sc, mt = self._obsidian_target(
            workspace=workspace, repo=repo, session_id=session_id,
            scope=scope, memory_type=memory_type, create=True,
        )
        if wid is None:
            raise RuntimeError("workspace creation failed")
        importer = ObsidianImporter(self)
        prepared = importer.prepare_import(
            scan, workspace_id=wid, repo_id=rid, session_id=sid,
            scope=sc, memory_type=mt, vault_id=vault_id,
            vault_label=label, on_conflict=policy,
            confirmed=True, strict_root=False,
        )
        job_id = str(prepared["job_id"])

        def run() -> None:
            try:
                importer.import_scan(
                    scan, workspace_id=wid, repo_id=rid, session_id=sid,
                    scope=sc, memory_type=mt, vault_id=vault_id,
                    vault_label=label, on_conflict=policy,
                    confirmed=True, actor="dashboard_browser_session",
                    strict_root=False, attachment_manifest=attachments,
                    prepared=prepared,
                )
            except BaseException:
                logger.exception("Obsidian import worker failed before final reporting")
                try:
                    self.store.conn.execute(
                        "UPDATE jobs SET state='failed', finished_at=?, heartbeat_at=? WHERE id=?",
                        (time.time(), time.time(), job_id),
                    )
                    self.store.conn.commit()
                except Exception:
                    logger.exception("Obsidian import worker finalization failed")
            finally:
                with self._graph_job_lock:
                    self._obsidian_job_threads.pop(job_id, None)

        worker = threading.Thread(
            target=run, name=f"engraphis-obsidian-import-{job_id[-8:]}", daemon=True,
        )
        with self._graph_job_lock:
            self._obsidian_job_threads[job_id] = worker
        try:
            worker.start()
        except BaseException:
            with self._graph_job_lock:
                self._obsidian_job_threads.pop(job_id, None)
            failed_at = time.time()
            self.store.conn.execute(
                "UPDATE jobs SET state='failed', finished_at=?, heartbeat_at=? "
                "WHERE id=?",
                (failed_at, failed_at, job_id),
            )
            self.store.conn.commit()
            raise
        return {
            "job_id": job_id, "id": job_id, "state": "running", "status": "running",
            "vault_id": prepared["vault_id"], "workspace": ws, "repo": repo,
            "total_items": len(scan.notes) + len(scan.rejected) + len(scan.skipped),
        }

    def list_obsidian_vaults(self, workspace: str) -> list[dict]:
        """Return registered identities/defaults without exposing local root digests."""
        ws = self._clean_ws(workspace)
        wid = self._lookup_workspace(ws)
        if wid is None:
            return []
        result = []
        for row in self.store.list_source_vaults(workspace_id=wid, kind="obsidian"):
            repo_name = None
            if row.get("repo_id"):
                repo_row = self.store.conn.execute(
                    "SELECT name FROM repos WHERE id=?", (row["repo_id"],),
                ).fetchone()
                repo_name = str(repo_row["name"]) if repo_row else None
            result.append({
                "id": row["id"], "label": row.get("display_name") or "Obsidian vault",
                "workspace": ws, "repo": repo_name, "session_id": row.get("session_id"),
                "scope": row.get("scope"), "memory_type": row.get("memory_type"),
                "importer_version": row.get("importer_version"),
            })
        return result

    def get_obsidian_import_job(self, job_id: str, *, workspace: str) -> dict:
        ws = self._clean_ws(workspace)
        wid = self._lookup_workspace(ws)
        clean_id = _clean_text(job_id, field="job_id", max_chars=MAX_NAME_CHARS)
        if wid is None:
            raise KeyError(clean_id)
        row = self.store.conn.execute(
            "SELECT * FROM jobs WHERE id=? AND workspace_id=? AND kind='obsidian_import'",
            (clean_id, wid),
        ).fetchone()
        if row is None:
            raise KeyError(clean_id)
        items = self.store.list_source_import_job_items(job_id=clean_id)
        files = [{
            "relative_path": item["relative_path"],
            "status": item["result_state"],
            "action": item["planned_action"],
            "reason": item.get("error_code") or "",
            "warning_count": int(item.get("warning_count") or 0),
            "format": item.get("source_format") or "",
        } for item in items]
        return {
            "id": clean_id, "job_id": clean_id, "workspace": ws,
            "state": row["state"], "status": row["state"],
            "total_items": int(row["total_items"]),
            "processed_items": int(row["processed_items"]),
            "counts": _loads(row["counts"], {}), "files": files,
            "report": {"state": row["state"], "counts": _loads(row["counts"], {}),
                       "files": files},
        }

    def cancel_obsidian_import_job(self, job_id: str, *, workspace: str) -> dict:
        ws = self._clean_ws(workspace)
        wid = self._lookup_workspace(ws)
        clean_id = _clean_text(job_id, field="job_id", max_chars=MAX_NAME_CHARS)
        if wid is None:
            raise KeyError(clean_id)
        changed = self.store.conn.execute(
            "UPDATE jobs SET cancel_requested=1 WHERE id=? AND workspace_id=? "
            "AND kind='obsidian_import' AND state IN ('queued','running')",
            (clean_id, wid),
        ).rowcount
        self.store.conn.commit()
        if not changed:
            row = self.store.conn.execute(
                "SELECT state FROM jobs WHERE id=? AND workspace_id=? "
                "AND kind='obsidian_import'", (clean_id, wid),
            ).fetchone()
            if row is None:
                raise KeyError(clean_id)
            return {"id": clean_id, "state": row["state"], "cancel_requested": False}
        return {"id": clean_id, "state": "running", "cancel_requested": True}

    def import_postgres_schema(self, dsn: str, *, workspace: str,
                               repo: Optional[str] = None,
                               schemas: Optional[list] = None,
                               actor: str = "user") -> dict:
        """Introspect PostgreSQL before opening the atomic local persistence transaction.

        The DSN is never persisted, logged, or returned. Only a one-way source digest
        produced by the backend is stored as provenance.
        """
        dsn = _clean_text(dsn, field="dsn", max_chars=4_000)
        ws = self._clean_ws(workspace)
        rp = _clean_name(repo, field="repo") if repo else None
        selected = _clean_string_list(
            schemas, field="schemas", max_items=100, max_chars=200
        ) if schemas else None
        actor = _clean_text(
            actor, field="actor", max_chars=MAX_NAME_CHARS, required=False
        ) or "user"
        selection_digest = hashlib.sha256(
            json.dumps(
                selected or [], ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()[:16]
        from engraphis.backends.postgres_schema import get_postgres_introspector
        snapshot = get_postgres_introspector().inspect(dsn, schemas=selected)
        pieces = (
            [(fact.content, fact.title) for fact in ChunkingExtractor().extract(snapshot.text)]
            if len(snapshot.text) > MAX_CONTENT_CHARS
            else [(snapshot.text, snapshot.title)]
        )
        return self._apply_postgres_schema_snapshot(
            snapshot, pieces, workspace=ws, repo=rp, actor=actor,
            selection_digest=selection_digest,
        )

    @_rollback_service_transaction
    def _apply_postgres_schema_snapshot(
        self, snapshot: Any, pieces: list, *, workspace: str,
        repo: Optional[str], actor: str, selection_digest: str,
    ) -> dict:
        """Persist one inspected catalog atomically after all remote I/O has completed.

        Stable per-source/schema/chunk claim keys let an identical successful retry
        reuse its live memory instead of duplicating it. Changed chunks stay on the normal
        guarded resolution path, preserving approval and bi-temporal safety policy.
        """
        source_identity = str(
            snapshot.metadata.get("source_digest")
            or snapshot.metadata.get("database")
            or "unknown"
        )
        source_digest = hashlib.sha256(
            source_identity.encode("utf-8")
        ).hexdigest()[:24]
        existing_wid = self._lookup_workspace(workspace)
        existing_rid = (
            self._lookup_repo(existing_wid, repo)
            if existing_wid is not None and repo
            else None
        )
        target_scope = Scope.REPO if repo else Scope.WORKSPACE
        stored_rows = []
        for index, (piece_content, piece_title) in enumerate(pieces):
            title = piece_title or snapshot.title
            subject_key = (
                f"postgres_schema:{source_digest}:{selection_digest}:{index}"
            )
            expected_chunk = {"index": index, "of": len(pieces)}
            if existing_wid is not None and (not repo or existing_rid is not None):
                prior = self.store.list_live_claims(
                    workspace_id=existing_wid,
                    repo_id=existing_rid,
                    session_id=None,
                    scope=target_scope,
                    mtype=MemoryType.SEMANTIC,
                    subject_key=subject_key,
                    claim_kind="catalog_snapshot_chunk",
                )
                exact = next((
                    record for record in prior
                    if record.content == piece_content
                    and record.title == title
                    and record.metadata.get("postgres_schema") == snapshot.metadata
                    and record.metadata.get("chunk") == expected_chunk
                ), None)
                if exact is not None:
                    stored_rows.append({
                        "id": exact.id,
                        "op": "noop",
                        "stored": False,
                    })
                    continue
            stored_rows.append(self.remember(
                piece_content, workspace=workspace, repo=repo,
                mtype="semantic", scope=target_scope.value,
                title=title,
                source="postgres_introspector", trusted=False,
                kind="postgres_schema",
                metadata={
                    "postgres_schema": snapshot.metadata,
                    "chunk": expected_chunk,
                },
                subject_key=subject_key,
                claim_kind="catalog_snapshot_chunk",
                resolve_conflicts=True,
            ))
        if not stored_rows:
            return {"workspace": workspace, "stored": 0, "entities": 0, "relations": 0}
        stored = stored_rows[0]
        wid, rid = self._require_scope(workspace, repo)
        actual_ids: dict[str, str] = {}
        for entity in snapshot.entities:
            source_id = str(entity.get("id") or "")
            name = str(entity.get("name") or source_id)
            kind = str(entity.get("kind") or "database_object")
            if not source_id or not name:
                continue
            actual_ids[source_id] = self.store.upsert_entity(Node(
                id="", name=name[:MAX_NAME_CHARS], ntype=kind[:MAX_NAME_CHARS],
                workspace_id=wid, repo_id=rid,
            ))
        relations_written = 0
        existing = {
            (edge.src, edge.dst, edge.relation)
            for edge in self.store.edges_in_scope(SearchFilter(
                workspace_id=wid, repo_id=rid
            ))
        }
        for relation in snapshot.relations:
            src = actual_ids.get(str(relation.get("source") or ""))
            dst = actual_ids.get(str(relation.get("target") or ""))
            rel = str(relation.get("relation") or "related")[:MAX_NAME_CHARS]
            if not src or not dst or src == dst or (src, dst, rel) in existing:
                continue
            self.store.upsert_edge(Edge(
                id="", src=src, dst=dst, relation=rel, layer=GraphLayer.ENTITY,
                workspace_id=wid, repo_id=rid,
                provenance={"source": "postgres_introspector",
                            "memory_id": stored["id"],
                            "memory_ids": [row["id"] for row in stored_rows]},
            ))
            existing.add((src, dst, rel))
            relations_written += 1
        self.store.audit(
            actor, "import_postgres_schema", stored["id"],
            f"{len(actual_ids)} entities, {relations_written} relations",
        )
        receipt = self.store.record_receipt(
            "remember", workspace_id=wid, repo_id=rid or "",
            actor=actor, target_count=len(stored_rows), status="postgres_schema",
            metadata={
                "entities": len(actual_ids),
                "relations": relations_written,
                "tables": snapshot.metadata.get("tables", 0),
            },
        )
        return {
            "workspace": workspace, "repo": repo, "id": stored["id"],
            "memory_ids": [row["id"] for row in stored_rows],
            "entities": len(actual_ids), "relations": relations_written,
            "schema": snapshot.metadata, "receipt": receipt,
        }

    def consolidate(self, *, workspace: str, repo: Optional[str] = None,
                    dry_run: bool = False, min_cluster: int = 3,
                    archive_below: float = 0.05, profiles: bool = False,
                    min_mentions: int = 3, infer: bool = False,
                    structured: bool = False) -> dict:
        """Sleep-time consolidation sweep (episodic→semantic distillation + decayed-
        transient archival). The report includes a ``compaction`` block with the tokens
        the sweep saved. With ``profiles=True`` a third pass rolls each entity's memories
        into one durable profile digest (report under ``profiles``). With ``infer=True`` a
        fourth pass proposes evidence-only links between memories in different subject
        clusters that share a bridging entity (report under ``inferences``); inferred
        memories are low-salience and untrusted. ``infer`` is off by default — a human
        opts in — and the pass follows this call's ``dry_run`` flag (a dry-run proposes,
        a real run applies). ``dry_run=True`` reports without changing anything.

        Manual deterministic consolidation remains local. Dream inference and scheduled
        maintenance execute only in Engraphis Cloud managed compute.

        ``structured=True`` asks a configured LLM to emit schema-validated consolidated
        facts with graph hints; any provider/schema failure falls back to the deterministic
        digest path. Model-derived facts remain review-pending and never supersede their
        authoritative source episodes automatically."""
        if infer:
            raise ValidationError("dream inference is available through Engraphis Cloud")
        wid, rid = self._require_scope(workspace, repo)
        try:
            min_cluster = max(2, min(20, int(min_cluster)))
            archive_below = float(archive_below)
            min_mentions = max(2, min(50, int(min_mentions)))
        except (TypeError, ValueError, OverflowError):
            raise ValidationError("min_cluster/min_mentions must be integers and "
                                  "archive_below a number")
        if not math.isfinite(archive_below):
            raise ValidationError("archive_below must be finite")
        archive_below = max(0.0, min(0.5, archive_below))
        llm = None
        if structured:
            try:
                from engraphis.llm.client import LLMClient
                llm = LLMClient()
            except Exception:
                llm = None
        try:
            return self.engine.consolidate(
                workspace_id=wid, repo_id=rid, dry_run=bool(dry_run),
                min_cluster=min_cluster, archive_below=archive_below,
                profiles=bool(profiles), min_mentions=min_mentions,
                infer=False, structured=bool(structured),
                llm=llm)
        finally:
            if llm is not None and hasattr(llm, "close"):
                try:
                    llm.close()
                except Exception:
                    pass

    # ── read ───────────────────────────────────────────────────────────────────
    def recall(self, query: str, *, workspace: Optional[str] = None,
               repo: Optional[str] = None, session_id: Optional[str] = None,
               mtypes: Optional[list] = None,
               k: int = 8, as_of: Optional[float] = None,
               valid_at: Optional[float] = None,
               known_at: Optional[float] = None,
               reinforce: bool = False, intent: str = "recall",
               graph_layers: Optional[list] = None,
               token_budget: Optional[int] = None,
               retrieval_profile: str = "balanced",
               candidate_depth: str = "fixed",
               response_mode: str = "full",
               diagnostics: bool = False,
               include_untrusted: bool = False,
               planning: str = "off",
               mtype_limits: Optional[dict] = None,
               record_receipt: bool = True) -> dict:
        """Retrieve the most relevant memories for ``query`` within scope."""
        query = _clean_text(query, field="query", max_chars=MAX_CONTENT_CHARS)
        try:
            k = int(k)
        except (TypeError, ValueError, OverflowError):
            raise ValidationError("k must be an integer")
        k = max(1, min(MAX_K, k))
        mts = [_enum(m, MemoryType, "mtype") for m in mtypes] if mtypes else None
        layers = (
            [_enum(layer, GraphLayer, "graph_layer") for layer in graph_layers]
            if graph_layers else None
        )
        as_of = _optional_timestamp(as_of, field="as_of")
        valid_at = _optional_timestamp(valid_at, field="valid_at")
        known_at = _optional_timestamp(known_at, field="known_at")
        if as_of is not None and valid_at is not None and as_of != valid_at:
            raise ValidationError("as_of and valid_at must match when both are supplied")
        valid_at = valid_at if valid_at is not None else as_of
        try:
            token_budget = (
                self.engine.recall_engine.token_budget
                if token_budget is None else int(token_budget)
            )
        except (TypeError, ValueError, OverflowError):
            raise ValidationError("token_budget must be an integer")
        token_budget = max(0, min(MAX_TOKEN_BUDGET, token_budget))
        retrieval_profile = str(retrieval_profile or "balanced").strip().casefold()
        if retrieval_profile not in RETRIEVAL_PROFILES:
            choices = ", ".join(sorted(RETRIEVAL_PROFILES))
            raise ValidationError(f"retrieval_profile must be one of: {choices}")
        candidate_depth = str(candidate_depth or "fixed").strip().casefold()
        if candidate_depth not in CANDIDATE_DEPTH_MODES:
            choices = ", ".join(sorted(CANDIDATE_DEPTH_MODES))
            raise ValidationError(f"candidate_depth must be one of: {choices}")
        response_mode = str(response_mode or "full").strip().casefold()
        if response_mode not in RESPONSE_MODES:
            raise ValidationError("response_mode must be one of: compact, full")
        include_untrusted = _strict_bool(include_untrusted, field="include_untrusted")
        planning, mtype_limits = _planning_controls(planning, mtype_limits)

        # A configured workspace binding or a bound dashboard user must never do a
        # workspace-less (global) recall — either case represents a tenant boundary.
        if not workspace and (
                self.allowed_workspaces is not None
                or _authenticated_principal() is not None):
            raise ValidationError("workspace is required on this instance")
        wid = rid = None
        sid = None
        if workspace:
            ws = self._clean_ws(workspace)
            wid = self._lookup_workspace(ws)
            if wid is None:
                return _with_retrieval_capabilities(_empty_recall(
                    query, token_budget=token_budget, response_mode=response_mode,
                    retrieval_profile=retrieval_profile, candidate_depth=candidate_depth,
                    planning=planning, mtype_limits=mtype_limits,
                    valid_at=valid_at,
                    known_at=known_at, note=f"no workspace named '{ws}' yet",
                ), self.engine.embedder, self.store)
            if repo:
                rp = _clean_name(repo, field="repo")
                rid = self._lookup_repo(wid, rp)
                if rid is None:
                    return _with_retrieval_capabilities(_empty_recall(
                        query, token_budget=token_budget, response_mode=response_mode,
                        retrieval_profile=retrieval_profile, candidate_depth=candidate_depth,
                        planning=planning, mtype_limits=mtype_limits,
                        valid_at=valid_at,
                        known_at=known_at,
                        note=f"no repo named '{rp}' in workspace '{ws}' yet",
                    ), self.engine.embedder, self.store)
            if session_id:
                sid = _clean_text(
                    session_id, field="session_id", max_chars=MAX_NAME_CHARS
                )
                session = self.store.get_session(sid)
                if session is None:
                    return _with_retrieval_capabilities(_empty_recall(
                        query, token_budget=token_budget, response_mode=response_mode,
                        retrieval_profile=retrieval_profile, candidate_depth=candidate_depth,
                        planning=planning, mtype_limits=mtype_limits,
                        valid_at=valid_at,
                        known_at=known_at, note=f"no session with id '{sid}'",
                    ), self.engine.embedder, self.store)
                if session["workspace_id"] != wid or (
                        rid is not None and session.get("repo_id") != rid):
                    raise ValidationError("session_id does not belong to that workspace/repo")
                self._authorize_session(session)
                rid = rid or session.get("repo_id")
        elif session_id:
            raise ValidationError("session_id requires workspace")

        recall_filter = _filter(
            wid, rid, mts, as_of, layers, session_id=sid,
            valid_at=valid_at, known_at=known_at,
        )
        result = self.engine.recall_engine.recall(
            query,
            recall_filter,
            k=k, reinforce=reinforce,
            token_budget=token_budget,
            retrieval_profile=retrieval_profile,
            candidate_depth=candidate_depth,
            diagnostics=bool(diagnostics),
            include_untrusted=include_untrusted,
            planning=planning,
            mtype_limits=mtype_limits,
        )
        memories = []
        for chunk in result.chunks:
            if response_mode == "compact":
                item = {
                    key: chunk.get(key)
                    for key in (
                        "id", "title", "scope", "mtype", "repo_id", "score",
                        "relative_score", "absolute_support", "arm"
                    )
                }
                item["provenance"] = _compact_provenance(chunk.get("provenance"))
            else:
                item = dict(chunk)
                arm = item.get("arm") or "hybrid"
                item["why_recalled"] = (
                    f"Matched by {arm} retrieval; query-relative fused rank "
                    f"{float(item.get('relative_score') or 0.0):.3f}, absolute support "
                    f"{float(item.get('absolute_support') or 0.0):.3f}, retention "
                    f"{float(item.get('retention') or 0.0):.3f}."
                )
            memories.append(item)
        usage = asdict(result.usage) if result.usage is not None else {
            "budget_tokens": token_budget,
            "context_tokens": 0,
            "source_tokens": 0,
            "saved_tokens": 0,
            "savings_ratio": 0.0,
            "packed_count": 0,
            "omitted_count": 0,
            "token_counter": "unknown",
        }
        usage = _annotate_context_usage(
            usage,
            operation="recall",
            intent=str(intent or "recall"),
        )
        packed_sources = [{
            "id": packed.id,
            "tokens": packed.tokens,
            "truncated": packed.truncated,
            "reason": packed.reason,
        } for packed in result.packed_chunks]
        capabilities = {
            "degraded_mode": result.degraded_mode,
            "semantic_support": result.semantic_support,
            "embedding_mode": result.embedding_mode,
            "degraded_reason": result.degraded_reason,
            "vector_search_ready": result.vector_search_ready,
        }
        out = {
            "query": query, "count": result.count,
            "context": result.context, "memories": memories,
            "packed_sources": packed_sources,
            "usage": usage,
            "valid_at": result.valid_at,
            "known_at": result.known_at,
            "historical": result.historical,
            "retrieval_profile": result.retrieval_profile,
            "candidate_depth": result.candidate_depth_mode,
            "candidate_k_requested": result.candidate_k_requested,
            "candidate_k_used": result.candidate_k_used,
            "candidate_depth_reason": result.candidate_depth_reason,
            "context_revision": result.context_revision,
            "planning": result.planning_mode,
            "mtype_limits": dict(mtype_limits),
            "response_mode": response_mode,
            "include_untrusted": include_untrusted,
            "score_semantics": _recall_score_semantics(capabilities),
            **capabilities,
        }
        if result.count == 0:
            eligibility = self.store.prompt_eligibility_counts(recall_filter)
            if (
                not include_untrusted
                and eligibility["total"] > 0
                and eligibility["prompt_eligible"] == 0
            ):
                out["note"] = (
                    "memories exist in this scope, but none are approved for prompt "
                    "recall; use 'engraphis-cli review list' and the governed bulk "
                    "approval workflow"
                )
                out["eligibility"] = eligibility
            elif eligibility["total"] > 0 and not result.vector_search_ready:
                out["note"] = result.degraded_reason
        if diagnostics:
            out["retrieval_trace"] = result.retrieval_trace or []
            out["planning_details"] = result.planning_details or {}
            out["graph_traversal_details"] = result.graph_traversal_details or []
        if record_receipt:
            out["receipt"] = self.store.record_receipt(
                "recall", workspace_id=wid or "", repo_id=rid or "", actor="agent",
                target_count=result.count, status="ok",
                metadata={"intent": str(intent or "recall")[:80], "k": k,
                          "result_count": result.count,
                          "graph_layers": [layer.value for layer in layers] if layers else [],
                          "retrieval_profile": result.retrieval_profile,
                          "candidate_depth": result.candidate_depth_mode,
                          "candidate_k_requested": result.candidate_k_requested,
                          "candidate_k_used": result.candidate_k_used,
                          "planning": result.planning_mode,
                          "context_revision": result.context_revision,
                          "mtype_limits": mtype_limits,
                          "response_mode": response_mode,
                          "historical": result.historical,
                          "token_usage": usage},
            )
        return out

    def adaptive_context(
        self,
        query: str,
        history: str,
        *,
        workspace: str,
        repo: Optional[str] = None,
        session_id: Optional[str] = None,
        mtypes: Optional[list] = None,
        as_of: Optional[float] = None,
        valid_at: Optional[float] = None,
        known_at: Optional[float] = None,
        k: int = 8,
        max_context_tokens: int = 4096,
        retrieval_token_budget: Optional[int] = None,
        confidence_floor: float = 0.25,
        retrieval_profile: str = "balanced",
        candidate_depth: str = "adaptive",
        diagnostics: bool = False,
        planning: str = "off",
        mtype_limits: Optional[dict] = None,
    ) -> dict:
        """Return prompt context without retrieving when supplied history fits.

        This host-facing API receives the exact history the caller already owns.
        It returns that history directly when it fits, compact recall when evidence
        is strong, or a bounded recent-history fallback when support is weak.
        Source bodies are not duplicated in routing telemetry.
        """
        clean_query = _clean_text(
            query,
            field="query",
            max_chars=MAX_CONTENT_CHARS,
        )
        clean_history = _clean_text(
            history,
            field="history",
            max_chars=MAX_CONTENT_CHARS,
            required=False,
        )
        if isinstance(k, bool):
            raise ValidationError("k must be an integer")
        try:
            k = int(k)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValidationError("k must be an integer") from exc
        k = max(1, min(MAX_K, k))
        if isinstance(max_context_tokens, bool):
            raise ValidationError("max_context_tokens must be an integer")
        try:
            max_context_tokens = int(max_context_tokens)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValidationError("max_context_tokens must be an integer") from exc
        if not 0 <= max_context_tokens <= MAX_TOKEN_BUDGET:
            raise ValidationError(
                f"max_context_tokens must be between 0 and {MAX_TOKEN_BUDGET}"
            )
        if retrieval_token_budget is not None:
            if isinstance(retrieval_token_budget, bool):
                raise ValidationError("retrieval_token_budget must be an integer")
            try:
                retrieval_token_budget = int(retrieval_token_budget)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValidationError("retrieval_token_budget must be an integer") from exc
            if not 0 <= retrieval_token_budget <= max_context_tokens:
                raise ValidationError(
                    "retrieval_token_budget must be between 0 and max_context_tokens"
                )
        mts = [_enum(m, MemoryType, "mtype") for m in mtypes] if mtypes else None
        as_of = _optional_timestamp(as_of, field="as_of")
        valid_at = _optional_timestamp(valid_at, field="valid_at")
        known_at = _optional_timestamp(known_at, field="known_at")
        if as_of is not None and valid_at is not None and as_of != valid_at:
            raise ValidationError("as_of and valid_at must match when both are supplied")
        valid_at = valid_at if valid_at is not None else as_of
        planning, mtype_limits = _planning_controls(planning, mtype_limits)
        wid, rid = self._require_scope(workspace, repo)
        sid = None
        if session_id:
            sid = _clean_text(session_id, field="session_id", max_chars=MAX_NAME_CHARS)
            session = self.store.get_session(sid)
            if session is None:
                raise ValidationError(f"no session with id '{sid}'")
            if session["workspace_id"] != wid or (
                rid is not None and session.get("repo_id") != rid
            ):
                raise ValidationError("session_id does not belong to that workspace/repo")
            self._authorize_session(session)
            rid = rid or session.get("repo_id")
        result = self.engine.adaptive_context(
            clean_query,
            clean_history,
            workspace_id=wid,
            repo_id=rid,
            session_id=sid,
            mtypes=mts,
            as_of=as_of,
            valid_at=valid_at,
            known_at=known_at,
            k=k,
            max_context_tokens=max_context_tokens,
            retrieval_token_budget=retrieval_token_budget,
            confidence_floor=confidence_floor,
            retrieval_profile=retrieval_profile,
            candidate_depth=candidate_depth,
            diagnostics=diagnostics,
            planning=planning,
            mtype_limits=mtype_limits,
            reinforce=False,
        )
        sources = []
        if result.mode == "retrieval" and result.recall is not None:
            chunks_by_id = {
                chunk.get("id"): chunk
                for chunk in result.recall.chunks
            }
            sources = [
                {
                    "id": chunk.get("id"),
                    "title": chunk.get("title"),
                    "scope": chunk.get("scope"),
                    "mtype": chunk.get("mtype"),
                    "provenance": _compact_provenance(chunk.get("provenance")),
                }
                for packed in result.recall.packed_chunks
                if (chunk := chunks_by_id.get(packed.id)) is not None
            ]
        recall_usage = result.recall.usage if result.recall is not None else None
        source_tokens = result.history_tokens
        context_tokens = result.context_tokens
        usage = {
            "budget_tokens": result.max_context_tokens,
            "context_tokens": context_tokens,
            "source_tokens": source_tokens,
            "saved_tokens": max(0, source_tokens - context_tokens),
            "savings_ratio": (
                max(0, source_tokens - context_tokens) / source_tokens
                if source_tokens else 0.0
            ),
            "packed_count": len(result.recall.packed_chunks) if result.recall else 0,
            "omitted_count": int(getattr(recall_usage, "omitted_count", 0) or 0),
            "token_counter": result.token_counter,
        }
        usage = _annotate_context_usage(
            usage,
            operation="adaptive_context",
            adaptive_mode=result.mode,
            baseline_tokens=result.history_tokens,
            emitted_tokens=result.context_tokens,
        )
        out = {
            "query": clean_query,
            "context": result.context,
            "context_revision": result.context_revision,
            "planning": planning,
            "mtype_limits": dict(mtype_limits),
            "decision": result.to_dict(),
            "sources": sources,
        }
        if diagnostics and result.recall is not None:
            out["retrieval_trace"] = result.recall.retrieval_trace or []
            out["planning_details"] = result.recall.planning_details or {}
            out["graph_traversal_details"] = result.recall.graph_traversal_details or []
        out["receipt"] = self.store.record_receipt(
            "adaptive_context", workspace_id=wid, repo_id=rid or "", actor="agent",
            target_count=len(sources), status="ok",
            metadata={
                "adaptive_mode": result.mode,
                "k": k,
                "result_count": len(sources),
                "retrieval_profile": retrieval_profile,
                "candidate_depth": candidate_depth,
                "planning": planning,
                "context_revision": result.context_revision,
                "mtype_limits": mtype_limits,
                "historical": any(
                    anchor is not None for anchor in (as_of, valid_at, known_at)
                ),
                "token_usage": usage,
            },
        )
        return out

    def grounded_recall(self, query: str, *, workspace: Optional[str] = None,
                        repo: Optional[str] = None, session_id: Optional[str] = None,
                        mtypes: Optional[list] = None,
                        k: int = 8, as_of: Optional[float] = None,
                        valid_at: Optional[float] = None,
                        known_at: Optional[float] = None,
                        min_support: Optional[float] = None,
                        max_citations: int = 5, llm=None,
                        token_budget: Optional[int] = None,
                        retrieval_profile: str = "balanced",
                        candidate_depth: str = "fixed",
                        response_mode: str = "full",
                        diagnostics: bool = False,
                        planning: str = "off",
                        mtype_limits: Optional[dict] = None) -> dict:
        """Grounded recall: an answer built strictly from retrieved memories, with
        ``[n]`` citations and an explicit abstain when evidence is insufficient
        (``core.grounded``). This path is offline/deterministic (extractive answer) — no
        LLM is invoked from the service, so it stays safe and reproducible for every
        front end. The abstain is a real threshold on absolute query↔memory support, not
        a ranking artefact — an off-topic query returns ``grounded: false`` instead of a
        confident-looking irrelevant memory."""
        query = _clean_text(query, field="query", max_chars=MAX_CONTENT_CHARS)
        try:
            k = int(k)
        except (TypeError, ValueError, OverflowError):
            raise ValidationError("k must be an integer")
        k = max(1, min(MAX_K, k))
        try:
            max_citations = int(max_citations)
        except (TypeError, ValueError, OverflowError):
            raise ValidationError("max_citations must be an integer")
        max_citations = max(1, min(MAX_K, max_citations))
        as_of = _optional_timestamp(as_of, field="as_of")
        valid_at = _optional_timestamp(valid_at, field="valid_at")
        known_at = _optional_timestamp(known_at, field="known_at")
        if as_of is not None and valid_at is not None and as_of != valid_at:
            raise ValidationError("as_of and valid_at must match when both are supplied")
        valid_at = valid_at if valid_at is not None else as_of
        try:
            token_budget = (
                self.engine.recall_engine.token_budget
                if token_budget is None else int(token_budget)
            )
        except (TypeError, ValueError, OverflowError):
            raise ValidationError("token_budget must be an integer")
        token_budget = max(0, min(MAX_TOKEN_BUDGET, token_budget))
        retrieval_profile = str(retrieval_profile or "balanced").strip().casefold()
        if retrieval_profile not in RETRIEVAL_PROFILES:
            choices = ", ".join(sorted(RETRIEVAL_PROFILES))
            raise ValidationError(f"retrieval_profile must be one of: {choices}")
        candidate_depth = str(candidate_depth or "fixed").strip().casefold()
        if candidate_depth not in CANDIDATE_DEPTH_MODES:
            choices = ", ".join(sorted(CANDIDATE_DEPTH_MODES))
            raise ValidationError(f"candidate_depth must be one of: {choices}")
        response_mode = str(response_mode or "full").strip().casefold()
        if response_mode not in RESPONSE_MODES:
            raise ValidationError("response_mode must be one of: compact, full")
        planning, mtype_limits = _planning_controls(planning, mtype_limits)
        if min_support is not None:
            try:
                min_support = float(min_support)
            except (TypeError, ValueError, OverflowError):
                raise ValidationError("min_support must be a number")
            if not math.isfinite(min_support):
                raise ValidationError("min_support must be finite")
            min_support = max(0.0, min(1.0, min_support))
        mts = [_enum(m, MemoryType, "mtype") for m in mtypes] if mtypes else None

        if not workspace and (
                self.allowed_workspaces is not None
                or _authenticated_principal() is not None):
            raise ValidationError("workspace is required on this instance")
        wid = rid = None
        sid = None
        if workspace:
            ws = self._clean_ws(workspace)
            wid = self._lookup_workspace(ws)
            if wid is None:
                return _with_retrieval_capabilities(_empty_grounded(
                    query, reason=f"no workspace named '{ws}' yet",
                    token_budget=token_budget, response_mode=response_mode,
                    retrieval_profile=retrieval_profile, candidate_depth=candidate_depth,
                    planning=planning, mtype_limits=mtype_limits,
                    valid_at=valid_at,
                    known_at=known_at,
                ), self.engine.embedder, self.store)
            if repo:
                rp = _clean_name(repo, field="repo")
                rid = self._lookup_repo(wid, rp)
                if rid is None:
                    return _with_retrieval_capabilities(_empty_grounded(
                        query,
                        reason=f"no repo named '{rp}' in workspace '{ws}' yet",
                        token_budget=token_budget, response_mode=response_mode,
                        retrieval_profile=retrieval_profile, candidate_depth=candidate_depth,
                        planning=planning, mtype_limits=mtype_limits,
                        valid_at=valid_at,
                        known_at=known_at,
                    ), self.engine.embedder, self.store)
            if session_id:
                sid = _clean_text(
                    session_id, field="session_id", max_chars=MAX_NAME_CHARS
                )
                session = self.store.get_session(sid)
                if session is None:
                    return _with_retrieval_capabilities(_empty_grounded(
                        query, reason=f"no session with id '{sid}'",
                        token_budget=token_budget, response_mode=response_mode,
                        retrieval_profile=retrieval_profile, candidate_depth=candidate_depth,
                        planning=planning, mtype_limits=mtype_limits,
                        valid_at=valid_at,
                        known_at=known_at,
                    ), self.engine.embedder, self.store)
                if session["workspace_id"] != wid or (
                        rid is not None and session.get("repo_id") != rid):
                    raise ValidationError("session_id does not belong to that workspace/repo")
                self._authorize_session(session)
                rid = rid or session.get("repo_id")
        elif session_id:
            raise ValidationError("session_id requires workspace")

        ans = self.engine.grounded_recall(
            query, workspace_id=wid, repo_id=rid, session_id=sid, mtypes=mts,
            as_of=as_of, valid_at=valid_at, known_at=known_at,
            k=k, llm=llm, min_support=min_support,
            max_citations=max_citations, token_budget=token_budget,
            retrieval_profile=retrieval_profile, candidate_depth=candidate_depth,
            diagnostics=bool(diagnostics),
            planning=planning,
            mtype_limits=mtype_limits,
        )
        out = {"query": query, **ans.to_dict()}
        out["response_mode"] = response_mode
        out["mtype_limits"] = dict(mtype_limits)
        out["usage"] = _annotate_context_usage(
            out.get("usage") or {},
            operation="grounded_recall",
        )
        if response_mode == "compact":
            compact_citations = []
            for citation in out.get("citations") or []:
                item = dict(citation)
                item.pop("content", None)
                item["provenance"] = _compact_provenance(item.get("provenance"))
                compact_citations.append(item)
            out["citations"] = compact_citations
        out["receipt"] = self.store.record_receipt(
            "grounded_recall", workspace_id=wid or "", repo_id=rid or "", actor="agent",
            target_count=len(out.get("citations") or []),
            status="grounded" if out.get("grounded") else "abstained",
            metadata={"intent": "grounded", "grounded": bool(out.get("grounded")),
                      "citations": len(out.get("citations") or []),
                      "retrieval_profile": out.get("retrieval_profile"),
                      "candidate_depth": out.get("candidate_depth"),
                      "candidate_k_requested": out.get("candidate_k_requested"),
                      "candidate_k_used": out.get("candidate_k_used"),
                      "planning": out.get("planning"),
                      "context_revision": out.get("context_revision"),
                      "mtype_limits": mtype_limits,
                      "response_mode": response_mode,
                      "historical": bool(out.get("historical")),
                      "token_usage": out.get("usage") or {}},
        )
        return out

    # ── session lifecycle ───────────────────────────────────────────────────────
    def start_session(self, workspace: str, *, repo: Optional[str] = None,
                      agent: str = "", goal: str = "", force_new: bool = False) -> dict:
        """Open a session. If this repo has a prior *ended* session, its summary and
        unresolved ``open_threads`` come back as ``bootstrap`` — the concrete fix for
        "the agent forgets everything between sessions".

        Idempotent by default for the exact ``(workspace, repo, user, agent, goal)`` task
        identity. A different user, agent, or goal opens a distinct session automatically;
        ``force_new=True`` deliberately branches even when every identity field matches.
        The lookup/create decision is one storage transaction, so concurrent retries cannot
        both insert a session."""
        ws = self._clean_ws(workspace)
        rp = _clean_name(repo, field="repo") if repo else None
        agent = _clean_text(agent, field="agent", max_chars=MAX_NAME_CHARS, required=False)
        goal = _clean_text(goal, field="goal", max_chars=MAX_TITLE_CHARS, required=False)
        wid = self._get_or_create_workspace(ws)
        rid = self.store.get_or_create_repo(wid, rp) if rp else None
        principal = _authenticated_principal()
        user_id = principal["id"] if principal is not None else ""
        sid, reused = self.store.get_or_start_session(
            wid, rid, agent=agent, user_id=user_id, goal=goal,
            force_new=bool(force_new),
        )
        if reused:
            return {"session_id": sid, "workspace": ws, "repo": rp,
                    "goal": goal, "status": "active", "reused": True,
                    "bootstrap": {}}
        bootstrap: dict = {}
        if rid:
            last = self.store.get_last_session(
                wid, rid, exclude=sid, user_id=user_id, agent=agent,
            )
            if last:
                bootstrap = {
                    "summary": last.get("summary") or "",
                    "open_threads": last.get("open_threads") or [],
                    "outcome": last.get("outcome") or "",
                }
        return {"session_id": sid, "workspace": ws, "repo": rp, "goal": goal,
               "status": "active", "reused": False, "bootstrap": bootstrap}

    def end_session(self, session_id: str, *, summary: str = "", outcome: str = "",
                    open_threads: Optional[list] = None) -> dict:
        sid = _clean_text(session_id, field="session_id", max_chars=MAX_NAME_CHARS)
        summary = _clean_text(summary, field="summary", max_chars=MAX_CONTENT_CHARS, required=False)
        outcome = _clean_text(outcome, field="outcome", max_chars=MAX_TITLE_CHARS, required=False)
        threads = _clean_string_list(open_threads, field="open_threads", max_items=MAX_KEYWORDS,
                                     max_chars=MAX_TITLE_CHARS)
        session = self.store.get_session(sid)
        if session is None:
            raise ValidationError(f"no session with id '{sid}'")
        self._authorize_session(session)
        result = self.store.end_session(
            sid, summary=summary, outcome=outcome, open_threads=threads,
        )
        if result == "missing":
            raise ValidationError(f"no session with id '{sid}'")
        if result == "conflict":
            raise ValidationError("session is already closed with a different handoff")
        return {"session_id": sid, "status": "summarized", "summary": summary,
               "open_threads": threads}

    # ── governance: retire / secure erase / pin / correct / promote ───────────
    def retire(self, memory_id: str, *, workspace: str, repo: Optional[str] = None,
               reason: str = "", actor: str = "user") -> dict:
        """Bi-temporally retire one memory. This preserves history and indexes."""
        mid = _clean_text(memory_id, field="memory_id", max_chars=MAX_NAME_CHARS)
        reason = _clean_text(reason, field="reason", max_chars=MAX_TITLE_CHARS, required=False)
        _reject_secret_capture((("reason", reason),))
        actor = _clean_text(actor, field="actor", max_chars=MAX_NAME_CHARS,
                            required=False) or "user"
        wid, rid = self._require_scope(workspace, repo)
        self._check_owns(mid, wid, rid)
        try:
            return self.engine.retire(mid, reason=reason, actor=actor)
        except (KeyError, ValueError) as exc:
            raise ValidationError(str(exc))

    def forget(self, memory_id: str, *, workspace: str, repo: Optional[str] = None,
               reason: str = "", actor: str = "user") -> dict:
        """Deprecated compatibility alias for :meth:`retire`."""
        result = self.retire(memory_id, workspace=workspace, repo=repo,
                             reason=reason, actor=actor)
        return {**result, "status": "forgotten", "deprecated": True}

    def secure_erase(self, memory_id: str, *, workspace: str, repo: Optional[str] = None,
                     actor: str = "user") -> dict:
        """Irreversibly remove one leaked record; unlike retire, history is destroyed."""
        mid = _clean_text(memory_id, field="memory_id", max_chars=MAX_NAME_CHARS)
        actor = _clean_text(actor, field="actor", max_chars=MAX_NAME_CHARS,
                            required=False) or "user"
        wid, rid = self._require_scope(workspace, repo)
        self._check_owns(mid, wid, rid)
        try:
            return self.engine.secure_erase(mid, actor=actor)
        except (KeyError, ValueError) as exc:
            raise ValidationError(str(exc))

    def pin(self, memory_id: str, *, workspace: str, repo: Optional[str] = None,
           pinned: bool = True, actor: str = "user") -> dict:
        mid = _clean_text(memory_id, field="memory_id", max_chars=MAX_NAME_CHARS)
        actor = _clean_text(actor, field="actor", max_chars=MAX_NAME_CHARS,
                            required=False) or "user"
        wid, rid = self._require_scope(workspace, repo)
        self._check_owns(mid, wid, rid)
        try:
            return self.engine.pin(mid, pinned=bool(pinned), actor=actor)
        except KeyError as exc:
            raise ValidationError(str(exc))

    def correct(self, memory_id: str, new_content: str, *, workspace: str,
               repo: Optional[str] = None, reason: str = "", actor: str = "user") -> dict:
        mid = _clean_text(memory_id, field="memory_id", max_chars=MAX_NAME_CHARS)
        new_content = _clean_text(new_content, field="new_content", max_chars=MAX_CONTENT_CHARS)
        reason = _clean_text(reason, field="reason", max_chars=MAX_TITLE_CHARS, required=False)
        actor = _clean_text(actor, field="actor", max_chars=MAX_NAME_CHARS,
                            required=False) or "user"
        wid, rid = self._require_scope(workspace, repo)
        self._check_owns(mid, wid, rid)
        try:
            return self.engine.correct(mid, new_content, reason=reason, actor=actor)
        except KeyError as exc:
            raise ValidationError(str(exc))

    def promote(self, memory_id: str, target_scope: str, *, workspace: str,
                repo: Optional[str] = None, reason: str = "",
                actor: str = "user") -> dict:
        """Widen a memory's visibility while preserving its narrow-scope history."""
        mid = _clean_text(memory_id, field="memory_id", max_chars=MAX_NAME_CHARS)
        target = _enum(target_scope, Scope, "target_scope")
        reason = _clean_text(
            reason, field="reason", max_chars=MAX_TITLE_CHARS, required=False
        )
        actor = _clean_text(
            actor, field="actor", max_chars=MAX_NAME_CHARS, required=False
        ) or "user"
        wid, rid = self._require_scope(workspace, repo)
        self._check_owns(mid, wid, rid)
        try:
            out = self.engine.promote(mid, target, reason=reason, actor=actor)
        except (KeyError, ValueError) as exc:
            raise ValidationError(str(exc))
        out["workspace"] = self._clean_ws(workspace)
        out["repo"] = None
        if target == Scope.REPO:
            promoted = self.store.get_memory(out["id"])
            if promoted and promoted.repo_id:
                row = self.store.conn.execute(
                    "SELECT name FROM repos WHERE id=?", (promoted.repo_id,)
                ).fetchone()
                out["repo"] = row["name"] if row else repo
        out["receipt"] = self.store.record_receipt(
            "promote", workspace_id=wid, repo_id=rid or "", actor=actor,
            target_count=1, status=out["op"],
            metadata={"scope": target.value, "resolution": "promotion"},
        )
        return out

    def merge(self, source_ids: list, merged_content: str, *, workspace: str,
              repo: Optional[str] = None, title: Optional[str] = None,
              mtype: Optional[str] = None, scope: Optional[str] = None,
              reason: str = "", actor: str = "user") -> dict:
        """Merge several memories into one (manual N→1), retiring the sources into
        history. Validated and authorized like every other governance op: the caller
        must name the workspace that owns the sources, and **every** source is
        ownership-checked, so a merge can neither read nor retire a memory outside the
        caller's workspace. Session-scoped sources must share one session unless the
        caller explicitly chooses an authorized wider ``repo`` or ``workspace`` target;
        the workspace itself stays a hard isolation boundary (``_check_owns``)."""
        ids = _clean_string_list(source_ids, field="source_ids", max_items=MAX_K,
                                 max_chars=MAX_NAME_CHARS)
        seen, uniq = set(), []
        for i in ids:
            if i not in seen:
                seen.add(i)
                uniq.append(i)
        if len(uniq) < 2:
            raise ValidationError("merge needs at least two distinct source memories")
        merged_content = _clean_text(merged_content, field="content",
                                     max_chars=MAX_CONTENT_CHARS)
        reason = _clean_text(reason, field="reason", max_chars=MAX_TITLE_CHARS,
                             required=False)
        actor = _clean_text(actor, field="actor", max_chars=MAX_NAME_CHARS,
                            required=False) or "user"
        title_clean = (None if title is None
                       else _clean_text(title, field="title", max_chars=MAX_TITLE_CHARS,
                                        required=False))
        mt = _enum(mtype, MemoryType, "memory_type") if mtype else None
        target_scope = _enum(scope, Scope, "scope") if scope else None
        wid, _ = self._require_scope(workspace, repo)
        for sid in uniq:
            self._check_owns(sid, wid, None)
        try:
            out = self.engine.merge(
                uniq, merged_content, title=title_clean, mtype=mt, scope=target_scope,
                reason=reason, actor=actor,
            )
        except (KeyError, ValueError) as exc:
            raise ValidationError(str(exc))
        out["workspace"] = self._clean_ws(workspace)
        return out

    # ── bi-temporal: why / timeline ──────────────────────────────────────────────
    def why(self, query: str, *, workspace: str, repo: Optional[str] = None, k: int = 5,
            as_of: Optional[float] = None, valid_at: Optional[float] = None,
            known_at: Optional[float] = None) -> dict:
        """Rationale + history for a decision/fact: the live answer plus whatever it
        superseded, if anything — the bi-temporal "why" a flat store can't answer."""
        query = _clean_text(query, field="query", max_chars=MAX_CONTENT_CHARS)
        wid, rid = self._require_scope(workspace, repo)
        k = max(1, min(MAX_K, int(k)))
        _, valid_at, known_at = _temporal_anchors(
            as_of=as_of, valid_at=valid_at, known_at=known_at,
        )
        out = self.engine.why(
            query, workspace_id=wid, repo_id=rid, k=k,
            valid_at=valid_at, known_at=known_at, prompt_only=True,
        )
        return {"query": query, "answer": [_mem_to_dict(r) for r in out["answer"]],
               "supersedes": [_mem_to_dict(r) for r in out["supersedes"]]}

    def timeline(self, query: str, *, workspace: str, repo: Optional[str] = None,
                limit: int = 20, as_of: Optional[float] = None,
                valid_at: Optional[float] = None, known_at: Optional[float] = None) -> dict:
        """Chronological, bi-temporal history of a fact: what we believed and when."""
        query = _clean_text(query, field="query", max_chars=MAX_CONTENT_CHARS)
        wid, rid = self._require_scope(workspace, repo)
        limit = max(1, min(MAX_K, int(limit)))
        _, valid_at, known_at = _temporal_anchors(
            as_of=as_of, valid_at=valid_at, known_at=known_at,
        )
        recs = self.engine.timeline(
            query, workspace_id=wid, repo_id=rid, limit=limit,
            valid_at=valid_at, known_at=known_at, prompt_only=True,
        )
        return {"query": query, "history": [_mem_to_dict(r) for r in recs]}

    def recall_proactive(self, *, workspace: str, repo: Optional[str] = None,
                         k: int = 10) -> dict:
        """"What should I know right now" with no query — importance + recency +
        retention, plus the repo's last-session handoff if there is one."""
        wid, rid = self._require_scope(workspace, repo)
        k = max(1, min(MAX_K, int(k)))
        principal = _authenticated_principal()
        user_id = principal["id"] if principal is not None else None
        out = self.engine.recall_proactive(
            workspace_id=wid, repo_id=rid, k=k, user_id=user_id, prompt_only=True,
        )
        return {"memories": [_mem_to_dict(r) for r in out["memories"]],
               "last_session": out["last_session"]}

    def proactive_context(self, *, workspace: str, repo: Optional[str] = None,
                          task: str = "", agent_state: str = "", k: int = 10,
                          synthesize: bool = False,
                          token_budget: Optional[int] = None,
                          response_mode: str = "full") -> dict:
        """Agent-ready proactive context packet.

        Combines queryless proactive recall, optional task-specific recall, and the
        last-session handoff into a cited context summary. Deterministic by default;
        when ``synthesize`` is true and an LLM is configured, the model may rewrite the
        summary, but only if it cites retrieved memories with ``[n]`` markers.
        """
        if response_mode not in RESPONSE_MODES:
            raise ValidationError("response_mode must be 'full' or 'compact'")
        if token_budget is not None:
            if isinstance(token_budget, bool):
                raise ValidationError("token_budget must be an integer")
            try:
                token_budget = int(token_budget)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValidationError("token_budget must be an integer") from exc
            if not 0 <= token_budget <= MAX_TOKEN_BUDGET:
                raise ValidationError(
                    f"token_budget must be between 0 and {MAX_TOKEN_BUDGET}"
                )
        task = _clean_text(task, field="task", max_chars=MAX_CONTEXT_TASK_CHARS,
                           required=False)
        agent_state = _clean_text(agent_state, field="agent_state",
                                  max_chars=MAX_AGENT_STATE_CHARS, required=False)
        k = max(1, min(MAX_K, int(k)))
        wid, rid = self._require_scope(workspace, repo)
        proactive = self.recall_proactive(workspace=workspace, repo=repo, k=k)
        memories = list(proactive.get("memories") or [])
        query = "\n".join(x for x in (task, agent_state) if x).strip()
        if query:
            try:
                recalled = self.recall(query, workspace=workspace, repo=repo, k=k,
                                        reinforce=False)
                memories.extend(recalled.get("memories") or [])
            except Exception as exc:
                logger.warning(
                    "proactive_context recall failed (%s)",
                    type(exc).__name__,
                )
        # Raw recall is an inspection surface and includes benign explicitly-untrusted
        # records. This method builds agent/model context, so enforce the stricter prompt
        # boundary before deterministic or LLM synthesis. Quarantined records never
        # reached either raw recall path.
        memories = [
            memory for memory in memories
            if prompt_eligible(memory.get("provenance"), memory.get("metadata"))
        ]
        llm = None
        if synthesize:
            try:
                from engraphis.config import settings
                if settings.llm_api_key:
                    from engraphis.llm.client import LLMClient
                    llm = LLMClient()
            except Exception:
                llm = None
        try:
            from engraphis.ai_context import build_proactive_context
            out = build_proactive_context(
                task=task, agent_state=agent_state, memories=memories,
                last_session=proactive.get("last_session") or {}, llm=llm,
                synthesize=bool(synthesize),
            )
        finally:
            if llm is not None:
                try:
                    llm.close()
                except Exception:
                    pass
        workspace_name = self._clean_ws(workspace)
        legacy = {"workspace": workspace_name, "repo": repo, **out}
        if response_mode == "full":
            # The default remains byte-for-byte the established proactive response
            # contract.  Compact mode is deliberately opt-in for new hosts.
            return legacy

        budget = (
            self.engine.recall_engine.token_budget
            if token_budget is None else token_budget
        )
        counter = getattr(self.engine.recall_engine.context_packer, "count_tokens", None)
        if not callable(counter):
            counter = RegexTokenCounter()
        full_context = str(out.get("context_summary") or "")
        context = _fit_context_lines(full_context, budget, counter)
        source_tokens = int(counter(full_context))
        context_tokens = int(counter(context))
        citations = list(out.get("citations") or [])
        cited_numbers = {int(number) for number in re.findall(r"\[(\d+)\]", context)}
        sources = [
            {
                "id": citation.get("id"),
                "n": citation.get("n"),
                "title": citation.get("title"),
                "mtype": citation.get("mtype"),
                "provenance": _compact_provenance(citation.get("provenance")),
            }
            for citation in citations
            if citation.get("n") in cited_numbers
        ]
        grounded = bool(sources)
        usage = {
            "budget_tokens": budget,
            "context_tokens": context_tokens,
            "source_tokens": source_tokens,
            "saved_tokens": max(0, source_tokens - context_tokens),
            "savings_ratio": (
                max(0, source_tokens - context_tokens) / source_tokens
                if source_tokens else 0.0
            ),
            "packed_count": len(sources),
            "token_counter": getattr(
                self.engine.recall_engine.context_packer,
                "token_counter_identity",
                getattr(counter, "identity", type(counter).__name__),
            ),
        }
        usage = _annotate_context_usage(
            usage,
            operation="proactive_context",
        )
        self.store.record_receipt(
            "proactive_context", workspace_id=wid, repo_id=rid or "", actor="agent",
            target_count=len(sources), status="ok",
            metadata={
                "response_mode": "compact",
                "grounded": grounded,
                "synthesized": bool(out.get("synthesized")),
                "token_usage": usage,
            },
        )
        return {
            "workspace": workspace_name,
            "repo": repo,
            "context": context,
            "sources": sources,
            "usage": usage,
            "grounded": grounded,
            "reason": (
                out.get("reason") or "deterministic fallback"
                if grounded else "context budget omitted cited sources"
            ),
        }

    # ── linking & events (A-MEM-style) ───────────────────────────────────────────
    def record_event(self, kind: str, content: str, *, workspace: str,
                     repo: Optional[str] = None, session_id: Optional[str] = None,
                     refs: Optional[list] = None) -> dict:
        kind = _clean_name(kind, field="kind")
        content = _clean_text(content, field="content", max_chars=MAX_CONTENT_CHARS)
        _reject_secret_capture((("event content", content), ("event refs", refs)))
        wid, rid = self._require_scope(workspace, repo)
        session = self._session_for_write(session_id, wid, rid)
        if rid is None and session is not None:
            rid = session.get("repo_id")
        try:
            eid = self.engine.record_event(
                kind, content, workspace_id=wid, repo_id=rid or "",
                session_id=session_id or "", refs=refs,
            )
        except ValueError as exc:
            if session_id and str(exc) in {
                f"no session with id '{session_id}'",
                "session_id does not belong to that workspace/repo",
                "session_id is not active",
            }:
                raise ValidationError(str(exc)) from exc
            raise
        return {"id": eid, "kind": kind}

    def link(self, a: str, b: str, *, workspace: str, repo: Optional[str] = None,
             relation: str = "related", layer: Optional[str] = None,
             reason: str = "") -> dict:
        a = _clean_text(a, field="a", max_chars=MAX_NAME_CHARS)
        b = _clean_text(b, field="b", max_chars=MAX_NAME_CHARS)
        relation = (_clean_text(relation, field="relation", max_chars=MAX_NAME_CHARS,
                                required=False) or "related")
        reason = _clean_text(
            reason, field="reason", max_chars=MAX_TITLE_CHARS, required=False
        )
        _reject_secret_capture((("link reason", reason),))
        graph_layer = normalize_graph_layer(
            _enum(layer, GraphLayer, "layer") if layer else None, relation
        )
        wid, rid = self._require_scope(workspace, repo)
        self._check_owns(a, wid, rid)
        self._check_owns(b, wid, rid)
        try:
            self.engine.link(
                a, b, relation=relation, layer=graph_layer, reason=reason
            )
        except KeyError as exc:
            raise ValidationError(str(exc))
        out = {"a": a, "b": b, "relation": relation,
               "layer": graph_layer.value,
               "reason": reason, "linked": True}
        out["receipt"] = self.store.record_receipt(
            "link", workspace_id=wid, repo_id=rid or "", actor="agent",
            target_count=2, status="ok",
            metadata={"relation": relation, "layer": graph_layer.value},
        )
        return out

    # ── code-symbol graph ────────────────────────────────────────────────────────
    def index_repo(self, *, workspace: str, repo: str, root_path: str,
                   languages: Optional[list] = None) -> dict:
        """Index (or re-index) a repo's code graph. Like ``remember``/``start_session``,
        this creates the workspace/repo if this is the first time you've named them —
        indexing a brand-new repo is the common case, unlike the read-only code tools
        below which require the repo to already exist."""
        if not repo:
            raise ValidationError("repo is required to index code")
        ws = self._clean_ws(workspace)
        rp = _clean_name(repo, field="repo")
        root_path = _clean_text(root_path, field="root_path", max_chars=MAX_CONTENT_CHARS)
        wid = self._get_or_create_workspace(ws)
        rid = self.store.get_or_create_repo(wid, rp)
        langs = None
        if languages:
            from engraphis.backends.codegraph import normalize_language, supported_languages
            requested = _clean_string_list(languages, field="languages", max_items=10,
                                           max_chars=40)
            supported = supported_languages()
            langs = {normalize_language(x) for x in requested}
            unknown = sorted(x for x in langs if x not in supported)
            if unknown:
                raise ValidationError(
                    f"unsupported language(s): {', '.join(unknown)}. "
                    f"Supported: {', '.join(sorted(supported))}. "
                    "Omit 'languages' to index every supported language found."
                )
        out = self.engine.index_repo(rid, root_path, languages=langs)
        out["workspace"] = ws
        out["repo"] = rp
        out["receipt"] = self.store.record_receipt(
            "index_repo", workspace_id=wid, repo_id=rid, actor="agent",
            target_count=out["files_indexed"], status="ok",
            metadata={"files_scanned": out["files_scanned"],
                      "files_indexed": out["files_indexed"],
                      "files_removed": out["files_removed"],
                      "symbols": out["symbols"], "edges": out["edges"]},
        )
        return out

    def index_repo_incremental(self, *, workspace: str, repo: str, root_path: str,
                               paths: list[str],
                               languages: Optional[list] = None) -> dict:
        """Incrementally re-index only the listed *paths* (absolute or repo-relative).

        Designed for filesystem-watcher callers: performs the same validated workspace
        / repo resolution and receipt recording as :meth:`index_repo`, but restricts
        the engine to the supplied paths instead of a full tree walk.  Files that no
        longer exist on disk are treated as deletions.
        """
        if not repo:
            raise ValidationError("repo is required to index code")
        ws = self._clean_ws(workspace)
        rp = _clean_name(repo, field="repo")
        root_path = _clean_text(root_path, field="root_path", max_chars=MAX_CONTENT_CHARS)
        if not isinstance(paths, (list, tuple)):
            raise ValidationError("paths must be a list of file paths")
        cleaned_paths = [
            _clean_text(p, field="paths[]", max_chars=MAX_CONTENT_CHARS) for p in paths
        ]
        wid = self._get_or_create_workspace(ws)
        rid = self.store.get_or_create_repo(wid, rp)
        langs = None
        if languages:
            from engraphis.backends.codegraph import normalize_language, supported_languages
            requested = _clean_string_list(languages, field="languages", max_items=10,
                                           max_chars=40)
            supported = supported_languages()
            langs = {normalize_language(x) for x in requested}
            unknown = sorted(x for x in langs if x not in supported)
            if unknown:
                raise ValidationError(
                    f"unsupported language(s): {', '.join(unknown)}. "
                    f"Supported: {', '.join(sorted(supported))}. "
                    "Omit 'languages' to index every supported language found."
                )
        out = self.engine.index_repo_incremental(rid, root_path, cleaned_paths, languages=langs)
        out["workspace"] = ws
        out["repo"] = rp
        out["receipt"] = self.store.record_receipt(
            "index_repo", workspace_id=wid, repo_id=rid, actor="agent",
            target_count=out["files_indexed"], status="ok",
            metadata={"files_scanned": out["files_scanned"],
                      "files_indexed": out["files_indexed"],
                      "files_removed": out["files_removed"],
                      "symbols": out["symbols"], "edges": out["edges"],
                      "incremental": True},
        )
        return out

    def search_code(self, query: str, *, workspace: str, repo: str, limit: int = 20,
                    as_of: Optional[float] = None,
                    valid_at: Optional[float] = None,
                    known_at: Optional[float] = None) -> dict:
        """Search code symbols and their memory bridges at one bi-temporal point.

        ``as_of`` is retained as the legacy alias for ``valid_at``.  Keeping the
        anchors on the direct code endpoint matters as much as on hybrid recall:
        callers otherwise receive a historical memory answer accompanied by present-day
        symbols and code-memory evidence.
        """
        if not repo:
            raise ValidationError("repo is required to search code")
        query = _clean_text(query, field="query", max_chars=MAX_CONTENT_CHARS)
        wid, rid = self._require_scope(workspace, repo)
        limit = max(1, min(MAX_K, int(limit)))
        as_of, valid_at, known_at = _temporal_anchors(
            as_of=as_of, valid_at=valid_at, known_at=known_at
        )
        return self.engine.search_code(
            query, repo_id=rid, limit=limit,
            flt=SearchFilter(
                workspace_id=wid, repo_id=rid, include_ancestors=True,
                as_of=as_of, valid_at=valid_at, known_at=known_at,
            ),
        )

    def code_path(self, source: str, target: str, *, workspace: str, repo: str,
                  max_depth: int = 8,
                  capacity: int = DEFAULT_CODE_QUERY_CAPACITY,
                  as_of: Optional[float] = None,
                  valid_at: Optional[float] = None,
                  known_at: Optional[float] = None) -> dict:
        if not repo:
            raise ValidationError("repo is required for a code path query")
        source = _clean_text(source, field="source", max_chars=500)
        target = _clean_text(target, field="target", max_chars=500)
        wid, rid = self._require_scope(workspace, repo)
        try:
            max_depth = max(1, min(32, int(max_depth)))
        except (TypeError, ValueError, OverflowError):
            raise ValidationError("max_depth must be an integer")
        capacity = _code_query_capacity(capacity)
        as_of, valid_at, known_at = _temporal_anchors(
            as_of=as_of, valid_at=valid_at, known_at=known_at
        )
        return self.engine.code_path(
            source, target, repo_id=rid, max_depth=max_depth, capacity=capacity,
            flt=SearchFilter(
                workspace_id=wid, repo_id=rid, include_ancestors=True,
                as_of=as_of, valid_at=valid_at, known_at=known_at,
            ),
        )

    def code_impact(self, changed_files: list, *, workspace: str, repo: str,
                    capacity: int = DEFAULT_CODE_QUERY_CAPACITY,
                    as_of: Optional[float] = None,
                    valid_at: Optional[float] = None,
                    known_at: Optional[float] = None) -> dict:
        if not repo:
            raise ValidationError("repo is required for impact analysis")
        files = _clean_string_list(
            changed_files, field="changed_files", max_items=2_000, max_chars=4_000
        )
        wid, rid = self._require_scope(workspace, repo)
        capacity = _code_query_capacity(capacity)
        as_of, valid_at, known_at = _temporal_anchors(
            as_of=as_of, valid_at=valid_at, known_at=known_at
        )
        return self.engine.analyze_impact(
            files, repo_id=rid, capacity=capacity,
            flt=SearchFilter(
                workspace_id=wid, repo_id=rid, include_ancestors=True,
                as_of=as_of, valid_at=valid_at, known_at=known_at,
            ),
        )

    def export_code_graph(self, *, workspace: str, repo: str,
                          capacity: int = DEFAULT_CODE_QUERY_CAPACITY,
                          as_of: Optional[float] = None,
                          valid_at: Optional[float] = None,
                          known_at: Optional[float] = None) -> dict:
        if not repo:
            raise ValidationError("repo is required to export a code graph")
        wid, rid = self._require_scope(workspace, repo)
        capacity = _code_query_capacity(capacity)
        as_of, valid_at, known_at = _temporal_anchors(
            as_of=as_of, valid_at=valid_at, known_at=known_at
        )
        flt = SearchFilter(
            workspace_id=wid, repo_id=rid, include_ancestors=True,
            as_of=as_of, valid_at=valid_at, known_at=known_at,
        )
        graph = self.engine.export_code_graph(repo_id=rid, limit=capacity, flt=flt)
        return {
            "graph": graph,
            "report_markdown": self.engine.code_graph_report(
                repo_id=rid, payload=graph, flt=flt
            ),
            "graph_html": self.engine.code_graph_html(
                repo_id=rid, payload=graph, flt=flt
            ),
            "valid_at": valid_at,
            "known_at": known_at,
            "historical": flt.historical,
        }

    def link_symbol(self, symbol_id: str, memory_id: str, *, workspace: str, repo: str,
                    relation: str = "mentions", confidence: float = 1.0,
                    reason: str = "") -> dict:
        """Create or reinforce a manual link between a code symbol and a memory.

        Validates that both the symbol and the memory exist within the given
        workspace/repo scope before writing. Idempotent: linking the same pair
        with the same relation returns the existing link id without duplicating.
        """
        if not repo:
            raise ValidationError("repo is required to link a symbol")
        symbol_id = _clean_text(symbol_id, field="symbol_id", max_chars=500)
        memory_id = _clean_text(memory_id, field="memory_id", max_chars=500)
        relation = _clean_name(relation, field="relation") or "mentions"
        reason = _clean_text(reason, field="reason", max_chars=MAX_TITLE_CHARS, required=False)
        _reject_secret_capture((("link_symbol reason", reason),))
        try:
            confidence = max(0.0, min(1.0, float(confidence)))
        except (TypeError, ValueError):
            raise ValidationError("confidence must be a number between 0 and 1")
        wid, rid = self._require_scope(workspace, repo)
        # Validate symbol exists in this repo.
        symbols = self.store.list_symbols(rid, identifiers=[symbol_id])
        if not symbols:
            raise ValidationError(f"no symbol '{symbol_id}' in repo '{repo}'")
        if len(symbols) > 1:
            raise ValidationError(
                f"symbol '{symbol_id}' is ambiguous; use the symbol ID or fully-qualified name"
            )
        # Validate memory exists and belongs to this workspace/repo.
        self._check_owns(memory_id, wid, rid)
        symbol = symbols[0]
        link_id = self.store.link_memory_symbol(
            repo_id=rid, symbol_id=symbol["id"], memory_id=memory_id,
            relation=relation, confidence=confidence,
        )
        principal = _authenticated_principal()
        actor = principal["id"] if principal is not None else "agent"
        receipt = self.store.record_receipt(
            "link", workspace_id=wid, repo_id=rid, actor=actor,
            target_count=1, status="ok",
            metadata={
                "relation": relation, "result_count": 1,
                "reason": reason[:200] if reason else "",
                "symbol_id": symbol["id"], "memory_id": memory_id,
            },
        )
        self.store.audit(
            actor, "link_symbol", link_id,
            f"symbol_id={symbol['id']}; memory_id={memory_id}; "
            f"relation={relation}; confidence={confidence:.6f}; reason={reason}",
        )
        return {"link_id": link_id, "symbol_id": symbol["id"],
                "memory_id": memory_id, "relation": relation,
                "reason": reason, "workspace": workspace, "repo": repo,
                "receipt": receipt}

    # ── inspection (powers the Memory Inspector UI) ─────────────────────────────
    def list_workspaces(self) -> dict:
        """Workspace/repo names with live-memory counts. On a bound instance only the
        permitted workspaces are listed — same boundary as every other read.

        Each entry carries ``visibility`` (``'shared'``/``'personal'``), plus whether the
        current user may change that access. In team mode a **personal** folder owned by
        someone other than the current user is omitted entirely — you can't see, count, or
        select a folder that isn't yours — mirroring the access check in
        ``_authorize_workspace``. Outside team mode there is no current user, so every
        folder is listed as before."""
        import time as _time
        now = _time.time()
        rows = self.store.conn.execute(
            "SELECT w.id, w.name, w.settings AS settings, COUNT(m.id) AS n FROM workspaces w "
            "LEFT JOIN memories m ON m.workspace_id = w.id "
            "AND COALESCE(m.scope, 'workspace')!='session' "
            "AND (m.valid_from IS NULL OR m.valid_from<=?) "
            "AND (m.valid_to IS NULL OR ?<m.valid_to) AND m.expired_at IS NULL "
            "GROUP BY w.id, w.name, w.settings ORDER BY w.name", (now, now)).fetchall()
        user = _authenticated_principal()
        my_email = user["email"] if user is not None else ""
        out = []
        for r in rows:
            if self.allowed_workspaces is not None and r["name"] not in self.allowed_workspaces:
                continue
            try:
                _vis, _owner = self._workspace_visibility(r["name"])
            except ValidationError:
                # A malformed access envelope is not a shared-folder declaration. Do not
                # list it as readable/selectable; repair requires an explicit operator action.
                continue
            try:
                _s = json.loads(r["settings"]) if r["settings"] else {}
                if not isinstance(_s, dict):
                    _s = {}
            except (TypeError, ValueError, RecursionError):
                _s = {}
            _desc = _s.get("description") or ""
            _owner_normalized = str(_owner).casefold()
            # Hide other users' personal folders from the listing (team mode only).
            if (user and _vis == "personal" and _owner
                    and _owner_normalized != my_email):
                continue
            repos = [dict(x) for x in self.store.conn.execute(
                "SELECT name FROM repos WHERE workspace_id=? ORDER BY name", (r["id"],))]
            entry = {"name": r["name"], "memories": int(r["n"]), "description": _desc,
                     "visibility": _vis, "repos": [x["name"] for x in repos]}
            if user:
                entry["can_change_access"] = bool(
                    _owner_normalized == my_email or user.get("role") == "admin"
                )
            if _vis == "personal":
                entry["owner"] = _owner
                entry["mine"] = bool(my_email and _owner_normalized == my_email)
            out.append(entry)
        return {"workspaces": out}

    # ── workspace curation (create / rename / describe / delete) ─────────────────
    @_rollback_service_transaction
    def create_workspace(self, name: str, description: str = "", *,
                         visibility: str = "personal", confirmed: bool = False,
                         actor: str = "user") -> dict:
        """Create an empty workspace (a "folder") so a team can set one up *before* any
        memory is written to it — the dashboard's Workspaces tab and the agent write path
        both otherwise only mint a workspace lazily (``get_or_create_workspace``), which
        left no way to pre-create the folders users then choose to submit to. Enforces the
        same explicit binding and name validation every other entry point does, so a
        deliberately bound service still refuses names outside its allow-list, and rejects
        a name that already exists (mirrors ``rename``'s uniqueness check).

        ``visibility`` defaults to ``'personal'``: a new team folder is private to its
        creator until they intentionally share it. ``'shared'`` requires
        ``confirmed=True`` so a client cannot make a whole-team folder by omission.
        Personal requires a signed-in dashboard user to own it; outside team mode there is
        no identity, so the established single-tenant behaviour remains unrestricted."""
        ws = self._clean_ws(name)
        description = _clean_text(description, field="description",
                                  max_chars=MAX_CONTENT_CHARS, required=False)
        actor = _clean_text(actor, field="actor", max_chars=MAX_NAME_CHARS,
                            required=False) or "user"
        visibility = str(visibility or "personal").lower()
        if visibility not in ("personal", "shared"):
            raise ValidationError("visibility must be 'personal' or 'shared'")
        if visibility == "shared" and confirmed is not True:
            raise ValidationError("sharing a folder requires explicit confirmation")
        user = _authenticated_principal()
        owner = user["email"] if user is not None else ""
        if visibility == "personal" and not owner:
            visibility = "shared"  # no identity to own it — don't orphan the folder
        if self._lookup_workspace(ws) is not None:
            raise ValidationError(f"a workspace named '{ws}' already exists")
        ws_settings: dict = {}
        if description:
            ws_settings["description"] = description
        ws_settings["visibility"] = visibility
        if owner:
            # For personal folders this is the access boundary. For deliberately shared
            # folders it records who may reverse the sharing decision later.
            ws_settings["owner"] = owner
        wid = self.store.create_workspace(ws, settings=ws_settings or None)
        self.store.audit(actor, "workspace_create", wid,
                         "%s (%s%s)" % (ws, visibility, ("; owner=" + owner) if owner else ""))
        self.store.conn.commit()
        return {"workspace": ws, "id": wid, "description": description,
                "visibility": visibility,
                "owner": owner if visibility == "personal" else "", "created": True}

    @_rollback_service_transaction
    def set_workspace_visibility(self, workspace: str, visibility: str, *,
                                 confirmed: bool = False, actor: str = "user") -> dict:
        """Explicitly share or unshare a team folder after user confirmation."""
        ws = self._clean_ws(workspace)
        target = str(visibility or "").lower()
        if target not in ("personal", "shared"):
            raise ValidationError("visibility must be 'personal' or 'shared'")
        if confirmed is not True:
            raise ValidationError("changing folder access requires explicit confirmation")
        user = _authenticated_principal()
        owner = user["email"] if user is not None else ""
        if not owner:
            raise ValidationError("changing folder access requires a signed-in team user")
        actor = _clean_text(actor, field="actor", max_chars=MAX_NAME_CHARS,
                            required=False) or "user"
        wid = self._lookup_workspace(ws)
        if wid is None:
            raise ValidationError(f"no workspace named '{ws}' yet")
        row = self.store.conn.execute("SELECT settings FROM workspaces WHERE id=?", (wid,)).fetchone()
        try:
            workspace_settings = json.loads(row["settings"]) if row and row["settings"] else {}
            if not isinstance(workspace_settings, dict):
                workspace_settings = {}
        except Exception:
            workspace_settings = {}
        previous, previous_owner = self._workspace_visibility(ws)
        if previous == "shared" and target == "personal" \
                and str(previous_owner).casefold() != owner \
                and user.get("role") != "admin":
            # Making a team-visible folder private removes it from every other member.
            # The user who deliberately shared it may reverse that decision; otherwise
            # only an admin may claim a legacy/team-owned shared workspace.
            raise ValidationError(
                "only the original sharer or an admin can make a shared folder personal")
        if target == "personal":
            workspace_settings["visibility"] = "personal"
            workspace_settings["owner"] = owner
            action = "workspace_unshare"
        else:
            workspace_settings["visibility"] = "shared"
            if previous == "personal":
                # Keep a controller while the folder is shared so its creator can undo
                # their own sharing decision. Ownership is not an access restriction while
                # visibility is shared and is not exposed by list_workspaces.
                workspace_settings["owner"] = previous_owner or owner
            action = "workspace_share"
        self.store.conn.execute("UPDATE workspaces SET settings=? WHERE id=?",
                                (json.dumps(workspace_settings), wid))
        self.store.audit(actor, action, wid, f"{ws}: {previous} -> {target}")
        self.store.conn.commit()
        return {"workspace": ws, "visibility": target,
                "owner": owner if target == "personal" else "", "changed": previous != target}

    @_rollback_service_transaction
    def rename_workspace(self, workspace: str, new_name: str, *, actor: str = "user") -> dict:
        """Rename a workspace's label. Memories key off ``workspace_id``, so this is a pure
        relabel — all data stays attached. Same binding + uniqueness the create path enforces."""
        old = self._clean_ws(workspace)
        self._authorize_workspace_control(old)
        new = self._authorize_workspace(_clean_name(new_name, field="new_name"))
        actor = _clean_text(actor, field="actor", max_chars=MAX_NAME_CHARS, required=False) or "user"
        wid = self._lookup_workspace(old)
        if wid is None:
            raise ValidationError(f"no workspace named '{old}' yet")
        if new != old and self._lookup_workspace(new) is not None:
            raise ValidationError(f"a workspace named '{new}' already exists")
        self.store.conn.execute("UPDATE workspaces SET name=? WHERE id=?", (new, wid))
        self.store.audit(actor, "workspace_rename", wid, f"{old} -> {new}")
        self.store.conn.commit()
        return {"old": old, "new": new, "id": wid}

    @_rollback_service_transaction
    def set_workspace_description(self, workspace: str, description: str,
                                 *, actor: str = "user") -> dict:
        """Store a human description in the workspace's ``settings`` JSON (no schema change)."""
        ws = self._clean_ws(workspace)
        self._authorize_workspace_control(ws)
        description = _clean_text(description, field="description",
                                  max_chars=MAX_CONTENT_CHARS, required=False)
        actor = _clean_text(actor, field="actor", max_chars=MAX_NAME_CHARS, required=False) or "user"
        wid = self._lookup_workspace(ws)
        if wid is None:
            raise ValidationError(f"no workspace named '{ws}' yet")
        row = self.store.conn.execute("SELECT settings FROM workspaces WHERE id=?", (wid,)).fetchone()
        try:
            settings = json.loads(row["settings"]) if row and row["settings"] else {}
            if not isinstance(settings, dict):
                settings = {}
        except Exception:
            settings = {}
        settings["description"] = description
        self.store.conn.execute("UPDATE workspaces SET settings=? WHERE id=?",
                                (json.dumps(settings), wid))
        self.store.audit(actor, "workspace_describe", wid, description[:200])
        self.store.conn.commit()
        return {"workspace": ws, "description": description}

    @_rollback_service_transaction
    def delete_workspace(self, workspace: str, *, actor: str = "user") -> dict:
        """HARD-delete a workspace and everything scoped to it (memories, vectors, FTS rows,
        entities/edges, sessions, events, repos + their code graph). Unlike ``forget`` this is
        irreversible, so the UI gates it behind an explicit confirm. Audit rows are retained."""
        ws = self._clean_ws(workspace)
        self._authorize_workspace_control(ws)
        actor = _clean_text(actor, field="actor", max_chars=MAX_NAME_CHARS, required=False) or "user"
        wid = self._lookup_workspace(ws)
        if wid is None:
            raise ValidationError(f"no workspace named '{ws}' yet")
        self._assert_no_active_graph_job(wid)
        c = self.store.conn
        memory_ids = [row["id"] for row in c.execute(
            "SELECT id FROM memories WHERE workspace_id=?", (wid,)
        ).fetchall()]
        n_mem = len(memory_ids)
        msub = "(SELECT id FROM memories WHERE workspace_id=?)"
        rsub = "(SELECT id FROM repos WHERE workspace_id=?)"
        ssub = f"(SELECT id FROM symbols WHERE repo_id IN {rsub})"
        # Retire each memory's evidence before the hard delete. This removes the
        # memory from any global/legacy edge it supported and closes an edge whose last
        # source is disappearing. Then delete the normalized evidence rows themselves:
        # hard deletion must not leave orphaned provenance behind.
        for memory_id in memory_ids:
            self.store.invalidate_edges_for_memory(memory_id, commit=False)
        c.execute(
            "DELETE FROM edge_supports WHERE memory_id IN " + msub,
            (wid,),
        )
        c.execute(
            "DELETE FROM edge_supports WHERE edge_id IN "
            "(SELECT id FROM edges WHERE workspace_id=?)",
            (wid,),
        )
        c.execute(
            f"DELETE FROM code_memory_links WHERE repo_id IN {rsub} "
            f"OR memory_id IN {msub} OR symbol_id IN {ssub}",
            (wid, wid, wid),
        )
        c.execute(
            "DELETE FROM memory_entities WHERE workspace_id=? "
            f"OR memory_id IN {msub} "
            "OR entity_id IN (SELECT id FROM entities WHERE workspace_id=?)",
            (wid, wid, wid),
        )
        c.execute(f"DELETE FROM mem_fts WHERE id IN {msub}", (wid,))
        c.execute(f"DELETE FROM mem_vectors WHERE id IN {msub}", (wid,))
        try:
            c.execute(f"DELETE FROM mem_vec_ann WHERE id IN {msub}", (wid,))
        except Exception:
            pass  # sqlite-vec vector table only present when that backend is active
        c.execute(f"DELETE FROM mem_links WHERE a IN {msub} OR b IN {msub}", (wid, wid))
        c.execute("DELETE FROM memories WHERE workspace_id=?", (wid,))
        # These content-free sync/governance rows are not foreign-key cascades. A
        # hard workspace deletion must remove them too, otherwise stale markers can
        # later authorize a tombstone or block a newly created workspace's sync.
        c.execute("DELETE FROM memory_sync_exports WHERE workspace_id=?", (wid,))
        c.execute("DELETE FROM memory_tombstones WHERE workspace_id=?", (wid,))
        c.execute("DELETE FROM maintenance_cursors WHERE workspace_id=?", (wid,))
        c.execute("DELETE FROM entities WHERE workspace_id=?", (wid,))
        c.execute("DELETE FROM edges WHERE workspace_id=?", (wid,))
        c.execute("DELETE FROM sessions WHERE workspace_id=?", (wid,))
        c.execute("DELETE FROM events WHERE workspace_id=?", (wid,))
        c.execute(f"DELETE FROM code_files WHERE repo_id IN {rsub}", (wid,))
        c.execute(f"DELETE FROM code_edges WHERE repo_id IN {rsub}", (wid,))
        c.execute(f"DELETE FROM symbols WHERE repo_id IN {rsub}", (wid,))
        c.execute("DELETE FROM repos WHERE workspace_id=?", (wid,))
        c.execute("DELETE FROM jobs WHERE workspace_id=?", (wid,))
        c.execute("DELETE FROM operation_receipts WHERE workspace_id=?", (wid,))
        c.execute("DELETE FROM receipt_chain_heads WHERE workspace_id=?", (wid,))
        # Entity/edge delete triggers may have recreated this generation row.
        c.execute("DELETE FROM graph_index_state WHERE workspace_id=?", (wid,))
        c.execute("DELETE FROM workspaces WHERE id=?", (wid,))
        self.store.audit(actor, "workspace_delete", wid, f"{ws} ({int(n_mem)} memories)")
        c.commit()
        return {"workspace": ws, "deleted": True, "memories_removed": int(n_mem)}

    @_rollback_service_transaction
    def merge_workspaces(self, source: str, target: str, *, actor: str = "user") -> dict:
        """Fold ``source`` into ``target``, then remove the now-empty ``source``
        workspace. This is the workspace-level counterpart to ``merge`` — and the
        dashboard deliberately exposes *only* this, not free-form merging of
        hand-picked, possibly-unrelated memories (see the removed multi-select
        "Merge selected" flow). Unlike ``merge``, this is lossless: every memory
        keeps its own id, content and full history, it just changes workspace.
        Repos/entities that collide by name with something already in ``target``
        are folded together (their memories, edges and code symbols repointed at
        the surviving row); source-import manifests are rehomed or merged by source
        identity; everything else is simply relabeled onto ``target``.
        Irreversible, so the UI gates it behind a confirm, same as delete."""
        src = self._clean_ws(source)
        dst = self._clean_ws(target)
        self._authorize_workspace_control(src)
        self._authorize_workspace_control(dst)
        actor = _clean_text(actor, field="actor", max_chars=MAX_NAME_CHARS, required=False) or "user"
        if src == dst:
            raise ValidationError("source and target workspaces must be different")
        wid_src = self._lookup_workspace(src)
        wid_dst = self._lookup_workspace(dst)
        if wid_src is None:
            raise ValidationError(f"no workspace named '{src}' yet")
        if wid_dst is None:
            raise ValidationError(f"no workspace named '{dst}' yet")
        self._assert_no_active_graph_job(wid_src, wid_dst)
        c = self.store.conn
        n_mem = c.execute("SELECT COUNT(*) AS n FROM memories WHERE workspace_id=?",
                          (wid_src,)).fetchone()["n"]

        # 1) Repos: fold same-named repos together (repoint their incremental file
        #    state, symbols, code edges, and memory bridges at the surviving row and
        #    drop the duplicate), else just relabel.
        repo_remap: dict = {}
        src_repos = [dict(x) for x in c.execute(
            "SELECT id, name FROM repos WHERE workspace_id=?", (wid_src,))]

        def _remap_file_links(loser_repo: str, winner_repo: str, file: str) -> None:
            """Re-point memory↔code links from a losing file snapshot's symbols to
            the winning snapshot's same-fqname symbols, so provenance survives the
            fold instead of being cleared with the stale symbols. Links whose
            symbol has no surviving counterpart (or that would duplicate an
            existing link) are left for ``clear_symbols_for_file`` to drop."""
            rows = c.execute(
                "SELECT l.id AS link_id, s.fqname FROM code_memory_links l "
                "JOIN symbols s ON s.id=l.symbol_id "
                "WHERE s.repo_id=? AND s.file=?",
                (loser_repo, file),
            ).fetchall()
            for row in rows:
                winner = c.execute(
                    "SELECT id FROM symbols WHERE repo_id=? AND file=? AND fqname=? "
                    "LIMIT 1",
                    (winner_repo, file, row["fqname"]),
                ).fetchone()
                if winner:
                    c.execute(
                        "UPDATE OR IGNORE code_memory_links SET symbol_id=? "
                        "WHERE id=?",
                        (winner["id"], row["link_id"]),
                    )

        for r in src_repos:
            existing = c.execute(
                "SELECT id FROM repos WHERE workspace_id=? AND name=?", (wid_dst, r["name"])
            ).fetchone()
            if existing:
                repo_remap[r["id"]] = existing["id"]
                # ``code_files`` is keyed by (repo_id, file), so fold overlapping file
                # snapshots deterministically before the duplicate repo disappears.
                for code_file in [dict(x) for x in c.execute(
                        "SELECT * FROM code_files WHERE repo_id=?", (r["id"],))]:
                    current = c.execute(
                        "SELECT * FROM code_files WHERE repo_id=? AND file=?",
                        (existing["id"], code_file["file"]),
                    ).fetchone()
                    if current is None:
                        c.execute(
                            "UPDATE code_files SET repo_id=? WHERE repo_id=? AND file=?",
                            (existing["id"], r["id"], code_file["file"]),
                        )
                        continue
                    current = dict(current)
                    incoming_key = (
                        float(code_file["indexed_at"] or 0),
                        str(code_file["content_hash"] or ""),
                    )
                    current_key = (
                        float(current["indexed_at"] or 0),
                        str(current["content_hash"] or ""),
                    )
                    if incoming_key > current_key:
                        c.execute(
                            "UPDATE code_files SET lang=?, content_hash=?, size_bytes=?, "
                            "mtime_ns=?, backend=?, indexed_at=? WHERE repo_id=? AND file=?",
                            (
                                code_file["lang"], code_file["content_hash"],
                                code_file["size_bytes"], code_file["mtime_ns"],
                                code_file["backend"], code_file["indexed_at"],
                                existing["id"], code_file["file"],
                            ),
                        )
                        # The surviving repo's older snapshot of this file loses:
                        # re-point its memory links at the incoming same-fqname
                        # symbols, then drop its symbols/edges so the incoming
                        # ones don't land next to stale duplicates.
                        _remap_file_links(existing["id"], r["id"], code_file["file"])
                        self.store.clear_symbols_for_file(
                            existing["id"], code_file["file"], commit=False
                        )
                    else:
                        # The surviving repo's snapshot wins: re-point the losing
                        # side's memory links at the surviving symbols, then drop
                        # its symbols before the blanket repo-id relabel would
                        # move them over as duplicates.
                        _remap_file_links(r["id"], existing["id"], code_file["file"])
                        self.store.clear_symbols_for_file(
                            r["id"], code_file["file"], commit=False
                        )
                    c.execute(
                        "DELETE FROM code_files WHERE repo_id=? AND file=?",
                        (r["id"], code_file["file"]),
                    )
                c.execute("UPDATE symbols SET repo_id=? WHERE repo_id=?", (existing["id"], r["id"]))
                c.execute("UPDATE code_edges SET repo_id=? WHERE repo_id=?", (existing["id"], r["id"]))
                # OR IGNORE + delete-leftovers: a link remapped by fqname above
                # could otherwise collide with an identical surviving link on the
                # UNIQUE(repo_id, symbol_id, memory_id, relation) constraint.
                c.execute(
                    "UPDATE OR IGNORE code_memory_links SET repo_id=? WHERE repo_id=?",
                    (existing["id"], r["id"]),
                )
                c.execute("DELETE FROM code_memory_links WHERE repo_id=?", (r["id"],))
                c.execute("DELETE FROM repos WHERE id=?", (r["id"],))
            else:
                c.execute("UPDATE repos SET workspace_id=? WHERE id=?", (wid_dst, r["id"]))

        def _new_repo(old_repo_id):
            return repo_remap.get(old_repo_id, old_repo_id) if old_repo_id is not None else None

        # 2) Entities: fold same name+type+repo together, else relabel.
        entity_remap: dict = {}
        src_entities = [dict(x) for x in c.execute(
            "SELECT id, repo_id, name, etype, canonical_id, normalized_name "
            "FROM entities WHERE workspace_id=?", (wid_src,))]
        for e in src_entities:
            nrid = _new_repo(e["repo_id"])
            normalized = e.get("normalized_name") or normalize_entity_name(e["name"])
            existing = c.execute(
                "SELECT id, canonical_id FROM entities WHERE workspace_id=? AND repo_id IS ? "
                "AND normalized_name=? AND etype IS ? ORDER BY id LIMIT 1",
                (wid_dst, nrid, normalized, e["etype"])
            ).fetchone()
            if existing:
                entity_remap[e["id"]] = existing["id"]
                c.execute("DELETE FROM entities WHERE id=?", (e["id"],))
            else:
                canonical = c.execute(
                    "SELECT COALESCE(canonical_id, id) AS canonical_id FROM entities "
                    "WHERE workspace_id=? AND normalized_name=? AND etype IS ? "
                    "ORDER BY id LIMIT 1",
                    (wid_dst, normalized, e["etype"]),
                ).fetchone()
                c.execute(
                    "UPDATE entities SET workspace_id=?, repo_id=?, normalized_name=?, "
                    "canonical_id=?, canonical_method=? WHERE id=?",
                    (wid_dst, nrid, normalized,
                     canonical["canonical_id"] if canonical else (e["canonical_id"] or e["id"]),
                     "exact_normalized" if canonical else "exact", e["id"]),
                )
        for old_id, new_id in entity_remap.items():
            c.execute(
                "UPDATE entities SET canonical_id=? WHERE workspace_id=? AND canonical_id=?",
                (new_id, wid_dst, old_id),
            )

        # Rehome the persisted sparse graph index alongside its memory/entity
        # endpoints. Entity folding can make two live incidences equivalent; retain
        # the duplicate as closed history instead of violating the partial unique
        # index or deleting evidence.
        incidence_closed_at = time.time()
        source_incidence = [dict(row) for row in c.execute(
            "SELECT * FROM memory_entities WHERE workspace_id=? ORDER BY id",
            (wid_src,),
        )]
        for incidence in source_incidence:
            mapped_entity = entity_remap.get(
                incidence["entity_id"], incidence["entity_id"]
            )
            mapped_repo = _new_repo(incidence["repo_id"])
            live = incidence["valid_to"] is None and incidence["expired_at"] is None
            duplicate = None
            if live:
                duplicate = c.execute(
                    "SELECT id, confidence, valid_from, ingested_at "
                    "FROM memory_entities WHERE id<>? AND memory_id=? "
                    "AND entity_id=? AND source_kind=? "
                    "AND valid_to IS NULL AND expired_at IS NULL LIMIT 1",
                    (
                        incidence["id"], incidence["memory_id"], mapped_entity,
                        incidence["source_kind"],
                    ),
                ).fetchone()
            if duplicate is None:
                c.execute(
                    "UPDATE memory_entities SET workspace_id=?, repo_id=?, entity_id=? "
                    "WHERE id=?",
                    (wid_dst, mapped_repo, mapped_entity, incidence["id"]),
                )
                continue
            valid_values = [
                value for value in (
                    duplicate["valid_from"], incidence["valid_from"]
                ) if value is not None
            ]
            known_values = [
                value for value in (
                    duplicate["ingested_at"], incidence["ingested_at"]
                ) if value is not None
            ]
            c.execute(
                "UPDATE memory_entities SET confidence=?, valid_from=?, ingested_at=? "
                "WHERE id=?",
                (
                    max(
                        float(duplicate["confidence"] or 0.0),
                        float(incidence["confidence"] or 0.0),
                    ),
                    min(valid_values) if valid_values else None,
                    min(known_values) if known_values else None,
                    duplicate["id"],
                ),
            )
            c.execute(
                "UPDATE memory_entities SET workspace_id=?, repo_id=?, entity_id=?, "
                "valid_to=?, valid_to_recorded_at=?, expired_at=? WHERE id=?",
                (
                    wid_dst, mapped_repo, mapped_entity,
                    incidence_closed_at, incidence_closed_at,
                    incidence_closed_at, incidence["id"],
                ),
            )

        # 3) Edges: relabel workspace/repo, remapping any entity ids folded in step 2.
        #    When a live source edge collides with an existing live target edge (same
        #    src/dst/relation/layer/repo), merge metadata instead of violating the
        #    partial unique index — mirrors Store._deduplicate_live_edges().
        src_edges = [dict(x) for x in c.execute(
            "SELECT id, repo_id, src, dst, relation, layer, weight, provenance, "
            "valid_from, ingested_at, valid_to, expired_at "
            "FROM edges WHERE workspace_id=?", (wid_src,))]
        for ed in src_edges:
            new_src = entity_remap.get(ed["src"], ed["src"])
            new_dst = entity_remap.get(ed["dst"], ed["dst"])
            new_repo = _new_repo(ed["repo_id"])
            is_live = ed["valid_to"] is None and ed["expired_at"] is None
            target = None
            if is_live:
                # Check for a live target edge with the same identity.
                if new_repo is not None:
                    target = c.execute(
                        "SELECT id, weight, provenance, valid_from, ingested_at "
                        "FROM edges WHERE workspace_id=? AND repo_id=? AND src=? "
                        "AND dst=? AND relation=? AND layer=? "
                        "AND valid_to IS NULL AND expired_at IS NULL LIMIT 1",
                        (wid_dst, new_repo, new_src, new_dst,
                         ed["relation"], ed["layer"]),
                    ).fetchone()
                else:
                    target = c.execute(
                        "SELECT id, weight, provenance, valid_from, ingested_at "
                        "FROM edges WHERE workspace_id=? AND repo_id IS NULL "
                        "AND src=? AND dst=? AND relation=? AND layer=? "
                        "AND valid_to IS NULL AND expired_at IS NULL LIMIT 1",
                        (wid_dst, new_src, new_dst,
                         ed["relation"], ed["layer"]),
                    ).fetchone()
            if target:
                # Merge: keep target as survivor, retire source edge.
                closed_at = time.time()
                src_prov = _loads(ed["provenance"], {})
                tgt_prov = _loads(target["provenance"], {})
                merged_prov = _merge_edge_provenance(
                    [tgt_prov, src_prov], merged_ids=[ed["id"]])
                merged_weight = max(
                    float(ed["weight"] or 0.0),
                    float(target["weight"] or 0.0))
                valid_vals = [v for v in (target["valid_from"], ed["valid_from"])
                              if v is not None]
                ingested_vals = [v for v in
                                 (target["ingested_at"], ed["ingested_at"])
                                 if v is not None]
                c.execute(
                    "UPDATE edges SET weight=?, provenance=?, "
                    "valid_from=?, ingested_at=? WHERE id=?",
                    (merged_weight,
                     json.dumps(merged_prov, ensure_ascii=False),
                     min(valid_vals) if valid_vals else None,
                     min(ingested_vals) if ingested_vals else None,
                     target["id"]))
                # Move live edge_supports from source to target, skipping any that
                # would collide with an identical live support already on the
                # survivor (idx_edge_support_live_unique is a partial unique index
                # on (edge_id, memory_id, source_kind) WHERE live) — a plain UPDATE
                # would raise IntegrityError and roll back the whole merge.
                c.execute(
                    "UPDATE OR IGNORE edge_supports SET edge_id=? WHERE edge_id=? "
                    "AND valid_to IS NULL AND expired_at IS NULL",
                    (target["id"], ed["id"]))
                # Whatever OR IGNORE left behind is still live but attached to an
                # edge that's about to close — soft-close it too instead of leaving
                # orphaned live evidence on a non-live edge, mirroring how
                # Store._deduplicate_live_edges() closes retired supports.
                c.execute(
                    "UPDATE edge_supports SET valid_to=?, valid_to_recorded_at=?, "
                    "expired_at=? "
                    "WHERE edge_id=? AND valid_to IS NULL AND expired_at IS NULL",
                    (closed_at, closed_at, closed_at, ed["id"]))
                # Bi-temporally close the source edge.
                src_prov["canonical_deduplicated_into"] = target["id"]
                c.execute(
                    "UPDATE edges SET valid_to=?, valid_to_recorded_at=?, expired_at=?, "
                    "provenance=? WHERE id=?",
                    (closed_at, closed_at, closed_at,
                     json.dumps(src_prov, ensure_ascii=False), ed["id"]))
            else:
                c.execute(
                    "UPDATE edges SET workspace_id=?, repo_id=?, src=?, dst=? "
                    "WHERE id=?",
                    (wid_dst, new_repo, new_src, new_dst, ed["id"]))

        # 4) Memories / sessions / events: relabel workspace/repo per distinct repo_id
        #    bucket (ids, content and history are untouched).
        for table in ("memories", "sessions", "events", "jobs"):
            buckets = [dict(x) for x in c.execute(
                f"SELECT DISTINCT repo_id FROM {table} WHERE workspace_id=?", (wid_src,))]
            for b in buckets:
                c.execute(
                    f"UPDATE {table} SET workspace_id=?, repo_id=? "
                    f"WHERE workspace_id=? AND repo_id IS ?",
                    (wid_dst, _new_repo(b["repo_id"]), wid_src, b["repo_id"]))

        # Source-import manifests are durable workspace state, not disposable job
        # output. Rehome them after repos, sessions, memories, and jobs so their scope
        # triggers see the destination hierarchy. When both workspaces imported the
        # same source identity, keep one vault and merge its current per-path manifest;
        # source-import job items are repointed before the losing row is removed so
        # historical reports do not lose their source references.
        from engraphis.obsidian_import import stable_source_key as _stable_source_key

        def _source_key_for_destination(item: dict, destination_vault_id: str) -> str:
            """Re-key a current source item when two vault ids are folded together."""
            memory_id = item.get("memory_id")
            if not memory_id:
                return str(item["source_key"])
            record = self.store.get_memory(str(memory_id))
            metadata = record.metadata if record is not None else {}
            envelope = (
                metadata.get("document") or metadata.get("obsidian")
                if isinstance(metadata, dict) else {}
            )
            branch = envelope.get("branch") if isinstance(envelope, dict) else ""
            return _stable_source_key(
                destination_vault_id, str(item["relative_path"]), branch=str(branch or "")
            )

        def _rewrite_import_memory_source(
            memory_id: Optional[str], *, source_id: str, vault_id: str,
        ) -> None:
            if not memory_id:
                return
            row = c.execute(
                "SELECT metadata FROM memories WHERE id=?", (memory_id,)
            ).fetchone()
            if row is None:
                return
            metadata = _loads(row["metadata"], {})
            if not isinstance(metadata, dict):
                return
            envelope_key = "document" if isinstance(metadata.get("document"), dict) else "obsidian"
            envelope = metadata.get(envelope_key)
            if not isinstance(envelope, dict):
                return
            envelope["source_id"] = source_id
            envelope["vault_id"] = vault_id
            c.execute(
                "UPDATE memories SET metadata=? WHERE id=?",
                (json.dumps(metadata, ensure_ascii=False, separators=(",", ":")), memory_id),
            )

        def _invalidate_import_memory(memory_id: Optional[str]) -> None:
            """Close a manifest memory that loses a duplicate-path merge race."""
            if not memory_id:
                return
            self.store.close_validity(
                str(memory_id), actor=actor,
                reason="source_import_merge_duplicate", commit=False,
            )

        source_vaults = [dict(row) for row in c.execute(
            "SELECT * FROM source_vaults WHERE workspace_id=? ORDER BY id", (wid_src,)
        )]
        for source_vault in source_vaults:
            source_vault_id = str(source_vault["id"])
            mapped_repo = _new_repo(source_vault.get("repo_id"))
            target_vault = c.execute(
                "SELECT * FROM source_vaults WHERE kind=? AND root_digest=? "
                "AND workspace_id=? AND repo_id IS ? AND session_id IS ?",
                (
                    source_vault["kind"], source_vault["root_digest"], wid_dst,
                    mapped_repo, source_vault.get("session_id"),
                ),
            ).fetchone()
            if target_vault is None:
                c.execute(
                    "UPDATE source_vaults SET workspace_id=?, repo_id=? WHERE id=?",
                    (wid_dst, mapped_repo, source_vault_id),
                )
                continue

            target_vault = dict(target_vault)
            target_vault_id = str(target_vault["id"])
            source_items = [dict(row) for row in c.execute(
                "SELECT * FROM source_imports WHERE vault_id=? ORDER BY source_key, id",
                (source_vault_id,),
            )]
            for source_item in source_items:
                destination_source_key = _source_key_for_destination(
                    source_item, target_vault_id,
                )
                target_item = c.execute(
                    "SELECT * FROM source_imports WHERE vault_id=? AND source_key=?",
                    (target_vault_id, destination_source_key),
                ).fetchone()
                if target_item is None:
                    _rewrite_import_memory_source(
                        str(source_item.get("memory_id") or "") or None,
                        source_id=str(source_item["id"]), vault_id=target_vault_id,
                    )
                    c.execute(
                        "UPDATE source_imports SET vault_id=?, source_key=? WHERE id=?",
                        (target_vault_id, destination_source_key, source_item["id"]),
                    )
                    continue

                target_item = dict(target_item)
                # Job rows have already moved to the destination workspace. Preserve
                # their source references before the losing source_imports row is
                # removed by its foreign-key cascade.
                c.execute(
                    "UPDATE source_import_items SET source_id=? WHERE source_id=?",
                    (target_item["id"], source_item["id"]),
                )
                source_seen = float(source_item.get("last_seen_at") or 0.0)
                target_seen = float(target_item.get("last_seen_at") or 0.0)
                source_memory_id = str(source_item.get("memory_id") or "") or None
                target_memory_id = str(target_item.get("memory_id") or "") or None
                if source_seen >= target_seen:
                    if target_memory_id != source_memory_id:
                        _invalidate_import_memory(target_memory_id)
                    _rewrite_import_memory_source(
                        source_memory_id,
                        source_id=str(target_item["id"]), vault_id=target_vault_id,
                    )
                    c.execute(
                        "UPDATE source_imports SET relative_path=?, memory_id=?, "
                        "subject_key=?, content_sha256=?, canonical_sha256=?, "
                        "file_size=?, file_mtime_ns=?, importer_version=?, "
                        "last_seen_job_id=?, state=?, first_imported_at=?, "
                        "last_imported_at=?, last_seen_at=?, missing_at=?, last_error=? "
                        "WHERE id=?",
                        (
                            source_item["relative_path"], source_item["memory_id"],
                            source_item["subject_key"], source_item["content_sha256"],
                            source_item["canonical_sha256"], source_item["file_size"],
                            source_item["file_mtime_ns"], source_item["importer_version"],
                            source_item["last_seen_job_id"], source_item["state"],
                            source_item["first_imported_at"], source_item["last_imported_at"],
                            source_item["last_seen_at"], source_item["missing_at"],
                            source_item["last_error"], target_item["id"],
                        ),
                    )
                elif source_memory_id != target_memory_id:
                    _invalidate_import_memory(source_memory_id)
                c.execute("DELETE FROM source_imports WHERE id=?", (source_item["id"],))
            c.execute("DELETE FROM source_vaults WHERE id=?", (source_vault_id,))

        # Export proofs and remote-erasure markers survive memory re-homing so the
        # next sync can still converge. Their repository owner follows the same
        # collision map as the memories themselves.
        for row in [dict(x) for x in c.execute(
                "SELECT memory_id, repo_id FROM memory_sync_exports "
                "WHERE workspace_id=?", (wid_src,))]:
            c.execute(
                "UPDATE memory_sync_exports SET workspace_id=?, repo_id=? "
                "WHERE memory_id=?",
                (wid_dst, _new_repo(row["repo_id"]), row["memory_id"]),
            )
        for row in [dict(x) for x in c.execute(
                "SELECT memory_id, repo_id FROM memory_tombstones "
                "WHERE workspace_id=?", (wid_src,))]:
            c.execute(
                "UPDATE memory_tombstones SET workspace_id=?, repo_id=? "
                "WHERE memory_id=?",
                (wid_dst, _new_repo(row["repo_id"]), row["memory_id"]),
            )
        c.execute("DELETE FROM maintenance_cursors WHERE workspace_id=?", (wid_src,))

        # 5) Receipt payload hashes bind their original workspace scope digest and chain
        # predecessor. Re-homing them would either forge that evidence or fork the target
        # chain, so remove the source-only ledger with the source workspace. The merge's
        # target-scoped audit entry below remains as the durable governance record.
        c.execute("DELETE FROM operation_receipts WHERE workspace_id=?", (wid_src,))
        c.execute("DELETE FROM receipt_chain_heads WHERE workspace_id=?", (wid_src,))

        # The source workspace is now empty — drop it.
        c.execute("DELETE FROM graph_index_state WHERE workspace_id=?", (wid_src,))
        c.execute("DELETE FROM workspaces WHERE id=?", (wid_src,))
        self.store.audit(actor, "workspace_merge", wid_dst, f"{src} ({int(n_mem)} memories) -> {dst}")
        c.commit()
        return {"source": src, "target": dst, "memories_moved": int(n_mem), "id": wid_dst}

    def _next_copy_name(self, base: str) -> str:
        """Auto-name a workspace copy: ``"foo" -> "foo copy" -> "foo copy 2" -> ...``.
        Only letters/digits/space/``._-/`` are ever emitted, so the result always
        satisfies ``_NAME_RE`` without needing to run back through ``_clean_name``."""
        n = 1
        while True:
            suffix = " copy" if n == 1 else f" copy {n}"
            candidate = base + suffix
            if len(candidate) > MAX_NAME_CHARS:
                candidate = base[: MAX_NAME_CHARS - len(suffix)] + suffix
            if self._lookup_workspace(candidate) is None:
                return candidate
            n += 1

    @_rollback_service_transaction
    def copy_workspace(self, source: str, new_name: Optional[str] = None, *,
                       actor: str = "user") -> dict:
        """Duplicate ``source`` into a brand-new workspace: repos (+ their code graph),
        entities, edges, memories (with vectors, full-text and cross-memory links),
        source-import manifests, and sessions/events are all cloned under fresh ids,
        leaving ``source`` untouched.
        This is the copy counterpart to ``merge_workspaces`` — merge moves rows in place
        (ids survive), copy inserts parallel rows with new ids so the two workspaces are
        fully independent afterwards (editing the copy never touches the original).
        When ``new_name`` is omitted — the dashboard's one-click "Copy" button never
        prompts — the name is auto-generated off ``source`` (``_next_copy_name``) so the
        copy never collides with an existing workspace."""
        src = self._clean_ws(source)
        self._authorize_workspace_control(src)
        wid_src = self._lookup_workspace(src)
        if wid_src is None:
            raise ValidationError(f"no workspace named '{src}' yet")
        self._assert_no_active_graph_job(wid_src)
        if new_name:
            dst = _clean_name(new_name, field="new_name")
            if self._lookup_workspace(dst) is not None:
                raise ValidationError(f"a workspace named '{dst}' already exists")
        else:
            dst = self._next_copy_name(src)
        dst = self._authorize_workspace(dst)
        actor = _clean_text(actor, field="actor", max_chars=MAX_NAME_CHARS, required=False) or "user"

        from engraphis.core import ids
        import time as _time
        ts = _time.time()
        c = self.store.conn
        wid_dst = ids.new_id("workspace")
        src_row = c.execute("SELECT settings FROM workspaces WHERE id=?", (wid_src,)).fetchone()
        c.execute("INSERT INTO workspaces(id, name, created_at, settings) VALUES (?,?,?,?)",
                 (wid_dst, dst, ts, src_row["settings"] if src_row else "{}"))

        # 1) Repos, cloned with fresh ids — plus their code graph (symbols/code_edges),
        #    which (unlike merge's non-colliding case) must be remapped since the repo
        #    id itself changes.
        repo_remap: dict = {}
        symbol_remap: dict = {}
        for r in [dict(x) for x in c.execute(
                "SELECT * FROM repos WHERE workspace_id=?", (wid_src,))]:
            nrid = ids.new_id("repo")
            repo_remap[r["id"]] = nrid
            c.execute(
                "INSERT INTO repos(id, workspace_id, name, root_path, vcs_remote, primary_lang, "
                "created_at, indexed_at, settings) VALUES (?,?,?,?,?,?,?,?,?)",
                (nrid, wid_dst, r["name"], r["root_path"], r["vcs_remote"], r["primary_lang"],
                 ts, r["indexed_at"], r["settings"]))
            for code_file in [dict(x) for x in c.execute(
                    "SELECT * FROM code_files WHERE repo_id=?", (r["id"],))]:
                c.execute(
                    "INSERT INTO code_files(repo_id, file, lang, content_hash, size_bytes, "
                    "mtime_ns, backend, indexed_at) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        nrid, code_file["file"], code_file["lang"],
                        code_file["content_hash"], code_file["size_bytes"],
                        code_file["mtime_ns"], code_file["backend"],
                        code_file["indexed_at"],
                    ),
                )
            for s in [dict(x) for x in c.execute(
                    "SELECT * FROM symbols WHERE repo_id=?", (r["id"],))]:
                nsid = ids.new_id("symbol")
                symbol_remap[s["id"]] = nsid
                c.execute(
                    "INSERT INTO symbols(id, repo_id, kind, name, fqname, file, span, signature, "
                    "docstring, lang, exported, content_hash, embedding_ref, updated_at, "
                    "valid_from, valid_to, valid_to_recorded_at, ingested_at, expired_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (nsid, nrid, s["kind"], s["name"], s["fqname"], s["file"], s["span"],
                     s["signature"], s["docstring"], s["lang"], s["exported"],
                     s["content_hash"], s["embedding_ref"], s["updated_at"],
                     s["valid_from"], s["valid_to"], s["valid_to_recorded_at"],
                     s["ingested_at"], s["expired_at"]))
            for ce in [dict(x) for x in c.execute(
                    "SELECT * FROM code_edges WHERE repo_id=?", (r["id"],))]:
                c.execute(
                    "INSERT INTO code_edges(id, repo_id, src, dst, relation, layer, file, line, "
                    "valid_from, valid_to, valid_to_recorded_at, ingested_at, expired_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (ids.new_id("edge"), nrid, symbol_remap.get(ce["src"], ce["src"]),
                     symbol_remap.get(ce["dst"], ce["dst"]), ce["relation"],
                     ce["layer"] or "entity",
                     ce["file"], ce["line"], ce["valid_from"], ce["valid_to"],
                     ce["valid_to_recorded_at"], ce["ingested_at"], ce["expired_at"]))

        def _new_repo(old_repo_id):
            return repo_remap.get(old_repo_id, old_repo_id) if old_repo_id is not None else None

        # 2) Entities, cloned with fresh ids.
        source_entities = [dict(x) for x in c.execute(
            "SELECT * FROM entities WHERE workspace_id=?", (wid_src,)
        )]
        entity_remap: dict = {
            entity["id"]: ids.new_id("entity") for entity in source_entities
        }
        for e in source_entities:
            neid = entity_remap[e["id"]]
            old_canonical_id = e.get("canonical_id")
            canonical_id = entity_remap.get(old_canonical_id, neid)
            canonical_method = (
                (e.get("canonical_method") or "identity")
                if old_canonical_id in entity_remap else "identity"
            )
            c.execute(
                "INSERT INTO entities(id, workspace_id, repo_id, name, etype, canonical_id, "
                "normalized_name, canonical_method, canonical_confidence, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (neid, wid_dst, _new_repo(e["repo_id"]), e["name"], e["etype"],
                 canonical_id, e.get("normalized_name") or normalize_entity_name(e["name"]),
                 canonical_method,
                 e.get("canonical_confidence") or 1.0, ts))

        # 3) Entity-graph edges, remapped onto the cloned entities/repos.
        source_edges = [dict(x) for x in c.execute(
            "SELECT * FROM edges WHERE workspace_id=?", (wid_src,)
        )]
        edge_remap: dict = {}
        for ed in source_edges:
            new_edge_id = ids.new_id("edge")
            edge_remap[ed["id"]] = new_edge_id
            c.execute(
                "INSERT INTO edges(id, workspace_id, repo_id, src, dst, relation, layer, "
                "weight, valid_from, valid_to, valid_to_recorded_at, ingested_at, "
                "expired_at, provenance) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (new_edge_id, wid_dst, _new_repo(ed["repo_id"]),
                 entity_remap.get(ed["src"], ed["src"]), entity_remap.get(ed["dst"], ed["dst"]),
                 ed["relation"], ed["layer"] or "semantic", ed["weight"],
                 ed["valid_from"], ed["valid_to"], ed["valid_to_recorded_at"], ed["ingested_at"],
                 ed["expired_at"], ed["provenance"]))

        # 4) Sessions, cloned with fresh ids (memories/events below repoint at these).
        session_remap: dict = {}
        for s in [dict(x) for x in c.execute(
                "SELECT * FROM sessions WHERE workspace_id=?", (wid_src,))]:
            nsid = ids.new_id("session")
            session_remap[s["id"]] = nsid
            c.execute(
                "INSERT INTO sessions(id, workspace_id, repo_id, agent, user_id, goal, status, "
                "started_at, ended_at, summary, open_threads, outcome) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (nsid, wid_dst, _new_repo(s["repo_id"]), s["agent"], s["user_id"], s["goal"],
                 s["status"], s["started_at"], s["ended_at"], s["summary"], s["open_threads"],
                 s["outcome"]))

        # 5) Memories, cloned with fresh ids — plus their full-text and vector mirrors,
        #    which key off the memory id and so need the same new id.
        source_memories = [dict(x) for x in c.execute(
            "SELECT * FROM memories WHERE workspace_id=?", (wid_src,)
        )]
        memory_remap = {
            memory["id"]: ids.new_id("memory") for memory in source_memories
        }
        source_vaults = [dict(row) for row in c.execute(
            "SELECT * FROM source_vaults WHERE workspace_id=? ORDER BY id",
            (wid_src,),
        )]
        vault_remap = {
            vault["id"]: ids.new_id("vault") for vault in source_vaults
        }
        source_imports = [dict(row) for row in c.execute(
            "SELECT source_imports.* FROM source_imports "
            "JOIN source_vaults ON source_vaults.id=source_imports.vault_id "
            "WHERE source_vaults.workspace_id=? ORDER BY source_imports.id",
            (wid_src,),
        )]
        source_import_remap = {
            item["id"]: ids.new_id("source") for item in source_imports
        }
        copy_reference_remap = {
            **memory_remap, **vault_remap, **source_import_remap,
        }

        # Pre-compute the sorted replacement pairs once. The old code re-sorted the
        # full remap dict on every invocation (tens of thousands of times for large
        # workspaces). Use word-boundary-anchored regex to prevent substring false
        # positives inside hashes, paths, or base64 blobs.
        _sorted_copy_pairs = sorted(
            copy_reference_remap.items(),
            key=lambda pair: len(str(pair[0])), reverse=True,
        )
        _copy_pattern = re.compile(
            r"\b((?:ws|repo|ses|mem|ent|edg|sym|evt|job|aud|dev|rcpt|vlt|src)_[A-Za-z0-9_-]+)\b"
        )
        _copy_lookup = {str(old): str(new) for old, new in _sorted_copy_pairs}

        def _remap_copy_text(raw: Any) -> str:
            return _copy_pattern.sub(
                lambda match: _copy_lookup.get(match.group(1), match.group(0)),
                str(raw or ""),
            )

        def _remap_json_memory_ids(raw):
            try:
                value = json.loads(raw or "{}")
            except (TypeError, ValueError):
                return raw

            def walk(item):
                if isinstance(item, dict):
                    remapped = {}
                    for key, child in item.items():
                        if key in ("memory_id", "corrects"):
                            replacement = memory_remap.get(str(child or ""))
                            if replacement:
                                remapped[key] = replacement
                            continue
                        if key in ("memory_ids", "supersedes") and isinstance(child, list):
                            replacements = [
                                memory_remap[str(old)] for old in child
                                if str(old) in memory_remap
                            ]
                            if replacements:
                                remapped[key] = list(dict.fromkeys(replacements))
                            continue
                        remapped[key] = walk(child)
                    return remapped
                if isinstance(item, list):
                    return [walk(child) for child in item]
                if isinstance(item, str):
                    return _remap_copy_text(item)
                return item

            return json.dumps(walk(value), ensure_ascii=False, separators=(",", ":"))

        def _remap_memory_ids_in_text(raw: Any) -> str:
            return _remap_copy_text(raw)

        for m in source_memories:
            # This historical row may predate capture-time secret blocking. Never
            # replicate it into another workspace through this raw SQL copy path.
            _reject_secret_capture((("title", m.get("title")), ("content", m.get("content")),
                                    ("summary", m.get("summary")),
                                    ("keywords", m.get("keywords")),
                                    ("metadata", m.get("metadata")),
                                    ("provenance", m.get("provenance"))))
            nmid = memory_remap[m["id"]]
            c.execute(
                "INSERT INTO memories (id, workspace_id, repo_id, session_id, scope, mtype, "
                "title, content, summary, keywords, metadata, importance, surprise, stability, "
                "confidence, access_count, last_access, valid_from, valid_to, "
                "valid_to_recorded_at, "
                "ingested_at, expired_at, subject_key, claim_kind, pinned, sensitivity, "
                "provenance, sort_order) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (nmid, wid_dst, _new_repo(m["repo_id"]), session_remap.get(m["session_id"]),
                 m["scope"], m["mtype"], m["title"], m["content"], m["summary"], m["keywords"],
                 _remap_json_memory_ids(m["metadata"]), m["importance"],
                 m["surprise"], m["stability"], m["confidence"],
                 m["access_count"], m["last_access"], m["valid_from"], m["valid_to"],
                 m["valid_to_recorded_at"], m["ingested_at"], m["expired_at"],
                 _remap_copy_text(m["subject_key"]), m["claim_kind"], m["pinned"], m["sensitivity"],
                 _remap_json_memory_ids(m["provenance"]), m["sort_order"]))
            fts_row = c.execute(
                "SELECT title, content, keywords FROM mem_fts WHERE id=?", (m["id"],)).fetchone()
            if fts_row:
                c.execute("INSERT INTO mem_fts(id, title, content, keywords) VALUES (?,?,?,?)",
                         (nmid, fts_row["title"], fts_row["content"], fts_row["keywords"]))
            vec_row = c.execute(
                "SELECT dim, vector, model FROM mem_vectors WHERE id=?", (m["id"],)).fetchone()
            if vec_row:
                c.execute("INSERT INTO mem_vectors(id, dim, vector, model) VALUES (?,?,?,?)",
                         (nmid, vec_row["dim"], vec_row["vector"], vec_row["model"]))
            try:
                ann_row = c.execute(
                    "SELECT embedding FROM mem_vec_ann WHERE id=?", (m["id"],)).fetchone()
                if ann_row:
                    c.execute("INSERT INTO mem_vec_ann(id, embedding) VALUES (?,?)",
                             (nmid, ann_row["embedding"]))
            except Exception:
                pass  # sqlite-vec vector table only present when that backend is active

        # 6) Cross-memory links where *both* endpoints were copied — a link to a memory
        #    outside this workspace can't be meaningfully cloned, so those are dropped.
        # Remap legacy provenance and normalized evidence only after memory ids exist.
        # Opaque canonical/support ids never cross the workspace boundary unchanged.
        for source_edge in source_edges:
            new_edge_id = edge_remap[source_edge["id"]]
            edge_provenance = _remap_json_memory_ids(
                source_edge.get("provenance") or "{}"
            )
            c.execute(
                "UPDATE edges SET provenance=? WHERE id=?",
                (edge_provenance, new_edge_id),
            )
            source_supports = [dict(row) for row in c.execute(
                "SELECT * FROM edge_supports WHERE edge_id=? ORDER BY id",
                (source_edge["id"],),
            )]
            for support in source_supports:
                new_memory_id = memory_remap.get(support["memory_id"])
                if new_memory_id is None:
                    continue
                support_provenance = _remap_json_memory_ids(
                    support.get("provenance") or "{}"
                )
                c.execute(
                    "INSERT INTO edge_supports(edge_id, memory_id, source_kind, confidence, "
                    "valid_from, valid_to, valid_to_recorded_at, ingested_at, "
                    "expired_at, provenance) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (new_edge_id, new_memory_id, support["source_kind"],
                     support["confidence"], support["valid_from"], support["valid_to"],
                     support["valid_to_recorded_at"], support["ingested_at"],
                     support["expired_at"], support_provenance),
                )
            if not source_supports:
                try:
                    fallback_provenance = json.loads(edge_provenance or "{}")
                except (TypeError, ValueError):
                    fallback_provenance = {}
                if isinstance(fallback_provenance, dict):
                    self.store._write_edge_supports(
                        new_edge_id, source_edge["relation"], fallback_provenance,
                        valid_from=source_edge["valid_from"],
                        valid_to=source_edge["valid_to"],
                        valid_to_recorded_at=source_edge["valid_to_recorded_at"],
                        ingested_at=source_edge["ingested_at"],
                        expired_at=source_edge["expired_at"],
                    )

        # Persisted sparse memory↔entity incidence is a first-class retrieval index,
        # not disposable cache. Clone it only after both endpoint maps exist so the
        # copied workspace has graph-recall parity without retaining source ids.
        for incidence in [dict(row) for row in c.execute(
                "SELECT * FROM memory_entities WHERE workspace_id=? ORDER BY id",
                (wid_src,),
        )]:
            new_memory_id = memory_remap.get(incidence["memory_id"])
            new_entity_id = entity_remap.get(incidence["entity_id"])
            if new_memory_id is None or new_entity_id is None:
                continue
            c.execute(
                "INSERT INTO memory_entities("
                "id, memory_id, entity_id, workspace_id, repo_id, source_kind, confidence, "
                "valid_from, valid_to, valid_to_recorded_at, ingested_at, expired_at, "
                "provenance) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    ids.new_id("edge"), new_memory_id, new_entity_id, wid_dst,
                    _new_repo(incidence["repo_id"]), incidence["source_kind"],
                    incidence["confidence"], incidence["valid_from"],
                    incidence["valid_to"], incidence["valid_to_recorded_at"],
                    incidence["ingested_at"], incidence["expired_at"],
                    _remap_json_memory_ids(incidence["provenance"]),
                ),
            )

        if memory_remap:
            old_ids = list(memory_remap.keys())
            marks = ",".join("?" for _ in old_ids)
            for ln in [dict(x) for x in c.execute(
                    f"SELECT a, b, relation, layer, reason, created_at, valid_from, valid_to, "
                    f"valid_to_recorded_at, ingested_at, expired_at FROM mem_links "
                    f"WHERE a IN ({marks}) AND b IN ({marks})", old_ids + old_ids)]:
                c.execute(
                    "INSERT INTO mem_links("
                    "a, b, relation, layer, reason, created_at, valid_from, valid_to, "
                    "valid_to_recorded_at, ingested_at, expired_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        memory_remap[ln["a"]], memory_remap[ln["b"]],
                        ln["relation"], ln["layer"],
                        _remap_memory_ids_in_text(ln["reason"]), ln["created_at"],
                        ln["valid_from"], ln["valid_to"], ln["valid_to_recorded_at"],
                        ln["ingested_at"], ln["expired_at"],
                    ),
                )

        # 7) Code↔memory bridges, after both endpoint remaps are complete.
        if repo_remap and symbol_remap and memory_remap:
            old_repo_ids = list(repo_remap)
            marks = ",".join("?" for _ in old_repo_ids)
            for link in [dict(x) for x in c.execute(
                    f"SELECT * FROM code_memory_links WHERE repo_id IN ({marks})",
                    old_repo_ids,
            )]:
                new_symbol = symbol_remap.get(link["symbol_id"])
                new_memory = memory_remap.get(link["memory_id"])
                if new_symbol is None or new_memory is None:
                    continue
                c.execute(
                    "INSERT INTO code_memory_links(id, repo_id, symbol_id, memory_id, "
                    "relation, confidence, created_at, valid_from, valid_to, "
                    "valid_to_recorded_at, ingested_at, expired_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        ids.new_id("edge"), repo_remap[link["repo_id"]],
                        new_symbol, new_memory, link["relation"],
                        link["confidence"], link["created_at"], link["valid_from"],
                        link["valid_to"], link["valid_to_recorded_at"],
                        link["ingested_at"], link["expired_at"],
                    ),
                )

        # 8) Events, cloned with fresh ids.
        for ev in [dict(x) for x in c.execute(
                "SELECT * FROM events WHERE workspace_id=?", (wid_src,))]:
            _reject_secret_capture((("event content", ev.get("content")),
                                    ("event refs", ev.get("refs"))))
            c.execute(
                "INSERT INTO events(id, workspace_id, repo_id, session_id, kind, content, refs, "
                "interaction_level, ts) VALUES (?,?,?,?,?,?,?,?,?)",
                (ids.new_id("event"), wid_dst, _new_repo(ev["repo_id"]),
                 session_remap.get(ev["session_id"]), ev["kind"], ev["content"],
                 _remap_json_memory_ids(ev["refs"]),
                 ev["interaction_level"], ev["ts"]))

        # 9) Source-import identities and current per-document manifests. Job history
        # is process-local and intentionally omitted from workspace copies, but the
        # manifest itself must follow the copied memories so a later local re-import
        # remains idempotent. Clear last_seen_job_id because the referenced job is not
        # cloned; the next import creates a fresh job and refreshes that field.
        for vault in source_vaults:
            c.execute(
                "INSERT INTO source_vaults("
                "id, kind, root_digest, display_name, workspace_id, repo_id, session_id, "
                "scope, memory_type, importer_version, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    vault_remap[vault["id"]], vault["kind"], vault["root_digest"],
                    vault["display_name"], wid_dst, _new_repo(vault.get("repo_id")),
                    session_remap.get(vault.get("session_id")),
                    vault["scope"], vault["memory_type"], vault["importer_version"],
                    vault["created_at"], vault["updated_at"],
                ),
            )
        for item in source_imports:
            new_source_id = source_import_remap[item["id"]]
            old_memory_id = item.get("memory_id")
            c.execute(
                "INSERT INTO source_imports("
                "id, vault_id, source_key, relative_path, memory_id, subject_key, "
                "content_sha256, canonical_sha256, file_size, file_mtime_ns, "
                "importer_version, last_seen_job_id, state, first_imported_at, "
                "last_imported_at, last_seen_at, missing_at, last_error) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    new_source_id, vault_remap[item["vault_id"]], item["source_key"],
                    item["relative_path"], memory_remap.get(old_memory_id),
                    _remap_copy_text(item["subject_key"]),
                    item["content_sha256"], item["canonical_sha256"], item["file_size"],
                    item["file_mtime_ns"], item["importer_version"], None, item["state"],
                    item["first_imported_at"], item["last_imported_at"],
                    item["last_seen_at"], item["missing_at"], item["last_error"],
                ),
            )

        self.store.audit(actor, "workspace_copy", wid_dst,
                         f"{src} -> {dst} ({len(memory_remap)} memories)")
        c.commit()
        return {"source": src, "workspace": dst, "id": wid_dst,
               "memories_copied": len(memory_remap),
               "sources_copied": len(source_vaults)}

    def update_memory(
        self, memory_id: str, *, workspace: str, repo: Optional[str] = None,
        title: Optional[str] = None, mtype: Optional[str] = None,
        importance: Optional[float] = None, actor: str = "user",
    ) -> dict:
        """Update changed metadata fields; an identical supplied retry is a true no-op."""
        mid = _clean_text(memory_id, field="memory_id", max_chars=MAX_NAME_CHARS)
        actor = (
            _clean_text(
                actor, field="actor", max_chars=MAX_NAME_CHARS, required=False
            )
            or "user"
        )
        wid, rid = self._require_scope(workspace, repo)
        self._check_owns(mid, wid, rid)
        existing = self.store.get_memory(mid)
        if title is None and mtype is None and importance is None:
            raise ValidationError("nothing to update")
        if title is not None:
            title = _clean_text(
                title, field="title", max_chars=MAX_TITLE_CHARS, required=False
            )
            _reject_secret_capture((("title", title),))
        if mtype is not None:
            mtype = _enum(mtype, MemoryType, "memory_type").value
        if importance is not None:
            try:
                importance = float(importance)
            except (TypeError, ValueError, OverflowError):
                raise ValidationError("importance must be a number")
            if not math.isfinite(importance):
                raise ValidationError("importance must be finite")
            importance = max(0.0, min(1.0, importance))
        # Check if FTS row exists; if title is provided but FTS is missing, rebuild it
        fts_row = self.store.conn.execute(
            "SELECT 1 FROM mem_fts WHERE id=?", (mid,)
        ).fetchone()
        needs_fts_rebuild = title is not None and fts_row is None

        if (
            not needs_fts_rebuild
            and (title is None or title == existing.title)
            and (mtype is None or mtype == existing.mtype.value)
            and (importance is None or importance == existing.importance)
        ):
            return {"id": mid, "updated": []}
        result, external_index_action = self._update_memory_transactional(
            mid,
            workspace=workspace,
            repo=repo,
            title=title,
            mtype=mtype,
            importance=importance,
            actor=actor,
        )
        if external_index_action is not None:
            self._publish_memory_index_action(external_index_action)
        return result

    def _publish_memory_index_action(
        self, action: tuple[str, str, Optional[np.ndarray], str],
    ) -> None:
        """Publish one committed title edit to a separately-backed vector index.

        The Store row/vector/FTS state is canonical and has already committed when this
        runs.  A provider failure therefore becomes explicit repair debt; it must never
        be raised as though the canonical edit had rolled back.
        """
        operation, memory_id, vector, model = action
        try:
            if operation == "delete":
                self.engine.index.delete([memory_id])
            elif operation == "upsert" and vector is not None:
                self.engine.index.upsert(
                    [memory_id], vector.reshape(1, -1), [{"model": model}],
                )
            else:  # pragma: no cover - action is constructed locally
                raise RuntimeError("invalid deferred vector-index action")
        except Exception as exc:  # noqa: BLE001 - canonical Store state is committed
            failure_type = type(exc).__name__
            logger.warning(
                "vector-index %s failed for title update %s (%s)",
                operation, memory_id, failure_type,
            )
            try:
                self.store.audit(
                    "engine", f"index_{operation}_failed", memory_id,
                    f"failure_type={failure_type}",
                    commit=not self.store.conn.transaction_owned_by_current_thread(),
                )
            except Exception as audit_exc:  # noqa: BLE001 - retain original repair debt
                logger.warning(
                    "could not audit title-update vector-index failure (%s)",
                    type(audit_exc).__name__,
                )

    @_rollback_service_transaction
    def _update_memory_transactional(
            self, memory_id: str, *, workspace: str, repo: Optional[str] = None,
                      title: Optional[str] = None, mtype: Optional[str] = None,
                      importance: Optional[float] = None,
                      actor: str = "user") -> tuple[
                          dict, Optional[tuple[str, str, Optional[np.ndarray], str]]
                      ]:
        """In-place edit of a memory's metadata fields. Content edits go through
        ``correct`` so bi-temporal history is preserved."""
        mid = _clean_text(memory_id, field="memory_id", max_chars=MAX_NAME_CHARS)
        actor = _clean_text(actor, field="actor", max_chars=MAX_NAME_CHARS, required=False) or "user"
        wid, rid = self._require_scope(workspace, repo)
        self._check_owns(mid, wid, rid)
        existing = self.store.get_memory(mid)
        old_title = existing.title if existing is not None else ""
        sets, params, changes = [], [], []
        title_changed = False
        external_index_action = None
        if title is not None:
            title = _clean_text(title, field="title", max_chars=MAX_TITLE_CHARS, required=False)
            _reject_secret_capture((("title", title),))
            title_changed = title != old_title
            if title_changed:
                sets.append("title=?")
                params.append(title)
                changes.append("title")
        if mtype is not None:
            mt = _enum(mtype, MemoryType, "memory_type").value
            if mt != existing.mtype.value:
                sets.append("mtype=?")
                params.append(mt)
                changes.append(f"type={mt}")
        if importance is not None:
            try:
                importance = float(importance)
            except (TypeError, ValueError, OverflowError):
                raise ValidationError("importance must be a number")
            if not math.isfinite(importance):
                raise ValidationError("importance must be finite")
            importance = max(0.0, min(1.0, importance))
            if importance != existing.importance:
                sets.append("importance=?")
                params.append(importance)
                changes.append("importance")
        if not sets and title is None:
            return {"id": mid, "updated": []}, None
        if sets:
            self.store.advance_memory_modified_hlc(mid, commit=False)
            params.append(mid)
            self.store.conn.execute(f"UPDATE memories SET {', '.join(sets)} WHERE id=?", params)
        if title is not None:
            row = self.store.conn.execute(
                "SELECT title, content, keywords FROM memories WHERE id=?", (mid,)).fetchone()
            kw = row["keywords"] or ""
            try:
                kw = " ".join(json.loads(kw)) if kw.strip().startswith("[") else kw
            except Exception:
                pass
            if title_changed:
                text = f"{row['title']}\n{row['content']}" if row["title"] else row["content"]
                # Quarantined records and explicitly secret records are retained for
                # local governance only. A metadata edit must not turn either into a
                # semantic candidate or send its payload to an embedder.
                if (
                    existing.sensitivity == "secret"
                    or not inspection_eligible(existing.provenance, existing.metadata)
                ):
                    self.store.conn.execute("DELETE FROM mem_vectors WHERE id=?", (mid,))
                    if vector_index_requires_sync(self.engine.index, self.store):
                        if vector_index_shares_store_transaction(
                            self.engine.index, self.store,
                        ):
                            self.engine.index.delete([mid], commit=False)
                        else:
                            external_index_action = ("delete", mid, None, "")
                else:
                    # Existing rows may predate the write-path secret guard.  Do not send
                    # such content to a remote embedder while changing unrelated metadata.
                    _reject_secret_capture((("content", row["content"]),))
                    model = self.engine.embedding_space
                    persistent_store = not _is_memory_database_path(self.store.path)
                    if persistent_store and (
                        not model or not self.store.embedding_space_ready(model)
                    ):
                        raise ValidationError(
                            "the configured embedding space is not active; restart "
                            "Engraphis to complete the guarded rebuild"
                        )
                    try:
                        vectors = np.asarray(
                            self.engine.embedder.embed([text]), dtype=np.float32,
                        )
                    except (TypeError, ValueError, OverflowError) as exc:
                        raise ValidationError("embedder returned an invalid vector") from exc
                    expected_dim = int(
                        getattr(self.engine.embedder, "dim",
                                getattr(self.engine.index, "dim", 0)) or 0
                    )
                    if (
                        vectors.ndim != 2
                        or vectors.shape != (1, expected_dim)
                        or not np.isfinite(vectors).all()
                    ):
                        raise ValidationError("embedder returned an invalid vector")
                    if vector_index_requires_sync(self.engine.index, self.store):
                        if vector_index_shares_store_transaction(
                            self.engine.index, self.store,
                        ):
                            self.engine.index.upsert(
                                [mid], vectors, [{"model": model}], commit=False
                            )
                        else:
                            external_index_action = (
                                "upsert", mid, vectors[0].copy(), model,
                            )
                    # Store owns the portable mirror for every backend. A separate
                    # index is published only after this transaction commits; NumPy
                    # searches this canonical row directly.
                    self.store.put_vector(mid, vectors[0], model=model)
            self.store._fts_upsert(
                mid, row["title"] or "", row["content"] or "", kw,
            )

        self.store.audit(actor, "memory_update", mid, "; ".join(changes))
        self.store.conn.commit()
        return {"id": mid, "updated": changes}, external_index_action
    @_rollback_service_transaction
    def reorder_memories(self, ids: list, *, workspace: str, repo: Optional[str] = None,
                         actor: str = "user") -> dict:
        """Persist a manual display order for the Memories tab's drag-to-reorder UI.
        Takes the full new top-to-bottom id order and assigns each a ``sort_order``
        (0, 1, 2, ...); ``routes.v2_api.memories`` sorts by it when present, falling
        back to recency for memories that have never been dragged (``sort_order``
        stays ``NULL`` until touched). Every id must already belong to this
        workspace/repo — the same ownership check every other governance tool uses
        (``_check_owns``), so a client can't smuggle in ids from elsewhere to reorder
        them."""
        wid, rid = self._require_scope(workspace, repo)
        if not isinstance(ids, (list, tuple)) or not ids:
            raise ValidationError("ids must be a non-empty list")
        if len(ids) > 1000:
            raise ValidationError("too many ids (max 1000)")
        actor = _clean_text(actor, field="actor", max_chars=MAX_NAME_CHARS, required=False) or "user"
        clean_ids = [_clean_text(i, field="id", max_chars=MAX_NAME_CHARS) for i in ids]
        for mid in clean_ids:
            self._check_owns(mid, wid, rid)
        c = self.store.conn
        c.executemany("UPDATE memories SET sort_order=? WHERE id=?",
                      [(float(i), mid) for i, mid in enumerate(clean_ids)])
        self.store.audit(actor, "memory_reorder", wid, f"{len(clean_ids)} memories")
        c.commit()
        return {"workspace": workspace, "reordered": len(clean_ids)}

    def inspect(self, memory_id: str, *, workspace: str, repo: Optional[str] = None) -> dict:
        """Everything the inspector shows for one memory: the record, its links, its
        audit trail, and the full supersession chain (oldest→newest) reconstructed from
        the ``supersedes``/``corrects`` pointers the write path records."""
        mid = _clean_text(memory_id, field="memory_id", max_chars=MAX_NAME_CHARS)
        wid, rid = self._require_scope(workspace, repo)
        self._check_owns(mid, wid, rid)
        rec = self.store.get_memory(mid)
        links = []
        for link in self.store.get_links(mid):
            other_id = link["b"] if link["a"] == mid else link["a"]
            other = self.store.get_memory(other_id)
            if (other is None or other.workspace_id != wid
                    or (rid is not None and other.repo_id != rid)
                    or not self._memory_visible_to_caller(other)):
                continue
            links.append({"id": other_id, "relation": link["relation"],
                          "layer": link.get("layer") or "semantic",
                          "reason": link.get("reason") or "",
                          "title": (other.title or other.content[:80]) if other else "?",
                          "live": bool(other and other.expired_at is None and
                                       other.valid_to is None)})
        audit = [dict(r) for r in self.store.conn.execute(
            "SELECT ts, actor, action, detail FROM audit WHERE target=? ORDER BY ts", (mid,))]
        chain = [self._chain_entry(r, wid) for r in self._chain_for(rec, wid)]
        return {"memory": _mem_to_dict(rec), "links": links, "audit": audit,
                "chain": chain}

    def conflict_review(self, *, workspace: str, repo: Optional[str] = None,
                        limit: int = 50) -> dict:
        """Return a scope-authorized review inbox without exposing untrusted bodies."""
        wid, rid = self._require_scope(workspace, repo)
        try:
            limit = int(limit)
        except (TypeError, ValueError, OverflowError):
            raise ValidationError("limit must be an integer") from None
        limit = max(1, min(100, limit))
        params: list[Any] = [wid]
        sql = (
            "SELECT id, title, content, metadata, provenance, workspace_id, repo_id, "
            "scope, session_id, ingested_at FROM memories WHERE workspace_id=? "
        )
        if rid is not None:
            sql += "AND repo_id=? "
            params.append(rid)
        items = []
        batch_size = max(100, limit * 4)
        scanned = 0
        truncated = False
        cursor_id = None
        cursor_ingested = None
        session_visibility: dict[tuple[str, Optional[str]], bool] = {}
        while len(items) < limit and scanned < CONFLICT_REVIEW_SCAN_LIMIT:
            batch_params = [*params]
            if cursor_id is None:
                batch_sql = sql
            elif cursor_ingested is None:
                batch_sql = sql + "AND ingested_at IS NULL AND id < ? "
                batch_params.append(cursor_id)
            else:
                batch_sql = sql + (
                    "AND (ingested_at IS NULL OR ingested_at < ? "
                    "OR (ingested_at = ? AND id < ?)) "
                )
                batch_params.extend([cursor_ingested, cursor_ingested, cursor_id])
            requested_batch_size = min(batch_size, CONFLICT_REVIEW_SCAN_LIMIT - scanned)
            batch_sql += (
                "ORDER BY CASE WHEN ingested_at IS NULL THEN 1 ELSE 0 END, "
                "ingested_at DESC, id DESC LIMIT ?"
            )
            rows = self.store.conn.execute(
                batch_sql, [*batch_params, requested_batch_size]).fetchall()
            if not rows:
                break
            scanned += len(rows)
            last = rows[-1]
            cursor_id = last["id"]
            cursor_ingested = last["ingested_at"]
            for row in rows:
                # The raw SQL scope is not enough for session records: an inbox is a
                # shared workspace surface, so enforce the same caller/session
                # authorization used by recall and inspection before exposing even an
                # id, state, or metadata-derived conflict marker. Cache the decision
                # because many memories can belong to one session.
                row_scope = str(row["scope"] or Scope.WORKSPACE.value)
                if row_scope not in (
                        Scope.SESSION.value, Scope.REPO.value,
                        Scope.WORKSPACE.value, Scope.USER.value):
                    continue
                if row_scope == Scope.SESSION.value:
                    sid = str(row["session_id"] or "")
                    if not sid:
                        continue
                    visibility_key = (sid, row["repo_id"])
                    visible = session_visibility.get(visibility_key)
                    if visible is None:
                        session = self.store.get_session(sid)
                        visible = bool(
                            session
                            and session.get("workspace_id") == wid
                            and session.get("repo_id") == row["repo_id"]
                        )
                        if visible:
                            try:
                                self._authorize_session(session)
                            except ValidationError:
                                visible = False
                        session_visibility[visibility_key] = visible
                    if not visible:
                        continue
                provenance = _loads(row["provenance"], {})
                provenance = provenance if isinstance(provenance, dict) else {}
                metadata = _loads(row["metadata"], {})
                metadata = metadata if isinstance(metadata, dict) else {}
                review_state = provenance.get("review_state") or ""
                quarantined = bool(metadata.get("quarantine") or provenance.get("quarantined"))
                conflicted = bool(metadata.get("conflict_with"))
                if not (quarantined or review_state == REVIEW_PENDING or conflicted):
                    continue
                # Pending/quarantined content is evidence for a human reviewer, not model
                # context. Return only an excerpt for already-approved conflict records.
                excerpt = ""
                if prompt_eligible(provenance, metadata):
                    excerpt = (row["content"] or row["title"] or "")[:200]
                items.append({
                    "id": row["id"],
                    "review_state": review_state,
                    "quarantined": quarantined,
                    "conflict_with": metadata.get("conflict_with") if conflicted else None,
                    "excerpt": excerpt,
                })
                if len(items) >= limit:
                    break
            if len(rows) < requested_batch_size:
                break
        if len(items) < limit and scanned >= CONFLICT_REVIEW_SCAN_LIMIT:
            truncated = True
        return {"workspace": workspace, "items": items, "count": len(items),
                "truncated": truncated}

    def _chain_entry(self, rec, wid: str) -> dict:
        d = _mem_to_dict(rec)
        d["stability"] = rec.stability
        d["access_count"] = rec.access_count
        rows = self.store.conn.execute(
            "SELECT ts, actor, action, detail FROM audit WHERE target=? "
            "AND action IN ('invalidate','noop','evolve') ORDER BY ts", (rec.id,)).fetchall()
        d["events"] = [dict(r) for r in rows]
        return d

    def _chain_for(self, rec, wid: str) -> list:
        """Collect the full supersession component around ``rec`` and return its
        closed history oldest→newest, followed by the live record. It follows
        ``supersedes``/``corrects`` metadata backward and matching pointers forward,
        including every predecessor of an N→1 ``merge``. A linear ``correct`` chain
        is the one-predecessor special case.

        ``wid`` is the *root* record's workspace id (``inspect()`` has already
        ``_check_owns``-verified ``rec`` belongs to it) and is the isolation boundary for
        the whole walk: ``metadata`` is caller-supplied and reaches storage intact, so a
        writer in another workspace can plant a ``supersedes``/``corrects`` pointer
        naming an id it doesn't own, or write a record that points *at* one. Every
        candidate — backward via ``get_memory(pid)``, forward via the LIKE scan below —
        is dropped unless it is itself in ``wid``, so a foreign-workspace record can
        never ride a forged pointer into this response; the walk does not continue past
        a dropped candidate (its own predecessors/successors are never visited)."""
        def predecessors(r):
            ids = list(r.metadata.get("supersedes") or [])
            if r.metadata.get("corrects"):
                ids.append(r.metadata["corrects"])
            return ids

        seen = {rec.id}
        members = {rec.id: rec}
        frontier = [rec]
        while frontier:
            cur = frontier.pop()
            for pid in predecessors(cur):
                if pid in seen:
                    continue
                seen.add(pid)
                prev = self.store.get_memory(pid)
                if (prev is not None and prev.workspace_id == wid
                        and self._memory_visible_to_caller(prev)):
                    members[pid] = prev
                    frontier.append(prev)
            while True:
                nxt = self._successor_of(cur.id, wid, seen)
                if nxt is None:
                    break
                seen.add(nxt.id)
                members[nxt.id] = nxt
                frontier.append(nxt)
        if len(members) == 1:
            return [rec]
        return sorted(members.values(), key=lambda r: (
            r.valid_to is None,
            r.valid_from or r.ingested_at or 0,
            r.valid_to if r.valid_to is not None else float("inf"),
            r.id,
        ))

    def _successor_of(self, memory_id: str, workspace_id: str, seen: set):
        escaped = memory_id.replace("%", "\\%").replace("_", "\\_")
        rows = self.store.conn.execute(
            "SELECT id, metadata FROM memories WHERE metadata LIKE ? ESCAPE '\\' "
            "AND id != ? AND workspace_id = ?",
            (f"%{escaped}%", memory_id, workspace_id)).fetchall()
        import json as _json
        for r in rows:
            if r["id"] in seen:
                continue
            try:
                meta = _json.loads(r["metadata"] or "{}")
            except ValueError:
                continue
            if memory_id in (meta.get("supersedes") or []) or meta.get("corrects") == memory_id:
                candidate = self.store.get_memory(r["id"])
                if candidate is not None and self._memory_visible_to_caller(candidate):
                    return candidate
        return None

    def audit_log(self, *, workspace: str, limit: int = 100) -> dict:
        """Recent audit entries for memories in this workspace (governance trail)."""
        wid, _ = self._require_scope(workspace, None)
        limit = max(1, min(500, int(limit)))
        rows = self.store.conn.execute(
            "SELECT a.ts, a.actor, a.action, a.target, a.detail FROM audit a "
            "JOIN memories m ON m.id = a.target WHERE m.workspace_id=? "
            "AND COALESCE(m.scope, 'workspace')!='session' "
            "ORDER BY a.ts DESC LIMIT ?", (wid, limit)).fetchall()
        return {"entries": [dict(r) for r in rows]}

    def receipt_log(self, *, workspace: str, limit: int = 100) -> dict:
        """Privacy-safe receipt-only audit view (no memory/query contents)."""
        wid, _ = self._require_scope(workspace, None)
        limit = max(1, min(10_000, int(limit)))
        entries = self.store.list_receipts(workspace_id=wid, limit=limit)
        return {
            "format": "engraphis-receipts/1",
            "workspace_digest": hashlib.sha256(wid.encode("utf-8")).hexdigest()[:24],
            "entries": entries,
        }

    def context_savings(
        self,
        *,
        workspace: Optional[str] = None,
        repo: Optional[str] = None,
        from_ts: Any = None,
        to_ts: Any = None,
        release_version: Optional[str] = None,
        format: Optional[str] = None,
        group_by: Optional[str] = None,
    ) -> dict:
        """Return receipt-backed context savings for an optional time/release window.

        Omitting ``workspace`` aggregates the complete visible local history. The summary
        contains only validated, content-free receipt counters; workspace-scoped callers
        retain the existing isolation behavior.
        """
        ws = self._clean_ws(workspace) if workspace is not None else None
        rp = _clean_name(repo, field="repo") if repo else None
        if ws is None and rp is not None:
            raise ValidationError("repo requires workspace")
        from_value = _optional_timestamp(from_ts, field="from_ts")
        to_value = _optional_timestamp(to_ts, field="to_ts")
        if from_value is not None and to_value is not None and from_value > to_value:
            raise ValidationError("from_ts must be less than or equal to to_ts")
        if release_version is not None:
            release_version = normalize_release_version(release_version)
            if not release_version:
                raise ValidationError("release_version must be a semantic version")
        if ws is None:
            wid = None
            rid = None
            visible_workspace_ids = self._visible_workspace_ids()
        else:
            wid, rid = self._require_scope(ws, rp)
            visible_workspace_ids = None
        fmt = str(format or "json").strip().casefold()
        if fmt not in ("json", "csv"):
            raise ValidationError("format must be 'json' or 'csv'")
        gb = str(group_by or "").strip().casefold() if group_by else ""
        base = {
            "format": "engraphis-context-savings/1",
            "scope": ({"workspace": ws, **({"repo": rp} if rp else {})}
                      if ws is not None else {"workspace": "all"}),
            **({"workspace_count": len(visible_workspace_ids)}
               if visible_workspace_ids is not None else {}),
            **self.store.context_savings(
                workspace_id=wid,
                workspace_ids=visible_workspace_ids,
                repo_id=rid,
                from_ts=from_value,
                to_ts=to_value,
                release_version=release_version,
            ),
        }
        if gb:
            valid_dims = {"workspace", "repo", "agent", "day"}
            if gb not in valid_dims:
                raise ValidationError(
                    f"group_by must be one of: {', '.join(sorted(valid_dims))}"
                )
            rows = self.store.context_savings_grouped(
                workspace_id=wid,
                workspace_ids=visible_workspace_ids,
                repo_id=rid,
                group_by=gb,
                from_ts=from_value,
                to_ts=to_value,
                release_version=release_version,
            )
            base["group_by"] = gb
            base["by_group"] = rows
        if fmt == "csv":
            import csv as _csv
            import io as _io
            buf = _io.StringIO()
            if gb:
                fields = [
                    "group_key", "token_counter", "receipt_count", "source_tokens",
                    "context_tokens", "saved_tokens", "budget_tokens",
                    "packed_count", "omitted_count", "savings_ratio",
                ]
                rows = base.get("by_group", [])
            else:
                fields = [
                    "token_counter", "receipt_count", "source_tokens", "context_tokens",
                    "saved_tokens", "budget_tokens", "packed_count", "omitted_count",
                    "savings_ratio",
                ]
                rows = base.get("by_token_counter", [])
            writer = _csv.DictWriter(buf, fieldnames=fields)
            writer.writeheader()
            for row in rows:
                writer.writerow({k: row.get(k, "") for k in fields})
            base["csv"] = buf.getvalue()
        return base

    def verify_receipts(self, *, workspace: str, expected_head: str = "",
                        expected_count: Optional[int] = None) -> dict:
        """Verify the local chain and optionally compare an externally saved anchor."""
        wid, _ = self._require_scope(workspace, None)
        expected_head = _clean_text(
            expected_head, field="expected_head", max_chars=128, required=False
        )
        if expected_count is not None:
            try:
                expected_count = int(expected_count)
            except (TypeError, ValueError, OverflowError):
                raise ValidationError("expected_count must be an integer")
            if expected_count < 0:
                raise ValidationError("expected_count must be non-negative")
        return self._safe_receipt_verification(
            self.store.verify_receipts(
                workspace_id=wid,
                expected_head=expected_head,
                expected_count=expected_count,
            )
        )

    @staticmethod
    def _redacted_receipt_value(value: Any) -> str:
        raw = value if isinstance(value, str) else str(value or "")
        return "redacted_sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @classmethod
    def _safe_receipt_id(cls, value: Any, *, allow_empty: bool = False) -> str:
        raw = value if isinstance(value, str) else str(value or "")
        if allow_empty and not raw:
            return ""
        if _RECEIPT_ID_RE.fullmatch(raw):
            return raw
        return cls._redacted_receipt_value(raw)

    @classmethod
    def _safe_receipt_hash(cls, value: Any, *, allow_empty: bool = False) -> str:
        raw = value if isinstance(value, str) else str(value or "")
        if allow_empty and not raw:
            return ""
        if _RECEIPT_HASH_RE.fullmatch(raw):
            return raw
        return cls._redacted_receipt_value(raw)

    @classmethod
    def _safe_receipt_verification(cls, value: Any) -> dict:
        """Project Store verification onto a fixed, content-free public schema."""
        raw = value if isinstance(value, dict) else {}
        errors: list[dict] = []
        raw_errors = raw.get("errors")
        if isinstance(raw_errors, list):
            for item in raw_errors:
                item = item if isinstance(item, dict) else {}
                index = item.get("index")
                if type(index) is not int or index < 0:
                    index = 0
                error = item.get("error")
                if (
                    not isinstance(error, str)
                    or error not in _RECEIPT_VERIFICATION_ERRORS
                ):
                    error = cls._redacted_receipt_value(error)
                errors.append({
                    "index": index,
                    "id": cls._safe_receipt_id(
                        item.get("id"), allow_empty=True
                    ),
                    "error": error,
                })
        count = raw.get("count")
        if type(count) is not int or count < 0:
            count = 0
        return {
            "valid": raw.get("valid") is True and not errors,
            "count": count,
            "head": cls._safe_receipt_hash(
                raw.get("head"), allow_empty=True
            ),
            "anchored": raw.get("anchored") is True,
            "errors": errors,
        }

    def export_receipts(self, *, workspace: str) -> dict:
        """Export every public receipt payload and chain hash.

        ``receipt_log`` is deliberately a bounded inspection view. An export must not
        silently inherit that 10,000-row ceiling because the omitted prefix is part of
        both the chain and its independent count/head anchor.
        """
        conn = self.store.conn
        owns_transaction = not conn.transaction_owned_by_current_thread()
        if owns_transaction:
            conn.execute("BEGIN")
        try:
            wid, _ = self._require_scope(workspace, None)
            result = {
                "format": "engraphis-receipts/1",
                "workspace_digest": hashlib.sha256(wid.encode("utf-8")).hexdigest()[:24],
                "entries": self._complete_receipt_rows(wid),
                "complete": True,
                "verification": self._safe_receipt_verification(
                    self.store.verify_receipts(workspace_id=wid)
                ),
            }
            if owns_transaction and conn.transaction_owned_by_current_thread():
                conn.commit()
            return result
        except BaseException:
            if owns_transaction and conn.transaction_owned_by_current_thread():
                conn.rollback()
            raise

    def _complete_receipt_rows(self, workspace_id: str) -> list[dict]:
        """Return the complete receipt chain in predecessor order.

        Invalid/tampered payloads are represented instead of being dropped. That keeps
        export counts honest and lets the verification result explain the corruption,
        without reflecting arbitrary database text into a supposedly privacy-safe export.
        """
        rows = list(self.store._receipt_chain_state(workspace_id)["rows"])
        return [_public_receipt_row(dict(row)) for row in rows]

    def export_workspace(self, *, workspace: str, recovery: bool = False,
                         canonical: bool = False) -> dict:
        """Return one internally consistent portable workspace snapshot.

        A caller-owned transaction is never committed or rolled back here. Otherwise a
        read transaction spans every constituent table and the receipt verification, so a
        concurrent writer cannot produce a canonical digest of mutually impossible states.
        """
        conn = self.store.conn
        owns_transaction = not conn.transaction_owned_by_current_thread()
        if owns_transaction:
            conn.execute("BEGIN")
        try:
            result = self._export_workspace_snapshot(
                workspace=workspace,
                recovery=recovery,
                canonical=canonical,
            )
            if owns_transaction and conn.transaction_owned_by_current_thread():
                conn.commit()
            return result
        except BaseException:
            if owns_transaction and conn.transaction_owned_by_current_thread():
                conn.rollback()
            raise

    def _export_workspace_snapshot(self, *, workspace: str, recovery: bool = False,
                                   canonical: bool = False) -> dict:
        """Portable dump of durable workspace state, including bi-temporal history.

        Version 2 expands the original memory/session/audit export to the durable graph,
        code, evidence, incidence, event, link, and receipt tables required to reconstruct
        the workspace. Regenerable search indexes and process-local maintenance state are
        explicitly disclosed as omitted. Authenticated non-admin callers receive shared
        records plus only their own session-private records; every derivative reference is
        filtered through that same boundary.
        """

        del recovery  # compatibility flag; local portability itself has no plan gate
        wid, _ = self._require_scope(workspace, None)
        conn = self.store.conn
        principal = _authenticated_principal()
        principal_scoped = principal is not None and principal.get("role") != "admin"

        workspace_row = dict(conn.execute(
            "SELECT * FROM workspaces WHERE id=?", (wid,)
        ).fetchone())
        repos = [dict(row) for row in conn.execute(
            "SELECT * FROM repos WHERE workspace_id=? ORDER BY id", (wid,)
        ).fetchall()]
        repo_ids = {str(row["id"]) for row in repos}

        all_sessions = [dict(row) for row in conn.execute(
            "SELECT * FROM sessions WHERE workspace_id=? ORDER BY id", (wid,)
        ).fetchall()]
        if principal_scoped:
            sessions = [
                row for row in all_sessions
                if str(row.get("user_id") or "") == principal["id"]
            ]
        else:
            sessions = all_sessions
        session_ids = {str(row["id"]) for row in sessions}

        all_memories = [dict(row) for row in conn.execute(
            "SELECT * FROM memories WHERE workspace_id=? ORDER BY id", (wid,)
        ).fetchall()]
        if principal_scoped:
            memories = [
                row for row in all_memories
                if (
                    str(row.get("scope") or "workspace") != "session"
                    or str(row.get("session_id") or "") in session_ids
                )
            ]
        else:
            memories = all_memories
        memory_ids = {str(row["id"]) for row in memories}

        all_source_vaults = [dict(row) for row in conn.execute(
            "SELECT * FROM source_vaults WHERE workspace_id=? ORDER BY id",
            (wid,),
        ).fetchall()]
        if principal_scoped:
            source_vaults = [
                row for row in all_source_vaults
                if (
                    str(row.get("scope") or "workspace") != "session"
                    or str(row.get("session_id") or "") in session_ids
                )
            ]
        else:
            source_vaults = all_source_vaults
        source_vault_ids = {str(row["id"]) for row in source_vaults}
        if source_vault_ids:
            source_imports = [dict(row) for row in conn.execute(
                "SELECT * FROM source_imports WHERE vault_id IN ("
                + ",".join("?" for _ in source_vault_ids)
                + ") ORDER BY vault_id, source_key, id",
                sorted(source_vault_ids),
            ).fetchall()]
        else:
            source_imports = []
        # Job rows are intentionally omitted from portability exports, so a manifest
        # entry cannot retain a dangling last_seen_job_id reference. Current source
        # hashes, paths, and memory associations remain resumable on the next import.
        for source_import in source_imports:
            source_import["last_seen_job_id"] = None
            if (
                source_import.get("memory_id")
                and str(source_import["memory_id"]) not in memory_ids
            ):
                source_import["memory_id"] = None

        all_entities = [dict(row) for row in conn.execute(
            "SELECT * FROM entities WHERE workspace_id=? ORDER BY id", (wid,)
        ).fetchall()]
        workspace_entity_ids = {str(row["id"]) for row in all_entities}
        all_edges = [dict(row) for row in conn.execute(
            "SELECT * FROM edges WHERE workspace_id=? ORDER BY id", (wid,)
        ).fetchall()]
        all_supports = [dict(row) for row in conn.execute(
            "SELECT support.* FROM edge_supports support "
            "JOIN edges edge ON edge.id=support.edge_id "
            "WHERE edge.workspace_id=? "
            "ORDER BY support.edge_id, support.memory_id, support.source_kind, support.id",
            (wid,),
        ).fetchall()]
        supports_by_edge: dict[str, list[dict]] = {}
        for support in all_supports:
            supports_by_edge.setdefault(str(support["edge_id"]), []).append(support)

        def _json_memory_references(raw: Any) -> set[str]:
            try:
                value = json.loads(raw or "{}") if isinstance(raw, str) else raw
            except (TypeError, ValueError, RecursionError):
                return set()
            found: set[str] = set()

            def walk(item: Any) -> None:
                if isinstance(item, dict):
                    for child in item.values():
                        walk(child)
                elif isinstance(item, (list, tuple)):
                    for child in item:
                        walk(child)
                elif isinstance(item, str) and item.startswith("mem_"):
                    found.add(item)

            walk(value)
            return found

        if principal_scoped:
            edges = []
            for edge in all_edges:
                if (
                    str(edge.get("src") or "") not in workspace_entity_ids
                    or str(edge.get("dst") or "") not in workspace_entity_ids
                ):
                    continue
                edge_supports = supports_by_edge.get(str(edge["id"]), [])
                visible_supports = [
                    row for row in edge_supports
                    if str(row.get("memory_id") or "") in memory_ids
                ]
                provenance_refs = _json_memory_references(edge.get("provenance"))
                if edge_supports and not visible_supports:
                    continue
                if (
                    not edge_supports
                    and provenance_refs
                    and provenance_refs.isdisjoint(memory_ids)
                ):
                    continue
                edges.append(edge)
        else:
            edges = all_edges
        edge_ids = {str(row["id"]) for row in edges}
        edge_supports = [
            row for row in all_supports
            if (
                str(row.get("edge_id") or "") in edge_ids
                and str(row.get("memory_id") or "") in memory_ids
            )
        ]

        all_incidence = [dict(row) for row in conn.execute(
            "SELECT * FROM memory_entities WHERE workspace_id=? "
            "ORDER BY memory_id, entity_id, source_kind, id",
            (wid,),
        ).fetchall()]
        memory_entities = [
            row for row in all_incidence
            if (
                str(row.get("memory_id") or "") in memory_ids
                and str(row.get("entity_id") or "") in workspace_entity_ids
            )
        ]
        if principal_scoped:
            entity_ids = {
                str(edge["src"]) for edge in edges
            } | {
                str(edge["dst"]) for edge in edges
            } | {
                str(row["entity_id"]) for row in memory_entities
            }
            entities = [
                row for row in all_entities if str(row["id"]) in entity_ids
            ]
        else:
            entities = all_entities
            entity_ids = workspace_entity_ids

        # A malformed/cross-workspace endpoint is not portable workspace state.
        edges = [
            row for row in edges
            if str(row.get("src") or "") in entity_ids
            and str(row.get("dst") or "") in entity_ids
        ]
        edge_ids = {str(row["id"]) for row in edges}
        edge_supports = [
            row for row in edge_supports
            if str(row.get("edge_id") or "") in edge_ids
        ]
        memory_entities = [
            row for row in memory_entities
            if str(row.get("entity_id") or "") in entity_ids
        ]

        memory_links = [dict(row) for row in conn.execute(
            "SELECT link.* FROM mem_links link "
            "JOIN memories left_memory ON left_memory.id=link.a "
            "JOIN memories right_memory ON right_memory.id=link.b "
            "WHERE left_memory.workspace_id=? AND right_memory.workspace_id=? "
            "ORDER BY link.a, link.b, link.relation, link.layer, link.created_at",
            (wid, wid),
        ).fetchall()]
        memory_links = [
            row for row in memory_links
            if str(row.get("a") or "") in memory_ids
            and str(row.get("b") or "") in memory_ids
        ]

        if repo_ids:
            symbols = [dict(row) for row in conn.execute(
                "SELECT symbol.* FROM symbols symbol "
                "JOIN repos repo ON repo.id=symbol.repo_id "
                "WHERE repo.workspace_id=? ORDER BY symbol.id",
                (wid,),
            ).fetchall()]
            code_edges = [dict(row) for row in conn.execute(
                "SELECT edge.* FROM code_edges edge "
                "JOIN repos repo ON repo.id=edge.repo_id "
                "WHERE repo.workspace_id=? ORDER BY edge.id",
                (wid,),
            ).fetchall()]
            code_files = [dict(row) for row in conn.execute(
                "SELECT file.* FROM code_files file "
                "JOIN repos repo ON repo.id=file.repo_id "
                "WHERE repo.workspace_id=? ORDER BY file.repo_id, file.file",
                (wid,),
            ).fetchall()]
            code_memory_links = [dict(row) for row in conn.execute(
                "SELECT link.* FROM code_memory_links link "
                "JOIN repos repo ON repo.id=link.repo_id "
                "WHERE repo.workspace_id=? ORDER BY link.id",
                (wid,),
            ).fetchall()]
        else:
            symbols = []
            code_edges = []
            code_files = []
            code_memory_links = []
        symbol_ids = {str(row["id"]) for row in symbols}
        code_memory_links = [
            row for row in code_memory_links
            if (
                str(row.get("memory_id") or "") in memory_ids
                and str(row.get("symbol_id") or "") in symbol_ids
            )
        ]

        events = [dict(row) for row in conn.execute(
            "SELECT * FROM events WHERE workspace_id=? ORDER BY id", (wid,)
        ).fetchall()]
        if principal_scoped:
            events = [
                row for row in events
                if (
                    not row.get("session_id")
                    or str(row["session_id"]) in session_ids
                )
            ]
        event_ids = {str(row["id"]) for row in events}

        audit_by_id: dict[str, dict] = {}
        for row in conn.execute(
            "SELECT audit.* FROM audit audit "
            "JOIN memories memory ON memory.id=audit.target "
            "WHERE memory.workspace_id=? "
            "UNION ALL SELECT audit.* FROM audit audit WHERE audit.target=?",
            (wid, wid),
        ).fetchall():
            item = dict(row)
            if (
                (not principal_scoped and item["target"] == wid)
                or str(item["target"]) in memory_ids
            ):
                audit_by_id[str(item["id"])] = item
        audit = sorted(
            audit_by_id.values(),
            key=lambda row: (
                float(row.get("ts") or 0.0),
                str(row.get("id") or ""),
            ),
        )
        audit_ids = {str(row["id"]) for row in audit}

        receipts = self._complete_receipt_rows(wid)
        receipt_ids = {str(row.get("id") or "") for row in receipts}
        receipt_chain_row = conn.execute(
            "SELECT receipt_count, head_hash, integrity_error, updated_at "
            "FROM receipt_chain_heads WHERE workspace_id=?",
            (wid,),
        ).fetchone()
        receipt_chain = dict(receipt_chain_row) if receipt_chain_row is not None else None
        if receipt_chain is not None:
            raw_count = receipt_chain.get("receipt_count")
            if type(raw_count) is not int or raw_count < 0:
                receipt_chain["receipt_count"] = None
            raw_updated_at = receipt_chain.get("updated_at")
            if (
                type(raw_updated_at) not in (int, float)
                or not math.isfinite(float(raw_updated_at))
            ):
                receipt_chain["updated_at"] = None
            raw_error = str(receipt_chain.get("integrity_error") or "")
            if raw_error not in {
                "", "pre_append_anchor_mismatch", "pre_append_anchor_missing",
                "pre_append_chain_corruption", "migration_chain_invalid",
            }:
                receipt_chain["integrity_error"] = self._redacted_receipt_value(
                    raw_error
                )
            receipt_chain["head_hash"] = self._safe_receipt_hash(
                receipt_chain.get("head_hash"), allow_empty=True
            )
        receipt_verification = self._safe_receipt_verification(
            self.store.verify_receipts(workspace_id=wid)
        )

        # Scrub structured references that point outside the export boundary. This is
        # essential for Team exports: dropping a private memory while leaving its id in
        # provenance, incidence, or event refs is still a privacy leak.
        allowed_reference_ids = {
            wid,
            *repo_ids,
            *session_ids,
            *memory_ids,
            *entity_ids,
            *edge_ids,
            *symbol_ids,
            *event_ids,
            *audit_ids,
            *receipt_ids,
            *(str(row.get("id") or "") for row in edge_supports),
            *(str(row.get("id") or "") for row in memory_entities),
            *(str(row.get("id") or "") for row in code_edges),
            *(str(row.get("id") or "") for row in code_memory_links),
            *source_vault_ids,
            *(str(row.get("id") or "") for row in source_imports),
        }
        typed_reference = re.compile(
            r"^(?:ws|repo|ses|mem|ent|edg|sym|evt|job|aud|dev|rcpt|vlt|src)_[A-Za-z0-9_-]+$"
        )
        embedded_reference = re.compile(
            r"(?:ws|repo|ses|mem|ent|edg|sym|evt|aud|dev|rcpt|vlt|src)_[A-Za-z0-9_-]+"
        )
        dropped = object()

        def scrub_value(value: Any) -> Any:
            if isinstance(value, dict):
                clean: dict = {}
                for key, child in value.items():
                    scrubbed = scrub_value(child)
                    if scrubbed is not dropped:
                        clean[key] = scrubbed
                return clean
            if isinstance(value, list):
                return [
                    scrubbed for child in value
                    if (scrubbed := scrub_value(child)) is not dropped
                ]
            if isinstance(value, tuple):
                return scrub_value(list(value))
            if isinstance(value, str):
                if typed_reference.fullmatch(value) and value not in allowed_reference_ids:
                    return dropped
                return embedded_reference.sub(
                    lambda match: (
                        "[redacted]"
                        if match.group(0) not in allowed_reference_ids
                        else match.group(0)
                    ),
                    value,
                )
            return value

        def scrub_json(raw: Any, default: Any) -> Any:
            if not isinstance(raw, str):
                return raw
            try:
                value = json.loads(raw)
            except (TypeError, ValueError, RecursionError):
                return embedded_reference.sub(
                    lambda match: (
                        "[redacted]"
                        if match.group(0) not in allowed_reference_ids
                        else match.group(0)
                    ),
                    raw,
                )
            clean = scrub_value(value)
            if clean is dropped:
                clean = default
            if not canonical and clean == value:
                return raw
            return json.dumps(
                clean,
                sort_keys=canonical,
                separators=(",", ":") if canonical else None,
                ensure_ascii=False,
            )

        workspace_row["settings"] = scrub_json(workspace_row.get("settings"), {})
        if principal_scoped:
            try:
                public_settings = json.loads(workspace_row.get("settings") or "{}")
            except (TypeError, ValueError, RecursionError):
                public_settings = {}
            if isinstance(public_settings, dict):
                public_settings.pop("owner", None)
                workspace_row["settings"] = json.dumps(
                    public_settings,
                    sort_keys=canonical,
                    separators=(",", ":") if canonical else None,
                    ensure_ascii=False,
                )
            # Do not expose server-local placement or credential-bearing remote URLs to a
            # remote Team member. Code/file records remain fully portable.
            for repo in repos:
                repo["root_path"] = None
                repo["vcs_remote"] = None
        for repo in repos:
            repo["settings"] = scrub_json(repo.get("settings"), {})
        for session in sessions:
            session["open_threads"] = scrub_json(session.get("open_threads"), [])
        for memory in memories:
            if str(memory.get("session_id") or "") not in session_ids:
                memory["session_id"] = None
            memory["keywords"] = scrub_json(memory.get("keywords"), [])
            memory["metadata"] = scrub_json(memory.get("metadata"), {})
            memory["provenance"] = scrub_json(memory.get("provenance"), {})
        for entity in entities:
            if str(entity.get("canonical_id") or "") not in entity_ids:
                entity["canonical_id"] = entity["id"]
        for edge in edges:
            edge["provenance"] = scrub_json(edge.get("provenance"), {})
        for support in edge_supports:
            support["provenance"] = scrub_json(support.get("provenance"), {})
        for incidence in memory_entities:
            incidence["provenance"] = scrub_json(incidence.get("provenance"), {})
        for event in events:
            event["refs"] = scrub_json(event.get("refs"), [])
        for source_import in source_imports:
            source_import["subject_key"] = embedded_reference.sub(
                lambda match: (
                    "[redacted]"
                    if match.group(0) not in allowed_reference_ids
                    else match.group(0)
                ),
                str(source_import.get("subject_key") or ""),
            )
            source_import["last_error"] = str(source_import.get("last_error") or "")
        for item in audit:
            item["detail"] = embedded_reference.sub(
                lambda match: (
                    "[redacted]"
                    if match.group(0) not in allowed_reference_ids
                    else match.group(0)
                ),
                str(item.get("detail") or ""),
            )

        table_rows = {
            "repos": repos,
            "sessions": sessions,
            "memories": memories,
            "source_vaults": source_vaults,
            "source_imports": source_imports,
            "entities": entities,
            "edges": edges,
            "edge_supports": edge_supports,
            "memory_entities": memory_entities,
            "memory_links": memory_links,
            "symbols": symbols,
            "code_edges": code_edges,
            "code_files": code_files,
            "code_memory_links": code_memory_links,
            "events": events,
            "audit": audit,
            "receipts": receipts,
        }
        payload = {
            "format": "engraphis-export/2",
            "workspace": workspace,
            "workspace_record": workspace_row,
            "schema_version": self.store.schema_version,
            "visibility": "principal" if principal_scoped else "workspace",
            "counts": {
                name: len(rows) for name, rows in table_rows.items()
            },
            "ordering": {
                "repos": ["id"],
                "sessions": ["id"],
                "memories": ["id"],
                "source_vaults": ["id"],
                "source_imports": ["vault_id", "source_key", "id"],
                "entities": ["id"],
                "edges": ["id"],
                "edge_supports": [
                    "edge_id", "memory_id", "source_kind", "id",
                ],
                "memory_entities": [
                    "memory_id", "entity_id", "source_kind", "id",
                ],
                "memory_links": [
                    "a", "b", "relation", "layer", "created_at",
                ],
                "symbols": ["id"],
                "code_edges": ["id"],
                "code_files": ["repo_id", "file"],
                "code_memory_links": ["id"],
                "events": ["id"],
                "audit": ["ts", "id"],
                "receipts": [
                    "verified predecessor order; deterministic digest order if corrupt",
                ],
            },
            "completeness": {
                "durable_workspace_state": True,
                "source_import_manifest": True,
                "receipts": True,
                "omitted_nonportable_or_regenerable_tables": [
                    "mem_fts",
                    "mem_vectors",
                    "mem_vec_ann",
                    "jobs",
                    "source_import_items",
                    "graph_index_state",
                ],
            },
            **table_rows,
            "receipt_chain": receipt_chain,
            "receipt_verification": receipt_verification,
        }
        if canonical:
            # The table queries and filters above already use their declared stable
            # ordering. The digest intentionally excludes wall-clock export time.
            payload["canonical"] = True
            encoded = json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                default=str,
            ).encode("utf-8")
            return {
                **payload,
                "sha256": hashlib.sha256(encoded).hexdigest(),
            }
        payload["exported_at"] = time.time()
        return payload

    def _recover_stale_graph_jobs(self, workspace_id: Optional[str] = None) -> int:
        """Fail expired process-local workers and release their rebuilding gate.

        Jobs are persisted but Python threads are not. A process crash must therefore
        become a bounded interruption rather than leaving graph reads and all future
        jobs blocked forever. The heartbeat lease also keeps this safe when separate
        service processes share the database.
        """
        now = time.time()
        cutoff = now - GRAPH_INDEX_LEASE_SECONDS
        where = "state IN ('queued','running') AND COALESCE(heartbeat_at, created_at)<?"
        params: list[Any] = [cutoff]
        if workspace_id:
            where += " AND workspace_id=?"
            params.append(workspace_id)
        stale = self.store.conn.execute(
            f"SELECT 1 FROM jobs WHERE {where} LIMIT 1", params
        ).fetchone()
        if stale is None:
            return 0
        owns_transaction = not self.store.conn.transaction_owned_by_current_thread()
        if owns_transaction:
            self.store.conn.execute("BEGIN IMMEDIATE")
        try:
            rows = self.store.conn.execute(
                f"SELECT id, workspace_id, counts, errors FROM jobs WHERE {where}",
                params,
            ).fetchall()
            for row in rows:
                counts = self._graph_job_json(row["counts"], {})
                counts["error_count"] = int(counts.get("error_count") or 0) + 1
                errors = self._graph_job_json(row["errors"], [])
                if len(errors) < 25:
                    errors.append({
                        "item": int(counts.get("memories_scanned") or 0),
                        "code": "worker_lease_expired",
                    })
                self.store.conn.execute(
                    "UPDATE jobs SET state='failed', counts=?, errors=?, finished_at=?, "
                    "heartbeat_at=? WHERE id=? AND state IN ('queued','running')",
                    (json.dumps(counts, sort_keys=True), json.dumps(errors, sort_keys=True),
                     now, now, row["id"]),
                )
                self.store.conn.execute(
                    "UPDATE graph_index_state SET state='ready', active_job_id=NULL, "
                    "updated_at=?, last_error='worker_lease_expired' "
                    "WHERE workspace_id=? AND active_job_id=?",
                    (now, row["workspace_id"], row["id"]),
                )
            if owns_transaction and self.store.conn.transaction_owned_by_current_thread():
                self.store.conn.commit()
        except BaseException:
            if owns_transaction and self.store.conn.transaction_owned_by_current_thread():
                self.store.conn.rollback()
            raise
        return len(rows)

    def _assert_no_active_graph_job(self, *workspace_ids: str) -> None:
        for workspace_id in dict.fromkeys(value for value in workspace_ids if value):
            self._recover_stale_graph_jobs(workspace_id)
            row = self.store.conn.execute(
                "SELECT id FROM jobs WHERE workspace_id=? AND kind='graph_index' "
                "AND state IN ('queued','running') LIMIT 1",
                (workspace_id,),
            ).fetchone()
            if row is not None:
                raise ValidationError(
                    f"workspace graph index job '{row['id']}' is still active"
                )

    def _graph_index_info(self, workspace_id: str) -> dict:
        row = self.store.conn.execute(
            "SELECT generation, state, active_job_id, updated_at, last_error "
            "FROM graph_index_state WHERE workspace_id=?",
            (workspace_id,),
        ).fetchone()
        if row is None:
            return {
                # Graph generations are write-trigger revisions, independent
                # of the database schema version.
                "generation": 0,
                "state": "ready",
                "active_job_id": None,
                "updated_at": None,
                "last_error": "",
            }
        return dict(row)

    def _assert_graph_index_ready(self, workspace_id: str) -> dict:
        self._recover_stale_graph_jobs(workspace_id)
        info = self._graph_index_info(workspace_id)
        if info["state"] == "rebuilding" and info.get("active_job_id"):
            raise GraphIndexRebuilding(str(info["active_job_id"]))
        return info

    @staticmethod
    def _graph_job_json(value: Any, fallback: Any) -> Any:
        try:
            parsed = json.loads(value or "")
        except (TypeError, ValueError, RecursionError):
            return fallback
        return parsed if isinstance(parsed, type(fallback)) else fallback

    def _graph_job_dict(self, row: Any, *, reused: bool = False) -> dict:
        data = dict(row)
        total = int(data.get("total_items") or 0)
        processed = int(data.get("processed_items") or 0)
        return {
            "id": data["id"],
            "workspace_id": data["workspace_id"],
            "repo_id": data.get("repo_id"),
            "kind": data["kind"],
            "state": data["state"],
            "dry_run": bool(data.get("dry_run")),
            "total_items": total,
            "processed_items": processed,
            "progress": (
                1.0 if data["state"] == "completed"
                else round(min(1.0, processed / total), 6) if total else 0.0
            ),
            "counts": self._graph_job_json(data.get("counts"), {}),
            "errors": self._graph_job_json(data.get("errors"), []),
            "cancel_requested": bool(data.get("cancel_requested")),
            "created_at": data.get("created_at"),
            "started_at": data.get("started_at"),
            "finished_at": data.get("finished_at"),
            "reused": reused,
        }

    def graph_index_job(self, job_id: str, *, workspace: str) -> dict:
        wid, _rid = self._require_scope(workspace, None)
        self._recover_stale_graph_jobs(wid)
        clean_id = _clean_text(job_id, field="job_id", max_chars=MAX_NAME_CHARS)
        row = self.store.conn.execute(
            "SELECT * FROM jobs WHERE id=? AND workspace_id=? AND kind='graph_index'",
            (clean_id, wid),
        ).fetchone()
        if row is None:
            raise ValidationError(f"no graph index job '{clean_id}' in workspace '{workspace}'")
        return self._graph_job_dict(row)

    def graph_index_status(self, *, workspace: str) -> dict:
        wid, _rid = self._require_scope(workspace, None)
        self._recover_stale_graph_jobs(wid)
        owns_transaction = not self.store.conn.transaction_owned_by_current_thread()
        if owns_transaction:
            self.store.conn.execute("BEGIN")
        try:
            info = self._graph_index_info(wid)
            row = self.store.conn.execute(
                "SELECT * FROM jobs WHERE workspace_id=? AND kind='graph_index' "
                "ORDER BY created_at DESC, id DESC LIMIT 1",
                (wid,),
            ).fetchone()
            result = {
                "workspace": workspace,
                "index": info,
                "job": self._graph_job_dict(row) if row is not None else None,
            }
            if owns_transaction and self.store.conn.transaction_owned_by_current_thread():
                self.store.conn.commit()
            return result
        except BaseException:
            if owns_transaction and self.store.conn.transaction_owned_by_current_thread():
                self.store.conn.rollback()
            raise

    def start_graph_index_job(self, *, workspace: str, repo: Optional[str] = None,
                              dry_run: bool = True, extractor: str = "regex") -> dict:
        wid, rid = self._require_scope(workspace, repo)
        clean_extractor = _clean_text(
            extractor, field="extractor", max_chars=32
        ).lower()
        if clean_extractor != "regex":
            raise ValidationError("extractor must be 'regex'")
        with self._graph_job_lock:
            if self._closing or self._closed:
                raise ValidationError("memory service is shutting down")
            self._recover_stale_graph_jobs()
            self._graph_job_threads = {
                key: value for key, value in self._graph_job_threads.items()
                if value.is_alive()
            }
            owns_graph_txn = not self.store.conn.transaction_owned_by_current_thread()
            if owns_graph_txn:
                self.store.conn.execute("BEGIN IMMEDIATE")
            try:
                current_scope = self.store.conn.execute(
                    "SELECT 1 FROM workspaces WHERE id=?", (wid,)
                ).fetchone()
                if current_scope is None:
                    raise ValidationError("workspace was removed before the job could start")
                if rid is not None and self.store.conn.execute(
                    "SELECT 1 FROM repos WHERE id=? AND workspace_id=?", (rid, wid)
                ).fetchone() is None:
                    raise ValidationError("repository was removed before the job could start")
                active = self.store.conn.execute(
                    "SELECT * FROM jobs WHERE workspace_id=? AND kind='graph_index' "
                    "AND state IN ('queued','running') ORDER BY created_at DESC LIMIT 1",
                    (wid,),
                ).fetchone()
                if active is not None:
                    if owns_graph_txn:
                        self.store.conn.commit()
                    return self._graph_job_dict(active, reused=True)
                global_active = int(self.store.conn.execute(
                    "SELECT COUNT(*) AS n FROM jobs WHERE kind='graph_index' "
                    "AND state IN ('queued','running')"
                ).fetchone()["n"])
                if (global_active >= MAX_GRAPH_INDEX_WORKERS
                        or len(self._graph_job_threads) >= MAX_GRAPH_INDEX_WORKERS):
                    raise ValidationError(
                        "too many graph index jobs are active; retry after one finishes"
                    )
                live_where = (
                    "workspace_id=? AND COALESCE(scope, 'workspace')!='session' "
                    "AND expired_at IS NULL AND valid_to IS NULL"
                    + (" AND repo_id=?" if rid else "")
                )
                params: tuple[Any, ...] = (wid, rid) if rid else (wid,)
                # This job creates derived graph state. Scan lightweight metadata in
                # deterministic pages and apply the canonical predicate before the
                # capacity guard: pending rows must not consume the approved-index
                # budget. JSON predicates alone would miss legacy/mixed metadata forms.
                total = 0
                upper_memory_id = ""
                after_memory_id = ""
                while True:
                    page_sql = (
                        f"SELECT id, metadata, provenance FROM memories WHERE {live_where} "
                        "AND id>? ORDER BY id LIMIT ?"
                    )
                    candidates = self.store.conn.execute(
                        page_sql,
                        (*params, after_memory_id, GRAPH_INDEX_BATCH_SIZE),
                    ).fetchall()
                    if not candidates:
                        break
                    after_memory_id = str(candidates[-1]["id"])
                    upper_memory_id = after_memory_id
                    for candidate in candidates:
                        if not prompt_eligible(
                            _loads(candidate["provenance"], {}),
                            _loads(candidate["metadata"], {}),
                        ):
                            continue
                        total += 1
                        if total > MAX_GRAPH_INDEX_MEMORIES:
                            raise ValidationError(
                                "graph index job exceeds the memory candidate limit; "
                                "filter by repository"
                            )
                entity_before = int(self.store.conn.execute(
                    "SELECT COUNT(*) AS n FROM entities WHERE workspace_id=?", (wid,)
                ).fetchone()["n"])
                edge_before = int(self.store.conn.execute(
                    "SELECT COUNT(*) AS n FROM edges WHERE workspace_id=?", (wid,)
                ).fetchone()["n"])
                # Bound maintenance metadata even if a client repeatedly starts dry runs.
                self.store.conn.execute(
                    "DELETE FROM jobs WHERE id IN (SELECT id FROM jobs "
                    "WHERE workspace_id=? AND kind='graph_index' "
                    "AND state NOT IN ('queued','running') "
                    "ORDER BY created_at DESC, id DESC LIMIT -1 OFFSET ?)",
                    (wid, GRAPH_INDEX_JOB_HISTORY - 1),
                )
                job_id = make_id("job")
                now = time.time()
                counts = {
                    "memories_scanned": 0,
                    "entity_mentions": 0,
                    "relation_mentions": 0,
                    "entities_before": entity_before,
                    "relations_before": edge_before,
                    "entities_after": entity_before,
                    "relations_after": edge_before,
                    "entities_added": 0,
                    "relations_added": 0,
                    "error_count": 0,
                }
                request = {
                    "workspace": workspace,
                    "repo": repo,
                    "extractor": clean_extractor,
                    "dry_run": bool(dry_run),
                    "upper_memory_id": upper_memory_id,
                }
                self.store.conn.execute(
                    "INSERT INTO jobs(id, workspace_id, repo_id, kind, state, dry_run, "
                    "total_items, processed_items, counts, errors, request, "
                    "cancel_requested, runner_id, heartbeat_at, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        job_id, wid, rid, "graph_index", "queued", int(bool(dry_run)),
                        total, 0, json.dumps(counts, sort_keys=True), "[]",
                        json.dumps(request, sort_keys=True), 0, self._graph_runner_id,
                        now, now,
                    ),
                )
                if not dry_run:
                    self.store.conn.execute(
                        "INSERT INTO graph_index_state "
                        "(workspace_id, generation, state, active_job_id, updated_at, "
                        "last_error) VALUES(?, 1, 'rebuilding', ?, ?, '') "
                        "ON CONFLICT(workspace_id) DO UPDATE SET "
                        "state='rebuilding', active_job_id=excluded.active_job_id, "
                        "updated_at=excluded.updated_at, last_error=''",
                        (wid, job_id, now),
                    )
                if owns_graph_txn:
                    self.store.conn.commit()
            except BaseException:
                if owns_graph_txn and self.store.conn.transaction_owned_by_current_thread():
                    self.store.conn.rollback()
                raise
            worker = threading.Thread(
                target=self._run_graph_index_job,
                args=(job_id,),
                name=f"engraphis-graph-index-{job_id[-8:]}",
                daemon=True,
            )
            self._graph_job_threads[job_id] = worker
            try:
                worker.start()
            except BaseException:
                self._graph_job_threads.pop(job_id, None)
                failed_at = time.time()
                self.store.conn.execute(
                    "UPDATE jobs SET state='failed', finished_at=?, heartbeat_at=? "
                    "WHERE id=?", (failed_at, failed_at, job_id),
                )
                self.store.conn.execute(
                    "UPDATE graph_index_state SET state='ready', active_job_id=NULL, "
                    "updated_at=?, last_error='worker_start_failed' "
                    "WHERE workspace_id=? AND active_job_id=?",
                    (failed_at, wid, job_id),
                )
                self.store.conn.commit()
                raise
            # Build the response without another SELECT to avoid pinning the
            # connection lock, which would block the worker thread from starting.
            return {
                "id": job_id,
                "workspace_id": wid,
                "repo_id": rid,
                "kind": "graph_index",
                "state": "queued",
                "dry_run": bool(dry_run),
                "total_items": total,
                "processed_items": 0,
                "progress": 0.0,
                "counts": counts,
                "errors": [],
                "cancel_requested": False,
                "created_at": now,
                "started_at": None,
                "finished_at": None,
                "reused": False,
            }

    def cancel_graph_index_job(self, job_id: str, *, workspace: str) -> dict:
        wid, _rid = self._require_scope(workspace, None)
        self._recover_stale_graph_jobs(wid)
        clean_id = _clean_text(job_id, field="job_id", max_chars=MAX_NAME_CHARS)
        row = self.store.conn.execute(
            "SELECT * FROM jobs WHERE id=? AND workspace_id=? AND kind='graph_index'",
            (clean_id, wid),
        ).fetchone()
        if row is None:
            raise ValidationError(f"no graph index job '{clean_id}' in workspace '{workspace}'")
        if row["state"] in {"queued", "running"}:
            self.store.conn.execute(
                "UPDATE jobs SET cancel_requested=1 WHERE id=?", (clean_id,)
            )
            self.store.conn.commit()
            row = self.store.conn.execute(
                "SELECT * FROM jobs WHERE id=?", (clean_id,)
            ).fetchone()
        return self._graph_job_dict(row)

    def _run_graph_index_job(self, job_id: str) -> None:
        from engraphis.backends.graph_extractor import (
            StructuredMetadataGraphExtractor,
            feed as graph_feed,
            get_graph_extractor,
        )

        row = self.store.conn.execute(
            "SELECT * FROM jobs WHERE id=? AND runner_id=?",
            (job_id, self._graph_runner_id),
        ).fetchone()
        if row is None:
            return
        wid, rid, dry_run = row["workspace_id"], row["repo_id"], bool(row["dry_run"])
        request = self._graph_job_json(row["request"], {})
        counts = {
            "memories_scanned": 0,
            "entity_mentions": 0,
            "relation_mentions": 0,
            "entities_before": 0,
            "relations_before": 0,
            "entities_after": 0,
            "relations_after": 0,
            "entities_added": 0,
            "relations_added": 0,
            "error_count": 0,
            **self._graph_job_json(row["counts"], {}),
        }
        errors: list[dict] = []
        final_state = "failed"
        error_code = ""
        try:
            if self._closing:
                final_state = "cancelled"
                return
            started = time.time()
            claimed = self.store.conn.execute(
                "UPDATE jobs SET state='running', started_at=?, heartbeat_at=? "
                "WHERE id=? AND runner_id=? AND state='queued'",
                (started, started, job_id, self._graph_runner_id),
            )
            self.store.conn.commit()
            if claimed.rowcount != 1:
                return
            regex_extractor = get_graph_extractor(str(request.get("extractor") or "regex"))
            upper_memory_id = str(request.get("upper_memory_id") or "")
            last_memory_id = ""
            processed = 0
            stop = False
            while not stop:
                if self._closing:
                    final_state = "cancelled"
                    break
                cancellation = self.store.conn.execute(
                    "SELECT cancel_requested, state, runner_id FROM jobs WHERE id=?",
                    (job_id,),
                ).fetchone()
                if (cancellation is None or bool(cancellation["cancel_requested"])
                        or cancellation["state"] != "running"
                        or cancellation["runner_id"] != self._graph_runner_id):
                    final_state = "cancelled"
                    break
                id_sql = (
                    "SELECT id, metadata, provenance FROM memories WHERE workspace_id=? "
                    "AND COALESCE(scope, 'workspace')!='session' "
                    "AND expired_at IS NULL AND valid_to IS NULL AND id>?"
                )
                id_params: list[Any] = [wid, last_memory_id]
                if rid:
                    id_sql += " AND repo_id=?"
                    id_params.append(rid)
                if upper_memory_id:
                    id_sql += " AND id<=?"
                    id_params.append(upper_memory_id)
                id_sql += " ORDER BY id LIMIT ?"
                id_params.append(GRAPH_INDEX_BATCH_SIZE)
                candidate_rows = self.store.conn.execute(
                    id_sql, id_params
                ).fetchall()
                if not candidate_rows:
                    final_state = "completed"
                    break
                for candidate in candidate_rows:
                    if self._closing:
                        final_state = "cancelled"
                        stop = True
                        break
                    memory_id = candidate["id"]
                    last_memory_id = memory_id
                    if not prompt_eligible(
                        _loads(candidate["provenance"], {}), _loads(candidate["metadata"], {})
                    ):
                        continue
                    cancelled = self.store.conn.execute(
                        "SELECT cancel_requested, state, runner_id FROM jobs WHERE id=?",
                        (job_id,),
                    ).fetchone()
                    if (cancelled is None or bool(cancelled["cancel_requested"])
                            or cancelled["state"] not in {"queued", "running"}
                            or cancelled["runner_id"] != self._graph_runner_id):
                        final_state = "cancelled"
                        stop = True
                        break
                    transaction_started = False
                    try:
                        if not dry_run:
                            self.store.conn.execute("BEGIN IMMEDIATE")
                            transaction_started = True
                        memory_sql = (
                            "SELECT id, repo_id, title, content, metadata, provenance FROM memories "
                            "WHERE id=? AND workspace_id=? AND expired_at IS NULL "
                            "AND valid_to IS NULL "
                            "AND COALESCE(scope, 'workspace')!='session'"
                        )
                        memory_params: list[Any] = [memory_id, wid]
                        if rid:
                            memory_sql += " AND repo_id=?"
                            memory_params.append(rid)
                        memory = self.store.conn.execute(
                            memory_sql, memory_params
                        ).fetchone()
                        if memory is not None and prompt_eligible(
                            _loads(memory["provenance"], {}), _loads(memory["metadata"], {})
                        ):
                            try:
                                metadata = json.loads(memory["metadata"] or "{}")
                            except (TypeError, ValueError, RecursionError):
                                metadata = {}
                            extractors: list[tuple[str, Any]] = []
                            if (isinstance(metadata, dict)
                                    and self.engine._has_structured_graph_metadata(metadata)):
                                extractors.append((
                                    "structured_index",
                                    StructuredMetadataGraphExtractor(metadata),
                                ))
                            extractors.append(("regex_index", regex_extractor))
                            for source, selected_extractor in extractors:
                                extraction = selected_extractor.extract(
                                    memory["content"] or "", title=memory["title"] or ""
                                )
                                counts["entity_mentions"] += len(extraction.entities)
                                counts["relation_mentions"] += len(extraction.relations)
                                if not dry_run:
                                    graph_feed(
                                        self.store,
                                        memory["content"] or "",
                                        workspace_id=wid,
                                        repo_id=memory["repo_id"],
                                        title=memory["title"] or "",
                                        extractor=selected_extractor,
                                        extraction=extraction,
                                        provenance={
                                            "source": source,
                                            "memory_id": memory["id"],
                                            "job_id": job_id,
                                        },
                                        commit=False,
                                    )
                        processed += 1
                        counts["memories_scanned"] = processed
                        heartbeat = time.time()
                        progress = self.store.conn.execute(
                            "UPDATE jobs SET processed_items=?, counts=?, errors=?, "
                            "heartbeat_at=? WHERE id=? AND runner_id=? AND state='running'",
                            (
                                processed,
                                json.dumps(counts, sort_keys=True),
                                json.dumps(errors, sort_keys=True),
                                heartbeat,
                                job_id,
                                self._graph_runner_id,
                            ),
                        )
                        self.store.conn.commit()
                        transaction_started = False
                        if progress.rowcount != 1:
                            final_state = "cancelled"
                            stop = True
                            break
                    except Exception as exc:  # noqa: BLE001 - isolate one bad memory
                        if (transaction_started
                                or self.store.conn.transaction_owned_by_current_thread()):
                            self.store.conn.rollback()
                        counts["error_count"] += 1
                        if len(errors) < 25:
                            errors.append({
                                "item": processed + 1,
                                "code": type(exc).__name__[:80],
                            })
                        processed += 1
                        counts["memories_scanned"] = processed
                        heartbeat = time.time()
                        self.store.conn.execute(
                            "UPDATE jobs SET processed_items=?, counts=?, errors=?, "
                            "heartbeat_at=? WHERE id=? AND runner_id=? AND state='running'",
                            (
                                processed,
                                json.dumps(counts, sort_keys=True),
                                json.dumps(errors, sort_keys=True),
                                heartbeat,
                                job_id,
                                self._graph_runner_id,
                            ),
                        )
                        self.store.conn.commit()

            entity_after = int(self.store.conn.execute(
                "SELECT COUNT(*) AS n FROM entities WHERE workspace_id=?", (wid,)
            ).fetchone()["n"])
            edge_after = int(self.store.conn.execute(
                "SELECT COUNT(*) AS n FROM edges WHERE workspace_id=?", (wid,)
            ).fetchone()["n"])
            counts.update({
                "entities_after": entity_after,
                "relations_after": edge_after,
                "entities_added": entity_after - int(counts["entities_before"]),
                "relations_added": edge_after - int(counts["relations_before"]),
            })
        except Exception as exc:  # noqa: BLE001 - persist a safe terminal job state
            error_code = type(exc).__name__[:80]
            counts["error_count"] = int(counts.get("error_count") or 0) + 1
            errors.append({"item": int(counts.get("memories_scanned") or 0),
                           "code": error_code})
            final_state = "failed"
        finally:
            try:
                status = "ok" if final_state == "completed" else final_state
                self.store.audit(
                    "system", f"graph_index_{final_state}", wid,
                    f"job={job_id}; dry_run={int(dry_run)}; "
                    f"processed={int(counts.get('memories_scanned') or 0)}",
                )
                self.store.record_receipt(
                    "graph_index",
                    workspace_id=wid,
                    repo_id=rid or "",
                    actor="system",
                    target_count=int(counts.get("memories_scanned") or 0),
                    status=status,
                    metadata={
                        "dry_run": bool(dry_run),
                        "error_count": int(counts.get("error_count") or 0),
                        "entities_added": int(counts.get("entities_added") or 0),
                        "relations_added": int(counts.get("relations_added") or 0),
                    },
                )
            except Exception as exc:  # noqa: BLE001 - terminal state must still persist
                error_code = type(exc).__name__[:80]
                counts["error_count"] = int(counts.get("error_count") or 0) + 1
                errors.append({"item": int(counts.get("memories_scanned") or 0),
                               "code": error_code})
                final_state = "failed"
            finally:
                finished = time.time()
                terminal = self.store.conn.execute(
                    "UPDATE jobs SET state=?, processed_items=?, counts=?, errors=?, "
                    "finished_at=?, heartbeat_at=? WHERE id=? AND runner_id=? "
                    "AND state IN ('queued','running')",
                    (
                        final_state,
                        int(counts.get("memories_scanned") or 0),
                        json.dumps(counts, sort_keys=True),
                        json.dumps(errors[:25], sort_keys=True),
                        finished,
                        finished,
                        job_id,
                        self._graph_runner_id,
                    ),
                )
                if not dry_run and terminal.rowcount == 1:
                    self.store.conn.execute(
                        "UPDATE graph_index_state SET state='ready', active_job_id=NULL, "
                        "updated_at=?, last_error=? "
                        "WHERE workspace_id=? AND active_job_id=? "
                        "AND EXISTS(SELECT 1 FROM workspaces WHERE id=?)",
                        (finished, error_code, wid, job_id, wid),
                    )
                self.store.conn.commit()
                self._graph_scene_cache.clear()
                with self._graph_job_lock:
                    self._graph_job_threads.pop(job_id, None)

    def _graph_scene_rows(self, *, workspace: str, repo: Optional[str] = None,
                          as_of: Optional[float] = None,
                          valid_at: Optional[float] = None,
                          known_at: Optional[float] = None,
                          entity_types: Optional[list[str]] = None,
                          memory_types: Optional[list[str]] = None,
                          time_from: Optional[float] = None,
                          time_to: Optional[float] = None,
                          include_weak_cooccurrence: bool = True,
                          include_code: bool = False,
                          include_complete_rows: bool = False,
                          include_history: bool = False,
                          include_memory_nodes: bool = True) -> tuple:
        """Load one transactionally consistent graph snapshot and generation state."""
        clean_workspace = self._clean_ws(workspace)
        workspace_id = self._lookup_workspace(clean_workspace)
        if workspace_id:
            self._recover_stale_graph_jobs(workspace_id)
        owns_transaction = not self.store.conn.transaction_owned_by_current_thread()
        if owns_transaction:
            self.store.conn.execute("BEGIN")
        try:
            rows = self._graph_scene_rows_unlocked(
                workspace=clean_workspace,
                repo=repo,
                as_of=as_of,
                valid_at=valid_at,
                known_at=known_at,
                entity_types=entity_types,
                memory_types=memory_types,
                time_from=time_from,
                time_to=time_to,
                include_weak_cooccurrence=include_weak_cooccurrence,
                include_code=include_code,
                include_complete_rows=include_complete_rows,
                include_history=include_history,
                include_memory_nodes=include_memory_nodes,
            )
            index_info = self._graph_index_info(rows[1]) if rows[1] else {
                "generation": 0,
                "state": "ready",
                "active_job_id": None,
                "updated_at": None,
                "last_error": "",
            }
            if owns_transaction and self.store.conn.transaction_owned_by_current_thread():
                self.store.conn.commit()
            return (*rows, index_info)
        except BaseException:
            if owns_transaction and self.store.conn.transaction_owned_by_current_thread():
                self.store.conn.rollback()
            raise

    def _graph_scene_rows_unlocked(self, *, workspace: str, repo: Optional[str] = None,
                                   as_of: Optional[float] = None,
                                   valid_at: Optional[float] = None,
                                   known_at: Optional[float] = None,
                                   entity_types: Optional[list[str]] = None,
                                   memory_types: Optional[list[str]] = None,
                                   time_from: Optional[float] = None,
                                   time_to: Optional[float] = None,
                                   include_weak_cooccurrence: bool = True,
                                   include_code: bool = False,
                                   include_complete_rows: bool = False,
                                   include_history: bool = False,
                                   include_memory_nodes: bool = True) -> tuple:
        """Load the complete scoped graph for deterministic server-side ranking.

        This is intentionally read-only. Graph population is an explicit write/index
        concern; no GET path calls the legacy lazy backfill helpers.
        """
        ws = self._clean_ws(workspace)
        wid = self._lookup_workspace(ws)
        if wid is None:
            return ws, "", [], [], [], [], [], []
        self._assert_graph_index_ready(wid)
        clean_entity_types = (
            _clean_string_list(
                entity_types, field="entity_types", max_items=64,
                max_chars=MAX_NAME_CHARS,
            )
            if entity_types is not None else []
        )
        clean_memory_types = sorted({
            _enum(value, MemoryType, "memory_type").value
            for value in _clean_string_list(
                memory_types, field="memory_types", max_items=4,
                max_chars=MAX_NAME_CHARS,
            )
        }) if memory_types is not None else []
        repo_id = None
        if repo:
            repo_name = _clean_name(repo, field="repo")
            repo_id = self._lookup_repo(wid, repo_name)
            if repo_id is None:
                raise ValidationError(f"no repo named '{repo_name}' in workspace '{ws}'")
        as_of, valid_at, known_at = _temporal_anchors(
            as_of=as_of, valid_at=valid_at, known_at=known_at
        )
        present = time.time()
        t = valid_at if valid_at is not None else present
        known_t = known_at if known_at is not None else present

        def temporal_ghost(row: Any) -> bool:
            try:
                valid_to_value = row["valid_to"]
                recorded_at = row["valid_to_recorded_at"]
            except (IndexError, KeyError, TypeError):
                valid_to_value = row.get("valid_to")
                recorded_at = row.get("valid_to_recorded_at")
            try:
                return bool(
                    valid_to_value is not None
                    and float(valid_to_value) <= t
                    and (recorded_at is None or float(recorded_at) <= known_t)
                )
            except (TypeError, ValueError):
                return False

        try:
            lower_time = float(time_from) if time_from is not None else None
            upper_time = float(time_to) if time_to is not None else None
        except (TypeError, ValueError, OverflowError):
            raise ValidationError("time range values must be finite timestamps")
        if ((lower_time is not None and not math.isfinite(lower_time))
                or (upper_time is not None and not math.isfinite(upper_time))):
            raise ValidationError("time range values must be finite timestamps")
        if lower_time is not None and upper_time is not None and lower_time > upper_time:
            raise ValidationError("time_from must be less than or equal to time_to")

        # Classify public endpoint visibility before applying the entity candidate cap.
        # A repository filter scopes the expensive support join; the endpoint-only scan
        # below still preserves workspace-wide private-edge classification.
        visibility_sql = (
            "SELECT visibility_edge.repo_id, visibility_edge.src, visibility_edge.dst, "
            "MAX(CASE "
            "WHEN visibility_support.edge_id IS NULL THEN 1 "
            "WHEN visibility_memory.id IS NOT NULL "
            "AND COALESCE(visibility_memory.scope, 'workspace')!='session' THEN 1 "
            "ELSE 0 END) AS public_edge "
            "FROM edges visibility_edge "
            "LEFT JOIN edge_supports visibility_support "
            "ON visibility_support.edge_id=visibility_edge.id "
            "LEFT JOIN memories visibility_memory "
            "ON visibility_memory.id=visibility_support.memory_id "
            "WHERE visibility_edge.workspace_id=? "
        )
        visibility_params: list[Any] = [wid]
        if repo_id:
            visibility_sql += "AND (visibility_edge.repo_id=? OR visibility_edge.repo_id IS NULL) "
            visibility_params.append(repo_id)
        visibility_sql += (
            "GROUP BY visibility_edge.id, visibility_edge.repo_id, "
            "visibility_edge.src, visibility_edge.dst"
        )
        visibility_rows = self.store.conn.execute(
            visibility_sql, visibility_params,
        ).fetchall()
        # Workspace-wide endpoints still prevent an unrelated private edge from
        # promoting a workspace-scoped entity into the selected repository.
        touching_rows = self.store.conn.execute(
            "SELECT src, dst FROM edges WHERE workspace_id=?", (wid,)
        ).fetchall()
        historical_touching_ids = {
            str(row[key])
            for row in touching_rows
            for key in ("src", "dst")
            if row[key]
        }
        historically_public_ids = {
            str(row[key])
            for row in visibility_rows
            if bool(row["public_edge"])
            and (not repo_id or row["repo_id"] in (None, repo_id))
            for key in ("src", "dst")
            if row[key]
        }

        entity_sql = (
            "SELECT id, workspace_id, repo_id, name, etype, canonical_id, "
            "normalized_name, canonical_method, canonical_confidence, created_at "
            "FROM entities entity WHERE workspace_id=? "
            "AND (created_at IS NULL OR created_at<=?)"
        )
        entity_params: list[Any] = [wid, known_t]
        if repo_id:
            entity_sql += " AND (repo_id=? OR repo_id IS NULL)"
            entity_params.append(repo_id)
        if clean_entity_types:
            clean_types = sorted(set(clean_entity_types))
            if clean_types:
                marks = ",".join("?" for _ in clean_types)
                entity_sql += f" AND etype IN ({marks})"
                entity_params.extend(clean_types)
        # Page until the cap is reached after session-scope pruning so private
        # evidence cannot crowd out public entities in the candidate set.
        entity_rows = []
        entity_batch_size = 500
        entity_offset = 0
        while True:
            raw_entity_rows = [dict(row) for row in self.store.conn.execute(
                entity_sql + " ORDER BY canonical_id, id LIMIT ? OFFSET ?",
                [*entity_params, entity_batch_size, entity_offset],
            ).fetchall()]
            for row in raw_entity_rows:
                entity_id = str(row.get("id") or "")
                if entity_id not in historical_touching_ids \
                        or entity_id in historically_public_ids:
                    entity_rows.append(row)
                    if len(entity_rows) > MAX_GRAPH_ANALYSIS_ENTITIES:
                        break
            if len(entity_rows) > MAX_GRAPH_ANALYSIS_ENTITIES \
                    or len(raw_entity_rows) < entity_batch_size:
                break
            entity_offset += len(raw_entity_rows)

        edge_sql = (
            "SELECT id, workspace_id, repo_id, src, dst, relation, layer, weight, "
            "valid_from, valid_to, valid_to_recorded_at, ingested_at, expired_at, "
            "provenance FROM edges WHERE workspace_id=? "
        )
        if include_history:
            edge_sql += (
                "AND (valid_from IS NULL OR valid_from<=?) "
                "AND (ingested_at IS NULL OR ingested_at<=?) "
                "AND (expired_at IS NULL OR ?<expired_at) AND "
                + _graph_edge_history_visibility_sql("edges", at=t, known_at=known_t)
            )
            edge_params: list[Any] = [wid, t, known_t, known_t]
        else:
            edge_sql += (
                "AND (valid_from IS NULL OR valid_from<=?) "
                "AND (valid_to IS NULL OR ?<valid_to "
                "OR (valid_to_recorded_at IS NOT NULL AND ?<valid_to_recorded_at)) "
                "AND (ingested_at IS NULL OR ingested_at<=?) "
                "AND (expired_at IS NULL OR ?<expired_at)"
            )
            edge_params = [wid, t, t, known_t, known_t, known_t]
        if repo_id:
            edge_sql += " AND (repo_id=? OR repo_id IS NULL)"
            edge_params.append(repo_id)
        # Weak co-occurrence is evaluated after canonical endpoint/relation bundling in
        # ``build_canonical_graph``. Filtering each physical edge here would incorrectly
        # discard two independent one-support alias edges whose canonical bundle has two
        # supports and is therefore eligible for the default scene.
        # Session scope is invisible without an explicit session context. Support-less
        # legacy/manual edges remain visible for compatibility; an edge that does carry
        # evidence must have a current non-session support.
        restrict_sessions = True
        # History visibility is enforced by ``_graph_edge_history_visibility_sql``.
        # The ordinary evidence predicate below is intentionally live-only and would
        # otherwise erase the closed relations that history mode is meant to expose.
        prune_entities = bool(
            clean_memory_types or lower_time is not None or upper_time is not None
        )
        # Every support must have public memory metadata before it reaches the graph
        # builder.  In history mode the metadata query deliberately retains closed
        # public memories, while still excluding session-scoped memories.
        evidence_filter = True
        allow_supportless = not (
            clean_memory_types or lower_time is not None or upper_time is not None
        )
        if not include_history:
            # Keep session-only relations out of the live candidate set. Filtering
            # their supports later is insufficient: build_canonical_graph can recover
            # provenance from an otherwise support-less edge when its endpoints are
            # also connected by public relations.
            edge_sql += " AND ("
            if allow_supportless:
                edge_sql += (
                    "NOT EXISTS (SELECT 1 FROM edge_supports any_graph_support "
                    "WHERE any_graph_support.edge_id=edges.id) OR "
                )
            edge_sql += (
                "EXISTS (SELECT 1 FROM edge_supports graph_support "
                "JOIN memories graph_memory ON graph_memory.id=graph_support.memory_id "
                "WHERE graph_support.edge_id=edges.id "
                "AND (graph_support.valid_from IS NULL OR graph_support.valid_from<=?) "
                "AND (graph_support.valid_to IS NULL OR ?<graph_support.valid_to "
                "OR (graph_support.valid_to_recorded_at IS NOT NULL "
                "AND ?<graph_support.valid_to_recorded_at)) "
                "AND (graph_support.ingested_at IS NULL OR graph_support.ingested_at<=?) "
                "AND (graph_support.expired_at IS NULL OR ?<graph_support.expired_at) "
                "AND graph_memory.workspace_id=? "
                "AND (graph_memory.valid_from IS NULL OR graph_memory.valid_from<=?) "
                "AND (graph_memory.valid_to IS NULL OR ?<graph_memory.valid_to "
                "OR (graph_memory.valid_to_recorded_at IS NOT NULL "
                "AND ?<graph_memory.valid_to_recorded_at)) "
                "AND (graph_memory.ingested_at IS NULL OR graph_memory.ingested_at<=?) "
                "AND (graph_memory.expired_at IS NULL OR ?<graph_memory.expired_at)"
            )
            edge_params.extend((
                t, t, t, known_t, known_t,
                wid, t, t, t, known_t, known_t,
            ))
            if restrict_sessions:
                edge_sql += " AND COALESCE(graph_memory.scope, 'workspace')!='session'"
            if clean_memory_types:
                marks = ",".join("?" for _ in clean_memory_types)
                edge_sql += f" AND graph_memory.mtype IN ({marks})"
                edge_params.extend(clean_memory_types)
            if lower_time is not None:
                edge_sql += " AND COALESCE(graph_memory.valid_from, graph_memory.ingested_at, 0)>=?"
                edge_params.append(lower_time)
            if upper_time is not None:
                edge_sql += " AND COALESCE(graph_memory.valid_from, graph_memory.ingested_at, 0)<=?"
                edge_params.append(upper_time)
            edge_sql += "))"
        if prune_entities and include_history:
            edge_sql += " AND ("
            if allow_supportless:
                edge_sql += (
                    "NOT EXISTS (SELECT 1 FROM edge_supports any_graph_support "
                    "WHERE any_graph_support.edge_id=edges.id) OR "
                )
            edge_sql += (
                "EXISTS (SELECT 1 FROM edge_supports graph_support "
                "JOIN memories graph_memory ON graph_memory.id=graph_support.memory_id "
                "WHERE graph_support.edge_id=edges.id "
                "AND (graph_support.valid_from IS NULL OR graph_support.valid_from<=?) "
                "AND (graph_support.ingested_at IS NULL OR graph_support.ingested_at<=?) "
                "AND (graph_support.expired_at IS NULL OR ?<graph_support.expired_at) "
                "AND graph_memory.workspace_id=? "
                "AND (graph_memory.valid_from IS NULL OR graph_memory.valid_from<=?) "
                "AND (graph_memory.ingested_at IS NULL OR graph_memory.ingested_at<=?) "
                "AND (graph_memory.expired_at IS NULL OR ?<graph_memory.expired_at)"
            )
            edge_params.extend((
                t, known_t, known_t, wid, t, known_t, known_t,
            ))
            if restrict_sessions:
                edge_sql += " AND COALESCE(graph_memory.scope, 'workspace')!='session'"
            if clean_memory_types:
                marks = ",".join("?" for _ in clean_memory_types)
                edge_sql += f" AND graph_memory.mtype IN ({marks})"
                edge_params.extend(clean_memory_types)
            if lower_time is not None:
                edge_sql += " AND COALESCE(graph_memory.valid_from, graph_memory.ingested_at, 0)>=?"
                edge_params.append(lower_time)
            if upper_time is not None:
                edge_sql += " AND COALESCE(graph_memory.valid_from, graph_memory.ingested_at, 0)<=?"
                edge_params.append(upper_time)
            edge_sql += "))"
        edge_sql += " ORDER BY id LIMIT ?"
        edge_params.append(MAX_GRAPH_ANALYSIS_EDGES + 1)
        edge_rows = [dict(row) for row in self.store.conn.execute(
            edge_sql, edge_params
        ).fetchall()]
        if include_history:
            for edge in edge_rows:
                valid_to_value = edge.get("valid_to")
                recorded_at = edge.get("valid_to_recorded_at")
                try:
                    edge["ghost"] = bool(
                        valid_to_value is not None
                        and float(valid_to_value) <= t
                        and (recorded_at is None or float(recorded_at) <= known_t)
                    )
                except (TypeError, ValueError):
                    edge["ghost"] = False
        if len(edge_rows) > MAX_GRAPH_ANALYSIS_EDGES:
            if include_complete_rows:
                raise GraphSceneCapacityExceeded(
                    resource="raw relations", count=len(edge_rows),
                    limit=MAX_GRAPH_ANALYSIS_EDGES,
                )
            raise ValidationError(
                "graph analysis exceeds the relation candidate limit; filter by repository"
            )

        touching_ids = historical_touching_ids
        visible_endpoint_ids = {
            str(edge[key])
            for edge in edge_rows
            for key in ("src", "dst")
            if edge.get(key)
        }
        canonical_by_id = {
            str(entity["id"]): str(entity.get("canonical_id") or entity["id"])
            for entity in entity_rows
        }
        visible_canonical_ids = {
            canonical_by_id.get(entity_id, entity_id)
            for entity_id in visible_endpoint_ids
        }
        entity_rows = [
            entity for entity in entity_rows
            if str(entity["id"]) not in touching_ids
            or canonical_by_id.get(str(entity["id"]), str(entity["id"]))
            in visible_canonical_ids
        ]
        if len(entity_rows) > MAX_GRAPH_ANALYSIS_ENTITIES:
            if include_complete_rows:
                raise GraphSceneCapacityExceeded(
                    resource="entity rows", count=len(entity_rows),
                    limit=MAX_GRAPH_ANALYSIS_ENTITIES,
                )
            raise ValidationError(
                "graph analysis exceeds the entity candidate limit; filter by repository"
            )

        if include_code:
            repo_sql = "SELECT id, name FROM repos WHERE workspace_id=?"
            repo_params: list[Any] = [wid]
            if repo_id:
                repo_sql += " AND id=?"
                repo_params.append(repo_id)
            repo_rows = self.store.conn.execute(
                repo_sql + " ORDER BY name, id", repo_params
            ).fetchall()
            for repo_row in repo_rows:
                remaining_entities = MAX_GRAPH_ANALYSIS_ENTITIES - len(entity_rows)
                symbol_sql = (
                    "SELECT id, kind, name, fqname, file, valid_from, valid_to, "
                    "valid_to_recorded_at, ingested_at, expired_at FROM symbols "
                    "WHERE repo_id=? AND (valid_from IS NULL OR valid_from<=?) "
                )
                symbol_params: list[Any] = [repo_row["id"], t]
                if include_history:
                    symbol_sql += (
                        "AND (ingested_at IS NULL OR ingested_at<=?) "
                        "AND (expired_at IS NULL OR ?<expired_at) "
                    )
                    symbol_params.extend((known_t, known_t))
                else:
                    symbol_sql += (
                        "AND (valid_to IS NULL OR ?<valid_to "
                        "OR (valid_to_recorded_at IS NOT NULL AND ?<valid_to_recorded_at)) "
                        "AND (ingested_at IS NULL OR ingested_at<=?) "
                        "AND (expired_at IS NULL OR ?<expired_at) "
                    )
                    symbol_params.extend((t, known_t, known_t, known_t))
                symbol_params.append(remaining_entities + 1)
                symbol_rows = [dict(row) for row in self.store.conn.execute(
                    symbol_sql + "ORDER BY id LIMIT ?", symbol_params,
                ).fetchall()]
                if include_history:
                    for symbol in symbol_rows:
                        valid_to_value = symbol.get("valid_to")
                        recorded_at = symbol.get("valid_to_recorded_at")
                        try:
                            symbol["ghost"] = bool(
                                valid_to_value is not None
                                and float(valid_to_value) <= t
                                and (recorded_at is None or float(recorded_at) <= known_t)
                            )
                        except (TypeError, ValueError):
                            symbol["ghost"] = False
                if len(symbol_rows) > remaining_entities:
                    if include_complete_rows:
                        raise GraphSceneCapacityExceeded(
                            resource="entity rows",
                            count=MAX_GRAPH_ANALYSIS_ENTITIES + 1,
                            limit=MAX_GRAPH_ANALYSIS_ENTITIES,
                        )
                    raise ValidationError(
                        "graph analysis exceeds the entity candidate limit; "
                        "filter the code overlay by repository"
                    )
                endpoint: dict[str, str] = {}
                for symbol in symbol_rows:
                    node_id = f"code:{symbol['id']}"
                    label = symbol.get("fqname") or symbol.get("name") or symbol["id"]
                    entity_rows.append({
                        "id": node_id, "workspace_id": wid, "repo_id": repo_row["id"],
                        "name": f"{repo_row['name']}:{label}",
                        "etype": f"code_{symbol.get('kind') or 'symbol'}",
                        "canonical_id": node_id,
                        "normalized_name": normalize_entity_name(label),
                        "canonical_method": "code_identity", "canonical_confidence": 1.0,
                        "ghost": bool(symbol.get("ghost")),
                    })
                    for key in (symbol.get("id"), symbol.get("fqname"), symbol.get("name")):
                        if key:
                            endpoint.setdefault(str(key), node_id)
                remaining_edges = MAX_GRAPH_ANALYSIS_EDGES - len(edge_rows)
                code_edge_sql = (
                    "SELECT id, src, dst, relation, layer, valid_from, valid_to, "
                    "valid_to_recorded_at, ingested_at, expired_at FROM code_edges "
                    "WHERE repo_id=? AND (valid_from IS NULL OR valid_from<=?) "
                )
                code_edge_params: list[Any] = [repo_row["id"], t]
                if include_history:
                    code_edge_sql += (
                        "AND (ingested_at IS NULL OR ingested_at<=?) "
                        "AND (expired_at IS NULL OR ?<expired_at) "
                    )
                    code_edge_params.extend((known_t, known_t))
                else:
                    code_edge_sql += (
                        "AND (valid_to IS NULL OR ?<valid_to "
                        "OR (valid_to_recorded_at IS NOT NULL AND ?<valid_to_recorded_at)) "
                        "AND (ingested_at IS NULL OR ingested_at<=?) "
                        "AND (expired_at IS NULL OR ?<expired_at) "
                    )
                    code_edge_params.extend((t, known_t, known_t, known_t))
                code_edge_params.append(remaining_edges + 1)
                code_edges = [dict(row) for row in self.store.conn.execute(
                    code_edge_sql + "ORDER BY id LIMIT ?", code_edge_params,
                ).fetchall()]
                if include_history:
                    for code_edge in code_edges:
                        valid_to_value = code_edge.get("valid_to")
                        recorded_at = code_edge.get("valid_to_recorded_at")
                        try:
                            code_edge["ghost"] = bool(
                                valid_to_value is not None
                                and float(valid_to_value) <= t
                                and (recorded_at is None or float(recorded_at) <= known_t)
                            )
                        except (TypeError, ValueError):
                            code_edge["ghost"] = False
                if len(code_edges) > remaining_edges:
                    if include_complete_rows:
                        raise GraphSceneCapacityExceeded(
                            resource="raw relations",
                            count=MAX_GRAPH_ANALYSIS_EDGES + 1,
                            limit=MAX_GRAPH_ANALYSIS_EDGES,
                        )
                    raise ValidationError(
                        "graph analysis exceeds the relation candidate limit; "
                        "filter the code overlay by repository"
                    )
                for code_edge in code_edges:
                    source = endpoint.get(str(code_edge["src"] or ""))
                    target = endpoint.get(str(code_edge["dst"] or ""))
                    if source and target and source != target:
                        edge_rows.append({
                            "id": f"code-edge:{code_edge['id']}", "workspace_id": wid,
                            "repo_id": repo_row["id"], "src": source, "dst": target,
                            "relation": code_edge["relation"] or "references",
                            "layer": code_edge["layer"] or "entity", "weight": 1.0,
                            "valid_from": code_edge.get("valid_from"),
                            "valid_to": code_edge.get("valid_to"),
                            "valid_to_recorded_at": code_edge.get("valid_to_recorded_at"),
                            "ingested_at": code_edge.get("ingested_at"),
                            "expired_at": code_edge.get("expired_at"),
                            "ghost": bool(code_edge.get("ghost")),
                            "provenance": json.dumps({"source": "code_index"}),
                        })
        if prune_entities:
            endpoints = {
                str(edge.get(key) or "") for edge in edge_rows
                for key in ("src", "dst") if edge.get(key)
            }
            canonical_by_member = {
                str(entity.get("id") or ""): str(
                    entity.get("canonical_id") or entity.get("id") or ""
                )
                for entity in entity_rows
            }
            endpoint_canonicals = {
                canonical_by_member.get(endpoint, endpoint) for endpoint in endpoints
            }
            entity_rows = [
                entity for entity in entity_rows
                if str(entity.get("canonical_id") or entity.get("id") or "")
                in endpoint_canonicals
            ]
        edge_ids = [row["id"] for row in edge_rows if not str(row["id"]).startswith("code-edge:")]
        # Preserve the distinction between a truly legacy support-less edge and an
        # edge whose normalized supports were filtered out by scope/time. The graph
        # builder may use edge provenance for the former, but must not resurrect a
        # foreign memory id for the latter.
        normalized_support_edge_ids: set[str] = set()
        for start in range(0, len(edge_ids), 500):
            chunk = edge_ids[start:start + 500]
            if not chunk:
                continue
            marks = ",".join("?" for _ in chunk)
            normalized_support_edge_ids.update(
                str(row["edge_id"])
                for row in self.store.conn.execute(
                    f"SELECT DISTINCT edge_id FROM edge_supports WHERE edge_id IN ({marks})",
                    chunk,
                ).fetchall()
            )
        for edge in edge_rows:
            if str(edge.get("id") or "") in normalized_support_edge_ids:
                edge["_has_normalized_support"] = True
        # Bounded IN chunks avoid a second scan of the relation table while preserving
        # the exact selected edge ids. Weak co-occurrence is filtered after canonical
        # relation bundling, once its aggregate support is known.
        scene_filter = SearchFilter(
            workspace_id=wid, repo_id=repo_id, include_ancestors=True,
            valid_at=t, known_at=known_t,
        )
        support_rows = self.store.edge_supports_in_scope(
            edge_ids, flt=scene_filter, limit=MAX_GRAPH_ANALYSIS_SUPPORTS + 1
        )
        if include_history:
            live_support_keys = {
                (str(row.get("edge_id") or ""), str(row.get("memory_id") or ""),
                 str(row.get("source_kind") or ""))
                for row in support_rows
            }
            historical_supports: list[dict] = []
            for start in range(0, len(edge_ids), 500):
                chunk = edge_ids[start:start + 500]
                if not chunk:
                    continue
                marks = ",".join("?" for _ in chunk)
                historical_sql = (
                    "SELECT support.edge_id, support.memory_id, support.source_kind, "
                    "support.confidence, support.valid_from, support.valid_to, "
                    "support.valid_to_recorded_at, support.ingested_at, "
                    "support.expired_at, support.provenance FROM edge_supports support "
                    "JOIN memories memory ON memory.id=support.memory_id "
                    f"WHERE support.edge_id IN ({marks}) "
                    "AND (support.valid_from IS NULL OR support.valid_from<=?) "
                    "AND (support.ingested_at IS NULL OR support.ingested_at<=?) "
                    "AND (support.expired_at IS NULL OR ?<support.expired_at) "
                    "AND (memory.valid_from IS NULL OR memory.valid_from<=?) "
                    "AND (memory.ingested_at IS NULL OR memory.ingested_at<=?) "
                    "AND (memory.expired_at IS NULL OR ?<memory.expired_at) "
                    "AND COALESCE(memory.scope, 'workspace')!='session' "
                    "AND memory.workspace_id=? "
                )
                historical_params: list[Any] = [
                    *chunk, t, known_t, known_t, t, known_t, known_t, wid,
                ]
                if repo_id:
                    historical_sql += "AND " + _repo_memory_scope_sql("memory") + " "
                    historical_params.append(repo_id)
                historical_sql += (
                    "ORDER BY support.edge_id, support.memory_id, support.source_kind"
                )
                rows = self.store.conn.execute(
                    historical_sql, historical_params,
                ).fetchall()
                historical_supports.extend(dict(row) for row in rows)
            for support in support_rows:
                support["ghost"] = temporal_ghost(support)
            for support in historical_supports:
                support["ghost"] = temporal_ghost(support)
                key = (
                    str(support.get("edge_id") or ""),
                    str(support.get("memory_id") or ""),
                    str(support.get("source_kind") or ""),
                )
                if key not in live_support_keys:
                    support_rows.append(support)
                    live_support_keys.add(key)
        if len(support_rows) > MAX_GRAPH_ANALYSIS_SUPPORTS:
            if include_complete_rows:
                raise GraphSceneCapacityExceeded(
                    resource="evidence rows", count=len(support_rows),
                    limit=MAX_GRAPH_ANALYSIS_SUPPORTS,
                )
            raise ValidationError(
                "graph analysis exceeds the evidence candidate limit; filter by repository"
            )
        # Attach only public analytical metadata from supporting memories. This both
        # makes the memory/time facets evidence-backed and ensures requested evidence
        # filters cannot be bypassed by another support row on the same relation.
        support_memory_ids = sorted({
            str(row.get("memory_id") or "") for row in support_rows
            if row.get("memory_id")
        })
        support_memory_meta: dict[str, dict[str, Any]] = {}
        for start in range(0, len(support_memory_ids), 500):
            chunk = support_memory_ids[start:start + 500]
            marks = ",".join("?" for _ in chunk)
            memory_sql = (
                "SELECT id, mtype, COALESCE(valid_from, ingested_at, 0) AS support_time, "
                "valid_to, valid_to_recorded_at "
                "FROM memories WHERE workspace_id=? AND id IN (" + marks + ") "
                "AND (valid_from IS NULL OR valid_from<=?) "
            )
            memory_params: list[Any] = [wid, *chunk, t]
            if repo_id is not None:
                memory_sql += "AND " + _repo_memory_scope_sql() + " "
                memory_params.append(repo_id)
            if include_history:
                # Historical scenes may intentionally surface memories that have
                # since been invalidated. The historical edge/support predicates
                # already establish the valid_at/known_at anchors; applying the
                # live valid_to window here would erase ghost relations whenever a
                # memory facet is requested.
                memory_sql += (
                    "AND (ingested_at IS NULL OR ingested_at<=?) "
                    "AND (expired_at IS NULL OR ?<expired_at)"
                )
                memory_params.extend([known_t, known_t])
            else:
                memory_sql += (
                    "AND (valid_to IS NULL OR ?<valid_to "
                    "OR (valid_to_recorded_at IS NOT NULL AND ?<valid_to_recorded_at)) "
                    "AND (ingested_at IS NULL OR ingested_at<=?) "
                    "AND (expired_at IS NULL OR ?<expired_at)"
                )
                memory_params.extend([t, known_t, known_t, known_t])
            memory_sql += " AND COALESCE(scope, 'workspace')!='session'"
            if clean_memory_types:
                type_marks = ",".join("?" for _ in clean_memory_types)
                memory_sql += f" AND mtype IN ({type_marks})"
                memory_params.extend(clean_memory_types)
            if lower_time is not None:
                memory_sql += " AND COALESCE(valid_from, ingested_at, 0)>=?"
                memory_params.append(lower_time)
            if upper_time is not None:
                memory_sql += " AND COALESCE(valid_from, ingested_at, 0)<=?"
                memory_params.append(upper_time)
            for memory in self.store.conn.execute(memory_sql, memory_params).fetchall():
                support_memory_meta[str(memory["id"])] = {
                    "memory_type": str(memory["mtype"] or ""),
                    "support_time": float(memory["support_time"] or 0.0),
                    "ghost": temporal_ghost(memory),
                }
        enriched_supports = []
        for support in support_rows:
            memory_id = str(support.get("memory_id") or "")
            metadata = support_memory_meta.get(memory_id)
            if evidence_filter and metadata is None:
                continue
            enriched = dict(support)
            if metadata is not None:
                enriched["memory_type"] = metadata["memory_type"]
                enriched["support_time"] = metadata["support_time"]
                enriched["memory_ghost"] = metadata["ghost"]
            enriched_supports.append(enriched)
        if prune_entities:
            matching_edge_ids = {
                str(support.get("edge_id") or "")
                for support in support_rows
                if str(support.get("memory_id") or "") in support_memory_meta
            }
            edge_rows = [
                edge for edge in edge_rows
                if str(edge.get("id") or "").startswith("code-edge:")
                or str(edge.get("id") or "") in matching_edge_ids
            ]
            endpoints = {
                str(edge.get(key) or "")
                for edge in edge_rows
                for key in ("src", "dst") if edge.get(key)
            }
            canonical_by_member = {
                str(entity.get("id") or ""): str(
                    entity.get("canonical_id") or entity.get("id") or ""
                )
                for entity in entity_rows
            }
            endpoint_canonicals = {
                canonical_by_member.get(endpoint, endpoint) for endpoint in endpoints
            }
            entity_rows = [
                entity for entity in entity_rows
                if str(entity.get("canonical_id") or entity.get("id") or "")
                in endpoint_canonicals
            ]

        memory_rows: list[dict] = []
        memory_link_rows: list[dict] = []
        code_memory_link_rows: list[dict] = []
        if include_complete_rows and include_memory_nodes:
            if include_history:
                memory_where = [
                    "workspace_id=?",
                    "(valid_from IS NULL OR valid_from<=?)",
                    "(ingested_at IS NULL OR ingested_at<=?)",
                    "(expired_at IS NULL OR ?<expired_at)",
                ]
                memory_params = [wid, t, known_t, known_t]
            else:
                memory_where = [
                    "workspace_id=?",
                    "(valid_from IS NULL OR valid_from<=?)",
                    "(valid_to IS NULL OR ?<valid_to "
                    "OR (valid_to_recorded_at IS NOT NULL AND ?<valid_to_recorded_at))",
                    "(ingested_at IS NULL OR ingested_at<=?)",
                    "(expired_at IS NULL OR ?<expired_at)",
                ]
                memory_params = [wid, t, t, known_t, known_t, known_t]
            memory_where.append("COALESCE(scope, 'workspace')!='session'")
            if repo_id:
                memory_where.append(_repo_memory_scope_sql())
                memory_params.append(repo_id)
            if clean_memory_types:
                marks = ",".join("?" for _ in clean_memory_types)
                memory_where.append(f"mtype IN ({marks})")
                memory_params.extend(clean_memory_types)
            if lower_time is not None:
                memory_where.append("COALESCE(valid_from, ingested_at, 0)>=?")
                memory_params.append(lower_time)
            if upper_time is not None:
                memory_where.append("COALESCE(valid_from, ingested_at, 0)<=?")
                memory_params.append(upper_time)
            scoped_memory_sql = "SELECT id FROM memories WHERE " + " AND ".join(memory_where)
            memory_rows = [dict(row) for row in self.store.conn.execute(
                "SELECT id, repo_id, session_id, scope, mtype, title, "
                "substr(content, 1, 160) AS content, substr(summary, 1, 160) AS summary, "
                "importance, valid_from, valid_to, valid_to_recorded_at, "
                "ingested_at, expired_at, pinned FROM memories WHERE "
                + " AND ".join(memory_where) + " ORDER BY id LIMIT ?",
                [*memory_params, MAX_GRAPH_COMPLETE_MEMORIES + 1],
            ).fetchall()]
            if include_history:
                for memory in memory_rows:
                    valid_to_value = memory.get("valid_to")
                    recorded_at = memory.get("valid_to_recorded_at")
                    try:
                        memory["ghost"] = bool(
                            valid_to_value is not None
                            and float(valid_to_value) <= t
                            and (recorded_at is None or float(recorded_at) <= known_t)
                        )
                    except (TypeError, ValueError):
                        memory["ghost"] = False
            if len(memory_rows) > MAX_GRAPH_COMPLETE_MEMORIES:
                raise GraphSceneCapacityExceeded(
                    resource="memory nodes", count=len(memory_rows),
                    limit=MAX_GRAPH_COMPLETE_MEMORIES,
                )

            memory_link_sql = (
                "WITH selected_memory AS (" + scoped_memory_sql + ") "
                "SELECT links.a, links.b, links.relation, links.layer, links.reason, "
                "links.created_at, links.valid_from, links.valid_to, "
                "links.valid_to_recorded_at, links.ingested_at, links.expired_at "
                "FROM mem_links links "
                "JOIN selected_memory source ON source.id=links.a "
                "JOIN selected_memory target ON target.id=links.b WHERE "
                "(links.valid_from IS NULL OR links.valid_from<=?) "
            )
            memory_link_params: list[Any] = [*memory_params, t]
            if include_history:
                memory_link_sql += (
                    "AND (links.ingested_at IS NULL OR links.ingested_at<=?) "
                    "AND (links.expired_at IS NULL OR ?<links.expired_at) "
                )
                memory_link_params.extend((known_t, known_t))
            else:
                memory_link_sql += (
                    "AND (links.valid_to IS NULL OR ?<links.valid_to "
                    "OR (links.valid_to_recorded_at IS NOT NULL "
                    "AND ?<links.valid_to_recorded_at)) "
                    "AND (links.ingested_at IS NULL OR links.ingested_at<=?) "
                    "AND (links.expired_at IS NULL OR ?<links.expired_at) "
                )
                memory_link_params.extend((t, known_t, known_t, known_t))
            memory_link_sql += (
                "ORDER BY links.a, links.b, links.relation, links.layer, links.created_at "
                "LIMIT ?"
            )
            memory_link_params.append(MAX_GRAPH_COMPLETE_MEMORY_LINKS + 1)
            memory_link_rows = [dict(row) for row in self.store.conn.execute(
                memory_link_sql, memory_link_params,
            ).fetchall()]
            if include_history:
                for link in memory_link_rows:
                    valid_to_value = link.get("valid_to")
                    recorded_at = link.get("valid_to_recorded_at")
                    try:
                        link["ghost"] = bool(
                            valid_to_value is not None
                            and float(valid_to_value) <= t
                            and (recorded_at is None or float(recorded_at) <= known_t)
                        )
                    except (TypeError, ValueError):
                        link["ghost"] = False
            if len(memory_link_rows) > MAX_GRAPH_COMPLETE_MEMORY_LINKS:
                raise GraphSceneCapacityExceeded(
                    resource="memory connectors", count=len(memory_link_rows),
                    limit=MAX_GRAPH_COMPLETE_MEMORY_LINKS,
                )

            if include_code:
                code_sql = (
                    "WITH selected_memory AS (" + scoped_memory_sql + ") "
                    "SELECT links.id, links.repo_id, links.symbol_id, links.memory_id, "
                    "links.relation, links.confidence, links.valid_from, links.valid_to, "
                    "links.valid_to_recorded_at, links.ingested_at, links.expired_at "
                    "FROM code_memory_links links "
                    "JOIN selected_memory memory ON memory.id=links.memory_id "
                    "JOIN repos repo ON repo.id=links.repo_id WHERE repo.workspace_id=? "
                    "AND (links.valid_from IS NULL OR links.valid_from<=?) "
                )
                code_params: list[Any] = [*memory_params, wid, t]
                if include_history:
                    code_sql += (
                        "AND (links.ingested_at IS NULL OR links.ingested_at<=?) "
                        "AND (links.expired_at IS NULL OR ?<links.expired_at)"
                    )
                    code_params.extend((known_t, known_t))
                else:
                    code_sql += (
                        "AND (links.valid_to IS NULL OR ?<links.valid_to "
                        "OR (links.valid_to_recorded_at IS NOT NULL "
                        "AND ?<links.valid_to_recorded_at)) "
                        "AND (links.ingested_at IS NULL OR links.ingested_at<=?) "
                        "AND (links.expired_at IS NULL OR ?<links.expired_at)"
                    )
                    code_params.extend((t, known_t, known_t, known_t))
                if repo_id:
                    code_sql += " AND links.repo_id=?"
                    code_params.append(repo_id)
                code_sql += " ORDER BY links.id LIMIT ?"
                code_params.append(MAX_GRAPH_COMPLETE_CODE_MEMORY_LINKS + 1)
                code_memory_link_rows = [dict(row) for row in self.store.conn.execute(
                    code_sql, code_params,
                ).fetchall()]
                if include_history:
                    for link in code_memory_link_rows:
                        valid_to_value = link.get("valid_to")
                        recorded_at = link.get("valid_to_recorded_at")
                        try:
                            link["ghost"] = bool(
                                valid_to_value is not None
                                and float(valid_to_value) <= t
                                and (recorded_at is None or float(recorded_at) <= known_t)
                            )
                        except (TypeError, ValueError):
                            link["ghost"] = False
                if len(code_memory_link_rows) > MAX_GRAPH_COMPLETE_CODE_MEMORY_LINKS:
                    raise GraphSceneCapacityExceeded(
                        resource="code-memory connectors",
                        count=len(code_memory_link_rows),
                        limit=MAX_GRAPH_COMPLETE_CODE_MEMORY_LINKS,
                    )
        referenced_repo_ids = sorted({
            str(row.get("repo_id") or "")
            for row in [*entity_rows, *memory_rows]
            if row.get("repo_id")
        })
        repo_name_by_id: dict[str, str] = {}
        for start in range(0, len(referenced_repo_ids), 500):
            chunk = referenced_repo_ids[start:start + 500]
            marks = ",".join("?" for _ in chunk)
            for row in self.store.conn.execute(
                f"SELECT id, name FROM repos WHERE workspace_id=? AND id IN ({marks})",
                [wid, *chunk],
            ).fetchall():
                repo_name_by_id[str(row["id"])] = str(row["name"])
        for row in [*entity_rows, *memory_rows]:
            repo_name = repo_name_by_id.get(str(row.get("repo_id") or ""))
            if repo_name:
                row["repo_name"] = repo_name
        return (
            ws, wid, entity_rows, edge_rows, enriched_supports,
            memory_rows, memory_link_rows, code_memory_link_rows,
        )

    def graph_scene(self, *, workspace: str, level: str = "overview",
                    center_id: Optional[str] = None,
                    system_id: Optional[str] = None,
                    seeds: Optional[list[str]] = None,
                    repo: Optional[str] = None,
                    layers: Optional[list[str]] = None,
                     relations: Optional[list[str]] = None,
                     entity_types: Optional[list[str]] = None,
                     memory_types: Optional[list[str]] = None,
                     as_of: Optional[float] = None,
                     valid_at: Optional[float] = None,
                     known_at: Optional[float] = None,
                     depth: int = 1,
                     time_from: Optional[float] = None,
                     time_to: Optional[float] = None,
                    min_support: int = 1, min_confidence: float = 0.0,
                    include_weak_cooccurrence: bool = False,
                    include_code: bool = False,
                    connected_only: bool = False,
                    include_history: bool = False,
                    include_memory_nodes: bool = True,
                    node_limit: Optional[int] = None,
                    edge_limit: Optional[int] = None) -> dict:
        started = time.perf_counter()
        clean_workspace = self._clean_ws(workspace)
        clean_level = _clean_text(
            level, field="level", max_chars=32
        ).lower()
        if clean_level not in {"overview", "system", "neighborhood", "path", "complete"}:
            raise ValidationError(
                "level must be one of: overview, system, neighborhood, path, complete"
            )
        for field, value in (
            ("connected_only", connected_only),
            ("include_history", include_history),
            ("include_memory_nodes", include_memory_nodes),
        ):
            if not isinstance(value, bool):
                raise ValidationError(f"{field} must be a boolean")
        if clean_level != "complete" and not include_memory_nodes:
            raise ValidationError("include_memory_nodes is only accepted for complete scenes")
        clean_center_id = (
            _clean_text(center_id, field="center_id", max_chars=MAX_NAME_CHARS)
            if center_id is not None else None
        )
        clean_system_id = (
            _clean_text(system_id, field="system_id", max_chars=MAX_NAME_CHARS)
            if system_id is not None else None
        )
        clean_seeds = list(dict.fromkeys(_clean_string_list(
            seeds, field="seeds", max_items=64, max_chars=MAX_NAME_CHARS,
        ))) if seeds is not None else []
        clean_repo = _clean_name(repo, field="repo") if repo is not None else None
        clean_relations = sorted(set(_clean_string_list(
            relations, field="relations", max_items=64, max_chars=MAX_NAME_CHARS,
        ))) if relations is not None else []
        clean_entity_types = sorted(set(_clean_string_list(
            entity_types, field="entity_types", max_items=64,
            max_chars=MAX_NAME_CHARS,
        ))) if entity_types is not None else []
        clean_memory_types = sorted({
            _enum(value, MemoryType, "memory_type").value
            for value in _clean_string_list(
                memory_types, field="memory_types", max_items=4,
                max_chars=MAX_NAME_CHARS,
            )
        }) if memory_types is not None else []
        clean_layers = None
        if layers is not None:
            layer_values = _clean_string_list(
                layers, field="layers", max_items=64, max_chars=MAX_NAME_CHARS,
            )
            clean_layers = sorted({
                _enum(value, GraphLayer, "layer").value for value in layer_values
            })

        def bounded_int(value: Any, field: str, minimum: int, maximum: int) -> int:
            try:
                parsed = int(value)
            except (TypeError, ValueError, OverflowError):
                raise ValidationError(f"{field} must be an integer")
            if parsed < minimum or parsed > maximum:
                raise ValidationError(f"{field} must be between {minimum} and {maximum}")
            return parsed

        clean_depth = bounded_int(depth, "depth", 0, 2)
        clean_min_support = bounded_int(min_support, "min_support", 0, 1_000_000)
        clean_node_limit = (
            bounded_int(node_limit, "node_limit", 1, 1000)
            if node_limit is not None else None
        )
        clean_edge_limit = (
            bounded_int(edge_limit, "edge_limit", 0, 2000)
            if edge_limit is not None else None
        )
        if clean_level == "complete" and (
            clean_node_limit is not None or clean_edge_limit is not None
        ):
            raise ValidationError(
                "complete scenes do not accept node_limit or edge_limit; "
                "use graph filters instead of silently truncating the chart"
            )
        try:
            clean_min_confidence = float(min_confidence)
        except (TypeError, ValueError, OverflowError):
            raise ValidationError("min_confidence must be a finite number")
        if not math.isfinite(clean_min_confidence) or not 0.0 <= clean_min_confidence <= 1.0:
            raise ValidationError("min_confidence must be between 0 and 1")
        clean_as_of, clean_valid_at, clean_known_at = _temporal_anchors(
            as_of=as_of, valid_at=valid_at, known_at=known_at
        )
        try:
            clean_time_from = float(time_from) if time_from is not None else None
            clean_time_to = float(time_to) if time_to is not None else None
        except (TypeError, ValueError, OverflowError):
            raise ValidationError("time range values must be finite timestamps")
        if ((clean_time_from is not None and not math.isfinite(clean_time_from))
                or (clean_time_to is not None and not math.isfinite(clean_time_to))):
            raise ValidationError("time range values must be finite timestamps")
        if (clean_time_from is not None and clean_time_to is not None
                and clean_time_from > clean_time_to):
            raise ValidationError("time_from must be less than or equal to time_to")

        cache_workspace_id = self._lookup_workspace(clean_workspace)
        if cache_workspace_id:
            self._assert_graph_index_ready(cache_workspace_id)
        revision = self._graph_scene_revision()
        cache_key = (
            revision, clean_workspace, clean_level, clean_center_id or "",
            clean_system_id or "", tuple(clean_seeds), clean_repo or "",
            tuple(clean_layers or ()), tuple(clean_relations), tuple(clean_entity_types),
            tuple(clean_memory_types), clean_as_of, clean_valid_at, clean_known_at,
            clean_time_from, clean_time_to,
            clean_depth, clean_min_support,
            clean_min_confidence, bool(include_weak_cooccurrence),
            bool(include_code), connected_only, include_history,
            include_memory_nodes, clean_node_limit, clean_edge_limit,
            GRAPH_SCENE_ALGORITHM_VERSION,
        )
        cached = self._graph_scene_cache.get(cache_key)
        # Cache hit only when both temporal axes are anchored (making the scene
        # a fixed historical query) or before the computed expiry deadline.
        # When known_at floats (defaults to system time), ghost state evolves as
        # system time crosses future valid_to_recorded_at boundaries.
        both_anchored = (
            clean_valid_at is not None and clean_known_at is not None
        )
        if cached is not None and (
                both_anchored or time.time() < cached[0]):
            self._graph_scene_cache.move_to_end(cache_key)
            scene = copy.deepcopy(cached[1])
            scene["meta"]["cache_hit"] = True
            scene["meta"]["query_ms"] = round(
                (time.perf_counter() - started) * 1000.0, 3
            )
            return scene
        if cached is not None:
            del self._graph_scene_cache[cache_key]
        present = time.time()
        query_at = clean_valid_at if clean_valid_at is not None else present
        query_known_at = clean_known_at if clean_known_at is not None else present
        (ws, _wid, entities, edges, supports, memories, memory_links,
         code_memory_links, index_info) = self._graph_scene_rows(
            workspace=clean_workspace, repo=clean_repo,
            valid_at=query_at, known_at=query_known_at,
            entity_types=clean_entity_types, memory_types=clean_memory_types,
            time_from=clean_time_from, time_to=clean_time_to,
            include_weak_cooccurrence=include_weak_cooccurrence,
            include_code=include_code,
            include_complete_rows=clean_level == "complete",
            include_history=include_history,
            include_memory_nodes=include_memory_nodes,
        )
        selected_layers = set(clean_layers) if clean_layers is not None else None
        selected_relations = set(clean_relations) or None
        filters = {
            "repo": clean_repo,
            "layers": sorted(selected_layers) if selected_layers is not None else None,
            "relations": sorted(selected_relations) if selected_relations else None,
            "entity_types": clean_entity_types,
            "memory_types": clean_memory_types,
            "as_of": clean_as_of,
            "valid_at": clean_valid_at,
            "known_at": clean_known_at,
            "time_from": clean_time_from,
            "time_to": clean_time_to,
            "min_support": clean_min_support,
            "min_confidence": clean_min_confidence,
            "include_weak_cooccurrence": bool(include_weak_cooccurrence),
            "include_code": bool(include_code),
            "connected_only": connected_only,
            "include_history": include_history,
            "include_memory_nodes": include_memory_nodes,
        }
        filters = {key: value for key, value in filters.items()
                   if key in {"connected_only", "include_history", "include_memory_nodes"}
                   or value not in (None, [], False)}
        scene = build_graph_scene(
            ws, entities, edges, supports, level=clean_level,
            memory_rows=memories, memory_link_rows=memory_links,
            code_memory_link_rows=code_memory_links,
            center_id=clean_center_id, system_id=clean_system_id,
            seeds=clean_seeds, depth=clean_depth,
            node_limit=clean_node_limit, edge_limit=clean_edge_limit,
            include_weak_cooccurrence=include_weak_cooccurrence,
            layers=selected_layers, relations=selected_relations,
            min_support=clean_min_support, min_confidence=clean_min_confidence,
            connected_only=connected_only, include_history=include_history,
            include_memory_nodes=include_memory_nodes,
            filters=filters, index_generation=int(index_info["generation"]),
        )
        scene["meta"]["index_state"] = index_info["state"]
        scene["meta"]["query_ms"] = round((time.perf_counter() - started) * 1000.0, 3)
        scene["meta"]["cache_hit"] = False
        if clean_level == "complete":
            scene["meta"]["safety_limits"] = {
                "entity_rows": MAX_GRAPH_ANALYSIS_ENTITIES,
                "raw_relations": MAX_GRAPH_ANALYSIS_EDGES,
                "evidence_rows": MAX_GRAPH_ANALYSIS_SUPPORTS,
                "memory_nodes": MAX_GRAPH_COMPLETE_MEMORIES,
                "memory_connectors": MAX_GRAPH_COMPLETE_MEMORY_LINKS,
                "code_memory_connectors": MAX_GRAPH_COMPLETE_CODE_MEMORY_LINKS,
                "payload_bytes": MAX_GRAPH_COMPLETE_PAYLOAD_BYTES,
            }
            payload_bytes = len(json.dumps(
                scene, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8"))
            if payload_bytes > MAX_GRAPH_COMPLETE_PAYLOAD_BYTES:
                raise GraphSceneCapacityExceeded(
                    resource="payload bytes", count=payload_bytes,
                    limit=MAX_GRAPH_COMPLETE_PAYLOAD_BYTES,
                )
            scene["meta"]["payload_bytes_estimate"] = payload_bytes
        # Indefinite cache only when both temporal axes are anchored, making the
        # scene a fixed historical query whose content cannot change. When
        # known_at floats (defaults to system time), ghost state evolves as
        # system time crosses future valid_to_recorded_at boundaries — the scene
        # must expire at those boundaries.
        valid_until = (
            math.inf if (
                clean_valid_at is not None and clean_known_at is not None
            ) or not _wid
            else self._graph_scene_valid_until(
                _wid, query_at, known_at=query_known_at,
                world_time_floating=clean_valid_at is None,
                system_time_floating=clean_known_at is None,
            )
        )
        # One complete scene can be many megabytes.  Keep at most one in the shared
        # LRU while retaining the normal 16-entry budget for compact analytical views.
        if clean_level == "complete":
            for key in [key for key in self._graph_scene_cache if key[2] == "complete"]:
                self._graph_scene_cache.pop(key, None)
        self._graph_scene_cache[cache_key] = (valid_until, copy.deepcopy(scene))
        self._graph_scene_cache.move_to_end(cache_key)
        while len(self._graph_scene_cache) > 16:
            self._graph_scene_cache.popitem(last=False)
        return scene

    def graph_suggest(self, query: str, *, workspace: str, limit: int = 8,
                      repo: Optional[str] = None,
                      memory_types: Optional[list[str]] = None,
                      as_of: Optional[float] = None,
                      valid_at: Optional[float] = None,
                      known_at: Optional[float] = None,
                      time_from: Optional[float] = None,
                      time_to: Optional[float] = None,
                      include_weak_cooccurrence: bool = False) -> dict:
        clean_query = _clean_text(
            query, field="query", max_chars=1_000, required=False
        )
        ws = self._clean_ws(workspace)
        wid = self._lookup_workspace(ws)
        limit = max(1, min(25, int(limit)))
        needle = normalize_entity_name(clean_query)
        empty_groups = {
            "systems": [], "entities": [], "memories": [], "repositories": [],
            "relations": [], "code_symbols": [],
        }
        if not wid:
            return {"workspace": ws, "query": clean_query, "groups": empty_groups}
        self._assert_graph_index_ready(wid)
        repo_id = None
        if repo:
            clean_repo = _clean_name(repo, field="repo")
            repo_id = self._lookup_repo(wid, clean_repo)
            if repo_id is None:
                raise ValidationError(f"no repo named '{clean_repo}' in workspace '{ws}'")
        as_of, valid_at, known_at = _temporal_anchors(
            as_of=as_of, valid_at=valid_at, known_at=known_at
        )
        present = time.time()
        suggestion_at = valid_at if valid_at is not None else present
        suggestion_known_at = known_at if known_at is not None else present
        try:
            lower_time = float(time_from) if time_from is not None else None
            upper_time = float(time_to) if time_to is not None else None
        except (TypeError, ValueError, OverflowError):
            raise ValidationError("graph suggestion times must be finite timestamps")
        if ((lower_time is not None and not math.isfinite(lower_time))
                or (upper_time is not None and not math.isfinite(upper_time))):
            raise ValidationError("graph suggestion times must be finite timestamps")
        if lower_time is not None and upper_time is not None and lower_time > upper_time:
            raise ValidationError("time_from must be less than or equal to time_to")
        clean_memory_types = sorted({
            _enum(value, MemoryType, "memory_type").value
            for value in _clean_string_list(
                memory_types, field="memory_types", max_items=4,
                max_chars=MAX_NAME_CHARS,
            )
        }) if memory_types is not None else []
        escaped = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"%{escaped}%"
        prefix = f"{escaped}%"

        def visible_sql(alias: str) -> tuple[str, list[float]]:
            prefix = f"{alias}."
            return (
                f"({prefix}valid_from IS NULL OR {prefix}valid_from<=?) "
                f"AND ({prefix}valid_to IS NULL OR ?<{prefix}valid_to "
                f"OR ({prefix}valid_to_recorded_at IS NOT NULL "
                f"AND ?<{prefix}valid_to_recorded_at)) "
                f"AND ({prefix}ingested_at IS NULL OR {prefix}ingested_at<=?) "
                f"AND ({prefix}expired_at IS NULL OR ?<{prefix}expired_at)",
                [
                    suggestion_at, suggestion_at, suggestion_known_at,
                    suggestion_known_at, suggestion_known_at,
                ],
            )

        # Search identity rows directly instead of rebuilding Louvain/PageRank for each
        # keystroke. A canonical entity id also resolves to its current deterministic
        # community in ``build_graph_scene``, so the same stable result can represent an
        # entity or a system without maintaining a second search index.
        entity_sql = (
            "SELECT id, canonical_id, name, normalized_name, etype, repo_id "
            "FROM entities entity WHERE workspace_id=? AND ("
            "normalized_name LIKE ? ESCAPE '\\' OR canonical_id=? OR id=?) AND "
            + _graph_entity_visibility_sql("entity", at=suggestion_at)
            + " AND (created_at IS NULL OR created_at<=?)"
        )
        entity_params: list[Any] = [
            wid, like, clean_query, clean_query, suggestion_known_at,
        ]
        if repo_id:
            entity_sql += " AND (repo_id=? OR repo_id IS NULL)"
            entity_params.append(repo_id)
        entity_sql += (
            " ORDER BY CASE WHEN canonical_id=? OR id=? THEN -1 "
            "WHEN normalized_name=? THEN 0 "
            "WHEN normalized_name LIKE ? ESCAPE '\\' THEN 1 ELSE 2 END, "
            "length(normalized_name), normalized_name, id LIMIT 500"
        )
        entity_params.extend((clean_query, clean_query, needle, prefix))
        matched_rows = [dict(row) for row in self.store.conn.execute(
            entity_sql, entity_params,
        ).fetchall()]
        matched_by_canonical: dict[str, list[dict]] = {}
        for row in matched_rows:
            canonical_id = str(row.get("canonical_id") or row["id"])
            matched_by_canonical.setdefault(canonical_id, []).append(row)

        def entity_rank(item: tuple[str, list[dict]]) -> tuple:
            canonical_id, rows = item
            exact_id = canonical_id == clean_query or any(
                str(row["id"]) == clean_query for row in rows
            )
            best = min(rows, key=lambda row: (
                0 if row["normalized_name"] == needle else
                1 if str(row["normalized_name"]).startswith(needle) else 2,
                len(str(row["normalized_name"])), str(row["normalized_name"]), row["id"],
            ))
            return (
                -1 if exact_id else
                0 if best["normalized_name"] == needle else
                1 if str(best["normalized_name"]).startswith(needle) else 2,
                len(str(best["normalized_name"])),
                str(best["normalized_name"]), canonical_id,
            )

        ranked_identity_items = sorted(matched_by_canonical.items(), key=entity_rank)

        def useful_identity(item: tuple[str, list[dict]]) -> bool:
            """Keep search useful without making extractor fragments undiscoverable.

            Exact label queries remain available by stable canonical id.  For broader
            prefix/substring searches, however, sentence fragments such as ``If Python``
            and ``Python-based`` must not crowd out the actual ``Python`` entity.
            """
            _canonical_id, rows = item
            best = min(rows, key=lambda row: (
                0 if row["normalized_name"] == needle else
                1 if str(row["normalized_name"]).startswith(needle) else 2,
                len(str(row["normalized_name"])), str(row["normalized_name"]), row["id"],
            ))
            exact = (
                _canonical_id == clean_query
                or any(str(row["id"]) == clean_query for row in rows)
                or str(best["normalized_name"]) == needle
            )
            return exact or not is_broad_search_fragment(
                str(best.get("name") or ""),
                str(best.get("etype") or "person_or_concept"),
            )

        selected_canonical_ids = [item[0] for item in ranked_identity_items
                                  if useful_identity(item)][:limit]
        member_rows: list[dict] = []
        if selected_canonical_ids:
            marks = ",".join("?" for _ in selected_canonical_ids)
            member_sql = (
                "SELECT id, canonical_id, name, normalized_name, etype, repo_id "
                f"FROM entities entity WHERE workspace_id=? "
                f"AND canonical_id IN ({marks}) AND "
                + _graph_entity_visibility_sql("entity", at=suggestion_at)
                + " AND (created_at IS NULL OR created_at<=?)"
            )
            member_params: list[Any] = [
                wid, *selected_canonical_ids, suggestion_known_at,
            ]
            if repo_id:
                member_sql += " AND (repo_id=? OR repo_id IS NULL)"
                member_params.append(repo_id)
            member_sql += " ORDER BY canonical_id, normalized_name, id"
            member_rows = [dict(row) for row in self.store.conn.execute(
                member_sql, member_params,
            ).fetchall()]
        members_by_canonical: dict[str, list[dict]] = {}
        member_to_canonical: dict[str, str] = {}
        for row in member_rows:
            canonical_id = str(row.get("canonical_id") or row["id"])
            members_by_canonical.setdefault(canonical_id, []).append(row)
            member_to_canonical[str(row["id"])] = canonical_id
        support_counts: Counter = Counter()
        member_ids = sorted(member_to_canonical)
        visible_member_ids: set[str] = set(member_ids)
        if member_ids:
            seen_supports: dict[str, set[str]] = {}
            for start in range(0, len(member_ids), 400):
                chunk = member_ids[start:start + 400]
                marks = ",".join("?" for _ in chunk)
                relation_visibility, relation_visibility_params = visible_sql("relation")
                support_visibility, support_visibility_params = visible_sql("support")
                memory_visibility, memory_visibility_params = visible_sql("memory")
                temporal_visibility = (
                    f"AND {relation_visibility} AND {support_visibility} "
                    f"AND {memory_visibility} "
                )
                support_sql = (
                    "SELECT endpoint, memory_id FROM ("
                    "SELECT relation.src AS endpoint, support.memory_id FROM edges relation "
                    "JOIN edge_supports support ON support.edge_id=relation.id "
                    "JOIN memories memory ON memory.id=support.memory_id "
                    f"WHERE relation.workspace_id=? AND relation.src IN ({marks}) "
                    + temporal_visibility +
                    "AND COALESCE(memory.scope, 'workspace')!='session' "
                    "UNION ALL "
                    "SELECT relation.dst AS endpoint, support.memory_id FROM edges relation "
                    "JOIN edge_supports support ON support.edge_id=relation.id "
                    "JOIN memories memory ON memory.id=support.memory_id "
                    f"WHERE relation.workspace_id=? AND relation.dst IN ({marks}) "
                    + temporal_visibility +
                    "AND COALESCE(memory.scope, 'workspace')!='session')"
                )
                temporal_params = [
                    *relation_visibility_params,
                    *support_visibility_params,
                    *memory_visibility_params,
                ]
                rows = self.store.conn.execute(
                    support_sql,
                    (
                        wid, *chunk, *temporal_params,
                        wid, *chunk, *temporal_params,
                    ),
                ).fetchall()
                for row in rows:
                    canonical_id = member_to_canonical.get(str(row["endpoint"]), "")
                    if canonical_id:
                        seen_supports.setdefault(canonical_id, set()).add(
                            str(row["memory_id"])
                        )
            support_counts.update({key: len(value) for key, value in seen_supports.items()})
        entity_results = []
        for canonical_id in selected_canonical_ids:
            rows = [
                row for row in (
                    members_by_canonical.get(canonical_id)
                    or matched_by_canonical[canonical_id]
                )
                if str(row["id"]) in visible_member_ids
            ]
            if not rows:
                continue
            best = min(rows, key=lambda row: (
                0 if row["normalized_name"] == needle else
                1 if str(row["normalized_name"]).startswith(needle) else 2,
                len(str(row["normalized_name"])), str(row["normalized_name"]), row["id"],
            ))
            aliases = sorted({str(row["name"]) for row in rows}, key=lambda value: (
                normalize_entity_name(value), value,
            ))
            types = Counter(str(row.get("etype") or "person_or_concept") for row in rows)
            entity_results.append({
                "id": canonical_id, "label": best["name"], "kind": "entity",
                "type": min(types, key=lambda value: (-types[value], value)),
                "aliases": aliases,
                "repo_ids": sorted({str(row["repo_id"]) for row in rows if row.get("repo_id")}),
                "support_count": int(support_counts.get(canonical_id, 0)),
            })
        system_results = [{
            "id": item["id"], "label": f"{item['label']} System", "kind": "system",
            "anchor_id": item["id"],
            "member_count": len([
                row for row in members_by_canonical.get(item["id"], [])
                if str(row["id"]) in visible_member_ids
            ]) or 1,
            "mass": float(item["support_count"]),
        } for item in entity_results]

        memories = []
        repositories = []
        relations_out = []
        code_symbols = []
        if wid:
            memory_visibility, memory_visibility_params = visible_sql("memories")
            memory_sql = (
                "SELECT id, title, content, mtype, repo_id FROM memories "
                f"WHERE workspace_id=? AND {memory_visibility} "
                "AND COALESCE(scope, 'workspace')!='session' "
                "AND (lower(title) LIKE ? ESCAPE '\\' OR lower(content) LIKE ? ESCAPE '\\')"
            )
            memory_params: list[Any] = [
                wid, *memory_visibility_params, like, like,
            ]
            if clean_memory_types:
                marks = ",".join("?" for _ in clean_memory_types)
                memory_sql += f" AND mtype IN ({marks})"
                memory_params.extend(clean_memory_types)
            if lower_time is not None:
                memory_sql += " AND COALESCE(valid_from, ingested_at, 0)>=?"
                memory_params.append(lower_time)
            if upper_time is not None:
                memory_sql += " AND COALESCE(valid_from, ingested_at, 0)<=?"
                memory_params.append(upper_time)
            if repo_id:
                memory_sql += " AND " + _repo_memory_scope_sql()
                memory_params.append(repo_id)
            memory_sql += (
                " ORDER BY COALESCE(last_access, valid_from, ingested_at) DESC, id LIMIT ?"
            )
            memory_params.append(limit)
            memory_rows = self.store.conn.execute(
                memory_sql, memory_params,
            ).fetchall()
            memories = [{
                "id": row["id"], "label": row["title"] or str(row["content"] or "")[:80],
                "kind": "memory", "type": row["mtype"], "repo_id": row["repo_id"],
            } for row in memory_rows]
            repo_rows = self.store.conn.execute(
                "SELECT id, name FROM repos WHERE workspace_id=? AND lower(name) LIKE ? ESCAPE '\\' "
                "ORDER BY name, id LIMIT ?", (wid, like, limit)
            ).fetchall()
            repositories = [{"id": row["id"], "label": row["name"], "kind": "repository"}
                            for row in repo_rows]
            relation_sql = (
                "SELECT relation, COUNT(*) AS count FROM edges relation_edge "
                "WHERE workspace_id=? "
                "AND relation LIKE ? ESCAPE '\\' "
            )
            relation_visibility, relation_visibility_params = visible_sql("relation_edge")
            support_visibility, support_visibility_params = visible_sql("suggest_support")
            support_memory_visibility, support_memory_visibility_params = visible_sql(
                "suggest_memory"
            )
            relation_sql += (
                f"AND {relation_visibility} AND ("
                "NOT EXISTS (SELECT 1 FROM edge_supports any_suggest_support "
                "WHERE any_suggest_support.edge_id=relation_edge.id) OR EXISTS ("
                "SELECT 1 FROM edge_supports suggest_support "
                "JOIN memories suggest_memory "
                "ON suggest_memory.id=suggest_support.memory_id "
                "WHERE suggest_support.edge_id=relation_edge.id "
                f"AND {support_visibility} AND {support_memory_visibility} "
                "AND COALESCE(suggest_memory.scope, 'workspace')!='session'))"
            )
            relation_params: list[Any] = [
                wid, like, *relation_visibility_params,
                *support_visibility_params, *support_memory_visibility_params,
            ]
            if repo_id:
                relation_sql += " AND (repo_id=? OR repo_id IS NULL)"
                relation_params.append(repo_id)
            relation_sql += " GROUP BY relation ORDER BY count DESC, relation LIMIT ?"
            relation_params.append(limit)
            relations_out = [{
                "id": row["relation"], "label": row["relation"], "kind": "relation",
                "count": int(row["count"]),
            } for row in self.store.conn.execute(relation_sql, relation_params).fetchall()]
            symbol_sql = (
                "SELECT s.id, s.name, s.fqname, s.kind, s.repo_id, r.name AS repo_name "
                "FROM symbols s JOIN repos r ON r.id=s.repo_id "
                "WHERE r.workspace_id=? AND (lower(s.name) LIKE ? ESCAPE '\\' "
                "OR lower(s.fqname) LIKE ? ESCAPE '\\')"
            )
            symbol_params: list[Any] = [wid, like, like]
            symbol_visibility, symbol_visibility_params = visible_sql("s")
            symbol_sql += f" AND {symbol_visibility}"
            symbol_params.extend(symbol_visibility_params)
            if repo_id:
                symbol_sql += " AND s.repo_id=?"
                symbol_params.append(repo_id)
            symbol_sql += " ORDER BY s.name, s.id LIMIT ?"
            symbol_params.append(limit)
            symbol_rows = self.store.conn.execute(symbol_sql, symbol_params).fetchall()
            code_symbols = [{
                "id": row["id"], "label": row["fqname"] or row["name"],
                "kind": "code_symbol", "type": row["kind"],
                "repo_id": row["repo_id"], "repo": row["repo_name"],
            } for row in symbol_rows]
        return {
            "workspace": ws, "query": clean_query,
            "as_of": as_of, "valid_at": valid_at, "known_at": known_at,
            "groups": {
                "systems": system_results, "entities": entity_results,
                "memories": memories, "repositories": repositories,
                "relations": relations_out, "code_symbols": code_symbols,
            },
        }

    def graph_entity(self, canonical_id: str, *, workspace: str,
                     repo: Optional[str] = None,
                     memory_types: Optional[list[str]] = None,
                     as_of: Optional[float] = None,
                     valid_at: Optional[float] = None,
                     known_at: Optional[float] = None,
                     time_from: Optional[float] = None,
                     time_to: Optional[float] = None,
                     include_weak_cooccurrence: bool = True) -> dict:
        clean_canonical_id = _clean_text(
            canonical_id, field="canonical_id", max_chars=MAX_NAME_CHARS
        )
        as_of, valid_at, known_at = _temporal_anchors(
            as_of=as_of, valid_at=valid_at, known_at=known_at
        )
        history_known_at = known_at if known_at is not None else time.time()
        (ws, wid, entities, edges, supports, _memories, _memory_links,
         _code_memory_links, _index_info) = self._graph_scene_rows(
            workspace=workspace, repo=repo, as_of=as_of,
            valid_at=valid_at, known_at=known_at,
            memory_types=memory_types, time_from=time_from, time_to=time_to,
            include_weak_cooccurrence=include_weak_cooccurrence,
        )
        graph = build_canonical_graph(
            entities, edges, supports,
            include_weak_cooccurrence=include_weak_cooccurrence, min_support=0,
        )
        resolved = graph["member_to_canonical"].get(
            clean_canonical_id, clean_canonical_id
        )
        node = graph["nodes"].get(resolved)
        if node is None:
            raise ValidationError(
                f"no entity '{clean_canonical_id}' in workspace '{ws}'"
            )
        repo_names = {row["id"]: row["name"] for row in self.store.conn.execute(
            "SELECT id, name FROM repos WHERE workspace_id=?", (wid,)
        ).fetchall()} if wid else {}
        relations_out = []
        connected_edge_ids: set[str] = set()
        memory_ids: set[str] = set()
        for edge in graph["edges"]:
            if resolved not in {edge["source"], edge["target"]}:
                continue
            direction = "outgoing" if edge["source"] == resolved else "incoming"
            other_id = edge["target"] if direction == "outgoing" else edge["source"]
            relations_out.append({
                **{key: value for key, value in edge.items()
                   if key not in {"support_memory_ids"} and not key.startswith("_")},
                "direction": direction, "other_id": other_id,
                "other_label": graph["nodes"][other_id]["label"],
            })
            connected_edge_ids.update(edge["_underlying_edge_ids_all"])
            memory_ids.update(edge["_support_ids_all"])
        support_map: dict[str, dict] = {}
        for row in supports:
            if row["edge_id"] not in connected_edge_ids:
                continue
            memory_id = str(row["memory_id"])
            current = support_map.get(memory_id)
            if current is None or float(row.get("confidence") or 0.0) > float(
                    current.get("confidence") or 0.0):
                support_map[memory_id] = row
        relation_total = len(relations_out)
        layer_order = {"causal": 0, "entity": 1, "temporal": 2, "semantic": 3}
        relations_out = sorted(relations_out, key=lambda item: (
            item["relation"] == "co_occurs",
            layer_order.get(item["layer"], 4),
            item["direction"],
            -item["strength"],
            item["id"],
        ))[:GRAPH_ENTITY_RELATION_LIMIT]
        evidence = []
        if memory_ids:
            ordered_ids = sorted(memory_ids, key=lambda memory_id: (
                -float(support_map.get(memory_id, {}).get("confidence") or 0.0),
                memory_id,
            ))[:GRAPH_ENTITY_EVIDENCE_LIMIT]
            for start in range(0, len(ordered_ids), 500):
                chunk = ordered_ids[start:start + 500]
                marks = ",".join("?" for _ in chunk)
                for memory in self.store.conn.execute(
                    "SELECT id, title, content, mtype, valid_from, valid_to, "
                    "valid_to_recorded_at, ingested_at, expired_at, provenance "
                    "FROM memories WHERE workspace_id=? "
                    "AND COALESCE(scope, 'workspace')!='session' "
                    "AND id IN (" + marks + ") "
                    "ORDER BY id", (wid, *chunk)
                ).fetchall():
                    support = support_map.get(memory["id"], {})
                    try:
                        memory_provenance = json.loads(memory["provenance"] or "{}")
                    except (TypeError, ValueError, RecursionError):
                        memory_provenance = {}
                    if not isinstance(memory_provenance, dict):
                        memory_provenance = {}
                    evidence.append({
                        "memory_id": memory["id"], "title": memory["title"] or "",
                        "excerpt": str(memory["content"] or "")[:500],
                        "memory_type": memory["mtype"],
                        "source_kind": support.get("source_kind", "legacy_unknown"),
                        "confidence": float(support.get("confidence", 0.5)),
                        "valid_from": memory["valid_from"], "valid_to": memory["valid_to"],
                        "valid_to_recorded_at": memory["valid_to_recorded_at"],
                        "ingested_at": memory["ingested_at"], "expired_at": memory["expired_at"],
                        "provenance": memory_provenance,
                    })
        evidence.sort(key=lambda item: (
            -float(item["confidence"]),
            -float(item.get("valid_from") or item.get("ingested_at") or 0.0),
            item["memory_id"],
        ))
        member_ids = node["member_ids"]
        history_filter = (
            "workspace_id=? AND (ingested_at IS NULL OR ingested_at<=?) "
            "AND ((valid_to IS NOT NULL AND "
            "(valid_to_recorded_at IS NULL OR valid_to_recorded_at<=?)) "
            "OR (expired_at IS NOT NULL AND expired_at<=?)) "
            "AND (src IN (SELECT id FROM entities WHERE workspace_id=? AND canonical_id=?) "
            "OR dst IN (SELECT id FROM entities WHERE workspace_id=? AND canonical_id=?))"
        )
        history_params: tuple[Any, ...] = (
            wid, history_known_at, history_known_at, history_known_at,
            wid, resolved, wid, resolved,
        )
        history_filter += (
            " AND (NOT EXISTS (SELECT 1 FROM edge_supports any_history_support "
            "WHERE any_history_support.edge_id=edges.id) OR EXISTS ("
            "SELECT 1 FROM edge_supports history_support "
            "JOIN memories history_memory "
            "ON history_memory.id=history_support.memory_id "
            "WHERE history_support.edge_id=edges.id "
            "AND history_memory.workspace_id=? "
            "AND COALESCE(history_memory.scope, 'workspace')!='session'))"
        )
        history_params = (*history_params, wid)
        history_total = int(self.store.conn.execute(
            f"SELECT COUNT(*) AS n FROM edges WHERE {history_filter}", history_params
        ).fetchone()["n"])
        history = [dict(row) for row in self.store.conn.execute(
            "SELECT id, src, dst, relation, layer, weight, valid_from, valid_to, "
            "valid_to_recorded_at, ingested_at, expired_at FROM edges WHERE "
            + history_filter + " "
            "ORDER BY COALESCE(valid_to, expired_at, valid_from, ingested_at) DESC, id DESC "
            "LIMIT ?",
            (*history_params, GRAPH_ENTITY_HISTORY_LIMIT),
        ).fetchall()]
        for item in history:
            item["event"] = "Relation invalidated" if item.get("valid_to") is not None else (
                "Relation expired"
            )
        return {
            "workspace": ws, "canonical_id": resolved, "label": node["label"],
            "type": node["type"], "member_ids": member_ids,
            "aliases": node.get("aliases", []),
            "repositories": [{"id": repo_id, "name": repo_names.get(repo_id, repo_id)}
                             for repo_id in node["repo_ids"]],
            "mass": {key: node[key] for key in (
                "mass_score", "gravity_mass", "visual_radius", "weighted_degree",
                "pagerank", "support_count", "anchor_role", "core_affinity"
            )},
            "relations": relations_out,
            "evidence": evidence, "history": history,
            "totals": {
                "relations": relation_total,
                "evidence": len(memory_ids),
                "history": history_total,
            },
            "truncation": {
                "relations": relation_total > len(relations_out),
                "evidence": len(memory_ids) > len(evidence),
                "history": history_total > len(history),
            },
            "as_of": as_of,
            "valid_at": valid_at,
            "known_at": known_at,
        }

    def graph_entity_evidence(self, canonical_id: str, *, workspace: str,
                              repo: Optional[str] = None,
                              as_of: Optional[float] = None,
                              valid_at: Optional[float] = None,
                              known_at: Optional[float] = None,
                              member_id: Optional[str] = None,
                              include_history: bool = False) -> dict:
        """Return one graph entity's public supporting memories without rebuilding the graph.

        Historical scenes project colliding canonical IDs as synthetic ``:ghost`` IDs.
        Those IDs are not reversible when a real canonical ID has the same suffix, so
        callers carry one physical member ID to identify the historical canonical group.
        """
        clean_canonical_id = _clean_text(
            canonical_id, field="canonical_id", max_chars=MAX_NAME_CHARS
        )
        if member_id is not None and clean_canonical_id.endswith(":ghost"):
            clean_canonical_id = clean_canonical_id[:-6]
        clean_member_id = (
            _clean_text(member_id, field="member_id", max_chars=MAX_NAME_CHARS)
            if member_id is not None else None
        )
        ws = self._clean_ws(workspace)
        wid = self._lookup_workspace(ws)
        if wid is None:
            raise ValidationError(f"no workspace '{ws}'")
        self._assert_graph_index_ready(wid)
        repo_id = None
        clean_repo = None
        if repo:
            clean_repo = _clean_name(repo, field="repo")
            repo_id = self._lookup_repo(wid, clean_repo)
            if repo_id is None:
                raise ValidationError(f"no repo named '{clean_repo}' in workspace '{ws}'")
        as_of, valid_at, known_at = _temporal_anchors(
            as_of=as_of, valid_at=valid_at, known_at=known_at
        )
        present = time.time()
        anchor = valid_at if valid_at is not None else present
        known_anchor = known_at if known_at is not None else present

        target = None
        if clean_member_id is not None:
            target = self.store.conn.execute(
                "SELECT id, canonical_id FROM entities "
                "WHERE workspace_id=? AND id=? LIMIT 1",
                (wid, clean_member_id),
            ).fetchone()
        if target is None and clean_member_id is None:
            target = self.store.conn.execute(
                "SELECT id, canonical_id FROM entities WHERE workspace_id=? AND id=? LIMIT 1",
                (wid, clean_canonical_id),
            ).fetchone()
        if target is None and clean_member_id is None:
            target = self.store.conn.execute(
                "SELECT id, canonical_id FROM entities WHERE workspace_id=? "
                "AND canonical_id=? LIMIT 1",
                (wid, clean_canonical_id),
            ).fetchone()
        if target is None:
            missing_id = clean_member_id or clean_canonical_id
            raise ValidationError(
                f"no entity '{missing_id}' in workspace '{ws}'"
            )
        resolved_canonical_id = str(target["canonical_id"] or target["id"])
        target_params = (wid, resolved_canonical_id, resolved_canonical_id)

        if include_history:
            support_conditions = (
                "relation.workspace_id=? AND relation.{endpoint}=target.id "
                "AND (relation.valid_from IS NULL OR relation.valid_from<=?) "
                "AND ("
                "(relation.valid_to IS NOT NULL AND relation.valid_to<=? "
                "AND (relation.valid_to_recorded_at IS NULL "
                "OR relation.valid_to_recorded_at<=?)) "
                "OR (support.valid_to IS NOT NULL AND support.valid_to<=? "
                "AND (support.valid_to_recorded_at IS NULL "
                "OR support.valid_to_recorded_at<=?)) "
                "OR (memory.valid_to IS NOT NULL AND memory.valid_to<=? "
                "AND (memory.valid_to_recorded_at IS NULL "
                "OR memory.valid_to_recorded_at<=?))"
                ") "
                "AND (relation.ingested_at IS NULL OR relation.ingested_at<=?) "
                "AND (relation.expired_at IS NULL OR ?<relation.expired_at) "
                "AND (support.valid_from IS NULL OR support.valid_from<=?) "
                "AND (support.ingested_at IS NULL OR support.ingested_at<=?) "
                "AND (support.expired_at IS NULL OR ?<support.expired_at) "
                "AND memory.workspace_id=? "
                "AND (memory.valid_from IS NULL OR memory.valid_from<=?) "
                "AND (memory.ingested_at IS NULL OR memory.ingested_at<=?) "
                "AND (memory.expired_at IS NULL OR ?<memory.expired_at) "
                "AND COALESCE(memory.scope, 'workspace')!='session'"
            )
            branch_params = (
                wid, anchor, anchor, known_anchor, anchor, known_anchor,
                anchor, known_anchor, known_anchor, known_anchor,
                anchor, known_anchor, known_anchor,
                wid, anchor, known_anchor, known_anchor,
            )
        else:
            support_conditions = (
                "relation.workspace_id=? AND relation.{endpoint}=target.id "
                "AND (relation.valid_from IS NULL OR relation.valid_from<=?) "
                "AND (relation.valid_to IS NULL OR ?<relation.valid_to "
                "OR (relation.valid_to_recorded_at IS NOT NULL "
                "AND ?<relation.valid_to_recorded_at)) "
                "AND (relation.ingested_at IS NULL OR relation.ingested_at<=?) "
                "AND (relation.expired_at IS NULL OR ?<relation.expired_at) "
                "AND (support.valid_from IS NULL OR support.valid_from<=?) "
                "AND (support.valid_to IS NULL OR ?<support.valid_to "
                "OR (support.valid_to_recorded_at IS NOT NULL "
                "AND ?<support.valid_to_recorded_at)) "
                "AND (support.ingested_at IS NULL OR support.ingested_at<=?) "
                "AND (support.expired_at IS NULL OR ?<support.expired_at) "
                "AND memory.workspace_id=? "
                "AND (memory.valid_from IS NULL OR memory.valid_from<=?) "
                "AND (memory.valid_to IS NULL OR ?<memory.valid_to "
                "OR (memory.valid_to_recorded_at IS NOT NULL "
                "AND ?<memory.valid_to_recorded_at)) "
                "AND (memory.ingested_at IS NULL OR memory.ingested_at<=?) "
                "AND (memory.expired_at IS NULL OR ?<memory.expired_at) "
                "AND COALESCE(memory.scope, 'workspace')!='session'"
            )
            branch_params = (
                wid,
                anchor, anchor, known_anchor, known_anchor, known_anchor,
                anchor, anchor, known_anchor, known_anchor, known_anchor,
                wid,
                anchor, anchor, known_anchor, known_anchor, known_anchor,
            )
        if repo_id is not None:
            support_conditions = support_conditions.replace(
                "relation.workspace_id=? AND relation.{endpoint}=target.id ",
                "relation.workspace_id=? AND (relation.repo_id=? OR relation.repo_id IS NULL) "
                "AND relation.{endpoint}=target.id ",
            ).replace(
                "AND memory.workspace_id=? ",
                "AND memory.workspace_id=? AND " + _repo_memory_scope_sql("memory") + " ",
            )
            if include_history:
                branch_params = (
                    wid, repo_id, anchor, anchor, known_anchor, anchor, known_anchor,
                    anchor, known_anchor, known_anchor, known_anchor,
                    anchor, known_anchor, known_anchor,
                    wid, repo_id, anchor, known_anchor, known_anchor,
                )
            else:
                branch_params = (
                    wid, repo_id, anchor, anchor, known_anchor, known_anchor, known_anchor,
                    anchor, anchor, known_anchor, known_anchor, known_anchor,
                    wid, repo_id, anchor, anchor, known_anchor, known_anchor, known_anchor,
                )
        source_conditions = support_conditions.format(endpoint="src")
        target_conditions = support_conditions.format(endpoint="dst")
        sql = """
            WITH target AS (
                SELECT id FROM entities WHERE workspace_id=? AND (id=? OR canonical_id=?)
            ), raw_supports AS (
                SELECT * FROM (
                    SELECT support.memory_id, support.confidence
                    FROM target
                    JOIN edges relation ON relation.src=target.id
                    JOIN edge_supports support ON support.edge_id=relation.id
                    JOIN memories memory ON memory.id=support.memory_id
                    WHERE {source_conditions}
                    LIMIT ?
                )
                UNION ALL
                SELECT * FROM (
                    SELECT support.memory_id, support.confidence
                    FROM target
                    JOIN edges relation ON relation.dst=target.id
                    JOIN edge_supports support ON support.edge_id=relation.id
                    JOIN memories memory ON memory.id=support.memory_id
                    WHERE {target_conditions}
                    LIMIT ?
                )
            ), ranked AS (
                SELECT memory_id, MAX(confidence) AS confidence
                FROM raw_supports GROUP BY memory_id
            )
            SELECT memory.id, memory.title, memory.content, memory.mtype,
                   memory.valid_from, memory.valid_to, memory.valid_to_recorded_at,
                   memory.ingested_at,
                   memory.expired_at, memory.provenance, ranked.confidence
            FROM ranked JOIN memories memory ON memory.id=ranked.memory_id
            WHERE memory.workspace_id=?
            ORDER BY ranked.confidence DESC,
                     COALESCE(memory.valid_from, memory.ingested_at, 0) DESC,
                     memory.id
            LIMIT ?
        """.format(
            source_conditions=source_conditions,
            target_conditions=target_conditions,
        )
        rows = self.store.conn.execute(
            sql,
            (*target_params, *branch_params, GRAPH_ENTITY_EVIDENCE_CANDIDATE_LIMIT,
             *branch_params, GRAPH_ENTITY_EVIDENCE_CANDIDATE_LIMIT, wid,
             GRAPH_ENTITY_EVIDENCE_LIMIT + 1),
        ).fetchall()
        truncated = len(rows) > GRAPH_ENTITY_EVIDENCE_LIMIT
        rows = rows[:GRAPH_ENTITY_EVIDENCE_LIMIT]
        evidence = []
        for row in rows:
            try:
                provenance = json.loads(row["provenance"] or "{}")
            except (TypeError, ValueError, RecursionError):
                provenance = {}
            if not isinstance(provenance, dict):
                provenance = {}
            evidence.append({
                "memory_id": row["id"], "title": row["title"] or "",
                "excerpt": str(row["content"] or "")[:500],
                "memory_type": row["mtype"], "source_kind": "graph_support",
                "confidence": max(0.0, min(1.0, _finite_float(row["confidence"], 0.0))),
                "valid_from": row["valid_from"], "valid_to": row["valid_to"],
                "valid_to_recorded_at": row["valid_to_recorded_at"],
                "ingested_at": row["ingested_at"], "expired_at": row["expired_at"],
                "provenance": provenance,
            })
        return {
            "workspace": ws, "canonical_id": resolved_canonical_id,
            "repo": clean_repo,
            "evidence": evidence,
            # This count is intentionally response-local: exact global totals would require
            # scanning every support of a hub node and defeat the click path's hard budget.
            "totals": {"evidence": len(evidence)},
            "truncation": {"evidence": truncated},
            "as_of": as_of,
            "valid_at": valid_at,
            "known_at": known_at,
        }

    def graph_path(self, source: str, target: str, *, workspace: str,
                   repo: Optional[str] = None, as_of: Optional[float] = None,
                   valid_at: Optional[float] = None,
                   known_at: Optional[float] = None,
                   memory_types: Optional[list[str]] = None,
                   time_from: Optional[float] = None,
                   time_to: Optional[float] = None,
                   max_hops: int = 8, max_visits: int = 10_000,
                   include_weak_cooccurrence: bool = False) -> dict:
        clean_source = _clean_text(
            source, field="source", max_chars=MAX_NAME_CHARS
        )
        clean_target = _clean_text(
            target, field="target", max_chars=MAX_NAME_CHARS
        )
        try:
            clean_max_hops = int(max_hops)
            clean_max_visits = int(max_visits)
        except (TypeError, ValueError, OverflowError):
            raise ValidationError("max_hops and max_visits must be integers")
        if not 1 <= clean_max_hops <= 8:
            raise ValidationError("max_hops must be between 1 and 8")
        if not 1 <= clean_max_visits <= 50_000:
            raise ValidationError("max_visits must be between 1 and 50000")
        (ws, _wid, entities, edges, supports, _memories, _memory_links,
         _code_memory_links, _index_info) = self._graph_scene_rows(
            workspace=workspace, repo=repo, as_of=as_of,
            valid_at=valid_at, known_at=known_at,
            memory_types=memory_types, time_from=time_from, time_to=time_to,
            include_weak_cooccurrence=include_weak_cooccurrence,
        )
        graph = build_canonical_graph(
            entities, edges, supports,
            include_weak_cooccurrence=include_weak_cooccurrence, min_support=0,
        )
        result = strongest_path(
            graph, clean_source, clean_target, max_hops=clean_max_hops,
            max_visits=clean_max_visits,
        )
        return {
            "workspace": ws, "source": clean_source, "target": clean_target, **result,
        }

    def graph(self, *, workspace: str, limit: int = 2000,
              layers: Optional[list] = None, include_code: bool = False,
              repo: Optional[str] = None, backfill: bool = True,
              full: bool = False, connected_only: bool = False,
              as_of: Optional[float] = None,
              valid_at: Optional[float] = None,
              known_at: Optional[float] = None) -> dict:
        """Entity-relation network for a workspace: nodes/edges plus type counts,
        top-connected entities, and connectivity stats — powers the Graph tab in
        both the v1-look dashboard and the Inspector UI (engraphis.graphdata
        shapes the rows so the two UIs can't drift). Same workspace-binding
        boundary as every other read: a bound instance refuses to read another
        tenant's graph even if the caller names it (SECURITY.md §3) — unlike the
        original dashboard-only implementation, which read the DB file directly
        and skipped this check entirely."""
        ws = self._clean_ws(workspace)  # binding enforced here, before any lookup
        as_of, valid_at, known_at = _temporal_anchors(
            as_of=as_of, valid_at=valid_at, known_at=known_at
        )
        wid = self._lookup_workspace(ws)
        if wid is None:
            return empty_graph(ws)
        self._assert_graph_index_ready(wid)
        # The dashboard's default overview remains compact. A user-requested full node
        # graph may use the analytical-scene ceiling, but never silently exceeds it: the
        # CTE below reports the visible total and we return an explicit capacity error when
        # a workspace needs filtering rather than pretending a truncated graph is complete.
        node_limit = MAX_GRAPH_ANALYSIS_ENTITIES if full else 5000
        limit = max(1, min(node_limit, int(limit)))
        conn = self.store.conn
        restrict_sessions = True
        present = time.time()
        world_anchor = valid_at if valid_at is not None else present
        system_anchor = known_at if known_at is not None else present
        # ``as_of``/``valid_at`` powers the Time view, which intentionally retains
        # superseded public relations as ghosts. A system-time-only read remains an
        # exact world-time snapshot while limiting the graph to what was then known.
        include_relation_history = valid_at is not None
        temporal_requested = valid_at is not None or known_at is not None
        selected_graph_layers = None
        selected_layers = None
        if layers is not None:
            selected_graph_layers = [
                _enum(layer, GraphLayer, "layer") for layer in layers
            ]
            selected_layers = {layer.value for layer in selected_graph_layers}

        selected_graph_layers = None
        selected_layers = None
        if layers is not None:
            selected_graph_layers = [
                _enum(layer, GraphLayer, "layer") for layer in layers
            ]
            selected_layers = {layer.value for layer in selected_graph_layers}

        def temporal_sql(alias: str, *, history: bool = False
                         ) -> tuple[str, list[float]]:
            """Parameterized world/system visibility for graph-owned SQL.

            ``history`` retains world-time closures for the Time view, but never
            relaxes system time: future ingestion and later expiry stay invisible.
            """
            prefix = f"{alias}."
            if history:
                world_sql = f"({prefix}valid_from IS NULL OR {prefix}valid_from<=?)"
                params: list[float] = [world_anchor]
            else:
                world_sql = (
                    f"({prefix}valid_from IS NULL OR {prefix}valid_from<=?) "
                    f"AND ({prefix}valid_to IS NULL OR ?<{prefix}valid_to "
                    f"OR ({prefix}valid_to_recorded_at IS NOT NULL "
                    f"AND ?<{prefix}valid_to_recorded_at))"
                )
                params = [world_anchor, world_anchor, system_anchor]
            return (
                world_sql
                + f" AND ({prefix}ingested_at IS NULL OR {prefix}ingested_at<=?)"
                + f" AND ({prefix}expired_at IS NULL OR ?<{prefix}expired_at)",
                [*params, system_anchor, system_anchor],
            )

        def public_edge_sql(alias: str, *, history: bool = False
                            ) -> tuple[str, list[float]]:
            """Evidence visibility for one edge alias, including session isolation."""
            support_sql, support_params = temporal_sql(
                "visibility_support", history=history
            )
            memory_sql, memory_params = temporal_sql(
                "visibility_memory", history=history
            )
            return (
                "(NOT EXISTS (SELECT 1 FROM edge_supports any_support "
                f"WHERE any_support.edge_id={alias}.id) OR EXISTS ("
                "SELECT 1 FROM edge_supports visibility_support "
                "JOIN memories visibility_memory "
                "ON visibility_memory.id=visibility_support.memory_id "
                f"WHERE visibility_support.edge_id={alias}.id "
                f"AND {support_sql} AND {memory_sql} "
                f"AND visibility_memory.workspace_id={alias}.workspace_id "
                "AND COALESCE(visibility_memory.scope, 'workspace')!='session'))",
                [*support_params, *memory_params],
            )

        selected_graph_layers = None
        selected_layers = None
        if layers is not None:
            selected_graph_layers = [
                _enum(layer, GraphLayer, "layer") for layer in layers
            ]
            selected_layers = {layer.value for layer in selected_graph_layers}

        def visible_entities():
            """Return public entities under both temporal anchors in one bounded query.

            An entity with no relation history remains a public/manual node. Once an
            entity has relation history, however, it is visible only when at least one
            touching relation has public evidence at the selected anchors. This avoids
            revealing session-only or future-supported identities.
            """
            relation_sql, relation_params = temporal_sql(
                "relation", history=include_relation_history
            )
            public_sql, public_params = public_edge_sql(
                "relation", history=include_relation_history
            )
            layer_sql = ""
            layer_params: list[Any] = []
            if connected_only and selected_layers is not None:
                if not selected_layers:
                    layer_sql = " AND 0"
                else:
                    marks = ",".join("?" for _ in selected_layers)
                    layer_sql = (
                        " AND COALESCE(relation.layer, 'semantic') IN ("
                        f"{marks})"
                    )
                    layer_params.extend(sorted(selected_layers))
            sql = f"""
                WITH edge_visibility AS (
                    SELECT relation.id, relation.src, relation.dst
                    FROM edges relation
                    WHERE relation.workspace_id=? AND {relation_sql}
                      AND {public_sql}{layer_sql}
                ), all_endpoint AS (
                    SELECT src AS entity_id FROM edges WHERE workspace_id=?
                    UNION ALL
                    SELECT dst AS entity_id FROM edges WHERE workspace_id=?
                ), entity_history AS (
                    SELECT entity_id, COUNT(*) AS degree
                    FROM all_endpoint GROUP BY entity_id
                ), visible_endpoint AS (
                    SELECT src AS entity_id FROM edge_visibility
                    UNION ALL
                    SELECT dst AS entity_id FROM edge_visibility
                ), entity_visibility AS (
                    SELECT entity_id, COUNT(*) AS degree
                    FROM visible_endpoint GROUP BY entity_id
                )
                SELECT entity.id, entity.name, entity.etype, repo.name AS repo,
                       entity.created_at AS valid_from,
                       COUNT(*) OVER() AS visible_total
                FROM entities entity
                LEFT JOIN repos repo ON repo.id=entity.repo_id
                LEFT JOIN entity_history history ON history.entity_id=entity.id
                LEFT JOIN entity_visibility visible ON visible.entity_id=entity.id
                WHERE entity.workspace_id=?
                  AND (entity.created_at IS NULL OR entity.created_at<=?)
                  AND (COALESCE(history.degree, 0)=0
                       OR COALESCE(visible.degree, 0)>0)
            """
            params: list[Any] = [
                wid, *relation_params, *public_params, *layer_params, wid, wid,
                wid, system_anchor,
            ]
            if connected_only:
                sql += " AND COALESCE(visible.degree, 0)>0"
            sql += " ORDER BY COALESCE(visible.degree, 0) DESC, entity.id LIMIT ?"
            params.append(limit)
            return conn.execute(sql, params).fetchall()

        ents = visible_entities()
        # Lazy backfill: old memories can predate graph extraction or predate the
        # structured-metadata graph bridge. On first Graph-tab open in a process, feed
        # the missing graph state once; feed() de-dupes entities/edges.
        # Strictly read-only surfaces disable this write-on-first-read migration.
        if (backfill and not temporal_requested
                and self._should_backfill_graph(wid, bool(ents))):
            self._lazy_backfill_graph(wid)
            # Rows created by the migration must be part of the same current read.
            # Explicit historical anchors never enter this write path.
            present = time.time()
            world_anchor = present
            system_anchor = present
            ents = visible_entities()
        visible_total = int(ents[0]["visible_total"]) if ents else 0
        if full and visible_total > MAX_GRAPH_ANALYSIS_ENTITIES:
            raise GraphSceneCapacityExceeded(
                resource="visible entity nodes",
                count=visible_total,
                limit=MAX_GRAPH_ANALYSIS_ENTITIES,
            )
        entity_rows = [dict(row) for row in ents]
        node_ids = {row["id"] for row in entity_rows}
        # Nodes are capped at ``limit``; edges need their own cap or a large workspace
        # graph / indexed repo lets the lowest-privilege caller pull an unbounded
        # payload. The SQL fetches are limited too, so server-side work stays bounded.
        edge_cap = min(MAX_GRAPH_ANALYSIS_EDGES, max(limit * 8, 2000))
        # A workspace can legitimately have an A-MEM graph before it has extracted
        # entities: direct memory links are first-class relationships, not merely an
        # implementation detail of the code overlay.  The old dashboard endpoint
        # returned an empty graph in that case even though the complete Galaxy scene
        # could already render the linked memories.  Surface the bounded, live subset
        # here as a useful fallback for both Graph-tab clients.
        memory_link_fallback: list[dict] = []
        if not entity_rows and selected_graph_layers != []:
            left_visibility, left_params = temporal_sql("left_memory")
            right_visibility, right_params = temporal_sql("right_memory")
            sql = (
                "SELECT link.a, link.b, link.relation, "
                "COALESCE(link.layer, 'semantic') AS layer, "
                "COALESCE(link.reason, '') AS reason, "
                "COALESCE(NULLIF(left_memory.title, ''), "
                "substr(left_memory.content, 1, 80)) AS a_name, "
                "left_memory.mtype AS a_mtype, "
                "COALESCE(NULLIF(right_memory.title, ''), "
                "substr(right_memory.content, 1, 80)) AS b_name, "
                "right_memory.mtype AS b_mtype "
                "FROM mem_links link "
                "JOIN memories left_memory ON left_memory.id=link.a "
                "JOIN memories right_memory ON right_memory.id=link.b "
                "WHERE left_memory.workspace_id=? AND right_memory.workspace_id=? "
                "AND COALESCE(left_memory.scope, 'workspace')!='session' "
                "AND COALESCE(right_memory.scope, 'workspace')!='session' "
                f"AND {left_visibility} AND {right_visibility} "
                "AND (link.valid_from IS NULL OR link.valid_from<=?) "
                "AND (link.valid_to IS NULL OR ?<link.valid_to "
                "OR (link.valid_to_recorded_at IS NOT NULL "
                "AND ?<link.valid_to_recorded_at)) "
                "AND (link.ingested_at IS NULL OR link.ingested_at<=?) "
                "AND (link.expired_at IS NULL OR ?<link.expired_at) "
            )
            params: list[Any] = [
                wid, wid, *left_params, *right_params,
                world_anchor, world_anchor, system_anchor, system_anchor, system_anchor,
            ]
            if selected_graph_layers:
                marks = ",".join("?" for _ in selected_graph_layers)
                sql += f"AND COALESCE(link.layer, 'semantic') IN ({marks}) "
                params.extend(layer.value for layer in selected_graph_layers)
            sql += "ORDER BY link.created_at, link.rowid LIMIT ?"
            params.append(edge_cap)

            fallback_nodes: dict[str, dict] = {}
            for row in conn.execute(sql, params).fetchall():
                link = dict(row)
                new_ids = {link["a"], link["b"]} - fallback_nodes.keys()
                if len(fallback_nodes) + len(new_ids) > limit:
                    continue
                for prefix, memory_id in (("a", link["a"]), ("b", link["b"])):
                    fallback_nodes.setdefault(memory_id, {
                        "id": memory_id,
                        "name": (
                            link[f"{prefix}_name"]
                            or memory_id
                        ),
                        "etype": f"memory_{link[f'{prefix}_mtype']}",
                    })
                memory_link_fallback.append({
                    "a": link["a"], "b": link["b"],
                    "relation": link["relation"], "layer": link["layer"],
                    "reason": link["reason"],
                })
            entity_rows = list(fallback_nodes.values())
        visible_edge_ids = None
        if restrict_sessions and not include_relation_history:
            relation_visibility, relation_visibility_params = temporal_sql("relation")
            public_visibility, public_visibility_params = public_edge_sql("relation")
            visible_edge_ids = {
                row["id"] for row in conn.execute(
                    "SELECT relation.id FROM edges relation "
                    f"WHERE relation.workspace_id=? AND {relation_visibility} "
                    f"AND {public_visibility}",
                    (
                        wid, *relation_visibility_params,
                        *public_visibility_params,
                    ),
                ).fetchall()
            }
        if not include_relation_history:
            graph_filter = SearchFilter(
                workspace_id=wid, graph_layers=selected_graph_layers,
                valid_at=world_anchor, known_at=system_anchor,
            )
            edgs = [
                {
                    "src": edge.src, "dst": edge.dst, "relation": edge.relation,
                    "layer": edge.layer.value if edge.layer else "semantic",
                }
                for edge in self.store.edges_in_scope(
                    graph_filter,
                    limit=edge_cap,
                )
                if edge.src in node_ids and edge.dst in node_ids
                and (visible_edge_ids is None or edge.id in visible_edge_ids)
                and (
                    selected_layers is None
                    or (edge.layer.value if edge.layer else "semantic") in selected_layers
                )
            ]
        else:
            relation_visibility, relation_visibility_params = temporal_sql(
                "relation", history=True
            )
            public_visibility, public_visibility_params = public_edge_sql(
                "relation", history=True
            )
            history_sql = (
                "SELECT relation.id, relation.src, relation.dst, relation.relation, "
                "relation.layer, relation.valid_from, relation.valid_to "
                "FROM edges relation WHERE relation.workspace_id=? "
                f"AND {relation_visibility} AND {public_visibility}"
            )
            history_params: list[Any] = [
                wid, *relation_visibility_params, *public_visibility_params,
            ]
            if selected_layers is not None:
                if not selected_layers:
                    history_sql += " AND 0"
                else:
                    marks = ",".join("?" for _ in selected_layers)
                    history_sql += f" AND relation.layer IN ({marks})"
                    history_params.extend(sorted(selected_layers))
            anchor = repr(world_anchor)
            known = repr(system_anchor)
            live_at_anchor = (
                "(relation.valid_from IS NULL OR relation.valid_from<=" + anchor + ") "
                "AND (relation.valid_to IS NULL OR " + anchor + "<relation.valid_to "
                "OR (relation.valid_to_recorded_at IS NOT NULL AND "
                + known + "<relation.valid_to_recorded_at))"
            )
            # The bounded Time payload must first preserve relations that were live at
            # the selected anchor. Remaining capacity is then used for recent ghosts.
            history_sql += (
                " ORDER BY CASE WHEN " + live_at_anchor + " THEN 0 ELSE 1 END, "
                "relation.valid_from DESC, relation.id LIMIT ?"
            )
            history_params.append(edge_cap)
            edgs = [
                dict(row) for row in conn.execute(history_sql, history_params).fetchall()
                if row["src"] in node_ids and row["dst"] in node_ids
            ]
        for link in memory_link_fallback:
            if len(edgs) >= edge_cap:
                break
            edgs.append({
                "src": link["a"], "dst": link["b"],
                "relation": link["relation"],
                "layer": link.get("layer") or "semantic",
                "reason": link.get("reason") or "",
            })
        repo_names: list[str] = []
        code_node_start = len(entity_rows)
        code_edge_start = len(edgs)
        if include_code:
            repo_rows = []
            if repo:
                repo_name = _clean_name(repo, field="repo")
                rid = self._lookup_repo(wid, repo_name)
                if rid is None:
                    raise ValidationError(
                        f"no repo named '{repo_name}' in workspace '{ws}'"
                    )
                repo_rows = [{"id": rid, "name": repo_name}]
            else:
                repo_rows = [
                    dict(row) for row in conn.execute(
                        "SELECT id, name FROM repos WHERE workspace_id=? ORDER BY name",
                        (wid,),
                    ).fetchall()
                ]
            for repo_row in repo_rows:
                rid = repo_row["id"]
                repo_name = repo_row["name"]
                repo_names.append(repo_name)
                code_filter = SearchFilter(
                    workspace_id=wid, repo_id=rid, include_ancestors=True,
                    valid_at=world_anchor, known_at=system_anchor,
                )
                symbols = self.store.list_symbols(
                    rid, limit=edge_cap if connected_only else limit, flt=code_filter
                )
                symbol_node: dict[str, str] = {}
                symbol_id_node: dict[str, str] = {}
                symbol_rows: dict[str, dict[str, str]] = {}
                for symbol in symbols:
                    node_id = f"code:{symbol['id']}"
                    label = symbol.get("fqname") or symbol.get("name") or node_id
                    row = {
                        "id": node_id,
                        "name": f"{repo_name}:{label}",
                        "etype": f"code_{symbol.get('kind') or 'symbol'}",
                    }
                    if connected_only:
                        symbol_id_node[symbol["id"]] = node_id
                        for key in (symbol.get("fqname"), symbol.get("name")):
                            if key:
                                symbol_node.setdefault(key, node_id)
                        symbol_rows[node_id] = row
                    elif len(entity_rows) < limit:
                        symbol_id_node[symbol["id"]] = node_id
                        for key in (symbol.get("fqname"), symbol.get("name")):
                            if key:
                                symbol_node.setdefault(key, node_id)
                        entity_rows.append(row)
                file_nodes: dict[str, str] = {}
                file_rows: dict[str, dict[str, str]] = {}

                def code_endpoint(value: str, file_hint: str = "") -> Optional[str]:
                    if value in symbol_id_node:
                        return symbol_id_node[value]
                    if value in symbol_node:
                        return symbol_node[value]
                    if value and (
                        "/" in value or "\\" in value
                        or value.endswith(tuple(
                            [".py", ".js", ".ts", ".go", ".rs", ".java", ".cs",
                             ".c", ".cpp", ".sql", ".tf"]
                        ))
                    ):
                        file_name = value.replace("\\", "/")
                    elif file_hint:
                        file_name = file_hint.replace("\\", "/")
                    else:
                        return None
                    if file_name not in file_nodes:
                        node_id = f"file:{rid}:{file_name}"
                        row = {
                            "id": node_id,
                            "name": f"{repo_name}:{file_name}",
                            "etype": "code_file",
                        }
                        if connected_only:
                            file_nodes[file_name] = node_id
                            file_rows[node_id] = row
                        elif len(entity_rows) < limit:
                            file_nodes[file_name] = node_id
                            entity_rows.append(row)
                    return file_nodes.get(file_name)

                for edge in self.store.list_code_edges(
                    rid, limit=edge_cap, layers=selected_graph_layers,
                    flt=code_filter,
                ):
                    if len(edgs) >= edge_cap:
                        break
                    edge_layer = edge.get("layer") or "entity"
                    if selected_layers is not None and edge_layer not in selected_layers:
                        continue
                    src = code_endpoint(edge.get("src") or "", edge.get("file") or "")
                    dst = code_endpoint(edge.get("dst") or "")
                    if src and dst and src != dst:
                        edgs.append({
                            "src": src, "dst": dst,
                            "relation": edge.get("relation") or "",
                            "layer": edge_layer,
                        })
                code_links = []
                if selected_layers is None or "semantic" in selected_layers:
                    code_links = self.store.list_code_memory_links(
                        rid, limit=edge_cap, flt=code_filter
                    )
                if connected_only:
                    connected_code_ids = {
                        endpoint
                        for edge in edgs[code_edge_start:]
                        for endpoint in (edge.get("src"), edge.get("dst"))
                        if isinstance(endpoint, str)
                        and endpoint.startswith(("code:", "file:"))
                    }
                    connected_code_ids.update(
                        symbol_id_node[link["symbol_id"]]
                        for link in code_links
                        if link.get("symbol_id") in symbol_id_node
                    )
                    for row in (*symbol_rows.values(), *file_rows.values()):
                        if row["id"] in connected_code_ids and len(entity_rows) < limit:
                            entity_rows.append(row)
                linked_memory_ids = set()
                for link in code_links:
                    if len(edgs) >= edge_cap:
                        break
                    code_id = symbol_id_node.get(link.get("symbol_id"))
                    memory_id = link.get("memory_id")
                    if not code_id or not memory_id:
                        continue
                    if memory_id not in linked_memory_ids and len(entity_rows) < limit:
                        # ``list_code_memory_links`` already applied the exact
                        # scope/world/system filter to the joined memory. Re-reading
                        # it through current-only ``get_memories`` would silently
                        # drop a valid historical bridge.
                        entity_rows.append({
                            "id": memory_id,
                            "name": link.get("title") or memory_id,
                            "etype": f"memory_{link.get('mtype') or 'semantic'}",
                        })
                        linked_memory_ids.add(memory_id)
                    if memory_id in linked_memory_ids:
                        edgs.append({
                            "src": code_id, "dst": memory_id,
                            "relation": link.get("relation") or "mentions",
                            "layer": "semantic",
                        })
                if selected_layers is None or "semantic" in selected_layers:
                    for link in self.store.links_among(
                        list(linked_memory_ids),
                        layers=(
                            [GraphLayer(layer) for layer in selected_layers]
                            if selected_layers else None
                        ),
                        flt=code_filter,
                    ):
                        if len(edgs) >= edge_cap:
                            break
                        edgs.append({
                            "src": link["a"], "dst": link["b"],
                            "relation": link["relation"],
                            "layer": link.get("layer") or "semantic",
                            "reason": link.get("reason") or "",
                        })
        payload = build_graph_payload(ws, entity_rows, edgs)
        payload["unified"] = bool(
            include_code
            and (len(entity_rows) > code_node_start or len(edgs) > code_edge_start)
        )
        payload["repos"] = repo_names
        payload["meta"] = {
            "nodes_available": max(visible_total, len(entity_rows)),
            "nodes_complete": len(entity_rows) >= visible_total,
            "mode": "full" if full else "overview",
        }
        if temporal_requested:
            payload["meta"].update({
                "as_of": as_of,
                "valid_at": valid_at,
                "known_at": known_at,
                "historical": True,
            })
        return payload

    def _should_backfill_graph(self, wid: str, has_entities: bool) -> bool:
        if wid in self._graph_backfilled:
            return False
        if not has_entities and self.engine.graph_extractor is not None:
            return True
        return self._has_structured_graph_rows(wid)

    def _has_structured_graph_rows(self, wid: str) -> bool:
        import json as _json
        import time as _time
        now = _time.time()
        rows = self.store.conn.execute(
            "SELECT metadata, provenance FROM memories WHERE workspace_id=? "
            "AND COALESCE(scope, 'workspace')!='session' "
            "AND (valid_from IS NULL OR valid_from<=?) "
            "AND (valid_to IS NULL OR ?<valid_to) AND expired_at IS NULL "
            "AND (metadata LIKE '%entities%' OR metadata LIKE '%relations%')", (wid, now, now))
        for row in rows:
            try:
                meta = _json.loads(row["metadata"] or "{}")
            except (TypeError, ValueError, RecursionError):
                continue
            try:
                provenance = _json.loads(row["provenance"] or "{}")
            except (TypeError, ValueError, RecursionError):
                provenance = meta.get("provenance") if isinstance(meta, dict) else {}
            if not prompt_eligible(provenance, meta):
                continue
            if self.engine._has_structured_graph_metadata(meta):
                return True
        return False

    def _lazy_backfill_graph(self, wid: str) -> None:
        """One-time, on-demand knowledge-graph population for a workspace whose
        memories were written before graph extraction was enabled. Feeds every live
        memory through the configured graph extractor, scoped to the memory's own
        workspace/repo. Idempotent — ``feed()`` de-dupes entities and skips existing
        edges — and instance-guarded so a workspace whose content yields no entities
        isn't rescanned on every open within a process. Content is untrusted here, as
        on the normal ingest path; it flows only through the (regex) extractor, which
        does no eval/exec/network."""
        if wid in self._graph_backfilled:
            return
        self._graph_backfilled.add(wid)
        from engraphis.backends.graph_extractor import (
            StructuredMetadataGraphExtractor, feed as _graph_feed,
        )
        import json as _json
        import time as _time
        now = _time.time()
        rows = self.store.conn.execute(
            "SELECT id, repo_id, title, content, metadata, provenance FROM memories "
            "WHERE workspace_id=? AND COALESCE(scope, 'workspace')!='session' "
            "AND (valid_from IS NULL OR valid_from<=?) "
            "AND (valid_to IS NULL OR ?<valid_to) AND expired_at IS NULL",
            (wid, now, now)).fetchall()
        for r in rows:
            try:
                meta = _json.loads(r["metadata"] or "{}")
            except (TypeError, ValueError, RecursionError):
                meta = {}
            try:
                provenance = _json.loads(r["provenance"] or "{}")
            except (TypeError, ValueError, RecursionError):
                provenance = meta.get("provenance") if isinstance(meta, dict) else {}
            if not prompt_eligible(provenance, meta):
                continue
            if self.engine._has_structured_graph_metadata(meta):
                try:
                    _graph_feed(self.store, r["content"] or "", workspace_id=wid,
                                repo_id=r["repo_id"], title=r["title"] or "",
                                extractor=StructuredMetadataGraphExtractor(meta),
                                provenance={"source": "structured_backfill", "memory_id": r["id"]})
                except Exception as exc:
                    logger.warning(
                        "structured graph backfill failed (%s)",
                        type(exc).__name__,
                    )
            if self.engine.graph_extractor is not None:
                try:
                    _graph_feed(self.store, r["content"] or "", workspace_id=wid,
                                repo_id=r["repo_id"], title=r["title"] or "",
                                extractor=self.engine.graph_extractor,
                                provenance={"source": "lazy_backfill", "memory_id": r["id"]})
                except Exception as exc:
                    logger.warning(
                        "lazy graph backfill failed (%s)",
                        type(exc).__name__,
                    )

    # ── introspection ───────────────────────────────────────────────────────────
    def stats(self, *, workspace: Optional[str] = None) -> dict:
        """Counts for quick health/onboarding checks (read-only)."""
        conn = self.store.conn
        params: list[Any] = []
        where = ""
        user = _authenticated_principal()
        # A bound instance or authenticated tenant must not report global aggregates.
        if not workspace and (self.allowed_workspaces is not None or user is not None):
            raise ValidationError("workspace is required on this instance")
        wid: Optional[str] = None
        if workspace:
            ws = self._clean_ws(workspace)
            wid = self._lookup_workspace(ws)
            if wid is None:
                return {"workspace": ws, "memories": 0, "note": "workspace not found"}
            where = " WHERE workspace_id=? AND COALESCE(scope, 'workspace')!='session'"
            params.append(wid)
        import time as _time
        now = _time.time()
        live = ("(valid_from IS NULL OR valid_from<=?) AND (valid_to IS NULL OR ?<valid_to) "
                "AND expired_at IS NULL")
        live_where = f"{where} AND {live}" if where else f" WHERE {live}"
        live_params = [*params, now, now]
        total_rows = conn.execute(
            f"SELECT COUNT(*) AS n FROM memories{where}", params).fetchone()["n"]
        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM memories{live_where}", live_params).fetchone()["n"]
        by_type = {
            r["mtype"]: r["n"] for r in conn.execute(
                f"SELECT mtype, COUNT(*) AS n FROM memories{live_where} GROUP BY mtype",
                live_params
            )
        }
        if wid is None:
            workspaces = conn.execute("SELECT COUNT(*) AS n FROM workspaces").fetchone()["n"]
            sessions = conn.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"]
        else:
            workspaces = 1
            if user is None:
                sessions = conn.execute(
                    "SELECT COUNT(*) AS n FROM sessions WHERE workspace_id=?", (wid,)
                ).fetchone()["n"]
            else:
                sessions = conn.execute(
                    "SELECT COUNT(*) AS n FROM sessions "
                    "WHERE workspace_id=? AND user_id=?",
                    (wid, user["id"]),
                ).fetchone()["n"]
        eligibility_filter = SearchFilter(
            workspace_id=wid,
            scopes=[Scope.WORKSPACE, Scope.REPO, Scope.USER],
        )
        eligibility = self.store.prompt_eligibility_counts(eligibility_filter)
        embedding = self.store.embedding_space_health(
            embedding_space_fingerprint(self.engine.embedder)
        )
        return {
            "workspace": workspace, "memories": int(total), "by_type": by_type,
            "total_rows": int(total_rows),   # live + superseded history (never deleted)
            "workspaces": int(workspaces), "sessions": int(sessions),
            "schema_version": self.store.schema_version,
            "prompt_eligibility": eligibility,
            "embedding": embedding,
        }

    def memory_health(self, *, workspace: str) -> dict:
        """Local memory health metrics: decay distribution, orphan count, conflict frequency.

        All queries are bounded and indexed. No sensitive content is exposed — only
        aggregate counts and distributions derived from stability, entity linkage,
        and audit action columns.
        """
        import time as _time
        wid = self._lookup_workspace(self._clean_ws(workspace))
        if wid is None:
            return {"workspace": workspace, "decay_distribution": [],
                    "orphan_count": 0, "conflict_frequency": {"total": 0, "last_7d": 0}}
        conn = self.store.conn
        now = _time.time()
        live = ("(valid_from IS NULL OR valid_from<=?) AND (valid_to IS NULL OR ?<valid_to) "
                "AND (ingested_at IS NULL OR ingested_at<=?) "
                "AND (expired_at IS NULL OR ?<expired_at)")
        base_where = " WHERE workspace_id=? AND COALESCE(scope,'workspace')!='session'"
        live_where = f"{base_where} AND {live}"
        live_params: list[Any] = [wid, now, now, now, now]
        # ── Decay distribution (retention buckets) ──────────────────────────────
        # R(t) = exp(-Δt_days / S). Bucket into 5 bands: critical (<0.2), low
        # (0.2–0.4), medium (0.4–0.6), high (0.6–0.8), strong (>0.8).
        # Computed in SQL via CASE on the retention formula so this is one indexed
        # scan, not a Python loop over every memory.
        decay_sql = f"""
            SELECT
                SUM(CASE WHEN ret < 0.2 THEN 1 ELSE 0 END) AS critical,
                SUM(CASE WHEN ret >= 0.2 AND ret < 0.4 THEN 1 ELSE 0 END) AS low,
                SUM(CASE WHEN ret >= 0.4 AND ret < 0.6 THEN 1 ELSE 0 END) AS medium,
                SUM(CASE WHEN ret >= 0.6 AND ret < 0.8 THEN 1 ELSE 0 END) AS high,
                SUM(CASE WHEN ret >= 0.8 THEN 1 ELSE 0 END) AS strong
            FROM (
                SELECT EXP(
                    -MAX(0, (? - COALESCE(last_access, ingested_at, ?)) / 86400.0)
                    / MAX(stability, 0.01)
                ) AS ret
                FROM memories{live_where}
            )
        """
        try:
            decay_row = conn.execute(decay_sql, [now, now, *live_params]).fetchone()
        except Exception:  # noqa: BLE001 — SQLite may lack SQLITE_ENABLE_MATH_FUNCTIONS
            # EXP() is an optional SQLite math function. On builds compiled without
            # it (or on SQLCipher), fall back to a portable Python computation so
            # memory_health() keeps working everywhere.
            decay_ret_sql = f"""
                SELECT
                    MAX(0, (? - COALESCE(last_access, ingested_at, ?)) / 86400.0)
                        / MAX(stability, 0.01) AS days_ratio
                FROM memories{live_where}
            """
            ratios = [float(r["days_ratio"]) for r in conn.execute(
                decay_ret_sql, [now, now, *live_params]
            ).fetchall()]
            buckets = {"critical": 0, "low": 0, "medium": 0, "high": 0, "strong": 0}
            for ratio in ratios:
                retention = math.exp(-ratio)
                if retention < 0.2:
                    buckets["critical"] += 1
                elif retention < 0.4:
                    buckets["low"] += 1
                elif retention < 0.6:
                    buckets["medium"] += 1
                elif retention < 0.8:
                    buckets["high"] += 1
                else:
                    buckets["strong"] += 1
            decay_distribution = [
                {"bucket": "critical", "label": "< 20%", "count": buckets["critical"]},
                {"bucket": "low",      "label": "20–40%", "count": buckets["low"]},
                {"bucket": "medium",   "label": "40–60%", "count": buckets["medium"]},
                {"bucket": "high",     "label": "60–80%", "count": buckets["high"]},
                {"bucket": "strong",   "label": "> 80%",  "count": buckets["strong"]},
            ]
        else:
            decay_distribution = [
                {"bucket": "critical", "label": "< 20%", "count": int(decay_row["critical"] or 0)},
                {"bucket": "low",      "label": "20–40%", "count": int(decay_row["low"] or 0)},
                {"bucket": "medium",   "label": "40–60%", "count": int(decay_row["medium"] or 0)},
                {"bucket": "high",     "label": "60–80%", "count": int(decay_row["high"] or 0)},
                {"bucket": "strong",   "label": "> 80%",  "count": int(decay_row["strong"] or 0)},
            ]
        # ── Orphan count (memories with no entity links) ────────────────────────
        # A memory is an orphan when it has zero live rows in memory_entities.
        # The NOT EXISTS subquery uses the existing idx_memory_entity_memory
        # index on (memory_id, valid_to, expired_at).
        orphan_params: list[Any] = [wid, now, now, now, now]
        orphan_sql_clean = (
            "SELECT COUNT(*) AS n FROM memories m "
            "WHERE m.workspace_id=? AND COALESCE(m.scope,'workspace')!='session' "
            "AND (m.valid_from IS NULL OR m.valid_from<=?) "
            "AND (m.valid_to IS NULL OR ?<m.valid_to) "
            "AND (m.ingested_at IS NULL OR m.ingested_at<=?) "
            "AND (m.expired_at IS NULL OR ?<m.expired_at) "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM memory_entities me "
            "  WHERE me.memory_id=m.id AND me.valid_to IS NULL AND me.expired_at IS NULL"
            ")"
        )
        orphan_count = int(conn.execute(orphan_sql_clean, orphan_params).fetchone()["n"])
        # ── Conflict frequency (audit actions in last 7 days + total) ───────────
        # Conflicts are recorded as audit entries whose target is the affected memory.
        # Join that target back to the already-authorized workspace instead of exposing
        # a process-global audit aggregate. Session-private memories stay out of this
        # workspace-level diagnostic just as they do from the decay/orphan metrics.
        seven_days_ago = now - 7 * 86400
        conflict_sql = (
            "SELECT COUNT(*) AS n FROM audit a "
            "JOIN memories m ON m.id=a.target "
            "WHERE a.action LIKE '%conflict%' AND m.workspace_id=? "
            "AND COALESCE(m.scope,'workspace')!='session'"
        )
        conflict_total = int(conn.execute(
            conflict_sql,
            (wid,),
        ).fetchone()["n"])
        conflict_7d = int(conn.execute(
            conflict_sql + " AND a.ts>=?",
            (wid, seven_days_ago),
        ).fetchone()["n"])
        return {
            "workspace": workspace,
            "decay_distribution": decay_distribution,
            "orphan_count": orphan_count,
            "conflict_frequency": {
                "total": conflict_total,
                "last_7d": conflict_7d,
            },
            "computed_at": now,
        }


def _filter(workspace_id, repo_id, mtypes, as_of, graph_layers=None, *, session_id=None,
            valid_at=None, known_at=None):
    from engraphis.core.interfaces import SearchFilter
    return SearchFilter(
        workspace_id=workspace_id, repo_id=repo_id, session_id=session_id,
        mtypes=mtypes, graph_layers=graph_layers, as_of=as_of,
        valid_at=valid_at, known_at=known_at,
        include_ancestors=True,
    )


def _repo_memory_scope_sql(alias: str = "") -> str:
    """Match memories visible from a repository while retaining workspace ancestors.

    Normal writes keep workspace/user rows repo-less, but migrated or legacy rows may
    retain a repo id. Visibility follows ``SearchFilter.include_ancestors`` semantics:
    repo-scoped rows must match the selected repository; workspace/user rows remain
    ancestors even when their stored repo id is non-null.
    """
    prefix = f"{alias}." if alias else ""
    return (
        f"((COALESCE({prefix}scope, 'workspace')='repo' "
        f"AND {prefix}repo_id=?) OR "
        f"COALESCE({prefix}scope, 'workspace') IN ('workspace','user'))"
    )


def _compact_provenance(value: Any) -> dict:
    """Return bounded provenance identity without copying source payload details."""
    if not isinstance(value, dict):
        return {}
    keys = ("source", "source_kind", "trusted", "kind", "origin")
    return {key: value[key] for key in keys if key in value}


def _planning_controls(planning: str, mtype_limits: Optional[dict]) -> tuple[str, dict]:
    mode = str(planning or "off").strip().casefold()
    if mode not in PLANNING_MODES:
        choices = ", ".join(sorted(PLANNING_MODES))
        raise ValidationError(f"planning must be one of: {choices}")
    if mtype_limits is None:
        return mode, {}
    if not isinstance(mtype_limits, dict):
        raise ValidationError(
            "mtype_limits must be an object of memory type to maximum count"
        )
    normalized = {}
    for raw_type, raw_limit in mtype_limits.items():
        mtype = _enum(raw_type, MemoryType, "mtype_limits key")
        if isinstance(raw_limit, bool) or not isinstance(raw_limit, int):
            raise ValidationError("mtype_limits values must be non-negative integers")
        if raw_limit < 0:
            raise ValidationError("mtype_limits values must be non-negative integers")
        normalized[mtype.value] = raw_limit
    return mode, normalized


def _empty_context_revision() -> str:
    canonical = json.dumps(
        {"token_counter": "engraphis.regex.v1", "packed": [], "context": ""},
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _empty_recall(query: str, *, token_budget: int, response_mode: str,
                  retrieval_profile: str, candidate_depth: str, valid_at: Optional[float],
                  known_at: Optional[float], note: str, planning: str = "off",
                  mtype_limits: Optional[dict] = None) -> dict:
    """Stable empty response for unknown scopes, including additive v2 accounting."""
    return {
        "query": query,
        "count": 0,
        "context": "",
        "memories": [],
        "packed_sources": [],
        "usage": {
            "budget_tokens": token_budget,
            "context_tokens": 0,
            "source_tokens": 0,
            "saved_tokens": 0,
            "savings_ratio": 0.0,
            "packed_count": 0,
            "omitted_count": 0,
            "token_counter": "engraphis.regex.v1",
        },
        "valid_at": valid_at,
        "known_at": known_at,
        "historical": valid_at is not None or known_at is not None,
        "retrieval_profile": retrieval_profile,
        "candidate_depth": candidate_depth,
        "candidate_k_requested": 50,
        "candidate_k_used": 50,
        "candidate_depth_reason": "no retrieval for unknown scope",
        "context_revision": _empty_context_revision(),
        "planning": planning,
        "mtype_limits": dict(mtype_limits or {}),
        "response_mode": response_mode,
        "score_semantics": dict(RECALL_SCORE_SEMANTICS),
        "note": note,
    }


def _empty_grounded(query: str, *, reason: str, token_budget: int,
                    response_mode: str, retrieval_profile: str, candidate_depth: str,
                    valid_at: Optional[float], known_at: Optional[float],
                    planning: str = "off", mtype_limits: Optional[dict] = None) -> dict:
    payload = _empty_recall(
        query,
        token_budget=token_budget,
        response_mode=response_mode,
        retrieval_profile=retrieval_profile,
        candidate_depth=candidate_depth,
        valid_at=valid_at,
        known_at=known_at,
        planning=planning,
        mtype_limits=mtype_limits,
        note=reason,
    )
    payload.pop("count", None)
    payload.pop("context", None)
    payload.pop("memories", None)
    payload.pop("note", None)
    payload.update({
        "grounded": False,
        "abstained": True,
        "answer": "",
        "support": 0.0,
        "synthesized": False,
        "citations": [],
        "reason": reason,
    })
    payload["usage"]["answer_tokens"] = 0
    return payload


def _mem_to_dict(rec: Any) -> dict:
    """Plain, JSON-able projection of a ``MemoryRecord`` for why/timeline/proactive
    responses — mirrors the fields ``RecallEngine`` already exposes in recall chunks."""
    return {
        "id": rec.id, "title": rec.title, "content": rec.content, "summary": rec.summary,
        "scope": rec.scope.value, "mtype": rec.mtype.value,
        "workspace_id": rec.workspace_id, "repo_id": rec.repo_id,
        "importance": rec.importance, "pinned": rec.pinned,
        "confidence": rec.confidence,
        "subject_key": rec.subject_key, "claim_kind": rec.claim_kind,
        "valid_from": rec.valid_from, "valid_to": rec.valid_to,
        "valid_to_recorded_at": rec.valid_to_recorded_at,
        "ingested_at": rec.ingested_at, "expired_at": rec.expired_at,
        "provenance": rec.provenance,
    }
