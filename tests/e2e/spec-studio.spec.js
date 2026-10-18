const { test, expect } = require('@playwright/test');
const AxeBuilder = require('@axe-core/playwright').default;
const { answerActionDialog } = require('./action-dialog-helper');

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

function limitedCluster({ nodes = false, words = false, total = 120 } = {}) {
  const memories = Array.from({ length: nodes ? 150 : 16 }, (_, i) => ({
    id: `entry-${i}`, title: `Entry ${i}`, mtype: 'semantic',
    word_analysis_truncated: words && i === 0,
  }));
  const conflicts = [];
  for (let i = 0; i < 16 && conflicts.length < Math.min(total, 50); i++) {
    for (let j = i + 1; j < 16 && conflicts.length < Math.min(total, 50); j++) {
      conflicts.push({ node_a: memories[i].id, node_b: memories[j].id,
        subject: 'port', reason: 'Different configured ports.',
        remedy: { action: 'clarify', candidate_nodes: [memories[i].id, memories[j].id],
          recommendation: 'Choose the retained configuration explicitly.' } });
    }
  }
  return { ...cluster, nodes: memories, node_count: memories.length, conflicts,
    orphans: [], policy_gaps: [], trace: [],
    total_eligible_node_count: nodes ? 151 : memories.length, node_selection_truncated: nodes,
    word_analysis_truncated_node_count: words ? 1 : 0, word_limit: 5000,
    conflicts_total: total, conflict_limit: 50, conflicts_truncated: total > 50,
    conflict_remediation_count: Math.min(total, 10), conflict_remediation_limit: 10,
    conflict_remediation_truncated: total > 10,
    analysis_truncated: nodes || words || total > 50, truncated: nodes || words || total > 50,
  };
}

for (const scenario of [
  { name: 'conflicts only', total: 120 },
  { name: 'word and conflict limits', words: true, total: 120 },
  { name: 'node and conflict limits', nodes: true, total: 120 },
  { name: 'node word and conflict limits', nodes: true, words: true, total: 120 },
  { name: 'exactly fifty complete conflicts with a partial plan', total: 50 },
  { name: 'zero conflicts', total: 0 },
  { name: 'exactly ten complete conflict suggestions', total: 10 },
  { name: 'eleven conflicts with a partial plan', total: 11 },
]) {
  test(`memory audit discloses ${scenario.name} without hiding other limits`, async ({ page }) => {
    const { errors } = await fixture(page);
    const report = limitedCluster(scenario);
    await page.route('**/api/spec/crawl/memories', route => route.fulfill({ json: report }));
    await page.locator('#spec-mode-memories').click();
    const list = page.locator('#spec-sections-list');
    const panel = page.locator('#spec-panel4-body');
    if (scenario.total > 50) {
      await expect(list).toContainText('Showing 50 of 120 conflicts');
      await expect(panel).toContainText('Showing 50 of 120 conflicts');
      await expect(page.locator('#spec-panel4-badge')).toContainText('50 / 120 conf');
    }
    if (scenario.total > 10) {
      await expect(list).toContainText(`10 of ${scenario.total} conflict suggestions`);
      await expect(panel).toContainText(`10 of ${scenario.total} conflict suggestions`);
    }
    if (scenario.nodes) await expect(list).toContainText('150 of 151 eligible memories');
    if (scenario.words) await expect(list).toContainText('Partial word analysis');
    await expect(list.locator('.spec-truncation-notice')).toHaveCount(
      Number(Boolean(scenario.nodes)) + Number(Boolean(scenario.words))
      + Number(scenario.total > 50) + Number(scenario.total > 10));
    await expect(panel.locator('.spec-conflict-card')).toHaveCount(Math.min(scenario.total, 50));
    expect(errors).toEqual([]);
  });
}

