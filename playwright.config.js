// @ts-check
const { defineConfig } = require('@playwright/test');

module.exports = defineConfig({
  testDir: './tests/e2e',
  timeout: 30_000,
  retries: 0,
  use: {
    baseURL: 'http://127.0.0.1:8700',
    headless: true,
    viewport: { width: 1280, height: 720 },
  },
  projects: [
    { name: 'chromium', use: { browserName: 'chromium' } },
  ],
  webServer: {
    command: 'engraphis-dashboard --no-open',
    port: 8700,
    timeout: 60_000,
    reuseExistingServer: true,
    env: {
      ENGRAPHIS_EMBED_MODEL: '',
      ENGRAPHIS_LOOP_INTERVAL: '0',
      ENGRAPHIS_HOST: '127.0.0.1',
    },
  },
});
