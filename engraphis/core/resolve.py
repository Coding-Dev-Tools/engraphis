"""Deterministic conflict resolution for the write path.

The original v2 design specs this step as LLM-driven (ADD/UPDATE/NOOP/INVALIDATE via a tool-calling
resolver against the top-K similar memories). House rule AGENTS.md §3.8 keeps ``core/``
runnable on ``numpy`` alone, and v2 has no LLM backend yet — so this is a **deterministic**
resolver, now with two signals: the embedding index narrows candidates (cheap, already
computed at write time) and supplies a cosine-similarity signal, and token-level overlap
on the text itself supplies a precise, embedder-independent signal. An LLM-backed resolver
can be plugged in later behind the same ``resolve()`` signature without touching callers.

For unkeyed reworded corrections — the case a lexical hashing embedder cannot score on
cosine alone — a third deterministic signal narrows the gap: an aligned token diff
(``difflib.SequenceMatcher``) between candidate and neighbor. A replace block whose two
sides carry disjoint changed numbers/dates of the same kind, anchored by a shared
neighbouring token (a *value swap*), or an explicit change marker in the candidate
("switched", "rescheduled", "increased", ...) upgrades same-subject overlap from
``RELATE`` to ``INVALIDATE``. Environment qualifiers (staging vs production), named
mixed-case identifiers in a swap (iOS -> Android), and clean noun-for-noun replacements
with no changed value (REST -> GraphQL docs, API runtime -> worker runtime) are vetoes:
those pairs are genuinely distinct facts and must both stay live.

It deliberately collapses the original design's UPDATE and INVALIDATE into one ``INVALIDATE``
("supersede") operation — close the old fact's validity, add the new one — because both
must preserve history under the non-negotiable "never overwrite" rule (AGENTS.md §3.2),
and reliably telling "refines" apart from "contradicts" needs semantic judgment that a
deterministic heuristic shouldn't pretend to have.
"""
from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from enum import Enum
import re
import unicodedata
from typing import Optional

from engraphis.core.interfaces import MemoryRecord
from engraphis.core.textutil import _STOPWORDS, jaccard, tokenize

# Embedding-similarity floor: skip the (cheap but not free) token-overlap check for
# neighbors the vector index itself considers unrelated. The real decision is below.
RELATED_SIM_FLOOR = 0.15
# Token Jaccard on title+content: at/above this, treat it as a restatement of the same fact.
DUP_TOKEN_JACCARD = 0.85
# Token Jaccard: at/above this (but below DUP) it's the same subject with new content.
SUBJECT_TOKEN_JACCARD = 0.40
# Supersession without an explicit claim key is intentionally stricter than a
STRONG_SUBJECT_TOKEN_JACCARD = 0.55
STRONG_JOINT_EMBED_SIM = 0.45
# Equal or near-equal lexical evidence from multiple live memories is not enough
# to retire one of them.  The hash-vector score is only a discovery/joint signal
# (see resolve()), so a margin keeps an ambiguous write from becoming a
# supersession merely because of candidate ordering.
AMBIGUITY_EPSILON = 1e-9
AMBIGUITY_MARGIN = 0.05

# Reworded-correction legs (unkeyed text only). Agreement bar: the pair shares enough
# surviving subject matter. The default bar is folded Jaccard >= REWRITE_JACCARD or
# folded containment of the smaller side >= REWRITE_CONTAINMENT (containment tolerates
# inflection drift like page/paging); an explicit change marker lowers the containment
# bar to REWRITE_MARKER_CONTAINMENT because the writer asserted the replacement, and a
# confident semantic cosine plus a marker opens the gate for embedders that survive
# heavy rewording.
REWRITE_JACCARD = 0.40
REWRITE_CONTAINMENT = 0.40
REWRITE_MARKER_CONTAINMENT = 0.20

