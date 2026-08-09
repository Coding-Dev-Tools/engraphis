"""Optional LLM-backed query planning.

This module is outside ``core`` by design. The caller injects any object satisfying
the core ``LLM`` protocol; no provider SDK is a required dependency.
"""
from __future__ import annotations

from typing import Optional

from engraphis.core.interfaces import (
    LLM,
    MemoryType,
    PlannedQuery,
    RetrievalPlan,
    SearchFilter,
)
from engraphis.core.query_planner import MAX_PLANNED_PRIORITY, MAX_PLANNED_QUERIES


_PLANNING_PROFILES = frozenset({"balanced", "fast", "lexical", "graph", "code"})
_MAX_PLANNED_QUERY_CHARS = 2_048
_MAX_REASON_CODES = 8
_MAX_REASON_CODE_CHARS = 80


class LLMQueryPlanner:
    """Ask an injected LLM for a bounded structured retrieval plan."""

    identity = "engraphis.query-planner.llm.v1"

    def __init__(self, llm: LLM) -> None:
        self.llm = llm

    def plan(
        self,
        query: str,
        *,
        filter: Optional[SearchFilter] = None,
        timeout_s: Optional[float] = None,
    ) -> RetrievalPlan:
        del filter
        schema = {
            "type": "object",
            "required": ["queries"],
            "properties": {
                "queries": {
                    "type": "array",
                    "maxItems": MAX_PLANNED_QUERIES,
                    "items": {
                        "type": "object",
                        "required": ["text", "priority", "profile"],
                        "properties": {
                            "text": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": _MAX_PLANNED_QUERY_CHARS,
                            },
                            "priority": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": MAX_PLANNED_PRIORITY,
                            },
                            "profile": {
                                "type": "string",
                                "enum": ["balanced", "fast", "lexical", "graph", "code"],
                            },
                            "mtypes": {
                                "type": "array",
                                "items": {"enum": [item.value for item in MemoryType]},
                                "maxItems": len(MemoryType),
                                "uniqueItems": True,
                            },
                        },
                    },
                },
                "mtype_limits": {
                    "type": "object",
                    "maxProperties": len(MemoryType),
                    "additionalProperties": {"type": "integer", "minimum": 0},
                },
                "reason_codes": {
                    "type": "array",
                    "maxItems": _MAX_REASON_CODES,
                    "items": {"type": "string", "maxLength": _MAX_REASON_CODE_CHARS},
                },
            },
        }
        prompt = (
            "Plan memory retrieval for the query below. Keep the original query first "
            "with priority 1. Add no more than two distinct queries. Use only balanced, "
            "fast, lexical, graph, or code profiles. Type limits are maxima, not boosts.\n\n"
            f"QUERY:\n{query}"
        )
        kwargs = {"timeout": timeout_s} if timeout_s is not None else {}
        raw = self.llm.extract_json(prompt, schema, **kwargs)
        if not isinstance(raw, dict):
            raise ValueError("planner output must be an object")
        raw_queries = raw.get("queries", [])
        if not isinstance(raw_queries, list) or len(raw_queries) > MAX_PLANNED_QUERIES:
            raise ValueError("planner queries must be a bounded array")
        queries = []
        for item in raw_queries:
            if not isinstance(item, dict):
                raise ValueError("planner query entries must be objects")
            text = item.get("text")
            priority = item.get("priority", 1)
            profile = item.get("profile", "balanced")
            raw_mtypes = item.get("mtypes", [])
            if (
                not isinstance(text, str)
                or not text.strip()
                or len(text) > _MAX_PLANNED_QUERY_CHARS
            ):
                raise ValueError("planner query text must be a bounded string")
            if isinstance(priority, bool) or not isinstance(priority, int):
                raise ValueError("planner query priority must be an integer")
            if not 1 <= priority <= MAX_PLANNED_PRIORITY:
                raise ValueError("planner query priority is outside the supported range")
            if not isinstance(profile, str) or profile not in _PLANNING_PROFILES:
                raise ValueError("planner query profile is unsupported")
            if not isinstance(raw_mtypes, list) or len(raw_mtypes) > len(MemoryType):
                raise ValueError("planner memory types must be a bounded array")
            queries.append(PlannedQuery(
                text=text,
                priority=priority,
                profile=profile,
                mtypes=tuple(MemoryType(value) for value in raw_mtypes),
            ))

        raw_limits = raw.get("mtype_limits", {})
        if raw_limits is None:
            raw_limits = {}
        if not isinstance(raw_limits, dict) or len(raw_limits) > len(MemoryType):
            raise ValueError("planner memory-type limits must be a bounded object")
        limits = {}
        for key, value in raw_limits.items():
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("planner memory-type limits must be non-negative integers")
            limits[MemoryType(key)] = value

        raw_reasons = raw.get("reason_codes", [])
        if raw_reasons is None:
            raw_reasons = []
        if not isinstance(raw_reasons, list) or len(raw_reasons) > _MAX_REASON_CODES:
            raise ValueError("planner reason codes must be a bounded array")
        if any(
            not isinstance(value, str) or len(value) > _MAX_REASON_CODE_CHARS
            for value in raw_reasons
        ):
            raise ValueError("planner reason codes must be bounded strings")
        return RetrievalPlan(tuple(queries), limits, tuple(raw_reasons))
