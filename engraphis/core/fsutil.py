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


# Cloud-files placeholders (OneDrive Files-On-Demand et al.) are reparse points, but
# unlike symlinks/junctions they name a real tree item that hydrates transparently on
# open — reading one never redirects outside its path. Both attribute constants are
# only ever set together with the reparse attribute on placeholder entries.
_PLACEHOLDER_ATTRS = (
    getattr(stat, "FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS", 0x400000)
    | getattr(stat, "FILE_ATTRIBUTE_RECALL_ON_OPEN", 0x40000)
)


def is_cloud_placeholder(info: object) -> bool:
    """Return whether *info* is a cloud-files placeholder rather than a link.

    Such entries must be allowed through link guards: rejecting them makes every
    not-locally-cached OneDrive file unimportable on Windows even though opening
    the file is safe and simply downloads it.
    """
    attributes = getattr(info, "st_file_attributes", 0)
    return bool(attributes & _PLACEHOLDER_ATTRS)


def is_link_indirection(info: object) -> bool:
    """Return whether *info* is a reparse point that must stay rejected: any symlink
    or junction — i.e. a reparse point that is not a benign cloud placeholder."""
    return is_reparse_point(info) and not is_cloud_placeholder(info)
