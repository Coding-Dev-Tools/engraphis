import copy
import hashlib
import json
from pathlib import Path

import pytest

from eval import local_capacity_campaign as campaign
from eval.external_checkpoints import RUNNER_LOCK_MARKER, _runner_lock


def test_capacity_source_snapshot_binds_the_shared_runner_lock(monkeypatch):
    monkeypatch.setattr(campaign, "_engine_snapshot", lambda: {"engine.py": "a" * 64})
    expected = campaign.sha256_file(Path(campaign.__file__).with_name("external_checkpoints.py"))
    assert campaign._snapshot() == {"engine.py": "a" * 64,
                                    "eval/external_checkpoints.py": expected}


@pytest.fixture
def plan(monkeypatch):
    host = {"hardware": {"physical_ram_bytes": 32 * 1024 ** 3},
            "host_identity_sha256": "a" * 64}
    monkeypatch.setattr(campaign, "host_observation", lambda: copy.deepcopy(host))
    monkeypatch.setattr(campaign, "_snapshot", lambda: {"engine.py": "b" * 64})
    monkeypatch.setattr("eval.engine_capacity._local_model", lambda *_:
                        {"semantic": True, "artifact_sha256": "c" * 64})
    return campaign.make_plan(hardware="shared32", model_dir="model", model_sha256="c" * 64)


def test_local_plan_is_only_the_observed_half_of_primary_matrix(plan):
    campaign.validate_plan(plan)
    assert len(plan["cells"]) == 24
    assert {c["hardware"] for c in plan["cells"]} == {"shared32"}
    assert {c["concurrency"] for c in plan["cells"]} == {1, 4, 16}
    assert plan["repetitions"] == 120
    assert plan["scheduled_operations"] == 240000
    assert plan["scheduled_arrival_hours"] == pytest.approx(29.1666666667)
    assert plan["primary_matrix_complete"] is False


def test_manifest_tampering_and_source_drift_stop_before_execution(plan, monkeypatch, tmp_path):
    changed = copy.deepcopy(plan)
    changed["cells"][0]["size"] = 1234
    with pytest.raises(ValueError, match="changed after freezing"):
        campaign.execute(changed, tmp_path)
    monkeypatch.setattr(campaign, "_snapshot", lambda: {"engine.py": "d" * 64})
    with pytest.raises(ValueError, match="source changed"):
        campaign.execute(plan, tmp_path)


def test_missing_cells_are_partial_not_zero_cost_success(plan, tmp_path):
    result = campaign.summarize(plan, tmp_path)
    assert result["status"] == "PARTIAL"
    assert result["completed_cells"] == result["observed_repetitions"] == 0
    assert all(c["status"] == "PENDING" for c in result["cells"])
    assert result["target_capacity_verified"] is False
    assert result["schema"] == campaign.SCHEMA
    assert result["summary_schema"] == campaign.SUMMARY_SCHEMA
    assert set(result["gate_status"]) == {"integrity", "resource", "latency", "backlog"}
    summary = tmp_path / campaign.SUMMARY_FILENAME
    assert summary.is_file() and summary.with_suffix(".json.sha256").is_file()
    assert summary.with_suffix(".json.sha256").read_text().split()[0] == hashlib.sha256(
        summary.read_bytes()).hexdigest()


def test_unknown_started_cell_is_not_replayed(plan, tmp_path):
    first = campaign.cell_id(plan["cells"][0])
    (tmp_path / (first + ".started.json")).write_text("{}")
    with pytest.raises(ValueError, match="unfinished cell reservation"):
        campaign.execute(plan, tmp_path, runner=lambda *_a, **_k: pytest.fail("replayed"))
    assert (tmp_path / ".runner.lock").read_bytes() == RUNNER_LOCK_MARKER
    with pytest.raises(ValueError, match="unfinished cell reservation"):
        campaign.execute(plan, tmp_path, runner=lambda *_a, **_k: pytest.fail("replayed"))


def test_saved_cell_with_residual_reservation_is_not_accepted(plan, tmp_path, monkeypatch):
    first = campaign.cell_id(plan["cells"][0])
    path = tmp_path / (first + ".json")
    path.write_text("{}")
    (tmp_path / (first + ".started.json")).write_text("{}")
    monkeypatch.setattr(campaign, "summarize", lambda *_a, **_k: {"status": "PARTIAL", "cells": []})
    with pytest.raises(ValueError, match="unfinished reservation"):
        campaign.execute(plan, tmp_path, runner=lambda *_a, **_k: pytest.fail("replayed"))


