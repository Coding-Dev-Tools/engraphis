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
  const audit = options.audit || [];
  const receipts = options.receipts || [];
  const workspaceList = options.workspaces || [{ name: workspace, memories: memories.length }];
  const licenseState = options.license || license();
  let automationPolicy = options.automationPolicy || null;
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
    if (path === '/graph') {
      if (typeof options.deferGraphRequest === 'function') {
        await options.deferGraphRequest(requestUrl);
      }
      const asOf = Number(requestUrl.searchParams.get('as_of'));
      const includeUnlinked = requestUrl.searchParams.get('connected_only') !== 'true';
      // Make the historical payload depend on the server's selected-day anchor. A client
      // that filters at midnight would incorrectly discard these later-in-the-day records.
      const validFrom = Number.isFinite(asOf) ? asOf - 1 : 100;
      const validTo = Number.isFinite(asOf) ? asOf - 0.5 : 200;
      return ok({
        nodes: [
          { id: 'postgres', label: 'Postgres', repo: 'data-stack', topic: 'storage', valid_from: validFrom },
          { id: 'engraphis', label: 'Engraphis', repo: 'agent-memory', topic: 'memory', valid_from: validFrom },
        ].concat(includeUnlinked ? [{
          id: 'unlinked', label: 'Unlinked Note', repo: 'agent-memory', topic: 'memory', valid_from: validFrom,
        }] : []),
        edges: [{ from: 'engraphis', to: 'postgres', valid_from: validFrom, valid_to: validTo }],
        layers: [
          { layer: 'temporal', count: 15 }, { layer: 'entity', count: 26 },
          { layer: 'causal', count: 22 }, { layer: 'semantic', count: 21 }, { layer: 'code', count: 0 },
        ],
      });
    }
    if (path === '/graph/entities/engraphis/memories') return ok({
      canonical_id: 'engraphis',
      evidence: [{
        memory_id: memories[0].id,
        title: memories[0].title,
        excerpt: memories[0].content,
        memory_type: memories[0].mtype,
        valid_from: 100,
      }],
      totals: { evidence: 1 },
      truncation: { evidence: false },
    });
    if (path === '/health') return ok({ status: 'ok' });
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
    if (path === '/sync/status') return ok({ available: false, last: null });
    if (path === '/analytics') {
      return ok({ totals: {}, entities: [], series: [] });
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
  const requests = await mockApi(page);
  const response = await page.goto('/');

  expect(response.headers()['content-security-policy']).not.toContain("'unsafe-inline'");
  await expect(page.getByRole('heading', { name: `What changed in ${workspace}` })).toBeVisible();
  await expect(page.locator('#decision-list').getByText('Postgres 16 is the main database.'))
    .toBeVisible();
  await expect(page.locator('#proactive-list').getByText(/<img src=x onerror=/)).toBeVisible();
  expect(await page.evaluate(() => window.__ledgerXss)).toBeUndefined();
  expect(requests).not.toContain('/graph');

  await page.getByRole('button', { name: 'Graph & Relations' }).click();
  await expect(page.locator('#graph-count')).toContainText('2 entities · 1 relations');
  expect(requests).toContain('/graph');

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
  await expect(page.locator('#sidebar-pro-cta')).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);

  const accessibility = await new AxeBuilder({ page }).analyze();
  expect(accessibility.violations).toEqual([]);
  // force-graph attempts a handful of inline <style> elements when it is mounted. The
  // strict production CSP blocks them; the companion graph-engine suite records the
  // SecurityPolicyViolationEvent objects and proves every one is style-src-elem (never
  // an attribute/script escape hatch). External CSS supplies the rendered canvas layout.
  const unexpectedErrors = errors.filter(message => !(
    message.includes('Applying inline style violates')
    && message.includes("style-src 'self'")
  ));
  expect(unexpectedErrors).toEqual([]);
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

  await page.locator('#proactive-list [data-memory-id="mem_database"]').click();
  await expect(page.locator('#memory-detail h2')).toHaveText('Database choice');
  await expect(page.locator('#memory-detail').getByRole('button', { name: 'Edit' })).toBeVisible();
  await expect(page.locator('#memory-detail').getByRole('button', { name: 'Forget' })).toBeVisible();

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

