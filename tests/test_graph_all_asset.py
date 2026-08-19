"""Focused contract tests for the worker-backed all-node graph profile."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "engraphis" / "dashboard_assets" / "engraphis-graph-worker.js"
RENDERER = ROOT / "engraphis" / "dashboard_assets" / "engraphis-graph-all.js"
LEDGER = ROOT / "engraphis" / "dashboard_assets" / "ledger.js"
MARKUP = ROOT / "engraphis" / "dashboard_assets" / "index.html"
STYLES = ROOT / "engraphis" / "dashboard_assets" / "ledger.css"


def _run_worker(nodes, links):
    source = json.dumps(WORKER.read_text(encoding="utf-8"))
    payload = json.dumps({"nodes": nodes, "links": links})
    script = f"""
const vm = require('vm'); const messages = [];
const context = {{ self: {{ postMessage: (message) => messages.push(message) }} }};
vm.runInNewContext({source}, context);
context.self.onmessage({{ data: {{ type: 'prepare', payload: {payload} }} }});
const ready = messages.find(message => message.type === 'ready');
context.self.onmessage({{ data: {{ type: 'camera', x: 0, y: 0, scale: 0.2, width: 1200, height: 800 }} }});
const low = messages.filter(message => message.type === 'visible').at(-1);
context.self.onmessage({{ data: {{ type: 'camera', x: 0, y: 0, scale: 0.8, width: 1200, height: 800 }} }});
const medium = messages.filter(message => message.type === 'visible').at(-1);
context.self.onmessage({{ data: {{ type: 'camera', x: 0, y: 0, scale: 1.5, width: 1200, height: 800 }} }});
const high = messages.filter(message => message.type === 'visible').at(-1);
context.self.onmessage({{ data: {{ type: 'hit', request: 9, x: 0, y: 0, scale: 1 }} }});
const hit = messages.filter(message => message.type === 'hit').at(-1);
console.log(JSON.stringify({{ready: {{nodes: ready.totalNodes, links: ready.totalLinks, ids: ready.ids, positions: ready.positions.constructor.name, edges: ready.edgeSources.constructor.name}}, lod: {{low: low.drawnLinks, medium: medium.drawnLinks, high: high.drawnLinks}}, hit: hit.index}}));
"""
    result = subprocess.run(
        ["node", "-"], cwd=ROOT, check=True, capture_output=True, text=True, input=script,
    )
    return json.loads(result.stdout)


def test_all_worker_compacts_identity_builds_typed_arrays_and_hits_spatial_index():
    nodes = [{"id": f"n-{index}", "name": f"Node {index}"} for index in range(8)]
    links = [{"source": "n-0", "target": f"n-{index}", "weight": index + 1} for index in range(1, 8)]
    result = _run_worker(nodes, links)
    assert result["ready"] == {"nodes": 8, "links": 7, "ids": [f"n-{index}" for index in range(8)], "positions": "Float32Array", "edges": "Uint32Array"}
    assert result["lod"]["low"] == 0
    assert result["lod"]["medium"] <= 7 and result["lod"]["high"] <= 7
    assert result["hit"] >= 0


def test_worker_honours_scene_canonical_positions_and_global_anchor():
    source = json.dumps(WORKER.read_text(encoding="utf-8"))
    payload = json.dumps({
        "canonical_positions": True,
        "nodes": [
            {"id": "hole", "anchor_role": "global", "x": 12, "y": -8, "gravity_mass": 100},
            {"id": "outer", "anchor_role": "community", "x": 412, "y": 92, "gravity_mass": 2},
        ],
        "links": [],
    })
    script = f"""
const vm = require('vm'); const messages = [];
const context = {{ self: {{ postMessage: (message) => messages.push(message) }} }};
vm.runInNewContext({source}, context);
context.self.onmessage({{ data: {{ type: 'settings', settings: {{ mode: 'galaxy',
  repel: 100, link: 8, gravity: 48 }}, relayout: true }} }});