# Relation persisted on ``mem_links`` when the deterministic detector finds a genuine
# high-severity contradiction that the resolver cannot safely supersede (no shared
# claim key and not enough joint lexical/semantic evidence). ``conflicts_with`` is a
# free-form relation label on an already-bi-temporal table: ``mem_links.relation`` is
# TEXT and every read/write path treats it as opaque, so no schema change is needed.
# The graph layer inference in ``core/graph_layers.py`` classifies unknown labels as
# the generic SEMANTIC overlay, which is the correct conservative default.
CONFLICT_RELATION = "conflicts_with"

_CHANGE_MARKERS = frozenset({
    "switched", "moved", "migrated", "replaced", "rescheduled", "relocated",
    "upgraded", "downgraded", "transferred", "renamed", "ported", "increased",
    "decreased", "raised", "lowered", "bumped", "extended", "reduced", "expanded",
    "changed", "grew", "resized", "retired", "deprecated", "instead", "now",
})
_LIGHT_TOKENS = frozenset({
    "use", "used", "using", "run", "ran", "set", "get", "go", "went",
}) | _CHANGE_MARKERS
_ENV_QUALIFIERS = frozenset({
    "staging", "production", "prod", "development", "dev", "test", "testing",
    "qa", "uat", "preview", "sandbox", "demo", "local",
})
# Aliases for the same logical environment so a write of "prod" and a record
# of "production" do not look like two distinct environments to the conflict
# veto. Tokens map to a single canonical form; a non-empty intersection
# between two canonical sets therefore means both sides refer to the same
# environment, and the "env_conflict" veto only fires when the canonical
# sets are non-empty on both sides AND disjoint.
_ENV_ALIASES: dict[str, str] = {
    "prod": "production",
    "production": "production",
    "dev": "development",
    "development": "development",
    "test": "test",
    "testing": "test",
    "qa": "qa",
    "uat": "qa",
    "staging": "staging",
    "preview": "preview",
    "sandbox": "sandbox",
    "demo": "demo",
    "local": "local",
}


def _canonical_env(tokens: set[str]) -> set[str]:
    """Fold env aliases to one canonical form per logical environment.

    A bare ``prod`` and a bare ``production`` are the same logical
    environment; folding them prevents a legitimate correction ("Prod API
    timeout is 30s" -> "Production API timeout increased to 90s") from being
    vetoed by ``env_conflict``.
    """
    return {_ENV_ALIASES.get(token, token) for token in tokens}


_MONTHS = frozenset({
    "january", "february", "march", "april", "may", "june", "july",
    "august", "september", "october", "november", "december",
})
_WEEKDAYS = frozenset({
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
})
_NUMBER_WORDS = frozenset({
    "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
    "ten", "eleven", "twelve", "hundred", "thousand", "million", "billion",
})
_ORDINAL_RE = re.compile(r"\d{1,10}(?:st|nd|rd|th)\Z")


def _normalise_claim_text(value: str) -> str:
    """Compare keyed claims independent of whitespace and terminal punctuation.

    Punctuation inside a token is part of the claim value: removing it would
    conflate versions (``v1.2``/``v12``), paths, and identifiers.  Only a
    separator at a whitespace or string boundary is presentation punctuation.
    """
    raw = str(value or "")
    return " ".join(
        "".join(
            " " if (
                unicodedata.category(character).startswith("P")
                and (
                    index == 0
                    or index == len(raw) - 1
                    or raw[index - 1].isspace()
                    or raw[index + 1].isspace()
                )
            ) else character
            for index, character in enumerate(raw)
        ).split()
    ).casefold()


def _fold(token: str) -> str:
    """Naive singular/plural fold so page/pages-style drift still aligns."""
    return token[:-1] if len(token) > 3 and token.endswith("s") else token


