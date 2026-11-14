import type { Device, NoncameraDevice, NoncameraState, StateEnvelope } from './types.ts';

const ROUTE_PREFIX = 'noncamera:';
const STATES = new Set(['disabled', 'starting', 'ready', 'partial', 'offline', 'error']);
const boundedText = (value: unknown, limit = 512): value is string =>
  typeof value === 'string' && value.length <= limit;
const revision = (value: unknown): value is number =>
  typeof value === 'number' && Number.isSafeInteger(value) && value >= 0;
const timestamp = (value: unknown) =>
  value === null || (typeof value === 'number' && Number.isFinite(value) && value >= 0);

export function validNoncamera(value: unknown): value is NoncameraState {
  if (!value || typeof value !== 'object') return false;
  const state = value as NoncameraState;
  return (
    state.schema === 1 &&
    revision(state.revision) &&
    revision(state.policy_revision) &&
    !!state.scope &&
    (state.scope.profile_id === null || boundedText(state.scope.profile_id)) &&
    (state.scope.account_generation === null || revision(state.scope.account_generation)) &&
    STATES.has(state.state) &&
    typeof state.complete === 'boolean' &&
    typeof state.enabled === 'boolean' &&
    timestamp(state.observed_at) &&
    state.safety?.read_only === true &&
    state.safety.panic_enabled === false &&
    state.safety.emergency_dispatch_enabled === false &&
    Array.isArray(state.devices) &&
    state.devices.length <= 256 &&
    new Set(state.devices.map((device) => device?.id)).size === state.devices.length &&
    state.devices.every((device) => {
      if (!device || typeof device !== 'object') return false;
      return (
        boundedText(device.id) &&
        device.id.length > 0 &&
        boundedText(device.name) &&
        boundedText(device.kind) &&
        boundedText(device.model) &&
        boundedText(device.location_id) &&
        boundedText(device.location_name) &&
        Array.isArray(device.capabilities) &&
        device.capabilities.length <= 64 &&
        device.capabilities.every((capability) => boundedText(capability, 128)) &&
        !!device.observed &&
        typeof device.observed === 'object' &&
        !Array.isArray(device.observed) &&
        Object.keys(device.observed).length <= 64 &&
        Object.values(device.observed).every(
          (observation) =>
            observation === null ||
            typeof observation === 'boolean' ||
            (typeof observation === 'number' && Number.isFinite(observation)) ||
            boundedText(observation),
        ) &&
        (device.available === null || typeof device.available === 'boolean') &&
        typeof device.stale === 'boolean' &&
        timestamp(device.observed_at) &&
        typeof device.supported === 'boolean' &&
        (device.unsupported_reason === null || boundedText(device.unsupported_reason))
      );
    })
  );
}

export function noncameraRouteId(id: string): string {
  return `${ROUTE_PREFIX}${id}`;
}

export function noncameraDevice(state: NoncameraState | undefined, route: string) {
  return route.startsWith(ROUTE_PREFIX)
    ? state?.devices.find((device) => device.id === route.slice(ROUTE_PREFIX.length))
    : undefined;
}

/** Device administration is camera-specific. Other devices never request those RPCs. */
export function cameraDetailTarget(
  snapshot: StateEnvelope | null,
  route: string,
): Device | undefined {
  if (!route || route.startsWith(ROUTE_PREFIX)) return undefined;
  return snapshot?.state?.devices.find(
    (device) => device.id === route || device.camera_id === route,
  );
}

export function monitoringBody(state: NoncameraState, enabled: boolean, operation_id: string) {
  if (!state.scope.profile_id || state.scope.account_generation == null)
    throw new Error('Connect a Ring account before changing device monitoring.');
  return {
    profile_id: state.scope.profile_id,
    account_generation: state.scope.account_generation,
    expected_revision: state.policy_revision,
    enabled,
    operation_id,
  };
}

export function noncameraHealth(device: NoncameraDevice, fresh: boolean): string {
  if (!fresh || device.stale) return 'Out of date';
  if (device.available === null) return 'Availability unknown';
  return device.available ? 'Available' : 'Unavailable';
}

export function monitoringStatus(state: NoncameraState | undefined, fresh: boolean): string {
  if (!state) return 'Not available in this server version';
  if (!fresh) return 'Status unknown';
  const labels: Record<NoncameraState['state'], string> = {
    disabled: 'Disabled',
    starting: 'Discovering devices',
    ready: 'Monitoring',
    partial: 'Some sources unavailable',
    offline: 'Sources unavailable',
    error: 'Monitoring needs attention',
  };
  return labels[state.state];
}

