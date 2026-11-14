"""Persistent Node executor shared by the offline dashboard graph tests.

One long-lived ``node`` broker process serves every call: it reads asset sources
from disk once instead of once per test, then runs each request on a fresh
``worker_threads`` worker (its own isolate and ``globalThis``) that compiles the
request with ``vm.Script``.  ``window``/``globalThis``/timers/mock DOM state stay
private to that call, and the broker is per Python process, so ``pytest-xdist``
workers never share one.
"""
from __future__ import annotations

import atexit
import collections
import json
import os
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
WORKER = Path(__file__).with_name("_node_worker.js")
NODE = shutil.which("node")

_DEFAULT_TIMEOUT_SECONDS = 300.0
_SHUTDOWN_LOCK_TIMEOUT_SECONDS = 2.0


class NodeExecutionError(AssertionError):
    """A worker request failed; mirrors a non-zero ``node`` exit."""

    def __init__(self, message: str, response: Optional[dict] = None) -> None:
        super().__init__(message)
        self.response = response


class _Worker:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._proc: Optional[subprocess.Popen] = None
        self._responses: queue.Queue = queue.Queue()
        self._stderr: collections.deque = collections.deque(maxlen=200)
        self._stderr_lock = threading.Lock()
        self._seq = 0
        self._start()

    def _start(self) -> None:
        if NODE is None:
            raise NodeExecutionError("node is not installed")
        responses: queue.Queue = queue.Queue()
        stderr: collections.deque = collections.deque(maxlen=200)
        stderr_lock = self._stderr_lock
        creationflags = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        )
        proc = subprocess.Popen(
            [NODE, str(WORKER)],
            cwd=str(ROOT),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creationflags,
        )
        self._proc = proc
        self._responses = responses
        self._stderr = stderr

        def capture_stderr(line: str) -> None:
            with stderr_lock:
                stderr.append(line)

        threading.Thread(
            target=self._pump, args=(proc.stdout, responses.put), daemon=True,
            name="engraphis-node-stdout",
        ).start()
        threading.Thread(
            target=self._pump, args=(proc.stderr, capture_stderr), daemon=True,
            name="engraphis-node-stderr",
        ).start()

    @staticmethod
    def _pump(stream, sink) -> None:
        try:
            for line in stream:
                sink(line)
        except (ValueError, OSError):
            pass

    def _stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
                proc.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                pass

    def _terminate(self) -> None:
        """Kill the broker without waiting for an in-flight request."""
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass

    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def run(self, payload: dict, *, timeout: float) -> dict:
        with self._lock:
            self._seq += 1
            payload = dict(payload, id=self._seq)
            for attempt in range(2):
                if not self.alive():
                    self._stop()
                    self._start()
                try:
                    proc = self._proc
                    if proc is None or proc.stdin is None:
                        raise RuntimeError("Node test broker process is not running")
                    proc.stdin.write(json.dumps(payload) + "\n")
                    proc.stdin.flush()
                    break
                except (BrokenPipeError, OSError, ValueError):
                    if attempt:
                        raise NodeExecutionError("node worker is unavailable") from None
                    self._stop()
            req_id = payload["id"]
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._stop()
                    raise subprocess.TimeoutExpired(
                        cmd=["node", str(WORKER)], timeout=timeout
                    )
                try:
                    raw_line = self._responses.get(timeout=min(0.25, remaining))
                    break
                except queue.Empty:
                    # A broker that died mid-request would otherwise burn the whole
                    # timeout waiting on a line that can never arrive.
                    if proc.poll() is not None and self._responses.empty():
                        rc = proc.returncode
                        self._stop()
                        raise NodeExecutionError(
                            "node broker exited unexpectedly with code %s" % rc,
                            {"id": req_id, "ok": False, "code": rc,
                             "stdout": "", "stderr": ""},
                        ) from None
            try:
                response = json.loads(raw_line)
            except json.JSONDecodeError:
                self._stop()
                raise NodeExecutionError(
                    "Invalid JSON from node broker: %s" % raw_line[:200],
                    {"id": req_id, "ok": False, "code": 1,
                     "stdout": "", "stderr": raw_line},
                ) from None
            if response.get("id") != req_id:
                self._stop()
                raise NodeExecutionError(
                    "Mismatched response id from node broker (expected %s, got %s): %s"
                    % (req_id, response.get("id"), response.get("stderr", "")),
                    response,
                )
            if response.get("error"):
                detail = str(response["error"])
                with self._stderr_lock:
                    stderr_lines = list(self._stderr)
                stderr = response.get("stderr") or "".join(stderr_lines)
                # A top-level failure is reported in both fields; do not repeat it.
                if stderr and stderr.strip() not in detail:
                    detail += "\n" + stderr
                raise NodeExecutionError(detail, response)
            return response


_worker: Optional[_Worker] = None
_worker_lock = threading.Lock()


def _shared_worker() -> _Worker:
    global _worker
    with _worker_lock:
        if _worker is None:
            _worker = _Worker()
        return _worker


def _shutdown() -> None:
    global _worker
    with _worker_lock:
        worker, _worker = _worker, None
    if worker is None:
        return
    # A worker stuck in a long request holds its lock; an interpreter exit must not
    # wait indefinitely for it. Kill the broker instead of blocking shutdown.
    if not worker._lock.acquire(timeout=_SHUTDOWN_LOCK_TIMEOUT_SECONDS):
        worker._terminate()
        return
    try:
        worker._stop()
    finally:
        worker._lock.release()


atexit.register(_shutdown)


def run_node(
    script: str,
    *,
    prelude: str = "",
    args: Optional[list] = None,
    env: Optional[dict] = None,
    files: Optional[dict] = None,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    filename: str = "engraphis-test-request.js",
) -> dict:
    """Execute *prelude + script* in a fresh Node worker context and return its output.

    The persistent broker inherited ``os.environ`` when it was spawned, so mutating
    ``os.environ`` in a test does not reach later Node calls. Pass the value
    explicitly in ``env`` (it is layered over the broker's environment) or call
    ``_shutdown()`` so the next call respawns the broker with the current one.
    """
    payload: dict[str, Any] = {
        "script": script,
        "prelude": prelude,
        "args": list(args or ()),
        "env": dict(env or {}),
        "files": dict(files or {}),
        "timeoutMs": int(timeout * 1000),
        "filename": filename,
    }
    return _shared_worker().run(payload, timeout=timeout)


def run_json(script: str, **kwargs: Any) -> Any:
    """Run a request and parse the last line it logged, like the old ``node -e`` path."""
    response = run_node(script, **kwargs)
    lines = str(response.get("stdout") or "").strip().splitlines()
    if not lines:
        detail = str(response.get("stderr") or "")
        raise NodeExecutionError("node produced no JSON output" +
                                 ("\n" + detail if detail else ""))
    return json.loads(lines[-1])
