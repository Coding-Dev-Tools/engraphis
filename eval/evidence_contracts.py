"""Small offline diagnostic for evidence packing and exact action validation.

These development fixtures measure deterministic boundary behavior, not external
benchmark performance, model task success, latency, or provider usage.
Run with ``python -m eval.evidence_contracts``.
"""
from __future__ import annotations

import json

from engraphis.core import evidence
from engraphis.core.context import DeterministicContextPacker, RegexTokenCounter
from engraphis.core.interfaces import Candidate, MemoryRecord


def coverage_query_diagnostic(*, packer_type=DeterministicContextPacker) -> dict:
    """Fixed query windows expose irrelevant-literal selection and span mistakes."""
    phone = "Support phone is 555-1234."
    contact = "Deployment token is ALPHA. " + phone
    duplicate = "First ALPHA. Second ALPHA."
    payload = '{\n  "mode": "canary"\n}'
    multiline = "Payload:\n" + payload + "\n" + phone
    expansion = "ALPHA. Support phone is 555-1234 with ALPHA today."
    cases = [
        ("unbound_phone", contact, "ALPHA", None, "support phone", 11, phone, False, False),
        ("bound_token", contact, "ALPHA", None, "deployment token", 9,
         "Deployment token is ALPHA.", True, False),
        ("both_sentences", contact, "ALPHA", None, "support phone", 24, contact, True, False),
        ("unbound_duplicate", duplicate, "ALPHA", (6, 11), "second", 8,
         "Second ALPHA.", False, False),
        ("bound_duplicate", duplicate, "ALPHA", (20, 25), "second", 8,
         "Second ALPHA.", True, False),
        ("unbound_multiline", multiline, payload, None, "support phone", 11, phone, False, False),
        ("bound_multiline", multiline, payload, None, "mode canary", 14, payload, True, False),
        ("expansion_rebind", expansion, "ALPHA", (0, 5), "support phone", 18,
         "Support phone is 555-1234 with ALPHA today.", False, True),
    ]
    outcomes = []
    for name, content, value, span, query, budget, expected, bound, extra_source in cases:
        binding = evidence.make_exact_value_binding(content, value, source_span=span)
        record = MemoryRecord(id=name, content=content, metadata={"exact_value": binding})
        candidates = [Candidate(name, 1.0, "lexical", record)]
        if extra_source:
            candidates.append(Candidate("other", 0.5, "lexical",
                                        MemoryRecord(id="other", content="Unrelated.")))
        packed = packer_type().pack_coverage(query, candidates, budget)
        selected = next((chunk for chunk in packed.chunks if chunk.id == name), None)
        excerpt = selected.excerpt if selected else ""
        actual_binding = selected.exact_value if selected else None
        expected_binding = binding if bound else None
        correct = (expected in excerpt and actual_binding == expected_binding
                   and packed.usage.context_tokens <= budget)
        outcomes.append({"case": name, "query": query, "budget": budget,
                         "tokens": packed.usage.context_tokens, "excerpt": excerpt,
                         "expected_excerpt": expected, "expected_bound": bound,
                         "actual_bound": actual_binding is not None, "correct": correct})
    return {"cases": len(outcomes), "correct": sum(row["correct"] for row in outcomes),
            "outcomes": outcomes}


