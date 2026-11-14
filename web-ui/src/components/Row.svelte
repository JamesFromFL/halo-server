<script lang="ts">
  import type { Snippet } from 'svelte';
  import Icon from './Icon.svelte';
  let {
    title,
    subtitle = '',
    icon = '',
    children,
  }: { title: string; subtitle?: string; icon?: string; children?: Snippet } = $props();
</script>

<div class="preference-row">
  {#if icon}<span class="row-icon"><Icon name={icon} /></span>{/if}
  <div class="row-label">
    <span>{title}</span>{#if subtitle}<small>{subtitle}</small>{/if}
  </div>
  {#if children}<div class="row-control">{@render children()}</div>{/if}
</div>
