"""Portable NumPy exact-vector reference backend.

The backend scans the scope-filtered vectors for every query, then performs a stable
exact top-k selection. It keeps the core dependency-light and deterministic; deployments
that need lower direct-search latency can select sqlite-vec's native exact-KNN backend.
The ``VectorIndex`` boundary also permits a future approximate index without changing
the recall pipeline.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
from contextlib import closing
from numbers import Integral
from typing import Optional, cast

import numpy as np

from engraphis.core import store as core_store
from engraphis.core.interfaces import SearchFilter
from engraphis.core.store import Store
from engraphis.core.vector_search import (
    canonical_vector_search,
    top_k_indices,
)

# Retain the reference helper import used by backend contract tests/adapters.
_top_k_indices = top_k_indices
logger = logging.getLogger(__name__)

#: Default retained budget for one decoded matrix cache (64 MiB).
DEFAULT_MAX_CACHE_BYTES = 64 * 1024 * 1024


def _default_max_cache_bytes() -> int:
    raw = os.environ.get("ENGRAPHIS_VECTOR_CACHE_BYTES", "").strip()
    if not raw:
        return DEFAULT_MAX_CACHE_BYTES
    try:
        return _validated_max_cache_bytes(int(raw))
    except ValueError:
        logger.warning("ignoring invalid ENGRAPHIS_VECTOR_CACHE_BYTES=%r; using %d", raw, DEFAULT_MAX_CACHE_BYTES)
        return DEFAULT_MAX_CACHE_BYTES


def _validated_dimension(dim: int) -> int:
    if isinstance(dim, bool) or not isinstance(dim, Integral) or int(dim) < 1:
        raise ValueError("embedding dimension must be a positive integer")
    return int(dim)


def _validated_k(k: int) -> int:
    if isinstance(k, bool) or not isinstance(k, Integral) or int(k) < 0:
        raise ValueError("k must be a non-negative integer")
    return int(k)


def _validated_max_cache_bytes(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral) or int(value) < 0:
        raise ValueError("max_cache_bytes must be a non-negative integer")
    return int(value)


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


def _enum_key(values) -> Optional[tuple[str, ...]]:
    return None if values is None else tuple(sorted(str(getattr(v, "value", v)) for v in values))


def _batch_bytes(ids: tuple[str, ...], matrix: np.ndarray) -> int:
    return int(matrix.nbytes) + sys.getsizeof(ids) + sum(sys.getsizeof(mid) for mid in ids)


def _stream_batches(stream):
    for ids, matrix in stream:
        yield list(ids), matrix


def _store_batches(store, flt: Optional[SearchFilter], dim: int):
    stream = store.iter_vector_matrices(flt, dim=dim)
    try:
        yield from _stream_batches(stream)
    finally:
        close = getattr(stream, "close", None)
        if callable(close):
            close()


def _filter_key(store, flt: Optional[SearchFilter]) -> tuple:
    """Hashable identity for everything that changes the query, authorization included."""
    allowed = getattr(store, "allowed_workspaces", None)
    allowed_key = None if allowed is None else tuple(sorted(str(name) for name in allowed))
    if flt is None:
        return (allowed_key,)
    return (
        allowed_key, flt.workspace_id, flt.repo_id, flt.session_id,
        _enum_key(flt.scopes), _enum_key(flt.mtypes), flt.include_ancestors,
        flt.valid_at, flt.known_at, flt.modified_since,
    )


class NumpyVectorIndex:
    """Store-backed brute-force cosine index.

    Vectors are stored normalized. A zero vector has no cosine direction, so zero
    queries return no hits and zero corpus rows are omitted rather than assigned an
    arbitrary score. Native backends implement the same contract.
    """

    shares_store_vector_table = True

    def __init__(self, store: Store, *, dim: Optional[int] = None,
                 max_cache_bytes: Optional[int] = None) -> None:
        self.store = store
        self.dim = _validated_dimension(dim) if dim is not None else None
        self.max_cache_bytes = (
            _default_max_cache_bytes() if max_cache_bytes is None
            else _validated_max_cache_bytes(max_cache_bytes)
        )
        self._cache_lock = threading.Lock()
        self._cache_key: Optional[tuple] = None
        self._cache_batches: tuple[tuple[tuple[str, ...], np.ndarray], ...] = ()
        self._cache_valid_from: float = 0.0
        self._cache_valid_until: Optional[float] = None

    def _invalidate_cache(self) -> None:
        with self._cache_lock:
            self._cache_key = None
            self._cache_batches = ()
            self._cache_valid_from = 0.0
            self._cache_valid_until = None

    def _cached_batches(self, flt: Optional[SearchFilter], dim: int):
        """Yield the exact batch stream ``Store.iter_vector_matrices`` would produce."""
        if not self.max_cache_bytes:
            if self._cache_key is not None:
                self._invalidate_cache()
            yield from _store_batches(self.store, flt, dim)
            return
        conn = getattr(self.store, "conn", None)
        # Uncommitted rows in a transaction owned by the current thread stream directly.
        in_tx = bool(conn is not None and (
            conn.transaction_owned_by_current_thread()
            if hasattr(conn, "transaction_owned_by_current_thread")
            else getattr(conn, "in_transaction", False)
        ))
        if in_tx:
            yield from _store_batches(self.store, flt, dim)
            return
        token_of = getattr(self.store, "vector_visibility_token", None) or getattr(
            self.store, "vector_generation", None)
        if not callable(token_of):
            yield from _store_batches(self.store, flt, dim)
            return
        now = core_store.now_ts()
        connection = (getattr(self.store, "_connection_epoch", 0), id(getattr(conn, "_raw", None)))
        key = (token_of(), int(dim), _filter_key(self.store, flt), connection)
        with self._cache_lock:
            batches = self._cache_batches if self._cache_key == key else None
            cached_from, cached_until = self._cache_valid_from, self._cache_valid_until
        if batches is not None:
            if cached_from <= now and (cached_until is None or now < cached_until):
                yield from _stream_batches(batches)
                return
            self._invalidate_cache()
        retained: list[tuple[tuple[str, ...], np.ndarray]] = []
        retained_bytes, over_budget = 0, False
        for raw_ids, matrix in _store_batches(self.store, flt, dim):
            ids = tuple(raw_ids)
            if not over_budget:
                retained_bytes += _batch_bytes(ids, matrix)
                if retained_bytes > self.max_cache_bytes:
                    over_budget, retained_bytes = True, 0
                    retained.clear()
                    self._invalidate_cache()
                else:
                    matrix.setflags(write=False)
                    retained.append((ids, matrix))
            yield list(ids), matrix
        if not over_budget:
            world = now if flt is None or flt.valid_at is None else flt.valid_at
            known = now if flt is None or flt.known_at is None else flt.known_at
            transition_of = getattr(self.store, "next_vector_visibility_transition", None)
            valid_until = cast(Optional[float], transition_of(flt, world, known)) if callable(transition_of) else None
            with self._cache_lock:
                self._cache_key = key
                self._cache_batches = tuple(retained)
                self._cache_valid_from = now
                self._cache_valid_until = valid_until

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
        self._invalidate_cache()

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
        self._invalidate_cache()

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
        with closing(self._cached_batches(filter, int(q.shape[0]))) as stream:
            return canonical_vector_search(
                self.store, q, k, filter=filter, batches=stream,
            )
