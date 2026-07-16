"""Re-embed vectors whose dimensions differ from the configured embedder."""
from __future__ import annotations

import argparse
import sqlite3
import time
from pathlib import Path
from typing import Optional

from engraphis.backends.embedder_deterministic import DeterministicEmbedder
from engraphis.backends.embedder_st import get_embedder
from engraphis.config import settings


def repair(db_path: str, *, model_name: Optional[str] = None,
           dim: Optional[int] = None, backup: bool = True) -> dict:
    """Re-embed dimension-mismatched rows into the active model's vector space."""
    configured_model = settings.embed_model if model_name is None else model_name
    embedder = get_embedder(configured_model or None, dim or settings.embed_dim or 384)
    if configured_model and isinstance(embedder, DeterministicEmbedder):
        raise RuntimeError(
            "configured embedder %r is unavailable; install its dependency before repair"
            % configured_model)

    path = Path(db_path).expanduser().resolve()
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    backup_path = None
    try:
        target_dim = int(embedder.dim)
        rows = conn.execute(
            "SELECT v.id, m.title, m.content "
            "FROM mem_vectors v JOIN memories m ON m.id=v.id "
            "WHERE v.dim!=? ORDER BY v.id",
            (target_dim,),
        ).fetchall()
        if rows and backup:
            stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
            backup_path = path.with_name("%s.embed-repair-%s.bak" % (path.name, stamp))
            backup_conn = sqlite3.connect(str(backup_path))
            try:
                conn.backup(backup_conn)
            finally:
                backup_conn.close()

        model = configured_model or "deterministic"
        with conn:
            for start in range(0, len(rows), 128):
                batch = rows[start:start + 128]
                texts = [
                    ("%s\n%s" % (row["title"] or "", row["content"] or "")).strip()
                    for row in batch
                ]
                vectors = embedder.embed(texts)
                conn.executemany(
                    "UPDATE mem_vectors SET dim=?, vector=?, model=? WHERE id=?",
                    [(target_dim, vector.tobytes(), model, row["id"])
                     for row, vector in zip(batch, vectors)],
                )

        by_dim = {
            int(row[0]): int(row[1])
            for row in conn.execute(
                "SELECT dim, COUNT(*) FROM mem_vectors GROUP BY dim").fetchall()
        }
        return {
            "repaired": len(rows),
            "target_dim": target_dim,
            "by_dim": by_dim,
            "backup": str(backup_path) if backup_path else None,
        }
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Repair vectors whose dimensions differ from the active embedder")
    parser.add_argument(
        "db_path", nargs="?",
        default=str(Path(__file__).resolve().parents[1] / "engraphis.db"))
    parser.add_argument("--model", default=None, help="override ENGRAPHIS_EMBED_MODEL")
    parser.add_argument("--dim", type=int, default=None,
                        help="fallback dimension when no model is configured")
    parser.add_argument("--no-backup", action="store_true")
    args = parser.parse_args()
    print(repair(args.db_path, model_name=args.model, dim=args.dim,
                 backup=not args.no_backup))


if __name__ == "__main__":
    main()
