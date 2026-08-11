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

/**
 * Stub the dashboard's API surface and start recording everything a browser can tell us that
 * a Node harness cannot: which scripts were fetched, which CSP rules fired, and what the page
 * logged.  Returns the recorders so each test can assert on them.
 */
async function openDashboard(page, { query = '' } = {}) {
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
    if (message.type() === 'error') consoleErrors.push(message.text());
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
    if (path === '/graph/scene') return json(graphScenePayload);
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
  const canvas = page.locator('#graph-net canvas').first();
  await expect(canvas).toBeAttached({ timeout: 20_000 });
  await page.waitForFunction(() => {
    const c = document.querySelector('#graph-net canvas');
    return c && c.width > 0 && c.height > 0;
  }, null, { timeout: 20_000 });
  return canvas;
}

/* Measure the hierarchy in graph space, where zoom-to-fit cannot fake orbital motion. System
   centres are evidence-mass weighted, matching the runtime force and server scene contract. */
async function galaxySystemSnapshot(page) {
  return page.evaluate(() => {
    const nodes = window.__fg.graphData().nodes.filter(node => !node.ghost);
    const anchor = nodes.slice().sort((left, right) => {
      const leftGlobal = left.anchor_role === 'global' ? 1 : 0;
      const rightGlobal = right.anchor_role === 'global' ? 1 : 0;
      return rightGlobal - leftGlobal
        || Number(right.gravity_mass || 0) - Number(left.gravity_mass || 0)
        || String(left.id).localeCompare(String(right.id));
    })[0];
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
      diagnostics: window.__engraphisGraph.physicsDiagnostics(),
      d3Budget: {
        time: typeof window.__fg.cooldownTime === 'function' ? window.__fg.cooldownTime() : null,
        ticks: typeof window.__fg.cooldownTicks === 'function' ? window.__fg.cooldownTicks() : null,
      },
      finite: nodes.every(node => [node.x, node.y, node.vx, node.vy]
        .every(value => Number.isFinite(value))),
    };
  });
}

function signedAngleDelta(from, to) {
  return Math.atan2(Math.sin(to - from), Math.cos(to - from));
}

async function reducedGravityTrial(page, gravity, stepCount = 8) {
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
    maximum: window.EngraphisGraph._internals.galaxyBlackHoleGravityConstant(100),
    localBaseline: window.EngraphisGraph._internals.galaxyLocalGravityConstant(48),
    localMaximum: window.EngraphisGraph._internals.galaxyLocalGravityConstant(100),
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

async function reducedOrbitalSeparationTrial(page, separation, stepCount = 8) {
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
      community_id: 'aurora', anchor_role: 'none', system_anchor_id: 'aurora-star',
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
  await page.mouse.move(drag.x + 80, drag.y + 40, { steps: 8 });
  await page.mouse.up();
  const after = await page.evaluate(() => {
    const node = window.__fg.graphData().nodes[0];
    return { x: node.x, y: node.y, fx: node.fx, fy: node.fy };
  });

  expect(after.x - drag.before.x).toBeCloseTo(80 / drag.zoom, 0);
  expect(after.y - drag.before.y).toBeCloseTo(40 / drag.zoom, 0);
  expect(after.fx).toBeUndefined();
  expect(after.fy).toBeUndefined();
  expect(session.pageErrors).toEqual([]);
});

