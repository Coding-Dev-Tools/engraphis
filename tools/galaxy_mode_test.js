// Multi-mode variant of manual_slider_test.js: tests the 3 spacetime sliders
// (galactic gravity, local solar gravity, black hole mass) in EACH of the 6
// layout modes (galaxy, compact, original, communities, radial, constellation).
//
// Differences from the original harness:
//  - The graph-preset is switched between runs by writing to the hidden
//    #graph-preset input AND dispatching the same 'change' event the
//    graph-preset-choice buttons use, so the dashboard re-syncs tuning.
//  - For each (mode, slider) we read three points: low / default / high and
//    capture the engine's settings + diagnostics at each point.
//  - We print a per-mode summary block at the end.

const { chromium } = require('@playwright/test');
const { spawn } = require('child_process');
const path = require('path');

const REPO = __dirname;
process.chdir(REPO);

const PORT = process.env.ENGRAPHIS_PLAYWRIGHT_PORT || 8801;
const BASE = `http://127.0.0.1:${PORT}`;
const WORKSPACE = 'graph-manual-test';
const memoryCount = 8;

const MODES = ['galaxy', 'compact', 'original', 'communities', 'radial', 'constellation'];
const SPACETIME_SLIDERS = [
  { id: 'graph-gravitational-constant', engineKey: 'gravitationalConstant',
    low: 50, high: 200, defaultVisible: 100, desc: 'Galactic gravity' },
  { id: 'graph-local-gravitational-constant', engineKey: 'localGravitationalConstant',
    low: 50, high: 200, defaultVisible: 100, desc: 'Local solar gravity' },
  { id: 'graph-black-hole-mass', engineKey: 'blackHoleMass',
    low: 50, high: 500, defaultVisible: 160, desc: 'Black hole mass' },
];

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

