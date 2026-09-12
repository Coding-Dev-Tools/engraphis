import importlib.util
import hashlib
import json
import queue

import pytest

from eval.benchmark import canonical_json, sha256_file, validate_report, write_canonical_artifact
from eval.engine_capacity import (
    Cell, RESOURCE_PHASES, _LifecycleObserver, _backlog_assessment, _local_model,
    _identity_digest, _repeat, acceptance_policy, host_observation, main, operation_plan,
    protocol, run_cell, validate_reference_hosts,
)


def test_protocol_declares_exact_capacity_matrix_and_sampling():
    plan = protocol()
    assert len(plan["primary_cells"]) == plan["primary_cell_count"] == 48
    assert plan["repeats"] == 5 and plan["operations_per_repeat"] == 2000
    assert plan["mixed_percent"] == {"recall": 80, "remember": 15, "correct": 4, "erase": 1}
    assert plan["target_capacity_verified"] is False
    assert plan["sqlite_durability"] == {"policy": "durable", "journal_mode": "wal", "synchronous": "FULL"}


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
    assert all(item["sqlite_durability"]["synchronous"] == "FULL" for item in repeat["startup"])
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
        resources = repeat["resource_observations"]
        assert resources["started_before_seeding"] is True
        assert resources["sampler_thread_stopped"] is True
        assert all(resources["phases"][phase]["sample_count"] >= 2 for phase in RESOURCE_PHASES)
    assert repeat["backlog_assessment"]["available"] is False
    assert repeat["backlog_assessment"]["general_capacity_proof"] is False
    assert repeat["backlog_observations"]["series"][-1]["scheduled_outstanding"] == 0
    assert write_canonical_artifact(report, tmp_path / "capacity.json")["sha256"]


class _Clock:
    value = 0.0

    def __call__(self):
        return self.value


def test_lifecycle_rss_keeps_seed_and_startup_peaks_and_unknown_samples():
    clock = _Clock()
    rss = [1000]
    observer = _LifecycleObserver(Cell(), clock=clock, rss_reader=lambda pids: rss[0])
    observer.sample()
    clock.value = 1
    observer.set_phase("startup")
    rss[0] = 900
    observer.sample()
    clock.value = 2
    rss[0] = 200
    observer.set_phase("workload")
    clock.value = 3
    rss[0] = None
    observer.sample()
    observer.set_phase("teardown")
    observer.close()
    report = observer.report()
    phases = report["resource_observations"]["phases"]
    assert report["observed_process_tree_peak_rss_bytes"] == 1000
    assert phases["seeding"]["observed_peak_rss_bytes"] == 1000
    assert phases["workload"]["observed_peak_rss_bytes"] == 200
    assert phases["teardown"]["observed_peak_rss_bytes"] is None
    assert phases["teardown"]["sample_count"] == 0
    assert phases["teardown"]["unavailable_samples"] == phases["teardown"]["sampling_attempts"]
    assert report["memory_samples"] == sum(row["sample_count"] for row in phases.values())
    assert phases["workload"]["max_sample_gap_s"] == 1


def test_outstanding_series_includes_dispatch_and_freezes_offering_after_failure():
    clock = _Clock()
    observer = _LifecycleObserver(Cell(arrival_rate=2), clock=clock, rss_reader=lambda pids: None)
    observer.set_phase("workload")
    observer.begin_load(0)
    observer.record_submission()
    clock.value = 2
    observer.record_submission()
    observer.record_receipt()
    observer.sample()
    series = observer.report()["backlog_observations"]["series"]
    assert series[-1] == {
        "elapsed_s": 2, "phase": "workload", "scheduled_due": 5, "submitted": 2,
        "received": 1, "scheduled_outstanding": 4, "dispatch_pending": 3, "submitted_unreceived": 1,
    }
    observer.end_load()
    clock.value = 10
    observer.set_phase("teardown")
    observer.close()
    report = observer.report()
    assert report["backlog_observations"]["offering_observed_until_s"] == 2
    assert report["backlog_observations"]["series"][-1]["scheduled_due"] == 5
    assert report["observed_process_tree_peak_rss_bytes"] is None
    assert report["memory_samples"] == 0


@pytest.mark.parametrize("growing", [False, True])
def test_backlog_growth_is_finite_predeclared_observation_and_excludes_drain(growing):
    cell = Cell(arrival_rate=4)
    observations = {"offering_observed_until_s": 35, "series": [
        {"elapsed_s": second, "scheduled_outstanding": second * 2 if growing else 3}
        for second in range(25)
    ] + [{"elapsed_s": 30, "scheduled_outstanding": 0}]}
    assessment = _backlog_assessment(cell, observations, execution_complete=not growing)
    assert assessment["available"] is True
    assert assessment["sustained_growth_observed"] is growing
    assert assessment["general_capacity_proof"] is False
    assert assessment["assessment_duration_s"] == 24.75
    assert assessment["active_samples"] == 25
    assert assessment["execution_complete"] is not growing
    assert assessment["observed_slope_operations_per_second"] == (2 if growing else 0)


@pytest.mark.parametrize("rate,until,series,reason", [
    (0, 20, [], "burst"),
    (4, None, [], "never started"),
    (4, 9, [], "ten seconds"),
    (4, 20, [{"elapsed_s": 0, "scheduled_outstanding": 1}] * 20, "insufficient sampling"),
])
def test_insufficient_backlog_evidence_is_unknown(rate, until, series, reason):
    result = _backlog_assessment(Cell(arrival_rate=rate),
        {"offering_observed_until_s": until, "series": series}, execution_complete=False)
    assert result["available"] is False
    assert result["sustained_growth_observed"] is None
    assert reason in result["reason"]


