"""Deterministic conflict resolution for the write path (MASTER_PLAN.md §8.3, §13.3).

MASTER_PLAN specs this step as LLM-driven (ADD/UPDATE/NOOP/INVALIDATE via a tool-calling
resolver against the top-K similar memories). House rule AGENTS.md §3.8 keeps ``core/``
runnable on ``numpy`` alone, and v2 has no LLM backend yet — so this is a **deterministic**
resolver: the embedding index narrows candidates (cheap, already computed at write time),
and the operation is decided from token-level overlap on the text itself, which is precise
and doesn't depend on embedder quality. An LLM-backed resolver can be plugged in later
behind the same ``resolve()`` signature without touching callers.

It deliberately collapses MASTER_PLAN's UPDATE and INVALIDATE into one ``INVALIDATE``
("supersede") operation — close the old fact's validity, add the new one — because both
must preserve history under the non-negotiable "never overwrite" rule (AGENTS.md §3.2),
and reliably telling "refines" apart from "contradicts" needs semantic judgment that a
deterministic heuristic shouldn't pretend to have.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from engraphis.core.interfaces import MemoryRecord
from engraphis.core.textutil import jaccard, tokenize

# Embedding-similarity floor: skip the (cheap but not free) token-overlap check for
# neighbors the vector index itself considers unrelated. The real decision is below.
RELATED_SIM_FLOOR = 0.15
# Token Jaccard on title+content: at/above this, treat it as a restatement of the same fact.
DUP_TOKEN_JACCARD = 0.85
# Token Jaccard: at/above this (but below DUP) it's the same subject with new content.
SUBJECT_TOKEN_JACCARD = 0.40


class ResolutionOp(str, Enum):
    ADD = "add"                # genuinely new -> insert
    NOOP = "noop"               # already known -> reinforce the existing memory, don't insert
    INVALIDATE = "invalidate"   # same subject, new content -> close old, insert new


@dataclass(frozen=True)
class Resolution:
    op: ResolutionOp
    target_id: Optional[str] = None   # the neighbor acted on, for noop/invalidate
    reason: str = ""


def resolve(candidate_text: str, neighbors: list[tuple[float, MemoryRecord]]) -> Resolution:
    """Decide ADD / NOOP / INVALIDATE for new content against its nearest neighbors.

    ``neighbors`` are ``(embedding_similarity, MemoryRecord)`` pairs that the caller has
    already scoped to the same workspace/repo/scope/mtype as the candidate (conflict
    resolution must not silently cross a scope boundary — promotion is explicit, §5.1)
    and filtered to currently-visible memories. Order doesn't matter; every neighbor
    above ``RELATED_SIM_FLOOR`` is checked and the best token-overlap match wins.
    """
    cand_tokens = tokenize(candidate_text)
    best: Optional[tuple[float, MemoryRecord, float]] = None  # (overlap, rec, sim)
    for sim, rec in neighbors:
        if sim < RELATED_SIM_FLOOR:
            continue
        overlap = jaccard(cand_tokens, tokenize(f"{rec.title} {rec.content}"))
        if best is None or overlap > best[0]:
            best = (overlap, rec, sim)

    if best is None:
        return Resolution(ResolutionOp.ADD, reason="no related memory in scope")

    overlap, rec, sim = best
    if overlap >= DUP_TOKEN_JACCARD:
        return Resolution(ResolutionOp.NOOP, target_id=rec.id,
                          reason=f"near-duplicate of {rec.id} (token overlap={overlap:.2f})")
    if overlap >= SUBJECT_TOKEN_JACCARD:
        return Resolution(ResolutionOp.INVALIDATE, target_id=rec.id,
                          reason=f"supersedes {rec.id} (same subject, "
                                 f"token overlap={overlap:.2f}, similarity={sim:.2f})")
    return Resolution(ResolutionOp.ADD, reason=f"related but distinct (best overlap={overlap:.2f})")
