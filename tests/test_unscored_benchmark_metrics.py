import json

import pytest

from engraphis.backends import DeterministicEmbedder
from eval import benchmark_analysis, external, external_checkpoints, harness


RETRIEVAL = ("recall_at_k", "hit_at_k", "mrr_at_k", "ndcg_at_k",
             "packed_recall_at_k", "packed_hit_at_k", "packed_mrr_at_k", "packed_ndcg_at_k")
ANSWERS = ("answer_token_recall", "packed_answer_token_recall")


def population(retrieval, answers):
    return [{"id": "one-case", "memories": [{"tag": "fact", "text": "Release is Tuesday."}],
             "questions": [{"id": "one-question", "q": "When is release?",
                            "supporting": ["fact"] if retrieval else [],
                            "answer": "Tuesday" if answers else "", "answerable": answers}]}]


@pytest.mark.parametrize("retrieval,answers", [(False, False), (False, True), (True, False)])
def test_unscored_populations_remain_undefined_in_fresh_and_cached_reports(tmp_path, retrieval, answers):
    cases = population(retrieval, answers)
    report = harness.run(cases)
    kwargs = {"directory": tmp_path / "checkpoints", "binding": {},
              "embedder": DeterministicEmbedder(), "snapshot": lambda: {}}
    fresh = external_checkpoints.run_resumable(cases, **kwargs)
    cached = external_checkpoints.run_resumable(cases, **kwargs)
    for result in (report, fresh, cached):
        assert result["scored_questions"] == int(retrieval)
        assert result["answer_scored_questions"] == int(answers)
        for names, scored in ((RETRIEVAL, retrieval), (ANSWERS, answers)):
            for name in names:
                assert (result[name] is not None) is scored
    assert (report["sufficient_evidence_proxy_rate"] is not None) is answers


def test_v2_unscored_metrics_and_intervals_are_null(tmp_path):
    dataset = tmp_path / "unscored.jsonl"
    cases = population(False, False)
    dataset.write_text(json.dumps(cases[0]) + "\n")
    report = harness.run(cases, v2=True, dataset_path=str(dataset), bootstrap_iterations=2)
    assert report["metrics"]["answer_token_recall"] is None
    for name, interval in report["metrics"]["confidence_intervals"].items():
        assert report["metrics"][name] is None
        assert interval["n"] == 0
        assert interval["point"] is interval["low"] is interval["high"] is None
    assert all(value is None for value in report["metrics"]["packed_evidence"].values())


def test_harness_cli_displays_unscored_and_cannot_pass_a_required_floor(tmp_path, capsys):
    dataset = tmp_path / "unscored.jsonl"
    dataset.write_text(json.dumps(population(False, False)[0]) + "\n")
    harness.main(["--dataset", str(dataset)])
    assert "unscored" in capsys.readouterr().out
    with pytest.raises(SystemExit) as error:
        harness._enforce_metric_floors(harness.run([]), "sample.jsonl")
    assert error.value.code == 1


def test_external_unscored_artifact_can_be_exported_and_analyzed(tmp_path, capsys):
    dataset = tmp_path / "locomo.json"
    dataset.write_text(json.dumps([{
        "sample_id": "conversation-1",
        "conversation": {"session_1_date_time": "2026-08-04",
                         "session_1": [{"speaker": "A", "dia_id": "D1:1", "text": "Release is Tuesday."}]},
        "qa": [{"question": "What is the unsupported secret?", "answer": "", "evidence": [], "category": 5}],
    }]))
    artifact = tmp_path / "artifact.json"
    assert external.main(["--dataset", str(dataset), "--format", "locomo", "--offline",
                          "--artifact", str(artifact)]) == 0
    assert "unscored" in capsys.readouterr().out
    report = benchmark_analysis.read_verified(artifact)
    assert all(report["metrics"][name] is None for name in RETRIEVAL + ANSWERS)
    summary = benchmark_analysis.summarize(artifact)
    assert summary["recall"]["point"] is summary["answer_token_evidence"]["point"] is None

    report["metrics"]["recall_at_k"] = 0.0
    artifact.write_text(json.dumps(report))
    artifact.with_suffix(".json.sha256").write_text(benchmark_analysis.sha256_file(artifact))
    with pytest.raises(ValueError, match="unscored aggregate must be undefined"):
        benchmark_analysis.read_verified(artifact)


def test_direct_external_export_keeps_recomputable_fractional_means(tmp_path):
    dataset = tmp_path / "locomo.json"
    dataset.write_text(json.dumps([{
        "sample_id": "conversation-1",
        "conversation": {"session_1_date_time": "2026-08-04", "session_1": [
            {"speaker": "A", "dia_id": "D1:1", "text": "Release is Tuesday."},
            {"speaker": "A", "dia_id": "D1:2", "text": "Owner is Delta."},
            {"speaker": "A", "dia_id": "D1:3", "text": "Color is blue."},
        ]},
        "qa": [{"question": "When is release Tuesday?", "answer": "Tuesday",
                "evidence": [f"D1:{i}"], "category": 1} for i in (1, 2, 3)],
    }]))
    artifact = tmp_path / "fractional.json"
    assert external.main(["--dataset", str(dataset), "--format", "locomo", "--offline", "--k", "1",
                          "--artifact", str(artifact)]) == 0
    report = benchmark_analysis.read_verified(artifact)
    assert report["metrics"]["recall_at_k"] == pytest.approx(1 / 3, abs=1e-6)
    assert benchmark_analysis.summarize(artifact)["status"] == "COMPLETE"
