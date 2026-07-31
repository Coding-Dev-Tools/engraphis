"""Reproducible recall latency, quality, and context-efficiency benchmark.

This measures the complete shipped recall path after ingestion: semantic + lexical +
graph candidate generation, fusion, scoring, reranking, context packing, and temporal
visibility. It deliberately disables reinforcement during timed reads so benchmark
iterations do not change the data they measure.

The deterministic embedder makes the default run offline and repeatable. Latency remains
machine-dependent, so reports include the runtime/backend/corpus shape and never compare
numbers from unlike environments.  The optional acceptance settings add cold/warm samples,
bounded concurrency, and independently-created worker processes without changing the small,
single-process default.

Usage::

    python -m eval.performance --dataset eval/datasets/codemem.jsonl --k 5
    python -m eval.performance --dataset eval/datasets/codemem.jsonl --iterations 20 --json
    python -m eval.performance --dataset cases.jsonl --concurrency 4 --processes 5
"""
from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from engraphis.backends import DeterministicEmbedder, NumpyVectorIndex
from engraphis.backends.reranker import IdentityReranker
from engraphis.core.context import RegexTokenCounter
from engraphis.core.engine import MemoryEngine
from engraphis.core.interfaces import MemoryType, Scope, SearchFilter
from engraphis.core.retrieval_policy import CANDIDATE_DEPTH_MODES, RETRIEVAL_PROFILES
from engraphis.core.store import Store
from eval import metrics
from eval.harness import load_dataset


SUPPORTED_CONCURRENCY = (1, 4, 16)


@dataclass(frozen=True)
class AcceptanceConfig:
    """Acceptance-protocol controls, validated before an expensive run starts."""

    concurrency: int = 1
    processes: int = 1
    minimum_queries: int = 0
    canonical: bool = False

    def validate(self, question_count: int) -> None:
        if self.concurrency not in SUPPORTED_CONCURRENCY:
            choices = ", ".join(str(value) for value in SUPPORTED_CONCURRENCY)
            raise ValueError(f"concurrency must be one of: {choices}")
        if self.processes < 1:
            raise ValueError("processes must be at least 1")
        if self.minimum_queries < 0:
            raise ValueError("minimum_queries must be at least 0")
        if question_count < self.minimum_queries:
            raise ValueError(
                f"dataset has {question_count} queries; minimum_queries requires "
                f"at least {self.minimum_queries}"
            )
        if self.canonical and question_count < 1000:
            raise ValueError("canonical acceptance requires at least 1000 queries")
        if self.canonical and self.processes < 5:
            raise ValueError("canonical acceptance requires at least 5 processes")


@dataclass
class _Measurements:
    cold_latencies_ms: list[float]
    warm_latencies_ms: list[float]
    context_tokens: list[int]
    source_tokens: list[int]
    full_payload_tokens: list[int]
    compact_payload_tokens: list[int]
    candidate_depths: list[int]
    quality: list[dict]


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _latency_summary(values: list[float]) -> dict[str, float]:
    return {
        "min": round(min(values, default=0.0), 3),
        "p50": round(_percentile(values, 0.50), 3),
        "p95": round(_percentile(values, 0.95), 3),
        "p99": round(_percentile(values, 0.99), 3),
        "max": round(max(values, default=0.0), 3),
    }


def _question_count(dataset: list[dict]) -> int:
    return sum(len(case.get("questions") or []) for case in dataset)


def _process_rss_bytes() -> Optional[int]:
    """Best-effort peak RSS, normalized to bytes when the platform exposes it."""
    try:
        import resource  # Unix only; intentionally optional on Windows.
    except ImportError:
        return None
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # macOS reports bytes, while Linux and the BSDs report KiB.
    return int(usage if platform.system() == "Darwin" else usage * 1024)


def _storage_bytes(store: Store) -> Optional[int]:
    """Report allocated SQLite storage even when the benchmark uses ``:memory:``."""
    try:
        page_count = store.conn.execute("PRAGMA page_count").fetchone()[0]
        page_size = store.conn.execute("PRAGMA page_size").fetchone()[0]
    except Exception:  # pragma: no cover - defensive for non-SQLite Store adapters.
        return None
    return int(page_count) * int(page_size)


