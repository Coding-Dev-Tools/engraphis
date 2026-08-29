// Manual UI test: verify that every dashboard slider actually changes the
// graph's physics when dragged in the browser. This catches regressions
// like the "black hole mass slider does nothing" bug where the slider
// value reached the engine but produced no visible effect.
//
// Follows the exact pattern of tests/e2e/ledger.spec.js:
//   - mockApi(page, options) intercepts the /api/* calls
//   - go to root (Ledger dashboard), click 'relations' tab
//   - wait for #graph-count to show entities
//   - drive sliders with page.locator('#graph-X').fill(value)
//   - read state from the diagnostics exposed via page.evaluate()

const { chromium } = require('@playwright/test');
const { spawn } = require('child_process');
const net = require('net');
const path = require('path');

const REPO = path.resolve(__dirname, '..');
process.chdir(REPO);

const configuredPort = process.env.ENGRAPHIS_PLAYWRIGHT_PORT;
let PORT = 0;
let BASE = '';
const WORKSPACE = 'graph-manual-test';
const memoryCount = 8;

function log(msg) { console.log(`[${new Date().toISOString().slice(11, 19)}] ${msg}`); }
function err(msg) { console.error(`[ERR] ${msg}`); }

async function waitForServer(url, timeoutMs = 60000) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    try {
      const res = await fetch(url);
      if (res.ok) return true;
    } catch (_) { /* not ready */ }
    await new Promise(r => setTimeout(r, 500));
  }
  return false;
}

async function reservePort() {
  const requestedPort = configuredPort === undefined ? 0 : Number(configuredPort);
  if (!Number.isInteger(requestedPort) || requestedPort < 0 || requestedPort > 65535) {
    throw new Error(`ENGRAPHIS_PLAYWRIGHT_PORT must be an integer from 0 to 65535; got ${configuredPort}`);
  }
  PORT = await new Promise((resolve, reject) => {
    const probe = net.createServer();
    probe.once('error', error => reject(new Error(
      `Dashboard port ${requestedPort} is unavailable: ${error.message}`,
    )));
    probe.listen(requestedPort, '127.0.0.1', () => {
      const address = probe.address();
      if (!address || typeof address !== 'object') {
        probe.close(() => reject(new Error('Could not determine the reserved dashboard port')));
        return;
      }
      probe.close(error => error ? reject(error) : resolve(address.port));
    });
  });
  BASE = `http://127.0.0.1:${PORT}`;
}

