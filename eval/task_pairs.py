"""Matched task outcomes with repository-family blocked uncertainty; no execution adapter."""
from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import re
from typing import Optional

from eval.benchmark import canonical_json, report_envelope, write_canonical_artifact
from eval.coding_acceptance import ARMS, BUDGETS, validate_corpus, validate_matched_bindings
from eval.rework_statistics import blocked_mean_interval


SCHEMA = "engraphis-task-pairs/v1"
MARGIN = 0.01
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}\Z")
_FIELDS = {"scenario_id", "source_sha256", "oracle_sha256", "binding_sha256", "repetition",
           "status", "task_success", "evidence_retained_ids", "critical_violations",
           "implementation_sha256", "run_id"}


def digest(value) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def _index(rows: list[dict], *, binding: dict, scenarios: dict[str, dict],
           repetitions: int, run_ids: set[str]) -> tuple[dict, str]:
    if not isinstance(rows, list):
        raise ValueError("task outcomes must be a list")
    indexed, implementation = {}, None
    for row in rows:
        if not isinstance(row, dict) or set(row) != _FIELDS:
            raise ValueError("task outcome fields must match the versioned pair contract")
        scenario_id = row["scenario_id"]
        if scenario_id not in scenarios:
            raise ValueError("outcome references a task outside the declared split")
        scenario = scenarios[scenario_id]
        number = row["repetition"]
        if type(number) is not int or not 0 <= number < repetitions:
            raise ValueError("outcome repetition is outside the declared range")
        key = (scenario_id, number)
        if key in indexed:
            raise ValueError("duplicate task/repetition outcome")
        run_id = row["run_id"]
        if not isinstance(run_id, str) or not _ID.fullmatch(run_id) or run_id in run_ids:
            raise ValueError("each attempt requires a unique run ID across both conditions")
        run_ids.add(run_id)
        if (row["source_sha256"] != scenario["source_sha256"]
                or row["oracle_sha256"] != scenario["oracle_sha256"]
                or row["binding_sha256"] != digest(binding)):
            raise ValueError("outcome source/oracle/run binding differs from the frozen inputs")
        revision = row["implementation_sha256"]
        if not isinstance(revision, str) or not _SHA.fullmatch(revision):
            raise ValueError("implementation requires a source SHA-256 identity")
        if implementation is not None and implementation != revision:
            raise ValueError("implementation changed within one condition")
        implementation = revision
        if row["status"] not in {"complete", "error", "timeout"}:
            raise ValueError("unknown outcome status; exclusions are not accepted")
        if type(row["task_success"]) is not bool:
            raise ValueError("task success must be an observed boolean")
        violations = row["critical_violations"]
        if type(violations) is not int or violations < 0:
            raise ValueError("critical violation count must be a nonnegative integer")
        evidence = row["evidence_retained_ids"]
        if (not isinstance(evidence, list) or len(set(evidence)) != len(evidence)
                or any(not isinstance(item, str) or not _ID.fullmatch(item) for item in evidence)):
            raise ValueError("retained evidence requires unique bounded IDs")
        if row["status"] != "complete" and (row["task_success"] or evidence):
            raise ValueError("failed attempts cannot carry successful task/evidence outcomes")
        indexed[key] = row
    return indexed, implementation or "unavailable"


def _score(row: Optional[dict], scenario: dict, metric: str) -> Optional[float]:
    if metric == "task_success":
        return float(row["task_success"]) if row is not None else 0.0
    required = set(scenario["required_evidence_ids"])
    if not required:
        return None  # Unsupported questions do not receive a free perfect retention score.
    retained = set(row["evidence_retained_ids"]) if row is not None else set()
    return len(required & retained) / len(required)


