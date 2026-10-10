const { test, expect } = require('@playwright/test');
const AxeBuilder = require('@axe-core/playwright').default;

const prompt = {
  score: 18, word_count: 4,
  sections: [{ index: 0, title: 'Rules', words: 4, kinds: { approval: 1 } }],
  coverage: { role: 0, objective: 0, context: 0, team: 0, rules: 1, review: 0, start: 0 },
  flags: [{ id: 'flag_001', kind: 'vague', token: 'all', start: 15, end: 18, question: 'Which tests?' }],
  counts: { read: 4, links: 0, flagged: 1 }, trace: [],
};
const cluster = {
  cluster_health: 77, node_count: 2,
  nodes: [{ id: 'old', title: 'Old', mtype: 'semantic' }, { id: 'new', title: 'New', mtype: 'semantic' }],
  conflicts: [{ node_a: 'old', node_b: 'new', subject: 'port', reason: 'Port differs',
    remedy: { action: 'supersede', keep_node: 'new', retire_node: 'old' } }],
  orphans: [], policy_gaps: [{ axis: 'team', description: 'Missing ownership policy.' }],
  coverage: prompt.coverage, trace: [],
};

async function fixture(page) {
  const requests = [], errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.route('**/api/**', async route => {
    const req = route.request();
    const path = new URL(req.url()).pathname.slice(4);
    const body = req.method() === 'POST' ? req.postDataJSON() : null;
    requests.push({ path, body });
    if (path === '/bootstrap') return route.fulfill({ json: {
      version: '1.7.9', workspaces: [{ name: 'alpha' }, { name: 'beta' }],
      license: { plan: 'local', features: [], known_features: {} },
    } });
    if (path === '/repos') return route.fulfill({ json: { repos: [{ id: 'repo_A', name: 'repoA' }] } });
    if (path === '/stats') return route.fulfill({ json: { memories: 2, sessions: 0, by_type: { semantic: 2 } } });
    if (path === '/memories' || path === '/proactive') return route.fulfill({ json: { memories: [] } });
    if (path === '/audit') return route.fulfill({ json: { entries: [] } });
    if (path === '/review-inbox') return route.fulfill({ json: { items: [], count: 0, has_more: false } });
    if (path === '/spec/crawl') return route.fulfill({ json: prompt });
    if (path === '/spec/crawl/memories') return route.fulfill({ json: cluster });
    return route.fulfill({ json: {} });
  });
  await page.goto('/?workspace=alpha&view=specstudio');
  await expect(page.locator('#connection-status')).toContainText('Local engine connected');
  return { requests, errors };
}

async function crawl(page, text = '# Rules\nVerify all tests.') {
  await page.locator('#spec-btn-new').click();
  await page.locator('#spec-input-text').fill(text);
  await page.locator('#spec-modal-submit').click();
  await expect(page.locator('#spec-score-display')).toHaveText(String(prompt.score));
}

test('API failure clears scores and shows the real failure', async ({ page }) => {
  const { errors } = await fixture(page);
  await page.route('**/api/spec/crawl', route => route.fulfill({ status: 503, json: { detail: 'Offline analysis unavailable' } }));
  await page.locator('#spec-btn-new').click();
  await page.locator('#spec-input-text').fill('# Rules\nVerify 12 tests.');
  await page.locator('#spec-modal-submit').click();
  await expect(page.locator('#spec-panel4-body')).toContainText('Offline analysis unavailable');
  await expect(page.locator('#spec-score-display')).toHaveText('—');
  await expect(page.locator('#spec-sections-list')).toBeEmpty();
  expect(errors).toEqual([]);
});

test('project selection scopes the memory audit', async ({ page }) => {
  const { requests } = await fixture(page);
  await expect(page.locator('#project-select')).toBeEnabled();
  await page.locator('#project-select').selectOption('repoA');
  await page.locator('#spec-mode-memories').click();
  await expect(page.locator('#spec-score-display')).toHaveText('77');
  expect(requests.filter(item => item.path === '/spec/crawl/memories').at(-1).body)
    .toMatchObject({ workspace: 'alpha', repo: 'repoA' });
});

test('clarification binds current source and scope and reports pending review', async ({ page }) => {
  const { requests, errors } = await fixture(page);
  await page.evaluate(() => SpecStudio.setScope('alpha', 'repoA'));
  await page.route('**/api/spec/crawl/answer', async route => {
    requests.push({ path: '/spec/crawl/answer', body: route.request().postDataJSON() });
    await route.fulfill({ json: { updated_text: '# Rules\nVerify 12 tests.', memory_id: 'pending', new_report: { ...prompt, flags: [] } } });
  });
  await crawl(page);
  await page.getByRole('button', { name: 'Clarify all' }).click();
  await expect(page.locator('#spec-ask-input')).toBeFocused();
  await page.keyboard.press('Shift+Tab');
  await expect(page.locator('#spec-ask-submit')).toBeFocused();
  await page.keyboard.press('Tab');
  await expect(page.locator('#spec-ask-input')).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(page.getByRole('button', { name: 'Clarify all' })).toBeFocused();
  await page.getByRole('button', { name: 'Clarify all' }).click();
  await page.locator('#spec-ask-input').fill('12');
  await page.locator('#spec-ask-submit').click();
  await expect(page.locator('#spec-toast')).toContainText('saved for review');
  const answer = requests.find(item => item.path === '/spec/crawl/answer').body;
  expect(answer).toMatchObject({ workspace: 'alpha', repo: 'repoA', spec_text: '# Rules\nVerify all tests.', answer: '12', save_as_memory: true });
  expect(errors).toEqual([]);
});

