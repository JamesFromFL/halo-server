import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  bridgePairingIdentity,
  bridgePairingMatches,
  bridgePublicationBody,
  bridgeRuntimeLabel,
  canResumeBridgeReset,
  validBridgePairing,
  validBridgePairingSecret,
} from '../src/lib/bridge-homekit.ts';
import { validBridge, validBridgeRuntime } from '../src/lib/bridge.ts';
import type { BridgePairing, BridgeRuntime, NoncameraState } from '../src/lib/types.ts';

const runtime: BridgeRuntime = {
  state: 'ready',
  reason: null,
  boot_id: 'runtime-one',
  published: true,
  publication_enabled: true,
  publication_revision: 4,
  reset_pending: false,
  reset_operation_id: null,
};
const monitoring: NoncameraState = {
  schema: 1,
  revision: 3,
  policy_revision: 1,
  scope: { profile_id: 'account-one', account_generation: 4 },
  state: 'ready',
  complete: true,
  observed_at: 100,
  devices: [],
  enabled: true,
  safety: { read_only: true, panic_enabled: false, emergency_dispatch_enabled: false },
  bridge: {
    revision: 9,
    configured: true,
    enabled: true,
    auto_import: false,
    accessory_uuid: 'bridge-identity',
    username: '02:00:00:00:00:01',
    capacity: 149,
    selected_count: 1,
    retained_count: 1,
    overflow_count: 0,
    included_devices: [],
    excluded_devices: [],
    excluded_locations: [],
    excluded_kinds: [],
    excluded_mappings: [],
    publication: 'implemented',
    runtime,
    status: 'ready',
    devices: [],
    truncated: false,
  },
};
const pairing: BridgePairing = {
  profile_id: 'account-one',
  account_generation: 4,
  registry_revision: 9,
  publication_revision: 4,
  accessory_id: 'bridge-identity',
  username: '02:00:00:00:00:01',
  boot_id: 'runtime-one',
  available: true,
  published: true,
  paired_known: true,
  paired: false,
  paired_clients: 0,
  pairing_revision: 'a'.repeat(64),
  pending_reset: false,
  reset_operation_id: null,
};

test('bridge publication has separate opt-in and CAS revisions from import preparation', () => {
  assert.equal(validBridge(monitoring.bridge), true);
  assert.deepEqual(bridgePublicationBody(monitoring, false, 'operation-one'), {
    profile_id: 'account-one',
    account_generation: 4,
    expected_registry_revision: 9,
    expected_revision: 4,
    enabled: false,
    operation_id: 'operation-one',
  });
  assert.equal(
    validBridge({ ...monitoring.bridge, publication: 'implemented', runtime: undefined }),
    false,
  );
  assert.throws(() =>
    bridgePublicationBody(
      { ...monitoring, bridge: { ...monitoring.bridge!, publication: 'not_implemented' } },
      true,
      'one',
    ),
  );
  for (const state of [
    { ...monitoring, enabled: false },
    { ...monitoring, bridge: { ...monitoring.bridge!, enabled: false } },
    {
      ...monitoring,
      bridge: { ...monitoring.bridge!, runtime: { ...runtime, reset_pending: true } },
    },
  ]) {
    assert.throws(() => bridgePublicationBody(state, true, 'one'));
    assert.equal(bridgePublicationBody(state, false, 'one').enabled, false);
  }
});

test('bridge pairing cannot cross account, registry, publication or child restart boundaries', () => {
  assert.equal(validBridgePairing(pairing), true);
  assert.equal(bridgePairingMatches(pairing, monitoring), true);
  for (const changes of [
    { profile_id: 'another-account' },
    { account_generation: 5 },
    { registry_revision: 10 },
    { publication_revision: 5 },
    { accessory_id: 'another-bridge' },
    { boot_id: 'another-child' },
  ])
    assert.equal(bridgePairingMatches({ ...pairing, ...changes }, monitoring), false);
  assert.deepEqual(bridgePairingIdentity(pairing), {
    profile_id: 'account-one',
    account_generation: 4,
    registry_revision: 9,
    accessory_id: 'bridge-identity',
    boot_id: 'runtime-one',
    pairing_revision: 'a'.repeat(64),
  });
});

test('only an explicitly pending reset can resume without a running adapter', () => {
  const pending = {
    ...pairing,
    boot_id: null,
    available: false,
    published: false,
    pending_reset: true,
    reset_operation_id: 'original-reset',
  };
  assert.equal(canResumeBridgeReset(pending), true);
  assert.equal(canResumeBridgeReset(pending, 'original-reset'), true);
  assert.equal(canResumeBridgeReset(pending, 'other-operation'), false);
  assert.equal(canResumeBridgeReset(pairing), false);
  assert.throws(() => bridgePairingIdentity(pending));
  assert.equal(bridgePairingIdentity(pending, true).boot_id, null);
  assert.equal(bridgePairingIdentity(pending, true).pairing_revision, pairing.pairing_revision);
  assert.throws(() => bridgePairingIdentity({ ...pending, reset_operation_id: null }, true));
  assert.throws(() => bridgePairingIdentity({ ...pending, pairing_revision: null }, true));
});

test('bridge setup secrets require the exact requested account and adapter scope', () => {
  const secret = {
    profile_id: pairing.profile_id,
    account_generation: pairing.account_generation,
    accessory_id: pairing.accessory_id,
    boot_id: pairing.boot_id,
    registry_revision: pairing.registry_revision,
    pairing_revision: pairing.pairing_revision,
    pin: '123-45-678',
    setup_uri: 'X-HM://0023ABCD1WXYZ',
    expires_in_ms: 60_000,
  };
  assert.equal(validBridgePairingSecret(secret, pairing), true);
  for (const changes of [
    { profile_id: 'another-account' },
    { account_generation: 5 },
    { accessory_id: 'another-bridge' },
    { boot_id: 'another-child' },
    { registry_revision: pairing.registry_revision + 1 },
    { pairing_revision: 'b'.repeat(64) },
    { pin: '<script>unsafe</script>' },
    { setup_uri: 'https://outside.example/collect' },
    { expires_in_ms: -1 },
    { expires_in_ms: Infinity },
  ])
    assert.equal(validBridgePairingSecret({ ...secret, ...changes }, pairing), false);
});

test('malformed runtime and pairing metadata do not become authoritative UI state', () => {
  for (const changes of [
    { state: 'not-a-state' },
    { publication_enabled: 'true' },
    { publication_revision: -1 },
    { published: null },
    { reset_pending: undefined },
  ])
    assert.equal(validBridgeRuntime({ ...runtime, ...changes }), false);
  for (const changes of [
    { account_generation: true },
    { available: null },
    { pairing_revision: 1 },
    { pairing_revision: 'not-a-manifest-digest' },
    { paired_known: undefined },
    { paired_clients: -1 },
  ])
    assert.equal(validBridgePairing({ ...pairing, ...changes }), false);
  assert.equal(bridgeRuntimeLabel(runtime, false), 'Unknown');
  assert.equal(bridgeRuntimeLabel(runtime, true), 'Connected to HomeKit');
  assert.equal(
    bridgeRuntimeLabel({ ...runtime, reset_pending: true }, true),
    'Reset needs attention',
  );
});
