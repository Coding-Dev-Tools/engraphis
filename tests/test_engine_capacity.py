import importlib.util
import hashlib

import pytest

from eval.benchmark import canonical_json, sha256_file, validate_report, write_canonical_artifact
from eval.engine_capacity import Cell, _local_model, operation_plan, protocol, run_cell


def test_protocol_declares_exact_capacity_matrix_and_sampling():
    plan = protocol()
    assert len(plan["primary_cells"]) == plan["primary_cell_count"] == 48
    assert plan["repeats"] == 5 and plan["operations_per_repeat"] == 2000
    assert plan["mixed_percent"] == {"recall": 80, "remember": 15, "correct": 4, "erase": 1}
    assert plan["target_capacity_verified"] is False


@pytest.mark.parametrize("changes", [
    {"concurrency": 2}, {"operations": 99}, {"size": 4}, {"arrival_rate": float("nan")},
    {"timeout_s": float("inf")}, {"backend": "auto"}, {"repeats": False},
    {"smoke": False}, {"concurrency": True}, {"arrival_rate": True},
    {"arrival_rate": 1, "timeout_s": 99},
])
def test_invalid_capacity_configuration_fails_before_storage(changes, monkeypatch):
    monkeypatch.setattr("eval.engine_capacity._seed", lambda *args: pytest.fail("opened storage"))
    with pytest.raises(ValueError):
        run_cell(Cell(**changes))


def test_workload_has_exact_ratios_and_disjoint_read_mutation_targets():
    cell = Cell()
    jobs = operation_plan(cell, [{"id": f"m{i}", "index": i} for i in range(cell.size)])
    from collections import Counter

    assert Counter(row["kind"] for row in jobs) == {"recall": 80, "remember": 15, "correct": 4, "erase": 1}
    read = {row["target"]["id"] for row in jobs if row["kind"] == "recall"}
    changed = [row["target"]["id"] for row in jobs if row["kind"] in {"correct", "erase"}]
    assert not read.intersection(changed)
    assert len(changed) == len(set(changed))
    assert jobs == operation_plan(cell, [{"id": f"m{i}", "index": i} for i in range(cell.size)])


def test_real_independent_engines_share_disposable_database_and_report_boundaries(tmp_path):
    report = run_cell(Cell(concurrency=4))
    assert validate_report(report) == []
    metrics = report["metrics"]
    repeat = metrics["repeats"][0]
    assert repeat["status"] == "complete"
    assert metrics["correctness_failures"] == 0
    assert len({item["pid"] for item in repeat["startup"]}) == 4
    assert len(repeat["operations"]) == 100
    assert all(row["wall_ms"] >= row["operation_ms"] for row in repeat["operations"])
    assert repeat["disk"]["database_bytes"] > 0
    assert repeat["input_sha256"]
    assert metrics["dataset_origin"] == "synthetic_generator"
    assert metrics["target_capacity_verified"] is False
    assert metrics["primary_matrix_complete"] is False
    assert metrics["model_stable"] is True
    assert report["models"]["embedding"]["semantic"] is False
    if importlib.util.find_spec("psutil"):
        assert repeat["observed_process_tree_peak_rss_bytes"] > 0
    assert write_canonical_artifact(report, tmp_path / "capacity.json")["sha256"]


def test_model_manifest_binds_existing_bytes_without_loading_a_model(tmp_path):
    artifact = tmp_path / "config.json"
    artifact.write_text('{"test_fixture": true}', encoding="utf-8")
    digest = hashlib.sha256(canonical_json({"config.json": sha256_file(artifact)}).encode()).hexdigest()
    assert _local_model(str(tmp_path), digest)["sha256"] == digest
    artifact.write_text('{"changed": true}', encoding="utf-8")
    with pytest.raises(ValueError, match="digest"):
        _local_model(str(tmp_path), digest)


def test_full_protocol_refuses_hashing_as_semantic_evidence():
    with pytest.raises(ValueError, match="semantic model"):
        run_cell(Cell(size=10_000, operations=2000, repeats=5, arrival_rate=1, smoke=False))


def test_missing_native_backend_is_not_silently_replaced():
    if importlib.util.find_spec("sqlite_vec"):
        pytest.skip("native extension exists; exercised by explicit native benchmark runs")
    with pytest.raises(ModuleNotFoundError, match="sqlite_vec"):
        run_cell(Cell(backend="sqlite-vec"))
