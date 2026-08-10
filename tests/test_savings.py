import pytest

from engraphis import __version__
from engraphis.core.savings import SavingsEstimate, annotate_usage, estimate_savings
from engraphis.core.store import Store
from engraphis.service import MemoryService, ValidationError


@pytest.mark.parametrize(
    ("operation", "intent", "adaptive_mode", "basis", "confidence", "eligible"),
    [
        ("adaptive_context", None, "retrieval", "history_retrieval", "high", True),
        ("adaptive_context", None, "history_fallback", "history_fallback", "medium", True),
        ("adaptive_context", None, "history_bypass", "history_bypass", "none", False),
        (
            "adaptive_context",
            None,
            "low_confidence_abstain",
            "low_confidence_abstain",
            "none",
            False,
        ),
        ("recall", "recall_context", None, "packed_context", "medium", True),
        ("grounded_recall", None, None, "packed_context", "medium", True),
        ("proactive_context", None, None, "packed_context", "medium", True),
        ("recall", "recall", None, "unclassified", "unknown", False),
    ],
)
def test_estimator_classifies_each_delivery_basis(
    operation, intent, adaptive_mode, basis, confidence, eligible
):
    estimate = estimate_savings(
        operation=operation,
        intent=intent,
        adaptive_mode=adaptive_mode,
        baseline_tokens=100,
        emitted_tokens=40,
        token_counter="engraphis.regex.v1",
        release_version="1.5",
    )

    assert isinstance(estimate, SavingsEstimate)
    assert estimate.basis == basis
    assert estimate.confidence == confidence
    assert estimate.eligible is eligible
    assert estimate.saved_tokens == (60 if eligible else 0)
    assert 0 <= estimate.saved_tokens <= estimate.baseline_tokens
    assert 0 <= estimate.savings_ratio <= 1
    assert estimate.release_version == "1.5"


def test_estimator_is_conservative_for_bad_counts_and_annotates_existing_usage():
    usage = annotate_usage(
        {"source_tokens": 90, "context_tokens": 30, "saved_tokens": 60,
         "token_counter": "engraphis.regex.v1"},
        operation="adaptive_context",
        adaptive_mode="history_fallback",
        baseline_tokens=90,
        emitted_tokens=30,
        release_version=__version__,
    )

    assert usage["estimated_saved_tokens"] == 60
    assert usage["savings_eligible"] is True
    assert usage["release_version"] == __version__
    abstained = estimate_savings(
        operation="adaptive_context",
        adaptive_mode="low_confidence_abstain",
        baseline_tokens=float("nan"),
        emitted_tokens=0,
    )
    assert abstained.saved_tokens == 0
    assert abstained.baseline_tokens == 0


def _usage(baseline, emitted, *, counter, release="1.5", eligible=True,
           basis="history_retrieval", confidence="high"):
    saved = max(0, baseline - emitted) if eligible else 0
    return {
        "source_tokens": baseline,
        "context_tokens": emitted,
        "saved_tokens": saved,
        "budget_tokens": baseline,
        "packed_count": 1,
        "omitted_count": 0,
        "token_counter": counter,
        "baseline_tokens": baseline,
        "emitted_tokens": emitted,
        "estimated_saved_tokens": saved,
        "estimated_savings_ratio": saved / baseline if baseline else 0.0,
        "savings_basis": basis,
        "savings_confidence": confidence,
        "savings_eligible": eligible,
        "release_version": release,
    }


