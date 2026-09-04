"""Deterministic, token-budgeted context packing.

The default packer deliberately has no model or tokenizer dependency.  It uses a
small, named regex tokenizer so its accounting is exact for the counter it
declares, reproducible offline, and replaceable by benchmark/provider-specific
token counters at the composition boundary.
"""
from __future__ import annotations

import copy
import dataclasses
import math
import re
from collections.abc import Callable
from typing import NamedTuple, Optional

from engraphis.core.interfaces import (
    Candidate,
    ContextUsage,
    PackedChunk,
)


_TOKEN_RE = re.compile(r"\w+|[^\w\s]", re.UNICODE)
_SENTENCE_RE = re.compile(r"(?<=[.!?])(?:[\"')\]]*)\s+|\n+")
_CLAUSE_SPLIT_RE = re.compile(r"(?<=[.!?;])(?:[\"')\]]*)\s+|\n+")
_WORD_RE = re.compile(r"\w+", re.UNICODE)
_BRIDGE_TERMS = frozenset({
    "call", "calls", "called", "caller", "dependency", "depends", "flow",
    "graph", "impact", "path", "related", "relationship", "why",
})
_QUALIFIER_TERMS = frozenset({
    "cannot", "except", "if", "must", "never", "no", "not", "only",
    "unless", "until", "when", "without",
})


class ContextPackResult(NamedTuple):
    """Result of deterministic context packing.

    Exposes the canonical 3-tuple contract ``(context, chunks, usage)`` with
    named attribute accessors and aliases for agent prompt composers.
    """

    context: str
    chunks: list[PackedChunk]
    usage: ContextUsage

    @property
    def packed_chunks(self) -> list[PackedChunk]:
        return self.chunks

    @property
    def packed(self) -> list[PackedChunk]:
        return self.chunks


def _extract_shingles(text: str, n: int = 4) -> set[tuple[str, ...]]:
    """Extract case-folded n-gram token shingles from text."""
    words = [match.group(0).casefold() for match in _WORD_RE.finditer(text or "")]
    if not words:
        return set()
    if len(words) < n:
        return {tuple(words)}
    return {tuple(words[i : i + n]) for i in range(len(words) - n + 1)}


def _split_clauses(text: str) -> list[str]:
    """Split text into sentence/clause units while preserving text content."""
    source = (text or "").strip()
    if not source:
        return []
    parts = [part.strip() for part in _CLAUSE_SPLIT_RE.split(source) if part.strip()]
    return parts if parts else [source]


def _normalize_clause(clause: str) -> str:
    """Case-folded normalized word sequence for exact clause matching."""
    return " ".join(_WORD_RE.findall(clause.casefold()))


def _is_clause_redundant(
    clause: str,
    admitted_shingles: set[tuple[str, ...]],
    admitted_clauses: set[str],
    admitted_qualifiers: set[str],
    *,
    shingle_size: int = 4,
    duplication_threshold: float = 0.6,
) -> bool:
    """Whether a candidate clause has significant verbatim overlap with admitted evidence."""
    norm = _normalize_clause(clause)
    if not norm:
        return True

    words = [match.group(0).casefold() for match in _WORD_RE.finditer(clause)]
    if not words:
        return True

    # 1. Exact verbatim match against already admitted clauses
    if norm in admitted_clauses:
        return True

    # Check semantic safety for qualifiers: never prune a clause that introduces
    # an exception or restriction not already covered
    clause_qualifiers = _terms(clause) & _QUALIFIER_TERMS
    if not clause_qualifiers.issubset(admitted_qualifiers):
        return False

    # 2. For short clauses (< shingle_size words), check exact clause containment
    if len(words) < shingle_size:
        return any(norm == ac for ac in admitted_clauses)

    # 3. For multi-word clauses, check token shingle duplication ratio
    shingles = _extract_shingles(clause, n=shingle_size)
    if not shingles:
        return False

    overlap = len(shingles & admitted_shingles)
    duplication_ratio = overlap / len(shingles)
    return duplication_ratio >= duplication_threshold


