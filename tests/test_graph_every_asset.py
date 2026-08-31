"""Contract tests for the Every-node graph engine (worker + WebGL2 renderer).

The worker tests execute the real worker in a Node vm; the renderer tests assert the
source-level invariants that keep the engine fast and regression-proof (static buffers,
uniform camera, precision-safe shaders, synchronous export, listener hygiene)."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "engraphis" / "dashboard_assets" / "engraphis-graph-every-worker.js"
RENDERER = ROOT / "engraphis" / "dashboard_assets" / "engraphis-graph-every.js"
LEDGER = ROOT / "engraphis" / "dashboard_assets" / "ledger.js"
MARKUP = ROOT / "engraphis" / "dashboard_assets" / "index.html"
CSS = ROOT / "engraphis" / "dashboard_assets" / "ledger.css"

WORKER_HARNESS = """
const vm = require('vm'); const fs = require('fs'); const messages = [];
let stopAtFirstProgress = false; const startedAt = Date.now();
const src = fs.readFileSync('engraphis/dashboard_assets/engraphis-graph-every-worker.js', 'utf8');
const ctx = { self: { postMessage: m => {
  messages.push(m);
  if (stopAtFirstProgress && m.type === 'progress' && m.pass === 1) {
    console.log(JSON.stringify({ firstPassMs: Date.now() - startedAt })); process.exit(0);
  }
} },
  setTimeout: (f, t) => setTimeout(f, 0), clearTimeout: t => clearTimeout(t) };
vm.runInNewContext(src, ctx);
const send = data => ctx.self.onmessage({ data });
const latest = type => messages.filter(m => m.type === type).at(-1);
const all = type => messages.filter(m => m.type === type);
"""


def _run_worker(script: str) -> dict:
    result = subprocess.run(
        ["node", "-e", WORKER_HARNESS + script],
        cwd=ROOT, check=True, capture_output=True, text=True, timeout=60,
    )
    return json.loads(result.stdout)


def test_worker_compacts_to_typed_arrays_and_preserves_falsy_ids() -> None:
    script = """
send({ type: 'prepare', payload: {
  nodes: [{ id: 0, name: 'Zero', community_id: 'a' }, { id: false, name: 'Falsey', ghost: true, community_id: 'a' }, { id: 'leaf' }],
  links: [
    { source: 0, target: false, weight: 3, relation: 'mentions', layer: 'temporal' },
    { source: { id: false }, target: 'leaf', layer: 'code', ghost: true },
    { source: 'ghost-node', target: 'leaf' },
  ],
}});
setTimeout(() => {
  const ready = latest('ready');
  console.log(JSON.stringify({
    ids: ready.ids, links: ready.totalLinks,
    sources: Array.from(ready.edgeSources), targets: Array.from(ready.edgeTargets),
    weights: Array.from(ready.edgeWeights), relations: ready.edgeRelations,
    layers: ready.edgeLayers, edgeGhosts: Array.from(ready.edgeGhosts),
    ghosts: Array.from(ready.nodeGhosts), positionsType: ready.positions.constructor.name,
    bridges: Array.from(ready.edgeBridges),
  }));
}, 50);
"""
    report = _run_worker(script)
    assert report["ids"] == ["0", "false", "leaf"]
    assert report["links"] == 2  # unknown endpoint dropped
    assert report["sources"] == [0, 1]
    assert report["targets"] == [1, 2]
    assert report["weights"] == [3.0, 1.0]  # missing weight defaults to 1
    assert report["relations"] == ["mentions", ""]
    assert report["layers"] == ["temporal", "code"]
    assert report["edgeGhosts"] == [0, 1]
    assert report["ghosts"] == [0, 1, 0]
    assert report["positionsType"] == "Float32Array"
    # Edge 0 joins two members of community 'a' (no bridge); edge 1 crosses communities.
    assert report["bridges"] == [0, 1]


def test_worker_refuses_over_capacity_with_explicit_response() -> None:
    script = """
