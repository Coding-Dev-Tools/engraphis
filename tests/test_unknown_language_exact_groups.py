"""Regressions for language-neutral exact-binding containment."""

import pytest

from engraphis.core.context import DeterministicContextPacker
from engraphis.core.evidence import make_exact_value_binding
from engraphis.core.interfaces import Candidate, MemoryRecord


@pytest.mark.parametrize(
    "content",
    [
        "Utilisez ALPHA uniquement en production.",
        "Use ALPHA exclusively in production.",
    ],
)
def test_unknown_language_same_unit_requires_the_complete_source(content: str) -> None:
    packer = DeterministicContextPacker()
    binding = make_exact_value_binding(content, "ALPHA", "identifier")
    record = MemoryRecord(id="bound", content=content, metadata={"exact_value": binding})
    candidate = Candidate(record.id, 1.0, "lexical", record)

    tight = packer.pack_coverage("ALPHA", [candidate], 4)
    assert not tight.chunks
    assert "ALPHA" not in tight.context

    roomy_budget = packer.count_tokens("[1]\n" + content)
    roomy = packer.pack_coverage("ALPHA", [candidate], roomy_budget)
    assert len(roomy.chunks) == 1
    assert roomy.chunks[0].excerpt == content
    assert roomy.chunks[0].exact_value == binding
    assert roomy.chunks[0].source_span == (binding["start"], binding["end"])


def test_unknown_language_later_unit_withholds_a_short_first_unit() -> None:
    content = "Credential is ALPHA. Utilisez-le uniquement en production."
    binding = make_exact_value_binding(content, "ALPHA", "identifier")
    record = MemoryRecord(id="bound", content=content, metadata={"exact_value": binding})
    candidate = Candidate(record.id, 1.0, "lexical", record)
    packer = DeterministicContextPacker()

    first_unit_budget = packer.count_tokens("[1]\nCredential is ALPHA.")
    packed = packer.pack_coverage("ALPHA", [candidate], first_unit_budget)
    assert not packed.chunks
    assert "ALPHA" not in packed.context


def test_unbound_records_keep_query_window_selection() -> None:
    content = "Deployment token is ALPHA. Support phone is 555-1234."
    record = MemoryRecord(id="plain", content=content)
    candidate = Candidate(record.id, 1.0, "lexical", record)
    packer = DeterministicContextPacker()

    budget = packer.count_tokens("[1]\nDeployment token is ALPHA.")
    packed = packer.pack_coverage("deployment token", [candidate], budget)
    assert packed.chunks[0].excerpt == "Deployment token is ALPHA."
    assert packed.chunks[0].exact_value is None
    assert packed.chunks[0].source_span is None


def test_legacy_full_source_marker_is_not_treated_as_truncation() -> None:
    value = "ALPHA [\N{HORIZONTAL ELLIPSIS}]"
    content = "Credential is " + value
    binding = make_exact_value_binding(content, value, "identifier")
    record = MemoryRecord(id="bound", content=content, metadata={"exact_value": binding})
    candidate = Candidate(record.id, 1.0, "lexical", record)
    packer = DeterministicContextPacker()

    budget = packer.count_tokens("[1]\n" + content)
    packed = packer.pack("ALPHA", [candidate], budget)
    assert packed.chunks[0].excerpt == content
    assert packed.chunks[0].exact_value == binding
    assert packed.chunks[0].source_span == (binding["start"], binding["end"])


def test_meaningful_source_trim_preserves_bound_literal_whitespace() -> None:
    content = "  ALPHA  "
    binding = make_exact_value_binding(content, content, "literal")
    record = MemoryRecord(id="bound", content=content, metadata={"exact_value": binding})
    candidate = Candidate(record.id, 1.0, "lexical", record)
    packer = DeterministicContextPacker()

    packed = packer.pack_coverage("ALPHA", [candidate], packer.count_tokens("[1]\n" + content))
    assert packed.chunks[0].excerpt == content
    assert packed.chunks[0].exact_value == binding
    assert packed.chunks[0].source_span == (0, len(content))


@pytest.mark.parametrize("mode", ["legacy", "coverage"])
def test_english_qualifier_does_not_hide_a_distant_foreign_restriction(mode) -> None:
    partial = "Credential is ALPHA only after approval."
    content = partial + " Utilisez-le uniquement en production."
    binding = make_exact_value_binding(content, "ALPHA", "identifier")
    record = MemoryRecord(id="mixed", content=content, metadata={"exact_value": binding})
    candidate = Candidate(record.id, 1.0, "lexical", record)
    packer = DeterministicContextPacker()
    method = packer.pack if mode == "legacy" else packer.pack_coverage
    tight = method("ALPHA approval", [candidate], packer.count_tokens("[1]\n" + partial))
    assert all(chunk.exact_value is None and chunk.source_span is None
               and chunk.evidence_unit["value"] is None
               and chunk.evidence_unit["source_span"] is None for chunk in tight.chunks)
    if mode == "legacy":
        assert partial in tight.context
    else:
        assert "ALPHA" not in tight.context
    roomy = method("ALPHA approval", [candidate], packer.count_tokens("[1]\n" + content))
    assert roomy.chunks[0].excerpt == content
    assert roomy.chunks[0].exact_value == binding
