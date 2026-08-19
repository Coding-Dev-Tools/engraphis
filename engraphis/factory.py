"""Outer composition root for the v2 memory engine.

Concrete backend selection belongs here, outside ``engraphis.core``.  The core engine
accepts only injected collaborators and keeps ``MemoryEngine.create`` as a compatibility
entry point that delegates to this provider.
"""
from __future__ import annotations

from typing import Optional

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
from engraphis.core.interfaces import GraphTraversalPolicy, QueryPlanner
from engraphis.core.store import Store


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
):
    """Construct a ``MemoryEngine`` and transfer ownership of all resources to it."""
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
        )
        owned.append(embedder)

        index = get_vector_index(
            store, dim=embedder.dim, prefer=vector_backend,
        )
        owned.append(index)

        reranker = get_reranker(
            rerank_model,
            revision=rerank_revision,
            require_immutable_models=require_immutable_models,
        )
        owned.append(reranker)
        extracted = get_extractor(
            extractor,
            require_immutable_models=require_immutable_models,
        )
        owned.append(extracted)
        if (
            isinstance(extracted, PassthroughExtractor)
            and not getattr(extracted, "fallback_from", None)
        ):
            extracted = None
        graph = (
            get_graph_extractor(graph_extractor)
            if graph_extractor and graph_extractor != "none"
            else None
        )
        if graph is not None:
            owned.append(graph)
        supervisor = get_retention_supervisor(retention_supervisor)
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
