"""Rebuild an existing database's vectors through the governed engine lifecycle."""
from __future__ import annotations

import argparse
import os
import sqlite3
import time
from pathlib import Path
from typing import Optional

from engraphis.backends.embedder_deterministic import DeterministicEmbedder
from engraphis.backends.embedder_st import get_embedder
from engraphis.config import settings
from engraphis.core.engine import MemoryEngine
from engraphis.core.interfaces import embedding_space_fingerprint


def _connect_existing(db_path: str) -> sqlite3.Connection:
    """Open one existing SQLite file read/write without create-if-missing semantics."""
    path = Path(db_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"embedding repair database is not a file: {path}")
    connection = sqlite3.connect(
        f"{path.as_uri()}?mode=rw",
        uri=True,
        timeout=30,
    )
    connection.row_factory = sqlite3.Row
    return connection


def _backup_database(source: sqlite3.Connection, path: Path) -> Path:
    """Take one SQLite-consistent backup beside the database."""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    backup_path = path.with_name(f"{path.name}.embed-repair-{stamp}.bak")
    descriptor = os.open(
        str(backup_path),
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    os.close(descriptor)
    backup_connection: Optional[sqlite3.Connection] = None
    try:
        backup_connection = sqlite3.connect(str(backup_path))
        source.backup(backup_connection)
    except BaseException:
        if backup_connection is not None:
            backup_connection.close()
        backup_path.unlink(missing_ok=True)
        raise
    backup_connection.close()
    return backup_path


def repair(db_path: str, *, model_name: Optional[str] = None,
           dim: Optional[int] = None, backup: bool = True) -> dict:
    """Rebuild all vectors into one active fingerprint and synchronize every backend."""
    path = Path(db_path).expanduser().resolve()
    configured_model = settings.embed_model if model_name is None else model_name
    embedder = get_embedder(
        configured_model or None,
        dim or settings.embed_dim or 384,
        revision=getattr(settings, "embed_revision", "") or None,
        require_immutable_models=bool(getattr(settings, "require_immutable_models", False)),
    )
    if configured_model and isinstance(embedder, DeterministicEmbedder):
        raise RuntimeError(
            "configured embedder %r is unavailable; install its dependency before repair"
            % configured_model)

    fingerprint = embedding_space_fingerprint(embedder)
    connection = _connect_existing(str(path))
    try:
        total_row = connection.execute(
            "SELECT COUNT(*) AS n FROM mem_vectors"
        ).fetchone()
        stale_row = connection.execute(
            "SELECT COUNT(*) AS n FROM mem_vectors "
            "WHERE model IS NULL OR model!=? OR dim!=?",
            (fingerprint, int(embedder.dim)),
        ).fetchone()
        total = int(total_row["n"]) if total_row is not None else 0
        stale = int(stale_row["n"]) if stale_row is not None else 0
        state = connection.execute(
            "SELECT version FROM embedding_state WHERE identity='__active__'"
        ).fetchone()
        rebuilding = connection.execute(
            "SELECT version FROM embedding_state WHERE identity='__rebuilding__'"
        ).fetchone()
        needs_rebuild = bool(
            stale
            or total == 0
            or state is None
            or str(state["version"]) != fingerprint
            or rebuilding is not None
        )
        backup_path = _backup_database(connection, path) if needs_rebuild and backup else None
    finally:
        connection.close()

    engine = MemoryEngine.create(
        str(path),
        embed_model=configured_model or "",
        embed_dim=int(embedder.dim),
        embed_revision=getattr(settings, "embed_revision", "") or "",
        vector_backend=getattr(settings, "vector_backend", "numpy"),
        require_immutable_models=bool(
            getattr(settings, "require_immutable_models", False)
        ),
    )
    try:
        health = engine.store.embedding_space_health(fingerprint)
    finally:
        close_index = getattr(engine.index, "close", None)
        try:
            if callable(close_index):
                close_index()
        finally:
            engine.store.close()

    if not bool(health.get("ready")):
        raise RuntimeError(
            "embedding repair did not converge on one active vector-space fingerprint"
        )
    return {
        "repaired": stale,
        "target_dim": int(embedder.dim),
        "fingerprint": fingerprint,
        "by_dim": {int(embedder.dim): int(health["vectors"])},
        "backup": str(backup_path) if backup_path else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Repair vectors whose dimensions differ from the active embedder")
    parser.add_argument(
        "db_path", nargs="?", default=str(settings.db_path),
        help="existing v2 database (default: ENGRAPHIS_DB_PATH)")
    parser.add_argument("--model", default=None, help="override ENGRAPHIS_EMBED_MODEL")
    parser.add_argument("--dim", type=int, default=None,
                        help="fallback dimension when no model is configured")
    parser.add_argument("--no-backup", action="store_true")
    args = parser.parse_args()
    print(repair(args.db_path, model_name=args.model, dim=args.dim,
                 backup=not args.no_backup))


if __name__ == "__main__":
    main()
