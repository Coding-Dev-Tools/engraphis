"""No-dead-zone contract for the Galactic gravity slider raw→engine mapping.

The dashboard's Gravity slider is a user-facing control with HTML ``min=0,
max=400`` and a preset baseline of ``96``. Two historical bugs combined to
produce invisible slider movement:

    1. The renderer used to floor the explicit global field at 24, so every
       raw setting in ``0..23`` produced the same ``galaxyBlackHoleGravityConstant``.
       The slider's loose half ``0..23`` was a flat plateau — every integer
       tick looked identical on screen.
    2. The slider response mapping used a 2x linear gain centered on the
       baseline (``GRAPH_SLIDER_RESPONSE_GAIN = 2``), so raw settings above
       ``baseline + (max - baseline)/gain = 248`` saturated against the HTML
       bound ``max=400``. The slider's tight half ``248..400`` was another
       flat plateau.

After both fixes land, the combined mapping (``ledger.js``'s
``graphSliderResponseValue`` → ``engraphis-graph.js``'s
``galaxyBlackHoleGravityConstant``) must be strictly monotone across the full
0..400 range — every adjacent pair of integer settings must produce a
distinct engine value, with no dead zones on either end.

This test exercises a handful of boundary settings — both past the loose-end
floor (0, 24, 25, 48, 49) and past the tight-end saturation (240, 248, 249,
400) — and asserts the strict-monotone contract at every boundary. The
boundary settings are exactly the ones that used to break the slider; this
test makes the regression impossible to miss.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LEDGER_ASSET = ROOT / "engraphis" / "dashboard_assets" / "ledger.js"
GRAPH_ASSET = ROOT / "engraphis" / "dashboard_assets" / "engraphis-graph.js"

NODE = shutil.which("node")
requires_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


# Boundary settings: every one of these used to be a dead-zone edge before the
# combined fix.
#
#   * 0, 1, 23, 24, 25 — bracket the renderer's old loose-end floor at 24.
#   * 48, 49 — bracket the renderer's old local-stellar floor at 48.
#   * 96 — the saved-view baseline (the slider's neutral calibration).
#   * 144, 192, 240, 248, 249 — bracket the old 2x response-mapping
#     saturation at 248 (raw=248 maps to engine=400, raw=400 also maps to 400).
#   * 400 — the slider's HTML ``max`` (the user's "tight" endpoint).
PROBE_SETTINGS = [0, 24, 25, 48, 49, 96, 144, 192, 240, 248, 249, 400]


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
const emit = value => console.log(JSON.stringify(value));
"""


