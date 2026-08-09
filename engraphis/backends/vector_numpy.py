"""Portable NumPy exact-vector reference backend.

The backend scans the scope-filtered vectors for every query, then performs a stable
exact top-k selection. It keeps the core dependency-light and deterministic; deployments
that need lower direct-search latency can select sqlite-vec's native exact-KNN backend.
The ``VectorIndex`` boundary also permits a future approximate index without changing
the recall pipeline.
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


def _top_k_indices(scores: np.ndarray, ids: list[str], k: int) -> list[int]:
    """Select the exact stable top-k without sorting an entire finite corpus.

    Search results promise descending cosine score with the memory id as the
    deterministic tie-breaker. ``partition`` finds the score cutoff in linear
    time, then we sort only scores above it plus the (usually tiny) tie boundary.
    A corpus made entirely of equal scores intentionally sorts that boundary,
    because every id participates in the observable ordering.
    """
    if k <= 0:
        return []
    if k >= len(ids) or not np.isfinite(scores).all():
        # A legacy caller can write non-finite vectors directly through Store. Keep
        # the prior Python ordering for that unsupported data rather than assigning
        # it new semantics in this hot-path optimization.
        return sorted(
            range(len(ids)), key=lambda index: (-float(scores[index]), ids[index])
        )[:k]

    cutoff_position = len(ids) - k
    cutoff = float(np.partition(scores, cutoff_position)[cutoff_position])
    above = np.flatnonzero(scores > cutoff).tolist()
    needed = k - len(above)
    boundary = np.flatnonzero(scores == cutoff).tolist()
    above.extend(sorted(boundary, key=ids.__getitem__)[:needed])
    above.sort(key=lambda index: (-float(scores[index]), ids[index]))
    return above


class NumpyVectorIndex:
    """Store-backed brute-force cosine index.

    Vectors are stored normalized. A zero vector has no cosine direction, so zero
    queries return no hits and zero corpus rows are omitted rather than assigned an
    arbitrary score. Native backends implement the same contract.
    """

    shares_store_vector_table = True

    def __init__(self, store: Store, *, dim: Optional[int] = None) -> None:
        self.store = store
        self.dim = _validated_dimension(dim) if dim is not None else None

    def upsert(self, ids: list[str], vecs: np.ndarray, meta: Optional[list[dict]] = None,
               *, commit: bool = True) -> None:
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
        if meta is not None and len(meta) != count:
            raise ValueError("meta length must match the vector batch")
        if not count:
            return
        default_model = str(
            self.store.embedding_rebuild_target()
            or self.store.active_embedding_space()
            or ""
        )
        models = [
            str(
                (meta[i] if meta is not None and isinstance(meta[i], dict) else {}).get(
                    "model"
                )
                or default_model
            )
            for i in range(count)
        ]
        conn = self.store.conn
        owns_transaction = not conn.transaction_owned_by_current_thread()
        try:
            if owns_transaction:
                conn.execute("BEGIN IMMEDIATE")
            for mid, value, model in zip(ids, values, models):
                self.store.put_vector(mid, value, model=model)
            # ``commit=True`` means settle a transaction this batch opened. It must
            # never steal a transaction already owned by the caller.
            if commit and owns_transaction and conn.transaction_owned_by_current_thread():
                conn.commit()
        except BaseException:
            if owns_transaction and conn.transaction_owned_by_current_thread():
                conn.rollback()
            raise

    def delete(self, ids: list[str], *, commit: bool = True) -> None:
        marks = ",".join("?" for _ in ids)
        if not ids:
            return
        conn = self.store.conn
        owns_transaction = not conn.transaction_owned_by_current_thread()
        try:
            if owns_transaction:
                conn.execute("BEGIN IMMEDIATE")
            conn.execute(f"DELETE FROM mem_vectors WHERE id IN ({marks})", ids)
            if commit and owns_transaction and conn.transaction_owned_by_current_thread():
                conn.commit()
        except BaseException:
            if owns_transaction and conn.transaction_owned_by_current_thread():
                conn.rollback()
            raise

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
        if n == 0:
            return []
        q = q / n
        ids, mat = self.store.vector_matrix(
            filter, dim=self.dim if self.dim is not None else int(q.shape[0])
        )
        if not ids:
            return []
        # Store filters by both the declared dimension and blob width, so legacy
        # rows from another embedding space cannot break this exact matrix scan.
        nonzero = np.any(mat != 0, axis=1)
        if not np.all(nonzero):
            ids = [memory_id for memory_id, keep in zip(ids, nonzero) if keep]
            mat = mat[nonzero]
            if not ids:
                return []
        scores = mat @ q                                # cosine == dot for unit vectors
        k = min(k, len(ids))
        top = _top_k_indices(scores, ids, k)
        return [(ids[index], float(scores[index])) for index in top]
