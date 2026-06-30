"""SQLite-backed storage layer — schema, connection, vector serialization."""
from __future__ import annotations

import json
import sqlite3
import struct
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np

from engraphis.config import settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    namespace       TEXT    NOT NULL,
    document_id     TEXT    NOT NULL,
    title           TEXT    NOT NULL DEFAULT '',
    content         TEXT    NOT NULL,
    metadata        TEXT    NOT NULL DEFAULT '{}',
    source_type     TEXT    DEFAULT NULL,
    priority        TEXT    DEFAULT NULL,
    vector          BLOB    DEFAULT NULL,
    created_at      REAL    NOT NULL,
    updated_at      REAL    NOT NULL,
    last_access     REAL    NOT NULL,
    access_count    INTEGER NOT NULL DEFAULT 0,
    stability       REAL    NOT NULL DEFAULT 1.0,
    surprise        REAL    NOT NULL DEFAULT 1.0,
    memory_type     TEXT    NOT NULL DEFAULT 'semantic',
    UNIQUE(namespace, document_id)
);
CREATE INDEX IF NOT EXISTS idx_mem_ns      ON memories(namespace);
CREATE INDEX IF NOT EXISTS idx_mem_doc     ON memories(document_id);
CREATE INDEX IF NOT EXISTS idx_mem_updated ON memories(namespace, updated_at);
CREATE INDEX IF NOT EXISTS idx_mem_type    ON memories(namespace, memory_type);

CREATE TABLE IF NOT EXISTS chunks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id   INTEGER NOT NULL,
    namespace   TEXT    NOT NULL,
    chunk_idx   INTEGER NOT NULL,
    content     TEXT    NOT NULL,
    vector      BLOB    NOT NULL,
    created_at  REAL    NOT NULL,
    FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_chunks_mem ON chunks(memory_id);
CREATE INDEX IF NOT EXISTS idx_chunks_ns  ON chunks(namespace);

CREATE TABLE IF NOT EXISTS entities (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    namespace   TEXT    NOT NULL,
    name        TEXT    NOT NULL,
    entity_type TEXT    DEFAULT NULL,
    created_at  REAL    NOT NULL,
    UNIQUE(namespace, name)
);
CREATE INDEX IF NOT EXISTS idx_ent_ns ON entities(namespace);

CREATE TABLE IF NOT EXISTS edges (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    namespace     TEXT    NOT NULL,
    source_entity TEXT    NOT NULL,
    target_entity TEXT    NOT NULL,
    relation      TEXT    NOT NULL,
    weight        REAL    NOT NULL DEFAULT 1.0,
    created_at    REAL    NOT NULL,
    updated_at    REAL    NOT NULL,
    UNIQUE(namespace, source_entity, target_entity, relation)
);
CREATE INDEX IF NOT EXISTS idx_edge_src ON edges(namespace, source_entity);
CREATE INDEX IF NOT EXISTS idx_edge_tgt ON edges(namespace, target_entity);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    namespace   TEXT    NOT NULL,
    entity_name TEXT    NOT NULL,
    event_type  TEXT    NOT NULL,
    description TEXT    DEFAULT NULL,
    payload     TEXT    NOT NULL DEFAULT '{}',
    timestamp   REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evt_ns   ON events(namespace);
CREATE INDEX IF NOT EXISTS idx_evt_time ON events(namespace, timestamp);
CREATE INDEX IF NOT EXISTS idx_evt_ent  ON events(namespace, entity_name);

CREATE TABLE IF NOT EXISTS interactions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    namespace        TEXT    NOT NULL,
    entity_name      TEXT    NOT NULL,
    interaction_level TEXT   DEFAULT NULL,
    description      TEXT    DEFAULT NULL,
    timestamp        REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_int_ns ON interactions(namespace);

CREATE TABLE IF NOT EXISTS thoughts (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    namespace         TEXT    NOT NULL,
    content           TEXT    NOT NULL,
    source_memory_ids TEXT    NOT NULL DEFAULT '[]',
    created_at        REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_thoughts_ns ON thoughts(namespace);

CREATE TABLE IF NOT EXISTS jobs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id     TEXT    NOT NULL UNIQUE,
    namespace  TEXT    DEFAULT NULL,
    job_type   TEXT    NOT NULL,
    state      TEXT    NOT NULL DEFAULT 'pending',
    payload    TEXT    NOT NULL DEFAULT '{}',
    created_at REAL    NOT NULL,
    updated_at REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state);

CREATE TABLE IF NOT EXISTS vaults (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    namespace    TEXT    NOT NULL UNIQUE,
    name         TEXT    NOT NULL,
    description  TEXT    DEFAULT '',
    color        TEXT    DEFAULT '#9d7cf6',
    memory_type  TEXT    DEFAULT 'semantic',
    is_active    INTEGER NOT NULL DEFAULT 0,
    created_at   REAL    NOT NULL,
    updated_at   REAL    NOT NULL
);
"""

_local = threading.local()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.db_path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def get_conn() -> sqlite3.Connection:
    """Thread-local connection (FastAPI runs handlers in a threadpool)."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = _connect()
        _local.conn = conn
    return conn


def init_db() -> None:
    """Create all tables if they don't exist."""
    Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = get_conn()
    conn.executescript(_SCHEMA)
    conn.commit()
    # Ensure a default vault exists
    from engraphis.stores.vaults import ensure_default_vault
    ensure_default_vault()


# ── Vector serialization helpers ────────────────────────────────────────────

def vector_to_blob(vec: np.ndarray) -> bytes:
    """Serialize a float32 numpy vector to a compact BLOB."""
    return vec.astype(np.float32).tobytes()


def blob_to_vector(blob: bytes) -> np.ndarray:
    """Deserialize a BLOB back to a float32 numpy vector."""
    return np.frombuffer(blob, dtype=np.float32).copy()


# ── JSON helpers ────────────────────────────────────────────────────────────

def _json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _json_loads(raw: str, default: Any = None) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def now_ts() -> float:
    return time.time()
