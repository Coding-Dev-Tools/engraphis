"""A checklist label cannot bypass missing, stale or contradictory evidence."""
import hashlib
import json
import subprocess

import pytest

from scripts.check_release_readiness import (
    COMPONENTS, LEADERSHIP_GATES, RECEIPT_SCHEMA, RELEASE_GATES,
    candidate_id, main, new_ledger, read_json, validate,
)


@pytest.fixture
def ledger(tmp_path):
    components = {}
    for name in COMPONENTS:
        path = tmp_path / (name + ".artifact")
        path.write_bytes(b"Synthetic component bytes.")
        components[name] = {"commit": "a" * 40, "artifact_path": path.name,
                            "artifact_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    return new_ledger(components)


def pass_gate(ledger, root, name):
    gate = next(gate for gate in ledger["gates"] if gate["id"] == name)
    artifact = root / (name + ".execution.json")
    artifact.write_text('{"exit_code":0,"fixture_only":true}\n', encoding="utf-8")
    receipt = {"schema": RECEIPT_SCHEMA, "gate_id": name,
               "candidate_id": ledger["candidate_id"], "passed": True,
               "observed_at": "2026-09-11T12:00:00+00:00",
               "evidence_kind": "automated", "summary": "Disposable fixture passed.",
               "artifacts": [{"path": artifact.name,
                              "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()}]}
    path = root / (name + ".json")
    path.write_text(json.dumps(receipt), encoding="utf-8")
    gate.update(status="PASS", blockers=[], evidence=[
        {"path": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}])
    return gate, receipt, path


def rewrite_receipt(gate, receipt, path):
    path.write_text(json.dumps(receipt), encoding="utf-8")
    gate["evidence"][0]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()


def test_new_ledger_is_valid_but_never_ready(ledger, tmp_path):
    result = validate(ledger, tmp_path)
    assert result["valid"]
    assert result["release_gate_status"] == "UNVERIFIED"
    assert result["leadership_gate_status"] == "UNVERIFIED"
    assert result["publication_authorized"] is False


def test_release_and_leadership_are_separate(ledger, tmp_path):
    for name in RELEASE_GATES:
        pass_gate(ledger, tmp_path, name)
    result = validate(ledger, tmp_path)
    assert result["release_gate_status"] == "PASS"
    assert result["leadership_gate_status"] == "UNVERIFIED"
    for name in LEADERSHIP_GATES:
        pass_gate(ledger, tmp_path, name)
    result = validate(ledger, tmp_path)
    assert result["leadership_gate_status"] == "PASS"
    assert result["execution_authenticity_verified"] is False
    assert result["publication_authorized"] is False


@pytest.mark.parametrize("change", ["missing", "duplicate", "unknown"])
def test_gate_inventory_cannot_be_reduced(ledger, tmp_path, change):
    if change == "missing":
        ledger["gates"].pop()
    elif change == "duplicate":
        ledger["gates"].append(ledger["gates"][0])
    else:
        ledger["gates"][0]["id"] = "different"
    assert not validate(ledger, tmp_path)["valid"]


@pytest.mark.parametrize("field,value", [
    ("candidate_id", "c" * 64), ("gate_id", "capacity"), ("passed", False),
    ("passed", 1), ("observed_at", "2026-09-11T12:00:00"),
    ("evidence_kind", "inferred"), ("schema", "unknown"),
])
def test_pass_rejects_incompatible_receipt(ledger, tmp_path, field, value):
    gate, receipt, path = pass_gate(ledger, tmp_path, "automated")
    receipt[field] = value
    rewrite_receipt(gate, receipt, path)
    assert not validate(ledger, tmp_path)["valid"]


def test_hash_mismatch_and_missing_evidence_fail(ledger, tmp_path):
    gate, _, path = pass_gate(ledger, tmp_path, "automated")
    path.write_text("{}", encoding="utf-8")
    assert not validate(ledger, tmp_path)["valid"]
    gate["evidence"] = []
    assert not validate(ledger, tmp_path)["valid"]


def test_failed_gate_remains_fail_in_summary(ledger, tmp_path):
    ledger["gates"][0]["status"] = "FAIL"
    result = validate(ledger, tmp_path)
    assert result["valid"]
    assert result["release_gate_status"] == "FAIL"


def test_receipt_requires_unchanged_execution_artifacts(ledger, tmp_path):
    gate, receipt, path = pass_gate(ledger, tmp_path, "automated")
    (tmp_path / "automated.execution.json").write_text("tampered", encoding="utf-8")
    assert not validate(ledger, tmp_path)["valid"]
    receipt["artifacts"] = []
    rewrite_receipt(gate, receipt, path)
    assert not validate(ledger, tmp_path)["valid"]


@pytest.mark.parametrize("relative", ["../receipt.json", "/receipt.json", "C:/receipt.json",
                                    "nested\\receipt.json"])
def test_evidence_paths_are_confined(ledger, tmp_path, relative):
    gate, _, _ = pass_gate(ledger, tmp_path, "automated")
    gate["evidence"][0]["path"] = relative
    assert not validate(ledger, tmp_path)["valid"]


def test_pass_cannot_ignore_blocked_dependency_or_cycle(ledger, tmp_path):
    gate, _, _ = pass_gate(ledger, tmp_path, "automated")
    gate["depends_on"] = ["memory_integrity"]
    assert not validate(ledger, tmp_path)["valid"]
    dependency, _, _ = pass_gate(ledger, tmp_path, "memory_integrity")
    dependency["depends_on"] = ["automated"]
    assert not validate(ledger, tmp_path)["valid"]


def test_candidate_rebinding_invalidates_prior_receipts(ledger, tmp_path):
    pass_gate(ledger, tmp_path, "automated")
    ledger["components"]["engine"]["artifact_sha256"] = "d" * 64
    ledger["candidate_id"] = candidate_id(ledger["components"])
    assert not validate(ledger, tmp_path)["valid"]


def test_missing_artifact_keeps_complete_checklist_unverified(ledger, tmp_path):
    ledger["components"]["cloud"]["artifact_sha256"] = None
    ledger["candidate_id"] = candidate_id(ledger["components"])
    for name in RELEASE_GATES:
        pass_gate(ledger, tmp_path, name)
    result = validate(ledger, tmp_path)
    assert result["valid"]
    assert result["release_gate_status"] == "UNVERIFIED"


def test_component_bytes_must_match_selected_identity(ledger, tmp_path):
    (tmp_path / "engine.artifact").write_bytes(b"different artifact")
    assert not validate(ledger, tmp_path)["valid"]


@pytest.mark.parametrize("report", [{"exit_code": 1}, {"status": "FAIL"}, {"passed": False},
                                   {"exit_code": 0, "valid": False}])
def test_failure_in_execution_cannot_support_pass(ledger, tmp_path, report):
    gate, receipt, path = pass_gate(ledger, tmp_path, "automated")
    artifact = tmp_path / "automated.execution.json"
    artifact.write_text(json.dumps(report), encoding="utf-8")
    receipt["artifacts"][0]["sha256"] = hashlib.sha256(artifact.read_bytes()).hexdigest()
    rewrite_receipt(gate, receipt, path)
    assert not validate(ledger, tmp_path)["valid"]


def test_false_receipt_cannot_be_relabelled_unverified(ledger, tmp_path):
    gate, receipt, path = pass_gate(ledger, tmp_path, "automated")
    gate.update(status="UNVERIFIED", blockers=["Still pending"])
    receipt["passed"] = False
    rewrite_receipt(gate, receipt, path)
    assert not validate(ledger, tmp_path)["valid"]


def test_eval_reporting_success_requires_selected_boolean(ledger, tmp_path):
    gate, receipt, path = pass_gate(ledger, tmp_path, "automated")
    artifact = tmp_path / "automated.execution.json"
    artifact.write_text('{"exit_code":0,"release_gates":{"planner":{"default_eligible":false}}}',
                        encoding="utf-8")
    reference = receipt["artifacts"][0]
    reference["sha256"] = hashlib.sha256(artifact.read_bytes()).hexdigest()
    rewrite_receipt(gate, receipt, path)
    assert not validate(ledger, tmp_path)["valid"]
    reference["planned_recall_gate"] = {"candidate": "planner", "level": "default"}
    rewrite_receipt(gate, receipt, path)
    assert not validate(ledger, tmp_path)["valid"]
    reference["required_true"] = ["/release_gates/planner/default_eligible"]
    rewrite_receipt(gate, receipt, path)
    assert not validate(ledger, tmp_path)["valid"]


@pytest.mark.parametrize("role", ["execution", "attachment"])
@pytest.mark.parametrize("change", ["fixture", "false_gate", "missing_gate", "incomplete", "wrong_count"])
def test_capacity_reporting_or_fixture_success_cannot_support_release(ledger, tmp_path, role, change):
    gate, receipt, path = pass_gate(ledger, tmp_path, "capacity")
    metrics = {
        "fixture": False, "protocol_observations_pass": True,
        "capacity_acceptance_pass": True, "responsiveness_gate_pass": True,
        "resource_stability_gate_pass": True, "all_measurements_complete": True,
        "cell_count": 48, "repetition_count": 240, "scheduled_operations": 480000,
    }
    if change == "fixture":
        metrics["fixture"] = True
    elif change == "false_gate":
        metrics["capacity_acceptance_pass"] = False
    elif change == "missing_gate":
        metrics.pop("capacity_acceptance_pass")
    elif change == "incomplete":
        metrics["all_measurements_complete"] = False
    else:
        metrics["scheduled_operations"] = 479999
    artifact = tmp_path / "capacity.execution.json"
    artifact.write_text(json.dumps({"exit_code": 0,
        "suite": {"name": "engraphis-capacity-matrix/v1"}, "metrics": metrics}), encoding="utf-8")
    reference = receipt["artifacts"][0]
    reference.update(sha256=hashlib.sha256(artifact.read_bytes()).hexdigest(), role=role,
                     required_true=["/metrics/protocol_observations_pass"])
    if role == "attachment":
        receipt["evidence_kind"] = "attended"
    rewrite_receipt(gate, receipt, path)
    assert not validate(ledger, tmp_path)["valid"]


@pytest.mark.parametrize("raw", ['{"a":1,"a":2}', '{"a":NaN}', '{"a":Infinity}', '{"a":1e9999}'])
def test_ambiguous_json_is_rejected(tmp_path, raw):
    path = tmp_path / "bad.json"
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(ValueError):
        read_json(path)


def test_strict_cli_requires_clean_matching_engine(ledger, tmp_path):
    repository = tmp_path / "repo"
    repository.mkdir()
    subprocess.run(["git", "init", "--quiet", str(repository)], check=True)
    subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                    "commit", "--quiet", "--allow-empty", "-m", "fixture"],
                   cwd=repository, check=True)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip()
    ledger["components"]["engine"]["commit"] = commit
    ledger["candidate_id"] = candidate_id(ledger["components"])
    for name in RELEASE_GATES:
        pass_gate(ledger, tmp_path, name)
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps(ledger), encoding="utf-8")
    args = ["--ledger", str(path), "--evidence-root", str(tmp_path), "--require-release"]
    assert main(args) == 1
    args += ["--engine-root", str(repository)]
    assert main(args) == 0
    (repository / "drift.txt").write_text("changed", encoding="utf-8")
    assert main(args) == 1


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
def test_hidden_git_changes_cannot_qualify_candidate(ledger, tmp_path, flag):
    repository = tmp_path / "hidden-repo"
    repository.mkdir()
    subprocess.run(["git", "init", "--quiet", str(repository)], check=True)
    source = repository / "engine.py"
    source.write_text("original", encoding="utf-8")
    subprocess.run(["git", "add", "engine.py"], cwd=repository, check=True)
    subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                    "commit", "--quiet", "-m", "fixture"], cwd=repository, check=True)
    ledger["components"]["engine"]["commit"] = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip()
    ledger["candidate_id"] = candidate_id(ledger["components"])
    subprocess.run(["git", "update-index", flag, "engine.py"], cwd=repository, check=True)
    source.write_text("changed", encoding="utf-8")
    result = validate(ledger, tmp_path, engine_root=repository)
    assert not result["engine_checkout_verified"]
    assert not result["valid"]


def test_receipt_replacement_is_not_parsed_as_verified_bytes(ledger, tmp_path, monkeypatch):
    from scripts import check_release_readiness as checker
    gate, receipt, path = pass_gate(ledger, tmp_path, "automated")
    original = checker._decode_json

    def replace_after_read(payload):
        if b"gate_id" in payload:
            path.write_text('{"passed":true}', encoding="utf-8")
        return original(payload)

    monkeypatch.setattr(checker, "_decode_json", replace_after_read)
    result = validate(ledger, tmp_path)
    assert result["valid"]  # The original complete, hash-verified snapshot was parsed.
    assert not validate(ledger, tmp_path)["valid"]  # Subsequent drift fails verification.