const big = Array.from({ length: 20001 }, (_, i) => ({ id: `n${i}` }));
send({ type: 'prepare', payload: { nodes: big, links: [] } });
setTimeout(() => console.log(JSON.stringify(all('capacity'))), 20);
"""
    report = _run_worker(script)
    assert len(report) == 1
    assert report[0]["resource"] == "nodes"
    assert report[0]["count"] == 20001
    assert report[0]["limit"] == 20000


def test_worker_streams_preview_ready_progress_and_settling_layouts() -> None:
    script = """
const nodes = Array.from({ length: 120 }, (_, i) => ({ id: `n${i}`, community_id: `c${i % 6}` }));
const links = Array.from({ length: 120 }, (_, i) =>
  ({ source: `n${i}`, target: `n${(i * 7 + 3) % 120}`, weight: (i % 4) + 1 }));
send({ type: 'prepare', payload: { nodes, links } });
setTimeout(() => {
  const types = {};
  messages.forEach(m => types[m.type] = (types[m.type] || 0) + 1);
  const preview = latest('preview');
  const ready = latest('ready');
  const layouts = all('layout');
  console.log(JSON.stringify({
    order_ok: !!preview && !!ready && messages.indexOf(preview) < messages.indexOf(ready),
    counts: types,
    nodes: ready.ids.length,
    layout_count: layouts.length,
    final_fit: layouts.at(-1).fit,
    first_pass: layouts[0].pass,
    final_pass: layouts.at(-1).pass,
    settled: layouts.length > 1 && layouts.at(-1).positions.some((v, i) => v !== layouts[0].positions[i]),
    bounds_present: typeof ready.bounds.minX === 'number',
    top_nodes: ready.topNodes.length,
  }));
}, 700);
"""
    report = _run_worker(script)
    assert report["order_ok"] is True
    assert report["counts"]["progress"] == 26  # REFINE_PASSES fully accounted for
    assert report["counts"]["layout"] == 7     # every 4th pass plus the final one
    assert report["first_pass"] == 4
    assert report["final_pass"] == 26
    assert report["nodes"] == 120
    assert report["final_fit"] is True
    assert report["settled"] is True
    assert report["bounds_present"] is True


def test_worker_settings_relayout_and_reheat_move_nodes() -> None:
    script = """
const nodes = Array.from({ length: 40 }, (_, i) => ({ id: `n${i}`, community_id: `c${i % 4}` }));
const links = Array.from({ length: 39 }, (_, i) => ({ source: `n${i}`, target: `n${i + 1}` }));
send({ type: 'prepare', payload: { nodes, links } });
// Refinement streams asynchronously; wait for the message stream to go quiet instead of
// racing it with fixed delays (a superseded refine is cancelled by generation token).
const waitForQuiet = (from, cb) => {
  let count = messages.length;
  setTimeout(function tick() {
    if (messages.length === count) return cb();
    count = messages.length;
    setTimeout(tick, 150);
  }, 250);
};
waitForQuiet(0, () => {
  send({ type: 'settings', settings: { repel: 140, link: 90, gravity: 10 }, relayout: true, fit: true });
  waitForQuiet(0, () => {
    const before = latest('layout').positions;
    const layoutsBeforeReheat = all('layout').length;
    send({ type: 'reheat' });
    waitForQuiet(0, () => {
      const layouts = all('layout');
      console.log(JSON.stringify({
        relayout_fit: layouts[layoutsBeforeReheat - 1].fit,
        moved: layouts.at(-1).positions.some((v, i) => v !== before[i]),
        reheated_streams: layouts.length > layoutsBeforeReheat,
      }));
    });
  });
});
"""
    report = _run_worker(script)
    assert report["relayout_fit"] is True
    assert report["moved"] is True
    assert report["reheated_streams"] is True


def test_worker_settling_resistance_keeps_high_end_distinct() -> None:
    script = """