def _serialized_tokens(payload: dict, counter: RegexTokenCounter) -> int:
    """Count the exact canonical JSON payload used for compact/full comparison."""
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return counter(encoded)


def _compact_provenance(value: object) -> dict:
    """Match the bounded provenance identity emitted by compact recall responses."""
    if not isinstance(value, dict):
        return {}
    keys = ("source", "source_kind", "trusted", "kind", "origin")
    return {key: value[key] for key in keys if key in value}


def _compact_payload(result) -> dict:
    """Mirror ``engraphis_recall_context`` without serializing unpacked candidates."""
    by_id = {str(chunk.get("id") or ""): chunk for chunk in result.chunks}
    sources = []
    for ordinal, packed in enumerate(result.packed_chunks, start=1):
        chunk = by_id.get(str(packed.id or ""), {})
        source = {
            "n": ordinal,
            "id": packed.id,
            "tokens": packed.tokens,
        }
        if chunk.get("title"):
            source["title"] = chunk["title"]
        provenance = _compact_provenance(chunk.get("provenance"))
        if provenance:
            source["provenance"] = provenance
        if packed.truncated:
            source["truncated"] = True
        if packed.reason and packed.reason not in {"full", "summary"}:
            source["reason"] = packed.reason
        sources.append(source)
    usage = vars(result.usage) if result.usage else {}
    return {"context": result.context, "sources": sources, "usage": usage}


def _measure_recall(
    engine: MemoryEngine,
    question: dict,
    search_filter: SearchFilter,
    *,
    k: int,
    candidate_k: int,
    candidate_depth: str,
    token_budget: int,
    retrieval_profile: str,
) -> tuple[dict, float]:
    started = time.perf_counter_ns()
    result = engine.recall_engine.recall(
        question["q"],
        search_filter,
        k=k,
        candidate_k=candidate_k,
        candidate_depth=candidate_depth,
        reinforce=False,
        token_budget=token_budget,
        retrieval_profile=retrieval_profile,
    )
    return result, (time.perf_counter_ns() - started) / 1_000_000


def _measure_batch(
    engine: MemoryEngine,
    questions: list[dict],
    search_filter: SearchFilter,
    *,
    k: int,
    candidate_k: int,
    candidate_depth: str,
    token_budget: int,
    retrieval_profile: str,
    concurrency: int,
) -> list[tuple[dict, float]]:
    if concurrency == 1:
        return [
            _measure_recall(
                engine,
                question,
                search_filter,
                k=k,
                candidate_k=candidate_k,
                candidate_depth=candidate_depth,
                token_budget=token_budget,
                retrieval_profile=retrieval_profile,
            )
            for question in questions
        ]
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [
            executor.submit(
                _measure_recall,
                engine,
                question,
                search_filter,
                k=k,
                candidate_k=candidate_k,
                candidate_depth=candidate_depth,
                token_budget=token_budget,
                retrieval_profile=retrieval_profile,
            )
            for question in questions
        ]
        return [future.result() for future in futures]


