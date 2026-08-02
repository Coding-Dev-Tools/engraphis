"""Evaluation-only imported-resource hierarchy prototype.

The prototype derives a file/section tree exclusively from import metadata
(path, heading, and chunk order) and deterministic extractive overviews. It does
not write Engraphis tables or change atomic-memory semantics. Production is gated
on a held-out long-document improvement at three budget points.
"""
from __future__ import annotations

import argparse
import json
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from engraphis.backends.extractor import ChunkingExtractor
from engraphis.core.context import RegexTokenCounter
from engraphis.core.textutil import jaccard, tokenize
from eval.harness import load_dataset


TOKEN_BUDGETS = (256, 512, 1024, 2048, 4096)
DEVELOPMENT_IDS = ("auth-service", "deploy-infra")
HELDOUT_IDS = ("memory-engine", "billing", "onboarding", "data-pipeline")


@dataclass(frozen=True)
class ResourceLeaf:
    id: str
    path: str
    heading: str
    order: int
    content: str


@dataclass(frozen=True)
class ResourceNode:
    id: str
    path: str
    heading: str
    overview: str
    children: tuple[str, ...]


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1))
    return ordered[index]


def _first_sentence(text: str, *, words: int = 28) -> str:
    sentence = str(text or "").strip().split(". ", 1)[0].strip()
    return " ".join(sentence.split()[:words])


def _build_tree(cases: list[dict]) -> tuple[list[ResourceLeaf], list[ResourceNode]]:
    chunker = ChunkingExtractor(target_tokens=96, overlap_tokens=0)
    leaves = []
    nodes = []
    for case in cases:
        path = f"imports/{case['id']}.md"
        file_leaves = []
        for order, (heading, content) in enumerate(chunker._chunks(case["document"])):
            leaf = ResourceLeaf(
                id=f"{case['id']}:chunk:{order}",
                path=path,
                heading=heading or Path(path).stem,
                order=order,
                content=content,
            )
            leaves.append(leaf)
            file_leaves.append(leaf)
            nodes.append(ResourceNode(
                id=f"{case['id']}:section:{order}",
                path=path,
                heading=leaf.heading,
                overview=f"{leaf.heading}: {_first_sentence(content)}",
                children=(leaf.id,),
            ))
        file_overview = "; ".join(
            f"{leaf.heading}: {_first_sentence(leaf.content, words=16)}"
            for leaf in file_leaves
        )
        nodes.append(ResourceNode(
            id=f"{case['id']}:file",
            path=path,
            heading=Path(path).name,
            overview=file_overview,
            children=tuple(leaf.id for leaf in file_leaves),
        ))
    return leaves, nodes


def _score(query: str, text: str) -> float:
    return jaccard(tokenize(query), tokenize(text))


def _flat_candidates(query: str, leaves: list[ResourceLeaf]) -> list[ResourceLeaf]:
    return sorted(
        leaves,
        key=lambda leaf: (
            -_score(query, f"{leaf.path} {leaf.heading} {leaf.content}"),
            leaf.path,
            leaf.order,
        ),
    )


def _hierarchy_candidates(
    query: str,
    leaves: list[ResourceLeaf],
    nodes: list[ResourceNode],
) -> list[ResourceLeaf]:
    leaf_by_id = {leaf.id: leaf for leaf in leaves}
    ranked_nodes = sorted(
        nodes,
        key=lambda node: (
            -_score(query, f"{node.path} {node.heading} {node.overview}"),
            node.id,
        ),
    )
    selected_ids = []
    # A bounded planned arm: two best section/file summaries, followed by one
    # additional file/section route when it contributes new detail leaves.
    for node in ranked_nodes:
        for child in node.children:
            if child not in selected_ids:
                selected_ids.append(child)
        if len(selected_ids) >= 8:
            break
    selected = [leaf_by_id[leaf_id] for leaf_id in selected_ids]
    return _flat_candidates(query, selected)


def _pack(
    leaves: list[ResourceLeaf],
    *,
    budget: int,
    counter: RegexTokenCounter,
) -> tuple[list[ResourceLeaf], int]:
    selected = []
    used = 0
    for leaf in leaves:
        text = f"[{leaf.path}#{leaf.heading}]\n{leaf.content}"
        tokens = counter(text)
        if used + tokens > budget:
            continue
        selected.append(leaf)
        used += tokens
    return selected, used


