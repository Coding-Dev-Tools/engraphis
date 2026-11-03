"""Evaluation gate for Spec Crawl: section parsing, flag detection, and determinism.

Usage:
    python -m eval.spec_crawl
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from engraphis.core.spec_crawl import analyze_memory_nodes, crawl_spec

ROOT = Path(__file__).resolve().parents[1]
DATASET_PATH = ROOT / "eval" / "datasets" / "spec_crawl.jsonl"
METRIC_FLOORS = {
    "determinism_rate": 0.90,
    "axes_accuracy": 0.90,
    "flag_recall": 0.90,
    "qualifier_accuracy": 0.90,
    "sections_accuracy": 0.90,
    "scores_accuracy": 1.0,
    "overall_accuracy": 0.90,
}
MEMORY_DATASET_PATH = ROOT / "eval" / "datasets" / "spec_crawl_memories.jsonl"


def evaluate_memory_fixtures(path: Path = MEMORY_DATASET_PATH) -> dict:
    """Check exact conflict pairs and lifecycle contracts in both input orders."""
    payload = path.read_bytes()
    cases = 0
    failures: list[str] = []
    for line in payload.decode("utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        cases += 1
        expected = {tuple(sorted(pair)) for pair in item["expected_conflicts"]}
        expected_nodes = {str(node["id"]) for node in item["memories"]}
        expected_retirements = {tuple(pair) for pair in item.get("expected_retirements", [])}
        expected_actions = {"supersede"} if expected_retirements else set()
        if len(expected_retirements) < len(expected):
            expected_actions.add("clarify")
        expected_actions = set(item.get("expected_actions", expected_actions))
        passed = True
        for nodes in (item["memories"], list(reversed(item["memories"]))):
            report = analyze_memory_nodes(nodes, include_trace=False)
            conflicts = report["conflicts"]
            actual = {tuple(sorted((row["node_a"], row["node_b"]))) for row in conflicts}
            passed = passed and actual == expected and len(conflicts) == len(expected)
            passed = passed and {node["id"] for node in report["nodes"]} == expected_nodes
            passed = passed and len(report["nodes"]) == len(expected_nodes)
            passed = passed and all("content" not in node for node in report["nodes"])
            if "expected_orphans" in item:
                passed = passed and {row["node_id"] for row in report["orphans"]} == set(item["expected_orphans"])
            passed = passed and {row["remedy"]["action"] for row in conflicts} == expected_actions
            retirements = {(row["remedy"]["keep_node"], row["remedy"]["retire_node"])
                           for row in conflicts if row["remedy"]["action"] == "supersede"}
            passed = passed and retirements == expected_retirements
        if not passed:
            failures.append(item["id"])
    passed_cases = cases - len(failures)
    return {"cases": cases, "passed": passed_cases,
            "accuracy": passed_cases / cases if cases else 0.0,
            "failures": failures, "dataset_sha256": hashlib.sha256(payload).hexdigest()}


def run() -> dict:
    if not DATASET_PATH.is_file():
        raise FileNotFoundError(f"Dataset missing: {DATASET_PATH}")

    total_specs = 0
    deterministic_runs = 0
    axes_correct = 0
    total_expected_axes = 0
    expected_flags_found = 0
    total_expected_flags = 0
    unexpected_flags_avoided = 0
    total_unexpected_flags = 0
    sections_correct = 0
    scores_correct = 0

    with open(DATASET_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            item = json.loads(line)
            total_specs += 1

            text = item["text"]
            r1 = crawl_spec(text)
            r2 = crawl_spec(text)
            sections_correct += len(r1["sections"]) == item["expected_sections"]
            scores_correct += item["min_score"] <= r1["score"] <= item.get("max_score", 100)

            # 1. Determinism check
            if r1["content_sha256"] == r2["content_sha256"] and r1["trace_sha256"] == r2["trace_sha256"] and r1["score"] == r2["score"]:
                deterministic_runs += 1

            # 2. Section axes check
            actual_axes = [s["axis"] for s in r1["sections"]]
            for index, exp_axis in enumerate(item.get("expected_axes", [])):
                total_expected_axes += 1
                if index < len(actual_axes) and exp_axis == actual_axes[index]:
                    axes_correct += 1

            # 3. Expected flags check
            flag_tokens = {f["token"].casefold() for f in r1["flags"]}
            for exp_flag in item.get("expected_flags", []):
                total_expected_flags += 1
                if exp_flag.casefold() in flag_tokens:
                    expected_flags_found += 1

            # 4. Unexpected flags avoided check
            for unexp in item.get("unexpected_flags", []):
                total_unexpected_flags += 1
                if unexp.casefold() not in flag_tokens:
                    unexpected_flags_avoided += 1

    # Also test dogfooding on repo's AGENTS.md
    agents_path = ROOT / "AGENTS.md"
    agents_ok = False
    if agents_path.is_file():
        agents_content = agents_path.read_text(encoding="utf-8")[:64_000]
        agents_r = crawl_spec(agents_content)
        # Score must be positive, have parsed sections, and detected flags
        if agents_r["score"] >= 50 and len(agents_r["sections"]) >= 8:
            agents_ok = True

    flag_recall = (expected_flags_found / total_expected_flags) if total_expected_flags else 1.0
    axes_accuracy = (axes_correct / total_expected_axes) if total_expected_axes else 1.0
    qualifier_accuracy = (unexpected_flags_avoided / total_unexpected_flags) if total_unexpected_flags else 1.0
    determinism_rate = (deterministic_runs / total_specs) if total_specs else 1.0

    sections_accuracy = sections_correct / total_specs if total_specs else 0.0
    scores_accuracy = scores_correct / total_specs if total_specs else 0.0
    overall_accuracy = (flag_recall + axes_accuracy + qualifier_accuracy + determinism_rate
                        + sections_accuracy + scores_accuracy) / 6.0

    return {
        "total_specs": total_specs,
        "determinism_rate": determinism_rate,
        "axes_accuracy": axes_accuracy,
        "flag_recall": flag_recall,
        "qualifier_accuracy": qualifier_accuracy,
        "sections_accuracy": sections_accuracy,
        "scores_accuracy": scores_accuracy,
        "overall_accuracy": overall_accuracy,
        "agents_dogfood_ok": agents_ok,
        "memory_audits": evaluate_memory_fixtures(),
    }


def main() -> None:
    res = run()
    print("\nEngraphis Spec Crawl Evaluation Gate")
    print(f"  Determinism rate      : {res['determinism_rate'] * 100:.1f}%")
    print(f"  Axis classification   : {res['axes_accuracy'] * 100:.1f}%")
    print(f"  Flag recall           : {res['flag_recall'] * 100:.1f}%")
    print(f"  Qualified-token avoidance: {res['qualifier_accuracy'] * 100:.1f}%")
    print(f"  Section count        : {res['sections_accuracy'] * 100:.1f}%")
    print(f"  Score boundaries     : {res['scores_accuracy'] * 100:.1f}%")
    print(f"  Overall accuracy      : {res['overall_accuracy'] * 100:.1f}%")
    memory = res["memory_audits"]
    print(f"  Memory audit contracts: {memory['passed']}/{memory['cases']} (both input orders)")
    print(f"  Memory fixture SHA-256: {memory['dataset_sha256']}")
    print(f"  AGENTS.md dogfooding  : {'PASSED' if res['agents_dogfood_ok'] else 'FAILED'}\n")

    violations = [
        f"{metric} {res[metric]:.1%} < {floor:.1%}"
        for metric, floor in METRIC_FLOORS.items()
        if not res[metric] >= floor
    ]
    if not res["agents_dogfood_ok"]:
        violations.append("AGENTS.md dogfooding failed")
    if not memory["accuracy"] >= 1.0:
        violations.append("memory audit contracts below 100%")
    if violations:
        sys.stderr.write("FLOOR VIOLATION: Spec Crawl evaluation: " + "; ".join(violations) + "\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
