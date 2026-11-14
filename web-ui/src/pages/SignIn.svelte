<script lang="ts">
  import { OWNER_USERNAME, USERNAME_MAX_LENGTH } from '../lib/auth.ts';
  import { PASSWORD_MIN_LENGTH, PASSWORD_MAX_LENGTH } from '../lib/password.ts';
  import { api, errorMessage } from '../lib/api.ts';
  import type { Session } from '../lib/types.ts';
  let { session, onauthenticated }: { session: Session; onauthenticated: () => Promise<void> } =
    $props();
  let username = $state('');
  let password = $state('');
  let confirmation = $state('');
  let setupCode = $state('');
  let busy = $state(false);
  let error = $state('');
  async function submit(event: SubmitEvent) {
    event.preventDefault();
    error = '';
    if (session.setup_required && password !== confirmation) {
      error = 'The two passwords do not match.';
      return;
    }
    busy = true;
    try {
      await api.post(
        session.setup_required ? 'setup' : 'login',
        session.setup_required
          ? { setup_code: setupCode, username: OWNER_USERNAME, password }
          : { username, password },
      );
      await onauthenticated();
    } catch (e) {
      error = errorMessage(e);
    } finally {
      password = '';
      confirmation = '';
      setupCode = '';
      busy = false;
    }
  }
</script>

<main class="auth-page">
  <section class="auth-card" aria-labelledby="sign-in-title">
    <div class="brand">
      <img src="/halo.png" alt="" />
      <div><strong>Halo</strong><small>Server</small></div>
    </div>
    <h1 id="sign-in-title">{session.setup_required ? 'Set up web access' : 'Welcome back'}</h1>
    <p>
      {session.setup_required
        ? 'Use the one-time code from the Halo owner command to create your web password.'
        : 'Sign in to manage your Halo Server. This password is separate from your Ring account.'}
    </p>
    {#if error}<div class="notice error" role="alert">{error}</div>{/if}
    <form class="form" onsubmit={submit}>
      {#if session.setup_required}
        <label
          >Username<input
            name="username"
            value={OWNER_USERNAME}
            autocomplete="username"
            autocapitalize="off"
            spellcheck="false"
            readonly
          /></label
        >
        <small>Halo uses one owner account named {OWNER_USERNAME}. Choose its password below.</small
        >
      {:else}
        <label
          >Username<input
            name="username"
            bind:value={username}
            autocomplete="username"
            autocapitalize="off"
            spellcheck="false"
            maxlength={USERNAME_MAX_LENGTH}
            required
          /></label
        >
        <small>The owner username is {OWNER_USERNAME} (lowercase).</small>
      {/if}
      {#if session.setup_required}<label
          >Setup code<input
            bind:value={setupCode}
            autocomplete="off"
            required
            maxlength="256"
            spellcheck="false"
          /></label
        >{/if}
      <label
        >{session.setup_required ? 'Create password' : 'Password'}<input
          type="password"
          name="password"
          bind:value={password}
          autocomplete={session.setup_required ? 'new-password' : 'current-password'}
          required
          minlength={PASSWORD_MIN_LENGTH}
          maxlength={PASSWORD_MAX_LENGTH}
        /></label
      >
      {#if session.setup_required}<label
          >Repeat password<input
            type="password"
            name="password-confirmation"
            bind:value={confirmation}
            autocomplete="new-password"
            required
            minlength={PASSWORD_MIN_LENGTH}
            maxlength={PASSWORD_MAX_LENGTH}
          /></label
        ><small
          >Use {PASSWORD_MIN_LENGTH}–{PASSWORD_MAX_LENGTH} characters. Keep this password in your password
          manager.</small
        >{/if}
      <button class="primary" type="submit" disabled={busy}
        >{busy ? 'Please wait…' : session.setup_required ? 'Create web access' : 'Sign in'}</button
      >
    </form>
    <p class="caption">
      Lost access? Use Halo’s local owner command to reset web authentication. Ring credentials and
      HomeKit pairing are preserved.
    </p>
  </section>
</main>