def coverage_restriction_diagnostic(*, packer_type=DeterministicContextPacker) -> dict:
    """Retain the original literal-retention expectations at fixed budgets.

    These unpunctuated sources expose the retention cost of complete-unit safety.
    Inputs and expected excerpts stay unchanged even when the safer packer omits
    the value. The separate complete-unit diagnostic gates safe omission and
    roomy retention; this population continues to report literal retention.
    """
    cases = [
        ("suffix_repeated_query", "deployment " * 8 + "VALUE filler only if approved",
         "VALUE", "deployment", 14, "VALUE filler only if approved"),
        ("prefix_repeated_query", "only if approved VALUE " + "deployment " * 8,
         "VALUE", "deployment", 14, "only if approved VALUE"),
        ("suffix_competing_queries",
         "deployment " * 5 + "VALUE neutral gap only if approved and never share",
         "VALUE", "deployment", 16,
         "VALUE neutral gap only if approved and never share"),
        ("prefix_long_condition", "must use only if approved VALUE " + "deployment " * 8,
         "VALUE", "deployment", 14, "must use only if approved VALUE"),
        ("tight_suffix", "deployment " * 6 + "VALUE only if approved",
         "VALUE", "deployment", 12, "VALUE only if approved"),
    ]
    outcomes = []
    for name, content, value, query, budget, expected in cases:
        binding = evidence.make_exact_value_binding(content, value, "identifier")
        record = MemoryRecord(id=name, content=content, metadata={"exact_value": binding})
        packed = packer_type().pack_coverage(
            query, [Candidate(name, 1.0, "lexical", record)], budget,
        )
        selected = next((chunk for chunk in packed.chunks if chunk.id == name), None)
        excerpt = selected.excerpt if selected else ""
        actual_binding = selected.exact_value if selected else None
        correct = (expected in excerpt and actual_binding == binding
                   and packed.usage.context_tokens <= budget)
        outcomes.append({
            "case": name,
            "query": query,
            "budget": budget,
            "tokens": packed.usage.context_tokens,
            "excerpt": excerpt,
            "expected_excerpt": expected,
            "expected_bound": True,
            "actual_bound": actual_binding is not None,
            "correct": correct,
        })
    return {"cases": len(outcomes), "correct": sum(row["correct"] for row in outcomes),
            "outcomes": outcomes}


def coverage_binding_safety_diagnostic(*, packer_type=DeterministicContextPacker) -> dict:
    """Require omission when a bound group's complete restrictions do not fit."""
    outcomes = []

    def check(name, query, budget, candidates):
        packed = packer_type().pack_coverage(query, candidates, budget)
        selected = next((chunk for chunk in packed.chunks if chunk.id == name), None)
        omitted = selected is None or (
            selected.exact_value is None
            and selected.source_span is None
            and selected.evidence_unit.get("value") is None
            and "VALUE" not in selected.excerpt
        )
        outcomes.append({
            "case": name,
            "query": query,
            "budget": budget,
            "tokens": packed.usage.context_tokens,
            "excerpt": selected.excerpt if selected else "",
            "actual_bound": selected.exact_value is not None if selected else False,
            "omitted_bound_group": omitted,
            "correct": omitted and packed.usage.context_tokens <= budget,
        })

    for name, content, budget in (
        ("tight_suffix", "VALUE only if approved", 6),
        ("tight_prefix", "only if approved VALUE", 6),
        ("tight_both_sides", "only if approved VALUE only if approved", 9),
        ("tight_multiline", "VALUE only if\napproved", 6),
    ):
        binding = evidence.make_exact_value_binding(content, "VALUE", "identifier")
        record = MemoryRecord(id=name, content=content, metadata={"exact_value": binding})
        check(name, "deployment", budget, [Candidate(name, 1.0, "lexical", record)])

    status = MemoryRecord(id="status", title="Status", content="Deployment status is green.")
    content = "VALUE only if approved"
    binding = evidence.make_exact_value_binding(content, "VALUE", "identifier")
    bound = MemoryRecord(
        id="second_pass_bound", title="Rule", content=content,
        metadata={"exact_value": binding},
    )
    check(
        "second_pass_bound", "deployment", 14,
        [Candidate("status", 1.0, "lexical", status),
         Candidate("second_pass_bound", 0.9, "lexical", bound)],
    )
    return {"cases": len(outcomes), "correct": sum(row["correct"] for row in outcomes),
            "outcomes": outcomes}


