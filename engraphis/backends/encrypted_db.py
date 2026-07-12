"""Optional encryption-at-rest for the memory database (SQLCipher).

OFF by default — the core Store keeps using stdlib ``sqlite3`` and the numpy-only floor is
unchanged. Set ``ENGRAPHIS_DB_KEY`` (or ``ENGRAPHIS_DB_KEY_FILE``) and install the extra
(``pip install "engraphis[encryption]"``) to transparently encrypt the whole database file
with AES-256 via SQLCipher. Because encryption is whole-file, full-text search, the graph
tables, and every query keep working unchanged — unlike field-level encryption, which would
blind the lexical recall arm.

Design: the core Store (``engraphis/core/store.py``) stays stdlib-only and simply accepts an
optional connection factory. This module provides that factory. SQLCipher's driver
(``sqlcipher3``) raises its OWN exception classes, which the stdlib-only core does not catch,
so we wrap the connection in a tiny adapter that re-raises the matching ``sqlite3`` exception
— the core's ``except sqlite3.OperationalError`` handlers then work against an encrypted DB.
"""
from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path
from typing import Callable, Optional

_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")


class EncryptionError(RuntimeError):
    """Encryption was requested (a key is set) but could not be honored — missing driver,
    wrong key, or an unreadable key file. Fail loud: never silently fall back to plaintext."""


def _resolve_key() -> Optional[str]:
    """Return the configured DB key, or None if encryption is not configured.

    Precedence: ``ENGRAPHIS_DB_KEY`` (inline) then ``ENGRAPHIS_DB_KEY_FILE`` (path to a file
    containing the key). A 64-hex-char value is used as a raw 32-byte key (no KDF); anything
    else is treated as a passphrase (SQLCipher KDFs it, PBKDF2 256k)."""
    inline = os.environ.get("ENGRAPHIS_DB_KEY", "").strip()
    if inline:
        return inline
    path = os.environ.get("ENGRAPHIS_DB_KEY_FILE", "").strip()
    if path:
        try:
            key = Path(path).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise EncryptionError(
                "ENGRAPHIS_DB_KEY_FILE=%s could not be read: %s" % (path, exc)) from exc
        if not key:
            raise EncryptionError("ENGRAPHIS_DB_KEY_FILE=%s is empty" % path)
        return key
    return None


def is_enabled() -> bool:
    return _resolve_key() is not None


def _key_pragma(key: str) -> str:
    """Build the ``PRAGMA key`` statement. Raw 32-byte keys use the ``x'..'`` blob form
    (no quoting risk — hex only); passphrases are single-quoted with quotes doubled so a
    passphrase can never break out of the literal (defense against a key with a quote)."""
    if _HEX64.match(key):
        return "PRAGMA key = \"x'%s'\"" % key.lower()
    return "PRAGMA key = '%s'" % key.replace("'", "''")


def _translate_exc(exc: Exception) -> Exception:
    """Map a sqlcipher3 exception to the stdlib ``sqlite3`` class of the same name so the
    stdlib-only core's ``except sqlite3.*`` handlers catch it."""
    target = getattr(sqlite3, type(exc).__name__, sqlite3.Error)
    if not (isinstance(target, type) and issubclass(target, BaseException)):
        target = sqlite3.Error
    return target(*exc.args)


def _guard(fn, *args, **kwargs):
    """Call *fn*, re-raising any sqlcipher3 exception as its stdlib equivalent. Non-driver
    exceptions propagate unchanged (never masked)."""
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - re-raised as the stdlib equivalent
        if type(exc).__module__.startswith("sqlcipher3"):
            raise _translate_exc(exc) from exc
        raise


