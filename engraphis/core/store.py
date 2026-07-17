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
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

import numpy as np

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
)
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


def _provenance_memory_ids(provenance: Any) -> list[str]:
    if not isinstance(provenance, dict):
        return []
    values = [provenance.get("memory_id")]
    many = provenance.get("memory_ids")
    if isinstance(many, (list, tuple, set)):
        values.extend(many)
    out: list[str] = []
    for value in values:
        mid = str(value or "")
        if mid and mid not in out:
            out.append(mid)
    return out


def _receipt_metadata(metadata: dict) -> dict:
    """Keep receipt metadata useful but content-free and bounded."""
    allowed = {
        "mtype", "scope", "resolution", "retention", "extracted", "intent", "k",
        "result_count", "grounded", "citations", "relation", "layer", "graph_layers",
        "files_scanned", "files_indexed", "files_removed", "symbols", "edges",
        "entities", "relations", "tables",
    }
    out: dict[str, Any] = {}
    for key in sorted(metadata, key=lambda item: str(item))[:24]:
        safe_key = str(key)[:64]
        if safe_key not in allowed:
            continue
        value = metadata[key]
        if isinstance(value, bool) or value is None:
            out[safe_key] = value
        elif isinstance(value, (int, float)):
            out[safe_key] = value
        elif isinstance(value, str):
            out[safe_key] = (
                value[:80] if len(value) <= 80
                else "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()
            )
        elif isinstance(value, (list, tuple)):
            out[safe_key] = len(value)
    return out


