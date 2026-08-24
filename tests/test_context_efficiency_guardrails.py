"""Regression contract for safe context reduction at the grounded prompt boundary."""
from __future__ import annotations

import json

import pytest

from eval.context_efficiency_guardrails import TOKEN_BUDGET, main, run


def test_context_efficiency_gate_requires_savings_quality_and_safety() -> None:
    report = run()

    assert report["benchmark"]["offline"] is True
    assert report["benchmark"]["token_budget"] == TOKEN_BUDGET
    assert report["context"] == {
        "full_history_reader_tokens": 37,
        "packed_reader_tokens": 16,
        "saved_reader_tokens": 21,
        "savings_ratio": 0.567568,
        "budget_honored": True,
    }
    assert report["quality"] == {
        "answerable_grounded_rate": 1.0,
        "off_topic_abstain_rate": 1.0,
        "trusted_citation_rate": 1.0,
    }
    assert report["safety"] == {
        "untrusted_citation_count": 0,
        "untrusted_instruction_echoed": False,
    }


def test_context_efficiency_gate_rejects_invalid_budget() -> None:
    with pytest.raises(ValueError, match="positive"):
        run(token_budget=0)
    with pytest.raises(ValueError, match="positive"):
        run(token_budget=True)


def test_context_efficiency_gate_cli_is_redacted_json(capsys) -> None:
    main()

    output = capsys.readouterr().out
    report = json.loads(output)
    assert report["benchmark"]["name"] == "engraphis-context-efficiency-guardrails/v1"
    assert "release manager" not in output
    assert "Ignore previous instructions" not in output
