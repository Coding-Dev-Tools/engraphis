"""Deterministic eval for session handoff effectiveness.

Measures whether context surfaced at session start contains the complete evidence needed by
the first queries of the next session. The fixture deliberately places more records than the
selection cutoff and makes recency-only, proactive, and reversed-proactive selections diverge.
Runs entirely offline with deterministic fixtures; no API keys are required.

Strategies compared:
  - last_n_memories:    top-k memories by ingestion recency only
  - proactive_ranking:  top-k memories by score_proactive
  - consolidated_summary: session summary + open threads (no individual memories)

Usage:
    python -m eval.handoff_quality
"""
from __future__ import annotations

import json
import math
from pathlib import Path

from engraphis.core import scoring
from engraphis.core.interfaces import MemoryRecord, MemoryType, Scope

DATASET = Path(__file__).with_name("datasets") / "handoff_quality.jsonl"
NOW = 1_700_000_000.0
STRATEGIES = ("last_n_memories", "proactive_ranking", "consolidated_summary")
DEFAULT_K = 5
QUALITY_FLOOR = 0.8
_REVERSED_STRATEGY = "reversed_proactive"


def load_cases(path: Path = DATASET) -> list[dict]:
    """Load and validate the rank-discriminating handoff fixture."""
    cases = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        case = json.loads(line)
        if not isinstance(case.get("id"), str) or not case["id"].strip():
            raise ValueError(f"invalid handoff case on line {line_number}: missing id")
        memories = case.get("session_1_memories")
        if not isinstance(memories, list) or len(memories) <= DEFAULT_K:
            raise ValueError(
                f"case {case['id']}: session_1_memories must contain more than {DEFAULT_K} items"
            )
        memory_ids = set()
        for i, spec in enumerate(memories):
            if not isinstance(spec, dict) or not isinstance(spec.get("text"), str):
                raise ValueError(f"case {case['id']} memory {i}: needs text")
            memory_id = spec.get("id")
            if not isinstance(memory_id, str) or not memory_id.startswith("mem_"):
                raise ValueError(f"case {case['id']} memory {i}: needs a typed id")
            if memory_id in memory_ids:
                raise ValueError(f"case {case['id']}: duplicate memory id {memory_id}")
            memory_ids.add(memory_id)
            try:
                importance = float(spec["importance"])
                age_hours = float(spec["age_hours"])
                stability_days = float(spec["stability_days"])
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                raise ValueError(
                    f"case {case['id']} memory {i}: invalid ranking fields"
                ) from exc
            if (
                not math.isfinite(importance)
                or not 0.0 <= importance <= 1.0
                or not math.isfinite(age_hours)
                or age_hours < 0.0
                or not math.isfinite(stability_days)
                or stability_days <= 0.0
            ):
                raise ValueError(f"case {case['id']} memory {i}: ranking fields out of range")

        queries = case.get("session_2_queries")
        if not isinstance(queries, list) or not queries:
            raise ValueError(f"case {case['id']}: session_2_queries must be non-empty list")
        query_ids = set()
        for i, query in enumerate(queries):
            keywords = query.get("supporting_keywords") if isinstance(query, dict) else None
            evidence_ids = query.get("evidence_memory_ids") if isinstance(query, dict) else None
            query_id = query.get("id") if isinstance(query, dict) else None
            if (
                not isinstance(query_id, str)
                or not query_id
                or query_id in query_ids
                or not isinstance(query.get("q"), str)
                or not isinstance(keywords, list)
                or not keywords
                or not all(isinstance(keyword, str) and keyword for keyword in keywords)
                or not isinstance(evidence_ids, list)
                or not evidence_ids
                or not all(isinstance(memory_id, str) for memory_id in evidence_ids)
                or not set(evidence_ids) <= memory_ids
            ):
                raise ValueError(f"case {case['id']} query {i}: invalid evidence contract")
            query_ids.add(query_id)
        cases.append(case)
    if not cases:
        raise ValueError("handoff quality fixture is empty")
    return cases