def coverage_distant_restriction_diagnostic(*, packer_type=DeterministicContextPacker) -> dict:
    """Require source-wide qualifier groups for distant bound literals.

    Tight budgets must withhold both the literal and its binding when a qualifier
    is separated by neutral units; a roomy budget must retain the whole
    punctuation-delimited group. This is a development boundary fixture, not a
    semantic claim that every qualifier is related to the literal.
    """
    cases = [
        (
            "prefix_tight",
            "Only use this credential in production. Neutral one. Neutral two. "
            "Credential is ALPHA.",
            8,
            False,
        ),
        (
            "prefix_roomy",
            "Only use this credential in production. Neutral one. Neutral two. "
            "Credential is ALPHA.",
            20,
            True,
        ),
        (
            "suffix_tight",
            "Credential is ALPHA. Neutral one. Neutral two. "
            "Never use this credential in staging.",
            8,
            False,
        ),
        (
            "suffix_roomy",
            "Credential is ALPHA. Neutral one. Neutral two. "
            "Never use this credential in staging.",
            20,
            True,
        ),
        (
            "both_tight",
            "Only use this credential in production. Neutral one. "
            "Credential is ALPHA. Neutral two. Never use this credential in staging.",
            20,
            False,
        ),
        (
            "both_roomy",
            "Only use this credential in production. Neutral one. "
            "Credential is ALPHA. Neutral two. Never use this credential in staging.",
            27,
            True,
        ),
        (
            "wrapped_prefix_tight",
            "Only use this credential in production.\nNeutral one.\nNeutral two.\n"
            "Credential is ALPHA.",
            8,
            False,
        ),
        (
            "wrapped_prefix_roomy",
            "Only use this credential in production.\nNeutral one.\nNeutral two.\n"
            "Credential is ALPHA.",
            20,
            True,
        ),
    ]
    outcomes = []
    for name, content, budget, expected_bound in cases:
        binding = evidence.make_exact_value_binding(content, "ALPHA", "identifier")
        record = MemoryRecord(id=name, content=content, metadata={"exact_value": binding})
        packed = packer_type().pack_coverage(
            "ALPHA", [Candidate(name, 1.0, "lexical", record)], budget,
        )
        selected = next((chunk for chunk in packed.chunks if chunk.id == name), None)
        excerpt = selected.excerpt if selected else ""
        actual_bound = bool(selected and selected.exact_value is not None)
        actual_literal = "ALPHA" in packed.context
        correct = (
            actual_bound is expected_bound
            and actual_literal is expected_bound
            and packed.usage.context_tokens <= budget
            and (not expected_bound or excerpt == content)
        )
        outcomes.append({
            "case": name,
            "budget": budget,
            "tokens": packed.usage.context_tokens,
            "expected_bound": expected_bound,
            "actual_bound": actual_bound,
            "literal_present": actual_literal,
            "excerpt": excerpt,
            "correct": correct,
        })
    return {
        "cases": len(outcomes),
        "correct": sum(row["correct"] for row in outcomes),
        "outcomes": outcomes,
    }


def coverage_complete_unit_diagnostic(*, packer_type=DeterministicContextPacker) -> dict:
    """Require complete qualifier units for exact-value retention.

    The historical unpunctuated restriction fixtures are retained as a separate
    safety population: their original tight budgets must withhold the value, while
    a budget large enough for the full source may retain it. Same-unit prefix and
    suffix controls catch partial windows on either side of a literal.
    """
    tight_cases = [
        ("suffix_repeated_query", "deployment " * 8 + "VALUE filler only if approved",
         "deployment", "VALUE", 14),
        ("prefix_repeated_query", "only if approved VALUE " + "deployment " * 8,
         "deployment", "VALUE", 14),
        ("suffix_competing_queries",
         "deployment " * 5 + "VALUE neutral gap only if approved and never share",
         "deployment", "VALUE", 16),
        ("prefix_long_condition", "must use only if approved VALUE " + "deployment " * 8,
         "deployment", "VALUE", 14),
        ("tight_suffix", "deployment " * 6 + "VALUE only if approved",
         "deployment", "VALUE", 12),
    ]
    cases = [
        *((case[0] + "_tight",) + case[1:] + (False,) for case in tight_cases),
        *((case[0] + "_roomy",) + case[1:4] + (packer_type().count_tokens("[1]\n" + case[1]), True)
          for case in tight_cases),
        ("same_unit_prefix_tight", "Only use ALPHA in production", "ALPHA", "ALPHA", 6, False),
        ("same_unit_prefix_roomy", "Only use ALPHA in production", "ALPHA", "ALPHA", 8, True),
        ("same_unit_suffix_tight", "Use ALPHA only in production", "ALPHA", "ALPHA", 7, False),
        ("same_unit_suffix_roomy", "Use ALPHA only in production", "ALPHA", "ALPHA", 8, True),
        ("bound_unit_suffix_tight", "Only use this credential. Credential is ALPHA in production.",
         "ALPHA", "ALPHA", 11, False),
        ("bound_unit_suffix_roomy", "Only use this credential. Credential is ALPHA in production.",
         "ALPHA", "ALPHA", 14, True),
    ]
    outcomes = []
    for name, content, query, value, budget, expected_bound in cases:
        binding = evidence.make_exact_value_binding(content, value, "identifier")
        record = MemoryRecord(id=name, content=content, metadata={"exact_value": binding})
        packed = packer_type().pack_coverage(
            query, [Candidate(name, 1.0, "lexical", record)], budget,
        )
        selected = next((chunk for chunk in packed.chunks if chunk.id == name), None)
        excerpt = selected.excerpt if selected else ""
        actual_bound = bool(selected and selected.exact_value is not None)
        literal_present = value in packed.context
        complete = not expected_bound or excerpt == content.strip()
        correct = (
            actual_bound is expected_bound
            and literal_present is expected_bound
            and complete
            and packed.usage.context_tokens <= budget
        )
        outcomes.append({
            "case": name,
            "query": query,
            "budget": budget,
            "tokens": packed.usage.context_tokens,
            "expected_bound": expected_bound,
            "actual_bound": actual_bound,
            "literal_present": literal_present,
            "excerpt": excerpt,
            "complete_excerpt": complete,
            "correct": correct,
        })
    return {
        "cases": len(outcomes),
        "correct": sum(row["correct"] for row in outcomes),
        "outcomes": outcomes,
    }



