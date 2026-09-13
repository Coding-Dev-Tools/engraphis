"""Synthetic metadata fixtures exercise aggregation, never measured capacity."""
from collections import Counter
from copy import deepcopy
from dataclasses import asdict
import json
import math
from pathlib import Path

import pytest

from eval.benchmark import report_envelope, sha256_file, validate_report, write_canonical_artifact
from eval.capacity_matrix import _fingerprint, _validate_repeat, aggregate_capacity, main
from eval.engine_capacity import (
    BACKLOG_INTERVAL_S, RSS_INTERVAL_S, RESOURCE_PHASES, Cell, HARDWARE, SCHEMA,
    _backlog_assessment, acceptance_policy, operation_plan, protocol,
)


@pytest.fixture(scope="module")
def synthetic_matrix():
    root = Path(__file__).resolve().parents[1]
    names = ["engraphis/core/context.py", "engraphis/core/engine.py", "engraphis/core/recall.py",
             "engraphis/core/store.py", "engraphis/factory.py", "engraphis/backends/vector_numpy.py",
             "engraphis/backends/vector_sqlitevec.py", "engraphis/backends/embedder_st.py",
             "engraphis/backends/embedder_deterministic.py", "eval/vector_scale.py",
             "eval/vector_scale_storage.py", "eval/engine_capacity.py", "eval/capacity_matrix.py", "eval/benchmark.py"]
    sources = {name: sha256_file(root / name) for name in names}
    template = report_envelope(suite=SCHEMA, dataset_path=root / "eval/engine_capacity.py",
                               config={}, records=[], source_paths=[root / name for name in names])
    reports = []
    for cell_number, axes in enumerate(protocol()["primary_cells"]):
        cell = Cell(**axes, operations=2000, repeats=5, dimension=384, arrival_rate=axes["concurrency"], smoke=False)
        jobs = operation_plan(cell, [{"index": i} for i in range(cell.size)])
        rows = [{"number": job["number"], "operation": job["kind"], "correct": True,
                 "pid": 10 + job["number"] % cell.concurrency, "wall_ms": 10.0 + job["number"] % 7,
                 "operation_ms": 1.0, "verification_ms": 0.5, "dispatch_lag_ms": 0.0,
                 "queue_ipc_ms": 8.5 + job["number"] % 7} for job in jobs]
        repeats = [{"execution_id": f"{cell_number * 5 + number:032x}", "repeat_number": number,
                    "status": "complete", "seed_ms": 100.0, "elapsed_s": 2000 / cell.concurrency,
                    "startup": [{"pid": 10 + i, "startup_ms": 20.0 + i,
                                 "backend": "NumpyVectorIndex" if cell.backend == "numpy" else "SqliteVecVectorIndex",
                                 "embedding_dimension": 384, "embedding_semantic": True}
                                for i in range(cell.concurrency)],
                    "operations": rows, "operation_counts": dict(Counter(job["kind"] for job in jobs)),
                    "memory_samples": 100, "observed_process_tree_peak_rss_bytes": 1024 ** 3,
                    "disk": {"database_bytes": 1000, "wal_bytes": 10, "shared_memory_bytes": 20, "total_bytes": 1030},
                    "input_sha256": _fingerprint([{"number": job["number"], "kind": job["kind"],
                                                   "target_index": job["target"]["index"]} for job in jobs])}
                   for number in range(5)]
        config = asdict(cell)
        report = deepcopy(template)
        report["system"]["config_sha256"] = _fingerprint(config)
        report["protocol"].update({"config": config, "n_total": 10000, "n_scored": 10000,
                                  "token_accounting": {"identity": "engraphis.regex.v1", "revision": sources["engraphis/core/context.py"],
                                                       "scope": "packed context", "method": "named regex counter"}})
        report["models"] = {"embedding": {"identity": "local_directory", "sha256": "a" * 64, "semantic": True},
                            "vector_backend": {"identity": cell.backend}}
        report["records"] = [{"question_id": f"r{number}-op{row['number']}", "category": row["operation"],
                              "qa_correct": row["correct"], "latency_ms": row["wall_ms"]}
                             for number in range(5) for row in rows]
        report["metrics"] = {
            "repeats": repeats, "source_before": sources, "source_after": sources, "source_stable": True, "model_stable": True,
            "measurement_origin": "synthetic_fixture", "measurement_version": 1,
            "dataset_origin": "synthetic_generator", "recall_diagnostics_enabled": True,
            "independent_task_quality": False, "target_capacity_verified": False, "primary_matrix_complete": False,
            "hardware": {"cpu": "SYNTHETIC TEST HARDWARE", "architecture": "test", "logical_cpus": 4,
                         "sqlite": "test-version", "physical_ram_bytes": HARDWARE[cell.hardware] * 1024 ** 3},
            "hardware_matches_declared_target": True, "runner_dependencies": {"psutil": "test", "sqlite-vec": "test"},
            "measurement_boundary": "synthetic fixture, no measurements", "memory_boundary": "synthetic fixture",
            "startup_boundary": "synthetic fixture", "correctness_failures": 0}
        reports.append(report)
    return reports


