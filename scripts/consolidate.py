#!/usr/bin/env python3
"""Sleep-time consolidation as a schedulable local job (MASTER_PLAN.md Phase 4).

Your machine, your schedule — no cloud service involved. Examples::

    # See what would happen (recommended first run)
    python -m scripts.consolidate --db engraphis.db --workspace acme --dry-run

    # Run for real; distill recurring episodes + archive decayed transients
    python -m scripts.consolidate --db engraphis.db --workspace acme

    # Nicer digests via the configured LLM (falls back to deterministic on error)
    python -m scripts.consolidate --db engraphis.db --workspace acme --llm

Schedule it (cron)::         0 3 * * *  cd /path/to/repo && python -m scripts.consolidate --db engraphis.db --workspace acme
Schedule it (Windows)::      schtasks /Create /SC DAILY /ST 03:00 /TN EngraphisConsolidate /TR "python -m scripts.consolidate --db C:\\path\\engraphis.db --workspace acme"
"""
from __future__ import annotations

import argparse
import json
import sys

from engraphis.core.engine import MemoryEngine


def main() -> int:
    ap = argparse.ArgumentParser(description="Run one Engraphis consolidation sweep.")
    ap.add_argument("--db", required=True, help="Path to the v2 database file.")
    ap.add_argument("--workspace", required=True, help="Workspace name to consolidate.")
    ap.add_argument("--repo", default=None, help="Restrict to one repo name.")
    ap.add_argument("--dry-run", action="store_true", help="Report only; change nothing.")
    ap.add_argument("--min-cluster", type=int, default=3,
                    help="Recurrences before an episodic pattern is digested (default 3).")
    ap.add_argument("--archive-below", type=float, default=0.05,
                    help="Retention floor for archiving transients (default 0.05).")
    ap.add_argument("--llm", action="store_true",
                    help="Summarize digests with the configured LLM (.env) instead of "
                         "the deterministic digest text.")
    args = ap.parse_args()

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
            print(f"error: no repo named '{args.repo}' in workspace "
                  f"'{args.workspace}'", file=sys.stderr)
            return 2
        rid = rid_row["id"]

    llm = None
    if args.llm:
        try:
            from engraphis.llm.client import LLMClient
            llm = LLMClient()
        except Exception as exc:  # noqa: BLE001
            print(f"warning: LLM unavailable ({exc}); using deterministic digests",
                  file=sys.stderr)

    report = engine.consolidate(
        workspace_id=wid_row["id"], repo_id=rid, dry_run=args.dry_run,
        min_cluster=args.min_cluster, archive_below=args.archive_below, llm=llm,
    )
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
