"""Resumable, serial capacity measurements for one observed reference host.

This is a deliberately separate contract from the two-host acceptance matrix.
Completing these 24 cells never grants the 48-cell capacity or publication gate.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import importlib.metadata
import json
import multiprocessing
import os
import platform
from pathlib import Path
import re
import sys
import time
import traceback
from typing import Callable, Optional

from eval.benchmark import canonical_json, read_artifact_snapshot, sha256_file, validate_report, write_canonical_artifact
from eval.capacity_matrix import _cell_identity, _validate_repeat
from eval.engine_capacity import (
    Cell, HARDWARE, _snapshot as _engine_snapshot, acceptance_policy, host_observation, operation_plan, protocol, run_cell,
)
from eval.external_checkpoints import _runner_lock
from eval.rework_statistics import blocked_mean_interval


SCHEMA = "engraphis-local-capacity-campaign/v1"
SUMMARY_SCHEMA = "engraphis-local-capacity-summary/v1"
SUMMARY_FILENAME = "summary.json"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RUNTIME_PACKAGES = (
    "engraphis", "numpy", "torch", "sentence-transformers", "transformers",
    "sqlite-vec", "psutil",
)


def _snapshot() -> dict:
    """Bind the shared lock implementation alongside the measured engine."""
    return {**_engine_snapshot(),
            "eval/external_checkpoints.py": sha256_file(Path(__file__).with_name("external_checkpoints.py"))}


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _runtime_identity() -> dict:
    """Capture reproducibility metadata without exposing host names or secrets."""
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
        "packages": packages,
    }


def _merge_status(values: list[str]) -> str:
    """Combine cell/repetition gates without turning missing observations into PASS."""
    values = [value for value in values if value not in {"PENDING", "NOT_APPLICABLE"}]
    if not values:
        return "PENDING"
    if "FAIL" in values:
        return "FAIL"
    if "UNAVAILABLE" in values:
        return "UNAVAILABLE"
    if "PARTIAL" in values:
        return "PARTIAL"
    if all(value == "PASS" for value in values):
        return "PASS"
    return "PARTIAL"


def _write_summary(directory: Path, summary: dict) -> Path:
    """Write a canonical, checksummed summary atomically after each observation."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / SUMMARY_FILENAME
    payload = (canonical_json(summary) + "\n").encode("utf-8")
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    checksum = hashlib.sha256(payload).hexdigest()
    sidecar = path.with_suffix(path.suffix + ".sha256")
    side_tmp = sidecar.with_name(sidecar.name + ".tmp")
    side_tmp.write_text(f"{checksum}  {path.name}\n", encoding="utf-8")
    os.replace(side_tmp, sidecar)
    return path


def _cell_watchdog_timeout(cell: Cell) -> float:
    """Bound the whole cell while allowing every declared repetition its engine deadline."""
    return float(cell.timeout_s) * int(cell.repeats) + max(300.0, 60.0 * int(cell.repeats))


def _cell_worker(output: str, error: str, config: dict, model_dir: str, model_sha256: str) -> None:
    """Spawn target for the real runner so a seed hang cannot strand the campaign parent."""
    try:
        report = run_cell(Cell(**config), model_dir=model_dir, model_sha256=model_sha256)
        write_canonical_artifact(report, Path(output))
    except BaseException as exc:
        Path(error).write_text(canonical_json({
            "error_class": type(exc).__name__, "reason": str(exc),
            "traceback": [
                {"file": Path(frame.filename).name, "line": int(frame.lineno),
                 "function": str(frame.name)}
                for frame in traceback.extract_tb(exc.__traceback__)[-24:]
            ],
        }) + "\n", encoding="utf-8")
        raise


def _terminate_cell_process(process: multiprocessing.Process) -> None:
    """Stop a timed-out cell and its disposable descendants when psutil is available."""
    descendants = []
    try:
        import psutil

        descendants = psutil.Process(process.pid).children(recursive=True)
    except Exception:
        descendants = []
    for child in reversed(descendants):
        try:
            child.terminate()
        except Exception:
            pass
    try:
        process.terminate()
    except (OSError, ValueError):
        pass
    process.join(timeout=5)
    for child in descendants:
        try:
            if child.is_running():
                child.kill()
        except Exception:
            pass
    if process.is_alive():
        process.kill()
        process.join(timeout=5)


