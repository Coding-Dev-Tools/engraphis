"""Validate retained diagnostics and derive content-free, clustered summaries.

This analysis does not execute engines or model calls. External retrieval scores
remain diagnostics; no analysis output grants independent acceptance.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import json
import math
from pathlib import Path
import random
from typing import Optional

from eval.benchmark import canonical_json, sha256_file, validate_report


SCHEMA = "engraphis-external-analysis/v1"


def read_verified(path: Path) -> dict:
    sidecar = path.with_suffix(path.suffix + ".sha256")
    if not sidecar.is_file() or sidecar.read_text(encoding="utf-8").split()[0] != sha256_file(path):
        raise ValueError("diagnostic artifact checksum missing or mismatched")
    report = json.loads(path.read_text(encoding="utf-8"))
    errors = validate_report(report)
    if errors:
        raise ValueError("invalid diagnostic envelope: " + errors[0])
    if report["metrics"].get("claim_boundary") != "evidence retrieval diagnostic; not generated-answer accuracy":
        raise ValueError("only external retrieval diagnostics are accepted")
    rows = report["records"]
    if report["metrics"]["questions"] != len(rows):
        raise ValueError("question count differs from records")
    ids = [row["question_id"] for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate diagnostic question")
    for row in rows:
        if not set(row["packed_ids"]) <= set(row["retrieved_ids"]):
            raise ValueError("packed evidence was not retrieved")
        if row["retrieval_scored"]:
            gold = set(row["supporting_ids"])
            if not gold:
                raise ValueError("scored retrieval requires gold evidence")
            for key, field in (("recall_at_k", "retrieved_ids"), ("packed_recall_at_k", "packed_ids")):
                observed = len(gold & set(row[field])) / len(gold)
                if not math.isclose(row[key], observed, abs_tol=5e-6):
                    raise ValueError("record retrieval count disagrees with metric")
        budget = report["protocol"]["config"]["token_budget"]
        if type(row["context_tokens"]) is not int or not 0 <= row["context_tokens"] <= budget:
            raise ValueError("record context exceeds its frozen budget")
    for field, eligible in (("recall_at_k", "retrieval_scored"), ("packed_recall_at_k", "retrieval_scored"),
                            ("answer_token_recall", "answer_scored"), ("packed_answer_token_recall", "answer_scored")):
        values = [row[field] for row in rows if row[eligible]]
        if values and not math.isclose(report["metrics"][field], sum(values) / len(values), abs_tol=5e-6):
            raise ValueError("aggregate does not match its scored records")
    return report


def clustered_interval(rows: list[dict], field: str, *, eligible: str = "retrieval_scored",
                       iterations: int = 2000, seed: int = 20260915) -> dict:
    """Bootstrap whole source cases, retaining each sampled case's question weight."""
    groups: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        if row.get(eligible):
            # Current harness artifacts retain a public-safe case identity.  Use
            # it instead of parsing question IDs: MemoryAgentBench upstream QA
            # IDs may contain colons, and collision-qualified IDs add another
            # colon-delimited suffix that is not a source-case boundary.
            groups[_source_case(row)].append(float(row[field]))
    blocks = list(groups.values())
    observed = [value for values in blocks for value in values]
    result = {"point": sum(observed) / len(observed) if observed else None,
              "low": None, "high": None, "confidence": .95, "source_cases": len(blocks),
              "scored_questions": len(observed), "iterations": iterations, "seed": seed,
              "method": "percentile bootstrap of source cases, question-weighted ratio",
              "boundary": "dataset sampling uncertainty; one execution, no model-repeat uncertainty"}
    if len(blocks) < 2:
        return result
    rng = random.Random(seed)
    estimates = []
    for _ in range(iterations):
        selected = [blocks[rng.randrange(len(blocks))] for _ in blocks]
        estimates.append(sum(sum(values) for values in selected) / sum(len(values) for values in selected))
    estimates.sort()
    result.update(low=estimates[int(.025 * (iterations - 1))], high=estimates[int(.975 * (iterations - 1))])
    return result


def _source_case(row: dict, identity: Optional[str] = None) -> str:
    """Return the retained source-case identity, with a legacy fallback."""

    source_case = row.get("case") or row.get("source_case_id")
    if not isinstance(source_case, str) or not source_case.strip():
        # Keep older retained artifacts readable; these predate the explicit case
        # field and use the historical ID convention.
        source_case = str(identity if identity is not None else row["question_id"]).rsplit(":", 1)[0]
    return source_case.strip()


