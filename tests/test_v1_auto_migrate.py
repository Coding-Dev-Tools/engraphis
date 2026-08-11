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
from pathlib import Path

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


def _build_partial_v2_db_missing_memories_expired_at(path: str) -> None:
    """Build a v2-shaped database that is missing only ``memories.expired_at``.

    This mirrors the compatibility gap that once surfaced during startup: the
    store can see an otherwise current schema, but the live memory indexes and
    list queries still need the bi-temporal column to be repaired in place.
    """
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at REAL
        );
        INSERT INTO schema_migrations(version, applied_at) VALUES (15, 1000.0);

        CREATE TABLE workspaces (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            created_at REAL,
            settings TEXT DEFAULT '{}'
        );
        INSERT INTO workspaces(id, name, created_at, settings)
        VALUES ('ws_legacy', 'default', 1000.0, '{}');

        CREATE TABLE memories (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            repo_id TEXT,
            session_id TEXT,
            scope TEXT NOT NULL DEFAULT 'repo',
            mtype TEXT NOT NULL DEFAULT 'semantic',
            title TEXT DEFAULT '',
            content TEXT NOT NULL,
            summary TEXT DEFAULT '',
            keywords TEXT DEFAULT '[]',
            metadata TEXT DEFAULT '{}',
            importance REAL DEFAULT 0.0,
            surprise REAL DEFAULT 1.0,
            stability REAL DEFAULT 1.0,
            confidence REAL NOT NULL DEFAULT 1.0,
            access_count INTEGER DEFAULT 0,
            last_access REAL,
            valid_from REAL,
            valid_to REAL,
            valid_to_recorded_at REAL,
            ingested_at REAL,
            modified_hlc TEXT NOT NULL DEFAULT '',
            subject_key TEXT DEFAULT '',
            claim_kind TEXT DEFAULT '',
            pinned INTEGER DEFAULT 0,
            sensitivity TEXT DEFAULT 'normal',
            provenance TEXT DEFAULT '{}',
            pinned_at REAL,
            unpinned_at REAL,
            sort_order REAL
        );
        INSERT INTO memories(
            id, workspace_id, repo_id, session_id, scope, mtype, title, content,
            summary, keywords, metadata, importance, surprise, stability, confidence,
            access_count, last_access, valid_from, valid_to, valid_to_recorded_at,
            ingested_at, modified_hlc, subject_key, claim_kind, pinned, sensitivity,
            provenance, pinned_at, unpinned_at, sort_order
        ) VALUES (
            'mem_legacy', 'ws_legacy', NULL, NULL, 'workspace', 'semantic', 'theme',
            'User prefers dark mode.', '', '[]', '{}', 0.0, 1.0, 1.0, 1.0,
            3, 1000.0, 1000.0, NULL, NULL, 1000.0, '',
            '', '', 0, 'normal', '{}', NULL, NULL, NULL
        );
        """
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


def test_memory_service_create_normalizes_file_uri(tmp_path, monkeypatch):
    db = tmp_path / "nested" / "engraphis.db"
    monkeypatch.chdir(tmp_path)

    svc = MemoryService.create(db.as_uri(), graph_extractor="none")
    try:
        assert Path(svc.store.path) == db
        assert db.exists()
    finally:
        svc.store.close()

    assert not (tmp_path / "file:").exists()


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



def test_crash_recovery_v2_swap_only(tmp_path):
    """Simulate a crash in legacy code after renaming original to .v2_swap
    but before anything else happened. db_path is absent, .v2_swap holds
    the original v1 data. Recovery must restore .v2_swap to db_path, then
    the normal migration path converts it to v2."""
    db = tmp_path / "engraphis.db"
    _build_v1_db(str(db))
    # Simulate the legacy crash: rename original to .v2_swap
    swap = db.with_suffix(".v2_swap")
    db.rename(swap)
    assert not db.exists()
    assert swap.exists()

    svc = MemoryService.create(str(db))  # must recover and migrate
    mems = svc.store.list_memories(include_invalid=True)
    assert len(mems) == 1
    assert "dark mode" in mems[0].content
    assert mems[0].workspace_id
    svc.store.close()
    # .v2_swap consumed by recovery
    assert not swap.exists()


def test_crash_recovery_v2_swap_plus_tmp_new(tmp_path):
    """Simulate a crash after legacy code renamed original to .v2_swap AND
    the migration wrote a complete .v2-migrating-* file, but the final
    os.replace into db_path never happened. Recovery must prefer the
    migrated v2 file over the stale v1 in .v2_swap."""
    db = tmp_path / "engraphis.db"
    _build_v1_db(str(db))

    # First, do a real migration to get a valid v2 file
    svc = MemoryService.create(str(db))
    svc.store.close()
    # Now db is v2-migrated. Save the migrated file aside.
    migrated_content = db.read_bytes()

    # Reset: rebuild v1, simulate the legacy crash window
    db.unlink()
    _build_v1_db(str(db))
    swap = db.with_suffix(".v2_swap")
    db.rename(swap)  # original renamed to .v2_swap (legacy step 1)

    # Plant the migrated file as a .v2-migrating-* (migration completed
    # but os.replace into db_path never happened)
    import time
    ts = int(time.time())
    tmp_new = db.with_name(db.stem + ".v2-migrating-%d" % ts + db.suffix)
    tmp_new.write_bytes(migrated_content)

    assert not db.exists()
    assert swap.exists()
    assert tmp_new.exists()

    svc = MemoryService.create(str(db))  # must prefer tmp_new
    mems = svc.store.list_memories(include_invalid=True)
    assert len(mems) == 1
    assert "dark mode" in mems[0].content
    assert mems[0].workspace_id  # it's the migrated v2 file, not the v1 original
    svc.store.close()

    # Both staging artifacts cleaned up
    assert not swap.exists()
    assert not tmp_new.exists()


def test_crash_recovery_stray_tmp_new_only(tmp_path):
    """Edge case: db_path absent, no .v2_swap, but a stray .v2-migrating-*
    exists. Recovery should promote it to db_path."""
    db = tmp_path / "engraphis.db"
    _build_v1_db(str(db))

    # Do a real migration first
    svc = MemoryService.create(str(db))
    svc.store.close()
    migrated_content = db.read_bytes()

    # Simulate: db_path gone, no .v2_swap, but migrating file remains
    db.unlink()
    import time
    ts = int(time.time())
    tmp_new = db.with_name(db.stem + ".v2-migrating-%d" % ts + db.suffix)
    tmp_new.write_bytes(migrated_content)

    assert not db.exists()
    assert tmp_new.exists()

    svc = MemoryService.create(str(db))
    mems = svc.store.list_memories(include_invalid=True)
    assert len(mems) == 1
    assert "dark mode" in mems[0].content
    svc.store.close()
    assert not tmp_new.exists()


def test_memory_service_create_repairs_partial_v2_db_missing_memories_expired_at(tmp_path):
    """A partially upgraded db must still open if only the memories expiry column
    is missing.

    This protects the schema-repair path that runs before live memory indexes are
    built, so an interrupted or hand-edited upgrade does not strand a user with a
    startup crash.
    """
    db = tmp_path / "engraphis.db"
    _build_partial_v2_db_missing_memories_expired_at(str(db))

    svc = MemoryService.create(str(db))
    mems = svc.store.list_memories(include_invalid=True)
    assert len(mems) == 1
    assert mems[0].content == "User prefers dark mode."
    columns = {
        row[1]
        for row in svc.store.conn.execute("PRAGMA table_info(memories)").fetchall()
    }
    assert "expired_at" in columns
    svc.store.close()