def _run_single(
    dataset: list[dict],
    *,
    k: int,
    candidate_k: int,
    candidate_depth: str,
    dim: int,
    warmups: int,
    iterations: int,
    filler_memories: int,
    token_budget: int,
    retrieval_profile: str,
    config: AcceptanceConfig,
    process_number: int,
    embedder: Optional[DeterministicEmbedder] = None,
) -> tuple[dict, _Measurements]:
    embedder = embedder or DeterministicEmbedder(dim=dim)
    store = Store(":memory:")
    workspace_id = store.get_or_create_workspace("performance")
    repo_id = store.get_or_create_repo(workspace_id, "corpus")
    index = NumpyVectorIndex(store)
    engine = MemoryEngine(store, embedder, index, IdentityReranker())
    search_filter = SearchFilter(
        workspace_id=workspace_id,
        repo_id=repo_id,
        include_ancestors=True,
    )

    id_to_tags: dict[str, list[str]] = {}
    id_to_text: dict[str, str] = {}
    questions: list[dict] = []
    for case_number, case in enumerate(dataset):
        case_id = str(case.get("id") or f"case-{case_number}")
        for memory_number, memory in enumerate(case.get("memories") or []):
            memory_id = engine.remember(
                memory["text"],
                workspace_id=workspace_id,
                repo_id=repo_id,
                mtype=MemoryType.EPISODIC,
                scope=Scope.REPO,
                resolve_conflicts=False,
            )
            tag = str(memory.get("tag") or f"memory-{memory_number}")
            id_to_tags.setdefault(memory_id, []).append(f"{case_id}:{tag}")
            id_to_text[memory_id] = memory["text"]
        for question in case.get("questions") or []:
            questions.append({
                "q": question["q"],
                "answer": question.get("answer", ""),
                "supporting": [
                    f"{case_id}:{tag}" for tag in question.get("supporting", [])
                ],
            })

    for number in range(filler_memories):
        engine.remember(
            "Synthetic benchmark filler %06d records unrelated deterministic context "
            "for corpus scaling." % number,
            workspace_id=workspace_id,
            repo_id=repo_id,
            mtype=MemoryType.EPISODIC,
            scope=Scope.REPO,
            resolve_conflicts=False,
        )

    cold = _measure_batch(
        engine,
        questions,
        search_filter,
        k=k,
        candidate_k=candidate_k,
        candidate_depth=candidate_depth,
        token_budget=token_budget,
        retrieval_profile=retrieval_profile,
        concurrency=config.concurrency,
    )
    for _ in range(warmups):
        _measure_batch(
            engine,
            questions,
            search_filter,
            k=k,
            candidate_k=candidate_k,
            candidate_depth=candidate_depth,
            token_budget=token_budget,
            retrieval_profile=retrieval_profile,
            concurrency=config.concurrency,
        )

    measurements = _Measurements([], [], [], [], [], [], [], [])
    counter = RegexTokenCounter()
    for iteration in range(iterations):
        for question_number, (result, latency_ms) in enumerate(_measure_batch(
            engine,
            questions,
            search_filter,
            k=k,
            candidate_k=candidate_k,
            candidate_depth=candidate_depth,
            token_budget=token_budget,
            retrieval_profile=retrieval_profile,
            concurrency=config.concurrency,
        )):
            measurements.warm_latencies_ms.append(latency_ms)
            if iteration != 0:
                continue
            retrieved_ids = [chunk["id"] for chunk in result.chunks]
            retrieved_tags = [
                tag for memory_id in retrieved_ids for tag in id_to_tags.get(memory_id, [])
            ]
            retrieved_texts = [id_to_text.get(memory_id, "") for memory_id in retrieved_ids]
            usage = result.usage
            measurements.context_tokens.append(usage.context_tokens if usage else 0)
            measurements.source_tokens.append(usage.source_tokens if usage else 0)
            full_payload = {"context": result.context, "memories": result.chunks}
            compact_payload = _compact_payload(result)
            measurements.full_payload_tokens.append(_serialized_tokens(full_payload, counter))
            measurements.compact_payload_tokens.append(
                _serialized_tokens(compact_payload, counter)
            )
            measurements.candidate_depths.append(result.candidate_k_used)
            question = questions[question_number]
            measurements.quality.append({
                "question": question_number,
                "recall_at_k": metrics.recall_at_k(retrieved_tags, question["supporting"]),
                "hit_at_k": metrics.hit_at_k(retrieved_tags, question["supporting"]),
                "answer_token_recall": metrics.answer_token_recall(
                    retrieved_texts, question["answer"]
                ),
            })

    measurements.cold_latencies_ms = [latency_ms for _, latency_ms in cold]
    process_resources = {
        "process": process_number,
        "rss_bytes": _process_rss_bytes(),
        "storage_bytes": _storage_bytes(store),
    }
    corpus = {
        "dataset_cases": len(dataset),
        "memories": store.conn.execute("SELECT COUNT(*) AS n FROM memories").fetchone()["n"],
        "questions": len(questions),
        "filler_memories": filler_memories,
    }
    environment = {
        "python": platform.python_version(),
        "platform": platform.system().lower(),
        "architecture": platform.machine().lower(),
        "embedder": type(embedder).__name__,
        "vector_backend": type(index).__name__,
    }
    store.close()
    return {"corpus": corpus, "environment": environment, "resources": process_resources}, measurements