def _with_pruned_content(
    candidate: Candidate, content: str, summary: str
) -> Candidate:
    """Create a shallow clone of candidate with pruned delta content/summary."""
    record = candidate.record
    if record is None:
        return candidate
    if dataclasses.is_dataclass(record):
        new_record = dataclasses.replace(record, content=content, summary=summary)
    else:
        new_record = copy.copy(record)
        new_record.content = content
        new_record.summary = summary
    if dataclasses.is_dataclass(candidate):
        return dataclasses.replace(candidate, record=new_record)
    else:
        new_candidate = copy.copy(candidate)
        new_candidate.record = new_record
        return new_candidate


class RegexTokenCounter:
    """Exact counter for Engraphis' dependency-free tokenization contract."""

    identity = "engraphis.regex.v1"

    def __call__(self, text: str) -> int:
        return len(_TOKEN_RE.findall(text or ""))


class DeterministicContextPacker:
    """Pack diverse, relevant evidence into a strict token budget.

    Selection is stable for identical inputs.  A supersession/consolidation
    family contributes at most one member, summaries are preferred when they
    retain query evidence, and oversized sources are reduced at sentence
    boundaries before a final token-boundary fallback.
    """

    def __init__(
        self,
        token_counter: Optional[Callable[[str], int]] = None,
        *,
        token_counter_identity: Optional[str] = None,
        redundancy_pruning: bool = True,
        score_elbow_gating: bool = True,
        elbow_ratio: float = 0.5,
        tail_confidence_floor: float = 0.35,
        shingle_size: int = 4,
        clause_duplication_threshold: float = 0.6,
    ) -> None:
        self._count = token_counter or RegexTokenCounter()
        self.token_counter_identity = (
            token_counter_identity
            or getattr(self._count, "identity", None)
            or getattr(self._count, "__name__", None)
            or type(self._count).__name__
        )
        self.redundancy_pruning = bool(redundancy_pruning)
        self.score_elbow_gating = bool(score_elbow_gating)
        self.elbow_ratio = float(elbow_ratio)
        self.tail_confidence_floor = float(tail_confidence_floor)
        self.shingle_size = max(2, int(shingle_size))
        self.clause_duplication_threshold = float(clause_duplication_threshold)

    def pack(
        self,
        query: str,
        candidates: list[Candidate],
        token_budget: int,
    ) -> ContextPackResult:
        budget = max(0, int(token_budget))
        source_tokens = sum(self._source_tokens(candidate) for candidate in candidates)
        if budget == 0 or not candidates:
            return ContextPackResult(
                context="",
                chunks=[],
                usage=self._usage(
                    budget, 0, source_tokens, 0, len(candidates)
                ),
            )

        representatives, duplicate_count = _family_representatives(candidates)
        query_terms = _terms(query)
        needs_bridge = bool(query_terms & _BRIDGE_TERMS) or bool(
            re.search(r"(?:\w+[./\\])+\w+|::|->|\b[A-Za-z_]\w*\(\)", query)
        )
        ordered = self._selection_order(
            representatives, query_terms=query_terms, needs_bridge=needs_bridge
        )

        context = ""
        packed: list[PackedChunk] = []
        covered: set[str] = set()
        remaining = list(ordered)

        top_score = max((float(c.score) for c in ordered), default=0.0)
        admitted_scores: list[float] = []
        admitted_shingles: set[tuple[str, ...]] = set()
        admitted_clauses: set[str] = set()
        admitted_qualifiers: set[str] = set()

        while remaining:
            # Re-evaluate novelty after every selection.  This gives compact,
            # complementary evidence preference over repeated keyword matches.
            remaining.sort(
                key=lambda candidate: self._utility(
                    candidate,
                    query_terms=query_terms,
                    covered=covered,
                    needs_bridge=needs_bridge,
                ),
                reverse=True,
            )
            candidate = remaining.pop(0)
            record = candidate.record
            if record is None:
                continue

            # Elastic score-elbow gating: gate candidate if scores drop steeply
            # into a low-confidence tail after evidence has been admitted.
            if self.score_elbow_gating and admitted_scores:
                if self._is_score_elbow(
                    candidate,
                    top_score=top_score,
                    last_admitted_score=admitted_scores[-1],
                    admitted_count=len(packed),
                    needs_bridge=needs_bridge,
                ):
                    continue

            # Inter-candidate clause redundancy pruning:
            # If higher-priority memories have already been admitted, prune
            # duplicate clauses to retain and pack only novel delta content.
            candidate_to_pack = candidate
            is_delta = False
            if self.redundancy_pruning and admitted_shingles:
                full_content = record.content or ""
                summary_content = record.summary or ""

                pruned_content, content_pruned = self._prune_redundant_clauses(
                    full_content,
                    admitted_shingles,
                    admitted_clauses,
                    admitted_qualifiers,
                )
                pruned_summary, summary_pruned = self._prune_redundant_clauses(
                    summary_content,
                    admitted_shingles,
                    admitted_clauses,
                    admitted_qualifiers,
                )

                has_original_text = bool(full_content.strip() or summary_content.strip())
                has_novel_text = bool(pruned_content.strip() or pruned_summary.strip())
                if has_original_text and not has_novel_text:
                    continue

                if content_pruned or summary_pruned:
                    is_delta = True
                    candidate_to_pack = _with_pruned_content(
                        candidate, pruned_content, pruned_summary
                    )

            prefix = "\n\n" if context else ""
            ordinal = len(packed) + 1
            header = self._header(candidate_to_pack, ordinal)
            base = f"{context}{prefix}{header}\n"
            excerpt = ""
            truncated = False
            reason = ""
            available = max(0, budget - self._count(base))
            if available:
                excerpt, truncated, reason = self._excerpt(
                    query, candidate_to_pack, available
                )

            # Keep the established single-pass behavior for ordinary sources.
            # Only retry against the cheaper ordinal-only header when the selected
            # excerpt already starts with the exact displayed title (or the titled
            # header left no room). This removes prompt duplication without deleting
            # evidence or weakening the stable ``[n]`` citation bridge.
            rec = candidate_to_pack.record or record
            if not excerpt or _starts_with_title(excerpt, rec.title):
                compact_base = (
                    f"{context}{prefix}"
                    f"{self._header(candidate_to_pack, ordinal, include_title=False)}\n"
                )
                if self._count(compact_base) < budget:
                    compact_available = budget - self._count(compact_base)
                    compact = self._excerpt(query, candidate_to_pack, compact_available)
                    if compact[0] and _starts_with_title(compact[0], rec.title):
                        base = compact_base
                        available = compact_available
                        excerpt, truncated, reason = compact
            if not excerpt:
                continue

            if is_delta:
                truncated = True
                if not reason or reason in ("full", "summary"):
                    reason = "novel_delta"

            proposed = f"{base}{excerpt}"
            if self._count(proposed) > budget:
                # A custom tokenizer need not be additive.  Fit against the
                # complete proposed context so the public hard-budget contract
                # still holds.
                excerpt = self._fit_text(
                    excerpt,
                    max_tokens=available,
                    prefix=base,
                    total_budget=budget,
                )
                truncated = True
                reason = "token_boundary_excerpt"
                if not excerpt:
                    continue
                proposed = f"{base}{excerpt}"

            context = proposed
            packed.append(PackedChunk(
                id=candidate.id,
                excerpt=excerpt,
                tokens=self._count(excerpt),
                truncated=truncated,
                reason=reason,
            ))
            covered.update(_terms(excerpt) & query_terms)

            # Track admitted evidence for subsequent redundancy pruning and elbow gating
            admitted_scores.append(float(candidate.score))
            admitted_shingles.update(_extract_shingles(excerpt, n=self.shingle_size))
            for cl in _split_clauses(excerpt):
                norm_cl = _normalize_clause(cl)
                if norm_cl:
                    admitted_clauses.add(norm_cl)
            admitted_qualifiers.update(_terms(excerpt) & _QUALIFIER_TERMS)

        context_tokens = self._count(context)
        omitted = len(candidates) - len(packed)
        # ``duplicate_count`` is intentionally folded into omitted_count; keep
        # the local name to make the family-diversity policy explicit.
        omitted = max(omitted, duplicate_count)
        return ContextPackResult(
            context=context,
            chunks=packed,
            usage=self._usage(
                budget, context_tokens, source_tokens, len(packed), omitted
            ),
        )

    pack_context = pack

    def _prune_redundant_clauses(
        self,
        text: str,
        admitted_shingles: set[tuple[str, ...]],
        admitted_clauses: set[str],
        admitted_qualifiers: set[str],
    ) -> tuple[str, bool]:
        """Prune redundant clauses from text, returning (novel_delta_text, was_pruned)."""
        if not text or not self.redundancy_pruning:
            return text, False

        clauses = _split_clauses(text)
        if not clauses:
            return "", False

        novel_clauses: list[str] = []
        pruned_any = False

        for clause in clauses:
            if _is_clause_redundant(
                clause,
                admitted_shingles,
                admitted_clauses,
                admitted_qualifiers,
                shingle_size=self.shingle_size,
                duplication_threshold=self.clause_duplication_threshold,
            ):
                pruned_any = True
            else:
                novel_clauses.append(clause)

        if not novel_clauses:
            return "", True

        if not pruned_any:
            return text, False

        delta_text = " ".join(novel_clauses)
        return delta_text, True

    def _is_score_elbow(
        self,
        candidate: Candidate,
        *,
        top_score: float,
        last_admitted_score: float,
        admitted_count: int,
        needs_bridge: bool,
    ) -> bool:
        """Elastic score-elbow gating for low-confidence candidate retrieval tails."""
        if not self.score_elbow_gating or admitted_count < 1 or top_score <= 0.0:
            return False

        if needs_bridge and candidate.arm in {"graph", "code"}:
            return candidate.score <= 0.0

        score = float(candidate.score)
        if score <= 0.0:
            return True

        rel_to_top = score / top_score
        rel_to_last = score / max(last_admitted_score, 1e-9)

        elastic_tail_floor = min(
            0.40, self.tail_confidence_floor + 0.03 * (admitted_count - 1)
        )
        elastic_elbow_ratio = min(
            0.60, self.elbow_ratio + 0.03 * (admitted_count - 1)
        )

        return rel_to_top < elastic_tail_floor and rel_to_last < elastic_elbow_ratio

    def count_tokens(self, text: str) -> int:
        """Count answer text with the exact counter declared by this packer."""
        return int(self._count(text or ""))

    def _selection_order(
        self,
        candidates: list[Candidate],
        *,
        query_terms: set[str],
        needs_bridge: bool,
    ) -> list[Candidate]:
        return sorted(
            candidates,
            key=lambda candidate: self._utility(
                candidate,
                query_terms=query_terms,
                covered=set(),
                needs_bridge=needs_bridge,
            ),
            reverse=True,
        )

    def _utility(
        self,
        candidate: Candidate,
        *,
        query_terms: set[str],
        covered: set[str],
        needs_bridge: bool,
    ) -> tuple[float, float, str]:
        record = candidate.record
        if record is None:
            return (-math.inf, -math.inf, candidate.id)
        text = f"{record.title} {record.summary or record.content}"
        terms = _terms(text)
        overlap = terms & query_terms
        novelty = len(overlap - covered) / max(1, len(query_terms))
        relevance = max(0.0, float(candidate.score))
        bridge = 0.2 if needs_bridge and candidate.arm in {"graph", "code"} else 0.0
        compactness = 1.0 / math.sqrt(max(1, self._count(text)))
        utility = (0.7 * relevance) + (0.25 * novelty) + bridge + (0.05 * compactness)
        # Negate the lexical id tie-break while sorting reverse by using a
        # stable ordinal derived from the original id separately below.
        return (utility, relevance, _reverse_text(candidate.id))

    def _excerpt(
        self,
        query: str,
        candidate: Candidate,
        max_tokens: int,
    ) -> tuple[str, bool, str]:
        record = candidate.record
        if record is None or max_tokens <= 0:
            return "", False, ""
        full = (record.content or "").strip()
        summary = (record.summary or "").strip()
        query_terms = _terms(query)

        if summary and self._summary_is_useful(summary, full, query_terms):
            if self._count(summary) <= max_tokens:
                return summary, summary != full, "summary"
            # A summary can still be more evidence-dense than the source even
            # when it does not fit in full.  Prefer a sentence-aligned subset
            # only when it retains the same safeguards required for replacing
            # the source at all: query evidence and every source qualifier.
            summary_excerpt = self._sentence_excerpt(
                summary, query_terms, max_tokens
            )
            if summary_excerpt and self._summary_is_useful(
                summary_excerpt, full, query_terms
            ):
                return summary_excerpt, True, "summary_excerpt"

        if full and self._count(full) <= max_tokens:
            return full, False, (
                "bridge_evidence" if candidate.arm in {"graph", "code"} else "full"
            )

        excerpt = self._sentence_excerpt(full or summary, query_terms, max_tokens)
        if excerpt:
            return excerpt, True, (
                "bridge_excerpt"
                if candidate.arm in {"graph", "code"}
                else "relevant_sentence_excerpt"
            )
        fitted = self._fit_text(full or summary, max_tokens=max_tokens)
        return fitted, bool(fitted), "token_boundary_excerpt"

    def _summary_is_useful(
        self,
        summary: str,
        full: str,
        query_terms: set[str],
    ) -> bool:
        if not full:
            return True
        full_overlap = _terms(full) & query_terms
        summary_terms = _terms(summary)
        preserves_query = not full_overlap or bool(summary_terms & full_overlap)
        qualifiers = _terms(full) & _QUALIFIER_TERMS
        preserves_qualifiers = qualifiers.issubset(summary_terms)
        return preserves_query and preserves_qualifiers

    def _sentence_excerpt(
        self,
        text: str,
        query_terms: set[str],
        max_tokens: int,
    ) -> str:
        sentences = [part.strip() for part in _SENTENCE_RE.split(text) if part.strip()]
        if not sentences:
            return ""
        ranked = sorted(
            enumerate(sentences),
            key=lambda item: (
                -len(_terms(item[1]) & query_terms),
                -len(_terms(item[1]) & _QUALIFIER_TERMS),
                item[0],
            ),
        )
        chosen: list[tuple[int, str]] = []
        qualifier_sentences = [
            item for item in ranked if _terms(item[1]) & _QUALIFIER_TERMS
        ]
        # A relevant positive sentence without a separate ``unless``/``except``/
        # ``not`` clause can reverse the source's meaning. Admit qualifying
        # sentences first; only then spend remaining budget on other evidence.
        def admit(items: list[tuple[int, str]]) -> None:
            nonlocal chosen
            for index, sentence in items:
                proposed = " ".join(
                    value for _, value in sorted(chosen + [(index, sentence)])
                )
                marker = " […]" if len(chosen) + 1 < len(sentences) else ""
                if self._count(proposed + marker) <= max_tokens:
                    chosen.append((index, sentence))

        admit(qualifier_sentences)
        if len(chosen) == len(qualifier_sentences):
            admit([item for item in ranked if item not in qualifier_sentences])
        if not chosen:
            preferred = qualifier_sentences[0] if qualifier_sentences else ranked[0]
            return self._fit_text(preferred[1], max_tokens=max_tokens)
        excerpt = " ".join(value for _, value in sorted(chosen))
        if len(chosen) < len(sentences):
            marked = f"{excerpt} […]"
            if self._count(marked) <= max_tokens:
                excerpt = marked
        return excerpt

    def _fit_text(
        self,
        text: str,
        *,
        max_tokens: int,
        prefix: str = "",
        total_budget: Optional[int] = None,
    ) -> str:
        if max_tokens <= 0:
            return ""
        required_qualifiers = _terms(text) & _QUALIFIER_TERMS

        def semantically_safe(excerpt: str) -> bool:
            return required_qualifiers.issubset(_terms(excerpt))

        tokens = list(_TOKEN_RE.finditer(text))
        if not tokens:
            return ""
        limit = min(len(tokens), max_tokens)
        while limit > 0:
            end = tokens[limit - 1].end()
            excerpt = text[:end].rstrip()
            if limit < len(tokens) and max_tokens > 1:
                marked = f"{excerpt} […]"
                if self._count(marked) <= max_tokens:
                    excerpt = marked
            within_local = self._count(excerpt) <= max_tokens
            within_total = (
                total_budget is None
                or self._count(f"{prefix}{excerpt}") <= total_budget
            )
            if within_local and within_total and semantically_safe(excerpt):
                return excerpt
            limit -= 1
        # A custom token counter may split a single regex token (for example a
        # character counter or provider tokenizer). In that case there is no
        # shorter regex boundary to try, even though a character prefix fits.
        # Find the longest safe prefix against the declared counter so tight
        # budgets are still used without violating the hard ceiling.
        low, high = 1, len(text)
        best = ""
        while low <= high:
            middle = (low + high) // 2
            excerpt = text[:middle].rstrip()
            if not excerpt:
                low = middle + 1
                continue
            marked = f"{excerpt} […]" if middle < len(text) else excerpt
            candidate = marked if self._count(marked) <= max_tokens else excerpt
            fits = (
                self._count(candidate) <= max_tokens
                and (
                    total_budget is None
                    or self._count(f"{prefix}{candidate}") <= total_budget
                )
            )
            if fits:
                if semantically_safe(candidate):
                    best = candidate
                low = middle + 1
            else:
                high = middle - 1
        return best

    def _header(
        self,
        candidate: Candidate,
        ordinal: int,
        *,
        include_title: bool = True,
    ) -> str:
        record = candidate.record
        if record is None:
            return f"[{ordinal}]"
        # The compact source list carries identity/scope. Repeating ULIDs and
        # scope labels inside the context spends reader tokens without adding
        # evidence; the ordinal is the citation bridge.
        header = f"[{ordinal}]"
        if include_title and record.title:
            title = " ".join(record.title.split())[:120]
            header += f" {title}"
        return header

    def _source_tokens(self, candidate: Candidate) -> int:
        record = candidate.record
        if record is None:
            return 0
        return self._count(f"{record.title}\n{record.content}")

    def _usage(
        self,
        budget: int,
        context_tokens: int,
        source_tokens: int,
        packed_count: int,
        omitted_count: int,
    ) -> ContextUsage:
        saved = max(0, source_tokens - context_tokens)
        ratio = (saved / source_tokens) if source_tokens else 0.0
        return ContextUsage(
            budget_tokens=budget,
            context_tokens=context_tokens,
            source_tokens=source_tokens,
            saved_tokens=saved,
            savings_ratio=ratio,
            packed_count=packed_count,
            omitted_count=max(0, omitted_count),
            token_counter=self.token_counter_identity,
        )


