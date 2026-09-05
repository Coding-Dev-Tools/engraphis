import json

import pytest

from eval.benchmark import validate_report, write_canonical_artifact
from eval import vector_scale_storage as scale


def _run(**kwargs):
    return scale.run_file_backed(
        [5, 12], dim=8, queries=2, iterations=1, warmups=0,
        k=2, seed=17, batch_size=3, mixed_writes=1, **kwargs,
    )


def test_file_backed_matrix_reports_durable_writes_and_resource_boundaries(tmp_path):
    report = _run(backend="numpy")
    assert validate_report(report) == []
    cells = report["metrics"]["cells"]
    assert {(row["corpus_size"], row["concurrency"]) for row in cells} == {
        (size, concurrency) for size in (5, 12) for concurrency in (1, 4, 16)
    }
    assert all(row["numpy_reference_parity"] for row in cells)
    for row in report["metrics"]["storage"]:
        assert row["durable_memory_rows"] == row["durable_vector_rows"] == row["corpus_size"] + 3
        assert all(item["committed_writes"] == 1 for item in row["mixed"])
        assert row["restart_ms"] >= 0
        assert row["final_disk"]["database_bytes"] > 0
        assert row["rebuild"]["measured"] is False
    assert report["metrics"]["source_stable"]
    assert report["models"]["vector_backend"]["identity"] == "NumpyVectorIndex"
    command = report["protocol"]["command"]
    for flag, value in (("--dim", "8"), ("--queries", "2"), ("--iterations", "1"),
                        ("--warmups", "0"), ("--k", "2"), ("--seed", "17"),
                        ("--mixed-writes", "1"), ("--batch-size", "3"),
                        ("--concurrencies", "1,4,16"), ("--tenants", "4")):
        assert command[command.index(flag) + 1] == value
    assert "not end-to-end" in report["metrics"]["measurement_scope"]
    encoded = json.dumps(report)
    assert "Synthetic exact-index record" not in encoded
    assert "corpus.db" not in encoded
    assert str(tmp_path) not in encoded
    written = write_canonical_artifact(report, tmp_path / "scale.json")
    assert written["sha256"]


def test_invalid_matrix_is_rejected_before_opening_storage(monkeypatch):
    def must_not_open(*args, **kwargs):
        raise AssertionError("validation must precede storage mutation")

    monkeypatch.setattr(scale, "Store", must_not_open)
    with pytest.raises(ValueError, match="concurrencies"):
        _run(concurrencies=[1, 1])
    with pytest.raises(ValueError, match="positive"):
        scale.run_file_backed([10], batch_size=0)


def test_source_drift_is_reported_without_claiming_matching_evidence(monkeypatch):
    snapshots = iter([{"revision": "before"}, {"revision": "after"}])
    monkeypatch.setattr(scale, "_source_snapshot", lambda: next(snapshots))
    report = _run(backend="numpy", concurrencies=[1])
    assert report["metrics"]["source_stable"] is False


def test_native_rebuild_and_restart_match_numpy_when_available():
    pytest.importorskip("sqlite_vec")
    native = _run(backend="sqlite-vec", concurrencies=[1])
    numpy = _run(backend="numpy", concurrencies=[1])
    assert native["protocol"]["config"]["inputs"] == numpy["protocol"]["config"]["inputs"]
    assert [cell["result_ids_sha256"] for cell in native["metrics"]["cells"]] == [
        cell["result_ids_sha256"] for cell in numpy["metrics"]["cells"]
    ]
    for row in native["metrics"]["storage"]:
        assert row["rebuild"]["measured"] is True
        assert row["rebuild"]["records_replayed"] == row["durable_memory_rows"]
    assert native["models"]["vector_backend"]["identity"] == "SqliteVecVectorIndex"


def test_interrupted_matrix_preserves_completed_cell_timings(tmp_path, monkeypatch):
    original = scale._reads
    calls = 0

    def interrupted_reads(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 3:  # cold read and the first measured cell already completed
            raise RuntimeError("simulated interruption")
        return original(*args, **kwargs)

    monkeypatch.setattr(scale, "_reads", interrupted_reads)
    output = tmp_path / "interrupted.checkpoint.json"
    with pytest.raises(RuntimeError, match="simulated interruption"):
        _run(checkpoint=lambda payload: scale.write_scale_checkpoint(output, payload))
    checkpoint = json.loads(output.read_text())
    assert checkpoint["status"] == "incomplete"
    assert checkpoint["config"]["seed"] == 17
    assert checkpoint["source_before"]["files"]
    assert len(checkpoint["completed_cells"]) == 1
    cell = checkpoint["completed_cells"][0]
    assert cell["latency_ms"]["p50"] > 0
    assert cell["numpy_reference_parity"]
    assert checkpoint["completed_storage"] == []
    assert "Synthetic exact-index record" not in output.read_text()
    assert not list(tmp_path.glob("*.tmp"))
