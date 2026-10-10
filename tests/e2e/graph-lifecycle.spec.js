const { test, expect } = require('@playwright/test');
const AxeBuilder = require('@axe-core/playwright').default;

const scene = {
  nodes: [
    { id: 'memory', label: 'Memory engine', repo_names: ['agent-memory'], community_id: 'memory', anchor_role: 'global', gravity_mass: 8, visual_radius: 13, x: 0, y: 0 },
    { id: 'database', label: 'Postgres', repo_names: ['agent-memory'], community_id: 'storage', gravity_mass: 2, visual_radius: 7, x: 40, y: 10 },
    { id: 'offline', label: 'Offline support', repo_names: ['agent-memory'], community_id: 'storage', gravity_mass: 1, visual_radius: 5, x: -20, y: 30 },
  ],
  edges: [{ from: 'memory', to: 'database' }, { from: 'memory', to: 'offline' }],
  communities: [{ id: 'memory', mass: 8 }, { id: 'storage', mass: 3 }],
  meta: { algorithm_version: 'galaxy-v6', layout_seed: 7 },
};

async function fixture(page, { deferGraph, holdEveryWorker = false } = {}) {
  const errors = [];
  const graphRequests = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.addInitScript(({ holdEveryWorker }) => {
    window.__lifecycleEngines = [];
    window.__lifecycleWorkers = [];
    const NativeWorker = window.Worker;
    window.Worker = class extends NativeWorker {
      constructor(...args) {
        super(...args);
        const record = { url: String(args[0]), terminated: false };
        if (holdEveryWorker && record.url.includes('engraphis-graph-every-worker.js')) {
          let listener = null, released = false;
          record.held = [];
          Object.defineProperty(this, 'onmessage', {
            get: () => listener,
            set: value => { listener = value; },
          });
          this.addEventListener('message', event => {
            if (released) { if (listener) listener.call(this, event); }
            else record.held.push(event);
          });
          record.release = () => {
            released = true;
            record.held.splice(0).forEach(event => { if (listener) listener.call(this, event); });
          };
        }
        window.__lifecycleWorkers.push(record);
        const terminate = this.terminate.bind(this);
        this.terminate = () => { record.terminated = true; return terminate(); };
      }
    };
    for (const name of ['EngraphisGraph', 'EngraphisEveryGraph']) {
      let factory;
      Object.defineProperty(window, name, {
        configurable: true,
        get: () => factory,
        set(value) {
          factory = { ...value, create(...args) {
            const api = value.create(...args);
            const record = { name, api, host: args[0], destroyed: false };
            const destroy = api.destroy.bind(api);
            api.destroy = () => { record.destroyed = true; return destroy(); };
            window.__lifecycleEngines.push(record);
            return api;
          } };
        },
      });
    }
  }, { holdEveryWorker });
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url());
    const path = url.pathname.slice(4);
    const ok = json => route.fulfill({ json });
    if (path === '/bootstrap') return ok({ workspaces: [{ name: 'graph-work', memories: 3 }], license: { plan: 'local', features: [], known_features: {} } });
    if (path === '/stats') return ok({ memories: 3, total_rows: 3, workspaces: 1, sessions: 1, by_type: { semantic: 3 } });
    if (path === '/repos') return ok({ repos: [{ id: 'repo_agent', name: 'agent-memory' }] });
    if (path === '/memories') return ok({ memories: [], count: 0, total_count: 0, next_cursor: null });
    if (path === '/review-inbox') return ok({ items: [], count: 0, has_more: false, truncated: false });
    if (path === '/graph/scene') {
      graphRequests.push(Object.fromEntries(url.searchParams));
      if (deferGraph) await deferGraph();
      return ok(scene);
    }
    if (path.startsWith('/graph/entities/') && path.endsWith('/memories')) {
      return ok({ evidence: [{ memory_id: 'mem_database', title: 'Database choice', excerpt: 'Postgres stores project facts.' }], totals: { evidence: 1 }, truncation: { evidence: false } });
    }
    return ok({});
  });
  await page.goto('/?workspace=graph-work&view=today');
  await expect(page.locator('#connection-status')).toContainText('Local engine connected');
  return { errors, graphRequests };
}

