/** Isolated Chime fixtures. Every write is handled locally; no Ring or HAP access. */
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
if (output && !output.startsWith('/')) throw new Error('HALO_BROWSER_OUTPUT must be absolute');
if (output) await mkdir(output, { recursive: true });
const subscribers = new Set(),
  requests = [],
  policyWrites = [],
  commands = [],
  checks = [],
  errors = [];
let reauths = 0,
  delayCommand = false,
  releaseCommand;
let outcome = {
  status: 'completed',
  result: { status: 'uncertain', code: 'readback-unconfirmed', possibly_sent: true },
};
const chime = {
  policy_revision: 3,
  enabled: false,
  sound_tests_enabled: false,
  provider_generation: 4,
  epoch: 5,
  observed_revision: 6,
  authorization: 'allowed',
  can_set: false,
  reason: 'controls-disabled',
  pending: false,
  outcome_unknown: false,
  requested_action: null,
  volume: 4,
  snooze_seconds_left: 0,
  automatic_nightlight: false,
  sound_active: false,
};
const bell = {
  id: 'chime-one',
  name: 'Hallway Chime',
  kind: 'chime',
  model: 'Chime Pro v2 (legacy contract)',
  location_id: 'location-one',
  location_name: 'Home',
  capabilities: ['volume'],
  observed: {},
  available: true,
  source_health: 'online',
  stale: false,
  observed_at: Date.now() / 1000,
  supported: true,
  unsupported_reason: null,
  chime,
};
const contact = {
  ...bell,
  id: 'contact-one',
  name: 'Front door',
  kind: 'contact-sensor',
  model: 'Contact Sensor',
  capabilities: ['contact'],
  observed: { contact_open: false },
  chime: undefined,
};
const monitoring = {
  schema: 1,
  revision: 1,
  policy_revision: 1,
  scope: { profile_id: 'account-one', account_generation: 2 },
  state: 'ready',
  complete: true,
  observed_at: Date.now() / 1000,
  devices: [bell, contact],
  enabled: true,
  safety: { read_only: true, panic_enabled: false, emergency_dispatch_enabled: false },
};
const owner = {
  schema: 1,
  epoch: 'owner-one',
  observed_at: Date.now() / 1000,
  daemon: {
    boot_id: 'daemon-one',
    version: 'fixture',
    uptime_seconds: 100,
    stopping: false,
    release: { managed: false },
  },
  ring: { authenticated: true, restoring: false, events: 'connected', inventory_complete: true },
  scope: monitoring.scope,
  homekit: {
    enabled: true,
    adapter_boot_id: 'camera-one',
    state: 'ready',
    registry_revision: 1,
    selected_count: 0,
    published_count: 0,
    recording_revision: 0,
  },
  devices: [],
  noncamera: monitoring,
  activity: { epoch: 'activity-one', cursor: 0, capacity: 100, entries: [], truncated: false },
  truncated: false,
};
function snapshot() {
  const now = Date.now() / 1000;
  owner.observed_at = now;
  return {
    schema: 1,
    web: { boot_id: 'web-one', observed_at: now },
    daemon: { available: true, stale: false, observed_at: now, error: null },
    state: owner,
    cursor: 'fixture',
  };
}
function publish() {
  monitoring.revision++;
  for (const response of subscribers)
    response.write(`event: state\ndata: ${JSON.stringify(snapshot())}\n\n`);
}
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
    assert.equal(value.device_id, bell.id);
    if (path === '/api/web/v1/devices/chime/policy') {
      assert.deepEqual(
        Object.keys(value).sort(),
        [
          'profile_id',
          'account_generation',
          'device_id',
          'expected_revision',
          'enabled',
          'sound_tests_enabled',
          'operation_id',
        ].sort(),
      );
      assert.equal(value.expected_revision, chime.policy_revision);
      assert.ok(!value.sound_tests_enabled || value.enabled);
      policyWrites.push(value);
      Object.assign(chime, {
        enabled: value.enabled,
        sound_tests_enabled: value.sound_tests_enabled,
        policy_revision: chime.policy_revision + 1,
        can_set: value.enabled,
        reason: value.enabled ? null : 'controls-disabled',
      });
      monitoring.safety.read_only = !value.enabled;
      publish();
      return send({
        operation_id: value.operation_id,
        status: 'completed',
        result: { enabled: value.enabled },
      });
    }
    if (path === '/api/web/v1/devices/chime/set') {
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
          'action',
          'value',
          'operation_id',
        ].sort(),
      );
      assert.equal(value.provider_generation, chime.provider_generation);
      assert.equal(value.expected_epoch, chime.epoch);
      assert.equal(value.expected_revision, chime.observed_revision);
      assert.equal(value.expected_policy_revision, chime.policy_revision);
      assert.equal(chime.enabled, true);
      assert.equal(chime.authorization, 'allowed');
      assert.equal(chime.pending, false);
      commands.push(value);
      chime.requested_action = value.action;
      if (value.action === 'test') {
        assert.equal(chime.sound_tests_enabled, true);
        assert.equal(chime.sound_active, false);
        chime.sound_active = true;
        chime.can_set = false;
      }
      if (value.action === 'sound_stopped') {
        assert.equal(value.value, null);
        assert.equal(chime.sound_active, true);
        chime.sound_active = false;
        chime.outcome_unknown = false;
        chime.can_set = true;
        chime.requested_action = null;
      } else chime.outcome_unknown = outcome.result.status === 'uncertain';
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
page.on('pageerror', (error) => errors.push(error.message));
const button = (name) => page.getByRole('button', { name, exact: true });
async function enabled(name, value = true) {
  await page.waitForFunction(
    ({ name, value }) =>
      [...document.querySelectorAll('button')].find((x) => x.textContent.trim() === name)
        ?.disabled === !value,
    { name, value },
  );
}
async function password() {
  const dialog = page.getByRole('dialog', { name: 'Confirm your Halo password' });
  await dialog.waitFor();
  await dialog.getByLabel('Password', { exact: true }).fill('fixture-password-123');
  await dialog.getByRole('button', { name: 'Confirm', exact: true }).click();
}
const observed = (label) =>
  page.locator('.preference-row').filter({ has: page.getByText(label, { exact: true }) });
