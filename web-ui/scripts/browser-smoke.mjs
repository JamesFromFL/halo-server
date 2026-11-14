/** Real companion smoke test. Never targets production or supplies Ring credentials. */
import { chromium } from '@playwright/test';
import assert from 'node:assert/strict';
import { mkdir, writeFile } from 'node:fs/promises';
import { OWNER_USERNAME, USERNAME_MAX_LENGTH } from '../src/lib/auth.ts';
const url = process.env.HALO_TEST_URL;
if (!url || !/^https:\/\/(127\.0\.0\.1|localhost):[0-9]+\/?$/.test(url))
  throw new Error('HALO_TEST_URL must identify an isolated loopback HTTPS companion');
const password = process.env.HALO_TEST_PASSWORD;
if (!password || password.length < 15)
  throw new Error('A test-only HALO_TEST_PASSWORD is required');
const output = process.env.HALO_BROWSER_OUTPUT;
if (!output?.startsWith('/'))
  throw new Error('HALO_BROWSER_OUTPUT must be an absolute task output directory');
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({
  ignoreHTTPSErrors: true,
  viewport: { width: 1440, height: 1000 },
});
const page = await context.newPage();
const errors = [];
const violations = [];
const requests = [];
page.on('pageerror', (error) => errors.push(error.message));
page.on('request', (request) =>
  requests.push({ method: request.method(), path: new URL(request.url()).pathname }),
);
await page.addInitScript(() =>
  document.addEventListener('securitypolicyviolation', (e) => {
    window.__haloCsp = [
      ...(window.__haloCsp ?? []),
      { directive: e.violatedDirective, blocked: e.blockedURI },
    ];
  }),
);
async function captureBlankAccessForm(name) {
  for (const width of [390, 1440]) {
    await page.setViewportSize({ width, height: 1000 });
    assert.ok(
      await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1),
      `${name} fits at ${width}px`,
    );
    await page.screenshot({ path: `${output}/${name}-${width}.png`, fullPage: true });
  }
}
try {
  await page.goto(url);
  await page.getByRole('heading', { name: /Set up web access|Welcome back/ }).waitFor();
  if (await page.getByRole('heading', { name: 'Set up web access' }).isVisible()) {
    await captureBlankAccessForm('setup-blank');
    if (!process.env.HALO_SETUP_CODE)
      throw new Error('Fresh test companion requires HALO_SETUP_CODE');
    await page.getByLabel('Setup code', { exact: true }).fill(process.env.HALO_SETUP_CODE);
    const owner = page.getByLabel('Username', { exact: true });
    assert.equal(await owner.inputValue(), OWNER_USERNAME);
    assert.equal(await owner.getAttribute('autocomplete'), 'username');
    assert.equal(await owner.getAttribute('name'), 'username');
    assert.equal(await owner.getAttribute('readonly'), '');
    const createPassword = page.getByLabel('Create password', { exact: true });
    for (const label of ['Create password', 'Repeat password']) {
      const field = page.getByLabel(label, { exact: true });
      assert.equal(await field.getAttribute('minlength'), '15');
      assert.equal(await field.getAttribute('maxlength'), '128');
    }
    await createPassword.pressSequentially('12345678901234');
    await page.getByLabel('Repeat password', { exact: true }).fill('12345678901234');
    await page.getByRole('button', { name: 'Create web access', exact: true }).click();
    assert.equal(
      await createPassword.evaluate((el) => el.validity.tooShort),
      true,
      '14-character setup password rejected locally',
    );
    assert.equal(
      requests.filter((r) => r.method === 'POST' && r.path.endsWith('/setup')).length,
      0,
      'invalid setup password never sent',
    );
    await createPassword.fill(password);
    await page.getByLabel('Repeat password', { exact: true }).fill(password);
    const enrollment = page.waitForRequest(
      (request) => request.method() === 'POST' && request.url().endsWith('/setup'),
    );
    await page.getByRole('button', { name: 'Create web access', exact: true }).click();
    assert.deepEqual((await enrollment).postDataJSON(), {
      setup_code: process.env.HALO_SETUP_CODE,
      username: OWNER_USERNAME,
      password,
    });
    await page.getByRole('heading', { name: 'Your Halo Server' }).waitFor();
    await page.evaluate(() => {
      location.hash = '/settings';
    });
    await page.getByRole('button', { name: 'Sign out', exact: true }).click();
  }
  await page.getByRole('heading', { name: 'Welcome back' }).waitFor();
  await page.evaluate(() => {
    location.hash = '/overview';
  });
  const usernameField = page.getByLabel('Username', { exact: true });
  const passwordField = page.getByLabel('Password', { exact: true });
  await captureBlankAccessForm('signin-blank');
  assert.equal(await usernameField.inputValue(), '', 'login does not invent an owner identity');
  assert.equal(await usernameField.getAttribute('autocomplete'), 'username');
  assert.equal(await usernameField.getAttribute('autocapitalize'), 'off');
  assert.equal(await usernameField.getAttribute('spellcheck'), 'false');
  assert.equal(await usernameField.getAttribute('maxlength'), String(USERNAME_MAX_LENGTH));
  assert.equal(await passwordField.getAttribute('autocomplete'), 'current-password');
  await passwordField.fill(password);
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  assert.equal(await usernameField.evaluate((el) => el.validity.valueMissing), true);
  assert.equal(
    requests.filter((r) => r.method === 'POST' && r.path.endsWith('/login')).length,
    0,
    'missing username never sent',
  );
  const failures = [];
  for (const [username, candidatePassword] of [
    ['unknown-owner', password],
    [OWNER_USERNAME, 'invalid-fixture-password'],
  ]) {
    await usernameField.fill(username);
    await passwordField.fill(candidatePassword);
    const request = page.waitForRequest(
      (request) => request.method() === 'POST' && request.url().endsWith('/login'),
    );
    const response = page.waitForResponse(
      (response) => response.request().method() === 'POST' && response.url().endsWith('/login'),
    );
    await page.getByRole('button', { name: 'Sign in', exact: true }).click();
    assert.deepEqual((await request).postDataJSON(), { username, password: candidatePassword });
    assert.equal((await response).status(), 401, 'invalid credentials are rejected');
    await page.getByRole('alert').waitFor();
    await page.getByRole('button', { name: 'Sign in', exact: true }).waitFor();
    failures.push(await page.getByRole('alert').textContent());
    assert.equal(await passwordField.inputValue(), '', 'failed login clears the password');
    assert.equal(await usernameField.inputValue(), username, 'failed login preserves the username');
  }
  assert.equal(failures[0], failures[1], 'credential errors do not identify which field failed');
  await usernameField.fill(OWNER_USERNAME);
  await passwordField.fill(password);
  const login = page.waitForRequest(
    (request) => request.method() === 'POST' && request.url().endsWith('/login'),
  );
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  assert.deepEqual((await login).postDataJSON(), { username: OWNER_USERNAME, password });
  await page.getByRole('heading', { name: 'Your Halo Server' }).waitFor();
  const cookies = await context.cookies();
  const sessionCookie = cookies.find((v) => v.name.startsWith('__Host-'));
  assert.ok(
    sessionCookie?.httpOnly && sessionCookie.secure && sessionCookie.sameSite === 'Strict',
    'secure session cookie',
  );
  const checks = [
    'web password limits match backend 15–128; short setup password blocked before POST',
    'setup uses readonly admin owner; login requires username and password with password-manager semantics',
    'unknown username and wrong password fail alike; failed login clears the password; valid admin credentials sign in',
  ];
  async function navigate(route) {
    await page.evaluate((value) => {
      location.hash = `/${value}`;
    }, route);
    await page.waitForFunction(
      (value) =>
        document.querySelector('#page-title')?.textContent?.toLowerCase() === value.split('/')[0],
      route,
    );
  }
  async function readApi(path) {
    return page.evaluate(async (path) => {
      const response = await fetch(`/api/web/v1/${path}`, { cache: 'no-store' });
      const value = await response.json();
      if (!response.ok || !value.ok) throw new Error(`Fixture API read failed: ${path}`);
      return value.result;
    }, path);
  }
  await navigate('devices');
  const links = page.locator('.device-link');
  await links.first().waitFor();
  assert.equal(await links.count(), 7, 'fixture contains seven cameras');
  const firstHref = await links.first().getAttribute('href');
  const search = page.getByRole('searchbox', { name: 'Search devices' });
  await search.fill('No such camera '.repeat(200));
  await page.getByText('No devices match your search.', { exact: true }).waitFor();
  await search.fill('');
  assert.equal(await links.count(), 7);
  await links.first().click();
  await page.getByRole('heading', { name: 'Recording', exact: true }).waitFor();
  await page.goBack();
  await search.waitFor();
  checks.push('seven devices; long/empty search; details and history Back');
  const controls = await readApi('controls');
  const light = controls.cameras.find((camera) => camera.has_light);
  assert.ok(light, 'fixture supplies reversible light control');
  await navigate(`devices/${encodeURIComponent(light.camera_id)}`);
  await page.getByRole('button', { name: 'On', exact: true }).waitFor({ state: 'visible' });
  await page.getByRole('button', { name: 'On', exact: true }).click();
  await page.getByText('Ring accepted the command.', { exact: false }).waitFor();
  await page.getByRole('button', { name: 'Off', exact: true }).click();
  await page.getByText('Ring accepted the command.', { exact: false }).waitFor();
  await navigate('settings');
  await page.getByRole('button', { name: 'Disconnect…', exact: true }).click();
  await page.getByRole('dialog').waitFor();
  await page.keyboard.press('Escape');
  await page.getByRole('dialog').waitFor({ state: 'hidden' });
  assert.equal(
    await page
      .getByRole('button', { name: 'Disconnect…', exact: true })
      .evaluate((el) => el === document.activeElement),
    true,
    'dialog restores focus',
  );
  checks.push(
    'real owner light On/Off; Ring disconnect confirmation canceled without sending; modal focus restored',
  );
  await navigate('settings');
  const mode = page.getByLabel('Operating mode', { exact: true });
  await mode.waitFor();
  const originalMode = await mode.inputValue();
  const draftMode = originalMode === 'homekit' ? 'ring-only' : 'homekit';
  await mode.selectOption(draftMode);
  page.once('dialog', (dialog) => dialog.dismiss());
  await page.evaluate(() => {
    location.hash = '/overview';
  });
  await page.waitForTimeout(100);
  assert.equal(await mode.inputValue(), draftMode, 'canceled navigation preserves draft');
  await page.getByRole('button', { name: 'Reload saved settings', exact: true }).click();
  await page.getByRole('dialog').waitFor();
  await page.getByRole('button', { name: 'Keep editing', exact: true }).click();
  assert.equal(await mode.inputValue(), draftMode);
  const latest = await readApi('configuration');
  const external = await page.evaluate(
    async ({ saved, mode }) => {
      const session = (await (await fetch('/api/web/v1/session')).json()).result;
      const { plugin_host, plugin_port, rtsp_host, rtsp_port } = saved;
      const response = await fetch('/api/web/v1/configuration', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Halo-CSRF': session.csrf_token },
        body: JSON.stringify({
          operation_id: crypto.randomUUID(),
          expected_revision: saved.revision,
          configuration: { mode, plugin_host, plugin_port, rtsp_host, rtsp_port },
        }),
      });
      return { status: response.status, value: await response.json() };
    },
    { saved: latest.saved, mode: draftMode },
  );
  assert.equal(external.status, 200, 'concurrent real owner save');
  assert.equal(external.value.result.status, 'completed');
  await page.getByRole('button', { name: 'Save settings', exact: true }).click();
  await page.getByRole('alert').waitFor();
  assert.equal(await mode.inputValue(), draftMode, 'revision conflict preserves unsaved draft');
  assert.ok(
    await page
      .getByRole('alert')
      .getByText('Your unsaved settings are preserved.', { exact: false })
      .isVisible(),
  );
  await page.getByRole('button', { name: 'Reload saved settings', exact: true }).click();
  await page
    .getByRole('dialog')
    .getByRole('button', { name: 'Reload saved settings', exact: true })
    .click();
  await page.getByText('Unsaved changes', { exact: true }).waitFor({ state: 'hidden' });
  await mode.selectOption(originalMode);
  await page.getByRole('button', { name: 'Save settings', exact: true }).click();
  await page.getByText('Unsaved changes', { exact: true }).waitFor({ state: 'hidden' });
  assert.equal(
    (await readApi('configuration')).saved.mode,
    originalMode,
    'fixture configuration restored',
  );
  checks.push(
    'concurrent real configuration save rejects stale revision and preserves draft; reload/save restores original configuration',
  );
  await page.getByRole('button', { name: 'Change password…', exact: true }).click();
  for (const label of ['Current Halo password', 'New Halo password', 'Repeat new password']) {
    const field = page.getByLabel(label, { exact: true });
    assert.equal(await field.getAttribute('minlength'), '15');
    assert.equal(await field.getAttribute('maxlength'), '128');
  }
  await page.getByLabel('New Halo password', { exact: true }).fill('test-unsaved-placeholder');
  await page.getByRole('button', { name: 'Change password…', exact: true }).click();
  await page.getByRole('button', { name: 'Change password…', exact: true }).click();
  assert.equal(
    await page.getByLabel('New Halo password', { exact: true }).inputValue(),
    '',
    'canceled password input cleared',
  );
  await page.getByRole('button', { name: 'Change password…', exact: true }).click();
  checks.push(
    'configuration draft survives canceled navigation/discard; canceled password cleared',
  );
  await page.locator('.toast').waitFor({ state: 'hidden' });
  const layouts = [];
  for (const theme of ['light', 'dark']) {
    await page.evaluate((value) => (document.documentElement.dataset.theme = value), theme);
    for (const width of [320, 390, 768, 1024, 1440]) {
      await page.setViewportSize({ width, height: 1000 });
      for (const route of [
        'overview',
        'devices',
        'integrations',
        'activity',
        'diagnostics',
        'settings',
      ]) {
        await page.evaluate((value) => {
          location.hash = `/${value}`;
        }, route);
        await page.waitForFunction(
          (value) => document.querySelector('#page-title')?.textContent?.toLowerCase() === value,
          route,
        );
        await page.waitForTimeout(200);
        const dimensions = await page.evaluate(() => ({
          viewport: innerWidth,
          scroll: document.documentElement.scrollWidth,
        }));
        assert.ok(
          dimensions.scroll <= dimensions.viewport + 1,
          `${route} overflows at ${width}px ${theme}: ${JSON.stringify(dimensions)}`,
        );
        layouts.push({ route, width, theme, ...dimensions });
        if (
          (width === 390 || width === 1440) &&
          ['overview', 'devices', 'settings'].includes(route)
        )
          await page.screenshot({
            path: `${output}/${route}-${theme}-${width}.png`,
            fullPage: true,
          });
      }
    }
  }
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await navigate('devices');
  await page.evaluate(() => {
    document.documentElement.style.zoom = '2';
  });
  assert.ok(
    await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1),
    '200% zoom fits',
  );
  await page.screenshot({ path: `${output}/devices-200-percent.png`, fullPage: true });
  await page.evaluate(() => {
    document.documentElement.style.zoom = '';
  });
  checks.push('200% CSS zoom; reduced-motion rendering');
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole('button', { name: 'Open navigation' }).click();
  await page.getByRole('button', { name: 'Close navigation', exact: true }).last().waitFor();
  await page.keyboard.press('Escape');
  assert.equal(
    await page.getByRole('button', { name: 'Open navigation' }).getAttribute('aria-expanded'),
    'false',
  );
  assert.equal(
    await page
      .getByRole('button', { name: 'Open navigation' })
      .evaluate((el) => el === document.activeElement),
    true,
    'drawer restores focus',
  );
  await page.evaluate(() => {
    location.hash = '/settings';
  });
  await page.getByRole('button', { name: 'Sign out', exact: true }).click();
  await page.getByRole('heading', { name: 'Welcome back' }).waitFor();
  const stored = await page.evaluate(() => ({
    local: Object.keys(localStorage),
    session: Object.keys(sessionStorage),
    violations: window.__haloCsp ?? [],
  }));
  violations.push(...stored.violations);
  assert.ok(
    stored.local.every((k) => k === 'halo-appearance') && stored.session.length === 0,
    'no auth secrets in browser storage',
  );
  assert.ok(
    !requests.some((r) => /snapshot|stream|capture|preview/.test(r.path)),
    'dashboard does not acquire media',
  );
  assert.deepEqual(errors, [], 'browser runtime errors');
  assert.deepEqual(violations, [], 'CSP violations');
  await writeFile(
    `${output}/browser-smoke.json`,
    JSON.stringify(
      {
        scope: 'isolated real HTTPS companion, synthetic owner data only',
        layouts,
        checks,
        cookies: cookies.map(({ name, httpOnly, secure, sameSite }) => ({
          name,
          httpOnly,
          secure,
          sameSite,
        })),
        errors,
        violations,
        requests,
      },
      null,
      2,
    ),
  );
  console.log(
    `PASS: ${layouts.length} theme/page/viewport combinations; secure auth/logout; zero media requests, browser errors, or CSP violations.`,
  );
} catch (error) {
  await page.screenshot({ path: `${output}/failure.png`, fullPage: true }).catch(() => {});
  await writeFile(
    `${output}/failure.json`,
    JSON.stringify({ error: String(error), errors, requests }, null, 2),
  );
  throw error;
} finally {
  await context.close();
  await browser.close();
}
