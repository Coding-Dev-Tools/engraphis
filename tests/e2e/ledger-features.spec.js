// Real-server coverage for the features Ledger took over from the retired Classic dashboard.
// These run against the Playwright web server's in-memory database, so the browser, the
// strict CSP and the real API contracts are all exercised together.
const { test, expect } = require('@playwright/test');
const { answerActionDialog } = require('./action-dialog-helper');

const TOKEN_URL = '/#token=engraphis-playwright-local-only';

// The real server answers the very first request with 401 until the URL token has been
// exchanged for the browser session, so resource-load reports are expected noise here.
function pageErrors(page) {
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('console', message => {
    if (message.type() === 'error' && !message.text().startsWith('Failed to load resource')) {
      errors.push(message.text());
    }
  });
  return errors;
}

async function createWorkspace(page, name) {
  await page.locator('.nav-item[data-view="manage"]').click();
  await page.locator('#create-workspace-toggle').click();
  await page.locator('#new-workspace-name').fill(name);
  await page.locator('#create-workspace-form button[type="submit"]').click();
  await expect(page.locator('#workspace-select')).toHaveValue(name);
}

async function addMemory(page, title, content) {
  await page.locator('.nav-item[data-view="library"]').click();
  await page.locator('#new-memory-button').click();
  await page.locator('#editor-memory-title').fill(title);
  await page.locator('#editor-memory-content').fill(content);
  await page.locator('#memory-editor button[type="submit"]').click();
  await expect(page.locator('#library-list')).toContainText(title);
}

const MARKDOWN = [
  '# Storage decision',
  '',
  'We keep **SQLite** in `WAL` mode for local installs.',
  '',
  '- Fast reads',
  '- See [the WAL notes](https://sqlite.org/wal.html)',
  '',
  '<img src=x onerror="window.__ledgerImportXss = true">',
  '',
  'Related: [[Backup policy]]',
].join('\n');

test('Quick import renders Markdown inertly and the editor previews, guards and diffs edits', async ({ page }) => {
  const errors = pageErrors(page);
  const workspace = `features-import-${Date.now()}`;
  await page.goto(TOKEN_URL);
  await expect(page.locator('#connection-status')).toContainText('connected');
  await createWorkspace(page, workspace);

  // Library → Import → Quick import… uploads a real Markdown file through the import API.
  await page.locator('.nav-item[data-view="library"]').click();
  const menuButton = page.locator('#import-menu-button');
  await menuButton.click();
  await expect(menuButton).toHaveAttribute('aria-expanded', 'true');
  await expect(page.locator('#import-menu')).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(page.locator('#import-menu')).toBeHidden();
  await expect(menuButton).toBeFocused();
  await menuButton.click();
  await page.locator('#import-quick-button').click();
  await expect(page.locator('#quick-import-dialog')).toBeVisible();
  await expect(page.locator('#quick-import-workspace')).toHaveText(workspace);
  await page.locator('#quick-import-files').setInputFiles({
    name: 'storage-decision.md', mimeType: 'text/markdown', buffer: Buffer.from(MARKDOWN),
  });
  await expect(page.locator('#quick-import-status')).toContainText('1 file imported');
  await expect(page.locator('#quick-import-status')).toContainText('untrusted until you review them');
  await page.locator('#quick-import-close').click();
  await expect(page.locator('#quick-import-dialog')).toBeHidden();

  // The imported record renders as Markdown, and its HTML stays inert text.
  const imported = page.locator('#library-list [role="option"]').filter({ hasText: 'SQLite' }).first();
  await imported.press('Enter');
  const body = page.locator('#memory-detail .markdown-body');
  await expect(body.locator('strong')).toHaveText('SQLite');
  await expect(body.locator('code').first()).toHaveText('WAL');
  const link = body.getByRole('link', { name: 'the WAL notes' });
  await expect(link).toHaveAttribute('href', 'https://sqlite.org/wal.html');
  await expect(link).toHaveAttribute('rel', 'noopener noreferrer nofollow');
  await expect(body.locator('.md-wikilink')).toHaveText('Backup policy');
  await expect(body.locator('img')).toHaveCount(0);
  await expect(body).toContainText('onerror');
  expect(await page.evaluate(() => window.__ledgerImportXss)).toBeUndefined();

  // A second memory, then an edit that is previewed, guarded and finally saved.
  await addMemory(page, 'Release checklist', 'Tag the release after the tests pass.');
  await page.locator('#library-list [role="option"]').filter({ hasText: 'Release checklist' }).press('Enter');
  await page.locator('#memory-detail').getByRole('button', { name: 'Edit', exact: true }).click();
  const content = page.locator('#editor-memory-content');
  await content.fill('Tag the **signed** release after the tests pass.');
  await expect(page.locator('#editor-save-state')).toHaveText('Unsaved changes');
  await page.locator('#editor-preview-mode').click();
  await expect(page.locator('#editor-preview strong')).toHaveText('signed');
  await expect(content).toBeHidden();
  await page.locator('#editor-write-mode').click();
  await expect(content).toBeVisible();

  // Opening another memory asks first; keeping the draft leaves it untouched.
  await page.locator('#library-list [role="option"]').filter({ hasText: 'SQLite' }).click();
  await answerActionDialog(page, { accept: false, expectText: ['Discard unsaved changes?'] });
  await expect(content).toHaveValue('Tag the **signed** release after the tests pass.');
  await page.keyboard.press('Control+s');
  await expect(page.locator('#library-list')).toContainText('signed');
  await expect(page.locator('#editor-save-state')).toHaveText('');

  // History shows the change as a word diff.
  await page.locator('#library-list [role="option"]').filter({ hasText: 'signed' }).press('Enter');
  const history = page.getByRole('region', { name: 'Record history' });
  await expect(history.locator('.history-diff ins').first()).toContainText('signed');
  expect(errors).toEqual([]);
});

