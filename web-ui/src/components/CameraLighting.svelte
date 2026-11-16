<script lang="ts">
  import { onMount } from 'svelte';
  import Row from './Row.svelte';
  import { cameraLightingView, lightingReason, motionDuration } from '../lib/camera-light.ts';
  let {
    lighting,
    legacyLight,
    fresh,
    onDisabled,
    offDisabled,
    oncommand,
  }: {
    lighting: unknown;
    legacyLight: boolean | null;
    fresh: boolean;
    onDisabled: boolean;
    offDisabled: boolean;
    oncommand: (enabled: boolean) => void;
  } = $props();
  let now = $state(performance.now());
  let receivedAt = $state(performance.now());
  $effect(() => {
    lighting;
    const received = performance.now();
    receivedAt = received;
    now = received;
  });
  onMount(() => {
    const timer = setInterval(() => (now = performance.now()), 1000);
    return () => clearInterval(timer);
  });
  const view = $derived(
    cameraLightingView(lighting, fresh, legacyLight, Math.max(0, now - receivedAt) / 1000),
  );
</script>

<section aria-label="Camera lighting">
  <h3>Lighting</h3>
  <div class="group">
    <Row
      title="Camera light"
      subtitle={`Ring reports: ${view.light === null ? 'Unknown' : view.light ? 'On' : 'Off'}.`}
      icon="light"
      ><button disabled={onDisabled} onclick={() => oncommand(true)}>On</button><button
        disabled={offDisabled}
        onclick={() => oncommand(false)}>Off</button
      ></Row
    >
    <Row
      title="Motion shut-off time"
      subtitle="Reported by Ring. Adjust this setting in the Ring app."
      ><span>{motionDuration(view.motion_duration_seconds)}</span></Row
    >
  </div>
  <p class="inline-note">
    Ring controls motion lighting. Manual On/Off uses Ring’s commands without a Halo timer. Staying
    on until the next Off has not yet been verified.
  </p>
  {#if view.observed_age_seconds !== null}<p class="caption">
      Last Ring observation: {Math.floor(view.observed_age_seconds)} seconds ago.
    </p>{/if}
  {#if view.reason}<p class="inline-note">{lightingReason(view.reason)}</p>{/if}
</section>
