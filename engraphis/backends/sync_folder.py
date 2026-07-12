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

The managed, end-to-end-encrypted relay (the headline Pro upsell) is a different
``SyncTransport`` implementation that plugs in here unchanged; this backend is what
makes the feature real and testable today.
"""
from __future__ import annotations

import os
from pathlib import Path

MAX_BUNDLE_BYTES = 256 * 1024 * 1024  # skip absurdly large blobs before reading them


class FolderTransport:
    """A ``SyncTransport`` backed by a shared filesystem directory.

    ``root`` is created if missing. Only ``*.json`` files are treated as bundles, so
    dropping a README or other files in the folder is harmless.
    """

    def __init__(self, root: str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def push(self, name: str, data: bytes) -> None:
        """Atomically write ``data`` to ``root/<name>`` (temp + fsync + os.replace)."""
        safe = os.path.basename(name)  # never let a bundle name escape the folder
        dest = self.root / safe
        tmp = self.root / (safe + ".tmp")
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, dest)

    def pull(self) -> list[tuple[str, bytes]]:
        """Return ``(name, data)`` for every bundle currently in the folder.

        Oversized files are skipped rather than read, bounding memory use if the
        shared folder ever holds a corrupt or hostile blob (defense in depth — the
        sync engine also caps row counts once the JSON is parsed)."""
        out: list[tuple[str, bytes]] = []
        for p in sorted(self.root.glob("*.json")):
            try:
                if p.stat().st_size > MAX_BUNDLE_BYTES:
                    continue
                out.append((p.name, p.read_bytes()))
            except OSError:
                continue  # a peer mid-write; skip this pass, catch it next sync
        return out

    def list_names(self) -> list[str]:
        return [p.name for p in sorted(self.root.glob("*.json"))]


def get_transport(kind: str = "folder", **kw):
    """Factory mirroring ``get_embedder``/``get_vector_index`` — select a transport by
    name so swapping the folder backend for the managed relay is a config change.

    - ``folder`` (default): shared-directory sync. Requires ``root=<shared directory>``.
    - ``relay``: the managed Pro relay transport (``RelayTransport``). Requires
      ``base_url=<relay root>`` and ``workspace_id=<namespace>`` (use the workspace
      *name*, so every device on the account shares one namespace); ``license_key`` and
      ``timeout`` are optional (the key defaults to this device's configured license).

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
