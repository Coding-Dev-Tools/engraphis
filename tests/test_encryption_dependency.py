"""Dependency-light SQLCipher failure behavior on platforms without a bundled driver."""
import sys

import pytest

from engraphis.backends import encrypted_db


def test_missing_driver_message_does_not_loop_on_unsupported_platforms(monkeypatch):
    monkeypatch.setitem(sys.modules, "sqlcipher3", None)
    with pytest.raises(encrypted_db.EncryptionError) as exc:
        encrypted_db.make_connector("test-key")
    message = str(exc.value)
    assert "CPython manylinux x86-64" in message
    assert "macOS, Windows, Linux ARM, or musl" in message
    assert "will not fall back to plaintext" in message
