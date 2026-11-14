<script lang="ts">
  import { onDestroy } from 'svelte';
  import { api, ApiError, errorMessage, operationId } from '../lib/api.ts';
  import {
    lightCanAcknowledge,
    lightCanSet,
    lightConfirmed,
    lightDurations,
    lightPolicyBody,
    lightSetBody,
    lightStateLabel,
    lightUnsettled,
  } from '../lib/light.ts';
  import type { LightDuration, NoncameraDevice, NoncameraState, Operation } from '../lib/types.ts';
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
  let busy = $state(false),
    authenticating = $state(false),
    error = $state('');
  let operation = $state<Operation | null>(null);
  let policyOperation = $state(false),
    submittedRevision = $state<number | null>(null);
  let draftEnabled = $state(false),
    draftDuration = $state<LightDuration>(60),
    draftRevision = $state(0);
  let observedAuthority = '',
    observedPolicy = '',
    destroyed = false;
  const light = $derived(device.light!);
  const authority = $derived(
    JSON.stringify([
      generation,
      monitoring.scope,
      device.id,
      device.location_id,
      light.provider_generation,
      light.epoch,
    ]),
  );
  const savedPolicy = $derived(
    JSON.stringify([authority, light.policy_revision, light.enabled, light.on_duration_seconds]),
  );
  const dirty = $derived(
    draftEnabled !== light.enabled || draftDuration !== light.on_duration_seconds,
  );
  const policyChanged = $derived(draftRevision !== light.policy_revision);
  const disabled = $derived(!fresh || busy || authenticating || !monitoring.scope.profile_id);
  const unsettled = $derived(lightUnsettled(operation));
  const current = $derived(fresh && monitoring.enabled && device.source_health === 'online');
  const canSet = $derived(
    !disabled && !unsettled && !dirty && !policyChanged && lightCanSet(monitoring, device, fresh),
  );
  const canApply = $derived(
    !disabled && dirty && !policyChanged && (!draftEnabled || (monitoring.enabled && !unsettled)),
  );
  const canAcknowledge = $derived(
    !disabled &&
      !policyOperation &&
      lightCanAcknowledge(monitoring, device, fresh, operation, submittedRevision),
  );

  function resetDraft() {
    draftEnabled = light.enabled;
    draftDuration = light.on_duration_seconds;
    draftRevision = light.policy_revision;
  }
  $effect(() => {
    if (observedAuthority !== authority) {
      observedAuthority = authority;
      observedPolicy = savedPolicy;
      operation = null;
      error = '';
      submittedRevision = null;
      resetDraft();
    } else if (observedPolicy !== savedPolicy) {
      observedPolicy = savedPolicy;
      // Preserve an existing draft across external saves; its CAS becomes stale.
      if (!dirty) resetDraft();
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
    submittedRevision = policyOperation ? null : light.observed_revision;
    try {
      const result = await api.post<Operation>(path, body);
      if (destroyed || authority !== expected) return;
      operation = result;
      if (policyOperation && result.status === 'completed')
        notify('Light control preferences saved.');
      else if (!policyOperation && lightConfirmed(result))
        notify('Ring confirmed the light request.');
      await refresh();
      if (!destroyed && authority === expected && policyOperation && result.status === 'completed')
        resetDraft();
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
  async function applyPolicy() {
    if (!canApply) return;
    const expected = savedPolicy;
    const enabled = draftEnabled,
      duration = draftDuration;
    if (enabled) {
      authenticating = true;
      let approved = false;
      try {
        approved = await reauthenticate();
      } finally {
        authenticating = false;
      }
      if (!approved || destroyed) return;
    }
    if (
      expected !== savedPolicy ||
      !fresh ||
      !canApply ||
      enabled !== draftEnabled ||
      duration !== draftDuration
    ) {
      error = 'Light preferences changed. Review the current settings before trying again.';
      return;
    }
    const id = operationId();
    try {
      await mutate(
        'devices/light/policy',
        lightPolicyBody(monitoring, device, enabled, duration, id),
        id,
      );
    } catch (cause) {
      error = errorMessage(cause);
    }
  }
  async function setLight(target: boolean) {
    if (!canSet) return;
    const id = operationId();
    try {
      await mutate('devices/light/set', lightSetBody(monitoring, device, target, id), id);
    } catch (cause) {
      error = errorMessage(cause);
    }
  }
</script>

<section aria-label="Ring light status and controls">
  <h3>Light</h3>
  <div class="group">
    <Row
      title="Observed light"
      subtitle="Reported by Ring; a request does not replace this observation."
    >
      <span class="status">{current ? lightStateLabel(light.on) : 'Unknown'}</span>
    </Row>
    {#if light.requested_on !== null}
      <Row
        title="Requested action"
        subtitle="This is the requested action, not the observed light state."
      >
        <span
          >{light.requested_on
            ? `On for ${lightDurations[light.on_duration_seconds]}`
            : 'Return to automatic'}</span
        >
      </Row>
    {/if}
    <Row
      title="Ring control authority"
      subtitle="Ring still enforces this account’s permissions for each request."
    >
      <span
        >{current
          ? { allowed: 'Verified', denied: 'Denied', unknown: 'Unknown' }[light.authorization]
          : 'Unknown'}</span
      >
    </Row>
    <Row
      title="Allow light controls"
      subtitle="Off by default. Applies to this light in Halo and HomeKit; saving does not switch the light."
      icon="light"
    >
      <input
        type="checkbox"
        role="switch"
        aria-label="Allow light controls"
        bind:checked={draftEnabled}
        disabled={disabled || policyChanged}
      />
    </Row>
    <Row
      title="On duration"
      subtitle="The saved duration applies to On requests from both Halo and HomeKit."
    >
      <select
        aria-label="Light on duration"
        bind:value={draftDuration}
        disabled={disabled || policyChanged}
      >
        {#each Object.entries(lightDurations) as [seconds, label]}<option value={Number(seconds)}
            >{label}</option
          >{/each}
      </select>
    </Row>
  </div>
  <div class="actions light-actions">
    <button class="primary" disabled={!canApply} onclick={applyPolicy}
      >Apply light preferences</button
    >
    {#if dirty || policyChanged}<button
        {disabled}
        onclick={() => {
          resetDraft();
          error = '';
        }}>Reload saved preferences</button
      >{/if}
  </div>
  {#if policyChanged}<p class="notice">
      Saved light preferences changed. Reload them before making another change.
    </p>
  {:else if dirty}<p class="inline-note">
      Preferences are not saved yet. Apply them before using light controls.
    </p>{/if}
  {#if !current}<p class="notice">
      Current light state is unavailable. Missing or stale observations do not mean the light is
      off.
    </p>
  {:else if light.authorization === 'denied'}<p class="notice">
      Ring has denied light control for this account.
    </p>
  {:else if light.authorization === 'unknown'}<p class="inline-note">
      Ring control authority is not verified. Commands remain unavailable.
    </p>{/if}
  {#if light.pending}<p class="notice" role="status">
      A light request is in progress. Waiting for Ring’s reported state.
    </p>
  {:else if light.outcome_unknown}<p class="notice">
      The previous light request’s outcome is uncertain. Halo will not resend it automatically.
    </p>{/if}
  {#if current && light.reason}<p class="inline-note">
      Control availability: {light.reason.replaceAll('-', ' ').replaceAll('_', ' ')}.
    </p>{/if}
  <div class="actions light-actions" aria-label="Light actions">
    <button disabled={!canSet} onclick={() => setLight(true)}
      >Turn on for {lightDurations[light.on_duration_seconds]}</button
    >
    <button disabled={!canSet} onclick={() => setLight(false)}>Return to automatic</button>
  </div>
  <p class="inline-note">
    Return to automatic releases the manual On setting. Ring motion rules, schedules, or another app
    may turn the light on again. The duration is a request to Ring, not a guaranteed shutoff time.
  </p>
  {#if error}<p class="notice error" role="alert">{error}</p>{/if}
  {#if operation}<OperationStatus
      {operation}
      configuration={policyOperation}
      light={!policyOperation}
      onupdate={(result) => {
        if (result.operation_id !== operation?.operation_id) return;
        operation = result;
        void refresh();
      }}
    />{/if}
  {#if canAcknowledge}<p class="inline-note">
      A newer Ring observation is available. Dismissing the earlier uncertain result does not
      confirm it or send another request.
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
    </div>{/if}
</section>

<style>
  .light-actions {
    margin-top: 16px;
    flex-wrap: wrap;
  }
</style>
