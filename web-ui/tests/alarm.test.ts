import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  alarmCanCommand,
  alarmCanAcknowledge,
  alarmConfirmed,
  alarmModeBody,
  alarmModeLabel,
  alarmPhaseLabel,
  alarmPolicyBody,
  alarmSignalLabel,
  alarmUnsettled,
  validAlarm,
} from '../src/lib/alarm.ts';
import { validNoncamera } from '../src/lib/noncamera.ts';
import type {
  AlarmMode,
  AlarmState,
  NoncameraDevice,
  NoncameraState,
  Operation,
} from '../src/lib/types.ts';

const alarm: AlarmState = {
  policy_revision: 3,
  enabled: true,
  provider_generation: 4,
  epoch: 5,
  observed_revision: 6,
  mode: 'disarmed',
  phase: 'idle',
  signal: 'none',
  authorization: 'allowed',
  can_set_mode: true,
  allowed_targets: ['disarmed', 'home', 'away'],
  reason: null,
  pending: false,
  outcome_unknown: false,
  requested_mode: null,
};
const panel: NoncameraDevice = {
  id: 'panel-id',
  name: 'Security panel',
  kind: 'security-panel',
  model: 'Panel',
  location_id: 'location-id',
  location_name: 'Home',
  capabilities: ['mode'],
  observed: {},
  available: true,
  source_health: 'online',
  stale: false,
  observed_at: 100,
  supported: true,
  unsupported_reason: null,
  alarm,
};
const monitoring: NoncameraState = {
  schema: 1,
  revision: 8,
  policy_revision: 1,
  scope: { profile_id: 'profile-one', account_generation: 2 },
  state: 'ready',
  complete: true,
  observed_at: 100,
  devices: [panel],
  enabled: true,
  safety: { read_only: false, panic_enabled: false, emergency_dispatch_enabled: false },
};

test('alarm DTO is additive and requires a real panel with complete guarded state', () => {
  assert.equal(validAlarm(alarm), true);
  assert.equal(validNoncamera(monitoring), true);
  assert.equal(
    validNoncamera({
      ...monitoring,
      devices: [{ ...panel, alarm: undefined }],
      safety: { ...monitoring.safety, read_only: true },
    }),
    true,
  );
  for (const changes of [
    { mode: 'night' },
    { authorization: 'owner-maybe' },
    { epoch: -1 },
    { observed_revision: true },
    { can_set_mode: 'true' },
    { allowed_targets: undefined },
    { allowed_targets: ['panic'] },
    { allowed_targets: ['home', 'home'] },
    { allowed_targets: [null] },
    { pending: undefined },
    { requested_mode: 'panic' },
    { outcome_unknown: null },
  ])
    assert.equal(validAlarm({ ...alarm, ...changes }), false);
  assert.equal(
    validNoncamera({ ...monitoring, devices: [{ ...panel, kind: 'base-station' }] }),
    false,
  );
  assert.equal(
    validNoncamera({ ...monitoring, safety: { ...monitoring.safety, panic_enabled: true } }),
    false,
  );
});

test('alarm policy changes are per location and carry only account and policy CAS', () => {
  assert.deepEqual(alarmPolicyBody(monitoring, panel, false, 'operation-one'), {
    profile_id: 'profile-one',
    account_generation: 2,
    location_id: 'location-id',
    expected_revision: 3,
    enabled: false,
    operation_id: 'operation-one',
  });
  assert.throws(() =>
    alarmPolicyBody(
      { ...monitoring, scope: { profile_id: null, account_generation: null } },
      panel,
      true,
      'one',
    ),
  );
  assert.throws(() => alarmPolicyBody(monitoring, { ...panel, kind: 'keypad' }, true, 'one'));
  assert.throws(() => alarmPolicyBody({ ...monitoring, devices: [] }, panel, true, 'one'));
});

test('normal alarm requests carry provider, epoch, observed and policy fences with no bypass', () => {
  assert.deepEqual(alarmModeBody(monitoring, panel, 'home', 'operation-one'), {
    profile_id: 'profile-one',
    account_generation: 2,
    device_id: 'panel-id',
    provider_generation: 4,
    expected_epoch: 5,
    expected_revision: 6,
    expected_policy_revision: 3,
    target: 'home',
    operation_id: 'operation-one',
  });
  for (const target of ['panic', 'night', 'bypass', 'disarm', 'unknown'])
    assert.throws(() => alarmModeBody(monitoring, panel, target as AlarmMode, 'one'));
});