async function openGraph(page) {
  await page.locator('.nav-item[data-view="relations"]').click();
  await expect(page.locator('#graph-canvas')).toHaveAttribute('aria-busy', 'false');
  await expect(page.locator('#graph-canvas canvas').first()).toBeVisible();
}

async function frames(page, count = 5) {
  await page.evaluate(count => new Promise(resolve => {
    function tick() { if (--count <= 0) resolve(); else requestAnimationFrame(tick); }
    requestAnimationFrame(tick);
  }), count);
}

test('leaving Explore pauses the actual primary renderer and returning retains its instance', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' });
  const session = await fixture(page);
  expect(await page.evaluate(() => window.__lifecycleEngines.length)).toBe(0);
  expect(session.graphRequests).toHaveLength(0);
  await openGraph(page);
  await page.waitForFunction(() => window.__lifecycleEngines[0].api.physicsDiagnostics().steps >= 5);
  expect(await page.evaluate(() => window.__lifecycleEngines[0].api.physicsDiagnostics().reducedMotion)).toBe(true);
  await page.evaluate(() => window.__lifecycleEngines[0].api.focus('database'));
  const before = await page.evaluate(() => {
    const api = window.__lifecycleEngines[0].api;
    return { camera: [api.graphToScreen(0, 0), api.graphToScreen(10, 10)], highlight: api.state().highlight };
  });
  await page.locator('.nav-item[data-view="library"]').click();
  expect(await page.evaluate(() => window.__lifecycleEngines[0].api.getPhysicsSnapshot().paused)).toBe(true);
  const steps = await page.evaluate(() => window.__lifecycleEngines[0].api.physicsDiagnostics().steps);
  await frames(page);
  expect(await page.evaluate(() => window.__lifecycleEngines[0].api.physicsDiagnostics().steps)).toBe(steps);
  await openGraph(page);
  expect(await page.evaluate(() => window.__lifecycleEngines.length)).toBe(1);
  expect(await page.evaluate(() => window.__lifecycleEngines[0].api.getPhysicsSnapshot().paused)).toBe(false);
  const after = await page.evaluate(() => {
    const api = window.__lifecycleEngines[0].api;
    return { camera: [api.graphToScreen(0, 0), api.graphToScreen(10, 10)], highlight: api.state().highlight };
  });
  expect(after).toEqual(before);
  expect(session.graphRequests).toHaveLength(1);
  expect(session.errors).toEqual([]);
});

