"""Cloud sync — convergent, offline-first replication of the memory store.

This is the *engine* half of the sync feature (the paid surface is gated at the
entry points — ``scripts/sync.py``, the MCP tool, the Inspector route — never in
here, so ``core/`` stays Apache-2.0 and license-free per AGENTS.md §3).

Why this is small: v2 already ships the hard primitives. Memory ids are globally
unique ULIDs (``core/ids.py``) minted with 80 bits of CSPRNG randomness, so two
offline devices never collide; ``Store.add_memory`` is an idempotent
``INSERT ... ON CONFLICT(id) DO UPDATE`` that only defaults timestamps when they are
null, so a remote write re-applies verbatim; and validity is bi-temporal, so a
"delete" is a ``valid_to`` we can merge rather than a destructive op. Sync is
therefore a **state-based CRDT** over memory rows, not a bespoke replication log:

* **Identity is global.** A memory's ULID is the same on every device; union by id.
* **Scope is per-device.** ``workspace_id``/``repo_id`` are per-device ULIDs, so we
  reconcile scope *by name* on apply (like ``scripts/migrate_to_v2.py`` re-homes
  rows) — memory identity stays stable, its scope pointers are re-homed locally.
* **Fields merge by a commutative lattice**, so the merged state is identical
  regardless of which device syncs first, and re-applying a bundle is a no-op:
    - ``valid_to`` / ``expired_at``: earliest non-null wins (an invalidation on any
      device invalidates everywhere — never resurrected).
    - ``stability`` / ``access_count`` / ``last_access``: ``max`` (reinforcement is
      monotone; the spacing effect only ever grows stability).
    - ``pinned``: a per-field pin lattice — ``pinned_at``/``unpinned_at`` markers
      merge latest-wins (the newest transition dominates), so a re-pin on one device
      beats a stale unpin on another instead of losing a legitimate toggle.
      ``pinned`` itself is derived from the merged markers.
    - ``deleted_at``: secure-erase tombstones are terminal within their known
      repository scope. An erased id is carried in the bundle's ``tombstones`` list
      (id + erasure time + origin device, never content) and merged earliest-wins;
      legacy repo-less markers remain global for compatibility, while a known marker
      cannot erase a same-id row from a sibling repository.
    - descriptive fields (title/content/keywords/…): last-writer-wins under the
      canonical ``modified_hlc``. Any initialized HLC beats the empty v1/v2 sentinel;
      legacy-only candidates retain bounded ``ingested_at`` plus a stable payload-hash
      tie-break. Recall's independent ``last_access`` max-join therefore cannot make an
      older edit win.
    - equal physical/logical HLC instants from concurrent devices still choose one
      deterministic winner by node id and payload hash, but the losing descriptive
      variant is retained once as a deterministic, explicitly untrusted conflict
      successor with a content-free audit trail. Replay cannot duplicate it.

Untrusted input: a pulled bundle is attacker-controlled (SECURITY.md — memory
poisoning is an explicit threat). ``apply_bundle`` validates and clamps every row,
re-homes it into the caller's own workspace, and never executes bundle content.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from typing import Any, Iterable, Optional

from engraphis.core.graph_layers import merge_graph_layers, normalize_graph_layer
from engraphis.core.interfaces import (
    MemoryRecord,
    MemoryType,
    Scope,
    SearchFilter,
    SyncTransport,
    embedding_space_fingerprint,
    normalize_modified_hlc,
    parse_modified_hlc,
    vector_index_requires_sync,
    vector_index_shares_store_transaction,
)
from engraphis.core.poisoning import (
    PoisoningDecision,
    apply_quarantine_metadata,
    assess_untrusted_payload,
    metadata_is_quarantined,
    prompt_eligible,
    provenance_is_approved,
)
from engraphis.core.secrets import SecretDetectedError, reject_secrets, secret_kind
from engraphis.core.retention_policy import effective_access_count, effective_stability
from engraphis.core.store import (
    Store,
    TOMBSTONE_NEVER_EXPORT,
    TOMBSTONE_REMOTE_ERASURE,
    now_ts,
)


logger = logging.getLogger("engraphis.sync")

_VectorIndexAction = tuple[str, str, Any, str]

# ── bundle format ─────────────────────────────────────────────────────────────
SYNC_FORMAT = "engraphis-sync"
SYNC_VERSION = 3
SYNC_ACCEPTED_VERSIONS = frozenset({1, 2, 3})

# ── tombstone bundle constants ────────────────────────────────────────────────
MAX_TOMBSTONES = 200_000             # same cap as MAX_MEMORIES (ids only, no content)

# ── validation caps (untrusted bundle → clamp, don't trust) ───────────────────
MAX_MEMORIES = 200_000
MAX_LINKS = 500_000
MAX_CONTENT_CHARS = 200_000
MAX_TITLE_CHARS = 4_000
MAX_SUMMARY_CHARS = 20_000
MAX_KEYWORDS = 64
MAX_KEYWORD_CHARS = 200
MAX_JSON_CHARS = 40_000            # metadata / provenance serialized cap
MAX_SESSION_ID_CHARS = 128
MAX_DEVICE_ID_CHARS = 128
MAX_REPOS = 10_000                 # cap repos map so an empty-memories bundle can't bloat
# Rows applied per transaction / per batched existence lookup. Bounded so applying a
# MAX_MEMORIES bundle never materializes the whole thing at once (see apply_bundle).
APPLY_BATCH = 500
MAX_WORKSPACE_NAME_CHARS = 200
MAX_REPO_NAME_CHARS = 200
TS_FUTURE_SKEW = 2 * 86400         # tolerate 2 days of cross-device clock skew, no more
_VALID_SENSITIVITY = ("normal", "sensitive", "secret")
_VALID_SCOPES = frozenset(scope.value for scope in Scope)

# Strip C0/C1 control + ANSI-escape bytes (keep \t\n\r) — the same defense the rest of
# the ingest surface applies (service.py) against hidden-instruction / terminal-injection
# payloads. The sync write path bypasses service.py, so it must strip here itself.
_NORMALISED_LEGACY_DEVICE_ID_RE = re.compile(r"^legacy_[0-9a-f]{16}$")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SAFE_DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_TYPED_DEVICE_ID_RE = re.compile(r"^dev_[0-9A-HJKMNPQRSTVWXYZ]{26}$")
_STATE_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_CROCKFORD32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
MAX_SYNC_GENERATION = (1 << 63) - 1


# Local trust/ingress and derived-index envelopes are persisted for policy and
# diagnostics, but they are not peer-authored descriptive state. Excluding them from
# the version hash prevents a round-tripped record from conflicting with itself.
_LOCAL_METADATA_FIELDS = frozenset({
    "provenance",
    "quarantine",
    "retention_supervision",
    "entities",
    "relations",
    "structured_extraction",
    "llm_extraction",
    "structured_consolidation",
    "sync_ingress",
    "embed_model",
})


class SyncError(Exception):
    """A bundle is structurally unusable (wrong format/version, not a dict).

    Row-level problems never raise — bad rows are dropped and counted as
    ``rejected`` so one poisoned record can't abort an otherwise good sync."""


# ── small deterministic helpers (pure) ────────────────────────────────────────

def _enum(v: Any) -> str:
    return v.value if hasattr(v, "value") else str(v)


def _stable_hash(obj: Any) -> str:
    """Content hash that is identical across machines/processes (unlike ``hash()``)."""
    raw = json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _encode_crockford(value: int, width: int) -> str:
    """Encode one bounded integer without introducing random conflict identity."""
    chars = ["0"] * width
    for index in range(width - 1, -1, -1):
        chars[index] = _CROCKFORD32[value & 0x1F]
        value >>= 5
    if value:
        raise ValueError("value does not fit Crockford field")
    return "".join(chars)


def _conflict_memory_id(physical_ms: int, digest: str) -> str:
    """Build a deterministic, time-sortable typed ULID from an HLC and pair hash."""
    randomness = int(digest[:20], 16)  # 80 deterministic bits, matching ULID width
    return (
        "mem_"
        + _encode_crockford(physical_ms, 10)
        + _encode_crockford(randomness, 16)
    )


