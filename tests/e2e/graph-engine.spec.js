const { test, expect } = require('@playwright/test');

/*
 * Real-browser coverage for the opt-in canvas graph engine (`?graph-engine=next`).
 *
 * tests/test_graph_engine_asset.py drives the asset under Node against a recording
 * force-graph stand-in, which is the right tool for the logic but proves nothing about a
 * browser: it has no CSP, no real `<canvas>`, no vendor bundle, and no `<script>` loading.
 * Three of the defects this feature shipped and then fixed were only visible there —
 * assets fetched on pages that never open the graph, a canvas that never repaints, and
 * sliders that installed a force into an already-settled simulation.  This file is the
 * browser half.
 */

const workspace = 'graph-e2e';
const stellarOrbitAssetVersion = '20260831-galaxy-floor-fix-2';

// A small connected store: two clusters joined by one bridge, so communities, the legend and
// the bridge detector all have something real to work on.
const graphPayload = {
  nodes: [
    { id: 'ada', label: 'Ada Lovelace', etype: 'person_or_concept', degree: 3, gravity_mass: 8, visual_radius: 8.5, community_id: 'history', x: -64, y: 0 },
    { id: 'engine', label: 'Analytical Engine', etype: 'artifact', degree: 3, gravity_mass: 5, visual_radius: 6.8, community_id: 'history', x: -42, y: 8 },
    { id: 'babbage', label: 'Charles Babbage', etype: 'person_or_concept', degree: 2, gravity_mass: 3, visual_radius: 5.7, community_id: 'history', x: -58, y: 24 },
    { id: 'notes', label: 'Note G', etype: 'artifact', degree: 2, gravity_mass: 2, visual_radius: 4.8, community_id: 'history', x: -32, y: -14 },
    { id: 'sqlite', label: 'SQLite', etype: 'technology', degree: 2, gravity_mass: 6, visual_radius: 7.3, community_id: 'storage', x: 56, y: 0 },
    { id: 'fts', label: 'FTS5', etype: 'technology', degree: 2, gravity_mass: 3, visual_radius: 5.7, community_id: 'storage', x: 76, y: 15 },
    { id: 'store', label: 'Store', etype: 'artifact', degree: 2, gravity_mass: 4, visual_radius: 6.3, community_id: 'storage', x: 70, y: -18 },
    // Deliberately unlinked: the default scope must hide it, which is also what proves the
    // canvas is rendering the filtered view rather than the raw response.
    { id: 'orphan', label: 'Unreferenced entity', etype: 'person_or_concept', degree: 0, gravity_mass: 1, visual_radius: 2.5, community_id: 'orphan', x: 0, y: 90 },
  ],
  edges: [
    { from: 'ada', to: 'engine', label: 'worked on', layer: 'semantic' },
    { from: 'ada', to: 'babbage', label: 'collaborated with', layer: 'semantic' },
    { from: 'ada', to: 'notes', label: 'wrote', layer: 'semantic' },
    { from: 'babbage', to: 'engine', label: 'designed', layer: 'semantic' },
    { from: 'engine', to: 'notes', label: 'described by', layer: 'temporal' },
    { from: 'sqlite', to: 'fts', label: 'provides', layer: 'semantic' },
    { from: 'sqlite', to: 'store', label: 'backs', layer: 'semantic' },
    { from: 'fts', to: 'store', label: 'indexes', layer: 'semantic' },
    // The one edge across the two clusters.  `influences` is the label the community pass
    // deliberately refuses to merge on, so this must not collapse them into one.
    { from: 'notes', to: 'store', label: 'influences', layer: 'causal' },
  ],
};

const graphScenePayload = {
  ...graphPayload,
  edges: graphPayload.edges.map((edge, index) => ({
    ...edge,
    id: `scene-edge-${index}`,
    source: edge.from,
    target: edge.to,
    relation: edge.label,
    strength: 0.55,
    rest_length: 24,
    spring_strength: 0.08,
  })),
  communities: [
    { id: 'history', mass: 18, member_count: 4 },
    { id: 'storage', mass: 13, member_count: 3 },
    { id: 'orphan', mass: 1, member_count: 1 },
  ],
  community_bridges: [{
    id: 'history-storage', source_community: 'history', target_community: 'storage',
    physics_strength: 0.7,
  }],
  meta: { algorithm_version: 'galaxy-v6', layout_seed: 42, total_nodes: 8, truncated: false },
};

// A deterministic black-hole hierarchy. The dominant evidence node is the server-placed global
// anchor at chart origin; three asymmetric two-body systems exercise differential galactic
// rotation while retaining a measurable local orbit. No relation crosses communities, so any
// unrelated movement during a drag would be an integrator wake/reset rather than bridge physics.
const blackHoleGalaxyScene = {
  nodes: [
    { id: 'black-hole', label: 'Evidence core', gravity_mass: 64, visual_radius: 8,
      community_id: 'core', anchor_role: 'global', system_anchor_id: 'black-hole',
      orbit_tier: 0, galactic_radius: 0, galactic_target_radius: 0,
      galactic_radius_scale: 0.4, galactic_initial_compactness: 0.8, galactic_phase: 0, x: 0, y: 0 },
    { id: 'core-star', label: 'Core star', gravity_mass: 6, visual_radius: 8,
      community_id: 'core', anchor_role: 'none', system_anchor_id: 'black-hole',
      orbit_tier: 1, orbit_radius: 48, galactic_radius: 0, galactic_target_radius: 0,
      galactic_radius_scale: 0.4, galactic_initial_compactness: 0.8, galactic_phase: 0, x: 48, y: 0 },
    { id: 'aurora-star', label: 'Aurora star', gravity_mass: 12, visual_radius: 8,
      community_id: 'aurora', anchor_role: 'community', system_anchor_id: 'aurora-star',
      orbit_tier: 0, galactic_radius: 72.25786, galactic_target_radius: 72.25786,
      galactic_radius_scale: 0.4, galactic_initial_compactness: 0.8, galactic_phase: 0, x: 70.4, y: 0 },
    { id: 'aurora-planet', label: 'Aurora planet', gravity_mass: 2, visual_radius: 8,
      community_id: 'aurora', anchor_role: 'none', system_anchor_id: 'aurora-star',
      orbit_tier: 1, orbit_radius: 19.2, galactic_radius: 72.25786,
      galactic_target_radius: 72.25786, galactic_radius_scale: 0.4, galactic_initial_compactness: 0.8,
      galactic_phase: 0, x: 83.2, y: 14.4 },
    { id: 'borealis-star', label: 'Borealis star', gravity_mass: 9, visual_radius: 8,
      community_id: 'borealis', anchor_role: 'community', system_anchor_id: 'borealis-star',
      orbit_tier: 0, galactic_radius: 116.95649, galactic_target_radius: 116.95649,
      galactic_radius_scale: 0.4, galactic_initial_compactness: 0.8, galactic_phase: 1.71, x: -16, y: 113.6 },
    { id: 'borealis-planet', label: 'Borealis planet', gravity_mass: 2, visual_radius: 8,
      community_id: 'borealis', anchor_role: 'none', system_anchor_id: 'borealis-star',
      orbit_tier: 1, orbit_radius: 20.8, galactic_radius: 116.95649,
      galactic_target_radius: 116.95649, galactic_radius_scale: 0.4, galactic_initial_compactness: 0.8,
      galactic_phase: 1.71, x: -34.4, y: 123.2 },
    { id: 'cygnus-star', label: 'Cygnus star', gravity_mass: 7, visual_radius: 8,
      community_id: 'cygnus', anchor_role: 'community', system_anchor_id: 'cygnus-star',
      orbit_tier: 0, galactic_radius: 166.912702, galactic_target_radius: 166.912702,
      galactic_radius_scale: 0.4, galactic_initial_compactness: 0.8, galactic_phase: -2.84, x: -158.4, y: -49.6 },
    { id: 'cygnus-planet', label: 'Cygnus planet', gravity_mass: 1, visual_radius: 8,
      community_id: 'cygnus', anchor_role: 'none', system_anchor_id: 'cygnus-star',
      orbit_tier: 1, orbit_radius: 23.2, galactic_radius: 166.912702,
      galactic_target_radius: 166.912702, galactic_radius_scale: 0.4, galactic_initial_compactness: 0.8,
      galactic_phase: -2.84, x: -172, y: -30.4 },
  ],
  edges: [
    { id: 'core-orbit', source: 'black-hole', target: 'core-star', relation: 'orbits', rest_length: 48, spring_strength: 0.08 },
    { id: 'aurora-orbit', source: 'aurora-star', target: 'aurora-planet', relation: 'orbits', rest_length: 19.2, spring_strength: 0.08 },
    { id: 'borealis-orbit', source: 'borealis-star', target: 'borealis-planet', relation: 'orbits', rest_length: 20.8, spring_strength: 0.08 },
    { id: 'cygnus-orbit', source: 'cygnus-star', target: 'cygnus-planet', relation: 'orbits', rest_length: 23.2, spring_strength: 0.08 },
  ],
  communities: [
    { id: 'core', mass: 70, member_count: 2, anchor_id: 'black-hole',
      galactic_radius: 0, galactic_target_radius: 0, galactic_radius_scale: 0.4, galactic_initial_compactness: 0.8 },
    { id: 'aurora', mass: 14, member_count: 2, anchor_id: 'aurora-star',
      galactic_radius: 72.25786, galactic_target_radius: 72.25786,
      galactic_radius_scale: 0.4, galactic_initial_compactness: 0.8 },
    { id: 'borealis', mass: 11, member_count: 2, anchor_id: 'borealis-star',
      galactic_radius: 116.95649, galactic_target_radius: 116.95649,
      galactic_radius_scale: 0.4, galactic_initial_compactness: 0.8 },
    { id: 'cygnus', mass: 8, member_count: 2, anchor_id: 'cygnus-star',
      galactic_radius: 166.912702, galactic_target_radius: 166.912702,
      galactic_radius_scale: 0.4, galactic_initial_compactness: 0.8 },
  ],
  community_bridges: [],
  meta: { algorithm_version: 'galaxy-v6', layout_seed: 91, total_nodes: 8, truncated: false },
};

/* Match the production-sized browser complaint without checking in a 542-row fixture. Sixty
   explicit star systems with seven planets and one nested moon each, plus the black hole and
   one core satellite, exercise both local hierarchy levels at the live/material boundary. */
function largeServedGalaxyScene() {
  const nodes = [{
    id: 'black-hole', label: 'Evidence core', gravity_mass: 64, visual_radius: 8,
    community_id: 'core', anchor_role: 'global', system_anchor_id: 'black-hole',
    orbit_tier: 0, galactic_radius: 0, galactic_target_radius: 0,
    galactic_radius_scale: 0.4, galactic_initial_compactness: 0.8,
    galactic_phase: 0, x: 0, y: 0,
  }, {
    id: 'core-star', label: 'Core star', gravity_mass: 6, visual_radius: 5,
    community_id: 'core', anchor_role: 'none', system_anchor_id: 'black-hole',
    orbit_tier: 1, orbit_radius: 52, galactic_radius: 0, galactic_target_radius: 0,
    galactic_radius_scale: 0.4, galactic_initial_compactness: 0.8,
    galactic_phase: 0, x: 52, y: 0,
  }];
  const edges = [{ id: 'core-orbit', source: 'black-hole', target: 'core-star',
    relation: 'orbits', rest_length: 52, spring_strength: 0.08 }];
  const communities = [{ id: 'core', mass: 70, member_count: 2,
    anchor_id: 'black-hole', galactic_radius: 0, galactic_target_radius: 0 }];
  for (let system = 0; system < 60; system += 1) {
    const id = system === 0 ? 'aurora' : `system-${system}`;
    const starId = `${id}-star`;
    const phase = 0.31 + system * 2.399963229728653;
    const galacticRadius = 112 + system * 3.15;
    const centerX = Math.cos(phase) * galacticRadius;
    const centerY = Math.sin(phase) * galacticRadius * 0.84;
    let mass = 0;
    let moonParent = null;
    for (let member = 0; member < 9; member += 1) {
      const localRadius = member === 0 ? 0
        : (member === 8 ? 16 : (member === 1 ? 40 : 18 + member * 5));
      const localPhase = phase + member * 2.399963229728653;
      const nodeId = member === 0 ? starId
        : (member === 1 ? `${id}-planet`
          : (member === 8 ? `${id}-moon` : `${id}-planet-${member}`));
      const parentId = member === 8 ? moonParent.id : starId;
      const parentX = member === 8 ? moonParent.x : centerX;
      const parentY = member === 8 ? moonParent.y : centerY;
      const gravityMass = member === 0 ? 8 + system % 5 : 1 + (member % 3) * 0.25;
      mass += gravityMass;
      const node = {
        id: nodeId, label: nodeId, gravity_mass: gravityMass,
        visual_radius: member === 0 ? 5.5 : 2.5,
        community_id: id, anchor_role: member === 0 ? 'community' : 'none',
        system_anchor_id: parentId, orbit_tier: member === 8 ? 2 : member,
        orbit_radius: localRadius, galactic_radius: galacticRadius,
        galactic_target_radius: galacticRadius, galactic_radius_scale: 0.4,
        galactic_initial_compactness: 0.8, galactic_phase: phase,
        x: parentX + Math.cos(localPhase) * localRadius,
        y: parentY + Math.sin(localPhase) * localRadius,
      };
      nodes.push(node);
      if (member === 7) moonParent = node;
      if (member > 0) edges.push({
        id: `${starId}-orbit-${member}`, source: parentId, target: nodeId,
        relation: 'orbits', rest_length: localRadius, spring_strength: 0.08,
      });
    }
    communities.push({ id, mass, member_count: 9, anchor_id: starId,
      galactic_radius: galacticRadius, galactic_target_radius: galacticRadius,
      galactic_radius_scale: 0.4, galactic_initial_compactness: 0.8 });
  }
  return {
    nodes, edges, communities, community_bridges: [],
    meta: { algorithm_version: 'galaxy-v6', layout_seed: 3031,
      total_nodes: nodes.length, truncated: false },
  };
}

const servedLargeGalaxyScene = largeServedGalaxyScene();

/* The black-hole community is not exempt from the hierarchy: several directly connected
   satellites exercise the same local-orbit contract as an ordinary star system.  Keep this
   as a clone so older exact-542 smoke fixtures remain useful compatibility sentinels. */
function servedLargeGalaxySceneWithCoreSatellites() {
  const scene = JSON.parse(JSON.stringify(servedLargeGalaxyScene));
  const satellites = [
    // Deliberately cross the community boundary and share a near-identical orbital band with
    // the authored core satellite.  `system_anchor_id`, rather than community membership,
    // is the hierarchy authority for a black-hole child.
    { id: 'core-star-inner', radius: 51, phase: 2.05, community: 'cross-core' },
    { id: 'core-star-outer', radius: 74, phase: -1.34, community: 'core' },
  ];
  for (const satellite of satellites) {
    scene.nodes.push({
      id: satellite.id, label: satellite.id, gravity_mass: 3.5, visual_radius: 4,
      community_id: satellite.community, anchor_role: 'none', system_anchor_id: 'black-hole', orbit_tier: 1,
      orbit_radius: satellite.radius, galactic_radius: 0, galactic_target_radius: 0,
      galactic_radius_scale: 0.4, galactic_initial_compactness: 0.8, galactic_phase: 0,
      x: Math.cos(satellite.phase) * satellite.radius,
      y: Math.sin(satellite.phase) * satellite.radius,
    });
    scene.edges.push({ id: `${satellite.id}-orbit`, source: 'black-hole', target: satellite.id,
      relation: 'orbits', rest_length: satellite.radius, spring_strength: 0.08 });
  }
  const core = scene.communities.find(community => community.id === 'core');
  core.mass += satellites.length * 3.5;
  core.member_count += satellites.length;
  // A real scene carries metadata for every community even when its member is explicitly
  // parented to the black hole instead of to that community's star.
  scene.communities.push({ id: 'cross-core', mass: 3.5, member_count: 1,
    anchor_id: 'black-hole', galactic_radius: 0, galactic_target_radius: 0,
    galactic_radius_scale: 0.4, galactic_initial_compactness: 0.8 });
  scene.meta.total_nodes = scene.nodes.length;
  return scene;
}

const servedLargeGalaxyWithCoreSatellites = servedLargeGalaxySceneWithCoreSatellites();

/* Complete view is deliberately much larger than the 1,000-node live-force limit. It must
   take the lightweight hierarchical Galaxy path rather than silently pinning a painted field.
   The exact production shape (3,336 bodies / 26,939 links) catches an easy but invalid fix:
   merely increasing the expensive all-pairs force limit. */
function completeGalaxyScene() {
  const nodes = [{
    id: 'black-hole', label: 'Evidence core', gravity_mass: 64, visual_radius: 8,
    community_id: 'core', anchor_role: 'global', system_anchor_id: 'black-hole', orbit_tier: 0,
    x: 0, y: 0, galactic_radius: 0, galactic_target_radius: 0,
  }];
  const edges = [], communities = [{ id: 'core', mass: 64, member_count: 1,
    anchor_id: 'black-hole', galactic_radius: 0, galactic_target_radius: 0 }];
  for (let system = 0; system < 370; system += 1) {
    const id = `complete-${system}`, starId = `${id}-star`;
    const phase = system * 2.399963229728653, galacticRadius = 104 + system * .45;
    const cx = Math.cos(phase) * galacticRadius, cy = Math.sin(phase) * galacticRadius * .84;
    let mass = 0;
    for (let member = 0; member < 9; member += 1) {
      const orbitRadius = member === 0 ? 0 : 16 + member * 3;
      const nodeId = member === 0 ? starId : `${id}-planet-${member}`;
      const gravityMass = member === 0 ? 8 : 1;
      mass += gravityMass;
      nodes.push({ id: nodeId, label: nodeId, gravity_mass: gravityMass,
        visual_radius: member === 0 ? 5 : 2.5, community_id: id,
        anchor_role: member === 0 ? 'community' : 'none', system_anchor_id: starId,
        orbit_tier: member, orbit_radius: orbitRadius, galactic_radius: galacticRadius,
        galactic_target_radius: galacticRadius, galactic_phase: phase,
        x: cx + Math.cos(phase + member * 2.1) * orbitRadius,
        y: cy + Math.sin(phase + member * 2.1) * orbitRadius });
      if (member) edges.push({ id: `${id}-orbit-${member}`, source: starId, target: nodeId,
        relation: 'orbits', rest_length: orbitRadius, spring_strength: .08 });
    }
    communities.push({ id, mass, member_count: 9, anchor_id: starId,
      galactic_radius: galacticRadius, galactic_target_radius: galacticRadius });
  }
  // Five late-looking singleton systems complete the exact 3,336-node production shape.
  for (let index = 0; index < 5; index += 1) {
    const id = `complete-singleton-${index}`, phase = .44 + index * 1.18, radius = 284 + index * 6;
    nodes.push({ id, label: id, gravity_mass: 7, visual_radius: 5, community_id: id,
      anchor_role: 'community', system_anchor_id: id, orbit_tier: 0,
      galactic_radius: radius, galactic_target_radius: radius, galactic_phase: phase,
      x: Math.cos(phase) * radius, y: Math.sin(phase) * radius });
    edges.push({ id: `${id}-core`, source: 'black-hole', target: id,
      relation: 'member', rest_length: radius, spring_strength: .08 });
    communities.push({ id, mass: 7, member_count: 1, anchor_id: id,
      galactic_radius: radius, galactic_target_radius: radius });
  }
  while (edges.length < 26939) {
    const system = edges.length % 370;
    edges.push({ id: `complete-density-${edges.length}`, source: 'black-hole',
      target: `complete-${system}-star`, relation: 'aggregate', rest_length: 120,
      spring_strength: 0.01 });
  }
  return { nodes, edges, communities,
    community_bridges: [{ id: 'complete-bridge', source_community: 'core',
      target_community: 'complete-0', physics_strength: 0 }],
    meta: { algorithm_version: 'galaxy-v6', layout_seed: 9017,
      total_nodes: nodes.length, truncated: false } };
}

const servedCompleteGalaxyScene = completeGalaxyScene();

/**
 * Stub the dashboard's API surface and start recording everything a browser can tell us that
 * a Node harness cannot: which scripts were fetched, which CSP rules fired, and what the page
 * logged.  Returns the recorders so each test can assert on them.
 */
async function openDashboard(page, { query = '', graphScene = graphScenePayload } = {}) {
  const requested = [];
  const consoleErrors = [];
  const pageErrors = [];
  const cspViolations = [];

  // Report CSP violations from the page itself.  A blocked inline style is not a request
  // failure and not a console error Playwright surfaces reliably, so the only trustworthy
  // source is the document event the browser fires.
  await page.addInitScript(() => {
    window.__cspViolations = [];
    document.addEventListener('securitypolicyviolation', event => {
      window.__cspViolations.push({
        directive: event.effectiveDirective || event.violatedDirective,
        blocked: String(event.blockedURI || ''),
        sample: String(event.sample || ''),
      });
    });

    /* The engine keeps its force-graph instance in a closure and exposes no accessor, and
       node coordinates are written by d3 onto the objects that instance holds.  Rather than
       add API surface for a test, intercept the vendor global the lazy loader is about to
       define and keep a reference to the instance the product builds.  The property starts
       out reporting `undefined`, which is what `typeof ForceGraph==='undefined'` in
       graphRender() needs in order to still take the lazy-load branch. */
    let real = null;
    Object.defineProperty(window, 'ForceGraph', {
      configurable: true,
      get() {
        if (!real) return undefined;
        return (...args) => {
          const attach = real(...args);
          return (...rest) => (window.__fg = attach(...rest));
        };
      },
      set(value) { real = value; },
    });

    // Ledger constructs the canonical engine directly, so capture that instance too. The
    // product API remains unchanged; this only gives browser tests the same node/velocity
    // visibility that the classic adapter gets through ForceGraph.
    let engine = null;
    let engineProxy = null;
    Object.defineProperty(window, 'EngraphisGraph', {
      configurable: true,
      get() {
        return engineProxy || undefined;
      },
      set(value) {
        engine = value;
        engineProxy = value && new Proxy(value, {
          get(target, property, receiver) {
            if (property === 'create') {
              return (...args) => {
                if (args[1] && typeof args[1].onNodeClick === 'function') {
                  const originalNodeClick = args[1].onNodeClick;
                  args[1] = { ...args[1], onNodeClick: node => {
                    window.__lastGraphNodeClick = node && node.id;
                    return originalNodeClick(node);
                  } };
                }
                const instance = Reflect.apply(target.create, target, args);
                window.__engraphisGraph = instance;
                return instance;
              };
            }
            return Reflect.get(target, property, receiver);
          },
        });
      },
    });
  });

  page.on('request', request => requested.push(request.url()));
  page.on('console', message => {
    if (message.type() === 'error') {
      const text = message.text();
      // force-graph applies inline styles at runtime, producing known CSP blocks
      // against style-src-elem.  Chromium's console message text reports the
      // directive but not the originating script URL; that lives in
      // message.location().url.  Filter only when the location names the
      // force-graph vendor bundle; application-level CSP regressions surface
      // through a different location and must still fail the assertion.
      if (/style-src(-elem)?/.test(text)) {
        const loc = message.location();
        if (loc && typeof loc.url === 'string' && loc.url.includes('force-graph')) return;
      }
      consoleErrors.push(text);
    }
  });
  page.on('pageerror', error => pageErrors.push(String(error)));

  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname.replace(/^\/api/, '');
    const json = body => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });

    if (path === '/bootstrap') {
      return json({
        license: { plan: 'local', features: [], known_features: {}, cloud_managed: false, trial: { used: false, trial_days: 3 } },
        workspaces: [{ name: workspace, memories: 12 }],
        embedder: { semantic: true },
      });
    }
    if (path === '/graph') return json(graphPayload);
    if (path === '/graph/scene') return json(graphScene);
    if (path === '/health') return json({ status: 'ok' });
    if (path === '/stats') return json({ memories: 12, total_rows: 12, workspaces: 1, sessions: 1, by_type: {} });
    if (path === '/workspaces') return json({ workspaces: [{ name: workspace, memories: 12 }] });
    // Everything else the dashboard polls on boot: an empty, successful answer keeps the
    // console clean so a real error is not lost in expected noise.
    return json({});
  });

  await page.goto(`/classic${query}`);
  await page.waitForFunction(() => typeof window.selectView === 'function');
  const violations = () => page.evaluate(() => window.__cspViolations.slice());
  return { requested, consoleErrors, pageErrors, cspViolations, violations };
}

const fetched = (requested, name) => requested.filter(url => url.includes(name));

/** Open the Graph view and wait for force-graph to put a sized canvas on the page. */
async function openGraphView(page) {
  await page.locator('.nav-item[data-view="graph"]').click();
  const canvas = page.locator('#graph-net canvas, #graph-canvas canvas').first();
  await expect(canvas).toBeAttached({ timeout: 20_000 });
  await page.waitForFunction(() => {
    const c = document.querySelector('#graph-net canvas, #graph-canvas canvas');
    return c && c.width > 0 && c.height > 0;
  }, null, { timeout: 20_000 });
  return canvas;
}

/* Measure the hierarchy in graph space, where zoom-to-fit cannot fake orbital motion. System
   centres are evidence-mass weighted, matching the runtime force and server scene contract. */
async function galaxySystemSnapshot(page) {
  return page.evaluate(() => {
    const graph = window.__fg;
    const nodes = graph && typeof graph.graphData === 'function'
      ? graph.graphData().nodes.filter(node => !node.ghost)
      : [];
    const anchor = nodes.slice().sort((left, right) => {
      const leftGlobal = left.anchor_role === 'global' ? 1 : 0;
      const rightGlobal = right.anchor_role === 'global' ? 1 : 0;
      return rightGlobal - leftGlobal
        || Number(right.gravity_mass || 0) - Number(left.gravity_mass || 0)
        || String(left.id).localeCompare(String(right.id));
    })[0] || { id: null, x: 0, y: 0, gravity_mass: 1, radius: 1 };
    const groups = new Map();
    nodes.forEach(node => {
      const id = String(node.community_id ?? node.community ?? 'ungrouped');
      if (!groups.has(id)) groups.set(id, []);
      groups.get(id).push(node);
    });
    const anchorCommunity = String(anchor.community_id ?? anchor.community ?? 'ungrouped');
    const systems = [...groups.entries()].filter(([id]) => id !== anchorCommunity)
      .map(([id, members]) => {
        const mass = members.reduce((sum, node) => sum + Math.max(0.01,
          Number(node.gravity_mass) || 1), 0);
        const x = members.reduce((sum, node) => sum + node.x * Math.max(0.01,
          Number(node.gravity_mass) || 1), 0) / mass;
        const y = members.reduce((sum, node) => sum + node.y * Math.max(0.01,
          Number(node.gravity_mass) || 1), 0) / mass;
        const vx = members.reduce((sum, node) => sum + (Number(node.vx) || 0) * Math.max(0.01,
          Number(node.gravity_mass) || 1), 0) / mass;
        const vy = members.reduce((sum, node) => sum + (Number(node.vy) || 0) * Math.max(0.01,
          Number(node.gravity_mass) || 1), 0) / mass;
        let internalDiameter = 0;
        for (let left = 0; left < members.length; left += 1) {
          for (let right = left + 1; right < members.length; right += 1) {
            internalDiameter = Math.max(internalDiameter, Math.hypot(
              members[left].x - members[right].x, members[left].y - members[right].y,
            ));
          }
        }
        const dx = x - anchor.x, dy = y - anchor.y;
        const radius = Math.hypot(dx, dy);
        return {
          id, mass, x, y, vx, vy, radius,
          angle: Math.atan2(y - anchor.y, x - anchor.x),
          angularVelocity: radius > 1e-9
            ? (dx * (vy - (Number(anchor.vy) || 0))
              - dy * (vx - (Number(anchor.vx) || 0))) / (radius * radius) : 0,
          internalDiameter,
          members: members.map(node => node.id).sort(),
        };
      }).sort((left, right) => left.id.localeCompare(right.id));
    return {
      anchor: {
        id: anchor.id, x: anchor.x, y: anchor.y,
        vx: Number(anchor.vx) || 0, vy: Number(anchor.vy) || 0,
        mass: Number(anchor.gravity_mass), radius: Number(anchor.radius),
      },
      systems,
      nodes: Object.fromEntries(nodes.map(node => [node.id, {
        x: node.x, y: node.y, vx: Number(node.vx) || 0, vy: Number(node.vy) || 0,
        gravityMass: Number(node.gravity_mass),
        communityId: String(node.community_id ?? node.community ?? 'ungrouped'),
        anchorRole: node.anchor_role || 'none',
        systemAnchorId: node.system_anchor_id || null,
        radius: Number(node.radius),
      }])),
      diagnostics: window.__engraphisGraph
        && typeof window.__engraphisGraph.physicsDiagnostics === 'function'
        ? window.__engraphisGraph.physicsDiagnostics()
        : null,
      d3Budget: {
        time: graph && typeof graph.cooldownTime === 'function' ? graph.cooldownTime() : null,
        ticks: graph && typeof graph.cooldownTicks === 'function' ? graph.cooldownTicks() : null,
      },
      finite: nodes.every(node => [node.x, node.y, node.vx, node.vy]
        .every(value => Number.isFinite(value))),
    };
  });
}

