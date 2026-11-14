<script lang="ts">
  import Row from '../components/Row.svelte';
  import Status from '../components/Status.svelte';
  import Icon from '../components/Icon.svelte';
  import { pretty, when } from '../lib/model.ts';
  import type { StateEnvelope } from '../lib/types.ts';
  let { snapshot, fresh }: { snapshot: StateEnvelope | null; fresh: boolean } = $props();
  const state = $derived(snapshot?.state);
  const events = $derived(
    typeof state?.ring.events === 'string'
      ? state.ring.events
      : (state?.ring.events?.state ?? state?.ring.events?.status),
  );
</script>

<div class="page-intro">
  <h2>Your Halo Server</h2>
  <p>Connections, devices, and anything that needs your attention.</p>
</div>
<div class="group">
  <div class="summary">
    <span class="summary-icon"><Icon name="server" size={25} /></span>
    <div>
      <strong
        >{snapshot?.web.draining
          ? 'Release maintenance is in progress'
          : fresh
            ? 'Halo is responding'
            : snapshot?.daemon.available
              ? 'Connection is being checked'
              : 'Halo daemon is unavailable'}</strong
      >
      <p class="muted">
        {snapshot?.web.draining
          ? 'The web console is read-only until local maintenance finishes. The daemon may still be running.'
          : fresh
            ? 'Management state is current. Camera playback is verified separately in Apple Home.'
            : 'The web console remains available. Last observations may be out of date.'}
      </p>
    </div>
  </div>
</div>
<h3>Connections</h3>
<div class="group">
  <Row
    title="Ring account"
    subtitle={state?.ring.restoring
      ? 'Restoring the saved Ring session'
      : 'Device and event source'}
    icon="account"
    ><Status
      value={fresh ? state?.ring.authenticated : null}
      yes="Connected"
      no="Not connected"
    /></Row
  >
  <Row title="Ring events" subtitle="Motion and doorbell delivery" icon="activity"
    ><span class="status">{fresh ? pretty(events) : 'Unknown'}</span></Row
  >
  <Row title="HomeKit" subtitle="Accessory publication" icon="integrations"
    ><span class="status">{fresh ? pretty(state?.homekit.state) : 'Unknown'}</span></Row
  >
</div>
<div class="section-head">
  <h3>Devices</h3>
  <a href="#/devices">Manage devices</a>
</div>
<div class="group">
  <Row
    title="Discovered cameras"
    subtitle={state?.ring.inventory_complete
      ? 'Ring inventory is complete'
      : 'Inventory may still be loading'}
    icon="camera"><span>{state?.devices.length ?? '—'}</span></Row
  >
  <Row title="Selected for HomeKit" subtitle="Owner selection is separate from publication"
    ><span>{state?.homekit.selected_count ?? '—'}</span></Row
  >
  <Row
    title="Published accessories"
    subtitle="Publication does not confirm pairing or stream quality"
    ><span>{fresh ? (state?.homekit.published_count ?? 'Unknown') : 'Unknown'}</span></Row
  >
</div>
{#if state?.truncated}<p class="notice">
    This summary has reached its display limit. The inventory may contain more devices.
  </p>{/if}
<div class="section-head">
  <h3>Recent activity</h3>
  <a href="#/activity">View activity</a>
</div>
<div class="group">
  {#each (state?.activity.entries ?? []).slice(-4).reverse() as item (item.sequence)}<Row
      title={pretty(item.action)}
      subtitle={when(item.at)}
      icon="activity"><span class="status">{pretty(item.outcome)}</span></Row
    >{:else}<div class="empty">No recent session activity.</div>{/each}
</div>
<p class="inline-note">
  The management dashboard does not open camera streams or request snapshots.
</p>