test('Graph & Relations uses the visual explorer controls and applies their state', async ({ page }) => {
  await mockApi(page);
  await page.goto('/');
  const initialGraphRequest = page.waitForRequest(request => {
    const url = new URL(request.url());
    return url.pathname === '/api/graph' && url.searchParams.get('connected_only') === 'true';
  });
  await page.getByRole('button', { name: 'Graph & Relations' }).click();
  await initialGraphRequest;
  await expect(page.locator('#graph-count')).toContainText('2 entities · 1 relations');

  await expect(page.getByRole('tab', { name: 'Explore' })).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByPlaceholder('Find an entity…')).toBeVisible();
  await expect(page.getByPlaceholder('Filter to a repository or topic…')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Cyber' })).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByRole('button', { name: 'Islands' })).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByRole('button', { name: 'Schema drift' })).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByRole('button', { name: 'Operations' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'People' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Code ↔ memory' })).toBeVisible();
  await expect(page.locator('#graph-flow-speed')).toHaveValue('45');
  await expect(page.locator('#graph-layer-temporal-count')).toHaveText('15');

  await expect(page.getByRole('button', { name: 'Show all nodes' })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Show unlinked nodes' })).toHaveAttribute('aria-pressed', 'false');
  const showUnlinkedRequest = page.waitForRequest(request => {
    const url = new URL(request.url());
    return url.pathname === '/api/graph' && !url.searchParams.has('connected_only');
  });
  await page.getByRole('button', { name: 'Show unlinked nodes' }).click();
  await showUnlinkedRequest;
  await expect(page.getByRole('button', { name: 'Hide unlinked nodes' })).toHaveAttribute('aria-pressed', 'true');
  await expect(page.locator('#graph-count')).toContainText('3 entities · 1 relations');
  const hideUnlinkedRequest = page.waitForRequest(request => {
    const url = new URL(request.url());
    return url.pathname === '/api/graph' && url.searchParams.get('connected_only') === 'true';
  });
  await page.getByRole('button', { name: 'Hide unlinked nodes' }).click();
  await hideUnlinkedRequest;
  await expect(page.getByRole('button', { name: 'Show unlinked nodes' })).toHaveAttribute('aria-pressed', 'false');
  await expect(page.locator('#graph-count')).toContainText('2 entities · 1 relations');

  const codeRequest = page.waitForRequest(request => {
    const url = new URL(request.url());
    return url.pathname === '/api/graph' && url.searchParams.get('include_code') === 'true';
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

  await page.getByRole('button', { name: 'Galaxy' }).click();
  await expect(page.locator('#graph-canvas')).toHaveAttribute('data-graph-style', 'galaxy');
  await page.getByRole('button', { name: 'Compact' }).click();
  await expect(page.locator('#graph-mode')).toContainText('Compact');
  await page.getByRole('button', { name: 'Type' }).click();
  await expect(page.getByRole('button', { name: 'Type' })).toHaveAttribute('aria-pressed', 'true');

  const flow = page.getByRole('switch', { name: 'Relation flow' });
  await flow.click();
  await expect(flow).toHaveAttribute('aria-checked', 'false');
  const freeze = page.getByRole('switch', { name: 'Freeze simulation' });
  await freeze.click();
  await expect(freeze).toHaveAttribute('aria-checked', 'true');

  const repoFilter = page.getByPlaceholder('Filter to a repository or topic…');
  await repoFilter.fill('agent-memory');
  await expect(page.locator('#graph-count')).toContainText('1 of 2 entities · 0 relations');
  const download = page.waitForEvent('download');
  await page.getByRole('button', { name: 'Export PNG or JSON' }).click();
  await page.getByRole('button', { name: 'JSON data' }).click();
  const json = JSON.parse(await fs.readFile(await (await download).path(), 'utf8'));
  expect(json.nodes.map(node => node.id)).toEqual(['engraphis']);
  expect(json.links).toEqual([]);

  await page.getByRole('button', { name: 'Reload data' }).click();
  await expect(repoFilter).toHaveValue('agent-memory');
  await expect(page.locator('#graph-count')).toContainText('1 of 2 entities · 0 relations');
  await repoFilter.fill('');
  await expect(page.locator('#graph-count')).toContainText('2 entities · 1 relations');

  await page.getByRole('tab', { name: 'Analyse' }).click();
  await expect(page.getByRole('heading', { name: 'Scope' })).toBeVisible();
  await page.getByLabel('Highlight bridges').check();
  await expect(page.locator('#graph-bridge-count')).toHaveText('1 bridge edge');
  await page.getByRole('tab', { name: 'Time' }).click();
  const asOf = page.getByLabel('As of date');
  await expect(asOf).toBeVisible();
  const temporalRequest = page.waitForRequest(request => {
    const url = new URL(request.url());
    return url.pathname === '/api/graph' && url.searchParams.has('as_of');
  });
  await asOf.fill('2021-01-01');
  await temporalRequest;
  await expect(page.locator('#graph-count')).toContainText('1 relations');
  await page.getByLabel('Show superseded ghosts').uncheck();
  await expect(page.locator('#graph-count')).toContainText('0 relations');

  await page.reload();
  await page.getByRole('button', { name: 'Graph & Relations' }).click();
  await expect(page.getByRole('button', { name: 'Galaxy' })).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByRole('button', { name: 'Compact' })).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByRole('button', { name: 'Type' })).toHaveAttribute('aria-pressed', 'true');
  await expect(page.locator('#graph-flow-speed')).toHaveValue('45');
  await expect(page.getByRole('switch', { name: 'Relation flow' })).toHaveAttribute('aria-checked', 'false');
  await expect(page.getByRole('switch', { name: 'Freeze simulation' })).toHaveAttribute('aria-checked', 'true');
});

