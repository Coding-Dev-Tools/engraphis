"""Non-secret artifact and capability identity for local diagnostics."""
from __future__ import annotations

from functools import lru_cache
import hashlib
from pathlib import Path

from engraphis import __version__
from engraphis.core.schema import SCHEMA_VERSION


@lru_cache(maxsize=1)
def package_build_info() -> dict:
    root = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix in {".py", ".js", ".css", ".html", ".json"}:
            digest.update(path.relative_to(root).as_posix().encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
    return {
        "schema": "engraphis-build/v1", "version": __version__,
        "schema_version": SCHEMA_VERSION,
        "package_source_sha256": digest.hexdigest(),
        "identity_boundary": "installed package files at first build-info request",
        "contracts": {"revision": "memory-command/v1", "history": "history/1",
                      "listing": "cursor/2", "diagnostics": "diagnostics/1",
                      "mcp": "engraphis-mcp-contract/v1"},
    }


def build_info(service) -> dict:
    from engraphis.core.store import _is_memory_database_path
    store = service.store
    independent = not store.read_only and not _is_memory_database_path(store.path) and (
        store._connect is None or callable(getattr(store._connect, "open_read_snapshot", None)))
    return {**package_build_info(), "database_schema_version": service.store.schema_version,
            "readers": {"independent_browsing": independent,
                        "capacity": store._read_snapshot_pool.limit if independent else 1,
                        "default_deadline_seconds": 5 if independent else None},
            "backends": {"embedder": type(service.engine.embedder).__name__,
                         "vector_index": type(service.engine.index).__name__}}
