"""Content-free identities and readiness policy for separate vector indexes."""
from __future__ import annotations

import hashlib
import logging
from typing import Any, Callable, Iterator, Optional, TYPE_CHECKING

import numpy as np

from engraphis.core.interfaces import (
    vector_index_requires_sync,
    vector_index_shares_store_transaction,
)
from engraphis.core.poisoning import inspection_eligible
from engraphis.core.store import _is_memory_database_path

if TYPE_CHECKING:
    from engraphis.core.store import Store


logger = logging.getLogger("engraphis.core.vector_repair")


def index_repair_identity(index, store: "Store") -> Optional[str]:
    """Keep credentials and connection details out of durable repair metadata.

    External adapters should provide a stable ``index_identity`` unique to their
    physical index. Unidentified adapters are supported but never used as the
    authority for complete search results; canonical search remains available.
    """
    if (not vector_index_requires_sync(index, store)
            or vector_index_shares_store_transaction(index, store)):
        return None
    identity = str(getattr(index, "index_identity", "") or "")
    namespace = f"{type(index).__module__}.{type(index).__qualname__}"
    digest = hashlib.sha256(f"{namespace}\n{identity}".encode("utf-8")).hexdigest()
    return f"index:v1:{digest}"


def canonical_search_required(index, store: "Store", *,
                              unregistered_is_uncertain: bool = True) -> bool:
    identity = index_repair_identity(index, store)
    if identity is None:
        return False
    # Physical loss can be reported before startup has reseeded the durable queue.
    if getattr(index, "requires_rebuild", False) is True:
        return True
    pending = store.vector_index_pending(identity)
    # A standalone RecallEngine may use a read-only/testing retrieval adapter
    # which has never participated in MemoryEngine's durable write lifecycle.
    if pending is None:
        return unregistered_is_uncertain
    return (
        not getattr(index, "index_identity", None)
        or pending != 0
    )


def _repair_candidates(store: "Store", target: str, memory_id: Optional[str],
                       ceiling: tuple[int, str]) -> Iterator[tuple[str, int]]:
    """Page queue identities without loading vectors or revisiting failed work."""
    after: Optional[tuple[int, str]] = None
    while True:
        sql = (
            "SELECT memory_id,generation FROM vector_index_repairs WHERE identity=? "
            "AND (generation,memory_id)<=(?,?)"
        )
        params: list[Any] = [target, *ceiling]
        if memory_id is not None:
            sql += " AND memory_id=?"
            params.append(memory_id)
        if after is not None:
            sql += " AND (generation,memory_id)>(?,?)"
            params.extend(after)
        rows = store.conn.execute(
            sql + " ORDER BY generation,memory_id LIMIT 100", params,
        ).fetchall()
        if not rows:
            return
        after = (int(rows[-1]["generation"]), str(rows[-1]["memory_id"]))
        for row in rows:
            yield str(row["memory_id"]), int(row["generation"])


def repair_vector_index(store: "Store", index: Any, *, embedding_space: str,
                        dim: int, limit: int = 100, memory_id: Optional[str] = None,
                        upsert: Optional[Callable[..., None]] = None,
                        actor: str = "engine") -> dict[str, int]:
    """Replay durable external work from current canonical state under the writer.

    A delayed caller supplies only a memory id. Captured payloads cannot overwrite
    newer vectors or resurrect erased records. Optional ``upsert`` preserves the
    public engine's compatibility adapter without coupling this coordinator to it.
    Cleanup precedes upserts, including when ``limit=1``. The limit bounds provider
    attempts; finding cleanup may inspect the whole pending queue in 100-row
    pages. Repeated calls can rescan pending upserts; this is not a latency bound.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise ValueError("repair limit must be an integer between 1 and 1000")
    target = index_repair_identity(index, store)
    if target is None:
        return {"attempted": 0, "repaired": 0, "pending": 0}
    if store.read_only or store.conn.transaction_owned_by_current_thread():
        raise RuntimeError("vector repair requires an independent writable transaction")
    store.register_vector_index(target)
    vector_writes_ready = (
        (_is_memory_database_path(store.path) and store.active_embedding_space() is None)
        or store.embedding_space_ready(embedding_space)
    )
    sql = "SELECT generation,memory_id FROM vector_index_repairs WHERE identity=?"
    params: list[Any] = [target]
    if memory_id is not None:
        sql += " AND memory_id=?"
        params.append(memory_id)
    last = store.conn.execute(
        sql + " ORDER BY generation DESC,memory_id DESC LIMIT 1", params,
    ).fetchone()
    if last is None:
        return {"attempted": 0, "repaired": 0,
                "pending": store.vector_index_pending(target) or 0}
    # New generations belong to the next invocation, even when a provider callback
    # changes canonical state during publication. Do not chase an expanding queue.
    ceiling = (int(last["generation"]), str(last["memory_id"]))
    attempted = repaired = 0
    for cleanup_only in (True, False):
        if attempted >= limit or (not cleanup_only and not vector_writes_ready):
            break
        for selected_id, generation in _repair_candidates(store, target, memory_id, ceiling):
            if attempted >= limit:
                break
            operation = "delete" if cleanup_only else "upsert"
            try:
                with store.write_transaction():
                    current = store.conn.execute(
                        "SELECT generation FROM vector_index_repairs "
                        "WHERE identity=? AND memory_id=?", (target, selected_id),
                    ).fetchone()
                    if current is None or int(current["generation"]) != generation:
                        continue
                    record = store.get_memory(selected_id)
                    vector = store.conn.execute(
                        "SELECT 1 FROM mem_vectors WHERE id=?" if cleanup_only else
                        "SELECT vector,dim,model FROM mem_vectors WHERE id=?", (selected_id,),
                    ).fetchone()
                    needs_upsert = (
                        record is not None and vector is not None
                        and inspection_eligible(record.provenance, record.metadata)
                    )
                    if needs_upsert == cleanup_only:
                        continue
                    attempted += 1
                    if needs_upsert:
                        assert vector is not None
                        if str(vector["model"] or "") != embedding_space or int(vector["dim"]) != dim:
                            raise RuntimeError("canonical vector space changed during repair")
                        values = np.frombuffer(vector["vector"], dtype=np.float32).reshape(1, -1)
                        meta = [{"model": embedding_space}]
                        if upsert is None:
                            index.upsert([selected_id], values, meta)
                        else:
                            upsert(index, [selected_id], values, meta)
                    else:
                        # Erasure/quarantine cleanup requires no compatible embedder.
                        index.delete([selected_id])
                    store.acknowledge_vector_index_repairs(target, {selected_id: generation})
                repaired += 1
            except Exception as exc:  # noqa: BLE001 - failed work stays durable for a later retry
                logger.warning("vector-index repair failed for %s (%s)", selected_id, type(exc).__name__)
                try:
                    store.audit(actor, f"index_{operation}_failed", selected_id,
                                f"failure_type={type(exc).__name__}")
                except Exception as audit_exc:  # noqa: BLE001 - preserve original durable repair debt
                    logger.warning("could not audit vector-index repair failure (%s)",
                                   type(audit_exc).__name__)
                if not cleanup_only:
                    # Cleanup has already had its turn; retain fail-fast publication
                    # during an upsert outage instead of repeatedly calling the provider.
                    break
    return {"attempted": attempted, "repaired": repaired,
            "pending": store.vector_index_pending(target) or 0}