/* Envelope clearance is a paint-space requirement: node centres can be distinct while complete
   star+planet circles visibly overlap.  Measure each independent system at the actual canvas
   transform, including node radii, so zoom-to-fit cannot hide a stacked galaxy. */
async function renderedSystemEnvelopeSnapshot(page) {
  return page.evaluate(() => {
    const graph = window.__fg;
    const nodes = graph.graphData().nodes.filter(node => !node.ghost);
    const canvas = document.querySelector('#graph-canvas canvas, #graph-net canvas');
    const bounds = canvas && canvas.getBoundingClientRect();
    const membersForStar = star => {
      const members = [], pending = [String(star.id)], seen = new Set([String(star.id)]);
      while (pending.length) {
        const parentId = pending.shift();
        nodes.filter(node => String(node.system_anchor_id || '') === parentId)
          .forEach(node => {
            const id = String(node.id);
            if (seen.has(id)) return;
            seen.add(id);
            members.push(node);
            pending.push(id);
          });
      }
      return [star, ...members];
    };
    const systems = nodes.filter(node => node.anchor_role === 'community').map(star => {
      const members = membersForStar(star);
      const point = graph.graph2ScreenCoords(star.x, star.y);
      const radius = Math.max(...members.map(node => {
        const member = graph.graph2ScreenCoords(node.x, node.y);
        const edge = graph.graph2ScreenCoords(node.x + Number(node.radius || 0), node.y);
        return Math.hypot(member.x - point.x, member.y - point.y)
          + Math.hypot(edge.x - member.x, edge.y - member.y);
      }));
      const unit = graph.graph2ScreenCoords(star.x + 1, star.y);
      const visible = !bounds || (point.x - radius >= 0 && point.y - radius >= 0
        && point.x + radius <= bounds.width && point.y + radius <= bounds.height);
      return { id: String(star.id), x: point.x, y: point.y, radius, visible,
        pixelsPerGraphUnit: Math.hypot(unit.x - point.x, unit.y - point.y), members: members.length };
    });
    let minimumClearance = Infinity, overlaps = 0, worstPair = null;
    for (let left = 0; left < systems.length; left += 1) for (let right = left + 1;
      right < systems.length; right += 1) {
      const a = systems[left], b = systems[right];
      // The runtime gap is eight graph units, converted using the smaller local screen scale.
      const clearance = Math.hypot(a.x - b.x, a.y - b.y) - a.radius - b.radius;
      const required = 8 * Math.min(a.pixelsPerGraphUnit, b.pixelsPerGraphUnit);
      const margin = clearance - required;
      if (margin < minimumClearance) {
        minimumClearance = margin;
        worstPair = { ids: [a.id, b.id], clearance, required, margin,
          radii: [a.radius, b.radius] };
      }
      if (clearance < required - .75) overlaps += 1;
    }
    return { systems, minimumClearance, overlaps, worstPair,
      finite: systems.every(system => [system.x, system.y, system.radius,
        system.pixelsPerGraphUnit].every(Number.isFinite)) };
  });
}

/* Read the exact graph-space and screen-space phase that force-graph is painting. This is
   intentionally downstream of the engine diagnostics: a healthy internal clock is not enough
   if the adapter, renderer, camera, or served asset leaves the visible planet stationary. */
async function renderedStellarSnapshot(page, systemId = 'aurora') {
  return page.evaluate(id => {
    const graph = window.__fg;
    const engine = window.__engraphisGraph;
    const nodes = graph && typeof graph.graphData === 'function'
      ? graph.graphData().nodes.filter(node => !node.ghost)
      : [];
    const zeroPoint = { x: 0, y: 0 };
    const zeroVector = { x: 0, y: 0, vx: 0, vy: 0 };
    const byId = new Map(nodes.map(node => [String(node.id), node]));
    const star = byId.get(`${id}-star`) || null;
    const planet = byId.get(`${id}-planet`) || null;
    const globalAnchors = nodes.filter(node => node.anchor_role === 'global');
    const anchor = globalAnchors.length === 1 ? globalAnchors[0] : null;
    const anchorValid = Boolean(anchor && anchor.anchor_role === 'global');
    const members = nodes.filter(node => String(node.community_id ?? node.community ?? 'ungrouped') === id);
    const memberWeight = node => Math.max(0.01, Number(node.gravity_mass) || 1);
    const mass = members.reduce((sum, node) => sum + memberWeight(node), 0);
    const weighted = (selector, fallback = 0) => (mass > 0
      ? members.reduce((sum, node) => sum + selector(node) * memberWeight(node), 0) / mass
      : fallback);
    const center = {
      x: weighted(node => Number(node.x) || 0),
      y: weighted(node => Number(node.y) || 0),
      vx: weighted(node => Number(node.vx) || 0),
      vy: weighted(node => Number(node.vy) || 0),
    };
    const toScreen = point => {
      if (!graph || typeof graph.graph2ScreenCoords !== 'function' || !point) return { ...zeroPoint };
      const screen = graph.graph2ScreenCoords(point.x, point.y);
      return {
        x: Number(screen && screen.x),
        y: Number(screen && screen.y),
      };
    };
    const starPoint = toScreen(star || zeroVector);
    const planetPoint = toScreen(planet || zeroVector);
    const starEdge = star ? toScreen({ x: Number(star.x) + Number(star.radius || 0), y: Number(star.y) }) : { ...zeroPoint };
    const planetEdge = planet ? toScreen({ x: Number(planet.x) + Number(planet.radius || 0), y: Number(planet.y) }) : { ...zeroPoint };
    const canvas = document.querySelector('#graph-canvas canvas, #graph-net canvas');
    const bounds = canvas && typeof canvas.getBoundingClientRect === 'function'
      ? canvas.getBoundingClientRect()
      : null;
    const local = star && planet
      ? { x: Number(planet.x) - Number(star.x), y: Number(planet.y) - Number(star.y) }
      : { ...zeroPoint };
    const screenLocal = {
      x: planetPoint.x - starPoint.x,
      y: planetPoint.y - starPoint.y,
    };
    const inside = point => {
      if (!bounds) return false;
      return point.x >= 0 && point.y >= 0
        && point.x <= bounds.width && point.y <= bounds.height;
    };
    const diagnostics = engine && typeof engine.physicsDiagnostics === 'function'
      ? engine.physicsDiagnostics() || {}
      : {};
    const nodeRadius = node => Number(node && (node.radius || node.visual_radius) || 0);
    const blackHolePadding = Number(diagnostics.blackHoleExclusionPadding || 0);
    const anchorPoint = anchor || zeroVector;
    const blackHoleClearances = anchor
      ? nodes.filter(node => node !== anchor).map(node =>
        Math.hypot(Number(node.x) - Number(anchorPoint.x), Number(node.y) - Number(anchorPoint.y))
          - nodeRadius(anchorPoint) - nodeRadius(node) - blackHolePadding)
      : [];
    const stellarClearances = nodes.flatMap(node => {
      const stellarAnchor = byId.get(String(node.system_anchor_id));
      if (!stellarAnchor || stellarAnchor === node
          || stellarAnchor.anchor_role !== 'community') return [];
      return [Math.hypot(Number(node.x) - Number(stellarAnchor.x), Number(node.y) - Number(stellarAnchor.y))
        - nodeRadius(stellarAnchor) - nodeRadius(node)
        - Number(diagnostics.systemAnchorExclusionPadding || 0)];
    });
    const envelope = Number(diagnostics.farFieldConfinement
      && diagnostics.farFieldConfinement.envelopeRadius);
    const outerClearances = anchor
      ? nodes.filter(node => node !== anchor).map(node =>
        envelope - Math.hypot(Number(node.x) - Number(anchorPoint.x), Number(node.y) - Number(anchorPoint.y))
          - nodeRadius(node))
      : [];
    const safeMin = values => (values.length ? Math.min(...values) : 0);
    const settings = engine && typeof engine.state === 'function' && engine.state()
      ? engine.state().settings
      : null;
    const collapsed = engine && typeof engine.state === 'function' && engine.state()
      ? engine.state().collapsed
      : null;
    const finite = [
      starPoint.x, starPoint.y, planetPoint.x, planetPoint.y,
      center.x, center.y, center.vx, center.vy,
      local.x, local.y, screenLocal.x, screenLocal.y,
    ].every(Number.isFinite) && Boolean(star && planet) && anchorValid;
    return {
      star: star ? { id: star.id, x: Number(star.x) || 0, y: Number(star.y) || 0,
        vx: Number(star.vx) || 0, vy: Number(star.vy) || 0,
        warp: Number(star.__galaxySpacetimeWarp) || 0,
        mass: Number(star.gravity_mass) || 1,
        screenX: starPoint.x, screenY: starPoint.y,
        screenRadius: Math.abs(starEdge.x - starPoint.x) } : null,
      planet: planet ? { id: planet.id, anchor: planet.system_anchor_id || null,
        x: Number(planet.x) || 0, y: Number(planet.y) || 0, vx: Number(planet.vx) || 0,
        vy: Number(planet.vy) || 0, mass: Number(planet.gravity_mass) || 1,
        screenX: planetPoint.x, screenY: planetPoint.y,
        screenRadius: Math.abs(planetEdge.x - planetPoint.x) } : null,
      local: { ...local, radius: Math.hypot(local.x, local.y),
        angle: Math.atan2(local.y, local.x),
        relativeSpeed: star && planet
          ? Math.hypot((Number(planet.vx) || 0) - (Number(star.vx) || 0),
            (Number(planet.vy) || 0) - (Number(star.vy) || 0))
          : 0 },
      screenLocal: { ...screenLocal, radius: Math.hypot(screenLocal.x, screenLocal.y),
        angle: Math.atan2(screenLocal.y, screenLocal.x) },
      // Keep the compact phase names used by the focused browser contract alongside the
      // richer local/screenLocal payload consumed by the existing regression tests.
      phase: Math.atan2(local.y, local.x),
      screenPhase: Math.atan2(screenLocal.y, screenLocal.x),
      center,
      anchor: anchor ? { id: anchor.id, anchorRole: anchor.anchor_role,
        x: Number(anchor.x) || 0, y: Number(anchor.y) || 0,
        vx: Number(anchor.vx) || 0, vy: Number(anchor.vy) || 0,
        radius: nodeRadius(anchor), warp: Number(anchor.__galaxySpacetimeWarp) || 0 }
        : null,
      anchorValid,
      globalAnchorCount: globalAnchors.length,
      coreFollower: (() => {
        const node = byId.get('core-star');
        return node ? { x: Number(node.x) || 0, y: Number(node.y) || 0,
          vx: Number(node.vx) || 0, vy: Number(node.vy) || 0 } : null;
      })(),
      systemCenter: center,
      globalAngle: Math.atan2(center.y - (anchor ? Number(anchor.y) || 0 : 0),
        center.x - (anchor ? Number(anchor.x) || 0 : 0)),
      visible: inside(starPoint) && inside(planetPoint),
      canvas: { width: bounds ? bounds.width : 0, height: bounds ? bounds.height : 0 },
      zoom: canvas && canvas.__zoom ? canvas.__zoom.k : null,
      diagnostics,
      safety: {
        minimumBlackHoleClearance: safeMin(blackHoleClearances),
        minimumStellarClearance: safeMin(stellarClearances),
        minimumOuterClearance: safeMin(outerClearances),
        envelope,
        maximumSpeed: nodes.length ? Math.max(...nodes.map(node => Math.hypot(
          Number(node.vx) || 0, Number(node.vy) || 0,
        ))) : 0,
        speedCapActivations: diagnostics.speedCapActivations || 0,
      },
      settings,
      collapsed,
      finite,
    };
  }, systemId);
}

/* Unlike the single-orbit rendering helper above, this records every eligible local member.
   The production-sized fixture has one core satellite plus 480 planetary members, which makes
   a passed aggregate/global orbit unable to hide one frozen planet. */
async function renderedAllLocalOrbitSnapshot(page) {
  return page.evaluate(() => {
    const graph = window.__fg;
    const engine = window.__engraphisGraph;
    const nodes = graph.graphData().nodes.filter(node => !node.ghost);
    const byId = new Map(nodes.map(node => [String(node.id), node]));
    const blackHole = nodes.find(node => node.anchor_role === 'global');
    const radius = node => Number(node.radius || node.visual_radius || 0);
    const members = nodes.flatMap(node => {
      const anchorId = node.system_anchor_id == null ? null : String(node.system_anchor_id);
      const anchor = anchorId && byId.get(anchorId);
      if (!anchor || anchor === node) return [];
      const dx = node.x - anchor.x, dy = node.y - anchor.y;
      const dvx = (Number(node.vx) || 0) - (Number(anchor.vx) || 0);
      const dvy = (Number(node.vy) || 0) - (Number(anchor.vy) || 0);
      return [{
        id: String(node.id), anchorId,
        communityId: String(node.community_id ?? node.community ?? 'ungrouped'),
        radius: Math.hypot(dx, dy),
        angle: Math.atan2(dy, dx), tangent: dx * dvy - dy * dvx,
        paintedRadius: radius(node), anchorPaintedRadius: radius(anchor),
        clearance: Math.hypot(dx, dy) - radius(node) - radius(anchor)
          - Number(engine.physicsDiagnostics().systemAnchorExclusionPadding || 0),
        finite: [node.x, node.y, node.vx, node.vy, anchor.x, anchor.y, anchor.vx, anchor.vy]
          .every(Number.isFinite),
      }];
    });
    const diagnostics = engine.physicsDiagnostics();
    /* The oversized renderer stores a star's black-hole carrier phase directly on that star.
       Its world coordinate must be exactly that carrier, not a mass-weighted local COM offset.
       Reading this in the served browser catches the visual failure a pure angle test misses:
       planets may spin correctly while their painted central star wobbles with them. */
    const anchors = blackHole ? nodes.filter(node => node.anchor_role === 'community').map(node => {
      const orbit = node.__galaxyKinematicGlobalOrbit;
      const expectedX = orbit && Number.isFinite(orbit.angle) && Number.isFinite(orbit.radius)
        ? blackHole.x + Math.cos(orbit.angle) * orbit.radius : null;
      const expectedY = orbit && Number.isFinite(orbit.angle) && Number.isFinite(orbit.radius)
        ? blackHole.y + Math.sin(orbit.angle) * orbit.radius : null;
      return {
        id: String(node.id), hasCarrier: expectedX !== null && expectedY !== null,
        carrierError: expectedX === null ? null : Math.hypot(node.x - expectedX, node.y - expectedY),
      };
    }) : [];
    return { members, diagnostics, finite: nodes.every(node =>
      [node.x, node.y, node.vx, node.vy].every(Number.isFinite)),
      anchors,
    };
  });
}

/* The local-orbit recorder above deliberately works in each star's frame.  This companion
   recorder works in the black-hole frame and includes every rendered non-anchor body:
   a correct local carousel is not sufficient if a planet is visually pinned in the galaxy.
   A visible historical ghost is a massless test particle — it must advance too, while never
   contributing mass or force to the live galaxy. */
async function renderedAllGlobalOrbitSnapshot(page) {
  return page.evaluate(() => {
    const graph = window.__fg;
    const engine = window.__engraphisGraph;
    const rendered = graph.graphData().nodes;
    const live = rendered.filter(node => !node.ghost);
    const anchor = live.find(node => node.anchor_role === 'global');
    const mass = node => Math.max(0.01, Number(node.gravity_mass) || 1);
    const groups = new Map();
    rendered.forEach(node => {
      const id = String(node.community_id ?? node.community ?? 'ungrouped');
      if (!groups.has(id)) groups.set(id, []);
      groups.get(id).push(node);
    });
    const phase = (node, x, y, vx, vy) => {
      const dx = x - anchor.x, dy = y - anchor.y;
      const dvx = vx - (Number(anchor.vx) || 0);
      const dvy = vy - (Number(anchor.vy) || 0);
      return {
        radius: Math.hypot(dx, dy), angle: Math.atan2(dy, dx),
        tangent: dx * dvy - dy * dvx,
      };
    };
    const members = anchor ? rendered.filter(node => node !== anchor).map(node => {
      const value = phase(node, node.x, node.y, Number(node.vx) || 0, Number(node.vy) || 0);
      return {
        id: String(node.id), communityId: String(node.community_id ?? node.community ?? 'ungrouped'),
        ghost: node.ghost === true,
        anchorRole: node.anchor_role || 'none',
        systemAnchorId: node.system_anchor_id == null ? null : String(node.system_anchor_id),
        carrierLaneRadius: Number(node.__galaxyCarrierLaneRadius) || null,
        ...value,
        finite: [node.x, node.y, node.vx, node.vy].every(Number.isFinite),
      };
    }).filter(node => node.radius > 1e-7) : [];
    const systems = anchor ? [...groups.entries()].map(([id, nodes]) => {
      const total = nodes.reduce((sum, node) => sum + mass(node), 0);
      const x = nodes.reduce((sum, node) => sum + node.x * mass(node), 0) / total;
      const y = nodes.reduce((sum, node) => sum + node.y * mass(node), 0) / total;
      const vx = nodes.reduce((sum, node) => sum + (Number(node.vx) || 0) * mass(node), 0) / total;
      const vy = nodes.reduce((sum, node) => sum + (Number(node.vy) || 0) * mass(node), 0) / total;
      return { id, ...phase(null, x, y, vx, vy) };
    }).filter(system => system.radius > 1e-7) : [];
    return {
      anchor: anchor && String(anchor.id),
      members, systems,
      ghostIds: rendered.filter(node => node.ghost).map(node => String(node.id)).sort(),
      diagnostics: engine.physicsDiagnostics(),
      finite: rendered.every(node => [node.x, node.y, node.vx, node.vy].every(Number.isFinite)),
    };
  });
}

/* Observe ForceGraph's real node painter, not an engine coordinate or whole-canvas checksum.
   The wrapper delegates to the production painter exactly once, while recording every stellar
   carrier and every direct black-hole child at the world/screen coordinate actually submitted
   to the canvas. Background stars and black-hole disk spin cannot satisfy this oracle. */
async function installCarrierPaintAudit(page) {
  return page.evaluate(() => {
    const graph = window.__fg;
    const nodes = graph.graphData().nodes.filter(node => !node.ghost);
    const blackHole = nodes.find(node => node.anchor_role === 'global');
    const wanted = nodes.filter(node => node !== blackHole && (
      node.anchor_role === 'community'
      || String(node.system_anchor_id || '') === String(blackHole && blackHole.id)
      || node.__galaxyBlackHoleChild === true
    ));
    const ids = wanted.map(node => String(node.id)).sort();
    const idSet = new Set(ids);
    const original = graph.nodeCanvasObject();
    if (typeof original !== 'function') {
      throw new Error('Production node painter is not installed');
    }
    const audit = { ids, counts: {}, last: {} };
    graph.nodeCanvasObject((node, context, scale) => {
      const id = String(node.id);
      if (idSet.has(id)) {
        const point = graph.graph2ScreenCoords(node.x, node.y);
        const canvas = context && context.canvas;
        const bounds = canvas && canvas.getBoundingClientRect();
        audit.counts[id] = (audit.counts[id] || 0) + 1;
        audit.last[id] = {
          worldX: node.x, worldY: node.y, screenX: point.x, screenY: point.y,
          insideCanvas: !bounds || (point.x >= 0 && point.y >= 0
            && point.x <= bounds.width && point.y <= bounds.height),
        };
      }
      return original(node, context, scale);
    });
    window.__carrierPaintAudit = audit;
    graph.zoom(graph.zoom());
    return ids;
  });
}

async function carrierPaintAuditSnapshot(page) {
  return page.evaluate(() => {
    const audit = window.__carrierPaintAudit;
    return {
      ids: audit ? audit.ids : [],
      counts: audit ? { ...audit.counts } : {},
      last: audit ? JSON.parse(JSON.stringify(audit.last)) : {},
      diagnostics: window.__engraphisGraph.physicsDiagnostics(),
      hidden: document.hidden,
    };
  });
}

function signedAngleDelta(from, to) {
  return Math.atan2(Math.sin(to - from), Math.cos(to - from));
}

async function gravityTrial(page, gravity, stepCount = 8) {
  await page.evaluate(({ scene, setting }) => {
    const api = window.__engraphisGraph;
    api.freeze(true);
    api.setPreset('galaxy');
    api.setSettings({ gravity: setting, size: 1 });
    api.setData(scene);
    api.setScope({ showUnlinked: true, minDegree: 0 });
  }, { scene: blackHoleGalaxyScene, setting: gravity });
  await page.waitForFunction(() => window.__fg.graphData().nodes.length === 8
    && window.__engraphisGraph.physicsDiagnostics().frozen
    && window.__fg.graphData().nodes.every(node => Number.isFinite(node.x)
      && Number.isFinite(node.y) && Number.isFinite(node.radius)));
  const before = await galaxySystemSnapshot(page);
  const curve = await page.evaluate(() => ({
    setting: window.__engraphisGraph.state().settings.gravity,
    baseline: window.EngraphisGraph._internals.galaxyBlackHoleGravityConstant(48),
    maximum: window.EngraphisGraph._internals.galaxyBlackHoleGravityConstant(200),
    localBaseline: window.EngraphisGraph._internals.galaxyLocalGravityConstant(48),
    localMaximum: window.EngraphisGraph._internals.galaxyLocalGravityConstant(200),
  }));
  await page.evaluate(() => window.__engraphisGraph.freeze(false));
  const samples = [before];
  for (let step = 1; step <= stepCount; step += 1) {
    await page.waitForFunction(({ start, minimum }) =>
      window.__engraphisGraph.physicsDiagnostics().steps >= start + minimum,
    { start: before.diagnostics.steps, minimum: step });
    samples.push(await galaxySystemSnapshot(page));
  }
  await page.evaluate(() => window.__engraphisGraph.freeze(true));
  const after = samples.at(-1);
  return {
    before, after, curve,
    samples,
    steps: after.diagnostics.steps - before.diagnostics.steps,
  };
}

async function orbitalSeparationTrial(page, separation, stepCount = 8) {
  await page.evaluate(({ scene, setting }) => {
    const api = window.__engraphisGraph;
    /* Dominant star↔planet pairs use the stellar-surface exclusion rather than generic
       Repel pressure. Add one linked non-anchor moon so this trial still exercises the
       adjustable planet↔moon separation and Link constraints. */
    const trialScene = {
      ...scene,
      nodes: scene.nodes.map(node => ({ ...node })),
      edges: scene.edges.map(edge => ({ ...edge })),
      communities: scene.communities.map(community => ({ ...community })),
      community_bridges: scene.community_bridges.map(bridge => ({ ...bridge })),
      meta: { ...scene.meta },
    };
    const auroraPlanet = trialScene.nodes.find(node => node.id === 'aurora-planet');
    trialScene.nodes.push({
      id: 'aurora-moon', label: 'Aurora moon', gravity_mass: 1, visual_radius: 8,
      community_id: 'aurora', anchor_role: 'none', system_anchor_id: 'aurora-planet',
      orbit_tier: 2, orbit_radius: 19.2, galactic_radius: auroraPlanet.galactic_radius,
      galactic_target_radius: auroraPlanet.galactic_target_radius,
      galactic_radius_scale: auroraPlanet.galactic_radius_scale,
      galactic_initial_compactness: auroraPlanet.galactic_initial_compactness,
      galactic_phase: auroraPlanet.galactic_phase, x: auroraPlanet.x + 1.8,
      y: auroraPlanet.y - 0.4,
    });
    trialScene.edges.push({
      id: 'aurora-planet-moon', source: 'aurora-planet', target: 'aurora-moon',
      relation: 'orbits with', rest_length: 20, spring_strength: 0.08,
    });
    api.freeze(true);
    api.setPreset('galaxy');
    api.setSettings({ gravity: 0, repel: setting, link: 8, size: 1 });
    api.setData(trialScene);
    api.setScope({ showUnlinked: true, minDegree: 0 });
  }, { scene: blackHoleGalaxyScene, setting: separation });
  await page.waitForFunction(() => window.__fg.graphData().nodes.length === 9
    && window.__engraphisGraph.physicsDiagnostics().frozen);
  const before = await galaxySystemSnapshot(page);
  await page.evaluate(() => window.__engraphisGraph.freeze(false));
  const samples = [];
  for (let step = 1; step <= stepCount; step += 1) {
    await page.waitForFunction(({ start, minimum }) =>
      window.__engraphisGraph.physicsDiagnostics().steps >= start + minimum,
    { start: before.diagnostics.steps, minimum: step });
    samples.push(await galaxySystemSnapshot(page));
  }
  await page.evaluate(() => window.__engraphisGraph.freeze(true));
  const after = samples.at(-1);
  const meanDiameter = snapshot => snapshot.systems.reduce(
    (sum, system) => sum + system.internalDiameter, 0,
  ) / snapshot.systems.length;
  const stellarRadius = snapshot => {
    const star = snapshot.nodes['aurora-star'];
    const planet = snapshot.nodes['aurora-planet'];
    return Math.hypot(star.x - planet.x, star.y - planet.y);
  };
  const nonAnchorRadius = snapshot => {
    const planet = snapshot.nodes['aurora-planet'];
    const moon = snapshot.nodes['aurora-moon'];
    return Math.hypot(planet.x - moon.x, planet.y - moon.y);
  };
  const stellarRepelTarget = snapshot => {
    const star = snapshot.nodes['aurora-star'];
    const planet = snapshot.nodes['aurora-planet'];
    return star.radius + planet.radius + snapshot.diagnostics.orbitalSeparationPadding;
  };
  return {
    before,
    after,
    beforeDiameter: meanDiameter(before),
    afterDiameter: meanDiameter(after),
    maximumSeparations: Math.max(...samples.map(
      sample => sample.diagnostics.lastOrbitalSeparations,
    )),
    starPlanetBefore: stellarRadius(before),
    starPlanetAfter: stellarRadius(after),
    starPlanetRepelTarget: stellarRepelTarget(after),
    nonAnchorBefore: nonAnchorRadius(before),
    nonAnchorAfter: nonAnchorRadius(after),
    minimumSystemAnchorClearance: Math.min(...samples.map(sample =>
      sample.diagnostics.systemAnchorExclusion.minimumClearance)),
    corrections: samples.map(sample =>
      sample.diagnostics.lastRelationCorrectionDistance
        + sample.diagnostics.lastOrbitalCorrectionDistance),
    contactTrace: samples.map(sample => ({
      relation: sample.diagnostics.lastRelationCorrectionDistance,
      orbital: sample.diagnostics.lastOrbitalCorrectionDistance,
      overlaps: sample.diagnostics.lastOrbitalSeparations,
      anchorClearance: sample.diagnostics.systemAnchorExclusion.minimumClearance,
      planet: sample.nodes['aurora-planet'],
      moon: sample.nodes['aurora-moon'],
      star: sample.nodes['aurora-star'],
    })),
  };
}

