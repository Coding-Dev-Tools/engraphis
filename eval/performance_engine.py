"""Serializable, local-only factory configuration for performance diagnostics.

The fixture benchmark keeps its historical constructor. This opt-in path builds a
fresh production engine in each worker and never opens an operator's existing DB.
"""
from __future__ import annotations

import hashlib
import json
import re
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Optional

from engraphis import factory
from engraphis.backends.embedder_st import _local_artifact_version
from engraphis.backends.model_source import is_local_model_source


_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_HUB_ID = re.compile(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)?\Z")


@dataclass(frozen=True)
class PerformanceEngineConfig:
    """Explicit factory backend choices; all optional models must already be local.

    Cached Hub selectors use ``local:org/model`` and a commit revision. Absolute
    local directories instead require the existing ``engraphis-local-artifact-v1``
    content digest; a claimed Hub revision alone cannot pin directory bytes.
    """

    storage: str = "disk"
    storage_root: Optional[str] = None
    vector_backend: str = "numpy"
    sqlite_durability: str = "durable"
    embed_model: Optional[str] = None
    embed_revision: Optional[str] = None
    embed_artifact_sha256: Optional[str] = None
    rerank_model: Optional[str] = None
    rerank_revision: Optional[str] = None
    rerank_artifact_sha256: Optional[str] = None

    @classmethod
    def from_dict(cls, value: object) -> PerformanceEngineConfig:
        if not isinstance(value, dict):
            raise ValueError("engine config must be a JSON object")
        unknown = set(value) - {field.name for field in fields(cls)}
        if unknown:
            raise ValueError("unknown engine config fields: " + ", ".join(sorted(unknown)))
        config = cls(**value)
        config.validate()
        return config

    def validate(self) -> None:
        if not isinstance(self.storage, str) or self.storage not in {"memory", "disk"}:
            raise ValueError("engine storage must be memory or disk")
        if not isinstance(self.vector_backend, str) or self.vector_backend not in {"numpy", "sqlite-vec"}:
            raise ValueError("engine vector_backend must explicitly select numpy or sqlite-vec")
        if not isinstance(self.sqlite_durability, str) or self.sqlite_durability not in {"durable", "balanced"}:
            raise ValueError("engine sqlite_durability must be durable or balanced")
        if self.storage_root is not None:
            if not isinstance(self.storage_root, str) or not self.storage_root.strip():
                raise ValueError("storage_root must be an existing absolute directory")
            root = Path(self.storage_root)
            if not root.is_absolute() or not root.is_dir():
                raise ValueError("storage_root must be an existing absolute directory")
            if self.storage != "disk":
                raise ValueError("storage_root requires disk storage")
        for role in ("embed", "rerank"):
            self._model_provenance(role)

    def _model_provenance(self, role: str, *, verify_bytes: bool = False) -> dict:
        model = getattr(self, f"{role}_model")
        revision = getattr(self, f"{role}_revision")
        digest = getattr(self, f"{role}_artifact_sha256")
        if model is None:
            if revision is not None or digest is not None:
                raise ValueError(f"{role} model is required with revision or artifact digest")
            return {"source": "deterministic" if role == "embed" else "identity"}
        if not isinstance(model, str) or not model.startswith("local:"):
            raise ValueError(f"{role}_model must use an explicit local: selector; downloads are disabled")
        source = model[len("local:"):]
        if not source or source != source.strip():
            raise ValueError(f"{role}_model local selector must not be empty or padded")
        if revision is not None and (
            not isinstance(revision, str) or _COMMIT.fullmatch(revision) is None
        ):
            raise ValueError(f"{role}_revision requires a lowercase 40-character commit")
        if is_local_model_source(source):
            path = Path(source)
            if not path.is_absolute() or not path.is_dir():
                raise ValueError(f"{role} local artifact must be an existing absolute directory")
            if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
                raise ValueError(f"{role}_artifact_sha256 must pin the local artifact bytes")
            if verify_bytes and _local_artifact_version(source) != f"local-content:{digest}":
                raise ValueError(f"{role} local artifact digest mismatch")
            return {
                "source": "local_artifact", "sha256": digest,
                "digest_method": "engraphis-local-artifact-v1", "revision": revision,
            }
        if _HUB_ID.fullmatch(source) is None:
            raise ValueError(f"{role}_model must name a local directory or cached Hub model")
        if not isinstance(revision, str) or _COMMIT.fullmatch(revision) is None:
            raise ValueError(f"{role}_revision must pin the cached model to a 40-character commit")
        if digest is not None:
            raise ValueError(f"{role}_artifact_sha256 is only supported for a local directory")
        return {"source": "cached_hub", "model": source, "revision": revision}

    def provenance(self, *, verify_bytes: bool = False) -> dict:
        self.validate()
        payload = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return {
            "mode": "factory", "storage": self.storage,
            "vector_backend": self.vector_backend,
            "sqlite_durability": self.sqlite_durability,
            "configuration_sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
            "embedder": self._model_provenance("embed", verify_bytes=verify_bytes),
            "reranker": self._model_provenance("rerank", verify_bytes=verify_bytes),
            "local_files_only": True, "require_exact_backends": True,
        }