def _build_report(
    base: dict,
    measurements: list[_Measurements],
    *,
    k: int,
    candidate_k: int,
    candidate_depth: str,
    warmups: int,
    iterations: int,
    token_budget: int,
    retrieval_profile: str,
    config: AcceptanceConfig,
    question_count: int,
    resources: list[dict],
) -> dict:
    cold_latencies = [value for item in measurements for value in item.cold_latencies_ms]
    warm_latencies = [value for item in measurements for value in item.warm_latencies_ms]
    context_tokens = [value for item in measurements for value in item.context_tokens]
    source_tokens = [value for item in measurements for value in item.source_tokens]
    full_payload_tokens = [value for item in measurements for value in item.full_payload_tokens]
    compact_payload_tokens = [value for item in measurements for value in item.compact_payload_tokens]
    candidate_depths = [value for item in measurements for value in item.candidate_depths]
    quality = [value for item in measurements for value in item.quality]
    full_total = sum(full_payload_tokens)
    compact_total = sum(compact_payload_tokens)
    saved_total = full_total - compact_total
    savings_ratios = [
        1.0 - compact / max(1, full)
        for full, compact in zip(full_payload_tokens, compact_payload_tokens)
    ]
    count = max(len(quality), 1)
    rss_values = [item["rss_bytes"] for item in resources if item["rss_bytes"] is not None]
    storage_values = [
        item["storage_bytes"] for item in resources if item["storage_bytes"] is not None
    ]
    warm_summary = _latency_summary(warm_latencies)

    return {
        "schema": "engraphis-performance/v1",
        "environment": base["environment"],
        "corpus": base["corpus"],
        "run": {
            "k": k,
            "candidate_k": candidate_k,
            "candidate_depth": candidate_depth,
            "actual_candidate_k": {
                "min": min(candidate_depths, default=0),
                "max": max(candidate_depths, default=0),
                "mean": round(sum(candidate_depths) / max(len(candidate_depths), 1), 2),
            },
            "warmups": warmups,
            "iterations": iterations,
            # Kept for compatibility: these are the warm, steady-state timed recalls.
            "timed_recalls": len(warm_latencies),
            "cold_timed_recalls": len(cold_latencies),
            "warm_timed_recalls": len(warm_latencies),
            "token_budget": token_budget,
            "retrieval_profile": retrieval_profile,
        },
        "acceptance": {
            "concurrency": config.concurrency,
            "independent_processes": config.processes,
            "minimum_queries": config.minimum_queries,
            "canonical": config.canonical,
            "query_count": question_count,
            "valid": True,
        },
        "quality": {
            "recall_at_k": round(sum(item["recall_at_k"] for item in quality) / count, 4),
            "hit_at_k": round(sum(item["hit_at_k"] for item in quality) / count, 4),
            "answer_token_recall": round(
                sum(item["answer_token_recall"] for item in quality) / count, 4
            ),
        },
        "context": {
            "mean_tokens": round(sum(context_tokens) / max(len(context_tokens), 1), 2),
            "max_tokens": max(context_tokens, default=0),
            "mean_source_tokens": round(
                sum(source_tokens) / max(len(source_tokens), 1), 2
            ),
            "full_serialized_payload_tokens": full_total,
            "compact_serialized_payload_tokens": compact_total,
            "saved_serialized_payload_tokens": saved_total,
            "serialized_payload_savings_ratio": round(
                saved_total / max(1, full_total), 4
            ),
            # Retain the original median measure for existing JSON consumers.
            "median_serialized_payload_savings_ratio": round(
                statistics.median(savings_ratios) if savings_ratios else 0.0, 4
            ),
            "token_counter": "engraphis.regex.v1",
        },
        "latency_ms": {
            **warm_summary,
            "cold": _latency_summary(cold_latencies),
            "warm": warm_summary,
        },
        "resources": {
            "processes": resources,
            "max_process_rss_bytes": max(rss_values, default=None),
            "max_storage_bytes": max(storage_values, default=None),
        },
        "detail": quality,
    }


