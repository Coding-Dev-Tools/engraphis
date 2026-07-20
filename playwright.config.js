// @ts-check
const { defineConfig } = require('@playwright/test');

module.exports = defineConfig({
  testDir: './tests/e2e',
  timeout: 60_000,
  retries: 0,
  use: {
    baseURL: 'http://127.0.0.1:8700',
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
  },
  webServer: {
    command: 'engraphis-dashboard --no-open --port 8700',
    url: 'http://127.0.0.1:8700/api/health',
    timeout: 120_000,
    reuseExistingServer: !process.env.CI,
    env: {
      ENGRAPHIS_EMBED_MODEL: '',
      ENGRAPHIS_LOOP_INTERVAL: '0',
      ENGRAPHIS_HOST: '127.0.0.1',
      ENGRAPHIS_SERVICE_MODE: 'customer',
    },
  },
  projects: [
    {
      name: 'chromium',
      use: { browserName: 'chromium' },
    },
  ],
});
