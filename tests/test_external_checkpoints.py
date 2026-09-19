import json

import pytest

from engraphis.backends import DeterministicEmbedder
from eval import external_checkpoints
from eval.benchmark import canonical_json, sha256_text
from eval.external_checkpoints import run_resumable


def cases():
    return [{"id": f"case-{i}", "memories": [{"tag": "fact", "text": "Release is Tuesday."}],
             "questions": [{"id": f"q-{i}", "q": "When is release?", "answer": "Tuesday",
                            "supporting": ["fact"]}]} for i in range(2)]


def execute(tmp_path, **kwargs):
    return run_resumable(cases(), directory=tmp_path, binding={"dataset_sha256": "a" * 64},
                          embedder=DeterministicEmbedder(), snapshot=lambda: {"code": "frozen"}, **kwargs)


def test_external_resume_preserves_full_denominator_and_no_duplicate(tmp_path):
    report = execute(tmp_path, maximum_cases=1)
    assert report["checkpoint_status"] == "PARTIAL"
    assert report["completed_cases"] == 1
    first_bytes = (tmp_path / "case-00000.json").read_bytes()
    report = execute(tmp_path)
    assert report["checkpoint_status"] == "COMPLETE"
    assert report["questions"] == 2
    assert report["scored_questions"] == 2
    assert (tmp_path / "case-00000.json").read_bytes() == first_bytes
    report = execute(tmp_path, runner=lambda *args, **kwargs: pytest.fail("completed case replayed"))
    assert report["questions"] == 2


def test_external_changed_config_or_source_rejected(tmp_path):
    execute(tmp_path, maximum_cases=1)
    with pytest.raises(ValueError, match="drift"):
        execute(tmp_path, k=5)


def test_external_tampered_checkpoint_rejected(tmp_path):
    execute(tmp_path, maximum_cases=1)
    path = tmp_path / "case-00000.json"
    value = json.loads(path.read_text())
    value["report"]["detail"][0]["packed_recall_at_k"] = 0.0
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="content changed"):
        execute(tmp_path)


def test_external_interruption_needs_explicit_local_restart(tmp_path):
    def interrupted(*args, **kwargs):
        raise RuntimeError("simulated process interruption")

    with pytest.raises(RuntimeError):
        execute(tmp_path, runner=interrupted)
    with pytest.raises(ValueError, match="explicit restart"):
        execute(tmp_path)
    report = execute(tmp_path, restart_interrupted=True)
    assert report["explicit_local_restarts"] == 1
    assert (tmp_path / "case-00000.retry-000").exists()


def test_source_drift_during_execution_cannot_write_completion(tmp_path):
    state = {"count": 0}

    def snapshot():
        state["count"] += 1
        return {"code": "before" if state["count"] == 1 else "after"}

    with pytest.raises(ValueError, match="changed during"):
        run_resumable(cases(), directory=tmp_path, binding={}, embedder=DeterministicEmbedder(), snapshot=snapshot)
    assert not (tmp_path / "case-00000.json").exists()


def test_checkpoint_categories_without_retrieval_labels_remain_undefined(tmp_path):
    population = cases()
    population[0]["questions"][0].update(
        category="abstention", supporting=[], answerable=False, answer="",
    )
    population[1]["questions"][0]["category"] = "answerable"
    report = run_resumable(population, directory=tmp_path, binding={},
                           embedder=DeterministicEmbedder(), snapshot=lambda: {})
    unscored = report["category_metrics"]["abstention"]
    assert unscored["questions"] == 1
    assert unscored["retrieval_scored_questions"] == 0
    assert unscored["recall_at_k"] is None
    assert unscored["packed_recall_at_k"] is None
    scored = report["category_metrics"]["answerable"]
    assert scored["retrieval_scored_questions"] == 1
    assert scored["recall_at_k"] == scored["packed_recall_at_k"] == 1.0


@pytest.mark.parametrize("corruption", ["wrong_id", "duplicate", "missing", "malformed",
                                        "wrong_category", "unscored", "missing_metric"])
def test_rehashed_cached_report_requires_exact_question_coverage(tmp_path, corruption):
    execute(tmp_path, maximum_cases=1)
    path = tmp_path / "case-00000.json"
    checkpoint = json.loads(path.read_text())
    report = checkpoint["report"]
    if corruption == "wrong_id":
        report["detail"][0]["question_id"] = "q-1"
    elif corruption == "duplicate":
        report["detail"].append(dict(report["detail"][0]))
    elif corruption == "missing":
        report.pop("detail")
    elif corruption == "malformed":
        report["detail"] = [None]
    elif corruption == "wrong_category":
        report["detail"][0]["category"] = "wrong"
    elif corruption == "unscored":
        report["detail"][0]["retrieval_scored"] = False
    else:
        report["detail"][0].pop("recall_at_k")
    checkpoint["report_sha256"] = sha256_text(canonical_json(report))
    path.write_text(json.dumps(checkpoint))
    with pytest.raises(ValueError, match="exact question coverage|invalid scored detail"):
        execute(tmp_path)


def test_cached_only_resume_checks_producer_after_loading(tmp_path):
    execute(tmp_path)
    calls = 0

    def snapshot():
        nonlocal calls
        calls += 1
        return {"code": "frozen" if calls == 1 else "changed"}

    with pytest.raises(ValueError, match="changed during checkpoint aggregation"):
        run_resumable(cases(), directory=tmp_path, binding={"dataset_sha256": "a" * 64},
                      embedder=DeterministicEmbedder(), snapshot=snapshot)


@pytest.mark.parametrize("changed", ["embedder", "environment"])
def test_checkpoint_resume_rejects_actual_runtime_drift(tmp_path, monkeypatch, changed):
    execute(tmp_path, maximum_cases=1)
    embedder = DeterministicEmbedder()
    if changed == "embedder":
        embedder = DeterministicEmbedder(dim=embedder.dim + 1)
    else:
        original = external_checkpoints.environment_provenance
        monkeypatch.setattr(external_checkpoints, "environment_provenance",
                            lambda: {**original(), "packages": {"numpy": "changed"}})
    with pytest.raises(ValueError, match="drift"):
        run_resumable(cases(), directory=tmp_path, binding={"dataset_sha256": "a" * 64},
                      embedder=embedder, snapshot=lambda: {"code": "frozen"})


def test_checkpoint_does_not_invent_labels_for_an_unlabeled_document(tmp_path):
    case = cases()[0]
    case["document"] = case.pop("memories")[0]["text"]
    case["questions"][0].pop("supporting")
    arguments = {"directory": tmp_path, "binding": {}, "embedder": DeterministicEmbedder(),
                 "snapshot": lambda: {}}
    fresh = run_resumable([case], **arguments)
    cached = run_resumable([case], **arguments)
    assert fresh["scored_questions"] == cached["scored_questions"] == 0
    assert fresh["category_metrics"]["unknown"]["recall_at_k"] is None
    assert cached["category_metrics"]["unknown"]["recall_at_k"] is None
