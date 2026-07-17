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
    - ``pinned``: logical OR.
    - descriptive fields (title/content/keywords/…): last-writer-wins under a
      **deterministic total order** — ``(last_access, ingested_at, content-hash)`` —
      so the winner is a function of the data, never of arrival order.

The one honest limitation: without a per-field logical clock (HLC), a rare
*simultaneous in-place edit of the same field on two devices* resolves by that
deterministic order rather than by true causality — it converges (no divergence,
no lost row), it just may pick a well-defined winner a human wouldn't. Corrections
go through ``MemoryEngine.correct`` (a new bi-temporal row, not an edit), so this
only bites raw ``title``/``mtype`` relabels. A follow-up increment adds an HLC.

Untrusted input: a pulled bundle is attacker-controlled (SECURITY.md — memory
poisoning is an explicit threat). ``apply_bundle`` validates and clamps every row,
re-homes it into the caller's own workspace, and never executes bundle content.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any, Optional

from engraphis.core.graph_layers import merge_graph_layers, normalize_graph_layer
from engraphis.core.interfaces import MemoryRecord, MemoryType, Scope, SearchFilter
from engraphis.core.store import Store, now_ts

# ── bundle format ─────────────────────────────────────────────────────────────
SYNC_FORMAT = "engraphis-sync"
SYNC_VERSION = 1

# ── validation caps (untrusted bundle → clamp, don't trust) ───────────────────
MAX_MEMORIES = 200_000
MAX_LINKS = 500_000
MAX_CONTENT_CHARS = 200_000
MAX_TITLE_CHARS = 4_000
MAX_SUMMARY_CHARS = 20_000
MAX_KEYWORDS = 64
MAX_KEYWORD_CHARS = 200
MAX_JSON_CHARS = 40_000            # metadata / provenance serialized cap
MAX_STABILITY = 1e6                # clamp so a bundle can't dominate retention scoring
MAX_ACCESS_COUNT = 1_000_000_000
MAX_SESSION_ID_CHARS = 128
MAX_REPOS = 10_000                 # cap repos map so an empty-memories bundle can't bloat
MAX_WORKSPACE_NAME_CHARS = 200
MAX_REPO_NAME_CHARS = 200
TS_FUTURE_SKEW = 2 * 86400         # tolerate 2 days of cross-device clock skew, no more
_VALID_SENSITIVITY = ("normal", "sensitive", "secret")

# Strip C0/C1 control + ANSI-escape bytes (keep \t\n\r) — the same defense the rest of
# the ingest surface applies (service.py) against hidden-instruction / terminal-injection
# payloads. The sync write path bypasses service.py, so it must strip here itself.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Descriptive fields resolved by last-writer-wins (the version key). The lattice
# fields below (valid_to/expired_at/stability/access_count/last_access/pinned) are
# handled separately and are NOT part of this set.
_LWW_FIELDS = (
    "title", "content", "summary", "keywords", "metadata", "mtype", "scope",
    "importance", "surprise", "sensitivity", "valid_from", "ingested_at",
    "session_id", "provenance",
)


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
    """The descriptive payload, canonicalized for hashing/compare (order-stable)."""
    return [
        rec.title, rec.content, rec.summary, sorted(rec.keywords or []),
        _enum(rec.mtype), _enum(rec.scope), rec.importance, rec.surprise,
        rec.sensitivity, rec.valid_from, rec.session_id,
        json.dumps(rec.metadata or {}, sort_keys=True, default=str),
        json.dumps(rec.provenance or {}, sort_keys=True, default=str),
    ]


def _version_key(rec: MemoryRecord) -> tuple:
    """Total order for last-writer-wins. Content hash is the final tiebreak so the
    winner depends only on the data — making merge commutative even when two devices
    edited at the same clock instant."""
    return (rec.last_access or 0.0, rec.ingested_at or 0.0, _stable_hash(_label_tuple(rec)))


