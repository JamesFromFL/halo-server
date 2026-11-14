<script lang="ts">
  import { onDestroy, untrack } from 'svelte';
  import { api, ApiError, errorMessage, operationId } from '../lib/api.ts';
  import { pretty } from '../lib/model.ts';
  import type { StateEnvelope, CameraPage, Operation } from '../lib/types.ts';
  import Row from '../components/Row.svelte';
  import Icon from '../components/Icon.svelte';
  import OperationStatus from '../components/OperationStatus.svelte';
  import BridgeHomeKit from '../components/BridgeHomeKit.svelte';
  let {
    snapshot,
    fresh,
    generation,
    reauthenticate,
    notify,
    refresh,
  }: {
    snapshot: StateEnvelope | null;
    fresh: boolean;
    generation: string;
    reauthenticate: () => Promise<boolean>;
    notify: (message: string) => void;
    refresh: () => Promise<void>;
  } = $props();
  let selection = $state<CameraPage | null>(null);
  let error = $state('');
  let busy = $state(false);
  let loading = $state(false);
  let operation = $state<Operation | null>(null);
  let serial = 0;
  let destroyed = false;
  let observedGeneration = '';
  const unsettled = $derived(operation?.status === 'pending' || operation?.status === 'unknown');
  $effect(() => {
    const identity = generation;
    if (observedGeneration !== identity) {
      observedGeneration = identity;
      serial++;
      selection = null;
      operation = null;
      error = '';
      busy = false;
    }
    if (identity) void untrack(load);
  });
  onDestroy(() => {
    destroyed = true;
    serial++;
  });
  async function load() {
    const token = ++serial;
    const expected = generation;
    loading = true;
    try {
      const value = await api.get<CameraPage>('selection');
      if (!destroyed && token === serial && expected === generation) {
        selection = value;
        error = '';
      }
    } catch (e) {
      if (!destroyed && token === serial && expected === generation) error = errorMessage(e);
    } finally {
      if (token === serial) loading = false;
    }
  }
  async function select(camera_id: string, enabled: boolean) {
    if (!selection || !fresh || busy || loading || unsettled) return;
    const expected = generation;
    busy = true;
    error = '';
    const id = operationId();
    try {
      const result = await api.post<Operation>('selection', {
        camera_id,
        enabled,
        profile_id: selection.profile_id,
        account_generation: selection.account_generation,
        expected_revision: selection.revision,
        operation_id: id,
      });
      if (destroyed || expected !== generation) return;
      operation = result;
      if (result.status === 'completed') {
        notify('Camera selection saved.');
        await refresh();
        if (!destroyed && expected === generation) await load();
      }
    } catch (e) {
      if (destroyed || expected !== generation) return;
      error = errorMessage(e);
      if (e instanceof ApiError && e.possiblySent)
        operation = { status: 'unknown', operation_id: id, possibly_sent: true };
    } finally {
      if (!destroyed && expected === generation) busy = false;
    }
  }
</script>

<div class="page-intro">
  <h2>Integrations</h2>
  <p>Choose how Halo makes your Ring devices available.</p>
</div>
<div class="group">
  <div class="summary">
    <span class="summary-icon"><Icon name="integrations" size={24} /></span>
    <div>
      <strong>Apple HomeKit</strong>
      <p class="muted">
        {fresh ? pretty(snapshot?.state?.homekit.state) : 'Current status unknown'}. Cameras connect
        directly through Halo’s HomeKit adapter.
      </p>
    </div>
  </div>
</div>
{#if snapshot?.state?.noncamera?.bridge}
  <BridgeHomeKit
    monitoring={snapshot.state.noncamera}
    homekitEnabled={snapshot.state.homekit.enabled}
    {fresh}
    {generation}
    {reauthenticate}
    {notify}
    {refresh}
  />
{:else if !snapshot?.state}<p class="notice" role="status">
    Integration status is unavailable while Halo reconnects. Saved preferences have not been
    removed.
  </p>
{/if}
<div class="section-head">
  <h3>Camera selection</h3>
  <button disabled={busy || loading} onclick={load}>{loading ? 'Refreshing…' : 'Refresh'}</button>
</div>
<p class="inline-note">
  Each change preserves every other camera’s selection. Temporarily missing cameras remain here
  until you explicitly deselect them.
</p>
{#if error}<div class="notice error" role="alert">{error}</div>{/if}
{#if operation}<OperationStatus
    {operation}
    configuration
    onupdate={(v) => {
      if (!operation?.operation_id || v.operation_id !== operation.operation_id) return;
      operation = v;
      if (v.status === 'completed') {
        void refresh();
        void load();
      }
    }}
  />{/if}
<div class="group">
  {#each selection?.cameras ?? [] as row (String(row.camera_id))}
    <Row
      title={typeof row.name === 'string' && row.name
        ? row.name
        : `Unavailable camera (${row.camera_id})`}
      subtitle={row.available === false
        ? 'Source device is temporarily unavailable'
        : 'Selection does not confirm pairing or stream quality'}
      icon="camera"
      ><input
        type="checkbox"
        role="switch"
        aria-label={`Expose ${row.name || row.camera_id} in Apple Home`}
        checked={row.selected === true}
        disabled={!fresh ||
          busy ||
          loading ||
          unsettled ||
          (row.available === false && row.selected !== true)}
        onchange={(event) => {
          event.currentTarget.checked = row.selected === true;
          void select(String(row.camera_id), row.selected !== true);
        }}
      /></Row
    >
  {:else}<div class="empty">
      {loading
        ? 'Loading registered cameras…'
        : !selection
          ? 'Camera selection is unavailable. Refresh when Halo is connected.'
          : 'No cameras are registered for HomeKit.'}
    </div>{/each}
</div>
<p class="inline-note">
  Open a camera in <a href="#/devices">Devices</a> to check its pairing status or reveal its setup code.
  Other ecosystems will appear here when supported.
</p>
