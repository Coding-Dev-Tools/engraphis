"""Synthetic metadata fixtures exercise aggregation, never measured capacity."""
from collections import Counter
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path

import pytest

from eval.benchmark import report_envelope, sha256_file, validate_report, write_canonical_artifact
from eval.capacity_matrix import _fingerprint, _validate_repeat, aggregate_capacity
from eval.engine_capacity import Cell, HARDWARE, SCHEMA, operation_plan, protocol


@pytest.fixture(scope="module")
def synthetic_matrix():
    root = Path(__file__).resolve().parents[1]
    names = ["engraphis/core/context.py", "engraphis/core/engine.py", "engraphis/core/recall.py",
             "engraphis/core/store.py", "engraphis/factory.py", "engraphis/backends/vector_numpy.py",
             "engraphis/backends/vector_sqlitevec.py", "engraphis/backends/embedder_st.py",
             "engraphis/backends/embedder_deterministic.py", "eval/vector_scale.py",
             "eval/vector_scale_storage.py", "eval/engine_capacity.py", "eval/benchmark.py"]
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
