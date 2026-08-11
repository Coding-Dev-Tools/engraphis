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
import math
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
PRIMARY_CSS = ROOT / "engraphis" / "dashboard_assets" / "ledger.css"
PRIMARY_VENDOR = ROOT / "engraphis" / "dashboard_assets" / "vendor" / "force-graph.min.js"

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
globalThis.cancelAnimationFrame = () => {};
const store = {}, calls = {}, invocations = {};
const fg = new Proxy({}, {
  get: (_target, prop) => prop === 'screen2GraphCoords' && typeof store.screen2GraphCoords === 'function'
    ? store.screen2GraphCoords
    : prop === 'd3Force' ? (function(name, force) {
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
    assert any(
        re.fullmatch(r"/static/dashboard\.js\?v=[A-Za-z0-9._-]+", item)
        for item in eager
    )
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
    assert re.search(
        r"script\.src='/static/vendor/force-graph\.min\.js\?v=[A-Za-z0-9._-]+'",
        source,
    )
    assert re.search(
        r"script\.src='/v2-assets/engraphis-graph\.js\?v=[A-Za-z0-9._-]+'",
        source,
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
        "/v2-assets/engraphis-graph.js?v=20260810-galaxy-explicit-reheat"
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
        "/v2-assets/engraphis-graph.js?v=20260810-galaxy-explicit-reheat"
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
def test_galaxy_evidence_mass_is_sanitized_and_authoritative_for_radius() -> None:
    report = _run_node(
        """
        const nodes = [
          { id: 'fallback', degree: 5 },
          { id: 'light', degree: 1, gravity_mass: 2, visual_radius: 9 },
          { id: 'heavy', degree: 2, gravity_mass: 8, visual_radius: 3 },
          { id: 'ghost', degree: 99, gravity_mass: 0, visual_radius: 12, ghost: true },
        ];
        I.sanitizeEvidenceMetrics(nodes, 5);
        const ordered = nodes.filter(n => !n.ghost).sort((a, b) => a.gravity_mass - b.gravity_mass);
        const clusterSmall = I.evidenceNodeRadius({ cluster: true, gravity_mass: 4 }, 3);
        const clusterLarge = I.evidenceNodeRadius({ cluster: true, gravity_mass: 16 }, 3);
        emit({
          nodes,
          monotonic: ordered.every((n, i) => !i || n.visual_radius >= ordered[i - 1].visual_radius),
          scaled: I.evidenceNodeRadius(nodes[0], 6) / I.evidenceNodeRadius(nodes[0], 3),
          clusterRatio: clusterLarge / clusterSmall,
          fallbackAgain: I.fallbackGravityMass(5, 5),
        });
        """
    )
    by_id = {node["id"]: node for node in report["nodes"]}
    assert by_id["fallback"]["gravity_mass"] == report["fallbackAgain"] == 16
    def radius(mass: float) -> float:
        return 1.5 + 2.0 * mass ** (2.0 / 3.0)
    assert by_id["fallback"]["visual_radius"] == pytest.approx(radius(16))
    assert by_id["light"]["visual_radius"] == pytest.approx(radius(2))
    assert by_id["heavy"]["visual_radius"] == pytest.approx(radius(8))
    assert by_id["ghost"]["gravity_mass"] == 0
    assert report["monotonic"] is True
    assert report["scaled"] == pytest.approx(2)
    assert report["clusterRatio"] == pytest.approx(radius(16) / radius(4))


@requires_node
def test_global_black_hole_radius_is_exactly_double_at_every_node_size_endpoint() -> None:
    report = _run_node(
        """
        const ordinary = { id: 'ordinary', gravity_mass: 8, visual_radius: 9 };
        const community = { ...ordinary, id: 'community', anchor_role: 'community' };
        const global = { ...ordinary, id: 'global', anchor_role: 'global' };
        const sizes = [1, 3, 12];
        emit({ sizes: sizes.map(size => ({
          size,
          ordinary: I.evidenceNodeRadius(ordinary, size),
          community: I.evidenceNodeRadius(community, size),
          global: I.evidenceNodeRadius(global, size),
        })), masses: [ordinary.gravity_mass, community.gravity_mass, global.gravity_mass] });
        """
    )
    for sample in report["sizes"]:
        assert sample["community"] == pytest.approx(sample["ordinary"])
        assert sample["global"] == pytest.approx(sample["ordinary"] * 2)
    assert report["masses"] == [8, 8, 8]
    source = ASSET.read_text(encoding="utf-8")
    assignment = source[source.index("data.nodes.forEach(n => {"):
                        source.index("const labelCap", source.index("data.nodes.forEach(n => {"))]
    assert "n.radius = galaxyMode" in assignment
    adornment = source[source.index("function paintGalaxyAnchorAdornment"):
                       source.index("function styleNode", source.index("function paintGalaxyAnchorAdornment"))]
    assert "finitePositive(node.radius" in adornment


@requires_node
def test_softened_galaxy_gravity_obeys_mass_distance_and_momentum_invariants() -> None:
    report = _run_node(
        """
        const run = (distance, sourceMass, sourceCommunity = 'system') => {
          const nodes = [
            { id: 'target', x: 0, y: 0, vx: 0, vy: 0, gravity_mass: 2, community_id: 'system' },
            { id: 'source', x: distance, y: 0, vx: 0, vy: 0, gravity_mass: sourceMass, community_id: sourceCommunity },
          ];
          I.applyGalaxyGravity(nodes, { gravity: 4, softening: 0.0001, alpha: 1 });
          return nodes;
        };
        const near = run(10, 4), far = run(20, 4), doubled = run(10, 8);
        const coincident = [
          { id: 'a', x: 0, y: 0, gravity_mass: 2, community_id: 'same' },
          { id: 'b', x: 0, y: 0, gravity_mass: 3, community_id: 'same' },
        ];
        I.applyGalaxyGravity(coincident, { gravity: 4, softening: 8, alpha: 1 });
        const isolated = run(10, 4, 'other');
        emit({
          inverseSquare: far[0].vx / near[0].vx,
          linearMass: doubled[0].vx / near[0].vx,
          momentum: 2 * near[0].vx + 4 * near[1].vx,
          coincidentFinite: coincident.every(n => Number.isFinite(n.vx) && Number.isFinite(n.vy)),
          isolated: isolated.map(n => [n.vx, n.vy]),
        });
        """
    )
    assert report["inverseSquare"] == pytest.approx(0.25, rel=2e-4)
    assert report["linearMass"] == pytest.approx(2)
    assert report["momentum"] == pytest.approx(0, abs=1e-12)
    assert report["coincidentFinite"] is True
    assert report["isolated"] == [[0, 0], [0, 0]]


@requires_node
def test_galaxy_central_well_contracts_systems_monotonically_and_preserves_momentum() -> None:
    report = _run_node(
        """
        const fixture = () => [
          { id: 'l1', x: -170, y: 0, vx: 0, vy: 0, gravity_mass: 2, community_id: 'left' },
          { id: 'l2', x: -150, y: 0, vx: 0, vy: 0, gravity_mass: 3, community_id: 'left' },
          { id: 'right', x: 180, y: 0, vx: 0, vy: 0, gravity_mass: 5, community_id: 'right' },
          { id: 'top', x: 0, y: 210, vx: 0, vy: 0, gravity_mass: 4, community_id: 'top' },
        ];
        const distance = nodes => {
          const centers = I.communityCenters(nodes);
          const a = centers.get('left'), b = centers.get('right'), c = centers.get('top');
          return Math.hypot(a.x - b.x, a.y - b.y)
            + Math.hypot(a.x - c.x, a.y - c.y)
            + Math.hypot(b.x - c.x, b.y - c.y);
        };
        const advance = gravity => {
          const nodes = fixture();
          I.applyGalaxyCentralGravity(nodes, {
            gravity, softening: 40, alpha: 1, accelerationCap: 1000,
          });
          nodes.forEach(node => { node.x += node.vx; node.y += node.vy; });
          return { nodes, span: distance(nodes) };
        };
        const initial = distance(fixture()), low = advance(24), high = advance(72);
        const coincident = [
          { id: 'a', x: 0, y: 0, gravity_mass: 2, community_id: 'a' },
          { id: 'b', x: 0, y: 0, gravity_mass: 3, community_id: 'b' },
        ];
        const stats = I.applyGalaxyCentralGravity(coincident, {
          gravity: 100, softening: 40, alpha: 1,
        });
        const capped = [
          { id: 'light', x: -1, y: 0, vx: 0, vy: 0, gravity_mass: 2, community_id: 'light' },
          { id: 'heavy', x: 1, y: 0, vx: 0, vy: 0, gravity_mass: 8, community_id: 'heavy' },
        ];
        const cappedStats = I.applyGalaxyCentralGravity(capped, {
          gravity: 10000, softening: 0.1, alpha: 1, accelerationCap: 0.4,
        });
        emit({
          initial, low: low.span, high: high.span,
          momentum: [
            high.nodes.reduce((sum, node) => sum + node.gravity_mass * node.vx, 0),
            high.nodes.reduce((sum, node) => sum + node.gravity_mass * node.vy, 0),
          ],
          rigidSystem: [
            high.nodes[0].vx - high.nodes[1].vx,
            high.nodes[0].vy - high.nodes[1].vy,
          ],
          coincidentFinite: coincident.every(node => Number.isFinite(node.vx) && Number.isFinite(node.vy)),
          systems: stats.systems,
          capped: capped.map(node => node.vx),
          cappedMomentum: capped.reduce(
            (sum, node) => sum + node.gravity_mass * node.vx, 0
          ),
          cappedPairs: cappedStats.applied,
        });
        """
    )
    assert report["initial"] > report["low"] > report["high"]
    assert report["momentum"] == pytest.approx([0, 0], abs=1e-12)
    assert report["rigidSystem"] == pytest.approx([0, 0], abs=1e-12)
    assert report["coincidentFinite"] is True
    assert report["systems"] == 2
    assert report["capped"][0] == pytest.approx(0.4)
    assert report["capped"][1] == pytest.approx(-0.1)
    assert report["cappedMomentum"] == pytest.approx(0, abs=1e-12)
    assert report["cappedPairs"] == 1
    source = ASSET.read_text(encoding="utf-8")
    assert "function galaxyGravityConstant(setting)" in source
    assert "function galaxySmoothstep(value)" in source
    assert "const boost = 1 + 0.25 * galaxySmoothstep(value / 48)" in source
    assert "function applyGalaxyCentralGravity(nodes, options)" in source
    assert "GALAXY_CENTER_SCALE" not in source
    central = source[source.index("function applyGalaxyCentralGravity"):
                     source.index("function applyCommunityBridgeGravity")]
    assert "driftX" not in central


@requires_node
def test_unlinked_solar_systems_exert_bounded_mass_aware_near_field_gravity() -> None:
    report = _run_node(
        """
        const fixture = distance => [
          { id: 'black-hole', x: 0, y: 0, vx: 0, vy: 0, gravity_mass: 50,
            community_id: 'core', anchor_role: 'global' },
          { id: 'left-star', x: 100, y: 0, vx: 0, vy: 0, gravity_mass: 8,
            community_id: 'left' },
          { id: 'left-planet', x: 104, y: 2, vx: 0, vy: 0, gravity_mass: 2,
            community_id: 'left' },
          { id: 'right-star', x: 100 + distance, y: 0, vx: 0, vy: 0, gravity_mass: 4,
            community_id: 'right' },
        ];
        const run = distance => {
          const nodes = fixture(distance);
          const stats = I.applyGalaxyMutualSystemGravity(nodes, {
            gravity: 48, strengthFraction: 0.12, softening: 1,
            accelerationCap: 0, exactLimit: 64,
          });
          return { nodes, stats };
        };
        const near = run(40), far = run(100);
        const large = [{ id: 'core', x: 0, y: 0, vx: 0, vy: 0, gravity_mass: 100,
          community_id: 'core', anchor_role: 'global' }];
        for (let index = 0; index < 100; index++) large.push({
          id: 's' + index,
          x: 100 + (index % 10) * 20, y: -90 + Math.floor(index / 10) * 20,
          gravity_mass: 1 + index % 7, community_id: 'system-' + index,
        });
        const largeStats = I.applyGalaxyMutualSystemGravity(large, {
          gravity: 48, strengthFraction: 0.12, softening: 40,
          accelerationCap: 10, exactLimit: 64, theta: 0.85,
        });
        emit({
          nearAcceleration: Math.hypot(near.nodes[1].vx, near.nodes[1].vy),
          farAcceleration: Math.hypot(far.nodes[1].vx, far.nodes[1].vy),
          blackHole: [near.nodes[0].vx, near.nodes[0].vy],
          rigid: [near.nodes[1].vx - near.nodes[2].vx,
            near.nodes[1].vy - near.nodes[2].vy],
          momentum: near.nodes.slice(1).reduce((sum, node) => ({
            x: sum.x + node.gravity_mass * node.vx,
            y: sum.y + node.gravity_mass * node.vy,
          }), { x: 0, y: 0 }),
          nearStats: near.stats,
          largeStats,
          finite: large.every(node => Number.isFinite(node.vx) && Number.isFinite(node.vy)),
        });
        """
    )
    assert report["nearAcceleration"] > report["farAcceleration"] > 0
    assert report["blackHole"] == [0, 0]
    assert report["rigid"] == pytest.approx([0, 0], abs=1e-12)
    assert [report["momentum"]["x"], report["momentum"]["y"]] == pytest.approx(
        [0, 0], abs=1e-12
    )
    assert report["nearStats"]["systems"] == 2
    assert report["nearStats"]["interactions"] == 1
    assert report["largeStats"]["approximations"] > 0
    assert report["largeStats"]["traversals"] < 100 * 100
    assert report["finite"] is True


@requires_node
def test_gravity_slider_response_has_exact_endpoints_and_scales_every_physics_layer() -> None:
    report = _run_node(
        """
        const ratio = (high, low) => high / low;
        const pairAcceleration = gravity => {
          const nodes = [
            { id: 'a', community_id: 'one', gravity_mass: 4, x: 0, y: 0, vx: 0, vy: 0 },
            { id: 'b', community_id: 'one', gravity_mass: 1, x: 30, y: 0, vx: 0, vy: 0 },
          ];
          I.applyGalaxyGravity(nodes, { gravity, softening: 12, alpha: 1 });
          return Math.abs(nodes[0].vx);
        };
        const haloAcceleration = gravity => {
          const nodes = [
            { id: 'star', anchor_role: 'community', community_id: 'one',
              gravity_mass: 4, x: 0, y: 0, vx: 0, vy: 0 },
            { id: 'planet', community_id: 'one', gravity_mass: 1,
              x: 30, y: 0, vx: 0, vy: 0 },
          ];
          I.applyGalaxySystemHaloGravity(nodes, {
            gravity, softening: 12, smoothFraction: 0.85, accelerationCap: 100,
          });
          return Math.abs(nodes[1].vx - nodes[0].vx);
        };
        const centralAcceleration = gravity => {
          const nodes = [
            { id: 'black-hole', anchor_role: 'global', community_id: 'core',
              gravity_mass: 8, x: 0, y: 0 },
            { id: 'system', community_id: 'outer', gravity_mass: 2, x: 120, y: 0 },
          ];
          return Math.abs(I.galaxyBlackHoleField(nodes, {
            gravity, softening: 40, accelerationCap: 100,
          }).systems[0].ax);
        };
        const bridgeAcceleration = gravity => {
          const nodes = [
            { id: 'a', community_id: 'left', gravity_mass: 4, x: 0, y: 0, vx: 0, vy: 0 },
            { id: 'b', community_id: 'right', gravity_mass: 1, x: 80, y: 0, vx: 0, vy: 0 },
          ];
          I.applyCommunityBridgeGravity(nodes, [{
            source_community: 'left', target_community: 'right', physics_strength: 0.8,
          }], { gravity, softening: 30, alpha: 1 });
          return Math.abs(nodes[0].vx);
        };
        const localSeedSpeedSquared = gravity => {
          const nodes = [
            { id: 'star', anchor_role: 'community', community_id: 'one',
              gravity_mass: 4, x: 0, y: 0, vx: 0, vy: 0 },
            { id: 'planet', community_id: 'one', gravity_mass: 1,
              x: 30, y: 0, vx: 0, vy: 0 },
          ];
          I.seedGalaxyOrbits(nodes, 9, gravity, 12, false, 0.15);
          const speed = Math.hypot(nodes[1].vx - nodes[0].vx,
            nodes[1].vy - nodes[0].vy);
          return speed * speed;
        };
        const systemSeedSpeedSquared = gravity => {
          const nodes = [
            { id: 'black-hole', anchor_role: 'global', community_id: 'core',
              gravity_mass: 8, x: 0, y: 0, vx: 0, vy: 0 },
            { id: 'system', anchor_role: 'community', community_id: 'outer',
              gravity_mass: 2, x: 120, y: 0, vx: 0, vy: 0 },
          ];
          I.seedGalaxySystemOrbits(nodes, 9, gravity, 40, false);
          const speed = Math.hypot(nodes[1].vx - nodes[0].vx,
            nodes[1].vy - nodes[0].vy);
          return speed * speed;
        };
        const settings = [0, 1, 12, 24, 48, 72, 100, 200, 400];
        const response = settings.map(I.galaxyGravityConstant);
        const legacy = setting => setting * (772 + 11 * setting) / 2600;
            // This is the release-stable calibration restored after the unsafe speed-up.
        const priorCalibration = setting => {
          const value = Math.max(0, Math.min(400, Number(setting) || 0));
          const base = value * (772 + 11 * value) / 2600;
          const smoothstep = raw => {
            const t = Math.max(0, Math.min(1, raw));
            return t * t * (3 - 2 * t);
          };
          const boost = 1 + 0.25 * smoothstep(value / 48)
            + 0.25 * smoothstep((value - 48) / 52);
          return base * boost * 4;
        };
        const fullRange = Array.from({ length: 401 }, (_, setting) => setting);
        const centralCap = (gravity, explicit) => {
          const nodes = [
            { id: 'black-hole', anchor_role: 'global', community_id: 'core',
              gravity_mass: 1000, x: 0, y: 0 },
            { id: 'near', community_id: 'outer', gravity_mass: 1000, x: 1, y: 0 },
          ];
          const options = { gravity, softening: 0.1 };
          if (explicit !== undefined) options.accelerationCap = explicit;
          const item = I.galaxyBlackHoleField(nodes, options).systems[0];
          return Math.hypot(item.ax, item.ay);
        };
        const compatibilityCentralCap = gravity => {
          const nodes = [
            { id: 'left', community_id: 'left', gravity_mass: 1000,
              x: -0.5, y: 0, vx: 0, vy: 0 },
            { id: 'right', community_id: 'right', gravity_mass: 1000,
              x: 0.5, y: 0, vx: 0, vy: 0 },
          ];
          I.applyGalaxyCentralGravity(nodes, { gravity, softening: 0.1 });
          return Math.max(...nodes.map(node => Math.hypot(node.vx, node.vy)));
        };
        const localHaloCap = gravity => {
          const nodes = [
            { id: 'star', anchor_role: 'community', community_id: 'one',
              gravity_mass: 1000, x: 0, y: 0, vx: 0, vy: 0 },
            { id: 'near', community_id: 'one', gravity_mass: 1000,
              x: 0.01, y: 0, vx: 0, vy: 0 },
          ];
          I.applyGalaxySystemHaloGravity(nodes, {
            gravity, softening: 0.1, smoothFraction: 0.85,
          });
          return Math.max(...nodes.map(node => Math.hypot(node.vx, node.vy)));
        };
        emit({
          response,
          endpoints: [I.galaxyGravityConstant(48), I.galaxyGravityConstant(100),
            I.galaxyGravityConstant(200), I.galaxyGravityConstant(400)],
          split: {
            blackHole: [I.galaxyBlackHoleGravityConstant(48),
              I.galaxyBlackHoleGravityConstant(100),
              I.galaxyBlackHoleGravityConstant(200),
              I.galaxyBlackHoleGravityConstant(400)],
            local: [I.galaxyLocalGravityConstant(48),
              I.galaxyLocalGravityConstant(100),
              I.galaxyLocalGravityConstant(200),
              I.galaxyLocalGravityConstant(400)],
          },
          clamps: [I.galaxyGravityConstant(-1), I.galaxyGravityConstant(401),
            I.galaxyGravityConstant(Infinity), I.galaxyGravityConstant(NaN)],
          layoutCompactness: [0, 48, 200, 400].map(I.galaxyLayoutCompactness),
          caps: [centralCap(48), centralCap(100), centralCap(100, 1)],
          compatibilityCaps: [compatibilityCentralCap(48), compatibilityCentralCap(100)],
          localCaps: [localHaloCap(48), localHaloCap(100)],
          neverWeaker: fullRange.every(setting =>
            I.galaxyGravityConstant(setting) >= legacy(setting) - 1e-12),
          matchesStableCalibration: fullRange.every(setting => Math.abs(
            I.galaxyGravityConstant(setting) - priorCalibration(setting)
          ) <= 1e-10),
          priorEndpoints: [48, 100, 200, 400].map(priorCalibration),
          fullRangeMonotone: fullRange.slice(1).every((setting, index) =>
            I.galaxyGravityConstant(setting) > I.galaxyGravityConstant(index)),
          ratios: {
            pair: ratio(pairAcceleration(100), pairAcceleration(48)),
            halo: ratio(haloAcceleration(100), haloAcceleration(48)),
            central: ratio(centralAcceleration(100), centralAcceleration(48)),
            bridge: ratio(bridgeAcceleration(100), bridgeAcceleration(48)),
            localSeed: ratio(localSeedSpeedSquared(100), localSeedSpeedSquared(48)),
            systemSeed: ratio(systemSeedSpeedSquared(100), systemSeedSpeedSquared(48)),
          },
        });
        """
    )
    assert report["endpoints"][:2] == [120, 432]
    assert report["endpoints"][2] == pytest.approx(1371.6923076923076)
    assert report["endpoints"][3] == pytest.approx(4774.153846153846)
    assert report["split"]["blackHole"] == pytest.approx(
        [240, 864, 2743.3846153846152, 9548.307692307691]
    )
    assert report["split"]["local"] == pytest.approx(
        [120, 432, 1371.6923076923076, 4774.153846153846]
    )
    assert report["split"]["local"] == [
        value * 0.5 for value in report["split"]["blackHole"]
    ]
    assert report["clamps"] == pytest.approx([0, 4774.153846153846, 0, 0])
    assert report["layoutCompactness"] == pytest.approx([1.75, 1.5616, 0.965, 0.18])
    assert all(
        right < left
        for left, right in zip(report["layoutCompactness"], report["layoutCompactness"][1:])
    )
    assert report["caps"] == pytest.approx([25, 90, 1])
    assert report["compatibilityCaps"] == pytest.approx([25, 90])
    assert report["localCaps"] == pytest.approx([12.5, 45])
    assert report["response"][0] == 0
    assert all(
        right > left
        for left, right in zip(report["response"], report["response"][1:])
    )
    assert report["neverWeaker"] is True
    assert report["matchesStableCalibration"] is True
    assert report["endpoints"] == pytest.approx(report["priorEndpoints"])
    assert report["fullRangeMonotone"] is True
    assert all(value == pytest.approx(3.6, rel=1e-12) for value in report["ratios"].values())
    source = ASSET.read_text(encoding="utf-8")
    assert "const GALAXY_FAR_FIELD_ENVELOPE_SCALE = 1.75;" in source
    assert "const GALAXY_GRAVITY_MAXIMUM = 400;" in source


@requires_node
def test_black_hole_field_is_twice_local_gravity_and_uses_only_anchor_mass() -> None:
    report = _run_node(
        """
        const local = [
          { id: 'star', community_id: 'solar', gravity_mass: 8,
            x: 0, y: 0, vx: 0, vy: 0 },
          { id: 'planet', community_id: 'solar', gravity_mass: 1,
            x: 120, y: 0, vx: 0, vy: 0 },
        ];
        I.applyGalaxyGravity(local, { gravity: 48, softening: 40, alpha: 1 });
        const central = [
          { id: 'black-hole', anchor_role: 'global', community_id: 'core',
            gravity_mass: 8, x: 0, y: 0 },
          { id: 'outer', community_id: 'outer', gravity_mass: 1, x: 120, y: 0 },
        ];
        const centralField = I.galaxyBlackHoleField(central, {
          gravity: 48, softening: 40, haloScale: 1e9, accelerationCap: 1e9,
        });
        const withBulge = I.galaxyBlackHoleField([
          { id: 'black-hole', anchor_role: 'global', community_id: 'core',
            gravity_mass: 8, x: 0, y: 0 },
          { id: 'bulge', community_id: 'core', gravity_mass: 100, x: 5, y: 0 },
          { id: 'outer', community_id: 'outer', gravity_mass: 1, x: 120, y: 0 },
        ], { gravity: 48, softening: 40, accelerationCap: 1e9 });
        emit({
          constants: [I.galaxyBlackHoleGravityConstant(48),
            I.galaxyLocalGravityConstant(48)],
          accelerationRatio: Math.abs(centralField.systems[0].ax / local[1].vx),
          masses: [withBulge.coreMass, withBulge.haloMass, withBulge.totalMass],
        });
        """
    )
    assert report["constants"] == [240, 120]
    assert report["accelerationRatio"] == pytest.approx(2, rel=1e-12)
    assert report["masses"] == [8, 101, 109]


@requires_node
def test_gravity_zero_retains_only_the_independent_stellar_floor() -> None:
    """Gravity zero stops the black-hole well without freezing declared solar systems."""
    report = _run_node(
        """
        const nodes = [
          { id: 'black-hole', anchor_role: 'global', community_id: 'core',
            system_anchor_id: 'black-hole', orbit_tier: 0, gravity_mass: 20, radius: 10,
            x: 0, y: 0, vx: 0, vy: 0 },
          { id: 'core-planet', community_id: 'core', system_anchor_id: 'black-hole',
            orbit_tier: 1, gravity_mass: 1, radius: 3,
            x: 45, y: 0, vx: 0, vy: 0 },
          { id: 'star', anchor_role: 'community', community_id: 'solar',
            system_anchor_id: 'star', orbit_tier: 0, gravity_mass: 8, radius: 5,
            x: 120, y: 0, vx: 0, vy: 0 },
          { id: 'planet', community_id: 'solar', system_anchor_id: 'star',
            orbit_tier: 1, gravity_mass: 1, radius: 3,
            x: 150, y: 0, vx: 0, vy: 0 },
        ];
        I.seedGalaxyOrbits(nodes, 404, 0, 38.4, false);
        I.seedGalaxySystemOrbits(nodes, 404, 0, 48, false);
        const [blackHole, corePlanet, star, planet] = nodes;
        const systemCenter = () => ({
          x: (star.x * 8 + planet.x) / 9,
          y: (star.y * 8 + planet.y) / 9,
          vx: (star.vx * 8 + planet.vx) / 9,
          vy: (star.vy * 8 + planet.vy) / 9,
        });
        const relative = () => ({
          x: planet.x - star.x, y: planet.y - star.y,
          vx: planet.vx - star.vx, vy: planet.vy - star.vy,
        });
        const before = { center: systemCenter(), relative: relative(),
          blackHole: [blackHole.x, blackHole.y, blackHole.vx, blackHole.vy],
          corePlanet: [corePlanet.x, corePlanet.y, corePlanet.vx, corePlanet.vy] };
        let previousAngle = Math.atan2(before.relative.y, before.relative.x);
        let angularTravel = 0, minimumRadius = Infinity, maximumRadius = 0, tick;
        for (let step = 0; step < 180; step += 1) {
          tick = I.integrateGalaxyLeapfrog(nodes, [], [], {
            gravity: 0, softening: 38.4, centralSoftening: 48,
            includeMutualSystems: false, includeRelations: false,
            includeOrbitalSeparation: false, skipSystemAnchorPairs: true,
            systemAnchorExclusionPadding: 1.5, systemAnchorRepulsionAcceleration: 0,
            includeBlackHoleExclusion: true, blackHoleExclusionPadding: 2.5,
            includeFarFieldConfinement: false, inwardConvergence: false,
            localRelativeSpeedLimit: 48, timestep: 0.032, wallClockSeconds: 1 / 30,
            velocityDecay: 0.00005, speedLimit: 48, includeCollisions: false,
          });
          const phase = relative(), radius = Math.hypot(phase.x, phase.y);
          const angle = Math.atan2(phase.y, phase.x);
          angularTravel += Math.atan2(Math.sin(angle - previousAngle),
            Math.cos(angle - previousAngle));
          previousAngle = angle;
          minimumRadius = Math.min(minimumRadius, radius);
          maximumRadius = Math.max(maximumRadius, radius);
        }
        emit({
          floorSetting: I.galaxyStellarGravityFloorSetting,
          mappedSettings: [0, 47, 48, 100, Infinity, NaN]
            .map(I.galaxyStellarGravitySetting),
          constants: {
            blackHole: I.galaxyBlackHoleGravityConstant(0),
            compatibilityLocal: I.galaxyLocalGravityConstant(0),
            stellar: I.galaxyStellarGravityConstant(0),
            defaultStellar: I.galaxyStellarGravityConstant(48),
          },
          before, after: { center: systemCenter(), relative: relative(),
            blackHole: [blackHole.x, blackHole.y, blackHole.vx, blackHole.vy],
            corePlanet: [corePlanet.x, corePlanet.y, corePlanet.vx, corePlanet.vy] },
          angularTravel, minimumRadius, maximumRadius,
          telemetry: tick.systemGravity,
          finite: nodes.every(node => [node.x, node.y, node.vx, node.vy]
            .every(Number.isFinite)),
        });
        """
    )
    assert report["finite"] is True
    assert report["floorSetting"] == 48
    assert report["mappedSettings"] == [48, 48, 48, 100, 48, 48]
    assert report["constants"] == {
        "blackHole": 0,
        "compatibilityLocal": 0,
        "stellar": 750,
        "defaultStellar": 750,
    }
    before, after = report["before"], report["after"]
    assert math.hypot(before["relative"]["vx"], before["relative"]["vy"]) > 1
    assert before["relative"]["x"] * before["relative"]["vx"] \
        + before["relative"]["y"] * before["relative"]["vy"] == pytest.approx(0, abs=1e-10)
    assert abs(report["angularTravel"]) > 1
    assert report["minimumRadius"] > 28
    assert report["maximumRadius"] < 32
    assert after["center"] == pytest.approx(before["center"], abs=1e-9)
    assert after["blackHole"] == before["blackHole"] == [0, 0, 0, 0]
    assert after["corePlanet"] == pytest.approx(before["corePlanet"], abs=1e-12)
    assert report["telemetry"]["gravitySetting"] == 0
    assert report["telemetry"]["stellarGravityFloorSetting"] == 48
    assert report["telemetry"]["stellarGravity"] == 750
    assert report["telemetry"]["eligibleStellarAnchors"] == 1
    assert report["telemetry"]["fallbackAnchors"] == 0
    assert report["telemetry"]["globalAnchors"] == 1
    assert report["telemetry"]["stellarFloorActive"] is True


@requires_node
def test_core_pair_reduction_is_complementary_momentum_safe_and_seed_exact() -> None:
    report = _run_node(
        """
        const system = (prefix, community, role = 'community') => [
          { id: prefix + '-star', anchor_role: role, community_id: community,
            gravity_mass: 4, x: 0, y: 0, vx: 0, vy: 0 },
          { id: prefix + '-planet', community_id: community,
            gravity_mass: 1, x: 30, y: 0, vx: 0, vy: 0 },
        ];
        const regularPair = system('regular-pair', 'regular');
        const corePair = system('core-pair', 'core');
        const pairs = [...regularPair, ...corePair];
        I.applyGalaxyGravity(pairs, {
          effectiveGravity: I.galaxyGravityConstant(48),
          pairFraction: 0.15,
          corePairFraction: 0.1125,
          coreCommunity: 'core',
          softening: 12,
        });
        const pairAcceleration = [Math.abs(regularPair[0].vx), Math.abs(corePair[0].vx)];
        const pairMomentum = [regularPair, corePair].map(members => members.reduce(
          (sum, node) => sum + node.gravity_mass * node.vx, 0
        ));

        const regularHalo = system('regular-halo', 'regular');
        const coreHalo = system('core-halo', 'core');
        I.applyGalaxySystemHaloGravity([...regularHalo, ...coreHalo], {
          gravity: 48,
          smoothFraction: 0.85,
          coreSmoothFraction: 0.8875,
          coreCommunity: 'core',
          softening: 12,
          accelerationCap: 100,
        });
        const relativeX = members => members[1].vx - members[0].vx;
        const haloAcceleration = [Math.abs(relativeX(regularHalo)),
          Math.abs(relativeX(coreHalo))];
        const haloMomentum = [regularHalo, coreHalo].map(members => members.reduce(
          (sum, node) => sum + node.gravity_mass * node.vx, 0
        ));

        const regularCombined = system('regular-combined', 'regular');
        const coreCombined = system('core-combined', 'core');
        const combined = [...regularCombined, ...coreCombined];
        I.applyGalaxyGravity(combined, {
          effectiveGravity: I.galaxyGravityConstant(48), pairFraction: 0.15, corePairFraction: 0.1125,
          coreCommunity: 'core', softening: 12,
        });
        I.applyGalaxySystemHaloGravity(combined, {
          gravity: 48, smoothFraction: 0.85, coreSmoothFraction: 0.8875,
          coreCommunity: 'core', softening: 12, accelerationCap: 100,
        });

        const seededCore = system('seeded', 'core', 'global');
        I.seedGalaxyOrbits(seededCore, 17, 48, 12, false, 0.15, 0.75);
        const seededAcceleration = I.galaxyAccelerations(seededCore, [], [], {
          gravity: 48, softening: 12, central: false,
          localPairFraction: 0.15, corePairMultiplier: 0.75,
        });
        const relativeSpeed = Math.hypot(
          seededCore[1].vx - seededCore[0].vx,
          seededCore[1].vy - seededCore[0].vy
        );
        const radialAcceleration = -(
          seededAcceleration.get(seededCore[1]).ax
          - seededAcceleration.get(seededCore[0]).ax
        );

        const coincident = [
          { id: 'global', anchor_role: 'global', community_id: 'core',
            gravity_mass: 4, x: 0, y: 0, vx: 0, vy: 0 },
          { id: 'same', community_id: 'core', gravity_mass: 1,
            x: 0, y: 0, vx: 0, vy: 0 },
        ];
        const finiteAcceleration = I.galaxyAccelerations(coincident, [], [], {
          gravity: 100, softening: 0.1, central: false,
          localPairFraction: 0.15, corePairMultiplier: 0.75,
        });
        const halfStep = [{ id: 'half', community_id: 'single', gravity_mass: 1,
          x: 3, y: -2, vx: 2, vy: -4 }];
        const oldStep = halfStep.map(node => ({ ...node }));
        I.integrateGalaxyLeapfrog(halfStep, [], [], {
          gravity: 0, central: false, timestep: 0.021328125,
          velocityDecay: 0, speedLimit: 100, includeCollisions: false,
        });
        I.integrateGalaxyLeapfrog(oldStep, [], [], {
          gravity: 0, central: false, timestep: 0.03046875,
          velocityDecay: 0, speedLimit: 100, includeCollisions: false,
        });
        emit({
          pairAcceleration,
          pairMomentum,
          haloAcceleration,
          haloMomentum,
          combined: [Math.abs(relativeX(regularCombined)),
            Math.abs(relativeX(coreCombined))],
          seedLaw: [relativeSpeed * relativeSpeed / 30, radialAcceleration],
          driftRatio: [(halfStep[0].x - 3) / (oldStep[0].x - 3),
            (halfStep[0].y + 2) / (oldStep[0].y + 2)],
          finite: [...finiteAcceleration.values()].every(value =>
            Number.isFinite(value.ax) && Number.isFinite(value.ay)),
        });
        """
    )
    assert report["pairAcceleration"][1] / report["pairAcceleration"][0] == pytest.approx(0.75)
    assert report["haloAcceleration"][1] / report["haloAcceleration"][0] == pytest.approx(
        0.8875 / 0.85
    )
    assert report["combined"][1] == pytest.approx(report["combined"][0], rel=1e-12)
    assert report["pairMomentum"] == pytest.approx([0, 0], abs=1e-12)
    assert report["haloMomentum"] == pytest.approx([0, 0], abs=1e-12)
    assert report["seedLaw"][0] == pytest.approx(report["seedLaw"][1], rel=1e-12)
    assert report["driftRatio"] == pytest.approx([0.7, 0.7])
    assert report["finite"] is True
    assert "const GALAXY_MOTION_RATE = 0.68;" in ASSET.read_text(encoding="utf-8")
    assert "const GALAXY_FIXED_TIMESTEP = 0.032;" in ASSET.read_text(encoding="utf-8")


@requires_node
def test_legacy_system_halo_and_anchor_integrator_preserve_free_system_com() -> None:
    report = _run_node(
        """
        const free = [
          { id: 'star', system_anchor_id: 'star', anchor_role: 'community',
            community_id: 'free', gravity_mass: 8, x: 0, y: 0, vx: 0, vy: 0 },
          { id: 'inner', system_anchor_id: 'star', orbit_tier: 1,
            community_id: 'free', gravity_mass: 2, x: 16, y: 0, vx: 0, vy: 0 },
          { id: 'outer', system_anchor_id: 'star', orbit_tier: 2,
            community_id: 'free', gravity_mass: 1, x: 28, y: 0, vx: 0, vy: 0 },
        ];
        const stats = I.applyGalaxySystemHaloGravity(free, {
          gravity: 100, softening: 12, smoothFraction: 0.85,
        });
        const momentum = free.reduce((sum, node) => sum
          + node.gravity_mass * node.vx, 0);
        const firstOrder = free.slice(1).map(node => node.__galaxyOrbitOrder.tier);
        free[1].x = 80; free[2].x = 10;
        free.forEach(node => { node.vx = 0; node.vy = 0; });
        I.applyGalaxySystemHaloGravity(free, {
          gravity: 100, softening: 12, smoothFraction: 0.85,
        });

        const freePair = [
          { id: 'a', anchor_role: 'community', community_id: 'pair',
            gravity_mass: 8, x: 0, y: 0, vx: 0, vy: 0 },
          { id: 'b', community_id: 'pair', gravity_mass: 1,
            x: 24, y: 0, vx: 0, vy: 0 },
        ];
        const freeAcceleration = I.galaxyAccelerations(freePair, [], [], {
          gravity: 100, softening: 12, central: false, localPairFraction: 0.15,
        });
        const freeRelative = freeAcceleration.get(freePair[1]).ax
          - freeAcceleration.get(freePair[0]).ax;
        // The live local field is star-only in the star frame; the system-wide recoil is a
        // common translation, not an extra planet mass in this relative acceleration.
        const expectedFree = -I.galaxyStellarGravityConstant(100) * 8 * 24
          / Math.pow(24 * 24 + 12 * 12, 1.5);

        const pinnedPair = freePair.map((node, index) => ({ ...node,
          id: index ? 'planet' : 'black-hole',
          anchor_role: index ? 'none' : 'global', vx: 0, vy: 0,
        }));
        const pinnedAcceleration = I.galaxyAccelerations(pinnedPair, [], [], {
          gravity: 100, softening: 12, central: false, localPairFraction: 0.15,
        });
        /* The live integrator now gives a global/pinned planet only its dominant star's
           well.  The direct legacy-halo calls above deliberately retain their old contract. */
        const expectedPinned = -I.galaxyGravityConstant(100) * 8 * 24
          / Math.pow(24 * 24 + 12 * 12, 1.5);
        const seededPair = freePair.map(node => ({ ...node, vx: 0, vy: 0 }));
        I.seedGalaxyOrbits(seededPair, 72, 100, 12, false, 0.15);
        const seededAcceleration = I.galaxyAccelerations(seededPair, [], [], {
          gravity: 100, softening: 12, central: false, localPairFraction: 0.15,
          // This legacy two-body law intentionally excludes the new near-surface pressure;
          // the seed uses the pure dominant-star circular field, as covered separately.
          systemAnchorRepulsionAcceleration: 0,
        });
        const relativeVelocity = Math.hypot(
          seededPair[1].vx - seededPair[0].vx,
          seededPair[1].vy - seededPair[0].vy
        );
        const seededRadialAcceleration = -(
          seededAcceleration.get(seededPair[1]).ax
            - seededAcceleration.get(seededPair[0]).ax
        );
        const degenerate = [
          { id: 'solo', community_id: 'one', gravity_mass: 2, x: 0, y: 0 },
          { id: 'ghost', community_id: 'one', ghost: true,
            gravity_mass: 2, x: 0, y: 0 },
          { id: 'tie-a', community_id: 'tie', gravity_mass: 2, x: 5, y: 5 },
          { id: 'tie-b', community_id: 'tie', gravity_mass: 2, x: 5, y: 5 },
        ];
        I.applyGalaxySystemHaloGravity(degenerate, {
          gravity: 100, softening: 12, smoothFraction: 0.85,
        });
        const pathological = [
          { id: 'massive', anchor_role: 'community', community_id: 'huge',
            gravity_mass: 1000, x: 0, y: 0, vx: 0, vy: 0 },
          { id: 'near', community_id: 'huge', gravity_mass: 1000,
            x: 0.01, y: 0, vx: 0, vy: 0 },
        ];
        I.applyGalaxySystemHaloGravity(pathological, {
          gravity: 10000, softening: 0.1, smoothFraction: 0.85,
        });
        emit({ stats, momentum, firstOrder,
          frozenOrder: free.slice(1).map(node => node.__galaxyOrbitOrder.tier),
          freeRelative, expectedFree,
          pinned: [pinnedAcceleration.get(pinnedPair[0]),
            pinnedAcceleration.get(pinnedPair[1])],
          expectedPinned,
          seedLaw: [relativeVelocity * relativeVelocity / 24,
            seededRadialAcceleration],
          capped: pathological.map(node => Math.hypot(node.vx, node.vy)),
          cappedMomentum: pathological.reduce((sum, node) => sum
            + node.gravity_mass * node.vx, 0),
          finite: degenerate.every(node => node.ghost || [node.vx, node.vy]
            .every(value => value === undefined || Number.isFinite(value))),
        });
        """
    )
    assert report["stats"] == {"communities": 1, "satellites": 2}
    assert report["momentum"] == pytest.approx(0, abs=1e-12)
    assert report["firstOrder"] == report["frozenOrder"] == [1, 2]
    assert report["freeRelative"] == pytest.approx(report["expectedFree"], rel=1e-12)
    assert report["pinned"][0] == {"ax": 0, "ay": 0}
    assert report["pinned"][1]["ax"] == pytest.approx(report["expectedPinned"], rel=1e-12)
    assert report["pinned"][1]["ay"] == pytest.approx(0, abs=1e-12)
    assert report["seedLaw"][0] == pytest.approx(report["seedLaw"][1], rel=1e-12)
    assert max(report["capped"]) == pytest.approx(497.3076923076923)
    assert report["cappedMomentum"] == pytest.approx(0, abs=1e-9)
    assert report["finite"] is True


@requires_node
def test_black_hole_composite_field_is_mass_aware_differential_and_linear_cost() -> None:
    report = _run_node(
        """
        const fixture = coreScale => [
          { id: 'black-hole', anchor_role: 'global', community_id: 'core',
            gravity_mass: 8 * coreScale, x: 0, y: 0, vx: 0, vy: 0 },
          { id: 'bulge', anchor_role: 'community', community_id: 'core',
            gravity_mass: 2 * coreScale, x: 8, y: 0, vx: 0, vy: 0 },
          { id: 'inner-a', community_id: 'inner', gravity_mass: 3,
            x: 78, y: 0, vx: 0, vy: 0 },
          { id: 'inner-b', community_id: 'inner', gravity_mass: 2,
            x: 84, y: 2, vx: 0, vy: 0 },
          { id: 'outer', community_id: 'outer', gravity_mass: 1,
            x: 240, y: 0, vx: 0, vy: 0 },
        ];
        const weakNodes = fixture(1), strongNodes = fixture(2);
        const weak = I.galaxyBlackHoleField(weakNodes, {
          gravity: 48, softening: 36, accelerationCap: 100,
        });
        const strong = I.galaxyBlackHoleField(strongNodes, {
          gravity: 48, softening: 36, accelerationCap: 100,
        });
        I.applyGalaxyBlackHoleGravity(weakNodes, {
          gravity: 48, softening: 36, accelerationCap: 100,
        });
        const inner = weak.systems.find(item => item.center.id === 'inner');
        const outer = weak.systems.find(item => item.center.id === 'outer');
        const strongInner = strong.systems.find(item => item.center.id === 'inner');
        const many = Array.from({ length: 600 }, (_, index) => ({
          id: index ? 'n' + index : 'bh',
          anchor_role: index ? 'none' : 'global',
          community_id: 'c' + index,
          gravity_mass: 1 + index % 7,
          x: index ? Math.cos(index * 2.399) * (40 + Math.sqrt(index) * 9) : 0,
          y: index ? Math.sin(index * 2.399) * (40 + Math.sqrt(index) * 9) : 0,
        }));
        const manyField = I.galaxyBlackHoleField(many, {
          gravity: 48, softening: 36,
        });
        emit({
          anchor: weak.anchor.id,
          masses: [weak.coreMass, weak.haloMass],
          traversals: weak.traversals,
          differential: [inner.omega, outer.omega],
          massRatio: Math.hypot(strongInner.ax, strongInner.ay)
            / Math.hypot(inner.ax, inner.ay),
          inward: weakNodes.filter(node => node.community_id !== 'core')
            .map(node => node.x * node.vx + node.y * node.vy),
          rigidInner: [weakNodes[2].vx - weakNodes[3].vx,
            weakNodes[2].vy - weakNodes[3].vy],
          many: { traversals: manyField.traversals, systems: manyField.systems.length },
        });
        """
    )
    assert report["anchor"] == "black-hole"
    assert report["masses"] == [8, 8]
    assert report["traversals"] == 3
    assert report["differential"][0] > report["differential"][1] > 0
    assert report["massRatio"] > 1.5
    assert all(dot < 0 for dot in report["inward"])
    assert report["rigidInner"] == pytest.approx([0, 0], abs=1e-12)
    assert report["many"]["traversals"] == 600
    assert report["many"]["systems"] == 599


@requires_node
def test_global_anchor_stays_exactly_centered_without_packing_the_disk() -> None:
    report = _run_node(
        """
        const nodes = [
          ['black-hole', 16, 'core', 0, 0, 'global'],
          ['bulge', 4, 'core', 12, 3, 'community'],
          ['inner-star', 5, 'inner', 80, 0, 'community'],
          ['inner-planet', 2, 'inner', 92, 4, 'none'],
          ['outer-star', 4, 'outer', 240, 0, 'community'],
          ['outer-planet', 1, 'outer', 252, -3, 'none'],
        ].map(([id, gravity_mass, community_id, x, y, anchor_role]) => ({
          id, gravity_mass, community_id, x, y, vx: 0, vy: 0,
          radius: 4, anchor_role,
        }));
        I.seedGalaxyOrbits(nodes, 19, 100, 8, false);
        I.seedGalaxySystemOrbits(nodes, 19, 100, 40, false);
        let exact = true;
        for (let step = 0; step < 90; step++) {
          I.integrateGalaxyLeapfrog(nodes, [], [], {
            gravity: 100, softening: 8, centralSoftening: 40,
            timestep: 0.75, velocityDecay: 0.0005, speedLimit: 48,
            collisionPadding: 1.5, collisionStrength: 0.7, collisionIterations: 2,
          });
          const anchor = nodes[0];
          exact = exact && anchor.x === 0 && anchor.y === 0
            && anchor.vx === 0 && anchor.vy === 0;
        }
        const centers = [...I.communityCenters(nodes).values()];
        let minimumSystemDistance = Infinity;
        for (let left = 0; left < centers.length; left++) for (
          let right = left + 1; right < centers.length; right++
        ) minimumSystemDistance = Math.min(minimumSystemDistance,
          Math.hypot(centers[left].x - centers[right].x,
            centers[left].y - centers[right].y));
        emit({ exact, finite: nodes.every(node => [node.x, node.y, node.vx, node.vy]
          .every(Number.isFinite)), minimumSystemDistance });
        """
    )
    assert report["exact"] is True
    assert report["finite"] is True
    assert report["minimumSystemDistance"] > 40


@requires_node
def test_actual_shaped_multi_member_galaxy_stays_bound_for_1800_steps() -> None:
    report = _run_node(
        """
        const nodes = [{
          id: 'black-hole', anchor_role: 'global', community_id: 'core',
          gravity_mass: 24, visual_radius: 10, radius: 10,
          galactic_radius: 0, x: 0, y: 0, vx: 0, vy: 0,
        }];
        const links = [];
        for (let system = 1; system <= 24; system++) {
          const galacticRadius = 140 + system * 16;
          const phase = system * 2.399963229728653;
          const centerX = Math.cos(phase) * galacticRadius;
          const centerY = Math.sin(phase) * galacticRadius * 0.82;
          for (let member = 0; member < 6; member++) {
            const localRadius = member === 0 ? 0 : 12 + member * 5;
            const localPhase = phase + member * 1.2566370614;
            nodes.push({
              id: `s${system}-n${member}`,
              anchor_role: member === 0 ? 'community' : 'none',
              community_id: `system-${system}`,
              gravity_mass: member === 0 ? 5 + system % 4 : 1 + (member % 3) * 0.5,
              visual_radius: member === 0 ? 5 : 2 + member % 2,
              radius: member === 0 ? 5 : 2 + member % 2,
              galactic_radius: galacticRadius,
              galactic_phase: phase,
              x: centerX + Math.cos(localPhase) * localRadius,
              y: centerY + Math.sin(localPhase) * localRadius,
              vx: 0, vy: 0,
            });
            if (member > 0) links.push({
              source: `s${system}-n0`, target: `s${system}-n${member}`,
              rest_length: localRadius, spring_strength: 0.08,
            });
          }
        }
        I.seedGalaxyOrbits(nodes, 91027, 100, 32, false, 0.15);
        I.seedGalaxySystemOrbits(nodes, 91027, 100, 40, false);
        const percentile = (values, fraction) => {
          const sorted = values.slice().sort((a, b) => a - b);
          return sorted[Math.min(sorted.length - 1, Math.floor((sorted.length - 1) * fraction))];
        };
        const snapshot = () => {
          const centers = [...I.communityCenters(nodes).values()]
            .filter(center => center.id !== 'core');
          const systemRadii = centers.map(center => Math.hypot(center.x, center.y));
          const nodeRadii = nodes.slice(1).map(node => Math.hypot(node.x, node.y));
          return {
            median: percentile(systemRadii, 0.5),
            p95: percentile(systemRadii, 0.95),
            maxNode: Math.max(...nodeRadii),
          };
        };
        const orbitalEnergy = () => {
          const field = I.galaxyBlackHoleField(nodes, { gravity: 100, softening: 40 });
          const g = I.galaxyGravityConstant(100);
          return field.systems.reduce((sum, item) => {
            let vx = 0, vy = 0;
            item.center.nodes.forEach(node => {
              vx += node.gravity_mass * node.vx;
              vy += node.gravity_mass * node.vy;
            });
            vx /= item.center.mass; vy /= item.center.mass;
            const kinetic = 0.5 * item.center.mass * (vx * vx + vy * vy);
            const potential = -item.center.mass * g * (
              field.coreMass / Math.sqrt(item.radius * item.radius + 40 * 40)
              + field.haloMass / Math.sqrt(
                item.radius * item.radius + field.haloScale * field.haloScale
              )
            );
            return sum + kinetic + potential;
          }, 0);
        };
        const initial = snapshot();
        const initialEnergy = orbitalEnergy();
        let minimumMedian = initial.median, maximumP95 = initial.p95;
        let maximumNode = initial.maxNode, minimumEnergy = initialEnergy;
        let maximumEnergy = initialEnergy, exactCenter = true, speedCaps = 0;
        const angleStep = (next, previous) => Math.atan2(
          Math.sin(next - previous), Math.cos(next - previous)
        );
        const globalAngles = new Map([...I.communityCenters(nodes).values()]
          .filter(center => center.id !== 'core')
          .map(center => [center.id, Math.atan2(center.y, center.x)]));
        const localAngles = new Map(nodes.slice(1).filter(node => node.anchor_role !== 'community')
          .map(node => {
            const star = nodes.find(candidate => candidate.community_id === node.community_id
              && candidate.anchor_role === 'community');
            return [node.id, Math.atan2(node.y - star.y, node.x - star.x)];
          }));
        let globalTravel = 0, localTravel = 0, minimumStarClearance = Infinity;
        let starContacts = 0;
        for (let step = 0; step < 1800; step++) {
          const tick = I.integrateGalaxyLeapfrog(nodes, links, [], {
            gravity: 100, softening: 32, centralSoftening: 40,
            timestep: 0.021328125, velocityDecay: 0.00005, speedLimit: 48,
            localPairFraction: 0.15, corePairMultiplier: 0.75,
            includeBridges: false, includeMutualSystems: true,
            mutualSystemGravityFraction: 0.12, mutualSystemSoftening: 80,
            includeRelations: true, includeRelationSprings: false,
            skipSystemAnchorRelations: true, relationStrengthMultiplier: 2,
            relationForceCap: 1.6, relationAccelerationCap: 3.2,
            relationConstraintRate: 24, relationConstraintMaxCorrection: 12,
            relationPadding: 1.5,
            includeOrbitalSeparation: true, orbitalSeparationPadding: 1.5,
            orbitalSeparationStrength: 0.8, crossCommunitySeparationPadding: 1.5,
            crossCommunitySeparationStrength: 0.144,
            orbitalSeparationMaxCorrection: 4, orbitalSeparationMaxVelocityCorrection: 8,
            preserveLocalTangentialVelocity: true, skipSystemAnchorPairs: true,
            systemAnchorExclusionPadding: 1.5,
            includeCollisions: false,
            includeBlackHoleExclusion: true, blackHoleExclusionPadding: 2.5,
            includeFarFieldConfinement: true, farFieldEnvelopeScale: 1.25,
            farFieldMinimumRadius: 96, farFieldSoftFraction: 0.82,
            farFieldAcceleration: 12, farFieldMaxAcceleration: 16,
            inwardConvergence: true, wallClockSeconds: 1 / 30,
          });
          if (tick.speedCapped) speedCaps++;
          starContacts += tick.systemAnchorExclusion.contacts;
          I.communityCenters(nodes).forEach(center => {
            if (center.id === 'core') return;
            const angle = Math.atan2(center.y, center.x);
            globalTravel += Math.abs(angleStep(angle, globalAngles.get(center.id)));
            globalAngles.set(center.id, angle);
          });
          localAngles.forEach((previous, id) => {
            const node = nodes.find(candidate => candidate.id === id);
            const star = nodes.find(candidate => candidate.community_id === node.community_id
              && candidate.anchor_role === 'community');
            const angle = Math.atan2(node.y - star.y, node.x - star.x);
            localTravel += Math.abs(angleStep(angle, previous));
            localAngles.set(id, angle);
            minimumStarClearance = Math.min(minimumStarClearance,
              Math.hypot(node.x - star.x, node.y - star.y) - node.radius - star.radius - 1.5);
          });
          const sample = snapshot();
          minimumMedian = Math.min(minimumMedian, sample.median);
          maximumP95 = Math.max(maximumP95, sample.p95);
          maximumNode = Math.max(maximumNode, sample.maxNode);
          const energy = orbitalEnergy();
          minimumEnergy = Math.min(minimumEnergy, energy);
          maximumEnergy = Math.max(maximumEnergy, energy);
          const anchor = nodes[0];
          exactCenter = exactCenter && anchor.x === 0 && anchor.y === 0
            && anchor.vx === 0 && anchor.vy === 0;
        }
        let overlaps = 0, minimumSeparation = Infinity, minimumSystemDiameter = Infinity;
        const bySystem = new Map();
        nodes.slice(1).forEach(node => {
          if (!bySystem.has(node.community_id)) bySystem.set(node.community_id, []);
          bySystem.get(node.community_id).push(node);
        });
        bySystem.forEach(members => {
          let diameter = 0;
          for (let left = 0; left < members.length; left++) for (
            let right = left + 1; right < members.length; right++
          ) {
            const separation = Math.hypot(members[left].x - members[right].x,
              members[left].y - members[right].y);
            minimumSeparation = Math.min(minimumSeparation, separation);
            diameter = Math.max(diameter, separation);
            if (separation < members[left].radius + members[right].radius) overlaps++;
          }
          minimumSystemDiameter = Math.min(minimumSystemDiameter, diameter);
        });
        emit({ initial, final: snapshot(), minimumMedian, maximumP95, maximumNode,
          energyDrift: (maximumEnergy - minimumEnergy) / Math.abs(initialEnergy),
          exactCenter, speedCaps, overlaps, minimumSeparation, minimumSystemDiameter,
          globalTravel, localTravel, minimumStarClearance, starContacts,
          finite: nodes.every(node => [node.x, node.y, node.vx, node.vy]
            .every(Number.isFinite)) });
        """
    )
    assert report["finite"] is True
    assert report["exactCenter"] is True
    # Gravity 100 is more than twice the live default.  Its emergency guard may engage for a
    # bounded minority of stress ticks (the default-48 fixture below remains cap-free), but it
    # must not become the system's steady state or replace the asserted orbital travel.
    assert report["speedCaps"] < 1800 * 0.3
    # The controlled projection deliberately permits painted envelopes to overlap as it draws
    # every orbit inward. Collision impulses remain off here because they can create the
    # outward/ejection response this mode forbids; the systems must still retain real extent.
    assert report["overlaps"] <= 18
    assert report["minimumSeparation"] > 0.1
    assert report["minimumSystemDiameter"] > 15
    # This large 144-satellite scene may begin already surface-safe, so a contact count is not
    # an invariant. The final 24-pass solver must nevertheless never reopen painted overlap.
    assert report["minimumStarClearance"] >= -1e-9
    assert report["globalTravel"] > 1
    assert report["localTravel"] > 1
    assert report["minimumMedian"] > report["initial"]["median"] * 0.05
    assert report["maximumP95"] < report["initial"]["p95"] * 1.45
    assert report["maximumNode"] < report["initial"]["maxNode"] * 1.45


@requires_node
def test_stronger_gravity_keeps_a_300_node_galaxy_on_the_controlled_inward_track() -> None:
    report = _run_node(
        """
        const nodes = [{ id: 'black-hole', anchor_role: 'global', community_id: 'core',
          gravity_mass: 24, radius: 10, x: 0, y: 0, vx: 0, vy: 0 }];
        for (let system = 1; system <= 50; system++) {
          const members = system === 50 ? 5 : 6;
          const radius = 105 + system * 5.5;
          const phase = system * 2.399963229728653;
          for (let member = 0; member < members; member++) {
            const localRadius = member === 0 ? 0 : 8 + member * 3.5;
            const localPhase = phase + member * 1.2566370614;
            nodes.push({
              id: `s${system}-n${member}`,
              anchor_role: member === 0 ? 'community' : 'none',
              community_id: `s${system}`,
              gravity_mass: member === 0 ? 5 + system % 4 : 1 + (member % 3) * 0.5,
              radius: member === 0 ? 5 : 2,
              x: Math.cos(phase) * radius + Math.cos(localPhase) * localRadius,
              y: Math.sin(phase) * radius * 0.82 + Math.sin(localPhase) * localRadius,
              vx: 0, vy: 0,
            });
          }
        }
        I.seedGalaxyOrbits(nodes, 91027, 100, 32, false, 0.15, 0.75);
        I.seedGalaxySystemOrbits(nodes, 91027, 100, 40, false);
        const systemSnapshot = () => new Map([...I.communityCenters(nodes).values()]
          .filter(center => center.id !== 'core')
          .map(center => [center.id, Math.hypot(center.x, center.y)]));
        const initial = systemSnapshot();
        let previous = new Map(initial), monotone = true, speedCaps = 0, maxSpeed = 0;
        for (let step = 0; step < 1800; step++) {
          const tick = I.integrateGalaxyLeapfrog(nodes, [], [], {
            gravity: 100, softening: 32, centralSoftening: 40, timestep: 0.032,
            velocityDecay: 0.00005, speedLimit: 48, localPairFraction: 0.15,
            corePairMultiplier: 0.75, includeBridges: false, includeRelations: false,
            includeCollisions: false, inwardConvergence: true, wallClockSeconds: 1 / 30,
          });
          speedCaps += tick.speedCapped ? 1 : 0;
          systemSnapshot().forEach((radius, id) => {
            monotone = monotone && radius <= previous.get(id) + 1e-8;
            previous.set(id, radius);
          });
          nodes.slice(1).forEach(node => {
            maxSpeed = Math.max(maxSpeed, Math.hypot(node.vx, node.vy));
          });
        }
        const ratios = [...previous.entries()].map(([id, radius]) => radius / initial.get(id))
          .sort((left, right) => left - right);
        emit({
          nodes: nodes.length, monotone, speedCaps, maxSpeed,
          ratioMin: ratios[0], ratioMedian: ratios[Math.floor(ratios.length / 2)],
          ratioMax: ratios[ratios.length - 1],
          anchor: [nodes[0].x, nodes[0].y, nodes[0].vx, nodes[0].vy],
          finite: nodes.every(node => [node.x, node.y, node.vx, node.vy]
            .every(Number.isFinite)),
        });
        """
    )
    assert report["nodes"] == 300
    assert report["monotone"] is True
    # The established emergency cap remains 48.  At this >2x-default stress field, inner
    # encounters may touch it for a bounded minority of ticks without owning the simulation.
    assert report["speedCaps"] < 1800 * 0.3
    assert report["maxSpeed"] <= 48 + 1e-10
    # A full wall-clock minute advances the convergence trajectory at 68% speed,
    # while retaining the maximum field's 3.6x response. Individual escape attempts may
    # receive the extra 10% inward correction but cannot eject outward.
    maximum_track = 0.75 ** (3.6 * 0.68)
    assert report["ratioMedian"] == pytest.approx(maximum_track, abs=1e-8)
    assert report["ratioMax"] <= maximum_track + 1e-8
    assert report["ratioMin"] > maximum_track * 0.75
    assert report["anchor"] == pytest.approx([0, 0, 0, 0], abs=1e-12)
    assert report["finite"] is True


@requires_node
def test_black_hole_adornment_is_bounded_and_does_not_change_hit_geometry() -> None:
    report = _run_node(
        """
        const calls = { arcs: 0, ellipses: 0, fills: 0, strokes: 0, gradients: 0 };
        const ctx = {
          save() {}, restore() {}, beginPath() {},
          arc() { calls.arcs++; }, ellipse() { calls.ellipses++; },
          fill() { calls.fills++; }, stroke() { calls.strokes++; },
          createRadialGradient() { calls.gradients++; return { addColorStop() {} }; },
          set fillStyle(value) {}, set strokeStyle(value) {}, set lineWidth(value) {},
        };
        const global = { id: 'bh', x: 0, y: 0, radius: 9,
          color: '#8f7cff', anchor_role: 'global' };
        const community = { id: 'star', x: 20, y: 0, radius: 5,
          color: '#63d8cb', anchor_role: 'community' };
        const ordinary = { id: 'planet', x: 30, y: 0, radius: 3,
          color: '#ffffff', anchor_role: 'none' };
        const before = [global.radius, community.radius, ordinary.radius];
        const painted = [
          I.paintGalaxyAnchorAdornment(ctx, global, 1, '#a58cff', false),
          I.paintGalaxyAnchorAdornment(ctx, global, 1, '#a58cff', true),
          I.paintGalaxyAnchorAdornment(ctx, community, 1, '#63d8cb', false),
          I.paintGalaxyAnchorAdornment(ctx, ordinary, 1, '#ffffff', false),
        ];
        emit({ calls, painted, before,
          after: [global.radius, community.radius, ordinary.radius] });
        """
    )
    assert report["painted"] == [1, 1, 1, 0]
    assert report["before"] == report["after"] == [9, 5, 3]
    assert report["calls"]["gradients"] == 1
    assert report["calls"]["ellipses"] == 1
    assert report["calls"]["arcs"] >= 3
    assert report["calls"]["fills"] >= 2
    assert report["calls"]["strokes"] >= 3
    source = ASSET.read_text(encoding="utf-8")
    style_node = source[source.index("function styleNode(node, ctx, scale)"):
                        source.index("function applyChrome", source.index("function styleNode(node, ctx, scale)"))]
    assert "state.settings.mode === 'galaxy'" in style_node
    assert style_node.count("paintGalaxyAnchorAdornment(") == 2


@requires_node
def test_galaxy_black_hole_seeds_inward_epicycles_with_tangential_rotation() -> None:
    report = _run_node(
        """
        const nodes = [
          { id: 'anchor', x: 0, y: 0, vx: 0, vy: 0, gravity_mass: 16,
            community_id: 'core', anchor_role: 'global' },
          { id: 'inner', x: 70, y: 0, vx: 0, vy: 0, gravity_mass: 2,
            community_id: 'inner' },
          { id: 'outer', x: 180, y: 0, vx: 0, vy: 0, gravity_mass: 1,
            community_id: 'outer' },
        ];
        I.seedGalaxySystemOrbits(nodes, 91, 48, 40, false);
        const radius = node => Math.hypot(node.x, node.y);
        const radialVelocity = node => node.x * node.vx + node.y * node.vy;
        const initial = nodes.slice(1).map(node => ({
          radius: radius(node), radial: radialVelocity(node),
          angular: node.x * node.vy - node.y * node.vx,
        }));
        for (let index = 0; index < 120; index++) {
          I.integrateGalaxyLeapfrog(nodes, [], [], {
              gravity: 48, softening: 8, centralSoftening: 40, timestep: 0.021328125,
            velocityDecay: 0.02, speedLimit: 100, collisionStrength: 0,
          });
        }
        emit({
          initial,
          final: nodes.slice(1).map(node => ({
            radius: radius(node),
            angular: node.x * node.vy - node.y * node.vx,
          })),
          anchor: [nodes[0].x, nodes[0].y, nodes[0].vx, nodes[0].vy],
        });
        """
    )
    # Every system begins settling toward the black-hole well; neither seed may fly outward.
    assert all(item["radial"] < 0 for item in report["initial"])
    assert all(
        0.5 * initial["radius"] < final["radius"] < 1.5 * initial["radius"]
        for initial, final in zip(report["initial"], report["final"])
    )
    assert all(abs(item["angular"]) > 1e-6 for item in report["initial"])
    assert all(abs(item["angular"]) > 1e-6 for item in report["final"])
    assert report["anchor"] == pytest.approx([0, 0, 0, 0])


@requires_node
def test_galaxy_relation_springs_are_local_mass_aware_and_momentum_symmetric() -> None:
    report = _run_node(
        """
        const fixture = () => [
          { id: 'heavy', x: 0, y: 0, vx: 0, vy: 0, gravity_mass: 4, community_id: 'solar' },
          { id: 'light', x: 30, y: 0, vx: 0, vy: 0, gravity_mass: 1, community_id: 'solar' },
          { id: 'remote', x: 80, y: 0, vx: 0, vy: 0, gravity_mass: 2, community_id: 'remote' },
          { id: 'history', x: 12, y: 0, vx: 0, vy: 0, gravity_mass: 0,
            community_id: 'solar', ghost: true },
        ];
        const stretched = fixture();
        const stretchedStats = I.applyGalaxyRelationSprings(stretched, [
          { source: 'heavy', target: 'light', rest_length: 20, spring_strength: 0.1 },
          { source: 'light', target: 'remote', rest_length: 20, spring_strength: 0.2 },
          { source: 'heavy', target: 'remote', rest_length: 20, spring_strength: 0.2,
            ghost: true, physics_strength: 0 },
          { source: 'heavy', target: 'history', rest_length: 20, spring_strength: 0.2 },
        ], { alpha: 1, orbitScale: 1 });
        const compressed = fixture();
        I.applyGalaxyRelationSprings(compressed, [
          { source: 'heavy', target: 'light', rest_length: 20, spring_strength: 0.1 },
        ], { alpha: 1, orbitScale: 2 });
        emit({
          stretched: stretched.map(node => [node.vx, node.vy]),
          compressed: compressed.map(node => [node.vx, node.vy]),
          applied: stretchedStats.applied,
          momentum: stretched.reduce(
            (sum, node) => sum + node.gravity_mass * node.vx, 0
          ),
        });
        """
    )
    assert report["stretched"][0] == pytest.approx([0.2, 0])
    assert report["stretched"][1] == pytest.approx([-0.8, 0])
    assert report["stretched"][2] == pytest.approx([0, 0])
    assert report["stretched"][3] == pytest.approx([0, 0])
    assert report["compressed"][0] == pytest.approx([-0.2, 0])
    assert report["compressed"][1] == pytest.approx([0.8, 0])
    assert report["compressed"][2] == pytest.approx([0, 0])
    assert report["compressed"][3] == pytest.approx([0, 0])
    assert report["applied"] == 1
    assert report["momentum"] == pytest.approx(0, abs=1e-12)


@requires_node
def test_galaxy_link_distance_has_squared_scale_and_release_stable_response() -> None:
    report = _run_node(
        """
        const spring = (setting, strengthMultiplier = 2,
          forceCap = 1.6, accelerationCap = 3.2) => {
          const nodes = [
            { id: 'star', x: 0, y: 0, vx: 0, vy: 0,
              gravity_mass: 4, radius: 1, community_id: 'solar' },
            { id: 'planet', x: 10, y: 0, vx: 0, vy: 0,
              gravity_mass: 1, radius: 1, community_id: 'solar' },
          ];
          const link = { source: 'star', target: 'planet',
            rest_length: 20, spring_strength: 0.1 };
          const orbitScale = I.galaxyRelationOrbitScale(setting);
          const stats = I.applyGalaxyRelationSprings(nodes, [link], {
            alpha: 1, orbitScale, strengthMultiplier,
            forceCap, accelerationCap,
          });
          return {
            orbitScale,
            target: I.galaxySpringDistance(link, orbitScale),
            velocities: nodes.map(node => node.vx),
            momentum: nodes.reduce(
              (sum, node) => sum + node.gravity_mass * node.vx, 0),
            stats,
          };
        };
        const ordinary = [
          { id: 'star', x: 0, y: 0, vx: 0, vy: 0,
            gravity_mass: 4, radius: 1, community_id: 'solar' },
          { id: 'planet', x: 10, y: 0, vx: 0, vy: 0,
            gravity_mass: 1, radius: 1, community_id: 'solar' },
        ];
        I.applyGalaxyRelationSprings(ordinary, [{
          source: 'star', target: 'planet', rest_length: 20, spring_strength: 0.1,
        }], { alpha: 1, orbitScale: 0.25, forceCap: 1.6, accelerationCap: 3.2 });
        emit({
          tight: spring(4), baseline: spring(8), reference: spring(16), loose: spring(80),
          unsafeLoose: spring(80, 4, 3.2, 6.4),
          ordinary: ordinary.map(node => node.vx),
          constraint: (() => {
            const make = () => [
              { id: 'star', x: 0, y: 0, vx: 0, vy: 0,
                gravity_mass: 4, radius: 1, community_id: 'solar' },
              { id: 'planet', x: 10, y: 0, vx: 0, vy: 0,
                gravity_mass: 1, radius: 1, community_id: 'solar' },
            ];
            const link = { source: 'star', target: 'planet',
              rest_length: 20, spring_strength: 0.1 };
            const run = (setting, responseMultiplier, maxCorrection) => {
              const nodes = make();
              const beforeCom = (nodes[0].x * 4 + nodes[1].x) / 5;
              const stats = I.applyGalaxyRelationDistanceConstraints(nodes, [link], {
                orbitScale: I.galaxyRelationOrbitScale(setting), strengthMultiplier: 2,
                responseMultiplier, wallClockSeconds: 1 / 30, rate: 24, maxCorrection,
              });
              return {
                distance: Math.abs(nodes[1].x - nodes[0].x),
                target: I.galaxySpringDistance(link, I.galaxyRelationOrbitScale(setting)),
                beforeCom, afterCom: (nodes[0].x * 4 + nodes[1].x) / 5, stats,
              };
            };
            return {
              tight: run(8, 1, 12), loose: run(80, 1, 12),
              responseStable: run(8, 1, 100), unsafeDoubled: run(8, 2, 100),
              capStable: run(80, 1, 12), unsafeCapDoubled: run(80, 2, 12),
            };
          })(),
        });
        """
    )
    assert report["tight"]["orbitScale"] == pytest.approx(1 / 16)
    assert report["baseline"]["orbitScale"] == pytest.approx(0.25)
    assert report["reference"]["orbitScale"] == pytest.approx(1)
    assert report["loose"]["orbitScale"] == pytest.approx(25)
    assert report["tight"]["target"] == pytest.approx(1.25)
    assert report["baseline"]["target"] == pytest.approx(5)
    assert report["loose"]["target"] == pytest.approx(500)
    assert report["baseline"]["velocities"] == pytest.approx(
        [value * 2 for value in report["ordinary"]]
    )
    assert report["loose"]["target"] == report["unsafeLoose"]["target"]
    assert report["unsafeLoose"]["velocities"] == pytest.approx(
        [value * 2 for value in report["loose"]["velocities"]]
    )
    assert report["unsafeLoose"]["stats"]["maximumAcceleration"] == pytest.approx(
        report["loose"]["stats"]["maximumAcceleration"] * 2
    )
    assert report["tight"]["velocities"][0] > 0
    assert report["loose"]["velocities"][0] < 0
    assert report["constraint"]["tight"]["distance"] < 10
    assert report["constraint"]["loose"]["distance"] > 10
    assert report["constraint"]["tight"]["stats"]["applied"] == 1
    assert report["constraint"]["loose"]["stats"]["applied"] == 1
    assert report["constraint"]["unsafeDoubled"]["target"] == \
        report["constraint"]["responseStable"]["target"]
    # Doubling a continuous convergence rate squares the fraction of relation error left
    # after one frame.  It must not multiply the completed displacement past the target.
    prior_correction = report["constraint"]["responseStable"]["stats"]["correctedDistance"]
    initial_error = 5
    prior_response = prior_correction / initial_error
    doubled_response = 1 - (1 - prior_response) ** 2
    assert report["constraint"]["unsafeDoubled"]["stats"]["correctedDistance"] \
        == pytest.approx(initial_error * doubled_response, rel=1e-12)
    assert report["constraint"]["unsafeDoubled"]["stats"]["correctedDistance"] \
        < prior_correction * 2
    assert report["constraint"]["capStable"]["stats"]["maximumNodeShift"] \
        == pytest.approx(9.6)
    assert report["constraint"]["unsafeCapDoubled"]["stats"]["maximumNodeShift"] \
        == pytest.approx(9.6)
    assert report["constraint"]["capStable"]["stats"]["correctedDistance"] \
        == pytest.approx(12)
    assert report["constraint"]["unsafeCapDoubled"]["stats"]["correctedDistance"] \
        == pytest.approx(12)
    assert report["constraint"]["unsafeCapDoubled"]["stats"]["correctedDistance"] \
        == pytest.approx(report["constraint"]["capStable"]["stats"]["correctedDistance"])
    assert report["constraint"]["tight"]["afterCom"] == pytest.approx(
        report["constraint"]["tight"]["beforeCom"], abs=1e-12
    )
    assert report["constraint"]["loose"]["afterCom"] == pytest.approx(
        report["constraint"]["loose"]["beforeCom"], abs=1e-12
    )
    assert all(
        item["momentum"] == pytest.approx(0, abs=1e-12)
        for item in (report["tight"], report["baseline"], report["loose"])
    )


@requires_node
def test_orbital_separation_is_contractive_and_preserves_local_mass_center() -> None:
    report = _run_node(
        """
        const run = (setting, strengthOverride = null) => {
          const nodes = [
            { id: 'star', x: 0, y: 0, vx: 0, vy: 0, radius: 3,
              gravity_mass: 4, community_id: 'solar' },
            { id: 'planet', x: 10, y: 0, vx: 0, vy: 0, radius: 3,
              gravity_mass: 1, community_id: 'solar' },
            { id: 'other-system', x: 1, y: 0, vx: 0, vy: 0, radius: 3,
              gravity_mass: 2, community_id: 'other' },
          ];
          const beforeCom = (nodes[0].x * 4 + nodes[1].x) / 5;
          const otherBefore = [nodes[2].x, nodes[2].y, nodes[2].vx, nodes[2].vy];
          const padding = I.galaxyOrbitalSeparationPadding(setting);
          const strength = I.galaxyOrbitalSeparationStrength(setting);
          const stats = I.applyGalaxyOrbitalSeparation(nodes, {
            padding, strength: strengthOverride === null ? strength : strengthOverride,
            maxCorrection: 100, maxVelocityCorrection: 100,
          });
          return {
            padding, strength, stats,
            distance: Math.hypot(nodes[1].x - nodes[0].x, nodes[1].y - nodes[0].y),
            beforeCom, afterCom: (nodes[0].x * 4 + nodes[1].x) / 5,
            otherBefore,
            otherAfter: [nodes[2].x, nodes[2].y, nodes[2].vx, nodes[2].vy],
          };
        };
        emit({ off: run(0), default: run(48), preset: run(60), maximum: run(120),
          priorDefault: run(48, 0.8), priorMaximum: run(120, 1) });
        """
    )
    assert report["off"]["padding"] == 0
    assert report["off"]["strength"] == 0
    assert report["off"]["distance"] == pytest.approx(10)
    assert report["default"]["padding"] == pytest.approx(12)
    assert report["default"]["strength"] == pytest.approx(0.8)
    assert report["default"]["distance"] == pytest.approx(16.4)
    assert report["preset"]["strength"] == pytest.approx(1)
    assert report["preset"]["distance"] == pytest.approx(21)
    assert report["maximum"]["padding"] == pytest.approx(30)
    assert report["maximum"]["strength"] == pytest.approx(1)
    assert report["maximum"]["distance"] == pytest.approx(36)
    # The release-safe response never exceeds one. It approaches contact monotonically and
    # retains the pre-speed-up 48-setting calibration instead of crossing the manifold.
    assert report["default"]["stats"]["correctionDistance"] == pytest.approx(
        report["priorDefault"]["stats"]["correctionDistance"]
    )
    assert report["maximum"]["stats"]["correctionDistance"] == pytest.approx(
        report["priorMaximum"]["stats"]["correctionDistance"]
    )
    for item in (report["default"], report["preset"], report["maximum"]):
        assert item["stats"]["overlaps"] == 1
        assert item["afterCom"] == pytest.approx(item["beforeCom"], abs=1e-12)
        assert item["otherAfter"] == item["otherBefore"]


@requires_node
def test_cross_system_repulsion_is_weak_bounded_and_preserves_orbital_velocity() -> None:
    report = _run_node(
        """
        const fixture = (leftVx, rightVx) => [
          { id: 'heavy', community_id: 'left-system', x: 0, y: 0,
            vx: leftVx, vy: 0, radius: 3, gravity_mass: 4 },
          { id: 'light', community_id: 'right-system', x: 4, y: 0,
            vx: rightVx, vy: 0, radius: 3, gravity_mass: 1 },
        ];
        const options = {
          padding: 12, strength: 0,
          crossCommunityPadding: 1.5, crossCommunityStrength: 0.16,
          maxCorrection: 4, maxVelocityCorrection: 8,
        };
        const closing = fixture(1, -1);
        const separating = fixture(-1, 1);
        const disabled = fixture(1, -1);
        const beforeCom = (closing[0].x * 4 + closing[1].x) / 5;
        const beforeMomentum = closing[0].vx * 4 + closing[1].vx;
        const stats = I.applyGalaxyOrbitalSeparation(closing, options);
        I.applyGalaxyOrbitalSeparation(separating, options);
        const disabledStats = I.applyGalaxyOrbitalSeparation(disabled, {
          ...options, crossCommunityStrength: 0,
        });
        emit({
          stats, disabledStats,
          distance: closing[1].x - closing[0].x,
          center: (closing[0].x * 4 + closing[1].x) / 5,
          beforeCom,
          momentum: closing[0].vx * 4 + closing[1].vx,
          beforeMomentum,
          closingVelocity: closing.map(node => node.vx),
          separatingVelocity: separating.map(node => node.vx),
          disabledPhase: disabled.map(node => [node.x, node.y, node.vx, node.vy]),
          finite: closing.concat(separating).every(node =>
            [node.x, node.y, node.vx, node.vy].every(Number.isFinite)),
        });
        """
    )
    assert report["finite"] is True
    assert report["stats"]["crossCommunityPairs"] == 1
    assert report["stats"]["crossCommunityOverlaps"] == 1
    assert report["stats"]["crossCommunityCorrectionDistance"] == pytest.approx(0.56)
    assert report["distance"] == pytest.approx(4.56)
    assert report["center"] == pytest.approx(report["beforeCom"], abs=1e-12)
    assert report["momentum"] == pytest.approx(report["beforeMomentum"], abs=1e-12)
    # Cross-system contact is positional only: dissipating its COM motion repeatedly in a
    # crowded galaxy bleeds the tangential velocity that keeps both systems orbiting the well.
    assert report["closingVelocity"] == pytest.approx([1, -1], abs=1e-12)
    assert report["separatingVelocity"] == pytest.approx([-1, 1], abs=1e-12)
    assert report["disabledStats"]["overlaps"] == 0
    assert report["disabledPhase"] == [[0, 0, 1, 0], [4, 0, -1, 0]]


@requires_node
def test_cross_system_repulsion_translates_whole_systems_without_warping_orbits() -> None:
    report = _run_node(
        """
        const fixture = () => [
          { id: 'left-star', community_id: 'left-system', x: 0, y: 0,
            vx: 1, vy: 0, radius: 1, gravity_mass: 3 },
          { id: 'left-moon', community_id: 'left-system', x: 2, y: 1,
            vx: 1, vy: 2, radius: 1, gravity_mass: 1 },
          { id: 'right-star', community_id: 'right-system', x: 5, y: 0,
            vx: -1, vy: 0, radius: 1, gravity_mass: 2 },
          { id: 'right-moon', community_id: 'right-system', x: 7, y: -1,
            vx: -1, vy: -3, radius: 1, gravity_mass: 1 },
        ];
        const options = {
          padding: 12, strength: 0,
          crossCommunityPadding: 1.5, crossCommunityStrength: 0.16,
          maxCorrection: 4, maxVelocityCorrection: 8,
        };
        const relativeState = nodes => [
          nodes[1].x - nodes[0].x, nodes[1].y - nodes[0].y,
          nodes[1].vx - nodes[0].vx, nodes[1].vy - nodes[0].vy,
          nodes[3].x - nodes[2].x, nodes[3].y - nodes[2].y,
          nodes[3].vx - nodes[2].vx, nodes[3].vy - nodes[2].vy,
        ];
        const totals = nodes => {
          const mass = nodes.reduce((sum, node) => sum + node.gravity_mass, 0);
          return {
            center: [
              nodes.reduce((sum, node) => sum + node.x * node.gravity_mass, 0) / mass,
              nodes.reduce((sum, node) => sum + node.y * node.gravity_mass, 0) / mass,
            ],
            momentum: [
              nodes.reduce((sum, node) => sum + node.vx * node.gravity_mass, 0),
              nodes.reduce((sum, node) => sum + node.vy * node.gravity_mass, 0),
            ],
          };
        };
        const nodes = fixture();
        const beforeRelative = relativeState(nodes);
        const beforeTotals = totals(nodes);
        const stats = I.applyGalaxyOrbitalSeparation(nodes, options);
        const fixed = fixture();
        const fixedLeftBefore = fixed.slice(0, 2).map(node =>
          [node.x, node.y, node.vx, node.vy]);
        I.applyGalaxyOrbitalSeparation(fixed, { ...options, fixedNodeId: 'left-star' });
        emit({
          stats,
          beforeRelative,
          afterRelative: relativeState(nodes),
          beforeTotals,
          afterTotals: totals(nodes),
          fixedLeftBefore,
          fixedLeftAfter: fixed.slice(0, 2).map(node =>
            [node.x, node.y, node.vx, node.vy]),
          fixedRightMoved: fixed[2].x !== 5 || fixed[2].y !== 0,
          finite: nodes.concat(fixed).every(node =>
            [node.x, node.y, node.vx, node.vy].every(Number.isFinite)),
        });
        """
    )
    assert report["finite"] is True
    assert report["stats"]["crossCommunityOverlaps"] == 1
    assert report["afterRelative"] == pytest.approx(
        report["beforeRelative"], abs=1e-12
    )
    assert report["afterTotals"]["center"] == pytest.approx(
        report["beforeTotals"]["center"], abs=1e-12
    )
    assert report["afterTotals"]["momentum"] == pytest.approx(
        report["beforeTotals"]["momentum"], abs=1e-12
    )
    assert report["fixedLeftAfter"] == report["fixedLeftBefore"]
    assert report["fixedRightMoved"] is True


@requires_node
def test_far_field_confinement_bounds_painted_members_without_erasing_orbits() -> None:
    """The outer guard is a physical boundary, not a centre-only convergence hint.

    In particular, a satellite in the anchor community and the outer member of a
    multi-node external system must both be contained.  The external system moves
    rigidly, while the core satellite keeps its angular motion.
    """
    report = _run_node(
        """
        const options = {
          /* Deliberately use the live/default envelope scale. */
          farFieldMinimumRadius: 120,
          farFieldSoftFraction: 0.55, farFieldAcceleration: 0.2,
          farFieldMaxAcceleration: 0.2,
        };
        const nodes = [
          { id: 'black-hole', anchor_role: 'global', community_id: 'core',
            gravity_mass: 64, radius: 12, x: 0, y: 0, vx: 0, vy: 0 },
          { id: 'core-satellite', community_id: 'core', gravity_mass: 1,
            radius: 3, x: 900, y: 0, vx: 0, vy: 8 },
          { id: 'outer-star', community_id: 'outer', gravity_mass: 4,
            radius: 5, x: 600, y: 0, vx: 0, vy: 3 },
          { id: 'outer-moon', community_id: 'outer', gravity_mass: 1,
            radius: 3, x: 760, y: 0, vx: 0, vy: 5 },
          /* A pointer-owned system exercises the same painted outer guard. */
          { id: 'fixed-star', community_id: 'fixed', gravity_mass: 2,
            radius: 3, x: 300, y: -40, vx: 2, vy: 1 },
          { id: 'fixed-moon', community_id: 'fixed', gravity_mass: 1,
            radius: 2, x: 320, y: -40, vx: 2, vy: 4 },
        ];
        const fixedPhase = nodes.slice(4).map(node => [node.x, node.y, node.vx, node.vy]);
        const bootstrap = I.applyGalaxyFarFieldConfinement(nodes, {
          ...options, fixedNodeId: 'fixed-star',
        });
        const envelope = bootstrap.envelopeRadius;
        const core = nodes[1], star = nodes[2], moon = nodes[3];

        /* The smooth far-field must act before the exact cap. Put the external system in
           its soft band, but leave the core satellite for the strict member-level case. */
        core.x = envelope - 10; core.y = 0; core.vx = 0; core.vy = 8;
        star.x = envelope - 80; star.y = 0; star.vx = 0; star.vy = 3;
        moon.x = envelope + 80; moon.y = 0; moon.vx = 0; moon.vy = 5;
        const gravity = I.applyGalaxyFarFieldGravity(nodes, options);
        const inwardAcceleration = (star.vx * 4 + moon.vx) / 5;
        const coreInwardAcceleration = core.vx;

        /* Escape the core member outright, and put only the outer painted member of the
           external system past the cached envelope. Its COM is still within it. */
        core.x = envelope + 90; core.y = 0; core.vx = 12; core.vy = 8;
        star.x = envelope - 180; star.y = 0; star.vx = 12; star.vy = 3;
        moon.x = envelope + 40; moon.y = 0; moon.vx = 12; moon.vy = 5;
        const externalRelativeBefore = [
          moon.x - star.x, moon.y - star.y, moon.vx - star.vx, moon.vy - star.vy,
        ];
        const coreAngularBefore = core.x * core.vy - core.y * core.vx;
        const constrained = I.applyGalaxyFarFieldConfinement(nodes, {
          ...options, fixedNodeId: 'fixed-star',
        });
        const externalRelativeAfterConstraint = [
          moon.x - star.x, moon.y - star.y, moon.vx - star.vx, moon.vy - star.vy,
        ];
        const coreAngularAfterConstraint = core.x * core.vy - core.y * core.vx;
        /* Pointer targets outside the envelope are clamped before paint for the source and
           every companion, so release does not need to repair stretched geometry. */
        const fixedStar = nodes[4], fixedMoon = nodes[5];
        fixedStar.x = envelope + 240; fixedStar.y = -40; fixedStar.vx = 12; fixedStar.vy = 1;
        fixedMoon.x = envelope + 260; fixedMoon.y = -40; fixedMoon.vx = 12; fixedMoon.vy = 4;
        const fixedHeldBefore = nodes.slice(4).map(node => [node.x, node.y, node.vx, node.vy]);
        const fixedHeld = I.applyGalaxyFarFieldConfinement(nodes, {
          ...options, fixedNodeId: 'fixed-star',
        });
        const fixedHeldAfter = nodes.slice(4).map(node => [node.x, node.y, node.vx, node.vy]);
        const fixedHeldClearance = nodes.slice(4).map(node =>
          envelope - (Math.hypot(node.x, node.y) + node.radius));
        const fixedBeforeRelease = nodes.slice(4).map(node => [node.x, node.y]);
        const released = I.applyGalaxyFarFieldConfinement(nodes, options);
        const maximumFixedReleaseStep = Math.max(...nodes.slice(4).map((node, index) =>
          Math.hypot(node.x - fixedBeforeRelease[index][0], node.y - fixedBeforeRelease[index][1])));
        const clearance = node => envelope - (Math.hypot(node.x, node.y) + node.radius);
        const nonFixed = nodes.slice(1, 4);
        let maximumRadius = Math.max(...nonFixed.map(node => Math.hypot(node.x, node.y) + node.radius));
        let minimumClearance = Math.min(...nonFixed.map(clearance));
        let finalStep;
        for (let step = 0; step < 240; step++) {
          finalStep = I.integrateGalaxyLeapfrog(nodes, [], [], {
            ...options, gravity: 0, central: true, fixedNodeId: 'fixed-star',
            includeFarFieldConfinement: true, includeBlackHoleExclusion: true,
            includeCollisions: false, includeRelations: false,
            includeOrbitalSeparation: false, inwardConvergence: false,
            timestep: 0.021328125, wallClockSeconds: 1 / 30,
            velocityDecay: 0, speedLimit: 24,
          });
          const currentEnvelope = finalStep.farFieldConfinement.envelopeRadius;
          nonFixed.forEach(node => {
            maximumRadius = Math.max(maximumRadius, Math.hypot(node.x, node.y) + node.radius);
            minimumClearance = Math.min(minimumClearance,
              currentEnvelope - (Math.hypot(node.x, node.y) + node.radius));
          });
        }
        emit({
          bootstrap, gravity, constrained, envelope, inwardAcceleration,
          coreInwardAcceleration,
          externalRelativeBefore,
          externalRelativeAfterConstraint,
          coreAngularBefore,
          coreAngularAfterConstraint,
          coreTangentAfterConstraint: core.vy,
          coreAngularAfter: core.x * core.vy - core.y * core.vx,
          fixedPhase,
          fixedHeld, fixedHeldBefore, fixedHeldAfter, fixedHeldClearance, released,
          maximumFixedReleaseStep,
          fixedAfterRelease: nodes.slice(4).map(node => [node.x, node.y, node.vx, node.vy]),
          minimumClearance, maximumRadius,
          finalEnvelope: finalStep.farFieldConfinement.envelopeRadius,
          maximumSpeed: finalStep.maximumSpeed,
          horizonClearance: Math.hypot(core.x, core.y) - nodes[0].radius - core.radius - 2.5,
          finite: nodes.every(node => [node.x, node.y, node.vx, node.vy].every(Number.isFinite)),
        });
        """
    )
    assert report["finite"] is True
    assert report["bootstrap"]["envelopeRadius"] > 0
    assert report["gravity"]["acceleratedSystems"] >= 1
    assert report["gravity"]["acceleratedCoreNodes"] >= 1
    assert report["inwardAcceleration"] < 0
    assert report["coreInwardAcceleration"] < 0
    assert report["constrained"]["boundedCoreNodes"] >= 1
    assert report["constrained"]["boundedSystems"] >= 1
    assert report["externalRelativeAfterConstraint"] == pytest.approx(
        report["externalRelativeBefore"], abs=1e-10
    )
    # The exact inward cap must retain the tangential direction instead of stopping or
    # reversing the satellite. It intentionally does not speed it up to manufacture L.
    assert 0 < report["coreAngularAfterConstraint"] <= report["coreAngularBefore"]
    assert report["coreTangentAfterConstraint"] > 0
    assert report["coreAngularAfter"] > 0
    assert report["fixedHeld"]["boundedFixedSource"] >= 1
    assert report["fixedHeld"]["boundedFixedFollowers"] >= 1
    assert min(report["fixedHeldClearance"]) >= -1e-8
    assert abs(report["fixedHeldClearance"][0]) <= 1e-8
    assert report["maximumFixedReleaseStep"] <= 48
    assert all(
        math.hypot(phase[0], phase[1]) + radius <= report["finalEnvelope"] + 1e-8
        for phase, radius in zip(report["fixedAfterRelease"], [3, 2])
    )
    assert report["minimumClearance"] >= -1e-8
    assert report["maximumRadius"] <= report["finalEnvelope"] + 1e-8
    assert report["horizonClearance"] >= -1e-8
    assert report["maximumSpeed"] <= 24


@requires_node
def test_far_field_envelope_cache_survives_frozen_anchor() -> None:
    """Object.defineProperty silently fails on frozen nodes; the WeakMap cache must still pin
    the envelope so a late outward escape cannot make the permitted radius chase it."""
    report = _run_node(
        """
        const nodes = [
          { id: 'black-hole', anchor_role: 'global', community_id: 'core',
            gravity_mass: 64, radius: 12, x: 0, y: 0, vx: 0, vy: 0 },
          { id: 'inner', community_id: 'core', gravity_mass: 2,
            radius: 3, x: 40, y: 0, vx: 0, vy: 4 },
          { id: 'outer-star', community_id: 'outer', gravity_mass: 4,
            radius: 5, x: 90, y: 0, vx: 0, vy: 3 },
          { id: 'outer-moon', community_id: 'outer', gravity_mass: 1,
            radius: 3, x: 102, y: 6, vx: 0, vy: 5 },
        ];
        const anchor = nodes[0];
        const first = I.galaxyFarFieldEnvelope(nodes, {
          farFieldMinimumRadius: 96, farFieldEnvelopeScale: 1.25,
          farFieldSoftFraction: 0.82,
        });
        Object.freeze(anchor);
        const whileFrozen = I.galaxyFarFieldEnvelope(nodes, {
          farFieldMinimumRadius: 96, farFieldEnvelopeScale: 1.25,
          farFieldSoftFraction: 0.82,
        });
        nodes[2].x = first.envelopeRadius + 400;
        nodes[2].y = 0;
        nodes[3].x = first.envelopeRadius + 420;
        nodes[3].y = 0;
        const afterEscape = I.galaxyFarFieldEnvelope(nodes, {
          farFieldMinimumRadius: 96, farFieldEnvelopeScale: 1.25,
          farFieldSoftFraction: 0.82,
        });
        emit({
          initial: first.envelopeRadius,
          whileFrozen: whileFrozen.envelopeRadius,
          afterEscape: afterEscape.envelopeRadius,
          anchorFrozen: Object.isFrozen(anchor),
          finite: nodes.every(node =>
            [node.x, node.y, node.vx, node.vy].every(Number.isFinite)),
        });
        """
    )
    assert report["finite"] is True
    assert report["anchorFrozen"] is True
    assert report["initial"] > 0
    assert report["whileFrozen"] == pytest.approx(report["initial"], abs=1e-12)
    assert report["afterEscape"] == pytest.approx(report["initial"], abs=1e-12)

@requires_node
def test_pathological_oversized_system_stays_inside_the_black_hole_annulus() -> None:
    """The final annular pass must solve both edges after an impossible rigid outer fit."""
    report = _run_node(
        """
        const nodes = [
          { id: 'black-hole', anchor_role: 'global', community_id: 'core',
            gravity_mass: 64, radius: 12, x: 0, y: 0, vx: 0, vy: 0 },
          /* A heavy near member makes the external COM stay near the horizon while its light
             partner stretches far beyond the cached envelope. The rigid outer correction
             therefore carries this member through the black hole unless the final annulus
             alternates the two strict boundaries member-by-member. */
          { id: 'heavy-near', community_id: 'pathological', gravity_mass: 100,
            radius: 4, x: 40, y: 0, vx: 2, vy: 3 },
          { id: 'light-far', community_id: 'pathological', gravity_mass: 1,
            radius: 4, x: 80, y: 0, vx: 2, vy: -2 },
        ];
        const options = {
          gravity: 0, central: true, includeFarFieldConfinement: true,
          includeBlackHoleExclusion: true, blackHoleExclusionPadding: 2.5,
          includeCollisions: false, includeRelations: false,
          includeOrbitalSeparation: false, inwardConvergence: false,
          timestep: 0.021328125, wallClockSeconds: 1 / 30,
          velocityDecay: 0, speedLimit: 24, farFieldMinimumRadius: 80,
        };
        /* Cache a normal painted extent first; this emulates a late pathological deformation
           rather than allowing the anomalous member to enlarge the initial envelope. */
        const bootstrap = I.applyGalaxyFarFieldConfinement(nodes, options);
        const envelope = bootstrap.envelopeRadius;
        nodes[1].x = 20; nodes[1].y = 0; nodes[1].vx = 4; nodes[1].vy = 3;
        nodes[2].x = envelope + 300; nodes[2].y = 0; nodes[2].vx = 4; nodes[2].vy = -2;
        let minimumInner = Infinity, minimumOuter = Infinity;
        let oversized = 0, horizonContacts = 0, annulusInner = 0, annulusOuter = 0;
        let finalStep;
        for (let step = 0; step < 8; step++) {
          finalStep = I.integrateGalaxyLeapfrog(nodes, [], [], options);
          const far = finalStep.farFieldConfinement;
          oversized += far.boundedOversizedNodes;
          horizonContacts += finalStep.blackHoleExclusion.contacts;
          annulusInner += far.annulus.innerCorrectedNodes;
          annulusOuter += far.annulus.outerCorrectedNodes;
          nodes.slice(1).forEach(node => {
            const distance = Math.hypot(node.x - nodes[0].x, node.y - nodes[0].y);
            minimumInner = Math.min(minimumInner,
              distance - nodes[0].radius - node.radius - options.blackHoleExclusionPadding);
            minimumOuter = Math.min(minimumOuter,
              far.envelopeRadius - (distance + node.radius));
          });
        }
        emit({
          bootstrap, finalStep, envelope, oversized, horizonContacts, annulusInner, annulusOuter,
          minimumInner, minimumOuter,
          anchor: [nodes[0].x, nodes[0].y, nodes[0].vx, nodes[0].vy],
          finite: nodes.every(node => [node.x, node.y, node.vx, node.vy].every(Number.isFinite)),
          maximumSpeed: finalStep.maximumSpeed,
        });
        """
    )
    assert report["bootstrap"]["envelopeRadius"] > 0
    assert report["finite"] is True
    assert report["anchor"] == pytest.approx([0, 0, 0, 0], abs=1e-12)
    assert report["oversized"] > 0
    assert report["horizonContacts"] > 0
    assert report["minimumInner"] >= -1e-8
    assert report["minimumOuter"] >= -1e-8
    assert report["maximumSpeed"] <= 24


@requires_node
def test_final_outer_annulus_never_reopens_a_dominant_star_surface_overlap() -> None:
    """The final painted phase must satisfy the outer and local stellar bounds together."""
    report = _run_node(
        """
        const blackHole = { id: 'bh', anchor_role: 'global', community_id: 'core',
          gravity_mass: 20, radius: 10, x: 0, y: 0, vx: 0, vy: 0 };
        const nodes = [blackHole];
        const boundaryOptions = {
          includeFarFieldConfinement: true, farFieldEnvelopeScale: 1,
          farFieldMinimumRadius: 96, farFieldSoftFraction: 0.82,
          farFieldAcceleration: 12, farFieldMaxAcceleration: 16,
        };
        // Cache the 96-unit envelope before the late outer system appears.
        const bootstrap = I.applyGalaxyFarFieldConfinement(nodes, boundaryOptions);
        const star = { id: 'star', anchor_role: 'community', community_id: 'solar',
          system_anchor_id: 'star', orbit_tier: 0, gravity_mass: 8, radius: 5,
          x: 88, y: 0, vx: 0, vy: 0 };
        const planet = { id: 'planet', community_id: 'solar', system_anchor_id: 'star',
          orbit_tier: 1, gravity_mass: 1, radius: 3, x: 96, y: 0, vx: 0, vy: 0 };
        nodes.push(star, planet);
        const options = {
          ...boundaryOptions, gravity: 0, softening: 32, centralSoftening: 40,
          includeRelations: false, includeMutualSystems: false,
          includeOrbitalSeparation: false, includeCollisions: false,
          includeBlackHoleExclusion: true, blackHoleExclusionPadding: 2.5,
          systemAnchorExclusionPadding: 1.5,
          timestep: 0.032, wallClockSeconds: 1 / 30,
          inwardConvergence: false, velocityDecay: 0.00005, speedLimit: 48,
        };
        let tick, minimumActualStarClearance = Infinity, firstFrame = null;
        let totalBoundedSystems = 0, totalCorrectedDistance = 0;
        for (let step = 0; step < 12; step += 1) {
          tick = I.integrateGalaxyLeapfrog(nodes, [], [], options);
          const actualStarClearance = Math.hypot(planet.x - star.x, planet.y - star.y)
            - star.radius - planet.radius - options.systemAnchorExclusionPadding;
          minimumActualStarClearance = Math.min(
            minimumActualStarClearance, actualStarClearance);
          totalBoundedSystems += tick.farFieldConfinement.boundedSystems;
          totalCorrectedDistance += tick.farFieldConfinement.correctedDistance;
          if (step === 0) {
            firstFrame = {
              starClearance: actualStarClearance,
              reportedStarClearance: tick.systemAnchorExclusion.minimumClearance,
              blackHoleClearance: Math.min(...nodes.slice(1).map(node =>
                Math.hypot(node.x - blackHole.x, node.y - blackHole.y)
                  - blackHole.radius - node.radius - options.blackHoleExclusionPadding)),
              outerClearance: Math.min(...nodes.slice(1).map(node =>
                tick.farFieldConfinement.envelopeRadius
                  - Math.hypot(node.x - blackHole.x, node.y - blackHole.y) - node.radius)),
            };
          }
        }
        const starClearance = Math.hypot(planet.x - star.x, planet.y - star.y)
          - star.radius - planet.radius - options.systemAnchorExclusionPadding;
        const blackHoleClearance = Math.min(...nodes.slice(1).map(node =>
          Math.hypot(node.x - blackHole.x, node.y - blackHole.y)
            - blackHole.radius - node.radius - options.blackHoleExclusionPadding));
        const outerClearance = Math.min(...nodes.slice(1).map(node =>
          tick.farFieldConfinement.envelopeRadius
            - Math.hypot(node.x - blackHole.x, node.y - blackHole.y) - node.radius));
        emit({
          bootstrap: bootstrap.envelopeRadius,
          envelope: tick.farFieldConfinement.envelopeRadius,
          starClearance, minimumActualStarClearance, blackHoleClearance, outerClearance,
          firstFrame, totalBoundedSystems, totalCorrectedDistance,
          reportedStarClearance: tick.systemAnchorExclusion.minimumClearance,
          boundaryIterations: tick.systemAnchorExclusion.boundaryIterations,
          annulus: tick.farFieldConfinement.annulus,
          finite: nodes.every(node => [node.x, node.y, node.vx, node.vy]
            .every(Number.isFinite)),
        });
        """
    )
    assert report["bootstrap"] == report["envelope"] == pytest.approx(96)
    assert report["finite"] is True
    assert report["minimumActualStarClearance"] >= -1e-9, report
    assert report["firstFrame"]["starClearance"] >= -1e-9, report
    assert report["firstFrame"]["reportedStarClearance"] == pytest.approx(
        report["firstFrame"]["starClearance"], abs=1e-9
    )
    assert report["firstFrame"]["blackHoleClearance"] >= -1e-9
    assert report["firstFrame"]["outerClearance"] >= -1e-9
    assert report["starClearance"] >= -1e-9
    assert report["blackHoleClearance"] >= -1e-9
    assert report["outerClearance"] >= -1e-9
    assert report["reportedStarClearance"] == pytest.approx(
        report["starClearance"], abs=1e-9
    )
    assert report["boundaryIterations"] > 0
    assert report["totalBoundedSystems"] > 0
    assert report["totalCorrectedDistance"] > 0
    assert report["annulus"]["infeasibleNodes"] == 0


@requires_node
def test_black_hole_exclusion_preserves_system_orbits_at_the_painted_edge() -> None:
    report = _run_node(
        """
        const nodes = [
          { id: 'black-hole', anchor_role: 'global', community_id: 'core',
            x: 0, y: 0, vx: 0, vy: 0, radius: 12, gravity_mass: 64 },
          { id: 'core-satellite', community_id: 'core',
            x: 2, y: 0, vx: -4, vy: 7, radius: 3, gravity_mass: 1 },
          { id: 'outer-star', community_id: 'outer',
            x: 4, y: 0, vx: -3, vy: 2, radius: 4, gravity_mass: 4 },
          { id: 'outer-planet', community_id: 'outer',
            x: 8, y: 0, vx: -3, vy: 7, radius: 2, gravity_mass: 1 },
        ];
        const before = {
          diameter: Math.hypot(nodes[3].x - nodes[2].x, nodes[3].y - nodes[2].y),
          relativeVelocity: [nodes[3].vx - nodes[2].vx, nodes[3].vy - nodes[2].vy],
          coreTangent: nodes[1].vy,
          outerTangent: (nodes[2].vy * 4 + nodes[3].vy) / 5,
          coreAngular: nodes[1].x * nodes[1].vy - nodes[1].y * nodes[1].vx,
          outerAngular: ((nodes[2].x * 4 + nodes[3].x) / 5)
            * ((nodes[2].vy * 4 + nodes[3].vy) / 5)
            - ((nodes[2].y * 4 + nodes[3].y) / 5)
              * ((nodes[2].vx * 4 + nodes[3].vx) / 5),
        };
        const stats = I.applyGalaxyBlackHoleExclusion(nodes, { padding: 2.5 });
        const anchor = nodes[0];
        const clearances = nodes.slice(1).map(node => Math.hypot(
          node.x - anchor.x, node.y - anchor.y
        ) - anchor.radius - node.radius - 2.5);
        emit({
          stats,
          anchor: [anchor.x, anchor.y, anchor.vx, anchor.vy],
          clearances,
          core: [nodes[1].x, nodes[1].y, nodes[1].vx, nodes[1].vy],
          diameter: Math.hypot(nodes[3].x - nodes[2].x, nodes[3].y - nodes[2].y),
          relativeVelocity: [nodes[3].vx - nodes[2].vx, nodes[3].vy - nodes[2].vy],
          outerTangent: (nodes[2].vy * 4 + nodes[3].vy) / 5,
          coreAngular: nodes[1].x * nodes[1].vy - nodes[1].y * nodes[1].vx,
          outerAngular: ((nodes[2].x * 4 + nodes[3].x) / 5)
            * ((nodes[2].vy * 4 + nodes[3].vy) / 5)
            - ((nodes[2].y * 4 + nodes[3].y) / 5)
              * ((nodes[2].vx * 4 + nodes[3].vx) / 5),
          finite: nodes.every(node => [node.x, node.y, node.vx, node.vy].every(Number.isFinite)),
          before,
        });
        """
    )
    assert report["finite"] is True
    assert report["anchor"] == pytest.approx([0, 0, 0, 0], abs=1e-12)
    assert min(report["clearances"]) >= -1e-10
    assert report["stats"]["contacts"] == 2
    assert report["stats"]["systems"] == 1
    assert report["stats"]["coreNodes"] == 1
    assert report["stats"]["repelledNodes"] == 3
    assert report["stats"]["minimumClearance"] == pytest.approx(0, abs=1e-10)
    assert report["stats"]["inwardVelocityRemoved"] == pytest.approx(7, abs=1e-12)
    assert report["stats"]["tangentialVelocityRemoved"] > 0
    assert report["core"][2] == pytest.approx(0, abs=1e-12)
    assert 0 < report["core"][3] < report["before"]["coreTangent"]
    assert report["coreAngular"] == pytest.approx(report["before"]["coreAngular"], abs=1e-12)
    assert report["diameter"] == pytest.approx(report["before"]["diameter"], abs=1e-12)
    assert report["relativeVelocity"] == pytest.approx(
        report["before"]["relativeVelocity"], abs=1e-12
    )
    assert 0 < report["outerTangent"] < report["before"]["outerTangent"]
    assert report["outerAngular"] == pytest.approx(
        report["before"]["outerAngular"], abs=1e-12
    )


@requires_node
def test_link_and_orbital_separation_share_one_settling_target_without_jitter() -> None:
    report = _run_node(
        """
        const nodes = [
          { id: 'star', x: 0, y: 0, vx: 0, vy: 0, radius: 3,
            gravity_mass: 4, community_id: 'solar' },
          { id: 'planet', x: 10, y: 0, vx: 0, vy: 0, radius: 3,
            gravity_mass: 1, community_id: 'solar' },
        ];
        const links = [{ source: 'star', target: 'planet', rest_length: 20,
          spring_strength: 0.1 }];
        const options = {
          gravity: 0, central: false, timestep: 0.021328125, velocityDecay: 0.00005,
          speedLimit: 48, includeCollisions: false,
          includeRelations: true, includeRelationSprings: false, orbitScale: 0.25,
          relationStrengthMultiplier: 2, relationConstraintRate: 24,
          relationConstraintMaxCorrection: 12, relationPadding: 12,
          wallClockSeconds: 1 / 30,
          includeOrbitalSeparation: true, orbitalSeparationPadding: 12,
          orbitalSeparationStrength: 0.8, orbitalSeparationMaxCorrection: 4,
          orbitalSeparationMaxVelocityCorrection: 8, localRelativeSpeedLimit: 16,
          // This unannotated compatibility pair is a relation/separation convergence fixture,
          // not an explicit community-star stellar-pressure test.
          systemAnchorRepulsionAcceleration: 0,
        };
        const distances = [Math.hypot(nodes[1].x - nodes[0].x,
          nodes[1].y - nodes[0].y)];
        const corrections = [];
        let speedCaps = 0;
        for (let step = 0; step < 120; step++) {
          const tick = I.integrateGalaxyLeapfrog(nodes, links, [], options);
          distances.push(Math.hypot(nodes[1].x - nodes[0].x,
            nodes[1].y - nodes[0].y));
          corrections.push(tick.relationConstraint.correctedDistance
            + tick.orbitalSeparation.correctionDistance);
          speedCaps += tick.speedCapped ? 1 : 0;
        }
        emit({
          distances, corrections, speedCaps,
          finalVelocity: nodes.map(node => [node.vx, node.vy]),
          finite: nodes.every(node => [node.x, node.y, node.vx, node.vy]
            .every(Number.isFinite)),
        });
        """
    )
    assert report["finite"] is True
    assert report["speedCaps"] == 0
    assert all(
        current >= previous - 1e-10
        for previous, current in zip(report["distances"], report["distances"][1:])
    )
    assert report["distances"][-1] == pytest.approx(18, abs=1e-8)
    assert max(report["corrections"][-20:]) < report["corrections"][0] * 1e-6
    assert [value for velocity in report["finalVelocity"] for value in velocity] == pytest.approx(
        [0, 0, 0, 0], abs=1e-10
    )


@requires_node
def test_live_relation_constraints_skip_only_explicit_orbital_system_links() -> None:
    """Topology links within an explicit solar system must not overwrite orbital phase."""
    report = _run_node(
        """
        const fixture = () => [
          { id: 'star', community_id: 'solar', system_anchor_id: 'star', orbit_tier: 0,
            gravity_mass: 8, x: 0, y: 0 },
          { id: 'planet', community_id: 'solar', system_anchor_id: 'star', orbit_tier: 1,
            gravity_mass: 1, x: 30, y: 0 },
          // Same community but no explicit anchor metadata: a compatibility relation remains
          // eligible for the legacy Link constraint.
          { id: 'legacy-a', community_id: 'legacy', gravity_mass: 1, x: 0, y: 20 },
          { id: 'legacy-b', community_id: 'legacy', gravity_mass: 1, x: 30, y: 20 },
        ];
        const links = [
          { source: 'star', target: 'planet', rest_length: 10, spring_strength: 0.2 },
          { source: 'legacy-a', target: 'legacy-b', rest_length: 10, spring_strength: 0.2 },
        ];
        const run = skipOrbitalSystemRelations => {
          const nodes = fixture();
          const before = nodes.map(node => [node.x, node.y]);
          const stats = I.applyGalaxyRelationDistanceConstraints(nodes, links, {
            orbitScale: 1, rate: 24, wallClockSeconds: 1 / 30, maxCorrection: 12,
            skipOrbitalSystemRelations,
          });
          return { stats, before, after: nodes.map(node => [node.x, node.y]) };
        };
        emit({ live: run(true), legacy: run(false) });
        """
    )
    live, legacy = report["live"], report["legacy"]
    assert live["stats"]["skippedOrbitalSystem"] == 1
    assert live["stats"]["applied"] == 1
    for actual, expected in zip(live["after"][:2], live["before"][:2]):
        assert actual == pytest.approx(expected)
    assert any(actual != pytest.approx(expected)
               for actual, expected in zip(live["after"][2:], live["before"][2:]))
    # Direct helper callers retain the compatibility behavior until they opt into the live
    # orbital-system guard; both relations are then eligible.
    assert legacy["stats"]["skippedOrbitalSystem"] == 0
    assert legacy["stats"]["applied"] == 2
    assert any(actual != pytest.approx(expected)
               for actual, expected in zip(legacy["after"][:2], legacy["before"][:2]))


@requires_node
def test_dense_hub_constraints_are_simultaneous_order_independent_and_bounded() -> None:
    report = _run_node(
        """
        const make = () => {
          const nodes = [{ id: 'hub', x: 0, y: 0, vx: 0, vy: 0,
            gravity_mass: 12, radius: 8, community_id: 'dense' }];
          for (let index = 0; index < 24; index++) nodes.push({
            id: 'leaf-' + index, x: 90 + index * 0.2, y: -18 + index * 1.5,
            vx: 0, vy: 0, gravity_mass: 1, radius: 2, community_id: 'dense',
          });
          return nodes;
        };
        const links = Array.from({ length: 24 }, (_, index) => ({
          source: 'hub', target: 'leaf-' + index,
          rest_length: 20, spring_strength: 0.1,
        }));
        const run = reverse => {
          const nodes = make();
          const beforeCom = nodes.reduce((sum, node) => ({
            x: sum.x + node.gravity_mass * node.x,
            y: sum.y + node.gravity_mass * node.y,
            mass: sum.mass + node.gravity_mass,
          }), { x: 0, y: 0, mass: 0 });
          const stats = I.applyGalaxyRelationDistanceConstraints(
            nodes, reverse ? [...links].reverse() : links,
            { orbitScale: 0.25, strengthMultiplier: 2,
              wallClockSeconds: 1 / 30, rate: 24, maxCorrection: 12, padding: 12 }
          );
          const afterCom = nodes.reduce((sum, node) => ({
            x: sum.x + node.gravity_mass * node.x,
            y: sum.y + node.gravity_mass * node.y,
            mass: sum.mass + node.gravity_mass,
          }), { x: 0, y: 0, mass: 0 });
          return {
            phase: Object.fromEntries(nodes.map(node => [node.id, [node.x, node.y]])),
            before: [beforeCom.x / beforeCom.mass, beforeCom.y / beforeCom.mass],
            after: [afterCom.x / afterCom.mass, afterCom.y / afterCom.mass],
            stats,
          };
        };
        emit({ forward: run(false), reverse: run(true) });
        """
    )
    assert report["forward"]["stats"]["applied"] == 24
    assert report["forward"]["stats"]["aggregateLimited"] is True
    assert report["forward"]["stats"]["maximumNodeShift"] == pytest.approx(12)
    assert report["forward"]["after"] == pytest.approx(report["forward"]["before"], abs=1e-12)
    assert report["reverse"]["after"] == pytest.approx(report["reverse"]["before"], abs=1e-12)
    for node_id, phase in report["forward"]["phase"].items():
        assert report["reverse"]["phase"][node_id] == pytest.approx(phase, abs=1e-12)


@requires_node
def test_dense_orbital_contacts_and_hot_members_receive_one_bounded_system_update() -> None:
    report = _run_node(
        """
        const nodes = [{ id: 'hub', x: 0, y: 0, vx: 0, vy: 0,
          gravity_mass: 12, radius: 8, community_id: 'dense' }];
        for (let index = 0; index < 20; index++) {
          const angle = index / 20 * Math.PI * 2;
          nodes.push({ id: 'leaf-' + index,
            x: Math.cos(angle) * 6, y: Math.sin(angle) * 6,
            vx: -Math.sin(angle) * (index === 3 ? 90 : 4),
            vy: Math.cos(angle) * (index === 3 ? 90 : 4),
            gravity_mass: 1, radius: 2, community_id: 'dense' });
        }
        const beforeCom = nodes.reduce((sum, node) => ({
          x: sum.x + node.gravity_mass * node.x,
          y: sum.y + node.gravity_mass * node.y,
          mass: sum.mass + node.gravity_mass,
        }), { x: 0, y: 0, mass: 0 });
        const separation = I.applyGalaxyOrbitalSeparation(nodes, {
          padding: 12, strength: 0.8, maxCorrection: 4, maxVelocityCorrection: 8,
        });
        const afterPositionCom = nodes.reduce((sum, node) => ({
          x: sum.x + node.gravity_mass * node.x,
          y: sum.y + node.gravity_mass * node.y,
          mass: sum.mass + node.gravity_mass,
        }), { x: 0, y: 0, mass: 0 });
        const beforeMomentum = nodes.reduce((sum, node) => ({
          x: sum.x + node.gravity_mass * node.vx,
          y: sum.y + node.gravity_mass * node.vy,
        }), { x: 0, y: 0 });
        const velocity = I.stabilizeGalaxySystemVelocities(nodes, { limit: 16 });
        const afterMomentum = nodes.reduce((sum, node) => ({
          x: sum.x + node.gravity_mass * node.vx,
          y: sum.y + node.gravity_mass * node.vy,
        }), { x: 0, y: 0 });
        const mass = beforeCom.mass;
        const centerVx = afterMomentum.x / mass, centerVy = afterMomentum.y / mass;
        emit({ separation, velocity,
          positionComBefore: [beforeCom.x / mass, beforeCom.y / mass],
          positionComAfter: [afterPositionCom.x / mass, afterPositionCom.y / mass],
          momentumBefore: beforeMomentum, momentumAfter: afterMomentum,
          maximumFinalRelativeSpeed: Math.max(...nodes.map(node =>
            Math.hypot(node.vx - centerVx, node.vy - centerVy))),
          finite: nodes.every(node => [node.x, node.y, node.vx, node.vy]
            .every(Number.isFinite)),
        });
        """
    )
    assert report["finite"] is True
    assert report["separation"]["overlaps"] > 20
    assert report["separation"]["aggregateLimited"] is True
    assert report["separation"]["maximumNodeShift"] <= 4 + 1e-12
    assert report["separation"]["maximumVelocityShift"] <= 8 + 1e-12
    assert report["positionComAfter"] == pytest.approx(report["positionComBefore"], abs=1e-12)
    assert report["velocity"]["limitedSystems"] == 1
    assert report["maximumFinalRelativeSpeed"] == pytest.approx(16, abs=1e-10)
    assert [report["momentumAfter"]["x"], report["momentumAfter"]["y"]] == pytest.approx(
        [report["momentumBefore"]["x"], report["momentumBefore"]["y"]], abs=1e-10
    )


@requires_node
def test_release_sized_dense_galaxy_never_reheats_or_ping_pongs_at_slider_extremes() -> None:
    """The 542-body release shape stays contractive at both ordinary and 120/80 tuning.

    Endpoint displacement did not catch the regression: over-unity cross-system contact could
    kick a solar-system COM one direction and project it back on the next frame while ending in
    a plausible place.  Sample every fixed step and require bounded radii/energy, signed phase,
    painted clearances, and a low per-system COM-step tail for six seconds of solver time.
    """
    report = _run_node(
        """
        const make = () => {
          const nodes = [{ id: 'black-hole', anchor_role: 'global', community_id: 'core',
            system_anchor_id: 'black-hole', orbit_tier: 0, gravity_mass: 64, radius: 8,
            x: 0, y: 0, vx: 0, vy: 0 },
          { id: 'core-star', community_id: 'core', system_anchor_id: 'black-hole',
            orbit_tier: 1, gravity_mass: 6, radius: 5, x: 52, y: 0, vx: 0, vy: 0 }];
          const links = [{ source: 'black-hole', target: 'core-star', rest_length: 52,
            spring_strength: 0.08 }];
          for (let system = 0; system < 60; system++) {
            const id = system === 0 ? 'aurora' : 'system-' + system;
            const starId = id + '-star';
            const phase = 0.31 + system * 2.399963229728653;
            const galacticRadius = 112 + system * 3.15;
            const centerX = Math.cos(phase) * galacticRadius;
            const centerY = Math.sin(phase) * galacticRadius * 0.84;
            for (let member = 0; member < 9; member++) {
              const localRadius = member === 0 ? 0 : (member === 1 ? 40 : 18 + member * 5);
              const localPhase = phase + member * 2.399963229728653;
              const nodeId = member === 0 ? starId
                : (member === 1 ? id + '-planet' : id + '-planet-' + member);
              nodes.push({ id: nodeId, community_id: id,
                anchor_role: member === 0 ? 'community' : 'none',
                system_anchor_id: starId, orbit_tier: member,
                gravity_mass: member === 0 ? 8 + system % 5 : 1 + (member % 3) * 0.25,
                radius: member === 0 ? 5.5 : 2.5,
                x: centerX + Math.cos(localPhase) * localRadius,
                y: centerY + Math.sin(localPhase) * localRadius, vx: 0, vy: 0 });
              if (member > 0) links.push({ source: starId, target: nodeId,
                rest_length: localRadius, spring_strength: 0.08 });
            }
          }
          return { nodes, links };
        };
        const quantile = (items, portion) => {
          const values = [...items].sort((a, b) => a - b);
          return values[Math.floor((values.length - 1) * portion)];
        };
        const delta = (next, previous) => Math.atan2(
          Math.sin(next - previous), Math.cos(next - previous));
        const run = (repel, link) => {
          const { nodes, links } = make();
          I.seedGalaxyOrbits(nodes, 3031, 48, 32, false);
          // Match galaxyIntegratorOptions(): Repel 60 yields live central softening 48.
          I.seedGalaxySystemOrbits(nodes, 3031, 48, 48, false);
          const separationPadding = I.galaxyOrbitalSeparationPadding(repel);
          const separationStrength = I.galaxyOrbitalSeparationStrength(repel);
          const options = {
            gravity: 48, softening: 32, centralSoftening: 48,
            exactLimit: 64, theta: 0.85,
            localPairFraction: 0.15, corePairMultiplier: 0.75,
            includeBridges: false, includeMutualSystems: true,
            mutualSystemGravityFraction: 0.12, mutualSystemSoftening: 80,
            includeRelations: true, includeRelationSprings: false,
            skipSystemAnchorRelations: true, skipOrbitalSystemRelations: true,
            orbitScale: I.galaxyRelationOrbitScale(link),
            relationConstraintStrengthMultiplier: 2,
            relationConstraintResponseMultiplier: 1,
            relationConstraintRate: 24, relationConstraintMaxCorrection: 12,
            relationPadding: Math.max(1.5, separationPadding),
            includeOrbitalSeparation: true,
            orbitalSeparationPadding: separationPadding,
            orbitalSeparationStrength: separationStrength,
            crossCommunitySeparationPadding: 1.5,
            crossCommunitySeparationStrength: separationStrength * 0.18,
            orbitalSeparationMaxCorrection: 4,
            orbitalSeparationMaxVelocityCorrection: 8,
            preserveLocalTangentialVelocity: true, preserveSystemRadii: true,
            skipSystemAnchorPairs: true, systemAnchorExclusionPadding: 1.5,
            systemAnchorRepulsionRange: 6, systemAnchorRepulsionAcceleration: 0.12,
            includeBlackHoleExclusion: true, blackHoleExclusionPadding: 2.5,
            includeFarFieldConfinement: true, farFieldEnvelopeScale: 1.75,
            farFieldMinimumRadius: 96, farFieldSoftFraction: 0.82,
            farFieldAcceleration: 12, farFieldMaxAcceleration: 16,
            localRelativeSpeedLimit: 48, timestep: 0.032,
            inwardConvergence: true, wallClockSeconds: 1 / 30,
            velocityDecay: 0.00005, speedLimit: 48, includeCollisions: false,
          };
          const byId = new Map(nodes.map(node => [node.id, node]));
          const tracked = ['aurora', 'system-11', 'system-23', 'system-35',
            'system-47', 'system-59'];
          const local = new Map(tracked.map(id => {
            const star = byId.get(id + '-star'), planet = byId.get(
              id === 'aurora' ? 'aurora-planet' : id + '-planet');
            const dx = planet.x - star.x, dy = planet.y - star.y;
            const dvx = planet.vx - star.vx, dvy = planet.vy - star.vy;
            return [id, { star, planet, radius0: Math.hypot(dx, dy),
              radiusMin: Math.hypot(dx, dy), radiusMax: Math.hypot(dx, dy),
              angle: Math.atan2(dy, dx), direction: Math.sign(dx * dvy - dy * dvx),
              reversals: 0, maxPhaseStep: 0, radialReversals: 0,
              previousRadius: Math.hypot(dx, dy), previousRadial: 0,
              kinetic0: 0.5 * star.gravity_mass * planet.gravity_mass
                / (star.gravity_mass + planet.gravity_mass) * (dvx * dvx + dvy * dvy),
              kineticMin: Infinity, kineticMax: 0 }];
          }));
          const centers = () => I.communityCenters(nodes);
          let previousCenters = centers();
          const globalTracks = new Map(tracked.map(id => {
            const center = previousCenters.get(id), radius = Math.hypot(center.x, center.y);
            const vx = center.nodes.reduce((sum, node) => sum
              + node.gravity_mass * node.vx, 0) / center.mass;
            const vy = center.nodes.reduce((sum, node) => sum
              + node.gravity_mass * node.vy, 0) / center.mass;
            return [id, { angle: Math.atan2(center.y, center.x),
              direction: Math.sign(center.x * vy - center.y * vx),
              radius0: radius, radiusMin: radius, radiusMax: radius,
              reversals: 0, maxPhaseStep: 0 }];
          }));
          const comSteps = [], crossCorrections = [];
          let speedCaps = 0, localVelocityLimits = 0, maximumSpeed = 0;
          let minimumBlackHoleClearance = Infinity, minimumStarClearance = Infinity;
          let minimumOuterClearance = Infinity, maximumOrbitalShift = 0;
          let alternatingRadialSteps = 0, relationApplications = 0;
          for (let step = 0; step < 180; step++) {
            const tick = I.integrateGalaxyLeapfrog(nodes, links, [], options);
            speedCaps += tick.speedCapped ? 1 : 0;
            localVelocityLimits += tick.systemVelocity.limitedSystems;
            maximumSpeed = Math.max(maximumSpeed, tick.maximumSpeed);
            maximumOrbitalShift = Math.max(maximumOrbitalShift,
              tick.orbitalSeparation.maximumNodeShift || 0);
            crossCorrections.push(tick.orbitalSeparation.crossCommunityCorrectionDistance || 0);
            relationApplications += tick.relationConstraint.applied || 0;
            const nextCenters = centers();
            nextCenters.forEach((center, id) => {
              if (id === 'core') return;
              const previous = previousCenters.get(id);
              if (previous) comSteps.push(Math.hypot(center.x - previous.x, center.y - previous.y));
            });
            tracked.forEach(id => {
              const item = local.get(id), star = item.star, planet = item.planet;
              const dx = planet.x - star.x, dy = planet.y - star.y;
              const radius = Math.hypot(dx, dy), angle = Math.atan2(dy, dx);
              const phaseStep = delta(angle, item.angle);
              if (item.direction && Math.sign(phaseStep) === -item.direction
                && Math.abs(phaseStep) > 0.001) item.reversals++;
              item.maxPhaseStep = Math.max(item.maxPhaseStep, Math.abs(phaseStep));
              const radialStep = radius - item.previousRadius;
              if (item.previousRadial * radialStep < -0.0025) item.radialReversals++;
              if (item.previousRadial * radialStep < -0.0025) alternatingRadialSteps++;
              item.previousRadial = radialStep;
              item.previousRadius = radius;
              item.radiusMin = Math.min(item.radiusMin, radius);
              item.radiusMax = Math.max(item.radiusMax, radius);
              item.angle = angle;
              const dvx = planet.vx - star.vx, dvy = planet.vy - star.vy;
              const kinetic = 0.5 * star.gravity_mass * planet.gravity_mass
                / (star.gravity_mass + planet.gravity_mass) * (dvx * dvx + dvy * dvy);
              item.kineticMin = Math.min(item.kineticMin, kinetic);
              item.kineticMax = Math.max(item.kineticMax, kinetic);
              minimumStarClearance = Math.min(minimumStarClearance,
                radius - star.radius - planet.radius - 1.5);
              const center = nextCenters.get(id), global = globalTracks.get(id);
              const globalRadius = Math.hypot(center.x, center.y);
              const globalStep = delta(Math.atan2(center.y, center.x), global.angle);
              if (global.direction && Math.sign(globalStep) === -global.direction
                && Math.abs(globalStep) > 0.001) global.reversals++;
              global.maxPhaseStep = Math.max(global.maxPhaseStep, Math.abs(globalStep));
              global.radiusMin = Math.min(global.radiusMin, globalRadius);
              global.radiusMax = Math.max(global.radiusMax, globalRadius);
              global.angle = Math.atan2(center.y, center.x);
            });
            const envelope = tick.farFieldConfinement.envelopeRadius;
            nodes.slice(1).forEach(node => {
              minimumBlackHoleClearance = Math.min(minimumBlackHoleClearance,
                Math.hypot(node.x, node.y) - nodes[0].radius - node.radius - 2.5);
              minimumOuterClearance = Math.min(minimumOuterClearance,
                envelope - Math.hypot(node.x, node.y) - node.radius);
            });
            previousCenters = nextCenters;
          }
          return {
            repel, link, separationStrength,
            crossStrength: separationStrength * 0.18,
            local: Object.fromEntries([...local].map(([id, item]) => [id, {
              radius0: item.radius0, radiusMin: item.radiusMin, radiusMax: item.radiusMax,
              reversals: item.reversals, radialReversals: item.radialReversals,
              maxPhaseStep: item.maxPhaseStep, kinetic0: item.kinetic0,
              kineticMin: item.kineticMin, kineticMax: item.kineticMax }])),
            global: Object.fromEntries(globalTracks),
            comStepMedian: quantile(comSteps, 0.5), comStepP95: quantile(comSteps, 0.95),
            comStepMax: Math.max(...comSteps),
            crossCorrectionP95: quantile(crossCorrections, 0.95),
            crossCorrectionMax: Math.max(...crossCorrections),
            speedCaps, localVelocityLimits, maximumSpeed, maximumOrbitalShift,
            alternatingRadialSteps, relationApplications,
            minimumBlackHoleClearance, minimumStarClearance, minimumOuterClearance,
            finite: nodes.every(node => [node.x, node.y, node.vx, node.vy]
              .every(Number.isFinite)),
          };
        };
        emit({ ordinary: run(60, 8), maximum: run(120, 80) });
        """
    )
    for trial in report.values():
        assert trial["finite"] is True
        assert trial["separationStrength"] == pytest.approx(1)
        # This is the release bug's exact oracle: pressure 0.36 crossed the contact manifold.
        assert trial["crossStrength"] == pytest.approx(0.18)
        assert trial["speedCaps"] == 0
        assert trial["localVelocityLimits"] == 0
        assert trial["maximumSpeed"] < 48
        assert trial["maximumOrbitalShift"] <= 4 + 1e-9
        assert trial["relationApplications"] == 0
        assert trial["minimumBlackHoleClearance"] >= -1e-8
        assert trial["minimumStarClearance"] >= -1e-8
        assert trial["minimumOuterClearance"] >= -1e-8
        assert trial["comStepP95"] < 1.25, trial
        assert trial["comStepMax"] < 3, trial
        assert trial["crossCorrectionP95"] < 500, trial
        assert trial["crossCorrectionMax"] < 900, trial
        # Sparse eccentric perturbations are physical; the regression was frame-to-frame
        # reversal across many systems. Across 1,080 tracked phase slices allow at most two.
        assert sum(system["reversals"] for system in trial["local"].values()) <= 2
        for system in trial["local"].values():
            assert system["reversals"] <= 2
            assert system["radialReversals"] <= 12
            # 0.085 rad is 4.9 degrees per fixed slice. The unstable response reached
            # 0.10415 here; retain margin for floating-point ordering without admitting it.
            assert system["maxPhaseStep"] < 0.085
            assert system["radiusMin"] > system["radius0"] * 0.65
            assert system["radiusMax"] < system["radius0"] * 1.35
            assert system["kineticMin"] > system["kinetic0"] * 0.15
            assert system["kineticMax"] < system["kinetic0"] * 4
        for system_id, system in trial["global"].items():
            # A crowded galaxy may receive an occasional genuine near-field perturbation;
            # four or fewer opposite samples in 180 slices is not the frame-to-frame ping-pong
            # produced by the former over-unity contact response.
            assert system["reversals"] <= 4, (system_id, system, {
                key: trial[key] for key in ("repel", "link", "comStepMedian",
                                            "comStepP95", "comStepMax")
            })
            assert system["maxPhaseStep"] < 0.08
            assert system["radiusMin"] > system["radius0"] * 0.6
            assert system["radiusMax"] <= system["radius0"] * 1.01


@requires_node
def test_drag_follow_uses_softened_source_mass_gravity_and_preserves_tangent() -> None:
    report = _run_node(
        """
        const run = ({ mass = 12, distance = 60, gravity = 48 } = {}) => {
          const source = { id: 'star', x: 0, y: 0, vx: 0, vy: 0,
            radius: 2, gravity_mass: mass, community_id: 'solar' };
          const follower = { id: 'planet', x: distance, y: 0, vx: 0, vy: 3,
            radius: 2, gravity_mass: 1, community_id: 'solar' };
          const remote = { id: 'remote', x: 200, y: 40, vx: 2, vy: -1,
            radius: 2, gravity_mass: 1, community_id: 'remote' };
          const beforeRemote = [remote.x, remote.y, remote.vx, remote.vy];
          const stats = I.applyDraggedNodeGravity(source, [{
            node: follower,
            link: { source: 'star', target: 'planet', rest_length: 20,
              spring_strength: 0.1 },
          }, { node: remote, link: null, proximity: 'field' }], {
            gravity, linkSetting: 8, softening: 12, duration: 6,
            maximumPull: 36, maximumImpulse: 8, padding: 1.5 });
          return {
            follower: [follower.x, follower.y, follower.vx, follower.vy],
            remote: [remote.x, remote.y, remote.vx, remote.vy],
            beforeRemote, stats,
          };
        };
        const coincidentSource = { id: 'same-star', x: 0, y: 0,
          gravity_mass: 12, community_id: 'same' };
        const coincident = { id: 'same-planet', x: 0, y: 0, vx: 1, vy: 2,
          gravity_mass: 1, community_id: 'same' };
        const coincidentStats = I.applyDraggedNodeGravity(coincidentSource,
          [{ node: coincident }], { gravity: 100 });
        emit({
          heavy: run(), light: run({ mass: 6 }),
          near: run({ distance: 60 }), far: run({ distance: 120 }),
          zero: run({ gravity: 0 }),
          coincident: [coincident.x, coincident.y, coincident.vx, coincident.vy],
          coincidentStats,
        });
        """
    )
    assert report["heavy"]["stats"]["applied"] == 2
    assert report["heavy"]["stats"]["maximumAcceleration"] == pytest.approx(
        report["light"]["stats"]["maximumAcceleration"] * 2, rel=1e-12
    )
    assert report["near"]["stats"]["maximumAcceleration"] > report["far"]["stats"][
        "maximumAcceleration"
    ]
    assert report["near"]["stats"]["maximumPull"] <= 36
    assert report["far"]["stats"]["maximumPull"] <= 36
    assert report["heavy"]["follower"][0] < 60
    assert report["heavy"]["follower"][2] < 0
    assert report["heavy"]["follower"][3] == pytest.approx(3)
    assert report["heavy"]["remote"] != report["heavy"]["beforeRemote"]
    assert report["heavy"]["remote"][0] < report["heavy"]["beforeRemote"][0]
    assert report["heavy"]["remote"][1] < report["heavy"]["beforeRemote"][1]
    assert report["zero"]["follower"] == pytest.approx([60, 0, 0, 3])
    assert report["coincident"] == pytest.approx([0, 0, 1, 2])
    assert report["coincidentStats"]["applied"] == 0


@requires_node
def test_live_drag_force_is_fixed_step_acceleration_not_pointer_displacement() -> None:
    report = _run_node(
        """
        const primary = { id: 'star', x: 0, y: 0, vx: 0, vy: 0,
          radius: 2, gravity_mass: 12, community_id: 'solar' };
        const follower = { id: 'planet', x: 60, y: 0, vx: 0, vy: 3,
          radius: 2, gravity_mass: 1, community_id: 'solar' };
        const before = [follower.x, follower.y, follower.vx, follower.vy];
        const stats = I.applyDraggedNodeAcceleration(primary, [{ node: follower }], {
          gravity: 48, softening: 12,
        });
        const expected = I.galaxyLocalGravityConstant(48) * 2 * 12 * 60
          / Math.pow(60 * 60 + 12 * 12, 1.5);
        emit({ before, after: [follower.x, follower.y, follower.vx, follower.vy],
          stats, expected });
        """
    )
    assert report["stats"]["applied"] == 1
    assert report["stats"]["maximumPull"] == 0
    assert report["stats"]["maximumAcceleration"] == pytest.approx(
        report["expected"], rel=1e-12
    )
    assert report["after"][:2] == report["before"][:2]
    assert report["after"][2] == pytest.approx(-report["expected"])
    assert report["after"][3] == pytest.approx(report["before"][3])


@requires_node
def test_connected_galaxy_drag_keeps_followers_and_unrelated_systems_bounded() -> None:
    """A cursor-owned source obeys painted bounds without turning bodies into projectiles."""
    report = _run_node(
        """
        const nodes = [
          { id: 'black-hole', anchor_role: 'global', community_id: 'core',
            gravity_mass: 64, radius: 12, x: 0, y: 0, vx: 0, vy: 0 },
          { id: 'dragged', community_id: 'cursor', gravity_mass: 8, radius: 4,
            x: 100, y: 0, vx: 0, vy: 0 },
          { id: 'follower-a', community_id: 'follower-a', gravity_mass: 2, radius: 3,
            x: 132, y: 0, vx: 0, vy: 2 },
          { id: 'follower-b', community_id: 'follower-b', gravity_mass: 2, radius: 3,
            x: 112, y: 30, vx: -1, vy: 1 },
          { id: 'remote-star', community_id: 'remote', gravity_mass: 5, radius: 4,
            x: -130, y: 30, vx: 0, vy: -2 },
          { id: 'remote-moon', community_id: 'remote', gravity_mass: 1, radius: 2,
            x: -112, y: 36, vx: 1, vy: -1 },
        ];
        const links = [
          { source: 'dragged', target: 'follower-a', rest_length: 30, spring_strength: 0.1 },
          { source: 'dragged', target: 'follower-b', rest_length: 30, spring_strength: 0.1 },
        ];
        const common = {
          gravity: 48, central: true, includeFarFieldConfinement: true,
          includeBlackHoleExclusion: true, blackHoleExclusionPadding: 2.5,
          includeMutualSystems: true, mutualSystemGravityFraction: 0.12,
          mutualSystemSoftening: 80, includeCollisions: false,
          includeRelations: true, includeRelationSprings: true,
          orbitScale: 0.25, relationStrengthMultiplier: 2,
          relationConstraintRate: 24, relationConstraintMaxCorrection: 12,
          relationPadding: 12, includeOrbitalSeparation: true,
          orbitalSeparationPadding: 12, orbitalSeparationStrength: 0.8,
          crossCommunitySeparationPadding: 1.5, crossCommunitySeparationStrength: 0.144,
          orbitalSeparationMaxCorrection: 4, orbitalSeparationMaxVelocityCorrection: 8,
          localRelativeSpeedLimit: 16, timestep: 0.021328125,
          wallClockSeconds: 1 / 30, velocityDecay: 0.00005, speedLimit: 24,
        };
        /* Establish the cached envelope, then make a gradual cursor path that crosses it. */
        I.applyGalaxyFarFieldConfinement(nodes, common);
        const envelope = I.galaxyFarFieldEnvelope(nodes, common).envelopeRadius;
        const dragged = nodes[1], followerA = nodes[2], followerB = nodes[3];
        dragged.x = envelope - 100; dragged.y = 0;
        followerA.x = envelope - 68; followerA.y = 0;
        followerB.x = envelope - 88; followerB.y = 30;
        const targets = [
          [envelope - 70, 0], [envelope - 35, 15], [envelope + 5, 20],
          [envelope + 45, 10], [envelope + 80, -5],
        ];
        const followers = [
          { node: followerA, link: links[0] }, { node: followerB, link: links[1] },
        ];
        let finite = true, maximumSpeed = 0, maximumFollowerStep = 0;
        let maximumLinkDistance = 0, maximumRemoteRadius = 0, maximumRemoteStep = 0;
        let dragAcceleration = 0, dragPull = 0;
        let requestedBeyondEnvelope = false, minimumSourceOuterClearance = Infinity;
        let sourceEdgeContact = false;
        for (const [x, y] of targets) {
          const beforeFollowers = [followerA, followerB].map(node => [node.x, node.y]);
          const beforeRemote = nodes.slice(4).map(node => [node.x, node.y]);
          dragged.x = x; dragged.y = y; dragged.vx = 0; dragged.vy = 0;
          const tick = I.integrateGalaxyLeapfrog(nodes, links, [], {
            ...common, fixedNodeId: 'dragged', dragSource: dragged, dragFollowers: followers,
          });
          requestedBeyondEnvelope = requestedBeyondEnvelope
            || Math.hypot(x, y) + dragged.radius > envelope + 1e-8;
          const sourceClearance = envelope - (Math.hypot(dragged.x, dragged.y) + dragged.radius);
          minimumSourceOuterClearance = Math.min(minimumSourceOuterClearance, sourceClearance);
          sourceEdgeContact = sourceEdgeContact || Math.abs(sourceClearance) <= 1e-8;
          dragAcceleration = Math.max(dragAcceleration, tick.dragGravity.maximumAcceleration);
          dragPull = Math.max(dragPull, tick.dragGravity.maximumPull);
          maximumSpeed = Math.max(maximumSpeed, tick.maximumSpeed);
          [followerA, followerB].forEach((node, index) => {
            maximumFollowerStep = Math.max(maximumFollowerStep,
              Math.hypot(node.x - beforeFollowers[index][0], node.y - beforeFollowers[index][1]));
          });
          links.forEach(link => {
            const source = nodes.find(node => node.id === link.source);
            const target = nodes.find(node => node.id === link.target);
            maximumLinkDistance = Math.max(maximumLinkDistance,
              Math.hypot(source.x - target.x, source.y - target.y));
          });
          nodes.slice(4).forEach((node, index) => {
            maximumRemoteRadius = Math.max(maximumRemoteRadius,
              Math.hypot(node.x, node.y) + node.radius);
            maximumRemoteStep = Math.max(maximumRemoteStep,
              Math.hypot(node.x - beforeRemote[index][0], node.y - beforeRemote[index][1]));
          });
          finite = finite && nodes.every(node => [node.x, node.y, node.vx, node.vy]
            .every(Number.isFinite));
        }
        const held = [dragged.x, dragged.y];
        let releaseSpeed = 0;
        for (let step = 0; step < 20; step++) {
          const tick = I.integrateGalaxyLeapfrog(nodes, links, [], common);
          releaseSpeed = Math.max(releaseSpeed, tick.maximumSpeed);
          finite = finite && nodes.every(node => [node.x, node.y, node.vx, node.vy]
            .every(Number.isFinite));
        }
        emit({
          envelope, requestedBeyondEnvelope, minimumSourceOuterClearance, sourceEdgeContact,
          finite, maximumSpeed, releaseSpeed,
          maximumFollowerStep, maximumLinkDistance, maximumRemoteRadius, maximumRemoteStep,
          dragAcceleration, dragPull, held, released: [dragged.x, dragged.y],
        });
        """
    )
    assert report["requestedBeyondEnvelope"] is True
    assert report["minimumSourceOuterClearance"] >= -1e-8
    assert report["sourceEdgeContact"] is True
    assert report["finite"] is True
    assert report["dragAcceleration"] > 0
    assert report["dragPull"] > 0
    assert report["maximumSpeed"] <= 24
    assert report["releaseSpeed"] <= 24
    # Fixed geometry and the relation cap limit every cursor sample; neither link may run away.
    assert report["maximumFollowerStep"] <= 48
    assert report["maximumLinkDistance"] <= 180
    assert report["maximumRemoteRadius"] <= report["envelope"] + 1e-8
    assert report["maximumRemoteStep"] <= 32
    # Removing fixedNodeId/dragSource lets the former cursor point resume normal physics.
    assert math.dist(report["held"], report["released"]) > 1e-4


@requires_node
@pytest.mark.parametrize(
    ("drag_community", "expect_fixed_system_nodes"),
    [("core", False), ("drag-system", True)],
)
def test_dragging_connected_core_node_over_black_hole_keeps_the_annulus_stable(
    drag_community: str, expect_fixed_system_nodes: bool,
) -> None:
    """The pointer may target the hole centre, but its painted body cannot cover it."""
    report = _run_node(
        "const dragCommunity = " + repr(drag_community)
        + ";\nconst externalSystem = " + ("true" if expect_fixed_system_nodes else "false")
        + ";\n" + """
        const nodes = [
          { id: 'black-hole', anchor_role: 'global', community_id: 'core',
            gravity_mass: 64, radius: 12, x: 0, y: 0, vx: 0, vy: 0 },
          { id: 'dragged', community_id: dragCommunity, gravity_mass: 8, radius: 4,
            x: 0, y: 0, vx: 0, vy: 0 },
          { id: 'core-follower-a', community_id: dragCommunity, gravity_mass: 2, radius: 3,
            x: 26, y: 0, vx: 0, vy: 2 },
          { id: 'core-follower-b', community_id: dragCommunity, gravity_mass: 2, radius: 3,
            x: 0, y: 28, vx: -2, vy: 0 },
          { id: 'remote-star', community_id: 'remote', gravity_mass: 5, radius: 4,
            x: -100, y: 25, vx: 0, vy: -2 },
          { id: 'remote-moon', community_id: 'remote', gravity_mass: 1, radius: 2,
            x: -84, y: 31, vx: 1, vy: -1 },
        ];
        const links = [
          { source: 'dragged', target: 'core-follower-a', rest_length: 24, spring_strength: 0.1 },
          { source: 'dragged', target: 'core-follower-b', rest_length: 24, spring_strength: 0.1 },
        ];
        const dragged = nodes[1], followers = [
          { node: nodes[2], link: links[0] }, { node: nodes[3], link: links[1] },
        ];
        const options = {
          gravity: 48, central: true, fixedNodeId: 'dragged', dragSource: dragged,
          dragFollowers: followers, includeFarFieldConfinement: true,
          includeBlackHoleExclusion: true, blackHoleExclusionPadding: 2.5,
          includeMutualSystems: true, mutualSystemGravityFraction: 0.12,
          mutualSystemSoftening: 80, includeCollisions: false,
          includeRelations: true, includeRelationSprings: true, orbitScale: 0.25,
          relationStrengthMultiplier: 2, relationConstraintRate: 24,
          relationConstraintMaxCorrection: 12, relationPadding: 12,
          includeOrbitalSeparation: true, orbitalSeparationPadding: 12,
          orbitalSeparationStrength: 0.8, crossCommunitySeparationPadding: 1.5,
          crossCommunitySeparationStrength: 0.144, orbitalSeparationMaxCorrection: 4,
          orbitalSeparationMaxVelocityCorrection: 8, localRelativeSpeedLimit: 16,
          timestep: 0.021328125, wallClockSeconds: 1 / 30,
          velocityDecay: 0.00005, speedLimit: 24,
        };
        I.applyGalaxyFarFieldConfinement(nodes, options);
        const envelope = I.galaxyFarFieldEnvelope(nodes, options).envelopeRadius;
        let minimumClearance = Infinity, maximumFollowerStep = 0, maximumLinkDistance = 0;
        let maximumRemoteRadius = 0, maximumSpeed = 0, dragPull = 0, finite = true;
        let fixedSystemNodes = 0, skippedFixedEndpoint = 0;
        let outerFollowerClearance = Infinity, minimumSourceOuterClearance = Infinity;
        let maximumOuterFollowerStep = 0, requestedBeyondEnvelope = false, sourceEdgeContact = false;
        for (let step = 0; step < 48; step++) {
          const before = nodes.slice(2, 4).map(node => [node.x, node.y]);
          const remoteBefore = nodes.slice(4).map(node => [node.x, node.y]);
          /* This is the adversarial pointer target. The final horizon owns the paint phase. */
          dragged.x = 0; dragged.y = 0; dragged.vx = 0; dragged.vy = 0;
          const tick = I.integrateGalaxyLeapfrog(nodes, links, [], options);
          maximumSpeed = Math.max(maximumSpeed, tick.maximumSpeed);
          dragPull = Math.max(dragPull, tick.dragGravity.maximumPull);
          fixedSystemNodes += tick.blackHoleExclusion.fixedSystemNodes;
          skippedFixedEndpoint += tick.relationConstraint.skippedFixedEndpoint;
          nodes.slice(1).forEach(node => {
            minimumClearance = Math.min(minimumClearance,
              Math.hypot(node.x, node.y) - nodes[0].radius - node.radius
                - options.blackHoleExclusionPadding);
          });
          nodes.slice(2, 4).forEach((node, index) => {
            maximumFollowerStep = Math.max(maximumFollowerStep,
              Math.hypot(node.x - before[index][0], node.y - before[index][1]));
          });
          links.forEach(link => {
            const target = nodes.find(node => node.id === link.target);
            maximumLinkDistance = Math.max(maximumLinkDistance,
              Math.hypot(dragged.x - target.x, dragged.y - target.y));
          });
          nodes.slice(4).forEach((node, index) => {
            maximumRemoteRadius = Math.max(maximumRemoteRadius,
              Math.hypot(node.x, node.y) + node.radius);
            maximumFollowerStep = Math.max(maximumFollowerStep,
              Math.hypot(node.x - remoteBefore[index][0], node.y - remoteBefore[index][1]));
          });
          finite = finite && nodes.every(node => [node.x, node.y, node.vx, node.vy]
            .every(Number.isFinite));
        }
        const centreHeld = [dragged.x, dragged.y];
        /* An external pointer may request a source beyond the envelope, but the painted source
           and its nonfixed followers must remain inside it throughout a long, gradual outward
           drag. This is the former 400-slice runaway: a skipped fixed system let followers
           drift hundreds of units out, then snap back only after release. */
        if (externalSystem) {
          const startRadius = nodes[0].radius + dragged.radius + options.blackHoleExclusionPadding;
          const endRadius = envelope + 320;
          for (let step = 0; step < 400; step++) {
            const before = nodes.slice(2, 4).map(node => [node.x, node.y]);
            const targetX = startRadius + (endRadius - startRadius) * (step + 1) / 400;
            dragged.x = targetX; dragged.y = 0; dragged.vx = 0; dragged.vy = 0;
            const tick = I.integrateGalaxyLeapfrog(nodes, links, [], options);
            requestedBeyondEnvelope = requestedBeyondEnvelope
              || targetX + dragged.radius > envelope + 1e-8;
            const sourceClearance = envelope - (Math.hypot(dragged.x, dragged.y) + dragged.radius);
            minimumSourceOuterClearance = Math.min(minimumSourceOuterClearance, sourceClearance);
            sourceEdgeContact = sourceEdgeContact || Math.abs(sourceClearance) <= 1e-8;
            maximumSpeed = Math.max(maximumSpeed, tick.maximumSpeed);
            dragPull = Math.max(dragPull, tick.dragGravity.maximumPull);
            fixedSystemNodes += tick.blackHoleExclusion.fixedSystemNodes;
            skippedFixedEndpoint += tick.relationConstraint.skippedFixedEndpoint;
            nodes.slice(1).forEach(node => {
              minimumClearance = Math.min(minimumClearance,
                Math.hypot(node.x, node.y) - nodes[0].radius - node.radius
                  - options.blackHoleExclusionPadding);
            });
            nodes.slice(2, 4).forEach((node, index) => {
              outerFollowerClearance = Math.min(outerFollowerClearance,
                envelope - (Math.hypot(node.x, node.y) + node.radius));
              maximumOuterFollowerStep = Math.max(maximumOuterFollowerStep,
                Math.hypot(node.x - before[index][0], node.y - before[index][1]));
            });
            finite = finite && nodes.every(node => [node.x, node.y, node.vx, node.vy]
              .every(Number.isFinite));
          }
        }
        const held = [dragged.x, dragged.y];
        let releaseSpeed = 0, maximumReleaseFollowerStep = 0;
        for (let step = 0; step < 20; step++) {
          const before = nodes.slice(2, 4).map(node => [node.x, node.y]);
          const tick = I.integrateGalaxyLeapfrog(nodes, links, [], {
            ...options, fixedNodeId: null, dragSource: null, dragFollowers: [],
          });
          releaseSpeed = Math.max(releaseSpeed, tick.maximumSpeed);
          nodes.slice(2, 4).forEach((node, index) => {
            maximumReleaseFollowerStep = Math.max(maximumReleaseFollowerStep,
              Math.hypot(node.x - before[index][0], node.y - before[index][1]));
          });
          finite = finite && nodes.every(node => [node.x, node.y, node.vx, node.vy]
            .every(Number.isFinite));
        }
        emit({
          envelope, minimumClearance, maximumFollowerStep, maximumLinkDistance,
          maximumRemoteRadius, maximumSpeed, releaseSpeed, dragPull, finite,
          fixedSystemNodes, skippedFixedEndpoint, requestedBeyondEnvelope, sourceEdgeContact,
          outerFollowerClearance, minimumSourceOuterClearance, maximumOuterFollowerStep,
          maximumReleaseFollowerStep,
          centreHeld, held, released: [dragged.x, dragged.y],
          anchor: [nodes[0].x, nodes[0].y, nodes[0].vx, nodes[0].vy],
          draggedRadius: Math.hypot(centreHeld[0], centreHeld[1]),
          paintedHorizon: nodes[0].radius + dragged.radius + options.blackHoleExclusionPadding,
        });
        """
    )
    assert report["finite"] is True
    assert report["anchor"] == pytest.approx([0, 0, 0, 0], abs=1e-12)
    # The fixed source is projected to the event horizon, not allowed to paint at the centre.
    assert report["draggedRadius"] == pytest.approx(report["paintedHorizon"], abs=1e-8)
    assert report["minimumClearance"] >= -1e-8
    assert report["dragPull"] > 0
    # The dragged cluster may be the anchor community or a pointer-owned external system. The
    # latter must use its dedicated horizon path, while both skip direct spring correction.
    if expect_fixed_system_nodes:
        assert report["fixedSystemNodes"] > 0
        # Pointer targets beyond the cached envelope are requests, not paint positions: the
        # source must meet the same finite outer boundary as every follower while held.
        assert report["requestedBeyondEnvelope"] is True
        assert report["minimumSourceOuterClearance"] >= -1e-8
        assert report["sourceEdgeContact"] is True
        assert report["outerFollowerClearance"] >= -1e-8
        assert report["maximumOuterFollowerStep"] <= 48
        assert report["maximumReleaseFollowerStep"] <= 48
    else:
        assert report["fixedSystemNodes"] == 0
    assert report["skippedFixedEndpoint"] > 0
    assert report["maximumSpeed"] <= 24
    assert report["releaseSpeed"] <= 24
    assert report["maximumFollowerStep"] <= 48
    assert report["maximumLinkDistance"] <= 96
    assert report["maximumRemoteRadius"] <= report["envelope"] + 1e-8
    assert math.dist(report["held"], report["released"]) > 1e-4


@requires_node
@pytest.mark.parametrize("drag_id", ["star", "planet"])
def test_dragging_star_or_planet_across_stellar_surface_stays_bounded(drag_id: str) -> None:
    """A fixed source may cross a stellar surface without a follower feedback runaway."""
    report = _run_node(
        "const dragId = " + repr(drag_id) + ";\n" + """
        const nodes = [
          { id: 'bh', anchor_role: 'global', community_id: 'core', gravity_mass: 8,
            radius: 10, x: 0, y: 0, vx: 0, vy: 0 },
          { id: 'star', community_id: 'solar', gravity_mass: 14,
            radius: 5, x: 54, y: 0, vx: 0, vy: 0 },
          { id: 'planet', orbit_tier: 1, community_id: 'solar', gravity_mass: 1,
            radius: 3, x: 64, y: 0, vx: 0, vy: 0 },
          { id: 'moon', orbit_tier: 2, community_id: 'solar', gravity_mass: 1,
            radius: 3, x: 54, y: 16, vx: 0, vy: 0 },
          { id: 'remote-star', community_id: 'remote', gravity_mass: 10,
            radius: 5, x: -60, y: 0, vx: 0, vy: 0 },
          { id: 'remote-planet', orbit_tier: 1, community_id: 'remote', gravity_mass: 1,
            radius: 3, x: -48, y: 0, vx: 0, vy: 0 },
        ];
        const links = [
          { source: 'star', target: 'planet', rest_length: 10, spring_strength: 0.08 },
          { source: 'star', target: 'moon', rest_length: 16, spring_strength: 0.08 },
        ];
        const dragSourceNode = nodes.find(node => node.id === dragId);
        const star = nodes.find(node => node.id === 'star');
        const planet = nodes.find(node => node.id === 'planet');
        const target = dragId === 'star' ? [planet.x, planet.y] : [star.x, star.y];
        const followers = nodes.filter(node => node !== dragSourceNode && node.id !== 'bh')
          .map(node => ({ node, link: links.find(link => link.source === node.id
            || link.target === node.id) || null }));
        const options = {
          gravity: 48, central: true, fixedNodeId: dragId, dragSource: dragSourceNode,
          dragFollowers: followers, softening: 12, centralSoftening: 40,
          includeMutualSystems: true, mutualSystemGravityFraction: 0.12,
          mutualSystemSoftening: 80, includeCollisions: false,
          includeRelations: true, includeRelationSprings: false,
          skipSystemAnchorRelations: true, relationStrengthMultiplier: 1,
          relationConstraintRate: 24, relationConstraintMaxCorrection: 12,
          includeOrbitalSeparation: true, orbitalSeparationPadding: 1.5,
          orbitalSeparationStrength: 0.8, orbitalSeparationMaxCorrection: 4,
          orbitalSeparationMaxVelocityCorrection: 8, preserveLocalTangentialVelocity: true,
          skipSystemAnchorPairs: true, systemAnchorExclusionPadding: 1.5,
          crossCommunitySeparationPadding: 1.5, crossCommunitySeparationStrength: 0.144,
          includeBlackHoleExclusion: true, blackHoleExclusionPadding: 2.5,
          includeFarFieldConfinement: true, farFieldEnvelopeScale: 1.25,
          farFieldMinimumRadius: 96, farFieldSoftFraction: 0.82,
          farFieldAcceleration: 12, farFieldMaxAcceleration: 16, inwardConvergence: true,
          timestep: 0.021328125, wallClockSeconds: 1 / 30,
          velocityDecay: 0.00005, speedLimit: 24, localRelativeSpeedLimit: 16,
        };
        let anchorContacts = 0, minimumStarClearance = Infinity, maximumFollowerStep = 0;
        let maximumSpeed = 0, finite = true, envelope = 0;
        for (let step = 0; step < 120; step++) {
          const before = followers.map(follower => [follower.node.x, follower.node.y]);
          dragSourceNode.x = target[0]; dragSourceNode.y = target[1];
          dragSourceNode.vx = 0; dragSourceNode.vy = 0;
          const tick = I.integrateGalaxyLeapfrog(nodes, links, [], options);
          anchorContacts += tick.systemAnchorExclusion.contacts;
          envelope = tick.farFieldConfinement.envelopeRadius;
          maximumSpeed = Math.max(maximumSpeed, tick.maximumSpeed);
          followers.forEach((follower, index) => {
            maximumFollowerStep = Math.max(maximumFollowerStep,
              Math.hypot(follower.node.x - before[index][0], follower.node.y - before[index][1]));
          });
          [planet, nodes.find(node => node.id === 'moon')].forEach(satellite => {
            if (satellite === star) return;
            minimumStarClearance = Math.min(minimumStarClearance,
              Math.hypot(satellite.x - star.x, satellite.y - star.y)
                - star.radius - satellite.radius - options.systemAnchorExclusionPadding);
          });
          finite = finite && nodes.every(node => [node.x, node.y, node.vx, node.vy]
            .every(Number.isFinite));
        }
        const held = [dragSourceNode.x, dragSourceNode.y];
        let maximumReleaseStep = 0;
        for (let step = 0; step < 40; step++) {
          const before = nodes.map(node => [node.x, node.y]);
          const tick = I.integrateGalaxyLeapfrog(nodes, links, [], {
            ...options, fixedNodeId: null, dragSource: null, dragFollowers: [],
          });
          maximumSpeed = Math.max(maximumSpeed, tick.maximumSpeed);
          maximumReleaseStep = Math.max(maximumReleaseStep, ...nodes.map((node, index) =>
            Math.hypot(node.x - before[index][0], node.y - before[index][1])));
          finite = finite && nodes.every(node => [node.x, node.y, node.vx, node.vy]
            .every(Number.isFinite));
        }
        emit({
          anchorContacts, minimumStarClearance, maximumFollowerStep, maximumReleaseStep,
          maximumSpeed, finite, held, released: [dragSourceNode.x, dragSourceNode.y],
          outerBounded: nodes.slice(1).every(node =>
            Math.hypot(node.x, node.y) + node.radius <= envelope + 1e-8),
        });
        """
    )
    assert report["anchorContacts"] > 0
    assert report["minimumStarClearance"] >= -1e-9
    assert report["finite"] is True
    assert report["outerBounded"] is True
    assert report["maximumSpeed"] <= 24
    assert report["maximumFollowerStep"] <= 32
    assert report["maximumReleaseStep"] <= 32
    assert math.dist(report["held"], report["released"]) > 1e-4


@requires_node
def test_dense_stellar_surface_exclusion_keeps_com_momentum_and_tangential_phase() -> None:
    """Many simultaneous planets must clear a star without a contact-induced slingshot."""
    report = _run_node(
        """
        const star = { id: 'star', anchor_role: 'community', community_id: 'solar',
          gravity_mass: 20, radius: 5, x: 40, y: -12, vx: 1.5, vy: -0.75 };
        const nodes = [star];
        for (let index = 0; index < 16; index++) {
          const angle = index * Math.PI * 2 / 16;
          const radius = 6; // strictly inside the 5 + 2 + 1.5 painted stellar surface
          nodes.push({ id: 'planet-' + index, community_id: 'solar', gravity_mass: 1,
            radius: 2, x: star.x + Math.cos(angle) * radius,
            y: star.y + Math.sin(angle) * radius,
            vx: star.vx - Math.sin(angle) * 3,
            vy: star.vy + Math.cos(angle) * 3 });
        }
        const totals = () => nodes.reduce((sum, node) => ({
          mass: sum.mass + node.gravity_mass,
          x: sum.x + node.gravity_mass * node.x,
          y: sum.y + node.gravity_mass * node.y,
          px: sum.px + node.gravity_mass * node.vx,
          py: sum.py + node.gravity_mass * node.vy,
        }), { mass: 0, x: 0, y: 0, px: 0, py: 0 });
        const before = totals();
        const exclusion = I.applyGalaxySystemAnchorExclusion(nodes, { padding: 1.5 });
        const after = totals();
        emit({
          exclusion,
          comShift: Math.hypot(after.x / after.mass - before.x / before.mass,
            after.y / after.mass - before.y / before.mass),
          momentumDelta: Math.hypot(after.px - before.px, after.py - before.py),
          finite: nodes.every(node => [node.x, node.y, node.vx, node.vy]
            .every(Number.isFinite)),
        });
        """
    )
    assert report["exclusion"]["contacts"] >= 16
    assert report["exclusion"]["minimumClearance"] >= -1e-10
    assert report["comShift"] <= 1e-10
    assert report["momentumDelta"] <= 1e-10
    assert report["exclusion"]["tangentialVelocityRemoved"] == 0
    assert report["finite"] is True


@requires_node
def test_dominant_star_has_smooth_mass_balanced_repulsion_before_its_hard_surface() -> None:
    """A star's surface pressure beats its well without becoming generic pair repulsion."""
    report = _run_node(
        """
        const fixture = innerMass => [
          { id: 'star', anchor_role: 'community', community_id: 'solar', gravity_mass: 8,
            radius: 5, x: 0, y: 0, vx: 1, vy: -2 },
          // 9.5 is the exact painted boundary: 5 + 3 radii + 1.5 padding.
          { id: 'inner', community_id: 'solar', orbit_tier: 1, gravity_mass: innerMass,
            radius: 3, x: 9.5, y: 0, vx: 1, vy: 2 },
          { id: 'outer', community_id: 'solar', orbit_tier: 2, gravity_mass: 1,
            radius: 3, x: 100, y: 0, vx: 1, vy: -2 },
        ];
        const trial = (innerMass, pressure = 0.12) => {
          const nodes = fixture(innerMass);
          const before = nodes.map(node => [node.vx, node.vy]);
          const momentum = nodes.reduce((total, node) => [
            total[0] + node.gravity_mass * node.vx,
            total[1] + node.gravity_mass * node.vy,
          ], [0, 0]);
          const stats = I.applyGalaxySystemAnchorGravity(nodes, {
            gravity: 0, alpha: 1, softening: 12, repulsionPadding: 1.5,
            repulsionRange: 6, repulsionAcceleration: pressure, accelerationCap: 100,
          });
          const afterMomentum = nodes.reduce((total, node) => [
            total[0] + node.gravity_mass * node.vx,
            total[1] + node.gravity_mass * node.vy,
          ], [0, 0]);
          return { before, after: nodes.map(node => [node.vx, node.vy]), stats,
            momentumDelta: [afterMomentum[0] - momentum[0], afterMomentum[1] - momentum[1]],
            radialRelative: nodes[1].vx - nodes[0].vx,
            outerRadialRelative: nodes[2].vx - nodes[0].vx,
            tangentialRelative: nodes[1].vy - nodes[0].vy,
          };
        };
        emit({ light: trial(1), heavy: trial(9),
          lightControl: trial(1, 0), heavyControl: trial(9, 0) });
        """
    )
    light, heavy = report["light"], report["heavy"]
    controls = (report["lightControl"], report["heavyControl"])
    for trial, control in zip((light, heavy), controls):
        stats = trial["stats"]
        assert stats["systems"] == stats["anchors"] == 1
        assert stats["satellites"] == 2
        assert stats["repulsions"] == 1
        assert stats["repulsionPadding"] == pytest.approx(1.5)
        assert stats["repulsionRange"] == pytest.approx(6)
        assert stats["repulsionAcceleration"] == pytest.approx(0.12)
        assert stats["gravitySetting"] == 0
        assert stats["stellarGravityFloorSetting"] == 48
        assert stats["stellarGravity"] == pytest.approx(750)
        assert stats["eligibleStellarAnchors"] == 1
        assert stats["fallbackAnchors"] == 0
        assert stats["globalAnchors"] == 0
        assert stats["stellarFloorActive"] is True
        assert stats["surfaceRepulsions"] == 1
        assert stats["maximumRepulsion"] > stats["maximumSampledAttraction"] > 0
        assert stats["maximumNetRepulsion"] == pytest.approx(0.12)
        assert stats["minimumSurfaceNetRepulsion"] == pytest.approx(0.12)
        # The live Gravity-zero stellar floor still attracts; pressure exceeds that sampled
        # attraction by the requested bounded margin at the painted surface.  Comparing with
        # pressure disabled isolates the radial correction from the shared gravity field.
        assert trial["radialRelative"] == pytest.approx(stats["maximumNetRepulsion"])
        assert trial["radialRelative"] - control["radialRelative"] == pytest.approx(
            stats["maximumRepulsion"]
        )
        assert trial["momentumDelta"] == pytest.approx([0, 0], abs=1e-12)
        assert trial["tangentialRelative"] == pytest.approx(4)
        # The inner planet is not promoted into a second pressure source: enabling its surface
        # correction leaves the remote planet's star-relative radial response unchanged.
        assert trial["outerRadialRelative"] == pytest.approx(
            control["outerRadialRelative"], abs=1e-12
        )
    # Surface strength depends on the star field and geometry, not satellite evidence mass.
    assert light["stats"]["maximumRepulsion"] == pytest.approx(
        heavy["stats"]["maximumRepulsion"], abs=1e-12
    )


@requires_node
def test_live_gravity_stellar_pressure_is_outward_at_the_surface_and_tapers_smoothly() -> None:
    """The soft stellar surface must beat live attraction locally without adding momentum."""
    report = _run_node(
        """
        const trial = (gravity, distance, repulsionAcceleration) => {
          const nodes = [
            { id: 'star', anchor_role: 'community', community_id: 'solar', gravity_mass: 8,
              radius: 5, x: 0, y: 0, vx: 1, vy: -2 },
            { id: 'planet', community_id: 'solar', system_anchor_id: 'star', orbit_tier: 1,
              gravity_mass: 1, radius: 3, x: distance, y: 0, vx: 1, vy: 2 },
          ];
          const before = nodes.map(node => ({ vx: node.vx, vy: node.vy }));
          const momentumBefore = ['vx', 'vy'].map(axis => nodes.reduce((sum, node) =>
            sum + node.gravity_mass * node[axis], 0));
          const options = { gravity, softening: 32, alpha: 1,
            repulsionPadding: 1.5, repulsionRange: 6 };
          if (repulsionAcceleration !== undefined) {
            options.repulsionAcceleration = repulsionAcceleration;
          }
          const stats = I.applyGalaxySystemAnchorGravity(nodes, options);
          const momentumAfter = ['vx', 'vy'].map(axis => nodes.reduce((sum, node) =>
            sum + node.gravity_mass * node[axis], 0));
          return {
            stats,
            relativeRadial: (nodes[1].vx - nodes[0].vx)
              - (before[1].vx - before[0].vx),
            relativeTangential: nodes[1].vy - nodes[0].vy,
            momentumDelta: momentumAfter.map((value, index) => value - momentumBefore[index]),
            finite: nodes.every(node => [node.vx, node.vy].every(Number.isFinite)),
          };
        };
        const hardDistance = 5 + 3 + 1.5;
        const pressureEdge = hardDistance + 6;
        const inside = trial(48, hardDistance - 0.75);
        const surface = trial(48, hardDistance);
        const surfaceWithoutPressure = trial(48, hardDistance, 0);
        const edge = trial(48, pressureEdge);
        const edgeWithoutPressure = trial(48, pressureEdge, 0);
        const maximum = trial(400, hardDistance);
        emit({ hardDistance, pressureEdge, inside, surface, surfaceWithoutPressure,
          edge, edgeWithoutPressure, maximum });
        """
    )
    for trial in (report["inside"], report["surface"], report["edge"], report["maximum"]):
        assert trial["finite"] is True
        assert trial["momentumDelta"] == pytest.approx([0, 0], abs=1e-10)
        assert trial["relativeTangential"] == pytest.approx(4, abs=1e-12)
    # At and just inside the painted 9.5-unit stellar surface, net star-relative acceleration
    # must point outward even with the ordinary gravity-48 central well active.
    assert report["inside"]["relativeRadial"] > 0
    assert report["surface"]["relativeRadial"] > 0
    assert report["inside"]["stats"]["repulsions"] == 1
    assert report["surface"]["stats"]["repulsions"] == 1
    assert report["inside"]["stats"]["surfaceRepulsions"] == 1
    assert report["surface"]["stats"]["surfaceRepulsions"] == 1
    assert report["surface"]["stats"]["maximumSampledAttraction"] > 0
    assert report["surface"]["stats"]["maximumNetRepulsion"] > 0
    assert report["surface"]["stats"]["minimumSurfaceNetRepulsion"] > 0
    assert report["surface"]["relativeRadial"] > \
        report["surfaceWithoutPressure"]["relativeRadial"]
    # Pressure reaches zero continuously at the 15.5-unit outer edge; ordinary gravity remains.
    assert report["edge"]["stats"]["repulsions"] == 0
    assert report["edge"]["relativeRadial"] == pytest.approx(
        report["edgeWithoutPressure"]["relativeRadial"], abs=1e-12
    )
    # The maximum visible gravity setting stays finite and below its tested acceleration cap.
    assert report["maximum"]["stats"]["surfaceRepulsions"] == 1
    assert report["maximum"]["stats"]["minimumSurfaceNetRepulsion"] > 0
    assert report["maximum"]["stats"]["maximumAcceleration"] <= 500
    assert abs(report["maximum"]["relativeRadial"]) <= 1000


@requires_node
def test_galaxy_collision_uses_evidence_mass_without_injecting_system_momentum() -> None:
    report = _run_node(
        """
        const contact = [
          { id: 'star', x: 0, y: 0, vx: 0, vy: 0, radius: 6, gravity_mass: 4 },
          { id: 'planet', x: 10, y: 0, vx: 0, vy: 0, radius: 6, gravity_mass: 1 },
          { id: 'remote', x: 100, y: 0, vx: 0, vy: 0, radius: 2, gravity_mass: 8 },
        ];
        const stats = I.applyGalaxyCollisions(contact, {
          padding: 0, strength: 1, iterations: 1,
        });
        const coincident = [
          { id: 'a', x: 0, y: 0, radius: 3, gravity_mass: 2 },
          { id: 'b', x: 0, y: 0, radius: 3, gravity_mass: 5 },
        ];
        I.applyGalaxyCollisions(coincident, { padding: 0, strength: 0.7, iterations: 2 });
        const sparse = Array.from({ length: 120 }, (_, index) => ({
          id: 's' + index, x: index * 30, y: 0, radius: 2, gravity_mass: 1,
        }));
        const sparseStats = I.applyGalaxyCollisions(sparse, {
          padding: 0, strength: 1, iterations: 1,
        });
        const tangent = [
          { id: 'left', x: 0, y: 0, vx: 0, vy: 1, radius: 6, gravity_mass: 1 },
          { id: 'right', x: 10, y: 0, vx: 0, vy: 0, radius: 6, gravity_mass: 1 },
        ];
        const closing = [
          { id: 'heavy', x: 0, y: 0, vx: 1, vy: 0, radius: 6, gravity_mass: 4 },
          { id: 'light', x: 10, y: 0, vx: -2, vy: 0, radius: 6, gravity_mass: 1 },
        ];
        const angular = bodies => bodies.reduce((sum, node) => sum
          + node.gravity_mass * (node.x * node.vy - node.y * node.vx), 0);
        const kinetic = bodies => bodies.reduce((sum, node) => sum
          + 0.5 * node.gravity_mass * (node.vx * node.vx + node.vy * node.vy), 0);
        const angularBefore = angular(tangent);
        const kineticBefore = kinetic(closing);
        I.applyGalaxyCollisions(tangent, { padding: 0, strength: 1, iterations: 1 });
        I.applyGalaxyCollisions(closing, { padding: 0, strength: 1, iterations: 1 });
        emit({
          positions: contact.map(node => [node.x, node.y]),
          velocities: contact.map(node => [node.vx, node.vy]),
          momentum: [
            contact.reduce((sum, node) => sum + node.gravity_mass * node.vx, 0),
            contact.reduce((sum, node) => sum + node.gravity_mass * node.vy, 0),
          ],
          overlaps: stats.overlaps,
          coincidentFinite: coincident.every(node => Number.isFinite(node.vx)
            && Number.isFinite(node.vy)),
          sparsePairs: sparseStats.pairs,
          quadratic: sparse.length * sparse.length,
          angularBefore,
          angularAfter: angular(tangent),
          kineticBefore,
          kineticAfter: kinetic(closing),
          closingMomentum: closing.reduce(
            (sum, node) => sum + node.gravity_mass * node.vx, 0
          ),
        });
        """
    )
    assert report["positions"][0] == pytest.approx([-0.4, 0])
    assert report["positions"][1] == pytest.approx([11.6, 0])
    assert report["velocities"][0] == pytest.approx([0, 0])
    assert report["velocities"][1] == pytest.approx([0, 0])
    assert report["velocities"][2] == pytest.approx([0, 0])
    assert report["momentum"] == pytest.approx([0, 0], abs=1e-12)
    assert report["overlaps"] == 1
    assert report["coincidentFinite"] is True
    assert report["sparsePairs"] < report["quadratic"] // 20
    assert report["angularAfter"] == pytest.approx(report["angularBefore"], abs=1e-12)
    assert report["kineticAfter"] <= report["kineticBefore"]
    assert report["closingMomentum"] == pytest.approx(2, abs=1e-12)


@requires_node
def test_galaxy_leapfrog_is_fixed_step_deterministic_and_does_not_depend_on_alpha() -> None:
    report = _run_node(
        """
        const fixture = () => [
          { id: 'sun', x: 0, y: 0, vx: 0, vy: 0, radius: 5,
            gravity_mass: 8, community_id: 'solar' },
          { id: 'planet', x: 28, y: 0, vx: 0, vy: 0, radius: 2,
            gravity_mass: 1, community_id: 'solar' },
        ];
        const first = fixture(), second = fixture(), damped = fixture(), conserved = fixture();
        I.seedGalaxyOrbits(first, 77, 12, 8, false);
        I.seedGalaxyOrbits(second, 77, 12, 8, false);
        I.seedGalaxyOrbits(conserved, 77, 12, 8, false);
        const seeded = first.map(node => [node.x, node.y, node.vx, node.vy]);
        const step = nodes => I.integrateGalaxyLeapfrog(nodes, [], [], {
          gravity: 12, softening: 8, central: false, timestep: 0.25,
          velocityDecay: 0.012, speedLimit: 18, collisionPadding: 0,
          collisionStrength: 0, collisionIterations: 1,
        });
        const initialAngular = first[1].x * first[1].vy - first[1].y * first[1].vx;
        let firstStep = step(first);
        step(second);
        for (let i = 0; i < 159; i++) { step(first); step(second); }
        const energy = nodes => {
          const kinetic = nodes.reduce((sum, node) => sum + 0.5 * node.gravity_mass
            * (node.vx * node.vx + node.vy * node.vy), 0);
          const dx = nodes[1].x - nodes[0].x, dy = nodes[1].y - nodes[0].y;
          return kinetic - (I.galaxyFallbackStellarGravityConstant(12) * 8)
            / Math.sqrt(dx * dx + dy * dy + 64);
        };
        const angularMomentum = nodes => nodes.reduce((sum, node) => sum + node.gravity_mass
          * (node.x * node.vy - node.y * node.vx), 0);
        const energyStart = energy(conserved), angularStart = angularMomentum(conserved);
        for (let i = 0; i < 400; i++) I.integrateGalaxyLeapfrog(conserved, [], [], {
          gravity: 12, softening: 8, central: false, timestep: 0.1,
          velocityDecay: 0, speedLimit: 100, collisionStrength: 0,
        });
        damped[0].vx = 6; damped[0].vy = -2;
        const beforeDamping = 0.5 * damped[0].gravity_mass
          * (damped[0].vx * damped[0].vx + damped[0].vy * damped[0].vy);
        const dampingStep = I.integrateGalaxyLeapfrog(damped, [], [], {
          gravity: 0, central: false, timestep: 1, velocityDecay: 0.2,
          speedLimit: 100, collisionStrength: 0,
        });
        emit({
          seeded,
          firstStep, initialAngular,
          first: first.map(node => [node.x, node.y, node.vx, node.vy]),
          second: second.map(node => [node.x, node.y, node.vx, node.vy]),
          finite: first.every(node => [node.x, node.y, node.vx, node.vy]
            .every(Number.isFinite)),
          maximumSpeed: Math.max(...first.map(node => Math.hypot(node.vx, node.vy))),
          beforeDamping, afterDamping: dampingStep.kinetic,
          energyStart, energyEnd: energy(conserved), angularStart,
          angularEnd: angularMomentum(conserved),
        });
        """
    )
    # A fixed sequence is repeatable and changes the seeded orbit without a D3 alpha input.
    assert [value for node in report["first"] for value in node] == pytest.approx(
        [value for node in report["second"] for value in node]
    )
    assert report["firstStep"]["bodies"] == 2
    assert report["initialAngular"] != 0
    assert report["finite"] is True
    assert report["maximumSpeed"] <= 18
    assert report["first"][1][:2] != pytest.approx(report["seeded"][1][:2])
    assert report["afterDamping"] < report["beforeDamping"]
    assert report["energyEnd"] == pytest.approx(report["energyStart"], rel=0.03)
    assert report["angularEnd"] == pytest.approx(report["angularStart"], rel=0.03)
    source = ASSET.read_text(encoding="utf-8")
    integrator = source[source.index("function integrateGalaxyLeapfrog"):
                        source.index("function fallbackCommunityBridges")]
    assert "alpha" not in integrator
    assert "kick-drift-kick" in integrator


@requires_node
def test_integrator_keeps_rotating_nodes_outside_black_hole_and_clamps_drag() -> None:
    report = _run_node(
        """
        const nodes = [
          { id: 'black-hole', anchor_role: 'global', community_id: 'core',
            gravity_mass: 64, radius: 12, x: 0, y: 0, vx: 0, vy: 0 },
          { id: 'aurora', community_id: 'aurora', gravity_mass: 4, radius: 3,
            x: 18, y: 0, vx: 0, vy: 0 },
          { id: 'borealis', community_id: 'borealis', gravity_mass: 3, radius: 3,
            x: 0, y: -22, vx: 0, vy: 0 },
          { id: 'cygnus', community_id: 'cygnus', gravity_mass: 2, radius: 2,
            x: -26, y: 4, vx: 0, vy: 0 },
        ];
        I.seedGalaxySystemOrbits(nodes, 123, 48, 40, false);
        const options = {
          gravity: 48, softening: 32, centralSoftening: 40,
          localPairFraction: 0.15, corePairMultiplier: 0.75,
          includeMutualSystems: true, mutualSystemGravityFraction: 0.12,
          mutualSystemSoftening: 80, includeRelations: false,
          includeOrbitalSeparation: true, orbitalSeparationPadding: 12,
          orbitalSeparationStrength: 0.8, orbitalSeparationMaxCorrection: 4,
          orbitalSeparationMaxVelocityCorrection: 8,
          includeBlackHoleExclusion: true, blackHoleExclusionPadding: 2.5,
          includeCollisions: false, inwardConvergence: true,
          timestep: 0.021328125, wallClockSeconds: 1 / 30,
          velocityDecay: 0.00005, speedLimit: 48, localRelativeSpeedLimit: 16,
        };
        const angles = new Map(nodes.slice(1).map(node => [node.id, Math.atan2(node.y, node.x)]));
        const angularTravel = new Map(nodes.slice(1).map(node => [node.id, 0]));
        let minimumClearance = Infinity, contacts = 0, finalStep = null;
        for (let step = 0; step < 600; step++) {
          finalStep = I.integrateGalaxyLeapfrog(nodes, [], [], options);
          contacts += finalStep.blackHoleExclusion.contacts;
          nodes.slice(1).forEach(node => {
            const clearance = Math.hypot(node.x, node.y)
              - nodes[0].radius - node.radius - 2.5;
            minimumClearance = Math.min(minimumClearance, clearance);
            const angle = Math.atan2(node.y, node.x);
            const previous = angles.get(node.id);
            angularTravel.set(node.id, angularTravel.get(node.id)
              + Math.abs(Math.atan2(Math.sin(angle - previous), Math.cos(angle - previous))));
            angles.set(node.id, angle);
          });
        }

        const dragged = [
          { id: 'drag-anchor', anchor_role: 'global', community_id: 'core',
            gravity_mass: 64, radius: 12, x: 0, y: 0, vx: 0, vy: 0 },
          { id: 'dragged', community_id: 'dragged-system', gravity_mass: 1, radius: 2,
            x: 0, y: 0, vx: 0, vy: 0 },
        ];
        const dragStep = I.integrateGalaxyLeapfrog(dragged, [], [], {
          gravity: 0, central: true, fixedNodeId: 'dragged', timestep: 0.021328125,
          includeBlackHoleExclusion: true, blackHoleExclusionPadding: 2.5,
          includeCollisions: false, includeRelations: false, inwardConvergence: false,
          velocityDecay: 0, speedLimit: 48,
        });
        emit({
          minimumClearance, contacts,
          angularTravel: Object.fromEntries(angularTravel),
          anchor: [nodes[0].x, nodes[0].y, nodes[0].vx, nodes[0].vy],
          finalRadii: nodes.slice(1).map(node => Math.hypot(node.x, node.y)),
          finite: nodes.concat(dragged).every(node =>
            [node.x, node.y, node.vx, node.vy].every(Number.isFinite)),
          maximumSpeed: finalStep.maximumSpeed,
          finalClearance: finalStep.blackHoleExclusion.minimumClearance,
          draggedClearance: Math.hypot(dragged[1].x, dragged[1].y)
            - dragged[0].radius - dragged[1].radius - 2.5,
          dragContacts: dragStep.blackHoleExclusion.contacts,
        });
        """
    )
    assert report["finite"] is True
    assert report["anchor"] == pytest.approx([0, 0, 0, 0], abs=1e-12)
    assert report["minimumClearance"] >= -1e-9
    assert report["finalClearance"] >= -1e-9
    assert report["contacts"] > 0
    assert min(report["angularTravel"].values()) > 0.05
    assert report["maximumSpeed"] <= 48
    assert report["draggedClearance"] >= -1e-9
    assert report["dragContacts"] > 0


@requires_node
def test_nested_galaxy_orbits_keep_global_and_local_angular_motion() -> None:
    """Dense cross-system contact must not erase either layer of orbital motion."""
    report = _run_node(
        """
        const nodes = [{ id: 'bh', anchor_role: 'global', community_id: 'core',
          gravity_mass: 24, radius: 10, x: 0, y: 0, vx: 0, vy: 0 }];
        const systemIds = [];
        for (let system = 0; system < 14; system++) {
          const phase = system * 2 * Math.PI / 14;
          systemIds.push('s' + system);
          for (let member = 0; member < 4; member++) {
            const localPhase = phase + member * Math.PI / 2;
            nodes.push({ id: `${system}-${member}`, community_id: `s${system}`,
              anchor_role: member ? 'none' : 'community', gravity_mass: member ? 1 : 5,
              radius: member ? 3 : 5,
              x: Math.cos(phase) * 38 + Math.cos(localPhase) * (member ? 9 : 0),
              y: Math.sin(phase) * 38 + Math.sin(localPhase) * (member ? 9 : 0),
              vx: 0, vy: 0 });
          }
        }
        I.seedGalaxyOrbits(nodes, 91, 48, 12, false, 0.15, 0.75);
        I.seedGalaxySystemOrbits(nodes, 91, 48, 40, false);
        const centers = () => I.communityCenters(nodes);
        const byId = id => nodes.find(node => node.id === id);
        const globalAngles = new Map(systemIds.map(id => {
          const center = centers().get(id);
          return [id, Math.atan2(center.y, center.x)];
        }));
        const localAngles = new Map(systemIds.map((id, system) => {
          const star = byId(`${system}-0`), planet = byId(`${system}-1`);
          return [id, Math.atan2(planet.y - star.y, planet.x - star.x)];
        }));
        const globalTravel = new Map(systemIds.map(id => [id, 0]));
        const localTravel = new Map(systemIds.map(id => [id, 0]));
        const angleStep = (next, previous) => Math.atan2(
          Math.sin(next - previous), Math.cos(next - previous)
        );
        const options = {
          gravity: 48, softening: 12, centralSoftening: 40,
          localPairFraction: 0.15, corePairMultiplier: 0.75,
          includeMutualSystems: true, mutualSystemGravityFraction: 0.12,
          mutualSystemSoftening: 80, includeRelations: false,
          includeOrbitalSeparation: true, orbitalSeparationPadding: 12,
          orbitalSeparationStrength: 0.8, orbitalSeparationMaxCorrection: 4,
          orbitalSeparationMaxVelocityCorrection: 8,
          crossCommunitySeparationPadding: 1.5, crossCommunitySeparationStrength: 0.144,
          includeCollisions: false,
          includeBlackHoleExclusion: true, blackHoleExclusionPadding: 2.5,
          includeFarFieldConfinement: true, farFieldEnvelopeScale: 1.25,
          farFieldMinimumRadius: 96, farFieldSoftFraction: 0.82,
          farFieldAcceleration: 12, farFieldMaxAcceleration: 16, inwardConvergence: true,
          timestep: 0.021328125, wallClockSeconds: 1 / 30,
          velocityDecay: 0.00005, speedLimit: 48, localRelativeSpeedLimit: 16,
        };
        let minimumClearance = Infinity, maximumSpeed = 0, minimumSystemSpeed = Infinity;
        let crossCommunityOverlaps = 0;
        for (let step = 0; step < 300; step++) {
          const tick = I.integrateGalaxyLeapfrog(nodes, [], [], options);
          crossCommunityOverlaps += tick.orbitalSeparation.crossCommunityOverlaps;
          systemIds.forEach((id, system) => {
            const center = centers().get(id);
            const global = Math.atan2(center.y, center.x);
            const globalDelta = angleStep(global, globalAngles.get(id));
            globalTravel.set(id, globalTravel.get(id) + Math.abs(globalDelta));
            globalAngles.set(id, global);
            const star = byId(`${system}-0`), planet = byId(`${system}-1`);
            const local = Math.atan2(planet.y - star.y, planet.x - star.x);
            const localDelta = angleStep(local, localAngles.get(id));
            localTravel.set(id, localTravel.get(id) + Math.abs(localDelta));
            localAngles.set(id, local);
            const radius = Math.hypot(center.x, center.y);
            const vx = center.nodes.reduce((sum, node) => sum
              + node.gravity_mass * node.vx, 0) / center.mass;
            const vy = center.nodes.reduce((sum, node) => sum
              + node.gravity_mass * node.vy, 0) / center.mass;
            minimumSystemSpeed = Math.min(minimumSystemSpeed, Math.abs(
              (-center.y / radius) * vx + (center.x / radius) * vy
            ));
          });
          nodes.slice(1).forEach(node => {
            minimumClearance = Math.min(minimumClearance, Math.hypot(node.x, node.y)
              - nodes[0].radius - node.radius - 2.5);
          });
          maximumSpeed = Math.max(maximumSpeed, tick.maximumSpeed);
        }
        emit({
          globalTravel: Object.fromEntries(globalTravel),
          localTravel: Object.fromEntries(localTravel),
          minimumClearance,
          maximumSpeed, crossCommunityOverlaps, minimumSystemSpeed,
          finite: nodes.every(node => [node.x, node.y, node.vx, node.vy]
            .every(Number.isFinite)),
        });
        """
    )
    assert report["finite"] is True
    assert report["minimumClearance"] >= -1e-9
    assert report["maximumSpeed"] <= 48
    assert report["crossCommunityOverlaps"] > 1000
    assert report["minimumSystemSpeed"] > 3
    assert min(report["globalTravel"].values()) > 1
    assert min(report["localTravel"].values()) > 0.3


@requires_node
def test_hierarchical_galaxy_keeps_planets_bound_to_one_dominant_star() -> None:
    """A local star is the sole source for its planets while its system orbits the hole.

    This deliberately starts one planet slightly inside its star's painted exclusion radius.
    The contact layer must repair that hard local boundary without draining either the
    system's black-hole orbit or the satellites' signed local angular phase.
    """
    report = _run_node(
        """
        const nodes = [
          { id: 'bh', anchor_role: 'global', community_id: 'core',
            gravity_mass: 64, radius: 10, x: 0, y: 0, vx: 0, vy: 0 },
          { id: 'a-star', community_id: 'a', system_anchor_id: 'a-star', gravity_mass: 14, radius: 5,
            x: 46, y: 0, vx: 0, vy: 0 },
          { id: 'a-inner', orbit_tier: 1, community_id: 'a', system_anchor_id: 'a-star', gravity_mass: 1, radius: 3,
            x: 54, y: 0, vx: 0, vy: 0 },
          { id: 'a-outer', orbit_tier: 2, community_id: 'a', system_anchor_id: 'a-star', gravity_mass: 1, radius: 3,
            x: 54, y: 7, vx: 0, vy: 0 },
          { id: 'b-star', community_id: 'b', system_anchor_id: 'b-star', gravity_mass: 12, radius: 5,
            x: -54, y: 0, vx: 0, vy: 0 },
          { id: 'b-inner', orbit_tier: 1, community_id: 'b', system_anchor_id: 'b-star', gravity_mass: 1, radius: 3,
            x: -44, y: 0, vx: 0, vy: 0 },
          { id: 'b-outer', orbit_tier: 2, community_id: 'b', system_anchor_id: 'b-star', gravity_mass: 1, radius: 3,
            x: -54, y: -16, vx: 0, vy: 0 },
        ];
        const links = [
          { source: 'a-star', target: 'a-inner', rest_length: 10, spring_strength: 0.08 },
          { source: 'a-star', target: 'a-outer', rest_length: 16, spring_strength: 0.08 },
          { source: 'b-star', target: 'b-inner', rest_length: 10, spring_strength: 0.08 },
          { source: 'b-star', target: 'b-outer', rest_length: 16, spring_strength: 0.08 },
        ];
        const systemIds = ['a', 'b'];
        const planetIds = ['a-inner', 'a-outer', 'b-inner', 'b-outer'];
        const byId = id => nodes.find(node => node.id === id);
        const centers = () => I.communityCenters(nodes);
        const angleStep = (next, previous) => Math.atan2(
          Math.sin(next - previous), Math.cos(next - previous)
        );
        const localSourceAcceleration = innerMass => {
          /* A planet's inertial mass must not make it an additional local gravity source. */
          const sample = [
            { id: 'star', anchor_role: 'community', community_id: 'sample',
              gravity_mass: 14, x: 0, y: 0, vx: 0, vy: 0 },
            { id: 'inner', community_id: 'sample', gravity_mass: innerMass,
              x: 16, y: 0, vx: 0, vy: 0 },
            { id: 'outer', community_id: 'sample', gravity_mass: 1,
              x: 0, y: 24, vx: 0, vy: 0 },
          ];
          I.applyGalaxySystemAnchorGravity(sample, {
            gravity: 48, softening: 12, accelerationCap: 100,
          });
          // The free-system frame can translate after a massive satellite recoils the star.
          // Only outer-minus-star acceleration proves planets are not secondary wells.
          return [sample[2].vx - sample[0].vx, sample[2].vy - sample[0].vy];
        };
        const lightPlanetField = localSourceAcceleration(1);
        const heavyPlanetField = localSourceAcceleration(8);

        I.seedGalaxyOrbits(nodes, 9, 48, 12, false, 0.15, 0.75);
        I.seedGalaxySystemOrbits(nodes, 9, 48, 40, false);
        const globalAngles = new Map(systemIds.map(id => {
          const center = centers().get(id);
          return [id, Math.atan2(center.y, center.x)];
        }));
        const localAngles = new Map(planetIds.map(id => {
          const planet = byId(id), star = byId(id.slice(0, 1) + '-star');
          return [id, Math.atan2(planet.y - star.y, planet.x - star.x)];
        }));
        const globalTravel = new Map(systemIds.map(id => [id, 0]));
        const localTravel = new Map(planetIds.map(id => [id, 0]));
        const options = {
          gravity: 48, softening: 12, centralSoftening: 40,
          localPairFraction: 0.15, corePairMultiplier: 0.75,
          includeMutualSystems: true, mutualSystemGravityFraction: 0.12,
          mutualSystemSoftening: 80, includeRelations: true,
          relationStrengthMultiplier: 1, relationConstraintRate: 24,
          relationConstraintMaxCorrection: 12,
          includeRelationSprings: false, skipSystemAnchorRelations: true,
          skipOrbitalSystemRelations: true,
          includeOrbitalSeparation: true, orbitalSeparationPadding: 1.5,
          orbitalSeparationStrength: 0.8, orbitalSeparationMaxCorrection: 4,
          orbitalSeparationMaxVelocityCorrection: 8,
          preserveLocalTangentialVelocity: true, skipSystemAnchorPairs: true,
          systemAnchorExclusionPadding: 1.5,
          crossCommunitySeparationPadding: 1.5, crossCommunitySeparationStrength: 0.144,
          includeCollisions: false,
          includeBlackHoleExclusion: true, blackHoleExclusionPadding: 2.5,
          includeFarFieldConfinement: true, farFieldEnvelopeScale: 1.25,
          farFieldMinimumRadius: 96, farFieldSoftFraction: 0.82,
          farFieldAcceleration: 12, farFieldMaxAcceleration: 16, inwardConvergence: true,
          timestep: 0.021328125, wallClockSeconds: 1 / 30,
          velocityDecay: 0.00005, speedLimit: 48, localRelativeSpeedLimit: 16,
        };
        let localContacts = 0, systemAnchorContacts = 0, systemRepulsions = 0;
        let surfaceRepulsions = 0, maximumSystemRepulsion = 0;
        let relationAnchorSkips = 0;
        let relationOrbitalSystemSkips = 0;
        let maximumSpeed = 0, minimumBlackHoleClearance = Infinity;
        let minimumStarClearance = Infinity, maximumInnerOrbitRadius = 0, finalTick = null;
        for (let step = 0; step < 600; step++) {
          finalTick = I.integrateGalaxyLeapfrog(nodes, links, [], options);
          localContacts += finalTick.orbitalSeparation.overlaps;
          systemAnchorContacts += finalTick.systemAnchorExclusion.contacts;
          systemRepulsions += finalTick.systemGravity.repulsions;
          surfaceRepulsions += finalTick.systemGravity.surfaceRepulsions;
          maximumSystemRepulsion = Math.max(
            maximumSystemRepulsion, finalTick.systemGravity.maximumRepulsion);
          relationAnchorSkips += finalTick.relationConstraint.skippedSystemAnchor;
          relationOrbitalSystemSkips += finalTick.relationConstraint.skippedOrbitalSystem;
          maximumSpeed = Math.max(maximumSpeed, finalTick.maximumSpeed);
          systemIds.forEach(id => {
            const center = centers().get(id);
            const angle = Math.atan2(center.y, center.x);
            globalTravel.set(id, globalTravel.get(id) + angleStep(angle, globalAngles.get(id)));
            globalAngles.set(id, angle);
          });
          planetIds.forEach(id => {
            const planet = byId(id), star = byId(id.slice(0, 1) + '-star');
            const angle = Math.atan2(planet.y - star.y, planet.x - star.x);
            localTravel.set(id, localTravel.get(id) + angleStep(angle, localAngles.get(id)));
            localAngles.set(id, angle);
            minimumStarClearance = Math.min(minimumStarClearance,
              Math.hypot(planet.x - star.x, planet.y - star.y)
              - star.radius - planet.radius - 1.5);
            if (id.endsWith('-inner')) maximumInnerOrbitRadius = Math.max(
              maximumInnerOrbitRadius, Math.hypot(planet.x - star.x, planet.y - star.y)
            );
          });
          nodes.slice(1).forEach(node => {
            minimumBlackHoleClearance = Math.min(minimumBlackHoleClearance,
              Math.hypot(node.x, node.y) - nodes[0].radius - node.radius - 2.5);
          });
        }
        const envelope = finalTick.farFieldConfinement.envelopeRadius;
        emit({
          dominantOnly: systemIds.every(id => {
            const star = byId(id + '-star');
            return !star.__galaxyOrbitOrder && ['inner', 'outer'].every(tier =>
              !!byId(id + '-' + tier).__galaxyOrbitOrder);
          }),
          localSourceShift: Math.hypot(
            lightPlanetField[0] - heavyPlanetField[0],
            lightPlanetField[1] - heavyPlanetField[1],
          ),
          globalTravel: Object.fromEntries(globalTravel),
          localTravel: Object.fromEntries(localTravel),
          localContacts, systemAnchorContacts, systemRepulsions, surfaceRepulsions,
          maximumSystemRepulsion,
          relationAnchorSkips, relationOrbitalSystemSkips,
          maximumSpeed, minimumBlackHoleClearance, minimumStarClearance,
          maximumInnerOrbitRadius,
          outerBounded: nodes.slice(1).every(node =>
            Math.hypot(node.x, node.y) + node.radius <= envelope + 1e-8),
          finite: nodes.every(node => [node.x, node.y, node.vx, node.vy]
            .every(Number.isFinite)),
        });
        """
    )
    assert report["dominantOnly"] is True
    assert report["localSourceShift"] <= 1e-10
    assert report["finite"] is True
    assert report["outerBounded"] is True
    assert report["localContacts"] > 0
    assert report["systemRepulsions"] > 0
    assert report["maximumSystemRepulsion"] > 0
    # Explicit orbital metadata now takes precedence over the older anchor-only exemption.
    assert report["relationAnchorSkips"] == 0
    assert report["relationOrbitalSystemSkips"] > 0
    assert report["minimumBlackHoleClearance"] >= -1e-9
    assert report["minimumStarClearance"] >= -1e-9
    # The six-unit soft stellar-pressure band intentionally expands the near-surface r=10
    # seeds, but they remain strongly bound below the retired always-on ~20 separation brake.
    assert report["maximumInnerOrbitRadius"] < 18
    assert report["maximumSpeed"] <= 48
    assert min(abs(value) for value in report["globalTravel"].values()) > 1
    assert min(abs(value) for value in report["localTravel"].values()) > 1


@requires_node
def test_render_enforces_horizon_before_paint_for_oversized_static_galaxy() -> None:
    report = _run_engine(
        """
        const nodes = [
          { id: 'black-hole', anchor_role: 'global', community_id: 'core',
            gravity_mass: 64, visual_radius: 8, degree: 1,
            x: 0, y: 0, vx: 0, vy: 0 },
          { id: 'intruder', community_id: 'intruder', gravity_mass: 1,
            visual_radius: 3, degree: 1, x: 0, y: 0, vx: 0, vy: 5 },
        ];
        for (let index = 0; index < 999; index++) nodes.push({
          id: 'filler-' + index, community_id: 'filler-' + index,
          gravity_mass: 1, visual_radius: 3, degree: 1,
          x: 240 + index * 2, y: 180 + (index % 17) * 3, vx: 0, vy: 0,
        });
        const api = G.create(el, { reducedMotion: () => true });
        api.setData({ nodes, links: [], communities: [], community_bridges: [],
          meta: { layout_seed: 7 } });
        const rendered = fg.graphData().nodes;
        const anchor = rendered.find(node => node.id === 'black-hole');
        const intruder = rendered.find(node => node.id === 'intruder');
        const diagnostics = api.physicsDiagnostics();
        const integrator = source.slice(source.indexOf('function integrateGalaxyLeapfrog'),
          source.indexOf('function galaxyMotionDiagnostics'));
        emit({
          staticLayout: diagnostics.staticLayout,
          exclusion: diagnostics.blackHoleExclusion,
          clearance: Math.hypot(intruder.x - anchor.x, intruder.y - anchor.y)
            - anchor.radius - intruder.radius - diagnostics.blackHoleExclusionPadding,
          anchor: [anchor.x, anchor.y, anchor.vx, anchor.vy],
          pinned: [intruder.fx, intruder.fy],
          position: [intruder.x, intruder.y],
          initialBeforeAcceleration: integrator.indexOf('const initialHorizon')
            < integrator.indexOf('const start = galaxyAccelerations'),
        });
        """
    )
    assert report["staticLayout"] is True
    assert report["exclusion"]["contacts"] > 0
    assert report["clearance"] >= -1e-9
    assert report["anchor"] == pytest.approx([0, 0, 0, 0], abs=1e-12)
    assert report["pinned"] == pytest.approx(report["position"], abs=1e-12)
    assert report["initialBeforeAcceleration"] is True


@requires_node
def test_render_reapplies_far_field_envelope_before_static_repaint() -> None:
    """A reused oversized/static payload must not bypass the cached outer boundary."""
    report = _run_engine(
        """
        const nodes = [
          { id: 'black-hole', anchor_role: 'global', community_id: 'core',
            gravity_mass: 64, visual_radius: 8, degree: 1,
            x: 0, y: 0, vx: 0, vy: 0 },
          { id: 'intruder', community_id: 'outer', gravity_mass: 1,
            visual_radius: 3, degree: 1, x: 300, y: 0, vx: 0, vy: 4 },
        ];
        for (let index = 0; index < 999; index++) nodes.push({
          id: 'filler-' + index, community_id: 'filler-' + index,
          gravity_mass: 1, visual_radius: 3, degree: 1,
          x: 160 + index * 2, y: 140 + (index % 17) * 3, vx: 0, vy: 0,
        });
        const api = G.create(el, { reducedMotion: () => true });
        api.setData({ nodes, links: [], communities: [], community_bridges: [],
          meta: { layout_seed: 19 } });
        const initial = api.physicsDiagnostics();
        const rendered = fg.graphData().nodes;
        const anchor = rendered.find(node => node.id === 'black-hole');
        const intruder = rendered.find(node => node.id === 'intruder');
        intruder.x = initial.farFieldConfinement.envelopeRadius + 400;
        intruder.y = 0;
        intruder.fx = intruder.x;
        intruder.fy = intruder.y;
        /* A cosmetic setting keeps the same static arrays; it must still project before
           force-graph's next paint rather than relying on the disabled live integrator. */
        api.setSettings({ font: 13 });
        const diagnostics = api.physicsDiagnostics();
        const clearance = diagnostics.farFieldConfinement.envelopeRadius
          - (Math.hypot(intruder.x - anchor.x, intruder.y - anchor.y) + intruder.radius);
        emit({
          staticLayout: diagnostics.staticLayout,
          initialEnvelope: initial.farFieldConfinement.envelopeRadius,
          confinement: diagnostics.farFieldConfinement,
          clearance,
          pinned: [intruder.fx, intruder.fy],
          position: [intruder.x, intruder.y],
          finite: rendered.every(node => [node.x, node.y, node.vx, node.vy]
            .every(Number.isFinite)),
        });
        """
    )
    assert report["staticLayout"] is True
    assert report["initialEnvelope"] > 0
    assert report["confinement"]["boundedSystems"] >= 1
    assert report["clearance"] >= -1e-8
    assert report["pinned"] == pytest.approx(report["position"], abs=1e-12)
    assert report["finite"] is True


@requires_node
def test_galaxy_inward_convergence_is_monotone_wall_clock_bound_and_keeps_orbits_tangential() -> None:
    report = _run_node(
        """
        const options = {
          gravity: 48, central: true, timestep: 0.021328125, velocityDecay: 0,
          speedLimit: 1000, includeCollisions: false, inwardConvergence: true,
          wallClockSeconds: 1 / 30,
        };
        const anchor = { id: 'black-hole', anchor_role: 'global', community_id: 'core',
          gravity_mass: 100, radius: 12, x: 0, y: 0, vx: 0, vy: 0 };
        const body = { id: 'outer', community_id: 'outer', gravity_mass: 1, radius: 2,
          x: 120, y: 0, vx: 0, vy: 0 };
        const nodes = [anchor, body];
        let previous = Math.hypot(body.x, body.y), monotone = true;
        for (let index = 0; index < 1800; index++) {
          I.integrateGalaxyLeapfrog(nodes, [], [], options);
          const radius = Math.hypot(body.x, body.y);
          monotone = monotone && radius <= previous + 1e-10;
          previous = radius;
        }
        const outbound = [
          { id: 'black-hole', anchor_role: 'global', community_id: 'core',
            gravity_mass: 100, radius: 12, x: 0, y: 0, vx: 0, vy: 0 },
          { id: 'escape', community_id: 'outer', gravity_mass: 1, radius: 2,
            x: 100, y: 0, vx: 30, vy: 0 },
        ];
        const escapeOptions = { ...options, gravity: 0 };
        const escape = I.integrateGalaxyLeapfrog(outbound, [], [], escapeOptions);
        const escapedRadius = Math.hypot(outbound[1].x, outbound[1].y);
        const candidateRadius = 100 + 30 * options.timestep;
        const attemptedOutward = candidateRadius - 100;
        const counteracted = candidateRadius - escapedRadius;
        const tangent = [
          { id: 'black-hole', anchor_role: 'global', community_id: 'core',
            gravity_mass: 100, radius: 12, x: 0, y: 0, vx: 0, vy: 0 },
          { id: 'orbit', community_id: 'outer', gravity_mass: 1, radius: 2,
            x: 120, y: 20, vx: 3, vy: 11 },
        ];
        const initial = new Map([['outer', { radius: 100 }]]);
        const unitX = tangent[1].x / Math.hypot(tangent[1].x, tangent[1].y);
        const unitY = tangent[1].y / Math.hypot(tangent[1].x, tangent[1].y);
        const tangentBefore = tangent[1].vx * -unitY + tangent[1].vy * unitX;
        const direct = I.applyGalaxyInwardConvergence(tangent, tangent[0], initial,
          { wallClockSeconds: 1 / 30 });
        const postX = tangent[1].x / Math.hypot(tangent[1].x, tangent[1].y);
        const postY = tangent[1].y / Math.hypot(tangent[1].x, tangent[1].y);
        const tangentAfter = tangent[1].vx * -postY + tangent[1].vy * postX;
        const localSystem = [
          { id: 'black-hole', anchor_role: 'global', community_id: 'core',
            gravity_mass: 100, x: 0, y: 0, vx: 0, vy: 0 },
          { id: 'star', community_id: 'solar', gravity_mass: 4,
            x: 100, y: 0, vx: 1, vy: 3 },
          { id: 'planet', community_id: 'solar', gravity_mass: 1,
            x: 112, y: 0, vx: -2, vy: 8 },
        ];
        const localCenter = I.communityCenters(localSystem).get('solar');
        const localInitial = new Map([['solar', {
          radius: Math.hypot(localCenter.x, localCenter.y),
        }]]);
        const internalBefore = Math.hypot(
          localSystem[2].x - localSystem[1].x, localSystem[2].y - localSystem[1].y);
        const relativeVelocityBefore = [
          localSystem[2].vx - localSystem[1].vx,
          localSystem[2].vy - localSystem[1].vy,
        ];
        I.applyGalaxyInwardConvergence(localSystem, localSystem[0], localInitial,
          { wallClockSeconds: 1 / 30, gravity: 48, timestep: 0.021328125 });
        const internalAfter = Math.hypot(
          localSystem[2].x - localSystem[1].x, localSystem[2].y - localSystem[1].y);
        const relativeVelocityAfter = [
          localSystem[2].vx - localSystem[1].vx,
          localSystem[2].vy - localSystem[1].vy,
        ];
        const dense = Array.from({ length: 512 }, (_, index) => ({
          id: `n${index}`, x: 40 + (index % 32), y: 30 + Math.floor(index / 32),
          vx: index % 3 - 1, vy: index % 5 - 2, community_id: `dense-${index}`,
        }));
        dense.unshift({ id: 'black-hole', anchor_role: 'global', community_id: 'core',
          x: 0, y: 0, vx: 0, vy: 0 });
        let denseInitial = new Map([...I.communityCenters(dense).entries()].map(
          ([id, center]) => [id, { radius: Math.hypot(center.x, center.y) }]));
        let denseReport;
        for (let index = 0; index < 120; index++) {
          denseReport = I.applyGalaxyInwardConvergence(dense, dense[0], denseInitial,
            { wallClockSeconds: 1 / 30 });
          denseInitial = new Map([...I.communityCenters(dense).entries()].map(
            ([id, center]) => [id, { radius: Math.hypot(center.x, center.y) }]));
        }
        emit({
          minuteRadius: previous, monotone,
          anchor: [anchor.x, anchor.y, anchor.vx, anchor.vy],
          escapedRadius, attemptedOutward, counteracted,
          outboundVelocity: outbound[1].vx,
          tangentBefore, tangentAfter, direct,
          internalBefore, internalAfter,
          relativeVelocityBefore, relativeVelocityAfter,
          finite: nodes.concat(outbound, tangent, dense).every(node =>
            [node.x, node.y, node.vx, node.vy].every(Number.isFinite)),
          denseApplied: denseReport.applied,
          factors: [0, 48, 100].map(gravity =>
            I.galaxyInwardConvergenceFactor(60, gravity)),
          rates: [0, 48, 100].map(gravity =>
            I.galaxyInwardConvergencePerMinute(gravity)),
          convergence: escape.convergence,
        });
        """
    )
    assert report["factors"][0] == pytest.approx(1, abs=1e-12)
    assert report["factors"][1] == pytest.approx(0.75**0.68, abs=1e-12)
    assert report["factors"][2] == pytest.approx(0.75 ** (3.6 * 0.68), abs=1e-12)
    assert report["rates"][0] == pytest.approx(0, abs=1e-12)
    assert report["rates"][1] == pytest.approx(1 - 0.75**0.68, abs=1e-12)
    assert report["rates"][2] > 0.35
    assert report["minuteRadius"] == pytest.approx(120 * 0.75**0.68, abs=1e-8)
    assert report["monotone"] is True
    assert report["anchor"] == pytest.approx([0, 0, 0, 0], abs=1e-12)
    # A 0.63984375-unit escape attempt ends 0.063984375 units inside its initial radius: the field
    # counteracts exactly 110% of the attempted outward displacement.
    assert report["escapedRadius"] == pytest.approx(99.936015625, abs=1e-8)
    assert report["counteracted"] == pytest.approx(
        1.1 * report["attemptedOutward"], abs=1e-8
    )
    assert report["outboundVelocity"] == pytest.approx(-3, abs=1e-12)
    assert report["tangentAfter"] == pytest.approx(report["tangentBefore"], abs=1e-12)
    assert report["internalAfter"] == pytest.approx(report["internalBefore"], abs=1e-12)
    assert report["relativeVelocityAfter"] == pytest.approx(
        report["relativeVelocityBefore"], abs=1e-12
    )
    assert report["finite"] is True
    assert report["denseApplied"] == 512
    assert report["convergence"]["overrides"] == 1


@requires_node
def test_gravity_setting_has_immediate_reversible_system_density_response() -> None:
    report = _run_node(
        """
        const fixture = () => [
          { id: 'black-hole', anchor_role: 'global', community_id: 'core',
            gravity_mass: 20, x: 0, y: 0, vx: 0, vy: 0 },
          { id: 'star-a', anchor_role: 'community', community_id: 'a',
            gravity_mass: 6, x: 120, y: 20, vx: 1, vy: 3 },
          { id: 'planet-a', community_id: 'a', gravity_mass: 1,
            x: 132, y: 20, vx: -2, vy: 7 },
          { id: 'star-b', anchor_role: 'community', community_id: 'b',
            gravity_mass: 4, x: -180, y: 80, vx: -1, vy: -2 },
        ];
        const radius = (nodes, id) => {
          const center = I.communityCenters(nodes).get(id);
          return Math.hypot(center.x, center.y);
        };
        const direct = fixture(), stepped = fixture();
        const before = {
          radius: radius(direct, 'a'),
          diameter: Math.hypot(direct[2].x - direct[1].x, direct[2].y - direct[1].y),
          phase: direct.map(node => [node.x, node.y, node.vx, node.vy]),
        };
        const tightened = I.applyGalaxyGravitySettingResponse(direct, 48, 100);
        const tight = {
          radius: radius(direct, 'a'),
          diameter: Math.hypot(direct[2].x - direct[1].x, direct[2].y - direct[1].y),
          phase: direct.map(node => [node.x, node.y, node.vx, node.vy]),
        };
        const loosened = I.applyGalaxyGravitySettingResponse(direct, 100, 48);
        [60, 80, 100].reduce((previous, setting) => {
          I.applyGalaxyGravitySettingResponse(stepped, previous, setting);
          return setting;
        }, 48);
        emit({
          before, tight,
          roundTrip: direct.map(node => [node.x, node.y, node.vx, node.vy]),
          stepped: stepped.map(node => [node.x, node.y, node.vx, node.vy]),
          expectedRatio: I.galaxyImmediateGravityRadiusScale(100)
            / I.galaxyImmediateGravityRadiusScale(48),
          tightened, loosened,
        });
        """
    )
    assert report["tightened"]["systems"] == 2
    assert report["tightened"]["moved"] == 3
    assert report["tightened"]["maximumShift"] > 5
    assert report["tight"]["radius"] / report["before"]["radius"] == pytest.approx(
        report["expectedRatio"], rel=1e-12
    )
    assert report["tight"]["diameter"] == pytest.approx(
        report["before"]["diameter"], abs=1e-12
    )
    # Density changes translate whole systems; the black hole, internal velocities, and
    # reversible round trip remain exact.
    assert report["tight"]["phase"][0] == report["before"]["phase"][0]
    assert [item[2:] for item in report["tight"]["phase"]] == [
        item[2:] for item in report["before"]["phase"]
    ]
    assert report["loosened"]["ratio"] == pytest.approx(
        1 / report["expectedRatio"], rel=1e-12
    )
    for actual, expected in zip(report["roundTrip"], report["before"]["phase"]):
        assert actual == pytest.approx(expected, abs=1e-12)
    for actual, expected in zip(report["stepped"], report["tight"]["phase"]):
        assert actual == pytest.approx(expected, abs=1e-12)


@requires_node
def test_unequal_mass_local_seed_remains_a_bound_two_body_orbit() -> None:
    report = _run_node(
        """
        const nodes = [
          { id: 'star', anchor_role: 'global', community_id: 'solar',
            gravity_mass: 8, x: 0, y: 0, vx: 0, vy: 0, radius: 4 },
          { id: 'planet', community_id: 'solar',
            gravity_mass: 1, x: 24, y: 0, vx: 0, vy: 0, radius: 2 },
        ];
        I.seedGalaxyOrbits(nodes, 31, 48, 7.68, false);
        let minimum = Infinity, maximum = 0, centered = true;
        for (let step = 0; step < 1200; step++) {
          I.integrateGalaxyLeapfrog(nodes, [], [], {
            gravity: 48, softening: 7.68, central: false,
            timestep: 0.525, velocityDecay: 0, speedLimit: 100,
            collisionStrength: 0,
          });
          const separation = Math.hypot(
            nodes[1].x - nodes[0].x, nodes[1].y - nodes[0].y
          );
          minimum = Math.min(minimum, separation);
          maximum = Math.max(maximum, separation);
          centered = centered && nodes[0].x === 0 && nodes[0].y === 0
            && nodes[0].vx === 0 && nodes[0].vy === 0;
        }
        emit({ minimum, maximum, centered,
          finite: nodes.every(node => [node.x, node.y, node.vx, node.vy]
            .every(Number.isFinite)) });
        """
    )
    assert report["centered"] is True
    assert report["finite"] is True
    assert report["minimum"] >= 23.9
    # Exact-2x gravity raises the integrator's dimensionless step at this deliberately coarse
    # 0.525 fixture timestep; the orbit remains within 1.7% of its seeded radius.
    assert report["maximum"] <= 24.4


@requires_node
def test_galaxy_motion_diagnostics_are_mass_weighted_finite_and_read_only() -> None:
    report = _run_node(
        """
        const clean = [
          { id: 'heavy', x: 2, y: 0, vx: 3, vy: 4, gravity_mass: 4 },
          { id: 'light', x: -2, y: 0, vx: -2, vy: 0, gravity_mass: 1 },
          { id: 'history', x: Infinity, y: 0, vx: NaN, vy: 0, ghost: true },
        ];
        const before = JSON.stringify(clean);
        const diagnostics = I.galaxyMotionDiagnostics(clean);
        const dirty = I.galaxyMotionDiagnostics([
          { id: 'bad', x: NaN, y: 0, vx: Infinity, vy: 0, gravity_mass: 2 },
        ]);
        emit({ diagnostics, dirty, unchanged: JSON.stringify(clean) === before });
        """
    )
    diagnostics = report["diagnostics"]
    assert diagnostics["bodies"] == 2
    assert diagnostics["invalidBodies"] == 0
    assert diagnostics["totalMass"] == 5
    assert diagnostics["centerX"] == pytest.approx(1.2)
    assert diagnostics["centerY"] == 0
    assert [diagnostics["momentumX"], diagnostics["momentumY"]] == pytest.approx([10, 16])
    assert diagnostics["kineticEnergy"] == pytest.approx(52)
    assert diagnostics["angularMomentum"] == pytest.approx(12.8)
    assert diagnostics["maxSpeed"] == pytest.approx(5)
    assert report["dirty"]["invalidBodies"] == 1
    assert all(math.isfinite(report["dirty"][key]) for key in (
        "totalMass", "centerX", "centerY", "momentum", "kineticEnergy", "maxSpeed"
    ))
    assert report["unchanged"] is True


@requires_node
def test_fixed_step_speed_guard_uses_one_common_scale_and_preserves_momentum() -> None:
    report = _run_node(
        """
        const bodies = [
          { id: 'heavy', x: 0, y: 0, gravity_mass: 10, vx: 10, vy: 0 },
          { id: 'light', x: 100, y: 0, gravity_mass: 1, vx: -100, vy: 0 },
          { id: 'invalid', x: 0, y: 100, gravity_mass: 2, vx: NaN, vy: Infinity },
          { id: 'history', x: 0, y: -100, gravity_mass: 0, vx: 99, vy: -99, ghost: true },
        ];
        I.integrateGalaxyLeapfrog(bodies, [], [], {
          gravity: 0, central: false, includeBridges: false, includeRelations: false,
          includeCollisions: false, timestep: 0.001, velocityDecay: 0, speedLimit: 14.4,
        });
        emit({
          velocities: bodies.map(node => [node.vx, node.vy]),
          momentum: [
            bodies.filter(node => !node.ghost).reduce(
              (sum, node) => sum + node.gravity_mass * node.vx, 0
            ),
            bodies.filter(node => !node.ghost).reduce(
              (sum, node) => sum + node.gravity_mass * node.vy, 0
            ),
          ],
          maximum: Math.max(...bodies.filter(node => !node.ghost)
            .map(node => Math.hypot(node.vx, node.vy))),
        });
        """
    )
    assert report["velocities"][0] == pytest.approx([1.44, 0])
    assert report["velocities"][1] == pytest.approx([-14.4, 0])
    assert report["velocities"][2] == pytest.approx([0, 0])
    assert report["velocities"][3] == pytest.approx([99, -99])
    assert report["momentum"] == pytest.approx([0, 0], abs=1e-12)
    assert report["maximum"] == pytest.approx(14.4)


@requires_node
def test_barnes_hut_matches_exact_fixture_with_subquadratic_traversal() -> None:
    report = _run_node(
        """
        const fixture = Array.from({ length: 80 }, (_, i) => ({
          id: 'n' + i, x: (i % 10) * 12 + (i % 3), y: Math.floor(i / 10) * 11,
          vx: 0, vy: 0, gravity_mass: 1 + (i % 5), community_id: 'large',
        }));
        const exact = fixture.map(n => ({ ...n })), approximate = fixture.map(n => ({ ...n }));
        I.applyGalaxyGravity(exact, { gravity: 2, softening: 5, alpha: 1, exactLimit: 1000 });
        const stats = I.applyGalaxyGravity(approximate, {
          gravity: 2, softening: 5, alpha: 1, exactLimit: 64, theta: 0.85,
        });
        let error = 0, signal = 0;
        exact.forEach((node, i) => {
          error += (node.vx - approximate[i].vx) ** 2 + (node.vy - approximate[i].vy) ** 2;
          signal += node.vx ** 2 + node.vy ** 2;
        });
        emit({
          relativeRms: Math.sqrt(error / signal), stats, quadratic: fixture.length ** 2,
          momentum: [
            approximate.reduce((sum, node) => sum + node.gravity_mass * node.vx, 0),
            approximate.reduce((sum, node) => sum + node.gravity_mass * node.vy, 0),
          ],
        });
        """
    )
    assert report["stats"]["approximations"] > 0
    assert report["stats"]["traversals"] < report["quadratic"]
    assert report["relativeRms"] < 0.25
    assert report["momentum"] == pytest.approx([0, 0], abs=1e-10)


@requires_node
def test_community_bridge_force_scales_with_evidence_and_preserves_momentum() -> None:
    report = _run_node(
        """
        const run = strength => {
          const nodes = [
            { id: 'left', x: 0, y: 0, vx: 0, vy: 0, gravity_mass: 2, community_id: 'left' },
            { id: 'right', x: 20, y: 0, vx: 0, vy: 0, gravity_mass: 4, community_id: 'right' },
          ];
          const stats = I.applyCommunityBridgeGravity(nodes, [{
            source_community: 'left', target_community: 'right', physics_strength: strength,
          }], { gravity: 4, softening: 8, alpha: 1 });
          return { nodes, stats };
        };
        const weak = run(0.4), strong = run(0.8), none = run(0);
        emit({
          ratio: strong.nodes[0].vx / weak.nodes[0].vx,
          momentum: 2 * strong.nodes[0].vx + 4 * strong.nodes[1].vx,
          applied: strong.stats.bridges,
          none: none.nodes.map(n => [n.vx, n.vy]),
        });
        """
    )
    assert report["ratio"] == pytest.approx(2)
    assert report["momentum"] == pytest.approx(0, abs=1e-12)
    assert report["applied"] == 1
    assert report["none"] == [[0, 0], [0, 0]]


@requires_node
def test_orbital_seed_is_deterministic_tangential_and_one_shot() -> None:
    report = _run_node(
        """
        const fixture = () => [
          { id: 'sun', x: 0, y: 0, gravity_mass: 8, community_id: 's' },
          { id: 'planet', x: 20, y: 0, gravity_mass: 1, community_id: 's' },
        ];
        const first = fixture(), second = fixture(), reduced = fixture();
        const haunted = fixture().concat([{
          id: 'history', x: 10, y: 10, vx: 9, vy: -7, gravity_mass: 0,
          community_id: 's', ghost: true,
        }]);
        I.seedGalaxyOrbits(first, 42, 48, 8, false);
        I.seedGalaxyOrbits(second, 42, 48, 8, false);
        const initial = first.map(n => [n.vx, n.vy]);
        first[1].vx = 123; first[1].vy = -456;
        I.seedGalaxyOrbits(first, 42, 48, 8, false);
        I.seedGalaxyOrbits(reduced, 42, 48, 8, true);
        I.seedGalaxyOrbits(reduced, 42, 48, 8, false);
        I.seedGalaxyOrbits(haunted, 42, 48, 8, false);
        emit({
          deterministic: initial,
          second: second.map(n => [n.vx, n.vy]),
          tangentialDot: 20 * initial[1][0],
          oneShot: [first[1].vx, first[1].vy],
          reduced: reduced.map(n => [n.vx, n.vy]),
          ghost: [haunted[2].vx, haunted[2].vy],
          hauntedMomentum: [
            8 * haunted[0].vx + haunted[1].vx,
            8 * haunted[0].vy + haunted[1].vy,
          ],
        });
        """
    )
    assert report["deterministic"] == report["second"]
    assert report["tangentialDot"] == pytest.approx(0, abs=1e-12)
    assert report["oneShot"] == [123, -456]
    assert report["reduced"] == report["deterministic"]
    assert report["ghost"] == [0, 0]
    assert report["hauntedMomentum"] == pytest.approx([0, 0], abs=1e-10)


@requires_node
def test_late_planet_gets_a_one_shot_orbit_without_erasing_the_existing_system() -> None:
    """Incremental reveal seeds the fresh planet and preserves the old star-relative phase."""
    report = _run_node(
        """
        const nodes = [
          { id: 'star', anchor_role: 'community', community_id: 'solar',
            system_anchor_id: 'star', orbit_tier: 0, gravity_mass: 8, radius: 5,
            x: 0, y: 0, vx: 0, vy: 0 },
          { id: 'p1', community_id: 'solar', system_anchor_id: 'star', orbit_tier: 1,
            gravity_mass: 1, radius: 3, x: 16, y: 0, vx: 0, vy: 0 },
        ];
        const momentum = () => ['vx', 'vy'].map(axis => nodes.reduce((sum, node) =>
          sum + node.gravity_mass * (Number(node[axis]) || 0), 0));
        const relative = (node, anchor) => [node.vx - anchor.vx, node.vy - anchor.vy];
        I.seedGalaxyOrbits(nodes, 901, 48, 32, false);
        const star = nodes[0], p1 = nodes[1];
        const oldRelative = relative(p1, star);
        const oldPhase = [p1.x - star.x, p1.y - star.y];
        const beforeMomentum = momentum();
        const p2 = { id: 'p2', community_id: 'solar', system_anchor_id: 'star', orbit_tier: 2,
          gravity_mass: 1, radius: 3, x: 0, y: 24, vx: 0, vy: 0 };
        nodes.push(p2);
        const revealedMomentum = momentum();
        I.seedGalaxyOrbits(nodes, 901, 48, 32, false);
        const afterRelative = relative(p1, star);
        const freshRelative = relative(p2, star);
        const freshRadialDot = (p2.x - star.x) * freshRelative[0]
          + (p2.y - star.y) * freshRelative[1];
        const oldAngular = oldPhase[0] * oldRelative[1] - oldPhase[1] * oldRelative[0];
        const freshAngular = (p2.x - star.x) * freshRelative[1]
          - (p2.y - star.y) * freshRelative[0];
        const afterMomentum = momentum();
        const afterFirst = nodes.map(node => [node.vx, node.vy]);
        I.seedGalaxyOrbits(nodes, 901, 48, 32, false);
        emit({
          oldRelative, afterRelative, oldPhase,
          newPhase: [p1.x - star.x, p1.y - star.y],
          freshRelative, freshRadialDot, oldAngular, freshAngular,
          beforeMomentum, revealedMomentum, afterMomentum,
          afterFirst, afterSecond: nodes.map(node => [node.vx, node.vy]),
          seeded: nodes.map(node => !!node.__galaxyOrbitSeeded),
        });
        """
    )
    assert report["seeded"] == [True, True, True]
    assert math.hypot(*report["freshRelative"]) > 1e-6
    assert report["freshRadialDot"] == pytest.approx(0, abs=1e-10)
    assert math.copysign(1, report["freshAngular"]) == math.copysign(
        1, report["oldAngular"]
    )
    assert report["afterRelative"] == pytest.approx(report["oldRelative"], abs=1e-10)
    assert report["newPhase"] == pytest.approx(report["oldPhase"], abs=1e-12)
    assert report["beforeMomentum"] == pytest.approx([0, 0], abs=1e-10)
    assert report["revealedMomentum"] == pytest.approx(report["beforeMomentum"], abs=1e-10)
    assert report["afterMomentum"] == pytest.approx(report["beforeMomentum"], abs=1e-10)
    for first, second in zip(report["afterFirst"], report["afterSecond"]):
        assert second == pytest.approx(first, abs=1e-12)


@requires_node
def test_many_massive_satellites_each_keep_a_star_only_circular_seed_and_visible_phase() -> None:
    """Aggregate stellar recoil and the soft pressure band cannot zero a planet's orbit seed."""
    report = _run_node(
        """
        const nodes = [{ id: 'star', anchor_role: 'community', community_id: 'solar',
          gravity_mass: 8, radius: 5, x: 0, y: 0, vx: 0, vy: 0 }];
        // The counter-orbiting probe lies inside the star's smooth 6-unit pressure band. The
        // many much heavier bodies on the other side make aggregate anchor recoil dominant in
        // the old relative-acceleration seeder (total satellite mass is 40 > star mass 8).
        nodes.push({ id: 'probe', community_id: 'solar', system_anchor_id: 'star', orbit_tier: 1,
          gravity_mass: 1, radius: 3, x: -13, y: 0, vx: 0, vy: 0 });
        for (let index = 0; index < 13; index += 1) {
          const angle = -0.78 + index * 0.13, radius = 21 + index * 2.2;
          nodes.push({ id: `heavy-${index}`, community_id: 'solar', system_anchor_id: 'star',
            orbit_tier: index + 2, gravity_mass: 3, radius: 2,
            x: Math.cos(angle) * radius, y: Math.sin(angle) * radius, vx: 0, vy: 0 });
        }
        const star = nodes[0], localG = I.galaxyStellarGravityConstant(48), softening = 32;
        I.seedGalaxyOrbits(nodes, 763, 48, softening, false);
        const seeded = nodes.slice(1).map(node => {
          const dx = node.x - star.x, dy = node.y - star.y, radius = Math.hypot(dx, dy);
          const relativeVx = node.vx - star.vx, relativeVy = node.vy - star.vy;
          const rawInward = localG * star.gravity_mass * radius
            / Math.pow(radius * radius + softening * softening, 1.5);
          return {
            id: node.id, radius, expectedSpeed: Math.sqrt(rawInward * radius),
            relativeSpeed: Math.hypot(relativeVx, relativeVy),
            radialDot: dx * relativeVx + dy * relativeVy,
            angular: dx * relativeVy - dy * relativeVx,
          };
        });
        const initialAngles = new Map(nodes.slice(1).map(node => [node.id,
          Math.atan2(node.y - star.y, node.x - star.x)]));
        const travel = new Map(nodes.slice(1).map(node => [node.id, 0]));
        const delta = (next, previous) => Math.atan2(Math.sin(next - previous),
          Math.cos(next - previous));
        let clearance = Infinity, maximumSpeed = 0, maximumRelativeRadialAcceleration = -Infinity;
        const options = {
            gravity: 48, softening, central: false, includeMutualSystems: false,
            includeRelations: false, includeBridges: false, includeCollisions: false,
            includeOrbitalSeparation: false, skipSystemAnchorPairs: true,
            systemAnchorExclusionPadding: 1.5, localRelativeSpeedLimit: 48,
            // This runtime-centrality oracle isolates the dominant-star law. The separate
            // pressure test covers the deliberate outward near-surface band.
            systemAnchorRepulsionAcceleration: 0,
            timestep: 0.032, velocityDecay: 0.00005, speedLimit: 48,
          };
        for (let step = 0; step < 360; step += 1) {
          const acceleration = I.galaxyAccelerations(nodes, [], [], options);
          const anchorAcceleration = acceleration.get(star);
          nodes.slice(1).forEach(node => {
            const dx = node.x - star.x, dy = node.y - star.y;
            const radius = Math.hypot(dx, dy);
            const bodyAcceleration = acceleration.get(node);
            maximumRelativeRadialAcceleration = Math.max(maximumRelativeRadialAcceleration,
              ((bodyAcceleration.ax - anchorAcceleration.ax) * dx
                + (bodyAcceleration.ay - anchorAcceleration.ay) * dy) / radius);
          });
          const tick = I.integrateGalaxyLeapfrog(nodes, [], [], options);
          maximumSpeed = Math.max(maximumSpeed, tick.maximumSpeed);
          nodes.slice(1).forEach(node => {
            const angle = Math.atan2(node.y - star.y, node.x - star.x);
            travel.set(node.id, travel.get(node.id) + delta(angle, initialAngles.get(node.id)));
            initialAngles.set(node.id, angle);
            clearance = Math.min(clearance, Math.hypot(node.x - star.x, node.y - star.y)
              - node.radius - star.radius - 1.5);
          });
        }
        emit({ seeded, travel: [...travel.values()], clearance, maximumSpeed,
          maximumRelativeRadialAcceleration,
          finite: nodes.every(node => [node.x, node.y, node.vx, node.vy].every(Number.isFinite)) });
        """
    )
    assert report["finite"] is True
    assert report["clearance"] >= -1e-9
    assert report["maximumSpeed"] <= 48
    seeded = report["seeded"]
    assert len(seeded) == 14
    # The velocity is the star-only softened circular law, even for the pressure-band probe;
    # all massive satellites share one local spin direction and none has a radial-only seed.
    assert all(item["relativeSpeed"] == pytest.approx(item["expectedSpeed"], rel=1e-10)
               for item in seeded), seeded
    assert all(abs(item["radialDot"]) <= 1e-10 for item in seeded), seeded
    assert all(abs(item["angular"]) > 1e-8 for item in seeded), seeded
    signs = {math.copysign(1, item["angular"]) for item in seeded}
    assert len(signs) == 1
    # Every live sample still sees an inward dominant-star relative acceleration even though
    # satellites outweigh their star fivefold. Aggregate star recoil must be common drift, not
    # an outward local force on the opposite probe.
    assert report["maximumRelativeRadialAcceleration"] < 0, report
    assert min(abs(value) for value in report["travel"]) > 0.45, report


@requires_node
def test_system_orbital_seed_preserves_barycentre_and_hierarchical_motion() -> None:
    report = _run_node(
        """
        const fixture = () => [
          { id: 'a', x: -100, y: 0, gravity_mass: 16, community_id: 'a' },
          { id: 'b', x: 80, y: 0, gravity_mass: 9, community_id: 'b' },
          { id: 'c', x: 0, y: 120, gravity_mass: 4, community_id: 'c' },
        ];
        const first = fixture(), second = fixture(), reduced = fixture(), late = fixture();
        I.seedGalaxySystemOrbits(first, 91, 48, 40, false);
        I.seedGalaxySystemOrbits(second, 91, 48, 40, false);
        const totalMass = first.reduce((sum, node) => sum + node.gravity_mass, 0);
        const bx = first.reduce((sum, node) => sum + node.x * node.gravity_mass, 0) / totalMass;
        const by = first.reduce((sum, node) => sum + node.y * node.gravity_mass, 0) / totalMass;
        const initial = first.map(node => [node.vx, node.vy]);
        first[0].vx = 123; first[0].vy = -456;
        I.seedGalaxySystemOrbits(first, 91, 48, 40, false);
        I.seedGalaxySystemOrbits(reduced, 91, 48, 40, true);
        I.seedGalaxySystemOrbits(reduced, 91, 48, 40, false);
        Object.defineProperty(late[0], '__galaxySystemOrbitSeeded', {
          value: true, writable: true, configurable: true,
        });
        Object.defineProperty(late[1], '__galaxySystemOrbitSeeded', {
          value: true, writable: true, configurable: true,
        });
        late[0].vx = 1; late[0].vy = 2;
        late[1].vx = -16 / 9; late[1].vy = -32 / 9;
        I.seedGalaxySystemOrbits(late, 91, 48, 40, false);
        emit({
          deterministic: initial,
          second: second.map(node => [node.vx, node.vy]),
          radialDots: second.map(node => (node.x - bx) * node.vx + (node.y - by) * node.vy),
          momentum: [
            second.reduce((sum, node) => sum + node.gravity_mass * node.vx, 0),
            second.reduce((sum, node) => sum + node.gravity_mass * node.vy, 0),
          ],
          angularSpeeds: second.map(node => {
            const dx = node.x - bx, dy = node.y - by;
            return Math.abs(dx * node.vy - dy * node.vx) / (dx * dx + dy * dy);
          }),
          moving: second.every(node => Math.hypot(node.vx, node.vy) > 0),
          oneShot: [first[0].vx, first[0].vy],
          reduced: reduced.map(node => [node.vx, node.vy]),
          late: late.map(node => [node.vx, node.vy]),
          lateSeeded: late.every(node => node.__galaxySystemOrbitSeeded),
        });
        """
    )
    assert report["deterministic"] == report["second"]
    # A shared barycentre correction removes net galaxy momentum. It can add a uniform
    # translation, so every absolute velocity need not be exactly tangential; the angular
    # rates must nevertheless remain differential rather than a solid-body constant.
    assert max(report["angularSpeeds"]) - min(report["angularSpeeds"]) > 1e-6
    assert report["momentum"] == pytest.approx([0, 0], abs=1e-10)
    assert report["moving"] is True
    assert report["oneShot"] == [123, -456]
    assert report["reduced"] == report["deterministic"]
    assert report["late"][0] == pytest.approx([1, 2])
    assert report["late"][1] == pytest.approx([-16 / 9, -32 / 9])
    assert report["late"][2] == pytest.approx([0, 0])
    assert report["lateSeeded"] is True


@requires_node
def test_global_system_seed_uses_release_stable_speed_cap_without_breaking_momentum() -> None:
    """High-field systems use the release-stable visible cap without injecting momentum."""
    report = _run_node(
        """
        const nodes = [
          { id: 'bh', anchor_role: 'global', community_id: 'core', gravity_mass: 1000,
            x: 0, y: 0, vx: 0, vy: 0 },
          { id: 'east-star', anchor_role: 'community', community_id: 'east', gravity_mass: 1,
            x: 100, y: 0, vx: 0, vy: 0 },
          { id: 'west-star', anchor_role: 'community', community_id: 'west', gravity_mass: 1,
            x: -100, y: 0, vx: 0, vy: 0 },
        ];
        const field = I.galaxyBlackHoleField(nodes, { gravity: 400, softening: 40 });
        I.seedGalaxySystemOrbits(nodes, 183, 400, 40, false);
        const anchor = nodes[0];
        emit({
          fieldSpeeds: field.systems.map(item => item.circularSpeed),
          relative: nodes.slice(1).map(node => {
            const dx = node.x - anchor.x, dy = node.y - anchor.y;
            const vx = node.vx - anchor.vx, vy = node.vy - anchor.vy;
            return { speed: Math.hypot(vx, vy), radialDot: dx * vx + dy * vy,
              angular: dx * vy - dy * vx };
          }),
          momentum: ['vx', 'vy'].map(axis => nodes.reduce((sum, node) =>
            sum + node.gravity_mass * node[axis], 0)),
          anchor: [anchor.x, anchor.y, anchor.vx, anchor.vy],
        });
        """
    )
    seed_limit = 18
    assert min(report["fieldSpeeds"]) > seed_limit
    # The deterministic settling kick and barycentric correction may trim the absolute
    # star-relative speed slightly. It remains calibrated to the release-stable 18-unit cap.
    assert all(seed_limit * 0.9 < item["speed"] <= seed_limit * 1.01
               for item in report["relative"]), report
    assert all(abs(item["angular"]) > 1e-8 for item in report["relative"])
    assert report["momentum"] == pytest.approx([0, 0], abs=1e-10)
    assert report["anchor"][:2] == pytest.approx([0, 0], abs=1e-12)


@requires_node
def test_galaxy_live_limit_matches_the_complete_overview_contract() -> None:
    """The complete public overview remains expanded and physical; larger scenes stay bounded."""
    report = _run_engine(
        """
        const within = [
          I.galaxySceneWithinLiveLimit({ nodes: Array(1000), links: Array(2000) }),
          I.galaxySceneWithinLiveLimit({ nodes: Array(1001), links: [] }),
          I.galaxySceneWithinLiveLimit({ nodes: [], links: Array(2001) }),
        ];
        let nextFrame = 1;
        const frames = new Map();
        window.requestAnimationFrame = callback => {
          const id = nextFrame++; frames.set(id, callback); return id;
        };
        window.cancelAnimationFrame = id => frames.delete(id);
        const flush = now => {
          const batch = [...frames.values()]; frames.clear(); batch.forEach(callback => callback(now));
        };
        const scene = (count, edgeCount) => ({
          meta: { layout_seed: 91 },
          nodes: Array.from({ length: count }, (_, index) => ({
            id: index === 0 ? 'black-hole' : `node-${index}`,
            community_id: 'core',
            system_anchor_id: 'black-hole',
            anchor_role: index === 0 ? 'global' : 'none',
            orbit_tier: index,
            gravity_mass: index === 0 ? 16 : 1,
            visual_radius: index === 0 ? 8 : 2,
            x: index === 0 ? 0 : 45 + index,
            y: index % 7,
            vx: 0,
            vy: 0,
          })),
          edges: Array.from({ length: edgeCount }, (_, index) => ({
            id: `edge-${index}`, source: 'black-hole',
            target: `node-${1 + index % Math.max(1, count - 1)}`,
            layer: 'semantic', strength: 0.5, rest_length: 20, spring_strength: 0.08,
          })),
        });

        const galaxy = G.create(el, { reducedMotion: () => true });
        galaxy.setData(scene(1000, 2000));
        store.onZoom({ k: 0.1 });
        const before = galaxy.physicsDiagnostics();
        flush(0); flush(34); flush(68);
        const live = galaxy.physicsDiagnostics();
        const autoCollapsed = galaxy.state().collapsed;
        galaxy.setCollapse(true);
        const explicitCollapsed = galaxy.state().collapsed;
        galaxy.setCollapse(false);
        galaxy.setData(scene(1001, 2000));
        const nodeOverflow = galaxy.physicsDiagnostics();
        galaxy.setData(scene(1000, 2001));
        const edgeOverflow = galaxy.physicsDiagnostics();
        galaxy.destroy();

        const full = G.create(el, {
          reducedMotion: () => false,
          renderMode: 'full',
        });
        full.setPreset('original');
        full.setData(scene(601, 600));
        const classicFull = full.physicsDiagnostics();
        emit({ within, before, live, autoCollapsed, explicitCollapsed, nodeOverflow,
          edgeOverflow, classicFull });
        """
    )
    assert report["within"] == [True, False, False]
    assert report["before"]["renderedNodes"] == 1000
    assert report["before"]["renderedLinks"] == 2000
    assert report["before"]["galaxyLiveNodeLimit"] == 1000
    assert report["before"]["galaxyLiveLinkLimit"] == 2000
    assert report["before"]["withinGalaxyLiveLimit"] is True
    assert report["before"]["largeRenderTier"] is True
    assert report["before"]["staticLayout"] is False
    assert report["before"]["active"] is True
    assert report["live"]["steps"] >= report["before"]["steps"] + 3
    assert report["live"]["active"] is True
    assert report["autoCollapsed"] is False
    assert report["explicitCollapsed"] is True
    assert report["nodeOverflow"]["staticLayout"] is True
    assert report["edgeOverflow"]["staticLayout"] is True
    assert report["classicFull"]["mode"] == "original"
    assert report["classicFull"]["staticLayout"] is True


@requires_node
def test_reduced_motion_keeps_eight_independent_solar_systems_orbiting() -> None:
    """The accessible visual preference keeps a visibly quick two-scale galaxy live.

    This deliberately uses eight independently phased systems and fixed solver time rather
    than wall-clock delay.  The former tuning only covered a barely visible minimum travel
    (0.317 rad around the black hole and 0.608 rad locally in this fixture).  A Galaxy has to
    make both levels of hierarchy legible in the ordinary dashboard interval.
    """
    report = _run_node(
        """
        const nodes=[{id:'bh',anchor_role:'global',community_id:'core',gravity_mass:16,radius:10,x:0,y:0,vx:0,vy:0}],links=[];
        for(let s=0;s<8;s++){const p=s*2.4,r=105+s*13,cx=Math.cos(p)*r,cy=Math.sin(p)*r*.82;
          for(let m=0;m<3;m++){const id=`s${s}-${m}`,q=m?14+m*5:0;
            nodes.push({id,community_id:`s${s}`,system_anchor_id:`s${s}-0`,anchor_role:m?'none':'community',orbit_tier:m,gravity_mass:m?1:7,radius:m?3:5,x:cx+Math.cos(p+m*1.5)*q,y:cy+Math.sin(p+m*1.5)*q,vx:0,vy:0});
            if(m)links.push({source:`s${s}-0`,target:id,rest_length:q,spring_strength:.08});}}
        const o={gravity:48,softening:32,centralSoftening:40,includeMutualSystems:true,mutualSystemGravityFraction:.12,mutualSystemSoftening:80,includeRelations:true,includeRelationSprings:false,skipSystemAnchorRelations:true,orbitScale:.25,relationConstraintRate:24,relationConstraintMaxCorrection:12,relationPadding:12,includeOrbitalSeparation:true,orbitalSeparationPadding:12,orbitalSeparationStrength:.8,crossCommunitySeparationPadding:1.5,crossCommunitySeparationStrength:.144,orbitalSeparationMaxCorrection:4,orbitalSeparationMaxVelocityCorrection:8,preserveLocalTangentialVelocity:true,skipSystemAnchorPairs:true,systemAnchorExclusionPadding:1.5,includeBlackHoleExclusion:true,blackHoleExclusionPadding:2.5,includeFarFieldConfinement:true,farFieldEnvelopeScale:1.75,farFieldMinimumRadius:96,farFieldSoftFraction:.82,farFieldAcceleration:12,farFieldMaxAcceleration:16,localRelativeSpeedLimit:48,timestep:.032,wallClockSeconds:1/30,inwardConvergence:true,velocityDecay:.00005,speedLimit:48,includeCollisions:false};
        I.seedGalaxyOrbits(nodes,91,48,32,true); I.seedGalaxySystemOrbits(nodes,91,48,40,true);
        const cs=()=>I.communityCenters(nodes),d=(a,b)=>Math.atan2(Math.sin(a-b),Math.cos(a-b)),systems=[...Array(8).keys()].map(i=>`s${i}`),planets=nodes.filter(n=>n.orbit_tier>0);
        const pg=new Map(systems.map(k=>{const c=cs().get(k);return[k,Math.atan2(c.y,c.x)]})),pl=new Map(planets.map(n=>{const a=nodes.find(x=>x.id===n.system_anchor_id);return[n.id,Math.atan2(n.y-a.y,n.x-a.x)]})),gt=new Map(systems.map(k=>[k,0])),lt=new Map(planets.map(n=>[n.id,0]));
        let clear=Infinity,max=0,envelope=0,speedCaps=0;for(let i=0;i<240;i++){const t=I.integrateGalaxyLeapfrog(nodes,links,[],o);max=Math.max(max,t.maximumSpeed);speedCaps+=t.speedCapped?1:0;envelope=t.farFieldConfinement.envelopeRadius;systems.forEach(k=>{const c=cs().get(k),a=Math.atan2(c.y,c.x);gt.set(k,gt.get(k)+d(a,pg.get(k)));pg.set(k,a)});planets.forEach(n=>{const a=nodes.find(x=>x.id===n.system_anchor_id),q=Math.atan2(n.y-a.y,n.x-a.x);lt.set(n.id,lt.get(n.id)+d(q,pl.get(n.id)));pl.set(n.id,q);clear=Math.min(clear,Math.hypot(n.x-a.x,n.y-a.y)-n.radius-a.radius-1.5)});}
        emit({global:[...gt.values()],local:[...lt.values()],clear,max,speedCaps,envelope,bounded:nodes.slice(1).every(n=>Math.hypot(n.x,n.y)+n.radius<=envelope+1e-8),finite:nodes.every(n=>[n.x,n.y,n.vx,n.vy].every(Number.isFinite))});
        """
    )
    assert report["finite"] is report["bounded"] is True
    assert report["clear"] >= -1e-9
    assert report["max"] <= 48
    assert report["speedCaps"] == 0
    # At 30 Hz this is eight seconds of real solver time: every solar-system COM advances a
    # clearly visible 26° and every planet advances 40° about its dominant star. These
    # thresholds reject the previous slow, technically-nonzero drift while leaving bounded
    # eccentric motion rather than requiring a rigid carousel.
    assert min(abs(value) for value in report["global"]) > 0.45, report
    assert min(abs(value) for value in report["local"]) > 0.70, report


@requires_node
def test_reduced_motion_has_exact_dual_scale_orbit_parity_and_star_surface_safety() -> None:
    """Reduced visual motion cannot alter Galaxy initial conditions or stellar boundaries."""
    report = _run_node(
        """
        const make = () => {
          const nodes = [{ id: 'bh', anchor_role: 'global', community_id: 'core',
            gravity_mass: 20, radius: 10, x: 0, y: 0, vx: 0, vy: 0 }], links = [];
          [0.25, 2.4, 4.6, 5.65].forEach((phase, index) => {
            const r = 80 + index * 25, id = `s${index}`;
            const x = Math.cos(phase) * r, y = Math.sin(phase) * r * 0.82;
            nodes.push({ id: `${id}-star`, anchor_role: 'community', community_id: id,
              system_anchor_id: `${id}-star`, orbit_tier: 0, gravity_mass: 8, radius: 5,
              x, y, vx: 0, vy: 0 });
            // The first satellite begins through the painted surface. The permanent stellar
            // exclusion must project it before the fast orbital clock starts.
            const distance = index === 0 ? 9 : 15 + index;
            nodes.push({ id: `${id}-planet`, community_id: id,
              system_anchor_id: `${id}-star`, orbit_tier: 1, gravity_mass: 1, radius: 3,
              x: x + Math.cos(phase + 1.1) * distance,
              y: y + Math.sin(phase + 1.1) * distance, vx: 0, vy: 0 });
            links.push({ source: `${id}-star`, target: `${id}-planet`,
              rest_length: distance, spring_strength: 0.08 });
          });
          return { nodes, links };
        };
        const delta = (next, previous) => Math.atan2(Math.sin(next - previous),
          Math.cos(next - previous));
        const run = reducedMotion => {
          const { nodes, links } = make();
          const options = {
            gravity: 48, softening: 32, centralSoftening: 40,
            includeMutualSystems: true, mutualSystemGravityFraction: 0.12,
            mutualSystemSoftening: 80, includeRelations: true, includeRelationSprings: false,
            skipSystemAnchorRelations: true, orbitScale: 0.25, relationConstraintRate: 24,
            relationConstraintMaxCorrection: 12, relationPadding: 12,
            includeOrbitalSeparation: true, orbitalSeparationPadding: 12,
            orbitalSeparationStrength: 0.8, crossCommunitySeparationPadding: 1.5,
            crossCommunitySeparationStrength: 0.144, orbitalSeparationMaxCorrection: 4,
            orbitalSeparationMaxVelocityCorrection: 8, preserveLocalTangentialVelocity: true,
            skipSystemAnchorPairs: true, systemAnchorExclusionPadding: 1.5,
            includeBlackHoleExclusion: true, blackHoleExclusionPadding: 2.5,
            includeFarFieldConfinement: true, farFieldEnvelopeScale: 1.75,
            farFieldMinimumRadius: 96, farFieldSoftFraction: 0.82,
            farFieldAcceleration: 12, farFieldMaxAcceleration: 16,
            localRelativeSpeedLimit: 48, timestep: 0.032, wallClockSeconds: 1 / 30,
            inwardConvergence: true, velocityDecay: 0.00005, speedLimit: 48,
            includeCollisions: false,
          };
          I.seedGalaxyOrbits(nodes, 4401, 48, 32, reducedMotion);
          I.seedGalaxySystemOrbits(nodes, 4401, 48, 40, reducedMotion);
          const centers = () => I.communityCenters(nodes);
          const systemIds = ['s0', 's1', 's2', 's3'];
          const globalBefore = new Map(systemIds.map(id => {
            const center = centers().get(id); return [id, Math.atan2(center.y, center.x)];
          }));
          const localBefore = new Map(systemIds.map(id => {
            const star = nodes.find(node => node.id === `${id}-star`);
            const planet = nodes.find(node => node.id === `${id}-planet`);
            return [id, Math.atan2(planet.y - star.y, planet.x - star.x)];
          }));
          const seededMomentum = ['vx', 'vy'].map(axis => nodes.reduce((sum, node) =>
            sum + node.gravity_mass * node[axis], 0));
          let clearance = Infinity, maximumSpeed = 0, envelope = 0;
          for (let step = 0; step < 180; step += 1) {
            const tick = I.integrateGalaxyLeapfrog(nodes, links, [], options);
            maximumSpeed = Math.max(maximumSpeed, tick.maximumSpeed);
            envelope = tick.farFieldConfinement.envelopeRadius;
            systemIds.forEach(id => {
              const star = nodes.find(node => node.id === `${id}-star`);
              const planet = nodes.find(node => node.id === `${id}-planet`);
              clearance = Math.min(clearance, Math.hypot(planet.x - star.x, planet.y - star.y)
                - star.radius - planet.radius - options.systemAnchorExclusionPadding);
            });
          }
          return {
            global: systemIds.map(id => {
              const center = centers().get(id);
              return delta(Math.atan2(center.y, center.x), globalBefore.get(id));
            }),
            local: systemIds.map(id => {
              const star = nodes.find(node => node.id === `${id}-star`);
              const planet = nodes.find(node => node.id === `${id}-planet`);
              return delta(Math.atan2(planet.y - star.y, planet.x - star.x), localBefore.get(id));
            }),
            seededMomentum, clearance, maximumSpeed, envelope,
            bounded: nodes.slice(1).every(node => Math.hypot(node.x, node.y) + node.radius
              <= envelope + 1e-8),
            finite: nodes.every(node => [node.x, node.y, node.vx, node.vy]
              .every(Number.isFinite)),
            final: nodes.map(node => [node.x, node.y, node.vx, node.vy]),
          };
        };
        emit({ reduced: run(true), ordinary: run(false) });
        """
    )
    reduced, ordinary = report["reduced"], report["ordinary"]
    # The preference is cosmetic, so every deterministic physical result is exactly identical.
    for actual, expected in zip(reduced["final"], ordinary["final"]):
        assert actual == pytest.approx(expected)
    assert reduced["seededMomentum"] == pytest.approx([0, 0], abs=1e-10)
    assert reduced["finite"] is reduced["bounded"] is True
    assert reduced["clearance"] >= -1e-9
    assert reduced["maximumSpeed"] <= 48
    assert min(abs(value) for value in reduced["global"]) > 0.3
    assert min(abs(value) for value in reduced["local"]) > 0.45


@requires_node
def test_galaxy_is_default_and_consumes_the_complete_scene_contract() -> None:
    report = _run_engine(
        """
        const linkForce = {
          id(value) { this.idValue = value; return this; },
          distance(value) { this.distanceValue = value; return this; },
          strength(value) { this.strengthValue = value; return this; },
        };
        globalThis.d3 = {
          forceLink: () => linkForce,
          forceCollide: () => ({ iterations() { return this; } }),
        };
        const api = G.create(el, { reducedMotion: () => true });
        api.setData({
          meta: { layout_seed: 73, scene_hash: 'scene' },
          communities: [{ id: 'left' }, { id: 'right' }],
          community_bridges: [{
            id: 'bridge', source_community: 'left', target_community: 'right',
            physics_strength: 0.8,
          }],
          nodes: [
            { id: 'a', x: -20, y: 0, gravity_mass: 1, visual_radius: 3, community_id: 'left' },
            { id: 'b', x: 0, y: 0, gravity_mass: 4, visual_radius: 7, community_id: 'left' },
            { id: 'c', x: 30, y: 0, gravity_mass: 2, visual_radius: 5, community_id: 'right' },
          ],
              edges: [
            { id: 'internal', source: 'a', target: 'b', rest_length: 20, spring_strength: 0.16 },
            { id: 'cross', source: 'b', target: 'c', rest_length: 30, spring_strength: 0.2 },
            { id: 'ghost', source: 'a', target: 'c', rest_length: 10, spring_strength: 0.2, ghost: true, physics_strength: 0 },
          ],
        });
        const exported = api.exportData();
        emit({
          mode: api.state().settings.mode,
          settings: {
            repel: api.state().settings.repel,
            link: api.state().settings.link,
            gravity: api.state().settings.gravity,
          },
          sizeBy: api.state().sizeBy,
          forces: {
            charge: store.d3Forces.charge === null,
            link: store.d3Forces.link === null,
            x: store.d3Forces.x === null,
            y: store.d3Forces.y === null,
            galaxy: store.d3Forces.galaxy === null,
            center: store.d3Forces.galaxyCenter === null,
            relations: store.d3Forces.galaxyRelations === null,
            defaultCenter: store.d3Forces.center === null,
            bridges: store.d3Forces.communityBridges === null,
          },
          radii: Object.fromEntries(store.graphData.nodes.map(node => [node.id, node.radius])),
          d3Budget: [store.cooldownTime, store.cooldownTicks, store.warmupTicks],
          diagnostics: api.physicsDiagnostics(),
          exported: {
            seed: exported.meta.layout_seed,
            communities: exported.communities.length,
            bridges: exported.community_bridges.length,
          },
          positions: store.graphData.nodes.map(node => [node.x, node.y]),
        });
        """
    )
    assert report["mode"] == "galaxy"
    assert report["settings"] == {"repel": 60, "link": 8, "gravity": 48}
    assert report["sizeBy"] == "mass"
    assert report["forces"] == {
        "charge": True,
        "link": True,
        "x": True,
        "y": True,
        "galaxy": True,
        "center": True,
        "relations": True,
        "defaultCenter": True,
        "bridges": True,
    }
    def radius(mass: float) -> float:
        return 1.5 + 2.0 * mass ** (2.0 / 3.0)
    assert report["radii"]["a"] == pytest.approx(radius(1))
    assert report["radii"]["b"] == pytest.approx(radius(4))
    assert report["radii"]["c"] == pytest.approx(radius(2))
    assert report["d3Budget"] == [0, 0, 0]
    assert report["diagnostics"]["timestep"] == pytest.approx(0.032)
    assert report["diagnostics"]["velocityDecay"] == pytest.approx(0.00005)
    assert report["diagnostics"]["gravitySetting"] == 48
    assert report["diagnostics"]["blackHoleGravity"] == pytest.approx(240)
    assert report["diagnostics"]["localGravity"] == pytest.approx(120)
    assert report["diagnostics"]["linkSetting"] == 8
    assert report["diagnostics"]["relationOrbitScale"] == pytest.approx(0.25)
    assert report["diagnostics"]["orbitalSeparationSetting"] == 60
    assert report["diagnostics"]["orbitalSeparationPadding"] == pytest.approx(15)
    assert report["diagnostics"]["orbitalSeparationStrength"] == pytest.approx(1)
    assert report["diagnostics"]["crossSystemRepulsionStrength"] == pytest.approx(0.18)
    assert report["diagnostics"]["systemOrbitSeedSpeedLimit"] == pytest.approx(18)
    assert report["diagnostics"]["systemAnchorExclusionPadding"] == pytest.approx(1.5)
    assert report["diagnostics"]["systemAnchorRepulsionRange"] == pytest.approx(6)
    assert report["diagnostics"]["systemAnchorRepulsionAcceleration"] == pytest.approx(0.12)
    assert report["diagnostics"]["reducedMotion"] is True
    assert report["exported"] == {"seed": 73, "communities": 2, "bridges": 1}
    assert report["positions"] == [[-20, 0], [0, 0], [30, 0]]


@requires_node
def test_collapsed_galaxy_systems_sum_live_mass_and_use_square_root_radius() -> None:
    report = _run_engine(
        """
        const api = G.create(el, { reducedMotion: () => true });
        api.setData({
          communities: [{ id: 'left' }, { id: 'right' }],
          nodes: [
            { id: 'a', x: 0, y: 0, gravity_mass: 4, visual_radius: 5, community_id: 'left' },
            { id: 'history', x: 5, y: 0, gravity_mass: 0, visual_radius: 9, community_id: 'left', ghost: true },
            { id: 'b', x: 30, y: 0, gravity_mass: 9, visual_radius: 8, community_id: 'right' },
            { id: 'old', x: 60, y: 0, gravity_mass: 0, visual_radius: 6, community_id: 'archive', ghost: true },
          ],
          edges: [
            { source: 'a', target: 'b' },
            { source: 'a', target: 'history', ghost: true, physics_strength: 0 },
              ],
            });
            api.setScope({ showUnlinked: true, minDegree: 0 });
            api.setCollapse(true);
        emit(store.graphData.nodes.map(node => ({
          id: node.id, members: node.members, mass: node.gravity_mass,
          visualRadius: node.visual_radius, radius: node.radius, ghost: node.ghost,
        })).sort((a, b) => a.id.localeCompare(b.id)));
        """
    )
    archive, left, right = report
    def radius(mass: float) -> float:
        return 1.5 + 2.0 * mass ** (2.0 / 3.0)
    assert archive == {
        "id": "cluster-archive", "members": 1, "mass": 0,
        "visualRadius": 0, "radius": 2.5, "ghost": True,
    }
    assert {key: left[key] for key in ("id", "members", "mass", "ghost")} == {
        "id": "cluster-left", "members": 2, "mass": 4, "ghost": False,
    }
    assert left["visualRadius"] == pytest.approx(radius(4))
    assert left["radius"] == pytest.approx(radius(4))
    assert {key: right[key] for key in ("id", "members", "mass", "ghost")} == {
        "id": "cluster-right", "members": 1, "mass": 9, "ghost": False,
    }
    assert right["visualRadius"] == pytest.approx(radius(9))
    assert right["radius"] == pytest.approx(radius(9))


@requires_node
def test_oversized_galaxy_pins_deterministic_scene_positions_without_live_forces() -> None:
    report = _run_engine(
        """
        const api = G.create(el, { reducedMotion: () => false });
        const scene = () => {
          const data = chain(1000);
          data.meta = { layout_seed: 91 };
          data.nodes.forEach((node, index) => {
            node.x = index - 300; node.y = (index % 7) * 3;
          });
          return data;
        };
        api.setData(scene());
        const first = store.graphData.nodes.map(node => [node.x, node.y, node.fx, node.fy]);
        api.setData(scene());
        const nodes = store.graphData.nodes;
        const repeated = nodes.map(node => [node.x, node.y, node.fx, node.fy]);
        const diagnostics = api.physicsDiagnostics();
        emit({
          mode: api.state().settings.mode,
          total: nodes.length,
          pinned: nodes.filter(node => Number.isFinite(node.fx) && Number.isFinite(node.fy)).length,
          finite: nodes.every(node => Number.isFinite(node.x) && Number.isFinite(node.y)),
          same: nodes.every(node => node.fx === node.x && node.fy === node.y),
          deterministic: first.every((position, index) => position.every((value, axis) =>
            value === repeated[index][axis])),
          endpoints: [[nodes[0].x, nodes[0].y], [nodes.at(-1).x, nodes.at(-1).y]],
          systemAnchorExclusion: diagnostics.systemAnchorExclusion,
          cooldown: [store.cooldownTime, store.cooldownTicks, store.warmupTicks],
          forces: ['galaxy', 'galaxyCenter', 'galaxyRelations', 'communityBridges',
            'charge', 'link'].map(name => store.d3Forces[name] === null),
        });
        """
    )
    assert report["mode"] == "galaxy"
    assert report["total"] == report["pinned"] == 1001
    assert report["finite"] is report["same"] is report["deterministic"] is True
    # The selected community star may project its nearest satellite before a static paint;
    # the far endpoint is unaffected and proves positions are otherwise preserved.
    assert report["endpoints"][1] == [700, 18]
    assert report["systemAnchorExclusion"]["minimumClearance"] >= -1e-9
    assert report["cooldown"] == [0, 0, 0]
    assert report["forces"] == [True, True, True, True, True, True]


@requires_node
def test_galaxy_reheat_unfreeze_and_drag_never_reseed_orbital_velocity() -> None:
    report = _run_engine(
        """
        const api = G.create(el, { reducedMotion: () => false });
        api.setData({
          meta: { layout_seed: 42 },
          nodes: [
            { id: 'sun', x: 0, y: 0, gravity_mass: 8, visual_radius: 8, community_id: 's' },
            { id: 'planet', x: 20, y: 0, gravity_mass: 1, visual_radius: 3, community_id: 's' },
          ],
          edges: [{ source: 'sun', target: 'planet', rest_length: 20, spring_strength: 0.1 }],
        });
        const planet = store.graphData.nodes.find(node => node.id === 'planet');
        const initial = [planet.vx, planet.vy];
        api.reheat();
        const reheated = [planet.vx, planet.vy];
        api.freeze(true);
        api.freeze(false);
        const unfrozen = [planet.vx, planet.vy];
        store.onNodeDragStart(planet);
        store.onNodeDragEnd(planet);
        const dragged = [planet.vx, planet.vy];

        const full = G.create(el, { reducedMotion: () => true });
        full.setRenderMode('full');
        full.setData(chain(400));
        emit({ initial, reheated, unfrozen, dragged,
          d3Calls: {
            alpha: calls.d3AlphaTarget || 0,
            resets: invocations.resetCountdown || 0,
            reheats: invocations.d3ReheatSimulation || 0,
          },
        });
        """
    )
    assert abs(report["initial"][1]) > 0
    assert report["reheated"] == pytest.approx(report["initial"])
    assert report["unfrozen"] == pytest.approx(report["initial"])
    assert report["dragged"] == pytest.approx(report["initial"])
    assert report["d3Calls"] == {"alpha": 0, "resets": 0, "reheats": 0}


@requires_node
def test_live_galaxy_fills_only_missing_compatibility_coordinates_once() -> None:
    report = _run_engine(
        """
        const scene = {
          meta: { layout_seed: 321 },
          nodes: [
            { id: 'server', x: 120, y: -30, gravity_mass: 8, community_id: 'system' },
            { id: 'missing-a', gravity_mass: 2, community_id: 'system' },
            { id: 'missing-b', gravity_mass: 1, community_id: 'other' },
          ],
          edges: [
            { source: 'server', target: 'missing-a' },
            { source: 'missing-a', target: 'missing-b' },
          ],
        };
        const snapshot = nodes => nodes.map(node => [node.id, node.x, node.y, node.vx, node.vy]);
        const api = G.create(el, { reducedMotion: () => false });
        api.setData(scene);
        const initial = snapshot(store.graphData.nodes);
        api.reheat();
        api.freeze(true);
        api.freeze(false);
        const afterExplicitActions = snapshot(store.graphData.nodes);

        const second = G.create(el, { reducedMotion: () => false });
        second.setData(scene);
        emit({
          initial,
          afterExplicitActions,
          repeated: snapshot(store.graphData.nodes),
          allFinite: initial.every(item => item.slice(1).every(Number.isFinite)),
          d3Budget: [store.cooldownTime, store.cooldownTicks, store.warmupTicks],
          d3Wakes: {
            alpha: calls.d3AlphaTarget || 0,
            resets: invocations.resetCountdown || 0,
            reheats: invocations.d3ReheatSimulation || 0,
          },
        });
        """
    )
    assert report["allFinite"] is True
    assert report["initial"][0][1:3] == [120, -30]
    for initial, after, repeated in zip(
        report["initial"], report["afterExplicitActions"], report["repeated"]
    ):
        assert initial[0] == after[0] == repeated[0]
        assert initial[1:] == pytest.approx(after[1:])
        assert initial[1:] == pytest.approx(repeated[1:])
    assert report["d3Budget"] == [0, 0, 0]
    assert report["d3Wakes"] == {"alpha": 0, "resets": 0, "reheats": 0}


@requires_node
def test_galaxy_phase_is_isolated_from_legacy_layouts_and_restores_server_seed() -> None:
    report = _run_engine(
        """
        const scene = {
          meta: { layout_seed: 17 },
          nodes: [
            { id: 'sun', x: -40, y: 3, gravity_mass: 8, community_id: 's' },
            { id: 'planet', x: 25, y: -4, gravity_mass: 1, community_id: 's' },
          ],
          edges: [{ source: 'sun', target: 'planet' }],
        };

        const first = G.create(el, { reducedMotion: () => false });
        first.setPreset('compact');
        first.setData(scene);
        const legacyDiscardedServer = store.graphData.nodes.map(node => node.x == null);
        first.setPreset('galaxy');
        const firstGalaxy = store.graphData.nodes.map(node => [node.id, node.x, node.y]);

        const api = G.create(el, { reducedMotion: () => false });
        api.setData(scene);
        const byId = Object.fromEntries(store.graphData.nodes.map(node => [node.id, node]));
        byId.sun.x = -22; byId.sun.y = 11; byId.sun.vx = 1.25; byId.sun.vy = -0.5;
        byId.planet.x = 31; byId.planet.y = 9; byId.planet.vx = -2; byId.planet.vy = 0.75;
        api.setPreset('compact');
        store.graphData.nodes.forEach((node, index) => {
          node.x = 700 + index * 100; node.y = -900; node.vx = 40; node.vy = -40;
        });
        api.setPreset('galaxy');
        emit({
          legacyDiscardedServer,
          firstGalaxy,
          restored: store.graphData.nodes.map(node => [
            node.id, node.x, node.y, node.vx, node.vy,
          ]),
          d3Budget: [store.cooldownTime, store.cooldownTicks, store.warmupTicks],
        });
        """
    )
    assert report["legacyDiscardedServer"] == [True, True]
    assert report["firstGalaxy"] == [["sun", -40, 3], ["planet", 25, -4]]
    assert report["restored"] == [
        ["sun", -22, 11, 1.25, -0.5],
        ["planet", 31, 9, -2, 0.75],
    ]
    assert report["d3Budget"] == [0, 0, 0]


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


@requires_node
def test_unfreezing_reapplies_enabled_relation_flow_after_a_frozen_render() -> None:
    """Freeze must not leave a still-enabled relation-flow switch visually inert."""

    report = _run_engine(
        """
        const api = G.create(el, {});
        const particles = () => store.linkDirectionalParticles({ layer: 'semantic' });
        api.setSettings({ flow: true });
        api.setData(chain(2));
        const live = particles();
        api.freeze(true);
        api.setData(chain(3));
        const frozen = particles();
        api.freeze(false);
        emit({ live, frozen, resumed: particles() });
        """
    )
    assert report == {"live": 3, "frozen": 0, "resumed": 3}


@requires_node
def test_a_dashboard_sync_that_turns_freeze_off_reheats_the_renderer() -> None:
    """Classic redraws send the full settings object, so ``frozen:false`` must be actionable."""

    report = _run_engine(
        """
        const api = G.create(el, {});
        api.setPreset('compact');
        api.setData(chain(2));
        api.freeze(true);
        const before = invocations.d3ReheatSimulation || 0;
        api.setSettings({ frozen: false });
        emit({
          state: api.state().settings.frozen,
          alpha: store.d3AlphaDecay,
          reheats: (invocations.d3ReheatSimulation || 0) - before,
          cooldown: store.cooldownTime,
        });
        """
    )
    assert report == {"state": False, "alpha": 0.035, "reheats": 1, "cooldown": 2200}


@requires_node
def test_reduced_motion_keeps_auto_fit_instant_while_physics_stays_live() -> None:
    """OS visual-motion preferences suppress camera animation, not layout physics."""

    report = _run_engine(
        """
        const timers = [];
        globalThis.setTimeout = (callback, delay) => { timers.push(delay); callback(); return timers.length; };
        globalThis.clearTimeout = () => {};
        store.getGraphBbox = { x: [-10, 10], y: [-10, 10] };
        const api = G.create(el, { reducedMotion: () => true });
        api.setData(chain(2));
        emit({ timers, center: store.centerAt, zoom: store.zoom,
          cooldown: [store.cooldownTime, store.cooldownTicks, store.warmupTicks],
          reduced: api.physicsDiagnostics().reducedMotion,
        });
        """
    )
    assert report["timers"] == [0]
    assert report["center"][-1] == 0
    assert report["zoom"][-1] == 0
    assert report["cooldown"] == [0, 0, 0]
    assert report["reduced"] is True


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
    # The custom 30 Hz physical clock invalidates only when it advances; force-graph's separate
    # full-rate redraw loop remains parked even while the affordable starfield is present.
    assert report["smallAutoPause"] is True
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
def test_unlinked_entities_are_shown_by_default_and_can_be_hidden() -> None:
    """The default graph is complete, while the user can still request a linked-only view."""
    report = _run_engine(
        """
        const seen = [];
        const api = G.create(el, { onStats: stats => seen.push(stats.nodes) });
        api.setData({
          nodes: [{ id: 'a' }, { id: 'b' }, { id: 'lonely' }],
          links: [{ source: 'a', target: 'b' }],
        });
        const shown = seen[seen.length - 1];
        api.setScope({ showUnlinked: false });
        const hidden = seen[seen.length - 1];
        api.setScope({ showUnlinked: true });
        emit({ hidden, shown, restored: seen[seen.length - 1] });
        """
    )
    assert report["hidden"] == 2
    assert report["shown"] == 3
    assert report["restored"] == 3


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
  apply(fn, fit, reheat) { fn(engine); log.apply = { fit: !!fit, reheat: !!reheat }; }, communityMap: () => ({}),
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
globalThis.prefersReducedMotion = () => !!scenario.reducedMotion;
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


def _run_render(
    *, show_unlinked: bool = False, parked: bool = False, reduced_motion: bool = False
) -> dict:
    source = DASHBOARD.read_text(encoding="utf-8")
    # The harness slices real source; keep its landmarks honest.
    assert "function graphRenderEngine(" in source
    assert "/* Nav away from the graph view" in source
    scenario = json.dumps({
        "showUnlinked": show_unlinked,
        "parked": parked,
        "reducedMotion": reduced_motion,
    })
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


@requires_node
def test_classic_graph_starts_live_even_when_the_os_prefers_reduced_motion() -> None:
    """Reduced visual motion cannot suppress the explicit physics default."""

    report = _run_render(reduced_motion=True)
    assert report["apply"] == {"fit": True, "reheat": True}

    source = CLASSIC_DASHBOARD.read_text(encoding="utf-8")
    assert "window.GSET.frozen=false;" in source
    engine = source[source.index("function graphRenderEngine("):]
    engine = engine[:engine.index("/* Nav away from the graph view")]
    assert "},fit,reheat);" in engine
    assert "reheat&&!prefersReducedMotion()" not in engine


def test_classic_freeze_switch_keeps_the_status_readout_in_sync() -> None:
    source = CLASSIC_DASHBOARD.read_text(encoding="utf-8")
    start = source.index("function graphToggleFreeze(")
    handler = source[start:source.index("\nfunction graphToggleLabels", start)]
    assert "GRAPH_ENGINE.freeze(control.checked);graphSetSimulationStatus(control.checked?'Layout frozen':'Adaptive layout',false);return" in handler


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
        const beforePost = labels.slice();
        store.onRenderFramePost(ctx, 1);
        const names = labels.filter(value => value.startsWith('n'));
        emit({ beforePost, names, distinct: [...new Set(names)] });
        """
    )
    assert report["beforePost"] == [], "node labels must wait until every node body is painted"
    assert len(report["distinct"]) == 3
    assert len(report["names"]) == 6  # shadow + foreground per selected node


def test_collapsed_cluster_labels_use_the_active_theme_text_colour() -> None:
    source = ASSET.read_text(encoding="utf-8")
    cluster_label = source[source.index("if (label.cluster)"):source.index("} else {", source.index("if (label.cluster)"))]
    assert "state.themeColors.label || '#e7e9ee'" in cluster_label


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
        store.onRenderFramePost(ctx, 1);
        emit({ styles });
        """
    )
    assert "#123456" in report["styles"], "node labels ignored the active theme text colour"


@requires_node
def test_drag_release_is_kinematic_and_never_wakes_unrelated_systems() -> None:
    """Pointer placement changes one node without touching global alpha or other bodies."""
    report = _run_engine(
        """
        const linkForce = {
          id() { return this; }, distance() { return this; }, strength() { return this; },
        };
        globalThis.d3 = {
          forceLink: () => linkForce,
          forceCollide: () => ({ iterations() { return this; } }),
        };
        store.d3Forces = { center: { vendorDefault: true } };
        const api = G.create(el, { reducedMotion: () => true });
        api.setData({
          nodes: [
            { id: 'dragged', x: -20, y: 0, gravity_mass: 4, community_id: 'local' },
            { id: 'neighbour', x: 0, y: 0, gravity_mass: 2, community_id: 'local' },
            { id: 'orphan', x: 80, y: 30, gravity_mass: 7, community_id: 'remote' },
          ],
          edges: [{ source: 'dragged', target: 'neighbour', rest_length: 20, spring_strength: 0.1 }],
        });
        api.setScope({ showUnlinked: true, minDegree: 0 });
        const byId = Object.fromEntries(store.graphData.nodes.map(node => [node.id, node]));
        byId.dragged.vx = 9; byId.dragged.vy = -7;
        byId.neighbour.vx = 3; byId.neighbour.vy = 4;
        byId.orphan.vx = -5; byId.orphan.vy = 6;
        const untouched = () => ['neighbour', 'orphan'].map(id => {
          const node = byId[id];
          return [id, node.x, node.y, node.vx, node.vy, node.fx, node.fy];
        });
        const wakes = () => ({
          alphaTarget: calls.d3AlphaTarget || 0,
          alphaDecay: calls.d3AlphaDecay || 0,
          resets: invocations.resetCountdown || 0,
          reheats: invocations.d3ReheatSimulation || 0,
        });
        const before = { untouched: untouched(), wakes: wakes() };
        store.onNodeDragStart(byId.dragged);
        const duringForces = ['charge', 'galaxy', 'galaxyCenter', 'galaxyRelations',
          'communityBridges', 'link', 'x', 'y', 'radial', 'collide', 'center',
          'velocityGuard']
          .map(name => store.d3Forces[name] === null);
        byId.dragged.x = byId.dragged.fx = 35;
        byId.dragged.y = byId.dragged.fy = 12;
        const during = { untouched: untouched(), wakes: wakes() };
        store.onNodeDragEnd(byId.dragged);
        setTimeout(() => emit({
          before, during,
          after: { untouched: untouched(), wakes: wakes() },
          duringForces,
          dragged: [byId.dragged.x, byId.dragged.y, byId.dragged.vx, byId.dragged.vy,
            byId.dragged.fx, byId.dragged.fy],
          restored: {
            linkRemoved: store.d3Forces.link === null,
            galaxy: typeof store.d3Forces.galaxy,
            galaxyCenter: typeof store.d3Forces.galaxyCenter,
            relations: typeof store.d3Forces.galaxyRelations,
            bridges: typeof store.d3Forces.communityBridges,
            guard: typeof store.d3Forces.velocityGuard,
            centerRemoved: store.d3Forces.center === null,
          },
        }), 0);
        """
    )
    assert all(report["duringForces"])
    assert report["before"]["untouched"] == report["during"]["untouched"]
    assert report["before"]["untouched"] == report["after"]["untouched"]
    assert report["during"]["wakes"]["alphaTarget"] == report["before"]["wakes"]["alphaTarget"]
    assert report["after"]["wakes"] == report["during"]["wakes"]
    for key in ("alphaDecay", "resets", "reheats"):
        assert report["during"]["wakes"][key] == report["before"]["wakes"][key]
    assert report["dragged"] == [35, 12, 9, -7, None, None]
    assert report["restored"] == {
        "linkRemoved": True,
        "galaxy": "object",
        "galaxyCenter": "object",
        "relations": "object",
        "bridges": "object",
        "guard": "object",
        "centerRemoved": True,
    }


@requires_node
def test_galaxy_drag_never_touches_d3_alpha_or_countdown() -> None:
    report = _run_engine(
        """
        globalThis.d3 = {};
        const api = G.create(el, { reducedMotion: () => true });
        api.setData({
          nodes: [
            { id: 'a', x: 0, y: 0, gravity_mass: 4, community_id: 'a' },
            { id: 'b', x: 80, y: 0, gravity_mass: 2, community_id: 'b' },
          ],
          edges: [],
        });
        api.setScope({ showUnlinked: true, minDegree: 0 });
        const dragged = store.graphData.nodes[0];
        api.reheat();
        const before = {
          alpha: calls.d3AlphaTarget || 0,
          resets: invocations.resetCountdown || 0,
          reheats: invocations.d3ReheatSimulation || 0,
        };
        store.onNodeDragStart(dragged);
        store.onNodeDragEnd(dragged);
        emit({
          alphaStops: (calls.d3AlphaTarget || 0) - before.alpha,
          countdownResets: (invocations.resetCountdown || 0) - before.resets,
          reheats: (invocations.d3ReheatSimulation || 0) - before.reheats,
        });
        """
    )
    assert report == {"alphaStops": 0, "countdownResets": 0, "reheats": 0}


def test_drag_keeps_galaxy_live_without_any_d3_reheat_path() -> None:
    """Dragging fixes one moving source; it must not detach or wake global physics."""
    source = ASSET.read_text(encoding="utf-8")
    assert "function isolateDragPhysics()" not in source
    assert "function restoreDragPhysics()" not in source
    assert "if (activeDragNode) return false" not in source
    assert "fixedNodeId: activeDragNode ? activeDragNode.id : null" in source
    assert "GALAXY_DRAG_GRAVITY_CAPTURE_RADIUS" in source
    assert "GALAXY_DRAG_GRAVITY_MULTIPLIER = 2" in source
    assert "dragSource: activeDragNode" in source
    begin = source[source.index("function beginNodeDrag(node) {"):]
    begin = begin[: begin.index("    function finishNodeDrag", 1)]
    finish = source[source.index("function finishNodeDrag(node) {"):]
    finish = finish[: finish.index("    /* A drag uses", 1)]
    forbidden = ("prepareReheat(", "softReheat(", "resetCountdown(",
                 "d3AlphaTarget(", "d3AlphaDecay(", "d3ReheatSimulation(")
    assert not any(call in begin for call in forbidden)
    assert not any(call in finish for call in forbidden)
    assert "cancelGalaxyDynamics(" not in begin
    assert "setSimulationBudget(false" not in begin
    follow = source[source.index("function followDraggedNode(node) {"):]
    follow = follow[: follow.index("    function beginNodeDrag", 1)]
    assert "applyDraggedNodeGravity(" not in follow
    assert "dragFollowers = captureDragFollowers(node)" in follow
    assert "reheatLiveLayout" not in source
    assert "makeDragFollowForce" not in source


@requires_node
def test_galaxy_freeze_keeps_d3_fully_stopped_before_and_after_unfreeze() -> None:
    """Galaxy resumes its own clock; it must never reactivate D3's position integrator."""

    report = _run_engine(
        """
        const api = G.create(el, {});
        api.setData(chain(2));
        api.freeze(true);
        api.setData(chain(3));
        const frozen = {
          time: store.cooldownTime, ticks: store.cooldownTicks, warmup: store.warmupTicks,
        };
        api.freeze(false);
        emit({
          frozen,
          resumed: {
            time: store.cooldownTime, ticks: store.cooldownTicks, warmup: store.warmupTicks,
          },
        });
        """
    )
    assert report["frozen"] == {"time": 0, "ticks": 0, "warmup": 0}
    assert report["resumed"] == {"time": 0, "ticks": 0, "warmup": 0}


@requires_node
def test_freeze_is_the_physics_gate_even_with_reduced_motion() -> None:
    """The switch must never claim physics is live while an OS preference disables it."""

    report = _run_engine(
        """
        const reheats = () => invocations.d3ReheatSimulation || 0;
        const api = G.create(el, { reducedMotion: () => true });
        api.setData(chain(2));
        const started = { budget: [store.cooldownTime, store.cooldownTicks],
          diagnostics: api.physicsDiagnostics(), reheats: reheats() };
        api.freeze(true);
        const frozen = { diagnostics: api.physicsDiagnostics(), reheats: reheats() };
        api.freeze(false);
        emit({ started, frozen,
          resumed: { diagnostics: api.physicsDiagnostics(), reheats: reheats() } });
        """
    )
    assert report["started"]["budget"] == [0, 0]
    assert report["started"]["diagnostics"]["reducedMotion"] is True
    assert report["frozen"]["diagnostics"]["frozen"] is True
    assert report["resumed"]["diagnostics"]["frozen"] is False
    assert report["started"]["reheats"] == report["frozen"]["reheats"] == report["resumed"]["reheats"] == 0


@requires_node
def test_persistent_galaxy_clock_is_fixed_bounded_and_lifecycle_safe() -> None:
    report = _run_engine(
        """
        let nextFrame = 1;
        const frameQueue = new Map();
        window.requestAnimationFrame = callback => {
          const id = nextFrame++;
          frameQueue.set(id, callback);
          return id;
        };
        window.cancelAnimationFrame = id => frameQueue.delete(id);
        const flush = timestamp => {
          const batch = [...frameQueue.values()];
          frameQueue.clear();
          batch.forEach(callback => callback(timestamp));
        };
        let hidden = false, visibilityHandler = null;
        globalThis.document = {
          get hidden() { return hidden; },
          addEventListener(name, handler) {
            if (name === 'visibilitychange') visibilityHandler = handler;
          },
          removeEventListener(name, handler) {
            if (name === 'visibilitychange' && visibilityHandler === handler) visibilityHandler = null;
          },
        };

        const api = G.create(el, { reducedMotion: () => false });
        api.setData({
          nodes: [
            { id: 'heavy', x: -20, y: 0, gravity_mass: 4, community_id: 'one' },
            { id: 'light', x: 20, y: 0, gravity_mass: 1, community_id: 'one' },
          ],
          edges: [{ source: 'heavy', target: 'light' }],
        });
        const actualNodes = store.graphData.nodes;
        const expectedNodes = actualNodes.map(node => ({ ...node }));
        I.integrateGalaxyLeapfrog(expectedNodes, store.graphData.links, [], {
          gravity: 48,
          softening: 38.4,
          centralSoftening: 48,
          bridgeSoftening: 38.4,
          exactLimit: 64,
          theta: 0.85,
          localPairFraction: 0.15,
          corePairMultiplier: 0.75,
              includeBridges: false,
              includeRelations: true,
              includeRelationSprings: false,
              skipSystemAnchorRelations: true,
              skipOrbitalSystemRelations: true,
              orbitScale: 0.25,
              relationStrengthMultiplier: 2,
              relationForceCap: 1.6,
              relationAccelerationCap: 3.2,
              relationConstraintStrengthMultiplier: 2,
              relationConstraintResponseMultiplier: 1,
              relationConstraintRate: 24,
              relationConstraintMaxCorrection: 12,
              relationPadding: 15,
              includeOrbitalSeparation: true,
              orbitalSeparationPadding: 15,
              orbitalSeparationStrength: 1,
              crossCommunitySeparationPadding: 1.5,
              crossCommunitySeparationStrength: 0.18,
              orbitalSeparationMaxCorrection: 4,
              orbitalSeparationMaxVelocityCorrection: 8,
              preserveLocalTangentialVelocity: true,
              preserveSystemRadii: true,
              skipSystemAnchorPairs: true,
              systemAnchorExclusionPadding: 1.5,
              systemAnchorRepulsionRange: 6,
              systemAnchorRepulsionAcceleration: 0.12,
              includeMutualSystems: true,
              mutualSystemGravityFraction: 0.12,
              mutualSystemSoftening: 80,
              localRelativeSpeedLimit: 48,
          timestep: 0.032,
          inwardConvergence: true,
          wallClockSeconds: 1 / 30,
          velocityDecay: 0.00005,
          speedLimit: 48,
          includeCollisions: false,
          collisionPadding: 1.5,
          collisionStrength: 0.7,
          collisionIterations: 1,
        });
        flush(100);
        const first = {
          actual: actualNodes.map(node => [node.x, node.y, node.vx, node.vy]),
          expected: expectedNodes.map(node => [node.x, node.y, node.vx, node.vy]),
          diagnostics: api.physicsDiagnostics(),
          budget: [store.cooldownTime, store.cooldownTicks, store.warmupTicks],
          d3ForcesOff: ['charge', 'link', 'center', 'galaxy', 'galaxyCenter',
            'galaxyRelations', 'communityBridges', 'collide', 'velocityGuard']
            .every(name => store.d3Forces[name] === null),
        };

        api.freeze(true);
        const frozenPositions = actualNodes.map(node => [node.x, node.y, node.vx, node.vy]);
        flush(5000);
        const frozen = {
          positions: actualNodes.map(node => [node.x, node.y, node.vx, node.vy]),
          diagnostics: api.physicsDiagnostics(),
          queued: frameQueue.size,
        };
        api.freeze(false);
        flush(9000);
        const resumed = api.physicsDiagnostics();

        hidden = true;
        visibilityHandler();
        const hiddenPositions = actualNodes.map(node => [node.x, node.y, node.vx, node.vy]);
        flush(50000);
        const whileHidden = {
          positions: actualNodes.map(node => [node.x, node.y, node.vx, node.vy]),
          diagnostics: api.physicsDiagnostics(),
        };
        hidden = false;
        visibilityHandler();
        flush(100000);
        const visibleAgain = api.physicsDiagnostics();

        const dragged = actualNodes[0], unrelated = actualNodes[1];
        store.onNodeDragStart(dragged);
        const unrelatedBeforeDrag = [unrelated.x, unrelated.y, unrelated.vx, unrelated.vy];
        dragged.x = dragged.fx = 75;
        dragged.y = dragged.fy = 25;
        flush(100100);
        const duringDrag = [unrelated.x, unrelated.y, unrelated.vx, unrelated.vy];
        const stepsBeforeRelease = api.physicsDiagnostics().steps;
        store.onNodeDragEnd(dragged);
        flush(100200);
        const releaseFrame = {
          unrelated: [unrelated.x, unrelated.y, unrelated.vx, unrelated.vy],
          steps: api.physicsDiagnostics().steps,
          dragged: [dragged.x, dragged.y, dragged.vx, dragged.vy, dragged.fx, dragged.fy],
        };
        flush(100234);
        const afterDragEvolution = api.physicsDiagnostics();

        api.pause();
        const pausedSteps = api.physicsDiagnostics().steps;
        flush(200000);
        const paused = api.physicsDiagnostics();
        api.resume();
        flush(300000);
        const resumedAfterPause = api.physicsDiagnostics();
        api.destroy();
        emit({
          first,
          frozenPositions,
          frozen,
          resumed,
          hiddenPositions,
          whileHidden,
          visibleAgain,
          unrelatedBeforeDrag,
          duringDrag,
          stepsBeforeRelease,
          releaseFrame,
          afterDragEvolution,
          pausedSteps,
          paused,
          resumedAfterPause,
          queuedAfterDestroy: frameQueue.size,
          d3Wakes: {
            alpha: calls.d3AlphaTarget || 0,
            resets: invocations.resetCountdown || 0,
            reheats: invocations.d3ReheatSimulation || 0,
          },
        });
        """
    )
    for actual, expected in zip(report["first"]["actual"], report["first"]["expected"]):
        assert actual == pytest.approx(expected)
    first = report["first"]["diagnostics"]
    assert report["first"]["budget"] == [0, 0, 0]
    assert report["first"]["d3ForcesOff"] is True
    assert first["frames"] == first["steps"] == first["lastSubsteps"] == 1
    assert first["timestep"] == pytest.approx(0.032)
    assert first["velocityDecay"] == pytest.approx(0.00005)
    assert first["reducedMotion"] is False
    assert first["kineticEnergy"] > 0
    assert first["speedCapActivations"] == 0

    assert report["frozen"]["positions"] == report["frozenPositions"]
    assert report["frozen"]["diagnostics"]["frozen"] is True
    assert report["frozen"]["diagnostics"]["steps"] == 1
    assert report["frozen"]["queued"] == 0
    # Resuming after a long wall-clock gap performs one ordinary step, never three catch-up steps.
    assert report["resumed"]["steps"] == 2
    assert report["resumed"]["lastSubsteps"] == 1

    assert report["whileHidden"]["positions"] == report["hiddenPositions"]
    assert report["whileHidden"]["diagnostics"]["steps"] == 2
    assert report["whileHidden"]["diagnostics"]["hidden"] is True
    assert report["visibleAgain"]["steps"] == 3
    assert report["visibleAgain"]["lastSubsteps"] == 1

    # Dragging owns only the primary node. The custom clock keeps integrating its related
    # body around that moving mass source, without waking D3 or running catch-up substeps.
    assert report["duringDrag"] != report["unrelatedBeforeDrag"]
    assert report["releaseFrame"]["unrelated"] != report["unrelatedBeforeDrag"]
    assert 3 < report["stepsBeforeRelease"] <= 6
    assert report["stepsBeforeRelease"] < report["releaseFrame"]["steps"] \
        <= report["stepsBeforeRelease"] + 3
    assert report["afterDragEvolution"]["steps"] \
        == report["releaseFrame"]["steps"] + 1
    assert all(value is not None for value in report["releaseFrame"]["dragged"][:4])
    assert report["releaseFrame"]["dragged"][4:] == [None, None]

    assert report["paused"]["steps"] == report["pausedSteps"] \
        == report["afterDragEvolution"]["steps"]
    assert report["paused"]["running"] is False
    assert report["resumedAfterPause"]["steps"] == report["pausedSteps"] + 1
    assert report["queuedAfterDestroy"] == 0
    assert report["d3Wakes"] == {"alpha": 0, "resets": 0, "reheats": 0}


@requires_node
def test_explicit_galaxy_reheat_never_adds_bonus_physical_slices() -> None:
    report = _run_engine(
        """
        let nextFrame = 1;
        const frameQueue = new Map();
        window.requestAnimationFrame = callback => {
          const id = nextFrame++;
          frameQueue.set(id, callback);
          return id;
        };
        window.cancelAnimationFrame = id => frameQueue.delete(id);
        const flush = timestamp => {
          const batch = [...frameQueue.values()];
          frameQueue.clear();
          batch.forEach(callback => callback(timestamp));
        };
        const api = G.create(el, { reducedMotion: () => false });
        api.setData({
          nodes: [
            { id: 'black-hole', x: 0, y: 0, vx: 0, vy: 0, gravity_mass: 20,
              community_id: 'core', anchor_role: 'global' },
            { id: 'unlinked-star', x: 140, y: 0, vx: 0, vy: 2, gravity_mass: 6,
              community_id: 'outer' },
          ],
          edges: [],
        });
        flush(100);
        flush(134);
        const star = store.graphData.nodes.find(node => node.id === 'unlinked-star');
        const before = {
          phase: [star.x, star.y, star.vx, star.vy],
          diagnostics: api.physicsDiagnostics(),
        };
        api.reheat();
        const queued = api.physicsDiagnostics();
        [200, 234, 268, 302, 336].forEach(flush);
        const after = {
          phase: [star.x, star.y, star.vx, star.vy],
          diagnostics: api.physicsDiagnostics(),
        };
        api.reheat();
        const recoalesced = api.physicsDiagnostics();
        api.freeze(true);
        emit({
          before, queued, after, recoalesced,
          frozen: api.physicsDiagnostics(),
          d3: {
            alpha: calls.d3AlphaTarget || 0,
            resets: invocations.resetCountdown || 0,
            reheats: invocations.d3ReheatSimulation || 0,
          },
        });
        """
    )
    assert report["queued"]["reheatActivations"] == 1
    assert report["queued"]["reheatStepsRemaining"] == 0
    assert report["queued"]["reheatStepsApplied"] == 0
    assert report["after"]["diagnostics"]["reheatStepsApplied"] == 0
    assert report["after"]["diagnostics"]["reheatStepsRemaining"] == 0
    assert report["after"]["diagnostics"]["lastReheatSubsteps"] == 0
    assert report["after"]["diagnostics"]["steps"] \
        == report["before"]["diagnostics"]["steps"] + 5
    assert report["after"]["diagnostics"]["frames"] \
        == report["before"]["diagnostics"]["frames"] + 5
    assert report["after"]["diagnostics"]["lastSubsteps"] == 1
    assert report["after"]["phase"] != pytest.approx(report["before"]["phase"])
    assert report["recoalesced"]["reheatActivations"] == 2
    assert report["recoalesced"]["reheatStepsRemaining"] == 0
    assert report["recoalesced"]["reheatStepsApplied"] == 0
    assert report["frozen"]["reheatStepsRemaining"] == 0
    assert report["d3"] == {"alpha": 0, "resets": 0, "reheats": 0}


@requires_node
def test_manual_drag_keeps_clock_live_and_nearby_bodies_follow_fixed_source() -> None:
    """Pointer ownership never freezes the graph; one source stays fixed while neighbours move."""

    report = _run_engine(
        """
        let nextFrame = 1;
        const frameQueue = new Map();
        window.requestAnimationFrame = callback => {
          const id = nextFrame++;
          frameQueue.set(id, callback);
          return id;
        };
        window.cancelAnimationFrame = id => frameQueue.delete(id);
        const flush = timestamp => {
          const batch = [...frameQueue.values()];
          frameQueue.clear();
          batch.forEach(callback => callback(timestamp));
        };
        const windowListeners = Object.create(null);
        window.addEventListener = (name, handler) => { windowListeners[name] = handler; };
        window.removeEventListener = (name, handler) => {
          if (windowListeners[name] === handler) delete windowListeners[name];
        };
        const elementListeners = Object.create(null);
        el.addEventListener = (name, handler) => { elementListeners[name] = handler; };
        el.removeEventListener = (name, handler) => {
          if (elementListeners[name] === handler) delete elementListeners[name];
        };
        el.querySelector = selector => selector === 'canvas' ? {
          getBoundingClientRect: () => ({ left: 0, top: 0 }),
        } : null;
        store.screen2GraphCoords = (x, y) => ({ x, y });

        const api = G.create(el, { reducedMotion: () => false });
        api.setData({
          nodes: [
            { id: 'black-hole', anchor_role: 'global', x: 0, y: 0,
              gravity_mass: 8, community_id: 'core' },
            { id: 'heavy', x: -30, y: 0, gravity_mass: 4, community_id: 'one' },
            { id: 'light', x: 30, y: 0, gravity_mass: 1, community_id: 'one' },
            { id: 'moon', x: 50, y: 20, gravity_mass: 1, community_id: 'one' },
            { id: 'remote', x: 140, y: -35, gravity_mass: 1, community_id: 'two' },
          ],
          edges: [{ source: 'heavy', target: 'light' }],
        });
        api.setScope({ showUnlinked: true, minDegree: 0 });
        flush(100);
        const nodes = Object.fromEntries(store.graphData.nodes.map(node => [node.id, node]));
        const pointer = (type, x, y) => ({
          type, button: 0, isPrimary: true, pointerId: 7, clientX: x, clientY: y,
          preventDefault() {}, stopPropagation() {},
        });
        const unrelatedPhase = () => [nodes.remote.x, nodes.remote.y, nodes.remote.vx, nodes.remote.vy];
        const followerPhase = () => [nodes.light.x, nodes.light.y, nodes.light.vx, nodes.light.vy];
        const moonPhase = () => [nodes.moon.x, nodes.moon.y, nodes.moon.vx, nodes.moon.vy];
        const candidatePhase = () => [nodes.heavy.x, nodes.heavy.y, nodes.heavy.vx, nodes.heavy.vy];

        const beforeDown = {
          unrelated: unrelatedPhase(), follower: followerPhase(), moon: moonPhase(),
          candidate: candidatePhase(),
          steps: api.physicsDiagnostics().steps,
        };
        elementListeners.pointerdown(pointer('pointerdown', nodes.heavy.x, nodes.heavy.y));
        const afterDown = {
          unrelated: unrelatedPhase(), follower: followerPhase(), moon: moonPhase(),
          candidate: candidatePhase(),
          steps: api.physicsDiagnostics().steps,
        };
        // Pointer-down alone is not a drag, and it must not suspend the Galaxy clock.
        flush(5000);
        const heldBeforeMove = {
          unrelated: unrelatedPhase(), follower: followerPhase(), moon: moonPhase(),
          candidate: candidatePhase(),
          steps: api.physicsDiagnostics().steps,
        };
        windowListeners.pointermove(pointer('pointermove', nodes.heavy.x + 90, nodes.heavy.y + 45));
        const placedCandidate = candidatePhase();
        flush(6000);
        const duringDrag = {
          unrelated: unrelatedPhase(), follower: followerPhase(), moon: moonPhase(),
          candidate: candidatePhase(), followers: api.physicsDiagnostics().dragFollowers,
          steps: api.physicsDiagnostics().steps,
          dragging: api.physicsDiagnostics().dragging,
        };
        windowListeners.pointerup(pointer('pointerup', nodes.heavy.x, nodes.heavy.y));
        const releaseSteps = api.physicsDiagnostics().steps;
        flush(7000); // physics continues immediately; no restore/isolation frame exists
        const releaseFrame = { unrelated: unrelatedPhase(), steps: api.physicsDiagnostics().steps };
        flush(7034);
        const evolvedSteps = api.physicsDiagnostics().steps;

        // A click also leaves the ordinary clock live.
        const clickBefore = candidatePhase();
        const clickBeforeSteps = api.physicsDiagnostics().steps;
        elementListeners.pointerdown(pointer('pointerdown', nodes.heavy.x, nodes.heavy.y));
        flush(9000);
        const clickHeld = candidatePhase();
        const clickHeldSteps = api.physicsDiagnostics().steps;
        windowListeners.pointerup(pointer('pointerup', nodes.heavy.x, nodes.heavy.y));
        const clickReleased = candidatePhase();
        const clickReleaseSteps = api.physicsDiagnostics().steps;
        flush(9034);
        const clickEvolvedSteps = api.physicsDiagnostics().steps;

        emit({
          beforeDown, afterDown, heldBeforeMove, duringDrag,
          placedCandidate, releaseSteps, releaseFrame, evolvedSteps,
          clickBefore, clickHeld, clickReleased, clickBeforeSteps, clickHeldSteps,
          clickReleaseSteps, clickEvolvedSteps,
          d3Wakes: {
            alpha: calls.d3AlphaTarget || 0,
            resets: invocations.resetCountdown || 0,
            reheats: invocations.d3ReheatSimulation || 0,
          },
        });
        """
    )
    assert report["afterDown"] == report["beforeDown"]
    assert report["heldBeforeMove"]["steps"] > report["beforeDown"]["steps"]
    assert report["heldBeforeMove"]["unrelated"] != report["beforeDown"]["unrelated"]
    assert report["duringDrag"]["unrelated"] != report["heldBeforeMove"]["unrelated"]
    assert report["duringDrag"]["follower"] != report["beforeDown"]["follower"]
    assert report["duringDrag"]["moon"] != report["beforeDown"]["moon"]
    assert report["duringDrag"]["candidate"] == pytest.approx(report["placedCandidate"])
    assert report["duringDrag"]["steps"] > report["heldBeforeMove"]["steps"]
    assert report["duringDrag"]["dragging"] == "heavy"
    assert set(report["duringDrag"]["followers"]) == {"light", "moon", "remote"}
    assert report["releaseFrame"]["unrelated"] != report["duringDrag"]["unrelated"]
    assert report["releaseFrame"]["steps"] > report["releaseSteps"]
    assert report["evolvedSteps"] > report["releaseSteps"]
    assert report["clickHeldSteps"] > report["clickBeforeSteps"]
    assert report["clickHeld"] != pytest.approx(report["clickBefore"])
    assert report["clickReleased"] == pytest.approx(report["clickHeld"])
    assert report["clickEvolvedSteps"] > report["clickReleaseSteps"]
    assert report["d3Wakes"] == {"alpha": 0, "resets": 0, "reheats": 0}


def test_primary_graph_dependencies_are_lazy_retryable_and_csp_clean() -> None:
    """The primary Ledger must not pay for graph assets before Graph opens."""

    markup = PRIMARY_INDEX.read_text(encoding="utf-8")
    source = PRIMARY_LEDGER.read_text(encoding="utf-8")
    vendor = PRIMARY_VENDOR.read_text(encoding="utf-8")
    styles = PRIMARY_CSS.read_text(encoding="utf-8")
    for asset in ("d3.min.js", "force-graph.min.js", "engraphis-graph.js"):
        assert asset not in markup
    assert 'id="graph-repel" type="range" min="0" max="120" value="60"' in markup
    assert 'id="graph-link" type="range" min="4" max="80" value="8"' in markup
    assert 'id="graph-gravity" type="range" min="0" max="400" value="48"' in markup
    assert "{ id: 'graph-repel', key: 'repel', fallback: 60 }" in source
    assert "{ id: 'graph-link', key: 'link', fallback: 8 }" in source
    assert "{ id: 'graph-gravity', key: 'gravity', fallback: 48 }" in source

    loader = source[source.index("function ensureGraphAssets()"):
                    source.index("function safeUrl", source.index("function ensureGraphAssets()"))]
    d3 = loader.index("'/v2-assets/vendor/d3.min.js?v=20260727-final'")
    force_graph = loader.index("'/v2-assets/vendor/force-graph.min.js?v=20260727-final'")
    renderer = loader.index("'/v2-assets/engraphis-graph.js?v=20260811-galaxy-release-stable-1'")
    assert d3 < force_graph < renderer
    assert '/v2-assets/ledger.js?v=20260811-galaxy-release-stable-1' in markup
    assert "if (graphAssetsPromise === attempt) releaseGraphAssetsAttempt(attempt)" in loader
    assert "graphAssetsRetry = Math.min(graphAssetsRetry + 1, 10)" in loader
    assert not re.search(r'document\.createElement\(["\']style["\']\)', vendor)
    assert ".force-graph-container canvas {" in styles
    assert ".force-graph-container .grabbable:active {" in styles
    assert ".float-tooltip-kap {" in styles


def test_primary_graph_starts_unfrozen_so_the_force_controls_take_effect() -> None:
    """A fresh graph must settle, rather than make every tuning control look inert."""

    assert "graphFrozen: false" in PRIMARY_LEDGER.read_text(encoding="utf-8")
    assert "state.graphFrozen = false;" in PRIMARY_LEDGER.read_text(encoding="utf-8")
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
    auto-collapsed view (only ``cluster-*`` bubbles are drawn below zoom 0.42) or by a scope
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
        // Galaxy preserves the coordinates from the expanded scene instead of throwing them
        // away and waiting for a fresh simulation tick.
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
    # ...so focusing a named entity expands it. Galaxy retains its known scene coordinate and
    # can center immediately instead of waiting for a second simulation frame.
    assert report["expanding"] is True
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
        api.setScope({ showUnlinked: false, minDegree: 1 });
        emit({
          seeded, paintOnly, afterScope: calls.graphData, before, recoloured,
          repaintsBefore, repaintsAfter, shown: store.graphData.nodes.length,
        });
        """
    )
    assert report["paintOnly"] == report["seeded"], "an appearance change restarted the layout"
    assert report["afterScope"] > report["seeded"], "a real view change never reached the canvas"
    assert report["shown"] == 12
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
        api.setPreset('compact');
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
        const frozen = G.create(el, { reducedMotion: () => true });
        frozen.setData(chain(40));
        frozen.freeze(true);
        emit({
          small, big,
          frozen: { time: store.cooldownTime, ticks: store.cooldownTicks },
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
    # Freeze, not the OS visual-motion preference, is the explicit static-layout control.
    assert report["frozen"]["time"] == 0
    assert report["frozen"]["ticks"] == 0


@requires_node
def test_physics_sliders_reheat_the_simulation_the_way_the_classic_renderer_does() -> None:
    """Installing a new force on a settled graph moves nothing without a reheat.

    ``graphSet`` (dashboard.js) routes Repel/Link/Gravity/Size/Font/Link-width/Label-density
    through ``setSettings`` under ``?graph-engine=next``.  The classic branch of that same
    function treats ``repel|link|gravity|size`` as *layout* changes: it re-applies the forces
    and then reheats unless the user explicitly froze the graph.  The engine's ``applyForces()``
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
        api.setPreset('compact');
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

        const reduced = G.create(el, { reducedMotion: () => true });
        reduced.setPreset('compact');
        reduced.setData(chain(40));
        const reducedMotion = bump(reduced, { repel: 260 });
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
    assert report["reducedMotion"] == 1, "reduced motion silently disabled live physics"


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
        api.setPreset('compact');
        api.setRenderMode('full');
        // Keep this below the responsive full-graph ceiling. Larger full graphs deliberately
        // take the deterministic, centred layout so a complete workspace cannot lock the UI.
        api.setData(chain(400));
        api.setSettings({ gravity: 98 });
        const nodes = store.graphData.nodes;
        emit({
          mode: api.state().renderMode,
          x: { target: typeof axes.x.at(-1).target === 'function' ? axes.x.at(-1).target(nodes[0]) : axes.x.at(-1).target, value: axes.x.at(-1).value },
          y: { target: typeof axes.y.at(-1).target === 'function' ? axes.y.at(-1).target(nodes[0]) : axes.y.at(-1).target, value: axes.y.at(-1).value },
          reheat: invocations.d3ReheatSimulation || 0,
          cooldown: store.cooldownTime,
          pinned: nodes.filter(node => node.fx !== undefined || node.fy !== undefined).length,
        });
        """
    )
    assert report["mode"] == "full"
    assert report["x"] == {"target": 0, "value": 0.98}
    assert report["y"] == {"target": 0, "value": 0.98}
    assert report["reheat"] == 0, "soft alpha updates must not invoke the unbounded full reheat path"
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
        api.setPreset('compact');
        api.setRenderMode('full');
        // `chain` supplies N+1 nodes, so this is one past the live-force ceiling.
        api.setData(chain(600));
        const before = span(store.graphData.nodes);
        const reheatBefore = invocations.d3ReheatSimulation || 0;
        api.setSettings({ gravity: 400 });
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
def test_layout_presets_use_distinct_force_geometry() -> None:
    """Each layout button must install a visibly different arrangement strategy."""

    for dashboard in (DASHBOARD, CLASSIC_DASHBOARD):
        classic_forces = dashboard.read_text(encoding="utf-8")
        forces = classic_forces[classic_forces.index("function graphApplyForces()") : classic_forces.index("function graphSetHighlight(")]
        assert "if(mode==='communities')" in forces
        assert "else if(mode==='radial'&&d3.forceRadial)" in forces
        assert "else if(mode==='constellation')" in forces

    report = _run_engine(
        """
        const targets = { x: [], y: [], radial: [] };
        const force = target => ({ target, strengthValue: null, strength(value) {
          if (arguments.length) { this.strengthValue = value; return this; }
          return this.strengthValue;
        } });
        globalThis.d3 = {
          forceX: target => { targets.x.push(target); return force(target); },
          forceY: target => { targets.y.push(target); return force(target); },
          forceRadial: target => { targets.radial.push(target); return force(target); },
          forceCollide: () => ({ iterations: () => ({}) }),
        };
        const api = G.create(el, { reducedMotion: () => true });
        api.setData({
          nodes: [{ id: 'a' }, { id: 'b' }, { id: 'c' }, { id: 'd' }, { id: 'e' }, { id: 'f' }],
          links: [
            { source: 'a', target: 'b' }, { source: 'a', target: 'c' }, { source: 'a', target: 'd' },
            { source: 'e', target: 'f' },
          ],
        });
        const read = mode => {
          targets.x = []; targets.y = []; targets.radial = [];
          api.setPreset(mode);
          const xForce = store.d3Forces.x, radialForce = store.d3Forces.radial;
          const nodes = store.graphData.nodes;
          const point = node => typeof xForce.target === 'function' ? xForce.target(node) : xForce.target;
          return {
            xKind: typeof xForce.target,
            xStrength: xForce.strengthValue,
            first: point(nodes[0]),
            second: point(nodes[nodes.length - 1]),
            radial: radialForce ? radialForce.target(nodes[0]) : null,
            radialOuter: radialForce ? radialForce.target(nodes[nodes.length - 1]) : null,
          };
        };
        emit({
          compact: read('compact'), original: read('original'), communities: read('communities'),
          radial: read('radial'), constellation: read('constellation'),
        });
        """
    )
    assert report["compact"]["first"] == 0
    assert report["original"]["first"] == 0
    assert report["compact"]["xStrength"] > report["original"]["xStrength"]
    # Communities mode keeps a gentle origin-based centering: a function target at a
    # distant grid slot would fight an explicit drag (the e2e drag-release contract),
    # so the mode's visible grouping comes from the charge/repel geometry instead.
    assert report["communities"]["xKind"] == "number"
    assert report["communities"]["first"] == 0
    assert report["radial"]["radial"] is not None
    assert report["radial"]["radial"] < report["radial"]["radialOuter"]
    assert report["constellation"]["xKind"] == "function"
    assert report["constellation"]["first"] != 0


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
        api.setPreset('compact');

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
    assert "node.x = node.fx = point.x + manualDrag.offsetX;" in move
    assert "node.vx = 0;" not in move
    begin = source[source.index("function beginNodeDrag(node) {"):
                   source.index("function finishNodeDrag(node) {")]
    assert "node.vx = 0;" in begin
    assert "node.vy = 0;" not in move
    assert "node.vy = 0;" in begin
    assert "node.fx = undefined;" in source
    assert "node.fy = undefined;" in source
    assert "activeDragLinks" not in source
    assert "other.vx" not in move
    assert "other.vy" not in move
    teardown = source[source.index("api.destroy = () => {"):]
    assert "if (detachManualDrag) { detachManualDrag(); detachManualDrag = null; }" in teardown


def test_graph_physics_updates_are_bounded_and_coalesced() -> None:
    """Explicit slider changes coalesce while pointer placement has no wake mechanism."""
    source = ASSET.read_text(encoding="utf-8")
    vendor = VENDOR.read_text(encoding="utf-8")
    primary_vendor = PRIMARY_VENDOR.read_text(encoding="utf-8")
    assert "const MIN_NODE_SPEED = 8;" in source
    assert "const MAX_NODE_SPEED = 48;" in source
    assert "function makeVelocityGuardForce()" in source
    assert "fg.d3Force('velocityGuard', velocityGuardForce);" in source
    assert ".enableNodeDrag(false)" in source
    assert "node.fx = undefined;" in source
    assert "node.fy = undefined;" in source
    assert "function schedulePhysicsUpdate()" in source
    assert "physicsReheatPending" in source
    assert "cancelAutoFit();" in source
    assert "function prepareReheat()" in source
    assert "function supportsSoftAlpha()" in source
    assert "function softReheat()" in source
    assert "fg.d3AlphaTarget(SETTINGS_ALPHA_TARGET);" in source
    assert "fg.resetCountdown();" in source
    assert "softReheat();" in source
    assert "DRAG_ALPHA_TARGET" not in source
    assert "DRAG_SETTLE_DELAY_MS" not in source
    assert "d3AlphaTarget" in vendor and "resetCountdown" in vendor
    assert "d3AlphaTarget" in primary_vendor and "resetCountdown" in primary_vendor


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
