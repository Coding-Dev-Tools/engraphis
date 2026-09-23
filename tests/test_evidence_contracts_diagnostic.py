"""Diagnostic gates must detect omitted bindings and corrupted accounting."""

import pytest

from engraphis.core.context import DeterministicContextPacker
from eval import evidence_contracts


@pytest.mark.parametrize("fault", ["omit_binding", "wrong_span", "wrong_tokens"])
def test_unknown_qualifier_gate_rejects_incomplete_or_misreported_evidence(fault, monkeypatch, capsys):
    class FaultyPacker(DeterministicContextPacker):
        def pack_coverage(self, *args, **kwargs):
            result = super().pack_coverage(*args, **kwargs)
            for chunk in result.chunks:
                if chunk.exact_value is None:
                    continue
                if fault == "omit_binding":
                    chunk.exact_value = None
                    chunk.source_span = None
                    chunk.evidence_unit["value"] = None
                    chunk.evidence_unit["source_span"] = None
                elif fault == "wrong_span":
                    chunk.evidence_unit["source_span"] = [0, 1]
            if fault == "wrong_tokens":
                result.usage.context_tokens = 0
            return result

    report = evidence_contracts.run()
    assert report["coverage_queries"]["correct"] == 5
    assert report["coverage_query_safety"]["safety_correct"] == 8
    monkeypatch.setattr(evidence_contracts, "run", lambda: report)
    assert evidence_contracts.main() == 0

    diagnostic = evidence_contracts.unknown_qualifier_diagnostic(packer_type=FaultyPacker)
    coverage = diagnostic["coverage"]
    assert coverage["correct"] < coverage["cases"]
    assert coverage["unsafe_binding_count"] == 0
    if fault == "omit_binding":
        assert coverage["roomy_omission_count"] == coverage["roomy_cases"] == 5
    elif fault == "wrong_tokens":
        assert coverage["token_accounting_errors"] > 0
    report["unknown_qualifiers"] = diagnostic
    assert evidence_contracts.main() == 1
    capsys.readouterr()