def _min_nonnull(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None:
        return b
    if b is None:
        return a
    return a if a <= b else b


def _max_nonnull(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None:
        return b
    if b is None:
        return a
    return a if a >= b else b


def _label_tuple(rec: MemoryRecord) -> list:
    """Canonical user-descriptive payload used only for version ordering."""
    metadata = dict(rec.metadata or {})
    for key in _LOCAL_METADATA_FIELDS:
        metadata.pop(key, None)
    return [
        rec.title, rec.content, rec.summary, sorted(rec.keywords or []),
        _enum(rec.mtype), _enum(rec.scope), rec.importance, rec.surprise,
        rec.confidence, rec.sensitivity, rec.valid_from, rec.ingested_at,
        rec.session_id, rec.subject_key, rec.claim_kind,
        json.dumps(metadata, sort_keys=True, default=str),
    ]


def _version_key(rec: MemoryRecord) -> tuple[int, str, float, str]:
    """Total order for descriptive content, independent of reinforcement reads.

    A canonical ``modified_hlc`` is the authoritative version. Any initialized HLC
    sorts after the empty legacy sentinel, so a real descriptive update dominates
    every pre-v13 copy. Two legacy rows retain the old ``ingested_at`` fallback;
    the stable payload hash makes both regimes deterministic on an exact clock tie.
    """
    payload_hash = _stable_hash(_label_tuple(rec))
    if rec.modified_hlc:
        return (1, rec.modified_hlc, 0.0, payload_hash)
    return (0, "", rec.ingested_at or 0.0, payload_hash)


def merge_record(local: MemoryRecord, incoming: MemoryRecord) -> MemoryRecord:
    """Deterministically merge two versions of the SAME memory id.

    Commutative, associative, and idempotent: ``merge(a, b) == merge(b, a)`` and
    ``merge(merge(a, b), b) == merge(a, b)``. ``incoming`` must already be re-homed
    into local scope (``workspace_id``/``repo_id`` set to local ids) — those fields
    are taken from ``local`` here and never LWW-merged, so re-homing is never undone.
    """
    winner = local if _version_key(local) >= _version_key(incoming) else incoming
    valid_to, valid_to_recorded_at = _merge_closure(local, incoming)
    pinned, pinned_at, unpinned_at = _pin_lattice(local, incoming)
    return MemoryRecord(
        id=local.id,
        # scope pointers are always local — never merged from the remote
        workspace_id=local.workspace_id,
        repo_id=local.repo_id,
        # descriptive fields: whole-record last-writer-wins
        content=winner.content, title=winner.title, summary=winner.summary,
        keywords=list(winner.keywords or []), metadata=dict(winner.metadata or {}),
        mtype=winner.mtype, scope=winner.scope, importance=winner.importance,
        surprise=winner.surprise, confidence=winner.confidence,
        sensitivity=winner.sensitivity,
        session_id=winner.session_id, provenance=dict(winner.provenance or {}),
        subject_key=winner.subject_key, claim_kind=winner.claim_kind,
        valid_from=winner.valid_from,
        # Keep the HLC and ingress timestamp from the same whole-record winner.
        # ``last_access`` remains an independent reinforcement lattice below.
        ingested_at=winner.ingested_at,
        modified_hlc=winner.modified_hlc,
        # lattice fields: commutative joins (independent of the LWW winner)
        valid_to=valid_to,
        expired_at=_min_nonnull(local.expired_at, incoming.expired_at),
        stability=max(local.stability, incoming.stability),
        access_count=max(local.access_count, incoming.access_count),
        last_access=_max_nonnull(local.last_access, incoming.last_access),
        pinned=pinned,
        pinned_at=pinned_at,
        unpinned_at=unpinned_at,
        valid_to_recorded_at=valid_to_recorded_at,
    )


def _merge_closure(
    local: MemoryRecord, incoming: MemoryRecord,
) -> tuple[Optional[float], Optional[float]]:
    """Join a world-time closure with the system-time at which it was learned.

    The earliest world-time closure wins. Its knowledge timestamp must travel
    with it; independently learned equal closures use the earliest timestamp.
    A missing timestamp is legacy v1 state whose closure was always visible, so
    it remains ``None`` rather than being silently assigned a later time.
    """
    if local.valid_to is None:
        return incoming.valid_to, (
            incoming.valid_to_recorded_at if incoming.valid_to is not None else None
        )
    if incoming.valid_to is None:
        return local.valid_to, local.valid_to_recorded_at
    if local.valid_to < incoming.valid_to:
        return local.valid_to, local.valid_to_recorded_at
    if incoming.valid_to < local.valid_to:
        return incoming.valid_to, incoming.valid_to_recorded_at
    if local.valid_to_recorded_at is None or incoming.valid_to_recorded_at is None:
        return local.valid_to, None
    return local.valid_to, min(
        local.valid_to_recorded_at, incoming.valid_to_recorded_at
    )


def _merge_closure_ts(a: Optional[float], b: Optional[float]) -> Optional[float]:
    """Latest non-null wins for one pin-transition marker.

    Pin state is a toggle, not a closure. The newest transition must therefore
    dominate an older peer marker; retaining only the earliest marker makes a
    legitimate re-pin impossible to propagate after an unpin.
    """
    if a is None:
        return b
    if b is None:
        return a
    return a if a >= b else b


def _normalise_pin_state(rec: MemoryRecord) -> tuple[bool, Optional[float], Optional[float]]:
    """Drop contradictory marker combinations before merging untrusted rows.

    A pinned row may be legacy (no markers), or carry a pin marker newer than its
    unpin marker. An unpinned row may carry the history of a pin, but a pin marker
    without an unpin marker is inconsistent with ``pinned=False`` and must not grant
    authority merely because a peer serialized a timestamp.
    """
    pinned = bool(rec.pinned)
    pinned_at = rec.pinned_at
    unpinned_at = rec.unpinned_at
    if pinned:
        if pinned_at is None:
            # Legacy pinned rows have no trustworthy transition clock. Preserve the
            # legacy state and ignore a marker-only forged unpin.
            unpinned_at = None
        elif unpinned_at is not None and pinned_at <= unpinned_at:
            # The markers describe an unpinned state; the explicit boolean cannot
            # override the newer unpin event.
            pinned = False
    elif pinned_at is not None and (
            unpinned_at is None or pinned_at > unpinned_at):
        # A false row must not become pinned from a lone peer-controlled marker.
        pinned_at = None
    return pinned, pinned_at, unpinned_at


def _pin_lattice(local: MemoryRecord, incoming: MemoryRecord) -> tuple[bool, Optional[float], Optional[float]]:
    """Merge pin state as a latest-transition lattice.

    The two markers retain the latest pin and unpin events. Deriving the boolean
    from their newest values makes pin/unpin/re-pin convergence commutative,
    associative, and idempotent while rejecting contradictory marker-only authority.
    """
    local_pinned, local_pinned_at, local_unpinned_at = _normalise_pin_state(local)
    incoming_pinned, incoming_pinned_at, incoming_unpinned_at = _normalise_pin_state(incoming)
    pinned_at = _merge_closure_ts(local_pinned_at, incoming_pinned_at)
    unpinned_at = _merge_closure_ts(local_unpinned_at, incoming_unpinned_at)
    # A legacy pinned row carries no marker. Treat it as pinned since the epoch so
    # an explicit unpin can still propagate to peers that have not migrated it.
    if pinned_at is None and (local_pinned or incoming_pinned):
        pinned_at = 0.0
    if unpinned_at is None:
        pinned = pinned_at is not None
    elif pinned_at is None:
        pinned = False
    else:
        pinned = pinned_at > unpinned_at
    return pinned, pinned_at, unpinned_at


def _initialize_sync_store_defaults(rec: MemoryRecord) -> MemoryRecord:
    """Give an imported row deterministic values for Store-required clocks.

    ``Store.add_memory`` normally fills these fields from the receiver's wall clock.
    That is appropriate for a local write, but sync omission must have the same meaning
    regardless of receiver time or bundle arrival order. Prefer wire ``ingested_at``;
    a modern HLC supplies the next portable anchor, and zero is the explicit legacy
    "unknown time" sentinel. Every candidate is canonicalized independently before merge.
    """
    if rec.ingested_at is None:
        if rec.modified_hlc:
            physical_ms, _, _ = parse_modified_hlc(rec.modified_hlc)
            rec.ingested_at = physical_ms / 1000.0
        else:
            rec.ingested_at = 0.0
    if rec.valid_from is None:
        rec.valid_from = rec.ingested_at
    if rec.last_access is None:
        rec.last_access = rec.ingested_at
    return rec


def _same_sync_payload(left: MemoryRecord, right: MemoryRecord) -> bool:
    """Compare the synced record payload while excluding local policy envelopes.

    ``metadata`` and ``provenance`` are sanitized on every external ingress.  They
    therefore cannot decide whether a peer replay represents a new memory version.
    The caller uses this only when the bundle omitted both fields, so an explicit
    metadata or provenance update still flows through normal LWW resolution.
    """
    return (
        left.title == right.title
        and left.content == right.content
        and left.summary == right.summary
        and list(left.keywords or []) == list(right.keywords or [])
        and left.mtype == right.mtype
        and left.scope == right.scope
        and left.importance == right.importance
        and left.surprise == right.surprise
        and left.confidence == right.confidence
        and left.sensitivity == right.sensitivity
        and left.valid_from == right.valid_from
        and left.ingested_at == right.ingested_at
        and left.session_id == right.session_id
        and left.subject_key == right.subject_key
        and left.claim_kind == right.claim_kind
    )


def _signature(rec: MemoryRecord) -> str:
    """Fingerprint of everything sync persists — to tell 'changed' from 'no-op'."""
    return _stable_hash(_label_tuple(rec) + [
        json.dumps(rec.metadata or {}, sort_keys=True, default=str),
        json.dumps(rec.provenance or {}, sort_keys=True, default=str),
        rec.modified_hlc,
        rec.valid_to, rec.valid_to_recorded_at, rec.expired_at,
        rec.stability, rec.access_count, rec.last_access, bool(rec.pinned),
        rec.pinned_at, rec.unpinned_at,
    ])


# ── serialization (embedding excluded — rebuilt locally, never trusted over the wire) ──

def record_to_dict(rec: MemoryRecord) -> dict:
    return {
        "id": rec.id, "workspace_id": rec.workspace_id, "repo_id": rec.repo_id,
        "session_id": rec.session_id, "scope": _enum(rec.scope), "mtype": _enum(rec.mtype),
        "title": rec.title, "content": rec.content, "summary": rec.summary,
        "keywords": list(rec.keywords or []), "metadata": rec.metadata or {},
        "importance": rec.importance, "surprise": rec.surprise, "stability": rec.stability,
        "confidence": rec.confidence, "access_count": rec.access_count,
        "last_access": rec.last_access,
        "valid_from": rec.valid_from, "valid_to": rec.valid_to,
        "valid_to_recorded_at": rec.valid_to_recorded_at,
        "ingested_at": rec.ingested_at, "expired_at": rec.expired_at,
        "modified_hlc": rec.modified_hlc,
        "pinned": bool(rec.pinned), "sensitivity": rec.sensitivity,
        "pinned_at": rec.pinned_at, "unpinned_at": rec.unpinned_at,
        "subject_key": rec.subject_key, "claim_kind": rec.claim_kind,
        "provenance": rec.provenance or {},
    }


def _as_float(v: Any, default: Optional[float]) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError, OverflowError):
        return default
    return f if math.isfinite(f) else default   # reject inf/nan (JSON Infinity/NaN, overflow)


def _as_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError, OverflowError):
        return default


def _clamp_num(v: Any, lo: float, hi: float, default: float) -> float:
    """Coerce to float and clamp to ``[lo, hi]`` — stops an untrusted bundle from
    poisoning recall ranking with absurd importance/stability/surprise values."""
    f = _as_float(v, default)
    if f is None:
        return default
    return max(lo, min(hi, f))


def _clamp_ts(v: Any, now: float) -> Optional[float]:
    """Coerce a system timestamp, preserving accepted values exactly.

    ``ingested_at`` is the legacy descriptive version clock, so an unclamped future
    value could permanently pin poisoned content above every honest future edit. A
    receiver-relative upper clamp is not convergent, however: two replicas would store
    different caps. Return ``None`` for an invalid/future value so the trust boundary can
    reject a supplied value; retain the skew window for ordinary clock drift.
    """
    f = _as_float(v, None)
    if f is None or f > now + TS_FUTURE_SKEW:
        return None
    return max(0.0, f)


# World-time validity ceiling (year ~2100). ``valid_from``/``valid_to`` are WORLD time — a
# fact may legitimately be true until a future date. Neither feeds the primary
# version-clock ordering (currently ``ingested_at``): ``valid_to`` is a lattice field,
# and ``valid_from`` participates only in the version key's deterministic content-hash
# tiebreak, so a future value cannot pin poisoned content above honest edits. Clamping
# these to now+skew truncated real future validity, which the earliest-wins merge then
# spread to every device. Bound only to a sane
# far-future ceiling to reject absurd/overflow values.
_WORLD_TS_MAX = 4_102_444_800.0


def _clamp_world_ts(v: Any) -> Optional[float]:
    """Coerce a world-time validity timestamp, allowing legitimate FUTURE values (bounded
    to a far-future ceiling). Clamping these to ``now + skew`` like the system timestamps
    truncated real future validity, and the earliest-wins merge then spread the truncation
    to every device."""
    f = _as_float(v, None)
    if f is None:
        return None
    return max(0.0, min(f, _WORLD_TS_MAX))


def _clamp_str(v: Any, n: int) -> str:
    s = v if isinstance(v, str) else ("" if v is None else str(v))
    return _CONTROL_RE.sub("", s)[:n]


def _normalise_device_id(value: Any) -> str:
    """Return a bounded report/provenance identity or reject malformed metadata."""
    if value is None or value == "":
        return "legacy_anonymous"
    if (
        not isinstance(value, str)
        or len(value) > MAX_DEVICE_ID_CHARS
        or _SAFE_DEVICE_ID_RE.fullmatch(value) is None
    ):
        raise SyncError("bundle device_id is invalid")
    if (
        value == "legacy_anonymous"
        or _TYPED_DEVICE_ID_RE.fullmatch(value) is not None
        or _NORMALISED_LEGACY_DEVICE_ID_RE.fullmatch(value) is not None
    ):
        return value
    return "legacy_" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _mtype(v: Any) -> MemoryType:
    try:
        return MemoryType(str(v))
    except ValueError:
        return MemoryType.SEMANTIC


def _scope(v: Any) -> Scope:
    try:
        return Scope(str(v))
    except ValueError:
        return Scope.REPO


def _safe_json_obj(v: Any) -> dict:
    if not isinstance(v, dict):
        return {}
    try:
        if len(json.dumps(v, default=str)) > MAX_JSON_CHARS:
            return {}
    except Exception:
        return {}
    return v


def _reject_nonfinite(token: str):
    raise ValueError("non-finite JSON constant: %s" % token)