async function startServer() {
  await reservePort();
  log(`Starting dashboard on port ${PORT}...`);
  const proc = spawn('python', ['-m', 'scripts.start_dashboard', '--no-open', '--port', String(PORT)], {
    cwd: REPO, shell: false,
    env: {
      ...process.env,
      ENGRAPHIS_DB_PATH: ':memory:',
      ENGRAPHIS_EMBED_MODEL: '',
      ENGRAPHIS_LOOP_INTERVAL: '0',
      ENGRAPHIS_HOST: '127.0.0.1',
      ENGRAPHIS_SERVICE_MODE: 'customer',
    },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  proc.stdout.on('data', d => process.stdout.write(`[srv] ${d}`));
  proc.stderr.on('data', d => process.stderr.write(`[srv-err] ${d}`));
  const ready = await waitForServer(`${BASE}/api/health`);
  if (!ready) { proc.kill(); throw new Error('Server failed to start'); }
  log('Server ready');
  return proc;
}

const license = () => ({
  plan: 'local', features: [], known_features: {}, cloud_managed: false,
  trial: { used: false, trial_days: 3 },
});

const memories = [
  { id: 'mem1', workspace: WORKSPACE, mtype: 'semantic', subject_key: 'entity_a',
    claim_kind: 'observation', content: 'Ada Lovelace was a mathematician who worked on analytical engines.',
    title: 'Ada Lovelace', importance: 0.5 },
  { id: 'mem2', workspace: WORKSPACE, mtype: 'semantic', subject_key: 'entity_a',
    claim_kind: 'observation', content: 'Charles Babbage designed the Difference Engine in the 1800s.',
    title: 'Babbage', importance: 0.4 },
  { id: 'mem3', workspace: WORKSPACE, mtype: 'semantic', subject_key: 'entity_b',
    claim_kind: 'observation', content: 'SQLite is a file-based SQL database engine.',
    title: 'SQLite', importance: 0.5 },
  { id: 'mem4', workspace: WORKSPACE, mtype: 'semantic', subject_key: 'entity_b',
    claim_kind: 'observation', content: 'FTS5 is a SQLite extension for full-text search.',
    title: 'FTS5', importance: 0.4 },
  { id: 'mem5', workspace: WORKSPACE, mtype: 'semantic', subject_key: 'entity_c',
    claim_kind: 'observation', content: 'Note G in the Analytical Engine describes looping operations.',
    title: 'Note G', importance: 0.6 },
  { id: 'mem6', workspace: WORKSPACE, mtype: 'semantic', subject_key: 'entity_c',
    claim_kind: 'observation', content: 'Loom patterns inspired the punched-card input design.',
    title: 'Loom', importance: 0.4 },
];

async function setupApiMocks(page) {
  await page.route('**/api/**', async route => {
    const request = route.request();
    const requestUrl = new URL(request.url());
    const path = requestUrl.pathname.replace(/^\/api/, '');
    const ok = body => route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(body),
    });
    if (path === '/bootstrap') {
      return ok({
        license: license(),
        workspaces: [{ name: WORKSPACE, memories: memoryCount }],
        stats: {
          memories: memoryCount, total_rows: memoryCount, workspaces: 1, sessions: 1,
        },
        embedder: { semantic: true },
      });
    }
    if (path === '/stats') {
      return ok({
        memories: memoryCount, total_rows: memoryCount, workspaces: 1, sessions: 1,
        by_type: { semantic: memoryCount },
      });
    }
    if (path === '/workspaces') return ok({ workspaces: [{ name: WORKSPACE, memories: memoryCount }] });
    if (path === '/memories') {
      const ws = requestUrl.searchParams.get('workspace') || WORKSPACE;
      return ok({ workspace: ws, memories });
    }
    if (path === '/graph') {
      return ok({
        nodes: [], edges: [], communities: [], community_bridges: [],
        meta: { layout_seed: 7, scene_hash: 'test' },
      });
    }
    if (path === '/graph/scene') {
      return ok({
        nodes: [
          { id: 'black-hole', label: 'Black hole', gravity_mass: 100,
            visual_radius: 9, community_id: 'core', anchor_role: 'global',
            system_anchor_id: 'black-hole', orbit_tier: 0,
            galactic_radius: 0, galactic_preferred_radius: 0, galactic_target_radius: 0,
            x: 0, y: 0 },
          { id: 'p1', label: 'P1', gravity_mass: 1, visual_radius: 5,
            community_id: 'sys1', anchor_role: 'community',
            system_anchor_id: 'p1', orbit_tier: 0,
            galactic_radius: 50, galactic_preferred_radius: 50, galactic_target_radius: 50,
            x: 50, y: 0 },
          { id: 'p2', label: 'P2', gravity_mass: 1, visual_radius: 5,
            community_id: 'sys2', anchor_role: 'community',
            system_anchor_id: 'p2', orbit_tier: 0,
            galactic_radius: 70, galactic_preferred_radius: 70, galactic_target_radius: 70,
            x: 0, y: 70 },
          { id: 'm1', label: 'M1', gravity_mass: 0.5, visual_radius: 3,
            community_id: 'sys1', anchor_role: 'member',
            system_anchor_id: 'p1', orbit_tier: 1,
            galactic_radius: 50, galactic_preferred_radius: 50, galactic_target_radius: 50,
            x: 55, y: 5, orbit_radius: 8, orbit_phase: 0 },
          { id: 'm2', label: 'M2', gravity_mass: 0.5, visual_radius: 3,
            community_id: 'sys2', anchor_role: 'member',
            system_anchor_id: 'p2', orbit_tier: 1,
            galactic_radius: 70, galactic_preferred_radius: 70, galactic_target_radius: 70,
            x: -5, y: 72, orbit_radius: 6, orbit_phase: 1.57 },
        ],
        edges: [
          { from: 'black-hole', to: 'p1', rest_length: 50, spring_strength: 0.2 },
          { from: 'black-hole', to: 'p2', rest_length: 70, spring_strength: 0.2 },
          { from: 'p1', to: 'm1', rest_length: 8, spring_strength: 0.5 },
          { from: 'p2', to: 'm2', rest_length: 6, spring_strength: 0.5 },
        ],
        communities: [
          { id: 'core', label: 'Core', color: '#7bb4ff', size: 1 },
          { id: 'sys1', label: 'Sys 1', color: '#ffcf6b', size: 2 },
          { id: 'sys2', label: 'Sys 2', color: '#ff7ea8', size: 2 },
        ],
        community_bridges: [
          { id: 'b1', source_community: 'sys1', target_community: 'sys2',
            physics_strength: 0.6 },
        ],
        meta: { algorithm_version: 'galaxy-v6', layout_seed: 7, total_nodes: 5 },
      });
    }
    if (path === '/recall') return ok({ matches: [] });
    if (path === '/timeline') return ok({ events: [] });
    if (path === '/audit') return ok({ events: [] });
    if (path === '/receipts') return ok({ receipts: [] });
    if (path === '/context-savings') return ok({});
    return ok({});
  });
}

