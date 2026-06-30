"""sqlite-vec ANN backend + factory (MASTER_PLAN.md §6.2).

Replaces the O(n) NumPy reference with an embedded ANN index that lives in the
same SQLite file — preserving the local-first, single-file story. If the
``sqlite-vec`` extension is not installable in the current environment, the
factory transparently falls back to ``NumpyVectorIndex`` (so nothing breaks),
which is exactly what happens in restricted CI sandboxes.

Note: scope/bi-temporal filtering is enforced by the recall engine's visibility
check after candidate generation, so this index over-fetches ``k`` and lets the
engine filter — keeping the ANN path simple.
"""
from __future__ import annotations

from typing import Optional

import numpy as np

from engraphis.backends.vector_numpy import NumpyVectorIndex
from engraphis.core.interfaces import SearchFilter
from engraphis.core.store import Store


class SqliteVecVectorIndex:
    """ANN over embeddings using the sqlite-vec extension."""

    def __init__(self, store: Store, dim: int) -> None:
        import sqlite_vec  # lazy: optional dependency / native extension
        self.store = store
        self.dim = dim
        conn = store.conn
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS mem_vec_ann USING vec0("
            f"id TEXT PRIMARY KEY, embedding FLOAT[{dim}])"
        )
        conn.commit()

    def upsert(self, ids: list[str], vecs: np.ndarray, meta: Optional[list[dict]] = None) -> None:
        vecs = np.asarray(vecs, dtype=np.float32)
        for i, mid in enumerate(ids):
            v = vecs[i]
            n = float(np.linalg.norm(v))
            if n > 0:
                v = v / n
            self.store.conn.execute(
                "INSERT OR REPLACE INTO mem_vec_ann(id, embedding) VALUES (?, ?)",
                (mid, v.tobytes()),
            )
        self.store.conn.commit()

    def search(self, vec: np.ndarray, k: int, *, filter: Optional[SearchFilter] = None) -> list[tuple[str, float]]:
        v = np.asarray(vec, dtype=np.float32)
        n = float(np.linalg.norm(v))
        if n > 0:
            v = v / n
        rows = self.store.conn.execute(
            "SELECT id, distance FROM mem_vec_ann WHERE embedding MATCH ? "
            "ORDER BY distance LIMIT ?",
            (v.tobytes(), k),
        ).fetchall()
        # vec0 returns L2 distance for normalized vectors → convert to a similarity.
        return [(r["id"], 1.0 - (r["distance"] / 2.0)) for r in rows]

    def delete(self, ids: list[str]) -> None:
        if not ids:
            return
        marks = ",".join("?" for _ in ids)
        self.store.conn.execute(f"DELETE FROM mem_vec_ann WHERE id IN ({marks})", ids)
        self.store.conn.commit()


def get_vector_index(store: Store, *, dim: int = 256, prefer: str = "auto"):
    """Return a sqlite-vec index if available, else the NumPy reference index.

    prefer: "auto" (try sqlite-vec, fall back), "sqlite-vec" (require it),
            or "numpy" (force the reference index).
    """
    if prefer == "numpy":
        return NumpyVectorIndex(store)
    try:
        return SqliteVecVectorIndex(store, dim)
    except Exception:
        if prefer == "sqlite-vec":
            raise
        return NumpyVectorIndex(store)
