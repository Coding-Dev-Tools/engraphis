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
import json
import hashlib
import contextvars
import math
import copy
import time
import threading
from collections import Counter, OrderedDict
from functools import wraps
from pathlib import Path
from typing import Any, Optional

from engraphis.backends.extractor import ChunkingExtractor
from engraphis.core.engine import MemoryEngine
from engraphis.core.graph_scene import (
    build_canonical_graph,
    build_graph_scene,
    is_broad_search_fragment,
    strongest_path,
)
from engraphis.core.graph_layers import normalize_graph_layer
from engraphis.core.ids import new_id as make_id
from engraphis.core.interfaces import Edge, GraphLayer, MemoryType, Node, Scope, SearchFilter
from engraphis.core.store import normalize_entity_name
from engraphis.graphdata import build_graph_payload, empty_graph

# ── validation limits (memory-poisoning / resource-exhaustion guards) ──────────
MAX_CONTENT_CHARS = 100_000
MAX_TITLE_CHARS = 1_000
MAX_NAME_CHARS = 200
MAX_KEYWORDS = 64
MAX_KEYWORD_CHARS = 128
MAX_METADATA_BYTES = 16_384
MAX_K = 50
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
MAX_GRAPH_ANALYSIS_ENTITIES = 20_000
MAX_GRAPH_ANALYSIS_EDGES = 100_000
MAX_GRAPH_ANALYSIS_SUPPORTS = 250_000
# Complete scenes are intentionally not representative samples.  These are hard
# refusal ceilings, not render caps: callers receive an explicit capacity error rather
# than a silently incomplete chart.
MAX_GRAPH_COMPLETE_MEMORIES = 50_000
MAX_GRAPH_COMPLETE_MEMORY_LINKS = 150_000
MAX_GRAPH_COMPLETE_CODE_MEMORY_LINKS = 150_000
MAX_GRAPH_COMPLETE_PAYLOAD_BYTES = 64 * 1024 * 1024
MAX_GRAPH_INDEX_MEMORIES = 20_000
MAX_GRAPH_INDEX_WORKERS = 2
GRAPH_INDEX_BATCH_SIZE = 100
GRAPH_INDEX_LEASE_SECONDS = 60.0
GRAPH_INDEX_JOB_HISTORY = 100
# Inspector payloads are deliberately smaller than analysis payloads. The endpoint
# reports complete counts, but bounds the returned detail so selecting a hub cannot
# produce a multi-megabyte response or lock the inspector's DOM.
GRAPH_ENTITY_RELATION_LIMIT = 200
GRAPH_ENTITY_EVIDENCE_LIMIT = 100
GRAPH_ENTITY_HISTORY_LIMIT = 50

# control characters except tab/newline/carriage-return
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_NAME_RE = re.compile(r"^[A-Za-z0-9._\-/ ]{1,%d}$" % MAX_NAME_CHARS)


class ValidationError(ValueError):
    """Raised when untrusted input fails a guard. Message is safe to surface."""


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
    """Roll back a failed multi-statement service mutation on the shared connection.

    The serialized SQLite wrapper pins its write lock until commit or rollback. Workspace
    lifecycle operations contain many dependent statements, so an unexpected storage or
    constraint failure must release both the partial transaction and that lock.
    """
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        started = not self.store.conn.in_transaction
        try:
            if started:
                self.store.conn.execute("BEGIN IMMEDIATE")
            result = method(self, *args, **kwargs)
            if started and self.store.conn.in_transaction:
                self.store.conn.commit()
            return result
        except BaseException:
            if self.store.conn.in_transaction:
                try:
                    self.store.conn.rollback()
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

    ``user`` is the auth-store session dict — only ``email`` and ``role`` are read here.
    Called exactly once per request by the team auth gate; contextvars are per-context so
    concurrent requests never see each other's user."""
    _CURRENT_USER.set(user)


def current_user() -> Optional[dict]:
    """The dashboard user bound to this request, or ``None`` outside team mode."""
    return _CURRENT_USER.get()


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


def _clean_name(value: Any, *, field: str) -> str:
    name = _clean_text(value, field=field, max_chars=MAX_NAME_CHARS)
    if not _NAME_RE.match(name):
        raise ValidationError(
            f"{field} may only contain letters, digits, space and . _ - / characters"
        )
    return name


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
    if any(k in value for k in _GRAPH_HINT_KEYS):
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
        hints = {k: value[k] for k in _GRAPH_HINT_KEYS if k in value}
        value = {k: v for k, v in value.items() if k not in _GRAPH_HINT_KEYS}
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
    folder = Path(raw_path).expanduser().resolve()
    if not folder.exists():
        raise ValidationError(f"path not found: {raw_path}")
    if not folder.is_dir():
        raise ValidationError(f"not a directory: {raw_path}")
    home = Path.home().resolve()
    allowed_roots = [home]
    env_roots = os.environ.get("ENGRAPHIS_IMPORT_ROOTS", "")
    if env_roots:
        allowed_roots.extend(Path(r).expanduser().resolve() for r in env_roots.split(os.pathsep) if r)
    if not any(folder == r or folder.is_relative_to(r) for r in allowed_roots):
        raise ValidationError(
            "import path must be under an allowed root (your home directory, or "
            "ENGRAPHIS_IMPORT_ROOTS)")
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
        parts = f.relative_to(folder).parts
        if any(p == "node_modules" or p == ".git" or p.startswith(".") for p in parts[:-1]):
            continue
        try:
            real = f.resolve()
        except OSError:
            continue
        if not (real == folder or real.is_relative_to(folder)):
            continue
        files.append(f)
    return files


def _title_from_content(content: str, fallback: str) -> str:
    """First Markdown H1 if present, else the caller-supplied fallback (usually the
    filename stem) — matches the retired v1 import-folder endpoint's title heuristic."""
    match = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
    return match.group(1).strip() if match else fallback


