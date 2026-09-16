import pytest

from eval.campaign_ledger import (
    BudgetApproval,
    BudgetExceeded,
    CampaignBinding,
    CampaignLedger,
    CampaignLedgerError,
    sha256_json,
)


def _binding() -> CampaignBinding:
    digest = "a" * 64
    return CampaignBinding(
        campaign_id="campaign-test",
        model="gpt-5.6-luna",
        reasoning_effort="medium",
        dataset_sha256=digest,
        config_sha256="b" * 64,
        repo_revision="c" * 40,
        pins_sha256="d" * 64,
    )


def _approval(*, max_calls: int = 3, max_cost_micros: int = 100_000) -> BudgetApproval:
    return BudgetApproval.create(
        max_calls=max_calls,
        max_cost_micros=max_cost_micros,
    )


def test_budget_artifact_is_hash_bound_and_ledger_hides_prompt(tmp_path):
    approval = _approval()
    artifact = approval.public_fields()
    assert BudgetApproval.from_artifact(artifact) == approval
    tampered = {**artifact, "max_cost_micros": artifact["max_cost_micros"] + 1}
    with pytest.raises(CampaignLedgerError, match="hash mismatch"):
        BudgetApproval.from_artifact(tampered)

    ledger = CampaignLedger(tmp_path / "calls.jsonl", _binding(), approval)
    request_hash = sha256_json({"prompt": "SECRET_PROMPT"})
    reservation = ledger.reserve(
        "call-a", "reader", 30_000, request_sha256=request_hash,
    )
    assert reservation.status == "reserved"
    ledger.mark_dispatched("call-a")
    ledger.complete(
        "call-a", "  safe answer\n", usage={"input_tokens": 2},
        actual_cost_micros=1,
    )
    raw = (tmp_path / "calls.jsonl").read_text(encoding="utf-8")
    assert "SECRET_PROMPT" not in raw
    assert ledger.lookup("call-a").status == "completed"


def test_completed_call_resumes_and_changed_request_is_rejected(tmp_path):
    approval = _approval()
    path = tmp_path / "calls.jsonl"
    first = CampaignLedger(path, _binding(), approval)
    request_hash = sha256_json({"input": "same"})
    first.reserve("call-a", "ingest", 20_000, request_sha256=request_hash)
    first.complete("call-a", "answer", usage={}, actual_cost_micros=0)

    resumed = CampaignLedger(path, _binding(), approval)
    result = resumed.reserve("call-a", "ingest", 20_000, request_sha256=request_hash)
    assert result.status == "completed"
    assert result.response == "answer"
    with pytest.raises(CampaignLedgerError, match="different request"):
        resumed.reserve("call-a", "ingest", 20_000,
                        request_sha256=sha256_json({"input": "changed"}))


def test_interrupted_reservation_becomes_uncertain_without_replay(tmp_path):
    approval = _approval(max_calls=2, max_cost_micros=40_000)
    path = tmp_path / "calls.jsonl"
    ledger = CampaignLedger(path, _binding(), approval)
    request_hash = sha256_json({"input": "possibly dispatched"})
    ledger.reserve("call-a", "correction", 20_000, request_sha256=request_hash)
    reopened = CampaignLedger(path, _binding(), approval)
    result = reopened.reserve("call-a", "correction", 20_000, request_sha256=request_hash)
    assert result.status == "uncertain"
    assert reopened.lookup("call-a").status == "uncertain"
    with pytest.raises(BudgetExceeded):
        reopened.reserve(
            "call-b", "reader", 20_001,
            request_sha256=sha256_json({"input": "second"}),
        )


def test_completion_cannot_exceed_its_reservation(tmp_path):
    ledger = CampaignLedger(tmp_path / "calls.jsonl", _binding(), _approval())
    ledger.reserve("call-a", "evaluator", 2, request_sha256="e" * 64)
    with pytest.raises(CampaignLedgerError, match="exceeds"):
        ledger.complete("call-a", "answer", usage={}, actual_cost_micros=3)
