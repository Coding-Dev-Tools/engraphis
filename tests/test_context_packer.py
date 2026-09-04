"""Focused contracts for DeterministicContextPacker, clause redundancy pruning, and score-elbow gating."""

from __future__ import annotations

from typing import Optional

from engraphis.core.context import (
    ContextPackResult,
    DeterministicContextPacker,
    pack_context,
)
from engraphis.core.interfaces import Candidate, MemoryRecord
from tests.test_context_packing import *  # noqa: F401, F403


def _candidate_item(
    memory_id: str,
    content: str,
    *,
    score: float = 1.0,
    arm: str = "semantic",
    title: str = "Deployment Policy",
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


def test_context_pack_result_tuple_contract_and_attributes() -> None:
    packer = DeterministicContextPacker()
    c1 = _candidate_item("mem_1", "Primary deployment rules.")
    res = packer.pack("deploy", [c1], token_budget=50)

    context, chunks, usage = res
    assert isinstance(res, tuple)
    assert len(res) == 3
    assert res[0] == context
    assert res[1] == chunks
    assert res[2] == usage

    assert res.context == context
    assert res.chunks == chunks
    assert res.packed_chunks == chunks
    assert res.packed == chunks
    assert res.usage == usage


def test_pack_context_functional_api_and_method_alias() -> None:
    c1 = _candidate_item("mem_1", "Primary deployment rules.")
    res1 = pack_context("deploy", [c1], token_budget=50)
    assert isinstance(res1, ContextPackResult)
    assert res1.chunks[0].id == "mem_1"

    packer = DeterministicContextPacker()
    res2 = packer.pack_context("deploy", [c1], token_budget=50)
    assert res1 == res2


def test_inter_candidate_clause_redundancy_pruning_packs_novel_delta() -> None:
    packer = DeterministicContextPacker()
    # Candidate 1 establishes the rule
    c1 = _candidate_item(
        "mem_primary",
        "Production deployments require approval from the release manager before rollout. "
        "Database migrations must run during the off-peak maintenance window.",
        score=0.95,
        title="Production Deployment Guide",
    )
    # Candidate 2 duplicates sentence 1 verbatim, but adds a novel sentence
    c2 = _candidate_item(
        "mem_checklist",
        "Production deployments require approval from the release manager before rollout. "
        "Canary analysis must run for 30 minutes before full promotion.",
        score=0.85,
        title="Release Checklist",
    )

    context, chunks, usage = packer.pack("deployment policy", [c1, c2], token_budget=150)

    assert len(chunks) == 2
    assert chunks[0].id == "mem_primary"
    assert chunks[1].id == "mem_checklist"

    assert "Production deployments require approval" in chunks[0].excerpt
    assert "Database migrations must run" in chunks[0].excerpt

    assert "Canary analysis must run for 30 minutes" in chunks[1].excerpt
    assert "Production deployments require approval" not in chunks[1].excerpt
    assert chunks[1].truncated is True
    assert chunks[1].reason == "novel_delta"

    assert context.count("Production deployments require approval") == 1
    assert "[2] Release Checklist\nCanary analysis must run" in context


def test_completely_redundant_candidate_is_omitted() -> None:
    packer = DeterministicContextPacker()
    c1 = _candidate_item(
        "mem_first",
        "Production deployments require approval from the release manager before rollout.",
        score=0.95,
        title="Release Rule",
    )
    c2 = _candidate_item(
        "mem_second",
        "Production deployments require approval from the release manager before rollout.",
        score=0.90,
        title="Duplicate Rule",
    )

    context, chunks, usage = packer.pack("deployment approval", [c1, c2], token_budget=100)

    assert len(chunks) == 1
    assert chunks[0].id == "mem_first"
    assert "[2]" not in context
    assert usage.packed_count == 1
    assert usage.omitted_count == 1


def test_redundancy_pruning_preserves_qualifier_modifications() -> None:
    packer = DeterministicContextPacker()
    c1 = _candidate_item(
        "mem_base",
        "Production deployments require approval from the release manager before rollout.",
        score=0.95,
    )
    c2 = _candidate_item(
        "mem_exception",
        "Production deployments require approval from the release manager before rollout, "
        "unless an emergency hotfix is authorized by the CTO.",
        score=0.88,
        title="Emergency Override",
    )

    context, chunks, usage = packer.pack("deployment approval", [c1, c2], token_budget=150)

    assert len(chunks) == 2
    assert "unless an emergency hotfix is authorized by the CTO" in chunks[1].excerpt


def test_elastic_score_elbow_gating_prunes_low_confidence_tail() -> None:
    packer = DeterministicContextPacker()
    c1 = _candidate_item(
        "mem_high1",
        "Rollout window is between 02:00 and 04:00 UTC.",
        score=0.95,
        title="Window",
    )
    c2 = _candidate_item(
        "mem_high2",
        "Rollout team must be on call during the window.",
        score=0.90,
        title="Team",
    )
    c3 = _candidate_item(
        "mem_tail1",
        "Random unrelated note mentioning deploy casually.",
        score=0.12,
        title="Unrelated 1",
    )
    c4 = _candidate_item(
        "mem_tail2",
        "Another noisy mention from months ago.",
        score=0.08,
        title="Unrelated 2",
    )

    context, chunks, usage = packer.pack(
        "rollout window", [c1, c2, c3, c4], token_budget=200
    )

    assert [chunk.id for chunk in chunks] == ["mem_high1", "mem_high2"]
    assert usage.packed_count == 2
    assert usage.omitted_count == 2


def test_elastic_score_elbow_preserves_gradual_score_decline() -> None:
    packer = DeterministicContextPacker()
    candidates = [
        _candidate_item("mem_1", "Primary fact Alpha.", score=0.90, title="Alpha"),
        _candidate_item("mem_2", "Secondary fact Beta.", score=0.78, title="Beta"),
        _candidate_item("mem_3", "Tertiary fact Gamma.", score=0.68, title="Gamma"),
    ]

    _, chunks, usage = packer.pack("fact inquiry", candidates, token_budget=150)

    assert [chunk.id for chunk in chunks] == ["mem_1", "mem_2", "mem_3"]
    assert usage.packed_count == 3


def test_elastic_score_elbow_preserves_bridge_arm_evidence() -> None:
    packer = DeterministicContextPacker()
    c1 = _candidate_item(
        "mem_vector",
        "Generic architecture notes.",
        score=0.85,
        arm="semantic",
        title="Notes",
    )
    c2 = _candidate_item(
        "mem_bridge",
        "Service auth calls database cluster directly.",
        score=0.32,
        arm="graph",
        title="Dependency Graph",
    )

    _, chunks, usage = packer.pack(
        "why dependency path between auth and database",
        [c1, c2],
        token_budget=100,
    )

    assert any(chunk.id == "mem_bridge" for chunk in chunks)


def test_toggling_redundancy_pruning_and_elbow_gating_flags() -> None:
    unpruned_packer = DeterministicContextPacker(redundancy_pruning=False)
    c1 = _candidate_item("mem_1", "Deployments must pass all checks.", score=0.95)
    c2 = _candidate_item("mem_2", "Deployments must pass all checks.", score=0.90)

    _, chunks_unpruned, _ = unpruned_packer.pack("deploy checks", [c1, c2], token_budget=100)
    assert len(chunks_unpruned) == 2

    ungated_packer = DeterministicContextPacker(score_elbow_gating=False)
    c_high = _candidate_item("mem_h", "High relevance fact.", score=0.95)
    c_tail = _candidate_item("mem_t", "Tail fact.", score=0.10)

    _, chunks_ungated, _ = ungated_packer.pack("relevance", [c_high, c_tail], token_budget=100)
    assert len(chunks_ungated) == 2
