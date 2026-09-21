from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path

import pytest

from eval.campaign_ledger import BudgetApproval
from eval import campaign_continuation as cc


def _manifest() -> dict:
    ids = ["atlas-north:long_documents"] + [
        f"atlas-north:category_{index}" for index in range(1, 10)
    ]
    return {
        "binding_sha256": "a" * 64,
        "campaign_id": "engraphis-test",
        "model": "gpt-5.6-luna",
        "reasoning_effort": "medium",
        "source": {},
        "stages": {
            "development_pilot": {
                "scenario_ids": ids,
                "arms": ["no_memory", "full_history", "lexical", "dense", "hybrid"],
                "token_budgets": [512, 1500, 4096],
                "repetitions": 1,
            }
        },
    }


def _excluded(manifest: dict) -> tuple[dict, ...]:
    unsigned = {
        "schema": "engraphis-campaign-eligibility/v1",
        "parent_campaign_sha256": manifest["binding_sha256"],
        "stage": "development_pilot",
        "origin": "implementation_team",
        "retrospective": True,
        "audit_artifact_sha256": "d" * 64,
        "exclusions": [
            {**cell, "reason": cc.INVALID_FIXTURE_REASON}
            for cell in cc._cells(manifest, "development_pilot")
            if cell["scenario_id"] == "atlas-north:long_documents"
        ],
    }
    artifact = {**unsigned, "binding_sha256": cc._digest(unsigned)}
    return cc.validate_eligibility(manifest, artifact)


def _row(cell: dict, status: str = "complete") -> dict:
    return {
        **cell, "status": status, "task_success": True if status == "complete" else None,
        "critical_violations": [], "context_tokens": 0,
    }


def _plan(tmp_path: Path, cells: tuple[dict, ...]) -> cc.ContinuationPlan:
    parent_results = tmp_path / "parent"
    stage = parent_results / "development_pilot"
    stage.mkdir(parents=True)
    ledger = parent_results / "spending" / "development_pilot.jsonl"
    ledger.parent.mkdir()
    ledger.write_text("immutable", encoding="utf-8")
    parent_manifest_path = tmp_path / "parent-manifest.json"
    parent_manifest_path.write_text("{}", encoding="utf-8")
    public_path = tmp_path / "parent-public.json"
    public_path.write_text("{}", encoding="utf-8")
    parent_manifest = {
        "binding_sha256": "a" * 64, "campaign_id": "engraphis-test",
        "model": "gpt-5.6-luna", "reasoning_effort": "medium", "source": {}, "repository_revision": "test",
    }
    child_manifest = {
        "binding_sha256": "b" * 64, "campaign_id": "engraphis-test-continuation",
        "model": "gpt-5.6-luna", "reasoning_effort": "medium", "source": {},
    }
    return cc.ContinuationPlan(
        parent_manifest_path, tmp_path / "companion.json", tmp_path / "eligibility.json",
        public_path, tmp_path / "approval.json", parent_results, tmp_path / "child",
        "development_pilot", parent_manifest, {}, {"binding_sha256": "c" * 64},
        child_manifest, {}, cc._checkpoint_digest(stage), ledger, cc.sha256_file(ledger) if hasattr(cc, "sha256_file") else hashlib.sha256(ledger.read_bytes()).hexdigest(),
        {"by_status": {"uncertain": 0}}, BudgetApproval.create(max_calls=298, max_cost_micros=3923508),
        BudgetApproval.create(max_calls=180, max_cost_micros=2359440), cells, (), cells,
    )


def test_exclusion_mask_leaves_exactly_90_eligible_cells():
    manifest = _manifest()
    excluded = _excluded(manifest)
    all_cells = cc._cells(manifest, "development_pilot")
    existing = {
        cc._cell_key(cell)
        for cell in all_cells
        if cell["scenario_id"] in {
            "atlas-north:category_1", "atlas-north:category_2", "atlas-north:category_3"
        }
    }
    existing.update(cc._cell_key(cell) for cell in excluded[:14])
    eligible, missing_excluded = cc.eligible_missing_cells(
        manifest, "development_pilot", existing, {cc._cell_key(cell) for cell in excluded}
    )
    assert len(all_cells) == 150
    assert len(eligible) == 90
    assert len(missing_excluded) == 1
    assert missing_excluded[0]["scenario_id"] == "atlas-north:long_documents"


