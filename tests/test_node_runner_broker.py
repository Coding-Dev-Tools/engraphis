"""Regression checks for the persistent Node broker used by the dashboard graph tests.

They cover the failure modes that must match standalone ``node``: an unhandled
rejection with no listener fails the request and reports it, a registered listener
handles it without failing, ``process.exit`` stops the request with its code, and a
listener that itself throws fails the request instead of hiding the error.
"""
from __future__ import annotations

import gc
import json
import os
import shutil
import threading
import time
import warnings
from pathlib import Path

import pytest

from tests._node_runner import NodeExecutionError, _shared_worker, _shutdown, run_node

NODE = shutil.which("node")
requires_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


def _failed_response(script: str) -> dict:
    with pytest.raises(NodeExecutionError) as captured:
        run_node(script, timeout=10)
    response = captured.value.response
    assert response is not None, "the broker did not return a response"
    return response


@requires_node
def test_an_unhandled_rejection_without_a_listener_fails_the_request() -> None:
    """Node exits non-zero on an unhandled rejection; the broker must not hide it."""
    response = _failed_response(
        """
        setTimeout(() => {
          Promise.reject(new Error('unhandled-boom'));
        }, 1);
        """
    )
    assert response["code"] == 1
    assert "unhandled-boom" in response["stderr"]


@requires_node
def test_a_registered_unhandled_rejection_listener_answers_without_failing() -> None:
    response = run_node(
        """
        process.on('unhandledRejection', reason => {
          console.error('handled: ' + (reason && reason.message));
        });
        setTimeout(() => {
          Promise.reject(new Error('handled-boom'));
        }, 1);
        setTimeout(() => {
          console.log('tail');
        }, 20);
        """,
        timeout=10,
    )
    assert response["code"] == 0
    assert "error" not in response
    assert "handled: handled-boom" in response["stderr"]
    assert "tail" in response["stdout"]


@requires_node
def test_process_exit_stops_the_request_and_preserves_its_code() -> None:
    top_level = _failed_response(
        """
        console.log('before');
        process.exit(2);
        console.log('after');
        """
    )
    assert top_level["code"] == 2
    assert top_level["stdout"] == "before\n"

    callback = _failed_response(
        """
        setTimeout(() => {
          process.exit(2);
          console.log('after');
        }, 1);
        setTimeout(() => {
          console.log('later');
        }, 50);
        """
    )
    assert callback["code"] == 2
    assert callback["stdout"] == ""


@requires_node
def test_console_writes_are_captured_and_errors_use_the_request_filename() -> None:
    response = run_node(
        """
        console.warn('warned');
        console.error('failed');
        console.log('logged');
        """
    )
    assert response["code"] == 0
    assert response["stdout"] == "logged\n"
    assert response["stderr"] == "warned\nfailed\n"

    with pytest.raises(NodeExecutionError) as captured:
        run_node("throw new Error('frame-check');", filename="custom-request.js")
    assert "custom-request.js" in str(captured.value)


@requires_node
def test_a_throwing_unhandled_rejection_handler_fails_the_request() -> None:
    """A handler that throws must not turn a failed request into ``code: 0``."""
    response = _failed_response(
        """
        process.on('unhandledRejection', () => {
          throw new Error('handler-boom');
        });
        setTimeout(() => {
          Promise.reject(new Error('source-boom'));
        }, 1);
        """
    )
    assert response["code"] == 1
    assert "handler-boom" in response["stderr"]


@requires_node
def test_a_throwing_uncaught_exception_handler_exits_with_code_7() -> None:
    response = _failed_response(
        """
        process.on('uncaughtException', () => {
          throw new Error('uncaught-handler-boom');
        });
        setTimeout(() => {
          throw new Error('source-boom');
        }, 1);
        """
    )
    assert response["code"] == 7
    assert "uncaught-handler-boom" in response["stderr"]


