"""Evidence-harness contracts for planned-recall release gates."""
from pathlib import Path

import pytest

from eval.harness import load_dataset
from eval.planned_recall import (
    ABLATIONS,
    TOKEN_BUDGETS,
    _evidence_retention_quality,
    _validate_dataset,
    main,
    require_gate,
    run,
)
from eval import planned_recall
from engraphis.core.schema import SCHEMA_VERSION


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
    assert report["benchmark"]["schema_versions"] == [SCHEMA_VERSION]
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


def test_dataset_validation_rejects_unknown_support_instead_of_awarding_perfect_quality():
    cases = load_dataset(str(DATASET))
    cases[0]["questions"][0]["supporting"] = ["missing-tag"]

    with pytest.raises(ValueError, match="unknown supporting memory tags"):
        _validate_dataset(cases)


@pytest.mark.parametrize("value", [False, None, "true", 1])
def test_requested_gate_rejects_false_missing_and_non_boolean_results(value):
    report = {"release_gates": {"planner": {"repository_local_gate_pass": value}}}
    with pytest.raises(ValueError, match="failed"):
        require_gate(report, "planner", level="repository-local")


def test_local_gate_does_not_authorize_default_promotion():
    report = {"release_gates": {"planner": {
        "repository_local_gate_pass": True,
        "safety_regressions_ok": True,
        "opt_in_eligible": False,
        "default_eligible": False,
    }}}
    require_gate(report, "planner", level="repository-local")
    with pytest.raises(ValueError, match="default_eligible"):
        require_gate(report, "planner")


def test_promotion_requires_all_prerequisite_booleans():
    gate = {
        "repository_local_gate_pass": True,
        "safety_regressions_ok": True,
        "opt_in_eligible": True,
        "default_eligible": True,
    }
    report = {"release_gates": {"planner": gate}}
    require_gate(report, "planner")
    for prerequisite in gate:
        incomplete = {**gate, prerequisite: False}
        with pytest.raises(ValueError, match=prerequisite):
            require_gate({"release_gates": {"planner": incomplete}}, "planner")


def test_default_report_does_not_fail_for_unpromoted_experiments(monkeypatch, capsys):
    monkeypatch.setattr(planned_recall, "load_dataset", lambda path: [])
    monkeypatch.setattr(planned_recall, "run", lambda dataset: {
        "release_gates": {"planner": {"repository_local_gate_pass": False, "default_eligible": False}},
    })
    assert main([]) is None
    assert "false" in capsys.readouterr().out


def test_cli_explicit_promotion_request_fails_but_keeps_report(monkeypatch, capsys):
    monkeypatch.setattr(planned_recall, "load_dataset", lambda path: [])
    monkeypatch.setattr(planned_recall, "run", lambda dataset: {
        "release_gates": {"planner": {"repository_local_gate_pass": True, "default_eligible": False}},
    })
    with pytest.raises(SystemExit) as failure:
        main(["--require-gate", "planner"])
    assert failure.value.code == 1
    captured = capsys.readouterr()
    assert '"default_eligible": false' in captured.out
    assert "required default gate for planner failed" in captured.err


def test_cli_gate_level_requires_an_explicit_candidate():
    with pytest.raises(SystemExit) as failure:
        main(["--gate-level", "repository-local"])
    assert failure.value.code == 2
