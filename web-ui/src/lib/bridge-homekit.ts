import type { BridgePairing, BridgePairingSecret, BridgeRuntime, NoncameraState } from './types.ts';

const count = (value: unknown): value is number =>
  typeof value === 'number' && Number.isSafeInteger(value) && value >= 0;
const text = (value: unknown): value is string =>
  typeof value === 'string' && value.length > 0 && value.length <= 128;
const optionalText = (value: unknown) => value === null || text(value);
const digest = (value: unknown): value is string =>
  typeof value === 'string' && /^[a-f0-9]{64}$/.test(value);

export function validBridgePairing(value: unknown): value is BridgePairing {
  if (!value || typeof value !== 'object') return false;
  const pairing = value as BridgePairing;
  return (
    text(pairing.profile_id) &&
    count(pairing.account_generation) &&
    pairing.account_generation > 0 &&
    count(pairing.registry_revision) &&
    count(pairing.publication_revision) &&
    text(pairing.accessory_id) &&
    text(pairing.username) &&
    optionalText(pairing.boot_id) &&
    ['available', 'published', 'paired_known', 'paired', 'pending_reset'].every(
      (key) => typeof pairing[key as keyof BridgePairing] === 'boolean',
    ) &&
    count(pairing.paired_clients) &&
    (pairing.pairing_revision === null || digest(pairing.pairing_revision)) &&
    optionalText(pairing.reset_operation_id)
  );
}

export function bridgePairingMatches(pairing: BridgePairing, monitoring: NoncameraState): boolean {
  const bridge = monitoring.bridge;
  return (
    !!bridge &&
    pairing.profile_id === monitoring.scope.profile_id &&
    pairing.account_generation === monitoring.scope.account_generation &&
    pairing.accessory_id === bridge.accessory_uuid &&
    pairing.registry_revision === bridge.revision &&
    pairing.publication_revision === bridge.runtime?.publication_revision &&
    pairing.boot_id === bridge.runtime?.boot_id
  );
}

export function bridgePublicationBody(
  monitoring: NoncameraState,
  enabled: boolean,
  operation_id: string,
) {
  const bridge = monitoring.bridge;
  if (
    !bridge ||
    bridge.publication !== 'implemented' ||
    !bridge.runtime ||
    !bridge.configured ||
    bridge.status !== 'ready' ||
    !monitoring.scope.profile_id ||
    monitoring.scope.account_generation === null
  )
    throw new Error('Refresh Halo Bridge before changing its HomeKit connection.');
  if (enabled && (!monitoring.enabled || !bridge.enabled || bridge.runtime.reset_pending))
    throw new Error('Enable device monitoring and prepare Halo Bridge before connecting it.');
  return {
    profile_id: monitoring.scope.profile_id,
    account_generation: monitoring.scope.account_generation,
    expected_registry_revision: bridge.revision,
    expected_revision: bridge.runtime.publication_revision,
    enabled,
    operation_id,
  };
}

export function bridgePairingIdentity(pairing: BridgePairing, reset = false) {
  const resuming = reset && pairing.pending_reset && !!pairing.reset_operation_id;
  if (
    !validBridgePairing(pairing) ||
    !pairing.pairing_revision ||
    (!resuming && (!pairing.boot_id || !pairing.available || !pairing.published))
  )
    throw new Error('Pairing information changed. Refresh Halo Bridge before continuing.');
  return {
    profile_id: pairing.profile_id,
    account_generation: pairing.account_generation,
    registry_revision: pairing.registry_revision,
    accessory_id: pairing.accessory_id,
    boot_id: pairing.boot_id,
    pairing_revision: pairing.pairing_revision,
  };
}

export function canResumeBridgeReset(
  pairing: BridgePairing | null,
  operation_id?: string,
): boolean {
  return (
    pairing?.pending_reset === true &&
    !!pairing.reset_operation_id &&
    (!operation_id || operation_id === pairing.reset_operation_id)
  );
}

export function validBridgePairingSecret(
  value: unknown,
  pairing: BridgePairing,
): value is BridgePairingSecret {
  if (!value || typeof value !== 'object') return false;
  const secret = value as BridgePairingSecret;
  return (
    secret.profile_id === pairing.profile_id &&
    secret.account_generation === pairing.account_generation &&
    secret.accessory_id === pairing.accessory_id &&
    secret.boot_id === pairing.boot_id &&
    secret.registry_revision === pairing.registry_revision &&
    secret.pairing_revision === pairing.pairing_revision &&
    typeof secret.pin === 'string' &&
    /^\d{3}-\d{2}-\d{3}$/.test(secret.pin) &&
    typeof secret.setup_uri === 'string' &&
    /^X-HM:\/\/[A-Z0-9]{9,32}$/.test(secret.setup_uri) &&
    count(secret.expires_in_ms) &&
    secret.expires_in_ms > 0 &&
    secret.expires_in_ms <= 300_000
  );
}

export function bridgeRuntimeLabel(runtime: BridgeRuntime | undefined, fresh: boolean): string {
  if (!fresh || !runtime) return 'Unknown';
  if (runtime.reset_pending) return 'Reset needs attention';
  if (runtime.published) return 'Connected to HomeKit';
  return {
    disabled: 'Disabled',
    starting: 'Connecting',
    ready: 'Ready',
    offline: 'Source unavailable',
    error: 'Needs attention',
    reset_pending: 'Reset needs attention',
  }[runtime.state];
}
