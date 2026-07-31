"""Paired code-agent A/B analysis for full-history versus Engraphis runs.

This module deliberately does not launch an agent. The selected agent controller and
task sandbox own execution; this analyzer enforces the paired evidence contract after
both conditions have produced content-free run records. It prevents unmatched tasks,
different success oracles, and unpaired averages from becoming a marketing claim.

Each JSON/JSONL row requires::

    {
      "task_id": "repo/task-1",
      "condition": "full_history",
      "oracle": "pytest:tests/test_task.py",
      "success": true,
      "input_tokens": 1000,
      "output_tokens": 120,
      "tool_tokens": 80,
      "retries": 0,
      "latency_ms": 2500,
      "cost_usd": 0.01
    }

``cost_usd`` is optional; every other metric is required and non-negative.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any, Optional

from eval.benchmark import paired_bootstrap_ci


CONDITIONS = ("full_history", "engraphis")
NUMERIC_FIELDS = (
    "input_tokens",
    "output_tokens",
    "tool_tokens",
    "retries",
    "latency_ms",
)


def _records(path: str | Path) -> list[dict[str, Any]]:
    source = Path(path)
    text = source.read_text(encoding="utf-8")
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        value = [
            json.loads(line)
            for line in text.splitlines()
            if line.strip()
        ]
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list) or not value or not all(
        isinstance(row, dict) for row in value
    ):
        raise ValueError(f"{source} must contain one or more JSON object records")
    return value


def load_runs(path: str | Path, *, condition: str) -> dict[str, dict[str, Any]]:
    """Load and strictly validate one experiment condition."""
    if condition not in CONDITIONS:
        raise ValueError(f"condition must be one of: {', '.join(CONDITIONS)}")
    runs: dict[str, dict[str, Any]] = {}
    for number, row in enumerate(_records(path), start=1):
        task_id = row.get("task_id")
        oracle = row.get("oracle")
        if not isinstance(task_id, str) or not task_id.strip():
            raise ValueError(f"{path}:{number} requires a non-empty task_id")
        task_id = task_id.strip()
        if task_id in runs:
            raise ValueError(f"{path} contains duplicate task_id {task_id!r}")
        if row.get("condition") != condition:
            raise ValueError(
                f"{path}:{number} condition must be {condition!r}"
            )
        if not isinstance(oracle, str) or not oracle.strip():
            raise ValueError(f"{path}:{number} requires a deterministic oracle label")
        oracle = oracle.strip()
        if not isinstance(row.get("success"), bool):
            raise ValueError(f"{path}:{number} success must be boolean")
        normalized = {
            "task_id": task_id,
            "condition": condition,
            "oracle": oracle,
            "success": row["success"],
        }
        for field in NUMERIC_FIELDS:
            value = row.get(field)
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or value < 0
                or not math.isfinite(float(value))
            ):
                raise ValueError(f"{path}:{number} {field} must be non-negative")
            normalized[field] = float(value)
        cost = row.get("cost_usd")
        if cost is not None and (
            not isinstance(cost, (int, float))
            or isinstance(cost, bool)
            or cost < 0
            or not math.isfinite(float(cost))
        ):
            raise ValueError(f"{path}:{number} cost_usd must be non-negative")
        normalized["cost_usd"] = float(cost) if cost is not None else None
        normalized["total_tokens"] = sum(
            normalized[field]
            for field in ("input_tokens", "output_tokens", "tool_tokens")
        )
        runs[task_id] = normalized
    return runs


def _mean(values: list[float]) -> float:
    return round(sum(values) / len(values), 6) if values else 0.0


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    metrics = {
        "success_rate": _mean([float(row["success"]) for row in rows]),
    }
    for field in (*NUMERIC_FIELDS, "total_tokens"):
        metrics[f"mean_{field}"] = _mean([float(row[field]) for row in rows])
    costs = [float(row["cost_usd"]) for row in rows if row["cost_usd"] is not None]
    metrics["mean_cost_usd"] = _mean(costs) if len(costs) == len(rows) else None
    return metrics


def evaluate(
    baseline: dict[str, dict[str, Any]],
    candidate: dict[str, dict[str, Any]],
    *,
    iterations: int = 5000,
    seed: int = 20260730,
) -> dict[str, Any]:
    """Return paired deltas as ``Engraphis - full_history`` with bootstrap CIs."""
    if type(iterations) is not int or iterations <= 0:
        raise ValueError("iterations must be a positive integer")
    if type(seed) is not int:
        raise ValueError("seed must be an integer")
    if set(baseline) != set(candidate):
        missing_candidate = sorted(set(baseline) - set(candidate))
        missing_baseline = sorted(set(candidate) - set(baseline))
        raise ValueError(
            "paired task IDs differ: "
            f"missing_engraphis={missing_candidate}, missing_full_history={missing_baseline}"
        )
    task_ids = sorted(baseline)
    for task_id in task_ids:
        if baseline[task_id]["oracle"] != candidate[task_id]["oracle"]:
            raise ValueError(f"task {task_id!r} used different success oracles")

    baseline_rows = [baseline[task_id] for task_id in task_ids]
    candidate_rows = [candidate[task_id] for task_id in task_ids]
    fields = {
        "success_rate": "success",
        **{f"mean_{field}": field for field in (*NUMERIC_FIELDS, "total_tokens")},
    }
    deltas = {}
    for label, field in fields.items():
        pairs = [
            (float(candidate[task_id][field]), float(baseline[task_id][field]))
            for task_id in task_ids
        ]
        deltas[label] = paired_bootstrap_ci(
            pairs, iterations=iterations, seed=seed,
        )
    if all(row["cost_usd"] is not None for row in baseline_rows + candidate_rows):
        deltas["mean_cost_usd"] = paired_bootstrap_ci(
            [
                (
                    float(candidate[task_id]["cost_usd"]),
                    float(baseline[task_id]["cost_usd"]),
                )
                for task_id in task_ids
            ],
            iterations=iterations,
            seed=seed,
        )

    return {
        "schema": "engraphis-code-agent-ab/v1",
        "paired_tasks": len(task_ids),
        "conditions": {
            "baseline": "full_history",
            "candidate": "engraphis",
        },
        "delta_direction": "engraphis_minus_full_history",
        "interpretation": {
            "success_rate": "positive_is_better",
            "tokens_retries_latency_cost": "negative_is_better",
        },
        "full_history": _summary(baseline_rows),
        "engraphis": _summary(candidate_rows),
        "paired_bootstrap": deltas,
        # The pairing checks above deliberately happen before aggregation, but
        # task IDs and oracle commands can reveal private repository layout.
        # The public-safe result therefore retains only the checked count.
        "matched_oracle_count": len(task_ids),
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Analyze paired full-history versus Engraphis code-agent runs."
    )
    parser.add_argument("--full-history", required=True)
    parser.add_argument("--engraphis", required=True)
    parser.add_argument("--iterations", type=int, default=5000)
    parser.add_argument("--seed", type=int, default=20260730)
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    try:
        report = evaluate(
            load_runs(args.full_history, condition="full_history"),
            load_runs(args.engraphis, condition="engraphis"),
            iterations=args.iterations,
            seed=args.seed,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    payload = json.dumps(report, indent=2, sort_keys=True, allow_nan=False)
    if args.output:
        Path(args.output).write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