def merge_record(local: MemoryRecord, incoming: MemoryRecord) -> MemoryRecord:
    """Deterministically merge two versions of the SAME memory id.

    Commutative, associative, and idempotent: ``merge(a, b) == merge(b, a)`` and
    ``merge(merge(a, b), b) == merge(a, b)``. ``incoming`` must already be re-homed
    into local scope (``workspace_id``/``repo_id`` set to local ids) — those fields
    are taken from ``local`` here and never LWW-merged, so re-homing is never undone.
    """
    winner = local if _version_key(local) >= _version_key(incoming) else incoming
    return MemoryRecord(
        id=local.id,
        # scope pointers are always local — never merged from the remote
        workspace_id=local.workspace_id,
        repo_id=local.repo_id,
        # descriptive fields: whole-record last-writer-wins
        content=winner.content, title=winner.title, summary=winner.summary,
        keywords=list(winner.keywords or []), metadata=dict(winner.metadata or {}),
        mtype=winner.mtype, scope=winner.scope, importance=winner.importance,
        surprise=winner.surprise, sensitivity=winner.sensitivity,
        session_id=winner.session_id, provenance=dict(winner.provenance or {}),
        valid_from=winner.valid_from,
        # lattice fields: commutative joins (independent of the LWW winner)
        valid_to=_min_nonnull(local.valid_to, incoming.valid_to),
        expired_at=_min_nonnull(local.expired_at, incoming.expired_at),
        ingested_at=_min_nonnull(local.ingested_at, incoming.ingested_at),
        stability=max(local.stability, incoming.stability),
        access_count=max(local.access_count, incoming.access_count),
        last_access=_max_nonnull(local.last_access, incoming.last_access),
        pinned=bool(local.pinned or incoming.pinned),
    )


def _signature(rec: MemoryRecord) -> str:
    """Fingerprint of everything sync persists — to tell 'changed' from 'no-op'."""
    return _stable_hash(_label_tuple(rec) + [
        rec.valid_to, rec.expired_at, rec.ingested_at, rec.stability,
        rec.access_count, rec.last_access, bool(rec.pinned),
    ])


# ── serialization (embedding excluded — rebuilt locally, never trusted over the wire) ──