def run(
    dataset: list[dict],
    *,
    k: int = 5,
    candidate_k: int = 50,
    candidate_depth: str = "fixed",
    dim: int = 256,
    warmups: int = 1,
    iterations: int = 5,
    filler_memories: int = 0,
    token_budget: int = 1500,
    retrieval_profile: str = "balanced",
    embedder: Optional[DeterministicEmbedder] = None,
    concurrency: int = 1,
    processes: int = 1,
    minimum_queries: int = 0,
    canonical: bool = False,
) -> dict:
    """Benchmark recall and return a JSON-safe report.

    Existing callers keep the single-process deterministic path.  ``processes`` creates
    isolated in-memory corpora in child processes; a caller-provided embedder is therefore
    intentionally limited to the established single-process API.
    """
    k = max(1, int(k))
    candidate_k = max(1, int(candidate_k))
    candidate_depth = str(candidate_depth or "").strip().casefold()
    if candidate_depth not in CANDIDATE_DEPTH_MODES:
        choices = ", ".join(sorted(CANDIDATE_DEPTH_MODES))
        raise ValueError(f"candidate_depth must be one of: {choices}")
    warmups = max(0, int(warmups))
    iterations = max(1, int(iterations))
    filler_memories = max(0, int(filler_memories))
    token_budget = max(0, int(token_budget))
    retrieval_profile = str(retrieval_profile or "").strip().casefold()
    if retrieval_profile not in RETRIEVAL_PROFILES:
        choices = ", ".join(sorted(RETRIEVAL_PROFILES))
        raise ValueError(f"retrieval_profile must be one of: {choices}")
    if canonical:
        raise ValueError("canonical acceptance requires run_acceptance_matrix")
    config = AcceptanceConfig(
        concurrency=int(concurrency),
        processes=int(processes),
        minimum_queries=int(minimum_queries),
        canonical=False,
    )
    question_count = _question_count(dataset)
    config.validate(question_count)
    if embedder is not None and config.processes != 1:
        raise ValueError("a custom embedder is only supported with processes=1")

    if config.processes == 1:
        base, measurement = _run_single(
            dataset,
            k=k,
            candidate_k=candidate_k,
            candidate_depth=candidate_depth,
            dim=dim,
            warmups=warmups,
            iterations=iterations,
            filler_memories=filler_memories,
            token_budget=token_budget,
            retrieval_profile=retrieval_profile,
            config=config,
            process_number=0,
            embedder=embedder,
        )
        return _build_report(
            base,
            [measurement],
            k=k,
            candidate_k=candidate_k,
            candidate_depth=candidate_depth,
            warmups=warmups,
            iterations=iterations,
            token_budget=token_budget,
            retrieval_profile=retrieval_profile,
            config=config,
            question_count=question_count,
            resources=[base["resources"]],
        )

    worker_args = {
        "k": k,
        "candidate_k": candidate_k,
        "candidate_depth": candidate_depth,
        "dim": dim,
        "warmups": warmups,
        "iterations": iterations,
        "filler_memories": filler_memories,
        "token_budget": token_budget,
        "retrieval_profile": retrieval_profile,
        "config": config,
    }
    with ProcessPoolExecutor(max_workers=config.processes) as executor:
        futures = [
            executor.submit(_run_single, dataset, process_number=number, **worker_args)
            for number in range(config.processes)
        ]
        process_results = [future.result() for future in futures]
    base, _ = process_results[0]
    return _build_report(
        base,
        [measurement for _, measurement in process_results],
        k=k,
        candidate_k=candidate_k,
        candidate_depth=candidate_depth,
        warmups=warmups,
        iterations=iterations,
        token_budget=token_budget,
        retrieval_profile=retrieval_profile,
        config=config,
        question_count=question_count,
        resources=[result["resources"] for result, _ in process_results],
    )