try {
  await page.goto(`http://127.0.0.1:${server.address().port}/#/devices/noncamera%3Achime-one`);
  const controls = page.getByRole('switch', { name: 'Allow Chime controls', exact: true });
  const sounds = page.getByRole('switch', { name: 'Allow Chime sound tests', exact: true });
  const volume = page.getByRole('combobox', { name: 'Chime volume', exact: true });
  await controls.waitFor();
  assert.equal(await controls.isChecked(), false);
  assert.equal(await sounds.isChecked(), false);
  assert.equal(await button('Test Ring sound').isDisabled(), true);
  assert.equal(await volume.inputValue(), '4');
  await controls.click();
  assert.equal(policyWrites.length, 0);
  await button('Apply Chime preferences').click();
  await page.getByRole('dialog').getByRole('button', { name: 'Cancel', exact: true }).click();
  assert.equal(policyWrites.length, 0);
  await button('Apply Chime preferences').click();
  await password();
  await enabled('Snooze for 1 hour');
  assert.equal(policyWrites.length, 1);
  assert.equal(commands.length, 0);
  assert.equal(chime.sound_tests_enabled, false);
  assert.equal(await button('Test Ring sound').isDisabled(), true);
  checks.push(
    'control grant and audible grant start off separately; explicit Apply and password cancel never actuate',
  );

  await volume.selectOption('8');
  assert.equal(commands.length, 0);
  await button('Apply volume').click();
  await button('Check outcome').waitFor();
  assert.equal(commands.length, 1);
  assert.equal(reauths, 1);
  assert.match(await observed('Observed volume').innerText(), /4 \/ 11/);
  await button('Check outcome').click();
  assert.equal(commands.length, 1);
  Object.assign(chime, {
    volume: 8,
    observed_revision: 7,
    outcome_unknown: false,
    requested_action: null,
  });
  publish();
  await button('Use current observed state').click();
  await enabled('Snooze for 1 hour');
  assert.match(await observed('Observed volume').innerText(), /8 \/ 11/);
  checks.push(
    'volume drafts send once on Apply; uncertain response preserves observed state and recovery never replays',
  );

  await sounds.click();
  await button('Apply Chime preferences').click();
  await password();
  await enabled('Test Ring sound');
  assert.equal(policyWrites.length, 2);
  assert.equal(reauths, 2);
  await button('Test Ring sound').click();
  await page.getByRole('dialog').getByRole('button', { name: 'Cancel', exact: true }).click();
  assert.equal(commands.length, 1);
  await button('Test Ring sound').click();
  await page
    .getByRole('dialog')
    .getByRole('button', { name: 'Play Ring sound', exact: true })
    .click();
  await page.getByRole('dialog', { name: 'Confirm your Halo password' }).waitFor();
  chime.observed_revision++;
  publish();
  await password();
  await page.getByText('Chime state changed. Review it before starting a sound test.').waitFor();
  assert.equal(commands.length, 1);
  outcome = {
    status: 'completed',
    result: { status: 'accepted', code: null, possibly_sent: true },
  };
  await button('Test Ring sound').click();
  await page
    .getByRole('dialog')
    .getByRole('button', { name: 'Play Ring sound', exact: true })
    .click();
  await password();
  await button('I have confirmed sound stopped').waitFor();
  assert.equal(commands.length, 2);
  assert.equal(commands.at(-1).value, 'ding');
  assert.match(
    await page.locator('.operation').innerText(),
    /does not confirm audible playback or completion/,
  );
  assert.equal(await button('Test Motion sound').isDisabled(), true);
  assert.equal(await volume.isDisabled(), true);
  const authBeforeAck = reauths;
  outcome = {
    status: 'completed',
    result: { status: 'confirmed', code: null, possibly_sent: false },
  };
  await button('I have confirmed sound stopped').click();
  await enabled('Test Motion sound');
  assert.equal(commands.at(-1).action, 'sound_stopped');
  assert.equal(reauths, authBeforeAck);
  checks.push(
    'each sound needs deliberate confirmation and password; source changes invalidate it; accepted playback stays locked until local acknowledgement',
  );

  outcome = {
    status: 'completed',
    result: { status: 'uncertain', code: 'reply-lost', possibly_sent: true },
  };
  await button('Test Motion sound').click();
  await page
    .getByRole('dialog')
    .getByRole('button', { name: 'Play Motion sound', exact: true })
    .click();
  await password();
  await enabled('I have confirmed sound stopped');
  assert.equal(chime.outcome_unknown, true);
  outcome = {
    status: 'completed',
    result: { status: 'confirmed', code: null, possibly_sent: false },
  };
  await button('I have confirmed sound stopped').click();
  await enabled('Test Motion sound');
  checks.push(
    'lost sound reply retains its lock and permits only explicit sound-stopped recovery, never an automatic repeat',
  );

  outcome = {
    status: 'completed',
    result: { status: 'confirmed', code: null, possibly_sent: true },
  };
  await page
    .getByRole('combobox', { name: 'Chime snooze duration', exact: true })
    .selectOption('1440');
  const beforeSnooze = commands.length;
  await button('Snooze for 24 hours').click();
  await enabled('Snooze for 24 hours');
  assert.equal(commands.length, beforeSnooze + 1);
  assert.equal(commands.at(-1).value, 1440);
  assert.match(await observed('Snooze').innerText(), /Not snoozed/);
  Object.assign(chime, {
    snooze_seconds_left: 86000,
    observed_revision: chime.observed_revision + 1,
  });
  publish();
  await enabled('Clear snooze');
  await button('Clear snooze').click();
  await enabled('Clear snooze');
  assert.equal(commands.at(-1).value, 0);
  const night = page.getByRole('switch', {
    name: 'Enable automatic Chime nightlight',
    exact: true,
  });
  await night.click();
  await enabled('Snooze for 24 hours');
  assert.equal(commands.at(-1).action, 'nightlight');
  assert.equal(commands.at(-1).value, true);
  assert.equal(await night.isChecked(), false);
  Object.assign(chime, {
    volume: null,
    snooze_seconds_left: null,
    automatic_nightlight: null,
    observed_revision: chime.observed_revision + 1,
  });
  publish();
  await night.waitFor({ state: 'detached' });
  await enabled('Snooze for 24 hours', false);
  assert.match(await observed('Observed volume').innerText(), /Unknown/);
  assert.match(await observed('Snooze').innerText(), /Unknown/);
  checks.push(
    'snooze and automatic-nightlight actions use exact values and preserve observations; unknown fields never become zero or lamp state',
  );

  Object.assign(chime, {
    volume: 8,
    snooze_seconds_left: 0,
    automatic_nightlight: true,
    observed_revision: chime.observed_revision + 1,
  });
  publish();
  if (await button('Use reported volume').count()) await button('Use reported volume').click();
  for (const changes of [
    { authorization: 'unknown' },
    { authorization: 'denied' },
    { pending: true },
    { can_set: false },
  ]) {
    const saved = { ...chime };
    Object.assign(chime, changes);
    publish();
    await enabled('Test Ring sound', false);
    Object.assign(chime, saved);
    publish();
    await enabled('Test Ring sound');
  }
  bell.source_health = 'offline';
  publish();
  await enabled('Test Ring sound', false);
  assert.match(await observed('Observed volume').innerText(), /Unknown/);
  bell.source_health = 'online';
  bell.stale = true;
  bell.available = null;
  publish();
  await enabled('Test Ring sound');
  checks.push(
    'unknown or denied authority, pending actions and offline source disable commands; unrelated stale diagnostics do not hide fresh Chime fields',
  );

  await volume.selectOption('6');
  delayCommand = true;
  await button('Apply volume').click();
  await page.waitForFunction(
    () => document.querySelector('[aria-label="Chime volume"]')?.disabled === true,
  );
  while (!releaseCommand) await new Promise((resolve) => setTimeout(resolve, 5));
  monitoring.scope = { profile_id: 'account-two', account_generation: 3 };
  owner.scope = monitoring.scope;
  chime.epoch++;
  chime.provider_generation++;
  chime.volume = 5;
  chime.requested_action = null;
  publish();
  releaseCommand();
  delayCommand = false;
  await enabled('Test Ring sound');
  assert.equal(await page.locator('.operation').count(), 0);
  assert.match(await observed('Observed volume').innerText(), /5 \/ 11/);
  checks.push('late command response cannot repaint a replacement account or provider epoch');

  for (const scheme of ['light', 'dark'])
    for (const width of [320, 768, 1440]) {
      await page.emulateMedia({ colorScheme: scheme });
      await page.setViewportSize({ width, height: 1000 });
      await page.locator('.toast').waitFor({ state: 'detached' });
      await page.evaluate(() => {
        document.activeElement?.blur();
        window.scrollTo(0, 0);
      });
      assert.equal(
        await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth),
        true,
      );
      if (output)
        await page.screenshot({
          path: resolve(output, `chime-${scheme}-${width}.png`),
          fullPage: true,
        });
    }
  checks.push('six responsive GTK-style light/dark layouts have no horizontal overflow');
  const countBeforeDisable = commands.length,
    authBeforeDisable = reauths;
  await controls.click();
  await button('Apply Chime preferences').click();
  await enabled('Test Ring sound', false);
  assert.equal(chime.sound_tests_enabled, false);
  assert.equal(commands.length, countBeforeDisable);
  assert.equal(reauths, authBeforeDisable);
  await page.goto(`http://127.0.0.1:${server.address().port}/#/devices/noncamera%3Acontact-one`);
  await page.getByRole('heading', { name: 'Front door', exact: true }).waitFor();
  assert.equal(await page.getByRole('switch', { name: 'Allow Chime controls' }).count(), 0);
  assert.equal(requests.filter((request) => /\/cameras(?:\/|$)/.test(request.path)).length, 0);
  checks.push(
    'explicit disable removes both grants without a device action; unrelated sensors make no Chime or camera administration calls',
  );
  if (process.env.HALO_CHIME_FIXTURE) {
    const fixture = JSON.parse(await readFile(process.env.HALO_CHIME_FIXTURE, 'utf8'));
    const row = fixture.devices.find((device) => device.chime);
    assert.ok(row);
    owner.noncamera = fixture;
    owner.scope = fixture.scope;
    const mutationCount = commands.length + policyWrites.length;
    publish();
    await page.goto(
      `http://127.0.0.1:${server.address().port}/#/devices/${encodeURIComponent(`noncamera:${row.id}`)}`,
    );
    await page.getByRole('heading', { name: row.name, exact: true }).waitFor();
    await page.getByRole('switch', { name: 'Allow Chime controls', exact: true }).waitFor();
    await page
      .getByText(
        'Chime settings and sound tests are available in Halo. This Chime has no HomeKit mapping yet.',
      )
      .waitFor();
    assert.equal(
      await page.getByRole('switch', { name: 'Include in Halo Bridge', exact: true }).count(),
      0,
    );
    assert.equal(commands.length + policyWrites.length, mutationCount);
    if (output)
      await page.screenshot({ path: resolve(output, 'chime-backend.png'), fullPage: true });
    checks.push('actual Python Chime DTO renders without a device action or preference write');
  }
  assert.deepEqual(errors, []);
  const receipt = {
    checks,
    policyWrites: policyWrites.length,
    commands: commands.length,
    reauths,
    pageErrors: errors,
  };
  if (output)
    await writeFile(resolve(output, 'receipt.json'), JSON.stringify(receipt, null, 2) + '\n');
  process.stdout.write(JSON.stringify(receipt, null, 2) + '\n');
} finally {
  releaseCommand?.();
  clearInterval(timer);
  for (const response of subscribers) response.end();
  await browser.close();
  await new Promise((resolve) => server.close(resolve));
}
