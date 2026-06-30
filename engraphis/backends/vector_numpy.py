"""NumPy brute-force vector index — the Phase-0 reference ``VectorIndex``.

This is intentionally simple and correct, not fast: it scans the (scope-filtered)
vectors for each query — the exact O(n) behaviour MASTER_PLAN.md §3.2 flags as
the #1 gap. It exists so the rest of the system is runnable and testable *today*.
Phase 1 swaps in an ANN index (sqlite-vec / LanceDB / Qdrant) behind this same
interface; nothing above the ``VectorIndex`` boundary changes.
"""
from __future__ import annotations

from typing import Optional

import numpy as np

from engraphis.core.interfaces import SearchFilter
from engraphis.core.store import Store


class NumpyVectorIndex:
    """Store-backed brute-force cosine index. Vectors are stored normalized."""

    def __init__(self, store: Store) -> None:
        self.store = store

    def upsert(self, ids: list[str], vecs: np.ndarray, meta: Optional[list[dict]] = None) -> None:
        vecs = np.asarray(vecs, dtype=np.float32)
        for i, mid in enumerate(ids):
            self.store.put_vector(mid, vecs[i])
        self.store.conn.commit()

    def search(self, vec: np.ndarray, k: int,
               *, filter: Optional[SearchFilter] = None) -> list[tuple[str, float]]:
        q = np.asarray(vec, dtype=np.float32)
        n = float(np.linalg.norm(q))
        if n > 0:
            q = q / n
        rows = list(self.store.iter_vectors(filter))
        if not rows:
            return []
        ids = [r[0] for r in rows]
        mat = np.vstack([r[1] for r in rows])          # already normalized on write
        scores = mat @ q                                # cosine == dot for unit vectors
        k = min(k, len(ids))
        top = np.argpartition(-scores, k - 1)[:k]
        top = top[np.argsort(-scores[top])]
        return [(ids[i], float(scores[i])) for i in top]

    def delete(self, ids: list[str]) -> None:
        marks = ",".join("?" for _ in ids)
        if not ids:
            return
        self.store.conn.execute(f"DELETE FROM mem_vectors WHERE id IN ({marks})", ids)
        self.store.conn.commit()
