"""Deterministic eval for consolidated-memory ranking preference.

The production scorer gives consolidated digests a small post-normalization bonus. This
fixture measures both sides of that change: summary queries should prefer the digest over
its raw episodes, while detail queries must still retrieve a more-specific raw memory.

Run offline with::

    python -m eval.consolidation_ranking
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from engraphis.core.consolidate import consolidate
from engraphis.core.engine import MemoryEngine
from engraphis.core.interfaces import MemoryType, SearchFilter
from engraphis.core.recall import CONSOLIDATION_BONUS


DATASET = Path(__file__).with_name("datasets") / "consolidation_ranking.jsonl"


def load_cases(path: Path = DATASET) -> list[dict[str, Any]]:
    """Load and validate the small checked-in digest/source ranking fixture."""
    cases: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        case = json.loads(line)
        if not isinstance(case.get("id"), str) or not case["id"].strip():
            raise ValueError(f"invalid consolidation ranking id on line {line_number}")
        cluster = case.get("cluster")
        if not isinstance(cluster, list) or len(cluster) < 3:
            raise ValueError(f"case {case['id']} needs at least three cluster memories")
        if not all(isinstance(item.get("text"), str) and item["text"].strip()
                   for item in cluster):
            raise ValueError(f"case {case['id']} has an invalid cluster memory")
        context = case.get("context", [])
        if not isinstance(context, list):
            raise ValueError(f"case {case['id']} context must be a list")
        probes = case.get("bonus_probes", [])
        if not isinstance(probes, list):
            raise ValueError(f"case {case['id']} bonus_probes must be a list")
        for probe in probes:
            if (
                not isinstance(probe, dict)
                or not isinstance(probe.get("id"), str)
                or probe.get("expected") not in {"source", "digest"}
            ):
                raise ValueError(f"case {case['id']} has an invalid bonus probe")
            for field in ("source_score", "digest_score"):
                value = probe.get(field)
                if (
                    not isinstance(value, (int, float))
                    or isinstance(value, bool)
                    or not math.isfinite(float(value))
                ):
                    raise ValueError(f"case {case['id']} has an invalid probe {field}")
        expected = case.get("expected")
        if not isinstance(expected, (str, dict)):
            raise ValueError(f"case {case['id']} needs an expected ranking target")
        if not isinstance(case.get("query"), str) or not case["query"].strip():
            raise ValueError(f"case {case['id']} needs a query")
        cases.append(case)
    if not cases:
        raise ValueError("consolidation ranking fixture is empty")
    return cases


def _memory_type(value: object, default: MemoryType) -> MemoryType:
    if value is None:
        return default
    try:
        return MemoryType(str(value))
    except ValueError as exc:
        raise ValueError(f"unknown memory type in consolidation ranking fixture: {value!r}") from exc


def _ranked(trace: list[dict[str, Any]], *, without_bonus: bool = False) -> list[dict[str, Any]]:
    def key(item: dict[str, Any]) -> tuple[float, str]:
        score = float(item.get("fusion_score") or 0.0)
        if without_bonus:
            score -= float(item.get("consolidation_bonus") or 0.0)
        return (-score, str(item["id"]))

    return sorted(trace, key=key)


def _expected_id(case: dict[str, Any], *, digest_id: str, context_ids: dict[str, str]) -> str:
    expected = case["expected"]
    if expected == "digest":
        return digest_id
    if isinstance(expected, dict):
        tag = str(expected.get("tag") or "")
    else:
        tag = str(expected)
    if tag not in context_ids:
        raise ValueError(f"case {case['id']} expected unknown context tag {tag!r}")
    return context_ids[tag]


def evaluate_case(case: dict[str, Any]) -> dict[str, Any]:
    """Run one fixture case and return current/baseline rank evidence."""
    engine = MemoryEngine.create(":memory:")
    workspace_id = engine.store.get_or_create_workspace("consolidation-eval")
    repo_id = engine.store.get_or_create_repo(workspace_id, str(case["id"]))
    source_ids: list[str] = []
    for item in case["cluster"]:
        source_ids.append(engine.remember(
            str(item["text"]), workspace_id=workspace_id, repo_id=repo_id,
            mtype=MemoryType.EPISODIC, resolve_conflicts=False,
        ))
    context_ids: dict[str, str] = {}
    for index, item in enumerate(case.get("context", [])):
        tag = str(item.get("tag") or f"context-{index}")
        context_ids[tag] = engine.remember(
            str(item["text"]), workspace_id=workspace_id, repo_id=repo_id,
            mtype=_memory_type(item.get("mtype"), MemoryType.SEMANTIC),
            resolve_conflicts=False,
        )
    report = consolidate(engine, workspace_id=workspace_id, repo_id=repo_id)
    if len(report["digests_created"]) != 1:
        raise AssertionError(f"case {case['id']} did not create exactly one digest")
    digest_id = str(report["digests_created"][0]["id"])
    result = engine.recall_engine.recall(
        str(case["query"]),
        SearchFilter(workspace_id=workspace_id, repo_id=repo_id),
        k=max(3, int(case.get("k", 5))), diagnostics=True, reinforce=False,
    )
    trace = result.retrieval_trace or []
    current = _ranked(trace)
    baseline = _ranked(trace, without_bonus=True)
    current_ids = [str(item["id"]) for item in current]
    baseline_ids = [str(item["id"]) for item in baseline]
    expected_id = _expected_id(case, digest_id=digest_id, context_ids=context_ids)

    def rank(ids: list[str], target: str) -> int | None:
        return ids.index(target) + 1 if target in ids else None

    trace_by_id = {str(item["id"]): item for item in trace}
    digest_score = float(trace_by_id[digest_id]["fusion_score"])
    best_source_score = max(
        (float(trace_by_id[source_id]["fusion_score"]) for source_id in source_ids
         if source_id in trace_by_id),
        default=0.0,
    )
    baseline_digest_score = digest_score - float(
        trace_by_id[digest_id].get("consolidation_bonus") or 0.0
    )
    digest_rank = rank(current_ids, digest_id)
    baseline_digest_rank = rank(baseline_ids, digest_id)
    expected_rank = rank(current_ids, expected_id)
    source_ranks = [rank(current_ids, source_id) for source_id in source_ids]
    source_ranks = [value for value in source_ranks if value is not None]
    expected_role = "digest" if case["expected"] == "digest" else "raw"
    expected_score = float(trace_by_id.get(expected_id, {}).get("fusion_score") or 0.0)
    return {
        "id": case["id"],
        "expected_id": expected_id,
        "expected_role": expected_role,
        "current_top": current_ids[0] if current_ids else None,
        "baseline_top": baseline_ids[0] if baseline_ids else None,
        "expected_score": expected_score,
        "digest_rank": digest_rank,
        "baseline_digest_rank": baseline_digest_rank,
        "digest_score": digest_score,
        "baseline_digest_score": baseline_digest_score,
        "best_source_score": best_source_score,
        "digest_improved": (
            digest_rank is not None and baseline_digest_rank is not None
            and digest_rank < baseline_digest_rank
        ),
        "ranking_changed": current_ids != baseline_ids,
        "expected_rank": expected_rank,
        "expected_hit_at_k": expected_rank is not None
        and expected_rank <= max(3, int(case.get("k", 5))),
        "source_hit_at_k": bool(source_ranks),
        "best_source_rank": min(source_ranks) if source_ranks else None,
    }


def _evaluate_bonus_probe(probe: dict[str, Any]) -> dict[str, Any]:
    """Exercise the exact post-normalization bonus at a controlled boundary."""
    baseline_scores = {
        "source": float(probe["source_score"]),
        "digest": float(probe["digest_score"]),
    }
    policy_scores = dict(baseline_scores)
    policy_scores["digest"] += CONSOLIDATION_BONUS
    baseline = sorted(baseline_scores, key=lambda kind: (-baseline_scores[kind], kind))
    policy = sorted(policy_scores, key=lambda kind: (-policy_scores[kind], kind))
    expected = str(probe["expected"])
    return {
        "id": probe["id"],
        "expected": expected,
        "baseline_top": baseline[0],
        "policy_top": policy[0],
        "baseline_hit_at_1": baseline[0] == expected,
        "policy_hit_at_1": policy[0] == expected,
        "ranking_changed": baseline != policy,
    }


def evaluate(path: Path = DATASET) -> dict[str, Any]:
    """Return ranking preference and raw-evidence retention metrics."""
    cases = load_cases(path)
    results = [evaluate_case(case) for case in cases]
    bonus_probes = [
        _evaluate_bonus_probe(probe)
        for case in cases for probe in case.get("bonus_probes", [])
    ]
    summary = [item for item in results if item["expected_role"] == "digest"]
    details = [item for item in results if item["expected_role"] == "raw"]
    source_regressions = [
        item["id"] for item in bonus_probes
        if item["expected"] == "source" and not item["policy_hit_at_1"]
    ]
    return {
        "cases": len(results),
        "summary_digest_top1_rate": (
            sum(item["current_top"] == item["expected_id"] for item in summary)
            / len(summary)
        ),
        "baseline_summary_digest_top1_rate": (
            sum(item["baseline_top"] == item["expected_id"] for item in summary)
            / len(summary)
        ),
        "production_trace_ranking_changed_rate": (
            sum(item["ranking_changed"] for item in results) / len(results)
        ),
        "bonus_probe_count": len(bonus_probes),
        "ranking_changed_rate": (
            sum(item["ranking_changed"] for item in bonus_probes) / len(bonus_probes)
            if bonus_probes else 0.0
        ),
        "bonus_probe_digest_top1_rate": (
            sum(item["policy_hit_at_1"] for item in bonus_probes
                if item["expected"] == "digest")
            / max(1, sum(item["expected"] == "digest" for item in bonus_probes))
        ),
        "baseline_bonus_probe_digest_top1_rate": (
            sum(item["baseline_hit_at_1"] for item in bonus_probes
                if item["expected"] == "digest")
            / max(1, sum(item["expected"] == "digest" for item in bonus_probes))
        ),
        "bonus_probe_source_top1_rate": (
            sum(item["policy_hit_at_1"] for item in bonus_probes
                if item["expected"] == "source")
            / max(1, sum(item["expected"] == "source" for item in bonus_probes))
        ),
        "bonus_probe_source_regressions": source_regressions,
        "bonus_probes": bonus_probes,
        "expected_hit_at_k": sum(item["expected_hit_at_k"] for item in results) / len(results),
        "raw_detail_hit_at_k": (
            sum(item["expected_hit_at_k"] for item in details) / len(details)
        ),
        "source_hit_at_k": sum(item["source_hit_at_k"] for item in results) / len(results),
        "results": results,
    }


def main() -> None:
    report = evaluate()
    print("Engraphis consolidation-ranking eval")
    print(f"  cases:                       {report['cases']}")
    print(f"  summary digest top-1:        {report['summary_digest_top1_rate']:.3f}")
    print(f"  baseline summary digest top-1:{report['baseline_summary_digest_top1_rate']:.3f}")
    print(f"  ranking changed rate:        {report['ranking_changed_rate']:.3f}")
    print(f"  expected hit@k:              {report['expected_hit_at_k']:.3f}")
    print(f"  raw detail hit@k:            {report['raw_detail_hit_at_k']:.3f}")
    print(f"  source evidence hit@k:       {report['source_hit_at_k']:.3f}")
    for result in report["results"]:
        print(
            f"  {result['id']}: top={result['current_top']} "
            f"baseline_top={result['baseline_top']} expected_rank={result['expected_rank']}"
        )


if __name__ == "__main__":
    main()
