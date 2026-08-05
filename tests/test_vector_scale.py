import json

import pytest

from eval import vector_scale


def test_scale_report_has_deterministic_inputs_and_observed_envelopes():
    first = vector_scale.run([3, 7], dim=8, queries=2, iterations=2, warmups=0, k=2, seed=9)
    second = vector_scale.run([3, 7], dim=8, queries=2, iterations=2, warmups=0, k=2, seed=9)

    assert first["schema"] == "engraphis-vector-scale/v1"
    assert first["measurement"]["timing_interpretation"] == (
        "machine-specific observed envelope, not a pass/fail limit"
    )
    assert first["inputs"] == second["inputs"]
    assert [row["result_ids_sha256"] for row in first["results"]] == [
        row["result_ids_sha256"] for row in second["results"]
    ]
    assert [row["corpus_size"] for row in first["results"]] == [3, 7]
    assert all(row["timed_searches"] == 4 for row in first["results"])
    assert all(row["latency_ms"]["p99"] >= row["latency_ms"]["p50"] for row in first["results"])


def test_sizes_are_validated():
    with pytest.raises(ValueError, match="distinct positive"):
        vector_scale.parse_sizes("3,3")
    with pytest.raises(ValueError, match="distinct positive"):
        vector_scale.parse_sizes("0")


def test_cli_writes_json(capsys):
    assert vector_scale.main([
        "--sizes", "3", "--dim", "8", "--queries", "1", "--iterations", "1", "--warmups", "0",
        "--json",
    ]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["results"][0]["corpus_size"] == 3


def test_backend_defaults_to_numpy_and_reports_exact_knn():
    report = vector_scale.run([3], dim=8, queries=1, iterations=1, warmups=0, k=2, seed=9)

    assert report["config"]["backend_requested"] == "numpy"
    assert report["environment"]["vector_backend"] == "NumpyVectorIndex"
    assert report["measurement"]["exact_knn"] is True
    assert report["measurement"]["setup_included_in_latency"] is False


def test_backend_validation_and_clear_sqlite_vec_unavailable_error(monkeypatch):
    with pytest.raises(ValueError, match="backend must be one of"):
        vector_scale.run([3], dim=8, queries=1, iterations=1, warmups=0, k=2, backend="ann")

    monkeypatch.setattr(vector_scale, "get_vector_index", lambda *_args, **_kwargs: (_ for _ in ()).throw(ImportError("missing")))
    with pytest.raises(RuntimeError, match="sqlite-vec backend is unavailable"):
        vector_scale.run([3], dim=8, queries=1, iterations=1, warmups=0, k=2, backend="sqlite-vec")


def test_cli_accepts_backend(capsys):
    assert vector_scale.main([
        "--sizes", "3", "--dim", "8", "--queries", "1", "--iterations", "1", "--warmups", "0",
        "--backend", "numpy", "--json",
    ]) == 0

    assert json.loads(capsys.readouterr().out)["config"]["backend_requested"] == "numpy"


def test_sqlite_vec_matches_numpy_result_ids_when_available():
    pytest.importorskip("sqlite_vec")
    kwargs = {"dim": 8, "queries": 2, "iterations": 2, "warmups": 0, "k": 2, "seed": 9}
    numpy_report = vector_scale.run([3, 7], backend="numpy", **kwargs)
    sqlite_report = vector_scale.run([3, 7], backend="sqlite-vec", **kwargs)

    assert sqlite_report["inputs"] == numpy_report["inputs"]
    assert [row["result_ids_sha256"] for row in sqlite_report["results"]] == [
        row["result_ids_sha256"] for row in numpy_report["results"]
    ]
    assert sqlite_report["environment"]["vector_backend"] == "SqliteVecVectorIndex"
