import type {
  CameraPage,
  Configuration,
  ConfigurationView,
  Device,
  PairingCamera,
  StateEnvelope,
} from './types.ts';
import { validNoncamera } from './noncamera.ts';
export const tri = (value: boolean | null | undefined, yes = 'Yes', no = 'No') =>
  value == null ? 'Unknown' : value ? yes : no;
export function pretty(value: unknown): string {
  if (value == null) return 'Unknown';
  if (typeof value === 'string') return value.replaceAll('_', ' ').replaceAll('-', ' ');
  if (typeof value === 'boolean') return value ? 'Yes' : 'No';
  return typeof value === 'number' ? String(value) : 'Details available';
}
export function authority(state: StateEnvelope | null): string {
  return JSON.stringify([
    state?.web.boot_id,
    state?.state?.epoch,
    state?.state?.scope,
    state?.state?.homekit.adapter_boot_id,
    state?.state?.homekit.registry_revision,
  ]);
}
export function isFresh(
  state: StateEnvelope | null,
  connected: boolean,
  now = Date.now(),
): boolean {
  return (
    !!state?.daemon.available &&
    !state.daemon.stale &&
    connected &&
    now - state.web.observed_at * 1000 < 15_000
  );
}
export function validState(value: unknown): value is StateEnvelope {
  if (!value || typeof value !== 'object') return false;
  const v = value as StateEnvelope;
  if (
    v.schema !== 1 ||
    !v.web ||
    typeof v.web.boot_id !== 'string' ||
    !Number.isFinite(v.web.observed_at) ||
    !v.daemon ||
    typeof v.daemon.available !== 'boolean' ||
    typeof v.daemon.stale !== 'boolean'
  )
    return false;
  if (v.state === null) return true;
  const state = v.state;
  if (
    !state ||
    state.schema !== 1 ||
    typeof state.epoch !== 'string' ||
    !Number.isFinite(state.observed_at) ||
    !state.daemon ||
    !state.daemon.release ||
    !state.ring ||
    typeof state.ring.authenticated !== 'boolean' ||
    !state.scope ||
    !state.homekit ||
    typeof state.homekit.state !== 'string' ||
    !Array.isArray(state.devices) ||
    state.devices.length > 256 ||
    (state.noncamera !== undefined &&
      (!validNoncamera(state.noncamera) ||
        (state.scope.profile_id !== null &&
          (state.noncamera.scope.profile_id !== state.scope.profile_id ||
            state.noncamera.scope.account_generation !== state.scope.account_generation)))) ||
    !state.activity ||
    !Array.isArray(state.activity.entries) ||
    state.activity.entries.length > 256
  )
    return false;
  return (
    state.devices.every(
      (d) =>
        d &&
        typeof d.id === 'string' &&
        typeof d.camera_id === 'string' &&
        typeof d.name === 'string' &&
        typeof d.model === 'string' &&
        !!d.scope &&
        !!d.observed &&
        !!d.homekit &&
        typeof d.homekit.selected === 'boolean' &&
        Array.isArray(d.capabilities),
    ) &&
    state.activity.entries.every(
      (e) =>
        e &&
        Number.isFinite(e.sequence) &&
        Number.isFinite(e.at) &&
        typeof e.action === 'string' &&
        typeof e.category === 'string' &&
        typeof e.outcome === 'string',
    )
  );
}
export interface PairingRequestScope {
  generation: string;
  deviceId: string;
  serial: number;
}
export function samePairingScope(
  request: PairingRequestScope,
  current: PairingRequestScope,
): boolean {
  return (
    request.generation === current.generation &&
    request.deviceId === current.deviceId &&
    request.serial === current.serial
  );
}
export function canResumeReset(
  camera: PairingCamera | undefined,
  operationId: string | undefined,
): boolean {
  return (
    camera?.pending_reset === true &&
    !!camera.reset_operation_id &&
    camera.reset_operation_id === operationId
  );
}
export function pairingIdentity(page: CameraPage<PairingCamera>, row: PairingCamera) {
  if (!page.boot_id || row.pairing_revision == null || !row.selected)
    throw new Error('Pairing information changed. Refresh the pairing panel.');
  return {
    camera_id: row.camera_id,
    profile_id: page.profile_id,
    account_generation: page.account_generation,
    registry_revision: page.registry_revision,
    pairing_revision: row.pairing_revision,
    boot_id: page.boot_id,
  };
}
export function selectionBody(
  device: Device,
  revision: number,
  enabled: boolean,
  operation_id: string,
) {
  return {
    camera_id: device.camera_id,
    enabled,
    profile_id: device.scope.profile_id,
    account_generation: device.scope.account_generation,
    expected_revision: revision,
    operation_id,
  };
}
export function configurationBody(
  view: ConfigurationView,
  draft: Configuration,
  operation_id: string,
) {
  return {
    expected_revision: view.saved.revision,
    operation_id,
    configuration: {
      mode: draft.mode,
      plugin_host: draft.plugin_host,
      plugin_port: Number(draft.plugin_port),
      rtsp_host: draft.rtsp_host,
      rtsp_port: Number(draft.rtsp_port),
    },
  };
}
export function when(at: number | null | undefined): string {
  return at ? new Date(at * 1000).toLocaleString() : 'Not observed';
}
export function readAppearance(): 'system' | 'light' | 'dark' {
  try {
    const v = localStorage.getItem('halo-appearance');
    return v === 'light' || v === 'dark' ? v : 'system';
  } catch {
    return 'system';
  }
}
export function saveAppearance(value: string): void {
  try {
    localStorage.setItem('halo-appearance', value);
  } catch {
    /* Appearance is optional; authentication never uses browser storage. */
  }
}
