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
from dataclasses import replace
from typing import NamedTuple, Optional, cast

from engraphis.core.interfaces import (
    Candidate,
    ContextUsage,
    PackedChunk,
)
from engraphis.core.evidence import exact_value_binding


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


def _protected_sentence(text: str) -> bool:
    """Conditions and numerical claims must retain their complete bindings."""
    return bool(_terms(text) & _QUALIFIER_TERMS) or bool(re.search(r"\d", text))


def _normalize_title(title: Optional[str]) -> str:
    """Keep source titles single-line across chunk storage and final rendering."""

    return " ".join(str(title or "").split())


class RegexTokenCounter:
    """Exact counter for Engraphis' dependency-free tokenization contract."""

    identity = "engraphis.regex.v1"

    def __call__(self, text: str) -> int:
        return len(_TOKEN_RE.findall(text or ""))


class DeterministicContextPacker:
    """Pack diverse, relevant evidence into a strict token budget.

    Selection is stable for identical inputs. Repeated memory IDs contribute
    at most one candidate, summaries are preferred when they
    retain query evidence, and oversized sources are reduced at sentence
    boundaries. A complete evidence unit that cannot fit is omitted.
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
        # Keep legacy pruning options accepted for caller compatibility. Shared
        # text across distinct records does not establish equivalent evidence:
        # titles, scope, provenance and neighboring sentences bind its meaning.
        # Only repeated canonical memory IDs deduplicate sources.
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
                    budget, 0, source_tokens, 0, len(candidates),
                    {"budget": len(candidates)} if candidates else {},
                ),
            )

        representatives, duplicate_count = _family_representatives(candidates)
        omissions = {"duplicate": duplicate_count, "budget": 0, "score_tail": 0,
                     "missing_record": 0, "unit_too_large": 0}
        owners = {
            _source_attribution(candidate) for candidate in representatives
            if candidate.record is not None
        }
        include_attribution = len(owners) > 1
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
                omissions["missing_record"] += 1
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
                    omissions["score_tail"] += 1
                    continue

            prefix = "\n\n" if context else ""
            ordinal = len(packed) + 1
            attribution = _source_attribution(candidate) if include_attribution else ""
            header = self._header(candidate, ordinal, attribution=attribution)
            base = f"{context}{prefix}{header}\n"
            excerpt = ""
            truncated = False
            reason = ""
            available = max(0, budget - self._count(base))
            if available:
                excerpt, truncated, reason = self._excerpt(
                    query, candidate, available
                )

            # Keep the established single-pass behavior for ordinary sources.
            # Only retry against the cheaper ordinal-only header when the selected
            # excerpt already starts with the exact displayed title (or the titled
            # header left no room). This removes prompt duplication without deleting
            # evidence or weakening the stable ``[n]`` citation bridge.
            if not excerpt or _starts_with_title(excerpt, record.title):
                compact_base = (
                    f"{context}{prefix}"
                    f"{self._header(candidate, ordinal, include_title=False, attribution=attribution)}\n"
                )
                if self._count(compact_base) < budget:
                    compact_available = budget - self._count(compact_base)
                    compact = self._excerpt(query, candidate, compact_available)
                    if compact[0] and _starts_with_title(compact[0], record.title):
                        base = compact_base
                        available = compact_available
                        excerpt, truncated, reason = compact
            if not excerpt:
                omissions["budget"] += 1
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
                    omissions["budget"] += 1
                    continue
                proposed = f"{base}{excerpt}"

            exact, source_span, evidence_unit_id, evidence_unit = self._evidence_details(
                candidate, excerpt, attribution=attribution,
            )

            context = proposed
            packed.append(PackedChunk(
                id=candidate.id,
                excerpt=excerpt,
                tokens=self._count(excerpt),
                truncated=truncated,
                reason=reason,
                attribution=attribution,
                exact_value=exact,
                source_span=source_span,
                evidence_unit_id=evidence_unit_id,
                evidence_unit=evidence_unit,
            ))
            covered.update(_terms(excerpt) & query_terms)

            # Track admitted scores for subsequent elbow gating.
            admitted_scores.append(float(candidate.score))

        context_tokens = self._count(context)
        omitted = len(candidates) - len(packed)
        # ``duplicate_count`` is intentionally folded into omitted_count; keep
        # the local name to distinguish repeated candidates from missing evidence.
        omitted = max(omitted, duplicate_count)
        return ContextPackResult(
            context=context,
            chunks=packed,
            usage=self._usage(
                budget, context_tokens, source_tokens, len(packed), omitted, omissions
            ),
        )

    pack_context = pack

    def pack_coverage(
        self,
        query: str,
        candidates: list[Candidate],
        token_budget: int,
    ) -> ContextPackResult:
        """Pack compact complete evidence units across more source records.

        The legacy path admits a candidate in score order and may spend most of
        the budget on the first long source.  Coverage mode reserves a bounded
        slice for each still-eligible source, admits one contiguous sentence
        window first, and only then spends remaining space expanding the windows.
        It is opt-in because changing source granularity changes benchmark
        measurements and must be compared with the legacy packer explicitly.
        """
        budget = max(0, int(token_budget))
        source_tokens = sum(self._source_tokens(candidate) for candidate in candidates)
        if budget == 0 or not candidates:
            return ContextPackResult(
                context="", chunks=[],
                usage=self._usage(
                    budget, 0, source_tokens, 0, len(candidates),
                    {"budget": len(candidates)} if candidates else {},
                ),
            )

        representatives, duplicate_count = _family_representatives(candidates)
        omissions = {"duplicate": duplicate_count, "budget": 0, "score_tail": 0,
                     "missing_record": 0, "unit_too_large": 0}
        owners = {
            _source_attribution(candidate) for candidate in representatives
            if candidate.record is not None
        }
        include_attribution = len(owners) > 1
        query_terms = _terms(query)
        needs_bridge = bool(query_terms & _BRIDGE_TERMS) or bool(
            re.search(r"(?:\w+[./\\])+\w+|::|->|\b[A-Za-z_]\w*\(\)", query)
        )
        ordered = self._selection_order(
            representatives, query_terms=query_terms, needs_bridge=needs_bridge,
        )
        remaining = []
        for candidate in ordered:
            if candidate.record is None:
                omissions["missing_record"] += 1
            else:
                remaining.append(candidate)
        context = ""
        packed: list[PackedChunk] = []

        # First pass: reserve a small complete unit for as many distinct sources
        # as can fit.  A source with a long body cannot consume the whole budget.
        while remaining and self._count(context) < budget:
            available_total = budget - self._count(context)
            source_slots = max(1, len(remaining))
            target = max(8, available_total // source_slots)
            separator = "\n\n" if context else ""
            chosen_index: Optional[int] = None
            chosen_excerpt = ""
            chosen_reason = ""
            chosen_exact: Optional[dict[str, object]] = None
            for index, candidate in enumerate(remaining):
                record = candidate.record
                if record is None:
                    continue
                ordinal = len(packed) + 1
                attribution = _source_attribution(candidate) if include_attribution else ""
                header = self._header(candidate, ordinal, attribution=attribution)
                excerpt, reason, exact = self._coverage_excerpt(
                    query, candidate, max_tokens=target,
                    rendered_fits=lambda text: self._count(
                        f"{context}{separator}{header}\n{text}"
                    ) <= budget,
                )
                if not excerpt:
                    continue
                proposed = f"{context}{separator}{header}\n{excerpt}"
                if self._count(proposed) <= budget:
                    chosen_index = index
                    chosen_excerpt = excerpt
                    chosen_reason = reason
                    chosen_exact = exact
                    break
            if chosen_index is None:
                # If the fair first-pass slice is too small, try every remaining
                # source against the actual space. A long top-ranked source must
                # not prevent a later source with a complete unit from fitting.
                for index, candidate in enumerate(remaining):
                    attribution = _source_attribution(candidate) if include_attribution else ""
                    header = self._header(candidate, len(packed) + 1, attribution=attribution)
                    chosen_excerpt, chosen_reason, chosen_exact = self._coverage_excerpt(
                        query, candidate, max_tokens=available_total,
                        rendered_fits=lambda text: self._count(
                            f"{context}{separator}{header}\n{text}"
                        ) <= budget,
                    )
                    proposed = f"{context}{separator}{header}\n{chosen_excerpt}"
                    if chosen_excerpt and self._count(proposed) <= budget:
                        chosen_index = index
                        break
                if chosen_index is None:
                    for candidate in remaining:
                        attribution = _source_attribution(candidate) if include_attribution else ""
                        header = self._header(candidate, len(packed) + 1, attribution=attribution)
                        excerpt, _, _ = self._coverage_excerpt(
                            query, candidate, max_tokens=available_total,
                        )
                        proposed = f"{context}{separator}{header}\n{excerpt}"
                        if not excerpt:
                            omissions["unit_too_large"] += 1
                        elif self._count(proposed) > budget:
                            omissions["budget"] += 1
                    break

            candidate = remaining.pop(chosen_index)
            attribution = _source_attribution(candidate) if include_attribution else ""
            header = self._header(candidate, len(packed) + 1, attribution=attribution)
            prefix = f"{context}{separator}{header}\n"
            context = f"{prefix}{chosen_excerpt}"
            exact, source_span, evidence_unit_id, evidence_unit = self._evidence_details(
                candidate, chosen_excerpt, attribution=attribution, binding=chosen_exact,
            )
            packed.append(PackedChunk(
                id=candidate.id,
                excerpt=chosen_excerpt,
                tokens=self._count(chosen_excerpt),
                truncated=chosen_excerpt.strip() != (
                    (candidate.record.content or candidate.record.summary or "").strip()
                ),
                reason=chosen_reason,
                attribution=attribution,
                exact_value=exact,
                title=_normalize_title(candidate.record.title),
                source_span=source_span,
                evidence_unit_id=evidence_unit_id,
                evidence_unit=evidence_unit,
            ))

        # Second pass: expand each admitted unit in score order using the space
        # left after coverage. Rebuilding the context keeps ordinal/header costs
        # exact for custom provider token counters.
        if packed:
            for index, chunk in enumerate(list(packed)):
                candidate = next((item for item in representatives if item.id == chunk.id), None)
                if candidate is None or candidate.record is None:
                    continue
                record = candidate.record
                current_excerpt = chunk.excerpt
                current_exact = chunk.exact_value
                current_reason = chunk.reason
                expanded, reason, exact = self._coverage_excerpt(
                    query, candidate, max_tokens=budget,
                    minimum_tokens=self._count(current_excerpt),
                    rendered_fits=lambda text: self._count(self._render_packed([
                        replace(item, excerpt=text) if offset == index else item
                        for offset, item in enumerate(packed)
                    ])) <= budget,
                )
                if not expanded or self._count(expanded) <= self._count(current_excerpt):
                    continue
                exact, source_span, evidence_unit_id, evidence_unit = self._evidence_details(
                    candidate, expanded, attribution=chunk.attribution, binding=exact or current_exact,
                )
                trial = list(packed)
                trial[index] = PackedChunk(
                    id=chunk.id, excerpt=expanded, tokens=self._count(expanded),
                    truncated=expanded.strip() != (
                        (record.content or record.summary or "").strip()
                    ), reason=reason or current_reason,
                    attribution=chunk.attribution, exact_value=exact,
                    title=_normalize_title(chunk.title or record.title),
                    source_span=source_span or chunk.source_span,
                    evidence_unit_id=evidence_unit_id or chunk.evidence_unit_id,
                    evidence_unit=evidence_unit or chunk.evidence_unit,
                )
                rendered = self._render_packed(trial)
                if self._count(rendered) <= budget:
                    packed = trial
                    context = rendered

        context = self._render_packed(packed)
        context_tokens = self._count(context)
        if remaining and context_tokens >= budget:
            omissions["budget"] += len(remaining)
        omitted = max(len(candidates) - len(packed), duplicate_count)
        return ContextPackResult(
            context=context,
            chunks=packed,
            usage=self._usage(
                budget, context_tokens, source_tokens, len(packed), omitted, omissions,
            ),
        )

    def _evidence_details(
        self,
        candidate: Candidate,
        excerpt: str,
        *,
        attribution: str = "",
        binding: Optional[dict[str, object]] = None,
    ) -> tuple[
        Optional[dict[str, object]],
        Optional[tuple[int, int]],
        str,
        dict[str, object],
    ]:
        """Return bounded provenance for one packed evidence unit.

        The unit is intentionally derived only from the retrieved record and the
        rendered excerpt.  Gold labels and arbitrary metadata never cross this
        packing boundary.  A literal is advertised as packed only when its exact
        Unicode text is present in the emitted excerpt.
        """
        record = candidate.record
        if record is None:
            return None, None, str(candidate.id), {"id": str(candidate.id), "source_id": str(candidate.id)}
        checked = binding or exact_value_binding(record.metadata, content=record.content)
        exact: Optional[dict[str, object]] = None
        span = None
        value: Optional[str] = None
        if checked is not None:
            raw_value = checked.get("value")
            raw_start = checked.get("start")
            raw_end = checked.get("end")
            if (
                isinstance(raw_value, str)
                and raw_value in excerpt
                and isinstance(raw_start, int)
                and not isinstance(raw_start, bool)
                and isinstance(raw_end, int)
                and not isinstance(raw_end, bool)
            ):
                exact = checked
                span = (raw_start, raw_end)
                value = raw_value
        metadata = record.metadata if isinstance(record.metadata, dict) else {}
        raw_unit_id = metadata.get("evidence_unit_id")
        unit_id = str(raw_unit_id).strip() if isinstance(raw_unit_id, str) else ""
        unit_id = unit_id[:512] or str(candidate.id)
        qualifier_terms = sorted(_terms(excerpt) & _QUALIFIER_TERMS)
        evidence_unit = {
            "id": unit_id,
            "source_id": str(candidate.id),
            "subject_key": str(record.subject_key or ""),
            "claim_kind": str(record.claim_kind or ""),
            "value": value,
            "qualifiers": qualifier_terms,
            "valid_from": record.valid_from,
            "valid_to": record.valid_to,
            "attribution": attribution,
            "source_span": list(span) if span is not None else None,
        }
        return exact, span, unit_id, evidence_unit

    def _coverage_excerpt(
        self,
        query: str,
        candidate: Candidate,
        *,
        max_tokens: int,
        minimum_tokens: int = 0,
        rendered_fits: Optional[Callable[[str], bool]] = None,
    ) -> tuple[str, str, Optional[dict[str, object]]]:
        """Choose a contiguous evidence window, retaining exact-value bindings."""
        record = candidate.record
        if record is None or max_tokens <= 0:
            return "", "", None
        source = record.content or record.summary or ""
        query_terms = _terms(query)
        binding = exact_value_binding(record.metadata, content=record.content)
        exact_value = ""
        if binding and isinstance(binding.get("value"), str):
            exact_value = binding["value"]

        def fits(text: str) -> bool:
            return self._count(text) <= max_tokens and (
                rendered_fits is None or rendered_fits(text)
            )

        # A JSON/string exact value may itself contain line breaks.  Sentence
        # splitting normalizes those separators, so handle the bound source span
        # directly before selecting sentence windows.  The literal remains
        # byte-for-byte copyable and is omitted if it cannot fit in the budget.
        if binding and any(separator in exact_value for separator in ("\n", "\r")):
            if (
                fits(exact_value)
                and self._count(exact_value) >= minimum_tokens
            ):
                return exact_value, "coverage_exact", binding
            return "", "", None
        sentences = [part.strip() for part in _SENTENCE_RE.split(source) if part.strip()]
        if not sentences:
            return "", "", None
        sentence_spans = []
        offset = 0
        for sentence in sentences:
            start = source.index(sentence, offset)
            offset = start + len(sentence)
            sentence_spans.append((start, offset))
        ranked = sorted(
            range(len(sentences)),
            key=lambda index: (
                -(len(_terms(sentences[index]) & query_terms)
                  + (4 if exact_value and exact_value in sentences[index] else 0)
                  + (2 if _terms(sentences[index]) & _QUALIFIER_TERMS else 0)),
                index,
            ),
        )
        best = ""
        best_score = -1
        best_reason = "coverage_unit"
        # A short adjacent window keeps subject, value, condition and date in
        # the same evidence unit without falling back to arbitrary token prefixes.
        for seed in ranked:
            for width in (3, 2, 1):
                start = max(0, min(seed, len(sentences) - width))
                end = start + width
                window_spans = sentence_spans[start:end]
                if binding and not (
                    window_spans[0][0] <= cast(int, binding["start"])
                    and cast(int, binding["end"]) <= window_spans[-1][1]
                ):
                    continue
                excerpt = " ".join(sentences[start:end])
                if exact_value and exact_value in source and exact_value not in excerpt:
                    continue
                if not fits(excerpt) or self._count(excerpt) < minimum_tokens:
                    continue
                score = sum(len(_terms(part) & query_terms) for part in sentences[start:end])
                score += sum(2 for part in sentences[start:end] if _terms(part) & _QUALIFIER_TERMS)
                if exact_value and exact_value in excerpt:
                    score += 8
                if score > best_score:
                    best, best_score = excerpt, score
                    best_reason = "coverage_exact" if exact_value and exact_value in excerpt else "coverage_unit"
        if not best and binding and exact_value and fits(exact_value):
            # An oversized sentence must not hide a small verified literal.
            # Grow a verbatim source window from its bound coordinates, choosing
            # nearby qualifier/query tokens first and balancing both sides.
            raw_source = record.content
            left, right = cast(int, binding["start"]), cast(int, binding["end"])
            before = list(_TOKEN_RE.finditer(raw_source, 0, left))
            after = list(_TOKEN_RE.finditer(raw_source, right))
            taken = [0, 0]
            best = raw_source[left:right]
            while True:
                options = []
                for side, matches in enumerate((before, after)):
                    if taken[side] >= len(matches):
                        continue
                    token = matches[-taken[side] - 1] if side == 0 else matches[taken[side]]
                    start, end = (token.start(), right) if side == 0 else (left, token.end())
                    excerpt = raw_source[start:end]
                    if fits(excerpt):
                        terms = _terms(token.group())
                        rank = (2 * len(terms & _QUALIFIER_TERMS) + len(terms & query_terms),
                                -taken[side], -side)
                        options.append((rank, side, start, end, excerpt))
                if not options:
                    break
                _, side, left, right, best = max(options)
                taken[side] += 1
            best_reason = "coverage_exact"
        if not best or self._count(best) < minimum_tokens:
            return "", "", None
        # Make the literal independently visible to the reader. This is only in
        # the opt-in coverage path and is charged to the same hard token budget.
        if binding and exact_value and exact_value not in best:
            line = f"Exact value (copy exactly): {exact_value}"
            proposed = f"{line}\n{best}"
            if fits(proposed):
                best = proposed
                best_reason = "coverage_exact"
            else:
                return "", "", None
        return best, best_reason, binding

    def _render_packed(self, chunks: list[PackedChunk]) -> str:
        parts = []
        for ordinal, chunk in enumerate(chunks, start=1):
            attribution = f" {chunk.attribution}" if chunk.attribution else ""
            displayed_title = _normalize_title(chunk.title)
            title = f" {displayed_title}" if displayed_title else ""
            parts.append(f"[{ordinal}]{attribution}{title}\n{chunk.excerpt}")
        return "\n\n".join(parts)

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
        source_sentences = {
            part.strip() for part in _SENTENCE_RE.split(full) if part.strip()
        }
        summary_sentences = {
            part.strip() for part in _SENTENCE_RE.split(summary)
            if part.strip() and part.strip() != "[…]"
        }
        # A summary's shared vocabulary is not proof of source entailment.
        # Admit extractive sentences only, preserving complete conditions and
        # numerical claims rather than merely their qualifier/value tokens.
        if not summary_sentences or not summary_sentences.issubset(source_sentences):
            return False
        protected = {part for part in source_sentences if _protected_sentence(part)}
        if not protected.issubset(summary_sentences):
            return False
        full_overlap = _terms(full) & query_terms
        summary_terms = _terms(summary)
        preserves_query = not full_overlap or bool(summary_terms & full_overlap)
        return preserves_query

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
        # Neither a token nor a character prefix proves a complete claim. An
        # English qualifier list cannot protect French, Chinese, identifiers,
        # or a value/scope at the end of a sentence. Sentence selection happens
        # before this fallback; here the whole selected evidence unit fits or
        # is omitted, including with context-sensitive custom token counters.
        text = text.strip()
        if self._count(text) > max_tokens:
            return ""
        if total_budget is not None and self._count(f"{prefix}{text}") > total_budget:
            return ""
        return text

    def _header(
        self,
        candidate: Candidate,
        ordinal: int,
        *,
        include_title: bool = True,
        attribution: str = "",
    ) -> str:
        record = candidate.record
        if record is None:
            return f"[{ordinal}]"
        # Ownership binds otherwise identical claims from different scopes. Include
        # it when the context spans owners, and charge it to the same hard budget.
        header = f"[{ordinal}]"
        if attribution:
            header += f" {attribution}"
        if include_title and record.title:
            title = " ".join(record.title.split())
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
        omission_reasons: Optional[dict[str, int]] = None,
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
            omission_reasons=omission_reasons or {},
        )


def _terms(text: str) -> set[str]:
    return {match.group(0).casefold() for match in _WORD_RE.finditer(text or "")}


def _starts_with_title(excerpt: str, title: str) -> bool:
    """Whether an excerpt already opens with the exact displayed title text."""
    displayed_title = " ".join((title or "").split())
    normalized_excerpt = " ".join((excerpt or "").split())
    if not displayed_title or not normalized_excerpt.startswith(displayed_title):
        return False
    return (
        len(normalized_excerpt) == len(displayed_title)
        or not normalized_excerpt[len(displayed_title)].isalnum()
    )


def _family_representatives(
    candidates: list[Candidate],
) -> tuple[list[Candidate], int]:
    """Collapse repeated candidates, never distinct canonical records.

    Claim keys and consolidation lineage do not prove equal evidence or ownership.
    Store visibility owns supersession: the packer cannot reinterpret historical
    reads without their temporal filter, nor assume a digest covers its sources.
    """
    selected: dict[str, Candidate] = {}
    for candidate in candidates:
        current = selected.get(candidate.id)
        if current is None or (candidate.score, candidate.id) > (
            current.score,
            current.id,
        ):
            selected[candidate.id] = candidate
    representatives = sorted(
        selected.values(), key=lambda candidate: (-candidate.score, candidate.id)
    )
    return representatives, len(candidates) - len(representatives)


def _source_attribution(candidate: Candidate) -> str:
    record = candidate.record
    if record is None:
        return ""
    scope = getattr(record.scope, "value", record.scope)
    fields = [f"scope={scope}"]
    for name in ("workspace_id", "repo_id", "session_id"):
        value = getattr(record, name)
        if value is not None:
            fields.append(f"{name}={value}")
    return "(" + "; ".join(fields) + ")"


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