def summarize(path: Path) -> dict:
    report = read_verified(path)
    rows, metrics = report["records"], report["metrics"]
    categories = {}
    for category in sorted({str(row["category"]) for row in rows}):
        selected = [row for row in rows if str(row["category"]) == category]
        scored = [row for row in selected if row["retrieval_scored"]]
        categories[category] = {"questions": len(selected), "scored": len(scored),
                                "recall_at_k": sum(row["recall_at_k"] for row in scored) / len(scored) if scored else None,
                                "packed_recall_at_k": sum(row["packed_recall_at_k"] for row in scored) / len(scored) if scored else None}
    return {"schema": SCHEMA, "input_artifact": path.name, "input_sha256": sha256_file(path),
            "dataset": report["suite"]["dataset"], "dataset_sha256": report["suite"]["sha256"],
            "configuration": report["protocol"]["config"], "models": report["models"],
            "status": metrics["checkpoint_status"], "questions": len(rows),
            "retrieval_scored_questions": sum(bool(row["retrieval_scored"]) for row in rows),
            "answer_token_scored_questions": sum(bool(row["answer_scored"]) for row in rows),
            "retrieval_exclusions": sum(not row["retrieval_scored"] for row in rows),
            "recall": clustered_interval(rows, "recall_at_k"),
            "packed_recall": clustered_interval(rows, "packed_recall_at_k"),
            "answer_token_evidence": clustered_interval(rows, "answer_token_recall", eligible="answer_scored"),
            "packed_answer_token_evidence": clustered_interval(rows, "packed_answer_token_recall", eligible="answer_scored"),
            "mean_context_tokens": sum(row["context_tokens"] for row in rows) / len(rows),
            "max_context_tokens": max(row["context_tokens"] for row in rows),
            "categories": categories, "official_qa_complete": False,
            "independent_acceptance_eligible": False, "leadership_eligible": False}


def paired_difference(baseline: Path, candidate: Path) -> dict:
    before, after = read_verified(baseline), read_verified(candidate)
    if before["suite"]["sha256"] != after["suite"]["sha256"] or before["models"] != after["models"]:
        raise ValueError("paired diagnostics require identical data bytes and models")
    before_config, after_config = before["protocol"]["config"], after["protocol"]["config"]
    # A repair can change memory text while preserving raw dataset bytes and
    # question/evidence IDs. Bind the normalization inputs before pairing rows.
    if (not before_config.get("format") or not after_config.get("format")
            or any(before_config.get(field) != after_config.get(field)
                   for field in ("format", "repair_manifest_sha256"))):
        raise ValueError("paired diagnostics require identical normalized-corpus bindings")
    left = {row["question_id"]: row for row in before["records"]}
    right = {row["question_id"]: row for row in after["records"]}
    if left.keys() != right.keys():
        raise ValueError("paired diagnostic coverage differs")
    deltas = []
    for identity in left:
        old, new = left[identity], right[identity]
        if (old["retrieval_scored"] != new["retrieval_scored"] or old["supporting_ids"] != new["supporting_ids"]):
            raise ValueError("paired diagnostic scoring or oracle differs")
        source_case = _source_case(old, identity)
        if _source_case(new, identity) != source_case:
            raise ValueError("paired diagnostic source-case identity differs")
        deltas.append({"question_id": identity, "retrieval_scored": old["retrieval_scored"],
                       "case": source_case,
                       "delta": new["packed_recall_at_k"] - old["packed_recall_at_k"]})
    return {"baseline_sha256": sha256_file(baseline), "candidate_sha256": sha256_file(candidate),
            "baseline_config": before["protocol"]["config"], "candidate_config": after["protocol"]["config"],
            "packed_recall_delta": clustered_interval(deltas, "delta"),
            "selection_boundary": "exploratory external configuration comparison; not a coding holdout gate"}


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reports", nargs="+", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--compare", action="store_true")
    args = parser.parse_args(argv)
    value = {"schema": SCHEMA, "reports": [summarize(path) for path in args.reports],
             "source_sha256": sha256_file(Path(__file__))}
    if args.compare:
        if len(args.reports) != 2:
            raise ValueError("comparison requires exactly two reports")
        value["comparison"] = paired_difference(*args.reports)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(canonical_json(value) + "\n")
    args.output.with_suffix(args.output.suffix + ".sha256").write_text(
        f"{sha256_file(args.output)}  {args.output.name}\n", encoding="utf-8")
    print(json.dumps(value, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