def _method_rows(
    cases: list[dict],
    *,
    method: str,
    budget: int,
    leaves: list[ResourceLeaf],
    nodes: list[ResourceNode],
) -> list[dict]:
    counter = RegexTokenCounter()
    rows = []
    for case in cases:
        for number, question in enumerate(case.get("questions", [])):
            started = time.perf_counter()
            if method == "flat":
                candidates = _flat_candidates(question["q"], leaves)
            else:
                candidates = _hierarchy_candidates(question["q"], leaves, nodes)
            packed, tokens = _pack(candidates, budget=budget, counter=counter)
            elapsed = (time.perf_counter() - started) * 1000.0
            evidence = str(question.get("evidence") or "").casefold()
            hit = any(evidence in leaf.content.casefold() for leaf in packed)
            rows.append({
                "task_id": f"{case['id']}:{number}",
                "quality": float(hit),
                "context_tokens": tokens,
                "latency_ms": elapsed,
            })
    return rows


def _summary(rows: list[dict]) -> dict:
    count = len(rows)
    latencies = [float(row["latency_ms"]) for row in rows]
    tokens = [int(row["context_tokens"]) for row in rows]
    return {
        "tasks": count,
        "quality": round(sum(row["quality"] for row in rows) / max(1, count), 6),
        "mean_context_tokens": round(sum(tokens) / max(1, count), 6),
        "p95_latency_ms": round(_percentile(latencies, 0.95), 6),
    }


def run(cases: list[dict]) -> dict:
    if not cases:
        raise ValueError("resource hierarchy evaluation requires held-out documents")
    by_id = {str(case.get("id") or ""): case for case in cases}
    required = set(DEVELOPMENT_IDS) | set(HELDOUT_IDS)
    missing = sorted(required - set(by_id))
    if missing:
        raise ValueError("resource hierarchy split is missing: " + ", ".join(missing))
    heldout_cases = [by_id[case_id] for case_id in HELDOUT_IDS]
    leaves, nodes = _build_tree(heldout_cases)
    methods = {method: {} for method in ("flat", "hierarchy")}
    improvements = 0
    latency_ok = True
    token_ok = True
    for budget in TOKEN_BUDGETS:
        for method in methods:
            methods[method][str(budget)] = _summary(_method_rows(
                heldout_cases,
                method=method,
                budget=budget,
                leaves=leaves,
                nodes=nodes,
            ))
        flat = methods["flat"][str(budget)]
        hierarchy = methods["hierarchy"][str(budget)]
        improvements += int(hierarchy["quality"] - flat["quality"] >= 0.03)
        token_ok = token_ok and (
            hierarchy["mean_context_tokens"] <= flat["mean_context_tokens"]
        )
        flat_latency = flat["p95_latency_ms"]
        latency_ratio = hierarchy["p95_latency_ms"] / flat_latency if flat_latency else 1.0
        latency_ok = latency_ok and latency_ratio <= 1.5
    passed = improvements >= 3 and token_ok and latency_ok
    return {
        "benchmark": {
            "name": "engraphis-imported-resource-hierarchy-prototype/v1",
            "offline": True,
            "evaluation_only": True,
            "metadata_inputs": ["file_path", "chunk_order", "markdown_heading"],
            "token_budgets": list(TOKEN_BUDGETS),
            "split": {
                "development_ids": list(DEVELOPMENT_IDS),
                "heldout_ids": list(HELDOUT_IDS),
                "fixed_before_evaluation": True,
            },
        },
        "workload": {
            "documents": len(heldout_cases),
            "questions": sum(len(case.get("questions", [])) for case in heldout_cases),
            "detail_leaves": len(leaves),
            "derived_nodes": len(nodes),
        },
        "methods": methods,
        "production_gate": {
            "quality_improvement_budget_count": improvements,
            "requires_three_budgets": True,
            "context_tokens_not_increased": token_ok,
            "p95_latency_within_1_5x": latency_ok,
            "passed": passed,
            "schema_action": "bump_to_8" if passed else "retain_7",
        },
    }


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset",
        default=str(Path(__file__).resolve().parent / "datasets" / "longdoc.jsonl"),
    )
    args = parser.parse_args(argv)
    try:
        report = run(load_dataset(args.dataset))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True))
        raise SystemExit(2) from exc
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
