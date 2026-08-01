"""Deterministic scale measurements for the production NumPy vector index.

This is deliberately narrower than :mod:`eval.performance`: it measures only the
store-backed ``NumpyVectorIndex`` scan so an operator can map corpus-size envelopes
on their own machine. Timings are observational data, never a universal capacity
limit or a CI acceptance gate.

Usage::

    python -m eval.vector_scale --sizes 1000,10000,100000 --queries 20 --iterations 3 --json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import statistics
import sys
import time
from typing import Optional

import numpy as np

from engraphis.backends.vector_numpy import NumpyVectorIndex
from engraphis.core.interfaces import MemoryRecord, MemoryType, Scope, SearchFilter
from engraphis.core.store import Store


SCHEMA = "engraphis-vector-scale/v1"


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _latency_ms(values: list[float]) -> dict[str, float]:
    return {
        "min": round(min(values, default=0.0), 3),
        "p50": round(_percentile(values, 0.50), 3),
        "p95": round(_percentile(values, 0.95), 3),
        "p99": round(_percentile(values, 0.99), 3),
        "max": round(max(values, default=0.0), 3),
        "mean": round(statistics.fmean(values) if values else 0.0, 3),
    }


def _normalized_random(rng: np.random.Generator, count: int, dim: int) -> np.ndarray:
    vectors = rng.standard_normal((count, dim)).astype(np.float32)
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    return vectors / np.maximum(norms, np.finfo(np.float32).tiny)


def parse_sizes(value: str) -> list[int]:
    try:
        sizes = [int(part.strip()) for part in value.split(",") if part.strip()]
    except ValueError as exc:
        raise ValueError("sizes must be a comma-separated list of positive integers") from exc
    if not sizes or any(size <= 0 for size in sizes) or len(set(sizes)) != len(sizes):
        raise ValueError("sizes must be distinct positive integers")
    return sorted(sizes)


def _result_hash(results: list[list[tuple[str, float]]]) -> str:
    stable = [[memory_id for memory_id, _ in result] for result in results]
    encoded = json.dumps(stable, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def run(
    sizes: list[int],
    *,
    dim: int = 256,
    queries: int = 20,
    iterations: int = 3,
    warmups: int = 1,
    k: int = 10,
    seed: int = 20260731,
) -> dict:
    """Return JSON-safe, machine-specific direct-index scale measurements."""
    sizes = parse_sizes(",".join(str(size) for size in sizes))
    if dim <= 0 or queries <= 0 or iterations <= 0 or warmups < 0 or k <= 0:
        raise ValueError("dim, queries, iterations, and k must be positive; warmups cannot be negative")

    largest = max(sizes)
    rng = np.random.default_rng(seed)
    vectors = _normalized_random(rng, largest, dim)
    query_vectors = _normalized_random(rng, queries, dim)
    vector_sha256 = hashlib.sha256(vectors.tobytes()).hexdigest()
    query_sha256 = hashlib.sha256(query_vectors.tobytes()).hexdigest()
    rows = []

    for size in sizes:
        store = Store(":memory:")
        workspace_id = store.get_or_create_workspace("vector-scale")
        repo_id = store.get_or_create_repo(workspace_id, "deterministic-corpus")
        index = NumpyVectorIndex(store)
        records = [
            MemoryRecord(
                id=f"mem_scale_{number:09d}",
                content=f"Deterministic vector scale record {number}",
                mtype=MemoryType.EPISODIC,
                scope=Scope.REPO,
                workspace_id=workspace_id,
                repo_id=repo_id,
                embedding=vectors[number],
            )
            for number in range(size)
        ]
        for record in records:
            store.add_memory(record, audit=False, commit=False)
        store.conn.commit()
        search_filter = SearchFilter(workspace_id=workspace_id, repo_id=repo_id)

        for _ in range(warmups):
            for query in query_vectors:
                index.search(query, k, filter=search_filter)

        latencies = []
        results = []
        for _ in range(iterations):
            for query in query_vectors:
                started = time.perf_counter_ns()
                result = index.search(query, k, filter=search_filter)
                latencies.append((time.perf_counter_ns() - started) / 1_000_000)
                results.append(result)
        rows.append({
            "corpus_size": size,
            "timed_searches": len(latencies),
            "latency_ms": _latency_ms(latencies),
            "result_ids_sha256": _result_hash(results),
        })
        store.close()

    return {
        "schema": SCHEMA,
        "measurement": {
            "kind": "direct_numpy_vector_search",
            "timing_interpretation": "machine-specific observed envelope, not a pass/fail limit",
        },
        "config": {
            "sizes": sizes,
            "dimension": dim,
            "queries": queries,
            "iterations": iterations,
            "warmups": warmups,
            "k": k,
            "seed": seed,
        },
        "inputs": {
            "vectors_sha256": vector_sha256,
            "queries_sha256": query_sha256,
        },
        "environment": {
            "python": platform.python_version(),
            "platform": platform.system().lower(),
            "architecture": platform.machine().lower(),
            "numpy": np.__version__,
            "vector_backend": "NumpyVectorIndex",
        },
        "results": rows,
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Measure deterministic corpus-size envelopes for NumpyVectorIndex."
    )
    parser.add_argument("--sizes", default="1000,10000,100000")
    parser.add_argument("--dim", type=int, default=256)
    parser.add_argument("--queries", type=int, default=20)
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument("--seed", type=int, default=20260731)
    parser.add_argument("--json", action="store_true", help="print the complete JSON report")
    args = parser.parse_args(argv)
    report = run(
        parse_sizes(args.sizes),
        dim=args.dim,
        queries=args.queries,
        iterations=args.iterations,
        warmups=args.warmups,
        k=args.k,
        seed=args.seed,
    )
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"{SCHEMA}: direct NumPy vector search (machine-specific envelope)")
        for row in report["results"]:
            latency = row["latency_ms"]
            print(
                f"  n={row['corpus_size']}: p50={latency['p50']:.3f}ms "
                f"p95={latency['p95']:.3f}ms p99={latency['p99']:.3f}ms "
                f"({row['timed_searches']} searches)"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
