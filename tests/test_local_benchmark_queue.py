import json
from pathlib import Path
import subprocess
import sys
import time
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


_SUBPROCESS_OPTIONS = {
    "creationflags": subprocess.CREATE_NO_WINDOW,
} if sys.platform == "win32" else {}

_REAL_QUEUE_OWNER = r"""
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from eval import local_benchmark_queue as queue

plan = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
directory = Path(sys.argv[1])
ready = Path(sys.argv[3])
calls = Path(sys.argv[4])
error = Path(sys.argv[5])


def record(value):
    with calls.open("a", encoding="utf-8") as handle:
        handle.write(value + "\n")
        handle.flush()


def runner(command, **_kwargs):
    marker = command[-1]
    record(marker)
    if marker == "--second":
        ready.write_text("ready", encoding="utf-8")
        while True:
            time.sleep(1)
    return SimpleNamespace(returncode=0)


try:
    queue.execute(plan, directory, runner=runner, poll_seconds=0.02,
                  default_timeout_seconds=3600)
except BaseException as exc:
    error.write_text(type(exc).__name__ + ":" + str(exc), encoding="utf-8")
    raise
"""

_CONTENDER = r"""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

from eval import local_benchmark_queue as queue

plan = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
directory = Path(sys.argv[1])
dispatched = Path(sys.argv[3])
error = Path(sys.argv[4])


def runner(*_args, **_kwargs):
    dispatched.write_text("dispatched", encoding="utf-8")
    return SimpleNamespace(returncode=0)


try:
    queue.execute(plan, directory, runner=runner, poll_seconds=0.02,
                  default_timeout_seconds=3600)
except BaseException as exc:
    error.write_text(type(exc).__name__ + ":" + str(exc), encoding="utf-8")
    raise
"""

_PRODUCER_HOLDER = r"""
import sys
import time
from pathlib import Path

from eval.external_checkpoints import _runner_lock

lock = Path(sys.argv[1])
ready = Path(sys.argv[2])
with _runner_lock(lock):
    ready.write_text("ready", encoding="utf-8")
    while True:
        time.sleep(1)
"""

_WAIT_QUEUE = r"""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

from eval import local_benchmark_queue as queue

plan = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
directory = Path(sys.argv[1])
started = Path(sys.argv[3])
verified = Path(sys.argv[4])
done = Path(sys.argv[5])
error = Path(sys.argv[6])
# Isolate filesystem roots for this process/lock regression; source drift is
# covered separately by the queue manifest tests.
queue.ROOT = directory.parent
queue.snapshot = lambda: plan["source"]
started.write_text("started", encoding="utf-8")


def verify(_path):
    verified.write_text("verified", encoding="utf-8")
    return {"schema": "test"}


def runner(*_args, **_kwargs):
    done.write_text("done", encoding="utf-8")
    return SimpleNamespace(returncode=0)


queue._verified_artifact = verify
try:
    queue.execute(plan, directory, runner=runner, poll_seconds=0.02,
                  wait_timeout=60, default_timeout_seconds=60)
except BaseException as exc:
    error.write_text(type(exc).__name__ + ":" + str(exc), encoding="utf-8")
    raise
"""


def _real_subprocess_plan():
    value = {
        "schema": queue.SCHEMA,
        "source": queue.snapshot(),
        "wait_for": None,
        "inputs": {},
        "jobs": [
            {"id": "first", "module": "eval.engine_capacity", "args": ["--first"]},
            {"id": "second", "module": "eval.engine_capacity", "args": ["--second"]},
        ],
    }
    value["binding_sha256"] = queue.digest({
        key: item for key, item in value.items() if key != "binding_sha256"
    })
    queue.validate(value)
    return value


def _wait_plan(lock, artifact):
    value = {
        "schema": queue.SCHEMA,
        "source": queue.snapshot(),
        "wait_for": {"artifact": artifact.name, "producer_lock": lock.name},
        "inputs": {},
        "jobs": [{"id": "wait", "module": "eval.engine_capacity", "args": ["--wait"]}],
    }
    value["binding_sha256"] = queue.digest({
        key: item for key, item in value.items() if key != "binding_sha256"
    })
    queue.validate(value)
    return value


def _wait_for_marker(path, process, *, timeout=20):
    deadline = time.monotonic() + timeout
    while not path.exists():
        if process.poll() is not None:
            stdout, stderr = process.communicate(timeout=10)
            raise AssertionError(
                f"queue owner exited before marker: {process.returncode}; "
                f"stdout={stdout!r}; stderr={stderr!r}"
            )
        if time.monotonic() >= deadline:
            process.kill()
            process.wait(timeout=10)
            process.communicate(timeout=10)
            raise AssertionError(f"timed out waiting for {path}")
        time.sleep(0.05)


