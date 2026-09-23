"""Public compact-recall and token-accounting contracts."""
from __future__ import annotations

import json

import pytest

from engraphis.core.context import RegexTokenCounter
from engraphis.core.interfaces import MemoryType, Scope
from engraphis.service import MemoryService, ValidationError


def _seed_service() -> MemoryService:
    service = MemoryService.create(":memory:")
    workspace_id = service.store.get_or_create_workspace("acme")
    repo_id = service.store.get_or_create_repo(workspace_id, "api")
    for index in range(4):
        # Fixture data models local, reviewed code-owned facts. The public service
        # surface is separately covered as pending-only.
        service.engine.remember(
            (
                f"Release policy evidence {index}: deployments require a signed tag and "
                "a successful backup verification before production promotion. "
                + "Operational rationale and audit detail remain attached to this record. " * 20
            ),
            workspace_id=workspace_id,
            repo_id=repo_id,
            mtype=MemoryType.SEMANTIC,
            scope=Scope.REPO,
            title=f"Release policy {index}",
            resolve_conflicts=False,
        )
    return service


def test_compact_recall_has_a_hard_budget_and_omits_duplicate_bodies():
    service = _seed_service()

    result = service.recall(
        "What evidence governs release deployment?",
        workspace="acme",
        repo="api",
        k=4,
        token_budget=80,
        response_mode="compact",
        reinforce=False,
        record_receipt=False,
    )

    assert result["usage"]["context_tokens"] == RegexTokenCounter()(result["context"])
    assert result["usage"]["context_tokens"] <= 80
    assert result["usage"]["token_counter"] == "engraphis.regex.v1"
    assert result["packed_sources"]
    assert all("content" not in source for source in result["memories"])


def test_compact_serialized_payload_saves_at_least_half_vs_legacy_full():
    service = _seed_service()
    kwargs = {
        "workspace": "acme",
        "repo": "api",
        "k": 4,
        "token_budget": 80,
        "reinforce": False,
        "record_receipt": False,
    }

    full = service.recall(
        "What evidence governs release deployment?",
        response_mode="full",
        **kwargs,
    )
    compact = service.recall(
        "What evidence governs release deployment?",
        response_mode="compact",
        **kwargs,
    )
    count = RegexTokenCounter()
    full_tokens = count(json.dumps(full, sort_keys=True))
    compact_tokens = count(json.dumps(compact, sort_keys=True))

    assert compact_tokens <= full_tokens * 0.5
    assert compact["usage"]["savings_ratio"] > 0.5


