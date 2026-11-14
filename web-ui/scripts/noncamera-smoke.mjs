/** Isolated browser fixture: no Ring credentials, HomeKit publication, or physical commands. */
import { testBrowser } from './browser-engine.mjs';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFile, mkdir, writeFile } from 'node:fs/promises';
import { resolve, dirname, extname, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const staticRoot = process.env.HALO_STATIC_ROOT || resolve(root, '../src/halo_server/web/static');
const output = process.env.HALO_BROWSER_OUTPUT;
if (output && !output.startsWith('/')) throw new Error('HALO_BROWSER_OUTPUT must be absolute');
if (output) await mkdir(output, { recursive: true });
const requests = [];
const mutations = [];
const bridgeMutations = [];
let uncertainBridgeSave = false;
let delayBridgeSave = false;
let finishDelayedSave;
let delayedOperationId;
const subscribers = new Set();
const errors = [];
const checks = [];
const sensor = {
  id: 'sensor-one',
  name: 'Front door',
  kind: 'contact-sensor',
  model: 'Contact Sensor',
  location_id: 'home',
  location_name: 'Home',
  capabilities: ['contact'],
  observed: { contact_open: false, battery_percent: null, tampered: false },
  available: true,
  stale: false,
  observed_at: Date.now() / 1000,
  supported: true,
  unsupported_reason: null,
};
const bridge = {
  revision: 4,
  configured: true,
  enabled: true,
  auto_import: true,
  accessory_uuid: 'bridge-one',
  username: 'CA:FE:00:00:00:01',
  capacity: 149,
  selected_count: 2,
  retained_count: 2,
  overflow_count: 0,
  included_devices: [],
  excluded_devices: ['missing-device'],
  excluded_locations: ['missing-location'],
  excluded_kinds: ['missing-kind'],
  excluded_mappings: ['smoke'],
  publication: 'not_implemented',
  status: 'ready',
  truncated: false,
  devices: [
    {
      id: 'sensor-one',
      accessory_uuid: 'sensor-identity',
      mappings: ['contact'],
      selected: true,
      excluded: false,
      eligible: true,
      reason: null,
      present: true,
    },
    {
      id: 'motion-one',
      accessory_uuid: 'motion-identity',
      mappings: ['motion'],
      selected: true,
      excluded: false,
      eligible: true,
      reason: null,
      present: true,
    },
    {
      id: 'panic-one',
      accessory_uuid: null,
      mappings: [],
      selected: false,
      excluded: false,
      eligible: false,
      reason: 'unsupported-mapping',
      present: true,
    },
  ],
};
const monitoring = {
  schema: 1,
  bridge,
  revision: 8,
  policy_revision: 3,
  scope: { profile_id: 'test-profile', account_generation: 2 },
  state: 'partial',
  complete: false,
  observed_at: Date.now() / 1000,
  devices: [
    sensor,
    {
      ...sensor,
      id: 'motion-one',
      name: 'Garage motion',
      kind: 'motion-sensor',
      model: 'Motion Sensor',
      location_id: 'garage',
      location_name: 'Garage',
      observed: { motion_detected: true },
      stale: true,
    },
    {
      ...sensor,
      id: 'panic-one',
      name: 'Panic button',
      kind: 'panic-button',
      model: 'Panic Button',
      observed: {},
      supported: false,
      unsupported_reason: 'Control is disabled in this release.',
    },
  ],
  safety: { read_only: true, panic_enabled: false, emergency_dispatch_enabled: false },
  enabled: true,
};
const state = {
  schema: 1,
  epoch: 'test-account',
  observed_at: Date.now() / 1000,
  daemon: {
    boot_id: 'daemon-test',
    version: 'fixture',
    uptime_seconds: 10,
    stopping: false,
    release: { managed: false },
  },
  ring: { authenticated: true, restoring: false, events: 'connected', inventory_complete: true },
  scope: monitoring.scope,
  homekit: {
    enabled: true,
    adapter_boot_id: 'adapter-test',
    state: 'published',
    registry_revision: 1,
    selected_count: 1,
    published_count: 1,
    recording_revision: 1,
  },
  devices: [
    {
      id: 'camera-one',
      camera_id: '42',
      name: 'Floodlight',
      model: 'Floodlight Camera',
      source: 'ring',
      kind: 'camera',
      scope: { ...monitoring.scope, device_generation: 1 },
      capabilities: [],
      observed: { source_available: true, battery_percent: null, light: false, siren: false },
      homekit: {
        selected: true,
        published: true,
        paired: true,
        owner_warm: false,
        recording_active: false,
        recording_audio_active: false,
      },
    },
  ],
  noncamera: monitoring,
  activity: { epoch: 'activity', cursor: 0, capacity: 256, entries: [], truncated: false },
  truncated: false,
};
const snapshot = () => ({
  schema: 1,
  web: { boot_id: 'web-test', observed_at: Date.now() / 1000 },
  daemon: { available: true, stale: false, observed_at: Date.now() / 1000, error: null },
  state,
  cursor: String(Date.now()),
});
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
      csrf_token: 'test-csrf',
      reauthenticated: true,
      session_id: 'test-session',
    });
  if (path === '/api/web/v1/state') return send(snapshot());
  if (path === '/api/web/v1/events') {
    response.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-store' });
    subscribers.add(response);
    publish();
    request.on('close', () => subscribers.delete(response));
    return;
  }
  if (path === '/api/web/v1/devices/monitoring' && request.method === 'POST') {
    let body = '';
    for await (const chunk of request) body += chunk;
    const value = JSON.parse(body);
    mutations.push(value);
    assert.equal(request.headers['x-halo-csrf'], 'test-csrf');
    assert.equal(value.expected_revision, monitoring.policy_revision);
    assert.equal(value.profile_id, monitoring.scope.profile_id);
    assert.equal(value.account_generation, monitoring.scope.account_generation);
    monitoring.enabled = value.enabled;
    monitoring.state = value.enabled ? 'partial' : 'disabled';
    monitoring.policy_revision++;
    publish();
    return send({ operation_id: value.operation_id, status: 'completed' });
  }
  if (path === '/api/web/v1/devices/bridge' && request.method === 'POST') {
    let body = '';
    for await (const chunk of request) body += chunk;
    const value = JSON.parse(body);
    bridgeMutations.push(value);
    assert.equal(request.headers['x-halo-csrf'], 'test-csrf');
    assert.equal(value.expected_revision, bridge.revision);
    assert.equal(value.profile_id, monitoring.scope.profile_id);
    assert.equal(value.account_generation, monitoring.scope.account_generation);
    assert.ok(value.excluded_devices.includes('missing-device'));
    assert.ok(value.excluded_locations.includes('missing-location'));
    assert.ok(value.excluded_kinds.includes('missing-kind'));
    assert.ok(value.excluded_mappings.includes('smoke'));
    if (delayBridgeSave) {
      delayedOperationId = value.operation_id;
      await new Promise((resolve) => (finishDelayedSave = resolve));
      return send({ operation_id: value.operation_id, status: 'completed' });
    }
    if (uncertainBridgeSave)
      return send({ operation_id: value.operation_id, status: 'unknown', possibly_sent: true });
    for (const key of [
      'enabled',
      'auto_import',
      'included_devices',
      'excluded_devices',
      'excluded_locations',
      'excluded_kinds',
      'excluded_mappings',
    ])
      bridge[key] = value[key];
    bridge.revision++;
    for (const entry of bridge.devices) {
      const device = monitoring.devices.find((item) => item.id === entry.id);
      entry.excluded =
        bridge.excluded_devices.includes(entry.id) ||
        bridge.excluded_locations.includes(device.location_id) ||
        bridge.excluded_kinds.includes(device.kind);
      entry.selected =
        entry.eligible &&
        !entry.excluded &&
        (bridge.auto_import || bridge.included_devices.includes(entry.id));
    }
    bridge.selected_count = bridge.devices.filter((entry) => entry.selected).length;
    publish();
    return send({ operation_id: value.operation_id, status: 'completed' });
  }
  if (/^\/api\/web\/v1\/(selection|pairing|recording|controls)$/.test(path))
    return send({
      ...monitoring.scope,
      boot_id: 'adapter-test',
      revision: 1,
      registry_revision: 1,
      cameras: [],
    });
  if (path.startsWith('/api/')) return send({}, 404);
  const file = resolve(staticRoot, `.${path === '/' ? '/index.html' : path}`);
  if (relative(staticRoot, file).startsWith('..')) {
    response.writeHead(404);
    response.end();
    return;
  }
  try {
    const body = await readFile(file);
    response.writeHead(200, {
      'Content-Type':
        { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.png': 'image/png' }[
          extname(file)
        ] ?? 'text/plain',
    });
    response.end(body);
  } catch {
    response.writeHead(404);
    response.end();
  }
});
await new Promise((resolve, reject) => {
  server.once('error', reject);
  server.listen(0, '127.0.0.1', resolve);
});
const timer = setInterval(publish, 1000);
const browser = await testBrowser.launch({ headless: true });
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
const page = await context.newPage();
page.on('pageerror', (error) => errors.push(error.message));
const cameraRequests = () =>
  requests.filter((request) =>
    /^\/api\/web\/v1\/(selection|pairing|recording|controls)$/.test(request.path),
  );
