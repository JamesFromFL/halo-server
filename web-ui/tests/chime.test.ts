import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  chimeCanAct,
  chimeCanAcknowledge,
  chimeConfirmed,
  chimePolicyBody,
  chimeSetBody,
  chimeSnoozeLabel,
  chimeUnsettled,
  validChime,
} from '../src/lib/chime.ts';
import { validNoncamera } from '../src/lib/noncamera.ts';
import type {
  ChimeAction,
  ChimeState,
  NoncameraDevice,
  NoncameraState,
  Operation,
} from '../src/lib/types.ts';

const chime: ChimeState = {
  policy_revision: 3,
  enabled: true,
  sound_tests_enabled: true,
  provider_generation: 4,
  epoch: 5,
  observed_revision: 6,
  authorization: 'allowed',
  can_set: true,
  reason: null,
  pending: false,
  outcome_unknown: false,
  requested_action: null,
  volume: 4,
  snooze_seconds_left: 0,
  automatic_nightlight: false,
  sound_active: false,
};
const device: NoncameraDevice = {
  id: 'chime-one',
  name: 'Hallway',
  kind: 'chime',
  model: 'chime_pro_v2',
  location_id: 'location-one',
  location_name: 'Home',
  capabilities: ['volume'],
  observed: {},
  available: true,
  source_health: 'online',
  stale: false,
  observed_at: 100,
  supported: true,
  unsupported_reason: null,
  chime,
};
const monitoring: NoncameraState = {
  schema: 1,
  revision: 8,
  policy_revision: 1,
  scope: { profile_id: 'account-one', account_generation: 2 },
  state: 'ready',
  complete: true,
  observed_at: 100,
  devices: [device],
  enabled: true,
  safety: { read_only: false, panic_enabled: false, emergency_dispatch_enabled: false },
};

