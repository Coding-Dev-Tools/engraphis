import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from engraphis.core import ids
from engraphis.core.schema import SCHEMA_SQL, SCHEMA_VERSION
from engraphis.core.store import Store


def _prepare_v13_database(path: Path) -> str:
    store = Store(str(path))
    workspace_id = store.get_or_create_workspace("preserved-v13")
    store.close()
    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE source_import_items")
    conn.execute("DROP TABLE source_imports")
    conn.execute("DROP TABLE source_vaults")
    conn.execute("DELETE FROM schema_migrations")
    conn.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (13, 0)")
    conn.commit()
    conn.close()
    return workspace_id


def test_import_source_ids_have_typed_prefixes():
    assert ids.new_id("vault").startswith("vlt_")
    assert ids.new_id("source").startswith("src_")


def test_source_manifest_is_scoped_idempotent_and_marks_missing():
    store = Store(":memory:")
    try:
        workspace_id = store.get_or_create_workspace("obsidian")
        vault_id = store.register_source_vault(
            kind="obsidian", root_digest="a" * 64, workspace_id=workspace_id,
            display_name="Personal",
        )
        assert vault_id == store.register_source_vault(
            kind="obsidian", root_digest="a" * 64, workspace_id=workspace_id,
            display_name="Renamed",
        )
        item_id = store.upsert_source_import_item(
            vault_id=vault_id, source_key="b" * 64, relative_path="Notes/One.md",
            content_sha256="c" * 64,
            importer_version="1", seen_at=10,
        )
        assert item_id.startswith("src_")
        assert store.mark_source_import_items_missing(vault_id=vault_id, seen_before=11) == 1
        item = store.get_source_import_item(vault_id=vault_id, source_key="b" * 64)
        assert item["state"] == "missing"
        store.upsert_source_import_item(
            vault_id=vault_id, source_key="b" * 64, relative_path="Archive/One.md",
            state="imported", seen_at=12,
        )
        assert store.get_source_import_item(vault_id=vault_id, source_key="b" * 64)["missing_at"] is None
    finally:
        store.close()


def test_source_vault_validates_scope_and_memory_type():
    store = Store(":memory:")
    try:
        workspace_id = store.get_or_create_workspace("scopes")
        with pytest.raises(ValueError, match="workspace source vault"):
            store.register_source_vault(
                kind="obsidian", root_digest="a" * 64, workspace_id=workspace_id,
                repo_id="repo_forbidden", scope="workspace",
            )
        with pytest.raises(ValueError, match="repo source vault"):
            store.register_source_vault(
                kind="obsidian", root_digest="b" * 64, workspace_id=workspace_id,
                scope="repo",
            )
        with pytest.raises(ValueError, match="memory_type"):
            store.register_source_vault(
                kind="obsidian", root_digest="c" * 64, workspace_id=workspace_id,
                memory_type="unknown",
            )
        other_workspace = store.get_or_create_workspace("other-scopes")
        foreign_repo = store.get_or_create_repo(other_workspace, "foreign")
        with pytest.raises(ValueError, match="does not belong"):
            store.register_source_vault(
                kind="obsidian", root_digest="d" * 64,
                workspace_id=workspace_id, repo_id=foreign_repo, scope="repo",
            )
    finally:
        store.close()


def test_source_import_item_allows_explicit_conflict_state():
    store = Store(":memory:")
    try:
        workspace_id = store.get_or_create_workspace("conflict")
        vault_id = store.register_source_vault(
            kind="obsidian", root_digest="d" * 64, workspace_id=workspace_id,
        )
        store.upsert_source_import_item(
            vault_id=vault_id, source_key="e" * 64, relative_path="One.md",
            state="conflict",
        )
        assert store.get_source_import_item(vault_id=vault_id, source_key="e" * 64)["state"] == "conflict"
    finally:
        store.close()


