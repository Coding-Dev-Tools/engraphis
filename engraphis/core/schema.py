"""Engraphis v2 schema.

The scoped, bi-temporal, code-aware schema that replaces the flat-namespace v1
tables. Vectors live in ``mem_vectors`` (BLOB) for the Phase-0 NumPy reference
index; Phase 1 swaps this for a ``sqlite-vec`` virtual table behind the same
``VectorIndex`` interface. Full-text lives in ``mem_fts`` (FTS5 when available,
with a plain-table fallback so the schema initializes on any SQLite build).
"""
from __future__ import annotations

SCHEMA_VERSION = 2

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
    weight       REAL DEFAULT 1.0,
    valid_from   REAL,
    valid_to     REAL,
    ingested_at  REAL,
    expired_at   REAL,
    provenance   TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_edge_src ON edges(workspace_id, src, valid_to, expired_at);
CREATE INDEX IF NOT EXISTS idx_edge_dst ON edges(workspace_id, dst);

CREATE TABLE IF NOT EXISTS mem_links (
    a          TEXT,
    b          TEXT,
    relation   TEXT,
    created_at REAL
);

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
    file     TEXT,
    line     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_code_edge_src ON code_edges(repo_id, src);

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
