"""Migrate a v1 (engraphis_v1.db) database into the v2 Engraphis schema.

v1 is flat: every memory has a single ``namespace`` string. v2 is scoped:
``workspace -> repo -> session -> memory`` with bi-temporal validity. This
migration maps each distinct v1 ``namespace`` to a v2 ``repo`` under one
workspace, carries memories/entities/edges/events/thoughts across, and records
typed v1 lineage plus valid vectors in ``provenance`` / ``mem_vectors``.

Usage:
    python -m scripts.migrate_to_v2 --old engraphis_v1.db --new engraphis_v2.db
    python -m scripts.migrate_to_v2 --dry-run            # report only, write nothing

Notes:
* ``--new`` must name a fresh path. The migrator refuses an existing or in-place
  target rather than mixing source history into an existing v2 database.
* Valid legacy vectors are carried as-is (original dim). Dynamically typed v1
  fields outside the v2 domain are normalized and recorded in provenance.
"""
from __future__ import annotations

import argparse
import json
import math
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
from engraphis.core.secrets import reject_secrets
from engraphis.core.store import Store, now_ts

_VALID_TYPES = {t.value for t in MemoryType}
_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _legacy_scalar(value: object) -> object:
    """Return one JSON-safe scalar without retaining non-finite numeric syntax."""
    if value is None or type(value) in (int, str):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    return str(value)


def _source_id(row: sqlite3.Row, columns: set[str]) -> object:
    """Return one JSON-safe legacy primary key for lineage."""
    if "id" not in columns or row["id"] is None:
        return None
    return _legacy_scalar(row["id"])


