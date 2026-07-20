const { test, expect } = require('@playwright/test');
const AxeBuilder = require('@axe-core/playwright').default;

const admin = { id: 'usr_admin', email: 'admin@example.com', name: 'Admin', role: 'admin' };
const teamLicense = {
  plan: 'team', is_trial: true, seats: 5, features: ['team', 'cloud_sync', 'analytics'],
  trial: { active: true, days_left: 3, trial_days: 3 }, known_features: {},
  pro_upgrade_url: 'https://engraphis.com/product.html#pricing',
  team_upgrade_url: 'https://engraphis.com/product.html#pricing',
};

async function mockApi(page, mode) {
  let setupDone = false;
  await page.route('**/api/**', async route => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname.replace(/^\/api/, '');
    let status = 200;
    let body = {};

    if (path === '/health') body = { status: 'ok' };
    else if (mode === 'setup' && path === '/auth/state') {
      body = setupDone
        ? { enabled: true, needs_setup: false, licensed: true, user: admin }
        : { enabled: true, needs_setup: true, licensed: true, user: null };
    } else if (mode === 'setup' && path === '/auth/setup' && request.method() === 'POST') {
      setupDone = true;
      body = { user: admin };
    } else if (mode === 'setup' && path === '/bootstrap') {
      body = { license: teamLicense, workspaces: [], embedder: { semantic: true } };
    } else if (mode === 'setup' && path === '/license') body = teamLicense;
    else if (mode === 'invitation' && path === '/auth/invitations/accept') {
      body = { user: { ...admin, email: 'member@example.com', role: 'member' } };
    } else if (mode === 'invitation' && path === '/auth/state') {
      body = { enabled: true, needs_setup: false, licensed: true, user: null };
    } else if (mode === 'invitation' && path === '/bootstrap') {
      body = { license: teamLicense, workspaces: [], embedder: { semantic: true } };
    }
    else if (mode === 'onboarding' && path === '/auth/state') {
      body = { enabled: false, needs_setup: false, user: null };
    } else if (mode === 'onboarding' && path === '/bootstrap') {
      body = { license: { plan: 'free', features: [] }, workspaces: [],
               embedder: { semantic: true } };
    } else if (mode === 'onboarding' && path === '/license') {
      body = { plan: 'free', features: [], trial: { used: false, trial_days: 3 } };
    } else if (mode === 'onboarding' && path === '/license/trials'
               && request.method() === 'POST') {
      body = { claim_id: 'claim_e2e', status: 'pending' };
    } else if (mode === 'onboarding' && path === '/license/trials/claim_e2e') {
      body = { claim_id: 'claim_e2e', status: 'pending', active: false };
    } else if (mode === 'team' && path === '/auth/state') {
      body = { enabled: true, needs_setup: false, licensed: true, user: admin };
    } else if (mode === 'team' && path === '/bootstrap') {
      body = { license: teamLicense, workspaces: [{ name: 'default', memories: 0 }],
               embedder: { semantic: true } };
    } else if (mode === 'team' && path === '/license') body = teamLicense;
    else if (mode === 'team' && path === '/auth/users') body = { users: [admin] };
    else if (mode === 'team' && path === '/auth/overview') {
      body = { active_users: 1, pending_invitations: 1, seats_used: 2, seats_total: 5 };
    } else if (mode === 'team' && path === '/auth/invitations') {
      body = { invitations: [{ id: 'inv_1', email: 'member@example.com', name: 'Member',
        role: 'member', expires_at: Date.now() / 1000 + 3600, delivery_state: 'failed' }] };
    } else if (mode === 'team' && path.startsWith('/auth/audit')) body = { events: [], total: 0 };
    else if (mode === 'team' && path === '/folders') body = { folders: [] };
    else if (mode === 'team' && path === '/overview') body = {};
    else if (mode === 'team' && path === '/analytics') body = {};
    else if (mode === 'team' && path === '/workspaces') body = { workspaces: [] };

    await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
  });
}

function recordBrowserErrors(page) {
  const errors = [];
  page.on('console', message => {
    if (message.type() === 'error') {
      const location = message.location();
      errors.push(message.text() + (location.url ? ` @ ${location.url}:${location.lineNumber}` : ''));
    }
  });
  page.on('pageerror', error => errors.push(error.message));
  return errors;
}

test('hosted Team onboarding is scanner-safe, keyboard operable, mobile, and strict-CSP', async ({ page }) => {
  const errors = recordBrowserErrors(page);
  await mockApi(page, 'onboarding');
  await page.setViewportSize({ width: 390, height: 844 });
  await page.emulateMedia({ reducedMotion: 'reduce' });
  const response = await page.goto('/');
  const csp = response.headers()['content-security-policy'];
  expect(csp).toBeTruthy();
  expect(csp).not.toContain("'unsafe-inline'");

  // Wait for boot() to complete before calling showHostedBootstrap
  await page.waitForFunction(() => typeof LIC !== 'undefined' && LIC !== null, { timeout: 10000 });
  await page.evaluate(() => showHostedBootstrap('Complete hosted onboarding to continue.'));
  // Wait for the button to be visible before clicking
  const startTrialBtn = page.getByRole('button', { name: 'Start Team trial' });
  await expect(startTrialBtn).toBeVisible({ timeout: 10000 });
  await startTrialBtn.click();

  const dialog = page.getByRole('dialog', { name: 'Start Team trial' });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByLabel('Email address')).toBeVisible();
  await expect(dialog.getByLabel('Deployment token')).toBeVisible();
  await dialog.getByLabel('Email address').fill('owner@example.com');
  await dialog.getByLabel('Deployment token').fill('playwright-only-deployment-token-1234567890');

  const requestPromise = page.waitForRequest(request =>
    request.url().endsWith('/api/license/trials') && request.method() === 'POST');
  await page.getByRole('button', { name: 'Send confirmation' }).click();
  const trialRequest = await requestPromise;
  expect(trialRequest.postDataJSON()).toMatchObject({ plan: 'team', email: 'owner@example.com' });

  await page.getByRole('button', { name: 'Start Team trial' }).click();
  await expect(dialog).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(dialog).toBeHidden();
  expect(errors).toEqual([]);
});

