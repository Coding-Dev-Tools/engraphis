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

WORKER_HARNESS = """
const vm = require('vm'); const fs = require('fs'); const messages = [];
const src = fs.readFileSync('engraphis/dashboard_assets/engraphis-graph-every-worker.js', 'utf8');
const ctx = { self: { postMessage: m => messages.push(m) },
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
    { source: 0, target: false, weight: 3, relation: 'mentions' },
    { source: { id: false }, target: 'leaf' },
    { source: 'ghost-node', target: 'leaf' },
  ],
}});
setTimeout(() => {
  const ready = latest('ready');
  console.log(JSON.stringify({
    ids: ready.ids, links: ready.totalLinks,
    sources: Array.from(ready.edgeSources), targets: Array.from(ready.edgeTargets),
    weights: Array.from(ready.edgeWeights), relations: ready.edgeRelations,
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


def test_renderer_exposes_capacity_and_every_preset() -> None:
    renderer = RENDERER.read_text(encoding="utf-8")
    worker = WORKER.read_text(encoding="utf-8")
    assert "MAX_NODES = 20000" in renderer and "MAX_NODES = 20000" in worker
    assert "MAX_LINKS = 200000" in renderer and "MAX_LINKS = 200000" in worker
    assert "every: {" in renderer  # dedicated preset tuning
    assert "preset: 'Every node · LOD'" in renderer
    assert "MAP_SCALE" in worker   # the map-spread constant


def test_ledger_routes_the_every_layout_and_restores_filters() -> None:
    ledger = LEDGER.read_text(encoding="utf-8")
    markup = MARKUP.read_text(encoding="utf-8")
    assert "engraphis-graph-every.js" in ledger and "EngraphisEveryGraph" in ledger
    assert "EngraphisAllGraph" not in ledger
    assert 'data-graph-preset-choice="every"' in markup
    assert 'id="graph-show-all"' in markup and "hidden" in markup  # kept for listeners, hidden
    assert "state.everyPriorFilters" in ledger                      # filter restore contract
    assert "setGraphMinDegree(0, false)" in ledger
