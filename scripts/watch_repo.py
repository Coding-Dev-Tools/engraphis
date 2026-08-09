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

from engraphis.backends.codegraph import LANG_BY_EXT, _DEFAULT_EXCLUDE_DIRS, load_ignore_patterns

logger = logging.getLogger("engraphis.watch_repo")

# File extensions worth reindexing. Keep this aligned with the codegraph indexer's
# supported extensions so the watcher follows the same language coverage as
# indexing, instead of drifting to a stale hand-maintained subset.
_WATCHED_EXTENSIONS = frozenset(LANG_BY_EXT)


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
        # Paths whose last reindex failed and are absent from the current scan.
        # Without this set, a failed deletion reindex would never retry because
        # the path is gone from both _signatures and the new scan on next poll.
        self._pending_deletions: set[str] = set()
        # Paths detected as deletions in the most recent poll() call.
        # Used by untrack() to distinguish deletion retries from other failures.
        self._last_deletions: set[str] = set()
        # Apply the same directory/name pruning the code indexer uses so the
        # watcher does not repeatedly hash every file under node_modules,
        # vendor, or build-output trees that will never be indexed anyway.
        # ``.engraphisignore`` at the repo root adds project-specific rules.
        self._exclude_dirs: set[str] = set(_DEFAULT_EXCLUDE_DIRS)
        try:
            ignore_names, _ignore_globs, unignore = load_ignore_patterns(str(root))
        except Exception:  # noqa: BLE001 — a broken ignore file must not abort scanning
            ignore_names, unignore = set(), set()
        self._exclude_dirs |= ignore_names
        self._exclude_dirs -= unignore

    def _scan(self) -> dict[str, tuple[int, int, bytes]]:
        """Walk the tree and collect content-backed signatures."""
        signatures: dict[str, tuple[int, int, bytes]] = {}
        for dirpath, dirnames, filenames in os.walk(self.root):
            # Prune excluded directories in-place so os.walk does not descend.
            dirnames[:] = [
                d for d in dirnames if d not in self._exclude_dirs
                and not d.startswith(".")
            ]
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
            self._last_deletions = set()
            return []

        changed: list[str] = []
        # Detect modified or new files, including backdated/same-mtime rewrites.
        for path, signature in current.items():
            if self._signatures.get(path) != signature:
                changed.append(path)
        # Detect deleted files (trigger reindex to clean stale symbols).
        deletions: set[str] = set()
        for path in self._signatures:
            if path not in current:
                changed.append(path)
                deletions.add(path)
        self._last_deletions = deletions

        # Re-emit paths from prior failed deletions that are still absent.
        # These were removed from _signatures by untrack() after the previous
        # failed reindex, so they would otherwise be invisible to this poll.
        still_missing = {
            p for p in self._pending_deletions if p not in current
        }
        changed.extend(still_missing)
        # Clear entries that have reappeared (file recreated between polls).
        self._pending_deletions -= set(current.keys())

        self._signatures = current
        return changed

    def untrack(self, paths: list[str], *, deletions: bool = False) -> None:
        """Remove *paths* from the signature cache.

        After a failed reindex the caller can ask the watcher to forget these
        files so the next :meth:`poll` reports them as new/changed again and
        the indexer gets another chance instead of silently dropping them.

        When *deletions* is True, paths that are already absent from
        ``_signatures`` are added to a pending-deletion set so they remain
        visible to future polls even though they no longer exist on disk.
        Without this flag (the default for created/modified file retries),
        absent paths are silently ignored.
        """
        for path in paths:
            if self._signatures.pop(path, None) is not None:
                continue
            if deletions and path in self._last_deletions:
                self._pending_deletions.add(path)


def _try_watchdog_watcher(root: Path, callback, stop_event):
    """Attempt watchdog-based watching.  Returns True if started, False if unavailable."""
    try:
        from watchdog.observers import Observer  # type: ignore[import-not-found]
        from watchdog.events import FileSystemEventHandler  # type: ignore[import-not-found]
    except ImportError:
        return False

    _MAX_RETRIES = 3

    class _Handler(FileSystemEventHandler):
        def _dispatch(self, paths: list[str]) -> None:
            for attempt in range(1, _MAX_RETRIES + 1):
                if callback(paths):
                    return
                logger.warning(
                    "watchdog reindex failed (attempt %d/%d) for %d file(s)",
                    attempt, _MAX_RETRIES, len(paths),
                )
                stop_event.wait(timeout=min(2 ** attempt, 8))
            logger.error(
                "watchdog reindex permanently failed after %d attempts for %s",
                _MAX_RETRIES, paths,
            )

        def on_modified(self, event):
            if not event.is_directory:
                ext = os.path.splitext(event.src_path)[1].lower()
                if ext in _WATCHED_EXTENSIONS:
                    self._dispatch([event.src_path])

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
                self._dispatch(paths)

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

    # Reconcile persisted code state before establishing any in-process watcher
    # baseline. This catches edits, renames, and deletions made while the watcher
    # was stopped and works for both one-shot and continuous modes.
    if not reindex([], fail_full=True):
        return 1
    if args.no_watch:
        print("Reindex complete.")
        return 0

    import threading
    stop_event = threading.Event()

    def _shutdown(signum, frame):
        logger.info("shutdown signal received")
        stop_event.set()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    if _try_watchdog_watcher(root, reindex, stop_event):
        return 0

    logger.info("watchdog not available; using polling (interval=%.1fs)", args.interval)
    watcher = _PollingWatcher(root, interval=args.interval)
    watcher.poll()
    print(f"Watching {root} (poll every {args.interval}s, Ctrl+C to stop)...")

    while not stop_event.is_set():
        stop_event.wait(timeout=watcher.interval)
        if stop_event.is_set():
            break
        changed = watcher.poll()
        if changed:
            if not reindex(changed):
                logger.warning(
                    "incremental reindex failed for %d file(s); "
                    "will retry on next poll cycle",
                    len(changed),
                )
                watcher.untrack(changed, deletions=True)

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
