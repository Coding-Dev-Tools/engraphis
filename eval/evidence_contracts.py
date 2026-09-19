"""Small offline diagnostic for evidence packing and exact action validation.

These development fixtures measure deterministic boundary behavior, not external
benchmark performance, model task success, latency, or provider usage.
Run with ``python -m eval.evidence_contracts``.
"""
from __future__ import annotations

import json

from engraphis.core import evidence
from engraphis.core.context import DeterministicContextPacker
from engraphis.core.interfaces import Candidate, MemoryRecord


def run(*, evidence_module=evidence, packer_type=DeterministicContextPacker) -> dict:
    binding = evidence_module.make_exact_value_binding("label=Δ-42", "Δ-42", "identifier")
    contract = evidence_module.make_action_contract(
        destination_field="release.label", source_id="fixture-source",
        binding=binding, authorized=True,
    )
    good = {"release": {"label": "Δ-42"}, "source_id": "fixture-source"}
    wrong_field = {"release": {"label": "stable"}, "comment": "Δ-42"}
    wrong_source = {"release": {"label": "Δ-42"}, "source_id": "other-source"}
    changed = {"release": {"label": "Δ-420"}}
    cases = [
        ("mapping_accept", good, True),
        ("json_accept", json.dumps(good, ensure_ascii=False), True),
        ("escaped_unicode_accept", json.dumps(good), True),
        ("mapping_wrong_field", wrong_field, False),
        ("json_wrong_field", json.dumps(wrong_field, ensure_ascii=False), False),
        ("mapping_wrong_source", wrong_source, False),
        ("json_wrong_source", json.dumps(wrong_source, ensure_ascii=False), False),
        ("mapping_changed_literal", changed, False),
        ("json_changed_literal", json.dumps(changed, ensure_ascii=False), False),
        ("unstructured_output", "Mention Δ-42 but do not set the release label.", False),
    ]
    outcomes = []
    for name, proposal, expected in cases:
        actual = evidence_module.validate_action_contract(contract, proposal)["valid"]
        outcomes.append({"case": name, "expected": expected, "actual": actual})
    for name, authorization in (("string_false", "false"), ("integer_one", 1),
                                ("boolean_false", False)):
        unapproved = evidence_module.make_action_contract(
            destination_field="release.label", source_id="fixture-source",
            binding=binding, authorized=authorization,
        )
        actual = evidence_module.validate_action_contract(unapproved, good)["valid"]
        outcomes.append({"case": name, "expected": False, "actual": actual})
    malformed = {**binding, "end": binding["end"] + 1}
    outcomes.append({
        "case": "inconsistent_source_span", "expected": False,
        "actual": evidence_module.exact_value_binding({"exact_value": malformed}) is not None,
    })

    sources = [
        ("long", "The rollout is blue. " + "This unrelated historical explanation continues. " * 12),
        ("approval", "The rollout is green after approval."),
        ("owner", "The owner is platform reliability."),
    ]
    candidates = [Candidate(name, 1.0 - index / 10, "semantic", MemoryRecord(id=name, content=text))
                  for index, (name, text) in enumerate(sources)]
    packer = packer_type()
    packed = {name: method("rollout owner", candidates, 35)
              for name, method in (("legacy", packer.pack), ("coverage", packer.pack_coverage))}
    oversized_source = "padding " * 80 + "must use Δ-42 only if approved " + "trailing " * 80
    oversized_record = MemoryRecord(
        id="oversized-literal", content=oversized_source,
        metadata={"exact_value": evidence_module.make_exact_value_binding(oversized_source, "Δ-42")},
    )
    oversized = packer.pack_coverage(
        "deployment approved", [Candidate(oversized_record.id, 1.0, "lexical", oversized_record)], 24,
    )
    return {
        "schema": "engraphis-evidence-contract-diagnostic/v1",
        "boundary": "Development fixtures; not external QA, generated-answer quality, or provider savings.",
        "action_validation": {
            "cases": len(outcomes),
            "correct": sum(row["actual"] == row["expected"] for row in outcomes),
            "false_acceptances": sum(row["actual"] and not row["expected"] for row in outcomes),
            "false_rejections": sum(not row["actual"] and row["expected"] for row in outcomes),
            "outcomes": outcomes,
        },
        "packing": {
            name: {"sources": len(result.chunks), "tokens": result.usage.context_tokens,
                   "budget": 35, "budget_honored": result.usage.context_tokens <= 35}
            for name, result in packed.items()
        },
        "oversized_exact": {
            "sources": len(oversized.chunks),
            "literal_preserved": "Δ-42" in oversized.context,
            "nearby_qualifiers_preserved": "must use Δ-42 only if approved" in oversized.context,
            "tokens": oversized.usage.context_tokens,
            "budget": 24,
            "budget_honored": oversized.usage.context_tokens <= 24,
        },
    }


def main() -> int:
    report = run()
    print(json.dumps(report, sort_keys=True))
    validation = report["action_validation"]
    return 0 if (validation["correct"] == validation["cases"]
                 and all(row["budget_honored"] for row in report["packing"].values())
                 and all(report["oversized_exact"][key] for key in (
                     "literal_preserved", "nearby_qualifiers_preserved", "budget_honored",
                 ))) else 1


if __name__ == "__main__":
    raise SystemExit(main())
