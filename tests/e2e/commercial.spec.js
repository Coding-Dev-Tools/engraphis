const { test, expect } = require('@playwright/test');
const AxeBuilder = require('@axe-core/playwright').default;
const { answerActionDialog } = require('./action-dialog-helper');

const workspace = 'commercial-e2e';

// The dashboard's entitlement vocabulary, mirrored from v2_api._FEATURE_LABELS. The
// license panel renders one row per key and ticks the ones `features` contains, so a
// missing key here would silently stop a paying customer's capability being asserted on.
const knownFeatures = {
  analytics: 'Analytics',
  automation: 'Automation',
  consolidation: 'Auto Consolidation',
  dreaming: 'Auto Dreaming',
  export: 'Compliance export',
  sync: 'Cloud Sync',
  team: 'Team administration',
};

const proFeatures = ['analytics', 'automation', 'consolidation', 'dreaming', 'export', 'sync'];
const teamFeatures = [...proFeatures, 'team'];

// Mirrored from v2_api.get_license(). Every key the dashboard branches on is produced
// here the way the route produces it, so a fixture cannot drift into a shape the server
// can never send — `access_state` and `trial.available` in particular are *derived*
// server-side (v2_api._access_state / the plan_source gate) and are what decide the
// panel copy and whether any surface offers a trial at all.
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
    trial_seconds: 604_800,
    grace_seconds: 86_400,
    grace_scope: 'private hosted account continuity only; free local core unaffected',
    pro_upgrade_url: 'https://cloud.engraphis.test/pro?plan=pro&interval=monthly#billing',
    team_upgrade_url: 'https://cloud.engraphis.test/team?plan=team&interval=monthly#billing',
    pro_monthly_upgrade_url: 'https://cloud.engraphis.test/pro?plan=pro&interval=monthly#billing',
    pro_annual_upgrade_url: 'https://cloud.engraphis.test/pro?plan=pro&interval=annual#billing',
    team_monthly_upgrade_url: 'https://cloud.engraphis.test/team?plan=team&interval=monthly#billing',
    team_annual_upgrade_url: 'https://cloud.engraphis.test/team?plan=team&interval=annual#billing',
    upgrade_url: 'https://cloud.engraphis.test/pro?plan=pro&interval=monthly#billing',
    // Plan-neutral by construction: `upgrade_url` above is what licensing.upgrade_url()
    // returns, and that resolves plan="pro". Only `account_url` is safe for the portal.
    account_url: 'https://cloud.engraphis.test/account',
    // The control plane refuses a second trial for any organization that already holds an
    // entitlement, so a connected customer is never offered one — only an installation
    // that belongs to no organization is.
    trial: { used: false, active: false, ends_at: 0, available: !paid, trial_days: 7, days_by_plan: { pro: 7, team: 14 } },
    ...overrides,
  };
}

// A FREE customer, and the paying and lapsed customers the panels must never mislead.
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

const account = content => `https://cloud.engraphis.test/account?utm_source=engraphis&utm_medium=product&utm_campaign=pro_conversion&utm_content=${content}`;

