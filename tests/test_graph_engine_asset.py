"""Contract checks for the opt-in browser graph engine (``?graph-engine=next``).

These tests intentionally stay dependency-light: the dashboard's offline CI floor does
not need a browser or a JavaScript package manager just to validate a shipped static
asset.  Where Node is available the asset is *executed* rather than pattern-matched, so
the checks assert behaviour (escaping, bridge detection, stack safety, load-order
independence) instead of the presence of source substrings.

The properties guarded here are the ones whose failure is silent in a browser:

* the asset must define its global without touching ``ForceGraph``/``document``, so a
  blocked or missing vendor bundle degrades instead of white-screening the dashboard;
* every label crossing into force-graph must be escaped, because force-graph's tooltip
  is an ``innerHTML`` sink and entity labels come from ingested memories;
* the client-side graph analysis must not recurse per node or run unbounded work;
* the per-style pane backgrounds must stay in CSS, since the production CSP sets
  ``style-src-attr 'none'``.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "engraphis" / "static"
ASSET = STATIC / "engraphis-graph.js"
INDEX = STATIC / "index.html"
CSS = STATIC / "dashboard.css"
DASHBOARD = STATIC / "dashboard.js"
VENDOR = STATIC / "vendor" / "force-graph.min.js"

NODE = shutil.which("node")
requires_node = pytest.mark.skipif(NODE is None, reason="node is not installed")

#: Evaluates the asset with nothing but a bare ``window`` object in scope.  Any top-level
#: use of a browser or vendor global would raise here, which is the point.
PRELUDE = """
const fs = require('fs');
const source = fs.readFileSync(process.argv[1], 'utf8');
const window = {};
new Function('window', source)(window);
const G = window.EngraphisGraph;
const I = G._internals;
const emit = value => console.log(JSON.stringify(value));
"""


def _run_node(script: str) -> object:
    result = subprocess.run(
        [NODE, "-e", PRELUDE + script, str(ASSET)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


# ── load order and failure isolation ────────────────────────────────────────────────


def test_opt_in_graph_asset_is_loaded_after_its_dependencies() -> None:
    html = INDEX.read_text(encoding="utf-8")
    d3 = html.index("/static/vendor/d3.min.js")
    force_graph = html.index("/static/vendor/force-graph.min.js")
    engine = html.index("/static/engraphis-graph.js")
    dashboard = html.index("/static/dashboard.js")
    assert d3 < force_graph < engine < dashboard


@requires_node
def test_graph_asset_defines_its_global_without_touching_its_dependencies() -> None:
    """Nothing may run at parse time except pure setup.

    ``PRELUDE`` supplies no ``ForceGraph``, no ``document`` and no ``requestAnimationFrame``.
    If the asset reached for any of them at the top level this would throw, and in a browser
    the same reach would abort the script and take ``window.EngraphisGraph`` with it.
    """
    report = _run_node(
        """
        emit({
          create: typeof G.create,
          presets: Object.keys(G.PRESETS).sort(),
          styles: Object.keys(G.STYLE_LAYERS).sort(),
        });
        """
    )
    assert report["create"] == "function"
    assert "communities" in report["presets"]
    assert report["styles"] == ["classic", "cyber", "galaxy", "solar"]


@requires_node
def test_create_fails_loudly_when_force_graph_is_unavailable() -> None:
    """A blocked vendor bundle must raise, not half-initialise a dead canvas."""
    report = _run_node(
        """
        let message = null;
        try { G.create({ getAttribute() { return null; } }, {}); }
        catch (error) { message = error.message; }
        emit({ message });
        """
    )
    assert report["message"] == "force-graph not loaded"


def test_dashboard_falls_back_to_the_classic_renderer_when_the_engine_throws() -> None:
    source = DASHBOARD.read_text(encoding="utf-8")
    # The opt-in flag must be latched off after a failure, and the render path must catch.
    assert "GRAPH_ENGINE_FAILED" in source
    assert "if(GRAPH_ENGINE_FAILED)return false" in source
    assert "graphEngineFallback(error)" in source
    engine_path = source[source.index("function graphRenderEngine"):]
    engine_path = engine_path[: engine_path.index("\nfunction ")]
    assert "try{" in engine_path and "}catch(error){" in engine_path


# ── XSS: untrusted entity labels reaching force-graph ───────────────────────────────


def test_force_graph_tooltip_is_still_an_inner_html_sink() -> None:
    """Guards the *reason* the engine sets its own label accessors.

    force-graph defaults ``nodeLabel``/``linkLabel`` to the accessor ``"name"`` and renders a
    string label through ``innerHTML``.  Node names here are entity labels extracted from
    ingested memories, i.e. untrusted.  If a vendor bump ever changes this, revisit whether
    the explicit escaped accessors below are still the right shape.
    """
    vendor = VENDOR.read_text(encoding="utf-8", errors="ignore")
    assert 'nodeLabel:{default:"name"' in vendor
    assert 'linkLabel:{default:"name"' in vendor


def test_engine_never_relies_on_the_default_label_accessor() -> None:
    source = ASSET.read_text(encoding="utf-8")
    assert ".nodeLabel(node => esc(nodeName(node)))" in source
    assert ".linkLabel(" in source
    assert "eval(" not in source
    # The engine paints to canvas; the only markup sink it may use is clearing its own
    # container on teardown.  Anything else would be a route for an unescaped entity label.
    writes = re.findall(r"\w+\.(?:inner|outer)HTML\s*=\s*[^;]+", source)
    assert writes == ["el.innerHTML = ''"], writes
    assert not re.search(r"insertAdjacentHTML|document\.write|createContextualFragment", source)


@requires_node
@pytest.mark.parametrize(
    "payload",
    [
        "<img src=x onerror=alert(1)>",
        "<script>alert(1)</script>",
        "\" onmouseover=\"alert(1)",
        "<svg/onload=alert(1)>",
    ],
)
def test_entity_labels_are_escaped_before_they_can_reach_a_dom_sink(payload: str) -> None:
    report = _run_node(
        "emit({ escaped: I.esc(%s), named: I.nodeName({ label: %s }) });"
        % (json.dumps(payload), json.dumps(payload))
    )
    escaped = report["escaped"]
    assert "<" not in escaped and ">" not in escaped
    assert '"' not in escaped and "'" not in escaped
    assert "&lt;" in escaped or "&quot;" in escaped
    # nodeName is the raw value; escaping is the accessor's job, so this documents the split.
    assert report["named"] == payload


# ── payload compatibility with the shipped /graph endpoint ──────────────────────────


@requires_node
def test_engine_accepts_both_the_api_and_renderer_link_shapes() -> None:
    report = _run_node(
        """
        const api = { from: 'a', to: 'b' };
        const renderer = { source: { id: 'c' }, target: 'd' };
        emit({
          apiSource: I.linkEndpoint(api, 'source'),
          apiTarget: I.linkEndpoint(api, 'target'),
          rendererSource: I.linkEndpoint(renderer, 'source'),
          rendererTarget: I.linkEndpoint(renderer, 'target'),
          label: I.nodeName({ label: 'Ada' }),
          name: I.nodeName({ name: 'Grace' }),
          fallback: I.nodeName({ id: 'ent_1' }),
        });
        """
    )
    assert report["apiSource"] == "a" and report["apiTarget"] == "b"
    assert report["rendererSource"] == "c" and report["rendererTarget"] == "d"
    assert report["label"] == "Ada"
    assert report["name"] == "Grace"
    assert report["fallback"] == "ent_1"


@requires_node
def test_valid_time_accepts_seconds_milliseconds_and_iso_strings() -> None:
    report = _run_node(
        """
        emit({
          seconds: I.asOfValue(1700000000),
          millis: I.asOfValue(1700000000000),
          iso: I.asOfValue('2023-11-14T22:13:20Z'),
          blank: I.asOfValue(''),
          junk: I.asOfValue('not a date'),
        });
        """
    )
    assert report["seconds"] == report["millis"] == 1700000000000
    assert report["iso"] == 1700000000000
    assert report["blank"] is None and report["junk"] is None


# ── client-side analysis: correctness and cost ──────────────────────────────────────


@requires_node
def test_bridge_detection_matches_a_known_graph() -> None:
    """A triangle has no bridges; the tail hanging off it is all bridges."""
    report = _run_node(
        """
        const nodes = ['a', 'b', 'c', 'd', 'e'].map(id => ({ id }));
        const links = [['a','b'], ['b','c'], ['c','a'], ['c','d'], ['d','e']]
          .map(([source, target]) => ({ source, target }));
        const adj = I.communities(nodes, links);
        I.findBridges(nodes, links, adj);
        emit({
          bridges: links.filter(l => l.bridge).map(l => l.source + '-' + l.target),
          communities: new Set(nodes.map(n => n.community)).size,
        });
        """
    )
    assert report["bridges"] == ["c-d", "d-e"]
    assert report["communities"] == 1


@requires_node
def test_parallel_edges_are_not_reported_as_bridges() -> None:
    report = _run_node(
        """
        const nodes = [{ id: 'a' }, { id: 'b' }];
        const links = [{ source: 'a', target: 'b' }, { source: 'a', target: 'b' }];
        const adj = I.communities(nodes, links);
        I.findBridges(nodes, links, adj);
        emit({ bridges: links.filter(l => l.bridge).length });
        """
    )
    assert report["bridges"] == 0


@requires_node
def test_disconnected_entities_are_labelled_as_separate_communities() -> None:
    report = _run_node(
        """
        const nodes = ['a', 'b', 'c', 'd'].map(id => ({ id }));
        const links = [{ source: 'a', target: 'b' }, { source: 'c', target: 'd' }];
        const adj = I.communities(nodes, links);
        emit({ groups: new Set(nodes.map(n => n.community)).size });
        """
    )
    assert report["groups"] == 2


@requires_node
def test_graph_analysis_is_stack_safe_and_bounded_on_a_large_store() -> None:
    """A long chain of entities is the worst case for both analyses.

    A recursive Tarjan overflows the call stack here, and exact Brandes betweenness is
    O(V*E) — minutes of blocked main thread.  Both are guarded, so this must finish well
    inside the bound even on a slow machine.
    """
    report = _run_node(
        """
        const N = 40000;
        const nodes = [], links = [];
        for (let i = 0; i < N; i++) {
          nodes.push({ id: 'n' + i });
          if (i) links.push({ source: 'n' + (i - 1), target: 'n' + i });
        }
        const adj = I.communities(nodes, links);
        const started = Date.now();
        I.findBridges(nodes, links, adj);
        I.betweenness(nodes, adj);
        const scores = nodes.map(n => n.betweenness);
        emit({
          ms: Date.now() - started,
          allBridges: links.every(l => l.bridge),
          finite: scores.every(Number.isFinite),
          peak: Math.max.apply(null, scores.slice(0, 1000).concat(scores.slice(-1000))),
        });
        """
    )
    assert report["allBridges"] is True
    assert report["finite"] is True
    # Ends of a chain are never on a shortest path between others.
    assert report["peak"] < 0.5
    assert report["ms"] < 30000, f"graph analysis took {report['ms']}ms on 40k entities"


@requires_node
def test_max_helper_survives_arrays_past_the_spread_limit() -> None:
    """``Math.max(...array)`` throws RangeError long before a store is unrenderable."""
    report = _run_node("emit({ max: I.maxOf(new Array(400000).fill(7), 1) });")
    assert report["max"] == 7


@requires_node
def test_colour_helpers_handle_the_shorthand_hex_the_palettes_may_carry() -> None:
    report = _run_node(
        """
        emit({
          short: I.hexRgb('#abc'),
          long: I.hexRgb('#8c83e8'),
          empty: I.hexRgb(''),
          light: I.contrastOn('#ffffff'),
          dark: I.contrastOn('#000000'),
        });
        """
    )
    assert report["short"] == [170, 187, 204]
    assert report["long"] == [140, 131, 232]
    assert report["empty"] == [140, 131, 232]
    assert report["light"] == "#111827"
    assert report["dark"] == "#f8fafc"


# ── CSP, styling and lifecycle ──────────────────────────────────────────────────────


def test_pane_backgrounds_are_owned_by_css_not_by_the_asset() -> None:
    """``style-src-attr 'none'`` forbids writing these onto the element."""
    css = CSS.read_text(encoding="utf-8")
    source = ASSET.read_text(encoding="utf-8")
    for style in ("galaxy", "solar", "cyber"):
        assert f'#graph-net[data-graph-style="{style}"]' in css
    assert "data-graph-style" in source
    # The gradients must exist in exactly one place, or the two copies drift.
    assert "radial-gradient" not in source
    assert "linear-gradient" not in source


def test_hover_cursor_class_the_asset_toggles_exists_in_css() -> None:
    css = CSS.read_text(encoding="utf-8")
    source = ASSET.read_text(encoding="utf-8")
    assert "engraphis-graph-node-hover" in source
    assert ".engraphis-graph-node-hover" in css


def test_csp_gate_covers_the_graph_asset() -> None:
    from scripts.externalize_dashboard_assets import EXTRA_SCRIPTS, check

    assert ASSET in EXTRA_SCRIPTS, "the graph engine must be inside the CSP drift gate"
    check()


def test_engine_exposes_a_teardown_and_the_dashboard_drives_it() -> None:
    source = ASSET.read_text(encoding="utf-8")
    dashboard = DASHBOARD.read_text(encoding="utf-8")
    for member in ("api.destroy", "api.pause", "api.resume", "api.resize"):
        assert member in source
    # force-graph keeps a rAF alive while resumed; leaving the view must park it.
    assert "if(v==='graph')graphEngineResume();else graphEnginePause()" in dashboard
    assert "GRAPH_ENGINE.destroy()" in dashboard


def test_reduced_motion_is_honoured_by_the_opt_in_renderer() -> None:
    source = ASSET.read_text(encoding="utf-8")
    dashboard = DASHBOARD.read_text(encoding="utf-8")
    assert "prefers-reduced-motion: reduce" in source
    assert "opts.reducedMotion" in source
    assert "reducedMotion:prefersReducedMotion" in dashboard


def test_graph_engine_is_syntactically_valid_when_node_is_installed() -> None:
    if NODE is None:
        pytest.skip("node is not installed")
    result = subprocess.run(
        [NODE, "--check", str(ASSET)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
