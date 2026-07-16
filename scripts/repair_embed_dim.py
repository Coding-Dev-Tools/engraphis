"""Repair mixed embedding dimensions in engraphis.db.

Root cause: the store accumulated memory vectors of two different dimensions
(384 from the canonical all-MiniLM-L6-v2 model, and 256 from an earlier
deterministic-fallback default). ``NumpyVectorIndex.search`` does
``np.vstack([...])`` over all scope-matching vectors, which raises
``ValueError: all the input array dimensions ... size 384 ... size 256`` and
breaks recall fleet-wide.

Fix: re-embed every non-384 vector at the canonical 384 dimension using the
offline deterministic embedder, restoring dimensional homogeneity so recall
works again. The backup is the caller's responsibility (cron archive).
"""
from __future__ import annotations

import sqlite3
import sys

from engraphis.backends.embedder_deterministic import DeterministicEmbedder

CANON_DIM = 384


def repair(db_path: str) -> dict:
    emb = DeterministicEmbedder(CANON_DIM)
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    # Memory text source: title + content (matches write-path embedding text).
    rows = cur.execute(
        "SELECT v.id AS id, m.title AS title, m.content AS content "
        "FROM mem_vectors v JOIN memories m ON m.id = v.id "
        "WHERE v.dim <> ?",
        (CANON_DIM,),
    ).fetchall()

    n = 0
    for r in rows:
        text = f"{r['title'] or ''}\n{r['content'] or ''}".strip()
        vec = emb.embed([text])[0]
        norm = float(__import__("numpy").linalg.norm(vec))
        if norm > 0:
            vec = vec / norm
        cur.execute(
            "UPDATE mem_vectors SET dim=?, vector=?, model='' WHERE id=?",
            (CANON_DIM, vec.tobytes(), r["id"]),
        )
        n += 1

    con.commit()

    by_dim = {}
    for dim, in cur.execute("SELECT dim FROM mem_vectors"):
        by_dim[dim] = by_dim.get(dim, 0) + 1
    con.close()
    return {"repaired": n, "by_dim": by_dim}


if __name__ == "__main__":
    import os

    p = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "engraphis.db")
    print("repairing:", p)
    print(repair(p))
