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
from engraphis.core.query_planner import MAX_PLANNED_PRIORITY


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
                    "maxItems": 3,
                    "items": {
                        "type": "object",
                        "required": ["text", "priority", "profile"],
                        "properties": {
                            "text": {"type": "string"},
                            "priority": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": MAX_PLANNED_PRIORITY,
                            },
                            "profile": {
                                "type": "string",
                                "enum": ["balanced", "lexical", "graph", "code"],
                            },
                            "mtypes": {
                                "type": "array",
                                "items": {"enum": [item.value for item in MemoryType]},
                            },
                        },
                    },
                },
                "mtype_limits": {"type": "object"},
                "reason_codes": {"type": "array", "items": {"type": "string"}},
            },
        }
        prompt = (
            "Plan memory retrieval for the query below. Keep the original query first "
            "with priority 1. Add no more than two distinct queries. Use only balanced, "
            "lexical, graph, or code profiles. Type limits are maxima, not boosts.\n\n"
            f"QUERY:\n{query}"
        )
        kwargs = {"timeout": timeout_s} if timeout_s is not None else {}
        raw = self.llm.extract_json(prompt, schema, **kwargs)
        if not isinstance(raw, dict):
            raise ValueError("planner output must be an object")
        queries = []
        for item in raw.get("queries", []):
            if not isinstance(item, dict):
                continue
            queries.append(PlannedQuery(
                text=str(item.get("text") or ""),
                priority=item.get("priority", 1),
                profile=str(item.get("profile") or "balanced"),
                mtypes=tuple(MemoryType(value) for value in item.get("mtypes", [])),
            ))
        limits = {
            MemoryType(key): value
            for key, value in (raw.get("mtype_limits") or {}).items()
        }
        reasons = tuple(str(value) for value in raw.get("reason_codes", []))
        return RetrievalPlan(tuple(queries), limits, reasons)
