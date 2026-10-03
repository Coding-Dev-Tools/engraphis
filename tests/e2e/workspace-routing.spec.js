const { test, expect } = require('@playwright/test');
const AxeBuilder = require('@axe-core/playwright').default;

const source = 'default';
const destination = 'client "Acme" <work>';
const alternative = 'research';
const project = 'app "quoted" <project>';

async function mockWorkspaceApi(page, options = {}) {
  const calls = { routing: [], previews: [], moves: [] };
  const mappings = new Map();
  const current = { id: 'mem_current', title: 'Database decision', content: 'Use Postgres.', scope: 'repo', repo_name: project, memory_type: 'semantic' };
  const history = { id: 'mem_history', title: 'Previous database decision', related: true };
  const other = { id: 'mem_other', title: 'Unrelated work', content: 'Keep this in the original workspace.', memory_type: 'semantic' };
  const memories = { [source]: [current, other], [destination]: [], [alternative]: [] };
  await page.addInitScript(() => {
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: {
      writeText: async value => { window.__copiedWorkspaceInstructions = value; },
    } });
  });
  await page.route('**/api/**', async route => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname.replace(/^\/api/, '');
    const body = request.method() === 'POST' ? JSON.parse(request.postData() || '{}') : null;
    const workspace = url.searchParams.get('workspace') || source;
    const ok = payload => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(payload) });
    if (path === '/bootstrap') return ok({
      workspaces: [source, destination, alternative].map(name => ({ name, memories: memories[name].length, visibility: name === source ? 'personal' : 'shared' })),
      license: { plan: 'local', features: [], known_features: {}, trial: {} },
      stats: { memories: 2, workspaces: 3, sessions: 0 }, embedder: { semantic: false },
    });
    if (path === '/stats') return ok({ memories: memories[workspace].length, total_rows: memories[workspace].length, by_type: {} });
    if (path === '/repos') return ok({ repos: [{ id: 'repo_project', name: project }, { id: 'repo_other', name: 'another-project' }] });
    if (path === '/memories') {
      const query = (url.searchParams.get('q') || '').toLowerCase();
      const values = memories[workspace].filter(memory => !query || memory.title.toLowerCase().includes(query));
      return ok({ workspace, memories: values, total_count: values.length });
    }
    if (path === '/workspace-routing') {
      const repo = body ? body.repo : url.searchParams.get('repo');
      if (body) {
        calls.routing.push(body);
        if (body.enabled) mappings.set(repo, body.workspace);
        else mappings.delete(repo);
      } else if (options.deferRouting) await options.deferRouting(repo);
      return ok({ repo, workspace: mappings.get(repo) || null, configured: mappings.has(repo), source: 'project' });
    }
    if (path === '/memories/move-preview') {
      calls.previews.push(body);
      if (options.deferPreview) await options.deferPreview(body);
      return ok({
        source: body.workspace, target: body.target_workspace, requested_ids: body.memory_ids,
        source_visibility: 'personal', target_visibility: 'shared',
        memory_ids: [...body.memory_ids, history.id], count: body.memory_ids.length + 1, related_count: 1,
        memories: [{ id: current.id, title: current.title, related: false }, history],
        sessions: 1, graph_edges: 2, repos: [project],
        blockers: options.blockers || [], can_move: !(options.blockers || []).length,
        preview_token: 'preview-for-' + body.target_workspace,
      });
    }
    if (path === '/memories/move') {
      calls.moves.push(body);
      if (options.moveConflict) return route.fulfill({ status: 409, contentType: 'application/json', body: JSON.stringify({ detail: 'Preview changed.' }) });
      const selected = memories[body.workspace].filter(memory => body.memory_ids.includes(memory.id));
      memories[body.workspace] = memories[body.workspace].filter(memory => !body.memory_ids.includes(memory.id));
      memories[body.target_workspace].push(...selected);
      return ok({ moved: [...body.memory_ids, history.id], count: body.memory_ids.length + 1, workspace: body.target_workspace });
    }
    if (path === '/proactive') return ok({ memories: [] });
    if (path === '/audit') return ok({ audit: [] });
    if (path === '/review-inbox') return ok({ items: [], count: 0, has_more: false });
    return ok({});
  });
  return calls;
}

