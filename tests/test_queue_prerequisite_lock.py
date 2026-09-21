"""Prerequisite verification must retain producer ownership throughout the read."""

import pytest
from pathlib import Path

from eval import local_benchmark_queue as queue
from eval.external_checkpoints import RunnerLockBusy, _runner_lock


@pytest.mark.parametrize("wait", [False, "artifact.json", [], {},
    {"artifact": "artifact.json"}, {"producer_lock": "producer.lock"},
    {"artifact": "artifact.json", "producer_lock": "producer.lock", "extra": 1}])
def test_malformed_prerequisite_cannot_bypass_manifest_validation(tmp_path, monkeypatch, wait):
    monkeypatch.setattr(queue, "ROOT", tmp_path)
    plan = {"schema": queue.SCHEMA, "wait_for": wait, "source": {}, "inputs": {},
            "jobs": [{"id": "smoke", "module": "eval.engine_capacity", "args": []}]}
    plan["binding_sha256"] = queue.digest(plan)
    with pytest.raises(ValueError, match="prerequisite requires"):
        queue.validate(plan, live=False)


@pytest.mark.parametrize("field", ["artifact", "producer_lock"])
@pytest.mark.parametrize("name", ["../outside", "/outside", "", None, 12])
def test_prerequisite_paths_must_remain_in_repository(tmp_path, monkeypatch, field, name):
    monkeypatch.setattr(queue, "ROOT", tmp_path)
    wait = {"artifact": "artifact.json", "producer_lock": "producer.lock", field: name}
    plan = {"schema": queue.SCHEMA, "wait_for": wait, "source": {}, "inputs": {},
            "jobs": [{"id": "smoke", "module": "eval.engine_capacity", "args": []}]}
    plan["binding_sha256"] = queue.digest(plan)
    with pytest.raises(ValueError, match="repository"):
        queue.execute(plan, tmp_path / "results", runner=lambda *a, **k: pytest.fail("dispatched"))
    assert not (tmp_path / "results").exists()


def test_prerequisite_preserves_the_named_lock_path_for_alias_rejection(tmp_path, monkeypatch):
    monkeypatch.setattr(queue, "ROOT", tmp_path)
    marker = tmp_path / "producer.lock"
    target = tmp_path / "real.lock"
    original_resolve = Path.resolve

    def resolve(path, *args, **kwargs):
        return target if path == marker else original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    artifact, actual_lock = queue._prerequisite_paths({
        "artifact": "artifact.json", "producer_lock": "producer.lock"})
    assert artifact == tmp_path / "artifact.json"
    assert actual_lock == marker


def test_prerequisite_verification_holds_the_persistent_producer_lock(tmp_path, monkeypatch):
    marker = tmp_path / ".runner.lock"
    artifact = tmp_path / "artifact.json"
    artifact.write_text("{}")
    with _runner_lock(marker):
        pass
    before = marker.read_bytes()
    calls = []

    def verify(path):
        calls.append(path)
        with pytest.raises(RunnerLockBusy):
            with _runner_lock(marker, create=False):
                pytest.fail("producer could restart during verification")
        return {}

    monkeypatch.setattr(queue, "_verified_artifact", verify)
    assert queue._prerequisite_ready(artifact, marker)
    assert calls == [artifact]
    assert marker.read_bytes() == before
    with _runner_lock(marker, create=False):
        pass


def test_artifact_read_failure_is_not_mistaken_for_an_absent_producer_lock(tmp_path, monkeypatch):
    marker = tmp_path / ".runner.lock"
    artifact = tmp_path / "artifact.json"
    artifact.write_text("{}")
    with _runner_lock(marker):
        pass
    calls = []

    def verify(path):
        calls.append(path)
        raise FileNotFoundError("artifact sidecar disappeared")

    monkeypatch.setattr(queue, "_verified_artifact", verify)
    with pytest.raises(FileNotFoundError, match="sidecar disappeared"):
        queue._prerequisite_ready(artifact, marker)
    assert calls == [artifact]
    with _runner_lock(marker, create=False):
        pass


def test_marker_disappearance_after_acquisition_is_not_legacy_completion(tmp_path, monkeypatch):
    marker = tmp_path / ".runner.lock"
    artifact = tmp_path / "artifact.json"
    artifact.write_text("{}")
    with _runner_lock(marker):
        pass
    original_lstat = Path.lstat
    inspections = []

    def disappear(path, *args, **kwargs):
        if path == marker:
            inspections.append(path)
            if len(inspections) == 2:
                raise FileNotFoundError("marker removed after acquisition")
        return original_lstat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", disappear)
    monkeypatch.setattr(queue, "_verified_artifact", lambda _: pytest.fail("verified changed lock"))
    with pytest.raises(ValueError, match="unsafe or changed"):
        queue._prerequisite_ready(artifact, marker)
    assert len(inspections) == 2
