"""Evaluation gate for Spec Crawl: section parsing, flag detection, and determinism.

Usage:
    python -m eval.spec_crawl
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from engraphis.core.spec_crawl import crawl_spec

ROOT = Path(__file__).resolve().parents[1]
DATASET_PATH = ROOT / "eval" / "datasets" / "spec_crawl.jsonl"


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
            actual_axes = {s["axis"] for s in r1["sections"]}
            for exp_axis in item.get("expected_axes", []):
                total_expected_axes += 1
                if exp_axis in actual_axes:
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
    }


def main() -> None:
    res = run()
    print("\nEngraphis Spec Crawl Evaluation Gate")
    print(f"  Determinism rate      : {res['determinism_rate'] * 100:.1f}%")
    print(f"  Axis classification   : {res['axes_accuracy'] * 100:.1f}%")
    print(f"  Flag recall           : {res['flag_recall'] * 100:.1f}%")
    print(f"  Qualifier precision   : {res['qualifier_accuracy'] * 100:.1f}%")
    print(f"  Section count        : {res['sections_accuracy'] * 100:.1f}%")
    print(f"  Score boundaries     : {res['scores_accuracy'] * 100:.1f}%")
    print(f"  Overall accuracy      : {res['overall_accuracy'] * 100:.1f}%")
    print(f"  AGENTS.md dogfooding  : {'PASSED' if res['agents_dogfood_ok'] else 'FAILED'}\n")

    if res["overall_accuracy"] < 0.90 or not res["agents_dogfood_ok"] or res["scores_accuracy"] < 1:
        sys.stderr.write("FLOOR VIOLATION: Spec Crawl evaluation fell below required 90% threshold\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
