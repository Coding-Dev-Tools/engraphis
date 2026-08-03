"""Deterministic budget curves and ablations for opt-in planned recall.

This is a repository-local regression/evidence harness, not a competitive
benchmark. It measures the shipped retrieval and packing path on the dedicated
40-task context-routing fixture and keeps raw question/context text out of its
report rows.
"""
from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path
from typing import Optional

from engraphis.backends import DeterministicEmbedder, NumpyVectorIndex
from engraphis.backends.reranker import IdentityReranker
from engraphis.core.context import RegexTokenCounter
from engraphis.core.engine import MemoryEngine
from engraphis.core.interfaces import MemoryType, Scope
from engraphis.core.store import Store
from engraphis.core.textutil import tokenize
from eval.benchmark import paired_bootstrap_ci, sha256_text
from eval.harness import _seed_case_graph, load_dataset


TOKEN_BUDGETS = (256, 512, 1024, 2048, 4096)
EVAL_TYPE_LIMITS = {
    "working": 1,
    "episodic": 2,
    "semantic": 2,
    "procedural": 2,
}
ABLATIONS = {
    "balanced": {"planning": "off", "mtype_limits": None},
    "planner": {"planning": "auto", "mtype_limits": None},
    "type_limits": {"planning": "off", "mtype_limits": EVAL_TYPE_LIMITS},
    "planner_type_limits": {
        "planning": "auto",
        "mtype_limits": EVAL_TYPE_LIMITS,
    },
}
REQUIRED_CATEGORIES = frozenset({
    "long_noisy_history",
    "mixed_memory_types",
    "multi_hop_relationship",
    "late_correction",
})


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1))
    return ordered[index]


def _seed_case(
    case: dict,
) -> tuple[Store, MemoryEngine, str, str, dict[str, str], dict[str, str]]:
    store = Store(":memory:")
    embedder = DeterministicEmbedder(256)
    workspace_id = store.get_or_create_workspace("context-routing")
    repo_id = store.get_or_create_repo(workspace_id, str(case.get("id") or "case"))
    engine = MemoryEngine(
        store,
        embedder,
        NumpyVectorIndex(store),
        IdentityReranker(),
    )
    _seed_case_graph(
        store,
        workspace_id=workspace_id,
        repo_id=repo_id,
        case=case,
    )
    tag_to_id: dict[str, str] = {}
    source_by_id: dict[str, str] = {}
    for memory in case.get("memories", []):
        invalidated_tag = str(memory.get("invalidate_previous") or "")
        if invalidated_tag:
            previous_id = tag_to_id.get(invalidated_tag)
            if previous_id is None:
                raise ValueError(
                    f"{case.get('id')}: unknown invalidate_previous tag {invalidated_tag!r}"
                )
            store.close_validity(previous_id)
        memory_id = engine.remember(
            str(memory.get("text") or ""),
            workspace_id=workspace_id,
            repo_id=repo_id,
            mtype=MemoryType(str(memory.get("mtype") or "semantic")),
            scope=Scope.REPO,
            title=str(memory.get("title") or ""),
            subject_key=str(memory.get("subject_key") or ""),
            claim_kind=str(memory.get("claim_kind") or ""),
            resolve_conflicts=False,
        )
        tag_to_id[str(memory.get("tag") or memory_id)] = memory_id
        source_by_id[memory_id] = str(memory.get("text") or "")

    memory_types = tuple(MemoryType)
    for index in range(max(0, int(case.get("noise_count") or 0))):
        engine.remember(
            (
                f"Unrelated fixture note {case.get('id')} number {index}: "
                f"routine inventory marker NOISE_{index:03d} was checked and archived. "
                + "The unrelated checklist covered capacity labels, office inventory, "
                  "training attendance, cafeteria supplies, and routine calendar cleanup. "
                  * 3
            ),
            workspace_id=workspace_id,
            repo_id=repo_id,
            mtype=memory_types[index % len(memory_types)],
            scope=Scope.REPO,
            resolve_conflicts=False,
        )
    return store, engine, workspace_id, repo_id, tag_to_id, source_by_id


def _evidence_retention_quality(
    *,
    question: str,
    supporting_ids: set[str],
    source_by_id: dict[str, str],
    excerpts_by_id: dict[str, str],
) -> float:
    """Measure answer-bearing token retention, not merely supporting-ID admission."""
    if not supporting_ids:
        return 1.0
    question_tokens = set(tokenize(question))
    per_source = []
    for memory_id in sorted(supporting_ids):
        source_tokens = set(tokenize(source_by_id.get(memory_id, "")))
        evidence_tokens = source_tokens - question_tokens or source_tokens
        excerpt_tokens = set(tokenize(excerpts_by_id.get(memory_id, "")))
        per_source.append(
            len(evidence_tokens & excerpt_tokens) / len(evidence_tokens)
            if evidence_tokens
            else 0.0
        )
    return sum(per_source) / len(per_source)


