const { test, expect } = require('@playwright/test');
const AxeBuilder = require('@axe-core/playwright').default;

const answer = {
  grounded: true, answer: 'Postgres is the supported database. [1]', support: 0.99,
  citations: [{ id: 'mem_answer', n: 1, title: 'Database evidence', content: 'Postgres is the supported database.', support: 0.99 }],
};
const emptyInbox = { items: [], count: 0, has_more: false, truncated: false, count_semantics: 'returned_sample' };

async function fixture(page, { coverage, answerFailures = 0, previewFailures = 0, reviews = emptyInbox, statsUnavailable = false } = {}) {
  const requests = [];
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/api/**', async route => {
    const req = route.request();
    const url = new URL(req.url());
    const path = url.pathname.slice(4);
    const body = req.method() === 'POST' ? req.postDataJSON() : null;
    requests.push({ path, body, query: Object.fromEntries(url.searchParams) });
    const ok = json => route.fulfill({ json });
    if (path === '/bootstrap') return ok({ version: '1.7.1', workspaces: [{ name: 'home-work', memories: 12 }], license: { plan: 'local', features: [], known_features: {} } });
    if (path === '/repos') return ok({ repos: [{ id: 'repo_alpha', name: 'alpha' }, { id: 'repo_beta', name: 'beta' }] });
    if (path === '/stats') return statsUnavailable ? route.fulfill({ status: 503, json: { detail: 'Stats unavailable' } }) : ok({ memories: 12, total_rows: 15, sessions: 3, workspaces: 1, by_type: { semantic: 12 } });
    if (path === '/memories') return ok({ memories: [], count: 0, total_count: 0, next_cursor: null });
    if (path === '/proactive') return ok({ memories: [{ id: 'mem_approved', title: 'A known fact', content: 'Approved memory is not a pending decision.', provenance: { trusted: true, review_state: 'approved' } }] });
    if (path === '/audit') return ok({ entries: [] });
    if (path === '/managed-processing') return ok({ enabled: false, notice: 'Managed processing is off.' });
    if (path === '/review-inbox') return ok(reviews);
    if (path === '/answer') {
      if (answerFailures-- > 0) return route.fulfill({ status: 503, json: { detail: 'Answer temporarily unavailable' } });
      return ok({ ...answer, ...(coverage === undefined ? {} : { answer_coverage: coverage }) });
    }
    if (path === '/recall') {
      if (previewFailures-- > 0) return route.fulfill({ status: 503, json: { detail: 'Preview temporarily unavailable' } });
      return ok({ memories: [{ id: 'mem_preview', title: 'Raw evidence', content: 'Candidate Postgres memory.' }] });
    }
    return ok({});
  });
  await page.goto('/?workspace=home-work&view=ask');
  await expect(page.locator('#connection-status')).toContainText('Local engine connected');
  return { requests, errors };
}

async function ask(page, question = 'Which database and retention period?') {
  await page.locator('#ask-input').fill(question);
  await page.locator('#ask-form button[type="submit"]').click();
}

for (const coverage of [undefined, 'partial', 'complete', 'unrecognized']) {
  test('question coverage is independent of support and citations: ' + String(coverage), async ({ page }) => {
    await fixture(page, { coverage });
    await ask(page);
    const expected = ['partial', 'complete'].includes(coverage) ? coverage : 'unknown';
    await expect(page.locator('.answer-coverage')).toContainText('Question coverage: ' + expected);
    await expect(page.locator('.answer-meta')).toContainText('Support 0.99');
    await expect(page.locator('.answer-meta')).toContainText('1 citation');
    await expect(page.locator('#answer-panel')).toContainText(answer.answer);
    if (expected === 'unknown') await expect(page.locator('.answer-coverage')).toContainText('has not been checked against every part');
  });
}

test('answer retry retains the submitted question and never repeats a successful preview', async ({ page }) => {
  const { requests } = await fixture(page, { answerFailures: 1 });
  await ask(page, 'Original question');
  await expect(page.locator('#ask-answer-retry')).toBeVisible();
  await expect(page.locator('#retrieval-list')).toContainText('Candidate Postgres memory.');
  await page.locator('#ask-input').fill('An edited but unsubmitted question');
  await page.locator('#ask-answer-retry').click();
  await expect(page.locator('#answer-panel')).toContainText(answer.answer);
  expect(requests.filter(item => item.path === '/answer').map(item => item.body.query)).toEqual(['Original question', 'Original question']);
  expect(requests.filter(item => item.path === '/recall')).toHaveLength(1);
  await expect(page.locator('#ask-result-query')).toContainText('Original question');
  await expect(page.locator('#ask-status')).toHaveText('Answer ready · Preview ready.');
});

test('preview retry preserves the answer and makes only the preview request', async ({ page }) => {
  const { requests } = await fixture(page, { previewFailures: 1 });
  await ask(page);
  await expect(page.locator('#answer-panel')).toContainText(answer.answer);
  await expect(page.locator('#ask-preview-retry')).toBeVisible();
  await page.locator('#ask-preview-retry').click();
  await expect(page.locator('#ask-status')).toHaveText('Answer ready · Preview ready.');
  expect(requests.filter(item => item.path === '/answer')).toHaveLength(1);
  expect(requests.filter(item => item.path === '/recall')).toHaveLength(2);
  await expect(page.locator('#answer-panel')).toContainText(answer.answer);
});

test('canceling a stalled preview preserves the ready answer and ignores its late response', async ({ page }) => {
  await fixture(page);
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  let previews = 0;
  await page.route('**/api/recall?*', async route => {
    const call = ++previews;
    if (call === 1) await gate;
    await route.fulfill({ json: { memories: [{ id: 'mem_' + call, title: call === 1 ? 'Late canceled preview' : 'Fresh preview' }] } }).catch(() => {});
  });
  await ask(page);
  await expect(page.locator('#answer-panel')).toContainText(answer.answer);
  await page.locator('#ask-cancel').click();
  await expect(page.locator('#ask-status')).toHaveText('Answer ready · Preview canceled.');
  await expect(page.locator('#ask-cancel')).toBeHidden();
  await expect(page.locator('#answer-panel')).toContainText(answer.answer);
  await page.locator('#ask-preview-retry').click();
  await expect(page.locator('#retrieval-list')).toContainText('Fresh preview');
  release();
  await expect(page.locator('#retrieval-list')).not.toContainText('Late canceled preview');
});

