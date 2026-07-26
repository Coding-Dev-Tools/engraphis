const { test, expect } = require('@playwright/test');
const AxeBuilder = require('@axe-core/playwright').default;

// The dashboard's entitlement vocabulary, mirrored from v2_api._FEATURE_LABELS. The
// license panel renders one row per key and ticks the ones `features` contains, so a
// missing key here would silently stop a paying customer's capability being asserted on.
const knownFeatures = {
  analytics: 'Analytics',
  automation: 'Automation',
  consolidation: 'Auto Consolidation',
  dreaming: 'Auto Dreaming',
  sync: 'Cloud Sync',
  team: 'Team administration',
};

const proFeatures = ['analytics', 'automation', 'consolidation', 'dreaming', 'sync'];
const teamFeatures = [...proFeatures, 'team'];

// Mirrored from v2_api.get_license(). Every key the dashboard branches on is produced
// here the way the route produces it, so a fixture cannot drift into a shape the server
// can never send — `access_state` and `trial.available` in particular are *derived*
// server-side (v2_api._access_state / the plan_source gate) and are what decide the badge,
// the panel copy, and whether any surface offers a trial at all.
function licenseFor(plan, features, overrides = {}) {
  const paid = plan === 'pro' || plan === 'team';
  return {
    plan,
    features,
    known_features: knownFeatures,
    cloud_managed: true,
    cloud_access_active: paid,
    access_state: paid ? 'active' : 'inactive',
    entitlement_status: paid ? 'active' : '',
    plan_source: paid ? 'session' : 'local',
    plan_checked_at: 0,
    is_trial: false,
    trial_seconds: 259_200,
    grace_seconds: 86_400,
    grace_scope: 'existing authenticated local workspace writes only',
    pro_upgrade_url: 'https://cloud.engraphis.test/pro',
    team_upgrade_url: 'https://cloud.engraphis.test/team',
    upgrade_url: 'https://cloud.engraphis.test/pro',
    // Plan-neutral by construction: `upgrade_url` above is what licensing.upgrade_url()
    // returns, and that resolves plan="pro". Only `account_url` is safe for the portal.
    account_url: 'https://cloud.engraphis.test/account',
    // The control plane refuses a second trial for any organization that already holds an
    // entitlement, so a connected customer is never offered one — only an installation
    // that belongs to no organization is.
    trial: { used: false, active: false, ends_at: 0, available: !paid, trial_days: 3 },
    ...overrides,
  };
}

// A FREE customer. Every test below used to run against this one payload, which is why a
// paying customer being shown a lock badge survived all the way to launch day.
const hostedLicense = licenseFor('local', []);
const proLicense = licenseFor('pro', proFeatures);
const teamLicense = licenseFor('team', teamFeatures);
// A Team subscription whose billing has lapsed: the plan is still Team, but the control
// plane has withdrawn every feature and named why.
const lapsedTeamLicense = licenseFor('team', [], {
  cloud_access_active: false,
  access_state: 'lapsed',
  entitlement_status: 'past_due',
});

