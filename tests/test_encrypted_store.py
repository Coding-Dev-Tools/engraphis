"""Encryption at rest (SQLCipher) — opt-in whole-DB AES-256 encryption of the memory DB.

Skips unless a compatible sqlcipher3 driver is installed (the ``encryption`` extra supplies
one on CPython manylinux x86-64), so the NumPy-only offline gate stays green. With the driver
present it proves: the file is unreadable without the key, recall + re-open work through the
encrypted connection (the re-open path exercises the exception-translating adapter), a wrong
key fails loudly, keys load from env or file, and the default (no key) path stays plaintext.
"""
import sqlite3
from pathlib import Path

import pytest

pytestmark = pytest.mark.native_sqlcipher


@pytest.fixture(autouse=True)
def _require_sqlcipher():
    """Defer the native import until after sqlite-vec integration tests finish.

    sqlite-vec and SQLCipher expose incompatible SQLite ABIs when loaded into
    the same interpreter.  The suite orders their marked integration tests so
    both real backends are still exercised without risking a native crash.
    """
    pytest.importorskip("sqlcipher3", reason="encryption extra not installed")

from engraphis.backends import encrypted_db  # noqa: E402
from engraphis.core.store import Store  # noqa: E402
from engraphis.service import MemoryService  # noqa: E402

KEY = "b3" * 32  # 64 hex chars → raw-key form


def _hits(res):
    items = res.get("memories") or res.get("chunks") or res.get("results") or []
    return [i.get("content", "") for i in items]


def test_encrypts_at_rest_unreadable_without_key(monkeypatch, tmp_path):
    import sqlcipher3

    monkeypatch.setenv("ENGRAPHIS_DB_KEY", KEY)
    db = str(tmp_path / "m.db")
    svc = MemoryService.create(db)
    svc.remember("Postgres 16 is the primary database.", workspace="demo", scope="workspace")
    svc.engine.store.conn.close()
    with open(db, "rb") as f:
        assert not f.read(16).startswith(b"SQLite format 3")   # not plaintext SQLite
    with pytest.raises(sqlite3.DatabaseError):                  # stdlib can't read it
        sqlite3.connect(db).execute("SELECT * FROM memories").fetchone()
    c = sqlcipher3.connect(db)                                  # sqlcipher WITH key can
    c.execute("PRAGMA key = \"x'%s'\"" % KEY)
    assert c.execute("SELECT count(*) FROM memories").fetchone()[0] >= 1
    c.close()


def test_recall_and_reopen_work_encrypted(monkeypatch, tmp_path):
    monkeypatch.setenv("ENGRAPHIS_DB_KEY", KEY)
    db = str(tmp_path / "m.db")
    svc = MemoryService.create(db)
    stored = svc.remember(
        "Deploys run Fridays at noon.", workspace="demo", scope="workspace", title="Deploy"
    )
    # Service writes from an agent are deliberately pending until a local reviewer
    # approves them for prompt use.  Prove encrypted reopen preserves both content
    # and that governance decision rather than weakening the recall policy here.
    svc.engine.approve_for_prompt(
        stored["id"], reviewer="test_operator", reason="encrypted reopen fixture"
    )
    svc.engine.store.conn.close()
    # Re-open runs the idempotent ALTER TABLE migration → sqlcipher raises its OWN
    # OperationalError; without the translating adapter the core's except would miss it.
    svc2 = MemoryService.create(db)
    assert any("Friday" in c for c in _hits(svc2.recall("deploy schedule", workspace="demo")))
    svc2.engine.store.conn.close()


def test_wrong_key_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("ENGRAPHIS_DB_KEY", KEY)
    db = str(tmp_path / "m.db")
    MemoryService.create(db).engine.store.conn.close()
    monkeypatch.setenv("ENGRAPHIS_DB_KEY", "aa" * 32)      # different key
    with pytest.raises(encrypted_db.EncryptionError):
        MemoryService.create(db)