context.self.onmessage({{ data: {{ type: 'prepare', payload: {payload} }} }});
const ready = messages.find(message => message.type === 'ready');
context.self.onmessage({{ data: {{ type: 'settings', settings: {{ gravity: 100 }},
  relayout: true }} }});
const transformed = messages.filter(message => message.type === 'layout').at(-1);
console.log(JSON.stringify({{canonical: ready.canonicalPositions,
  positions: Array.from(ready.positions), transformed: Array.from(transformed.positions),
  roles: ready.anchorRoles}}));
"""
    result = subprocess.run(
        ["node", "-"], cwd=ROOT, check=True, capture_output=True, text=True, input=script,
    )
    value = json.loads(result.stdout)
    assert value["canonical"] is True
    assert value["roles"] == ["global", "community"]
    assert value["positions"] == [12, -8, 412, 92]
    assert value["transformed"][0:2] == [12, -8]
    assert value["transformed"] != value["positions"]


def test_all_renderer_is_flat_worker_webgl_and_not_a_live_force_simulation():
    worker = WORKER.read_text(encoding="utf-8")
    renderer = RENDERER.read_text(encoding="utf-8")
    assert "new Worker" in renderer and "webgl2" in renderer
    assert "Uint32Array" in renderer and "Float32Array" in renderer
    assert "forceSimulation" not in renderer and "force-graph" not in renderer
    assert "createRadialGradient" not in renderer and "shadowBlur" not in renderer
    assert "new Map" in worker and "state.grid" in worker
    assert "MEDIUM_ZOOM_EDGE_LIMIT" in worker and "HIGH_ZOOM_EDGE_LIMIT" in worker
    assert "CANVAS_MEDIUM_ZOOM_EDGE_LIMIT" in worker and "CANVAS_HIGH_ZOOM_EDGE_LIMIT" in worker
    assert "adjacencyOffsets" in worker and "edgePositions" in worker
    assert "lastCameraKey" in worker and "edgePositions.buffer" in worker
    assert "for (let index = 0; index < state.ids.length; index += 1) if (inViewport" not in worker


def test_all_renderer_declares_capacity_and_progressive_lod_profile():
    renderer = RENDERER.read_text(encoding="utf-8")
    worker = WORKER.read_text(encoding="utf-8")
    assert "MAX_NODES = 20000" in renderer and "MAX_NODES = 20000" in worker
    assert "All nodes · LOD" in renderer and "linksPending" in renderer
    assert "type: 'preview'" in worker and "type: 'capacity'" in worker


def test_all_renderer_keeps_controls_live_and_clears_transient_hover_paint():
    renderer = RENDERER.read_text(encoding="utf-8")
    worker = WORKER.read_text(encoding="utf-8")
    for marker in (
        "setPreset(value)", "setSettings(value)", "setLayers(value)",
        "setBridges(value)", "setGhosts(value)", "freeze(value = true)",
        "getPhysicsSnapshot()", "graphToScreen(x, y)",
    ):
        assert marker in renderer
    assert "type: 'settings'" in renderer and "message.type === 'settings'" in worker
    assert "setBridges(value)" in renderer and "type: 'ghosts'" in renderer
    assert "message.type === 'bridges'" in worker and "message.type === 'ghosts'" in worker
    assert "function drawLabels(clear = false, now = 0)" in renderer
    assert "function clearHover()" in renderer and "pointerout" in renderer


def test_worker_preserves_falsy_endpoint_ids_and_filters_ghost_nodes():
    source = json.dumps(WORKER.read_text(encoding="utf-8"))
    payload = json.dumps({
        "nodes": [
            {"id": 0, "x": 0, "y": 0},
            {"id": False, "x": 40, "y": 0, "ghost": True},
            {"id": "leaf", "x": 80, "y": 0},
        ],
        "links": [
            {"source": 0, "target": False},
            {"source": {"id": False}, "target": "leaf"},
        ],
    })
    script = f"""