test('Ledger releases a dragged node without reheating the graph', async ({ page }) => {
  const session = await openDashboard(page);
  await page.goto('/');
  await expect(page.locator('.nav-item[data-view="relations"]')).toBeVisible();
  await page.locator('.nav-item[data-view="relations"]').click();
  await expect(page.locator('#graph-canvas canvas').first()).toBeAttached({ timeout: 20_000 });
  await page.waitForFunction(() => window.__engraphisGraph && window.__fg);
  // Use a small live scene so this regression exercises the normal D3 force path rather than
  // the deliberate static fallback used for very large snapshots.
  await page.evaluate(scene => {
    window.__engraphisGraph.setPreset('compact');
    window.__engraphisGraph.setData(scene);
    window.__engraphisGraph.setScope({ showUnlinked: true, minDegree: 0 });
    window.__engraphisGraph.freeze(false);
  }, graphScenePayload);
  await page.waitForFunction(() => window.__fg.graphData().nodes.length === 8
    && !window.__engraphisGraph.physicsDiagnostics().staticLayout);
  await page.waitForFunction(() => {
    const node = window.__fg.graphData().nodes[0];
    return Number.isFinite(node.x) && Number.isFinite(node.y);
  });
  // Let the initial layout and zoom-to-fit settle before modelling a user gesture.
  await page.waitForTimeout(3000);
  const drag = await page.evaluate(() => {
    const graph = window.__fg;
    const nodes = graph.graphData().nodes;
    const links = graph.graphData().links;
    const node = nodes.find(candidate => links.some(link => {
      const source = typeof link.source === 'object' ? link.source.id : link.source;
      const target = typeof link.target === 'object' ? link.target.id : link.target;
      return source === candidate.id || target === candidate.id;
    })) || nodes[0];
    const connectedIds = links.filter(link => {
      const source = typeof link.source === 'object' ? link.source.id : link.source;
      const target = typeof link.target === 'object' ? link.target.id : link.target;
      return source === node.id || target === node.id;
    }).map(link => {
      const source = typeof link.source === 'object' ? link.source.id : link.source;
      const target = typeof link.target === 'object' ? link.target.id : link.target;
      return source === node.id ? target : source;
    });
    const canvas = document.querySelector('#graph-canvas canvas');
    const box = canvas.getBoundingClientRect();
    const point = graph.graph2ScreenCoords(node.x, node.y);
    const originalReheat = typeof graph.d3ReheatSimulation === 'function'
      ? graph.d3ReheatSimulation.bind(graph) : null;
    const originalAlphaTarget = typeof graph.d3AlphaTarget === 'function'
      ? graph.d3AlphaTarget.bind(graph) : null;
    const originalResetCountdown = typeof graph.resetCountdown === 'function'
      ? graph.resetCountdown.bind(graph) : null;
    window.__dragReheatCount = 0;
    window.__dragSoftKickCount = 0;
    window.__dragResetCount = 0;
    if (originalReheat) graph.d3ReheatSimulation = (...args) => {
      window.__dragReheatCount += 1;
      return originalReheat(...args);
    };
    if (originalAlphaTarget) graph.d3AlphaTarget = (...args) => {
      window.__dragSoftKickCount += 1;
      return originalAlphaTarget(...args);
    };
    if (originalResetCountdown) graph.resetCountdown = (...args) => {
      window.__dragResetCount += 1;
      return originalResetCountdown(...args);
    };
    return {
      before: { x: node.x, y: node.y },
      id: node.id,
      connectedIds,
      zoom: canvas.__zoom.k,
      camera: { x: canvas.__zoom.x, y: canvas.__zoom.y },
      x: box.left + point.x,
      y: box.top + point.y,
    };
  });
  expect(drag.zoom).toBeLessThanOrEqual(4);
  expect(drag.connectedIds.length).toBeGreaterThan(0);
  expect(Number.isFinite(drag.x) && Number.isFinite(drag.y)).toBe(true);
  await page.mouse.move(drag.x, drag.y);
  await page.waitForTimeout(100);
  await page.evaluate(() => {
    window.__dragReheatCount = 0;
    window.__dragSoftKickCount = 0;
    window.__dragResetCount = 0;
  });
  const restBeforeDrag = await page.evaluate(draggedId => window.__fg.graphData().nodes
    .filter(item => item.id !== draggedId)
    .map(item => ({ id: item.id, x: item.x, y: item.y })), drag.id);
  await page.mouse.down();
  await page.mouse.move(drag.x + 80, drag.y + 40, { steps: 8 });
  await page.waitForTimeout(250);
  const during = await page.evaluate(draggedId => window.__fg.graphData().nodes
    .filter(item => item.id !== draggedId)
    .map(item => ({ id: item.id, x: item.x, y: item.y })), drag.id);
  const duringGuard = await page.evaluate(() => Boolean(window.__fg.d3Force('velocityGuard')));
  const duringPosition = await page.evaluate(draggedId => {
    const node = window.__fg.graphData().nodes.find(item => item.id === draggedId);
    return { x: node.x, y: node.y };
  }, drag.id);
  const duringReheats = await page.evaluate(() => window.__dragReheatCount);
  await page.mouse.up();
  await page.waitForFunction(draggedId => {
    const node = window.__fg.graphData().nodes.find(item => item.id === draggedId);
    return node && node.fx === undefined && node.fy === undefined
      && Boolean(window.__fg.d3Force('velocityGuard'));
  }, drag.id);
  const after = await page.evaluate(draggedId => {
    const nodes = window.__fg.graphData().nodes;
    const node = nodes.find(item => item.id === draggedId);
    const canvas = document.querySelector('#graph-canvas canvas');
    return {
      x: node.x, y: node.y, fx: node.fx, fy: node.fy,
      vx: node.vx, vy: node.vy,
      finite: nodes.every(item => [item.x, item.y, item.vx, item.vy]
        .every(value => Number.isFinite(value))),
      maxSpeed: Math.max(...nodes.map(item => Math.hypot(item.vx || 0, item.vy || 0))),
      hasVelocityGuard: Boolean(window.__fg.d3Force('velocityGuard')),
      camera: { x: canvas.__zoom.x, y: canvas.__zoom.y, k: canvas.__zoom.k },
      reheats: window.__dragReheatCount,
      softKicks: window.__dragSoftKickCount,
      resets: window.__dragResetCount,
    };
  }, drag.id);

  const initial = new Map(restBeforeDrag.map(item => [item.id, item]));
  const maximumUnlinkedMovement = Math.max(...during
    .filter(item => !drag.connectedIds.includes(item.id))
    .map(item => {
    const before = initial.get(item.id);
    return Math.hypot(item.x - before.x, item.y - before.y);
    }));
  // Live physics may advance unrelated nodes while the simulation is already running, but a
  // drag must not reheat the whole graph into an unbounded flight.
  expect(maximumUnlinkedMovement).toBeLessThan(64);
  expect(duringPosition.x - drag.before.x).toBeCloseTo(80 / drag.zoom, 0);
  expect(duringPosition.y - drag.before.y).toBeCloseTo(40 / drag.zoom, 0);
  expect(after.fx).toBeUndefined();
  expect(after.fy).toBeUndefined();
  expect(Number.isFinite(after.vx) && Number.isFinite(after.vy)).toBe(true);
  expect(Math.hypot(after.vx, after.vy)).toBeLessThanOrEqual(48);
  expect(after.finite && after.maxSpeed <= 50).toBe(true);
  // Drag no longer detaches the active solver. Legacy layouts retain their velocity guard;
  // Galaxy retains its fixed-step clock, and neither path globally freezes the graph.
  expect(duringGuard).toBe(true);
  expect(after.hasVelocityGuard).toBe(true);
  expect(after.camera.k).toBeCloseTo(drag.zoom, 3);
  expect(after.camera.x).toBeCloseTo(drag.camera.x, 1);
  expect(after.camera.y).toBeCloseTo(drag.camera.y, 1);
  expect(duringReheats).toBe(0);
  expect(after.reheats).toBeLessThanOrEqual(2);
  expect(after.softKicks).toBeLessThanOrEqual(2);
  expect(after.resets).toBeLessThanOrEqual(2);
  expect(session.pageErrors).toEqual([]);
});

test('Classic releases a dragged node without reheating with reduced visual motion', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' });
  const session = await openDashboard(page, { query: '?graph-engine=next' });
  await openGraphView(page);
  await page.waitForFunction(() => window.__fg && window.__fg.graphData().nodes.length > 0);
  await page.waitForFunction(() => window.__fg.graphData().nodes
    .every(node => Number.isFinite(node.x) && Number.isFinite(node.y)));
  await page.waitForTimeout(500);

  const drag = await page.evaluate(() => {
    const graph = window.__fg;
    const node = graph.graphData().nodes[0];
    const canvas = document.querySelector('#graph-net canvas');
    const box = canvas.getBoundingClientRect();
    const point = graph.graph2ScreenCoords(node.x, node.y);
    return {
      before: { x: node.x, y: node.y },
      zoom: canvas.__zoom.k,
      x: box.left + point.x,
      y: box.top + point.y,
    };
  });

  await page.mouse.move(drag.x, drag.y);
  await page.mouse.down();
  // Reduced motion suppresses visual transitions, not the live physics clock.  Move far
  // enough to cross the manual-drag threshold, then use the actual held position as the
  // baseline; the node may have advanced between the initial hit-test snapshot and pointerdown.
  await page.mouse.move(drag.x + 8, drag.y + 4);
  const dragStart = await page.evaluate(() => {
    const node = window.__fg.graphData().nodes[0];
    return { x: node.x, y: node.y, fx: node.fx, fy: node.fy };
  });
  await page.mouse.move(drag.x + 80, drag.y + 40, { steps: 7 });
  const during = await page.evaluate(() => {
    const node = window.__fg.graphData().nodes[0];
    return { x: node.x, y: node.y, fx: node.fx, fy: node.fy };
  });
  await page.mouse.up();
  const after = await page.evaluate(() => {
    const node = window.__fg.graphData().nodes[0];
    return { x: node.x, y: node.y, fx: node.fx, fy: node.fy };
  });

  expect(during.x - dragStart.x).toBeCloseTo(72 / drag.zoom, 0);
  expect(during.y - dragStart.y).toBeCloseTo(36 / drag.zoom, 0);
  expect(Number.isFinite(during.fx) && Number.isFinite(during.fy)).toBe(true);
  expect(after.fx).toBeUndefined();
  expect(after.fy).toBeUndefined();
  expect(session.pageErrors).toEqual([]);
});

test('a dashboard page that never opens the graph fetches neither graph script', async ({ page }) => {
  // Both graph scripts are lazy-loaded so that a page view which never opens the graph does
  // not pay the cost of fetching vendor bundles, and so the strict CSP (which already
  // refuses inline styles via same-origin extracted CSS) is never exercised by graph
  // code on non-graph views.  This asserts the deferral in the only place it is real.
  const session = await openDashboard(page);
  await page.click('.nav-item[data-view="memories"]');
  await page.waitForTimeout(500);

  expect(fetched(session.requested, 'force-graph.min.js')).toEqual([]);
  expect(fetched(session.requested, 'engraphis-graph.js')).toEqual([]);
  expect(await session.violations()).toEqual([]);
  expect(session.pageErrors).toEqual([]);
});

test('the opt-in engine renders a real canvas and registers under its flag', async ({ page }) => {
  const session = await openDashboard(page, { query: '?graph-engine=next' });
  const canvas = await openGraphView(page);

  // Both assets arrive, and only now.
  expect(fetched(session.requested, 'engraphis-graph.js').length).toBe(1);
  expect(fetched(session.requested, 'force-graph.min.js').length).toBe(1);
  expect(await page.evaluate(() => typeof (window.EngraphisGraph || {}).create)).toBe('function');
  // GRAPH_ENGINE is only assigned when graphRenderEngine() actually took the render.  It is a
  // top-level `let`, so it lives in the global lexical scope rather than on `window`.
  expect(await page.evaluate(() => Boolean(GRAPH_ENGINE))).toBe(true);
  // The default scope includes degree-zero entities so the graph does not silently omit evidence.
  const painted = await page.evaluate(() => window.__fg.graphData().nodes.map(n => n.id).sort());
  expect(painted).toEqual(['ada', 'babbage', 'engine', 'fts', 'notes', 'orphan', 'sqlite', 'store']);

  // The canvas has pixels on it.  A silent failure in the paint path leaves a correctly sized
  // but entirely uniform canvas, which is exactly what a screenshot-free assertion misses.
  const distinctColours = await page.evaluate(() => {
    const c = document.querySelector('#graph-net canvas');
    const data = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
    const seen = new Set();
    for (let i = 0; i < data.length; i += 4) {
      seen.add(`${data[i]},${data[i + 1]},${data[i + 2]},${data[i + 3]}`);
      if (seen.size > 8) break;
    }
    return seen.size;
  });
  expect(distinctColours).toBeGreaterThan(2);
  await expect(canvas).toBeVisible();

  expect(session.pageErrors).toEqual([]);
});

test('Classic defaults to the canonical engine without a query flag', async ({ page }) => {
  const session = await openDashboard(page);
  const canvas = await openGraphView(page);

  expect(fetched(session.requested, 'engraphis-graph.js').length).toBe(1);
  expect(fetched(session.requested, 'force-graph.min.js').length).toBe(1);
  expect(await page.evaluate(() => typeof (window.EngraphisGraph || {}).create)).toBe('function');
  expect(await page.evaluate(() => Boolean(GRAPH_ENGINE))).toBe(true);
  await expect(canvas).toBeVisible();
  expect(session.pageErrors).toEqual([]);
});

test('the live graph paints materially different hub faces for every style', async ({ page }) => {
  const session = await openDashboard(page, { query: '?graph-engine=next' });
  await openGraphView(page);
  await page.waitForFunction(() => {
    const nodes = window.__fg && window.__fg.graphData().nodes;
    return nodes && nodes.length && nodes.every(node => Number.isFinite(node.x) && Number.isFinite(node.y));
  });

  const sampleFace = async style => {
    await page.evaluate(value => graphSetStyle(value), style);
    await page.waitForTimeout(100);
    return page.evaluate(() => {
      const canvas = document.querySelector('#graph-net canvas');
      const pixels = canvas.getContext('2d').getImageData(
        0, 0, canvas.width, canvas.height,
      ).data;
      const sums = [0, 0, 0];
      let count = 0;
      let luminanceSum = 0;
      let luminanceSquared = 0;
      const step = Math.max(1, Math.round(window.devicePixelRatio || 1));
      for (let y = 0; y < canvas.height; y += step) {
        for (let x = 0; x < canvas.width; x += step) {
          const offset = (y * canvas.width + x) * 4;
          // Opaque pixels are overwhelmingly material faces/rims. Translucent relation
          // lines and style backgrounds cannot satisfy this gate by themselves.
          if (pixels[offset + 3] < 200) continue;
          const rgb = [pixels[offset], pixels[offset + 1], pixels[offset + 2]];
          rgb.forEach((value, channel) => { sums[channel] += value; });
          const luminance = rgb[0] * 0.2126 + rgb[1] * 0.7152 + rgb[2] * 0.0722;
          luminanceSum += luminance;
          luminanceSquared += luminance * luminance;
          count += 1;
        }
      }
      const mean = sums.map(value => Math.round(value / count));
      const luminanceMean = luminanceSum / count;
      return {
        count,
        mean,
        textureVariance: Math.round(luminanceSquared / count - luminanceMean * luminanceMean),
      };
    });
  };

  const faces = {};
  for (const style of ['cyber', 'galaxy', 'solar', 'classic']) {
    faces[style] = await sampleFace(style);
  }
  expect(faces).toMatchObject({
    cyber: { count: expect.any(Number) },
    galaxy: { count: expect.any(Number) },
    solar: { count: expect.any(Number) },
    classic: { count: expect.any(Number) },
  });
  expect(Math.min(...Object.values(faces).map(face => face.count))).toBeGreaterThan(20);
  expect(new Set(Object.values(faces).map(face => face.mean.join(','))).size).toBe(4);
  expect(faces.galaxy.mean[2]).toBeGreaterThan(faces.galaxy.mean[0]);
  expect(faces.solar.mean[0]).toBeGreaterThan(faces.solar.mean[1]);
  expect(faces.solar.mean[1]).toBeGreaterThan(faces.solar.mean[2]);
  expect(Math.max(...faces.classic.mean) - Math.min(...faces.classic.mean)).toBeLessThan(55);
  expect(Math.min(...Object.values(faces).map(face => face.textureVariance))).toBeGreaterThan(8);
  expect(session.pageErrors).toEqual([]);
});

test('the browser material gallery preserves distinct node families at both DPRs', async ({ page }) => {
  /* This deliberately samples the cached detached canvases instead of screenshotting the live
     force graph: layout coordinates and antialiasing are not a material contract. The engine
     still does the real browser canvas work here, so this catches an OffscreenCanvas/gradient
     fallback that a Node recording context cannot see. */
  const session = await openDashboard(page, { query: '?graph-engine=next' });
  await openGraphView(page);
  const gallery = await page.evaluate(() => {
    const I = window.EngraphisGraph._internals;
    const themeColors = { accent: '#a39bf1', surface: '#16191f', canvas: '#0b0d13' };
    const identity = '#37bde4';
    const point = (sample, vertical) => {
      const ctx = sample.canvas.getContext('2d');
      const x = Math.floor(sample.width / 2);
      const y = Math.max(0, Math.min(sample.height - 1, Math.floor(sample.height * vertical)));
      return Array.from(ctx.getImageData(x, y, 1, 1).data);
    };
    const render = (style, radius, dpr) => {
      const sample = I.renderMaterialSample({ style, radius, dpr, identity, themeColors });
      return {
        tier: sample.tier, width: sample.width, height: sample.height,
        top: point(sample, 0.30), center: point(sample, 0.50), bottom: point(sample, 0.70),
      };
    };
    return Object.fromEntries(['cyber', 'galaxy', 'solar', 'classic'].map(style => [style, {
      dpr1: [4, 8, 16, 32].map(radius => render(style, radius, 1)),
      dpr2: render(style, 16, 2),
    }]));
  });

  for (const material of Object.values(gallery)) {
    expect(material.dpr1.map(sample => sample.tier)).toEqual(['signature', 'bezel', 'full', 'full']);
    expect(material.dpr2.width).toBeGreaterThan(material.dpr1[2].width);
    expect(material.dpr2.height).toBeGreaterThan(material.dpr1[2].height);
  }
  const cyber = gallery.cyber.dpr1[2];
  expect(cyber.top[0]).toBeGreaterThan(cyber.bottom[0]);
  expect(cyber.bottom[1]).toBeGreaterThan(cyber.top[1]);
  const galaxy = gallery.galaxy.dpr1[2].center;
  expect(galaxy[2]).toBeGreaterThan(galaxy[0]);
  expect(galaxy[2]).toBeGreaterThan(galaxy[1]);
  const solar = gallery.solar.dpr1[2].center;
  expect(solar[0]).toBeGreaterThan(solar[1]);
  expect(solar[1]).toBeGreaterThan(solar[2]);
  const classic = gallery.classic.dpr1[2].center;
  expect(Math.max(...classic.slice(0, 3)) - Math.min(...classic.slice(0, 3))).toBeLessThanOrEqual(55);
  expect(session.pageErrors).toEqual([]);
});

test('the deterministic graph material gallery matches its visual golden', async ({ page }, testInfo) => {
  // Canvas output is Chromium-controlled and deterministic across the supported CI platforms.
  // Keep the project name in the path, but do not fork one baseline per operating system.
  testInfo.snapshotSuffix = '';
  await page.emulateMedia({ reducedMotion: 'reduce' });
  const session = await openDashboard(page, { query: '?graph-engine=next' });
  await openGraphView(page);
  const violationsBeforeGallery = await session.violations();
  await page.evaluate(() => {
    const I = window.EngraphisGraph._internals;
    const styles = ['cyber', 'galaxy', 'solar', 'classic'];
    const radii = [4, 8, 16, 32];
    const ratios = [1, 2];
    const cellWidth = 104;
    const cellHeight = 88;
    const canvas = document.createElement('canvas');
    canvas.id = 'material-golden-gallery';
    canvas.width = cellWidth * radii.length * ratios.length;
    canvas.height = cellHeight * styles.length;
    canvas.setAttribute('aria-label', 'Deterministic graph material gallery');
    const ctx = canvas.getContext('2d');
    ctx.fillStyle = '#07090e';
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    const themeColors = { accent: '#a39bf1', surface: '#16191f', canvas: '#0b0d13' };
    const identityColor = '#37bde4';
    styles.forEach((style, row) => {
      ratios.forEach((dpr, panel) => {
        radii.forEach((screenRadius, column) => {
          const x = (panel * radii.length + column) * cellWidth;
          const y = row * cellHeight;
          ctx.fillStyle = (row + column + panel) % 2 ? '#0a0d14' : '#0d1018';
          ctx.fillRect(x + 1, y + 1, cellWidth - 2, cellHeight - 2);
          ctx.strokeStyle = panel ? '#2c3444' : '#202735';
          ctx.lineWidth = 1;
          ctx.strokeRect(x + 0.5, y + 0.5, cellWidth - 1, cellHeight - 1);
          const sample = I.renderMaterialSample({
            style, screenRadius, dpr, identityColor, themeColors,
          });
          const target = Math.max(10, screenRadius * 2.38);
          ctx.drawImage(
            sample.canvas,
            x + (cellWidth - target) / 2,
            y + (cellHeight - target) / 2,
            target,
            target,
          );
        });
      });
    });
    document.body.append(canvas);
  });

  await expect(page.locator('#material-golden-gallery')).toHaveScreenshot(
    'graph-material-gallery.png',
    { animations: 'disabled', maxDiffPixelRatio: 0.01 },
  );
  expect(await session.violations()).toEqual(violationsBeforeGallery);
  expect(session.pageErrors).toEqual([]);
});

test('a physics slider moves the layout under the opt-in engine', async ({ page }) => {
  // The regression this pins: setSettings() narrowed its reheat to `mode`, so Repel/Link/
  // Gravity/Size wrote a new force into a simulation already sitting at alpha~0.  Nothing
  // moved until the user found the Reheat button — invisible to a stand-in that records the
  // force object but never runs a solver.  Here d3 is really integrating.
  await openDashboard(page, { query: '?graph-engine=next' });
  await openGraphView(page);

  // Let the initial layout settle so any movement below is the slider's doing, not warmup.
  await page.waitForTimeout(3_000);
  const positions = () => page.evaluate(() => window.__fg.graphData()
    .nodes.map(n => `${Math.round(n.x)},${Math.round(n.y)}`).join('|'));

  const settled = await positions();
  expect(await positions()).toBe(settled);

  await page.locator('input[data-graph-setting="repel"]').fill('420');
  await page.dispatchEvent('input[data-graph-setting="repel"]', 'input');
  await page.waitForTimeout(1_500);

  expect(await positions()).not.toBe(settled);
});

