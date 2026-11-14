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
  location_id: string;
  location_name: string;
  capabilities: string[];
  observed: Record<string, Json>;
  available: boolean | null;
  stale: boolean;
  observed_at: number | null;
  supported: boolean;
  unsupported_reason: string | null;
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
    read_only: true;
    panic_enabled: false;
    emergency_dispatch_enabled: false;
  };
  enabled: boolean;
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
export interface PairingSecret {
  camera_id: string;
  pin: string;
  setup_uri: string;
  expires_in_ms: number;
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
