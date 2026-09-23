"""Offline subprocess coverage for campaign execution lock ownership."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from eval import benchmark_campaign as campaign
from eval import campaign_continuation as continuation
from eval.external_checkpoints import RUNNER_LOCK_MARKER, RunnerLockBusy


ROOT = Path(campaign.__file__).resolve().parents[1]
CHILD_WAIT_SECONDS = 120
READY_WAIT_SECONDS = 15.0


def _manifest() -> dict:
    value = {
        "stages": {"development_pilot": {
            "split": "development",
            "scenario_ids": ["fixture-a"],
            "arms": ["no_memory"],
            "repetitions": 1,
            "token_budgets": [512],
            "max_reader_turns": 2,
            "max_peer_internal_calls_per_attempt": 32,
            "max_input_tokens": 32768,
            "max_output_tokens": 4096,
        }},
    }
    value["binding_sha256"] = campaign.digest(value)
    return value


def _row(cell: dict) -> dict:
    return {
        **cell,
        "family_id": "family-a",
        "category": "corrections",
        "status": "complete",
        "task_success": True,
        "critical_violations": [],
        "private_responses": [],
    }


def _wait_for_ready(process: subprocess.Popen, ready: Path) -> None:
    deadline = time.monotonic() + READY_WAIT_SECONDS
    while time.monotonic() < deadline:
        if ready.exists():
            return
        if process.poll() is not None:
            stdout, stderr = process.communicate(timeout=5)
            raise AssertionError(
                f"lock child exited before ready: {process.returncode}; {stdout}; {stderr}"
            )
        time.sleep(0.05)
    raise AssertionError(f"lock child did not become ready: {ready}")


def _stop(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.kill()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.terminate()
        process.wait(timeout=15)
    try:
        process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate(timeout=5)


def _spawn_child(role: str, directory: Path, ready: Path) -> subprocess.Popen:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    return subprocess.Popen(
        [sys.executable, "-c",
         "import runpy, sys; script = sys.argv.pop(1); runpy.run_path(script, run_name='__main__')",
         str(Path(__file__).resolve()), "child", role, str(directory), str(ready)],
        cwd=str(ROOT),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=flags,
    )


def _hold_campaign(directory: Path, ready: Path) -> None:
    manifest = _manifest()

    def runner(_manifest, _stage, cell, _corpus, _client):
        ready.write_text("ready", encoding="ascii")
        time.sleep(CHILD_WAIT_SECONDS)
        return _row(cell)

    campaign.execute(manifest, "development_pilot", directory, None, None,
                     attempt_runner=runner)


def _hold_continuation(directory: Path, ready: Path) -> None:
    with continuation._execution_lock(directory, "continuation"):
        ready.write_text("ready", encoding="ascii")
        time.sleep(CHILD_WAIT_SECONDS)


def _child_main() -> None:
    if len(sys.argv) != 5 or sys.argv[1] != "child":
        raise SystemExit("child role, lock kind, directory, and ready path are required")
    directory, ready = Path(sys.argv[3]), Path(sys.argv[4])
    if sys.argv[2] == "campaign":
        _hold_campaign(directory, ready)
    elif sys.argv[2] == "continuation":
        _hold_continuation(directory, ready)
    else:
        raise SystemExit(f"unknown lock kind: {sys.argv[2]}")


def test_campaign_execution_lock_releases_after_death_without_replay(tmp_path):
    manifest = _manifest()
    directory = tmp_path / "campaign-results"
    ready = tmp_path / "campaign-ready"
    process = _spawn_child("campaign", directory, ready)
    marker = directory / ".campaign-execution.lock"
    try:
        _wait_for_ready(process, ready)
        assert marker.exists()
        called = []
        with pytest.raises(ValueError, match="campaign runner|lock"):
            campaign.execute(
                manifest, "development_pilot", directory, None, None,
                attempt_runner=lambda *args: called.append(args),
            )
        assert called == []
        with pytest.raises(continuation.ContinuationError, match="execution lock"):
            with continuation._execution_lock(directory, "continuation"):
                pytest.fail("cross-wrapper lock acquisition unexpectedly succeeded")

        _stop(process)
        assert marker.read_bytes() == RUNNER_LOCK_MARKER
        started = list((directory / "development_pilot").glob("*.started"))
        assert len(started) == 1
        started_bytes = started[0].read_bytes()

        replayed = []
        with pytest.raises(ValueError, match="unfinished attempt"):
            campaign.execute(
                manifest, "development_pilot", directory, None, None,
                attempt_runner=lambda *args: replayed.append(args),
            )
        assert replayed == []
        assert started[0].read_bytes() == started_bytes

        # This is explicit reconciliation in a disposable test directory.
        reconciled = started[0].with_name(started[0].name + ".reconciled")
        started[0].replace(reconciled)
        executed = []
        summary = campaign.execute(
            manifest, "development_pilot", directory, None, None,
            attempt_runner=lambda _m, _s, cell, *_args: (executed.append(cell), _row(cell))[1],
        )
        assert summary["status"] == "COMPLETE"
        assert len(executed) == 1
        assert reconciled.read_bytes() == started_bytes

        cached_calls = []
        cached = campaign.execute(
            manifest, "development_pilot", directory, None, None,
            attempt_runner=lambda *args: cached_calls.append(args),
        )
        assert cached["status"] == "COMPLETE"
        assert cached_calls == []
        assert reconciled.read_bytes() == started_bytes
    finally:
        _stop(process)


def test_continuation_lock_releases_after_death_and_preserves_marker(tmp_path):
    directory = tmp_path / "continuation-results"
    ready = tmp_path / "continuation-ready"
    process = _spawn_child("continuation", directory, ready)
    marker = directory / ".campaign-execution.lock"
    try:
        _wait_for_ready(process, ready)
        assert marker.exists()
        with pytest.raises(continuation.ContinuationError, match="execution lock"):
            with continuation._execution_lock(directory, "continuation"):
                pytest.fail("live continuation lock was not excluded")
        with pytest.raises(ValueError, match="campaign runner|lock"):
            campaign.execute(
                _manifest(), "development_pilot", directory, None, None,
                attempt_runner=lambda *args: pytest.fail("live continuation permitted campaign dispatch"),
            )
        _stop(process)
        assert marker.read_bytes() == RUNNER_LOCK_MARKER
        with continuation._execution_lock(directory, "continuation"):
            pass
        assert marker.read_bytes() == RUNNER_LOCK_MARKER
    finally:
        _stop(process)


@pytest.mark.parametrize("marker_contents", [b"legacy-pid\n", b""])
def test_campaign_execution_lock_rejects_unrecognized_markers(tmp_path, marker_contents):
    manifest = _manifest()
    directory = tmp_path / "campaign-results"
    directory.mkdir()
    (directory / ".campaign-execution.lock").write_bytes(marker_contents)
    called = []
    with pytest.raises(ValueError, match="lock"):
        campaign.execute(
            manifest, "development_pilot", directory, None, None,
            attempt_runner=lambda *args: called.append(args),
        )
    assert called == []


@pytest.mark.parametrize("marker_contents", [b"legacy-pid\n", b""])
def test_continuation_lock_rejects_unrecognized_markers(tmp_path, marker_contents):
    directory = tmp_path / "continuation-results"
    directory.mkdir()
    (directory / ".campaign-execution.lock").write_bytes(marker_contents)
    with pytest.raises(continuation.ContinuationError, match="lock"):
        with continuation._execution_lock(directory, "continuation"):
            pytest.fail("unrecognized marker was reclaimed")


@pytest.mark.parametrize("role", ["campaign", "continuation"])
@pytest.mark.parametrize("error_type", [RuntimeError, ValueError, RunnerLockBusy])
def test_campaign_locks_preserve_body_error_type(tmp_path, error_type, role):
    directory = tmp_path / "results"
    lock = (campaign._campaign_execution_lock(directory / ".campaign-execution.lock")
            if role == "campaign" else continuation._execution_lock(directory, "continuation"))
    with pytest.raises(error_type, match="body-sentinel") as caught:
        with lock:
            raise error_type("body-sentinel")
    assert type(caught.value) is error_type


def test_campaign_source_snapshot_binds_shared_lock_helper():
    assert "eval/external_checkpoints.py" in campaign.source_snapshot()


if __name__ == "__main__":
    _child_main()