test('canceling both pending requests exposes two independent retries', async ({ page }) => {
  await fixture(page);
  await page.route('**/api/answer', () => {});
  await page.route('**/api/recall?*', () => {});
  await ask(page);
  await page.locator('#ask-cancel').click();
  await expect(page.locator('#ask-status')).toHaveText('Answer canceled · Preview canceled.');
  await expect(page.locator('#ask-answer-retry')).toBeVisible();
  await expect(page.locator('#ask-preview-retry')).toBeVisible();
  await expect(page.locator('#answer-panel')).toHaveAttribute('aria-busy', 'false');
  await expect(page.locator('#retrieval-list')).toHaveAttribute('aria-busy', 'false');
});

test('a late retried answer cannot cross a project change or overwrite its new question', async ({ page }) => {
  await fixture(page);
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  const seen = [];
  await page.route('**/api/answer', async route => {
    const body = route.request().postDataJSON();
    seen.push(body);
    if (body.repo === 'alpha' && seen.length === 1) return route.fulfill({ status: 503, json: { detail: 'Retry needed' } });
    if (body.repo === 'alpha') await gate;
    return route.fulfill({ json: { ...answer, answer: body.repo + ' answer' } }).catch(() => {});
  });
  await page.locator('#project-select').selectOption('alpha');
  await ask(page, 'Alpha question');
  await expect(page.locator('#ask-answer-retry')).toBeVisible();
  await page.locator('#ask-answer-retry').click();
  await expect.poll(() => seen.length).toBe(2);
  await page.locator('#project-select').selectOption('beta');
  await expect(page.locator('#ask-result-query')).toBeEmpty();
  await expect(page.locator('#ask-answer-retry')).toBeHidden();
  await ask(page, 'Beta question');
  await expect(page.locator('#answer-panel')).toContainText('beta answer');
  release();
  await expect(page.locator('#answer-panel')).not.toContainText('alpha answer');
  await expect(page.locator('#ask-result-query')).toContainText('Beta question');
});

