"""CLI — quick interactive access to your memory system, no server required.

Talks directly to the v2 :class:`~engraphis.service.MemoryService` (the same
validated facade the MCP server and Inspector use) against ``ENGRAPHIS_DB_PATH``,
so every command works offline with nothing else running. The ``--namespace``
flag maps onto a v2 *workspace*.

Usage:
    engraphis-cli ingest "User prefers dark mode" --namespace preferences --key theme
    engraphis-cli ingest-file notes.md --namespace vault
    engraphis-cli recall "What does the user prefer?" --namespace preferences
    engraphis-cli chat "What do you know about Alice?"
    engraphis-cli thoughts --namespace vault
    engraphis-cli list --namespace vault
    engraphis-cli delete-namespace vault
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from engraphis.config import settings
from engraphis.service import MemoryService, ValidationError


def _service() -> MemoryService:
    return MemoryService.create(
        settings.db_path,
        embed_model=settings.embed_model or None,
        allowed_workspaces=settings.allowed_workspaces,
        extractor=settings.extractor,
    )


def cmd_ingest(args: argparse.Namespace) -> None:
    out = _service().remember(
        args.content,
        workspace=args.namespace,
        title=args.key or "",
        metadata={"source": "cli"} | (args.metadata or {}),
        source="cli",
    )
    print(f"Stored: {out['id']} (workspace={out['workspace']}, op={out['op']})")
    if out.get("resolution"):
        print(f"  resolution: {out['resolution']}")


def cmd_ingest_file(args: argparse.Namespace) -> None:
    p = Path(args.file)
    if not p.exists():
        print(f"File not found: {p}")
        sys.exit(1)
    content = p.read_text(encoding="utf-8", errors="replace")
    doc_id = args.key or p.stem
    out = _service().ingest(
        content,
        workspace=args.namespace,
        metadata={"source": "cli", "document_id": doc_id, "file": p.name},
        source="cli",
    )
    print(f"Stored '{p.name}' as {doc_id} ({len(content)} chars, "
          f"{out['count']} memories, extracted={out['extracted']})")


def cmd_recall(args: argparse.Namespace) -> None:
    out = _service().recall(args.prompt, workspace=args.namespace, k=args.num_chunks)
    if not out["count"]:
        print(f"(no memories found{': ' + out['note'] if out.get('note') else ''})")
        return
    print(f"Found {out['count']} memories:\n")
    print(out["context"])


def cmd_chat(args: argparse.Namespace) -> None:
    # Grounded, citation-backed answer built strictly from stored memories —
    # offline and deterministic (no LLM/API key needed, unlike the old REST chat).
    out = _service().grounded_recall(args.prompt)
    if not out.get("grounded"):
        print(f"(no grounded answer: {out.get('reason') or 'insufficient supporting memories'})")
        return
    print(out["answer"])
    for i, c in enumerate(out.get("citations", []), start=1):
        print(f"  [{i}] {c.get('title') or c.get('content', '')[:80]}")


def cmd_thoughts(args: argparse.Namespace) -> None:
    # v2 equivalent of thought synthesis: the sleep-time consolidation sweep
    # (episodic→semantic distillation + decayed-transient archival).
    out = _service().consolidate(workspace=args.namespace or "default",
                                 min_cluster=max(2, min(20, args.num_chunks // 2 or 3)))
    print(json.dumps(out, indent=2, default=str))


def cmd_list(args: argparse.Namespace) -> None:
    out = _service().recall_proactive(workspace=args.namespace, k=args.limit)
    if not out["memories"]:
        print("(no memories)")
        return
    for m in out["memories"]:
        title = (m["title"] or m["content"])[:60]
        print(f"  [{m['id']}] {title}  "
              f"({m['mtype']}, importance={m['importance']:.2f}"
              f"{', pinned' if m['pinned'] else ''})")


def cmd_delete_ns(args: argparse.Namespace) -> None:
    if not args.force:
        print(f"This will delete ALL memories in namespace '{args.namespace}'. "
              f"Use --force to confirm.")
        sys.exit(1)
    svc = _service()
    wid, _ = svc._require_scope(args.namespace, None)
    rows = svc.store.conn.execute(
        "SELECT id FROM memories WHERE workspace_id=? AND expired_at IS NULL", (wid,)
    ).fetchall()
    for r in rows:
        svc.forget(r["id"], workspace=args.namespace,
                   reason="cli delete-namespace", actor="cli")
    print(f"Deleted {len(rows)} memories from '{args.namespace}' (audited soft-delete)")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="engraphis-cli", description="Engraphis CLI",
        epilog="Works offline against ENGRAPHIS_DB_PATH via the v2 MemoryService — no server "
               "needed. The old --server URL mode (v1 REST /memory/insert|/memory/query) was "
               "removed; point ENGRAPHIS_DB_PATH at the server's database to share its memory.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("ingest", help="Store a text memory")
    p.add_argument("content", help="Memory content text")
    p.add_argument("--namespace", "-n", default="default", help="Namespace")
    p.add_argument("--key", "-k", help="Document key/ID")
    p.add_argument("--metadata", help="JSON metadata string", default=None)
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("ingest-file", help="Store a file as a memory")
    p.add_argument("file", help="Path to file")
    p.add_argument("--namespace", "-n", default="vault", help="Namespace")
    p.add_argument("--key", "-k", help="Document key/ID")
    p.set_defaults(func=cmd_ingest_file)

    p = sub.add_parser("recall", help="Recall memories for a prompt")
    p.add_argument("prompt", help="Query prompt")
    p.add_argument("--namespace", "-n", default=None, help="Namespace")
    p.add_argument("--num-chunks", "-c", type=int, default=5)
    p.set_defaults(func=cmd_recall)

    p = sub.add_parser("chat", help="Grounded answer from memory (offline, cited)")
    p.add_argument("prompt", help="Your question")
    p.set_defaults(func=cmd_chat)

    p = sub.add_parser("thoughts", help="Generate consolidated thoughts")
    p.add_argument("--namespace", "-n", default=None)
    p.add_argument("--num-chunks", "-c", type=int, default=10)
    p.set_defaults(func=cmd_thoughts)

    p = sub.add_parser("list", help="List documents in a namespace")
    p.add_argument("--namespace", "-n", default="default")
    p.add_argument("--limit", "-l", type=int, default=20)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("delete-namespace", help="Delete an entire namespace")
    p.add_argument("namespace", help="Namespace to delete")
    p.add_argument("--force", action="store_true", help="Confirm deletion")
    p.set_defaults(func=cmd_delete_ns)

    args = parser.parse_args()
    if getattr(args, "metadata", None):
        args.metadata = json.loads(args.metadata)
    try:
        args.func(args)
    except ValidationError as exc:
        print(f"Error: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
