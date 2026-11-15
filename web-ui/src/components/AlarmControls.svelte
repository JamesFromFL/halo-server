<script lang="ts">
  import { onDestroy } from 'svelte';
  import { api, ApiError, errorMessage, operationId } from '../lib/api.ts';
  import {
    alarmCanCommand,
    alarmCanAcknowledge,
    alarmConfirmed,
    alarmModeBody,
    alarmModeLabel,
    alarmModes,
    alarmPhaseLabel,
    alarmPolicyBody,
    alarmSignalLabel,
    alarmTargetBlockers,
    alarmUnsettled,
  } from '../lib/alarm.ts';
  import type { AlarmMode, NoncameraDevice, NoncameraState, Operation } from '../lib/types.ts';
  import Confirm from './Confirm.svelte';
  import OperationStatus from './OperationStatus.svelte';
  import Row from './Row.svelte';

  let {
    monitoring,
    device,
    fresh,
    generation,
    reauthenticate,
    notify,
    refresh,
  }: {
    monitoring: NoncameraState;
    device: NoncameraDevice;
    fresh: boolean;
    generation: string;
    reauthenticate: () => Promise<boolean>;
    notify: (message: string) => void;
    refresh: () => Promise<void>;
  } = $props();
  let busy = $state(false);
  let authenticating = $state(false);
  let error = $state('');
  let operation = $state<Operation | null>(null);
  let policyOperation = $state(false);
  let submittedRevision = $state<number | null>(null);
  let confirmation = $state<'enable' | AlarmMode | null>(null);
  let observedAuthority = '';
  let observedDecision = '';
  let destroyed = false;
  const alarm = $derived(device.alarm!);
  const authority = $derived(
    JSON.stringify([
      generation,
      monitoring.scope,
      device.id,
      device.location_id,
      alarm.provider_generation,
      alarm.epoch,
    ]),
  );
  const decision = $derived(
    JSON.stringify([
      authority,
      alarm.policy_revision,
      alarm.enabled,
      alarm.mode,
      alarm.phase,
      alarm.signal,
      alarm.authorization,
      alarm.can_set_mode,
      alarm.allowed_targets,
      alarm.target_reasons,
      alarm.pending,
      fresh,
      device.source_health,
    ]),
  );
  const current = $derived(fresh && monitoring.enabled && device.source_health === 'online');
  const unsettled = $derived(alarmUnsettled(operation));
  const disabled = $derived(!fresh || busy || authenticating || !monitoring.scope.profile_id);
  const canCommand = $derived(
    !disabled && !unsettled && alarmCanCommand(monitoring, device, fresh),
  );
  const canAcknowledge = $derived(
    !disabled &&
      !policyOperation &&
      alarmCanAcknowledge(monitoring, device, fresh, operation, submittedRevision),
  );

  $effect(() => {
    if (observedAuthority !== authority) {
      observedAuthority = authority;
      operation = null;
      submittedRevision = null;
      error = '';
      confirmation = null;
    }
  });
  $effect(() => {
    if (observedDecision !== decision) {
      observedDecision = decision;
      confirmation = null;
    }
  });
  onDestroy(() => {
    destroyed = true;
  });

  async function mutate(path: string, body: Record<string, unknown>, id: string) {
    const expected = authority;
    busy = true;
    error = '';
    policyOperation = path.endsWith('/policy');
    submittedRevision = policyOperation ? null : alarm.observed_revision;
    try {
      const result = await api.post<Operation>(path, body);
      if (destroyed || authority !== expected) return;
      operation = result;
      if (policyOperation && result.status === 'completed')
        notify('Alarm control preference saved.');
      else if (!policyOperation && alarmConfirmed(result))
        notify('Ring confirmed the alarm mode change.');
      await refresh();
    } catch (cause) {
      if (destroyed || authority !== expected) return;
      error = errorMessage(cause);
      if (cause instanceof ApiError && cause.possiblySent)
        operation = { operation_id: id, status: 'unknown', possibly_sent: true };
      await refresh();
    } finally {
      busy = false;
    }
  }

  async function disablePolicy() {
    if (disabled || !alarm.enabled) return;
    const id = operationId();
    try {
      await mutate('devices/alarm/policy', alarmPolicyBody(monitoring, device, false, id), id);
    } catch (cause) {
      error = errorMessage(cause);
    }
  }

  async function confirmAction() {
    const action = confirmation;
    confirmation = null;
    if (!action || disabled || unsettled || (action !== 'enable' && !canCommand)) return;
    const expected = decision;
    authenticating = true;
    let approved = false;
    try {
      approved = await reauthenticate();
    } finally {
      authenticating = false;
    }
    if (!approved || destroyed) return;
    if (expected !== decision || !fresh) {
      error = 'Alarm state changed. Review its current state before trying again.';
      return;
    }
    const id = operationId();
    try {
      if (action === 'enable')
        await mutate('devices/alarm/policy', alarmPolicyBody(monitoring, device, true, id), id);
      else await mutate('devices/alarm/mode', alarmModeBody(monitoring, device, action, id), id);
    } catch (cause) {
      error = errorMessage(cause);
    }
  }