const vm = require('vm'); const messages = [];
const context = {{ self: {{ postMessage: message => messages.push(message) }} }};
vm.runInNewContext({source}, context);
context.self.onmessage({{ data: {{ type: 'prepare', payload: {payload} }} }});
const ready = messages.find(message => message.type === 'ready');
context.self.onmessage({{ data: {{ type: 'ghosts', value: false }} }});
context.self.onmessage({{ data: {{ type: 'camera', x: 40, y: 0, scale: 0.2,
  width: 1200, height: 800 }} }});
const visible = messages.filter(message => message.type === 'visible').at(-1);
context.self.onmessage({{ data: {{ type: 'camera', x: 40, y: 0, scale: 0.8,
  width: 100000, height: 100000 }} }});
const wide = messages.filter(message => message.type === 'visible').at(-1);
context.self.onmessage({{ data: {{ type: 'hit', request: 4, x: 40, y: 0, scale: 1 }} }});
const hit = messages.filter(message => message.type === 'hit').at(-1);
console.log(JSON.stringify({{
  ids: ready.ids, links: ready.totalLinks, ghosts: Array.from(ready.nodeGhosts),
  visible: Array.from(visible.nodes).map(index => ready.ids[index]),
  wide: Array.from(wide.nodes).map(index => ready.ids[index]),
  hit: hit.index < 0 ? null : ready.ids[hit.index],
}}));
"""
    result = subprocess.run(
        ["node", "-"], cwd=ROOT, check=True, capture_output=True, text=True, input=script,
    )
    report = json.loads(result.stdout)
    assert report["ids"] == ["0", "false", "leaf"]
    assert report["links"] == 2
    assert report["ghosts"] == [0, 1, 0]
    assert report["visible"] == ["0", "leaf"]
    assert report["wide"] == ["0", "leaf"]
    assert report["hit"] != "false"


def test_all_renderer_batches_colors_throttles_hits_composites_and_releases_gpu():
    renderer = RENDERER.read_text(encoding="utf-8")
    assert "in vec3 a_color" in renderer
    assert "state.nodeColors" in renderer and "nodeColor(index)" in renderer
    assert "state.edgeColors.length < edges.length * 6" in renderer
    assert "state.edgeColors.subarray(0, edges.length * 6)" in renderer
    assert "state.edgeColors[offset + 5]" in renderer
    assert "pendingHit" in renderer and "if (hitFrame) return" in renderer
    assert "context.drawImage(canvas, 0, 0)" in renderer
    assert "context.drawImage(labels, 0, 0)" in renderer
    assert "destroy: destroyGraph" in renderer
    assert "state.nodeGhosts = message.nodeGhosts || state.nodeGhosts" in renderer
    assert renderer.count("setVisibleNodes(drawableNodeIndices())") >= 3
    assert "const visible = state.visibleNodes, compact" in renderer
    assert "worker.onmessage = handleWorkerMessage" in renderer
    assert "const handleWorkerMessage = worker.onmessage" not in renderer
    assert "gl.deleteBuffer(buffer)" in renderer
    assert "gl.deleteProgram(value)" in renderer
    assert "WEBGL_lose_context" in renderer
    assert "handleWorkerFailure" in renderer
    assert "worker.addEventListener('error', handleWorkerFailure)" in renderer
    assert "worker.addEventListener('messageerror', handleWorkerFailure)" in renderer
    assert "worker.removeEventListener('error', handleWorkerFailure)" in renderer
    assert "error.code || 'GRAPH_WORKER'" in renderer
    assert "if (state.ready) camera(); else schedule();" in renderer
    # Canvas fallback must retain the same Highlight bridges semantics as WebGL.
    assert "state.bridges && state.edgeBridges[edge]" in renderer
    assert "rgba(244,211,127" in renderer


def test_all_worker_applies_scope_depth_layers_and_auto_collapse_without_reloading():
    source = json.dumps(WORKER.read_text(encoding="utf-8"))
    payload = json.dumps({
        "nodes": [
            {"id": "a", "community_id": "one"},
            {"id": "b", "community_id": "one"},
            {"id": "c", "community_id": "one"},
            {"id": "d", "community_id": "two"},
            {"id": "e", "community_id": "two"},
            {"id": "lonely"},
        ],
        "links": [
            {"source": "a", "target": "b", "layer": "semantic", "weight": 3},
            {"source": "b", "target": "c", "layer": "semantic", "weight": 2},
            {"source": "d", "target": "e", "layer": "temporal", "weight": 1},
        ],
    })
    script = f"""