test('Library keeps a keyboard reorder across reloads', async ({ page }) => {
  const errors = pageErrors(page);
  const workspace = `features-order-${Date.now()}`;
  await page.goto(TOKEN_URL);
  await expect(page.locator('#connection-status')).toContainText('connected');
  await createWorkspace(page, workspace);
  await addMemory(page, 'First written', 'Written first.');
  await addMemory(page, 'Second written', 'Written second.');

  const options = page.locator('#library-list [role="option"]');
  await expect(options).toHaveCount(2);
  const before = await options.allInnerTexts();
  const saved = page.waitForResponse(response => response.url().includes('/api/memories/reorder')
    && response.request().method() === 'POST');
  await options.first().focus();
  await page.keyboard.press('Alt+ArrowDown');
  expect((await saved).status()).toBe(200);
  await expect(page.locator('#library-reorder-status')).toContainText('moved to position 2');
  await expect(options.nth(1)).toBeFocused();

  await page.reload();
  await expect(page.locator('#workspace-select')).toHaveValue(workspace);
  await page.locator('.nav-item[data-view="library"]').click();
  await expect(options).toHaveCount(2);
  const after = await options.allInnerTexts();
  expect(after[0]).toBe(before[1]);
  expect(after[1]).toBe(before[0]);
  expect(errors).toEqual([]);
});

test('Workspaces can be described, copied, merged, exported and hard-deleted', async ({ page }) => {
  const errors = pageErrors(page);
  const stamp = Date.now();
  const source = `features-source-${stamp}`;
  const target = `features-target-${stamp}`;
  await page.goto(TOKEN_URL);
  await expect(page.locator('#connection-status')).toContainText('connected');
  await createWorkspace(page, target);
  await createWorkspace(page, source);
  await addMemory(page, 'Portable note', 'This note travels between workspaces.');

  await page.locator('.nav-item[data-view="manage"]').click();
  // A card's heading is the workspace name, followed by an "active" tag on the current one.
  const card = name => page.locator('.workspace-card').filter({
    has: page.getByRole('heading', { name: new RegExp(`^${name}( active)?$`) }),
  });

  await card(source).getByRole('button', { name: 'Describe' }).click();
  await answerActionDialog(page, { fields: { value: 'Notes that move between projects.' } });
  await expect(card(source)).toContainText('Notes that move between projects.');

  const download = page.waitForEvent('download');
  await card(source).getByRole('button', { name: 'Export JSON' }).click();
  expect((await download).suggestedFilename()).toBe(`engraphis-export-${source}.json`);

  const named = name => page.locator('#workspace-select option').filter({ hasText: new RegExp(`^${name}$`) });
  const copies = page.locator('#workspace-select option').filter({ hasText: new RegExp(`^${source}.+`) });
  await card(source).getByRole('button', { name: 'Copy' }).click();
  await expect(copies).toHaveCount(1);
  await expect(page.locator('#notice-banner')).toContainText('1 memory');

  await card(source).getByRole('button', { name: 'Merge into…' }).click();
  await expect(page.locator('#action-dialog')).toContainText('is removed afterwards');
  await page.locator('#action-dialog-field-target').selectOption(target);
  await page.locator('#action-dialog-submit').click();
  await expect(named(source)).toHaveCount(0);
  await expect(page.locator('#workspace-select')).toHaveValue(target);
  await expect(card(target)).toContainText('1 memory ·');

  await card(target).getByRole('button', { name: 'Delete' }).click();
  await answerActionDialog(page, { expectText: ['hard delete and cannot be undone'] });
  await expect(named(target)).toHaveCount(0);
  expect(errors).toEqual([]);
});

