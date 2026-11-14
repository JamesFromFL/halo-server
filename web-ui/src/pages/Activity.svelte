<script lang="ts">
  import type { StateEnvelope } from '../lib/types.ts';
  import { pretty, when } from '../lib/model.ts';
  let { snapshot }: { snapshot: StateEnvelope | null } = $props();
  let category = $state('');
  let device = $state('');
  const entries = $derived(
    (snapshot?.state?.activity.entries ?? [])
      .filter((v) => (!category || v.category === category) && (!device || v.camera_id === device))
      .reverse(),
  );
  const categories = $derived([
    ...new Set(snapshot?.state?.activity.entries.map((v) => v.category) ?? []),
  ]);
</script>

<div class="page-intro">
  <h2>Activity</h2>
  <p>Recent events and owner actions in the current server session.</p>
</div>
<div class="searchbar">
  <select aria-label="Filter activity type" bind:value={category}
    ><option value="">All activity</option>{#each categories as value}<option {value}
        >{pretty(value)}</option
      >{/each}</select
  ><select aria-label="Filter activity device" bind:value={device}
    ><option value="">All devices</option>{#each snapshot?.state?.devices ?? [] as value}<option
        value={value.camera_id}>{value.name}</option
      >{/each}</select
  >
</div>
{#if snapshot?.state?.activity.truncated}<div class="notice">
    Older entries have left the bounded activity history. This is not a complete archive.
  </div>{/if}
<div class="group">
  <div class="table-scroll">
    <table>
      <thead><tr><th>When</th><th>Activity</th><th>Device</th><th>Outcome</th></tr></thead><tbody
        >{#each entries as item (item.sequence)}<tr
            ><td>{when(item.at)}</td><td>{pretty(item.action)}</td><td
              >{snapshot?.state?.devices.find((d) => d.camera_id === item.camera_id)?.name ??
                (item.camera_id ? 'Device no longer available' : 'Server')}</td
            ><td>{pretty(item.outcome)}</td></tr
          >{:else}<tr><td colspan="4" class="empty">No activity matches this view.</td></tr
          >{/each}</tbody
      >
    </table>
  </div>
</div>
<p class="inline-note">
  Activity is bounded and cleared when its account scope changes. It is not a recording library or
  durable security audit.
</p>
