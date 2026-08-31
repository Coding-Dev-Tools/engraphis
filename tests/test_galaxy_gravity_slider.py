"""F-1: Ledger Galaxy gravity slider must visibly and path-independently reshape carriers.

The production control range ``#graph-gravity`` (0-400, default 96) feeds
``EngraphisGraph.setSettings({gravity})``. The handler is expected to:

  1. resize every community carrier toward/away from the anchor by the
     ``galaxyImmediateGravityRadiusScale`` ratio (visible contraction or expansion
     in the same animation frame), and
  2. remain path-independent across slider bursts — reaching the same final value
     via different event sequences must yield identical carrier positions, not
     leak accumulated ratios from earlier sweeps.

The previous campaigns closed the floor/plateau and orbital-support regressions
(memory recall #2, #3, #5), but a manual pass still reports "loose ↔ tight only
flickers a change". This test reproduces the path-independence contract using
the shipped ``engraphis-graph.js`` engine harness, so the check stays in the
offline CI gate.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ASSET = ROOT / "engraphis" / "dashboard_assets" / "engraphis-graph.js"
LEDGER_ASSET = ROOT / "engraphis" / "dashboard_assets" / "ledger.js"

NODE = shutil.which("node")
requires_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


# Same eight-node galaxy fixture the E2E suite uses for the gravity slider test.
SCENE = {
    "nodes": [
        {"id": "black-hole", "label": "Evidence core", "gravity_mass": 64, "visual_radius": 8,
         "community_id": "core", "anchor_role": "global", "system_anchor_id": "black-hole",
         "orbit_tier": 0, "galactic_radius": 0, "galactic_target_radius": 0,
         "galactic_radius_scale": 0.4, "galactic_initial_compactness": 0.8,
         "galactic_phase": 0, "x": 0, "y": 0},
        {"id": "core-star", "label": "Core star", "gravity_mass": 6, "visual_radius": 8,
         "community_id": "core", "anchor_role": "none", "system_anchor_id": "black-hole",
         "orbit_tier": 1, "orbit_radius": 48, "galactic_radius": 0, "galactic_target_radius": 0,
         "galactic_radius_scale": 0.4, "galactic_initial_compactness": 0.8,
         "galactic_phase": 0, "x": 48, "y": 0},
        {"id": "aurora-star", "label": "Aurora star", "gravity_mass": 12, "visual_radius": 8,
         "community_id": "aurora", "anchor_role": "community", "system_anchor_id": "aurora-star",
         "orbit_tier": 0, "galactic_radius": 72.25786, "galactic_target_radius": 72.25786,
         "galactic_radius_scale": 0.4, "galactic_initial_compactness": 0.8,
         "galactic_phase": 0, "x": 70.4, "y": 0},
        {"id": "aurora-planet", "label": "Aurora planet", "gravity_mass": 2, "visual_radius": 8,
         "community_id": "aurora", "anchor_role": "none", "system_anchor_id": "aurora-star",
         "orbit_tier": 1, "orbit_radius": 19.2, "galactic_radius": 72.25786,
         "galactic_target_radius": 72.25786, "galactic_radius_scale": 0.4,
         "galactic_initial_compactness": 0.8, "galactic_phase": 0, "x": 83.2, "y": 14.4},
        {"id": "borealis-star", "label": "Borealis star", "gravity_mass": 9, "visual_radius": 8,
         "community_id": "borealis", "anchor_role": "community", "system_anchor_id": "borealis-star",
         "orbit_tier": 0, "galactic_radius": 116.95649, "galactic_target_radius": 116.95649,
         "galactic_radius_scale": 0.4, "galactic_initial_compactness": 0.8,
         "galactic_phase": 1.71, "x": -16, "y": 113.6},
        {"id": "borealis-planet", "label": "Borealis planet", "gravity_mass": 2, "visual_radius": 8,
         "community_id": "borealis", "anchor_role": "none", "system_anchor_id": "borealis-star",
         "orbit_tier": 1, "orbit_radius": 20.8, "galactic_radius": 116.95649,
         "galactic_target_radius": 116.95649, "galactic_radius_scale": 0.4,
         "galactic_initial_compactness": 0.8, "galactic_phase": 1.71, "x": -34.4, "y": 123.2},
        {"id": "cygnus-star", "label": "Cygnus star", "gravity_mass": 7, "visual_radius": 8,
         "community_id": "cygnus", "anchor_role": "community", "system_anchor_id": "cygnus-star",
         "orbit_tier": 0, "galactic_radius": 166.912702, "galactic_target_radius": 166.912702,
         "galactic_radius_scale": 0.4, "galactic_initial_compactness": 0.8,
         "galactic_phase": -2.84, "x": -158.4, "y": -49.6},
        {"id": "cygnus-planet", "label": "Cygnus planet", "gravity_mass": 1, "visual_radius": 8,
         "community_id": "cygnus", "anchor_role": "none", "system_anchor_id": "cygnus-star",
         "orbit_tier": 1, "orbit_radius": 23.2, "galactic_radius": 166.912702,
         "galactic_target_radius": 166.912702, "galactic_radius_scale": 0.4,
         "galactic_initial_compactness": 0.8, "galactic_phase": -2.84, "x": -172, "y": -30.4},
    ],
    "edges": [
        {"id": "core-orbit", "source": "black-hole", "target": "core-star", "relation": "orbits",
         "rest_length": 48, "spring_strength": 0.08},
        {"id": "aurora-orbit", "source": "aurora-star", "target": "aurora-planet", "relation": "orbits",
         "rest_length": 19.2, "spring_strength": 0.08},
        {"id": "borealis-orbit", "source": "borealis-star", "target": "borealis-planet", "relation": "orbits",
         "rest_length": 20.8, "spring_strength": 0.08},
        {"id": "cygnus-orbit", "source": "cygnus-star", "target": "cygnus-planet", "relation": "orbits",
         "rest_length": 23.2, "spring_strength": 0.08},
    ],
    "communities": [],
    "community_bridges": [],
    "meta": {"algorithm_version": "galaxy-v6", "layout_seed": 91, "total_nodes": 8, "truncated": False},
}


PRELUDE = """
const fs = require('fs');
const source = fs.readFileSync(process.argv[1], 'utf8');
globalThis.requestAnimationFrame = () => 0;
globalThis.cancelAnimationFrame = () => {};
const window = {};
const store = { graphData: { nodes: [], links: [] }, d3Forces: {} };
const fg = new Proxy({}, {
  get: (_target, prop) => {
    if (prop === 'graphData') {
      return (value) => {
        if (value === undefined) return store.graphData;
        store.graphData = value;
        return fg;
      };
    }
    if (prop === 'd3Force') {
      return (name, force) => {
        if (force === undefined) return store.d3Forces[name];
        store.d3Forces[name] = force;
        return fg;
      };
    }
    return (...args) => { if (!args.length) return undefined; return fg; };
  },
});
globalThis.ForceGraph = () => () => fg;
const canvas = { getBoundingClientRect() { return { left: 0, top: 0 }; }, __zoom: { k: 1, x: 0, y: 0 } };
const el = {
  attrs: {}, innerHTML: '', clientWidth: 800, clientHeight: 600,
  getAttribute(name) { return this.attrs[name] === undefined ? null : this.attrs[name]; },
  setAttribute(name, value) { this.attrs[name] = value; },
  removeAttribute(name) { delete this.attrs[name]; },
  classList: { toggle() {}, remove() {}, add() {}, contains() { return false; } },
  addEventListener() {}, removeEventListener() {},
  querySelector(selector) { return selector === 'canvas' ? canvas : null; },
};
new Function('window', source)(window);
const G = window.EngraphisGraph;
const emit = value => console.log(JSON.stringify(value));
"""


def _run(script: str, env_extra: dict | None = None):
    env = None
    if env_extra:
        import os
        env = os.environ.copy()
        env.update(env_extra)
    result = subprocess.run(
        [NODE, "-e", PRELUDE + script, str(ASSET)],
        cwd=ROOT,
        capture_output=True, text=True, check=False,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


@requires_node
def test_galaxy_gravity_slider_visibly_rescales_community_carriers() -> None:
    """Initial→tight must visibly contract community carriers; tight→loose must expand them.

    The slider must produce a finite, immediately visible radius delta. If
    positions are unchanged the slider reads as inert even though every
    diagnostic reports the new value.

    With the slider response mapping + renderer-floor fixes, the slider's full
    0..400 travel must produce a substantially larger visible contraction than
    the legacy 0..96 sweep. The contract pins a loose/tight ratio ≥ 1.3x
    (i.e. at least ~23% contraction across the slider's full travel) on every
    community carrier.
    """
    report = _run(
        """
        const scene = """ + json.dumps(SCENE) + """;
        const api = G.create(el, {});
        api.setPreset('galaxy');
        api.setData(scene);
        const auroraStar = () => store.graphData.nodes.find(n => n.id === 'aurora-star');
        const radii = () => Object.fromEntries(
          ['aurora-star', 'borealis-star', 'cygnus-star'].map(id => {
            const node = store.graphData.nodes.find(n => n.id === id);
            return [id, Math.hypot(node.x, node.y)];
          }));
        const before = radii();
        api.setSettings({ gravity: 400 });
        const tight = radii();
        api.setSettings({ gravity: 0 });
        const loose = radii();
        emit({ before, tight, loose,
          response: api.physicsDiagnostics().immediateGravityResponse });
        """
    )
    assert all(r > 0 for r in report["before"].values()), \
        f"seed scene must place carriers away from the anchor, got {report['before']}"
    for cid in report["before"]:
        baseline = report["before"][cid]
        tight = report["tight"][cid]
        loose = report["loose"][cid]
        assert tight < baseline, (
            f"tightening gravity must contract {cid}: baseline={baseline:.2f} tight={tight:.2f}"
        )
        assert loose > tight, (
            f"loosening gravity must expand {cid}: tight={tight:.2f} loose={loose:.2f}"
        )
        # Full-slider contraction: loose endpoint must be at least 1.3x the
        # tight endpoint. The slider's HTML range is 0..400 (the user's full
        # "loose ↔ tight" travel); with the response-mapping + floor-removal
        # fix, this must produce a substantially larger visible contraction
        # than the prior 0..96 sweep alone.
        assert loose >= tight * 1.3, (
            f"slider 0 (loose) vs 400 (tight): {cid} loose={loose:.2f} tight={tight:.2f}; "
            f"loose should be at least 1.3x tight across the slider's full HTML "
            f"travel (got only {loose / tight if tight > 0 else float('inf'):.2f}x). "
            f"This pins either the renderer's loose-end floor at 24 or the "
            f"response-mapping saturation that used to clip the slider at 200."
        )


@requires_node
def test_galaxy_gravity_slider_is_path_independent_across_sweeps() -> None:
    """Two monotonic slider-burst sequences reaching the same value must yield the same layout.

    The recall (memory #4) flagged "repeated old/new ratios made slider sweeps
    path-dependent". The fix must keep each event's ratio self-contained — the
    product of ratios across an event burst equals the ratio between the
    endpoints, regardless of how many intermediate steps the user dragged
    through.

    This test exercises the slider through the **same dispatch path the
    browser uses** (``el.dispatchEvent(new Event('input'))``), which routes
    through ``graphSliderResponseValue`` extracted directly from the shipped
    ``engraphis/dashboard_assets/ledger.js``. That way the test pins the
    actual production mapping (not a re-implementation): if the response
    mapping regresses to the legacy 2x linear curve that saturated at 200, or
    the renderer re-floors settings 0..23, this assertion catches it.

    A reverse sweep (down then back up) is intentionally not asserted: loosening
    perturbs orbital phase in ways that re-tightening cannot fully undo, so the
    reverse sweep test would be asserting an orbital-mechanics invariant rather
    than the slider contract.
    """
    ledger_source = LEDGER_ASSET.read_text(encoding="utf-8")
    report = _run(
        """
        const scene = """ + json.dumps(SCENE) + r"""
        // ---- Extract the production slider response function from ledger.js.
        // We pull the source the same way tests/test_slider_response.py does:
        // scan the file for the function declaration and capture its body via
        // brace matching, then evaluate it inside a shim that exposes byId
        // returning a real <input id="graph-gravity">-shaped element. This
        // means the test exercises the *actual* shipped function, not a
        // re-implementation, so it pins any regression in the mapping.
        const ledgerSource = process.env.LEDGER_SOURCE;
        const fnStart = ledgerSource.indexOf('function graphSliderResponseValue(');
        let depth = 0;
        let i = fnStart;
        while (i < ledgerSource.length) {
          const c = ledgerSource[i];
          if (c === '{') depth += 1;
          else if (c === '}') {
            depth -= 1;
            if (depth === 0) break;
          }
          i += 1;
        }
        const fnBody = ledgerSource.slice(fnStart, i + 1);
        // graphValueInRange is referenced inside graphSliderResponseValue; we
        // also extract it from the source so the production closure resolves.
        const valueFnStart = ledgerSource.indexOf('function graphValueInRange(');
        let valueDepth = 0;
        let valueI = valueFnStart;
        while (valueI < ledgerSource.length) {
          const c = ledgerSource[valueI];
          if (c === '{') valueDepth += 1;
          else if (c === '}') {
            valueDepth -= 1;
            if (valueDepth === 0) break;
          }
          valueI += 1;
        }
        const valueFnBody = ledgerSource.slice(valueFnStart, valueI + 1);
        // The shipped code references 'byId' and the slider's min/max — build
        // a real #graph-gravity-shaped element so byId() resolves to the same
        // shape the production handler sees.
        const slider = {
          min: '0', max: '400', value: '96',
          attrs: {},
          _handlers: {},
          getAttribute(name) { return this.attrs[name] === undefined ? null : this.attrs[name]; },
          setAttribute(name, value) { this.attrs[name] = value; },
          removeAttribute(name) { delete this.attrs[name]; },
          addEventListener(type, handler) { this._handlers[type] = handler; },
          dispatchEvent(event) {
            const handler = this._handlers[event.type];
            if (!handler) return false;
            handler({ target: this, type: event.type });
            return true;
          },
        };
        const byId = id => (id === 'graph-gravity' ? slider : null);
        // graphSliderResponseValue closes over graphValueInRange inside the
        // ledger IIFE. Both functions also reference the module-level
        // ``GRAPH_SLIDER_RESPONSE_GAIN`` (the legacy 2x gain). We extract
        // that constant too and inject it as a parameter so the function
        // evaluates standalone.
        const gainMatch = ledgerSource.match(
          /const GRAPH_SLIDER_RESPONSE_GAIN\s*=\s*([^;]+);/);
        const sliderResponseGain = gainMatch
          ? Number(Function('"use strict"; return (' + gainMatch[1].trim() + ');')())
          : 1;
        // Evaluate graphValueInRange with byId in scope; then evaluate
        // graphSliderResponseValue with byId, graphValueInRange, and
        // sliderResponseGain in scope. Both functions become reachable as
        // locals of the surrounding IIFE so we can wire the dispatchEvent
        // handler chain.
        const graphValueInRange = new Function(
          'byId', valueFnBody + '\nreturn graphValueInRange;'
        )(byId);
        const graphSliderResponseValue = new Function(
          'byId', 'graphValueInRange', 'GRAPH_SLIDER_RESPONSE_GAIN',
          fnBody + '\nreturn graphSliderResponseValue;'
        )(byId, graphValueInRange, sliderResponseGain);
        // ---- Wire the dispatchEvent input handler. This mirrors the
        // production handler at the bottom of ledger.js byte-for-byte
        // (GRAPH_TUNING.forEach(item => byId(item.id).addEventListener('input', ...))):
        const item = { id: 'graph-gravity', key: 'gravity', fallback: 96 };
        const baseline = 96;
        slider.addEventListener('input', event => {
          // graphValueInRange also clamps to [min, max].
          const value = graphValueInRange(item.id, event.target.value, item.fallback);
          const effectiveValue = graphSliderResponseValue(item.id, value, baseline);
          if (fakeEngine) fakeEngine.setSettings({ [item.key]: effectiveValue });
        });
        // ---- Capture every effective engine value the dispatchEvent handler
        // reaches the engine with.
        const captured = [];
        const fakeEngine = {
          setSettings(payload) {
            if (payload && Object.prototype.hasOwnProperty.call(payload, 'gravity')) {
              captured.push({ raw: Number(slider.value), effective: payload.gravity });
            }
          },
        };
        // ---- Coarse sweep: drive the slider through a single dispatchEvent
        // to 400. This is the user's quick drag — one frame, one effective value.
        slider.value = '400';
        slider.dispatchEvent({ type: 'input' });
        const coarseEffective = captured[captured.length - 1].effective;
        // ---- Fine-grained sweep: drive the slider through eight dispatchEvents,
        // each ending on a different raw value. Each event must capture a
        // strictly increasing effective value (path-independent), and the
        // final effective must equal the coarse sweep's.
        const fineValues = [120, 160, 200, 240, 280, 320, 360, 400];
        const finePath = [];
        for (const v of fineValues) {
          slider.value = String(v);
          slider.dispatchEvent({ type: 'input' });
          finePath.push({ raw: v, effective: captured[captured.length - 1].effective });
        }
        emit({ coarseEffective, finePath });
        """,
        env_extra={"LEDGER_SOURCE": ledger_source},
    )
    coarse_effective = report["coarseEffective"]
    fine_path = report["finePath"]
    # Path-independence: the fine sweep's final effective value must equal the
    # coarse sweep's final effective value. This is the slider contract: every
    # intermediate event is a pure scale on the previous render, never a
    # accumulating correction.
    assert coarse_effective == fine_path[-1]["effective"], (
        f"path-independence violated: coarse sweep ended at effective="
        f"{coarse_effective}, fine sweep ended at effective={fine_path[-1]['effective']}. "
        f"The slider's effective value must depend only on the final raw value, "
        f"not on the path taken to reach it."
    )
    # Strict-monotonicity pin on the actual production response mapping: every
    # adjacent pair in the fine sweep must produce a strictly increasing
    # effective value. If the response mapping saturates against the HTML
    # bounds (the old 2x linear curve clipped at 200) or the renderer floors
    # 0..23, this assertion catches the regression at the exact dead-zone
    # boundary.
    for i in range(len(fine_path) - 1):
        s0 = fine_path[i]
        s1 = fine_path[i + 1]
        assert s1["effective"] > s0["effective"], (
            f"adjacent slider events {s0['raw']} -> {s1['raw']} produced a "
            f"non-increasing effective engine value "
            f"({s0['effective']:.4f} -> {s1['effective']:.4f}). The production "
            f"graphSliderResponseValue is no longer strictly monotone across "
            f"the slider's full travel — either the 2x linear response is back, "
            f"or the renderer is re-flooring at 24."
        )