"""Thought synthesis engine — Phase 2 of the consciousness loop.

Calls the configured external LLM to produce a compact latent-state update
from recalled memory context, then persists the thought as a new memory
artifact (Phase 4 write-back).
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from engraphis.engines import recall as recall_engine
from engraphis.llm.client import LLMClient
from engraphis.stores import ledger as ledger_store

logger = logging.getLogger("engraphis.thoughts")


def synthesize_thoughts(
    *,
    namespace: Optional[str] = None,
    max_chunks: int = 10,
    temperature: float = 0.3,
    randomness_seed: Optional[int] = None,
    persist: bool = True,
    thought_prompt: Optional[str] = None,
) -> dict[str, Any]:
    """Recall recent high-salience memories, synthesize a thought via LLM, optionally persist."""
    # Pass the namespace through as-is: ``None`` recalls across ALL namespaces (the
    # consciousness loop calls this with namespace=None). Coercing to a nonexistent
    # "_global" namespace made every global synthesis silently recall nothing and no-op.
    ctx = recall_engine.recall_master(namespace=namespace, max_chunks=max_chunks)
    chunks = ctx.get("chunks", [])
    if not chunks:
        return {"thought": None, "source_count": 0, "persisted": False, "reason": "no_memories"}

    context_text = ctx.get("llmContextMessage", "")
    source_ids = [c.get("documentId") for c in chunks]

    try:
        with LLMClient() as llm:
            thought = llm.synthesize_thought(
                context=context_text,
                temperature=temperature,
                thought_prompt=thought_prompt,
            )
    except Exception as e:
        logger.error("Thought synthesis failed: %s", e)
        return {"thought": None, "source_count": len(chunks), "persisted": False, "error": str(e)}

    persisted_id = None
    if persist and thought:
        content = json.dumps(thought, ensure_ascii=False)
        persisted_id = ledger_store.save_thought(
            namespace=namespace or "_global",
            content=content,
        )

    return {
        "thought": thought,
        "source_memory_ids": source_ids,
        "source_count": len(chunks),
        "persisted": persisted_id is not None,
        "thought_id": persisted_id,
    }


def get_thoughts(namespace: str, limit: int = 50) -> list[dict[str, Any]]:
    return ledger_store.get_thoughts(namespace, limit=limit)
