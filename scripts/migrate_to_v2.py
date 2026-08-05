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
* ``--new`` must name a fresh path. The migrator refuses an existing or in-place
  target rather than mixing source history into an existing v2 database.
* Vectors are carried as-is (original dim). Re-embedding with a SOTA model is a
  Phase-1 step; this migration is lossless and reversible.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Optional

import numpy as np

from engraphis.core.interfaces import Edge, MemoryRecord, MemoryType, Node, Scope
from engraphis.config import _publish_no_replace
from engraphis.core.poisoning import (
    PoisoningDecision,
    apply_quarantine_metadata,
    assess_untrusted_payload,
)
from engraphis.core.store import Store, now_ts

_VALID_TYPES = {t.value for t in MemoryType}
_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _untrusted_v1_metadata(metadata: dict, *, source: str, namespace: str,
                           document_id: object = None) -> tuple[dict, dict]:
    """Envelope a legacy payload before it reaches any v2 write/index path.

    A v1 database predates v2's trust boundary, so neither its metadata nor a
    familiar-looking provenance field can vouch for a migrated payload.  The
    envelope is deliberately written last and retained both in the dedicated
    provenance column and metadata for compatibility with existing readers.
    """
    out = dict(metadata or {})
    provenance = {
        "source": source,
        "trusted": False,
        "trust_origin": "v1_migration",
        "v1_namespace": namespace,
    }
    if document_id is not None:
        provenance["v1_document_id"] = document_id
    out["provenance"] = dict(provenance)
    return out, provenance


def _quarantine_migrated_payload(content: str, *, title: str, metadata: dict,
                                 provenance: dict, created: float,
                                 embedding: Optional[np.ndarray]) -> tuple[
                                     dict, dict, float | None, float | None,
                                     Optional[np.ndarray], PoisoningDecision,
                                 ]:
    """Apply the deterministic policy before a v1 payload is retained/indexed."""
    decision = assess_untrusted_payload(content, title=title, metadata=metadata)
    if not decision.quarantined:
        return metadata, provenance, None, None, embedding, decision
    metadata = apply_quarantine_metadata(metadata, decision)
    return (
        metadata,
        dict(metadata["provenance"]),
        created,
        now_ts(),
        None,
        decision,
    )


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


def _migrate_to_path(old_path: str, new_path: str, *, workspace: str = "default",
                     dry_run: bool = False, _precreated_target: bool = False) -> dict:
    source_path = Path(old_path).expanduser().resolve()
    target_path = Path(new_path).expanduser().resolve()
    # The migration writes a complete new v2 database. Reusing an output path can
    # silently mix old and new state, while an in-place run reaches Store() with a
    # v1-shaped ``memories`` table and fails only after attempting schema work. Refuse
    # both before opening either database so the source and any existing target remain
    # untouched. A dry run is read-only and intentionally remains available for either
    # path, which is useful when planning an upgrade.
    if not dry_run:
        if source_path == target_path:
            raise ValueError("v1 migration requires --new to differ from --old")
        if target_path.exists() and not _precreated_target:
            raise FileExistsError(
                "v1 migration requires a fresh --new path; refusing existing target "
                f"{target_path}"
            )
    # sqlite3.connect() creates a missing path. Validate the source first so a
    # failed migration (especially a dry run) never leaves a new empty database
    # behind or creates an output parent before discovering the missing input.
    if not source_path.is_file():
        raise FileNotFoundError(f"v1 migration source is not a file: {source_path}")

    src = sqlite3.connect(str(source_path))
    src.row_factory = sqlite3.Row
    store: Optional[Store] = None
    try:
        if not _has_table(src, "memories"):
            raise SystemExit(f"No 'memories' table in {old_path} — is this a v1 database?")
        wid = ""
        if not dry_run:
            store = Store(str(target_path))
            wid = store.get_or_create_workspace(workspace)
        return _migrate_rows(
            src, store, wid=wid, target_path=target_path,
        )
    finally:
        try:
            if store is not None:
                store.close()
        finally:
            src.close()