class _TranslatingCursor:
    """A cursor whose statement methods translate sqlcipher3 exceptions. Returned by
    :meth:`_TranslatingConnection.cursor` so error handling holds even for code that drives
    a cursor directly (the core doesn't today, but this closes the gap for future callers)."""

    def __init__(self, raw) -> None:
        object.__setattr__(self, "_raw", raw)

    def __getattr__(self, name):
        return getattr(self._raw, name)

    def __setattr__(self, name, value):
        setattr(self._raw, name, value)

    def __iter__(self):
        return iter(self._raw)

    def execute(self, *a, **k):
        _guard(self._raw.execute, *a, **k)
        return self

    def executemany(self, *a, **k):
        _guard(self._raw.executemany, *a, **k)
        return self

    def executescript(self, *a, **k):
        _guard(self._raw.executescript, *a, **k)
        return self


class _TranslatingConnection:
    """Adapts a sqlcipher3 connection so it raises stdlib ``sqlite3`` exceptions.

    The stdlib-only core catches ``sqlite3.OperationalError``/``IntegrityError``; sqlcipher3
    raises unrelated classes of the same name. We translate on the statement-executing
    methods (and cursors) and pass everything else through."""

    def __init__(self, raw) -> None:
        object.__setattr__(self, "_raw", raw)

    def __getattr__(self, name):
        return getattr(self._raw, name)

    def __setattr__(self, name, value):
        setattr(self._raw, name, value)

    def execute(self, *a, **k):
        return _guard(self._raw.execute, *a, **k)

    def executescript(self, *a, **k):
        return _guard(self._raw.executescript, *a, **k)

    def executemany(self, *a, **k):
        return _guard(self._raw.executemany, *a, **k)

    def commit(self):
        return _guard(self._raw.commit)

    def cursor(self, *a, **k):
        return _TranslatingCursor(self._raw.cursor(*a, **k))

    def __enter__(self):
        self._raw.__enter__()
        return self

    def __exit__(self, *exc):
        return self._raw.__exit__(*exc)


def make_connector(key: str) -> Callable[[str], object]:
    """Return a ``connect(path) -> connection`` factory that opens *path* as an encrypted
    SQLCipher database keyed with *key*. Raises :class:`EncryptionError` with an actionable
    message if the driver is missing or the key does not unlock an existing file."""
    try:
        import sqlcipher3  # noqa: F401  (optional dependency)
    except Exception as exc:  # noqa: BLE001
        raise EncryptionError(
            "ENGRAPHIS_DB_KEY is set but the SQLCipher driver is not installed. "
            "Install it with:  pip install \"engraphis[encryption]\"") from exc

    pragma = _key_pragma(key)

    def _connect(path: str):
        import sqlcipher3
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        raw = sqlcipher3.connect(path, timeout=30, check_same_thread=False)
        try:
            raw.execute(pragma)                   # MUST be the first statement
        except Exception:  # noqa: BLE001
            raw.close()
            # Suppress the driver message (`from None`): a PRAGMA syntax error can echo the
            # statement text, which contains the key. Never surface key material.
            raise EncryptionError(
                "failed to apply the database key — check the ENGRAPHIS_DB_KEY format") from None
        try:
            # Touch the header so a wrong key / plaintext-vs-encrypted mismatch fails now,
            # with a clear message, instead of deep inside an unrelated query later.
            raw.execute("SELECT count(*) FROM sqlite_master").fetchone()
        except Exception as exc:  # noqa: BLE001
            raw.close()
            raise EncryptionError(
                "could not open the encrypted database at %s — wrong ENGRAPHIS_DB_KEY, or "
                "the file is not SQLCipher-encrypted (an existing plaintext DB cannot be "
                "opened with a key; migrate it first)." % path) from exc
        raw.row_factory = sqlcipher3.Row
        return _TranslatingConnection(raw)

    return _connect


def connector_from_env() -> Optional[Callable[[str], object]]:
    """The connection factory for the current environment, or None when encryption is off.

    Callers pass the result to ``Store(path, connect=...)`` / ``MemoryEngine.create`` /
    ``MemoryService.create``. None means "use the stdlib sqlite3 default" (plaintext)."""
    key = _resolve_key()
    if key is None:
        return None
    return make_connector(key)
