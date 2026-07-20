"""Folder transport — sync via any shared directory.

The zero-infrastructure, self-hostable tier of cloud sync: point two or more
devices at the same folder that is *already* replicated between them — a Dropbox /
iCloud Drive / OneDrive folder, a Syncthing share, a mounted network drive, or even
a git repo you push/pull — and Engraphis handles the memory-aware merge on top.
This is the same free path Obsidian users cobble together by hand, except the merge
is deterministic instead of "conflicted copy" files.

It implements the ``SyncTransport`` Protocol (``core/interfaces.py``): opaque named
byte blobs, no knowledge of memory semantics. Each device writes exactly one
full-state bundle (``bundle-<device_id>.json``) and overwrites it each sync, so the
folder stays small and there is nothing to garbage-collect. Writes are atomic
(temp file + ``os.replace``) so a half-written bundle is never observed — the same
mount-safe discipline the rest of the repo uses (AGENTS.md §7).

The managed TLS relay (the headline Pro upsell) is a different ``SyncTransport``
implementation that plugs in here unchanged. Client-side end-to-end encryption is a
documented follow-up; today's relay stores opaque but plaintext bundle bytes at rest.
"""
from __future__ import annotations

import heapq
import os
import re
import secrets
import stat
from pathlib import Path
from typing import Optional

MAX_BUNDLE_BYTES = 256 * 1024 * 1024  # skip absurdly large blobs before reading them
MAX_TOTAL_PULL_BYTES = 256 * 1024 * 1024
MAX_BUNDLES = 64
MAX_DIRECTORY_ENTRIES = 10_000
MAX_BUNDLE_NAME_CHARS = 200


def _safe_name(name: object) -> str:
    raw = str(name or "").strip()
    value = os.path.basename(raw)
    if (
        value != raw
        or len(value) > MAX_BUNDLE_NAME_CHARS
        or not value.endswith(".json")
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value) is None
    ):
        return ""
    return value


class FolderTransport:
    """A ``SyncTransport`` backed by a shared filesystem directory.

    ``root`` is created if missing. Only ``*.json`` files are treated as bundles, so
    dropping a README or other files in the folder is harmless.
    """

    def __init__(self, root: str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def push(self, name: str, data: bytes) -> None:
        """Atomically write ``data`` to ``root/<name>`` (temp + fsync + os.replace).

        The shared folder is untrusted on the *write* side too: a hostile peer can
        pre-plant a symlink at a predictable temp path so our own write follows it
        and clobbers an arbitrary local file. Defend by using an unpredictable temp
        name and opening with ``O_CREAT|O_EXCL|O_NOFOLLOW`` so the temp file must be
        a brand-new regular file we created ourselves. ``os.replace`` then swaps the
        directory entry itself, which never follows a symlink at ``dest``.
        """
        if len(data) > MAX_BUNDLE_BYTES:
            raise ValueError(
                f"sync bundle exceeds the {MAX_BUNDLE_BYTES}-byte transport limit"
            )
        safe = _safe_name(name)
        if not safe:
            raise ValueError("sync bundle name is invalid")
        dest = self.root / safe
        tmp = self.root / f"{safe}.{secrets.token_hex(8)}.tmp"
        flags = (
            os.O_WRONLY | os.O_CREAT | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        )
        # 0o644 (pre-umask) keeps bundles readable by peer accounts on multi-user
        # shares, matching the plain open() behavior this replaced; O_EXCL already
        # guarantees we created the file ourselves.
        fd = os.open(tmp, flags, 0o644)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, dest)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def pull(self) -> list[tuple[str, bytes]]:
        """Return ``(name, data)`` for every bundle currently in the folder.

        Oversized files are skipped rather than read, bounding memory use if the
        shared folder ever holds a corrupt or hostile blob (defense in depth — the
        sync engine also caps row counts once the JSON is parsed)."""
        out: list[tuple[str, bytes]] = []
        total = 0
        for p in self._bundle_paths():
            data = self._read_regular_bundle(p)
            if data is None:
                continue
            if total + len(data) > MAX_TOTAL_PULL_BYTES:
                continue
            out.append((p.name, data))
            total += len(data)
        return out

    def list_names(self) -> list[str]:
        return [p.name for p in self._bundle_paths()]

    def _bundle_paths(self) -> list[Path]:
        """Return a deterministic, bounded set of regular bundle files.

        The shared folder is untrusted. Do not follow symlinks, and do not materialize an
        unbounded directory listing merely to sort it.
        """
        def candidates():
            try:
                with os.scandir(self.root) as entries:
                    for index, entry in enumerate(entries):
                        if index >= MAX_DIRECTORY_ENTRIES:
                            break
                        try:
                            if not entry.is_file(follow_symlinks=False):
                                continue
                        except OSError:
                            continue
                        if _safe_name(entry.name) == entry.name:
                            yield Path(entry.path)
            except OSError:
                return

        return heapq.nsmallest(MAX_BUNDLES, candidates(), key=lambda path: path.name)

    @staticmethod
    def _read_regular_bundle(path: Path) -> Optional[bytes]:
        """Open one bundle without following a symlink swapped in after enumeration."""
        fd = -1
        try:
            before = os.lstat(path)
            if not stat.S_ISREG(before.st_mode):
                return None
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(path, flags)
            opened = os.fstat(fd)
            if (
                not stat.S_ISREG(opened.st_mode)
                or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
                or opened.st_size > MAX_BUNDLE_BYTES
            ):
                return None
            with os.fdopen(fd, "rb") as fh:
                fd = -1
                data = fh.read(MAX_BUNDLE_BYTES + 1)
            return data if len(data) <= MAX_BUNDLE_BYTES else None
        except OSError:
            return None  # peer mid-write/replaced file; retry on the next sync pass
        finally:
            if fd >= 0:
                os.close(fd)


def get_transport(kind: str = "folder", **kw):
    """Factory mirroring ``get_embedder``/``get_vector_index`` — select a transport by
    name so swapping the folder backend for the managed relay is a config change.

    - ``folder`` (default): shared-directory sync. Requires ``root=<shared directory>``.
    - ``relay``: the managed Pro relay transport (``RelayTransport``). Requires
      ``base_url=<relay root>`` and ``workspace_id=<namespace>`` (use the workspace
      *name*, so every authorized device on the account shares one namespace);
      ``license_key`` is a compatibility parameter for a scoped bearer token and
      ``timeout`` is optional. The token defaults to the saved per-user sync token.

    Both implement the ``SyncTransport`` protocol (``core/interfaces.py``) and plug into
    ``SyncEngine.sync`` unchanged. ``relay`` is imported lazily so a folder-only install
    never pays for it and ``core`` stays dependency-light (the client is stdlib-only)."""
    if kind in ("folder", "auto"):
        root = kw.get("root")
        if not root:
            raise ValueError("folder transport requires root=<shared directory>")
        return FolderTransport(root)
    if kind == "relay":
        base_url = kw.get("base_url")
        workspace_id = kw.get("workspace_id")
        if not base_url:
            raise ValueError("relay transport requires base_url=<relay root>")
        if not workspace_id:
            raise ValueError("relay transport requires workspace_id=<namespace>")
        from engraphis.backends.sync_relay import RelayTransport
        return RelayTransport(base_url, workspace_id,
                              license_key=kw.get("license_key"),
                              timeout=kw.get("timeout", 30.0))
    raise ValueError("unknown sync transport %r (have: folder, relay)" % kind)