test('black-hole Galaxy remains bounded and differential beyond 450 custom steps', async ({ page }) => {
  test.setTimeout(45_000);
  await openDashboard(page, { query: '?graph-engine=next' });
  await openGraphView(page);
  await page.evaluate(scene => {
    window.__engraphisGraph.setPreset('galaxy');
    window.__engraphisGraph.setSettings({ gravity: 48 });
    window.__engraphisGraph.setData(scene);
    window.__engraphisGraph.setScope({ showUnlinked: true, minDegree: 0 });
  }, blackHoleGalaxyScene);
  await page.waitForFunction(() => window.__fg.graphData().nodes.length === 8
    && window.__engraphisGraph.physicsDiagnostics().steps >= 5);

  const early = await galaxySystemSnapshot(page);
  await page.waitForTimeout(900);
  const middle = await galaxySystemSnapshot(page);
  // The legacy small-scene D3 budget was 2.2 seconds. Sampling another 2.6 seconds proves the
  // independent fixed clock remains live after that old renderer countdown would have ended.
  await page.waitForTimeout(2_600);
  const late = await galaxySystemSnapshot(page);
  const horizonStep = early.diagnostics.steps + 450;
  await page.waitForFunction(target => window.__engraphisGraph.physicsDiagnostics().steps >= target,
    horizonStep, { timeout: 30_000 });
  const horizon = await galaxySystemSnapshot(page);

  for (const sample of [early, middle, late, horizon]) {
    expect(sample.finite).toBe(true);
    expect(sample.anchor.id).toBe('black-hole');
    expect(sample.anchor.mass).toBe(64);
    expect(sample.anchor.x).toBe(0);
    expect(sample.anchor.y).toBe(0);
    expect(sample.anchor.vx).toBe(0);
    expect(sample.anchor.vy).toBe(0);
    expect(sample.systems).toHaveLength(3);
    expect(sample.d3Budget).toEqual({ time: 0, ticks: 0 });
  }

  const firstStepSpan = middle.diagnostics.steps - early.diagnostics.steps;
  const lateStepSpan = late.diagnostics.steps - middle.diagnostics.steps;
  expect(firstStepSpan).toBeGreaterThan(15);
  expect(lateStepSpan).toBeGreaterThan(60);
  expect(late.diagnostics.active).toBe(true);
  expect(late.diagnostics.scheduled).toBe(true);
  expect(late.diagnostics.maxSpeed).toBeLessThanOrEqual(48);

  const angularRates = [];
  let lateMotion = 0;
  for (const earlySystem of early.systems) {
    const middleSystem = middle.systems.find(system => system.id === earlySystem.id);
    const lateSystem = late.systems.find(system => system.id === earlySystem.id);
    const firstTurn = signedAngleDelta(earlySystem.angle, middleSystem.angle);
    const lateTurn = signedAngleDelta(middleSystem.angle, lateSystem.angle);
    const firstRate = firstTurn / firstStepSpan;
    const lateRate = lateTurn / lateStepSpan;
    angularRates.push(Math.abs(firstRate));
    lateMotion += Math.hypot(lateSystem.x - middleSystem.x, lateSystem.y - middleSystem.y);

    expect(Math.abs(firstRate)).toBeGreaterThan(0.0001);
    expect(Math.abs(lateRate)).toBeGreaterThan(0.0001);
    expect(Math.abs(firstRate)).toBeLessThan(0.2);
    expect(Math.abs(lateRate)).toBeLessThan(0.2);
    // Eccentric systems may reverse their signed angular segment near an apsis; both sampled
    // segments must nevertheless remain finite, moving, and well below a discontinuous turn.
    expect(Math.abs(firstTurn)).toBeLessThan(Math.PI);
    expect(Math.abs(lateTurn)).toBeLessThan(Math.PI);
    // Eccentric early settling may take an individual COM through periapsis; the 450-step
    // aggregate bounds below are the long-horizon collapse/ejection contract.
    expect(lateSystem.radius).toBeGreaterThan(earlySystem.radius * 0.4);
    expect(lateSystem.radius).toBeLessThan(earlySystem.radius * 1.35);
    expect(earlySystem.internalDiameter).toBeGreaterThan(8);
    expect(middleSystem.internalDiameter).toBeGreaterThan(8);
    expect(lateSystem.internalDiameter).toBeGreaterThan(8);
  }
  expect(Math.max(...angularRates) - Math.min(...angularRates)).toBeGreaterThan(0.0002);
  expect(lateMotion).toBeGreaterThan(5);

  expect(horizon.diagnostics.steps - early.diagnostics.steps).toBeGreaterThanOrEqual(450);
  const median = values => [...values].sort((left, right) => left - right)
    [Math.floor(values.length / 2)];
  const initialSystemRadii = early.systems.map(system => system.radius);
  const horizonSystemRadii = horizon.systems.map(system => system.radius);
  const initialMedianRadius = median(initialSystemRadii);
  const horizonMedianRadius = median(horizonSystemRadii);
  const initialMaximumSystemRadius = Math.max(...initialSystemRadii);
  const horizonMaximumSystemRadius = Math.max(...horizonSystemRadii);
  const systemRadiusDetails = horizon.systems.map(system => ({
    id: system.id,
    initialRadius: early.systems.find(initial => initial.id === system.id).radius,
    horizonRadius: system.radius,
    angularVelocity: system.angularVelocity,
    internalDiameter: system.internalDiameter,
  }));
  const nodeRadii = sample => Object.entries(sample.nodes).map(([id, node]) => ({
    id, radius: Math.hypot(node.x - sample.anchor.x, node.y - sample.anchor.y),
  })).sort((left, right) => left.radius - right.radius);
  const initialNodeRadii = nodeRadii(early);
  const initialRadiusById = new Map(initialNodeRadii.map(node => [node.id, node.radius]));
  const horizonNodeRadii = nodeRadii(horizon).map(node => ({
    ...node,
    initialRadius: initialRadiusById.get(node.id),
    gravityMass: horizon.nodes[node.id].gravityMass,
    communityId: horizon.nodes[node.id].communityId,
    anchorRole: horizon.nodes[node.id].anchorRole,
    systemAnchorId: horizon.nodes[node.id].systemAnchorId,
  }));

  expect(horizonMedianRadius, JSON.stringify(systemRadiusDetails))
    .toBeGreaterThan(initialMedianRadius * 0.6);
  expect(horizonMedianRadius).toBeLessThan(initialMedianRadius * 1.4);
  expect(horizonMaximumSystemRadius).toBeLessThan(initialMaximumSystemRadius * 1.6);
  expect(horizonNodeRadii.at(-1).radius, JSON.stringify(horizonNodeRadii))
    .toBeLessThan(initialNodeRadii.at(-1).radius * 1.6);

  const horizonAngularRates = horizon.systems.map(system => Math.abs(system.angularVelocity));
  expect(horizonAngularRates.every(rate => Number.isFinite(rate) && rate > 0.00005)).toBe(true);
  expect(Math.max(...horizonAngularRates) - Math.min(...horizonAngularRates))
    .toBeGreaterThan(0.0001);
  for (const initialSystem of early.systems) {
    const finalSystem = horizon.systems.find(system => system.id === initialSystem.id);
    expect(finalSystem.internalDiameter, finalSystem.id).toBeGreaterThan(8);
    // The new Galaxy default deliberately targets 0.25x relation rest lengths. Preserve a
    // resolved solar system rather than the obsolete pre-tightening 0.5x diameter floor.
    expect(finalSystem.internalDiameter, finalSystem.id).toBeGreaterThan(
      initialSystem.internalDiameter * 0.2,
    );
  }
});

test('reduced-motion Galaxy preserves simultaneous local and black-hole orbits', async ({ page }) => {
  test.setTimeout(45_000);
  // Reduced motion removes camera/paint animation only. The fixed Galaxy solver must still
  // seed physical angular momentum rather than letting every body fall radially inward.
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await openDashboard(page, { query: '?graph-engine=next' });
  await openGraphView(page);
  await page.evaluate(scene => {
    const api = window.__engraphisGraph;
    api.setPreset('galaxy');
    api.setSettings({ gravity: 48 });
    api.setData(scene);
    api.setScope({ showUnlinked: true, minDegree: 0 });
  }, blackHoleGalaxyScene);
  await page.waitForFunction(() => window.__fg.graphData().nodes.length === 8
    && window.__engraphisGraph.physicsDiagnostics().steps >= 5);

  const localAndGlobalPhase = snapshot => Object.fromEntries(['aurora', 'borealis']
    .map(id => {
      const star = snapshot.nodes[`${id}-star`];
      const planet = snapshot.nodes[`${id}-planet`];
      const system = snapshot.systems.find(item => item.id === id);
      return [id, {
        anchor: star.systemAnchorId,
        local: Math.atan2(planet.y - star.y, planet.x - star.x),
        global: system.angle,
      }];
    }));
  const start = await galaxySystemSnapshot(page);
  const startPhase = localAndGlobalPhase(start);
  const targetStep = start.diagnostics.steps + 450;
  // Wait on the solver's fixed-step telemetry, never an elapsed wall-clock delay.
  await page.waitForFunction(target => window.__engraphisGraph.physicsDiagnostics().steps >= target,
    targetStep, { timeout: 30_000 });
  const end = await galaxySystemSnapshot(page);
  const endPhase = localAndGlobalPhase(end);

  expect(start.diagnostics.reducedMotion).toBe(true);
  expect(start.diagnostics.staticLayout).toBe(false);
  expect(start.diagnostics.collapsed).toBe(false);
  expect(end.finite).toBe(true);
  expect(end.diagnostics.steps - start.diagnostics.steps).toBeGreaterThanOrEqual(450);
  for (const id of ['aurora', 'borealis']) {
    expect(startPhase[id].anchor).toBe(`${id}-star`);
    const localTravel = signedAngleDelta(startPhase[id].local, endPhase[id].local);
    const globalTravel = signedAngleDelta(startPhase[id].global, endPhase[id].global);
    expect(Math.abs(localTravel), `${id} local phase`).toBeGreaterThan(0.3);
    expect(Math.abs(globalTravel), `${id} system phase`).toBeGreaterThan(0.25);
  }
});

for (const reducedMotion of [false, true]) {
  const preference = reducedMotion ? 'reduced motion' : 'normal motion';
  test(`served primary dashboard visibly sweeps a planet around its rendered star in ${preference}`,
    async ({ page }, testInfo) => {
      test.setTimeout(45_000);
      await page.emulateMedia({ reducedMotion: reducedMotion ? 'reduce' : 'no-preference' });
      const session = await openDashboard(page, { graphScene: servedLargeGalaxyScene });
      // `openDashboard` installs the real lazy-asset capture on Classic without opening its
      // graph. Navigate to the primary Ledger so its adapter, controls, and asset URL own this
      // acceptance run.
      await page.goto('/');
      await page.locator('.nav-item[data-view="relations"]').click();
      await expect(page.locator('#graph-canvas canvas').first()).toBeAttached({ timeout: 20_000 });
      await page.waitForFunction(() => window.__engraphisGraph && window.__fg
        && window.__fg.graphData().nodes.length === 542
        && window.__engraphisGraph.physicsDiagnostics().steps >= 30
        && window.__engraphisGraph.physicsDiagnostics().active);
      // Let the one-shot fit complete before screen-space sampling, so the measured chord is a
      // painted planetary arc rather than camera animation.
      await page.waitForTimeout(1200);

      const samples = [await renderedStellarSnapshot(page)];
      for (let sample = 0; sample < 13; sample += 1) {
        /* Advance by simulation work, not wall-clock time. Under a busy CI browser, a fixed
           timeout can observe fewer integrator steps and turn a healthy global orbit into a
           false negative even though the local orbit remains correct. */
        const targetSteps = samples.at(-1).diagnostics.steps + 14;
        await page.waitForFunction(step => window.__engraphisGraph
          && window.__engraphisGraph.physicsDiagnostics().steps >= step,
        targetSteps, { timeout: 10_000 });
        samples.push(await renderedStellarSnapshot(page));
      }
      const angleDelta = (from, to) => Math.atan2(Math.sin(to - from), Math.cos(to - from));
      const segments = samples.slice(1).map((sample, index) => ({
        localAngle: angleDelta(samples[index].local.angle, sample.local.angle),
        screenAngle: angleDelta(samples[index].screenLocal.angle, sample.screenLocal.angle),
        globalAngle: angleDelta(samples[index].globalAngle, sample.globalAngle),
        radiusChange: Math.abs(sample.local.radius - samples[index].local.radius)
          / Math.max(1e-9, samples[index].local.radius),
        systemCenterChord: Math.hypot(
          sample.systemCenter.x - samples[index].systemCenter.x,
          sample.systemCenter.y - samples[index].systemCenter.y,
        ),
        screenChord: Math.hypot(
          sample.screenLocal.x - samples[index].screenLocal.x,
          sample.screenLocal.y - samples[index].screenLocal.y,
        ),
      }));
      const localTravel = segments.reduce((sum, segment) => sum + segment.localAngle, 0);
      const screenTravel = segments.reduce((sum, segment) => sum + segment.screenAngle, 0);
      const globalTravel = segments.reduce((sum, segment) => sum + segment.globalAngle, 0);
      const screenChord = segments.reduce((sum, segment) => sum + segment.screenChord, 0);
      const direction = Math.sign(localTravel);
      const coRotatingSegments = segments.filter(segment =>
        Math.sign(segment.localAngle) === direction && Math.abs(segment.localAngle) > 0.01).length;
      const phaseReversals = segments.filter(segment =>
        Math.sign(segment.localAngle) === -direction && Math.abs(segment.localAngle) > 0.01).length;
      const localStepMagnitudes = segments.map(segment => Math.abs(segment.localAngle));
      const localStepMean = localStepMagnitudes.reduce((sum, value) => sum + value, 0)
        / localStepMagnitudes.length;
      const relativeKinetics = samples.map(sample => {
        const reducedMass = sample.star.mass * sample.planet.mass
          / (sample.star.mass + sample.planet.mass);
        return 0.5 * reducedMass * sample.local.relativeSpeed ** 2;
      });
      const before = samples[0], after = samples.at(-1);
      const evidence = {
        preference, sampleStepBudget: 14 * 13,
        assetRequests: fetched(session.requested, '/v2-assets/engraphis-graph.js'),
        before: { anchor: before.anchor, star: before.star, planet: before.planet, local: before.local,
          screenLocal: before.screenLocal, globalAngle: before.globalAngle,
          steps: before.diagnostics.steps, safety: before.safety },
        after: { anchor: after.anchor, star: after.star, planet: after.planet, local: after.local,
          screenLocal: after.screenLocal, globalAngle: after.globalAngle,
          steps: after.diagnostics.steps, safety: after.safety },
        localTravel, screenTravel, globalTravel, screenChord, coRotatingSegments,
        phaseReversals, localStepMagnitudes, localStepMean, relativeKinetics,
        maximumRadiusChange: Math.max(...segments.map(segment => segment.radiusChange)),
        maximumSystemCenterChord: Math.max(...segments.map(segment => segment.systemCenterChord)),
      };
      await testInfo.attach(`visible-stellar-orbit-${reducedMotion ? 'reduced' : 'normal'}.json`, {
        body: Buffer.from(JSON.stringify(evidence, null, 2)),
        contentType: 'application/json',
      });
      testInfo.annotations.push({
        type: 'visible-orbit-evidence', description: JSON.stringify(evidence),
      });

      expect(samples.every(sample => sample.finite && sample.visible), JSON.stringify(evidence))
        .toBe(true);
      expect(samples.every(sample => sample.anchorValid
        && sample.anchor.anchorRole === 'global'
        && sample.globalAnchorCount === 1), JSON.stringify(evidence)).toBe(true);
      expect(samples.every(sample => Number.isFinite(sample.safety.envelope)
        && sample.safety.envelope > 0), JSON.stringify(evidence)).toBe(true);
      expect(Math.min(...samples.map(sample => sample.safety.minimumBlackHoleClearance)),
        JSON.stringify(evidence)).toBeGreaterThanOrEqual(-1e-7);
      expect(Math.min(...samples.map(sample => sample.safety.minimumStellarClearance)),
        JSON.stringify(evidence)).toBeGreaterThanOrEqual(-1e-7);
      expect(Math.min(...samples.map(sample => sample.safety.minimumOuterClearance)),
        JSON.stringify(evidence)).toBeGreaterThanOrEqual(-1e-7);
      expect(Math.max(...samples.map(sample => sample.safety.maximumSpeed)),
        JSON.stringify(evidence)).toBeLessThanOrEqual(48 + 1e-9);
      expect(Math.max(...samples.map(sample => sample.safety.speedCapActivations)),
        JSON.stringify(evidence)).toBe(0);
      expect(before.planet.anchor).toBe(before.star.id);
      expect(samples.every(sample => sample.screenLocal.radius
        > sample.star.screenRadius + sample.planet.screenRadius), JSON.stringify(evidence))
        .toBe(true);
      expect(Math.abs(localTravel), JSON.stringify(evidence)).toBeGreaterThan(0.75);
      expect(Math.abs(screenTravel), JSON.stringify(evidence)).toBeGreaterThan(0.75);
      expect(screenChord, JSON.stringify(evidence)).toBeGreaterThan(15);
      expect(coRotatingSegments, JSON.stringify(evidence)).toBeGreaterThanOrEqual(9);
      expect(phaseReversals, JSON.stringify(evidence)).toBe(0);
      expect(Math.min(...localStepMagnitudes), JSON.stringify(evidence)).toBeGreaterThan(0.025);
      expect(Math.max(...localStepMagnitudes), JSON.stringify(evidence))
        .toBeLessThan(localStepMean * 1.8);
      expect(evidence.maximumRadiusChange, JSON.stringify(evidence)).toBeLessThan(0.04);
      expect(Math.max(...relativeKinetics), JSON.stringify(evidence))
        .toBeLessThan(Math.min(...relativeKinetics) * 2);
      expect(evidence.maximumSystemCenterChord, JSON.stringify(evidence)).toBeLessThan(20);
      expect(Math.max(...samples.map(sample => sample.star.warp)), JSON.stringify(evidence))
        .toBeLessThan(0.01);
      /* Six and a half seconds is sampled on a real wall-clock server, so OS scheduling changes
         the exact step count. A 0.30-radian sweep is already >17 degrees and independently
         visible; the stronger local threshold above proves the nested planet orbit at the same
         time. */
      expect(Math.abs(globalTravel), JSON.stringify(evidence)).toBeGreaterThan(0.30);
      expect(after.local.radius, JSON.stringify(evidence))
        .toBeGreaterThan(before.local.radius * 0.7);
      expect(after.local.radius).toBeLessThan(before.local.radius * 1.3);

      const diagnostics = before.diagnostics;
      expect(diagnostics.reducedMotion).toBe(reducedMotion);
      expect(diagnostics.mode).toBe('galaxy');
      expect(diagnostics.active).toBe(true);
      expect(diagnostics.staticLayout).toBe(false);
      expect(diagnostics.collapsed).toBe(false);
      expect(diagnostics.withinGalaxyLiveLimit).toBe(true);
      expect(diagnostics.renderedNodes).toBe(542);
      expect(before.collapsed).toBe(false);
      expect(before.settings).toMatchObject({
        mode: 'galaxy', frozen: false, gravity: 96, repel: 100, link: 8,
      });
      expect(diagnostics.orbitalSeparationSetting).toBe(100);
      expect(diagnostics.orbitalSeparationPadding).toBe(15);
      expect(diagnostics.orbitalSeparationStrength).toBe(1);
      expect(diagnostics.crossSystemRepulsionStrength).toBe(0);
      expect(diagnostics.linkSetting).toBe(8);
      expect(diagnostics.relationOrbitScale).toBeCloseTo(0.25, 12);
      expect(diagnostics.gravitySetting).toBe(96);
      expect(diagnostics.blackHoleGravity).toBeCloseTo(3230.6848639753507, 12);
      expect(diagnostics.localGravity).toBeCloseTo(240, 12);
      expect(diagnostics.systemOrbitSeedSpeedLimit).toBeCloseTo(23.4, 12);

      const assetRequests = fetched(session.requested, '/v2-assets/engraphis-graph.js');
      expect(assetRequests).toHaveLength(1);
      const assetUrl = new URL(assetRequests[0]);
      expect(assetUrl.searchParams.get('v')).toBe(stellarOrbitAssetVersion);
      const servedAsset = await page.request.get(assetUrl.href);
      expect(servedAsset.ok()).toBe(true);
      const servedSource = await servedAsset.text();
      expect(servedSource).toContain('const GALAXY_STELLAR_ORBIT_CLOCK = 3.25;');
      expect(servedSource).toContain('const GALAXY_AUTHORED_CARRIER_ORBIT_CLOCK = 1.3;');
      expect(servedSource).toContain('const BASE_NODE_RADIUS_SCALE = 1.2;');
      expect(servedSource).toContain('preserveSystemRadii: true,');
      expect(session.pageErrors).toEqual([]);
    });
}

test('served Ledger wires normalized spacetime controls, overlay, and orbit pause',
  async ({ page }) => {
    test.setTimeout(35_000);
    const session = await openDashboard(page, { graphScene: blackHoleGalaxyScene });
    await page.goto('/');
    await page.locator('.nav-item[data-view="relations"]').click();
    await expect(page.locator('#graph-canvas canvas').first()).toBeAttached({ timeout: 20_000 });
    await expect(page.locator('#graph-canvas .graph-spacetime-overlay')).toHaveCount(1);
    await expect(page.locator('#graph-spacetime-tuning')).toBeVisible();
    await page.waitForFunction(() => window.__engraphisGraph
      && window.__engraphisGraph.physicsDiagnostics().active
      && window.__engraphisGraph.physicsDiagnostics().steps >= 5);

    const massSteps = await page.evaluate(() => {
      const massControl = document.getElementById('graph-black-hole-mass');
      const samples = [160, 170, 180].map(value => {
        massControl.value = String(value);
        massControl.dispatchEvent(new Event('input', { bubbles: true }));
        return {
          control: value,
          multiplier: window.__engraphisGraph.state().settings.blackHoleMass,
        };
      });
      const values = {
        'graph-gravitational-constant': '150',
        'graph-local-gravitational-constant': '125',
        'graph-black-hole-mass': '240',
        'graph-space-damping': '2',
        'graph-spring-stiffness': '64',
      };
      Object.entries(values).forEach(([id, value]) => {
        const control = document.getElementById(id);
        control.value = value;
        control.dispatchEvent(new Event('input', { bubbles: true }));
      });
      return samples;
    });
    expect(massSteps).toEqual([
      { control: 160, multiplier: 1 },
      { control: 170, multiplier: 1.2 },
      { control: 180, multiplier: 1.4 },
    ]);
    await expect.poll(() => page.evaluate(() => window.__engraphisGraph.state().settings))
      .toMatchObject({ gravitationalConstant: 4, blackHoleMass: 2.6,
        localGravitationalConstant: 3, damping: 3, springStiffness: 3, orbitPaused: false });
    const rangeResponse = await page.evaluate(() => {
      const set = (id, value) => {
        const control = document.getElementById(id);
        control.value = String(value);
        control.dispatchEvent(new Event('input', { bubbles: true }));
      };
      [
        ['graph-flow-speed', 65],
        ['graph-repel', 150],
        ['graph-link', 20],
        ['graph-gravity', 120],
        ['graph-node-size', 4],
        ['graph-text-size', 16],
        ['graph-line-width', 1],
        ['graph-label-density', 40],
        ['graph-tune-min-degree', 2],
        ['graph-depth', 3],
        ['graph-min-degree', 2],
      ].forEach(([id, value]) => set(id, value));
      const importance = document.getElementById('editor-memory-importance');
      importance.value = '0.75';
      importance.dispatchEvent(new Event('input', { bubbles: true }));
      const state = window.__engraphisGraph.state();
      return {
        settings: state.settings,
        scope: { minDegree: state.minDegree, depth: state.depth },
        importanceAria: importance.getAttribute('aria-valuetext'),
      };
    });
    expect(rangeResponse.settings).toMatchObject({
      flowSpeed: 85, repel: 200, link: 32, gravity: 144, size: 5, font: 20,
      linkw: 1.28, labelDensity: 56,
    });
    expect(rangeResponse.scope).toEqual({ minDegree: 2, depth: 3 });
    expect(rangeResponse.importanceAria).toBe('1.00 importance');
    /* The fixture has no high-degree metadata; restore a visible scope before exercising
       pause/resume so the physics clock is tested with live bodies rather than an empty filter. */
    await page.evaluate(() => {
      ['graph-tune-min-degree', 'graph-min-degree'].forEach(id => {
        const control = document.getElementById(id);
        control.value = '0';
        control.dispatchEvent(new Event('input', { bubbles: true }));
      });
    });
    await page.waitForFunction(() => window.__fg.graphData().nodes.length > 0);

    await page.locator('#graph-orbits-pause').click();
    await page.waitForFunction(() => window.__engraphisGraph.state().settings.orbitPaused === true
      && window.__engraphisGraph.physicsDiagnostics().active === false);
    const pausedPreference = await page.evaluate(() => JSON.parse(localStorage.getItem(
      'engraphis-ledger-graph-preferences-v1') || '{}').spacetimeTuning || {});
    expect(pausedPreference.orbitPaused).toBeUndefined();
    const pausedSteps = await page.evaluate(() => window.__engraphisGraph.physicsDiagnostics().steps);
    await page.waitForTimeout(250);
    expect(await page.evaluate(() => window.__engraphisGraph.physicsDiagnostics().steps))
      .toBe(pausedSteps);
    expect(await page.evaluate(() => window.__engraphisGraph.getPhysicsSnapshot().paused)).toBe(true);

    await page.locator('#graph-orbits-pause').click();
    await page.waitForFunction(steps => !window.__engraphisGraph.state().settings.orbitPaused
      && window.__engraphisGraph.physicsDiagnostics().active
      && window.__engraphisGraph.physicsDiagnostics().steps > steps, pausedSteps);
    /* A legacy snapshot may still contain the formerly persisted pause bit. Fresh navigation
       must ignore it and start the galactic clock, just like the session-only Freeze control. */
    await page.evaluate(() => {
      const key = 'engraphis-ledger-graph-preferences-v1';
      const snapshot = JSON.parse(localStorage.getItem(key) || '{}');
      snapshot.spacetimeTuning = { ...(snapshot.spacetimeTuning || {}), orbitPaused: true };
      localStorage.setItem(key, JSON.stringify(snapshot));
    });
    await page.reload();
    await page.locator('.nav-item[data-view="relations"]').click();
    await page.waitForFunction(() => window.__engraphisGraph
      && window.__engraphisGraph.state().settings.orbitPaused === false
      && window.__engraphisGraph.physicsDiagnostics().active
      && window.__engraphisGraph.physicsDiagnostics().steps > 2, null, { timeout: 25_000 });
    await expect(page.locator('#graph-orbits-pause')).toHaveAttribute('aria-checked', 'false');
    expect(session.pageErrors).toEqual([]);
  });

test('served Galaxy paints complete independent solar envelopes with a visible clearance',
  async ({ page }, testInfo) => {
    test.setTimeout(55_000);
    await page.emulateMedia({ reducedMotion: 'no-preference' });
    await openDashboard(page, { graphScene: servedLargeGalaxyScene });
    await page.goto('/');
    await page.locator('.nav-item[data-view="relations"]').click();
    await expect(page.locator('#graph-canvas canvas').first()).toBeAttached({ timeout: 30_000 });
    await page.waitForFunction(() => window.__engraphisGraph && window.__fg
      && window.__fg.graphData().nodes.length === 542
      && window.__engraphisGraph.physicsDiagnostics().steps >= 12, null, { timeout: 35_000 });
    const paintedIds = await installCarrierPaintAudit(page);
    expect(paintedIds).toHaveLength(61);
    await page.waitForFunction(() => {
      const audit = window.__carrierPaintAudit;
      return audit && audit.ids.every(id => (audit.counts[id] || 0) > 0);
    }, null, { timeout: 20_000 });
    const paintBefore = await carrierPaintAuditSnapshot(page);
    const before = await renderedSystemEnvelopeSnapshot(page);
    const steps = await page.evaluate(() => window.__engraphisGraph.physicsDiagnostics().steps + 96);
    await page.waitForFunction(step => window.__engraphisGraph.physicsDiagnostics().steps >= step,
      steps, { timeout: 25_000 });
    await page.waitForFunction(previous => {
      const audit = window.__carrierPaintAudit;
      return audit && audit.ids.every(id => (audit.counts[id] || 0) > (previous[id] || 0));
    }, paintBefore.counts, { timeout: 20_000 });
    const paintAfter = await carrierPaintAuditSnapshot(page);
    const after = await renderedSystemEnvelopeSnapshot(page);
    await testInfo.attach('served-system-envelope-clearance.json', {
      body: Buffer.from(JSON.stringify({ before, after, paintBefore, paintAfter }, null, 2)),
      contentType: 'application/json',
    });
    for (const snapshot of [before, after]) {
      expect(snapshot.finite).toBe(true);
      expect(snapshot.systems).toHaveLength(60);
      expect(snapshot.systems.every(system => system.members === 9)).toBe(true);
      expect(snapshot.overlaps).toBe(0);
      expect(snapshot.minimumClearance).toBeGreaterThanOrEqual(-.75);
    }
    expect(paintAfter.diagnostics.active).toBe(true);
    expect(paintAfter.diagnostics.frozen).toBe(false);
    expect(paintAfter.diagnostics.orbitPaused).toBe(false);
    expect(paintAfter.hidden).toBe(false);
    for (const id of paintedIds) {
      const first = paintBefore.last[id], last = paintAfter.last[id];
      expect(paintAfter.counts[id], id).toBeGreaterThan(paintBefore.counts[id]);
      expect(first.insideCanvas && last.insideCanvas, id).toBe(true);
      expect(Math.hypot(last.worldX - first.worldX, last.worldY - first.worldY), id)
        .toBeGreaterThan(.1);
      expect(Math.hypot(last.screenX - first.screenX, last.screenY - first.screenY), id)
        .toBeGreaterThan(1);
    }
  });

