"""Sourcewide exact groups retain distant restrictions or withhold the value."""

import pytest

from engraphis.core.context import DeterministicContextPacker
from engraphis.core.evidence import make_exact_value_binding
from engraphis.core.interfaces import Candidate, MemoryRecord


_PREFIX = (
    "Only use this credential in production. Neutral one. Neutral two. "
    "Credential is ALPHA."
)
_SUFFIX = (
    "Credential is ALPHA. Neutral one. Neutral two. "
    "Never use this credential in staging."
)
_BOTH = (
    "Only use this credential in production. Neutral one. "
    "Credential is ALPHA. Neutral two. Never use this credential in staging."
)


def _candidate(content: str, *, value: str = "ALPHA") -> tuple[Candidate, dict[str, object]]:
    binding = make_exact_value_binding(content, value, "identifier")
    record = MemoryRecord(
        id="bound", content=content, metadata={"exact_value": binding},
    )
    return Candidate(record.id, 1.0, "lexical", record), binding


@pytest.mark.parametrize(
    ("content", "budget", "must_retain"),
    [
        (_PREFIX, 8, False),
        (_PREFIX, 20, True),
        (_SUFFIX, 8, False),
        (_SUFFIX, 20, True),
        (_BOTH, 20, False),
        (_BOTH, 27, True),
        (
            "Only use this credential in production.\n"
            "Neutral one.\nNeutral two.\nCredential is ALPHA.",
            8,
            False,
        ),
        (
            "Only use this credential in production.\n"
            "Neutral one.\nNeutral two.\nCredential is ALPHA.",
            20,
            True,
        ),
    ],
)
def test_distant_qualifier_group_is_complete_or_withheld(
    content: str, budget: int, must_retain: bool,
) -> None:
    candidate, binding = _candidate(content)
    result = DeterministicContextPacker().pack_coverage(
        "ALPHA", [candidate], budget,
    )
    assert result.usage.context_tokens <= budget
    if must_retain:
        assert result.chunks[0].excerpt == content
        assert result.chunks[0].exact_value == binding
        assert result.chunks[0].source_span == (binding["start"], binding["end"])
        assert "ALPHA" in result.context
    else:
        assert "ALPHA" not in result.context
        assert all(chunk.exact_value is None for chunk in result.chunks)
        assert all(chunk.source_span is None for chunk in result.chunks)
        assert all(chunk.evidence_unit.get("value") is None for chunk in result.chunks)


def test_unpunctuated_restriction_scope_falls_back_to_full_record() -> None:
    content = "Only use ALPHA in production " + ("neutral " * 50) + " only if approved"
    candidate, _ = _candidate(content.replace("ALPHA", "VALUE"), value="VALUE")
    result = DeterministicContextPacker().pack_coverage(
        "VALUE", [candidate], 8,
    )
    assert "VALUE" not in result.context
    assert all(chunk.exact_value is None for chunk in result.chunks)


@pytest.mark.parametrize(
    ("content", "query", "value", "budget"),
    [
        ("deployment " * 8 + "VALUE filler only if approved", "deployment", "VALUE", 14),
        ("only if approved VALUE " + "deployment " * 8, "deployment", "VALUE", 14),
        (
            "deployment " * 5 + "VALUE neutral gap only if approved and never share",
            "deployment", "VALUE", 16,
        ),
        ("must use only if approved VALUE " + "deployment " * 8, "deployment", "VALUE", 14),
        ("deployment " * 6 + "VALUE only if approved", "deployment", "VALUE", 12),
    ],
)
def test_complete_unpunctuated_qualifier_unit_withholds_at_historical_budget(
    content: str, query: str, value: str, budget: int,
) -> None:
    candidate, _ = _candidate(content, value=value)
    result = DeterministicContextPacker().pack_coverage(query, [candidate], budget)
    assert value not in result.context
    assert all(chunk.exact_value is None for chunk in result.chunks)
    assert all(chunk.source_span is None for chunk in result.chunks)
    assert result.usage.context_tokens <= budget