const vm = require('vm'); const messages = [];
const context = {{ self: {{ postMessage: message => messages.push(message) }} }};
vm.runInNewContext({source}, context);
const send = data => context.self.onmessage({{ data }});
const latest = type => messages.filter(message => message.type === type).at(-1);
send({{ type: 'prepare', payload: {payload} }});
const ready = latest('ready');
const ids = values => Array.from(values).map(index => ready.ids[index]);
send({{ type: 'scope', scope: {{ minDegree: 2, showUnlinked: false, depth: 1 }} }});
send({{ type: 'camera', x: 0, y: 0, scale: 1.5, width: 100000, height: 100000 }});
const filtered = latest('visible');
send({{ type: 'scope', scope: {{ minDegree: 0, showUnlinked: true, depth: 1 }} }});
send({{ type: 'collapse', value: 'auto' }});
send({{ type: 'camera', x: 0, y: 0, scale: 0.2, width: 100000, height: 100000 }});
const collapsed = latest('visible');
send({{ type: 'focus', index: ready.ids.indexOf('a') }});
send({{ type: 'camera', x: 0, y: 0, scale: 1.5, width: 100000, height: 100000 }});
const depthOne = latest('visible');
send({{ type: 'scope', scope: {{ minDegree: 0, showUnlinked: true, depth: 2 }} }});
send({{ type: 'camera', x: 1, y: 0, scale: 1.5, width: 100000, height: 100000 }});
const depthTwo = latest('visible');
send({{ type: 'layers', layers: {{ semantic: false, temporal: true }} }});
send({{ type: 'camera', x: 2, y: 0, scale: 1.5, width: 100000, height: 100000 }});
const layered = latest('visible');
console.log(JSON.stringify({{
  filtered: ids(filtered.nodes), filteredEdges: filtered.drawnLinks,
  collapsed: ids(collapsed.nodes), isCollapsed: collapsed.collapsed,
  depthOne: ids(depthOne.nodes), depthTwo: ids(depthTwo.nodes),
  layeredEdges: layered.drawnLinks,
}}));
"""
    result = subprocess.run(
        ["node", "-"], cwd=ROOT, check=True, capture_output=True, text=True, input=script,
    )
    report = json.loads(result.stdout)
    assert report["filtered"] == ["b"]
    assert report["filteredEdges"] == 0
    assert report["isCollapsed"] is True
    assert set(report["collapsed"]) == {"b", "d", "lonely"}
    assert report["depthOne"] == ["a", "b"]
    assert report["depthTwo"] == ["a", "b", "c"]
    assert report["layeredEdges"] == 0


def test_all_worker_force_controls_and_reheat_change_the_settled_layout():
    source = json.dumps(WORKER.read_text(encoding="utf-8"))
    payload = json.dumps({
        "nodes": [{"id": f"n-{index}", "community_id": f"c-{index % 3}"}
                  for index in range(18)],
        "links": [{"source": f"n-{index}", "target": f"n-{index + 1}", "weight": 2}
                  for index in range(17)],
    })
    script = f"""
