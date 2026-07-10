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

import re
import json
from typing import Any, Optional

from engraphis.core.engine import MemoryEngine
from engraphis.core.interfaces import MemoryType, Scope
from engraphis.graphdata import build_graph_payload, empty_graph

# ── validation limits (memory-poisoning / resource-exhaustion guards) ──────────
MAX_CONTENT_CHARS = 100_000
MAX_TITLE_CHARS = 1_000
MAX_NAME_CHARS = 200
MAX_KEYWORDS = 64
MAX_KEYWORD_CHARS = 128
MAX_METADATA_BYTES = 16_384
MAX_K = 50

# control characters except tab/newline/carriage-return
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_NAME_RE = re.compile(r"^[A-Za-z0-9._\-/ ]{1,%d}$" % MAX_NAME_CHARS)


class ValidationError(ValueError):
    """Raised when untrusted input fails a guard. Message is safe to surface."""


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


def _clean_metadata(value: Any) -> dict:
    if not value:
        return {}
    if not isinstance(value, dict):
        raise ValidationError("metadata must be an object")
    import json
    try:
        encoded = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        raise ValidationError("metadata must be JSON-serializable")
    if len(encoded.encode("utf-8")) > MAX_METADATA_BYTES:
        raise ValidationError(f"metadata exceeds {MAX_METADATA_BYTES} bytes")
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

    @classmethod
    def create(cls, db_path: str = ":memory:", *, embed_model: Optional[str] = None,
               embed_dim: int = 256, vector_backend: str = "auto",
               rerank_model: Optional[str] = None,
               allowed_workspaces: Optional[list] = None,
               extractor: str = "none") -> "MemoryService":
        engine = MemoryEngine.create(
            db_path, embed_model=embed_model, embed_dim=embed_dim,
            vector_backend=vector_backend, rerank_model=rerank_model,
            extractor=extractor,
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
        single-tenant local default — is unrestricted, so existing setups are unaffected."""
        if self.allowed_workspaces is not None and ws not in self.allowed_workspaces:
            raise ValidationError(f"workspace '{ws}' is not permitted on this instance")
        return ws

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

    # ── write ──────────────────────────────────────────────────────────────────
    def remember(self, content: str, *, workspace: str, repo: Optional[str] = None,
                 session_id: Optional[str] = None, mtype: str = "semantic",
                 scope: str = "repo", title: str = "", importance: float = 0.0,
                 keywords: Optional[list] = None, metadata: Optional[dict] = None,
                 source: str = "agent", trusted: bool = True,
                 kind: Optional[str] = None, resolve_conflicts: bool = True) -> dict:
        """Store one memory. Returns its id, resolved scope, and the resolution
        outcome (``op``: add/noop/invalidate — see ``MemoryEngine.remember_with_resolution``).
        """
        content = _clean_text(content, field="content", max_chars=MAX_CONTENT_CHARS)
        title = _clean_text(title, field="title", max_chars=MAX_TITLE_CHARS, required=False)
        ws = self._clean_ws(workspace)
        rp = _clean_name(repo, field="repo") if repo else None
        mt = _enum(mtype, MemoryType, "mtype")
        sc = _enum(scope, Scope, "scope")
        kws = _clean_keywords(keywords)
        meta = _clean_metadata(metadata)
        try:
            importance = float(importance)
        except (TypeError, ValueError):
            raise ValidationError("importance must be a number")
        importance = max(0.0, min(1.0, importance))

        wid = self.store.get_or_create_workspace(ws)
        rid = self.store.get_or_create_repo(wid, rp) if rp else None
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
        return out

    def ingest(self, content: str, *, workspace: str, repo: Optional[str] = None,
               session_id: Optional[str] = None, mtype: str = "semantic",
               scope: str = "repo", metadata: Optional[dict] = None,
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
        sc = _enum(scope, Scope, "scope")
        meta = _clean_metadata(metadata)
        wid = self.store.get_or_create_workspace(ws)
        rid = self.store.get_or_create_repo(wid, rp) if rp else None
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
        return {"workspace": ws, "repo": rp, "count": out["count"],
                "extracted": out["extracted"],
                "facts": [{"id": r["id"], "op": r["op"],
                           **({"superseded": r["superseded"]} if "superseded" in r else {})}
                          for r in out["facts"]]}

    def consolidate(self, *, workspace: str, repo: Optional[str] = None,
                    dry_run: bool = False, min_cluster: int = 3,
                    archive_below: float = 0.05, profiles: bool = False,
                    min_mentions: int = 3) -> dict:
        """Sleep-time consolidation sweep (episodic→semantic distillation + decayed-
        transient archival). The report includes a ``compaction`` block with the tokens
        the sweep saved. With ``profiles=True`` a third pass rolls each entity's memories
        into one durable profile digest (report under ``profiles``). ``dry_run=True``
        reports without changing anything."""
        wid, rid = self._require_scope(workspace, repo)
        try:
            min_cluster = max(2, min(20, int(min_cluster)))
            archive_below = max(0.0, min(0.5, float(archive_below)))
            min_mentions = max(2, min(50, int(min_mentions)))
        except (TypeError, ValueError):
            raise ValidationError("min_cluster/min_mentions must be integers and "
                                  "archive_below a number")
        return self.engine.consolidate(workspace_id=wid, repo_id=rid, dry_run=bool(dry_run),
                                       min_cluster=min_cluster, archive_below=archive_below,
                                       profiles=bool(profiles), min_mentions=min_mentions)

    # ── read ───────────────────────────────────────────────────────────────────
    def recall(self, query: str, *, workspace: Optional[str] = None,
               repo: Optional[str] = None, mtypes: Optional[list] = None,
               k: int = 8, as_of: Optional[float] = None,
               reinforce: bool = True) -> dict:
        """Retrieve the most relevant memories for ``query`` within scope."""
        query = _clean_text(query, field="query", max_chars=MAX_CONTENT_CHARS)
        try:
            k = int(k)
        except (TypeError, ValueError):
            raise ValidationError("k must be an integer")
        k = max(1, min(MAX_K, k))
        mts = [_enum(m, MemoryType, "mtype") for m in mtypes] if mtypes else None

        # A bound instance must never do a workspace-less (global) recall — that would read
        # across every tenant's memories, the exact boundary the binding exists to enforce.
        if not workspace and self.allowed_workspaces is not None:
            raise ValidationError("workspace is required on this instance")
        wid = rid = None
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

        result = self.engine.recall_engine.recall(
            query,
            _filter(wid, rid, mts, as_of),
            k=k, reinforce=reinforce,
        )
        return {
            "query": query, "count": result.count,
            "context": result.context, "memories": result.chunks,
        }

    def grounded_recall(self, query: str, *, workspace: Optional[str] = None,
                        repo: Optional[str] = None, mtypes: Optional[list] = None,
                        k: int = 8, as_of: Optional[float] = None,
                        min_support: Optional[float] = None,
                        max_citations: int = 5) -> dict:
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
            min_support = max(0.0, min(1.0, min_support))
        mts = [_enum(m, MemoryType, "mtype") for m in mtypes] if mtypes else None

        if not workspace and self.allowed_workspaces is not None:
            raise ValidationError("workspace is required on this instance")
        wid = rid = None
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

        ans = self.engine.grounded_recall(
            query, workspace_id=wid, repo_id=rid, mtypes=mts, as_of=as_of, k=k,
            min_support=min_support, max_citations=max_citations,
        )
        return {"query": query, **ans.to_dict()}

    # ── session lifecycle ───────────────────────────────────────────────────────
    def start_session(self, workspace: str, *, repo: Optional[str] = None,
                      agent: str = "", goal: str = "") -> dict:
        """Open a session. If this repo has a prior *ended* session, its summary and
        unresolved ``open_threads`` come back as ``bootstrap`` — the concrete fix for
        "the agent forgets everything between sessions"."""
        ws = self._clean_ws(workspace)
        rp = _clean_name(repo, field="repo") if repo else None
        agent = _clean_text(agent, field="agent", max_chars=MAX_NAME_CHARS, required=False)
        goal = _clean_text(goal, field="goal", max_chars=MAX_TITLE_CHARS, required=False)
        wid = self.store.get_or_create_workspace(ws)
        rid = self.store.get_or_create_repo(wid, rp) if rp else None
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
               "status": "active", "bootstrap": bootstrap}

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

    # ── governance: forget / pin / correct (audited, never a silent hard delete) ──
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
            relation: str = "related") -> dict:
        a = _clean_text(a, field="a", max_chars=MAX_NAME_CHARS)
        b = _clean_text(b, field="b", max_chars=MAX_NAME_CHARS)
        relation = (_clean_text(relation, field="relation", max_chars=MAX_NAME_CHARS,
                                required=False) or "related")
        wid, rid = self._require_scope(workspace, repo)
        self._check_owns(a, wid, rid)
        self._check_owns(b, wid, rid)
        try:
            self.engine.link(a, b, relation=relation)
        except KeyError as exc:
            raise ValidationError(str(exc))
        return {"a": a, "b": b, "relation": relation, "linked": True}

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
        langs = set(_clean_string_list(languages, field="languages", max_items=10,
                                       max_chars=40)) if languages else None
        return self.engine.index_repo(rid, root_path, languages=langs)

    def search_code(self, query: str, *, workspace: str, repo: str, limit: int = 20) -> dict:
        if not repo:
            raise ValidationError("repo is required to search code")
        query = _clean_text(query, field="query", max_chars=MAX_CONTENT_CHARS)
        wid, rid = self._require_scope(workspace, repo)
        limit = max(1, min(MAX_K, int(limit)))
        return self.engine.search_code(query, repo_id=rid, limit=limit)

    # ── inspection (powers the Memory Inspector UI) ─────────────────────────────
    def list_workspaces(self) -> dict:
        """Workspace/repo names with live-memory counts. On a bound instance only the
        permitted workspaces are listed — same boundary as every other read."""
        rows = self.store.conn.execute(
            "SELECT w.id, w.name, w.settings AS settings, COUNT(m.id) AS n FROM workspaces w "
            "LEFT JOIN memories m ON m.workspace_id = w.id AND m.expired_at IS NULL "
            "GROUP BY w.id, w.name, w.settings ORDER BY w.name").fetchall()
        out = []
        for r in rows:
            if self.allowed_workspaces is not None and r["name"] not in self.allowed_workspaces:
                continue
            repos = [dict(x) for x in self.store.conn.execute(
                "SELECT name FROM repos WHERE workspace_id=? ORDER BY name", (r["id"],))]
            try:
                _s = json.loads(r["settings"]) if r["settings"] else {}
                _desc = (_s.get("description") or "") if isinstance(_s, dict) else ""
            except Exception:
                _desc = ""
            out.append({"name": r["name"], "memories": int(r["n"]), "description": _desc,
                        "repos": [x["name"] for x in repos]})
        return {"workspaces": out}

    # ── workspace curation (rename / describe / delete) ──────────────────────────
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
        c.execute(f"DELETE FROM code_edges WHERE repo_id IN {rsub}", (wid,))
        c.execute(f"DELETE FROM symbols WHERE repo_id IN {rsub}", (wid,))
        c.execute("DELETE FROM repos WHERE workspace_id=?", (wid,))
        c.execute("DELETE FROM workspaces WHERE id=?", (wid,))
        self.store.audit(actor, "workspace_delete", wid, f"{ws} ({int(n_mem)} memories)")
        c.commit()
        return {"workspace": ws, "deleted": True, "memories_removed": int(n_mem)}

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
                          "title": (other.title or other.content[:80]) if other else "?",
                          "live": bool(other and other.expired_at is None and
                                       other.valid_to is None)})
        audit = [dict(r) for r in self.store.conn.execute(
            "SELECT ts, actor, action, detail FROM audit WHERE target=? ORDER BY ts", (mid,))]
        chain = [self._chain_entry(r, wid) for r in self._chain_for(rec)]
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

    def _chain_for(self, rec) -> list:
        """Walk the supersession chain in both directions from ``rec``:
        backward via this record's ``supersedes``/``corrects`` metadata, forward by
        finding records that point back at it. Returns oldest→newest."""
        def predecessors(r):
            ids = list(r.metadata.get("supersedes") or [])
            if r.metadata.get("corrects"):
                ids.append(r.metadata["corrects"])
            return ids

        seen = {rec.id}
        back = []
        cur = rec
        while True:
            prev = None
            for pid in predecessors(cur):
                if pid not in seen:
                    prev = self.store.get_memory(pid)
                    break
            if prev is None:
                break
            back.append(prev)
            seen.add(prev.id)
            cur = prev
        fwd = []
        cur = rec
        while True:
            nxt = self._successor_of(cur.id, seen)
            if nxt is None:
                break
            fwd.append(nxt)
            seen.add(nxt.id)
            cur = nxt
        return list(reversed(back)) + [rec] + fwd

    def _successor_of(self, memory_id: str, seen: set):
        rows = self.store.conn.execute(
            "SELECT id, metadata FROM memories WHERE metadata LIKE ? AND id != ?",
            (f"%{memory_id}%", memory_id)).fetchall()
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

    def export_workspace(self, *, workspace: str) -> dict:
        """Full bi-temporal dump of one workspace — memories (live *and* superseded),
        sessions, and the audit trail. The compliance story in one artifact: nothing is
        ever silently deleted, and the export proves it. Scope-checked like any other
        read; the Pro license gate lives in the HTTP layer (inspector/app.py), keeping
        the service honest for OSS callers."""
        wid, _ = self._require_scope(workspace, None)
        conn = self.store.conn
        memories = [dict(r) for r in conn.execute(
            "SELECT * FROM memories WHERE workspace_id=? ORDER BY rowid", (wid,))]
        sessions = [dict(r) for r in conn.execute(
            "SELECT * FROM sessions WHERE workspace_id=? ORDER BY rowid", (wid,))]
        audit = [dict(r) for r in conn.execute(
            "SELECT a.* FROM audit a JOIN memories m ON m.id = a.target "
            "WHERE m.workspace_id=? ORDER BY a.ts", (wid,))]
        import time as _time
        return {"format": "engraphis-export/1", "exported_at": _time.time(),
                "workspace": workspace, "counts": {"memories": len(memories),
                "sessions": len(sessions), "audit": len(audit)},
                "memories": memories, "sessions": sessions, "audit": audit}

    def graph(self, *, workspace: str, limit: int = 2000) -> dict:
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
            "SELECT name, etype FROM entities WHERE workspace_id=? LIMIT ?",
            (wid, limit)).fetchall()
        edgs = conn.execute(
            "SELECT src, dst, relation FROM edges WHERE workspace_id=?", (wid,)).fetchall()
        return build_graph_payload(ws, ents, edgs)

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


def _filter(workspace_id, repo_id, mtypes, as_of):
    from engraphis.core.interfaces import SearchFilter
    return SearchFilter(workspace_id=workspace_id, repo_id=repo_id, mtypes=mtypes, as_of=as_of)


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
