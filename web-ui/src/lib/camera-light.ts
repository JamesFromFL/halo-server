import type { CameraControlPage, CameraLighting, Device } from './types.ts';

const tri = (value: unknown) => value === null || typeof value === 'boolean';
export function validCameraLighting(value: unknown): value is CameraLighting {
  if (!value || typeof value !== 'object') return false;
  const lighting = value as CameraLighting;
  return (
    typeof lighting.fresh === 'boolean' &&
    (lighting.observed_age_seconds === null ||
      (typeof lighting.observed_age_seconds === 'number' &&
        Number.isFinite(lighting.observed_age_seconds) &&
        lighting.observed_age_seconds >= 0)) &&
    tri(lighting.light) &&
    (lighting.motion_duration_seconds === null ||
      (Number.isSafeInteger(lighting.motion_duration_seconds) &&
        lighting.motion_duration_seconds >= 30 &&
        lighting.motion_duration_seconds <= 900)) &&
    lighting.manual_timer_status === 'unverified' &&
    lighting.activation_source === null &&
    [null, 'source_unknown', 'source_stale', 'lighting_changed'].includes(lighting.reason)
  );
}

export function cameraControl(page: CameraControlPage | null, device: Device | undefined) {
  if (
    !page ||
    !device ||
    page.profile_id !== device.scope.profile_id ||
    page.account_generation !== device.scope.account_generation ||
    !Array.isArray(page.cameras)
  )
    return undefined;
  const rows = page.cameras.filter((row) => row?.camera_id === device.camera_id);
  const row = rows.length === 1 ? rows[0] : undefined;
  return row?.device_generation === device.scope.device_generation ? row : undefined;
}

/** An invalid or stale extension cannot fall back to a conflicting older state. */
export function cameraLightingView(
  value: unknown,
  fresh: boolean,
  legacyLight: unknown,
  elapsedSeconds = 0,
) {
  const projection = validCameraLighting(value) ? value : null;
  const age =
    projection?.observed_age_seconds !== null &&
    projection?.observed_age_seconds !== undefined &&
    Number.isFinite(elapsedSeconds) &&
    elapsedSeconds >= 0
      ? projection.observed_age_seconds + elapsedSeconds
      : null;
  const expired = age !== null && age > 90;
  const current =
    fresh &&
    projection?.fresh === true &&
    age !== null &&
    !expired &&
    (projection.reason === null || projection.reason === 'source_unknown');
  return {
    light: current
      ? projection.light
      : value === undefined && fresh && typeof legacyLight === 'boolean'
        ? legacyLight
        : null,
    motion_duration_seconds: current ? projection.motion_duration_seconds : null,
    observed_age_seconds: age,
    reason:
      !fresh || expired
        ? 'source_stale'
        : (projection?.reason ?? (projection ? null : 'source_unknown')),
  };
}

export function motionDuration(seconds: number | null): string {
  if (seconds === null) return 'Unknown';
  if (seconds % 60 === 0) return `${seconds / 60} minute${seconds === 60 ? '' : 's'}`;
  return `${seconds} seconds`;
}

export function lightingReason(reason: unknown): string {
  if (reason === null) return '';
  if (reason === 'source_stale')
    return 'Lighting information is out of date. The light state and motion timer are unknown until a fresh Ring report arrives.';
  if (reason === 'lighting_changed')
    return 'A lighting request made the previous report out of date. Waiting for a fresh Ring observation.';
  return 'Ring has not provided all lighting details. Missing values remain unknown.';
}
