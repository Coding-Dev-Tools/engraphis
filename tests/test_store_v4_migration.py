from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path

import pytest

from engraphis.core.store import Store


def _adversarial_link(target: Path, link: Path) -> None:
    try:
        link.symlink_to(target)
    except (NotImplementedError, OSError):
        os.link(str(target), str(link))


def _prepare_v3(path: Path) -> None:
    store = Store(str(path))
    workspace_id = store.get_or_create_workspace("migration-test")
    store.conn.execute(
        "INSERT INTO edges(id, workspace_id, src, dst, relation, layer, provenance) "
        "VALUES ('edge_v3', ?, 'a', 'b', 'related', 'semantic', ?)",
        (workspace_id, '{"memory_id":"mem_source","source":"structured"}'),
    )
    store.conn.execute("DELETE FROM edge_supports")
    store.conn.execute("DELETE FROM schema_migrations")
    store.conn.execute(
        "INSERT INTO schema_migrations(version, applied_at) VALUES (3, 0)"
    )
    store.conn.commit()
    store.close()


def _version(path: Path) -> int:
    conn = sqlite3.connect(path)
    try:
        return int(conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0])
    finally:
        conn.close()


def _quick_check(path: Path) -> str:
    conn = sqlite3.connect(path)
    try:
        return str(conn.execute("PRAGMA quick_check").fetchone()[0])
    finally:
        conn.close()


def test_v3_upgrade_creates_verified_pre_mutation_backup_and_is_idempotent(tmp_path):
    db = tmp_path / "v3.db"
    _prepare_v3(db)

    migrated = Store(str(db))
    assert migrated.schema_version == 4
    assert migrated.conn.execute(
        "SELECT COUNT(*) FROM edge_supports WHERE edge_id='edge_v3'"
    ).fetchone()[0] == 1
    migrated.close()

    backup = Path(f"{db}.pre-migration-v4.bak")
    assert backup.is_file()
    assert _quick_check(backup) == "ok"
    assert _version(backup) == 3
    backup_digest = hashlib.sha256(backup.read_bytes()).hexdigest()

    reopened = Store(str(db))
    assert reopened.conn.execute(
        "SELECT COUNT(*) FROM edge_supports WHERE edge_id='edge_v3'"
    ).fetchone()[0] == 1
    reopened.close()
    assert hashlib.sha256(backup.read_bytes()).hexdigest() == backup_digest


def test_migration_transform_failure_rolls_back_and_restart_completes(
        monkeypatch, tmp_path):
    db = tmp_path / "restart.db"
    _prepare_v3(db)
    original = Store._backfill_edge_supports

    def fail_after_prior_schema_work(self):
        raise RuntimeError("injected migration failure")

    monkeypatch.setattr(Store, "_backfill_edge_supports", fail_after_prior_schema_work)
    with pytest.raises(RuntimeError, match="injected migration failure"):
        Store(str(db))

    assert _quick_check(db) == "ok"
    assert _version(db) == 3
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM edge_supports").fetchone()[0] == 0
    finally:
        conn.close()

    backup = Path(f"{db}.pre-migration-v4.bak")
    assert _quick_check(backup) == "ok"
    assert _version(backup) == 3

    monkeypatch.setattr(Store, "_backfill_edge_supports", original)
    restarted = Store(str(db))
    assert restarted.schema_version == 4
    assert restarted.conn.execute(
        "SELECT COUNT(*) FROM edge_supports WHERE edge_id='edge_v3'"
    ).fetchone()[0] == 1
    restarted.close()


class _ConnectorAdapter:
    """Stand-in for SQLCipher's translating connection wrapper."""

    def __init__(self, raw) -> None:
        self._raw = raw

    def __getattr__(self, name):
        return getattr(self._raw, name)


