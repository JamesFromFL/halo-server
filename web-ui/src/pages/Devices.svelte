<script lang="ts">
  import { onDestroy, untrack } from 'svelte';
  import { api, ApiError, errorMessage, operationId } from '../lib/api.ts';
  import {
    canResumeReset,
    pairingIdentity,
    samePairingScope,
    selectionBody,
    tri,
  } from '../lib/model.ts';
  import { cameraDetailTarget, noncameraDevice } from '../lib/noncamera.ts';
  import type {
    StateEnvelope,
    CameraPage,
    PairingCamera,
    PairingSecret,
    Operation,
  } from '../lib/types.ts';
  import Row from '../components/Row.svelte';
  import Status from '../components/Status.svelte';
  import Icon from '../components/Icon.svelte';
  import Confirm from '../components/Confirm.svelte';
  import PairingCode from '../components/PairingCode.svelte';
  import OperationStatus from '../components/OperationStatus.svelte';
  import NoncameraDevices from '../components/NoncameraDevices.svelte';
  let {
    snapshot,
    fresh,
    deviceId,
    generation,
    reauthenticate,
    notify,
    refresh,
  }: {
    snapshot: StateEnvelope | null;
    fresh: boolean;
    deviceId: string;
    generation: string;
    reauthenticate: () => Promise<boolean>;
    notify: (s: string) => void;
    refresh: () => Promise<void>;
  } = $props();
  let search = $state('');
  let filter = $state('');
  let selection = $state<CameraPage | null>(null);
  let pairing = $state<CameraPage<PairingCamera> | null>(null);
  let recording = $state<CameraPage | null>(null);
  let controls = $state<CameraPage | null>(null);
  let secret = $state<PairingSecret | null>(null);
  let error = $state('');
  let loading = $state(false);
  let busy = $state(false);
  let confirmation = $state<'reset' | 'siren' | 'warm' | null>(null);
  let operation = $state<Operation | null>(null);
  let serial = 0;
  let destroyed = false;
  let operationDevice = '';
  const device = $derived(cameraDetailTarget(snapshot, deviceId));
  const cameraKey = $derived(device?.camera_id ?? '');
  const noncamera = $derived(snapshot?.state?.noncamera);
  const otherDevice = $derived(noncameraDevice(noncamera, deviceId));
  const matches = $derived(
    (snapshot?.state?.devices ?? []).filter(
      (d) =>
        `${d.name} ${d.model}`.toLocaleLowerCase().includes(search.toLocaleLowerCase()) &&
        (!filter || (filter === 'selected' ? d.homekit.selected : !d.homekit.selected)),
    ),
  );
  const pair = $derived(pairing?.cameras.find((d) => d.camera_id === device?.camera_id));
  const warm = $derived(recording?.cameras.find((d) => d.camera_id === device?.camera_id));
  const warmBudget = $derived(
    recording?.warm_budget as { capacity: number; reserved: number } | undefined,
  );
  const control = $derived(controls?.cameras.find((d) => d.camera_id === device?.camera_id));
  const unsettled = $derived(operation?.status === 'unknown' || operation?.status === 'pending');
  const disabled = $derived(!fresh || busy || loading || unsettled);
  $effect(() => {
    const key = cameraKey;
    const epoch = generation;
    if (operationDevice !== key) {
      operation = null;
      operationDevice = key;
    }
    if (key && epoch) void untrack(load);
    else {
      serial++;
      secret = null;
      pairing = null;
      selection = null;
      recording = null;
      controls = null;
      loading = false;
      confirmation = null;
    }
  });
  $effect(() => {
    if (!fresh) {
      secret = null;
      confirmation = null;
    }
  });
  onDestroy(() => {
    destroyed = true;
    serial++;
    secret = null;
  });
  async function load() {
    if (!device) return;
    const token = ++serial;
    secret = null;
    pairing = null;
    selection = null;
    recording = null;
    controls = null;
    loading = true;
    error = '';
    const values = await Promise.allSettled([
      api.get<CameraPage>('selection'),
      api.get<CameraPage<PairingCamera>>('pairing'),
      api.get<CameraPage>('recording'),
      api.get<CameraPage>('controls'),
    ]);
    if (destroyed || token !== serial) return;
    if (values[0].status === 'fulfilled') selection = values[0].value;
    if (values[1].status === 'fulfilled') pairing = values[1].value;
    if (values[2].status === 'fulfilled') recording = values[2].value;
    if (values[3].status === 'fulfilled') controls = values[3].value;
    const failure = values.find((v) => v.status === 'rejected');
    if (failure?.status === 'rejected') error = errorMessage(failure.reason);
    loading = false;
  }
  async function mutate(path: string, body: Record<string, unknown>, id: string) {
    busy = true;
    error = '';
    secret = null;
    const target = deviceId;
    const expected = generation;
    try {
      const value = await api.post<Operation>(path, { ...body, operation_id: id });
      if (destroyed || target !== deviceId || expected !== generation) return;
      operation = value;
      if (operation.status === 'completed') {
        notify('Halo received the change.');
        await refresh();
        await load();
      } else if (path === 'pairing/reset' && operation.status === 'pending') await load();
    } catch (e) {
      if (destroyed || target !== deviceId || expected !== generation) return;
      error = errorMessage(e);
      if (e instanceof ApiError && e.possiblySent)
        operation = { operation_id: id, status: 'unknown', possibly_sent: true };
    } finally {
      busy = false;
      confirmation = null;
    }
  }
  async function select(enabled: boolean) {
    if (!device || !selection || disabled) return;
    const id = operationId();
    await mutate('selection', selectionBody(device, selection.revision, enabled, id), id);
  }
  async function reveal() {
    if (!pairing || !pair || disabled) return;
    const expected = generation;
    const target = deviceId;
    const requestSerial = serial;
    const requestedScope = { generation: expected, deviceId: target, serial: requestSerial };
    if (
      !(await reauthenticate()) ||
      destroyed ||
      !samePairingScope(requestedScope, { generation, deviceId, serial }) ||
      !fresh
    )
      return;
    busy = true;
    error = '';
    try {
      const value = await api.post<PairingSecret>('pairing/reveal', pairingIdentity(pairing, pair));
      if (
        !destroyed &&
        samePairingScope(requestedScope, { generation, deviceId, serial }) &&
        value.camera_id === device?.camera_id &&
        !document.hidden &&
        fresh &&
        device?.homekit.selected
      )
        secret = value;
    } catch (e) {
      error = errorMessage(e);
    } finally {
      busy = false;
    }
  }
  async function reset() {
    if (!pairing || !pair) return;
    confirmation = null;
    const expected = generation;
    const target = deviceId;
    const requestSerial = serial;
    const requestedScope = { generation: expected, deviceId: target, serial: requestSerial };
    if (
      !(await reauthenticate()) ||
      destroyed ||
      !samePairingScope(requestedScope, { generation, deviceId, serial }) ||
      !fresh
    )
      return;
    const id =
      pair.pending_reset && pair.reset_operation_id ? pair.reset_operation_id : operationId();
    await mutate('pairing/reset', pairingIdentity(pairing, pair), id);
  }
  async function command(kind: 'light' | 'siren', enabled: boolean) {
    if (
      !device ||
      !controls ||
      !control ||
      !fresh ||
      busy ||
      loading ||
      (unsettled && !(kind === 'siren' && !enabled))
    )
      return;
    if (kind === 'siren' && enabled && confirmation !== 'siren') {
      confirmation = 'siren';
      return;
    }
    const id = operationId();
    await mutate(
      'controls',
      {
        profile_id: controls.profile_id,
        account_generation: controls.account_generation,
        camera_id: device.camera_id,
        device_generation: control.device_generation,
        control: kind,
        enabled,
      },
      id,
    );
  }
  async function setWarm(enabled: boolean) {
    if (!device || !recording || disabled) return;
    if (enabled && confirmation !== 'warm') {
      confirmation = 'warm';
      return;
    }
    const id = operationId();
    await mutate(
      'recording/warm',
      {
        profile_id: recording.profile_id,
        account_generation: recording.account_generation,
        camera_id: device.camera_id,
        expected_revision: recording.revision,
        enabled,
      },
      id,
    );
  }
