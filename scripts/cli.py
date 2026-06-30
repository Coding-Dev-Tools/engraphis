"""CLI — quick interactive access to your memory system.

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
import sys
from pathlib import Path

import httpx

from engraphis.config import settings

BASE = settings.base_url


def cmd_ingest(args: argparse.Namespace) -> None:
    with httpx.Client(base_url=BASE, timeout=60) as c:
        r = c.post("/memory/insert", json={
            "key": args.key or f"note-{int(__import__('time').time())}",
            "content": args.content,
            "namespace": args.namespace,
            "metadata": {"source": "cli"} | (args.metadata or {}),
        })
        r.raise_for_status()
        print(f"Stored: {r.json()['data']}")


def cmd_ingest_file(args: argparse.Namespace) -> None:
    p = Path(args.file)
    if not p.exists():
        print(f"File not found: {p}")
        sys.exit(1)
    content = p.read_text(encoding="utf-8", errors="replace")
    doc_id = args.key or p.stem
    with httpx.Client(base_url=BASE, timeout=120) as c:
        r = c.post("/memory/documents", json={
            "title": p.stem,
            "content": content,
            "namespace": args.namespace,
            "document_id": doc_id,
            "source_type": "file",
        })
        r.raise_for_status()
        data = r.json()["data"]
        print(f"Stored '{p.name}' as {doc_id} ({len(content)} chars, "
              f"{data.get('entities', 0)} entities, {data.get('edges', 0)} edges)")


def cmd_recall(args: argparse.Namespace) -> None:
    with httpx.Client(base_url=BASE, timeout=60) as c:
        r = c.post("/memory/query", json={
            "namespace": args.namespace,
            "query": args.prompt,
            "maxChunks": args.num_chunks,
        })
        r.raise_for_status()
        data = r.json()["data"]
        if not data["count"]:
            print("(no memories found)")
            return
        print(f"Found {data['count']} memories:\n")
        print(data["llmContextMessage"])


def cmd_chat(args: argparse.Namespace) -> None:
    with httpx.Client(base_url=BASE, timeout=120) as c:
        r = c.post("/memory/conversations", json={
            "messages": [{"role": "user", "content": args.prompt}],
            "temperature": 0.3,
            "maxTokens": 1024,
        })
        r.raise_for_status()
        data = r.json()["data"]
        print(data["answer"])


def cmd_thoughts(args: argparse.Namespace) -> None:
    with httpx.Client(base_url=BASE, timeout=120) as c:
        r = c.post("/memory/memories/thoughts", json={
            "namespace": args.namespace,
            "maxChunks": args.num_chunks,
        })
        r.raise_for_status()
        data = r.json()["data"]
        if data.get("thought"):
            import json
            print(json.dumps(data["thought"], indent=2))
        else:
            print(f"No thought generated: {data}")


def cmd_list(args: argparse.Namespace) -> None:
    with httpx.Client(base_url=BASE, timeout=30) as c:
        r = c.get("/memory/documents", params={
            "namespace": args.namespace,
            "limit": args.limit,
        })
        r.raise_for_status()
        docs = r.json()["data"]["documents"]
        if not docs:
            print("(no documents)")
            return
        for d in docs:
            print(f"  [{d['document_id']}] {d['title'][:60]}  "
                  f"(accessed {d['access_count']}x, stability={d['stability']:.2f})")


def cmd_delete_ns(args: argparse.Namespace) -> None:
    if not args.force:
        print(f"This will delete ALL memories in namespace '{args.namespace}'. "
              f"Use --force to confirm.")
        sys.exit(1)
    with httpx.Client(base_url=BASE, timeout=30) as c:
        r = c.post("/memory/admin/delete", json={
            "namespace": args.namespace, "delete_all": True,
        })
        r.raise_for_status()
        print(f"Deleted {r.json()['data']['deleted']} memories from '{args.namespace}'")


def main() -> None:
    parser = argparse.ArgumentParser(prog="engraphis-cli", description="Engraphis CLI")
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

    p = sub.add_parser("chat", help="Chat with memory context via your configured LLM")
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
    if args.metadata:
        import json
        args.metadata = json.loads(args.metadata)
    args.func(args)


if __name__ == "__main__":
    main()
