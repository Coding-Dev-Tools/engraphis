"""Source path exclusions and bounded, identity-checked local reads."""
from __future__ import annotations

import os
import re
import stat
from pathlib import Path


_HAS_DESCRIPTOR_WALK = (
    os.open in os.supports_dir_fd and os.stat in os.supports_dir_fd
)


SENSITIVE_FILENAMES = {
    ".env", "credentials", "credentials.json", "id_rsa", "id_dsa",
    ".netrc", "_netrc", ".npmrc", ".pypirc", ".git-credentials", ".pgpass", "pgpass.conf",
    ".aws", ".ssh", ".gnupg", ".azure", ".kube", ".docker", ".engraphis",
    "id_ecdsa", "id_ed25519", "authorized_keys", "known_hosts",
    "recovery-codes", "recovery_codes", "tokens", "tokens.json",
    "secrets", "secrets.json", "secret", "secret.json",
}
SENSITIVE_DIRECTORIES = SENSITIVE_FILENAMES | {
    "credential", "token", ".credential", ".credentials", ".secrets", ".secret", ".token", ".tokens",
}


def sensitive_directory(name: str) -> bool:
    """Exclude known credential components without broad word-substring matching."""
    lowered = name.casefold()
    return (lowered in SENSITIVE_DIRECTORIES or lowered.startswith(".env.")
            or lowered.endswith((".pem", ".key", ".p12", ".pfx"))
            or bool(re.fullmatch(r"\.?recovery[-_ ]?codes?", lowered)))


def sensitive_filename(name: str) -> bool:
    """Keep the broader credential heuristic for actual file basenames."""
    return sensitive_directory(name) or bool(
        re.search(r"(?:credential|recovery[-_ ]?code|secret|token)", name.casefold())
    )


def sensitive_path(path: str, *, is_directory: bool = False) -> bool:
    """Check selected components lexically, treating a known directory root explicitly.

    Both separators are interpreted lexically, including for uploaded Windows paths.
    Generic hidden directories are left to each scanner's existing relative policy;
    an allowed root under a benign hidden worktree remains usable.
    """
    parts = [part for part in path.replace("\\", "/").split("/") if part]
    directories = parts if is_directory else parts[:-1]
    return any(sensitive_directory(part) for part in directories) or (
        bool(parts) and not is_directory and sensitive_filename(parts[-1])
    )


def _link_indirection(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _same_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def read_local_source(path: Path, *, max_bytes: int) -> bytes:
    """Read an already approved canonical file, rejecting entry substitution.

    POSIX opens every directory component relative to its checked descriptor with
    ``O_NOFOLLOW``. Platforms without that facility (including Windows) retain
    lstat/fstat identity checks, reject reparse points, and recheck the complete
    parent chain before reading and before returning any bytes. The caller must
    still authorize the canonical path against its operator-owned root policy.
    """
    if not path.is_absolute() or sensitive_path(str(path)):
        raise ValueError("unsafe source path")
    parents = list(reversed(path.parents))
    parent_info = [(parent, os.lstat(parent)) for parent in parents]
    for _, info in parent_info:
        if not stat.S_ISDIR(info.st_mode) or _link_indirection(info):
            raise ValueError("unsafe source directory")
    before = os.lstat(path)
    if not stat.S_ISREG(before.st_mode) or _link_indirection(before):
        raise ValueError("unsafe source file type")

    def check_path() -> None:
        for parent, initial in parent_info:
            current = os.lstat(parent)
            if (_link_indirection(current) or not stat.S_ISDIR(current.st_mode)
                    or not _same_identity(initial, current)):
                raise ValueError("source directory changed during read")
        current = os.lstat(path)
        if (_link_indirection(current) or not stat.S_ISREG(current.st_mode)
                or not _same_identity(before, current) or path.resolve(strict=True) != path):
            raise ValueError("source file changed during read")

    check_path()
    directory_fds: list[int] = []
    file_fd = None
    try:
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        directory_flag = getattr(os, "O_DIRECTORY", 0)
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
        if _HAS_DESCRIPTOR_WALK and nofollow and directory_flag:
            directory_flags = os.O_RDONLY | directory_flag | nofollow
            for index, (parent, initial) in enumerate(parent_info):
                if index == 0:
                    fd = os.open(parent, directory_flags)
                else:
                    fd = os.open(parent.name, directory_flags, dir_fd=directory_fds[-1])
                directory_fds.append(fd)
                if not _same_identity(initial, os.fstat(fd)):
                    raise ValueError("source directory changed during open")
            file_fd = os.open(path.name, flags | nofollow, dir_fd=directory_fds[-1])
        else:
            file_fd = os.open(path, flags | nofollow)
        opened = os.fstat(file_fd)
        if (not stat.S_ISREG(opened.st_mode) or _link_indirection(opened)
                or not _same_identity(before, opened)
                or before.st_size != opened.st_size
                or before.st_mtime_ns != opened.st_mtime_ns
                or before.st_ctime_ns != opened.st_ctime_ns):
            raise ValueError("source file changed during open")
        check_path()
        if opened.st_size > max_bytes:
            raise ValueError(f"source file exceeds {max_bytes} bytes")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(file_fd, min(64 * 1024, max_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise ValueError(f"source file exceeds {max_bytes} bytes")
        finished = os.fstat(file_fd)
        if (not _same_identity(opened, finished) or opened.st_size != finished.st_size
                or opened.st_mtime_ns != finished.st_mtime_ns
                or opened.st_ctime_ns != finished.st_ctime_ns):
            raise ValueError("source file changed during read")
        check_path()
        return b"".join(chunks)
    finally:
        if file_fd is not None:
            os.close(file_fd)
        for fd in reversed(directory_fds):
            os.close(fd)
