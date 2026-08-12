const { test, expect } = require('@playwright/test');
const fs = require('fs/promises');
const AxeBuilder = require('@axe-core/playwright').default;

const workspace = 'ledger-e2e';
const memories = [
  {
    id: 'mem_database',
    title: 'Database choice',
    content: 'Postgres 16 is the main database.',
    memory_type: 'semantic',
    ingested_at: Date.now() / 1000,
  },
  {
    id: 'mem_safety',
    title: 'Safe rendering',
    content: '<img src=x onerror="window.__ledgerXss=true"> remains inert text.',
    memory_type: 'procedural',
    ingested_at: Date.now() / 1000,
  },
];

function license() {
  return {
    plan: 'local',
    features: [],
    known_features: {},
    cloud_managed: true,
    cloud_access_active: false,
    access_state: 'inactive',
    plan_source: 'local',
    trial: { used: false, active: false, available: true, trial_days: 3 },
    pro_upgrade_url: 'https://cloud.engraphis.test/pro',
    team_upgrade_url: 'https://cloud.engraphis.test/team',
    pro_monthly_upgrade_url: 'https://cloud.engraphis.test/account?plan=pro&interval=monthly#billing',
    pro_annual_upgrade_url: 'https://cloud.engraphis.test/account?plan=pro&interval=annual#billing',
    team_monthly_upgrade_url: 'https://cloud.engraphis.test/account?plan=team&interval=monthly#billing',
    team_annual_upgrade_url: 'https://cloud.engraphis.test/account?plan=team&interval=annual#billing',
    account_url: 'https://cloud.engraphis.test/account',
  };
}

async function mockApi(page, options = {}) {
  const requests = [];
  requests.automationPolicies = [];
  requests.automationBootstraps = [];
  requests.syncRuns = [];
  requests.details = [];
  requests.documentImports = [];
  requests.contextSavingsQueries = [];
  const audit = options.audit || [];
  const receipts = options.receipts || [];
  const workspaceList = options.workspaces || [{ name: workspace, memories: memories.length }];
  const licenseState = options.license || license();
  let automationPolicy = options.automationPolicy || null;
  let documentPolls = 0;
  const memoriesFor = requestUrl => {
    const selected = requestUrl.searchParams.get('workspace') || workspace;
    return (options.memoriesByWorkspace && options.memoriesByWorkspace[selected]) || memories;
  };
  const llmStatus = options.llmStatus || {
    configured: false,
    key_set: false,
    provider: 'openai',
    model: 'gpt-4o-mini',
    extractor: 'none',
    extractor_enabled: false,
    retention_supervisor: 'none',
    default_models: {
      openai: 'gpt-4o-mini',
      anthropic: 'claude-3-5-sonnet-20241022',
      google: 'gemini-1.5-flash',
      openrouter: 'openai/gpt-4o-mini',
    },
    env_snippet: '',
  };
  await page.route('**/api/**', async route => {
    const request = route.request();
    const requestUrl = new URL(request.url());
    const path = requestUrl.pathname.replace(/^\/api/, '');
    requests.push(path);
    requests.details.push({ path, method: request.method() });
    const ok = body => route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(body),
    });
    if (path === '/bootstrap') {
      return ok({
        license: licenseState,
        workspaces: workspaceList,
        stats: {
          memories: memories.length,
          total_rows: memories.length,
          workspaces: 1,
          sessions: 1,
        },
        embedder: { semantic: true },
      });
    }
    if (path === '/stats') {
      return ok({
        memories: memories.length,
        total_rows: memories.length,
        workspaces: 1,
        sessions: 1,
        by_type: { semantic: 1, procedural: 1 },
      });
    }
    if (path === '/memories') {
      return ok({ workspace: requestUrl.searchParams.get('workspace') || workspace,
        memories: memoriesFor(requestUrl) });
    }
    if (path.startsWith('/memory/')) {
      const id = path.split('/').pop();
      return ok({ memory: memories.find(memory => memory.id === id) || null, chain: [] });
    }
    if (path === '/recall') return ok({
      workspace,
      memories: options.rawCandidates || [memories[0]],
    });
    if (path === '/why') return ok({ answer: [memories[0]], supersedes: [memories[1]] });
    if (path === '/timeline') return ok({ history: memories });
    if (path === '/answer') {
      return ok({
        query: 'Which database?',
        grounded: true,
        abstained: false,
        answer: 'Postgres 16 is the main database. [1]',
        support: 0.92,
        citations: [{
          n: 1,
          id: memories[0].id,
          title: memories[0].title,
          content: memories[0].content,
          support: 0.92,
        }],
      });
    }
    if (path === '/proactive') return ok({ workspace, memories });
    if (path === '/audit') return ok({ workspace, audit });
    if (path === '/receipts') return ok({ workspace, receipts });
    if (path === '/graph/scene') {
      if (typeof options.deferGraphRequest === 'function') {
        await options.deferGraphRequest(requestUrl);
      }
      if (options.graphScene) return ok(options.graphScene);
      const asOf = Number(requestUrl.searchParams.get('as_of'));
      const includeUnlinked = requestUrl.searchParams.get('connected_only') !== 'true';
      // Make the historical payload depend on the server's selected-day anchor. A client
      // that filters at midnight would incorrectly discard these later-in-the-day records.
      const validFrom = Number.isFinite(asOf) ? asOf - 1 : 100;
      const validTo = Number.isFinite(asOf) ? asOf - 0.5 : 200;
      return ok({
        nodes: [
          { id: 'postgres', label: 'Postgres', repo_names: ['data-stack'], topic: 'storage', valid_from: validFrom, gravity_mass: 2, visual_radius: 7, community_id: 'storage', x: -20, y: 10 },
          { id: 'engraphis', label: 'Engraphis', repo_names: ['agent-memory'], topic: 'memory', valid_from: validFrom, gravity_mass: 8, visual_radius: 13, community_id: 'memory', anchor_role: 'global', x: 20, y: -10 },
        ].concat(includeUnlinked ? [{
          id: 'unlinked', label: 'Unlinked Note', repo_names: ['agent-memory'], topic: 'memory', valid_from: validFrom, gravity_mass: 1, visual_radius: 5, community_id: 'memory',
        }] : []),
        edges: [{ from: 'engraphis', to: 'postgres', valid_from: validFrom, valid_to: validTo, rest_length: 18, spring_strength: 0.25 }],
        communities: [{ id: 'memory', mass: 9 }, { id: 'storage', mass: 2 }],
        community_bridges: [{ source_community: 'memory', target_community: 'storage', physics_strength: 0.8 }],
        meta: { algorithm_version: 'galaxy-v6', layout_seed: 7 },
        layers: [
          { layer: 'temporal', count: 15 }, { layer: 'entity', count: 26 },
          { layer: 'causal', count: 22 }, { layer: 'semantic', count: 21 }, { layer: 'code', count: 0 },
        ],
      });
    }
    if (path.startsWith('/graph/entities/') && path.endsWith('/memories')) {
      const canonicalId = path.split('/')[3];
      return ok({
        canonical_id: canonicalId,
        evidence: [{
          memory_id: memories[0].id,
          title: memories[0].title,
          excerpt: memories[0].content,
          memory_type: memories[0].memory_type,
          valid_from: 100,
        }],
        totals: { evidence: 1 },
        truncation: { evidence: false },
      });
    }
    if (path === '/health') return ok({ status: 'ok' });
    if (path === '/context-savings') {
      requests.contextSavingsQueries.push(Object.fromEntries(requestUrl.searchParams.entries()));
      return ok({
      format: 'engraphis-context-savings/1',
      scope: { workspace: 'all' },
      workspace_count: 2,
      period: { from_ts: null, to_ts: null },
      release_version: null,
      estimated: {
        eligible_receipt_count: 2,
        baseline_tokens: 4096,
        emitted_tokens: 2048,
        saved_tokens: 2048,
        savings_ratio: 0.5,
        confidence: 'high',
        by_basis: [{
          basis: 'adaptive_history',
          confidence: 'high',
          baseline_tokens: 4096,
          emitted_tokens: 2048,
          saved_tokens: 2048,
          receipt_count: 2,
        }],
        by_token_counter: [{ token_counter: 'test-counter', receipt_count: 2, saved_tokens: 2048 }],
      },
      });
    }
    if (path === '/license') return ok(licenseState);
    if (path === '/auth/state') {
      return ok({
        enabled: false,
        mode: 'open',
        hosted_team: true,
        cloud_url: 'https://cloud.engraphis.test/team',
      });
    }
    if (path === '/llm/status') return ok(llmStatus);
    if (path === '/llm/test') return ok(llmStatus.key_set ? {
      ok: true,
      provider: llmStatus.provider,
      model: llmStatus.model,
    } : {
      ok: false,
      provider: llmStatus.provider,
      model: llmStatus.model,
      error: 'No API key configured. Set ENGRAPHIS_LLM_API_KEY in your .env and restart.',
    });
    if (path === '/llm/extractor') {
      const body = JSON.parse(request.postData() || '{}');
      llmStatus.extractor_enabled = Boolean(body.enabled);
      llmStatus.extractor = llmStatus.extractor_enabled ? 'llm_structured' : 'none';
      return ok({ ok: true, extractor_enabled: llmStatus.extractor_enabled, persisted: true });
    }
    if (path === '/sync/status') return ok(options.syncStatus || { available: false, last: null });
    if (path === '/sync/run') {
      requests.syncRuns.push(JSON.parse(request.postData() || '{}'));
      const summary = options.syncRun || {
        complete: true, attempted: 1, succeeded: 1, exported: 1,
        added: 0, updated: 0, errors: [],
      };
      return ok({ ok: options.syncRunOk ?? summary.complete !== false, summary });
    }
    if (path === '/analytics') {
      return ok({ totals: {}, entities: [], series: [] });
    }
    if (path === '/automation/bootstrap') {
      requests.automationBootstraps.push(requestUrl.searchParams.get('workspace'));
      automationPolicy = {
        ...(options.automationBootstrap || automationPolicy || {}),
        bootstrap_required: false,
      };
      return ok(automationPolicy);
    }
    if (path === '/automation') {
      if (automationPolicy) {
        if (request.method() === 'POST') {
          const body = JSON.parse(request.postData() || '{}');
          requests.automationPolicies.push(body);
          automationPolicy = {
            ...automationPolicy,
            ...body,
            dream: body.dream_enabled,
          };
        }
        return ok(automationPolicy);
      }
      return route.fulfill({
        status: 402,
        contentType: 'application/json',
        body: JSON.stringify({ detail: { error: 'A hosted plan is required.' } }),
      });
    }
    return ok({});
  });
  return requests;
}