test('Chime DTO is additive and does not normalize missing volume or snooze into zero', () => {
  assert.equal(validChime(chime), true);
  // A saved sound grant may remain visible while runtime permission is unavailable.
  assert.equal(validChime({ ...chime, enabled: false, sound_tests_enabled: true }), true);
  assert.equal(
    chimeCanAct(monitoring, { ...device, chime: { ...chime, enabled: false } }, true, 'test'),
    false,
  );
  assert.equal(validNoncamera(monitoring), true);
  assert.equal(validNoncamera({ ...monitoring, devices: [{ ...device, chime: undefined }] }), true);
  assert.equal(
    validChime({ ...chime, volume: null, snooze_seconds_left: null, automatic_nightlight: null }),
    true,
  );
  for (const changes of [
    { volume: true },
    { volume: 12 },
    { volume: 1.5 },
    { volume: undefined },
    { snooze_seconds_left: -1 },
    { snooze_seconds_left: '0' },
    { automatic_nightlight: 0 },
    { sound_active: null },
    { epoch: -1 },
    { authorization: 'yes' },
    { requested_action: true },
  ])
    assert.equal(validChime({ ...chime, ...changes }), false);
  for (const kind of ['camera', 'beams-light', 'doorbell', 'unknown'])
    assert.equal(validNoncamera({ ...monitoring, devices: [{ ...device, kind }] }), false);
  assert.equal(chimeSnoozeLabel(0), 'Not snoozed');
  assert.equal(chimeSnoozeLabel(null), 'Unknown');
  assert.equal(chimeSnoozeLabel(undefined), 'Unknown');
  assert.match(chimeSnoozeLabel(30), /when reported/);
});
test('control and audible permissions are separate default-off device-scoped grants', () => {
  assert.deepEqual(chimePolicyBody(monitoring, device, true, false, 'one'), {
    profile_id: 'account-one',
    account_generation: 2,
    device_id: 'chime-one',
    expected_revision: 3,
    enabled: true,
    sound_tests_enabled: false,
    operation_id: 'one',
  });
  assert.throws(() => chimePolicyBody(monitoring, device, false, true, 'one'));
  assert.throws(() => chimePolicyBody({ ...monitoring, devices: [] }, device, true, true, 'one'));
  assert.throws(() =>
    chimePolicyBody(
      { ...monitoring, scope: { profile_id: null, account_generation: null } },
      device,
      true,
      false,
      'one',
    ),
  );
});
test('all Chime actions carry exact identity and state fences with bounded action-specific values', () => {
  for (const [action, value] of [
    ['volume', 0],
    ['volume', 11],
    ['snooze', 0],
    ['snooze', 1440],
    ['nightlight', false],
    ['test', 'ding'],
    ['test', 'motion'],
  ] as const) {
    assert.deepEqual(chimeSetBody(monitoring, device, action, value, 'one'), {
      profile_id: 'account-one',
      account_generation: 2,
      device_id: 'chime-one',
      provider_generation: 4,
      expected_epoch: 5,
      expected_revision: 6,
      expected_policy_revision: 3,
      action,
      value,
      operation_id: 'one',
    });
  }
  for (const [action, value] of [
    ['volume', 12],
    ['volume', true],
    ['snooze', 1441],
    ['snooze', 1.5],
    ['test', 'panic'],
    ['test', true],
    ['nightlight', 'on'],
    ['sound_stopped', true],
    ['reboot', null],
  ])
    assert.throws(() => chimeSetBody(monitoring, device, action as ChimeAction, value, 'one'));
});
test('per-capability evidence, current source, owner grant and sound lock govern each action', () => {
  assert.equal(chimeCanAct(monitoring, device, true, 'volume'), true);
  assert.equal(
    chimeCanAct(monitoring, { ...device, stale: true, available: null }, true, 'volume'),
    true,
  );
  for (const change of [
    { enabled: false, sound_tests_enabled: false },
    { authorization: 'unknown' },
    { authorization: 'denied' },
    { pending: true },
    { outcome_unknown: true },
    { sound_active: true },
    { can_set: false },
  ]) {
    const changed = { ...device, chime: { ...chime, ...change } as ChimeState };
    for (const action of ['volume', 'test', 'snooze', 'nightlight'] as const)
      assert.equal(chimeCanAct(monitoring, changed, true, action), false);
  }
  for (const [action, change] of [
    ['volume', { volume: null }],
    ['snooze', { snooze_seconds_left: null }],
    ['nightlight', { automatic_nightlight: null }],
    ['test', { sound_tests_enabled: false }],
  ] as const)
    assert.equal(
      chimeCanAct(monitoring, { ...device, chime: { ...chime, ...change } }, true, action),
      false,
    );
  assert.equal(chimeCanAct(monitoring, device, false, 'test'), false);
  assert.equal(chimeCanAct({ ...monitoring, enabled: false }, device, true, 'test'), false);
  for (const source_health of ['offline', 'stale', 'unknown', 'unavailable'] as const)
    assert.equal(chimeCanAct(monitoring, { ...device, source_health }, true, 'volume'), false);
});
test('sound stopped acknowledgement clears only the active local lock, including lost replies', () => {
  const locked = {
    ...device,
    chime: { ...chime, sound_active: true, can_set: false, outcome_unknown: true },
  };
  assert.equal(chimeCanAct(monitoring, locked, true, 'sound_stopped'), true);
  assert.equal(chimeSetBody(monitoring, locked, 'sound_stopped', null, 'ack').value, null);
  assert.equal(chimeCanAct(monitoring, device, true, 'sound_stopped'), false);
  assert.equal(
    chimeCanAct(
      monitoring,
      { ...locked, chime: { ...locked.chime, pending: true } },
      true,
      'sound_stopped',
    ),
    false,
  );
  assert.equal(
    chimeCanAct(
      monitoring,
      { ...locked, chime: { ...locked.chime, authorization: 'unknown' } },
      true,
      'sound_stopped',
    ),
    false,
  );
});
test('HTTP sound acceptance is not confirmation and uncertain settings require newer observed evidence', () => {
  const operation: Operation = {
    operation_id: 'one',
    status: 'completed',
    result: { status: 'accepted' },
  };
  assert.equal(chimeConfirmed(operation), false);
  assert.equal(chimeUnsettled(operation), false);
  const uncertain: Operation = { ...operation, result: { status: 'uncertain' } };
  assert.equal(chimeUnsettled(uncertain), true);
  assert.equal(chimeCanAcknowledge(monitoring, device, true, uncertain, 5), true);
  for (const revision of [null, 6, 7])
    assert.equal(chimeCanAcknowledge(monitoring, device, true, uncertain, revision), false);
  assert.equal(
    chimeCanAcknowledge(
      monitoring,
      { ...device, chime: { ...chime, sound_active: true } },
      true,
      uncertain,
      5,
    ),
    false,
  );
  assert.equal(
    chimeCanAcknowledge(monitoring, device, true, { ...uncertain, status: 'pending' }, 5),
    false,
  );
});
