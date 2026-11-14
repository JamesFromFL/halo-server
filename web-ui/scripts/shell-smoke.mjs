/** Shell recovery and session races using isolated loopback and fully mocked APIs. */
import assert from 'node:assert/strict';
import { readFile, mkdir, writeFile } from 'node:fs/promises';
import { testBrowser, browserName } from './browser-engine.mjs';

const url = process.env.HALO_TEST_URL;
if (!url || !/^https?:\/\/(127\.0\.0\.1|localhost):[0-9]+\/?$/.test(url))
  throw new Error('Shell tests require an isolated loopback interface');
const output = process.env.HALO_BROWSER_OUTPUT;
if (!output?.startsWith('/')) throw new Error('An absolute evidence directory is required');
await mkdir(output, { recursive: true });
const origin = new URL(url).origin;
const stamp = Date.now() / 1000;
const state = process.env.HALO_STATE_FIXTURE
  ? JSON.parse(await readFile(process.env.HALO_STATE_FIXTURE, 'utf8'))
  : {
      schema: 1,
      web: { boot_id: 'fixture-web', observed_at: stamp },
      daemon: { available: true, stale: false, observed_at: stamp, error: null },
      cursor: 'fixture:1',
      state: {
        schema: 1,
        epoch: 'fixture-account',
        observed_at: stamp,
        daemon: {
          boot_id: 'fixture-daemon',
          version: 'fixture-version',
          uptime_seconds: 1,
          stopping: false,
          release: { managed: true },
        },
        ring: {
          authenticated: true,
          restoring: false,
          events: 'connected',
          inventory_complete: true,
        },
        scope: { profile_id: 'fixture-profile', account_generation: 1 },
        homekit: {
          enabled: true,
          adapter_boot_id: 'fixture-adapter',
          state: 'published',
          registry_revision: 1,
          selected_count: 0,
          published_count: 0,
          recording_revision: 0,
        },
        devices: [],
        activity: {
          epoch: 'fixture',
          cursor: 1,
          capacity: 256,
          entries: [
            {
              sequence: 1,
              at: stamp,
              category: 'fixture',
              action: 'fixture_status',
              outcome: 'accepted',
              camera_id: null,
            },
          ],
          truncated: false,
        },
        truncated: false,
      },
    };
state.web.observed_at = stamp;
const configuration = {
  version: 1,
  revision: 1,
  mode: 'homekit',
  plugin_host: '127.0.0.1',
  plugin_port: 18443,
  rtsp_host: '127.0.0.1',
  rtsp_port: 18554,
};
let session = {
  authenticated: true,
  setup_required: false,
  csrf_token: 'fixture-csrf-one',
  reauthenticated: true,
  session_id: 'fixture-session-one',
};
let delayLogin;
let delayRingLogin;
let ringLoginStatus = 'mfa_required';
let expireState = false;
const writes = [],
  errors = [],
  checks = [];
