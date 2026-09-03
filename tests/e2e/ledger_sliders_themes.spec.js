const { test, expect } = require('@playwright/test');

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
      galactic_target_radius: 72.25786, galactic_radius_scale: 0.4,
      galactic_initial_compactness: 0.8, galactic_phase: 0, x: 83.2, y: 14.4 },
    { id: 'borealis-star', label: 'Borealis star', gravity_mass: 9, visual_radius: 8,
      community_id: 'borealis', anchor_role: 'community', system_anchor_id: 'borealis-star',
      orbit_tier: 0, galactic_radius: 116.95649, galactic_target_radius: 116.95649,
      galactic_radius_scale: 0.4, galactic_initial_compactness: 0.8, galactic_phase: 1.71, x: -16, y: 113.6 },
    { id: 'borealis-planet', label: 'Borealis planet', gravity_mass: 2, visual_radius: 8,
      community_id: 'borealis', anchor_role: 'none', system_anchor_id: 'borealis-star',
      orbit_tier: 1, orbit_radius: 20.8, galactic_radius: 116.95649,
      galactic_target_radius: 116.95649, galactic_radius_scale: 0.4,
      galactic_initial_compactness: 0.8, galactic_phase: 1.71, x: -34.4, y: 123.2 },
    { id: 'cygnus-star', label: 'Cygnus star', gravity_mass: 7, visual_radius: 8,
      community_id: 'cygnus', anchor_role: 'community', system_anchor_id: 'cygnus-star',
      orbit_tier: 0, galactic_radius: 166.912702, galactic_target_radius: 166.912702,
      galactic_radius_scale: 0.4, galactic_initial_compactness: 0.8, galactic_phase: -2.84, x: -158.4, y: -49.6 },
    { id: 'cygnus-planet', label: 'Cygnus planet', gravity_mass: 1, visual_radius: 8,
      community_id: 'cygnus', anchor_role: 'none', system_anchor_id: 'cygnus-star',
      orbit_tier: 1, orbit_radius: 23.2, galactic_radius: 166.912702,
      galactic_target_radius: 166.912702, galactic_radius_scale: 0.4,
      galactic_initial_compactness: 0.8, galactic_phase: -2.84, x: -172, y: -30.4 },
  ],
  edges: [
    { id: 'core-orbit', source: 'black-hole', target: 'core-star', relation: 'orbits', rest_length: 48, spring_strength: 0.08 },
    { id: 'aurora-orbit', source: 'aurora-star', target: 'aurora-planet', relation: 'orbits', rest_length: 19.2, spring_strength: 0.08 },
    { id: 'borealis-orbit', source: 'borealis-star', target: 'borealis-planet', relation: 'orbits', rest_length: 20.8, spring_strength: 0.08 },
    { id: 'cygnus-orbit', source: 'cygnus-star', target: 'cygnus-planet', relation: 'orbits', rest_length: 23.2, spring_strength: 0.08 },
  ],
  communities: [
    { id: 'core', mass: 70, member_count: 2, anchor_id: 'black-hole', galactic_radius: 0, galactic_target_radius: 0 },
    { id: 'aurora', mass: 14, member_count: 2, anchor_id: 'aurora-star', galactic_radius: 72.25786, galactic_target_radius: 72.25786 },
    { id: 'borealis', mass: 11, member_count: 2, anchor_id: 'borealis-star', galactic_radius: 116.95649, galactic_target_radius: 116.95649 },
    { id: 'cygnus', mass: 8, member_count: 2, anchor_id: 'cygnus-star', galactic_radius: 166.912702, galactic_target_radius: 166.912702 },
  ],
  community_bridges: [],
  meta: { algorithm_version: 'galaxy-v6', layout_seed: 91, total_nodes: 8, truncated: false },
};

