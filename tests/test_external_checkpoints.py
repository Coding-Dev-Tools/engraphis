import json
import os
from pathlib import Path
import subprocess
import sys
import time

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


def test_restart_history_survives_cached_only_aggregation(tmp_path):
    def interrupted(*args, **kwargs):
        raise RuntimeError("simulated interruption")

    with pytest.raises(RuntimeError):
        execute(tmp_path, runner=interrupted)
    with pytest.raises(RuntimeError):
        execute(tmp_path, runner=interrupted, restart_interrupted=True)
    fresh = execute(tmp_path, restart_interrupted=True)
    assert fresh["explicit_local_restarts"] == 2
    receipts = {path.name: path.read_bytes() for path in tmp_path.glob("*.retry-*")}
    cached = execute(tmp_path, runner=lambda *args, **kwargs: pytest.fail("completed case replayed"))
    assert cached["explicit_local_restarts"] == fresh["explicit_local_restarts"]
    assert receipts == {path.name: path.read_bytes() for path in tmp_path.glob("*.retry-*")}


@pytest.mark.parametrize("corruption", ["wrong_case", "wrong_reason", "malformed", "gap", "unknown_case", "started"])
def test_retained_restart_receipts_are_validated_on_cached_resume(tmp_path, corruption):
    def interrupted(*args, **kwargs):
        raise RuntimeError("simulated interruption")

    with pytest.raises(RuntimeError):
        execute(tmp_path, runner=interrupted)
    execute(tmp_path, restart_interrupted=True)
    path = tmp_path / "case-00000.retry-000"
    value = json.loads(path.read_text())
    if corruption == "wrong_case":
        value["case_sha256"] = "0" * 64
        path.write_text(json.dumps(value))
    elif corruption == "wrong_reason":
        value["reason"] = "automatic retry"
        path.write_text(json.dumps(value))
    elif corruption == "malformed":
        path.write_text("[]")
    elif corruption == "gap":
        path.rename(path.with_name("case-00000.retry-001"))
    elif corruption == "unknown_case":
        path.rename(path.with_name("case-99999.retry-000"))
    else:
        (tmp_path / "case-00000.started").write_text(json.dumps({"case_sha256": "0" * 64, "ordinal": 0}))
    with pytest.raises(ValueError, match="restart receipt|start receipt"):
        execute(tmp_path)


def test_cached_case_without_retries_validates_retained_start_receipt(tmp_path):
    execute(tmp_path)
    assert not list(tmp_path.glob("*.retry-*"))
    start = tmp_path / "case-00000.started"
    start.write_text(json.dumps({"ordinal": 1, "case_sha256": "0" * 64}))
    with pytest.raises(ValueError, match="start receipt"):
        execute(tmp_path)


@pytest.mark.parametrize("payload", [b"", str(os.getpid()).encode("ascii"), b"{", b"unknown",
                                    b"\0", external_checkpoints.RUNNER_LOCK_MARKER + b"extra"])
def test_unrecognized_marker_fails_closed_without_changing_checkpoint_files(tmp_path, payload):
    marker = tmp_path / ".runner.lock"
    marker.write_bytes(payload)
    with pytest.raises(ValueError, match="manual inspection"):
        execute(tmp_path)
    assert marker.read_bytes() == payload
    assert sorted(path.name for path in tmp_path.iterdir()) == [".runner.lock"]


@pytest.mark.parametrize("prefix_length", [1, len(external_checkpoints.RUNNER_LOCK_MARKER) - 1])
def test_interrupted_marker_initialization_can_complete_under_lock(tmp_path, prefix_length):
    marker = tmp_path / ".runner.lock"
    marker.write_bytes(external_checkpoints.RUNNER_LOCK_MARKER[:prefix_length])
    execute(tmp_path)
    assert marker.read_bytes() == external_checkpoints.RUNNER_LOCK_MARKER
    previous = marker.stat()
    execute(tmp_path, runner=lambda *args, **kwargs: pytest.fail("completed case replayed"))
    assert marker.stat().st_mtime_ns == previous.st_mtime_ns
    assert marker.stat().st_ino == previous.st_ino


def test_hardlinked_marker_is_rejected_without_writing_target(tmp_path):
    target = tmp_path / "target"
    target.write_bytes(external_checkpoints.RUNNER_LOCK_MARKER[:1])
    os.link(target, tmp_path / ".runner.lock")
    with pytest.raises(ValueError, match="unsafe"):
        execute(tmp_path)
    assert target.read_bytes() == external_checkpoints.RUNNER_LOCK_MARKER[:1]
    assert not (tmp_path / "manifest.json").exists()


def test_existing_only_lock_probe_never_creates_a_directory_or_marker(tmp_path):
    marker = tmp_path / "missing" / ".runner.lock"
    with pytest.raises(FileNotFoundError):
        with external_checkpoints._runner_lock(marker, create=False):
            pytest.fail("missing marker was accepted")
    assert not marker.parent.exists()


@pytest.mark.parametrize("payload", [b"", b"123", b"unknown",
                                    external_checkpoints.RUNNER_LOCK_MARKER[:1]])