test('graph node connections expose linked memory evidence without leaving the graph', async ({ page }) => {
  await mockApi(page);
  await page.goto('/');
  await page.getByRole('button', { name: 'Graph & Relations' }).click();
  await page.getByRole('tab', { name: 'Analyse' }).click();
  await expect(page.locator('#graph-top button')).toHaveCount(2);

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

test('changing the time anchor replaces a pending graph request', async ({ page }) => {
  let releaseInitial;
  const initialRelease = new Promise(resolve => { releaseInitial = resolve; });
  let initialStarted;
  const waitForInitial = new Promise(resolve => { initialStarted = resolve; });
  await mockApi(page, {
    deferGraphRequest: async url => {
      if (!url.searchParams.has('as_of') && url.searchParams.get('connected_only') === 'true') {
        initialStarted();
        await initialRelease;
      }
    },
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'Graph & Relations' }).click();
  await waitForInitial;

  await page.getByRole('tab', { name: 'Time' }).click();
  const anchored = page.waitForRequest(request => {
    const url = new URL(request.url());
    return url.pathname === '/api/graph'
      && url.searchParams.has('as_of')
      && url.searchParams.get('connected_only') === 'true';
  }, { timeout: 5_000 });
  await page.getByLabel('As of date').fill('2021-01-01');
  await anchored;
  releaseInitial();
  await expect(page.locator('#graph-count')).toContainText('1 relations');
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
  await page.getByRole('button', { name: 'Graph & Relations' }).click();

  const restored = page.waitForRequest(request => {
    const url = new URL(request.url());
    return url.pathname === '/api/graph' && url.searchParams.has('as_of')
      && url.searchParams.get('include_code') === 'true';
  });
  await page.getByRole('button', { name: 'Saved custom' }).click();
  await restored;

  await expect(page.getByRole('button', { name: 'Galaxy' })).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByRole('button', { name: 'Radial' })).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByRole('button', { name: 'Type' })).toHaveAttribute('aria-pressed', 'true');
  await expect(page.locator('#graph-flow-speed')).toHaveValue('67');
  await expect(page.locator('#graph-repel')).toHaveValue('80');
  await expect(page.getByRole('switch', { name: 'Relation flow' })).toHaveAttribute('aria-checked', 'false');
  await expect(page.getByRole('switch', { name: 'Entity labels' })).toHaveAttribute('aria-checked', 'true');
  await expect(page.getByRole('switch', { name: 'Freeze simulation' })).toHaveAttribute('aria-checked', 'true');
  await expect(page.getByLabel('Size by')).toHaveValue('betweenness');
  await expect(page.getByLabel('Highlight bridges')).toBeChecked();
  await expect(page.getByLabel('Auto-collapse clusters')).toBeChecked();
  await expect(page.getByLabel('As of date')).toHaveValue('2021-01-01');
  await expect(page.getByLabel('Show superseded ghosts')).not.toBeChecked();
  await expect(page.getByPlaceholder('Filter to a repository or topic…')).toHaveValue('agent-memory');
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
    await page.waitForTimeout(25);
    // axe-core probes colour/visibility with temporary inline style nodes. The product CSP
    // correctly blocks those probes and Chromium reports them as console errors; isolate only
    // that analyzer-owned interval so real application errors before or after it still fail.
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
  await mockApi(page, { llmStatus: {
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
});

test('Ledger gives active Pro members direct Cloud access and saves hosted policy changes', async ({ page }) => {
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

  await expect(page.locator('#plan-badge')).toHaveText('PRO');
  await expect(page.locator('#plan-badge')).toHaveAttribute(
    'href',
    'https://cloud.engraphis.test/account?utm_source=engraphis&utm_medium=product&utm_campaign=pro_conversion&utm_content=header',
  );
  await expect(page.locator('#sidebar-pro-cta')).toHaveText('Open Engraphis Cloud');
  await expect(page.locator('#sidebar-pro-cta')).toHaveAttribute(
    'href',
    'https://cloud.engraphis.test/account?utm_source=engraphis&utm_medium=product&utm_campaign=pro_conversion&utm_content=sidebar',
  );

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
  expect(requests.automationPolicies[0]).toEqual({
    enabled: true,
    cadence_hours: 12,
    dream_enabled: true,
    dream_min_new: 25,
    dream_idle_minutes: 15,
    infer: false,
  });
  await expect(page.getByRole('spinbutton', { name: 'Run every (hours)' })).toHaveValue('12');
});

test('billing cadence selects the exact Pro and Team checkout target', async ({ page }) => {
  await mockApi(page);
  await page.goto('/');
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
