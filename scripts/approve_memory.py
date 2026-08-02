"""Interactive, human-only release of a governed memory into prompt context.

This command deliberately rejects redirected input and has no MCP/REST equivalent.
It is the local-owner approval ceremony for records held by the review gate.
"""
from __future__ import annotations

import argparse
import getpass
import sys

from engraphis.config import settings
from engraphis.core.engine import MemoryEngine


def main() -> None:
    parser = argparse.ArgumentParser(description="Interactively approve one pending memory")
    parser.add_argument("memory_id")
    parser.add_argument("--db", default=settings.db_path)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--reviewer", default=getpass.getuser())
    args = parser.parse_args()
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        parser.error("approval requires an interactive TTY")
    phrase = f"APPROVE {args.memory_id}"
    entered = input(f"Type '{phrase}' to release this memory: ").strip()
    if entered != phrase:
        parser.error("approval confirmation did not match")
    engine = MemoryEngine.create(args.db)
    try:
        result = engine.approve_for_prompt(
            args.memory_id, reviewer=args.reviewer, reason=args.reason,
        )
    finally:
        engine.store.close()
    print(result["id"])


if __name__ == "__main__":
    main()