def _surface_tokens(text: str) -> list[tuple[str, bool]]:
    """Ordered ``(folded_token, is_named_identifier)`` pairs for diff alignment.

    Mirrors ``tokenize``'s filtering (same stopword set; short tokens dropped,
    except digit-bearing ones, which carry the values corrections change) and adds
    a named flag: a token with an uppercase letter past its first character that is
    not an all-caps acronym (``ProviderA``, ``iOS``, ``v2Beta``, but not ``API``).
    Such tokens are usually identifiers whose replacement signals distinct facts
    rather than a corrected value.
    """
    out: list[tuple[str, bool]] = []
    for surface in re.findall(r"[A-Za-z0-9]+", str(text or "")):
        lowered = surface.lower()
        named = (
            any(character.isupper() for character in surface[1:])
            and not surface.isupper()
        )
        folded = _fold(lowered)
        if folded in _STOPWORDS:
            continue
        if len(folded) <= 1 and not any(c.isdigit() for c in folded):
            continue
        out.append((folded, named))
    return out


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


@dataclass(frozen=True)
class CorrectionEvidence:
    """Deterministic diff verdict on whether a candidate rewrites a neighbour."""

    marker: bool
    value_swap: bool
    proper_swap: bool
    heavy_swap: bool
    env_conflict: bool
    shared_subject: int = 0


