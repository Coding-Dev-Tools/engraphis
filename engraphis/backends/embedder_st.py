"""Real embedding model adapter + factory.

Wraps a sentence-transformers model (BGE-M3, Qwen3-Embedding, E5, MiniLM, …)
behind the ``Embedder`` interface. ``get_embedder`` returns a real model when one
is configured and importable, and otherwise falls back to the dependency-free
``DeterministicEmbedder`` so the system always runs (offline, CI).

``local:<path>`` is an explicit local-only selector.  It asks sentence-transformers
to load only files already present at ``<path>`` or in its local cache.  Engraphis
does not ship a model in this package, so a missing local model degrades to lexical
hashing and reports that fact through the normal embedder capability response.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from numbers import Integral
from pathlib import Path
from typing import Any, Literal, Optional

import numpy as np

from engraphis.backends.embedder_deterministic import DeterministicEmbedder
from engraphis.backends.model_source import is_local_model_source, validate_model_source
from engraphis.core.interfaces import Embedder


LOCAL_MODEL_PREFIX = "local:"


_IMMUTABLE_COMMIT = re.compile(r"[0-9a-f]{40}\Z")


_LOCAL_ARTIFACT_HASH_CHUNK = 1_048_576


def _stat_signature(info: os.stat_result) -> tuple[int, ...]:
    identity = (
        int(info.st_dev),
        int(info.st_ino),
        int(info.st_size),
        int(info.st_mtime_ns),
    )
    # Windows exposes creation time as st_ctime and can update its reported
    # precision when a descriptor is opened. POSIX st_ctime is a useful mutation
    # signal, so retain it only where path-stat and descriptor-stat are stable.
    return identity if os.name == "nt" else identity + (int(info.st_ctime_ns),)


def _local_artifact_inventory(model_name: str):
    source = Path(os.path.expanduser(model_name))
    if not source.exists():
        return None
    root = source.resolve(strict=True)
    single_file = root.is_file()
    files = [root] if single_file else sorted(
        (path for path in root.rglob("*") if path.is_file()),
        key=lambda path: path.relative_to(root).as_posix(),
    )
    state = tuple(
        (
            path.name if single_file else path.relative_to(root).as_posix(),
            *_stat_signature(path.stat()),
        )
        for path in files
    )
    return root, files, state


def _local_artifact_state(model_name: str):
    try:
        inventory = _local_artifact_inventory(model_name)
    except OSError:
        raise RuntimeError("local model artifacts could not be inspected") from None
    return inventory[2] if inventory is not None else None


def _local_artifact_version(
    model_name: str,
    *,
    expected_state=None,
    verify_expected: bool = False,
) -> str:
    """Hash local artifact bytes once, while proving the manifest stayed stable."""
    try:
        inventory = _local_artifact_inventory(model_name)
        state = inventory[2] if inventory is not None else None
        if verify_expected and state != expected_state:
            raise RuntimeError("local model artifacts changed while the model was loading")
        if inventory is None:
            return ""
        _, files, state = inventory
        digest = hashlib.sha256(b"engraphis-local-artifact-v1\0")
        for path, expected in zip(files, state):
            relative, *expected_signature = expected
            digest.update(relative.encode("utf-8", errors="surrogatepass"))
            digest.update(b"\0")
            digest.update(str(expected_signature[2]).encode("ascii"))
            digest.update(b"\0")
            with path.open("rb") as stream:
                if _stat_signature(os.fstat(stream.fileno())) != tuple(expected_signature):
                    raise RuntimeError(
                        "local model artifacts changed while they were fingerprinted"
                    )
                while True:
                    chunk = stream.read(_LOCAL_ARTIFACT_HASH_CHUNK)
                    if not chunk:
                        break
                    digest.update(chunk)
                if _stat_signature(os.fstat(stream.fileno())) != tuple(expected_signature):
                    raise RuntimeError(
                        "local model artifacts changed while they were fingerprinted"
                    )
        after = _local_artifact_inventory(model_name)
        if after is None or after[2] != state:
            raise RuntimeError("local model artifacts changed while they were fingerprinted")
        return "local-content:" + digest.hexdigest()
    except RuntimeError:
        raise
    except OSError:
        raise RuntimeError("local model artifacts could not be fingerprinted") from None


def _loaded_commit(model: object) -> str:
    """Return the immutable Hub commit recorded by sentence-transformers/transformers."""
    first = None
    first_module = getattr(model, "_first_module", None)
    if callable(first_module):
        try:
            first = first_module()
        except Exception:
            first = None
    auto_model = getattr(first, "auto_model", None)
    candidates = (
        model,
        getattr(model, "_model_card_vars", None),
        first,
        auto_model,
        getattr(auto_model, "config", None),
    )
    for candidate in candidates:
        if isinstance(candidate, dict):
            values = (
                candidate.get("_commit_hash"),
                candidate.get("commit_hash"),
                candidate.get("revision"),
            )
        else:
            values = (
                getattr(candidate, "_commit_hash", None),
                getattr(candidate, "commit_hash", None),
                getattr(candidate, "revision", None),
            )
        for value in values:
            normalized = str(value or "").strip().lower()
            if _IMMUTABLE_COMMIT.fullmatch(normalized):
                return normalized
    return ""


class SentenceTransformerEmbedder:
    supports_semantic_search = True
    embedding_mode = "semantic"
    embedding_identity = "sentence_transformers"

    def __init__(
        self,
        model_name: str,
        *,
        revision: Optional[str] = None,
        local_files_only: bool = False,
        require_immutable_models: Optional[bool] = None,
    ) -> None:
        validation_source = (
            f"{LOCAL_MODEL_PREFIX}{model_name}"
            if local_files_only and not is_local_model_source(model_name)
            else model_name
        )
        validate_model_source(
            validation_source,
            revision,
            require_immutable_models=require_immutable_models,
            loader="sentence-transformers model",
        )
        local_source = is_local_model_source(model_name)
        local_before = _local_artifact_state(model_name) if local_source else None
        from sentence_transformers import SentenceTransformer  # pyright: ignore[reportMissingImports]  # lazy: optional dependency
        kwargs: dict[str, Any] = {"trust_remote_code": False}
        if revision:
            kwargs["revision"] = revision
        if local_files_only:
            # This avoids a Hub request when an operator explicitly selected the
            # local mode.  It still supports both a local model directory and an
            # already-populated sentence-transformers cache.
            kwargs["local_files_only"] = True
        # Keep declared model provenance beside the loaded object.  Benchmark
        # artifacts must be able to distinguish a pinned model from a mutable
        # fallback without inspecting implementation-specific internals.
        self.model_name = model_name
        self.revision = revision
        self.local_files_only = local_files_only
        self.model = SentenceTransformer(model_name, **kwargs)
        local_after = (
            _local_artifact_version(
                model_name,
                expected_state=local_before,
                verify_expected=True,
            )
            if local_source
            else ""
        )
        resolved_commit = _loaded_commit(self.model)
        declared_commit = str(revision or "").strip().lower()
        if not resolved_commit and _IMMUTABLE_COMMIT.fullmatch(declared_commit):
            resolved_commit = declared_commit
        self._artifact_version = (
            local_after or (f"hf-commit:{resolved_commit}" if resolved_commit else "")
        )
        dimension = self.model.get_embedding_dimension()
        if (
            isinstance(dimension, bool)
            or not isinstance(dimension, Integral)
            or int(dimension) <= 0
        ):
            raise ValueError(
                "sentence-transformers model did not report a positive embedding dimension"
            )
        self._dim = int(dimension)

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def embedding_version(self) -> str:
        """Identify the loaded artifact space without exposing model paths."""
        artifact_version = str(getattr(self, "_artifact_version", "") or "").strip()
        if not artifact_version:
            return ""
        configured = f"v2\0{self.model_name}\0{artifact_version}"
        digest = hashlib.sha256(configured.encode("utf-8")).hexdigest()[:24]
        return f"st:{digest}"

    def embed(self, texts: list[str], *, kind: Literal["text", "code"] = "text") -> np.ndarray:
        if not texts:
            return np.empty((0, self._dim), dtype=np.float32)
        try:
            vecs = self.model.encode(texts, normalize_embeddings=True, convert_to_numpy=True)
            result = np.asarray(vecs, dtype=np.float32)
        except (TypeError, ValueError, OverflowError, RuntimeError):  # noqa: BLE001
            raise RuntimeError("sentence-transformers returned malformed embeddings") from None
        if result.ndim == 1 and len(texts) == 1:
            result = result.reshape(1, -1)
        if result.shape != (len(texts), self._dim) or not np.isfinite(result).all():
            raise RuntimeError("sentence-transformers returned malformed embeddings")
        with np.errstate(over="ignore", invalid="ignore"):
            norms = np.linalg.norm(result, axis=1, keepdims=True)
        if not np.isfinite(norms).all():
            raise RuntimeError("sentence-transformers returned malformed embeddings")
        result = result / np.where(norms == 0, 1.0, norms)
        return result


#: Why the real embedder last failed to load ("" when it loaded fine). The dashboard
#: surfaces this so a user can see and fix a broken semantic-search setup.
LAST_EMBEDDER_ERROR = ""


def get_embedder(
    model_name: Optional[str] = None,
    dim: int = 256,
    *,
    revision: Optional[str] = None,
    require_immutable_models: Optional[bool] = None,
) -> Embedder:
    """Return a semantic model when available, else explicit lexical degradation.

    Prefix a configured model with ``local:`` to require a local path or cached
    model.  That mode never asks sentence-transformers to download the model.  It
    is deliberately opt-in because a regular model identifier retains the existing
    behavior for operators who want sentence-transformers to resolve it normally.
    """
    global LAST_EMBEDDER_ERROR
    if model_name:
        raw_model_name = str(model_name).strip()
        validate_model_source(
            raw_model_name,
            revision,
            require_immutable_models=require_immutable_models,
            loader="sentence-transformers model",
        )
        has_local_prefix = raw_model_name.startswith(LOCAL_MODEL_PREFIX)
        local_files_only = has_local_prefix or is_local_model_source(raw_model_name)
        resolved_model_name = (
            raw_model_name[len(LOCAL_MODEL_PREFIX):].strip()
            if has_local_prefix
            else raw_model_name
        )
        try:
            if not resolved_model_name:
                raise ValueError("local embedder selector requires a path or cached model name")
            factory_kwargs: dict[str, Any] = {
                "revision": revision,
                "require_immutable_models": require_immutable_models,
            }
            if local_files_only:
                factory_kwargs["local_files_only"] = True
            emb = SentenceTransformerEmbedder(resolved_model_name, **factory_kwargs)
            LAST_EMBEDDER_ERROR = ""
            return emb
        except Exception as exc:  # noqa: BLE001 - optional dep; record why we fall back
            # Provider and local-loader exception text can contain credentials, signed
            # URLs, or filesystem paths. Keep only the exception class in diagnostics.
            error_kind = type(exc).__name__
            LAST_EMBEDDER_ERROR = error_kind
            log = logging.getLogger("engraphis")
            emit = log.info if isinstance(exc, ModuleNotFoundError) else log.warning
            emit(
                "Configured semantic embedder unavailable (%s); using the %d-dim deterministic "
                "embedder; semantic recall/why/timeline will not match stored vectors.",
                error_kind, dim)
            source = "requested local semantic model" if local_files_only else "requested semantic model"
            return DeterministicEmbedder(
                dim,
                semantic_support_reason=(
                    f"{source} is unavailable; deterministic feature hashing captures "
                    "lexical overlap only, so semantic vector retrieval and semantic "
                    "grounding are disabled"
                ),
            )
    return DeterministicEmbedder(dim)
