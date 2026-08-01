from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
from pathlib import Path

import pytest

from engraphis.core.store import Store
from engraphis.core.interfaces import Edge, MemoryRecord, Scope, SearchFilter


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
    assert migrated.schema_version == 7
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


def test_v4_upgrade_rebuilds_code_history_and_backfills_claim_identity(tmp_path):
    """Exercise the physical v4 link-table shape, not just its version marker."""
    db = tmp_path / "v4-code-history.db"
    store = Store(str(db))
    workspace_id = store.get_or_create_workspace("acme")
    repo_id = store.get_or_create_repo(workspace_id, "api")
    memory_id = store.add_memory(MemoryRecord(
        id="", content="Production deploys require an approval.",
        workspace_id=workspace_id, repo_id=repo_id, scope=Scope.REPO,
        metadata={"subject_key": "production-deploy", "claim_kind": "policy"},
    ))
    symbol_id = store.upsert_symbol(
        repo_id=repo_id, kind="function", name="deploy", fqname="deploy",
        file="deploy.py", span="1-1",
    )
    # Recreate v4's non-temporal code-link table, including its table-level UNIQUE
    # constraint.  A migration that only bumps the version cannot pass this test.
    for index in (
        "idx_code_mem_live_unique", "idx_code_mem_live_symbol",
        "idx_code_mem_symbol", "idx_code_mem_memory",
    ):
        store.conn.execute(f"DROP INDEX IF EXISTS {index}")
    store.conn.execute("DROP TABLE code_memory_links")
    store.conn.execute(
        "CREATE TABLE code_memory_links ("
        "id TEXT PRIMARY KEY, repo_id TEXT NOT NULL, symbol_id TEXT NOT NULL, "
        "memory_id TEXT NOT NULL, relation TEXT DEFAULT 'mentions', "
        "confidence REAL DEFAULT 1.0, created_at REAL, "
        "UNIQUE(repo_id, symbol_id, memory_id, relation))"
    )
    store.conn.execute(
        "INSERT INTO code_memory_links "
        "(id, repo_id, symbol_id, memory_id, relation, confidence, created_at) "
        "VALUES ('old_link', ?, ?, ?, 'mentions', 0.7, 10)",
        (repo_id, symbol_id, memory_id),
    )
    store.conn.execute(
        "UPDATE memories SET subject_key='', claim_kind='' WHERE id=?", (memory_id,)
    )
    store.conn.execute("DELETE FROM schema_migrations")
    store.conn.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (4, 0)")
    store.conn.commit()
    store.close()

    # A real v4 database may retain its immutable v3→v4 recovery snapshot.
    # The v5 migration must not try to overwrite or validate that older file
    # against the newer source.
    legacy_backup = Path(f"{db}.pre-migration-v4.bak")
    shutil.copyfile(db, legacy_backup)
    legacy_conn = sqlite3.connect(legacy_backup)
    try:
        legacy_conn.execute("DELETE FROM schema_migrations")
        legacy_conn.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (3, 0)"
        )
        legacy_conn.commit()
    finally:
        legacy_conn.close()
    legacy_digest = hashlib.sha256(legacy_backup.read_bytes()).hexdigest()

    upgraded = Store(str(db))
    try:
        columns = {row["name"] for row in upgraded.conn.execute(
            "PRAGMA table_info(code_memory_links)"
        ).fetchall()}
        link = upgraded.conn.execute(
            "SELECT valid_from, ingested_at FROM code_memory_links WHERE id='old_link'"
        ).fetchone()
        record = upgraded.get_memory(memory_id)

        assert upgraded.schema_version == 7
        assert Path(f"{db}.pre-migration-v5.bak").is_file()
        assert hashlib.sha256(legacy_backup.read_bytes()).hexdigest() == legacy_digest
        assert {"valid_from", "valid_to", "ingested_at", "expired_at"} <= columns
        assert link["valid_from"] == 10
        assert link["ingested_at"] == 10
        assert record.subject_key == "production-deploy"
        assert record.claim_kind == "policy"

        # Retire and recreate the same tuple: the v5 partial uniqueness constraint
        # permits history plus one live row, unlike v4's table-level UNIQUE.
        upgraded.clear_code_memory_links(repo_id)
        recreated = upgraded.link_memory_symbol(
            repo_id=repo_id, symbol_id=symbol_id, memory_id=memory_id,
        )
        assert recreated != "old_link"
        assert upgraded.conn.execute(
            "SELECT COUNT(*) AS n FROM code_memory_links WHERE repo_id=? "
            "AND symbol_id=? AND memory_id=? AND relation='mentions'",
            (repo_id, symbol_id, memory_id),
        ).fetchone()["n"] == 2
    finally:
        upgraded.close()


def test_v4_upgrade_backfills_closed_graph_support_for_historical_recall(tmp_path):
    db = tmp_path / "v4-closed-incidence.db"
    store = Store(str(db))
    workspace_id = store.get_or_create_workspace("acme")
    repo_id = store.get_or_create_repo(workspace_id, "api")
    memory_id = store.add_memory(MemoryRecord(
        id="", content="Alpha depended on Beta.",
        workspace_id=workspace_id, repo_id=repo_id, scope=Scope.REPO,
        valid_from=10.0, ingested_at=10.0,
    ))
    edge_id = store.upsert_edge(Edge(
        id="", src="ent_alpha", dst="ent_beta", relation="depends_on",
        workspace_id=workspace_id, repo_id=repo_id,
        valid_from=10.0, ingested_at=10.0,
        provenance={"memory_id": memory_id},
    ))
    store.close_validity(memory_id, at=20.0)
    store.conn.execute("DELETE FROM memory_entities")
    store.conn.execute("DELETE FROM schema_migrations")
    store.conn.execute(
        "INSERT INTO schema_migrations(version, applied_at) VALUES (4, 0)"
    )
    store.conn.commit()
    store.close()

    upgraded = Store(str(db))
    try:
        historical = SearchFilter(
            workspace_id=workspace_id,
            repo_id=repo_id,
            valid_at=15.0,
            known_at=25.0,
        )
        incidence = upgraded.list_memory_entities(historical)
        assert {
            (row["memory_id"], row["entity_id"]) for row in incidence
        } == {
            (memory_id, "ent_alpha"),
            (memory_id, "ent_beta"),
        }
        assert upgraded.edge_supports_in_scope(
            [edge_id], flt=historical
        )
    finally:
        upgraded.close()


