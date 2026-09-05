"""The manual graph probe must never attach to an unrelated dashboard."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is required for the manual probe")
def test_graph_probe_rejects_an_occupied_port_without_contacting_its_owner():
    script = r"""
const assert = require('node:assert/strict');
const net = require('node:net');
const path = require('node:path');
(async () => {
  let contacted = false;
  const owner = net.createServer(socket => { contacted = true; socket.end(); });
  await new Promise(resolve => owner.listen(0, '127.0.0.1', resolve));
  try {
    process.env.ENGRAPHIS_PLAYWRIGHT_PORT = String(owner.address().port);
    const before = process.cwd();
    const probe = require('./tools/galaxy_mode_test.js');
    assert.equal(probe.REPO, path.resolve('.'));
    assert.equal(process.cwd(), before);
    await assert.rejects(probe.startServer(), /unavailable/);
    assert.equal(contacted, false);
    assert.equal(owner.listening, true);
  } finally {
    await new Promise(resolve => owner.close(resolve));
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    result = subprocess.run(["node", "-e", script], cwd=ROOT, capture_output=True,
                            text=True, timeout=15)
    assert result.returncode == 0, result.stderr
