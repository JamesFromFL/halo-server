/** Isolated lighting UI fixture. No Ring, HAP listener, or physical commands. */
import { testBrowser } from './browser-engine.mjs';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFile, mkdir, writeFile } from 'node:fs/promises';
import { resolve, dirname, extname, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

const staticRoot = resolve(
  dirname(fileURLToPath(import.meta.url)),
  '../../src/halo_server/web/static',
);
const output = process.env.HALO_BROWSER_OUTPUT;
const groupMode = process.env.HALO_LIGHT_KIND === 'group';
if (output && !output.startsWith('/')) throw new Error('HALO_BROWSER_OUTPUT must be absolute');
if (output) await mkdir(output, { recursive: true });
const subscribers = new Set(),
  requests = [],
  policyWrites = [],
  bridgeWrites = [],
  commands = [],
  checks = [],
  errors = [];
let reauths = 0,
  delayCommand = false,
  releaseCommand;
let outcome = {
  status: 'unknown',
  result: { status: 'uncertain', code: 'confirmation-timeout', possibly_sent: true, on: null },
};
const light = {
  policy_revision: 3,
  enabled: false,
  on_duration_seconds: 60,
  provider_generation: 4,
  epoch: 5,
  observed_revision: 6,
  on: false,
  authorization: 'allowed',
  can_set: false,
  reason: 'controls-disabled',
  pending: false,
  outcome_unknown: false,
  requested_on: null,
};
const lamp = {
  id: 'light-one',
  name: 'Garden path',
  kind: groupMode ? 'light-group' : 'beams-light',
  model: groupMode ? 'Ring Light Group' : 'Beams light',
  provider: groupMode ? 'ring-light-group' : 'ring-hub',
  family: groupMode ? 'lighting-group' : 'hub',
  capability_evidence: [groupMode ? 'ring-light-group-schema' : 'ring-hub-device-schema'],
  location_id: 'location-one',
  location_name: 'Home',
  capabilities: groupMode ? ['switch'] : ['light'],
  observed: {},
  available: true,
  source_health: 'online',
  stale: false,
  observed_at: Date.now() / 1000,
  supported: true,
  unsupported_reason: null,
  light,
};
const contact = {
  ...lamp,
  id: 'contact-one',
  name: 'Front door',
  kind: 'contact-sensor',
  model: 'Contact Sensor',
  capabilities: ['contact'],
  observed: { contact_open: false },
  light: undefined,
};
const monitoring = {
  schema: 1,
  revision: 2,
  policy_revision: 1,
  scope: { profile_id: '11111111-1111-4111-8111-111111111111', account_generation: 2 },
  state: 'ready',
  complete: true,
  observed_at: Date.now() / 1000,
  devices: [lamp, contact],
  enabled: true,
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
      reauthenticated: false,
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
  if (path.startsWith('/api/web/v1/operations/'))
    return send({ operation_id: path.split('/').at(-1), ...outcome });
  if (request.method === 'POST' && path.startsWith('/api/')) {
    assert.equal(request.headers['x-halo-csrf'], 'fixture-csrf');
    let body = '';
    for await (const chunk of request) body += chunk;
    const value = JSON.parse(body);
    if (path === '/api/web/v1/reauth') {
      assert.equal(value.password, 'fixture-password-123');
      reauths++;
      return send({
        authenticated: true,
        setup_required: false,
        csrf_token: 'fixture-csrf',
        reauthenticated: true,
        session_id: 'fixture-session',
      });
    }
    assert.equal(value.profile_id, monitoring.scope.profile_id);
    assert.equal(value.account_generation, monitoring.scope.account_generation);
    if (path === '/api/web/v1/devices/bridge' && groupMode) {
      assert.equal(value.expected_revision, monitoring.bridge.revision);
      assert.equal(value.auto_import, true);
      assert.deepEqual(value.excluded_locations, ['unlisted-location']);
      assert.deepEqual(value.excluded_kinds, []);
      bridgeWrites.push(value);
      monitoring.bridge.included_devices = value.included_devices;
      monitoring.bridge.excluded_devices = value.excluded_devices;
      monitoring.bridge.revision++;
      const selected = value.included_devices.includes(lamp.id);
      Object.assign(monitoring.bridge.devices[0], {
        selected,
        excluded: !selected,
        reason: selected ? null : 'device-excluded',
      });
      monitoring.bridge.selected_count = selected ? 1 : 0;
      publish();
      return send({ operation_id: value.operation_id, status: 'completed', result: {} });
    }
    assert.equal(value.device_id, lamp.id);
    if (path === '/api/web/v1/devices/light/policy') {
      assert.deepEqual(
        Object.keys(value).sort(),
        [
          'profile_id',
          'account_generation',
          'device_id',
          'expected_revision',
          'enabled',
          'on_duration_seconds',
          'operation_id',
        ].sort(),
      );
      assert.equal(value.expected_revision, light.policy_revision);
      assert.ok([30, 60, 300, 900].includes(value.on_duration_seconds));
      if (value.enabled) assert.ok(reauths > 0);
      policyWrites.push(value);
      light.enabled = value.enabled;
      light.on_duration_seconds = value.on_duration_seconds;
      light.policy_revision++;
      light.can_set = value.enabled && light.authorization === 'allowed';
      light.reason = value.enabled ? null : 'controls-disabled';
      monitoring.safety.read_only = !value.enabled;
      publish();
      return send({
        operation_id: value.operation_id,
        status: 'completed',
        result: { enabled: value.enabled, on_duration_seconds: value.on_duration_seconds },
      });
    }
    if (path === '/api/web/v1/devices/light/set') {
      assert.deepEqual(
        Object.keys(value).sort(),
        [
          'profile_id',
          'account_generation',
          'device_id',
          'provider_generation',
          'expected_epoch',
          'expected_revision',
          'expected_policy_revision',
          'target',
          'operation_id',
        ].sort(),
      );
      assert.equal(value.provider_generation, light.provider_generation);
      assert.equal(value.expected_epoch, light.epoch);
      assert.equal(value.expected_revision, light.observed_revision);
      assert.equal(value.expected_policy_revision, light.policy_revision);
      assert.equal(typeof value.target, 'boolean');
      assert.equal(light.enabled, true);
      assert.equal(light.authorization, 'allowed');
      assert.equal(light.pending, false);
      commands.push(value);
      light.requested_on = value.target;
      light.outcome_unknown = outcome.status === 'unknown';
      publish();
      if (delayCommand)
        await new Promise((resolve) => {
          releaseCommand = resolve;
        });
      return send({ operation_id: value.operation_id, ...outcome });
    }
    throw new Error(`Unexpected mutation ${path}`);
  }
  if (path.startsWith('/api/')) return send(null, 404);
  const file = resolve(staticRoot, path === '/' ? 'index.html' : `.${path}`);
  if (relative(staticRoot, file).startsWith('..')) {
    response.writeHead(403);
    response.end();
    return;
  }
  try {
    const data = await readFile(file);
    response.writeHead(200, {
      'Content-Type':
        { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.png': 'image/png' }[
          extname(file)
        ] || 'application/octet-stream',
    });
    response.end(data);
  } catch {
    response.writeHead(404);
    response.end();
  }
});
await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
const timer = setInterval(publish, 1000),
  browser = await testBrowser.launch({ headless: true });
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
page.setDefaultTimeout(30_000);
page.on('pageerror', (error) => errors.push(error.message));
async function password() {
  const dialog = page.getByRole('dialog', { name: 'Confirm your Halo password' });
  await dialog.waitFor();
  await dialog.getByLabel('Password', { exact: true }).fill('fixture-password-123');
  await dialog.getByRole('button', { name: 'Confirm', exact: true }).click();
}
const observed = () =>
  page.locator('.preference-row').filter({
    has: page.getByText(groupMode ? 'Observed group state' : 'Observed light', { exact: true }),
  });
