from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
from pathlib import Path

import pytest

from engraphis.core.store import Store
from engraphis.core.interfaces import Edge, MemoryRecord, Scope, SearchFilter
from engraphis.core.retention_policy import (
    DEFAULT_STABILITY_DAYS,
    MAX_ACCESS_COUNT,
    MAX_STABILITY_DAYS,
    MIN_STABILITY_DAYS,
)
from engraphis.core.schema import SCHEMA_VERSION


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
    assert migrated.schema_version == SCHEMA_VERSION
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

        assert upgraded.schema_version == SCHEMA_VERSION
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
        assert upgraded.schema_version == SCHEMA_VERSION
        assert Path(f"{db}.pre-migration-v{SCHEMA_VERSION}.bak").is_file()
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
        assert upgraded.schema_version == SCHEMA_VERSION
        assert Path(f"{db}.pre-migration-v6.bak").is_file()
        assert hashlib.sha256(legacy_backup.read_bytes()).hexdigest() == legacy_digest
        assert history["file"] == "api.py"
        assert history["content_hash"] == "v5-hash"
        assert history["valid_from"] == history["ingested_at"]
    finally:
        upgraded.close()


def test_v6_upgrade_adds_confidence_and_preserves_rows(tmp_path):
    """A v6 database upgrades to the current schema: the additive ``confidence``
    column appears, every existing row keeps its identity/content, and the column
    defaults to 1.0 so pre-existing memories score exactly as they did before."""
    db = tmp_path / "v6-confidence.db"
    store = Store(str(db))
    workspace_id = store.get_or_create_workspace("acme")
    memory_id = store.add_memory(MemoryRecord(
        id="mem_v6",
        content="Staging runs PostgreSQL 16.",
        workspace_id=workspace_id,
        scope=Scope.WORKSPACE,
        importance=0.7,
    ))
    store.conn.execute(
        "UPDATE memories SET importance=0.7 WHERE id=?", (memory_id,)
    )
    # Downgrade the schema marker to v6 so the next open runs the v6→v7→v8→v9 path
    # (the additive ALTERs, confidence marker, and scoped tombstones).
    store.conn.execute("DELETE FROM schema_migrations")
    store.conn.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (6, 0)")
    store.conn.commit()
    store.close()

    upgraded = Store(str(db))
    try:
        columns = {row["name"] for row in upgraded.conn.execute(
            "PRAGMA table_info(memories)"
        ).fetchall()}
        row = upgraded.conn.execute(
            "SELECT id, content, importance, confidence FROM memories WHERE id=?",
            (memory_id,),
        ).fetchone()
        record = upgraded.get_memory(memory_id)

        assert upgraded.schema_version == SCHEMA_VERSION
        # A v6 source backs up as v7 (min(SCHEMA_VERSION, previous_version + 1)).
        assert Path(f"{db}.pre-migration-v7.bak").is_file()
        assert "confidence" in columns
        assert row["id"] == memory_id
        assert row["content"] == "Staging runs PostgreSQL 16."
        assert row["importance"] == 0.7
        assert float(row["confidence"]) == 1.0          # NOT NULL DEFAULT 1.0 backfill
        assert record is not None
        assert record.confidence == 1.0
        assert record.importance == 0.7

        # The v7 deterministic-hashing marker also lands (v6 < v7), keeping the
        # embed-rebuild durable marker retryable on the next open.
        assert upgraded.embedding_version("deterministic_hashing") is not None
    finally:
        upgraded.close()