def _canonical_matrix_concurrencies(concurrencies: Optional[list[int]]) -> tuple[int, ...]:
    requested = tuple(SUPPORTED_CONCURRENCY if concurrencies is None else concurrencies)
    if len(requested) != len(SUPPORTED_CONCURRENCY) or set(requested) != set(
        SUPPORTED_CONCURRENCY
    ):
        choices = ", ".join(str(value) for value in SUPPORTED_CONCURRENCY)
        raise ValueError(f"canonical acceptance matrix must include every concurrency: {choices}")
    return SUPPORTED_CONCURRENCY


def run_acceptance_matrix(
    dataset: list[dict],
    *,
    k: int = 5,
    candidate_k: int = 50,
    candidate_depth: str = "fixed",
    dim: int = 256,
    warmups: int = 1,
    iterations: int = 5,
    filler_memories: int = 0,
    token_budget: int = 1500,
    retrieval_profile: str = "balanced",
    processes: int = 5,
    minimum_queries: int = 1000,
    concurrencies: Optional[list[int]] = None,
) -> dict:
    """Run the complete canonical 1/4/16-concurrency acceptance protocol.

    ``run`` remains the backwards-compatible per-slice primitive.  This wrapper is
    intentionally the only public API that declares a complete canonical result, and
    validates every requirement before starting the expensive process matrix.
    """
    matrix = _canonical_matrix_concurrencies(concurrencies)
    effective_minimum = max(1000, int(minimum_queries))
    question_count = _question_count(dataset)
    AcceptanceConfig(
        concurrency=1,
        processes=int(processes),
        minimum_queries=effective_minimum,
        canonical=True,
    ).validate(question_count)

    slices = {}
    for concurrency in matrix:
        slices[str(concurrency)] = run(
            dataset,
            k=k,
            candidate_k=candidate_k,
            candidate_depth=candidate_depth,
            dim=dim,
            warmups=warmups,
            iterations=iterations,
            filler_memories=filler_memories,
            token_budget=token_budget,
            retrieval_profile=retrieval_profile,
            concurrency=concurrency,
            processes=processes,
            minimum_queries=effective_minimum,
            canonical=False,
        )
    return {
        "schema": "engraphis-performance-matrix/v1",
        "acceptance": {
            "canonical": True,
            "concurrency_matrix": list(matrix),
            "independent_processes": int(processes),
            "minimum_queries": effective_minimum,
            "query_count": question_count,
            "valid": True,
        },
        "slices": slices,
    }


def _print(report: dict) -> None:
    corpus = report["corpus"]
    run_info = report["run"]
    quality = report["quality"]
    latency = report["latency_ms"]
    context = report["context"]
    environment = report["environment"]
    acceptance = report["acceptance"]
    print(
        "Engraphis performance — "
        f"{corpus['memories']} memories · {corpus['questions']} questions · "
        f"{run_info['timed_recalls']} warm timed recalls @ k={run_info['k']} "
        f"(candidates={run_info['candidate_k']}, "
        f"actual={run_info['actual_candidate_k']['mean']:.1f}, "
        f"depth={run_info['candidate_depth']}, "
        f"profile={run_info['retrieval_profile']})"
    )
    print(
        "  environment          : "
        f"{environment['platform']}/{environment['architecture']} · "
        f"Python {environment['python']} · "
        f"{environment['embedder']} + {environment['vector_backend']}"
    )
    print(
        "  acceptance           : "
        f"concurrency={acceptance['concurrency']} · "
        f"processes={acceptance['independent_processes']} · "
        f"cold={run_info['cold_timed_recalls']} warm={run_info['warm_timed_recalls']}"
    )
    print(
        "  quality              : "
        f"recall@k={quality['recall_at_k']:.3f} · "
        f"hit@k={quality['hit_at_k']:.3f} · "
        f"answer-token={quality['answer_token_recall']:.3f}"
    )
    print(
        "  context tokens       : "
        f"mean={context['mean_tokens']:.2f} · max={context['max_tokens']} · "
        "compact payload saved="
        f"{context['serialized_payload_savings_ratio']:.1%}"
    )
    print(
        "  recall latency (ms)  : "
        f"cold p50={latency['cold']['p50']:.3f} · "
        f"warm p50={latency['p50']:.3f} · p95={latency['p95']:.3f} · "
        f"p99={latency['p99']:.3f} · max={latency['max']:.3f}"
    )