async function setSlider(page, id, value) {
  // Use fill() which triggers the proper input event handlers (per the e2e pattern)
  await page.locator(`#${id}`).fill(String(value));
  // For sliders with a non-integer step, the browser may snap the value to
  // the nearest valid step. Verify what the slider actually settled on.
  const actual = await page.evaluate((id) => document.getElementById(id).value, id);
  return actual;
}

async function getSliderOutput(page, id) {
  const v = await page.evaluate((id) => {
    const el = document.getElementById(id);
    return el ? el.textContent : null;
  }, id + '-output');
  return v;
}

async function getGraphNodeSnapshot(page) {
  return await page.evaluate(() => {
    // The engine is stored in module-internal state. We can read node positions
    // from the d3 simulation data via the global force-graph instance.
    const fg = window.__fg || (window.d3 && window.d3.simulation);
    if (!fg || !fg.nodes) return null;
    return fg.nodes().map(n => ({id: n.id, x: n.x, y: n.y, vx: n.vx, vy: n.vy}));
  });
}

async function readDiagnostics(page) {
  // The diagnostics are exposed via the UI's status text. We can also
  // read directly from the engine's physicsDiagnostics.
  return await page.evaluate(() => {
    const get = id => {
      const el = document.getElementById(id);
      return el ? el.textContent : null;
    };
    return {
      mode: get('graph-mode'),
      effectiveGravity: get('graph-a11y-help') || '',
      count: get('graph-count'),
      forceGraph: !!window.__fg,
    };
  });
}

async function readEngineState(page) {
  // Capture the engine's actual stored state via window.__engraphisGraph,
  // which is captured by the addInitScript proxy. Returns the engine's
  // current settings and physics diagnostics.
  return await page.evaluate(() => {
    const g = window.__engraphisGraph;
    if (!g) return {available: false, reason: 'no engine on window'};
    const result = {available: true};
    if (typeof g.state === 'function') {
      const st = g.state();
      result.settings = st.settings;
      result.minDegree = st.minDegree;
      result.depth = st.depth;
      result.sizeBy = st.sizeBy;
    }
    if (typeof g.physicsDiagnostics === 'function') {
      result.diagnostics = g.physicsDiagnostics();
    }
    if (typeof g.graphData === 'function') {
      const data = g.graphData();
      if (data && data.nodes) {
        result.nodeCount = data.nodes.length;
        result.positions = data.nodes.map(n => ({
          id: n.id, x: n.x, y: n.y, vx: n.vx, vy: n.vy, role: n.anchor_role,
        }));
      }
    }
    result.mode = document.getElementById('graph-mode')?.textContent || null;
    result.count = document.getElementById('graph-count')?.textContent || null;
    return result;
  });
}

async function measureSlider(page, sliderId, lowValue, highValue, settleMs = 2500) {
  // Read the slider output before any change
  const baselineOutput = await getSliderOutput(page, sliderId);

  // Drive low (capture the actual settled value because some sliders have non-integer steps)
  const lowActual = await setSlider(page, sliderId, lowValue);
  await page.waitForTimeout(settleMs);
  const lowOutput = await getSliderOutput(page, sliderId);
  const lowState = await readEngineState(page);
  const lowCount = await page.locator('#graph-count').textContent();
  // Hold the unchanged low setting for one equivalent interval. This gives
  // the physics engine a control-drift baseline for the high-vs-low sample.
  await page.waitForTimeout(settleMs);
  const baselineState = await readEngineState(page);

  // Drive high
  const highActual = await setSlider(page, sliderId, highValue);
  await page.waitForTimeout(settleMs);
  const highOutput = await getSliderOutput(page, sliderId);
  const highState = await readEngineState(page);
  const highCount = await page.locator('#graph-count').textContent();

  // settingsReached: the output element should match the slider value
  // (the output mirrors the slider via setGraphTuningControl/
  // setGraphSpacetimeControl). Use numeric equality so step-snapped values
  // (e.g. damping step=0.1) match even when one side is "0" and the other
  // is "0.0".
  const numEq = (a, b) => Number(a) === Number(b);
  const settingsReached = numEq(lowActual, lowValue)
    && numEq(highActual, highValue)
    && numEq(lowOutput, lowValue)
    && numEq(highOutput, highValue);

  return {
    sliderId, lowValue, highValue, lowActual, highActual,
    baselineOutput, lowOutput, highOutput,
    baselineState, lowState, highState,
    lowCount, highCount,
    settingsReached,
  };
}