def test_v7_reopen_canonicalizes_legacy_entity_aliases_idempotently(
        monkeypatch, tmp_path):
    """A v7 store gets the one-time alias repair when it first reopens."""
    db = tmp_path / "v7-entity-aliases.db"
    store = Store(str(db))
    workspace_id = store.get_or_create_workspace("acme")
    store.conn.executemany(
        "INSERT INTO entities("
        "id, workspace_id, repo_id, name, etype, canonical_id, normalized_name, "
        "canonical_method, canonical_confidence, created_at"
        ") VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            ("ent_openai", workspace_id, None, "OpenAI", "org", "ent_openai", "",
             "identity", 1.0, 1.0),
            ("ent_open_ai", workspace_id, None, "Open AI", "org", "ent_open_ai", "",
             "identity", 1.0, 2.0),
        ],
    )
    store.conn.execute("DELETE FROM schema_migrations")
    store.conn.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (7, 0)")
    store.conn.commit()
    store.close()

    reopened = Store(str(db))
    try:
        rows = reopened.conn.execute(
            "SELECT id, canonical_id, canonical_method FROM entities "
            "ORDER BY id"
        ).fetchall()
        assert reopened.schema_version == SCHEMA_VERSION
        assert [(row["canonical_id"], row["canonical_method"]) for row in rows] == [
            ("ent_open_ai", "token_overlap"),
            ("ent_open_ai", "token_overlap"),
        ]
    finally:
        reopened.close()

    def unexpected(*_args, **_kwargs):
        raise AssertionError("entity canonicalization repeated after v9 migration")

    monkeypatch.setattr(Store, "_backfill_entity_canonicalization", unexpected)
    reopened_again = Store(str(db))
    try:
        rows = reopened_again.conn.execute(
            "SELECT id, canonical_id, canonical_method FROM entities "
            "ORDER BY id"
        ).fetchall()
        assert [(row["canonical_id"], row["canonical_method"]) for row in rows] == [
            ("ent_open_ai", "token_overlap"),
            ("ent_open_ai", "token_overlap"),
        ]
    finally:
        reopened_again.close()


def test_v8_tombstone_shape_rebuilds_repo_index_and_preserves_legacy_rows(tmp_path):
    db = tmp_path / "v8-tombstones.db"
    store = Store(str(db))
    store.conn.execute("DROP INDEX idx_memory_tombstones_workspace")
    store.conn.execute("ALTER TABLE memory_tombstones RENAME TO memory_tombstones_current")
    store.conn.execute(
        "CREATE TABLE memory_tombstones ("
        "memory_id TEXT PRIMARY KEY, deleted_at REAL NOT NULL, device_id TEXT NOT NULL, "
        "workspace_id TEXT, created_at REAL NOT NULL)"
    )
    store.conn.execute(
        "INSERT INTO memory_tombstones "
        "(memory_id, deleted_at, device_id, workspace_id, created_at) "
        "VALUES ('legacy-erased', 10.0, 'old-device', NULL, 10.0)"
    )
    store.conn.execute("DROP TABLE memory_tombstones_current")
    store.conn.execute(
        "CREATE INDEX idx_memory_tombstones_workspace "
        "ON memory_tombstones(workspace_id, memory_id)"
    )
    store.conn.execute("DELETE FROM schema_migrations")
    store.conn.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (8, 0)")
    store.conn.commit()
    store.close()

    upgraded = Store(str(db))
    try:
        columns = [
            row["name"] for row in upgraded.conn.execute(
                "PRAGMA table_info(memory_tombstones)"
            ).fetchall()
        ]
        index_columns = [
            row["name"] for row in upgraded.conn.execute(
                "PRAGMA index_info('idx_memory_tombstones_workspace')"
            ).fetchall()
        ]
        row = upgraded.conn.execute(
            "SELECT memory_id, repo_id, export_class FROM memory_tombstones "
            "WHERE memory_id='legacy-erased'"
        ).fetchone()
        assert upgraded.schema_version == SCHEMA_VERSION
        assert "repo_id" in columns
        assert "export_class" in columns
        assert index_columns == ["workspace_id", "repo_id", "memory_id"]
        assert row["memory_id"] == "legacy-erased"
        assert row["repo_id"] is None
        assert row["export_class"] == "never_export"
        assert Path(f"{db}.pre-migration-v9.bak").is_file()


    finally:
        upgraded.close()

    reopened = Store(str(db))
    try:
        assert [
            row["name"] for row in reopened.conn.execute(
                "PRAGMA index_info('idx_memory_tombstones_workspace')"
            ).fetchall()
        ] == ["workspace_id", "repo_id", "memory_id"]
    finally:
        reopened.close()


