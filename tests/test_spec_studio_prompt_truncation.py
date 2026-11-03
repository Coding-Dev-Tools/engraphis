"""Prompt partial-analysis notices use the real renderer and clear on replacement."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is required for the renderer harness")
@pytest.mark.parametrize("case", ["complete", "truncated", "source_cap", "empty_sections", "replacement"])
def test_prompt_partial_analysis_is_visible_next_to_sections_flags_and_score(tmp_path, case):
    script = tmp_path / "prompt-truncation.cjs"
    script.write_text(r"""
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const elements = new Map();
function element() {
  return {
    children: [], ownText: '', className: '', classList: {add() {}}, setAttribute() {},
    appendChild(child) { this.children.push(child); },
    append(...children) { this.children.push(...children); },
    replaceChildren(...children) { this.children = children; this.ownText = ''; },
    set textContent(value) { this.ownText = String(value); this.children = []; },
    get textContent() { return this.ownText + this.children.map(child => child.textContent).join(' '); },
  };
}
const document = {readyState: 'loading', addEventListener() {}, createElement: element,
  createElementNS: element, getElementById(id) {
    if (!elements.has(id)) elements.set(id, element()); return elements.get(id);
  }};
const sandbox = {window: {}, document};
let source = fs.readFileSync(process.argv[2], 'utf8');
const marker = 'window.SpecStudio = { init, setScope, setPromptReport, setMemoryReport, setMode, resizeCanvas };';
assert.ok(source.includes(marker));
source = source.replace(marker, marker + `
window.__renderPrompt = report => {
  currentMode = 'prompt'; currentReport = report;
  renderPromptSectionsPanel(); renderHeatGrid(); renderScore(report.score, 0);
};`);
vm.runInNewContext(source, sandbox);
const which = process.argv[3];
const report = {score: 100, word_count: which === 'source_cap' ? 1 : 5000,
  sections: which === 'empty_sections' ? [] : [{title: 'Rules', words: 5000}],
  flags: [], truncated: which !== 'complete', procedural_source_truncated: which === 'source_cap'};
sandbox.window.__renderPrompt(report);
function notices(id) { return elements.get(id).children.filter(x => x.className === 'spec-truncation-notice'); }
const expected = which === 'complete' ? 0 : 1;
for (const id of ['spec-sections-list', 'spec-panel4-body']) {
  assert.equal(notices(id).length, expected, 'Partial prompt analysis needs a visible notice in both panels');
  if (expected) {
    assert.match(notices(id)[0].textContent, /part.*specification.*analyzed/i);
    assert.doesNotMatch(notices(id)[0].textContent, /5,?000|all content|no issues/i);
  }
}
if (expected) assert.match(elements.get('spec-score-motto').textContent, /partial/i);
if (which === 'replacement') {
  sandbox.window.__renderPrompt({...report, truncated: false, word_count: 2});
  for (const id of ['spec-sections-list', 'spec-panel4-body']) assert.equal(notices(id).length, 0);
  assert.doesNotMatch(elements.get('spec-score-motto').textContent, /partial/i);
}
console.log(JSON.stringify({case: which, partial: expected}));
""", encoding="utf-8")
    source = Path(__file__).resolve().parents[1] / "engraphis/dashboard_assets/spec-crawl.js"
    result = subprocess.run(["node", str(script), str(source), case], text=True,
                            capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["case"] == case
