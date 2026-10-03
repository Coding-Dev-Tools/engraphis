"""Opt-in, bounded BYOK comparison of Jev route selection on synthetic tasks.

This is an evaluation utility, not a CI benchmark or a product claim. It makes at
most one remote Jev request per task, only after --allow-remote, and sends generated
public task queries plus locally generated route strings. Provider output and task
text are never written to the report. Recall@5 and packed answer-token coverage
must be non-inferior while nDCG@5 improves before this evaluation can support a
retrieval-benefit claim.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
import json
import math
import random
import statistics
import sys
from typing import Any

from engraphis.backends.jev_decision import JevDecisionBackend
from engraphis.backends.jev_query_planner import JevAssistedQueryPlanner
from engraphis.backends.jev_transport import MODEL, select_decision_client
from engraphis.core.interfaces import MemoryType, Scope
from engraphis.core.query_planner import DeterministicQueryPlanner
from engraphis.core.textutil import tokenize
from engraphis.service import MemoryService


TASKS_PER_CATEGORY = 10
MAX_REMOTE_REQUESTS = 40
CATEGORIES = (
    "lexical_identifiers",
    "graph_relationships",
    "temporal_changes",
    "procedural_context",
)
_ALLOWED_TYPES = {
    "lexical_identifiers": MemoryType.SEMANTIC,
    "graph_relationships": MemoryType.EPISODIC,
    "temporal_changes": MemoryType.EPISODIC,
    "procedural_context": MemoryType.PROCEDURAL,
}


@dataclass(frozen=True)
class SyntheticTask:
    task_id: str
    category: str
    query: str
    answer_tokens: frozenset[str]
    answer_phrase: str
    target_memory_id: str = ""
    repo_id: str = ""


class _CappedClient:
    """Count provider attempts and reject any request beyond the configured cap."""

    def __init__(self, client, maximum: int) -> None:
        self.client = client
        self.maximum = maximum
        self.calls = 0
        self.is_configured = bool(client.is_configured)
        self.allow_fallback = False

    def evaluate(self, state, questions, **options):
        if self.calls >= self.maximum:
            raise RuntimeError("remote request cap reached")
        self.calls += 1
        return self.client.evaluate(state, questions, **options)


def synthetic_tasks() -> list[SyntheticTask]:
    """Build forty isolated tasks with two deterministic alternate routes each."""
    tasks = []
    planner = DeterministicQueryPlanner()
    index = 0
    for category in CATEGORIES:
        for ordinal in range(TASKS_PER_CATEGORY):
            index += 1
            service = f"SERVICE_{index:02d}"
            cache = f"CACHE_{index:02d}"
            if category == "procedural_context":
                query = f"How should {service} impact {cache} after the current workflow change?"
            elif category == "graph_relationships":
                query = f"Why is {service} related to {cache} after the current version changed?"
            elif category == "temporal_changes":
                query = f"Why did {service} impact {cache} before the current migration change?"
            else:
                query = f"Why does {service} impact {cache} after the current release change?"
            answer_phrase = f"witness{index:02d} stabilizes lease renewal"
            if len(planner.plan(query).queries) != 3:
                raise ValueError("synthetic task must have two deterministic alternate routes")
            tasks.append(SyntheticTask(
                task_id=f"task-{index:02d}",
                category=category,
                query=query,
                answer_tokens=frozenset(tokenize(answer_phrase)),
                answer_phrase=answer_phrase,
            ))
    return tasks


def _seed(service: MemoryService, tasks: list[SyntheticTask]) -> list[SyntheticTask]:
    workspace_id = service.store.get_or_create_workspace("jev-synthetic-evaluation")
    seeded = []
    for task in tasks:
        repo_id = service.store.get_or_create_repo(workspace_id, task.task_id)
        index = int(task.task_id.removeprefix("task-"))
        service.engine.remember(
            (
                f"Synthetic fixture {index}: {task.answer_phrase}. This verified fact links "
                f"{task.category.replace('_', ' ')} evidence for {task.query.split()[2]} "
                f"and {task.query.split()[4]}."
            ),
            workspace_id=workspace_id,
            repo_id=repo_id,
            scope=Scope.REPO,
            mtype=_ALLOWED_TYPES[task.category],
            title=f"Synthetic evidence {index}",
            resolve_conflicts=False,
        )
        for distractor in range(12):
            distractor_type = tuple(MemoryType)[distractor % len(MemoryType)]
            service.engine.remember(
                (
                    f"Synthetic decoy {index}-{distractor}: routine "
                    f"{task.category.replace('_', ' ')} status for {task.query.split()[2]} "
                    f"and {task.query.split()[4]}; no verified outcome is recorded."
                ),
                workspace_id=workspace_id,
                repo_id=repo_id,
                scope=Scope.REPO,
                mtype=distractor_type,
                title=f"Synthetic decoy {index}-{distractor}",
                resolve_conflicts=False,
            )
        target = service.store.conn.execute(
            "SELECT id FROM memories WHERE repo_id=? AND title=? ORDER BY ingested_at DESC LIMIT 1",
            (repo_id, f"Synthetic evidence {index}"),
        ).fetchone()
        if target is None:
            raise RuntimeError("synthetic target memory was not created")
        seeded.append(SyntheticTask(
            task_id=task.task_id,
            category=task.category,
            query=task.query,
            answer_tokens=task.answer_tokens,
            answer_phrase=task.answer_phrase,
            target_memory_id=str(target[0]),
            repo_id=repo_id,
        ))
    return seeded


def _metrics(result, task: SyntheticTask) -> dict[str, float]:
    ids = [str(chunk.get("id") or "") for chunk in result.chunks[:5]]
    try:
        rank = ids.index(task.target_memory_id) + 1
    except ValueError:
        rank = 0
    context_tokens = set(tokenize(result.context))
    answer_coverage = (
        len(task.answer_tokens & context_tokens) / len(task.answer_tokens)
        if task.answer_tokens else 1.0
    )
    return {
        "recall_at_5": float(rank > 0),
        "ndcg_at_5": 1.0 / math.log2(rank + 1) if rank else 0.0,
        "answer_token_coverage": answer_coverage,
    }


def _bootstrap_ci(values: list[float], *, seed: int) -> tuple[float, float, float]:
    if not values:
        return 0.0, 0.0, 0.0
    rng = random.Random(seed)
    means = [
        statistics.fmean(values[rng.randrange(len(values))] for _ in values)
        for _ in range(10_000)
    ]
    means.sort()
    return (
        statistics.fmean(values),
        means[249],
        means[9_749],
    )


def benefit_gate(deltas: dict[str, tuple[float, float, float]]) -> bool:
    """Require a positive nDCG interval and non-negative safety-metric intervals."""
    return (
        deltas["ndcg_at_5"][1] > 0
        and deltas["recall_at_5"][1] >= 0
        and deltas["answer_token_coverage"][1] >= 0
    )


def run_evaluation(*, timeout_s: float, max_requests: int) -> dict[str, Any]:
    tasks = synthetic_tasks()[:max_requests]
    if not tasks or len(tasks) > MAX_REMOTE_REQUESTS:
        raise ValueError("evaluation request count exceeds the 40-call safety cap")
    # Importing this module alone does not load Engraphis' owner-private config;
    # initialize its trusted environment loader before resolving the explicit BYOK client.
    import engraphis.config  # noqa: F401

    client, provider = select_decision_client("byok")
    if provider != "typesafe_byok" or client is None or not client.is_configured:
        raise RuntimeError("a configured explicit BYOK client is required")
    capped_client = _CappedClient(client, max_requests)

    service = MemoryService.create(":memory:", embed_model="", embed_dim=256)
    try:
        tasks = _seed(service, tasks)
        engine = service.engine
        engine.recall_engine.planner_timeout_s = timeout_s
        local_planner = DeterministicQueryPlanner()
        assisted_planner = JevAssistedQueryPlanner(
            JevDecisionBackend(client=capped_client, model=MODEL),
            deterministic=local_planner,
        )
        workspace_id = service.store.get_or_create_workspace("jev-synthetic-evaluation")
        baseline_rows = []
        assisted_rows = []
        reasons = Counter()
        for task in tasks:
            engine.recall_engine.query_planner = local_planner
            baseline = engine.recall(
                task.query,
                workspace_id=workspace_id,
                repo_id=task.repo_id,
                scopes=[Scope.REPO],
                k=5,
                token_budget=128,
                planning="auto",
                reinforce=False,
            )
            baseline_rows.append(_metrics(baseline, task))

            engine.recall_engine.query_planner = assisted_planner
            assisted = engine.recall(
                task.query,
                workspace_id=workspace_id,
                repo_id=task.repo_id,
                scopes=[Scope.REPO],
                k=5,
                token_budget=128,
                planning="auto",
                jev_assisted=True,
                allow_remote=True,
                data_classification="public",
                reinforce=False,
            )
            assisted_rows.append(_metrics(assisted, task))
            advisory = assisted.planning_advisory or {}
            reasons[str(advisory.get("reason") or advisory.get("status") or "unknown")] += 1

        deltas = {}
        for name in ("ndcg_at_5", "recall_at_5", "answer_token_coverage"):
            differences = [
                float(after[name]) - float(before[name])
                for before, after in zip(baseline_rows, assisted_rows)
            ]
            deltas[name] = _bootstrap_ci(differences, seed=20261003)
        request_count = capped_client.calls
        if request_count > max_requests or request_count > MAX_REMOTE_REQUESTS:
            raise RuntimeError("evaluation exceeded its configured remote-request cap")
        return {
            "schema": "engraphis-jev-recall-evaluation/v1",
            "fixture": "synthetic-independent-tasks/v1",
            "tasks": len(tasks),
            "categories": dict(Counter(task.category for task in tasks)),
            "model": MODEL,
            "remote_calls_max": max_requests,
            "remote_calls_requested": request_count,
            "classification": "public",
            "consent_per_call": True,
            "retrieval_quality": {
                "baseline_mean": {
                    name: round(statistics.fmean(float(row[name]) for row in baseline_rows), 6)
                    for name in ("ndcg_at_5", "recall_at_5", "answer_token_coverage")
                },
                "jev_mean": {
                    name: round(statistics.fmean(float(row[name]) for row in assisted_rows), 6)
                    for name in ("ndcg_at_5", "recall_at_5", "answer_token_coverage")
                },
                "paired_delta_mean_ci95": {
                    name: {
                        "mean": round(value[0], 6),
                        "lower": round(value[1], 6),
                        "upper": round(value[2], 6),
                    }
                    for name, value in deltas.items()
                },
                "benefit_gate_passed": benefit_gate(deltas),
            },
            "jev_result_reasons": dict(sorted(reasons.items())),
        }
    finally:
        service.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-remote", action="store_true")
    parser.add_argument("--max-requests", type=int, default=MAX_REMOTE_REQUESTS)
    parser.add_argument("--timeout", type=float, default=8.0)
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args(argv)
    if not 1 <= args.max_requests <= MAX_REMOTE_REQUESTS:
        parser.error("--max-requests must be between 1 and 40")
    if not math.isfinite(args.timeout) or not 0.5 <= args.timeout <= 15:
        parser.error("--timeout must be between 0.5 and 15 seconds")
    if args.check_only:
        tasks = synthetic_tasks()[:args.max_requests]
        print(json.dumps({
            "fixture": "synthetic-independent-tasks/v1",
            "tasks": len(tasks),
            "categories": dict(Counter(task.category for task in tasks)),
            "eligible_tasks": sum(
                len(DeterministicQueryPlanner().plan(task.query).queries) == 3
                for task in tasks
            ),
            "remote_calls": 0,
        }, sort_keys=True))
        return 0
    if not args.allow_remote:
        parser.error("remote evaluation requires explicit --allow-remote consent")
    try:
        report = run_evaluation(timeout_s=args.timeout, max_requests=args.max_requests)
    except Exception:
        raise SystemExit(
            "Jev evaluation stopped safely; provider details, prompts, and credentials were not logged."
        ) from None
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
