"""Recall engine — Phase 2 retrieval with retention-aware reranking.

Implements Conscious Recall: retrieves memories by semantic similarity, then
reranks by retention_score × cosine_similarity × surprise. Reinforces accessed
memories (Phase 4 write-back happens here too, on the recall path).
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import numpy as np

from engraphis.engines import embedder, reweight
from engraphis.stores import vectors as mem_store

logger = logging.getLogger("engraphis.recall")


def recall(
    *,
    namespace: Optional[str] = None,
    prompt: str,
    num_chunks: int = 10,
    document_ids: Optional[list[str]] = None,
    min_retention: float = 0.0,
    reinforce: bool = True,
) -> dict[str, Any]:
    """Query memory and return an LLM-friendly context string + source items."""
    query_vec = embedder.embed(prompt)
    candidates = mem_store.all_vectors(namespace=namespace)

    if document_ids:
        candidates = [c for c in candidates if c[2] in document_ids]

    if not candidates:
        return {"context": "", "chunks": [], "count": 0, "llmContextMessage": ""}

    scored = []
    for mem_id, ns, doc_id, vec, mem in candidates:
        r = reweight.retention_score(mem)
        if r < min_retention:
            continue
        sim = float(np.dot(query_vec, vec))
        surprise = mem.get("surprise", 1.0)
        score = r * sim * surprise
        scored.append((score, mem_id, mem, vec))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:num_chunks]

    chunks = []
    for score, mem_id, mem, vec in top:
        if reinforce:
            reweight.reinforce(mem_id)
        chunks.append({
            "documentId": mem["document_id"],
            "title": mem["title"],
            "namespace": mem["namespace"],
            "content": mem["content"],
            "score": score,
            "retention": reweight.retention_score(mem),
            "metadata": mem.get("metadata", {}),
            "createdAt": mem.get("created_at"),
            "updatedAt": mem.get("updated_at"),
        })

    context_str = _format_context(chunks)
    return {
        "context": chunks,
        "chunks": chunks,
        "count": len(chunks),
        "llmContextMessage": context_str,
    }


def recall_master(*, namespace: str, max_chunks: int = 10) -> dict[str, Any]:
    """Recall the highest-retention memories in a namespace (no prompt needed)."""
    candidates = mem_store.all_vectors(namespace=namespace)
    if not candidates:
        return {"context": [], "chunks": [], "count": 0, "llmContextMessage": ""}

    scored = []
    for mem_id, ns, doc_id, vec, mem in candidates:
        r = reweight.retention_score(mem)
        scored.append((r * mem.get("surprise", 1.0), mem_id, mem))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:max_chunks]

    chunks = []
    for score, mem_id, mem in top:
        reweight.reinforce(mem_id)
        chunks.append({
            "documentId": mem["document_id"],
            "title": mem["title"],
            "namespace": mem["namespace"],
            "content": mem["content"],
            "score": score,
            "retention": reweight.retention_score(mem),
            "metadata": mem.get("metadata", {}),
        })

    return {
        "context": chunks,
        "chunks": chunks,
        "count": len(chunks),
        "llmContextMessage": _format_context(chunks),
    }


def recall_by_retention(
    *,
    namespace: Optional[str] = None,
    top_k: int = 10,
    min_retention: float = 0.0,
    as_of: Optional[float] = None,
) -> dict[str, Any]:
    """Recall from the Ebbinghaus bank — pure retention ranking, no semantic query."""
    candidates = mem_store.all_vectors(namespace=namespace)
    scored = []
    for mem_id, ns, doc_id, vec, mem in candidates:
        r = reweight.retention_score(mem, now=as_of)
        if r < min_retention:
            continue
        scored.append((r, mem))
    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:top_k]
    memories = [
        {
            "documentId": mem["document_id"],
            "title": mem["title"],
            "content": mem["content"],
            "namespace": mem["namespace"],
            "retention": score,
            "stability": mem.get("stability"),
            "access_count": mem.get("access_count"),
            "metadata": mem.get("metadata", {}),
        }
        for score, mem in top
    ]
    return {"memories": memories, "count": len(memories)}


def _format_context(chunks: list[dict[str, Any]]) -> str:
    """Build the LLM-friendly context string passed to the model."""
    if not chunks:
        return ""
    parts = []
    for c in chunks:
        header = f"[{c['namespace']}:{c['documentId']}]"
        if c.get("title"):
            header += f" {c['title']}"
        parts.append(f"{header}\n{c['content']}")
    return "\n\n".join(parts)
