"""Recall engine — Phase 2 retrieval with retention-aware reranking.

Implements Conscious Recall: retrieves memories by semantic similarity, then
reranks by retention_score × cosine_similarity × surprise. Prompt recall only
reinforces accessed memories when explicitly requested.
"""
from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from typing import Any, Optional

import numpy as np

from engraphis.engines import embedder, reweight
from engraphis.stores import vectors as mem_store
from engraphis.core.poisoning import prompt_eligible

logger = logging.getLogger("engraphis.recall")

_MISSING = object()


def _nonnegative_limit(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


def _finite_number(value: Any, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{field} must be a finite number")
    return result


def _prompt_eligible(mem: Mapping[str, Any]) -> bool:
    metadata = mem.get("metadata")
    if not isinstance(metadata, Mapping):
        return False
    provenance = metadata.get("provenance", _MISSING)
    if provenance is _MISSING:
        # Rows written before provenance stamping are retained for v1 compatibility,
        # but an old top-level authority marker is ambiguous and fails closed.
        if any(key in metadata for key in (
            "trusted", "review_state", "quarantined", "quarantine",
        )):
            return False
        provenance = {
            "source": "legacy_store",
            "trusted": True,
            "trust_origin": "legacy_store",
            "review_state": "approved",
        }
    elif not isinstance(provenance, Mapping):
        return False
    # Restrictive legacy markers must win even when a nested marker is approved.
    if metadata.get("trusted", _MISSING) is False:
        return False
    if metadata.get("review_state", _MISSING) not in (_MISSING, "approved"):
        return False
    if metadata.get("quarantined") is True:
        return False
    return prompt_eligible(provenance, metadata)


def recall(
    *,
    namespace: Optional[str] = None,
    prompt: str,
    num_chunks: int = 10,
    document_ids: Optional[list[str]] = None,
    min_retention: float = 0.0,
    reinforce: bool = False,
) -> dict[str, Any]:
    """Query memory and return an LLM-friendly context string plus source items.

    Reinforcement is opt-in: retrieval alone must not strengthen attacker-controlled
    planted content.
    """
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must be a non-empty string")
    if namespace is not None and not isinstance(namespace, str):
        raise ValueError("namespace must be a string")
    num_chunks = _nonnegative_limit(num_chunks, field="num_chunks")
    min_retention = _finite_number(min_retention, field="min_retention")
    if document_ids is not None:
        if not isinstance(document_ids, list) or any(
            not isinstance(doc_id, str) for doc_id in document_ids
        ):
            raise ValueError("document_ids must be a list of strings")
    try:
        query_vec = np.asarray(embedder.embed(prompt), dtype=np.float32)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("embedder returned an invalid vector") from None
    if query_vec.ndim != 1 or query_vec.size == 0 or not np.isfinite(query_vec).all():
        raise ValueError("embedder returned an invalid vector")
    candidates = mem_store.all_vectors(namespace=namespace)

    if document_ids:
        candidates = [c for c in candidates if c[2] in document_ids]

    if not candidates:
        return {"context": "", "chunks": [], "count": 0, "llmContextMessage": ""}

    scored = []
    for mem_id, ns, doc_id, vec, mem in candidates:
        # The legacy store carries provenance inside metadata; reuse the v2 prompt gate
        # instead of trusting loosely typed ``trusted``/``review_state`` values.
        if not _prompt_eligible(mem):
            continue
        try:
            vec = np.asarray(vec, dtype=np.float32)
            if vec.ndim != 1 or vec.shape != query_vec.shape or not np.isfinite(vec).all():
                continue
            r = float(reweight.retention_score(mem))
            surprise = float(mem.get("surprise", 1.0))
            if not math.isfinite(r) or not math.isfinite(surprise) or r < min_retention:
                continue
            sim = float(np.dot(query_vec, vec))
            if not math.isfinite(sim):
                continue
        except (TypeError, ValueError, OverflowError):
            continue
        score = r * sim * surprise
        if not math.isfinite(score):
            continue
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


def recall_master(*, namespace: Optional[str] = None, max_chunks: int = 10) -> dict[str, Any]:
    """Recall the highest-retention memories in a namespace (no prompt needed)."""
    if namespace is not None and not isinstance(namespace, str):
        raise ValueError("namespace must be a string")
    max_chunks = _nonnegative_limit(max_chunks, field="max_chunks")
    candidates = mem_store.all_vectors(namespace=namespace)
    if not candidates:
        return {"context": [], "chunks": [], "count": 0, "llmContextMessage": ""}

    scored = []
    for mem_id, ns, doc_id, vec, mem in candidates:
        if not _prompt_eligible(mem):
            continue
        try:
            r = float(reweight.retention_score(mem))
            surprise = float(mem.get("surprise", 1.0))
            score = r * surprise
        except (TypeError, ValueError):
            continue
        if not math.isfinite(score):
            continue
        scored.append((score, mem_id, mem))

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
    if namespace is not None and not isinstance(namespace, str):
        raise ValueError("namespace must be a string")
    top_k = _nonnegative_limit(top_k, field="top_k")
    min_retention = _finite_number(min_retention, field="min_retention")
    if as_of is not None:
        as_of = _finite_number(as_of, field="as_of")
    candidates = mem_store.all_vectors(namespace=namespace)
    scored = []
    for mem_id, ns, doc_id, vec, mem in candidates:
        if not _prompt_eligible(mem):
            continue
        try:
            r = float(reweight.retention_score(mem, now=as_of))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(r) or r < min_retention:
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
