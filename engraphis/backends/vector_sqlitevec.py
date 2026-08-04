"""sqlite-vec ANN backend + factory.

Replaces the O(n) NumPy reference with an embedded ANN index that lives in the
same SQLite file — preserving the local-first, single-file story. If the
``sqlite-vec`` extension is not installable in the current environment, the
factory transparently falls back to ``NumpyVectorIndex`` (so nothing breaks),
which is exactly what happens in restricted CI sandboxes.

Note: sqlite-vec cannot apply Engraphis' bi-temporal/workspace filter inside the vec0
MATCH directly, so ``search`` expands the ANN window until it has enough visible hits.
"""
from __future__ import annotations

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
            f"vector dimension {actual} does not match the ANN index dimension {dim}"
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
            f"query dimension {actual} does not match the ANN index dimension {dim}"
        )
    if not np.isfinite(values).all():
        raise ValueError("query vector must contain only finite values")
    return values


class SqliteVecVectorIndex:
    """ANN over embeddings using the sqlite-vec extension."""

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
        import sqlite_vec  # lazy: optional dependency / native extension
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
                    f"existing ANN index dimension {match.group(1)} does not match "
                    f"requested dimension {dimension}"
                )
        conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS mem_vec_ann USING vec0("
            f"id TEXT PRIMARY KEY, embedding FLOAT[{dimension}])"
        )
        conn.commit()

    def upsert(self, ids: list[str], vecs: np.ndarray, meta: Optional[list[dict]] = None) -> None:
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
        if not count:
            return
        for i, mid in enumerate(ids):
            v = values[i]
            with np.errstate(over="ignore", invalid="ignore"):
                n = float(np.linalg.norm(v))
            if not np.isfinite(n):
                raise ValueError("vector norm must be finite")
            if n > 0:
                v = v / n
            self.store.conn.execute(
                "INSERT OR REPLACE INTO mem_vec_ann(id, embedding) VALUES (?, ?)",
                (mid, v.tobytes()),
            )
        self.store.conn.commit()

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
        total = k
        if filter is not None:
            total = int(self.store.conn.execute(
                "SELECT COUNT(*) AS n FROM mem_vec_ann").fetchone()["n"])
            if total == 0:
                return []
        limit = min(k, total)
        while True:
            # The KNN cap uses vec0's explicit `k = ?` constraint, NOT `LIMIT ?`.
            rows = self.store.conn.execute(
                "SELECT id, distance FROM mem_vec_ann WHERE embedding MATCH ? "
                "AND k = ? ORDER BY distance",
                (v.tobytes(), int(limit)),
            ).fetchall()
            out: list[tuple[str, float]] = []
            for row in rows:
                if filter is not None:
                    rec = self.store.get_memory(row["id"])
                    if rec is None or not _visible(rec, filter):
                        continue
                # A zero query has no direction; retain the NumPy backend's
                # deterministic zero similarity rather than converting its
                # distance to the mathematically unrelated 0.5.
                score = 0.0 if n == 0 else _cosine_from_l2(row["distance"])
                out.append((row["id"], score))
                if len(out) >= k:
                    return out
            if filter is None or len(rows) < limit or limit >= total:
                return out
            # Filtered search widens geometrically until k visible hits are found.
            limit = total if limit * 2 >= total // 4 else limit * 2

    def delete(self, ids: list[str]) -> None:
        if not ids:
            return
        marks = ",".join("?" for _ in ids)
        self.store.conn.execute(f"DELETE FROM mem_vec_ann WHERE id IN ({marks})", ids)
        self.store.conn.commit()


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
