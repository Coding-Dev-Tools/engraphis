#!/usr/bin/env python3
"""Cloud sync as a local command — your machine, your folder, your keys.

Point two or more devices at one shared folder (Dropbox / iCloud / OneDrive /
Syncthing / a network drive / a git repo) and sync your Engraphis memory store
across all of them, with deterministic conflict resolution — no "conflicted copy"
files, no lost notes. Examples::

    # Preview what a sync would change (recommended first run — never writes)
    python -m scripts.sync --db engraphis.db --workspace acme --remote "D:/Dropbox/engraphis" --dry-run

    # Sync for real: publish this device's snapshot, pull + merge every other device's
    python -m scripts.sync --db engraphis.db --workspace acme --remote "D:/Dropbox/engraphis"

Schedule it (cron)::      */15 * * * *  cd /path/to/repo && python -m scripts.sync --db engraphis.db --workspace acme --remote ~/Dropbox/engraphis
Schedule it (Windows)::   schtasks /Create /SC MINUTE /MO 15 /TN EngraphisSync /TR "python -m scripts.sync --db C:\\path\\engraphis.db --workspace acme --remote C:\\Users\\me\\Dropbox\\engraphis"

Cloud sync is a Pro feature. The gate lives HERE (via the same ``require_feature``
helper the Inspector uses); the core engine in ``engraphis/core/sync.py`` never
checks a license. Start a free 3-day trial from the dashboard's Settings → License
panel (one click, no key) to try it.
"""
from __future__ import annotations

import argparse
import json
import sys

from engraphis.core.engine import MemoryEngine
from engraphis.core.sync import SyncEngine


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Sync an Engraphis workspace across devices.")
    ap.add_argument("--db", required=True, help="Path to the v2 database file.")
    ap.add_argument("--workspace", required=True, help="Workspace name to sync.")
    ap.add_argument("--remote", required=True, metavar="DIR",
                    help="Shared folder both devices can see (Dropbox/iCloud/Syncthing/…).")
    ap.add_argument("--repo", default=None, help="Restrict the sync to one repo name.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Report what would change; write nothing (locally or to the folder).")
    args = ap.parse_args(argv)

    # ── Pro gate (checked up front, before touching the DB or the folder) ──────
    from engraphis.licensing import LicenseError, require_feature
    try:
        require_feature("sync")
    except LicenseError as exc:
        print(f"error: cloud sync is a Pro feature. {exc}", file=sys.stderr)
        return 2

    engine = MemoryEngine.create(args.db)
    wid_row = engine.store.conn.execute(
        "SELECT id FROM workspaces WHERE name=?", (args.workspace,)).fetchone()
    if not wid_row:
        print(f"error: no workspace named '{args.workspace}' in {args.db}", file=sys.stderr)
        return 2
    rid = None
    if args.repo:
        rid_row = engine.store.conn.execute(
            "SELECT id FROM repos WHERE workspace_id=? AND name=?",
            (wid_row["id"], args.repo)).fetchone()
        if not rid_row:
            print(f"error: no repo named '{args.repo}' in workspace '{args.workspace}'",
                  file=sys.stderr)
            return 2
        rid = rid_row["id"]

    try:
        from engraphis.backends.sync_folder import get_transport
        transport = get_transport("folder", root=args.remote)
    except (ValueError, OSError) as exc:
        print(f"error: could not open sync folder '{args.remote}': {exc}", file=sys.stderr)
        return 2

    from engraphis.config import settings
    engine_sync = SyncEngine(engine.store, embedder=engine.embedder,
                             vector_index=engine.index,
                             allowed_workspaces=settings.allowed_workspaces or None)
    report = engine_sync.sync(transport, wid_row["id"], repo_id=rid, dry_run=args.dry_run)
    print(json.dumps(report, indent=2))

    t = report["totals"]
    verb = "would sync" if args.dry_run else "synced"
    print(
        f"{verb}: exported {report['exported_memories']} memories · "
        f"pulled {report['peers_applied']} peer(s) · "
        f"+{t['added']} new, {t['updated']} updated, {t['unchanged']} unchanged, "
        f"+{t['links_added']} links"
        + (f" · {t['rejected']} rejected" if t.get("rejected") else ""),
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