def test_seed_failure_retains_full_denominator_and_resource_observations(monkeypatch):
    observers = []

    def build_observer(cell):
        observer = _LifecycleObserver(cell, rss_reader=lambda pids: 1000)
        observers.append(observer)
        return observer

    def fail_seed(*args):
        assert observers[0].sampling_started
        assert observers[0].phases["seeding"]["sample_count"] >= 1
        raise RuntimeError("raw source text must not enter the artifact")

    monkeypatch.setattr("eval.engine_capacity._LifecycleObserver", build_observer)
    monkeypatch.setattr("eval.engine_capacity._seed", fail_seed)
    repeat = _repeat(Cell(), None)
    assert repeat["status"] == "startup_failed"
    assert len(repeat["operations"]) == 100
    assert all(row["correct"] is False and "wall_ms" not in row for row in repeat["operations"])
    assert repeat["resource_observations"]["sampler_thread_stopped"]
    assert repeat["resource_observations"]["phases"]["teardown"]["sample_count"] >= 1
    assert repeat["lifecycle_errors"] == [{"phase": "seeding", "error_type": "RuntimeError"}]
    assert "raw source" not in json.dumps(repeat)


def test_startup_deadline_retains_missing_operations_and_teardown_observation():
    repeat = _repeat(Cell(timeout_s=1e-9), None)
    assert repeat["status"] == "startup_failed"
    assert {"phase": "startup", "error_type": "TimeoutError"} in repeat["lifecycle_errors"]
    assert sum(not row["correct"] for row in repeat["operations"]) == 100
    assert repeat["resource_observations"]["phases"]["startup"]["sampling_attempts"] >= 1
    assert repeat["resource_observations"]["phases"]["teardown"]["sampling_attempts"] >= 1


def test_workload_timeout_keeps_outstanding_work_and_failed_denominator(monkeypatch):
    class DisposableQueue(queue.Queue):
        def cancel_join_thread(self):
            pass

        def close(self):
            pass

    class Worker:
        pid = 999999999
        exitcode = None
        alive = True

        def start(self):
            pass

        def join(self, timeout):
            pass

        def is_alive(self):
            return self.alive

        def terminate(self):
            self.alive, self.exitcode = False, -15

    class Context:
        def __init__(self):
            self.queues = []

        def Queue(self):
            result = DisposableQueue()
            if self.queues:
                result.put({"kind": "ready", "pid": Worker.pid})
            self.queues.append(result)
            return result

        def Process(self, **kwargs):
            return Worker()

    monkeypatch.setattr("eval.engine_capacity.multiprocessing.get_context", lambda method: Context())
    monkeypatch.setattr("eval.engine_capacity._seed", lambda *args: ([{"index": i} for i in range(16)], 1))
    repeat = _repeat(Cell(timeout_s=0.02), None)
    assert repeat["status"] == "timeout"
    assert len(repeat["operations"]) == 100
    assert all(row["correct"] is False for row in repeat["operations"])
    assert repeat["backlog_observations"]["series"][-1]["scheduled_outstanding"] == 100
    assert repeat["resource_observations"]["sampler_thread_stopped"]


def test_cli_fails_on_teardown_failure_even_when_operation_checks_pass(monkeypatch, capsys):
    monkeypatch.setattr("eval.engine_capacity.run_cell", lambda *args, **kwargs: {"metrics": {
        "correctness_failures": 0, "source_stable": True, "model_stable": True,
        "repeats": [{"status": "worker_error"}],
    }})
    assert main(["--smoke"]) == 1
    assert json.loads(capsys.readouterr().out)["metrics"]["correctness_failures"] == 0


def test_real_bounded_load_observations_are_accepted_as_observations_only():
    from eval.capacity_matrix import _backlog_summary, _resource_summary

    cell = Cell(arrival_rate=1000, concurrency=4)
    repeat = _repeat(cell, None)
    assert repeat["status"] == "complete"
    measured = sum("wall_ms" in row for row in repeat["operations"])
    assert measured == 100
    if importlib.util.find_spec("psutil"):
        assert _resource_summary(repeat)["all_lifecycle_phases_observed"] is True
    backlog = _backlog_summary(repeat, cell, measured)
    assert backlog["backlog_assessment_available"] is False
    assert backlog["no_sustained_backlog_growth_observed"] is False


def test_host_inventory_is_read_only_and_missing_machine_identity_stays_unknown(monkeypatch, capsys):
    monkeypatch.setattr("eval.engine_capacity._seed", lambda *args: pytest.fail("opened storage"))
    assert main(["--host-identity"]) == 0
    observation = json.loads(capsys.readouterr().out)
    assert observation["hardware_sha256"] == _identity_digest(observation["hardware"])
    assert observation["policy_sha256"] == _identity_digest(acceptance_policy())
    monkeypatch.setattr("eval.engine_capacity.platform.node", lambda: "")
    assert host_observation()["host_identity_sha256"] is None


def test_reference_hosts_bind_current_policy_and_two_distinct_host_identities():
    manifest = {"schema": "engraphis-capacity-reference-hosts/v1",
                "policy_sha256": _identity_digest(acceptance_policy()), "hosts": {
                    "laptop16": {"host_identity_sha256": "a" * 64, "hardware_sha256": "b" * 64},
                    "shared32": {"host_identity_sha256": "c" * 64, "hardware_sha256": "d" * 64},
                }}
    validate_reference_hosts(manifest)
    manifest["hosts"]["shared32"]["host_identity_sha256"] = "a" * 64
    with pytest.raises(ValueError, match="distinct hosts"):
        validate_reference_hosts(manifest)
    manifest["policy_sha256"] = "e" * 64
    with pytest.raises(ValueError, match="current acceptance policy"):
        validate_reference_hosts(manifest)


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
