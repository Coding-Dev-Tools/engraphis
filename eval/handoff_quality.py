"""Deterministic eval for session handoff effectiveness.

Measures whether the context surfaced at session start (via proactive recall)
actually contains evidence relevant to the first queries of the next session.
Runs entirely offline with deterministic fixtures — no API keys required.

Strategies compared:
  - last_n_memories:    top-k memories by ingestion recency only
  - proactive_ranking:  score_proactive (importance × retention + recency)
  - consolidated_summary: session summary + open threads (no individual memories)

Usage:
    python -m eval.handoff_quality
"""
from __future__ import annotations

import json
from pathlib import Path

from engraphis.core import scoring
from engraphis.core.interfaces import MemoryRecord, MemoryType, Scope

DATASET = Path(__file__).with_name("datasets") / "handoff_quality.jsonl"
NOW = 1_700_000_000.0
STRATEGIES = ("last_n_memories", "proactive_ranking", "consolidated_summary")
DEFAULT_K = 5


def load_cases(path: Path = DATASET) -> list[dict]:
    """Load and validate the handoff quality fixture."""
    cases = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        case = json.loads(line)
        if not isinstance(case.get("id"), str):
            raise ValueError(f"invalid handoff case on line {line_number}: missing id")
        memories = case.get("session_1_memories")
        if not isinstance(memories, list) or not memories:
            raise ValueError(f"case {case['id']}: session_1_memories must be non-empty list")
        queries = case.get("session_2_queries")
        if not isinstance(queries, list) or not queries:
            raise ValueError(f"case {case['id']}: session_2_queries must be non-empty list")
        for i, q in enumerate(queries):
            if not isinstance(q.get("q"), str) or not isinstance(q.get("supporting_keywords"), list):
                raise ValueError(f"case {case['id']} query {i}: needs 'q' and 'supporting_keywords'")
        cases.append(case)
    if not cases:
        raise ValueError("handoff quality fixture is empty")
    return cases


def _build_record(spec: dict, index: int) -> MemoryRecord:
    """Convert a fixture memory spec into a MemoryRecord with deterministic timestamps."""
    # Stagger ingestion times so recency ordering is deterministic and distinct.
    timestamp = NOW - float(len(spec.get("text", ""))) * 60.0 - float(index) * 3600.0
    return MemoryRecord(
        id=f"mem_{index}",
        content=str(spec["text"]),
        workspace_id="eval",
        scope=Scope.WORKSPACE,
        mtype=MemoryType.SEMANTIC,
        importance=float(spec.get("importance", 0.5)),
        stability=1.0,
        ingested_at=timestamp,
        last_access=timestamp,
    )


def _context_contains_evidence(context_text: str, keywords: list[str]) -> bool:
    """Check if the handoff context contains at least one supporting keyword.

    Uses case-insensitive substring matching — deterministic, no embedding needed.
    A query is satisfied when ANY of its supporting keywords appear in the context.
    """
    if not keywords:
        return False
    lower_context = context_text.lower()
    return any(kw.lower() in lower_context for kw in keywords if kw)


def _strategy_last_n(records: list[MemoryRecord], k: int) -> str:
    """Return context from the k most recently ingested memories."""
    sorted_recs = sorted(records, key=lambda r: -(r.ingested_at or 0.0))
    selected = sorted_recs[:k]
    return "\n".join(r.content for r in selected)


def _strategy_proactive(records: list[MemoryRecord], k: int) -> str:
    """Return context from top-k memories ranked by score_proactive."""
    scored = [
        (scoring.score_proactive(rec, now=NOW), rec)
        for rec in records
    ]
    scored.sort(key=lambda t: (-t[0], t[1].id))
    selected = [rec for _, rec in scored[:k]]
    return "\n".join(r.content for r in selected)


def _strategy_consolidated(case: dict) -> str:
    """Return the session summary + open threads as the handoff context."""
    parts = []
    summary = case.get("session_1_summary", "")
    if summary:
        parts.append(summary)
    threads = case.get("session_1_open_threads", [])
    if threads:
        parts.append("Open threads: " + "; ".join(threads))
    return "\n".join(parts)


def evaluate_case(case: dict, strategy: str, k: int = DEFAULT_K) -> dict:
    """Evaluate one session transition under one strategy.

    Returns per-query satisfaction and aggregate rate for this case.
    """
    records = [
        _build_record(spec, i)
        for i, spec in enumerate(case["session_1_memories"])
    ]

    if strategy == "last_n_memories":
        context = _strategy_last_n(records, k)
    elif strategy == "proactive_ranking":
        context = _strategy_proactive(records, k)
    elif strategy == "consolidated_summary":
        context = _strategy_consolidated(case)
    else:
        raise ValueError(f"unknown strategy: {strategy}")

    queries = case["session_2_queries"][:5]  # first 5 queries only
    results = []
    for q in queries:
        satisfied = _context_contains_evidence(context, q["supporting_keywords"])
        results.append({
            "query": q["q"],
            "satisfied": satisfied,
        })

    total = len(results)
    hits = sum(1 for r in results if r["satisfied"])
    return {
        "case_id": case["id"],
        "strategy": strategy,
        "satisfaction_rate": hits / total if total else 0.0,
        "hits": hits,
        "total": total,
        "queries": results,
    }


def evaluate(strategy: str, k: int = DEFAULT_K) -> dict:
    """Run the handoff quality eval across all cases for one strategy."""
    cases = load_cases()
    case_results = [evaluate_case(case, strategy, k) for case in cases]
    total_hits = sum(r["hits"] for r in case_results)
    total_queries = sum(r["total"] for r in case_results)
    return {
        "strategy": strategy,
        "satisfaction_rate": total_hits / total_queries if total_queries else 0.0,
        "total_hits": total_hits,
        "total_queries": total_queries,
        "cases": len(case_results),
        "per_case": case_results,
    }


def run() -> dict:
    """Evaluate all strategies and return a comparative report."""
    results = {}
    for strategy in STRATEGIES:
        results[strategy] = evaluate(strategy)
    return results


def main() -> None:
    report = run()
    print("Engraphis handoff-quality eval")
    print(f"  Fixture: {len(load_cases())} session transitions, first-5 queries each\n")
    for strategy in STRATEGIES:
        r = report[strategy]
        print(f"  {strategy:24s}  satisfaction={r['satisfaction_rate']:.3f} "
              f"({r['total_hits']}/{r['total_queries']} queries, "
              f"{r['cases']} cases)")
    print()


if __name__ == "__main__":
    main()