function browserErrors(page) {
  const errors = [];
  page.on('console', message => {
    if (message.type() === 'error') errors.push(message.text());
  });
  page.on('pageerror', error => errors.push(error.message));
  return errors;
}

test('Ledger is live, safe, lazy, accessible, and responsive', async ({ page }) => {
  const errors = browserErrors(page);
  const assetRequests = [];
  await page.addInitScript(() => {
    window.__ledgerCspViolations = [];
    document.addEventListener('securitypolicyviolation', event => {
      window.__ledgerCspViolations.push({
        directive: event.violatedDirective,
        blocked: event.blockedURI,
      });
    });
  });
  page.on('request', request => {
    const pathname = new URL(request.url()).pathname;
    if (/\/v2-assets\/(?:vendor\/(?:d3|force-graph)\.min\.js|engraphis-graph\.js)$/.test(pathname)) {
      assetRequests.push(pathname);
    }
  });
  const requests = await mockApi(page);
  const response = await page.goto('/');

  expect(response.headers()['content-security-policy']).not.toContain("'unsafe-inline'");
  await expect(page.getByRole('heading', { name: `What changed in ${workspace}` })).toBeVisible();
  await expect(page.locator('#context-savings-summary')).toHaveCount(0);
  await expect(page.locator('#context-savings-persistent')).not.toBeVisible();
  expect(requests.contextSavingsQueries).toEqual([]);
  await expect(page.locator('#decision-list').getByText('Postgres 16 is the main database.'))
    .toBeVisible();
  await expect(page.locator('#proactive-list').getByText(/<img src=x onerror=/)).toBeVisible();
  expect(await page.evaluate(() => window.__ledgerXss)).toBeUndefined();
  expect(requests).not.toContain('/graph/scene');
  expect(assetRequests).toEqual([]);

  await page.getByRole('button', { name: 'Manage' }).click();
  await expect(page.locator('#context-savings-persistent')).toBeVisible();
  await expect(page.locator('#context-savings-persistent-value')).toHaveText('2,048');
  await expect(page.locator('#context-savings-persistent-rate')).toHaveText('50.0% estimated reduction');
  expect(requests.contextSavingsQueries.every(query => !Object.hasOwn(query, 'workspace'))).toBe(true);

  await page.locator('.nav-item[data-view="relations"]').click();
  await expect(page.locator('#graph-count')).toContainText('3 entities · 1 relations');
  expect(requests).toContain('/graph/scene');
  expect(assetRequests).toEqual([
    '/v2-assets/vendor/d3.min.js',
    '/v2-assets/vendor/force-graph.min.js',
    '/v2-assets/engraphis-graph.js',
  ]);

  await page.getByRole('button', { name: /^Ask/ }).click();
  await page.getByRole('textbox', { name: 'Question' }).fill('Which database?');
  await page.getByRole('button', { name: 'Grounded answer', exact: true }).click();
  await expect(page.locator('#answer-panel').getByText('Postgres 16 is the main database. [1]'))
    .toBeVisible();

  await page.getByRole('button', { name: 'Library' }).click();
  await page.getByRole('searchbox', { name: 'Search' }).fill('safe');
  await expect(page.getByRole('heading', { name: 'Safe rendering' })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Database choice' })).toBeHidden();

  await page.setViewportSize({ width: 375, height: 812 });
  await expect(page.getByRole('button', { name: 'Manage' })).toBeVisible();
  await expect(page.locator('#workspace-select')).toBeVisible();
  await expect(page.locator('#sidebar-pro-cta')).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);

  const accessibility = await new AxeBuilder({ page }).analyze();
  expect(accessibility.violations).toEqual([]);
  expect(await page.evaluate(() => window.__ledgerCspViolations)).toEqual([]);
  expect(errors).toEqual([]);
});

test('Ledger retries a failed lazy graph load and opens search evidence by keyboard', async ({ page }) => {
  await mockApi(page);
  let d3Attempts = 0;
  await page.route('**/v2-assets/vendor/d3.min.js*', async route => {
    d3Attempts += 1;
    if (d3Attempts === 1) return route.abort('failed');
    return route.fallback();
  });
  await page.goto('/');

  await page.locator('.nav-item[data-view="relations"]').click();
  await expect(page.locator('#graph-empty')).toContainText('Graph unavailable');
  await page.getByRole('button', { name: 'Reload data' }).click();
  await expect(page.locator('#graph-count')).toContainText('3 entities · 1 relations');
  expect(d3Attempts).toBe(2);

  await page.locator('#graph-search').fill('Engraphis');
  const result = page.locator('#graph-search-results button').filter({ hasText: 'Engraphis' });
  await result.focus();
  await page.keyboard.press('Enter');
  const dialog = page.locator('#graph-connections-dialog');
  await expect(dialog).toBeVisible();
  await expect(dialog.locator('#graph-connections-list')).toContainText('Postgres');
  const connection = dialog.locator('.graph-connection-row').filter({ hasText: 'Postgres' })
    .getByRole('button', { name: 'Memories' });
  await connection.focus();
  await page.keyboard.press('Enter');
  await expect(dialog.locator('#graph-connection-memory-list')).toContainText('Database choice');
});

test('Ledger cache-busts a graph renderer that fetched but did not register', async ({ page }) => {
  await mockApi(page);
  const rendererRequests = [];
  await page.route('**/v2-assets/engraphis-graph.js*', async route => {
    rendererRequests.push(route.request().url());
    if (rendererRequests.length === 1) {
      // A valid 200 response that never defines EngraphisGraph models a stale cached asset that
      // fetched successfully but failed while executing. `onerror` cannot detect this case.
      return route.fulfill({
        status: 200,
        contentType: 'application/javascript',
        body: 'window.__nonRegisteringGraphAsset = true;',
      });
    }
    return route.fallback();
  });
  await page.goto('/');
  await page.locator('.nav-item[data-view="relations"]').click();
  await expect(page.locator('#graph-empty')).toContainText('Graph unavailable');
  expect(rendererRequests).toHaveLength(1);
  const first = new URL(rendererRequests[0]);
  expect(first.searchParams.get('v')).toBe('20260812-graph-capacity-2x-1');
  expect(first.searchParams.has('retry')).toBe(false);

  await page.getByRole('button', { name: 'Reload data' }).click();
  await expect(page.locator('#graph-count')).toContainText('3 entities · 1 relations');
  expect(rendererRequests).toHaveLength(2);
  const second = new URL(rendererRequests[1]);
  expect(second.searchParams.get('v')).toBe('20260812-graph-capacity-2x-1');
  expect(second.searchParams.get('retry')).toBe('1');
});

test('Ledger narrowly migrates only the legacy Galaxy spacing default', async ({ page }) => {
  const key = 'engraphis-ledger-graph-preferences-v1';
  const writePreferences = preferences => page.evaluate(({ storageKey, value }) => {
    localStorage.setItem(storageKey, JSON.stringify(value));
  }, { storageKey: key, value: preferences });
  const readPreferences = () => page.evaluate(storageKey => {
    const value = localStorage.getItem(storageKey);
    return value === null ? null : JSON.parse(value);
  }, key);

  await mockApi(page);
  await page.goto('/');
  await expect(page.locator('#graph-repel')).toHaveValue('60');
  await expect(page.locator('#graph-link')).toHaveValue('8');
  await expect(page.locator('#graph-gravity')).toHaveValue('48');
  // A first-time dashboard may use the new HTML default without manufacturing preferences.
  expect(await readPreferences()).toBeNull();

  await page.evaluate(() => {
    [['graph-repel', '120'], ['graph-link', '80'], ['graph-gravity', '400']]
      .forEach(([id, value]) => {
        const control = document.getElementById(id);
        control.value = value;
        control.dispatchEvent(new Event('input', { bubbles: true }));
      });
    document.getElementById('graph-reset-tuning').click();
  });
  await expect(page.locator('#graph-repel')).toHaveValue('60');
  await expect(page.locator('#graph-link')).toHaveValue('8');
  await expect(page.locator('#graph-gravity')).toHaveValue('48');

  await writePreferences({
    preset: 'galaxy', style: 'solar', tuning: { repel: 48, link: 8, gravity: 0 },
    layers: { temporal: false, entity: true, causal: false, semantic: true, code: false },
  });
  await page.reload();
  await expect(page.locator('#graph-repel')).toHaveValue('60');
  await expect(page.locator('#graph-gravity')).toHaveValue('0');
  const migrated = await readPreferences();
  expect(migrated.physicsVersion).toBe(2);
  expect(migrated.preset).toBe('galaxy');
  expect(migrated.style).toBe('solar');
  expect(migrated.tuning.repel).toBe(60);
  expect(migrated.tuning.link).toBe(8);
  expect(migrated.tuning.gravity).toBe(0);
  expect(migrated.layers).toEqual({
    temporal: false, entity: true, causal: false, semantic: true, code: false,
  });

  await writePreferences({
    preset: 'galaxy', style: 'galaxy', tuning: { repel: 73, link: 21, gravity: 0 },
  });
  await page.reload();
  await expect(page.locator('#graph-repel')).toHaveValue('73');
  await expect(page.locator('#graph-link')).toHaveValue('21');
  await expect(page.locator('#graph-gravity')).toHaveValue('0');
  const custom = await readPreferences();
  expect(custom.physicsVersion).toBe(2);
  expect(custom.tuning.repel).toBe(73);
  expect(custom.tuning.link).toBe(21);
  expect(custom.tuning.gravity).toBe(0);

  // Once versioned, 48 is a deliberate user selection rather than the retired default.
  await writePreferences({
    physicsVersion: 2, preset: 'galaxy', tuning: { repel: 48, gravity: 0 },
  });
  await page.reload();
  await expect(page.locator('#graph-repel')).toHaveValue('48');
  expect((await readPreferences()).tuning.repel).toBe(48);
});

