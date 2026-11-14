import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  authority,
  configurationBody,
  isFresh,
  pairingIdentity,
  selectionBody,
  tri,
  validState,
  samePairingScope,
  canResumeReset,
} from '../src/lib/model.ts';
import type {
  CameraPage,
  ConfigurationView,
  Device,
  PairingCamera,
  StateEnvelope,
} from '../src/lib/types.ts';
const state = {
  schema: 1,
  web: { boot_id: 'web-one', observed_at: 10 },
  daemon: { available: true, stale: false, observed_at: 10, error: null },
  state: {
    schema: 1,
    observed_at: 10,
    daemon: {
      boot_id: 'daemon',
      version: 'test',
      uptime_seconds: 1,
      stopping: false,
      release: { managed: false },
    },
    ring: { authenticated: true, restoring: false, events: 'connected', inventory_complete: true },
    truncated: false,
    epoch: 'account-one',
    scope: { profile_id: 'p', account_generation: 3 },
    homekit: { adapter_boot_id: 'a', registry_revision: 4, state: 'published' },
    devices: [],
    activity: { entries: [] },
  },
  cursor: 1,
} as unknown as StateEnvelope;
test('stale/disconnected data never appears current and unknown is not false', () => {
  assert.equal(isFresh(state, true, 12_000), true);
  assert.equal(isFresh(state, false, 12_000), false);
  assert.equal(isFresh(state, true, 26_000), false);
  assert.equal(
    isFresh({ ...state, daemon: { ...state.daemon, stale: true } }, true, 12_000),
    false,
  );
  assert.equal(tri(null), 'Unknown');
  assert.equal(tri(false), 'No');
});
test('authority changes on account/adapter/registry rather than observation time', () => {
  assert.equal(authority(state), authority({ ...state, web: { ...state.web, observed_at: 11 } }));
  assert.notEqual(authority(state), authority({ ...state, web: { ...state.web, boot_id: 'new' } }));
  assert.notEqual(
    authority(state),
    authority({ ...state, state: { ...state.state!, epoch: 'new-account' } }),
  );
});
test('selection sends a scoped toggle and revision, never replacement camera list', () => {
  const device = {
    camera_id: '42',
    scope: { profile_id: 'p', account_generation: 3, device_generation: 9 },
  } as Device;
  assert.deepEqual(selectionBody(device, 8, false, 'op'), {
    camera_id: '42',
    enabled: false,
    profile_id: 'p',
    account_generation: 3,
    expected_revision: 8,
    operation_id: 'op',
  });
});
test('configuration retains original revision and limits editable fields', () => {
  const saved = {
    version: 1,
    revision: 7,
    mode: 'homekit',
    plugin_host: '127.0.0.1',
    plugin_port: 18443,
    rtsp_host: '127.0.0.1',
    rtsp_port: 18554,
  };
  const view: ConfigurationView = { saved, applied: { ...saved }, restart_required: false };
  const draft = { ...saved, mode: 'ring-only' };
  const body = configurationBody(view, draft, 'op');
  assert.equal(body.expected_revision, 7);
  assert.equal(body.configuration.mode, 'ring-only');
  assert.equal('revision' in body.configuration, false);
  assert.equal(view.saved.mode, 'homekit');
  assert.equal(draft.mode, 'ring-only');
});
test('pairing requires current boot, revision, and selected camera', () => {
  const page = {
    profile_id: 'p',
    account_generation: 3,
    registry_revision: 4,
    boot_id: 'boot',
  } as CameraPage<PairingCamera>;
  const camera = { camera_id: '42', selected: true, pairing_revision: 9 } as PairingCamera;
  assert.equal(pairingIdentity(page, camera).pairing_revision, 9);
  assert.throws(() => pairingIdentity(page, { ...camera, pairing_revision: null }));
  assert.throws(() => pairingIdentity({ ...page, boot_id: null }, camera));
  assert.throws(() => pairingIdentity(page, { ...camera, selected: false }));
});
test('state rejects oversized and malformed summaries', () => {
  assert.equal(validState(state), true);
  assert.equal(validState({ ...state, schema: 2 }), false);
  assert.equal(
    validState({ ...state, state: { ...state.state, devices: Array(257).fill({}) } }),
    false,
  );
  assert.equal(validState(null), false);
});

test('late pairing replies cannot cross camera, scope, or panel refresh boundaries', () => {
  const request = { generation: 'account1:adapter1', deviceId: '42', serial: 1 };
  assert.equal(samePairingScope(request, { ...request }), true);
  assert.equal(samePairingScope(request, { ...request, deviceId: '43' }), false);
  assert.equal(samePairingScope(request, { ...request, generation: 'account2:adapter2' }), false);
  assert.equal(samePairingScope(request, { ...request, serial: 2 }), false);
});
test('only the original pending reset operation is resumable', () => {
  const camera = { pending_reset: true, reset_operation_id: 'reset-one' } as PairingCamera;
  assert.equal(canResumeReset(camera, 'reset-one'), true);
  assert.equal(canResumeReset(camera, 'reset-two'), false);
  assert.equal(canResumeReset({ ...camera, pending_reset: false }, 'reset-one'), false);
  assert.equal(canResumeReset(undefined, 'reset-one'), false);
});