def _run_cell_with_watchdog(cell: Cell, *, model_dir: str, model_sha256: str, output: Path) -> dict:
    timeout_seconds = _cell_watchdog_timeout(cell)
    error = output.with_suffix(".error.json")
    context = multiprocessing.get_context("spawn")
    process = context.Process(target=_cell_worker, args=(str(output), str(error),
                                                          asdict(cell), model_dir, model_sha256))
    process.start()
    deadline = time.monotonic() + timeout_seconds
    while process.is_alive():
        process.join(timeout=min(1.0, max(0.01, deadline - time.monotonic())))
        if time.monotonic() >= deadline and process.is_alive():
            _terminate_cell_process(process)
            raise TimeoutError(f"capacity cell exceeded whole-cell watchdog {timeout_seconds:g}s")
    process.join()
    if process.exitcode != 0:
        details = {}
        if error.is_file():
            try:
                details = json.loads(error.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                details = {}
        reason = details.get("reason") or "worker exited before producing a complete artifact"
        trace = details.get("traceback")
        suffix = f"; traceback={trace}" if isinstance(trace, list) and trace else ""
        raise ValueError(f"capacity cell worker failed: {reason}{suffix}")
    report = _read_verified(output)
    error.unlink(missing_ok=True)
    return report


def cell_id(config: dict) -> str:
    return "{hardware}-{size}-{backend}-{concurrency}-{workload}".format(**config)


def make_plan(*, hardware: str, model_dir: str, model_sha256: str) -> dict:
    """Bind this host and producer bytes before any measured operation."""
    from eval.engine_capacity import _local_model

    if hardware not in HARDWARE:
        raise ValueError("unknown reference hardware profile")
    observed = host_observation()
    ram = observed["hardware"].get("physical_ram_bytes")
    if ram is None or abs(ram / (HARDWARE[hardware] * 1024 ** 3) - 1) > .125:
        raise ValueError("observed RAM does not match selected reference profile")
    model = _local_model(model_dir, model_sha256)
    if not model["semantic"]:
        raise ValueError("local capacity campaign requires a pinned semantic model")
    cells = []
    for entry in protocol()["primary_cells"]:
        if entry["hardware"] != hardware:
            continue
        cell = Cell(**entry, repeats=5, operations=2000, dimension=384,
                    arrival_rate=entry["concurrency"], smoke=False)
        cell.validate()
        cells.append(asdict(cell))
    # Stage the smaller corpus first and expose concurrency issues before the
    # slow single-process arrival cells. Membership and sampling are unchanged.
    cells.sort(key=lambda item: (item["size"], -item["concurrency"], item["backend"], item["workload"]))
    result = {
        "schema": SCHEMA, "hardware_profile": hardware,
        "host": observed, "source": _snapshot(), "orchestrator_sha256": sha256_file(Path(__file__)),
        "statistics_sha256": sha256_file(Path(__file__).with_name("rework_statistics.py")),
        "model_dir": str(Path(model_dir).resolve()), "model_sha256": model_sha256,
        "model": model, "cells": cells, "policy": acceptance_policy(),
        "runtime": _runtime_identity(),
        "scheduled_operations": 240000, "repetitions": 120,
        "scheduled_arrival_hours": sum(5 * 2000 / c["arrival_rate"] for c in cells) / 3600,
        "primary_matrix_complete": False, "publication_ready": False,
    }
    result["binding_sha256"] = _digest(result)
    return result


def validate_plan(plan: dict, *, live: bool = True) -> None:
    if not isinstance(plan, dict) or plan.get("schema") != SCHEMA:
        raise ValueError("invalid local capacity manifest")
    binding = {key: value for key, value in plan.items() if key != "binding_sha256"}
    if plan.get("binding_sha256") != _digest(binding):
        raise ValueError("capacity manifest was changed after freezing")
    hardware = plan.get("hardware_profile")
    if hardware not in HARDWARE:
        raise ValueError("unknown hardware profile")
    expected = {cell_id(c) for c in protocol()["primary_cells"] if c["hardware"] == hardware}
    cells = plan.get("cells")
    if not isinstance(cells, list) or len(cells) != 24:
        raise ValueError("local campaign requires exactly 24 cells")
    found = set()
    for config in cells:
        cell = Cell(**config)
        cell.validate()
        key = cell_id(config)
        if (cell.smoke or cell.operations != 2000 or cell.repeats != 5
                or key not in expected or key in found):
            raise ValueError("invalid, duplicate or non-primary local cell")
        found.add(key)
    if (plan.get("scheduled_operations") != 240000 or plan.get("repetitions") != 120
            or plan.get("policy") != acceptance_policy()
            or plan.get("primary_matrix_complete") is not False
            or plan.get("publication_ready") is not False):
        raise ValueError("local campaign cannot change sampling or grant primary acceptance")
    if live:
        from eval.engine_capacity import _local_model

        if plan.get("orchestrator_sha256") != sha256_file(Path(__file__)) or plan["source"] != _snapshot():
            raise ValueError("capacity producer source changed; create a new campaign")
        if plan.get("statistics_sha256") != sha256_file(Path(__file__).with_name("rework_statistics.py")):
            raise ValueError("capacity statistics source changed")
        if plan["host"] != host_observation():
            raise ValueError("capacity host identity or environment changed")
        if plan["model"] != _local_model(plan["model_dir"], plan["model_sha256"]):
            raise ValueError("capacity model bytes changed")
        if plan.get("runtime") is not None and plan["runtime"] != _runtime_identity():
            raise ValueError("capacity runtime identity changed")


def _read_verified(path: Path) -> dict:
    return _read_verified_snapshot(path)[0]


def _read_verified_snapshot(path: Path) -> tuple[dict, str]:
    report, digest = read_artifact_snapshot(path)
    errors = validate_report(report)
    if errors:
        raise ValueError("invalid capacity artifact: " + errors[0])
    return report, digest


def _repetition_gate_statuses(summaries: list[dict], cell: Cell, hardware: dict) -> dict:
    """Classify independent gates while retaining unavailable evidence explicitly."""
    integrity = "PASS" if all(
        summary.get("status") == "complete" and summary.get("measured") == 2000
        and summary.get("clean_worker_teardown") and summary.get("durable_workers_observed")
        and summary.get("failures") == 0 for summary in summaries
    ) else "FAIL"

    ram = hardware.get("hardware", {}).get("physical_ram_bytes") if isinstance(hardware, dict) else None
    resource_values = []
    for summary in summaries:
        peak = summary.get("memory_peak")
        if (not summary.get("all_lifecycle_phases_observed") or type(peak) not in {int, float}
                or not isinstance(ram, (int, float)) or ram <= 0):
            resource_values.append("UNAVAILABLE")
        elif peak >= ram * acceptance_policy()["rss_fraction_of_physical_ram_exclusive"]:
            resource_values.append("FAIL")
        else:
            resource_values.append("PASS")
    resource = _merge_status(resource_values)

    limits = [limit for limit in acceptance_policy()["recall_p95_ms"]
              if all(getattr(cell, axis) == limit[axis] for axis in ("hardware", "size", "concurrency"))]
    if not limits:
        latency = "NOT_APPLICABLE"
    else:
        latency_values = []
        limit = limits[0]["maximum"]
        for summary in summaries:
            observation = summary.get("operations", {}).get("recall", {}).get("wall_latency_ms")
            if not isinstance(observation, dict) or not isinstance(observation.get("p95"), (int, float)):
                latency_values.append("UNAVAILABLE")
            elif observation["p95"] <= limit:
                latency_values.append("PASS")
            else:
                latency_values.append("FAIL")
        latency = _merge_status(latency_values)

    backlog_values = []
    for summary in summaries:
        available = summary.get("backlog_assessment_available")
        no_growth = summary.get("no_sustained_backlog_growth_observed")
        if available is not True:
            backlog_values.append("UNAVAILABLE")
        elif no_growth is True:
            backlog_values.append("PASS")
        else:
            backlog_values.append("FAIL")
    return {"integrity": integrity, "resource": resource,
            "latency": latency, "backlog": _merge_status(backlog_values)}


def _repetition_statistics(report: dict, cell: Cell, identities: set) -> dict:
    """Reuse the full protocol's measurement validation without granting its 48-cell gate."""
    identity, _hardware = _cell_identity(report, cell, fixture=False)
    if not identity["observation_contract"]:
        raise ValueError("capacity requires the current resource observation contract")
    repeats = report["metrics"].get("repeats", [])
    if (len(repeats) != 5 or any(type(r.get("repeat_number")) is not int for r in repeats)
            or {r["repeat_number"] for r in repeats} != set(range(5))):
        raise ValueError("saved cell lacks five distinct numbered repetitions")
    jobs = operation_plan(cell, [{"index": i} for i in range(cell.size)])
    summaries = [_validate_repeat(r, cell, jobs, identities, observation_contract=True)
                 for r in sorted(repeats, key=lambda r: r["repeat_number"])]
    expected = {f"r{r['repeat_number']}-op{row['number']}":
                (row["operation"], row["correct"], row.get("wall_ms"))
                for r in repeats for row in r["operations"]}
    if len(report["records"]) != 10000:
        raise ValueError("capacity envelope must retain every scheduled operation")
    for row in report["records"]:
        if expected.pop(row["question_id"], None) != (
                row.get("category"), row.get("qa_correct"), row.get("latency_ms")):
            raise ValueError("capacity envelope disagrees with retained operations")
    failures = sum(r["failures"] for r in summaries)
    if failures != report["metrics"].get("correctness_failures"):
        raise ValueError("capacity failure summary contradicts operation records")
    operations = {}
    for kind in summaries[0]["operations"]:
        rows = [r["operations"][kind] for r in summaries]
        values = [r["mean_wall_ms"] for r in rows if r["mean_wall_ms"] is not None]
        interval = blocked_mean_interval(values, unit="fresh database/process repetition")
        censored = any(r["measured"] != r["scheduled"] for r in rows)
        if len(values) != 5 or censored:
            interval["inferentially_usable"] = False
        operations[kind] = {"per_repeat": rows, "mean_wall_ms_interval": interval,
                            "timing_censored": censored}
    complete = failures == 0 and all(
        r["status"] == "complete" and r["measured"] == 2000
        and r["clean_worker_teardown"] and r["durable_workers_observed"] for r in summaries)
    throughput = blocked_mean_interval([r["received_operations_per_second"] for r in summaries],
                                       unit="fresh database/process repetition")
    if not complete:
        throughput["inferentially_usable"] = False
    gates = _repetition_gate_statuses(summaries, cell, _hardware)
    return {"execution_integrity_pass": complete, "failures": failures,
            "repetitions": summaries, "operations": operations,
            "received_operations_per_second_interval": throughput,
            "integrity_status": gates["integrity"], "resource_status": gates["resource"],
            "latency_status": gates["latency"], "backlog_status": gates["backlog"],
            "gate_status": gates,
            "interval_boundary": "five fresh repetitions; exploratory, not operation-level independent trials"}


def summarize(plan: dict, directory: Path, *, write_summary: bool = True) -> dict:
    """Report missing and failed cells without promoting partial measurements."""
    validate_plan(plan, live=False)
    cells, identities, runtime_identities = [], set(), {}
    for config in plan["cells"]:
        key = cell_id(config)
        path = directory / (key + ".json")
        if not path.exists():
            cells.append({"id": key, "status": "PENDING"})
            continue
        report, artifact_digest = _read_verified_snapshot(path)
        if report["protocol"]["config"] != config:
            raise ValueError("saved cell does not match frozen configuration")
        metrics = report["metrics"]
        if (metrics.get("source_before") != plan["source"]
                or metrics.get("source_after") != plan["source"]
                or not metrics.get("source_stable") or not metrics.get("model_stable")
                or metrics.get("host_identity_sha256") != plan["host"]["host_identity_sha256"]
                or report.get("models", {}).get("embedding") != plan["model"]):
            raise ValueError("cell source, model or host differs from frozen campaign")
        statistics = _repetition_statistics(report, Cell(**config), identities)
        environment = report.get("environment")
        if isinstance(environment, dict):
            identity = {key: environment.get(key) for key in ("python", "packages")}
            identity_hash = _digest(identity)
            runtime_identities[identity_hash] = identity
        cells.append({"id": key, "status": "COMPLETE" if statistics["execution_integrity_pass"] else "FAILED",
                      "sha256": artifact_digest, **statistics,
                      "wall_latency_ms": metrics.get("wall_latency_ms"),
                      "hardware_matches": metrics.get("hardware_matches_declared_target")})
    done = sum(c["status"] == "COMPLETE" for c in cells)
    gate_status = {name: _merge_status([cell.get(f"{name}_status", "PENDING") for cell in cells])
                   for name in ("integrity", "resource", "latency", "backlog")}
    result = {"schema": SCHEMA, "summary_schema": SUMMARY_SCHEMA,
              "binding_sha256": plan["binding_sha256"],
              "status": "COMPLETE" if done == 24 else "PARTIAL",
              "completed_cells": done, "declared_cells": 24, "cells": cells,
              "cell_artifact_sha256": {cell["id"]: cell["sha256"] for cell in cells
                                       if cell.get("sha256")},
              "observed_repetitions": len(identities), "gate_status": gate_status,
              "integrity_status": gate_status["integrity"],
              "resource_status": gate_status["resource"],
              "latency_status": gate_status["latency"],
              "backlog_status": gate_status["backlog"],
              "runtime": plan.get("runtime") or _runtime_identity(),
              "runtime_identities": runtime_identities,
              "primary_matrix_complete": False, "target_capacity_verified": False,
              "publication_ready": False,
              "boundary": "one-host execution completeness; no two-host acceptance decision"}
    if write_summary:
        _write_summary(directory, result)
    return result


def execute(plan: dict, directory: Path, *, max_cells: Optional[int] = None,
            runner: Callable = run_cell) -> dict:
    """Never repeat a saved cell; incomplete reservations require explicit investigation."""
    validate_plan(plan)
    if max_cells is not None and (type(max_cells) is not int or max_cells < 1):
        raise ValueError("max_cells must be a positive integer")
    directory.mkdir(parents=True, exist_ok=True)
    lock = directory / ".runner.lock"
    with _runner_lock(lock):
        summarize(plan, directory, write_summary=True)
        executed = 0
        for config in plan["cells"]:
            path = directory / (cell_id(config) + ".json")
            reservation = path.with_suffix(".started.json")
            if path.exists():
                if reservation.exists():
                    raise ValueError("saved cell has an unfinished reservation; preserve it and investigate")
                continue
            if reservation.exists():
                raise ValueError("unfinished cell reservation; preserve it and investigate before a new campaign")
            validate_plan(plan)
            cell = Cell(**config)
            whole_cell_timeout = _cell_watchdog_timeout(cell) if runner is run_cell else None
            with reservation.open("x", encoding="utf-8") as handle:
                json.dump({"binding_sha256": plan["binding_sha256"], "cell": config,
                           "started_unix": time.time(), "runtime": _runtime_identity(),
                           "whole_cell_timeout_seconds": whole_cell_timeout}, handle)
                handle.flush()
                os.fsync(handle.fileno())
            if runner is run_cell:
                report = _run_cell_with_watchdog(
                    cell, model_dir=plan["model_dir"], model_sha256=plan["model_sha256"], output=path,
                )
            else:
                report = runner(cell, model_dir=plan["model_dir"],
                                model_sha256=plan["model_sha256"])
            write_canonical_artifact(report, path)
            # The artifact and sidecar are the durable completion receipt. If
            # verification or cleanup is interrupted, the reservation remains
            # and the next invocation stops for explicit reconciliation.
            _read_verified(path)
            reservation.unlink()
            executed += 1
            status = summarize(plan, directory, write_summary=True)
            print(json.dumps({"cell": cell_id(config), "completed_cells": status["completed_cells"]}),
                  flush=True)
            if any(c["status"] == "FAILED" for c in status["cells"]):
                break
            if max_cells is not None and executed >= max_cells:
                break
        return summarize(plan, directory, write_summary=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--hardware", choices=sorted(HARDWARE), default="shared32")
    parser.add_argument("--model-dir")
    parser.add_argument("--model-sha256")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--results", type=Path)
    parser.add_argument("--max-cells", type=int)
    args = parser.parse_args(argv)
    try:
        if args.prepare:
            if args.execute or not args.model_dir or not args.model_sha256:
                raise ValueError("prepare requires pinned local model and cannot execute")
            plan = make_plan(hardware=args.hardware, model_dir=args.model_dir,
                             model_sha256=args.model_sha256)
            args.manifest.parent.mkdir(parents=True, exist_ok=True)
            with args.manifest.open("x", encoding="utf-8") as handle:
                handle.write(canonical_json(plan) + "\n")
        else:
            plan = json.loads(args.manifest.read_text(encoding="utf-8"))
            validate_plan(plan)
        if args.execute:
            if args.results is None:
                raise ValueError("execution requires --results")
            result = execute(plan, args.results, max_cells=args.max_cells)
        elif args.results:
            result = summarize(plan, args.results, write_summary=True)
        else:
            result = {key: plan[key] for key in ("schema", "binding_sha256", "hardware_profile",
                "scheduled_operations", "repetitions", "scheduled_arrival_hours")}
            result["dry_run"] = True
        print(json.dumps(result, indent=2))
        return 0 if result.get("status") != "PARTIAL" or not args.execute else 2
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(f"capacity campaign stopped: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