async function selectForMove(page) {
  await page.goto('/?view=library');
  await page.getByRole('button', { name: 'Select memories', exact: true }).click();
  await page.locator('[data-memory-id="mem_current"]').click();
  await expect(page.locator('#library-selection-status')).toContainText('1 selected');
  await page.getByRole('button', { name: 'Move selected', exact: true }).click();
  await page.getByLabel('Destination workspace', { exact: true }).selectOption(destination);
}

test('workspace instructions quote the selected scope and routing saves only on user action', async ({ page }) => {
  const calls = await mockWorkspaceApi(page);
  await page.goto('/?view=connections');
  await page.getByLabel('Active project', { exact: true }).selectOption(project);
  const instructions = page.locator('#connection-workspace-instructions');
  await expect(instructions).toContainText('omit workspace');
  await expect(instructions).toContainText('repo=' + JSON.stringify(project));
  await expect(page.locator('#connection-routing-status')).toContainText('No project default is saved');
  expect(calls.routing).toEqual([]);
  await expect(page.getByRole('button', { name: 'Copy workspace instructions', exact: true })).toBeDisabled();
  await page.getByRole('button', { name: 'Save project routing', exact: true }).click();
  await expect(page.locator('#connection-routing-status')).toContainText('Default for ' + JSON.stringify(project));
  expect(calls.routing).toEqual([{ workspace: source, repo: project, enabled: true }]);
  await page.getByRole('button', { name: 'Copy workspace instructions', exact: true }).click();
  expect(await page.evaluate(() => window.__copiedWorkspaceInstructions)).toEqual(await instructions.textContent());
  await expect(instructions).not.toContainText('Use Engraphis workspace=');
  await page.getByRole('button', { name: 'Remove project routing', exact: true }).click();
  await expect(page.locator('#connection-routing-status')).toContainText('No project default is saved');
  expect(calls.routing[1]).toEqual({ workspace: source, repo: project, enabled: false });
  await expect(page.getByRole('button', { name: 'Copy workspace instructions', exact: true })).toBeDisabled();
  await page.getByLabel('Active workspace', { exact: true }).selectOption(destination);
  await expect(instructions).toContainText('workspace=' + JSON.stringify(destination));
  await expect(instructions).toContainText('No project is selected');
  await expect(page.locator('#connection-routing-save')).toBeDisabled();
  await expect(page.locator('#connection-routing-remove')).toBeDisabled();
  expect((await new AxeBuilder({ page }).include('[data-view-panel="connections"]').analyze()).violations).toEqual([]);
});

test('selective workspace move previews complete history before applying and preserves unselected records', async ({ page }) => {
  const calls = await mockWorkspaceApi(page);
  await selectForMove(page);
  await expect(page.locator('#memory-move-apply')).toBeDisabled();
  expect(calls.moves).toEqual([]);
  await page.getByRole('button', { name: 'Preview move', exact: true }).click();
  await expect(page.locator('#memory-move-apply')).toBeEnabled();
  await expect(page.locator('#memory-move-preview')).toContainText(destination);
  await expect(page.locator('#memory-move-preview')).toContainText('Access: Personal → Shared');
  await expect(page.locator('#memory-move-preview')).toContainText('Other users with workspace access can read');
  await expect(page.locator('#memory-move-preview')).toContainText('Related memories and history');
  await page.getByText('Review all 2 records', { exact: true }).click();
  await expect(page.locator('#memory-move-preview')).toContainText('Previous database decision');
  expect(calls.previews).toEqual([{ workspace: source, target_workspace: destination, memory_ids: ['mem_current'] }]);
  expect((await new AxeBuilder({ page }).include('#memory-move-dialog').analyze()).violations).toEqual([]);
  await page.getByRole('button', { name: 'Move memories', exact: true }).click();
  await expect(page.locator('#memory-move-dialog')).not.toBeVisible();
  await expect(page.locator('[data-memory-id="mem_current"]')).toHaveCount(0);
  await expect(page.locator('[data-memory-id="mem_other"]')).toBeVisible();
  expect(calls.moves).toEqual([{
    workspace: source, target_workspace: destination, memory_ids: ['mem_current'],
    preview_token: 'preview-for-' + destination, confirmed: true,
  }]);
  await page.getByLabel('Active workspace', { exact: true }).selectOption(destination);
  await expect(page.locator('[data-memory-id="mem_current"]')).toBeVisible();
  await expect(page.locator('[data-memory-id="mem_other"]')).toHaveCount(0);
});