test('Every node preserves keyboard pan and zoom across hiding and releases its worker on replacement', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' });
  const session = await fixture(page);
  await openGraph(page);
  await page.locator('#graph-advanced > summary').click();
  await page.locator('[data-graph-preset-choice="every"]').click();
  await expect(page.locator('#graph-canvas')).toHaveAttribute('aria-busy', 'false');
  await page.waitForFunction(() => window.__lifecycleEngines.length === 2
    && window.__lifecycleEngines[1].api.state().mode === 'all');
  expect(await page.evaluate(() => window.__lifecycleEngines[0].destroyed)).toBe(true);
  const start = await page.evaluate(() => {
    const api = window.__lifecycleEngines[1].api;
    return [api.graphToScreen(0, 0), api.graphToScreen(10, 10)];
  });
  await page.locator('#graph-canvas').focus();
  await page.keyboard.press('+');
  await page.keyboard.press('ArrowRight');
  const before = await page.evaluate(() => {
    const api = window.__lifecycleEngines[1].api;
    return [api.graphToScreen(0, 0), api.graphToScreen(10, 10)];
  });
  expect(before[1].x - before[0].x).toBeGreaterThan(start[1].x - start[0].x);
  expect(before[0].x).not.toBe(start[0].x);
  await page.locator('.nav-item[data-view="manage"]').click();
  expect(await page.evaluate(() => window.__lifecycleEngines[1].api.state().paused)).toBe(true);
  await frames(page);
  await openGraph(page);
  expect(await page.evaluate(() => window.__lifecycleEngines.length)).toBe(2);
  expect(await page.evaluate(() => window.__lifecycleEngines[1].api.state().paused)).toBe(false);
  const after = await page.evaluate(() => {
    const api = window.__lifecycleEngines[1].api;
    return [api.graphToScreen(0, 0), api.graphToScreen(10, 10)];
  });
  expect(after).toEqual(before);
  expect(await page.evaluate(() => window.__lifecycleWorkers.filter(item => !item.terminated).length)).toBe(1);
  await page.locator('#graph-retry').click();
  await expect(page.locator('#graph-canvas')).toHaveAttribute('aria-busy', 'false');
  expect(await page.evaluate(() => window.__lifecycleEngines[1].destroyed)).toBe(true);
  expect(await page.evaluate(() => window.__lifecycleWorkers[0].terminated)).toBe(true);
  expect(await page.evaluate(() => window.__lifecycleWorkers.filter(item => !item.terminated).length)).toBe(1);
  // A detached host retaining its keyboard handler would still prevent this event's default.
  expect(await page.evaluate(() => window.__lifecycleEngines[1].host.dispatchEvent(
    new KeyboardEvent('keydown', { key: 'ArrowRight', cancelable: true }),
  ))).toBe(true);
  await page.locator('#graph-canvas').focus();
  await page.keyboard.press('f');
  expect(session.errors).toEqual([]);
});

for (const gesture of ['keyboard', 'wheel', 'drag', 'small-drag', 'automatic']) {
  test(`Every node handles deferred worker fits after ${gesture} navigation and hiding`, async ({ page }) => {
    const session = await fixture(page, { holdEveryWorker: true });
    await openGraph(page);
    await page.locator('#graph-advanced > summary').click();
    await page.locator('[data-graph-preset-choice="every"]').click();
    await expect(page.locator('#graph-canvas')).toHaveAttribute('aria-busy', 'false');
    await page.waitForFunction(() => window.__lifecycleEngines.length === 2
      && window.__lifecycleWorkers.some(record => record.held
        && record.held.some(event => event.data.type === 'layout' && event.data.fit === true)));
    const camera = () => page.evaluate(() => {
      const api = window.__lifecycleEngines[1].api;
      return [api.graphToScreen(0, 0), api.graphToScreen(10, 10)];
    });
    const start = await camera();
    const box = await page.locator('.engraphis-all-canvas').boundingBox();
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    if (gesture === 'keyboard') {
      await page.locator('#graph-canvas').focus();
      await page.keyboard.press('+');
      await page.keyboard.press('ArrowRight');
    } else if (gesture === 'wheel') {
      await page.mouse.wheel(0, -200);
    } else if (gesture === 'drag' || gesture === 'small-drag') {
      await page.mouse.down();
      await page.mouse.move(box.x + box.width / 2 + (gesture === 'small-drag' ? 1 : 70),
        box.y + box.height / 2, { steps: 3 });
      await page.mouse.up();
    }
    const before = await camera();
    if (gesture !== 'automatic') expect(before).not.toEqual(start);
    await page.locator('.nav-item[data-view="manage"]').click();
    await frames(page);
    expect(await camera()).toEqual(before);
    await page.evaluate(() => window.__lifecycleWorkers.forEach(record => { if (record.release) record.release(); }));
    expect(await camera()).toEqual(before);
    await openGraph(page);
    await frames(page);
    const after = await camera();
    if (gesture === 'automatic') {
      expect(after).not.toEqual(before);
      expect(after[1].x - after[0].x).toBeGreaterThan(0.05);
    }
    else expect(after).toEqual(before);
    await page.locator('#graph-canvas').focus();
    await page.keyboard.press('f');
    const fitted = await camera();
    expect(fitted[1].x - fitted[0].x).toBeGreaterThan(0.05);
    expect(session.errors).toEqual([]);
  });
}