test('a dashboard page that never opens the graph fetches neither graph script', async ({ page }) => {
  // The reason both scripts are lazy is not weight, it is CSP: force-graph applies inline
  // styles at runtime, so an eager <script> reported a violation on every page view — including
  // the views that have no graph.  This asserts the deferral in the only place it is real.
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

test('Galaxy motion is another 30 percent slower while core perturbation stays bound', async ({ page }) => {
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
    const start = nodes.map(node => ({ ...node }));
    const half = start.map(node => ({ ...node }));
    const old = start.map(node => ({ ...node }));
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
      I.integrateGalaxyLeapfrog(half, [], [], options(0.021328125));
      I.integrateGalaxyLeapfrog(old, [], [], options(0.03046875));
    }
    const halfPhase = phase(half), oldPhase = phase(old);
    const halfTurns = {
      system: Math.abs(delta(initialPhase.system, halfPhase.system)),
      local: Math.abs(delta(initialPhase.local, halfPhase.local)),
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
      .map(node => ({ ...node }));
    const initialCoreRadius = Math.hypot(
      coreOrbit[1].x - coreOrbit[0].x, coreOrbit[1].y - coreOrbit[0].y,
    );
    let minimumCoreRadius = initialCoreRadius;
    let maximumCoreRadius = initialCoreRadius;
    let speedCaps = 0;
    for (let step = 0; step < 450; step += 1) {
      const tick = I.integrateGalaxyLeapfrog(coreOrbit, [], [], {
        ...options(0.021328125), central: false,
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
      halfTurns,
      oldTurns,
      ratios: {
        system: halfTurns.system / oldTurns.system,
        local: halfTurns.local / oldTurns.local,
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

  expect(report.diagnostics.timestep).toBe(0.021328125);
  expect(report.diagnostics.frameIntervalMs).toBeCloseTo(1000 / 30, 8);
  expect(report.halfTurns.system).toBeGreaterThan(0);
  expect(report.halfTurns.local).toBeGreaterThan(0);
  expect(report.ratios.system).toBeGreaterThan(0.65);
  expect(report.ratios.system).toBeLessThan(0.75);
  expect(report.ratios.local).toBeGreaterThan(0.65);
  expect(report.ratios.local).toBeLessThan(0.75);
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
  expect(finalResume.after).toEqual(finalResume.before);
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
  expect(evolved.diagnostics.maxSpeed).toBeLessThanOrEqual(48);
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
  expect(during.unlinkedTowardDrag).toBeGreaterThan(0);
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

test('Galaxy sliders span density plus doubled Link and Orbital separation', async ({ page }) => {
  // This test exercises the live fixed-step solver; reduced motion intentionally disables it.
  await page.emulateMedia({ reducedMotion: 'no-preference' });
  await openDashboard(page, { query: '?graph-engine=next' });
  await openGraphView(page);
  await page.waitForFunction(() => window.__engraphisGraph && window.__fg);
  const baseline = await reducedGravityTrial(page, 48);
  const strong = await reducedGravityTrial(page, 100);
  const compactOrbits = await reducedOrbitalSeparationTrial(page, 0);
  const separatedOrbits = await reducedOrbitalSeparationTrial(page, 120, 16);
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
    api.setSettings({ gravity: 100, size: 1 });
    const after = { radii: radii(), diameter: diameter(), velocities: velocities(),
      diagnostics: api.physicsDiagnostics() };
    api.freeze(true);
    return {
      before, after,
      expectedRatio: I.galaxyImmediateGravityRadiusScale(100)
        / I.galaxyImmediateGravityRadiusScale(48),
    };
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
    const maximum = measure(100);
    return {
      baseline, maximum,
      ratios: Object.fromEntries(Object.keys(baseline).map(id => [id,
        maximum[id] / baseline[id]])),
      densityFactors: [0, 48, 100].map(gravity =>
        window.EngraphisGraph._internals.galaxyInwardConvergenceFactor(60, gravity)),
      linkScales: [4, 8, 80].map(setting =>
        window.EngraphisGraph._internals.galaxyRelationOrbitScale(setting)),
    };
  }, blackHoleGalaxyScene);

  expect(baseline.curve.setting).toBe(48);
  expect(strong.curve.setting).toBe(100);
  expect(baseline.curve.baseline).toBe(240);
  expect(baseline.curve.maximum).toBe(864);
  expect(baseline.curve.localBaseline).toBe(120);
  expect(baseline.curve.localMaximum).toBe(432);
  expect(baseline.curve.localBaseline).toBe(baseline.curve.baseline * 0.5);
  expect(baseline.curve.localMaximum).toBe(baseline.curve.maximum * 0.5);
  expect(baseline.curve.maximum / baseline.curve.baseline).toBe(3.6);
  expect(baseline.before.diagnostics.gravitySetting).toBe(48);
  expect(baseline.before.diagnostics.effectiveGravity).toBe(240);
  expect(baseline.before.diagnostics.blackHoleGravity).toBe(240);
  expect(baseline.before.diagnostics.localGravity).toBe(120);
  expect(strong.before.diagnostics.gravitySetting).toBe(100);
  expect(strong.before.diagnostics.effectiveGravity).toBe(864);
  expect(strong.before.diagnostics.blackHoleGravity).toBe(864);
  expect(strong.before.diagnostics.localGravity).toBe(432);
  expect(compactOrbits.before.diagnostics.orbitalSeparationSetting).toBe(0);
  expect(compactOrbits.before.diagnostics.orbitalSeparationPadding).toBe(0);
  expect(compactOrbits.before.diagnostics.orbitalSeparationStrength).toBe(0);
  expect(separatedOrbits.before.diagnostics.orbitalSeparationSetting).toBe(120);
  expect(separatedOrbits.before.diagnostics.orbitalSeparationPadding).toBe(30);
  expect(separatedOrbits.before.diagnostics.orbitalSeparationStrength).toBe(1);
  expect(separatedOrbits.maximumSeparations).toBeGreaterThan(0);
  expect(separatedOrbits.nonAnchorAfter).toBeGreaterThan(
    compactOrbits.nonAnchorAfter * 1.35,
  );
  // Repel remains active for the linked non-anchor planet↔moon contact, but it must not
  // turn the dominant star↔planet orbit into the old large-radius slider cushion.
  expect(separatedOrbits.starPlanetAfter).toBeLessThan(separatedOrbits.starPlanetRepelTarget);
  expect(separatedOrbits.minimumSystemAnchorClearance).toBeGreaterThanOrEqual(0);
  expect(Math.max(...separatedOrbits.corrections.slice(-4))).toBeLessThan(
    Math.max(...separatedOrbits.corrections.slice(0, 4)) * 0.05,
  );
  expect(baseline.before.diagnostics.linkSetting).toBe(8);
  expect(baseline.before.diagnostics.relationOrbitScale).toBeCloseTo(0.25, 12);
  expect(physicalField.densityFactors[0]).toBeCloseTo(1, 12);
  expect(physicalField.densityFactors[1]).toBeCloseTo(0.75 ** 0.455, 12);
  expect(physicalField.densityFactors[2]).toBeCloseTo(0.75 ** (3.6 * 0.455), 12);
  expect(physicalField.linkScales).toEqual([1 / 16, 0.25, 25]);
  expect(immediate.after.diagnostics.immediateGravityResponse.systems).toBe(3);
  expect(immediate.after.diagnostics.immediateGravityResponse.maximumShift).toBeGreaterThan(10);
  expect(immediate.after.diagnostics.immediateGravityResponse.ratio).toBeCloseTo(
    immediate.expectedRatio, 12,
  );
  for (const [id, radius] of Object.entries(immediate.before.radii)) {
    expect(immediate.after.radii[id] / radius, id).toBeCloseTo(immediate.expectedRatio, 10);
  }
  expect(immediate.after.diameter).toBeCloseTo(immediate.before.diameter, 10);
  expect(immediate.after.velocities).toEqual(immediate.before.velocities);
  expect(baseline.steps).toBeGreaterThanOrEqual(8);
  expect(strong.steps).toBeGreaterThanOrEqual(8);
  expect(Math.abs(strong.steps - baseline.steps)).toBeLessThanOrEqual(2);
  for (const [id, ratio] of Object.entries(physicalField.ratios)) {
    expect(physicalField.baseline[id], id).toBeGreaterThan(0);
    expect(physicalField.maximum[id], id).toBeGreaterThan(0);
    expect(ratio, id).toBeCloseTo(3.6, 10);
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
      // Every observed sample is non-increasing: the black-hole boundary cannot let a solar
      // system fall away. The endpoint remains visibly bounded rather than collapsing it.
      expect(radii.every((radius, index) => index === 0 || radius <= radii[index - 1] + 1e-6),
        JSON.stringify({ id: systemBefore.id, radii })).toBe(true);
      const item = track.at(-1);
      expect(item.radius, systemBefore.id).toBeLessThan(systemBefore.radius);
      expect(item.radius, systemBefore.id).toBeGreaterThan(systemBefore.radius * 0.6);
      expect(item.internalDiameter, systemBefore.id).toBeGreaterThan(8);
      /* Link's new positional constraint deliberately reaches the selected tight scale
         immediately. Keep a substantial, visible local orbit without restoring the old
         50% floor that made the tighter default fail by construction. */
      expect(item.internalDiameter, systemBefore.id).toBeGreaterThan(
        systemBefore.internalDiameter * 0.45,
      );
    }
  }

  const contraction = trial => trial.before.systems.reduce((total, beforeSystem) => {
    const afterSystem = trial.after.systems.find(system => system.id === beforeSystem.id);
    return total + (1 - afterSystem.radius / beforeSystem.radius);
  }, 0) / trial.before.systems.length;
  expect(contraction(strong)).toBeGreaterThan(contraction(baseline) * 2);

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
  expect(anchorGeometry.radius).toBeGreaterThanOrEqual(anchorGeometry.ordinary * 2);
  expect(anchorGeometry.x).toBe(0);
  expect(anchorGeometry.y).toBe(0);

  // Force-graph's shadow canvas is the real hit-test path. Waiting through its throttle and
  // clicking the graph-space origin proves the doubled star is interaction geometry, not paint.
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

test('Ledger Gravity slider changes Galaxy density synchronously', async ({ page }) => {
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
    control.value = '100';
    control.dispatchEvent(new Event('input', { bubbles: true }));
    const after = radii();
    const diagnostics = api.physicsDiagnostics();
    const output = document.querySelector('#graph-gravity-output').textContent;
    api.freeze(true);
    return {
      before, after,
      expectedRatio: I.galaxyImmediateGravityRadiusScale(100)
        / I.galaxyImmediateGravityRadiusScale(48),
      diagnostics, output,
    };
  }, blackHoleGalaxyScene);

  expect(report.output).toBe('100');
  expect(report.diagnostics.gravitySetting).toBe(100);
  expect(report.diagnostics.immediateGravityResponse.systems).toBe(3);
  expect(report.diagnostics.immediateGravityResponse.maximumShift).toBeGreaterThan(10);
  for (const [id, radius] of Object.entries(report.before)) {
    expect(report.after[id] / radius, id).toBeCloseTo(report.expectedRatio, 10);
  }
  expect(session.pageErrors).toEqual([]);
});

test('Reheat layout control advances a bounded Galaxy relaxation burst', async ({ page }) => {
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
    return diagnostics.reheatActivations > previous.activations
      && diagnostics.reheatStepsApplied >= previous.applied + 6;
  }, {
    activations: before.diagnostics.reheatActivations,
    applied: before.diagnostics.reheatStepsApplied,
  });
  const after = await page.evaluate(() => {
    const star = window.__fg.graphData().nodes.find(node => node.id === 'cygnus-star');
    return {
      phase: [star.x, star.y, star.vx || 0, star.vy || 0],
      diagnostics: window.__engraphisGraph.physicsDiagnostics(),
      d3: window.__explicitReheatD3,
    };
  });
  expect(after.diagnostics.reheatActivations).toBe(before.diagnostics.reheatActivations + 1);
  expect(after.diagnostics.reheatStepsApplied).toBeGreaterThanOrEqual(
    before.diagnostics.reheatStepsApplied + 6,
  );
  expect(after.diagnostics.reheatStepsRemaining).toBeLessThan(30);
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

test('repel and gravity slider bursts coalesce into one bounded reheat', async ({ page }) => {
  await openDashboard(page, { query: '?graph-engine=next' });
  await openGraphView(page);
  await page.waitForFunction(() => window.__fg && window.__fg.graphData().nodes
    .every(node => Number.isFinite(node.x) && Number.isFinite(node.y)));
  await page.waitForTimeout(1_800);

  const before = await page.evaluate(() => {
    const canvas = document.querySelector('#graph-net canvas');
    return { k: canvas.__zoom.k, x: canvas.__zoom.x, y: canvas.__zoom.y };
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
      };
    }));
  }
  expect(samples.every(sample => sample.finite && sample.maxSpeed <= 50)).toBe(true);
  expect(samples.every(sample => Math.abs(sample.zoom.k - before.k) < 0.001
    && Math.abs(sample.zoom.x - before.x) < 0.5
    && Math.abs(sample.zoom.y - before.y) < 0.5)).toBe(true);
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

test('the canonical engine limits CSP violations to vendor stylesheets', async ({ page }) => {
  /* Opening the graph is *not* CSP-clean and this PR does not make it so: force-graph injects
     a handful of `<style>` elements when it attaches, which `style-src 'self'` blocks.  The
     scripts are lazy so that cost is confined to the graph view instead of every dashboard
     load.  What must stay true is that the canonical renderer adds nothing on top: it owns
     only canvas paint, and any inline style of its own would show up here.
     `style-src-attr 'none'` in particular admits no escape hatch at all. */
  const session = await openDashboard(page, { query: '?graph-engine=next' });
  await openGraphView(page);
  await page.waitForTimeout(2_000);
  const violations = await session.violations();

  // Every one is an injected vendor stylesheet, not an inline style attribute: the renderer
  // itself must never reach for `element.style`, which is what this drift gate enforces.
  expect(violations.every(v => v.directive === 'style-src-elem')).toBe(true);
  expect(session.pageErrors).toEqual([]);
});

test('Classic does not expose a complete graph control', async ({ page }) => {
  const session = await openDashboard(page);
  await openGraphView(page);

  await expect(page.locator('#graph-show-all')).toBeVisible();
  await expect(page.locator('#graph-show-iso')).toBeEnabled();
  expect(await page.evaluate(() => GRAPH_FULL)).toBe(false);
  expect(session.pageErrors).toEqual([]);
});