def test_budget_carry_deducts_all_parent_reservations():
    parent = BudgetApproval.create(max_calls=298, max_cost_micros=3923508)
    child = cc.derive_child_approval(
        parent, parent_calls=82, parent_reserved_cost_micros=1074856
    )
    assert child.max_calls == 180
    assert child.max_cost_micros == 2359440
    assert child.max_calls <= parent.max_calls - 82
    assert child.max_cost_micros <= parent.max_cost_micros - 1074856
    with pytest.raises(cc.ContinuationError, match="remaining allowance"):
        cc.derive_child_approval(parent, parent_calls=119, parent_reserved_cost_micros=1074856)


def test_duplicate_checkpoint_is_skipped(tmp_path):
    cell1 = {"scenario_id": "x:a", "arm": "hybrid", "token_budget": 512, "repetition": 0}
    cell2 = {"scenario_id": "x:b", "arm": "hybrid", "token_budget": 512, "repetition": 0}
    plan = _plan(tmp_path, (cell1, cell2))
    stage = plan.child_results / plan.stage_name
    stage.mkdir(parents=True)
    prior = _row(cell1)
    cc._save_new(stage / f"{cc.campaign.digest(cell1)}.json", {
        "binding_sha256": plan.child_manifest["binding_sha256"], "cell": cell1,
        "row": prior, "row_sha256": cc._digest(prior),
    })
    seen = []
    report = cc.run_continuation(
        plan, corpus=None, client=object(),
        attempt_runner=lambda manifest, stage_name, cell, corpus, client: seen.append(cell) or _row(cell),
        enforce_source=False,
    )
    assert seen == [cell2]
    assert report["metrics"]["valid_missing_attempts"] == 0


def test_checkpoint_digest_and_validation_use_one_byte_snapshot(monkeypatch, tmp_path):
    cell = {"scenario_id": "x:a", "arm": "hybrid", "token_budget": 512, "repetition": 0}
    directory = tmp_path / "checkpoints"
    directory.mkdir()
    row = _row(cell)
    checkpoint = {
        "binding_sha256": "a" * 64,
        "cell": cell,
        "row": row,
        "row_sha256": cc._digest(row),
    }
    path = directory / f"{cc.campaign.digest(cell)}.json"
    cc._save_new(path, checkpoint)
    original_payload = path.read_bytes()
    original_read_bytes = Path.read_bytes
    mutation_seen = False

    def replace_after_snapshot(candidate):
        nonlocal mutation_seen
        payload = original_read_bytes(candidate)
        if candidate == path and not mutation_seen:
            mutation_seen = True
            candidate.write_text("{}\n", encoding="utf-8")
        return payload

    monkeypatch.setattr(Path, "read_bytes", replace_after_snapshot)
    monkeypatch.setattr(cc.campaign, "validate_row", lambda value, expected: None)

    rows, digest = cc._load_checkpoints(
        directory,
        "a" * 64,
        {cc._cell_key(cell): cell},
        expected_count=1,
    )

    assert mutation_seen
    assert rows[cc._cell_key(cell)] == row
    assert digest == cc._digest([{
        "name": path.name, "sha256": hashlib.sha256(original_payload).hexdigest()
    }])


def test_combined_report_rejects_parent_replacement_during_envelope(monkeypatch, tmp_path):
    cell = {"scenario_id": "x:a", "arm": "hybrid", "token_budget": 512, "repetition": 0}
    plan = _plan(tmp_path, (cell,))
    original_report_envelope = cc.report_envelope

    def replace_before_envelope(**kwargs):
        plan.parent_manifest_path.write_text('{"changed":true}\n', encoding="utf-8")
        return original_report_envelope(**kwargs)

    monkeypatch.setattr(cc, "report_envelope", replace_before_envelope)
    with pytest.raises(cc.ContinuationError, match="evidence sources changed"):
        cc.combined_report(plan)


