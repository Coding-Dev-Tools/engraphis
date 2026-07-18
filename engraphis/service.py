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
from pathlib import Path
from typing import Any, Optional

from engraphis.backends.extractor import ChunkingExtractor
from engraphis.core.engine import MemoryEngine
from engraphis.core.interfaces import Edge, GraphLayer, MemoryType, Node, Scope, SearchFilter
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

# control characters except tab/newline/carriage-return
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_NAME_RE = re.compile(r"^[A-Za-z0-9._\-/ ]{1,%d}$" % MAX_NAME_CHARS)


class ValidationError(ValueError):
    """Raised when untrusted input fails a guard. Message is safe to surface."""


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
            "SELECT id FROM repos WHERE workspace_id=? AND name=?", (workspace_id, name)
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

        # A bound instance must never do a workspace-less (global) recall — that would read
        # across every tenant's memories, the exact boundary the binding exists to enforce.
        if not workspace and self.allowed_workspaces is not None:
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

        if not workspace and self.allowed_workspaces is not None:
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
        graph_layer = _enum(layer, GraphLayer, "layer") if layer else None
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
               "layer": (graph_layer.value if graph_layer else None),
               "reason": reason, "linked": True}
        out["receipt"] = self.store.record_receipt(
            "link", workspace_id=wid, repo_id=rid or "", actor="agent",
            target_count=2, status="ok",
            metadata={"relation": relation,
                      "layer": graph_layer.value if graph_layer else "inferred"},
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
        wid = self.store.get_or_create_workspace(ws)
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
        return self.engine.search_code(query, repo_id=rid, limit=limit)

    def code_path(self, source: str, target: str, *, workspace: str, repo: str,
                  max_depth: int = 8) -> dict:
        if not repo:
            raise ValidationError("repo is required for a code path query")
        source = _clean_text(source, field="source", max_chars=500)
        target = _clean_text(target, field="target", max_chars=500)
        _, rid = self._require_scope(workspace, repo)
        try:
            max_depth = max(1, min(32, int(max_depth)))
        except (TypeError, ValueError):
            raise ValidationError("max_depth must be an integer")
        return self.engine.code_path(source, target, repo_id=rid, max_depth=max_depth)

    def code_impact(self, changed_files: list, *, workspace: str, repo: str) -> dict:
        if not repo:
            raise ValidationError("repo is required for impact analysis")
        files = _clean_string_list(
            changed_files, field="changed_files", max_items=2_000, max_chars=4_000
        )
        _, rid = self._require_scope(workspace, repo)
        return self.engine.analyze_impact(files, repo_id=rid)

    def export_code_graph(self, *, workspace: str, repo: str) -> dict:
        if not repo:
            raise ValidationError("repo is required to export a code graph")
        _, rid = self._require_scope(workspace, repo)
        graph = self.engine.export_code_graph(repo_id=rid)
        return {
            "graph": graph,
            "report_markdown": self.engine.code_graph_report(
                repo_id=rid, payload=graph
            ),
            "graph_html": self.engine.code_graph_html(repo_id=rid, payload=graph),
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

    def delete_workspace(self, workspace: str, *, actor: str = "user") -> dict:
        """HARD-delete a workspace and everything scoped to it (memories, vectors, FTS rows,
        entities/edges, sessions, events, repos + their code graph). Unlike ``forget`` this is
        irreversible, so the UI gates it behind an explicit confirm. Audit rows are retained."""
        ws = self._clean_ws(workspace)
        actor = _clean_text(actor, field="actor", max_chars=MAX_NAME_CHARS, required=False) or "user"
        wid = self._lookup_workspace(ws)
        if wid is None:
            raise ValidationError(f"no workspace named '{ws}' yet")
        c = self.store.conn
        n_mem = c.execute("SELECT COUNT(*) AS n FROM memories WHERE workspace_id=?", (wid,)).fetchone()["n"]
        msub = "(SELECT id FROM memories WHERE workspace_id=?)"
        rsub = "(SELECT id FROM repos WHERE workspace_id=?)"
        ssub = f"(SELECT id FROM symbols WHERE repo_id IN {rsub})"
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
        c.execute("DELETE FROM workspaces WHERE id=?", (wid,))
        self.store.audit(actor, "workspace_delete", wid, f"{ws} ({int(n_mem)} memories)")
        c.commit()
        return {"workspace": ws, "deleted": True, "memories_removed": int(n_mem)}

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
            "SELECT id, repo_id, name, etype FROM entities WHERE workspace_id=?", (wid_src,))]
        for e in src_entities:
            nrid = _new_repo(e["repo_id"])
            existing = c.execute(
                "SELECT id FROM entities WHERE workspace_id=? AND repo_id IS ? AND name=? AND etype IS ?",
                (wid_dst, nrid, e["name"], e["etype"])
            ).fetchone()
            if existing:
                entity_remap[e["id"]] = existing["id"]
                c.execute("DELETE FROM entities WHERE id=?", (e["id"],))
            else:
                c.execute("UPDATE entities SET workspace_id=?, repo_id=? WHERE id=?",
                          (wid_dst, nrid, e["id"]))

        # 3) Edges: relabel workspace/repo, remapping any entity ids folded in step 2.
        src_edges = [dict(x) for x in c.execute(
            "SELECT id, repo_id, src, dst FROM edges WHERE workspace_id=?", (wid_src,))]
        for ed in src_edges:
            c.execute(
                "UPDATE edges SET workspace_id=?, repo_id=?, src=?, dst=? WHERE id=?",
                (wid_dst, _new_repo(ed["repo_id"]),
                 entity_remap.get(ed["src"], ed["src"]), entity_remap.get(ed["dst"], ed["dst"]),
                 ed["id"]))

        # 4) Memories / sessions / events: relabel workspace/repo per distinct repo_id
        #    bucket (ids, content and history are untouched).
        for table in ("memories", "sessions", "events"):
            buckets = [dict(x) for x in c.execute(
                f"SELECT DISTINCT repo_id FROM {table} WHERE workspace_id=?", (wid_src,))]
            for b in buckets:
                c.execute(
                    f"UPDATE {table} SET workspace_id=?, repo_id=? "
                    f"WHERE workspace_id=? AND repo_id IS ?",
                    (wid_dst, _new_repo(b["repo_id"]), wid_src, b["repo_id"]))

        # 5) The source workspace is now empty — drop it.
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
        entity_remap: dict = {}
        for e in [dict(x) for x in c.execute(
                "SELECT * FROM entities WHERE workspace_id=?", (wid_src,))]:
            neid = ids.new_id("entity")
            entity_remap[e["id"]] = neid
            c.execute(
                "INSERT INTO entities(id, workspace_id, repo_id, name, etype, canonical_id, "
                "created_at) VALUES (?,?,?,?,?,?,?)",
                (neid, wid_dst, _new_repo(e["repo_id"]), e["name"], e["etype"],
                 e["canonical_id"], ts))

        # 3) Entity-graph edges, remapped onto the cloned entities/repos.
        for ed in [dict(x) for x in c.execute(
                "SELECT * FROM edges WHERE workspace_id=?", (wid_src,))]:
            c.execute(
                "INSERT INTO edges(id, workspace_id, repo_id, src, dst, relation, layer, "
                "weight, valid_from, valid_to, ingested_at, expired_at, provenance) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ids.new_id("edge"), wid_dst, _new_repo(ed["repo_id"]),
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
        memory_remap: dict = {}
        for m in [dict(x) for x in c.execute(
                "SELECT * FROM memories WHERE workspace_id=?", (wid_src,))]:
            nmid = ids.new_id("memory")
            memory_remap[m["id"]] = nmid
            c.execute(
                "INSERT INTO memories (id, workspace_id, repo_id, session_id, scope, mtype, "
                "title, content, summary, keywords, metadata, importance, surprise, stability, "
                "access_count, last_access, valid_from, valid_to, ingested_at, expired_at, "
                "pinned, sensitivity, provenance, sort_order) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (nmid, wid_dst, _new_repo(m["repo_id"]), session_remap.get(m["session_id"]),
                 m["scope"], m["mtype"], m["title"], m["content"], m["summary"], m["keywords"],
                 m["metadata"], m["importance"], m["surprise"], m["stability"],
                 m["access_count"], m["last_access"], m["valid_from"], m["valid_to"],
                 m["ingested_at"], m["expired_at"], m["pinned"], m["sensitivity"],
                 m["provenance"], m["sort_order"]))
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
                 session_remap.get(ev["session_id"]), ev["kind"], ev["content"], ev["refs"],
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

    def graph(self, *, workspace: str, limit: int = 2000,
              layers: Optional[list] = None, include_code: bool = False,
              repo: Optional[str] = None) -> dict:
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
        limit = max(1, min(5000, int(limit)))
        conn = self.store.conn
        ents = conn.execute(
            "SELECT id, name, etype FROM entities WHERE workspace_id=? LIMIT ?",
            (wid, limit)).fetchall()
        # Lazy backfill: old memories can predate graph extraction or predate the
        # structured-metadata graph bridge. On first Graph-tab open in a process, feed
        # the missing graph state once; feed() de-dupes entities/edges.
        if self._should_backfill_graph(wid, bool(ents)):
            self._lazy_backfill_graph(wid)
            ents = conn.execute(
                "SELECT id, name, etype FROM entities WHERE workspace_id=? LIMIT ?",
                (wid, limit)).fetchall()
        entity_rows = [dict(row) for row in ents]
        node_ids = {row["id"] for row in entity_rows}
        selected_layers = None
        if layers:
            selected_layers = {
                _enum(layer, GraphLayer, "layer").value for layer in layers
            }
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
                SearchFilter(workspace_id=wid), limit=edge_cap
            )
            if edge.src in node_ids and edge.dst in node_ids
            and (
                selected_layers is None
                or (edge.layer.value if edge.layer else "semantic") in selected_layers
            )
        ]
        repo_names: list[str] = []
        if include_code and (
            selected_layers is None
            or {"entity", "semantic"} & selected_layers
        ):
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

                if selected_layers is None or "entity" in selected_layers:
                    for edge in self.store.list_code_edges(rid, limit=edge_cap):
                        if len(edgs) >= edge_cap:
                            break
                        src = code_endpoint(edge.get("src") or "", edge.get("file") or "")
                        dst = code_endpoint(edge.get("dst") or "")
                        if src and dst and src != dst:
                            edgs.append({
                                "src": src, "dst": dst,
                                "relation": edge.get("relation") or "",
                                "layer": edge.get("layer") or "entity",
                            })
                linked_memory_ids = set()
                if selected_layers is None or "semantic" in selected_layers:
                    code_links = self.store.list_code_memory_links(rid, limit=edge_cap)
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