test('Ledger deadline includes stalled graph assets and Reload data starts a fresh attempt', async ({ page }) => {
  await page.addInitScript(() => {
    const nativeSetTimeout = window.setTimeout.bind(window);
    let shortenedGraphDeadline = false;
    window.setTimeout = (callback, delay, ...args) => {
      const firstGraphDeadline = delay === 12_000 && !shortenedGraphDeadline;
      if (firstGraphDeadline) shortenedGraphDeadline = true;
      return nativeSetTimeout(callback, firstGraphDeadline ? 80 : delay, ...args);
    };
  });
  await mockApi(page);
  let releaseStalledAsset;
  const stalledAsset = new Promise(resolve => { releaseStalledAsset = resolve; });
  let d3Attempts = 0;
  await page.route('**/v2-assets/vendor/d3.min.js*', async route => {
    d3Attempts += 1;
    if (d3Attempts === 1) {
      await stalledAsset;
      return route.abort('failed');
    }
    return route.fallback();
  });
  await page.goto('/');
  await page.locator('.nav-item[data-view="relations"]').click();
  await expect(page.locator('#graph-empty')).toContainText('Graph loading timed out');

  await page.getByRole('button', { name: 'Reload data' }).click();
  await expect(page.locator('#graph-count')).toContainText('3 entities · 1 relations', { timeout: 15000 });
  expect(d3Attempts).toBe(2);
  releaseStalledAsset();
});

test('Ledger requests a remote token in a masked retryable dialog', async ({ page }) => {
  let authenticated = false;
  await mockApi(page);
  await page.route('**/api/bootstrap*', async route => {
    if (authenticated) return route.fallback();
    return route.fulfill({
      status: 401,
      contentType: 'application/json',
      body: JSON.stringify({ detail: 'Authentication required' }),
    });
  });
  await page.route('**/auth/session', async route => {
    const token = JSON.parse(route.request().postData() || '{}').token;
    if (token !== 'correct-token') {
      return route.fulfill({
        status: 401,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'Invalid deployment token' }),
      });
    }
    authenticated = true;
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ authenticated: true }),
    });
  });
  await page.goto('/');

  const dialog = page.locator('#browser-auth-dialog');
  const token = page.locator('#browser-auth-token');
  await expect(dialog).toBeVisible();
  await expect(token).toHaveAttribute('type', 'password');
  await expect(token).toHaveAttribute('autocomplete', 'off');

  await token.fill('wrong-token');
  await dialog.getByRole('button', { name: 'Connect', exact: true }).click();
  await expect(page.locator('#browser-auth-error')).toContainText('Invalid deployment token');
  await expect(token).toHaveValue('');

  await token.fill('correct-token');
  await Promise.all([
    page.waitForNavigation(),
    dialog.getByRole('button', { name: 'Connect', exact: true }).click(),
  ]);
  await expect(page.getByRole('heading', { name: `What changed in ${workspace}` })).toBeVisible();
  expect(page.url()).not.toContain('token');
});

test('Ledger exposes Cloud Sync status and reports partial runs as incomplete', async ({ page }) => {
  const requests = await mockApi(page, {
    syncStatus: {
      available: true,
      last: {
        summary: {
          complete: true, attempted: 2, succeeded: 2, exported: 1,
          added: 1, updated: 0, errors: [],
        },
      },
    },
    syncRun: {
      complete: false, attempted: 2, succeeded: 1, exported: 1,
      added: 0, updated: 0, errors: ['relay unavailable'],
    },
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'Manage' }).click();
  await page.getByRole('tab', { name: 'Cloud Sync' }).click();

  const result = page.locator('#sync-result');
  await expect(result).toContainText('Last sync complete');
  await page.getByRole('button', { name: 'Sync now' }).click();
  await expect(result).toContainText('Last sync incomplete');
  await expect(result).toContainText('1 error');
  expect(requests.syncRuns).toEqual([{}]);
});

test('Ledger treats a Cloud Sync ok:false response as incomplete', async ({ page }) => {
  await mockApi(page, {
    syncStatus: { available: true, last: null },
    syncRunOk: false,
    syncRun: {
      complete: true, attempted: 1, succeeded: 1, exported: 1,
      added: 0, updated: 0, errors: [],
    },
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'Manage' }).click();
  await page.getByRole('tab', { name: 'Cloud Sync' }).click();
  await page.getByRole('button', { name: 'Sync now' }).click();

  await expect(page.locator('#sync-result')).toContainText('Last sync incomplete');
  await expect(page.locator('#notice-text')).toContainText('Cloud Sync is incomplete');
});

test('Ledger initializes hosted automation only after an explicit upload action', async ({ page }) => {
  page.on('dialog', dialog => dialog.accept());
  const requests = await mockApi(page, {
    automationPolicy: {
      enabled: false,
      cadence_hours: 24,
      dream_enabled: true,
      dream_min_new: 25,
      dream_idle_minutes: 15,
      infer: false,
      version: 0,
      bootstrap_required: true,
    },
    automationBootstrap: {
      enabled: true,
      cadence_hours: 24,
      dream_enabled: true,
      dream_min_new: 25,
      dream_idle_minutes: 15,
      infer: false,
      version: 1,
    },
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'Manage' }).click();
  await page.getByRole('tab', { name: 'Automation' }).click();

  const result = page.locator('#automation-result');
  await expect(result).toContainText('No upload occurs until you choose this action.');
  await expect(page.locator('#automation-enabled')).toHaveCount(0);
  await page.getByRole('button', { name: 'Initialize hosted automation' }).click();
  await expect(page.locator('#automation-enabled')).toBeChecked();
  expect(requests.automationBootstraps).toEqual([workspace]);
  expect(requests.automationPolicies).toEqual([]);
});

test('provenance merges audit seconds and receipt milliseconds chronologically', async ({ page }) => {
  await mockApi(page, {
    audit: [{ id: 'aud_older', ts: 1_700_000_100, action: 'older audit action' }],
    receipts: [{ id: 'rcpt_newer', ts_ms: 1_700_000_200_000, operation: 'newer receipt operation' }],
  });
  await page.goto('/');

  await page.getByRole('button', { name: 'Provenance why, timeline, receipts' }).click();
  await page.getByRole('tab', { name: 'Audit & receipts' }).click();

  const cards = page.locator('#audit-list .audit-card');
  await expect(cards).toHaveCount(2);
  await expect(cards.nth(0)).toContainText('newer receipt operation');
  await expect(cards.nth(1)).toContainText('older audit action');
});

test('memory listings open the editable Library detail from every dashboard view', async ({ page }) => {
  await mockApi(page);
  await page.goto('/');

  await page.getByRole('button', { name: 'Library memories and imports' }).click();
  const libraryOptions = page.locator('#library-list [role="option"]');
  await expect(libraryOptions.first()).toHaveAttribute('tabindex', '0');
  await expect(libraryOptions.nth(1)).toHaveAttribute('tabindex', '-1');
  await libraryOptions.first().press('ArrowDown');
  await expect(libraryOptions.nth(1)).toHaveAttribute('tabindex', '0');
  await page.getByRole('button', { name: 'Today changes and decisions' }).click();

  await page.locator('#proactive-list [data-memory-id="mem_database"]').click();
  await expect(page.locator('#memory-detail h2')).toHaveText('Database choice');
  await expect(page.locator('#memory-detail').getByRole('button', { name: 'Edit' })).toBeVisible();
  await expect(page.locator('#memory-detail').getByRole('button', { name: 'Retire' })).toBeVisible();

  await page.getByRole('button', { name: 'Ask grounded answers' }).click();
  await page.getByRole('textbox', { name: 'Question' }).fill('Which database?');
  await page.getByRole('button', { name: 'Grounded answer', exact: true }).click();
  await page.locator('#answer-panel [data-memory-id="mem_database"]').click();
  await expect(page.locator('#memory-detail h2')).toHaveText('Database choice');

  await page.getByRole('button', { name: 'Provenance why, timeline, receipts' }).click();
  await page.getByLabel('Claim or topic').fill('Which database?');
  await page.getByRole('button', { name: 'Trace belief' }).click();
  await page.locator('#why-result [data-memory-id="mem_safety"]').click();
  await expect(page.locator('#memory-detail h2')).toHaveText('Safe rendering');

  await page.getByRole('button', { name: 'Provenance why, timeline, receipts' }).click();
  await page.getByRole('tab', { name: 'Timeline' }).click();
  await page.locator('#timeline-input').fill('database');
  await page.getByRole('button', { name: 'Show history' }).click();
  await page.locator('#timeline-result [data-memory-id="mem_database"]').click();
  await expect(page.locator('#memory-detail h2')).toHaveText('Database choice');
});

