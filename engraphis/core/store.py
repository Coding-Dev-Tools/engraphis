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
from typing import Any, Iterable, Optional

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
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _loads(raw: Any, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
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

    def __init__(self, path: str = ":memory:") -> None:
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.has_fts5 = False
        self.init_schema()

    # ── schema ──────────────────────────────────────────────────────────────
    def init_schema(self) -> None:
        self.conn.executescript(SCHEMA_SQL)
        self.has_fts5 = _fts5_available(self.conn)
        self.conn.execute(FTS_SQL_FTS5 if self.has_fts5 else FTS_SQL_FALLBACK)
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
    def create_workspace(self, name: str, *, settings: Optional[dict] = None) -> str:
        wid = ids.new_id("workspace")
        self.conn.execute(
            "INSERT INTO workspaces(id, name, created_at, settings) VALUES (?,?,?,?)",
            (wid, name, now_ts(), _dumps(settings or {})),
        )
        self.conn.commit()
        return wid

    def get_or_create_workspace(self, name: str) -> str:
        row = self.conn.execute("SELECT id FROM workspaces WHERE name=?", (name,)).fetchone()
        return row["id"] if row else self.create_workspace(name)

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

    # ── memories ──────────────────────────────────────────────────────────────
    def add_memory(self, rec: MemoryRecord) -> str:
        if not rec.id:
            rec.id = ids.new_id("memory")
        ts = now_ts()
        rec.ingested_at = rec.ingested_at or ts
        rec.valid_from = rec.valid_from if rec.valid_from is not None else ts
        rec.last_access = rec.last_access or ts
        self.conn.execute(
            """INSERT OR REPLACE INTO memories
               (id, workspace_id, repo_id, session_id, scope, mtype, title, content, summary,
                keywords, metadata, importance, surprise, stability, access_count, last_access,
                valid_from, valid_to, ingested_at, expired_at, pinned, sensitivity, provenance)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
        at = at or now_ts()
        self.conn.execute("UPDATE memories SET valid_to=? WHERE id=? AND valid_to IS NULL",
                          (at, memory_id))
        self.audit(actor, "invalidate", memory_id, reason)
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
            "SELECT id FROM entities WHERE workspace_id IS ? AND repo_id IS ? AND name=? AND etype IS ?",
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

    def neighbors(self, node_ids: list[str], *, at: Optional[float] = None) -> list[Edge]:
        if not node_ids:
            return []
        t = at or now_ts()
        marks = ",".join("?" for _ in node_ids)
        rows = self.conn.execute(
            f"SELECT * FROM edges WHERE (src IN ({marks}) OR dst IN ({marks})) "
            f"AND (valid_from IS NULL OR valid_from<=?) AND (valid_to IS NULL OR ?<valid_to) "
            f"AND expired_at IS NULL",
            (*node_ids, *node_ids, t, t),
        ).fetchall()
        return [_row_to_edge(r) for r in rows]

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

    # ── helpers ───────────────────────────────────────────────────────────────
    def _where(self, flt: Optional[SearchFilter], include_invalid: bool,
               alias: str = "") -> tuple[list[str], list[Any]]:
        p = f"{alias}." if alias else ""
        where: list[str] = []
        params: list[Any] = []
        if flt:
            if flt.workspace_id:
                where.append(f"{p}workspace_id=?"); params.append(flt.workspace_id)
            if flt.repo_id:
                where.append(f"{p}repo_id=?"); params.append(flt.repo_id)
            if flt.session_id:
                where.append(f"{p}session_id=?"); params.append(flt.session_id)
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
            where.append(f"({p}valid_from IS NULL OR {p}valid_from<=?)"); params.append(t)
            where.append(f"({p}valid_to IS NULL OR ?<{p}valid_to)"); params.append(t)
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
        weight=row["weight"], valid_from=row["valid_from"], valid_to=row["valid_to"],
        ingested_at=row["ingested_at"], expired_at=row["expired_at"],
        provenance=_loads(row["provenance"], {}),
    )


def _fts_query(q: str) -> str:
    """Make a safe FTS5 MATCH query: OR the alphanumeric terms as prefixes."""
    terms = [t for t in "".join(c if c.isalnum() else " " for c in q).split() if t]
    return " OR ".join(f'{t}*' for t in terms) if terms else '""'
