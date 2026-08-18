const { test, expect } = require('@playwright/test');

test('All-node controls filter, collapse, reflow, freeze, and expose directional flow', async ({ page }) => {
  await page.goto('/');
  await page.addScriptTag({ url: '/v2-assets/engraphis-graph-all.js?v=20260818-all-nodes-lod-5' });
  const result = await page.evaluate(async () => {
    const host = document.createElement('div');
    host.style.cssText = 'position:fixed;inset:20px;width:900px;height:600px';
    document.body.append(host);
    const nodes = [
      { id: 'a', community_id: 'one' }, { id: 'b', community_id: 'one' },
      { id: 'c', community_id: 'one' }, { id: 'd', community_id: 'two' },
      { id: 'e', community_id: 'two' }, { id: 'lonely' },
    ];
    const links = [
      { source: 'a', target: 'b', weight: 3 },
      { source: 'b', target: 'c', weight: 2 },
      { source: 'd', target: 'e', weight: 1 },
    ];
    const engine = window.EngraphisAllGraph.create(host, { reducedMotion: () => true });
    const waitFor = async predicate => {
      const deadline = Date.now() + 6000;
      while (!predicate() && Date.now() < deadline) await new Promise(resolve => setTimeout(resolve, 25));
      if (!predicate()) throw new Error(`All-node state did not settle: ${JSON.stringify(engine.state())}`);
    };
    engine.setPreset('radial');
    engine.setColorBy('type');
    engine.setSettings({ flow: true, flowSpeed: 73, repel: 82, link: 34, gravity: 27 });
    engine.setScope({ minDegree: 2, showUnlinked: false, depth: 1 });
    engine.setCollapse(false);
    engine.setData({ nodes, links });
    await waitFor(() => engine.state().nodeCount === 6 && engine.state().visibleNodeCount === 1);
    const filtered = engine.state();
    engine.setScope({ minDegree: 0, showUnlinked: true, depth: 2 });
    await waitFor(() => engine.state().visibleNodeCount === 6);
    const before = engine.getPhysicsSnapshot().nodes.map(node => [node.id, node.x, node.y]);
    engine.reheat();
    await waitFor(() => engine.getPhysicsSnapshot().nodes.some((node, index) =>
      node.x !== before[index][1] || node.y !== before[index][2]));
    const canvas = host.querySelector('.engraphis-all-canvas');
    engine.setCollapse('auto');
    for (let index = 0; index < 12; index += 1) {
      canvas.dispatchEvent(new WheelEvent('wheel', {
        deltaY: 450, clientX: 450, clientY: 300, bubbles: true, cancelable: true,
      }));
    }
    await waitFor(() => engine.state().collapsed === true);
    const collapsed = engine.state();
    engine.setCollapse(false);
    await waitFor(() => engine.state().collapsed === false);
    engine.freeze(true);
    const frozenBefore = engine.getPhysicsSnapshot().nodes.map(node => [node.x, node.y]);
    engine.reheat();
    await new Promise(resolve => setTimeout(resolve, 80));
    const frozenAfter = engine.getPhysicsSnapshot().nodes.map(node => [node.x, node.y]);
    const final = engine.state();
    engine.destroy(); host.remove();
    return { filtered, collapsed, final, frozenBefore, frozenAfter };
  });
  expect(result.filtered.visibleNodeCount).toBe(1);
  expect(result.filtered.relationFlow).toBe(true);
  expect(result.filtered.flowSpeed).toBe(73);
  expect(result.collapsed.visibleNodeCount).toBe(3);
  expect(result.final.collapsed).toBe(false);
  expect(result.final.frozen).toBe(true);
  expect(result.frozenAfter).toEqual(result.frozenBefore);
});

/* Release-style browser fixture for the supported mid-range WebGL2 target. It is intentionally
   synthetic: the API contract tests own server capacity, while this test isolates handoff,
   worker preparation, LOD painting, and interaction without needing a 20k-row database fixture. */