def test_v3_backup_uses_injected_connection_factory_for_source_and_destination(tmp_path):
    db = tmp_path / "factory.db"
    _prepare_v3(db)
    opened: list[str] = []

    def connector(path: str):
        opened.append(path)
        raw = sqlite3.connect(path, timeout=30, check_same_thread=False)
        raw.row_factory = sqlite3.Row
        return _ConnectorAdapter(raw)

    store = Store(str(db), connect=connector)
    store.close()

    assert opened[0] == str(db)
    assert opened[1] == str(db)
    assert ".pre-migration-v4.bak.tmp-" in opened[2]
    assert _quick_check(Path(f"{db}.pre-migration-v4.bak")) == "ok"


def test_backup_failure_aborts_before_source_mutation(monkeypatch, tmp_path):
    db = tmp_path / "backup-failure.db"
    _prepare_v3(db)
    before = sqlite3.connect(db)
    try:
        edge_before = before.execute(
            "SELECT relation, layer, provenance FROM edges WHERE id='edge_v3'"
        ).fetchone()
    finally:
        before.close()

    monkeypatch.setattr(Store, "_quick_check", staticmethod(lambda _conn: False))
    with pytest.raises(RuntimeError, match="could not create and verify"):
        Store(str(db))

    assert _quick_check(db) == "ok"
    assert _version(db) == 3
    after = sqlite3.connect(db)
    try:
        assert after.execute(
            "SELECT relation, layer, provenance FROM edges WHERE id='edge_v3'"
        ).fetchone() == edge_before
    finally:
        after.close()
    assert not Path(f"{db}.pre-migration-v4.bak").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission-bit contract")
def test_v4_backup_is_owner_only_even_under_permissive_umask(tmp_path):
    db = tmp_path / "private-v3.db"
    _prepare_v3(db)
    os.chmod(db, 0o600)
    previous = os.umask(0o022)
    try:
        Store(str(db)).close()
    finally:
        os.umask(previous)

    backup = Path(f"{db}.pre-migration-v4.bak")
    assert backup.stat().st_mode & 0o777 == 0o600


def test_stale_private_backup_stage_is_swept_before_migration(tmp_path):
    db = tmp_path / "stale-v3.db"
    _prepare_v3(db)
    stale = Path(f"{db}.pre-migration-v4.bak.tmp-1-2-3")
    stale.write_text("private crash residue", encoding="utf-8")

    Store(str(db)).close()

    assert not stale.exists()
    assert _quick_check(Path(f"{db}.pre-migration-v4.bak")) == "ok"


def test_linked_backup_stage_aborts_without_touching_victim(
        monkeypatch, tmp_path):
    db = tmp_path / "linked-v3.db"
    _prepare_v3(db)
    victim = tmp_path / "victim.db"
    _prepare_v3(victim)
    before = hashlib.sha256(victim.read_bytes()).hexdigest()
    monkeypatch.setattr("engraphis.core.store.os.getpid", lambda: 11)
    monkeypatch.setattr("engraphis.core.store.threading.get_ident", lambda: 22)
    monkeypatch.setattr("engraphis.core.store.time.time_ns", lambda: 33)
    stage = Path(f"{db}.pre-migration-v4.bak.tmp-11-22-33")
    _adversarial_link(victim, stage)

    with pytest.raises(RuntimeError, match="could not create and verify"):
        Store(str(db))

    assert hashlib.sha256(victim.read_bytes()).hexdigest() == before
    assert _version(db) == 3


def test_backup_directory_is_durable_before_schema_transform(monkeypatch, tmp_path):
    db = tmp_path / "ordered-v3.db"
    _prepare_v3(db)
    flushed = False
    original_flush = Store._fsync_backup_parent
    original_apply = Store._apply_schema

    def record_flush(path):
        nonlocal flushed
        original_flush(path)
        flushed = True

    def require_flush_before_schema(self, previous_version):
        assert flushed is True
        return original_apply(self, previous_version)

    monkeypatch.setattr(Store, "_fsync_backup_parent", staticmethod(record_flush))
    monkeypatch.setattr(Store, "_apply_schema", require_flush_before_schema)

    Store(str(db)).close()
    assert _version(db) == 4