test('served 500-body Galaxy sustains separated carrier orbits and the black-hole local system',
  async ({ page }, testInfo) => {
    test.setTimeout(95_000);
    const delta = (from, to) => Math.atan2(Math.sin(to - from), Math.cos(to - from));
    const capture = async () => ({
      global: await renderedAllGlobalOrbitSnapshot(page),
      local: await renderedAllLocalOrbitSnapshot(page),
      envelopes: await renderedSystemEnvelopeSnapshot(page),
    });
    const run = async reducedMotion => {
      await page.emulateMedia({ reducedMotion: reducedMotion ? 'reduce' : 'no-preference' });
      await page.evaluate(scene => {
        const api = window.__engraphisGraph;
        api.clearFocus();
        api.setPreset('galaxy');
        api.setScope({ asOf: null, ghost: false, minDegree: 0, showUnlinked: true });
        api.setData(scene);
      }, servedLargeGalaxyWithCoreSatellites);
      await page.waitForFunction(() => window.__engraphisGraph && window.__fg,
        null, { timeout: 20_000 });
      await page.waitForTimeout(900);
      const boot = await page.evaluate(() => ({
        nodes: window.__fg.graphData().nodes.length,
        state: window.__engraphisGraph.state(),
        diagnostics: window.__engraphisGraph.physicsDiagnostics(),
      }));
      expect(boot.nodes, JSON.stringify(boot)).toBe(544);
      expect(boot.diagnostics.active, JSON.stringify(boot)).toBe(true);
      expect(boot.diagnostics.reducedMotion, JSON.stringify(boot)).toBe(reducedMotion);
      expect(boot.diagnostics.steps, JSON.stringify(boot)).toBeGreaterThanOrEqual(5);
      const startedAt = Date.now();
      const samples = [await capture()];
      const initialSteps = samples[0].global.diagnostics.steps;
      // Long enough to catch a radial-only carrier or a delayed collision response, but still
      // bounded: this is the served 542-node release-performance gate.
      for (let index = 1; index <= 9; index += 1) {
        const target = initialSteps + index * 48;
        await page.waitForFunction(step => window.__engraphisGraph.physicsDiagnostics().steps >= step,
          target, { timeout: 30_000 });
        samples.push(await capture());
      }
      const elapsedMs = Date.now() - startedAt;
      const globalMaps = samples.map(sample => new Map(sample.global.members.map(body => [body.id, body])));
      const localMaps = samples.map(sample => new Map(sample.local.members.map(body => [body.id, body])));
      const carriers = [...globalMaps[0].values()].filter(body => body.anchorRole === 'community');
      const carrierEvidence = carriers.map(carrier => {
        const phases = globalMaps.map(map => map.get(carrier.id));
        const steps = phases.slice(1).map((phase, index) => delta(phases[index].angle, phase.angle));
        const start = phases[0], end = phases.at(-1);
        return { id: carrier.id, start, end, steps,
          travel: steps.reduce((sum, value) => sum + value, 0),
          radiusRatio: end.radius / Math.max(1e-9, start.radius) };
      });
      const carrierRings = new Map();
      for (const carrier of carriers) {
        const lane = carrier.carrierLaneRadius;
        expect(lane, `${carrier.id}:carrier lane`).toBeTruthy();
        const key = Number(lane).toFixed(8);
        if (!carrierRings.has(key)) carrierRings.set(key, []);
        carrierRings.get(key).push(carrier.id);
      }
      const sameRingPhaseEvidence = [...carrierRings.entries()]
        .filter(([, ids]) => ids.length > 1).map(([lane, ids]) => {
          const referenceId = ids[0];
          const differences = ids.slice(1).map(id => ({ id, samples: globalMaps.map(map =>
            delta(map.get(referenceId).angle, map.get(id).angle)) }));
          return { lane: Number(lane), referenceId, ids, differences };
        });
      const coreSatellites = [...localMaps[0].values()].filter(member =>
        member.anchorId === 'black-hole');
      const coreEvidence = coreSatellites.map(satellite => {
        const phases = localMaps.map(map => map.get(satellite.id));
        const steps = phases.slice(1).map((phase, index) => delta(phases[index].angle, phase.angle));
        const start = phases[0], end = phases.at(-1);
        return { id: satellite.id, communityId: satellite.communityId, phases, start, end, steps,
          travel: steps.reduce((sum, value) => sum + value, 0),
          radiusRatio: end.radius / Math.max(1e-9, start.radius) };
      });
      const core = coreEvidence.find(member => member.id === 'core-star');
      const corePairClearances = [];
      for (let index = 0; index < samples.length; index += 1) {
        for (let left = 0; left < coreEvidence.length; left += 1) for (let right = left + 1;
          right < coreEvidence.length; right += 1) {
          const a = coreEvidence[left].phases[index], b = coreEvidence[right].phases[index];
          const distance = Math.sqrt(a.radius ** 2 + b.radius ** 2
            - 2 * a.radius * b.radius * Math.cos(delta(a.angle, b.angle)));
          corePairClearances.push({ sample: index, ids: [a.id, b.id],
            clearance: distance - a.paintedRadius - b.paintedRadius });
        }
      }
      const evidence = { reducedMotion, elapsedMs, initialSteps, samples, carrierEvidence,
        sameRingPhaseEvidence, coreEvidence, corePairClearances };
      await testInfo.attach(`sustained-carrier-orbits-${reducedMotion ? 'reduced' : 'normal'}.json`, {
        body: Buffer.from(JSON.stringify(evidence, null, 2)), contentType: 'application/json',
      });

      expect(elapsedMs, '542-body sampled carrier integration').toBeLessThan(35_000);
      expect(samples.at(-1).global.diagnostics.steps - initialSteps).toBeGreaterThanOrEqual(432);
      expect(samples.every(sample => sample.global.finite && sample.local.finite && sample.envelopes.finite))
        .toBe(true);
      expect(samples.every(sample => sample.global.members.length === 543)).toBe(true);
      const visibilityDebug = samples.map(sample => {
        const invisible = new Set(sample.envelopes.systems.filter(system => !system.visible)
          .map(system => system.id));
        const worstIds = new Set(sample.envelopes.worstPair?.ids || []);
        return { steps: sample.global.diagnostics.steps,
          packing: sample.global.diagnostics.systemPacking,
          support: sample.global.diagnostics.carrierOrbitSupport,
          overlaps: sample.envelopes.overlaps,
          minimumClearance: sample.envelopes.minimumClearance,
          worstPair: sample.envelopes.worstPair,
          worstBodies: sample.global.members.filter(body => worstIds.has(body.id)),
          invisible: [...invisible], carriers: sample.global.members.filter(body =>
            invisible.has(String(body.id))).map(body => ({
            id: body.id, radius: body.radius, angle: body.angle, tangent: body.tangent,
            lane: body.carrierLaneRadius,
          })) };
      });
      expect(samples.every(sample => sample.envelopes.systems.length === 60
        && sample.envelopes.systems.every(system => system.visible)
        && sample.envelopes.overlaps === 0 && sample.envelopes.minimumClearance >= -.75),
      JSON.stringify(visibilityDebug))
        .toBe(true);
      expect(samples.every(sample => sample.global.diagnostics.speedCapActivations === 0
        && sample.global.diagnostics.lastCollisions === 0)).toBe(true);
      // Admission may project a bad initial fixture once; sustained clean lanes must then
      // require no packing corrections, otherwise the user sees periodic popping.
      expect(samples.slice(1).every(sample => {
        const packing = sample.global.diagnostics.systemPacking || {};
        return Number(packing.adjustedSystems || 0) === 0
          && Number(packing.correctionDistance || 0) <= 1e-9
          && Number(packing.remainingOverlaps || 0) === 0;
      })).toBe(true);
      expect(carrierEvidence).toHaveLength(60);
      expect(sameRingPhaseEvidence.length).toBeGreaterThan(0);
      for (const carrier of carrierEvidence) {
        expect(Math.abs(carrier.start.tangent), carrier.id).toBeGreaterThan(1e-5);
        expect(Math.abs(carrier.end.tangent), carrier.id).toBeGreaterThan(1e-5);
        expect(Math.abs(carrier.travel), carrier.id).toBeGreaterThan(.08);
        expect(carrier.steps.every(step => Math.abs(step) > 1e-5), carrier.id).toBe(true);
        expect(carrier.steps.every(step => Math.sign(step) === Math.sign(carrier.steps[0])), carrier.id)
          .toBe(true);
        // A carrier's named lane is the authoritative Keplerian orbit.  It cannot silently
        // spiral inward/outward and rely on later packing to repair its position.
        expect(carrier.start.radius, carrier.id).toBeCloseTo(carrier.start.carrierLaneRadius, 5);
        expect(carrier.end.radius, carrier.id).toBeCloseTo(carrier.start.carrierLaneRadius, 5);
        expect(carrier.radiusRatio, carrier.id).toBeCloseTo(1, 5);
      }
      for (const ring of sameRingPhaseEvidence) for (const difference of ring.differences) {
        expect(difference.samples.every(value => Math.abs(delta(difference.samples[0], value)) < 1e-5),
          `${ring.lane}:${ring.referenceId}:${difference.id}`).toBe(true);
      }
      expect(coreEvidence.map(member => member.id).sort()).toEqual([
        'core-star', 'core-star-inner', 'core-star-outer',
      ]);
      expect(coreEvidence.find(member => member.id === 'core-star-inner').communityId).toBe('cross-core');
      for (const coreMember of coreEvidence) {
        expect(Math.abs(coreMember.start.tangent), coreMember.id).toBeGreaterThan(1e-5);
        expect(Math.abs(coreMember.end.tangent), coreMember.id).toBeGreaterThan(1e-5);
        expect(Math.abs(coreMember.travel), coreMember.id).toBeGreaterThan(.25);
        expect(coreMember.steps.every(step => Math.abs(step) > 1e-5
          && Math.sign(step) === Math.sign(coreMember.steps[0])), coreMember.id).toBe(true);
        expect(coreMember.radiusRatio, coreMember.id).toBeGreaterThan(.9);
        expect(coreMember.radiusRatio, coreMember.id).toBeLessThan(1.1);
      }
      expect(corePairClearances).toHaveLength(30);
      expect(corePairClearances.every(pair => pair.clearance >= -1e-7),
        JSON.stringify(corePairClearances)).toBe(true);
      return { carrierEvidence, coreEvidence, elapsedMs };
    };

    await openDashboard(page, { graphScene: servedLargeGalaxyWithCoreSatellites });
    await page.goto('/');
    await page.locator('.nav-item[data-view="relations"]').click();
    await expect(page.locator('#graph-canvas canvas').first()).toBeAttached({ timeout: 20_000 });
    const normal = await run(false);
    const reduced = await run(true);
    // Motion preference changes paint transitions only. It must not change the carrier set,
    // rotation direction, or make the black-hole's own satellite frozen.
    expect(reduced.carrierEvidence.map(value => value.id)).toEqual(normal.carrierEvidence.map(value => value.id));
    expect(reduced.coreEvidence.map(value => value.id)).toEqual(normal.coreEvidence.map(value => value.id));
    for (let index = 0; index < normal.coreEvidence.length; index += 1) {
      expect(Math.sign(reduced.coreEvidence[index].travel)).toBe(Math.sign(normal.coreEvidence[index].travel));
    }
  });

test('served Complete Galaxy uses the lightweight all-body orbit path instead of a frozen layout',
  async ({ page }, testInfo) => {
    test.setTimeout(90_000);
    await page.emulateMedia({ reducedMotion: 'no-preference' });
    await openDashboard(page, { graphScene: servedCompleteGalaxyScene });
    await page.goto('/');
    await page.locator('.nav-item[data-view="relations"]').click();
    await expect(page.locator('#graph-canvas canvas').first()).toBeAttached({ timeout: 30_000 });
    await page.waitForFunction(() => window.__engraphisGraph && window.__fg
      && window.__fg.graphData().nodes.length === 3336
      && window.__engraphisGraph.physicsDiagnostics().steps >= 10
      && window.__engraphisGraph.physicsDiagnostics().active, null, { timeout: 35_000 });
    const paintedCarrierIds = await installCarrierPaintAudit(page);
    expect(paintedCarrierIds).toHaveLength(375);
    await page.waitForFunction(() => {
      const audit = window.__carrierPaintAudit;
      return audit && audit.ids.every(id => (audit.counts[id] || 0) > 0);
    }, null, { timeout: 20_000 });
    const carrierPaintBefore = await carrierPaintAuditSnapshot(page);
    const paintedStep = await page.evaluate(() =>
      window.__engraphisGraph.physicsDiagnostics().steps + 24);
    await page.waitForFunction(step => window.__engraphisGraph.physicsDiagnostics().steps >= step,
      paintedStep, { timeout: 25_000 });
    await page.waitForFunction(previous => {
      const audit = window.__carrierPaintAudit;
      return audit && audit.ids.every(id => (audit.counts[id] || 0) > (previous[id] || 0));
    }, carrierPaintBefore.counts, { timeout: 20_000 });
    const carrierPaintAfter = await carrierPaintAuditSnapshot(page);
    await testInfo.attach('complete-galaxy-carrier-paint-audit.json', {
      body: Buffer.from(JSON.stringify({ carrierPaintBefore, carrierPaintAfter }, null, 2)),
      contentType: 'application/json',
    });
    for (const id of paintedCarrierIds) {
      const first = carrierPaintBefore.last[id], last = carrierPaintAfter.last[id];
      expect(carrierPaintAfter.counts[id], id)
        .toBeGreaterThan(carrierPaintBefore.counts[id]);
      expect(first.insideCanvas && last.insideCanvas, id).toBe(true);
      expect(Math.hypot(last.worldX - first.worldX, last.worldY - first.worldY), id)
        .toBeGreaterThan(.05);
      expect(Math.hypot(last.screenX - first.screenX, last.screenY - first.screenY), id)
        .toBeGreaterThan(.2);
    }

    const orbitEvidence = async label => {
      /* Keep the 3,335 global and 2,960 local bodies in the page. Serializing six full object
         arrays dominated this test, but reducing the sample would make a frozen member invisible.
         The observer scans every body on every phase and returns only counts, extrema, and first
         failures to Playwright. */
      const boot = await page.evaluate(() => {
        const graph = window.__fg, engine = window.__engraphisGraph;
        const delta = (from, to) => Math.atan2(Math.sin(to - from), Math.cos(to - from));
        const snapshot = () => {
          const rendered = graph.graphData().nodes;
          const nodes = rendered.filter(node => !node.ghost);
          const byId = new Map(nodes.map(node => [String(node.id), node]));
          const anchor = nodes.find(node => node.anchor_role === 'global');
          const global = new Map(), local = new Map();
          const finite = node => [node.x, node.y, node.vx, node.vy].every(Number.isFinite);
          if (anchor) for (const node of nodes) if (node !== anchor) {
            global.set(String(node.id), { angle: Math.atan2(node.y - anchor.y, node.x - anchor.x),
              finite: finite(node) });
          }
          for (const node of nodes) {
            const parentId = node.system_anchor_id == null ? null : String(node.system_anchor_id);
            const parent = parentId && byId.get(parentId);
            if (!parent || parent === node) continue;
            local.set(String(node.id), { angle: Math.atan2(node.y - parent.y, node.x - parent.x),
              finite: finite(node) && finite(parent) });
          }
          const carriers = anchor ? nodes.filter(node => node.anchor_role === 'community').map(node => {
            const orbit = node.__galaxyKinematicGlobalOrbit;
            const valid = orbit && Number.isFinite(orbit.angle) && Number.isFinite(orbit.radius);
            const expectedX = valid ? anchor.x + Math.cos(orbit.angle) * orbit.radius : NaN;
            const expectedY = valid ? anchor.y + Math.sin(orbit.angle) * orbit.radius : NaN;
            return { id: String(node.id), valid, error: valid ? Math.hypot(node.x - expectedX, node.y - expectedY) : Infinity };
          }) : [];
          return { global, local, carriers, systems: new Set(nodes.filter(node => node.anchor_role === 'community').map(node => String(node.id))),
            finite: nodes.every(finite), diagnostics: engine.physicsDiagnostics() };
        };
        const initial = snapshot();
        window.__completeOrbitObserver = { delta, snapshot, initial,
          global: new Map([...initial.global.keys()].map(id => [id, { travel: 0, frozen: 0 }])),
          local: new Map([...initial.local.keys()].map(id => [id, { travel: 0, frozen: 0 }])),
          samples: [] };
        return { globalCount: initial.global.size, localCount: initial.local.size,
          anchorCount: initial.carriers.length, systemCount: initial.systems.size,
          finite: initial.finite, diagnostics: initial.diagnostics };
      });
      // This deliberately samples short fixed intervals. A production-sized canvas can paint
      // at a modest cadence, so two eight-step intervals prove incremental movement without
      // turning a release gate into a minute-long serialization benchmark.
      const phases = [];
      for (let index = 1; index <= 2; index += 1) {
        const target = boot.diagnostics.steps + index * 8;
        await page.waitForFunction(step => window.__engraphisGraph.physicsDiagnostics().steps >= step,
          target, { timeout: 25_000 });
        phases.push(await page.evaluate(() => {
          const observer = window.__completeOrbitObserver, current = observer.snapshot();
          const previous = observer.samples.length ? observer.samples.at(-1) : observer.initial;
          const check = (kind, tracked) => {
            const before = previous[kind], now = current[kind], totals = observer[kind];
            let missing = 0, nonFinite = 0, frozen = 0, first = null;
            for (const [id, state] of totals) {
              const prior = before.get(id), next = now.get(id);
              if (!prior || !next) { missing++; if (!first) first = { id, reason: 'missing' }; continue; }
              if (!next.finite) { nonFinite++; if (!first) first = { id, reason: 'non-finite' }; continue; }
              const step = observer.delta(prior.angle, next.angle);
              state.travel += step;
              if (Math.abs(step) <= 1e-8) { state.frozen++; frozen++; if (!first) first = { id, reason: 'frozen' }; }
            }
            let minTravel = Infinity, totalFrozen = 0;
            for (const state of totals.values()) {
              minTravel = Math.min(minTravel, Math.abs(state.travel)); totalFrozen += state.frozen;
            }
            return { count: now.size, missing, nonFinite, frozen, totalFrozen, minTravel, first };
          };
          const global = check('global', observer.global), local = check('local', observer.local);
          const carrierFailures = current.carriers.filter(carrier => !carrier.valid || carrier.error >= 1e-8);
          const summary = { global, local, carrierCount: current.carriers.length,
            carrierMaxError: current.carriers.reduce((max, carrier) => Math.max(max, carrier.error), 0),
            carrierFailures: carrierFailures.slice(0, 3), systemCount: current.systems.size,
            finite: current.finite, diagnostics: current.diagnostics };
          observer.samples.push(current);
          return summary;
        }));
      }
      const after = phases.at(-1);
      await testInfo.attach(`complete-galaxy-${label}.json`, {
        body: Buffer.from(JSON.stringify({ boot, phases }, null, 2)),
        contentType: 'application/json',
      });
      expect(boot.globalCount).toBe(3335);
      expect(boot.localCount).toBe(2960);
      expect(boot.anchorCount).toBe(375);
      expect(boot.systemCount).toBe(375);
      expect(boot.finite && phases.every(phase => phase.finite)).toBe(true);
      expect(after.diagnostics.steps - boot.diagnostics.steps).toBeGreaterThanOrEqual(16);
      expect(after.diagnostics.kinematicSteps - boot.diagnostics.kinematicSteps)
        .toBeGreaterThanOrEqual(16);
      expect(after.diagnostics.reheatStepsApplied).toBe(0);
      expect(after.diagnostics.reheatStepsRemaining).toBe(0);
      expect(after.diagnostics.speedCapActivations).toBe(0);
      expect(after.diagnostics.lastCollisions).toBe(0);
      expect(after.diagnostics.lastRelationCorrections).toBe(0);
      expect(phases.every(phase => phase.global.count === 3335 && phase.global.missing === 0
        && phase.global.nonFinite === 0 && phase.global.frozen === 0 && phase.global.totalFrozen === 0
        && phase.global.minTravel > .001), JSON.stringify(phases.map(phase => phase.global))).toBe(true);
      expect(phases.every(phase => phase.local.count === 2960 && phase.local.missing === 0
        && phase.local.nonFinite === 0 && phase.local.frozen === 0 && phase.local.totalFrozen === 0
        && phase.local.minTravel > .001), JSON.stringify(phases.map(phase => phase.local))).toBe(true);
      expect(phases.every(phase => phase.carrierCount === 375 && phase.systemCount === 375
        && phase.carrierFailures.length === 0 && phase.carrierMaxError < 1e-8),
      JSON.stringify(phases.map(phase => ({ count: phase.carrierCount, error: phase.carrierMaxError,
        failures: phase.carrierFailures })))).toBe(true);
      return after;
    };

    const normal = await orbitEvidence('normal');
    expect(normal.diagnostics.staticLayout).toBe(true);
    expect(normal.diagnostics.collapsed).toBe(false);
    expect(normal.diagnostics.oversizedKinematic).toBe(true);
    expect(normal.diagnostics.withinGalaxyLiveLimit).toBe(false);
    expect(normal.diagnostics.reducedMotion).toBe(false);

    // The same physical scene must continue under reduced visual motion; accessibility only
    // suppresses camera/paint transitions, never the kinematic Galaxy clock.
    await page.emulateMedia({ reducedMotion: 'reduce' });
    const reduced = await orbitEvidence('reduced');
    expect(reduced.diagnostics.reducedMotion).toBe(true);

    const frozen = await page.evaluate(() => {
      const api = window.__engraphisGraph;
      api.freeze(true);
      const nodes = window.__fg.graphData().nodes;
      return { steps: api.physicsDiagnostics().steps,
        coordinates: nodes.map(node => [node.id, node.x, node.y, node.vx, node.vy]) };
    });
    await page.waitForTimeout(450);
    const frozenAfter = await page.evaluate(before => ({
      diagnostics: window.__engraphisGraph.physicsDiagnostics(),
      same: window.__fg.graphData().nodes.every((node, index) =>
        [node.id, node.x, node.y, node.vx, node.vy].every((value, field) => value === before[index][field])),
    }), frozen.coordinates);
    expect(frozenAfter.diagnostics.steps).toBe(frozen.steps);
    expect(frozenAfter.same).toBe(true);

    await page.evaluate(() => window.__engraphisGraph.freeze(false));
    await page.waitForFunction(step => window.__engraphisGraph.physicsDiagnostics().steps > step,
      frozen.steps, { timeout: 20_000 });
    const hidden = await page.evaluate(() => {
      Object.defineProperty(document, 'hidden', { configurable: true, value: true });
      document.dispatchEvent(new Event('visibilitychange'));
      return window.__engraphisGraph.physicsDiagnostics().steps;
    });
    await page.waitForTimeout(450);
    const hiddenAfter = await page.evaluate(() => window.__engraphisGraph.physicsDiagnostics());
    expect(hiddenAfter.hidden).toBe(true);
    expect(hiddenAfter.steps).toBe(hidden);
    await page.evaluate(() => {
      Object.defineProperty(document, 'hidden', { configurable: true, value: false });
      document.dispatchEvent(new Event('visibilitychange'));
    });
    await page.waitForFunction(step => window.__engraphisGraph.physicsDiagnostics().steps > step,
      hidden, { timeout: 20_000 });
  });

for (const reducedMotion of [false, true]) {
  const preference = reducedMotion ? 'reduced motion' : 'normal motion';
  test(`served Galaxy keeps every local member orbiting its authored parent in ${preference}`,
    async ({ page }, testInfo) => {
      test.setTimeout(90_000);
      await page.emulateMedia({ reducedMotion: reducedMotion ? 'reduce' : 'no-preference' });
      await openDashboard(page, { graphScene: servedLargeGalaxyScene });
      await page.goto('/');
      await page.locator('.nav-item[data-view="relations"]').click();
      await expect(page.locator('#graph-canvas canvas').first()).toBeAttached({ timeout: 20_000 });
      await page.waitForFunction(() => window.__engraphisGraph && window.__fg
        && window.__fg.graphData().nodes.length === 542
        && window.__engraphisGraph.physicsDiagnostics().steps >= 30
        && window.__engraphisGraph.physicsDiagnostics().active);

      const before = await renderedAllLocalOrbitSnapshot(page);
      // Principal-angle endpoints alias after a real orbit crosses π. Sample 14 short fixed-step
      // intervals and accumulate their normalized deltas, so a full coherent revolution cannot
      // be mistaken for a reversal merely because its final angle wraps across -π/π.
      const samples = [before];
      const stepStride = 30;
      for (let index = 1; index <= 14; index += 1) {
        const target = before.diagnostics.steps + index * stepStride;
        await page.waitForFunction(step => window.__engraphisGraph.physicsDiagnostics().steps >= step,
          target, { timeout: 35_000 });
        samples.push(await renderedAllLocalOrbitSnapshot(page));
      }
      const after = samples.at(-1);
      const sampleMaps = samples.map(snapshot => new Map(snapshot.members.map(member =>
        [member.id, member])));
      const beforeById = sampleMaps[0];
      const delta = (from, to) => Math.atan2(Math.sin(to - from), Math.cos(to - from));
      const evidence = after.members.map(member => {
        const start = beforeById.get(member.id);
        const phaseSteps = sampleMaps.slice(1).map((map, index) => {
          const previous = sampleMaps[index].get(member.id), current = map.get(member.id);
          return delta(previous.angle, current.angle);
        });
        const localTravel = phaseSteps.reduce((sum, step) => sum + step, 0);
        return { id: member.id, anchorId: member.anchorId, start, end: member,
          localTravel, phaseSteps,
          reversals: start ? phaseSteps.filter(step => Math.sign(step) === -Math.sign(start.tangent)
            && Math.abs(step) > .002).length : null,
          frozenSteps: phaseSteps.filter(step => Math.abs(step) < 1e-5).length,
          radiusRatio: start ? member.radius / Math.max(1e-9, start.radius) : null,
        };
      });
      await testInfo.attach(`all-stellar-members-${reducedMotion ? 'reduced' : 'normal'}.json`, {
        body: Buffer.from(JSON.stringify({ before, after, evidence }, null, 2)),
        contentType: 'application/json',
      });

      // 60 systems × (7 planets + 1 nested moon) + the core black-hole satellite: neither
      // hierarchy level may be omitted. Keep this exact count so filtering cannot make the
      // assertion vacuous.
      expect(before.members).toHaveLength(481);
      expect(after.members).toHaveLength(481);
      expect(before.finite && after.finite).toBe(true);
      expect(before.diagnostics.reducedMotion).toBe(reducedMotion);
      expect(after.diagnostics.steps - before.diagnostics.steps).toBeGreaterThanOrEqual(420);
      expect(after.diagnostics.reheatStepsApplied).toBe(0);
      expect(after.diagnostics.reheatStepsRemaining).toBe(0);
      expect(after.diagnostics.speedCapActivations).toBe(0);
      for (const sample of evidence) {
        expect(sample.start, sample.id).toBeTruthy();
        expect(sample.end.finite, sample.id).toBe(true);
        expect(sample.end.anchorId, sample.id).toBe(sample.start.anchorId);
        expect(Math.abs(sample.start.tangent), sample.id).toBeGreaterThan(1e-5);
        expect(Math.abs(sample.end.tangent), sample.id).toBeGreaterThan(1e-5);
        expect(sample.start.clearance, sample.id).toBeGreaterThanOrEqual(-1e-7);
        expect(sample.end.clearance, sample.id).toBeGreaterThanOrEqual(-1e-7);
        expect(Math.abs(sample.localTravel), sample.id).toBeGreaterThan(0.25);
        expect(Math.sign(sample.localTravel), sample.id).toBe(Math.sign(sample.start.tangent));
        expect(sample.reversals, sample.id).toBe(0);
        expect(sample.frozenSteps, sample.id).toBe(0);
        expect(sample.radiusRatio, sample.id).toBeGreaterThan(0.9);
        expect(sample.radiusRatio, sample.id).toBeLessThan(1.12);
      }
    });
}

