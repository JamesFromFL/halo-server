import type {
  BridgeDevice,
  BridgePreferences,
  BridgeRuntime,
  BridgeState,
  NoncameraState,
} from './types.ts';

export const mappingLabels: Record<string, string> = {
  contact: 'Contact',
  motion: 'Motion',
  leak: 'Water leak',
  smoke: 'Smoke',
  carbon_monoxide: 'Carbon monoxide',
  security_system: 'Security system',
  lightbulb: 'Lightbulb',
};

const count = (value: unknown): value is number =>
  typeof value === 'number' && Number.isSafeInteger(value) && value >= 0;
const text = (value: unknown): value is string =>
  typeof value === 'string' && value.length > 0 && value.length <= 512;
const optionalText = (value: unknown) => value === null || text(value);
const textList = (value: unknown, maximum: number): value is string[] =>
  Array.isArray(value) &&
  value.length <= maximum &&
  value.every(text) &&
  new Set(value).size === value.length;

export function validBridgeRuntime(value: unknown): value is BridgeRuntime {
  if (!value || typeof value !== 'object') return false;
  const runtime = value as BridgeRuntime;
  return (
    ['disabled', 'starting', 'ready', 'offline', 'error', 'reset_pending'].includes(
      runtime.state,
    ) &&
    optionalText(runtime.reason) &&
    optionalText(runtime.boot_id) &&
    typeof runtime.published === 'boolean' &&
    typeof runtime.publication_enabled === 'boolean' &&
    count(runtime.publication_revision) &&
    typeof runtime.reset_pending === 'boolean' &&
    optionalText(runtime.reset_operation_id)
  );
}

export function validBridge(value: unknown): value is BridgeState {
  if (!value || typeof value !== 'object') return false;
  const bridge = value as BridgeState;
  return (
    count(bridge.revision) &&
    typeof bridge.configured === 'boolean' &&
    typeof bridge.enabled === 'boolean' &&
    typeof bridge.auto_import === 'boolean' &&
    optionalText(bridge.accessory_uuid) &&
    optionalText(bridge.username) &&
    count(bridge.capacity) &&
    bridge.capacity === 149 &&
    count(bridge.selected_count) &&
    bridge.selected_count <= bridge.capacity &&
    count(bridge.retained_count) &&
    count(bridge.overflow_count) &&
    textList(bridge.included_devices, 512) &&
    textList(bridge.excluded_devices, 512) &&
    bridge.included_devices.length + bridge.excluded_devices.length <= 512 &&
    textList(bridge.excluded_locations, 128) &&
    textList(bridge.excluded_kinds, 64) &&
    textList(bridge.excluded_mappings, 7) &&
    bridge.excluded_mappings.every((mapping) => Object.hasOwn(mappingLabels, mapping)) &&
    !bridge.included_devices.some((id) => bridge.excluded_devices.includes(id)) &&
    (bridge.publication === 'not_implemented' ||
      (bridge.publication === 'implemented' && validBridgeRuntime(bridge.runtime))) &&
    ['ready', 'loading', 'error', 'offline'].includes(bridge.status) &&
    typeof bridge.truncated === 'boolean' &&
    Array.isArray(bridge.devices) &&
    bridge.devices.length <= 256 &&
    new Set(bridge.devices.map((device) => device?.id)).size === bridge.devices.length &&
    bridge.devices.every(
      (device) =>
        device &&
        text(device.id) &&
        optionalText(device.accessory_uuid) &&
        textList(device.mappings, 32) &&
        typeof device.selected === 'boolean' &&
        typeof device.excluded === 'boolean' &&
        typeof device.eligible === 'boolean' &&
        optionalText(device.reason) &&
        typeof device.present === 'boolean',
    )
  );
}

export function bridgePreferences(bridge: BridgeState): BridgePreferences {
  return {
    enabled: bridge.enabled,
    auto_import: bridge.auto_import,
    included_devices: [...bridge.included_devices],
    excluded_devices: [...bridge.excluded_devices],
    excluded_locations: [...bridge.excluded_locations],
    excluded_kinds: [...bridge.excluded_kinds],
    excluded_mappings: [...bridge.excluded_mappings],
  };
}

/** Preferences replace the complete saved policy; never reconstruct it from a cropped list. */
export function bridgeBody(
  monitoring: NoncameraState,
  patch: Partial<BridgePreferences>,
  operation_id: string,
) {
  const bridge = monitoring.bridge;
  if (!bridge || bridge.status !== 'ready')
    throw new Error('Refresh Halo Bridge preferences before making this change.');
  if (!monitoring.scope.profile_id || monitoring.scope.account_generation == null)
    throw new Error('Connect a Ring account before changing Halo Bridge preferences.');
  const preferences = { ...bridgePreferences(bridge), ...patch };
  return {
    profile_id: monitoring.scope.profile_id,
    account_generation: monitoring.scope.account_generation,
    expected_revision: bridge.revision,
    enabled: preferences.enabled,
    auto_import: preferences.auto_import,
    included_devices: [...preferences.included_devices],
    excluded_devices: [...preferences.excluded_devices],
    excluded_locations: [...preferences.excluded_locations],
    excluded_kinds: [...preferences.excluded_kinds],
    excluded_mappings: [...preferences.excluded_mappings],
    operation_id,
  };
}

export function changeMembership(values: string[], id: string, include: boolean): string[] {
  return include ? [...new Set([...values, id])].sort() : values.filter((value) => value !== id);
}

export function bridgeDevicePreference(bridge: BridgeState, id: string, include: boolean) {
  return {
    included_devices: changeMembership(bridge.included_devices, id, include),
    excluded_devices: changeMembership(bridge.excluded_devices, id, !include),
  };
}

export function bridgeDeviceStatus(device: BridgeDevice | undefined): string {
  if (!device) return 'Import information unavailable';
  const reasons: Record<string, string> = {
    'bridge-not-configured': 'Bridge not prepared',
    'device-excluded': 'Excluded',
    'location-excluded': 'Location excluded',
    'kind-excluded': 'Device type excluded',
    'mappings-excluded': 'All eligible capabilities excluded',
    'capability-mismatch': 'Capabilities need verification',
    'registry-capacity': 'Device identity capacity reached',
    'bridge-capacity': 'Bridge capacity reached',
    'bridge-disabled': 'Bridge preparation disabled',
    'device-missing': 'Selected; source temporarily missing',
  };
  if (device.reason && Object.hasOwn(reasons, device.reason)) return reasons[device.reason]!;
  if (!device.eligible) return 'Not eligible for import';
  if (device.excluded) return 'Excluded';
  if (!device.present)
    return device.selected ? 'Selected; source temporarily missing' : 'Source temporarily missing';
  return device.selected ? 'Selected for Halo Bridge' : 'Not selected';
}
