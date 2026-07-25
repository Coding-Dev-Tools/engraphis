// @ts-check
const { defineConfig } = require('@playwright/test');

const playwrightPort = Number(process.env.ENGRAPHIS_PLAYWRIGHT_PORT || 8700);
if (!Number.isInteger(playwrightPort) || playwrightPort < 1024 || playwrightPort > 65535) {
  throw new Error('ENGRAPHIS_PLAYWRIGHT_PORT must be an integer from 1024 to 65535');
}
const playwrightBaseURL = `http://127.0.0.1:${playwrightPort}`;

module.exports = defineConfig({
  testDir: './tests/e2e',
  timeout: 60_000,
  retries: 0,
  use: {
    baseURL: playwrightBaseURL,
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
  },
  webServer: {
    // Run the checked-out source, not a possibly stale globally installed console script.
    command: `python -m scripts.start_dashboard --no-open --port ${playwrightPort}`,
    url: `${playwrightBaseURL}/api/health`,
    timeout: 120_000,
    reuseExistingServer: false,
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