def test_complete_structural_matrix_never_becomes_measured_capacity(synthetic_matrix, tmp_path):
    report = aggregate_capacity(synthetic_matrix, fixture=True, iterations=1000)
    assert validate_report(report) == []
    metrics = report["metrics"]
    assert metrics["matrix_structurally_complete"] is True
    assert (metrics["cell_count"], metrics["repetition_count"], metrics["scheduled_operations"]) == (48, 240, 480000)
    assert metrics["target_capacity_verified"] is False
    assert metrics["measurement_authenticity_verified"] is False
    assert metrics["fixture"] is True
    assert metrics["capacity_acceptance_pass"] is False
    assert metrics["protocol_observations_pass"] is False
    assert metrics["hardware_gates_pass"] is False
    assert metrics["cells"][0]["operations"]["recall"]["mean_wall_ms_interval"]["units"] == 5
    assert write_canonical_artifact(report, tmp_path / "synthetic-matrix.json")["sha256"]


def test_fixture_is_rejected_as_observed_measurements(synthetic_matrix):
    with pytest.raises(ValueError, match="origin"):
        aggregate_capacity(synthetic_matrix)


@pytest.mark.parametrize("damage", ["missing_cell", "duplicate_cell", "missing_repeat", "duplicate_repeat", "duplicate_execution",
                                   "source", "model", "hardware", "backend", "missing_operation", "duplicate_operation", "wrong_schedule", "record"])
def test_incomplete_or_incompatible_matrix_cannot_be_promoted(synthetic_matrix, damage):
    reports = list(synthetic_matrix)
    first = reports[0] = deepcopy(reports[0])
    repeat = first["metrics"]["repeats"][0]
    if damage == "missing_cell":
        reports.pop()
    elif damage == "duplicate_cell":
        reports[1] = first
    elif damage == "missing_repeat":
        first["metrics"]["repeats"].pop()
    elif damage == "duplicate_repeat":
        first["metrics"]["repeats"][1]["repeat_number"] = 0
    elif damage == "duplicate_execution":
        first["metrics"]["repeats"][1]["execution_id"] = repeat["execution_id"]
    elif damage == "source":
        first["metrics"]["source_stable"] = False
    elif damage == "model":
        first["models"]["embedding"]["semantic"] = False
    elif damage == "hardware":
        first["metrics"]["hardware"]["physical_ram_bytes"] *= 2
    elif damage == "backend":
        repeat["startup"][0]["backend"] = "SilentFallback"
    elif damage == "missing_operation":
        repeat["operations"].pop()
    elif damage == "duplicate_operation":
        repeat["operations"][1]["number"] = 0
    elif damage == "wrong_schedule":
        repeat["input_sha256"] = "b" * 64
    else:
        first["records"][0]["qa_correct"] = False
    with pytest.raises(ValueError):
        aggregate_capacity(reports, fixture=True, iterations=1000)


def test_interrupted_repeat_retains_every_unmeasured_operation_as_failure(synthetic_matrix):
    report = synthetic_matrix[0]
    cell = Cell(**report["protocol"]["config"])
    repeat = deepcopy(report["metrics"]["repeats"][0])
    repeat["status"] = "timeout"
    repeat["operations"][0] = {"number": 0, "operation": "recall", "correct": False, "error_type": "timeout"}
    jobs = operation_plan(cell, [{"index": i} for i in range(cell.size)])
    result = _validate_repeat(repeat, cell, jobs, set())
    assert result["measured"] == 1999 and result["failures"] == 1
    assert result["operations"]["recall"]["scheduled"] == 2000