async function mockLocalClient(page, {
  cloudStatus = 402,
  cloudErrorCode = null,
  syncRunStatus = null,
  automationPostStatus = null,
  license = hostedLicense,
  bootstrapFailures = 0,
} = {}) {
  const calls = [];
  let remainingBootstrapFailures = bootstrapFailures;

  await page.route('**/api/**', async route => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname.replace(/^\/api/, '');
    calls.push({ path, method: request.method(), query: url.search });

    let status = 200;
    let body = {};
    const failure = (code, error, detail = {}) => {
      status = code;
      body = { detail: { error, ...detail } };
    };

    if (path === '/bootstrap' && remainingBootstrapFailures > 0) {
      remainingBootstrapFailures -= 1;
      status = 503;
      body = { detail: 'temporary bootstrap failure' };
    } else if (path === '/bootstrap') {
      body = {
        license,
        workspaces: [{ name: workspace, memories: 0 }],
        stats: { memories: 0, total_rows: 0, workspaces: 1, sessions: 0 },
        embedder: { semantic: true },
      };
    } else if (path === '/health') {
      body = { status: 'ok' };
    } else if (path === '/stats') {
      body = { memories: 0, total_rows: 0, workspaces: 1, sessions: 0, by_type: {} };
    } else if (path === '/memories') {
      body = { workspace, memories: [], total: 0, next_cursor: null };
    } else if (path === '/proactive') {
      body = { workspace, memories: [], handoff: null };
    } else if (path === '/review-inbox') {
      body = { workspace, items: [], count: 0, has_more: false };
    } else if (path === '/license') {
      body = license;
    } else if (path === '/auth/state') {
      body = {
        enabled: false,
        mode: 'open',
        user: null,
        deployment_mode: 'local',
        hosted_team: true,
        cloud_url: 'https://cloud.engraphis.test/team',
      };
    } else if (path === '/sync/status') {
      body = syncRunStatus === null
        ? { available: false, ready: false, last: null }
        : { available: true, ready: true, has_key: true, key_state: 'ready', has_cloud_session: true, last: null };
    } else if (path === '/sync/run') {
      failure(syncRunStatus || 402, syncRunStatus === 402
        ? 'Cloud Sync entitlement is inactive (upgrade or renew required)'
        : 'cloud relay synchronization failed', { upgrade_url: 'https://cloud.engraphis.test/pro' });
    } else if (path === '/llm/status') {
      body = {
        configured: false,
        key_set: false,
        provider: 'openai',
        model: 'gpt-4o-mini',
        extractor: 'none',
        extractor_enabled: false,
        default_models: { openai: 'gpt-4o-mini' },
        env_snippet: '',
      };
    } else if (path === '/automation' && request.method() === 'POST' && automationPostStatus !== null) {
      failure(automationPostStatus, 'Managed processing requires approval for this workspace.', {
        code: 'consent_required',
      });
    } else if ((path === '/analytics' || path === '/automation') && cloudStatus !== 200) {
      failure(cloudStatus, cloudStatus === 409
        ? 'Managed processing requires approval for this workspace.'
        : cloudStatus === 401
          ? 'Connect this installation to Engraphis Cloud.'
          : cloudStatus === 402
            ? 'A hosted Pro or Team entitlement is required.'
            : 'This capability is available through Engraphis Cloud.',
      cloudStatus === 409 ? { code: 'consent_required' }
        : cloudErrorCode ? { code: cloudErrorCode } : {});
    } else if (path === '/automation') {
      body = {
        enabled: false, cadence_hours: 24, dream: true, dream_min_new: 25,
        dream_idle_minutes: 15, infer: false, workspaces: [workspace], version: 1,
      };
    }

    await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
  });

  return calls;
}

function recordBrowserErrors(page) {
  const errors = [];
  page.on('console', message => {
    if (message.type() !== 'error') return;
    const location = message.location();
    const expected = [
      [/\/api\/(analytics|automation)/, /status of (401|402|409|501)/],
      [/\/api\/sync\/run/, /status of (401|402|403)/],
      [/\/api\/bootstrap/, /status of 503/],
    ].some(([url, text]) => url.test(location.url || '') && text.test(message.text()));
    // axe-core probes colour and visibility with temporary inline styles, which the
    // product CSP correctly refuses; those reports are the analyzer's, not the page's.
    const axeProbe = message.text().startsWith('Applying inline style violates');
    if (expected || axeProbe) return;
    errors.push(message.text() + (location.url ? ` @ ${location.url}:${location.lineNumber}` : ''));
  });
  page.on('pageerror', error => errors.push(error.message));
  return errors;
}

async function openDashboard(page) {
  const response = await page.goto('/');
  await expect(page.locator('#workspace-select')).toHaveValue(workspace);
  return response;
}

async function openSettingsTab(page, name) {
  await page.locator('.nav-item[data-view="manage"]').click();
  const tab = page.getByRole('tab', { name, exact: true });
  await tab.click();
  await expect(tab).toHaveAttribute('aria-selected', 'true');
}

const trialHref = (plan, content, interval = 'monthly') => `https://cloud.engraphis.test/${plan}?plan=${plan}&interval=${interval}&trial=${plan}&utm_source=engraphis&utm_medium=product&utm_campaign=pro_conversion&utm_content=${content}#billing`;

test('a failed first load keeps the page and Retry now recovers it', async ({ page }) => {
  const errors = recordBrowserErrors(page);
  const calls = await mockLocalClient(page, { bootstrapFailures: 1 });

  await page.goto('/');
  const banner = page.locator('#connection-banner');
  await expect(banner).toBeVisible();
  await expect(banner).toContainText('temporary bootstrap failure');
  await expect(page.locator('#connection-status')).toContainText('unavailable');

  await page.locator('#connection-retry').click();
  await expect(banner).toBeHidden();
  await expect(page.locator('#workspace-select')).toHaveValue(workspace);
  await expect(page.locator('#connection-status')).toContainText('Local engine connected');
  expect(calls.filter(call => call.path === '/bootstrap')).toHaveLength(2);
  expect(errors).toEqual([]);
});