def test_v11_upgrade_classifies_legacy_tombstones_as_never_export(tmp_path):
    db = tmp_path / "v11-tombstones.db"
    store = Store(str(db))
    store.add_memory_tombstone(
        "mem_legacy_tombstone",
        deleted_at=10.0,
        device_id="legacy-device",
    )
    store.conn.execute("DROP INDEX idx_memory_tombstones_workspace")
    store.conn.execute(
        "ALTER TABLE memory_tombstones RENAME TO memory_tombstones_current"
    )
    store.conn.execute(
        "CREATE TABLE memory_tombstones ("
        "memory_id TEXT PRIMARY KEY, deleted_at REAL NOT NULL, "
        "device_id TEXT NOT NULL, workspace_id TEXT, repo_id TEXT, "
        "created_at REAL NOT NULL)"
    )
    store.conn.execute(
        "INSERT INTO memory_tombstones "
        "(memory_id, deleted_at, device_id, workspace_id, repo_id, created_at) "
        "SELECT memory_id, deleted_at, device_id, workspace_id, repo_id, created_at "
        "FROM memory_tombstones_current"
    )
    store.conn.execute("DROP TABLE memory_tombstones_current")
    store.conn.execute(
        "CREATE INDEX idx_memory_tombstones_workspace "
        "ON memory_tombstones(workspace_id, repo_id, memory_id)"
    )
    store.conn.execute("DELETE FROM schema_migrations")
    store.conn.execute(
        "INSERT INTO schema_migrations(version, applied_at) VALUES (11, 0)"
    )
    store.conn.commit()
    store.close()

    upgraded = Store(str(db))
    try:
        marker = upgraded.list_memory_tombstones()[0]
        assert upgraded.schema_version == SCHEMA_VERSION
        assert marker["id"] == "mem_legacy_tombstone"
        assert marker["export_class"] == "never_export"
        assert Path(f"{db}.pre-migration-v12.bak").is_file()
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
        assert reopened.schema_version == SCHEMA_VERSION
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
    assert restarted.schema_version == SCHEMA_VERSION
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
    assert _version(db) == SCHEMA_VERSION


def test_v9_upgrade_repairs_unsafe_retention_state(tmp_path):
    db = tmp_path / "v9-retention.db"
    store = Store(str(db))
    workspace_id = store.get_or_create_workspace("acme")
    ids = [
        store.add_memory(MemoryRecord(id=f"mem_retention_{index}", content=str(index),
                                      workspace_id=workspace_id))
        for index in range(5)
    ]
    rows = [
        (None, None),
        (-2.0, -3),
        (0.01, 4),
        (float("inf"), MAX_ACCESS_COUNT + 10),
        (250.0, 5),
    ]
    for memory_id, (stability, count) in zip(ids, rows):
        store.conn.execute(
            "UPDATE memories SET stability=?, access_count=? WHERE id=?",
            (stability, count, memory_id),
        )
    store.conn.execute("DELETE FROM schema_migrations")
    store.conn.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (9, 0)")
    store.conn.commit()
    store.close()

    upgraded = Store(str(db))
    try:
        repaired = upgraded.conn.execute(
            "SELECT stability, access_count FROM memories ORDER BY id"
        ).fetchall()
        assert upgraded.schema_version == SCHEMA_VERSION
        assert [row["stability"] for row in repaired] == [
            DEFAULT_STABILITY_DAYS,
            DEFAULT_STABILITY_DAYS,
            MIN_STABILITY_DAYS,
            MAX_STABILITY_DAYS,
            MAX_STABILITY_DAYS,
        ]
        assert [row["access_count"] for row in repaired] == [
            0, 0, 4, MAX_ACCESS_COUNT, 5,
        ]
        assert Path(f"{db}.pre-migration-v10.bak").is_file()
    finally:
        upgraded.close()