test('stale, unavailable, unverified, disabled and pending alarm state never enables commands', () => {
  assert.equal(alarmCanCommand(monitoring, panel, true), true);
  assert.equal(
    alarmCanCommand(monitoring, { ...panel, stale: true, available: null }, true),
    true,
    'old battery data does not invalidate fresh alarm fields',
  );
  assert.equal(alarmCanCommand(monitoring, panel, false), false);
  assert.equal(alarmCanCommand({ ...monitoring, enabled: false }, panel, true), false);
  for (const device of [
    { ...panel, source_health: 'stale' as const },
    { ...panel, source_health: 'unknown' as const },
    { ...panel, source_health: 'offline' as const },
    ...[
      { enabled: false },
      { authorization: 'unknown' },
      { authorization: 'denied' },
      { pending: true },
      { outcome_unknown: true },
      { can_set_mode: false },
      { allowed_targets: [] },
      { mode: null },
    ].map((changes) => ({ ...panel, alarm: { ...alarm, ...changes } as AlarmState })),
  ]) {
    assert.equal(alarmCanCommand(monitoring, device, true), false);
    assert.throws(() => alarmModeBody(monitoring, device, 'away', 'one'));
  }
});

test('target-specific restrictions preserve permitted disarm without enabling other modes', () => {
  const restricted = {
    ...panel,
    alarm: { ...alarm, allowed_targets: ['disarmed'] as AlarmMode[] },
  };
  assert.equal(alarmCanCommand(monitoring, restricted, true), true);
  assert.equal(alarmModeBody(monitoring, restricted, 'disarmed', 'one').target, 'disarmed');
  assert.throws(() => alarmModeBody(monitoring, restricted, 'home', 'one'));
  assert.throws(() => alarmModeBody(monitoring, restricted, 'away', 'one'));
});

test('missing modes and phases remain unknown rather than appearing disarmed', () => {
  assert.equal(alarmModeLabel(null), 'Unknown');
  assert.equal(alarmModeLabel('disarmed'), 'Disarmed');
  assert.equal(alarmPhaseLabel(null), 'Unknown');
  assert.equal(alarmPhaseLabel('unrecognized'), 'Unknown');
  assert.equal(alarmPhaseLabel('entry-delay'), 'Entry delay');
  assert.equal(alarmPhaseLabel('exit-delay'), 'Exit delay');
  assert.equal(alarmPhaseLabel('alarming'), 'Alarm triggered');
  assert.equal(alarmSignalLabel(null), 'Unknown');
  assert.equal(alarmSignalLabel('unrecognized'), 'Unknown');
  assert.equal(alarmSignalLabel('none'), 'None reported');
  assert.equal(alarmSignalLabel('fire'), 'Fire');
});

test('only confirmed results are success; accepted and uncertain never imply observed state', () => {
  const result = (status: string): Operation => ({
    operation_id: 'one',
    status: 'completed',
    result: { status },
  });
  assert.equal(alarmConfirmed(result('confirmed')), true);
  for (const status of ['accepted', 'busy', 'uncertain', 'rejected'])
    assert.equal(alarmConfirmed(result(status)), false);
  assert.equal(alarmUnsettled(result('uncertain')), true);
  assert.equal(alarmUnsettled({ operation_id: 'one', status: 'unknown' }), true);
  assert.equal(alarmUnsettled({ operation_id: 'one', status: 'pending' }), true);
  assert.equal(alarmUnsettled(result('confirmed')), false);
});

test('uncertain receipt recovery requires a newer fresh permitted observation and no pending action', () => {
  const uncertain: Operation = {
    operation_id: 'one',
    status: 'unknown',
    result: { status: 'uncertain' },
  };
  assert.equal(alarmCanAcknowledge(monitoring, panel, true, uncertain, 5), true);
  assert.equal(alarmCanAcknowledge(monitoring, panel, true, uncertain, 6), false);
  assert.equal(alarmCanAcknowledge(monitoring, panel, true, uncertain, null), false);
  assert.equal(alarmCanAcknowledge(monitoring, panel, false, uncertain, 5), false);
  assert.equal(
    alarmCanAcknowledge(monitoring, panel, true, { ...uncertain, status: 'pending' }, 5),
    false,
  );
  for (const changes of [
    { outcome_unknown: true },
    { pending: true },
    { can_set_mode: false },
    { allowed_targets: [] },
  ])
    assert.equal(
      alarmCanAcknowledge(
        monitoring,
        { ...panel, alarm: { ...alarm, ...changes } },
        true,
        uncertain,
        5,
      ),
      false,
    );
});