test('Explore keeps essential controls first and reveals advanced controls with the keyboard', async ({ page }) => {
  await page.setViewportSize({ width: 640, height: 900 });
  const session = await fixture(page);
  await openGraph(page);
  await expect(page.locator('#graph-search')).toBeVisible();
  await expect(page.locator('#graph-repo-filter')).toBeVisible();
  await expect(page.locator('#graph-fit')).toBeVisible();
  await expect(page.locator('[data-graph-preset-choice="galaxy"]')).not.toBeVisible();
  await page.locator('#graph-search').fill('Postgres');
  await page.locator('#graph-search-results button').first().focus();
  await page.keyboard.press('Enter');
  await expect(page.locator('#graph-connections-dialog')).toBeVisible();
  await page.keyboard.press('Escape');
  await page.locator('#graph-advanced > summary').focus();
  await page.keyboard.press('Enter');
  await expect(page.locator('[data-graph-preset-choice="galaxy"]')).toBeVisible();
  await page.locator('#graph-advanced > summary').focus();
  await page.keyboard.press('Space');
  await expect(page.locator('[data-graph-preset-choice="galaxy"]')).not.toBeVisible();
  const audit = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21aa']).analyze();
  expect(audit.violations).toEqual([]);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  expect(session.errors).toEqual([]);
});

test('lifecycle observes document visibility for pending renderers and removes its listener on disposal', async ({ page }) => {
  await fixture(page);
  const result = await page.evaluate(() => {
    let visible = true;
    let tabHidden = false;
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => tabHidden });
    const calls = [];
    const statuses = [];
    const engine = name => ({
      pause: () => calls.push(name + ':pause'), resume: () => calls.push(name + ':resume'),
      destroy: () => calls.push(name + ':destroy'),
    });
    const old = engine('old');
    const pending = engine('pending');
    const lifecycle = window.EngraphisGraphLifecycle.create({
      isVisible: () => visible, onStatus: status => statuses.push(status),
    });
    lifecycle.replace(old);
    lifecycle.track(pending);
    tabHidden = true;
    document.dispatchEvent(new Event('visibilitychange'));
    const hiddenCalls = calls.slice();
    lifecycle.replace(pending);
    tabHidden = false;
    document.dispatchEvent(new Event('visibilitychange'));
    const resumed = statuses.at(-1);
    visible = false;
    lifecycle.sync();
    lifecycle.destroy();
    lifecycle.destroy();
    const disposedCalls = calls.slice();
    document.dispatchEvent(new Event('visibilitychange'));
    delete document.hidden;
    return { hiddenCalls, resumed, disposedCalls, finalCalls: calls };
  });
  expect(result.hiddenCalls).toEqual(['old:resume', 'pending:resume', 'old:pause', 'pending:pause']);
  expect(result.resumed).toEqual({ visible: true, capability: 'drawing' });
  expect(result.finalCalls).toEqual(result.disposedCalls);
  expect(result.finalCalls.filter(call => call === 'old:destroy')).toHaveLength(1);
  expect(result.finalCalls.filter(call => call === 'pending:destroy')).toHaveLength(1);
});

test('compatibility fallback preserves freeze preferences without claiming a drawing pause', async ({ page }) => {
  await fixture(page);
  const result = await page.evaluate(() => {
    const cases = [];
    for (const initial of [true, false]) {
      let visible = true;
      let frozen = initial;
      const statuses = [];
      const engine = { state: () => ({ settings: { frozen } }), freeze: value => { frozen = value; }, destroy() {} };
      const lifecycle = window.EngraphisGraphLifecycle.create({
        isVisible: () => visible, onStatus: status => statuses.push(status),
      });
      lifecycle.replace(engine);
      visible = false;
      lifecycle.sync();
      const hidden = frozen;
      visible = true;
      lifecycle.sync();
      cases.push({ initial, hidden, restored: frozen, capability: statuses.at(-1).capability });
      lifecycle.destroy();
    }
    let status;
    const lifecycle = window.EngraphisGraphLifecycle.create({ isVisible: () => false, onStatus: value => { status = value; } });
    lifecycle.replace({ destroy() {} });
    const unsupported = status;
    lifecycle.destroy();
    return { cases, unsupported };
  });
  expect(result.cases).toEqual([
    { initial: true, hidden: true, restored: true, capability: 'physics' },
    { initial: false, hidden: true, restored: false, capability: 'physics' },
  ]);
  expect(result.unsupported).toEqual({ visible: false, capability: 'unsupported' });
});

