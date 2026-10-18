// Shared browser-test helper for the dashboard's in-page action dialog (#action-dialog),
// which replaced the browser's native confirm() and prompt() boxes.
const { expect } = require('@playwright/test');

/**
 * Wait for the action dialog, optionally check its text and fill its fields, then
 * confirm (default) or cancel it.
 *
 * @param {import('@playwright/test').Page} page
 * @param {{ accept?: boolean, fields?: Record<string, string>, expectText?: string[] }} options
 */
async function answerActionDialog(page, { accept = true, fields = {}, expectText = [] } = {}) {
  const dialog = page.locator('#action-dialog');
  await expect(dialog).toBeVisible();
  for (const expected of expectText) await expect(dialog).toContainText(expected);
  for (const [name, value] of Object.entries(fields)) {
    await page.locator(`#action-dialog-field-${name}`).fill(value);
  }
  await page.locator(accept ? '#action-dialog-submit' : '#action-dialog-cancel').click();
  await expect(dialog).toBeHidden();
}

module.exports = { answerActionDialog };
