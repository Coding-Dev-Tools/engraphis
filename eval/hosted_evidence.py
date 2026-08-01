"""Aggregate-only, reproducible evidence for hosted productivity benchmarks.

This module never starts a model or reads task text into its output.  It accepts
the private ``detail`` portion returned by :mod:`eval.productivity`, performs
strict paired analysis, and emits a public artifact containing only counts,
rates, provenance hashes, and confidence intervals.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
import platform
import random
import statistics
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence, Union


SCHEMA = "engraphis-hosted-evidence/v1"
DEFAULT_REQUIRED_USAGE = ("input_tokens", "output_tokens", "total_tokens", "latency_ms")
STRATEGIES = ("full_history", "retrieval", "adaptive")
USAGE_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
    "latency_ms",
)
PUBLIC_EXPERIMENT_FIELDS = frozenset({
    "stage",
    "model",
    "reasoning_effort",
    "tasks",
    "repetitions",
    "projected_max_hosted_calls",
    "authorized_max_hosted_calls",
    "retries",
    "timeout_seconds",
    "sandbox",
    "fresh_thread_per_attempt",
    "strategy_schedule",
    "calls_started",
})


def canonical_json(value: Any) -> str:
    """Return strict, portable JSON suitable for hashing."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def dataset_provenance(path: Union[str, Path]) -> dict[str, Union[str, int]]:
    """Hash dataset bytes without retaining its path, records, or identifiers."""
    source = Path(path)
    digest = hashlib.sha256()
    size = 0
    with source.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    return {"sha256": digest.hexdigest(), "bytes": size}


