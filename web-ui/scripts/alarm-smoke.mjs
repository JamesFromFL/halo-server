/** Isolated alarm UI fixture. No Ring connection, HAP listener, or physical command. */
import { testBrowser } from './browser-engine.mjs';
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
const subscribers = new Set(),
  requests = [],
  policyWrites = [],
  modeWrites = [],
  checks = [],
  errors = [];
let reauths = 0,
  delayMode = false,
  releaseMode;
let checkedOutcome = {
  status: 'unknown',
  result: { status: 'uncertain', code: 'confirmation-timeout', possibly_sent: true, mode: null },
};
const alarm = {
  policy_revision: 3,
  enabled: false,
  provider_generation: 4,
  epoch: 5,
  observed_revision: 6,
  mode: 'disarmed',
  phase: 'idle',
  signal: 'none',
  authorization: 'allowed',
  can_set_mode: false,
  allowed_targets: [],
  reason: 'policy-disabled',
  pending: false,
  outcome_unknown: false,
  requested_mode: null,
};
const panel = {
  id: 'panel-one',
  name: 'Home alarm',
  kind: 'security-panel',
  model: 'Security Panel',
  location_id: 'location-one',
  location_name: 'Home',
  capabilities: ['mode'],
  observed: {},
  available: true,
  source_health: 'online',
  stale: false,
  observed_at: Date.now() / 1000,
  supported: true,
  unsupported_reason: null,
  alarm,
};
const contact = {
  ...panel,
  id: 'contact-one',
  name: 'Front door',
  kind: 'contact-sensor',
  model: 'Contact Sensor',
  capabilities: ['contact'],
  observed: { contact_open: false },
  alarm: undefined,
};
const monitoring = {
  schema: 1,
  revision: 2,
  policy_revision: 1,
  scope: { profile_id: '11111111-1111-4111-8111-111111111111', account_generation: 2 },
  state: 'ready',
  complete: true,
  observed_at: Date.now() / 1000,
  devices: [panel, contact],
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
    return send({ operation_id: path.split('/').at(-1), ...checkedOutcome });
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
    if (path === '/api/web/v1/devices/alarm/policy') {
      assert.deepEqual(
        Object.keys(value).sort(),
        [
          'profile_id',
          'account_generation',
          'location_id',
          'expected_revision',
          'enabled',
          'operation_id',
        ].sort(),
      );
      assert.equal(value.location_id, panel.location_id);
      assert.equal(value.expected_revision, alarm.policy_revision);
      if (value.enabled) assert.ok(reauths > 0);
      policyWrites.push(value);
      alarm.enabled = value.enabled;
      alarm.policy_revision++;
      alarm.can_set_mode = value.enabled && alarm.authorization === 'allowed';
      alarm.allowed_targets = alarm.can_set_mode ? ['disarmed', 'home', 'away'] : [];
      alarm.reason = value.enabled ? null : 'policy-disabled';
      monitoring.safety.read_only = !value.enabled;
      publish();
      return send({
        operation_id: value.operation_id,
        status: 'completed',
        result: { enabled: value.enabled },
      });
    }
    if (path === '/api/web/v1/devices/alarm/mode') {
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
      assert.equal(value.device_id, panel.id);
      assert.equal(value.provider_generation, alarm.provider_generation);
      assert.equal(value.expected_epoch, alarm.epoch);
      assert.equal(value.expected_revision, alarm.observed_revision);
      assert.equal(value.expected_policy_revision, alarm.policy_revision);
      assert.ok(['disarmed', 'home', 'away'].includes(value.target));
      assert.equal(alarm.enabled, true);
      assert.equal(alarm.authorization, 'allowed');
      assert.equal(alarm.pending, false);
      assert.ok(reauths > 0);
      modeWrites.push(value);
      alarm.requested_mode = value.target;
      alarm.outcome_unknown = true;
      publish();
      if (delayMode) {
        await new Promise((resolve) => (releaseMode = resolve));
        return send({
          operation_id: value.operation_id,
          status: 'completed',
          result: { status: 'confirmed', code: null, possibly_sent: false, mode: value.target },
        });
      }
      return send({ operation_id: value.operation_id, ...checkedOutcome });
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
const timer = setInterval(publish, 1000);
const browser = await testBrowser.launch({ headless: true });
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
const page = await context.newPage();
page.on('pageerror', (error) => errors.push(error.message));
async function password() {
  const dialog = page.getByRole('dialog', { name: 'Confirm your Halo password' });
  await dialog.waitFor();
  await dialog.getByLabel('Password', { exact: true }).fill('fixture-password-123');
  await dialog.getByRole('button', { name: 'Confirm', exact: true }).click();
}
const observed = () =>
  page
    .locator('.preference-row')
    .filter({ has: page.getByText('Observed alarm mode', { exact: true }) });
try {
  await page.goto(`http://127.0.0.1:${server.address().port}/#/devices/noncamera%3Apanel-one`);
  const toggle = page.getByRole('switch', { name: 'Allow normal alarm controls', exact: true });
  const home = page.getByRole('button', { name: 'Arm Home', exact: true });
  await toggle.waitFor();
  assert.equal(await toggle.isChecked(), false);
  assert.equal(await home.isDisabled(), true);
  assert.equal(modeWrites.length, 0);
  assert.equal(policyWrites.length, 0);
  await toggle.click();
  await page.getByRole('dialog').getByRole('button', { name: 'Cancel', exact: true }).click();
  assert.equal(policyWrites.length, 0);
  await toggle.click();
  await page.getByRole('button', { name: 'Allow controls', exact: true }).click();
  await page
    .getByRole('dialog', { name: 'Confirm your Halo password' })
    .getByRole('button', { name: 'Cancel', exact: true })
    .click();
  assert.equal(policyWrites.length, 0);
  await toggle.click();
  await page.getByRole('button', { name: 'Allow controls', exact: true }).click();
  await password();
  await page.waitForFunction(
    () => document.querySelector('[aria-label="Allow normal alarm controls"]').checked,
  );
  assert.equal(policyWrites.length, 1);
  assert.equal(modeWrites.length, 0);
  await observed().getByText('Disarmed', { exact: true }).waitFor();
  checks.push(
    'location policy is default off, requires confirmation and password, and never arms itself',
  );

  await home.click();
  await page.getByRole('dialog').getByRole('button', { name: 'Cancel', exact: true }).click();
  assert.equal(modeWrites.length, 0);
  await home.click();
  await page.getByRole('button', { name: 'Confirm mode change', exact: true }).click();
  await page.getByRole('dialog', { name: 'Confirm your Halo password' }).waitFor();
  alarm.phase = 'entry-delay';
  alarm.observed_revision++;
  publish();
  await page.getByText('Entry delay', { exact: true }).waitFor();
  await password();
  await page
    .getByText('Alarm state changed. Review its current state before trying again.', {
      exact: true,
    })
    .waitFor();
  assert.equal(modeWrites.length, 0);
  alarm.phase = 'idle';
  alarm.observed_revision++;
  publish();
  await page.getByText('Idle', { exact: true }).waitFor();
  checks.push(
    'mode confirmation and changed source state during reauthentication never send a stale command',
  );

  await home.click();
  await page.getByRole('button', { name: 'Confirm mode change', exact: true }).click();
  await password();
  await page.getByRole('button', { name: 'Check outcome', exact: true }).waitFor();
  await observed().getByText('Disarmed', { exact: true }).waitFor();
  assert.equal(await home.isDisabled(), true);
  assert.equal(modeWrites.length, 1);
  assert.equal(
    await page.getByRole('button', { name: 'Use current observed state', exact: true }).count(),
    0,
  );
  await page.getByRole('button', { name: 'Check outcome', exact: true }).click();
  assert.equal(modeWrites.length, 1);
  alarm.mode = 'home';
  alarm.requested_mode = null;
  alarm.outcome_unknown = false;
  alarm.observed_revision++;
  publish();
  await page.getByRole('button', { name: 'Check outcome', exact: true }).click();
  await observed().getByText('Home', { exact: true }).waitFor();
  const acknowledge = page.getByRole('button', { name: 'Use current observed state', exact: true });
  await acknowledge.waitFor();
  assert.equal(await home.isDisabled(), true);
  assert.equal(modeWrites.length, 1);
  await acknowledge.click();
  await page.waitForFunction(
    () =>
      [...document.querySelectorAll('button')].find((button) => button.textContent === 'Arm Home')
        ?.disabled === false,
  );
  assert.equal(modeWrites.length, 1);
  checks.push(
    'uncertain command never updates mode or replays; explicit recovery needs a newer fresh source and clears only the local receipt',
  );

  alarm.allowed_targets = ['disarmed'];
  alarm.reason = 'alarm-triggered';
  publish();
  await page.waitForFunction(
    () =>
      [...document.querySelectorAll('button')].find((button) => button.textContent === 'Arm Home')
        ?.disabled === true,
  );
  assert.equal(await page.getByRole('button', { name: 'Disarm', exact: true }).isDisabled(), false);
  assert.equal(
    await page.getByRole('button', { name: 'Arm Away', exact: true }).isDisabled(),
    true,
  );
  await page.getByText('Control availability: alarm triggered.', { exact: true }).waitFor();
  assert.equal(modeWrites.length, 1);
  checks.push(
    'target-specific restrictions retain only allowed normal modes and explain availability',
  );
  alarm.allowed_targets = ['disarmed', 'home', 'away'];
  alarm.reason = null;

  for (const authorization of ['denied', 'unknown']) {
    alarm.authorization = authorization;
    alarm.can_set_mode = false;
    publish();
    await page.waitForFunction(
      () =>
        [...document.querySelectorAll('button')].find((button) => button.textContent === 'Arm Home')
          ?.disabled === true,
    );
    await page
      .getByText(
        authorization === 'denied'
          ? 'Ring has denied alarm control for this account.'
          : 'Ring account authority is not verified.',
        { exact: false },
      )
      .waitFor();
  }
  alarm.authorization = 'allowed';
  alarm.can_set_mode = true;
  alarm.pending = true;
  publish();
  await page
    .getByText('An alarm command is in progress for this location.', { exact: false })
    .waitFor();
  assert.equal(await home.isDisabled(), true);
  alarm.pending = false;
  panel.stale = true;
  panel.source_health = 'stale';
  publish();
  await observed().getByText('Unknown', { exact: true }).waitFor();
  assert.equal(await home.isDisabled(), true);
  panel.stale = false;
  panel.available = false;
  panel.source_health = 'offline';
  publish();
  assert.equal(await home.isDisabled(), true);
  panel.available = true;
  panel.source_health = 'online';
  alarm.mode = null;
  alarm.phase = null;
  alarm.can_set_mode = false;
  publish();
  await observed().getByText('Unknown', { exact: true }).waitFor();
  checks.push(
    'permission denied/unknown, pending command, stale/offline source and missing mode stay distinct and safe',
  );

  alarm.mode = 'home';
  alarm.phase = 'idle';
  alarm.can_set_mode = true;
  publish();
  await observed().getByText('Home', { exact: true }).waitFor();
  delayMode = true;
  await home.click();
  await page.getByRole('button', { name: 'Confirm mode change', exact: true }).click();
  await password();
  while (!releaseMode) await new Promise((resolve) => setTimeout(resolve, 10));
  monitoring.scope.account_generation++;
  alarm.provider_generation++;
  alarm.epoch++;
  alarm.requested_mode = null;
  alarm.outcome_unknown = false;
  alarm.mode = 'away';
  publish();
  await observed().getByText('Away', { exact: true }).waitFor();
  releaseMode();
  delayMode = false;
  await page.waitForTimeout(100);
  assert.equal(await page.locator('.operation').count(), 0);
  await observed().getByText('Away', { exact: true }).waitFor();
  checks.push('late command response cannot update another account/provider epoch');

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
          path: resolve(output, `alarm-${theme}-${width}.png`),
          fullPage: true,
        });
    }
  }
  checks.push('six responsive GTK-style light/dark layouts without overflow');
  await toggle.click();
  await page.waitForFunction(
    () => !document.querySelector('[aria-label="Allow normal alarm controls"]').checked,
  );
  assert.equal(policyWrites.length, 2);
  assert.equal(policyWrites[1].enabled, false);
  assert.equal(modeWrites.length, 2);
  checks.push('controls can be explicitly disabled without issuing a mode command');
  await page.evaluate(() => {
    location.hash = '/devices/noncamera%3Acontact-one';
  });
  await page.getByRole('heading', { name: 'Front door', exact: true }).waitFor();
  assert.equal(await toggle.count(), 0);
  assert.equal(await home.count(), 0);
  checks.push('sensor detail has no alarm controls and no camera administration reads');
  if (process.env.HALO_ALARM_FIXTURE) {
    const backend = JSON.parse(await readFile(process.env.HALO_ALARM_FIXTURE, 'utf8'));
    const actual = backend.devices.find((device) => device.alarm);
    assert.ok(actual);
    state.noncamera = backend;
    state.scope = backend.scope;
    state.epoch = 'actual-alarm-fixture';
    publish();
    await page.evaluate((id) => {
      location.hash = `/devices/${encodeURIComponent(`noncamera:${id}`)}`;
    }, actual.id);
    await page.getByRole('heading', { name: actual.name, exact: true, level: 2 }).waitFor();
    await toggle.waitFor();
    assert.equal(await toggle.isChecked(), actual.alarm.enabled);
    await page.locator('.toast').waitFor({ state: 'detached', timeout: 7000 });
    if (output)
      await page.screenshot({ path: resolve(output, 'alarm-backend.png'), fullPage: true });
    checks.push(
      'actual Python alarm catalog projection renders without any request to arm or change policy',
    );
  }
  assert.equal(
    requests.filter((request) => /\/(controls|selection|pairing|recording)$/.test(request.path))
      .length,
    0,
  );
  assert.equal(
    requests.filter(
      (request) =>
        request.method === 'POST' &&
        !/\/(reauth|devices\/alarm\/(policy|mode))$/.test(request.path),
    ).length,
    0,
  );
  assert.deepEqual(errors, []);
  const receipt = {
    checks,
    policyWrites: policyWrites.length,
    modeWrites: modeWrites.length,
    reauths,
    pageErrors: errors,
  };
  if (output)
    await writeFile(resolve(output, 'receipt.json'), JSON.stringify(receipt, null, 2) + '\n');
  process.stdout.write(JSON.stringify(receipt, null, 2) + '\n');
} finally {
  clearInterval(timer);
  releaseMode?.();
  await browser.close();
  for (const response of subscribers) response.end();
  server.closeAllConnections();
  await new Promise((resolve) => server.close(resolve));
}