test('20k-node all profile paints progressively and stays responsive after handoff', async ({ page }) => {
  await page.goto('/');
  const gpu = await page.evaluate(() => {
    const gl = document.createElement('canvas').getContext('webgl2');
    if (!gl) return { supported: false, renderer: '' };
    const debug = gl.getExtension('WEBGL_debug_renderer_info');
    return { supported: true, renderer: debug ? String(gl.getParameter(debug.UNMASKED_RENDERER_WEBGL) || '') : '' };
  });
  test.skip(!gpu.supported || /swiftshader|llvmpipe|software renderer/i.test(gpu.renderer), 'All-node performance target requires hardware-accelerated WebGL2');
  await page.addScriptTag({ url: '/v2-assets/engraphis-graph-all.js?v=20260818-all-nodes-lod-5' });
  const result = await page.evaluate(async () => {
    const host = document.createElement('div');
    host.className = 'graph-network';
    host.setAttribute('aria-label', '20k-node performance fixture');
    document.body.append(host);
    const nodes = Array.from({ length: 20000 }, (_value, index) => ({
      id: `n-${index}`, label: `Node ${index}`, community_id: `c-${index % 32}`,
    }));
    const links = Array.from({ length: 200000 }, (_value, index) => ({
      source: `n-${index % 20000}`, target: `n-${(index * 17 + 1) % 20000}`, weight: (index % 100) + 1,
    }));
    const progressive = [];
    const engine = window.EngraphisAllGraph.create(host, { onStats: stats => progressive.push({ nodes: stats.nodes, links: stats.links, pending: stats.linksPending, drawn: stats.drawnLinks }) });
    engine.setData({ nodes, links });
    /* The object-to-worker postMessage is the explicit initial handoff. Measure the
       interaction/rendering phase after that handoff, not the one-time structured clone
       of the server-shaped fixture payload. */
    const longTasks = [];
    const observer = typeof PerformanceObserver === 'function' ? new PerformanceObserver(list => {
      list.getEntries().forEach(entry => longTasks.push(entry.duration));
    }) : null;
    if (observer) { try { observer.observe({ type: 'longtask', buffered: false }); } catch (_error) {} }
    const deadline = Date.now() + 30000;
    while ((!progressive.some(item => item.pending) || !progressive.some(item => item.links === 200000)) && Date.now() < deadline) await new Promise(resolve => setTimeout(resolve, 25));
    const canvas = host.querySelector('.engraphis-all-canvas');
    for (let index = 0; index < 18; index += 1) {
      canvas.dispatchEvent(new WheelEvent('wheel', { deltaY: index % 2 ? 180 : -180, clientX: 400 + index, clientY: 240, bubbles: true, cancelable: true }));
      canvas.dispatchEvent(new PointerEvent('pointermove', { clientX: 400 + index * 2, clientY: 240 + index, bubbles: true }));
    }
    engine.reveal('n-10000');
    await new Promise(resolve => setTimeout(resolve, 700));
    if (observer) observer.disconnect();
    const settled = progressive.at(-1) || {};
    engine.destroy(); host.remove();
    return { progressive, settled, longTasks, webgl: true };
  });
  expect(result.progressive.some(item => item.pending)).toBeTruthy();
  expect(result.progressive.some(item => item.nodes === 20000 && item.links === 200000)).toBeTruthy();
  expect(result.settled.nodes).toBe(20000);
  expect(result.settled.links).toBe(200000);
  expect(result.settled.drawn).toBeLessThanOrEqual(75000);
  expect(result.longTasks.filter(duration => duration > 50)).toEqual([]);
});

