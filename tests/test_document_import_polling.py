"""Exercise import polling ownership while the completed job refreshes its workspace."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


@pytest.mark.parametrize("refresh_fails", [False, True])
def test_new_job_keeps_polling_during_completed_job_refresh(tmp_path, refresh_fails):
    harness = r"""
const fs = require('fs'), vm = require('vm');
const source = fs.readFileSync('engraphis/dashboard_assets/ledger.js', 'utf8');
const start = source.indexOf('  async function pollObsidianImport(');
const end = source.indexOf('  async function runObsidianImport(', start);
const requests = [], timers = [], elements = new Map(), notices = [];
let finishRefresh, finishStatus;
const refresh = new Promise((resolve, reject) => {
  finishRefresh = () => REFRESH_FAILS ? reject(new Error('refresh unavailable')) : resolve();
});
const status = new Promise(resolve => { finishStatus = resolve; });
const ctx = {
  obsidianImport: { polling: false, running: true, pollFailures: 0 },
  state: { workspace: 'acme', editorSession: null },
  encodeURIComponent, query: workspace => 'workspace=' + workspace,
  byId(id) { if (!elements.has(id)) elements.set(id, {}); return elements.get(id); },
  obsidianApi: async url => {
    requests.push(url);
    return url.includes('/first?') ? { state: 'complete' } : status;
  },
  renderObsidianReport() {}, obsidianSummary() {},
  showNotice(message) { notices.push(message); }, selectWorkspace: () => refresh,
  window: { setTimeout(callback) { timers.push(callback); return timers.length; } },
};
vm.createContext(ctx); vm.runInContext(source.slice(start, end), ctx);
(async () => {
  const first = ctx.pollObsidianImport('first', 'acme');
  await new Promise(resolve => setImmediate(resolve));
  ctx.obsidianImport.running = true;
  const second = ctx.pollObsidianImport('second', 'acme');
  await new Promise(resolve => setImmediate(resolve));
  finishRefresh(); await first;
  // A completed job's refresh must not clear ownership of the newer status request.
  await ctx.pollObsidianImport('second', 'acme');
  const pollingDuringStatus = ctx.obsidianImport.polling;
  finishStatus({ state: 'running' }); await second;
  console.log(JSON.stringify({ requests, pollingDuringStatus, timers: timers.length,
    jobId: ctx.obsidianImport.jobId, running: ctx.obsidianImport.running,
    polling: ctx.obsidianImport.polling, failures: ctx.obsidianImport.pollFailures,
    notices }));
})().catch(error => { console.error(error); process.exitCode = 1; });
""".replace("REFRESH_FAILS", str(refresh_fails).lower())
    script = tmp_path / "polling.cjs"
    script.write_text(harness, encoding="utf-8")
    result = subprocess.run(
        [NODE, str(script)], cwd=ROOT, capture_output=True, text=True,
        encoding="utf-8", check=True, timeout=20,
    )
    report = json.loads(result.stdout)
    assert len(report["requests"]) == 2
    assert "/first?" in report["requests"][0]
    assert "/second?" in report["requests"][1]
    assert report["pollingDuringStatus"] is True
    assert report["timers"] == 1
    assert report["jobId"] == "second"
    assert report["running"] is True
    assert report["polling"] is False
    assert report["failures"] == 0
    assert len(report["notices"]) == (2 if refresh_fails else 1)
