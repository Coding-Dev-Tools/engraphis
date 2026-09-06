const { test, expect } = require('@playwright/test');
const AxeBuilder = require('@axe-core/playwright').default;

async function workflowFixture(page, { projectsUnavailable = false, delayProject = '', idsOnly = false, delayProjectList = false, revisionFailures = [], delayRevision = false } = {}) {
  const requests = [];
  const errors = [];
  const records = [
    { id: 'mem_01M1EXAMPLEALPHA', version: 'mv1:alpha', title: '', content: 'Alpha uses Postgres for durable records.', scope: 'repo', workspace_id: 'ws_one', repo_id: 'repo_alpha', repo: 'alpha', mtype: 'semantic', can_revise: true },
    { id: 'mem_01M1EXAMPLEBETA', version: 'mv1:beta', title: 'Beta database', content: 'Beta uses SQLite.', scope: 'repo', workspace_id: 'ws_one', repo_id: 'repo_beta', repo: 'beta', mtype: 'semantic', can_revise: true },
  ];
  if (idsOnly) records.forEach(record => { delete record.repo; });
  const projectName = record => record.repo || { repo_alpha: 'alpha', repo_beta: 'beta' }[record.repo_id];
  const receipts = new Map();
  let release;
  const delayed = new Promise(resolve => { release = resolve; });
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/api/**', async route => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname.replace(/^\/api/, '');
    const body = request.method() === 'POST' ? request.postDataJSON() : null;
    requests.push({ path, query: Object.fromEntries(url.searchParams), body });
    const ok = payload => route.fulfill({ contentType: 'application/json', body: JSON.stringify(payload) });
    if (path === '/bootstrap') return ok({
      version: '1.7.1', workspaces: [{ name: 'work-one', memories: 2 }, { name: 'work-two', memories: 0 }],
      license: { plan: 'local', features: [], known_features: {}, trial: { used: false, trial_days: 3 } },
      embedder: { semantic: true },
    });
    if (path === '/repos') {
      if (delayProjectList) await delayed;
      if (projectsUnavailable) return route.fulfill({ status: 503, contentType: 'application/json', body: '{"detail":"unavailable"}' });
      return ok({ repos: [{ id: 'repo_alpha', name: 'alpha' }, { id: 'repo_beta', name: 'beta' }] });
    }
    if (path === '/stats') return ok({ memories: 2, total_rows: 2, workspaces: 2, sessions: 1, by_type: {} });
    if (path === '/memories') {
      if (delayProject && url.searchParams.get('repo') === delayProject) await delayed;
      const selected = records.filter(record => !record.valid_to && (!url.searchParams.get('repo') || projectName(record) === url.searchParams.get('repo') || record.scope === 'workspace'));
      return ok({ memories: selected, total_count: selected.length, count: selected.length, next_cursor: null }).catch(() => {});
    }
    if (path === '/memory/revise') {
      if (delayRevision) await delayed;
      if (receipts.has(body.operation_id)) return ok(receipts.get(body.operation_id)).catch(() => {});
      const failure = revisionFailures.shift();
      if (failure === 'unavailable') return route.fulfill({ status: 503, json: { detail: 'Temporary interruption' } });
      const old = records.find(record => record.id === body.id);
      if (failure === 'conflict') {
        old.can_revise = false;
        old.valid_to = 100;
        records.push({ ...old, id: 'mem_remote_revision', version: 'mv1:remote', content: 'Changed by another agent.', valid_to: null, can_revise: true });
        return route.fulfill({ status: 409, json: { detail: { code: 'version_conflict', error: 'Refresh before editing.' } } });
      }
      old.can_revise = false;
      old.valid_to = 100;
      const id = 'mem_revision_' + records.length;
      const version = 'mv1:' + id;
      records.push({ ...old, ...body, id, version, valid_to: null, can_revise: true });
      const result = { id, version, receipt: { operation_id: body.operation_id, operation: 'revise', status: 'committed' } };
      receipts.set(body.operation_id, result);
      if (failure === 'drop-committed') return route.abort('failed');
      return ok(result).catch(() => {});
    }
    if (path.endsWith('/history')) {
      const record = records.find(item => item.id === path.split('/')[2]);
      const versions = records.filter(item => item.repo_id === record.repo_id);
      return ok({ versions, count: versions.length, total_count: versions.length, next_cursor: null });
    }
    if (path.startsWith('/memory/')) return ok({ memory: records.find(record => record.id === path.slice('/memory/'.length)), chain: [] });
    if (path === '/remember') {
      const id = 'mem_created_' + records.length;
      records.push({ ...body, id, version: 'mv1:' + id, workspace_id: 'ws_one', repo_id: body.repo ? 'repo_' + body.repo : null, content: body.content, can_revise: true });
      return ok({ id });
    }
    if (path === '/answer') return ok({ grounded: true, answer: 'Project answer.', support: 0.9, citations: [] });
    if (path === '/recall') return ok({ memories: records.filter(record => record.repo === url.searchParams.get('repo')) });
    if (path === '/proactive') return ok({ memories: [] });
    if (path === '/review-inbox') return ok({ items: [], count: 0, has_more: false, truncated: false, count_semantics: 'returned_sample' });
    if (path === '/audit') return ok({ entries: [] });
    if (path === '/managed-processing') return ok({ enabled: false, notice: 'Managed processing is off.' });
    return ok({});
  });
  await page.goto('/?workspace=work-one&view=library');
  if (!delayProjectList) await expect(page.locator('#connection-status')).toContainText('Local engine connected');
  await expect(page.locator('#project-select')).toBeEnabled();
  return { requests, errors, release, records };
}

test('memory navigation, readable titles, and project scope agree across Library and Ask', async ({ page }) => {
  const { requests, errors } = await workflowFixture(page);
  await expect(page.locator('.primary-nav [data-view]')).toHaveText([
    'Homesetup, reviews and activity', 'Askgrounded answers', 'Librarymemories and imports', 'Connectionsset up your coding agent',
  ], { useInnerText: false });
  await expect(page.locator('.manage-nav [data-view="relations"]')).toBeVisible();
  await expect(page.locator('#library-list')).toContainText('Alpha uses Postgres for durable records.');
  await expect(page.locator('#library-list h2').first()).toHaveText('Alpha uses Postgres for durable records.');
  await expect(page.locator('#library-list .memory-ownership')).toHaveText(['work-one / alpha', 'work-one / beta']);
  await page.locator('#project-select').selectOption('alpha');
  await expect(page.locator('#library-count')).toHaveText('1 memory');
  await expect(page.locator('#library-list')).not.toContainText('Beta uses SQLite.');
  await expect(page.locator('[data-view-panel="library"] [data-memory-context]')).toHaveText('work-one / alpha');
  await page.locator('[data-view="ask"]').click();
  await page.locator('#ask-input').fill('Which database?');
  await page.locator('#ask-form').getByRole('button', { name: 'Grounded answer' }).click();
  await expect(page.locator('#answer-panel')).toContainText('Project answer.');
  expect(requests.find(request => request.path === '/answer').body.repo).toBe('alpha');
  expect(requests.find(request => request.path === '/recall').query.repo).toBe('alpha');
  await page.locator('#project-select').selectOption('');
  await expect(page.locator('#answer-panel')).not.toContainText('Project answer.');
  await page.locator('[data-view="library"]').click();
  await expect(page.locator('#library-count')).toHaveText('2 memories');
  expect(errors).toEqual([]);
});

test('All projects resolves actual ownership after discovery and preserves inspector IDs', async ({ page }) => {
  const { release, errors } = await workflowFixture(page, { idsOnly: true, delayProjectList: true });
  await expect(page.locator('#library-list .memory-ownership')).toHaveText([
    'work-one / project ownership unavailable', 'work-one / project ownership unavailable',
  ]);
  release();
  await expect(page.locator('#library-list .memory-ownership')).toHaveText(['work-one / alpha', 'work-one / beta']);
  await page.locator('#library-list [role="option"]').first().click();
  await expect(page.locator('#memory-detail .memory-ownership')).toHaveText('work-one / alpha');
  await expect(page.locator('#memory-detail .definition-list')).toContainText('repo_alpha');
  await expect(page.locator('#memory-detail .definition-list')).toContainText('ws_one');
  expect(errors).toEqual([]);
});

test('unknown project ownership never adopts the selected project and workspace-wide records stay explicit', async ({ page }) => {
  const { records, errors } = await workflowFixture(page, { idsOnly: true, projectsUnavailable: true });
  records.push({ id: 'mem_workspace', title: 'Shared policy', content: 'All projects retain history.', scope: 'workspace', workspace_id: 'ws_one', mtype: 'semantic' });
  await page.locator('#project-new summary').click();
  await page.locator('#project-name').fill('alpha');
  await page.locator('#project-apply').click();
  await expect(page.locator('#library-list .memory-ownership')).toHaveText([
    'work-one / project ownership unavailable', 'work-one / workspace-wide',
  ]);
  await page.locator('#project-select').selectOption('');
  await expect(page.locator('#library-list .memory-ownership')).toHaveText([
    'work-one / project ownership unavailable', 'work-one / project ownership unavailable', 'work-one / workspace-wide',
  ]);
  expect(errors).toEqual([]);
});

test('Library keeps task navigation before appearance controls at wide and narrow widths', async ({ page }, testInfo) => {
  const { errors } = await workflowFixture(page);
  await expect(page.locator('.manage-nav [data-view] > span')).toHaveText(['Explore', 'Activity', 'Settings']);
  await expect(page.locator('#library-list')).toContainText('Alpha uses Postgres');
  const layout = [];
  for (const [name, width, height] of [['wide', 1280, 900], ['narrow', 390, 844]]) {
    await page.setViewportSize({ width, height });
    await page.evaluate(() => window.scrollTo(0, 0));
    const metrics = await page.evaluate(() => {
      const bounds = selector => {
        const rect = document.querySelector(selector).getBoundingClientRect();
        return { top: rect.top, bottom: rect.bottom };
      };
      return { primary: bounds('.primary-nav'), options: bounds('#sidebar-options'), search: bounds('#library-filter'), library: bounds('#library-title'), optionsOpen: document.querySelector('#sidebar-options').open, viewport: innerHeight, overflow: document.documentElement.scrollWidth > innerWidth };
    });
    layout.push({ name, width, height, ...metrics });
    expect(metrics.options.top).toBeGreaterThanOrEqual(metrics.primary.bottom);
    expect(metrics.optionsOpen).toBe(name === 'wide');
    expect(metrics.overflow).toBe(false);
    expect(metrics.search.bottom).toBeLessThan(metrics.viewport);
    const screenshot = testInfo.outputPath('library-' + name + '.png');
    await page.screenshot({ path: screenshot });
    await testInfo.attach('Library ' + name, { path: screenshot, contentType: 'image/png' });
  }
  await page.locator('#sidebar-options > summary').focus();
  await page.keyboard.press('Enter');
  await expect(page.locator('#sidebar-theme-select')).toBeVisible();
  await page.locator('#sidebar-options > summary').focus();
  await page.keyboard.press('Space');
  await expect(page.locator('#sidebar-theme-select')).not.toBeVisible();
  await testInfo.attach('navigation-layout', { body: JSON.stringify(layout, null, 2), contentType: 'application/json' });
  expect(errors).toEqual([]);
});

test('projects persist per workspace and late results cannot replace the current project', async ({ page }) => {
  const { errors, release } = await workflowFixture(page, { delayProject: 'alpha' });
  await page.locator('#project-select').selectOption('alpha');
  await page.locator('#project-select').selectOption('beta');
  await expect(page.locator('#library-list')).toContainText('Beta uses SQLite.');
  release();
  await expect(page.locator('#library-list')).not.toContainText('Alpha uses Postgres');
  await page.locator('#workspace-select').selectOption('work-two');
  await expect(page.locator('#project-select')).toHaveValue('');
  await page.locator('#workspace-select').selectOption('work-one');
  await expect(page.locator('#project-select')).toHaveValue('beta');
  await page.reload();
  await expect(page.locator('#project-select')).toHaveValue('beta');
  await expect(page.locator('#library-list')).toContainText('Beta uses SQLite.');
  expect(errors).toEqual([]);
});

test('new memories explicitly select project or workspace while existing ownership stays fixed', async ({ page }) => {
  const { requests } = await workflowFixture(page);
  await page.locator('#project-select').selectOption('alpha');
  await page.locator('#new-memory-button').click();
  await expect(page.locator('#editor-memory-scope')).toHaveValue('repo');
  await expect(page.locator('#editor-scope-note')).toContainText('work-one');
  await page.locator('#editor-memory-content').fill('Alpha preserves write history.');
  await page.locator('#memory-editor button[type="submit"]').click();
  await expect(page.locator('#memory-detail')).toContainText('Alpha preserves write history.');
  expect(requests.find(request => request.path === '/remember').body).toMatchObject({ workspace: 'work-one', scope: 'repo', repo: 'alpha' });
  await page.locator('#memory-detail').getByRole('button', { name: 'Edit', exact: true }).click();
  await expect(page.locator('#editor-scope-control')).toBeHidden();
  await expect(page.locator('#editor-scope-note')).toContainText('preserves the existing repo scope');
  await page.locator('#editor-close').click();
  await page.locator('#new-memory-button').click();
  await page.locator('#editor-memory-scope').selectOption('workspace');
  await page.locator('#editor-memory-content').fill('All projects require source review.');
  await page.locator('#memory-editor button[type="submit"]').click();
  await expect.poll(() => requests.filter(request => request.path === '/remember').length).toBe(2);
  const shared = requests.filter(request => request.path === '/remember')[1].body;
  expect(shared.scope).toBe('workspace');
  expect(shared).not.toHaveProperty('repo');
});

test('Connections stores only user-confirmed progress for the selected context', async ({ page }) => {
  await workflowFixture(page);
  await page.locator('#project-select').selectOption('alpha');
  await page.locator('[data-view="connections"]').click();
  await expect(page.locator('#connection-progress')).toContainText('0 of 3 steps confirmed by you');
  await expect(page.locator('#connection-progress')).toContainText('not verified by this dashboard');
  await page.locator('#connection-host').selectOption('claude');
  await expect(page.locator('#connection-command')).toHaveText('claude mcp add engraphis -- engraphis-mcp');
  await page.locator('#connection-configured').check();
  await page.locator('#connection-recalled').check();
  await page.reload();
  await expect(page.locator('#connection-progress')).toContainText('2 of 3 steps confirmed by you');
  await expect(page.locator('#connection-host')).toHaveValue('claude');
  await page.locator('#project-select').selectOption('beta');
  await expect(page.locator('#connection-progress')).toContainText('0 of 3 steps confirmed by you');
  await page.locator('#connection-add-memory').click();
  await expect(page.locator('#editor-memory-title')).toBeFocused();
  await expect(page.locator('#editor-memory-scope')).toHaveValue('repo');
});

test('unavailable project discovery preserves context and permits an explicit new project', async ({ page }) => {
  await workflowFixture(page, { projectsUnavailable: true });
  await expect(page.locator('#project-load-status')).toContainText('Project list unavailable');
  await page.locator('#project-new summary').click();
  await page.locator('#project-name').fill('new-project');
  await page.locator('#project-apply').click();
  await expect(page.locator('#project-select')).toHaveValue('new-project');
  await page.reload();
  await expect(page.locator('#project-select')).toHaveValue('new-project');
  await page.locator('#new-memory-button').click();
  await expect(page.locator('#editor-memory-scope')).toHaveValue('repo');
});

test('Connections remains accessible and fits a narrow viewport', async ({ page }) => {
  const { errors } = await workflowFixture(page);
  await page.locator('[data-view="connections"]').click();
  await expect(page.locator('#connections-title')).toBeFocused();
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  const audit = await new AxeBuilder({ page }).analyze();
  expect(audit.violations).toEqual([]);
  await page.locator('#connection-host').focus();
  await page.keyboard.press('ArrowDown');
  await page.keyboard.press('Tab');
  await expect(page.locator('#connection-copy')).toBeFocused();
  expect(errors).toEqual([]);
});

async function openRevision(page) {
  await page.locator('#library-list [data-memory-id="mem_01M1EXAMPLEALPHA"]').click();
  await page.locator('#memory-detail').getByRole('button', { name: 'Edit', exact: true }).click();
  await page.locator('#editor-memory-content').fill('Alpha now uses verified Postgres records.');
}

test('one atomic revision saves content and descriptive fields using the loaded version', async ({ page }) => {
  const { requests, errors } = await workflowFixture(page);
  await openRevision(page);
  await page.locator('#editor-memory-title').fill('Verified storage');
  await page.locator('#editor-memory-type').selectOption('procedural');
  await page.locator('#editor-memory-importance').fill('0.75');
  await page.locator('#memory-editor button[type="submit"]').click();
  await expect(page.locator('#memory-detail')).toContainText('Verified storage');
  const writes = requests.filter(request => request.body);
  expect(writes).toHaveLength(1);
  expect(writes[0]).toMatchObject({ path: '/memory/revise', body: {
    id: 'mem_01M1EXAMPLEALPHA', expected_version: 'mv1:alpha', workspace: 'work-one',
    content: 'Alpha now uses verified Postgres records.', title: 'Verified storage', mtype: 'procedural', importance: 0.75,
  } });
  await page.locator('#memory-detail').getByRole('button', { name: 'Edit', exact: true }).click();
  await page.locator('#editor-memory-title').fill('Second revision');
  await page.locator('#memory-editor button[type="submit"]').click();
  await expect(page.locator('#memory-detail')).toContainText('Second revision');
  const revisions = requests.filter(request => request.path === '/memory/revise');
  expect(revisions[1].body).toMatchObject({ id: 'mem_revision_2', expected_version: 'mv1:mem_revision_2' });
  expect(revisions[1].body.operation_id).not.toBe(revisions[0].body.operation_id);
  expect(errors).toEqual([]);
});

test('an uncertain committed response preserves the draft and retries the exact same operation', async ({ page }) => {
  const { requests, records } = await workflowFixture(page, { revisionFailures: ['drop-committed'] });
  await openRevision(page);
  await page.locator('#memory-editor button[type="submit"]').click();
  await expect(page.locator('#editor-error')).toContainText('Your draft is retained');
  await expect(page.locator('#editor-memory-content')).toHaveValue('Alpha now uses verified Postgres records.');
  await expect(page.locator('#editor-memory-content')).toBeEnabled();
  await page.locator('#memory-editor button[type="submit"]').click();
  await expect(page.locator('#memory-detail')).toContainText('Alpha now uses verified Postgres records.');
  const revisions = requests.filter(request => request.path === '/memory/revise');
  expect(revisions).toHaveLength(2);
  expect(revisions[1].body).toEqual(revisions[0].body);
  expect(records.filter(record => record.id.startsWith('mem_revision_'))).toHaveLength(1);
});

test('changing a failed draft creates a new intent while an unchanged retry keeps its ID', async ({ page }) => {
  const { requests } = await workflowFixture(page, { revisionFailures: ['unavailable', 'unavailable'] });
  await openRevision(page);
  await page.locator('#memory-editor button[type="submit"]').click();
  await expect(page.locator('#editor-error')).toBeVisible();
  await page.locator('#editor-memory-title').fill('Changed intent');
  await page.locator('#memory-editor button[type="submit"]').click();
  await expect(page.locator('#editor-error')).toBeVisible();
  await expect(page.locator('#editor-memory-title')).toHaveValue('Changed intent');
  await page.locator('#memory-editor button[type="submit"]').click();
  await expect(page.locator('#memory-detail')).toContainText('Changed intent');
  const revisions = requests.filter(request => request.path === '/memory/revise');
  expect(revisions).toHaveLength(3);
  expect(revisions[0].body.operation_id).not.toBe(revisions[1].body.operation_id);
  expect(revisions[1].body).toEqual(revisions[2].body);
  expect(revisions.every(request => request.body.expected_version === 'mv1:alpha')).toBe(true);
});

test('conflicts require explicit history refresh and base selection without discarding the draft', async ({ page }) => {
  const { requests } = await workflowFixture(page, { revisionFailures: ['conflict'] });
  await openRevision(page);
  await page.locator('#editor-memory-title').fill('My retained draft');
  await page.locator('#memory-editor button[type="submit"]').click();
  await expect(page.locator('#editor-error')).toContainText('This saved record changed');
  await expect(page.locator('#memory-editor button[type="submit"]')).toBeDisabled();
  await page.locator('#memory-editor').dispatchEvent('submit');
  expect(requests.filter(request => request.path === '/memory/revise')).toHaveLength(1);
  await page.locator('#editor-refresh').click();
  await expect(page.locator('#editor-history')).toContainText('Changed by another agent.');
  await expect(page.locator('#editor-history').getByRole('button', { name: 'Use this version as editing base' })).toHaveCount(1);
  await page.locator('#editor-history').getByRole('button', { name: 'Use this version as editing base' }).click();
  await expect(page.locator('#editor-memory-title')).toHaveValue('My retained draft');
  await expect(page.locator('#editor-memory-content')).toHaveValue('Alpha now uses verified Postgres records.');
  await expect(page.locator('#memory-editor button[type="submit"]')).toBeEnabled();
  await page.locator('#memory-editor button[type="submit"]').click();
  await expect(page.locator('#memory-detail')).toContainText('My retained draft');
  const revisions = requests.filter(request => request.path === '/memory/revise');
  expect(revisions[1].body).toMatchObject({ id: 'mem_remote_revision', expected_version: 'mv1:remote' });
  expect(revisions[1].body.operation_id).not.toBe(revisions[0].body.operation_id);
});

test('a pending save locks its draft and a late result cannot replace another workspace editor', async ({ page }) => {
  const { requests, release } = await workflowFixture(page, { delayRevision: true });
  await openRevision(page);
  await page.locator('#memory-editor button[type="submit"]').click();
  await expect.poll(() => requests.filter(request => request.path === '/memory/revise').length).toBe(1);
  await expect(page.locator('#editor-memory-content')).toBeDisabled();
  await expect(page.locator('#editor-close')).toBeDisabled();
  await page.locator('#workspace-select').selectOption('work-two');
  await page.locator('#new-memory-button').click();
  await page.locator('#editor-memory-content').fill('A separate workspace draft.');
  release();
  await expect(page.locator('#editor-memory-content')).toHaveValue('A separate workspace draft.');
  await expect(page.locator('#editor-memory-content')).toBeEnabled();
  await expect(page.locator('#editor-title')).toHaveText('New memory');
});

test('server-owned revision eligibility guides recovery without changing original ownership', async ({ page }) => {
  const { records, requests } = await workflowFixture(page);
  const source = records[0];
  source.can_revise = false;
  records.push({ ...source, id: 'mem_promoted', version: 'mv1:promoted', can_revise: true, scope: 'workspace', content: 'A promoted fact.' });
  records.push({ ...source, id: 'mem_successor', version: 'mv1:successor', can_revise: true, content: 'The approved project fact.' });
  await page.locator('#library-list [data-memory-id="mem_01M1EXAMPLEALPHA"]').click();
  await page.locator('#memory-detail').getByRole('button', { name: 'Review saved versions', exact: true }).click();
  await expect(page.locator('#memory-editor button[type="submit"]')).toBeDisabled();
  await page.locator('#editor-memory-content').fill('Keep this project-scoped draft.');
  await page.locator('#editor-refresh').click();
  await expect(page.locator('#editor-history [data-history-id]')).toHaveCount(3);
  await expect(page.locator('#editor-history').getByRole('button', { name: 'Use this version as editing base' })).toHaveCount(1);
  await page.locator('#editor-history [data-history-id="mem_successor"]').getByRole('button').click();
  await expect(page.locator('#editor-memory-content')).toHaveValue('Keep this project-scoped draft.');
  await page.locator('#memory-editor button[type="submit"]').click();
  await expect(page.locator('#memory-detail')).toContainText('Keep this project-scoped draft.');
  expect(requests.find(request => request.path === '/memory/revise').body).toMatchObject({ id: 'mem_successor', expected_version: 'mv1:successor' });
});

test('record history pages by lineage and recovers visibly from a stale continuation', async ({ page }) => {
  await workflowFixture(page);
  await page.locator('#project-select').selectOption('alpha');
  const versions = Array.from({ length: 51 }, (_, index) => ({
    id: 'mem_history_' + index, title: 'Saved version ' + index, content: 'Version evidence ' + index,
    valid_from: index, valid_to: index + 1, provenance: { source: 'fixture' },
  }));
  let stale = true;
  const cursors = [];
  const scopes = [];
  await page.route('**/api/memory/*/history?*', async route => {
    const params = new URL(route.request().url()).searchParams;
    const cursor = params.get('cursor');
    cursors.push(cursor);
    scopes.push({ workspace: params.get('workspace'), repo: params.get('repo') });
    if (cursor && stale) {
      stale = false;
      return route.fulfill({ status: 409, json: { detail: { code: 'cursor_stale' } } });
    }
    return route.fulfill({ json: { versions: cursor ? versions.slice(50) : versions.slice(0, 50), total_count: 51, next_cursor: cursor ? null : 'history-continuation' } });
  });
  await page.locator('#library-list [data-memory-id="mem_01M1EXAMPLEALPHA"]').click();
  await expect(page.locator('.record-history [data-history-id]')).toHaveCount(50);
  await page.getByRole('button', { name: 'Load more versions' }).click();
  await expect(page.locator('.record-history [role="status"]')).toContainText('History changed while paging');
  await expect(page.locator('.record-history [data-history-id]')).toHaveCount(50);
  await page.getByRole('button', { name: 'Refresh history' }).click();
  await expect(page.locator('.record-history [role="status"]')).toHaveText('50 of 51 saved versions.');
  await page.getByRole('button', { name: 'Load more versions' }).click();
  await expect(page.locator('.record-history [data-history-id]')).toHaveCount(51);
  await expect(page.locator('.record-history [role="status"]')).toHaveText('51 of 51 saved versions.');
  expect(cursors).toEqual([null, 'history-continuation', null, 'history-continuation']);
  expect(scopes).toEqual(Array(4).fill({ workspace: 'work-one', repo: 'alpha' }));
});

test('a real project memory is scoped, discoverable, revised atomically, and retained after reload', async ({ page }) => {
  const workspace = 'project-workflow-' + Date.now();
  page.on('dialog', dialog => dialog.accept(dialog.type() === 'prompt' ? 'Reviewed the project fixture.' : undefined));
  await page.goto('/?view=manage#token=engraphis-playwright-local-only');
  await expect(page.locator('#connection-status')).toContainText('Local engine connected');
  await page.locator('#create-workspace-toggle').click();
  await page.locator('#new-workspace-name').fill(workspace);
  await page.locator('#create-workspace-form button[type="submit"]').click();
  await expect(page.locator('#workspace-select')).toHaveValue(workspace);
  await page.locator('#project-new summary').click();
  await page.locator('#project-name').fill('project-a');
  await page.locator('#project-apply').click();
  await page.locator('[data-view="library"]').click();
  await page.locator('#new-memory-button').click();
  await expect(page.locator('#editor-memory-scope')).toHaveValue('repo');
  await page.locator('#editor-memory-content').fill('Project A uses Postgres for durable memory.');
  await page.locator('#memory-editor button[type="submit"]').click();
  await expect(page.locator('#memory-detail')).toContainText('Project A uses Postgres for durable memory.');
  const approvalResponse = page.waitForResponse(response => response.url().endsWith('/dashboard/review/approve'));
  await page.locator('#memory-detail').getByRole('button', { name: 'Approve for prompt…' }).click();
  const approved = await (await approvalResponse).json();
  await expect(page.locator('#memory-detail')).toContainText('approved');
  await page.locator('[data-view="ask"]').click();
  await page.locator('#ask-input').fill('Project A uses Postgres for durable memory.');
  await page.locator('#ask-form button[type="submit"]').click();
  await expect(page.locator('#answer-panel')).toContainText('Project A uses Postgres for durable memory.');
  await page.locator('[data-view="library"]').click();
  await page.locator('#library-list [data-memory-id="' + approved.id + '"]').click();
  await page.locator('#memory-detail').getByRole('button', { name: 'Edit', exact: true }).click();
  await page.locator('#editor-memory-title').fill('Project A durable storage');
  await page.locator('#editor-memory-type').selectOption('procedural');
  await page.locator('#editor-memory-content').fill('Project A uses verified Postgres for durable memory.');
  const revisionResponse = page.waitForResponse(response => response.url().endsWith('/api/memory/revise'));
  await page.locator('#memory-editor button[type="submit"]').click();
  const saved = await (await revisionResponse).json();
  expect(saved.receipt).toMatchObject({ operation: 'revise', status: 'committed' });
  expect(saved.version).toMatch(/^mv1:/);
  await expect(page.locator('#memory-detail h2')).toHaveText('Project A durable storage');
  await expect(page.locator('#memory-detail .record-history')).toContainText('Project A uses Postgres for durable memory.');
  await expect(page.locator('#memory-detail .record-history')).toContainText('Project A uses verified Postgres for durable memory.');
  await page.reload();
  await expect(page.locator('#project-select')).toHaveValue('project-a');
  await expect(page.locator('#library-list')).toContainText('Project A uses verified Postgres for durable memory.');
  await page.locator('#project-new summary').click();
  await page.locator('#project-name').fill('project-b');
  await page.locator('#project-apply').click();
  await expect(page.locator('#library-list')).not.toContainText('Project A uses verified Postgres for durable memory.');
  await page.locator('#project-select').selectOption('');
  await expect(page.locator('#library-list')).toContainText('Project A uses verified Postgres for durable memory.');
});
