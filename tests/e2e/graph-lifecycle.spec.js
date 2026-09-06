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

async function fixture(page, { deferGraph } = {}) {
  const errors = [];
  const graphRequests = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.addInitScript(() => {
    window.__lifecycleEngines = [];
    window.__lifecycleWorkers = [];
    const NativeWorker = window.Worker;
    window.Worker = class extends NativeWorker {
      constructor(...args) {
        super(...args);
        const record = { url: String(args[0]), terminated: false };
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
  });
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
