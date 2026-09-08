"""Content-free receipts and optimistic guards for atomic memory transitions.

Preparation is performed by the caller. Validation and completion must share the
Store writer reservation with the canonical write. No backend or transport lives
at this boundary.
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Optional, Protocol

from .interfaces import MemoryRecord, Scope
from .poisoning import metadata_is_quarantined


def governable_source(record: MemoryRecord, *, at: float) -> bool:
    """Accept current truth and quarantined evidence for governed derivations."""
    if record.expired_at is not None:
        return False
    if (
        metadata_is_quarantined(record.metadata)
        or bool((record.provenance or {}).get("quarantined"))
    ):
        return True
    return (
        (record.valid_from is None or record.valid_from <= at)
        and (record.valid_to is None or record.valid_to > at)
    )


class MemoryConflict(ValueError):
    """The requested transition no longer applies; refresh before another write."""

    def __init__(self, message: str, *, code: str = "memory_conflict") -> None:
        super().__init__(message)
        self.code = code


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
    ).encode("utf-8")).hexdigest()


def memory_version(record: MemoryRecord) -> str:
    """Portable edit version; ordinary reinforcement does not change the version."""
    fields = (
        "id", "workspace_id", "repo_id", "session_id", "scope", "mtype",
        "title", "content", "summary", "keywords", "importance", "confidence",
        "provenance", "metadata", "pinned", "sensitivity", "subject_key",
        "claim_kind", "valid_from", "valid_to", "valid_to_recorded_at",
        "ingested_at", "expired_at", "modified_hlc",
    )
    return "mv1:" + _digest({name: getattr(record, name, None) for name in fields})


class MutationStore(Protocol):
    conn: Any

    def get_memory(self, memory_id: str) -> Optional[MemoryRecord]: ...

    def get_session(self, session_id: str) -> Optional[dict]: ...


def can_revise(record: Optional[MemoryRecord], store: MutationStore) -> bool:
    """Read-side hint only; the command revalidates under the writer."""
    if record is None or not governable_source(record, at=time.time()):
        return False
    if record.scope == Scope.SESSION:
        session = store.get_session(str(record.session_id or ""))
        if session is None or session.get("status") != "active":
            return False
    unclaimed = store.conn.execute(
        "SELECT 1 FROM memory_command_sources WHERE source_id=?", (record.id,),
    ).fetchone() is None
    if not unclaimed:
        return False
    # Pre-command approvals preserved their pending source. Match structured
    # lineage, never title similarity, including a successor later retired.
    if record.provenance.get("review_state") == "pending":
        rows = store.conn.execute(
            "SELECT provenance,metadata FROM memories WHERE workspace_id=? "
            "AND (instr(provenance,?)>0 OR instr(metadata,?)>0)",
            (record.workspace_id, record.id, record.id),
        )
        for row in rows:
            try:
                provenance = json.loads(row["provenance"] or "{}")
                metadata = json.loads(row["metadata"] or "{}")
            except (ValueError, TypeError):
                continue
            if (isinstance(provenance, dict) and isinstance(metadata, dict)
                    and provenance.get("review_state") == "approved"
                    and (provenance.get("approved_from") == record.id
                         or metadata.get("approved_from") == record.id)):
                return False
    return True


class MemoryCommand:
    """An idempotent operation and its prepared source versions.

Receipt rows contain only identifiers, fingerprints and timestamps. Source claims
also protect quarantined records whose empty validity interval cannot be closed
again. They survive erasure so a delayed request cannot recreate its result.
"""

    def __init__(self, store: MutationStore, operation: str,
                 sources: list[MemoryRecord], payload: dict[str, Any], *,
                 operation_id: Optional[str] = None) -> None:
        self.store = store
        self.sources = sources
        self.operation = operation
        self.workspace_id = sources[0].workspace_id
        self.versions = {record.id: memory_version(record) for record in sources}
        self.request_hash = _digest({
            "operation": operation, "sources": sorted(self.versions), "payload": payload,
        })
        self.operation_id = operation_id or "auto:" + self.request_hash

    def replay(self) -> Optional[dict[str, Any]]:
        row = self.store.conn.execute(
            "SELECT request_hash, result_id, result_version FROM memory_commands "
            "WHERE workspace_id=? AND operation_id=?",
            (self.workspace_id, self.operation_id),
        ).fetchone()
        if row is None:
            return None
        if row["request_hash"] != self.request_hash:
            raise MemoryConflict("operation ID was used for a different request",
                                 code="operation_conflict")
        result = self.store.get_memory(row["result_id"])
        if result is None:
            raise MemoryConflict("operation result was erased; it cannot be recreated",
                                 code="result_unavailable")
        if result.expired_at is not None or (
            result.valid_to is not None and result.valid_to <= time.time()
            and not metadata_is_quarantined(result.metadata)
        ):
            raise MemoryConflict("operation result has been superseded or retired",
                                 code="result_unavailable")
        return {"id": row["result_id"], "op": "noop", "version": row["result_version"]}

    def validate(self) -> Optional[dict[str, Any]]:
        if not self.store.conn.transaction_owned_by_current_thread():
            raise RuntimeError("memory command requires a writer reservation")
        replay = self.replay()
        if replay is not None:
            return replay
        for record in self.sources:
            current = self.store.get_memory(record.id)
            if current is None or memory_version(current) != self.versions[record.id]:
                raise MemoryConflict("memory changed during preparation; refresh before editing")
            claimed = self.store.conn.execute(
                "SELECT operation_id FROM memory_command_sources WHERE source_id=?",
                (record.id,),
            ).fetchone()
            if claimed is not None:
                raise MemoryConflict("memory already has a successor; refresh before editing")
        return None

    def complete(self, result_id: str) -> None:
        result = self.store.get_memory(result_id)
        if result is None:
            raise RuntimeError("memory command result was not stored")
        self.store.conn.execute(
            "INSERT INTO memory_commands(workspace_id, operation_id, operation, "
            "request_hash, result_id, result_version, created_at) VALUES(?,?,?,?,?,?,?)",
            (self.workspace_id, self.operation_id, self.operation, self.request_hash,
             result_id, memory_version(result), time.time()),
        )
        for record in self.sources:
            self.store.conn.execute(
                "INSERT INTO memory_command_sources(source_id, workspace_id, operation_id) "
                "VALUES(?,?,?)", (record.id, self.workspace_id, self.operation_id),
            )
