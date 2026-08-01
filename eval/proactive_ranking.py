"""Deterministic calibration eval for queryless proactive ranking.

The fixture makes the tradeoff explicit: important policies should survive a fresh
zero-importance scratch note for a bounded period, while a low-importance old note
should still yield.  It runs entirely offline:

    python -m eval.proactive_ranking
"""
from __future__ import annotations

import json
from pathlib import Path

from engraphis.core import scoring
from engraphis.core.interfaces import MemoryRecord, MemoryType, Scope

DATASET = Path(__file__).with_name("datasets") / "proactive_ranking.jsonl"
NOW = 1_700_000_000.0


def load_cases(path: Path = DATASET) -> list[dict]:
    """Load the small checked-in ranking fixture with deterministic input checks."""
    cases = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        case = json.loads(line)
        if not isinstance(case.get("id"), str) or not isinstance(case.get("expected_top"), str):
            raise ValueError(f"invalid proactive ranking case on line {line_number}")
        records = case.get("records")
        if not isinstance(records, list) or len(records) < 2:
            raise ValueError(f"case {case['id']} must contain at least two records")
        if case["expected_top"] not in {record.get("id") for record in records}:
            raise ValueError(f"case {case['id']} expected_top is not a record id")
        cases.append(case)
    if not cases:
        raise ValueError("proactive ranking fixture is empty")
    return cases


def _record(spec: dict) -> MemoryRecord:
    age_seconds = float(spec["age_days"]) * 86400.0
    timestamp = NOW - age_seconds
    return MemoryRecord(
        id=str(spec["id"]), content=str(spec["id"]), workspace_id="eval",
        scope=Scope.WORKSPACE, mtype=MemoryType.SEMANTIC,
        importance=float(spec["importance"]), stability=1.0,
        ingested_at=timestamp, last_access=timestamp,
    )


def evaluate(*, importance_retention_floor: float) -> dict:
    """Return top-1 accuracy and margins for one prospective floor coefficient."""
    results = []
    for case in load_cases():
        ranked = sorted(
            (
                (
                    scoring.score_proactive(
                        _record(spec), now=NOW,
                        importance_retention_floor=importance_retention_floor,
                    ),
                    str(spec["id"]),
                )
                for spec in case["records"]
            ),
            key=lambda item: (-item[0], item[1]),
        )
        expected = case["expected_top"]
        expected_score = next(score for score, record_id in ranked if record_id == expected)
        competing_score = max(score for score, record_id in ranked if record_id != expected)
        results.append({
            "id": case["id"], "expected_top": expected, "actual_top": ranked[0][1],
            "margin": expected_score - competing_score,
        })
    hits = sum(result["actual_top"] == result["expected_top"] for result in results)
    return {
        "importance_retention_floor": importance_retention_floor,
        "top_1_accuracy": hits / len(results), "hits": hits, "cases": len(results),
        "minimum_expected_margin": min(result["margin"] for result in results),
        "results": results,
    }


def run() -> dict:
    """Compare the prior and calibrated floors on the fixed fixture."""
    return {
        "no_floor": evaluate(importance_retention_floor=0.0),
        "prior_floor": evaluate(importance_retention_floor=0.60),
        "calibrated_floor": evaluate(
            importance_retention_floor=scoring.PROACTIVE_IMPORTANCE_RETENTION_FLOOR,
        ),
    }


def main() -> None:
    report = run()
    print("Engraphis proactive-ranking eval")
    for label, result in report.items():
        print(f"  {label:16} top-1={result['top_1_accuracy']:.3f} "
              f"({result['hits']}/{result['cases']}), "
              f"minimum expected margin={result['minimum_expected_margin']:.3f}")


if __name__ == "__main__":
    main()