def _metric_pairs(scenarios: dict, baseline: dict, candidate: dict, *, repetitions: int,
                  metric: str, iterations: int, seed: int) -> dict:
    families = defaultdict(list)
    baseline_values, candidate_values = [], []
    for scenario_id, scenario in scenarios.items():
        for repetition in range(repetitions):
            key = (scenario_id, repetition)
            before, after = (_score(baseline.get(key), scenario, metric),
                             _score(candidate.get(key), scenario, metric))
            if before is None or after is None:
                continue
            families[scenario["family_id"]].append(after - before)
            baseline_values.append(before)
            candidate_values.append(after)
    means = [sum(values) / len(values) for _, values in sorted(families.items())]
    interval = blocked_mean_interval(means, unit="repository family", iterations=iterations, seed=seed)
    return {"baseline_mean": sum(baseline_values) / len(baseline_values) if baseline_values else None,
            "candidate_mean": sum(candidate_values) / len(candidate_values) if candidate_values else None,
            "scored_task_attempt_pairs": len(baseline_values), "family_count": len(families),
            "delta_family_mean_interval": interval,
            "noninferiority_supported": False}


def evaluate_task_pairs(baseline_rows: list[dict], candidate_rows: list[dict], *,
                        corpus: dict, bindings: list[dict], baseline_arm: str = "full_history",
                        candidate_arm: str = "hybrid", budget: int = 1500,
                        split: str = "held_out", repetitions: int = 3,
                        iterations: int = 2000, seed: int = 20260905,
                        attestation_path: Optional[Path] = None) -> dict:
    """Missing pairs are conservative failures and can never approve a default change."""
    corpus_check = validate_corpus(corpus, require_independent=corpus.get("origin") == "independent_human",
                                    attestation_path=attestation_path)
    validate_matched_bindings(bindings)
    if (baseline_arm not in ARMS or candidate_arm not in ARMS or baseline_arm == candidate_arm
            or budget not in BUDGETS or split not in {"development", "validation", "held_out"}
            or type(repetitions) is not int or repetitions < 1):
        raise ValueError("invalid pair conditions/budget/split/repetitions")
    if any(binding["corpus_sha256"] != corpus_check["manifest_sha256"] for binding in bindings):
        raise ValueError("matrix binding does not reference this exact corpus manifest")
    before_binding = next(row for row in bindings if row["arm"] == baseline_arm and row["token_budget"] == budget)
    after_binding = next(row for row in bindings if row["arm"] == candidate_arm and row["token_budget"] == budget)
    scenarios = {row["id"]: row for row in corpus["scenarios"] if row["split"] == split}
    run_ids: set[str] = set()
    baseline, before_revision = _index(baseline_rows, binding=before_binding, scenarios=scenarios,
                                       repetitions=repetitions, run_ids=run_ids)
    candidate, after_revision = _index(candidate_rows, binding=after_binding, scenarios=scenarios,
                                      repetitions=repetitions, run_ids=run_ids)
    expected = {(scenario_id, number) for scenario_id in scenarios for number in range(repetitions)}
    complete = set(baseline) == set(candidate) == expected
    family_count = len({row["family_id"] for row in scenarios.values()})
    enough = split == "held_out" and family_count >= 24 and len(scenarios) >= 240 and repetitions >= 3
    failures = {"baseline": sum(row["status"] != "complete" for row in baseline.values()) + len(expected - set(baseline)),
                "candidate": sum(row["status"] != "complete" for row in candidate.values()) + len(expected - set(candidate))}
    violations = sum(row["critical_violations"] for row in [*baseline.values(), *candidate.values()])
    metrics = {name: _metric_pairs(scenarios, baseline, candidate, repetitions=repetitions,
                                   metric=name, iterations=iterations, seed=seed)
               for name in ("task_success", "evidence_retention")}
    declared_independent = corpus_check["origin"] == "independent_human"
    for result in metrics.values():
        interval = result["delta_family_mean_interval"]
        if not complete:
            interval["inferentially_usable"] = False
        result["noninferiority_supported"] = bool(
            complete and enough and declared_independent and violations == 0
            and sum(failures.values()) == 0 and interval["inferentially_usable"]
            and interval["low"] is not None and interval["low"] > -MARGIN)
    categories = {}
    for category in sorted({row["category"] for row in scenarios.values()}):
        selected = {name: row for name, row in scenarios.items() if row["category"] == category}
        categories[category] = {metric: _metric_pairs(
            selected, baseline, candidate, repetitions=repetitions, metric=metric,
            iterations=iterations, seed=seed) for metric in metrics}
        if not complete:
            for result in categories[category].values():
                result["delta_family_mean_interval"]["inferentially_usable"] = False
    return report_envelope(
        suite=SCHEMA, dataset_path=Path(__file__),
        config={"baseline_arm": baseline_arm, "candidate_arm": candidate_arm, "budget": budget,
                "split": split, "repetitions": repetitions, "iterations": iterations, "seed": seed,
                "noninferiority_margin": MARGIN, "corpus_sha256": corpus_check["manifest_sha256"],
                "baseline_rows_sha256": digest(baseline_rows), "candidate_rows_sha256": digest(candidate_rows),
                "bindings_sha256": digest(bindings)},
        records=[{"question_id": f"pair-{index}", "category": scenario["category"],
                  "qa_correct": bool(_score(candidate.get((scenario_id, number)), scenario, "task_success"))}
                 for index, (scenario_id, number) in enumerate(sorted(expected))
                 for scenario in [scenarios[scenario_id]]],
        metrics={"outcomes": metrics, "categories": categories, "complete_pairs": complete,
                 "expected_task_attempt_pairs": len(expected), "scenario_count": len(scenarios),
                 "family_count": family_count, "minimum_sampling_satisfied": enough and complete,
                 "observed_matched_families": len({scenarios[key[0]]["family_id"] for key in set(baseline) & set(candidate)}),
                 "missing_baseline": len(expected - set(baseline)),
                 "missing_candidate": len(expected - set(candidate)), "execution_failures": failures,
                 "critical_violations": violations, "default_action": "keep_current_defaults",
                 "publication_ready": False, "independently_authored_verified": False,
                 "authorship_status": corpus_check["authorship_status"], "origin": corpus_check["origin"],
                 "implementation_identities": {"baseline": before_revision, "candidate": after_revision},
                 "delta_direction": "candidate minus baseline; positive is better",
                 "limitations": ["matched metadata does not prove independent authorship or actual task execution",
                    "percentile family bootstrap is degenerate for identical family effects; no noninferiority pass",
                    "24 held-out families, 240 tasks and three repetitions are minimum sampling, not a power guarantee",
                    "citation validity and answer completeness need separate adjudicated oracles",
                    "statistical noninferiority alone does not authorize default changes or publication"]},
        source_paths=[Path(__file__), Path(__file__).with_name("rework_statistics.py")],
        models={"reader": {"identity": before_binding["reader_id"], "revision": before_binding["reader_revision"]}},
        token_accounting={"identity": before_binding["tokenizer_id"], "revision": before_binding["tokenizer_revision"],
                          "scope": "declared common reader budget", "method": "bound run manifest; no new token measurement"},
        command=["python", "-m", "eval.task_pairs", "--baseline", "<baseline-outcomes>", "--candidate", "<candidate-outcomes>"])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("baseline", "candidate", "corpus", "bindings", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--attestation", type=Path)
    parser.add_argument("--baseline-arm", choices=ARMS, default="full_history")
    parser.add_argument("--candidate-arm", choices=ARMS, default="hybrid")
    parser.add_argument("--budget", type=int, choices=BUDGETS, default=1500)
    args = parser.parse_args(argv)
    def load(path):
        return json.loads(path.read_text(encoding="utf-8"))
    report = evaluate_task_pairs(load(args.baseline), load(args.candidate), corpus=load(args.corpus),
                                 bindings=load(args.bindings), baseline_arm=args.baseline_arm,
                                 candidate_arm=args.candidate_arm, budget=args.budget,
                                 attestation_path=args.attestation)
    print(json.dumps(write_canonical_artifact(report, args.output)))
    return int(not report["metrics"]["complete_pairs"] or report["metrics"]["critical_violations"] > 0)


if __name__ == "__main__":
    raise SystemExit(main())
