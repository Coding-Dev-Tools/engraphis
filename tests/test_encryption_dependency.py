"""Dependency-light SQLCipher failure behavior on platforms without a bundled driver."""
import sqlite3
import sys
import traceback
import types

import pytest

from engraphis.backends import encrypted_db
from engraphis.core.store import Store


def test_missing_driver_message_does_not_loop_on_unsupported_platforms(monkeypatch):
    monkeypatch.setitem(sys.modules, "sqlcipher3", None)
    with pytest.raises(encrypted_db.EncryptionError) as exc:
        encrypted_db.make_connector("test-key")
    message = str(exc.value)
    assert "CPython manylinux x86-64" in message
    assert "macOS, Windows, Linux ARM, or musl" in message
    assert "will not fall back to plaintext" in message


def test_key_file_error_redacts_private_path_and_exception_chain(monkeypatch, tmp_path):
    private_path = tmp_path / "customer-secret-database-key"
    monkeypatch.delenv("ENGRAPHIS_DB_KEY", raising=False)
    monkeypatch.setenv("ENGRAPHIS_DB_KEY_FILE", str(private_path))

    with pytest.raises(encrypted_db.EncryptionError) as exc_info:
        encrypted_db._resolve_key()

    rendered = "".join(
        traceback.format_exception(
            type(exc_info.value),
            exc_info.value,
            exc_info.value.__traceback__,
        )
    )
    assert str(private_path) not in str(exc_info.value)
    assert str(private_path) not in rendered


@pytest.mark.parametrize(
    ("stage", "expected"),
    [
        ("connect", "could not initialize"),
        ("pragma", "failed to apply"),
        ("header", "could not open"),
    ],
)
def test_connector_setup_errors_redact_paths_keys_and_driver_chains(
    monkeypatch, tmp_path, stage, expected,
):
    marker = "sqlcipher-driver-secret"
    key = "inline-key-secret"
    private_path = tmp_path / "private-customer.db"

    class _Raw:
        def execute(self, statement):
            if statement.startswith("PRAGMA"):
                if stage == "pragma":
                    raise RuntimeError(f"{marker}:{statement}")
                return self
            if stage == "header":
                raise RuntimeError(f"{marker}:{private_path}")
            return self

        def fetchone(self):
            return (1,)

        def close(self):
            raise RuntimeError(f"{marker}:close")

    def connect(path, **_kwargs):
        if stage == "connect":
            raise RuntimeError(f"{marker}:{path}")
        return _Raw()

    driver = types.SimpleNamespace(connect=connect, Row=object)
    monkeypatch.setattr(encrypted_db.importlib, "import_module", lambda _name: driver)
    connector = encrypted_db.make_connector(key)

    with pytest.raises(encrypted_db.EncryptionError, match=expected) as exc_info:
        connector(str(private_path))

    rendered = "".join(
        traceback.format_exception(
            type(exc_info.value),
            exc_info.value,
            exc_info.value.__traceback__,
        )
    )
    for secret in (marker, key, str(private_path)):
        assert secret not in str(exc_info.value)
        assert secret not in rendered


def test_encrypted_connector_read_only_contract_uses_immutable_uri(
    monkeypatch, tmp_path,
):
    database = tmp_path / "existing-encrypted.db"
    database.write_bytes(b"opaque encrypted fixture")
    calls = []
    statements = []

    class _Result:
        def fetchone(self):
            return (1,)

    class _Raw:
        row_factory = None

        def execute(self, statement):
            statements.append(statement)
            return _Result()

        def close(self):
            return None

    def connect(target, **options):
        calls.append((target, options))
        return _Raw()

    driver = types.SimpleNamespace(connect=connect, Row=object)
    monkeypatch.setattr(encrypted_db.importlib, "import_module", lambda _name: driver)
    connector = encrypted_db.make_connector("fixture-key")
    connection = connector.open_read_only(str(database))
    connection.close()

    target, options = calls[0]
    assert target == database.resolve().as_uri() + "?mode=ro&immutable=1"
    assert options == {"timeout": 30, "check_same_thread": False, "uri": True}
    assert statements == [
        encrypted_db._key_pragma("fixture-key"),
        "PRAGMA query_only=ON",
        "SELECT count(*) FROM sqlite_master",
    ]


def test_encrypted_read_only_missing_path_never_creates_or_calls_driver(
    monkeypatch, tmp_path,
):
    calls = []

    def connect(target, **options):
        calls.append((target, options))
        raise AssertionError("driver must not be called for a missing snapshot")

    driver = types.SimpleNamespace(connect=connect, Row=object)
    monkeypatch.setattr(encrypted_db.importlib, "import_module", lambda _name: driver)
    connector = encrypted_db.make_connector("fixture-key")
    missing = tmp_path / "not-created" / "encrypted.db"

    with pytest.raises(RuntimeError, match="existing regular database"):
        Store(str(missing), connect=connector, read_only=True)

    assert calls == []
    assert not missing.parent.exists()
    assert not missing.exists()