test('switching workspaces clears a stale memory editor before it can write elsewhere', async ({ page }) => {
  const otherWorkspace = 'ledger-other';
  await mockApi(page, {
    workspaces: [
      { name: workspace, memories: memories.length },
      { name: otherWorkspace, memories: 1 },
    ],
    memoriesByWorkspace: {
      [workspace]: memories,
      [otherWorkspace]: [{
        id: 'mem_other', title: 'Other workspace memory', content: 'Separate evidence.',
        memory_type: 'semantic', ingested_at: Date.now() / 1000,
      }],
    },
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'Library memories and imports' }).click();
  await page.locator('#library-list [data-memory-id="mem_database"]').click();
  await expect(page.locator('#memory-detail h2')).toHaveText('Database choice');
  await page.locator('#memory-detail').getByRole('button', { name: 'Edit' }).click();
  await expect(page.locator('#memory-editor')).toBeVisible();

  await page.getByLabel('Active workspace').selectOption(otherWorkspace);

  await expect(page.locator('#memory-editor')).toBeHidden();
  await expect(page.locator('#memory-detail')).toBeHidden();
  await expect(page.locator('#library-list')).toContainText('Other workspace memory');
  await expect(page.locator('#library-list')).not.toContainText('Database choice');
});

test('late Ask, audit, and automation responses cannot cross workspace boundaries', async ({ page }) => {
  const otherWorkspace = 'ledger-other';
  let releaseAsk;
  let resolveAskStarted;
  let askCompleted = 0;
  const askGate = new Promise(resolve => { releaseAsk = resolve; });
  const askStarted = new Promise(resolve => { resolveAskStarted = resolve; });
  let delayAudit = false;
  let releaseAudit;
  let resolveAuditStarted;
  let auditCompleted = 0;
  const auditGate = new Promise(resolve => { releaseAudit = resolve; });
  const auditStarted = new Promise(resolve => { resolveAuditStarted = resolve; });
  let delayAutomation = false;
  let releaseAutomation;
  let resolveAutomationStarted;
  let automationCompleted = 0;
  let savedAutomation = null;
  const automationGate = new Promise(resolve => { releaseAutomation = resolve; });
  const automationStarted = new Promise(resolve => { resolveAutomationStarted = resolve; });

  await mockApi(page, {
    workspaces: [
      { name: workspace, memories: memories.length },
      { name: otherWorkspace, memories: 1 },
    ],
    memoriesByWorkspace: {
      [workspace]: memories,
      [otherWorkspace]: [{
        id: 'mem_other', title: 'Other workspace memory', content: 'Separate evidence.',
        memory_type: 'semantic', ingested_at: Date.now() / 1000,
      }],
    },
  });
  await page.route('**/api/answer*', async route => {
    const request = route.request();
    const requestUrl = new URL(request.url());
    const selected = requestUrl.searchParams.get('workspace')
      || JSON.parse(request.postData() || '{}').workspace;
    if (selected === workspace) {
      resolveAskStarted();
      await askGate;
      askCompleted += 1;
    }
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        query: 'workspace boundary',
        grounded: true,
        abstained: false,
        answer: `${selected} grounded answer [1]`,
        support: 0.9,
        citations: [{
          n: 1, id: `mem_${selected}`, title: `${selected} citation`,
          content: `${selected} evidence`, support: 0.9,
        }],
      }),
    });
  });
  await page.route('**/api/recall*', async route => {
    const requestUrl = new URL(route.request().url());
    const selected = requestUrl.searchParams.get('workspace');
    if (selected === workspace) {
      resolveAskStarted();
      await askGate;
      askCompleted += 1;
    }
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        workspace: selected,
        memories: [{
          id: `raw_${selected}`, title: `${selected} raw result`,
          content: `${selected} raw evidence`, memory_type: 'semantic',
          ingested_at: Date.now() / 1000,
        }],
      }),
    });
  });
  for (const endpoint of ['audit', 'receipts', 'context-savings']) {
    await page.route(`**/api/${endpoint}*`, async route => {
      const requestUrl = new URL(route.request().url());
      const selected = requestUrl.searchParams.get('workspace');
      if (delayAudit && selected === workspace) {
        resolveAuditStarted();
        await auditGate;
        auditCompleted += 1;
      }
      const body = endpoint === 'audit'
        ? { audit: [{
          id: `aud_${selected}`, actor: 'tester', action: `${selected} audit`,
          timestamp: Date.now() / 1000,
        }] }
        : endpoint === 'receipts'
          ? { receipts: [{
            id: `receipt_${selected}`, operation: `${selected} receipt`,
            created_at: Date.now() / 1000, verified: true,
          }] }
          : {
            format: 'engraphis-context-savings/1',
            scope: { workspace: 'all' },
            workspace_count: 2,
            period: { from_ts: null, to_ts: null },
            release_version: null,
            estimated: {
              eligible_receipt_count: 2,
              baseline_tokens: 4096,
              emitted_tokens: 2048,
              saved_tokens: 2048,
              savings_ratio: 0.5,
              confidence: 'high',
              by_basis: [{
                basis: 'adaptive_history',
                confidence: 'high',
                baseline_tokens: 4096,
                emitted_tokens: 2048,
                saved_tokens: 2048,
                receipt_count: 2,
              }],
              by_token_counter: [{ token_counter: 'test-counter', receipt_count: 2, saved_tokens: 2048 }],
            },
          };
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(body),
      });
    });
  }
  await page.route('**/api/automation*', async route => {
    const request = route.request();
    const requestUrl = new URL(request.url());
    const selected = requestUrl.searchParams.get('workspace');
    if (request.method() === 'POST') {
      savedAutomation = {
        workspace: selected,
        body: JSON.parse(request.postData() || '{}'),
      };
    } else if (delayAutomation && selected === workspace) {
      resolveAutomationStarted();
      await automationGate;
      automationCompleted += 1;
    }
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        enabled: false,
        cadence_hours: selected === workspace ? 24 : 72,
        dream_enabled: false,
        dream_min_new: 25,
        dream_idle_minutes: 30,
        infer: false,
      }),
    });
  });

  await page.goto('/');
  await page.getByRole('button', { name: 'Ask grounded answers' }).click();
  await page.getByRole('textbox', { name: 'Question' }).fill('Workspace boundary?');
  await page.getByRole('button', { name: 'Grounded answer', exact: true }).click();
  await askStarted;
  await page.getByLabel('Active workspace').selectOption(otherWorkspace);
  await expect(page.locator('#today-title')).toHaveText(`What changed in ${otherWorkspace}`);
  await page.getByRole('textbox', { name: 'Question' }).fill('Workspace boundary?');
  await page.getByRole('button', { name: 'Grounded answer', exact: true }).click();
  await expect(page.locator('#answer-panel')).toContainText(`${otherWorkspace} grounded answer`);
  releaseAsk();
  await expect.poll(() => askCompleted).toBe(2);
  await expect(page.locator('#answer-panel')).toContainText(`${otherWorkspace} grounded answer`);
  await expect(page.locator('#answer-panel')).not.toContainText(`${workspace} grounded answer`);

  await page.getByLabel('Active workspace').selectOption(workspace);
  await expect(page.locator('#today-title')).toHaveText(`What changed in ${workspace}`);
  delayAudit = true;
  await page.getByRole('button', { name: 'Provenance why, timeline, receipts' }).click();
  await page.getByRole('tab', { name: 'Audit & receipts' }).click();
  await auditStarted;
  await page.getByLabel('Active workspace').selectOption(otherWorkspace);
  await page.getByRole('tab', { name: 'Audit & receipts' }).click();
  await expect(page.locator('#audit-list')).toContainText(`${otherWorkspace} audit`);
  releaseAudit();
  // Audit and receipts remain workspace-scoped. Context savings is now a global visible-
  // workspace aggregate, so it deliberately carries no workspace query to join this gate.
  await expect.poll(() => auditCompleted).toBe(2);
  await expect(page.locator('#audit-list')).toContainText(`${otherWorkspace} audit`);
  await expect(page.locator('#audit-list')).not.toContainText(`${workspace} audit`);

  await page.getByLabel('Active workspace').selectOption(workspace);
  delayAutomation = true;
  await page.getByRole('button', { name: 'Manage' }).click();
  await page.getByRole('tab', { name: 'Automation' }).click();
  await automationStarted;
  await page.getByLabel('Active workspace').selectOption(otherWorkspace);
  await page.getByRole('tab', { name: 'Automation' }).click();
  const form = page.locator('#automation-result form');
  await expect(form).toHaveAttribute('data-workspace', otherWorkspace);
  await expect(page.locator('#automation-cadence')).toHaveValue('72');
  releaseAutomation();
  await expect.poll(() => automationCompleted).toBe(1);
  await expect(form).toHaveAttribute('data-workspace', otherWorkspace);
  await expect(page.locator('#automation-cadence')).toHaveValue('72');

  await page.locator('#automation-cadence').fill('96');
  await form.getByRole('button', { name: 'Save hosted policy' }).click();
  await expect.poll(() => savedAutomation && savedAutomation.workspace).toBe(otherWorkspace);
  expect(savedAutomation.body.cadence_hours).toBe(96);
});