def _print_matrix(report: dict) -> None:
    acceptance = report["acceptance"]
    print(
        "Engraphis canonical performance matrix — "
        f"queries={acceptance['query_count']} · "
        f"processes={acceptance['independent_processes']} · "
        f"concurrency={acceptance['concurrency_matrix']}"
    )
    for concurrency in acceptance["concurrency_matrix"]:
        slice_report = report["slices"][str(concurrency)]
        latency = slice_report["latency_ms"]
        print(
            f"  concurrency={concurrency:<2} : "
            f"cold p50={latency['cold']['p50']:.3f}ms · "
            f"warm p50={latency['p50']:.3f}ms · p95={latency['p95']:.3f}ms · "
            f"p99={latency['p99']:.3f}ms"
        )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Measure full-pipeline Engraphis recall quality, context, and latency."
    )
    parser.add_argument(
        "--dataset",
        default=str(Path(__file__).resolve().parent / "datasets" / "codemem.jsonl"),
    )
    parser.add_argument(
        "--candidate-depth",
        choices=sorted(CANDIDATE_DEPTH_MODES),
        default="fixed",
        help="fixed preserves the requested depth; adaptive uses a profile-aware bounded pool",
    )
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument(
        "--candidate-k",
        type=int,
        default=50,
        help="per-arm candidate depth; sweep this to measure quality/latency tradeoffs",
    )
    parser.add_argument("--dim", type=int, default=256)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument("--token-budget", type=int, default=1500)
    parser.add_argument(
        "--retrieval-profile",
        choices=sorted(RETRIEVAL_PROFILES),
        default="balanced",
        help="retrieval policy to benchmark (default: balanced)",
    )
    parser.add_argument(
        "--filler-memories",
        type=int,
        default=0,
        help="add deterministic unrelated memories to measure corpus scaling",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        choices=SUPPORTED_CONCURRENCY,
        default=1,
        help="concurrent queries per isolated process (default: 1)",
    )
    parser.add_argument(
        "--processes",
        type=int,
        default=1,
        help="number of independent benchmark processes (default: 1)",
    )
    parser.add_argument(
        "--minimum-queries",
        type=int,
        default=0,
        help="reject a dataset with fewer benchmark queries",
    )
    parser.add_argument(
        "--canonical",
        action="store_true",
        help="reserved for compatibility; use --acceptance-matrix for canonical protocol",
    )
    parser.add_argument(
        "--acceptance-matrix",
        action="store_true",
        help="run the canonical 1/4/16-concurrency, >=5-process acceptance matrix",
    )
    parser.add_argument("--json", action="store_true", help="print the full JSON report")
    args = parser.parse_args(argv)
    dataset = load_dataset(args.dataset)
    if args.acceptance_matrix:
        report = run_acceptance_matrix(
            dataset,
            k=args.k,
            candidate_k=args.candidate_k,
            candidate_depth=args.candidate_depth,
            dim=args.dim,
            warmups=args.warmups,
            iterations=args.iterations,
            filler_memories=args.filler_memories,
            token_budget=args.token_budget,
            retrieval_profile=args.retrieval_profile,
            processes=args.processes,
            minimum_queries=args.minimum_queries,
        )
    else:
        report = run(
            dataset,
            k=args.k,
            candidate_k=args.candidate_k,
            candidate_depth=args.candidate_depth,
            dim=args.dim,
            warmups=args.warmups,
            iterations=args.iterations,
            filler_memories=args.filler_memories,
            token_budget=args.token_budget,
            retrieval_profile=args.retrieval_profile,
            concurrency=args.concurrency,
            processes=args.processes,
            minimum_queries=args.minimum_queries,
            canonical=args.canonical,
        )
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        (_print_matrix if args.acceptance_matrix else _print)(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