test('Home separates actual review states from ordinary proactive memories and sample counts', async ({ page }) => {
  await fixture(page, { reviews: { ...emptyInbox, count: 3, has_more: true, items: [
    { id: 'mem_pending', review_state: 'pending' },
    { id: 'mem_quarantined', quarantined: true, review_state: 'pending' },
    { id: 'mem_conflict', review_state: 'approved', conflict_with: ['mem_old'], excerpt: 'Approved conflict excerpt.' },
  ] } });
  await page.locator('[data-view="today"]').click();
  await expect(page.locator('#decision-list')).toContainText('Source review pending');
  await expect(page.locator('#decision-list')).toContainText('Quarantined');
  await expect(page.locator('#decision-list')).toContainText('Conflicting evidence');
  await expect(page.locator('#decision-list')).not.toContainText('Approved memory is not a pending decision');
  await expect(page.locator('#proactive-list')).toContainText('Approved memory is not a pending decision');
  await expect(page.locator('#review-status')).toHaveText('3 review items shown. This is a partial list; more records may need review.');
  await expect(page.getByRole('heading', { name: 'Needs a decision' })).toHaveCount(0);
});

test('Home review failure is unknown and a fresh scoped read recovers it', async ({ page }) => {
  await fixture(page);
  let fail = true;
  const scopes = [];
  await page.route('**/api/review-inbox?*', route => {
    scopes.push(Object.fromEntries(new URL(route.request().url()).searchParams));
    return fail ? route.fulfill({ status: 503, json: { detail: 'Unavailable' } }) : route.fulfill({ json: emptyInbox });
  });
  await page.locator('[data-view="today"]').click();
  await page.locator('#project-select').selectOption('alpha');
  await expect(page.locator('#review-status')).toContainText('Review status is unknown');
  await expect(page.locator('#decision-list')).not.toContainText('No records currently need review');
  fail = false;
  await page.locator('#review-refresh').click();
  await expect(page.locator('#decision-list')).toContainText('No records currently need review in this context.');
  expect(scopes).toEqual([{ workspace: 'home-work', repo: 'alpha', limit: '6' }, { workspace: 'home-work', repo: 'alpha', limit: '6' }]);
});

test('Home setup progress records user confirmations separately from the dashboard connection', async ({ page }) => {
  await fixture(page);
  await page.locator('[data-view="today"]').click();
  await expect(page.locator('#home-engine-status')).toContainText('Dashboard: Local engine connected');
  await expect(page.locator('#home-setup-progress')).toHaveText('0 of 3 setup steps confirmed by you.');
  await expect(page.locator('#home-setup-next')).toContainText('not verified by this dashboard');
  await page.locator('#project-select').selectOption('alpha');
  await page.getByRole('button', { name: 'Continue setup', exact: true }).click();
  await page.locator('#connection-configured').check();
  await page.locator('[data-view="today"]').click();
  await expect(page.locator('#home-setup-progress')).toHaveText('1 of 3 setup steps confirmed by you.');
  await expect(page.locator('#home-setup-next')).toContainText('save a project fact');
  await page.reload();
  await expect(page.locator('#home-setup-progress')).toHaveText('1 of 3 setup steps confirmed by you.');
  await page.locator('#project-select').selectOption('beta');
  await expect(page.locator('#home-setup-progress')).toHaveText('0 of 3 setup steps confirmed by you.');
});

