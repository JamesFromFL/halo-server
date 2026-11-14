<script lang="ts">
  let {
    value,
    yes = 'Available',
    no = 'Unavailable',
  }: { value: boolean | null | undefined; yes?: string; no?: string } = $props();
</script>

<span
  class:positive={value === true}
  class:muted={value == null}
  class:negative={value === false}
  class="status">{value == null ? 'Unknown' : value ? yes : no}</span
>
