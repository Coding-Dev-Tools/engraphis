"""Focused contracts for deterministic, budgeted context packing."""

from __future__ import annotations

import pytest
from typing import Optional

from engraphis.core.context import DeterministicContextPacker, RegexTokenCounter
from engraphis.core.interfaces import Candidate, MemoryRecord


def _candidate(
    memory_id: str,
    content: str,
    *,
    score: float = 1.0,
    arm: str = "semantic",
    title: str = "Deployment",
    summary: str = "",
    metadata: Optional[dict[str, object]] = None,
) -> Candidate:
    return Candidate(
        id=memory_id,
        score=score,
        arm=arm,
        record=MemoryRecord(
            id=memory_id,
            title=title,
            content=content,
            summary=summary,
            repo_id="repo_demo",
            metadata=metadata or {},
        ),
    )


def test_strict_budget_holds_when_the_first_source_is_oversized() -> None:
    packer = DeterministicContextPacker()
    candidate = _candidate(
        "mem_oversized",
        "Deploys must not run until backup passes. "
        "This supporting explanation is deliberately much longer than the available budget. "
        * 6,
    )

    context, chunks, usage = packer.pack("deploy backup", [candidate], token_budget=25)

    assert chunks
    assert chunks[0].truncated is True
    assert usage.context_tokens == RegexTokenCounter()(context)
    assert usage.context_tokens <= usage.budget_tokens == 25


def test_unfit_header_does_not_block_a_later_compact_source() -> None:
    packer = DeterministicContextPacker()
    candidates = [
        _candidate(
            "mem_long_header",
            "Alpha.",
            score=1.0,
            title="A title whose many separate words consume the entire tiny token budget",
        ),
        _candidate(
            "mem_compact",
            "Beta.",
            score=0.9,
            title="",
        ),
    ]

    context, chunks, usage = packer.pack("evidence", candidates, token_budget=6)

    assert [chunk.id for chunk in chunks] == ["mem_compact"]
    assert "Beta." in context
    assert usage.context_tokens <= 6


def test_sentence_excerpt_marks_omission_and_preserves_qualifying_evidence() -> None:
    packer = DeterministicContextPacker()
    candidate = _candidate(
        "mem_qualifier",
        "Deploys are normally routine. "
        "Deploys must not run until backup passes. "
        "This unrelated operational history is only background. "
        "More unrelated detail follows here.",
        summary="Deploys run after backup.",
    )

    _, chunks, _ = packer.pack("deploy backup", [candidate], token_budget=25)

    assert len(chunks) == 1
    assert chunks[0].reason == "relevant_sentence_excerpt"
    assert "must not run until backup passes" in chunks[0].excerpt.casefold()
    assert "[…]" in chunks[0].excerpt


def test_qualifier_preserving_summary_is_preferred_when_it_fits() -> None:
    packer = DeterministicContextPacker()
    summary = "Deploys must not run until backup passes."
    candidate = _candidate(
        "mem_summary",
        ("Deploys must not run until backup passes. " * 10).strip(),
        summary=summary,
    )

    _, chunks, _ = packer.pack("deploy backup", [candidate], token_budget=30)

    assert len(chunks) == 1
    assert chunks[0].reason == "summary"
    assert chunks[0].excerpt == summary
    assert chunks[0].truncated is True


def test_sentence_aligned_summary_excerpt_leaves_room_for_more_evidence() -> None:
    packer = DeterministicContextPacker()
    candidates = [
        _candidate(
            "mem_rollout",
            "The release ledger records extensive historical rollout details. "
            "Platform Reliability owns deployment evidence and maintains the signed "
            "release ledger with the complete verification record for every rollout.",
            score=1.0,
            title="",
            summary=(
                "Platform Reliability owns deployment evidence. "
                "The signed release ledger keeps the complete verification record for "
                "every production rollout and post-release review, including approvals, "
                "rollbacks, and independently signed audit receipts."
            ),
        ),
        _candidate(
            "mem_owner",
            "The service owner is Platform Reliability.",
            score=0.9,
            title="",
        ),
    ]

    _, chunks, usage = packer.pack(
        "deployment evidence owner", candidates, token_budget=28
    )

    assert [chunk.id for chunk in chunks] == ["mem_rollout", "mem_owner"]
    assert chunks[0].reason == "summary_excerpt"
    assert chunks[0].excerpt == "Platform Reliability owns deployment evidence. […]"
    assert usage.context_tokens <= usage.budget_tokens == 28


def test_summary_must_preserve_every_qualifier_before_it_can_replace_source() -> None:
    packer = DeterministicContextPacker()
    candidate = _candidate(
        "mem_partial_qualifier",
        "Deploys must run only when backup passes unless incident command grants an exception.",
        summary="Deploys must run when backup passes.",
    )

    _, chunks, _ = packer.pack("deploy backup", [candidate], token_budget=40)

    assert len(chunks) == 1
    assert chunks[0].reason == "full"
    assert "only when backup passes unless" in chunks[0].excerpt.casefold()


def test_tight_excerpt_prefers_a_separate_qualifier_sentence() -> None:
    packer = DeterministicContextPacker()
    candidate = _candidate(
        "mem_separate_qualifier",
        "Deploys run after backup. "
        "Unless incident command approves an exception, deploys must not run.",
        title="",
    )

    _, chunks, _ = packer.pack("deploy backup", [candidate], token_budget=18)

    assert len(chunks) == 1
    assert "unless" in chunks[0].excerpt.casefold()
    assert "must not run" in chunks[0].excerpt.casefold()