test('the local dashboard ships a strict CSP and no local account or license surfaces', async ({ page }) => {
  const errors = recordBrowserErrors(page);
  const calls = await mockLocalClient(page);
  const response = await openDashboard(page);

  const csp = response.headers()['content-security-policy'];
  expect(csp).toBeTruthy();
  expect(csp).not.toContain("'unsafe-inline'");
  for (const selector of ['#auth-overlay', '#session-action', '#lic-key']) {
    await expect(page.locator(selector)).toHaveCount(0);
  }
  for (const removedLabel of ['Create admin account', 'Sign in', 'Accept team invitation', 'Activate license']) {
    await expect(page.getByText(removedLabel, { exact: true })).toHaveCount(0);
  }

  await openSettingsTab(page, 'Team');
  await expect(page.locator('#team-result')).toContainText('Team is a hosted plan.');
  await expect(page.locator('#team-result')).toContainText('Local · single user');
  await expect(page.locator('#team-cloud-cta')).toHaveText('Start 14-day Team trial');
  await expect(page.locator('#team-cloud-cta')).toHaveAttribute('href', trialHref('team', 'team'));
  expect(calls.some(call => [
    '/auth/setup', '/auth/login', '/auth/invitations/accept', '/license/activate',
  ].includes(call.path))).toBe(false);

  const scan = await new AxeBuilder({ page })
    .include('#manage-team-panel')
    .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'])
    .analyze();
  expect(scan.violations).toEqual([]);
  expect(errors).toEqual([]);
});

test('a free installation is offered the trial wherever it can start one', async ({ page }) => {
  const errors = recordBrowserErrors(page);
  await mockLocalClient(page);
  await openDashboard(page);

  await expect(page.locator('#sidebar-pro-cta')).toHaveText('Start 7-day Pro trial');
  await openSettingsTab(page, 'Plans & billing');
  await expect(page.locator('#license-status-body')).toContainText('No hosted plan on this installation');
  await expect(page.locator('#license-status-body')).toContainText('Not connected');
  const pro = page.locator('#plan-cards [data-pro-cta="pro"]');
  const team = page.locator('#plan-cards [data-pro-cta="team"]');
  await expect(pro).toHaveText('Start 7-day Pro trial');
  await expect(pro).toHaveAttribute('href', trialHref('pro', 'plans'));
  await expect(team).toHaveText('Start 14-day Team trial');
  await expect(page.locator('.plan-card.featured .plan-benefit-list')).toContainText(
    'Hosted Cloud Sync across your installations',
  );
  expect(errors).toEqual([]);
});

test('a paying Team customer sees Team included and is never offered a trial', async ({ page }) => {
  const errors = recordBrowserErrors(page);
  await mockLocalClient(page, { cloudStatus: 200, license: teamLicense });
  await openDashboard(page);

  await expect(page.locator('#sidebar-pro-copy')).toHaveText('Thank you for supporting Engraphis.');
  await openSettingsTab(page, 'Plans & billing');
  const status = page.locator('#license-status-body');
  await expect(status).toContainText('TEAM');
  // Every capability Team grants is ticked.
  for (const label of Object.values(knownFeatures)) await expect(status).toContainText(`✓ ${label}`);
  await expect(status).not.toContainText('○');
  await expect(page.locator('#plan-cards [data-pro-cta="team"]')).toHaveText('Open Team Cloud');
  await expect(page.locator('#plan-cards [data-pro-cta="team"]')).toHaveAttribute('href', account('plans'));
  await expect(page.locator('#plan-cards [data-pro-cta="pro"]')).toHaveText('Open Engraphis Cloud');
  await expect(page.getByRole('link', { name: /Start \d+-day/ })).toHaveCount(0);

  // The Team tab describes the hosted service; it must not tell a Team customer that their
  // subscription excludes Team.
  await openSettingsTab(page, 'Team');
  await expect(page.locator('#team-result')).toContainText('Your Team subscription is active.');
  await expect(page.locator('#team-result')).not.toContainText('does not include');
  await expect(page.locator('#team-cloud-cta')).toHaveText('Open Team Cloud');
  expect(errors).toEqual([]);
});

