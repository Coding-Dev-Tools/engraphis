"""Unit tests for the shared filesystem safety helper."""
from __future__ import annotations

import stat
from types import SimpleNamespace

from engraphis.core.fsutil import (
    is_cloud_placeholder,
    is_link_indirection,
    is_reparse_point,
)


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


def _attrs(**flags: int) -> int:
    return sum(flags.values())


def test_cloud_placeholder_detected():
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    recall = getattr(stat, "FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS", 0x400000)
    info = SimpleNamespace(st_file_attributes=_attrs(REPARSE=reparse, RECALL=recall))
    assert is_cloud_placeholder(info) is True
    # A placeholder must NOT be treated as a link indirection: OneDrive
    # Files-On-Demand files hydrate on open instead of redirecting reads.
    assert is_link_indirection(info) is False


def test_symlink_junction_still_blocked():
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    info = SimpleNamespace(st_file_attributes=reparse)
    assert is_cloud_placeholder(info) is False
    assert is_link_indirection(info) is True


def test_placeholder_helpers_false_when_attribute_missing():
    # Non-Windows stat_result carries no st_file_attributes at all.
    info = SimpleNamespace()
    assert is_cloud_placeholder(info) is False
    assert is_link_indirection(info) is False