def test_source_lineage_and_per_job_results_are_separate_content_free_tables():
    store = Store(":memory:")
    try:
        workspace_id = store.get_or_create_workspace("jobs")
        vault_id = store.register_source_vault(
            kind="obsidian", root_digest="f" * 64, workspace_id=workspace_id,
        )
        source_id = store.upsert_source_import_item(
            vault_id=vault_id, source_key="1" * 64, relative_path="One.md",
            content_sha256="2" * 64, canonical_sha256="3" * 64,
            file_size=12, importer_version="1",
        )
        job_id = ids.new_id("job")
        store.conn.execute(
            "INSERT INTO jobs(id, workspace_id, kind, state, created_at) "
            "VALUES (?,?,'obsidian_import','running',0)",
            (job_id, workspace_id),
        )
        store.conn.commit()
        item_id = store.record_source_import_job_item(
            job_id=job_id, source_id=source_id, relative_path="One.md",
            source_format="markdown", planned_action="imported",
            result_state="imported", warning_count=2,
        )
        assert item_id.startswith("src_")
        lineage = store.get_source_import(source_id)
        assert lineage["relative_path"] == "One.md"
        assert lineage["content_sha256"] == "2" * 64
        result = store.list_source_import_job_items(job_id=job_id)[0]
        assert result["result_state"] == "imported"
        assert result["source_format"] == "markdown"
        assert result["warning_count"] == 2
        assert "content" not in result and "content_sha256" not in result
        with pytest.raises(ValueError, match="format"):
            store.record_source_import_job_item(
                job_id=job_id, relative_path="Bad.md", source_format="text/private path",
                planned_action="rejected", result_state="rejected",
            )
    finally:
        store.close()


def test_source_vault_null_identity_and_cross_scope_lineage_are_durable():
    store = Store(":memory:")
    try:
        workspace_id = store.get_or_create_workspace("vault-owner")
        other_workspace = store.get_or_create_workspace("vault-other")
        vault_id = store.register_source_vault(
            kind="obsidian", root_digest="a" * 64,
            workspace_id=workspace_id,
        )
        with pytest.raises(sqlite3.IntegrityError):
            store.conn.execute(
                "INSERT INTO source_vaults(id,kind,root_digest,workspace_id,scope,"
                "memory_type,created_at,updated_at) "
                "VALUES (?,?,?,?,'workspace','semantic',0,0)",
                (ids.new_id("vault"), "obsidian", "a" * 64, workspace_id),
            )
        source_id = store.upsert_source_import_item(
            vault_id=vault_id, source_key="b" * 64, relative_path="One.md",
        )
        foreign_job = ids.new_id("job")
        store.conn.execute(
            "INSERT INTO jobs(id,workspace_id,kind,state,created_at) "
            "VALUES (?,?,'obsidian_import','running',0)",
            (foreign_job, other_workspace),
        )
        store.conn.commit()
        with pytest.raises(sqlite3.IntegrityError, match="scope mismatch"):
            store.record_source_import_job_item(
                job_id=foreign_job, source_id=source_id,
                relative_path="One.md", planned_action="imported",
            )
        with pytest.raises(sqlite3.IntegrityError, match="scope mismatch"):
            store.upsert_source_import_item(
                vault_id=vault_id, source_key="b" * 64,
                relative_path="One.md", import_id=foreign_job,
            )
    finally:
        store.close()


def test_source_manifest_rejects_cross_adapter_jobs():
    store = Store(":memory:")
    try:
        workspace_id = store.get_or_create_workspace("adapter-binding")
        document_vault = store.register_source_vault(
            kind="documents", root_digest="7" * 64, workspace_id=workspace_id,
        )
        source_id = store.upsert_source_import_item(
            vault_id=document_vault, source_key="8" * 64,
            relative_path="notes/One.txt",
        )
        document_job = ids.new_id("job")
        obsidian_job = ids.new_id("job")
        store.conn.executemany(
            "INSERT INTO jobs(id,workspace_id,kind,state,created_at) VALUES (?,?,?,'running',0)",
            (
                (document_job, workspace_id, "document_import"),
                (obsidian_job, workspace_id, "obsidian_import"),
            ),
        )
        store.conn.commit()
        store.upsert_source_import_item(
            vault_id=document_vault, source_key="8" * 64,
            relative_path="notes/One.txt", import_id=document_job,
        )
        with pytest.raises(sqlite3.IntegrityError, match="seen-job scope mismatch"):
            store.upsert_source_import_item(
                vault_id=document_vault, source_key="8" * 64,
                relative_path="notes/One.txt", import_id=obsidian_job,
            )
        with pytest.raises(sqlite3.IntegrityError, match="job scope mismatch"):
            store.record_source_import_job_item(
                job_id=obsidian_job, source_id=source_id,
                relative_path="notes/One.txt", planned_action="imported",
            )
    finally:
        store.close()


