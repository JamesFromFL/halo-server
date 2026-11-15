/** Isolated keypad UI fixture. No Ring, HAP listener, or physical commands. */
import { testBrowser } from './browser-engine.mjs';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFile, mkdir, writeFile } from 'node:fs/promises';
import { resolve, dirname, extname, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

const staticRoot =
  process.env.HALO_STATIC_ROOT ??
  resolve(dirname(fileURLToPath(import.meta.url)), '../../src/halo_server/web/static');
const output = process.env.HALO_BROWSER_OUTPUT;
if (output && !output.startsWith('/')) throw new Error('HALO_BROWSER_OUTPUT must be absolute');
if (output) await mkdir(output, { recursive: true });
const subscribers = new Set(),
  requests = [],
  policyWrites = [],
  lockWrites = [],
  commands = [],
  checks = [],
  errors = [];
let reauths = 0,
  delayCommand = false,
  releaseCommand;
const keypad = {
  policy_revision: 3,
  enabled: false,
  locked: true,
  provider_generation: 4,
  epoch: 5,
  observed_revision: 6,
  brightness: 40,
  on_brightness: 40,
  authorization: 'allowed',
  can_set: false,
  can_set_lock: false,
  reason: 'controls-disabled',
  pending: false,
  outcome_unknown: false,
  requested_brightness: null,
};
const lamp = {
  id: 'keypad-one',
  name: 'Bedroom keypad',
  kind: 'keypad',
  model: 'security-keypad',
  provider: 'ring-hub',
  family: 'hub',
  capability_evidence: ['ring-hub-device-schema'],
  capabilities: ['keypad-brightness'],
  location_id: 'location-one',
  location_name: 'Home',
  observed: { keypad_brightness: 40, motion_detected: null },
  available: true,
  source_health: 'online',
  stale: false,
  observed_at: Date.now() / 1000,
  supported: true,
  unsupported_reason: null,
  keypad,
};
const monitoring = {
  schema: 1,
  revision: 2,
  policy_revision: 1,
  scope: { profile_id: '11111111-1111-4111-8111-111111111111', account_generation: 2 },
  state: 'ready',
  complete: true,
  observed_at: Date.now() / 1000,
  devices: [lamp],
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
    assert.equal(value.device_id, lamp.id);
    if (path === '/api/web/v1/devices/keypad/policy') {
      assert.equal(value.expected_revision, keypad.policy_revision);
      assert.ok(reauths > 0);
      policyWrites.push(value);
      Object.assign(keypad, {
        enabled: value.enabled,
        locked: value.locked,
        can_set_lock: value.enabled,
        can_set: value.enabled && !value.locked,
        reason: value.locked ? 'brightness-locked' : null,
      });
      keypad.policy_revision++;
      publish();
      return send({
        operation_id: value.operation_id,
        status: 'completed',
        result: {
          enabled: keypad.enabled,
          locked: keypad.locked,
          policy_revision: keypad.policy_revision,
        },
      });
    }
    if (path === '/api/web/v1/devices/keypad/lock') {
      assert.equal(value.expected_policy_revision, keypad.policy_revision);
      lockWrites.push(value);
      keypad.locked = value.locked;
      keypad.policy_revision++;
      keypad.can_set = keypad.enabled && !keypad.locked && !keypad.pending;
      keypad.reason = keypad.locked ? 'brightness-locked' : null;
      publish();
      return send({
        operation_id: value.operation_id,
        status: 'completed',
        result: { locked: keypad.locked, policy_revision: keypad.policy_revision },
      });
    }
    if (path === '/api/web/v1/devices/keypad/brightness') {
      assert.equal(value.expected_policy_revision, keypad.policy_revision);
      assert.equal(value.expected_revision, keypad.observed_revision);
      assert.equal(value.provider_generation, keypad.provider_generation);
      assert.equal(value.expected_epoch, keypad.epoch);
      assert.equal(keypad.locked, false);
      assert.equal(keypad.can_set, true);
      assert.ok(Number.isInteger(value.target) && value.target >= 0 && value.target <= 100);
      commands.push(value);
      keypad.pending = true;
      keypad.can_set = false;
      keypad.requested_brightness = value.target;
      publish();
      if (delayCommand)
        await new Promise((resolve) => {
          releaseCommand = resolve;
        });
      if (keypad.locked) {
        keypad.pending = false;
        keypad.requested_brightness = null;
        publish();
        return send({
          operation_id: value.operation_id,
          status: 'rejected',
          result: {
            status: 'rejected',
            code: 'brightness-locked',
            possibly_sent: false,
            brightness: keypad.brightness,
          },
        });
      }
      keypad.brightness = value.target;
      keypad.observed_revision++;
      keypad.pending = false;
      keypad.can_set = true;
      keypad.requested_brightness = null;
      lamp.observed.keypad_brightness = value.target;
      publish();
      return send({
        operation_id: value.operation_id,
        status: 'completed',
        result: { status: 'confirmed', code: null, possibly_sent: true, brightness: value.target },
      });
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
  page
    .locator('.preference-row')
    .filter({ has: page.getByText('Configured brightness', { exact: true }) });
const wait = async (fn) => page.waitForFunction(fn);
try {
  await page.goto(`http://127.0.0.1:${server.address().port}/#/devices/noncamera%3Akeypad-one`);
  const grant = page.getByRole('switch', { name: 'Allow keypad controls', exact: true }),
    lock = page.getByRole('switch', { name: 'Brightness lock', exact: true }),
    slider = page.getByRole('slider', { name: 'Keypad brightness', exact: true }),
    apply = page.getByRole('button', { name: 'Apply brightness', exact: true });
  await grant.waitFor();
  assert.equal(await grant.isChecked(), false);
  assert.equal(await lock.isChecked(), true);
  assert.equal(await slider.isDisabled(), true);
  await observed().getByText('40%', { exact: true }).waitFor();
  await grant.click();
  await page.getByRole('dialog').getByRole('button', { name: 'Cancel', exact: true }).click();
  assert.equal(policyWrites.length, 0);
  await grant.click();
  await password();
  await wait(() => document.querySelector('[aria-label="Brightness lock"]').disabled === false);
  assert.equal(policyWrites.length, 1);
  assert.equal(policyWrites[0].locked, true);
  assert.equal(commands.length, 0);
  assert.equal(await slider.isDisabled(), true);
  checks.push(
    'grant requires password; cancellation sends nothing; enable remains locked without physical command',
  );
  await lock.click();
  await wait(() => document.querySelector('[aria-label="Keypad brightness"]').disabled === false);
  assert.equal(lockWrites.length, 1);
  assert.equal(commands.length, 0);
  await slider.fill('65');
  assert.equal(commands.length, 0);
  await apply.click();
  await observed().getByText('65%', { exact: true }).waitFor();
  assert.equal(commands.length, 1);
  assert.equal(commands[0].target, 65);
  assert.equal(reauths, 1);
  await lock.click();
  await wait(() => document.querySelector('[aria-label="Brightness lock"]').checked === true);
  assert.equal(await slider.isDisabled(), true);
  assert.equal(
    await page.getByRole('button', { name: 'Turn off', exact: true }).isDisabled(),
    true,
  );
  assert.equal(commands.length, 1);
  checks.push(
    'explicit Apply writes one bounded brightness; locking keeps observed level and disables room-light controls',
  );
  await lock.click();
  await wait(() => document.querySelector('[aria-label="Keypad brightness"]').disabled === false);
  delayCommand = true;
  await slider.fill('80');
  await apply.click();
  await wait(() =>
    document.body.textContent.includes('Waiting for Ring to report the brightness.'),
  );
  assert.equal(await lock.isDisabled(), false);
  await lock.click();
  await wait(() => document.querySelector('[aria-label="Brightness lock"]').checked === true);
  assert.ok(releaseCommand);
  releaseCommand();
  delayCommand = false;
  await page.getByText('rejected', { exact: true }).waitFor();
  await observed().getByText('65%', { exact: true }).waitFor();
  assert.equal(commands.length, 2);
  checks.push(
    'local lock stays usable while a brightness request is pending and no optimistic level replaces readback',
  );
  lamp.source_health = 'offline';
  publish();
  await observed().getByText('Unknown', { exact: true }).waitFor();
  assert.equal(await lock.isDisabled(), false);
  assert.equal(await slider.isDisabled(), true);
  lamp.source_health = 'online';
  publish();
  await observed().getByText('65%', { exact: true }).waitFor();
  await page
    .getByText('Waiting for this keypad to report its first proximity event.', { exact: false })
    .waitFor();
  lamp.capabilities.push('motion');
  lamp.capability_evidence.push('ring-keypad-motion-impulse');
  lamp.observed.motion_detected = true;
  publish();
  await page.getByText('Detected', { exact: true }).waitFor();
  lamp.observed.motion_detected = false;
  publish();
  await page.getByText('No recent detection', { exact: true }).waitFor();
  checks.push(
    'offline brightness is unknown; lock remains local; proximity stays unknown before real evidence',
  );
  for (const [theme, width, height] of [
    ['dark', 1440, 1000],
    ['light', 1440, 1000],
    ['dark', 390, 844],
    ['light', 390, 844],
  ]) {
    await page.emulateMedia({ colorScheme: theme });
    await page.setViewportSize({ width, height });
    await page.waitForTimeout(50);
    assert.equal(
      await page.evaluate(() => document.documentElement.scrollWidth > innerWidth),
      false,
    );
    if (output)
      await page.screenshot({
        path: resolve(output, `keypad-${theme}-${width}.png`),
        fullPage: true,
      });
  }
  await page.getByRole('button', { name: 'Reset slider', exact: true }).click();
  assert.equal(await slider.inputValue(), '65');
  assert.equal(commands.length, 2);
  assert.deepEqual(errors, []);
  assert.equal(
    requests.some((row) => row.path.includes('/cameras/')),
    false,
  );
  checks.push('desktop/mobile light/dark layouts; no page errors or camera requests');
  const report = {
    schema: 1,
    physical_device_commands: false,
    checks,
    policy_writes: policyWrites.length,
    lock_writes: lockWrites.length,
    fake_brightness_commands: commands.length,
    errors,
  };
  if (output)
    await writeFile(resolve(output, 'keypad-browser.json'), JSON.stringify(report, null, 2) + '\n');
  console.log(JSON.stringify(report));
} finally {
  clearInterval(timer);
  for (const response of subscribers) response.end();
  server.closeAllConnections();
  await browser.close();
  await new Promise((resolve) => server.close(resolve));
}
