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


def test_hover_selection_and_clear_invalidate_settled_geometry():
    result = run_node(r"""
let scheduled = 0;
const uploads = [];
const ctx = {
  state: { ready: true, contextLost: false, hover: 0, focus: -1,
    neighbors: [[1], [0], []], nodeFlags: new Uint8Array([1, 1, 1]) },
  gl: { bindBuffer() {}, bufferData(_, flags) { uploads.push(Array.from(flags)); } },
  nodeProgram: {}, nodeBuffers: { flag: {} }, schedule() { scheduled++; },
};
load(ctx, ['applyHoverToFlags']);
ctx.applyHoverToFlags();
ctx.state.hover = -1; ctx.state.focus = 2; ctx.applyHoverToFlags();
ctx.state.focus = -1; ctx.applyHoverToFlags();
console.log(JSON.stringify({ scheduled, uploads }));
""")
    assert result == {"scheduled": 3, "uploads": [[1, 1, 2], [2, 2, 1], [1, 1, 1]]}


def test_worker_refinement_repaints_without_overriding_owned_camera():
    result = run_node(r"""
let scheduled = 0, overlays = 0, uploads = 0;
const ctx = {
  state: { positions: new Float32Array([1, 2]), bounds: null, hover: 0, focus: -1,
    camera: { x: 73, y: 29, scale: 2 }, cameraOwned: true, pendingFit: null,
    edgeVertexCount: 0, lastLabelKey: 'old' },
  uploadNodePositions() { uploads++; }, uploadEdges() {}, uploadEdgePositions() {},
  computeCommunityRegions() {}, drawRegions() {}, schedule() { scheduled++; },
  scheduleLabels() { overlays++; },
};
load(ctx, ['camera', 'fit', 'applyLayout']);
ctx.applyLayout(new Float32Array([5, 8]), { minX: 0, maxX: 10, minY: 0, maxY: 10 }, true);
console.log(JSON.stringify({ scheduled, overlays, uploads, camera: ctx.state.camera,
  cameraOwned: ctx.state.cameraOwned, hoverPoint: ctx.state.hoverPoint,
  pickDirty: ctx.state.pickDirty, lastLabelKey: ctx.state.lastLabelKey }));
""")
    assert result == {
        "scheduled": 1, "overlays": 1, "uploads": 1,
        "camera": {"x": 73, "y": 29, "scale": 2}, "cameraOwned": True,
        "hoverPoint": [5, 8], "pickDirty": True, "lastLabelKey": "",
    }


@pytest.mark.parametrize("hz", [60, 120])
def test_camera_invalidations_paint_each_frame_with_flow_without_redrawing_at_rest(hz):
    result = run_node(r"""
let draws = 0, scheduled = 0;
const gl = new Proxy({ clear() { draws++; } }, { get(target, key) {
  return target[key] || (() => {});
}});
const ctx = {
  state: { destroyed: false, paused: false, contextLost: false, ready: true,
    camera: { x: 0, y: 0, scale: 1 }, dpr: 1, ids: ['n'], settings: { flow: true },
    edgeVertexCount: 0, hover: -1, focus: -1, hoverPoint: [0, 0], focusPoint: [0, 0] },
  canvas: { width: 800, height: 600 }, gl, nodeProgram: {}, edgeProgram: {},
  nodeBuffers: { attrs: {} }, edgeBuffers: { attrs: {} }, glowAmount: () => 0,
  zoomRatio: () => 0, EDGE_START: 0.2, FLOW_FRAME_MS: 34,
  clamp: (value, min, max) => Math.max(min, Math.min(max, value)),
  flowAnimating: () => true, bindNodeGeometry() {}, bindEdgeGeometry() {},
  drawRegions() {}, scheduleLabels() {}, schedule() { scheduled++; },
};
load(ctx, ['draw']);
const hz = HZ;
for (let frame = 1; frame <= hz; frame++) {
  ctx.state.camera.x += 1;
  ctx.draw(frame * 1000 / hz);
}
console.log(JSON.stringify({ draws, scheduled }));
""".replace("HZ", str(hz)))
    assert result == {"draws": hz, "scheduled": 0}