@pytest.fixture(scope="module")
def lifecycle_matrix(synthetic_matrix):
    """Invented observations test structure only; every artifact retains its fixture origin."""
    references = {"schema": "engraphis-capacity-reference-hosts/v1",
                  "policy_sha256": _fingerprint(acceptance_policy()), "hosts": {}}
    for profile, symbol in (("laptop16", "c"), ("shared32", "d")):
        report = next(report for report in synthetic_matrix if report["protocol"]["config"]["hardware"] == profile)
        references["hosts"][profile] = {"host_identity_sha256": symbol * 64,
                                       "hardware_sha256": _fingerprint(report["metrics"]["hardware"])}
    reports = []
    for source in synthetic_matrix:
        report = {**source, "metrics": dict(source["metrics"])}
        metrics = report["metrics"]
        cell = Cell(**report["protocol"]["config"])
        metrics.update({"resource_observation_version": 2, "acceptance_policy": acceptance_policy(),
                        "acceptance_policy_sha256": _fingerprint(acceptance_policy()),
                        "sqlite_durability": acceptance_policy()["sqlite_durability"],
                        "reference_hosts_sha256": _fingerprint(references),
                        "host_identity_sha256": references["hosts"][cell.hardware]["host_identity_sha256"],
                        "host_identity_stable": True})
        metrics["repeats"] = []
        for original in source["metrics"]["repeats"]:
            repeat = dict(original)
            repeat["startup"] = [{**item, "sqlite_durability": {
                "configured": "durable", "effective": "durable", "journal_mode": "wal", "synchronous": "FULL",
                "file_backed": True, "read_only": False, "matches_requested": True,
            }} for item in original["startup"]]
            repeat.update({"lifecycle_errors": [], "late_result_count": 0, "worker_exitcodes": [0] * cell.concurrency})
            repeat["resource_observations"] = {
                "version": 2, "started_before_seeding": True, "sampler_thread_stopped": True,
                "requested_sample_interval_s": RSS_INTERVAL_S,
                "phases": {name: {"sampling_attempts": 25, "sample_count": 25, "unavailable_samples": 0,
                                  "observed_peak_rss_bytes": 1024 ** 3, "first_sample_elapsed_s": index * 2,
                                  "last_sample_elapsed_s": index * 2 + 1.2, "max_sample_gap_s": 0.05}
                           for index, name in enumerate(RESOURCE_PHASES)},
            }
            series = []
            elapsed = repeat["elapsed_s"]
            for second in list(range(math.ceil(elapsed))) + [elapsed]:
                due = min(cell.operations, math.floor(second * cell.arrival_rate) + 1)
                received = cell.operations if second == elapsed else max(0, due - 1)
                series.append({"elapsed_s": second, "phase": "workload", "scheduled_due": due,
                               "submitted": due, "received": received, "scheduled_outstanding": due - received,
                               "dispatch_pending": 0, "submitted_unreceived": due - received})
            repeat["backlog_observations"] = {
                "version": 1, "requested_sample_interval_s": BACKLOG_INTERVAL_S,
                "offered_operations_per_second": cell.arrival_rate,
                "offering_observed_until_s": elapsed, "series": series,
            }
            repeat["backlog_assessment"] = _backlog_assessment(cell, repeat["backlog_observations"], execution_complete=True)
            metrics["repeats"].append(repeat)
        reports.append(report)
    return reports, references


def test_complete_synthetic_lifecycle_matrix_only_passes_structural_observations(lifecycle_matrix):
    reports, references = lifecycle_matrix
    report = aggregate_capacity(reports, fixture=True, reference_hosts=references, iterations=1000)
    metrics = report["metrics"]
    assert metrics["protocol_observations_pass"] is True
    assert metrics["hardware_gates_pass"] is False
    assert metrics["capacity_acceptance_pass"] is False
    assert metrics["resource_stability_gate_pass"] is False
    assert metrics["responsiveness_gate_pass"] is False
    assert metrics["target_capacity_verified"] is False
    assert metrics["publication_ready"] is False
    assert metrics["fixture"] is True
    assert all(cell["startup_rss_observed"] for cell in metrics["cells"])
    limits = [cell for cell in metrics["cells"] if cell["recall_p95_limit_ms"] is not None]
    assert len(limits) == 8
    assert {cell["recall_p95_limit_ms"] for cell in limits} == {1000, 2000}


@pytest.mark.parametrize("damage", ["missing_reference", "unknown_startup", "partial_resource", "sampler_running",
                                   "rss_boundary", "missing_backlog", "worker_failed", "normal_durability", "p95", "growing_backlog"])
