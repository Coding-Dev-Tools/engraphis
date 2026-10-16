"""Render real Spec Studio functions locally without a server or browser socket."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is required for DOM rendering")
@pytest.mark.parametrize("case", [
    "words_only", "nodes_only", "both", "legacy", "complete", "word_details_only",
])
def test_memory_audit_labels_distinguish_missing_nodes_and_incomplete_words(tmp_path, case):
    script = tmp_path / "spec-studio-truncation.cjs"
    script.write_text(r"""
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const elements = new Map();
function element() {
  return {
    children: [], ownText: '', className: '', classList: { add() {} },
    setAttribute() {},
    appendChild(child) { this.children.push(child); },
    set innerHTML(value) { this.children = []; this.ownText = ''; },
    set textContent(value) { this.ownText = String(value); this.children = []; },
    get textContent() { return this.ownText + this.children.map(c => c.textContent).join(' '); },
  };
}
const document = {
  readyState: 'loading', addEventListener() {}, createElement: element,
  getElementById(id) {
    if (!elements.has(id)) elements.set(id, element());
    return elements.get(id);
  },
};
const sandbox = { window: {}, document };
let source = fs.readFileSync(process.argv[2], 'utf8');
const marker = 'window.SpecStudio = { init, setScope, setPromptReport, setMemoryReport, setMode, resizeCanvas };';
assert.ok(source.includes(marker));
source = source.replace(marker, marker + `
window.__renderAudit = report => {
  currentReport = report;
  renderMemoryNodesPanel();
  renderMemoryHealthScore(42);
};`);
vm.runInNewContext(source, sandbox);
const which = process.argv[3];
const nodes = which === 'nodes_only' || which === 'both';
const words = ['words_only', 'both', 'word_details_only'].includes(which);
const report = {
  node_count: 1, nodes: [{ id: 'policy', title: 'Policy', mtype: 'procedural' }],
  total_eligible_node_count: nodes || which === 'legacy' ? 151 : 1,
  node_selection_truncated: nodes, word_analysis_truncated_node_count: words ? 1 : 0,
  word_limit: 5000, analysis_truncated: nodes || words || which === 'legacy',
  truncated: nodes || words || which === 'legacy',
};
if (which === 'legacy') delete report.node_selection_truncated;
if (which === 'word_details_only') {
  delete report.word_analysis_truncated_node_count;
  report.nodes[0].word_analysis_truncated = true;
}
sandbox.window.__renderAudit(report);
const list = elements.get('spec-sections-list');
console.log(JSON.stringify({
  motto: elements.get('spec-score-motto').textContent,
  badge: elements.get('spec-sections-count').textContent,
  panel: list.textContent,
  notices: list.children.filter(c => c.className === 'spec-truncation-notice').length,
}));
""", encoding="utf-8")
    source = Path(__file__).resolve().parents[1] / "engraphis/dashboard_assets/spec-crawl.js"
    completed = subprocess.run(
        ["node", str(script), str(source), case], text=True, capture_output=True, timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    rendered = json.loads(completed.stdout)
    node_sample = case in {"nodes_only", "both", "legacy"}
    word_sample = case in {"words_only", "both", "word_details_only"}
    assert rendered["badge"] == ("1 / 151" if node_sample else "1")
    if node_sample:
        assert "sample health" in rendered["motto"]
        assert "more remain" in rendered["motto"]
        assert "1 of 151 eligible memories" in rendered["panel"]
    else:
        assert "more remain" not in rendered["motto"]
        assert "first 1 nodes" not in rendered["motto"]
    if word_sample:
        assert "word" in rendered["motto"]
        assert "5,000" in rendered["panel"]
        assert "1 memory" in rendered["panel"]
    assert rendered["notices"] == int(node_sample) + int(word_sample)
    if case == "complete":
        assert rendered["motto"] == "cluster health"