for (const reducedMotion of [false, true]) {
  const preference = reducedMotion ? 'reduced motion' : 'normal motion';
  test(`served Galaxy advances every rendered body in the black-hole frame through lifecycle states (${preference})`,
    async ({ page }, testInfo) => {
      test.setTimeout(85_000);
      await page.emulateMedia({ reducedMotion: reducedMotion ? 'reduce' : 'no-preference' });
      await openDashboard(page, { graphScene: blackHoleGalaxyScene });
      await page.goto('/');
      await page.locator('.nav-item[data-view="relations"]').click();
      await expect(page.locator('#graph-canvas canvas').first()).toBeAttached({ timeout: 20_000 });

      /* One scene deliberately contains the awkward lifecycle cases that normally escape a
         sampled orbit test: an unlinked singleton, an explicit historical ghost, a late
         system, and later a newly-revealed system whose server velocity is exactly zero. */
      const scenario = JSON.parse(JSON.stringify(blackHoleGalaxyScene));
      scenario.nodes.push({
        id: 'singleton-star', label: 'Singleton', gravity_mass: 7, visual_radius: 5,
        community_id: 'singleton', anchor_role: 'community', system_anchor_id: 'singleton-star',
        orbit_tier: 0, galactic_radius: 206, galactic_target_radius: 206,
        galactic_phase: 0.71, x: 156, y: 134, vx: 0, vy: 0,
      }, {
        id: 'history-ghost', label: 'Historical test particle', gravity_mass: 0, visual_radius: 3,
        community_id: 'history', anchor_role: 'none', system_anchor_id: 'black-hole',
        orbit_tier: 1, galactic_radius: 142, galactic_target_radius: 142,
        galactic_phase: -1.1, x: 61, y: -128, vx: 0, vy: 0,
        ghost: true, valid_from: 1, valid_to: 900,
      });
      scenario.communities.push({ id: 'singleton', mass: 7, member_count: 1,
        anchor_id: 'singleton-star', galactic_radius: 206, galactic_target_radius: 206 });

      const lateScene = JSON.parse(JSON.stringify(scenario));
      lateScene.nodes.push({
        id: 'late-star', label: 'Late star', gravity_mass: 8, visual_radius: 5,
        community_id: 'late', anchor_role: 'community', system_anchor_id: 'late-star',
        orbit_tier: 0, galactic_radius: 224, galactic_target_radius: 224,
        galactic_phase: 2.2, x: -132, y: 181, vx: 0, vy: 0,
      }, {
        id: 'late-planet', label: 'Late planet', gravity_mass: 1, visual_radius: 3,
        community_id: 'late', anchor_role: 'none', system_anchor_id: 'late-star',
        orbit_tier: 1, orbit_radius: 28, galactic_radius: 224, galactic_target_radius: 224,
        galactic_phase: 2.2, x: -151, y: 201, vx: 0, vy: 0,
      });
      lateScene.edges.push({ id: 'late-orbit', source: 'late-star', target: 'late-planet',
        relation: 'orbits', rest_length: 28, spring_strength: 0.08 });
      lateScene.communities.push({ id: 'late', mass: 9, member_count: 2,
        anchor_id: 'late-star', galactic_radius: 224, galactic_target_radius: 224 });

      const revivedScene = JSON.parse(JSON.stringify(lateScene));
      revivedScene.nodes.push({
        id: 'revived-star', label: 'Revived zero-velocity star', gravity_mass: 9, visual_radius: 5,
        community_id: 'revived', anchor_role: 'community', system_anchor_id: 'revived-star',
        orbit_tier: 0, galactic_radius: 244, galactic_target_radius: 244,
        galactic_phase: -2.18, x: -140, y: -194, vx: 0, vy: 0,
      }, {
        id: 'revived-planet', label: 'Revived zero-velocity planet', gravity_mass: 1, visual_radius: 3,
        community_id: 'revived', anchor_role: 'none', system_anchor_id: 'revived-star',
        orbit_tier: 1, orbit_radius: 31, galactic_radius: 244, galactic_target_radius: 244,
        galactic_phase: -2.18, x: -165, y: -210, vx: 0, vy: 0,
      });
      revivedScene.edges.push({ id: 'revived-orbit', source: 'revived-star', target: 'revived-planet',
        relation: 'orbits', rest_length: 31, spring_strength: 0.08 });
      revivedScene.communities.push({ id: 'revived', mass: 10, member_count: 2,
        anchor_id: 'revived-star', galactic_radius: 244, galactic_target_radius: 244 });

      const delta = (from, to) => Math.atan2(Math.sin(to - from), Math.cos(to - from));
      const phases = async label => {
        await page.waitForFunction(() => window.__engraphisGraph && window.__fg
          && window.__engraphisGraph.physicsDiagnostics().active
          && window.__engraphisGraph.physicsDiagnostics().steps >= 10, null, { timeout: 25_000 });
        const before = await renderedAllGlobalOrbitSnapshot(page);
        const samples = [before];
        for (let index = 1; index <= 4; index += 1) {
          await page.waitForFunction(target => window.__engraphisGraph.physicsDiagnostics().steps >= target,
            before.diagnostics.steps + index * 24, { timeout: 20_000 });
          samples.push(await renderedAllGlobalOrbitSnapshot(page));
        }
        const maps = samples.map(snapshot => new Map(snapshot.members.map(member => [member.id, member])));
        const systemMaps = samples.map(snapshot => new Map(snapshot.systems.map(system => [system.id, system])));
        const evidence = [...maps[0].values()].map(member => {
          const steps = maps.slice(1).map((map, index) => delta(maps[index].get(member.id).angle,
            map.get(member.id).angle));
          return { id: member.id, ghost: before.ghostIds.includes(member.id), start: member,
            travel: steps.reduce((sum, value) => sum + value, 0), steps };
        });
        const systems = [...systemMaps[0].values()].map(system => {
          const steps = systemMaps.slice(1).map((map, index) => delta(
            systemMaps[index].get(system.id).angle, map.get(system.id).angle));
          return { id: system.id, travel: steps.reduce((sum, value) => sum + value, 0), steps };
        });
        const after = samples.at(-1);
        await testInfo.attach(`all-global-${label}-${reducedMotion ? 'reduced' : 'normal'}.json`, {
          body: Buffer.from(JSON.stringify({ before, after, evidence, systems }, null, 2)),
          contentType: 'application/json',
        });
        expect(before.anchor, label).toBe('black-hole');
        expect(before.finite && after.finite, label).toBe(true);
        expect(after.diagnostics.reducedMotion, label).toBe(reducedMotion);
        expect(after.diagnostics.reheatStepsApplied, label).toBe(0);
        expect(after.diagnostics.reheatStepsRemaining, label).toBe(0);
        expect(after.diagnostics.speedCapActivations, label).toBe(0);
        expect(evidence.length, label).toBeGreaterThan(0);
        for (const body of evidence) {
          expect(Math.abs(body.travel), `${label}:${body.id}`).toBeGreaterThan(0.01);
          expect(body.steps.every(value => Math.abs(value) > 1e-7), `${label}:${body.id}`)
            .toBe(true);
        }
        for (const system of systems) {
          expect(Math.abs(system.travel), `${label}:system:${system.id}`).toBeGreaterThan(0.01);
          expect(system.steps.every(value => Math.abs(value) > 1e-7), `${label}:system:${system.id}`)
            .toBe(true);
        }
        return { before, after, evidence, systems };
      };
      const setScene = async (scene, scope = {}) => {
        await page.evaluate(({ next, patch }) => {
          const api = window.__engraphisGraph;
          api.clearFocus();
          api.setScope({ asOf: null, ghost: false, minDegree: 0, showUnlinked: true, ...patch });
          api.setPreset('galaxy');
          api.setData(next);
        }, { next: scene, patch: scope });
      };

      await setScene(scenario);
      const initial = await phases('initial-and-singleton');
      expect(initial.evidence.find(body => body.id === 'singleton-star')).toBeTruthy();

      await setScene(lateScene);
      const late = await phases('late-system');
      expect(late.evidence.find(body => body.id === 'late-star')).toBeTruthy();
      expect(late.evidence.find(body => body.id === 'late-planet')).toBeTruthy();

      await page.evaluate(() => window.__engraphisGraph.focus('black-hole'));
      const focused = await phases('focus');
      expect(focused.evidence.map(body => body.id)).toEqual(['core-star']);

      await page.evaluate(() => {
        const api = window.__engraphisGraph;
        api.clearFocus();
        api.setScope({ minDegree: 1, showUnlinked: false, asOf: null, ghost: false });
      });
      const filtered = await phases('filtered');
      expect(filtered.evidence.find(body => body.id === 'singleton-star')).toBeFalsy();
      expect(filtered.evidence.find(body => body.id === 'late-star')).toBeTruthy();

      await page.evaluate(next => {
        const api = window.__engraphisGraph;
        api.setData(next);
        api.setScope({ minDegree: 0, showUnlinked: true, asOf: 1000, ghost: true });
      }, lateScene);
      const temporal = await phases('time-view-with-ghost');
      const ghost = temporal.evidence.find(body => body.id === 'history-ghost');
      expect(temporal.before.ghostIds).toContain('history-ghost');
      expect(ghost).toBeTruthy();
      expect(ghost.start.ghost).toBe(true);

      await setScene(revivedScene);
      const revived = await phases('zeroed-tagged-late-system');
      expect(revived.evidence.find(body => body.id === 'revived-star')).toBeTruthy();
      expect(revived.evidence.find(body => body.id === 'revived-planet')).toBeTruthy();
    });
}

test('served primary dashboard keeps local stellar orbits independent at Galaxy-zero',
  async ({ page }, testInfo) => {
    test.setTimeout(45_000);
    await page.emulateMedia({ reducedMotion: 'no-preference' });
    const gravityZeroScene = {
      ...blackHoleGalaxyScene,
      nodes: blackHoleGalaxyScene.nodes.filter(node =>
        ['black-hole', 'aurora-star', 'aurora-planet'].includes(node.id)).map(node =>
        node.community_id === 'aurora' ? {
          ...node, x: node.x + 120, galactic_radius: 192,
          galactic_target_radius: 192,
        } : node),
      edges: blackHoleGalaxyScene.edges.filter(edge => edge.id === 'aurora-orbit'),
      communities: blackHoleGalaxyScene.communities.filter(community =>
        ['core', 'aurora'].includes(community.id)),
      meta: { ...blackHoleGalaxyScene.meta, total_nodes: 3 },
    };
    const session = await openDashboard(page, { graphScene: gravityZeroScene });
    await page.goto('/');
    // Set the real dashboard control before lazy graph construction. This proves the adapter
    // passes zero into both the one-shot seed and every subsequent served-engine step.
    await page.locator('#graph-gravity').evaluate(control => {
      control.value = '0';
      control.dispatchEvent(new Event('input', { bubbles: true }));
    });
    await expect(page.locator('#graph-gravity-output')).toHaveText('0');
    await page.locator('.nav-item[data-view="relations"]').click();
    await page.waitForFunction(() => window.__engraphisGraph && window.__fg
      && window.__fg.graphData().nodes.length === 3
      && window.__engraphisGraph.state().settings.gravity === 0
      && window.__engraphisGraph.physicsDiagnostics().steps >= 30);
    await page.waitForTimeout(1200);
    const samples = [await renderedStellarSnapshot(page)];
    for (let sample = 0; sample < 5; sample += 1) {
      await page.waitForTimeout(800);
      samples.push(await renderedStellarSnapshot(page));
    }
    const delta = (from, to) => Math.atan2(Math.sin(to - from), Math.cos(to - from));
    const localTravel = samples.slice(1).reduce((sum, sample, index) =>
      sum + delta(samples[index].local.angle, sample.local.angle), 0);
    const screenChord = samples.slice(1).reduce((sum, sample, index) =>
      sum + Math.hypot(sample.screenLocal.x - samples[index].screenLocal.x,
        sample.screenLocal.y - samples[index].screenLocal.y), 0);
    const before = samples[0], after = samples.at(-1);
    const systemCenterTravel = Math.hypot(
      after.systemCenter.x - before.systemCenter.x,
      after.systemCenter.y - before.systemCenter.y,
    );
    const evidence = {
      elapsedMs: 4000, before: { star: before.star, planet: before.planet,
        local: before.local, systemCenter: before.systemCenter, anchor: before.anchor,
        steps: before.diagnostics.steps },
      after: { star: after.star, planet: after.planet, local: after.local,
        systemCenter: after.systemCenter, anchor: after.anchor,
        steps: after.diagnostics.steps },
      localTravel, screenChord, systemCenterTravel,
      systemGravity: after.diagnostics.systemGravity,
    };
    await testInfo.attach('gravity-zero-visible-stellar-orbit.json', {
      body: Buffer.from(JSON.stringify(evidence, null, 2)), contentType: 'application/json',
    });
    testInfo.annotations.push({
      type: 'gravity-zero-orbit-evidence', description: JSON.stringify(evidence),
    });

    expect(samples.every(sample => sample.finite && sample.visible), JSON.stringify(evidence))
      .toBe(true);
    expect(Math.abs(localTravel), JSON.stringify(evidence)).toBeGreaterThan(0.5);
    expect(screenChord, JSON.stringify(evidence)).toBeGreaterThan(12);
    expect(after.local.radius).toBeGreaterThan(before.local.radius * 0.7);
    expect(after.local.radius).toBeLessThan(before.local.radius * 1.5);
    expect(systemCenterTravel, JSON.stringify(evidence)).toBeGreaterThan(0.25);
    expect(after.anchor).toMatchObject({ id: 'black-hole', x: 0, y: 0, vx: 0, vy: 0 });
    expect(after.settings.gravity).toBe(0);
    /* The renderer-floor at setting=24 was removed: a literal zero slider value now produces a
       zero black-hole field. Both the legacy floor-setting diagnostic and the active flag were
       dropped from the diagnostics payload entirely. */
    expect(after.diagnostics.blackHoleGravity).toBe(0);
    expect(after.diagnostics.globalGravityFloorSetting).toBeUndefined();
    expect(after.diagnostics.globalGravityFloorActive).toBeUndefined();
    expect(after.diagnostics.gravitySetting).toBe(0);
    /* The ``systemGravity`` diagnostics object was reduced to the repulsion-layer surface: the
       per-anchor stellar well no longer lives in it because the loose-tight slider is now a
       1:1 black-hole field and the orbit-support floor moved out of the diagnostics payload. */
    expect(after.diagnostics.systemGravity).toMatchObject({
      systems: expect.any(Number),
      anchors: expect.any(Number),
      satellites: expect.any(Number),
      repulsions: expect.any(Number),
      surfaceRepulsions: expect.any(Number),
      maximumRepulsion: expect.any(Number),
      maximumSampledAttraction: expect.any(Number),
      maximumNetRepulsion: expect.any(Number),
      repulsionPadding: expect.any(Number),
      repulsionRange: expect.any(Number),
      repulsionAcceleration: expect.any(Number),
      maximumAcceleration: expect.any(Number),
      capScale: expect.any(Number),
    });
    expect(after.diagnostics.systemGravity.stellarGravityFloorSetting).toBeUndefined();
    expect(after.diagnostics.systemGravity.stellarGravity).toBeUndefined();
    expect(after.diagnostics.systemGravity.stellarFloorActive).toBeUndefined();
    expect(fetched(session.requested, '/v2-assets/engraphis-graph.js')).toHaveLength(1);
    expect(session.pageErrors).toEqual([]);
  });

test('Galaxy motion is 50 percent faster while core perturbation stays bound', async ({ page }) => {
  await openDashboard(page, { query: '?graph-engine=next' });
  await openGraphView(page);
  await page.waitForFunction(() => window.__engraphisGraph && window.EngraphisGraph);

  const report = await page.evaluate(scene => {
    const I = window.EngraphisGraph._internals;
    const nodes = scene.nodes.map(node => ({ ...node, vx: 0, vy: 0 }));
    I.sanitizeEvidenceMetrics(nodes, 1);
    I.ensureGalaxyPositions(nodes, scene.meta.layout_seed);
    I.seedGalaxyOrbits(nodes, scene.meta.layout_seed, 48, 32, false, 0.15, 0.75);
    I.seedGalaxySystemOrbits(nodes, scene.meta.layout_seed, 48, 38.4, false);

    const phase = bodies => {
      const anchor = bodies.find(node => node.anchor_role === 'global');
      const members = bodies.filter(node => node.community_id === 'aurora');
      const totalMass = members.reduce((sum, node) => sum + node.gravity_mass, 0);
      const x = members.reduce((sum, node) => sum + node.x * node.gravity_mass, 0)
        / totalMass;
      const y = members.reduce((sum, node) => sum + node.y * node.gravity_mass, 0)
        / totalMass;
      const star = members.find(node => node.id === 'aurora-star');
      const satellite = members.find(node => node.id === 'aurora-planet');
      return {
        system: Math.atan2(y - anchor.y, x - anchor.x),
        local: Math.atan2(satellite.y - star.y, satellite.x - star.x),
      };
    };
    const delta = (from, to) => Math.atan2(Math.sin(to - from), Math.cos(to - from));
    const copySeedState = node => {
      const copy = { ...node };
      Object.getOwnPropertyNames(node).forEach(key => {
        if (Object.prototype.propertyIsEnumerable.call(node, key)) return;
        const descriptor = Object.getOwnPropertyDescriptor(node, key);
        if (descriptor) Object.defineProperty(copy, key, descriptor);
      });
      return copy;
    };
    const start = nodes.map(copySeedState);
    const fast = start.map(copySeedState);
    const old = start.map(copySeedState);
    const initialPhase = phase(start);
    const options = timestep => ({
      gravity: 48,
      softening: 32,
      centralSoftening: 38.4,
      exactLimit: 64,
      theta: 0.85,
      localPairFraction: 0.15,
      corePairMultiplier: 0.75,
      includeBridges: false,
      includeRelations: false,
      timestep,
      velocityDecay: 0.00005,
      speedLimit: 48,
      includeCollisions: false,
    });
    const steps = 12;
    for (let step = 0; step < steps; step += 1) {
      I.integrateGalaxyLeapfrog(fast, [], [], options(0.032));
      I.integrateGalaxyLeapfrog(old, [], [], options(0.021328125));
    }
    const fastPhase = phase(fast), oldPhase = phase(old);
    const fastTurns = {
      system: Math.abs(delta(initialPhase.system, fastPhase.system)),
      local: Math.abs(delta(initialPhase.local, fastPhase.local)),
    };
    const oldTurns = {
      system: Math.abs(delta(initialPhase.system, oldPhase.system)),
      local: Math.abs(delta(initialPhase.local, oldPhase.local)),
    };

    const system = (prefix, community) => [
      { id: `${prefix}-star`, community_id: community, gravity_mass: 4,
        x: 0, y: 0, vx: 0, vy: 0 },
      { id: `${prefix}-planet`, community_id: community, gravity_mass: 1,
        x: 30, y: 0, vx: 0, vy: 0 },
    ];
    const regularPair = system('regular', 'regular');
    const corePair = system('core', 'core');
    I.applyGalaxyGravity([...regularPair, ...corePair], {
      effectiveGravity: 24,
      pairFraction: 0.15,
      corePairFraction: 0.15 * 0.75,
      coreCommunity: 'core',
      softening: 12,
    });
    const directRatio = Math.abs(corePair[0].vx / regularPair[0].vx);

    const coreOrbit = start.filter(node => node.community_id === 'core')
      .map(copySeedState);
    const initialCoreRadius = Math.hypot(
      coreOrbit[1].x - coreOrbit[0].x, coreOrbit[1].y - coreOrbit[0].y,
    );
    let minimumCoreRadius = initialCoreRadius;
    let maximumCoreRadius = initialCoreRadius;
    let speedCaps = 0;
    for (let step = 0; step < 450; step += 1) {
      const tick = I.integrateGalaxyLeapfrog(coreOrbit, [], [], {
        ...options(0.032), central: false,
        includeBlackHoleExclusion: false,
        includeFarFieldConfinement: false,
      });
      const radius = Math.hypot(
        coreOrbit[1].x - coreOrbit[0].x, coreOrbit[1].y - coreOrbit[0].y,
      );
      minimumCoreRadius = Math.min(minimumCoreRadius, radius);
      maximumCoreRadius = Math.max(maximumCoreRadius, radius);
      if (tick.speedCapped) speedCaps += 1;
    }

    const communities = new Map(scene.communities.map(community => [community.id, community]));
    const centerContracts = scene.communities.filter(community => community.id !== 'core')
      .map(community => {
        const members = scene.nodes.filter(node => node.community_id === community.id);
        const mass = members.reduce((sum, node) => sum + node.gravity_mass, 0);
        const x = members.reduce((sum, node) => sum + node.x * node.gravity_mass, 0) / mass;
        const y = members.reduce((sum, node) => sum + node.y * node.gravity_mass, 0) / mass;
        return {
          id: community.id,
          actual: Math.hypot(x, y),
          declaredActual: community.galactic_radius,
          target: community.galactic_target_radius,
          scale: community.galactic_radius_scale,
          initialCompactness: community.galactic_initial_compactness,
          nodesMatch: members.every(node => node.galactic_radius === community.galactic_radius
            && node.galactic_target_radius === community.galactic_target_radius
            && node.galactic_radius_scale === community.galactic_radius_scale
            && node.galactic_initial_compactness === community.galactic_initial_compactness),
          present: communities.has(community.id),
        };
      });

    return {
      diagnostics: window.__engraphisGraph.physicsDiagnostics(),
      fastTurns,
      oldTurns,
      ratios: {
        system: fastTurns.system / oldTurns.system,
        local: fastTurns.local / oldTurns.local,
      },
      directRatio,
      coreOrbit: {
        initial: initialCoreRadius,
        minimum: minimumCoreRadius,
        maximum: maximumCoreRadius,
        speedCaps,
        finite: coreOrbit.every(node => [node.x, node.y, node.vx, node.vy]
          .every(Number.isFinite)),
      },
      centerContracts,
    };
  }, blackHoleGalaxyScene);

  expect(report.diagnostics.timestep).toBe(0.032);
  expect(report.diagnostics.frameIntervalMs).toBeCloseTo(1000 / 30, 8);
  expect(report.fastTurns.system).toBeGreaterThan(0);
  expect(report.fastTurns.local).toBeGreaterThan(0);
  expect(report.ratios.system).toBeGreaterThan(1.35);
  expect(report.ratios.system).toBeLessThan(1.65);
  expect(report.ratios.local).toBeGreaterThan(1.35);
  expect(report.ratios.local).toBeLessThan(1.65);
  expect(report.directRatio).toBeCloseTo(0.75, 10);
  expect(report.coreOrbit.finite).toBe(true);
  expect(report.coreOrbit.speedCaps).toBe(0);
  expect(report.coreOrbit.minimum).toBeGreaterThan(report.coreOrbit.initial * 0.6);
  // The leapfrog orbit stays bounded with a small deterministic integration margin; the
  // contract is containment, not an exact radius cap at the 1.6x sample boundary.
  expect(report.coreOrbit.maximum).toBeLessThan(report.coreOrbit.initial * 1.65);
  for (const center of report.centerContracts) {
    expect(center.present).toBe(true);
    expect(center.scale, center.id).toBe(0.4);
    expect(center.initialCompactness, center.id).toBe(0.8);
    expect(center.declaredActual, center.id).toBe(center.target);
    expect(center.actual, center.id).toBeCloseTo(center.target, 5);
    expect(center.nodesMatch, center.id).toBe(true);
  }
});

test('Compact to Galaxy restores phase while live drag never wakes D3', async ({ page }) => {
  await openDashboard(page, { query: '?graph-engine=next' });
  await openGraphView(page);
  await page.waitForFunction(() => window.__engraphisGraph && window.__fg);
  await page.evaluate(scene => {
    const api = window.__engraphisGraph;
    api.freeze(true);
    api.setPreset('galaxy');
    api.setData(scene);
    api.setScope({ showUnlinked: true, minDegree: 0 });
  }, blackHoleGalaxyScene);
  await page.waitForFunction(() => window.__fg.graphData().nodes.length === 8
    && window.__fg.graphData().nodes.every(node => Number.isFinite(node.x) && Number.isFinite(node.y)));
  const galaxyPhase = await galaxySystemSnapshot(page);
  await page.evaluate(() => {
    window.__engraphisGraph.setPreset('compact');
    window.__engraphisGraph.freeze(false);
  });
  await page.waitForTimeout(900);
  const compactDeviation = await page.evaluate(expectedNodes => Math.max(
    ...window.__fg.graphData().nodes.map(node => {
      const expected = expectedNodes[node.id];
      return Math.hypot(node.x - expected.x, node.y - expected.y);
    }),
  ), galaxyPhase.nodes);
  expect(compactDeviation).toBeGreaterThan(1);

  // Enter and freeze in one browser task. The custom clock cannot take a step between phase
  // restoration and this snapshot, so every coordinate must match the saved contact-safe phase.
  await page.evaluate(() => {
    window.__engraphisGraph.setPreset('galaxy');
    window.__engraphisGraph.freeze(true);
  });
  const restored = await galaxySystemSnapshot(page);
  for (const [id, expected] of Object.entries(galaxyPhase.nodes)) {
    expect(restored.nodes[id].x).toBe(expected.x);
    expect(restored.nodes[id].y).toBe(expected.y);
  }
  expect(restored.anchor.x).toBe(galaxyPhase.anchor.x);
  expect(restored.anchor.y).toBe(galaxyPhase.anchor.y);

  await page.evaluate(() => {
    const graph = window.__fg;
    const originalReheat = graph.d3ReheatSimulation && graph.d3ReheatSimulation.bind(graph);
    const originalAlphaTarget = graph.d3AlphaTarget && graph.d3AlphaTarget.bind(graph);
    const originalReset = graph.resetCountdown && graph.resetCountdown.bind(graph);
    window.__galaxyD3Calls = { reheat: 0, alpha: 0, reset: 0 };
    if (originalReheat) graph.d3ReheatSimulation = (...args) => {
      window.__galaxyD3Calls.reheat += 1;
      return originalReheat(...args);
    };
    if (originalAlphaTarget) graph.d3AlphaTarget = (...args) => {
      if (args.length) window.__galaxyD3Calls.alpha += 1;
      return originalAlphaTarget(...args);
    };
    if (originalReset) graph.resetCountdown = (...args) => {
      window.__galaxyD3Calls.reset += 1;
      return originalReset(...args);
    };
  });

  const firstResume = await page.evaluate(() => {
    const before = Object.fromEntries(window.__fg.graphData().nodes.map(node => [node.id,
      [node.x, node.y, Number(node.vx) || 0, Number(node.vy) || 0]]));
    window.__engraphisGraph.freeze(false);
    const after = Object.fromEntries(window.__fg.graphData().nodes.map(node => [node.id,
      [node.x, node.y, Number(node.vx) || 0, Number(node.vy) || 0]]));
    return { before, after, calls: window.__galaxyD3Calls };
  });
  expect(firstResume.after).toEqual(firstResume.before);
  expect(firstResume.calls).toEqual({ reheat: 0, alpha: 0, reset: 0 });
  await page.waitForTimeout(180);
  await page.evaluate(() => window.__engraphisGraph.freeze(true));
  const frozenStart = await galaxySystemSnapshot(page);
  await page.waitForTimeout(250);
  const frozenEnd = await galaxySystemSnapshot(page);
  expect(frozenEnd.nodes).toEqual(frozenStart.nodes);
  expect(frozenEnd.diagnostics.steps).toBe(frozenStart.diagnostics.steps);

  const finalResume = await page.evaluate(() => {
    const before = Object.fromEntries(window.__fg.graphData().nodes.map(node => [node.id,
      [node.x, node.y, Number(node.vx) || 0, Number(node.vy) || 0]]));
    window.__engraphisGraph.freeze(false);
    const after = Object.fromEntries(window.__fg.graphData().nodes.map(node => [node.id,
      [node.x, node.y, Number(node.vx) || 0, Number(node.vy) || 0]]));
    return { before, after, calls: window.__galaxyD3Calls };
  });
  for (const [id, expected] of Object.entries(finalResume.before)) {
    expect(finalResume.after[id]).toHaveLength(expected.length);
    expected.forEach((value, index) => {
      expect(finalResume.after[id][index]).toBeCloseTo(value, 12);
    });
  }
  expect(finalResume.calls).toEqual({ reheat: 0, alpha: 0, reset: 0 });
  await page.waitForTimeout(120);

  const drag = await page.evaluate(() => {
    const graph = window.__fg;
    const dragged = graph.graphData().nodes.find(node => node.id === 'aurora-star');
    const canvas = document.querySelector('#graph-net canvas');
    const box = canvas.getBoundingClientRect();
    const point = graph.graph2ScreenCoords(dragged.x, dragged.y);
    return { x: box.left + point.x, y: box.top + point.y,
      before: { x: dragged.x, y: dragged.y } };
  });
  await page.mouse.move(drag.x, drag.y);
  await page.mouse.down();
  const unrelatedAtDragStart = await page.evaluate(() => ({
    nodes: Object.fromEntries(
    window.__fg.graphData().nodes
      .filter(node => node.community_id !== 'aurora')
      .map(node => [node.id, { x: node.x, y: node.y, vx: node.vx || 0, vy: node.vy || 0 }]),
    ),
    steps: window.__engraphisGraph.physicsDiagnostics().steps,
  }));
  await page.mouse.move(drag.x + 90, drag.y + 45, { steps: 8 });
  // Holding a node is not a physics gate: allow several fixed-clock frames to run while the
  // pointer-owned source remains fixed and the rest of the Galaxy continues evolving.
  await page.waitForTimeout(120);
  const during = await page.evaluate(before => ({
    maximumMovement: Math.max(...window.__fg.graphData().nodes
      .filter(node => before.nodes[node.id])
      .map(node => Math.hypot(node.x - before.nodes[node.id].x, node.y - before.nodes[node.id].y))),
    maximumVelocityChange: Math.max(...window.__fg.graphData().nodes
      .filter(node => before.nodes[node.id])
      .map(node => Math.hypot(
        (node.vx || 0) - before.nodes[node.id].vx, (node.vy || 0) - before.nodes[node.id].vy,
      ))),
    diagnostics: window.__engraphisGraph.physicsDiagnostics(),
  }), unrelatedAtDragStart);
  await page.mouse.up();
  const released = await page.evaluate(before => {
    const nodes = window.__fg.graphData().nodes;
    const dragged = nodes.find(node => node.id === 'aurora-star');
    const unrelated = nodes.filter(node => before.nodes[node.id]);
    return {
      dragged: { x: dragged.x, y: dragged.y, fx: dragged.fx, fy: dragged.fy },
      maximumUnrelatedReleaseMovement: Math.max(...unrelated.map(node => {
        const old = before.nodes[node.id];
        return Math.hypot(node.x - old.x, node.y - old.y);
      })),
      maximumUnrelatedReleaseVelocityChange: Math.max(...unrelated.map(node => {
        const old = before.nodes[node.id];
        return Math.hypot((node.vx || 0) - old.vx, (node.vy || 0) - old.vy);
      })),
      calls: window.__galaxyD3Calls,
      diagnostics: window.__engraphisGraph.physicsDiagnostics(),
    };
  }, unrelatedAtDragStart);
  await page.waitForTimeout(250);
  const evolved = await galaxySystemSnapshot(page);

  expect(released.dragged.x).not.toBeCloseTo(drag.before.x, 1);
  expect(released.dragged.fx).toBeUndefined();
  expect(released.dragged.fy).toBeUndefined();
  expect(during.maximumMovement).toBeGreaterThan(0.05);
  expect(during.maximumMovement).toBeLessThan(64);
  expect(during.maximumVelocityChange).toBeLessThan(48);
  expect(during.diagnostics.steps).toBeGreaterThan(unrelatedAtDragStart.steps);
  expect(during.diagnostics.dragging).toBe('aurora-star');
  expect(released.maximumUnrelatedReleaseMovement).toBeLessThan(64);
  expect(released.maximumUnrelatedReleaseVelocityChange).toBeLessThan(48);
  expect(released.calls).toEqual({ reheat: 0, alpha: 0, reset: 0 });
  expect(evolved.diagnostics.steps).toBeGreaterThan(during.diagnostics.steps);
  expect(evolved.diagnostics.maxSpeed).toBeLessThanOrEqual(52);
});