def _validate_dataset(dataset: list[dict]) -> None:
    task_count = sum(len(case.get("questions", [])) for case in dataset)
    if task_count < 40:
        raise ValueError("context-routing stress dataset must contain at least 40 tasks")
    categories = {str(case.get("category") or "") for case in dataset}
    missing = REQUIRED_CATEGORIES - categories
    if missing:
        raise ValueError(
            "context-routing stress dataset is missing categories: "
            + ", ".join(sorted(missing))
        )
    case_ids: set[str] = set()
    task_ids: set[str] = set()
    for case in dataset:
        case_id = str(case.get("id") or "").strip()
        if not case_id or case_id in case_ids:
            raise ValueError("context-routing cases require unique non-empty ids")
        case_ids.add(case_id)
        tags = [str(memory.get("tag") or "").strip() for memory in case.get("memories", [])]
        if any(not tag for tag in tags) or len(tags) != len(set(tags)):
            raise ValueError(f"{case_id}: memory tags must be unique and non-empty")
        known_tags = set(tags)
        for question in case.get("questions", []):
            task_id = str(question.get("id") or "").strip()
            if not task_id or task_id in task_ids:
                raise ValueError("context-routing questions require unique non-empty ids")
            task_ids.add(task_id)
            supporting = [str(tag) for tag in question.get("supporting", [])]
            if not supporting:
                raise ValueError(f"{task_id}: at least one supporting memory is required")
            unknown = sorted(set(supporting) - known_tags)
            if unknown:
                raise ValueError(
                    f"{task_id}: unknown supporting memory tags: {', '.join(unknown)}"
                )


def _summarize(rows: list[dict]) -> dict:
    count = len(rows)
    quality = sum(float(row["quality"]) for row in rows) / max(1, count)
    tokens = [int(row["context_tokens"]) for row in rows]
    latencies = [float(row["latency_ms"]) for row in rows]
    cached = [
        int(row["provider_cached_input_tokens"])
        for row in rows
        if row.get("provider_cached_input_tokens") is not None
    ]
    return {
        "tasks": count,
        "quality": round(quality, 6),
        "exact_injected_tokens": {
            "total": sum(tokens),
            "mean": round(sum(tokens) / max(1, count), 6),
            "p50": round(_percentile([float(value) for value in tokens], 0.50), 6),
            "p95": round(_percentile([float(value) for value in tokens], 0.95), 6),
        },
        "latency_ms": {
            "p50": round(_percentile(latencies, 0.50), 6),
            "p95": round(_percentile(latencies, 0.95), 6),
        },
        "planner_failures": sum(bool(row["planner_failed"]) for row in rows),
        "context_revisions": len({row["context_revision"] for row in rows}),
        "provider_cached_input_tokens": sum(cached) if cached else None,
    }


def _release_gates(
    rows: dict[str, dict[int, list[dict]]],
    summaries: dict[str, dict[int, dict]],
    *,
    safety_regressions_ok: Optional[bool],
) -> dict:
    reports = {}
    for candidate in ("planner", "planner_type_limits"):
        budgets = {}
        pareto_budgets = 0
        noninferior_everywhere = True
        latency_ok = True
        for budget in TOKEN_BUDGETS:
            baseline_rows = rows["balanced"][budget]
            candidate_rows = rows[candidate][budget]
            baseline_by_task = {row["task_id"]: row for row in baseline_rows}
            pairs = [
                (float(row["quality"]), float(baseline_by_task[row["task_id"]]["quality"]))
                for row in candidate_rows
            ]
            confidence = paired_bootstrap_ci(pairs)
            baseline = summaries["balanced"][budget]
            measured = summaries[candidate][budget]
            quality_delta = measured["quality"] - baseline["quality"]
            baseline_tokens = baseline["exact_injected_tokens"]["mean"]
            candidate_tokens = measured["exact_injected_tokens"]["mean"]
            pareto = (
                quality_delta >= 0.02 and candidate_tokens <= baseline_tokens
            ) or (
                candidate_tokens <= 0.9 * baseline_tokens
                and measured["quality"] >= baseline["quality"]
            )
            pareto_budgets += int(pareto)
            noninferior = confidence["low"] >= -0.01
            noninferior_everywhere = noninferior_everywhere and noninferior
            baseline_latency = baseline["latency_ms"]["p95"]
            ratio = (
                measured["latency_ms"]["p95"] / baseline_latency
                if baseline_latency else 1.0
            )
            latency_ok = latency_ok and ratio <= 1.5
            budgets[str(budget)] = {
                "paired_quality_delta": confidence,
                "quality_delta": round(quality_delta, 6),
                "mean_token_delta": round(candidate_tokens - baseline_tokens, 6),
                "p95_latency_ratio": round(ratio, 6),
                "strict_pareto": pareto,
            }
        evidence_pass = noninferior_everywhere and pareto_budgets >= 2 and latency_ok
        reports[candidate] = {
            "budgets": budgets,
            "noninferior_at_every_budget": noninferior_everywhere,
            "strict_pareto_budget_count": pareto_budgets,
            "offline_p95_within_1_5x": latency_ok,
            "safety_regressions_ok": safety_regressions_ok,
            "repository_local_gate_pass": evidence_pass,
            # Local synthetic evidence must never authorize a release. The official
            # LongMemEval matrix and the independent safety suites are separate artifacts.
            "opt_in_eligible": False,
            "opt_in_blockers": [
                "requires a complete pinned 20-cell LongMemEval-V2 matrix",
                "requires verified grounded, temporal, and poisoning safety artifacts",
            ],
            "default_eligible": False,
            "default_blockers": [
                "requires strict Pareto improvement at three budgets",
                "requires p95 latency within 1.25x",
                "requires hosted provider cache-cost measurements",
            ],
        }
    return reports