def test_continuation_cli_fails_on_critical_violations(monkeypatch, tmp_path, capsys):
    plan = object()
    monkeypatch.setattr(cc, "prepare_plan", lambda **kwargs: plan)
    monkeypatch.setattr(cc, "build_continuation_client", lambda value: object())
    monkeypatch.setattr(cc, "load_corpus", lambda path: [])
    monkeypatch.setattr(
        cc,
        "run_continuation",
        lambda *args, **kwargs: {"metrics": {
            "status": "BLOCKED",
            "eligible_execution_status": "BLOCKED",
            "valid_missing_attempts": 0,
            "statuses": {},
            "critical_violations": 1,
        }},
    )
    paths = [str(tmp_path / name) for name in (
        "parent.json", "companion.json", "eligibility.json", "audit.json",
        "public.json", "approval.json", "parent-results", "child-results",
    )]

    result = cc.main([
        "--parent-manifest", paths[0], "--companion", paths[1],
        "--eligibility", paths[2], "--audit-artifact", paths[3],
        "--public-artifact", paths[4], "--parent-approval", paths[5],
        "--parent-results", paths[6], "--child-results", paths[7],
        "--execute",
    ])

    assert result == 2
    assert json.loads(capsys.readouterr().out)["critical_violations"] == 1


@pytest.mark.parametrize(
    ("eligible_execution_status", "valid_missing_attempts", "eligible_statuses", "expected"),
    [
        ("COMPLETE", 0, {"complete": 89}, 0),
        ("BLOCKED", 0, {"complete": 88, "error": 1}, 2),
        ("PARTIAL", 1, {"complete": 88}, 2),
    ],
)
def test_continuation_cli_exit_gate_uses_eligible_cohort(
    monkeypatch, tmp_path, capsys, eligible_execution_status,
    valid_missing_attempts, eligible_statuses, expected,
):
    # The raw protocol retains an excluded fixture error.  It must not block a
    # complete eligible cohort, while eligible errors and missing cells fail closed.
    report = {"metrics": {
        "status": "BLOCKED" if expected else "COMPLETE",
        "eligible_execution_status": eligible_execution_status,
        "valid_missing_attempts": valid_missing_attempts,
        "statuses": {**eligible_statuses, "error": eligible_statuses.get("error", 0) + 1},
        "eligible_statuses": eligible_statuses,
        "critical_violations": 0,
    }}
    monkeypatch.setattr(cc, "prepare_plan", lambda **kwargs: object())
    monkeypatch.setattr(cc, "build_continuation_client", lambda value: object())
    monkeypatch.setattr(cc, "load_corpus", lambda path: [])
    monkeypatch.setattr(cc, "run_continuation", lambda *args, **kwargs: report)
    paths = [str(tmp_path / name) for name in (
        "parent.json", "companion.json", "eligibility.json", "audit.json",
        "public.json", "approval.json", "parent-results", "child-results",
    )]
    args = ["--execute"]
    for name, path in zip((
        "parent-manifest", "companion", "eligibility", "audit-artifact",
        "public-artifact", "parent-approval", "parent-results", "child-results",
    ), paths):
        args.extend(["--" + name, path])

    assert cc.main(args) == expected
    assert json.loads(capsys.readouterr().out)["eligible_execution_status"] == eligible_execution_status


@pytest.mark.parametrize("unsupported", ["eligible", "excluded", "none", "unattempted_excluded"])
def test_terminal_unsupported_attempts_cannot_complete_eligible_execution(
    monkeypatch, tmp_path, capsys, unsupported,
):
    manifest = _manifest()
    cells = tuple(cc._cells(manifest, "development_pilot"))
    excluded = _excluded(manifest)
    excluded_keys = {cc._cell_key(cell) for cell in excluded}
    rows = {}
    for cell in cells:
        is_excluded = cc._cell_key(cell) in excluded_keys
        if unsupported == "unattempted_excluded" and is_excluded:
            continue
        status = "unsupported" if (
            unsupported == "eligible" and not is_excluded
            or unsupported == "excluded" and is_excluded
        ) else "complete"
        rows[cc._cell_key(cell)] = _row(cell, status)
    plan = replace(_plan(tmp_path, cells), parent_rows=rows,
                   excluded_cells=excluded, eligible_missing=())
    report = cc.combined_report(plan)
    metrics = report["metrics"]
    assert metrics["valid_missing_attempts"] == 0
    assert metrics["missing_attempts"] == (15 if unsupported == "unattempted_excluded" else 0)
    assert metrics["status"] == (
        "BLOCKED" if unsupported in {"eligible", "unattempted_excluded"} else "COMPLETE"
    )
    assert metrics["eligible_execution_status"] == (
        "PARTIAL" if unsupported == "eligible" else "COMPLETE"
    )
    monkeypatch.setattr(cc, "prepare_plan", lambda **kwargs: plan)
    monkeypatch.setattr(cc, "build_continuation_client", lambda value: object())
    monkeypatch.setattr(cc, "load_corpus", lambda path: [])
    monkeypatch.setattr(cc, "run_continuation", lambda *args, **kwargs: report)
    args = ["--execute"]
    for name in ("parent-manifest", "companion", "eligibility", "audit-artifact",
                 "public-artifact", "parent-approval", "parent-results", "child-results"):
        args.extend(["--" + name, str(tmp_path / name)])

    assert cc.main(args) == (2 if unsupported == "eligible" else 0)
    assert json.loads(capsys.readouterr().out)["status"] == metrics["status"]