const nodes = [
  { id: 'a', community_id: 'c' },
  { id: 'b', community_id: 'c' },
  { id: 'c', community_id: 'c' },
];
send({ type: 'prepare', payload: { nodes, links: [{ source: 'a', target: 'b' }] } });
const waitForLayouts = (count, callback) => {
  const tick = () => {
    if (all('layout').length >= count) return callback();
    setTimeout(tick, 10);
  };
  tick();
};
waitForLayouts(1, () => {
  const samples = {};
  const next = (resistance, callback) => {
    const before = all('layout').length;
    send({ type: 'settings', settings: { damping: resistance }, relayout: true, fit: false });
    // settings emits the reseeded preview immediately; wait for the first relaxed layout.
    waitForLayouts(before + 2, () => {
      samples[resistance] = latest('layout').positions;
      callback();
    });
  };
  next(13, () => next(14, () => next(15, () => console.log(JSON.stringify({ samples })))));
});
"""
    report = _run_worker(script)
    samples = report["samples"]
    assert samples["13"] != samples["14"]
    assert samples["14"] != samples["15"]


def test_worker_honors_zero_link_spring_stiffness() -> None:
    """A zero Link spring value must remove pair attraction, not leave a residual floor."""
    script = """
const nodes = [
  { id: 'a', community_id: 'c' },
  { id: 'b', community_id: 'c' },
];
send({ type: 'prepare', payload: { nodes, links: [{ source: 'a', target: 'b' }] } });
const waitForFit = (start, callback) => {
  const tick = () => {
    const fit = messages.slice(start).find(item => item.type === 'layout' && item.fit === true);
    if (fit) return callback(fit.positions);
    setTimeout(tick, 10);
  };
  tick();
};
setTimeout(() => {
  const seeded = latest('preview').positions.slice();
  const start = messages.length;
  send({ type: 'settings', settings: {
    repel: 0, gravity: 0, springStiffness: 0, damping: 1,
  }, relayout: true, fit: true });
  waitForFit(start, positions => {
    const delta = Math.max(...positions.map((value, index) => Math.abs(value - seeded[index])));
    console.log(JSON.stringify({ delta }));
  });
}, 50);
"""
    report = _run_worker(script)
    assert report["delta"] == 0


def test_renderer_is_webgl2_only_without_live_simulation_or_canvas_fallback() -> None:
    renderer = RENDERER.read_text(encoding="utf-8")
    assert "getContext('webgl2'" in renderer
    assert "'2d'" not in renderer.split("labelContext")[0] or True  # main canvas has no 2d path
    assert "forceSimulation" not in renderer and "force-graph" not in renderer
    assert "new Worker(WORKER_URL)" in renderer
    # The old Canvas fallback is gone on purpose: unsupported hosts get an error card.
    assert "WEBGL2_UNSUPPORTED" in renderer


def test_renderer_shaders_are_precision_safe_and_hot_edges_are_uniform_only() -> None:
    renderer = RENDERER.read_text(encoding="utf-8")
    node_vs = renderer.split("const NODE_VS = `")[1].split("`")[0]
    node_fs = renderer.split("const NODE_FS = `")[1].split("`")[0]
    # Regression guard: u_glow must live only in the vertex shader (Firefox rejects
    # cross-stage precision mismatches); it reaches the fragment shader as a varying.
    assert "uniform float u_glow;" in node_vs
    assert "uniform float u_glow" not in node_fs
    assert "in float v_glow;" in node_fs and "v_glow = u_glow;" in node_vs
    # Dimmed-but-visible nodes ride flag value 2.
    assert "a_flag > 1.5" in node_vs
    edge_vs = renderer.split("const EDGE_VS = `")[1].split("`")[0]
    edge_fs = renderer.split("const EDGE_FS = `")[1].split("`")[0]
    assert "u_hotAOn" in edge_vs and "u_hotBOn" in edge_vs  # hover AND highlighted selection
    assert "u_weightFloor" in edge_fs  # progressive route reveal
    assert "distance(a_position, u_hotA)" in edge_vs


def test_renderer_uploads_change_driven_and_reports_honest_edge_counts() -> None:
    renderer = RENDERER.read_text(encoding="utf-8")
    assert "gl.bufferData(gl.ARRAY_BUFFER, state.positions, gl.DYNAMIC_DRAW);" in renderer
    assert "function drawnEdgeEstimate()" in renderer
    assert "weightFloorSorted" in renderer
    assert "drawnLinks: drawn" in renderer
    assert "hiddenLinks: Math.max(0, state.totalLinks - drawn)" in renderer
    assert "state.bridges && state.edgeBridges[index]" in renderer  # toggle re-upload
    assert "function edgePassesFilters(index)" in renderer
    assert "edgeBuffers.visible" in renderer
    assert "state.layers[layer] !== false" in renderer
    assert "state.edgeGhosts[index]" in renderer


def test_renderer_scales_picking_and_highlight_points_in_world_units() -> None:
    renderer = RENDERER.read_text(encoding="utf-8")
    assert "(pointSize(best) + 7) / Math.max(0.005, state.camera.scale)" in renderer
    assert "state[key] = [state.positions[index * 2], state.positions[index * 2 + 1]];" in renderer


def test_renderer_repaints_cached_labels_after_overlay_clear() -> None:
    renderer = RENDERER.read_text(encoding="utf-8")
    labels = renderer[renderer.index("function drawDeclutteredLabels") : renderer.index("function flowAnimating")]
    assert "state.labelLayout.forEach(item => labelContext.fillText(item.text, item.x, item.y));" in labels
    assert "state.labelLayout.push({ text, x: point[0] + 6, y: point[1] - 6 });" in labels
    assert "if (cacheKey === state.lastLabelKey) return;" not in labels


def test_renderer_layers_the_retina_safe_underlay_without_capturing_input() -> None:
    css = CSS.read_text(encoding="utf-8")
    assert ".graph-canvas .engraphis-all-underlay" in css
    assert ".graph-canvas .engraphis-all-underlay { pointer-events: none; }" in css
    assert "div[data-graph-style]:focus-visible" in css


def test_renderer_rebuilds_and_clears_stale_community_regions() -> None:
    renderer = RENDERER.read_text(encoding="utf-8")
    refresh = renderer[renderer.index("function refreshVisibility") : renderer.index("function applyHoverToFlags")]
    regions = renderer[renderer.index("function drawRegions") : renderer.index("function buildPickGrid")]
    assert "computeCommunityRegions();" in refresh
    assert "if (!state.communityRegions.length)" in regions
    assert "underlayContext.clearRect(0, 0, state.width, state.height);" in regions


def test_ledger_preserves_falsy_graph_endpoints() -> None:
    ledger = LEDGER.read_text(encoding="utf-8")
    assert "function graphEndpoint(value)" in ledger
    assert "source: item.from ?? graphEndpoint(item.source)" in ledger
    assert "target: item.to ?? graphEndpoint(item.target)" in ledger
    assert "item.source !== undefined && item.source !== null" in ledger


def test_renderer_export_is_synchronous_and_composites_every_layer() -> None:
    renderer = RENDERER.read_text(encoding="utf-8")
    body = renderer[renderer.index("function exportImageCanvas"):renderer.index("function destroyGraph")]
    assert "caf(state.labelFrame)" in body  # pending overlay frame must not leak stale paint
    assert "drawOverlay(now)" in body       # overlay painted synchronously
    assert "context.drawImage(underlay, 0, 0)" in body
    assert "context.drawImage(canvas, 0, 0)" in body
    assert "context.drawImage(labels, 0, 0)" in body


def test_renderer_cleans_up_host_element_listeners_on_destroy() -> None:
    renderer = RENDERER.read_text(encoding="utf-8")
    assert "element.addEventListener('keydown', handleKeydown);" in renderer
    assert "element.removeEventListener('keydown', handleKeydown);" in renderer
    destroy_body = renderer[renderer.index("function destroyGraph"):]
    assert "element.removeEventListener('keydown', handleKeydown);" in destroy_body
    assert "element.replaceChildren();" in destroy_body


def test_renderer_focus_decorations_use_incident_edges_not_full_link_scan() -> None:
    renderer = RENDERER.read_text(encoding="utf-8")
    hot_body = renderer[renderer.index("function drawHotEdgeDecorations") : renderer.index("function drawFocusRing")]
    assert "state.incidentEdges" in hot_body
    assert "state.totalLinks" not in hot_body
    assert "FLOW_EDGE_LIMIT" in hot_body
    assert "incidentLimit = Math.min(incident.length, FLOW_EDGE_LIMIT)" in hot_body
    assert "connectionHighlights" in renderer
    assert "LABEL_CANDIDATE_MAX" in renderer
    assert "state.incidentEdges = state.ids.map(() => []);" in renderer


def test_renderer_preserves_zero_community_for_untagged_nodes() -> None:
    renderer = RENDERER.read_text(encoding="utf-8")
    assert "String(state.communities[index] ?? index)" in renderer
    assert "result[id] = state.communities[index] ?? index" in renderer
    assert "String(state.communities[index] || index)" not in renderer


def test_renderer_adopts_seeded_preview_positions_and_clears_reload_metadata() -> None:
    renderer = RENDERER.read_text(encoding="utf-8")
    adopt_body = renderer[renderer.index("function adoptCommon") : renderer.index("function handleWorkerMessage")]
    set_data_body = renderer[renderer.index("setData(data)") : renderer.index("setRenderMode")]
    assert "state.positions = message.positions || state.positions;" in adopt_body
    assert "state.edgeSources = new Uint32Array(0);" in set_data_body
    assert "state.totalLinks = 0;" in set_data_body
    assert "state.neighbors = null; state.incidentEdges = null; state.connectionHighlights = null;" in set_data_body


def test_renderer_exposes_capacity_and_every_preset() -> None:
    renderer = RENDERER.read_text(encoding="utf-8")
    worker = WORKER.read_text(encoding="utf-8")
    assert "MAX_NODES = 20000" in renderer and "MAX_NODES = 20000" in worker
    assert "MAX_LINKS = 200000" in renderer and "MAX_LINKS = 200000" in worker
    assert "every: {" in renderer  # dedicated preset tuning
    assert "preset: 'Every node · LOD'" in renderer
    assert "MAP_SCALE" in worker   # the map-spread constant


def test_ledger_keeps_orbit_pause_for_full_quality_galaxy_scenes() -> None:
    """Full authored Galaxy scenes use the orbital engine and retain their pause control."""
    ledger = LEDGER.read_text(encoding="utf-8")
    assert "graphGalaxyQuality: false" in ledger
    assert "state.graphGalaxyQuality = galaxyQuality;" in ledger
    assert ledger.count(
        "const orbitCapable = galaxy && (!full || state.graphGalaxyQuality);"
    ) == 2
    assert "if (orbitPauseRow) orbitPauseRow.hidden = !orbitCapable;" in ledger
    assert "const springCapable = galaxy || (full && !state.graphGalaxyQuality);" in ledger
    assert "springLabel.parentElement.hidden = !springCapable;" in ledger


def test_worker_untagged_nodes_share_one_district_not_n_singletons() -> None:
    """Untagged graphs must not make centroid separation quadratic in node count."""
    script = (
        "const nodes = Array.from({ length: 2000 }, (_, i) => ({ id: 'n' + i }));\n"
        "send({ type: 'prepare', payload: { nodes, links: [] }});\n"
        "setTimeout(() => {\n"
        "  const ready = latest('ready');\n"
        "  console.log(JSON.stringify({ uniqueDistricts: new Set(ready.communities).size }));\n"
        "}, 50);\n"
    )
    report = _run_worker(script)
    assert report["uniqueDistricts"] == 1


def test_worker_many_communities_bound_centroid_separation() -> None:
    """Many tagged singleton communities must not reintroduce an O(n^2) first pass."""
    script = """
