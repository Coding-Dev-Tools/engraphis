"""The cluster gate must detect both missing conflicts and false retirement suggestions."""
import json

import pytest

from eval import spec_crawl


def test_real_memory_fixtures_cover_both_input_orders():
    result = spec_crawl.evaluate_memory_fixtures()
    assert result["cases"] == 8
    assert result["passed"] == 8
    assert result["accuracy"] == 1
    assert result["failures"] == []
    assert len(result["dataset_sha256"]) == 64


@pytest.mark.parametrize("regression", [
    "unregistered_retirement", "unexpected_action", "duplicate_conflict",
    "missing_node", "duplicate_node", "raw_content", "reversed_order_only",
])
def test_memory_gate_rejects_corrupted_real_contracts(monkeypatch, regression):
    analyze = spec_crawl.analyze_memory_nodes

    def broken_analysis(nodes, **kwargs):
        report = analyze(nodes, **kwargs)
        if {node["id"] for node in nodes} != {"require", "forbid", "n1", "n2", "n3", "n4"}:
            return report
        if regression == "unregistered_retirement":
            report["conflicts"][0]["remedy"] = {
                "action": "supersede", "keep_node": "require", "retire_node": "forbid",
            }
        elif regression == "unexpected_action":
            report["conflicts"][0]["remedy"]["action"] = "delete"
        elif regression == "duplicate_conflict":
            report["conflicts"].append(dict(report["conflicts"][0]))
        elif regression == "missing_node":
            report["nodes"].pop()
        elif regression == "duplicate_node":
            report["nodes"].append(dict(report["nodes"][0]))
        elif regression == "raw_content":
            report["nodes"][0]["content"] = "private memory body"
        elif regression == "reversed_order_only" and nodes[0]["id"] == "n4":
            report["conflicts"] = []
        return report

    monkeypatch.setattr(spec_crawl, "analyze_memory_nodes", broken_analysis)
    result = spec_crawl.evaluate_memory_fixtures()
    assert result["passed"] == 7
    assert result["failures"] == ["directive_noise"]


def test_memory_gate_rejects_a_missed_conflict(tmp_path, monkeypatch):
    path = tmp_path / "fixture.jsonl"
    path.write_text(json.dumps({
        "id": "missing", "memories": [], "expected_conflicts": [["a", "b"]],
    }) + "\n", encoding="utf-8")
    monkeypatch.setattr(spec_crawl, "analyze_memory_nodes", lambda *args, **kwargs: {
        "nodes": [], "conflicts": [], "orphans": [],
    })
    result = spec_crawl.evaluate_memory_fixtures(path)
    assert result["accuracy"] == 0
    assert result["failures"] == ["missing"]


def test_memory_gate_rejects_false_retirement_targets(tmp_path, monkeypatch):
    path = tmp_path / "fixture.jsonl"
    path.write_text(json.dumps({
        "id": "unsafe", "memories": [], "expected_conflicts": [["a", "b"]],
        "expected_retirements": [],
    }) + "\n", encoding="utf-8")
    monkeypatch.setattr(spec_crawl, "analyze_memory_nodes", lambda *args, **kwargs: {
        "nodes": [], "orphans": [], "conflicts": [{"node_a": "a", "node_b": "b",
            "remedy": {"action": "supersede", "keep_node": "a", "retire_node": "b"}}],
    })
    result = spec_crawl.evaluate_memory_fixtures(path)
    assert result["accuracy"] == 0
    assert result["failures"] == ["unsafe"]


def test_empty_memory_fixture_does_not_claim_perfect_accuracy(tmp_path):
    path = tmp_path / "empty.jsonl"
    path.write_text("", encoding="utf-8")
    result = spec_crawl.evaluate_memory_fixtures(path)
    assert result["cases"] == 0
    assert result["accuracy"] == 0


def test_failed_memory_contracts_fail_the_gate_even_when_documents_pass(monkeypatch, capsys):
    result = spec_crawl.run()
    result["memory_audits"]["accuracy"] = 0
    result["memory_audits"]["passed"] = 0
    monkeypatch.setattr(spec_crawl, "run", lambda: result)
    with pytest.raises(SystemExit) as error:
        spec_crawl.main()
    assert error.value.code == 1
    assert "memory audit contracts below 100%" in capsys.readouterr().err