def test_tight_budget_omits_evidence_if_a_late_qualifier_cannot_survive() -> None:
    packer = DeterministicContextPacker()
    candidate = _candidate(
        "mem_late_qualifier",
        "Use the fast deployment path in all environments except production.",
        title="",
    )

    context, chunks, usage = packer.pack(
        "deployment path",
        [candidate],
        token_budget=8,
    )

    assert context == ""
    assert chunks == []
    assert usage.packed_count == 0


@pytest.mark.parametrize("qualifier", ["without production approval", "but cannot run in production"])
def test_tight_budget_does_not_strip_other_negative_qualifiers(qualifier: str) -> None:
    packer = DeterministicContextPacker()
    candidate = _candidate(
        "mem_negative_qualifier",
        f"Use the fast deployment path {qualifier}.",
        title="",
    )

    context, chunks, usage = packer.pack(
        "deployment path",
        [candidate],
        token_budget=8,
    )

    assert context == ""
    assert chunks == []
    assert usage.packed_count == 0


def test_custom_counter_can_truncate_inside_one_regex_token() -> None:
    class CharacterCounter:
        identity = "test.characters"

        def __call__(self, text: str) -> int:
            return len(text)

    counter = CharacterCounter()
    packer = DeterministicContextPacker(counter)
    candidate = _candidate(
        "mem_single_token",
        "x" * 200,
        title="",
    )

    context, chunks, usage = packer.pack(
        "x",
        [candidate],
        token_budget=24,
    )

    assert chunks and chunks[0].truncated
    assert usage.context_tokens == counter(context)
    assert 0 < usage.context_tokens <= usage.budget_tokens == 24


def test_supersession_and_claim_family_deduplication_keep_best_candidate() -> None:
    packer = DeterministicContextPacker()
    candidates = [
        _candidate(
            "mem_original",
            "The original rollout policy is recorded here.",
            score=0.6,
            metadata={"claim_key": "rollout-policy"},
        ),
        _candidate(
            "mem_revision",
            "The revised rollout policy replaces the original.",
            score=0.8,
            metadata={"claim_key": "rollout-policy", "supersedes": "mem_original"},
        ),
        _candidate(
            "mem_latest",
            "The current rollout policy is authoritative.",
            score=0.9,
            metadata={"supersedes": ["mem_revision"]},
        ),
    ]

    _, chunks, usage = packer.pack("current rollout policy", candidates, token_budget=80)

    assert [chunk.id for chunk in chunks] == ["mem_latest"]
    assert usage.packed_count == 1
    assert usage.omitted_count == 2


def test_legacy_subject_key_families_keep_distinct_claim_kinds() -> None:
    packer = DeterministicContextPacker()
    candidates = [
        _candidate(
            "mem_owner",
            "The service owner is the platform team.",
            score=0.9,
            metadata={"subject_key": "service.api", "claim_kind": "owner"},
        ),
        _candidate(
            "mem_status",
            "The service status is in maintenance.",
            score=0.8,
            metadata={"subject_key": "service.api", "claim_kind": "status"},
        ),
    ]

    _, chunks, usage = packer.pack("service owner and status", candidates, token_budget=80)

    assert [chunk.id for chunk in chunks] == ["mem_owner", "mem_status"]
    assert usage.omitted_count == 0


@pytest.mark.parametrize("bridge_arm", ["graph", "code"])
def test_graph_and_code_bridge_evidence_gets_selected_for_bridge_queries(bridge_arm: str) -> None:
    packer = DeterministicContextPacker()
    candidates = [
        _candidate(
            "mem_vector",
            "Dependency path summary is only generic information.",
            score=0.60,
        ),
        _candidate(
            "mem_bridge",
            "Dependency path provides decisive evidence.",
            score=0.50,
            arm=bridge_arm,
        ),
    ]

    _, chunks, _ = packer.pack("why dependency path", candidates, token_budget=20)

    assert chunks[0].id == "mem_bridge"
    assert chunks[0].reason == "bridge_evidence"


def test_packing_is_deterministic_and_reports_exact_usage_accounting() -> None:
    packer = DeterministicContextPacker()
    candidates = [
        _candidate("mem_a", "Alpha deployment evidence. Additional context follows."),
        _candidate("mem_b", "Beta deployment evidence. Extra details follow.", score=0.9),
    ]

    first = packer.pack("deployment evidence", candidates, token_budget=60)
    second = packer.pack("deployment evidence", candidates, token_budget=60)
    context, chunks, usage = first

    assert first == second
    assert usage.context_tokens == RegexTokenCounter()(context)
    assert usage.source_tokens == sum(
        RegexTokenCounter()(f"{candidate.record.title}\n{candidate.record.content}")
        for candidate in candidates
        if candidate.record is not None
    )
    assert usage.saved_tokens == max(0, usage.source_tokens - usage.context_tokens)
    assert usage.savings_ratio == pytest.approx(usage.saved_tokens / usage.source_tokens)
    assert usage.packed_count == len(chunks)
    assert usage.omitted_count == len(candidates) - len(chunks)
    assert usage.token_counter == "engraphis.regex.v1"
