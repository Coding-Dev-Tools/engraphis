"""Exact-value groups retain complete restrictions or withhold the bound value."""

import pytest

from engraphis.core.context import DeterministicContextPacker
from engraphis.core.evidence import make_exact_value_binding
from engraphis.core.interfaces import Candidate, MemoryRecord


def _bound(content, *, value="VALUE", title="", score=1.0):
    binding = make_exact_value_binding(content, value)
    record = MemoryRecord(id="bound", content=content, title=title,
                          metadata={"exact_value": binding})
    return Candidate(record.id, score, "lexical", record), binding


@pytest.mark.parametrize("expected,prefix,budget", [
    ("VALUE filler only if approved", False, 14),
    ("VALUE filler only if approved by the release owner", False, 14),
    ("only if approved filler VALUE", True, 14),
    ("VALUE filler never for production", False, 14),
    ("VALUE filler only if approved, and never for production", False, 16),
])
def test_unpunctuated_restrictions_withhold_until_the_complete_unit_fits(expected, prefix, budget):
    noise = "deployment " * 8
    content = expected + " " + noise if prefix else noise + expected
    candidate, binding = _bound(content)
    packer = DeterministicContextPacker()
    tight = packer.pack_coverage("deployment", [candidate], budget)
    assert "VALUE" not in tight.context
    assert all(chunk.exact_value is None and chunk.source_span is None for chunk in tight.chunks)
    assert tight.usage.context_tokens <= budget
    budget = packer.count_tokens("[1]\n" + content.strip())
    result = packer.pack_coverage("deployment", [candidate], budget)
    chunk = result.chunks[0]
    assert chunk.excerpt == content.strip()
    assert chunk.exact_value == binding
    assert chunk.source_span == (binding["start"], binding["end"])
    assert result.usage.context_tokens <= budget


@pytest.mark.parametrize("separator", ["\n", "\r\n"])
@pytest.mark.parametrize("prefix", [False, True])
@pytest.mark.parametrize("period", ["", "."])
def test_wrapped_restrictions_continue_to_a_hard_boundary(separator, prefix, period):
    restriction = separator.join("only if approved by the release owner".split())
    expected = restriction + period + separator + "VALUE" if prefix else "VALUE" + separator + restriction + period
    content = expected + " " + "deployment " * 8 if prefix else "deployment " * 8 + expected
    candidate, binding = _bound(content)
    packer = DeterministicContextPacker()
    budget = packer.count_tokens("[1]\n" + expected)
    tight = packer.pack_coverage("deployment", [candidate], budget)
    assert "VALUE" not in tight.context
    assert all(chunk.exact_value is None and chunk.source_span is None for chunk in tight.chunks)
    assert tight.usage.context_tokens <= budget
    # A line wrap cannot detach the repeated words from the complete bound unit.
    budget = packer.count_tokens("[1]\n" + content.strip())
    result = packer.pack_coverage("deployment", [candidate], budget)
    assert result.chunks[0].excerpt.strip() == content.strip()
    assert result.chunks[0].exact_value == binding
    assert result.usage.context_tokens == budget


@pytest.mark.parametrize("budget", [4, 5, 6, 7])
def test_incomplete_suffix_never_exposes_a_bound_value(budget):
    candidate, _ = _bound("Use deployment VALUE only if approved.")
    result = DeterministicContextPacker().pack_coverage("deployment", [candidate], budget)
    assert result.context == ""
    assert result.chunks == []
    assert sum(result.usage.omission_reasons[key] for key in ("unit_too_large", "budget")) == 1


@pytest.mark.parametrize("budget", [8, 9, 10])
def test_both_sides_of_a_restriction_form_one_group(budget):
    content = "Only approved deployment deployment VALUE unless staging"
    candidate, binding = _bound(content)
    result = DeterministicContextPacker().pack_coverage("deployment", [candidate], budget)
    if budget < 10:
        assert result.chunks == []
        assert sum(result.usage.omission_reasons[key] for key in ("unit_too_large", "budget")) == 1
    else:
        assert result.chunks[0].excerpt == content
        assert result.chunks[0].exact_value == binding
    assert result.usage.context_tokens <= budget


def test_second_pass_cannot_restore_an_incomplete_exact_group():
    candidate, _ = _bound("deployment " * 8 + "VALUE only if approved", score=0.9)
    phone = MemoryRecord(id="phone", content="Support phone is 555-1234.")
    candidates = [candidate, Candidate(phone.id, 1.0, "lexical", phone)]
    result = DeterministicContextPacker().pack_coverage("support phone", candidates, 15)
    assert any(chunk.id == "phone" for chunk in result.chunks)
    assert "VALUE" not in result.context
    assert all(chunk.exact_value is None and chunk.source_span is None for chunk in result.chunks)
    assert result.usage.context_tokens <= 15


def test_legacy_packing_withholds_binding_when_a_later_restriction_is_omitted():
    content = (
        "Credential is ALPHA only in production. "
        "Never use this credential in staging environments under any circumstances whatsoever."
    )
    candidate, binding = _bound(content, value="ALPHA")
    packer = DeterministicContextPacker()

    tight = packer.pack("ALPHA production", [candidate], 13)
    assert tight.chunks[0].excerpt.startswith("Credential is ALPHA only in production.")
    assert "Never use this credential" not in tight.chunks[0].excerpt
    assert tight.chunks[0].exact_value is None
    assert tight.chunks[0].source_span is None
    assert tight.chunks[0].evidence_unit["value"] is None

    roomy = packer.pack(
        "ALPHA production", [candidate], packer.count_tokens("[1]\n" + content)
    )
    assert roomy.chunks[0].excerpt == content
    assert roomy.chunks[0].exact_value == binding
    assert roomy.chunks[0].source_span == (binding["start"], binding["end"])


def test_qualifier_words_inside_a_literal_are_not_surrounding_conditions():
    candidate, binding = _bound("deployment " * 8 + "ONLY plain suffix", value="ONLY")
    result = DeterministicContextPacker().pack_coverage("deployment", [candidate], 4)
    assert result.chunks[0].excerpt == "ONLY"
    assert result.chunks[0].exact_value == binding


def test_a_bounded_condition_fits_inside_an_oversized_record():
    content = "padding " * 80 + ". must use Δ-42 only if approved. " + "trailing " * 80
    candidate, binding = _bound(content, value="Δ-42", title="Deployment")
    result = DeterministicContextPacker().pack_coverage("deployment approved", [candidate], 24)
    assert "must use Δ-42 only if approved." in result.chunks[0].excerpt
    assert result.chunks[0].excerpt in content
    assert result.chunks[0].exact_value == binding
    assert result.chunks[0].source_span == (binding["start"], binding["end"])
    assert result.usage.context_tokens <= 24


def test_complete_group_charges_title_and_custom_counter_without_outer_whitespace():
    expected = "VALUE only if approved."
    content = "deployment " * 8 + expected + "   \r\n"
    candidate, binding = _bound(content, title="Release")
    packer = DeterministicContextPacker(token_counter=len)
    budget = len("[1] Release\n" + expected)
    tight = packer.pack_coverage("deployment", [candidate], budget)
    assert "VALUE" not in tight.context
    assert all(chunk.exact_value is None and chunk.source_span is None for chunk in tight.chunks)
    budget = len("[1] Release\n" + content.strip())
    result = packer.pack_coverage("deployment", [candidate], budget)
    assert result.chunks[0].excerpt == content.strip()
    assert result.chunks[0].exact_value == binding
    assert result.usage.context_tokens == budget