def test_existing_only_probe_does_not_repair_or_reclaim_markers(tmp_path, payload):
    marker = tmp_path / ".runner.lock"
    marker.write_bytes(payload)
    before = marker.stat()
    with pytest.raises(external_checkpoints.UnrecognizedRunnerLock):
        with external_checkpoints._runner_lock(marker, create=False):
            pytest.fail("unrecognized producer marker was accepted")
    assert marker.read_bytes() == payload
    assert marker.stat().st_mtime_ns == before.st_mtime_ns
    assert marker.stat().st_ino == before.st_ino


def test_existing_only_probe_rejects_a_dangling_symlink(tmp_path):
    marker = tmp_path / ".runner.lock"
    missing = tmp_path / "missing"
    try:
        marker.symlink_to(missing)
    except OSError:
        pytest.skip("symlink creation unavailable")
    with pytest.raises(ValueError, match="unsafe"):
        with external_checkpoints._runner_lock(marker, create=False):
            pytest.fail("dangling marker was treated as a completed producer")
    assert marker.is_symlink()
    assert not missing.exists()


def test_existing_only_probe_rejects_hardlinks_without_mutation(tmp_path):
    marker = tmp_path / ".runner.lock"
    target = tmp_path / "target"
    target.write_bytes(external_checkpoints.RUNNER_LOCK_MARKER)
    os.link(target, marker)
    before = target.stat()
    with pytest.raises(ValueError, match="unsafe"):
        with external_checkpoints._runner_lock(marker, create=False):
            pytest.fail("aliased marker was accepted")
    assert target.read_bytes() == external_checkpoints.RUNNER_LOCK_MARKER
    assert target.stat().st_mtime_ns == before.st_mtime_ns


@pytest.mark.parametrize("body_error", [False, True])
def test_lock_release_failure_preserves_an_active_runner_error(tmp_path, monkeypatch, body_error):
    if sys.platform == "win32":
        import msvcrt
        module, operation, unlock = msvcrt, "locking", msvcrt.LK_UNLCK
    else:
        module, operation, unlock = external_checkpoints.fcntl, "flock", external_checkpoints.fcntl.LOCK_UN
    original = getattr(module, operation)

    def fail_after_unlock(fd, mode, *args):
        result = original(fd, mode, *args)
        if mode == unlock:
            raise OSError("unlock failed")
        return result

    with monkeypatch.context() as scoped:
        scoped.setattr(module, operation, fail_after_unlock)
        expected = RuntimeError if body_error else OSError
        with pytest.raises(expected, match="runner failed" if body_error else "unlock failed"):
            with external_checkpoints._runner_lock(tmp_path / ".runner.lock"):
                if body_error:
                    raise RuntimeError("runner failed")
    assert execute(tmp_path)["checkpoint_status"] == "COMPLETE"


def test_abrupt_runner_death_releases_os_lock_for_explicit_resume(tmp_path):
    ready = tmp_path / "runner-ready"
    script = r'''
import sys
import time
from pathlib import Path
from engraphis.backends import DeterministicEmbedder
from eval.external_checkpoints import run_resumable

cases = [{"id": f"case-{i}", "memories": [{"tag": "fact", "text": "Release is Tuesday."}],
          "questions": [{"id": f"q-{i}", "q": "When is release?", "answer": "Tuesday",
                          "supporting": ["fact"]}]} for i in range(2)]

def interrupted(*args, **kwargs):
    Path(sys.argv[2]).write_text("ready", encoding="utf-8")
    while True:
        time.sleep(1)

run_resumable(cases, directory=Path(sys.argv[1]), binding={"dataset_sha256": "a" * 64},
              embedder=DeterministicEmbedder(), snapshot=lambda: {"code": "frozen"},
              runner=interrupted)
'''
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path), str(ready)],
        cwd=Path.cwd(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    try:
        deadline = time.monotonic() + 15
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        if not ready.exists():
            if process.poll() is None:
                process.kill()
            pytest.fail(process.communicate(timeout=10)[1] or "child runner did not become ready")
        retained = {path.name: path.read_bytes() for path in tmp_path.iterdir()
                    if path.name != ".runner.lock"}
        with pytest.raises(ValueError, match="already owns"):
            run_resumable(cases(), directory=tmp_path, binding={},
                          embedder=DeterministicEmbedder(), restart_interrupted=True,
                          snapshot=lambda: pytest.fail("manifest accessed before exclusive ownership"))
        assert retained == {path.name: path.read_bytes() for path in tmp_path.iterdir()
                            if path.name != ".runner.lock"}
        process.kill()
        process.wait(timeout=10)
        marker = tmp_path / ".runner.lock"
        assert marker.read_bytes() == external_checkpoints.RUNNER_LOCK_MARKER
        assert "case-00000.started" in retained
        with pytest.raises(ValueError, match="explicit restart"):
            execute(tmp_path)
        assert retained == {path.name: path.read_bytes() for path in tmp_path.iterdir()
                            if path.name != ".runner.lock"}
        report = execute(tmp_path, restart_interrupted=True)
        assert report["checkpoint_status"] == "COMPLETE"
        assert report["explicit_local_restarts"] == 1
        assert [path.name for path in tmp_path.glob("*.retry-*")] == ["case-00000.retry-000"]
        assert all((tmp_path / name).read_bytes() == payload for name, payload in retained.items())
        completed = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
        cached = execute(tmp_path, runner=lambda *args, **kwargs: pytest.fail("completed case replayed"))
        assert cached["questions"] == report["questions"] == 2
        assert cached["explicit_local_restarts"] == 1
        assert completed == {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)