test('a graph requested before leaving Explore commits paused until the view returns', async ({ page }) => {
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  const session = await fixture(page, { deferGraph: () => gate });
  await page.locator('.nav-item[data-view="relations"]').click();
  await expect.poll(() => session.graphRequests.length).toBe(1);
  await page.locator('.nav-item[data-view="library"]').click();
  release();
  await expect(page.locator('#graph-canvas')).toHaveAttribute('aria-busy', 'false');
  expect(await page.evaluate(() => window.__lifecycleEngines[0].api.getPhysicsSnapshot().paused)).toBe(true);
  await openGraph(page);
  expect(await page.evaluate(() => window.__lifecycleEngines[0].api.getPhysicsSnapshot().paused)).toBe(false);
  expect(session.graphRequests).toHaveLength(1);
  expect(session.errors).toEqual([]);
});

for (const pauseTiming of ['before-loss', 'during-loss', 'active']) {
  test(`Every node restores WebGL with caller lifecycle intent: ${pauseTiming}`, async ({ page }) => {
    const session = await fixture(page);
    await openGraph(page);
    await page.locator('#graph-advanced > summary').click();
    await page.locator('[data-graph-preset-choice="every"]').click();
    await expect(page.locator('#graph-canvas')).toHaveAttribute('aria-busy', 'false');
    await page.waitForFunction(() => window.__lifecycleEngines.at(-1).name === 'EngraphisEveryGraph'
      && window.__lifecycleEngines.at(-1).api.state().nodeCount === 3);
    if (pauseTiming === 'before-loss') {
      await page.locator('.nav-item[data-view="library"]').click();
    }
    await page.evaluate(async () => {
      const record = window.__lifecycleEngines.at(-1);
      const canvas = record.host.querySelector('.engraphis-all-canvas');
      const gl = canvas.getContext('webgl2');
      const extension = gl.getExtension('WEBGL_lose_context');
      if (!extension) throw new Error('The Chromium fixture requires WEBGL_lose_context');
      record.contextCanvas = canvas;
      record.contextExtension = extension;
      record.drawCalls = 0;
      const drawArrays = gl.drawArrays.bind(gl);
      gl.drawArrays = (...args) => { record.drawCalls += 1; return drawArrays(...args); };
      const lost = new Promise(resolve => canvas.addEventListener('webglcontextlost', resolve, { once: true }));
      extension.loseContext();
      await lost;
    });
    if (pauseTiming === 'during-loss') {
      await page.locator('.nav-item[data-view="library"]').click();
    }
    expect(await page.evaluate(() => window.__lifecycleEngines.at(-1).api.exportImageCanvas())).toBeNull();
    await page.evaluate(async () => {
      const record = window.__lifecycleEngines.at(-1);
      const restored = new Promise(resolve => record.contextCanvas.addEventListener('webglcontextrestored', resolve, { once: true }));
      record.contextExtension.restoreContext();
      await restored;
    });
    const paused = pauseTiming !== 'active';
    expect(await page.evaluate(() => window.__lifecycleEngines.at(-1).api.state().paused)).toBe(paused);
    if (paused) {
      const before = await page.evaluate(() => window.__lifecycleEngines.at(-1).drawCalls);
      await frames(page);
      expect(await page.evaluate(() => window.__lifecycleEngines.at(-1).drawCalls)).toBe(before);
      await openGraph(page);
      expect(await page.evaluate(() => window.__lifecycleEngines.at(-1).api.state().paused)).toBe(false);
    }
    await expect.poll(() => page.evaluate(() => window.__lifecycleEngines.at(-1).drawCalls)).toBeGreaterThan(0);
    expect(await page.evaluate(() => window.__lifecycleEngines.at(-1).api.state().nodeCount)).toBe(3);
    expect(session.errors).toEqual([]);
  });
}

