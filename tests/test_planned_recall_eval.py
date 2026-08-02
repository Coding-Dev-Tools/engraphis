"""Evidence-harness contracts for planned-recall release gates."""
from pathlib import Path

from eval.harness import load_dataset
from eval.planned_recall import (
    ABLATIONS,
    TOKEN_BUDGETS,
    _evidence_retention_quality,
    run,
)


DATASET = (
    Path(__file__).resolve().parents[1]
    / "eval"
    / "datasets"
    / "context_routing_stress.jsonl"
)


def test_context_routing_fixture_has_required_40_task_coverage():
    dataset = load_dataset(str(DATASET))

    assert sum(len(case["questions"]) for case in dataset) >= 40
    assert {case["category"] for case in dataset} == {
        "long_noisy_history",
        "mixed_memory_types",
        "multi_hop_relationship",
        "late_correction",
    }


def test_planned_recall_ablation_reports_budget_curves_and_gates():
    report = run(load_dataset(str(DATASET)))

    assert report["workload"]["tasks"] == 40
    assert report["benchmark"]["schema_versions"] == [7]
    assert set(report["methods"]) == set(ABLATIONS)
    for method in ABLATIONS:
        assert set(report["methods"][method]) == {str(value) for value in TOKEN_BUDGETS}
        for budget in TOKEN_BUDGETS:
            summary = report["methods"][method][str(budget)]
            assert summary["tasks"] == 40
            assert summary["exact_injected_tokens"]["total"] >= 0
            assert summary["latency_ms"]["p95"] >= 0
            assert summary["provider_cached_input_tokens"] is None
    gate = report["release_gates"]["planner_type_limits"]
    assert gate["safety_regressions_ok"] is None
    assert gate["opt_in_eligible"] is False
    assert len(gate["opt_in_blockers"]) == 2
    assert gate["default_eligible"] is False


def test_quality_requires_answer_bearing_excerpt_content_not_only_supporting_id():
    quality = _evidence_retention_quality(
        question="Which token format authenticates Borealis internal calls?",
        supporting_ids={"mem_support"},
        source_by_id={
            "mem_support": "Borealis internal calls use PASETO v4.public tokens."
        },
        excerpts_by_id={"mem_support": "Borealis internal calls use tokens."},
    )

    assert quality < 1.0
