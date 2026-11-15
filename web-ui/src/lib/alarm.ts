import type { AlarmMode, AlarmState, NoncameraDevice, NoncameraState, Operation } from './types.ts';

export const alarmModes: Record<AlarmMode, string> = {
  disarmed: 'Disarmed',
  home: 'Home',
  away: 'Away',
};
const count = (value: unknown) =>
  typeof value === 'number' && Number.isSafeInteger(value) && value >= 0;
const mode = (value: unknown) =>
  value === null || (typeof value === 'string' && Object.hasOwn(alarmModes, value));
const text = (value: unknown) =>
  value === null || (typeof value === 'string' && value.length <= 512);

export function validAlarm(value: unknown): value is AlarmState {
  if (!value || typeof value !== 'object') return false;
  const alarm = value as AlarmState;
  return (
    ['policy_revision', 'provider_generation', 'epoch', 'observed_revision'].every((key) =>
      count(alarm[key as keyof AlarmState]),
    ) &&
    ['enabled', 'can_set_mode', 'pending', 'outcome_unknown'].every(
      (key) => typeof alarm[key as keyof AlarmState] === 'boolean',
    ) &&
    mode(alarm.mode) &&
    mode(alarm.requested_mode) &&
    Array.isArray(alarm.allowed_targets) &&
    alarm.allowed_targets.length <= 3 &&
    new Set(alarm.allowed_targets).size === alarm.allowed_targets.length &&
    alarm.allowed_targets.every((target) => target !== null && mode(target)) &&
    text(alarm.phase) &&
    text(alarm.signal) &&
    text(alarm.reason) &&
    ['allowed', 'denied', 'unknown'].includes(alarm.authorization)
  );
}

function scopedAlarm(monitoring: NoncameraState, device: NoncameraDevice) {
  if (
    !monitoring.scope.profile_id ||
    monitoring.scope.account_generation === null ||
    device.kind !== 'security-panel' ||
    !device.location_id ||
    !device.alarm ||
    !validAlarm(device.alarm) ||
    !monitoring.devices.some(
      (item) => item.id === device.id && item.location_id === device.location_id,
    )
  )
    throw new Error('Refresh this alarm before continuing.');
  return device.alarm;
}

export function alarmPolicyBody(
  monitoring: NoncameraState,
  device: NoncameraDevice,
  enabled: boolean,
  operation_id: string,
) {
  const alarm = scopedAlarm(monitoring, device);
  return {
    profile_id: monitoring.scope.profile_id,
    account_generation: monitoring.scope.account_generation,
    location_id: device.location_id,
    expected_revision: alarm.policy_revision,
    enabled,
    operation_id,
  };
}

export function alarmCanCommand(
  monitoring: NoncameraState,
  device: NoncameraDevice,
  fresh: boolean,
): boolean {
  const alarm = device.alarm;
  return (
    fresh &&
    monitoring.enabled &&
    device.kind === 'security-panel' &&
    device.source_health === 'online' &&
    !!alarm &&
    validAlarm(alarm) &&
    alarm.mode !== null &&
    alarm.enabled &&
    alarm.authorization === 'allowed' &&
    alarm.can_set_mode &&
    alarm.allowed_targets.length > 0 &&
    !alarm.pending &&
    !alarm.outcome_unknown
  );
}

export function alarmModeBody(
  monitoring: NoncameraState,
  device: NoncameraDevice,
  target: AlarmMode,
  operation_id: string,
) {
  const alarm = scopedAlarm(monitoring, device);
  if (
    !Object.hasOwn(alarmModes, target) ||
    !alarmCanCommand(monitoring, device, true) ||
    !alarm.allowed_targets.includes(target)
  )
    throw new Error('Alarm controls are unavailable. Refresh its current state.');
  return {
    profile_id: monitoring.scope.profile_id,
    account_generation: monitoring.scope.account_generation,
    device_id: device.id,
    provider_generation: alarm.provider_generation,
    expected_epoch: alarm.epoch,
    expected_revision: alarm.observed_revision,
    expected_policy_revision: alarm.policy_revision,
    target,
    operation_id,
  };
}

export function alarmModeLabel(value: AlarmMode | null | undefined): string {
  return value && Object.hasOwn(alarmModes, value) ? alarmModes[value] : 'Unknown';
}

export function alarmPhaseLabel(value: string | null | undefined): string {
  return (
    (
      {
        idle: 'Stable',
        'exit-delay': 'Exit countdown (arming)',
        'entry-delay': 'Entry countdown',
        alarming: 'Alarm triggered',
      } as Record<string, string>
    )[value ?? ''] ?? 'Unknown'
  );
}

export function alarmUnsettled(operation: Operation | null): boolean {
  const result = operation?.result as Record<string, unknown> | undefined;
  return (
    operation?.status === 'pending' ||
    operation?.status === 'unknown' ||
    result?.status === 'uncertain'
  );
}

export function alarmCanAcknowledge(
  monitoring: NoncameraState,
  device: NoncameraDevice,
  fresh: boolean,
  operation: Operation | null,
  submittedRevision: number | null,
): boolean {
  return (
    submittedRevision !== null &&
    alarmUnsettled(operation) &&
    operation?.status !== 'pending' &&
    alarmCanCommand(monitoring, device, fresh) &&
    !device.alarm!.outcome_unknown &&
    device.alarm!.observed_revision > submittedRevision
  );
}

export function alarmSignalLabel(value: string | null | undefined): string {
  return (
    (
      {
        none: 'None reported',
        burglar: 'Burglar',
        fire: 'Fire',
        'carbon-monoxide': 'Carbon monoxide',
        'fire-or-carbon-monoxide': 'Fire or carbon monoxide',
        panic: 'Panic',
      } as Record<string, string>
    )[value ?? ''] ?? 'Unknown'
  );
}

export function alarmConfirmed(operation: Operation): boolean {
  return (
    operation.status === 'completed' &&
    (operation.result as Record<string, unknown> | undefined)?.status === 'confirmed'
  );
}
