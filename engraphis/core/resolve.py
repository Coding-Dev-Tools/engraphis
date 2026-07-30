"""Deterministic conflict resolution for the write path.

The original v2 design specs this step as LLM-driven (ADD/UPDATE/NOOP/INVALIDATE via a tool-calling
resolver against the top-K similar memories). House rule AGENTS.md §3.8 keeps ``core/``
runnable on ``numpy`` alone, and v2 has no LLM backend yet — so this is a **deterministic**
resolver, now with two signals: the embedding index narrows candidates (cheap, already
computed at write time) and supplies a cosine-similarity signal, and token-level overlap
on the text itself supplies a precise, embedder-independent signal. An LLM-backed resolver
can be plugged in later behind the same ``resolve()`` signature without touching callers.

It deliberately collapses the original design's UPDATE and INVALIDATE into one ``INVALIDATE``
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
# Embedding cosine at/above which two texts are treated as the *same fact in different
# words* (paraphrase), even when token overlap is low. Token Jaccard alone misses reworded
# contradictions ("limit is 100 rpm" vs "capped at five hundred requests each minute") —
# this reuses the embedding similarity the write path has already computed, so it stays
# deterministic and offline (no LLM call on untrusted input). Kept conservative, and the
# op is INVALIDATE (supersede), never NOOP: a wrongly-NOOP'd contradiction would discard
# the new fact and reinforce the stale one, while a wrongly-INVALIDATE'd restatement just
# refreshes the phrasing and keeps the old version readable in history.
PARAPHRASE_EMBED_SIM = 0.90
# Supersession without an explicit claim key is intentionally stricter than a
# generic "related" judgment.  Both independent signals must agree before a
# write hides a currently-live fact from ordinary recall.
STRONG_SUBJECT_TOKEN_JACCARD = 0.55
STRONG_JOINT_EMBED_SIM = 0.45


class ResolutionOp(str, Enum):
    ADD = "add"                # genuinely new -> insert
    NOOP = "noop"               # already known -> reinforce the existing memory, don't insert
    INVALIDATE = "invalidate"   # same subject, new content -> close old, insert new
    RELATE = "relate"           # retain both facts and persist a semantic relation


@dataclass(frozen=True)
class Resolution:
    op: ResolutionOp
    target_id: Optional[str] = None   # the neighbor acted on, for noop/invalidate
    reason: str = ""


def resolve(candidate_text: str, neighbors: list[tuple[float, MemoryRecord]], *,
            subject_key: str = "", claim_kind: str = "",
            candidate_content: Optional[str] = None) -> Resolution:
    """Decide ADD / NOOP / INVALIDATE for new content against its nearest neighbors.

    ``neighbors`` are ``(embedding_similarity, MemoryRecord)`` pairs that the caller has
    already scoped to the same workspace/repo/scope/mtype as the candidate (conflict
    resolution must not silently cross a scope boundary — promotion is explicit, §5.1)
    and filtered to currently-visible memories. Order doesn't matter; every neighbor
    above ``RELATED_SIM_FLOOR`` is checked and the best token-overlap match wins, with
    the embedding cosine as a second signal for paraphrased restatements/contradictions.
    """
    cand_tokens = tokenize(candidate_text)
    candidate_subject = str(subject_key or "").strip()
    candidate_kind = str(claim_kind or "").strip()
    exact_claim_neighbors: list[tuple[float, MemoryRecord]] = []
    fallback_neighbors: list[tuple[float, MemoryRecord]] = []
    for sim, rec in neighbors:
        record_subject = str(rec.subject_key or "").strip()
        record_kind = str(rec.claim_kind or "").strip()
        # Explicit claim identities outrank similarity. Two keyed records that
        # disagree on subject or predicate cannot be duplicate/supersession
        # candidates merely because their prose happens to be similar.
        if candidate_subject and record_subject:
            if candidate_subject != record_subject or candidate_kind != record_kind:
                continue
            exact_claim_neighbors.append((sim, rec))
            continue
        if sim < RELATED_SIM_FLOOR:
            continue
        fallback_neighbors.append((sim, rec))

    considered = exact_claim_neighbors or fallback_neighbors
    best: Optional[tuple[float, MemoryRecord, float]] = None      # (overlap, rec, sim)
    best_sim: Optional[tuple[float, MemoryRecord]] = None         # highest-cosine neighbor
    for sim, rec in considered:
        overlap = jaccard(cand_tokens, tokenize(f"{rec.title} {rec.content}"))
        if best is None or overlap > best[0]:
            best = (overlap, rec, sim)
        if best_sim is None or sim > best_sim[0]:
            best_sim = (sim, rec)

    if best is None:
        return Resolution(ResolutionOp.ADD, reason="no related memory in scope")

    overlap, rec, sim = best
    same_subject = bool(candidate_subject) and candidate_subject == (
        str(rec.subject_key or "").strip()
    )
    same_claim = same_subject and candidate_kind == str(rec.claim_kind or "").strip()
    if same_claim:
        # Candidate embeddings and overlap include a display title, but durable
        # claim equality is about the stored content.  Comparing title+content to
        # content would turn an identical titled write into a false supersession.
        duplicate_text = candidate_content if candidate_content is not None else candidate_text
        candidate_normalized = " ".join(duplicate_text.split()).casefold()
        record_normalized = " ".join(rec.content.split()).casefold()
        if candidate_normalized == record_normalized:
            return Resolution(
                ResolutionOp.NOOP,
                target_id=rec.id,
                reason=f"exact duplicate of keyed claim {rec.id}",
            )
        return Resolution(ResolutionOp.INVALIDATE, target_id=rec.id,
                          reason=f"supersedes {rec.id} (shared claim key, "
                                 f"token overlap={overlap:.2f}, similarity={sim:.2f})")
    if overlap >= DUP_TOKEN_JACCARD:
        if candidate_subject:
            return Resolution(
                ResolutionOp.INVALIDATE,
                target_id=rec.id,
                reason=f"replaces unkeyed duplicate {rec.id} with durable claim identity "
                       f"(token overlap={overlap:.2f})",
            )
        return Resolution(ResolutionOp.NOOP, target_id=rec.id,
                          reason=f"near-duplicate of {rec.id} (token overlap={overlap:.2f})")
    # Without an explicit claim key, invalidation needs strong agreement from
    # lexical and semantic signals.  A high cosine alone can be a topical
    # paraphrase rather than a contradiction, so it becomes a relation instead.
    if overlap >= STRONG_SUBJECT_TOKEN_JACCARD and sim >= STRONG_JOINT_EMBED_SIM:
        return Resolution(ResolutionOp.INVALIDATE, target_id=rec.id,
                          reason=f"supersedes {rec.id} (strong joint evidence: "
                                 f"token overlap={overlap:.2f}, similarity={sim:.2f})")
    if best_sim is not None and best_sim[0] >= PARAPHRASE_EMBED_SIM:
        psim, prec = best_sim
        povl = jaccard(cand_tokens, tokenize(f"{prec.title} {prec.content}"))
        return Resolution(ResolutionOp.RELATE, target_id=prec.id,
                          reason=f"related to {prec.id} (paraphrase-like: cosine={psim:.2f}, "
                                 f"token overlap={povl:.2f})")
    if overlap >= SUBJECT_TOKEN_JACCARD:
        return Resolution(ResolutionOp.RELATE, target_id=rec.id,
                          reason=f"related to {rec.id} (same topic, "
                                 f"token overlap={overlap:.2f}, similarity={sim:.2f})")
    return Resolution(ResolutionOp.ADD, reason=f"related but distinct (best overlap={overlap:.2f})")
