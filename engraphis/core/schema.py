"""Engraphis v2 schema.

The scoped, bi-temporal, code-aware schema that replaces the flat-namespace v1
tables. Vectors live in ``mem_vectors`` (BLOB) for the Phase-0 NumPy reference
index; Phase 1 swaps this for a ``sqlite-vec`` virtual table behind the same
``VectorIndex`` interface. Full-text lives in ``mem_fts`` (FTS5 when available,
with a plain-table fallback so the schema initializes on any SQLite build).
"""
from __future__ import annotations

SCHEMA_VERSION = 4

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    INTEGER PRIMARY KEY,
    applied_at REAL
);

-- ── Tenancy & structure ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS workspaces (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    created_at REAL,
    settings   TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS repos (
    id           TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    root_path    TEXT,
    vcs_remote   TEXT,
    primary_lang TEXT,
    created_at   REAL,
    indexed_at   REAL,
    settings     TEXT DEFAULT '{}',
    UNIQUE(workspace_id, name)
);

CREATE TABLE IF NOT EXISTS sessions (
    id           TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    repo_id      TEXT,
    agent        TEXT,
    user_id      TEXT,
    goal         TEXT,
    status       TEXT DEFAULT 'open',          -- open|active|summarized|consolidated
    started_at   REAL,
    ended_at     REAL,
    summary      TEXT,
    open_threads TEXT DEFAULT '[]',
    outcome      TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_repo ON sessions(workspace_id, repo_id, status);

-- ── Memories (the atomic note) ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS memories (
    id           TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    repo_id      TEXT,
    session_id   TEXT,
    scope        TEXT NOT NULL DEFAULT 'repo',     -- session|repo|workspace|user
    mtype        TEXT NOT NULL DEFAULT 'semantic', -- working|episodic|semantic|procedural
    title        TEXT DEFAULT '',
    content      TEXT NOT NULL,
    summary      TEXT DEFAULT '',
    keywords     TEXT DEFAULT '[]',
    metadata     TEXT DEFAULT '{}',
    importance   REAL DEFAULT 0.0,
    surprise     REAL DEFAULT 1.0,
    stability    REAL DEFAULT 1.0,
    access_count INTEGER DEFAULT 0,
    last_access  REAL,
    valid_from   REAL,                             -- world-time validity
    valid_to     REAL,
    ingested_at  REAL,                             -- system-time validity
    expired_at   REAL,
    pinned       INTEGER DEFAULT 0,
    sensitivity  TEXT DEFAULT 'normal',
    provenance   TEXT DEFAULT '{}',
    sort_order   REAL                              -- manual drag-to-reorder position (dashboard); NULL = unordered
);
CREATE INDEX IF NOT EXISTS idx_mem_scope   ON memories(workspace_id, repo_id, scope, mtype);
CREATE INDEX IF NOT EXISTS idx_mem_session ON memories(session_id);
CREATE INDEX IF NOT EXISTS idx_mem_valid   ON memories(valid_from, valid_to, expired_at);

-- Vectors (Phase 0 reference store; Phase 1 → sqlite-vec vec0 virtual table).
CREATE TABLE IF NOT EXISTS mem_vectors (
    id     TEXT PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
    dim    INTEGER NOT NULL,
    vector BLOB    NOT NULL,
    model  TEXT
);

-- ── Knowledge graph (bi-temporal) ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS entities (
    id           TEXT PRIMARY KEY,
    workspace_id TEXT,
    repo_id      TEXT,
    name         TEXT,
    etype        TEXT,
    canonical_id TEXT,                              -- cross-repo entity resolution
    normalized_name TEXT NOT NULL DEFAULT '',
    canonical_method TEXT NOT NULL DEFAULT 'exact',
    canonical_confidence REAL NOT NULL DEFAULT 1.0,
    created_at   REAL,
    UNIQUE(workspace_id, repo_id, name, etype)
);

CREATE TABLE IF NOT EXISTS edges (
    id           TEXT PRIMARY KEY,
    workspace_id TEXT,
    repo_id      TEXT,
    src          TEXT NOT NULL,
    dst          TEXT NOT NULL,
    relation     TEXT NOT NULL,
    layer        TEXT DEFAULT 'semantic',
    weight       REAL DEFAULT 1.0,
    valid_from   REAL,
    valid_to     REAL,
    ingested_at  REAL,
    expired_at   REAL,
    provenance   TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_edge_src ON edges(workspace_id, src, valid_to, expired_at);
CREATE INDEX IF NOT EXISTS idx_edge_dst ON edges(workspace_id, dst);
-- Store.edges_in_scope() (the PPR retrieval arm) filters workspace_id + repo_id + the
-- bi-temporal window; the two indexes above lead on workspace_id but then key on src/dst,
-- so a repo-scoped graph read had to scan the whole workspace. Also bounds the
-- workspace-scoped candidate scan in Store.invalidate_edges_for_memory().
CREATE INDEX IF NOT EXISTS idx_edge_workspace_repo
    ON edges(workspace_id, repo_id, valid_to, expired_at);

-- Evidence is normalized into an indexed, bi-temporal table. ``edges.provenance``
-- remains populated for one compatibility release and for legacy exports.
CREATE TABLE IF NOT EXISTS edge_supports (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    edge_id      TEXT NOT NULL,
    memory_id    TEXT NOT NULL,
    source_kind  TEXT NOT NULL DEFAULT 'legacy_unknown',
    confidence   REAL NOT NULL DEFAULT 0.5,
    valid_from   REAL,
    valid_to     REAL,
    ingested_at  REAL,
    expired_at   REAL,
    provenance   TEXT DEFAULT '{}',
    FOREIGN KEY(edge_id) REFERENCES edges(id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_edge_support_live_unique
    ON edge_supports(edge_id, memory_id, source_kind)
    WHERE valid_to IS NULL AND expired_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_edge_support_edge
    ON edge_supports(edge_id, valid_to, expired_at);
CREATE INDEX IF NOT EXISTS idx_edge_support_memory
    ON edge_supports(memory_id, valid_to, expired_at);

-- Explicit, persisted derived-index work. Graph reads never backfill implicitly;
-- writers run one of these bounded jobs and readers receive a rebuilding state while
-- a mutating job is active. ``jobs`` is generic enough for later v2 maintenance jobs,
-- while ``graph_index_state`` is the cheap generation/state lookup used by scene caches.
CREATE TABLE IF NOT EXISTS jobs (
    id               TEXT PRIMARY KEY,
    workspace_id     TEXT NOT NULL,
    repo_id          TEXT,
    kind             TEXT NOT NULL,
    state            TEXT NOT NULL DEFAULT 'queued',
    dry_run          INTEGER NOT NULL DEFAULT 1,
    total_items      INTEGER NOT NULL DEFAULT 0,
    processed_items  INTEGER NOT NULL DEFAULT 0,
    counts           TEXT NOT NULL DEFAULT '{}',
    errors           TEXT NOT NULL DEFAULT '[]',
    request          TEXT NOT NULL DEFAULT '{}',
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    runner_id        TEXT,
    heartbeat_at     REAL,
    created_at       REAL NOT NULL,
    started_at       REAL,
    finished_at      REAL
);
CREATE INDEX IF NOT EXISTS idx_jobs_scope_state
    ON jobs(workspace_id, kind, state, created_at);

CREATE TABLE IF NOT EXISTS graph_index_state (
    workspace_id  TEXT PRIMARY KEY,
    generation    INTEGER NOT NULL DEFAULT 1,
    state         TEXT NOT NULL DEFAULT 'ready',
    active_job_id TEXT,
    updated_at    REAL NOT NULL,
    last_error    TEXT NOT NULL DEFAULT ''
);

CREATE TRIGGER IF NOT EXISTS trg_graph_generation_entity_insert
AFTER INSERT ON entities WHEN NEW.workspace_id IS NOT NULL BEGIN
    INSERT INTO graph_index_state(workspace_id, generation, state, updated_at)
    VALUES(NEW.workspace_id, 1, 'ready', CAST(strftime('%s','now') AS REAL))
    ON CONFLICT(workspace_id) DO UPDATE SET
        generation=graph_index_state.generation+1, updated_at=excluded.updated_at;
END;
CREATE TRIGGER IF NOT EXISTS trg_graph_generation_entity_update
AFTER UPDATE ON entities WHEN NEW.workspace_id IS NOT NULL BEGIN
    INSERT INTO graph_index_state(workspace_id, generation, state, updated_at)
    VALUES(NEW.workspace_id, 1, 'ready', CAST(strftime('%s','now') AS REAL))
    ON CONFLICT(workspace_id) DO UPDATE SET
        generation=graph_index_state.generation+1, updated_at=excluded.updated_at;
END;
CREATE TRIGGER IF NOT EXISTS trg_graph_generation_entity_delete
AFTER DELETE ON entities WHEN OLD.workspace_id IS NOT NULL BEGIN
    INSERT INTO graph_index_state(workspace_id, generation, state, updated_at)
    VALUES(OLD.workspace_id, 1, 'ready', CAST(strftime('%s','now') AS REAL))
    ON CONFLICT(workspace_id) DO UPDATE SET
        generation=graph_index_state.generation+1, updated_at=excluded.updated_at;
END;
CREATE TRIGGER IF NOT EXISTS trg_graph_generation_edge_insert
AFTER INSERT ON edges WHEN NEW.workspace_id IS NOT NULL BEGIN
    INSERT INTO graph_index_state(workspace_id, generation, state, updated_at)
    VALUES(NEW.workspace_id, 1, 'ready', CAST(strftime('%s','now') AS REAL))
    ON CONFLICT(workspace_id) DO UPDATE SET
        generation=graph_index_state.generation+1, updated_at=excluded.updated_at;
END;
CREATE TRIGGER IF NOT EXISTS trg_graph_generation_edge_update
AFTER UPDATE ON edges WHEN NEW.workspace_id IS NOT NULL BEGIN
    INSERT INTO graph_index_state(workspace_id, generation, state, updated_at)
    VALUES(NEW.workspace_id, 1, 'ready', CAST(strftime('%s','now') AS REAL))
    ON CONFLICT(workspace_id) DO UPDATE SET
        generation=graph_index_state.generation+1, updated_at=excluded.updated_at;
END;
CREATE TRIGGER IF NOT EXISTS trg_graph_generation_edge_delete
AFTER DELETE ON edges WHEN OLD.workspace_id IS NOT NULL BEGIN
    INSERT INTO graph_index_state(workspace_id, generation, state, updated_at)
    VALUES(OLD.workspace_id, 1, 'ready', CAST(strftime('%s','now') AS REAL))
    ON CONFLICT(workspace_id) DO UPDATE SET
        generation=graph_index_state.generation+1, updated_at=excluded.updated_at;
END;
CREATE TRIGGER IF NOT EXISTS trg_graph_generation_support_insert
AFTER INSERT ON edge_supports BEGIN
    INSERT INTO graph_index_state(workspace_id, generation, state, updated_at)
    SELECT workspace_id, 1, 'ready', CAST(strftime('%s','now') AS REAL)
    FROM edges WHERE id=NEW.edge_id
    ON CONFLICT(workspace_id) DO UPDATE SET
        generation=graph_index_state.generation+1, updated_at=excluded.updated_at;
END;
CREATE TRIGGER IF NOT EXISTS trg_graph_generation_support_update
AFTER UPDATE ON edge_supports BEGIN
    INSERT INTO graph_index_state(workspace_id, generation, state, updated_at)
    SELECT workspace_id, 1, 'ready', CAST(strftime('%s','now') AS REAL)
    FROM edges WHERE id=NEW.edge_id
    ON CONFLICT(workspace_id) DO UPDATE SET
        generation=graph_index_state.generation+1, updated_at=excluded.updated_at;
END;
CREATE TRIGGER IF NOT EXISTS trg_graph_generation_support_delete
AFTER DELETE ON edge_supports BEGIN
    INSERT INTO graph_index_state(workspace_id, generation, state, updated_at)
    SELECT workspace_id, 1, 'ready', CAST(strftime('%s','now') AS REAL)
    FROM edges WHERE id=OLD.edge_id
    ON CONFLICT(workspace_id) DO UPDATE SET
        generation=graph_index_state.generation+1, updated_at=excluded.updated_at;
END;

CREATE TABLE IF NOT EXISTS mem_links (
    a          TEXT,
    b          TEXT,
    relation   TEXT,
    layer      TEXT DEFAULT 'semantic',
    reason     TEXT DEFAULT '',
    created_at REAL
);
CREATE INDEX IF NOT EXISTS idx_mem_links_ab ON mem_links(a, b);
-- Links are undirected: Store.get_links()/has_link()/add_link() all match "a=? OR b=?".
-- idx_mem_links_ab only serves the `a` branch, so the `b` branch was a full table scan.
CREATE INDEX IF NOT EXISTS idx_mem_links_b ON mem_links(b);

-- ── Code symbol graph ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS symbols (
    id            TEXT PRIMARY KEY,
    repo_id       TEXT NOT NULL,
    kind          TEXT,
    name          TEXT,
    fqname        TEXT,
    file          TEXT,
    span          TEXT,
    signature     TEXT,
    docstring     TEXT DEFAULT '',
    lang          TEXT,
    exported      INTEGER,
    content_hash  TEXT,
    embedding_ref TEXT,
    updated_at    REAL
);
CREATE INDEX IF NOT EXISTS idx_sym_repo ON symbols(repo_id, name);

CREATE TABLE IF NOT EXISTS code_edges (
    id       TEXT PRIMARY KEY,
    repo_id  TEXT,
    src      TEXT,
    dst      TEXT,
    relation TEXT,                                  -- calls|imports|references|implements|tests
    layer    TEXT DEFAULT 'entity',
    file     TEXT,
    line     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_code_edge_src ON code_edges(repo_id, src);
CREATE INDEX IF NOT EXISTS idx_code_edge_dst ON code_edges(repo_id, dst);

CREATE TABLE IF NOT EXISTS code_files (
    repo_id       TEXT NOT NULL,
    file          TEXT NOT NULL,
    lang          TEXT,
    content_hash  TEXT NOT NULL,
    size_bytes    INTEGER DEFAULT 0,
    mtime_ns      INTEGER DEFAULT 0,
    backend       TEXT DEFAULT '',
    indexed_at    REAL,
    PRIMARY KEY(repo_id, file)
);
CREATE INDEX IF NOT EXISTS idx_code_files_lang ON code_files(repo_id, lang);

CREATE TABLE IF NOT EXISTS code_memory_links (
    id          TEXT PRIMARY KEY,
    repo_id     TEXT NOT NULL,
    symbol_id   TEXT NOT NULL,
    memory_id   TEXT NOT NULL,
    relation    TEXT DEFAULT 'mentions',
    confidence  REAL DEFAULT 1.0,
    created_at  REAL,
    UNIQUE(repo_id, symbol_id, memory_id, relation)
);
CREATE INDEX IF NOT EXISTS idx_code_mem_symbol
    ON code_memory_links(repo_id, symbol_id);
CREATE INDEX IF NOT EXISTS idx_code_mem_memory
    ON code_memory_links(repo_id, memory_id);

-- ── Event ledger & audit ───────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS events (
    id               TEXT PRIMARY KEY,
    workspace_id     TEXT,
    repo_id          TEXT,
    session_id       TEXT,
    kind             TEXT,
    content          TEXT,
    refs             TEXT DEFAULT '[]',
    interaction_level TEXT,
    ts               REAL
);
CREATE INDEX IF NOT EXISTS idx_evt_session ON events(session_id, ts);

CREATE TABLE IF NOT EXISTS audit (
    id     TEXT PRIMARY KEY,
    ts     REAL,
    actor  TEXT,
    action TEXT,
    target TEXT,
    detail TEXT
);
-- Every audit read is keyed on target and ordered by ts: MemoryService.inspect() and
-- _chain_entry() ("WHERE target=? ORDER BY ts"), audit_log()/export()/analytics
-- ("JOIN memories m ON m.id = a.target"). The table had no index at all, so each of
-- those was a full scan that grows without bound as the audit trail accumulates.
CREATE INDEX IF NOT EXISTS idx_audit_target ON audit(target, ts);

CREATE TABLE IF NOT EXISTS operation_receipts (
    id             TEXT PRIMARY KEY,
    ts             REAL NOT NULL,
    operation      TEXT NOT NULL,
    workspace_id   TEXT,
    repo_id        TEXT,
    scope_digest   TEXT NOT NULL,
    actor          TEXT DEFAULT 'system',
    target_count   INTEGER DEFAULT 0,
    status         TEXT DEFAULT 'ok',
    payload        TEXT NOT NULL,
    prev_hash      TEXT DEFAULT '',
    receipt_hash   TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_receipt_scope
    ON operation_receipts(workspace_id, ts, id);
CREATE INDEX IF NOT EXISTS idx_receipt_operation
    ON operation_receipts(operation, ts);

-- Independent chain anchor maintained atomically with each receipt. It detects tail
-- truncation (which predecessor hashes alone cannot detect without an expected head).
CREATE TABLE IF NOT EXISTS receipt_chain_heads (
    workspace_id  TEXT PRIMARY KEY,
    receipt_count INTEGER NOT NULL,
    head_hash     TEXT NOT NULL,
    integrity_error TEXT NOT NULL DEFAULT '',
    updated_at    REAL NOT NULL
);

-- ── Sync state (device identity + per-peer cursors) ─────────────────────────
-- Additive, local-only bookkeeping for the cloud-sync layer (core/sync.py).
-- A tiny KV: 'device_id' (this database's stable origin id) and, later,
-- 'peer:<remote>:cursor' high-water marks. Never part of a sync bundle's payload;
-- device identity is metadata, not memory.
CREATE TABLE IF NOT EXISTS sync_state (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at REAL
);
"""

# FTS5 if available, else a plain fallback table with the same columns.
FTS_SQL_FTS5 = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS mem_fts "
    "USING fts5(id UNINDEXED, title, content, keywords);"
)
FTS_SQL_FALLBACK = (
    "CREATE TABLE IF NOT EXISTS mem_fts "
    "(id TEXT PRIMARY KEY, title TEXT, content TEXT, keywords TEXT);"
)