test('a paying Pro customer keeps only the Team upsell', async ({ page }) => {
  const errors = recordBrowserErrors(page);
  await mockLocalClient(page, { cloudStatus: 200, license: proLicense });
  await openDashboard(page);

  await openSettingsTab(page, 'Plans & billing');
  const status = page.locator('#license-status-body');
  await expect(status).toContainText(`✓ ${knownFeatures.analytics}`);
  await expect(status).toContainText(`✓ ${knownFeatures.consolidation}`);
  await expect(status).toContainText(`○ ${knownFeatures.team}`);
  await expect(page.locator('#plan-cards [data-pro-cta="pro"]')).toHaveText('Open Engraphis Cloud');
  await expect(page.locator('#plan-cards [data-pro-cta="team"]')).toHaveText('Subscribe to Team');

  await openSettingsTab(page, 'Team');
  await expect(page.locator('#team-result')).toContainText('Team is a hosted plan.');
  await expect(page.locator('#team-cloud-cta')).toHaveText('Subscribe to Team');
  expect(errors).toEqual([]);
});

test('a lapsed Team subscription is sent to billing, not to a spent trial', async ({ page }) => {
  const errors = recordBrowserErrors(page);
  await mockLocalClient(page, { license: lapsedTeamLicense });
  await openDashboard(page);

  await openSettingsTab(page, 'Plans & billing');
  const status = page.locator('#license-status-body');
  // Why it is locked, in the customer's own terms, including the status the cloud named.
  await expect(status).toContainText('subscription is no longer active');
  await expect(status).toContainText('The last payment did not go through');
  await expect(status).toContainText('Local memories are unaffected');
  // Renewal goes to the plan-neutral account portal — never a checkout, never a trial.
  for (const plan of ['pro', 'team']) {
    const cta = page.locator(`#plan-cards [data-pro-cta="${plan}"]`);
    await expect(cta).toHaveText('Update billing');
    await expect(cta).toHaveAttribute('href', account('plans'));
  }
  await expect(page.getByRole('link', { name: /Start \d+-day/ })).toHaveCount(0);
  expect(errors).toEqual([]);
});

test('a spent trial says so, and is never offered another one', async ({ page }) => {
  const errors = recordBrowserErrors(page);
  await mockLocalClient(page, {
    license: licenseFor('pro', [], {
      cloud_access_active: false,
      access_state: 'trial_expired',
      entitlement_status: 'expired',
      is_trial: true,
      trial: { used: true, active: false, ends_at: 1751068800, available: false, trial_days: 7 },
    }),
  });
  await openDashboard(page);

  await openSettingsTab(page, 'Plans & billing');
  const status = page.locator('#license-status-body');
  await expect(status).toContainText(/Your free trial has ended on \S+/);
  await expect(status).toContainText('still in your local database');
  await expect(status).toContainText('The trial runs once per account');
  await expect(page.locator('#plan-cards [data-pro-cta="pro"]')).toHaveText('Subscribe to Pro');
  await expect(page.getByRole('link', { name: /Start \d+-day/ })).toHaveCount(0);
  expect(errors).toEqual([]);
});

