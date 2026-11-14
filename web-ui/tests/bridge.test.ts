import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  bridgeBody,
  bridgeDevicePreference,
  bridgeDeviceStatus,
  validBridge,
} from '../src/lib/bridge.ts';
import { validNoncamera } from '../src/lib/noncamera.ts';
import type { BridgeState, NoncameraState } from '../src/lib/types.ts';

const bridge: BridgeState = {
  revision: 9,
  configured: true,
  enabled: true,
  auto_import: false,
  accessory_uuid: 'bridge-identity',
  username: 'CA:FE:00:00:00:01',
  capacity: 149,
  selected_count: 1,
  retained_count: 2,
  overflow_count: 0,
  included_devices: ['included-missing'],
  excluded_devices: ['excluded-missing'],
  excluded_locations: ['unlisted-location'],
  excluded_kinds: ['unlisted-kind'],
  excluded_mappings: ['smoke'],
  publication: 'not_implemented',
  status: 'ready',
  devices: [
    {
      id: 'front-door',
      accessory_uuid: 'child-identity',
      mappings: ['contact'],
      selected: true,
      excluded: false,
      eligible: true,
      reason: null,
      present: true,
    },
  ],
  truncated: true,
};
const monitoring: NoncameraState = {
  schema: 1,
  revision: 3,
  policy_revision: 1,
  scope: { profile_id: 'account-one', account_generation: 4 },
  state: 'partial',
  complete: false,
  observed_at: null,
  devices: [],
  enabled: true,
  safety: { read_only: true, panic_enabled: false, emergency_dispatch_enabled: false },
  bridge,
};

test('bridge preferences preserve hidden and missing device policy with the registry CAS scope', () => {
  const body = bridgeBody(monitoring, { enabled: false }, 'operation-one');
  assert.deepEqual(body, {
    profile_id: 'account-one',
    account_generation: 4,
    expected_revision: 9,
    enabled: false,
    auto_import: false,
    included_devices: ['included-missing'],
    excluded_devices: ['excluded-missing'],
    excluded_locations: ['unlisted-location'],
    excluded_kinds: ['unlisted-kind'],
    excluded_mappings: ['smoke'],
    operation_id: 'operation-one',
  });
  body.excluded_devices.push('changed-copy');
  assert.deepEqual(bridge.excluded_devices, ['excluded-missing']);
});

test('individual import choices are explicit and keep every unrelated exclusion', () => {
  const included = bridgeDevicePreference(bridge, 'front-door', true);
  assert.deepEqual(included, {
    included_devices: ['front-door', 'included-missing'],
    excluded_devices: ['excluded-missing'],
  });
  const excluded = bridgeDevicePreference({ ...bridge, ...included }, 'front-door', false);
  assert.deepEqual(excluded, {
    included_devices: ['included-missing'],
    excluded_devices: ['excluded-missing', 'front-door'],
  });
  assert.deepEqual(bridgeBody(monitoring, included, 'operation-two').excluded_locations, [
    'unlisted-location',
  ]);
  assert.deepEqual(bridgeBody(monitoring, included, 'operation-two').excluded_mappings, ['smoke']);
});

test('bridge preparation refuses missing account and unavailable registry state', () => {
  assert.throws(() => bridgeBody({ ...monitoring, bridge: undefined }, {}, 'one'));
  for (const status of ['error', 'loading', 'offline'] as const)
    assert.throws(() => bridgeBody({ ...monitoring, bridge: { ...bridge, status } }, {}, 'one'));
  assert.throws(() =>
    bridgeBody({ ...monitoring, scope: { profile_id: null, account_generation: null } }, {}, 'one'),
  );
});

test('bridge DTO validation is additive and rejects unsafe or incomplete preference snapshots', () => {
  assert.equal(validBridge(bridge), true);
  assert.equal(validNoncamera(monitoring), true);
  assert.equal(validNoncamera({ ...monitoring, bridge: undefined }), true);
  const invalid = [
    { ...bridge, revision: -1 },
    { ...bridge, publication: 'published' },
    { ...bridge, status: 'unknown' },
    { ...bridge, selected_count: 150 },
    { ...bridge, excluded_mappings: ['panic'] },
    { ...bridge, excluded_devices: ['included-missing'] },
    { ...bridge, devices: [bridge.devices[0], bridge.devices[0]] },
    { ...bridge, excluded_devices: Array.from({ length: 512 }, (_, index) => `device-${index}`) },
    { ...bridge, devices: [{ ...bridge.devices[0], selected: null }] },
    { ...bridge, excluded_locations: undefined },
  ];
  for (const value of invalid) {
    assert.equal(validBridge(value), false);
    assert.equal(validNoncamera({ ...monitoring, bridge: value }), false);
  }
});

test('import status distinguishes missing, excluded, unsupported and bridge selection', () => {
  const device = bridge.devices[0]!;
  assert.equal(bridgeDeviceStatus(undefined), 'Import information unavailable');
  assert.equal(bridgeDeviceStatus(device), 'Selected for Halo Bridge');
  assert.equal(
    bridgeDeviceStatus({ ...device, reason: 'bridge-capacity', selected: false }),
    'Bridge capacity reached',
  );
  assert.equal(
    bridgeDeviceStatus({ ...device, reason: 'registry-capacity', selected: false }),
    'Device identity capacity reached',
  );
  assert.equal(
    bridgeDeviceStatus({ ...device, reason: 'capability-mismatch', eligible: false }),
    'Capabilities need verification',
  );
  assert.equal(
    bridgeDeviceStatus({ ...device, reason: 'bridge-disabled' }),
    'Bridge preparation disabled',
  );
  assert.equal(
    bridgeDeviceStatus({ ...device, present: false }),
    'Selected; source temporarily missing',
  );
  assert.equal(bridgeDeviceStatus({ ...device, excluded: true, selected: false }), 'Excluded');
  assert.equal(
    bridgeDeviceStatus({ ...device, eligible: false, selected: false }),
    'Not eligible for import',
  );
  assert.equal(
    bridgeDeviceStatus({ ...device, present: false, selected: false }),
    'Source temporarily missing',
  );
});