test('memory audit rejects unsafe count metadata and keeps report text inert', async ({ page }) => {
  const { errors } = await fixture(page);
  const report = limitedCluster();
  const payload = '<img src=x onerror="window.__countXss=true">';
  Object.assign(report, { conflicts_total: payload, conflict_remediation_count: payload,
    conflict_remediation_limit: -1 });
  report.conflicts[0].reason = payload;
  await page.route('**/api/spec/crawl/memories', route => route.fulfill({ json: report }));
  await page.locator('#spec-mode-memories').click();
  await expect(page.locator('#spec-sections-list')).toContainText('Additional conflicts are omitted');
  await expect(page.locator('#spec-panel4-body')).toContainText(payload);
  await expect(page.locator('#spec-panel4-body img')).toHaveCount(0);
  await expect(page.locator('#spec-panel4-body .spec-conflict-card')).toHaveCount(50);
  expect(await page.evaluate(() => window.__countXss)).toBeUndefined();
  expect(errors).toEqual([]);
});

test('bounded memory audits label health and node counts as a sample', async ({ page }) => {
  await fixture(page);
  await page.route('**/api/spec/crawl/memories', route => route.fulfill({ json: {
    ...cluster, analysis_truncated: true, total_eligible_node_count: 151,
  } }));
  await page.locator('#spec-mode-memories').click();
  await expect(page.locator('#spec-score-motto')).toContainText('sample health');
  await expect(page.locator('#spec-score-motto')).toContainText('more remain');
  await expect(page.locator('#spec-sections-count')).toHaveText('2 / 151');
});

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
  await expect(page.locator('#spec-ask-save-memory')).not.toBeChecked();
  await page.locator('#spec-ask-save-memory').check();
  await page.locator('#spec-ask-submit').click();
  await expect(page.locator('#spec-toast')).toContainText('saved for review');
  const answer = requests.find(item => item.path === '/spec/crawl/answer').body;
  expect(answer).toMatchObject({ workspace: 'alpha', repo: 'repoA', spec_text: '# Rules\nVerify all tests.', answer: '12', save_as_memory: true });
  expect(errors).toEqual([]);
});

test('clarifications update the spec without saving a memory by default', async ({ page }) => {
  const { requests, errors } = await fixture(page);
  await page.route('**/api/spec/crawl/answer', async route => {
    requests.push({ path: '/spec/crawl/answer', body: route.request().postDataJSON() });
    await route.fulfill({ json: { updated_text: '# Rules\nVerify 12 tests.', memory_id: null,
      new_report: { ...prompt, flags: [] } } });
  });
  await crawl(page);
  await page.getByRole('button', { name: 'Clarify all' }).click();
  await expect(page.locator('#spec-ask-save-memory')).not.toBeChecked();
  await page.locator('#spec-ask-input').fill('12');
  await page.locator('#spec-ask-submit').click();
  await expect(page.locator('#spec-toast')).toContainText('Specification updated.');
  expect(requests.find(item => item.path === '/spec/crawl/answer').body.save_as_memory).toBe(false);
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
  await page.evaluate(() => {
    window.__nativeConfirmationCalls = 0;
    window.confirm = () => { window.__nativeConfirmationCalls++; return false; };
  });
  await page.evaluate(() => SpecStudio.setScope('alpha', 'repoA'));
  await page.route('**/api/spec/crawl/memories/resolve', async route => {
    requests.push({ path: '/spec/crawl/memories/resolve', body: route.request().postDataJSON() });
    await route.fulfill({ status: 403, json: { detail: 'Forbidden' } });
  });
  await page.locator('#spec-mode-memories').click();
  await expect(page.locator('#spec-panel4-body')).toContainText('Missing ownership policy.');
  await page.getByRole('button', { name: 'Supersede', exact: true }).click();
  await answerActionDialog(page, { accept: false, expectText: ['old', 'new'] });
  expect(requests.filter(item => item.path.endsWith('/resolve'))).toHaveLength(0);
  await page.getByRole('button', { name: 'Supersede', exact: true }).click();
  await answerActionDialog(page, { expectText: ['old', 'new'] });
  await expect(page.locator('#spec-toast')).toContainText('Forbidden');
  expect(requests.find(item => item.path.endsWith('/resolve')).body).toMatchObject({
    node_a: 'new', node_b: 'old', workspace: 'alpha', repo: 'repoA', confirmed: true,
  });
  await expect(page.locator('#spec-score-display')).toHaveText('—');
  expect(await page.evaluate(() => window.__nativeConfirmationCalls)).toBe(0);
});