def test_concurrent_null_scoped_vault_registration_has_one_winner(tmp_path):
    db = tmp_path / "vault-race.db"
    initial = Store(str(db))
    workspace_id = initial.get_or_create_workspace("race")
    initial.close()

    def register(_index):
        candidate = Store(str(db))
        try:
            return candidate.register_source_vault(
                kind="obsidian", root_digest="e" * 64,
                workspace_id=workspace_id,
            )
        finally:
            candidate.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        vault_ids = list(pool.map(register, range(2)))
    assert len(set(vault_ids)) == 1
    verifier = Store(str(db))
    try:
        assert len(verifier.list_source_vaults(workspace_id=workspace_id)) == 1
    finally:
        verifier.close()


def test_source_manifest_store_methods_enforce_workspace_allowlist(tmp_path):
    db = tmp_path / "tenants.db"
    owner = Store(str(db))
    allowed_id = owner.get_or_create_workspace("allowed")
    denied_id = owner.get_or_create_workspace("denied")
    allowed_vault = owner.register_source_vault(
        kind="obsidian", root_digest="c" * 64, workspace_id=allowed_id,
    )
    denied_vault = owner.register_source_vault(
        kind="obsidian", root_digest="d" * 64, workspace_id=denied_id,
    )
    owner.close()

    scoped = Store(str(db), allowed_workspaces={"allowed"})
    try:
        assert scoped.get_source_vault(allowed_vault)["workspace_id"] == allowed_id
        with pytest.raises(ValueError, match="not permitted"):
            scoped.get_source_vault(denied_vault)
        assert [row["id"] for row in scoped.list_source_vaults()] == [allowed_vault]
    finally:
        scoped.close()


def test_manifest_snapshot_handles_absent_and_v13_database_without_writing(tmp_path):
    absent = tmp_path / "absent.db"
    assert Store.snapshot_source_import_manifest(str(absent)) == {
        "schema_version": 0, "vaults": [], "items": [],
    }
    legacy = tmp_path / "legacy.db"
    conn = sqlite3.connect(legacy)
    conn.execute("CREATE TABLE schema_migrations(version INTEGER, applied_at REAL)")
    conn.execute("INSERT INTO schema_migrations VALUES (13, 0)")
    conn.commit()
    conn.close()
    assert Store.snapshot_source_import_manifest(str(legacy)) == {
        "schema_version": 13, "vaults": [], "items": [],
    }


def test_manifest_snapshot_uses_injected_immutable_read_connector(tmp_path):
    db = tmp_path / "manifest.db"
    store = Store(str(db))
    store.close()
    seen = []

    class Connector:
        def __call__(self, _path):
            raise AssertionError("writable connector must not be used by a snapshot")

        def open_read_only(self, path):
            snapshot_path = Path(path)
            seen.append({
                "path": snapshot_path,
                "exists": snapshot_path.is_file(),
                "mode": snapshot_path.stat().st_mode,
            })
            uri = Path(path).as_uri() + "?mode=ro&immutable=1"
            connection = sqlite3.connect(uri, uri=True)
            connection.row_factory = sqlite3.Row
            return connection

    snapshot = Store.snapshot_source_import_manifest(str(db), connect=Connector())

    assert snapshot["schema_version"] == SCHEMA_VERSION
    assert len(seen) == 1
    assert seen[0]["exists"] is True
    assert seen[0]["path"] != db.resolve()
    assert seen[0]["path"].name == "manifest.db"
    if os.name != "nt":
        assert seen[0]["mode"] & 0o077 == 0
    assert not seen[0]["path"].exists()
    assert not seen[0]["path"].parent.exists()
    assert not Path(str(db) + "-wal").exists()
    assert not Path(str(db) + "-shm").exists()
    assert not (tmp_path / "legacy.db-wal").exists()


