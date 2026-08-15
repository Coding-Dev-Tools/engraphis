const { test, expect } = require('@playwright/test');

test('All-node controls filter, collapse, reflow, freeze, and expose directional flow', async ({ page }) => {
  await page.goto('/');
  await page.addScriptTag({ url: '/v2-assets/engraphis-graph-all.js?v=20260815-merge-ready-1' });
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
  await page.addScriptTag({ url: '/v2-assets/engraphis-graph-all.js?v=20260815-merge-ready-1' });
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