test('canonical 3229-node Galaxy projection keeps the global anchor and stays drawable', async ({ page }) => {
  await page.goto('/');
  await page.addScriptTag({ url: '/v2-assets/engraphis-graph-all.js?v=20260818-all-nodes-lod-5' });
  const result = await page.evaluate(async () => {
    const host = document.createElement('div'); host.style.cssText = 'position:fixed;inset:0;width:900px;height:600px'; document.body.append(host);
    const nodes = Array.from({ length: 3229 }, (_value, index) => index === 0
      ? { id: 'black-hole', anchor_role: 'global', gravity_mass: 1000, x: 0, y: 0 }
      : { id: `n-${index}`, anchor_role: 'none', gravity_mass: index % 17 + 1, x: 1200 + index * 0.4, y: (index % 31) * 7 - 100 });
    window.__allClicked = null; window.__allHovered = null;
    const engine = window.EngraphisAllGraph.create(host, {
      reducedMotion: () => true,
      onHover: node => { window.__allHovered = node && node.id; },
      onNodeClick: node => { window.__allClicked = node && node.id; },
    });
    window.__allEngine = engine; window.__allHost = host;
    engine.setData({ nodes, links: [], meta: { canonical_positions: true } });
    const deadline = Date.now() + 10000;
    while (engine.state().nodeCount !== 3229 && Date.now() < deadline) await new Promise(resolve => setTimeout(resolve, 25));
    engine.fit();
    await new Promise(resolve => setTimeout(resolve, 80));
    const state = engine.state(), center = engine.graphToScreen(0, 0);
    const box = host.getBoundingClientRect();
    const snapshot = engine.getPhysicsSnapshot().nodes;
    const blackHole = snapshot.find(node => node.id === 'black-hole');
    const ordinary = snapshot.find(node => node.id !== 'black-hole');
    return { state, center: { x: box.left + center.x, y: box.top + center.y },
      blackHoleRadius: blackHole && blackHole.radius,
      ordinaryRadius: ordinary && ordinary.radius,
      canvases: host.querySelectorAll('canvas').length };
  });
  expect(result.state.nodeCount).toBe(3229);
  expect(result.state.canonicalPositions).toBe(true);
  expect(result.state.visibleNodeCount).toBeGreaterThanOrEqual(3077);
  expect(result.center.x).toBeGreaterThan(300);
  expect(result.center.x).toBeLessThan(600);
  expect(result.canvases).toBe(2);
  expect(result.blackHoleRadius).toBeGreaterThanOrEqual(result.ordinaryRadius * 2);
  await page.mouse.move(result.center.x, result.center.y);
  await expect.poll(() => page.evaluate(() => window.__allHovered)).toBe('black-hole');
  await page.mouse.click(result.center.x, result.center.y);
  await expect.poll(() => page.evaluate(() => window.__allClicked)).toBe('black-hole');
  await page.evaluate(() => { window.__allEngine.destroy(); window.__allHost.remove(); });
});

test('Canvas fallback keeps the complete canonical projection readable and centered', async ({ page }) => {
  await page.addInitScript(() => {
    const original = HTMLCanvasElement.prototype.getContext;
    HTMLCanvasElement.prototype.getContext = function getContext(kind, ...args) {
      if (kind === 'webgl2') return null;
      return original.call(this, kind, ...args);
    };
  });
  await page.goto('/');
  await page.addScriptTag({ url: '/v2-assets/engraphis-graph-all.js?v=20260818-all-nodes-lod-5' });
  const report = await page.evaluate(async () => {
    const host = document.createElement('div');
    host.style.cssText = 'position:fixed;inset:0;width:900px;height:600px';
    document.body.append(host);
    const nodes = Array.from({ length: 918 }, (_value, index) => index === 0
      ? { id: 'black-hole', anchor_role: 'global', gravity_mass: 1000, x: 0, y: 0 }
      : { id: `n-${index}`, gravity_mass: index % 11 + 1,
        x: Math.cos(index * 2.399963) * (80 + index * 0.28),
        y: Math.sin(index * 2.399963) * (80 + index * 0.28) });
    const engine = window.EngraphisAllGraph.create(host, { reducedMotion: () => true });
    engine.setData({ nodes, links: [], meta: { canonical_positions: true } });
    const deadline = Date.now() + 10000;
    while (engine.state().nodeCount !== nodes.length && Date.now() < deadline) {
      await new Promise(resolve => setTimeout(resolve, 25));
    }
    engine.fit(); await new Promise(resolve => setTimeout(resolve, 80));
    const state = engine.state(), center = engine.graphToScreen(0, 0);
    const exportCanvas = engine.exportImageCanvas();
    engine.destroy(); host.remove();
    return { state, center, exported: Boolean(exportCanvas && exportCanvas.width > 0) };
  });
  expect(report.state.renderer).toBe('canvas');
  expect(report.state.nodeCount).toBe(918);
  expect(report.state.visibleNodeCount).toBeGreaterThanOrEqual(872);
  expect(report.center.x).toBeGreaterThan(300);
  expect(report.center.x).toBeLessThan(600);
  expect(report.exported).toBe(true);
});
