/** Camera lighting UI checks; synthetic loopback only, no device API access. */
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
  holdRead = false,
  releaseRead;
const initial = () => ({
  fresh: true,
  observed_age_seconds: 1,
  light: false,
  motion_duration_seconds: 60,
  manual_timer_status: 'unverified',
  activation_source: null,
  reason: null,
});
let lighting = initial(),
  operation = null;
const scope = () => ({ profile_id: `fixture-${account}`, account_generation: account });
const camera = () => ({
  id: 'camera-one',
  camera_id: 'camera-one',
  name: `Camera ${account}`,
  model: 'Fixture Floodlight',
  source: 'ring',
  kind: 'camera',
  scope: { ...scope(), device_generation: account },
  capabilities: ['video', 'light'],
  observed: { source_available: true, battery_percent: null, light: false, siren: null },
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
    web: { boot_id: 'web-fixture', observed_at: now },
    daemon: { available, stale: !available, observed_at: now, error: null },
    state: {
      schema: 1,
      epoch: `account-${account}`,
      observed_at: now,
      daemon: {
        boot_id: 'daemon-fixture',
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
        registry_revision: 1,
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
  const row = {
    ...camera().scope,
    camera_id: 'camera-one',
    name: camera().name,
    selected: true,
    published: true,
    paired_known: true,
    paired: true,
    pairing_revision: 'fixture-pairing',
    pending_reset: false,
    has_light: true,
    has_siren: false,
    light: false,
    siren: null,
    owner_warm: false,
    recording_supported: true,
    warm_state: 'disabled',
    active: false,
    audio_active: false,
    configuration: null,
  };
  if (kind === 'controls' && lighting !== undefined) row.lighting = structuredClone(lighting);
  if (kind === 'privacy')
    Object.assign(row, {
      revision: 1,
      mode: 'ring',
      source_fresh: true,
      ring_live_allowed: true,
      ring_recording_allowed: true,
      ring_snapshot_allowed: true,
      ring_audio_allowed: true,
      effective_live_allowed: true,
      effective_recording_allowed: true,
      effective_snapshot_allowed: true,
      effective_audio_allowed: true,
      reason: null,
    });
  return {
    ...scope(),
    revision: 1,
    registry_revision: 1,
    available: true,
    boot_id: 'fixture-adapter',
    cameras: [row],
    total: 1,
    warm_budget: { capacity: null, reserved: 0 },
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
      assert.equal(path, '/api/web/v1/controls');
      assert.equal(request.method, 'POST');
      assert.equal(request.headers['x-halo-csrf'], 'fixture-csrf');
      let raw = '';
      for await (const part of request) raw += part;
      const body = JSON.parse(raw);
      assert.deepEqual(
        Object.keys(body).sort(),
        [
          'profile_id',
          'account_generation',
          'camera_id',
          'device_generation',
          'control',
          'enabled',
          'operation_id',
        ].sort(),
      );
      assert.equal(body.profile_id, scope().profile_id);
      assert.equal(body.account_generation, account);
      assert.equal(body.camera_id, 'camera-one');
      assert.equal(body.device_generation, account);
      assert.equal(body.control, 'light');
      assert.equal(typeof body.enabled, 'boolean');
      assert.match(body.operation_id, /^[0-9a-f-]{36}$/);
      assert.ok(!writes.some((w) => w.operation_id === body.operation_id));
      writes.push(body);
      if (body.enabled) {
        lighting = { ...initial(), fresh: false, reason: 'lighting_changed' };
        operation = {
          operation_id: body.operation_id,
          status: 'pending',
          result: { status: 'pending' },
          possibly_sent: false,
        };
      } else {
        lighting = initial();
        operation = {
          operation_id: body.operation_id,
          status: 'completed',
          result: { status: 'accepted' },
          possibly_sent: false,
        };
      }
      return send(operation);
    }
    if (path === '/api/web/v1/session')
      return send({
        authenticated: true,
        setup_required: false,
        csrf_token: 'fixture-csrf',
        reauthenticated: true,
        session_id: 'fixture-session',
      });
    if (path === '/api/web/v1/state') return send(snapshot());
    if (path === '/api/web/v1/events') {
      response.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-store' });
      subscribers.add(response);
      publish();
      request.on('close', () => subscribers.delete(response));
      return;
    }
    if (path.startsWith('/api/web/v1/operations/')) {
      assert.equal(path.split('/').at(-1), operation.operation_id);
      operation = {
        ...operation,
        status: 'unknown',
        result: { status: 'unknown' },
        possibly_sent: true,
      };
      return send(operation);
    }
    const kind = path.split('/').at(-1);
    if (
      path.startsWith('/api/web/v1/') &&
      ['selection', 'pairing', 'recording', 'controls', 'privacy'].includes(kind)
    ) {
      const result = cameraPage(kind);
      if (kind === 'controls' && holdRead) {
        holdRead = false;
        releaseRead = () => send(result);
        return;
      }
      return send(result);
    }
    if (path.startsWith('/api/')) throw new Error(`Unexpected fixture API ${path}`);
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
    response.end('Rejected fixture request');
  }
});
await new Promise((done) => server.listen(0, '127.0.0.1', done));
const timer = setInterval(publish, 1000);
const browser = await testBrowser.launch({ headless: true });
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
const page = await context.newPage(),
  origin = `http://127.0.0.1:${server.address().port}`;
await context.route('**/*', (route) =>
  new URL(route.request().url()).origin === origin ? route.continue() : route.abort(),
);
page.on('pageerror', (error) => errors.push(error.message));
const section = page.getByRole('region', { name: 'Camera lighting', exact: true });
const on = section.getByRole('button', { name: 'On', exact: true }),
  off = section.getByRole('button', { name: 'Off', exact: true });
const motion = section
  .locator('.preference-row')
  .filter({ hasText: 'Motion shut-off time' })
  .locator('.row-control');
async function refresh() {
  await page.getByRole('button', { name: 'Refresh camera details', exact: true }).click();
  await expect(
    page.getByRole('button', { name: 'Refresh camera details', exact: true }),
  ).toBeEnabled();
}
let failure;
try {
  await page.goto(`${origin}/#/devices/camera-one`);
  await expect(on).toBeEnabled();
  await expect(section).toContainText('Ring reports: Off.');
  await expect(motion).toHaveText('1 minute');
  await expect(section.locator('input,select')).toHaveCount(0);
  assert.equal(writes.length, 0);
  checks.push(
    'One-minute native motion observation; explicit On/Off and no timer setters or invented controls',
  );
  lighting = { ...initial(), light: true, motion_duration_seconds: null, reason: 'source_unknown' };
  await refresh();
  await expect(section).toContainText('Ring reports: On.');
  await expect(motion).toHaveText('Unknown');
  lighting = { ...initial(), light: null, reason: 'source_unknown' };
  await refresh();
  await expect(section).toContainText('Ring reports: Unknown.');
  await expect(motion).toHaveText('1 minute');
  checks.push('Partial fresh receipt preserves independently known light and motion timer');
  lighting = { ...initial(), observed_age_seconds: 89.5 };
  await refresh();
  await expect(motion).toHaveText('Unknown', { timeout: 4000 });
  await expect(section).toContainText('Ring reports: Unknown.');
  await expect(section).toContainText('out of date');
  checks.push('An open page expires lighting observations after the 90-second TTL');
  lighting = { ...initial(), fresh: false, reason: 'lighting_changed' };
  await refresh();
  await expect(section).toContainText('Ring reports: Unknown.');
  await expect(motion).toHaveText('Unknown');
  lighting = { ...initial(), manual_timer_status: 'until_off' };
  await refresh();
  await expect(motion).toHaveText('Unknown');
  lighting = undefined;
  await refresh();
  await expect(section).toContainText('Ring reports: Off.');
  await expect(motion).toHaveText('Unknown');
  checks.push(
    'Changed/malformed receipts never borrow old state; absent optional DTO preserves legacy light observation',
  );
  lighting = initial();
  await refresh();
  await on.click();
  await expect(
    page.getByText('Halo is still finishing this action.', { exact: true }),
  ).toBeVisible();
  await expect(on).toBeDisabled();
  await expect(off).toBeDisabled();
  await expect(section).toContainText('Ring reports: Unknown.');
  assert.equal(writes.length, 1);
  await page.getByRole('button', { name: 'Check outcome', exact: true }).click();
  await expect(off).toBeEnabled();
  await expect(on).toBeDisabled();
  assert.equal(writes.length, 1);
  await off.click();
  await expect(off).toBeEnabled();
  await expect(on).toBeEnabled();
  await expect(page.getByText('Ring accepted the command.', { exact: false })).toBeVisible();
  assert.equal(writes.length, 2);
  assert.deepEqual(
    writes.map((w) => w.enabled),
    [true, false],
  );
  checks.push(
    'Pending blocks overlapping writes; uncertain outcome is read-only checked; explicit Off remains usable without replay',
  );
  holdRead = true;
  await page.getByRole('button', { name: 'Refresh camera details', exact: true }).click();
  await expect.poll(() => typeof releaseRead).toBe('function');
  account++;
  publish();
  await expect(page.getByRole('heading', { name: 'Camera 2', exact: true })).toBeVisible();
  await expect(on).toBeEnabled();
  releaseRead();
  releaseRead = undefined;
  await expect(motion).toHaveText('1 minute');
  assert.equal(writes.length, 2);
  available = false;
  publish();
  await expect(on).toBeDisabled();
  await expect(off).toBeDisabled();
  await expect(section).toContainText('Ring reports: Unknown.');
  available = true;
  publish();
  await expect(on).toBeEnabled();
  checks.push(
    'Late previous-account details and disconnected source cannot restore stale controls',
  );
  await expect(page.locator('.toast')).toHaveCount(0, { timeout: 10000 });
  for (const theme of ['light', 'dark'])
    for (const width of [390, 1440]) {
      await page.setViewportSize({ width, height: 1000 });
      await page.evaluate((value) => {
        document.documentElement.dataset.theme = value;
      }, theme);
      await section.evaluate((node) => {
        node.scrollIntoView({ block: 'start' });
        window.scrollBy(0, -76);
      });
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
      if (output)
        await page.screenshot({ path: resolve(output, `camera-light-${theme}-${width}.png`) });
    }
  assert.equal(writes.length, 2);
  assert.equal(
    requests.filter(({ path }) => /snapshot|stream|capture|preview/.test(path)).length,
    0,
  );
  assert.deepEqual(errors, []);
  checks.push(
    'Light/dark desktop/mobile layouts fit; no media requests, settings writes, or browser errors',
  );
} catch (error) {
  failure = error;
} finally {
  if (output)
    await writeFile(
      resolve(output, 'camera-light-smoke.json'),
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
  releaseRead?.();
  await browser.close();
  clearInterval(timer);
  for (const response of subscribers) response.end();
  await new Promise((done) => server.close(done));
}
if (failure) throw failure;
console.log(
  JSON.stringify({
    outcome: 'passed',
    browser: browserName,
    checks: checks.length,
    synthetic_control_writes: writes.length,
  }),
);
