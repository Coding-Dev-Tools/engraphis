from __future__ import annotations

import json

import pytest

from eval import jev_recall_quality as evaluation
from engraphis.core.query_planner import DeterministicQueryPlanner
from engraphis.service import MemoryService


def test_jev_evaluation_fixture_is_bounded_and_has_two_routes_per_task():
    tasks = evaluation.synthetic_tasks()

    assert len(tasks) == evaluation.MAX_REMOTE_REQUESTS == 40
    assert {category: sum(task.category == category for task in tasks)
            for category in evaluation.CATEGORIES} == {category: 10 for category in evaluation.CATEGORIES}
    assert all(len(DeterministicQueryPlanner().plan(task.query).queries) == 3 for task in tasks)
    assert len({task.answer_phrase for task in tasks}) == len(tasks)
    assert all(task.service_id in task.query and task.cache_id in task.query for task in tasks)


def test_graph_fixture_seeds_explicit_service_and_cache_identifiers():
    task = next(task for task in evaluation.synthetic_tasks()
                if task.category == "graph_relationships")
    service = MemoryService.create(":memory:", embed_model="", embed_dim=256)
    try:
        seeded = evaluation._seed(service, [task])[0]
        rows = service.store.conn.execute(
            "SELECT content FROM memories WHERE repo_id=?", (seeded.repo_id,),
        ).fetchall()

        assert len(rows) == 13
        assert all(task.service_id in str(row[0]) and task.cache_id in str(row[0]) for row in rows)
    finally:
        service.close()


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
    assert report["fixture"] == "synthetic-independent-tasks/v2"
    assert report["eligible_tasks"] == 40
    assert report["remote_calls"] == 0


def test_direct_remote_evaluation_requires_consent_before_client_lookup(monkeypatch):
    monkeypatch.setattr(
        evaluation,
        "select_decision_client",
        lambda _backend: (_ for _ in ()).throw(AssertionError("provider lookup attempted")),
    )

    with pytest.raises(PermissionError, match="explicit allow_remote=True consent"):
        evaluation.run_evaluation(timeout_s=8.0, max_requests=1)
