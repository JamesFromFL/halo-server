/** Pairing lifecycle against real HTTPS/auth/SSE and synthetic owner metadata. */
import { chromium, request as requestContext } from '@playwright/test';
import assert from 'node:assert/strict';
import { mkdir, writeFile } from 'node:fs/promises';

const url = process.env.HALO_TEST_URL;
if (!url || !/^https:\/\/(127\.0\.0\.1|localhost):[0-9]+\/?$/.test(url))
  throw new Error('Pairing regression requires isolated loopback HTTPS');
const password = process.env.HALO_TEST_PASSWORD;
const code = process.env.HALO_SETUP_CODE;
const output = process.env.HALO_BROWSER_OUTPUT;
if (!password || password.length < 15 || !code || !output?.startsWith('/'))
  throw new Error('Synthetic credentials and an absolute output directory are required');
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({ ignoreHTTPSErrors: true });
const page = await context.newPage();
const checks = [],
  errors = [],
  requests = [];
let outcome = 'failed',
  failure = null;
page.on('pageerror', (error) => errors.push(error.message));
page.on('request', (r) => requests.push({ method: r.method(), path: new URL(r.url()).pathname }));
await page.addInitScript(() => {
  window.__haloPairingEvents = [];
  const RealEventSource = window.EventSource;
  let serial = 0;
  window.EventSource = class extends RealEventSource {
    constructor(...args) {
      super(...args);
      const id = ++serial;
      for (const type of ['state', 'resync', 'error'])
        this.addEventListener(type, () =>
          window.__haloPairingEvents.push({ id, type, at: Date.now() }),
        );
    }
  };
});
const visibleCode = page.getByRole('region', { name: 'HomeKit pairing code' });
const reveal = page.getByRole('button', { name: 'Reveal code', exact: true });
async function navigate(id = 'fixture-camera-a') {
  await page.evaluate((id) => {
    location.hash = `/devices/${id}`;
  }, id);
  await reveal.waitFor();
  await page.waitForFunction(() =>
    [...document.querySelectorAll('button')].some(
      (b) => b.textContent.trim() === 'Reveal code' && !b.disabled,
    ),
  );
}
async function authenticate() {
  await reveal.click();
  await page.getByRole('dialog').waitFor();
  await page.getByLabel('Password', { exact: true }).fill(password);
  await page.getByRole('button', { name: 'Confirm', exact: true }).click();
  await visibleCode.waitFor();
}
async function threeUpdates(newerThan = 0) {
  await page.waitForFunction(
    (id) => window.__haloPairingEvents.some((e) => e.id > id && e.type !== 'error'),
    newerThan,
  );
  const before = await page.evaluate(() => {
    const events = window.__haloPairingEvents.filter((e) => e.type !== 'error');
    const id = events.at(-1)?.id;
    return { id, count: events.filter((e) => e.id === id).length };
  });
  assert.ok(before.id, 'a real state feed must be established');
  await page.waitForFunction(
    ({ id, count }) =>
      window.__haloPairingEvents.filter((e) => e.id === id && e.type !== 'error').length >=
      count + 3,
    before,
    { timeout: 20000 },
  );
  assert.equal(
    await page.evaluate(
      (id) => window.__haloPairingEvents.some((e) => e.id === id && e.type === 'error'),
      before.id,
    ),
    false,
    'the replacement feed must remain connected',
  );
}
async function setHidden(hidden) {
  await page.evaluate((hidden) => {
    Object.defineProperty(document, 'hidden', { configurable: true, value: hidden });
    document.dispatchEvent(new Event('visibilitychange'));
  }, hidden);
}
try {
  await page.goto(url);
  await page.getByLabel('Setup code', { exact: true }).fill(code);
  await page.getByLabel('Create password', { exact: true }).fill(password);
  await page.getByLabel('Repeat password', { exact: true }).fill(password);
  await page.getByRole('button', { name: 'Create web access', exact: true }).click();
  await page.getByRole('heading', { name: 'Your Halo Server' }).waitFor();
  for (const [id, heading, paired] of [
    ['fixture-camera-paired', 'Fixture paired camera', true],
    ['fixture-camera-unknown', 'Fixture unknown camera', false],
  ]) {
    await page.evaluate((id) => {
      location.hash = `/devices/${id}`;
    }, id);
    await page.getByRole('heading', { name: heading, exact: true }).waitFor();
    if (paired) {
      await page.getByText('Already paired', { exact: true }).waitFor();
      assert.equal(await reveal.count(), 0, 'paired camera does not offer a setup code');
      assert.equal(
        await page.getByRole('button', { name: 'Reset…', exact: true }).isEnabled(),
        true,
        'explicit reset stays available for an already-paired camera',
      );
    } else {
      await page
        .getByText('Pairing status is unknown. Refresh this device before requesting a code.', {
          exact: true,
        })
        .waitFor();
      assert.equal(await reveal.isDisabled(), true, 'unknown camera pairing cannot reveal');
      await page.getByRole('button', { name: 'Refresh pairing status', exact: true }).click();
      await page.waitForFunction(
        () =>
          ![...document.querySelectorAll('button')].find(
            (button) => button.textContent.trim() === 'Refresh pairing status',
          )?.disabled,
      );
      assert.equal(await reveal.isDisabled(), true);
    }
    assert.equal(await page.getByRole('dialog').count(), 0);
    assert.equal(await visibleCode.count(), 0);
  }
  assert.equal(
    requests.filter(
      (request) =>
        request.path.endsWith('/reauth') ||
        request.path.endsWith('/pairing/reveal') ||
        request.path.endsWith('/pairing/reset'),
    ).length,
    0,
    'paired/unknown metadata does not prompt, reveal, or reset automatically',
  );
  checks.push(
    'already-paired camera explains existing Home association without a reveal prompt; unknown pairing stays disabled; explicit reset remains separate',
  );
  await navigate();
  const oldCookie = (await context.cookies()).find(
    (cookie) => cookie.name === '__Host-HaloSession',
  );
  assert.ok(oldCookie, 'authenticated fixture has a session cookie');
  await authenticate();
  const rotated = (await context.cookies()).find((cookie) => cookie.name === oldCookie.name);
  assert.notEqual(rotated?.value, oldCookie.value, 'password confirmation rotates the session');
  const isolated = await requestContext.newContext({ ignoreHTTPSErrors: true });
  try {
    const oldResponse = await isolated.get(`${url}/api/web/v1/state`, {
      headers: { Cookie: `${oldCookie.name}=${oldCookie.value}` },
    });
    assert.equal(oldResponse.status(), 401, 'old session remains revoked');
  } finally {
    await isolated.dispose();
  }
  await threeUpdates();
  assert.equal(
    await visibleCode.isVisible(),
    true,
    'pairing code must remain visible across three real post-reauth state updates',
  );
  assert.match(
    await page.locator('.footer').textContent(),
    /Updated:/,
    'new session remains connected',
  );
  checks.push('reauth rotates and invalidates old cookie; code survives three real SSE updates');

  await setHidden(true);
  await visibleCode.waitFor({ state: 'hidden' });
  await setHidden(false);
  await navigate();
  await authenticate();
  await threeUpdates();
  assert.equal(await visibleCode.isVisible(), true, 'second reauth also preserves the new feed');
  await navigate('fixture-camera-b');
  await visibleCode.waitFor({ state: 'hidden' });
  checks.push('tab hiding and camera navigation clear pairing code; later reveal remains stable');

  let release, entered;
  const gate = new Promise((resolve) => {
    release = resolve;
  });
  const arrival = new Promise((resolve) => {
    entered = resolve;
  });
  await page.route(
    '**/api/web/v1/reauth',
    async (route) => {
      entered();
      await gate;
      await route.continue();
    },
    { times: 1 },
  );
  await reveal.click();
  await page.getByLabel('Password', { exact: true }).fill(password);
  const delayedResponse = page
    .waitForResponse((response) => response.url().endsWith('/reauth'))
    .catch(() => null);
  await page.getByRole('button', { name: 'Confirm', exact: true }).click();
  await arrival;
  await page.keyboard.press('Escape');
  await page.getByRole('dialog').waitFor({ state: 'hidden' });
  // Returning from a hidden tab restores the old valid feed while the password
  // request remains in flight; this makes a replacement prompt reachable.
  await setHidden(true);
  await setHidden(false);
  await navigate('fixture-camera-b');
  await reveal.click();
  await page.getByRole('dialog').waitFor();
  const revealsBefore = requests.filter((r) => r.path.endsWith('/pairing/reveal')).length;
  const precedingFeed = await page.evaluate(() => window.__haloPairingEvents.at(-1)?.id ?? 0);
  release();
  assert.equal(
    (await delayedResponse)?.status(),
    200,
    'delayed request still exercises real password verification',
  );
  await threeUpdates(precedingFeed);
  assert.equal(
    await page.getByRole('dialog').isVisible(),
    true,
    'canceled old request must not resolve replacement password prompt',
  );
  assert.equal(
    await visibleCode.count(),
    0,
    'replacement prompt cannot reveal a code without confirmation',
  );
  assert.equal(
    requests.filter((r) => r.path.endsWith('/pairing/reveal')).length,
    revealsBefore,
    'canceled reauth cannot submit another reveal',
  );
  await page.keyboard.press('Escape');
  checks.push('delayed canceled reauth cannot approve a replacement dialog or reveal');

  await authenticate();
  await visibleCode.waitFor();
  const logoutStatus = await page.evaluate(async () => {
    const session = (await (await fetch('/api/web/v1/session')).json()).result;
    return (
      await fetch('/api/web/v1/logout', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Halo-CSRF': session.csrf_token },
        body: '{}',
      })
    ).status;
  });
  assert.equal(logoutStatus, 200);
  await visibleCode.waitFor({ state: 'hidden' });
  await page.getByRole('heading', { name: 'Welcome back' }).waitFor();
  checks.push('server-side logout clears revealed code and returns to sign-in');
  assert.deepEqual(errors, [], 'no browser exceptions');
  outcome = 'passed';
} catch (error) {
  failure = error instanceof Error ? error.message : 'Pairing regression failed';
  throw error;
} finally {
  const events = await page.evaluate(() => window.__haloPairingEvents ?? []).catch(() => []);
  await writeFile(
    `${output}/pairing-browser.json`,
    JSON.stringify(
      { outcome, checks, failure, browser_errors: errors, events, requests },
      null,
      2,
    ) + '\n',
  );
  await context.close();
  await browser.close();
  console.log(JSON.stringify({ outcome, checks, failure }));
}