test('late provenance and plan responses cannot cross workspace boundaries', async ({ page }) => {
  const workspace = 'ledger-e2e';
  const otherWorkspace = 'ledger-other';
  await mockApi(page, {
    workspaces: [
      { name: workspace, description: 'Primary' },
      { name: otherWorkspace, description: 'Other' },
    ],
  });

  const whyGate = {};
  whyGate.promise = new Promise(resolve => { whyGate.release = resolve; });
  const whyStarted = new Promise(resolve => { whyGate.started = resolve; });
  const timelineGate = {};
  timelineGate.promise = new Promise(resolve => { timelineGate.release = resolve; });
  const timelineStarted = new Promise(resolve => { timelineGate.started = resolve; });
  const plansGate = {};
  plansGate.promise = new Promise(resolve => { plansGate.release = resolve; });
  const plansStarted = new Promise(resolve => { plansGate.started = resolve; });
  let delayWhy = false;
  let delayTimeline = false;
  let delayPlans = false;

  let whyCompleted = 0;
  let timelineCompleted = 0;
  let plansCompleted = 0;
  const scopedMemory = (selected, suffix) => ({
    id: `mem_${suffix}_${selected}`,
    title: `${selected} ${suffix}`,
    content: `${selected} ${suffix} evidence`,
    memory_type: 'semantic',
    ingested_at: Date.now() / 1000,
  });
  await page.route('**/api/why*', async route => {
    const selected = new URL(route.request().url()).searchParams.get('workspace');
    if (delayWhy && selected === workspace) {
      whyGate.started();
      await whyGate.promise;
      whyCompleted += 1;
    }
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ answer: [scopedMemory(selected, 'belief')], supersedes: [] }),
    });
  });
  await page.route('**/api/timeline*', async route => {
    const selected = new URL(route.request().url()).searchParams.get('workspace');
    if (delayTimeline && selected === workspace) {
      timelineGate.started();
      await timelineGate.promise;
      timelineCompleted += 1;
    }
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ history: [scopedMemory(selected, 'timeline')] }),
    });
  });
  await page.route('**/api/license*', async route => {
    const selected = new URL(route.request().url()).searchParams.get('workspace');
    if (delayPlans && selected === workspace) {
      plansGate.started();
      await plansGate.promise;
      plansCompleted += 1;
    }
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        plan: selected === otherWorkspace ? 'team' : selected === workspace ? 'pro' : 'free',
        plan_source: 'session',
        cloud_access_active: true,
        trial: { available: false },
      }),
    });
  });

  try {
    await page.goto('/');
    await page.getByRole('button', { name: 'Provenance why, timeline, receipts' }).click();
    delayWhy = true;
    await page.getByLabel('Claim or topic').fill('Workspace boundary');
    await page.getByRole('button', { name: 'Trace belief' }).click();
    await whyStarted;
    await page.getByLabel('Active workspace').selectOption(otherWorkspace);
    await page.getByLabel('Claim or topic').fill('Workspace boundary');
    await page.getByRole('button', { name: 'Trace belief' }).click();
    await expect(page.locator('#why-result')).toContainText(`${otherWorkspace} belief`);
    whyGate.release();
    await expect.poll(() => whyCompleted).toBe(1);
    await expect(page.locator('#why-result')).not.toContainText(`${workspace} belief`);

    await page.getByLabel('Active workspace').selectOption(workspace);
    await page.getByRole('tab', { name: 'Timeline' }).click();
    delayTimeline = true;
    await page.locator('#timeline-input').fill('Workspace boundary');
    await page.getByRole('button', { name: 'Show history' }).click();
    await timelineStarted;
    await page.getByLabel('Active workspace').selectOption(otherWorkspace);
    await page.locator('#timeline-input').fill('Workspace boundary');
    await page.getByRole('button', { name: 'Show history' }).click();
    await expect(page.locator('#timeline-result')).toContainText(`${otherWorkspace} timeline`);
    timelineGate.release();
    await expect.poll(() => timelineCompleted).toBe(1);
    await expect(page.locator('#timeline-result')).not.toContainText(`${workspace} timeline`);

    await page.getByLabel('Active workspace').selectOption(workspace);
    await page.getByRole('button', { name: 'Manage' }).click();
    delayPlans = true;
    await page.getByRole('tab', { name: 'Plans & billing' }).click();
    await plansStarted;
    await page.getByLabel('Active workspace').selectOption(otherWorkspace);
    const teamCard = page.locator('#plan-cards .plan-card')
      .filter({ has: page.locator('h2', { hasText: 'Team' }) });
    await expect(teamCard.locator('.eyebrow')).toHaveText('Current plan');
    plansGate.release();
    await expect.poll(() => plansCompleted).toBe(1);
    await expect(teamCard.locator('.eyebrow')).toHaveText('Current plan');
  } finally {
    whyGate.release();
    timelineGate.release();
    plansGate.release();
  }
});

test('Ask keeps the raw retrieval preview alongside its single grounded answer', async ({ page }) => {
  const rawOnlyCandidate = {
    id: 'mem_raw_only',
    title: 'Uncited raw candidate',
    content: 'This candidate is shown for inspection but is not a grounded citation.',
    memory_type: 'semantic',
    ingested_at: Date.now() / 1000,
  };
  const requests = await mockApi(page, { rawCandidates: [memories[0], rawOnlyCandidate] });
  await page.goto('/');

  await page.getByRole('button', { name: 'Ask grounded answers' }).click();
  await page.getByRole('textbox', { name: 'Question' }).fill('Which database?');
  await page.getByRole('button', { name: 'Grounded answer', exact: true }).click();

  await expect(page.locator('#answer-panel').getByText('Postgres 16 is the main database. [1]'))
    .toBeVisible();
  await page.locator('.retrieval-details summary').click();
  await expect(page.locator('#retrieval-list').getByRole('heading', {
    name: 'Uncited raw candidate',
  })).toBeVisible();
  expect(requests.filter(path => path === '/answer')).toHaveLength(1);
  expect(requests.filter(path => path === '/recall')).toHaveLength(1);
});

