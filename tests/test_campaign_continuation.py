from __future__ import annotations

from dataclasses import replace
import hashlib
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