test('changing move destination ignores a late preview and filtering clears the selection', async ({ page }) => {
  let releasePreview;
  const previewGate = new Promise(resolve => { releasePreview = resolve; });
  const calls = await mockWorkspaceApi(page, { deferPreview: body => body.target_workspace === destination ? previewGate : Promise.resolve() });
  try {
    await selectForMove(page);
    await page.getByRole('button', { name: 'Preview move', exact: true }).click();
    await expect.poll(() => calls.previews.length).toBe(1);
    await page.getByLabel('Destination workspace', { exact: true }).selectOption(alternative);
    releasePreview();
    await expect(page.locator('#memory-move-apply')).toBeDisabled();
    await expect(page.locator('#memory-move-preview')).toContainText('Choose a destination');
    await page.getByRole('button', { name: 'Preview move', exact: true }).click();
    await expect(page.locator('#memory-move-apply')).toBeEnabled();
    await expect(page.locator('#memory-move-preview')).toContainText(alternative);
    await page.getByRole('button', { name: 'Cancel', exact: true }).click();
    await page.locator('#library-filter').fill('Unrelated');
    await expect(page.locator('#library-selection-status')).toContainText('0 selected');
    await expect(page.locator('#library-move')).toBeDisabled();
    expect(calls.moves).toEqual([]);
  } finally { releasePreview(); }
});

test('move blockers are visible and prevent submission', async ({ page }) => {
  const calls = await mockWorkspaceApi(page, { blockers: [{ code: 'source_import', message: 'Imported records must stay with their source collection.' }] });
  await selectForMove(page);
  await page.getByRole('button', { name: 'Preview move', exact: true }).click();
  await expect(page.locator('#memory-move-preview')).toContainText('Imported records must stay with their source collection.');
  await expect(page.locator('#memory-move-apply')).toBeDisabled();
  expect(calls.moves).toEqual([]);
});

test('a move conflict clears approval and requires another preview', async ({ page }) => {
  const calls = await mockWorkspaceApi(page, { moveConflict: true });
  await selectForMove(page);
  await page.getByRole('button', { name: 'Preview move', exact: true }).click();
  await expect(page.locator('#memory-move-apply')).toBeEnabled();
  await page.getByRole('button', { name: 'Move memories', exact: true }).click();
  await expect(page.locator('#memory-move-error')).toContainText('Preview the move again');
  await expect(page.locator('#memory-move-apply')).toBeDisabled();
  await expect(page.locator('#memory-move-preview-button')).toBeEnabled();
  expect(calls.moves).toHaveLength(1);
});