try {
  await page.goto(`http://127.0.0.1:${server.address().port}/#/devices`);
  await page.getByRole('heading', { name: 'Other Ring devices', exact: true }).waitFor();
  await page.getByText('Some observations need attention', { exact: true }).waitFor();
  assert.equal(await page.locator('.device-link').count(), 4);
  assert.equal(cameraRequests().length, 0);
  assert.equal(await page.getByRole('button', { name: /panic|arm|unlock|siren/i }).count(), 0);
  checks.push('camera plus non-camera inventory; partial source status; no actuation controls');
  await page.getByLabel('Filter other devices by location').selectOption('garage');
  assert.equal(await page.locator('.device-link').count(), 2);
  await page.getByLabel('Filter other devices by location').selectOption('');
  await page.getByLabel('Filter other devices by type').selectOption('contact-sensor');
  assert.equal(await page.locator('.device-link').count(), 2);
  await page.getByLabel('Filter other devices by type').selectOption('');
  await page.locator('.device-link').filter({ hasText: 'Front door' }).click();
  await page.getByRole('heading', { name: 'Observations', exact: true }).waitFor();
  assert.equal(await page.getByText('Closed', { exact: true }).count(), 1);
  assert.equal(await page.getByText('Unknown', { exact: true }).count(), 1);
  assert.equal(await page.getByRole('heading', { name: 'Recording', exact: true }).count(), 0);
  assert.equal(cameraRequests().length, 0, 'generic details never request camera administration');
  delayBridgeSave = true;
  await page
    .getByRole('switch', { name: 'Include Front door in Halo Bridge', exact: true })
    .click();
  for (let attempt = 0; !finishDelayedSave && attempt < 50; attempt++)
    await new Promise((resolve) => setTimeout(resolve, 10));
  assert.ok(finishDelayedSave);
  await page.evaluate(() => (location.hash = '/devices/noncamera%3Amotion-one'));
  await page.getByRole('heading', { name: 'Garage motion', exact: true, level: 2 }).waitFor();
  finishDelayedSave();
  finishDelayedSave = undefined;
  delayBridgeSave = false;
  await page.waitForTimeout(100);
  assert.equal(
    await page.getByText(`Operation: ${delayedOperationId}`, { exact: true }).count(),
    0,
    'Another device never displays the previous device’s delayed operation',
  );
  checks.push('Late bridge preference completion cannot attach to another device detail');

  await page.getByText('Current device state is unavailable.', { exact: false }).waitFor();
  assert.equal(
    await page.getByText('Detected', { exact: true }).count(),
    0,
    'stale true motion is not a current event',
  );
  assert.equal(await page.getByText('Unknown', { exact: true }).count(), 1);
  assert.equal(cameraRequests().length, 0);
  checks.push(
    'location and type filtering; unknown battery distinct from zero; stale motion hidden; generic detail makes no camera RPCs',
  );
  await page.getByRole('link', { name: 'Back to devices', exact: true }).click();
  await page.getByRole('switch', { name: 'Prepare Halo Bridge', exact: true }).waitFor();
  assert.equal(
    await page.getByRole('link', { name: 'Manage Halo Bridge', exact: true }).count(),
    1,
  );
  await page.getByRole('switch', { name: 'Automatically include supported devices' }).click();
  await page.getByText('0 of 149 selected', { exact: true }).waitFor();
  assert.equal(bridge.auto_import, false);
  await page.locator('.device-link').filter({ hasText: 'Front door' }).click();
  await page.getByRole('switch', { name: 'Include Front door in Halo Bridge' }).click();
  await page.getByText('Selected for Halo Bridge', { exact: true }).waitFor();
  assert.deepEqual(bridge.included_devices, ['sensor-one']);
  await page.getByRole('switch', { name: 'Include Front door in Halo Bridge' }).click();
  await page.getByText('Excluded', { exact: true }).waitFor();
  assert.deepEqual(bridge.excluded_devices, ['missing-device', 'sensor-one']);
  await page.getByRole('link', { name: 'Back to devices', exact: true }).click();
  await page.locator('summary').filter({ hasText: 'Import exclusions' }).click();
  await page.getByLabel('Location to exclude from Halo Bridge').selectOption('home');
  await page.getByRole('button', { name: 'Exclude location', exact: true }).click();
  await page
    .getByRole('button', { name: 'Remove location exclusion for Home', exact: true })
    .waitFor();
  await page.getByLabel('Device type to exclude from Halo Bridge').selectOption('motion-sensor');
  await page.getByRole('button', { name: 'Exclude type', exact: true }).click();
  await page
    .getByRole('button', { name: 'Remove device type exclusion for motion sensor', exact: true })
    .waitFor();
  await page.getByRole('switch', { name: 'Include contact capability in Halo Bridge' }).click();
  await page.waitForFunction(
    () =>
      !document.querySelector('[aria-label="Include contact capability in Halo Bridge"]').checked,
  );
  assert.deepEqual(bridge.excluded_mappings, ['contact', 'smoke']);
  assert.equal(cameraRequests().length, 0);
  checks.push(
    'scoped bridge CAS preferences preserve missing devices, locations, types and capability exclusions; no actuation or camera RPCs',
  );
  await page.locator('.device-link').filter({ hasText: 'Front door' }).click();
  assert.equal(
    await page.getByRole('switch', { name: 'Include Front door in Halo Bridge' }).isDisabled(),
    true,
  );
  await page.getByRole('link', { name: 'Back to devices', exact: true }).click();
  await page.getByRole('switch', { name: 'Automatically include supported devices' }).waitFor();
  uncertainBridgeSave = true;
  const bridgeSavesBefore = bridgeMutations.length;
  await page.getByRole('switch', { name: 'Automatically include supported devices' }).click();
  await page.getByRole('button', { name: 'Check outcome' }).waitFor();
  assert.equal(
    await page.getByRole('switch', { name: 'Prepare Halo Bridge', exact: true }).isDisabled(),
    true,
  );
  assert.equal(
    await page
      .getByRole('switch', { name: 'Automatically include supported devices' })
      .isDisabled(),
    true,
  );
  await page.waitForTimeout(100);
  assert.equal(bridgeMutations.length, bridgeSavesBefore + 1);
  uncertainBridgeSave = false;
  checks.push(
    'uncertain bridge saves disable follow-on edits and are not automatically retried; broader exclusions block per-device overrides',
  );
  for (const theme of ['light', 'dark']) {
    await page.evaluate((theme) => (document.documentElement.dataset.theme = theme), theme);
    for (const width of [320, 390, 768, 1440]) {
      await page.setViewportSize({ width, height: 1000 });
      for (const route of [
        'devices',
        'devices/noncamera%3Asensor-one',
        'devices/noncamera%3Amotion-one',
      ]) {
        await page.evaluate((route) => (location.hash = `/${route}`), route);
        await page.waitForTimeout(80);
        if (route === 'devices') {
          const details = page.locator('.bridge-exclusions');
          if (await details.count()) await details.evaluate((element) => (element.open = true));
        }
        const dimensions = await page.evaluate(() => ({
          scroll: document.documentElement.scrollWidth,
          width: innerWidth,
        }));
        assert.ok(
          dimensions.scroll <= dimensions.width,
          `${theme} ${width} ${route} horizontal overflow`,
        );
        if (output && [390, 1440].includes(width))
          await page.screenshot({
            path: `${output}/${theme}-${width}-${route.replaceAll('/', '-')}.png`,
            fullPage: true,
          });
      }
    }
  }
  checks.push('24 layout combinations: 320/390/768/1440px, light/dark, list/current/stale details');
  await page.evaluate(() => (location.hash = '/devices'));
  const toggle = page.getByRole('switch', { name: 'Enable other Ring device monitoring' });
  await toggle.waitFor();
  await toggle.click();
  await page.getByText('Disabled', { exact: true }).waitFor();
  assert.equal(mutations.length, 1);
  assert.equal(mutations[0].enabled, false);
  assert.equal(cameraRequests().length, 0);
  checks.push(
    'monitoring disable sends exactly one scoped policy mutation and no camera or device command',
  );
  await page.locator('.device-link').filter({ hasText: 'Floodlight' }).click();
  await page.getByRole('heading', { name: 'Recording', exact: true }).waitFor();
  assert.equal(cameraRequests().length, 4, 'camera administration remains available');
  publish();
  await page.waitForTimeout(100);
  assert.equal(
    cameraRequests().length,
    4,
    'ordinary state updates do not refetch camera administration',
  );
  checks.push('camera details retain exactly their four administration reads');
  state.scope = { profile_id: null, account_generation: null };
  state.devices = [];
  state.ring.authenticated = false;
  monitoring.enabled = true;
  monitoring.state = 'partial';
  publish();
  await page.evaluate(() => (location.hash = '/devices'));
  await page.getByRole('heading', { name: 'Other Ring devices', exact: true }).waitFor();
  assert.equal(await page.locator('.device-link').count(), 3);
  assert.equal(
    await page.getByRole('switch', { name: 'Enable other Ring device monitoring' }).isEnabled(),
    true,
  );
  await page.locator('.device-link').filter({ hasText: 'Front door' }).click();
  await page.getByRole('heading', { name: 'Observations', exact: true }).waitFor();
  assert.equal(await page.getByText('Closed', { exact: true }).count(), 1);
  assert.equal(cameraRequests().length, 4);
  checks.push(
    'non-camera monitoring and observations remain usable with no ready camera inventory',
  );
  await page.evaluate(() => (location.hash = '/devices'));
  await page.getByRole('switch', { name: 'Prepare Halo Bridge', exact: true }).waitFor();
  delayBridgeSave = true;
  await page.getByRole('switch', { name: 'Prepare Halo Bridge', exact: true }).click();
  for (let attempt = 0; !finishDelayedSave && attempt < 50; attempt++)
    await new Promise((resolve) => setTimeout(resolve, 10));
  assert.ok(finishDelayedSave);
  monitoring.scope = { profile_id: 'new-account', account_generation: 5 };
  state.epoch = 'new-account';
  monitoring.bridge = {
    ...bridge,
    configured: false,
    enabled: false,
    revision: 0,
    devices: [],
    included_devices: [],
    excluded_devices: [],
    excluded_locations: [],
    excluded_kinds: [],
    excluded_mappings: [],
    selected_count: 0,
    retained_count: 0,
  };
  publish();
  await page
    .getByText('Prepare the bridge to save its first configuration.', { exact: true })
    .waitFor();
  finishDelayedSave();
  await page.waitForTimeout(100);
  assert.equal(
    await page.getByText(`Operation: ${delayedOperationId}`, { exact: true }).count(),
    0,
  );
  assert.equal(
    await page.getByRole('switch', { name: 'Prepare Halo Bridge', exact: true }).isChecked(),
    false,
  );
  checks.push(
    'late successful preference reply from previous Ring account cannot update the new account panel',
  );
  delete monitoring.bridge;
  publish();
  await page.evaluate(() => (location.hash = '/devices'));
  await page.getByRole('heading', { name: 'Other Ring devices', exact: true }).waitFor();
  assert.equal(
    await page.getByRole('switch', { name: 'Prepare Halo Bridge', exact: true }).count(),
    0,
  );
  checks.push('older backend without bridge projection retains non-camera monitoring');
  if (process.env.HALO_BRIDGE_FIXTURE) {
    const backend = JSON.parse(await readFile(process.env.HALO_BRIDGE_FIXTURE, 'utf8'));
    assert.equal(backend.bridge.status, 'ready');
    assert.equal(backend.bridge.selected_count, 1);
    state.noncamera = backend;
    state.scope = backend.scope;
    state.epoch = 'actual-backend-projection';
    publish();
    await page.getByText('1 of 149 selected', { exact: true }).waitFor();
    await page.locator('.device-link').filter({ hasText: backend.devices[0].name }).click();
    await page.getByText('Selected for Halo Bridge', { exact: true }).waitFor();
    assert.equal(
      await page
        .getByRole('switch', { name: `Include ${backend.devices[0].name} in Halo Bridge` })
        .isChecked(),
      true,
    );
    assert.equal(cameraRequests().length, 4);
    checks.push(
      'actual Python catalog and registry projection with UUID/hash identities renders and selects correctly',
    );
    backend.bridge.excluded_mappings = ['contact'];
    backend.bridge.devices[0].mappings = [];
    backend.bridge.devices[0].selected = false;
    backend.bridge.devices[0].excluded = true;
    backend.bridge.devices[0].reason = 'mappings-excluded';
    backend.bridge.selected_count = 0;
    publish();
    await page.getByText('All eligible capabilities excluded', { exact: true }).waitFor();
    const include = page.getByRole('switch', {
      name: `Include ${backend.devices[0].name} in Halo Bridge`,
    });
    assert.equal(await include.isChecked(), false);
    assert.equal(await include.isDisabled(), true);
    await page
      .getByText('All eligible device capabilities are excluded.', { exact: false })
      .waitFor();
    backend.bridge.overflow_count = 1;
    backend.bridge.devices[0].reason = 'registry-capacity';
    publish();
    await page.getByRole('link', { name: 'Back to devices', exact: true }).click();
    await page
      .getByText(
        'Exclusions do not remove retained identities or resolve the retained-identity limit.',
        { exact: false },
      )
      .waitFor();
    checks.push(
      'all excluded capabilities disable the device include switch; overflow guidance distinguishes retained identities from selection slots',
    );
  }
  assert.deepEqual(errors, []);
  const receipt = {
    status: 'passed',
    checks,
    page_errors: errors,
    mutations: mutations.length,
    camera_reads: cameraRequests().length,
    bridge_mutations: bridgeMutations.length,
  };
  if (output) await writeFile(`${output}/receipt.json`, JSON.stringify(receipt, null, 2) + '\n');
  console.log(JSON.stringify(receipt, null, 2));
} finally {
  clearInterval(timer);
  await browser.close();
  for (const response of subscribers) response.end();
  await new Promise((resolve) => server.close(resolve));
}
