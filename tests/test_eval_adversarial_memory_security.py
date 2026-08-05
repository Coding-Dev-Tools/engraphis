"""Regression coverage for the bounded v2 prompt-boundary security gate."""
from __future__ import annotations

import json

from eval import adversarial_memory_security as gate


def test_adversarial_memory_security_gate_passes_all_declared_metrics():
    report = gate.run()

    assert report["schema"] == "engraphis-adversarial-memory-security/v1"
    assert report["passed"] is True
    assert all(metric == {"passed": 1, "n": 1, "rate": 1.0}
               for metric in report["metrics"].values())
    assert report["diagnostics"] == {
        "raw_graph_contains_direct_pending": True,
        "raw_graph_contains_supported_pending": True,
        "raw_graph_contains_self_asserted": True,
        "prompt_graph_contains_direct_pending": False,
        "prompt_graph_contains_supported_pending": False,
        "prompt_graph_contains_self_asserted": False,
        "prompt_recall_contains_direct_pending": False,
        "prompt_recall_contains_supported_pending": False,
        "prompt_recall_contains_self_asserted": False,
        "prompt_recall_contains_quarantined": False,
        "direct_edge_created": True,
        "supported_edge_created": True,
        "self_asserted_edge_created": True,
    }


def test_adversarial_memory_security_cli_json_is_machine_readable(capsys):
    assert gate.main(["--json"]) == 0
    report = json.loads(capsys.readouterr().out)

    assert report["passed"] is True
    assert report["metrics"]["trusted_memory_available_in_prompt_graph"]["rate"] == 1.0
