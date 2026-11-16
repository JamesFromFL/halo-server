import type { CameraPrivacy, CameraPrivacyMode, CameraPrivacyPage, Device } from './types.ts';

const revision = (value: unknown) =>
  typeof value === 'number' && Number.isSafeInteger(value) && value >= 0;
const tri = (value: unknown) => value === null || typeof value === 'boolean';
export function validPrivacyPage(value: unknown): value is CameraPrivacyPage {
  if (!value || typeof value !== 'object') return false;
  const page = value as CameraPrivacyPage;
  return (
    typeof page.available === 'boolean' &&
    ((typeof page.profile_id === 'string' &&
      page.profile_id.length > 0 &&
      revision(page.account_generation)) ||
      (!page.available && page.profile_id === null && page.account_generation === null)) &&
    Array.isArray(page.cameras) &&
    page.cameras.length <= 256 &&
    new Set(page.cameras.map((camera) => camera?.camera_id)).size === page.cameras.length &&
    page.cameras.every(
      (camera) =>
        camera &&
        typeof camera.camera_id === 'string' &&
        camera.camera_id.length > 0 &&
        revision(camera.revision) &&
        ['ring', 'homekit'].includes(camera.mode) &&
        tri(camera.ring_live_allowed) &&
        tri(camera.ring_recording_allowed) &&
        tri(camera.ring_snapshot_allowed) &&
        tri(camera.ring_audio_allowed) &&
        typeof camera.source_fresh === 'boolean' &&
        typeof camera.effective_live_allowed === 'boolean' &&
        typeof camera.effective_recording_allowed === 'boolean' &&
        typeof camera.effective_snapshot_allowed === 'boolean' &&
        typeof camera.effective_audio_allowed === 'boolean' &&
        (camera.reason === null ||
          (typeof camera.reason === 'string' && camera.reason.length <= 128)),
    )
  );
}

export function cameraPrivacy(page: CameraPrivacyPage | null, device: Device | undefined) {
  if (
    !page ||
    !device ||
    page.profile_id !== device.scope.profile_id ||
    page.account_generation !== device.scope.account_generation
  )
    return undefined;
  return page.cameras.find((camera) => camera.camera_id === device.camera_id);
}

export function privacyBody(page: CameraPrivacyPage, device: Device, mode: CameraPrivacyMode) {
  const camera = validPrivacyPage(page) && cameraPrivacy(page, device);
  if (
    !page.available ||
    !page.profile_id ||
    page.account_generation === null ||
    !camera ||
    !['ring', 'homekit'].includes(mode)
  )
    throw new Error('Refresh this camera’s privacy settings before saving.');
  return {
    profile_id: page.profile_id,
    account_generation: page.account_generation,
    camera_id: camera.camera_id,
    expected_revision: camera.revision,
    mode,
  };
}

export function privacyAccess(fresh: boolean, value: unknown): string {
  return !fresh || typeof value !== 'boolean' ? 'Unknown' : value ? 'Allowed' : 'Blocked';
}

export function privacyReason(reason: unknown): string {
  switch (reason) {
    case null:
      return '';
    case 'ring_live_disabled':
      return 'Ring reports that Live View is disabled for this camera.';
    case 'ring_motion_disabled':
      return 'Ring reports motion recording disabled. Follow Ring privacy blocks background capture and snapshots; manual Live View is checked separately.';
    case 'ring_audio_disabled':
      return 'Ring reports audio recording disabled. Follow Ring privacy blocks camera audio for Live View and recording. For video-only recording, turn off Record Audio in Apple Home.';
    case 'source_unknown':
      return 'Ring has not provided complete privacy information for this camera.';
    case 'source_stale':
      return 'Ring privacy information is out of date. Follow Ring blocks source access until it refreshes.';
    case 'account_unavailable':
      return 'The Ring account is unavailable. Reconnect to check current source access.';
    default:
      return 'Current privacy information is unavailable. Refresh this camera to check again.';
  }
}