def record_to_dict(rec: MemoryRecord) -> dict:
    return {
        "id": rec.id, "workspace_id": rec.workspace_id, "repo_id": rec.repo_id,
        "session_id": rec.session_id, "scope": _enum(rec.scope), "mtype": _enum(rec.mtype),
        "title": rec.title, "content": rec.content, "summary": rec.summary,
        "keywords": list(rec.keywords or []), "metadata": rec.metadata or {},
        "importance": rec.importance, "surprise": rec.surprise, "stability": rec.stability,
        "access_count": rec.access_count, "last_access": rec.last_access,
        "valid_from": rec.valid_from, "valid_to": rec.valid_to,
        "ingested_at": rec.ingested_at, "expired_at": rec.expired_at,
        "pinned": bool(rec.pinned), "sensitivity": rec.sensitivity,
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
    """Coerce a timestamp and bound it to ``[0, now + skew]``. Timestamps feed the
    last-writer-wins version key, so an unclamped future value could permanently pin
    poisoned content above every honest future edit; the skew still tolerates real
    cross-device clock drift."""
    f = _as_float(v, None)
    if f is None:
        return None
    return max(0.0, min(f, now + TS_FUTURE_SKEW))


# World-time validity ceiling (year ~2100). ``valid_from``/``valid_to`` are WORLD time —
# a fact may legitimately be true until a future date — and, unlike the system timestamps,
# do NOT feed the LWW version key (see _version_key), so a future value can't pin content.
# Bound only to a sane far-future ceiling to reject absurd/overflow values.
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


def dict_to_record(d: dict) -> Optional[MemoryRecord]:
    """Validate + clamp one untrusted bundle row into a MemoryRecord, or ``None`` if
    it is unusable (no id / no content). Never raises — this is the trust boundary."""
    if not isinstance(d, dict):
        return None
    mid = d.get("id")
    content = d.get("content")
    if not isinstance(mid, str) or not mid or not isinstance(content, str) or not content:
        return None
    kws = d.get("keywords") or []
    if not isinstance(kws, list):
        kws = []
    kws = [_clamp_str(k, MAX_KEYWORD_CHARS) for k in kws[:MAX_KEYWORDS]]
    sens = d.get("sensitivity")
    if sens not in _VALID_SENSITIVITY:
        sens = "normal"
    now = now_ts()
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
        stability=_clamp_num(d.get("stability"), 0.0, MAX_STABILITY, 1.0),
        access_count=min(MAX_ACCESS_COUNT, max(0, _as_int(d.get("access_count"), 0))),
        last_access=_clamp_ts(d.get("last_access"), now),
        # World-time validity may be in the future; system timestamps may not (they feed
        # the version key / anti-poison defense).
        valid_from=_clamp_world_ts(d.get("valid_from")),
        valid_to=_clamp_world_ts(d.get("valid_to")),
        ingested_at=_clamp_ts(d.get("ingested_at"), now),
        expired_at=_clamp_ts(d.get("expired_at"), now),
        pinned=bool(d.get("pinned")), sensitivity=sens,
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
        self.index = vector_index
        self.device_id = device_id or store.device_id()
        
        # Default allowed_workspaces to global settings to prevent accidental bypass of ENGRAPHIS_WORKSPACES
        if allowed_workspaces is None:
            try:
                from engraphis.config import settings
                if settings.allowed_workspaces:
                    allowed_workspaces = settings.allowed_workspaces
            except (ImportError, AttributeError):
                pass
                
        # Same hard boundary MemoryService enforces (SECURITY.md §3): when set, a bundle
        # may only be applied into one of these workspaces, so the folder transport can
        # never be steered into writing a workspace the operator never authorized.
        self.allowed_workspaces = (frozenset(allowed_workspaces)
                                   if allowed_workspaces else None)

    # ── export ────────────────────────────────────────────────────────────────
    def export_bundle(self, workspace_id: str, *, repo_id: Optional[str] = None) -> dict:
        """Full-state snapshot of one workspace (all repos unless ``repo_id`` given).

        Includes invalidated memories on purpose: a closed ``valid_to`` is state that
        must propagate so a forget/correct on one device reaches the others."""
        ws_row = self.store.conn.execute(
            "SELECT name FROM workspaces WHERE id=?", (workspace_id,)).fetchone()
        ws_name = ws_row["name"] if ws_row else "default"
        if self.allowed_workspaces is not None and ws_name not in self.allowed_workspaces:
            raise SyncError("workspace %r is not authorized for sync" % ws_name)
        flt = SearchFilter(workspace_id=workspace_id, repo_id=repo_id)
        # 'secret'-flagged memories never leave the device — sync is the first feature to
        # transmit memory content off-box, so this is the first place the label bites.
        mems = [m for m in self.store.list_memories(flt, include_invalid=True)
                if m.sensitivity != "secret"]
        if repo_id is not None:
            repo_rows = self.store.conn.execute(
                "SELECT id, name FROM repos WHERE workspace_id=? AND id=?",
                (workspace_id, repo_id)).fetchall()
        else:
            repo_rows = self.store.conn.execute(
                "SELECT id, name FROM repos WHERE workspace_id=?", (workspace_id,)).fetchall()
        ids_in = [m.id for m in mems]
        links = self.store.links_among(ids_in) if ids_in else []
        return {
            "format": SYNC_FORMAT, "version": SYNC_VERSION,
            "device_id": self.device_id, "created_at": now_ts(),
            "workspace_name": ws_name,
            "repos": {r["id"]: r["name"] for r in repo_rows},
            "memories": [record_to_dict(m) for m in mems],
            "mem_links": [
                {
                    "a": ln["a"], "b": ln["b"], "relation": ln["relation"],
                    "layer": ln.get("layer") or "semantic",
                    "reason": ln.get("reason") or "",
                }
                for ln in links
            ],
        }

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
        if _as_int(bundle.get("version"), 0) != SYNC_VERSION:
            raise SyncError("unsupported bundle version %r" % bundle.get("version"))
        src_device = bundle.get("device_id")

        mem_dicts = bundle.get("memories") or []
        link_dicts = bundle.get("mem_links") or []
        if not isinstance(mem_dicts, list) or not isinstance(link_dicts, list):
            raise SyncError("bundle memories/mem_links must be lists")
        if len(mem_dicts) > MAX_MEMORIES or len(link_dicts) > MAX_LINKS:
            raise SyncError("bundle exceeds size caps")

        raw_ws_name = into_workspace if into_workspace is not None else bundle.get("workspace_name")
        if raw_ws_name is not None and not isinstance(raw_ws_name, str):
            raise SyncError("bundle workspace_name must be a string")
        ws_name = _clamp_str(raw_ws_name or "default", MAX_WORKSPACE_NAME_CHARS).strip()
        if not ws_name:
            ws_name = "default"
        if self.allowed_workspaces is not None and ws_name not in self.allowed_workspaces:
            raise SyncError("workspace %r is not authorized for sync" % ws_name)
        report = {"added": 0, "updated": 0, "unchanged": 0, "rejected": 0,
                  "links_added": 0, "links_updated": 0,
                  "workspace": ws_name, "dry_run": bool(dry_run)}

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
            local_ws = row["id"] if row else None
            for rid, rname in valid_remote_repos.items():
                repo_row = (self.store.conn.execute(
                    "SELECT id FROM repos WHERE workspace_id=? AND name=?",
                    (local_ws, rname)).fetchone() if local_ws is not None else None)
                repo_remap[rid] = repo_row["id"] if repo_row else None
        else:
            local_ws = self.store.get_or_create_workspace(ws_name)
            for rid, rname in valid_remote_repos.items():
                repo_remap[rid] = self.store.get_or_create_repo(local_ws, rname)

        accepted: dict[str, MemoryRecord] = {}

        for d in mem_dicts:
            rec = dict_to_record(d)
            if rec is None:
                report["rejected"] += 1
                continue
            # Re-home into local scope, and tag provenance with the origin device so a
            # synced-in memory stays auditable ("why is this known?" — AGENTS.md §3.6).
            rec.workspace_id = local_ws
            remote_repo_id = d.get("repo_id")
            if remote_repo_id:
                if remote_repo_id not in repo_remap:
                    report["rejected"] += 1
                    continue
                rec.repo_id = repo_remap[remote_repo_id]
                if rec.repo_id is None and only_repo_id is not None:
                    report["rejected"] += 1
                    continue
            else:
                rec.repo_id = None
            if only_repo_id is not None and rec.repo_id != only_repo_id:
                report["rejected"] += 1
                continue
            if src_device:
                prov = dict(rec.provenance or {})
                prov.setdefault("synced_from_device", _clamp_str(src_device, 128))
                rec.provenance = prov
            existing = self.store.get_memory(rec.id)
            if existing is not None and existing.workspace_id != local_ws:
                # This id already lives in a DIFFERENT workspace: never let a bundle reach
                # across the scope boundary (SECURITY.md §3 confinement).
                report["rejected"] += 1
                continue
            if existing is not None and existing.sensitivity == "secret":
                # ``secret`` is device-local by contract. A peer may know this id from an
                # older sync that happened before the memory was classified secret, but it
                # must never be able to overwrite, invalidate, or downgrade the local row
                # back to an exportable sensitivity.
                report["rejected"] += 1
                continue
            if (existing is not None and only_repo_id is not None
                    and existing.repo_id != only_repo_id):
                # The incoming row's claimed repo cannot re-home an existing memory from
                # another repo during a repo-restricted sync.
                report["rejected"] += 1
                continue
            if existing is None:
                if not dry_run:
                    self._write(rec)
                    self.store.audit(
                        "sync:%s" % _clamp_str(src_device or "peer", 128),
                        "sync_add", rec.id,
                        f"new memory created from synced bundle (device: {src_device or 'peer'})")
                report["added"] += 1
                accepted[rec.id] = rec
            else:
                accepted[rec.id] = existing
                merged = merge_record(existing, rec)
                if _signature(merged) == _signature(existing):
                    report["unchanged"] += 1
                else:
                    if not dry_run:
                        self._write(merged)
                        # A synced bundle overwriting existing content is exactly the
                        # memory-poisoning surface (SECURITY.md): record who/what so the
                        # overwrite is never silent and "why is this known?" stays answerable.
                        self.store.audit(
                            "sync:%s" % _clamp_str(src_device or "peer", 128),
                            "sync_overwrite", merged.id,
                            "content replaced by synced bundle (last-writer-wins)")
                    report["updated"] += 1
                    accepted[rec.id] = merged

        # mem_links: grow-only set; endpoints must be memories we actually hold.
        for ln in link_dicts:
            if not isinstance(ln, dict):
                continue
            a, b = ln.get("a"), ln.get("b")
            rel = _clamp_str(ln.get("relation") or "related", 64) or "related"
            layer = normalize_graph_layer(ln.get("layer"), rel).value
            reason = _clamp_str(ln.get("reason") or "", MAX_TITLE_CHARS)
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
            existing_link = self.store.conn.execute(
                "SELECT layer, reason FROM mem_links "
                "WHERE ((a=? AND b=?) OR (a=? AND b=?)) AND relation=? LIMIT 1",
                (a, b, b, a, rel),
            ).fetchone()
            if existing_link:
                # Link metadata has no clock in sync format v1. Resolve concurrent
                # metadata deterministically so peers converge regardless of arrival.
                merged_layer = merge_graph_layers(
                    existing_link["layer"], layer, rel
                ).value
                merged_reason = max(existing_link["reason"] or "", reason)
                if (merged_layer, merged_reason) == (
                    existing_link["layer"] or "semantic",
                    existing_link["reason"] or "",
                ):
                    continue
                if not dry_run:
                    self.store.add_link(
                        a, b, rel, layer=merged_layer, reason=merged_reason
                    )
                report["links_updated"] += 1
                continue
            if not dry_run:
                self.store.add_link(a, b, rel, layer=layer, reason=reason)
                self.store.audit(
                    "sync:%s" % _clamp_str(src_device or "peer", 128),
                    "sync_link", a,
                    f"linked to {b} with relation {rel}")
            report["links_added"] += 1

        return report

    def _write(self, rec: MemoryRecord) -> None:
        """Persist a merged/new record verbatim (ids + timestamps preserved) and keep
        derived state coherent: re-embed for the vector arm when an embedder is wired."""
        if self.embedder is not None:
            try:
                text = f"{rec.title}\n{rec.content}" if rec.title else rec.content
                rec.embedding = self.embedder.embed([text])[0]
            except Exception:
                rec.embedding = None
        self.store.add_memory(rec, audit=False)  # sync logs its own semantic audit (sync_add/sync_overwrite)
        if rec.embedding is not None and self.index is not None:
            try:
                self.index.upsert([rec.id], rec.embedding.reshape(1, -1))
            except Exception:
                pass

    # ── one round-trip over a transport ─────────────────────────────────────────
    def sync(self, transport, workspace_id: str, *, repo_id: Optional[str] = None,
             dry_run: bool = False) -> dict:
        """Push this device's snapshot, then pull and apply every *other* device's.

        Full-state and idempotent, so it is safe to run on any cadence (cron, a
        file-watcher, or by hand) and safe to interrupt. Returns a per-peer report."""
        bundle = self.export_bundle(workspace_id, repo_id=repo_id)
        ws_name = bundle["workspace_name"]

        own_name = "bundle-%s.json" % self.device_id
        pushed = False
        if not dry_run:
            transport.push(own_name, json.dumps(bundle).encode("utf-8"))
            pushed = True

        applied: list[dict] = []
        totals = {
            "added": 0, "updated": 0, "unchanged": 0, "rejected": 0,
            "links_added": 0, "links_updated": 0,
        }
        for name, data in transport.pull():
            if name == own_name:
                continue
            try:
                remote = loads_strict(data)
            except (ValueError, UnicodeDecodeError):
                applied.append({"bundle": name, "error": "unreadable"})
                continue
            if not isinstance(remote, dict) or remote.get("device_id") == self.device_id:
                continue  # our own writes (or a non-object blob) — never apply
            try:
                rep = self.apply_bundle(remote, into_workspace=ws_name,
                                        only_repo_id=repo_id, dry_run=dry_run)
            except Exception as exc:  # one hostile bundle must never abort the whole sync
                applied.append({"bundle": name, "error": str(exc)})
                continue
            rep["from_device"] = remote.get("device_id", "?")
            applied.append(rep)
            for k in totals:
                totals[k] += rep.get(k, 0)

        return {"pushed": own_name if pushed else None, "workspace": ws_name,
                "device_id": self.device_id, "exported_memories": len(bundle["memories"]),
                "peers_applied": len([a for a in applied if "error" not in a]),
                "totals": totals, "applied": applied, "dry_run": bool(dry_run)}
