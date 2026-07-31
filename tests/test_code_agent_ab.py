import json

import pytest

from eval.code_agent_ab import evaluate, load_runs, main


def _write(path, condition, rows):
    path.write_text(
        "\n".join(json.dumps({"condition": condition, **row}) for row in rows) + "\n",
        encoding="utf-8",
    )
    return path


def _rows():
    return [
        {
            "task_id": "task-a",
            "oracle": "pytest:test_a",
            "success": True,
            "input_tokens": 100,
            "output_tokens": 20,
            "tool_tokens": 10,
            "retries": 1,
            "latency_ms": 1000,
            "cost_usd": 0.02,
        },
        {
            "task_id": "task-b",
            "oracle": "pytest:test_b",
            "success": False,
            "input_tokens": 200,
            "output_tokens": 40,
            "tool_tokens": 20,
            "retries": 2,
            "latency_ms": 2000,
            "cost_usd": 0.04,
        },
    ]


def test_paired_report_measures_success_and_resource_deltas(tmp_path):
    baseline_path = _write(tmp_path / "baseline.jsonl", "full_history", _rows())
    candidate_rows = _rows()
    candidate_rows[1] = {
        **candidate_rows[1],
        "success": True,
        "input_tokens": 80,
        "output_tokens": 20,
        "tool_tokens": 10,
        "retries": 0,
        "latency_ms": 900,
        "cost_usd": 0.015,
    }
    candidate_path = _write(tmp_path / "candidate.jsonl", "engraphis", candidate_rows)

    report = evaluate(
        load_runs(baseline_path, condition="full_history"),
        load_runs(candidate_path, condition="engraphis"),
        iterations=100,
    )

    assert report["paired_tasks"] == 2
    assert report["full_history"]["success_rate"] == 0.5
    assert report["engraphis"]["success_rate"] == 1.0
    assert report["paired_bootstrap"]["success_rate"]["delta"] == 0.5
    assert report["paired_bootstrap"]["mean_total_tokens"]["delta"] < 0
    assert report["paired_bootstrap"]["mean_cost_usd"]["delta"] < 0


def test_pairing_rejects_mismatched_tasks_and_oracles(tmp_path):
    baseline_path = _write(tmp_path / "baseline.jsonl", "full_history", _rows())
    candidate_rows = _rows()[:1]
    candidate_path = _write(tmp_path / "candidate.jsonl", "engraphis", candidate_rows)

    with pytest.raises(ValueError, match="paired task IDs differ"):
        evaluate(
            load_runs(baseline_path, condition="full_history"),
            load_runs(candidate_path, condition="engraphis"),
        )

    candidate_rows = _rows()
    candidate_rows[0] = {**candidate_rows[0], "oracle": "different"}
    candidate_path = _write(tmp_path / "candidate.jsonl", "engraphis", candidate_rows)
    with pytest.raises(ValueError, match="different success oracles"):
        evaluate(
            load_runs(baseline_path, condition="full_history"),
            load_runs(candidate_path, condition="engraphis"),
        )


def test_cli_writes_json_report(tmp_path):
    baseline_path = _write(tmp_path / "baseline.jsonl", "full_history", _rows())
    candidate_path = _write(tmp_path / "candidate.jsonl", "engraphis", _rows())
    output = tmp_path / "report.json"

    assert main([
        "--full-history", str(baseline_path),
        "--engraphis", str(candidate_path),
        "--iterations", "10",
        "--output", str(output),
    ]) == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["paired_tasks"] == 2
    assert report["matched_oracle_count"] == 2
    assert "task-a" not in json.dumps(report)
    assert "pytest:test_a" not in json.dumps(report)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_run_loader_rejects_non_finite_metrics(tmp_path, value):
    bad = _write(tmp_path / "bad.jsonl", "full_history", [{
        **_rows()[0], "latency_ms": value,
    }])

    with pytest.raises(ValueError, match="latency_ms must be non-negative"):
        load_runs(bad, condition="full_history")


@pytest.mark.parametrize("iterations", [0, -1, True, 1.5, "10"])
def test_evaluate_rejects_invalid_iterations(tmp_path, iterations):
    baseline = _write(tmp_path / "baseline.jsonl", "full_history", _rows())
    candidate = _write(tmp_path / "candidate.jsonl", "engraphis", _rows())
    with pytest.raises(ValueError, match="iterations must be a positive integer"):
        evaluate(
            load_runs(baseline, condition="full_history"),
            load_runs(candidate, condition="engraphis"),
            iterations=iterations,
        )


@pytest.mark.parametrize("seed", [True, 1.5, "20260730"])
def test_evaluate_rejects_non_integer_seed(tmp_path, seed):
    baseline = _write(tmp_path / "baseline.jsonl", "full_history", _rows())
    candidate = _write(tmp_path / "candidate.jsonl", "engraphis", _rows())
    with pytest.raises(ValueError, match="seed must be an integer"):
        evaluate(
            load_runs(baseline, condition="full_history"),
            load_runs(candidate, condition="engraphis"),
            seed=seed,
        )
