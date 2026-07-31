"""Deterministic, token-budgeted context packing.

The default packer deliberately has no model or tokenizer dependency.  It uses a
small, named regex tokenizer so its accounting is exact for the counter it
declares, reproducible offline, and replaceable by benchmark/provider-specific
token counters at the composition boundary.
"""
from __future__ import annotations

import math
import re
from collections.abc import Callable
from typing import Optional

from engraphis.core.interfaces import (
    Candidate,
    ContextUsage,
    PackedChunk,
)


_TOKEN_RE = re.compile(r"\w+|[^\w\s]", re.UNICODE)
_SENTENCE_RE = re.compile(r"(?<=[.!?])(?:[\"')\]]*)\s+|\n+")
_WORD_RE = re.compile(r"\w+", re.UNICODE)
_BRIDGE_TERMS = frozenset({
    "call", "calls", "called", "caller", "dependency", "depends", "flow",
    "graph", "impact", "path", "related", "relationship", "why",
})
_QUALIFIER_TERMS = frozenset({
    "cannot", "except", "if", "must", "never", "no", "not", "only",
    "unless", "until", "when", "without",
})


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
    ) -> None:
        self._count = token_counter or RegexTokenCounter()
        self.token_counter_identity = (
            token_counter_identity
            or getattr(self._count, "identity", None)
            or getattr(self._count, "__name__", None)
            or type(self._count).__name__
        )

    def pack(
        self,
        query: str,
        candidates: list[Candidate],
        token_budget: int,
    ) -> tuple[str, list[PackedChunk], ContextUsage]:
        budget = max(0, int(token_budget))
        source_tokens = sum(self._source_tokens(candidate) for candidate in candidates)
        if budget == 0 or not candidates:
            return "", [], self._usage(
                budget, 0, source_tokens, 0, len(candidates)
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

            prefix = "\n\n" if context else ""
            header = self._header(candidate, len(packed) + 1)
            base = f"{context}{prefix}{header}\n"
            if self._count(base) >= budget:
                continue

            available = budget - self._count(base)
            excerpt, truncated, reason = self._excerpt(
                query, candidate, available
            )
            if not excerpt:
                continue
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

        context_tokens = self._count(context)
        omitted = len(candidates) - len(packed)
        # ``duplicate_count`` is intentionally folded into omitted_count; keep
        # the local name to make the family-diversity policy explicit.
        omitted = max(omitted, duplicate_count)
        return context, packed, self._usage(
            budget, context_tokens, source_tokens, len(packed), omitted
        )

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

    def _header(self, candidate: Candidate, ordinal: int) -> str:
        record = candidate.record
        if record is None:
            return f"[{ordinal}]"
        # The compact source list carries identity/scope. Repeating ULIDs and
        # scope labels inside the context spends reader tokens without adding
        # evidence; the ordinal is the citation bridge.
        header = f"[{ordinal}]"
        if record.title:
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