def test_context_savings_aggregates_estimates_filters_releases_and_counters():
    store = Store(":memory:")
    wid = store.get_or_create_workspace("savings")
    rid = store.get_or_create_repo(wid, "repo")
    first = store.record_receipt(
        "adaptive_context",
        workspace_id=wid,
        repo_id=rid,
        metadata={"adaptive_mode": "retrieval", "token_usage": _usage(
            100, 40, counter="engraphis.regex.v1"
        )},
    )
    second = store.record_receipt(
        "adaptive_context",
        workspace_id=wid,
        repo_id=rid,
        metadata={"adaptive_mode": "history_bypass", "token_usage": _usage(
            80, 80, counter="engraphis.regex.v1", eligible=False,
            basis="history_bypass", confidence="none"
        )},
    )
    third = store.record_receipt(
        "recall",
        workspace_id=wid,
        repo_id=rid,
        metadata={"intent": "recall_context", "token_usage": _usage(
            50, 20, counter="estimate_tokens", release="1.4.0",
            basis="packed_context", confidence="medium"
        )},
    )
    old = store.record_receipt(
        "recall",
        workspace_id=wid,
        repo_id=rid,
        metadata={"intent": "recall_context", "token_usage": {
            "source_tokens": 20, "context_tokens": 10, "saved_tokens": 10,
            "token_counter": "engraphis.regex.v1",
        }},
    )
    for timestamp, receipt in ((100.0, first), (110.0, second), (120.0, third), (130.0, old)):
        store.conn.execute(
            "UPDATE operation_receipts SET ts=? WHERE id=?", (timestamp, receipt["id"])
        )
    store.conn.commit()

    summary = store.context_savings(
        workspace_id=wid, repo_id=rid, from_ts=99, to_ts=121
    )
    assert summary["estimated"]["eligible_receipt_count"] == 2
    assert summary["estimated"]["excluded_receipt_count"] == 1
    assert summary["estimated"]["unclassified_receipt_count"] == 0
    assert summary["estimated"]["baseline_tokens"] == 150
    assert summary["estimated"]["emitted_tokens"] == 60
    assert summary["estimated"]["saved_tokens"] == 90
    assert {row["token_counter"] for row in summary["estimated"]["by_token_counter"]} == {
        "engraphis.regex.v1", "estimate_tokens"
    }
    assert summary["period"] == {"from_ts": 99, "to_ts": 121}
    all_time = store.context_savings(workspace_id=wid, repo_id=rid)
    assert all_time["estimated"]["unclassified_receipt_count"] == 1

    current = store.context_savings(
        workspace_id=wid, repo_id=rid, release_version="1.5"
    )
    assert current["receipt_count"] == 2
    assert current["usage_receipt_count"] == 2
    assert current["estimated"]["eligible_receipt_count"] == 1
    assert current["estimated"]["saved_tokens"] == 60
    assert current["estimated"]["by_basis"][0]["basis"] == "history_retrieval"

    with pytest.raises(ValueError, match="semantic version"):
        store.context_savings(workspace_id=wid, release_version="not-a-release")


def test_service_context_savings_filters_and_new_receipts_are_versioned():
    service = MemoryService.create(":memory:", graph_extractor="none")
    service.remember("Versioned context delivery.", workspace="versioned", scope="workspace")
    service.recall(
        "context delivery",
        workspace="versioned",
        token_budget=32,
        response_mode="compact",
        intent="recall_context",
    )
    receipt = service.receipt_log(workspace="versioned")["entries"][0]
    usage = receipt["metadata"]["token_usage"]
    assert usage["release_version"] == __version__
    assert usage["savings_basis"] == "packed_context"
    assert usage["savings_eligible"] is True
    filtered = service.context_savings(
        workspace="versioned", release_version=__version__,
        from_ts=0, to_ts=9_999_999_999,
    )
    assert filtered["estimated"]["eligible_receipt_count"] == 1
    with pytest.raises(ValidationError, match="semantic version"):
        service.context_savings(workspace="versioned", release_version="legacy")


