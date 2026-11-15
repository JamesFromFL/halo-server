<script lang="ts">
  import { onDestroy } from 'svelte';
  import { api, ApiError, errorMessage, operationId } from '../lib/api.ts';
  import {
    keypadBrightnessBody,
    keypadCanSet,
    keypadLockBody,
    keypadPolicyBody,
    qualifiedKeypad,
    validBrightness,
  } from '../lib/keypad.ts';
  import type { NoncameraDevice, NoncameraState, Operation } from '../lib/types.ts';
  import Row from './Row.svelte';
  import OperationStatus from './OperationStatus.svelte';

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
  let brightnessBusy = $state(false),
    policyBusy = $state(false),
    authenticating = $state(false),
    error = $state('');
  let operation = $state<Operation | null>(null),
    policyOperation = $state<Operation | null>(null);
  let submittedRevision = $state<number | null>(null),
    draftBrightness = $state(100),
    draftDirty = $state(false);
  let previousAuthority = '',
    previousBrightness: number | null | undefined = undefined,
    destroyed = false;
  const keypad = $derived(device.keypad!);
  const authority = $derived(
    JSON.stringify([
      generation,
      monitoring.scope,
      device.id,
      device.location_id,
      keypad.provider_generation,
      keypad.epoch,
    ]),
  );
  const current = $derived(fresh && monitoring.enabled && device.source_health === 'online');
  const unsettled = $derived(
    operation?.status === 'pending' ||
      operation?.status === 'unknown' ||
      (operation?.result as Record<string, unknown> | undefined)?.status === 'uncertain',
  );
  const policyUnsettled = $derived(
    policyOperation?.status === 'pending' || policyOperation?.status === 'unknown',
  );
  const canSet = $derived(
    !brightnessBusy &&
      !policyBusy &&
      !authenticating &&
      !unsettled &&
      !policyUnsettled &&
      keypadCanSet(monitoring, device, fresh),
  );
  // Locking stays available while a brightness command is pending. It revokes any unsent write.
  const canLock = $derived(
    fresh &&
      keypad.enabled &&
      keypad.can_set_lock &&
      !policyBusy &&
      !authenticating &&
      !policyUnsettled,
  );
  const canGrant = $derived(
    fresh &&
      !policyBusy &&
      !authenticating &&
      !policyUnsettled &&
      !!monitoring.scope.profile_id &&
      qualifiedKeypad(device),
  );
  const motionQualified = $derived(
    device.capabilities.includes('motion') &&
      device.capability_evidence?.includes('ring-keypad-motion-impulse'),
  );
  const motion = $derived(current && motionQualified ? device.observed.motion_detected : null);
  const canDismiss = $derived(
    fresh &&
      unsettled &&
      operation?.status !== 'pending' &&
      submittedRevision !== null &&
      keypad.observed_revision > submittedRevision &&
      !keypad.outcome_unknown &&
      !keypad.pending,
  );

  $effect(() => {
    if (previousAuthority !== authority) {
      previousAuthority = authority;
      previousBrightness = keypad.brightness;
      draftBrightness = keypad.brightness ?? keypad.on_brightness;
      draftDirty = false;
      operation = null;
      policyOperation = null;
      submittedRevision = null;
      error = '';
    } else if (previousBrightness !== keypad.brightness) {
      previousBrightness = keypad.brightness;
      if (!draftDirty && keypad.brightness !== null) draftBrightness = keypad.brightness;
    }
  });
  onDestroy(() => {
    destroyed = true;
  });

  async function mutate(
    path: string,
    payload: Record<string, unknown>,
    id: string,
    policy: boolean,
  ) {
    const expected = authority;
    if (policy) policyBusy = true;
    else {
      brightnessBusy = true;
      submittedRevision = keypad.observed_revision;
    }
    error = '';
    try {
      const result = await api.post<Operation>(path, payload);
      if (destroyed || authority !== expected) return;
      if (policy) policyOperation = result;
      else operation = result;
      if (result.status === 'completed') {
        if (policy) notify('Keypad preferences saved.');
        else if ((result.result as Record<string, unknown> | undefined)?.status === 'confirmed') {
          notify('Ring confirmed the keypad brightness.');
          draftDirty = false;
        }
      }
      await refresh();
    } catch (cause) {
      if (destroyed || authority !== expected) return;
      error = errorMessage(cause);
      if (cause instanceof ApiError && cause.possiblySent) {
        const unknown: Operation = { operation_id: id, status: 'unknown', possibly_sent: true };
        if (policy) policyOperation = unknown;
        else operation = unknown;
      }
      await refresh();
    } finally {
      if (policy) policyBusy = false;
      else brightnessBusy = false;
    }
  }
  async function grant(enabled: boolean) {
    if (!canGrant) return;
    const expected = authority,
      revision = keypad.policy_revision;
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
    if (!canGrant || expected !== authority || revision !== keypad.policy_revision) {
      error = 'Keypad preferences changed. Review them before trying again.';
      return;
    }
    const id = operationId();
    // Enabling controls starts protected; it does not change brightness or Ring LED patterns.
    try {
      await mutate(
        'devices/keypad/policy',
        keypadPolicyBody(monitoring, device, enabled, enabled ? true : keypad.locked, id),
        id,
        true,
      );
    } catch (cause) {
      error = errorMessage(cause);
    }
  }
  async function setLock(locked: boolean) {
    if (!canLock) return;
    const id = operationId();
    try {
      await mutate('devices/keypad/lock', keypadLockBody(monitoring, device, locked, id), id, true);
    } catch (cause) {
      error = errorMessage(cause);
    }
  }
  async function setBrightness(target: number) {
    if (!canSet || !validBrightness(target)) return;
    const id = operationId();
    try {
      await mutate(
        'devices/keypad/brightness',
        keypadBrightnessBody(monitoring, device, target, id),
        id,
        false,
      );
    } catch (cause) {
      error = errorMessage(cause);
    }
  }
