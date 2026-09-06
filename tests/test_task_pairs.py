"""Generated outcomes are validator tests, not independent or measured task results."""
from copy import deepcopy

import pytest

from eval.benchmark import validate_report, write_canonical_artifact
from eval.coding_acceptance import ARMS, BUDGETS, CATEGORIES, SCHEMA, family_splits
from eval.rework_statistics import blocked_mean_interval
from eval.task_pairs import digest, evaluate_task_pairs


@pytest.fixture(scope="module")
def generated_pairs():
    families = [f"test-family-{i}" for i in range(40)]
    splits = family_splits(families, 42)
    corpus = {"schema": SCHEMA, "origin": "synthetic_fixture", "split_seed": 42,
              "implementation_author_ids": ["test-generator"], "reviewer_ids": ["test-reviewer-a", "test-reviewer-b"],
              "attestation_sha256": "a" * 64, "frozen_at": "2026-09-05T00:00:00Z",
              "arms": list(ARMS), "token_budgets": list(BUDGETS),
              "scenarios": [{"id": f"{family}:{category}", "family_id": family, "category": category,
                             "split": splits[family], "origin": "synthetic_fixture", "author_ids": ["test-generator"],
                             "source_sha256": digest([family, category]), "oracle_sha256": digest([category, family]),
                             "required_evidence_ids": [] if category == "unsupported_questions" else ["fact-1", "fact-2"]}
                            for family in families for category in CATEGORIES]}
    common = {"corpus_sha256": digest(corpus), "prompt_sha256": "a" * 64, "reader_id": "test-reader", "reader_revision": "test",
              "tokenizer_id": "test-tokenizer", "tokenizer_revision": "test", "max_input_tokens": 128000, "max_output_tokens": 8192,
              "embedding_id": "test-local-model", "embedding_revision": "b" * 64, "embedding_semantic": True,
              "source_visibility_sha256": "c" * 64, "history_overflow_policy": "fail_preflight", "seed": 42}
    bindings = [{**common, "arm": arm, "token_budget": budget} for arm in ARMS for budget in BUDGETS]
    outcomes = []
    for arm in ("full_history", "hybrid"):
        binding = next(row for row in bindings if row["arm"] == arm and row["token_budget"] == 1500)
        outcomes.append([{"scenario_id": scenario["id"], "source_sha256": scenario["source_sha256"],
                          "oracle_sha256": scenario["oracle_sha256"], "binding_sha256": digest(binding),
                          "repetition": number, "status": "complete", "task_success": True,
                          "evidence_retained_ids": list(scenario["required_evidence_ids"]), "critical_violations": 0,
                          "implementation_sha256": "d" * 64, "run_id": f"{arm}:{scenario['id']}:{number}"}
                         for scenario in corpus["scenarios"] if scenario["split"] == "held_out" for number in range(3)])
    return corpus, bindings, *outcomes


def test_identical_generated_results_never_pass_noninferiority(generated_pairs, tmp_path):
    corpus, bindings, before, after = generated_pairs
    report = evaluate_task_pairs(before, after, corpus=corpus, bindings=bindings, iterations=1000)
    assert validate_report(report) == []
    metrics = report["metrics"]
    assert metrics["complete_pairs"] is True
    assert metrics["expected_task_attempt_pairs"] == 720
    assert metrics["family_count"] == 24
    assert metrics["default_action"] == "keep_current_defaults"
    assert metrics["independently_authored_verified"] is False
    for result in metrics["outcomes"].values():
        assert result["noninferiority_supported"] is False
        assert result["delta_family_mean_interval"]["degenerate"] is True
        assert result["delta_family_mean_interval"]["units"] == 24
    assert metrics["categories"]["unsupported_questions"]["evidence_retention"]["baseline_mean"] is None
    assert write_canonical_artifact(report, tmp_path / "synthetic-pairs.json")["sha256"]


def test_missing_and_tiny_pairs_are_failures_in_the_declared_denominator(generated_pairs):
    corpus, bindings, before, after = generated_pairs
    report = evaluate_task_pairs(before[:3], after[:2], corpus=corpus, bindings=bindings, iterations=1000)
    metrics = report["metrics"]
    assert metrics["complete_pairs"] is False
    assert metrics["missing_baseline"] == 717 and metrics["missing_candidate"] == 718
    assert metrics["minimum_sampling_satisfied"] is False
    assert metrics["observed_matched_families"] == 1
    assert metrics["outcomes"]["task_success"]["scored_task_attempt_pairs"] == 720
    assert metrics["outcomes"]["task_success"]["noninferiority_supported"] is False
    assert metrics["default_action"] == "keep_current_defaults"


@pytest.mark.parametrize("field,value", [("binding_sha256", "f" * 64), ("oracle_sha256", "f" * 64),
                                       ("source_sha256", "f" * 64), ("repetition", 3), ("status", "excluded"),
                                       ("implementation_sha256", "f" * 64), ("task_success", None)])
def test_unmatched_inputs_and_invalid_outcomes_fail_closed(generated_pairs, field, value):
    corpus, bindings, before, after = generated_pairs
    damaged = deepcopy(after)
    damaged[0][field] = value
    with pytest.raises(ValueError):
        evaluate_task_pairs(before, damaged, corpus=corpus, bindings=bindings, iterations=1000)


def test_duplicate_attempt_cannot_be_counted_as_an_independent_repetition(generated_pairs):
    corpus, bindings, before, after = generated_pairs
    damaged = deepcopy(after)
    damaged[1]["run_id"] = damaged[0]["run_id"]
    with pytest.raises(ValueError, match="unique run"):
        evaluate_task_pairs(before, damaged, corpus=corpus, bindings=bindings)


def test_family_block_interval_counts_families_and_is_deterministic():
    values = [-0.3, 0.1, 0.2, 0.4]
    result = blocked_mean_interval(values, unit="repository family", iterations=1000, seed=7)
    assert result["units"] == 4 and result["confidence"] == 0.95
    assert result["low"] < result["point"] < result["high"]
    assert result == blocked_mean_interval(values, unit="repository family", iterations=1000, seed=7)
    assert blocked_mean_interval([0.0], unit="repository family")["low"] is None


def test_zero_bootstrap_iterations_cannot_manufacture_an_exact_interval():
    with pytest.raises(ValueError):
        blocked_mean_interval([0.0, 0.0], unit="repository family", iterations=0)
