import type {
  LightDuration,
  LightState,
  NoncameraDevice,
  NoncameraState,
  Operation,
} from './types.ts';

export const lightDurations: Record<LightDuration, string> = {
  30: '30 seconds',
  60: '1 minute',
  300: '5 minutes',
  900: '15 minutes',
};
const count = (value: unknown) =>
  typeof value === 'number' && Number.isSafeInteger(value) && value >= 0;
const optionalBoolean = (value: unknown) => value === null || typeof value === 'boolean';
export const validLightDuration = (value: unknown): value is LightDuration =>
  typeof value === 'number' && Object.hasOwn(lightDurations, value);

export function validLight(value: unknown): value is LightState {
  if (!value || typeof value !== 'object') return false;
  const light = value as LightState;
  return (
    ['policy_revision', 'provider_generation', 'epoch', 'observed_revision'].every((key) =>
      count(light[key as keyof LightState]),
    ) &&
    ['enabled', 'can_set', 'pending', 'outcome_unknown'].every(
      (key) => typeof light[key as keyof LightState] === 'boolean',
    ) &&
    validLightDuration(light.on_duration_seconds) &&
    optionalBoolean(light.on) &&
    optionalBoolean(light.requested_on) &&
    ['allowed', 'denied', 'unknown'].includes(light.authorization) &&
    (light.reason === null || (typeof light.reason === 'string' && light.reason.length <= 512))
  );
}

function scopedLight(monitoring: NoncameraState, device: NoncameraDevice) {
  if (
    !monitoring.scope.profile_id ||
    monitoring.scope.account_generation === null ||
    device.kind !== 'beams-light' ||
    !device.light ||
    !validLight(device.light) ||
    !monitoring.devices.some(
      (item) => item.id === device.id && item.location_id === device.location_id,
    )
  )
    throw new Error('Refresh this light before continuing.');
  return device.light;
}

export function lightPolicyBody(
  monitoring: NoncameraState,
  device: NoncameraDevice,
  enabled: boolean,
  duration: LightDuration,
  operation_id: string,
) {
  const light = scopedLight(monitoring, device);
  if (typeof enabled !== 'boolean' || !validLightDuration(duration))
    throw new Error('Choose a supported light duration.');
  return {
    profile_id: monitoring.scope.profile_id,
    account_generation: monitoring.scope.account_generation,
    device_id: device.id,
    expected_revision: light.policy_revision,
    enabled,
    on_duration_seconds: duration,
    operation_id,
  };
}

export function lightCanSet(
  monitoring: NoncameraState,
  device: NoncameraDevice,
  fresh: boolean,
): boolean {
  const light = device.light;
  return (
    fresh &&
    monitoring.enabled &&
    device.kind === 'beams-light' &&
    device.source_health === 'online' &&
    !!light &&
    validLight(light) &&
    light.on !== null &&
    light.enabled &&
    light.authorization === 'allowed' &&
    light.can_set &&
    !light.pending &&
    !light.outcome_unknown
  );
}

export function lightSetBody(
  monitoring: NoncameraState,
  device: NoncameraDevice,
  target: boolean,
  operation_id: string,
) {
  const light = scopedLight(monitoring, device);
  if (typeof target !== 'boolean' || !lightCanSet(monitoring, device, true))
    throw new Error('Light controls are unavailable. Refresh its current state.');
  return {
    profile_id: monitoring.scope.profile_id,
    account_generation: monitoring.scope.account_generation,
    device_id: device.id,
    provider_generation: light.provider_generation,
    expected_epoch: light.epoch,
    expected_revision: light.observed_revision,
    expected_policy_revision: light.policy_revision,
    target,
    operation_id,
  };
}

export const lightStateLabel = (value: boolean | null | undefined) =>
  value === true ? 'On' : value === false ? 'Off' : 'Unknown';
export function lightUnsettled(operation: Operation | null): boolean {
  return (
    operation?.status === 'pending' ||
    operation?.status === 'unknown' ||
    (operation?.result as Record<string, unknown> | undefined)?.status === 'uncertain'
  );
}
export const lightConfirmed = (operation: Operation) =>
  operation.status === 'completed' &&
  (operation.result as Record<string, unknown> | undefined)?.status === 'confirmed';
export function lightCanAcknowledge(
  monitoring: NoncameraState,
  device: NoncameraDevice,
  fresh: boolean,
  operation: Operation | null,
  submittedRevision: number | null,
): boolean {
  return (
    submittedRevision !== null &&
    lightUnsettled(operation) &&
    operation?.status !== 'pending' &&
    lightCanSet(monitoring, device, fresh) &&
    device.light!.observed_revision > submittedRevision
  );
}