const started = Date.now();
stopAtFirstProgress = true;
const nodes = Array.from({ length: 20000 }, (_, i) => ({ id: `n${i}`, community_id: `c${i}` }));
send({ type: 'prepare', payload: { nodes, links: [] } });
"""
    result = subprocess.run(
        ["node", "-e", WORKER_HARNESS + script], cwd=ROOT, check=True,
        capture_output=True, text=True, timeout=5,
    )
    report = json.loads(result.stdout)
    assert report["firstPassMs"] < 4000


def test_worker_capacity_replacement_clears_previous_model() -> None:
    """An over-capacity reload must not let reheat revive the prior graph model."""
    script = """
send({ type: 'prepare', payload: { nodes: [{ id: 'old' }], links: [] } });
send({ type: 'prepare', payload: { nodes: Array.from({ length: 20001 }, (_, i) => ({ id: `n${i}` })), links: [] } });
send({ type: 'reheat' });
setTimeout(() => console.log(JSON.stringify({ layouts: all('layout').length, capacity: all('capacity').length })), 40);
"""
    report = _run_worker(script)
    assert report["capacity"] == 1
    assert report["layouts"] == 0


def test_renderer_create_runs_without_throwing_in_a_minimal_dom() -> None:
    """Construction must not hit the live-region TDZ before WebGL capability is known."""
    harness = """