const browser = await testBrowser.launch({ headless: true });
const context = await browser.newContext({
  ignoreHTTPSErrors: true,
  viewport: { width: 1440, height: 1000 },
});
const page = await context.newPage();
page.on('pageerror', (error) => errors.push(error.message));
await context.route('**/*', async (route) => {
  const request = route.request(),
    target = new URL(request.url());
  if (target.origin !== origin) return route.abort();
  if (!target.pathname.startsWith('/api/web/v1/')) return route.continue();
  const path = target.pathname.slice('/api/web/v1/'.length);
  let result;
  if (request.method() !== 'GET') {
    writes.push(path);
    assert.equal(request.headers()['x-halo-csrf'], session.csrf_token);
    if (path === 'logout') session = { ...session, authenticated: false };
    else if (path === 'login') {
      if (delayLogin) await delayLogin;
      session = { ...session, authenticated: true, csrf_token: 'fixture-login-csrf' };
    } else if (path === 'password') {
      assert.equal(
        await page.evaluate(() => window.__shellFeeds.filter((feed) => !feed.closed).length),
        0,
        'old feed closes before password rotation',
      );
      session = {
        ...session,
        csrf_token: 'fixture-password-csrf',
        session_id: 'fixture-password-session',
      };
    } else if (path === 'ring/login') {
      if (delayRingLogin) await delayRingLogin;
      return route.fulfill({
        json: {
          ok: true,
          result: {
            status: ringLoginStatus,
            ...(ringLoginStatus === 'mfa_required'
              ? { challenge_id: 'fixture-challenge', expires_at: Date.now() / 1000 + 60 }
              : {}),
          },
        },
      });
    } else if (path === 'ring/challenge/fixture-challenge') {
      return route.fulfill({ json: { ok: true, result: { cancelled: true } } });
    } else
      return route.fulfill({
        status: 400,
        json: {
          ok: false,
          error: { code: 'unexpected_fixture_write', message: 'Unexpected test action' },
        },
      });
    return route.fulfill({ json: { ok: true, result: session } });
  }
  if (path === 'session') result = session;
  else if (path === 'state') {
    if (expireState) {
      session = { ...session, authenticated: false };
      return route.fulfill({
        status: 401,
        json: { ok: false, error: { code: 'session_required', message: 'Sign in again' } },
      });
    }
    state.web.observed_at = Date.now() / 1000;
    result = state;
  } else if (path === 'configuration')
    result = { saved: configuration, applied: configuration, restart_required: false };
  else if (path === 'access') result = { canonical_url: origin, lan_enabled: false };
  else if (path === 'restart')
    result = { available: false, code: 'fixture', selection_revision: null };
  else if (path === 'sessions')
    result = { sessions: [{ session_id: session.session_id, current: true, created_at: stamp }] };
  else if (path === 'operations') result = { operations: [] };
  else
    return route.fulfill({
      status: 404,
      json: { ok: false, error: { code: 'not_found', message: 'Fixture read unavailable' } },
    });
  return route.fulfill({ json: { ok: true, result } });
});
await page.addInitScript((state) => {
  window.__shellState = state;
  window.__shellFeeds = [];
  window.EventSource = class extends EventTarget {
    closed = false;
    constructor() {
      super();
      window.__shellFeeds.push(this);
      setTimeout(() => this.push(), 50);
    }
    push() {
      if (this.closed) return;
      window.__shellState.web.observed_at = Date.now() / 1000;
      this.dispatchEvent(new MessageEvent('state', { data: JSON.stringify(window.__shellState) }));
    }
    close() {
      this.closed = true;
    }
  };
  const fetch = window.fetch.bind(window);
  window.fetch = (input, init) => {
    if (window.__delayConfiguration && String(input).endsWith('/configuration')) {
      window.__delayConfiguration = false;
      return fetch(input, init).then(
        (response) =>
          new Promise((resolve) => {
            window.__releaseConfiguration = () => resolve(response);
          }),
      );
    }
    if (window.__delayState && String(input).endsWith('/state')) {
      window.__delayState = false;
      const older = structuredClone(window.__shellState);
      older.state.daemon.version = 'obsolete-observation';
      // Ignore AbortSignal deliberately: the response could already be buffered.
      return new Promise((resolve) => {
        window.__releaseState = () =>
          resolve(new Response(JSON.stringify({ ok: true, result: older })));
      });
    }
    return fetch(input, init);
  };
}, state);
async function navigate(route) {
  await page.evaluate((route) => {
    location.hash = `/${route}`;
  }, route);
  await page.waitForFunction(
    (route) => document.querySelector('#page-title')?.textContent?.toLowerCase() === route,
    route,
  );
}
async function holdState() {
  await page.evaluate(() => {
    window.__delayState = true;
    window.__releaseState = null;
  });
  await page.getByRole('button', { name: 'Refresh server state' }).click();
  await page.waitForFunction(() => typeof window.__releaseState === 'function');
}
try {
  await page.goto(url);
  await page.getByRole('heading', { name: 'Your Halo Server' }).waitFor();
  assert.equal(await page.locator('#startup-status').count(), 0);
  checks.push('authenticated real-shaped state renders without leaving the startup fallback');

  await holdState();
  state.state.daemon.version = 'newest-observation';
  await page.evaluate((state) => {
    window.__shellState = state;
    for (const feed of window.__shellFeeds) feed.push();
    window.__releaseState();
  }, state);
  await page.waitForFunction(() =>
    document.querySelector('.footer')?.textContent.includes('newest-observation'),
  );
  await page.waitForTimeout(100);
  assert.ok(!(await page.locator('.footer').innerText()).includes('obsolete-observation'));
  checks.push(
    'a late state read cannot overwrite newer pushed observations even when abort is ignored',
  );

  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole('button', { name: 'Open navigation' }).click();
  assert.equal(await page.locator('.main').getAttribute('inert'), '');
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.waitForFunction(() => !document.querySelector('.main')?.hasAttribute('inert'));
  checks.push('resizing an open mobile drawer to desktop restores page interaction');

  await navigate('settings');
  await page.getByRole('button', { name: 'Reload saved settings', exact: true }).waitFor();
  await page.evaluate(() => {
    window.__delayConfiguration = true;
  });
  await page.getByRole('button', { name: 'Reload saved settings', exact: true }).click();
  await page.waitForFunction(() => typeof window.__releaseConfiguration === 'function');
  await page.getByLabel('Operating mode', { exact: true }).selectOption('ring-only');
  await page.evaluate(() => window.__releaseConfiguration());
  await page.waitForFunction(
    () => ![...document.querySelectorAll('p')].some((p) => p.textContent === 'Loading settings…'),
  );
  assert.equal(await page.getByLabel('Operating mode', { exact: true }).inputValue(), 'ring-only');
  await page.getByLabel('Operating mode', { exact: true }).selectOption('homekit');
  checks.push('a delayed settings reload preserves edits made after the read began');

  await page.getByRole('button', { name: 'Connect account…', exact: true }).click();
  await page.getByLabel('Ring email', { exact: true }).fill('fixture@example.invalid');
  await page.getByLabel('Ring password', { exact: true }).fill('synthetic-ring-password');
  let finishRingLogin;
  delayRingLogin = new Promise((resolve) => {
    finishRingLogin = resolve;
  });
  await page.getByRole('button', { name: 'Connect Ring', exact: true }).click();
  await page.waitForTimeout(100);
  await page.evaluate(() => {
    Object.defineProperty(document, 'hidden', { configurable: true, value: true });
    document.dispatchEvent(new Event('visibilitychange'));
  });
  const cancelledChallenge = page.waitForResponse((response) =>
    response.url().endsWith('/ring/challenge/fixture-challenge'),
  );
  finishRingLogin();
  await cancelledChallenge;
  assert.ok(writes.includes('ring/challenge/fixture-challenge'));
  assert.equal(
    await page.getByRole('heading', { name: 'Ring verification', exact: true }).count(),
    0,
  );
  assert.equal(await page.getByLabel('Ring password', { exact: true }).inputValue(), '');
  await page.evaluate(() => {
    Object.defineProperty(document, 'hidden', { configurable: true, value: false });
    document.dispatchEvent(new Event('visibilitychange'));
  });
  checks.push(
    'a late Ring challenge after hiding the page is canceled without resurfacing verification or credentials',
  );

  ringLoginStatus = 'connected';
  delayRingLogin = new Promise((resolve) => {
    finishRingLogin = resolve;
  });
  await page.getByLabel('Ring email', { exact: true }).fill('next-fixture@example.invalid');
  await page.getByLabel('Ring password', { exact: true }).fill('synthetic-ring-password');
  const connecting = page.waitForRequest((request) => request.url().endsWith('/ring/login'));
  const connectedReply = page.waitForResponse((response) => response.url().endsWith('/ring/login'));
  await page.getByRole('button', { name: 'Connect Ring', exact: true }).click();
  await connecting;
  state.state.epoch = 'replacement-account-epoch';
  await page.evaluate((state) => {
    window.__shellState = state;
    for (const feed of window.__shellFeeds) feed.push();
  }, state);
  await page.getByLabel('Ring email', { exact: true }).waitFor({ state: 'hidden' });
  finishRingLogin();
  await connectedReply;
  await page.getByRole('button', { name: 'Connect account…', exact: true }).click();
  assert.equal(await page.getByLabel('Ring email', { exact: true }).inputValue(), '');
  await page.getByRole('button', { name: 'Connect account…', exact: true }).click();
  checks.push(
    'an account-changing SSE retires the old Ring login form before its connected HTTP reply',
  );

  await page.getByRole('button', { name: 'Change password…' }).click();
  await page.getByLabel('Current Halo password', { exact: true }).fill('synthetic-old-password');
  await page.getByLabel('New Halo password', { exact: true }).fill('synthetic-new-password');
  await page.getByLabel('Repeat new password', { exact: true }).fill('synthetic-new-password');
  await page.getByRole('button', { name: 'Change password', exact: true }).click();
  await page
    .getByText('Web password changed. Other sessions are revoked.', { exact: true })
    .waitFor();
  await page.waitForFunction(() => window.__shellFeeds.filter((feed) => !feed.closed).length === 1);
  assert.equal(writes.filter((path) => path === 'password').length, 1);
  checks.push(
    'password rotation closes the old stream, adopts the new session and starts exactly one replacement',
  );

  await holdState();
  await page.getByRole('button', { name: 'Sign out', exact: true }).click();
  await page.getByRole('heading', { name: 'Welcome back' }).waitFor();
  await page.evaluate(() => window.__releaseState());
  await page.waitForTimeout(100);
  assert.equal(await page.locator('.app').count(), 0);
  checks.push('sign-out discards delayed authenticated state');

  let finishLogin;
  delayLogin = new Promise((resolve) => {
    finishLogin = resolve;
  });
  await page.getByLabel('Username', { exact: true }).fill('admin');
  await page.getByLabel('Password', { exact: true }).fill('synthetic-test-password');
  await page.locator('form').evaluate((form) => {
    form.requestSubmit();
    form.requestSubmit();
  });
  await page.waitForTimeout(100);
  assert.equal(writes.filter((path) => path === 'login').length, 1);
  finishLogin();
  await page.getByRole('button', { name: 'Sign out', exact: true }).waitFor();
  checks.push(
    'duplicate login submissions send credentials once and successful login adopts its returned session',
  );

  await navigate('overview');
  await page.evaluate(() => {
    window.__originalDateString = Date.prototype.toLocaleString;
    Date.prototype.toLocaleString = () => {
      throw new Error('Synthetic page-render failure');
    };
    location.hash = '/activity';
  });
  await page.getByRole('heading', { name: 'This page could not be displayed' }).waitFor();
  assert.equal(await page.getByRole('navigation').count(), 1);
  await page.evaluate(() => {
    Date.prototype.toLocaleString = window.__originalDateString;
  });
  await page.getByRole('button', { name: 'Try this page again' }).click();
  await page.getByRole('heading', { name: 'Activity', exact: true, level: 2 }).waitFor();
  checks.push(
    'a page rendering failure keeps navigation visible and can recover without changing server state',
  );

  expireState = true;
  await page.getByRole('button', { name: 'Refresh server state' }).click();
  await page.getByRole('heading', { name: 'Welcome back' }).waitFor();
  checks.push('an expired session returns to sign-in instead of retaining authenticated controls');

  const blocked = await context.newPage();
  await blocked.route('**/*', (route) =>
    route.request().resourceType() === 'script' ? route.abort() : route.fallback(),
  );
  await blocked.goto(url);
  await blocked.getByRole('link', { name: 'Reload Halo', exact: true }).waitFor();
  assert.equal(await blocked.locator('#app').innerText(), '');
  await blocked.close();
  checks.push('missing JavaScript leaves a usable plain-HTML reload link');
  assert.deepEqual(errors, []);
  const receipt = {
    passed: true,
    browser: browserName,
    checks,
    page_errors: errors,
    all_api_requests_mocked: true,
    production_contacted: false,
    camera_count: state.state.devices.length,
    noncamera_count: state.state.noncamera?.devices.length ?? 0,
  };
  await writeFile(`${output}/shell-${browserName}.json`, JSON.stringify(receipt, null, 2));
  console.log(JSON.stringify(receipt, null, 2));
} finally {
  await context.close();
  await browser.close();
}
