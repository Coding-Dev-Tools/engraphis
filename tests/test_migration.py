import sqlite3

import numpy as np

from engraphis.core.interfaces import MemoryType
from engraphis.core.store import Store
from scripts.migrate_to_v2 import migrate


def _build_v1_db(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE memories (
            id INTEGER PRIMARY KEY, namespace TEXT, document_id TEXT, title TEXT, content TEXT,
            metadata TEXT, source_type TEXT, priority TEXT, vector BLOB, created_at REAL,
            updated_at REAL, last_access REAL, access_count INTEGER, stability REAL,
            surprise REAL, memory_type TEXT
        );
        CREATE TABLE entities (id INTEGER PRIMARY KEY, namespace TEXT, name TEXT,
            entity_type TEXT, created_at REAL);
        CREATE TABLE edges (id INTEGER PRIMARY KEY, namespace TEXT, source_entity TEXT,
            target_entity TEXT, relation TEXT, weight REAL, created_at REAL, updated_at REAL);
        CREATE TABLE thoughts (id INTEGER PRIMARY KEY, namespace TEXT, content TEXT,
            source_memory_ids TEXT, created_at REAL);
        """
    )
    vec = np.random.rand(384).astype(np.float32).tobytes()
    conn.execute(
        "INSERT INTO memories (namespace, document_id, title, content, metadata, vector, "
        "created_at, updated_at, last_access, access_count, stability, surprise, memory_type) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("preferences", "pref-1", "theme", "User prefers dark mode.", '{"tags":["ui"]}', vec,
         1000.0, 1000.0, 1000.0, 3, 2.0, 1.0, "semantic"),
    )
    conn.execute(
        "INSERT INTO memories (namespace, document_id, title, content, metadata, created_at, "
        "memory_type) VALUES (?,?,?,?,?,?,?)",
        ("infra", "infra-1", "db", "Staging runs PostgreSQL 16.", "{}", 1001.0, "episodic"),
    )
    conn.execute("INSERT INTO entities (namespace, name, entity_type, created_at) VALUES (?,?,?,?)",
                 ("infra", "PostgreSQL", "tech", 1001.0))
    conn.execute("INSERT INTO edges (namespace, source_entity, target_entity, relation, weight, "
                 "created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                 ("infra", "staging", "PostgreSQL", "uses", 1.0, 1001.0, 1001.0))
    conn.execute("INSERT INTO thoughts (namespace, content, source_memory_ids, created_at) "
                 "VALUES (?,?,?,?)", ("preferences", "User cares about UI polish.", "[]", 1002.0))
    conn.commit()
    conn.close()


def test_migration_dry_run_counts(tmp_path):
    old = tmp_path / "neocortex.db"
    _build_v1_db(str(old))
    counts = migrate(str(old), str(tmp_path / "x.db"), dry_run=True)
    assert counts["memories"] == 2
    assert counts["repos"] == 2          # two namespaces -> two repos
    assert counts["entities"] == 1
    assert counts["edges"] == 1
    assert counts["thoughts"] == 1


def test_migration_writes_scoped_v2(tmp_path):
    old = tmp_path / "neocortex.db"
    new = tmp_path / "engraphis_v2.db"
    _build_v1_db(str(old))
    migrate(str(old), str(new))

    store = Store(str(new))
    # default workspace exists, two repos created from namespaces
    assert store.get_or_create_workspace("default")
    repos = {r["name"] for r in store.conn.execute("SELECT name FROM repos").fetchall()}
    assert {"preferences", "infra"} <= repos
    # 2 migrated memories + 1 thought-as-memory = 3
    mems = store.list_memories(include_invalid=True)
    assert len(mems) == 3
    assert any(m.mtype == MemoryType.SEMANTIC and "UI polish" in m.content for m in mems)
    # provenance preserved
    assert any(m.provenance.get("v1_namespace") == "preferences" for m in mems)
    # vector carried across for the row that had one
    vrows = store.conn.execute("SELECT COUNT(*) AS c FROM mem_vectors").fetchone()["c"]
    assert vrows >= 1
    store.close()
