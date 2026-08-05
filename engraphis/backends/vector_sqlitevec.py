"""sqlite-vec native exact-KNN backend + factory.

Replaces Python/NumPy scan orchestration with an embedded native vector index in the
same SQLite file — preserving the local-first, single-file story. If the
``sqlite-vec`` extension is not installable in the current environment, the
factory transparently falls back to ``NumpyVectorIndex`` (so nothing breaks),
which is exactly what happens in restricted CI sandboxes.

Stable ``vec0`` performs exact KNN; it is not a sublinear ANN algorithm.

Note: sqlite-vec cannot apply Engraphis' bi-temporal/workspace filter inside the vec0
MATCH directly, so ``search`` expands the KNN window until it has enough visible hits.
"""
from __future__ import annotations

import importlib
import re
import sys
from numbers import Integral
from typing import Optional

import numpy as np

from engraphis.backends.embedder_deterministic import MAX_EMBEDDING_DIM
from engraphis.backends.vector_numpy import NumpyVectorIndex
from engraphis.core.interfaces import SearchFilter
from engraphis.core.store import Store, memory_matches_filter


def _visible(rec, flt: SearchFilter) -> bool:
    return memory_matches_filter(rec, flt)


def _cosine_from_l2(distance: float) -> float:
    """Convert Euclidean distance between unit vectors back to cosine similarity."""
    return max(-1.0, min(1.0, 1.0 - (float(distance) ** 2) / 2.0))


def _validated_dimension(dim: int) -> int:
    """Return a bounded integer safe to interpolate into sqlite-vec DDL."""
    if isinstance(dim, bool) or not isinstance(dim, Integral):
        raise ValueError("embedding dimension must be a positive integer")
    dimension = int(dim)
    if not 1 <= dimension <= MAX_EMBEDDING_DIM:
        raise ValueError(
            f"embedding dimension must be between 1 and {MAX_EMBEDDING_DIM}"
        )
    return dimension


def _validated_k(k: int) -> int:
    if isinstance(k, bool) or not isinstance(k, Integral) or int(k) < 0:
        raise ValueError("k must be a non-negative integer")
    return int(k)


def _vector_batch(vecs: np.ndarray, dim: int) -> np.ndarray:
    try:
        values = np.asarray(vecs, dtype=np.float32)
    except (TypeError, ValueError) as exc:
        raise ValueError("vectors must be a finite float32 array") from exc
    if values.ndim != 2 or values.shape[1] != dim:
        actual = values.shape[1] if values.ndim == 2 else "?"
        raise ValueError(
            f"vector dimension {actual} does not match the index dimension {dim}"
        )
    if not np.isfinite(values).all():
        raise ValueError("vectors must contain only finite values")
    return values


def _vector_query(vec: np.ndarray, dim: int) -> np.ndarray:
    try:
        values = np.asarray(vec, dtype=np.float32)
    except (TypeError, ValueError) as exc:
        raise ValueError("query vector must be a finite 1-D float32 array") from exc
    if values.ndim != 1 or values.shape[0] != dim:
        actual = values.shape[0] if values.ndim == 1 else "?"
        raise ValueError(
            f"query dimension {actual} does not match the index dimension {dim}"
        )
    if not np.isfinite(values).all():
        raise ValueError("query vector must contain only finite values")
    return values