@pytest.mark.parametrize("content", [
    "deployment " * 8 + "VALUE filler only if approved",
    "only if approved VALUE " + "deployment " * 8,
    "deployment " * 5 + "VALUE neutral gap only if approved and never share",
    "must use only if approved VALUE " + "deployment " * 8,
    "deployment " * 6 + "VALUE only if approved",
])
def test_unpunctuated_qualifier_unit_retains_full_source_when_roomy(content: str) -> None:
    candidate, binding = _candidate(content, value="VALUE")
    packer = DeterministicContextPacker()
    budget = packer.count_tokens("[1]\n" + content)
    result = packer.pack_coverage("deployment", [candidate], budget)
    assert result.chunks[0].excerpt == content.strip()
    assert result.chunks[0].exact_value == binding
    assert result.chunks[0].source_span == (binding["start"], binding["end"])


@pytest.mark.parametrize(
    ("content", "budget"),
    [
        ("Only use ALPHA in production", 6),
        ("Use ALPHA only in production", 7),
    ],
)
def test_same_unit_qualifier_never_exposes_partial_bound_value(
    content: str, budget: int,
) -> None:
    candidate, _ = _candidate(content)
    result = DeterministicContextPacker().pack_coverage("ALPHA", [candidate], budget)
    assert "ALPHA" not in result.context
    assert all(chunk.exact_value is None for chunk in result.chunks)
    assert result.usage.context_tokens <= budget


@pytest.mark.parametrize("content", [
    "Only use ALPHA in production",
    "Use ALPHA only in production",
])
def test_same_unit_qualifier_retains_only_as_a_complete_unit(content: str) -> None:
    candidate, binding = _candidate(content)
    packer = DeterministicContextPacker()
    budget = packer.count_tokens("[1]\n" + content)
    result = packer.pack_coverage("ALPHA", [candidate], budget)
    assert result.chunks[0].excerpt == content
    assert result.chunks[0].exact_value == binding
    assert result.chunks[0].source_span == (binding["start"], binding["end"])


def test_distant_qualifier_requires_complete_bound_unit_suffix() -> None:
    content = "Only use this credential. Credential is ALPHA in production."
    candidate, binding = _candidate(content)
    packer = DeterministicContextPacker()
    tight = packer.pack_coverage("ALPHA", [candidate], 11)
    assert "ALPHA" not in tight.context
    assert all(chunk.exact_value is None for chunk in tight.chunks)
    roomy = packer.pack_coverage("ALPHA", [candidate], 14)
    assert roomy.chunks[0].excerpt == content
    assert roomy.chunks[0].exact_value == binding


def test_whitespace_literal_between_units_keeps_detected_restrictions() -> None:
    content = "Only use the blank token. \n\nApproved."
    candidate, binding = _candidate(content, value="\n\n")
    packer = DeterministicContextPacker()
    tight = packer.pack_coverage("blank", [candidate], 3)
    assert all(chunk.exact_value is None and chunk.source_span is None for chunk in tight.chunks)
    roomy = packer.pack_coverage("blank", [candidate], 20)
    assert roomy.chunks[0].excerpt == content
    assert roomy.chunks[0].exact_value == binding


def test_literal_whitespace_outside_a_unit_is_still_atomic_with_custom_counter() -> None:
    content = "Only use ALPHA.  " + "Neutral detail. " * 8
    candidate, binding = _candidate(content, value="ALPHA.  ")
    packer = DeterministicContextPacker(token_counter=len)
    complete = "Only use ALPHA.  "
    budget = len("[1]\n" + complete)
    tight = packer.pack_coverage("ALPHA", [candidate], budget - 1)
    assert "ALPHA" not in tight.context
    assert all(chunk.exact_value is None for chunk in tight.chunks)
    partial = packer.pack_coverage("ALPHA", [candidate], budget)
    assert "ALPHA" not in partial.context
    assert all(chunk.exact_value is None for chunk in partial.chunks)
    full_budget = len("[1]\n" + content.strip())
    fitted = packer.pack_coverage("ALPHA", [candidate], full_budget)
    assert fitted.chunks[0].excerpt == content.strip()
    assert fitted.chunks[0].exact_value == binding
    assert fitted.usage.context_tokens == full_budget
