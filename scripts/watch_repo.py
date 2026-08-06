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
import logging
import os
import signal
import sys
import time
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
    """Poll-based file change detector using os.stat mtime comparison.

    No external dependencies.  Scans the repo root for files matching
    ``_WATCHED_EXTENSIONS`` and compares mtimes against the last known state.
    """

    def __init__(self, root: Path, interval: float = 5.0) -> None:
        self.root = root
        self.interval = max(1.0, interval)
        self._mtimes: dict[str, float] = {}
        self._initial_scan_done = False

    def _scan(self) -> dict[str, float]:
        """Walk the tree and collect mtimes for watched extensions."""
        mtimes: dict[str, float] = {}
        for dirpath, _dirnames, filenames in os.walk(self.root):
            for fname in filenames:
                ext = os.path.splitext(fname)[1].lower()
                if ext not in _WATCHED_EXTENSIONS:
                    continue
                full = os.path.join(dirpath, fname)
                try:
                    mtimes[full] = os.stat(full).st_mtime
                except OSError:
                    pass
        return mtimes

    def poll(self) -> list[str]:
        """Return list of changed file paths since last poll.

        On first call, records baseline and returns empty (no changes yet).
        """
        current = self._scan()
        if not self._initial_scan_done:
            self._mtimes = current
            self._initial_scan_done = True
            return []

        changed: list[str] = []
        # Detect modified or new files.
        for path, mtime in current.items():
            old = self._mtimes.get(path)
            if old is None or mtime > old:
                changed.append(path)
        # Detect deleted files (trigger reindex to clean stale symbols).
        for path in self._mtimes:
            if path not in current:
                changed.append(path)

        self._mtimes = current
        return changed


def _try_watchdog_watcher(root: Path, callback, stop_event):
    """Attempt watchdog-based watching.  Returns True if started, False if unavailable."""
    try:
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler
    except ImportError:
        return False

    class _Handler(FileSystemEventHandler):
        def on_modified(self, event):
            if not event.is_directory:
                ext = os.path.splitext(event.src_path)[1].lower()
                if ext in _WATCHED_EXTENSIONS:
                    callback([event.src_path])

        def on_created(self, event):
            self.on_modified(event)

        def on_deleted(self, event):
            self.on_modified(event)

    observer = Observer()
    observer.schedule(_Handler(), str(root), recursive=True)
    observer.start()
    logger.info("watchdog observer started on %s", root)
    try:
        while not stop_event.is_set():
            stop_event.wait(timeout=1.0)
    finally:
        observer.stop()
        observer.join()
    return True


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
                    help="One-shot scan: detect and reindex changes, then exit.")
    args = ap.parse_args(argv)

    from engraphis.core.engine import MemoryEngine

    engine = MemoryEngine.create(args.db)
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

    def reindex(paths: list[str]) -> None:
        if not paths:
            return
        logger.info("reindexing %d changed file(s)", len(paths))
        try:
            result = engine.index_repo_incremental(rid, root, paths)
            scanned = result.get("files_scanned", 0)
            symbols = result.get("symbols_indexed", 0)
            logger.info("reindex complete: %d files, %d symbols", scanned, symbols)
        except Exception as exc:
            logger.error("reindex failed: %s", exc)

    if args.no_watch:
        watcher = _PollingWatcher(root, interval=args.interval)
        watcher.poll()  # baseline
        time.sleep(0.1)
        changed = watcher.poll()
        if changed:
            reindex(changed)
            print(f"Reindexed {len(changed)} changed file(s).")
        else:
            print("No changes detected.")
        return 0

    # Graceful shutdown on SIGINT/SIGTERM.
    import threading
    stop_event = threading.Event()

    def _shutdown(signum, frame):
        logger.info("shutdown signal received")
        stop_event.set()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # Try watchdog first; fall back to polling.
    if _try_watchdog_watcher(root, reindex, stop_event):
        return 0

    logger.info("watchdog not available; using polling (interval=%.1fs)", args.interval)
    watcher = _PollingWatcher(root, interval=args.interval)
    watcher.poll()  # baseline
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


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    sys.exit(main())