test('Graph & Relationships uses the visual explorer controls and applies their state', async ({ page }) => {
  await mockApi(page);
  await page.goto('/');
  const initialGraphRequest = page.waitForRequest(request => {
    const url = new URL(request.url());
    return url.pathname === '/api/graph/scene'
      && url.searchParams.get('level') === 'overview'
      && url.searchParams.get('node_limit') === '1000'
      && url.searchParams.get('edge_limit') === '2000'
      && !url.searchParams.has('connected_only');
  });
  await page.locator('.nav-item[data-view="relations"]').click();
  await initialGraphRequest;
  await expect(page.locator('#graph-count')).toContainText('3 entities · 1 relations');

  await expect(page.getByRole('tab', { name: 'Explore' })).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByPlaceholder('Find an entity…')).toBeVisible();
  await expect(page.getByPlaceholder('Filter to a repository or topic…')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Cyber' })).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByRole('button', { name: 'Galaxy gravity' })).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByLabel('Size by')).toHaveValue('evidence_mass');
  await expect(page.getByLabel('Size by')).toBeDisabled();
  await expect(page.locator('#graph-repel-label')).toHaveText('Orbital speed');
  await expect(page.locator('#graph-repel')).toHaveValue('60');
  await expect(page.locator('#graph-link-label')).toHaveText('Link distance · tight ↔ loose');
  await expect(page.locator('#graph-link')).toHaveValue('8');
  await expect(page.locator('#graph-gravity-label')).toHaveText('Gravity strength · loose ↔ tight');
  await expect(page.locator('#graph-gravity')).toHaveValue('48');
  await expect(page.getByRole('button', { name: 'Schema drift' })).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByRole('button', { name: 'Operations' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'People' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Code ↔ memory' })).toBeVisible();
  await expect(page.locator('#graph-flow-speed')).toHaveValue('45');
  await expect(page.locator('#graph-layer-temporal-count')).toHaveText('15');

  await expect(page.getByRole('button', { name: 'Show all nodes' })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Hide unlinked nodes' })).toHaveAttribute('aria-pressed', 'true');
  await expect(page.locator('#graph-count')).toContainText('3 entities · 1 relations');
  const paletteNotice = page.locator('#notice-banner');
  await page.locator('[data-graph-palette-choice="ember"]').click();
  await expect(paletteNotice).toHaveText('ember palette applied to the graph.');
  await expect(paletteNotice).toBeHidden({ timeout: 4500 });
  const hideUnlinkedRequest = page.waitForRequest(request => {
    const url = new URL(request.url());
    return url.pathname === '/api/graph/scene' && url.searchParams.get('connected_only') === 'true';
  });
  await page.getByRole('button', { name: 'Hide unlinked nodes' }).click();
  await hideUnlinkedRequest;
  await expect(page.getByRole('button', { name: 'Show unlinked nodes' })).toHaveAttribute('aria-pressed', 'false');
  await expect(page.locator('#graph-count')).toContainText('2 entities · 1 relations');
  const showUnlinkedRequest = page.waitForRequest(request => {
    const url = new URL(request.url());
    return url.pathname === '/api/graph/scene' && !url.searchParams.has('connected_only');
  });
  await page.getByRole('button', { name: 'Show unlinked nodes' }).click();
  await showUnlinkedRequest;
  await expect(page.getByRole('button', { name: 'Hide unlinked nodes' })).toHaveAttribute('aria-pressed', 'true');
  await expect(page.locator('#graph-count')).toContainText('3 entities · 1 relations');

  const codeRequest = page.waitForRequest(request => {
    const url = new URL(request.url());
    return url.pathname === '/api/graph/scene' && url.searchParams.get('include_code') === 'true';
  });
  await page.getByRole('button', { name: 'Code ↔ memory' }).click();
  await codeRequest;
  await expect(page.getByRole('button', { name: 'Code ↔ memory' })).toHaveAttribute('aria-pressed', 'true');
  await page.getByRole('button', { name: 'Schema drift' }).click();
  await expect(page.getByRole('button', { name: 'Schema drift' })).toHaveAttribute('aria-pressed', 'true');

  await page.locator('#graph-flow-speed').evaluate(control => {
    control.value = '67';
    control.dispatchEvent(new Event('input', { bubbles: true }));
  });
  await expect(page.locator('#graph-flow-speed')).toHaveValue('67');
  await page.locator('#graph-repel').evaluate(control => {
    control.value = '80';
    control.dispatchEvent(new Event('input', { bubbles: true }));
  });
  await expect(page.locator('#graph-repel-output')).toHaveText('80');
  await page.getByRole('button', { name: 'Reset to preset defaults' }).click();
  await expect(page.locator('#graph-repel-output')).toHaveText('48');
  await expect(page.locator('#graph-line-width-output')).toHaveText('0.72');
  await expect(page.locator('#graph-label-density-output')).toHaveText('24');
  await page.getByRole('button', { name: 'Save current' }).click();
  await expect(page.locator('#graph-saved-view-status')).toHaveText('Current graph view saved locally.');

  await page.getByRole('button', { name: 'Galaxy', exact: true }).click();
  await expect(page.locator('#graph-canvas')).toHaveAttribute('data-graph-style', 'galaxy');
  await page.getByRole('button', { name: 'Compact' }).click();
  await expect(page.locator('#graph-mode')).toContainText('Compact');
  await page.getByRole('button', { name: 'Type' }).click();
  await expect(page.getByRole('button', { name: 'Type' })).toHaveAttribute('aria-pressed', 'true');

  const flow = page.getByRole('switch', { name: 'Relation flow' });
  await flow.click();
  await expect(flow).toHaveAttribute('aria-checked', 'false');
  const freeze = page.getByRole('switch', { name: 'Freeze simulation' });
  await expect(freeze).toHaveAttribute('aria-checked', 'false');
  await freeze.click();
  await expect(freeze).toHaveAttribute('aria-checked', 'true');
  await freeze.click();
  await expect(freeze).toHaveAttribute('aria-checked', 'false');
  await freeze.click();
  await expect(freeze).toHaveAttribute('aria-checked', 'true');

  const repoFilter = page.getByPlaceholder('Filter to a repository or topic…');
  await repoFilter.fill('agent-memory');
  await expect(page.locator('#graph-count')).toContainText('2 of 3 entities · 0 relations');
  const download = page.waitForEvent('download');
  await page.getByRole('button', { name: 'Export PNG or JSON' }).click();
  await page.getByRole('button', { name: 'JSON data' }).click();
  const json = JSON.parse(await fs.readFile(await (await download).path(), 'utf8'));
  expect(json.nodes.map(node => node.id).sort()).toEqual(['engraphis', 'unlinked']);
  expect(json.links).toEqual([]);

  await page.getByRole('button', { name: 'Reload data' }).click();
  await expect(repoFilter).toHaveValue('agent-memory');
  await expect(page.locator('#graph-count')).toContainText('2 of 3 entities · 0 relations');
  await repoFilter.fill('');
  await expect(page.locator('#graph-count')).toContainText('3 entities · 1 relations');

  await page.getByRole('tab', { name: 'Analyse' }).click();
  await expect(page.getByRole('heading', { name: 'Scope' })).toBeVisible();
  await page.getByLabel('Highlight bridges').check();
  await expect(page.locator('#graph-bridge-count')).toHaveText('1 bridge edge');
  await page.getByRole('tab', { name: 'Time' }).click();
  const asOf = page.getByLabel('As of date');
  await expect(asOf).toBeVisible();
  const temporalRequest = page.waitForRequest(request => {
    const url = new URL(request.url());
    return url.pathname === '/api/graph/scene' && url.searchParams.has('as_of')
      && url.searchParams.get('include_history') === 'true';
  });
  await asOf.fill('2021-01-01');
  await temporalRequest;
  await expect(page.locator('#graph-count')).toContainText('1 relations');
  await page.getByLabel('Show superseded ghosts').uncheck();
  await expect(page.locator('#graph-count')).toContainText('0 relations');

  await page.reload();
  await page.locator('.nav-item[data-view="relations"]').click();
  await expect(page.getByRole('button', { name: 'Galaxy', exact: true })).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByRole('button', { name: 'Compact' })).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByRole('button', { name: 'Type' })).toHaveAttribute('aria-pressed', 'true');
  await expect(page.locator('#graph-flow-speed')).toHaveValue('45');
  await expect(page.getByRole('switch', { name: 'Relation flow' })).toHaveAttribute('aria-checked', 'false');
  await expect(page.getByRole('switch', { name: 'Freeze simulation' })).toHaveAttribute('aria-checked', 'false');
});

test('graph node connections expose linked memory evidence without leaving the graph', async ({ page }) => {
  await mockApi(page);
  await page.goto('/');
  await page.locator('.nav-item[data-view="relations"]').click();
  await page.getByRole('tab', { name: 'Analyse' }).click();
  await expect(page.locator('#graph-top button')).toHaveCount(3);

  const firstGraphFact = page.locator('#graph-top button').first();
  await firstGraphFact.click();
  await expect(page.locator('#graph-connections-dialog')).toBeVisible();
  await expect(page.locator('#graph-connections-list')).toContainText('Engraphis');

  const evidenceRequest = page.waitForRequest(request =>
    new URL(request.url()).pathname === '/api/graph/entities/engraphis/memories'
  );
  await page.locator('#graph-connections-list').getByRole('button', { name: 'Memories' }).click();
  await evidenceRequest;
  await expect(page.locator('#graph-connection-memory-list')).toContainText('Database choice');
  await expect(page.locator('#graph-connections-dialog')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Open in Library' })).toBeVisible();
});

test('historical connections request ghost-edge evidence for live and ghost endpoints', async ({ page }) => {
  await mockApi(page, {
    graphScene: {
      nodes: [
        {
          id: 'live-node', label: 'Origin Node', member_ids: ['live-member'],
          gravity_mass: 4, visual_radius: 8, community_id: 'graph', x: -20, y: 0,
        },
        {
          id: 'canon:ghost:ghost', label: 'Archived Node', ghost: true,
          member_ids: ['archived-member'], gravity_mass: 0, visual_radius: 0,
          community_id: 'history', x: 20, y: 0,
        },
        {
          id: 'historical-live', label: 'Historical Live Node',
          member_ids: ['historical-live-member'], gravity_mass: 2, visual_radius: 6,
          community_id: 'graph', x: 0, y: 20,
        },
      ],
      edges: [
        {
          id: 'historical-ghost-edge', source: 'live-node', target: 'canon:ghost:ghost',
          relation: 'used', ghost: true, rest_length: 18, spring_strength: 0,
        },
        {
          id: 'historical-live-edge', source: 'live-node', target: 'historical-live',
          relation: 'superseded', ghost: true, rest_length: 18, spring_strength: 0,
        },
      ],
      communities: [{ id: 'graph', mass: 4 }],
      community_bridges: [],
      meta: { algorithm_version: 'galaxy-v6', layout_seed: 7 },
    },
  });
  await page.goto('/');
  await page.locator('.nav-item[data-view="relations"]').click();
  await page.getByRole('tab', { name: 'Analyse' }).click();
  await page.locator('#graph-top button').filter({ hasText: 'Origin Node' }).click();
  await expect(page.locator('#graph-connections-list')).toContainText('Archived Node');
  await expect(page.locator('#graph-connections-list')).toContainText('Historical Live Node');

  const evidenceRequest = page.waitForRequest(request => {
    const url = new URL(request.url());
    return url.pathname.endsWith('/memories')
      && url.searchParams.get('member_id') === 'archived-member'
      && url.searchParams.get('include_history') === 'true';
  });
  await page.locator('.graph-connection-row').filter({ hasText: 'Archived Node' })
    .getByRole('button', { name: 'Memories' }).click();
  await evidenceRequest;
  await expect(page.locator('#graph-connection-memory-list')).toContainText('Database choice');

  const liveEndpointRequest = page.waitForRequest(request => {
    const url = new URL(request.url());
    return url.pathname.includes('/graph/entities/historical-live/memories')
      && url.searchParams.get('include_history') === 'true'
      && !url.searchParams.has('member_id');
  });
  await page.locator('.graph-connection-row').filter({ hasText: 'Historical Live Node' })
    .getByRole('button', { name: 'Memories' }).click();
  await liveEndpointRequest;
});

test('changing the time anchor replaces a pending graph request', async ({ page }) => {
  let releaseInitial;
  const initialRelease = new Promise(resolve => { releaseInitial = resolve; });
  let initialStarted;
  const waitForInitial = new Promise(resolve => { initialStarted = resolve; });
  await mockApi(page, {
    deferGraphRequest: async url => {
      if (!url.searchParams.has('as_of') && !url.searchParams.has('connected_only')) {
        initialStarted();
        await initialRelease;
      }
    },
  });
  await page.goto('/');
  await page.locator('.nav-item[data-view="relations"]').click();
  await waitForInitial;

  await page.getByRole('tab', { name: 'Time' }).click();
  const anchored = page.waitForRequest(request => {
    const url = new URL(request.url());
    return url.pathname === '/api/graph/scene'
      && url.searchParams.has('as_of')
      && !url.searchParams.has('connected_only');
  }, { timeout: 5_000 });
  await page.getByLabel('As of date').fill('2021-01-01');
  await anchored;
  releaseInitial();
  await expect(page.locator('#graph-count')).toContainText('1 relations');
});

test('Reload data replaces an identical pending graph request once', async ({ page }) => {
  let releaseFirst;
  const firstGate = new Promise(resolve => { releaseFirst = resolve; });
  let releaseRetry;
  const retryGate = new Promise(resolve => { releaseRetry = resolve; });
  let firstStarted;
  const waitForFirst = new Promise(resolve => { firstStarted = resolve; });
  let retryStarted;
  const waitForRetry = new Promise(resolve => { retryStarted = resolve; });
  let graphAttempts = 0;
  await mockApi(page, {
    deferGraphRequest: async url => {
      if (url.searchParams.has('connected_only')) return;
      graphAttempts += 1;
      if (graphAttempts === 1) {
        firstStarted();
        await firstGate;
      } else if (graphAttempts === 2) {
        retryStarted();
        await retryGate;
      }
    },
  });
  await page.goto('/');
  await page.locator('.nav-item[data-view="relations"]').click();
  await waitForFirst;

  await page.getByRole('button', { name: 'Reload data' }).click();
  await waitForRetry;
  // A second click while the forced retry is pending must not churn another identical request.
  await page.getByRole('button', { name: 'Reload data' }).click();
  await expect.poll(() => graphAttempts).toBe(2);

  releaseFirst();
  releaseRetry();
  await expect(page.locator('#graph-count')).toContainText('3 entities · 1 relations');
});

test('a custom graph view restores every saved control and server filter', async ({ page }) => {
  await page.route('**/', async route => {
    const response = await route.fetch();
    const html = await response.text();
    await route.fulfill({
      response,
      body: html.replace(
        'data-graph-saved-view="operations" aria-pressed="false">Operations',
        'data-graph-saved-view="custom" aria-pressed="false">Saved custom',
      ),
    });
  });
  await page.addInitScript(() => {
    localStorage.setItem('engraphis-ledger-graph-custom-view-v1', JSON.stringify({
      preset: 'radial', style: 'galaxy', color: 'type', palette: 'ocean',
      flow: false, labels: true, frozen: true,
      tuning: { repel: 80, link: 26, gravity: 12, size: 4, font: 13, linkw: 0.75, labelDensity: 55, flowSpeed: 67 },
      minDegree: 1, depth: 1, showUnlinked: true,
      layers: { temporal: false, entity: true, causal: false, semantic: true, code: true },
      includeCode: true, bridges: true, collapse: true,
      asOf: '2021-01-01', ghosts: false, size: 'betweenness', repoFilter: 'agent-memory',
    }));
  });
  await mockApi(page);
  await page.goto('/');
  await page.locator('.nav-item[data-view="relations"]').click();

  const restored = page.waitForRequest(request => {
    const url = new URL(request.url());
    return url.pathname === '/api/graph/scene' && url.searchParams.has('as_of')
      && url.searchParams.get('include_code') === 'true';
  });
  await page.getByRole('button', { name: 'Saved custom' }).click();
  await restored;

  await expect(page.getByRole('button', { name: 'Galaxy', exact: true })).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByRole('button', { name: 'Radial' })).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByRole('button', { name: 'Type' })).toHaveAttribute('aria-pressed', 'true');
  await expect(page.locator('#graph-flow-speed')).toHaveValue('67');
  await expect(page.locator('#graph-repel')).toHaveValue('80');
  await expect(page.getByRole('switch', { name: 'Relation flow' })).toHaveAttribute('aria-checked', 'false');
  await expect(page.getByRole('switch', { name: 'Entity labels' })).toHaveAttribute('aria-checked', 'true');
  await expect(page.getByRole('switch', { name: 'Freeze simulation' })).toHaveAttribute('aria-checked', 'false');
  await expect(page.getByLabel('Size by')).toHaveValue('betweenness');
  await expect(page.getByLabel('Highlight bridges')).toBeChecked();
  await expect(page.getByLabel('Auto-collapse clusters')).toBeChecked();
  await expect(page.getByLabel('As of date')).toHaveValue('2021-01-01');
  await expect(page.getByLabel('Show superseded ghosts')).not.toBeChecked();
  await expect(page.getByPlaceholder('Filter to a repository or topic…')).toHaveValue('agent-memory');
});

