import type {
  ChimeAction,
  ChimeState,
  NoncameraDevice,
  NoncameraState,
  Operation,
} from './types.ts';

const integer = (value: unknown, max = Number.MAX_SAFE_INTEGER): value is number =>
  typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 && value <= max;
export const snoozeDurations: Record<number, string> = {
  15: '15 minutes',
  30: '30 minutes',
  60: '1 hour',
  120: '2 hours',
  240: '4 hours',
  480: '8 hours',
  1440: '24 hours',
};
export function validChime(value: unknown): value is ChimeState {
  if (!value || typeof value !== 'object') return false;
  const chime = value as ChimeState;
  return (
    ['policy_revision', 'provider_generation', 'epoch', 'observed_revision'].every((key) =>
      integer(chime[key as keyof ChimeState]),
    ) &&
    [
      'enabled',
      'sound_tests_enabled',
      'can_set',
      'pending',
      'outcome_unknown',
      'sound_active',
    ].every((key) => typeof chime[key as keyof ChimeState] === 'boolean') &&
    ['allowed', 'denied', 'unknown'].includes(chime.authorization) &&
    (chime.reason === null || (typeof chime.reason === 'string' && chime.reason.length <= 512)) &&
    (chime.requested_action === null ||
      (typeof chime.requested_action === 'string' && chime.requested_action.length <= 128)) &&
    (chime.volume === null || integer(chime.volume, 11)) &&
    (chime.snooze_seconds_left === null || integer(chime.snooze_seconds_left, 2147483647)) &&
    (chime.automatic_nightlight === null || typeof chime.automatic_nightlight === 'boolean')
  );
}
function scopedChime(monitoring: NoncameraState, device: NoncameraDevice) {
  if (
    !monitoring.scope.profile_id ||
    monitoring.scope.account_generation === null ||
    device.kind !== 'chime' ||
    !device.chime ||
    !validChime(device.chime) ||
    !monitoring.devices.some(
      (item) => item.id === device.id && item.location_id === device.location_id,
    )
  )
    throw new Error('Refresh this Chime before continuing.');
  return device.chime;
}
export function chimePolicyBody(
  monitoring: NoncameraState,
  device: NoncameraDevice,
  enabled: boolean,
  sound_tests_enabled: boolean,
  operation_id: string,
) {
  const chime = scopedChime(monitoring, device);
  if (
    typeof enabled !== 'boolean' ||
    typeof sound_tests_enabled !== 'boolean' ||
    (sound_tests_enabled && !enabled)
  )
    throw new Error('Sound tests require Chime controls to be enabled.');
  return {
    profile_id: monitoring.scope.profile_id,
    account_generation: monitoring.scope.account_generation,
    device_id: device.id,
    expected_revision: chime.policy_revision,
    enabled,
    sound_tests_enabled,
    operation_id,
  };
}
export function chimeCanAct(
  monitoring: NoncameraState,
  device: NoncameraDevice,
  fresh: boolean,
  action: ChimeAction,
): boolean {
  const chime = device.chime;
  if (
    !fresh ||
    !monitoring.enabled ||
    device.kind !== 'chime' ||
    device.source_health !== 'online' ||
    !chime ||
    !validChime(chime) ||
    !chime.enabled ||
    chime.authorization !== 'allowed' ||
    chime.pending
  )
    return false;
  // This acknowledgement changes only Halo's local sound lock, including after a lost reply.
  if (action === 'sound_stopped') return chime.sound_active;
  if (!chime.can_set || chime.outcome_unknown || chime.sound_active) return false;
  if (action === 'volume') return chime.volume !== null;
  if (action === 'snooze') return chime.snooze_seconds_left !== null;
  if (action === 'nightlight') return chime.automatic_nightlight !== null;
  return action === 'test' && chime.sound_tests_enabled && chime.volume !== null;
}
export function chimeSetBody(
  monitoring: NoncameraState,
  device: NoncameraDevice,
  action: ChimeAction,
  value: unknown,
  operation_id: string,
) {
  const chime = scopedChime(monitoring, device);
  const valid =
    action === 'volume'
      ? integer(value, 11)
      : action === 'snooze'
        ? integer(value, 1440)
        : action === 'test'
          ? value === 'ding' || value === 'motion'
          : action === 'nightlight'
            ? typeof value === 'boolean'
            : action === 'sound_stopped' && value === null;
  if (!valid || !chimeCanAct(monitoring, device, true, action))
    throw new Error('This Chime action is unavailable. Refresh its current state.');
  return {
    profile_id: monitoring.scope.profile_id,
    account_generation: monitoring.scope.account_generation,
    device_id: device.id,
    provider_generation: chime.provider_generation,
    expected_epoch: chime.epoch,
    expected_revision: chime.observed_revision,
    expected_policy_revision: chime.policy_revision,
    action,
    value,
    operation_id,
  };
}
export const chimeSnoozeLabel = (seconds: number | null | undefined): string =>
  seconds === 0
    ? 'Not snoozed'
    : typeof seconds === 'number' && integer(seconds, 2147483647)
      ? `${seconds} seconds remaining when reported`
      : 'Unknown';
export const chimeUnsettled = (operation: Operation | null): boolean =>
  operation?.status === 'pending' ||
  operation?.status === 'unknown' ||
  (operation?.result as Record<string, unknown> | undefined)?.status === 'uncertain';
export const chimeConfirmed = (operation: Operation): boolean =>
  operation.status === 'completed' &&
  (operation.result as Record<string, unknown> | undefined)?.status === 'confirmed';
export function chimeCanAcknowledge(
  monitoring: NoncameraState,
  device: NoncameraDevice,
  fresh: boolean,
  operation: Operation | null,
  submittedRevision: number | null,
): boolean {
  const chime = device.chime;
  return (
    submittedRevision !== null &&
    chimeUnsettled(operation) &&
    operation?.status !== 'pending' &&
    !!chime &&
    validChime(chime) &&
    chime.observed_revision > submittedRevision &&
    !chime.sound_active &&
    ['volume', 'snooze', 'nightlight'].some((action) =>
      chimeCanAct(monitoring, device, fresh, action as ChimeAction),
    )
  );
}