def test_grouped_context_savings_uses_the_same_time_and_release_filters():
    service = MemoryService.create(":memory:", graph_extractor="none")
    wid = service.store.get_or_create_workspace("grouped")
    rid = service.store.get_or_create_repo(wid, "api")
    included = service.store.record_receipt(
        "recall",
        workspace_id=wid,
        repo_id=rid,
        metadata={"token_usage": _usage(
            100, 40, counter="engraphis.regex.v1", release="1.5"
        )},
    )
    other_release = service.store.record_receipt(
        "recall",
        workspace_id=wid,
        repo_id=rid,
        metadata={"token_usage": _usage(
            80, 20, counter="engraphis.regex.v1", release="1.4"
        )},
    )
    outside_window = service.store.record_receipt(
        "recall",
        workspace_id=wid,
        repo_id=rid,
        metadata={"token_usage": _usage(
            50, 10, counter="engraphis.regex.v1", release="1.5"
        )},
    )
    for timestamp, receipt in (
        (100.0, included), (120.0, other_release), (200.0, outside_window)
    ):
        service.store.conn.execute(
            "UPDATE operation_receipts SET ts=? WHERE id=?", (timestamp, receipt["id"])
        )
    service.store.conn.commit()

    summary = service.context_savings(
        workspace="grouped",
        repo="api",
        group_by="repo",
        from_ts=90,
        to_ts=150,
        release_version="1.5",
        format="csv",
    )

    assert summary["receipt_count"] == 1
    assert summary["by_group"] == [{
        "group_key": rid,
        "token_counter": "engraphis.regex.v1",
        "receipt_count": 1,
        "source_tokens": 100,
        "context_tokens": 40,
        "saved_tokens": 60,
        "budget_tokens": 100,
        "packed_count": 1,
        "omitted_count": 0,
        "savings_ratio": 0.6,
    }]
    assert summary["csv"].splitlines()[0].startswith("group_key,token_counter,")


def test_ungrouped_context_savings_honors_csv_format():
    service = MemoryService.create(":memory:", graph_extractor="none")
    wid = service.store.get_or_create_workspace("ungrouped-csv")
    service.store.record_receipt(
        "recall",
        workspace_id=wid,
        metadata={"token_usage": _usage(100, 40, counter="engraphis.regex.v1")},
    )

    summary = service.context_savings(workspace="ungrouped-csv", format="csv")

    assert summary["csv"].splitlines()[0].startswith("token_counter,receipt_count,")
    assert "engraphis.regex.v1" in summary["csv"]


def test_grouped_context_savings_separates_counters_and_rejects_invalid_saved():
    store = Store(":memory:")
    wid = store.get_or_create_workspace("grouped-counters")
    valid = (
        _usage(100, 40, counter="engraphis.regex.v1"),
        _usage(80, 20, counter="estimate_tokens"),
    )
    invalid = _usage(70, 30, counter="engraphis.regex.v1")
    invalid["saved_tokens"] = 999
    for usage in (*valid, invalid):
        store.record_receipt(
            "recall",
            workspace_id=wid,
            metadata={"token_usage": usage},
        )

    assert store.context_savings_grouped(
        workspace_id=wid, group_by="workspace"
    ) == [
        {
            "group_key": wid,
            "token_counter": "engraphis.regex.v1",
            "receipt_count": 1,
            "source_tokens": 100,
            "context_tokens": 40,
            "saved_tokens": 60,
            "budget_tokens": 100,
            "packed_count": 1,
            "omitted_count": 0,
            "savings_ratio": 0.6,
        },
        {
            "group_key": wid,
            "token_counter": "estimate_tokens",
            "receipt_count": 1,
            "source_tokens": 80,
            "context_tokens": 20,
            "saved_tokens": 60,
            "budget_tokens": 80,
            "packed_count": 1,
            "omitted_count": 0,
            "savings_ratio": 0.75,
        },
    ]


def test_context_savings_ignores_gateway_copies_and_rejects_noncanonical_estimates():
    store = Store(":memory:")
    wid = store.get_or_create_workspace("gateway-savings")
    authoritative = _usage(100, 40, counter="engraphis.regex.v1")
    store.record_receipt(
        "adaptive_context", workspace_id=wid,
        metadata={"token_usage": authoritative},
    )
    store.record_receipt(
        "smart_gateway", workspace_id=wid,
        metadata={"token_usage": authoritative},
    )
    noncanonical = _usage(80, 20, counter="engraphis.regex.v1")
    noncanonical["estimated_savings_ratio"] = 0.1
    store.record_receipt(
        "adaptive_context", workspace_id=wid,
        metadata={"token_usage": noncanonical},
    )

    summary = store.context_savings(workspace_id=wid)
    assert summary["estimated"]["eligible_receipt_count"] == 1
    assert summary["estimated"]["saved_tokens"] == 60
    assert summary["estimated"]["invalid_estimate_count"] == 1
