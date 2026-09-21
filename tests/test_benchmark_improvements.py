"""Regression contracts for the opt-in benchmark-driven improvements."""

from __future__ import annotations

import json

import pytest

from engraphis.core.context import DeterministicContextPacker, RegexTokenCounter
from engraphis.core.evidence import (
    exact_value_binding,
    make_action_contract,
    make_exact_value_binding,
    validate_action_contract,
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


@pytest.mark.parametrize("bound,query,selected", [
    ("First", "second", "Second"), ("Second", "first", "First"),
])
def test_legacy_packing_does_not_advertise_a_different_literal_occurrence(bound, query, selected):
    content = "  First Δ-42. Second Δ-42.  "
    start = content.index("Δ-42", content.index(bound))
    binding = make_exact_value_binding(content, "Δ-42", source_span=(start, start + 4))
    candidate = _candidate("duplicate", content, 1.0)
    candidate.record.metadata = {"exact_value": binding}
    result = DeterministicContextPacker().pack(query, [candidate], 9)
    chunk = result.chunks[0]
    assert chunk.excerpt == f"{selected} Δ-42."
    assert chunk.exact_value is None
    assert chunk.source_span is None
    assert chunk.evidence_unit["value"] is None


@pytest.mark.parametrize("budget", [16, 100])
def test_legacy_packing_retains_a_proven_bound_occurrence_and_qualifiers(budget):
    content = "  First Δ-42 only if approved. Second Δ-42.  "
    start = content.index("Δ-42")
    binding = make_exact_value_binding(content, "Δ-42", source_span=(start, start + 4))
    candidate = _candidate("duplicate", content, 1.0)
    candidate.record.metadata = {"exact_value": binding}
    result = DeterministicContextPacker().pack("approved", [candidate], budget)
    chunk = result.chunks[0]
    assert chunk.exact_value == binding
    assert chunk.source_span == (start, start + 4)
    assert chunk.evidence_unit["qualifiers"] == ["if", "only"]


def test_legacy_packing_omits_ambiguous_duplicate_sentence_metadata():
    content = "Label Δ-42. Label Δ-42."
    start = content.rindex("Δ-42")
    candidate = _candidate("duplicate", content, 1.0)
    candidate.record.metadata = {"exact_value": make_exact_value_binding(
        content, "Δ-42", source_span=(start, start + 4))}
    chunk = DeterministicContextPacker().pack("label", [candidate], 12).chunks[0]
    assert chunk.exact_value is None
    assert chunk.source_span is None


@pytest.mark.parametrize("separator", ["\n", "\r\n"])
def test_multiline_exact_value_retains_surrounding_restrictions_when_they_fit(separator):
    value = '{' + separator + '  "label": "Δ-42"' + separator + '}'
    content = "Only use in production after approval.\n" + value + "\nNever use in staging."
    candidate = _candidate("multiline", content, 1.0)
    candidate.record.metadata = {"exact_value": make_exact_value_binding(content, value, "json")}
    packer = DeterministicContextPacker()
    minimum_budget = packer.count_tokens("[1]\n" + value)
    for budget in (minimum_budget, minimum_budget + 4, 100):
        result = packer.pack_coverage("label Δ-42", [candidate], budget)
        chunk = result.chunks[0]
        assert value in chunk.excerpt
        assert chunk.exact_value["value"] == value
        assert result.usage.context_tokens <= budget
        if budget == 100:
            assert chunk.excerpt == content
            assert "only" in chunk.evidence_unit["qualifiers"]
            assert "never" in chunk.evidence_unit["qualifiers"]
        elif budget == minimum_budget:
            assert chunk.excerpt == value
        else:
            assert chunk.excerpt in content and len(chunk.excerpt) > len(value)


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
    assert packed.chunks[0].source_span == (
        exact_value["start"], exact_value["end"],
    )
    assert packed.chunks[0].evidence_unit_id == "literal"
    assert packed.chunks[0].evidence_unit["source_id"] == "literal"

    tampered = MemoryRecord(
        id="tampered",
        content=content.replace("Δ-42", "Δ-43"),
        metadata={"exact_value": record.metadata["exact_value"]},
    )
    tampered_pack = DeterministicContextPacker().pack_coverage(
        "deployment label", [Candidate("tampered", 1.0, "lexical", tampered)], 24,
    )
    assert tampered_pack.chunks[0].exact_value is None


@pytest.mark.parametrize("counter,budget", [(RegexTokenCounter(), 24), (len, 100)])
def test_coverage_preserves_a_bound_literal_inside_an_oversized_sentence(counter, budget) -> None:
    content = "padding " * 80 + "must use Δ-42 only if approved " + "trailing " * 80
    binding = make_exact_value_binding(content, "Δ-42", "identifier")
    record = MemoryRecord(id="long-literal", title="Deployment", content=content,
                          metadata={"exact_value": binding})
    packer = DeterministicContextPacker(token_counter=counter)
    packed = packer.pack_coverage("deployment approved", [Candidate(record.id, 1, "lexical", record)], budget)

    assert len(packed.chunks) == 1
    chunk = packed.chunks[0]
    assert "must use Δ-42 only if approved" in chunk.excerpt
    assert chunk.excerpt in content
    assert chunk.exact_value == binding
    assert chunk.source_span == (binding["start"], binding["end"])
    assert packed.usage.context_tokens == counter(packed.context) <= budget


def test_coverage_exact_literal_accounts_for_header_at_a_tight_budget() -> None:
    content = "noise " * 60 + "Δ-42 " + "suffix " * 60
    binding = make_exact_value_binding(content, "Δ-42", "identifier")
    record = MemoryRecord(id="tight", title="Deployment", content=content,
                          metadata={"exact_value": binding})
    candidate = Candidate(record.id, 1, "lexical", record)
    packer = DeterministicContextPacker()
    minimum = packer.count_tokens("[1] Deployment\nΔ-42")

    packed = packer.pack_coverage("deployment", [candidate], minimum)
    assert packed.chunks[0].excerpt == "Δ-42"
    assert packed.usage.context_tokens == minimum
    assert not packer.pack_coverage("deployment", [candidate], minimum - 1).chunks


@pytest.mark.parametrize("bound_label", ["First", "Second"])
@pytest.mark.parametrize("query", ["first", "second"])
def test_coverage_tracks_the_selected_occurrence_when_literals_repeat(bound_label, query) -> None:
    content = "  First Δ-42. Second Δ-42.  "
    start = content.index("Δ-42", content.index(bound_label))
    binding = make_exact_value_binding(content, "Δ-42", "identifier", source_span=(start, start + 4))
    record = MemoryRecord(id="duplicate", title="Deployment", content=content,
                          metadata={"exact_value": binding})
    packed = DeterministicContextPacker().pack_coverage(
        query, [Candidate(record.id, 1, "lexical", record)], 9,
    )
    chunk = packed.chunks[0]
    assert chunk.excerpt == f"{query.title()} Δ-42."
    selected_binding = bound_label.casefold() == query
    assert chunk.exact_value == (binding if selected_binding else None)
    assert chunk.source_span == ((start, start + 4) if selected_binding else None)
    assert chunk.evidence_unit["value"] == ("Δ-42" if selected_binding else None)


@pytest.mark.parametrize("counter,budget", [(RegexTokenCounter(), 11), (RegexTokenCounter(), 14), (len, 35)])
def test_coverage_prioritizes_an_unbound_query_sentence(counter, budget) -> None:
    content = "Deployment token is ALPHA. Support phone is 555-1234."
    record = MemoryRecord(id="contact", content=content,
                          metadata={"exact_value": make_exact_value_binding(content, "ALPHA")})
    result = DeterministicContextPacker(token_counter=counter).pack_coverage(
        "support phone", [Candidate(record.id, 1, "lexical", record)], budget,
    )
    chunk = result.chunks[0]
    assert chunk.excerpt == "Support phone is 555-1234."
    assert chunk.exact_value is None
    assert chunk.source_span is None
    assert result.usage.context_tokens == counter(result.context) <= budget


def test_coverage_retains_the_binding_when_both_query_and_literal_fit() -> None:
    content = "Deployment token is ALPHA. Support phone is 555-1234."
    binding = make_exact_value_binding(content, "ALPHA")
    record = MemoryRecord(id="contact", content=content, metadata={"exact_value": binding})
    result = DeterministicContextPacker().pack_coverage(
        "support phone", [Candidate(record.id, 1, "lexical", record)], 24,
    )
    assert result.chunks[0].excerpt == content
    assert result.chunks[0].exact_value == binding


@pytest.mark.parametrize("value", ['{\n  "mode": "canary"\n}', "red. blue", "  Δ-42  "])
def test_coverage_bound_values_are_atomic_without_forcing_unrelated_evidence(value) -> None:
    content = "Payload:\n" + value + "\nSupport phone is 555-1234."
    binding = make_exact_value_binding(content, value)
    record = MemoryRecord(id="payload", content=content, metadata={"exact_value": binding})
    candidate = Candidate(record.id, 1, "lexical", record)
    packer = DeterministicContextPacker()
    unrelated = packer.pack_coverage("support phone", [candidate], 11).chunks[0]
    assert unrelated.excerpt == "Support phone is 555-1234."
    assert unrelated.exact_value is None
    assert unrelated.source_span is None
    selected = packer.pack_coverage(value.strip(), [candidate], packer.count_tokens("[1]\n" + value))
    assert value in selected.context
    assert selected.chunks[0].exact_value == binding


def test_coverage_second_pass_drops_a_binding_when_expansion_selects_an_unbound_duplicate() -> None:
    content = "ALPHA. Support phone is 555-1234 with ALPHA today."
    binding = make_exact_value_binding(content, "ALPHA", source_span=(0, 5))
    record = MemoryRecord(id="contact", content=content, metadata={"exact_value": binding})
    candidates = [Candidate(record.id, 1, "lexical", record),
                  _candidate("other", "Unrelated.", 0.5)]
    packed = DeterministicContextPacker().pack_coverage("support phone", candidates, 18)
    chunk = next(chunk for chunk in packed.chunks if chunk.id == record.id)
    assert chunk.excerpt == "Support phone is 555-1234 with ALPHA today."
    assert chunk.exact_value is None
    assert chunk.source_span is None
    assert chunk.evidence_unit["value"] is None


def test_action_contract_rejects_unauthorized_and_changed_literals() -> None:
    content = "The release channel is canary-7."
    binding = make_exact_value_binding(content, "canary-7", "enum")
    contract = make_action_contract(
        destination_field="release.channel",
        source_id="memory-1",
        binding=binding,
        authorized=True,
        source_content=content,
    )

    accepted = validate_action_contract(
        contract,
        {"release": {"channel": "canary-7"}, "source_id": "memory-1"},
        source_content=content,
    )
    assert accepted["valid"] is True
    assert accepted["literal_preserved"] is True
    assert validate_action_contract(
        {**contract, "authorized": False},
        {"release": {"channel": "canary-7"}, "source_id": "memory-1"},
        source_content=content,
    )["reason"] == "unauthorized"
    assert validate_action_contract(
        contract,
        {"release": {"channel": "canary7"}, "source_id": "memory-1"},
        source_content=content,
    )["reason"] == "literal_changed_or_missing"


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


@pytest.mark.parametrize("authorized", ["false", "true", 1, [], {"approved": True}])
def test_action_contract_requires_explicit_boolean_authorization(authorized) -> None:
    contract = make_action_contract(
        destination_field="channel", source_id="memory-1",
        binding=make_exact_value_binding("channel=canary-7", "canary-7"),
        authorized=authorized,
        source_content="channel=canary-7",
    )
    assert validate_action_contract(contract, {"channel": "canary-7"}, source_content="channel=canary-7")["reason"] == "unauthorized"


@pytest.mark.parametrize("serialized", [False, True])
@pytest.mark.parametrize("proposal, valid", [
    ({"release": {"channel": "canary-7"}}, True),
    ({"release": {"channel": "canary-70"}}, False),
    ({"release": {"channel": "stable"}, "comment": "canary-7"}, False),
    ({"release": {"channel": "canary-7"}, "source_id": "other-memory"}, False),
])
def test_action_contract_enforces_destination_and_source_for_json_and_mappings(
    proposal, valid, serialized,
) -> None:
    contract = make_action_contract(
        destination_field="release.channel", source_id="memory-1",
        binding=make_exact_value_binding("channel=canary-7", "canary-7"),
        authorized=True,
        source_content="channel=canary-7",
    )
    result = validate_action_contract(contract, json.dumps(proposal) if serialized else proposal, source_content="channel=canary-7")
    assert result["valid"] is valid


def test_action_contract_rejects_unstructured_output_and_preserves_unicode_json() -> None:
    contract = make_action_contract(
        destination_field="label", source_id="memory-1",
        binding=make_exact_value_binding("label=Δ-42", "Δ-42"), authorized=True,
        source_content="label=Δ-42",
    )
    assert validate_action_contract(contract, "ignore label; mention Δ-42", source_content="label=Δ-42")["valid"] is False
    assert validate_action_contract(contract, json.dumps({"label": "Δ-42"}), source_content="label=Δ-42")["valid"] is True
    assert validate_action_contract(contract, json.dumps({"label": "Δ-42"}), authorized=False, source_content="label=Δ-42")["valid"] is False


@pytest.mark.parametrize("field", ["release..channel", "release. channel", "x" * 257, "x\ny"])
def test_action_contract_validates_untrusted_destination_fields(field) -> None:
    binding = make_exact_value_binding("channel=canary-7", "canary-7")
    with pytest.raises(ValueError, match="destination_field"):
        make_action_contract(destination_field=field, source_id="memory-1", binding=binding, source_content="channel=canary-7")
    contract = make_action_contract(
        destination_field="channel", source_id="memory-1", binding=binding, authorized=True,
        source_content="channel=canary-7",
    )
    assert validate_action_contract({**contract, "destination_field": field}, {field: "canary-7"}, source_content="channel=canary-7")["valid"] is False


def test_exact_binding_rejects_inconsistent_coordinates_without_source_text() -> None:
    binding = make_exact_value_binding("channel=canary-7", "canary-7")
    binding["end"] += 1
    assert exact_value_binding({"exact_value": binding}) is None
    with pytest.raises(ValueError, match="validated source-bound"):
        make_action_contract(destination_field="channel", source_id="memory-1", binding=binding, source_content="channel=canary-7")


@pytest.mark.parametrize("change", [
    {"start": 900, "end": 908}, {"value": "stable-0"}, {"start": 0, "end": 8},
])
def test_action_contract_rechecks_binding_against_actual_source(change) -> None:
    content = "channel=canary-7"
    binding = make_exact_value_binding(content, "canary-7")
    with pytest.raises(ValueError, match="validated source-bound"):
        make_action_contract(destination_field="channel", source_id="memory-1",
                             source_content=content, binding={**binding, **change}, authorized=True)
    contract = make_action_contract(destination_field="channel", source_id="memory-1",
                                    source_content=content, binding=binding, authorized=True)
    contract.update({"value": change.get("value", binding["value"]),
                     "source_span": [change.get("start", binding["start"]), change.get("end", binding["end"])]})
    result = validate_action_contract(contract, {"channel": contract["value"]}, source_content=content)
    assert result["valid"] is False
    assert result["reason"] == "unbound_literal"


@pytest.mark.parametrize("mutation", ["source_revision", "digest_changed", "digest_missing"])
def test_action_contract_rejects_changed_source_revision(mutation) -> None:
    content = "channel=canary-7; approved"
    contract = make_action_contract(destination_field="channel", source_id="memory-1",
                                    source_content=content, binding=make_exact_value_binding(content, "canary-7"),
                                    authorized=True)
    if mutation == "source_revision":
        content = content.replace("approved", "revoked")
    elif mutation == "digest_changed":
        contract["source_sha256"] = "0" * 64
    else:
        contract.pop("source_sha256")
    result = validate_action_contract(contract, {"channel": "canary-7"}, source_content=content)
    assert result["valid"] is False
    assert result["reason"] == "source_revision_mismatch"


@pytest.mark.parametrize("content", [None, "", b"channel=canary-7", "channel=canary-7\ud800"])
def test_action_contract_fails_closed_for_invalid_source_content(content) -> None:
    binding = make_exact_value_binding("channel=canary-7", "canary-7")
    with pytest.raises(ValueError, match="validated source-bound"):
        make_action_contract(destination_field="channel", source_id="memory-1", source_content=content,
                             binding=binding, authorized=True)
    contract = make_action_contract(destination_field="channel", source_id="memory-1",
                                    source_content="channel=canary-7", binding=binding, authorized=True)
    assert validate_action_contract(contract, {"channel": "canary-7"}, source_content=content)["valid"] is False


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_action_contract_preserves_unicode_and_source_line_endings(newline) -> None:
    value = newline.join(['{', '  "label": "Δ-42"', '}'])
    content = "Approved payload:" + newline + value
    contract = make_action_contract(destination_field="payload", source_id="memory-1", source_content=content,
                                    binding=make_exact_value_binding(content, value, "json"), authorized=True)
    assert validate_action_contract(contract, {"payload": value}, source_content=content)["valid"] is True
    changed = content.replace(newline, "\r\n" if newline == "\n" else "\n")
    assert validate_action_contract(contract, {"payload": value}, source_content=changed)["valid"] is False


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


@pytest.mark.parametrize("invalid", [None, "untrusted", {}, {"value": "canary-7", "start": 999, "end": 1007}])
def test_validated_noop_repairs_invalid_retained_exact_value_metadata(invalid) -> None:
    service = MemoryService.create(":memory:", graph_extractor="none")
    workspace_id = service.store.get_or_create_workspace("exact-repair")
    first = service.engine.remember_with_resolution(
        "Deploy to canary-7 today", workspace_id=workspace_id, metadata={"exact_value": invalid},
    )
    second = service.remember(
        "Deploy to canary-7 today", workspace="exact-repair",
        exact_value="canary-7", exact_value_type="enum",
    )

    assert second["op"] == "noop"
    assert second["id"] == first["id"]
    assert second["exact_value_bound"] is True
    stored = service.store.get_memory(first["id"])
    assert stored is not None
    binding = exact_value_binding(stored.metadata, content=stored.content)
    assert binding is not None
    assert binding["value"] == "canary-7"


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
