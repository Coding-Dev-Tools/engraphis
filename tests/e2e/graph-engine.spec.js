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

test('Ledger bounds auto-fit zoom and keeps a dragged node under the pointer', async ({ page }) => {
  const session = await openDashboard(page);
  await page.goto('/');
  await page.locator('.nav-item[data-view="relations"]').click();
  await page.waitForFunction(() => window.__fg && window.__fg.graphData().nodes.length > 0);
  await page.waitForFunction(() => {
    const node = window.__fg.graphData().nodes[0];
    return Number.isFinite(node.x) && Number.isFinite(node.y);
  });
  // Let the initial layout and zoom-to-fit settle before modelling a user gesture.
  await page.waitForTimeout(3000);
  const drag = await page.evaluate(() => {
    const graph = window.__fg;
    const node = graph.graphData().nodes[0];
    const canvas = document.querySelector('#graph-canvas canvas');
    const box = canvas.getBoundingClientRect();
    const point = graph.graph2ScreenCoords(node.x, node.y);
    return {
      before: { x: node.x, y: node.y },
      zoom: canvas.__zoom.k,
      x: box.left + point.x,
      y: box.top + point.y,
    };
  });
  expect(drag.zoom).toBeLessThanOrEqual(4);
  expect(Number.isFinite(drag.x) && Number.isFinite(drag.y)).toBe(true);
  await page.mouse.move(drag.x, drag.y);
  await page.waitForTimeout(100);
  const restBeforeDrag = await page.evaluate(() => window.__fg.graphData().nodes.slice(1)
    .map(item => ({ id: item.id, x: item.x, y: item.y })));
  await page.mouse.down();
  await page.mouse.move(drag.x + 80, drag.y + 40, { steps: 8 });
  await page.mouse.up();
  await page.waitForTimeout(100);
  const after = await page.evaluate(() => {
    const nodes = window.__fg.graphData().nodes;
    const node = nodes[0];
    return {
      x: node.x, y: node.y, fx: node.fx, fy: node.fy,
      otherNodes: nodes.slice(1).map(item => ({ id: item.id, x: item.x, y: item.y })),
    };
  });

  expect(after.x - drag.before.x).toBeCloseTo(80 / drag.zoom, 0);
  expect(after.y - drag.before.y).toBeCloseTo(40 / drag.zoom, 0);
  const initial = new Map(restBeforeDrag.map(item => [item.id, item]));
  const maximumOtherMovement = Math.max(...after.otherNodes.map(item => {
    const before = initial.get(item.id);
    return Math.hypot(item.x - before.x, item.y - before.y);
  }));
  expect(maximumOtherMovement).toBeLessThan(0.5);
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

  await expect(page.locator('#graph-show-all')).toHaveCount(0);
  await expect(page.locator('#graph-show-iso')).toBeEnabled();
  expect(await page.evaluate(() => GRAPH_FULL)).toBe(false);
  expect(session.pageErrors).toEqual([]);
});