def resolve(candidate_text: str, neighbors: list[tuple[float, MemoryRecord]], *,
            subject_key: str = "", claim_kind: str = "",
            candidate_content: Optional[str] = None,
            temporal_splice: bool = False) -> Resolution:
    """Decide ADD / NOOP / INVALIDATE for new content against its nearest neighbors.

    ``neighbors`` are ``(embedding_similarity, MemoryRecord)`` pairs that the caller has
    already scoped to the same workspace/repo/scope/mtype as the candidate (conflict
    resolution must not silently cross a scope boundary — promotion is explicit, §5.1)
    and filtered to currently-visible memories. Order doesn't matter; every neighbor
    above ``RELATED_SIM_FLOOR`` is checked, and the best token-overlap match wins unless
    another live memory is a near-equal strong match, in which case resolution relates
    without superseding either one. Cosine is candidate-discovery and *joint* evidence
    only: the dependency-free hashing embedder is lexical, not a sound
    paraphrase/contradiction classifier.
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
    scored: list[tuple[float, MemoryRecord, float]] = []
    for sim, rec in considered:
        overlap = jaccard(cand_tokens, tokenize(f"{rec.title} {rec.content}"))
        scored.append((overlap, rec, sim))
    # Retrieval order is not part of the resolution contract.  Stable tie-breaking
    # makes repeated writes idempotent even when a vector backend returns equal-score
    # neighbors in a different order.  When a claim has multiple visible versions,
    # prefer the latest world-time version before falling back to its id so a
    # supersession follows the temporal chain rather than arbitrary retrieval order.
    scored.sort(
        key=lambda item: (
            -item[0],
            -item[2],
            -(item[1].valid_from if item[1].valid_from is not None else float("-inf")),
            str(item[1].id),
        )
    )
    best: Optional[tuple[float, MemoryRecord, float]] = (
        scored[0] if scored else None
    )
    if best is None:
        return Resolution(ResolutionOp.ADD, reason="no related memory in scope")

    overlap, rec, sim = best
    rec_text = f"{rec.title} {rec.content}"
    same_subject = bool(candidate_subject) and candidate_subject == (
        str(rec.subject_key or "").strip()
    )
    same_claim = same_subject and candidate_kind == str(rec.claim_kind or "").strip()
    if same_claim:
        # Candidate embeddings and overlap include a display title, but durable
        # claim equality is about the stored content.  Comparing title+content to
        # content would turn an identical titled write into a false supersession.
        duplicate_text = candidate_content if candidate_content is not None else candidate_text
        candidate_normalized = _normalise_claim_text(duplicate_text)
        record_normalized = _normalise_claim_text(rec.content)
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
            # A new explicit claim identity must not retire a merely similar unkeyed
            # note. Promote only an exact restatement; a reworded match needs either a
            # shared key or an explicit human correction because offline hashing cannot
            # prove that the two claims have the same predicate.
            duplicate_text = candidate_content if candidate_content is not None else candidate_text
            candidate_normalized = " ".join(duplicate_text.split()).casefold()
            record_normalized = " ".join(rec.content.split()).casefold()
            if candidate_normalized == record_normalized:
                return Resolution(
                    ResolutionOp.INVALIDATE,
                    target_id=rec.id,
                    reason=f"replaces exact unkeyed duplicate {rec.id} with durable claim "
                           f"identity (token overlap={overlap:.2f})",
                )
            return Resolution(
                ResolutionOp.RELATE,
                target_id=rec.id,
                reason=f"related unkeyed memory {rec.id}; explicit claim identity differs "
                       f"(token overlap={overlap:.2f})",
            )
        return Resolution(ResolutionOp.NOOP, target_id=rec.id,
                          reason=f"near-duplicate of {rec.id} (token overlap={overlap:.2f})")
    # Without an explicit claim key, invalidation needs agreement from the lexical
    # and semantic signals plus — for reworded corrections — diff evidence that a
    # value actually changed. A clean noun-for-noun replacement with no changed
    # value ("runtime for the API service" vs "for the worker service", REST docs
    # vs GraphQL docs) vetoes even strong joint evidence: the pair is two distinct
    # facts about one topic, not a correction.
    evidence: Optional[CorrectionEvidence] = None
    strong = (not candidate_subject and overlap >= STRONG_SUBJECT_TOKEN_JACCARD
              and sim >= STRONG_JOINT_EMBED_SIM)
    containment = _containment(cand_tokens, rec_text)
    marker = _has_marker(candidate_text)
    rewrite_gate = (
        not candidate_subject and sim >= RELATED_SIM_FLOOR
        and (overlap >= REWRITE_JACCARD
             or containment >= REWRITE_CONTAINMENT
             or (containment >= REWRITE_MARKER_CONTAINMENT and marker)
             or (sim >= STRONG_JOINT_EMBED_SIM and marker))
    )
    if strong or rewrite_gate:
        evidence = _correction_evidence(candidate_text, rec_text)
    if strong:
        ambiguous = [
            item for item in scored[1:]
            if item[0] >= STRONG_SUBJECT_TOKEN_JACCARD
            and item[2] >= STRONG_JOINT_EMBED_SIM
            and overlap - item[0] <= AMBIGUITY_MARGIN + AMBIGUITY_EPSILON
        ]
        if ambiguous:
            ids = ", ".join(sorted({rec.id, *(item[1].id for item in ambiguous)}))
            return Resolution(
                ResolutionOp.RELATE,
                reason=f"ambiguous strong match among {ids}; no memory superseded "
                       f"(best overlap={overlap:.2f}, similarity={sim:.2f})",
            )
        # Swap vetoes protect *live* neighbours on ordinary present-time writes:
        # a heavy or named-identifier swap against a live fact reads as two
        # coexisting facts. An explicitly time-anchored write asserts chain
        # membership (bi-temporal splice), and a closed predecessor is already
        # historical — strong joint evidence supersedes both regardless of prose.
        # Clashing environment qualifiers (staging vs production) always veto,
        # matching the contract honoured by the rewrite_gate branch below: a
        # strong overlap between a staging fact and a production fact is two
        # coexisting truths, not a single fact being corrected. The marker
        # override on proper_swap requires a value_swap alongside the marker
        # so a bare "now" can never retire a fact it merely shares surface
        # nouns with.
        assert evidence is not None  # strong => evidence was computed above
        swap_veto = (evidence.heavy_swap
                     or (evidence.proper_swap and not (marker and evidence.value_swap))
                     or evidence.env_conflict)
        if not swap_veto or temporal_splice or rec.valid_to is not None:
            return Resolution(ResolutionOp.INVALIDATE, target_id=rec.id,
                              reason=f"supersedes {rec.id} (strong joint evidence: "
                                     f"token overlap={overlap:.2f}, similarity={sim:.2f})")
    if rewrite_gate and evidence is not None and not evidence.env_conflict:
        # A bare change marker ("now", "actually", ...) on a candidate that
        # shares no subject tokens with the neighbour is not correction
        # evidence — common words leak into every sentence. Require the
        # same shared-subject floor that the value-swap branch uses, so the
        # marker can only lift a candidate that already overlaps on the
        # same subject.
        marker_corrected = (
            evidence.marker
            and evidence.value_swap
            and not evidence.proper_swap
            and not evidence.heavy_swap
            and evidence.shared_subject >= 2
        )
        value_corrected = (
            evidence.value_swap
            and evidence.shared_subject >= 2
            and not evidence.proper_swap
            and not evidence.heavy_swap
        )
        if marker_corrected or value_corrected:
            if marker_corrected:
                kind = "change marker"
            else:
                kind = "value change"
            return Resolution(
                ResolutionOp.INVALIDATE, target_id=rec.id,
                reason=f"supersedes {rec.id} (reworded correction by {kind}: "
                       f"token overlap={overlap:.2f}, similarity={sim:.2f})",
            )
    if overlap >= SUBJECT_TOKEN_JACCARD:
        return Resolution(ResolutionOp.RELATE, target_id=rec.id,
                          reason=f"related to {rec.id} (same topic, "
                                 f"token overlap={overlap:.2f}, similarity={sim:.2f})")
    return Resolution(ResolutionOp.ADD, reason=f"related but distinct (best overlap={overlap:.2f})")


def _containment(cand_tokens: set[str], rec_text: str) -> float:
    """Share of the smaller folded token set that survives across the pair."""
    cand_folded = {_fold(token) for token in cand_tokens}
    rec_folded = {_fold(token) for token in tokenize(rec_text)}
    if not cand_folded or not rec_folded:
        return 0.0
    return len(cand_folded & rec_folded) / min(len(cand_folded), len(rec_folded))


def _has_marker(text: str) -> bool:
    return bool({word for word in re.findall(r"[a-z]+", str(text or "").lower())}
                & _CHANGE_MARKERS)


def _is_value(token: str) -> bool:
    """Numbers, dates (incl. month/weekday names and spelled-out small counts)."""
    return (
        any(character.isdigit() for character in token)
        or bool(_ORDINAL_RE.fullmatch(token))
        or token in _MONTHS or token in _WEEKDAYS or token in _NUMBER_WORDS
    )


def _value_kind(token: str) -> str:
    """Coarse value class so "budget 50k" never swaps against "deadline March 15"."""
    if token in _MONTHS or token in _WEEKDAYS:
        return "cal"
    if _ORDINAL_RE.fullmatch(token):
        return "ord"
    if any(character.isdigit() for character in token):
        return "num"
    return "numword"


_SWAP_SPAN = tuple[tuple[int, int], tuple[int, int]]


def _swap_spans(cand_words: list[str], rec_words: list[str]) -> list[_SWAP_SPAN]:
    """Index spans of aligned replace blocks.

    SequenceMatcher with ``autojunk=False`` never emits adjacent delete+insert
    pairs for genuine value swaps (it always uses ``replace``), so we rely on
    the ``replace`` opcode alone and skip the merge.
    """
    matcher = SequenceMatcher(None, cand_words, rec_words, autojunk=False)
    return [((i1, i2), (j1, j2)) for op, i1, i2, j1, j2
            in matcher.get_opcodes() if op == "replace"]


def _anchor_ok(cand: list[tuple[str, bool]], rec: list[tuple[str, bool]],
               old_span: tuple[int, int], new_span: tuple[int, int]) -> bool:
    """True when a value swap shares a neighbouring token across both sides.

    The neighbours of the changed *values themselves* (within +/-1 of each value
    position) approximate the attribute being re-valued: "2 replicas -> 6
    replicas" shares ``replicas``; "budget 50k -> deadline March 15" shares only
    sentence furniture ("per", "the"), because the attribute itself changed — a
    distinct fact, not a correction. The tight radius keeps shared subject nouns
    ("search index", "staging database") outside the window.
    """
    def neighbourhood(seq: list[tuple[str, bool]], span: tuple[int, int]) -> set[str]:
        positions: list[int] = []
        for index in range(*span):
            if _is_value(seq[index][0]):
                positions.extend([index - 1, index, index + 1])
        return {seq[i][0] for i in positions if 0 <= i < len(seq)}

    return bool(neighbourhood(cand, old_span) & neighbourhood(rec, new_span))


def _correction_evidence(candidate_text: str, record_text: str) -> CorrectionEvidence:
    """Deterministic diff evidence for (or against) a reworded correction.

    Inspects the aligned replace blocks: a *value swap* changes disjoint
    numbers/dates of the same kind with a shared attribute neighbour; a *heavy
    swap* replaces plain nouns with no value involved — the signature of two
    distinct facts. Named mixed-case identifiers inside a value block set
    ``proper_swap`` (ProviderA -> ProviderB beside 4 -> 8 workers), and clashing
    environment qualifiers (staging vs production) set ``env_conflict``; both veto
    the value-swap leg.
    """
    cand = _surface_tokens(candidate_text)
    rec = _surface_tokens(record_text)
    cand_words = [token for token, _ in cand]
    rec_words = [token for token, _ in rec]
    env_a = {token for token, _ in cand if token in _ENV_QUALIFIERS}
    env_b = {token for token, _ in rec if token in _ENV_QUALIFIERS}
    env_a_canon = _canonical_env(env_a)
    env_b_canon = _canonical_env(env_b)
    env_conflict = bool(env_a_canon and env_b_canon and env_a_canon.isdisjoint(env_b_canon))

    value_swap = False
    proper_swap = False
    heavy_swap = False
    for old_span, new_span in _swap_spans(cand_words, rec_words):
        old_pairs = cand[old_span[0]:old_span[1]]
        new_pairs = rec[new_span[0]:new_span[1]]
        old_values = [token for token, _ in old_pairs if _is_value(token)]
        new_values = [token for token, _ in new_pairs if _is_value(token)]
        if old_values and new_values:
            if (set(old_values).isdisjoint(new_values)
                    and _value_kind(old_values[0]) == _value_kind(new_values[0])
                    and _anchor_ok(cand, rec, old_span, new_span)):
                value_swap = True
            if any(named for _, named in [*old_pairs, *new_pairs]):
                proper_swap = True
        elif old_pairs and new_pairs:
            # Env-alias tokens (prod/production, dev/development, ...) fold
            # to the same canonical form, so swapping one for the other is
            # not a noun-for-noun replacement — exclude them from the
            # heavy_swap count so legitimate corrections like "prod API
            # timeout is 30s" -> "production API timeout increased to 90s"
            # are not vetoed as coexisting facts.
            old_heavy = [token for token, _ in old_pairs
                         if token not in _LIGHT_TOKENS
                         and not _is_value(token)
                         and token not in _ENV_QUALIFIERS]
            new_heavy = [token for token, _ in new_pairs
                         if token not in _LIGHT_TOKENS
                         and not _is_value(token)
                         and token not in _ENV_QUALIFIERS]
            if old_heavy and new_heavy:
                heavy_swap = True

    def _subject_tokens(pairs: list[tuple[str, bool]]) -> set[str]:
        return {token for token, _ in pairs
                if token not in _LIGHT_TOKENS and not _is_value(token)}

    shared_subject = len(_subject_tokens(cand) & _subject_tokens(rec))
    return CorrectionEvidence(
        marker=_has_marker(candidate_text), value_swap=value_swap,
        proper_swap=proper_swap, heavy_swap=heavy_swap,
        env_conflict=env_conflict, shared_subject=shared_subject,
    )