test('late review rows cannot cross a project switch', async ({ page }) => {
  await fixture(page);
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  let alphaStarted = false;
  await page.route('**/api/review-inbox?*', async route => {
    const repo = new URL(route.request().url()).searchParams.get('repo');
    if (repo === 'alpha') { alphaStarted = true; await gate; }
    await route.fulfill({ json: { ...emptyInbox, count: 1, items: [{ id: 'mem_' + repo, review_state: 'pending' }] } }).catch(() => {});
  });
  await page.locator('[data-view="today"]').click();
  await page.locator('#project-select').selectOption('alpha');
  await expect.poll(() => alphaStarted).toBe(true);
  await page.locator('#project-select').selectOption('beta');
  await expect(page.locator('#decision-list [data-memory-id="mem_beta"]')).toBeVisible();
  release();
  await expect(page.locator('#decision-list [data-memory-id="mem_alpha"]')).toHaveCount(0);
});

test('unavailable workspace counts are not presented as an empty first-use workspace', async ({ page }) => {
  await fixture(page, { statsUnavailable: true });
  await page.locator('[data-view="today"]').click();
  await expect(page.locator('#first-memory-journey')).toBeHidden();
  await expect(page.locator('#metrics strong').first()).toHaveText('—');
  await expect(page.locator('#type-bars')).toContainText('Workspace composition is unavailable');
  await expect(page.locator('#home-setup-progress')).toHaveText('0 of 3 setup steps confirmed by you.');
});

test('Ask controls and Home remain accessible at a narrow width', async ({ page }) => {
  const { errors } = await fixture(page, { previewFailures: 1 });
  await ask(page);
  await expect(page.locator('#ask-preview-retry')).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
  await page.route('**/api/answer', route => route.fulfill({ status: 503, json: { detail: 'Answer unavailable' } }));
  await ask(page, 'A question whose answer fails');
  await expect(page.locator('#ask-answer-retry')).toBeVisible();
  await page.locator('.retrieval-details summary').click();
  await expect(page.locator('#retrieval-list')).toContainText('Candidate Postgres memory.');
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
  await page.locator('[data-view="today"]').click();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
  expect(errors).toEqual([]);
});

test('a real pending source leaves the Home review inbox after governed approval', async ({ page }) => {
  page.on('dialog', dialog => dialog.accept(dialog.type() === 'prompt' ? 'Reviewed this local browser fixture.' : undefined));
  await page.goto('/?view=manage#token=engraphis-playwright-local-only');
  await expect(page.locator('#connection-status')).toContainText('Local engine connected');
  await page.locator('#create-workspace-toggle').click();
  await page.locator('#new-workspace-name').fill('home-review-' + Date.now());
  await page.locator('#create-workspace-form button[type="submit"]').click();
  await page.locator('[data-view="library"]').click();
  await page.locator('#new-memory-button').click();
  await page.locator('#editor-memory-content').fill('Home review requires explicit human source approval.');
  await page.locator('#memory-editor button[type="submit"]').click();
  await expect(page.locator('#memory-detail')).toContainText('Home review requires explicit human source approval.');
  await page.locator('[data-view="today"]').click();
  await expect(page.locator('#review-status')).toHaveText('1 review item shown.');
  await expect(page.locator('#home-setup-progress')).toHaveText('0 of 3 setup steps confirmed by you.');
  await page.locator('#decision-list [data-memory-id]').click();
  await page.locator('#memory-detail').getByRole('button', { name: 'Approve for prompt…' }).click();
  await expect(page.locator('#memory-detail')).toContainText('approved');
  await page.locator('[data-view="today"]').click();
  await expect(page.locator('#decision-list')).toContainText('No records currently need review in this context.');
});