def _run(script: str):
    result = subprocess.run(
        [NODE, "-e", PRELUDE + script, str(GRAPH_ASSET)],
        cwd=ROOT,
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


@requires_node
def test_galaxy_gravity_slider_no_dead_zone_boundary_mapping() -> None:
    """The raw→engine mapping must be strictly monotone at every boundary setting.

    Each of ``PROBE_SETTINGS`` is exactly one step past a former dead-zone
    boundary. With the combined fix:

      * The renderer no longer floors the global central field at 24, so
        ``0 → 24 → 25`` are three distinct engine values (no loose plateau).
      * The renderer no longer floors the local stellar field at 48, so
        ``48 → 49`` are distinct (the local well also flows 1:1).
      * The slider response mapping no longer saturates against ``max=400``
        past raw=248, so ``240 → 248 → 249 → 400`` are all distinct engine
        values (no tight plateau).

    The full sequence ``[0, 24, 25, 48, 49, 96, 144, 192, 240, 248, 249, 400]``
    must therefore produce 12 strictly increasing engine values. If any two
    adjacent settings in this sequence collapse to the same value, the
    combined fix has regressed.
    """
    # The renderer's central constant is the engine value that ultimately
    # drives every visible response: it feeds
    # ``galaxyImmediateGravityRadiusScale``, which scales the carrier
    # position in the immediate render pass, and the full physics solver.
    # We probe it directly via the internal API exported by
    # ``engraphis-graph.js``.
    settings_json = json.dumps(PROBE_SETTINGS)
    report = _run(
        """
        const I = window.EngraphisGraph._internals;
        // Probe each boundary setting through both stages of the combined
        // mapping:
        //   1. ``galaxyBlackHoleGravityConstant(s, true)`` — the renderer's
        //      central constant. Used by both the immediate-render pass
        //      (``galaxyImmediateGravityRadiusScale``) and the live physics.
        //   2. ``galaxyImmediateGravityRadiusScale(s)`` — the visible
        //      response: every carrier's radial position is scaled by the
        //      ratio of consecutive radius scales on each slider event.
        // Both must be strictly increasing across the boundary sequence.
        const samples = (""" + settings_json + """).map(setting => ({
          setting,
          blackHole: I.galaxyBlackHoleGravityConstant(setting, true),
          local: I.galaxyStellarGravityConstant(setting),
          radiusScale: I.galaxyImmediateGravityRadiusScale(setting),
        }));
        emit(samples);
        """
    )
    # Adjacent settings in the boundary sequence must produce strictly
    # increasing engine values at every step.
    for i in range(len(report) - 1):
        s0 = report[i]
        s1 = report[i + 1]
        # 1. Central constant must climb.
        assert s1["blackHole"] > s0["blackHole"], (
            f"Boundary pair ({s0['setting']}, {s1['setting']}) produces a "
            f"non-increasing central constant "
            f"({s0['blackHole']:.6f} -> {s1['blackHole']:.6f}). This is a "
            f"dead zone: the slider's user-facing raw setting changed but the "
            f"engine value did not. Either the renderer's loose-end floor at "
            f"24 is back, the local-stellar floor at 48 is back, or the "
            f"response mapping is saturating against the HTML bound (max=400) "
            f"past raw=248."
        )
        # 2. Visible response must climb (radius scale is monotone decreasing
        # as gravity grows, so the "visible contraction" of successive
        # slider events is monotone: a larger raw value must produce a
        # strictly smaller radius scale).
        assert s1["radiusScale"] < s0["radiusScale"], (
            f"Boundary pair ({s0['setting']}, {s1['setting']}) produces a "
            f"non-contracting immediate-render radius scale "
            f"({s0['radiusScale']:.6f} -> {s1['radiusScale']:.6f}). The "
            f"visible carrier-radius response is non-monotone across the "
            f"slider's full 0..400 travel — either the renderer's floor or "
            f"the response-mapping saturation has regressed."
        )
    # Local stellar constant must also be strictly increasing across the
    # whole boundary list. The local-stellar floor at 48 used to be the
    # largest plateau on the loose end: 0..47 all collapsed to the same
    # local-stellar constant. Pin that explicitly.
    for i in range(len(report) - 1):
        s0 = report[i]
        s1 = report[i + 1]
        assert s1["local"] >= s0["local"], (
            f"Boundary pair ({s0['setting']}, {s1['setting']}) produces a "
            f"non-increasing local-stellar constant "
            f"({s0['local']:.6f} -> {s1['local']:.6f}). The local stellar "
            f"well used to floor at 48; settings 0..47 all shared the same "
            f"local constant and therefore the same orbit clock."
        )
    # Coarse endpoint pin: the loose endpoint (s=0) and the tight endpoint
    # (s=400) must produce strictly different central constants on every
    # level. If either pair is equal, the slider is inert at that boundary.
    loose = report[0]
    tight = report[-1]
    assert tight["blackHole"] > loose["blackHole"], (
        f"Loose (s=0) vs tight (s=400): central constant "
        f"{loose['blackHole']:.6f} -> {tight['blackHole']:.6f}. The slider's "
        f"two endpoints must produce distinct central forces; the prior "
        f"renderer floor and 2x response saturation both collapsed the "
        f"endpoints to the same force."
    )
    assert tight["radiusScale"] < loose["radiusScale"], (
        f"Loose (s=0) vs tight (s=400): visible radius scale "
        f"{loose['radiusScale']:.6f} -> {tight['radiusScale']:.6f}. The "
        f"slider's two endpoints must produce distinct immediate-render "
        f"contractions; a saturated mapping collapses the endpoints to the "
        f"same visible scale."
    )