class FactoryBenchmarkSession:
    def __init__(self, config: PerformanceEngineConfig, db_path: str, dim: int):
        self.config = config
        self.db_path = db_path
        self.dim = dim
        self.engine = None
        self.provenance = {}
        self.reopen_ms = None
        started = time.perf_counter_ns()
        self._open()
        self.startup_ms = (time.perf_counter_ns() - started) / 1_000_000

    def _open(self) -> None:
        provenance = self.config.provenance(verify_bytes=True)
        engine = factory.create_memory_engine(
            self.db_path,
            embed_dim=self.dim,
            embed_model=self.config.embed_model,
            embed_revision=self.config.embed_revision,
            rerank_model=self.config.rerank_model,
            rerank_revision=self.config.rerank_revision,
            vector_backend=self.config.vector_backend,
            sqlite_durability=self.config.sqlite_durability,
            require_immutable_models=True,
            require_exact_backends=True,
        )
        try:
            if self.config.embed_model is not None and not getattr(
                engine.embedder, "supports_semantic_search", False
            ):
                raise RuntimeError("configured semantic benchmark embedder resolved to a fallback")
            expected_index = {
                "numpy": "NumpyVectorIndex", "sqlite-vec": "SqliteVecVectorIndex",
            }[self.config.vector_backend]
            if type(engine.index).__name__ != expected_index:
                raise RuntimeError("configured benchmark vector backend resolved to a fallback")
            if self.config.rerank_model is not None and type(engine.reranker).__name__ == "IdentityReranker":
                raise RuntimeError("configured benchmark reranker resolved to a fallback")
            # The reranker does not otherwise fingerprint local artifacts. Verify
            # both directories again so a changing local snapshot cannot acquire a receipt.
            self.config.provenance(verify_bytes=True)
        except BaseException:
            engine.close()
            raise
        self.engine = engine
        self.provenance = provenance

    def reopen(self) -> None:
        """Measure reopening the populated DB, including model/index construction."""
        if self.config.storage != "disk":
            return
        self.engine.close()
        self.engine = None
        started = time.perf_counter_ns()
        self._open()
        self.reopen_ms = (time.perf_counter_ns() - started) / 1_000_000

    def close(self) -> None:
        if self.engine is not None:
            self.engine.close()


@contextmanager
def factory_benchmark_session(config: PerformanceEngineConfig, *, dim: int):
    config.validate()
    with tempfile.TemporaryDirectory(
        prefix="engraphis-performance-", dir=config.storage_root,
    ) as directory:
        db_path = str(Path(directory) / "corpus.db") if config.storage == "disk" else ":memory:"
        session = FactoryBenchmarkSession(config, db_path, dim)
        try:
            yield session
        finally:
            session.close()
