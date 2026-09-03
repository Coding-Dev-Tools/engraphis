"""Outer composition root for the v2 memory engine.

Concrete backend selection belongs here, outside ``engraphis.core``.  The core engine
accepts only injected collaborators and keeps ``MemoryEngine.create`` as a compatibility
entry point that delegates to this provider.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

from engraphis.backends.codegraph import (
    SourceWalkLimitExceeded,
    detect_lang,
    get_code_indexer,
    iter_source_files,
    source_path_allowed,
)
from engraphis.backends.embedder_st import get_embedder
from engraphis.backends.extractor import PassthroughExtractor, get_extractor
from engraphis.backends.graph_extractor import (
    StructuredMetadataGraphExtractor,
    feed as feed_graph,
    get_graph_extractor,
)
from engraphis.backends.reranker import get_reranker
from engraphis.backends.retention import get_retention_supervisor
from engraphis.backends.vector_sqlitevec import get_vector_index
from engraphis.config import resolve_vector_backend
from engraphis.core.interfaces import GraphTraversalPolicy, QueryPlanner
from engraphis.core.store import Store

_logger = logging.getLogger("engraphis.factory")


def _is_prod_env() -> bool:
    """Return True when the process runs in the production deployment environment."""
    return os.environ.get("ENGRAPHIS_ENV", "").strip().lower() == "prod"


def _backend_identity(backend: Any, *, configured: str = "") -> dict:
    """Return the configured vs resolved identity for one constructed backend."""
    resolved = type(backend).__name__ if backend is not None else "none"
    identity = str(getattr(backend, "embedding_identity", "") or "") if backend is not None else ""
    info: dict = {"configured": configured, "resolved": resolved}
    if identity:
        info["identity"] = identity
    return info


def backend_health(engine: Any = None, *, vector_backend: str = "numpy") -> dict:
    """Return the resolved backend identities served by one engine (or selector)."""
    if engine is None:
        return {
            "vector_backend": {
                "configured": vector_backend,
                "resolved": resolve_vector_backend(vector_backend),
            },
        }
    index = getattr(engine, "index", None)
    return {
        "vector_backend": _backend_identity(
            index, configured=str(getattr(engine, "vector_backend", vector_backend) or vector_backend),
        ),
        "embedder": _backend_identity(getattr(engine, "embedder", None)),
        "reranker": _backend_identity(getattr(engine, "reranker", None)),
        "extractor": _backend_identity(getattr(engine, "extractor", None)),
    }


def _feed_graph(
    store,
    content: str,
    *,
    workspace_id: str,
    repo_id=None,
    title: str = "",
    extractor=None,
    structured_metadata=None,
    provenance=None,
    valid_from=None,
    ingested_at=None,
):
    selected = (
        StructuredMetadataGraphExtractor(structured_metadata)
        if structured_metadata is not None
        else extractor
    )
    return feed_graph(
        store,
        content,
        workspace_id=workspace_id,
        repo_id=repo_id,
        title=title,
        extractor=selected,
        provenance=provenance,
        valid_from=valid_from,
        ingested_at=ingested_at,
    )


def _close_quietly(resource) -> None:
    close = getattr(resource, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


def create_memory_engine(
    db_path: str = ":memory:",
    *,
    engine_cls=None,
    embed_model: Optional[str] = None,
    embed_revision: Optional[str] = None,
    require_immutable_models: Optional[bool] = None,
    embed_dim: int = 384,
    vector_backend: str = "numpy",
    rerank_model: Optional[str] = None,
    rerank_revision: Optional[str] = None,
    extractor: str = "none",
    graph_extractor: str = "none",
    retention_supervisor: str = "none",
    allow_automatic_critical_retention: bool = False,
    auto_evolve: bool = True,
    connect=None,
    graph_traversal_policy: Optional[GraphTraversalPolicy] = None,
    query_planner: Optional[QueryPlanner] = None,
    read_only: bool = False,
    require_exact_backends: bool = False,
):
    """Construct a ``MemoryEngine`` and transfer ownership of all resources to it.

    Args:
        require_exact_backends: When True, raise an error if any configured backend
            is unavailable instead of falling back to degraded alternatives. Use this
            for production deployments where silent degradation is unacceptable.
            ``ENGRAPHIS_ENV=prod`` forces this on regardless of the argument.
    """
    if _is_prod_env():
        require_exact_backends = True
    # In exact mode "auto" must not silently degrade to the portable reference:
    # require the native backend so a missing extension fails closed instead.
    effective_vector_backend = (
        "sqlite-vec"
        if (require_exact_backends and (vector_backend or "").strip().lower() == "auto")
        else vector_backend
    )
    if engine_cls is None:
        from engraphis.core.engine import MemoryEngine

        engine_cls = MemoryEngine

    store = Store(db_path, connect=connect, read_only=read_only)
    owned = []
    try:
        embedder = get_embedder(
            embed_model,
            embed_dim,
            revision=embed_revision,
            require_immutable_models=require_immutable_models,
            require_exact=require_exact_backends,
        )
        owned.append(embedder)

        index = get_vector_index(
            store, dim=embedder.dim, prefer=effective_vector_backend,
        )
        owned.append(index)

        reranker = get_reranker(
            rerank_model,
            revision=rerank_revision,
            require_immutable_models=require_immutable_models,
            require_exact=require_exact_backends,
        )
        owned.append(reranker)
        extracted = get_extractor(
            extractor,
            require_immutable_models=require_immutable_models,
            require_exact=require_exact_backends,
        )
        owned.append(extracted)
        if (
            isinstance(extracted, PassthroughExtractor)
            and not getattr(extracted, "fallback_from", None)
        ):
            extracted = None
        graph = (
            get_graph_extractor(graph_extractor, require_exact=require_exact_backends)
            if graph_extractor and graph_extractor != "none"
            else None
        )
        if graph is not None:
            owned.append(graph)
        supervisor = get_retention_supervisor(
            retention_supervisor, require_exact=require_exact_backends,
        )
        if supervisor is not None:
            owned.append(supervisor)

        engine = engine_cls(
            store,
            embedder,
            index,
            reranker,
            auto_evolve=auto_evolve,
            extractor=extracted,
            graph_extractor=graph,
            graph_feeder=_feed_graph,
            retention_supervisor=supervisor,
            allow_automatic_critical_retention=allow_automatic_critical_retention,
            graph_traversal_policy=graph_traversal_policy,
            query_planner=query_planner,
            code_indexer_factory=get_code_indexer,
            code_language_detector=detect_lang,
            code_source_iterator=iter_source_files,
            code_source_policy=source_path_allowed,
            code_walk_limit_error=SourceWalkLimitExceeded,
        )
        if read_only:
            active_space = store.active_embedding_space()
            if active_space and (
                not engine.embedding_space
                or not store.embedding_space_ready(engine.embedding_space)
            ):
                raise RuntimeError(
                    "read-only embedding space is unavailable or stale; open the database "
                    "writable once with the matching embedder to complete its rebuild "
                    f"(active={active_space!r}, configured={engine.embedding_space!r})"
                )
        else:
            engine._rebuild_versioned_embeddings()
        engine.vector_backend = effective_vector_backend
        engine.backend_identities = backend_health(
            engine, vector_backend=effective_vector_backend,
        )
        engine._adopt_resources([store, *owned])
        return engine
    except BaseException:
        seen = set()
        for resource in reversed(owned):
            identity = id(resource)
            if identity in seen:
                continue
            seen.add(identity)
            _close_quietly(resource)
        _close_quietly(store)
        raise