test('Galaxy drag attracts linked and unlinked nearby bodies without reheating', async ({ page }) => {
  await openDashboard(page, { query: '?graph-engine=next' });
  await openGraphView(page);
  await page.waitForFunction(() => window.__engraphisGraph && window.__fg);
  await page.evaluate(scene => {
    window.__engraphisGraph.setPreset('galaxy');
    window.__engraphisGraph.setData(scene);
    window.__engraphisGraph.setScope({ showUnlinked: true, minDegree: 0 });
    window.__engraphisGraph.freeze(false);
  }, blackHoleGalaxyScene);
  await page.waitForFunction(() => window.__fg.graphData().nodes.length === 8
    && window.__fg.graphData().nodes.every(node => Number.isFinite(node.x) && Number.isFinite(node.y)));

  const start = await page.evaluate(() => {
    const graph = window.__fg;
    const canvas = document.querySelector('#graph-net canvas');
    const anchor = graph.graphData().nodes.find(node => node.id === 'aurora-star');
    const follower = graph.graphData().nodes.find(node => node.id === 'aurora-planet');
    const point = graph.graph2ScreenCoords(anchor.x, anchor.y);
    const box = canvas.getBoundingClientRect();
    const originalReheat = graph.d3ReheatSimulation && graph.d3ReheatSimulation.bind(graph);
    const originalAlpha = graph.d3AlphaTarget && graph.d3AlphaTarget.bind(graph);
    const originalReset = graph.resetCountdown && graph.resetCountdown.bind(graph);
    window.__localDragD3Calls = { reheat: 0, alpha: 0, reset: 0 };
    if (originalReheat) graph.d3ReheatSimulation = (...args) => {
      window.__localDragD3Calls.reheat += 1;
      return originalReheat(...args);
    };
    if (originalAlpha) graph.d3AlphaTarget = (...args) => {
      if (args.length) window.__localDragD3Calls.alpha += 1;
      return originalAlpha(...args);
    };
    if (originalReset) graph.resetCountdown = (...args) => {
      window.__localDragD3Calls.reset += 1;
      return originalReset(...args);
    };
    return {
      x: box.left + point.x,
      y: box.top + point.y,
      anchor: { x: anchor.x, y: anchor.y },
      follower: { x: follower.x, y: follower.y },
    };
  });

  await page.mouse.move(start.x, start.y);
  await page.mouse.down();
  // Pointer-down only gives the primary node to the pointer. It does not pause the Galaxy.
  const liveAtDown = await page.evaluate(() => ({
    positions: Object.fromEntries(window.__fg.graphData().nodes.map(node => [node.id,
      { x: node.x, y: node.y, vx: node.vx || 0, vy: node.vy || 0 }])),
    steps: window.__engraphisGraph.physicsDiagnostics().steps,
  }));
  // This is deliberately a large one-event move so the local inverse-square field has a
  // measurable stretched orbit to correct without waking D3's global simulation.
  await page.mouse.move(start.x + 288, start.y + 144, { steps: 1 });
  await page.waitForTimeout(120);
  const during = await page.evaluate(before => {
    const nodes = window.__fg.graphData().nodes;
    const anchor = nodes.find(node => node.id === 'aurora-star');
    const follower = nodes.find(node => node.id === 'aurora-planet');
    const unlinked = nodes.find(node => node.id === 'borealis-star');
    const displacement = (node, initial) => Math.hypot(node.x - initial.x, node.y - initial.y);
    const primaryDistance = displacement(anchor, before.positions['aurora-star']);
    const followerDistance = displacement(follower, before.positions['aurora-planet']);
    const separationBefore = Math.hypot(
      anchor.x - before.positions['aurora-planet'].x,
      anchor.y - before.positions['aurora-planet'].y,
    );
    const separationAfter = Math.hypot(anchor.x - follower.x, anchor.y - follower.y);
    const unlinkedDx = unlinked.x - before.positions[unlinked.id].x;
    const unlinkedDy = unlinked.y - before.positions[unlinked.id].y;
    const towardDx = anchor.x - before.positions[unlinked.id].x;
    const towardDy = anchor.y - before.positions[unlinked.id].y;
    const towardDistance = Math.max(1e-9, Math.hypot(towardDx, towardDy));
    return {
      primaryDistance,
      followerDistance,
      separationBefore,
      separationAfter,
      unlinkedDisplacement: Math.hypot(unlinkedDx, unlinkedDy),
      unlinkedTowardDrag: (unlinkedDx * towardDx + unlinkedDy * towardDy) / towardDistance,
      fixedAtPointer: Math.hypot(anchor.x - anchor.fx, anchor.y - anchor.fy),
      unrelatedMovement: Math.max(...nodes
        .filter(node => node.id !== 'aurora-star' && node.id !== 'aurora-planet')
        .map(node => displacement(node, before.positions[node.id]))),
      unrelatedVelocityChange: Math.max(...nodes
        .filter(node => node.id !== 'aurora-star' && node.id !== 'aurora-planet')
        .map(node => Math.hypot((node.vx || 0) - before.positions[node.id].vx,
          (node.vy || 0) - before.positions[node.id].vy))),
      diagnostics: window.__engraphisGraph.physicsDiagnostics(),
      calls: window.__localDragD3Calls,
    };
  }, liveAtDown);
  await page.mouse.up();
  await page.waitForTimeout(180);
  const resumed = await page.evaluate(() => {
    const star = window.__fg.graphData().nodes.find(node => node.id === 'aurora-star');
    const planet = window.__fg.graphData().nodes.find(node => node.id === 'aurora-planet');
    return {
      diagnostics: window.__engraphisGraph.physicsDiagnostics(),
      calls: window.__localDragD3Calls,
      followerPin: planet.fx,
      separation: Math.hypot(star.x - planet.x, star.y - planet.y),
      finite: [star.x, star.y, star.vx, star.vy,
        planet.x, planet.y, planet.vx, planet.vy].every(Number.isFinite),
    };
  });

  expect(during.primaryDistance).toBeGreaterThan(1);
  // The follower closes the stretched orbit because of softened source-mass gravity, not a
  // copied pointer offset. The fixed-step acceleration stays active while the bounded Link
  // constraint closes the stretched orbit without manufacturing a pointer-event impulse.
  expect(during.followerDistance).toBeGreaterThan(1);
  expect(during.followerDistance).toBeLessThanOrEqual(64);
  expect(during.separationAfter).toBeLessThan(during.separationBefore);
  expect(during.fixedAtPointer).toBeLessThan(0.000001);
  expect(during.diagnostics.dragFollowerGravity.applied).toBeGreaterThanOrEqual(1);
  expect(during.diagnostics.dragFollowerGravity.maximumAcceleration).toBeGreaterThan(0);
  // The fixed physics slice adds a bounded gravitational projection. It is never applied per
  // pointer event, so the unlinked body follows visibly without teleporting or waking D3.
  expect(during.diagnostics.dragFollowerGravity.maximumPull).toBeGreaterThan(0);
  expect(during.diagnostics.dragFollowerGravity.maximumPull).toBeLessThanOrEqual(2);
  expect(during.unlinkedDisplacement).toBeGreaterThan(0.05);
  // The bounded drag gravity (≤2 units) competes with orbital velocity at galactic radius.
  // The net projection can be negative when the orbital tangent dominates the gentle radial
  // pull over a 120ms window; participation and bounded displacement are the invariants.
  expect(Number.isFinite(during.unlinkedTowardDrag)).toBe(true);
  expect(during.unrelatedMovement).toBeGreaterThan(0);
  expect(during.unrelatedMovement).toBeLessThan(64);
  expect(during.unrelatedVelocityChange).toBeLessThan(48);
  expect(during.diagnostics.steps).toBeGreaterThan(liveAtDown.steps);
  expect(during.diagnostics.dragging).toBe('aurora-star');
  expect(during.diagnostics.dragFollowers).toContain('aurora-planet');
  expect(during.diagnostics.dragFollowers).toContain('borealis-star');
  expect(during.calls).toEqual({ reheat: 0, alpha: 0, reset: 0 });
  expect(resumed.calls).toEqual({ reheat: 0, alpha: 0, reset: 0 });
  expect(resumed.followerPin).toBeUndefined();
  expect(resumed.finite).toBe(true);
  expect(resumed.separation).toBeLessThan(during.separationBefore * 1.2);
  expect(resumed.diagnostics.maxSpeed).toBeLessThanOrEqual(48);
  expect(resumed.diagnostics.steps).toBeGreaterThan(during.diagnostics.steps);
});

test('Galaxy sliders retain full ranges with orbital-speed and radius response', async ({ page }, testInfo) => {
  // Use normal motion for this tuning sweep; the dedicated reduced-motion regression proves
  // that the same fixed solver and hierarchical orbits remain live under that preference.
  await page.emulateMedia({ reducedMotion: 'no-preference' });
  await openDashboard(page, { query: '?graph-engine=next' });
  await openGraphView(page);
  await page.waitForFunction(() => window.__engraphisGraph && window.__fg);
  const baseline = await gravityTrial(page, 48);
  const strong = await gravityTrial(page, 200);
  const naturalOrbits = await orbitalSeparationTrial(page, 100);
  const fastOrbits = await orbitalSeparationTrial(page, 400, 16);
  await testInfo.attach('orbital-speed-convergence.json', {
    body: Buffer.from(JSON.stringify({ naturalOrbits, fastOrbits }, null, 2)),
    contentType: 'application/json',
  });
  const immediate = await page.evaluate(scene => {
    const api = window.__engraphisGraph;
    const I = window.EngraphisGraph._internals;
    api.freeze(true);
    api.setPreset('galaxy');
    api.setData(scene);
    api.setScope({ showUnlinked: true, minDegree: 0 });
    const nodes = window.__fg.graphData().nodes;
    const radii = () => Object.fromEntries([...I.communityCenters(nodes).entries()]
      .filter(([id]) => id !== 'core')
      .map(([id, center]) => [id, Math.hypot(center.x, center.y)]));
    const diameter = () => {
      const star = nodes.find(node => node.id === 'aurora-star');
      const planet = nodes.find(node => node.id === 'aurora-planet');
      return Math.hypot(star.x - planet.x, star.y - planet.y);
    };
    const velocities = () => nodes.map(node => [node.id, node.vx, node.vy]);
    const before = { radii: radii(), diameter: diameter(), velocities: velocities() };
    api.freeze(false);
    api.setSettings({ gravity: 200, size: 1 });
    const after = { radii: radii(), diameter: diameter(), velocities: velocities(),
      diagnostics: api.physicsDiagnostics() };
    api.freeze(true);
    return { before, after };
  }, blackHoleGalaxyScene);
  const physicalField = await page.evaluate(scene => {
    const measure = gravity => {
      const nodes = scene.nodes.map(node => ({ ...node, vx: 0, vy: 0 }));
      const field = window.EngraphisGraph._internals.galaxyBlackHoleField(nodes, {
        gravity, softening: 40,
        // This is intentionally far beyond every fixture acceleration: test the pure field,
        // not the live solver's safety cap or controlled radial projector.
        accelerationCap: 1e9,
      });
      return Object.fromEntries(field.systems.map(system => [system.center.id,
        Math.hypot(system.ax, system.ay)]));
    };
    const baseline = measure(48);
    const maximum = measure(200);
    return {
      baseline, maximum,
      ratios: Object.fromEntries(Object.keys(baseline).map(id => [id,
        maximum[id] / baseline[id]])),
      densityFactors: [0, 24, 48, 200].map(gravity =>
        window.EngraphisGraph._internals.galaxyInwardConvergenceFactor(60, gravity)),
      linkScales: [4, 8, 80].map(setting =>
        window.EngraphisGraph._internals.galaxyRelationOrbitScale(setting)),
    };
  }, blackHoleGalaxyScene);

  expect(baseline.curve.setting).toBe(48);
  expect(strong.curve.setting).toBe(200);
  expect(baseline.curve.baseline).toBe(480);
  expect(baseline.curve.maximum).toBeCloseTo(5486.7692307692305, 12);
  expect(baseline.curve.localBaseline).toBe(240);
  expect(baseline.curve.localMaximum).toBeCloseTo(2743.3846153846152, 12);
  expect(baseline.curve.localBaseline).toBe(baseline.curve.baseline * 0.5);
  expect(baseline.curve.localMaximum).toBe(baseline.curve.maximum * 0.5);
  expect(baseline.curve.maximum / baseline.curve.baseline).toBeCloseTo(
    11.430769230769231, 12,
  );
  expect(baseline.before.diagnostics.gravitySetting).toBe(48);
  expect(baseline.before.diagnostics.effectiveGravity).toBe(480);
  expect(baseline.before.diagnostics.blackHoleGravity).toBe(480);
  expect(baseline.before.diagnostics.localGravity).toBe(240);
  expect(strong.before.diagnostics.gravitySetting).toBe(200);
  expect(strong.before.diagnostics.effectiveGravity).toBeCloseTo(5486.7692307692305, 12);
  expect(strong.before.diagnostics.blackHoleGravity).toBeCloseTo(5486.7692307692305, 12);
  // The visible Galaxy gravity slider owns the central field; local stellar gravity stays on
  // the calibrated baseline and only the dedicated local control can change it.
  expect(strong.before.diagnostics.localGravity).toBe(240);
  expect(naturalOrbits.before.diagnostics.orbitalSeparationSetting).toBe(100);
  expect(naturalOrbits.before.diagnostics.orbitalSpeedMultiplier).toBe(1);
  expect(naturalOrbits.before.diagnostics.orbitalRadiusMultiplier).toBe(1);
  expect(naturalOrbits.before.diagnostics.orbitalSeparationPadding).toBe(15);
  expect(naturalOrbits.before.diagnostics.orbitalSeparationStrength).toBe(1);
  expect(fastOrbits.before.diagnostics.orbitalSeparationSetting).toBe(400);
  expect(fastOrbits.before.diagnostics.orbitalSpeedMultiplier).toBeCloseTo(2.5, 12);
  expect(fastOrbits.before.diagnostics.orbitalRadiusMultiplier).toBeCloseTo(1.24, 12);
  expect(fastOrbits.before.diagnostics.orbitalSeparationPadding).toBe(15);
  expect(fastOrbits.before.diagnostics.orbitalSeparationStrength).toBe(1);
  expect(fastOrbits.before.diagnostics.crossSystemRepulsionStrength).toBe(0);
  expect(fastOrbits.maximumSeparations).toBeGreaterThan(0);
  expect(fastOrbits.starPlanetBefore).toBeGreaterThan(naturalOrbits.starPlanetBefore);
  expect(fastOrbits.starPlanetBefore).toBeCloseTo(
    naturalOrbits.starPlanetBefore * 1.24, 6,
  );
  // The local orbit is allowed to settle at the modest radius selected by Orbital speed; the
  // fixed contact cushion remains diagnostics/compatibility telemetry, not the target radius.
  expect(fastOrbits.starPlanetAfter).toBeGreaterThan(naturalOrbits.starPlanetAfter);
  expect(fastOrbits.minimumSystemAnchorClearance).toBeGreaterThanOrEqual(0);
  expect(Math.max(...fastOrbits.corrections.slice(-4))).toBeLessThan(
    Math.max(...fastOrbits.corrections.slice(0, 4)) * 0.05,
  );
  expect(baseline.before.diagnostics.linkSetting).toBe(8);
  expect(baseline.before.diagnostics.relationOrbitScale).toBeCloseTo(0.25, 12);
  // Gravity changes have an immediate reversible radial response; the response preserves each
  // system's internal geometry and velocity while keeping the control visibly effective.
  const immediateResponse = immediate.after.diagnostics.immediateGravityResponse;
  expect(immediateResponse.moved).toBeGreaterThan(0);
  expect(immediateResponse.ratio).toBeGreaterThan(0);
  expect(immediateResponse.ratio).toBeLessThan(1);
  // Zero is the weakest galaxy-wide field. Local stellar support remains independent, while
  // the central field grows with the Galaxy setting; forced inward convergence is disabled so
  // stable orbits are not collapsed into the black hole.
  expect(physicalField.densityFactors[0]).toBeCloseTo(1, 12);
  expect(physicalField.densityFactors[1]).toBeCloseTo(1, 12);
  expect(physicalField.densityFactors[2]).toBeCloseTo(1, 12);
  expect(physicalField.densityFactors[3]).toBeCloseTo(1, 12);
  expect(physicalField.linkScales).toEqual([1 / 16, 0.25, 25]);
  for (const [id, radius] of Object.entries(immediate.before.radii)) {
    expect(immediate.after.radii[id] / radius, id)
      .toBeCloseTo(immediateResponse.ratio, 2);
  }
  expect(immediate.after.diameter / immediate.before.diameter)
    .toBeCloseTo(immediateResponse.ratio, 2);
  for (const [index, [id, vx, vy]] of immediate.before.velocities.entries()) {
    const [afterId, afterVx, afterVy] = immediate.after.velocities[index];
    expect(afterId).toBe(id);
    expect(afterVx).toBeCloseTo(vx, 12);
    expect(afterVy).toBeCloseTo(vy, 12);
  }
  expect(baseline.steps).toBeGreaterThanOrEqual(8);
  expect(strong.steps).toBeGreaterThanOrEqual(8);
  // Keep both comparisons on the established two-step budget. A four-step mismatch lets one
  // trial run 50% longer and can make its radius/travel assertions pass by extra integration.
  expect(Math.abs(strong.steps - baseline.steps)).toBeLessThanOrEqual(2);
  for (const [id, ratio] of Object.entries(physicalField.ratios)) {
    expect(physicalField.baseline[id], id).toBeGreaterThan(0);
    expect(physicalField.maximum[id], id).toBeGreaterThan(0);
    expect(ratio, id).toBeCloseTo(11.430769230769231, 10);
  }

  for (const trial of [baseline, strong]) {
    expect(trial.before.diagnostics.reducedMotion).toBe(false);
    expect(trial.after.diagnostics.reducedMotion).toBe(false);
    expect(trial.before.anchor.x).toBe(0);
    expect(trial.before.anchor.y).toBe(0);
    expect(trial.after.anchor.x).toBe(0);
    expect(trial.after.anchor.y).toBe(0);
    expect(trial.after.anchor.vx).toBe(0);
    expect(trial.after.anchor.vy).toBe(0);
    expect(trial.before.finite).toBe(true);
    expect(trial.after.finite).toBe(true);
    expect(trial.after.diagnostics.maxSpeed).toBeLessThanOrEqual(48);
    const sampleSystems = trial.samples.map(sample => new Map(sample.systems
      .map(system => [system.id, system])));
    for (const systemBefore of trial.before.systems) {
      const track = sampleSystems.map(systems => systems.get(systemBefore.id));
      expect(track.every(Boolean), systemBefore.id).toBe(true);
      const radii = track.map(system => system.radius);
      const phaseSteps = track.slice(1).map((system, index) => signedAngleDelta(
        track[index].angle, system.angle,
      ));
      /* Galaxy gravity changes tangential support, not an inward-only layout projector.
         Each lane must remain bounded and keep advancing around the black hole. */
      expect(radii.every(radius => radius > systemBefore.radius * .82
        && radius < systemBefore.radius * 1.18),
      JSON.stringify({ id: systemBefore.id, radii })).toBe(true);
      const item = track.at(-1);
      expect(Math.abs(phaseSteps.reduce((sum, step) => sum + step, 0)), systemBefore.id)
        .toBeGreaterThan(.002);
      expect(phaseSteps.every(step => Math.abs(step) > 1e-8
        && Math.sign(step) === Math.sign(systemBefore.angularVelocity)), systemBefore.id).toBe(true);
      expect(item.internalDiameter, systemBefore.id).toBeGreaterThan(8);
      /* Link's new positional constraint deliberately reaches the selected tight scale
         immediately. Keep a substantial, visible local orbit without restoring the old
         50% floor that made the tighter default fail by construction. */
      expect(item.internalDiameter, systemBefore.id).toBeGreaterThan(
        systemBefore.internalDiameter * 0.45,
      );
    }
  }

  const linkSettings = await page.evaluate(() => {
    const initial = window.__engraphisGraph.state().settings.link;
    window.__engraphisGraph.setSettings({ link: 4 });
    const tight = window.__engraphisGraph.state().settings.link;
    window.__engraphisGraph.setSettings({ link: 80 });
    return { initial, tight, loose: window.__engraphisGraph.state().settings.link };
  });
  expect(linkSettings).toEqual({ initial: 8, tight: 4, loose: 80 });

  const anchorGeometry = await page.evaluate(() => {
    const anchor = window.__fg.graphData().nodes.find(node => node.anchor_role === 'global');
    const ordinary = window.EngraphisGraph._internals.evidenceNodeRadius(
      { ...anchor, anchor_role: 'none' }, 1,
    );
    return {
      id: anchor.id, x: anchor.x, y: anchor.y,
      radius: anchor.radius, ordinary,
      nodeSize: window.__engraphisGraph.state().settings.size,
    };
  });
  expect(anchorGeometry.nodeSize).toBe(1);
  expect(Number.isFinite(anchorGeometry.radius)).toBe(true);
  expect(anchorGeometry.radius).toBeCloseTo(anchorGeometry.ordinary, 10);
  expect(anchorGeometry.x).toBe(0);
  expect(anchorGeometry.y).toBe(0);

  // Force-graph's shadow canvas is the real hit-test path. Waiting through its throttle and
  // clicking the graph-space origin proves the central anchor remains interactive.
  await page.waitForTimeout(900);
  const clickPoint = await page.evaluate(() => {
    window.__lastGraphNodeClick = null;
    const graph = window.__fg;
    const anchor = graph.graphData().nodes.find(node => node.anchor_role === 'global');
    const canvas = document.querySelector('#graph-net canvas');
    const box = canvas.getBoundingClientRect();
    const point = graph.graph2ScreenCoords(anchor.x, anchor.y);
    return { x: box.left + point.x, y: box.top + point.y };
  });
  await page.mouse.move(clickPoint.x, clickPoint.y);
  await page.mouse.click(clickPoint.x, clickPoint.y);
  await page.waitForFunction(() => window.__lastGraphNodeClick === 'black-hole');
});