for (const cloudStatus of [402, 501]) {
  test(`Analytics and Automation route cloud status ${cloudStatus} to plans`, async ({ page }) => {
    const errors = recordBrowserErrors(page);
    const calls = await mockLocalClient(page, { cloudStatus });
    await openDashboard(page);

    await openSettingsTab(page, 'Analytics');
    const analytics = page.locator('#analytics-result');
    await expect(analytics).toContainText('Analytics needs Engraphis Pro');
    await expect(analytics).toContainText('exactly 7 active days');
    await expect(page.locator('#analytics-pro-cta')).toHaveText('Start 7-day Pro trial');
    await expect(page.locator('#analytics-pro-cta')).toHaveAttribute('href', trialHref('pro', 'analytics'));

    await openSettingsTab(page, 'Automation');
    const automation = page.locator('#automation-result');
    await expect(automation).toContainText('Hosted automation needs Engraphis Pro');
    await automation.getByRole('button', { name: 'Compare plans' }).click();
    await expect(page.getByRole('tab', { name: 'Plans & billing' })).toHaveAttribute('aria-selected', 'true');
    await expect(page.locator('#plan-cards [data-pro-cta="pro"]')).toHaveText('Start 7-day Pro trial');

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
  await mockLocalClient(page, { cloudStatus: 401, license: proLicense });
  await openDashboard(page);

  for (const [tab, target] of [['Analytics', '#analytics-result'], ['Automation', '#automation-result']]) {
    await openSettingsTab(page, tab);
    const panel = page.locator(target);
    await expect(panel).toContainText('Reconnect Engraphis Cloud');
    await expect(panel).toContainText('your plan is unchanged');
    await expect(panel.getByRole('link', { name: 'Open Engraphis Cloud' }))
      .toHaveAttribute('href', account('reconnect'));
    await expect(panel.getByRole('button', { name: 'Compare plans' })).toHaveCount(0);
    await expect(panel).not.toContainText('needs Engraphis Pro');
  }
  expect(errors).toEqual([]);
});

test('An unconfigured local install hears how the Cloud trial and approval work', async ({ page }) => {
  const errors = recordBrowserErrors(page);
  await mockLocalClient(page, { cloudStatus: 401, cloudErrorCode: 'cloud_unconfigured' });
  await openDashboard(page);

  for (const [tab, target, feature] of [
    ['Analytics', '#analytics-result', 'Analytics'],
    ['Automation', '#automation-result', 'Hosted automation'],
  ]) {
    await openSettingsTab(page, tab);
    const panel = page.locator(target);
    await expect(panel).toContainText(`${feature} runs in Engraphis Cloud`);
    await expect(panel).toContainText('exactly 7 active days');
    await expect(panel).toContainText('approve each workspace in Settings → Preferences');
    await expect(panel).not.toContainText('ENGRAPHIS_MANAGED_COMPUTE_CONSENT');
    await expect(panel.getByRole('button', { name: 'Compare plans' })).toBeVisible();
  }
  expect(errors).toEqual([]);
});

test('A subscribed customer is sent to workspace approval, never a repurchase prompt', async ({ page }) => {
  const errors = recordBrowserErrors(page);
  await mockLocalClient(page, { cloudStatus: 409, license: proLicense });
  await openDashboard(page);

  await openSettingsTab(page, 'Analytics');
  const analytics = page.locator('#analytics-result');
  await expect(analytics).toContainText('Analytics is waiting for your approval');
  await expect(analytics).toContainText(
    'Readable managed processing is paused until you approve this workspace in Settings → Preferences.',
  );
  await expect(analytics).toContainText('Secret and session-scoped memories stay local.');
  await expect(analytics.getByRole('button', { name: 'Compare plans' })).toHaveCount(0);
  await expect(page.locator('#analytics-pro-cta')).toHaveText('Open Engraphis Cloud');
  await analytics.getByRole('button', { name: 'Review workspace processing' }).click();
  await expect(page.getByRole('tab', { name: 'Preferences' })).toHaveAttribute('aria-selected', 'true');
  await expect(page.locator('#managed-processing-form')).toBeVisible();
  await expect(page.locator('#managed-processing-approval')).not.toBeChecked();
  expect(errors).toEqual([]);
});

test('Saving a hosted policy that needs approval explains the approval step', async ({ page }) => {
  const errors = recordBrowserErrors(page);
  const calls = await mockLocalClient(page, { cloudStatus: 200, automationPostStatus: 409, license: proLicense });
  await openDashboard(page);

  await openSettingsTab(page, 'Automation');
  await page.getByRole('checkbox', { name: 'Enable hosted maintenance' }).check();
  await page.getByRole('button', { name: 'Save & send policy to Cloud' }).click();
  await answerActionDialog(page, { expectText: ['normal and sensitive memory content'] });
  const automation = page.locator('#automation-result');
  await expect(automation).toContainText('Hosted automation is waiting for your approval');
  await expect(automation.getByRole('button', { name: 'Review workspace processing' })).toBeVisible();
  await expect(automation).not.toContainText('ENGRAPHIS_MANAGED_COMPUTE_CONSENT');
  expect(calls.filter(call => call.path === '/automation' && call.method === 'POST')).toHaveLength(1);
  expect(errors).toEqual([]);
});

test('A refused Cloud Sync run explains the cause and the plan that unlocks it', async ({ page }) => {
  const errors = recordBrowserErrors(page);
  const calls = await mockLocalClient(page, { syncRunStatus: 402 });
  await openDashboard(page);

  await openSettingsTab(page, 'Cloud Sync');
  const sync = page.locator('#sync-result');
  await expect(sync).toContainText('Cloud Sync encrypts eligible shared-workspace changes end-to-end');
  await sync.getByRole('button', { name: 'Sync now' }).click();
  await expect(sync).toContainText('Cloud Sync failed: Cloud Sync entitlement is inactive');
  await expect(sync).toContainText('Cloud Sync needs Engraphis Pro');
  await expect(sync.getByRole('button', { name: 'Compare plans' })).toBeVisible();
  expect(calls.some(call => call.path === '/sync/run' && call.method === 'POST')).toBe(true);
  expect(errors).toEqual([]);
});