def test_receipt_records_only_privacy_safe_token_aggregates():
    service = _seed_service()
    secret_query = "private-query-marker release deployment"

    service.recall(
        secret_query,
        workspace="acme",
        repo="api",
        token_budget=64,
        response_mode="compact",
    )
    row = service.store.conn.execute(
        "SELECT payload FROM operation_receipts ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    payload = json.loads(row["payload"])
    metadata = payload["metadata"]

    assert secret_query not in row["payload"]
    assert metadata["token_usage"]["budget_tokens"] == 64
    assert metadata["token_usage"]["context_tokens"] <= 64
    assert metadata["token_usage"]["token_counter"] == "engraphis.regex.v1"


def test_recall_temporal_alias_conflict_and_modes_fail_closed():
    service = _seed_service()

    with pytest.raises(ValidationError, match="must match"):
        service.recall(
            "release",
            workspace="acme",
            as_of=100.0,
            valid_at=101.0,
        )
    with pytest.raises(ValidationError, match="response_mode"):
        service.recall("release", workspace="acme", response_mode="tiny")
    with pytest.raises(ValidationError, match="retrieval_profile"):
        service.recall("release", workspace="acme", retrieval_profile="magic")


def test_diagnostics_preserve_each_score_stage_without_changing_default_payload():
    service = _seed_service()

    normal = service.recall(
        "release deployment evidence",
        workspace="acme",
        repo="api",
        reinforce=False,
        record_receipt=False,
    )
    diagnostic = service.recall(
        "release deployment evidence",
        workspace="acme",
        repo="api",
        reinforce=False,
        record_receipt=False,
        diagnostics=True,
        retrieval_profile="lexical",
    )

    assert "retrieval_trace" not in normal
    assert diagnostic["retrieval_profile"] == "lexical"
    assert diagnostic["retrieval_trace"]
    stages = diagnostic["retrieval_trace"][0]
    assert {
        "raw",
        "normalized",
        "fusion_score",
        "rerank_score",
        "calibrated_score",
        "arm_agreement",
    } <= stages.keys()


def test_service_exposes_claim_identity_for_safe_supersession():
    service = MemoryService.create(":memory:")
    first = service.remember(
        "The configured API request ceiling is one hundred.",
        workspace="acme",
        subject_key="api.rate_limit",
        claim_kind="configured_value",
    )
    second = service.remember(
        "The configured API request ceiling is five hundred.",
        workspace="acme",
        subject_key="api.rate_limit",
        claim_kind="configured_value",
    )

    # Local-agent callers may resolve a claim immediately; owner approval is not
    # required for normal memory creation.
    assert second["op"] == "invalidate"
    assert second["superseded"] == [first["id"]]
    assert service.store.get_memory(second["id"]).provenance["review_state"] == "approved"


def test_compact_grounded_response_does_not_repeat_cited_bodies():
    service = MemoryService.create(":memory:")
    long_body = (
        "The API authenticates with PASETO v4 public tokens. "
        + "This intentionally long evidence body carries bounded operational detail. " * 24
    )
    workspace_id = service.store.get_or_create_workspace("acme")
    repo_id = service.store.get_or_create_repo(workspace_id, "api")
    service.engine.remember(
        long_body,
        workspace_id=workspace_id,
        repo_id=repo_id,
        mtype=MemoryType.SEMANTIC,
        scope=Scope.REPO,
    )

    full = service.grounded_recall(
        "How does the API authenticate?",
        workspace="acme",
        repo="api",
        min_support=0.0,
        response_mode="full",
        token_budget=48,
    )
    compact = service.grounded_recall(
        "How does the API authenticate?",
        workspace="acme",
        repo="api",
        min_support=0.0,
        response_mode="compact",
        token_budget=48,
    )

    assert full["citations"] and "content" in full["citations"][0]
    assert compact["citations"] and "content" not in compact["citations"][0]
    assert "excerpt" not in compact["citations"][0]
    assert compact["grounded"] == full["grounded"]
    assert compact["answer"] == full["answer"]
    assert [citation["id"] for citation in compact["citations"]] == [
        citation["id"] for citation in full["citations"]
    ]
    assert "PASETO" in compact["answer"]
    assert long_body not in json.dumps(compact, sort_keys=True)
    assert compact["usage"]["answer_tokens"] == RegexTokenCounter()(compact["answer"])
    assert compact["usage"]["answer_tokens"] <= 48
    assert compact["usage"]["context_tokens"] <= 48


@pytest.fixture
def exact_service():
    service = MemoryService.create(":memory:")
    yield service
    service.close()


def test_compact_recall_emits_exact_binding_only_for_admitted_packed_source(exact_service):
    service = exact_service
    content = "Deployment label is ALPHA."
    stored = service.remember(
        content,
        workspace="acme",
        exact_value="ALPHA",
        exact_value_type="identifier",
    )

    empty = service.recall(
        "deployment label",
        workspace="acme",
        token_budget=0,
        response_mode="compact",
        record_receipt=False,
    )
    assert empty["context"] == ""
    assert empty["packed_sources"] == []
    assert [row["id"] for row in empty["memories"]] == [stored["id"]]
    assert all("exact_value" not in memory for memory in empty["memories"])

    full = service.recall(
        "deployment label",
        workspace="acme",
        token_budget=0,
        response_mode="full",
        record_receipt=False,
    )
    full_memory = next(memory for memory in full["memories"] if memory["id"] == stored["id"])
    assert full_memory["content"] == content
    assert full_memory["exact_value"]["value"] == "ALPHA"

    roomy = service.recall(
        "deployment label",
        workspace="acme",
        token_budget=100,
        response_mode="compact",
        record_receipt=False,
    )
    packed = next(source for source in roomy["packed_sources"] if source["id"] == stored["id"])
    assert packed["exact_value"] == {
        "value": "ALPHA",
        "type": "identifier",
        "source": "content",
        "start": content.index("ALPHA"),
        "end": content.index("ALPHA") + len("ALPHA"),
        "copy_exactly": True,
    }
    assert packed["source_span"] == [content.index("ALPHA"), content.index("ALPHA") + 5]
    assert all("exact_value" not in memory for memory in roomy["memories"])


def test_intent_compact_recall_does_not_detach_exact_binding_from_context(exact_service):
    service = exact_service
    stored = service.remember(
        "Deployment label is ALPHA.",
        workspace="acme",
        exact_value="ALPHA",
        exact_value_type="identifier",
    )

    result = service.intent_recall(
        "deployment label",
        workspace="acme",
        token_budget=0,
        response_mode="compact",
        record_receipt=False,
    )

    assert result["context"] == ""
    assert result["packed_sources"] == []
    assert [row["id"] for row in result["memories"]] == [stored["id"]]
    assert all("exact_value" not in memory for memory in result["memories"])


@pytest.mark.parametrize("budget", [8, 16])
def test_compact_source_identity_does_not_imply_bound_occurrence_admission(exact_service, budget):
    content = "First Δ-42. Second Δ-42."
    start = content.rindex("Δ-42")
    stored = exact_service.remember(
        content, workspace="acme", exact_value="Δ-42", exact_value_type="identifier",
        exact_value_span=(start, start + 4),
    )
    result = exact_service.recall(
        "First", workspace="acme", k=1, token_budget=budget,
        response_mode="compact", reinforce=False, record_receipt=False,
    )
    assert [row["id"] for row in result["memories"]] == [stored["id"]]
    assert "exact_value" not in result["memories"][0]
    packed = result["packed_sources"][0]
    assert packed["id"] == stored["id"]
    if budget == 8:
        assert result["context"] == "[1]\nFirst Δ-42."
        assert "exact_value" not in packed and "source_span" not in packed
        assert packed["evidence_unit"]["value"] is None
    else:
        assert content in result["context"]
        assert packed["exact_value"]["value"] == "Δ-42"
        assert packed["source_span"] == [start, start + 4]
