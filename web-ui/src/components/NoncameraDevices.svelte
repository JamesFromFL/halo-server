<script lang="ts">
  import { onDestroy } from 'svelte';
  import { api, ApiError, errorMessage, operationId } from '../lib/api.ts';
  import { when } from '../lib/model.ts';
  import {
    deviceIcon,
    deviceKind,
    deviceMatches,
    deviceObservations,
    monitoringBody,
    monitoringStatus,
    noncameraHealth,
    noncameraRouteId,
  } from '../lib/noncamera.ts';
  import type { NoncameraDevice, NoncameraState, Operation } from '../lib/types.ts';
  import Icon from './Icon.svelte';
  import Row from './Row.svelte';
  import OperationStatus from './OperationStatus.svelte';
  import BridgePreferences from './BridgePreferences.svelte';
  import AlarmControls from './AlarmControls.svelte';
  import LightControls from './LightControls.svelte';

  let {
    monitoring,
    device,
    search = '',
    fresh,
    generation,
    reauthenticate,
    notify,
    refresh,
  }: {
    monitoring: NoncameraState | undefined;
    device?: NoncameraDevice;
    search?: string;
    fresh: boolean;
    generation: string;
    reauthenticate: () => Promise<boolean>;
    notify: (message: string) => void;
    refresh: () => Promise<void>;
  } = $props();
  let location = $state('');
  let kind = $state('');
  let busy = $state(false);
  let error = $state('');
  let operation = $state<Operation | null>(null);
  let requestedScope = '';
  let destroyed = false;
  const identity = $derived(JSON.stringify([generation, monitoring?.scope]));
  const locations = $derived(
    [
      ...new Map(
        (monitoring?.devices ?? []).map((item) => [item.location_id, item.location_name]),
      ).entries(),
    ].sort((a, b) => a[1].localeCompare(b[1])),
  );
  const kinds = $derived([...new Set((monitoring?.devices ?? []).map((item) => item.kind))].sort());
  const matches = $derived(
    (monitoring?.devices ?? []).filter((item) => deviceMatches(item, search, location, kind)),
  );
  const unsettled = $derived(operation?.status === 'pending' || operation?.status === 'unknown');
  const observations = $derived(
    device
      ? deviceObservations(device).filter(
          (item) =>
            (!device.alarm || !['alarm_mode', 'alarm_state', 'alarm_signal'].includes(item.key)) &&
            (!device.light || item.key !== 'light_on'),
        )
      : [],
  );
  const current = $derived(
    fresh &&
      !!monitoring?.enabled &&
      !!device &&
      (device.alarm || device.light
        ? device.source_health === 'online'
        : !device.stale && device.available === true),
  );
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
  async function setMonitoring(enabled: boolean) {
    if (!monitoring || !fresh || busy || (enabled && unsettled)) return;
    const expected = identity;
    const id = operationId();
    busy = true;
    error = '';
    try {
      const result = await api.post<Operation>(
        'devices/monitoring',
        monitoringBody(monitoring, enabled, id),
      );
      if (destroyed || expected !== identity) return;
      operation = result;
      if (result.status === 'completed') {
        notify(enabled ? 'Device monitoring enabled.' : 'Device monitoring disabled.');
        await refresh();
      }
    } catch (cause) {
      if (destroyed || expected !== identity) return;
      error = errorMessage(cause);
      if (cause instanceof ApiError && cause.possiblySent)
        operation = { operation_id: id, status: 'unknown', possibly_sent: true };
    } finally {
      busy = false;
    }
  }
</script>