test('Secure erase removes a leaked secret only after an explicit confirmation', async ({ page }) => {
  const errors = pageErrors(page);
  const workspace = `features-erase-${Date.now()}`;
  await page.goto(TOKEN_URL);
  await expect(page.locator('#connection-status')).toContainText('connected');
  await createWorkspace(page, workspace);
  await addMemory(page, 'Leaked deploy key', 'The deploy key is test-key-not-real-0001.');
  await page.locator('#library-list [role="option"]').filter({ hasText: 'Leaked deploy key' }).press('Enter');

  const erase = page.locator('#memory-detail').getByRole('button', { name: 'Secure erase leak' });
  await erase.click();
  await answerActionDialog(page, { accept: false, expectText: ['cannot be undone', 'Rotate the leaked credential'] });
  await expect(page.locator('#library-list')).toContainText('Leaked deploy key');

  const erased = page.waitForResponse(response => response.url().includes('/api/secure-erase'));
  await erase.click();
  await answerActionDialog(page);
  expect((await erased).status()).toBe(200);
  await expect(page.locator('#notice-banner')).toContainText('securely erased');
  await expect(page.locator('#notice-banner')).toContainText('Rotate the leaked credential');
  await expect(page.locator('#library-list')).not.toContainText('Leaked deploy key');
  expect(errors).toEqual([]);
});

test('Dialogs never display bidi controls carried by untrusted names', async ({ page }) => {
  const errors = pageErrors(page);
  await page.goto(TOKEN_URL);
  await expect(page.locator('#connection-status')).toContainText('connected');
  // A crafted title could reorder the dialog's prose on screen, e.g. to make "Delete" read
  // as something harmless. Every explicit and implicit direction control is dropped.
  const controls = '؜‎‏‪‫‬‭‮⁦⁧⁨⁩';
  const answer = page.evaluate(marks => window.EngraphisDialog.confirm({
    title: `Delete ${marks}txt.exe?`,
    message: [`Workspace ${marks}notes`, 'This cannot be undone.'],
    confirmLabel: `Delete${marks}`,
    danger: true,
  }), controls);
  const dialog = page.locator('#action-dialog');
  await expect(dialog).toBeVisible();
  const shown = await dialog.evaluate(element => element.textContent);
  expect(shown).toContain('Delete txt.exe?');
  expect(shown).toContain('Workspace notes');
  expect([...controls].filter(mark => shown.includes(mark))).toEqual([]);
  // Destructive confirmations start on Cancel, so Enter cannot confirm by accident.
  await expect(page.locator('#action-dialog-cancel')).toBeFocused();
  await page.locator('#action-dialog-cancel').click();
  expect(await answer).toBe(false);
  expect(errors).toEqual([]);
});

test('A save that fails after leaving the editor is still reported', async ({ page }) => {
  const errors = pageErrors(page);
  const workspace = `features-save-${Date.now()}`;
  await page.goto(TOKEN_URL);
  await expect(page.locator('#connection-status')).toContainText('connected');
  await createWorkspace(page, workspace);
  await addMemory(page, 'Existing note', 'Already saved.');

  // Hold the next save, then answer it with a server error.
  let release;
  const held = new Promise(resolve => { release = resolve; });
  await page.route('**/api/remember', async route => {
    await held;
    await route.fulfill({ status: 500, contentType: 'application/json', body: '{"error":"disk full"}' });
  });
  await page.locator('#new-memory-button').click();
  await page.locator('#editor-memory-title').fill('Draft in flight');
  await page.locator('#editor-memory-content').fill('This save will fail.');
  await page.locator('#memory-editor button[type="submit"]').click();

  // Leaving during the save says what is at stake instead of offering to "discard".
  await page.locator('#library-list [role="option"]').filter({ hasText: 'Existing note' }).click();
  await answerActionDialog(page, { expectText: ['still being saved', 'cannot be recovered'] });
  await expect(page.locator('#memory-editor')).toBeHidden();
  release();
  await expect(page.locator('#notice-banner')).toContainText('Could not save “Draft in flight”');
  expect(errors).toEqual([]);
});
