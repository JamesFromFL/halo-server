import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  cameraDetailTarget,
  deviceMatches,
  deviceObservations,
  monitoringBody,
  monitoringStatus,
  noncameraDevice,
  noncameraHealth,
  noncameraRouteId,
  validNoncamera,
} from '../src/lib/noncamera.ts';
import { validState } from '../src/lib/model.ts';
import type { NoncameraDevice, NoncameraState, StateEnvelope } from '../src/lib/types.ts';

const sensor: NoncameraDevice = {
  id: 'ring-device-one',
  name: 'Front door',
  kind: 'contact-sensor',
  model: 'Contact Sensor',
  location_id: 'location-one',
  location_name: 'Home',
  capabilities: ['contact'],
  observed: { contact_open: false, battery_percent: null, tampered: false },
  available: true,
  stale: false,
  observed_at: 10,
  supported: true,
  unsupported_reason: null,
};
const monitoring: NoncameraState = {
  schema: 1,
  revision: 13,
  policy_revision: 2,
  scope: { profile_id: 'profile-one', account_generation: 4 },
  state: 'ready',
  complete: true,
  observed_at: 10,
  devices: [sensor],
  safety: { read_only: true, panic_enabled: false, emergency_dispatch_enabled: false },
  enabled: true,
};
const snapshot = {
  schema: 1,
  web: { boot_id: 'web-one', observed_at: 10 },
  daemon: { available: true, stale: false, observed_at: 10, error: null },
  state: {
    schema: 1,
    epoch: 'one',
    observed_at: 10,
    daemon: { release: {} },
    ring: { authenticated: true },
    scope: { profile_id: 'profile-one', account_generation: 4 },
    homekit: { state: 'published' },
    devices: [
      {
        id: sensor.id,
        camera_id: '42',
        name: 'Camera',
        model: 'Floodlight',
        scope: {},
        capabilities: [],
        observed: {},
        homekit: { selected: true },
      },
    ],
    noncamera: monitoring,
    activity: { entries: [] },
  },
} as unknown as StateEnvelope;

test('only actual camera routes admit camera administration, even on an ID collision', () => {
  const route = noncameraRouteId(sensor.id);
  assert.equal(cameraDetailTarget(snapshot, route), undefined);
  assert.equal(noncameraDevice(monitoring, route), sensor);
  assert.equal(cameraDetailTarget(snapshot, '42')?.name, 'Camera');
  assert.equal(cameraDetailTarget(snapshot, sensor.id)?.name, 'Camera');
  assert.equal(cameraDetailTarget(snapshot, 'unknown'), undefined);
  assert.equal(cameraDetailTarget(null, '42'), undefined);
  assert.equal(noncameraDevice(monitoring, sensor.id), undefined);
});

test('monitoring toggles carry account scope and policy revision, never alarm commands', () => {
  assert.deepEqual(monitoringBody(monitoring, false, 'stop-one'), {
    profile_id: 'profile-one',
    account_generation: 4,
    expected_revision: 2,
    enabled: false,
    operation_id: 'stop-one',
  });
  assert.throws(() =>
    monitoringBody(
      { ...monitoring, scope: { profile_id: null, account_generation: null } },
      true,
      'one',
    ),
  );
});

test('missing sensor state never means closed, clear, or disarmed', () => {
  const observations = deviceObservations({
    ...sensor,
    observed: {
      contact_open: null,
      alarm_mode: null,
      smoke_detected: false,
      battery_percent: null,
      tampered: true,
      raw_password: 'must not display',
    },
  });
  assert.equal(observations.find((item) => item.key === 'contact_open')?.value, 'Unknown');
  assert.equal(observations.find((item) => item.key === 'alarm_mode')?.value, 'Unknown');
  assert.equal(observations.find((item) => item.key === 'smoke_detected')?.value, 'Not detected');
  assert.equal(observations.find((item) => item.key === 'battery_percent')?.value, 'Unknown');
  assert.equal(observations.find((item) => item.key === 'tampered')?.value, 'Detected');
  assert.equal(
    observations.some((item) => item.key === 'raw_password'),
    false,
  );
  assert.equal(noncameraHealth(sensor, true), 'Available');
  assert.equal(noncameraHealth({ ...sensor, available: null }, true), 'Availability unknown');
  assert.equal(noncameraHealth({ ...sensor, available: false }, true), 'Unavailable');
  assert.equal(noncameraHealth({ ...sensor, stale: true }, true), 'Out of date');
  assert.equal(noncameraHealth(sensor, false), 'Out of date');
});

test('partial discovery and disabled monitoring never claim a complete healthy provider', () => {
  assert.equal(
    monitoringStatus({ ...monitoring, state: 'partial', complete: false }, true),
    'Some sources unavailable',
  );
  assert.equal(
    monitoringStatus({ ...monitoring, state: 'disabled', enabled: false }, true),
    'Disabled',
  );
  assert.equal(monitoringStatus(monitoring, false), 'Status unknown');
  assert.equal(monitoringStatus(undefined, true), 'Not available in this server version');
});

test('device filters use real location identity and device type', () => {
  assert.equal(deviceMatches(sensor, 'home', 'location-one', 'contact-sensor'), true);
  assert.equal(deviceMatches(sensor, 'front', 'different-location'), false);
  assert.equal(deviceMatches(sensor, '', '', 'motion-sensor'), false);
  assert.equal(deviceMatches(sensor, 'CAMERA'), false);
});

test('additive state remains compatible and rejects unsafe or malformed device projections', () => {
  assert.equal(validNoncamera(monitoring), true);
  assert.equal(validState(snapshot), true);
  const oldState = structuredClone(snapshot);
  delete oldState.state!.noncamera;
  assert.equal(validState(oldState), true);
  assert.equal(
    validState({
      ...snapshot,
      state: {
        ...snapshot.state,
        devices: [],
        scope: { profile_id: null, account_generation: null },
      },
    }),
    true,
    'independent monitoring remains usable before camera inventory is ready',
  );
  assert.equal(
    validState({
      ...snapshot,
      state: { ...snapshot.state, scope: { profile_id: 'other-profile', account_generation: 9 } },
    }),
    false,
    'a previous account inventory cannot appear under a new account scope',
  );
  for (const invalid of [
    { ...monitoring, policy_revision: -1 },
    { ...monitoring, safety: { ...monitoring.safety, panic_enabled: true } },
    { ...monitoring, safety: { ...monitoring.safety, emergency_dispatch_enabled: true } },
    { ...monitoring, safety: { ...monitoring.safety, read_only: 'false' } },
    { ...monitoring, devices: [sensor, sensor] },
    { ...monitoring, devices: [{ ...sensor, observed: { nested: { raw: true } } }] },
    { ...monitoring, devices: [{ ...sensor, stale: undefined }] },
    { ...monitoring, devices: [{ ...sensor, available: 0 }] },
    {
      ...monitoring,
      devices: Array.from({ length: 257 }, (_, i) => ({ ...sensor, id: `id-${i}` })),
    },
  ]) {
    assert.equal(validNoncamera(invalid), false);
    assert.equal(
      validState({ ...snapshot, state: { ...snapshot.state, noncamera: invalid } }),
      false,
    );
  }
});
