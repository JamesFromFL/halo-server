<script lang="ts">
  import App from './App.svelte';
</script>

<svelte:boundary>
  <App />
  {#snippet failed()}
    <main class="auth-page">
      <section class="auth-card" role="alert">
        <h1>Halo could not display its interface</h1>
        <p>Your server keeps running. Reload this page to load the current interface.</p>
        <button class="primary" onclick={() => location.reload()}>Reload interface</button>
      </section>
    </main>
  {/snippet}
</svelte:boundary>