def test_driver_exception_translation_is_limited_to_stdlib_exception_classes():
    driver_operational_error = type("OperationalError", (Exception,), {})("locked")
    translated = encrypted_db._translate_exc(driver_operational_error)
    assert isinstance(translated, sqlite3.OperationalError)
    assert str(translated) == "locked"

    driver_base_exception = type("KeyboardInterrupt", (Exception,), {})("stop")
    fallback = encrypted_db._translate_exc(driver_base_exception)
    assert type(fallback) is sqlite3.Error


_DriverOperationalError = type(
    "OperationalError",
    (Exception,),
    {"__module__": "sqlcipher3.dbapi2"},
)


class _FailingCursor:
    description = (("value",),)

    def execute(self, *args, **_kwargs):
        if args and args[0] == "FAIL":
            raise _DriverOperationalError("cursor execute failed")
        return self

    def executemany(self, *_args, **_kwargs):
        return self

    def executescript(self, *_args, **_kwargs):
        return self

    def fetchone(self):
        raise _DriverOperationalError("fetchone failed")

    def fetchmany(self):
        raise _DriverOperationalError("fetchmany failed")

    def fetchall(self):
        raise _DriverOperationalError("fetchall failed")

    def __iter__(self):
        return self

    def __next__(self):
        raise _DriverOperationalError("iteration failed")

    def close(self):
        raise _DriverOperationalError("cursor close failed")

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        raise _DriverOperationalError("cursor exit failed")


class _FailingConnection:
    def execute(self, *_args, **_kwargs):
        return _FailingCursor()

    def executemany(self, *_args, **_kwargs):
        return _FailingCursor()

    def executescript(self, *_args, **_kwargs):
        return _FailingCursor()

    def cursor(self, *_args, **_kwargs):
        return _FailingCursor()

    def commit(self):
        raise _DriverOperationalError("commit failed")

    def rollback(self):
        raise _DriverOperationalError("rollback failed")

    def close(self):
        raise _DriverOperationalError("connection close failed")

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        raise _DriverOperationalError("connection exit failed")


class _EntryFailingConnection(_FailingConnection):
    def execute(self, *_args, **_kwargs):
        raise _DriverOperationalError("connection execute failed")

    def executemany(self, *_args, **_kwargs):
        raise _DriverOperationalError("connection executemany failed")

    def executescript(self, *_args, **_kwargs):
        raise _DriverOperationalError("connection executescript failed")


def test_cursor_result_lifecycle_translates_driver_errors():
    cursor = encrypted_db._TranslatingCursor(_FailingCursor())

    for operation, message in (
        (cursor.fetchone, "fetchone failed"),
        (cursor.fetchmany, "fetchmany failed"),
        (cursor.fetchall, "fetchall failed"),
        (lambda: next(cursor), "iteration failed"),
        (cursor.close, "cursor close failed"),
    ):
        with pytest.raises(sqlite3.OperationalError, match=message):
            operation()

    with pytest.raises(sqlite3.OperationalError, match="cursor execute failed"):
        cursor.execute("FAIL")

    with pytest.raises(sqlite3.OperationalError, match="cursor exit failed"):
        cursor.__exit__(None, None, None)


def test_connection_direct_statements_return_translating_cursors():
    connection = encrypted_db._TranslatingConnection(_FailingConnection())

    for cursor in (
        connection.execute("SELECT 1"),
        connection.executemany("SELECT 1", ()),
        connection.executescript("SELECT 1"),
        connection.cursor(),
    ):
        assert isinstance(cursor, encrypted_db._TranslatingCursor)
        with pytest.raises(sqlite3.OperationalError, match="fetchone failed"):
            cursor.fetchone()


def test_connection_statement_entry_translates_driver_errors():
    connection = encrypted_db._TranslatingConnection(_EntryFailingConnection())

    for operation, message in (
        (lambda: connection.execute("SELECT 1"), "connection execute failed"),
        (
            lambda: connection.executemany("SELECT 1", ()),
            "connection executemany failed",
        ),
        (
            lambda: connection.executescript("SELECT 1"),
            "connection executescript failed",
        ),
    ):
        with pytest.raises(sqlite3.OperationalError, match=message):
            operation()


def test_connection_lifecycle_translates_driver_errors():
    connection = encrypted_db._TranslatingConnection(_FailingConnection())

    for operation, message in (
        (connection.commit, "commit failed"),
        (connection.rollback, "rollback failed"),
        (connection.close, "connection close failed"),
    ):
        with pytest.raises(sqlite3.OperationalError, match=message):
            operation()

    with pytest.raises(sqlite3.OperationalError, match="connection exit failed"):
        connection.__exit__(None, None, None)
