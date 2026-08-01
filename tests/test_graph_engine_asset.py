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
ASSET = ROOT / "engraphis" / "dashboard_assets" / "engraphis-graph.js"
LEGACY_ADAPTER = STATIC / "engraphis-graph.js"
INDEX = STATIC / "index.html"
CSS = STATIC / "dashboard.css"
DASHBOARD = STATIC / "dashboard.js"
CLASSIC_DASHBOARD = ROOT / "engraphis" / "classic_assets" / "dashboard.js"
VENDOR = STATIC / "vendor" / "force-graph.min.js"
PRIMARY_LEDGER = ROOT / "engraphis" / "dashboard_assets" / "ledger.js"
PRIMARY_INDEX = ROOT / "engraphis" / "dashboard_assets" / "index.html"

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


#: Same, plus a recording stand-in for force-graph so ``create()`` can be *driven*.  Every
#: accessor is a chainable setter that returns the stored value when called with no arguments —
#: force-graph's own kapsule semantics — so the paint configuration the engine installs can be
#: read back and invoked instead of pattern-matched.  ``calls`` counts the invalidations the
#: engine requests, which is the only observable form a "redraw now" takes.  ``invocations``
#: counts the *argument-less* calls, which under kapsule semantics are the commands rather than
#: the setters — ``d3ReheatSimulation()`` is one, and it has no other observable effect here.
ENGINE_PRELUDE = """
const fs = require('fs');
const source = fs.readFileSync(process.argv[1], 'utf8');
const window = {};
globalThis.requestAnimationFrame = () => {};
const store = {}, calls = {}, invocations = {};
const fg = new Proxy({}, {
  get: (_target, prop) => prop === 'd3Force' ? (function(name, force) {
    /* d3Force(name) is a getter and d3Force(name, force) is a setter. Modelling that
       distinction keeps the behavioural force tests below honest. */
    if (arguments.length === 1) return store.d3Forces && store.d3Forces[name];
    calls.d3Force = (calls.d3Force || 0) + 1;
    store.d3Forces = store.d3Forces || {};
    store.d3Forces[name] = force;
    return fg;
  }) : (...args) => {
    if (!args.length) { invocations[prop] = (invocations[prop] || 0) + 1; return store[prop]; }
    calls[prop] = (calls[prop] || 0) + 1;
    store[prop] = args.length === 1 ? args[0] : args;
    return fg;
  },
});
globalThis.ForceGraph = () => () => fg;
const el = {
  attrs: {}, innerHTML: '', clientWidth: 800, clientHeight: 600,
  getAttribute(name) { return this.attrs[name] === undefined ? null : this.attrs[name]; },
  setAttribute(name, value) { this.attrs[name] = value; },
  removeAttribute(name) { delete this.attrs[name]; },
  classList: { toggle() {}, remove() {} },
};
const chain = count => {
  const nodes = [], links = [];
  for (let i = 0; i <= count; i++) nodes.push({ id: 'n' + i });
  for (let i = 0; i < count; i++) {
    links.push({ source: 'n' + i, target: 'n' + (i + 1), layer: 'semantic' });
  }
  return { nodes, links };
};
new Function('window', source)(window);
const G = window.EngraphisGraph;
const I = G._internals;
const emit = value => console.log(JSON.stringify(value));
"""