test('Team administration exposes pending delivery recovery with accessible semantics', async ({ page }) => {
  const errors = recordBrowserErrors(page);
  await mockApi(page, 'team');
  await page.goto('/');
  const teamNavigation = page.getByRole('button', { name: /^Team/ });
  await teamNavigation.focus();
  await page.keyboard.press('Enter');
  await expect(page.getByText('Pending invitations (1)')).toBeVisible();
  await expect(page.getByText('member@example.com')).toBeVisible();
  await expect(page.getByRole('button', { name: /Resend/ })).toBeVisible();

  const scan = await new AxeBuilder({ page })
    .include('#view-team')
    .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'])
    .analyze();
  expect(scan.violations).toEqual([]);
  expect(errors).toEqual([]);
});

test('desktop first-admin setup, login, and purchase paths are labelled and operable', async ({ page }) => {
  const errors = recordBrowserErrors(page);
  await mockApi(page, 'setup');
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto('/');

  const setup = page.getByRole('dialog', { name: 'Create admin account' });
  await expect(setup).toBeVisible();
  await setup.getByLabel('Email').fill('owner@example.com');
  await setup.getByLabel('Name').fill('Owner');
  await setup.getByLabel('Password').fill('recipient-chosen-1');
  await setup.getByLabel(/Deployment token/).fill('d'.repeat(32));
  const setupRequest = page.waitForRequest(request =>
    request.url().endsWith('/api/auth/setup') && request.method() === 'POST');
  await setup.getByRole('button', { name: 'Create admin' }).click();
  const request = await setupRequest;
  expect(request.headers().authorization).toBe(`Bearer ${'d'.repeat(32)}`);
  expect(request.postDataJSON()).toMatchObject({ email: 'owner@example.com', name: 'Owner' });
  await expect(setup).toBeHidden();

  await page.evaluate(() => showAuth({ enabled: true, needs_setup: false, user: null }));
  const login = page.getByRole('dialog', { name: 'Sign in' });
  await expect(login.getByLabel('Email')).toBeVisible();
  await expect(login.getByLabel('Password')).toBeVisible();
  const authScan = await new AxeBuilder({ page })
    .include('#auth-overlay')
    .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'])
    .analyze();
  expect(authScan.violations).toEqual([]);
  await page.keyboard.press('Escape');

  await page.evaluate(license => { selectView('settings'); renderLicense(license); }, teamLicense);
  await expect(page.getByRole('link', { name: /Buy Pro/ })).toHaveAttribute('href', /pricing/);
  await expect(page.getByRole('link', { name: /^Team/ })).toHaveAttribute('href', /pricing/);
  expect(errors).toEqual([]);
});

test('invitation acceptance is screen-reader labelled and posts the one-time token', async ({ page }) => {
  const errors = recordBrowserErrors(page);
  await mockApi(page, 'invitation');
  await page.goto('/#invite_token=invite-once-1234567890');

  const dialog = page.getByRole('dialog', { name: 'Accept team invitation' });
  await expect(dialog).toBeVisible();
  expect(await page.evaluate(() => location.search + location.hash)).not.toContain('invite_token');
  await dialog.getByLabel('Password', { exact: true }).fill('recipient-chosen-1');
  await dialog.getByLabel('Confirm password').fill('recipient-chosen-1');
  const acceptanceRequest = page.waitForRequest(request =>
    request.url().endsWith('/api/auth/invitations/accept') && request.method() === 'POST');
  await dialog.getByRole('button', { name: 'Create account and sign in' }).click();
  const request = await acceptanceRequest;
  expect(request.postDataJSON()).toEqual({
    token: 'invite-once-1234567890', password: 'recipient-chosen-1',
  });
  await expect(dialog).toBeHidden();
  expect(errors).toEqual([]);
});

test('query invitation and reset credentials are ignored and scrubbed', async ({ page }) => {
  await mockApi(page, 'invitation');
  await page.goto('/?invite_token=query-invite-secret&reset_token=query-reset-secret');

  await expect(page.getByRole('dialog', { name: 'Sign in' })).toBeVisible();
  await expect(page.getByRole('dialog', { name: 'Accept team invitation' })).toBeHidden();
  await expect(page.getByRole('dialog', { name: 'Set a new password' })).toBeHidden();
  const locationValue = await page.evaluate(() => location.search + location.hash);
  expect(locationValue).not.toContain('invite_token');
  expect(locationValue).not.toContain('reset_token');
});

test('password reset fragment is scrubbed and posts its one-time token', async ({ page }) => {
  await mockApi(page, 'invitation');
  await page.goto('/#reset_token=reset-once-1234567890');

  const dialog = page.getByRole('dialog', { name: 'Set a new password' });
  await expect(dialog).toBeVisible();
  expect(await page.evaluate(() => location.search + location.hash)).not.toContain('reset_token');
  await dialog.getByLabel('New password').fill('replacement-pass-1');
  const resetRequest = page.waitForRequest(request =>
    request.url().endsWith('/api/auth/reset') && request.method() === 'POST');
  await dialog.getByRole('button', { name: 'Set new password' }).click();
  const request = await resetRequest;
  expect(request.postDataJSON()).toEqual({
    token: 'reset-once-1234567890', password: 'replacement-pass-1',
  });
});