def repository_provenance(repo: Union[str, Path]) -> dict[str, Union[str, bool]]:
    """Return commit and a content-only dirty-state fingerprint.

    The digest includes tracked diffs and untracked file bytes but never exposes
    filenames or patch text in the public artifact.
    """
    root = str(Path(repo))
    try:
        commit = subprocess.check_output(
            ["git", "-C", root, "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL,
        ).strip()
        status = subprocess.check_output(
            ["git", "-C", root, "status", "--porcelain=v1", "-z"], stderr=subprocess.DEVNULL,
        )
        patch = subprocess.check_output(
            ["git", "-C", root, "diff", "--binary", "HEAD", "--"], stderr=subprocess.DEVNULL,
        )
        untracked = subprocess.check_output(
            ["git", "-C", root, "ls-files", "--others", "--exclude-standard", "-z"],
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        return {"commit": "unknown", "dirty": True, "dirty_patch_sha256": _sha256_text("unavailable")}

    dirty_hasher = hashlib.sha256()
    dirty_hasher.update(status)
    dirty_hasher.update(patch)
    for relative_bytes in sorted(item for item in untracked.split(b"\0") if item):
        relative = relative_bytes.decode("utf-8", "surrogateescape")
        candidate = Path(root, relative)
        if candidate.is_file():
            dirty_hasher.update(relative_bytes)
            dirty_hasher.update(b"\0")
            dirty_hasher.update(candidate.read_bytes())
    return {
        "commit": commit or "unknown",
        "dirty": bool(status),
        "dirty_patch_sha256": dirty_hasher.hexdigest(),
    }


def environment_provenance() -> dict[str, Optional[str]]:
    """Return the host details relevant to a hosted-Codex repetition."""
    try:
        codex_version = importlib.metadata.version("openai-codex")
    except importlib.metadata.PackageNotFoundError:
        codex_version = None
    return {
        "python": sys.version.split()[0],
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "openai_codex": codex_version,
    }


def utc_timestamp(value: Optional[datetime] = None) -> str:
    """Return a seconds-precision UTC timestamp, injectable for reproducible artifacts."""
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return current.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a non-negative finite number")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{label} must be a non-negative finite number")
    return number


def _mean(values: Iterable[float]) -> float:
    items = list(values)
    return sum(items) / len(items) if items else 0.0


def paired_bootstrap_95(
    pairs: Sequence[tuple[float, float]], *, iterations: int = 5000, seed: int = 20260731,
) -> dict[str, Union[int, float]]:
    """Deterministic percentile CI for mean ``candidate - baseline`` differences."""
    if type(iterations) is not int or iterations <= 0:
        raise ValueError("iterations must be a positive integer")
    if type(seed) is not int:
        raise ValueError("seed must be an integer")
    deltas = [candidate - baseline for candidate, baseline in pairs]
    point = _mean(deltas)
    rng = random.Random(seed)
    samples = sorted(_mean(rng.choice(deltas) for _ in deltas) for _ in range(iterations)) if deltas else [point]
    low_index = max(0, math.floor(0.025 * (len(samples) - 1)))
    high_index = min(len(samples) - 1, math.ceil(0.975 * (len(samples) - 1)))
    return {
        "delta": round(point, 6),
        "median_delta": round(float(statistics.median(deltas)), 6) if deltas else 0.0,
        "low": round(samples[low_index], 6),
        "high": round(samples[high_index], 6), "n": len(deltas), "iterations": iterations,
        "seed": seed, "confidence_level": 0.95,
    }


def paired_cluster_bootstrap_95(
    clusters: Mapping[str, Sequence[tuple[float, float]]], *, iterations: int = 5000,
    seed: int = 20260731,
) -> dict[str, Union[int, float]]:
    """Bootstrap task-level paired means so repetitions stay within their task cluster."""
    means: list[tuple[float, float]] = []
    for task_id in sorted(clusters):
        pairs = clusters[task_id]
        if not pairs:
            raise ValueError("every task cluster requires one or more paired observations")
        means.append((
            _mean(candidate for candidate, _ in pairs),
            _mean(baseline for _, baseline in pairs),
        ))
    return paired_bootstrap_95(means, iterations=iterations, seed=seed)


def _private_report(report: Mapping[str, Any]) -> Mapping[str, Any]:
    """Accept a direct private report or a ``{private, public}`` repetition envelope."""
    candidate = report.get("private", report)
    if not isinstance(candidate, Mapping):
        raise ValueError("each repetition requires a private productivity report")
    detail = candidate.get("detail")
    if not isinstance(detail, Mapping) or not detail:
        raise ValueError("each repetition requires private detail for paired evidence")
    return candidate


def _normalize_reports(
    reports: Sequence[Mapping[str, Any]], required_usage: Sequence[str], baseline: str,
) -> tuple[dict[str, list[dict[str, Any]]], int, tuple[str, ...]]:
    if not reports:
        raise ValueError("at least one repetition report is required")
    if baseline not in STRATEGIES:
        raise ValueError("baseline must be a known productivity strategy")
    if not required_usage or any(field not in USAGE_FIELDS for field in required_usage):
        raise ValueError("required_usage must name one or more known provider counters")
    # These counters feed the published paired comparisons and therefore may
    # never be made optional by a caller changing the coverage policy.
    required = tuple(dict.fromkeys((*required_usage, "total_tokens", "latency_ms")))

    output: dict[str, list[dict[str, Any]]] = {}
    expected_strategies: Optional[set[str]] = None
    expected_task_ids: Optional[set[str]] = None
    for repetition, report in enumerate(reports):
        detail = _private_report(report)["detail"]
        strategies = set(detail)
        if not strategies or not strategies.issubset(STRATEGIES):
            raise ValueError("private reports must use only known productivity strategies")
        if baseline not in strategies:
            raise ValueError("every repetition must include the baseline strategy")
        if expected_strategies is None:
            expected_strategies = strategies
        elif strategies != expected_strategies:
            raise ValueError("every repetition must contain the same strategies")
        by_strategy: dict[str, dict[str, dict[str, Any]]] = {}
        for strategy in sorted(strategies):
            rows = detail[strategy]
            if not isinstance(rows, list) or not rows:
                raise ValueError("every strategy must contain one or more private rows")
            indexed: dict[str, dict[str, Any]] = {}
            for row in rows:
                if not isinstance(row, Mapping):
                    raise ValueError("private detail rows must be objects")
                task_id = row.get("task_id")
                if not isinstance(task_id, str) or not task_id:
                    raise ValueError("private detail rows require task_id for pairing")
                if task_id in indexed:
                    raise ValueError("private detail rows have duplicate task IDs")
                if not isinstance(row.get("completed"), bool) or not isinstance(row.get("wrong_answer"), bool):
                    raise ValueError("private detail rows require boolean completion and mistake fields")
                if not isinstance(row.get("first_attempt_error"), bool) or not isinstance(
                    row.get("correction_attempted"), bool
                ):
                    raise ValueError("private detail rows require first-attempt and correction fields")
                memory_calls = _finite_number(row.get("memory_calls"), "memory_calls")
                agent_turns = _finite_number(row.get("agent_turns"), "agent_turns")
                provider = row.get("provider")
                if not isinstance(provider, Mapping):
                    raise ValueError("private detail rows require provider usage")
                usage: dict[str, Optional[float]] = {}
                for field in USAGE_FIELDS:
                    raw = provider.get(field)
                    usage[field] = None if raw is None else _finite_number(raw, f"provider {field}")
                if any(usage[field] is None for field in required):
                    raise ValueError("required provider usage counters are missing")
                indexed[task_id] = {
                    "completed": float(row["completed"]), "mistake": float(row["wrong_answer"]),
                    "first_completed": float(not row["first_attempt_error"]),
                    "correction": float(row["correction_attempted"]),
                    "memory_calls": memory_calls,
                    "agent_turns": agent_turns,
                    "usage": usage,
                }
            by_strategy[strategy] = indexed
        reference = set(by_strategy[baseline])
        if any(set(indexed) != reference for indexed in by_strategy.values()):
            raise ValueError("strategies must have exactly matched task IDs in every repetition")
        if expected_task_ids is None:
            expected_task_ids = reference
        elif reference != expected_task_ids:
            raise ValueError("every repetition must contain the same task IDs")
        for strategy, indexed in by_strategy.items():
            output.setdefault(strategy, []).extend(
                {"repetition": repetition, "task_id": task_id, **row}
                for task_id, row in indexed.items()
            )
    return output, len(reports), required


def _coverage(rows: Sequence[dict[str, Any]], field: str) -> dict[str, Union[int, float]]:
    available = sum(row["usage"][field] is not None for row in rows)
    total = len(rows)
    return {"available": available, "total": total, "rate": round(available / total, 6) if total else 0.0}


def _strategy_summary(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    coverage = {field: _coverage(rows, field) for field in USAGE_FIELDS}
    usage_mean = {
        field: (round(_mean(row["usage"][field] for row in rows if row["usage"][field] is not None), 6)
                if coverage[field]["available"] else None)
        for field in USAGE_FIELDS
    }
    usage_total = {
        field: (
            round(sum(row["usage"][field] for row in rows), 6)
            if coverage[field]["available"] == coverage[field]["total"]
            else None
        )
        for field in USAGE_FIELDS
    }
    usage_median = {
        field: (
            round(
                float(statistics.median(row["usage"][field] for row in rows)),
                6,
            )
            if coverage[field]["available"] == coverage[field]["total"]
            else None
        )
        for field in USAGE_FIELDS
    }
    return {
        "observations": len(rows),
        "first_attempt_completion_rate": round(
            _mean(row["first_completed"] for row in rows), 6
        ),
        "completion_rate": round(_mean(row["completed"] for row in rows), 6),
        "mistake_rate": round(_mean(row["mistake"] for row in rows), 6),
        "corrections": int(sum(row["correction"] for row in rows)),
        "agent_turns": int(sum(row["agent_turns"] for row in rows)),
        "memory_calls": int(sum(row["memory_calls"] for row in rows)),
        "provider_usage_mean": usage_mean,
        "provider_usage_median": usage_median,
        "provider_usage_total": usage_total,
        "usage_coverage": coverage,
    }


def _task_cluster_bootstrap(
    candidate: Mapping[tuple[int, str], Mapping[str, Any]],
    reference: Mapping[tuple[int, str], Mapping[str, Any]],
    ordered: Sequence[tuple[int, str]], value: Callable[[Mapping[str, Any]], float], *,
    iterations: int, seed: int,
) -> dict[str, Union[int, float]]:
    clusters: dict[str, list[tuple[float, float]]] = {}
    for key in ordered:
        clusters.setdefault(key[1], []).append((value(candidate[key]), value(reference[key])))
    return paired_cluster_bootstrap_95(clusters, iterations=iterations, seed=seed)


def aggregate_reports(
    reports: Sequence[Mapping[str, Any]], *, baseline: str = "full_history",
    required_usage: Sequence[str] = DEFAULT_REQUIRED_USAGE, iterations: int = 5000,
    seed: int = 20260731,
) -> dict[str, Any]:
    """Aggregate private repetitions without exposing their task-level records."""
    normalized, repetitions, required = _normalize_reports(reports, required_usage, baseline)
    strategies = {strategy: _strategy_summary(rows) for strategy, rows in sorted(normalized.items())}
    reference = {(row["repetition"], row["task_id"]): row for row in normalized[baseline]}
    comparisons: dict[str, Any] = {}
    for strategy, rows in sorted(normalized.items()):
        if strategy == baseline:
            continue
        candidate = {(row["repetition"], row["task_id"]): row for row in rows}
        if set(candidate) != set(reference):
            raise ValueError("strategies must have matched repetition/task pairs")
        ordered = sorted(reference)
        comparisons[strategy] = {
            "baseline": baseline,
            "delta_direction": f"{strategy}_minus_{baseline}",
            "completion_rate": _task_cluster_bootstrap(
                candidate, reference, ordered, lambda row: row["completed"],
                iterations=iterations, seed=seed,
            ),
            "first_attempt_completion_rate": _task_cluster_bootstrap(
                candidate, reference, ordered, lambda row: row["first_completed"],
                iterations=iterations,
                seed=seed,
            ),
            "mistake_rate": _task_cluster_bootstrap(
                candidate, reference, ordered, lambda row: row["mistake"],
                iterations=iterations, seed=seed,
            ),
            "correction_rate": _task_cluster_bootstrap(
                candidate, reference, ordered, lambda row: row["correction"],
                iterations=iterations,
                seed=seed,
            ),
            "total_tokens": _task_cluster_bootstrap(
                candidate, reference, ordered, lambda row: row["usage"]["total_tokens"],
                iterations=iterations, seed=seed,
            ),
            "latency_ms": _task_cluster_bootstrap(
                candidate, reference, ordered, lambda row: row["usage"]["latency_ms"],
                iterations=iterations, seed=seed,
            ),
        }
    return {
        "repetitions": repetitions,
        "baseline": baseline,
        "required_usage": list(required),
        "strategies": strategies,
        "paired_bootstrap": comparisons,
    }


def build_public_evidence(
    reports: Sequence[Mapping[str, Any]], *, dataset_path: Union[str, Path], config: Mapping[str, Any],
    repo_path: Union[str, Path] = ".", baseline: str = "full_history",
    required_usage: Sequence[str] = DEFAULT_REQUIRED_USAGE, iterations: int = 5000,
    seed: int = 20260731, timestamp: Optional[datetime] = None,
    calls_started: Optional[int] = None,
) -> dict[str, Any]:
    """Build a content-free public artifact and attach its canonical checksum."""
    if not isinstance(config, Mapping):
        raise ValueError("config must be an object")
    aggregate = aggregate_reports(
        reports, baseline=baseline, required_usage=required_usage, iterations=iterations, seed=seed,
    )
    if calls_started is not None and (
        isinstance(calls_started, bool)
        or not isinstance(calls_started, int)
        or calls_started < 0
    ):
        raise ValueError("calls_started must be a non-negative integer")
    experiment = {
        field: config[field]
        for field in PUBLIC_EXPERIMENT_FIELDS
        if field in config and field != "calls_started"
    }
    experiment["calls_started"] = calls_started
    public = {
        "schema": SCHEMA,
        "created_at": utc_timestamp(timestamp),
        "experiment": experiment,
        "provenance": {
            "dataset": dataset_provenance(dataset_path),
            "repository": repository_provenance(repo_path),
            "environment": environment_provenance(),
            "config_sha256": _sha256_text(canonical_json(dict(config))),
        },
        **aggregate,
    }
    public["sha256"] = _sha256_text(canonical_json(public))
    return public


def public_json(evidence: Mapping[str, Any]) -> str:
    """Serialize a completed evidence artifact deterministically and verify its checksum."""
    _assert_public_schema(evidence)
    _assert_public_safe(evidence)
    copy = dict(evidence)
    observed = copy.pop("sha256", None)
    expected = _sha256_text(canonical_json(copy))
    if observed != expected:
        raise ValueError("public evidence checksum does not match its content")
    return canonical_json({**copy, "sha256": observed})


def _assert_public_safe(value: Any) -> None:
    """Reject accidental task-level fields even if a caller bypassed the builder."""
    forbidden = {
        "answer",
        "context",
        "detail",
        "evidence",
        "expected",
        "final_response",
        "history",
        "memory",
        "memories",
        "oracle",
        "prompt",
        "question",
        "response",
        "source_text",
        "task_id",
    }
    if isinstance(value, Mapping):
        if forbidden.intersection(value):
            raise ValueError("public evidence must not contain task-level content")
        for item in value.values():
            _assert_public_safe(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _assert_public_safe(item)


def _assert_public_schema(evidence: Mapping[str, Any]) -> None:
    """Reject checksum-valid data that is outside the aggregate evidence schema."""
    top_level = {
        "schema", "created_at", "experiment", "provenance", "repetitions", "baseline",
        "required_usage", "strategies", "paired_bootstrap", "sha256",
    }
    if not isinstance(evidence, Mapping) or set(evidence) != top_level:
        raise ValueError("public evidence has unexpected top-level fields")
    if evidence.get("schema") != SCHEMA:
        raise ValueError("public evidence has an unexpected schema")
    experiment = evidence.get("experiment")
    if not isinstance(experiment, Mapping) or not set(experiment).issubset(
        PUBLIC_EXPERIMENT_FIELDS
    ):
        raise ValueError("public evidence has unexpected experiment fields")
    provenance = evidence.get("provenance")
    if not isinstance(provenance, Mapping) or set(provenance) != {
        "dataset", "repository", "environment", "config_sha256",
    }:
        raise ValueError("public evidence has unexpected provenance fields")
    provenance_shapes = {
        "dataset": {"sha256", "bytes"},
        "repository": {"commit", "dirty", "dirty_patch_sha256"},
        "environment": {"python", "implementation", "platform", "openai_codex"},
    }
    for field, allowed in provenance_shapes.items():
        value = provenance[field]
        if not isinstance(value, Mapping) or set(value) != allowed:
            raise ValueError(f"public evidence has unexpected {field} provenance fields")
    strategies = evidence.get("strategies")
    if not isinstance(strategies, Mapping) or not set(strategies).issubset(STRATEGIES):
        raise ValueError("public evidence has unexpected strategy fields")
    summary_fields = {
        "observations", "first_attempt_completion_rate", "completion_rate",
        "mistake_rate", "corrections", "agent_turns", "memory_calls",
        "provider_usage_mean", "provider_usage_median", "provider_usage_total",
        "usage_coverage",
    }
    for summary in strategies.values():
        if not isinstance(summary, Mapping) or set(summary) != summary_fields:
            raise ValueError("public evidence has unexpected strategy summary fields")
        for usage_key in (
            "provider_usage_mean", "provider_usage_median", "provider_usage_total",
            "usage_coverage",
        ):
            if not isinstance(summary[usage_key], Mapping) or set(summary[usage_key]) != set(
                USAGE_FIELDS
            ):
                raise ValueError("public evidence has unexpected usage fields")
    comparisons = evidence.get("paired_bootstrap")
    if not isinstance(comparisons, Mapping) or not set(comparisons).issubset(STRATEGIES):
        raise ValueError("public evidence has unexpected comparison fields")
    comparison_fields = {
        "baseline", "delta_direction", "completion_rate",
        "first_attempt_completion_rate", "mistake_rate", "correction_rate",
        "total_tokens", "latency_ms",
    }
    interval_fields = {
        "delta", "median_delta", "low", "high", "n", "iterations", "seed",
        "confidence_level",
    }
    for comparison in comparisons.values():
        if not isinstance(comparison, Mapping) or set(comparison) != comparison_fields:
            raise ValueError("public evidence has unexpected comparison summary fields")
        for field in comparison_fields - {"baseline", "delta_direction"}:
            interval = comparison[field]
            if not isinstance(interval, Mapping) or set(interval) != interval_fields:
                raise ValueError("public evidence has unexpected confidence interval fields")
