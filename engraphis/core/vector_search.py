"""Bounded exact search over the canonical vector store."""
from __future__ import annotations

from contextlib import closing
from typing import Iterable, Optional, TYPE_CHECKING

import numpy as np

from engraphis.core.interfaces import SearchFilter

if TYPE_CHECKING:
    from engraphis.core.store import Store


def top_k_indices(scores: np.ndarray, ids: list[str], k: int) -> list[int]:
    """Select exact top-k with the memory id as the stable cutoff tie-breaker."""
    if k <= 0:
        return []
    if k >= len(ids) or not np.isfinite(scores).all():
        return sorted(range(len(ids)), key=lambda i: (-float(scores[i]), ids[i]))[:k]
    cutoff = float(np.partition(scores, len(ids) - k)[len(ids) - k])
    above = np.flatnonzero(scores > cutoff).tolist()
    boundary = np.flatnonzero(scores == cutoff).tolist()
    above.extend(sorted(boundary, key=ids.__getitem__)[:k - len(above)])
    above.sort(key=lambda i: (-float(scores[i]), ids[i]))
    return above


def _rank_batches(
    batches: Iterable[tuple[list[str], np.ndarray]], query: np.ndarray, k: int,
) -> list[tuple[str, float]]:
    winners: list[tuple[str, float]] = []
    for ids, matrix in batches:
        nonzero = np.any(matrix != 0, axis=1)
        if not np.all(nonzero):
            ids = [memory_id for memory_id, keep in zip(ids, nonzero) if keep]
            matrix = matrix[nonzero]
        if not ids:
            continue
        scores = matrix @ query
        winners.extend((ids[i], float(scores[i])) for i in top_k_indices(scores, ids, k))
        winners.sort(key=lambda row: (-row[1], row[0]))
        del winners[k:]
    return winners


def canonical_vector_search(
    store: "Store",
    vec: np.ndarray,
    k: int,
    *,
    filter: Optional[SearchFilter] = None,
    batches: Optional[Iterable[tuple[list[str], np.ndarray]]] = None,
) -> list[tuple[str, float]]:
    """Read a consistent snapshot using O(batch * dimension + k) vector memory.

    ``batches`` lets a visibility-token-keyed caller supply an already-materialized,
    identical batch stream (the portable NumPy index cache). That cache refuses a hit
    when the store token changes, when a dynamic temporal anchor crosses its next
    clock boundary, and whenever an entry would exceed the caller's byte budget, in
    which case the remaining data is streamed instead. The scoring, tie-break
    ordering, zero-row masking and float32 arithmetic are unchanged in both modes.
    """
    query = np.asarray(vec, dtype=np.float32)
    if query.ndim != 1 or query.size == 0 or not np.isfinite(query).all():
        raise ValueError("query vector must be a finite non-empty one-dimensional array")
    with np.errstate(over="ignore", invalid="ignore"):
        norm = float(np.linalg.norm(query))
    if not np.isfinite(norm):
        raise ValueError("query vector norm must be finite")
    if norm == 0 or k <= 0:
        return []
    query = query / norm
    if batches is None:
        with closing(store.iter_vector_matrices(filter, dim=int(query.size))) as stream:
            return _rank_batches(stream, query, k)
    try:
        return _rank_batches(batches, query, k)
    finally:
        close = getattr(batches, "close", None)
        if callable(close):
            close()