test('a saved code view reloads when only its repository changes', async ({ page }) => {
  await page.route('**/', async route => {
    const response = await route.fetch();
    const html = await response.text();
    await route.fulfill({
      response,
      body: html.replace(
        'data-graph-saved-view="operations" aria-pressed="false">Operations',
        'data-graph-saved-view="custom" aria-pressed="false">Saved custom',
      ),
    });
  });
  await page.addInitScript(() => {
    localStorage.setItem('engraphis-ledger-graph-preferences-v1', JSON.stringify({
      includeCode: true,
      repoFilter: 'repo-before',
    }));
    localStorage.setItem('engraphis-ledger-graph-custom-view-v1', JSON.stringify({
      preset: 'radial', style: 'galaxy', color: 'community', palette: 'theme',
      flow: true, labels: false, tuning: {}, minDegree: 1, depth: 2,
      showUnlinked: false,
      layers: { temporal: true, entity: true, causal: true, semantic: true, code: true },
      includeCode: true, asOf: '', ghosts: true, size: 'degree', bridges: false,
      collapse: false, repoFilter: 'repo-after',
    }));
  });
  await mockApi(page);
  await page.goto('/');
  const before = page.waitForRequest(request => {
    const url = new URL(request.url());
    return url.pathname === '/api/graph/scene'
      && url.searchParams.get('include_code') === 'true'
      && url.searchParams.get('repo') === 'repo-before';
  });
  await page.locator('.nav-item[data-view="relations"]').click();
  await before;

  const after = page.waitForRequest(request => {
    const url = new URL(request.url());
    return url.pathname === '/api/graph/scene'
      && url.searchParams.get('include_code') === 'true'
      && url.searchParams.get('repo') === 'repo-after';
  });
  await page.getByRole('button', { name: 'Saved custom' }).click();
  await after;
});