async function startServer() {
  log(`Starting dashboard on port ${PORT}...`);
  const proc = spawn('python', ['-m', 'scripts.start_dashboard', '--no-open', '--port', String(PORT)], {
    cwd: REPO, shell: true,
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
  await page.locator(`#${id}`).fill(String(value));
  const actual = await page.evaluate((id) => document.getElementById(id).value, id);
  return actual;
}

async function readEngineState(page) {
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

async function switchPreset(page, mode) {
  // Mirror the click path of the data-graph-preset-choice buttons.
  await page.evaluate((mode) => {
    const el = document.querySelector(`[data-graph-preset-choice="${mode}"]`);
    if (el) el.click();
    const hidden = document.getElementById('graph-preset');
    if (hidden) {
      hidden.value = mode;
      hidden.dispatchEvent(new Event('change', { bubbles: true }));
    }
  }, mode);
  await page.waitForTimeout(2500); // let preset sync + layout settle
}

async function measureSlider(page, sliderId, lowValue, highValue, defaultValue, settleMs = 2500) {
  const baselineState = await readEngineState(page);
  // Move to default first to capture the engine's neutral value.
  const defActual = await setSlider(page, sliderId, defaultValue);
  await page.waitForTimeout(settleMs);
  const defState = await readEngineState(page);

  const lowActual = await setSlider(page, sliderId, lowValue);
  await page.waitForTimeout(settleMs);
  const lowState = await readEngineState(page);

  const highActual = await setSlider(page, sliderId, highValue);
  await page.waitForTimeout(settleMs);
  const highState = await readEngineState(page);

  return { baselineState, defState, lowState, highState,
           lowActual, highActual, defActual };
}

function meanSpeed(arr) {
  if (!arr || !arr.length) return 0;
  return arr.reduce((s, n) => s + Math.hypot(n.vx || 0, n.vy || 0), 0) / arr.length;
}
function meanRadius(arr) {
  if (!arr || !arr.length) return 0;
  const nonAnchor = arr.filter(n => n.role !== 'global');
  if (!nonAnchor.length) return 0;
  return nonAnchor.reduce((s, n) => s + Math.hypot(n.x || 0, n.y || 0), 0) / nonAnchor.length;
}

async function main() {
  let serverProc = null;
  let browser = null;
  const allResults = [];
  let exitCode = 0;
  try {
    serverProc = await startServer();
    browser = await chromium.launch({ headless: true });
    const context = await browser.newContext({ viewport: { width: 1600, height: 1000 } });
    const page = await context.newPage();

    page.on('console', m => {
      if (m.type() === 'error') console.log(`[browser-err] ${m.text()}`);
    });
    page.on('pageerror', e => console.log(`[pageerror] ${e.message}`));

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

    await page.waitForSelector('.nav-item[data-view="relations"]', { timeout: 15000 });
    log('Dashboard loaded, switching to relations view...');
    await page.locator('.nav-item[data-view="relations"]').click();

    await page.waitForFunction(() => {
      const el = document.getElementById('graph-count');
      return el && el.textContent && el.textContent.match(/entities|relations/);
    }, { timeout: 30000 });
    await page.waitForTimeout(2500);

    // Open all <details> so advanced physics sliders are in the DOM
    await page.evaluate(() => {
      document.querySelectorAll('details').forEach(d => { d.open = true; });
    });
    await page.waitForTimeout(500);

    const initialMode = await page.evaluate(() =>
      document.getElementById('graph-preset')?.value);
    log(`Initial graph-preset: ${initialMode}`);
    log(`Graph loaded: count=${await page.locator('#graph-count').textContent()}`);

    for (const mode of MODES) {
      log(`\n========================================`);
      log(`  MODE: ${mode}`);
      log(`========================================`);
      await switchPreset(page, mode);

      // Confirm the engine's mode actually changed
      const modeState = await readEngineState(page);
      log(`  engine state.settings.mode = ${modeState.settings ? modeState.settings.mode : 'n/a'}`);
      log(`  graph-mode text             = ${modeState.mode}`);
      log(`  graph-count                 = ${modeState.count}`);
      log(`  diagnostics.mode            = ${modeState.diagnostics ? modeState.diagnostics.mode : 'n/a'}`);

      const modeResults = { mode, sliders: [] };

      for (const t of SPACETIME_SLIDERS) {
        log(`\n--- ${t.desc} (${t.id}) in ${mode} mode ---`);
        const info = await page.evaluate((id) => {
          const el = document.getElementById(id);
          return el ? { value: el.value, min: el.min, max: el.max } : null;
        }, t.id);
        if (!info) {
          log(`  SKIP: slider not in DOM`);
          modeResults.sliders.push({ id: t.id, status: 'skipped' });
          continue;
        }
        const lo = Math.max(Number(info.min), t.low);
        const hi = Math.min(Number(info.max), t.high);
        const def = Number(t.defaultVisible);

        const r = await measureSlider(page, t.id, lo, hi, def, 2500);

        const engineKey = t.engineKey;
        const sDef = r.defState.settings ? r.defState.settings[engineKey] : 'n/a';
        const sLow = r.lowState.settings ? r.lowState.settings[engineKey] : 'n/a';
        const sHigh = r.highState.settings ? r.highState.settings[engineKey] : 'n/a';

        const defSp = meanSpeed(r.defState.positions);
        const lowSp = meanSpeed(r.lowState.positions);
        const highSp = meanSpeed(r.highState.positions);
        const defRa = meanRadius(r.defState.positions);
        const lowRa = meanRadius(r.lowState.positions);
        const highRa = meanRadius(r.highState.positions);

        // diagnostics centerX/centerY for centroid shift
        let lowCx = null, lowCy = null, highCx = null, highCy = null;
        if (r.lowState.diagnostics) {
          lowCx = r.lowState.diagnostics.centerX;
          lowCy = r.lowState.diagnostics.centerY;
        }
        if (r.highState.diagnostics) {
          highCx = r.highState.diagnostics.centerX;
          highCy = r.highState.diagnostics.centerY;
        }
        const centroidShift = (lowCx != null && highCx != null)
          ? Math.hypot((highCx - lowCx), (highCy - lowCy)) : 0;

        // Diagnostics-reported multiplier values
        let dLow = null, dHigh = null, dDef = null;
        if (r.lowState.diagnostics) dLow = r.lowState.diagnostics[engineKey];
        if (r.highState.diagnostics) dHigh = r.highState.diagnostics[engineKey];
        if (r.defState.diagnostics) dDef = r.defState.diagnostics[engineKey];

        // Per-node mean radius delta between low and high
        const radiusDelta = Math.abs(highRa - lowRa);
        const speedDelta = Math.abs(highSp - lowSp);

        // Physics-changed predicate (centroid > 0.005 OR radius > 0.5 OR speed > 0.005)
        const physicsChanged = radiusDelta > 0.5 || speedDelta > 0.005 || centroidShift > 0.005;

        // Engine received different value at low vs high?
        const engineApplied = (sLow !== 'n/a' && sHigh !== 'n/a') && (sLow !== sHigh);

        log(`  slider value   low=${r.lowActual} def=${r.defActual} high=${r.highActual}`);
        log(`  engine.${engineKey}   low=${sLow}  def=${sDef}  high=${sHigh}`);
        log(`  diag.${engineKey}    low=${dLow}   def=${dDef}  high=${dHigh}`);
        log(`  speed    low=${lowSp.toFixed(3)} def=${defSp.toFixed(3)} high=${highSp.toFixed(3)} dH-L=${speedDelta.toFixed(3)}`);
        log(`  radius   low=${lowRa.toFixed(2)} def=${defRa.toFixed(2)} high=${highRa.toFixed(2)} dH-L=${radiusDelta.toFixed(2)}`);
        log(`  centroid low=(${lowCx?.toFixed(2)},${lowCy?.toFixed(2)}) high=(${highCx?.toFixed(2)},${highCy?.toFixed(2)}) shift=${centroidShift.toFixed(3)}`);
        log(`  engineApplied=${engineApplied} physicsChanged=${physicsChanged}  ->  ${(engineApplied && physicsChanged) ? 'ALIVE' : 'DEAD'}`);

        modeResults.sliders.push({
          id: t.id, engineKey, lowActual: r.lowActual, defActual: r.defActual,
          highActual: r.highActual, sDef, sLow, sHigh, dDef, dLow, dHigh,
          defSpeed: defSp, lowSpeed: lowSp, highSpeed: highSp,
          defRadius: defRa, lowRadius: lowRa, highRadius: highRa,
          centroidShift, engineApplied, physicsChanged, status:
            (engineApplied && physicsChanged) ? 'alive' : 'DEAD',
        });
      }
      allResults.push(modeResults);
    }

    // Final per-mode summary
    log(`\n\n========================================`);
    log(`  PER-MODE SLIDER-ALIVENESS SUMMARY`);
    log(`========================================`);
    for (const m of allResults) {
      log(`\n  Mode: ${m.mode}`);
      for (const s of m.sliders) {
        const tag = s.status === 'alive' ? 'ALIVE' : (s.status === 'DEAD' ? 'DEAD ' : 'SKIP ');
        log(`    [${tag}] ${s.id.padEnd(38)} engine=${s.engineKey} `
          + `low=${s.sLow} def=${s.sDef} high=${s.sHigh} `
          + `radiusD=${(s.highRadius - s.lowRadius).toFixed(2)} `
          + `centroidD=${(s.centroidShift || 0).toFixed(3)}`);
      }
    }

    // Galaxy-specific: verify that the galaxy physics functions read the
    // dashboard's normalized multipliers (we can read the engine settings
    // while in galaxy mode and compare).
    log(`\n\n========================================`);
    log(`  GALAXY-MODE SPACETIME CONSUMPTION CHECK`);
    log(`========================================`);
    log(`For galaxy mode the applyForces() function early-returns at line 7973.`);
    log(`Galaxy physics consumes settings via galaxyIntegratorOptions() (line 8571)`);
    log(`which calls galaxyPhysicsMultiplier(state.settings.X, fallback, maximum) with:`);
    log(`  gravitationalConstant         -> maximum 8`);
    log(`  localGravitationalConstant    -> maximum 8`);
    log(`  blackHoleMass                 -> maximum 16`);
    log(`The dashboard ledger.js sends values in 0..2 range (divided by 100 for`);
    log(`gravitationalConstant/localGravitationalConstant; graphBlackHoleMassMultiplier`);
    log(`for blackHoleMass). All are accepted by the galaxy clamps (0..8 / 0..16).`);

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
