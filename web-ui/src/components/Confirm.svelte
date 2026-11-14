<script lang="ts">
  import { onMount } from 'svelte';
  import type { Snippet } from 'svelte';
  let { title, onclose, children }: { title: string; onclose: () => void; children: Snippet } =
    $props();
  let dialog: HTMLDialogElement;
  onMount(() => {
    const previous = document.activeElement;
    dialog.showModal();
    return () => {
      dialog.close();
      if (previous instanceof HTMLElement && previous.isConnected) previous.focus();
    };
  });
</script>

<dialog
  bind:this={dialog}
  aria-labelledby="confirmation-title"
  oncancel={(event) => {
    event.preventDefault();
    onclose();
  }}
>
  <h2 id="confirmation-title">{title}</h2>
  {@render children()}
</dialog>