_MAX_BUNDLE_DEPTH = 200  # generous; real bundles are shallow. Explicit DoS guard so
# deeply-nested input is rejected on every Python version (3.12+'s JSON scanner no
# longer raises RecursionError for ~1000-deep input, so we can't rely on that alone).


def _scan_depth(s: str) -> int:
    """Cheap max-nesting-depth scan that skips JSON string literals; used to reject
    pathologically deep bundles without relying on the JSON scanner's RecursionError."""
    depth = 0
    max_depth = 0
    in_str = False
    esc = False
    for ch in s:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "[{":
            depth += 1
            if depth > max_depth:
                max_depth = depth
        elif ch in "]}":
            if depth > 0:
                depth -= 1
    return max_depth


def loads_strict(data: bytes):
    """Parse untrusted bundle bytes, rejecting the non-standard ``Infinity``/``NaN``
    tokens Python's ``json`` accepts by default (they later raise ``OverflowError`` in
    ``int()`` and would otherwise abort the whole sync run). Deeply-nested input that
    would raise ``RecursionError`` in the JSON scanner is normalized to ``ValueError``
    so a single hostile bundle can't crash the whole sync run (DoS)."""
    text = data.decode("utf-8")
    if _scan_depth(text) > _MAX_BUNDLE_DEPTH:
        raise ValueError("bundle JSON is nested too deeply")
    try:
        return json.loads(text, parse_constant=_reject_nonfinite)
    except RecursionError:
        raise ValueError("bundle JSON is nested too deeply")


def _snapshot_hash(bundle: dict) -> str:
    """Hash authenticated snapshot state while excluding its volatile wall clock."""
    return _stable_hash({
        key: value
        for key, value in bundle.items()
        if key not in {"created_at", "state_hash"}
    })


def _validated_snapshot_freshness(bundle: dict) -> Optional[tuple[int, str, str]]:
    """Validate v3 generation/hash-chain metadata before any destructive apply."""
    version = _as_int(bundle.get("version"), 0)
    if version < 3:
        return None
    generation = bundle.get("generation")
    previous_hash = bundle.get("previous_hash")
    state_hash = bundle.get("state_hash")
    tombstone_hash = bundle.get("tombstone_checkpoint")
    tombstone_count = bundle.get("tombstone_count")
    tombstones = bundle.get("tombstones") or []
    if (
        isinstance(generation, bool)
        or not isinstance(generation, int)
        or not 1 <= generation <= MAX_SYNC_GENERATION
        or not isinstance(previous_hash, str)
        or (generation == 1 and previous_hash != "")
        or (generation > 1 and _STATE_HASH_RE.fullmatch(previous_hash) is None)
        or not isinstance(state_hash, str)
        or _STATE_HASH_RE.fullmatch(state_hash) is None
        or not isinstance(tombstone_hash, str)
        or _STATE_HASH_RE.fullmatch(tombstone_hash) is None
        or isinstance(tombstone_count, bool)
        or not isinstance(tombstone_count, int)
        or tombstone_count != len(tombstones)
        or tombstone_hash != _stable_hash(tombstones)
        or state_hash != _snapshot_hash(bundle)
    ):
        raise SyncError("bundle freshness metadata is invalid")
    return generation, previous_hash, state_hash


def dict_to_record(d: Any) -> Optional[MemoryRecord]:
    """Validate + clamp one untrusted bundle row into a MemoryRecord, or ``None`` if
    it is unusable (no id / no content). Never raises — this is the trust boundary."""
    if not isinstance(d, dict):
        return None
    mid = d.get("id")
    content = d.get("content")
    if not isinstance(mid, str) or not mid or not isinstance(content, str) or not content:
        return None
    # Sync is an external memory write path. Reject the row before it can reach the
    # raw Store upsert, FTS, or a locally rebuilt vector; a secret-bearing peer row is
    # simply counted as rejected like any other malformed bundle entry.
    try:
        reject_secrets((("title", d.get("title")), ("content", content),
                        ("summary", d.get("summary")), ("keywords", d.get("keywords")),
                        ("metadata", d.get("metadata")), ("provenance", d.get("provenance")),
                        ("subject_key", d.get("subject_key")),
                        ("claim_kind", d.get("claim_kind"))))
    except SecretDetectedError:
        return None
    kws = d.get("keywords") or []
    if not isinstance(kws, list):
        kws = []
    kws = [_clamp_str(k, MAX_KEYWORD_CHARS) for k in kws[:MAX_KEYWORDS]]
    sens = d.get("sensitivity")
    if sens not in _VALID_SENSITIVITY:
        sens = "normal"
    now = now_ts()
    modified_hlc = d.get("modified_hlc", "")
    try:
        modified_hlc = normalize_modified_hlc(modified_hlc, allow_empty=True)
        physical_ms, _, _ = parse_modified_hlc(modified_hlc, allow_empty=True)
    except ValueError:
        return None
    if modified_hlc and physical_ms > int((now + TS_FUTURE_SKEW) * 1000):
        # A peer-controlled clock beyond the same skew allowed for system timestamps
        # could permanently win LWW ordering. Reject it; never clamp poison into authority.
        return None
    ingested_at = _clamp_ts(d.get("ingested_at"), now)
    last_access = _clamp_ts(d.get("last_access"), now)
    valid_to_recorded_at = _clamp_ts(d.get("valid_to_recorded_at"), now)
    expired_at = _clamp_ts(d.get("expired_at"), now)
    pinned_at = _clamp_ts(d.get("pinned_at"), now)
    unpinned_at = _clamp_ts(d.get("unpinned_at"), now)
    supplied_system_times = {
        "ingested_at": ingested_at,
        "last_access": last_access,
        "valid_to_recorded_at": valid_to_recorded_at,
        "expired_at": expired_at,
        "pinned_at": pinned_at,
        "unpinned_at": unpinned_at,
    }
    if any(
        d.get(field_name) is not None and value is None
        for field_name, value in supplied_system_times.items()
    ):
        return None
    valid_from = _clamp_world_ts(d.get("valid_from"))
    if modified_hlc and (ingested_at is None or valid_from is None):
        # A real descriptive clock identifies a complete modern write. Allowing its
        # descriptive timestamps to be omitted would synthesize receiver-side values
        # under the same HLC and manufacture a false concurrent-edit conflict.
        return None
    return MemoryRecord(
        id=_clamp_str(mid, 128), content=_clamp_str(content, MAX_CONTENT_CHARS),
        mtype=_mtype(d.get("mtype")), scope=_scope(d.get("scope")),
        workspace_id=d.get("workspace_id"), repo_id=d.get("repo_id"),
        session_id=_clamp_str(d.get("session_id"), MAX_SESSION_ID_CHARS)
        if isinstance(d.get("session_id"), str) else None,
        title=_clamp_str(d.get("title"), MAX_TITLE_CHARS),
        summary=_clamp_str(d.get("summary"), MAX_SUMMARY_CHARS),
        keywords=kws, metadata=_safe_json_obj(d.get("metadata")),
        importance=_clamp_num(d.get("importance"), 0.0, 1.0, 0.0),
        surprise=_clamp_num(d.get("surprise"), 0.0, 100.0, 1.0),
        stability=effective_stability(d.get("stability")),
        confidence=_clamp_num(d.get("confidence"), 0.0, 1.0, 1.0),
        access_count=effective_access_count(d.get("access_count")),
        last_access=last_access,
        # World-time validity may be in the future. System timestamps and the HLC
        # above are bounded against peer-controlled future-time authority.
        valid_from=valid_from,
        valid_to=_clamp_world_ts(d.get("valid_to")),
        valid_to_recorded_at=valid_to_recorded_at,
        ingested_at=ingested_at,
        expired_at=expired_at,
        modified_hlc=modified_hlc,
        # Authority-bearing booleans are strict. In particular ``"false"`` must
        # not become truthy and then remain permanently pinned through the CRDT OR.
        pinned=d.get("pinned") is True, sensitivity=sens,
        pinned_at=pinned_at,
        unpinned_at=unpinned_at,
        subject_key=_clamp_str(d.get("subject_key"), 512),
        claim_kind=_clamp_str(d.get("claim_kind"), 256),
        provenance=_safe_json_obj(d.get("provenance")),
    )


# ── the engine ────────────────────────────────────────────────────────────────

