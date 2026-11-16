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
  import { recordingEligibility, recordingReadiness, recordingSources } from '../lib/recording.ts';
  import { cameraControl } from '../lib/camera-light.ts';
  import {
    cameraPrivacy,
    privacyAccess,
    privacyBody,
    privacyReason,
    validPrivacyPage,
  } from '../lib/privacy.ts';
  import type {
    StateEnvelope,
    CameraPage,
    PairingCamera,
    PairingSecret,
    Operation,
    CameraPrivacyMode,
    CameraPrivacyPage,
    CameraControlPage,
  } from '../lib/types.ts';
  import Row from '../components/Row.svelte';
  import Status from '../components/Status.svelte';
  import Icon from '../components/Icon.svelte';
  import Confirm from '../components/Confirm.svelte';
  import PairingCode from '../components/PairingCode.svelte';
  import OperationStatus from '../components/OperationStatus.svelte';
  import NoncameraDevices from '../components/NoncameraDevices.svelte';
  import CameraLighting from '../components/CameraLighting.svelte';
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
  let controls = $state<CameraControlPage | null>(null);
  let privacy = $state<CameraPrivacyPage | null>(null);
  let secret = $state<PairingSecret | null>(null);
  let error = $state('');
  let loading = $state(false);
  let busy = $state(false);
  let confirmation = $state<'reset' | 'siren' | 'warm' | 'privacy' | null>(null);
  let operation = $state<Operation | null>(null);
  let controlOperation = $state('');
  let serial = 0;
  let destroyed = false;
  let operationScope = '';
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
  const alreadyPaired = $derived(fresh && pair?.paired_known === true && pair.paired === true);
  const canReveal = $derived(
    pair?.paired_known === true &&
      pair.paired === false &&
      pair.published === true &&
      pair.pairing_revision != null &&
      !pair.pending_reset,
  );
  const warm = $derived(recording?.cameras.find((d) => d.camera_id === device?.camera_id));
  const eligibility = $derived(
    recordingEligibility(fresh, device?.homekit.selected === true, recording?.available, warm),
  );
  const batteryCapable = $derived(device?.capabilities.includes('battery') === true);
  const control = $derived(cameraControl(controls, device));
  const privacyCamera = $derived(cameraPrivacy(privacy, device));
  const privacyFresh = $derived(fresh && privacyCamera?.source_fresh === true);
  const privacyEffectiveFresh = $derived(fresh && privacy?.available === true && !!privacyCamera);
  const unsettled = $derived(operation?.status === 'unknown' || operation?.status === 'pending');
  const disabled = $derived(!fresh || busy || loading || unsettled);
  $effect(() => {
    const key = cameraKey;
    const epoch = generation;
    const scope = JSON.stringify([key, epoch]);
    if (operationScope !== scope) {
      operation = null;
      controlOperation = '';
      confirmation = null;
      operationScope = scope;
    }
    if (key && epoch) void untrack(load);
    else {
      serial++;
      secret = null;
      pairing = null;
      selection = null;
      recording = null;
      controls = null;
      privacy = null;
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
  $effect(() => {
    if (!canReveal) secret = null;
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
    privacy = null;
    loading = true;
    error = '';
    const values = await Promise.allSettled([
      api.get<CameraPage>('selection'),
      api.get<CameraPage<PairingCamera>>('pairing'),
      api.get<CameraPage>('recording'),
      api.get<CameraControlPage>('controls'),
      api.get<unknown>('privacy'),
    ]);
    if (destroyed || token !== serial) return;
    if (values[0].status === 'fulfilled') selection = values[0].value;
    if (values[1].status === 'fulfilled') pairing = values[1].value;
    if (values[2].status === 'fulfilled') recording = values[2].value;
    if (values[3].status === 'fulfilled') controls = values[3].value;
    if (values[4].status === 'fulfilled') {
      if (validPrivacyPage(values[4].value)) privacy = values[4].value;
      else
        error =
          'Halo returned incomplete camera privacy information. Refresh this device before saving privacy changes.';
    }
    const failure = values.find((v) => v.status === 'rejected');
    if (failure?.status === 'rejected') error = errorMessage(failure.reason);
    loading = false;
  }
  async function mutate(path: string, body: Record<string, unknown>, id: string) {
    busy = true;
    error = '';
    secret = null;
    controlOperation = path === 'controls' ? id : '';
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
      } else if (path === 'controls') {
        await refresh();
        await load();
      } else if (path === 'pairing/reset' && operation.status === 'pending') await load();
    } catch (e) {
      if (destroyed || target !== deviceId || expected !== generation) return;
      error = errorMessage(e);
      if (e instanceof ApiError && e.possiblySent) {
        operation = { operation_id: id, status: 'unknown', possibly_sent: true };
        if (path === 'controls') {
          await refresh();
          await load();
        }
      }
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
    if (!pairing || !pair || disabled || !canReveal) return;
    const expected = generation;
    const target = deviceId;
    const requestSerial = serial;
    const requestedScope = { generation: expected, deviceId: target, serial: requestSerial };
    if (
      !(await reauthenticate()) ||
      destroyed ||
      !samePairingScope(requestedScope, { generation, deviceId, serial }) ||
      !fresh ||
      !canReveal
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
        canReveal &&
        device?.homekit.selected
      )
        secret = value;
    } catch (e) {
      if (!destroyed && samePairingScope(requestedScope, { generation, deviceId, serial }))
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
      (unsettled && !(!enabled && (kind === 'siren' || operation?.status !== 'pending')))
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
    if (!device || !recording || disabled || (enabled && !eligibility.eligible)) return;
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
  async function setPrivacy(mode: CameraPrivacyMode, confirmed = false) {
    if (!device || !privacy || !privacyCamera || disabled || !privacy.available) return;
    if (mode === 'homekit' && !confirmed) {
      confirmation = 'privacy';
      return;
    }
    const requestedScope = { generation, deviceId, serial };
    const body = privacyBody(privacy, device, mode);
    confirmation = null;
    busy = true;
    try {
      if (
        !(await reauthenticate()) ||
        destroyed ||
        !samePairingScope(requestedScope, { generation, deviceId, serial }) ||
        !fresh
      )
        return;
      await mutate('privacy', body, operationId());
    } catch (e) {
      if (!destroyed && samePairingScope(requestedScope, { generation, deviceId, serial }))
        error = errorMessage(e);
    } finally {
      busy = false;
    }
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
        {!snapshot?.state
          ? 'Camera inventory is unavailable while Halo reconnects. Saved cameras have not been removed.'
          : search || filter
            ? 'No cameras match your search or filters.'
            : 'No cameras are currently discovered. Check the Ring connection in Settings.'}
      </div>{/each}
  </div>
  {#if search || filter}<div class="actions">
      <button
        onclick={() => {
          search = '';
          filter = '';
        }}>Clear camera search and filters</button
      >
    </div>{/if}
  <p class="inline-note">
    Historical HomeKit accessories remain in <a href="#/integrations">Integrations</a> if a source device
    is temporarily missing.
  </p>
  <NoncameraDevices
    monitoring={noncamera}
    inventoryAvailable={!!snapshot?.state}
    {search}
    {fresh}
    {generation}
    {reauthenticate}
    {notify}
    {refresh}
  />
{:else if otherDevice}
  <NoncameraDevices
    monitoring={noncamera}
    device={otherDevice}
    {fresh}
    {generation}
    {reauthenticate}
    {notify}
    {refresh}
  />
{:else if !device}<a href="#/devices">← Devices</a>
  <div class="empty">
    <h2>
      {snapshot?.state ? 'Device is no longer available' : 'Device information is unavailable'}
    </h2>
    <p>
      {snapshot?.state
        ? 'Its account or inventory scope changed. Refresh the device list.'
        : 'Halo is reconnecting. This does not mean the device or its pairing was removed.'}
    </p>
    <button onclick={refresh}>Refresh device list</button>
  </div>
{:else}
  <div class="detail-heading">
    <a href="#/devices" aria-label="Back to devices"><Icon name="back" /></a>
    <div>
      <h2>{device.name}</h2>
      <p class="muted">{device.model || 'Ring camera'}</p>
    </div>
  </div>
  <div class="actions">
    <button onclick={load} disabled={busy || loading}>
      {loading ? 'Refreshing details…' : 'Refresh camera details'}
    </button>
  </div>
  {#if error}<div class="notice error" role="alert">
      {error}
      <div class="actions"><button onclick={load} disabled={busy}>Refresh details</button></div>
    </div>{/if}
  {#if loading}<p role="status" class="inline-note">Loading device administration…</p>{/if}
  {#if operation}<OperationStatus
      {operation}
      onupdate={(value) => {
        if (!operation?.operation_id || value.operation_id !== operation.operation_id) return;
        operation = value;
        if (
          value.status === 'completed' ||
          (value.operation_id === controlOperation && value.status !== 'pending')
        ) {
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
        >{!fresh
          ? 'Unknown'
          : device.observed.battery_percent == null
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
    <Row
      title="Pairing code"
      subtitle={alreadyPaired
        ? 'No code is needed for this Home. To move to another Home, remove this camera from its current Apple Home first; reset here only if pairing remains.'
        : !fresh || pair?.paired_known !== true
          ? 'Pairing status is unknown. Refresh this device before requesting a code.'
          : 'Hidden until you confirm your Halo password'}
    >
      {#if alreadyPaired}<span>Already paired</span>
      {:else}<button disabled={disabled || !canReveal} onclick={reveal}>Reveal code</button>
        {#if fresh && pair?.paired_known !== true}<button disabled={busy || loading} onclick={load}
            >Refresh pairing status</button
          >{/if}
      {/if}
    </Row>
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
  <h3>Privacy</h3>
  <div class="group">
    <Row
      title="Use Apple Home privacy controls"
      subtitle="Default: follow Ring privacy. This per-camera choice controls how Halo applies Ring’s reported privacy settings."
      ><input
        type="checkbox"
        role="switch"
        aria-label="Use Apple Home privacy controls"
        checked={privacyCamera?.mode === 'homekit'}
        disabled={disabled || !privacyCamera || privacy?.available !== true}
        onchange={(event) => {
          event.currentTarget.checked = privacyCamera?.mode === 'homekit';
          void setPrivacy(privacyCamera?.mode === 'homekit' ? 'ring' : 'homekit');
        }}
      /></Row
    >
    <Row title="Saved privacy choice"
      ><span
        >{privacyCamera?.mode === 'homekit'
          ? 'Apple Home'
          : privacyCamera?.mode === 'ring'
            ? 'Follow Ring'
            : 'Unavailable'}</span
      ></Row
    >
    <Row
      title="Live View source access"
      subtitle={`Ring reports Live View: ${privacyAccess(privacyFresh, privacyCamera?.ring_live_allowed)}.`}
      ><span>{privacyAccess(privacyEffectiveFresh, privacyCamera?.effective_live_allowed)}</span
      ></Row
    >
    <Row
      title="Recording source access"
      subtitle={`Ring permits recording source: ${privacyAccess(privacyFresh, privacyCamera?.ring_recording_allowed)}. Apple Home recording preferences still apply.`}
      ><span
        >{privacyAccess(privacyEffectiveFresh, privacyCamera?.effective_recording_allowed)}</span
      ></Row
    >
    <Row
      title="Snapshot source access"
      subtitle={`Ring permits snapshot source: ${privacyAccess(privacyFresh, privacyCamera?.ring_snapshot_allowed)}.`}
      ><span>{privacyAccess(privacyEffectiveFresh, privacyCamera?.effective_snapshot_allowed)}</span
      ></Row
    >
    <Row
      title="Audio capture source access"
      subtitle={`Ring reports audio recording: ${privacyAccess(privacyFresh, privacyCamera?.ring_audio_allowed)}. Apple Home microphone preferences still apply.`}
      ><span>{privacyAccess(privacyEffectiveFresh, privacyCamera?.effective_audio_allowed)}</span
      ></Row
    >
  </div>
  {#if privacyCamera?.reason}<p class="inline-note">{privacyReason(privacyCamera.reason)}</p>{/if}
  <p class="inline-note">
    Following Ring privacy blocks background capture and snapshots when motion recording is off;
    manual Live View is checked separately. Source access does not mean a stream or recording is
    active. Halo cannot override restrictions enforced by Ring or change Ring cloud recording. Ring
    mode changes take effect after the next source refresh. Following Ring privacy blocks source
    access while that information is stale or unknown. These permissions were reported at the last
    camera details refresh; refresh the details to check for changes.
  </p>
  <h3>Recording</h3>
  <div class="group">
    <Row
      title="Recording sources"
      subtitle="Reported at the last device refresh. Includes starting sources and cleanup still in progress. Server and network capacity still apply."
      ><span>{recordingSources(fresh, recording?.warm_budget)}</span></Row
    >
    <Row title="Recording eligibility" subtitle={eligibility.detail}
      ><span>{eligibility.label}</span></Row
    >
    <Row
      title="Enable HomeKit Secure Video support"
      subtitle="Makes recording available to Apple Home. May keep a continuous Ring source for pre-event footage when recording is requested."
      ><input
        type="checkbox"
        role="switch"
        aria-label="Enable HomeKit Secure Video support"
        checked={warm?.owner_warm === true}
        disabled={disabled || !warm || (warm.owner_warm !== true && !eligibility.eligible)}
        onchange={(event) => {
          event.currentTarget.checked = warm?.owner_warm === true;
          void setWarm(warm?.owner_warm !== true);
        }}
      /></Row
    >
    <Row
      title="Pre-event source"
      subtitle="Reported at the last device refresh. Buffer readiness does not confirm that Apple Home saved an event."
      ><span>{recordingReadiness(fresh, warm?.warm_state)}</span></Row
    >
    <Row
      title="Apple Home recording"
      subtitle="Choose recording options in Apple Home after enabling HomeKit Secure Video support here."
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
  </div>
  {#if control?.has_light === true}<CameraLighting
      lighting={control.lighting}
      legacyLight={device.observed.light}
      {fresh}
      onDisabled={disabled}
      offDisabled={!fresh || busy || loading || operation?.status === 'pending'}
      oncommand={(enabled) => void command('light', enabled)}
    />{/if}
  {#if control?.has_siren === true}<h3>Camera controls</h3>
    <div class="group">
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
{:else if confirmation === 'privacy'}<Confirm
    title="Use Apple Home privacy controls?"
    onclose={() => (confirmation = null)}
    ><p>
      In the Ring app, allow Live View and enable motion detection/recording for this camera in
      every mode you intend to use. Allow Ring audio recording if you want audio in Apple Home. Then
      choose when to stream or record in Apple Home. Halo cannot override restrictions enforced by
      Ring or change Ring cloud recording.
    </p>
    <p>
      This changes only this camera’s saved privacy choice. Your Halo password is required to save.
    </p>
    <div class="actions end">
      <button onclick={() => (confirmation = null)}>Cancel</button><button
        class="primary"
        onclick={() => setPrivacy('homekit', true)}>Continue</button
      >
    </div></Confirm
  >
{:else if confirmation === 'warm'}<Confirm
    title="Enable HomeKit Secure Video support?"
    onclose={() => (confirmation = null)}
    ><p>
      This makes recording available to Apple Home for this camera. It may keep the camera connected
      to Ring continuously for pre-event footage when recording is requested. Choose whether to
      record and include audio separately in Apple Home.
    </p>
    <p>
      {batteryCapable
        ? 'This camera supports battery power. Continuous use can drain its battery more quickly, including with wired or solar assistance.'
        : 'For battery-powered cameras, continuous use can drain the battery more quickly, including with wired or solar assistance.'}
    </p>
    <div class="actions end">
      <button onclick={() => (confirmation = null)}>Cancel</button><button
        class="primary"
        onclick={() => setWarm(true)}>Enable HKSV support</button
      >
    </div></Confirm
  >{/if}