class SqliteVecVectorIndex:
    """Native exact KNN over embeddings using the sqlite-vec extension."""

    shares_store_vector_table = False

    def __init__(self, store: Store, dim: int) -> None:
        dimension = _validated_dimension(dim)
        # sqlite-vec is a loadable SQLite extension.  SQLCipher ships a different
        # SQLite build, and loading both native libraries into one interpreter has
        # caused hard crashes rather than a normal Python exception.  An `auto`
        # request below can safely use NumPy instead; an explicit sqlite-vec
        # request gets this actionable error before any unsafe native call.
        if any(name == "sqlcipher3" or name.startswith("sqlcipher3.")
               for name in sys.modules):
            raise RuntimeError(
                "sqlite-vec cannot share a process with SQLCipher; use "
                "vector_backend='numpy' or run the accelerated backend in a fresh process"
            )
        sqlite_vec = importlib.import_module('sqlite_vec')  # lazy optional extension
        self.store = store
        self.dim = dimension
        conn = store.conn
        conn.enable_load_extension(True)
        try:
            sqlite_vec.load(conn)
        finally:
            # Never leave extension loading enabled on a shared connection, including
            # when the optional native load fails.
            conn.enable_load_extension(False)
        existing = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='mem_vec_ann'"
        ).fetchone()
        if existing and existing["sql"]:
            match = re.search(r"FLOAT\s*\[\s*(\d+)\s*\]", existing["sql"], re.IGNORECASE)
            if match and int(match.group(1)) != dimension:
                raise ValueError(
                    f"existing vector index dimension {match.group(1)} does not match "
                    f"requested dimension {dimension}"
                )
        conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS mem_vec_ann USING vec0("
            f"id TEXT PRIMARY KEY, embedding FLOAT[{dimension}])"
        )
        conn.commit()

    def upsert(self, ids: list[str], vecs: np.ndarray, meta: Optional[list[dict]] = None,
               *, commit: bool = True) -> None:
        values = _vector_batch(vecs, self.dim)
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
        normalized = values.astype(np.float64, copy=True)
        with np.errstate(over="ignore", invalid="ignore"):
            norms = np.linalg.norm(normalized, axis=1)
        if not np.isfinite(norms).all():
            raise ValueError("vector norm must be finite")
        nonzero = norms > 0
        normalized[nonzero] /= norms[nonzero, None]
        normalized = normalized.astype(np.float32)

        conn = self.store.conn
        owns_transaction = not conn.transaction_owned_by_current_thread()
        try:
            if owns_transaction:
                conn.execute("BEGIN IMMEDIATE")
            # ``vec0`` virtual tables do not implement SQLite's conflict-resolution
            # algorithms consistently: INSERT OR REPLACE can still raise a UNIQUE
            # constraint error when a persisted row is hydrated after reopening a
            # database. Delete the batch first, then insert the replacement rows in
            # the same transaction so restart hydration remains idempotent and
            # failures roll back to the previous index state.
            marks = ",".join("?" for _ in ids)
            conn.execute(f"DELETE FROM mem_vec_ann WHERE id IN ({marks})", ids)
            for mid, vector in zip(ids, normalized):
                conn.execute(
                    "INSERT INTO mem_vec_ann(id, embedding) VALUES (?, ?)",
                    (mid, vector.tobytes()),
                )
            if commit and owns_transaction and conn.transaction_owned_by_current_thread():
                conn.commit()
        except BaseException:
            if owns_transaction and conn.transaction_owned_by_current_thread():
                conn.rollback()
            raise

    def delete(self, ids: list[str], *, commit: bool = True) -> None:
        if not ids:
            return
        marks = ",".join("?" for _ in ids)
        conn = self.store.conn
        owns_transaction = not conn.transaction_owned_by_current_thread()
        try:
            if owns_transaction:
                conn.execute("BEGIN IMMEDIATE")
            conn.execute(f"DELETE FROM mem_vec_ann WHERE id IN ({marks})", ids)
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
        v = _vector_query(vec, self.dim)
        with np.errstate(over="ignore", invalid="ignore"):
            n = float(np.linalg.norm(v))
        if not np.isfinite(n):
            raise ValueError("query vector norm must be finite")
        if n > 0:
            v = v / n
        total_row = self.store.conn.execute(
            "SELECT COUNT(*) AS n FROM mem_vec_ann"
        ).fetchone()
        total = int(total_row["n"]) if total_row is not None else 0
        if total == 0:
            return []
        # Fetch one look-ahead row so the common unique-distance case can prove the
        # kth boundary complete without issuing a second metadata hydration query.
        limit = min(k + 1, total)
        while True:
            # The KNN cap uses vec0's explicit `k = ?` constraint, NOT `LIMIT ?`.
            rows = self.store.conn.execute(
                "SELECT id, distance FROM mem_vec_ann WHERE embedding MATCH ? "
                "AND k = ? ORDER BY distance",
                (v.tobytes(), int(limit)),
            ).fetchall()
            # Match NumPy's live-record contract even for direct callers that omit a
            # filter; orphaned, closed, and future ANN rows must never leak.
            effective_filter = filter if filter is not None else SearchFilter()
            visible_records = self.store.get_memories(row["id"] for row in rows)
            eligible = []
            for row in rows:
                rec = visible_records.get(row["id"])
                if rec is None or not _visible(rec, effective_filter):
                    continue
                eligible.append(row)
            eligible.sort(key=lambda row: (float(row["distance"]), str(row["id"])))

            # vec0 may choose an unspecified subset when equal-distance rows straddle
            # its k boundary. Widen until the raw boundary is strictly farther than
            # the kth visible row; then the complete tie group is present and the
            # memory-id secondary order is deterministic across backends and runs.
            exhausted = len(rows) < limit or limit >= total
            enough = len(eligible) >= k
            boundary_complete = (
                enough
                and (
                    exhausted
                    or float(rows[-1]["distance"]) > float(eligible[k - 1]["distance"])
                )
            )
            if boundary_complete or exhausted:
                selected = eligible[:k]
                # A zero query has no direction; retain the NumPy backend's
                # deterministic zero similarity rather than converting its
                # distance to the mathematically unrelated 0.5.
                return [
                    (
                        row["id"],
                        0.0 if n == 0 else _cosine_from_l2(row["distance"]),
                    )
                    for row in selected
                ]
            # Filtered search widens geometrically until k visible hits are found.
            limit = total if limit * 2 >= total // 4 else limit * 2



def get_vector_index(store: Store, *, dim: int = 384, prefer: str = "auto"):
    """Return a sqlite-vec index if available, else the NumPy reference index.

    prefer: "auto" (try sqlite-vec, fall back), "sqlite-vec" (require it),
            or "numpy" (force the reference index).
    """
    dimension = _validated_dimension(dim)
    if prefer not in {"auto", "sqlite-vec", "numpy"}:
        raise ValueError("prefer must be one of: auto, sqlite-vec, numpy")
    if prefer == "numpy":
        return NumpyVectorIndex(store, dim=dimension)
    try:
        return SqliteVecVectorIndex(store, dimension)
    except Exception:
        if prefer == "sqlite-vec":
            raise
        return NumpyVectorIndex(store, dim=dimension)
