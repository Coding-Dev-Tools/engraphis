"""Content-free identities and readiness policy for separate vector indexes."""
from __future__ import annotations

import hashlib
from typing import Optional, TYPE_CHECKING

from engraphis.core.interfaces import (
    vector_index_requires_sync,
    vector_index_shares_store_transaction,
)

if TYPE_CHECKING:
    from engraphis.core.store import Store


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