def legacy_binding_safety_diagnostic(*, packer_type=DeterministicContextPacker) -> dict:
    """Pair legacy context output with source-bound binding safety.

    The legacy default remains a context-and-token baseline: a fix for a partial
    qualifier group must suppress the exact binding while preserving the exact
    selected context and accounting.  Tight cases deliberately retain the literal
    as visible text, but do not publish a binding; roomy cases retain the complete
    source and binding.  This is a deterministic development diagnostic, not a
    semantic condition classifier or external benchmark quality measure.
    """
    cases = [
        {
            "case": "suffix_tight",
            "content": (
                "Credential is ALPHA only in production. Never use this credential "
                "in staging environments under any circumstances whatsoever."
            ),
            "query": "credential",
            "budget": 10,
            "expected_context": "[1]\nCredential is ALPHA only in production.",
            "expected_bound": False,
        },
        {
            "case": "prefix_tight",
            "content": (
                "Only use this credential in production. Credential is ALPHA only "
                "in production."
            ),
            "query": "ALPHA",
            "budget": 10,
            "expected_context": "[1]\nCredential is ALPHA only in production.",
            "expected_bound": False,
        },
        {
            "case": "both_tight",
            "content": (
                "Only use this credential in production. Credential is ALPHA only "
                "in production. Never use this credential in staging."
            ),
            "query": "ALPHA",
            "budget": 10,
            "expected_context": "[1]\nCredential is ALPHA only in production.",
            "expected_bound": False,
        },
        {
            "case": "distant_tight",
            "content": (
                "Credential is ALPHA only in production. Neutral note. Never use "
                "this credential in staging."
            ),
            "query": "ALPHA",
            "budget": 10,
            "expected_context": "[1]\nCredential is ALPHA only in production.",
            "expected_bound": False,
        },
        {
            "case": "suffix_roomy",
            "content": (
                "Credential is ALPHA only in production. Never use this credential "
                "in staging environments under any circumstances whatsoever."
            ),
            "query": "credential",
            "budget": 22,
            "expected_context": (
                "[1]\nCredential is ALPHA only in production. Never use this credential "
                "in staging environments under any circumstances whatsoever."
            ),
            "expected_bound": True,
        },
        {
            "case": "prefix_roomy",
            "content": (
                "Only use this credential in production. Credential is ALPHA only "
                "in production."
            ),
            "query": "ALPHA",
            "budget": 17,
            "expected_context": (
                "[1]\nOnly use this credential in production. Credential is ALPHA only "
                "in production."
            ),
            "expected_bound": True,
        },
        {
            "case": "both_roomy",
            "content": (
                "Only use this credential in production. Credential is ALPHA only "
                "in production. Never use this credential in staging."
            ),
            "query": "ALPHA",
            "budget": 24,
            "expected_context": (
                "[1]\nOnly use this credential in production. Credential is ALPHA only "
                "in production. Never use this credential in staging."
            ),
            "expected_bound": True,
        },
        {
            "case": "distant_roomy",
            "content": (
                "Credential is ALPHA only in production. Neutral note. Never use "
                "this credential in staging."
            ),
            "query": "ALPHA",
            "budget": 20,
            "expected_context": (
                "[1]\nCredential is ALPHA only in production. Neutral note. Never use "
                "this credential in staging."
            ),
            "expected_bound": True,
        },
    ]
    baseline_counter = RegexTokenCounter()
    outcomes = []
    for case in cases:
        name = case["case"]
        content = case["content"]
        value = "ALPHA"
        binding = evidence.make_exact_value_binding(content, value, "identifier")
        record = MemoryRecord(
            id=name, content=content, metadata={"exact_value": binding},
        )
        candidate = Candidate(name, 1.0, "lexical", record)
        packed = packer_type().pack(case["query"], [candidate], case["budget"])
        selected = next((chunk for chunk in packed.chunks if chunk.id == name), None)
        actual_binding = selected.exact_value if selected else None
        actual_bound = actual_binding is not None
        evidence_unit = (
            selected.evidence_unit
            if selected is not None and isinstance(selected.evidence_unit, dict)
            else {}
        )
        literal_present = value in packed.context
        expected_context = case["expected_context"]
        expected_context_tokens = baseline_counter(expected_context)
        expected_source_tokens = baseline_counter(f"\n{content}")
        expected_saved_tokens = max(0, expected_source_tokens - expected_context_tokens)
        usage = packed.usage
        context_unchanged = packed.context == expected_context
        accounting_unchanged = (
            usage.budget_tokens == case["budget"]
            and usage.context_tokens == expected_context_tokens
            and usage.source_tokens == expected_source_tokens
            and usage.saved_tokens == expected_saved_tokens
            and usage.packed_count == 1
            and usage.omitted_count == 0
        )
        if case["expected_bound"]:
            binding_preserved = (
                actual_binding == binding
                and selected is not None
                and selected.source_span == (binding["start"], binding["end"])
                and evidence_unit.get("value") == value
                and evidence_unit.get("source_span") == [binding["start"], binding["end"]]
            )
        else:
            binding_preserved = (
                actual_binding is None
                and selected is not None
                and selected.source_span is None
                and evidence_unit.get("value") is None
                and evidence_unit.get("source_span") is None
            )
        correct = (
            context_unchanged
            and accounting_unchanged
            and literal_present
            and actual_bound is case["expected_bound"]
            and binding_preserved
        )
        outcomes.append({
            "case": name,
            "query": case["query"],
            "budget": case["budget"],
            "tokens": usage.context_tokens,
            "expected_tokens": expected_context_tokens,
            "context": packed.context,
            "expected_context": expected_context,
            "context_unchanged": context_unchanged,
            "accounting_unchanged": accounting_unchanged,
            "expected_bound": case["expected_bound"],
            "actual_bound": actual_bound,
            "literal_present": literal_present,
            "binding_preserved": binding_preserved,
            "correct": correct,
        })
    suppressed = sum(
        row["expected_bound"] is False and row["actual_bound"] is False
        for row in outcomes
    )
    retained = sum(
        row["expected_bound"] is True and row["actual_bound"] is True
        for row in outcomes
    )
    return {
        "boundary": (
            "Fixed synthetic legacy-packing fixtures; paired commits must preserve "
            "context and token accounting while suppressing incomplete exact bindings. "
            "Not semantic condition inference or external benchmark quality."
        ),
        "cases": len(outcomes),
        "unsafe_cases": sum(not row["expected_bound"] for row in outcomes),
        "roomy_cases": sum(row["expected_bound"] for row in outcomes),
        "binding_suppressed": suppressed,
        "roomy_retained": retained,
        "contexts_unchanged": sum(row["context_unchanged"] for row in outcomes),
        "token_accounting_unchanged": sum(row["accounting_unchanged"] for row in outcomes),
        "correct": sum(row["correct"] for row in outcomes),
        "outcomes": outcomes,
    }