def _build_record(spec: dict) -> MemoryRecord:
    """Convert one explicit fixture record into a deterministic MemoryRecord."""
    timestamp = NOW - float(spec["age_hours"]) * 3600.0
    return MemoryRecord(
        id=str(spec["id"]),
        content=str(spec["text"]),
        workspace_id="eval",
        scope=Scope.WORKSPACE,
        mtype=MemoryType.SEMANTIC,
        importance=float(spec["importance"]),
        stability=float(spec["stability_days"]),
        ingested_at=timestamp,
        last_access=timestamp,
    )


def _context_contains_evidence(context_text: str, keywords: list[str]) -> bool:
    """Require every evidence-specific keyword, not one weak/common-token match."""
    if not keywords:
        return False
    lower_context = context_text.casefold()
    return all(keyword.casefold() in lower_context for keyword in keywords)


def _select_last_n(records: list[MemoryRecord], k: int) -> list[MemoryRecord]:
    """Select the k most recently ingested memories."""
    return sorted(records, key=lambda record: (-(record.ingested_at or 0.0), record.id))[:k]


def _select_proactive(
        records: list[MemoryRecord], k: int, *, reverse: bool = False) -> list[MemoryRecord]:
    """Select top-k proactive records, or the deliberately worst records for the design check."""
    scored = [(scoring.score_proactive(record, now=NOW), record) for record in records]
    if reverse:
        scored.sort(key=lambda item: (item[0], item[1].id))
    else:
        scored.sort(key=lambda item: (-item[0], item[1].id))
    return [record for _, record in scored[:k]]


def _strategy_consolidated(case: dict) -> str:
    """Return the session summary plus open threads as the handoff context."""
    parts = []
    summary = case.get("session_1_summary", "")
    if summary:
        parts.append(summary)
    threads = case.get("session_1_open_threads", [])
    if threads:
        parts.append("Open threads: " + "; ".join(threads))
    return "\n".join(parts)


def evaluate_case(case: dict, strategy: str, k: int = DEFAULT_K) -> dict:
    """Evaluate one transition and report both evidence coverage and selected memory IDs."""
    records = [_build_record(spec) for spec in case["session_1_memories"]]
    selected: list[MemoryRecord]
    if strategy == "last_n_memories":
        selected = _select_last_n(records, k)
        context = "\n".join(record.content for record in selected)
    elif strategy == "proactive_ranking":
        selected = _select_proactive(records, k)
        context = "\n".join(record.content for record in selected)
    elif strategy == _REVERSED_STRATEGY:
        selected = _select_proactive(records, k, reverse=True)
        context = "\n".join(record.content for record in selected)
    elif strategy == "consolidated_summary":
        selected = []
        context = _strategy_consolidated(case)
    else:
        raise ValueError(f"unknown strategy: {strategy}")

    queries = case["session_2_queries"][:5]
    results = []
    for query in queries:
        satisfied = _context_contains_evidence(context, query["supporting_keywords"])
        results.append({
            "query_id": query["id"],
            "query": query["q"],
            "satisfied": satisfied,
        })

    hits = sum(1 for result in results if result["satisfied"])
    return {
        "case_id": case["id"],
        "strategy": strategy,
        "selected_ids": [record.id for record in selected],
        "satisfaction_rate": hits / len(results) if results else 0.0,
        "hits": hits,
        "total": len(results),
        "queries": results,
    }


def _evaluate_cases(cases: list[dict], strategy: str, k: int) -> dict:
    case_results = [evaluate_case(case, strategy, k) for case in cases]
    total_hits = sum(result["hits"] for result in case_results)
    total_queries = sum(result["total"] for result in case_results)
    return {
        "strategy": strategy,
        "satisfaction_rate": total_hits / total_queries if total_queries else 0.0,
        "total_hits": total_hits,
        "total_queries": total_queries,
        "cases": len(case_results),
        "per_case": case_results,
    }