</script>

<section aria-label="Ring Alarm status and controls">
  <h3>Ring Alarm</h3>
  <div class="group">
    <Row
      title="Observed alarm mode"
      subtitle="Reported by Ring; command acceptance does not change this value."
    >
      <span class="status">{current ? alarmModeLabel(alarm.mode) : 'Unknown'}</span>
    </Row>
    <Row
      title="Alarm phase"
      subtitle="Entry and exit countdowns are temporary phases. A countdown is not a triggered alarm."
      ><span>{current ? alarmPhaseLabel(alarm.phase) : 'Unknown'}</span></Row
    >
    <Row title="Alarm signal"
      ><span>{current ? alarmSignalLabel(alarm.signal) : 'Unknown'}</span></Row
    >
    {#if alarm.requested_mode}
      <Row
        title="Requested mode"
        subtitle="Requested target; separate from the observed alarm mode."
        ><span>{alarmModeLabel(alarm.requested_mode)}</span></Row
      >
    {/if}
    <Row
      title="Ring account authority"
      subtitle="Ring still enforces account and subscription permissions for each command."
    >
      <span
        >{current
          ? { allowed: 'Owner role verified', denied: 'Denied', unknown: 'Unknown' }[
              alarm.authorization
            ]
          : 'Unknown'}</span
      >
    </Row>
    <Row
      title="Available modes"
      subtitle="Current Ring observations and Halo’s safety checks determine which modes are available."
    >
      <span
        >{current && alarm.allowed_targets.length
          ? alarm.allowed_targets.map(alarmModeLabel).join(', ')
          : 'Unavailable'}</span
      >
    </Row>
    <Row
      title="Allow normal alarm controls"
      subtitle={`Applies to ${device.location_name || 'this location'}. Disarm, Home, and Away only; off by default.`}
      icon="shield"
    >
      <input
        type="checkbox"
        role="switch"
        aria-label="Allow normal alarm controls"
        checked={alarm.enabled}
        disabled={disabled || (!alarm.enabled && (unsettled || !monitoring.enabled))}
        onchange={(event) => {
          event.currentTarget.checked = alarm.enabled;
          if (alarm.enabled) void disablePolicy();
          else confirmation = 'enable';
        }}
      />
    </Row>
  </div>
  {#if !current}<p class="notice">
      Current alarm state is unavailable. Halo cannot confirm that this location is armed or
      disarmed.
    </p>
  {:else if alarm.authorization === 'denied'}<p class="notice">
      Ring has denied alarm control for this account. Enabling Halo controls does not change Ring’s
      permissions or subscription requirements.
    </p>
  {:else if alarm.authorization === 'unknown'}<p class="inline-note">
      Ring account authority is not verified. Commands stay unavailable until Halo has explicit
      authority evidence.
    </p>{/if}
  {#if current && (alarm.phase === null || alarm.signal === null)}<p class="notice">
      Ring’s alarm phase or signal has not been confirmed. A current mode alone does not confirm
      that the alarm is clear; mode controls stay unavailable until all required observations are
      current.
    </p>{/if}
  {#if alarm.pending}<p class="notice" role="status">
      An alarm command is in progress for this location. Waiting for Ring’s reported state.
    </p>
  {:else if alarm.outcome_unknown}<p class="notice">
      The previous alarm command’s outcome is uncertain. Check the observed state before making
      another change; Halo will not resend it automatically.
    </p>{/if}
  {#if current && alarm.reason}<p class="inline-note">
      Control availability: {alarm.reason.replaceAll('-', ' ').replaceAll('_', ' ')}.
    </p>{/if}
  {#if current && alarm.enabled && !alarm.pending && !alarm.outcome_unknown && !alarm.reason}
    {#each alarmTargetBlockers(alarm) as blocker}
      <p class="inline-note"><strong>{blocker.mode} unavailable:</strong> {blocker.message}</p>
    {/each}
  {/if}
  <div class="actions alarm-actions" aria-label="Normal alarm modes">
    {#each Object.entries(alarmModes) as [target, label]}
      <button
        disabled={!canCommand || !alarm.allowed_targets.includes(target as AlarmMode)}
        onclick={() => (confirmation = target as AlarmMode)}
        >{target === 'disarmed' ? 'Disarm' : `Arm ${label}`}</button
      >
    {/each}
  </div>
  <div class="group">
    <Row
      title="HomeKit alarm alerts"
      subtitle="Confirmed alarms are sent immediately through Halo Bridge, even when normal alarm controls are disabled. Pair the bridge and enable notifications for the security system in Apple Home."
      icon="bell"><span>Observed alarms only</span></Row
    >
  </div>
  <p class="inline-note">
    Apple Home and your device settings control critical alerts, sound, and Focus behavior. Halo
    cannot enable those permissions or confirm delivery to your phone. Entry and exit countdowns do
    not send a triggered-alarm event.
  </p>
  <p class="inline-note">
    Each mode change from this page requires confirmation and your Halo password. Panic, emergency
    dispatch, sensor bypass, and Night mode are unavailable.
  </p>
  {#if error}<p class="notice error" role="alert">{error}</p>{/if}
  {#if operation}<OperationStatus
      {operation}
      configuration={policyOperation}
      alarm={!policyOperation}
      onupdate={(result) => {
        if (result.operation_id !== operation?.operation_id) return;
        operation = result;
        void refresh();
      }}
    />{/if}
  {#if canAcknowledge}
    <p class="inline-note">
      A newer Ring observation is available. Dismissing this earlier uncertain result does not
      confirm the earlier command or send another one.
    </p>
    <div class="actions">
      <button
        onclick={() => {
          if (!canAcknowledge) return;
          operation = null;
          error = '';
          submittedRevision = null;
        }}>Use current observed state</button
      >
    </div>
  {/if}
</section>

{#if confirmation}<Confirm
    title={confirmation === 'enable'
      ? 'Allow alarm controls for this location?'
      : confirmation === 'disarmed'
        ? 'Disarm this location?'
        : `Arm this location in ${alarmModes[confirmation]} mode?`}
    onclose={() => (confirmation = null)}
  >
    {#if confirmation === 'enable'}
      <p>
        This allows normal alarm mode changes for {device.location_name || 'this location'} through Halo,
        including its HomeKit security accessory when connected.
      </p>
      <p>
        Ring must still permit each command. Panic, emergency dispatch, and sensor bypass remain
        blocked. This preference does not arm or disarm the alarm.
      </p>
    {:else}
      <p>
        This sends a real alarm command to {device.location_name || 'this location'}. Ring’s
        reported state will confirm the result.
      </p>
      <p>
        Halo will not bypass open or faulted sensors. A rejected or uncertain command will not be
        retried automatically.
      </p>
    {/if}
    <div class="actions end">
      <button onclick={() => (confirmation = null)}>Cancel</button><button
        class={confirmation === 'disarmed' ? 'danger' : 'primary'}
        onclick={confirmAction}
        >{confirmation === 'enable' ? 'Allow controls' : 'Confirm mode change'}</button
      >
    </div>
  </Confirm>{/if}

<style>
  .alarm-actions {
    margin-top: 16px;
    flex-wrap: wrap;
  }
</style>
