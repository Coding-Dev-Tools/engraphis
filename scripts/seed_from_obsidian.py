"""Seed the memory system from an Obsidian vault (or any folder of markdown files).

Usage:
    python -m scripts.seed_from_obsidian <vault_path> [--namespace vault]
    python -m scripts.seed_from_obsidian "C:/Users/home/OneDrive/Documents/Obsidian Vault Local"

Each .md file becomes a memory document with:
    document_id = relative path (sanitized)
    title       = first H1 or filename
    content     = full file text
    metadata    = {file, tags, links, word_count}
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

from engraphis.engines import ingest as ingest_engine


def extract_title(content: str, fallback: str) -> str:
    m = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
    return m.group(1).strip() if m else fallback


def extract_tags(content: str) -> list[str]:
    return re.findall(r"#([a-zA-Z][a-zA-Z0-9_-]+)", content)


def extract_links(content: str) -> list[str]:
    return re.findall(r"\[\[([^\]]+)\]\]", content)


def seed_vault(vault_path: str, namespace: str = "vault", limit: int = 0) -> dict:
    vault = Path(vault_path)
    if not vault.exists():
        print(f"ERROR: path does not exist: {vault}")
        sys.exit(1)

    md_files = sorted(vault.rglob("*.md"))
    if limit:
        md_files = md_files[:limit]

    print(f"Seeding {len(md_files)} markdown files from {vault} → namespace='{namespace}'")
    successful = 0
    errors = 0
    t0 = time.time()

    for i, fpath in enumerate(md_files):
        try:
            rel = fpath.relative_to(vault).as_posix()
            doc_id = rel.replace("/", "__").replace(".md", "")
            content = fpath.read_text(encoding="utf-8", errors="replace")
            if not content.strip():
                continue
            title = extract_title(content, fpath.stem)
            tags = extract_tags(content)
            links = extract_links(content)

            ingest_engine.ingest_document(
                namespace=namespace,
                document_id=doc_id,
                title=title,
                content=content,
                source_type="obsidian",
                metadata={
                    "file": rel,
                    "tags": tags,
                    "links": links,
                    "word_count": len(content.split()),
                },
            )
            successful += 1
            if (i + 1) % 50 == 0:
                print(f"  ... {i + 1}/{len(md_files)} ingested")
        except Exception as e:
            errors += 1
            print(f"  ERROR on {fpath}: {e}")

    elapsed = time.time() - t0
    print(f"\nDone: {successful} ingested, {errors} errors, {elapsed:.1f}s")
    return {"ingested": successful, "errors": errors, "elapsed_s": elapsed}


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed Engraphis from an Obsidian vault")
    parser.add_argument("vault_path", help="Path to the vault folder (containing .md files)")
    parser.add_argument("--namespace", default="vault", help="Namespace to store under (default: vault)")
    parser.add_argument("--limit", type=int, default=0, help="Max files to ingest (0 = all)")
    args = parser.parse_args()
    seed_vault(args.vault_path, namespace=args.namespace, limit=args.limit)


if __name__ == "__main__":
    main()
