"""Release contract: the grounded-recall eval separates evidence from overlap.

Locks the extended ``eval/grounded.py`` fixture to its deterministic outcome:
answerable queries ground, off-topic queries abstain even with a
lexically-overlapping distractor live in the store, and the probe whose only
supporting memory is quarantined abstains without leaking the answer.
"""
from eval import grounded


def test_grounded_eval_fixture_stays_perfect_with_distractors():
    report = grounded.run()
    assert report["answer_rate"] == 1.0
    assert report["abstain_rate"] == 1.0
    assert report["accuracy"] == 1.0


def test_grounded_eval_reports_quarantine_abstain_separately():
    report = grounded.run()
    assert report["n_quarantine"] == len(grounded.QUARANTINE_PROBES) >= 1
    assert report["quarantine_hits"] == report["n_quarantine"]
    assert grounded.DISTRACTOR_FACTS, "distractor fixture must not be empty"
    assert grounded.QUARANTINED_FACTS, "quarantine fixture must not be empty"


def test_quarantine_probe_abstains_without_leaking_the_answer():
    eng, wid, rid = grounded._engine()
    for probe in grounded.QUARANTINE_PROBES:
        answer = eng.grounded_recall(probe, workspace_id=wid, repo_id=rid)
        assert answer.abstained and not answer.grounded
        assert answer.answer == "" and answer.citations == []
        assert "bluebird" not in answer.answer.lower()
