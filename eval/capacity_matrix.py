"""Strict complete-matrix aggregation; consistency checks are not capacity certification."""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path
import re

from eval.benchmark import canonical_json, report_envelope, sha256_file, validate_report, write_canonical_artifact
from eval.engine_capacity import Cell, HARDWARE, SCHEMA as CELL_SCHEMA, operation_plan, protocol
from eval.rework_statistics import blocked_mean_interval
from eval.vector_scale import _latency_ms


SCHEMA = "engraphis-capacity-matrix/v1"
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_RUN = re.compile(r"[0-9a-f]{32}\Z")
_AXES = ("hardware", "size", "backend", "concurrency", "workload")
_BACKENDS = {"numpy": "NumpyVectorIndex", "sqlite-vec": "SqliteVecVectorIndex"}


def _number(value, field: str, *, positive: bool = False) -> float:
    if (type(value) not in {int, float} or not math.isfinite(value)
            or value < 0 or (positive and value == 0)):
        raise ValueError(f"{field} must be a finite {'positive' if positive else 'nonnegative'} number")
    return float(value)


def _digest(value, field: str) -> str:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise ValueError(f"{field} requires a SHA-256 identity")
    return value


def _fingerprint(value) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def _key(config: dict) -> tuple:
    return tuple(config.get(axis) for axis in _AXES)


def _cell_identity(report: dict, cell: Cell, *, fixture: bool) -> tuple[dict, dict]:
    metrics = report["metrics"]
    before = metrics.get("source_before")
    required_sources = {"engraphis/core/context.py", "engraphis/core/engine.py",
                        "engraphis/core/recall.py", "engraphis/core/store.py",
                        "engraphis/factory.py", "engraphis/backends/vector_numpy.py",
                        "engraphis/backends/vector_sqlitevec.py", "engraphis/backends/embedder_st.py",
                        "engraphis/backends/embedder_deterministic.py", "eval/vector_scale.py",
                        "eval/vector_scale_storage.py", "eval/engine_capacity.py", "eval/benchmark.py"}
    if (not isinstance(before, dict) or not required_sources <= set(before)
            or metrics.get("source_after") != before or metrics.get("source_stable") is not True
            or metrics.get("model_stable") is not True):
        raise ValueError("source/model identity is incomplete or changed during execution")
    for name, value in before.items():
        _digest(value, f"source {name}")
    if report["suite"]["sha256"] != before["eval/engine_capacity.py"]:
        raise ValueError("dataset generator must match the frozen runner source")
    observed_sources = Counter((item["name"], item["sha256"]) for item in report["suite"]["sources"])
    # Public envelopes deliberately retain basenames only; preserve multiplicity.
    if observed_sources != Counter((Path(name).name, digest) for name, digest in before.items()):
        raise ValueError("envelope source identities do not match measured source bytes")
    model = report["models"].get("embedding", {})
    if model.get("identity") != "local_directory" or model.get("semantic") is not True:
        raise ValueError("full capacity cells require the pinned local semantic model")
    _digest(model.get("sha256"), "semantic model")
    if report["models"].get("vector_backend") != {"identity": cell.backend}:
        raise ValueError("requested backend identity differs from the cell")
    counter = report["protocol"].get("token_accounting")
    if (not isinstance(counter, dict) or counter.get("identity") != "engraphis.regex.v1"
            or counter.get("revision") != before["engraphis/core/context.py"]):
        raise ValueError("token counter identity/revision is not bound to measured source")
    origin = metrics.get("measurement_origin")
    allowed = {"synthetic_fixture"} if fixture else {"observed_local_engine"}
    if origin not in allowed or metrics.get("measurement_version") != 1:
        raise ValueError("measurement origin/version does not match this validation mode")
    if (metrics.get("dataset_origin") != "synthetic_generator"
            or metrics.get("recall_diagnostics_enabled") is not True
            or metrics.get("independent_task_quality") is not False
            or metrics.get("target_capacity_verified") is not False
            or metrics.get("primary_matrix_complete") is not False):
        raise ValueError("cell provenance or claims differ from the runner contract")
    hardware = metrics.get("hardware", {})
    for name in ("cpu", "architecture", "sqlite"):
        if not isinstance(hardware.get(name), str) or not hardware[name].strip():
            raise ValueError(f"hardware {name} identity is missing")
    _number(hardware.get("logical_cpus"), "CPU count", positive=True)
    ram = _number(hardware.get("physical_ram_bytes"), "physical RAM", positive=True)
    matches = abs(ram / (HARDWARE[cell.hardware] * 1024 ** 3) - 1) <= 0.125
    if type(metrics.get("hardware_matches_declared_target")) is not bool or matches != metrics[
        "hardware_matches_declared_target"
    ]:
        raise ValueError("reported hardware gate disagrees with observed RAM")
    dependencies = metrics.get("runner_dependencies", {})
    if not isinstance(dependencies.get("psutil"), str) or not dependencies["psutil"]:
        raise ValueError("process-tree sampler dependency identity is required")
    if cell.backend == "sqlite-vec" and not dependencies.get("sqlite-vec"):
        raise ValueError("native backend version is required")
    common = {"source": before, "model": model, "token_counter": counter,
              "dimension": cell.dimension, "token_budget": cell.token_budget, "seed": cell.seed,
              "timeout_s": cell.timeout_s, "measurement_origin": origin,
              "git_commit": report["system"]["git_commit"],
              "packages": report["environment"].get("packages"), "dependencies": dependencies,
              "python": report["environment"].get("python"),
              "boundaries": {name: metrics.get(name) for name in
                             ("measurement_boundary", "memory_boundary", "startup_boundary")}}
    if not isinstance(common["git_commit"], str) or not re.fullmatch(r"[0-9a-f]{40}", common["git_commit"]):
        raise ValueError("cell evidence requires an exact Git revision")
    if any(not isinstance(value, str) or not value for value in common["boundaries"].values()):
        raise ValueError("measurement boundaries are required")
    return common, {"hardware": hardware, "environment": report["environment"],
                    "dependencies": dependencies, "ram_matches": matches}


