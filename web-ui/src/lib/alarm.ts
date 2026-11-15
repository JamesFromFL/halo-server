import type { AlarmMode, AlarmState, NoncameraDevice, NoncameraState, Operation } from './types.ts';

export const alarmModes: Record<AlarmMode, string> = {
  disarmed: 'Disarmed',
  home: 'Home',
  away: 'Away',
};
const targetReasonLabels: Record<string, string> = {
  'source-unavailable': 'Ring device monitoring is unavailable.',
  'connection-superseded': 'The Ring connection changed. Wait for fresh observations.',
  'panel-offline': 'The alarm panel is offline.',
  'multiple-panels': 'Halo cannot identify one current alarm panel for this location.',
  'control-unavailable': 'Ring alarm controls are currently unavailable.',
  'panel-asset-unsupported': 'This alarm panel connection does not support mode controls.',
  'panel-stale': 'Halo is waiting for fresh alarm panel observations.',
  'outcome-unknown': 'The previous command’s outcome is uncertain. Check the observed state.',
  'transition-active': 'An entry or exit countdown is in progress.',
  'sensors-require-bypass':
    'A sensor reports a fault. Check the sensors in Ring; Halo does not bypass them.',
  'faulted-device-list-unknown':
    'Halo cannot confirm that every required sensor is clear. Check for unavailable or unrecognized sensors in the device list and Ring app.',
  'command-pending': 'Another alarm command is waiting for confirmation.',
  'authorization-denied': 'Ring has denied alarm control for this account.',
  'authorization-unknown': 'Ring account authority has not been verified.',
  'cellular-backup': 'The alarm base station is using cellular backup.',
  'panel-inventory-incomplete': 'Halo is waiting for a complete alarm device inventory.',
  'life-safety-active': 'A smoke or carbon monoxide alarm is active.',
  'mode-unknown': 'The current alarm mode has not been confirmed.',
  'alarm-active': 'An alarm is currently active.',
  'panel-state-unknown': 'The current alarm phase or signal has not been confirmed.',
  'faulted-device-list-invalid': 'Ring supplied fault information that Halo could not validate.',
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
    (alarm.target_reasons === undefined ||
      (alarm.target_reasons !== null &&
        typeof alarm.target_reasons === 'object' &&
        !Array.isArray(alarm.target_reasons) &&
        Object.keys(alarm.target_reasons).length <= 3 &&
        Object.entries(alarm.target_reasons).every(
          ([target, reason]) =>
            Object.hasOwn(alarmModes, target) &&
            !alarm.allowed_targets.includes(target as AlarmMode) &&
            typeof reason === 'string' &&
            Object.hasOwn(targetReasonLabels, reason),
        ))) &&
    text(alarm.phase) &&
    text(alarm.signal) &&
    text(alarm.reason) &&
    ['allowed', 'denied', 'unknown'].includes(alarm.authorization)
  );
}

export function alarmTargetBlockers(alarm: AlarmState): { mode: string; message: string }[] {
  if (!validAlarm(alarm)) return [];
  return Object.entries(alarmModes)
    .filter(([target]) => !alarm.allowed_targets.includes(target as AlarmMode))
    .map(([target, label]) => ({
      mode: label,
      message:
        targetReasonLabels[alarm.target_reasons?.[target as AlarmMode] ?? ''] ??
        'This mode is currently unavailable. Refresh the alarm state for details.',
    }));
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
