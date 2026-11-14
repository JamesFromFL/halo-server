/** Synthetic bridge publication/pairing browser test. No Ring or HAP listener is started. */
import { chromium } from '@playwright/test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFile, mkdir, writeFile } from 'node:fs/promises';
import { resolve, dirname, extname, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const staticRoot = resolve(root, '../src/halo_server/web/static');
const output = process.env.HALO_BROWSER_OUTPUT;
if (output && !output.startsWith('/')) throw new Error('HALO_BROWSER_OUTPUT must be absolute');
if (output) await mkdir(output, { recursive: true });
const requests = [],
  publicationWrites = [],
  resetWrites = [],
  reveals = [],
  checks = [],
  errors = [];
const subscribers = new Set();
let reauths = 0;
let delayReveal = false;
let releaseReveal;
let unknownPublication = false;
let operationStatus = 'completed';
let pairingDigest = 'a'.repeat(64);
let backendFixture = null;
const runtime = {
  state: 'disabled',
  reason: null,
  boot_id: null,
  published: false,
  publication_enabled: false,
  publication_revision: 4,
  reset_pending: false,
  reset_operation_id: null,
};
const bridge = {
  revision: 9,
  configured: true,
  enabled: true,
  auto_import: true,
  accessory_uuid: '11111111-1111-4111-8111-111111111111',
  username: '02:00:00:00:00:01',
  capacity: 149,
  selected_count: 1,
  retained_count: 1,
  overflow_count: 0,
  included_devices: [],
  excluded_devices: [],
  excluded_locations: [],
  excluded_kinds: [],
  excluded_mappings: [],
  publication: 'implemented',
  runtime,
  status: 'ready',
  truncated: false,
  devices: [
    {
      id: 'sensor-one',
      accessory_uuid: '22222222-2222-4222-8222-222222222222',
      mappings: ['contact'],
      selected: true,
      excluded: false,
      eligible: true,
      reason: 'ready',
      present: true,
    },
  ],
};
const sensor = {
  id: 'sensor-one',
  name: 'Front door',
  kind: 'contact-sensor',
  model: 'Contact Sensor',
  location_id: 'home',
  location_name: 'Home',
  capabilities: ['contact'],
  observed: { contact_open: false },
  available: true,
  stale: false,
  observed_at: Date.now() / 1000,
  supported: true,
  unsupported_reason: null,
};
const monitoring = {
  schema: 1,
  revision: 3,
  policy_revision: 1,
  scope: { profile_id: '33333333-3333-4333-8333-333333333333', account_generation: 2 },
  state: 'ready',
  complete: true,
  observed_at: Date.now() / 1000,
  devices: [sensor],
  enabled: true,
  bridge,
  safety: { read_only: true, panic_enabled: false, emergency_dispatch_enabled: false },
};
const state = {
  schema: 1,
  epoch: 'fixture-account',
  observed_at: Date.now() / 1000,
  daemon: {
    boot_id: 'daemon-one',
    version: 'fixture',
    uptime_seconds: 10,
    stopping: false,
    release: { managed: false },
  },
  ring: { authenticated: true, restoring: false, events: 'connected', inventory_complete: true },
  scope: monitoring.scope,
  homekit: {
    enabled: true,
    adapter_boot_id: 'camera-child',
    state: 'published',
    registry_revision: 1,
    selected_count: 0,
    published_count: 0,
    recording_revision: 1,
  },
  devices: [],
  noncamera: monitoring,
  activity: { epoch: 'fixture-activity', cursor: 0, capacity: 256, entries: [], truncated: false },
  truncated: false,
};
const snapshot = () => ({
  schema: 1,
  web: { boot_id: 'web-one', observed_at: Date.now() / 1000 },
  daemon: { available: true, stale: false, observed_at: Date.now() / 1000, error: null },
  state: { ...state, observed_at: Date.now() / 1000 },
  cursor: String(Date.now()),
});
const pairing = () =>
  backendFixture?.pairing ?? {
    ...monitoring.scope,
    registry_revision: bridge.revision,
    publication_revision: runtime.publication_revision,
    accessory_id: bridge.accessory_uuid,
    username: bridge.username,
    boot_id: runtime.boot_id,
    available: runtime.state === 'ready',
    published: runtime.published,
    paired_known: runtime.state === 'ready',
    paired: false,
    paired_clients: 0,
    pairing_revision: runtime.published || runtime.reset_pending ? pairingDigest : null,
    pending_reset: runtime.reset_pending,
    reset_operation_id: runtime.reset_operation_id,
  };
const publish = () => {
  for (const response of subscribers)
    response.write(`event: state\ndata: ${JSON.stringify(snapshot())}\n\n`);
};
const server = createServer(async (request, response) => {
  const path = new URL(request.url, 'http://localhost').pathname;
  requests.push({ method: request.method, path });
  const send = (result, status = 200) => {
    response.writeHead(status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' });
    response.end(JSON.stringify({ ok: status < 400, result }));
  };
  if (path === '/api/web/v1/session')
    return send({
      authenticated: true,
      setup_required: false,
      csrf_token: 'fixture-csrf',
      reauthenticated: reauths > 0,
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
  if (path === '/api/web/v1/selection')
    return send({
      ...monitoring.scope,
      revision: 1,
      registry_revision: 1,
      boot_id: 'camera-child',
      cameras: [],
      total: 0,
    });
  if (path === '/api/web/v1/devices/bridge/pairing' && request.method === 'GET')
    return send(pairing());
  if (path.startsWith('/api/web/v1/operations/'))
    return send({ operation_id: path.split('/').at(-1), status: operationStatus });
  if (request.method === 'POST' && path.startsWith('/api/')) {
    assert.equal(request.headers['x-halo-csrf'], 'fixture-csrf');
    let body = '';
    for await (const chunk of request) body += chunk;
    const value = JSON.parse(body);
    if (path === '/api/web/v1/reauth') {
      assert.equal(value.password, 'fixture-password-123');
      reauths++;
      return send({ authenticated: true });
    }
    if (path === '/api/web/v1/devices/bridge/publication') {
      assert.equal(value.profile_id, monitoring.scope.profile_id);
      assert.equal(value.account_generation, monitoring.scope.account_generation);
      assert.equal(value.expected_registry_revision, bridge.revision);
      assert.equal(value.expected_revision, runtime.publication_revision);
      publicationWrites.push(value);
      runtime.publication_enabled = value.enabled;
      runtime.publication_revision++;
      runtime.published = value.enabled;
      runtime.state = value.enabled ? 'ready' : 'disabled';
      runtime.boot_id = value.enabled ? `bridge-child-${runtime.publication_revision}` : null;
      publish();
      return send({
        operation_id: value.operation_id,
        status: unknownPublication ? 'unknown' : 'completed',
        possibly_sent: unknownPublication,
      });
    }
    const identity = pairing();
    for (const key of [
      'profile_id',
      'account_generation',
      'registry_revision',
      'accessory_id',
      'boot_id',
      'pairing_revision',
    ])
      assert.equal(value[key], identity[key], `scoped ${key}`);
    assert.ok(reauths > 0, 'pairing action requires reauthentication');
    if (path === '/api/web/v1/devices/bridge/pairing/reveal') {
      reveals.push(value);
      const secret = backendFixture?.secret ?? {
        ...identity,
        pin: '123-45-678',
        setup_uri: 'X-HM://0023ABCD1WXYZ',
        expires_in_ms: 60_000,
      };
      if (delayReveal) await new Promise((resolve) => (releaseReveal = resolve));
      return send(secret);
    }
    if (path === '/api/web/v1/devices/bridge/pairing/reset') {
      resetWrites.push(value);
      if (!runtime.reset_pending) {
        runtime.reset_pending = true;
        runtime.reset_operation_id = value.operation_id;
        runtime.publication_enabled = false;
        runtime.publication_revision++;
        runtime.published = false;
        runtime.boot_id = null;
        runtime.state = 'reset_pending';
        publish();
        return send({ operation_id: value.operation_id, status: 'pending' });
      }
      assert.equal(
        value.operation_id,
        runtime.reset_operation_id,
        'reset resumes original operation',
      );
      runtime.reset_pending = false;
      runtime.reset_operation_id = null;
      runtime.state = 'disabled';
      runtime.publication_revision++;
      pairingDigest = 'b'.repeat(64);
      publish();
      return send({ operation_id: value.operation_id, status: 'completed' });
    }
    throw new Error(`Unexpected mutation ${path}`);
  }
  if (path.startsWith('/api/')) return send({}, 404);
  const file = resolve(staticRoot, `.${path === '/' ? '/index.html' : path}`);
  if (relative(staticRoot, file).startsWith('..')) return send({}, 404);
  try {
    const bytes = await readFile(file);
    response.writeHead(200, {
      'Content-Type':
        { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.png': 'image/png' }[
          extname(file)
        ] ?? 'text/plain',
    });
    response.end(bytes);
  } catch {
    response.writeHead(404);
    response.end();
  }
});
await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
const timer = setInterval(publish, 1000);
const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
const page = await context.newPage();
page.on('pageerror', (error) => errors.push(error.message));
async function confirmPassword() {
  const dialog = page.getByRole('dialog', { name: 'Confirm your Halo password' });
  await dialog.waitFor();
  await dialog.getByLabel('Password', { exact: true }).fill('fixture-password-123');
  await dialog.getByRole('button', { name: 'Confirm', exact: true }).click();
}
try {
  await page.goto(`http://127.0.0.1:${server.address().port}/#/integrations`);
  const toggle = page.getByRole('switch', { name: 'Connect Halo Bridge to Apple Home' });
  const reveal = page.getByRole('button', { name: 'Show bridge pairing code' });
  await toggle.waitFor();
  await page.waitForFunction(
    () => !document.querySelector('[aria-label="Connect Halo Bridge to Apple Home"]').disabled,
  );
  assert.equal(await toggle.isChecked(), false);
  assert.equal(publicationWrites.length, 0);
  assert.equal(await page.locator('.pairing-secret').count(), 0);
  assert.equal(
    await page.getByRole('region', { name: 'Halo Bridge HomeKit connection' }).count(),
    1,
  );
  await toggle.click();
  await page.waitForFunction(
    () =>
      ![...document.querySelectorAll('button')].find((b) =>
        b.textContent.includes('Show bridge pairing code'),
      ).disabled,
  );
  assert.equal(publicationWrites.length, 1);
  checks.push('prepared bridge never publishes automatically; one explicit scoped publication');
  await reveal.click();
  await page.getByRole('dialog').getByRole('button', { name: 'Cancel', exact: true }).click();
  assert.equal(reveals.length, 0);
  await reveal.click();
  await confirmPassword();
  await page.locator('.pairing-secret').waitFor();
  assert.equal(await page.locator('.pairing-secret canvas').count(), 1);
  assert.equal(await page.locator('.pairing-pin').textContent(), '123-45-678');
  await page.evaluate(() => {
    Object.defineProperty(document, 'hidden', { configurable: true, value: true });
    document.dispatchEvent(new Event('visibilitychange'));
  });
  await page.locator('.pairing-secret').waitFor({ state: 'hidden' });
  await page.evaluate(() => {
    Object.defineProperty(document, 'hidden', { configurable: true, value: false });
    document.dispatchEvent(new Event('visibilitychange'));
  });
  await page.waitForFunction(
    () =>
      ![...document.querySelectorAll('button')].find((b) =>
        b.textContent.includes('Show bridge pairing code'),
      ).disabled,
  );
  checks.push(
    'pairing reveal requires password confirmation; cancel sends nothing; secret hides when tab hidden',
  );
  await page.evaluate(() => {
    location.hash = '/devices/noncamera%3Asensor-one';
  });
  await page.getByRole('heading', { name: 'Front door', exact: true }).waitFor();
  assert.equal(await page.getByRole('button', { name: /pairing code/i }).count(), 0);
  assert.equal(await page.locator('.pairing-secret').count(), 0);
  await page.getByRole('link', { name: 'Manage Halo Bridge', exact: true }).click();
  await reveal.waitFor();
  checks.push('child sensor inherits the single bridge pairing and has no setup code');
  delayReveal = true;
  await reveal.click();
  await confirmPassword();
  while (!releaseReveal) await new Promise((resolve) => setTimeout(resolve, 10));
  monitoring.scope.profile_id = '44444444-4444-4444-8444-444444444444';
  monitoring.scope.account_generation++;
  bridge.accessory_uuid = '55555555-5555-4555-8555-555555555555';
  runtime.boot_id = 'new-account-child';
  publish();
  releaseReveal();
  delayReveal = false;
  await page.waitForTimeout(100);
  assert.equal(await page.locator('.pairing-secret').count(), 0);
  assert.equal(publicationWrites.length, 1, 'account change never publishes itself');
  checks.push('late setup secret discarded after account and bridge identity change');
  await page.getByRole('button', { name: 'Refresh bridge', exact: true }).click();
  await page.waitForFunction(
    () =>
      ![...document.querySelectorAll('button')].find((b) => b.textContent.includes('Reset bridge…'))
        .disabled,
  );
  await page.getByRole('button', { name: 'Reset bridge…', exact: true }).click();
  await page.getByRole('dialog').getByRole('button', { name: 'Cancel', exact: true }).click();
  assert.equal(resetWrites.length, 0);
  await page.getByRole('button', { name: 'Reset bridge…', exact: true }).click();
  await page.getByRole('button', { name: 'Confirm bridge reset', exact: true }).click();
  await confirmPassword();
  await page.getByRole('button', { name: 'Resume bridge reset…', exact: true }).waitFor();
  assert.equal(resetWrites.length, 1);
  assert.equal(runtime.publication_enabled, false);
  await page.reload();
  await page.getByRole('button', { name: 'Resume bridge reset…', exact: true }).waitFor();
  assert.equal(resetWrites.length, 1, 'no automatic destructive resume after reload');
  await page.getByRole('button', { name: 'Resume bridge reset…', exact: true }).click();
  await page.getByRole('button', { name: 'Confirm bridge reset', exact: true }).click();
  await confirmPassword();
  await page.getByText('Halo Bridge pairing reset.', { exact: true }).waitFor();
  assert.equal(resetWrites.length, 2);
  assert.equal(resetWrites[0].operation_id, resetWrites[1].operation_id);
  assert.equal(resetWrites[1].boot_id, null);
  assert.equal(resetWrites[1].pairing_revision, resetWrites[0].pairing_revision);
  assert.equal(await toggle.isChecked(), false);
  assert.equal(publicationWrites.length, 1);
  checks.push(
    'reset requires confirmation and reauthentication; resumes exact manifest/operation only; completed reset stays disconnected',
  );
  unknownPublication = true;
  await toggle.click();
  await page.getByText('Halo may have saved these preferences.', { exact: false }).waitFor();
  assert.equal(await toggle.isDisabled(), true);
  const writes = publicationWrites.length;
  await page.getByRole('button', { name: 'Check outcome', exact: true }).click();
  await page.waitForFunction(
    () => !document.querySelector('[aria-label="Connect Halo Bridge to Apple Home"]').disabled,
  );
  assert.equal(publicationWrites.length, writes);
  checks.push('uncertain publication is checked without replay');
  for (const theme of ['light', 'dark']) {
    await page.evaluate((theme) => {
      document.documentElement.dataset.theme = theme;
    }, theme);
    for (const width of [320, 768, 1440]) {
      await page.setViewportSize({ width, height: 1000 });
      assert.equal(
        await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth),
        true,
      );
      if (output)
        await page.screenshot({
          path: resolve(output, `bridge-${theme}-${width}.png`),
          fullPage: true,
        });
    }
  }
  checks.push('six responsive light/dark layouts without overflow');
  if (process.env.HALO_BRIDGE_PUBLICATION_FIXTURE) {
    const fixtures = JSON.parse(
      await readFile(process.env.HALO_BRIDGE_PUBLICATION_FIXTURE, 'utf8'),
    );
    const originalWrites = publicationWrites.length;
    for (const name of ['prepared', 'published', 'pending']) {
      backendFixture = fixtures[name];
      state.noncamera = backendFixture.monitoring;
      state.scope = backendFixture.monitoring.scope;
      state.epoch = `actual-backend-${name}`;
      publish();
      await page.getByRole('button', { name: 'Refresh bridge', exact: true }).waitFor();
      await page.waitForFunction(
        () =>
          [...document.querySelectorAll('button')].find((button) =>
            button.textContent.includes('Refresh bridge'),
          )?.disabled === false,
      );
      await page.getByRole('button', { name: 'Refresh bridge', exact: true }).click();
      await page.waitForFunction(
        () =>
          [...document.querySelectorAll('button')].find((button) =>
            button.textContent.includes('Refresh bridge'),
          )?.disabled === false,
      );
      assert.equal(await toggle.isChecked(), name === 'published');
      assert.equal(await reveal.isDisabled(), name !== 'published');
      if (name === 'published') {
        await reveal.click();
        await confirmPassword();
        await page.locator('.pairing-secret').waitFor();
        assert.equal(await page.locator('.pairing-pin').textContent(), backendFixture.secret.pin);
        assert.equal(await page.locator('.pairing-secret canvas').count(), 1);
        await page.getByRole('button', { name: 'Hide pairing code', exact: true }).click();
      } else if (name === 'pending') {
        assert.equal(
          await page.getByRole('button', { name: 'Resume bridge reset…', exact: true }).isEnabled(),
          true,
        );
        assert.equal(await toggle.isDisabled(), true);
        if (output)
          await page.screenshot({
            path: resolve(output, 'bridge-backend-pending.png'),
            fullPage: true,
          });
      }
      assert.equal(publicationWrites.length, originalWrites);
    }
    checks.push(
      'actual Python prepared, published, reveal, and interrupted-reset DTOs render without auto-publication or reset',
    );
  }
  assert.equal(
    requests.filter(
      (r) =>
        r.method === 'POST' &&
        /\/controls|\/selection|\/devices\/monitoring|\/devices\/bridge$/.test(r.path),
    ).length,
    0,
  );
  assert.deepEqual(errors, []);
  const receipt = {
    checks,
    publicationWrites: publicationWrites.length,
    reveals: reveals.length,
    resetRequests: resetWrites.length,
    reauths,
    pageErrors: errors,
  };
  if (output)
    await writeFile(resolve(output, 'receipt.json'), JSON.stringify(receipt, null, 2) + '\n');
  process.stdout.write(JSON.stringify(receipt, null, 2) + '\n');
} finally {
  clearInterval(timer);
  releaseReveal?.();
  await browser.close();
  for (const response of subscribers) response.end();
  server.closeAllConnections();
  await new Promise((resolve) => server.close(resolve));
}