def test_existing_v5_database_with_legacy_memory_links_is_upgraded_safely(tmp_path):
    """Repair the short-lived v5 shape without treating old links as ancient facts."""
    db = tmp_path / "v5-direct-link-history.db"
    store = Store(str(db))
    store.conn.execute("DROP INDEX IF EXISTS idx_mem_links_temporal")
    store.conn.execute("DROP INDEX IF EXISTS idx_mem_links_b")
    store.conn.execute("DROP INDEX IF EXISTS idx_mem_links_ab")
    store.conn.execute("DROP TABLE mem_links")
    store.conn.execute(
        "CREATE TABLE mem_links ("
        "a TEXT, b TEXT, relation TEXT, layer TEXT DEFAULT 'semantic', "
        "reason TEXT DEFAULT '', created_at REAL)"
    )
    store.conn.execute(
        "INSERT INTO mem_links(a, b, relation, layer, reason, created_at) "
        "VALUES ('mem_a', 'mem_b', 'related', 'semantic', 'legacy', 123)"
    )
    store.conn.commit()
    store.close()

    upgraded = Store(str(db))
    try:
        columns = {row["name"] for row in upgraded.conn.execute(
            "PRAGMA table_info(mem_links)"
        ).fetchall()}
        row = upgraded.conn.execute(
            "SELECT valid_from, ingested_at, valid_to, expired_at "
            "FROM mem_links WHERE a='mem_a'"
        ).fetchone()
        assert upgraded.schema_version == 7
        assert Path(f"{db}.pre-migration-v7.bak").is_file()
        assert {"valid_from", "valid_to", "valid_to_recorded_at", "ingested_at", "expired_at"} <= columns
        assert row["valid_from"] == row["ingested_at"] == 123
        assert row["valid_to"] is None and row["expired_at"] is None
    finally:
        upgraded.close()


def test_v5_upgrade_seeds_temporal_code_file_manifest(tmp_path):
    db = tmp_path / "v5-code-files.db"
    store = Store(str(db))
    workspace_id = store.get_or_create_workspace("acme")
    repo_id = store.get_or_create_repo(workspace_id, "api")
    store.upsert_code_file(
        repo_id=repo_id, file="api.py", lang="python", content_hash="v5-hash",
        size_bytes=12, mtime_ns=34, backend="regex",
    )
    store.conn.execute("DELETE FROM code_file_history")
    store.conn.execute("DELETE FROM schema_migrations")
    store.conn.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (5, 0)")
    store.conn.commit()
    store.close()

    # A v5 database may retain its immutable v4→v5 recovery artifact.  A later v5
    # write makes its contents intentionally differ from the current v5 database.
    legacy_backup = Path(f"{db}.pre-migration-v5.bak")
    shutil.copyfile(db, legacy_backup)
    legacy_conn = sqlite3.connect(legacy_backup)
    try:
        legacy_conn.execute("DELETE FROM schema_migrations")
        legacy_conn.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (4, 0)"
        )
        legacy_conn.commit()
    finally:
        legacy_conn.close()
    legacy_digest = hashlib.sha256(legacy_backup.read_bytes()).hexdigest()
    current = sqlite3.connect(db)
    try:
        current.execute("UPDATE code_files SET mtime_ns=35 WHERE file='api.py'")
        current.commit()
    finally:
        current.close()

    upgraded = Store(str(db))
    try:
        history = upgraded.conn.execute(
            "SELECT file, content_hash, valid_from, ingested_at FROM code_file_history"
        ).fetchone()
        assert upgraded.schema_version == 7
        assert Path(f"{db}.pre-migration-v6.bak").is_file()
        assert hashlib.sha256(legacy_backup.read_bytes()).hexdigest() == legacy_digest
        assert history["file"] == "api.py"
        assert history["content_hash"] == "v5-hash"
        assert history["valid_from"] == history["ingested_at"]
    finally:
        upgraded.close()


def test_reopening_v5_does_not_repeat_full_history_migrations(tmp_path, monkeypatch):
    db = tmp_path / "already-v5.db"
    Store(str(db)).close()

    def unexpected(*_args, **_kwargs):
        raise AssertionError("v5 migration transform repeated on an already-v5 database")

    monkeypatch.setattr(Store, "_migrate_code_history_v5", unexpected)
    monkeypatch.setattr(Store, "_backfill_claim_identity_v5", unexpected)
    monkeypatch.setattr(Store, "_backfill_memory_entities_v5", unexpected)
    monkeypatch.setattr(Store, "_migrate_code_file_history_v6", unexpected)
    reopened = Store(str(db))
    try:
        assert reopened.schema_version == 7
    finally:
        reopened.close()


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
    assert restarted.schema_version == 7
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
    assert _version(db) == 7
