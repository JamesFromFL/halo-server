import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  keypadBrightnessBody,
  keypadCanSet,
  keypadLockBody,
  keypadPolicyBody,
  qualifiedKeypad,
  validKeypad,
} from '../src/lib/keypad.ts';
import { validNoncamera } from '../src/lib/noncamera.ts';
import type { KeypadState, NoncameraDevice, NoncameraState } from '../src/lib/types.ts';
const keypad: KeypadState = {
  policy_revision: 1,
  enabled: true,
  locked: false,
  provider_generation: 2,
  epoch: 3,
  observed_revision: 4,
  brightness: 40,
  on_brightness: 40,
  authorization: 'allowed',
  can_set: true,
  can_set_lock: true,
  reason: null,
  pending: false,
  outcome_unknown: false,
  requested_brightness: null,
};
const device: NoncameraDevice = {
  id: 'keypad-one',
  name: 'Bedroom keypad',
  kind: 'keypad',
  model: 'security-keypad',
  family: 'hub',
  provider: 'ring-hub',
  capabilities: ['keypad-brightness'],
  capability_evidence: ['ring-hub-device-schema'],
  location_id: 'home-one',
  location_name: 'Home',
  observed: { keypad_brightness: 40 },
  available: true,
  source_health: 'online',
  stale: false,
  observed_at: 100,
  supported: true,
  unsupported_reason: null,
  keypad,
};
const monitoring: NoncameraState = {
  schema: 1,
  revision: 1,
  policy_revision: 1,
  scope: { profile_id: 'account-one', account_generation: 1 },
  state: 'ready',
  complete: true,
  observed_at: 100,
  devices: [device],
  enabled: true,
  safety: { read_only: false, panic_enabled: false, emergency_dispatch_enabled: false },
};
function variant(change: Partial<KeypadState>, extra: Partial<NoncameraDevice> = {}) {
  const row = { ...device, ...extra, keypad: { ...keypad, ...change } };
  return { row, state: { ...monitoring, devices: [row] } };
}
test('keypad DTO validates finite bounded brightness and exact qualification', () => {
  assert.equal(validKeypad(keypad), true);
  assert.equal(validNoncamera(monitoring), true);
  for (const change of [
    { brightness: -1 },
    { brightness: 101 },
    { brightness: 1.2 },
    { brightness: true },
    { brightness: NaN },
    { requested_brightness: Infinity },
    { on_brightness: 0 },
    { locked: 1 },
    { locked: true, can_set: true },
    { can_set_lock: undefined },
    { policy_revision: -1 },
  ])
    assert.equal(validKeypad({ ...keypad, ...change }), false);
  for (const change of [
    { kind: 'security-panel' },
    { model: 'other' },
    { provider: 'ring-rest' },
    { family: 'base_stations' },
    { supported: false },
    { capabilities: [] },
    { capability_evidence: [] },
  ]) {
    const { row, state } = variant({}, change);
    assert.equal(qualifiedKeypad(row), false);
    assert.equal(validNoncamera(state), row.kind === 'keypad');
    assert.equal(keypadCanSet(state, row, true), false);
  }
});
test('brightness changes require current unlocked authorized observations', () => {
  assert.equal(keypadCanSet(monitoring, device, true), true);
  assert.equal(keypadBrightnessBody(monitoring, device, 0, 'operation').target, 0);
  for (const change of [
    { locked: true, can_set: false },
    { enabled: false },
    { brightness: null },
    { authorization: 'unknown' as const },
    { pending: true },
    { outcome_unknown: true },
    { can_set: false },
  ]) {
    const { row, state } = variant(change);
    assert.equal(keypadCanSet(state, row, true), false);
    assert.throws(() => keypadBrightnessBody(state, row, 40, 'operation'));
  }
  assert.equal(keypadCanSet(monitoring, device, false), false);
  assert.throws(() => keypadBrightnessBody(monitoring, device, 101, 'operation'));
});
test('local lock stays available while a physical write is pending or source offline', () => {
  const { row, state } = variant({ pending: true, can_set: false }, { source_health: 'offline' });
  assert.equal(keypadLockBody(state, row, true, 'operation').locked, true);
  assert.equal(keypadCanSet(state, row, true), false);
  for (const change of [{ can_set_lock: false }, { enabled: false }]) {
    const { row, state } = variant(change);
    assert.throws(() => keypadLockBody(state, row, true, 'operation'));
  }
});
test('every body carries scoped revisions and no physical fields in local policy', () => {
  assert.deepEqual(keypadPolicyBody(monitoring, device, true, true, 'op'), {
    profile_id: 'account-one',
    account_generation: 1,
    device_id: 'keypad-one',
    expected_revision: 1,
    enabled: true,
    locked: true,
    operation_id: 'op',
  });
  assert.deepEqual(keypadLockBody(monitoring, device, true, 'op'), {
    profile_id: 'account-one',
    account_generation: 1,
    device_id: 'keypad-one',
    expected_policy_revision: 1,
    locked: true,
    operation_id: 'op',
  });
  const body = keypadBrightnessBody(monitoring, device, 75, 'op');
  assert.equal(body.expected_epoch, 3);
  assert.equal(body.expected_revision, 4);
  assert.equal(body.expected_policy_revision, 1);
  assert.equal(body.provider_generation, 2);
  assert.throws(() => keypadPolicyBody({ ...monitoring, devices: [] }, device, true, true, 'op'));
});