for (const deviceScaleFactor of [1, 1.25, 2]) {
  test.describe(`Classic PNG at DPR ${deviceScaleFactor}`, () => {
    test.use({ deviceScaleFactor });

    for (const reducedMotion of deviceScaleFactor === 1.25 ? [] : ['no-preference', 'reduce']) {
      test(`Paper export preserves screen blending and ${reducedMotion} opacity`, async ({ page }) => {
        await page.emulateMedia({ reducedMotion });
        const session = await fixture(page);
        await openGraph(page);
        await page.locator('#sidebar-theme-select').selectOption('paper');
        await expect(page.locator('body')).toHaveAttribute('data-theme', 'paper');
        await page.locator('#graph-advanced > summary').click();
        await page.locator('[data-graph-style-choice="classic"]').click();
        const image = await page.evaluate(() => {
          const { api, host } = window.__lifecycleEngines.at(-1);
          api.pause();
          const graph = host.querySelector('.force-graph-container canvas');
          const overlay = host.querySelector('.graph-spacetime-overlay');
          // Known pixels isolate export composition from changing physics and label positions.
          for (const canvas of [graph, overlay]) {
            const ctx = canvas.getContext('2d');
            ctx.setTransform(1, 0, 0, 1, 0, 0);
            ctx.globalAlpha = 1;
            ctx.globalCompositeOperation = 'source-over';
            ctx.clearRect(0, 0, canvas.width, canvas.height);
          }
          const foreground = graph.getContext('2d');
          foreground.fillStyle = '#ff0000';
          foreground.fillRect(10, 10, 10, 10);
          const background = overlay.getContext('2d');
          background.fillStyle = '#0000ff';
          background.fillRect(10 * overlay.width / graph.width, 10 * overlay.height / graph.height,
            30 * overlay.width / graph.width, 30 * overlay.height / graph.height);
          const output = api.exportImageCanvas();
          const ctx = output.getContext('2d');
          const pixel = (x, y) => Array.from(ctx.getImageData(x, y, 1, 1).data);
          return {
            paneBackground: getComputedStyle(host).backgroundColor,
            overlayBlend: getComputedStyle(overlay).mixBlendMode,
            overlayOpacity: getComputedStyle(overlay).opacity,
            background: pixel(0, 0), foreground: pixel(15, 15), overlay: pixel(30, 30),
            size: [output.width, output.height], graphSize: [graph.width, graph.height],
          };
        });
        expect(image.paneBackground).toBe('rgb(240, 238, 232)');
        expect(image.background).toEqual([240, 238, 232, 255]);
        expect(image.foreground).toEqual([255, 0, 0, 255]);
        expect(image.overlayBlend).toBe('screen');
        expect(image.overlayOpacity).toBe(reducedMotion === 'reduce' ? '0.42' : '1');
        expect(image.overlay).toEqual([240, 238, reducedMotion === 'reduce' ? 242 : 255, 255]);
        expect(image.size).toEqual(image.graphSize);
        expect(session.errors).toEqual([]);
      });
    }

    for (const style of deviceScaleFactor === 1.25 ? ['cyber'] : ['galaxy', 'solar', 'cyber']) {
      test(`${style} export matches browser CSS gradient and grid pixels`, async ({ page }) => {
        const session = await fixture(page);
        await openGraph(page);
        await page.locator('#graph-advanced > summary').click();
        await page.locator(`[data-graph-style-choice="${style}"]`).click();
        await expect(page.locator('#graph-canvas')).toHaveAttribute('data-graph-style', style);
        await page.evaluate(() => window.__lifecycleEngines.at(-1).api.pause());
        await frames(page);
        const exported = await page.evaluate(() => {
          const { api, host } = window.__lifecycleEngines.at(-1);
          for (const canvas of host.querySelectorAll('canvas')) {
            const ctx = canvas.getContext('2d');
            if (!ctx) continue;
            ctx.setTransform(1, 0, 0, 1, 0, 0);
            ctx.clearRect(0, 0, canvas.width, canvas.height);
          }
          const output = api.exportImageCanvas();
          const ctx = output.getContext('2d');
          const width = host.clientWidth, height = host.clientHeight;
          const points = [[width * .24, height * .22], [width * .82, height * .78],
            [width * .62, height * .42], [width * .5, height * .5],
            [60.25, 181.25], [60.75, 181.25], [61.25, 181.25],
            [61.25, 180.25], [61.25, 180.75],
            [90.25, 181.25], [90.75, 181.25], [91.25, 181.25]];
          return {
            size: [output.width, output.height], cssSize: [width, height], points,
            background: getComputedStyle(host).backgroundColor.match(/\d+/g).map(Number),
            samples: points.map(([x, y]) => Array.from(ctx.getImageData(
              Math.floor(x * devicePixelRatio), Math.floor(y * devicePixelRatio), 1, 1,
            ).data)),
          };
        });
        // The browser's own CSS compositor is the oracle, not a duplicate gradient formula.
        const screenshot = await page.locator('#graph-canvas').screenshot({ animations: 'disabled' });
        const displayed = await page.evaluate(async ({ png, points }) => {
          const bytes = Uint8Array.from(atob(png), character => character.charCodeAt(0));
          const bitmap = await createImageBitmap(new Blob([bytes], { type: 'image/png' }));
          const canvas = document.createElement('canvas');
          canvas.width = bitmap.width; canvas.height = bitmap.height;
          const ctx = canvas.getContext('2d');
          ctx.drawImage(bitmap, 0, 0);
          const samples = points.map(([x, y]) => Array.from(ctx.getImageData(
            Math.floor(x * devicePixelRatio), Math.floor(y * devicePixelRatio), 1, 1,
          ).data));
          bitmap.close();
          return samples;
        }, { png: screenshot.toString('base64'), points: exported.points });
        expect(exported.size).toEqual(exported.cssSize.map(size => Math.floor(size * deviceScaleFactor)));
        for (let sample = 0; sample < displayed.length; sample++) {
          for (let channel = 0; channel < 4; channel++) {
            expect(Math.abs(exported.samples[sample][channel] - displayed[sample][channel]),
              `${style} sample ${sample}: exported ${exported.samples[sample]}, displayed ${displayed[sample]}`)
              .toBeLessThanOrEqual(3);
          }
        }
        expect(exported.samples.some(pixel => pixel.slice(0, 3).some(
          (value, channel) => Math.abs(value - exported.background[channel]) > 3,
        ))).toBe(true);
        if (style === 'cyber') {
          // Both halves of each 1 CSS px line survive at DPR 2, with a clear gap after it.
          // Compare with adjacent gaps: the underlying radial gradient varies across tiles.
          for (const line of [4, 5, 7, 8]) {
            expect(exported.samples[line][1]).toBeGreaterThan(exported.samples[6][1] + 5);
          }
          // At fractional DPR this stripe straddles two physical pixels. Average
          // their coverage for contrast; both still have individual CSS pixel checks.
          const repeatedLineGreen = [exported.samples[9][1], exported.samples[10][1]];
          const contrastSamples = Number.isInteger(deviceScaleFactor) ? repeatedLineGreen
            : [(repeatedLineGreen[0] + repeatedLineGreen[1]) / 2];
          for (const green of contrastSamples) {
            expect(green).toBeGreaterThan(exported.samples[11][1] + 5);
          }
        }
        expect(session.errors).toEqual([]);
      });
    }
  });
}
