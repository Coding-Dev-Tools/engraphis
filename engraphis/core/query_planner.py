"""Bounded query planning for opt-in planned recall.

The deterministic planner is deliberately conservative and dependency-free. It
does not retrieve data or relax filters; it only proposes at most two additional
query formulations and optional memory-type targeting. Recall sanitizes every
plan again before execution, so an injected planner is never a policy boundary.
"""
from __future__ import annotations

import re
from typing import Optional

from engraphis.core.interfaces import (
    MemoryType,
    PlannedQuery,
    RetrievalPlan,
    SearchFilter,
)


PLANNING_MODES = frozenset({"off", "auto"})
MAX_PLANNED_QUERIES = 3
MAX_PLANNED_PRIORITY = 1000

_QUOTED_RE = re.compile(r'"([^"\r\n]{1,160})"|\'([^\'\r\n]{1,160})\'')
_IDENTIFIER_RE = re.compile(
    r"(?:\b[A-Z][A-Z0-9_]{2,}\b|\b[A-Za-z_]\w*(?:::\w+|\.\w+|\(\))+)"
)
_GRAPH_RE = re.compile(
    r"\b(?:calls?|causes?|depends?|impact|path|related|relationship|why|between)\b",
    re.IGNORECASE,
)
_TEMPORAL_RE = re.compile(
    r"\b(?:before|after|changed|change|current|currently|latest|now|previous|"
    r"previously|supersed(?:e|ed|es)|timeline|when)\b",
    re.IGNORECASE,
)
_PROCEDURAL_RE = re.compile(
    r"\b(?:how\s+(?:do|does|should|to)|procedure|process|steps?|workflow|playbook|recipe)\b",
    re.IGNORECASE,
)
_SESSION_RE = re.compile(
    r"\b(?:current|this)\s+(?:chat|conversation|session|task|thread)\b|"
    r"\b(?:just|earlier)\s+(?:said|discussed|decided)\b",
    re.IGNORECASE,
)
_GRAPH_STOPWORDS = frozenset({
    "a", "an", "and", "are", "between", "does", "how", "is", "of", "the",
    "to", "what", "which", "who", "why",
})


class DeterministicQueryPlanner:
    """Offline planner with stable regex-based rules and no model dependency."""

    identity = "engraphis.query-planner.deterministic.v1"

    def plan(
        self,
        query: str,
        *,
        filter: Optional[SearchFilter] = None,
        timeout_s: Optional[float] = None,
    ) -> RetrievalPlan:
        del filter, timeout_s
        text = " ".join(str(query or "").split())
        mtypes, type_reason = _intent_mtypes(text)
        # The original query remains broad. Type intent narrows only an additional
        # route, so a mistaken intent classification cannot remove relevant evidence.
        planned = [PlannedQuery(text=text, priority=1, profile="balanced")]
        reasons = [type_reason] if type_reason else []

        exact_terms = []
        for match in _QUOTED_RE.finditer(text):
            value = next((group for group in match.groups() if group), "").strip()
            if value and value.casefold() not in {term.casefold() for term in exact_terms}:
                exact_terms.append(value)
        for value in _IDENTIFIER_RE.findall(text):
            value = value.strip()
            if value and value.casefold() not in {term.casefold() for term in exact_terms}:
                exact_terms.append(value)
        if exact_terms:
            planned.append(PlannedQuery(
                text=" ".join(exact_terms[:6]),
                priority=2,
                profile="lexical",
                mtypes=mtypes,
            ))
            reasons.append("exact_term")

        if _GRAPH_RE.search(text) and len(planned) < MAX_PLANNED_QUERIES:
            graph_text = _graph_query(text)
            if graph_text.casefold() != text.casefold():
                planned.append(PlannedQuery(
                    text=graph_text,
                    priority=len(planned) + 1,
                    profile="graph",
                    mtypes=mtypes,
                ))
                reasons.append("relationship_intent")

        if mtypes and len(planned) < MAX_PLANNED_QUERIES:
            suffix = {
                "current_session_intent": "current session",
                "procedural_intent": "procedure steps",
                "temporal_intent": "timeline changes",
            }[type_reason]
            planned.append(PlannedQuery(
                text=f"{text} {suffix}",
                priority=len(planned) + 1,
                profile="balanced",
                mtypes=mtypes,
            ))

        return RetrievalPlan(
            queries=tuple(planned[:MAX_PLANNED_QUERIES]),
            reason_codes=tuple(reasons),
        )


def _intent_mtypes(query: str) -> tuple[tuple[MemoryType, ...], str]:
    if _SESSION_RE.search(query):
        return (MemoryType.WORKING, MemoryType.EPISODIC), "current_session_intent"
    if _PROCEDURAL_RE.search(query):
        return (MemoryType.PROCEDURAL, MemoryType.SEMANTIC), "procedural_intent"
    if _TEMPORAL_RE.search(query):
        return (MemoryType.EPISODIC, MemoryType.SEMANTIC), "temporal_intent"
    return (), ""


def _graph_query(query: str) -> str:
    terms = [
        term
        for term in re.findall(r"[A-Za-z0-9_./:-]+", query)
        if term.casefold() not in _GRAPH_STOPWORDS
    ]
    return " ".join(terms[:16]) or query