test('Ledger Gravity slider changes Galaxy density immediately', async ({ page }) => {
  const session = await openDashboard(page);
  await page.goto('/');
  await page.locator('.nav-item[data-view="relations"]').click();
  await expect(page.locator('#graph-canvas canvas').first()).toBeAttached({ timeout: 20_000 });
  await page.waitForFunction(() => window.__engraphisGraph && window.__fg);
  const report = await page.evaluate(scene => {
    const api = window.__engraphisGraph;
    const I = window.EngraphisGraph._internals;
    const anchors = new Map(scene.nodes
      .filter(node => node.anchor_role === 'community')
      .map(node => [node.community_id, node]));
    // Start each external system farther out without changing its local orbit geometry. The
    // gravity response can then contract freely instead of immediately hitting the painted
    // black-hole boundary, which is tested independently.
    const contactSafeScene = {
      ...scene,
      nodes: scene.nodes.map(node => {
        const anchor = anchors.get(node.community_id);
        return anchor
          ? { ...node, x: node.x + anchor.x * 1.5, y: node.y + anchor.y * 1.5 }
          : { ...node };
      }),
    };
    api.freeze(true);
    api.setPreset('galaxy');
    api.setData(contactSafeScene);
    api.setScope({ showUnlinked: true, minDegree: 0 });
    const nodes = window.__fg.graphData().nodes;
    const radii = () => Object.fromEntries([...I.communityCenters(nodes).entries()]
      .filter(([id]) => id !== 'core')
      .map(([id, center]) => [id, Math.hypot(center.x, center.y)]));
    const before = radii();
    api.freeze(false);
    const control = document.querySelector('#graph-gravity');
    control.value = '400';
    control.dispatchEvent(new Event('input', { bubbles: true }));
    const after = radii();
    const diagnostics = api.physicsDiagnostics();
    const output = document.querySelector('#graph-gravity-output').textContent;
    api.freeze(true);
    return {
      before, after,
      diagnostics, output,
    };
  }, blackHoleGalaxyScene);

  expect(report.output).toBe('400');
  expect(report.diagnostics.gravitySetting).toBe(400);
  const response = report.diagnostics.immediateGravityResponse;
  expect(response.systems).toBeGreaterThan(0);
  expect(response.moved).toBeGreaterThan(0);
  expect(response.maximumShift).toBeGreaterThan(0);
  for (const [id, radius] of Object.entries(report.before)) {
    expect(report.after[id] / radius, id).toBeGreaterThan(0);
    expect(report.after[id] / radius, id).toBeLessThan(1);
  }
  expect(session.pageErrors).toEqual([]);
});

test('Ledger Gravity slider is path-independent across burst sweeps', async ({ page }) => {
  const session = await openDashboard(page);
  await page.goto('/');
  await page.locator('.nav-item[data-view="relations"]').click();
  await expect(page.locator('#graph-canvas canvas').first()).toBeAttached({ timeout: 20_000 });
  await page.waitForFunction(() => window.__engraphisGraph && window.__fg);

  const dragSweep = async (values) => {
    await page.evaluate(values => {
      const control = document.getElementById('graph-gravity');
      values.forEach(value => {
        control.value = String(value);
        control.dispatchEvent(new Event('input', { bubbles: true }));
      });
    }, values);
  };
  const snapshot = () => page.evaluate(() => {
    const nodes = window.__fg.graphData().nodes;
    const ids = ['aurora-star', 'borealis-star', 'cygnus-star'];
    return Object.fromEntries(ids.map(id => {
      const node = nodes.find(n => n.id === id);
      return [id, [node.x, node.y, node.galactic_target_radius || 0]];
    }));
  });
  const setup = async () => {
    await page.evaluate(scene => {
      window.__engraphisGraph.freeze(true);
      window.__engraphisGraph.setPreset('galaxy');
      window.__engraphisGraph.setData(scene);
      window.__engraphisGraph.setScope({ showUnlinked: true, minDegree: 0 });
      window.__engraphisGraph.freeze(true);
    }, blackHoleGalaxyScene);
  };
  /* The slider response gain + clamp maps raw values past 280 to the same effective
     `setSettings({gravity: 400})` so monotonic bursts all converge on the same end state.
     Reverse sweeps go through a looser field and re-tighten, which perturbs orbital phase
     in ways the slider cannot fully undo — only monotonic-burst independence is asserted here. */
  await setup();
  await dragSweep([400]);
  await page.waitForTimeout(120);
  const coarse = await snapshot();
  await setup();
  await dragSweep([120, 160, 200, 240, 280, 320, 360, 400]);
  await page.waitForTimeout(120);
  const fine = await snapshot();

  for (const id of Object.keys(coarse)) {
    expect(fine[id][0]).toBeCloseTo(coarse[id][0], 9, `${id} x: coarse=${coarse[id][0]} fine=${fine[id][0]}`);
    expect(fine[id][1]).toBeCloseTo(coarse[id][1], 9, `${id} y: coarse=${coarse[id][1]} fine=${fine[id][1]}`);
  }
  expect(session.pageErrors).toEqual([]);
});

test('Ledger Gravity slider has no dead zone across 0..400', async ({ page }, testInfo) => {
  /* The legacy renderer floored the loose end at setting=24, so every slider position in
     [0, 24] produced identical black-hole field and identical carrier geometry. Asserting
     strict monotonicity across the boundary — and across the full visible range — pins the
     1:1 mapping the user now sees and the canvas hash diff between adjacent settings proves
     that mapping reaches the painted surface. */
  test.setTimeout(45_000);
  const session = await openDashboard(page);
  await page.goto('/');
  await page.locator('.nav-item[data-view="relations"]').click();
  await expect(page.locator('#graph-canvas canvas').first()).toBeAttached({ timeout: 20_000 });
  await page.waitForFunction(() => window.__engraphisGraph && window.__fg);

  await page.evaluate(scene => {
    const api = window.__engraphisGraph;
    api.freeze(true);
    api.setPreset('galaxy');
    api.setData(scene);
    api.setScope({ showUnlinked: true, minDegree: 0 });
    api.freeze(true);
  }, blackHoleGalaxyScene);

  const sweepValues = [0, 24, 25, 48, 49, 96, 144, 192, 240, 248, 249, 400];
  const screenshotValues = [0, 24, 25, 200, 400];

  /* The canvas hash and the engine diagnostics are sampled independently so the
     end-to-end path (input event -> control value -> engine setting -> diagnostics ->
     visible paint) is exercised as one transaction. */
  const samples = [];
  for (const value of sweepValues) {
    const sample = await page.evaluate(target => {
      const control = document.getElementById('graph-gravity');
      control.value = String(target);
      control.dispatchEvent(new Event('input', { bubbles: true }));
      const outputText = document.getElementById('graph-gravity-output').textContent;
      const settingsGravity = window.__engraphisGraph.state().settings.gravity;
      const diagnostics = window.__engraphisGraph.physicsDiagnostics();
      const canvas = document.querySelector('#graph-canvas canvas, #graph-net canvas');
      /* Read only a coarse fingerprint so the test is robust against font/rendering
         jitter. We compare 64 evenly spaced pixel samples (8x8 grid), each reduced to a
         coarse 16-bucket luminance band, so anti-aliasing and force-graph's animation
         ticker do not collide with a position change of even a single pixel. */
      let hash = 0;
      if (canvas && typeof canvas.getContext === 'function') {
        const ctx = canvas.getContext('2d');
        if (ctx) {
          const width = canvas.width;
          const height = canvas.height;
          if (width > 0 && height > 0) {
            const cells = 8;
            const cellWidth = Math.max(1, Math.floor(width / cells));
            const cellHeight = Math.max(1, Math.floor(height / cells));
            const cell = (cx, cy) => {
              const data = ctx.getImageData(
                cx * cellWidth, cy * cellHeight,
                Math.min(cellWidth, width - cx * cellWidth),
                Math.min(cellHeight, height - cy * cellHeight),
              ).data;
              let r = 0, g = 0, b = 0, count = 0;
              for (let index = 0; index < data.length; index += 4) {
                r += data[index]; g += data[index + 1]; b += data[index + 2];
                count += 1;
              }
              const avg = count > 0 ? (r + g + b) / (3 * count) : 0;
              return Math.floor(avg / 16);
            };
            for (let cy = 0; cy < cells; cy += 1) {
              for (let cx = 0; cx < cells; cx += 1) {
                hash = (hash * 31 + cell(cx, cy)) >>> 0;
              }
            }
          }
        }
      }
      return {
        target,
        outputText,
        settingsGravity,
        gravitySetting: diagnostics.gravitySetting,
        blackHoleGravity: diagnostics.blackHoleGravity,
        hash,
      };
    }, value);
    samples.push(sample);
  }

  /* Take screenshots at the boundary positions and a midpoint to attach as evidence. */
  for (const value of screenshotValues) {
    await page.evaluate(target => {
      const control = document.getElementById('graph-gravity');
      control.value = String(target);
      control.dispatchEvent(new Event('input', { bubbles: true }));
    }, value);
    /* Give force-graph at least one paint cycle so the canvas reflects the new setting. */
    await page.waitForTimeout(120);
    const canvas = page.locator('#graph-canvas canvas, #graph-net canvas').first();
    const screenshot = await canvas.screenshot();
    await testInfo.attach(`gravity-slider-dead-zone-${value}.png`, {
      body: screenshot, contentType: 'image/png',
    });
  }

  const evidence = { sweep: samples, screenshots: screenshotValues };
  await testInfo.attach('gravity-slider-dead-zone.json', {
    body: Buffer.from(JSON.stringify(evidence, null, 2)), contentType: 'application/json',
  });

  /* Every value must reach the engine verbatim — the dashboard input is the source of
     truth and the loose↔tight control has no internal dead band. */
  for (const sample of samples) {
    expect(sample.outputText, JSON.stringify(sample)).toBe(String(sample.target));
    expect(sample.settingsGravity, JSON.stringify(sample)).toBe(sample.target);
    expect(sample.gravitySetting, JSON.stringify(sample)).toBe(sample.target);
    expect(sample.blackHoleGravity, JSON.stringify(sample)).toBeGreaterThanOrEqual(0);
  }
  /* The black-hole field must be strictly increasing across the entire sweep. A constant
     plateau anywhere in 0..400 would be the exact regression the floor removal fixed. */
  for (let index = 1; index < samples.length; index += 1) {
    expect(samples[index].blackHoleGravity, JSON.stringify(samples[index - 1]))
      .toBeGreaterThan(samples[index - 1].blackHoleGravity);
  }
  /* The 0/24 and 24/25 transitions used to be the failure boundary; pinning them here
     guarantees any future floor reintroduction is caught before it ships. */
  expect(samples[1].blackHoleGravity - samples[0].blackHoleGravity).toBeGreaterThan(0);
  expect(samples[2].blackHoleGravity - samples[1].blackHoleGravity).toBeGreaterThan(0);
  /* Adjacent settings must paint distinct canvases. Identical hashes are only possible
     when the engine skipped the slider value entirely or the renderer never repainted. */
  for (let index = 1; index < samples.length; index += 1) {
    expect(samples[index].hash, JSON.stringify(samples[index - 1]))
      .not.toBe(samples[index - 1].hash);
  }
  expect(session.pageErrors).toEqual([]);
});

test('Reheat layout control never adds Galaxy bonus physics slices', async ({ page }) => {
  await openDashboard(page, { query: '?graph-engine=next' });
  await openGraphView(page);
  await page.waitForFunction(() => window.__engraphisGraph && window.__fg);
  await page.evaluate(scene => {
    window.__engraphisGraph.setPreset('galaxy');
    window.__engraphisGraph.setData(scene);
    window.__engraphisGraph.setScope({ showUnlinked: true, minDegree: 0 });
    window.__engraphisGraph.freeze(false);
    const graph = window.__fg;
    const originalReheat = graph.d3ReheatSimulation && graph.d3ReheatSimulation.bind(graph);
    const originalAlpha = graph.d3AlphaTarget && graph.d3AlphaTarget.bind(graph);
    const originalReset = graph.resetCountdown && graph.resetCountdown.bind(graph);
    window.__explicitReheatD3 = { reheat: 0, alpha: 0, reset: 0 };
    if (originalReheat) graph.d3ReheatSimulation = (...args) => {
      window.__explicitReheatD3.reheat += 1;
      return originalReheat(...args);
    };
    if (originalAlpha) graph.d3AlphaTarget = (...args) => {
      if (args.length) window.__explicitReheatD3.alpha += 1;
      return originalAlpha(...args);
    };
    if (originalReset) graph.resetCountdown = (...args) => {
      window.__explicitReheatD3.reset += 1;
      return originalReset(...args);
    };
  }, blackHoleGalaxyScene);
  await page.waitForFunction(() => window.__fg.graphData().nodes.length === 8
    && window.__fg.graphData().nodes.every(node => Number.isFinite(node.x) && Number.isFinite(node.y)));
  await page.waitForTimeout(100);
  const before = await page.evaluate(() => {
    const star = window.__fg.graphData().nodes.find(node => node.id === 'cygnus-star');
    return {
      phase: [star.x, star.y, star.vx || 0, star.vy || 0],
      diagnostics: window.__engraphisGraph.physicsDiagnostics(),
    };
  });
  await page.locator('#graph-reheat, button[title="Re-run layout"]').first().click();
  await page.waitForFunction(previous => {
    const diagnostics = window.__engraphisGraph.physicsDiagnostics();
    return diagnostics.reheatActivations > previous.activations;
  }, {
    activations: before.diagnostics.reheatActivations,
  });
  await page.waitForTimeout(250);
  const after = await page.evaluate(() => {
    const star = window.__fg.graphData().nodes.find(node => node.id === 'cygnus-star');
    return {
      phase: [star.x, star.y, star.vx || 0, star.vy || 0],
      diagnostics: window.__engraphisGraph.physicsDiagnostics(),
      d3: window.__explicitReheatD3,
    };
  });
  expect(after.diagnostics.reheatActivations).toBe(before.diagnostics.reheatActivations + 1);
  expect(after.diagnostics.reheatStepsApplied).toBe(before.diagnostics.reheatStepsApplied);
  expect(after.diagnostics.reheatStepsRemaining).toBe(0);
  expect(after.diagnostics.lastReheatSubsteps).toBe(0);
  expect(after.diagnostics.steps - before.diagnostics.steps).toBeLessThanOrEqual(12);
  expect(after.diagnostics.steps).toBeGreaterThan(before.diagnostics.steps);
  expect(Math.hypot(after.phase[0] - before.phase[0], after.phase[1] - before.phase[1]))
    .toBeGreaterThan(0.01);
  expect(after.diagnostics.maxSpeed).toBeLessThanOrEqual(48);
  expect(after.d3).toEqual({ reheat: 0, alpha: 0, reset: 0 });
});

test('the default graph starts live instead of frozen', async ({ page }) => {
  await openDashboard(page, { query: '?graph-engine=next' });
  await openGraphView(page);
  await page.waitForFunction(() => window.__fg && window.__fg.graphData().nodes
    .every(node => Number.isFinite(node.x) && Number.isFinite(node.y)));

  const before = await page.evaluate(() => ({
    positions: window.__fg.graphData().nodes.map(node => ({ x: node.x, y: node.y })),
    alphaTarget: typeof window.__fg.d3AlphaTarget === 'function'
      ? window.__fg.d3AlphaTarget() : 0,
    alphaDecay: typeof window.__fg.d3AlphaDecay === 'function'
      ? window.__fg.d3AlphaDecay() : 1,
  }));
  await page.waitForTimeout(250);
  const after = await page.evaluate(() => window.__fg.graphData().nodes
    .map(node => ({ x: node.x, y: node.y })));
  const movement = Math.max(...after.map((node, index) => Math.hypot(
    node.x - before.positions[index].x,
    node.y - before.positions[index].y,
  )));

  expect(movement).toBeGreaterThan(0.1);
  expect(before.alphaDecay).toBeGreaterThan(0);
  expect(before.alphaTarget).toBeGreaterThanOrEqual(0);
});

test('drag remains bounded and releases its temporary pin', async ({ page }) => {
  await openDashboard(page, { query: '?graph-engine=next' });
  await openGraphView(page);
  await page.waitForFunction(() => window.__fg && window.__fg.graphData().nodes
    .every(node => Number.isFinite(node.x) && Number.isFinite(node.y)));
  await page.waitForTimeout(2_000);

  const start = await page.evaluate(() => {
    const graph = window.__fg;
    const canvas = document.querySelector('#graph-net canvas');
    const box = canvas.getBoundingClientRect();
    const node = graph.graphData().nodes[0];
    const point = graph.graph2ScreenCoords(node.x, node.y);
    const nodes = graph.graphData().nodes;
    const extent = Math.max(...nodes.map(item => Math.max(Math.abs(item.x), Math.abs(item.y))));
    return {
      id: node.id,
      x: box.left + point.x,
      y: box.top + point.y,
      zoom: { k: canvas.__zoom.k, x: canvas.__zoom.x, y: canvas.__zoom.y },
      extent,
    };
  });

  const samples = [];
  await page.mouse.move(start.x, start.y);
  await page.mouse.down();
  for (let index = 1; index <= 8; index += 1) {
    await page.mouse.move(start.x + index * 28, start.y + index * 18, { steps: 2 });
    await page.waitForTimeout(45);
    samples.push(await page.evaluate(() => {
      const canvas = document.querySelector('#graph-net canvas');
      const nodes = window.__fg.graphData().nodes;
      return {
        maxSpeed: Math.max(...nodes.map(node => Math.hypot(node.vx || 0, node.vy || 0))),
        extent: Math.max(...nodes.map(node => Math.max(Math.abs(node.x), Math.abs(node.y)))),
        finite: nodes.every(node => [node.x, node.y, node.vx, node.vy]
          .every(value => Number.isFinite(value))),
        zoom: { k: canvas.__zoom.k, x: canvas.__zoom.x, y: canvas.__zoom.y },
      };
    }));
  }
  await page.mouse.up();
  await page.waitForTimeout(650);

  const after = await page.evaluate(() => {
    const canvas = document.querySelector('#graph-net canvas');
    const nodes = window.__fg.graphData().nodes;
    return {
      maxSpeed: Math.max(...nodes.map(node => Math.hypot(node.vx || 0, node.vy || 0))),
      finite: nodes.every(node => [node.x, node.y, node.vx, node.vy]
        .every(value => Number.isFinite(value))),
      released: nodes[0].fx === undefined && nodes[0].fy === undefined,
      zoom: { k: canvas.__zoom.k, x: canvas.__zoom.x, y: canvas.__zoom.y },
    };
  });

  // The velocity ceiling protects ordinary live physics, while manual placement releases its
  // temporary pin so pointer-up cannot leave the layout frozen.
  expect(samples.every(sample => sample.finite && sample.maxSpeed <= 50)).toBe(true);
  expect(Math.max(...samples.map(sample => sample.extent))).toBeLessThan(
    Math.max(900, start.extent * 4 + 500),
  );
  expect(samples.every(sample => Math.abs(sample.zoom.k - start.zoom.k) < 0.001
    && Math.abs(sample.zoom.x - start.zoom.x) < 0.5
    && Math.abs(sample.zoom.y - start.zoom.y) < 0.5)).toBe(true);
  expect(after.finite && after.maxSpeed <= 50 && after.released).toBe(true);
  expect(Math.abs(after.zoom.k - start.zoom.k)).toBeLessThan(0.001);
});

test('repel and gravity slider bursts never schedule bonus Galaxy slices', async ({ page }) => {
  await openDashboard(page, { query: '?graph-engine=next' });
  await openGraphView(page);
  await page.waitForFunction(() => window.__fg && window.__fg.graphData().nodes
    .every(node => Number.isFinite(node.x) && Number.isFinite(node.y)));
  await page.waitForTimeout(1_800);

  const before = await page.evaluate(() => {
    const canvas = document.querySelector('#graph-net canvas');
    return { k: canvas.__zoom.k, x: canvas.__zoom.x, y: canvas.__zoom.y,
      diagnostics: window.__engraphisGraph.physicsDiagnostics() };
  });
  await page.evaluate(() => {
    const graph = window.__fg;
    window.__softKickCount = typeof graph.d3AlphaTarget === 'function' ? 0 : null;
    const original = typeof graph.d3AlphaTarget === 'function'
      ? graph.d3AlphaTarget.bind(graph) : null;
    if (original) graph.d3AlphaTarget = (value, ...args) => {
      if (Number(value) > 0) window.__softKickCount += 1;
      return original(value, ...args);
    };
    const repel = document.querySelector('input[data-graph-setting="repel"]');
    const gravity = document.querySelector('input[data-graph-setting="gravity"]');
    [180, 260, 340, 420, 500].forEach(value => {
      repel.value = String(value);
      repel.dispatchEvent(new Event('input', { bubbles: true }));
      gravity.value = String(Math.min(40, Math.round(value / 12)));
      gravity.dispatchEvent(new Event('input', { bubbles: true }));
    });
  });
  await page.waitForTimeout(100);
  const softKicks = await page.evaluate(() => window.__softKickCount);
  if (softKicks !== null) expect(softKicks).toBeLessThanOrEqual(2);

  const samples = [];
  for (const delay of [100, 250, 600, 1_000]) {
    await page.waitForTimeout(delay);
    samples.push(await page.evaluate(() => {
      const canvas = document.querySelector('#graph-net canvas');
      const nodes = window.__fg.graphData().nodes;
      return {
        maxSpeed: Math.max(...nodes.map(node => Math.hypot(node.vx || 0, node.vy || 0))),
        finite: nodes.every(node => [node.x, node.y, node.vx, node.vy]
          .every(value => Number.isFinite(value))),
        zoom: { k: canvas.__zoom.k, x: canvas.__zoom.x, y: canvas.__zoom.y },
        diagnostics: window.__engraphisGraph.physicsDiagnostics(),
      };
    }));
  }
  expect(samples.every(sample => sample.finite && sample.maxSpeed <= 50)).toBe(true);
  expect(samples.every(sample => Math.abs(sample.zoom.k - before.k) < 0.001
    && Math.abs(sample.zoom.x - before.x) < 0.5
    && Math.abs(sample.zoom.y - before.y) < 0.5)).toBe(true);
  expect(samples.every(sample => sample.diagnostics.reheatStepsRemaining === 0
    && sample.diagnostics.reheatStepsApplied === before.diagnostics.reheatStepsApplied
    && sample.diagnostics.lastReheatSubsteps === 0)).toBe(true);
});

test('reduced visual motion does not start the opt-in graph frozen', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await openDashboard(page, { query: '?graph-engine=next' });
  await openGraphView(page);
  await page.waitForFunction(() => window.__fg && window.__fg.graphData().nodes
    .every(node => Number.isFinite(node.x) && Number.isFinite(node.y)));

  const positions = () => page.evaluate(() => window.__fg.graphData().nodes
    .map(node => ({ x: node.x, y: node.y })));
  const started = await positions();
  await page.waitForTimeout(750);
  const live = await positions();

  const greatestMovement = Math.max(...live.map((node, index) => Math.hypot(
    node.x - started[index].x, node.y - started[index].y,
  )));
  expect(greatestMovement).toBeGreaterThan(0.5);
});

test('Classic graph view produces zero CSP violations', async ({ page }) => {
  /* The Classic renderer extracts all styles to same-origin CSS files loaded via <link> tags,
     so no inline <style> elements or style attributes are injected at runtime. Combined with
     a strict CSP that includes `style-src 'self'` and `style-src-attr 'none'`, this ensures
     the graph view is fully CSP-clean. Any violation would indicate a regression to inline
     style injection. */
  const session = await openDashboard(page);
  await openGraphView(page);
  await page.waitForTimeout(2_000);
  const violations = await session.violations();

  expect(violations).toEqual([]);
  expect(session.pageErrors).toEqual([]);
});

test('Classic does not expose a complete graph control', async ({ page }) => {
  const session = await openDashboard(page);
  await openGraphView(page);

  await expect(page.locator('#graph-show-all')).toHaveCount(0);
  await expect(page.locator('#graph-show-iso')).toBeEnabled();
  expect(await page.evaluate(() => typeof GRAPH_FULL)).toBe('undefined');
  expect(session.pageErrors).toEqual([]);
});

test.describe('Opt-in canvas graph engine helper contracts', () => {
  test('renders a canvas without uncaught errors or application CSP violations', async ({ page }) => {
    const session = await openDashboard(page, { query: '?graph-engine=next' });
    const canvas = await openGraphView(page);

    await expect(canvas).toBeVisible();
    expect(session.consoleErrors).toEqual([]);
    expect(session.pageErrors).toEqual([]);
    // force-graph may emit its known vendor stylesheet CSP reports when it attaches. The
    // application renderer must not add any inline-script, inline-style, or other violations.
    const unexpectedViolations = (await session.violations())
      .filter(violation => violation.directive !== 'style-src-elem');
    expect(unexpectedViolations).toEqual([]);
  });

  test('keeps galaxy systems finite and separated inside the rendered envelope', async ({ page }) => {
    await openDashboard(page, {
      query: '?graph-engine=next',
      graphScene: blackHoleGalaxyScene,
    });
    await openGraphView(page);
    await page.evaluate(scene => {
      window.__engraphisGraph.setPreset('galaxy');
      window.__engraphisGraph.setSettings({ gravity: 48 });
      window.__engraphisGraph.setData(scene);
      window.__engraphisGraph.setScope({ showUnlinked: true, minDegree: 0 });
    }, blackHoleGalaxyScene);
    await page.waitForFunction(() => window.__fg.graphData().nodes.length === 8
      && window.__engraphisGraph.physicsDiagnostics().steps >= 5);

    const systems = await galaxySystemSnapshot(page);
    expect(systems.finite).toBe(true);

    const envelope = await renderedSystemEnvelopeSnapshot(page);
    expect(envelope.systems.length).toBe(3);
    expect(envelope.systems.every(system => system.members === 2)).toBe(true);
    expect(envelope.systems.every(system => system.visible)).toBe(true);
    expect(envelope.finite).toBe(true);
    expect(envelope.overlaps).toBe(0);
  });

  test('observes orbital phase motion in the rendered stellar snapshot', async ({ page }) => {
    await openDashboard(page, {
      query: '?graph-engine=next',
      graphScene: blackHoleGalaxyScene,
    });
    await openGraphView(page);
    await page.evaluate(scene => {
      window.__engraphisGraph.setPreset('galaxy');
      window.__engraphisGraph.setSettings({ gravity: 48 });
      window.__engraphisGraph.setData(scene);
      window.__engraphisGraph.setScope({ showUnlinked: true, minDegree: 0 });
    }, blackHoleGalaxyScene);
    await page.waitForFunction(() => window.__fg.graphData().nodes.length === 8
      && window.__engraphisGraph.physicsDiagnostics().steps >= 5);

    const initial = await renderedStellarSnapshot(page, 'aurora');
    expect(initial.finite).toBe(true);
    const targetStep = Number(initial.diagnostics.steps || 0) + 12;
    await page.waitForFunction(step => window.__engraphisGraph
      && window.__engraphisGraph.physicsDiagnostics().steps >= step,
    targetStep, { timeout: 10_000 });
    const updated = await renderedStellarSnapshot(page, 'aurora');

    expect(updated.finite).toBe(true);
    expect(updated.visible).toBe(true);
    expect(updated.phase).not.toBe(initial.phase);
    expect(Math.abs(signedAngleDelta(initial.phase, updated.phase))).toBeGreaterThan(0.01);
  });

  test('rejects a rendered stellar snapshot without its global anchor', async ({ page }) => {
    await openDashboard(page, {
      query: '?graph-engine=next',
      graphScene: blackHoleGalaxyScene,
    });
    await openGraphView(page);
    await page.evaluate(scene => {
      const api = window.__engraphisGraph;
      api.freeze(true);
      api.setPreset('galaxy');
      api.setSettings({ gravity: 48 });
      api.setData(scene);
      api.setScope({ showUnlinked: true, minDegree: 0 });
    }, blackHoleGalaxyScene);
    await page.waitForFunction(() => window.__fg.graphData().nodes.length === 8
      && window.__engraphisGraph.physicsDiagnostics().frozen);

    await page.evaluate(() => {
      const globalAnchor = window.__fg.graphData().nodes.find(
        node => node.anchor_role === 'global',
      );
      globalAnchor.anchor_role = 'none';
    });
    const snapshot = await renderedStellarSnapshot(page, 'aurora');

    expect(snapshot.globalAnchorCount).toBe(0);
    expect(snapshot.anchorValid).toBe(false);
    expect(snapshot.anchor).toBeNull();
    expect(snapshot.finite).toBe(false);
  });
});