def run(
    dataset: list[dict],
    *,
    budgets: tuple[int, ...] = TOKEN_BUDGETS,
    safety_regressions_ok: Optional[bool] = None,
) -> dict:
    _validate_dataset(dataset)
    normalized_budgets = tuple(sorted(set(int(value) for value in budgets)))
    if normalized_budgets != TOKEN_BUDGETS:
        raise ValueError(f"planned recall budgets must be exactly {TOKEN_BUDGETS}")
    rows: dict[str, dict[int, list[dict]]] = {
        method: {budget: [] for budget in TOKEN_BUDGETS}
        for method in ABLATIONS
    }
    schema_versions = set()
    for case in dataset:
        store, engine, workspace_id, repo_id, tag_to_id, source_by_id = _seed_case(case)
        try:
            schema_versions.add(store.schema_version)
            for question in case.get("questions", []):
                supporting_ids = {
                    tag_to_id[tag]
                    for raw_tag in question.get("supporting", [])
                    if (tag := str(raw_tag)) in tag_to_id
                }
                task_id = str(question.get("id") or "")
                question_text = str(question.get("q") or "")
                for budget in TOKEN_BUDGETS:
                    methods = list(ABLATIONS)
                    rotation = int(sha256_text(f"{task_id}:{budget}")[:8], 16) % len(methods)
                    methods = methods[rotation:] + methods[:rotation]
                    for method in methods:
                        options = ABLATIONS[method]
                        started = time.perf_counter()
                        result = engine.recall(
                            question_text,
                            workspace_id=workspace_id,
                            repo_id=repo_id,
                            k=40,
                            token_budget=budget,
                            planning=options["planning"],
                            mtype_limits=options["mtype_limits"],
                            diagnostics=True,
                        )
                        latency_ms = (time.perf_counter() - started) * 1000.0
                        excerpts_by_id = {
                            chunk.id: chunk.excerpt for chunk in result.packed_chunks
                        }
                        quality = _evidence_retention_quality(
                            question=question_text,
                            supporting_ids=supporting_ids,
                            source_by_id=source_by_id,
                            excerpts_by_id=excerpts_by_id,
                        )
                        details = result.planning_details or {}
                        rows[method][budget].append({
                            "task_id": task_id,
                            "task_sha256": sha256_text(task_id),
                            "category": str(case.get("category") or "unknown"),
                            "quality": round(quality, 6),
                            "context_tokens": int(result.usage.context_tokens),
                            "latency_ms": round(latency_ms, 6),
                            "planner_failed": bool(details.get("fallback_reason")),
                            "context_revision": result.context_revision,
                            "provider_cached_input_tokens": question.get(
                                "provider_cached_input_tokens"
                            ),
                        })
        finally:
            store.close()
    summaries = {
        method: {
            budget: _summarize(method_rows[budget])
            for budget in TOKEN_BUDGETS
        }
        for method, method_rows in rows.items()
    }
    return {
        "benchmark": {
            "name": "engraphis-context-routing-stress/v1",
            "offline": True,
            "token_counter": RegexTokenCounter.identity,
            "token_budgets": list(TOKEN_BUDGETS),
            "ablations": list(ABLATIONS),
            "schema_versions": sorted(schema_versions),
            "scope": "repository-local regression evidence; not a competitor comparison",
            "latency_scope": (
                "single-process indicative timing with deterministic ablation-order rotation; "
                "not release-authoritative"
            ),
        },
        "workload": {
            "scenarios": len(dataset),
            "tasks": sum(len(case.get("questions", [])) for case in dataset),
            "categories": sorted({str(case.get("category")) for case in dataset}),
        },
        "methods": {
            method: {str(budget): value for budget, value in budgets_map.items()}
            for method, budgets_map in summaries.items()
        },
        "release_gates": _release_gates(
            rows,
            summaries,
            safety_regressions_ok=safety_regressions_ok,
        ),
        "detail": {
            method: {str(budget): value for budget, value in budgets_map.items()}
            for method, budgets_map in rows.items()
        },
    }


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset",
        default=str(
            Path(__file__).resolve().parent / "datasets" / "context_routing_stress.jsonl"
        ),
    )
    parser.add_argument("--details", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = run(load_dataset(args.dataset))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True))
        raise SystemExit(2) from exc
    if not args.details:
        report.pop("detail", None)
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