const vm = require('vm'); const messages = [];
const context = {{ self: {{ postMessage: message => messages.push(message) }} }};
vm.runInNewContext({source}, context);
const send = data => context.self.onmessage({{ data }});
const latest = type => messages.filter(message => message.type === type).at(-1);
const extent = positions => {{
  const xs = [], ys = [];
  for (let index = 0; index < positions.length; index += 2) {{ xs.push(positions[index]); ys.push(positions[index + 1]); }}
  return Math.max(...xs) - Math.min(...xs) + Math.max(...ys) - Math.min(...ys);
}};
send({{ type: 'prepare', payload: {payload} }});
send({{ type: 'settings', settings: {{ mode: 'communities', repel: 120, link: 80,
  gravity: 0, springStiffness: 1, damping: 1 }}, relayout: true }});
const loose = latest('layout');
send({{ type: 'settings', settings: {{ repel: 0, link: 4, gravity: 400,
  springStiffness: 1, damping: 1 }}, relayout: true }});
const tight = latest('layout');
send({{ type: 'reheat' }});
const reheated = latest('layout');
    console.log(JSON.stringify({{
      loose: extent(loose.positions), tight: extent(tight.positions),
      changed: Array.from(tight.positions).some((value, index) => value !== reheated.positions[index]),
    }}));
    """
    result = subprocess.run(
        ["node", "-"], cwd=ROOT, check=True, capture_output=True, text=True, input=script,
    )
    report = json.loads(result.stdout)
    assert report["loose"] > report["tight"] * 1.3
    assert report["changed"] is True


def test_all_renderer_has_bounded_directional_flow_and_worker_control_messages():
    renderer = RENDERER.read_text(encoding="utf-8")
    worker = WORKER.read_text(encoding="utf-8")
    styles = STYLES.read_text(encoding="utf-8")
    assert "FLOW_EDGE_LIMIT = 900" in renderer
    assert "FLOW_FRAME_MS = 34" in renderer
    assert "function drawRelationFlow(now)" in renderer
    assert "prefers-reduced-motion: reduce" in renderer
    assert "type: 'scope'" in renderer and "message.type === 'scope'" in worker
    assert "type: 'collapse'" in renderer and "message.type === 'collapse'" in worker
    assert "type: 'reheat'" in renderer and "message.type === 'reheat'" in worker
    assert "MAX_LINKS = 200000" in worker
    assert "state.lastVisibleMask" in worker
    # The host owns the style surface so every theme, including Paper, retains
    # a high-contrast graph well behind the transparent WebGL canvases.
    assert "element.setAttribute('data-graph-style', opts.style || 'cyber')" in renderer
    assert "element.setAttribute('data-graph-style', state.styleName)" in renderer
    assert "element.removeAttribute('data-graph-style')" in renderer
    assert 'body[data-theme="paper"] .graph-header' in styles


def test_ledger_routes_every_shared_sidebar_control_to_the_dedicated_all_renderer():
    ledger = LEDGER.read_text(encoding="utf-8")
    markup = MARKUP.read_text(encoding="utf-8")
    assert "if (loadAll) return ensureGraphAllAsset();" in ledger
    assert "const graphFactory = fullGraph ? window.EngraphisAllGraph" in ledger
    assert "galaxyQuality" not in ledger
    assert "graph.setCollapse(byId('graph-collapse').checked ? 'auto' : false)" in ledger
    assert "const includeCode = targetIncludeCode ? '&include_code=true' : '';" in ledger
    assert "minDegree: number(byId('graph-min-degree').value)" in ledger
    assert "showUnlinked: state.graphShowUnlinked" in ledger
    assert "scopeControl.disabled = full" not in ledger
    assert "animated flow and orbital simulation are unavailable" not in ledger
    for control in (
        "graph-preset", "graph-color", "graph-flow", "graph-flow-speed", "graph-repel",
        "graph-link", "graph-gravity", "graph-tune-min-degree", "graph-depth",
        "graph-collapse", "graph-ghosts", "graph-size",
    ):
        assert f'id="{control}"' in markup
    assert 'id="graph-lod-note"' in markup