{#if device}
  <div class="detail-heading">
    <a href="#/devices" aria-label="Back to devices"><Icon name="back" /></a>
    <div>
      <h2>{device.name}</h2>
      <p class="muted">{device.model || deviceKind(device.kind)}</p>
    </div>
  </div>
  {#if !current}<p class="notice">
      Current device state is unavailable. Missing or out-of-date observations do not mean a sensor
      is clear or an alarm is disarmed.
    </p>{/if}
  <h3>Device</h3>
  <div class="group">
    <Row title="Location"><span>{device.location_name || 'Not reported'}</span></Row>
    <Row title="Type"><span>{deviceKind(device.kind)}</span></Row>
    <Row title="Source availability"
      ><span class="status">{noncameraHealth(device, fresh && !!monitoring?.enabled)}</span></Row
    >
    <Row title="Last observation"><span>{when(device.observed_at)}</span></Row>
    <Row title="Support"
      ><span
        >{device.alarm
          ? 'Alarm monitoring and guarded controls'
          : device.light
            ? 'Light monitoring and guarded controls'
            : device.supported
              ? 'Read-only monitoring'
              : 'Not yet supported'}</span
      ></Row
    >
  </div>
  {#if !device.supported && device.unsupported_reason}<p class="inline-note">
      {deviceKind(device.unsupported_reason)}
    </p>{/if}
  {#if monitoring && device.alarm}<AlarmControls
      {monitoring}
      {device}
      {fresh}
      {generation}
      {reauthenticate}
      {notify}
      {refresh}
    />{/if}
  {#if monitoring && device.light}<LightControls
      {monitoring}
      {device}
      {fresh}
      {generation}
      {reauthenticate}
      {notify}
      {refresh}
    />{/if}
  {#if observations.length || (!device.alarm && !device.light)}<h3>Observations</h3>
    <div class="group">
      {#each observations as observation (observation.key)}
        <Row
          title={observation.title}
          subtitle={current ? 'Latest source observation' : 'No current authoritative observation'}
        >
          <span>{current ? observation.value : 'Unknown'}</span>
        </Row>
      {:else}<div class="empty">
          No qualified observations are available for this device yet.
        </div>{/each}
    </div>{/if}
  {#if monitoring?.bridge}
    <BridgePreferences {monitoring} {device} {fresh} {generation} {notify} {refresh} />
  {:else}
    <h3>HomeKit</h3>
    <div class="group">
      <Row
        title="Halo Bridge"
        subtitle="Halo Bridge information is unavailable in this server response."
        ><span>Unavailable</span></Row
      >
    </div>
  {/if}
  {#if !device.alarm && !device.light}<p class="inline-note">
      This device is read-only in Halo. Device commands, panic actions, and emergency dispatch are
      blocked by the server.
    </p>{/if}
{:else}
  <div class="section-head">
    <h3>Other Ring devices</h3>
    <span class="muted"
      >{monitoring?.devices.length ?? 0}{monitoring?.truncated ? '+' : ''} discovered</span
    >
  </div>
  <div class="group">
    <Row
      title="Device monitoring"
      subtitle="Discover other Ring devices and read their state. Cameras continue independently."
      icon="devices"
    >
      <input
        type="checkbox"
        role="switch"
        aria-label="Enable other Ring device monitoring"
        checked={monitoring?.enabled === true}
        disabled={!monitoring ||
          !fresh ||
          busy ||
          !monitoring.scope.profile_id ||
          (unsettled && !monitoring.enabled)}
        onchange={(event) => {
          event.currentTarget.checked = monitoring?.enabled === true;
          void setMonitoring(!monitoring?.enabled);
        }}
      />
    </Row>
    <Row
      title="Monitoring status"
      subtitle={monitoring?.complete ? 'Discovery completed' : 'Discovery may be incomplete'}
      ><span class="status">{monitoringStatus(monitoring, fresh)}</span></Row
    >
    <Row
      title="Alarm protections"
      subtitle="Normal alarm controls require a separate opt-in for each location. Panic actions and emergency dispatch remain blocked."
      icon="shield"
      ><span class="badge"
        >{monitoring?.safety.read_only === false ? 'Guarded controls' : 'Read-only'}</span
      ></Row
    >
  </div>
  {#if error}<p class="notice error" role="alert">{error}</p>{/if}
  {#if operation}<OperationStatus
      {operation}
      onupdate={(value) => {
        if (!operation?.operation_id || value.operation_id !== operation.operation_id) return;
        operation = value;
        if (value.status === 'completed') void refresh();
      }}
    />{/if}
  {#if unsettled}<div class="actions">
      <button disabled={!fresh || busy} onclick={() => setMonitoring(false)}
        >Disable device monitoring</button
      >
    </div>{/if}
  {#if monitoring?.bridge}<BridgePreferences
      {monitoring}
      {fresh}
      {generation}
      {notify}
      {refresh}
    />{/if}
  {#if monitoring?.truncated}<p class="notice">
      This device summary has reached its display limit. More devices may be present in the
      inventory.
    </p>
  {:else if monitoring?.enabled && !monitoring.complete}<p class="inline-note">
      Some devices may still be loading or their source may be unavailable. An incomplete discovery
      does not remove devices.
    </p>{/if}
  {#if (monitoring?.devices.length ?? 0) > 0}
    <div class="searchbar device-filters">
      <select aria-label="Filter other devices by location" bind:value={location}
        ><option value="">All locations</option>{#each locations as [id, name]}<option value={id}
            >{name || 'Unnamed location'}</option
          >{/each}</select
      >
      <select aria-label="Filter other devices by type" bind:value={kind}
        ><option value="">All device types</option>{#each kinds as item}<option value={item}
            >{deviceKind(item)}</option
          >{/each}</select
      >
    </div>
    <div class="group">
      {#each matches as item (item.id)}
        <a class="device-link" href={`#/devices/${encodeURIComponent(noncameraRouteId(item.id))}`}>
          <Icon name={deviceIcon(item.kind)} />
          <span class="row-label"
            ><span>{item.name}</span><small
              >{item.model || deviceKind(item.kind)} · {item.location_name ||
                'Location not reported'}</small
            ></span
          >
          <span class="device-status"
            ><span class="badge"
              >{item.alarm
                ? 'Alarm'
                : item.light
                  ? 'Light'
                  : item.supported
                    ? 'Read-only'
                    : 'Unsupported'}</span
            ><span class="status">{noncameraHealth(item, fresh && !!monitoring?.enabled)}</span
            ></span
          ><Icon name="next" size={16} />
        </a>
      {:else}<div class="empty">No other devices match your search or filters.</div>{/each}
    </div>
  {:else}<div class="empty">
      {!monitoring
        ? 'Other device discovery is not available in this server version.'
        : monitoring.enabled
          ? 'No other devices have been discovered yet.'
          : 'Enable monitoring to discover sensors, alarms, chimes, lighting, and other Ring devices.'}
    </div>{/if}
  <p class="inline-note">
    Manage Halo Bridge in <a href="#/integrations">Integrations</a> to share selected devices with Apple
    Home. Cameras keep their separate HomeKit accessories.
  </p>
{/if}

<style>
  .device-filters {
    margin-top: 16px;
  }
</style>
