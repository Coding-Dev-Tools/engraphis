"""Contracts for prompt-aware bypass and confidence-triggered widening."""
from __future__ import annotations

import pytest

from engraphis.core.adaptive_context import AdaptiveContextResult
from engraphis.core.engine import MemoryEngine
from engraphis.core.interfaces import MemoryType, PackedChunk, Scope
from engraphis.core.recall import RecallResult
from engraphis.service import MemoryService, ValidationError


def _seed_engine() -> tuple[MemoryEngine, str, str]:
    engine = MemoryEngine.create(":memory:")
    workspace_id = engine.store.get_or_create_workspace("adaptive")
    repo_id = engine.store.get_or_create_repo(workspace_id, "context")
    engine.remember(
        "Deployment approval belongs to the release manager.",
        workspace_id=workspace_id,
        repo_id=repo_id,
        mtype=MemoryType.SEMANTIC,
        scope=Scope.REPO,
        resolve_conflicts=False,
    )
    return engine, workspace_id, repo_id


def test_history_that_fits_bypasses_embedding_and_retrieval(monkeypatch) -> None:
    engine, workspace_id, repo_id = _seed_engine()

    def fail(*args, **kwargs):
        raise AssertionError("recall must not run when supplied history already fits")

    monkeypatch.setattr(engine.recall_engine, "recall", fail)
    result = engine.adaptive_context(
        "Who approves deployment?",
        "The release manager approves deployment.",
        workspace_id=workspace_id,
        repo_id=repo_id,
        max_context_tokens=64,
    )

    assert result.mode == "history_bypass"
    assert result.retrieved is False
    assert result.context == "The release manager approves deployment."
    assert result.context_tokens == result.history_tokens
    assert result.to_dict()["reason"] == "provided history already fits the prompt budget"


def test_large_history_uses_compact_retrieval_when_absolute_support_is_strong() -> None:
    engine, workspace_id, repo_id = _seed_engine()
    history = "\n".join(
        [f"Unrelated operational note number {number}." for number in range(40)]
        + ["Deployment approval belongs to the release manager."]
    )

    result = engine.adaptive_context(
        "Who owns deployment approval?",
        history,
        workspace_id=workspace_id,
        repo_id=repo_id,
        max_context_tokens=80,
        retrieval_token_budget=32,
    )

    assert result.mode == "retrieval"
    assert result.retrieved is True
    assert result.widened is False
    assert result.retrieval_support >= 0.25
    assert result.context_tokens <= 32
    assert "release manager" in result.context


def test_adaptive_support_includes_the_packed_source_title() -> None:
    engine = MemoryEngine.create(":memory:")
    workspace_id = engine.store.get_or_create_workspace("adaptive")
    repo_id = engine.store.get_or_create_repo(workspace_id, "context")
    engine.remember(
        "Every 30 days.",
        workspace_id=workspace_id,
        repo_id=repo_id,
        title="OAUTH_TOKEN_ROTATION",
        mtype=MemoryType.SEMANTIC,
        scope=Scope.REPO,
        resolve_conflicts=False,
    )
    history = " ".join(f"unrelated note {number}" for number in range(50))

    result = engine.adaptive_context(
        "OAUTH_TOKEN_ROTATION",
        history,
        workspace_id=workspace_id,
        repo_id=repo_id,
        max_context_tokens=40,
        retrieval_token_budget=16,
        retrieval_profile="lexical",
    )

    assert result.mode == "retrieval"
    assert result.retrieval_support >= 0.25
    assert "OAUTH_TOKEN_ROTATION" in result.context
    assert "Every 30 days" in result.context


def test_weak_retrieval_widens_to_recent_raw_history_without_reinforcing() -> None:
    engine, workspace_id, repo_id = _seed_engine()
    memory_id = engine.store.conn.execute(
        "SELECT id FROM memories WHERE repo_id=?",
        (repo_id,),
    ).fetchone()[0]
    before = engine.store.get_memory(memory_id)
    history = "\n".join(
        f"Recent task event {number} completed with status green."
        for number in range(30)
    )

    result = engine.adaptive_context(
        "What minerals are found on Europa?",
        history,
        workspace_id=workspace_id,
        repo_id=repo_id,
        max_context_tokens=48,
        retrieval_token_budget=12,
        confidence_floor=0.99,
        reinforce=True,
    )
    after = engine.store.get_memory(memory_id)

    assert result.mode == "history_fallback"
    assert result.retrieved is True
    assert result.widened is True
    assert result.truncated_history is True
    assert 12 < result.context_tokens <= 48
    assert "Recent task event 29" in result.context
    assert before is not None and after is not None
    assert after.access_count == before.access_count


