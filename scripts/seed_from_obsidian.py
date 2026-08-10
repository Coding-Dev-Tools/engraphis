"""Deprecated compatibility wrapper for the v2 Obsidian importer.

Use ``engraphis import obsidian PATH --workspace NAME``.  This module remains so
older local automation cannot accidentally fall back to the legacy v1 namespace
ingester.
"""
from __future__ import annotations

import argparse
import sys


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Deprecated: import an Obsidian vault into Engraphis v2.",
    )
    parser.add_argument("vault_path", help="path to the Obsidian vault")
    target = parser.add_mutually_exclusive_group()
    target.add_argument(
        "--namespace", default=None,
        help="legacy namespace; maps to a v2 workspace",
    )
    target.add_argument("--workspace", help="v2 target workspace")
    parser.add_argument("--repo", help="v2 target repository")
    parser.add_argument("--session", help="active v2 target session ID")
    parser.add_argument("--scope", choices=("workspace", "repo", "session"))
    parser.add_argument(
        "--memory-type", default="semantic",
        choices=("working", "episodic", "semantic", "procedural"),
    )
    parser.add_argument("--db", help="v2 database path")
    parser.add_argument("--limit", type=int, default=0, help="process at most N notes this run")
    parser.add_argument(
        "--on-conflict", default="error", choices=("error", "replace", "new"),
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.limit < 0:
        parser.error("--limit must be zero or greater")
    print(
        "warning: scripts.seed_from_obsidian is deprecated; "
        "use `engraphis import obsidian`",
        file=sys.stderr,
    )
    from scripts.importer import main as importer_main

    forwarded = [
        "obsidian",
        args.vault_path,
        "--workspace",
        args.workspace or args.namespace or "vault",
        "--memory-type",
        args.memory_type,
        "--on-conflict",
        args.on_conflict,
    ]
    for option, value in (
        ("--repo", args.repo),
        ("--session", args.session),
        ("--scope", args.scope),
        ("--db", args.db),
    ):
        if value:
            forwarded.extend((option, value))
    if args.limit:
        forwarded.extend(("--limit", str(args.limit)))
    if args.dry_run:
        forwarded.append("--dry-run")
    else:
        # Invoking the historical write command is the owner's explicit local
        # confirmation; forwarding --yes preserves its non-interactive contract.
        forwarded.append("--yes")
    if args.json:
        forwarded.append("--json")
    return int(importer_main(forwarded))


if __name__ == "__main__":
    raise SystemExit(main())
