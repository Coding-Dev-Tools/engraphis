"""Regression contracts for the opt-in benchmark-driven improvements."""

from __future__ import annotations

import pytest

from engraphis.core.context import DeterministicContextPacker, RegexTokenCounter
from engraphis.core.evidence import (
    exact_value_binding,
    make_exact_value_binding,
    validate_exact_copy,
)
from engraphis.core.interfaces import Candidate, MemoryRecord
from engraphis.core.recall import _pack_context
from engraphis.core.retrieval_policy import apply_retrieval_recipe
from engraphis.service import MemoryService


def _candidate(memory_id: str, content: str, score: float) -> Candidate:
    return Candidate(
        memory_id,
        score,
        "semantic",
        MemoryRecord(id=memory_id, content=content),
    )


def test_coverage_packing_spreads_complete_units_across_long_sources() -> None:
    packer = DeterministicContextPacker()
    candidates = [
        _candidate(
            "long",
            "The rollout is blue. "
            + ("This unrelated historical explanation continues. " * 12),
            1.0,
        ),
        _candidate("approval", "The rollout is green after approval.", 0.9),
        _candidate("owner", "The owner is platform reliability.", 0.8),
    ]

    legacy = packer.pack("rollout owner", candidates, 35)
    coverage = packer.pack_coverage("rollout owner", candidates, 35)

    assert len(coverage.chunks) >= len(legacy.chunks)
    assert {chunk.id for chunk in coverage.chunks} == {"long", "approval", "owner"}
    assert all(chunk.excerpt and "[…]" not in chunk.excerpt for chunk in coverage.chunks)
    assert coverage.usage.context_tokens == RegexTokenCounter()(coverage.context)
    assert coverage.usage.context_tokens <= coverage.usage.budget_tokens


def test_coverage_packing_expands_admitted_sources_when_no_candidates_remain() -> None:
    candidates = [
        _candidate("a", "A0 w0. A1 w0. A2 w0.", 1.0),
        _candidate(
            "b",
            "B0 w0. B1 w0. B2 w0 w1 w2 w3 w4 w5 w6 w7.",
            0.9,
        ),
    ]
    packed = DeterministicContextPacker().pack_coverage("w0 w1 w2", candidates, 31)

    assert {chunk.id for chunk in packed.chunks} == {"a", "b"}
    assert "B2 w0 w1 w2 w3 w4 w5 w6 w7." in packed.chunks[0].excerpt


def test_coverage_packing_skips_an_oversized_top_source_when_a_later_unit_fits() -> None:
    candidates = [
        _candidate("oversized", " ".join(["oversized"] * 40) + ".", 1.0),
        _candidate("short", "The short source has enough evidence.", 0.9),
    ]
    packed = DeterministicContextPacker().pack_coverage("evidence", candidates, 18)

    assert [chunk.id for chunk in packed.chunks] == ["short"]
    assert packed.usage.omission_reasons["unit_too_large"] == 1


def test_coverage_mode_rejects_a_packer_without_the_coverage_extension() -> None:
    class LegacyOnlyPacker:
        def pack(self, _query, _candidates, _budget):
            raise AssertionError("legacy packer must not be used for coverage mode")

    with pytest.raises(ValueError, match="requires a ContextPacker with pack_coverage"):
        _pack_context(LegacyOnlyPacker(), "query", [], 32, "coverage")


def test_exact_value_binding_requires_a_unique_verbatim_source_span() -> None:
    content = "The deployment label is Δ-42 in production."
    binding = make_exact_value_binding(content, "Δ-42", "identifier")

    assert binding["copy_exactly"] is True
    assert content[binding["start"] : binding["end"]] == "Δ-42"
    assert validate_exact_copy(binding, "set label to Δ-42")
    assert not validate_exact_copy(binding, "set label to delta 42")
    with pytest.raises(ValueError, match="more than once"):
        make_exact_value_binding("x=42; fallback=42", "42", "number")
    assert exact_value_binding({"exact_value": {
        "value": "42", "type": "number", "source": "content", "copy_exactly": True,
    }}) is None


def test_coverage_packing_retains_source_bound_exact_value_metadata() -> None:
    content = "Use the deployment label Δ-42. Keep the production qualifier."
    record = MemoryRecord(
        id="literal",
        content=content,
        metadata={"exact_value": make_exact_value_binding(content, "Δ-42", "identifier")},
    )
    packed = DeterministicContextPacker().pack_coverage(
        "deployment label", [Candidate("literal", 1.0, "lexical", record)], 24,
    )

    assert packed.chunks
    exact_value = packed.chunks[0].exact_value
    assert exact_value is not None
    assert exact_value["value"] == "Δ-42"
    assert "Δ-42" in packed.context

    tampered = MemoryRecord(
        id="tampered",
        content=content.replace("Δ-42", "Δ-43"),
        metadata={"exact_value": record.metadata["exact_value"]},
    )
    tampered_pack = DeterministicContextPacker().pack_coverage(
        "deployment label", [Candidate("tampered", 1.0, "lexical", tampered)], 24,
    )
    assert tampered_pack.chunks[0].exact_value is None


