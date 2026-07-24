const { test, expect } = require('@playwright/test');
const AxeBuilder = require('@axe-core/playwright').default;

const hostedLicense = {
  plan: 'local',
  features: [],
  cloud_managed: true,
  trial_seconds: 259_200,
  grace_seconds: 86_400,
  grace_scope: 'existing authenticated local workspace writes only',
  pro_upgrade_url: 'https://cloud.engraphis.test/pro',
  team_upgrade_url: 'https://cloud.engraphis.test/team',
  upgrade_url: 'https://cloud.engraphis.test/pricing',
  trial: { used: false, trial_days: 3 },
};

async function mockLocalClient(page, cloudStatus = 402, syncRunStatus = null) {
  const calls = [];
  let syncLast = null;

  await page.route('**/api/**', async route => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname.replace(/^\/api/, '');
    calls.push({ path, method: request.method(), query: url.search });

    let status = 200;
    let body = {};

    if (path === '/bootstrap') {
      body = {
        license: hostedLicense,
        workspaces: [],
        embedder: { semantic: true },
      };
    } else if (path === '/health') {
      body = { status: 'ok' };
    } else if (path === '/stats') {
      body = {
        memories: 0,
        total_rows: 0,
        workspaces: 0,
        sessions: 0,
        by_type: {},
      };
    } else if (path === '/license') {
      body = hostedLicense;
    } else if (path === '/auth/state') {
      body = {
        enabled: false,
        mode: 'open',
        user: null,
        hosted_team: true,
        cloud_url: 'https://cloud.engraphis.test/team',
      };
    } else if (path === '/sync/status') {
      body = { available: syncRunStatus !== null, last: syncLast };
    } else if (path === '/sync/run' && syncRunStatus !== null) {
      status = syncRunStatus;
      syncLast = {
        at: Date.now() / 1000,
        attempted: 1,
        succeeded: 0,
        exported: 0,
        added: 0,
        errors: [{ status: syncRunStatus }],
      };
      body = {
        detail: {
          error: syncRunStatus === 402
            ? 'Cloud Sync entitlement is inactive (upgrade or renew required)'
            : 'cloud relay synchronization failed',
          upgrade_url: 'https://cloud.engraphis.test/pro',
        },
      };
    } else if (path === '/llm/status') {
      body = {
        configured: false,
        key_set: false,
        provider: 'openai',
        model: 'gpt-4o-mini',
        extractor: 'passthrough',
        extractor_enabled: false,
        default_models: { openai: 'gpt-4o-mini' },
        env_snippet: '',
      };
    } else if (path === '/analytics' || path === '/automation') {
      status = cloudStatus;
      body = {
        detail: {
          error: cloudStatus === 409
            ? 'managed cloud operation failed'
            : cloudStatus === 401
            ? 'Connect this installation to Engraphis Cloud.'
            : cloudStatus === 402
              ? 'A hosted Pro or Team entitlement is required.'
              : 'This capability is available through Engraphis Cloud.',
          ...(cloudStatus === 409 ? { code: 'consent_required' } : {}),
        },
      };
    }

    await route.fulfill({
      status,
      contentType: 'application/json',
      body: JSON.stringify(body),
    });
  });

  return calls;
}

test('Cloud Sync denial returns an unlicensed installation to the hosted upgrade CTA', async ({ page }) => {
  const errors = recordBrowserErrors(page);
  const calls = await mockLocalClient(page, 402, 402);
  await page.goto('/');
  await openView(page, 'settings');

  await expect(page.getByRole('button', { name: 'Sync now' })).toBeVisible();
  await page.getByRole('button', { name: 'Sync now' }).click();

  const sync = page.locator('#sync-body');
  await expect(sync).toContainText('Cloud Sync runs in Engraphis Pro Cloud');
  await expect(sync.getByRole('link', { name: 'Start hosted Pro trial' }))
    .toHaveAttribute('href', 'https://cloud.engraphis.test/pro?plan=pro&trial=pro');
  await expect(calls.some(call => call.path === '/sync/run' && call.method === 'POST')).toBe(true);

  await page.reload();
  await openView(page, 'settings');
  await expect(page.locator('#sync-body')).toContainText('Cloud Sync runs in Engraphis Pro Cloud');
  await expect(page.getByRole('button', { name: 'Sync now' })).toHaveCount(0);
  expect(errors).toEqual([]);
});

function recordBrowserErrors(page) {
  const errors = [];
  page.on('console', message => {
    if (message.type() === 'error') {
      const location = message.location();
      const expectedCloudDenial = /\/api\/(analytics|automation)/.test(location.url || '')
        && /status of (401|402|409|501)/.test(message.text());
      const expectedSyncDenial = /\/api\/sync\/run/.test(location.url || '')
        && /status of (401|402|403)/.test(message.text());
      if (expectedCloudDenial || expectedSyncDenial) return;
      errors.push(message.text() + (location.url
        ? ` @ ${location.url}:${location.lineNumber}`
        : ''));
    }
  });
  page.on('pageerror', error => errors.push(error.message));
  return errors;
}

async function openView(page, name) {
  await page.locator(`.nav-item[data-view="${name}"]`).click();
  await expect(page.locator(`#view-${name}`)).toHaveClass(/\bactive\b/);
}

