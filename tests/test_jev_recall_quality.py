from __future__ import annotations

import json

from eval import jev_recall_quality as evaluation
from engraphis.core.query_planner import DeterministicQueryPlanner


def test_jev_evaluation_fixture_is_bounded_and_has_two_routes_per_task():
    tasks = evaluation.synthetic_tasks()

    assert len(tasks) == evaluation.MAX_REMOTE_REQUESTS == 40
    assert {category: sum(task.category == category for task in tasks)
            for category in evaluation.CATEGORIES} == {category: 10 for category in evaluation.CATEGORIES}
    assert all(len(DeterministicQueryPlanner().plan(task.query).queries) == 3 for task in tasks)
    assert len({task.answer_phrase for task in tasks}) == len(tasks)


def test_jev_evaluation_benefit_gate_requires_improvement_and_noninferiority():
    assert evaluation.benefit_gate({
        "ndcg_at_5": (0.1, 0.01, 0.2),
        "recall_at_5": (0.0, 0.0, 0.0),
        "answer_token_coverage": (0.0, 0.0, 0.0),
    })
    assert not evaluation.benefit_gate({
        "ndcg_at_5": (0.1, 0.01, 0.2),
        "recall_at_5": (-0.01, -0.02, 0.01),
        "answer_token_coverage": (0.0, 0.0, 0.0),
    })
    assert not evaluation.benefit_gate({
        "ndcg_at_5": (0.0, 0.0, 0.0),
        "recall_at_5": (0.0, 0.0, 0.0),
        "answer_token_coverage": (0.0, 0.0, 0.0),
    })


def test_check_only_does_not_resolve_or_call_a_provider(monkeypatch, capsys):
    monkeypatch.setattr(
        evaluation,
        "select_decision_client",
        lambda _backend: (_ for _ in ()).throw(AssertionError("provider lookup attempted")),
    )

    assert evaluation.main(["--check-only", "--max-requests", "40"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["tasks"] == 40
    assert report["eligible_tasks"] == 40
    assert report["remote_calls"] == 0