async function main() {
  let serverProc = null;
  let browser = null;
  let exitCode = 0;
  try {
    serverProc = await startServer();
    browser = await chromium.launch({ headless: true });
    const context = await browser.newContext({ viewport: { width: 1600, height: 1000 } });
    const page = await context.newPage();

    page.on('console', m => {
      if (m.type() === 'error') {
        console.log(`[browser-err] ${m.text()}`);
      }
    });
    page.on('pageerror', e => console.log(`[pageerror] ${e.message}`));

    // Install an engine-capture proxy: intercept window.EngraphisGraph so
    // the Ledger dashboard's graphFactory.create() call yields an instance
    // we can read state from. This is the same pattern used in
    // tests/e2e/graph-engine.spec.js.
    await page.addInitScript(() => {
      let engine = null;
      let engineProxy = null;
      Object.defineProperty(window, 'EngraphisGraph', {
        configurable: true,
        get() { return engineProxy || undefined; },
        set(value) {
          engine = value;
          engineProxy = value && new Proxy(value, {
            get(target, property, receiver) {
              if (property === 'create') {
                return (...args) => {
                  const inst = target.create(...args);
                  window.__engraphisGraph = inst;
                  return inst;
                };
              }
              return Reflect.get(target, property, receiver);
            },
          });
        },
      });
    });

    await setupApiMocks(page);

    log('Loading dashboard...');
    await page.goto(BASE, { waitUntil: 'domcontentloaded' });

    // Wait for the relations nav-item to appear
    await page.waitForSelector('.nav-item[data-view="relations"]', { timeout: 15000 });
    log('Dashboard loaded, switching to relations view...');
    await page.locator('.nav-item[data-view="relations"]').click();

    // Wait for the graph to load
    await page.waitForFunction(() => {
      const el = document.getElementById('graph-count');
      return el && el.textContent && el.textContent.match(/entities|relations/);
    }, { timeout: 30000 });
    await page.waitForTimeout(2000);  // extra settle for initial layout

    log(`Graph loaded: count=${await page.locator('#graph-count').textContent()}`);
    log(`Mode: ${await page.locator('#graph-mode').textContent()}`);

    // Verify each slider exists in the DOM. The spacetime/black-hole-mass
    // sliders are inside a <details id="graph-spacetime-tuning"> that may be
    // collapsed. Open it (and all nested <details> for good measure) so
    // every advanced-physics slider is in the DOM.
    await page.evaluate(() => {
      document.querySelectorAll('details').forEach(d => { d.open = true; });
    });
    await page.waitForTimeout(500);

    const tests = [
      { id: 'graph-gravity', low: 24, high: 400, desc: 'Galactic gravity' },
      { id: 'graph-repel', low: 50, high: 400, desc: 'Orbital speed' },
      { id: 'graph-link', low: 4, high: 80, desc: 'Link distance' },
      { id: 'graph-gravitational-constant', low: 50, high: 200, desc: 'Gravitational constant' },
      { id: 'graph-local-gravitational-constant', low: 50, high: 200, desc: 'Local solar gravity' },
      { id: 'graph-black-hole-mass', low: 50, high: 500, desc: 'Black hole mass (USER COMPLAINT)' },
      { id: 'graph-space-damping', low: 0, high: 15, desc: 'Space friction' },
      { id: 'graph-spring-stiffness', low: 16, high: 100, desc: 'Spring stiffness' },
    ];

    const results = [];
    for (const t of tests) {
      log(`\n--- ${t.desc} (${t.id}) ---`);
      const info = await page.evaluate((id) => {
        const el = document.getElementById(id);
        if (!el) return null;
        return { value: el.value, min: el.min, max: el.max };
      }, t.id);
      if (!info) {
        // Try opening the spacetime tuning details and look again
        await page.evaluate(() => {
          const details = document.querySelectorAll('details');
          details.forEach(d => { d.open = true; });
        });
        await page.waitForTimeout(200);
        const info2 = await page.evaluate((id) => {
          const el = document.getElementById(id);
          return el ? { value: el.value, min: el.min, max: el.max } : null;
        }, t.id);
        if (!info2) {
          log(`  SKIP: slider not in DOM (even with all <details> open)`);
          results.push({ ...t, status: 'skipped' });
          continue;
        }
        log(`  (found after opening <details>)`);
        // fall through with info2
        Object.assign(info = info2, info2);
      }
      log(`  range: ${info.min}..${info.max}, default value: ${info.value}`);
      const lo = Math.max(Number(info.min), t.low);
      const hi = Math.min(Number(info.max), t.high);

      const r = await measureSlider(page, t.id, lo, hi, 2000);

      // Compute physics-deltas: total distance moved by all non-anchor nodes
      // between low and high.
      const meanSpeed = arr => {
        if (!arr || !arr.length) return 0;
        return arr.reduce((s, n) => s + Math.hypot(n.vx || 0, n.vy || 0), 0) / arr.length;
      };
      const meanRadius = arr => {
        if (!arr || !arr.length) return 0;
        const nonAnchor = arr.filter(n => n.role !== 'global');
        if (!nonAnchor.length) return 0;
        return nonAnchor.reduce((s, n) => s + Math.hypot(n.x || 0, n.y || 0), 0) / nonAnchor.length;
      };
      const lowSpeed = meanSpeed(r.lowState.positions);
      const highSpeed = meanSpeed(r.highState.positions);
      const baselineSpeed = meanSpeed(r.baselineState.positions);
      const lowRadius = meanRadius(r.lowState.positions);
      const highRadius = meanRadius(r.highState.positions);
      const baselineRadius = meanRadius(r.baselineState.positions);
      const centroid = arr => {
        const nonAnchor = (arr || []).filter(n => n.role !== 'global');
        if (!nonAnchor.length) return { x: 0, y: 0 };
        return {
          x: nonAnchor.reduce((s, n) => s + (n.x || 0), 0) / nonAnchor.length,
          y: nonAnchor.reduce((s, n) => s + (n.y || 0), 0) / nonAnchor.length,
        };
      };
      const distance = (a, b) => Math.hypot(a.x - b.x, a.y - b.y);
      const lowCentroid = centroid(r.lowState.positions);
      const baselineCentroid = centroid(r.baselineState.positions);
      const highCentroid = centroid(r.highState.positions);
      const noOpSpeedDrift = Math.abs(baselineSpeed - lowSpeed);
      const noOpRadiusDrift = Math.abs(baselineRadius - lowRadius);
      const noOpCentroidDrift = distance(baselineCentroid, lowCentroid);
      const drivenSpeedDelta = Math.abs(highSpeed - lowSpeed);
      const drivenRadiusDelta = Math.abs(highRadius - lowRadius);
      const drivenCentroidShift = distance(highCentroid, lowCentroid);

      // Engine state for low / high
      log(`  baseline output: ${r.baselineOutput}`);
      log(`  low  output:     ${r.lowOutput}  (input=${lo})`);
      log(`  high output:     ${r.highOutput}  (input=${hi})`);
      log(`  engine on window: ${r.lowState.available ? 'YES' : 'NO'}`);
      if (r.lowState.settings) {
        const keyMap = {
          'graph-gravity': 'gravity',
          'graph-repel': 'repel',
          'graph-link': 'link',
          'graph-gravitational-constant': 'gravitationalConstant',
          'graph-local-gravitational-constant': 'localGravitationalConstant',
          'graph-black-hole-mass': 'blackHoleMass',
          'graph-damping': 'damping',
          'graph-spring-stiffness': 'springStiffness',
        };
        const k = keyMap[t.id];
        log(`  engine settings.${k}  low=${r.lowState.settings[k]}  high=${r.highState.settings[k]}`);
      }
      if (r.lowState.diagnostics) {
        const d = r.lowState.diagnostics;
        const h = r.highState.diagnostics;
        log(`  physics.${Object.keys(d)[0] || '?'}: low=${JSON.stringify(d).slice(0, 80)}... high=${JSON.stringify(h).slice(0, 80)}...`);
      }
      log(`  per-node speed  low=${lowSpeed.toFixed(3)} high=${highSpeed.toFixed(3)} Δ=${drivenSpeedDelta.toFixed(3)} no-op drift=${noOpSpeedDrift.toFixed(3)}`);
      log(`  mean radius      low=${lowRadius.toFixed(2)} high=${highRadius.toFixed(2)} Δ=${drivenRadiusDelta.toFixed(2)} no-op drift=${noOpRadiusDrift.toFixed(2)}`);

      // A slider is "alive" if:
      //   (a) its DOM output reflects the typed value (basic wiring), AND
      //   (b) the engine's stored settings reflect the typed value (real wiring).
      const settingsKey = ({
        'graph-gravity': 'gravity', 'graph-repel': 'repel', 'graph-link': 'link',
        'graph-gravitational-constant': 'gravitationalConstant',
        'graph-local-gravitational-constant': 'localGravitationalConstant',
        'graph-black-hole-mass': 'blackHoleMass',
        'graph-space-damping': 'damping', 'graph-spring-stiffness': 'springStiffness',
      })[t.id];
      let engineApplied = false;
      if (r.lowState.settings && settingsKey) {
        // The engine receives the value AFTER graphSliderResponseValue +
        // graphSpacetimeEngineSettings, so it may differ from the raw input.
        // We just check that the low vs high engine values differ.
        const lv = r.lowState.settings[settingsKey];
        const hv = r.highState.settings[settingsKey];
        engineApplied = lv !== hv;
        log(`  engine ${settingsKey} differs: ${engineApplied ? 'YES' : 'NO'} (low=${lv} high=${hv})`);
      }
      // Physics changed only when the high-vs-low delta exceeds the drift
      // measured during an unchanged low-setting interval. This prevents
      // ordinary simulation evolution from being credited to the slider.
      const physicsChanged = drivenSpeedDelta > Math.max(0.005, noOpSpeedDrift * 1.25)
        || drivenRadiusDelta > Math.max(0.5, noOpRadiusDrift * 1.25)
        || drivenCentroidShift > Math.max(0.005, noOpCentroidDrift * 1.25);
      log(`  centroid shift:    ${drivenCentroidShift.toFixed(4)} world units (no-op drift=${noOpCentroidDrift.toFixed(4)})`);
      log(`  physics actually changed: ${physicsChanged ? 'YES' : 'no'}`);

      const alive = r.settingsReached && engineApplied && physicsChanged;
      results.push({...t, ...r, lowSpeed, highSpeed, lowRadius, highRadius,
        engineApplied, physicsChanged, status: alive ? 'alive' : 'DEAD'});
    }

    log('\n========================================');
    log('  SLIDER UI TEST SUMMARY');
    log('========================================');
    let passed = 0, failed = 0, skipped = 0;
    for (const r of results) {
      const tag = r.status === 'alive' ? '[OK]   '
        : r.status === 'DEAD' ? '[FAIL] ' : '[SKIP] ';
      if (r.status === 'alive') passed++;
      else if (r.status === 'DEAD') failed++;
      else skipped++;
      log(`${tag} ${r.id.padEnd(38)} wired=${r.settingsReached?'Y':'N'} engine=${r.engineApplied?'Y':'N'} physics=${r.physicsChanged?'Y':'N'} speedΔ=${(r.highSpeed - r.lowSpeed).toFixed(3)} radiusΔ=${(r.highRadius - r.lowRadius).toFixed(2)}`);
    }
    log(`\nResult: ${passed} alive, ${failed} dead, ${skipped} skipped`);
    if (skipped > 0) {
      // A skipped slider means the DOM no longer contains the element
      // we were supposed to verify. Treat that as a failure: the harness
      // cannot claim to have verified every slider if some were absent.
      err(`${skipped} slider(s) skipped because they were absent from the DOM (dashboard changed shape).`);
      exitCode = 1;
    }

    // Take a screenshot showing the final state
    await page.screenshot({ path: path.join(REPO, 'dashboard_slider_test.png'), fullPage: true });
    log('Screenshot saved: dashboard_slider_test.png');

    if (failed > 0) {
      err(`${failed} slider(s) are dead (value doesn't reach engine or has no physics effect)!`);
      exitCode = 1;
    } else {
      log('All tested sliders wire through to the engine and have measurable physics impact.');
    }
  } catch (e) {
    err(`Test failed: ${e.message}`);
    err(e.stack);
    exitCode = 2;
  } finally {
    if (browser) await browser.close();
    if (serverProc) serverProc.kill();
  }
  process.exit(exitCode);
}

main();