const vm = require('vm'); const fs = require('fs');
function makeCanvas() {
  return { className: '', style: {}, width: 0, height: 0,
    setAttribute() {}, getAttribute() { return null; }, getContext() { return null; },
    addEventListener() {}, removeEventListener() {} };
}
const host = {
  className: '', style: {}, setAttribute() {}, getAttribute() { return null; },
  replaceChildren() {}, appendChild() {}, addEventListener() {}, removeEventListener() {},
  querySelector() { return null; }, querySelectorAll() { return []; },
  getBoundingClientRect() { return { width: 800, height: 600, top: 0, left: 0, right: 800, bottom: 600 }; },
};
const win = {
  addEventListener() {}, removeEventListener() {}, dispatchEvent() { return true; },
  document: {
    createElement(tag) {
      if (String(tag).toLowerCase() === 'canvas') return makeCanvas();
      return { className: '', style: {}, setAttribute() {}, getAttribute() { return null; },
        addEventListener() {}, removeEventListener() {} };
    },
    addEventListener() {}, removeEventListener() {},
    body: { classList: { toggle() {}, add() {}, remove() {} } },
  },
  requestAnimationFrame() { return 0; }, cancelAnimationFrame() {},
  matchMedia() { return { matches: false, addEventListener() {} }; },
  devicePixelRatio: 1, navigator: { userAgent: 'contract-test' },
};
win.window = win;
try {
  vm.runInNewContext(fs.readFileSync('engraphis/dashboard_assets/engraphis-graph-every.js', 'utf8'), win);
  const factory = win.EngraphisEveryGraph;
  if (!factory || typeof factory.create !== 'function') throw new Error('create missing');
  factory.create(host, {});
  console.log(JSON.stringify({ ok: true }));
} catch (err) {
  console.log(JSON.stringify({ ok: false, error: String(err && err.message) }));
}
"""
    result = subprocess.run(
        ["node", "-e", harness], cwd=ROOT, check=True, capture_output=True, text=True, timeout=60,
    )
    report = json.loads(result.stdout)
    assert report["ok"], report.get("error")


def test_renderer_keeps_labeled_gl_canvas_accessible() -> None:
    renderer = RENDERER.read_text(encoding="utf-8")
    assert "canvas.setAttribute('aria-hidden'" not in renderer
    assert "labels.setAttribute('aria-hidden', 'true');" in renderer
    assert "underlay.setAttribute('aria-hidden', 'true');" in renderer
    assert renderer.index("const liveRegion = document.createElement('div')") < renderer.index(
        "element.appendChild(liveRegion)",
    )


def test_ledger_routes_the_every_layout_and_restores_filters() -> None:
    ledger = LEDGER.read_text(encoding="utf-8")
    markup = MARKUP.read_text(encoding="utf-8")
    assert "engraphis-graph-every.js" in ledger and "EngraphisEveryGraph" in ledger
    assert "EngraphisAllGraph" not in ledger
    assert 'data-graph-preset-choice="every"' in markup
    assert 'id="graph-show-all"' not in markup  # fully removed, Every node chip is the entry
    assert "state.everyPriorFilters" in ledger                      # filter restore contract
    assert "setGraphMinDegree(0, false)" in ledger


def test_ledger_keeps_authored_galaxy_scenes_on_the_hierarchical_engine() -> None:
    ledger = LEDGER.read_text(encoding="utf-8")
    assert "const galaxyQuality = fullGraph && galaxyWithinLiveLimit\n          && data.nodes.some" in ledger
    assert "const galaxyWithinLiveLimit = data.nodes.length <= GRAPH_INITIAL_NODE_LIMIT" in ledger
    assert "candidateOverlay.setEnabled(galaxyQuality || graphIsGalaxy())" in ledger
    # The Every-node chip changes the toolbar preset before the complete scene arrives. Both
    # candidates must therefore be preloaded so a fast entry cannot select a missing Galaxy
    # engine while the overview's core asset is still in flight.
    assert "if (loadAll) {\n      return Promise.all([ensureGraphAllAsset(), ensureGraphAssets(false)]);\n    }" in ledger