def test_existing_encrypted_database_opens_read_only_without_sidecar_mutation(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("ENGRAPHIS_DB_KEY", KEY)
    db_path = tmp_path / "read-only-encrypted.db"
    service = MemoryService.create(str(db_path))
    stored = service.remember(
        "Encrypted immutable evidence.", workspace="demo", scope="workspace",
    )
    service.engine.store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    service.engine.store.close()
    tracked = [
        db_path,
        tmp_path / "read-only-encrypted.db-wal",
        tmp_path / "read-only-encrypted.db-shm",
        tmp_path / "read-only-encrypted.db-journal",
    ]

    def state(path):
        return (
            path.exists(),
            path.stat().st_size if path.exists() else None,
            path.stat().st_mtime_ns if path.exists() else None,
        )

    before = {path.name: state(path) for path in tracked}
    read_only = Store(
        str(db_path), connect=encrypted_db.make_connector(KEY), read_only=True,
    )
    try:
        record = read_only.get_memory(stored["id"])
        assert record is not None and record.content == "Encrypted immutable evidence."
        assert read_only.conn.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            read_only.conn.execute(
                "UPDATE memories SET content='changed' WHERE id=?", (stored["id"],)
            )
    finally:
        read_only.close()
    assert {path.name: state(path) for path in tracked} == before


def test_encrypted_read_only_open_rejects_wrong_key(monkeypatch, tmp_path):
    monkeypatch.setenv("ENGRAPHIS_DB_KEY", KEY)
    db_path = tmp_path / "wrong-read-only-key.db"
    service = MemoryService.create(str(db_path))
    service.engine.store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    service.engine.store.close()
    tracked = [
        db_path,
        tmp_path / "wrong-read-only-key.db-wal",
        tmp_path / "wrong-read-only-key.db-shm",
        tmp_path / "wrong-read-only-key.db-journal",
    ]

    def state(path):
        return (
            path.exists(),
            path.stat().st_size if path.exists() else None,
            path.stat().st_mtime_ns if path.exists() else None,
        )

    before = {path.name: state(path) for path in tracked}

    with pytest.raises(encrypted_db.EncryptionError):
        Store(
            str(db_path), connect=encrypted_db.make_connector("aa" * 32),
            read_only=True,
        )
    assert {path.name: state(path) for path in tracked} == before


def test_encrypted_read_only_open_rejects_active_wal_before_connector(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("ENGRAPHIS_DB_KEY", KEY)
    db_path = tmp_path / "active-encrypted-wal.db"
    writable = Store(str(db_path), connect=encrypted_db.make_connector(KEY))
    try:
        writable.conn.execute("PRAGMA wal_autocheckpoint=0")
        writable.get_or_create_workspace("active-encrypted-wal")
        wal_path = tmp_path / "active-encrypted-wal.db-wal"
        assert wal_path.is_file() and wal_path.stat().st_size > 0
        before = (wal_path.stat().st_size, wal_path.stat().st_mtime_ns)

        with pytest.raises(RuntimeError, match="active WAL found"):
            Store(
                str(db_path), connect=encrypted_db.make_connector(KEY),
                read_only=True,
            )
        assert (wal_path.stat().st_size, wal_path.stat().st_mtime_ns) == before
    finally:
        writable.close()


def test_key_from_file(monkeypatch, tmp_path):
    keyfile = tmp_path / "db.key"
    keyfile.write_text(KEY + "\n")
    monkeypatch.delenv("ENGRAPHIS_DB_KEY", raising=False)
    monkeypatch.setenv("ENGRAPHIS_DB_KEY_FILE", str(keyfile))
    db = str(tmp_path / "m.db")
    svc = MemoryService.create(db)
    svc.remember("keyfile content", workspace="w", scope="workspace")
    svc.engine.store.conn.close()
    with pytest.raises(sqlite3.DatabaseError):
        sqlite3.connect(db).execute("SELECT * FROM memories").fetchone()


def test_encrypted_manifest_snapshot_is_immutable_and_read_only(monkeypatch, tmp_path):
    monkeypatch.setenv("ENGRAPHIS_DB_KEY", KEY)
    db = str(tmp_path / "manifest.db")
    service = MemoryService.create(db)
    workspace_id = service.store.get_or_create_workspace("encrypted-manifest")
    vault_id = service.store.register_source_vault(
        kind="obsidian", root_digest="a" * 64, workspace_id=workspace_id,
        display_name="Encrypted vault",
    )
    source_id = service.store.upsert_source_import_item(
        vault_id=vault_id, source_key="b" * 64, relative_path="Notes/One.md",
        content_sha256="c" * 64, importer_version="1", seen_at=10,
    )
    service.store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    service.close()
    tracked = [
        Path(db), Path(db + "-wal"), Path(db + "-shm"), Path(db + "-journal"),
    ]

    def state(path):
        return (
            path.exists(),
            path.stat().st_size if path.exists() else None,
            path.stat().st_mtime_ns if path.exists() else None,
        )

    before = {path.name: state(path) for path in tracked}

    snapshot = Store.snapshot_source_import_manifest(
        db, connect=encrypted_db.connector_from_env(),
    )

    assert snapshot["schema_version"] >= 15
    assert [vault["id"] for vault in snapshot["vaults"]] == [vault_id]
    assert [item["id"] for item in snapshot["items"]] == [source_id]
    assert {path.name: state(path) for path in tracked} == before


def test_passphrase_key_non_hex(monkeypatch, tmp_path):
    monkeypatch.setenv("ENGRAPHIS_DB_KEY", "correct horse battery staple")  # → passphrase (KDF)
    db = str(tmp_path / "m.db")
    svc = MemoryService.create(db)
    svc.remember("passphrase content", workspace="w", scope="workspace")
    svc.engine.store.conn.close()
    with pytest.raises(sqlite3.DatabaseError):
        sqlite3.connect(db).execute("SELECT * FROM memories").fetchone()


def test_no_key_is_plaintext_and_backward_compatible(monkeypatch, tmp_path):
    monkeypatch.delenv("ENGRAPHIS_DB_KEY", raising=False)
    monkeypatch.delenv("ENGRAPHIS_DB_KEY_FILE", raising=False)
    assert encrypted_db.connector_from_env() is None
    db = str(tmp_path / "m.db")
    svc = MemoryService.create(db)
    svc.remember("plain content", workspace="w", scope="workspace")
    svc.engine.store.conn.close()
    assert sqlite3.connect(db).execute("SELECT count(*) FROM memories").fetchone()[0] == 1


def test_key_pragma_escapes_quotes():
    # a passphrase containing a quote must not break out of the SQL literal
    assert encrypted_db._key_pragma("ab'; DROP") == "PRAGMA key = 'ab''; DROP'"
    # a 64-hex key uses the raw blob form
    assert encrypted_db._key_pragma("ff" * 32).startswith("PRAGMA key = \"x'")