def test_started_marker_fails_closed_without_runner(tmp_path):
    cell = {"scenario_id": "x:a", "arm": "hybrid", "token_budget": 512, "repetition": 0}
    plan = _plan(tmp_path, (cell,))
    marker = plan.child_results / plan.stage_name / f"{cc.campaign.digest(cell)}.started"
    marker.parent.mkdir(parents=True)
    marker.write_text("reserved", encoding="utf-8")
    called = []
    with pytest.raises(cc.ContinuationError, match="unfinished child"):
        cc.run_continuation(
            plan, corpus=None, client=object(),
            attempt_runner=lambda *args: called.append(args),
            enforce_source=False,
        )
    assert called == []


def test_parent_ledger_drift_leaves_marker_and_stops(tmp_path):
    cell = {"scenario_id": "x:a", "arm": "hybrid", "token_budget": 512, "repetition": 0}
    plan = _plan(tmp_path, (cell,))
    def mutate_parent(*args):
        plan.parent_ledger_path.write_text("mutated", encoding="utf-8")
        return _row(cell)
    with pytest.raises(cc.ContinuationError, match="immutable parent"):
        cc.run_continuation(plan, corpus=None, client=object(), attempt_runner=mutate_parent, enforce_source=False)
    assert list((plan.child_results / plan.stage_name).glob("*.started"))


def test_new_error_is_terminal_and_stops_later_cells(tmp_path):
    cell1 = {"scenario_id": "x:a", "arm": "hybrid", "token_budget": 512, "repetition": 0}
    cell2 = {"scenario_id": "x:b", "arm": "hybrid", "token_budget": 512, "repetition": 0}
    plan = _plan(tmp_path, (cell1, cell2))
    seen = []
    def fail_once(manifest, stage_name, cell, corpus, client):
        seen.append(cell)
        raise RuntimeError("provider body must not be persisted")
    report = cc.run_continuation(
        plan, corpus=None, client=object(), attempt_runner=fail_once, enforce_source=False,
    )
    assert seen == [cell1]
    assert report["metrics"]["status"] == "BLOCKED"
    assert report["metrics"]["statuses"] == {"error": 1}
    assert len(list((plan.child_results / plan.stage_name).glob("*.json"))) == 1
    assert len(report["records"]) == 1
    assert report["protocol"]["n_scored"] == 0


def test_parent_allocation_prevents_second_results_directory(tmp_path):
    cell = {"scenario_id": "x:a", "arm": "hybrid", "token_budget": 512, "repetition": 0}
    plan = _plan(tmp_path, (cell,))
    cc.run_continuation(plan, corpus=None, client=object(),
                        attempt_runner=lambda *args: _row(cell), enforce_source=False)
    other = replace(plan, child_results=tmp_path / "another-child")
    with pytest.raises(cc.ContinuationError, match="already allocated"):
        cc.run_continuation(other, corpus=None, client=object(),
                            attempt_runner=lambda *args: pytest.fail("attempt replayed"), enforce_source=False)


def test_parent_drift_blocks_second_reader_call(tmp_path):
    cell = {"scenario_id": "x:a", "arm": "hybrid", "token_budget": 512, "repetition": 0}
    plan = _plan(tmp_path, (cell,))
    class Client:
        calls = 0
        def complete(self):
            self.calls += 1
            plan.parent_ledger_path.write_text("changed after first call", encoding="utf-8")
    client = Client()
    def runner(manifest, stage, cell, corpus, guarded):
        guarded.complete()
        guarded.complete()
        return _row(cell)
    with pytest.raises(cc.ContinuationError, match="immutable parent"):
        cc.run_continuation(plan, corpus=None, client=client, attempt_runner=runner, enforce_source=False)
    assert client.calls == 1