</script>

<section aria-label="Keypad brightness and motion">
  <h3>Keypad light</h3>
  <div class="group">
    <Row
      title="Configured brightness"
      subtitle="The level reported by Ring. The keypad’s LED pattern controls when its lights illuminate."
      ><span class="status"
        >{current && keypad.brightness !== null ? `${keypad.brightness}%` : 'Unknown'}</span
      ></Row
    >
    <Row
      title="Allow keypad controls"
      subtitle="Enable brightness and lock controls in Halo and Apple Home. Enabling starts with brightness locked; it does not change the light."
      ><input
        type="checkbox"
        role="switch"
        aria-label="Allow keypad controls"
        checked={keypad.enabled}
        disabled={!canGrant}
        onchange={(event) => {
          const enabled = event.currentTarget.checked;
          event.currentTarget.checked = keypad.enabled;
          void grant(enabled);
        }}
      /></Row
    >
    <Row
      title="Brightness lock"
      subtitle="Prevents Halo and Siri from changing the level, including room-wide lights-off commands. Saved across Halo restarts."
      ><input
        type="checkbox"
        role="switch"
        aria-label="Brightness lock"
        checked={keypad.locked}
        disabled={!canLock}
        onchange={(event) => {
          const locked = event.currentTarget.checked;
          event.currentTarget.checked = keypad.locked;
          void setLock(locked);
        }}
      /></Row
    >
    <Row
      title="Adjust brightness"
      subtitle={keypad.locked
        ? 'Unlock brightness to adjust the level, then lock it again.'
        : 'Choose a level and apply it. Moving the slider does not send a command.'}
    >
      <div class="brightness-input">
        <input
          type="range"
          min="0"
          max="100"
          step="1"
          aria-label="Keypad brightness"
          bind:value={draftBrightness}
          disabled={!canSet}
          oninput={() => {
            draftDirty = true;
          }}
        /><output aria-live="off">{draftBrightness}%</output>
      </div>
    </Row>
  </div>
  <div class="actions keypad-actions">
    <button
      class="primary"
      disabled={!canSet ||
        !validBrightness(draftBrightness) ||
        draftBrightness === keypad.brightness}
      onclick={() => setBrightness(draftBrightness)}>Apply brightness</button
    ><button disabled={!canSet || keypad.brightness === 0} onclick={() => setBrightness(0)}
      >Turn off</button
    >
  </div>
  {#if draftDirty && draftBrightness !== keypad.brightness}<p class="inline-note">
      The slider has an unsaved level. Configured brightness above is the current Ring setting.
      {#if current && keypad.brightness !== null}<button
          class="text-button"
          onclick={() => {
            draftBrightness = keypad.brightness!;
            draftDirty = false;
          }}>Reset slider</button
        >{/if}
    </p>{/if}
  {#if keypad.pending || brightnessBusy}<p class="inline-note" role="status">
      Waiting for Ring to report the brightness. The lock remains available to block any unsent
      request.
    </p>{:else if keypad.outcome_unknown}<p class="notice">
      The last brightness request has an uncertain outcome. Halo will not resend it automatically.
    </p>{/if}
  {#if keypad.requested_brightness !== null}<p class="inline-note">
      Requested level: {keypad.requested_brightness}%. This is separate from the reported
      brightness.
    </p>{/if}
  {#if current && keypad.reason && !['brightness-locked', 'controls-disabled'].includes(keypad.reason)}<p
      class="inline-note"
    >
      Control availability: {keypad.reason.replaceAll('-', ' ')}.
    </p>{/if}
  <p class="inline-note">
    The lock applies to Halo and Apple Home. Ring’s app, LED patterns and power-saving settings can
    still affect the light. Siri may report that this protected light could not be changed.
  </p>
  <h3>Proximity motion</h3>
  <div class="group">
    <Row
      title="Keypad motion"
      subtitle={motionQualified
        ? 'An event stays active for 20 seconds after the most recent detection.'
        : 'Waiting for this keypad to report its first proximity event. HomeKit motion support appears after a real event is received.'}
      ><span
        >{motion === true ? 'Detected' : motion === false ? 'No recent detection' : 'Unknown'}</span
      ></Row
    >
  </div>
  <p class="inline-note">
    Ring must allow motion wake for proximity events. In the Ring app, check this keypad’s LED
    Settings. Halo does not change those settings.
  </p>
  {#if error}<p class="notice error" role="alert">{error}</p>{/if}
  {#if policyOperation}<OperationStatus
      operation={policyOperation}
      configuration
      onupdate={(result) => {
        if (result.operation_id !== policyOperation?.operation_id) return;
        policyOperation = result;
        void refresh();
      }}
    />{/if}
  {#if operation}<OperationStatus
      {operation}
      keypad
      onupdate={(result) => {
        if (result.operation_id !== operation?.operation_id) return;
        operation = result;
        void refresh();
      }}
    />{/if}
  {#if canDismiss}<div class="actions">
      <button
        onclick={() => {
          if (!canDismiss) return;
          operation = null;
          submittedRevision = null;
          error = '';
        }}>Use current observed state</button
      >
    </div>{/if}
</section>

<style>
  .keypad-actions {
    margin-top: 16px;
    flex-wrap: wrap;
  }
  .brightness-input {
    display: flex;
    align-items: center;
    gap: 12px;
    max-width: 100%;
  }
  .brightness-input input {
    width: 170px;
    max-width: 100%;
    min-width: 80px;
    accent-color: var(--accent);
  }
  .brightness-input output {
    min-width: 4ch;
    text-align: right;
    font-variant-numeric: tabular-nums;
  }
  @media (max-width: 480px) {
    .brightness-input input {
      width: 120px;
    }
  }
</style>