def _run_node(script: str, prelude: str = PRELUDE) -> object:
    result = subprocess.run(
        [NODE, "-e", prelude + script, str(ASSET)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def _run_engine(script: str) -> object:
    return _run_node(script, prelude=ENGINE_PRELUDE)


# ── load order and failure isolation ────────────────────────────────────────────────


def test_graph_assets_are_never_loaded_on_a_plain_page_view() -> None:
    """Neither graph script may sit in index.html.

    force-graph applies inline styles at runtime, so under the production CSP
    (``style-src 'self'``) every page load that fetched it reported a violation per attempt —
    including the pages that never open the graph.
    """
    html = INDEX.read_text(encoding="utf-8")
    eager = re.findall(r'<script[^>]+src=["\'](/static/[^"\']+)["\']', html)
    assert "/static/vendor/d3.min.js" in eager
    assert "/static/dashboard.js?v=20260728-reference-materials" in eager
    assert "/static/vendor/force-graph.min.js" not in eager
    assert "/static/engraphis-graph.js" not in eager


def test_v1_graph_asset_is_only_a_compatibility_adapter() -> None:
    """New renderer code stays on the v2 dashboard surface, not the legacy server."""
    adapter = LEGACY_ADAPTER.read_text(encoding="utf-8")
    assert "canonicalAsset: '/v2-assets/engraphis-graph.js'" in adapter
    assert "window.EngraphisGraph =" not in adapter
    assert "window.EngraphisGraph =" in ASSET.read_text(encoding="utf-8")


def test_opt_in_graph_asset_is_lazily_loaded_after_its_dependencies() -> None:
    """The load order the removed script tags used to guarantee now lives in graphRender().

    ``graphRender`` returns early until ForceGraph is defined, so by the time the engine
    branch runs its dependency is already in scope.
    """
    source = DASHBOARD.read_text(encoding="utf-8")
    assert "script.src='/static/vendor/force-graph.min.js'" in source
    assert (
        "script.src='/v2-assets/engraphis-graph.js?v=20260730-drag-stability'"
        in source
    )
    render = source[source.index("function graphRender("):]
    render = render[: render.index("\nfunction ")]
    force_graph_gate = render.index("typeof ForceGraph==='undefined'")
    engine_gate = render.index("if(enginePending)")
    classic = render.index("graphRenderEngine(data,fit,reheat)")
    assert force_graph_gate < engine_gate < classic


def test_classic_dashboard_copies_share_the_canonical_route_gate() -> None:
    """Classic must use the canonical renderer, including mounted `/classic` routes."""
    sources = [path.read_text(encoding="utf-8") for path in (DASHBOARD, CLASSIC_DASHBOARD)]
    assert sources[0] == sources[1]
    start = sources[0].index("function graphEngineEnabled()")
    body = sources[0][start:sources[0].index("function graphEngineFallback", start)]
    assert "/(^|\\/)classic\\/?$/.test(window.location.pathname)" in body
    assert "GRAPH_ENGINE_FAILED" in body


def test_engine_node_labels_honor_the_configured_font_at_normal_zoom() -> None:
    source = ASSET.read_text(encoding="utf-8")
    assert "state.settings.font / scale / 3.4" not in source
    assert "state.settings.font / scale" in source


#: Executes dashboard.js's real graph-render *routing* decision against a stub DOM.
#: ``graphEngineEnabled``, ``graphEngineFallback``, ``loadForceGraph``, ``loadGraphEngine`` and
#: the routing half of ``graphRender`` are verbatim source slices — nothing is re-implemented.
#: Only the classic renderer body below the routing decision is swapped for a ``CLASSIC()``
#: marker, so the test can see which renderer a deep link actually reaches.
ROUTING_HARNESS = """
const fs = require('fs');
const src = fs.readFileSync(process.argv.slice(1).find(a => a.endsWith('dashboard.js')), 'utf8');
const scenario = process.argv[process.argv.length - 1];
const between = (from, to) => src.slice(src.indexOf(from), src.indexOf(to, src.indexOf(from)));
const flags = between('let GRAPH_ENGINE_FAILED=false;', 'function graphEngineEmptyMessage');
const loaders = between('let FORCE_GRAPH_LOADING=null;', 'function graphRender(');
const DECISION = 'if(graphEngineEnabled()&&graphRenderEngine(data,fit,reheat))return;';
const start = src.indexOf('function graphRender(');
const routing = src.slice(start, src.indexOf(DECISION, start) + DECISION.length) +
  '\\n CLASSIC();\\n}';

const log = { appended: [], warned: [], engine: 0, classic: 0 };
let pending = null;
const element = { clientWidth: 800, clientHeight: 600, classList: { toggle() {} },
                  setAttribute() {}, set textContent(v) {} };
globalThis.document = {
  getElementById: () => element,
  querySelectorAll: () => [],
  createElement: () => (pending = {}),
  head: { appendChild: s => log.appended.push(s.src) },
};
const location = scenario === 'classic'
  ? { search: '', pathname: '/classic' }
  : { search: '?graph-engine=next', pathname: '/' };
globalThis.window = { location, GSET: { mode: 'compact' },
                      console: globalThis.console };
globalThis.console = { warn: (...a) => log.warned.push(String(a[0])) };
globalThis.showAs = () => {};
globalThis.graphSetLayoutStatus = () => {};
globalThis.graphData = () => ({ nodes: [], links: [] });
/* Mirrors graphRenderEngine's real first line — `if(!element||typeof EngraphisGraph===
   'undefined')return false` — because that bail is exactly what a naive lazy-load would turn
   into a silent Classic fallback. Asserted against the real source below. */
globalThis.graphRenderEngine = () => {
  if (typeof EngraphisGraph === 'undefined') return false;
  log.engine += 1;
  return true;
};
globalThis.CLASSIC = () => { log.classic += 1; };
globalThis.GRAPH_PRESETS = { compact: {} };
globalThis.GRAPH_ENGINE = globalThis.GACTIVE_DATA = globalThis.GCOMPONENT_LAYOUT = null;
globalThis.GHILITE = globalThis.GHOVERSET = null;
/* The vendor bundle is already in scope: this exercises the engine gate, not the vendor gate. */
globalThis.ForceGraph = function () {};

new Function(flags + loaders + routing + '\\nreturn {graphRender};')().graphRender();
const settled = { engine: log.engine, classic: log.classic };
if (scenario === 'loads' || scenario === 'classic') {
  globalThis.EngraphisGraph = { create() {} }; pending.onload();
}
else { pending.onerror(); }
setTimeout(() => process.stdout.write(JSON.stringify({
  beforeSettle: settled, engine: log.engine, classic: log.classic,
  appended: log.appended, warned: log.warned,
})), 0);
"""


def _run_routing(scenario: str) -> dict:
    result = subprocess.run(
        [NODE, "-e", ROUTING_HARNESS, str(DASHBOARD), scenario],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


@requires_node
def test_graph_engine_deep_link_reaches_the_next_engine_after_a_lazy_load() -> None:
    """``?graph-engine=next`` must not degrade just because its asset is not loaded yet.

    ``graphRenderEngine`` bails when ``EngraphisGraph`` is undefined, and that bail cannot tell
    "not fetched yet" from "unavailable".  Deferring the script would turn every deep link into
    that bail — the user asks for the new engine and silently gets Classic.  So graphRender
    fetches the asset and waits, then renders.
    """
    # Keep the harness's stub honest: it only proves anything while the real function really
    # does bail on an undefined global.
    source = DASHBOARD.read_text(encoding="utf-8")
    engine_path = source[source.index("function graphRenderEngine"):]
    assert "typeof EngraphisGraph==='undefined')return false" in engine_path[:400]

    report = _run_routing("loads")

    assert report["appended"] == [
        "/v2-assets/engraphis-graph.js?v=20260730-drag-stability"
    ]
    # It waits rather than rendering something wrong in the meantime.
    assert report["beforeSettle"] == {"engine": 0, "classic": 0}
    # And it lands on the next engine, never touching the classic renderer.
    assert report["engine"] == 1
    assert report["classic"] == 0
    assert report["warned"] == []


@requires_node
def test_classic_route_reaches_the_canonical_engine_without_a_query_flag() -> None:
    report = _run_routing("classic")

    assert report["appended"] == [
        "/v2-assets/engraphis-graph.js?v=20260730-drag-stability"
    ]
    assert report["beforeSettle"] == {"engine": 0, "classic": 0}
    assert report["engine"] == 1
    assert report["classic"] == 0
    assert report["warned"] == []


@requires_node
def test_graph_engine_deep_link_degrades_loudly_when_the_asset_cannot_load() -> None:
    """A genuine load failure is the only thing that reaches Classic, and it says so."""
    report = _run_routing("fails")

    assert report["engine"] == 0
    assert report["classic"] == 1
    assert report["warned"] == [
        "graph-engine=next failed; falling back to the classic renderer"
    ]


def test_lazy_graph_engine_load_cannot_raise_an_unhandled_rejection() -> None:
    """An unhandled rejection prints a console error — the exact thing this fix removes.

    ``graphRender`` can start the engine fetch on a pass that returns at the ForceGraph gate,
    before it attaches its own handler, so the memoized promise carries its own.
    """
    source = DASHBOARD.read_text(encoding="utf-8")
    loader = source[source.index("function loadGraphEngine()"):]
    loader = loader[: loader.index("\nfunction ")]
    assert "GRAPH_ENGINE_LOADING.catch(()=>{})" in loader
    # A 200 that never registers the global is a corrupt asset, not a success.
    assert "reject(new Error('Graph engine asset loaded without registering EngraphisGraph'))" in loader


def test_force_graph_loader_rejects_a_success_without_the_vendor_global() -> None:
    """A truncated 200 must not enter the render loop without ``ForceGraph``."""
    source = DASHBOARD.read_text(encoding="utf-8")
    loader = source[source.index("function loadForceGraph()"):]
    loader = loader[: loader.index("\nlet GRAPH_ENGINE_LOADING")]
    assert "typeof ForceGraph==='undefined'" in loader
    assert "reject(new Error('Force graph asset loaded without registering ForceGraph'))" in loader


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


@requires_node
def test_node_geometry_stays_compact_for_small_overviews_and_is_style_neutral() -> None:
    """Material style changes must not turn a compact overview into oversized discs.

    A seven-node workspace is intentionally common in the Ledger overview.  Its normalized
    degree metric used to produce a dense-graph radius, and ``zoomToFit`` magnified that radius
    until every node filled a large part of the canvas.  The radius helper now shares the
    bounded scale used by Classic and does not know about visual style.
    """
    report = _run_node(
        """
        emit({
          leaf: I.graphNodeRadius({ degree: 0 }, 3, 0),
          hub: I.graphNodeRadius({ degree: 6 }, 3, 1),
          cluster: I.graphNodeRadius({ cluster: true, members: 64 }, 3, 1),
          styles: ['classic', 'cyber', 'galaxy', 'solar'].map(() => I.graphNodeRadius({ degree: 6 }, 3, 1)),
        });
        """
    )
    assert report["leaf"] >= 0.8
    assert report["hub"] < 4
    assert report["cluster"] < 7
    assert len(set(report["styles"])) == 1
    assert "if (sun) r *= 1.7" not in ASSET.read_text(encoding="utf-8")
    assert "if(sun)r*=1.7;" not in CLASSIC_DASHBOARD.read_text(encoding="utf-8")
    assert "if(sun)r*=1.7;" not in DASHBOARD.read_text(encoding="utf-8")


@requires_node
def test_auto_fit_cap_does_not_limit_manual_graph_inspection() -> None:
    """The auto-fit guard must not become a global force-graph zoom limit."""
    report = _run_engine(
        """
        G.create(el, {});
        emit({ maxZoom: store.maxZoom === undefined ? null : store.maxZoom });
        """
    )
    assert report["maxZoom"] is None
    source = ASSET.read_text(encoding="utf-8")
    assert "function autoFit(" in source
    assert "api.fit = () => { if (!destroyed) fg.zoomToFit" in source


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
def test_explorer_exports_its_visible_data_and_reports_bridge_metrics() -> None:
    """Filtering and analysis controls must affect the user-facing export/readout,
    rather than only changing paint on an otherwise stale payload."""
    report = _run_engine(
        """
        const reports = [];
        const api = G.create(el, { reducedMotion: () => true, onMetrics: value => reports.push(value) });
        api.setData({
          nodes: [
            { id: 'a', repo: 'engraphis' }, { id: 'b', repo: 'engraphis' },
            { id: 'c', repo: 'elsewhere' },
          ],
          links: [
            { source: 'a', target: 'b', valid_from: 100, valid_to: 200 },
            { source: 'b', target: 'c', valid_from: 100 },
          ],
        });
        api.setBridges(true);
        api.setRepoFilter('engraphis');
        const filtered = api.exportData();
        api.focus('a');
        api.clearFocus();
        api.setRepoFilter('');
        api.setAsOf(250);
        api.setGhosts(false);
        const withoutGhosts = api.exportData();
        api.setGhosts(true);
        const withGhosts = api.exportData();
        emit({
          bridges: reports[reports.length - 1].bridges,
          filtered, state: api.state(), withoutGhosts, withGhosts,
        });
        """
    )
    assert report["bridges"] == 2
    assert [node["id"] for node in report["filtered"]["nodes"]] == ["a", "b"]
    assert [(link["source"], link["target"]) for link in report["filtered"]["links"]] == [
        ("a", "b")
    ]
    assert report["state"]["focusId"] is None and report["state"]["highlight"] is None
    assert len(report["withoutGhosts"]["links"]) == 1
    assert len(report["withGhosts"]["links"]) == 2


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
def test_influence_relations_do_not_merge_two_topics_into_one_community() -> None:
    """Community Islands must not fuse two topics over a single cross-topic relation.

    ``influences`` edges routinely span otherwise separate bodies of work.  The classic
    renderer keeps them drawn and traversable but builds its clustering adjacency without
    them (``GCOMM_ADJ``); adding every link to one adjacency gives both topics the same
    colour and the same force centre.
    """
    report = _run_node(
        """
        const nodes = ['a', 'b', 'c', 'd'].map(id => ({ id }));
        const links = [
          { source: 'a', target: 'b', label: 'mentions' },
          { source: 'c', target: 'd', label: 'mentions' },
          { source: 'b', target: 'c', label: 'influences' },
        ];
        const adj = I.communities(nodes, links);
        I.findBridges(nodes, links, adj);
        emit({
          groups: new Set(nodes.map(n => n.community)).size,
          merged: nodes[1].community === nodes[2].community,
          neighbours: (adj.b || []).slice().sort(),
          bridges: links.filter(l => l.bridge).length,
        });
        """
    )
    assert report["groups"] == 2
    assert report["merged"] is False
    # The relation itself stays in the traversal adjacency: hover neighbourhood, focus depth
    # and bridge detection all still see it.  Only the clustering ignores it.
    assert report["neighbours"] == ["a", "c"]
    assert report["bridges"] == 3


@requires_node
def test_community_ids_are_ranked_by_size_so_the_legend_describes_the_right_nodes() -> None:
    """Legend labels and canvas swatches must agree about which cluster is "Cluster 1".

    ``graphRenderLegend()`` sorts communities by size and calls the largest "Cluster 1", but
    node colour indexes the palette by the community *id* (``commPal()[community % n]``).
    Assigning ids in raw payload order therefore made the legend describe one component with
    another's colour whenever a smaller component appeared first — which the payload order
    alone decides.  The classic ``graphComputeCommunities()`` sorts before assigning; so must
    this.
    """
    report = _run_node(
        """
        // Payload order is deliberately worst-case: the singleton comes first, the largest
        // component last, so raw iteration order and size order disagree completely.
        const nodes = ['solo', 'm1', 'm2', 'a', 'b', 'c'].map(id => ({ id }));
        const links = [
          { source: 'm1', target: 'm2' },
          { source: 'a', target: 'b' },
          { source: 'b', target: 'c' },
        ];
        I.communities(nodes, links);
        const byId = {};
        nodes.forEach(n => { byId[n.id] = n.community; });
        emit({ byId, distinct: new Set(nodes.map(n => n.community)).size });
        """
    )
    assert report["distinct"] == 3
    # Largest component (3 nodes) owns palette slot 0, i.e. the legend's "Cluster 1".
    assert report["byId"]["a"] == 0
    assert report["byId"]["b"] == 0
    assert report["byId"]["c"] == 0
    # Then the 2-node component, then the singleton — strictly by size, not by payload order.
    assert report["byId"]["m1"] == 1
    assert report["byId"]["m2"] == 1
    assert report["byId"]["solo"] == 2


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


# ── render configuration: what the engine actually installs on force-graph ──────────


@requires_node
def test_flow_particles_are_capped_on_a_large_relation_set() -> None:
    """Three animated particles per relation does not survive a real ``/graph`` response.

    force-graph advances every particle on every frame, so a few thousand relations is tens
    of thousands of animated objects and an unusable canvas.  The classic renderer refuses to
    draw them past 800 links; the opt-in engine must use the same cutoff rather than trusting
    that no store is big.
    """
    report = _run_engine(
        """
        const api = G.create(el, {});
        const particlesFor = link => store.linkDirectionalParticles(link || { layer: 'semantic' });
        api.setStyle('cyber');
        api.setSettings({ flow: true });
        api.setData(chain(40));
        const small = particlesFor();
        api.setData(chain(800));
        const atLimit = particlesFor();
        api.setData(chain(801));
        const overLimit = particlesFor();
        api.setData(chain(4000));
        emit({ small, atLimit, overLimit, realistic: particlesFor() * 4000,
               particleWidth: store.linkDirectionalParticleWidth,
               particleArrow: typeof store.linkDirectionalParticleCanvasObject === 'function' });
        """
    )
    assert report["small"] == 3
    assert report["atLimit"] == 3
    assert report["overLimit"] == 0
    # The number this guards: 4k relations x 3 particles was 12,000 animated objects a frame.
    assert report["realistic"] == 0
    assert report["particleWidth"] == 1
    assert report["particleArrow"] is True


def test_legacy_flow_particles_use_small_directional_arrows() -> None:
    """Classic and its static compatibility copy must not regress to round flow dots."""
    for path in (DASHBOARD, CLASSIC_DASHBOARD):
        source = path.read_text(encoding="utf-8")
        assert "linkDirectionalArrowLength(GPERF.dense?0:.625)" in source
        assert (
            "linkDirectionalParticleWidth(.85).linkDirectionalParticleCanvasObject"
            "(graphPaintFlowArrow)" in source
        )


#: A canvas 2D stand-in that counts the fills the galaxy starfield performs.  The engine wraps
#: ``onRenderFramePre`` in a try/catch, so a stub too thin to survive the real paint would read
#: as "no stars drawn"; the small-graph leg of the test below is what proves it is thick enough.
CANVAS_STUB = """
let fills = 0;
const ctx = {
  globalAlpha: 1, globalCompositeOperation: '', fillStyle: '', strokeStyle: '', lineWidth: 1,
  save() {}, restore() {}, beginPath() {}, arc() {}, ellipse() {}, stroke() {},
  fill() { fills += 1; },
  createRadialGradient() { return { addColorStop() {} }; },
};
"""


@requires_node
def test_galaxy_stops_animating_once_the_graph_is_large() -> None:
    """A settled graph must fall off the CPU, and galaxy was the one style that never did.

    The starfield lives in ``onRenderFramePre``, which force-graph's change detection cannot
    see, so the engine holds ``autoPauseRedraw(false)`` for it — repainting every node and link
    every frame, forever, even after particles and the simulation have stopped.  The classic
    path simply drops the starfield past ``GPERF.large`` (``if(GPERF.large)return``); with the
    stars gone there is nothing left that needs a frame the vendor would not schedule itself.
    """
    report = _run_engine(
        CANVAS_STUB
        + """
        const api = G.create(el, {});
        api.setStyle('galaxy');

        api.setData(chain(40));
        const smallAutoPause = store.autoPauseRedraw;
        fills = 0; store.onRenderFramePre(ctx, 1);
        const smallStars = fills;

        // 3001 entities / 3000 relations — past the classic renderer's 600-node signal.
        api.setData(chain(3000));
        const bigAutoPause = store.autoPauseRedraw;
        fills = 0; store.onRenderFramePre(ctx, 1);
        const bigStars = fills;

        // Style is what costs the frames, not size alone: cyber never asked for them.
        api.setStyle('cyber');
        api.setData(chain(40));
        emit({ smallAutoPause, bigAutoPause, smallStars, bigStars,
               cyberAutoPause: store.autoPauseRedraw });
        """
    )
    # Small galaxy graph: the animation is affordable, so the engine keeps driving frames.
    assert report["smallAutoPause"] is False
    assert report["smallStars"] > 0, "canvas stub never reached the starfield"
    # Large galaxy graph: no starfield, and the redraw loop is handed back to force-graph.
    assert report["bigStars"] == 0
    assert report["bigAutoPause"] is True, "a large galaxy graph repaints every frame forever"
    assert report["cyberAutoPause"] is True


@requires_node
def test_type_colours_follow_the_active_theme_not_a_hard_coded_dark_palette() -> None:
    """``applyTheme()`` recolours the canvas, but the engine had no theme to recolour to.

    The legend and controls read the ``--entity-*`` custom properties, so switching to Light,
    Midnight, Solarized or Sepia moved them while the canvas kept the dark-theme constants —
    an inconsistent palette and, on the light themes, poor contrast.  The engine cannot read
    CSS variables from a canvas, so the dashboard supplies the resolved values.
    """
    report = _run_engine(
        """
        const api = G.create(el, {});
        // setData first: the force-graph stand-in only starts answering graphData() once the
        // engine has pushed data into it, where the real vendor seeds an empty graph.
        // Linked, because the default scope hides degree-zero entities.
        api.setData({
          nodes: [{ id: 'a', etype: 'person_or_concept' }, { id: 'b', etype: 'person_or_concept' }],
          links: [{ source: 'a', target: 'b', layer: 'entity' }],
        });
        api.setColorBy('type');
        api.setStyle('classic');
        // `store` holds the values handed to force-graph, so this is the node object the
        // engine actually painted from — recoloured in place by refreshColors()/render().
        const colour = () => store.graphData.nodes[0].color;

        const fallback = colour();
        api.setThemeColors({ person_or_concept: '#112233' });
        const themed = colour();

        // A style palette still outranks the theme, exactly as classic graphTypeColor() does.
        api.setStyle('cyber');
        const styled = colour();

        // ...and an explicit user override still outranks both.
        api.setStyle('classic');
        api.setTypeColor('person_or_concept', '#abcdef');
        const overridden = colour();

        // A theme with no entry for the type must not strand the previous theme's colour.
        api.setThemeColors({});
        emit({ fallback, themed, styled, overridden, cleared: colour() });
        """
    )
    assert report["fallback"] == "#8c83e8"
    assert report["themed"] == "#112233", "the engine ignores the active theme"
    assert report["styled"] == "#ff3ea5"
    assert report["overridden"] == "#abcdef"
    # The override survives; only the theme tier was replaced.
    assert report["cleared"] == "#abcdef"


@requires_node
def test_hovering_a_node_asks_for_a_redraw() -> None:
    """A highlight nobody repaints is invisible.

    ``onNodeHover`` mutates closure state the paint callbacks read.  With reduced motion on,
    flow disabled, or a settled simulation, force-graph's ``autoPauseRedraw`` loop has nothing
    left to animate and will not repaint just because the callback fired.
    """
    report = _run_engine(
        """
        const api = G.create(el, { reducedMotion: () => true });
        api.setData({ nodes: [{ id: 'a' }, { id: 'b' }], links: [{ source: 'a', target: 'b' }] });
        const settled = calls.nodeCanvasObject;
        store.onNodeHover({ id: 'a' });
        const hovered = calls.nodeCanvasObject;
        store.onNodeHover(null);
        emit({
          settled, hovered, cleared: calls.nodeCanvasObject,
          particles: store.linkDirectionalParticles({ layer: 'semantic' }),
        });
        """
    )
    # Reduced motion: nothing is in flight, so an unrequested redraw would never arrive.
    assert report["particles"] == 0
    assert report["hovered"] > report["settled"]
    assert report["cleared"] > report["hovered"]


@requires_node
def test_unlinked_entities_are_shown_only_when_the_engine_is_told_to() -> None:
    """The lever the dashboard has to pull for its "Show unlinked nodes" checkbox."""
    report = _run_engine(
        """
        const seen = [];
        const api = G.create(el, { onStats: stats => seen.push(stats.nodes) });
        api.setData({
          nodes: [{ id: 'a' }, { id: 'b' }, { id: 'lonely' }],
          links: [{ source: 'a', target: 'b' }],
        });
        const hidden = seen[seen.length - 1];
        api.setScope({ showUnlinked: true });
        emit({ hidden, shown: seen[seen.length - 1] });
        """
    )
    assert report["hidden"] == 2
    assert report["shown"] == 3


#: Executes the *real* ``graphRenderEngine`` source against stubs.  Only its collaborators are
#: faked; the function itself is a verbatim slice, so what it forwards to the engine — and when
#: it parks a freshly created renderer — is observed rather than asserted about the source text.
RENDER_HARNESS = """
const fs = require('fs');
const src = fs.readFileSync(process.argv.slice(1).find(a => a.endsWith('dashboard.js')), 'utf8');
const scenario = JSON.parse(process.argv[process.argv.length - 1]);
const start = src.indexOf('function graphRenderEngine(');
const slice = src.slice(start, src.indexOf('/* Nav away from the graph view', start));

/* The theme-colour lookup is sliced verbatim too, not stubbed: the property under test is
   that the dashboard resolves the *active* CSS custom properties and hands them over, so
   faking the resolver would assert nothing. Only `getComputedStyle` below is synthetic. */
const between = (from, to) => src.slice(src.indexOf(from), src.indexOf(to, src.indexOf(from)));
const themeSrc = between('const ETYPE_TOKEN=', 'const GRAPH_PALETTES=')
  + between('function cssvar(', 'function graphValidColor(')
  + between('function graphThemeTypeColors(', 'function graphContrastColor(');

/* A stand-in for a non-dark theme: every --entity-* token differs from the engine's
   hard-coded THEME_ETYPE constants, so a renderer that ignored these would be visible. */
const THEME_VARS = {
  '--entity-concept': '#112233', '--entity-mention': '#223344', '--entity-hashtag': '#334455',
  '--entity-email': '#445566', '--entity-organization': '#556677', '--entity-location': '#667788',
  '--color-accent': '#778899', '--color-panel': '#9a7654', '--color-canvas': '#345678',
  '--color-text-dim': '#123456',
};
globalThis.getComputedStyle = () => ({ getPropertyValue: name => THEME_VARS[name] || '' });

const log = { created: 0, paused: 0, seeded: 0, scope: null, themeColors: null, error: null };
const checkbox = { checked: scenario.showUnlinked };
const element = { classList: { toggle() {} }, setAttribute() {}, set textContent(value) {} };
globalThis.document = {
  getElementById: id => (id === 'graph-show-iso' ? checkbox : element),
  querySelectorAll: () => [],
  body: {},
};
const engine = {
  setSettings() {}, setStyle() {}, setColorBy() {}, setPalette() {}, setTypeColors() {},
  setLayers() {}, setScope(patch) { log.scope = patch; },
  setThemeColors(map) { log.themeColors = map; },
  setData(data) { log.seeded = data.nodes.length; },
};
const api = {
  apply(fn) { fn(engine); }, communityMap: () => ({}),
  freeze() {}, destroy() {}, resume() {}, pause() { log.paused += 1; },
};
globalThis.EngraphisGraph = { create() { log.created += 1; return api; } };
globalThis.window = { GSET: { mode: 'compact', frozen: false } };
globalThis.GRAPH = { nodes: [] };
globalThis.GRAPH_ENGINE = null;
globalThis.GACTIVE_DATA = null;
globalThis.GCOLOR_OVERRIDES = {};
/* The state the nav-away pause recorded while GRAPH_ENGINE was still null. */
globalThis.GRAPH_ENGINE_PARKED = scenario.parked;
globalThis.showAs = () => {};
globalThis.prefersReducedMotion = () => false;
for (const name of ['graphSetLayoutStatus', 'graphSyncReadouts', 'graphUpdateEditedBadge',
                    'graphUpdateHud', 'graphRenderLegend', 'graphSetHighlight',
                    'graphSetSimulationStatus', 'syncGraphExplorerSelection', 'graphNodeClick',
                    'graphEngineEmptyMessage']) globalThis[name] = () => {};
globalThis.graphEngineFallback = error => {
  log.error = String((error && error.message) || error);
};

const graphRenderEngine = new Function(themeSrc + slice + '\\nreturn graphRenderEngine;')();
const rendered = graphRenderEngine({
  nodes: [{ id: 'a' }, { id: 'b' }, { id: 'lonely' }],
  links: [{ source: 'a', target: 'b' }],
}, true, true);
console.log(JSON.stringify(Object.assign({ rendered }, log)));
"""


def _run_render(*, show_unlinked: bool = False, parked: bool = False) -> dict:
    source = DASHBOARD.read_text(encoding="utf-8")
    # The harness slices real source; keep its landmarks honest.
    assert "function graphRenderEngine(" in source
    assert "/* Nav away from the graph view" in source
    scenario = json.dumps({"showUnlinked": show_unlinked, "parked": parked})
    result = subprocess.run(
        [NODE, "-e", RENDER_HARNESS, str(DASHBOARD), scenario],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert report["error"] is None, report["error"]
    assert report["rendered"] is True
    return report


@requires_node
@pytest.mark.parametrize("checked", [False, True])
def test_dashboard_tells_the_engine_whether_to_show_unlinked_entities(checked: bool) -> None:
    """"Show unlinked nodes" is filtered twice, and only one half was wired up.

    ``graphData()`` starts supplying degree-zero entities when the box is ticked, but the
    engine re-filters on its own ``showUnlinked``/``minDegree`` state — which stays at the
    defaults that drop exactly those entities — unless the dashboard says otherwise.
    """
    report = _run_render(show_unlinked=checked)

    assert report["scope"] is not None, "the engine never learns the checkbox state"
    assert report["scope"]["showUnlinked"] is checked
    # minDegree matters just as much: showUnlinked alone still loses to `degree >= 1`.
    assert report["scope"]["minDegree"] == (0 if checked else 1)


@requires_node
def test_dashboard_hands_the_engine_the_active_themes_entity_colours() -> None:
    """The other half of the theme fix: the engine can only use what it is given."""
    report = _run_render()

    assert report["themeColors"] is not None, "the engine never learns the active theme"
    # Resolved from the stubbed --entity-* custom properties, not from any JS constant.
    assert report["themeColors"]["person_or_concept"] == "#112233"
    assert report["themeColors"]["organization"] == "#556677"
    assert report["themeColors"]["accent"] == "#778899"
    assert report["themeColors"]["surface"] == "#9a7654"
    assert report["themeColors"]["canvas"] == "#345678"
    assert report["themeColors"]["relation_label"] == "#123456"
    assert report["themeColors"]["label"] == "#e7e9ee"
    # Every type the legend can show must be covered, or the canvas falls back per type.
    assert set(report["themeColors"]) == {
        "person_or_concept", "mention", "hashtag", "email", "organization", "location",
        "accent", "surface", "canvas", "relation_label", "label",
    }


def test_a_theme_switch_repaints_the_opt_in_canvas() -> None:
    """``applyTheme()`` is the only place a theme change is observable.

    It already calls ``graphRecolor()``; that path has to reach the engine, or the canvas keeps
    the previous theme until the next full graph render.
    """
    source = DASHBOARD.read_text(encoding="utf-8")
    assert "if(typeof graphRecolor==='function')graphRecolor()" in source
    recolor = source[source.index("function graphRecolor()"):]
    recolor = recolor[: recolor.index("\nfunction graphFit")]
    assert "engine.setThemeColors(graphThemeTypeColors())" in recolor


@requires_node
def test_a_renderer_created_after_leaving_the_graph_view_is_born_paused() -> None:
    """The rAF leak this PR already fixed once, reached by a different route.

    ``/graph`` and both lazy scripts resolve asynchronously.  Leaving Graph before they do runs
    the pause while ``GRAPH_ENGINE`` is still null, so the pending callback would create and
    start a renderer against a hidden pane that nothing ever pauses again.
    """
    parked = _run_render(parked=True)
    assert parked["created"] == 1
    assert parked["paused"] == 1, "a renderer created off-view keeps repainting forever"

    # On the view, the same path must not park a renderer the user is looking at.
    live = _run_render(parked=False)
    assert live["created"] == 1
    assert live["paused"] == 0


def test_leaving_the_graph_view_records_the_pause_as_well_as_applying_it() -> None:
    source = DASHBOARD.read_text(encoding="utf-8")
    assert "if(v==='graph')graphEngineResume();else graphEnginePause()" in source
    pause = source[source.index("function graphEnginePause()"):]
    pause = pause[: pause.index("\nfunction graphInvalidateData")]
    assert "GRAPH_ENGINE_PARKED=true" in pause
    assert "GRAPH_ENGINE_PARKED=false" in pause


#: Force-graph resolves each link's ``source``/``target`` from an id to the node object once it
#: owns the data, and the paint callbacks read ``.x``/``.y`` off those objects.  The recording
#: stand-in stores the arrays untouched, so a test that wants to *drive* a link painter has to
#: do that resolution — and give the nodes coordinates — itself.
LAY_OUT = """
const layOut = () => {
  const data = store.graphData;
  const byId = new Map(data.nodes.map(n => [n.id, n]));
  data.nodes.forEach((n, i) => { n.x = i * 10; n.y = i; });
  data.links.forEach(l => {
    const s = byId.get(l.source && l.source.id !== undefined ? l.source.id : l.source);
    const t = byId.get(l.target && l.target.id !== undefined ? l.target.id : l.target);
    if (s) l.source = s;
    if (t) l.target = t;
  });
  return data;
};
let painted = [];
const linkCtx = {
  font: '', fillStyle: '', textAlign: '', textBaseline: '',
  fillText(text) { painted.push(String(text)); },
};
const paintLinks = (scale, links) => {
  painted = [];
  const mode = store.linkCanvasObjectMode ? store.linkCanvasObjectMode() : undefined;
  const draw = store.linkCanvasObject;
  if (mode === 'after' && draw) (links || store.graphData.links).forEach(l => draw(l, linkCtx, scale));
  return painted.slice();
};
"""


@requires_node
def test_relation_labels_are_painted_when_the_labels_box_is_ticked() -> None:
    """**Labels** turns on two label layers on the classic path; the engine only had one.

    ``graphToggleLabels`` forwards the checkbox straight to ``setSettings({labels})``, and the
    classic renderer answers it with *both* entity names and a ``linkCanvasObject`` that paints
    each meaningful ``link.label``. Implicit ``co_occurs`` links are structural and deliberately
    excluded. The opt-in engine configured no link painter at all, so relation names silently
    disappeared under ``?graph-engine=next`` and could only be read by hovering one edge at a
    time.
    """
    report = _run_engine(
        LAY_OUT
        + """
        const api = G.create(el, { reducedMotion: () => true });
        api.setData({
          nodes: [{ id: 'a' }, { id: 'b' }],
          links: [
            { source: 'a', target: 'b', layer: 'entity', label: 'mentions' },
            { source: 'b', target: 'a', layer: 'semantic', label: 'co_occurs' },
          ],
        });
        layOut();
        const unticked = paintLinks(4);
        api.setSettings({ labels: true });
        api.setThemeColors({ relation_label: '#123456' });
        const ticked = paintLinks(4);
        const labelColor = linkCtx.fillStyle;
        // Relation labels are the noisiest layer: they stay off until the user zooms in.
        const zoomedOut = paintLinks(1);
        emit({ unticked, ticked, zoomedOut, labelColor });
        """
    )
    assert report["unticked"] == []
    assert report["ticked"] == ["mentions"], "the Labels checkbox never paints relation names"
    assert report["labelColor"] == "#123456", "relation labels ignore the active theme"
    assert report["zoomedOut"] == []


def test_classic_graph_hides_implicit_co_occurrence_edge_labels() -> None:
    """The Labels toggle keeps meaningful relation names but omits structural co-occurrences."""
    static = DASHBOARD.read_text(encoding="utf-8")
    classic = CLASSIC_DASHBOARD.read_text(encoding="utf-8")
    assert static == classic, "the classic dashboard assets must remain synchronized"
    label_guard = "function graphShowRelationLabel(label){return !!label&&String(label).toLowerCase()!=='co_occurs'}"
    assert label_guard in static
    assert "if(scale<2.4||!graphShowRelationLabel(link.label)||!link.source.x" in static


@requires_node
def test_node_labels_are_capped_at_the_configured_density() -> None:
    """A high density setting must still bound per-frame node-label painting."""
    report = _run_engine(
        """
        let labels = [];
        const ctx = {
          globalAlpha: 1, fillStyle: '', strokeStyle: '', lineWidth: 1, font: '', textBaseline: '',
          save() {}, restore() {}, beginPath() {}, arc() {}, stroke() {}, fill() {},
          createLinearGradient() { return { addColorStop() {} }; },
          createRadialGradient() { return { addColorStop() {} }; },
          fillText(text) { labels.push(String(text)); },
        };
        const api = G.create(el, { reducedMotion: () => true });
        api.setData(chain(20));
        api.setSettings({ labels: true, labelDensity: 3 });
        store.graphData.nodes.forEach((node, index) => {
          node.x = index * 10; node.y = 0; store.nodeCanvasObject(node, ctx, 1);
        });
        const names = labels.filter(value => value.startsWith('n'));
        emit({ names, distinct: [...new Set(names)] });
        """
    )
    assert len(report["distinct"]) == 3
    assert len(report["names"]) == 6  # shadow + foreground per selected node


def test_collapsed_cluster_labels_use_the_active_theme_text_colour() -> None:
    source = ASSET.read_text(encoding="utf-8")
    cluster_paint = source[source.index("if (node.cluster)"):source.index("if (state.bridges", source.index("if (node.cluster)"))]
    assert "state.themeColors.label || '#e7e9ee'" in cluster_paint


@requires_node
def test_node_labels_use_the_active_theme_text_colour() -> None:
    """Classic labels paint onto the canvas, so near-white is unreadable on light themes."""

    report = _run_engine(
        LAY_OUT
        + """
        const api = G.create(el, { reducedMotion: () => true });
        api.setData(chain(2));
        const data = layOut();
        api.setStyle('classic');
        api.setThemeColors({ label: '#123456' });
        api.setHighlight('n0');
        const styles = [];
        const ctx = {
          set fillStyle(value) { styles.push(value); }, get fillStyle() { return ''; },
          font: '', textBaseline: '', lineWidth: 0, strokeStyle: '', globalAlpha: 1,
          beginPath() {}, arc() {}, fill() {}, stroke() {}, fillText() {}, save() {}, restore() {},
          createRadialGradient() { return { addColorStop() {} }; },
          createLinearGradient() { return { addColorStop() {} }; },
        };
        store.nodeCanvasObject(data.nodes[0], ctx, 1);
        emit({ styles });
        """
    )
    assert "#123456" in report["styles"], "node labels ignored the active theme text colour"


@requires_node
def test_unfreezing_releases_nodes_pinned_by_dragging() -> None:
    """Freeze off must resume the whole layout, including nodes a prior drag pinned."""

    report = _run_engine(
        """
        const api = G.create(el, { reducedMotion: () => false });
        api.setData(chain(2));
        const node = store.graphData.nodes[0];
        node.x = 17; node.y = 23;
        store.onNodeDragEnd(node);
        const pinned = { fx: node.fx, fy: node.fy };
        api.freeze(true);
        api.freeze(false);
        emit({ pinned, released: { fx: node.fx, fy: node.fy } });
        """
    )
    assert report["pinned"] == {"fx": 17, "fy": 23}
    assert report["released"] == {}, "unfreezing left a dragged node immovable"


def test_primary_graph_starts_unfrozen_so_the_force_controls_take_effect() -> None:
    """A fresh graph must settle, rather than make every tuning control look inert."""

    assert "graphFrozen: false" in PRIMARY_LEDGER.read_text(encoding="utf-8")
    assert "graphPreference('frozen', false)" in PRIMARY_LEDGER.read_text(encoding="utf-8")
    assert 'id="graph-freeze" class="graph-switch"' in PRIMARY_INDEX.read_text(encoding="utf-8")
    freeze_control = PRIMARY_INDEX.read_text(encoding="utf-8").split('id="graph-freeze"', 1)[1]
    assert 'aria-checked="false"' in freeze_control


def test_primary_dashboard_has_no_visible_notice_popup() -> None:
    """Action feedback must not cover the dashboard with a dismissible toast."""

    markup = PRIMARY_INDEX.read_text(encoding="utf-8")
    source = PRIMARY_LEDGER.read_text(encoding="utf-8")
    styles = (ROOT / "engraphis" / "dashboard_assets" / "ledger.css").read_text(encoding="utf-8")
    assert 'id="notice"' not in markup
    assert ">Dismiss<" not in markup
    assert 'id="notice-text" class="sr-only"' in markup
    assert "byId('notice').hidden" not in source
    assert "notice-close" not in source
    assert ".notice {" not in styles


def test_primary_layout_choices_resume_a_frozen_graph_including_full_mode() -> None:
    """An explicit layout choice must visibly apply rather than merely change its selected chip."""

    source = PRIMARY_LEDGER.read_text(encoding="utf-8")
    handler = source.split("all('[data-graph-preset-choice]')", 1)[1].split(
        "all('[data-graph-style-choice]')", 1
    )[0]
    assert "const resumeLayout = state.graphFrozen;" in handler
    assert "state.graphFrozen = false;" in handler
    assert "state.graphEngine.freeze(false);" in handler
    assert "state.graphEngine.setPreset(preset);" in handler


@requires_node
def test_focusing_an_entity_the_canvas_is_not_showing_does_not_report_success() -> None:
    """``zoomToNode`` is the dashboard's visibility oracle, and it was answering from memory.

    ``graphFocus`` treats ``false`` as "offer the recovery path" — tick *Show unlinked*, retry,
    and otherwise say *Entity not in view*.  The engine answered from ``raw.nodes``, which keeps
    the coordinates force-graph left on a node from an earlier render, so a node hidden by the
    auto-collapsed view (only ``cluster-*`` bubbles are drawn below zoom 0.55) or by a scope
    filter still reported success — the camera moved to nothing and the user got no explanation.
    """
    report = _run_engine(
        """
        const collapses = [];
        const api = G.create(el, {
          reducedMotion: () => true, onCollapseChange: value => collapses.push(value),
        });
        api.setData({
          nodes: [{ id: 'a' }, { id: 'b' }, { id: 'c' }, { id: 'lonely' }],
          links: [{ source: 'a', target: 'b' }, { source: 'b', target: 'c' }],
        });
        const shownIds = () => (store.graphData.nodes || []).map(n => n.id);
        // Everything visible once, so every entity carries real coordinates from here on.
        api.setScope({ showUnlinked: true, minDegree: 0 });
        store.graphData.nodes.forEach((n, i) => { n.x = i * 10; n.y = i; });

        // 1. Hidden by the scope filter, but still remembered with valid coordinates.
        api.setScope({ showUnlinked: false, minDegree: 1 });
        const filtered = { found: api.zoomToNode('lonely'), shown: shownIds() };

        // 2. Hidden by the collapsed view, which paints cluster bubbles instead of entities.
        api.setCollapse(true);
        const whileCollapsed = shownIds();
        const expanding = api.zoomToNode('c');
        // The recording renderer has no simulation tick to assign fresh coordinates after
        // expansion. The Ledger reveal helper retries on the next animation frame; model that
        // settled frame here before asserting the second camera attempt.
        const rendered = (store.graphData.nodes || []).find(n => n.id === 'c');
        rendered.x = 20; rendered.y = 2;
        const focused = api.zoomToNode('c');
        emit({
          filtered, whileCollapsed, expanding, focused, collapses,
          afterFocus: shownIds(), collapsed: api.state().collapsed,
        });
        """
    )
    # A filtered-out entity is not in view, so the dashboard must be told to recover.
    assert report["filtered"]["found"] is False, "a filtered-out entity reported as visible"
    assert "lonely" not in report["filtered"]["shown"]
    # A collapsed view really is showing only bubbles...
    assert report["whileCollapsed"] == ["cluster-0"]
    # ...so focusing a named entity has to expand it. A live force graph assigns fresh positions
    # on its next frame, which Ledger's reveal helper retries before centering the node.
    assert report["expanding"] is False
    assert report["focused"] is True
    assert report["collapsed"] is False
    assert "c" in report["afterFocus"], "the entity is still not on the canvas"
    assert report["collapses"][-1] is False, "the dashboard was never told the view expanded"


@requires_node
def test_revealing_a_graph_fact_centers_the_rendered_entity_without_a_fit_race() -> None:
    """A Graph facts row must reveal one stable entity, not restart and fit a subgraph.

    The camera must use the coordinates ForceGraph is currently painting. That avoids stale
    raw-node coordinates and, by cancelling pending ``zoomToFit``, prevents the delayed global
    fit that used to pull the selected entity off-screen after the row click.
    """
    report = _run_engine(
        """
        const api = G.create(el, { reducedMotion: () => true });
        api.setData({
          nodes: [{ id: 'a' }, { id: 'selected' }, { id: 'c' }],
          links: [{ source: 'a', target: 'selected' }, { source: 'selected', target: 'c' }],
        });
        const seeded = calls.graphData;
        // Deliberately differ from raw data: `reveal` must follow what the canvas renders.
        store.graphData = { nodes: [{ id: 'selected', x: 37, y: -53 }], links: [] };
        const revealed = api.reveal('selected');
        emit({
          revealed, seeded, after: calls.graphData,
          centerAt: store.centerAt, zoom: store.zoom,
          fits: calls.zoomToFit || 0,
        });
        """
    )
    assert report["revealed"] is True
    assert report["after"] == report["seeded"], "revealing a fact reseeded the graph"
    assert report["centerAt"] == [37, -53, 0]
    assert report["zoom"] == [3, 0]
    assert report["fits"] == 0, "a global fit competed with the selected-node camera move"


@requires_node
def test_appearance_only_changes_do_not_restart_the_layout() -> None:
    """Style, Color by, Labels and Flow repaint the graph; they must not re-run it.

    ``visible()`` allocates fresh arrays on every call, and force-graph treats any ``graphData``
    call as a data update: it re-copies the nodes and d3 resets the simulation alpha to 1.  So
    every appearance-only setter threw the settled layout away and made the whole graph move.
    The classic renderer guards the same seed with ``if(dataChanged)FG.graphData(data)``.
    """
    report = _run_engine(
        """
        const api = G.create(el, { reducedMotion: () => true });
        const nodes = [{ id: 'lonely', etype: 'organization' }], links = [];
        for (let i = 0; i < 12; i++) nodes.push({ id: 'n' + i, etype: 'person_or_concept' });
        for (let i = 0; i < 11; i++) links.push({ source: 'n' + i, target: 'n' + (i + 1) });
        api.setData({ nodes, links });
        const seeded = calls.graphData;
        const before = store.graphData.nodes[0].color;
        const repaintsBefore = calls.nodeCanvasObject;

        api.setStyle('galaxy');
        api.setColorBy('type');
        api.setSettings({ labels: true });
        api.setSettings({ flow: false });
        const paintOnly = calls.graphData;
        const recoloured = store.graphData.nodes[0].color;
        const repaintsAfter = calls.nodeCanvasObject;

        // A genuine change to the visible set still has to reach force-graph.
        api.setScope({ showUnlinked: true, minDegree: 0 });
        emit({
          seeded, paintOnly, afterScope: calls.graphData, before, recoloured,
          repaintsBefore, repaintsAfter, shown: store.graphData.nodes.length,
        });
        """
    )
    assert report["paintOnly"] == report["seeded"], "an appearance change restarted the layout"
    assert report["afterScope"] > report["seeded"], "a real view change never reached the canvas"
    assert report["shown"] == 13
    # Skipping the reseed must not mean skipping the paint.
    assert report["recoloured"] != report["before"]
    assert report["repaintsAfter"] > report["repaintsBefore"]


@requires_node
def test_simulation_time_is_bounded_on_a_large_graph() -> None:
    """force-graph's default cooldown is 15 seconds; nothing here was overriding it.

    The classic path caps a large graph at 1.1s / 80 ticks precisely because running the layout
    — and therefore repainting every node and link — for the full default window is what makes a
    big store feel broken on load and after every reheat.
    """
    report = _run_engine(
        """
        const api = G.create(el, {});
        api.setData(chain(40));
        const small = {
          time: store.cooldownTime, ticks: store.cooldownTicks, warmup: store.warmupTicks,
          alpha: store.d3AlphaDecay, velocity: store.d3VelocityDecay,
        };
        // 3001 entities / 3000 relations — past the classic renderer's 600-node signal.
        api.setData(chain(3000));
        const big = {
          time: store.cooldownTime, ticks: store.cooldownTicks, warmup: store.warmupTicks,
          alpha: store.d3AlphaDecay, velocity: store.d3VelocityDecay,
        };
        const still = G.create(el, { reducedMotion: () => true });
        still.setData(chain(40));
        emit({
          small, big,
          reduced: { time: store.cooldownTime, ticks: store.cooldownTicks },
        });
        """
    )
    assert report["small"]["time"] == 2200
    assert report["small"]["ticks"] == 160
    # The number this guards: the vendor default left a 3k-relation store simulating for 15s.
    assert report["big"]["time"] == 1100
    assert report["big"]["ticks"] == 80
    assert report["big"]["warmup"] == 18
    # A large graph also settles harder, exactly as GPERF.large does on the classic path.
    assert report["big"]["alpha"] > report["small"]["alpha"]
    assert report["big"]["velocity"] > report["small"]["velocity"]
    # Reduced motion asks for a static layout, not a shorter animation.
    assert report["reduced"]["time"] == 0
    assert report["reduced"]["ticks"] == 1


@requires_node
def test_physics_sliders_reheat_the_simulation_the_way_the_classic_renderer_does() -> None:
    """Installing a new force on a settled graph moves nothing without a reheat.

    ``graphSet`` (dashboard.js) routes Repel/Link/Gravity/Size/Font/Link-width/Label-density
    through ``setSettings`` under ``?graph-engine=next``.  The classic branch of that same
    function treats ``repel|link|gravity|size`` as *layout* changes: it re-applies the forces
    and then reheats unless the user asked for reduced motion.  The engine's ``applyForces()``
    only swaps the charge/link/forceX-forceY/collide values into the running simulation — and a
    settled graph sits at alpha~0 — so without the reheat those four sliders are inert until
    the user finds the Reheat button.  The paint-only settings must *not* reheat: restarting
    the layout because a label got bigger throws away the arrangement the user is reading.
    """
    report = _run_engine(
        """
        const reheats = () => invocations.d3ReheatSimulation || 0;
        const bump = (api, patch) => { const before = reheats(); api.setSettings(patch); return reheats() - before; };

        const api = G.create(el, {});
        api.setData(chain(40));
        const layout = {
          repel: bump(api, { repel: 260 }),
          link: bump(api, { link: 90 }),
          gravity: bump(api, { gravity: 12 }),
          size: bump(api, { size: 5 }),
          mode: bump(api, { mode: 'radial' }),
        };
        const paint = {
          font: bump(api, { font: 11 }),
          linkw: bump(api, { linkw: 2.4 }),
          labelDensity: bump(api, { labelDensity: 40 }),
          labels: bump(api, { labels: true }),
          flow: bump(api, { flow: false }),
        };

        // The classic path's `if(layout&&!prefersReducedMotion())` exemption.
        const still = G.create(el, { reducedMotion: () => true });
        still.setData(chain(40));
        const reducedMotion = bump(still, { repel: 260 });
        emit({ layout, paint, reducedMotion });
        """
    )
    # The four sliders the classic renderer calls a layout change, plus the preset itself.
    assert report["layout"] == {
        "repel": 1, "link": 1, "gravity": 1, "size": 1, "mode": 1
    }, "a physics slider installed new forces on a settled graph and nothing moved"
    # Appearance-only settings keep the arrangement the user is looking at.
    assert report["paint"] == {
        "font": 0, "linkw": 0, "labelDensity": 0, "labels": 0, "flow": 0
    }, "an appearance change restarted the layout"
    assert report["reducedMotion"] == 0, "reduced motion still got an animated relayout"


@requires_node
def test_full_graph_within_the_force_budget_keeps_centre_gravity_live() -> None:
    """Full mode must not turn a normal large workspace into a pinned, inert ring.

    The screenshot regression occurred at a few thousand relationships: the UI showed a
    centre-gravity value, but the full-graph branch had removed every D3 force and fixed every
    node's coordinates.  It is safe to run a bounded simulation at this size, so the same
    centre force and reheat contract as Overview must remain observable in Full mode.
    """
    report = _run_engine(
        """
        const axes = { x: [], y: [] };
        const bodyForce = () => ({ strength(value) { this.value = value; return this; } });
        globalThis.d3 = {
          forceManyBody: bodyForce,
          forceLink: () => ({ id(value) { this.idValue = value; return this; }, distance(value) { this.value = value; return this; } }),
          forceX: target => { const force = { target, strength(value) { this.value = value; return this; } }; axes.x.push(force); return force; },
          forceY: target => { const force = { target, strength(value) { this.value = value; return this; } }; axes.y.push(force); return force; },
          forceCollide: () => ({ iterations(value) { this.value = value; return this; } }),
        };
        const api = G.create(el, {});
        api.setRenderMode('full');
        // Keep this below the responsive full-graph ceiling. Larger full graphs deliberately
        // take the deterministic, centred layout so a complete workspace cannot lock the UI.
        api.setData(chain(400));
        const before = invocations.d3ReheatSimulation || 0;
        api.setSettings({ gravity: 98 });
        const nodes = store.graphData.nodes;
        emit({
          mode: api.state().renderMode,
          x: axes.x.at(-1), y: axes.y.at(-1),
          reheat: (invocations.d3ReheatSimulation || 0) - before,
          cooldown: store.cooldownTime,
          pinned: nodes.filter(node => node.fx !== undefined || node.fy !== undefined).length,
        });
        """
    )
    assert report["mode"] == "full"
    assert report["x"] == {"target": 0, "value": 0.98}
    assert report["y"] == {"target": 0, "value": 0.98}
    assert report["reheat"] == 1
    assert report["cooldown"] == 1100
    assert report["pinned"] == 0


@requires_node
def test_full_graph_beyond_responsive_force_budget_is_centred_and_responds_to_gravity() -> None:
    """A complete graph past the responsive budget takes the centred static fallback.

    Above the live-force ceiling the deterministic layout protects responsiveness.  Its
    geometry is nevertheless a centred grid whose compactness follows the same gravity input,
    so the user retains a meaningful correction even for a very large workspace.
    """
    report = _run_engine(
        """
        const span = nodes => Math.max(...nodes.map(node => node.x)) - Math.min(...nodes.map(node => node.x));
        const api = G.create(el, {});
        api.setRenderMode('full');
        // `chain` supplies N+1 nodes, so this is one past the live-force ceiling.
        api.setData(chain(600));
        const before = span(store.graphData.nodes);
        const reheatBefore = invocations.d3ReheatSimulation || 0;
        api.setSettings({ gravity: 98 });
        const nodes = store.graphData.nodes;
        emit({
          before, after: span(nodes),
          reheat: (invocations.d3ReheatSimulation || 0) - reheatBefore,
          pinned: nodes.filter(node => Number.isFinite(node.fx) && Number.isFinite(node.fy)).length,
          total: nodes.length,
          cooldown: store.cooldownTime,
        });
        """
    )
    assert report["after"] < report["before"] * 0.5
    assert report["reheat"] == 0
    assert report["pinned"] == report["total"] == 601
    assert report["cooldown"] == 0


@requires_node
def test_curves_arrows_and_relation_labels_are_dropped_on_a_dense_graph() -> None:
    """Three per-edge costs the classic path turns off past ``GPERF.dense`` (links > 1500).

    A curved link is a quadratic bezier instead of a straight line, an arrowhead is a filled
    triangle, and a relation label is a text layout — each per relation, each every frame.  At
    this density they are unreadable anyway, so the classic renderer pays for none of them.
    """
    report = _run_engine(
        LAY_OUT
        + """
        const api = G.create(el, { reducedMotion: () => true });
        api.setSettings({ labels: true });

        api.setData(chain(1500));
        const atLimit = {
          curve: store.linkCurvature, arrow: store.linkDirectionalArrowLength,
        };

        api.setData(chain(1501));
        const overLimit = {
          curve: store.linkCurvature, arrow: store.linkDirectionalArrowLength,
        };
        // One laid-out relation is enough to drive the label painter at this size.
        const data = layOut();
        data.links[0].label = 'mentions';
        const denseUnhighlighted = paintLinks(4, [data.links[0]]);
        store.onNodeHover(data.nodes[0]);
        const denseHighlighted = paintLinks(4, [data.links[0]]);
        emit({ atLimit, overLimit, denseUnhighlighted, denseHighlighted });
        """
    )
    # 1500 links is the classic threshold itself, so nothing is dropped yet.
    assert report["atLimit"]["curve"] == 0.12
    assert report["atLimit"]["arrow"] == 0.625
    assert report["overLimit"]["curve"] == 0
    assert report["overLimit"]["arrow"] == 0
    # Relation labels come back for the one neighbourhood the user is actually pointing at.
    assert report["denseUnhighlighted"] == []
    assert report["denseHighlighted"] == ["mentions"]


#: A ``d3`` stand-in for the force constructors ``applyForces()`` reaches for.  The asset reads
#: ``d3`` as a free variable, so assigning it on ``globalThis`` is what the browser's global
#: script tag does; without it ``applyForces()`` returns before it ever configures collision.
D3_STUB = """
let collide = null;
globalThis.d3 = {
  forceX: () => ({ strength: () => ({}) }),
  forceY: () => ({ strength: () => ({}) }),
  forceRadial: () => ({ strength: () => ({}) }),
  forceCollide: radius => ({ radius, iterations(n) { collide = { radius, iterations: n }; return this; } }),
};
"""


@requires_node
def test_default_community_layout_uses_one_shared_gravity_center() -> None:
    """The default must not put each detected community in a separate orbit.

    The old community target function placed groups on a broad ring, leaving the centre empty
    and stretching cross-community relations across the whole canvas.  Both dashboard renderers
    now use the origin as their default force target; charge and links are sufficient to retain
    readable local separation.
    """

    classic = DASHBOARD.read_text(encoding="utf-8")
    classic_forces = classic[classic.index("function graphApplyForces()") : classic.index("function graphSetHighlight(")]
    assert "if(mode==='communities')" not in classic_forces
    assert "FG.d3Force('x',d3.forceX(0).strength(centering));" in classic_forces
    assert "FG.d3Force('y',d3.forceY(0).strength(centering));" in classic_forces

    report = _run_engine(
        """
        const targets = { x: [], y: [] };
        globalThis.d3 = {
          forceX: target => { targets.x.push(target); return { strength: () => ({}) }; },
          forceY: target => { targets.y.push(target); return { strength: () => ({}) }; },
          forceRadial: () => ({ strength: () => ({}) }),
          forceCollide: () => ({ iterations: () => ({}) }),
        };
        const api = G.create(el, { reducedMotion: () => true });
        api.setData(chain(8));
        emit({ x: targets.x, y: targets.y });
        """
    )
    assert report == {"x": [0], "y": [0]}


@requires_node
def test_collision_runs_one_pass_on_a_large_graph_like_the_classic_renderer() -> None:
    """``forceCollide().iterations(2)`` is a second full quadtree traversal per node per tick.

    ``graphApplyForces()`` on the classic path spends it only when it is affordable
    (``.iterations(GPERF.large?1:2)``).  The opt-in engine computes the same ``large`` signal for
    its cooldown and alpha-decay constants but was pinning two iterations regardless, so the one
    case where the extra pass hurts most — the initial layout and every reheat of a big store —
    was the case that paid for it twice over.
    """
    report = _run_engine(
        D3_STUB
        + """
        const api = G.create(el, { reducedMotion: () => true });

        api.setData(chain(40));
        const small = collide.iterations;

        // 601 entities / 600 relations — one past the classic renderer's 600-node cutoff.
        api.setData(chain(600));
        const big = collide.iterations;

        // A slider move re-runs applyForces() on the running simulation; it must not undo this.
        api.setSettings({ repel: 90 });
        const afterSlider = collide.iterations;
        emit({ small, big, afterSlider, radiusIsAFunction: typeof collide.radius === 'function' });
        """
    )
    assert report["small"] == 2
    assert report["big"] == 1, "a large graph still runs two collision passes per tick"
    assert report["afterSlider"] == 1, "a slider move restored the expensive collision pass"
    # Guards the whole call rather than the argument in isolation: a per-node radius, not a
    # constant, is what makes collision agree with the sizes the renderer actually painted.
    assert report["radiusIsAFunction"] is True


#: Counts the gradient and blur primitives independently. They are per node, per frame, so the
#: large-graph branch must never rebuild them hundreds of times during a layout tick.
GLOW_CANVAS_STUB = """
let gradients = 0, blurs = 0, fills = 0;
const ctx = {
  globalAlpha: 1, globalCompositeOperation: '', strokeStyle: '', lineWidth: 1, font: '',
  textBaseline: '', shadowColor: '',
  set shadowBlur(v) { if (v) blurs += 1; },
  get shadowBlur() { return 0; },
  set fillStyle(v) {}, get fillStyle() { return ''; },
  save() {}, restore() {}, beginPath() {}, arc() {}, ellipse() {}, stroke() {},
  setLineDash() {}, fillText() {},
  fill() { fills += 1; },
  createRadialGradient() { gradients += 1; return { addColorStop() {} }; },
  createLinearGradient() { gradients += 1; return { addColorStop() {} }; },
};
const paintNodes = () => {
  gradients = 0; blurs = 0; fills = 0;
  const draw = store.nodeCanvasObject;
  store.graphData.nodes.forEach((n, i) => { n.x = i * 10; n.y = i; draw(n, ctx, 4); });
  return { gradients, blurs, fills };
};
"""


@requires_node
@pytest.mark.parametrize("style", ["galaxy", "solar"])
def test_per_node_glow_is_dropped_on_a_large_graph(style: str) -> None:
    """Every ``rich`` node was getting a bloom or a gradient on every frame, at any size.

    The classic renderer gates all three of them on ``!GPERF.large`` — the galaxy halo, the solar
    corona and its sphere shading. A radial gradient is a fresh object per node; at the >600-node
    cutoff that is hundreds rebuilt per tick, on top of the layout, which is what made a dense
    workspace crawl even after the other large-graph optimisations kicked in.

    ``fills`` is the control: the nodes are still being drawn, so a zero glow count means the
    effect was skipped, not that the paint never ran.
    """
    report = _run_engine(
        GLOW_CANVAS_STUB
        + f"""
        const api = G.create(el, {{ reducedMotion: () => true }});
        api.setStyle("{style}");

        api.setData(chain(40));
        const small = paintNodes();

        api.setData(chain(600));
        const big = paintNodes();
        emit({{ small, big }});
        """
    )
    small, big = report["small"], report["big"]
    assert small["fills"] > 0 and big["fills"] > 0, "canvas stub never reached the node painter"
    assert small["gradients"] + small["blurs"] > 0, "the small graph lost its glow entirely"
    assert big["gradients"] == 0, f"{style} still builds a radial gradient per node when large"
    assert big["blurs"] == 0, f"{style} still shadow-blurs every node when large"


@requires_node
def test_material_recipes_keep_four_fixed_families_and_only_react_at_the_edges() -> None:
    """A graph palette is an identity accent, not a licence to repaint every alloy the same.

    This replaces the old gradient-stop counts: those merely documented one shared thin-film
    painter.  The pure recipe seam makes the intended material contract directly testable.
    """
    report = _run_node(
        """
        const slate = { accent: '#a39bf1', surface: '#16191f', canvas: '#0b0d13' };
        const matrix = { accent: '#3ce072', surface: '#04140a', canvas: '#020703' };
        const make = (theme, palette, identity) => Object.fromEntries(
          ['cyber', 'galaxy', 'solar', 'classic'].map(style =>
            [style, I.materialRecipe(style, theme, palette, identity)]));
        emit({ slate: make(slate, 'ocean', '#37bde4'), matrix: make(matrix, 'ember', '#f59e55') });
        """
    )
    slate, matrix = report["slate"], report["matrix"]
    assert {recipe["family"] for recipe in slate.values()} == {
        "iridescent-pvd", "anodized-alloy", "brushed-copper", "satin-gunmetal"
    }
    assert slate["cyber"]["film"] == slate["cyber"]["fixedPalette"]
    assert len(slate["cyber"]["film"]) >= 4
    # Fixed material signatures survive a theme/palette switch; only the substrate/identity
    # inputs may react. Solar must never inherit Cyber's cyan/magenta spectrum.
    for style in slate:
        assert slate[style]["family"] == matrix[style]["family"]
        assert slate[style]["fixedPalette"] == matrix[style]["fixedPalette"]
        assert slate[style]["substrate"] != matrix[style]["substrate"]
        assert slate[style]["identity"] != matrix[style]["identity"]
    assert "#19d8ed" not in {value.lower() for value in slate["solar"]["fixedPalette"]}


@requires_node
def test_material_tiers_are_screen_space_not_graph_size_heuristics() -> None:
    report = _run_node(
        """
        emit({
          tiny: I.materialTier(4), bezel: I.materialTier(8), full: I.materialTier(16),
          exactLow: I.materialTier(5.99), exactBezel: I.materialTier(6),
          exactFull: I.materialTier(12), forced: I.materialTier(32, true),
        });
        """
    )
    assert report == {
        "tiny": "signature", "bezel": "bezel", "full": "full",
        "exactLow": "signature", "exactBezel": "bezel", "exactFull": "full",
        "forced": "signature",
    }


@requires_node
def test_material_colour_invariants_are_distinct_and_deterministic() -> None:
    """Pin visual intent in RGB rather than vendor-specific gradient primitive counts."""
    report = _run_node(
        """
        const theme = { accent: '#a39bf1', surface: '#16191f', canvas: '#0b0d13' };
        const sample = style => ['top', 'center', 'bottom'].map(position =>
          I.sampleMaterialColour(style, position, '#37bde4', theme));
        emit({ once: Object.fromEntries(['cyber', 'galaxy', 'solar', 'classic'].map(s => [s, sample(s)])),
          twice: Object.fromEntries(['cyber', 'galaxy', 'solar', 'classic'].map(s => [s, sample(s)])) });
        """
    )
    assert report["once"] == report["twice"], "static materials must not rotate or flicker"
    cyber_top, _, cyber_bottom = report["once"]["cyber"]
    galaxy = report["once"]["galaxy"][1]
    solar = report["once"]["solar"][1]
    classic = report["once"]["classic"][1]
    assert cyber_top[0] > cyber_bottom[0] and cyber_bottom[1] > cyber_top[1], (
        "Cyber must retain the fixed warm/magenta-top, cyan-lower iridescent direction"
    )
    assert galaxy[2] > galaxy[0] and galaxy[2] > galaxy[1], "Galaxy must read blue/violet"
    assert solar[0] > solar[1] > solar[2], "Solar must read as warm copper, never cyan"
    assert max(classic[:3]) - min(classic[:3]) <= 55, "Classic must remain low-saturation steel"


@requires_node
def test_material_cache_is_bounded_and_warm_repaints_allocate_nothing() -> None:
    report = _run_node(
        """
        const gradient = () => ({ addColorStop() {} });
        const ctx = {
          save() {}, restore() {}, beginPath() {}, closePath() {}, arc() {}, fill() {}, stroke() {},
          clearRect() {}, fillRect() {}, translate() {}, rotate() {}, scale() {}, clip() {},
          createLinearGradient: gradient, createRadialGradient: gradient, createConicGradient: gradient,
          setLineDash() {}, drawImage() {}, globalAlpha: 1, globalCompositeOperation: 'source-over',
          lineWidth: 1, fillStyle: '', strokeStyle: '', shadowBlur: 0, shadowColor: '',
        };
        I.setMaterialCanvasFactory(() => ({ width: 0, height: 0, getContext: () => ctx }));
        I.clearMaterialCache(true);
        const options = { style: 'cyber', radius: 16, dpr: 2,
          identity: '#37bde4', themeColors: { accent: '#a39bf1', surface: '#16191f' } };
        I.renderMaterialSample(options);
        const cold = I.materialCacheStats();
        I.renderMaterialSample(options);
        const warm = I.materialCacheStats();
        for (let n = 0; n < cold.limit + 3; n += 1) {
          I.renderMaterialSample({ ...options, identity: '#' + n.toString(16).padStart(6, '0') });
        }
        const saturated = I.materialCacheStats();
        I.setMaterialCanvasFactory(null);
        emit({ cold, warm, saturated });
        """
    )
    assert report["cold"]["allocations"] == 1
    assert report["warm"]["allocations"] == report["cold"]["allocations"]
    assert report["warm"]["hits"] > report["cold"]["hits"]
    assert report["saturated"]["size"] <= report["saturated"]["limit"]
    assert report["saturated"]["evictions"] > 0


@requires_node
def test_material_cache_is_invalidated_by_theme_palette_style_and_dpr_changes() -> None:
    report = _run_engine(
        """
        const gradient = () => ({ addColorStop() {} });
        const ctx = {
          save() {}, restore() {}, beginPath() {}, closePath() {}, arc() {}, fill() {}, stroke() {},
          clearRect() {}, fillRect() {}, translate() {}, rotate() {}, scale() {}, clip() {},
          createLinearGradient: gradient, createRadialGradient: gradient, createConicGradient: gradient,
          setLineDash() {}, drawImage() {}, globalAlpha: 1, globalCompositeOperation: 'source-over',
          lineWidth: 1, fillStyle: '', strokeStyle: '', shadowBlur: 0, shadowColor: '',
        };
        I.setMaterialCanvasFactory(() => ({ width: 0, height: 0, getContext: () => ctx }));
        I.clearMaterialCache(true);
        const sample = dpr => I.renderMaterialSample({ style: 'cyber', radius: 16, dpr,
          identity: '#37bde4', themeColors: { accent: '#a39bf1', surface: '#16191f' } });
        sample(1); const populated = I.materialCacheStats();
        const api = G.create(el, { reducedMotion: () => true });
        api.setData(chain(2));
        api.setThemeColors({ accent: '#3ce072', surface: '#04140a' });
        const themed = I.materialCacheStats();
        sample(1); api.setPalette('ember'); const paletted = I.materialCacheStats();
        sample(1); api.setStyle('solar'); const styled = I.materialCacheStats();
        sample(1); sample(2); const dprChanged = I.materialCacheStats();
        I.setMaterialCanvasFactory(null);
        emit({ populated, themed, paletted, styled, dprChanged });
        """
    )
    assert report["populated"]["size"] > 0
    for name in ("themed", "paletted", "styled"):
        assert report[name]["size"] == 0, f"{name} material update retained stale sprites"
    assert report["dprChanged"]["size"] == 1
    assert report["dprChanged"]["clears"] >= 4


@requires_node
def test_material_fallback_without_conic_gradient_still_paints() -> None:
    report = _run_node(
        """
        const gradient = () => ({ addColorStop() {} });
        let fills = 0;
        const ctx = {
          save() {}, restore() {}, beginPath() {}, closePath() {}, arc() {}, stroke() {},
          fill() { fills += 1; }, clearRect() {}, fillRect() {}, translate() {}, rotate() {}, clip() {},
          createLinearGradient: gradient, createRadialGradient: gradient,
          lineWidth: 1, fillStyle: '', strokeStyle: '', globalAlpha: 1, shadowBlur: 0, shadowColor: '',
        };
        const recipe = I.materialRecipe('cyber', { accent: '#a39bf1', surface: '#16191f' }, 'ocean', '#37bde4');
        I.paintMaterialDirect(ctx, 20, 20, 16, recipe, 'full');
        emit({ fills });
        """
    )
    assert report["fills"] > 0


@requires_node
@pytest.mark.parametrize("style", ["cyber", "galaxy", "solar", "classic"])
def test_all_metal_styles_keep_the_large_graph_canvas_path_cheap(style: str) -> None:
    """Material richness must not turn into a per-node shader workload above the cutoff."""
    report = _run_engine(
        GLOW_CANVAS_STUB
        + f"""
        const api = G.create(el, {{ reducedMotion: () => true }});
        api.setStyle('{style}');
        api.setData(chain(600));
        emit(paintNodes());
        """
    )
    assert report["fills"] > 0
    assert report["gradients"] == 0, f"{style} creates per-node gradients in a large graph"
    assert report["blurs"] == 0, f"{style} creates per-node blur in a large graph"


def test_legacy_classic_canvas_uses_the_same_nonwhite_material_profiles_as_ledger() -> None:
    """Classic's no-flag renderer is distinct from Ledger's engine and must not drift.

    The user can switch between Ledger and `/classic`, while Classic also retains a direct
    force-graph path for installations that do not opt into the newer engine. Both copies need
    the material profile rather than Classic silently returning to white-centred flat discs.
    """
    def material_block(path: Path) -> str:
        source = path.read_text(encoding="utf-8")
        start = source.index("function graphRgb(")
        return source[start:source.index("function graphApplyStyleChrome()", start)]

    static = material_block(DASHBOARD)
    classic = material_block(CLASSIC_DASHBOARD)
    assert static == classic, "the classic dashboard material painter drifted from its fallback"
    assert "function graphMaterialProfile(style,col)" in classic
    assert "function graphPaintMaterialSurface(" in classic
    assert "function graphMaterialTier(" in classic
    assert "function graphMaterialSprite(" in classic
    assert "graphMaterialProfile('cyber',col)" in classic
    assert "graphMaterialProfile('galaxy',col)" in classic
    assert "graphMaterialProfile('solar'" in classic
    assert "graphMaterialProfile('classic',col)" in classic
    assert "GRAPH_MATERIAL_CACHE_LIMIT=192" in classic
    assert "ctx.drawImage(sprite.canvas" in classic
    assert "#eafcff" not in classic
    assert "rgba(255,255,255" not in classic
    assert "graphIridescent(" not in classic
    for marker in (
        "family:'iridescent-pvd'",
        "family:'anodized-alloy'",
        "family:'brushed-copper'",
        "family:'satin-gunmetal'",
    ):
        assert marker in classic
        assert marker.replace(":'", ": '") in ASSET.read_text(encoding="utf-8")
    # The fallback selects the gradient-free signature recipe before building/painting a
    # sprite, so hundreds of nodes keep their material identity without per-node shaders.
    paint = classic[
        classic.index("function graphPaintMaterialSurface("):
        classic.index("function graphStyleBackground(")
    ]
    assert "graphMaterialTier(screenRadius,large)" in paint
    assert "paintDirect&&tier==='full'&&screenRadius>GRAPH_MATERIAL_RADIUS.full" in paint
    assert "directMaterial=node.id===GHILITE||node.rank===0" in classic
    full_classic = CLASSIC_DASHBOARD.read_text(encoding="utf-8")
    style_node = full_classic[full_classic.index("function graphStyleNode("):full_classic.index("function graphApplyStyleChrome()")]
    assert "graphPaintMaterialSurface(ctx,node.x,node.y,r,scale,profile,GPERF.large,directMaterial)" in style_node
    assert "graphPaintMaterialSurface(ctx,node.x,node.y,r,scale,profile,GPERF.large)" not in style_node
    assert classic.count("if(tier==='signature')") >= 4


def test_legacy_node_geometry_is_bounded_like_ledger_for_all_styles() -> None:
    """Classic must not resurrect the degree-squared visual blow-up behind the style switch.

    The material painter is shared across four styles, so a geometry regression here affects
    every theme even when the newer Ledger engine is correct.  Keep the two legacy copies in
    lockstep and pin the compact radius contract: normalized degree emphasis, a 0.8 minimum,
    and a size-slider-relative 1.1 maximum.
    """
    classic = CLASSIC_DASHBOARD.read_text(encoding="utf-8")
    static = DASHBOARD.read_text(encoding="utf-8")
    helper_start = classic.index("function graphNodeRadius(")
    helper_end = classic.index("const ETYPE_TOKEN", helper_start)
    assert static[static.index("function graphNodeRadius("):static.index("const ETYPE_TOKEN", static.index("function graphNodeRadius("))] == classic[helper_start:helper_end]
    assert "const maxDegree=Math.max(1,...nodes.map(node=>node.degree||0));" in classic
    assert "graphNodeRadius(node,window.GSET.size,(node.degree||0)/maxDegree)" in classic
    assert "return Math.max(.8,Math.min(size*1.1,radius));" in classic
    assert "Math.sqrt(node.val)" not in classic
    assert "Math.sqrt(node.val)" not in static


def test_classic_graph_overview_uses_ledger_scope_and_limit() -> None:
    """Classic and Ledger must start from the same responsive connected graph.

    Classic used to omit both query parameters, so the backend returned its default 2,000
    entities and the legacy canvas rendered a fundamentally different graph from Ledger's
    320-node connected overview.  Keep the full-graph control explicit while pinning the
    default request to Ledger's contract in both shipped dashboard copies.
    """
    for path in (DASHBOARD, CLASSIC_DASHBOARD):
        source = path.read_text(encoding="utf-8")
        load = source[source.index("async function loadLegacyGraph("):source.index("function graphUpdateAllNodesControl(")]
        assert "showUnlinked=GRAPH_FULL||!!document.getElementById('graph-show-iso').checked" in load
        assert "graphLimit=GRAPH_FULL?20000:320" in load
        assert "graphScope=GRAPH_FULL?'&full=true':(showUnlinked?'':'&connected_only=true')" in load
        assert "+'&limit='+graphLimit+graphScope" in load


def _community_palettes(source: str) -> dict:
    """Parse a ``COMMUNITY_PALS`` literal out of either renderer."""
    # Anchor on the declaration: both files also name the table in prose comments.
    match = re.search(r"COMMUNITY_PALS\s*=\s*\{", source)
    assert match is not None, "COMMUNITY_PALS is not declared here"
    block = source[match.end():source.index("};", match.end())]
    return {
        name: re.findall(r"#[0-9a-fA-F]{3,8}", body)
        for name, body in re.findall(r"(\w+)\s*:\s*\[([^\]]*)\]", block)
    }


def test_community_colours_match_the_dashboard_and_the_legend_swatches() -> None:
    """The cluster legend is painted from CSS, so palette *order* is a contract, not a taste.

    ``graphRenderLegend`` sorts communities by size and gives the largest a
    ``.graph-cluster-0`` swatch, while the canvas colours that same community with palette slot
    0.  The swatch colours live in ``dashboard.css`` and encode the Cyber palette — the default
    style — so a renderer whose slot 0 is a different colour makes the legend describe cluster 1
    with cluster 2's colour, on the default style, for every workspace.
    """
    engine = _community_palettes(ASSET.read_text(encoding="utf-8"))
    classic = _community_palettes(DASHBOARD.read_text(encoding="utf-8"))
    assert engine, "COMMUNITY_PALS could not be parsed out of the engine"
    assert engine == classic, "the opt-in renderer paints communities a different colour"

    swatches = dict(
        re.findall(r"\.graph-cluster-(\d+)\{background:(#[0-9a-fA-F]{3,8})\}",
                   CSS.read_text(encoding="utf-8"))
    )
    assert swatches, "the cluster legend swatches are missing from the stylesheet"
    for index, colour in sorted(swatches.items()):
        assert engine["cyber"][int(index)].lower() == colour.lower(), (
            f"legend swatch {index} does not match the canvas colour for that cluster"
        )


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


def test_manual_drag_controller_detaches_with_the_graph() -> None:
    """Reopening Ledger must not leave stale pointer controllers on the shared pane."""
    source = ASSET.read_text(encoding="utf-8")
    assert "let detachManualDrag = null;" in source
    assert "el.addEventListener('pointerdown', beginManualDrag, true);" in source
    assert "el.removeEventListener('pointerdown', beginManualDrag, true);" in source
    assert "window.removeEventListener('pointermove', moveManualDrag, true);" in source
    assert "event.type !== 'pointercancel'" in source
    direct_click = source[source.index("} else if (event.type !== 'pointercancel') {"):]
    direct_click = direct_click[:direct_click.index("      };", 1)]
    assert direct_click.index("handleNodeClick(current.node);") < direct_click.index("suppressNodeClick();")
    move = source[source.index("const moveManualDrag = event => {"):]
    move = move[:move.index("      const beginManualDrag", 1)]
    assert "if (!manualDrag.dragged)" in move
    assert move.index("if (Math.hypot(dx, dy) < 3)") < move.index("const node = manualDrag.node;")
    teardown = source[source.index("api.destroy = () => {"):]
    assert "if (detachManualDrag) { detachManualDrag(); detachManualDrag = null; }" in teardown


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
