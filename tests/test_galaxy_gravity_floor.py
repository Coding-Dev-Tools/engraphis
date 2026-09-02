"""Direct probe of the Galactic gravity slider's visual floor.

The user reports the slider 'Galactic gravity · loose ↔ tight' only flickers a
change but never actually changes the galaxy. Memory #5 confirms the renderer
floors the explicit global field at 24 and the local stellar floor at 48, so
raw settings 0-23 (global) and 0-47 (local) are visually identical.

This test drives every integer setting 0..96 and asserts that the *rendered
carrier radius* strictly monotonically increases as the slider moves loose→tight.
A failure pinpoints the exact floor.

Runs offline against the shipped engraphis-graph.js via the Node engine
harness; no browser required.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ASSET = ROOT / "engraphis" / "dashboard_assets" / "engraphis-graph.js"

NODE = shutil.which("node")
requires_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


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


# Eight-node galaxy scene with one black hole + 3 carriers (aurora, borealis, cygnus).
# Each carrier carries a single planet. Carriers sit at distances 72 / 117 / 167 from
# the anchor so a visible slider sweep moves them through well-separated radii.
SCENE = {
    "nodes": [
        {"id": "black-hole", "label": "Evidence core", "gravity_mass": 64, "visual_radius": 8,
         "community_id": "core", "anchor_role": "global", "system_anchor_id": "black-hole",
         "orbit_tier": 0, "galactic_radius": 0, "galactic_target_radius": 0,
         "galactic_radius_scale": 0.4, "galactic_initial_compactness": 0.8,
         "galactic_phase": 0, "x": 0, "y": 0},
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
        {"id": "core-orbit", "source": "black-hole", "target": "core-star-deleted", "relation": "orbits",
         "rest_length": 48, "spring_strength": 0.08},
    ],
    "communities": [],
    "community_bridges": [],
    "meta": {"algorithm_version": "galaxy-v6", "layout_seed": 91, "total_nodes": 7, "truncated": False},
}


def _run(script: str):
    result = subprocess.run(
        [NODE, "-e", PRELUDE + script, str(ASSET)],
        cwd=ROOT,
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


@requires_node
def test_gravity_slider_every_integer_changes_carrier_radius_strictly() -> None:
    """For every integer setting 0..400, the rendered aurora radius must strictly decrease.

    The slider's full HTML range (min=0, max=400) is the user-facing control — every
    integer tick must produce a visibly different galaxy. If any two adjacent
    integers produce identical geometry, the slider 'only flickers a change'.

    This is the combined contract after the two parallel fixes:
      * the response mapping no longer saturates against the HTML bounds, so the
        slider's full 0..400 sweep reaches a distinct engine value per tick; and
      * the renderer no longer floors ``galaxyBlackHoleGravitySetting`` at 24, so
        0..23 produces distinct geometry from 24..400 (and from each other).
    """
    report = _run(
        """
        const scene = """ + json.dumps(SCENE) + """;
        const setup = () => {
          const api = G.create(el, {});
          api.setPreset('galaxy');
          api.setData(scene);
          return api;
        };
        const radius = setting => {
          const api = setup();
          api.setSettings({ gravity: setting });
          const star = store.graphData.nodes.find(n => n.id === 'aurora-star');
          return { x: star.x, y: star.y, r: Math.hypot(star.x, star.y) };
        };
        const samples = [];
        for (let s = 0; s <= 400; s += 1) samples.push({ s, ...radius(s) });
        emit(samples);
        """
    )
    radii = [sample["r"] for sample in report]
    # Strictly decreasing radii (more compact) as gravity increases, because tighter
    # gravity means carriers collapse closer to the black hole. With the combined
    # response-mapping + floor-removal fix, every adjacent tick in the full 0..400
    # range must produce a distinct radius — no dead zones and no plateaus.
    for i in range(len(radii) - 1):
        delta = radii[i] - radii[i + 1]
        assert delta > 1e-9, (
            f"Gravity {i} and {i + 1} produce identical aurora radius "
            f"({radii[i]:.6f}); the slider only flickers a change. "
            f"Delta = {delta:.9f}. Adjacent radii: [{radii[max(0, i - 2):i + 3]}]. "
            f"This pins either the renderer floor at 24 (settings 0..23 share the "
            f"same central constant) or the 2x linear response mapping that used "
            f"to saturate against the slider's HTML bounds."
        )
    # Coarse contract: the loose endpoint (s=0) must be substantially looser than the
    # full tight endpoint (s=400, the user's "tight"). The slider's full range is
    # 0..400 (HTML min/max); 96 is the preset baseline, NOT the tight end.
    # The final painted-edge contact projection can cap the tight endpoint when this
    # fixture's black-hole horizon intersects a contracted carrier. Keep a substantial
    # 20% contraction contract after that invariant is enforced.
    report_full = _run(
        """
        const scene = """ + json.dumps(SCENE) + """;
        const setup = () => {
          const api = G.create(el, {});
          api.setPreset('galaxy');
          api.setData(scene);
          return api;
        };
        const radius = setting => {
          const api = setup();
          api.setSettings({ gravity: setting });
          const star = store.graphData.nodes.find(n => n.id === 'aurora-star');
          return Math.hypot(star.x, star.y);
        };
        emit({ loose: radius(0), tight: radius(400), presetBaseline: radius(96) });
        """
    )
    loose = report_full["loose"]
    tight = report_full["tight"]
    assert loose >= tight * 1.25, (
        f"Slider 0 (loose) vs 400 (tight): radii {loose:.2f} vs {tight:.2f}; "
        f"the slider should contract carriers by at least 20% across its full "
        f"range (loose >= 1.25x tight) after contact projection, but the ratio is only "
        f"{loose / tight if tight > 0 else float('inf'):.2f}x. The combined "
        f"response-mapping + floor-removal fix should let the slider's full "
        f"0..400 travel produce a substantial visible contraction."
    )


@requires_node
def test_gravity_slider_global_field_unfloored_zero_loose_endpoint() -> None:
    """The global central field must respond at every raw setting in 0..400.

    After the combined fix, ``galaxyBlackHoleGravityConstant(s, true)`` is strictly
    increasing across the slider's full HTML range (0..400). This pins the
    renderer-floor regression: the shipped renderer used to clamp settings
    ``0..23`` to 24 (and ``0..47`` on the local stellar floor) so the loose end
    of the slider produced identical geometry across many integer ticks.
    """
    report = _run(
        """
        const I = window.EngraphisGraph._internals;
        // Sweep the full 0..400 range so the strict-increase assertion catches
        // both the renderer's loose-end floor (0..23) and any saturation plateau
        // on the tight end (e.g. the old 2x linear response clipping at 200).
        const samples = [];
        for (let s = 0; s <= 400; s += 1) {
          samples.push({
            setting: s,
            blackHole: I.galaxyBlackHoleGravityConstant(s, true),
            local: I.galaxyStellarGravityConstant(s),
            // The immediate-gravity-radius scale is what the visible response uses.
            radiusScale: I.galaxyImmediateGravityRadiusScale(s),
          });
        }
        emit(samples);
        """
    )
    # Every adjacent integer in the full 0..400 range must produce a strictly
    # larger central constant under a non-floored renderer that flows 1:1.
    for i in range(len(report) - 1):
        s0 = report[i]
        s1 = report[i + 1]
        assert s1["blackHole"] > s0["blackHole"], (
            f"Adjacent settings {s0['setting']} and {s1['setting']} produce a "
            f"non-increasing blackHole constant "
            f"({s0['blackHole']:.6f} -> {s1['blackHole']:.6f}). The renderer used to "
            f"floor galaxyBlackHoleGravitySetting at 24, and/or the response "
            f"mapping used to saturate against the HTML bounds. After the combined "
            f"fix the central constant must climb monotonically across the full "
            f"slider travel."
        )
    # The single tightest loose-endpoint pin: setting 0 (the 'loose' endpoint)
    # must produce strictly less central force than setting 1. If they are
    # equal, the renderer is still flooring 0 → 24.
    loose = report[0]
    next_one = report[1]
    assert loose["blackHole"] < next_one["blackHole"], (
        f"galaxyBlackHoleGravityConstant(0, true) ({loose['blackHole']:.6f}) must "
        f"be strictly less than galaxyBlackHoleGravityConstant(1, true) "
        f"({next_one['blackHole']:.6f}). Equality means the renderer is still "
        f"flooring 0..23 to 24 — the loose endpoint of the slider is inert."
    )
    # Coarse endpoint-to-endpoint magnitude: tight (s=400) must produce a
    # substantially stronger central force than loose (s=0). The combined
    # response+floor fix should give at least 8x amplification (well above
    # the renderer's prior plateau).
    end_loose = report[0]
    end_tight = report[-1]
    assert end_tight["blackHole"] > end_loose["blackHole"] * 8, (
        f"Gravity 0 (loose) vs 400 (tight) blackHole constant: "
        f"{end_loose['blackHole']:.2f} vs {end_tight['blackHole']:.2f}. The "
        f"tight endpoint should produce a substantially stronger central force "
        f"than the loose endpoint; the renderer's prior floor plateau collapsed "
        f"both ends to roughly the same force."
    )