def _validate_repeat(repeat: dict, cell: Cell, expected: list[dict],
                     executions: set[str]) -> dict:
    execution = repeat.get("execution_id")
    if not isinstance(execution, str) or not _RUN.fullmatch(execution) or execution in executions:
        raise ValueError("repetitions require distinct execution IDs across the whole matrix")
    executions.add(execution)
    rows = repeat.get("operations")
    if (not isinstance(rows, list) or len(rows) != 2000
            or any(type(row.get("number")) is not int for row in rows)
            or {row.get("number") for row in rows} != set(range(2000))):
        raise ValueError("each repetition requires all 2000 distinct scheduled operations")
    rows = sorted(rows, key=lambda row: row["number"])
    expected_counts = dict(Counter(job["kind"] for job in expected))
    if repeat.get("operation_counts") != expected_counts:
        raise ValueError("declared workload ratios do not match the schedule")
    schedule = [{"number": job["number"], "kind": job["kind"],
                 "target_index": job["target"]["index"]} for job in expected]
    if repeat.get("input_sha256") != _fingerprint(schedule):
        raise ValueError("dataset/workload schedule differs from the frozen generator")
    startup = repeat.get("startup")
    if not isinstance(startup, list) or len(startup) > cell.concurrency:
        raise ValueError("invalid startup observations")
    pids = set()
    for item in startup:
        pid = item.get("pid")
        if type(pid) is not int or pid <= 0 or pid in pids:
            raise ValueError("worker startup requires distinct process IDs")
        pids.add(pid)
        if (item.get("backend") != _BACKENDS[cell.backend]
                or item.get("embedding_dimension") != cell.dimension
                or item.get("embedding_semantic") is not True):
            raise ValueError("observed backend/dimension/capability differs from cell identity")
        _number(item.get("startup_ms"), "engine startup")
    status = repeat.get("status")
    if status not in {"complete", "timeout", "worker_exit", "worker_error", "startup_failed"}:
        raise ValueError("unknown repetition completion status")
    if status == "complete" and len(startup) != cell.concurrency:
        raise ValueError("complete repetition is missing process startup evidence")
    for row, job in zip(rows, expected):
        if row.get("operation") != job["kind"] or type(row.get("correct")) is not bool:
            raise ValueError("operation outcome/schedule mismatch")
        if "wall_ms" not in row:
            if row["correct"] or not row.get("error_type") or status == "complete":
                raise ValueError("missing timing cannot become a successful or complete observation")
            continue
        wall = _number(row["wall_ms"], "queue-inclusive wall time")
        if row.get("pid") not in pids:
            raise ValueError("operation was not measured by a declared process")
        for name in ("dispatch_lag_ms", "queue_ipc_ms"):
            _number(row.get(name), name)
        if row["correct"] and "error_type" in row:
            raise ValueError("failed operations cannot be scored correct")
        if "error_type" not in row:
            operation = _number(row.get("operation_ms"), "engine operation")
            verification = _number(row.get("verification_ms"), "canonical verification")
            if wall + 0.01 < operation + verification:
                raise ValueError("wall boundary excludes measured engine/verification work")
    elapsed = _number(repeat.get("elapsed_s"), "repeat elapsed", positive=True)
    if any(row["number"] / cell.arrival_rate + row["wall_ms"] / 1000 > elapsed + 0.05
           for row in rows if "wall_ms" in row):
        raise ValueError("queued latency and arrival schedule exceed the recorded repeat boundary")
    _number(repeat.get("seed_ms"), "seeding")
    samples = repeat.get("memory_samples")
    if type(samples) is not int or samples < 0:
        raise ValueError("memory sample count must be a nonnegative integer")
    peak = repeat.get("observed_process_tree_peak_rss_bytes")
    if peak is not None:
        _number(peak, "observed memory peak", positive=True)
    if (samples == 0) != (peak is None):
        raise ValueError("memory peak must agree with whether memory was sampled")
    disk = repeat.get("disk", {})
    values = [_number(disk.get(name), name) for name in
              ("database_bytes", "wal_bytes", "shared_memory_bytes")]
    if sum(values) != disk.get("total_bytes"):
        raise ValueError("disk total disagrees with component observations")
    summary = {"status": status, "failures": sum(not row["correct"] for row in rows),
               "measured": sum("wall_ms" in row for row in rows),
               "memory_peak": peak, "memory_samples": samples,
               "startup_ms": [item["startup_ms"] for item in startup], "operations": {}}
    for kind in expected_counts:
        selected = [row for row in rows if row["operation"] == kind]
        measured = [row["wall_ms"] for row in selected if "wall_ms" in row]
        summary["operations"][kind] = {
            "scheduled": len(selected), "measured": len(measured),
            "failures": sum(not row["correct"] for row in selected),
            "wall_latency_ms": _latency_ms(measured) if measured else None,
            "mean_wall_ms": sum(measured) / len(measured) if measured else None,
        }
    summary["received_operations_per_second"] = summary["measured"] / elapsed
    return summary


