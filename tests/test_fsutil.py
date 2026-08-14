"""Unit tests for the shared filesystem safety helper."""
from __future__ import annotations

import stat
from types import SimpleNamespace

from engraphis.core.fsutil import is_reparse_point


def test_reparse_point_absent_on_plain_file():
    info = SimpleNamespace(st_file_attributes=0)
    assert is_reparse_point(info) is False


def test_reparse_point_detected_when_bit_set():
    marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    info = SimpleNamespace(st_file_attributes=marker)
    assert is_reparse_point(info) is True


def test_reparse_point_ignores_other_attribute_bits():
    # Other Windows attribute bits (readonly, hidden, archive, ...) must not
    # trigger a positive detection.
    other_bits = 0x027  # READONLY | HIDDEN | SYSTEM | ARCHIVE
    info = SimpleNamespace(st_file_attributes=other_bits)
    assert is_reparse_point(info) is False


def test_reparse_point_returns_false_when_attribute_missing():
    # Non-Windows stat_result carries no st_file_attributes; the helper must
    # degrade to False rather than raise.
    info = SimpleNamespace()
    assert is_reparse_point(info) is False
