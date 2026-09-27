"""Execute renderer functions to protect frame ordering and canvas invalidation."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")
HARNESS = r"""
const fs = require('fs'), vm = require('vm');
const source = fs.readFileSync('engraphis/dashboard_assets/engraphis-graph-every.js', 'utf8');
function load(context, names) {
  vm.createContext(context);
  for (const name of names) {
    const start = source.indexOf('    function ' + name + '(');
    if (start < 0) throw new Error('missing renderer function: ' + name);
    const end = source.indexOf('\n    function ', start + 1);
    vm.runInContext(source.slice(start, end < 0 ? undefined : end), context);
  }
}
"""


def run_node(script: str) -> dict:
    result = subprocess.run(
        [NODE, "-e", HARNESS + script], cwd=ROOT, check=True,
        capture_output=True, text=True, timeout=20,
    )
    return json.loads(result.stdout)


def test_slow_frames_do_not_starve_labels_and_immediate_updates_bypass_throttle():
    result = run_node(r"""
const queue = new Map(); let nextId = 0, paints = 0;
const ctx = {
  state: { labelFrame: 0, destroyed: false, paused: false, overlayPaintAt: 0,
    width: 800, height: 600, dpr: 1 },
  labelContext: { setTransform() {}, clearRect() {} }, FLOW_FRAME_MS: 34,
  raf: fn => { queue.set(++nextId, fn); return nextId; }, caf: id => queue.delete(id),
  flowAnimating: () => true, drawRelationFlow() {}, drawFocusRing() {},
  drawDeclutteredLabels: () => { paints++; }, drawHotEdgeDecorations() {}, drawHoverCardLayer() {},
};
load(ctx, ['drawOverlay', 'runOverlayFrame', 'scheduleLabels']);
// Match the main draw loop's order: register its next frame, then request labels.
function mainFrame() { ctx.raf(mainFrame); ctx.scheduleLabels(); }
ctx.raf(mainFrame);
for (let tick = 1; tick <= 20; tick++) {
  for (const [id, callback] of [...queue]) {
    if (!queue.delete(id)) continue;
    callback(tick * 50);
  }
}
const slowPaints = paints, queued = queue.size;
ctx.scheduleLabels(true);
const immediateId = ctx.state.labelFrame, callback = queue.get(immediateId);
queue.delete(immediateId); callback(1001);
console.log(JSON.stringify({ slowPaints, queued, immediatePaints: paints - slowPaints }));
""")
    assert result["slowPaints"] >= 10
    assert result["queued"] <= 2
    assert result["immediatePaints"] == 1


def test_same_size_resize_repaints_the_cleared_region_canvas():
    result = run_node(r"""
let painted = false, paintCalls = 0;
const backing = () => ({ set width(_) { painted = false; }, set height(_) { painted = false; } });
const ctx = {
  state: { ready: true, camera: { x: 0, y: 0, scale: 1 }, width: 800, height: 600,
    dpr: 1, styleName: 'galaxy', palette: 'ocean', colorBy: 'community', underlayKey: '',
    communityRegions: [{ x: 0, y: 0, r: 30, tint: [1, 0, 0], count: 3 }] },
  underlay: backing(), canvas: backing(), labels: backing(),
  underlayContext: { setTransform() {}, clearRect() { painted = false; },
    createRadialGradient() { return { addColorStop() {} }; }, beginPath() {}, arc() {},
    fill() { painted = true; paintCalls++; }, stroke() {} },
  element: { getBoundingClientRect: () => ({ width: 800, height: 600 }) },
  window: { devicePixelRatio: 1 }, zoomRatio: () => 1, screen: () => [400, 300],
  clamp: (value, min, max) => Math.max(min, Math.min(max, value)),
  schedule() {}, scheduleLabels() {},
};
load(ctx, ['drawRegions', 'resize']);
ctx.drawRegions(); const before = painted;
ctx.resize(); const cleared = !painted;
ctx.drawRegions();
console.log(JSON.stringify({ before, cleared, repainted: painted, paintCalls }));
""")
    assert result == {"before": True, "cleared": True, "repainted": True, "paintCalls": 2}


def test_authored_cached_orbit_radius_survives_satellite_motion():
    result = run_node(r"""
const window = {};
new Function('window', fs.readFileSync('engraphis/dashboard_assets/engraphis-graph.js', 'utf8'))(window);
const nodes = [{ id: 'star', x: 0, y: 0 }, ...[31, 32, 33].map((x, i) => ({
  id: 'p' + i, system_anchor_id: 'star', orbit_tier: 1,
  orbit_radius: 30, __galaxyOrbitBaseRadius: 30, x, y: 0,
}))];
const lanes = window.EngraphisGraph._internals.prepareGalaxyOrbitLaneTopology(nodes);
console.log(JSON.stringify({ lanes: lanes.map(({ radius, members, dynamicRadius }) =>
  ({ radius, members, dynamicRadius })) }));
""")
    assert result["lanes"] == [{"radius": 30, "members": 3, "dynamicRadius": False}]
