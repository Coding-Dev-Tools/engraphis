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

from typing import Optional

import numpy as np

from engraphis.backends.vector_numpy import NumpyVectorIndex
from engraphis.core.interfaces import SearchFilter
from engraphis.core.store import Store, memory_matches_filter


def _visible(rec, flt: SearchFilter) -> bool:
    return memory_matches_filter(rec, flt)


def _cosine_from_l2(distance: float) -> float:
    """Convert Euclidean distance between unit vectors back to cosine similarity."""
    return max(-1.0, min(1.0, 1.0 - (float(distance) ** 2) / 2.0))


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

    def search(self, vec: np.ndarray, k: int,
               *, filter: Optional[SearchFilter] = None) -> list[tuple[str, float]]:
        if k <= 0:
            return []
        v = np.asarray(vec, dtype=np.float32)
        n = float(np.linalg.norm(v))
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
            # The KNN cap uses vec0's explicit ``k = ?`` constraint, NOT ``LIMIT ?``:
            # SQLite < 3.41 never passes a LIMIT down to a virtual table's xBestIndex,
            # so vec0 raises "A LIMIT or 'k = ?' constraint is required on vec0 knn
            # queries" — which the resolve path swallows, silently degrading every
            # near-duplicate write to ADD on those systems. ``k = ?`` is the syntax
            # sqlite-vec documents for exactly this reason and works on every
            # supported SQLite/sqlite-vec combination.
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
                out.append((row["id"], _cosine_from_l2(row["distance"])))
                if len(out) >= k:
                    return out
            if filter is None or len(rows) < limit or limit >= total:
                return out
            # Filtered search widens geometrically until k visible hits are found. Once
            # the next doubling would already cover a quarter of the index, jump straight
            # to a single full scan: on a workspace dense with invisible rows (expired/
            # out-of-scope), the geometric tail otherwise re-runs several near-full ANN
            # scans back to back for one query.
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
    if prefer == "numpy":
        return NumpyVectorIndex(store)
    try:
        return SqliteVecVectorIndex(store, dim)
    except Exception:
        if prefer == "sqlite-vec":
            raise
        return NumpyVectorIndex(store)
