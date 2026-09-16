import json

import pytest

from engraphis.backends import DeterministicEmbedder
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
