"""NumPy brute-force vector index — the Phase-0 reference ``VectorIndex``.

This is intentionally simple and correct, not fast: it scans the (scope-filtered)
vectors for each query — the exact O(n) behaviour that is the #1 scale gap.
It exists so the rest of the system is runnable and testable *today*.
Phase 1 swaps in an ANN index (sqlite-vec / LanceDB / Qdrant) behind this same
interface; nothing above the ``VectorIndex`` boundary changes.
"""
from __future__ import annotations

from numbers import Integral
from typing import Optional

import numpy as np

from engraphis.core.interfaces import SearchFilter
from engraphis.core.store import Store


def _validated_dimension(dim: int) -> int:
    if isinstance(dim, bool) or not isinstance(dim, Integral) or int(dim) < 1:
        raise ValueError("embedding dimension must be a positive integer")
    return int(dim)


def _validated_k(k: int) -> int:
    if isinstance(k, bool) or not isinstance(k, Integral) or int(k) < 0:
        raise ValueError("k must be a non-negative integer")
    return int(k)


def _vector_batch(vecs: np.ndarray) -> np.ndarray:
    try:
        values = np.asarray(vecs, dtype=np.float32)
    except (TypeError, ValueError) as exc:
        raise ValueError("vectors must be a finite float32 array") from exc
    if values.ndim != 2 or values.shape[1] < 1:
        raise ValueError("vector batch must be a 2-D array of shape (n, dim>0)")
    if not np.isfinite(values).all():
        raise ValueError("vectors must contain only finite values")
    return values


def _vector_query(vec: np.ndarray) -> np.ndarray:
    try:
        values = np.asarray(vec, dtype=np.float32)
    except (TypeError, ValueError) as exc:
        raise ValueError("query vector must be a finite 1-D float32 array") from exc
    if values.ndim != 1 or values.shape[0] < 1:
        raise ValueError("query vector must be a 1-D array of shape (dim>0,)")
    if not np.isfinite(values).all():
        raise ValueError("query vector must contain only finite values")
    return values


class NumpyVectorIndex:
    """Store-backed brute-force cosine index. Vectors are stored normalized."""

    def __init__(self, store: Store, *, dim: Optional[int] = None) -> None:
        self.store = store
        self.dim = _validated_dimension(dim) if dim is not None else None

    def upsert(self, ids: list[str], vecs: np.ndarray, meta: Optional[list[dict]] = None) -> None:
        values = _vector_batch(vecs)
        if self.dim is not None and values.shape[1] != self.dim:
            raise ValueError(
                f"vector dimension {values.shape[1]} does not match the index dimension {self.dim}"
            )
        try:
            count = len(ids)
        except TypeError as exc:
            raise ValueError("ids must be a sequence matching the vector batch") from exc
        if count != values.shape[0]:
            raise ValueError(
                f"ids length {count} does not match vector batch size {values.shape[0]}"
            )
        if any(not isinstance(mid, str) or not mid for mid in ids):
            raise ValueError("ids must contain non-empty strings")
        if not count:
            return
        for i, mid in enumerate(ids):
            self.store.put_vector(mid, values[i])
        self.store.conn.commit()

    def search(self, vec: np.ndarray, k: int,
               *, filter: Optional[SearchFilter] = None) -> list[tuple[str, float]]:
        k = _validated_k(k)
        if k == 0:
            return []
        q = _vector_query(vec)
        if self.dim is not None and q.shape[0] != self.dim:
            raise ValueError(
                f"query dimension {q.shape[0]} does not match the index dimension {self.dim}"
            )
        with np.errstate(over="ignore", invalid="ignore"):
            n = float(np.linalg.norm(q))
        if not np.isfinite(n):
            raise ValueError("query vector norm must be finite")
        if n > 0:
            q = q / n
        rows = list(self.store.iter_vectors(
            filter, dim=self.dim if self.dim is not None else int(q.shape[0])
        ))
        if not rows:
            return []
        # Guard against heterogeneous stored dimensions (an embedder model or
        # ENGRAPHIS_EMBED_DIM change can leave legacy rows at a different width).
        # Skipping mismatched rows keeps the semantic arm alive instead of
        # raising on np.vstack and turning recall into a 500.
        matched = [(r[0], r[1]) for r in rows if r[1].shape[0] == q.shape[0]]
        if not matched:
            return []
        ids = [r[0] for r in matched]
        mat = np.vstack([r[1] for r in matched])       # already normalized on write
        scores = mat @ q                                # cosine == dot for unit vectors
        k = min(k, len(ids))
        # ``argpartition`` does not define which equal-scored rows survive at
        # the top-k boundary. Hashing embeddings produce ties frequently, so
        # use the memory id as an explicit stable secondary key.
        top = sorted(
            range(len(ids)), key=lambda index: (-float(scores[index]), ids[index])
        )[:k]
        return [(ids[index], float(scores[index])) for index in top]

    def delete(self, ids: list[str]) -> None:
        marks = ",".join("?" for _ in ids)
        if not ids:
            return
        self.store.conn.execute(f"DELETE FROM mem_vectors WHERE id IN ({marks})", ids)
        self.store.conn.commit()
