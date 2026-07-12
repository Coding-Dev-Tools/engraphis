"""Engraphis v2 store — SQLite implementation of the memory/graph/event layer.

A thin, dependency-light persistence layer over the §12 schema. It deliberately
does *not* own retrieval scoring (that is the recall engine, Phase 1) — it owns
durable state and the primitives the engines need: scoped + bi-temporal reads,
vector storage, full-text, the knowledge graph, sessions, and an audit trail.

Connections use WAL + foreign keys. Vectors are stored L2-normalized so the
NumPy reference index can use a dot product as cosine similarity.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

import numpy as np

from engraphis.core import ids
from engraphis.core.interfaces import Edge, MemoryRecord, MemoryType, Node, Scope, SearchFilter
from engraphis.core.schema import (
    FTS_SQL_FALLBACK,
    FTS_SQL_FTS5,
    SCHEMA_SQL,
    SCHEMA_VERSION,
)


def now_ts() -> float:
    return time.time()


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


def _fts5_available(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS _fts_probe USING fts5(x)")
        conn.execute("DROP TABLE IF EXISTS _fts_probe")
        return True
    except sqlite3.OperationalError:
        return False


class Store:
    """A connection to one Engraphis v2 database (one file, or ``:memory:``)."""

    def __init__(self, path: str = ":memory:", *,
                 allowed_workspaces: Optional[set] = None,
                 connect: Optional[Callable[[str], Any]] = None) -> None:
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        if connect is not None:
            # Injected connection factory (e.g. the SQLCipher encrypted backend). It owns
            # opening + keying + row_factory; the core never imports the concrete driver.
            self.conn = connect(path)
        else:
            self.conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
            self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.has_fts5 = False
        self.allowed_workspaces: Optional[frozenset] = (
            frozenset(allowed_workspaces) if allowed_workspaces else None
        )
        self.init_schema()

    # ── schema ──────────────────────────────────────────────────────────────
    def init_schema(self) -> None:
        self.conn.executescript(SCHEMA_SQL)
        self.has_fts5 = _fts5_available(self.conn)
        self.conn.execute(FTS_SQL_FTS5 if self.has_fts5 else FTS_SQL_FALLBACK)
        # Additive columns for DBs created before they existed — CREATE TABLE IF NOT
        # EXISTS above is a no-op on an already-existing table, so new columns need an
        # explicit, idempotent ALTER TABLE here (SQLite has no "ADD COLUMN IF NOT EXISTS").
        for stmt in (
            "ALTER TABLE memories ADD COLUMN sort_order REAL",
        ):
            try:
                self.conn.execute(stmt)
            except sqlite3.OperationalError:
                pass  # column already exists
        self.conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?,?)",
            (SCHEMA_VERSION, now_ts()),
        )
        self.conn.commit()

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
                      *, agent: str = "", user_id: str = "", goal: str = "") -> str:
        sid = ids.new_id("session")
        self.conn.execute(
            "INSERT INTO sessions(id, workspace_id, repo_id, agent, user_id, goal, status, "
            "started_at) VALUES (?,?,?,?,?,?,?,?)",
            (sid, workspace_id, repo_id, agent, user_id, goal, "active", now_ts()),
        )
        self.conn.commit()
        return sid

    def end_session(self, session_id: str, *, summary: str = "",
                    open_threads: Optional[list] = None, outcome: str = "") -> None:
        self.conn.execute(
            "UPDATE sessions SET status='summarized', ended_at=?, summary=?, open_threads=?, "
            "outcome=? WHERE id=?",
            (now_ts(), summary, _dumps(open_threads or []), outcome, session_id),
        )
        self.conn.commit()

    def get_session(self, session_id: str) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["open_threads"] = _loads(d.get("open_threads"), [])
        return d

    def get_active_session(self, workspace_id: str, repo_id: Optional[str],
                           *, agent: str = "") -> Optional[dict]:
        """Most recent still-``active`` session for this exact scope (and ``agent`` when
        given). Powers idempotent ``start_session``: a repeat start in the same scope
        reuses this session instead of opening a second concurrent one, which would put
        two writers on the single-writer SQLite store (the "trampling" symptom)."""
        sql = ("SELECT * FROM sessions WHERE workspace_id=? AND repo_id IS ? "
               "AND status='active'")
        params: list[Any] = [workspace_id, repo_id]
        if agent:
            sql += " AND agent=?"
            params.append(agent)
        sql += " ORDER BY started_at DESC LIMIT 1"
        row = self.conn.execute(sql, params).fetchone()
        if not row:
            return None
        d = dict(row)
        d["open_threads"] = _loads(d.get("open_threads"), [])
        return d

    def get_last_session(self, workspace_id: str, repo_id: Optional[str],
                         *, exclude: Optional[str] = None) -> Optional[dict]:
        """Most recently *ended* session in this repo — the cross-session handoff
        source: the next session bootstraps from its
        ``summary``/``open_threads`` instead of starting from nothing."""
        sql = ("SELECT * FROM sessions WHERE workspace_id=? AND repo_id IS ? "
               "AND ended_at IS NOT NULL")
        params: list[Any] = [workspace_id, repo_id]
        if exclude:
            sql += " AND id != ?"
            params.append(exclude)
        sql += " ORDER BY ended_at DESC LIMIT 1"
        row = self.conn.execute(sql, params).fetchone()
        if not row:
            return None
        d = dict(row)
        d["open_threads"] = _loads(d.get("open_threads"), [])
        return d

    # ── memories ──────────────────────────────────────────────────────────────
    def add_memory(self, rec: MemoryRecord, *, audit: bool = True) -> str:
        if not rec.id:
            rec.id = ids.new_id("memory")
        existing = self.conn.execute(
            "SELECT provenance, workspace_id FROM memories WHERE id=?", (rec.id,)
        ).fetchone()
        if existing is not None:
            if existing["workspace_id"] != rec.workspace_id:
                self.audit("system", "cross_workspace_overwrite_blocked", rec.id,
                           f"existing workspace={existing['workspace_id']}, "
                           f"incoming workspace={rec.workspace_id}")
                rec.id = ids.new_id("memory")
            elif audit:
                # Generic provenance-change record for direct writes. The sync path
                # passes audit=False and logs its own semantic 'sync_overwrite' instead,
                # so a synced update yields exactly one audit row rather than a duplicate.
                self.audit("system", "overwrite", rec.id,
                           f"existing provenance={existing['provenance']}, "
                           f"incoming provenance={_dumps(rec.provenance)}")
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
        self.conn.commit()
        return rec.id

    def get_memory(self, memory_id: str) -> Optional[MemoryRecord]:
        row = self.conn.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
        return _row_to_record(row) if row else None

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

    def close_validity(self, memory_id: str, *, at: Optional[float] = None,
                       actor: str = "system", reason: str = "contradicted") -> None:
        """Bi-temporal invalidation (§8.3): close a fact's validity window without deleting."""
        at = at if at is not None else now_ts()
        self.conn.execute("UPDATE memories SET valid_to=? WHERE id=? AND valid_to IS NULL",
                          (at, memory_id))
        self.audit(actor, "invalidate", memory_id, reason)
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
                     *, include_invalid: bool = False) -> Iterable[tuple[str, np.ndarray]]:
        """Yield (id, normalized vector) for memories matching the filter."""
        where, params = self._where(flt, include_invalid, alias="m")
        sql = ("SELECT v.id AS id, v.vector AS vector FROM mem_vectors v "
               "JOIN memories m ON m.id = v.id")
        if where:
            sql += " WHERE " + " AND ".join(where)
        for r in self.conn.execute(sql, params):
            yield r["id"], np.frombuffer(r["vector"], dtype=np.float32)

    # ── full text ─────────────────────────────────────────────────────────────
    def _fts_upsert(self, mid: str, title: str, content: str, keywords: str) -> None:
        self.conn.execute("DELETE FROM mem_fts WHERE id=?", (mid,))
        self.conn.execute(
            "INSERT INTO mem_fts(id, title, content, keywords) VALUES (?,?,?,?)",
            (mid, title, content, keywords),
        )

    def fts_search(self, query: str, k: int = 20) -> list[tuple[str, float]]:
        """Lexical arm. Uses FTS5 BM25 when available, else a LIKE fallback."""
        q = (query or "").strip()
        if not q:
            return []
        if self.has_fts5:
            try:
                rows = self.conn.execute(
                    "SELECT id, bm25(mem_fts) AS rank FROM mem_fts "
                    "WHERE mem_fts MATCH ? ORDER BY rank LIMIT ?",
                    (_fts_query(q), k),
                ).fetchall()
                # bm25: lower is better → convert to a descending score
                return [(r["id"], 1.0 / (1.0 + max(r["rank"], 0.0))) for r in rows]
            except sqlite3.OperationalError:
                pass
        like = f"%{q}%"
        rows = self.conn.execute(
            "SELECT id FROM mem_fts WHERE content LIKE ? OR title LIKE ? LIMIT ?",
            (like, like, k),
        ).fetchall()
        return [(r["id"], 0.5) for r in rows]

    # ── graph ─────────────────────────────────────────────────────────────────
    def upsert_entity(self, node: Node) -> str:
        existing = self.conn.execute(
            "SELECT id FROM entities WHERE workspace_id=? AND repo_id IS ? AND name=? AND etype IS ?",
            (node.workspace_id, node.repo_id, node.name, node.ntype),
        ).fetchone()
        if existing:
            return existing["id"]
        nid = node.id or ids.new_id("entity")
        self.conn.execute(
            "INSERT INTO entities(id, workspace_id, repo_id, name, etype, canonical_id, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (nid, node.workspace_id, node.repo_id, node.name, node.ntype,
             node.canonical_id, now_ts()),
        )
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

    def upsert_edge(self, edge: Edge) -> str:
        eid = edge.id or ids.new_id("edge")
        self.conn.execute(
            "INSERT OR REPLACE INTO edges(id, workspace_id, repo_id, src, dst, relation, weight, "
            "valid_from, valid_to, ingested_at, expired_at, provenance) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (eid, edge.workspace_id, edge.repo_id, edge.src, edge.dst, edge.relation, edge.weight,
             edge.valid_from if edge.valid_from is not None else now_ts(), edge.valid_to,
             edge.ingested_at or now_ts(), edge.expired_at, _dumps(edge.provenance)),
        )
        self.conn.commit()
        return eid

    def invalidate_edge(self, edge_id: str, at: Optional[float] = None) -> None:
        self.conn.execute("UPDATE edges SET valid_to=? WHERE id=? AND valid_to IS NULL",
                          (at or now_ts(), edge_id))
        self.conn.commit()

    # ── memory-to-memory links (A-MEM style) ────────────────────────────────────
    def add_link(self, a: str, b: str, relation: str = "related") -> None:
        """Idempotent per (pair, relation): re-linking the same two memories with the
        same relation is a no-op in either direction, so auto-evolution and explicit
        ``engraphis_link`` calls can't accrete duplicate rows."""
        if self.has_link(a, b, relation=relation):
            return
        self.conn.execute(
            "INSERT INTO mem_links(a, b, relation, created_at) VALUES (?,?,?,?)",
            (a, b, relation, now_ts()),
        )
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
            "SELECT a, b, relation, created_at FROM mem_links WHERE a=? OR b=?",
            (memory_id, memory_id),
        ).fetchall()
        return [dict(r) for r in rows]

    def edges_in_scope(self, flt: Optional[SearchFilter] = None,
                       *, at: Optional[float] = None) -> list[Edge]:
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
            sql += " AND repo_id=?"
            params.append(flt.repo_id)
        rows = self.conn.execute(sql, params).fetchall()
        return [_row_to_edge(r) for r in rows]

    def links_among(self, ids: list[str]) -> list[dict]:
        """mem_links rows where *both* endpoints are in ``ids`` (for graph retrieval)."""
        if not ids:
            return []
        marks = ",".join("?" for _ in ids)
        rows = self.conn.execute(
            f"SELECT a, b, relation FROM mem_links WHERE a IN ({marks}) AND b IN ({marks})",
            (*ids, *ids),
        ).fetchall()
        return [dict(r) for r in rows]

    def neighbors(self, node_ids: list[str], *, at: Optional[float] = None) -> list[Edge]:
        if not node_ids:
            return []
        t = at if at is not None else now_ts()
        marks = ",".join("?" for _ in node_ids)
        rows = self.conn.execute(
            f"SELECT * FROM edges WHERE (src IN ({marks}) OR dst IN ({marks})) "
            f"AND (valid_from IS NULL OR valid_from<=?) AND (valid_to IS NULL OR ?<valid_to) "
            f"AND expired_at IS NULL",
            (*node_ids, *node_ids, t, t),
        ).fetchall()
        return [_row_to_edge(r) for r in rows]

    # ── code symbol graph ────────────────────────────────────────────────────────
    def clear_symbols_for_file(self, repo_id: str, file: str) -> None:
        """Re-indexing a file replaces its symbols/edges — incremental indexing is
        idempotent per file, not additive."""
        self.conn.execute("DELETE FROM symbols WHERE repo_id=? AND file=?", (repo_id, file))
        self.conn.execute("DELETE FROM code_edges WHERE repo_id=? AND file=?", (repo_id, file))
        self.conn.commit()

    def upsert_symbol(self, *, repo_id: str, kind: str, name: str, fqname: str, file: str,
                      span: str, signature: str = "", lang: str = "", exported: bool = False,
                      content_hash: str = "") -> str:
        sid = ids.new_id("symbol")
        self.conn.execute(
            "INSERT INTO symbols(id, repo_id, kind, name, fqname, file, span, signature, "
            "lang, exported, content_hash, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, repo_id, kind, name, fqname, file, span, signature, lang, int(exported),
             content_hash, now_ts()),
        )
        self.conn.commit()
        return sid

    def add_code_edge(self, *, repo_id: str, src: str, dst: str, relation: str,
                      file: str = "", line: int = 0) -> str:
        eid = ids.new_id("edge")
        self.conn.execute(
            "INSERT INTO code_edges(id, repo_id, src, dst, relation, file, line) "
            "VALUES (?,?,?,?,?,?,?)",
            (eid, repo_id, src, dst, relation, file, line),
        )
        self.conn.commit()
        return eid

    def search_symbols(self, repo_id: str, query: str, *, limit: int = 20) -> list[dict]:
        """Substring match on name/fqname (no embedding yet — v1 is lexical)."""
        like = f"%{query}%"
        rows = self.conn.execute(
            "SELECT * FROM symbols WHERE repo_id=? AND (name LIKE ? OR fqname LIKE ?) "
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

    # ── events & audit ──────────────────────────────────────────────────────
    def append_event(self, *, kind: str, content: str, workspace_id: str = "",
                     repo_id: str = "", session_id: str = "", refs: Optional[list] = None,
                     interaction_level: str = "") -> str:
        eid = ids.new_id("event")
        self.conn.execute(
            "INSERT INTO events(id, workspace_id, repo_id, session_id, kind, content, refs, "
            "interaction_level, ts) VALUES (?,?,?,?,?,?,?,?,?)",
            (eid, workspace_id, repo_id, session_id, kind, content, _dumps(refs or []),
             interaction_level, now_ts()),
        )
        self.conn.commit()
        return eid

    def audit(self, actor: str, action: str, target: str, detail: str = "") -> None:
        self.conn.execute(
            "INSERT INTO audit(id, ts, actor, action, target, detail) VALUES (?,?,?,?,?,?)",
            (ids.new_id("audit"), now_ts(), actor, action, target, detail),
        )

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