def _migrate_rows(src: sqlite3.Connection, store: Optional[Store], *, wid: str,
                  target_path: Path) -> dict:
    counts = {"memories": 0, "entities": 0, "edges": 0, "events": 0, "thoughts": 0, "repos": 0}

    # namespace -> repo_id
    repo_ids: dict[str, str] = {}
    entity_ids: dict[tuple[str, str, str], str] = {}
    edge_entity_candidates: dict[tuple[str, str], set[str]] = {}

    def repo_for(namespace: str) -> str:
        ns = namespace or "default"
        if ns not in repo_ids:
            counts["repos"] += 1
            if store is not None:
                repo_ids[ns] = store.get_or_create_repo(wid, ns)
            else:
                repo_ids[ns] = f"(repo:{ns})"
        return repo_ids[ns]

    def entity_for(namespace: str, name: object, entity_type: str = "") -> str:
        ns = namespace or "default"
        label = str(name or "").strip()
        ntype = str(entity_type or "").strip()
        if not label:
            raise ValueError("v1 migration found an edge/entity with an empty name")
        name_key = (ns, label.casefold())
        key = (*name_key, ntype)
        if key not in entity_ids:
            if store is not None:
                entity_ids[key] = store.upsert_entity(Node(
                    id="", name=label, ntype=ntype,
                    workspace_id=wid, repo_id=repo_for(ns),
                ))
            else:
                entity_ids[key] = f"(entity:{ns}:{label}:{ntype})"
            edge_entity_candidates.setdefault(name_key, set()).add(entity_ids[key])
        return entity_ids[key]

    def edge_entity_for(namespace: str, name: object) -> str:
        """Resolve type-less v1 edge names without conflating typed entities."""
        ns = namespace or "default"
        label = str(name or "").strip()
        candidates = edge_entity_candidates.get((ns, label.casefold()), set())
        if len(candidates) == 1:
            return next(iter(candidates))
        # Missing or ambiguous endpoints retain the v1 name as an untyped node.
        return entity_for(ns, label)

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
        title = (r["title"] if "title" in mcols else "") or ""
        document_id = r["document_id"] if "document_id" in mcols else None
        meta, provenance = _untrusted_v1_metadata(
            meta, source="v1", namespace=ns, document_id=document_id,
        )
        meta, provenance, valid_to, valid_to_recorded_at, emb, decision = (
            _quarantine_migrated_payload(
                r["content"], title=title, metadata=meta, provenance=provenance,
                created=created, embedding=emb,
            )
        )
        rec = MemoryRecord(
            id="", content=r["content"], mtype=MemoryType(mtype), scope=Scope.REPO,
            workspace_id=wid, repo_id=rid,
            title=title,
            keywords=keywords, metadata=meta,
            stability=(r["stability"] if "stability" in mcols else 1.0) or 1.0,
            surprise=(r["surprise"] if "surprise" in mcols else 1.0) or 1.0,
            access_count=(r["access_count"] if "access_count" in mcols else 0) or 0,
            last_access=(r["last_access"] if "last_access" in mcols else created),
            valid_from=created, valid_to=valid_to,
            valid_to_recorded_at=valid_to_recorded_at, ingested_at=created,
            provenance=provenance,
            embedding=emb,
        )
        memory_id = store.add_memory(rec)
        if decision.quarantined:
            store.audit(
                "v1_migration", "quarantine", memory_id,
                "policy=%s; reasons=%s" % (decision.policy, ",".join(decision.reasons)),
            )

    # ── entities ──────────────────────────────────────────────────────────────
    if _has_table(src, "entities"):
        ecols = _columns(src, "entities")
        for r in src.execute("SELECT * FROM entities").fetchall():
            counts["entities"] += 1
            if store is None:
                continue
            ns = r["namespace"] if "namespace" in ecols else "default"
            entity_for(
                ns,
                r["name"],
                (r["entity_type"] if "entity_type" in ecols else "") or "",
            )

    # ── edges ─────────────────────────────────────────────────────────────────
    if _has_table(src, "edges"):
        gcols = _columns(src, "edges")
        for r in src.execute("SELECT * FROM edges").fetchall():
            counts["edges"] += 1
            if store is None:
                continue
            ns = r["namespace"] if "namespace" in gcols else "default"
            store.upsert_edge(Edge(
                id="",
                src=edge_entity_for(ns, r["source_entity"]),
                dst=edge_entity_for(ns, r["target_entity"]),
                relation=r["relation"],
                weight=(r["weight"] if "weight" in gcols else 1.0) or 1.0,
                workspace_id=wid, repo_id=repo_for(ns),
                valid_from=(r["created_at"] if "created_at" in gcols else now_ts()),
                provenance={
                    "source": "v1",
                    "trusted": False,
                    "trust_origin": "v1_migration",
                    "review_state": "pending",
                },
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
            title = "synthesized thought"
            meta, provenance = _untrusted_v1_metadata(
                {}, source="v1:thought", namespace=ns,
            )
            meta, provenance, valid_to, valid_to_recorded_at, _, decision = (
                _quarantine_migrated_payload(
                    r["content"], title=title, metadata=meta, provenance=provenance,
                    created=created, embedding=None,
                )
            )
            memory_id = store.add_memory(MemoryRecord(
                id="", content=r["content"], mtype=MemoryType.SEMANTIC, scope=Scope.REPO,
                workspace_id=wid, repo_id=repo_for(ns), title=title, metadata=meta,
                valid_from=created, valid_to=valid_to,
                valid_to_recorded_at=valid_to_recorded_at, ingested_at=created,
                provenance=provenance,
            ))
            if decision.quarantined:
                store.audit(
                    "v1_migration", "quarantine", memory_id,
                    "policy=%s; reasons=%s" % (decision.policy, ",".join(decision.reasons)),
                )

    if store is not None:
        store.audit("migration", "migrate_v1_to_v2", str(target_path), str(counts))
        store.conn.commit()
    return counts


def _validate_and_flush_stage(path: Path) -> None:
    connection = sqlite3.connect(str(path), timeout=30)
    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        check = connection.execute("PRAGMA quick_check").fetchone()
        if not check or check[0] != "ok":
            raise sqlite3.DatabaseError("v1 migration integrity check failed")
    finally:
        connection.close()
    descriptor = os.open(
        str(path),
        os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _cleanup_stage(path: Path) -> None:
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        try:
            candidate.unlink()
        except OSError:
            pass


def migrate(old_path: str, new_path: str, *, workspace: str = "default",
            dry_run: bool = False) -> dict:
    """Migrate through a same-directory stage and publish only a verified database."""
    source_path = Path(old_path).expanduser().resolve()
    target_path = Path(new_path).expanduser().resolve()
    if dry_run:
        return _migrate_to_path(
            str(source_path), str(target_path), workspace=workspace, dry_run=True,
        )
    if source_path == target_path:
        raise ValueError("v1 migration requires --new to differ from --old")
    if target_path.exists():
        raise FileExistsError(
            "v1 migration requires a fresh --new path; refusing existing target "
            f"{target_path}"
        )

    target_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, stage_name = tempfile.mkstemp(
        prefix=f".{target_path.name}.migration-",
        suffix=".db",
        dir=str(target_path.parent),
    )
    os.close(descriptor)
    stage_path = Path(stage_name)
    try:
        counts = _migrate_to_path(
            str(source_path), str(stage_path), workspace=workspace,
            _precreated_target=True,
        )
        _validate_and_flush_stage(stage_path)
        _publish_no_replace(stage_path, target_path)
        return counts
    finally:
        _cleanup_stage(stage_path)


def main() -> None:
    # Keep argparse output ASCII-only: Windows' default CP1252 console cannot encode
    # the Unicode arrow formerly used here, which made even ``--help`` crash.
    ap = argparse.ArgumentParser(description="Migrate v1 engraphis_v1.db -> v2 Engraphis schema.")
    ap.add_argument("--old", default=str(_PROJECT_ROOT / "engraphis_v1.db"))
    ap.add_argument(
        "--new", default=str(_PROJECT_ROOT / "engraphis_v2.db"),
        help="fresh v2 output path (must not already exist unless --dry-run)",
    )
    ap.add_argument("--workspace", default="default")
    ap.add_argument("--dry-run", action="store_true", help="report counts, write nothing")
    args = ap.parse_args()

    if not Path(args.old).exists():
        raise SystemExit(f"Old DB not found: {args.old}")

    counts = migrate(args.old, args.new, workspace=args.workspace, dry_run=args.dry_run)
    mode = "DRY RUN - nothing written" if args.dry_run else f"written -> {args.new}"
    print(f"Engraphis migration ({mode})")
    for k, v in counts.items():
        print(f"  {k:10s}: {v}")


if __name__ == "__main__":
    main()