const waitEnabled = async (label, enabled) =>
  page.waitForFunction(
    ({ label, enabled }) =>
      [...document.querySelectorAll('button')].find((button) => button.textContent === label)
        ?.disabled === !enabled,
    { label, enabled },
  );
try {
  await page.goto(`http://127.0.0.1:${server.address().port}/#/devices/noncamera%3Alight-one`);
  const toggle = page.getByRole('switch', { name: 'Allow light controls', exact: true });
  const duration = page.getByRole('combobox', { name: 'Light on duration', exact: true });
  const apply = page.getByRole('button', { name: 'Apply light preferences', exact: true });
  const automatic = page.getByRole('button', {
    name: groupMode ? 'Turn off group' : 'Return to automatic',
    exact: true,
  });
  const on = () => page.getByRole('button', { name: /^Turn on for / });
  await toggle.waitFor();
  if (groupMode) {
    await page
      .getByText('Ring’s group report does not confirm the state of every member light.', {
        exact: true,
      })
      .waitFor();
    await page.getByText('Turn off group requests Off for the group.', { exact: false }).waitFor();
    assert.equal(
      await page.getByRole('button', { name: 'Return to automatic', exact: true }).count(),
      0,
    );
  }
  assert.equal(await toggle.isChecked(), false);
  assert.equal(await duration.inputValue(), '60');
  assert.equal(await on().isDisabled(), true);
  assert.equal(await apply.isDisabled(), true);
  await toggle.click();
  await duration.selectOption('300');
  assert.equal(policyWrites.length, 0);
  assert.equal(commands.length, 0);
  await apply.click();
  await page.getByRole('dialog').getByRole('button', { name: 'Cancel', exact: true }).click();
  assert.equal(policyWrites.length, 0);
  await apply.click();
  await password();
  await waitEnabled('Turn on for 5 minutes', true);
  assert.equal(policyWrites.length, 1);
  assert.equal(policyWrites[0].on_duration_seconds, 300);
  assert.equal(commands.length, 0);
  await observed().getByText('Off', { exact: true }).waitFor();
  checks.push(
    'default-off and duration drafts require Apply; canceled password sends nothing; saved duration applies without actuating',
  );

  await on().click();
  await page.getByRole('button', { name: 'Check outcome', exact: true }).waitFor();
  assert.equal(commands.length, 1);
  assert.equal(reauths, 1);
  await observed().getByText('Off', { exact: true }).waitFor();
  assert.equal(await on().isDisabled(), true);
  await page.getByRole('button', { name: 'Check outcome', exact: true }).click();
  assert.equal(commands.length, 1);
  light.on = true;
  light.requested_on = null;
  light.outcome_unknown = false;
  light.observed_revision++;
  publish();
  await observed().getByText('On', { exact: true }).waitFor();
  await page.getByRole('button', { name: 'Use current observed state', exact: true }).click();
  await waitEnabled('Turn on for 5 minutes', true);
  assert.equal(commands.length, 1);
  outcome = {
    status: 'completed',
    result: { status: 'confirmed', code: null, possibly_sent: false, on: true },
  };
  await automatic.click();
  await page
    .getByText(
      'Ring confirmed the light request. Its observed illumination is reported separately.',
      { exact: true },
    )
    .waitFor();
  assert.equal(commands.length, 2);
  assert.equal(commands[1].target, false);
  assert.equal(reauths, 1);
  await observed().getByText('On', { exact: true }).waitFor();
  checks.push(
    'normal actions need no extra password; uncertainty never replays and automatic mode never fabricates observed Off',
  );

  await duration.selectOption('30');
  assert.equal(await on().isDisabled(), true);
  light.policy_revision++;
  light.on_duration_seconds = 900;
  publish();
  await page
    .getByText('Saved light preferences changed. Reload them before making another change.', {
      exact: true,
    })
    .waitFor();
  assert.equal(await apply.isDisabled(), true);
  await page.getByRole('button', { name: 'Reload saved preferences', exact: true }).click();
  assert.equal(await duration.inputValue(), '900');
  await duration.selectOption('60');
  await apply.click();
  await page.getByRole('dialog').waitFor();
  light.epoch++;
  publish();
  await password();
  await page
    .getByText('Light preferences changed. Review the current settings before trying again.', {
      exact: true,
    })
    .waitFor();
  assert.equal(policyWrites.length, 1);
  checks.push(
    'external preference changes and provider epoch changes during reauthentication invalidate unsaved requests',
  );

  for (const authorization of ['unknown', 'denied']) {
    light.authorization = authorization;
    light.can_set = false;
    publish();
    await waitEnabled('Turn on for 15 minutes', false);
    await page
      .getByText(
        authorization === 'denied'
          ? 'Ring has denied light control for this account.'
          : 'Ring control authority is not verified. Commands remain unavailable.',
        { exact: true },
      )
      .waitFor();
  }
  light.authorization = 'allowed';
  light.can_set = true;
  light.pending = true;
  publish();
  await page.getByText('A light request is in progress.', { exact: false }).waitFor();
  assert.equal(await on().isDisabled(), true);
  light.pending = false;
  lamp.source_health = 'offline';
  publish();
  await observed().getByText('Unknown', { exact: true }).waitFor();
  lamp.source_health = 'online';
  light.on = null;
  publish();
  assert.equal(await on().isDisabled(), true);
  light.on = true;
  lamp.stale = true;
  lamp.available = null;
  publish();
  await waitEnabled('Turn on for 15 minutes', true);
  checks.push(
    'unknown/denied authority, pending work and unavailable light state block commands; stale battery does not hide current illumination',
  );

  delayCommand = true;
  await on().click();
  while (!releaseCommand) await new Promise((resolve) => setTimeout(resolve, 10));
  monitoring.scope.account_generation++;
  light.provider_generation++;
  light.epoch++;
  light.requested_on = null;
  publish();
  await page.waitForTimeout(50);
  releaseCommand();
  delayCommand = false;
  await page.waitForTimeout(100);
  assert.equal(await page.locator('.operation').count(), 0);
  assert.equal(commands.length, 3);
  checks.push('late command result is discarded after account/provider replacement');

  monitoring.bridge = {
    revision: 1,
    configured: true,
    enabled: true,
    auto_import: true,
    accessory_uuid: 'fixture-bridge',
    username: 'CA:FE:00:00:00:01',
    capacity: 149,
    selected_count: 0,
    retained_count: 1,
    overflow_count: 0,
    included_devices: [],
    excluded_devices: [],
    excluded_locations: groupMode ? ['unlisted-location'] : [],
    excluded_kinds: groupMode ? [] : ['switch'],
    excluded_mappings: [],
    publication: 'not_implemented',
    status: 'ready',
    truncated: false,
    devices: [
      {
        id: lamp.id,
        accessory_uuid: 'fixture-light',
        mappings: ['lightbulb'],
        selected: false,
        excluded: !groupMode,
        eligible: true,
        reason: groupMode ? null : 'kind-excluded',
        present: true,
      },
    ],
  };
  publish();
  const imported = page.getByRole('switch', {
    name: 'Include Garden path in Halo Bridge',
    exact: true,
  });
  if (groupMode) {
    await imported.waitFor();
    await page
      .getByText('Groups require individual inclusion to avoid duplicate controls', {
        exact: false,
      })
      .waitFor();
    assert.equal(await imported.isDisabled(), false);
    assert.equal(await imported.isChecked(), false);
    await waitEnabled('Turn on for 15 minutes', true);
    const beforeCommands = commands.length;
    await imported.click();
    await page.waitForFunction(
      () => document.querySelector('[aria-label="Include Garden path in Halo Bridge"]').checked,
    );
    assert.equal(bridgeWrites.length, 1);
    assert.deepEqual(bridgeWrites[0].included_devices, [lamp.id]);
    await imported.click();
    await page.waitForFunction(
      () => !document.querySelector('[aria-label="Include Garden path in Halo Bridge"]').checked,
    );
    assert.equal(bridgeWrites.length, 2);
    assert.deepEqual(bridgeWrites[1].excluded_devices, [lamp.id]);
    await waitEnabled('Turn on for 15 minutes', true);
    assert.equal(commands.length, beforeCommands);
    checks.push(
      'group import is unchecked under auto-import; explicit include/exclude preserves hidden policy and never grants or actuates Halo controls',
    );
  } else {
    await page
      .getByText('Excluded by a saved switch-family preference.', { exact: false })
      .waitFor();
    assert.equal(await imported.isDisabled(), true);
    assert.equal(await imported.isChecked(), false);
    monitoring.bridge.excluded_kinds = [];
    monitoring.bridge.revision++;
    monitoring.bridge.selected_count = 1;
    Object.assign(monitoring.bridge.devices[0], { selected: true, excluded: false, reason: null });
    publish();
    await page.waitForFunction(
      () =>
        document.querySelector('[aria-label="Include Garden path in Halo Bridge"]').disabled ===
        false,
    );
    assert.equal(await imported.isChecked(), true);
    checks.push(
      'legacy switch-family exclusions still block enriched light imports and are never overwritten',
    );
  }

  await page.locator('.toast').waitFor({ state: 'detached', timeout: 7000 });
  for (const theme of ['light', 'dark']) {
    await page.evaluate((theme) => {
      document.documentElement.dataset.theme = theme;
    }, theme);
    for (const width of [320, 768, 1440]) {
      await page.setViewportSize({ width, height: 1000 });
      await page.evaluate(() => {
        document.activeElement?.blur();
        window.scrollTo(0, 0);
      });
      assert.equal(
        await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
        true,
      );
      if (output)
        await page.screenshot({
          path: resolve(output, `light-${theme}-${width}.png`),
          fullPage: true,
        });
    }
  }
  checks.push('six GTK-style light/dark layouts have no horizontal overflow');
  const priorReauths = reauths;
  await toggle.click();
  await apply.click();
  await waitEnabled('Turn on for 15 minutes', false);
  await page.waitForFunction(
    () => !document.querySelector('[aria-label="Allow light controls"]').checked,
  );
  assert.equal(policyWrites.length, 2);
  assert.equal(policyWrites[1].enabled, false);
  assert.equal(reauths, priorReauths);
  assert.equal(commands.length, 3);
  await page.evaluate(() => {
    location.hash = '/devices/noncamera%3Acontact-one';
  });
  await page.getByRole('heading', { name: 'Front door', exact: true }).waitFor();
  assert.equal(await toggle.count(), 0);
  checks.push(
    'explicit disable saves policy only; sensor detail has no light controls or camera administration reads',
  );
  if (process.env.HALO_LIGHT_FIXTURE) {
    const backend = JSON.parse(await readFile(process.env.HALO_LIGHT_FIXTURE, 'utf8'));
    const actual = backend.devices.find(
      (device) => device.light && device.kind === (groupMode ? 'light-group' : 'beams-light'),
    );
    assert.ok(actual);
    state.noncamera = backend;
    state.scope = backend.scope;
    state.epoch = 'actual-light-fixture';
    publish();
    await page.evaluate((id) => {
      location.hash = `/devices/${encodeURIComponent(`noncamera:${id}`)}`;
    }, actual.id);
    await page.getByRole('heading', { name: actual.name, exact: true, level: 2 }).waitFor();
    await toggle.waitFor();
    assert.equal(await toggle.isChecked(), actual.light.enabled);
    assert.equal(await duration.inputValue(), String(actual.light.on_duration_seconds));
    if (groupMode) {
      assert.equal(backend.bridge.overflow_count, 0);
      assert.equal(backend.bridge.devices[0].reason, 'explicit-selection-required');
      await page.getByText('Choose to include this group', { exact: true }).waitFor();
      assert.equal(
        await page
          .getByRole('switch', { name: `Include ${actual.name} in Halo Bridge`, exact: true })
          .isChecked(),
        false,
      );
    }
    await page.locator('.toast').waitFor({ state: 'detached', timeout: 7000 });
    if (output)
      await page.screenshot({ path: resolve(output, 'light-backend.png'), fullPage: true });
    checks.push('actual Python light DTO renders without any physical action or preference write');
    if (groupMode) {
      assert.equal(actual.provider, 'ring-light-group');
      actual.provider = 'ring-hub';
      publish();
      await page.waitForFunction(
        () => document.querySelector('[aria-label="Allow light controls"]')?.disabled === true,
      );
      await observed().getByText('Unknown', { exact: true }).waitFor();
      assert.equal(
        await page.getByRole('button', { name: 'Turn off group', exact: true }).isDisabled(),
        true,
      );
      checks.push(
        'malformed cross-provider group DTO invalidates freshness and disables controls without reinterpreting it as a physical light',
      );
    }
  }
  assert.equal(
    requests.filter(
      (request) =>
        request.path.startsWith('/api/web/v1/devices/') &&
        !/\/light\/(policy|set)$/.test(request.path) &&
        !(groupMode && request.path === '/api/web/v1/devices/bridge'),
    ).length,
    0,
  );
  assert.deepEqual(errors, []);
  const receipt = {
    checks,
    policyWrites: policyWrites.length,
    bridgeWrites: bridgeWrites.length,
    commands: commands.length,
    reauths,
    pageErrors: errors,
  };
  if (output)
    await writeFile(resolve(output, 'receipt.json'), JSON.stringify(receipt, null, 2) + '\n');
  process.stdout.write(JSON.stringify(receipt, null, 2) + '\n');
} finally {
  clearInterval(timer);
  releaseCommand?.();
  await browser.close();
  for (const response of subscribers) response.end();
  server.closeAllConnections();
  await new Promise((resolve) => server.close(resolve));
}
