import json
import sys
from types import SimpleNamespace

import pytest

from eval import local_benchmark_queue as queue
from eval import benchmark_analysis


def plan(monkeypatch):
    monkeypatch.setattr(queue, "snapshot", lambda: {"producer": "frozen"})
    value = {"schema": queue.SCHEMA, "source": queue.snapshot(), "wait_for": None, "inputs": {},
             "jobs": [{"id": "smoke", "module": "eval.engine_capacity", "args": ["--smoke"]}]}
    value["binding_sha256"] = queue.digest(value)
    return value


def test_queue_refuses_hosted_modules_even_after_rehash(monkeypatch):
    value = plan(monkeypatch)
    value["jobs"][0]["module"] = "eval.benchmark_campaign"
    value["binding_sha256"] = queue.digest({key: v for key, v in value.items() if key != "binding_sha256"})
    with pytest.raises(ValueError, match="allowed"):
        queue.validate(value)


def test_queue_resumes_completed_jobs_without_dispatch(monkeypatch, tmp_path):
    value = plan(monkeypatch)
    calls = []
    def runner(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0)
    assert queue.execute(value, tmp_path, runner=runner)["status"] == "COMPLETE"
    assert queue.execute(value, tmp_path, runner=runner)["status"] == "COMPLETE"
    assert len(calls) == 1


def test_queue_preserves_failure_and_stops_resuming(monkeypatch, tmp_path):
    value = plan(monkeypatch)
    with pytest.raises(ValueError, match="failure"):
        queue.execute(value, tmp_path, runner=lambda *a, **k: SimpleNamespace(returncode=2))
    assert json.loads((tmp_path / "status.json").read_text())["status"] == "BLOCKED"
    with pytest.raises(ValueError, match="previous job failed"):
        queue.execute(value, tmp_path, runner=lambda *a, **k: pytest.fail("replayed"))


def test_queue_source_drift_prevents_dispatch(monkeypatch, tmp_path):
    value = plan(monkeypatch)
    monkeypatch.setattr(queue, "snapshot", lambda: {"producer": "changed"})
    with pytest.raises(ValueError, match="source changed"):
        queue.execute(value, tmp_path, runner=lambda *a, **k: pytest.fail("dispatched"))


def test_queue_revalidates_source_after_job_before_checkpoint(monkeypatch, tmp_path):
    value = plan(monkeypatch)
    snapshots = iter(({"producer": "frozen"}, {"producer": "frozen"}, {"producer": "changed"}))
    monkeypatch.setattr(queue, "snapshot", lambda: next(snapshots))

    with pytest.raises(ValueError, match="source changed"):
        queue.execute(value, tmp_path, runner=lambda *a, **k: SimpleNamespace(returncode=0))

    assert not (tmp_path / "smoke.json").exists()
    assert (tmp_path / "smoke.started").is_file()


def test_queue_interrupted_job_is_not_automatically_retried(monkeypatch, tmp_path):
    value = plan(monkeypatch)
    (tmp_path / "smoke.started").write_text("{}")
    with pytest.raises(ValueError, match="interrupted"):
        queue.execute(value, tmp_path, runner=lambda *a, **k: pytest.fail("replayed"))


def test_queue_input_drift_prevents_dispatch(monkeypatch, tmp_path):
    value = plan(monkeypatch)
    monkeypatch.setattr(queue, "ROOT", tmp_path)
    source = tmp_path / "data.json"
    source.write_text('{"source": 1}')
    value["inputs"] = {"data.json": queue.sha256_file(source)}
    value["binding_sha256"] = queue.digest({key: v for key, v in value.items() if key != "binding_sha256"})
    source.write_text('{"source": 2}')
    with pytest.raises(ValueError, match="input changed"):
        queue.execute(value, tmp_path / "results", runner=lambda *a, **k: pytest.fail("dispatched"))


def test_queue_records_runtime_and_can_stop_after_a_completed_job(monkeypatch, tmp_path):
    value = plan(monkeypatch)
    second = {"id": "second", "module": "eval.engine_capacity", "args": ["--smoke"]}
    value["jobs"].append(second)
    value["binding_sha256"] = queue.digest({key: v for key, v in value.items()
                                             if key != "binding_sha256"})
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0)

    result = queue.execute(value, tmp_path, runner=runner, stop_after_job="smoke")
    assert result == {"status": "PAUSED", "completed_jobs": ["smoke"],
                      "stopped_after_job": "smoke"}
    assert len(calls) == 1
    status = json.loads((tmp_path / "status.json").read_text())
    assert status["status"] == "PAUSED" and status["runtime"]["python_executable"]
    checkpoint = json.loads((tmp_path / "smoke.json").read_text())
    assert checkpoint["runtime"]["packages"]

    assert queue.execute(value, tmp_path, runner=runner)["status"] == "COMPLETE"
    assert len(calls) == 2


def test_queue_rejects_nonpositive_job_timeout(monkeypatch):
    value = plan(monkeypatch)
    value["jobs"][0]["timeout_seconds"] = 0
    value["binding_sha256"] = queue.digest({key: v for key, v in value.items()
                                             if key != "binding_sha256"})
    with pytest.raises(ValueError, match="timeout_seconds"):
        queue.validate(value)