@requires_node
def test_a_rejected_top_level_promise_cannot_succeed() -> None:
    """A rejected top-level script promise is a failure even with no listener."""
    response = _failed_response("Promise.reject(new Error('top-boom'));")
    assert response["code"] == 1
    assert "top-boom" in response["stderr"]


@requires_node
def test_a_mismatched_broker_response_fails_fast_and_the_broker_respawns() -> None:
    """A fatal ``{"id": null, ...}`` line must fail the call instead of being waited out."""
    try:
        _shared_worker()._responses.put(
            json.dumps({"id": None, "ok": False, "error": "fatal broker"}) + "\n"
        )
        started = time.monotonic()
        with pytest.raises(NodeExecutionError) as captured:
            run_node("console.log('never');", timeout=5.0)
        assert time.monotonic() - started < 4.5
        assert "Mismatched response id" in str(captured.value)
        assert captured.value.response is not None
        assert captured.value.response["id"] is None

        revived = run_node("console.log('revived');", timeout=10.0)
        assert revived["code"] == 0
        assert revived["stdout"] == "revived\n"
    finally:
        _shared_worker()._stop()


@requires_node
def test_a_broker_that_dies_mid_request_fails_fast_and_the_broker_respawns(
    tmp_path: Path,
) -> None:
    """A broker killed under a live request must not stall the caller for the timeout."""
    marker = tmp_path / "request-started"
    outcome: dict = {}
    try:
        script = (
            "require('fs').writeFileSync(%s, 'started');\n"
            "setTimeout(() => {}, 30000);\n" % json.dumps(str(marker))
        )

        def call() -> None:
            try:
                run_node(script, timeout=30.0)
            except Exception as error:
                outcome["error"] = error

        thread = threading.Thread(target=call, daemon=True)
        thread.start()
        deadline = time.monotonic() + 10.0
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert marker.exists(), "the broker never started the request"

        started = time.monotonic()
        proc = _shared_worker()._proc
        assert proc is not None, "the broker process was not started"
        proc.kill()
        thread.join(timeout=10.0)
        assert not thread.is_alive(), "run_node() kept waiting on a dead broker"
        assert time.monotonic() - started < 5.0
        error = outcome.get("error")
        assert isinstance(error, NodeExecutionError), repr(error)
        assert error.response is not None
        assert error.response["code"] != 0
        assert "exited unexpectedly" in str(error)

        revived = run_node("console.log('revived');", timeout=10.0)
        assert revived["code"] == 0
        assert revived["stdout"] == "revived\n"
    finally:
        _shared_worker()._stop()


@requires_node
def test_setting_process_exit_code_and_requiring_process_honor_the_request_shim() -> None:
    """Setting process.exitCode or calling require('process') must use the per-request shim."""
    with pytest.raises(NodeExecutionError) as exc_info:
        run_node(
            "require('process').stdout.write('before-exit-code\\n');\n"
            "process.exitCode = 3;\n",
            timeout=10.0,
        )
    assert exc_info.value.response is not None
    assert exc_info.value.response["code"] == 3
    assert exc_info.value.response["stdout"] == "before-exit-code\n"


@requires_node
def test_bare_process_exit_preserves_exit_code_and_three_arg_write_calls_callback() -> None:
    """`process.exit()` preserves preset `exitCode`, and 3-arg stream writes call `cb`."""
    try:
        with pytest.raises(NodeExecutionError) as exc_info:
            run_node(
                "process.stdout.write('part1-', 'utf8', () => process.stdout.write('cb'));\n"
                "process.exitCode = 5;\n"
                "process.exit();\n",
                timeout=10.0,
            )
        assert exc_info.value.response is not None
        assert exc_info.value.response["code"] == 5
        assert exc_info.value.response["stdout"] == "part1-cb"
    finally:
        _shutdown()


