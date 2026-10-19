"""Exercise the real async remediation guard with deterministic dialog races."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is required for the browser-script harness")
@pytest.mark.parametrize("case", [
    "confirm", "cancel", "workspace", "repo", "scope_roundtrip", "report", "clear",
    "mode", "request", "report_roundtrip", "ids", "nodes", "duplicate", "invalid_pair", "link",
    "missing_dialog", "rejecting_dialog", "nonboolean_confirmation", "cancel_retry",
])
def test_remediation_confirmation_rechecks_the_original_report_and_scope(tmp_path, case):
    harness = tmp_path / "confirmation.cjs"
    harness.write_text(r"""
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const which = process.argv[3];
const posted = [], dialogs = [];
let decide;
const report = {
  nodes: [{id: 'new'}, {id: 'old'}],
  conflicts: [{remedy: {action: 'supersede', keep_node: 'new', retire_node: 'old'}}],
  orphans: [{node_id: 'old', suggested_link_to: 'new'}],
};
const sandbox = {
  window: {
    // Embedded browsers can suppress native confirmation outright.
    confirm: () => false,
    matchMedia: () => ({matches: true}),
    EngraphisDialog: {confirm: spec => {
      dialogs.push(spec);
      return new Promise(resolve => { decide = resolve; });
    }},
  },
  document: {readyState: 'loading', addEventListener() {}},
  fetch: async (url, init) => {
    posted.push({url, body: JSON.parse(init.body)});
    return {ok: true, json: async () => ({ok: true})};
  },
};
let source = fs.readFileSync(process.argv[2], 'utf8');
const marker = 'window.SpecStudio = { init, setScope, setPromptReport, setMemoryReport, setMode, resizeCanvas };';
assert.ok(source.includes(marker));
source = source.replace(marker, marker + `
window.__guard = {
  prepare(report) {
    currentWorkspace = 'alpha'; currentRepo = 'repoA'; currentMode = 'memories';
    currentReport = report; reportScopeKey = scopeKey();
    loadMemoryClusterCrawl = async () => {};
    showToast = () => {};
    recomputeNodeLayout = renderMemoryStepper = renderMemoryNodesPanel = renderRadar =
      updateKindCounts = renderConflictsAndRemediationPanel = renderMemoryHealthScore =
      jumpToFinal = () => {};
  },
  change(kind) {
    if (kind === 'workspace') currentWorkspace = 'beta';
    if (kind === 'repo') currentRepo = 'repoB';
    if (kind === 'scope_roundtrip') { requestEpoch += 2; }
    if (kind === 'report') currentReport = {...currentReport};
    if (kind === 'report_roundtrip') {
      const original = currentReport;
      setMemoryReport({...original}); setMemoryReport(original);
    }
    if (kind === 'clear') currentReport = null;
    if (kind === 'mode') currentMode = 'prompt';
    if (kind === 'request') requestEpoch++;
    if (kind === 'ids') currentReport.conflicts[0].remedy.retire_node = 'unreviewed';
    if (kind === 'nodes') currentReport.nodes = [{id: 'new'}];
  },
  run: resolveRemedy,
  pending: () => mutationPending,
};`);
vm.runInNewContext(source, sandbox);
(async () => {
  const guard = sandbox.window.__guard;
  guard.prepare(report);
  if (which === 'missing_dialog') delete sandbox.window.EngraphisDialog;
  if (which === 'rejecting_dialog') sandbox.window.EngraphisDialog.confirm = () => Promise.reject(new Error('Dialog failed'));
  const action = which === 'link' ? 'link' : 'supersede';
  const task = guard.run(action, which === 'link' ? 'old' : 'new',
    which === 'invalid_pair' ? 'unknown' : which === 'link' ? 'new' : 'old');
  await Promise.resolve();
  if (['link', 'invalid_pair', 'missing_dialog', 'rejecting_dialog'].includes(which)) {
    await task;
    assert.equal(dialogs.length, 0);
    assert.equal(posted.length, which === 'link' ? 1 : 0);
  } else {
    assert.equal(dialogs.length, 1, 'Use the in-page dialog even when native confirmation is suppressed');
    assert.equal(posted.length, 0, 'Never send a mutation before the human answers');
    assert.equal(guard.pending(), true);
    assert.equal(dialogs[0].danger, true);
    assert.match(dialogs[0].message, /old/);
    assert.match(dialogs[0].message, /new/);
    if (which === 'duplicate') await guard.run(action, 'new', 'old');
    guard.change(which);
    decide(which === 'nonboolean_confirmation' ? 'yes' : !['cancel', 'cancel_retry'].includes(which));
    await task;
    if (which === 'cancel_retry') {
      assert.equal(posted.length, 0);
      assert.equal(guard.pending(), false);
      const retry = guard.run(action, 'new', 'old');
      await Promise.resolve();
      assert.equal(dialogs.length, 2);
      decide(true);
      await retry;
      assert.equal(posted.length, 1);
      assert.equal(guard.pending(), false);
      console.log(JSON.stringify({case: which, posted: posted.length, dialogs: dialogs.length}));
      return;
    }
    const accepted = which === 'confirm' || which === 'duplicate';
    assert.equal(posted.length, accepted ? 1 : 0);
    assert.equal(dialogs.length, 1);
    if (accepted) {
      assert.equal(posted[0].url, '/api/spec/crawl/memories/resolve');
      assert.deepEqual(posted[0].body, {
        action: 'supersede', node_a: 'new', node_b: 'old', confirmed: true,
        workspace: 'alpha', repo: 'repoA',
      });
    }
  }
  assert.equal(guard.pending(), false);
  console.log(JSON.stringify({case: which, posted: posted.length, dialogs: dialogs.length}));
})().catch(error => { console.error(error); process.exitCode = 1; });
""", encoding="utf-8")
    source = Path(__file__).resolve().parents[1] / "engraphis/dashboard_assets/spec-crawl.js"
    result = subprocess.run(["node", str(harness), str(source), case],
                            text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["case"] == case