def test_queue_watchdog_keeps_started_attempt_on_timeout(monkeypatch, tmp_path):
    value = plan(monkeypatch)

    class Process:
        pid = 4242

        def poll(self):
            return None

        def kill(self):
            self.killed = True

        def wait(self, **_kwargs):
            return -9

    class Child:
        def __init__(self):
            self.killed = False

        def kill(self):
            self.killed = True

        def wait(self, **_kwargs):
            return None

    child = Child()

    class Root:
        def children(self, *, recursive):
            assert recursive is True
            return [child]

    process = Process()
    monkeypatch.setattr(queue.subprocess, "Popen", lambda *_a, **_k: process)
    monkeypatch.setitem(sys.modules, "psutil", SimpleNamespace(Process=lambda _pid: Root()))
    ticks = iter((0.0, 0.0, 2.0, 2.0, 2.0, 2.0, 2.0))
    monkeypatch.setattr(queue.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(queue.time, "sleep", lambda _seconds: None)
    with pytest.raises(queue.JobTimeoutError, match="timeout"):
        queue.execute(value, tmp_path, poll_seconds=0.01, default_timeout_seconds=1)
    assert process.killed
    assert child.killed
    assert (tmp_path / "smoke.started").is_file()
    status = json.loads((tmp_path / "status.json").read_text())
    assert status["status"] == "BLOCKED"
    assert status["process_tree_teardown"]["descendants_killed"] == 1


def test_queue_requires_complete_external_analysis_artifact(tmp_path):
    valid = tmp_path / "valid.json"
    # Retained analyses bind their historical producer. Exercise a fresh output
    # instead of treating an old producer checksum as current implementation.
    assert benchmark_analysis.main([
        "--reports", "docs/benchmark-evidence/longmemeval-full-20260916.json",
        "docs/benchmark-evidence/longmemeval-budget4096-20260916.json",
        "--compare", "--output", str(valid),
    ]) == 0
    report = json.loads(valid.read_text(encoding="utf-8"))
    assert queue._verified_artifact(valid)["schema"] == "engraphis-external-analysis/v1"

    report["reports"][0]["status"] = "PARTIAL"
    invalid = tmp_path / "invalid.json"
    invalid.write_text(json.dumps(report), encoding="utf-8")
    invalid.with_suffix(".json.sha256").write_text(
        f"{queue.sha256_file(invalid)}  invalid.json\n", encoding="utf-8")
    with pytest.raises(ValueError, match="incomplete"):
        queue._verified_artifact(invalid)


def test_queue_waits_for_producer_lock_release_before_verifying_artifact(monkeypatch, tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setattr(queue, "ROOT", root)
    value = plan(monkeypatch)
    artifact = root / "diagnostic.json"
    artifact.write_text("{}", encoding="utf-8")
    producer_lock = root / "producer.lock"
    producer_lock.write_text("running", encoding="utf-8")
    value["wait_for"] = {"artifact": "diagnostic.json", "producer_lock": "producer.lock"}
    value["binding_sha256"] = queue.digest({key: v for key, v in value.items()
                                             if key != "binding_sha256"})
    verified_while_locked = []

    def verify(path):
        verified_while_locked.append(producer_lock.exists())
        return {"schema": "test"}

    monkeypatch.setattr(queue, "_verified_artifact", verify)
    original_sleep = queue.time.sleep

    def release_lock(seconds):
        if producer_lock.exists():
            producer_lock.unlink()
        original_sleep(0)

    monkeypatch.setattr(queue.time, "sleep", release_lock)
    result = queue.execute(
        value, tmp_path / "results", runner=lambda *a, **k: SimpleNamespace(returncode=0),
        poll_seconds=0.01,
    )

    assert result["status"] == "COMPLETE"
    assert verified_while_locked == [False]


def _analysis_artifact(path):
    report = {"schema": "engraphis-external-analysis/v1", "source_sha256": "a" * 64,
              "reports": [{"schema": "engraphis-external-analysis/v1", "status": "COMPLETE",
                           "input_sha256": "b" * 64, "dataset_sha256": "c" * 64,
                           "input_artifact": "input.json", "dataset": "fixture",
                           "configuration": {}, "models": {}, "questions": 0,
                           "retrieval_scored_questions": 0}]}
    path.write_text(json.dumps(report), encoding="utf-8")
    digest = queue.sha256_file(path)
    path.with_suffix(".json.sha256").write_text(digest, encoding="utf-8")
    return report, digest


@pytest.mark.parametrize("resume", [False, True])
def test_queue_checkpoint_binds_the_snapshot_it_validated(tmp_path, monkeypatch, resume):
    monkeypatch.setattr(queue, "ROOT", tmp_path)
    value = plan(monkeypatch)
    value["jobs"][0]["artifacts"] = ["analysis.json"]
    value["binding_sha256"] = queue.digest({key: item for key, item in value.items() if key != "binding_sha256"})
    artifact = tmp_path / "analysis.json"
    _, expected = _analysis_artifact(artifact)
    def runner(*args, **kwargs):
        return SimpleNamespace(returncode=0)
    directory = tmp_path / "results"
    if resume:
        queue.execute(value, directory, runner=runner)

    original = type(artifact).read_bytes
    def replace_after_read(path):
        payload = original(path)
        if path == artifact:
            path.write_text('{"replacement": true}', encoding="utf-8")
        return payload
    monkeypatch.setattr(type(artifact), "read_bytes", replace_after_read)

    assert queue.execute(value, directory, runner=runner)["status"] == "COMPLETE"
    checkpoint = json.loads((directory / "smoke.json").read_text(encoding="utf-8"))
    assert checkpoint["artifact_sha256"]["analysis.json"] == expected
    with pytest.raises(ValueError, match="checksum"):
        queue.execute(value, directory, runner=runner)


@pytest.mark.parametrize("sidecar", ["", "0" * 64])
def test_queue_rejects_empty_or_changed_checksum(tmp_path, monkeypatch, sidecar):
    monkeypatch.setattr(queue, "ROOT", tmp_path)
    path = tmp_path / "analysis.json"
    _analysis_artifact(path)
    path.with_suffix(".json.sha256").write_text(sidecar)
    with pytest.raises(ValueError, match="checksum"):
        queue._verified_artifact(path)