def _stop_process(process):
    if process.poll() is None:
        process.kill()
    try:
        process.wait(timeout=10)
    finally:
        process.communicate(timeout=10)


def _wait_for_prerequisite_status(directory, process, *, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate(timeout=10)
            raise AssertionError(f"queue exited before waiting: {stdout!r}; {stderr!r}")
        try:
            status = json.loads((directory / "status.json").read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            status = {}
        if status.get("phase") == "waiting_for_existing_diagnostic":
            return
        time.sleep(0.02)
    raise AssertionError("queue did not wait for the prerequisite producer")


def _start_owner(plan_path, result_dir, ready, calls, error):
    return subprocess.Popen(
        [sys.executable, "-c", _REAL_QUEUE_OWNER, str(result_dir), str(plan_path),
         str(ready), str(calls), str(error)],
        cwd=Path.cwd(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        **_SUBPROCESS_OPTIONS,
    )


def _start_wait_queue(plan_path, result_dir, started, verified, done, error):
    return subprocess.Popen(
        [sys.executable, "-c", _WAIT_QUEUE, str(result_dir), str(plan_path),
         str(started), str(verified), str(done), str(error)],
        cwd=Path.cwd(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        **_SUBPROCESS_OPTIONS,
    )


def test_real_queue_lock_releases_after_owner_kill_without_replay(tmp_path):
    """A killed queue leaves receipts inspectable and releases only its OS lock."""
    plan = _real_subprocess_plan()
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    result_dir = tmp_path / "results"
    ready = tmp_path / "owner-ready"
    calls = tmp_path / "owner-calls"
    owner_error = tmp_path / "owner-error"
    owner = _start_owner(plan_path, result_dir, ready, calls, owner_error)

    try:
        _wait_for_marker(ready, owner)
        assert owner.poll() is None
        assert (result_dir / "first.json").is_file()
        assert not (result_dir / "first.started").exists()
        assert (result_dir / "second.started").is_file()
        before_owner_entries = {
            path.name: path.read_bytes() for path in result_dir.iterdir()
            if path.name != ".runner.lock"
        }

        # A live owner still excludes a competing queue before its runner can dispatch,
        # and the contender must not write queue status, receipts, or checkpoints.
        contender_marker = tmp_path / "contender-dispatched"
        contender_error = tmp_path / "contender-error"
        contender = subprocess.run(
            [sys.executable, "-c", _CONTENDER, str(result_dir), str(plan_path),
             str(contender_marker), str(contender_error)],
            cwd=Path.cwd(), capture_output=True, text=True, check=False, timeout=10,
            **_SUBPROCESS_OPTIONS,
        )
        assert contender.returncode != 0
        assert not contender_marker.exists()
        assert owner.poll() is None
        assert before_owner_entries == {
            path.name: path.read_bytes() for path in result_dir.iterdir()
            if path.name != ".runner.lock"
        }
        busy_error = contender_error.read_text(encoding="utf-8").lower()
        assert (
            ("already" in busy_error and any(
                term in busy_error for term in ("runner", "queue", "directory")
            )) or "locked" in busy_error
        )

        _stop_process(owner)
        owner = None
        assert (result_dir / ".runner.lock").exists()
        assert (result_dir / "first.json").is_file()
        assert (result_dir / "second.started").is_file()
        assert calls.read_text(encoding="utf-8").splitlines() == ["--first", "--second"]

        # The OS lock is released by process death, but the durable started receipt
        # still blocks an implicit retry and no runner call is made.
        blocked_calls = []

        def must_not_replay(command, **_kwargs):
            blocked_calls.append(command[-1])
            raise AssertionError("interrupted job was replayed")

        with pytest.raises(ValueError, match="interrupted"):
            queue.execute(plan, result_dir, runner=must_not_replay, poll_seconds=0.02)
        assert blocked_calls == []

        # Explicit reconciliation preserves both original receipts before allowing a
        # new attempt; the already-completed first job remains cached.
        interrupted = result_dir / "second.started"
        retained_receipt = result_dir / "second.started.reconciled"
        original_receipt = interrupted.read_bytes()
        interrupted.replace(retained_receipt)
        assert retained_receipt.read_bytes() == original_receipt
        interrupted_log = result_dir / "second.log"
        retained_log = result_dir / "second.log.reconciled"
        original_log = interrupted_log.read_bytes()
        interrupted_log.replace(retained_log)
        assert retained_log.read_bytes() == original_log
        first_checkpoint = (result_dir / "first.json").read_bytes()

        resumed_calls = []

        def resume_runner(command, **_kwargs):
            resumed_calls.append(command[-1])
            return SimpleNamespace(returncode=0)

        assert queue.execute(plan, result_dir, runner=resume_runner, poll_seconds=0.02)[
            "status"
        ] == "COMPLETE"
        assert resumed_calls == ["--second"]
        assert (result_dir / "first.json").read_bytes() == first_checkpoint
        assert (result_dir / "second.json").is_file()
        assert not (result_dir / "second.started").exists()
        assert retained_receipt.read_bytes() == original_receipt
        assert retained_log.read_bytes() == original_log

        # A cached-only resume must be successful and must not dispatch either job.
        cached_calls = []
        assert queue.execute(
            plan, result_dir,
            runner=lambda command, **_kwargs: cached_calls.append(command[-1]),
            poll_seconds=0.02,
        )["status"] == "COMPLETE"
        assert cached_calls == []
    finally:
        if owner is not None:
            _stop_process(owner)


def test_queue_waits_for_held_persistent_external_producer_lock_then_releases(
        tmp_path):
    artifact = tmp_path / "diagnostic.json"
    artifact.write_text("{}", encoding="utf-8")
    producer_lock = tmp_path / "producer.lock"
    producer_ready = tmp_path / "producer-ready"
    queue_ready = tmp_path / "queue-ready"
    verified = tmp_path / "verified"
    done = tmp_path / "done"
    queue_error = tmp_path / "queue-error"
    queue_plan = _wait_plan(producer_lock, artifact)
    plan_path = tmp_path / "wait-plan.json"
    plan_path.write_text(json.dumps(queue_plan), encoding="utf-8")
    holder = None
    queue_process = None
    try:
        holder = subprocess.Popen(
            [sys.executable, "-c", _PRODUCER_HOLDER, str(producer_lock), str(producer_ready)],
            cwd=Path.cwd(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            **_SUBPROCESS_OPTIONS,
        )
        _wait_for_marker(producer_ready, holder)
        queue_process = _start_wait_queue(
            plan_path, tmp_path / "results", queue_ready, verified, done, queue_error,
        )
        _wait_for_marker(queue_ready, queue_process)
        _wait_for_prerequisite_status(tmp_path / "results", queue_process)
        assert holder.poll() is None
        assert queue_process.poll() is None
        assert not verified.exists()
        assert not done.exists()

        _stop_process(holder)
        holder = None
        _wait_for_marker(done, queue_process)
        queue_process.wait(timeout=10)
        stdout, stderr = queue_process.communicate(timeout=10)
        assert queue_process.returncode == 0, f"stdout={stdout!r}; stderr={stderr!r}"
        assert verified.is_file()
        assert producer_lock.is_file()
    finally:
        if holder is not None:
            _stop_process(holder)
        if queue_process is not None:
            if queue_process.poll() is None:
                _stop_process(queue_process)
            else:
                queue_process.communicate(timeout=10)


def test_queue_waits_for_legacy_ephemeral_producer_marker_until_removed(tmp_path):
    artifact = tmp_path / "diagnostic.json"
    artifact.write_text("{}", encoding="utf-8")
    producer_lock = tmp_path / "legacy-producer.lock"
    producer_lock.write_text("running", encoding="utf-8")
    queue_ready = tmp_path / "queue-ready"
    verified = tmp_path / "verified"
    done = tmp_path / "done"
    queue_error = tmp_path / "queue-error"
    queue_plan = _wait_plan(producer_lock, artifact)
    plan_path = tmp_path / "wait-plan.json"
    plan_path.write_text(json.dumps(queue_plan), encoding="utf-8")
    queue_process = None
    try:
        queue_process = _start_wait_queue(
            plan_path, tmp_path / "results", queue_ready, verified, done, queue_error,
        )
        _wait_for_marker(queue_ready, queue_process)
        _wait_for_prerequisite_status(tmp_path / "results", queue_process)
        assert queue_process.poll() is None
        assert not verified.exists()
        producer_lock.unlink()
        _wait_for_marker(done, queue_process)
        queue_process.wait(timeout=10)
        stdout, stderr = queue_process.communicate(timeout=10)
        assert queue_process.returncode == 0, f"stdout={stdout!r}; stderr={stderr!r}"
        assert verified.is_file()
    finally:
        if queue_process is not None:
            if queue_process.poll() is None:
                _stop_process(queue_process)
            else:
                queue_process.communicate(timeout=10)