def test_coverage_packing_preserves_titles_and_multiline_exact_values() -> None:
    content = 'JSON payload:\n{\n  "mode": "canary"\n}'
    record = MemoryRecord(
        id="multiline",
        title="Deployment\n  payload",
        content=content,
        metadata={
            "exact_value": make_exact_value_binding(
                content, '{\n  "mode": "canary"\n}', "json",
            ),
        },
    )
    packed = DeterministicContextPacker().pack_coverage(
        "deployment payload", [Candidate("multiline", 1.0, "lexical", record)], 24,
    )

    assert packed.chunks[0].title == "Deployment payload"
    assert "[1] Deployment payload" in packed.context
    assert '{\n  "mode": "canary"\n}' in packed.context
    assert packed.chunks[0].exact_value is not None


def test_engine_recipe_distinguishes_omitted_k_from_explicit_k(monkeypatch: pytest.MonkeyPatch) -> None:
    service = MemoryService.create(":memory:", graph_extractor="none")
    captured: dict[str, object] = {}
    original = service.engine.recall_engine.recall

    def spy(query, flt, **kwargs):
        captured.update(kwargs)
        return original(query, flt, **kwargs)

    monkeypatch.setattr(service.engine.recall_engine, "recall", spy)
    service.engine.recall("deployment", retrieval_recipe="conversation")

    assert captured["k"] is None
    assert captured["k_supplied"] is False


def test_service_rebinds_exact_value_on_a_reworded_deduplicated_write() -> None:
    service = MemoryService.create(":memory:", graph_extractor="none")
    first = service.remember("Deploy to canary-7 today", workspace="acme")
    second = service.remember(
        "Today deploy to canary-7",
        workspace="acme",
        exact_value="canary-7",
        exact_value_type="enum",
    )

    assert second["op"] == "noop"
    assert second["id"] == first["id"]
    assert second["exact_value_bound"] is True
    stored = service.store.get_memory(first["id"])
    assert stored is not None
    binding = exact_value_binding(stored.metadata, content=stored.content)
    assert binding is not None
    assert stored.content[binding["start"]:binding["end"]] == "canary-7"


@pytest.mark.parametrize(
    ("recipe", "expected_k", "expected_budget"),
    [("default", 8, 1500), ("conversation", 20, 1500), ("long_session", 10, 4096)],
)
def test_measured_retrieval_recipes_are_opt_in_and_bounded(
    recipe: str, expected_k: int, expected_budget: int,
) -> None:
    assert apply_retrieval_recipe(
        recipe, k=8, token_budget=1500, k_supplied=False,
        token_budget_supplied=False,
    ) == (expected_k, expected_budget, recipe)


def test_explicit_depth_and_budget_win_over_recipe() -> None:
    assert apply_retrieval_recipe(
        "conversation", k=12, token_budget=700, k_supplied=True,
        token_budget_supplied=True,
    ) == (12, 700, "conversation")


def test_service_persists_exact_value_and_reports_opt_in_controls() -> None:
    service = MemoryService.create(":memory:", graph_extractor="none")
    stored = service.remember(
        "The deployment label is Δ-42 in production.",
        workspace="acme",
        exact_value="Δ-42",
        exact_value_type="identifier",
    )

    record = service.store.get_memory(stored["id"])
    assert record is not None
    assert record.metadata["exact_value"]["value"] == "Δ-42"

    recalled = service.recall(
        "deployment label",
        workspace="acme",
        k=8,
        packing_mode="coverage",
        retrieval_recipe="conversation",
        response_mode="full",
    )
    assert recalled["packing_mode"] == "coverage"
    assert recalled["retrieval_recipe"] == "conversation"
    assert any(
        chunk.get("exact_value", {}).get("value") == "Δ-42"
        for chunk in recalled["packed_sources"]
    )


def test_service_batch_accepts_source_bound_exact_value() -> None:
    service = MemoryService.create(":memory:", graph_extractor="none")
    result = service.remember_batch(
        [{
            "content": "The release channel is canary-7.",
            "exact_value": "canary-7",
            "exact_value_type": "enum",
        }],
        workspace="acme",
    )

    record = service.store.get_memory(result["results"][0]["id"])
    assert record is not None
    assert record.metadata["exact_value"]["type"] == "enum"


def test_service_binds_exact_value_on_a_deduplicated_write() -> None:
    service = MemoryService.create(":memory:", graph_extractor="none")
    first = service.remember("release channel is canary-7", workspace="acme")
    second = service.remember(
        "release channel is canary-7",
        workspace="acme",
        exact_value="canary-7",
        exact_value_type="enum",
    )

    assert first["op"] == "add"
    assert second["op"] == "noop"
    assert second["id"] == first["id"]
    assert second["exact_value_bound"] is True
    stored = service.store.get_memory(first["id"])
    assert stored is not None
    assert stored.metadata["exact_value"]["value"] == "canary-7"


def test_unknown_scope_recall_reports_opt_in_controls_and_effective_budget() -> None:
    service = MemoryService.create(":memory:", graph_extractor="none")
    result = service.recall(
        "missing", workspace="unknown", retrieval_recipe="conversation",
        packing_mode="coverage",
    )

    assert result["packing_mode"] == "coverage"
    assert result["retrieval_recipe"] == "conversation"
    assert result["usage"]["budget_tokens"] == 1_500