test('local dashboard exposes hosted Pro and Team CTAs without local commercial controls', async ({ page }) => {
  const errors = recordBrowserErrors(page);
  const calls = await mockLocalClient(page);
  const response = await page.goto('/');

  const csp = response.headers()['content-security-policy'];
  expect(csp).toBeTruthy();
  expect(csp).not.toContain("'unsafe-inline'");
  await expect(page.getByLabel('Open hosted plan settings')).toHaveText('LOCAL');

  await openView(page, 'settings');
  const licensePanel = page.locator('.settings-license-panel');
  await expect(licensePanel.getByText('LOCAL CORE', { exact: true })).toBeVisible();
  await expect(licensePanel.getByRole('button', { name: 'Start hosted Pro trial' })).toBeVisible();
  await expect(licensePanel.getByRole('button', { name: 'Start hosted Team trial' })).toBeVisible();
  await expect(licensePanel).toContainText(
    'The email-confirmed, no-card trial lasts exactly 3 active days; '
      + 'local-only write grace is separate, capped at 24 hours, and never extends cloud access.',
  );

  await openView(page, 'team');
  const team = page.locator('#team-body');
  await expect(team.getByText('Engraphis Team Cloud', { exact: false })).toBeVisible();
  await expect(team.getByRole('link', { name: 'Start hosted Team trial' }))
    .toHaveAttribute('href', 'https://cloud.engraphis.test/team?plan=team&trial=team');
  await expect(team.getByRole('link', { name: 'Open Team Cloud' }))
    .toHaveAttribute('href', 'https://cloud.engraphis.test/team?plan=team');
  await expect(team).toContainText('exactly 3 active days');
  await expect(team).toContainText(
    'A separate local-only write grace is capped at 24 hours and never extends Team or other cloud access.',
  );

  for (const selector of ['#auth-overlay', '#session-action', '#lic-key']) {
    await expect(page.locator(selector)).toHaveCount(0);
  }
  for (const removedLabel of [
    'Create admin account',
    'Sign in',
    'Accept team invitation',
    'Activate license',
  ]) {
    await expect(page.getByText(removedLabel, { exact: true })).toHaveCount(0);
  }
  expect(calls.some(call => [
    '/auth/setup',
    '/auth/login',
    '/auth/invitations/accept',
    '/license/activate',
  ].includes(call.path))).toBe(false);

  const scan = await new AxeBuilder({ page })
    .include('#view-team')
    .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'])
    .analyze();
  expect(scan.violations).toEqual([]);
  expect(errors).toEqual([]);
});

for (const cloudStatus of [401, 402, 501]) {
  test(`Analytics and Automation defer to cloud proxy status ${cloudStatus}`, async ({ page }) => {
    const errors = recordBrowserErrors(page);
    const calls = await mockLocalClient(page, cloudStatus);
    await page.goto('/');
    await expect(page.getByLabel('Open hosted plan settings')).toHaveText('LOCAL');

    const analyticsBefore = calls.filter(call => call.path === '/analytics').length;
    await openView(page, 'analytics');
    await expect.poll(
      () => calls.filter(call => call.path === '/analytics').length,
    ).toBeGreaterThan(analyticsBefore);
    const analytics = page.locator('#analytics-body');
    await expect(analytics).toContainText('Analytics runs in Engraphis Pro Cloud');
    await expect(analytics).toContainText('exactly 3 active days');
    await expect(analytics).toContainText(
      'Local-only write grace is separate, capped at 24 hours, and never extends cloud access.',
    );
    await expect(analytics.getByRole('link', { name: 'Start hosted Pro trial' }))
      .toHaveAttribute('href', 'https://cloud.engraphis.test/pro?plan=pro&trial=pro');
    await expect(analytics.getByRole('link', { name: 'View Pro plans' }))
      .toHaveAttribute('href', 'https://cloud.engraphis.test/pro?plan=pro');
    await expect(page.locator('#an-lock')).toHaveText('PRO');

    const automationBefore = calls.filter(call => call.path === '/automation').length;
    await openView(page, 'automation');
    await expect.poll(
      () => calls.filter(call => call.path === '/automation').length,
    ).toBeGreaterThan(automationBefore);
    const automation = page.locator('#automation-body');
    await expect(automation).toContainText(
      'Automation, Auto Consolidation, and Auto Dreaming runs in Engraphis Pro Cloud',
    );
    await expect(automation).toContainText('exactly 3 active days');
    await expect(automation).toContainText(
      'Local-only write grace is separate, capped at 24 hours, and never extends cloud access.',
    );
    await expect(automation.getByRole('link', { name: 'Start hosted Pro trial' }))
      .toHaveAttribute('href', 'https://cloud.engraphis.test/pro?plan=pro&trial=pro');
    await expect(automation.getByRole('link', { name: 'View Pro plans' }))
      .toHaveAttribute('href', 'https://cloud.engraphis.test/pro?plan=pro');
    await expect(page.locator('#au-lock')).toHaveText('PRO');

    expect(calls.some(call => call.path === '/analytics' && call.method === 'GET')).toBe(true);
    expect(calls.some(call => call.path === '/automation' && call.method === 'GET')).toBe(true);
    expect(errors).toEqual([]);
  });
}

test('Analytics explains the local managed-compute consent step', async ({ page }) => {
  const errors = recordBrowserErrors(page);
  await mockLocalClient(page, 409);
  await page.goto('/');
  await openView(page, 'analytics');

  const analytics = page.locator('#analytics-body');
  await expect(analytics).toContainText('needs your explicit permission');
  await expect(analytics).toContainText('ENGRAPHIS_MANAGED_COMPUTE_CONSENT=1');
  await expect(analytics).toContainText('restart Engraphis');
  await expect(page.locator('#an-lock')).toHaveText('CLOUD');
  expect(errors).toEqual([]);
});
