"""Migrate a v1 (engraphis_v1.db) database into the v2 Engraphis schema.

v1 is flat: every memory has a single ``namespace`` string. v2 is scoped:
``workspace -> repo -> session -> memory`` with bi-temporal validity. This
migration maps each distinct v1 ``namespace`` to a v2 ``repo`` under one
workspace, carries memories/entities/edges/events/thoughts across, and preserves
the original ids and vectors in ``provenance`` / ``mem_vectors``.

Usage:
    python -m scripts.migrate_to_v2 --old engraphis_v1.db --new engraphis_v2.db
    python -m scripts.migrate_to_v2 --dry-run            # report only, write nothing

Notes:
* Idempotent target: run against a fresh --new file.
* Vectors are carried as-is (original dim). Re-embedding with a SOTA model is a
  Phase-1 step; this migration is lossless and reversible.
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path
from typing import Optional

import numpy as np

from engraphis.core.interfaces import Edge, MemoryRecord, MemoryType, Node, Scope
from engraphis.core.store import Store, now_ts

_VALID_TYPES = {t.value for t in MemoryType}
_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except sqlite3.OperationalError:
        return set()


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def migrate(old_path: str, new_path: str, *, workspace: str = "default",
            dry_run: bool = False) -> dict:
    src = sqlite3.connect(old_path)
    src.row_factory = sqlite3.Row

    counts = {"memories": 0, "entities": 0, "edges": 0, "events": 0, "thoughts": 0, "repos": 0}
    if not _has_table(src, "memories"):
        src.close()
        raise SystemExit(f"No 'memories' table in {old_path} — is this a v1 database?")

    store: Optional[Store] = None
    if not dry_run:
        store = Store(new_path)
        wid = store.get_or_create_workspace(workspace)

    # namespace -> repo_id
    repo_ids: dict[str, str] = {}

    def repo_for(namespace: str) -> str:
        ns = namespace or "default"
        if ns not in repo_ids:
            counts["repos"] += 1
            if store is not None:
                repo_ids[ns] = store.get_or_create_repo(wid, ns)
            else:
                repo_ids[ns] = f"(repo:{ns})"
        return repo_ids[ns]

    # ── memories ──────────────────────────────────────────────────────────────
    mcols = _columns(src, "memories")
    for r in src.execute("SELECT * FROM memories").fetchall():
        ns = r["namespace"] if "namespace" in mcols else "default"
        rid = repo_for(ns)
        counts["memories"] += 1
        if store is None:
            continue
        mtype = r["memory_type"] if "memory_type" in mcols else "semantic"
        mtype = mtype if mtype in _VALID_TYPES else "semantic"
        meta = {}
        if "metadata" in mcols and r["metadata"]:
            import json
            try:
                meta = json.loads(r["metadata"])
            except Exception:
                meta = {}
        keywords = meta.get("tags", []) if isinstance(meta.get("tags"), list) else []
        emb = None
        if "vector" in mcols and r["vector"] is not None:
            emb = np.frombuffer(r["vector"], dtype=np.float32).copy()
        created = r["created_at"] if "created_at" in mcols else now_ts()
        rec = MemoryRecord(
            id="", content=r["content"], mtype=MemoryType(mtype), scope=Scope.REPO,
            workspace_id=wid, repo_id=rid,
            title=(r["title"] if "title" in mcols else "") or "",
            keywords=keywords, metadata=meta,
            stability=(r["stability"] if "stability" in mcols else 1.0) or 1.0,
            surprise=(r["surprise"] if "surprise" in mcols else 1.0) or 1.0,
            access_count=(r["access_count"] if "access_count" in mcols else 0) or 0,
            last_access=(r["last_access"] if "last_access" in mcols else created),
            valid_from=created, ingested_at=created,
            provenance={"source": "v1", "v1_namespace": ns,
                        "v1_document_id": r["document_id"] if "document_id" in mcols else None},
            embedding=emb,
        )
        store.add_memory(rec)

    # ── entities ──────────────────────────────────────────────────────────────
    if _has_table(src, "entities"):
        ecols = _columns(src, "entities")
        for r in src.execute("SELECT * FROM entities").fetchall():
            counts["entities"] += 1
            if store is None:
                continue
            ns = r["namespace"] if "namespace" in ecols else "default"
            store.upsert_entity(Node(
                id="", name=r["name"],
                ntype=(r["entity_type"] if "entity_type" in ecols else "") or "",
                workspace_id=wid, repo_id=repo_for(ns),
            ))

    # ── edges ─────────────────────────────────────────────────────────────────
    if _has_table(src, "edges"):
        gcols = _columns(src, "edges")
        for r in src.execute("SELECT * FROM edges").fetchall():
            counts["edges"] += 1
            if store is None:
                continue
            ns = r["namespace"] if "namespace" in gcols else "default"
            store.upsert_edge(Edge(
                id="", src=r["source_entity"], dst=r["target_entity"], relation=r["relation"],
                weight=(r["weight"] if "weight" in gcols else 1.0) or 1.0,
                workspace_id=wid, repo_id=repo_for(ns),
                valid_from=(r["created_at"] if "created_at" in gcols else now_ts()),
                provenance={"source": "v1"},
            ))

    # ── events ────────────────────────────────────────────────────────────────
    if _has_table(src, "events"):
        vcols = _columns(src, "events")
        for r in src.execute("SELECT * FROM events").fetchall():
            counts["events"] += 1
            if store is None:
                continue
            ns = r["namespace"] if "namespace" in vcols else "default"
            store.append_event(
                kind=(r["event_type"] if "event_type" in vcols else "event"),
                content=(r["description"] if "description" in vcols else "") or "",
                workspace_id=wid, repo_id=repo_for(ns),
            )

    # ── thoughts → semantic memories ───────────────────────────────────────────
    if _has_table(src, "thoughts"):
        tcols = _columns(src, "thoughts")
        for r in src.execute("SELECT * FROM thoughts").fetchall():
            counts["thoughts"] += 1
            if store is None:
                continue
            ns = r["namespace"] if "namespace" in tcols else "default"
            created = r["created_at"] if "created_at" in tcols else now_ts()
            store.add_memory(MemoryRecord(
                id="", content=r["content"], mtype=MemoryType.SEMANTIC, scope=Scope.REPO,
                workspace_id=wid, repo_id=repo_for(ns), title="synthesized thought",
                valid_from=created, ingested_at=created,
                provenance={"source": "v1:thought"},
            ))

    src.close()
    if store is not None:
        store.audit("migration", "migrate_v1_to_v2", new_path, str(counts))
        store.conn.commit()
        store.close()
    return counts


def main() -> None:
    ap = argparse.ArgumentParser(description="Migrate v1 engraphis_v1.db → v2 Engraphis schema.")
    ap.add_argument("--old", default=str(_PROJECT_ROOT / "engraphis_v1.db"))
    ap.add_argument("--new", default=str(_PROJECT_ROOT / "engraphis_v2.db"))
    ap.add_argument("--workspace", default="default")
    ap.add_argument("--dry-run", action="store_true", help="report counts, write nothing")
    args = ap.parse_args()

    if not Path(args.old).exists():
        raise SystemExit(f"Old DB not found: {args.old}")

    counts = migrate(args.old, args.new, workspace=args.workspace, dry_run=args.dry_run)
    mode = "DRY RUN — nothing written" if args.dry_run else f"written → {args.new}"
    print(f"Engraphis migration ({mode})")
    for k, v in counts.items():
        print(f"  {k:10s}: {v}")


if __name__ == "__main__":
    main()