@requires_node
def test_worker_isolates_globals_env_and_recovers_from_thread_timeout_and_bad_message() -> None:
    """Requests cannot leak globals/env, and worker timeouts leave the broker healthy."""
    try:
        r1 = run_node(
            "globalThis.leakedVar = 42;\n"
            "Object.prototype.pollutedProp = 99;\n"
            "Math.random = () => 0.123;\n"
            "console.log(process.env.BROKER_TEST_ENV);\n",
            env={"BROKER_TEST_ENV": "only-request-1"},
            timeout=10.0,
        )
        assert r1["stdout"] == "only-request-1\n"

        r2 = run_node(
            "console.log(JSON.stringify({"
            "leaked: globalThis.leakedVar === undefined,"
            "protoOk: ({}).pollutedProp === undefined,"
            "randOk: Math.random() !== 0.123,"
            "envOk: process.env.BROKER_TEST_ENV === undefined"
            "}));",
            timeout=10.0,
        )
        assert json.loads(r2["stdout"]) == {
            "leaked": True,
            "protoOk": True,
            "randOk": True,
            "envOk": True,
        }

        with pytest.raises(NodeExecutionError) as timeout_exc:
            run_node("while (true) {}", timeout=0.4)
        assert "timed out" in str(timeout_exc.value)
        assert _shared_worker().alive(), "worker thread timeout should keep broker alive"

        with pytest.raises(NodeExecutionError) as bad_msg:
            run_node("require('worker_threads').parentPort.postMessage(null);", timeout=10.0)
        assert "Malformed" in str(bad_msg.value)

        recovered = run_node("console.log('healthy');", timeout=10.0)
        assert recovered["stdout"] == "healthy\n"
    finally:
        _shutdown()


@requires_node
def test_source_cache_invalidates_when_disk_asset_changes(tmp_path: Path) -> None:
    """Updating an argument file between calls must not serve stale cached bytes."""
    asset = tmp_path / "asset.js"
    asset.write_text("v1", encoding="utf-8")
    try:
        r1 = run_node(
            "console.log(require('fs').readFileSync(process.argv[1], 'utf8'));",
            args=[str(asset)],
            timeout=10.0,
        )
        assert r1["stdout"] == "v1\n"

        asset.write_text("version-two-updated", encoding="utf-8")
        stat = asset.stat()
        os.utime(asset, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000))
        r2 = run_node(
            "console.log(require('fs').readFileSync(process.argv[1], 'utf8'));",
            args=[str(asset)],
            timeout=10.0,
        )
        assert r2["stdout"] == "version-two-updated\n"
    finally:
        _shutdown()


@requires_node
def test_unexpected_exit_preserves_stderr_and_stop_closes_pipes_cleanly(
    tmp_path: Path,
) -> None:
    """Abrupt broker crashes retain stderr diagnostics and _stop/_shutdown leak no pipes."""
    marker = tmp_path / "started"
    outcome: dict = {}
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", ResourceWarning)
            worker = _shared_worker()
            run_node("console.log('warm');", timeout=10.0)
            worker._stop()
            gc.collect()
            run_node("console.log('respawned');", timeout=10.0)
            _shutdown()
            gc.collect()

        # Verify stderr collected prior to an unexpected mid-request death surfaces.
        worker = _shared_worker()
        script = (
            "require('fs').writeFileSync(%s, 'go');\n"
            "setTimeout(() => {}, 30000);\n" % json.dumps(str(marker))
        )

        def call() -> None:
            try:
                run_node(script, timeout=10.0)
            except Exception as exc:
                outcome["error"] = exc

        thread = threading.Thread(target=call, daemon=True)
        thread.start()
        deadline = time.monotonic() + 10.0
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert marker.exists(), "broker never started request"
        with worker._stderr_lock:
            worker._stderr.append("fatal-broker-diagnostic\n")
        proc = worker._proc
        assert proc is not None
        proc.kill()
        thread.join(timeout=10.0)
        err = outcome.get("error")
        assert isinstance(err, NodeExecutionError)
        assert err.response is not None
        assert "fatal-broker-diagnostic" in str(err)
        assert "fatal-broker-diagnostic" in err.response["stderr"]
    finally:
        _shutdown()