class SyncEngine:
    """Convergent sync over a ``Store``. Transport-agnostic and offline-testable.

    ``embedder``/``vector_index`` are optional and injected (Protocols, never
    imported concretely here): when present, applied rows are re-embedded so the
    vector arm can recall them; when absent, lexical/FTS recall still works and
    vectors can be rebuilt later. This mirrors how ``RecallEngine`` takes its
    backends — a config choice, not a hard dependency (AGENTS.md §3.1/§3.8).
    """

    def __init__(self, store: Store, *, embedder=None, vector_index=None,
                 device_id: Optional[str] = None,
                 allowed_workspaces: Optional[frozenset] = None) -> None:
        self.store = store
        self.embedder = embedder
        self.embedding_space = (
            embedding_space_fingerprint(embedder) if embedder is not None else ""
        )
        self.index = vector_index
        self.device_id = _normalise_device_id(device_id or store.device_id())

        # Same hard boundary MemoryService enforces (SECURITY.md §3): when set, a bundle
        # may only be applied into one of these workspaces, so the folder transport can
        # never be steered into writing a workspace the operator never authorized.
        self.allowed_workspaces = (frozenset(allowed_workspaces)
                                   if allowed_workspaces else None)

    @staticmethod
    def _checkpoint_key(workspace_id: str, repo_id: Optional[str],
                        device_id: str) -> str:
        scope = hashlib.sha256(
            (str(workspace_id) + "\0" + str(repo_id or "")).encode("utf-8")
        ).hexdigest()[:24]
        device = hashlib.sha256(device_id.encode("utf-8")).hexdigest()[:24]
        return f"sync_snapshot:{scope}:{device}"

    def _load_snapshot_checkpoint(
            self, workspace_id: str, repo_id: Optional[str],
            device_id: str) -> Optional[tuple[int, str]]:
        raw = self.store.get_sync_state(
            self._checkpoint_key(workspace_id, repo_id, device_id)
        )
        if raw is None:
            return None
        try:
            value = json.loads(raw)
            generation = value["generation"]
            state_hash = value["state_hash"]
        except (KeyError, TypeError, ValueError, RecursionError):
            raise SyncError("local sync freshness checkpoint is invalid") from None
        if (
            isinstance(generation, bool)
            or not isinstance(generation, int)
            or not 1 <= generation <= MAX_SYNC_GENERATION
            or not isinstance(state_hash, str)
            or _STATE_HASH_RE.fullmatch(state_hash) is None
        ):
            raise SyncError("local sync freshness checkpoint is invalid")
        return generation, state_hash

    def _save_snapshot_checkpoint(
            self, workspace_id: str, repo_id: Optional[str], device_id: str,
            generation: int, state_hash: str) -> None:
        self.store.set_sync_state(
            self._checkpoint_key(workspace_id, repo_id, device_id),
            json.dumps(
                {"generation": generation, "state_hash": state_hash},
                separators=(",", ":"), sort_keys=True,
            ),
        )

    def _stamp_snapshot(self, bundle: dict, workspace_id: str,
                        repo_id: Optional[str], *, save_checkpoint: bool = True) -> dict:
        device_id = _normalise_device_id(bundle["device_id"])
        checkpoint = self._load_snapshot_checkpoint(
            workspace_id, repo_id, device_id
        )
        generation = 1 if checkpoint is None else checkpoint[0] + 1
        if generation > MAX_SYNC_GENERATION:
            raise SyncError("local sync generation is exhausted")
        bundle["generation"] = generation
        bundle["previous_hash"] = "" if checkpoint is None else checkpoint[1]
        tombstones = bundle.get("tombstones") or []
        bundle["tombstone_count"] = len(tombstones)
        bundle["tombstone_checkpoint"] = _stable_hash(tombstones)
        bundle["state_hash"] = _snapshot_hash(bundle)
        if save_checkpoint:
            self._save_snapshot_checkpoint(
                workspace_id, repo_id, device_id,
                generation, bundle["state_hash"],
            )
        return bundle

    def _check_incoming_freshness(
            self, bundle: dict, workspace_id: str,
            repo_id: Optional[str], device_id: str,
    ) -> tuple[Optional[tuple[int, str, str]], bool, bool]:
        freshness = _validated_snapshot_freshness(bundle)
        checkpoint = self._load_snapshot_checkpoint(
            workspace_id, repo_id, device_id
        )
        if freshness is None:
            if checkpoint is not None:
                raise SyncError("legacy snapshot is older than the local checkpoint")
            return None, False, False
        generation, previous_hash, state_hash = freshness
        if checkpoint is None:
            return freshness, True, False
        known_generation, known_hash = checkpoint
        if generation < known_generation:
            raise SyncError("snapshot generation rolled back")
        if generation == known_generation:
            if state_hash != known_hash:
                raise SyncError("snapshot generation conflicts with local checkpoint")
            return freshness, False, True
        if generation == known_generation + 1 and previous_hash != known_hash:
            raise SyncError("snapshot hash chain does not extend local checkpoint")
        return freshness, False, False

    # ── export ────────────────────────────────────────────────────────────────
    def export_bundle(self, workspace_id: str, *, repo_id: Optional[str] = None,
                      _save_checkpoint: bool = True) -> dict:
        """Full-state snapshot of one workspace (all repos unless ``repo_id`` given).

        Includes invalidated memories on purpose: a closed ``valid_to`` is state that
        must propagate so a forget/correct on one device reaches the others."""
        ws_row = self.store.conn.execute(
            "SELECT name FROM workspaces WHERE id=?", (workspace_id,)).fetchone()
        ws_name = ws_row["name"] if ws_row else "default"
        if self.allowed_workspaces is not None and ws_name not in self.allowed_workspaces:
            raise SyncError("workspace %r is not authorized for sync" % ws_name)
        flt = SearchFilter(workspace_id=workspace_id, repo_id=repo_id)
        # Secret and session-scoped memories never leave the device. Include invalidated
        # public rows so forget/correct still converges, but do not let closed session history
        # become exportable. links_among() below receives only the retained ids, which also
        # prevents a link from disclosing a filtered endpoint.
        mems = [m for m in self.store.list_memories(flt, include_invalid=True)
                if m.sensitivity != "secret"
                and m.scope not in (Scope.SESSION, Scope.USER)]
        if repo_id is not None:
            repo_rows = self.store.conn.execute(
                "SELECT id, name FROM repos WHERE workspace_id=? AND id=?",
                (workspace_id, repo_id)).fetchall()
        else:
            repo_rows = self.store.conn.execute(
                "SELECT id, name FROM repos WHERE workspace_id=?", (workspace_id,)).fetchall()
        ids_in = [m.id for m in mems]
        links = self.store.links_among(ids_in, include_invalid=True) if ids_in else []
        tombstones = [
            tomb for tomb in self.store.list_memory_tombstones(workspace_id, repo_id)
            if tomb.get("export_class") == TOMBSTONE_REMOTE_ERASURE
        ]
        bundle = {
            "format": SYNC_FORMAT, "version": SYNC_VERSION,
            "device_id": _normalise_device_id(self.device_id), "created_at": now_ts(),
            "workspace_name": ws_name,
            "repos": {r["id"]: r["name"] for r in repo_rows},
            "memories": [record_to_dict(m) for m in mems],
            "tombstones": tombstones,
            "mem_links": [
                {
                    "a": ln["a"], "b": ln["b"], "relation": ln["relation"],
                    "layer": ln.get("layer") or "semantic",
                    "reason": ln.get("reason") or "",
                    "valid_from": ln.get("valid_from"),
                    "valid_to": ln.get("valid_to"),
                    "valid_to_recorded_at": ln.get("valid_to_recorded_at"),
                    "ingested_at": ln.get("ingested_at"),
                    "expired_at": ln.get("expired_at"),
                }
                for ln in links
            ],
        }
        return self._stamp_snapshot(
            bundle, workspace_id, repo_id, save_checkpoint=_save_checkpoint
        )

    # ── apply (the trust boundary) ──────────────────────────────────────────────
    def apply_bundle(self, bundle: Any, *, into_workspace: Optional[str] = None,
                     only_repo_id: Optional[str] = None, dry_run: bool = False) -> dict:
        """Merge an untrusted remote bundle into local state, re-homing it into
        ``into_workspace`` (defaults to the bundle's own workspace name). Idempotent:
        applying the same bundle twice reports the second as all-unchanged.

        Confinement: a row is only merged into an existing memory when that memory
        already lives in ``into_workspace`` — a bundle can never reach across into a
        workspace the peer wasn't syncing. ``only_repo_id`` narrows that to one repo."""
        if not isinstance(bundle, dict):
            raise SyncError("bundle is not an object")
        if bundle.get("format") != SYNC_FORMAT:
            raise SyncError("not an %s bundle" % SYNC_FORMAT)
        if _as_int(bundle.get("version"), 0) not in SYNC_ACCEPTED_VERSIONS:
            raise SyncError("unsupported bundle version %r" % bundle.get("version"))
        _validated_snapshot_freshness(bundle)
        src_device = _normalise_device_id(bundle.get("device_id"))

        mem_dicts = bundle.get("memories") or []
        link_dicts = bundle.get("mem_links") or []
        tomb_dicts = bundle.get("tombstones") or []
        if not isinstance(mem_dicts, list) or not isinstance(link_dicts, list) \
                or not isinstance(tomb_dicts, list):
            raise SyncError("bundle memories/mem_links/tombstones must be lists")
        if len(mem_dicts) > MAX_MEMORIES or len(link_dicts) > MAX_LINKS \
                or len(tomb_dicts) > MAX_TOMBSTONES:
            raise SyncError("bundle exceeds size caps")

        raw_ws_name = into_workspace if into_workspace is not None else bundle.get("workspace_name")
        if raw_ws_name is not None and not isinstance(raw_ws_name, str):
            raise SyncError("bundle workspace_name must be a string")
        ws_name = _clamp_str(raw_ws_name or "default", MAX_WORKSPACE_NAME_CHARS).strip()
        if not ws_name:
            ws_name = "default"
        if self.allowed_workspaces is not None and ws_name not in self.allowed_workspaces:
            raise SyncError("workspace %r is not authorized for sync" % ws_name)
        report = {
            "added": 0, "updated": 0, "unchanged": 0, "rejected": 0,
            "conflicts_preserved": 0, "links_added": 0, "links_updated": 0,
            "tombstones_applied": 0,
                  "workspace": ws_name, "from_device": src_device,
                  "dry_run": bool(dry_run)}

        # Resolve scope by NAME (per-device ids differ; names are the sync key). A
        # dry run must not mutate, so it resolves existing ids only and never creates.
        remote_repos = bundle.get("repos") or {}
        if not isinstance(remote_repos, dict):
            raise SyncError("bundle repos must be an object")
        if len(remote_repos) > MAX_REPOS:
            raise SyncError("bundle exceeds repo cap")
        valid_remote_repos = {
            rid: _clamp_str(rname, MAX_REPO_NAME_CHARS)
            for rid, rname in remote_repos.items()
            if isinstance(rid, str) and isinstance(rname, str) and rname
        }
        repo_remap: dict[str, Optional[str]] = {}
        if dry_run:
            row = self.store.conn.execute(
                "SELECT id FROM workspaces WHERE name=?", (ws_name,)).fetchone()
            # Use non-persisted scope sentinels when the target does not exist yet.
            # Dry-run must evaluate the same repo-scoped acceptance path as a real
            # apply; ``None`` would incorrectly reject rows that the real apply would
            # accept after creating the workspace/repository.
            local_ws = str(row["id"]) if row else f"__dry_run_workspace__:{ws_name}"
            for rid, rname in valid_remote_repos.items():
                repo_row = (self.store.conn.execute(
                    "SELECT id FROM repos WHERE workspace_id=? AND name=?",
                    (row["id"], rname)).fetchone() if row else None)
                repo_remap[rid] = (
                    repo_row["id"] if repo_row
                    else f"__dry_run_repo__:{ws_name}:{rid}"
                )
        else:
            local_ws = self.store.get_or_create_workspace(ws_name)
        accepted: dict[str, MemoryRecord] = {}
        incoming_freshness, _, _ = self._check_incoming_freshness(
            bundle, local_ws, only_repo_id, src_device
        )
        if not dry_run:
            for rid, rname in valid_remote_repos.items():
                repo_remap[rid] = self.store.get_or_create_repo(local_ws, rname)
        report["rejected"] += sum(
            1 for tomb in tomb_dicts
            if (
                not isinstance(tomb, dict)
                or tomb.get("export_class") != TOMBSTONE_REMOTE_ERASURE
            )
        )
        parsed_tombstones = self._parse_tombstones(tomb_dicts, src_device)
        accepted_tombstones: list[dict] = []
        tombstone_state_changed = False

        # Tombstones are scoped before they are applied. A bundle authorized for one
        # workspace must never hard-delete a known id owned by another workspace.
        for tomb in parsed_tombstones:
            remote_tomb_repo = tomb.get("repo_id")
            mapped_tomb_repo = (
                repo_remap.get(remote_tomb_repo)
                if remote_tomb_repo is not None else None
            )
            if remote_tomb_repo is not None and mapped_tomb_repo is None:
                report["rejected"] += 1
                continue
            existing = (
                self.store.get_memory(tomb["id"])
                if local_ws is not None else None
            )
            # Tombstone scope is durable even after the erased row disappears. Do not
            # let a same-id marker from another workspace overwrite or poison the local
            # workspace's deletion state when the id is no longer present locally.
            tombstone_row = self.store.conn.execute(
                "SELECT workspace_id, repo_id, deleted_at, export_class "
                "FROM memory_tombstones WHERE memory_id=?",
                (tomb["id"],)
            ).fetchone()
            if (tombstone_row is not None
                    and tombstone_row["workspace_id"] is not None
                    and tombstone_row["workspace_id"] != local_ws):
                report["rejected"] += 1
                continue
            # A never-export marker is durable local privacy state. A peer cannot
            # upgrade it into a shareable remote-erasure marker after the source
            # content and its classification have already been destroyed.
            if (tombstone_row is not None
                    and tombstone_row["export_class"] == TOMBSTONE_NEVER_EXPORT):
                report["rejected"] += 1
                continue
            # Once a tombstone has a repository identity, a marker from a sibling
            # repository must not overwrite it.  A NULL marker is legacy global
            # state and must not be upgraded from an incoming repository identity.
            if (tombstone_row is not None
                    and tombstone_row["repo_id"] is not None
                    and mapped_tomb_repo is not None
                    and tombstone_row["repo_id"] != mapped_tomb_repo):
                report["rejected"] += 1
                continue
            if (existing is not None and existing.workspace_id != local_ws):
                report["rejected"] += 1
                continue
            # A repo-scoped tombstone can only erase a row in that same repo.
            # Legacy repo-less markers retain their historical global-id behavior.
            if (existing is not None and mapped_tomb_repo is not None
                    and existing.repo_id != mapped_tomb_repo):
                report["rejected"] += 1
                continue
            if (existing is not None and only_repo_id is not None
                    and existing.repo_id != only_repo_id):
                report["rejected"] += 1
                continue
            if (only_repo_id is not None and mapped_tomb_repo is not None
                    and mapped_tomb_repo != only_repo_id):
                report["rejected"] += 1
                continue
            # A peer assertion is not erase authority. Protected local records require a
            # separately authenticated user/device authorization before their irreversible
            # rows and derivatives may be removed.
            if existing is not None and (
                    existing.sensitivity == "secret"
                    or existing.scope == Scope.SESSION
                    or provenance_is_approved(existing.provenance)):
                report["rejected"] += 1
                if not dry_run:
                    self.store.audit(
                        "sync:%s" % _clamp_str(src_device or "peer", 128),
                        "sync_trust_conflict",
                        existing.id,
                        "peer erasure ignored because local record is protected",
                        commit=False,
                    )
                    tombstone_state_changed = True
                continue
            # Preserve an already-known repository identity, but never infer one
            # from the live row for a legacy marker: doing so narrows a global marker
            # and permits a same-id row from a sibling repository to resurrect.
            stored_tomb_repo = mapped_tomb_repo
            if stored_tomb_repo is None and tombstone_row is not None:
                stored_tomb_repo = tombstone_row["repo_id"]
            marker_changed = (
                tombstone_row is None
                or float(tomb["deleted_at"]) < float(tombstone_row["deleted_at"])
                or tombstone_row["repo_id"] != stored_tomb_repo
                or tombstone_row["export_class"] != TOMBSTONE_REMOTE_ERASURE
            )
            accepted_tombstones.append({
                **tomb, "_mapped_repo_id": stored_tomb_repo,
            })
            if not dry_run:
                self.store.add_memory_tombstone(
                    tomb["id"], deleted_at=tomb["deleted_at"],
                    device_id=tomb["device"], workspace_id=local_ws,
                    repo_id=stored_tomb_repo,
                    export_class=TOMBSTONE_REMOTE_ERASURE,
                )
                tombstone_state_changed = True
                # A peer's secure erase must remove a row this device still holds
                # immediately, not only block a future re-add.
                if existing is not None:
                    try:
                        self.store._erase_memory_rows(
                            self.store.conn, tomb["id"], actor="sync_tombstone"
                        )
                    except Exception:  # noqa: BLE001 — never leave erased data resident
                        # The tombstone must not be treated as successfully applied if
                        # local derivative cleanup failed. Roll back this tombstone batch
                        # so a retry can recover instead of leaving stale content behind.
                        self.store.conn.rollback()
                        raise
            if marker_changed or dry_run:
                report["tombstones_applied"] += 1
        if not dry_run and (accepted_tombstones or tombstone_state_changed):
            self.store.conn.commit()

        # Bulk apply. Previously this was N+1: a SELECT per id to test existence, then a
        # Store.add_memory that did its own dupe-check SELECT, INSERT, FTS delete+insert,
        # vector upsert AND its own commit() — one durability fsync per row, up to
        # MAX_MEMORIES times. Now: one batched existence lookup and one transaction per
        # APPLY_BATCH rows.
        #
        # Batching rather than a single bundle-wide transaction is deliberate and preserves
        # two properties. Peak memory stays bounded at MAX_MEMORIES scale (rows are parsed
        # a batch at a time, not 200k at once). And a failure part-way through still leaves
        # the rows that already committed applied — the same partial-apply outcome callers
        # see today, since SyncEngine.sync catches per-bundle and records the error rather
        # than retrying; one wide transaction would silently roll the whole bundle back.
        try:
            self._apply_memories(mem_dicts, report, accepted, local_ws,
                                 repo_remap, only_repo_id, src_device, dry_run,
                                 accepted_tombstones)
            self._apply_links(link_dicts, report, accepted, local_ws,
                              only_repo_id, src_device, dry_run)
        except BaseException:
            # Never leave the shared connection pinned in an open transaction — that would
            # stall every other thread on _SerializedConnection's lock. Roll back only the
            # in-flight batch; earlier APPLY_BATCH commits already preserve partial apply.
            try:
                self.store.conn.rollback()
            except Exception:  # noqa: BLE001 — best-effort cleanup
                pass
            raise
        if not dry_run and incoming_freshness is not None:
            self._save_snapshot_checkpoint(
                local_ws,
                only_repo_id,
                src_device,
                incoming_freshness[0],
                incoming_freshness[2],
            )
        return report

    def _apply_memories(self, mem_dicts: list, report: dict,
                        accepted: dict[str, MemoryRecord], local_ws, repo_remap: dict,
                        only_repo_id, src_device, dry_run: bool,
                        tombstones: Optional[list[dict]] = None) -> None:
        # Keep repository identity with the terminal marker.  A known repo marker
        # must not reject a same-id memory from a sibling repo; a legacy NULL repo
        # marker remains global for backward compatibility.
        live_tombstones = (
            {
                t["id"]: (float(t["deleted_at"]), t.get("repo_id"))
                for t in self.store.list_memory_tombstones(local_ws)
            }
            if local_ws is not None else {}
        )
        for tomb in tombstones or []:
            timestamp = float(tomb["deleted_at"])
            mapped_repo = tomb.get("_mapped_repo_id")
            if mapped_repo is None and tomb.get("repo_id") is not None:
                mapped_repo = repo_remap.get(tomb["repo_id"])
            existing = live_tombstones.get(tomb["id"])
            if existing is None:
                live_tombstones[tomb["id"]] = (timestamp, mapped_repo)
            elif existing[1] is None:
                # A legacy marker is global.  Never upgrade it to a repository
                # identity merely because a newer peer also knows a repo scope.
                continue
            elif mapped_repo is not None and timestamp < existing[0]:
                live_tombstones[tomb["id"]] = (timestamp, mapped_repo)
        for start in range(0, len(mem_dicts), APPLY_BATCH):
            batch = mem_dicts[start:start + APPLY_BATCH]
            pending_index_actions: list[_VectorIndexAction] = []
            parsed = [dict_to_record(d) for d in batch]
            # One IN(...) lookup for the whole batch instead of get_memory() per row.
            # ``known`` doubles as the write-through cache so a duplicate id LATER in the
            # same batch still sees the row this loop just wrote, exactly as the per-row
            # get_memory() did.
            known = self.store.get_memories(
                [rec.id for rec in parsed if rec is not None])
            # Dry-run has no durable prior batch to query, so carry its simulated state
            # across APPLY_BATCH boundaries. Live apply must trust the fresh DB lookup:
            # a local edit may legitimately land between committed batches.
            if dry_run:
                for rec in parsed:
                    if rec is not None and rec.id in accepted:
                        known[rec.id] = accepted[rec.id]
            for d, rec in zip(batch, parsed):
                self._apply_one(d, rec, report, accepted, known, local_ws,
                                repo_remap, only_repo_id, src_device, dry_run,
                                live_tombstones, pending_index_actions)
            if not dry_run:
                self.store.conn.commit()
                self._publish_index_actions(pending_index_actions)

    def _apply_one(self, d: dict, rec, report: dict, accepted: dict, known: dict,
                   local_ws, repo_remap: dict, only_repo_id, src_device,
                   dry_run: bool, live_tombstones: Optional[dict] = None,
                   pending_index_actions: Optional[list[_VectorIndexAction]] = None,
                   ) -> None:
        pending_index_actions = (
            pending_index_actions if pending_index_actions is not None else []
        )
        if rec is None:
            report["rejected"] += 1
            return
        # Sync bundles have no authenticated session owner or session lifecycle metadata.
        # Never create or merge private session state from an untrusted/legacy peer, even in
        # dry-run mode or when the incoming id already exists locally.
        if rec.scope == Scope.SESSION:
            report["rejected"] += 1
            return
        rec.session_id = None
        remote_repo_id = d.get("repo_id")
        raw_scope = d.get("scope")
        if raw_scope is None:
            # Sync v1 allowed callers to omit scope. Preserve that compatibility while
            # canonicalizing the row: a repo pointer means repo scope; otherwise the row
            # belongs to the workspace. Never persist the old invalid repo-without-owner
            # default produced by ``_scope(None)``.
            rec.scope = Scope.REPO if remote_repo_id is not None else Scope.WORKSPACE
        elif not isinstance(raw_scope, str) or raw_scope not in _VALID_SCOPES:
            report["rejected"] += 1
            return
        # Scope pointers are an untrusted trust-boundary input, not merely metadata.
        # A repo-scoped row must name one of the bundle's repos; workspace/user rows
        # must not carry a repo pointer.  Accepting an invalid combination and then
        # re-homing it would turn a repo-owned row into an ancestor-visible global row.
        if rec.scope == Scope.REPO:
            if not isinstance(remote_repo_id, str) or not remote_repo_id:
                report["rejected"] += 1
                return
        elif remote_repo_id is not None:
            report["rejected"] += 1
            return
        # Re-home into local scope, and tag provenance with the origin device so a
        # synced-in memory stays auditable ("why is this known?" — AGENTS.md §3.6).
        rec.workspace_id = local_ws
        if remote_repo_id:
            if (remote_repo_id not in repo_remap
                    or repo_remap[remote_repo_id] is None):
                # Dry-run does not create missing repositories. A repo-scoped row whose
                # source repo cannot be resolved must still be rejected; accepting it
                # with repo_id=None would silently broaden visibility to the workspace.
                report["rejected"] += 1
                return
            rec.repo_id = repo_remap[remote_repo_id]
        else:
            rec.repo_id = None
        # A known repository tombstone is terminal only for that repository.  Legacy
        # repo-less tombstones intentionally retain their historical global-id behavior.
        tombstone = (live_tombstones or {}).get(rec.id)
        if tombstone is not None:
            tombstone_repo = tombstone[1] if isinstance(tombstone, tuple) else None
            if tombstone_repo is None or tombstone_repo == rec.repo_id:
                report["rejected"] += 1
                return
        if only_repo_id is not None and rec.repo_id != only_repo_id:
            report["rejected"] += 1
            return
        existing = known.get(rec.id)
        # Missing store-required clocks must be canonical per candidate. Inheriting
        # them from whichever version arrived first makes legacy merge non-commutative.
        _initialize_sync_store_defaults(rec)
        if existing is not None and existing.workspace_id != local_ws:
            # This id already lives in a DIFFERENT workspace: never let a bundle reach
            # across the scope boundary (SECURITY.md §3 confinement).
            report["rejected"] += 1
            return
        if existing is not None and existing.sensitivity == "secret":
            # ``secret`` is device-local by contract. A peer may know this id from an
            # older sync that happened before the memory was classified secret, but it
            # must never be able to overwrite, invalidate, or downgrade the local row
            # back to an exportable sensitivity.
            report["rejected"] += 1
            return
        if existing is not None and existing.scope == Scope.SESSION:
            # A peer that learned an id before this boundary was enforced cannot relabel or
            # overwrite the local private row with a non-session scope either.
            report["rejected"] += 1
            return
        if (existing is not None
                and (existing.scope != rec.scope or existing.repo_id != rec.repo_id)):
            # ``merge_record`` deliberately keeps scope pointers local.  Letting the
            # descriptive LWW winner change ``scope`` while retaining the existing local
            # pointer would therefore create an impossible row (for example a
            # workspace-scoped memory still attached to a repo), and could make a
            # repo-owned fact ancestor-visible.  Scope promotion is a local, explicit
            # operation; a sync peer may merge a record only at its existing visibility.
            # This also fails closed for malformed legacy rows: repairing an orphaned
            # scope is a local migration decision, never authority delegated to a peer.
            report["rejected"] += 1
            return
        if existing is not None:
            # Sync v1 bundles predate durable claim identity. Omission means
            # "unknown to this peer", not an instruction to erase local keys.
            if "subject_key" not in d:
                rec.subject_key = existing.subject_key
            if "claim_kind" not in d:
                rec.claim_kind = existing.claim_kind
            if "valid_to_recorded_at" not in d and rec.valid_to == existing.valid_to:
                rec.valid_to_recorded_at = existing.valid_to_recorded_at
        if (existing is not None and only_repo_id is not None
                and existing.repo_id != only_repo_id):
            # The incoming row's claimed repo cannot re-home an existing memory from
            # another repo during a repo-restricted sync.
            report["rejected"] += 1
            return
        # A peer has no authority to revise a locally approved record. Keep the
        # local trusted fact as the safe winner; the peer's payload is never merged
        # into its provenance, graph state, or temporal validity. This runs after
        # all scope checks above so malformed remote rows are still rejected rather
        # than being disguised as harmless trust conflicts.
        if existing is not None and provenance_is_approved(existing.provenance):
            content_changed = rec.content != existing.content
            self._rehome_external_record(rec, src_device=src_device)
            conflict_action = self._preserve_hlc_conflict(
                existing, rec, report=report, known=known, dry_run=dry_run,
            )
            if conflict_action is not None:
                pending_index_actions.append(conflict_action)
            if not dry_run and content_changed:
                self.store.audit(
                    "sync:%s" % _clamp_str(src_device or "peer", 128),
                    "sync_trust_conflict",
                    existing.id,
                    "peer content ignored because local record is explicitly trusted",
                    commit=False,
                )
            accepted[rec.id] = existing
            report["unchanged"] += 1
            return
        # A bundle is untrusted even when it originated on a known device.  Preserve
        # only bounded diagnostic identity and re-home all payload provenance under
        # the local policy; a peer cannot make content trusted by serialising that
        # bit in the bundle.  This path bypasses MemoryEngine, so it performs the
        # same quarantine decision before it can be indexed.
        #
        # An idempotent replay may omit both policy-managed blobs.  Re-homing such a
        # no-op would manufacture a provenance/metadata difference, let the hash
        # tiebreak select it, and rewrite the otherwise identical row forever.  Keep
        # the already-local policy envelope only when every sync-owned descriptive
        # value is the same and the peer supplied neither blob.  Any actual content,
        # timestamp, or metadata/provenance change still receives a fresh untrusted
        # envelope below.
        if (existing is not None and "metadata" not in d and "provenance" not in d
                and _same_sync_payload(existing, rec)):
            rec.metadata = dict(existing.metadata or {})
            rec.provenance = dict(existing.provenance or {})
        else:
            self._rehome_external_record(rec, src_device=src_device)
        # Quarantine is sticky across peer last-writer-wins updates. A benign-looking
        # same-id payload must not erase a local governance decision; only the local
        # interactive approval path may create a separate approved successor.
        if existing is not None and (
            metadata_is_quarantined(existing.metadata or {})
            or bool((existing.provenance or {}).get("quarantined"))
        ):
            # Merge inherited quarantine with any existing reasons so the audit trail
            # preserves why the record was originally quarantined (e.g. prompt injection)
            # alongside the inheritance marker.
            prior_reasons: tuple[str, ...] = ()
            prior_q = (existing.metadata or {}).get("quarantine")
            if isinstance(prior_q, dict):
                raw = prior_q.get("reasons") or ()
                if isinstance(raw, (list, tuple)):
                    prior_reasons = tuple(str(r) for r in raw if isinstance(r, str))
            merged_reasons = tuple(dict.fromkeys((*prior_reasons, "inherited_quarantine")))
            rec.metadata = apply_quarantine_metadata(
                rec.metadata, PoisoningDecision(True, reasons=merged_reasons)
            )
            rec.provenance = dict(rec.metadata["provenance"])
            # Preserve the locally governed start boundary rather than letting a peer's
            # LWW timestamps reactivate or future-date a quarantined record. A peer
            # overwrite closes an open quarantined interval at the sync boundary, which
            # keeps the replaced payload out of ordinary retrieval while retaining its
            # history for governed inspection.
            rec.valid_from = existing.valid_from
            rec.valid_to = (
                existing.valid_to if existing.valid_to is not None else now_ts()
            )
            rec.valid_to_recorded_at = now_ts()
            rec.embedding = None
        if existing is not None:
            conflict_action = self._preserve_hlc_conflict(
                existing, rec, report=report, known=known, dry_run=dry_run,
            )
            if conflict_action is not None:
                pending_index_actions.append(conflict_action)
        if existing is None:
            if not dry_run:
                index_action = self._write(rec, commit=False)
                if index_action is not None:
                    pending_index_actions.append(index_action)
                self.store.audit(
                    "sync:%s" % _clamp_str(src_device or "peer", 128),
                    "sync_add", rec.id,
                    f"new memory created from synced bundle (device: {src_device or 'peer'})",
                    commit=False)
                if metadata_is_quarantined(rec.metadata):
                    self.store.audit(
                        "poisoning_policy", "sync_quarantine", rec.id,
                        "synced record quarantined by deterministic policy",
                        commit=False,
                    )
            # Keep the dry-run view write-through as well: duplicate ids in one
            # bundle must be evaluated against the first row, not the pre-bundle store.
            known[rec.id] = rec
            report["added"] += 1
            accepted[rec.id] = rec
        else:
            accepted[rec.id] = existing
            merged = merge_record(existing, rec)
            if _signature(merged) == _signature(existing):
                report["unchanged"] += 1
            else:
                if not dry_run:
                    index_action = self._write(merged, commit=False)
                    if index_action is not None:
                        pending_index_actions.append(index_action)
                    # A synced bundle overwriting existing content is exactly the
                    # memory-poisoning surface (SECURITY.md): record who/what so the
                    # overwrite is never silent and "why is this known?" stays answerable.
                    self.store.audit(
                        "sync:%s" % _clamp_str(src_device or "peer", 128),
                        "sync_overwrite", merged.id,
                        "content replaced by synced bundle (last-writer-wins)",
                        commit=False)
                # Keep duplicate processing deterministic during dry-run too.
                known[rec.id] = merged
                report["updated"] += 1
                accepted[rec.id] = merged

    def _apply_links(self, link_dicts: list, report: dict, accepted: dict,
                     local_ws, only_repo_id, src_device, dry_run: bool) -> None:
        # mem_links: grow-only set; endpoints must be memories we actually hold.
        pending = 0

        def owner_row(memory: MemoryRecord) -> dict:
            return {
                "id": memory.id,
                "workspace_id": memory.workspace_id,
                "repo_id": memory.repo_id,
                "session_id": memory.session_id,
                "scope": _enum(memory.scope),
                "metadata": json.dumps(memory.metadata or {}, default=str),
                "provenance": json.dumps(memory.provenance or {}, default=str),
            }
        for ln in link_dicts:
            if not isinstance(ln, dict):
                continue
            a, b = ln.get("a"), ln.get("b")
            rel = _clamp_str(ln.get("relation") or "related", 64) or "related"
            layer = normalize_graph_layer(ln.get("layer"), rel)
            reason = _clamp_str(ln.get("reason") or "", MAX_TITLE_CHARS)
            if secret_kind(reason):
                report["rejected"] += 1
                continue
            if not isinstance(a, str) or not isinstance(b, str) or a == b:
                continue
            if a not in accepted or b not in accepted:
                continue
            ma, mb = accepted[a], accepted[b]
            if local_ws is not None and (ma.workspace_id != local_ws
                                         or mb.workspace_id != local_ws):
                continue
            if (only_repo_id is not None
                    and (ma.repo_id != only_repo_id or mb.repo_id != only_repo_id)):
                continue
            allow_scope_transition = rel in {"promotes", "merges"}
            first_owner = (ma.repo_id, ma.session_id, _enum(ma.scope))
            second_owner = (mb.repo_id, mb.session_id, _enum(mb.scope))
            if allow_scope_transition and first_owner != second_owner:
                try:
                    # Use Store's single governance rule even for dry-run records that
                    # deliberately do not exist in SQLite yet.
                    self.store._validate_memory_link_owner_rows(
                        owner_row(ma),
                        owner_row(mb),
                        rel,
                        allow_scope_transition=True,
                    )
                except ValueError:
                    report["rejected"] += 1
                    continue
            # Link records carry no independent authenticated provenance. A peer
            # therefore cannot attach an arbitrary graph edge to a locally approved
            # memory, where it could influence graph recall despite the peer payload
            # itself being untrusted. Links wholly inside the untrusted replica stay
            # inspectable, but only a local trusted write may connect trusted nodes.
            if (prompt_eligible(ma.provenance, ma.metadata)
                    or prompt_eligible(mb.provenance, mb.metadata)):
                continue
            pending += 1
            if pending >= APPLY_BATCH:
                if not dry_run:
                    self.store.conn.commit()
                pending = 0
            # v2 bundles carry a complete bi-temporal link version. Preserve accepted
            # timestamps verbatim, including closed intervals. A partial or invalid
            # temporal payload is rejected rather than reinterpreted as a v1 link.
            temporal_fields = (
                "valid_from", "valid_to", "valid_to_recorded_at",
                "ingested_at", "expired_at",
            )
            if any(field_name in ln for field_name in temporal_fields):
                link_now = now_ts()
                valid_from = _clamp_world_ts(ln.get("valid_from"))
                valid_to = _clamp_world_ts(ln.get("valid_to"))
                valid_to_recorded_at = _clamp_ts(
                    ln.get("valid_to_recorded_at"), link_now
                )
                ingested_at = _clamp_ts(ln.get("ingested_at"), link_now)
                expired_at = _clamp_ts(ln.get("expired_at"), link_now)
                parsed_temporal = {
                    "valid_from": valid_from,
                    "valid_to": valid_to,
                    "valid_to_recorded_at": valid_to_recorded_at,
                    "ingested_at": ingested_at,
                    "expired_at": expired_at,
                }
                if (
                    "valid_from" not in ln
                    or "ingested_at" not in ln
                    or valid_from is None
                    or ingested_at is None
                    or (valid_to is not None and valid_to < valid_from)
                    or any(
                        ln.get(field_name) is not None and value is None
                        for field_name, value in parsed_temporal.items()
                    )
                ):
                    report["rejected"] += 1
                    continue
                existing_version = self.store.conn.execute(
                    "SELECT 1 FROM mem_links "
                    "WHERE ((a=? AND b=?) OR (a=? AND b=?)) AND relation=? "
                    "AND layer=? AND reason=? AND valid_from IS ? AND valid_to IS ? "
                    "AND valid_to_recorded_at IS ? AND ingested_at IS ? AND expired_at IS ? "
                    "LIMIT 1",
                    (
                        a, b, b, a, rel, layer.value, reason,
                        valid_from, valid_to, valid_to_recorded_at, ingested_at, expired_at,
                    ),
                ).fetchone()
                if existing_version:
                    continue
                if not dry_run:
                    inserted = self.store.add_link_version(
                        a, b, rel, layer=layer, reason=reason,
                        valid_from=valid_from, valid_to=valid_to,
                        valid_to_recorded_at=valid_to_recorded_at,
                        ingested_at=ingested_at, expired_at=expired_at,
                        commit=False,
                        allow_scope_transition=allow_scope_transition,
                    )
                    if inserted:
                        self.store.audit(
                            "sync:%s" % _clamp_str(src_device or "peer", 128),
                            "sync_link", a,
                            f"linked to {b} with relation {rel}", commit=False)
                report["links_added"] += 1
                continue
            existing_link = self.store.conn.execute(
                "SELECT layer, reason FROM mem_links "
                "WHERE ((a=? AND b=?) OR (a=? AND b=?)) AND relation=? "
                "AND valid_to IS NULL AND expired_at IS NULL "
                "ORDER BY rowid DESC LIMIT 1",
                (a, b, b, a, rel),
            ).fetchone()
            if existing_link:
                # Link metadata has no clock in sync format v1. Resolve concurrent
                # metadata deterministically so peers converge regardless of arrival.
                merged_layer = merge_graph_layers(existing_link["layer"], layer, rel)
                merged_reason = max(existing_link["reason"] or "", reason)
                if (merged_layer.value, merged_reason) == (
                    existing_link["layer"] or "semantic",
                    existing_link["reason"] or "",
                ):
                    continue
                if not dry_run:
                    self.store.add_link(
                        a, b, rel, layer=merged_layer, reason=merged_reason,
                        commit=False,
                        allow_scope_transition=allow_scope_transition,
                    )
                report["links_updated"] += 1
                continue
            if not dry_run:
                self.store.add_link(
                    a, b, rel, layer=layer, reason=reason, commit=False,
                    allow_scope_transition=allow_scope_transition,
                )
                self.store.audit(
                    "sync:%s" % _clamp_str(src_device or "peer", 128),
                    "sync_link", a,
                    f"linked to {b} with relation {rel}", commit=False)
            report["links_added"] += 1
        if not dry_run:
            self.store.conn.commit()

    def _parse_tombstones(self, tomb_dicts: list, src_device: object) -> list[dict]:
        """Validate + clamp untrusted bundle tombstones. Never raises.

        A tombstone is ``{id, deleted_at, device, repo_id, export_class}`` — no
        content — so there is nothing to quarantine; it is clamped like any other
        untrusted input and a malformed entry is silently dropped. Only an explicit
        ``remote_erasure`` classification grants propagation authority; legacy or
        unknown classifications fail closed. ``deleted_at`` is bounded to
        ``[0, now + skew]`` so a hostile far-future erasure cannot permanently
        tombstone a memory id. A missing ``repo_id`` is a legacy global marker.
        """
        # Scope is part of tombstone identity now.  Keep the earliest event for
        # each (memory id, repository) pair, but a legacy repo-less marker is
        # global and therefore suppresses every repo-scoped marker for that id.
        best: dict[tuple[str, Optional[str]], dict] = {}
        positions: dict[tuple[str, Optional[str]], int] = {}
        now = now_ts()
        for t in tomb_dicts:
            if not isinstance(t, dict):
                continue
            if t.get("export_class") != TOMBSTONE_REMOTE_ERASURE:
                continue
            mid = t.get("id")
            deleted_at = _as_float(t.get("deleted_at"), None)
            if not isinstance(mid, str) or not mid or deleted_at is None:
                continue
            # Identity fields are never normalized: control characters or whitespace
            # must not be stripped into another valid memory id before secure erase.
            if (mid != mid.strip() or any(char.isspace() for char in mid)
                    or mid != _clamp_str(mid, 128)):
                continue
            deleted_at = max(0.0, min(deleted_at, now + TS_FUTURE_SKEW))
            try:
                device = (
                    _normalise_device_id(t.get("device"))
                    if t.get("device") else _normalise_device_id(src_device)
                )
            except SyncError:
                device = _normalise_device_id(src_device)
            repo_id = (
                _clamp_str(t.get("repo_id"), 128)
                if isinstance(t.get("repo_id"), str) and t.get("repo_id")
                else None
            )
            key = (mid, repo_id)
            previous = best.get(key)
            if previous is not None and previous["deleted_at"] <= deleted_at:
                continue
            best[key] = {
                "id": mid, "deleted_at": deleted_at,
                "device": device or (_clamp_str(src_device, 128) if src_device else ""),
                "repo_id": repo_id,
                "export_class": TOMBSTONE_REMOTE_ERASURE,
            }
            if key not in positions:
                positions[key] = len(positions)
        global_ids = {mid for mid, repo_id in best if repo_id is None}
        return [
            best[key] for key in positions
            if key[1] is None or key[0] not in global_ids
        ]

    def _audit_index_failure(
        self,
        action: str,
        memory_id: str,
        exc: Exception,
    ) -> None:
        """Record derived-index repair debt without reflecting provider details."""
        failure_type = type(exc).__name__
        logger.warning(
            "sync vector-index %s failed for %s (%s)",
            action,
            memory_id,
            failure_type,
        )
        try:
            self.store.audit(
                "sync",
                "index_%s_failed" % action,
                memory_id,
                "failure_type=%s" % failure_type,
                commit=not self.store.conn.transaction_owned_by_current_thread(),
            )
        except Exception as audit_exc:
            logger.warning(
                "could not audit sync vector-index failure (%s)",
                type(audit_exc).__name__,
            )

    @staticmethod
    def _hlc_conflict(
        left: MemoryRecord,
        right: MemoryRecord,
    ) -> Optional[tuple[int, int, str, str]]:
        """Return logical time and payload hashes for one concurrent HLC conflict."""
        if not left.modified_hlc or not right.modified_hlc:
            return None
        left_physical, left_logical, _ = parse_modified_hlc(left.modified_hlc)
        right_physical, right_logical, _ = parse_modified_hlc(right.modified_hlc)
        if (left_physical, left_logical) != (right_physical, right_logical):
            return None
        left_hash = _stable_hash(_label_tuple(left))
        right_hash = _stable_hash(_label_tuple(right))
        if left_hash == right_hash:
            return None
        return left_physical, left_logical, left_hash, right_hash

    def _preserve_hlc_conflict(
        self,
        existing: MemoryRecord,
        incoming: MemoryRecord,
        *,
        report: dict,
        known: dict,
        dry_run: bool,
    ) -> Optional[_VectorIndexAction]:
        """Keep the losing concurrent edit as one deterministic untrusted successor."""
        conflict = self._hlc_conflict(existing, incoming)
        if conflict is None:
            return None
        physical, logical, existing_hash, incoming_hash = conflict
        winner = (
            existing
            if _version_key(existing) >= _version_key(incoming)
            else incoming
        )
        loser = incoming if winner is existing else existing
        winner_hash = existing_hash if winner is existing else incoming_hash
        loser_hash = incoming_hash if winner is existing else existing_hash
        variants = sorted((
            (existing.modified_hlc, existing_hash),
            (incoming.modified_hlc, incoming_hash),
        ))
        digest = _stable_hash({
            "kind": "sync_hlc_conflict_v1",
            "memory_id": existing.id,
            "logical_time": [physical, logical],
            "variants": variants,
        })
        conflict_id = _conflict_memory_id(physical, digest)
        already_preserved = known.get(conflict_id)
        if already_preserved is None and not dry_run:
            already_preserved = self.store.get_memory(conflict_id)
        metadata = dict(loser.metadata or {})
        # Preserve quarantine before stripping local fields — conflict successors
        # derived from quarantined payloads must remain quarantined so _write
        # skips embedding poisoned content into the vector index.
        _preserved_quarantine = metadata.get("quarantine")
        for key in _LOCAL_METADATA_FIELDS:
            metadata.pop(key, None)
        if _preserved_quarantine is not None:
            metadata["quarantine"] = _preserved_quarantine
        conflict_provenance = {
            "source": "sync_conflict",
            "trusted": False,
            "review_state": "pending",
            "trust_origin": "sync_untrusted",
            "conflict_of": existing.id,
        }
        _, _, loser_node = parse_modified_hlc(loser.modified_hlc)
        conflict_provenance["synced_from_device"] = loser_node
        metadata["sync_conflict"] = {
            "memory_id": existing.id,
            "logical_time": f"{physical:012X}:{logical:08X}",
            "winner_hlc": winner.modified_hlc,
            "loser_hlc": loser.modified_hlc,
            "winner_hash": winner_hash,
            "loser_hash": loser_hash,
        }
        metadata["provenance"] = dict(conflict_provenance)
        preserved = MemoryRecord(
            id=conflict_id,
            content=loser.content,
            mtype=loser.mtype,
            scope=loser.scope,
            workspace_id=existing.workspace_id,
            repo_id=existing.repo_id,
            session_id=None,
            title=loser.title,
            summary=loser.summary,
            keywords=list(loser.keywords or []),
            metadata=metadata,
            importance=loser.importance,
            surprise=loser.surprise,
            stability=loser.stability,
            access_count=loser.access_count,
            last_access=loser.last_access,
            valid_from=loser.valid_from,
            valid_to=loser.valid_to,
            ingested_at=loser.ingested_at,
            expired_at=loser.expired_at,
            subject_key=loser.subject_key,
            claim_kind=loser.claim_kind,
            pinned=loser.pinned,
            sensitivity=loser.sensitivity,
            provenance=conflict_provenance,
            valid_to_recorded_at=loser.valid_to_recorded_at,
            pinned_at=loser.pinned_at,
            unpinned_at=loser.unpinned_at,
            confidence=loser.confidence,
            modified_hlc=loser.modified_hlc,
        )
        if already_preserved is not None:
            marker = (already_preserved.metadata or {}).get("sync_conflict")
            expected = metadata["sync_conflict"]
            core_keys = (
                "memory_id", "logical_time", "winner_hlc", "loser_hlc",
                "winner_hash", "loser_hash",
            )
            if (
                not isinstance(marker, dict)
                or any(marker.get(key) != expected[key] for key in core_keys)
                or already_preserved.modified_hlc != preserved.modified_hlc
                or not _same_sync_payload(already_preserved, preserved)
            ):
                raise SyncError("sync conflict identity collision")
            return None
        index_action = None
        if not dry_run:
            index_action = self._write(preserved, commit=False)
            self.store.audit(
                "sync",
                "sync_conflict_preserved",
                conflict_id,
                (
                    f"concurrent variant of {existing.id} preserved at "
                    f"{physical:012X}:{logical:08X}; "
                    f"winner={winner_hash}; loser={loser_hash}"
                ),
                commit=False,
            )
        known[conflict_id] = preserved
        report["conflicts_preserved"] += 1
        return index_action

    def _write(
        self, rec: MemoryRecord, *, commit: bool = True,
    ) -> Optional[_VectorIndexAction]:
        """Persist a merged/new record verbatim (ids + timestamps preserved) and keep
        derived state coherent: re-embed for the vector arm when an embedder is wired.

        ``commit=False`` leaves the transaction open for the caller's batch (apply_bundle)."""
        quarantined = metadata_is_quarantined(rec.metadata)
        external_index_action = None
        persistent_store = (
            self.store.path != ":memory:"
            and not self.store.path.startswith("file::memory:")
        )
        embedder = self.embedder
        rebuild_target = (
            self.store.embedding_rebuild_target() if persistent_store else None
        )
        if rebuild_target and rebuild_target != self.embedding_space:
            raise RuntimeError(
                "sync embedding space does not match the active rebuild target"
            )
        vector_writes_ready = (
            not persistent_store
            or self.store.embedding_space_ready(self.embedding_space)
            or rebuild_target == self.embedding_space
        )
        if embedder is not None and vector_writes_ready and not quarantined:
            try:
                text = f"{rec.title}\n{rec.content}" if rec.title else rec.content
                rec.embedding = embedder.embed([text])[0]
                rec.metadata = {
                    **(rec.metadata or {}),
                    "embed_model": self.embedding_space,
                }
            except Exception as exc:
                logger.warning(
                    "sync embedding failed for %s (%s)",
                    rec.id,
                    type(exc).__name__,
                )
                raise RuntimeError("sync embedding unavailable") from exc
        # sync logs its own semantic audit (sync_add/sync_overwrite), hence audit=False.
        # Preserve an empty v1/v2 clock so later legacy versions still resolve by the
        # deterministic legacy key; stamping the first arrival with a local v13 HLC
        # would make it permanently beat every subsequent legacy update.
        self.store.add_memory(
            rec,
            audit=False,
            commit=False,
            _preserve_legacy_modified_hlc=True,
        )
        if quarantined:
            # ``add_memory(..., embedding=None)`` deliberately leaves an existing
            # vector untouched for ordinary metadata updates. A sync overwrite that
            # becomes quarantined is different: retaining the prior vector leaves
            # stale derived state for a payload the policy has removed from retrieval.
            self.store.conn.execute("DELETE FROM mem_vectors WHERE id=?", (rec.id,))
            if (
                self.index is not None
                and vector_index_requires_sync(self.index, self.store)
            ):
                if vector_index_shares_store_transaction(self.index, self.store):
                    try:
                        self.index.delete([rec.id], commit=False)
                    except Exception as exc:
                        self._audit_index_failure("delete", rec.id, exc)
                else:
                    external_index_action = ("delete", rec.id, None, "")
            if commit:
                self.store.conn.commit()
                self._publish_index_actions([external_index_action])
                return None
            return external_index_action
        if (
            rec.embedding is not None
            and not quarantined
            and self.index is not None
            and vector_index_requires_sync(self.index, self.store)
        ):
            if vector_index_shares_store_transaction(self.index, self.store):
                try:
                    self.index.upsert(
                        [rec.id], rec.embedding.reshape(1, -1),
                        [{"model": self.embedding_space}],
                        commit=False,
                    )
                except Exception as exc:
                    self._audit_index_failure("upsert", rec.id, exc)
            else:
                external_index_action = (
                    "upsert", rec.id, rec.embedding.copy(), self.embedding_space,
                )
        if commit:
            self.store.conn.commit()
            self._publish_index_actions([external_index_action])
            return None
        return external_index_action

    def _publish_index_actions(
        self, actions: Iterable[Optional[_VectorIndexAction]],
    ) -> None:
        """Publish committed Store vectors to a separately-backed index.

        Coalescing by id avoids exposing intermediate vectors when a bundle repeats one
        memory inside a batch. Provider failures remain content-free repair debt while
        the already-committed canonical memory stays available.
        """
        latest: dict[str, _VectorIndexAction] = {}
        for action in actions:
            if action is not None:
                latest[action[1]] = action
        index = self.index
        if index is None:
            return
        for operation, memory_id, vector, model in latest.values():
            try:
                if operation == "delete":
                    index.delete([memory_id])
                elif operation == "upsert" and vector is not None:
                    index.upsert(
                        [memory_id], vector.reshape(1, -1), [{"model": model}],
                    )
                else:  # pragma: no cover - actions are constructed locally
                    raise RuntimeError("invalid deferred vector-index action")
            except Exception as exc:  # noqa: BLE001 - canonical Store state is committed
                self._audit_index_failure(operation, memory_id, exc)

    @staticmethod
    def _rehome_external_record(rec: MemoryRecord, *, src_device: object) -> None:
        """Replace peer control data with a canonical local untrusted envelope."""
        upstream = rec.provenance if isinstance(rec.provenance, dict) else {}
        upstream_source = _clamp_str(upstream.get("source"), 128)
        device = _clamp_str(src_device, 128) if src_device else ""
        metadata = dict(rec.metadata or {})
        marker = metadata.get("sync_conflict")
        conflict_of = marker.get("memory_id") if isinstance(marker, dict) else None
        conflict_hlc = marker.get("loser_hlc") if isinstance(marker, dict) else None
        is_conflict = (
            isinstance(conflict_of, str)
            and bool(conflict_of)
            and conflict_of == conflict_of.strip()
            and not any(char.isspace() for char in conflict_of)
            and conflict_of == _clamp_str(conflict_of, 128)
            and conflict_hlc == rec.modified_hlc
            and bool(rec.modified_hlc)
        )
        provenance = {
            "source": "sync_conflict" if is_conflict else "sync",
            "trusted": False,
            "review_state": "pending",
            "trust_origin": "sync_untrusted",
        }
        if is_conflict:
            _, _, conflict_node = parse_modified_hlc(rec.modified_hlc)
            provenance["conflict_of"] = conflict_of
            provenance["synced_from_device"] = conflict_node
        elif device:
            provenance["synced_from_device"] = device
        # Incoming control-plane keys must never survive as if this process had
        # produced them.  Record only a bounded diagnostic summary of the upstream
        # claim; raw source metadata remains in the peer's bundle, not local policy.
        for key in _LOCAL_METADATA_FIELDS:
            metadata.pop(key, None)
        metadata["provenance"] = dict(provenance)
        metadata["sync_ingress"] = {
            "source": upstream_source or "unknown",
            "claimed_trusted": upstream.get("trusted") is True,
            "device": device or "peer",
        }
        decision = assess_untrusted_payload(
            rec.content, title=rec.title, metadata=metadata
        )
        if decision.quarantined:
            metadata = apply_quarantine_metadata(metadata, decision)
            at = rec.valid_from if rec.valid_from is not None else now_ts()
            rec.valid_from = at
            rec.valid_to = at
            rec.valid_to_recorded_at = now_ts()
            rec.embedding = None
        rec.metadata = metadata
        rec.provenance = dict(metadata["provenance"])

    # ── one round-trip over a transport ─────────────────────────────────────────
    def sync(self, transport: SyncTransport, workspace_id: str, *,
             repo_id: Optional[str] = None, dry_run: bool = False,
             push: bool = True) -> dict:
        """Pull and merge authenticated snapshots before replacing this device's copy.

        Per-device generation checkpoints reject rollback after a snapshot has been
        observed. A first snapshot without an external manifest anchor is applied for
        convergence but reported as incomplete until its checkpoint is established.
        """
        if self.store.conn.transaction_owned_by_current_thread():
            raise RuntimeError("sync cannot run inside an active store transaction")
        bundle = self.export_bundle(
            workspace_id, repo_id=repo_id, _save_checkpoint=False
        )
        ws_name = bundle["workspace_name"]
        local_device = _normalise_device_id(self.device_id)
        own_name = "bundle-%s.json" % local_device

        applied: list[dict] = []
        totals = {
            "added": 0, "updated": 0, "unchanged": 0, "rejected": 0,
            "conflicts_preserved": 0, "links_added": 0, "links_updated": 0,
            "tombstones_applied": 0,
        }
        received_bytes = 0
        peers_applied = 0
        try:
            bundles = iter(transport.pull())
        except Exception as exc:  # noqa: BLE001 — transport setup failure
            logger.warning("sync transport pull failed (%s)", type(exc).__name__)
            applied.append({
                "bundle": "?",
                "error": "transport failure",
                "error_type": type(exc).__name__,
            })
            bundles = iter(())

        while True:
            try:
                name, data = next(bundles)
            except StopIteration:
                break
            except Exception as exc:  # noqa: BLE001 — partial transport failure
                logger.warning("sync transport pull failed (%s)", type(exc).__name__)
                applied.append({
                    "bundle": "?",
                    "error": "transport failure",
                    "error_type": type(exc).__name__,
                })
                break
            try:
                remote = loads_strict(data)
                if not isinstance(remote, dict):
                    raise SyncError("bundle is not an object")
                remote_device = _normalise_device_id(remote.get("device_id"))
                freshness, bootstrap, duplicate = self._check_incoming_freshness(
                    remote, workspace_id, repo_id, remote_device
                )
                if duplicate and remote_device == local_device:
                    continue
                rep = self.apply_bundle(
                    remote, into_workspace=ws_name,
                    only_repo_id=repo_id, dry_run=dry_run,
                )
                rep["from_device"] = remote_device
                if freshness is None or bootstrap:
                    rep["error"] = "snapshot freshness unavailable"
                    rep["error_type"] = "SyncError"
            except (ValueError, UnicodeDecodeError) as exc:
                rep = {
                    "bundle": name,
                    "error": "unreadable",
                    "error_type": type(exc).__name__,
                }
            except Exception as exc:  # one hostile bundle must never abort the round
                logger.warning("sync bundle rejected (%s)", type(exc).__name__)
                rep = {
                    "bundle": name,
                    "error": "bundle rejected",
                    "error_type": type(exc).__name__,
                }
            else:
                received_bytes += len(data)
                peers_applied += 1
                if not dry_run:
                    # Attribute transport volume to this local device. Peer-controlled
                    # identities remain report/provenance data and cannot create an
                    # unbounded number of durable telemetry rows across repeated rounds.
                    try:
                        self.store.add_sync_bytes(
                            local_device, received=len(data), commit=False
                        )
                    except BaseException:
                        # sync() rejects a caller-owned transaction at entry, so any
                        # transaction here belongs to this telemetry write. The peer's
                        # applied bundle was committed independently and remains durable.
                        if self.store.conn.transaction_owned_by_current_thread():
                            self.store.conn.rollback()
                        raise
                for key in totals:
                    totals[key] += rep.get(key, 0)
            applied.append(rep)

        pushed = False
        pushed_bytes = 0
        try:
            # Settle receive telemetry before external I/O. Pull application is already
            # durable, and a failed push must never leave its local telemetry transaction
            # pinning the shared connection.
            if (
                not dry_run
                and self.store.conn.transaction_owned_by_current_thread()
            ):
                self.store.conn.commit()
            if not dry_run and push:
                # Re-export after pull: imported changes and checkpoints must be reflected
                # in the snapshot that replaces this device's durable transport copy.
                bundle = self.export_bundle(
                    workspace_id, repo_id=repo_id, _save_checkpoint=False
                )
                # Bind content-free erasure eligibility to the exact live rows selected
                # for this push. The marker batches and snapshot checkpoint share one
                # transaction: a failed transport write rolls them all back, while a
                # successful push cannot commit its checkpoint without the proof needed
                # to propagate a later secure erasure.
                exported_ids = [
                    item["id"] for item in bundle["memories"]
                    if isinstance(item, dict) and isinstance(item.get("id"), str)
                ]
                for start in range(0, len(exported_ids), APPLY_BATCH):
                    self.store.mark_memories_sync_exported(
                        exported_ids[start:start + APPLY_BATCH],
                        workspace_id=workspace_id,
                        commit=False,
                    )
                payload = json.dumps(bundle).encode("utf-8")
                transport.push(own_name, payload)
                pushed = True
                pushed_bytes = len(payload)
                self.store.add_sync_bytes(
                    local_device, sent=pushed_bytes, commit=False
                )
                self._save_snapshot_checkpoint(
                    workspace_id, repo_id, local_device,
                    bundle["generation"], bundle["state_hash"],
                )
        except BaseException:
            if self.store.conn.transaction_owned_by_current_thread():
                self.store.conn.rollback()
            raise

        errors = [item for item in applied if "error" in item]
        return {
            "pushed": own_name if pushed else None,
            "workspace": ws_name,
            "device_id": local_device,
            "exported_memories": len(bundle["memories"]),
            "read_only": bool(not push and not dry_run),
            "peers_applied": peers_applied,
            "bytes_sent": pushed_bytes,
            "bytes_received": received_bytes,
            "complete": not errors,
            "errors": errors,
            "totals": totals,
            "applied": applied,
            "dry_run": bool(dry_run),
        }