def _auto_migrate_v1_if_needed(db_path: str) -> None:
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
        os.replace(str(tmp_new), str(p))            # atomic swap only on full success
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
        # Replicate the binding on the Store itself so no caller (including a future
        # sync path) can bypass ENGRAPHIS_WORKSPACES by calling Store methods directly.
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
        self._graph_runner_id = make_id("device")

    def _graph_scene_revision(self) -> tuple[int, int, int]:
        row = self.store.conn.execute("PRAGMA data_version").fetchone()
        data_version = int(row[0]) if row is not None else 0
        return (int(self.store.conn.total_changes), data_version,
                int(self.store.schema_version))

    def _graph_scene_valid_until(self, workspace_id: str, at: float) -> float:
        """Earliest future world-time boundary that can change a current graph scene."""
        row = self.store.conn.execute(
            "SELECT MIN(boundary) FROM ("
            "SELECT valid_from AS boundary FROM edges "
            "WHERE workspace_id=? AND valid_from>? AND expired_at IS NULL "
            "UNION ALL SELECT valid_to FROM edges "
            "WHERE workspace_id=? AND valid_to>? AND expired_at IS NULL "
            "UNION ALL SELECT s.valid_from FROM edge_supports s "
            "JOIN edges e ON e.id=s.edge_id WHERE e.workspace_id=? "
            "AND s.valid_from>? AND s.expired_at IS NULL "
            "UNION ALL SELECT s.valid_to FROM edge_supports s "
            "JOIN edges e ON e.id=s.edge_id WHERE e.workspace_id=? "
            "AND s.valid_to>? AND s.expired_at IS NULL)",
            (workspace_id, at, workspace_id, at, workspace_id, at, workspace_id, at),
        ).fetchone()
        return float(row[0]) if row is not None and row[0] is not None else math.inf

    @classmethod
    def create(cls, db_path: str = ":memory:", *, embed_model: Optional[str] = None,
               embed_dim: int = 384, vector_backend: str = "auto",
               rerank_model: Optional[str] = None,
               allowed_workspaces: Optional[list] = None,
               extractor: Optional[str] = None,
               graph_extractor: Optional[str] = None,
               retention_supervisor: Optional[str] = None) -> "MemoryService":
        # extractor / graph_extractor default to the configured backends
        # (ENGRAPHIS_EXTRACTOR — "none" | "chunk" | "llm" | "llm_structured";
        # ENGRAPHIS_GRAPH_EXTRACTOR — "regex" by default) so the dashboard,
        # auto-maintenance, MCP server, and CLI all honor the same config knob. An
        # explicit value (e.g. extractor="none") still overrides the environment.
        if extractor is None or graph_extractor is None or retention_supervisor is None:
            from engraphis.config import settings
            if extractor is None:
                extractor = settings.extractor
            if graph_extractor is None:
                graph_extractor = settings.graph_extractor
            if retention_supervisor is None:
                retention_supervisor = settings.retention_supervisor
        # One-time, safe upgrade path for a self-host whose ENGRAPHIS_DB_PATH already
        # holds a v1-shaped database (see docstring) — must run before Store() ever
        # touches the file. No-ops instantly for a fresh install or an already-v2 db.
        if db_path != ":memory:":
            _auto_migrate_v1_if_needed(db_path)
        # Optional encryption at rest: if ENGRAPHIS_DB_KEY[_FILE] is set, memories are
        # stored in a SQLCipher-encrypted database. Off by default (returns None).
        from engraphis.backends.encrypted_db import connector_from_env
        connect = connector_from_env()
        engine = MemoryEngine.create(
            db_path, embed_model=embed_model, embed_dim=embed_dim,
            vector_backend=vector_backend, rerank_model=rerank_model,
            extractor=extractor, graph_extractor=graph_extractor,
            retention_supervisor=retention_supervisor, connect=connect,
        )
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

    def _authorize_workspace(self, ws: str) -> str:
        """Enforce the server-side workspace binding. When this instance is bound to a set
        of workspaces (``ENGRAPHIS_WORKSPACES``), no caller may read or write a workspace
        outside it — knowing or guessing the name is not enough. This is what makes
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
            raise ValidationError(f"workspace '{ws}' is not permitted on this instance")
        self._enforce_personal_access(ws)
        return ws

    def _workspace_visibility(self, ws: str) -> tuple[str, str]:
        """Return ``(visibility, owner)`` for an existing workspace, read from its
        ``settings`` JSON. Folders created before per-folder access controls have no
        visibility recorded and remain shared for compatibility; all new team folders are
        written explicitly as personal unless their creator deliberately shares them.
        Never raises: a missing row or malformed settings is treated as shared, so a bad
        settings payload cannot turn into an accidental denial of service."""
        try:
            row = self.store.conn.execute(
                "SELECT settings FROM workspaces WHERE name=?", (ws,)).fetchone()
        except Exception:  # noqa: BLE001 — treat any lookup failure as unrestricted-shared
            return ("shared", "")
        if row is None or not row["settings"]:
            return ("shared", "")
        try:
            s = json.loads(row["settings"])
        except Exception:  # noqa: BLE001
            return ("shared", "")
        if not isinstance(s, dict):
            return ("shared", "")
        vis = s.get("visibility") or "shared"
        return (vis if vis == "personal" else "shared", s.get("owner") or "")

    def _get_or_create_workspace(self, ws: str) -> str:
        """Get ``ws`` or create it private to the authenticated team user.

        Writes can create a workspace without first using the dashboard's explicit
        Create-folder dialog. That path must obey the same safe default, otherwise an
        agent or import could silently create a team-visible folder. Non-team callers do
        not have an identity and retain the established single-tenant behaviour.
        """
        existing = self._lookup_workspace(ws)
        if existing is not None:
            return existing
        user = current_user() or {}
        owner = user.get("email") or ""
        workspace_settings = {"visibility": "personal", "owner": owner} if owner else None
        return self.store.create_workspace(ws, settings=workspace_settings)

    def _enforce_personal_access(self, ws: str) -> None:
        """Block access to another user's personal folder. No current user (single-tenant,
        standalone MCP, CLI, sync, tests) → no restriction. A shared folder, or a personal folder the
        current user owns → allowed. A personal folder owned by someone else → refused,
        with a message that neither confirms nor denies the folder's contents beyond the
        fact that it's private (the name is already known to the caller who supplied it)."""
        user = current_user()
        if not user:
            return
        vis, owner = self._workspace_visibility(ws)
        if vis == "personal" and owner and owner != (user.get("email") or ""):
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
        return session

    # ── write ──────────────────────────────────────────────────────────────────
    def remember(self, content: str, *, workspace: str, repo: Optional[str] = None,
                 session_id: Optional[str] = None, mtype: str = "semantic",
                 scope: Optional[str] = None, title: str = "", importance: float = 0.0,
                 keywords: Optional[list] = None, metadata: Optional[dict] = None,
                 source: str = "agent", trusted: bool = True,
                 kind: Optional[str] = None, resolve_conflicts: bool = True,
                 retention_class: Optional[str] = None,
                 retention_reason: str = "") -> dict:
        """Store one memory. Returns its id, resolved scope, and the resolution
        outcome (``op``: add/noop/invalidate — see ``MemoryEngine.remember_with_resolution``).
        """
        content = _clean_text(content, field="content", max_chars=MAX_CONTENT_CHARS)
        title = _clean_text(title, field="title", max_chars=MAX_TITLE_CHARS, required=False)
        ws = self._clean_ws(workspace)
        rp = _clean_name(repo, field="repo") if repo else None
        mt = _enum(mtype, MemoryType, "mtype")
        scope_was_omitted = scope is None
        sc = _write_scope(scope, repo=rp, session_id=session_id)
        kws = _clean_keywords(keywords)
        meta = _clean_metadata(metadata)
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
        except (TypeError, ValueError):
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
        provenance = {"source": _clean_text(source, field="source", max_chars=MAX_NAME_CHARS,
                                            required=False) or "agent",
                      "trusted": bool(trusted)}
        if kind:
            provenance["kind"] = _clean_name(kind, field="kind")
        result = self.engine.remember_with_resolution(
            content, workspace_id=wid, repo_id=rid, session_id=session_id,
            mtype=mt, scope=sc, title=title, importance=importance,
            keywords=kws, metadata={**meta, "provenance": provenance},
            resolve_conflicts=bool(resolve_conflicts),
        )
        out = {
            "id": result["id"], "workspace": ws, "repo": rp,
            "scope": sc.value, "mtype": mt.value, "stored": True, "op": result["op"],
        }
        if result["op"] in ("noop", "invalidate"):
            out["resolution"] = result.get("reason", "")
        if result["op"] == "invalidate":
            out["superseded"] = result["superseded"]
        out["receipt"] = self.store.record_receipt(
            "remember", workspace_id=wid, repo_id=rid or "", actor=provenance["source"],
            target_count=1, status=result["op"],
            metadata={"mtype": mt.value, "scope": sc.value, "resolution": result["op"],
                      "retention": (retention or {}).get("label", "")},
        )
        return out

    def ingest(self, content: str, *, workspace: str, repo: Optional[str] = None,
               session_id: Optional[str] = None, mtype: str = "semantic",
               scope: Optional[str] = None, metadata: Optional[dict] = None,
               source: str = "agent", trusted: bool = True,
               kind: Optional[str] = None, resolve_conflicts: bool = True) -> dict:
        """Store raw, undistilled text. With an extractor configured (ENGRAPHIS_EXTRACTOR)
        the text is first distilled into discrete typed facts; without one this behaves
        exactly like ``remember``. Every fact goes through the same validation,
        resolution, and evolution as any other write."""
        content = _clean_text(content, field="content", max_chars=MAX_CONTENT_CHARS)
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
        provenance = {"source": _clean_text(source, field="source", max_chars=MAX_NAME_CHARS,
                                            required=False) or "agent",
                      "trusted": bool(trusted)}
        if kind:
            provenance["kind"] = _clean_name(kind, field="kind")
        out = self.engine.ingest(
            content, workspace_id=wid, repo_id=rid, session_id=session_id, scope=sc,
            default_mtype=mt, metadata={**meta, "provenance": provenance},
            resolve_conflicts=bool(resolve_conflicts),
        )
        result = {"workspace": ws, "repo": rp, "count": out["count"],
                  "extracted": out["extracted"],
                  "facts": [{"id": r["id"], "op": r["op"],
                             **({"superseded": r["superseded"]}
                                if "superseded" in r else {})}
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
                        retention_reason: str = "") -> dict:
        out = self.remember(
            text, workspace=workspace, repo=repo, title=title, mtype=mtype,
            scope=scope, importance=importance, metadata=metadata,
            retention_class=retention_class, retention_reason=retention_reason,
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
                      reinforce: bool = True,
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
            as_of=as_of, intent=intent_clean, graph_layers=layers,
            reinforce=reinforce, record_receipt=record_receipt,
        )
        response = {"operation": "recall", "intent": intent_clean, **out}
        if normalized in {"locate_code", "code"} and workspace and repo:
            response["code"] = self.search_code(
                query, workspace=workspace, repo=repo, limit=k
            )
        elif normalized in {"explain", "why"} and workspace:
            response["explanation"] = self.why(
                query, workspace=workspace, repo=repo, k=min(k, 10)
            )
        elif normalized in {"summarize_history", "history", "timeline"} and workspace:
            response["history"] = self.timeline(
                query, workspace=workspace, repo=repo, limit=min(max(k * 2, 10), 50)
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
            return {"file": name, "error": str(exc)}

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
                errors += 1
                details.append({"file": f.name, "error": str(exc)})
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
                    file_warnings.append(f"fact derivation failed: {exc}")
            if file_warnings:
                warnings.append({"file": rel, "warnings": file_warnings})

        self.store.audit(actor, "import_folder", wid,
                         f"{raw_path} ({imported} imported)")
        self.store.conn.commit()
        return {"workspace": ws, "path": str(folder), "scanned": len(files),
                "imported": imported, "skipped": skipped, "errors": errors,
                "derived_facts": derived_facts, "details": details[:50],
                "warnings": warnings[:50]}

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
                errors += 1
                details.append({"file": name, "error": str(exc)})
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
                    file_warnings.append(f"fact derivation failed: {exc}")
            if file_warnings:
                warnings.append({"file": name, "warnings": file_warnings})

        self.store.audit(actor, "import_files", wid, f"{imported} imported")
        self.store.conn.commit()
        return {"workspace": ws, "scanned": len(files), "imported": imported,
                "skipped": skipped, "errors": errors, "derived_facts": derived_facts,
                "details": details[:50], "warnings": warnings[:50]}

    def import_postgres_schema(self, dsn: str, *, workspace: str,
                               repo: Optional[str] = None,
                               schemas: Optional[list] = None,
                               actor: str = "user") -> dict:
        """Introspect a live PostgreSQL catalog into one schema memory plus graph nodes.

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
        from engraphis.backends.postgres_schema import get_postgres_introspector
        snapshot = get_postgres_introspector().inspect(dsn, schemas=selected)
        pieces = (
            [(fact.content, fact.title) for fact in ChunkingExtractor().extract(snapshot.text)]
            if len(snapshot.text) > MAX_CONTENT_CHARS
            else [(snapshot.text, snapshot.title)]
        )
        stored_rows = []
        for index, (piece_content, piece_title) in enumerate(pieces):
            stored_rows.append(self.remember(
                piece_content, workspace=ws, repo=rp,
                mtype="semantic", scope="repo" if rp else "workspace",
                title=(piece_title or snapshot.title),
                source="postgres_introspector", trusted=False,
                kind="postgres_schema",
                metadata={
                    "postgres_schema": snapshot.metadata,
                    "chunk": {"index": index, "of": len(pieces)},
                },
                resolve_conflicts=False,
            ))
        stored = stored_rows[0]
        wid, rid = self._require_scope(ws, rp)
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
            "workspace": ws, "repo": rp, "id": stored["id"],
            "memory_ids": [row["id"] for row in stored_rows],
            "entities": len(actual_ids), "relations": relations_written,
            "schema": snapshot.metadata, "receipt": receipt,
        }

    def consolidate(self, *, workspace: str, repo: Optional[str] = None,
                    dry_run: bool = False, min_cluster: int = 3,
                    archive_below: float = 0.05, profiles: bool = False,
                    min_mentions: int = 3, infer: bool = False,
                    structured: bool = False, supersede_sources: bool = False) -> dict:
        """Sleep-time consolidation sweep (episodic→semantic distillation + decayed-
        transient archival). The report includes a ``compaction`` block with the tokens
        the sweep saved. With ``profiles=True`` a third pass rolls each entity's memories
        into one durable profile digest (report under ``profiles``). With ``infer=True`` a
        fourth pass proposes evidence-only links between memories in different subject
        clusters that share a bridging entity (report under ``inferences``); inferred
        memories are low-salience and untrusted. ``infer`` is off by default — a human
        opts in — and the pass follows this call's ``dry_run`` flag (a dry-run proposes,
        a real run applies). ``dry_run=True`` reports without changing anything.

        Licensing: manual consolidation (``infer=False``) is a free, in-product
        housekeeping action; the **inference pass is a paid ``automation`` capability**
        (the dream pass 4), so ``infer=True`` is gated here as defense in depth — every
        caller (the ``/api/consolidate`` route, ``run_maintenance``) funnels through
        this, so the Pro-only pass can't be reached without a server-approved license.

        ``structured=True`` asks a configured LLM to emit schema-validated consolidated
        facts with graph hints; any provider/schema failure falls back to the deterministic
        digest path. ``supersede_sources=True`` is intentionally opt-in: it bi-temporally
        closes the source episodes only after validated structured facts are written."""
        if supersede_sources and not structured:
            raise ValidationError("supersede_sources requires structured=true")
        if infer:
            from engraphis.licensing import require_feature
            require_feature("automation")
        wid, rid = self._require_scope(workspace, repo)
        try:
            min_cluster = max(2, min(20, int(min_cluster)))
            archive_below = float(archive_below)
            min_mentions = max(2, min(50, int(min_mentions)))
        except (TypeError, ValueError):
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
                infer=bool(infer), structured=bool(structured),
                supersede_sources=bool(supersede_sources), llm=llm)
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
               reinforce: bool = True, intent: str = "recall",
               graph_layers: Optional[list] = None,
               record_receipt: bool = True) -> dict:
        """Retrieve the most relevant memories for ``query`` within scope."""
        query = _clean_text(query, field="query", max_chars=MAX_CONTENT_CHARS)
        try:
            k = int(k)
        except (TypeError, ValueError):
            raise ValidationError("k must be an integer")
        k = max(1, min(MAX_K, k))
        mts = [_enum(m, MemoryType, "mtype") for m in mtypes] if mtypes else None
        layers = (
            [_enum(layer, GraphLayer, "graph_layer") for layer in graph_layers]
            if graph_layers else None
        )

        # A configured workspace binding or a bound dashboard user must never do a
        # workspace-less (global) recall — either case represents a tenant boundary.
        if not workspace and (self.allowed_workspaces is not None or current_user() is not None):
            raise ValidationError("workspace is required on this instance")
        wid = rid = None
        sid = None
        if workspace:
            ws = self._clean_ws(workspace)
            wid = self._lookup_workspace(ws)
            if wid is None:
                return {"query": query, "count": 0, "context": "", "memories": [],
                        "note": f"no workspace named '{ws}' yet"}
            if repo:
                rp = _clean_name(repo, field="repo")
                rid = self._lookup_repo(wid, rp)
                if rid is None:
                    return {"query": query, "count": 0, "context": "", "memories": [],
                            "note": f"no repo named '{rp}' in workspace '{ws}' yet"}
            if session_id:
                sid = _clean_text(
                    session_id, field="session_id", max_chars=MAX_NAME_CHARS
                )
                session = self.store.get_session(sid)
                if session is None:
                    return {"query": query, "count": 0, "context": "", "memories": [],
                            "note": f"no session with id '{sid}'"}
                if session["workspace_id"] != wid or (
                        rid is not None and session.get("repo_id") != rid):
                    raise ValidationError("session_id does not belong to that workspace/repo")
                rid = rid or session.get("repo_id")
        elif session_id:
            raise ValidationError("session_id requires workspace")

        result = self.engine.recall_engine.recall(
            query,
            _filter(wid, rid, mts, as_of, layers, session_id=sid),
            k=k, reinforce=reinforce,
        )
        memories = []
        for chunk in result.chunks:
            item = dict(chunk)
            arm = item.get("arm") or "hybrid"
            item["why_recalled"] = (
                f"Matched by {arm} retrieval; fused score "
                f"{float(item.get('score') or 0.0):.3f}, retention "
                f"{float(item.get('retention') or 0.0):.3f}."
            )
            memories.append(item)
        out = {
            "query": query, "count": result.count,
            "context": result.context, "memories": memories,
        }
        if record_receipt:
            out["receipt"] = self.store.record_receipt(
                "recall", workspace_id=wid or "", repo_id=rid or "", actor="agent",
                target_count=result.count, status="ok",
                metadata={"intent": str(intent or "recall")[:80], "k": k,
                          "result_count": result.count,
                          "graph_layers": [layer.value for layer in layers] if layers else []},
            )
        return out

    def grounded_recall(self, query: str, *, workspace: Optional[str] = None,
                        repo: Optional[str] = None, session_id: Optional[str] = None,
                        mtypes: Optional[list] = None,
                        k: int = 8, as_of: Optional[float] = None,
                        min_support: Optional[float] = None,
                        max_citations: int = 5, llm=None) -> dict:
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
        except (TypeError, ValueError):
            raise ValidationError("k must be an integer")
        k = max(1, min(MAX_K, k))
        try:
            max_citations = int(max_citations)
        except (TypeError, ValueError):
            raise ValidationError("max_citations must be an integer")
        max_citations = max(1, min(MAX_K, max_citations))
        if min_support is not None:
            try:
                min_support = float(min_support)
            except (TypeError, ValueError):
                raise ValidationError("min_support must be a number")
            if not math.isfinite(min_support):
                raise ValidationError("min_support must be finite")
            min_support = max(0.0, min(1.0, min_support))
        mts = [_enum(m, MemoryType, "mtype") for m in mtypes] if mtypes else None

        if not workspace and (self.allowed_workspaces is not None or current_user() is not None):
            raise ValidationError("workspace is required on this instance")
        wid = rid = None
        sid = None
        if workspace:
            ws = self._clean_ws(workspace)
            wid = self._lookup_workspace(ws)
            if wid is None:
                return {"query": query, "grounded": False, "abstained": True,
                        "answer": "", "support": 0.0, "citations": [],
                        "reason": f"no workspace named '{ws}' yet"}
            if repo:
                rp = _clean_name(repo, field="repo")
                rid = self._lookup_repo(wid, rp)
                if rid is None:
                    return {"query": query, "grounded": False, "abstained": True,
                            "answer": "", "support": 0.0, "citations": [],
                            "reason": f"no repo named '{rp}' in workspace '{ws}' yet"}
            if session_id:
                sid = _clean_text(
                    session_id, field="session_id", max_chars=MAX_NAME_CHARS
                )
                session = self.store.get_session(sid)
                if session is None:
                    return {"query": query, "grounded": False, "abstained": True,
                            "answer": "", "support": 0.0, "citations": [],
                            "reason": f"no session with id '{sid}'"}
                if session["workspace_id"] != wid or (
                        rid is not None and session.get("repo_id") != rid):
                    raise ValidationError("session_id does not belong to that workspace/repo")
                rid = rid or session.get("repo_id")
        elif session_id:
            raise ValidationError("session_id requires workspace")

        ans = self.engine.grounded_recall(
            query, workspace_id=wid, repo_id=rid, session_id=sid, mtypes=mts,
            as_of=as_of, k=k, llm=llm, min_support=min_support,
            max_citations=max_citations,
        )
        out = {"query": query, **ans.to_dict()}
        out["receipt"] = self.store.record_receipt(
            "recall", workspace_id=wid or "", repo_id=rid or "", actor="agent",
            target_count=len(out.get("citations") or []),
            status="grounded" if out.get("grounded") else "abstained",
            metadata={"intent": "grounded", "grounded": bool(out.get("grounded")),
                      "citations": len(out.get("citations") or [])},
        )
        return out

    # ── session lifecycle ───────────────────────────────────────────────────────
    def start_session(self, workspace: str, *, repo: Optional[str] = None,
                      agent: str = "", goal: str = "", force_new: bool = False) -> dict:
        """Open a session. If this repo has a prior *ended* session, its summary and
        unresolved ``open_threads`` come back as ``bootstrap`` — the concrete fix for
        "the agent forgets everything between sessions".

        Idempotent by default: if a session for the same ``(workspace, repo, agent)`` is
        already ``active``, that one is returned (``reused: true``) instead of opening a
        second concurrent session. Two live sessions in one scope means two writers on
        the single-writer SQLite store — the "opens up 2 instances that trample on each
        other" failure. Pass ``force_new=True`` to deliberately branch a fresh session
        (e.g. a genuinely separate task in the same repo)."""
        ws = self._clean_ws(workspace)
        rp = _clean_name(repo, field="repo") if repo else None
        agent = _clean_text(agent, field="agent", max_chars=MAX_NAME_CHARS, required=False)
        goal = _clean_text(goal, field="goal", max_chars=MAX_TITLE_CHARS, required=False)
        wid = self._get_or_create_workspace(ws)
        rid = self.store.get_or_create_repo(wid, rp) if rp else None
        if not force_new:
            existing = self.store.get_active_session(wid, rid, agent=agent)
            if existing:
                return {"session_id": existing["id"], "workspace": ws, "repo": rp,
                        "goal": existing.get("goal") or goal, "status": "active",
                        "reused": True, "bootstrap": {}}
        sid = self.store.start_session(wid, rid, agent=agent, goal=goal)
        bootstrap: dict = {}
        if rid:
            last = self.store.get_last_session(wid, rid, exclude=sid)
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
        if self.store.get_session(sid) is None:
            raise ValidationError(f"no session with id '{sid}'")
        self.store.end_session(sid, summary=summary, outcome=outcome, open_threads=threads)
        return {"session_id": sid, "status": "summarized", "summary": summary,
               "open_threads": threads}

    # ── governance: forget / pin / correct / promote (audited; history preserved) ──
    def forget(self, memory_id: str, *, workspace: str, repo: Optional[str] = None,
              reason: str = "", actor: str = "user") -> dict:
        mid = _clean_text(memory_id, field="memory_id", max_chars=MAX_NAME_CHARS)
        reason = _clean_text(reason, field="reason", max_chars=MAX_TITLE_CHARS, required=False)
        actor = _clean_text(actor, field="actor", max_chars=MAX_NAME_CHARS,
                            required=False) or "user"
        wid, rid = self._require_scope(workspace, repo)
        self._check_owns(mid, wid, rid)
        try:
            return self.engine.forget(mid, reason=reason, actor=actor)
        except KeyError as exc:
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
              mtype: Optional[str] = None, reason: str = "", actor: str = "user") -> dict:
        """Merge several memories into one (manual N→1), retiring the sources into
        history. Validated and authorized like every other governance op: the caller
        must name the workspace that owns the sources, and **every** source is
        ownership-checked, so a merge can neither read nor retire a memory outside the
        caller's workspace. Ownership is checked at workspace level (not repo), so
        near-duplicates spread across repos of the same workspace can still be merged;
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
        wid, _ = self._require_scope(workspace, repo)
        for sid in uniq:
            self._check_owns(sid, wid, None)
        try:
            out = self.engine.merge(uniq, merged_content, title=title_clean, mtype=mt,
                                    reason=reason, actor=actor)
        except (KeyError, ValueError) as exc:
            raise ValidationError(str(exc))
        out["workspace"] = self._clean_ws(workspace)
        return out

    # ── bi-temporal: why / timeline ──────────────────────────────────────────────
    def why(self, query: str, *, workspace: str, repo: Optional[str] = None, k: int = 5) -> dict:
        """Rationale + history for a decision/fact: the live answer plus whatever it
        superseded, if anything — the bi-temporal "why" a flat store can't answer."""
        query = _clean_text(query, field="query", max_chars=MAX_CONTENT_CHARS)
        wid, rid = self._require_scope(workspace, repo)
        k = max(1, min(MAX_K, int(k)))
        out = self.engine.why(query, workspace_id=wid, repo_id=rid, k=k)
        return {"query": query, "answer": [_mem_to_dict(r) for r in out["answer"]],
               "supersedes": [_mem_to_dict(r) for r in out["supersedes"]]}

    def timeline(self, query: str, *, workspace: str, repo: Optional[str] = None,
                limit: int = 20) -> dict:
        """Chronological, bi-temporal history of a fact: what we believed and when."""
        query = _clean_text(query, field="query", max_chars=MAX_CONTENT_CHARS)
        wid, rid = self._require_scope(workspace, repo)
        limit = max(1, min(MAX_K, int(limit)))
        recs = self.engine.timeline(query, workspace_id=wid, repo_id=rid, limit=limit)
        return {"query": query, "history": [_mem_to_dict(r) for r in recs]}

    def recall_proactive(self, *, workspace: str, repo: Optional[str] = None,
                         k: int = 10) -> dict:
        """"What should I know right now" with no query — importance + recency +
        retention, plus the repo's last-session handoff if there is one."""
        wid, rid = self._require_scope(workspace, repo)
        k = max(1, min(MAX_K, int(k)))
        out = self.engine.recall_proactive(workspace_id=wid, repo_id=rid, k=k)
        return {"memories": [_mem_to_dict(r) for r in out["memories"]],
               "last_session": out["last_session"]}

    def proactive_context(self, *, workspace: str, repo: Optional[str] = None,
                          task: str = "", agent_state: str = "", k: int = 10,
                          synthesize: bool = False) -> dict:
        """Agent-ready proactive context packet.

        Combines queryless proactive recall, optional task-specific recall, and the
        last-session handoff into a cited context summary. Deterministic by default;
        when ``synthesize`` is true and an LLM is configured, the model may rewrite the
        summary, but only if it cites retrieved memories with ``[n]`` markers.
        """
        task = _clean_text(task, field="task", max_chars=MAX_CONTEXT_TASK_CHARS,
                           required=False)
        agent_state = _clean_text(agent_state, field="agent_state",
                                  max_chars=MAX_AGENT_STATE_CHARS, required=False)
        k = max(1, min(MAX_K, int(k)))
        proactive = self.recall_proactive(workspace=workspace, repo=repo, k=k)
        memories = list(proactive.get("memories") or [])
        query = "\n".join(x for x in (task, agent_state) if x).strip()
        if query:
            try:
                recalled = self.recall(query, workspace=workspace, repo=repo, k=k,
                                       reinforce=False)
                memories.extend(recalled.get("memories") or [])
            except Exception:
                pass
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
        return {"workspace": self._clean_ws(workspace), "repo": repo, **out}

    # ── linking & events (A-MEM-style) ───────────────────────────────────────────
    def record_event(self, kind: str, content: str, *, workspace: str,
                     repo: Optional[str] = None, session_id: Optional[str] = None,
                     refs: Optional[list] = None) -> dict:
        kind = _clean_name(kind, field="kind")
        content = _clean_text(content, field="content", max_chars=MAX_CONTENT_CHARS)
        wid, rid = self._require_scope(workspace, repo)
        eid = self.engine.record_event(kind, content, workspace_id=wid, repo_id=rid or "",
                                       session_id=session_id or "", refs=refs)
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

    def search_code(self, query: str, *, workspace: str, repo: str, limit: int = 20) -> dict:
        if not repo:
            raise ValidationError("repo is required to search code")
        query = _clean_text(query, field="query", max_chars=MAX_CONTENT_CHARS)
        wid, rid = self._require_scope(workspace, repo)
        limit = max(1, min(MAX_K, int(limit)))
        return self.engine.search_code(
            query, repo_id=rid, limit=limit,
            flt=SearchFilter(
                workspace_id=wid, repo_id=rid, include_ancestors=True
            ),
        )

    def code_path(self, source: str, target: str, *, workspace: str, repo: str,
                  max_depth: int = 8) -> dict:
        if not repo:
            raise ValidationError("repo is required for a code path query")
        source = _clean_text(source, field="source", max_chars=500)
        target = _clean_text(target, field="target", max_chars=500)
        wid, rid = self._require_scope(workspace, repo)
        try:
            max_depth = max(1, min(32, int(max_depth)))
        except (TypeError, ValueError):
            raise ValidationError("max_depth must be an integer")
        return self.engine.code_path(
            source, target, repo_id=rid, max_depth=max_depth,
            flt=SearchFilter(
                workspace_id=wid, repo_id=rid, include_ancestors=True
            ),
        )

    def code_impact(self, changed_files: list, *, workspace: str, repo: str) -> dict:
        if not repo:
            raise ValidationError("repo is required for impact analysis")
        files = _clean_string_list(
            changed_files, field="changed_files", max_items=2_000, max_chars=4_000
        )
        wid, rid = self._require_scope(workspace, repo)
        return self.engine.analyze_impact(
            files, repo_id=rid,
            flt=SearchFilter(
                workspace_id=wid, repo_id=rid, include_ancestors=True
            ),
        )

    def export_code_graph(self, *, workspace: str, repo: str) -> dict:
        if not repo:
            raise ValidationError("repo is required to export a code graph")
        wid, rid = self._require_scope(workspace, repo)
        flt = SearchFilter(
            workspace_id=wid, repo_id=rid, include_ancestors=True
        )
        graph = self.engine.export_code_graph(repo_id=rid, flt=flt)
        return {
            "graph": graph,
            "report_markdown": self.engine.code_graph_report(
                repo_id=rid, payload=graph, flt=flt
            ),
            "graph_html": self.engine.code_graph_html(
                repo_id=rid, payload=graph, flt=flt
            ),
        }

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
            "AND (m.valid_from IS NULL OR m.valid_from<=?) "
            "AND (m.valid_to IS NULL OR ?<m.valid_to) AND m.expired_at IS NULL "
            "GROUP BY w.id, w.name, w.settings ORDER BY w.name", (now, now)).fetchall()
        user = current_user()
        my_email = (user or {}).get("email") or ""
        out = []
        for r in rows:
            if self.allowed_workspaces is not None and r["name"] not in self.allowed_workspaces:
                continue
            try:
                _s = json.loads(r["settings"]) if r["settings"] else {}
                if not isinstance(_s, dict):
                    _s = {}
            except Exception:
                _s = {}
            _desc = _s.get("description") or ""
            _vis = "personal" if _s.get("visibility") == "personal" else "shared"
            _owner = _s.get("owner") or ""
            # Hide other users' personal folders from the listing (team mode only).
            if user and _vis == "personal" and _owner and _owner != my_email:
                continue
            repos = [dict(x) for x in self.store.conn.execute(
                "SELECT name FROM repos WHERE workspace_id=? ORDER BY name", (r["id"],))]
            entry = {"name": r["name"], "memories": int(r["n"]), "description": _desc,
                     "visibility": _vis, "repos": [x["name"] for x in repos]}
            if user:
                entry["can_change_access"] = bool(
                    _owner == my_email or user.get("role") == "admin"
                )
            if _vis == "personal":
                entry["owner"] = _owner
                entry["mine"] = bool(my_email and _owner == my_email)
            out.append(entry)
        return {"workspaces": out}

    # ── workspace curation (create / rename / describe / delete) ─────────────────
    def create_workspace(self, name: str, description: str = "", *,
                         visibility: str = "personal", confirmed: bool = False,
                         actor: str = "user") -> dict:
        """Create an empty workspace (a "folder") so a team can set one up *before* any
        memory is written to it — the dashboard's Workspaces tab and the agent write path
        both otherwise only mint a workspace lazily (``get_or_create_workspace``), which
        left no way to pre-create the folders users then choose to submit to. Enforces the
        same binding and name validation every other entry point does, so a bound instance
        (``ENGRAPHIS_WORKSPACES``) still refuses names outside its allow-list, and rejects a
        name that already exists (mirrors ``rename``'s uniqueness check).

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
        u = current_user() or {}
        owner = u.get("email") or ""
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

    def set_workspace_visibility(self, workspace: str, visibility: str, *,
                                 confirmed: bool = False, actor: str = "user") -> dict:
        """Explicitly share or unshare a team folder after user confirmation."""
        ws = self._clean_ws(workspace)
        target = str(visibility or "").lower()
        if target not in ("personal", "shared"):
            raise ValidationError("visibility must be 'personal' or 'shared'")
        if confirmed is not True:
            raise ValidationError("changing folder access requires explicit confirmation")
        user = current_user() or {}
        owner = user.get("email") or ""
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
                and previous_owner != owner and user.get("role") != "admin":
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

    def rename_workspace(self, workspace: str, new_name: str, *, actor: str = "user") -> dict:
        """Rename a workspace's label. Memories key off ``workspace_id``, so this is a pure
        relabel — all data stays attached. Same binding + uniqueness the create path enforces."""
        old = self._clean_ws(workspace)
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

    def set_workspace_description(self, workspace: str, description: str,
                                 *, actor: str = "user") -> dict:
        """Store a human description in the workspace's ``settings`` JSON (no schema change)."""
        ws = self._clean_ws(workspace)
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
        c.execute(f"DELETE FROM mem_fts WHERE id IN {msub}", (wid,))
        c.execute(f"DELETE FROM mem_vectors WHERE id IN {msub}", (wid,))
        try:
            c.execute(f"DELETE FROM mem_vec_ann WHERE id IN {msub}", (wid,))
        except Exception:
            pass  # sqlite-vec ANN table only present when that backend is active
        c.execute(f"DELETE FROM mem_links WHERE a IN {msub} OR b IN {msub}", (wid, wid))
        c.execute("DELETE FROM memories WHERE workspace_id=?", (wid,))
        c.execute("DELETE FROM entities WHERE workspace_id=?", (wid,))
        c.execute("DELETE FROM edges WHERE workspace_id=?", (wid,))
        c.execute("DELETE FROM sessions WHERE workspace_id=?", (wid,))
        c.execute("DELETE FROM events WHERE workspace_id=?", (wid,))
        c.execute(f"DELETE FROM code_files WHERE repo_id IN {rsub}", (wid,))
        c.execute(f"DELETE FROM code_edges WHERE repo_id IN {rsub}", (wid,))
        c.execute(f"DELETE FROM symbols WHERE repo_id IN {rsub}", (wid,))
        c.execute("DELETE FROM repos WHERE workspace_id=?", (wid,))
        c.execute("DELETE FROM jobs WHERE workspace_id=?", (wid,))
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
        the surviving row); everything else is simply relabeled onto ``target``.
        Irreversible, so the UI gates it behind a confirm, same as delete."""
        src = self._clean_ws(source)
        dst = self._clean_ws(target)
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

        # 3) Edges: relabel workspace/repo, remapping any entity ids folded in step 2.
        #    Before relabeling, check for a live collision on the partial unique index
        #    (workspace_id, [repo_id,] src, dst, relation, layer).  On collision the
        #    source edge's supports merge into the surviving target edge and the source
        #    duplicate is expired instead of violating the constraint.
        src_edges = [dict(x) for x in c.execute(
            "SELECT id, repo_id, src, dst, relation, layer FROM edges WHERE workspace_id=?",
            (wid_src,))]
        for ed in src_edges:
            new_repo = _new_repo(ed["repo_id"])
            new_src = entity_remap.get(ed["src"], ed["src"])
            new_dst = entity_remap.get(ed["dst"], ed["dst"])
            if new_repo is not None:
                collision = c.execute(
                    "SELECT id FROM edges WHERE workspace_id=? AND repo_id=? "
                    "AND src=? AND dst=? AND relation=? AND layer=? "
                    "AND valid_to IS NULL AND expired_at IS NULL AND id<>?",
                    (wid_dst, new_repo, new_src, new_dst,
                     ed["relation"], ed["layer"], ed["id"]),
                ).fetchone()
            else:
                collision = c.execute(
                    "SELECT id FROM edges WHERE workspace_id=? AND repo_id IS NULL "
                    "AND src=? AND dst=? AND relation=? AND layer=? "
                    "AND valid_to IS NULL AND expired_at IS NULL AND id<>?",
                    (wid_dst, new_src, new_dst,
                     ed["relation"], ed["layer"], ed["id"]),
                ).fetchone()
            if collision:
                # Merge live edge_supports from the source duplicate into the survivor.
                for sup in c.execute(
                    "SELECT memory_id, source_kind, confidence, valid_from, "
                    "ingested_at, provenance FROM edge_supports "
                    "WHERE edge_id=? AND valid_to IS NULL AND expired_at IS NULL",
                    (ed["id"],),
                ).fetchall():
                    if c.execute(
                        "SELECT 1 FROM edge_supports WHERE edge_id=? "
                        "AND memory_id=? AND source_kind=? "
                        "AND valid_to IS NULL AND expired_at IS NULL",
                        (collision["id"], sup["memory_id"], sup["source_kind"]),
                    ).fetchone() is None:
                        c.execute(
                            "INSERT INTO edge_supports "
                            "(edge_id, memory_id, source_kind, confidence, "
                            "valid_from, ingested_at, provenance) "
                            "VALUES (?,?,?,?,?,?,?)",
                            (collision["id"], sup["memory_id"], sup["source_kind"],
                             sup["confidence"], sup["valid_from"],
                             sup["ingested_at"], sup["provenance"]),
                        )
                closed_at = time.time()
                c.execute(
                    "UPDATE edges SET valid_to=? WHERE id=? AND valid_to IS NULL",
                    (closed_at, ed["id"]),
                )
                c.execute(
                    "UPDATE edge_supports SET valid_to=? WHERE edge_id=? "
                    "AND valid_to IS NULL AND expired_at IS NULL",
                    (closed_at, ed["id"]),
                )
            else:
                c.execute(
                    "UPDATE edges SET workspace_id=?, repo_id=?, src=?, dst=? WHERE id=?",
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

        # 5) The source workspace is now empty — drop it.
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
        entities, edges, memories (with vectors, full-text and cross-memory links) and
        sessions/events are all cloned under fresh ids, leaving ``source`` untouched.
        This is the copy counterpart to ``merge_workspaces`` — merge moves rows in place
        (ids survive), copy inserts parallel rows with new ids so the two workspaces are
        fully independent afterwards (editing the copy never touches the original).
        When ``new_name`` is omitted — the dashboard's one-click "Copy" button never
        prompts — the name is auto-generated off ``source`` (``_next_copy_name``) so the
        copy never collides with an existing workspace."""
        src = self._clean_ws(source)
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
                    "docstring, lang, exported, content_hash, embedding_ref, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (nsid, nrid, s["kind"], s["name"], s["fqname"], s["file"], s["span"],
                     s["signature"], s["docstring"], s["lang"], s["exported"],
                     s["content_hash"], s["embedding_ref"], s["updated_at"]))
            for ce in [dict(x) for x in c.execute(
                    "SELECT * FROM code_edges WHERE repo_id=?", (r["id"],))]:
                c.execute(
                    "INSERT INTO code_edges(id, repo_id, src, dst, relation, layer, file, line) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (ids.new_id("edge"), nrid, symbol_remap.get(ce["src"], ce["src"]),
                     symbol_remap.get(ce["dst"], ce["dst"]), ce["relation"],
                     ce["layer"] or "entity",
                     ce["file"], ce["line"]))

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
                "weight, valid_from, valid_to, ingested_at, expired_at, provenance) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (new_edge_id, wid_dst, _new_repo(ed["repo_id"]),
                 entity_remap.get(ed["src"], ed["src"]), entity_remap.get(ed["dst"], ed["dst"]),
                 ed["relation"], ed["layer"] or "semantic", ed["weight"],
                 ed["valid_from"], ed["valid_to"], ed["ingested_at"],
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
                    return memory_remap.get(item, item)
                return item

            return json.dumps(walk(value), ensure_ascii=False, separators=(",", ":"))

        for m in source_memories:
            nmid = memory_remap[m["id"]]
            c.execute(
                "INSERT INTO memories (id, workspace_id, repo_id, session_id, scope, mtype, "
                "title, content, summary, keywords, metadata, importance, surprise, stability, "
                "access_count, last_access, valid_from, valid_to, ingested_at, expired_at, "
                "pinned, sensitivity, provenance, sort_order) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (nmid, wid_dst, _new_repo(m["repo_id"]), session_remap.get(m["session_id"]),
                 m["scope"], m["mtype"], m["title"], m["content"], m["summary"], m["keywords"],
                 _remap_json_memory_ids(m["metadata"]), m["importance"],
                 m["surprise"], m["stability"],
                 m["access_count"], m["last_access"], m["valid_from"], m["valid_to"],
                 m["ingested_at"], m["expired_at"], m["pinned"], m["sensitivity"],
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
                pass  # sqlite-vec ANN table only present when that backend is active

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
                    "valid_from, valid_to, ingested_at, expired_at, provenance) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (new_edge_id, new_memory_id, support["source_kind"],
                     support["confidence"], support["valid_from"], support["valid_to"],
                     support["ingested_at"], support["expired_at"], support_provenance),
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
                        ingested_at=source_edge["ingested_at"],
                        expired_at=source_edge["expired_at"],
                    )

        if memory_remap:
            old_ids = list(memory_remap.keys())
            marks = ",".join("?" for _ in old_ids)
            for ln in [dict(x) for x in c.execute(
                    f"SELECT a, b, relation, layer, reason, created_at FROM mem_links "
                    f"WHERE a IN ({marks}) AND b IN ({marks})", old_ids + old_ids)]:
                c.execute(
                    "INSERT INTO mem_links(a, b, relation, layer, reason, created_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (
                        memory_remap[ln["a"]], memory_remap[ln["b"]],
                        ln["relation"], ln["layer"], ln["reason"], ln["created_at"],
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
                    "relation, confidence, created_at) VALUES (?,?,?,?,?,?,?)",
                    (
                        ids.new_id("edge"), repo_remap[link["repo_id"]],
                        new_symbol, new_memory, link["relation"],
                        link["confidence"], link["created_at"],
                    ),
                )

        # 8) Events, cloned with fresh ids.
        for ev in [dict(x) for x in c.execute(
                "SELECT * FROM events WHERE workspace_id=?", (wid_src,))]:
            c.execute(
                "INSERT INTO events(id, workspace_id, repo_id, session_id, kind, content, refs, "
                "interaction_level, ts) VALUES (?,?,?,?,?,?,?,?,?)",
                (ids.new_id("event"), wid_dst, _new_repo(ev["repo_id"]),
                 session_remap.get(ev["session_id"]), ev["kind"], ev["content"],
                 _remap_json_memory_ids(ev["refs"]),
                 ev["interaction_level"], ev["ts"]))

        self.store.audit(actor, "workspace_copy", wid_dst,
                         f"{src} -> {dst} ({len(memory_remap)} memories)")
        c.commit()
        return {"source": src, "workspace": dst, "id": wid_dst,
               "memories_copied": len(memory_remap)}

    def update_memory(self, memory_id: str, *, workspace: str, repo: Optional[str] = None,
                      title: Optional[str] = None, mtype: Optional[str] = None,
                      actor: str = "user") -> dict:
        """In-place edit of a memory's label fields (title, type). Content edits go through
        ``correct`` so bi-temporal history is preserved; title/type are mutable labels."""
        mid = _clean_text(memory_id, field="memory_id", max_chars=MAX_NAME_CHARS)
        actor = _clean_text(actor, field="actor", max_chars=MAX_NAME_CHARS, required=False) or "user"
        wid, rid = self._require_scope(workspace, repo)
        self._check_owns(mid, wid, rid)
        sets, params, changes = [], [], []
        if title is not None:
            title = _clean_text(title, field="title", max_chars=MAX_TITLE_CHARS, required=False)
            sets.append("title=?")
            params.append(title)
            changes.append("title")
        if mtype is not None:
            mt = _enum(mtype, MemoryType, "memory_type").value
            sets.append("mtype=?")
            params.append(mt)
            changes.append(f"type={mt}")
        if not sets:
            raise ValidationError("nothing to update")
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
            self.store._fts_upsert(mid, row["title"], row["content"], kw)
        self.store.audit(actor, "memory_update", mid, "; ".join(changes))
        self.store.conn.commit()
        return {"id": mid, "updated": changes}

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
                if prev is not None and prev.workspace_id == wid:
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
                return self.store.get_memory(r["id"])
        return None

    def audit_log(self, *, workspace: str, limit: int = 100) -> dict:
        """Recent audit entries for memories in this workspace (governance trail)."""
        wid, _ = self._require_scope(workspace, None)
        limit = max(1, min(500, int(limit)))
        rows = self.store.conn.execute(
            "SELECT a.ts, a.actor, a.action, a.target, a.detail FROM audit a "
            "JOIN memories m ON m.id = a.target WHERE m.workspace_id=? "
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
        return self.store.verify_receipts(
            workspace_id=wid,
            expected_head=expected_head,
            expected_count=expected_count,
        )

    def export_receipts(self, *, workspace: str) -> dict:
        """Export only public receipt payloads and chain hashes."""
        out = self.receipt_log(workspace=workspace, limit=10_000)
        out["verification"] = self.verify_receipts(workspace=workspace)
        return out

    def export_workspace(self, *, workspace: str) -> dict:
        """Full bi-temporal dump of one workspace — memories (live *and* superseded),
        sessions, and the audit trail. The compliance story in one artifact: nothing is
        ever silently deleted, and the export proves it. Scope-checked like any other
        read; the Pro license gate lives here so every caller (Inspector, v1 dashboard,
        v2 dashboard) passes through one check."""
        from engraphis.licensing import require_feature
        require_feature("export")

        wid, _ = self._require_scope(workspace, None)
        conn = self.store.conn
        memories = [dict(r) for r in conn.execute(
            "SELECT * FROM memories WHERE workspace_id=? ORDER BY rowid", (wid,))]
        sessions = [dict(r) for r in conn.execute(
            "SELECT * FROM sessions WHERE workspace_id=? ORDER BY rowid", (wid,))]
        audit = [dict(r) for r in conn.execute(
            "SELECT a.* FROM audit a JOIN memories m ON m.id = a.target "
            "WHERE m.workspace_id=? ORDER BY a.ts", (wid,))]
        receipts = self.store.list_receipts(workspace_id=wid, limit=10_000)
        import time as _time
        return {"format": "engraphis-export/1", "exported_at": _time.time(),
                "workspace": workspace, "counts": {"memories": len(memories),
                "sessions": len(sessions), "audit": len(audit),
                "receipts": len(receipts)},
                "memories": memories, "sessions": sessions, "audit": audit,
                "receipts": receipts}

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
        owns_transaction = not self.store.conn.in_transaction
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
            if owns_transaction:
                self.store.conn.commit()
        except BaseException:
            if owns_transaction and self.store.conn.in_transaction:
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
                "generation": self.store.schema_version,
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
        owns_transaction = not self.store.conn.in_transaction
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
            if owns_transaction:
                self.store.conn.commit()
            return result
        except BaseException:
            if owns_transaction and self.store.conn.in_transaction:
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
            self._recover_stale_graph_jobs()
            self._graph_job_threads = {
                key: value for key, value in self._graph_job_threads.items()
                if value.is_alive()
            }
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
                    "workspace_id=? AND expired_at IS NULL AND valid_to IS NULL"
                    + (" AND repo_id=?" if rid else "")
                )
                params: tuple[Any, ...] = (wid, rid) if rid else (wid,)
                snapshot = self.store.conn.execute(
                    f"SELECT COUNT(*) AS n, MAX(id) AS upper_id FROM memories "
                    f"WHERE {live_where}", params,
                ).fetchone()
                total = int(snapshot["n"] or 0)
                if total > MAX_GRAPH_INDEX_MEMORIES:
                    raise ValidationError(
                        "graph index job exceeds the memory candidate limit; filter by repository"
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
                    "upper_memory_id": snapshot["upper_id"] or "",
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
                self.store.conn.commit()
            except BaseException:
                if self.store.conn.in_transaction:
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
            row = self.store.conn.execute(
                "SELECT * FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            return self._graph_job_dict(row)

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
                    "SELECT id FROM memories WHERE workspace_id=? "
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
                memory_ids = [row["id"] for row in self.store.conn.execute(
                    id_sql, id_params
                ).fetchall()]
                if not memory_ids:
                    final_state = "completed"
                    break
                for memory_id in memory_ids:
                    last_memory_id = memory_id
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
                            "SELECT id, repo_id, title, content, metadata FROM memories "
                            "WHERE id=? AND workspace_id=? AND expired_at IS NULL "
                            "AND valid_to IS NULL"
                        )
                        memory_params: list[Any] = [memory_id, wid]
                        if rid:
                            memory_sql += " AND repo_id=?"
                            memory_params.append(rid)
                        memory = self.store.conn.execute(
                            memory_sql, memory_params
                        ).fetchone()
                        if memory is not None:
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
                        if transaction_started or self.store.conn.in_transaction:
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
                          entity_types: Optional[list[str]] = None,
                          memory_types: Optional[list[str]] = None,
                          time_from: Optional[float] = None,
                          time_to: Optional[float] = None,
                          include_weak_cooccurrence: bool = True,
                          include_code: bool = False,
                          include_complete_rows: bool = False) -> tuple:
        """Load one transactionally consistent graph snapshot and generation state."""
        clean_workspace = self._clean_ws(workspace)
        workspace_id = self._lookup_workspace(clean_workspace)
        if workspace_id:
            self._recover_stale_graph_jobs(workspace_id)
        owns_transaction = not self.store.conn.in_transaction
        if owns_transaction:
            self.store.conn.execute("BEGIN")
        try:
            rows = self._graph_scene_rows_unlocked(
                workspace=clean_workspace,
                repo=repo,
                as_of=as_of,
                entity_types=entity_types,
                memory_types=memory_types,
                time_from=time_from,
                time_to=time_to,
                include_weak_cooccurrence=include_weak_cooccurrence,
                include_code=include_code,
                include_complete_rows=include_complete_rows,
            )
            index_info = self._graph_index_info(rows[1]) if rows[1] else {
                "generation": self.store.schema_version,
                "state": "ready",
                "active_job_id": None,
                "updated_at": None,
                "last_error": "",
            }
            if owns_transaction:
                self.store.conn.commit()
            return (*rows, index_info)
        except BaseException:
            if owns_transaction and self.store.conn.in_transaction:
                self.store.conn.rollback()
            raise

    def _graph_scene_rows_unlocked(self, *, workspace: str, repo: Optional[str] = None,
                                   as_of: Optional[float] = None,
                                   entity_types: Optional[list[str]] = None,
                                   memory_types: Optional[list[str]] = None,
                                   time_from: Optional[float] = None,
                                   time_to: Optional[float] = None,
                                   include_weak_cooccurrence: bool = True,
                                   include_code: bool = False,
                                   include_complete_rows: bool = False) -> tuple:
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
        entity_sql = (
            "SELECT id, workspace_id, repo_id, name, etype, canonical_id, "
            "normalized_name, canonical_method, canonical_confidence, created_at "
            "FROM entities WHERE workspace_id=?"
        )
        entity_params: list[Any] = [wid]
        if repo_id:
            entity_sql += " AND (repo_id=? OR repo_id IS NULL)"
            entity_params.append(repo_id)
        if clean_entity_types:
            clean_types = sorted(set(clean_entity_types))
            if clean_types:
                marks = ",".join("?" for _ in clean_types)
                entity_sql += f" AND etype IN ({marks})"
                entity_params.extend(clean_types)
        entity_sql += " ORDER BY canonical_id, id LIMIT ?"
        entity_params.append(MAX_GRAPH_ANALYSIS_ENTITIES + 1)
        entity_rows = [dict(row) for row in self.store.conn.execute(
            entity_sql, entity_params
        ).fetchall()]
        if len(entity_rows) > MAX_GRAPH_ANALYSIS_ENTITIES:
            if include_complete_rows:
                raise GraphSceneCapacityExceeded(
                    resource="entity rows", count=len(entity_rows),
                    limit=MAX_GRAPH_ANALYSIS_ENTITIES,
                )
            raise ValidationError(
                "graph analysis exceeds the entity candidate limit; filter by repository"
            )

        try:
            t = float(as_of) if as_of is not None else __import__("time").time()
        except (TypeError, ValueError, OverflowError):
            raise ValidationError("as_of must be a finite timestamp")
        if not math.isfinite(t):
            raise ValidationError("as_of must be a finite timestamp")
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
        edge_sql = (
            "SELECT id, workspace_id, repo_id, src, dst, relation, layer, weight, "
            "valid_from, valid_to, ingested_at, expired_at, provenance FROM edges "
            "WHERE workspace_id=? AND (valid_from IS NULL OR valid_from<=?) "
            "AND (valid_to IS NULL OR ?<valid_to) AND expired_at IS NULL"
        )
        edge_params: list[Any] = [wid, t, t]
        if repo_id:
            edge_sql += " AND (repo_id=? OR repo_id IS NULL)"
            edge_params.append(repo_id)
        # Weak co-occurrence is evaluated after canonical endpoint/relation bundling in
        # ``build_canonical_graph``. Filtering each physical edge here would incorrectly
        # discard two independent one-support alias edges whose canonical bundle has two
        # supports and is therefore eligible for the default scene.
        evidence_filter = bool(
            clean_memory_types or lower_time is not None or upper_time is not None
        )
        if evidence_filter:
            edge_sql += (
                " AND EXISTS (SELECT 1 FROM edge_supports graph_support "
                "JOIN memories graph_memory ON graph_memory.id=graph_support.memory_id "
                "WHERE graph_support.edge_id=edges.id "
                "AND (graph_support.valid_from IS NULL OR graph_support.valid_from<=?) "
                "AND (graph_support.valid_to IS NULL OR ?<graph_support.valid_to) "
                "AND graph_support.expired_at IS NULL "
                "AND graph_memory.workspace_id=? "
                "AND (graph_memory.valid_from IS NULL OR graph_memory.valid_from<=?) "
                "AND (graph_memory.valid_to IS NULL OR ?<graph_memory.valid_to) "
                "AND graph_memory.expired_at IS NULL"
            )
            edge_params.extend((t, t, wid, t, t))
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
            edge_sql += ")"
        edge_sql += " ORDER BY id LIMIT ?"
        edge_params.append(MAX_GRAPH_ANALYSIS_EDGES + 1)
        edge_rows = [dict(row) for row in self.store.conn.execute(
            edge_sql, edge_params
        ).fetchall()]
        if len(edge_rows) > MAX_GRAPH_ANALYSIS_EDGES:
            if include_complete_rows:
                raise GraphSceneCapacityExceeded(
                    resource="raw relations", count=len(edge_rows),
                    limit=MAX_GRAPH_ANALYSIS_EDGES,
                )
            raise ValidationError(
                "graph analysis exceeds the relation candidate limit; filter by repository"
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
                symbol_rows = [dict(row) for row in self.store.conn.execute(
                    "SELECT id, kind, name, fqname, file FROM symbols "
                    "WHERE repo_id=? ORDER BY id LIMIT ?",
                    (repo_row["id"], remaining_entities + 1),
                ).fetchall()]
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
                    })
                    for key in (symbol.get("id"), symbol.get("fqname"), symbol.get("name")):
                        if key:
                            endpoint.setdefault(str(key), node_id)
                remaining_edges = MAX_GRAPH_ANALYSIS_EDGES - len(edge_rows)
                code_edges = self.store.conn.execute(
                    "SELECT id, src, dst, relation, layer FROM code_edges "
                    "WHERE repo_id=? ORDER BY id LIMIT ?",
                    (repo_row["id"], remaining_edges + 1),
                ).fetchall()
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
                            "valid_from": None, "valid_to": None, "ingested_at": None,
                            "expired_at": None,
                            "provenance": json.dumps({"source": "code_index"}),
                        })
        if evidence_filter:
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
        # Bounded IN chunks avoid a second scan of the relation table while preserving
        # the exact selected edge ids. Weak co-occurrence is filtered after canonical
        # relation bundling, once its aggregate support is known.
        support_rows = self.store.edge_supports_in_scope(
            edge_ids, at=t, limit=MAX_GRAPH_ANALYSIS_SUPPORTS + 1
        )
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
        support_memory_meta: dict[str, tuple[str, float]] = {}
        for start in range(0, len(support_memory_ids), 500):
            chunk = support_memory_ids[start:start + 500]
            marks = ",".join("?" for _ in chunk)
            memory_sql = (
                "SELECT id, mtype, COALESCE(valid_from, ingested_at, 0) AS support_time "
                "FROM memories WHERE workspace_id=? AND id IN (" + marks + ") "
                "AND (valid_from IS NULL OR valid_from<=?) "
                "AND (valid_to IS NULL OR ?<valid_to) AND expired_at IS NULL"
            )
            memory_params: list[Any] = [wid, *chunk, t, t]
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
                support_memory_meta[str(memory["id"])] = (
                    str(memory["mtype"] or ""), float(memory["support_time"] or 0.0)
                )
        enriched_supports = []
        for support in support_rows:
            memory_id = str(support.get("memory_id") or "")
            metadata = support_memory_meta.get(memory_id)
            if evidence_filter and metadata is None:
                continue
            enriched = dict(support)
            if metadata is not None:
                enriched["memory_type"] = metadata[0]
                enriched["support_time"] = metadata[1]
            enriched_supports.append(enriched)

        memory_rows: list[dict] = []
        memory_link_rows: list[dict] = []
        code_memory_link_rows: list[dict] = []
        if include_complete_rows:
            memory_where = [
                "workspace_id=?",
                "(valid_from IS NULL OR valid_from<=?)",
                "(valid_to IS NULL OR ?<valid_to)",
                "expired_at IS NULL",
            ]
            memory_params: list[Any] = [wid, t, t]
            if repo_id:
                memory_where.append("(repo_id=? OR repo_id IS NULL)")
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
                "importance, valid_from, ingested_at, pinned FROM memories WHERE "
                + " AND ".join(memory_where) + " ORDER BY id LIMIT ?",
                [*memory_params, MAX_GRAPH_COMPLETE_MEMORIES + 1],
            ).fetchall()]
            if len(memory_rows) > MAX_GRAPH_COMPLETE_MEMORIES:
                raise GraphSceneCapacityExceeded(
                    resource="memory nodes", count=len(memory_rows),
                    limit=MAX_GRAPH_COMPLETE_MEMORIES,
                )

            memory_link_rows = [dict(row) for row in self.store.conn.execute(
                "WITH selected_memory AS (" + scoped_memory_sql + ") "
                "SELECT links.a, links.b, links.relation, links.layer, links.reason, "
                "links.created_at FROM mem_links links "
                "JOIN selected_memory source ON source.id=links.a "
                "JOIN selected_memory target ON target.id=links.b "
                "ORDER BY links.a, links.b, links.relation, links.layer, links.created_at "
                "LIMIT ?",
                [*memory_params, MAX_GRAPH_COMPLETE_MEMORY_LINKS + 1],
            ).fetchall()]
            if len(memory_link_rows) > MAX_GRAPH_COMPLETE_MEMORY_LINKS:
                raise GraphSceneCapacityExceeded(
                    resource="memory connectors", count=len(memory_link_rows),
                    limit=MAX_GRAPH_COMPLETE_MEMORY_LINKS,
                )

            if include_code:
                code_sql = (
                    "WITH selected_memory AS (" + scoped_memory_sql + ") "
                    "SELECT links.id, links.repo_id, links.symbol_id, links.memory_id, "
                    "links.relation, links.confidence FROM code_memory_links links "
                    "JOIN selected_memory memory ON memory.id=links.memory_id "
                    "JOIN repos repo ON repo.id=links.repo_id WHERE repo.workspace_id=?"
                )
                code_params: list[Any] = [*memory_params, wid]
                if repo_id:
                    code_sql += " AND links.repo_id=?"
                    code_params.append(repo_id)
                code_sql += " ORDER BY links.id LIMIT ?"
                code_params.append(MAX_GRAPH_COMPLETE_CODE_MEMORY_LINKS + 1)
                code_memory_link_rows = [dict(row) for row in self.store.conn.execute(
                    code_sql, code_params,
                ).fetchall()]
                if len(code_memory_link_rows) > MAX_GRAPH_COMPLETE_CODE_MEMORY_LINKS:
                    raise GraphSceneCapacityExceeded(
                        resource="code-memory connectors",
                        count=len(code_memory_link_rows),
                        limit=MAX_GRAPH_COMPLETE_CODE_MEMORY_LINKS,
                    )
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
                     as_of: Optional[float] = None, depth: int = 1,
                     time_from: Optional[float] = None,
                     time_to: Optional[float] = None,
                    min_support: int = 1, min_confidence: float = 0.0,
                    include_weak_cooccurrence: bool = False,
                    include_code: bool = False,
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
            bounded_int(node_limit, "node_limit", 1, 300)
            if node_limit is not None else None
        )
        clean_edge_limit = (
            bounded_int(edge_limit, "edge_limit", 0, 900)
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
        try:
            clean_as_of = float(as_of) if as_of is not None else None
        except (TypeError, ValueError, OverflowError):
            raise ValidationError("as_of must be a finite timestamp")
        if clean_as_of is not None and not math.isfinite(clean_as_of):
            raise ValidationError("as_of must be a finite timestamp")
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
            tuple(clean_memory_types), clean_as_of, clean_time_from, clean_time_to,
            clean_depth, clean_min_support,
            clean_min_confidence, bool(include_weak_cooccurrence),
            bool(include_code), clean_node_limit, clean_edge_limit,
        )
        cached = self._graph_scene_cache.get(cache_key)
        if cached is not None and (clean_as_of is not None or time.time() < cached[0]):
            self._graph_scene_cache.move_to_end(cache_key)
            scene = copy.deepcopy(cached[1])
            scene["meta"]["cache_hit"] = True
            scene["meta"]["query_ms"] = round(
                (time.perf_counter() - started) * 1000.0, 3
            )
            return scene
        if cached is not None:
            del self._graph_scene_cache[cache_key]
        query_at = clean_as_of if clean_as_of is not None else time.time()
        (ws, _wid, entities, edges, supports, memories, memory_links,
         code_memory_links, index_info) = self._graph_scene_rows(
            workspace=clean_workspace, repo=clean_repo, as_of=query_at,
            entity_types=clean_entity_types, memory_types=clean_memory_types,
            time_from=clean_time_from, time_to=clean_time_to,
            include_weak_cooccurrence=include_weak_cooccurrence,
            include_code=include_code,
            include_complete_rows=clean_level == "complete",
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
            "time_from": clean_time_from,
            "time_to": clean_time_to,
            "min_support": clean_min_support,
            "min_confidence": clean_min_confidence,
            "include_weak_cooccurrence": bool(include_weak_cooccurrence),
            "include_code": bool(include_code),
        }
        filters = {key: value for key, value in filters.items()
                   if value not in (None, [], False)}
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
        valid_until = (
            math.inf if clean_as_of is not None or not _wid
            else self._graph_scene_valid_until(_wid, query_at)
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
        try:
            suggestion_at = float(as_of) if as_of is not None else time.time()
            lower_time = float(time_from) if time_from is not None else None
            upper_time = float(time_to) if time_to is not None else None
        except (TypeError, ValueError, OverflowError):
            raise ValidationError("graph suggestion times must be finite timestamps")
        if (not math.isfinite(suggestion_at)
                or (lower_time is not None and not math.isfinite(lower_time))
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

        # Search identity rows directly instead of rebuilding Louvain/PageRank for each
        # keystroke. A canonical entity id also resolves to its current deterministic
        # community in ``build_graph_scene``, so the same stable result can represent an
        # entity or a system without maintaining a second search index.
        entity_sql = (
            "SELECT id, canonical_id, name, normalized_name, etype, repo_id "
            "FROM entities WHERE workspace_id=? AND ("
            "normalized_name LIKE ? ESCAPE '\\' OR canonical_id=? OR id=?)"
        )
        entity_params: list[Any] = [wid, like, clean_query, clean_query]
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
                f"FROM entities WHERE workspace_id=? AND canonical_id IN ({marks})"
            )
            member_params: list[Any] = [wid, *selected_canonical_ids]
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
        if member_ids:
            seen_supports: dict[str, set[str]] = {}
            for start in range(0, len(member_ids), 400):
                chunk = member_ids[start:start + 400]
                marks = ",".join("?" for _ in chunk)
                support_sql = (
                    "SELECT endpoint, memory_id FROM ("
                    "SELECT relation.src AS endpoint, support.memory_id FROM edges relation "
                    "JOIN edge_supports support ON support.edge_id=relation.id "
                    f"WHERE relation.workspace_id=? AND relation.src IN ({marks}) "
                    "AND relation.valid_to IS NULL AND relation.expired_at IS NULL "
                    "AND support.valid_to IS NULL AND support.expired_at IS NULL "
                    "UNION ALL "
                    "SELECT relation.dst AS endpoint, support.memory_id FROM edges relation "
                    "JOIN edge_supports support ON support.edge_id=relation.id "
                    f"WHERE relation.workspace_id=? AND relation.dst IN ({marks}) "
                    "AND relation.valid_to IS NULL AND relation.expired_at IS NULL "
                    "AND support.valid_to IS NULL AND support.expired_at IS NULL)"
                )
                rows = self.store.conn.execute(
                    support_sql, (wid, *chunk, wid, *chunk)
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
            rows = members_by_canonical.get(canonical_id) or matched_by_canonical[canonical_id]
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
            "member_count": len(members_by_canonical.get(item["id"], [])) or 1,
            "mass": float(item["support_count"]),
        } for item in entity_results]

        memories = []
        repositories = []
        relations_out = []
        code_symbols = []
        if wid:
            memory_sql = (
                "SELECT id, title, content, mtype, repo_id FROM memories "
                "WHERE workspace_id=? AND (valid_from IS NULL OR valid_from<=?) "
                "AND (valid_to IS NULL OR ?<valid_to) AND expired_at IS NULL "
                "AND (lower(title) LIKE ? ESCAPE '\\' OR lower(content) LIKE ? ESCAPE '\\')"
            )
            memory_params: list[Any] = [wid, suggestion_at, suggestion_at, like, like]
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
                memory_sql += " AND (repo_id=? OR repo_id IS NULL)"
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
                "SELECT relation, COUNT(*) AS count FROM edges WHERE workspace_id=? "
                "AND relation LIKE ? ESCAPE '\\' AND (valid_from IS NULL OR valid_from<=?) "
                "AND (valid_to IS NULL OR ?<valid_to) AND expired_at IS NULL"
            )
            relation_params: list[Any] = [wid, like, suggestion_at, suggestion_at]
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
                     time_from: Optional[float] = None,
                     time_to: Optional[float] = None,
                     include_weak_cooccurrence: bool = True) -> dict:
        clean_canonical_id = _clean_text(
            canonical_id, field="canonical_id", max_chars=MAX_NAME_CHARS
        )
        (ws, wid, entities, edges, supports, _memories, _memory_links,
         _code_memory_links, _index_info) = self._graph_scene_rows(
            workspace=workspace, repo=repo, as_of=as_of,
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
                    "SELECT id, title, content, mtype, valid_from, valid_to, ingested_at, "
                    "expired_at, provenance FROM memories WHERE workspace_id=? "
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
            "workspace_id=? AND (valid_to IS NOT NULL OR expired_at IS NOT NULL) "
            "AND (src IN (SELECT id FROM entities WHERE workspace_id=? AND canonical_id=?) "
            "OR dst IN (SELECT id FROM entities WHERE workspace_id=? AND canonical_id=?))"
        )
        history_params = (wid, wid, resolved, wid, resolved)
        history_total = int(self.store.conn.execute(
            f"SELECT COUNT(*) AS n FROM edges WHERE {history_filter}", history_params
        ).fetchone()["n"])
        history = [dict(row) for row in self.store.conn.execute(
            "SELECT id, src, dst, relation, layer, weight, valid_from, valid_to, "
            "ingested_at, expired_at FROM edges WHERE " + history_filter + " "
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
        }

    def graph_path(self, source: str, target: str, *, workspace: str,
                   repo: Optional[str] = None, as_of: Optional[float] = None,
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
              repo: Optional[str] = None, backfill: bool = True) -> dict:
        """Entity-relation network for a workspace: nodes/edges plus type counts,
        top-connected entities, and connectivity stats — powers the Graph tab in
        both the v1-look dashboard and the Inspector UI (engraphis.graphdata
        shapes the rows so the two UIs can't drift). Same workspace-binding
        boundary as every other read: a bound instance refuses to read another
        tenant's graph even if the caller names it (SECURITY.md §3) — unlike the
        original dashboard-only implementation, which read the DB file directly
        and skipped this check entirely."""
        ws = self._clean_ws(workspace)  # binding enforced here, before any lookup
        wid = self._lookup_workspace(ws)
        if wid is None:
            return empty_graph(ws)
        self._assert_graph_index_ready(wid)
        limit = max(1, min(5000, int(limit)))
        conn = self.store.conn
        ents = conn.execute(
            "SELECT id, name, etype FROM entities WHERE workspace_id=? LIMIT ?",
            (wid, limit)).fetchall()
        # Lazy backfill: old memories can predate graph extraction or predate the
        # structured-metadata graph bridge. On first Graph-tab open in a process, feed
        # the missing graph state once; feed() de-dupes entities/edges.
        # Strictly read-only surfaces disable this write-on-first-read migration.
        if backfill and self._should_backfill_graph(wid, bool(ents)):
            self._lazy_backfill_graph(wid)
            ents = conn.execute(
                "SELECT id, name, etype FROM entities WHERE workspace_id=? LIMIT ?",
                (wid, limit)).fetchall()
        entity_rows = [dict(row) for row in ents]
        node_ids = {row["id"] for row in entity_rows}
        selected_graph_layers = None
        selected_layers = None
        if layers is not None:
            selected_graph_layers = [
                _enum(layer, GraphLayer, "layer") for layer in layers
            ]
            selected_layers = {layer.value for layer in selected_graph_layers}
        # Nodes are capped at ``limit``; edges need their own cap or a large workspace
        # graph / indexed repo lets the lowest-privilege caller pull an unbounded
        # payload (the SQL fetches are LIMIT-ed too, so server-side work stays
        # bounded as well — entity edges sync from peers, so they are as attacker-
        # growable as code edges).
        edge_cap = max(limit * 8, 2000)
        edgs = [
            {
                "src": edge.src, "dst": edge.dst, "relation": edge.relation,
                "layer": edge.layer.value if edge.layer else "semantic",
            }
            for edge in self.store.edges_in_scope(
                SearchFilter(
                    workspace_id=wid, graph_layers=selected_graph_layers
                ),
                limit=edge_cap,
            )
            if edge.src in node_ids and edge.dst in node_ids
            and (
                selected_layers is None
                or (edge.layer.value if edge.layer else "semantic") in selected_layers
            )
        ]
        repo_names: list[str] = []
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
                    workspace_id=wid, repo_id=rid, include_ancestors=True
                )
                symbols = self.store.list_symbols(rid, limit=limit)
                symbol_node: dict[str, str] = {}
                symbol_id_node: dict[str, str] = {}
                for symbol in symbols:
                    if len(entity_rows) >= limit:
                        break
                    node_id = f"code:{symbol['id']}"
                    label = symbol.get("fqname") or symbol.get("name") or node_id
                    entity_rows.append({
                        "id": node_id,
                        "name": f"{repo_name}:{label}",
                        "etype": f"code_{symbol.get('kind') or 'symbol'}",
                    })
                    symbol_id_node[symbol["id"]] = node_id
                    for key in (symbol.get("fqname"), symbol.get("name")):
                        if key:
                            symbol_node.setdefault(key, node_id)
                file_nodes: dict[str, str] = {}

                def code_endpoint(value: str, file_hint: str = "") -> Optional[str]:
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
                    if file_name not in file_nodes and len(entity_rows) < limit:
                        file_nodes[file_name] = f"file:{rid}:{file_name}"
                        entity_rows.append({
                            "id": file_nodes[file_name],
                            "name": f"{repo_name}:{file_name}",
                            "etype": "code_file",
                        })
                    return file_nodes.get(file_name)

                for edge in self.store.list_code_edges(
                    rid, limit=edge_cap, layers=selected_graph_layers
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
                linked_memory_ids = set()
                if selected_layers is None or "semantic" in selected_layers:
                    code_links = self.store.list_code_memory_links(
                        rid, limit=edge_cap, flt=code_filter
                    )
                    # Batched: up to `limit` (<=5000) individual get_memory() calls here
                    # was the dominant cost of an include_code=True request. Collect the
                    # candidate ids first and resolve them in one IN (...) query
                    # (Store.get_memories) — same liveness/limit checks below, just no
                    # per-row round trip.
                    candidate_ids = [link.get("memory_id") for link in code_links
                                     if link.get("memory_id")]
                    memories_by_id = self.store.get_memories(candidate_ids)
                    for link in code_links:
                        if len(edgs) >= edge_cap:
                            break
                        code_id = symbol_id_node.get(link.get("symbol_id"))
                        memory_id = link.get("memory_id")
                        if not code_id or not memory_id:
                            continue
                        if memory_id not in linked_memory_ids and len(entity_rows) < limit:
                            memory = memories_by_id.get(memory_id)
                            if memory and memory.expired_at is None and memory.valid_to is None:
                                entity_rows.append({
                                    "id": memory_id,
                                    "name": memory.title or memory.content[:80] or memory_id,
                                    "etype": f"memory_{memory.mtype.value}",
                                })
                                linked_memory_ids.add(memory_id)
                        if memory_id in linked_memory_ids:
                            edgs.append({
                                "src": code_id, "dst": memory_id,
                                "relation": link.get("relation") or "mentions",
                                "layer": "semantic",
                            })
                    for link in self.store.links_among(
                        list(linked_memory_ids),
                        layers=(
                            [GraphLayer(layer) for layer in selected_layers]
                            if selected_layers else None
                        ),
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
        payload["unified"] = bool(include_code)
        payload["repos"] = repo_names
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
            "SELECT metadata FROM memories WHERE workspace_id=? "
            "AND (valid_from IS NULL OR valid_from<=?) "
            "AND (valid_to IS NULL OR ?<valid_to) AND expired_at IS NULL "
            "AND (metadata LIKE '%entities%' OR metadata LIKE '%relations%')", (wid, now, now))
        for row in rows:
            try:
                meta = _json.loads(row["metadata"] or "{}")
            except ValueError:
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
            "SELECT id, repo_id, title, content, metadata FROM memories "
            "WHERE workspace_id=? AND (valid_from IS NULL OR valid_from<=?) "
            "AND (valid_to IS NULL OR ?<valid_to) AND expired_at IS NULL",
            (wid, now, now)).fetchall()
        for r in rows:
            try:
                meta = _json.loads(r["metadata"] or "{}")
            except ValueError:
                meta = {}
            if self.engine._has_structured_graph_metadata(meta):
                try:
                    _graph_feed(self.store, r["content"] or "", workspace_id=wid,
                                repo_id=r["repo_id"], title=r["title"] or "",
                                extractor=StructuredMetadataGraphExtractor(meta),
                                provenance={"source": "structured_backfill", "memory_id": r["id"]})
                except Exception:
                    pass
            if self.engine.graph_extractor is not None:
                try:
                    _graph_feed(self.store, r["content"] or "", workspace_id=wid,
                                repo_id=r["repo_id"], title=r["title"] or "",
                                extractor=self.engine.graph_extractor,
                                provenance={"source": "lazy_backfill", "memory_id": r["id"]})
                except Exception:
                    pass

    # ── introspection ───────────────────────────────────────────────────────────
    def stats(self, *, workspace: Optional[str] = None) -> dict:
        """Counts for quick health/onboarding checks (read-only)."""
        conn = self.store.conn
        params: list[Any] = []
        where = ""
        # A bound instance must not report global (cross-tenant) aggregate counts.
        if not workspace and self.allowed_workspaces is not None:
            raise ValidationError("workspace is required on this instance")
        if workspace:
            ws = self._clean_ws(workspace)
            wid = self._lookup_workspace(ws)
            if wid is None:
                return {"workspace": ws, "memories": 0, "note": "workspace not found"}
            where = " WHERE workspace_id=?"
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
        workspaces = conn.execute("SELECT COUNT(*) AS n FROM workspaces").fetchone()["n"]
        sessions = conn.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"]
        return {
            "workspace": workspace, "memories": int(total), "by_type": by_type,
            "total_rows": int(total_rows),   # live + superseded history (never deleted)
            "workspaces": int(workspaces), "sessions": int(sessions),
            "schema_version": self.store.schema_version,
        }


def _filter(workspace_id, repo_id, mtypes, as_of, graph_layers=None, *, session_id=None):
    from engraphis.core.interfaces import SearchFilter
    return SearchFilter(
        workspace_id=workspace_id, repo_id=repo_id, session_id=session_id,
        mtypes=mtypes, graph_layers=graph_layers, as_of=as_of,
        include_ancestors=True,
    )


def _mem_to_dict(rec: Any) -> dict:
    """Plain, JSON-able projection of a ``MemoryRecord`` for why/timeline/proactive
    responses — mirrors the fields ``RecallEngine`` already exposes in recall chunks."""
    return {
        "id": rec.id, "title": rec.title, "content": rec.content, "summary": rec.summary,
        "scope": rec.scope.value, "mtype": rec.mtype.value, "repo_id": rec.repo_id,
        "importance": rec.importance, "pinned": rec.pinned,
        "valid_from": rec.valid_from, "valid_to": rec.valid_to,
        "ingested_at": rec.ingested_at, "expired_at": rec.expired_at,
        "provenance": rec.provenance,
    }