def test_manifest_snapshot_rejects_path_replacement_between_lstat_and_open(
    monkeypatch, tmp_path,
):
    db = tmp_path / "manifest.db"
    owner = Store(str(db))
    owner.close()
    replacement = tmp_path / "replacement.db"
    foreign = Store(str(replacement))
    foreign.close()
    backup = tmp_path / "manifest-owner.db"
    real_open = os.open
    swapped = False

    def swapping_open(path, flags, mode=0o777):
        nonlocal swapped
        if not swapped and Path(path) == db.resolve():
            swapped = True
            os.replace(db, backup)
            os.replace(replacement, db)
        if flags & os.O_CREAT:
            return real_open(path, flags, mode)
        return real_open(path, flags)

    monkeypatch.setattr(os, "open", swapping_open)
    try:
        with pytest.raises(RuntimeError, match="changed while it was opened"):
            Store.snapshot_source_import_manifest(str(db))
    finally:
        if swapped:
            os.replace(db, replacement)
            os.replace(backup, db)


def test_manifest_snapshot_cleans_private_copy_when_connector_fails(tmp_path):
    db = tmp_path / "manifest.db"
    store = Store(str(db))
    store.close()
    seen = []

    class FailingConnector:
        def __call__(self, _path):
            raise AssertionError("writable connector must not be used by a snapshot")

        def open_read_only(self, path):
            seen.append(Path(path))
            assert seen[-1].is_file()
            raise RuntimeError("synthetic connector failure")

    with pytest.raises(RuntimeError, match="synthetic connector failure"):
        Store.snapshot_source_import_manifest(
            str(db), connect=FailingConnector(),
        )

    assert len(seen) == 1
    assert not seen[0].exists()
    assert not seen[0].parent.exists()


def test_manifest_snapshot_rejects_bare_connector_but_keeps_empty_semantics(tmp_path):
    db = tmp_path / "manifest.db"
    store = Store(str(db))
    store.close()
    calls = []

    def writable_only(path):
        calls.append(path)
        return sqlite3.connect(path)

    with pytest.raises(TypeError, match="open_read_only"):
        Store.snapshot_source_import_manifest(
            str(db), connect=writable_only,  # type: ignore[arg-type]
        )
    assert calls == []

    empty = {"schema_version": 0, "vaults": [], "items": []}
    assert Store.snapshot_source_import_manifest(
        str(tmp_path / "missing.db"),
        connect=writable_only,  # type: ignore[arg-type]
    ) == empty
    assert Store.snapshot_source_import_manifest(
        ":memory:", connect=writable_only,  # type: ignore[arg-type]
    ) == empty
    assert calls == []


def test_manifest_snapshot_refuses_active_wal(tmp_path):
    db = tmp_path / "active.db"
    store = Store(str(db))
    try:
        assert store.schema_version == SCHEMA_VERSION
        store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        store.conn.execute("BEGIN IMMEDIATE")
        store.conn.execute("INSERT INTO workspaces(id, name, created_at, settings) VALUES (?,?,?,?)",
                           ("ws_test", "snapshot", 0, "{}"))
        store.conn.commit()
        with pytest.raises(RuntimeError, match="active WAL"):
            Store.snapshot_source_import_manifest(str(db))
    finally:
        store.close()


def test_manifest_snapshot_refuses_active_rollback_journal(tmp_path):
    db = tmp_path / "active-journal.db"
    store = Store(str(db))
    store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    store.close()
    journal = Path(f"{db}-journal")
    journal.write_bytes(b"simulated hot rollback journal")
    before = (journal.stat().st_size, journal.stat().st_mtime_ns)

    with pytest.raises(RuntimeError, match="active rollback journal"):
        Store.snapshot_source_import_manifest(str(db))

    assert (journal.stat().st_size, journal.stat().st_mtime_ns) == before


def test_v13_writable_upgrade_creates_durable_current_manifest_schema(tmp_path):
    db = tmp_path / "v13.db"
    workspace_id = _prepare_v13_database(db)
    upgraded = Store(str(db))
    try:
        assert upgraded.schema_version == SCHEMA_VERSION == 15
        assert upgraded.conn.execute(
            "SELECT id FROM workspaces WHERE id=?", (workspace_id,)
        ).fetchone() is not None
        objects = {
            row["name"] for row in upgraded.conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','index','trigger')"
            ).fetchall()
        }
        assert {
            "source_vaults", "source_imports", "source_import_items",
            "idx_source_vaults_identity", "trg_source_vault_scope_insert",
            "trg_source_import_scope_insert", "trg_source_import_job_insert",
        }.issubset(objects)
        assert Path(f"{db}.pre-migration-v14.bak").is_file()
    finally:
        upgraded.close()


