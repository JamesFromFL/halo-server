<script lang="ts">
  import { onDestroy } from 'svelte';
  import { api, errorMessage } from '../lib/api.ts';
  import { pretty, when } from '../lib/model.ts';
  import type { StateEnvelope } from '../lib/types.ts';
  import Row from '../components/Row.svelte';
  import Confirm from '../components/Confirm.svelte';
  import Status from '../components/Status.svelte';
  let {
    snapshot,
    fresh,
    notify,
  }: { snapshot: StateEnvelope | null; fresh: boolean; notify: (s: string) => void } = $props();
  let report = $state<Record<string, unknown> | null>(null);
  let busy = $state(false);
  let error = $state('');
  let probe = $state(false);
  let reportAt = $state<number | null>(null);
  let serial = 0;
  let destroyed = false;
  let observedScope = '';
  const scope = $derived(JSON.stringify([snapshot?.web.boot_id, snapshot?.state?.epoch]));
  $effect(() => {
    if (observedScope !== scope) {
      observedScope = scope;
      serial++;
      report = null;
      reportAt = null;
      error = '';
      probe = false;
      busy = false;
    }
  });
  onDestroy(() => {
    destroyed = true;
    serial++;
  });
  const findings = $derived.by(() => {
    const daemon = report?.daemon;
    if (!daemon || typeof daemon !== 'object') return [];
    const local = (daemon as Record<string, unknown>).local;
    if (!local || typeof local !== 'object') return [];
    const checks = (local as Record<string, unknown>).checks;
    if (!Array.isArray(checks)) return [];
    return checks.filter(
      (item): item is { name: string; status: string; code: string } =>
        !!item &&
        typeof item.name === 'string' &&
        typeof item.status === 'string' &&
        typeof item.code === 'string',
    );
  });
  async function doctor(advanced = false) {
    if (busy) return;
    const request = ++serial;
    const expected = scope;
    busy = true;
    error = '';
    probe = false;
    try {
      const result = advanced
        ? await api.post<Record<string, unknown>>('doctor', { probe: true })
        : await api.get<Record<string, unknown>>('doctor');
      if (destroyed || request !== serial || expected !== scope) return;
      report = result;
      reportAt = Date.now() / 1000;
      notify('Health check completed.');
    } catch (e) {
      if (!destroyed && request === serial && expected === scope) error = errorMessage(e);
    } finally {
      if (!destroyed && request === serial) busy = false;
    }
  }
  async function support() {
    if (busy) return;
    const request = ++serial;
    const expected = scope;
    busy = true;
    error = '';
    try {
      const value = await api.get<unknown>('support');
      if (destroyed || request !== serial || expected !== scope) return;
      const blob = new Blob([JSON.stringify(value, null, 2) + '\n'], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = 'halo-support.json';
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      notify('Sanitized support report downloaded.');
    } catch (e) {
      if (!destroyed && request === serial && expected === scope) error = errorMessage(e);
    } finally {
      if (!destroyed && request === serial) busy = false;
    }
  }
</script>

<div class="page-intro">
  <h2>Diagnostics</h2>
  <p>Check local components and collect a useful support report.</p>
</div>
{#if error}<div class="notice error" role="alert">{error}</div>{/if}
<h3>Observed state</h3>
<div class="group">
  <Row title="Daemon connection" subtitle={when(snapshot?.daemon.observed_at)}
    ><span>{fresh ? 'Available' : 'Unavailable or stale'}</span></Row
  ><Row title="Software version"
    ><span>{snapshot?.state?.daemon.version ?? 'Unavailable'}</span></Row
  ><Row title="Release" subtitle="Selected immutable release"
    ><span class="wrap caption"
      >{snapshot?.state?.daemon.release.release_id ?? 'Unmanaged / unavailable'}</span
    ></Row
  >
</div>
<h3>Health checks</h3>
<div class="group">
  <Row title="Halo Doctor" subtitle="Read-only component checks; no camera playback"
    ><button disabled={busy} onclick={() => doctor()}>Run check</button></Row
  ><Row title="Advanced probes" subtitle="Deliberate dependency probes may take longer"
    ><button disabled={busy} onclick={() => (probe = true)}>Run probes…</button></Row
  ><Row title="Support report" subtitle="Bounded, sanitized diagnostics. No raw logs or media."
    ><button disabled={busy} onclick={support}>Download</button></Row
  >
</div>
{#if busy}<p class="inline-note" role="status">Collecting diagnostics…</p>{/if}
{#if report}<h3>Doctor findings</h3>
  <p class="inline-note">Completed {when(reportAt)}. Run another check to update this report.</p>
  <div class="group">
    <Row
      title="Web companion"
      subtitle="Local management remains available independently of the daemon"
      ><Status
        value={(report.web as Record<string, unknown> | undefined)?.ready === true}
        yes="Ready"
        no="Not ready"
      /></Row
    >
    <Row title="Daemon diagnostics"
      ><Status
        value={typeof report.daemon_available === 'boolean' ? report.daemon_available : null}
        yes="Available"
        no="Unavailable"
      /></Row
    >
    {#each findings as item}
      <Row title={pretty(item.name)} subtitle={pretty(item.code)}
        ><Status
          value={item.status === 'pass' ? true : item.status === 'fail' ? false : null}
          yes="Pass"
          no="Needs attention"
        /></Row
      >
    {/each}
    <details class="form-section">
      <summary>Technical details</summary>
      <pre class="report">{JSON.stringify(report, null, 2)}</pre>
    </details>
  </div>{/if}
{#if probe}<Confirm title="Run advanced health probes?" onclose={() => (probe = false)}
    ><p>
      Halo will actively check local dependencies. This does not request a camera stream. Normal
      camera operation continues.
    </p>
    <div class="actions end">
      <button onclick={() => (probe = false)}>Cancel</button><button
        class="primary"
        onclick={() => doctor(true)}>Run probes</button
      >
    </div></Confirm
  >{/if}
