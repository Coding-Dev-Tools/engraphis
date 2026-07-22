"""Small, dependency-free primitives for security-sensitive local state files.

These helpers deliberately protect the final pathname component.  State directories may
legitimately live on mounted or symlinked persistent volumes, but a credential/state leaf
must never be read through a link/reparse point or replaced after a pathname race.
"""
from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path
from typing import Optional


_UNSET = object()

# On Windows, Python 3.12 can report different ``st_ctime_ns`` values for ``lstat``
# and ``fstat`` of the same unchanged file. Windows historically exposes creation time
# through this field (and deprecated that meaning in 3.12), so it is not a reliable
# cross-handle version signal there. Identity, size, and nanosecond mtime remain stable;
# POSIX keeps ctime as the additional metadata/change detector.
_VERSION_FIELDS = (
    ("st_size", "st_mtime_ns")
    if os.name == "nt"
    else ("st_size", "st_mtime_ns", "st_ctime_ns")
)


class UnsafeStateFile(OSError):
    """A private-state path is not a stable, single-link regular file."""


def _unsafe(path: Path, reason: str) -> UnsafeStateFile:
    return UnsafeStateFile("unsafe private state file %s: %s" % (path, reason))


def _checked_lstat(path: Path, *, allow_missing: bool = False):
    try:
        info = os.lstat(str(path))
    except FileNotFoundError:
        if allow_missing:
            return None
        raise
    attributes = getattr(info, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if stat.S_ISLNK(info.st_mode) or (reparse_flag and attributes & reparse_flag):
        raise _unsafe(path, "links and reparse points are not accepted")
    if not stat.S_ISREG(info.st_mode):
        raise _unsafe(path, "expected a regular file")
    # A hard-linked credential can disclose updates through another pathname.  Normal
    # files have one link; fail closed rather than silently preserving that alias.
    if getattr(info, "st_nlink", 1) != 1:
        raise _unsafe(path, "hard-linked files are not accepted")
    return info


def private_file_stat(path: Path, *, allow_missing: bool = False):
    """Return a validated ``lstat`` result for a private state leaf."""
    return _checked_lstat(Path(path), allow_missing=allow_missing)


def _same_file(left, right) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _same_version(left, right) -> bool:
    """Identity plus metadata that changes on an in-place rewrite."""
    return _same_file(left, right) and all(
        getattr(left, name, None) == getattr(right, name, None)
        for name in _VERSION_FIELDS
    )


def _fsync_parent(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(
        str(path.parent), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def read_private_text(path: Path, *, max_bytes: int,
                      allow_missing: bool = False) -> Optional[str]:
    """Read bounded UTF-8 from a stable, non-linked regular file.

    The pre-open ``lstat``, ``O_NOFOLLOW`` where supported, and descriptor/path identity
    checks close both the ordinary symlink case and a swap between inspection and open.
    """
    path = Path(path)
    before = _checked_lstat(path, allow_missing=allow_missing)
    if before is None:
        return None
    if before.st_size > max_bytes:
        raise _unsafe(path, "file exceeds %d bytes" % max_bytes)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(str(path), flags)
    except FileNotFoundError:
        if allow_missing:
            return None
        raise
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or not _same_file(before, opened):
            raise _unsafe(path, "path changed while it was opened")
        if getattr(opened, "st_nlink", 1) != 1:
            raise _unsafe(path, "hard-linked files are not accepted")
        data = bytearray()
        while len(data) <= max_bytes:
            chunk = os.read(descriptor, min(65536, max_bytes + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) > max_bytes:
            raise _unsafe(path, "file exceeds %d bytes" % max_bytes)
        after = os.fstat(descriptor)
        current = _checked_lstat(path)
        if not _same_version(opened, after) or not _same_version(after, current):
            raise _unsafe(path, "file changed while it was read")
    finally:
        os.close(descriptor)
    try:
        return bytes(data).decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise _unsafe(path, "file is not valid UTF-8") from None


def atomic_private_text(path: Path, value: str, *, mode: int = 0o600,
                        expected_stat=_UNSET) -> None:
    """Atomically replace a private leaf through an exclusive randomized temp file.

    A concurrent appearance or replacement is treated as a conflict instead of being
    overwritten.  This is intentionally stricter than a generic atomic-write helper.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    before = _checked_lstat(path, allow_missing=True)
    if expected_stat is not _UNSET:
        if expected_stat is None and before is not None:
            raise _unsafe(path, "file appeared after it was read")
        if (expected_stat is not None
                and (before is None or not _same_version(expected_stat, before))):
            raise _unsafe(path, "file changed after it was read")
    fd, name = tempfile.mkstemp(prefix=".%s." % path.name, dir=str(path.parent))
    temporary = Path(name)
    try:
        fchmod = getattr(os, "fchmod", None)
        if fchmod is not None:
            try:
                fchmod(fd, mode)
            except OSError:
                pass
        payload = value.encode("utf-8")
        offset = 0
        while offset < len(payload):
            offset += os.write(fd, payload[offset:])
        os.fsync(fd)
        os.close(fd)
        fd = -1
        current = _checked_lstat(path, allow_missing=True)
        if before is None and current is not None:
            raise _unsafe(path, "file appeared during atomic write")
        if before is not None and (current is None or not _same_version(before, current)):
            raise _unsafe(path, "file changed during atomic write")
        os.replace(str(temporary), str(path))
        _fsync_parent(path)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def publish_private_text_if_absent(path: Path, value: str, *, mode: int = 0o600) -> bool:
    """Publish *value* without replacing an existing pathname.

    Returns ``True`` for the winner and ``False`` when another process already created
    the leaf.  Callers must validate/read the winner with :func:`read_private_text`.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _checked_lstat(path, allow_missing=True)
    fd, name = tempfile.mkstemp(prefix=".%s." % path.name, dir=str(path.parent))
    temporary = Path(name)
    try:
        fchmod = getattr(os, "fchmod", None)
        if fchmod is not None:
            try:
                fchmod(fd, mode)
            except OSError:
                pass
        payload = value.encode("utf-8")
        offset = 0
        while offset < len(payload):
            offset += os.write(fd, payload[offset:])
        os.fsync(fd)
        os.close(fd)
        fd = -1
        try:
            os.link(str(temporary), str(path))
        except FileExistsError:
            return False
        temporary.unlink()
        _fsync_parent(path)
        return True
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