def test_adaptive_context_abstains_when_weak_and_no_history_fits() -> None:
    engine, workspace_id, repo_id = _seed_engine()

    result = engine.adaptive_context(
        "What minerals are found on Europa?",
        "history cannot fit",
        workspace_id=workspace_id,
        repo_id=repo_id,
        max_context_tokens=0,
        retrieval_token_budget=0,
        confidence_floor=0.99,
    )

    assert result.mode == "low_confidence_abstain"
    assert result.context == ""
    assert result.context_tokens == 0
    assert result.truncated_history is True


def test_empty_retrieval_widens_history_even_with_a_zero_confidence_floor() -> None:
    engine = MemoryEngine.create(":memory:")
    workspace_id = engine.store.get_or_create_workspace("adaptive")
    repo_id = engine.store.get_or_create_repo(workspace_id, "context")
    history = " ".join(f"recent-{number}" for number in range(20))

    result = engine.adaptive_context(
        "Who approves deployment?",
        history,
        workspace_id=workspace_id,
        repo_id=repo_id,
        max_context_tokens=6,
        retrieval_token_budget=0,
        confidence_floor=0,
    )

    assert result.mode == "history_fallback"
    assert result.retrieval_support == 0.0
    assert result.context


def test_fit_recent_history_preserves_suffix_after_unicode_whitespace_boundary() -> None:
    from engraphis.core.adaptive_context import fit_recent_history

    history = "older context\tlatest answer"

    fitted, truncated = fit_recent_history(
        history,
        token_budget=2,
        count_tokens=lambda text: len(text.split()),
    )

    assert truncated is True
    assert fitted == "latest answer"
    assert len(fitted.split()) <= 2


def test_confidence_ignores_relevant_candidates_omitted_by_the_packer() -> None:
    engine, workspace_id, repo_id = _seed_engine()
    history = "\n".join(
        f"Recent task state {number} remains available."
        for number in range(20)
    )

    result = engine.adaptive_context(
        "Who owns deployment approval?",
        history,
        workspace_id=workspace_id,
        repo_id=repo_id,
        max_context_tokens=32,
        retrieval_token_budget=0,
    )

    assert result.recall is not None and result.recall.chunks
    assert result.recall.packed_chunks == []
    assert result.retrieval_support == 0.0
    assert result.mode == "history_fallback"


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"max_context_tokens": -1}, "max_context_tokens"),
        ({"max_context_tokens": 10, "retrieval_token_budget": 11}, "retrieval_token_budget"),
        ({"confidence_floor": float("nan")}, "confidence_floor"),
        ({"confidence_floor": 1.1}, "confidence_floor"),
        ({"k": 0}, "k"),
        ({"retrieval_profile": "unknown"}, "retrieval_profile"),
        ({"candidate_depth": "unknown"}, "candidate_depth"),
    ],
)
def test_adaptive_context_rejects_unsafe_policy_values(kwargs, message) -> None:
    engine, workspace_id, repo_id = _seed_engine()

    with pytest.raises(ValueError, match=message):
        engine.adaptive_context(
            "query",
            "history",
            workspace_id=workspace_id,
            repo_id=repo_id,
            **kwargs,
        )


def test_service_exposes_content_without_duplicating_memory_bodies_in_telemetry() -> None:
    service = MemoryService.create(":memory:")
    service.remember(
        "Deployment approval belongs to the release manager.",
        workspace="adaptive",
        repo="context",
    )

    result = service.adaptive_context(
        "Who approves deployment?",
        "The release manager approves deployment.",
        workspace="adaptive",
        repo="context",
        max_context_tokens=64,
    )

    assert result["context"] == "The release manager approves deployment."
    assert result["decision"]["mode"] == "history_bypass"
    assert result["sources"] == []
    assert "release manager" not in str(result["decision"]).casefold()


def test_service_adaptive_context_requires_an_existing_authorized_scope() -> None:
    service = MemoryService.create(":memory:")

    with pytest.raises(ValidationError, match="no workspace"):
        service.adaptive_context(
            "query",
            "history",
            workspace="missing",
        )


def test_service_does_not_label_rejected_weak_memories_as_fallback_sources() -> None:
    service = MemoryService.create(":memory:")
    service.remember(
        "Deployment approval belongs to the release manager.",
        workspace="adaptive",
        repo="context",
    )
    history = "\n".join(
        f"Recent unrelated event {number} remains green."
        for number in range(20)
    )

    result = service.adaptive_context(
        "What minerals are found on Europa?",
        history,
        workspace="adaptive",
        repo="context",
        max_context_tokens=32,
        retrieval_token_budget=12,
        confidence_floor=0.99,
    )

    assert result["decision"]["mode"] == "history_fallback"
    assert result["sources"] == []


