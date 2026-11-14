<script lang="ts">
  import { onDestroy, onMount, untrack } from 'svelte';
  import { api, ApiError, errorMessage, operationId } from '../lib/api.ts';
  import {
    bridgePairingIdentity,
    bridgePairingMatches,
    bridgePublicationBody,
    bridgeRuntimeLabel,
    canResumeBridgeReset,
    validBridgePairing,
    validBridgePairingSecret,
  } from '../lib/bridge-homekit.ts';
  import { tri } from '../lib/model.ts';
  import type {
    BridgePairing,
    BridgePairingSecret,
    NoncameraState,
    Operation,
  } from '../lib/types.ts';
  import Confirm from './Confirm.svelte';
  import OperationStatus from './OperationStatus.svelte';
  import PairingCode from './PairingCode.svelte';
  import Row from './Row.svelte';

  let {
    monitoring,
    homekitEnabled,
    fresh,
    generation,
    reauthenticate,
    notify,
    refresh,
  }: {
    monitoring: NoncameraState;
    homekitEnabled: boolean;
    fresh: boolean;
    generation: string;
    reauthenticate: () => Promise<boolean>;
    notify: (message: string) => void;
    refresh: () => Promise<void>;
  } = $props();
  let pairing = $state<BridgePairing | null>(null);
  let secret = $state<BridgePairingSecret | null>(null);
  let operation = $state<Operation | null>(null);
  let resetOperation = $state(false);
  let error = $state('');
  let busy = $state(false);
  let loading = $state(false);
  let confirmation = $state(false);
  let destroyed = false;
  let serial = 0;
  let observedAuthority = '';
  let observedView = '';
  const bridge = $derived(monitoring.bridge);
  const runtime = $derived(bridge?.runtime);
  const authority = $derived(
    JSON.stringify([generation, monitoring.scope, bridge?.accessory_uuid]),
  );
  const view = $derived(
    JSON.stringify([
      authority,
      bridge?.revision,
      runtime?.publication_revision,
      runtime?.boot_id,
      runtime?.published,
      runtime?.reset_operation_id,
    ]),
  );
  const supported = $derived(bridge?.publication === 'implemented' && !!runtime);
  const unsettled = $derived(operation?.status === 'pending' || operation?.status === 'unknown');
  const currentPairing = $derived(
    pairing && bridgePairingMatches(pairing, monitoring) ? pairing : null,
  );
  const disabled = $derived(!fresh || busy || loading || bridge?.status !== 'ready');
  const canEnable = $derived(
    supported &&
      bridge?.configured &&
      bridge.enabled &&
      monitoring.enabled &&
      homekitEnabled &&
      !runtime?.reset_pending,
  );
  const canPair = $derived(
    !!currentPairing?.available &&
      !!currentPairing.published &&
      !!currentPairing.boot_id &&
      !!currentPairing.pairing_revision &&
      !currentPairing.pending_reset,
  );
  const canResume = $derived(
    canResumeBridgeReset(
      currentPairing,
      resetOperation && unsettled ? operation?.operation_id : undefined,
    ),
  );

  $effect(() => {
    const identity = authority;
    if (observedAuthority !== identity) {
      observedAuthority = identity;
      operation = null;
      error = '';
    }
  });
  $effect(() => {
    const identity = view;
    if (observedView !== identity) {
      observedView = identity;
      secret = null;
      confirmation = false;
      if (supported && bridge?.configured) void untrack(load);
      else {
        serial++;
        pairing = null;
        loading = false;
      }
    }
  });
  $effect(() => {
    if (!fresh) {
      secret = null;
      confirmation = false;
    }
  });
  onMount(() => {
    const hide = () => {
      if (document.hidden) {
        secret = null;
        confirmation = false;
      }
    };
    document.addEventListener('visibilitychange', hide);
    return () => document.removeEventListener('visibilitychange', hide);
  });
  onDestroy(() => {
    destroyed = true;
    serial++;
    secret = null;
  });

  async function load(clearError = true) {
    if (!supported || !bridge?.configured) return;
    const request = ++serial;
    const expected = view;
    secret = null;
    pairing = null;
    loading = true;
    try {
      const result = await api.get<BridgePairing>('devices/bridge/pairing');
      if (destroyed || request !== serial || expected !== view) return;
      if (!validBridgePairing(result) || !bridgePairingMatches(result, monitoring))
        throw new Error('Halo Bridge changed. Refresh its pairing information.');
      pairing = result;
      if (clearError) error = '';
    } catch (cause) {
      if (!destroyed && request === serial && expected === view) error = errorMessage(cause);
    } finally {
      if (request === serial) loading = false;
    }
  }

  async function mutate(path: string, body: Record<string, unknown>, id: string) {
    const expected = authority;
    busy = true;
    error = '';
    secret = null;
    resetOperation = path.endsWith('/reset');
    try {
      const result = await api.post<Operation>(path, { ...body, operation_id: id });
      if (destroyed || expected !== authority) return;
      operation = result;
      if (result.status === 'completed')
        notify(
          path.endsWith('/reset')
            ? 'Halo Bridge pairing reset.'
            : 'Halo Bridge connection updated.',
        );
      await refresh();
      if (!destroyed && expected === authority) await load();
    } catch (cause) {
      if (destroyed || expected !== authority) return;
      error = errorMessage(cause);
      if (cause instanceof ApiError && cause.possiblySent)
        operation = { operation_id: id, status: 'unknown', possibly_sent: true };
      await refresh();
      if (!destroyed && expected === authority) await load(false);
    } finally {
      busy = false;
      confirmation = false;
    }
  }

  async function publish(enabled: boolean) {
    if (disabled || unsettled || !supported || (enabled && !canEnable)) return;
    const id = operationId();
    try {
      await mutate(
        'devices/bridge/publication',
        bridgePublicationBody(monitoring, enabled, id),
        id,
      );
    } catch (cause) {
      error = errorMessage(cause);
    }
  }

  async function reveal() {
    if (disabled || unsettled || !canPair || !currentPairing) return;
    const target = currentPairing;
    const expected = view;
    const request = serial;
    secret = null;
    if (!(await reauthenticate()) || destroyed || expected !== view || request !== serial || !fresh)
      return;
    busy = true;
    error = '';
    try {
      const result = await api.post<BridgePairingSecret>(
        'devices/bridge/pairing/reveal',
        bridgePairingIdentity(target),
      );
      if (destroyed || expected !== view || request !== serial || !fresh || document.hidden) return;
      if (!validBridgePairingSecret(result, target))
        throw new Error('Halo returned invalid or changed bridge pairing information.');
      secret = result;
    } catch (cause) {
      if (!destroyed && expected === view && request === serial) error = errorMessage(cause);
    } finally {
      busy = false;
    }
  }

  async function reset() {
    confirmation = false;
    if (!currentPairing || disabled || (!canPair && !canResume)) return;
    const target = currentPairing;
    const expected = view;
    const request = serial;
    secret = null;
    if (!(await reauthenticate()) || destroyed || expected !== view || request !== serial || !fresh)
      return;
    const id =
      target.pending_reset && target.reset_operation_id ? target.reset_operation_id : operationId();
    try {
      await mutate('devices/bridge/pairing/reset', bridgePairingIdentity(target, true), id);
    } catch (cause) {
      error = errorMessage(cause);
    }
  }
