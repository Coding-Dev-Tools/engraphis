#!/usr/bin/env python3
"""Watch a repository for file changes and trigger incremental reindex.

Uses polling-based mtime detection by default (no external dependencies).
Optionally uses ``watchdog`` for inotify/FSEvents when available.

Examples::

    # Poll every 5 seconds (default)
    python -m scripts.watch_repo --db engraphis.db --workspace acme --repo backend

    # One-shot scan (no watching)
    python -m scripts.watch_repo --db engraphis.db --workspace acme --repo backend --no-watch

    # Custom poll interval
    python -m scripts.watch_repo --db engraphis.db --workspace acme --repo backend --interval 2
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import os
import signal
import sys
from pathlib import Path

logger = logging.getLogger("engraphis.watch_repo")

# File extensions worth reindexing.  Tree-sitter covers more, but these are the
# high-signal set that catches most code changes without thrashing on config/docs.
_WATCHED_EXTENSIONS = frozenset({
    ".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rs", ".java",
    ".c", ".cpp", ".h", ".hpp", ".cs", ".rb", ".php", ".swift",
    ".kt", ".scala", ".lua", ".sh", ".bash", ".zsh",
})


class _PollingWatcher:
    """Poll-based detector using content-backed file signatures.

    No external dependencies. Scans the repo root for files matching
    ``_WATCHED_EXTENSIONS`` and compares nanosecond mtime, size, and a bounded
    digest. The digest catches same-size rewrites whose mtime was preserved or
    restored by build and sync tools.
    """

    def __init__(self, root: Path, interval: float = 5.0) -> None:
        self.root = root
        self.interval = max(1.0, interval)
        self._signatures: dict[str, tuple[int, int, bytes]] = {}
        self._initial_scan_done = False

    def _scan(self) -> dict[str, tuple[int, int, bytes]]:
        """Walk the tree and collect content-backed signatures."""
        signatures: dict[str, tuple[int, int, bytes]] = {}
        for dirpath, _dirnames, filenames in os.walk(self.root):
            for fname in filenames:
                ext = os.path.splitext(fname)[1].lower()
                if ext not in _WATCHED_EXTENSIONS:
                    continue
                full = os.path.join(dirpath, fname)
                try:
                    digest = hashlib.blake2b(digest_size=16)
                    with open(full, "rb") as handle:
                        info = os.fstat(handle.fileno())
                        while True:
                            chunk = handle.read(64 * 1024)
                            if not chunk:
                                break
                            digest.update(chunk)
                    signatures[full] = (
                        int(getattr(info, "st_mtime_ns", info.st_mtime * 1_000_000_000)),
                        int(info.st_size),
                        digest.digest(),
                    )
                except OSError:
                    pass
        return signatures

    def poll(self) -> list[str]:
        """Return list of changed file paths since last poll.

        On first call, records baseline and returns empty (no changes yet).
        """
        current = self._scan()
        if not self._initial_scan_done:
            self._signatures = current
            self._initial_scan_done = True
            return []

        changed: list[str] = []
        # Detect modified or new files, including backdated/same-mtime rewrites.
        for path, signature in current.items():
            if self._signatures.get(path) != signature:
                changed.append(path)
        # Detect deleted files (trigger reindex to clean stale symbols).
        for path in self._signatures:
            if path not in current:
                changed.append(path)

        self._signatures = current
        return changed


def _try_watchdog_watcher(root: Path, callback, stop_event, startup_reconcile):
    """Watch while queuing events that arrive during startup reconciliation."""
    try:
        from watchdog.observers import Observer  # type: ignore[import-not-found]
        from watchdog.events import FileSystemEventHandler  # type: ignore[import-not-found]
    except ImportError:
        return None

    import threading

    startup_done = threading.Event()
    pending_paths: list[str] = []
    pending_lock = threading.Lock()
    callback_lock = threading.Lock()

    def dispatch(paths):
        unique = list(dict.fromkeys(paths))
        if unique:
            with callback_lock:
                callback(unique)

    def enqueue(paths):
        with pending_lock:
            if not startup_done.is_set():
                pending_paths.extend(paths)
                return
        dispatch(paths)

    class _Handler(FileSystemEventHandler):
        def on_modified(self, event):
            if not event.is_directory:
                ext = os.path.splitext(event.src_path)[1].lower()
                if ext in _WATCHED_EXTENSIONS:
                    enqueue([event.src_path])

        def on_created(self, event):
            self.on_modified(event)

        def on_deleted(self, event):
            self.on_modified(event)

        def on_moved(self, event):
            if event.is_directory:
                return
            paths = [
                path
                for path in (event.src_path, event.dest_path)
                if os.path.splitext(path)[1].lower() in _WATCHED_EXTENSIONS
            ]
            if paths:
                enqueue(paths)

    observer = Observer()
    observer.schedule(_Handler(), str(root), recursive=True)
    observer.start()
    logger.info("watchdog observer started on %s", root)
    try:
        if not startup_reconcile():
            return 1
        with pending_lock:
            startup_done.set()
            startup_paths = list(pending_paths)
            pending_paths.clear()
        dispatch(startup_paths)
        while not stop_event.is_set():
            stop_event.wait(timeout=1.0)
    finally:
        observer.stop()
        observer.join()
    return 0


def _run(args, engine) -> int:
    wid_row = engine.store.conn.execute(
        "SELECT id FROM workspaces WHERE name=?", (args.workspace,)
    ).fetchone()
    if not wid_row:
        print(f"error: no workspace '{args.workspace}' in {args.db}", file=sys.stderr)
        return 2
    wid = wid_row["id"]
    rid_row = engine.store.conn.execute(
        "SELECT id, root_path FROM repos WHERE workspace_id=? AND name=?",
        (wid, args.repo),
    ).fetchone()
    if not rid_row:
        print(f"error: no repo '{args.repo}' in workspace '{args.workspace}'",
              file=sys.stderr)
        return 2
    rid = rid_row["id"]
    root_path = rid_row["root_path"]
    if not root_path or not os.path.isdir(root_path):
        print(f"error: repo root '{root_path}' is not a directory", file=sys.stderr)
        return 2

    root = Path(root_path)

    def reindex(paths: list[str], *, fail_full: bool = False) -> bool:
        if not paths and not fail_full:
            return True
        try:
            if fail_full:
                result = engine.index_repo(rid, root)
            else:
                result = engine.index_repo_incremental(rid, root, paths)
        except Exception as exc:
            logger.error("%s reindex failed: %s",
                         "startup" if fail_full else "incremental", exc)
            return False
        failed = int(result.get("files_failed", 0))
        if failed:
            logger.error(
                "%s reindex incomplete: %d file(s) failed",
                "startup" if fail_full else "incremental",
                failed,
            )
            return False
        logger.info(
            "%s reindex complete: %d changed, %d unchanged, %d removed",
            "startup" if fail_full else "incremental",
            result.get("files_indexed", 0),
            result.get("files_unchanged", 0),
            result.get("files_removed", 0),
        )
        return True

    if args.no_watch:
        if not reindex([], fail_full=True):
            return 1
        print("Reindex complete.")
        return 0

    import threading
    stop_event = threading.Event()

    def _shutdown(signum, frame):
        logger.info("shutdown signal received")
        stop_event.set()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    watchdog_status = _try_watchdog_watcher(
        root, reindex, stop_event,
        lambda: reindex([], fail_full=True),
    )
    if watchdog_status is not None:
        return watchdog_status

    logger.info("watchdog not available; using polling (interval=%.1fs)", args.interval)
    watcher = _PollingWatcher(root, interval=args.interval)
    # Establish the polling baseline before the full startup reconciliation so
    # edits made during the scan are replayed after it completes.
    watcher.poll()
    if not reindex([], fail_full=True):
        return 1
    changed_during_startup = watcher.poll()
    if changed_during_startup:
        reindex(changed_during_startup)
    print(f"Watching {root} (poll every {args.interval}s, Ctrl+C to stop)...")

    while not stop_event.is_set():
        stop_event.wait(timeout=watcher.interval)
        if stop_event.is_set():
            break
        changed = watcher.poll()
        if changed:
            reindex(changed)

    print("Stopped.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Watch a repository and trigger incremental reindex on changes."
    )
    ap.add_argument("--db", required=True, help="Path to the v2 database file.")
    ap.add_argument("--workspace", required=True, help="Workspace name.")
    ap.add_argument("--repo", required=True, help="Repo name (must already be indexed).")
    ap.add_argument("--interval", type=float, default=5.0,
                    help="Poll interval in seconds (default 5).")
    ap.add_argument("--no-watch", action="store_true",
                    help="One-shot full reconciliation, then exit.")
    args = ap.parse_args(argv)

    from engraphis.core.engine import MemoryEngine

    engine = MemoryEngine.create(args.db)
    try:
        return _run(args, engine)
    finally:
        close_index = getattr(engine.index, "close", None)
        try:
            if callable(close_index):
                close_index()
        finally:
            engine.store.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    sys.exit(main())
