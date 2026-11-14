<script lang="ts">
  import { onDestroy } from 'svelte';
  import { api, ApiError, errorMessage, operationId } from '../lib/api.ts';
  import {
    chimeCanAct,
    chimeCanAcknowledge,
    chimeConfirmed,
    chimePolicyBody,
    chimeSetBody,
    chimeSnoozeLabel,
    chimeUnsettled,
    snoozeDurations,
  } from '../lib/chime.ts';
  import type { ChimeAction, NoncameraDevice, NoncameraState, Operation } from '../lib/types.ts';
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
  let busy = $state(false),
    authenticating = $state(false),
    error = $state('');
  let operation = $state<Operation | null>(null),
    policyOperation = $state(false);
  let submittedRevision = $state<number | null>(null);
  let confirmation = $state<'ding' | 'motion' | null>(null);
  let draftEnabled = $state(false),
    draftSounds = $state(false),
    draftPolicyRevision = $state(0);
  let draftVolume = $state<number | ''>(''),
    draftVolumeRevision = $state(0);
  let snoozeMinutes = $state(60);
  let observedAuthority = '',
    observedPolicy = '',
    observedDecision = '',
    destroyed = false;
  const chime = $derived(device.chime!);
  const authority = $derived(
    JSON.stringify([
      generation,
      monitoring.scope,
      device.id,
      device.location_id,
      chime.provider_generation,
      chime.epoch,
    ]),
  );
  const savedPolicy = $derived(
    JSON.stringify([authority, chime.policy_revision, chime.enabled, chime.sound_tests_enabled]),
  );
  const decision = $derived(
    JSON.stringify([
      authority,
      chime.observed_revision,
      savedPolicy,
      chime.can_set,
      chime.authorization,
      chime.pending,
      chime.outcome_unknown,
      chime.sound_active,
      fresh,
      device.source_health,
    ]),
  );
  const current = $derived(fresh && monitoring.enabled && device.source_health === 'online');
  const disabled = $derived(!fresh || busy || authenticating || !monitoring.scope.profile_id);
  const unsettled = $derived(chimeUnsettled(operation));
  const dirty = $derived(
    draftEnabled !== chime.enabled || draftSounds !== chime.sound_tests_enabled,
  );
  const policyChanged = $derived(draftPolicyRevision !== chime.policy_revision);
  const canApply = $derived(
    !disabled && dirty && !policyChanged && (!draftEnabled || (monitoring.enabled && !unsettled)),
  );
  const canAcknowledge = $derived(
    !disabled &&
      !policyOperation &&
      chimeCanAcknowledge(monitoring, device, fresh, operation, submittedRevision),
  );
  const volumeChanged = $derived(draftVolumeRevision !== chime.observed_revision);

  function resetPolicy() {
    draftEnabled = chime.enabled;
    draftSounds = chime.sound_tests_enabled;
    draftPolicyRevision = chime.policy_revision;
  }
  function resetVolume() {
    draftVolume = chime.volume ?? '';
    draftVolumeRevision = chime.observed_revision;
  }
  function canAct(action: ChimeAction) {
    return (
      !disabled &&
      !dirty &&
      !policyChanged &&
      (action === 'sound_stopped' || !unsettled) &&
      chimeCanAct(monitoring, device, fresh, action)
    );
  }
  $effect(() => {
    if (observedAuthority !== authority) {
      observedAuthority = authority;
      observedPolicy = savedPolicy;
      operation = null;
      submittedRevision = null;
      confirmation = null;
      error = '';
      resetPolicy();
      resetVolume();
    } else if (observedPolicy !== savedPolicy) {
      observedPolicy = savedPolicy;
      if (!dirty) resetPolicy();
    }
  });
  $effect(() => {
    if (observedDecision !== decision) {
      observedDecision = decision;
      confirmation = null;
      if (draftVolume === chime.volume || draftVolume === '') resetVolume();
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
    submittedRevision = policyOperation ? null : chime.observed_revision;
    try {
      const result = await api.post<Operation>(path, body);
      if (destroyed || authority !== expected) return;
      operation = result;
      if (policyOperation && result.status === 'completed')
        notify('Chime control preferences saved.');
      else if (!policyOperation && chimeConfirmed(result))
        notify(
          body.action === 'sound_stopped'
            ? 'Sound acknowledgement saved.'
            : 'Ring reported the requested Chime setting.',
        );
      await refresh();
      if (!destroyed && authority === expected && policyOperation && result.status === 'completed')
        resetPolicy();
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
    const expected = savedPolicy,
      enabled = draftEnabled,
      sounds = draftSounds;
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
      !canApply ||
      enabled !== draftEnabled ||
      sounds !== draftSounds
    ) {
      error = 'Chime preferences changed. Review them before trying again.';
      return;
    }
    const id = operationId();
    try {
      await mutate(
        'devices/chime/policy',
        chimePolicyBody(monitoring, device, enabled, sounds, id),
        id,
      );
    } catch (cause) {
      error = errorMessage(cause);
    }
  }
  async function command(action: ChimeAction, value: unknown) {
    if (!canAct(action)) return;
    const id = operationId();
    try {
      await mutate('devices/chime/set', chimeSetBody(monitoring, device, action, value, id), id);
    } catch (cause) {
      error = errorMessage(cause);
    }
  }
  async function confirmSound() {
    const kind = confirmation;
    confirmation = null;
    if (!kind || !canAct('test')) return;
    const expected = decision;
    authenticating = true;
    let approved = false;
    try {
      approved = await reauthenticate();
    } finally {
      authenticating = false;
    }
    if (!approved || destroyed) return;
    if (decision !== expected || !canAct('test')) {
      error = 'Chime state changed. Review it before starting a sound test.';
      return;
    }
    await command('test', kind);
  }
</script>

<section aria-label="Chime status and controls">
  <h3>Chime</h3>
  <div class="group">
    <Row
      title="Observed volume"
      subtitle="Reported by Ring on its 0–11 scale. Missing volume does not mean muted."
      ><span>{current && chime.volume !== null ? `${chime.volume} / 11` : 'Unknown'}</span></Row
    >
    <Row title="Snooze" subtitle="Latest Ring report; Halo does not infer when a snooze has ended."
      ><span>{current ? chimeSnoozeLabel(chime.snooze_seconds_left) : 'Unknown'}</span></Row
    >
    <Row
      title="Automatic nightlight"
      subtitle="Whether Ring allows the light sensor to control the nightlight; this is not measured illumination."
      ><span
        >{current && chime.automatic_nightlight !== null
          ? chime.automatic_nightlight
            ? 'Enabled'
            : 'Disabled'
          : 'Unknown'}</span
      ></Row
    >
    <Row
      title="Ring control authority"
      subtitle="Ring still enforces this account’s permissions for each request."
      ><span
        >{current
          ? { allowed: 'Owner role verified', denied: 'Denied', unknown: 'Unknown' }[
              chime.authorization
            ]
          : 'Unknown'}</span
      ></Row
    >
    <Row
      title="Allow Chime controls"
      subtitle="Off by default. Enables supported settings for this Chime; saving does not change the device."
      icon="bell"
    >
      <input
        type="checkbox"
        role="switch"
        aria-label="Allow Chime controls"
        checked={draftEnabled}
        disabled={disabled || policyChanged}
        onchange={(event) => {
          draftEnabled = event.currentTarget.checked;
          if (!draftEnabled) draftSounds = false;
        }}
      />
    </Row>
    <Row
      title="Allow sound tests"
      subtitle="Separate permission for audible Ring and Motion tests. Each test requires confirmation and your password."
    >
      <input
        type="checkbox"
        role="switch"
        aria-label="Allow Chime sound tests"
        bind:checked={draftSounds}
        disabled={disabled || policyChanged || !draftEnabled}
      />
    </Row>
  </div>
  <div class="actions chime-actions">
    <button class="primary" disabled={!canApply} onclick={applyPolicy}
      >Apply Chime preferences</button
    >
    {#if dirty || policyChanged}<button
        {disabled}
        onclick={() => {
          resetPolicy();
          error = '';
        }}>Reload saved preferences</button
      >{/if}
  </div>
  {#if policyChanged}<p class="notice">
      Saved Chime preferences changed. Reload them before making another change.
    </p>
  {:else if dirty}<p class="inline-note">
      Preferences are not saved yet. Apply them before using Chime controls.
    </p>{/if}
  {#if !current}<p class="notice">
      Current Chime state is unavailable. Missing observations do not mean muted, unsnoozed, or
      switched off.
    </p>
  {:else if chime.authorization !== 'allowed'}<p class="notice">
      {chime.authorization === 'denied'
        ? 'Ring has denied Chime control for this account.'
        : 'Ring account authority is not verified. Commands remain unavailable.'}
    </p>{/if}
  {#if chime.pending}<p class="notice" role="status">A Chime request is in progress.</p>
  {:else if chime.outcome_unknown}<p class="notice">
      The previous request’s outcome is uncertain. Halo will not send it again automatically.
    </p>{/if}
  {#if chime.requested_action}<p class="inline-note">
      Requested action: {chime.requested_action.replaceAll('_', ' ').replaceAll('-', ' ')}. This
      does not replace the observed state.
    </p>{/if}
  {#if current && chime.reason}<p class="inline-note">
      Control availability: {chime.reason.replaceAll('_', ' ').replaceAll('-', ' ')}.
    </p>{/if}
  <h3>Settings</h3>
  <div class="group">
    <Row
      title="Volume"
      subtitle="Choose a level, then Apply volume. Moving the control makes no request."
    >
      <select
        aria-label="Chime volume"
        bind:value={draftVolume}
        disabled={!canAct('volume') || volumeChanged}
      >
        <option value="" disabled>Unknown</option>
        {#each Array.from({ length: 12 }, (_, index) => index) as volume}<option value={volume}
            >{volume}</option
          >{/each}
      </select>
    </Row>
  </div>
  <div class="actions chime-actions">
    <button
      disabled={!canAct('volume') ||
        volumeChanged ||
        draftVolume === '' ||
        draftVolume === chime.volume}
      onclick={() => command('volume', draftVolume)}>Apply volume</button
    >
    {#if volumeChanged}<button {disabled} onclick={resetVolume}>Use reported volume</button>{/if}
  </div>
  {#if volumeChanged}<p class="inline-note">
      Ring reported a newer state. Review its volume before applying another level.
    </p>{/if}
  <div class="group">
    <Row
      title="Snooze duration"
      subtitle="Silence Chime alerts temporarily. The reported remaining time above is separate from this choice."
    >
      <select
        aria-label="Chime snooze duration"
        bind:value={snoozeMinutes}
        disabled={!canAct('snooze')}
      >
        {#each Object.entries(snoozeDurations) as [minutes, label]}<option value={Number(minutes)}
            >{label}</option
          >{/each}
      </select>
    </Row>
    {#if chime.automatic_nightlight !== null}<Row
        title="Enable automatic nightlight"
        subtitle="Enable or disable Ring’s light-sensor behavior."
      >
        <input
          type="checkbox"
          role="switch"
          aria-label="Enable automatic Chime nightlight"
          checked={current && chime.automatic_nightlight}
          disabled={!canAct('nightlight')}
          onchange={(event) => {
            event.currentTarget.checked = chime.automatic_nightlight === true;
            void command('nightlight', !chime.automatic_nightlight);
          }}
        />
      </Row>{/if}
  </div>
  <div class="actions chime-actions">
    <button disabled={!canAct('snooze')} onclick={() => command('snooze', snoozeMinutes)}
      >Snooze for {snoozeDurations[snoozeMinutes]}</button
    >
    <button
      disabled={!canAct('snooze') || chime.snooze_seconds_left === 0}
      onclick={() => command('snooze', 0)}>Clear snooze</button
    >
  </div>
  <h3>Sound tests</h3>
  <p class="inline-note">
    A test plays on this physical Chime. Ring accepting a request does not prove that sound played
    or finished.
  </p>
  <div class="actions chime-actions">
    <button disabled={!canAct('test')} onclick={() => (confirmation = 'ding')}
      >Test Ring sound</button
    >
    <button disabled={!canAct('test')} onclick={() => (confirmation = 'motion')}
      >Test Motion sound</button
    >
  </div>
  {#if chime.sound_active}<p class="notice">
      A sound test may still be playing. Listen at this Chime before confirming it has stopped.
      Other actions remain blocked until you acknowledge it.
    </p>
    <div class="actions">
      <button disabled={!canAct('sound_stopped')} onclick={() => command('sound_stopped', null)}
        >I have confirmed sound stopped</button
      >
    </div>{/if}
  {#if error}<p class="notice error" role="alert">{error}</p>{/if}
  {#if operation}<OperationStatus
      {operation}
      configuration={policyOperation}
      chime={!policyOperation}
      onupdate={(result) => {
        if (result.operation_id !== operation?.operation_id) return;
        operation = result;
        void refresh();
      }}
    />{/if}
  {#if canAcknowledge}<p class="inline-note">
      A newer Ring observation is available. Dismissing the earlier uncertain result neither
      confirms it nor repeats the request.
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
{#if confirmation}<Confirm title="Play a Chime sound test?" onclose={() => (confirmation = null)}>
    <p>
      This sends one audible {confirmation === 'ding' ? 'Ring' : 'Motion'} test to {device.name}.
      Confirm only when people nearby are ready. You will need to listen before acknowledging that
      the sound has stopped.
    </p>
    <div class="actions">
      <button onclick={() => (confirmation = null)}>Cancel</button><button
        class="primary"
        onclick={confirmSound}>Play {confirmation === 'ding' ? 'Ring' : 'Motion'} sound</button
      >
    </div>
  </Confirm>{/if}

<style>
  .chime-actions {
    margin-top: 16px;
    flex-wrap: wrap;
  }
</style>