export function deviceIcon(kind: string): string {
  if (kind.includes('light')) return 'light';
  if (kind.includes('chime')) return 'bell';
  if (kind.includes('lock')) return 'lock';
  if (kind.includes('alarm') || kind.includes('keypad')) return 'shield';
  if (kind.includes('contact')) return 'contact';
  if (kind.includes('motion')) return 'activity';
  return 'devices';
}

export function deviceKind(kind: string): string {
  return kind.replaceAll('_', ' ').replaceAll('-', ' ');
}

export function deviceMatches(device: NoncameraDevice, search: string, location = '', kind = '') {
  return (
    (!location || device.location_id === location) &&
    (!kind || device.kind === kind) &&
    `${device.name} ${device.model} ${device.kind} ${device.location_name}`
      .toLocaleLowerCase()
      .includes(search.toLocaleLowerCase())
  );
}

const observationLabels: Record<
  string,
  { title: string; yes?: string; no?: string; unit?: string }
> = {
  contact_open: { title: 'Contact', yes: 'Open', no: 'Closed' },
  motion: { title: 'Motion', yes: 'Detected', no: 'Not detected' },
  motion_detected: { title: 'Motion', yes: 'Detected', no: 'Not detected' },
  alarm_mode: { title: 'Alarm mode' },
  mode: { title: 'Mode' },
  alarm_state: { title: 'Alarm state' },
  entry_delay: { title: 'Entry delay', unit: ' seconds' },
  exit_delay: { title: 'Exit delay', unit: ' seconds' },
  alarm_triggered: { title: 'Alarm', yes: 'Triggered', no: 'Not triggered' },
  battery_percent: { title: 'Battery', unit: '%' },
  battery_status: { title: 'Battery status' },
  battery_low: { title: 'Low battery', yes: 'Reported', no: 'Not reported' },
  tampered: { title: 'Tamper', yes: 'Detected', no: 'Not detected' },
  tamper: { title: 'Tamper', yes: 'Detected', no: 'Not detected' },
  light: { title: 'Light', yes: 'On', no: 'Off' },
  light_on: { title: 'Light', yes: 'On', no: 'Off' },
  on: { title: 'Power', yes: 'On', no: 'Off' },
  brightness: { title: 'Brightness', unit: '%' },
  locked: { title: 'Lock', yes: 'Locked', no: 'Unlocked' },
  lock_state: { title: 'Lock state' },
  leak: { title: 'Water leak', yes: 'Detected', no: 'Not detected' },
  leak_detected: { title: 'Water leak', yes: 'Detected', no: 'Not detected' },
  smoke: { title: 'Smoke', yes: 'Detected', no: 'Not detected' },
  smoke_detected: { title: 'Smoke', yes: 'Detected', no: 'Not detected' },
  co: { title: 'Carbon monoxide', yes: 'Detected', no: 'Not detected' },
  co_detected: { title: 'Carbon monoxide', yes: 'Detected', no: 'Not detected' },
  temperature_c: { title: 'Temperature', unit: ' °C' },
  temperature_celsius: { title: 'Temperature', unit: ' °C' },
  freeze: { title: 'Freezing', yes: 'Detected', no: 'Not detected' },
  freeze_detected: { title: 'Freezing', yes: 'Detected', no: 'Not detected' },
  connected: { title: 'Source connection', yes: 'Connected', no: 'Disconnected' },
  ac_power: { title: 'External power', yes: 'Connected', no: 'Disconnected' },
  siren_active: { title: 'Siren', yes: 'Active', no: 'Inactive' },
  bypassed: { title: 'Bypassed', yes: 'Yes', no: 'No' },
  faulted: { title: 'Fault', yes: 'Reported', no: 'Not reported' },
  glass_break_detected: { title: 'Glass break', yes: 'Detected', no: 'Not detected' },
  tilt_detected: { title: 'Tilt', yes: 'Detected', no: 'Not detected' },
  valve_open: { title: 'Valve', yes: 'Open', no: 'Closed' },
  alarm_signal: { title: 'Alarm signal' },
  volume: { title: 'Volume' },
  snoozed: { title: 'Chime snooze', yes: 'Enabled', no: 'Disabled' },
  firmware: { title: 'Firmware' },
  power_source: { title: 'Power source' },
};

/** Only the server's normalized observation vocabulary is eligible for display. */
export function deviceObservations(device: NoncameraDevice) {
  return Object.entries(observationLabels)
    .filter(([key]) => Object.hasOwn(device.observed, key))
    .map(([key, labels]) => {
      const value = device.observed[key];
      const rendered =
        value == null
          ? 'Unknown'
          : typeof value === 'boolean'
            ? value
              ? (labels.yes ?? 'Yes')
              : (labels.no ?? 'No')
            : typeof value === 'number'
              ? `${value}${labels.unit ?? ''}`
              : typeof value === 'string'
                ? deviceKind(value)
                : 'Unknown';
      return { key, title: labels.title, value: rendered };
    });
}