def _terms(text: str) -> set[str]:
    return {match.group(0).casefold() for match in _WORD_RE.finditer(text or "")}


def _starts_with_title(excerpt: str, title: str) -> bool:
    """Whether an excerpt already opens with the exact displayed title text."""
    displayed_title = " ".join((title or "").split())[:120].casefold()
    normalized_excerpt = " ".join((excerpt or "").split()).casefold()
    if not displayed_title or not normalized_excerpt.startswith(displayed_title):
        return False
    return (
        len(normalized_excerpt) == len(displayed_title)
        or not normalized_excerpt[len(displayed_title)].isalnum()
    )


def _family_representatives(
    candidates: list[Candidate],
) -> tuple[list[Candidate], int]:
    """Keep the highest-ranked member of each supersession/consolidation family."""
    parents: dict[str, str] = {}

    def find(value: str) -> str:
        parents.setdefault(value, value)
        while parents[value] != value:
            parents[value] = parents[parents[value]]
            value = parents[value]
        return value

    def union(left: str, right: str) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parents[max(left_root, right_root)] = min(left_root, right_root)

    by_claim: dict[str, str] = {}
    for candidate in candidates:
        find(candidate.id)
        record = candidate.record
        metadata = record.metadata if record and isinstance(record.metadata, dict) else {}
        direct_subject = str(getattr(record, "subject_key", "") or "").strip()
        direct_kind = str(getattr(record, "claim_kind", "") or "").strip()
        if direct_subject:
            claim_identity = f"{direct_subject}\0{direct_kind}"
            prior = by_claim.setdefault(
                f"subject_key:{claim_identity}", candidate.id
            )
            union(candidate.id, prior)
        for field in ("subject_key", "claim_key", "consolidation_family"):
            value = str(metadata.get(field) or "").strip()
            if value:
                if field == "subject_key":
                    # Legacy rows may carry their claim identity solely in metadata.
                    # Preserve independently relevant kinds for the same subject.
                    claim_kind = str(metadata.get("claim_kind") or direct_kind).strip()
                    value = f"{value}\0{claim_kind}"
                prior = by_claim.setdefault(f"{field}:{value}", candidate.id)
                union(candidate.id, prior)
        related = metadata.get("supersedes") or metadata.get("source_ids") or []
        if isinstance(related, str):
            related = [related]
        if isinstance(related, list):
            for item in related:
                if isinstance(item, str) and item:
                    union(candidate.id, item)

    selected: dict[str, Candidate] = {}
    for candidate in candidates:
        root = find(candidate.id)
        current = selected.get(root)
        if current is None or (candidate.score, candidate.id) > (
            current.score,
            current.id,
        ):
            selected[root] = candidate
    representatives = sorted(
        selected.values(), key=lambda candidate: (-candidate.score, candidate.id)
    )
    return representatives, len(candidates) - len(representatives)


