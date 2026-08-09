const { test, expect } = require('@playwright/test');
const { createServer } = require('node:http');
const fs = require('node:fs/promises');
const path = require('node:path');

const demoHtml = path.resolve(__dirname, '../../demo/engraphis_screen_demo.html');

async function serve(payload) {
  const html = await fs.readFile(demoHtml);
  const server = createServer((request, response) => {
    if (request.url === '/' || request.url.startsWith('/engraphis_screen_demo.html')) {
      response.writeHead(200, { 'content-type': 'text/html; charset=utf-8' });
      response.end(html);
      return;
    }
    if (request.url === '/generated/screen_demo_payload.json') {
      response.writeHead(200, { 'content-type': 'application/json' });
      response.end(JSON.stringify(payload));
      return;
    }
    response.writeHead(404);
    response.end('Not found');
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const { port } = server.address();
  return {
    url: `http://127.0.0.1:${port}/engraphis_screen_demo.html`,
    close: () => new Promise((resolve, reject) => server.close(error => error ? reject(error) : resolve())),
  };
}

test('screen demo visibly labels malformed generated evidence as sample fallback', async ({ page }) => {
  const server = await serve({});
  try {
    await page.goto(server.url);
    await expect.poll(() => page.evaluate(() => window.demoPayloadReady)).toBe(true);
    await expect(page.locator('#payload-source')).toBeVisible();
    await expect(page.locator('#payload-source')).toHaveText('sample fallback data');
    await expect(page.locator('[data-recall-title]').first()).toHaveText('Where to build');
    expect(await page.evaluate(() => window.demoPayloadSource)).toBe('fallback');
  } finally {
    await server.close();
  }
});

test('screen demo hides the fallback label only for a complete generated payload', async ({ page }) => {
  const payload = {
    session: {
      session_id: 'ses_generated',
      bootstrap: { summary: 'Generated handoff', open_threads: ['Generated thread'] },
    },
    recall: {
      query: 'Generated query',
      memory: {
        title: 'Generated memory',
        content: 'Generated memory evidence', arm: 'semantic', score: 4.2, retention: 0.9,
        provenance: { source: 'generated-run' },
      },
    },
    timeline: [
      { content: 'Old generated fact', valid_to: 100, provenance: { source: 'generated-run' } },
      { content: 'Current generated fact', valid_to: null, provenance: { source: 'generated-run' } },
    ],
    why: { current: { answer: ['Current generated fact'] }, supersedes: [{}] },
    inspection: { events: [{ action: 'invalidate', detail: 'generated event' }] },
  };
  const server = await serve(payload);
  try {
    await page.goto(server.url);
    await expect.poll(() => page.evaluate(() => window.demoPayloadReady)).toBe(true);
    await expect(page.locator('#payload-source')).toBeHidden();
    await expect(page.locator('[data-recall-title]').first()).toHaveText('Generated memory');
    expect(await page.evaluate(() => window.demoPayloadSource)).toBe('generated');
  } finally {
    await server.close();
  }
});