def run(*, evidence_module=evidence, packer_type=DeterministicContextPacker) -> dict:
    binding = evidence_module.make_exact_value_binding("label=Δ-42", "Δ-42", "identifier")
    contract = evidence_module.make_action_contract(
        destination_field="release.label", source_id="fixture-source",
        binding=binding, authorized=True,
        source_content="label=Δ-42",
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
        actual = evidence_module.validate_action_contract(contract, proposal, source_content="label=Δ-42")["valid"]
        outcomes.append({"case": name, "expected": expected, "actual": actual})
    for name, authorization in (("string_false", "false"), ("integer_one", 1),
                                ("boolean_false", False)):
        unapproved = evidence_module.make_action_contract(
            destination_field="release.label", source_id="fixture-source",
            binding=binding, authorized=authorization,
            source_content="label=Δ-42",
        )
        actual = evidence_module.validate_action_contract(unapproved, good, source_content="label=Δ-42")["valid"]
        outcomes.append({"case": name, "expected": False, "actual": actual})
    malformed = {**binding, "end": binding["end"] + 1}
    outcomes.append({
        "case": "inconsistent_source_span", "expected": False,
        "actual": evidence_module.exact_value_binding({"exact_value": malformed}) is not None,
    })
    forged_binding = {**binding, "start": 900, "end": 904}
    try:
        evidence_module.make_action_contract(
            destination_field="release.label", source_id="fixture-source",
            source_content="label=Δ-42", binding=forged_binding, authorized=True,
        )
        forged_accepted = True
    except ValueError:
        forged_accepted = False
    source_outcomes = [{"case": "forged_source_span", "expected": False, "actual": forged_accepted}]
    for name, candidate, source in (
        ("changed_source_revision", contract, "label=Δ-42; authorization revoked"),
        ("missing_source_digest", {key: value for key, value in contract.items() if key != "source_sha256"}, "label=Δ-42"),
        ("tampered_contract_span", {**contract, "source_span": [900, 904]}, "label=Δ-42"),
    ):
        actual = evidence_module.validate_action_contract(candidate, good, source_content=source)["valid"]
        source_outcomes.append({"case": name, "expected": False, "actual": actual})

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
        "source_validation": {
            "cases": len(source_outcomes),
            "correct": sum(row["actual"] == row["expected"] for row in source_outcomes),
            "outcomes": source_outcomes,
        },
        "coverage_queries": coverage_query_diagnostic(packer_type=packer_type),
        "coverage_restrictions": coverage_restriction_diagnostic(packer_type=packer_type),
        "coverage_binding_safety": coverage_binding_safety_diagnostic(packer_type=packer_type),
        "coverage_distant_restrictions": coverage_distant_restriction_diagnostic(packer_type=packer_type),
        "coverage_complete_units": coverage_complete_unit_diagnostic(packer_type=packer_type),
        "legacy_binding_safety": legacy_binding_safety_diagnostic(packer_type=packer_type),
        "packing": {
            name: {"sources": len(result.chunks), "tokens": result.usage.context_tokens,
                   "budget": 35, "budget_honored": result.usage.context_tokens <= 35}
            for name, result in packed.items()
        },
        "oversized_exact": {
            "sources": len(oversized.chunks),
            "literal_preserved": "Δ-42" in oversized.context,
            "nearby_qualifiers_preserved": "must use Δ-42 only if approved" in oversized.context,
            "withheld_boundary": not oversized.chunks,
            "omission_reasons": oversized.usage.omission_reasons,
            "tokens": oversized.usage.context_tokens,
            "budget": 24,
            "budget_honored": oversized.usage.context_tokens <= 24,
        },
    }


def main() -> int:
    report = run()
    print(json.dumps(report, sort_keys=True))
    validation = report["action_validation"]
    # Preserve the old five-case retention score without treating unsafe partial
    # units as the target behavior. Complete-unit safety and roomy retention are
    # separately required below, including those same sources and tight budgets.
    return 0 if (validation["correct"] == validation["cases"]
                 and report["source_validation"]["correct"] == report["source_validation"]["cases"]
                 and report["coverage_queries"]["correct"] == report["coverage_queries"]["cases"]
                 and report["coverage_binding_safety"]["correct"] == report["coverage_binding_safety"]["cases"]
                 and report["coverage_distant_restrictions"]["correct"] == report["coverage_distant_restrictions"]["cases"]
                 and report["coverage_complete_units"]["correct"] == report["coverage_complete_units"]["cases"]
                 and report["legacy_binding_safety"]["correct"] == report["legacy_binding_safety"]["cases"]
                 and all(row["budget_honored"] for row in report["packing"].values())
                 and report["oversized_exact"]["withheld_boundary"]
                 and not report["oversized_exact"]["literal_preserved"]
                 and not report["oversized_exact"]["nearby_qualifiers_preserved"]
                 and report["oversized_exact"]["budget_honored"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