async function mockLocalClient(
  page,
  cloudStatus = 402,
  syncRunStatus = null,
  automationPostStatus = null,
  license = hostedLicense,
) {
  const calls = [];
  let syncLast = null;
  let activeSyncRunStatus = syncRunStatus;
  calls.setSyncRunStatus = status => { activeSyncRunStatus = status; };

  await page.route('**/api/**', async route => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname.replace(/^\/api/, '');
    calls.push({ path, method: request.method(), query: url.search });

    let status = 200;
    let body = {};

    if (path === '/bootstrap') {
      body = {
        license,
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
      body = license;
    } else if (path === '/auth/state') {
      body = {
        enabled: false,
        mode: 'open',
        user: null,
        hosted_team: true,
        cloud_url: 'https://cloud.engraphis.test/team',
      };
    } else if (path === '/sync/status') {
      body = { available: activeSyncRunStatus !== null, last: syncLast };
    } else if (path === '/sync/run' && activeSyncRunStatus !== null) {
      status = activeSyncRunStatus;
      if (status === 200) {
        syncLast = {
          at: Date.now() / 1000,
          attempted: 1,
          succeeded: 1,
          exported: 0,
          added: 0,
          errors: [],
        };
        body = { ok: true, summary: syncLast };
      } else {
        syncLast = {
          at: Date.now() / 1000,
          attempted: 1,
          succeeded: 0,
          exported: 0,
          added: 0,
          errors: [{ status: activeSyncRunStatus }],
        };
        body = {
          detail: {
            error: activeSyncRunStatus === 402
              ? 'Cloud Sync entitlement is inactive (upgrade or renew required)'
              : 'cloud relay synchronization failed',
            upgrade_url: 'https://cloud.engraphis.test/pro',
          },
        };
      }
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
    } else if (
      path === '/automation'
      && request.method() === 'POST'
      && automationPostStatus !== null
    ) {
      status = automationPostStatus;
      body = {
        detail: {
          error: 'managed cloud operation failed',
          code: 'consent_required',
        },
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
  await expect(sync).toContainText('Unlock Cloud Sync and more');
  await expect(sync.getByRole('link', { name: 'Start hosted Pro trial' }))
    .toHaveAttribute('href', 'https://cloud.engraphis.test/pro?plan=pro&trial=pro');
  await expect(calls.some(call => call.path === '/sync/run' && call.method === 'POST')).toBe(true);

  await page.reload();
  await openView(page, 'settings');
  await expect(page.locator('#sync-body')).toContainText('Unlock Cloud Sync and more');
  await expect(page.getByRole('button', { name: 'Try Cloud Sync again' })).toBeVisible();

  calls.setSyncRunStatus(200);
  await page.getByRole('button', { name: 'Try Cloud Sync again' }).click();
  await expect(page.locator('#sync-body')).toContainText('Hosted relay');
  await expect(page.locator('#sync-body')).toContainText('CONNECTED');
  await expect(page.getByRole('button', { name: 'Sync now' })).toBeVisible();
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

// ── paying customers ─────────────────────────────────────────────────────────────────
// Until these existed, every test in this file ran against one FREE payload, so the whole
// end-to-end suite never once rendered a customer who had paid. That is exactly how a
// paying customer being shown a lock badge on a feature they had bought survived to
// launch day. `#nav-*-lock` is the element that carried the wrong badge, so it is what
// these assert on directly.
const navLocks = {
  analytics: '#nav-analytics-lock',
  automation: '#nav-automation-lock',
  team: '#nav-team-lock',
};

test('a free installation is locked out of every hosted capability', async ({ page }) => {
  const errors = recordBrowserErrors(page);
  await mockLocalClient(page);
  await page.goto('/');

  // The baseline the paid cases below are the counterpart to: all three locks drawn.
  await expect(page.getByLabel('Open hosted plan settings')).toHaveText('LOCAL');
  await expect(page.locator(navLocks.analytics)).toHaveText('PRO');
  await expect(page.locator(navLocks.automation)).toHaveText('PRO');
  await expect(page.locator(navLocks.team)).toHaveText('TEAM');
  expect(errors).toEqual([]);
});

test('a paying Team customer sees TEAM with Team administration unlocked', async ({ page }) => {
  const errors = recordBrowserErrors(page);
  await mockLocalClient(page, 200, null, null, teamLicense);
  await page.goto('/');

  // The badge settles before the locks: dashboard.js sets both inside loadLicense(),
  // so asserting it first means the empty locks below cannot be read pre-render.
  await expect(page.getByLabel('Open hosted plan settings')).toHaveText('TEAM');
  for (const [feature, selector] of Object.entries(navLocks)) {
    await expect(page.locator(selector), `${feature} is still locked for a Team customer`)
      .toHaveText('');
  }

  await openView(page, 'settings');
  const licensePanel = page.locator('.settings-license-panel');
  await expect(licensePanel.getByText('TEAM', { exact: true })).toBeVisible();
  // Every capability Team grants is ticked, including the two the server folds into
  // `automation` and this client names separately.
  for (const label of Object.values(knownFeatures)) {
    await expect(licensePanel).toContainText(`✓ ${label}`);
  }
  await expect(licensePanel).not.toContainText('○');
  // A paying customer is offered the account portal, never another trial.
  await expect(licensePanel.getByRole('link', { name: 'Open Team Cloud' }))
    .toHaveAttribute('href', 'https://cloud.engraphis.test/team?plan=team');
  await expect(licensePanel.getByRole('button', { name: 'Start hosted Team trial' }))
    .toHaveCount(0);
  await expect(licensePanel.getByRole('button', { name: 'Start hosted Pro trial' }))
    .toHaveCount(0);
  expect(errors).toEqual([]);
});

test('a paying Pro customer keeps only the Team upsell', async ({ page }) => {
  const errors = recordBrowserErrors(page);
  await mockLocalClient(page, 200, null, null, proLicense);
  await page.goto('/');

  await expect(page.getByLabel('Open hosted plan settings')).toHaveText('PRO');
  await expect(page.locator(navLocks.analytics)).toHaveText('');
  await expect(page.locator(navLocks.automation)).toHaveText('');
  await expect(page.locator(navLocks.team)).toHaveText('TEAM');

  await openView(page, 'settings');
  const licensePanel = page.locator('.settings-license-panel');
  await expect(licensePanel.getByText('PRO', { exact: true })).toBeVisible();
  await expect(licensePanel).toContainText(`✓ ${knownFeatures.analytics}`);
  await expect(licensePanel).toContainText(`✓ ${knownFeatures.consolidation}`);
  await expect(licensePanel).toContainText(`○ ${knownFeatures.team}`);
  expect(errors).toEqual([]);
});

test('a lapsed Team subscription is sent to billing, not to a spent trial', async ({ page }) => {
  const errors = recordBrowserErrors(page);
  await mockLocalClient(page, 402, null, null, lapsedTeamLicense);
  await page.goto('/');

  // The badge says the plan AND that it is no longer live: a bare `TEAM` over rows of
  // locks was the defect. The locks come back because the control plane withdrew the
  // features.
  await expect(page.getByLabel('Open hosted plan settings')).toHaveText('TEAM INACTIVE');
  await expect(page.locator(navLocks.team)).toHaveText('TEAM');
  await expect(page.locator(navLocks.analytics)).toHaveText('PRO');

  await openView(page, 'settings');
  const licensePanel = page.locator('.settings-license-panel');
  // Why it is locked, in the customer's own terms, including the status the cloud named.
  await expect(licensePanel).toContainText('subscription is no longer active');
  await expect(licensePanel).toContainText('The last payment did not go through');
  await expect(licensePanel).toContainText('Your local memories are unaffected');
  // Renewal goes to the plan they actually hold — never the Pro checkout — and the portal
  // is the plan-neutral hosted entry point rather than a second checkout.
  await expect(licensePanel.getByRole('link', { name: 'Update billing' }))
    .toHaveAttribute('href', 'https://cloud.engraphis.test/team?plan=team');
  await expect(licensePanel.getByRole('link', { name: 'Open account portal' }))
    .toHaveAttribute('href', 'https://cloud.engraphis.test/account');
  // A lapsed subscription is a billing problem, not an unspent trial.
  await expect(licensePanel.getByRole('button', { name: 'Start hosted Team trial' }))
    .toHaveCount(0);
  await expect(licensePanel.getByRole('button', { name: 'Start hosted Pro trial' }))
    .toHaveCount(0);
  expect(errors).toEqual([]);
});

test('a spent trial says so, and is never offered another one', async ({ page }) => {
  const errors = recordBrowserErrors(page);
  // The state that was unreachable before this client read `access_state`: the trial ran
  // out, so the plan name is still Pro but nothing it grants is live.
  await mockLocalClient(page, 402, null, null, licenseFor('pro', [], {
    cloud_access_active: false,
    access_state: 'trial_expired',
    entitlement_status: 'expired',
    is_trial: true,
    trial: { used: true, active: false, ends_at: 1751068800, available: false, trial_days: 3 },
  }));
  await page.goto('/');

  await expect(page.getByLabel('Open hosted plan settings')).toHaveText('TRIAL ENDED');

  await openView(page, 'settings');
  const licensePanel = page.locator('.settings-license-panel');
  await expect(licensePanel).toContainText('Your free trial has ended on 2025-06-28');
  await expect(licensePanel).toContainText('still in your local database');
  await expect(licensePanel).toContainText('cannot be started again');
  // Buyable, not trialable.
  await expect(licensePanel.getByRole('link', { name: 'Subscribe to Pro' }))
    .toHaveAttribute('href', 'https://cloud.engraphis.test/pro?plan=pro');
  await expect(licensePanel.getByRole('link', { name: 'Subscribe to Team' }))
    .toHaveAttribute('href', 'https://cloud.engraphis.test/team?plan=team');
  await expect(licensePanel.getByRole('button', { name: 'Start hosted Pro trial' }))
    .toHaveCount(0);
  expect(errors).toEqual([]);
});

for (const cloudStatus of [402, 501]) {
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
    await expect(analytics).toContainText('Unlock Analytics and more');
    await expect(analytics).toContainText('exactly 3 active days');
    await expect(analytics).toContainText('$10/month or $100/year');
    await expect(analytics).toContainText('Hosted Cloud Sync across your installations');
    await expect(analytics.getByRole('link', { name: 'Start hosted Pro trial' }))
      .toHaveAttribute('href', 'https://cloud.engraphis.test/pro?plan=pro&trial=pro');
    await expect(analytics.getByRole('link', { name: 'Purchase Pro license' }))
      .toHaveAttribute('href', 'https://cloud.engraphis.test/pro?plan=pro');
    await expect(page.locator('#an-lock')).toHaveText('PRO');

    const automationBefore = calls.filter(call => call.path === '/automation').length;
    await openView(page, 'automation');
    await expect.poll(
      () => calls.filter(call => call.path === '/automation').length,
    ).toBeGreaterThan(automationBefore);
    const automation = page.locator('#automation-body');
    await expect(automation).toContainText(
      'Unlock Automation, Auto Consolidation, and Auto Dreaming and more',
    );
    await expect(automation).toContainText('exactly 3 active days');
    await expect(automation).toContainText('$10/month or $100/year');
    await expect(automation).toContainText('Auto Dreaming with reviewable managed proposals');
    await expect(automation.getByRole('link', { name: 'Start hosted Pro trial' }))
      .toHaveAttribute('href', 'https://cloud.engraphis.test/pro?plan=pro&trial=pro');
    await expect(automation.getByRole('link', { name: 'Purchase Pro license' }))
      .toHaveAttribute('href', 'https://cloud.engraphis.test/pro?plan=pro');
    await expect(page.locator('#au-lock')).toHaveText('PRO');

    expect(calls.some(call => call.path === '/analytics' && call.method === 'GET')).toBe(true);
    expect(calls.some(call => call.path === '/automation' && call.method === 'GET')).toBe(true);
    expect(errors).toEqual([]);
  });
}

// 401 is a credential problem, not a billing one: the cloud maps it to "the cloud session
// expired or was revoked; connect again". Selling Pro to a customer who already owns it --
// and who only needs to reconnect -- is the regression this guards.
test('An expired cloud session asks the customer to reconnect, not to buy', async ({ page }) => {
  const errors = recordBrowserErrors(page);
  await mockLocalClient(page, 401);
  await page.goto('/');

  await openView(page, 'analytics');
  const analytics = page.locator('#analytics-body');
  await expect(analytics).not.toContainText('Unlock Analytics and more');
  await expect(analytics.getByRole('link', { name: 'Purchase Pro license' })).toHaveCount(0);

  await openView(page, 'automation');
  const automation = page.locator('#automation-body');
  await expect(automation).not.toContainText('Unlock Automation');
  await expect(automation.getByRole('link', { name: 'Purchase Pro license' })).toHaveCount(0);

  expect(errors).toEqual([]);
});

// Consent travels with the cloud account: connecting an installation accepts the terms
// covering managed compute. A 409 consent_required therefore means "this installation is
// not connected", never "go and hand-edit an environment variable", and it must never be
// mistaken for an unpaid invoice and answered with the Pro purchase panel.
test('Analytics explains that managed compute follows the cloud connection', async ({ page }) => {
  const errors = recordBrowserErrors(page);
  await mockLocalClient(page, 409);
  await page.goto('/');
  await openView(page, 'analytics');

  const analytics = page.locator('#analytics-body');
  await expect(analytics).toContainText('managed compute is turned off for this installation');
  await expect(analytics).toContainText('Connect this installation to Engraphis Cloud');
  await expect(analytics).not.toContainText('ENGRAPHIS_MANAGED_COMPUTE_CONSENT');
  await expect(analytics).not.toContainText('Purchase Pro license');
  await expect(page.locator('#an-lock')).toHaveText('CLOUD');
  expect(errors).toEqual([]);
});

test('Automation policy save explains that managed compute follows the cloud connection', async ({ page }) => {
  const errors = recordBrowserErrors(page);
  await mockLocalClient(page, 200, null, 409);
  await page.goto('/');
  await openView(page, 'automation');

  await page.locator('#au-enabled').check();
  await page.getByRole('button', { name: 'Save hosted policy' }).click();
  const result = page.locator('#au-result');
  await expect(result).toContainText('managed compute is turned off for this installation');
  await expect(result).toContainText('Connect this installation to Engraphis Cloud');
  await expect(result).not.toContainText('ENGRAPHIS_MANAGED_COMPUTE_CONSENT');
  await expect(result).not.toContainText('Purchase Pro license');
  expect(errors).toEqual([]);
});
