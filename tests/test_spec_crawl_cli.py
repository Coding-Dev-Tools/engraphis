"""Exercise the Spec Crawl entrypoint's real formatted and JSON output."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

from engraphis.core.spec_crawl import analyze_memory_nodes
from scripts import spec_crawl as cli


def _memory_report(case):
    words = case in {"words_only", "both", "legacy_words", "word_details_only"}
    content = "neutral " * 5001 if words else "Local reference notes."
    report = analyze_memory_nodes([{"id": "policy", "content": content}], include_trace=False)
    sample = case in {"nodes_only", "both", "legacy", "legacy_no_total"}
    report.update({
        "node_selection_truncated": sample,
        "total_eligible_node_count": 2 if sample else 1,
        "truncated": sample or report["analysis_truncated"],
        "analysis_truncated": sample or report["analysis_truncated"],
    })
    if case.startswith("legacy"):
        report.pop("node_selection_truncated")
    if case == "legacy_no_total":
        report.pop("total_eligible_node_count")
    if case == "word_details_only":
        report.pop("word_analysis_truncated_node_count")
    return report


@pytest.mark.parametrize("case", [
    "words_only", "nodes_only", "both", "legacy", "complete",
    "legacy_no_total", "legacy_words", "word_details_only",
])
def test_workspace_cli_distinguishes_sampled_nodes_from_partial_word_analysis(
    monkeypatch, capsys, case,
):
    report = _memory_report(case)
    monkeypatch.setattr(cli, "_audit_workspace_memories", lambda **kwargs: report)
    assert cli.main(["--workspace", "alpha", "--no-trace"]) == 0
    captured = capsys.readouterr()
    output = captured.out
    sample = case in {"nodes_only", "both", "legacy", "legacy_no_total"}
    words = case in {"words_only", "both", "legacy_words", "word_details_only"}
    assert ("describe this sample" in output) is sample
    assert ("Partial word analysis" in output) is words
    assert not captured.err
    if sample:
        expected = "1 nodes analyzed" if case == "legacy_no_total" else "1 of 2 eligible nodes"
        assert expected in output
    if words:
        assert "5,000 tokens in 1 memory" in output
        assert "Word counts are lower bounds" in output
        assert "qualifier checks are incomplete" in output
        assert "1 of 1 eligible nodes" not in output
    if case == "complete":
        assert "Partial" not in output


@pytest.mark.parametrize("case", ["words_only", "nodes_only", "both", "legacy", "complete"])
def test_workspace_cli_json_preserves_the_complete_machine_report(monkeypatch, capsys, case):
    report = _memory_report(case)
    monkeypatch.setattr(cli, "_audit_workspace_memories", lambda **kwargs: report)
    assert cli.main(["--workspace", "alpha", "--json", "--no-trace"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == report
    assert not captured.err


def test_workspace_cli_uses_reported_word_limit_and_affected_memory_count(monkeypatch, capsys):
    report = _memory_report("words_only")
    report.update({"word_limit": 1234, "word_analysis_truncated_node_count": 2,
                   "node_count": 2, "total_eligible_node_count": 2})
    monkeypatch.setattr(cli, "_audit_workspace_memories", lambda **kwargs: report)
    assert cli.main(["--workspace", "alpha", "--no-trace"]) == 0
    output = capsys.readouterr().out
    assert "1,234 tokens in 2 memories" in output
    assert "describe this sample" not in output


@pytest.mark.parametrize("word_count", [5000, 5001])
def test_stdin_cli_reports_partial_word_analysis_at_the_actual_limit(word_count):
    result = subprocess.run(
        [sys.executable, "-m", "scripts.spec_crawl", "-", "--no-trace"],
        cwd=Path(__file__).resolve().parents[1], input="neutral " * word_count,
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert not result.stderr
    assert "words=5000" in result.stdout
    assert ("Partial word analysis" in result.stdout) is (word_count > 5000)
    if word_count > 5000:
        assert "stopped at 5,000 tokens" in result.stdout
        assert "Word counts are lower bounds" in result.stdout
        assert "qualifier checks are incomplete" in result.stdout
