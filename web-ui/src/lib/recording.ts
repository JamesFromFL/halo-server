export function recordingEligibility(
  fresh: boolean,
  selected: boolean,
  available: unknown,
  camera: Record<string, unknown> | undefined,
) {
  if (!fresh)
    return {
      eligible: false,
      label: 'Unknown',
      detail: 'Reconnect to Halo to check this camera’s recording support.',
    };
  if (!selected)
    return {
      eligible: false,
      label: 'Not selected',
      detail: 'Expose this camera in Apple Home before enabling recording support.',
    };
  if (!camera || available !== true)
    return {
      eligible: false,
      label: 'Unavailable',
      detail: 'Recording details are unavailable. Refresh this device before continuing.',
    };
  return camera.recording_supported === true
    ? {
        eligible: true,
        label: 'Supported',
        detail:
          'This camera provides video. Battery power and model name do not restrict recording support.',
      }
    : {
        eligible: false,
        label: 'Video unavailable',
        detail: 'Halo needs a discovered video-capable camera to enable recording support.',
      };
}

export function recordingSources(fresh: boolean, value: unknown): string {
  if (!fresh || !value || typeof value !== 'object') return 'Unavailable';
  const { capacity, reserved } = value as { capacity?: unknown; reserved?: unknown };
  if (typeof reserved !== 'number' || !Number.isInteger(reserved) || reserved < 0)
    return 'Unavailable';
  if (capacity === null) return `${reserved} sources reserved · No configured camera cap`;
  if (typeof capacity === 'number' && Number.isInteger(capacity) && capacity >= 0)
    return `${reserved} of ${capacity} sources reserved`;
  return 'Unavailable';
}

export function recordingReadiness(fresh: boolean, value: unknown): string {
  if (!fresh) return 'Unknown';
  switch (value) {
    case 'disabled':
      return 'Not buffering';
    case 'warming':
      return 'Preparing pre-event buffer';
    case 'ready':
      return 'Pre-event buffer ready';
    case 'capacity':
      return 'Waiting for source capacity';
    case 'cleanup_pending':
      return 'Waiting for source cleanup';
    default:
      return 'Unknown';
  }
}