def _legacy_float(
    value: object,
    *,
    default: float,
    field: str,
    repairs: list[str],
    nonnegative: bool = False,
    positive: bool = False,
) -> float:
    """Normalize a dynamically typed SQLite value into the finite v2 domain."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        repairs.append(field)
        return float(default)
    normalized = float(value)
    invalid = not math.isfinite(normalized)
    invalid = invalid or (nonnegative and normalized < 0.0)
    invalid = invalid or (positive and normalized <= 0.0)
    if invalid:
        repairs.append(field)
        return float(default)
    return normalized


def _legacy_int(
    value: object,
    *,
    default: int,
    field: str,
    repairs: list[str],
) -> int:
    if type(value) is not int or value < 0:
        repairs.append(field)
        return default
    return value


def _legacy_vector(
    value: object,
    *,
    repairs: list[str],
) -> Optional[np.ndarray]:
    if value is None:
        return None
    if not isinstance(value, (bytes, bytearray, memoryview)):
        repairs.append("vector")
        return None
    try:
        vector = np.frombuffer(value, dtype=np.float32).copy()
    except (TypeError, ValueError):
        repairs.append("vector")
        return None
    norm = float(np.linalg.norm(vector))
    if vector.size == 0 or not np.isfinite(vector).all() or not math.isfinite(norm) or norm <= 0:
        repairs.append("vector")
        return None
    return vector


def _decode_metadata(value: object) -> object:
    if value is None or value == "":
        return {}
    if not isinstance(value, (str, bytes, bytearray)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError, UnicodeError):
        return value


def _untrusted_v1_metadata(
    metadata: object,
    *,
    source: str,
    namespace: str,
    document_id: object = None,
    source_kind: str = "",
    source_id: object = None,
    repairs: tuple[str, ...] = (),
) -> tuple[dict, dict]:
    """Envelope one legacy payload before it reaches any v2 write/index path."""
    normalized_fields = list(repairs)
    if isinstance(metadata, dict):
        out = dict(metadata)
    else:
        out = {}
        normalized_fields.append("metadata")
    provenance = {
        "source": source,
        "trusted": False,
        "trust_origin": "v1_migration",
        "review_state": "pending",
        "v1_namespace": namespace,
    }
    if document_id is not None:
        provenance["v1_document_id"] = _legacy_scalar(document_id)
    if source_kind and source_id is not None:
        provenance[f"v1_{source_kind}_id"] = _legacy_scalar(source_id)
    if normalized_fields:
        provenance["v1_normalized_fields"] = sorted(set(normalized_fields))
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


def _vector_dim_histogram(src: sqlite3.Connection) -> tuple[dict[int, int], list]:
    """Scan v1 vectors into a ``{dim: count}`` histogram plus a re-embed queue.

    The queue holds raw v1 ``id`` values whose vectors are missing, undecodable,
    or non-finite — rows the v2 embedder must re-embed after migration.
    """
    histogram: dict[int, int] = {}
    queue: list = []
    if not _has_table(src, "memories"):
        return histogram, queue
    if "vector" not in _columns(src, "memories"):
        return histogram, queue
    for row in src.execute("SELECT id, vector FROM memories").fetchall():
        raw = row["vector"]
        if raw is None:
            queue.append(row["id"])
            continue
        if not isinstance(raw, (bytes, bytearray, memoryview)):
            queue.append(row["id"])
            continue
        try:
            vector = np.frombuffer(raw, dtype=np.float32)
        except (TypeError, ValueError):
            queue.append(row["id"])
            continue
        if int(vector.size) == 0 or not bool(np.isfinite(vector).all()):
            queue.append(row["id"])
            continue
        dim = int(vector.size)
        histogram[dim] = histogram.get(dim, 0) + 1
    return histogram, queue


def _write_reembed_queue(path: str, queue: list) -> None:
    """Persist one raw v1 id per line for post-migration re-embedding."""
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as handle:
        for item in queue:
            handle.write(f"{item}\n")


def _migrate_to_path(
    old_path: str,
    new_path: str,
    *,
    workspace: str = "default",
    dry_run: bool = False,
    resume: bool = False,
    reembed_queue: Optional[str] = None,
    _precreated_target: bool = False,
) -> dict:
    source_path = Path(old_path).expanduser().resolve()
    target_path = Path(new_path).expanduser().resolve()
    if not dry_run:
        if source_path == target_path:
            raise ValueError("v1 migration requires --new to differ from --old")
        if target_path.exists() and not (_precreated_target or resume):
            raise FileExistsError(
                "v1 migration requires a fresh --new path; refusing existing target "
                f"{target_path} (pass --resume to continue into it)"
            )
    else:
        # A read-only SQLite connection may need to materialize shared-memory state for
        # an uncheckpointed WAL. Refuse rather than violating dry-run's no-write contract.
        wal_path = Path(f"{source_path}-wal")
        try:
            has_uncheckpointed_wal = wal_path.is_file() and wal_path.stat().st_size > 0
        except OSError:
            has_uncheckpointed_wal = True
        if has_uncheckpointed_wal:
            raise RuntimeError(
                "dry-run requires a checkpointed v1 database; an active WAL is present"
            )

    source_uri = f"{source_path.as_uri()}?mode=ro"
    src = sqlite3.connect(source_uri, uri=True, timeout=30)
    src.row_factory = sqlite3.Row
    store: Optional[Store] = None
    try:
        src.execute("PRAGMA query_only=ON")
        src.execute("BEGIN")
        if not _has_table(src, "memories"):
            raise SystemExit(f"No 'memories' table in {old_path} - is this a v1 database?")
        wid = ""
        if not dry_run:
            store = Store(str(target_path))
            wid = store.get_or_create_workspace(workspace)
        return _migrate_rows(
            src, store, wid=wid, target_path=target_path,
            resume=resume, reembed_queue=reembed_queue,
        )
    finally:
        try:
            if store is not None:
                store.close()
        finally:
            try:
                src.rollback()
            finally:
                src.close()


def _migrate_rows(
    src: sqlite3.Connection,
    store: Optional[Store],
    *,
    wid: str,
    target_path: Path,
    resume: bool = False,
    reembed_queue: Optional[str] = None,
) -> dict:
    counts = {
        "memories": 0,
        "entities": 0,
        "edges": 0,
        "events": 0,
        "thoughts": 0,
        "repos": 0,
        "quarantined": 0,
        "repaired_fields": 0,
        "resumed_skipped": 0,
        "reembed_queued": 0,
    }
    # Dim-histogram preflight: report every legacy embedding width before any
    # write, so a mixed-dim source is visible instead of silently carried.
    dim_histogram, reembed_ids = _vector_dim_histogram(src)
    counts["reembed_queued"] = len(reembed_ids)
    if reembed_queue and reembed_ids:
        _write_reembed_queue(reembed_queue, reembed_ids)
    # Resume support: skip v1 source ids already carried into this target.
    migrated_memory_ids: set = set()
    migrated_thought_ids: set = set()
    migrated_event_ids: set = set()
    if resume and store is not None:
        try:
            for prow in store.conn.execute(
                "SELECT provenance FROM memories"
            ).fetchall():
                try:
                    penv = json.loads(prow["provenance"] or "{}")
                except (TypeError, ValueError):
                    continue
                if not isinstance(penv, dict):
                    continue
                if "v1_memory_id" in penv:
                    migrated_memory_ids.add(json.dumps(
                        penv["v1_memory_id"], sort_keys=True, ensure_ascii=True))
                if "v1_thought_id" in penv:
                    migrated_thought_ids.add(json.dumps(
                        penv["v1_thought_id"], sort_keys=True, ensure_ascii=True))
        except Exception:
            pass
        try:
            for erow in store.conn.execute("SELECT refs FROM events").fetchall():
                try:
                    refs = json.loads(erow["refs"] or "[]")
                except (TypeError, ValueError):
                    continue
                if not isinstance(refs, list):
                    continue
                for ref in refs:
                    if isinstance(ref, dict) and ref.get("kind") == "v1_event_id":
                        migrated_event_ids.add(json.dumps(
                            ref.get("id"), sort_keys=True, ensure_ascii=True))
        except Exception:
            pass
    migration_time = now_ts()
    repo_ids: dict[str, str] = {}
    entity_ids: dict[tuple[str, str, str], str] = {}
    edge_entity_candidates: dict[tuple[str, str], set[str]] = {}

    def namespace_value(value: object) -> str:
        return str(value or "default").strip() or "default"

    def repo_for(namespace: object) -> str:
        ns = namespace_value(namespace)
        if ns not in repo_ids:
            counts["repos"] += 1
            if store is not None:
                repo_ids[ns] = store.get_or_create_repo(wid, ns)
            else:
                repo_ids[ns] = f"(repo:{ns})"
        return repo_ids[ns]

    def entity_for(
        namespace: object,
        name: object,
        entity_type: object = "",
        *,
        source_id: object = None,
    ) -> str:
        ns = namespace_value(namespace)
        label = str(name or "").strip()
        ntype = str(entity_type or "").strip()
        if not label:
            raise ValueError("v1 migration found an edge/entity with an empty name")
        name_key = (ns, label.casefold())
        key = (*name_key, ntype)
        if key not in entity_ids:
            node = Node(
                id="",
                name=label,
                ntype=ntype,
                workspace_id=wid or None,
                repo_id=repo_for(ns),
            )
            if store is not None:
                entity_ids[key] = store.upsert_entity(node)
            else:
                entity_ids[key] = f"(entity:{ns}:{label}:{ntype})"
            edge_entity_candidates.setdefault(name_key, set()).add(entity_ids[key])
        if store is not None and source_id is not None:
            store.audit(
                "v1_migration",
                "lineage",
                entity_ids[key],
                f"v1_entity_id={json.dumps(source_id, ensure_ascii=True)}",
                commit=False,
            )
        return entity_ids[key]

    def edge_entity_for(namespace: object, name: object) -> str:
        """Resolve type-less v1 edge names without conflating typed entities."""
        ns = namespace_value(namespace)
        label = str(name or "").strip()
        candidates = edge_entity_candidates.get((ns, label.casefold()), set())
        if len(candidates) == 1:
            return next(iter(candidates))
        return entity_for(ns, label)

    mcols = _columns(src, "memories")
    for row in src.execute("SELECT * FROM memories").fetchall():
        counts["memories"] += 1
        if resume:
            resume_key = json.dumps(
                _legacy_scalar(_source_id(row, mcols)), sort_keys=True, ensure_ascii=True)
            if resume_key in migrated_memory_ids:
                counts["resumed_skipped"] += 1
                continue
        repairs: list[str] = []
        ns = namespace_value(
            row["namespace"] if "namespace" in mcols else "default"
        )
        rid = repo_for(ns)
        mtype_value = row["memory_type"] if "memory_type" in mcols else "semantic"
        if mtype_value not in _VALID_TYPES:
            mtype_value = "semantic"
            repairs.append("memory_type")
        meta_value = _decode_metadata(
            row["metadata"] if "metadata" in mcols else None
        )
        raw_tags = meta_value.get("tags", []) if isinstance(meta_value, dict) else []
        keywords = [str(item) for item in raw_tags] if isinstance(raw_tags, list) else []
        embedding = _legacy_vector(
            row["vector"] if "vector" in mcols else None,
            repairs=repairs,
        )
        created = _legacy_float(
            row["created_at"] if "created_at" in mcols else migration_time,
            default=migration_time,
            field="created_at",
            repairs=repairs,
        )
        last_access = _legacy_float(
            row["last_access"] if "last_access" in mcols else created,
            default=created,
            field="last_access",
            repairs=repairs,
        )
        stability = _legacy_float(
            row["stability"] if "stability" in mcols else 1.0,
            default=1.0,
            field="stability",
            repairs=repairs,
            positive=True,
        )
        surprise = _legacy_float(
            row["surprise"] if "surprise" in mcols else 1.0,
            default=1.0,
            field="surprise",
            repairs=repairs,
            nonnegative=True,
        )
        importance = _legacy_float(
            row["importance"] if "importance" in mcols else 0.0,
            default=0.0,
            field="importance",
            repairs=repairs,
            nonnegative=True,
        )
        if importance > 1.0:
            importance = 1.0
            repairs.append("importance")
        access_count = _legacy_int(
            row["access_count"] if "access_count" in mcols else 0,
            default=0,
            field="access_count",
            repairs=repairs,
        )
        title = str((row["title"] if "title" in mcols else "") or "")
        content = str((row["content"] if "content" in mcols else "") or "")
        document_id = _legacy_scalar(
            row["document_id"] if "document_id" in mcols else None
        )
        metadata, provenance = _untrusted_v1_metadata(
            meta_value,
            source="v1",
            namespace=ns,
            document_id=document_id,
            source_kind="memory",
            source_id=_source_id(row, mcols),
            repairs=tuple(repairs),
        )
        metadata, provenance, valid_to, valid_to_recorded_at, embedding, decision = (
            _quarantine_migrated_payload(
                content,
                title=title,
                metadata=metadata,
                provenance=provenance,
                created=created,
                embedding=embedding,
            )
        )
        normalized = provenance.get("v1_normalized_fields", [])
        counts["repaired_fields"] += len(normalized)
        if decision.quarantined:
            counts["quarantined"] += 1
        reject_secrets((
            ("memory title", title),
            ("memory content", content),
            ("memory metadata", metadata),
            ("memory provenance", provenance),
        ))
        record = MemoryRecord(
            id="",
            content=content,
            mtype=MemoryType(mtype_value),
            scope=Scope.REPO,
            workspace_id=wid or None,
            repo_id=rid,
            title=title,
            keywords=keywords,
            metadata=metadata,
            importance=importance,
            stability=stability,
            surprise=surprise,
            access_count=access_count,
            last_access=last_access,
            valid_from=created,
            valid_to=valid_to,
            valid_to_recorded_at=valid_to_recorded_at,
            ingested_at=created,
            provenance=provenance,
            embedding=embedding,
        )
        if store is not None:
            memory_id = store.add_memory(record)
            if decision.quarantined:
                store.audit(
                    "v1_migration",
                    "quarantine",
                    memory_id,
                    "policy=%s; reasons=%s"
                    % (decision.policy, ",".join(decision.reasons)),
                    commit=False,
                )

    if _has_table(src, "entities"):
        ecols = _columns(src, "entities")
        for row in src.execute("SELECT * FROM entities").fetchall():
            counts["entities"] += 1
            ns = namespace_value(
                row["namespace"] if "namespace" in ecols else "default"
            )
            entity_for(
                ns,
                row["name"],
                row["entity_type"] if "entity_type" in ecols else "",
                source_id=_source_id(row, ecols),
            )

    if _has_table(src, "edges"):
        gcols = _columns(src, "edges")
        for row in src.execute("SELECT * FROM edges").fetchall():
            counts["edges"] += 1
            repairs = []
            ns = namespace_value(
                row["namespace"] if "namespace" in gcols else "default"
            )
            weight = _legacy_float(
                row["weight"] if "weight" in gcols else 1.0,
                default=1.0,
                field="weight",
                repairs=repairs,
                nonnegative=True,
            )
            created = _legacy_float(
                row["created_at"] if "created_at" in gcols else migration_time,
                default=migration_time,
                field="created_at",
                repairs=repairs,
            )
            relation = str(
                (row["relation"] if "relation" in gcols else "") or ""
            ).strip()
            if not relation:
                raise ValueError("v1 migration found an edge with an empty relation")
            _, provenance = _untrusted_v1_metadata(
                {},
                source="v1:edge",
                namespace=ns,
                source_kind="edge",
                source_id=_source_id(row, gcols),
                repairs=tuple(repairs),
            )
            counts["repaired_fields"] += len(
                provenance.get("v1_normalized_fields", [])
            )
            edge = Edge(
                id="",
                src=edge_entity_for(ns, row["source_entity"]),
                dst=edge_entity_for(ns, row["target_entity"]),
                relation=relation,
                weight=weight,
                workspace_id=wid or None,
                repo_id=repo_for(ns),
                valid_from=created,
                ingested_at=created,
                provenance=provenance,
            )
            if store is not None:
                store.upsert_edge(edge)

    if _has_table(src, "events"):
        vcols = _columns(src, "events")
        for row in src.execute("SELECT * FROM events").fetchall():
            counts["events"] += 1
            if resume:
                event_key = json.dumps(
                    _legacy_scalar(_source_id(row, vcols)), sort_keys=True, ensure_ascii=True)
                if event_key in migrated_event_ids:
                    counts["resumed_skipped"] += 1
                    continue
            ns = namespace_value(
                row["namespace"] if "namespace" in vcols else "default"
            )
            rid = repo_for(ns)
            kind = str(
                (row["event_type"] if "event_type" in vcols else "event")
                or "event"
            )
            content = str(
                (row["description"] if "description" in vcols else "") or ""
            )
            refs = []
            source_id = _source_id(row, vcols)
            if source_id is not None:
                refs.append({"kind": "v1_event_id", "id": source_id})
            entity_name = str(
                (row["entity_name"] if "entity_name" in vcols else "") or ""
            ).strip()
            if entity_name:
                refs.append({"kind": "v1_entity", "name": entity_name})
            if "payload" in vcols:
                raw_payload = row["payload"]
                try:
                    payload = json.loads(raw_payload or "{}")
                except (TypeError, ValueError, RecursionError):
                    payload = str(raw_payload or "")
                refs.append({"kind": "v1_payload", "value": payload})
            event_repairs = []
            event_ts = _legacy_float(
                row["timestamp"] if "timestamp" in vcols else migration_time,
                default=migration_time,
                field="timestamp",
                repairs=event_repairs,
            )
            counts["repaired_fields"] += len(event_repairs)
            reject_secrets((("event content", content), ("event refs", refs)))
            if store is not None:
                store.append_event(
                    kind=kind,
                    content=content,
                    workspace_id=wid,
                    repo_id=rid,
                    refs=refs,
                    ts=event_ts,
                )

    if _has_table(src, "thoughts"):
        tcols = _columns(src, "thoughts")
        for row in src.execute("SELECT * FROM thoughts").fetchall():
            counts["thoughts"] += 1
            if resume:
                thought_key = json.dumps(
                    _legacy_scalar(_source_id(row, tcols)), sort_keys=True, ensure_ascii=True)
                if thought_key in migrated_thought_ids:
                    counts["resumed_skipped"] += 1
                    continue
            repairs = []
            ns = namespace_value(
                row["namespace"] if "namespace" in tcols else "default"
            )
            created = _legacy_float(
                row["created_at"] if "created_at" in tcols else migration_time,
                default=migration_time,
                field="created_at",
                repairs=repairs,
            )
            source_refs = []
            if "source_memory_ids" in tcols and row["source_memory_ids"]:
                decoded_refs = _decode_metadata(row["source_memory_ids"])
                if isinstance(decoded_refs, list):
                    validated_refs = []
                    for item in decoded_refs:
                        scalar = _legacy_scalar(item)
                        # A thought's source links must be non-empty JSON-safe
                        # scalars; anything else is a legacy encoding bug, not a
                        # lineage pointer. Drop it and record the repair.
                        if scalar is None or scalar == "" or scalar == [] or scalar == {}:
                            repairs.append("source_memory_ids")
                            continue
                        if type(scalar) not in (int, str, float):
                            repairs.append("source_memory_ids")
                            continue
                        validated_refs.append(scalar)
                    source_refs = validated_refs
                else:
                    repairs.append("source_memory_ids")
            title = "synthesized thought"
            content = str(
                (row["content"] if "content" in tcols else "") or ""
            )
            metadata, provenance = _untrusted_v1_metadata(
                {},
                source="v1:thought",
                namespace=ns,
                source_kind="thought",
                source_id=_source_id(row, tcols),
                repairs=tuple(repairs),
            )
            if source_refs:
                provenance["v1_source_memory_ids"] = source_refs
                metadata["provenance"] = dict(provenance)
            metadata, provenance, valid_to, valid_to_recorded_at, _, decision = (
                _quarantine_migrated_payload(
                    content,
                    title=title,
                    metadata=metadata,
                    provenance=provenance,
                    created=created,
                    embedding=None,
                )
            )
            counts["repaired_fields"] += len(
                provenance.get("v1_normalized_fields", [])
            )
            if decision.quarantined:
                counts["quarantined"] += 1
            reject_secrets((
                ("thought content", content),
                ("thought metadata", metadata),
                ("thought provenance", provenance),
            ))
            thought = MemoryRecord(
                id="",
                content=content,
                mtype=MemoryType.SEMANTIC,
                scope=Scope.REPO,
                workspace_id=wid or None,
                repo_id=repo_for(ns),
                title=title,
                metadata=metadata,
                valid_from=created,
                valid_to=valid_to,
                valid_to_recorded_at=valid_to_recorded_at,
                ingested_at=created,
                provenance=provenance,
            )
            if store is not None:
                memory_id = store.add_memory(thought)
                if decision.quarantined:
                    store.audit(
                        "v1_migration",
                        "quarantine",
                        memory_id,
                        "policy=%s; reasons=%s"
                        % (decision.policy, ",".join(decision.reasons)),
                        commit=False,
                    )

    counts["dim_histogram"] = {str(dim): count for dim, count in sorted(dim_histogram.items())}
    if store is not None:
        store.audit(
            "migration",
            "migrate_v1_to_v2",
            str(target_path),
            json.dumps(counts, sort_keys=True),
            commit=False,
        )
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
            dry_run: bool = False, resume: bool = False,
            reembed_queue: Optional[str] = None) -> dict:
    """Migrate through a same-directory stage and publish only a verified database.

    ``resume`` continues a previously interrupted migration into the existing
    ``--new`` database, skipping v1 source ids already carried across.
    ``reembed_queue`` names a file receiving one raw v1 id per line for every
    memory whose legacy vector is missing or undecodable.
    """
    source_path = Path(old_path).expanduser().resolve()
    target_path = Path(new_path).expanduser().resolve()
    if not source_path.is_file():
        raise FileNotFoundError(f"v1 migration source is not a file: {source_path}")
    if dry_run:
        return _migrate_to_path(
            str(source_path), str(target_path), workspace=workspace, dry_run=True,
            resume=resume, reembed_queue=reembed_queue,
        )
    if source_path == target_path:
        raise ValueError("v1 migration requires --new to differ from --old")
    if target_path.exists() and not resume:
        raise FileExistsError(
            "v1 migration requires a fresh --new path; refusing existing target "
            f"{target_path} (pass --resume to continue into it)"
        )
    if resume and target_path.exists():
        # Continue directly into the existing database: staging plus publish
        # would abandon the already-migrated rows the resume set was built from.
        return _migrate_to_path(
            str(source_path), str(target_path), workspace=workspace,
            resume=True, reembed_queue=reembed_queue,
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
            resume=resume, reembed_queue=reembed_queue,
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
        help="fresh v2 output path (must not already exist unless --dry-run/--resume)",
    )
    ap.add_argument("--workspace", default="default")
    ap.add_argument("--dry-run", action="store_true", help="report counts, write nothing")
    ap.add_argument("--resume", action="store_true",
                    help="continue an interrupted migration into the existing --new database")
    ap.add_argument("--reembed-queue", default=None,
                    help="write one raw v1 id per line for memories needing re-embedding")
    args = ap.parse_args()

    if not Path(args.old).exists():
        raise SystemExit(f"Old DB not found: {args.old}")

    counts = migrate(args.old, args.new, workspace=args.workspace, dry_run=args.dry_run,
                     resume=args.resume, reembed_queue=args.reembed_queue)
    mode = "DRY RUN - nothing written" if args.dry_run else f"written -> {args.new}"
    print(f"Engraphis migration ({mode})")
    for k, v in counts.items():
        print(f"  {k:10s}: {v}")


if __name__ == "__main__":
    main()