for (const change of ['workspace', 'repo', 'scope roundtrip', 'mode', 'report', 'report roundtrip', 'ids', 'nodes', 'refresh']) {
  test(`supersession cancels when ${change} changes while the in-page confirmation is open`, async ({ page }) => {
    const { requests, errors } = await fixture(page);
    await page.evaluate(report => {
      window.confirm = () => false;
      window.__confirmationReport = report;
      SpecStudio.setScope('alpha', 'repoA');
    }, cluster);
    await page.locator('#spec-mode-memories').click();
    await expect(page.getByRole('button', { name: 'Supersede', exact: true })).toBeVisible();
    // Expose the same ordinary report object to the public report setter so that the
    // in-place ID case exercises authorization, independently of identity/epoch.
    await page.evaluate(() => SpecStudio.setMemoryReport(window.__confirmationReport));
    await page.getByRole('button', { name: 'Supersede', exact: true }).click();
    await expect(page.locator('#action-dialog')).toBeVisible();
    await page.evaluate(kind => {
      if (kind === 'workspace') SpecStudio.setScope('beta', 'repoA');
      if (kind === 'repo') SpecStudio.setScope('alpha', 'repoB');
      if (kind === 'scope roundtrip') { SpecStudio.setScope('beta', 'repoA'); SpecStudio.setScope('alpha', 'repoA'); }
      if (kind === 'mode') SpecStudio.setMode('prompt');
      if (kind === 'report') SpecStudio.setMemoryReport({ ...window.__confirmationReport });
      if (kind === 'report roundtrip') {
        SpecStudio.setMemoryReport({ ...window.__confirmationReport });
        SpecStudio.setMemoryReport(window.__confirmationReport);
      }
      if (kind === 'ids') window.__confirmationReport.conflicts[0].remedy.retire_node = 'unreviewed';
      if (kind === 'nodes') window.__confirmationReport.nodes = [{ id: 'new' }];
      if (kind === 'refresh') document.getElementById('spec-btn-new').click();
    }, change);
    await answerActionDialog(page);
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    expect(requests.filter(item => item.path.endsWith('/resolve'))).toHaveLength(0);
    expect(errors).toEqual([]);
  });
}

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

test('ambiguous keeper instructions remain text and cannot trigger supersession', async ({ page }) => {
  const { requests, errors } = await fixture(page);
  await page.evaluate(report => SpecStudio.setMemoryReport(report), {
    ...cluster,
    nodes: [{ id: 'old', title: '<input id="unsafe-candidate">', valid_from: 2000 },
      { id: 'new', title: 'Current configuration', valid_from: 2000 }],
    conflicts: [{ ...cluster.conflicts[0], remedy: {
      action: 'clarify', candidate_nodes: ['old', 'new'],
      recommendation: 'Effective dates are equal. Choose the retained memory explicitly.',
    } }],
  });
  const panel = page.locator('#spec-panel4-body');
  await expect(panel).toContainText('Effective dates are equal. Choose the retained memory explicitly.');
  await expect(panel).toContainText('<input id="unsafe-candidate"> (old)');
  await expect(panel).toContainText('1970-01-01T00:33:20.000Z');
  await expect(page.locator('#unsafe-candidate')).toHaveCount(0);
  await expect(panel.locator('.btn-resolve')).toHaveCount(0);
  expect(requests.filter(item => item.path.endsWith('/resolve'))).toEqual([]);
  expect(errors).toEqual([]);
});
