"""Interactive, human-only release of a governed memory into prompt context.

This command deliberately rejects redirected input and has no MCP/REST equivalent.
It is the local-owner approval ceremony for records held by the review gate.
"""
from __future__ import annotations

import argparse
import getpass
import sys

from engraphis.config import settings
from engraphis.service import MemoryService


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
    service = MemoryService.create(
        args.db,
        embed_model=settings.embed_model or None,
        embed_revision=getattr(settings, "embed_revision", "") or None,
        require_immutable_models=bool(getattr(settings, "require_immutable_models", False)),
        embed_dim=settings.embed_dim or 384,
        vector_backend=settings.vector_backend,
        rerank_model=settings.rerank_model or None,
        rerank_revision=getattr(settings, "rerank_revision", "") or None,
    )
    try:
        result = service.engine.approve_for_prompt(
            args.memory_id, reviewer=args.reviewer, reason=args.reason,
        )
    finally:
        service.close()
    print(result["id"])


if __name__ == "__main__":
    main()
