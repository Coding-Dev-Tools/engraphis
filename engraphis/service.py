"""MemoryService — transport-agnostic facade over :class:`MemoryEngine`.

This is the layer the MCP server (and any other front end) calls. It deliberately
has **no MCP dependency**, so it runs and unit-tests offline on ``numpy`` alone
(per AGENTS.md §3). Responsibilities:

* resolve human-friendly ``workspace`` / ``repo`` names to scoped IDs;
* **validate and sanitize all untrusted input** before it reaches the store —
  ingested content is untrusted and memory poisoning is an explicit threat
  (MASTER_PLAN.md §16). Validation lives here so every front end inherits it;
* return plain JSON-serializable dicts.

The companion :mod:`engraphis.mcp_server` is a thin binding of these methods to
MCP tools; nothing in this module imports ``mcp``.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from engraphis.core.engine import MemoryEngine
from engraphis.core.interfaces import MemoryType, Scope

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

    def __init__(self, engine: MemoryEngine) -> None:
        self.engine = engine
        self.store = engine.store

    @classmethod
    def create(cls, db_path: str = ":memory:", *, embed_model: Optional[str] = None,
               embed_dim: int = 256, vector_backend: str = "auto",
               rerank_model: Optional[str] = None) -> "MemoryService":
        engine = MemoryEngine.create(
            db_path, embed_model=embed_model, embed_dim=embed_dim,
            vector_backend=vector_backend, rerank_model=rerank_model,
        )
        return cls(engine)

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
        ws = _clean_name(workspace, field="workspace")
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
                 source: str = "agent", resolve_conflicts: bool = True) -> dict:
        """Store one memory. Returns its id, resolved scope, and the resolution
        outcome (``op``: add/noop/invalidate — see ``MemoryEngine.remember_with_resolution``).
        """
        content = _clean_text(content, field="content", max_chars=MAX_CONTENT_CHARS)
        title = _clean_text(title, field="title", max_chars=MAX_TITLE_CHARS, required=False)
        ws = _clean_name(workspace, field="workspace")
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
                                            required=False) or "agent"}
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

        wid = rid = None
        if workspace:
            ws = _clean_name(workspace, field="workspace")
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

    # ── session lifecycle ───────────────────────────────────────────────────────
    def start_session(self, workspace: str, *, repo: Optional[str] = None,
                      agent: str = "", goal: str = "") -> dict:
        """Open a session. If this repo has a prior *ended* session, its summary and
        unresolved ``open_threads`` come back as ``bootstrap`` — the concrete fix for
        "the agent forgets everything between sessions" (MASTER_PLAN.md §10.1-10.2)."""
        ws = _clean_name(workspace, field="workspace")
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
              reason: str = "") -> dict:
        mid = _clean_text(memory_id, field="memory_id", max_chars=MAX_NAME_CHARS)
        reason = _clean_text(reason, field="reason", max_chars=MAX_TITLE_CHARS, required=False)
        wid, rid = self._require_scope(workspace, repo)
        self._check_owns(mid, wid, rid)
        try:
            return self.engine.forget(mid, reason=reason)
        except KeyError as exc:
            raise ValidationError(str(exc))

    def pin(self, memory_id: str, *, workspace: str, repo: Optional[str] = None,
           pinned: bool = True) -> dict:
        mid = _clean_text(memory_id, field="memory_id", max_chars=MAX_NAME_CHARS)
        wid, rid = self._require_scope(workspace, repo)
        self._check_owns(mid, wid, rid)
        try:
            return self.engine.pin(mid, pinned=bool(pinned))
        except KeyError as exc:
            raise ValidationError(str(exc))

    def correct(self, memory_id: str, new_content: str, *, workspace: str,
               repo: Optional[str] = None, reason: str = "") -> dict:
        mid = _clean_text(memory_id, field="memory_id", max_chars=MAX_NAME_CHARS)
        new_content = _clean_text(new_content, field="new_content", max_chars=MAX_CONTENT_CHARS)
        reason = _clean_text(reason, field="reason", max_chars=MAX_TITLE_CHARS, required=False)
        wid, rid = self._require_scope(workspace, repo)
        self._check_owns(mid, wid, rid)
        try:
            return self.engine.correct(mid, new_content, reason=reason)
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

    # ── code-symbol graph (MASTER_PLAN.md §9) ───────────────────────────────────
    def index_repo(self, *, workspace: str, repo: str, root_path: str,
                   languages: Optional[list] = None) -> dict:
        """Index (or re-index) a repo's code graph. Like ``remember``/``start_session``,
        this creates the workspace/repo if this is the first time you've named them —
        indexing a brand-new repo is the common case, unlike the read-only code tools
        below which require the repo to already exist."""
        if not repo:
            raise ValidationError("repo is required to index code")
        ws = _clean_name(workspace, field="workspace")
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

    # ── introspection ───────────────────────────────────────────────────────────
    def stats(self, *, workspace: Optional[str] = None) -> dict:
        """Counts for quick health/onboarding checks (read-only)."""
        conn = self.store.conn
        params: list[Any] = []
        where = ""
        if workspace:
            ws = _clean_name(workspace, field="workspace")
            wid = self._lookup_workspace(ws)
            if wid is None:
                return {"workspace": ws, "memories": 0, "note": "workspace not found"}
            where = " WHERE workspace_id=?"
            params.append(wid)
        total = conn.execute(f"SELECT COUNT(*) AS n FROM memories{where}", params).fetchone()["n"]
        by_type = {
            r["mtype"]: r["n"] for r in conn.execute(
                f"SELECT mtype, COUNT(*) AS n FROM memories{where} GROUP BY mtype", params
            )
        }
        workspaces = conn.execute("SELECT COUNT(*) AS n FROM workspaces").fetchone()["n"]
        sessions = conn.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"]
        return {
            "workspace": workspace, "memories": int(total), "by_type": by_type,
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