def test_completed_cell_removes_reservation_after_verified_artifact(plan, tmp_path, monkeypatch):
    monkeypatch.setattr(campaign, "summarize",
                        lambda *_a, **_k: {"status": "PARTIAL", "cells": [], "completed_cells": 0})
    monkeypatch.setattr(campaign, "write_canonical_artifact",
                        lambda _report, path: path.write_text("{}", encoding="utf-8"))
    monkeypatch.setattr(campaign, "_read_verified", lambda _path: {})
    first = campaign.cell_id(plan["cells"][0])
    campaign.execute(plan, tmp_path, max_cells=1,
                     runner=lambda *_a, **_k: {})
    assert (tmp_path / (first + ".json")).is_file()
    assert not (tmp_path / (first + ".started.json")).exists()


def test_legacy_timing_lock_is_not_reclaimed(plan, tmp_path):
    (tmp_path / ".runner.lock").write_text("123")
    with pytest.raises(ValueError, match="manual inspection"):
        campaign.execute(plan, tmp_path, runner=lambda *_a, **_k: pytest.fail("ran"))
    assert (tmp_path / ".runner.lock").read_text() == "123"


def test_timing_lock_prevents_parallel_execution_before_summary_writes(plan, tmp_path):
    with _runner_lock(tmp_path / ".runner.lock"):
        with pytest.raises(ValueError, match="already owns"):
            campaign.execute(plan, tmp_path, runner=lambda *_a, **_k: pytest.fail("ran"))
        assert sorted(path.name for path in tmp_path.iterdir()) == [".runner.lock"]


def test_rehashed_incomplete_or_duplicate_matrix_still_rejected(plan):
    plan["cells"][-1] = plan["cells"][0]
    plan["binding_sha256"] = campaign._digest({k: v for k, v in plan.items()
                                              if k != "binding_sha256"})
    with pytest.raises(ValueError, match="duplicate"):
        campaign.validate_plan(plan)


def test_checksum_required_for_existing_cell(plan, tmp_path):
    path = tmp_path / (campaign.cell_id(plan["cells"][0]) + ".json")
    path.write_text("{}")
    with pytest.raises(ValueError, match="checksum"):
        campaign.summarize(plan, tmp_path)


def test_summary_keeps_the_verified_cell_digest_when_file_changes_during_analysis(plan, tmp_path, monkeypatch):
    config = plan["cells"][0]
    key = campaign.cell_id(config)
    path = tmp_path / (key + ".json")
    report = {"protocol": {"config": config}, "models": {"embedding": plan["model"]},
              "metrics": {"source_before": plan["source"], "source_after": plan["source"],
                          "source_stable": True, "model_stable": True, "wall_latency_ms": 7,
                          "host_identity_sha256": plan["host"]["host_identity_sha256"]}}
    payload = json.dumps(report).encode()
    digest = hashlib.sha256(payload).hexdigest()
    path.write_bytes(payload)
    path.with_suffix(".json.sha256").write_text(digest)
    monkeypatch.setattr(campaign, "validate_report", lambda _report: [])

    def replace_during_statistics(observed, *_args):
        assert observed == report
        path.write_text('{"replacement": true}')
        return {"execution_integrity_pass": True}

    monkeypatch.setattr(campaign, "_repetition_statistics", replace_during_statistics)
    result = campaign.summarize(plan, tmp_path, write_summary=False)
    assert result["cells"][0]["wall_latency_ms"] == 7
    assert result["cells"][0]["sha256"] == digest
    assert result["cell_artifact_sha256"][key] == digest


def test_wrong_ram_profile_rejected(monkeypatch):
    monkeypatch.setattr(campaign, "host_observation", lambda:
                        {"hardware": {"physical_ram_bytes": 32 * 1024 ** 3}})
    with pytest.raises(ValueError, match="RAM"):
        campaign.make_plan(hardware="laptop16", model_dir="unused", model_sha256="c" * 64)