def test_partial_or_failing_observations_never_pass_acceptance(lifecycle_matrix, damage):
    original, references = lifecycle_matrix
    reports = list(original)
    target = next(index for index, report in enumerate(reports)
                  if report["protocol"]["config"]["size"] == 100_000
                  and report["protocol"]["config"]["hardware"] == "laptop16"
                  and report["protocol"]["config"]["concurrency"] == 4)
    first = reports[target] = deepcopy(reports[target])
    repeat = first["metrics"]["repeats"][0]
    if damage == "missing_reference":
        references = None
    elif damage == "unknown_startup":
        phase = repeat["resource_observations"]["phases"]["startup"]
        phase.update({"sample_count": 0, "unavailable_samples": 25, "observed_peak_rss_bytes": None})
        repeat["memory_samples"] -= 25
    elif damage == "partial_resource":
        repeat["resource_observations"]["phases"].pop("seeding")
    elif damage == "sampler_running":
        repeat["resource_observations"]["sampler_thread_stopped"] = False
    elif damage == "rss_boundary":
        peak = first["metrics"]["hardware"]["physical_ram_bytes"] * 0.75
        repeat["resource_observations"]["phases"]["startup"]["observed_peak_rss_bytes"] = peak
        repeat["observed_process_tree_peak_rss_bytes"] = peak
    elif damage == "missing_backlog":
        repeat.pop("backlog_observations")
    elif damage == "worker_failed":
        repeat["worker_exitcodes"][0] = 1
    elif damage == "normal_durability":
        repeat["startup"][0]["sqlite_durability"]["synchronous"] = "NORMAL"
    elif damage == "p95":
        # Preserve record/raw consistency while exceeding the existing 1s target.
        repeat["operations"] = deepcopy(repeat["operations"])
        for index, row in enumerate(repeat["operations"]):
            row["wall_ms"] += 1001
            first["records"][index]["latency_ms"] += 1001
        repeat["elapsed_s"] += 2
        observations = repeat["backlog_observations"]
        observations["offering_observed_until_s"] = repeat["elapsed_s"]
        observations["series"].append({**observations["series"][-1], "elapsed_s": repeat["elapsed_s"]})
        for sample in observations["series"]:
            sample["received"] = sum(row["number"] / 4 + row["wall_ms"] / 1000 <= sample["elapsed_s"]
                                     for row in repeat["operations"])
            sample["scheduled_outstanding"] = sample["scheduled_due"] - sample["received"]
            sample["submitted_unreceived"] = sample["scheduled_outstanding"]
        repeat["backlog_assessment"] = _backlog_assessment(
            Cell(**first["protocol"]["config"]), observations, execution_complete=True)
    else:
        observations = repeat["backlog_observations"]
        for row in observations["series"][:-1]:
            row["received"] = row["scheduled_due"] // 2
            row["scheduled_outstanding"] = row["scheduled_due"] - row["received"]
            row["submitted_unreceived"] = row["scheduled_outstanding"]
        repeat["operations"] = deepcopy(repeat["operations"])
        for index, row in enumerate(repeat["operations"]):
            receipt = min(repeat["elapsed_s"], (row["number"] * 2 + 1) / 4)
            row["wall_ms"] = (receipt - row["number"] / 4) * 1000
            first["records"][index]["latency_ms"] = row["wall_ms"]
        repeat["backlog_assessment"] = _backlog_assessment(
            Cell(**first["protocol"]["config"]), observations, execution_complete=True)
    metrics = aggregate_capacity(reports, fixture=True, reference_hosts=references, iterations=1000)["metrics"]
    assert metrics["protocol_observations_pass"] is False
    assert metrics["capacity_acceptance_pass"] is False


@pytest.mark.parametrize("damage", ["assessment", "counter", "resource_total", "policy", "reference_host", "unbound_reference"])
def test_relabeling_or_inconsistent_lifecycle_evidence_fails_closed(lifecycle_matrix, damage):
    original, references = lifecycle_matrix
    reports = list(original)
    first = reports[0] = deepcopy(reports[0])
    repeat = first["metrics"]["repeats"][0]
    if damage == "assessment":
        repeat["backlog_assessment"]["sustained_growth_observed"] = True
    elif damage == "counter":
        repeat["backlog_observations"]["series"][1]["scheduled_outstanding"] += 1
    elif damage == "resource_total":
        repeat["memory_samples"] += 1
    elif damage == "policy":
        first["metrics"]["acceptance_policy_sha256"] = "e" * 64
    elif damage == "unbound_reference":
        first["metrics"]["reference_hosts_sha256"] = None
    else:
        first["metrics"]["host_identity_sha256"] = "e" * 64
    with pytest.raises(ValueError):
        aggregate_capacity(reports, fixture=True, reference_hosts=references, iterations=1000)


def test_require_acceptance_cli_cannot_promote_a_fixture(tmp_path, monkeypatch, capsys):
    path = tmp_path / "fixture.json"
    path.write_text('{}', encoding="utf-8")
    path.with_name(path.name + ".sha256").write_text(sha256_file(path), encoding="utf-8")
    calls = []

    def aggregate(reports, **kwargs):
        calls.append(kwargs)
        return {"metrics": {"correctness_failures": 0, "hardware_gates_pass": True,
                            "protocol_observations_pass": True, "capacity_acceptance_pass": False,
                            "fixture": True}}

    monkeypatch.setattr("eval.capacity_matrix.aggregate_capacity", aggregate)
    monkeypatch.setattr("eval.capacity_matrix.write_canonical_artifact", lambda *args: {"synthetic_fixture": True})
    assert main(["--inputs", str(path), "--output", str(tmp_path / "result.json"),
                 "--fixture", "--require-acceptance"]) == 1
    assert calls == [{"fixture": True, "reference_hosts": None}]
    assert json.loads(capsys.readouterr().out) == {"synthetic_fixture": True}