test('scope changes clear old logs and ignore late memory responses', async ({ page }) => {
  const { requests } = await fixture(page);
  let release, delayed;
  const gate = new Promise(resolve => { release = resolve; });
  const started = new Promise(resolve => { delayed = resolve; });
  await page.route('**/api/spec/crawl/memories', async route => {
    const body = route.request().postDataJSON();
    requests.push({ path: '/spec/crawl/memories', body });
    if (body.workspace === 'alpha') { delayed(); await gate; }
    await route.fulfill({ json: { ...cluster, cluster_health: body.workspace === 'beta' ? 77 : 4 } });
  });
  await page.evaluate(() => {
    SpecStudio.setPromptReport({ score: 1, sections: [], coverage: {}, flags: [], trace: [], counts: {} });
    document.getElementById('spec-log-body').textContent = 'alpha-only';
  });
  await page.locator('#spec-mode-memories').click();
  await started;
  await page.locator('#workspace-select').selectOption('beta');
  await expect(page.locator('#spec-score-display')).toHaveText('77');
  release();
  await expect(page.locator('#spec-log-body')).not.toContainText('alpha-only');
  await expect(page.locator('#spec-score-display')).toHaveText('77');
  expect(requests.filter(item => item.path === '/spec/crawl/memories').at(-1).body.workspace).toBe('beta');
});

test('remediation uses the recommended keeper and requires confirmation', async ({ page }) => {
  const { requests } = await fixture(page);
  await page.evaluate(() => SpecStudio.setScope('alpha', 'repoA'));
  await page.route('**/api/spec/crawl/memories/resolve', async route => {
    requests.push({ path: '/spec/crawl/memories/resolve', body: route.request().postDataJSON() });
    await route.fulfill({ status: 403, json: { detail: 'Forbidden' } });
  });
  await page.locator('#spec-mode-memories').click();
  await expect(page.locator('#spec-panel4-body')).toContainText('Missing ownership policy.');
  page.once('dialog', dialog => dialog.dismiss());
  await page.getByRole('button', { name: 'Supersede Older' }).click();
  expect(requests.filter(item => item.path.endsWith('/resolve'))).toHaveLength(0);
  page.once('dialog', dialog => dialog.accept());
  await page.getByRole('button', { name: 'Supersede Older' }).click();
  await expect(page.locator('#spec-toast')).toContainText('Forbidden');
  expect(requests.find(item => item.path.endsWith('/resolve')).body).toMatchObject({
    node_a: 'new', node_b: 'old', workspace: 'alpha', repo: 'repoA', confirmed: true,
  });
  await expect(page.locator('#spec-score-display')).toHaveText('—');
});

test('untrusted report text stays text and the view passes accessibility checks', async ({ page }) => {
  await fixture(page);
  await page.route('**/api/spec/crawl', route => route.fulfill({ json: {
    ...prompt, sections: [{ ...prompt.sections[0], title: '<input id="injected-heading">' }],
  } }));
  await crawl(page);
  await expect(page.locator('#spec-sections-list')).toContainText('<input id="injected-heading">');
  await expect(page.locator('#injected-heading')).toHaveCount(0);
  await page.evaluate(report => SpecStudio.setMemoryReport(report), {
    ...cluster, conflicts: [{ ...cluster.conflicts[0], subject: '<input id="injected-subject">' }],
    orphans: [{ node_id: 'old', title: '<input id="injected-orphan">' }],
  });
  await expect(page.locator('#injected-subject, #injected-orphan')).toHaveCount(0);
  const result = await new AxeBuilder({ page }).include('.specstudio-view').withTags(['wcag2a', 'wcag2aa', 'wcag21aa']).analyze();
  expect(result.violations).toEqual([]);
});

test('score ring tracks the report and empty traces do not keep requesting frames', async ({ page }) => {
  await fixture(page);
  await crawl(page);
  const offset = Number(await page.locator('#spec-score-ring').getAttribute('stroke-dashoffset'));
  expect(offset).toBeCloseTo(205.82, 2);
  await page.emulateMedia({ reducedMotion: 'no-preference' });
  const frames = await page.evaluate(async () => {
    let count = 0;
    const raf = window.requestAnimationFrame.bind(window);
    window.requestAnimationFrame = callback => raf(time => { count++; callback(time); });
    SpecStudio.setPromptReport({ score: 0, sections: [], coverage: {}, flags: [], trace: [], counts: {} });
    await new Promise(resolve => setTimeout(resolve, 100));
    return count;
  });
  expect(frames).toBe(0);
});
