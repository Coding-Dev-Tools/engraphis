"""Durable serial queue for local-only benchmark work lasting longer than a session.

The allowlist excludes every hosted reader and official paid evaluator. Jobs are
never dispatched concurrently or silently repeated after an interrupted attempt.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import os
import platform
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Callable, Optional

from eval.benchmark import canonical_json, sha256_file, validate_report


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "engraphis-local-benchmark-queue/v1"
MODULES = {"eval.external", "eval.agent_benchmarks", "eval.engine_capacity",
           "eval.local_capacity_campaign", "eval.benchmark_analysis"}
CAPACITY_SUMMARY_SCHEMA = "engraphis-local-capacity-summary/v1"
DEFAULT_JOB_TIMEOUT_SECONDS = 7 * 24 * 60 * 60.0
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RUNTIME_PACKAGES = (
    "engraphis", "numpy", "torch", "sentence-transformers", "transformers",
    "sqlite-vec", "psutil",
)


def snapshot() -> dict:
    files = [p for folder in ("engraphis/core", "engraphis/backends", "eval")
             for p in (ROOT / folder).rglob("*.py")]
    files += [ROOT / "engraphis/factory.py", ROOT / "pyproject.toml"]
    return {p.relative_to(ROOT).as_posix(): sha256_file(p) for p in sorted(files)}


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def runtime_identity(environment: Optional[dict] = None) -> dict:
    """Return non-secret execution identities that make a local attempt reproducible."""
    environment = os.environ if environment is None else environment
    packages = {}
    for package in _RUNTIME_PACKAGES:
        try:
            packages[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            packages[package] = None
    return {
        "python_executable": str(Path(sys.executable).resolve()),
        "python_version": platform.python_version(),
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "virtual_environment": environment.get("VIRTUAL_ENV"),
        "packages": packages,
        "thread_limits": {name: environment.get(name) for name in (
            "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
        )},
    }


def _new(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(canonical_json(value) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _status(directory: Path, **value) -> None:
    target = directory / "status.json"
    temporary = directory / "status.tmp"
    temporary.write_text(canonical_json({"pid": os.getpid(), "updated_unix": time.time(), **value}) + "\n",
                         encoding="utf-8")
    os.replace(temporary, target)


def _positive_finite(value: object, name: str) -> float:
    if type(value) not in {int, float} or not math.isfinite(float(value)) or float(value) <= 0:
        raise ValueError(f"{name} must be a finite positive number")
    return float(value)


def _artifact_path(name: str) -> Path:
    if not isinstance(name, str) or not name or Path(name).is_absolute():
        raise ValueError("queued artifact must be a repository-relative file")
    path = (ROOT / name).resolve()
    root = ROOT.resolve()
    if path == root or root not in path.parents:
        raise ValueError("queued artifact must stay inside the repository")
    return path


def _job_artifacts(job: dict) -> list[str]:
    """Resolve implicit capacity summary output while retaining explicit manifests."""
    artifacts = list(job.get("artifacts", []))
    if artifacts or job.get("module") != "eval.local_capacity_campaign":
        return artifacts
    args = job.get("args", [])
    if "--results" in args:
        position = args.index("--results") + 1
        if position < len(args):
            return [str(Path(args[position]) / "summary.json")]
    return artifacts


def validate(plan: dict, *, live: bool = True) -> None:
    if plan.get("schema") != SCHEMA or plan.get("binding_sha256") != digest(
            {key: value for key, value in plan.items() if key != "binding_sha256"}):
        raise ValueError("queue manifest checksum/schema mismatch")
    ids = set()
    if not isinstance(plan.get("jobs"), list):
        raise ValueError("queue requires a job list")
    for job in plan["jobs"]:
        if (not isinstance(job, dict) or job.get("module") not in MODULES
                or not isinstance(job.get("args"), list)
                or any(not isinstance(arg, str) for arg in job["args"])
                or not isinstance(job.get("id"), str)
                or not job["id"].replace("-", "").isalnum() or job["id"] in ids):
            raise ValueError("queue job is not an allowed distinct local benchmark")
        if "timeout_seconds" in job:
            _positive_finite(job["timeout_seconds"], "job timeout_seconds")
        if "artifacts" in job and (
                not isinstance(job["artifacts"], list)
                or any(not isinstance(item, str) for item in job["artifacts"])):
            raise ValueError("job artifacts must be repository-relative paths")
        for artifact in _job_artifacts(job):
            _artifact_path(artifact)
        ids.add(job["id"])
    if not ids:
        raise ValueError("queue requires jobs")
    inputs = plan.get("inputs")
    if not isinstance(inputs, dict):
        raise ValueError("queue requires a frozen input file mapping")
    for name, expected in inputs.items():
        path = (ROOT / name).resolve()
        if (not isinstance(name, str) or Path(name).is_absolute()
                or ROOT.resolve() not in path.parents):
            raise ValueError("queue input must be a repository-relative file")
        if live and (not path.is_file() or sha256_file(path) != expected):
            raise ValueError("queue input changed after freeze")
    if live and plan["source"] != snapshot():
        raise ValueError("queue source changed after freeze")


def _require_sha(value: object, name: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{name} must be a SHA-256 identity")
    return value


def _validate_external_analysis(report: dict) -> None:
    if not isinstance(report, dict) or report.get("schema") != "engraphis-external-analysis/v1":
        raise ValueError("queued analysis has the wrong schema")
    _require_sha(report.get("source_sha256"), "analysis source_sha256")
    source = ROOT / "eval" / "benchmark_analysis.py"
    if source.is_file() and report["source_sha256"] != sha256_file(source):
        raise ValueError("queued analysis source changed after it was produced")
    reports = report.get("reports")
    if not isinstance(reports, list) or not reports:
        raise ValueError("queued analysis must contain report summaries")
    for summary in reports:
        if not isinstance(summary, dict) or summary.get("schema") != "engraphis-external-analysis/v1":
            raise ValueError("queued analysis contains an invalid report summary")
        if summary.get("status") != "COMPLETE":
            raise ValueError("queued analysis contains an incomplete report")
        for field in ("input_sha256", "dataset_sha256"):
            _require_sha(summary.get(field), f"analysis {field}")
        for field in ("input_artifact", "dataset"):
            if not isinstance(summary.get(field), str) or not summary[field].strip():
                raise ValueError(f"analysis {field} is missing")
        for field in ("configuration", "models"):
            if not isinstance(summary.get(field), dict):
                raise ValueError(f"analysis {field} is missing")
        for field in ("questions", "retrieval_scored_questions"):
            if type(summary.get(field)) is not int or summary[field] < 0:
                raise ValueError(f"analysis {field} must be a nonnegative integer")
        if summary["retrieval_scored_questions"] > summary["questions"]:
            raise ValueError("analysis scored question count exceeds its denominator")


def _validate_capacity_summary(report: dict) -> None:
    if (not isinstance(report, dict)
            or report.get("summary_schema") != CAPACITY_SUMMARY_SCHEMA
            and report.get("schema") != CAPACITY_SUMMARY_SCHEMA):
        raise ValueError("queued capacity summary has the wrong schema")
    _require_sha(report.get("binding_sha256"), "capacity summary binding_sha256")
    if report.get("status") != "COMPLETE" or report.get("completed_cells") != 24:
        raise ValueError("queued capacity summary is incomplete")
    if report.get("declared_cells") != 24 or not isinstance(report.get("cells"), list):
        raise ValueError("queued capacity summary has an invalid cell matrix")
    hashes = report.get("cell_artifact_sha256")
    if not isinstance(hashes, dict) or len(hashes) != 24:
        raise ValueError("queued capacity summary lacks per-cell artifact hashes")
    if len(report["cells"]) != 24:
        raise ValueError("queued capacity summary has an incomplete cell list")
    for cell in report["cells"]:
        if (not isinstance(cell, dict) or not isinstance(cell.get("id"), str)
                or cell.get("status") != "COMPLETE"):
            raise ValueError("queued capacity summary contains an incomplete cell")
        _require_sha(cell.get("sha256"), "capacity cell sha256")
        if hashes.get(cell["id"]) != cell["sha256"]:
            raise ValueError("capacity cell hash index disagrees with its cell entry")
    statuses = report.get("gate_status", {})
    if not isinstance(statuses, dict):
        raise ValueError("queued capacity summary lacks separate gate statuses")
    for name in ("integrity", "resource", "latency", "backlog"):
        if statuses.get(name) not in {"PASS", "FAIL", "UNAVAILABLE", "NOT_APPLICABLE", "PARTIAL"}:
            raise ValueError(f"capacity {name} status is missing")


def _verified_artifact(path: Path) -> dict:
    sidecar = path.with_suffix(path.suffix + ".sha256")
    if not sidecar.is_file() or sidecar.read_text(encoding="utf-8").split()[0] != sha256_file(path):
        raise ValueError("queued artifact checksum missing or mismatched")
    report = json.loads(path.read_text(encoding="utf-8"))
    if report.get("schema") == "engraphis-external-analysis/v1":
        _validate_external_analysis(report)
        return report
    if (report.get("schema") == CAPACITY_SUMMARY_SCHEMA
            or report.get("summary_schema") == CAPACITY_SUMMARY_SCHEMA):
        _validate_capacity_summary(report)
        return report
    errors = validate_report(report)
    if errors:
        raise ValueError("queued artifact failed validation")
    if report.get("metrics", {}).get("checkpoint_status") not in (None, "COMPLETE"):
        raise ValueError("prerequisite diagnostic is incomplete")
    return report


class JobTimeoutError(TimeoutError):
    """The child was stopped at a known deadline; its started marker is retained."""

    def __init__(self, message: str, *, teardown: Optional[dict] = None):
        super().__init__(message)
        self.teardown = teardown


def _terminate_process_tree(process: subprocess.Popen, *, wait_seconds: float = 30.0) -> dict:
    """Kill a timed-out launcher and descendants, including Windows venv wrappers."""
    descendants = []
    psutil_available = False
    try:
        import psutil

        psutil_available = True
        descendants = psutil.Process(process.pid).children(recursive=True)
    except Exception:
        # The queue still kills the direct child when psutil is unavailable or
        # the launcher has already exited; the started marker remains durable.
        descendants = []
    killed = 0
    for child in reversed(descendants):
        try:
            child.kill()
            killed += 1
        except Exception:
            pass
    try:
        process.kill()
    except Exception:
        pass
    deadline = time.monotonic() + wait_seconds
    try:
        process.wait(timeout=max(0.1, deadline - time.monotonic()))
    except Exception:
        try:
            process.kill()
        except Exception:
            pass
    for child in descendants:
        try:
            child.wait(timeout=max(0.1, deadline - time.monotonic()))
        except Exception:
            # A second kill handles descendants that ignored the first signal.
            try:
                child.kill()
            except Exception:
                pass
    return {"psutil_available": psutil_available, "descendant_count": len(descendants),
            "descendants_killed": killed}


def _run_subprocess(command: list[str], *, cwd: Path, env: dict, log, timeout_seconds: float,
                    poll_seconds: float, heartbeat: Callable[[dict], None]) -> tuple[int, Optional[int], float]:
    started = time.monotonic()
    process = subprocess.Popen(command, cwd=cwd, env=env, stdout=log,
                               stderr=subprocess.STDOUT)
    heartbeat({"job_pid": process.pid, "elapsed_seconds": 0.0, "timeout_seconds": timeout_seconds})
    while True:
        returncode = process.poll()
        elapsed = time.monotonic() - started
        if returncode is not None:
            heartbeat({"job_pid": process.pid, "elapsed_seconds": elapsed,
                       "returncode": returncode, "timeout_seconds": timeout_seconds})
            return returncode, process.pid, elapsed
        if elapsed >= timeout_seconds:
            teardown = _terminate_process_tree(
                process, wait_seconds=min(30.0, max(1.0, poll_seconds)),
            )
            heartbeat({"job_pid": process.pid, "elapsed_seconds": elapsed,
                       "timeout_seconds": timeout_seconds, "timed_out": True,
                       "process_tree_teardown": teardown})
            raise JobTimeoutError(f"local benchmark job exceeded {timeout_seconds:g}s timeout",
                                  teardown=teardown)
        heartbeat({"job_pid": process.pid, "elapsed_seconds": elapsed,
                   "timeout_seconds": timeout_seconds})
        time.sleep(min(poll_seconds, max(0.001, timeout_seconds - elapsed)))


def execute(plan: dict, directory: Path, *, runner: Callable = subprocess.run,
            poll_seconds: float = 10, wait_timeout: float = 21600,
            stop_after_job: Optional[str] = None,
            default_timeout_seconds: float = DEFAULT_JOB_TIMEOUT_SECONDS) -> dict:
    validate(plan)
    poll_seconds = _positive_finite(poll_seconds, "poll_seconds")
    wait_timeout = _positive_finite(wait_timeout, "wait_timeout")
    default_timeout_seconds = _positive_finite(default_timeout_seconds, "default_timeout_seconds")
    job_ids = {job["id"] for job in plan["jobs"]}
    if stop_after_job is not None and stop_after_job not in job_ids:
        raise ValueError("stop-after-job must name a queued job")
    directory.mkdir(parents=True, exist_ok=True)
    lock = directory / ".runner.lock"
    with lock.open("x", encoding="utf-8") as handle:
        handle.write(str(os.getpid()))
    completed = []
    environment = {**os.environ, "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"}
    runtime = runtime_identity(environment)

    def heartbeat(**value):
        _status(directory, status="PARTIAL", phase="running", completed_jobs=completed,
                runtime=runtime, heartbeat_unix=time.time(), **value)

    try:
        wait = plan.get("wait_for")
        if wait:
            deadline = time.monotonic() + wait_timeout
            artifact = ROOT / wait["artifact"]
            producer_lock = ROOT / wait["producer_lock"]
            while not artifact.exists() or producer_lock.exists():
                if time.monotonic() >= deadline:
                    if artifact.exists():
                        raise ValueError("prerequisite producer has not released its timing lock")
                    raise ValueError("prerequisite producer stopped or exceeded its wait window")
                if not artifact.exists() and not producer_lock.exists():
                    raise ValueError("prerequisite producer stopped or exceeded its wait window")
                _status(directory, status="PARTIAL", phase="waiting_for_existing_diagnostic",
                        completed_jobs=completed, current_artifact=wait["artifact"], runtime=runtime,
                        heartbeat_unix=time.time())
                time.sleep(poll_seconds)
            # Verify only after the producer has released its lock and completed
            # the JSON plus checksum sidecar write.
            _verified_artifact(artifact)
        for job in plan["jobs"]:
            validate(plan)
            checkpoint = directory / f"{job['id']}.json"
            started = directory / f"{job['id']}.started"
            if checkpoint.exists():
                saved = json.loads(checkpoint.read_text(encoding="utf-8"))
                if saved.get("binding_sha256") != plan["binding_sha256"] or saved.get("job") != job:
                    raise ValueError("queue checkpoint does not bind this job")
                if saved.get("runtime") is not None and saved["runtime"] != runtime:
                    raise ValueError("queue checkpoint runtime identity changed")
                if saved.get("returncode") != 0:
                    raise ValueError("previous job failed; preserve its attempt instead of automatic replay")
                for artifact in _job_artifacts(job):
                    _verified_artifact(_artifact_path(artifact))
                    if saved.get("artifact_sha256", {}).get(artifact) != sha256_file(_artifact_path(artifact)):
                        raise ValueError("queued result changed after completion")
                completed.append(job["id"])
                if stop_after_job == job["id"]:
                    result = {"status": "PAUSED", "completed_jobs": completed,
                              "stopped_after_job": job["id"]}
                    _status(directory, **result, runtime=runtime)
                    return result
                continue
            if started.exists():
                raise ValueError("interrupted job requires explicit reconciliation; no automatic replay")
            timeout_seconds = _positive_finite(job.get("timeout_seconds", default_timeout_seconds),
                                               "job timeout_seconds")
            started_unix = time.time()
            _new(started, {"binding_sha256": plan["binding_sha256"], "job": job,
                           "started_unix": started_unix, "runtime": runtime,
                           "timeout_seconds": timeout_seconds})
            _status(directory, status="PARTIAL", phase="running", current_job=job["id"],
                    completed_jobs=completed, runtime=runtime, timeout_seconds=timeout_seconds,
                    heartbeat_unix=started_unix)
            with (directory / f"{job['id']}.log").open("x", encoding="utf-8") as log:
                command = [sys.executable, "-m", job["module"], *job["args"]]
                started_monotonic = time.monotonic()
                if runner is subprocess.run:
                    returncode, job_pid, elapsed = _run_subprocess(
                        command, cwd=ROOT, env=environment, log=log,
                        timeout_seconds=timeout_seconds, poll_seconds=poll_seconds,
                        heartbeat=lambda value: heartbeat(current_job=job["id"], **value),
                    )
                else:
                    heartbeat(current_job=job["id"], job_pid=None, elapsed_seconds=0.0,
                              timeout_seconds=timeout_seconds)
                    result = runner(command, cwd=ROOT, env=environment, stdout=log,
                                    stderr=subprocess.STDOUT, check=False)
                    returncode = result.returncode
                    job_pid = getattr(result, "pid", None)
                    elapsed = time.monotonic() - started_monotonic
                    heartbeat(current_job=job["id"], job_pid=job_pid, elapsed_seconds=elapsed,
                              returncode=returncode, timeout_seconds=timeout_seconds)
            if returncode != 0:
                _new(checkpoint, {"binding_sha256": plan["binding_sha256"], "job": job,
                                  "returncode": returncode, "finished_unix": time.time(),
                                  "runtime": runtime, "job_pid": job_pid,
                                  "elapsed_seconds": elapsed, "timeout_seconds": timeout_seconds})
                started.unlink()
                raise ValueError("local benchmark job returned a failure or incomplete result")
            # Revalidate the frozen source and inputs after the child exits and
            # before accepting its artifacts. A concurrent edit can otherwise
            # leave a successful checkpoint bound to different bytes than the
            # plan that was scored.
            validate(plan)
            for artifact in _job_artifacts(job):
                _verified_artifact(_artifact_path(artifact))
            _new(checkpoint, {"binding_sha256": plan["binding_sha256"], "job": job,
                              "returncode": 0, "finished_unix": time.time(), "runtime": runtime,
                              "job_pid": job_pid, "elapsed_seconds": elapsed,
                              "timeout_seconds": timeout_seconds,
                              "artifact_sha256": {p: sha256_file(_artifact_path(p))
                                                   for p in _job_artifacts(job)}})
            started.unlink()
            completed.append(job["id"])
            if stop_after_job == job["id"]:
                result = {"status": "PAUSED", "completed_jobs": completed,
                          "stopped_after_job": job["id"]}
                _status(directory, **result, runtime=runtime)
                return result
        result = {"status": "COMPLETE", "completed_jobs": completed, "runtime": runtime}
        _status(directory, **result)
        return result
    except BaseException as exc:
        details = {"process_tree_teardown": exc.teardown} if isinstance(exc, JobTimeoutError) else {}
        _status(directory, status="BLOCKED", phase="stopped", completed_jobs=completed,
                error_class=type(exc).__name__, reason=str(exc), runtime=runtime,
                heartbeat_unix=time.time(), **details)
        raise
    finally:
        lock.unlink(missing_ok=True)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--prepare-from", type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--results", type=Path)
    parser.add_argument("--stop-after-job")
    parser.add_argument("--job-timeout-seconds", type=float, default=DEFAULT_JOB_TIMEOUT_SECONDS)
    args = parser.parse_args(argv)
    if args.prepare_from:
        spec = json.loads(args.prepare_from.read_text(encoding="utf-8"))
        plan = {"schema": SCHEMA, "jobs": spec["jobs"], "wait_for": spec.get("wait_for"),
                "source": snapshot(),
                "inputs": {name: sha256_file(ROOT / name) for name in spec.get("input_files", [])}}
        plan["binding_sha256"] = digest(plan)
        validate(plan)
        _new(args.manifest, plan)
    plan = json.loads(args.manifest.read_text(encoding="utf-8"))
    validate(plan)
    if args.execute:
        if not args.results:
            raise ValueError("execution requires a private results directory")
        print(json.dumps(execute(plan, args.results, stop_after_job=args.stop_after_job,
                                 default_timeout_seconds=args.job_timeout_seconds), indent=2))
    else:
        print(json.dumps({"status": "PREPARED", "jobs": [job["id"] for job in plan["jobs"]]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