def test_v11_schema_repairs_missing_session_handoff_without_losing_rows(tmp_path):
    db = tmp_path / "v11-without-handoff.db"
    initial = Store(str(db))
    wid = initial.get_or_create_workspace("handoff")
    rid = initial.get_or_create_repo(wid, "repo")
    sid = initial.start_session(wid, rid, agent="agent", goal="preserve")
    initial.close()

    conn = sqlite3.connect(db)
    conn.execute("ALTER TABLE sessions DROP COLUMN handoff")
    conn.execute("DELETE FROM schema_migrations")
    conn.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (11, 0)")
    conn.commit()
    conn.close()

    repaired = Store(str(db))
    try:
        columns = {
            row["name"] for row in repaired.conn.execute(
                "PRAGMA table_info(sessions)"
            ).fetchall()
        }
        assert "handoff" in columns
        preserved = repaired.conn.execute(
            "SELECT id, handoff FROM sessions WHERE id=?", (sid,)
        ).fetchone()
        assert preserved is not None
        assert preserved["id"] == sid
        assert preserved["handoff"] == "{}"
        assert repaired.end_session(
            sid,
            summary="done",
            open_threads=["verify continuity"],
        ) == "ended"
        ended = repaired.get_last_session(wid, rid)
        assert ended is not None and ended["id"] == sid
    finally:
        repaired.close()

    reopened = Store(str(db))
    try:
        assert reopened.schema_version == SCHEMA_VERSION
        session = reopened.get_session(sid)
        assert session is not None
        assert session["status"] == "summarized"
        assert session["summary"] == "done"
        assert session["open_threads"] == ["verify continuity"]
        handoff_row = reopened.conn.execute(
            "SELECT handoff FROM sessions WHERE id=?", (sid,)
        ).fetchone()
        assert handoff_row is not None
        assert handoff_row["handoff"] == "{}"
    finally:
        reopened.close()


def test_v12_upgrade_adds_descriptive_hlc_and_sync_export_proof(tmp_path):
    db = tmp_path / "v12-without-descriptive-hlc.db"
    initial = Store(str(db))
    wid = initial.get_or_create_workspace("descriptive-hlc")
    memory_id = initial.add_memory(MemoryRecord(
        id="mem_pre_hlc",
        content="legacy descriptive state",
        workspace_id=wid,
        scope=Scope.WORKSPACE,
    ))
    repair_provenance = {
        "source": "agent",
        "trusted": True,
        "review_state": "approved",
        "trust_origin": "local_mcp_agent",
    }
    repair_id = initial.add_memory(MemoryRecord(
        id="mem_pre_hlc_repair",
        content="legacy model-authored state",
        workspace_id=wid,
        scope=Scope.WORKSPACE,
        provenance=repair_provenance,
        metadata={
            "provenance": repair_provenance,
            "llm_extraction": {"mode": "llm_structured", "provider": "test"},
        },
    ))
    initial.conn.execute(
        "DELETE FROM sync_state "
        "WHERE key='__schema_v12_llm_extraction_trust_repair'"
    )
    initial.conn.commit()
    initial.close()

    conn = sqlite3.connect(db)
    conn.execute("ALTER TABLE memories DROP COLUMN modified_hlc")
    conn.execute("DROP TABLE memory_sync_exports")
    conn.execute("DELETE FROM schema_migrations")
    conn.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (12, 0)")
    conn.commit()
    conn.close()

    upgraded = Store(str(db))
    try:
        assert upgraded.schema_version == SCHEMA_VERSION
        memory_columns = {
            row["name"] for row in upgraded.conn.execute(
                "PRAGMA table_info(memories)"
            ).fetchall()
        }
        assert "modified_hlc" in memory_columns
        assert upgraded.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='memory_sync_exports'"
        ).fetchone() is not None
        legacy = upgraded.get_memory(memory_id)
        assert legacy is not None and legacy.modified_hlc == ""
        repaired = upgraded.get_memory(repair_id)
        assert repaired is not None
        assert repaired.modified_hlc
        assert repaired.provenance["review_state"] == "pending"
        advanced = upgraded.advance_memory_modified_hlc(memory_id)
        assert advanced
        persisted = upgraded.get_memory(memory_id)
        assert persisted is not None and persisted.modified_hlc == advanced
        assert Path(f"{db}.pre-migration-v13.bak").is_file()
    finally:
        upgraded.close()