test('a late project-routing response cannot replace the current project context', async ({ page }) => {
  let releaseRouting;
  const routingGate = new Promise(resolve => { releaseRouting = resolve; });
  let requested = false;
  await mockWorkspaceApi(page, { deferRouting: repo => {
    if (repo !== project) return Promise.resolve();
    requested = true;
    return routingGate;
  } });
  try {
    await page.goto('/?view=connections');
    await page.getByLabel('Active project', { exact: true }).selectOption(project);
    await expect.poll(() => requested).toBe(true);
    await page.getByLabel('Active project', { exact: true }).selectOption('another-project');
    await expect(page.locator('#connection-routing-status')).toContainText('another-project');
    releaseRouting();
    await expect(page.locator('#connection-workspace-instructions')).toContainText('repo="another-project"');
    await expect(page.locator('#connection-routing-status')).not.toContainText(project);
  } finally { releaseRouting(); }
});

test('saved routing and a selected-memory move work against the isolated local server', async ({ page }, testInfo) => {
  const suffix = Date.now();
  const from = 'routing-source-' + suffix;
  const to = 'routing-target-' + suffix;
  const repo = 'routing-project-' + suffix;
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto('/?view=manage#token=engraphis-playwright-local-only');
  await expect(page.locator('#connection-status')).toContainText('Local engine connected');
  for (const workspace of [from, to]) {
    await page.locator('#create-workspace-toggle').click();
    await page.locator('#new-workspace-name').fill(workspace);
    await page.locator('#create-workspace-form button[type="submit"]').click();
    await expect(page.locator('#workspace-select')).toHaveValue(workspace);
  }
  const headers = { Authorization: 'Bearer engraphis-playwright-local-only' };
  const originalResponse = await page.request.post('/api/remember', { headers, data: {
    workspace: from, repo, title: 'Existing project decision',
    content: 'Cedar deployment requires a staged rollout.',
  } });
  expect(originalResponse.ok()).toBe(true);
  const original = await originalResponse.json();

  await page.locator('#project-new summary').click();
  await page.locator('#project-name').fill(repo);
  await page.locator('#project-apply').click();
  await page.locator('[data-view="connections"]').click();
  await page.getByRole('button', { name: 'Save project routing', exact: true }).click();
  await expect(page.locator('#connection-routing-status')).toContainText('Default for ');
  await page.reload();
  await expect(page.locator('#connection-routing-status')).toContainText(JSON.stringify(to));
  const routedResponse = await page.request.post('/api/remember', { headers, data: {
    repo, title: 'New routed fact', content: 'Orchid specimens bloom in spring.',
  } });
  expect(routedResponse.ok()).toBe(true);
  const routed = await routedResponse.json();
  expect(routed).toMatchObject({ workspace: to, repo, workspace_source: 'project' });

  await page.getByLabel('Active workspace', { exact: true }).selectOption(from);
  await page.locator('[data-view="library"]').click();
  await expect(page.locator('#library-list [data-memory-id="' + original.id + '"]')).toBeVisible();
  await page.getByRole('button', { name: 'Select memories', exact: true }).click();
  await page.locator('#library-list [data-memory-id="' + original.id + '"]').click();
  await page.getByRole('button', { name: 'Move selected', exact: true }).click();
  await page.getByLabel('Destination workspace', { exact: true }).selectOption(to);
  await page.getByRole('button', { name: 'Preview move', exact: true }).click();
  await expect(page.locator('#memory-move-apply')).toBeEnabled();
  await expect(page.locator('#memory-move-preview')).toContainText('Existing project decision');
  await page.screenshot({ path: testInfo.outputPath('workspace-move-preview.png') });
  await page.getByRole('button', { name: 'Move memories', exact: true }).click();
  await expect(page.locator('#memory-move-dialog')).not.toBeVisible();
  await expect(page.locator('#library-list [data-memory-id="' + original.id + '"]')).toHaveCount(0);
  await page.getByLabel('Active workspace', { exact: true }).selectOption(to);
  await page.reload();
  await expect(page.locator('#library-list [data-memory-id="' + original.id + '"]')).toBeVisible();
  await expect(page.locator('#library-list [data-memory-id="' + routed.id + '"]')).toBeVisible();
  expect(errors).toEqual([]);
});