def evaluate(strategy: str, k: int = DEFAULT_K) -> dict:
    """Run the handoff quality eval across all cases for one strategy."""
    return _evaluate_cases(load_cases(), strategy, k)


def _validate_discrimination(cases: list[dict], results: dict, k: int) -> dict:
    """Fail if the fixture cannot detect broken, reversed, or recency-only ranking."""
    reversed_result = _evaluate_cases(cases, _REVERSED_STRATEGY, k)
    checks = []
    by_strategy = {
        strategy: {result["case_id"]: result for result in results[strategy]["per_case"]}
        for strategy in ("last_n_memories", "proactive_ranking")
    }
    reversed_cases = {result["case_id"]: result for result in reversed_result["per_case"]}
    for case in cases:
        case_id = case["id"]
        recent_ids = set(by_strategy["last_n_memories"][case_id]["selected_ids"])
        proactive_ids = set(by_strategy["proactive_ranking"][case_id]["selected_ids"])
        relevant_ids = {
            memory_id
            for query in case["session_2_queries"][:5]
            for memory_id in query["evidence_memory_ids"]
        }
        if recent_ids == proactive_ids:
            raise ValueError(f"case {case_id}: recency and proactive select identical IDs")
        if not (proactive_ids - recent_ids) & relevant_ids:
            raise ValueError(f"case {case_id}: proactive adds no relevant record above cutoff")
        if not (recent_ids - proactive_ids) - relevant_ids:
            raise ValueError(f"case {case_id}: recency adds no distractor above cutoff")
        checks.append({
            "case_id": case_id,
            "last_n_selected_ids": sorted(recent_ids),
            "proactive_selected_ids": sorted(proactive_ids),
            "reversed_selected_ids": reversed_cases[case_id]["selected_ids"],
        })

    recent_rate = results["last_n_memories"]["satisfaction_rate"]
    proactive_rate = results["proactive_ranking"]["satisfaction_rate"]
    summary_rate = results["consolidated_summary"]["satisfaction_rate"]
    reversed_rate = reversed_result["satisfaction_rate"]
    if proactive_rate <= recent_rate:
        raise ValueError("proactive ranking does not beat recency-only selection")
    if summary_rate <= recent_rate:
        raise ValueError("consolidated handoff does not beat recency-only selection")
    if proactive_rate < QUALITY_FLOOR:
        raise ValueError("proactive ranking misses the quality floor")
    if reversed_rate >= QUALITY_FLOOR:
        raise ValueError("reversed proactive ranking incorrectly passes the quality floor")
    return {
        "quality_floor": QUALITY_FLOOR,
        "reversed_satisfaction_rate": reversed_rate,
        "per_case": checks,
    }


def run(k: int = DEFAULT_K) -> dict:
    """Evaluate all strategies and enforce that the fixture discriminates their rankings."""
    cases = load_cases()
    results = {
        strategy: _evaluate_cases(cases, strategy, k)
        for strategy in STRATEGIES
    }
    results["design_checks"] = _validate_discrimination(cases, results, k)
    return results


def main() -> None:
    report = run()
    print("Engraphis handoff-quality eval")
    print(f"  Fixture: {len(load_cases())} session transitions, first-5 queries each\n")
    for strategy in STRATEGIES:
        result = report[strategy]
        print(f"  {strategy:24s}  satisfaction={result['satisfaction_rate']:.3f} "
              f"({result['total_hits']}/{result['total_queries']} queries, "
              f"{result['cases']} cases)")
        for case in result["per_case"]:
            selected = ", ".join(case["selected_ids"]) or "summary + open threads"
            print(f"    {case['case_id']}: selected=[{selected}]")
    checks = report["design_checks"]
    print(f"\n  reversed_proactive        satisfaction="
          f"{checks['reversed_satisfaction_rate']:.3f}")
    print("\n  quality gate: PASS (ranked and consolidated context beat recency-only; "
          "reversed ranking stays below the floor)")


if __name__ == "__main__":
    main()
