#!/usr/bin/env python3
"""Run one explicit local consolidation sweep.

Your machine, your schedule — no cloud service involved. Examples::

    # See what would happen (recommended first run)
    python -m scripts.consolidate --db engraphis.db --workspace acme --dry-run

    # Run for real; distill recurring episodes + archive decayed transients
    python -m scripts.consolidate --db engraphis.db --workspace acme

    # Nicer digests via the configured LLM (falls back to deterministic on error)
    python -m scripts.consolidate --db engraphis.db --workspace acme --llm

    # Schema-first LLM distillation with entity/relation graph hints
    python -m scripts.consolidate --db engraphis.db --workspace acme --structured

This command does not install a timer or background worker. Hosted schedules, reports,
auto-consolidation, and dreaming run in Engraphis Cloud.
"""
from __future__ import annotations

import argparse
import json
import sys

from engraphis.core.engine import MemoryEngine

def main(argv=None) -> int:
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
    ap.add_argument("--profiles", action="store_true",
                    help="Also roll each entity's memories into one durable profile "
                         "digest (needs graph entities; report lands under 'profiles').")
    ap.add_argument("--structured", action="store_true",
                    help="Use configured LLM for schema-validated consolidation facts "
                         "with entities/relations/confidence; falls back to deterministic.")
    ap.add_argument("--supersede-sources", action="store_true",
                    help="Only with --structured: bi-temporally close source episodes "
                         "after validated facts are written.")
    ap.add_argument("--min-mentions", type=int, default=3,
                    help="Memories mentioning an entity before it earns a profile "
                         "(default 3; only used with --profiles).")
    args = ap.parse_args(argv)
    if args.supersede_sources and not args.structured:
        print("error: --supersede-sources requires --structured", file=sys.stderr)
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
            print(f"error: no repo named '{args.repo}' in workspace "
                  f"'{args.workspace}'", file=sys.stderr)
            return 2
        rid = rid_row["id"]

    llm = None
    if args.llm or args.structured:
        try:
            from engraphis.llm.client import LLMClient
            llm = LLMClient()
        except Exception as exc:  # noqa: BLE001
            print(f"warning: LLM unavailable ({exc}); using deterministic digests",
                  file=sys.stderr)

    try:
        report = engine.consolidate(
            workspace_id=wid_row["id"], repo_id=rid, dry_run=args.dry_run,
            min_cluster=args.min_cluster, archive_below=args.archive_below, llm=llm,
            profiles=args.profiles, min_mentions=args.min_mentions,
            structured=args.structured, supersede_sources=args.supersede_sources,
        )
    finally:
        if llm is not None and hasattr(llm, "close"):
            try:
                llm.close()
            except Exception:
                pass
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
