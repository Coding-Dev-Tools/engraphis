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
    { id: 'ada', label: 'Ada Lovelace', etype: 'person_or_concept', degree: 3 },
    { id: 'engine', label: 'Analytical Engine', etype: 'artifact', degree: 3 },
    { id: 'babbage', label: 'Charles Babbage', etype: 'person_or_concept', degree: 2 },
    { id: 'notes', label: 'Note G', etype: 'artifact', degree: 2 },
    { id: 'sqlite', label: 'SQLite', etype: 'technology', degree: 2 },
    { id: 'fts', label: 'FTS5', etype: 'technology', degree: 2 },
    { id: 'store', label: 'Store', etype: 'artifact', degree: 2 },
    // Deliberately unlinked: the default scope must hide it, which is also what proves the
    // canvas is rendering the filtered view rather than the raw response.
    { id: 'orphan', label: 'Unreferenced entity', etype: 'person_or_concept', degree: 0 },
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
    if (path === '/health') return json({ status: 'ok' });
    if (path === '/stats') return json({ memories: 12, total_rows: 12, workspaces: 1, sessions: 1, by_type: {} });
    if (path === '/workspaces') return json({ workspaces: [{ name: workspace, memories: 12 }] });
    // Everything else the dashboard polls on boot: an empty, successful answer keeps the
    // console clean so a real error is not lost in expected noise.
    return json({});
  });

  await page.goto(`/${query}`);
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

test('the opt-in engine renders a real canvas and registers only under its flag', async ({ page }) => {
  const session = await openDashboard(page, { query: '?graph-engine=next' });
  const canvas = await openGraphView(page);

  // Both assets arrive, and only now.
  expect(fetched(session.requested, 'engraphis-graph.js').length).toBe(1);
  expect(fetched(session.requested, 'force-graph.min.js').length).toBe(1);
  expect(await page.evaluate(() => typeof (window.EngraphisGraph || {}).create)).toBe('function');
  // GRAPH_ENGINE is only assigned when graphRenderEngine() actually took the render.  It is a
  // top-level `let`, so it lives in the global lexical scope rather than on `window`.
  expect(await page.evaluate(() => Boolean(GRAPH_ENGINE))).toBe(true);
  // The unlinked entity must not be on the canvas: the default scope hides degree-zero nodes,
  // so this is what separates "rendered the filtered view" from "rendered the raw response".
  const painted = await page.evaluate(() => window.__fg.graphData().nodes.map(n => n.id).sort());
  expect(painted).toEqual(['ada', 'babbage', 'engine', 'fts', 'notes', 'sqlite', 'store']);

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

test('the opt-in engine adds no CSP violation the classic renderer does not already cause', async ({ page }) => {
  /* Opening the graph is *not* CSP-clean and this PR does not make it so: force-graph injects
     a handful of `<style>` elements when it attaches, which `style-src 'self'` blocks.  That
     is a vendor behaviour on the classic path too, which is the entire reason both scripts are
     lazy — the cost is confined to the one view that asked for a graph instead of being paid
     on every page load.  What must stay true is that the new renderer adds nothing on top:
     it owns only canvas paint, and any inline style of its own would show up here.
     `style-src-attr 'none'` in particular admits no escape hatch at all. */
  const session = await openDashboard(page, { query: '?graph-engine=next' });
  await openGraphView(page);
  await page.waitForTimeout(2_000);
  const underNext = await session.violations();

  await page.goto('/');
  await page.waitForFunction(() => typeof window.selectView === 'function');
  await openGraphView(page);
  await page.waitForTimeout(2_000);
  // The recorder is re-installed by addInitScript on the new document, so this is classic only.
  const underClassic = await session.violations();

  const shape = list => list.map(v => v.directive).sort();
  expect(shape(underNext)).toEqual(shape(underClassic));
  // Every one of them is an injected stylesheet, not an inline style attribute: nothing in
  // either renderer is reaching for `element.style`, which is what the drift gate enforces.
  expect(underNext.every(v => v.directive === 'style-src-elem')).toBe(true);
  expect(session.pageErrors).toEqual([]);
});

test('the graph view without the flag stays on the classic renderer', async ({ page }) => {
  const session = await openDashboard(page);
  await openGraphView(page);

  expect(fetched(session.requested, 'force-graph.min.js').length).toBe(1);
  expect(fetched(session.requested, 'engraphis-graph.js')).toEqual([]);
  expect(await page.evaluate(() => typeof window.EngraphisGraph)).toBe('undefined');
  expect(session.pageErrors).toEqual([]);
});
