/** Privacy acceptance: loopback, synthetic state, no Ring or device APIs. */
import assert from 'node:assert/strict';
import { expect } from '@playwright/test';
import { createServer } from 'node:http';
import { readFile, mkdir, writeFile } from 'node:fs/promises';
import { dirname, extname, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { testBrowser, browserName } from './browser-engine.mjs';
const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const staticRoot = process.env.HALO_STATIC_ROOT || resolve(root, '../src/halo_server/web/static');
assert.ok(staticRoot.startsWith('/'));
const output = process.env.HALO_BROWSER_OUTPUT;
if (output) {
  assert.ok(output.startsWith('/'));
  await mkdir(output, { recursive: true });
}
const requests = [],
  writes = [],
  errors = [],
  checks = [],
  subscribers = new Set();
let account = 1,
  available = true,
  sourceFresh = true,
  reason = 'ring_motion_disabled';
let malformed = false,
  reauths = 0,
  holdReauth = false,
  releaseReauth;
let mode = 'ring',
  revision = 7;
let session = {
  authenticated: true,
  setup_required: false,
  csrf_token: 'fixture-csrf-0',
  reauthenticated: false,
  session_id: 'fixture-session-0',
};
const scope = () => ({ profile_id: `fixture-account-${account}`, account_generation: account });
const camera = () => ({
  id: 'camera-one',
  camera_id: 'camera-one',
  name: `Camera account ${account}`,
  model: 'Synthetic camera',
  source: 'ring',
  kind: 'camera',
  scope: { ...scope(), device_generation: 2 },
  capabilities: ['video'],
  observed: { source_available: true, battery_percent: null, light: null, siren: null },
  homekit: {
    selected: true,
    published: true,
    paired: true,
    owner_warm: false,
    recording_active: false,
    recording_audio_active: false,
  },
});
function snapshot() {
  const now = Date.now() / 1000;
  return {
    schema: 1,
    cursor: String(Date.now()),
    web: { boot_id: 'fixture-web', observed_at: now },
    daemon: { available: true, stale: false, observed_at: now, error: null },
    state: {
      schema: 1,
      epoch: `fixture-${account}`,
      observed_at: now,
      daemon: {
        boot_id: 'fixture-daemon',
        version: 'fixture',
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
      scope: scope(),
      homekit: {
        enabled: true,
        adapter_boot_id: 'fixture-adapter',
        state: 'published',
        registry_revision: 2,
        selected_count: 1,
        published_count: 1,
        recording_revision: 0,
      },
      devices: [camera()],
      activity: { epoch: 'fixture', cursor: 0, capacity: 256, entries: [], truncated: false },
      truncated: false,
    },
  };
}
function cameraPage(kind) {
  const common = {
    ...scope(),
    revision: 99,
    registry_revision: 2,
    available,
    boot_id: 'fixture-adapter',
    total: 1,
    warm_budget: { capacity: null, reserved: 0 },
  };
  if (kind === 'privacy')
    return {
      ...common,
      cameras: [
        {
          camera_id: 'camera-one',
          revision,
          mode: malformed ? 'unexpected-mode' : mode,
          ring_live_allowed: sourceFresh ? true : null,
          ring_recording_allowed: sourceFresh ? false : null,
          ring_snapshot_allowed: sourceFresh ? false : null,
          ring_audio_allowed: sourceFresh ? false : null,
          source_fresh: sourceFresh,
          effective_live_allowed: sourceFresh || mode === 'homekit',
          effective_recording_allowed: mode === 'homekit',
          effective_snapshot_allowed: mode === 'homekit',
          effective_audio_allowed: mode === 'homekit',
          reason,
        },
      ],
    };
  return {
    ...common,
    cameras: [
      {
        ...camera().scope,
        camera_id: 'camera-one',
        name: camera().name,
        selected: true,
        published: true,
        paired_known: true,
        paired: true,
        pairing_revision: 'fixture-pairing',
        pending_reset: false,
        has_light: false,
        has_siren: false,
        owner_warm: false,
        recording_supported: true,
        warm_state: 'disabled',
        mains_confirmed: false,
        active: false,
        audio_active: false,
        configuration: null,
      },
    ],
  };
}
function publish() {
  for (const response of subscribers)
    response.write(`event: state\ndata: ${JSON.stringify(snapshot())}\n\n`);
}
const server = createServer(async (request, response) => {
  const path = new URL(request.url, 'http://localhost').pathname;
  const send = (result) => {
    response.writeHead(200, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' });
    response.end(JSON.stringify({ ok: true, result }));
  };
  try {
    if (path.startsWith('/api/')) requests.push({ method: request.method, path });
    if (request.method !== 'GET') {
      assert.ok(
        path === '/api/web/v1/reauth' || path === '/api/web/v1/privacy',
        'No device writes',
      );
      assert.equal(request.headers['x-halo-csrf'], session.csrf_token);
      let raw = '';
      for await (const part of request) raw += part;
      const body = JSON.parse(raw);
      if (path.endsWith('/reauth')) {
        assert.equal(body.password, 'fixture-password-123');
        if (holdReauth) {
          holdReauth = false;
          await new Promise((done) => {
            releaseReauth = done;
          });
        }
        reauths++;
        session = {
          ...session,
          reauthenticated: true,
          csrf_token: `fixture-csrf-${reauths}`,
          session_id: `fixture-session-${reauths}`,
        };
        return send(session);
      }
      assert.deepEqual(
        Object.keys(body).sort(),
        [
          'profile_id',
          'account_generation',
          'camera_id',
          'expected_revision',
          'mode',
          'operation_id',
        ].sort(),
      );
      assert.equal(body.profile_id, scope().profile_id);
      assert.equal(body.account_generation, account);
      assert.equal(body.camera_id, 'camera-one');
      assert.equal(body.expected_revision, revision);
      assert.ok(['ring', 'homekit'].includes(body.mode));
      assert.match(body.operation_id, /^[0-9a-f-]{36}$/);
      assert.ok(reauths > writes.length);
      writes.push(body);
      mode = body.mode;
      revision++;
      return send({ operation_id: body.operation_id, status: 'completed', possibly_sent: false });
    }
    if (path === '/api/web/v1/session') return send(session);
    if (path === '/api/web/v1/state') return send(snapshot());
    if (path === '/api/web/v1/events') {
      response.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-store' });
      subscribers.add(response);
      publish();
      request.on('close', () => subscribers.delete(response));
      return;
    }
    if (
      ['selection', 'pairing', 'recording', 'controls', 'privacy'].includes(
        path.split('/').at(-1),
      ) &&
      path.startsWith('/api/web/v1/')
    )
      return send(cameraPage(path.split('/').at(-1)));
    if (path.startsWith('/api/')) throw new Error(`Unexpected fixture request ${path}`);
    const file = resolve(staticRoot, path === '/' ? 'index.html' : `.${path}`);
    assert.ok(!relative(staticRoot, file).startsWith('..'));
    const body = await readFile(file);
    response.writeHead(200, {
      'Content-Type':
        {
          '.html': 'text/html',
          '.js': 'application/javascript',
          '.css': 'text/css',
          '.png': 'image/png',
          '.svg': 'image/svg+xml',
        }[extname(file)] || 'application/octet-stream',
    });
    response.end(body);
  } catch (error) {
    errors.push(error.message);
    response.writeHead(500);
    response.end('Synthetic fixture rejected request');
  }
});
await new Promise((done) => server.listen(0, '127.0.0.1', done));
const interval = setInterval(publish, 1000);
const browser = await testBrowser.launch({ headless: true });
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
const page = await context.newPage(),
  origin = `http://127.0.0.1:${server.address().port}`;
await context.route('**/*', (route) =>
  new URL(route.request().url()).origin === origin ? route.continue() : route.abort(),
);
page.on('pageerror', (error) => errors.push(error.message));
const toggle = page.getByRole('switch', { name: 'Use Apple Home privacy controls', exact: true });
const row = (title) =>
  page.locator('.preference-row').filter({
    has: page.locator('.row-label > span').filter({ hasText: new RegExp(`^${title}$`) }),
  });
const ready = () => expect(toggle).toBeEnabled();
async function refresh() {
  await page.getByRole('button', { name: 'Refresh camera details', exact: true }).click();
  await expect(
    page.getByRole('button', { name: 'Refresh camera details', exact: true }),
  ).toBeEnabled();
}
async function beginOverride() {
  await ready();
  await toggle.click();
  const dialog = page.getByRole('dialog', { name: 'Use Apple Home privacy controls?' });
  await expect(dialog).toContainText('Halo cannot override restrictions enforced by Ring');
  await expect(dialog).toContainText('Allow Ring audio recording');
  await dialog.getByRole('button', { name: 'Continue', exact: true }).click();
}
async function password() {
  const dialog = page.getByRole('dialog', { name: 'Confirm your Halo password' });
  await dialog.locator('input[type=password]').fill('fixture-password-123');
  await dialog.getByRole('button', { name: 'Confirm', exact: true }).click();
}
let failure;
try {
  await page.goto(`${origin}/#/devices/camera-one`);
  await ready();
  await expect(toggle).not.toBeChecked();
  await expect(row('Live View source access').locator('.row-control')).toHaveText('Allowed');
  await expect(row('Recording source access').locator('.row-control')).toHaveText('Blocked');
  await expect(row('Audio capture source access').locator('.row-control')).toHaveText('Blocked');
  assert.equal(writes.length, 0);
  await toggle.click();
  await page.getByRole('dialog').getByRole('button', { name: 'Cancel', exact: true }).click();
  await ready();
  assert.equal(writes.length, 0);
  await beginOverride();
  await page
    .getByRole('dialog', { name: 'Confirm your Halo password' })
    .getByRole('button', { name: 'Cancel', exact: true })
    .click();
  await ready();
  assert.equal(writes.length, 0);
  checks.push(
    'Follow Ring default, effective permissions, both confirmation cancellations do not write',
  );
  await beginOverride();
  await password();
  await expect(toggle).toBeChecked();
  await ready();
  assert.equal(writes.length, 1);
  assert.equal(writes[0].expected_revision, 7);
  await expect(row('Recording source access').locator('.row-control')).toHaveText('Allowed');
  await expect(row('Audio capture source access').locator('.row-control')).toHaveText('Allowed');
  await expect(
    page.getByRole('switch', { name: 'Enable HomeKit Secure Video support', exact: true }),
  ).not.toBeChecked();
  checks.push(
    'Confirmed override reauthenticates, rotates CSRF, uses camera revision, and does not enable HKSV',
  );
  sourceFresh = false;
  reason = 'source_stale';
  await refresh();
  await ready();
  await expect(row('Live View source access').locator('.row-control')).toHaveText('Allowed');
  await expect(row('Live View source access')).toContainText('Ring reports Live View: Unknown');
  await expect(row('Audio capture source access').locator('.row-control')).toHaveText('Allowed');
  await expect(row('Audio capture source access')).toContainText(
    'Ring reports audio recording: Unknown',
  );
  await expect(row('Saved privacy choice').locator('.row-control')).toHaveText('Apple Home');
  await toggle.click();
  await password();
  await expect(toggle).not.toBeChecked();
  await ready();
  assert.equal(writes.length, 2);
  assert.equal(writes[1].expected_revision, 8);
  await expect(row('Live View source access').locator('.row-control')).toHaveText('Blocked');
  await expect(row('Audio capture source access').locator('.row-control')).toHaveText('Blocked');
  checks.push(
    'Unknown Ring reports are separate from effective HomeKit allowance; Follow Ring blocks stale sources',
  );
  sourceFresh = true;
  reason = 'ring_motion_disabled';
  await refresh();
  await beginOverride();
  holdReauth = true;
  await password();
  await expect.poll(() => typeof releaseReauth).toBe('function');
  account++;
  releaseReauth();
  releaseReauth = undefined;
  await expect(page.getByRole('heading', { name: 'Camera account 2', exact: true })).toBeVisible();
  await ready();
  assert.equal(writes.length, 2);
  await expect(toggle).not.toBeChecked();
  checks.push('Account change during password verification drops old scoped privacy action');
  malformed = true;
  await refresh();
  await expect(toggle).toBeDisabled();
  await expect(page.getByRole('alert')).toContainText('incomplete camera privacy information');
  malformed = false;
  available = false;
  await refresh();
  await expect(toggle).toBeDisabled();
  available = true;
  reason = '<untrusted-private-source-detail>';
  await refresh();
  await ready();
  await expect(
    page.getByText(
      'Current privacy information is unavailable. Refresh this camera to check again.',
    ),
  ).toBeVisible();
  await expect(page.getByText('<untrusted-private-source-detail>')).toHaveCount(0);
  checks.push(
    'Invalid/unavailable data cannot write; unknown reasons never render raw source text',
  );
  reason = 'ring_motion_disabled';
  await refresh();
  await ready();
  await expect(page.locator('.toast')).toHaveCount(0, { timeout: 10000 });
  for (const theme of ['light', 'dark'])
    for (const width of [390, 1440]) {
      await page.setViewportSize({ width, height: 1000 });
      await page.evaluate((value) => {
        document.documentElement.dataset.theme = value;
      }, theme);
      await page.getByRole('heading', { name: 'Privacy', exact: true }).evaluate((heading) => {
        heading.scrollIntoView({ block: 'start' });
        window.scrollBy(0, -76);
      });
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
      if (output) await page.screenshot({ path: resolve(output, `privacy-${theme}-${width}.png`) });
    }
  checks.push('Desktop/mobile light/dark layouts have no horizontal overflow');
  assert.equal(
    requests.filter(({ path }) => /snapshot|stream|capture|preview/.test(path)).length,
    0,
  );
  assert.equal(writes.length, 2);
  assert.deepEqual(errors, []);
  checks.push('No media/device requests or JavaScript/fixture errors');
} catch (error) {
  failure = error;
} finally {
  if (output)
    await writeFile(
      resolve(output, 'privacy-smoke.json'),
      JSON.stringify(
        {
          outcome: failure ? 'failed' : 'passed',
          browser: browserName,
          static_root: staticRoot,
          checks,
          writes,
          requests,
          errors,
          failure: failure?.message ?? null,
        },
        null,
        2,
      ),
    );
  await browser.close();
  clearInterval(interval);
  for (const response of subscribers) response.end();
  await new Promise((done) => server.close(done));
}
if (failure) throw failure;
console.log(
  JSON.stringify({
    outcome: 'passed',
    browser: browserName,
    checks: checks.length,
    writes: writes.length,
  }),
);