test('themes persist and both visible interface selectors round-trip', async ({ page }) => {
  const errors = browserErrors(page);
  const isAxeStyleProbe = message => (
    message.startsWith('Applying inline style violates')
    && [
      'sha256-47DEQpj8HBSa+/TImW+5JCeuQeRkm5NMpJWZG3hSuFU=',
      'sha256-jtRRMYY2VbEXuBKbHvqM1/fPT8vazFrEooXdvwUK4y8=',
      'sha256-9xjtvxMT1ApHlgn9ohbh2FNfvK5Tqtzy94BjfXBeMSY=',
    ].some(hash => message.includes(hash))
  );
  const analyzeUnderStrictCsp = async builder => {
    const start = errors.length;
    const result = await builder.analyze();
    // axe-core probes colour/visibility with temporary inline style nodes. The product CSP
    // correctly blocks those probes and Chromium reports them as console errors. Playwright
    // delivers console events emitted by the completed analysis before its promise resolves,
    // so no fixed delay is needed here; real application errors remain in the same interval.
    const analyzerErrors = errors.splice(start);
    expect(analyzerErrors.filter(message => !isAxeStyleProbe(message))).toEqual([]);
    return result;
  };
  await mockApi(page);
  await page.goto('/');
  await page.getByRole('button', { name: 'Manage' }).click();
  await page.getByRole('tab', { name: 'Settings' }).click();

  const theme = page.locator('#theme-select');
  await theme.selectOption('matrix');
  await expect(page.locator('body')).toHaveAttribute('data-theme', 'matrix');
  await page.reload();
  await page.getByRole('tab', { name: 'Settings' }).click();
  await expect(theme).toHaveValue('matrix');

  await page.keyboard.press('Tab');
  const focused = page.locator(':focus');
  expect(await focused.evaluate(element => getComputedStyle(element).outlineStyle))
    .not.toBe('none');

  for (const value of ['slate', 'midnight', 'paper', 'matrix']) {
    await theme.selectOption(value);
    const accessibility = await analyzeUnderStrictCsp(new AxeBuilder({ page }));
    expect(accessibility.violations, `${value} theme accessibility`).toEqual([]);
  }

  await page.getByRole('combobox', { name: 'Interface' }).selectOption('classic');
  await expect(page).toHaveURL(/\/classic$/);
  await page.getByRole('button', { name: 'Settings', exact: true }).click();
  await expect(page.getByRole('combobox', { name: 'Dashboard interface' }))
    .toHaveValue('classic');
  await expect(page.getByRole('combobox', { name: 'theme select' }))
    .toHaveValue('matrix');
  const classicAccessibility = await analyzeUnderStrictCsp(new AxeBuilder({ page })
    .include('#view-settings')
    .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa']));
  expect(classicAccessibility.violations).toEqual([]);
  await page.getByRole('combobox', { name: 'Dashboard interface' }).selectOption('primary');
  await expect(page).toHaveURL(/\/$/);
  expect(errors.filter(message => !isAxeStyleProbe(message))).toEqual([]);
});

test('Ledger exposes local LLM setup and extraction controls', async ({ page }) => {
  await mockApi(page);
  await page.goto('/');
  await page.getByRole('button', { name: 'Manage' }).click();
  await page.getByRole('tab', { name: 'Settings' }).click();

  await expect(page.getByRole('heading', { name: 'Connect an LLM' })).toBeVisible();
  await expect(page.getByText('not configured', { exact: true })).toBeVisible();
  const provider = page.getByRole('combobox', { name: 'Provider' });
  const model = page.getByRole('combobox', { name: 'Model' });
  await expect(provider).toHaveValue('openai');
  await expect(model).toHaveValue('gpt-4o-mini');
  await expect(page.getByLabel('Local .env setup')).toHaveValue(/ENGRAPHIS_LLM_PROVIDER=openai/);
  await expect(page.getByRole('button', { name: 'Turn on' })).toBeDisabled();
  await expect(page.getByRole('button', { name: 'Turn off' })).toBeDisabled();

  await provider.selectOption('anthropic');
  await expect(model).toHaveValue('claude-3-5-sonnet-20241022');
  await expect(page.getByLabel('Local .env setup')).toHaveValue(/ENGRAPHIS_LLM_PROVIDER=anthropic/);
  await page.getByRole('button', { name: 'Test connection' }).click();
  await expect(page.locator('#llm-test-result')).toContainText('No API key configured');
});

test('Ledger applies the configured LLM extraction toggle', async ({ page }) => {
  const requests = await mockApi(page, { llmStatus: {
    configured: true,
    key_set: true,
    provider: 'openai',
    model: 'gpt-4o-mini',
    extractor: 'none',
    extractor_enabled: false,
    retention_supervisor: 'llm',
    default_models: { openai: 'gpt-4o-mini' },
  } });
  await page.goto('/');
  await page.getByRole('button', { name: 'Manage' }).click();
  await page.getByRole('tab', { name: 'Settings' }).click();

  const turnOn = page.getByRole('button', { name: 'Turn on' });
  await expect(turnOn).toBeEnabled();
  await expect(page.getByText(/Retention supervision is ON/)).toBeVisible();
  await expect(page.getByText(/bounded excerpt/)).toBeVisible();
  page.once('dialog', dialog => {
    expect(dialog.message()).toContain('configured LLM provider');
    expect(dialog.message()).toContain('provider must read that text');
    return dialog.accept();
  });
  await turnOn.click();
  await expect(page.getByText('ON', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Turn off' })).toBeEnabled();

  await page.getByRole('button', { name: 'Turn off' }).click();
  await expect(page.getByText('OFF', { exact: true })).toBeVisible();
  await expect(page.getByText(/Retention supervision is ON/)).toBeVisible();
  await expect(turnOn).toBeEnabled();
  expect(requests.details.filter(({ path, method }) => (
    path === '/llm/extractor' && method === 'POST'
  ))).toHaveLength(2);
});

test('Ledger gives active Pro members direct Cloud access and saves hosted policy changes', async ({ page }) => {
  const errors = browserErrors(page);
  const activePro = {
    ...license(),
    plan: 'pro',
    features: ['cloud_sync', 'analytics', 'automation'],
    cloud_access_active: true,
    access_state: 'active',
    plan_source: 'cloud',
    trial: { used: true, active: false, available: false, trial_days: 3 },
  };
  const requests = await mockApi(page, {
    license: activePro,
    automationPolicy: {
      enabled: true,
      cadence_hours: 24,
      dream: true,
      dream_min_new: 25,
      dream_idle_minutes: 15,
      infer: false,
      last_run: Date.now() / 1000,
    },
  });
  await page.goto('/');

  await expect(page.locator('#plan-badge')).toHaveCount(1);
  await expect(page.locator('#sidebar-pro-cta')).toHaveText('Open Engraphis Cloud');
  await expect(page.locator('#sidebar-pro-cta')).toHaveAttribute(
    'href',
    'https://cloud.engraphis.test/account?utm_source=engraphis&utm_medium=product&utm_campaign=pro_conversion&utm_content=sidebar',
  );
  await expect(page.locator('#plan-badge')).toHaveText('PRO');

  await page.getByRole('button', { name: 'Manage' }).click();
  await page.getByRole('tab', { name: 'Settings' }).click();
  const cloudSettings = page.locator('#cloud-account-settings');
  await expect(cloudSettings.getByRole('link', { name: 'Open Engraphis Cloud' })).toHaveAttribute(
    'href',
    'https://cloud.engraphis.test/account?utm_source=engraphis&utm_medium=product&utm_campaign=pro_conversion&utm_content=settings',
  );

  await page.getByRole('tab', { name: 'Automation' }).click();
  await expect(page.getByRole('checkbox', { name: 'Enable hosted maintenance' })).toBeChecked();
  await page.getByRole('spinbutton', { name: 'Run every (hours)' }).fill('12');
  page.once('dialog', dialog => {
    expect(dialog.message()).toContain(
      'Cloud Sync encrypts eligible shared-workspace changes end-to-end',
    );
    expect(dialog.message()).toContain('Engraphis Cloud cannot read their contents');
    expect(dialog.message()).toContain('Engraphis will submit a bounded snapshot');
    expect(dialog.message()).toContain(
      'normal and sensitive memory content to Cloud for managed compute',
    );
    expect(dialog.message()).not.toContain('Privacy, by design.');
    return dialog.accept();
  });
  await page.getByRole('button', { name: 'Save & send policy to Cloud' }).click();
  await expect.poll(() => requests.automationPolicies.length).toBe(1);
  expect(requests.details.filter(({ path, method }) => (
    path === '/automation' && method === 'POST'
  ))).toHaveLength(1);
  expect(requests.automationPolicies[0]).toEqual({
    enabled: true,
    cadence_hours: 12,
    dream_enabled: true,
    dream_min_new: 25,
    dream_idle_minutes: 15,
    infer: false,
  });
  await expect(page.getByRole('spinbutton', { name: 'Run every (hours)' })).toHaveValue('12');
  expect(errors).toEqual([]);
});

test('billing cadence selects the exact Pro and Team checkout target', async ({ page }) => {
  await mockApi(page);
  await page.goto('/');
  await expect(page.locator('#plan-badge')).toHaveCount(1);
  await expect(page.locator('#sidebar-pro-cta')).toHaveText('Start 3-day Pro trial');
  await expect(page.locator('#sidebar-pro-cta')).toHaveAttribute(
    'href',
    'https://cloud.engraphis.test/account?plan=pro&interval=monthly&trial=pro&utm_source=engraphis&utm_medium=product&utm_campaign=pro_conversion&utm_content=sidebar#billing',
  );
  await page.getByRole('button', { name: 'Manage' }).click();
  await page.getByRole('tab', { name: 'Analytics' }).click();
  await expect(page.locator('#analytics-pro-cta')).toHaveText('Start 3-day Pro trial');
  await expect(page.locator('#analytics-pro-cta')).toHaveAttribute(
    'href',
    'https://cloud.engraphis.test/account?plan=pro&interval=monthly&trial=pro&utm_source=engraphis&utm_medium=product&utm_campaign=pro_conversion&utm_content=analytics#billing',
  );
  await page.getByRole('tab', { name: 'Automation' }).click();
  await expect(page.locator('#automation-pro-cta')).toHaveText('Start 3-day Pro trial');
  await expect(page.locator('#automation-pro-cta')).toHaveAttribute(
    'href',
    'https://cloud.engraphis.test/account?plan=pro&interval=monthly&trial=pro&utm_source=engraphis&utm_medium=product&utm_campaign=pro_conversion&utm_content=automation#billing',
  );
  await page.getByRole('tab', { name: 'Plans & billing' }).click();
  await expect(page.locator('.plan-card.featured .plan-support')).toContainText(
    'Support continued Engraphis development with Pro.',
  );
  await expect(page.locator('.plan-card.featured .plan-benefits')).toContainText(
    'Cloud Sync, Analytics, Auto Consolidation, and Auto Dreaming',
  );

  const pro = page.locator('#plan-cards [data-pro-cta="pro"]');
  const team = page.locator('#plan-cards [data-pro-cta="team"]');
  await expect(pro).toHaveAttribute(
    'href',
    'https://cloud.engraphis.test/account?plan=pro&interval=monthly&trial=pro&utm_source=engraphis&utm_medium=product&utm_campaign=pro_conversion&utm_content=plans#billing',
  );
  await expect(team).toHaveAttribute(
    'href',
    'https://cloud.engraphis.test/account?plan=team&interval=monthly&trial=team&utm_source=engraphis&utm_medium=product&utm_campaign=pro_conversion&utm_content=plans#billing',
  );

  await page.getByRole('combobox', { name: 'Billing' }).selectOption('annual');
  await expect(pro).toHaveAttribute(
    'href',
    'https://cloud.engraphis.test/account?plan=pro&interval=annual&trial=pro&utm_source=engraphis&utm_medium=product&utm_campaign=pro_conversion&utm_content=plans#billing',
  );
  await expect(team).toHaveAttribute(
    'href',
    'https://cloud.engraphis.test/account?plan=team&interval=annual&trial=team&utm_source=engraphis&utm_medium=product&utm_campaign=pro_conversion&utm_content=plans#billing',
  );
});