async function openDashboard(page, { graphScene = blackHoleGalaxyScene } = {}) {
  const consoleErrors = [];
  const pageErrors = [];

  await page.addInitScript(() => {
    window.__cspViolations = [];
    document.addEventListener('securitypolicyviolation', event => {
      window.__cspViolations.push({
        directive: event.effectiveDirective || event.violatedDirective,
        blocked: String(event.blockedURI || ''),
      });
    });

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

  page.on('console', message => {
    if (message.type() === 'error') {
      const text = message.text();
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
        workspaces: [{ name: 'default', memories: 8 }],
        embedder: { semantic: true },
      });
    }
    if (path === '/graph/scene') return json(graphScene);
    if (path === '/health') return json({ status: 'ok' });
    if (path === '/stats') return json({ memories: 8, total_rows: 8, workspaces: 1, sessions: 1, by_type: {} });
    if (path === '/workspaces') return json({ workspaces: [{ name: 'default', memories: 8 }] });
    if (path === '/memories') return json({ memories: [{ id: 'mem_1', title: 'Test Memory', content: 'Test content', memory_type: 'semantic', importance: 0.5 }] });
    return json({});
  });

  return { consoleErrors, pageErrors };
}

test.describe('Ledger Dashboard Sliders, Gravity Physics, Themes, and Options', () => {
  test('all 17 sliders, gravity physics, 4 themes, buttons and switches operate cleanly without CSP or console errors', async ({ page }) => {
    test.setTimeout(60_000);
    const session = await openDashboard(page);

    await page.goto('/');

    // 1. Navigate to Graph & Relationships view
    await page.locator('.nav-item[data-view="relations"]').click();
    await expect(page.locator('#graph-canvas canvas').first()).toBeAttached({ timeout: 20_000 });

    // Wait for canvas engine to attach
    await page.waitForFunction(() => (
      window.__engraphisGraph
      && window.__engraphisGraph.state()
      && typeof window.__engraphisGraph.physicsDiagnostics === 'function'
      && window.__engraphisGraph.physicsDiagnostics().active
      && window.__engraphisGraph.physicsDiagnostics().steps >= 5
    ), null, { timeout: 25_000 });

    // 2. Ensure Tuning details are open
    await page.evaluate(() => {
      document.querySelectorAll('details.graph-drawer-section').forEach(d => { d.open = true; });
    });
    await expect(page.locator('#graph-repel')).toBeVisible();

    // 3. Test Themes: Slate, Midnight, Paper, Matrix
    const sidebarTheme = page.locator('#sidebar-theme-select');
    const themes = ['slate', 'midnight', 'paper', 'matrix'];
    for (const theme of themes) {
      await sidebarTheme.selectOption(theme);
      await expect(page.locator('body')).toHaveAttribute('data-theme', theme);

      // Verify CSS Custom properties
      const styles = await page.evaluate(() => {
        const cs = getComputedStyle(document.body);
        return {
          bg: cs.getPropertyValue('--c-bg').trim(),
          acc: cs.getPropertyValue('--c-acc').trim(),
          fg: cs.getPropertyValue('--c-fg').trim(),
        };
      });
      expect(styles.bg).not.toBe('');
      expect(styles.acc).not.toBe('');
      expect(styles.fg).not.toBe('');

      // Verify canvas themeColors sync
      const engineThemeColors = await page.evaluate(() => window.__engraphisGraph.state().themeColors);
      expect(engineThemeColors).not.toBeNull();
      expect(engineThemeColors.canvas).not.toBe('');
      expect(engineThemeColors.accent).not.toBe('');
    }

    // Return to default slate theme
    await sidebarTheme.selectOption('slate');

    // 4. Test All Graph Sliders & Outputs
    const slidersToTest = [
      { id: 'graph-flow-speed', value: '75', outputId: 'graph-flow-speed-output', expectedText: '75' },
      { id: 'graph-repel', value: '250', outputId: 'graph-repel-output', expectedText: '250' },
      { id: 'graph-link', value: '30', outputId: 'graph-link-output', expectedText: '30' },
      { id: 'graph-gravity', value: '180', outputId: 'graph-gravity-output', expectedText: '180' },
      { id: 'graph-node-size', value: '6', outputId: 'graph-node-size-output', expectedText: '6' },
      { id: 'graph-text-size', value: '16', outputId: 'graph-text-size-output', expectedText: '16' },
      { id: 'graph-line-width', value: '1.25', outputId: 'graph-line-width-output', expectedText: '1.25' },
      { id: 'graph-label-density', value: '50', outputId: 'graph-label-density-output', expectedText: '50' },
      { id: 'graph-tune-min-degree', value: '2', outputId: 'graph-tune-min-degree-output', expectedText: '2' },
      { id: 'graph-depth', value: '3', outputId: 'graph-depth-output', expectedText: '3' },
      { id: 'graph-gravitational-constant', value: '150', outputId: 'graph-gravitational-constant-output', expectedText: '150' },
      { id: 'graph-black-hole-mass', value: '240', outputId: 'graph-black-hole-mass-output', expectedText: '240' },
      { id: 'graph-local-gravitational-constant', value: '120', outputId: 'graph-local-gravitational-constant-output', expectedText: '120' },
      { id: 'graph-space-damping', value: '2.5', outputId: 'graph-space-damping-output', expectedText: '2.5' },
      { id: 'graph-spring-stiffness', value: '64', outputId: 'graph-spring-stiffness-output', expectedText: '64' },
    ];

    for (const s of slidersToTest) {
      await page.evaluate(({ id, value }) => {
        const el = document.getElementById(id);
        el.value = value;
        el.dispatchEvent(new Event('input', { bubbles: true }));
      }, s);
      await expect(page.locator(`#${s.outputId}`)).toHaveText(s.expectedText);
    }

    // Min degree bidirectional sync between analyse tab and tuning drawer
    const analyseTab = page.locator('#graph-analyse-tab');
    await analyseTab.click();
    await expect(page.locator('#graph-min-degree-output')).toHaveText('2');
    await page.evaluate(() => {
      const el = document.getElementById('graph-min-degree');
      el.value = '3';
      el.dispatchEvent(new Event('input', { bubbles: true }));
    });
    await expect(page.locator('#graph-min-degree-output')).toHaveText('3');
    await expect(page.locator('#graph-tune-min-degree-output')).toHaveText('3');

    // Restore min-degree to 0 so all nodes stay visible for physics tests
    await page.evaluate(() => {
      const el = document.getElementById('graph-min-degree');
      el.value = '0';
      el.dispatchEvent(new Event('input', { bubbles: true }));
    });

    // Switch back to explore tab
    await page.locator('#graph-explore-tab').click();

    // 5. Test Gravity-Based Physics & Sweeps
    // Sweep to extreme tight (400)
    await page.evaluate(() => {
      const el = document.getElementById('graph-gravity');
      el.value = '400';
      el.dispatchEvent(new Event('input', { bubbles: true }));
    });
    await expect(page.locator('#graph-gravity-output')).toHaveText('400');
    expect(await page.evaluate(() => window.__engraphisGraph.state().settings.gravity)).toBe(400);

    // Sweep to Galaxy-zero (0)
    await page.evaluate(() => {
      const el = document.getElementById('graph-gravity');
      el.value = '0';
      el.dispatchEvent(new Event('input', { bubbles: true }));
    });
    await expect(page.locator('#graph-gravity-output')).toHaveText('0');
    expect(await page.evaluate(() => window.__engraphisGraph.state().settings.gravity)).toBe(0);

    // Verify physics is stable at zero (no NaN coordinates, active steps)
    const zeroDiagnostics = await page.evaluate(() => {
      const diag = window.__engraphisGraph.physicsDiagnostics();
      const nodes = window.__engraphisGraph.exportData().nodes;
      const hasNaN = nodes.some(n => Number.isNaN(n.x) || Number.isNaN(n.y));
      return { active: diag.active, hasNaN, steps: diag.steps };
    });
    expect(zeroDiagnostics.hasNaN).toBe(false);
    expect(zeroDiagnostics.active).toBe(true);

    // 6. Test Motion & Physics Switches
    // Orbits pause toggle
    const pauseSwitch = page.locator('#graph-orbits-pause');
    await pauseSwitch.click();
    await expect(pauseSwitch).toBeChecked();
    expect(await page.evaluate(() => window.__engraphisGraph.state().settings.orbitPaused)).toBe(true);
    await pauseSwitch.click();
    await expect(pauseSwitch).not.toBeChecked();
    expect(await page.evaluate(() => window.__engraphisGraph.state().settings.orbitPaused)).toBe(false);

    // Freeze simulation toggle
    const freezeSwitch = page.locator('#graph-freeze');
    await freezeSwitch.click();
    await expect(freezeSwitch).toBeChecked();
    expect(await page.evaluate(() => window.__engraphisGraph.state().settings.frozen)).toBe(true);
    await freezeSwitch.click();
    await expect(freezeSwitch).not.toBeChecked();
    expect(await page.evaluate(() => window.__engraphisGraph.state().settings.frozen)).toBe(false);

    // Flow animation toggle
    const flowSwitch = page.locator('#graph-flow');
    await flowSwitch.click();
    expect(await page.evaluate(() => window.__engraphisGraph.state().settings.flow)).toBe(false);
    await flowSwitch.click();
    expect(await page.evaluate(() => window.__engraphisGraph.state().settings.flow)).toBe(true);

    // Labels toggle
    const labelsSwitch = page.locator('#graph-labels');
    await labelsSwitch.click();
    expect(await page.evaluate(() => window.__engraphisGraph.state().settings.labels)).toBe(true);
    await labelsSwitch.click();
    expect(await page.evaluate(() => window.__engraphisGraph.state().settings.labels)).toBe(false);

    // 7. Test Presets, Styles, and Palettes
    // Layout presets
    const presets = ['compact', 'communities', 'original', 'radial', 'constellation', 'galaxy'];
    for (const p of presets) {
      await page.locator(`[data-graph-preset-choice="${p}"]`).click();
      const currentMode = await page.evaluate(() => window.__engraphisGraph.state().settings.mode);
      expect(currentMode).toBe(p);
    }

    // Rendering styles
    const styles = ['cyber', 'galaxy', 'solar', 'classic'];
    for (const s of styles) {
      await page.locator(`[data-graph-style-choice="${s}"]`).click();
      const currentStyle = await page.evaluate(() => window.__engraphisGraph.state().styleName);
      expect(currentStyle).toBe(s);
    }

    // Palettes
    const palettes = ['aurora', 'ocean', 'ember', 'contrast', 'theme'];
    for (const pal of palettes) {
      await page.locator(`[data-graph-palette-choice="${pal}"]`).click();
      const currentPal = await page.evaluate(() => window.__engraphisGraph.state().palette);
      expect(currentPal).toBe(pal);
    }

    // 8. Test Action Buttons
    await page.locator('#graph-reheat').click();
    await page.locator('#graph-fit').click();
    await page.locator('#graph-clear-focus').click();

    // Reset tuning to preset defaults
    await page.locator('#graph-reset-tuning').click();
    const defaults = await page.evaluate(() => ({
      repel: window.__engraphisGraph.state().settings.repel,
      link: window.__engraphisGraph.state().settings.link,
      gravity: window.__engraphisGraph.state().settings.gravity,
    }));
    expect(defaults.repel).toBe(100);
    expect(defaults.link).toBe(8);
    expect(defaults.gravity).toBe(96);

    // 9. Test Memory Importance Slider in Library View
    await page.locator('.nav-item[data-view="library"]').click();
    const importanceSlider = page.locator('#editor-memory-importance');
    await expect(importanceSlider).toBeAttached();
    await page.evaluate(() => {
      const el = document.getElementById('editor-memory-importance');
      el.value = '0.85';
      el.dispatchEvent(new Event('input', { bubbles: true }));
    });
    await expect(importanceSlider).toHaveAttribute('aria-valuetext', '0.85 importance');

    // 10. Assert zero console errors and zero CSP violations
    const violations = await page.evaluate(() => window.__cspViolations || []);
    expect(violations).toEqual([]);
    expect(session.consoleErrors).toEqual([]);
    expect(session.pageErrors).toEqual([]);
  });
});