</script>

<section aria-label="Halo Bridge HomeKit connection">
  <div class="section-head">
    <h3>Halo Bridge</h3>
    <button disabled={busy || loading || !supported || !bridge?.configured} onclick={() => load()}>
      {loading ? 'Refreshing…' : 'Refresh bridge'}
    </button>
  </div>
  <p class="inline-note">
    Add Halo Bridge once to bring its selected devices into Apple Home. Cameras keep their own
    pairing codes. Manage device selection in <a href="#/devices">Devices</a>.
  </p>
  <div class="group">
    <Row
      title="Connect Halo Bridge to Apple Home"
      subtitle={!supported
        ? 'This server version does not publish Halo Bridge yet.'
        : !bridge?.configured || !bridge.enabled
          ? 'Prepare Halo Bridge in Devices first.'
          : !monitoring.enabled
            ? 'Enable device monitoring in Devices first.'
            : !homekitEnabled
              ? 'Enable HomeKit mode in Settings first.'
              : 'Publish this bridge and its selected devices on your local network. Alarm and light controls require their own opt-in.'}
      icon="integrations"
    >
      <input
        type="checkbox"
        role="switch"
        aria-label="Connect Halo Bridge to Apple Home"
        checked={runtime?.publication_enabled === true}
        disabled={disabled ||
          unsettled ||
          !supported ||
          (!runtime?.publication_enabled && !canEnable) ||
          runtime?.reset_pending}
        onchange={(event) => {
          event.currentTarget.checked = runtime?.publication_enabled === true;
          void publish(!runtime?.publication_enabled);
        }}
      />
    </Row>
    <Row title="Bridge connection"
      ><span class="status">{bridgeRuntimeLabel(runtime, fresh)}</span></Row
    >
    <Row title="HomeKit pairing" subtitle="All selected devices share this bridge’s pairing.">
      <span
        >{fresh && currentPairing?.paired_known
          ? tri(currentPairing.paired, 'Paired', 'Not paired')
          : 'Unknown'}</span
      >
    </Row>
    <Row title="Selected devices"
      ><span>{fresh && bridge ? `${bridge.selected_count} of ${bridge.capacity}` : 'Unknown'}</span
      ></Row
    >
    <Row title="Pairing code" subtitle="Confirm your Halo password to show the code briefly.">
      <button disabled={disabled || unsettled || !canPair} onclick={reveal}
        >Show bridge pairing code</button
      >
    </Row>
    <Row
      title="Reset bridge pairing"
      subtitle="Removes this bridge’s HomeKit pairing and resets all of its device accessories."
    >
      <button
        class="danger"
        disabled={disabled || (!canPair && !canResume) || (unsettled && !canResume)}
        onclick={() => (confirmation = true)}
      >
        {currentPairing?.pending_reset ? 'Resume bridge reset…' : 'Reset bridge…'}
      </button>
    </Row>
  </div>
  {#if runtime?.reset_pending}<p class="notice">
      A bridge reset is unfinished. Halo keeps the bridge disconnected until you explicitly resume
      and finish it.
    </p>{/if}
  {#if secret}<PairingCode {secret} onhide={() => (secret = null)} />{/if}
  {#if error}<p class="notice error" role="alert">{error}</p>{/if}
  {#if operation}<OperationStatus
      {operation}
      configuration
      pairing={resetOperation}
      onupdate={(result) => {
        if (result.operation_id !== operation?.operation_id) return;
        operation = result;
        if (result.status === 'completed' || result.status === 'pending') {
          void refresh();
          void load();
        }
      }}
    />{/if}
</section>

{#if confirmation}<Confirm title="Reset Halo Bridge?" onclose={() => (confirmation = false)}>
    <p>
      This resets Halo Bridge and every device accessory paired through it. Camera pairings stay
      unchanged.
    </p>
    <p>
      After the reset, remove the old Halo Bridge from Apple Home, reconnect it here, and add it
      again. Its HomeKit automations may need to be recreated. The bridge remains disconnected until
      you choose to reconnect.
    </p>
    <div class="actions end">
      <button onclick={() => (confirmation = false)}>Cancel</button><button
        class="danger"
        onclick={reset}>Confirm bridge reset</button
      >
    </div>
  </Confirm>{/if}