def test_plan_mode_does_not_dispatch(monkeypatch, plan, tmp_path, capsys):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(campaign.canonical_json(plan), encoding="utf-8")
    monkeypatch.setattr(campaign, "execute", lambda *_a, **_k: pytest.fail("dispatched"))
    assert campaign.main(["--manifest", str(manifest)]) == 0
    assert '"dry_run": true' in capsys.readouterr().out
    assert list(tmp_path.iterdir()) == [Path(manifest)]


def test_cell_worker_persists_traceback_coordinates_for_reproducible_failures(
    monkeypatch, tmp_path,
):
    error = tmp_path / "cell.error.json"
    output = tmp_path / "cell.json"

    def fail(*_args, **_kwargs):
        raise RuntimeError("diagnostic-only failure")

    monkeypatch.setattr(campaign, "run_cell", fail)
    with pytest.raises(RuntimeError, match="diagnostic-only"):
        campaign._cell_worker(
            str(output), str(error), campaign.asdict(campaign.Cell()), "model", "sha",
        )
    details = json.loads(error.read_text(encoding="utf-8"))
    assert details["error_class"] == "RuntimeError"
    assert details["reason"] == "diagnostic-only failure"
    assert details["traceback"]
    assert {"file", "line", "function"} <= set(details["traceback"][-1])


def test_local_intervals_use_five_repetitions_and_check_public_rows(monkeypatch):
    cell = campaign.Cell(size=10000, operations=2000, repeats=5, arrival_rate=1, smoke=False)
    repeats = [{"repeat_number": number, "operations": [
        {"number": i, "operation": "recall", "correct": True, "wall_ms": 10.0 * (number + 1)}
        for i in range(2000)]} for number in range(5)]
    report = {"metrics": {"repeats": repeats, "correctness_failures": 0},
              "records": [{"question_id": f"r{r['repeat_number']}-op{row['number']}",
                           "category": "recall", "qa_correct": True, "latency_ms": row["wall_ms"]}
                          for r in repeats for row in r["operations"]]}
    # The full validator's tests cover real resource observations. This fixture
    # isolates the statistical unit and consistency between the two exports.
    monkeypatch.setattr(campaign, "_cell_identity", lambda *a, **k: ({"observation_contract": True}, {}))

    def validated(repeat, *_args, **_kwargs):
        mean = repeat["operations"][0]["wall_ms"]
        return {"failures": 0, "status": "complete", "measured": 2000,
                "clean_worker_teardown": True, "durable_workers_observed": True,
                "received_operations_per_second": 1000 / mean,
                "operations": {"recall": {"mean_wall_ms": mean, "measured": 2000, "scheduled": 2000}}}

    monkeypatch.setattr(campaign, "_validate_repeat", validated)
    observed = campaign._repetition_statistics(report, cell, set())
    interval = observed["operations"]["recall"]["mean_wall_ms_interval"]
    assert interval["units"] == 5
    assert interval["point"] == 30
    assert interval["low"] < interval["point"] < interval["high"]
    assert observed["execution_integrity_pass"] is True
    report["records"][0]["latency_ms"] += 1
    with pytest.raises(ValueError, match="disagrees"):
        campaign._repetition_statistics(report, cell, set())


def test_gate_statuses_keep_resource_latency_and_backlog_separate(monkeypatch):
    cell = campaign.Cell(size=100000, operations=2000, repeats=5, arrival_rate=16,
                         hardware="shared32", concurrency=16, smoke=False)
    summaries = [{
        "status": "complete", "measured": 2000, "failures": 0,
        "clean_worker_teardown": True, "durable_workers_observed": True,
        "all_lifecycle_phases_observed": True, "memory_peak": 100,
        "backlog_assessment_available": True, "no_sustained_backlog_growth_observed": True,
        "operations": {"recall": {"wall_latency_ms": {"p95": 1500}}},
    } for _ in range(5)]
    gates = campaign._repetition_gate_statuses(
        summaries, cell, {"hardware": {"physical_ram_bytes": 32 * 1024 ** 3}})
    assert gates == {"integrity": "PASS", "resource": "PASS",
                     "latency": "PASS", "backlog": "PASS"}
    summaries[0]["operations"]["recall"]["wall_latency_ms"]["p95"] = 2500
    assert campaign._repetition_gate_statuses(
        summaries, cell, {"hardware": {"physical_ram_bytes": 32 * 1024 ** 3}})["latency"] == "FAIL"