</script>

{#if !deviceId}
  <div class="page-intro">
    <h2>Devices</h2>
    <p>Your Ring cameras, other devices, and their current integration state.</p>
  </div>
  <div class="searchbar">
    <input
      type="search"
      aria-label="Search devices"
      placeholder="Search devices"
      bind:value={search}
    /><select aria-label="Filter HomeKit selection" bind:value={filter}
      ><option value="">All cameras</option><option value="selected">Selected for HomeKit</option
      ><option value="unselected">Not selected</option></select
    >
  </div>
  <h3>Cameras</h3>
  <div class="group">
    {#each matches as item (item.id)}<a
        class="device-link"
        href={`#/devices/${encodeURIComponent(item.id)}`}
        ><Icon name={item.kind === 'doorbell' ? 'bell' : 'camera'} /><span class="row-label"
          ><span>{item.name}</span><small>{item.model || 'Ring camera'}</small></span
        ><span class="device-status"
          ><span class="badge">{item.homekit.selected ? 'HomeKit selected' : 'Not selected'}</span
          ><Status
            value={fresh ? item.observed.source_available : null}
            yes="Source available"
            no="Source unavailable"
          /></span
        ><Icon name="next" size={16} /></a
      >{:else}<div class="empty">
        {search || filter
          ? 'No devices match your search.'
          : 'No cameras are currently discovered. Check the Ring connection in Settings.'}
      </div>{/each}
  </div>
  <p class="inline-note">
    Historical HomeKit accessories remain in <a href="#/integrations">Integrations</a> if a source device
    is temporarily missing.
  </p>
  <NoncameraDevices monitoring={noncamera} {search} {fresh} {generation} {notify} {refresh} />
{:else if otherDevice}
  <NoncameraDevices
    monitoring={noncamera}
    device={otherDevice}
    {fresh}
    {generation}
    {notify}
    {refresh}
  />
{:else if !device}<a href="#/devices">← Devices</a>
  <div class="empty">
    <h2>Device is no longer available</h2>
    <p>Its account or inventory scope changed. Refresh the device list.</p>
  </div>
{:else}
  <div class="detail-heading">
    <a href="#/devices" aria-label="Back to devices"><Icon name="back" /></a>
    <div>
      <h2>{device.name}</h2>
      <p class="muted">{device.model || 'Ring camera'}</p>
    </div>
  </div>
  {#if error}<div class="notice error" role="alert">
      {error}
      <div class="actions"><button onclick={load} disabled={busy}>Refresh details</button></div>
    </div>{/if}
  {#if loading}<p role="status" class="inline-note">Loading device administration…</p>{/if}
  {#if operation}<OperationStatus
      {operation}
      onupdate={(value) => {
        operation = value;
        if (value.status === 'completed') {
          void refresh();
          void load();
        }
      }}
    />{/if}
  <h3>Source</h3>
  <div class="group">
    <Row title="Ring availability" subtitle="Latest source observation"
      ><Status value={fresh ? device.observed.source_available : null} /></Row
    ><Row title="Battery"
      ><span
        >{device.observed.battery_percent == null
          ? 'Not reported'
          : `${device.observed.battery_percent}%`}</span
      ></Row
    >
  </div>
  <h3>HomeKit</h3>
  <div class="group">
    <Row title="Expose in Apple Home" subtitle="Changes only this camera’s selection"
      ><input
        aria-label={`Expose ${device.name} in Apple Home`}
        type="checkbox"
        role="switch"
        checked={device.homekit.selected}
        disabled={disabled || !selection}
        onchange={(event) => {
          event.currentTarget.checked = device.homekit.selected;
          void select(!device.homekit.selected);
        }}
      /></Row
    >
    <Row title="Published" subtitle="Advertised by the HomeKit adapter"
      ><Status
        value={fresh ? device.homekit.published : null}
        yes="Published"
        no="Not published"
      /></Row
    >
    <Row title="Paired" subtitle="Checked separately from publication"
      ><Status
        value={fresh && pair?.paired_known ? pair.paired : null}
        yes="Paired"
        no="Not paired"
      /></Row
    >
    <Row title="Pairing code" subtitle="Hidden until you confirm your Halo password"
      ><button
        disabled={disabled || !pair?.published || pair.pairing_revision == null}
        onclick={reveal}>Reveal code</button
      ></Row
    >
    <Row
      title={pair?.pending_reset ? 'Pending pairing reset' : 'Reset pairing'}
      subtitle="Removes this camera’s HomeKit pairing and briefly interrupts the adapter"
      ><button
        class="danger"
        disabled={!fresh ||
          busy ||
          loading ||
          !pair?.selected ||
          pair.pairing_revision == null ||
          (unsettled && !canResumeReset(pair, operation?.operation_id))}
        onclick={() => (confirmation = 'reset')}
        >{pair?.pending_reset ? 'Resume reset…' : 'Reset…'}</button
      ></Row
    >
  </div>
  {#if secret}<PairingCode {secret} onhide={() => (secret = null)} />{/if}
  <h3>Recording</h3>
  <div class="group">
    <Row
      title="Shared warm-source budget"
      subtitle="Reservations include starting sources and cleanup still in progress"
      ><span
        >{warmBudget &&
        Number.isInteger(warmBudget.capacity) &&
        Number.isInteger(warmBudget.reserved)
          ? `${warmBudget.reserved} of ${warmBudget.capacity} slots reserved`
          : 'Unavailable'}</span
      ></Row
    >
    <Row
      title="Apple Home recording"
      subtitle="Managed in Apple Home; separate from Halo’s warm-source preference"
      ><span>{fresh ? tri(device.homekit.recording_active, 'Enabled', 'Disabled') : 'Unknown'}</span
      ></Row
    >
    <Row title="Apple Home recording audio"
      ><span
        >{fresh
          ? tri(device.homekit.recording_audio_active, 'Enabled', 'Disabled')
          : 'Unknown'}</span
      ></Row
    >
    <Row
      title="Keep recording source warm"
      subtitle={warm?.mains_confirmed === true
        ? 'Mains power confirmed. Uses a bounded continuous Ring source while eligible.'
        : 'Enabling requires confirmed mains power and selected HomeKit access.'}
      ><input
        type="checkbox"
        role="switch"
        aria-label="Keep recording source warm"
        checked={warm?.owner_warm === true}
        disabled={disabled ||
          !warm ||
          (warm.owner_warm !== true && (warm.mains_confirmed !== true || !device.homekit.selected))}
        onchange={(event) => {
          event.currentTarget.checked = warm?.owner_warm === true;
          void setWarm(warm?.owner_warm !== true);
        }}
      /></Row
    >
  </div>
  {#if control?.has_light === true || control?.has_siren === true}<h3>Camera controls</h3>
    <div class="group">
      {#if control.has_light === true}<Row
          title="Camera light"
          subtitle={`Observed: ${fresh ? tri(device.observed.light, 'On', 'Off') : 'Unknown'}`}
          icon="light"
          ><button {disabled} onclick={() => command('light', true)}>On</button><button
            {disabled}
            onclick={() => command('light', false)}>Off</button
          ></Row
        >{/if}
      {#if control.has_siren === true}<Row
          title="Siren"
          subtitle="Ring command is bounded to 30 seconds"
          icon="bell"
          ><button class="danger" {disabled} onclick={() => command('siren', true)}
            >Sound siren…</button
          ><button disabled={!fresh || busy || loading} onclick={() => command('siren', false)}
            >Stop</button
          ></Row
        >{/if}
    </div>{/if}
  <p class="inline-note">
    Accepted commands are not proof of physical state. Stream and audio quality are tested through
    Apple Home.
  </p>
{/if}
{#if confirmation === 'reset' && device}<Confirm
    title={`Reset ${device.name}?`}
    onclose={() => (confirmation = null)}
    ><p>
      This removes only this camera’s HomeKit pairing. You will need to add it to Apple Home again.
      Other camera connections may briefly pause while the adapter resets.
    </p>
    <div class="actions end">
      <button onclick={() => (confirmation = null)}>Cancel</button><button
        class="danger"
        onclick={reset}>Confirm reset</button
      >
    </div></Confirm
  >
{:else if confirmation === 'siren' && device}<Confirm
    title={`Sound ${device.name}’s siren?`}
    onclose={() => (confirmation = null)}
    ><p>The camera siren may be loud. Halo bounds this command to 30 seconds.</p>
    <div class="actions end">
      <button onclick={() => (confirmation = null)}>Cancel</button><button
        class="danger"
        onclick={() => command('siren', true)}>Sound siren</button
      >
    </div></Confirm
  >
{:else if confirmation === 'warm'}<Confirm
    title="Keep this recording source warm?"
    onclose={() => (confirmation = null)}
    ><p>
      This may keep the mains-powered camera connected to Ring continuously when recording is
      eligible. It consumes one shared warm-source slot. Apple Home still controls whether
      recordings are enabled.
    </p>
    <div class="actions end">
      <button onclick={() => (confirmation = null)}>Cancel</button><button
        class="primary"
        onclick={() => setWarm(true)}>Enable warm source</button
      >
    </div></Confirm
  >{/if}
