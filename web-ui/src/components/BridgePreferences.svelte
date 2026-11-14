<script lang="ts">
  import { onDestroy } from 'svelte';
  import { api, ApiError, errorMessage, operationId } from '../lib/api.ts';
  import {
    bridgeBody,
    bridgeDevicePreference,
    bridgeDevicePreferred,
    bridgeDeviceStatus,
    changeMembership,
    mappingLabels,
  } from '../lib/bridge.ts';
  import { deviceKind } from '../lib/noncamera.ts';
  import type {
    BridgePreferences,
    NoncameraDevice,
    NoncameraState,
    Operation,
  } from '../lib/types.ts';
  import Row from './Row.svelte';
  import OperationStatus from './OperationStatus.svelte';

  let {
    monitoring,
    device,
    fresh,
    generation,
    notify,
    refresh,
  }: {
    monitoring: NoncameraState;
    device?: NoncameraDevice;
    fresh: boolean;
    generation: string;
    notify: (message: string) => void;
    refresh: () => Promise<void>;
  } = $props();
  let busy = $state(false);
  let error = $state('');
  let operation = $state<Operation | null>(null);
  let location = $state('');
  let kind = $state('');
  let requestedScope = '';
  let destroyed = false;
  const bridge = $derived(monitoring.bridge);
  const identity = $derived(JSON.stringify([generation, monitoring.scope, device?.id]));
  const registration = $derived(bridge?.devices.find((item) => item.id === device?.id));
  const unsettled = $derived(operation?.status === 'pending' || operation?.status === 'unknown');
  const disabled = $derived(
    !fresh || busy || unsettled || bridge?.status !== 'ready' || !monitoring.scope.profile_id,
  );
  const locations = $derived([
    ...new Map(monitoring.devices.map((item) => [item.location_id, item.location_name])).entries(),
  ]);
  const kinds = $derived([...new Set(monitoring.devices.map((item) => item.kind))].sort());
  const mappingExclusion = $derived(
    registration?.eligible === true && registration.mappings.length === 0,
  );
  const legacySwitchExclusion = $derived(
    device?.kind === 'beams-light' && bridge?.excluded_kinds.includes('switch') === true,
  );
  const broaderExclusion = $derived(
    !!device &&
      !!bridge &&
      (bridge.excluded_locations.includes(device.location_id) ||
        bridge.excluded_kinds.includes(device.kind) ||
        legacySwitchExclusion ||
        mappingExclusion),
  );
  const preferred = $derived(!!device && !!bridge && bridgeDevicePreferred(bridge, device));

  $effect(() => {
    if (requestedScope !== identity) {
      requestedScope = identity;
      operation = null;
      error = '';
      location = '';
      kind = '';
    }
  });
  onDestroy(() => {
    destroyed = true;
  });

  async function save(patch: Partial<BridgePreferences>) {
    if (!bridge || disabled) return;
    const expected = identity;
    const id = operationId();
    busy = true;
    error = '';
    try {
      const result = await api.post<Operation>('devices/bridge', bridgeBody(monitoring, patch, id));
      if (destroyed || expected !== identity) return;
      operation = result;
      if (result.status === 'completed') {
        notify('Halo Bridge import preferences saved.');
        location = '';
        kind = '';
      }
      await refresh();
    } catch (cause) {
      if (destroyed || expected !== identity) return;
      error = errorMessage(cause);
      if (cause instanceof ApiError && cause.possiblySent)
        operation = { operation_id: id, status: 'unknown', possibly_sent: true };
      await refresh();
    } finally {
      busy = false;
    }
  }
</script>