def test_service_adaptive_context_keeps_sources_in_packed_citation_order(monkeypatch) -> None:
    service = MemoryService.create(":memory:")
    service.remember("Bootstrap fact.", workspace="adaptive", repo="context")
    recall = RecallResult(
        chunks=[
            {"id": "mem_first", "title": "First", "scope": "repo", "mtype": "episodic"},
            {
                "id": "mem_second",
                "title": "Second",
                "scope": "repo",
                "mtype": "semantic",
                "provenance": {
                    "source": "agent:review",
                    "trusted": True,
                    "secret": "must not be forwarded",
                },
            },
        ],
        packed_chunks=[
            PackedChunk("mem_second", "second evidence", 2),
            PackedChunk("mem_first", "first evidence", 2),
        ],
    )
    decision = AdaptiveContextResult(
        context="[1] second evidence\n[2] first evidence",
        mode="retrieval",
        reason="strong support",
        history_tokens=20,
        context_tokens=4,
        max_context_tokens=16,
        retrieval_budget_tokens=8,
        retrieval_support=1.0,
        retrieved=True,
        token_counter="engraphis.regex.v1",
        recall=recall,
    )
    monkeypatch.setattr(service.engine, "adaptive_context", lambda *args, **kwargs: decision)

    result = service.adaptive_context(
        "question",
        "long history that triggers routing",
        workspace="adaptive",
        repo="context",
        max_context_tokens=16,
        retrieval_token_budget=8,
    )

    assert [source["id"] for source in result["sources"]] == [
        "mem_second", "mem_first",
    ]
    assert result["sources"][0]["provenance"] == {
        "source": "agent:review",
        "trusted": True,
    }
    assert result["sources"][1]["provenance"] == {}


def test_service_bounds_adaptive_prompt_budgets() -> None:
    service = MemoryService.create(":memory:")
    service.remember("Fact.", workspace="adaptive", repo="context")

    with pytest.raises(ValidationError, match="max_context_tokens"):
        service.adaptive_context(
            "query",
            "history",
            workspace="adaptive",
            repo="context",
            max_context_tokens=32_769,
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"k": True},
        {"max_context_tokens": True},
        {"retrieval_token_budget": True},
    ],
)
def test_service_adaptive_context_rejects_boolean_policy_values(kwargs) -> None:
    service = MemoryService.create(":memory:")
    service.remember("Fact.", workspace="adaptive", repo="context")

    with pytest.raises(ValidationError):
        service.adaptive_context(
            "query",
            "history that needs routing",
            workspace="adaptive",
            repo="context",
            **kwargs,
        )


def test_service_adaptive_context_scopes_session_memories_and_rejects_foreign_sessions() -> None:
    service = MemoryService.create(":memory:")
    session = service.start_session("adaptive", repo="context", goal="routing")
    service.remember(
        "Only the release manager may approve this session deployment.",
        workspace="adaptive",
        repo="context",
        session_id=session["session_id"],
        scope="session",
        mtype="semantic",
    )
    history = "\n".join(
        f"Unrelated task history item {number}." for number in range(40)
    )

    result = service.adaptive_context(
        "Who may approve this session deployment?",
        history,
        workspace="adaptive",
        repo="context",
        session_id=session["session_id"],
        mtypes=["semantic"],
        max_context_tokens=64,
        retrieval_token_budget=32,
    )

    assert result["decision"]["mode"] == "retrieval"
    assert result["sources"]
    assert result["sources"][0]["scope"] == "session"

    foreign = service.start_session("foreign", repo="context", goal="routing")
    with pytest.raises(ValidationError, match="session_id does not belong"):
        service.adaptive_context(
            "Who may approve this session deployment?",
            history,
            workspace="adaptive",
            repo="context",
            session_id=foreign["session_id"],
            max_context_tokens=64,
            retrieval_token_budget=32,
        )


def test_service_adaptive_context_records_content_free_routing_receipt() -> None:
    service = MemoryService.create(":memory:")
    service.remember(
        "The release manager owns deployment approval.",
        workspace="adaptive",
        repo="context",
    )
    history = "\n".join(
        f"Unrelated task history item {number}." for number in range(40)
    )

    result = service.adaptive_context(
        "Who owns deployment approval?",
        history,
        workspace="adaptive",
        repo="context",
        max_context_tokens=64,
        retrieval_token_budget=32,
    )

    receipt = result["receipt"]
    assert receipt["operation"] == "adaptive_context"
    assert receipt["metadata"]["adaptive_mode"] == "retrieval"
    assert "release manager" not in str(receipt).casefold()
    assert "unrelated task history" not in str(receipt).casefold()
    savings = service.context_savings(workspace="adaptive", repo="context")
    adaptive = next(
        item
        for bucket in savings["by_token_counter"]
        for item in bucket["by_operation"]
        if item["operation"] == "adaptive_context"
    )
    assert adaptive["receipt_count"] == 1
    assert adaptive["saved_tokens"] > 0
