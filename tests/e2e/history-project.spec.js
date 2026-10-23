const { test, expect } = require('@playwright/test');
const { randomUUID } = require('node:crypto');

const token = 'engraphis-playwright-local-only';

async function historyFixture(page) {
  const workspace = 'history-project-' + randomUUID();
  const errors = [];
  const requests = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('request', request => {
    const url = new URL(request.url());
    if (url.pathname.endsWith('/history')) requests.push(url);
  });
  const post = async (path, data) => {
    const response = await page.request.post('/api/' + path, {
      headers: { Authorization: 'Bearer ' + token }, data: { workspace, ...data },
    });
    expect(response.ok(), await response.text()).toBe(true);
    return response.json();
  };
  await post('workspaces/create', {});
  const alpha = await post('remember', {
    repo: 'alpha', content: 'Alpha retains structured diagnostic events.', dedupe: false,
  });
  // A sibling can carry a lineage pointer without becoming visible in alpha.
  // The real service includes it in All projects, and filters it for repo=alpha.
  const beta = await post('remember', {
    repo: 'beta', content: 'Beta has a separate lineage claim.', dedupe: false,
    metadata: { corrects: alpha.id },
  });
  await page.goto('/?workspace=' + workspace + '&view=library#token=' + token);
  await expect(page.locator('#workspace-select')).toHaveValue(workspace);
  await expect(page.locator('#project-select')).toBeEnabled();
  await page.locator('#project-select').selectOption('alpha');
  await expect(page.locator('#library-list [data-memory-id="' + alpha.id + '"]')).toBeVisible();
  return { workspace, alpha, beta, errors, requests, post };
}

const inspectorHistory = page => page.locator('#memory-detail .record-history');
const historyIds = history => history.locator('[data-history-id]').evaluateAll(
  rows => rows.map(row => row.dataset.historyId),
);

test('project history excludes sibling lineage while preserving promoted and All projects versions', async ({ page }) => {
  const { workspace, alpha, beta, post, requests, errors } = await historyFixture(page);
  await page.locator('#library-list [data-memory-id="' + alpha.id + '"]').click();
  await expect(inspectorHistory(page).getByRole('status')).toHaveText('1 of 1 saved versions.');
  expect(await historyIds(inspectorHistory(page))).toEqual([alpha.id]);
  expect(requests[0].searchParams.get('repo')).toBe('alpha');

  const promoted = await post('promote', { id: alpha.id, repo: 'alpha', target_scope: 'workspace', reason: 'Shared fixture convention.' });
  await inspectorHistory(page).getByRole('button', { name: 'Refresh history' }).click();
  await expect(inspectorHistory(page).getByRole('status')).toHaveText('2 of 2 saved versions.');
  expect(await historyIds(inspectorHistory(page))).toEqual([alpha.id, promoted.id]);
  await inspectorHistory(page).locator('[data-history-id="' + promoted.id + '"]').getByRole('button', { name: 'Inspect version' }).click();
  await expect(inspectorHistory(page).getByRole('status')).toHaveText('2 of 2 saved versions.');
  expect(await historyIds(inspectorHistory(page))).toEqual([alpha.id, promoted.id]);

  await inspectorHistory(page).locator('[data-history-id="' + alpha.id + '"]').getByRole('button', { name: 'Inspect version' }).click();
  await page.locator('#memory-detail').getByRole('button', { name: 'Review saved versions', exact: true }).click();
  await page.locator('#editor-refresh').click();
  const editorHistory = page.locator('#editor-history .record-history');
  await expect(editorHistory.getByRole('status')).toHaveText('2 of 2 saved versions.');
  expect(await historyIds(editorHistory)).toEqual([alpha.id, promoted.id]);
  expect(requests.every(url => url.searchParams.get('workspace') === workspace && url.searchParams.get('repo') === 'alpha')).toBe(true);

  await page.locator('#project-select').selectOption('');
  await page.locator('#library-list [data-memory-id="' + promoted.id + '"]').click();
  await expect(inspectorHistory(page).getByRole('status')).toHaveText('3 of 3 saved versions.');
  expect(await historyIds(inspectorHistory(page))).toEqual([alpha.id, beta.id, promoted.id]);
  expect(requests.at(-1).searchParams.has('repo')).toBe(false);
  expect(errors).toEqual([]);
});

for (const view of ['inspector', 'editor']) {
  for (const destination of ['project', 'workspace']) {
    test(`late ${view} history cannot replace the selected ${destination}`, async ({ page }) => {
      const { workspace, alpha, beta, post, errors } = await historyFixture(page);
      const otherWorkspace = workspace + '-other';
      await post('workspaces/create', { workspace: otherWorkspace });
      const other = await post('remember', { workspace: otherWorkspace, content: 'Other workspace evidence.', dedupe: false });
      const promoted = await post('promote', { id: alpha.id, repo: 'alpha', target_scope: 'workspace', reason: 'Shared fixture convention.' });
      await page.reload();
      await page.locator('#library-list [data-memory-id="' + promoted.id + '"]').click();
      await expect(inspectorHistory(page).getByRole('status')).toHaveText('2 of 2 saved versions.');
      if (view === 'editor') {
        await inspectorHistory(page).locator('[data-history-id="' + alpha.id + '"]').getByRole('button', { name: 'Inspect version' }).click();
        await page.locator('#memory-detail').getByRole('button', { name: 'Review saved versions', exact: true }).click();
      }

      let release, fetched, delivered, heldRequest;
      const failedRequests = [];
      page.on('requestfailed', request => failedRequests.push(request));
      const held = new Promise(resolve => { release = resolve; });
      const ready = new Promise(resolve => { fetched = resolve; });
      const completed = new Promise(resolve => { delivered = resolve; });
      // Fetch a real authorized response, then deliver it after scope navigation.
      await page.route('**/api/memory/*/history?*', async route => {
        heldRequest = route.request();
        const response = await route.fetch();
        fetched();
        await held;
        try { await route.fulfill({ response }); } catch { /* Navigation may abort transport. */ }
        delivered();
      }, { times: 1 });
      if (view === 'editor') await page.locator('#editor-refresh').click();
      else await inspectorHistory(page).getByRole('button', { name: 'Refresh history' }).click();
      await ready;

      if (destination === 'project') await page.locator('#project-select').selectOption('beta');
      else await page.locator('#workspace-select').selectOption(otherWorkspace);
      await page.locator('#library-list [data-memory-id="' + (destination === 'project' ? beta.id : other.id) + '"]').click();
      const expectedIds = destination === 'project' ? [beta.id, promoted.id] : [other.id];
      await expect(inspectorHistory(page).getByRole('status')).toHaveText(`${expectedIds.length} of ${expectedIds.length} saved versions.`);
      const currentHistory = await historyIds(inspectorHistory(page));
      expect(currentHistory).toEqual(expectedIds);
      release();
      await completed;
      await expect.poll(() => failedRequests.includes(heldRequest)).toBe(true);
      expect(heldRequest.failure().errorText).toMatch(/abort|cancel/i);
      await expect(inspectorHistory(page).getByRole('status')).toHaveText(`${expectedIds.length} of ${expectedIds.length} saved versions.`);
      expect(await historyIds(inspectorHistory(page))).toEqual(currentHistory);
      await expect(page.locator('#editor-history')).toBeHidden();
      await expect(page.locator('#memory-editor')).toBeHidden();
      expect(errors).toEqual([]);
    });
  }
}
