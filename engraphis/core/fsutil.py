"""Shared filesystem safety utilities.

Extracted from duplicated definitions across documents.py, obsidian.py,
resources.py, and routes/vault.py to a single canonical location with zero
dependency risk (only stdlib os + stat).
"""
from __future__ import annotations

import stat


def is_reparse_point(info: object) -> bool:
    """Return whether *info* carries the Windows reparse-point attribute.

    Reparse points include symlinks, junctions, and other NTFS indirections that
    can escape a sandboxed root or redirect reads to unexpected targets.  The
    check is safe on non-Windows platforms where the attribute constant is absent.
    """
    marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(getattr(info, "st_file_attributes", 0) & marker)