def _reverse_text(value: str) -> str:
    # Stable reverse-sort helper without relying on process-randomized hashes.
    return "".join(chr(0x10FFFF - ord(char)) for char in value)


def pack_response_text(
    text: str,
    token_budget: int,
    counter: Optional[Callable[[str], int]] = None,
) -> tuple[str, int]:
    """Truncate free-form response text to fit within *token_budget*.

    Preserves sentence boundaries and qualifier terms when possible; falls
    back to token-boundary truncation.  Returns ``(packed_text, actual_count)``.
    """
    count = counter or RegexTokenCounter()
    text = (text or "").strip()
    if not text or token_budget <= 0:
        return "", 0
    if count(text) <= token_budget:
        return text, count(text)

    tokens = list(_TOKEN_RE.finditer(text))
    if not tokens:
        return "", 0

    # Prefer sentence-aligned truncation when the text has multiple sentences.
    sentences = [part.strip() for part in _SENTENCE_RE.split(text) if part.strip()]
    if len(sentences) > 1:
        built = ""
        for index, sentence in enumerate(sentences):
            proposed = f"{built} {sentence}".strip() if built else sentence
            remaining = index + 1 < len(sentences)
            marked = f"{proposed} […]" if remaining else proposed
            if count(marked if remaining else proposed) <= token_budget:
                built = proposed
            else:
                break
        if built:
            if count(built) < count(text):
                marked = f"{built} […]"
                if count(marked) <= token_budget:
                    built = marked
            return built, count(built)

    # Token-boundary fallback.
    limit = min(len(tokens), token_budget)
    while limit > 0:
        end = tokens[limit - 1].end()
        excerpt = text[:end].rstrip()
        if limit < len(tokens):
            marked = f"{excerpt} […]"
            if count(marked) <= token_budget:
                excerpt = marked
        if count(excerpt) <= token_budget:
            return excerpt, count(excerpt)
        limit -= 1
    return "", 0


def pack_context(
    query: str,
    candidates: list[Candidate],
    token_budget: int,
    *,
    packer: Optional[DeterministicContextPacker] = None,
    **kwargs,
) -> ContextPackResult:
    """Pack budgeted context from candidate memories into a ContextPackResult.

    Convenience functional API wrapping :class:`DeterministicContextPacker`.
    """
    p = packer or DeterministicContextPacker(**kwargs)
    return p.pack(query, candidates, token_budget)
