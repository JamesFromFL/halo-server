/** Page lifecycle/availability regression fixture. No real accounts, media, or controls. */
import { testBrowser } from './browser-engine.mjs';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFile, mkdir, writeFile } from 'node:fs/promises';
import { dirname, extname, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const staticRoot = process.env.HALO_STATIC_ROOT || resolve(root, '../src/halo_server/web/static');
assert.ok(staticRoot.startsWith('/'));
const output = process.env.HALO_BROWSER_OUTPUT;
if (output) {
  assert.ok(output.startsWith('/'));
  await mkdir(output, { recursive: true });
}
const errors = [],
  checks = [],
  writes = [],
  replayRequests = [],
  sensitiveRequests = [];
const replayFixture = process.env.HALO_STATE_FIXTURE
  ? JSON.parse(await readFile(process.env.HALO_STATE_FIXTURE, 'utf8'))
  : null;
let replay = false;
let replaySummary = null;
const subscribers = new Set();
let available = false;
let account = 1;
let holdSelection = false,
  releaseSelection;
let holdOperation = false,
  releaseOperation;
let holdDoctor = false,
  releaseDoctor;
const scope = () => ({ profile_id: `fixture-account-${account}`, account_generation: account });
const camera = () => ({
  id: 'camera-one',
  camera_id: '1',
  name: `Camera account ${account}`,
  model: 'Floodlight',
  source: 'ring',
  kind: 'camera',
  scope: { ...scope(), device_generation: 1 },
  capabilities: [],
  observed: { source_available: true, battery_percent: null, light: null, siren: null },
  homekit: {
    selected: true,
    published: true,
    paired: null,
    owner_warm: false,
    recording_active: false,
    recording_audio_active: false,
  },
});
const sensor = () => ({
  id: 'sensor-one',
  name: 'Contact with stale battery',
  kind: 'contact-sensor',
  model: 'Contact Sensor',
  provider: 'ring-hub',
  family: 'hub',
  location_id: 'home',
  location_name: 'Home',
  capabilities: ['contact'],
  observed: { contact_open: false, battery_percent: null },
  available: null,
  source_health: 'online',
  stale: true,
  observed_at: Date.now() / 1000,
  supported: true,
  unsupported_reason: null,
});
function snapshot() {
  const now = Date.now() / 1000;
  if (replay) {
    const value = structuredClone(replayFixture);
    // Keep observation contents intact; only the delivery envelope is current.
    value.web.observed_at = now;
    value.daemon.observed_at = now;
    value.daemon.available = true;
    value.daemon.stale = false;
    value.state.observed_at = now;
    value.cursor = String(Date.now());
    return value;
  }
  return {
    schema: 1,
    cursor: String(Date.now()),
    web: { boot_id: 'web-fixture', observed_at: now },
    daemon: { available, stale: !available, observed_at: available ? now : null, error: null },
    state: !available
      ? null
      : {
          schema: 1,
          epoch: `account-${account}`,
          observed_at: now,
          daemon: {
            boot_id: 'daemon-fixture',
            version: 'fixture',
            uptime_seconds: 10,
            stopping: false,
            release: { managed: false },
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
            adapter_boot_id: 'adapter-fixture',
            state: 'published',
            registry_revision: account,
            selected_count: 1,
            published_count: 1,
            recording_revision: 0,
          },
          devices: [camera()],
          noncamera: {
            schema: 1,
            revision: 1,
            policy_revision: 1,
            scope: scope(),
            state: 'partial',
            complete: false,
            observed_at: now,
            devices: [sensor()],
            enabled: true,
            truncated: false,
            safety: { read_only: true, panic_enabled: false, emergency_dispatch_enabled: false },
          },
          activity: {
            epoch: `activity-${account}`,
            cursor: 1,
            capacity: 256,
            truncated: false,
            entries: [
              {
                sequence: 1,
                at: now,
                category: 'camera',
                action: 'camera_selected',
                outcome: 'completed',
                camera_id: '1',
              },
            ],
          },
          truncated: false,
        },
  };
}
function publish() {
  for (const response of subscribers)
    response.write(`event: state\ndata: ${JSON.stringify(snapshot())}\n\n`);
}
function selection() {
  if (replay) return replayCameraPage('selection');
  return {
    ...scope(),
    boot_id: 'adapter-fixture',
    revision: account,
    registry_revision: account,
    total: 1,
    cameras: [{ camera_id: '1', name: camera().name, selected: true, available: true }],
  };
}
// Administrative GETs are deliberately synthetic. The sanitized owner-state replay
// supplies the actual device shapes, not pairing secrets or live control authority.
function replayCameraPage(kind) {
  const state = replay ? replayFixture.state : snapshot().state;
  return {
    ...state.scope,
    boot_id: state.homekit.adapter_boot_id,
    revision: state.homekit.registry_revision,
    registry_revision: state.homekit.registry_revision,
    available: true,
    total: state.devices.length,
    warm_budget: { capacity: null, reserved: 0 },
    cameras: state.devices.map((device) => ({
      ...device.scope,
      camera_id: device.camera_id,
      name: device.name,
      selected: device.homekit.selected,
      available: device.observed.source_available,
      ...(kind === 'pairing'
        ? {
            published: device.homekit.published,
            paired_known: !replay,
            paired: false,
            pairing_revision: replay ? null : 'fixture-pairing-revision',
            pending_reset: false,
          }
        : {}),
      ...(kind === 'recording'
        ? {
            owner_warm: device.homekit.owner_warm,
            mains_confirmed: false,
            recording_supported: device.capabilities.includes('video'),
            warm_state: 'disabled',
          }
        : {}),
      ...(kind === 'controls'
        ? {
            has_light: device.capabilities.includes('light'),
            has_siren: device.capabilities.includes('siren'),
          }
        : {}),
    })),
  };
}
const server = createServer(async (request, response) => {
  const path = new URL(request.url, 'http://localhost').pathname;
  if (path.startsWith('/api/') && /reveal|reset|command|recording\/warm/.test(path))
    sensitiveRequests.push(path);
  if (replay && path.startsWith('/api/')) {
    replayRequests.push({ method: request.method, path });
    if (request.method !== 'GET') {
      response.writeHead(405);
      response.end();
      return;
    }
  }
  const send = (result) => {
    response.writeHead(200, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' });
    response.end(JSON.stringify({ ok: true, result }));
  };
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
  if (['pairing', 'recording', 'controls'].some((name) => path === `/api/web/v1/${name}`))
    return send(replayCameraPage(path.split('/').at(-1)));
  if (path === '/api/web/v1/selection' && request.method === 'GET') {
    if (!available) {
      response.writeHead(503, { 'Content-Type': 'application/json' });
      response.end(
        JSON.stringify({
          ok: false,
          error: { code: 'daemon_unavailable', message: 'Fixture daemon unavailable.' },
        }),
      );
      return;
    }
    const value = selection();
    if (holdSelection) {
      holdSelection = false;
      releaseSelection = () => send(value);
      return;
    }
    return send(value);
  }
  if (path === '/api/web/v1/selection' && request.method === 'POST') {
    let body = '';
    for await (const part of request) body += part;
    const value = JSON.parse(body);
    writes.push(value);
    return send({ operation_id: value.operation_id, status: 'unknown', possibly_sent: true });
  }
  if (path.startsWith('/api/web/v1/operations/')) {
    const value = {
      operation_id: path.split('/').at(-1),
      status: 'completed',
      message: 'OLD OPERATION RESULT',
    };
    if (holdOperation) {
      holdOperation = false;
      releaseOperation = () => send(value);
      return;
    }
    return send(value);
  }
  if (path === '/api/web/v1/doctor') {
    const value = {
      daemon_available: available,
      web: { ready: true },
      daemon: { local: { checks: [{ name: 'fixture', status: 'pass', code: 'healthy' }] } },
    };
    if (holdDoctor) {
      holdDoctor = false;
      releaseDoctor = () => send(value);
      return;
    }
    return send(value);
  }
  if (path.startsWith('/api/')) {
    response.writeHead(404);
    response.end();
    return;
  }
  const file = resolve(staticRoot, path === '/' ? 'index.html' : `.${path}`);
  if (relative(staticRoot, file).startsWith('..')) {
    response.writeHead(403);
    response.end();
    return;
  }
  try {
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
  } catch {
    response.writeHead(404);
    response.end();
  }
});
await new Promise((done) => server.listen(0, '127.0.0.1', done));
const url = `http://127.0.0.1:${server.address().port}`;
async function until(ready) {
  const deadline = Date.now() + 5000;
  while (!ready()) {
    assert.ok(Date.now() < deadline, 'Fixture request arrived within five seconds');
    await new Promise((done) => setTimeout(done, 10));
  }
}
const browser = await testBrowser.launch({ headless: true });
const context = await browser.newContext({ viewport: { width: 1280, height: 1000 } });
const page = await context.newPage();
page.on('pageerror', (error) => errors.push(error.message));
const navigate = async (path, heading) => {
  await page.evaluate((path) => {
    location.hash = path;
  }, path);
  await page.getByRole('heading', { name: heading, exact: true, level: 2 }).waitFor();
};
try {
  await page.goto(`${url}/#/devices`);
  await page
    .getByText('Camera inventory is unavailable while Halo reconnects.', { exact: false })
    .waitFor();
  await page
    .getByText('Other device inventory is unavailable while Halo reconnects.', { exact: true })
    .waitFor();
  await navigate('/devices/camera-one', 'Device information is unavailable');
  assert.equal(
    await page.getByRole('heading', { name: 'Device is no longer available', exact: true }).count(),
    0,
  );
  await navigate('/activity', 'Activity');
  await page.getByText('Waiting for current activity.', { exact: true }).waitFor();
  assert.equal(await page.getByLabel('Filter activity type').isDisabled(), true);
  await navigate('/integrations', 'Integrations');
  await page.getByText('Camera selection is unavailable.', { exact: false }).waitFor();
  checks.push('Unavailable data is not presented as removed cameras or empty history');

  available = true;
  publish();
  await page.getByLabel('Expose Camera account 1 in Apple Home', { exact: true }).waitFor();
  holdSelection = true;
  await page.getByRole('button', { name: 'Refresh', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('button') !== null);
  await until(() => releaseSelection);
  account = 2;
  publish();
  await page.getByLabel('Expose Camera account 2 in Apple Home', { exact: true }).waitFor();
  releaseSelection();
  releaseSelection = undefined;
  await page.waitForTimeout(150);
  assert.equal(
    await page.getByLabel('Expose Camera account 1 in Apple Home', { exact: true }).count(),
    0,
  );
  checks.push('Late camera selection read cannot restore the prior account');

  await page.getByLabel('Expose Camera account 2 in Apple Home', { exact: true }).click();
  await page.getByRole('button', { name: 'Check outcome', exact: true }).waitFor();
  holdOperation = true;
  await page.getByRole('button', { name: 'Check outcome', exact: true }).click();
  await until(() => releaseOperation);
  account = 3;
  publish();
  await page.getByLabel('Expose Camera account 3 in Apple Home', { exact: true }).waitFor();
  releaseOperation();
  releaseOperation = undefined;
  await page.waitForTimeout(150);
  assert.equal(await page.getByText('OLD OPERATION RESULT', { exact: true }).count(), 0);
  assert.equal(await page.locator('.operation').count(), 0);
  checks.push('Late operation checks cannot restore another account’s operation');

  await navigate('/devices/noncamera%3Asensor-one', 'Contact with stale battery');
  await page.getByText('Some observations are out of date.', { exact: false }).waitFor();
  const contact = page
    .locator('.preference-row')
    .filter({ has: page.locator('.row-label > span').filter({ hasText: /^Contact$/ }) });
  await contact.getByText('Closed', { exact: true }).waitFor();
  const battery = page
    .locator('.preference-row')
    .filter({ has: page.locator('.row-label > span').filter({ hasText: /^Battery$/ }) });
  await battery.getByText('Unknown', { exact: true }).waitFor();
  checks.push('Stale optional fields do not suppress current normalized sensor observations');

  await navigate('/activity', 'Activity');
  await page.getByLabel('Filter activity type').selectOption('camera');
  await page.getByRole('button', { name: 'Clear activity filters', exact: true }).click();
  assert.equal(await page.getByLabel('Filter activity type').inputValue(), '');
  checks.push('Activity filters can be cleared');

  await navigate('/diagnostics', 'Diagnostics');
  holdDoctor = true;
  await page.getByRole('button', { name: 'Run check', exact: true }).click();
  await until(() => releaseDoctor);
  await navigate('/activity', 'Activity');
  releaseDoctor();
  releaseDoctor = undefined;
  await page.waitForTimeout(150);
  assert.equal(await page.getByText('Health check completed.', { exact: true }).count(), 0);
  await navigate('/diagnostics', 'Diagnostics');
  await page.getByRole('button', { name: 'Run check', exact: true }).click();
  await page.getByRole('heading', { name: 'Doctor findings', exact: true }).waitFor();
  await page.getByText('Run another check to update this report.', { exact: false }).waitFor();
  checks.push('Diagnostics ignore abandoned results and timestamp completed reports');
  await navigate('/devices/camera-one', 'Camera account 3');
  const resetButton = page.getByRole('button', { name: 'Reset…', exact: true });
  await resetButton.click();
  const resetDialog = page.getByRole('dialog', { name: 'Reset Camera account 3?', exact: true });
  await resetDialog.waitFor();
  const firstTitle = await resetDialog.getAttribute('aria-labelledby');
  assert.ok(firstTitle && firstTitle !== 'confirmation-title');
  await page.keyboard.press('Escape');
  await resetDialog.waitFor({ state: 'detached' });
  await resetButton.click();
  await page.getByRole('button', { name: 'Confirm reset', exact: true }).click();
  const authDialog = page.getByRole('dialog', { name: 'Confirm your Halo password', exact: true });
  await authDialog.waitFor();
  const authTitle = await authDialog.getAttribute('aria-labelledby');
  assert.notEqual(firstTitle, authTitle, 'Each confirmation owns its accessible label');
  assert.equal(
    await page.locator('dialog[open]').count(),
    1,
    'Confirmation retires before authentication',
  );
  await page.keyboard.press('Escape');
  await authDialog.waitFor({ state: 'detached' });
  assert.deepEqual(sensitiveRequests, [], 'Cancelled confirmations send no sensitive command');
  checks.push(
    'Confirmation dialogs have unique accessible labels and Escape cancels without a command',
  );
  for (const width of [390, 768, 1440]) {
    await page.setViewportSize({ width, height: 1000 });
    for (const [path, heading] of [
      ['/devices', 'Devices'],
      ['/integrations', 'Integrations'],
      ['/activity', 'Activity'],
      ['/diagnostics', 'Diagnostics'],
    ]) {
      await navigate(path, heading);
      assert.ok(
        await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1),
        `${path} fits ${width}px`,
      );
      if (output)
        await page.screenshot({
          path: `${output}/${heading.toLowerCase()}-${width}.png`,
          fullPage: true,
        });
    }
  }
  if (replayFixture) {
    assert.equal(replayFixture.schema, 1);
    assert.ok(
      replayFixture.state?.noncamera?.devices.length,
      'Replay includes noncamera inventory',
    );
    replay = true;
    publish();
    const cameras = replayFixture.state.devices;
    const devices = replayFixture.state.noncamera.devices;
    const kindCounts = Object.fromEntries(
      [...new Set(devices.map((device) => device.kind))]
        .sort()
        .map((kind) => [kind, devices.filter((device) => device.kind === kind).length]),
    );
    let detailViews = 0;
    for (const width of [390, 1440]) {
      await page.setViewportSize({ width, height: 1000 });
      await navigate('/devices', 'Devices');
      await page
        .getByText(`${devices.length} of ${devices.length} inventory entries shown.`, {
          exact: true,
        })
        .waitFor();
      assert.equal(await page.locator('a.device-link').count(), cameras.length + devices.length);
      for (const device of devices) {
        await navigate(`/devices/${encodeURIComponent(`noncamera:${device.id}`)}`, device.name);
        await page.getByRole('heading', { name: 'Device', exact: true, level: 3 }).waitFor();
        assert.ok(
          await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1),
          `${device.kind} detail fits ${width}px`,
        );
        detailViews++;
      }
      for (const device of cameras) {
        await navigate(`/devices/${encodeURIComponent(device.id)}`, device.name);
        await page.getByLabel('Enable HomeKit Secure Video support', { exact: true }).waitFor();
        await page.waitForFunction(
          () => !document.body.textContent.includes('Loading device administration…'),
        );
        assert.equal(
          await page.locator('.notice.error').count(),
          0,
          'Camera details load without an error',
        );
        assert.equal(
          await page.getByRole('button', { name: 'Reveal code', exact: true }).isDisabled(),
          true,
          'Unknown pairing state never offers a secret',
        );
        assert.ok(
          await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1),
          `Camera detail fits ${width}px`,
        );
        detailViews++;
      }
      for (const [path, heading] of [
        ['/devices', 'Devices'],
        ['/integrations', 'Integrations'],
        ['/activity', 'Activity'],
        ['/diagnostics', 'Diagnostics'],
      ]) {
        await navigate(path, heading);
        assert.ok(
          await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1),
          `Replay ${path} fits ${width}px`,
        );
        if (output)
          await page.screenshot({
            path: `${output}/replay-${heading.toLowerCase()}-${width}.png`,
            fullPage: true,
          });
      }
    }
    assert.ok(replayRequests.length > 0);
    assert.ok(
      replayRequests.every((request) => request.method === 'GET'),
      'Inventory replay sends no writes',
    );
    assert.ok(
      replayRequests.every((request) => !/reveal|reset|snapshot|media|command/.test(request.path)),
      'Inventory replay requests no secrets, media, resets, or commands',
    );
    replaySummary = {
      cameras: cameras.length,
      noncamera: devices.length,
      kinds: kindCounts,
      detail_views: detailViews,
      layouts: 8,
      reads: replayRequests.length,
      writes: 0,
    };
    checks.push(
      'Sanitized owner-state replay renders every camera and noncamera detail without writes',
    );
  }
  assert.deepEqual(errors, []);
  assert.equal(writes.length, 1, 'Only the explicit synthetic selection action was sent');
  const report = {
    checks,
    browser_errors: errors,
    layouts: 12,
    synthetic_selection_writes: writes.length,
    replay: replaySummary,
  };
  if (output) await writeFile(`${output}/pages-smoke.json`, JSON.stringify(report, null, 2) + '\n');
  console.log(JSON.stringify(report));
} finally {
  releaseSelection?.();
  releaseOperation?.();
  releaseDoctor?.();
  await context.close();
  await browser.close();
  for (const response of subscribers) response.end();
  server.closeAllConnections();
  await new Promise((done) => server.close(done));
}
