import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  lightCanAcknowledge,
  lightCanSet,
  lightConfirmed,
  lightPolicyBody,
  lightSetBody,
  lightStateLabel,
  lightUnsettled,
  validLight,
} from '../src/lib/light.ts';
import { validNoncamera } from '../src/lib/noncamera.ts';
import type {
  LightDuration,
  LightState,
  NoncameraDevice,
  NoncameraState,
  Operation,
} from '../src/lib/types.ts';

const light: LightState = {
  policy_revision: 3,
  enabled: true,
  on_duration_seconds: 60,
  provider_generation: 4,
  epoch: 5,
  observed_revision: 6,
  on: false,
  authorization: 'allowed',
  can_set: true,
  reason: null,
  pending: false,
  outcome_unknown: false,
  requested_on: null,
};
const device: NoncameraDevice = {
  id: 'light-one',
  name: 'Pathway',
  kind: 'beams-light',
  model: 'beams-switch',
  location_id: 'location-one',
  location_name: 'Home',
  capabilities: ['light'],
  observed: {},
  available: true,
  source_health: 'online',
  stale: false,
  observed_at: 100,
  supported: true,
  unsupported_reason: null,
  light,
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

test('light DTO is additive and restricted to qualified physical Beams lights', () => {
  assert.equal(validLight(light), true);
  assert.equal(validNoncamera(monitoring), true);
  assert.equal(validNoncamera({ ...monitoring, devices: [{ ...device, light: undefined }] }), true);
  for (const changes of [
    { on: 'false' },
    { requested_on: 0 },
    { on_duration_seconds: 0 },
    { on_duration_seconds: '60' },
    { epoch: -1 },
    { observed_revision: true },
    { authorization: 'maybe' },
    { can_set: 1 },
    { pending: undefined },
  ])
    assert.equal(validLight({ ...light, ...changes }), false);
  for (const kind of ['lighting-bridge', 'beams-light-group', 'motion-sensor', 'unknown', 'camera'])
    assert.equal(validNoncamera({ ...monitoring, devices: [{ ...device, kind }] }), false);
});

test('saved duration is bounded and policy carries device-scoped CAS without switching the light', () => {
  for (const duration of [30, 60, 300, 900] as LightDuration[])
    assert.deepEqual(lightPolicyBody(monitoring, device, false, duration, 'one'), {
      profile_id: 'account-one',
      account_generation: 2,
      device_id: 'light-one',
      expected_revision: 3,
      enabled: false,
      on_duration_seconds: duration,
      operation_id: 'one',
    });
  for (const value of [0, 59, 3600, NaN, '60', true])
    assert.throws(() => lightPolicyBody(monitoring, device, true, value as LightDuration, 'one'));
  assert.throws(() => lightPolicyBody({ ...monitoring, devices: [] }, device, true, 60, 'one'));
  assert.throws(() =>
    lightPolicyBody(
      { ...monitoring, scope: { profile_id: null, account_generation: null } },
      device,
      true,
      60,
      'one',
    ),
  );
});

test('light request carries fences and boolean target, never a duration, brightness or camera setting', () => {
  assert.deepEqual(lightSetBody(monitoring, device, false, 'one'), {
    profile_id: 'account-one',
    account_generation: 2,
    device_id: 'light-one',
    provider_generation: 4,
    expected_epoch: 5,
    expected_revision: 6,
    expected_policy_revision: 3,
    target: false,
    operation_id: 'one',
  });
  for (const target of ['on', 'off', 1, 0, null])
    assert.throws(() => lightSetBody(monitoring, device, target as unknown as boolean, 'one'));
});

test('light commands reject stale or uncertain authority without conflating battery age with light freshness', () => {
  assert.equal(lightCanSet(monitoring, device, true), true);
  assert.equal(lightCanSet(monitoring, { ...device, stale: true, available: null }, true), true);
  assert.equal(lightCanSet(monitoring, device, false), false);
  assert.equal(lightCanSet({ ...monitoring, enabled: false }, device, true), false);
  for (const changes of [
    { enabled: false },
    { can_set: false },
    { authorization: 'unknown' },
    { authorization: 'denied' },
    { pending: true },
    { outcome_unknown: true },
    { on: null },
  ]) {
    const changed = { ...device, light: { ...light, ...changes } as LightState };
    assert.equal(lightCanSet(monitoring, changed, true), false);
    assert.throws(() => lightSetBody(monitoring, changed, true, 'one'));
  }
  for (const source_health of ['offline', 'stale', 'unknown', 'unavailable'] as const)
    assert.equal(lightCanSet(monitoring, { ...device, source_health }, true), false);
});

test('only explicit observed booleans become On or Off and only confirmed operations indicate success', () => {
  assert.equal(lightStateLabel(false), 'Off');
  assert.equal(lightStateLabel(true), 'On');
  assert.equal(lightStateLabel(null), 'Unknown');
  assert.equal(lightStateLabel(undefined), 'Unknown');
  for (const status of ['accepted', 'uncertain', 'rejected', 'busy'])
    assert.equal(
      lightConfirmed({ operation_id: 'one', status: 'completed', result: { status } }),
      false,
    );
  assert.equal(
    lightConfirmed({ operation_id: 'one', status: 'completed', result: { status: 'confirmed' } }),
    true,
  );
});

test('uncertainty acknowledgement requires a newer permitted observation and cannot dismiss pending work', () => {
  const uncertain: Operation = {
    operation_id: 'one',
    status: 'unknown',
    result: { status: 'uncertain' },
  };
  assert.equal(lightUnsettled(uncertain), true);
  assert.equal(lightCanAcknowledge(monitoring, device, true, uncertain, 5), true);
  for (const revision of [null, 6, 7])
    assert.equal(lightCanAcknowledge(monitoring, device, true, uncertain, revision), false);
  assert.equal(lightCanAcknowledge(monitoring, device, false, uncertain, 5), false);
  assert.equal(
    lightCanAcknowledge(monitoring, device, true, { ...uncertain, status: 'pending' }, 5),
    false,
  );
  assert.equal(
    lightCanAcknowledge(
      monitoring,
      { ...device, light: { ...light, outcome_unknown: true } },
      true,
      uncertain,
      5,
    ),
    false,
  );
});