def aggregate_capacity(reports: list[dict], *, fixture: bool = False,
                       iterations: int = 2000, seed: int = 20260905) -> dict:
    """Require 48 complete cell manifests; failed scheduled operations remain visible."""
    if not isinstance(reports, list) or len(reports) != 48:
        raise ValueError("the primary matrix requires exactly 48 cell artifacts")
    expected_cells = {_key(config) for config in protocol()["primary_cells"]}
    found, executions, hardware_identities, cells, common = set(), set(), {}, [], None
    for report in reports:
        errors = validate_report(report)
        if errors:
            raise ValueError("invalid benchmark envelope: " + errors[0])
        if report["suite"].get("name") != CELL_SCHEMA or report.get("exclusions"):
            raise ValueError("only unexcluded engine-capacity cell reports are accepted")
        config = report["protocol"]["config"]
        if set(config) != set(asdict(Cell())):
            raise ValueError("capacity configuration fields must match the runner contract")
        cell = Cell(**config)
        cell.validate()
        if report["system"].get("config_sha256") != _fingerprint(config):
            raise ValueError("configuration digest differs from the declared cell")
        key = _key(config)
        if (cell.smoke or cell.repeats != 5 or cell.operations != 2000
                or key not in expected_cells or key in found):
            raise ValueError("duplicate, non-primary, smoke or incomplete capacity cell")
        found.add(key)
        identity, hardware = _cell_identity(report, cell, fixture=fixture)
        if common is not None and identity != common:
            raise ValueError("cells have incompatible source/model/tokenizer/configuration identities")
        common = identity
        previous = hardware_identities.setdefault(cell.hardware, hardware)
        if previous != hardware:
            raise ValueError("cells for a hardware profile used different machines/environments")
        repeats = report["metrics"].get("repeats")
        if (not isinstance(repeats, list) or len(repeats) != 5
                or any(type(row.get("repeat_number")) is not int for row in repeats)
                or {row.get("repeat_number") for row in repeats} != set(range(5))):
            raise ValueError("each cell requires five distinct numbered repetitions")
        jobs = operation_plan(cell, [{"index": i} for i in range(cell.size)])
        summaries = [_validate_repeat(row, cell, jobs, executions)
                     for row in sorted(repeats, key=lambda row: row["repeat_number"])]
        expected_records = {}
        for repeat in repeats:
            for row in repeat["operations"]:
                expected_records[f"r{repeat['repeat_number']}-op{row['number']}"] = (
                    row["operation"], row["correct"], row.get("wall_ms"))
        if len(report["records"]) != 10000:
            raise ValueError("cell envelope must retain all 10000 scheduled outcomes")
        for record in report["records"]:
            expected = expected_records.pop(record["question_id"], None)
            if expected != (record.get("category"), record.get("qa_correct"), record.get("latency_ms")):
                raise ValueError("envelope records disagree with raw measured outcomes")
        failure_count = sum(summary["failures"] for summary in summaries)
        if failure_count != report["metrics"].get("correctness_failures"):
            raise ValueError("reported failure total differs from scheduled outcomes")
        operations = {}
        for kind in summaries[0]["operations"]:
            values = [summary["operations"][kind] for summary in summaries]
            means = [value["mean_wall_ms"] for value in values if value["mean_wall_ms"] is not None]
            operations[kind] = {"scheduled": sum(value["scheduled"] for value in values),
                                "measured": sum(value["measured"] for value in values),
                                "failures": sum(value["failures"] for value in values),
                                "per_repeat": values,
                                "mean_wall_ms_interval": blocked_mean_interval(
                                    means, unit="fresh database/process repetition",
                                    iterations=iterations, seed=seed)}
            operations[kind]["timing_censored"] = any(value["measured"] != value["scheduled"] for value in values)
            if len(means) < 5 or operations[kind]["timing_censored"]:
                operations[kind]["mean_wall_ms_interval"]["inferentially_usable"] = False
        ram = hardware["hardware"]["physical_ram_bytes"]
        peaks = [summary["memory_peak"] for summary in summaries]
        cells.append({"cell": {axis: config[axis] for axis in _AXES}, "repeats": summaries,
                      "operations": operations, "failures": failure_count,
                      "hardware_ram_matches": hardware["ram_matches"],
                      "observed_rss_within_physical_ram": all(peak is not None and peak <= ram for peak in peaks),
                      "startup_peak_memory_known": False, "cold_cache_verified": False})
    if found != expected_cells or len(executions) != 240:
        raise ValueError("missing primary cells or independent repetition identities")
    cells.sort(key=lambda value: _key(value["cell"]))
    return report_envelope(
        suite=SCHEMA, dataset_path=Path(__file__),
        config={"iterations": iterations, "seed": seed, "fixture": fixture,
                "input_sha256": sorted(_fingerprint(report) for report in reports)},
        records=[{"question_id": "-".join(map(str, _key(cell["cell"]))),
                  "category": cell["cell"]["workload"], "qa_correct": cell["failures"] == 0}
                 for cell in cells],
        metrics={"cells": cells, "cell_count": 48, "repetition_count": 240,
                 "scheduled_operations": 480000, "matrix_structurally_complete": True,
                 "all_measurements_complete": all(summary["status"] == "complete" and
                     summary["measured"] == 2000 for cell in cells for summary in cell["repeats"]),
                 "target_capacity_verified": False, "publication_ready": False,
                 "measurement_authenticity_verified": False, "fixture": fixture,
                 "correctness_failures": sum(cell["failures"] for cell in cells),
                 "hardware_gates_pass": all(cell["hardware_ram_matches"] and
                     cell["observed_rss_within_physical_ram"] for cell in cells),
                 "input_identity": common, "hardware_identities": hardware_identities,
                 "limitations": ["RSS is sampled during operations, not a startup/allocation peak",
                    "startup uses warm OS cache; no cold-disk certification",
                    "five-repeat intervals are exploratory, not operation-level independent trials",
                    "no predeclared latency/resource SLO or independent task acceptance is evaluated",
                    "consistent artifacts and execution IDs do not prove measurement authenticity"]},
        source_paths=[Path(__file__), Path(__file__).with_name("rework_statistics.py")],
        models={"embedding": common["model"]}, token_accounting=common["token_counter"],
        command=["python", "-m", "eval.capacity_matrix", "--inputs", "<48-cell-artifacts>"])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", nargs="+", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--fixture", action="store_true")
    args = parser.parse_args(argv)
    reports = []
    for path in args.inputs:
        digest = sha256_file(path)
        checksum = path.with_name(path.name + ".sha256").read_text(encoding="utf-8").split()[0]
        if digest != checksum:
            raise ValueError("input artifact checksum does not match")
        reports.append(json.loads(path.read_text(encoding="utf-8")))
    report = aggregate_capacity(reports, fixture=args.fixture)
    print(json.dumps(write_canonical_artifact(report, args.output)))
    return int(report["metrics"]["correctness_failures"] > 0 or not report["metrics"]["hardware_gates_pass"])


if __name__ == "__main__":
    raise SystemExit(main())