def _fts5_available(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS _fts_probe USING fts5(x)")
        conn.execute("DROP TABLE IF EXISTS _fts_probe")
        return True
    except sqlite3.OperationalError:
        return False


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
        if flt.scopes and rec.scope not in flt.scopes:
            return False
        if flt.mtypes and rec.mtype not in flt.mtypes:
            return False
    if include_invalid:
        return True
    t = at if at is not None else (
        flt.as_of if flt and flt.as_of is not None else now_ts()
    )
    if rec.expired_at is not None:
        return False
    if rec.valid_from is not None and rec.valid_from > t:
        return False
    if rec.valid_to is not None and t >= rec.valid_to:
        return False
    return True


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
    released on commit/rollback — so transactions never interleave. Read-only statements
    lock only for the individual call. Two safety nets keep a stuck transaction from
    deadlocking the process: a statement that raises while a transaction is open rolls it
    back and frees the pin, and lock acquisition times out (raising, not blocking forever).
    Non-statement attributes/methods (``in_transaction``, ``enable_load_extension`` at
    setup, ...) pass straight through.
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
        self._acquire()
        try:
            fn()
        finally:
            if self._pinned():
                self._pin.held = False
                self._lock.release()          # release the transaction pin
            self._lock.release()              # release this call's acquire

    def execute(self, *a, **k):
        return self._run(self._raw.execute, *a, **k)

    def executemany(self, *a, **k):
        return self._run(self._raw.executemany, *a, **k)

    def executescript(self, *a, **k):
        return self._run(self._raw.executescript, *a, **k)

    def commit(self):
        self._finish(self._raw.commit)

    def rollback(self):
        self._finish(self._raw.rollback)

    def close(self):
        self._raw.close()

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
                 connect: Optional[Callable[[str], Any]] = None) -> None:
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        if connect is not None:
            # Injected connection factory (e.g. the SQLCipher encrypted backend). It owns
            # opening + keying + row_factory; the core never imports the concrete driver.
            raw_conn = connect(path)
        else:
            raw_conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
            raw_conn.row_factory = sqlite3.Row
        # Serialize the shared connection so concurrent threadpool handlers can't interleave
        # transactions on it (see _SerializedConnection). All Store/service/backend access
        # goes through self.conn, so wrapping here covers every writer.
        self.conn = _SerializedConnection(raw_conn)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.has_fts5 = False
        self._receipt_lock = threading.Lock()
        self.allowed_workspaces: Optional[frozenset] = (
            frozenset(allowed_workspaces) if allowed_workspaces else None
        )
        self.init_schema()

    # ── schema ──────────────────────────────────────────────────────────────
    def init_schema(self) -> None:
        previous_version = 0
        try:
            row = self.conn.execute(
                "SELECT MAX(version) AS v FROM schema_migrations"
            ).fetchone()
            previous_version = int(row["v"]) if row and row["v"] is not None else 0
        except Exception:
            # A new database has no migration table yet. Injected SQLite-compatible
            # drivers may use their own exception types, so treat any probe failure as
            # "no prior schema" and let the canonical schema create it below.
            previous_version = 0
        self.conn.executescript(SCHEMA_SQL)
        self.has_fts5 = _fts5_available(self.conn)
        self.conn.execute(FTS_SQL_FTS5 if self.has_fts5 else FTS_SQL_FALLBACK)
        # Additive columns for DBs created before they existed — CREATE TABLE IF NOT
        # EXISTS above is a no-op on an already-existing table, so new columns need an
        # explicit, idempotent ALTER TABLE here (SQLite has no "ADD COLUMN IF NOT EXISTS").
        for stmt in (
            "ALTER TABLE memories ADD COLUMN sort_order REAL",
            "ALTER TABLE edges ADD COLUMN layer TEXT DEFAULT 'semantic'",
            "ALTER TABLE mem_links ADD COLUMN layer TEXT DEFAULT 'semantic'",
            "ALTER TABLE mem_links ADD COLUMN reason TEXT DEFAULT ''",
            "ALTER TABLE code_edges ADD COLUMN layer TEXT DEFAULT 'entity'",
            "ALTER TABLE symbols ADD COLUMN docstring TEXT DEFAULT ''",
            "ALTER TABLE receipt_chain_heads ADD COLUMN integrity_error TEXT DEFAULT ''",
        ):
            try:
                self.conn.execute(stmt)
            except sqlite3.OperationalError:
                pass  # column already exists
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
        # Backfill the independent receipt anchor for databases created before the
        # anchor table existed. From this point onward every append updates it atomically,
        # allowing verification to detect deletion of the newest receipt as well as an
        # interior chain break.
        self.conn.execute(
            "UPDATE operation_receipts SET workspace_id='' WHERE workspace_id IS NULL"
        )
        self.conn.execute(
            "UPDATE operation_receipts SET repo_id='' WHERE repo_id IS NULL"
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO receipt_chain_heads "
            "(workspace_id, receipt_count, head_hash, integrity_error, updated_at) "
            "SELECT COALESCE(r.workspace_id, ''), COUNT(*), "
            "  (SELECT r2.receipt_hash FROM operation_receipts r2 "
            "   WHERE COALESCE(r2.workspace_id, '')=COALESCE(r.workspace_id, '') "
            "   ORDER BY r2.rowid DESC LIMIT 1), "
            "  '', "
            "  COALESCE(MAX(r.ts), 0) "
            "FROM operation_receipts r GROUP BY COALESCE(r.workspace_id, '')"
        )
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
        # Authorize on the RETRIEVE path too, not just create — otherwise a workspace
        # outside ENGRAPHIS_WORKSPACES that already exists in the DB (e.g. predating the
        # allow-list, or arriving via sync) could be handed back, silently bypassing the
        # isolation boundary _authorize_workspace is meant to enforce ("create or retrieve").
        self._authorize_workspace(name)
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
                           f"incoming workspace={rec.workspace_id}", commit=False)
                rec.id = ids.new_id("memory")
            elif audit:
                # Generic provenance-change record for direct writes. The sync path
                # passes audit=False and logs its own semantic 'sync_overwrite' instead,
                # so a synced update yields exactly one audit row rather than a duplicate.
                self.audit("system", "overwrite", rec.id,
                           f"existing provenance={existing['provenance']}, "
                           f"incoming provenance={_dumps(rec.provenance)}", commit=False)
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
        self.audit(actor, "invalidate", memory_id, reason, commit=False)
        self.invalidate_edges_for_memory(memory_id, at=at, commit=False)
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
                     *, include_invalid: bool = False,
                     dim: Optional[int] = None) -> Iterable[tuple[str, np.ndarray]]:
        """Yield normalized vectors matching the memory filter and optional dimension."""
        where, params = self._where(flt, include_invalid, alias="m")
        if dim is not None:
            where.append("v.dim=?")
            params.append(int(dim))
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

    def fts_search(self, query: str, k: int = 20,
                   *, filter: Optional[SearchFilter] = None) -> list[tuple[str, float]]:
        """Lexical arm. Uses FTS5 BM25 when available, else a LIKE fallback."""
        q = (query or "").strip()
        if not q:
            return []
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
        like = f"%{q}%"
        rows = self.conn.execute(
            "SELECT f.id FROM mem_fts f JOIN memories m ON m.id = f.id "
            "WHERE (f.content LIKE ? OR f.title LIKE ?)" + extra + " LIMIT ?",
            (like, like, *params, k),
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
        layer = normalize_graph_layer(edge.layer, edge.relation).value
        self.conn.execute(
            "INSERT OR REPLACE INTO edges(id, workspace_id, repo_id, src, dst, relation, layer, "
            "weight, valid_from, valid_to, ingested_at, expired_at, provenance) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (eid, edge.workspace_id, edge.repo_id, edge.src, edge.dst, edge.relation, layer,
             edge.weight, edge.valid_from if edge.valid_from is not None else now_ts(),
             edge.valid_to, edge.ingested_at or now_ts(), edge.expired_at,
             _dumps(edge.provenance)),
        )
        self.conn.commit()
        return eid

    def invalidate_edge(self, edge_id: str, at: Optional[float] = None) -> None:
        self.conn.execute("UPDATE edges SET valid_to=? WHERE id=? AND valid_to IS NULL",
                          (now_ts() if at is None else at, edge_id))
        self.conn.commit()

    def add_edge_support(self, edge_id: str, provenance: dict) -> None:
        """Record another source memory supporting an existing graph edge."""
        incoming = _provenance_memory_ids(provenance)
        if not incoming:
            return
        row = self.conn.execute("SELECT provenance FROM edges WHERE id=?", (edge_id,)).fetchone()
        if row is None:
            return
        stored = _loads(row["provenance"], {})
        if not isinstance(stored, dict):
            stored = {}
        supports = _provenance_memory_ids(stored)
        merged = supports + [mid for mid in incoming if mid not in supports]
        if merged == supports:
            return
        stored["memory_id"] = merged[0]
        stored["memory_ids"] = merged
        self.conn.execute("UPDATE edges SET provenance=? WHERE id=?",
                          (_dumps(stored), edge_id))
        self.conn.commit()

    def invalidate_edges_for_memory(self, memory_id: str, *, at: Optional[float] = None,
                                    commit: bool = True) -> None:
        """Remove one memory's support and close edges with no remaining sources."""
        ts = at if at is not None else now_ts()
        rows = self.conn.execute(
            "SELECT id, provenance FROM edges WHERE valid_to IS NULL AND provenance LIKE ?",
            (f"%{memory_id}%",),
        ).fetchall()
        ids_to_close: list[str] = []
        for row in rows:
            prov = _loads(row["provenance"], {})
            supports = _provenance_memory_ids(prov)
            if memory_id not in supports:
                continue
            remaining = [mid for mid in supports if mid != memory_id]
            if not remaining:
                ids_to_close.append(row["id"])
                continue
            prov["memory_id"] = remaining[0]
            prov["memory_ids"] = remaining
            self.conn.execute("UPDATE edges SET provenance=? WHERE id=?",
                              (_dumps(prov), row["id"]))
        if ids_to_close:
            marks = ",".join("?" for _ in ids_to_close)
            self.conn.execute(f"UPDATE edges SET valid_to=? WHERE id IN ({marks})",
                              (ts, *ids_to_close))
        if commit:
            self.conn.commit()

    # ── memory-to-memory links (A-MEM style) ────────────────────────────────────
    def add_link(self, a: str, b: str, relation: str = "related",
                 layer: Optional[GraphLayer] = None, reason: str = "") -> None:
        """Idempotent per (pair, relation): re-linking the same two memories with the
        same relation is a no-op in either direction, so auto-evolution and explicit
        ``engraphis_link`` calls can't accrete duplicate rows."""
        existing = self.conn.execute(
            "SELECT rowid, layer, reason FROM mem_links "
            "WHERE ((a=? AND b=?) OR (a=? AND b=?)) AND relation=? LIMIT 1",
            (a, b, b, a, relation),
        ).fetchone()
        if existing:
            updates: list[str] = []
            params: list[Any] = []
            if layer is not None:
                graph_layer = normalize_graph_layer(layer, relation).value
                if existing["layer"] != graph_layer:
                    updates.append("layer=?")
                    params.append(graph_layer)
            if reason and existing["reason"] != reason:
                updates.append("reason=?")
                params.append(reason)
            if updates:
                params.append(existing["rowid"])
                self.conn.execute(
                    f"UPDATE mem_links SET {', '.join(updates)} WHERE rowid=?",
                    params,
                )
                self.conn.commit()
            return
        graph_layer = normalize_graph_layer(layer, relation).value
        self.conn.execute(
            "INSERT INTO mem_links(a, b, relation, layer, reason, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (a, b, relation, graph_layer, reason, now_ts()),
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
            "SELECT a, b, relation, layer, reason, created_at "
            "FROM mem_links WHERE a=? OR b=?",
            (memory_id, memory_id),
        ).fetchall()
        return [dict(r) for r in rows]

    def edges_in_scope(self, flt: Optional[SearchFilter] = None,
                       *, at: Optional[float] = None,
                       limit: Optional[int] = None) -> list[Edge]:
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
            sql += " AND (repo_id=? OR repo_id IS NULL)" if flt.include_ancestors else " AND repo_id=?"
            params.append(flt.repo_id)
        if flt and flt.graph_layers:
            marks = ",".join("?" for _ in flt.graph_layers)
            sql += f" AND layer IN ({marks})"
            params.extend(_enum(layer) for layer in flt.graph_layers)
        if limit is not None:
            sql += " LIMIT ?"
            params.append(max(0, int(limit)))
        rows = self.conn.execute(sql, params).fetchall()
        return [_row_to_edge(r) for r in rows]

    def links_among(self, ids: list[str], *,
                    layers: Optional[list[GraphLayer]] = None) -> list[dict]:
        """mem_links rows where *both* endpoints are in ``ids`` (for graph retrieval)."""
        if not ids:
            return []
        marks = ",".join("?" for _ in ids)
        sql = (
            f"SELECT a, b, relation, layer, reason FROM mem_links "
            f"WHERE a IN ({marks}) AND b IN ({marks})"
        )
        params: list[Any] = [*ids, *ids]
        if layers:
            layer_marks = ",".join("?" for _ in layers)
            sql += f" AND layer IN ({layer_marks})"
            params.extend(_enum(layer) for layer in layers)
        rows = self.conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def neighbors(self, node_ids: list[str], *, at: Optional[float] = None,
                  layers: Optional[list[GraphLayer]] = None) -> list[Edge]:
        if not node_ids:
            return []
        t = at if at is not None else now_ts()
        marks = ",".join("?" for _ in node_ids)
        sql = (
            f"SELECT * FROM edges WHERE (src IN ({marks}) OR dst IN ({marks})) "
            f"AND (valid_from IS NULL OR valid_from<=?) AND (valid_to IS NULL OR ?<valid_to) "
            f"AND expired_at IS NULL"
        )
        params: list[Any] = [*node_ids, *node_ids, t, t]
        if layers:
            layer_marks = ",".join("?" for _ in layers)
            sql += f" AND layer IN ({layer_marks})"
            params.extend(_enum(layer) for layer in layers)
        rows = self.conn.execute(sql, params).fetchall()
        return [_row_to_edge(r) for r in rows]

    # ── code symbol graph ────────────────────────────────────────────────────────
    def clear_symbols_for_file(self, repo_id: str, file: str, *,
                               commit: bool = True) -> None:
        """Re-indexing a file replaces its symbols/edges — incremental indexing is
        idempotent per file, not additive."""
        symbol_rows = self.conn.execute(
            "SELECT id FROM symbols WHERE repo_id=? AND file=?", (repo_id, file)
        ).fetchall()
        symbol_ids = [row["id"] for row in symbol_rows]
        if symbol_ids:
            marks = ",".join("?" for _ in symbol_ids)
            self.conn.execute(
                f"DELETE FROM code_memory_links WHERE repo_id=? "
                f"AND symbol_id IN ({marks})",
                (repo_id, *symbol_ids),
            )
        self.conn.execute("DELETE FROM symbols WHERE repo_id=? AND file=?", (repo_id, file))
        self.conn.execute("DELETE FROM code_edges WHERE repo_id=? AND file=?", (repo_id, file))
        if commit:
            self.conn.commit()

    def upsert_symbol(self, *, repo_id: str, kind: str, name: str, fqname: str, file: str,
                      span: str, signature: str = "", docstring: str = "",
                      lang: str = "", exported: bool = False,
                      content_hash: str = "", commit: bool = True) -> str:
        sid = ids.new_id("symbol")
        self.conn.execute(
            "INSERT INTO symbols(id, repo_id, kind, name, fqname, file, span, signature, "
            "docstring, lang, exported, content_hash, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, repo_id, kind, name, fqname, file, span, signature, docstring,
             lang, int(exported), content_hash, now_ts()),
        )
        if commit:
            self.conn.commit()
        return sid

    def add_code_edge(self, *, repo_id: str, src: str, dst: str, relation: str,
                      file: str = "", line: int = 0, layer: Optional[GraphLayer] = None,
                      commit: bool = True) -> str:
        eid = ids.new_id("edge")
        graph_layer = normalize_graph_layer(layer, relation)
        if graph_layer == GraphLayer.SEMANTIC:
            graph_layer = GraphLayer.ENTITY
        self.conn.execute(
            "INSERT INTO code_edges(id, repo_id, src, dst, relation, layer, file, line) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (eid, repo_id, src, dst, relation, graph_layer.value, file, line),
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
                        languages: Optional[set] = None) -> list[dict]:
        sql = "SELECT * FROM code_files WHERE repo_id=?"
        params: list[Any] = [repo_id]
        if languages:
            marks = ",".join("?" for _ in languages)
            sql += f" AND lang IN ({marks})"
            params.extend(sorted(languages))
        sql += " ORDER BY file"
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def upsert_code_file(self, *, repo_id: str, file: str, lang: str,
                         content_hash: str, size_bytes: int, mtime_ns: int,
                         backend: str, commit: bool = True) -> None:
        self.conn.execute(
            "INSERT INTO code_files(repo_id, file, lang, content_hash, size_bytes, "
            "mtime_ns, backend, indexed_at) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(repo_id, file) DO UPDATE SET "
            "lang=excluded.lang, content_hash=excluded.content_hash, "
            "size_bytes=excluded.size_bytes, mtime_ns=excluded.mtime_ns, "
            "backend=excluded.backend, indexed_at=excluded.indexed_at",
            (repo_id, file, lang, content_hash, int(size_bytes), int(mtime_ns),
             backend, now_ts()),
        )
        if commit:
            self.conn.commit()

    def remove_code_file(self, repo_id: str, file: str, *, commit: bool = True) -> None:
        self.clear_symbols_for_file(repo_id, file, commit=False)
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

    def list_symbols(self, repo_id: str, *, limit: Optional[int] = None) -> list[dict]:
        sql = "SELECT * FROM symbols WHERE repo_id=? ORDER BY file, fqname"
        params: list[Any] = [repo_id]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(max(0, int(limit)))  # never -1 == SQLite "unlimited"
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def list_code_edges(self, repo_id: str, *, limit: Optional[int] = None) -> list[dict]:
        sql = "SELECT * FROM code_edges WHERE repo_id=? ORDER BY file, line, id"
        params: list[Any] = [repo_id]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(max(0, int(limit)))  # never -1 == SQLite "unlimited"
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def symbols_for_files(self, repo_id: str, files: list[str]) -> list[dict]:
        if not files:
            return []
        marks = ",".join("?" for _ in files)
        rows = self.conn.execute(
            f"SELECT * FROM symbols WHERE repo_id=? AND file IN ({marks}) "
            "ORDER BY file, fqname",
            (repo_id, *files),
        ).fetchall()
        return [dict(r) for r in rows]

    def count_code_edges(self, repo_id: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM code_edges WHERE repo_id=?", (repo_id,)
        ).fetchone()
        return int(row["n"]) if row else 0

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

    def link_memory_symbol(self, *, repo_id: str, symbol_id: str, memory_id: str,
                           relation: str = "mentions", confidence: float = 1.0,
                           commit: bool = True) -> str:
        link_id = ids.new_id("edge")
        self.conn.execute(
            "INSERT OR IGNORE INTO code_memory_links("
            "id, repo_id, symbol_id, memory_id, relation, confidence, created_at"
            ") VALUES (?,?,?,?,?,?,?)",
            (link_id, repo_id, symbol_id, memory_id, relation,
             max(0.0, min(1.0, float(confidence))), now_ts()),
        )
        row = self.conn.execute(
            "SELECT id FROM code_memory_links WHERE repo_id=? AND symbol_id=? "
            "AND memory_id=? AND relation=?",
            (repo_id, symbol_id, memory_id, relation),
        ).fetchone()
        if commit:
            self.conn.commit()
        return row["id"] if row else link_id

    def clear_code_memory_links(self, repo_id: str, *, commit: bool = True) -> None:
        self.conn.execute("DELETE FROM code_memory_links WHERE repo_id=?", (repo_id,))
        if commit:
            self.conn.commit()

    def list_code_memory_links(self, repo_id: str, *,
                               limit: Optional[int] = None) -> list[dict]:
        sql = (
            "SELECT l.*, s.name, s.fqname, s.file, s.kind AS symbol_kind, "
            "m.title, m.mtype, m.valid_to, m.expired_at "
            "FROM code_memory_links l "
            "LEFT JOIN symbols s ON s.id=l.symbol_id "
            "LEFT JOIN memories m ON m.id=l.memory_id "
            "WHERE l.repo_id=? ORDER BY l.created_at, l.id"
        )
        params: list[Any] = [repo_id]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(max(0, int(limit)))  # never -1 == SQLite "unlimited"
        rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def memories_for_symbol(self, repo_id: str, symbol_id: str, *,
                            limit: int = 20) -> list[dict]:
        rows = self.conn.execute(
            "SELECT m.id, m.title, m.content, m.mtype, m.scope, m.importance, "
            "m.provenance, l.relation, l.confidence "
            "FROM code_memory_links l JOIN memories m ON m.id=l.memory_id "
            "WHERE l.repo_id=? AND l.symbol_id=? "
            "AND (m.valid_to IS NULL OR ?<m.valid_to) AND m.expired_at IS NULL "
            "ORDER BY l.confidence DESC, m.importance DESC, m.ingested_at DESC LIMIT ?",
            (repo_id, symbol_id, now_ts(), max(1, min(100, int(limit)))),
        ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["provenance"] = _loads(item.get("provenance"), {})
            out.append(item)
        return out

    def symbols_for_memory(self, repo_id: str, memory_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT s.*, l.relation, l.confidence FROM code_memory_links l "
            "JOIN symbols s ON s.id=l.symbol_id "
            "WHERE l.repo_id=? AND l.memory_id=? ORDER BY l.confidence DESC, s.fqname",
            (repo_id, memory_id),
        ).fetchall()
        return [dict(row) for row in rows]

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

    def audit(self, actor: str, action: str, target: str, detail: str = "",
              *, commit: bool = True) -> None:
        self.conn.execute(
            "INSERT INTO audit(id, ts, actor, action, target, detail) VALUES (?,?,?,?,?,?)",
            (ids.new_id("audit"), now_ts(), actor, action, target, detail),
        )
        if commit:
            self.conn.commit()

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
        operation = str(operation or "unknown")[:80]
        actor = str(actor or "system")[:200]
        workspace_id = str(workspace_id or "")
        repo_id = str(repo_id or "")
        with self._receipt_lock:
            # The Python lock serializes threads sharing this Store. BEGIN IMMEDIATE also
            # serializes separate Store/process connections before predecessor selection,
            # preventing two Team workers from forking the same workspace chain.
            transaction_started = False
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                transaction_started = True
                ts = now_ts()
                receipt_id = ids.new_id("receipt")
                scope_digest = hashlib.sha256(
                    f"{workspace_id}\0{repo_id}".encode("utf-8")
                ).hexdigest()[:24]
                actor_digest = hashlib.sha256(actor.encode("utf-8")).hexdigest()[:16]
                chain = self.conn.execute(
                    "SELECT COUNT(*) AS n, "
                    "COALESCE((SELECT receipt_hash FROM operation_receipts "
                    "WHERE workspace_id=? ORDER BY rowid DESC LIMIT 1), '') AS head "
                    "FROM operation_receipts WHERE workspace_id=?",
                    (workspace_id, workspace_id),
                ).fetchone()
                current_count = int(chain["n"] or 0)
                prev_hash = str(chain["head"] or "")
                anchor = self.conn.execute(
                    "SELECT receipt_count, head_hash, integrity_error "
                    "FROM receipt_chain_heads "
                    "WHERE workspace_id=?",
                    (workspace_id,),
                ).fetchone()
                anchor_error = str(anchor["integrity_error"] or "") if anchor else ""
                if anchor is not None and (
                    int(anchor["receipt_count"]) != current_count
                    or str(anchor["head_hash"]) != prev_hash
                ):
                    # Preserve evidence of the mismatch without bricking the operation that
                    # requested this receipt. The new receipt continues from the rows that
                    # actually remain, while verification stays invalid until an explicit
                    # repair/export decision clears the persistent integrity marker.
                    anchor_error = anchor_error or "pre_append_anchor_mismatch"
                safe_meta = _receipt_metadata(metadata or {})
                payload_obj = {
                    "version": 1,
                    "id": receipt_id,
                    "ts_ms": int(ts * 1000),
                    "operation": operation,
                    "scope_digest": scope_digest,
                    "actor_digest": actor_digest,
                    "target_count": max(0, int(target_count)),
                    "status": str(status or "ok")[:40],
                    "metadata": safe_meta,
                    "prev_hash": prev_hash,
                }
                payload = json.dumps(
                    payload_obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                )
                receipt_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
                self.conn.execute(
                    "INSERT INTO operation_receipts(id, ts, operation, workspace_id, repo_id, "
                    "scope_digest, actor, target_count, status, payload, prev_hash, "
                    "receipt_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        receipt_id, ts, operation, workspace_id, repo_id, scope_digest,
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
                self.conn.commit()
                return {**payload_obj, "hash": receipt_hash}
            except Exception:
                if transaction_started:
                    self.conn.rollback()
                raise

    def list_receipts(self, *, workspace_id: str, limit: int = 100) -> list[dict]:
        rows = self.conn.execute(
            "SELECT payload, receipt_hash FROM operation_receipts WHERE workspace_id=? "
            "ORDER BY rowid DESC LIMIT ?",
            (workspace_id, max(1, min(10_000, int(limit)))),
        ).fetchall()
        out = []
        for row in rows:
            payload = _loads(row["payload"], {})
            if isinstance(payload, dict):
                payload["hash"] = row["receipt_hash"]
                out.append(payload)
        return out

    def verify_receipts(self, *, workspace_id: str, expected_head: str = "",
                        expected_count: Optional[int] = None) -> dict:
        rows = self.conn.execute(
            "SELECT id, payload, prev_hash, receipt_hash FROM operation_receipts "
            "WHERE workspace_id=? ORDER BY rowid ASC",
            (workspace_id,),
        ).fetchall()
        previous = ""
        errors: list[dict] = []
        for index, row in enumerate(rows):
            actual = hashlib.sha256(row["payload"].encode("utf-8")).hexdigest()
            if actual != row["receipt_hash"]:
                errors.append({"index": index, "id": row["id"], "error": "hash_mismatch"})
            payload = _loads(row["payload"], {})
            if not isinstance(payload, dict) or payload.get("id") != row["id"] \
                    or payload.get("prev_hash") != row["prev_hash"]:
                errors.append({"index": index, "id": row["id"],
                               "error": "payload_mismatch"})
            if row["prev_hash"] != previous:
                errors.append({"index": index, "id": row["id"], "error": "chain_break"})
            previous = row["receipt_hash"]
        anchor = self.conn.execute(
            "SELECT receipt_count, head_hash, integrity_error "
            "FROM receipt_chain_heads WHERE workspace_id=?",
            (workspace_id,),
        ).fetchone()
        if rows and anchor is None:
            errors.append({"index": len(rows), "id": "", "error": "missing_anchor"})
        elif anchor is not None:
            if int(anchor["receipt_count"]) != len(rows):
                errors.append({
                    "index": len(rows), "id": "", "error": "anchor_count_mismatch",
                })
            if str(anchor["head_hash"]) != previous:
                errors.append({
                    "index": len(rows), "id": "", "error": "anchor_head_mismatch",
                })
            if str(anchor["integrity_error"] or ""):
                errors.append({
                    "index": len(rows), "id": "", "error": "anchor_integrity_error",
                })
        expected_head = str(expected_head or "").strip()
        if expected_head and previous != expected_head:
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
            "head": previous,
            "anchored": anchor is not None,
            "errors": errors,
        }

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
        layer=normalize_graph_layer(
            row["layer"] if "layer" in row.keys() else None, row["relation"]
        ),
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
