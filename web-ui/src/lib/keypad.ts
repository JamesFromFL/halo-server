import type { KeypadState, NoncameraDevice, NoncameraState } from './types.ts';

const revision = (value: unknown) =>
  typeof value === 'number' && Number.isSafeInteger(value) && value >= 0;
export const validBrightness = (value: unknown): value is number =>
  revision(value) && (value as number) <= 100;
export const qualifiedKeypad = (device: NoncameraDevice): boolean =>
  device.kind === 'keypad' &&
  device.provider === 'ring-hub' &&
  device.model === 'security-keypad' &&
  device.family === 'hub' &&
  device.supported === true &&
  device.capabilities?.includes('keypad-brightness') &&
  device.capability_evidence?.includes('ring-hub-device-schema') === true;
export function validKeypad(value: unknown): value is KeypadState {
  if (!value || typeof value !== 'object') return false;
  const state = value as KeypadState;
  return (
    ['policy_revision', 'provider_generation', 'epoch', 'observed_revision'].every((key) =>
      revision(state[key as keyof KeypadState]),
    ) &&
    ['enabled', 'locked', 'can_set', 'can_set_lock', 'pending', 'outcome_unknown'].every(
      (key) => typeof state[key as keyof KeypadState] === 'boolean',
    ) &&
    (state.brightness === null || validBrightness(state.brightness)) &&
    (state.requested_brightness === null || validBrightness(state.requested_brightness)) &&
    validBrightness(state.on_brightness) &&
    state.on_brightness > 0 &&
    ['allowed', 'denied', 'unknown'].includes(state.authorization) &&
    (state.reason === null || (typeof state.reason === 'string' && state.reason.length <= 512)) &&
    !(state.locked && state.can_set)
  );
}
function scoped(monitoring: NoncameraState, device: NoncameraDevice) {
  if (
    !monitoring.scope.profile_id ||
    monitoring.scope.account_generation === null ||
    !qualifiedKeypad(device) ||
    !validKeypad(device.keypad) ||
    !monitoring.devices.some(
      (item) => item.id === device.id && item.location_id === device.location_id,
    )
  )
    throw new Error('Refresh this keypad before continuing.');
  return device.keypad;
}
export function keypadCanSet(monitoring: NoncameraState, device: NoncameraDevice, fresh: boolean) {
  const state = device.keypad;
  return (
    fresh &&
    monitoring.enabled &&
    qualifiedKeypad(device) &&
    device.source_health === 'online' &&
    !!state &&
    validKeypad(state) &&
    state.enabled &&
    !state.locked &&
    state.brightness !== null &&
    state.authorization === 'allowed' &&
    state.can_set &&
    !state.pending &&
    !state.outcome_unknown
  );
}
export function keypadPolicyBody(
  monitoring: NoncameraState,
  device: NoncameraDevice,
  enabled: boolean,
  locked: boolean,
  operation_id: string,
) {
  const state = scoped(monitoring, device);
  if (typeof enabled !== 'boolean' || typeof locked !== 'boolean')
    throw new Error('Choose valid keypad preferences.');
  return {
    ...monitoring.scope,
    device_id: device.id,
    expected_revision: state.policy_revision,
    enabled,
    locked,
    operation_id,
  };
}
export function keypadLockBody(
  monitoring: NoncameraState,
  device: NoncameraDevice,
  locked: boolean,
  operation_id: string,
) {
  const state = scoped(monitoring, device);
  if (!state.enabled || !state.can_set_lock || typeof locked !== 'boolean')
    throw new Error('Keypad lock controls are unavailable.');
  return {
    ...monitoring.scope,
    device_id: device.id,
    expected_policy_revision: state.policy_revision,
    locked,
    operation_id,
  };
}
export function keypadBrightnessBody(
  monitoring: NoncameraState,
  device: NoncameraDevice,
  target: number,
  operation_id: string,
) {
  const state = scoped(monitoring, device);
  if (!validBrightness(target) || !keypadCanSet(monitoring, device, true))
    throw new Error('Unlock brightness and refresh this keypad before adjusting it.');
  return {
    ...monitoring.scope,
    device_id: device.id,
    provider_generation: state.provider_generation,
    expected_epoch: state.epoch,
    expected_revision: state.observed_revision,
    expected_policy_revision: state.policy_revision,
    target,
    operation_id,
  };
}
