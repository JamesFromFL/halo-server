export type Json = null | boolean | number | string | Json[] | { [key: string]: Json };
export interface Session {
  authenticated: boolean;
  setup_required: boolean;
  csrf_token: string;
  reauthenticated: boolean;
  session_id: string | null;
}
export interface Scope {
  profile_id: string;
  account_generation: number;
  device_generation: number;
}
export interface Device {
  id: string;
  camera_id: string;
  name: string;
  source: string;
  kind: string;
  model: string;
  scope: Scope;
  capabilities: string[];
  observed: {
    source_available: boolean | null;
    battery_percent: number | null;
    light: boolean | null;
    siren: boolean | null;
  };
  homekit: {
    selected: boolean;
    published: boolean | null;
    paired: boolean | null;
    owner_warm: boolean;
    recording_active: boolean | null;
    recording_audio_active: boolean | null;
  };
}
export interface Activity {
  sequence: number;
  at: number;
  category: string;
  action: string;
  outcome: string;
  camera_id: string | null;
}
export interface NoncameraDevice {
  id: string;
  name: string;
  kind: string;
  model: string;
  provider?: string;
  family?: string;
  capability_evidence?: string[];
  location_id: string;
  location_name: string;
  capabilities: string[];
  observed: Record<string, Json>;
  available: boolean | null;
  source_health?: 'unknown' | 'online' | 'offline' | 'stale' | 'unavailable';
  stale: boolean;
  observed_at: number | null;
  supported: boolean;
  unsupported_reason: string | null;
  alarm?: AlarmState;
  light?: LightState;
  chime?: ChimeState;
  keypad?: KeypadState;
}
export interface KeypadState {
  policy_revision: number;
  enabled: boolean;
  locked: boolean;
  provider_generation: number;
  epoch: number;
  observed_revision: number;
  brightness: number | null;
  on_brightness: number;
  authorization: 'allowed' | 'denied' | 'unknown';
  can_set: boolean;
  can_set_lock: boolean;
  reason: string | null;
  pending: boolean;
  outcome_unknown: boolean;
  requested_brightness: number | null;
}
export type ChimeAction = 'volume' | 'test' | 'snooze' | 'nightlight' | 'sound_stopped';
export interface ChimeState {
  policy_revision: number;
  enabled: boolean;
  sound_tests_enabled: boolean;
  provider_generation: number;
  epoch: number;
  observed_revision: number;
  authorization: 'allowed' | 'denied' | 'unknown';
  can_set: boolean;
  reason: string | null;
  pending: boolean;
  outcome_unknown: boolean;
  requested_action: string | null;
  volume: number | null;
  snooze_seconds_left: number | null;
  automatic_nightlight: boolean | null;
  sound_active: boolean;
}
export type LightDuration = 30 | 60 | 300 | 900;
export interface LightState {
  policy_revision: number;
  enabled: boolean;
  on_duration_seconds: LightDuration;
  provider_generation: number;
  epoch: number;
  observed_revision: number;
  on: boolean | null;
  authorization: 'allowed' | 'denied' | 'unknown';
  can_set: boolean;
  reason: string | null;
  pending: boolean;
  outcome_unknown: boolean;
  requested_on: boolean | null;
}
export type AlarmMode = 'disarmed' | 'home' | 'away';
export interface AlarmState {
  policy_revision: number;
  enabled: boolean;
  provider_generation: number;
  epoch: number;
  observed_revision: number;
  mode: AlarmMode | null;
  phase: string | null;
  signal: string | null;
  authorization: 'allowed' | 'denied' | 'unknown';
  can_set_mode: boolean;
  allowed_targets: AlarmMode[];
  target_reasons?: Partial<Record<AlarmMode, string>>;
  reason: string | null;
  pending: boolean;
  outcome_unknown: boolean;
  requested_mode: AlarmMode | null;
}
export interface BridgePreferences {
  enabled: boolean;
  auto_import: boolean;
  included_devices: string[];
  excluded_devices: string[];
  excluded_locations: string[];
  excluded_kinds: string[];
  excluded_mappings: string[];
}
export interface BridgeDevice {
  id: string;
  accessory_uuid: string | null;
  mappings: string[];
  selected: boolean;
  excluded: boolean;
  eligible: boolean;
  reason: string | null;
  present: boolean;
}
export interface BridgeRuntime {
  alarm_events_pending?: number;
  alarm_events_dropped?: number;
  state: 'disabled' | 'starting' | 'ready' | 'offline' | 'error' | 'reset_pending';
  reason: string | null;
  boot_id: string | null;
  published: boolean;
  publication_enabled: boolean;
  publication_revision: number;
  reset_pending: boolean;
  reset_operation_id: string | null;
}
export interface BridgeState extends BridgePreferences {
  revision: number;
  configured: boolean;
  accessory_uuid: string | null;
  username: string | null;
  capacity: number;
  selected_count: number;
  retained_count: number;
  overflow_count: number;
  publication: 'not_implemented' | 'implemented';
  runtime?: BridgeRuntime;
  status: 'ready' | 'loading' | 'error' | 'offline';
  devices: BridgeDevice[];
  truncated: boolean;
}
export interface BridgePairing {
  profile_id: string;
  account_generation: number;
  registry_revision: number;
  publication_revision: number;
  accessory_id: string;
  username: string;
  boot_id: string | null;
  available: boolean;
  published: boolean;
  paired_known: boolean;
  paired: boolean;
  paired_clients: number;
  pairing_revision: string | null;
  pending_reset: boolean;
  reset_operation_id: string | null;
}
export interface BridgePairingSecret extends PairingSetup {
  registry_revision: number;
  pairing_revision: string;
  profile_id: string;
  account_generation: number;
  accessory_id: string;
  boot_id: string;
  [key: string]: unknown;
}
export interface NoncameraState {
  schema: 1;
  revision: number;
  policy_revision: number;
  scope: { profile_id: string | null; account_generation: number | null };
  state: 'disabled' | 'starting' | 'ready' | 'partial' | 'offline' | 'error';
  complete: boolean;
  observed_at: number | null;
  devices: NoncameraDevice[];
  safety: {
    read_only: boolean;
    panic_enabled: false;
    emergency_dispatch_enabled: false;
  };
  enabled: boolean;
  bridge?: BridgeState;
  truncated?: boolean;
}
export interface OwnerState {
  schema: 1;
  epoch: string;
  observed_at: number;
  daemon: {
    boot_id: string;
    version: string;
    uptime_seconds: number;
    stopping: boolean;
    release: { managed: boolean; release_id?: string };
  };
  ring: {
    authenticated: boolean;
    restoring: boolean;
    events: string | Record<string, Json>;
    inventory_complete: boolean;
  };
  scope: { profile_id: string | null; account_generation: number | null };
  homekit: {
    enabled: boolean;
    adapter_boot_id: string | null;
    state: string;
    registry_revision: number | null;
    selected_count: number;
    published_count: number | null;
    recording_revision: number | null;
  };
  devices: Device[];
  noncamera?: NoncameraState;
  activity: {
    epoch: string;
    cursor: number;
    capacity: number;
    entries: Activity[];
    truncated: boolean;
  };
  truncated: boolean;
}
export interface StateEnvelope {
  schema: 1;
  web: { boot_id: string; observed_at: number; draining?: boolean; pending_operations?: number };
  daemon: { available: boolean; stale: boolean; observed_at: number | null; error: string | null };
  state: OwnerState | null;
  cursor: string | number;
}
export interface CameraPage<T = Record<string, Json>> {
  profile_id: string;
  account_generation: number;
  boot_id: string | null;
  revision: number;
  registry_revision: number;
  cameras: T[];
  total: number;
  [key: string]: unknown;
}
export type CameraPrivacyMode = 'ring' | 'homekit';
export interface CameraPrivacy {
  camera_id: string;
  revision: number;
  mode: CameraPrivacyMode;
  ring_live_allowed: boolean | null;
  ring_recording_allowed: boolean | null;
  ring_snapshot_allowed: boolean | null;
  ring_audio_allowed: boolean | null;
  source_fresh: boolean;
  effective_live_allowed: boolean;
  effective_recording_allowed: boolean;
  effective_snapshot_allowed: boolean;
  effective_audio_allowed: boolean;
  reason: string | null;
}
export interface CameraPrivacyPage {
  profile_id: string | null;
  account_generation: number | null;
  available: boolean;
  cameras: CameraPrivacy[];
}
export interface PairingCamera {
  camera_id: string;
  accessory_id: string;
  name: string;
  selected: boolean;
  available: boolean;
  published: boolean;
  paired_known: boolean;
  paired: boolean;
  pairing_revision: number | null;
  pending_reset?: boolean;
  reset_operation_id?: string | null;
  [key: string]: unknown;
}
export interface PairingSetup {
  pin: string;
  setup_uri: string;
  expires_in_ms: number;
}
export interface PairingSecret extends PairingSetup {
  camera_id: string;
  [key: string]: unknown;
}
export interface Configuration {
  version: number;
  revision: number;
  mode: string;
  plugin_host: string;
  plugin_port: number;
  rtsp_host: string;
  rtsp_port: number;
}
export interface ConfigurationView {
  saved: Configuration;
  applied: Configuration;
  restart_required: boolean;
}
export interface Operation {
  operation_id?: string;
  status?: string;
  outcome?: string;
  message?: string;
  [key: string]: unknown;
}
export interface RingChallenge {
  status: 'connected' | 'mfa_required';
  challenge_id?: string;
  expires_at?: number;
}
export type Page =
  'overview' | 'devices' | 'integrations' | 'activity' | 'diagnostics' | 'settings';
