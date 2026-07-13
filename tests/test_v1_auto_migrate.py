"""MemoryService.create() auto-migrates a pre-existing v1-shaped database.

Regression coverage for the 2026-07-13 production incident: a self-host whose
ENGRAPHIS_DB_PATH already held a v1 database (created by ``engraphis-server``) crashed
every time it started ``engraphis-dashboard`` (v2) against that same path, because
``Store.init_schema()`` runs ``CREATE INDEX ... ON memories(workspace_id, ...)``
unconditionally and v1's ``memories`` table has no ``workspace_id`` column.
``MemoryService.create()`` now detects this shape up front and migrates in place
(engraphis.service._auto_migrate_v1_if_needed) before ``Store`` ever touches the file.
"""
import sqlite3

import numpy as np

from engraphis.core.store import Store
from engraphis.service import MemoryService, _auto_migrate_v1_if_needed


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
        """
    )
    vec = np.random.rand(384).astype(np.float32).tobytes()
    conn.execute(
        "INSERT INTO memories (namespace, document_id, title, content, metadata, vector, "
        "created_at, updated_at, last_access, access_count, stability, surprise, memory_type) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("preferences", "pref-1", "theme", "User prefers dark mode.", "{}", vec,
         1000.0, 1000.0, 1000.0, 3, 2.0, 1.0, "semantic"),
    )
    conn.commit()
    conn.close()


def test_store_crashes_on_a_raw_v1_db_without_the_guard(tmp_path):
    """Pin down the actual production failure mode so this test suite would have
    caught it: Store() alone (no MemoryService in front) really does blow up on a
    v1-shaped file with exactly the error seen in the Railway crash logs."""
    db = tmp_path / "engraphis.db"
    _build_v1_db(str(db))
    try:
        Store(str(db))
    except sqlite3.OperationalError as exc:
        assert "workspace_id" in str(exc)
    else:
        raise AssertionError("expected Store() to fail against a raw v1-shaped database")


def test_memory_service_create_auto_migrates_v1_db(tmp_path):
    db = tmp_path / "engraphis.db"
    _build_v1_db(str(db))

    svc = MemoryService.create(str(db))          # must not raise
    mems = svc.store.list_memories(include_invalid=True)
    assert len(mems) == 1
    assert "dark mode" in mems[0].content
    assert mems[0].workspace_id                   # migrated rows are properly scoped
    svc.store.close()

    # original v1 data preserved untouched alongside the now-migrated db_path
    backups = list(tmp_path.glob("engraphis.v1-backup-*.db"))
    assert len(backups) == 1
    backup_conn = sqlite3.connect(str(backups[0]))
    cols = {r[1] for r in backup_conn.execute("PRAGMA table_info(memories)").fetchall()}
    assert "workspace_id" not in cols              # the backup is the untouched v1 shape
    row = backup_conn.execute("SELECT content FROM memories").fetchone()
    assert row[0] == "User prefers dark mode."
    backup_conn.close()


def test_memory_service_create_is_idempotent_after_migration(tmp_path):
    """A second startup against the now-migrated (v2-shaped) db_path must not re-migrate,
    re-copy a backup, or crash — this is the normal path on every restart after the
    first one."""
    db = tmp_path / "engraphis.db"
    _build_v1_db(str(db))

    svc1 = MemoryService.create(str(db))
    svc1.store.close()
    first_backups = list(tmp_path.glob("engraphis.v1-backup-*.db"))
    assert len(first_backups) == 1

    svc2 = MemoryService.create(str(db))          # second "startup" — must stay a no-op
    mems = svc2.store.list_memories(include_invalid=True)
    assert len(mems) == 1                          # unchanged, not duplicated
    svc2.store.close()

    second_backups = list(tmp_path.glob("engraphis.v1-backup-*.db"))
    assert second_backups == first_backups         # no new backup created on the re-run


def test_fresh_install_is_untouched(tmp_path):
    """A brand-new db_path (no file yet) must not trip the migration path at all."""
    db = tmp_path / "fresh.db"
    _auto_migrate_v1_if_needed(str(db))            # no-op: nothing exists yet
    assert not db.exists()
    assert list(tmp_path.glob("*.v1-backup-*"))  == []

    svc = MemoryService.create(str(db))
    assert svc.store.list_memories(include_invalid=True) == []
    svc.store.close()


def test_already_v2_db_is_left_alone(tmp_path):
    """A db_path that's already v2-shaped (has workspace_id) must not be touched."""
    db = tmp_path / "engraphis.db"
    store = Store(str(db))
    wid = store.get_or_create_workspace("default")
    store.get_or_create_repo(wid, "default")  # v2-shape the db
    store.close()

    svc = MemoryService.create(str(db))            # must not raise, must not back up
    assert list(tmp_path.glob("*.v1-backup-*")) == []
    svc.store.close()