def test_v13_to_current_failure_rolls_back_schema_and_version(monkeypatch, tmp_path):
    db = tmp_path / "rollback-v13.db"
    _prepare_v13_database(db)
    original = Store._apply_schema

    def fail_after_schema(self, previous_version):
        original(self, previous_version)
        raise RuntimeError("injected current migration failure")

    monkeypatch.setattr(Store, "_apply_schema", fail_after_schema)
    with pytest.raises(RuntimeError, match="injected current"):
        Store(str(db))
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 13
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_vaults'"
        ).fetchone() is None
    finally:
        conn.close()


def test_v13_read_only_refuses_without_writing_then_accepts_upgraded_db(tmp_path):
    db = tmp_path / "readonly-v13.db"
    _prepare_v13_database(db)
    with pytest.raises(RuntimeError, match="complete current schema"):
        Store(str(db), read_only=True)
    assert not Path(f"{db}.pre-migration-v14.bak").exists()

    writable = Store(str(db))
    writable.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    writable.close()
    readonly = Store(str(db), read_only=True)
    try:
        assert readonly.schema_version == 15
        with pytest.raises(sqlite3.OperationalError):
            readonly.conn.execute(
                "INSERT INTO workspaces(id,name) VALUES ('ws_nope','nope')"
            )
    finally:
        readonly.close()


def test_v14_manifest_upgrade_preserves_lineage_and_accepts_documents(tmp_path):
    db = tmp_path / "v14-manifest.db"
    conn = sqlite3.connect(db)
    legacy_sql = SCHEMA_SQL.replace(
        "CHECK(kind IN ('documents','obsidian'))", "CHECK(kind IN ('obsidian'))",
    )
    conn.executescript(legacy_sql)
    workspace_id = ids.new_id("workspace")
    job_id = ids.new_id("job")
    vault_id = ids.new_id("vault")
    source_id = ids.new_id("source")
    conn.execute(
        "INSERT INTO schema_migrations(version,applied_at) VALUES (14,0)"
    )
    conn.execute(
        "INSERT INTO workspaces(id,name,created_at,settings) VALUES (?,?,0,'{}')",
        (workspace_id, "v14-owner"),
    )
    conn.execute(
        "INSERT INTO jobs(id,workspace_id,kind,state,created_at) "
        "VALUES (?,?,'obsidian_import','completed',0)",
        (job_id, workspace_id),
    )
    conn.execute(
        "INSERT INTO source_vaults(id,kind,root_digest,display_name,workspace_id,"
        "scope,memory_type,importer_version,created_at,updated_at) "
        "VALUES (?,'obsidian',?,'Legacy',?,'workspace','semantic','1',0,0)",
        (vault_id, "a" * 64, workspace_id),
    )
    conn.execute(
        "INSERT INTO source_imports(id,vault_id,source_key,relative_path,"
        "importer_version,state,last_seen_job_id) "
        "VALUES (?,?,?,?, '1','imported',?)",
        (source_id, vault_id, "b" * 64, "Legacy.md", job_id),
    )
    conn.execute(
        "INSERT INTO source_import_items(id,job_id,source_id,relative_path,"
        "planned_action,result_state,created_at) VALUES (?,?,?,?, 'imported','imported',0)",
        (ids.new_id("source"), job_id, source_id, "Legacy.md"),
    )
    conn.commit()
    conn.close()

    upgraded = Store(str(db))
    try:
        assert upgraded.schema_version == 15
        assert upgraded.get_source_vault(vault_id)["kind"] == "obsidian"
        assert upgraded.get_source_import(source_id)["relative_path"] == "Legacy.md"
        assert upgraded.list_source_import_job_items(job_id=job_id)[0]["source_id"] == source_id
        assert upgraded.list_source_import_job_items(job_id=job_id)[0]["source_format"] == ""
        documents_id = upgraded.register_source_vault(
            kind="documents", root_digest="c" * 64, workspace_id=workspace_id,
            display_name="Mixed documents",
        )
        assert upgraded.get_source_vault(documents_id)["kind"] == "documents"
        assert upgraded.conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert upgraded.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert Path(f"{db}.pre-migration-v15.bak").is_file()
    finally:
        upgraded.close()
