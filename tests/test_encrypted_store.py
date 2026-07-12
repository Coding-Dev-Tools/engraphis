"""Encryption at rest (SQLCipher) — opt-in whole-DB AES-256 encryption of the memory DB.

Skips unless the optional sqlcipher3 driver is installed (``pip install engraphis[encryption]``),
like the fastapi/mcp extras — so the numpy-only offline gate stays green. With the driver
present it proves: the file is unreadable without the key, recall + re-open work through the
encrypted connection (the re-open path exercises the exception-translating adapter), a wrong
key fails loudly, keys load from env or file, and the default (no key) path stays plaintext.
"""
import sqlite3

import pytest

sqlcipher3 = pytest.importorskip("sqlcipher3", reason="encryption extra not installed")

from engraphis.backends import encrypted_db  # noqa: E402
from engraphis.service import MemoryService  # noqa: E402

KEY = "b3" * 32  # 64 hex chars → raw-key form


def _hits(res):
    items = res.get("memories") or res.get("chunks") or res.get("results") or []
    return [i.get("content", "") for i in items]


def test_encrypts_at_rest_unreadable_without_key(monkeypatch, tmp_path):
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
    svc.remember("Deploys run Fridays at noon.", workspace="demo", scope="workspace", title="Deploy")
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