def test_every_labels_obey_density_and_remeasure_font_changes():
    result = run_node(r"""
let measured = 0, paints = [];
const context = { fillText(text) { paints.push(text); }, measureText(text) {
  measured++; return { width: parseFloat(this.font) * text.length / 2 };
}};
const ctx = {
  state: { ready: true, camera: { x: 0, y: 0, scale: 1 },
    width: 1000, height: 600, settings: { labels: true, font: 12, labelDensity: 1 },
    labelLayout: [], labelMetrics: new Map(), lastLabelKey: '',
    nodeFlags: new Float32Array(30).fill(1), positions: new Float32Array(60),
    topNodes: Array.from({ length: 30 }, (_, i) => i), hover: -1, focus: -1 },
  labelContext: context, LABEL_MAX: 220, LABEL_CANDIDATE_MAX: 1800,
  labelText: i => 'Name' + i,
  screen: (_, __) => [0, 0],
  clamp: (value, min, max) => Math.max(min, Math.min(max, value)),
};
for (let i = 0; i < 30; i++) {
  ctx.state.positions[i * 2] = 50 + (i % 6) * 150;
  ctx.state.positions[i * 2 + 1] = 50 + Math.floor(i / 6) * 100;
}
ctx.screen = (x, y) => [x, y];
load(ctx, ['drawDeclutteredLabels']);
ctx.drawDeclutteredLabels(); const low = paints.length;
paints = []; ctx.state.settings.labelDensity = 24;
ctx.drawDeclutteredLabels(); const normal = paints.length, before = measured;
paints = []; ctx.state.settings.font = 24;
ctx.drawDeclutteredLabels(); const remeasured = measured - before;
console.log(JSON.stringify({ low, normal, remeasured, cache: [...ctx.state.labelMetrics.keys()] }));
""")
    assert result["low"] == 1
    assert result["normal"] == 24
    assert result["remeasured"] >= 24
    assert any(key.startswith("24px ") for key in result["cache"])


def test_zoomed_out_picking_respects_screen_hit_radius_and_visibility():
    result = run_node(r"""
const ctx = {
  state: { ready: true, ids: ['near', 'hidden'], nodeFlags: [1, 0],
    positions: [0, 0, 100, 0], pickDirty: true, camera: { scale: 0.02 } },
  pointSize: () => 4,
};
load(ctx, ['buildPickGrid', 'pickAt']);
const exact = ctx.pickAt(0, 0), twoPixels = ctx.pickAt(100, 0);
const outside = ctx.pickAt(1000, 0);
ctx.state.camera.scale = 4;
const close = ctx.pickAt(2, 0), closeOutside = ctx.pickAt(3, 0);
console.log(JSON.stringify({ exact, twoPixels, outside, close, closeOutside }));
""")
    assert result == {"exact": 0, "twoPixels": 0, "outside": -1, "close": 0, "closeOutside": -1}


def test_hidden_refinement_defers_upload_until_resume():
    result = run_node(r"""
let applications = [];
const ctx = {
  state: { paused: true, contextLost: false, destroyed: false,
    pendingLayout: { positions: [9, 12], bounds: { minX: 9 }, fit: true } },
  applyLayout: (...args) => applications.push(args),
};
load(ctx, ['flushPendingLayout']);
ctx.flushPendingLayout(); const hidden = applications.length;
ctx.state.paused = false; ctx.flushPendingLayout(); ctx.flushPendingLayout();
console.log(JSON.stringify({ hidden, applications, retained: ctx.state.pendingLayout }));
""")
    assert result == {
        "hidden": 0, "applications": [[[9, 12], {"minX": 9}, True]], "retained": None,
    }