{#if bridge}
  <h3>Halo Bridge</h3>
  {#if device}
    <div class="group">
      <Row
        title="Import status"
        subtitle="Saved preferences determine which device capabilities this bridge shares."
      >
        <span class="status"
          >{fresh && bridge.status === 'ready'
            ? bridgeDeviceStatus(registration)
            : 'Import status unavailable'}</span
        >
      </Row>
      <Row
        title="Include in Halo Bridge"
        subtitle={mappingExclusion
          ? 'All eligible device capabilities are excluded. Change capability preferences in Devices first.'
          : legacySwitchExclusion
            ? 'Excluded by a saved switch-family preference. Remove that device-type exclusion in Devices to include this light.'
            : broaderExclusion
              ? 'Excluded by a location or device-type preference. Change that preference in Devices first.'
              : device.kind === 'light-group'
                ? 'Groups require individual inclusion to avoid duplicate controls for lights already shared separately. Halo controls work without this import.'
                : 'Only qualified device capabilities are eligible. Alarm and light controls require their own opt-in.'}
      >
        <input
          type="checkbox"
          role="switch"
          aria-label={`Include ${device.name} in Halo Bridge`}
          checked={preferred && !broaderExclusion && registration?.eligible === true}
          disabled={disabled || !bridge.configured || !registration?.eligible || broaderExclusion}
          onchange={(event) => {
            event.currentTarget.checked =
              preferred && !broaderExclusion && registration?.eligible === true;
            void save(bridgeDevicePreference(bridge, device.id, !preferred));
          }}
        />
      </Row>
      <Row
        title="HomeKit publication"
        subtitle="This device shares Halo Bridge’s pairing and has no individual setup code."
        ><a href="#/integrations">Manage Halo Bridge</a></Row
      >
    </div>
    {#if registration?.mappings.length}<p class="inline-note">
        Eligible capabilities: {registration.mappings
          .map((mapping) => mappingLabels[mapping] ?? deviceKind(mapping))
          .join(', ')}.
      </p>{/if}
  {:else}
    <div class="group">
      <Row
        title="Prepare Halo Bridge"
        subtitle="Save import preferences and stable device identities for this Ring account."
        icon="integrations"
      >
        <input
          type="checkbox"
          role="switch"
          aria-label="Prepare Halo Bridge"
          checked={bridge.enabled}
          {disabled}
          onchange={(event) => {
            event.currentTarget.checked = bridge.enabled;
            void save({ enabled: !bridge.enabled });
          }}
        />
      </Row>
      <Row
        title="Automatically include supported devices"
        subtitle="Includes qualified devices as they are discovered. Lighting groups require individual inclusion to avoid duplicate controls. Exclusions take priority; alarm and light controls remain separate opt-ins."
      >
        <input
          type="checkbox"
          role="switch"
          aria-label="Automatically include supported devices"
          checked={bridge.auto_import}
          disabled={disabled || !bridge.configured}
          onchange={(event) => {
            event.currentTarget.checked = bridge.auto_import;
            void save({ auto_import: !bridge.auto_import });
          }}
        />
      </Row>
      <Row
        title="Import preview"
        subtitle={bridge.status !== 'ready'
          ? 'Saved bridge preferences are currently unavailable.'
          : bridge.configured
            ? `${bridge.retained_count} retained device identities`
            : 'Prepare the bridge to save its first configuration.'}
      >
        <span class="status"
          >{fresh && bridge.status === 'ready'
            ? `${bridge.selected_count} of ${bridge.capacity} selected`
            : 'Unavailable'}</span
        >
      </Row>
      <Row
        title="HomeKit publication"
        subtitle="Preparing the bridge does not connect it to HomeKit. Choose that separately in Integrations."
        ><a href="#/integrations">Manage Halo Bridge</a></Row
      >
    </div>
    {#if bridge.overflow_count > 0}<p class="notice">
        {bridge.overflow_count} eligible devices reached a selection or retained-identity limit. Excluding
        selected devices can free selection slots. Exclusions do not remove retained identities or resolve
        the retained-identity limit.
      </p>{/if}
    {#if bridge.truncated}<p class="inline-note">
        This import preview is incomplete. Saved preferences still cover the full inventory.
      </p>{/if}
    <details class="bridge-exclusions">
      <summary>Import exclusions</summary>
      <p class="inline-note">
        Exclude locations, device types, or individual device capabilities. Open a device to change
        its own import preference.
      </p>
      <div class="group">
        <Row title="Exclude a location">
          <select
            aria-label="Location to exclude from Halo Bridge"
            bind:value={location}
            disabled={disabled || !bridge.configured}
          >
            <option value="">Choose location</option>
            {#each locations.filter(([id]) => !bridge.excluded_locations.includes(id)) as [id, name]}<option
                value={id}>{name || 'Unnamed location'}</option
              >{/each}
          </select>
          <button
            disabled={disabled || !bridge.configured || !location}
            onclick={() =>
              save({
                excluded_locations: changeMembership(bridge.excluded_locations, location, true),
              })}>Exclude location</button
          >
        </Row>
        {#each bridge.excluded_locations as id (id)}
          <Row
            title={locations.find(([key]) => key === id)?.[1] || 'Unavailable location'}
            subtitle="Location excluded from import"
          >
            <button
              {disabled}
              aria-label={`Remove location exclusion for ${locations.find(([key]) => key === id)?.[1] || 'unavailable location'}`}
              onclick={() =>
                save({
                  excluded_locations: changeMembership(bridge.excluded_locations, id, false),
                })}>Remove exclusion</button
            >
          </Row>
        {/each}
        <Row title="Exclude a device type">
          <select
            aria-label="Device type to exclude from Halo Bridge"
            bind:value={kind}
            disabled={disabled || !bridge.configured}
          >
            <option value="">Choose device type</option>
            {#each kinds.filter((item) => !bridge.excluded_kinds.includes(item)) as item}<option
                value={item}>{deviceKind(item)}</option
              >{/each}
          </select>
          <button
            disabled={disabled || !bridge.configured || !kind}
            onclick={() =>
              save({ excluded_kinds: changeMembership(bridge.excluded_kinds, kind, true) })}
            >Exclude type</button
          >
        </Row>
        {#each bridge.excluded_kinds as item (item)}
          <Row title={deviceKind(item)} subtitle="Device type excluded from import">
            <button
              {disabled}
              aria-label={`Remove device type exclusion for ${deviceKind(item)}`}
              onclick={() =>
                save({ excluded_kinds: changeMembership(bridge.excluded_kinds, item, false) })}
              >Remove exclusion</button
            >
          </Row>
        {/each}
      </div>
      <h3>Device capabilities</h3>
      <div class="group">
        {#each Object.entries(mappingLabels) as [mapping, label]}
          <Row title={label} subtitle="Include this reported capability on eligible devices">
            <input
              type="checkbox"
              role="switch"
              aria-label={`Include ${label.toLowerCase()} capability in Halo Bridge`}
              checked={!bridge.excluded_mappings.includes(mapping)}
              disabled={disabled || !bridge.configured}
              onchange={(event) => {
                event.currentTarget.checked = !bridge.excluded_mappings.includes(mapping);
                void save({
                  excluded_mappings: changeMembership(
                    bridge.excluded_mappings,
                    mapping,
                    !bridge.excluded_mappings.includes(mapping),
                  ),
                });
              }}
            />
          </Row>
        {/each}
      </div>
    </details>
  {/if}
  {#if error}<p class="notice error" role="alert">{error}</p>{/if}
  {#if operation}<OperationStatus
      {operation}
      configuration
      onupdate={(value) => {
        if (!operation?.operation_id || value.operation_id !== operation.operation_id) return;
        operation = value;
        if (value.status === 'completed') void refresh();
      }}
    />{/if}
  {#if unsettled}<p class="inline-note">
      The save outcome is not confirmed. Further import changes are paused while Halo checks this
      operation.
    </p>{/if}
{/if}

<style>
  .bridge-exclusions {
    margin-top: 16px;
  }
  summary {
    cursor: pointer;
    font-weight: 600;
    min-height: 44px;
    padding: 10px 0;
  }
</style>
