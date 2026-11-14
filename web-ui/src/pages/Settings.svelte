<script lang="ts">
  import { OWNER_USERNAME } from '../lib/auth.ts';
  import { PASSWORD_MIN_LENGTH, PASSWORD_MAX_LENGTH } from '../lib/password.ts';
  import { onMount, onDestroy } from 'svelte';
  import { api, ApiError, errorMessage, operationId } from '../lib/api.ts';
  import { configurationBody, pretty, readAppearance, saveAppearance, when } from '../lib/model.ts';
  import type {
    Configuration,
    ConfigurationView,
    Operation,
    RingChallenge,
    Session,
    StateEnvelope,
  } from '../lib/types.ts';
  import Row from '../components/Row.svelte';
  import Confirm from '../components/Confirm.svelte';
  import OperationStatus from '../components/OperationStatus.svelte';
  let {
    snapshot,
    fresh,
    session,
    reauthenticate,
    notify,
    refresh,
    logout,
    setNavigationGuard,
  }: {
    snapshot: StateEnvelope | null;
    fresh: boolean;
    session: Session;
    reauthenticate: () => Promise<boolean>;
    notify: (s: string) => void;
    refresh: () => Promise<void>;
    logout: () => Promise<void>;
    setNavigationGuard: (guard: (() => boolean) | null) => void;
  } = $props();
  let appearance = $state(readAppearance());
  let configuration = $state<ConfigurationView | null>(null);
  let draft = $state<Configuration | null>(null);
  let access = $state<Record<string, unknown> | null>(null);
  let restart = $state<{
    available: boolean;
    code: string | null;
    selection_revision: number | null;
  } | null>(null);
  let sessions = $state<{ session_id: string; current: boolean; created_at: number }[]>([]);
  let operation = $state<Operation | null>(null);
  let restartOperation = $state<Operation | null>(null);
  let receipts = $state<Operation[]>([]);
  let error = $state('');
  let loading = $state(false);
  let busy = $state(false);
  let confirmation = $state<'restart' | 'ring-logout' | 'discard' | null>(null);
  let now = $state(Date.now());
  let email = $state('');
  let ringPassword = $state('');
  let otp = $state('');
  let challenge = $state<RingChallenge | null>(null);
  let showRing = $state(false);
  let currentPassword = $state('');
  let newPassword = $state('');
  let repeatPassword = $state('');
  let showPassword = $state(false);
  let destroyed = false;
  const dirty = $derived(
    !!draft && !!configuration && JSON.stringify(draft) !== JSON.stringify(configuration.saved),
  );
  const unsettled = $derived(operation?.status === 'pending' || operation?.status === 'unknown');
  const challengeRemaining = $derived(
    challenge?.expires_at ? Math.max(0, Math.ceil(challenge.expires_at - now / 1000)) : 0,
  );
  let accountEpoch: string | null = null;
  $effect(() => {
    const epoch = snapshot?.state?.epoch ?? null;
    if (accountEpoch !== null && accountEpoch !== epoch) void cancelChallenge();
    accountEpoch = epoch;
  });
  $effect(() => {
    if (challenge && challengeRemaining === 0) void cancelChallenge();
  });
  onMount(() => {
    void load();
    const timer = setInterval(() => (now = Date.now()), 1000);
    setNavigationGuard(
      () => !dirty || window.confirm('Discard your unsaved server settings and leave this page?'),
    );
    const unload = (e: BeforeUnloadEvent) => {
      if (dirty) {
        e.preventDefault();
        e.returnValue = '';
      }
    };
    const visibility = () => {
      if (document.hidden) {
        void cancelChallenge();
        ringPassword = '';
        currentPassword = '';
        newPassword = '';
        repeatPassword = '';
      }
    };
    document.addEventListener('visibilitychange', visibility);
    window.addEventListener('beforeunload', unload);
    return () => {
      clearInterval(timer);
      window.removeEventListener('beforeunload', unload);
      document.removeEventListener('visibilitychange', visibility);
      setNavigationGuard(null);
    };
  });
  onDestroy(() => {
    destroyed = true;
    void cancelChallenge();
    ringPassword = '';
    otp = '';
    currentPassword = '';
    newPassword = '';
    repeatPassword = '';
  });
  async function load() {
    loading = true;
    const results = await Promise.allSettled([
      api.get<ConfigurationView>('configuration'),
      api.get<Record<string, unknown>>('access'),
      api.get<{ available: boolean; code: string | null; selection_revision: number | null }>(
        'restart',
      ),
      api.get<{ sessions: typeof sessions }>('sessions'),
      api.get<{ operations: Operation[] }>('operations'),
    ]);
    if (destroyed) return;
    if (results[0].status === 'fulfilled') {
      configuration = results[0].value;
      draft = { ...results[0].value.saved };
    }
    if (results[1].status === 'fulfilled') access = results[1].value;
    if (results[2].status === 'fulfilled') restart = results[2].value;
    if (results[3].status === 'fulfilled') sessions = results[3].value.sessions;
    if (results[4].status === 'fulfilled') receipts = results[4].value.operations;
    const failure = results.find((r) => r.status === 'rejected');
    if (failure?.status === 'rejected') error = errorMessage(failure.reason);
    loading = false;
  }
  async function recoverReceipt(id: string) {
    busy = true;
    error = '';
    try {
      operation = await api.get<Operation>(`operations/${id}`);
    } catch (e) {
      error = errorMessage(e);
    } finally {
      busy = false;
    }
  }
  function togglePassword() {
    showPassword = !showPassword;
    if (!showPassword) {
      currentPassword = '';
      newPassword = '';
      repeatPassword = '';
    }
  }
  async function signOut() {
    if (!dirty || window.confirm('Discard your unsaved server settings and sign out?'))
      await logout();
  }
  function changeAppearance() {
    document.documentElement.dataset.theme = appearance;
    saveAppearance(appearance);
  }
  async function save(event: SubmitEvent) {
    event.preventDefault();
    if (!configuration || !draft || !fresh || busy || unsettled) return;
    busy = true;
    error = '';
    const id = operationId();
    try {
      operation = await api.post<Operation>(
        'configuration',
        configurationBody(configuration, draft, id),
      );
      if (operation.status === 'rejected') {
        error =
          operation.code === 'configuration_conflict'
            ? 'The saved settings changed in another session. Reload them before saving again.'
            : `Settings were not saved: ${pretty(operation.code ?? 'request rejected')}.`;
      }
      if (operation.status === 'completed') {
        const value = operation.result as ConfigurationView;
        if (value?.saved && value?.applied) {
          configuration = value;
          draft = { ...value.saved };
        }
        notify('Settings saved. Restart separately when ready.');
      }
    } catch (e) {
      error = errorMessage(e);
      if (e instanceof ApiError && e.possiblySent)
        operation = { operation_id: id, status: 'unknown', possibly_sent: true };
    } finally {
      busy = false;
    }
  }
  async function ringLogin(event: SubmitEvent) {
    event.preventDefault();
    if (busy || !fresh) return;
    busy = true;
    error = '';
    try {
      const value = await api.post<RingChallenge>('ring/login', {
        email,
        password: ringPassword,
        epoch: snapshot?.state?.epoch,
      });
      if (destroyed) {
        if (value.challenge_id)
          void api.delete(`ring/challenge/${value.challenge_id}`).catch(() => {});
        return;
      }
      if (value.status === 'mfa_required') challenge = value;
      else {
        challenge = null;
        showRing = false;
        email = '';
        notify('Ring account connected.');
        await refresh();
      }
    } catch (e) {
      error = errorMessage(e);
    } finally {
      ringPassword = '';
      busy = false;
    }
  }
  async function verifyOtp(event: SubmitEvent) {
    event.preventDefault();
    if (!challenge?.challenge_id || busy) return;
    busy = true;
    error = '';
    try {
      const value = await api.post<RingChallenge>('ring/challenge', {
        challenge_id: challenge.challenge_id,
        otp_code: otp,
      });
      if (value.status === 'connected') {
        challenge = null;
        showRing = false;
        email = '';
        notify('Ring account connected.');
        await refresh();
      } else challenge = value;
    } catch (e) {
      error = errorMessage(e);
    } finally {
      otp = '';
      busy = false;
    }
  }
  async function cancelChallenge() {
    const id = challenge?.challenge_id;
    challenge = null;
    ringPassword = '';
    otp = '';
    if (id) {
      try {
        await api.delete(`ring/challenge/${id}`);
      } catch (e) {
        if (!destroyed) error = errorMessage(e);
      }
    }
  }
  async function ringLogout() {
    confirmation = null;
    busy = true;
    error = '';
    const id = operationId();
    try {
      operation = await api.post<Operation>('ring/logout', {
        epoch: snapshot?.state?.epoch,
        operation_id: id,
      });
      await refresh();
    } catch (e) {
      error = errorMessage(e);
      if (e instanceof ApiError && e.possiblySent)
        operation = { status: 'unknown', operation_id: id, possibly_sent: true };
    } finally {
      busy = false;
    }
  }
  async function password(event: SubmitEvent) {
    event.preventDefault();
    if (newPassword !== repeatPassword) {
      error = 'The new passwords do not match.';
      return;
    }
    busy = true;
    error = '';
    try {
      await api.post('password', { current_password: currentPassword, password: newPassword });
      await api.session();
      showPassword = false;
      notify('Web password changed. Other sessions are revoked.');
      sessions = (await api.get<{ sessions: typeof sessions }>('sessions')).sessions;
    } catch (e) {
      error = errorMessage(e);
    } finally {
      busy = false;
      currentPassword = '';
      newPassword = '';
      repeatPassword = '';
    }
  }
  async function revoke(id: string) {
    if (!(await reauthenticate()) || destroyed) return;
    busy = true;
    error = '';
    try {
      await api.delete(`sessions/${id}`);
      sessions = (await api.get<{ sessions: typeof sessions }>('sessions')).sessions;
      notify('Session revoked.');
    } catch (e) {
      error = errorMessage(e);
    } finally {
      busy = false;
    }
  }
  async function restartDaemon() {
    if (!restart?.available || restart.selection_revision == null) return;
    confirmation = null;
    busy = true;
    error = '';
    const id = operationId();
    try {
      restartOperation = await api.post<Operation>('restart', {
        operation_id: id,
        expected_revision: restart.selection_revision,
      });
    } catch (e) {
      error = errorMessage(e);
      if (e instanceof ApiError && e.possiblySent)
        restartOperation = { operation_id: id, status: 'unknown' };
    } finally {
      busy = false;
    }
  }
  async function checkRestart() {
    if (!restartOperation?.operation_id) return;
    busy = true;
    try {
      restartOperation = await api.get<Operation>(`restart/${restartOperation.operation_id}`);
      if (restartOperation.status === 'completed') {
        notify('Halo restarted and its identity was verified.');
        await refresh();
      }
    } catch (e) {
      error = errorMessage(e);
    } finally {
      busy = false;
    }
  }
</script>

<div class="page-intro">
  <h2>Settings</h2>
  <p>Appearance, account access, and server preferences.</p>
</div>
{#if error}<div class="notice error" role="alert">
    {error}{#if dirty}<p>
        Your unsaved settings are preserved. Review them or explicitly reload the saved version.
      </p>{/if}
  </div>{/if}
{#if loading}<p class="inline-note" role="status">Loading settings…</p>{/if}
{#if operation}<OperationStatus
    {operation}
    onupdate={(v) => {
      operation = v;
    }}
  />{/if}
<h3>Appearance</h3>
<div class="group">
  <Row title="Color scheme" subtitle="Follows your device unless you choose an appearance"
    ><select aria-label="Color scheme" bind:value={appearance} onchange={changeAppearance}
      ><option value="system">System</option><option value="light">Light</option><option
        value="dark">Dark</option
      ></select
    ></Row
  >
</div>
<h3>Ring account</h3>
<div class="group">
  <Row
    title="Account connection"
    subtitle={snapshot?.state?.ring.restoring
      ? 'Restoring saved session'
      : 'Existing Ring credentials remain in the Halo daemon'}
    ><span
      >{fresh
        ? snapshot?.state?.ring.authenticated
          ? 'Connected'
          : 'Not connected'
        : 'Unknown'}</span
    ></Row
  ><Row title="Manage Ring access" subtitle="Ring credentials are never saved in this browser"
    ><button
      disabled={busy || !fresh}
      onclick={() => {
        showRing = !showRing;
        if (!showRing) void cancelChallenge();
      }}>Connect account…</button
    >{#if snapshot?.state?.ring.authenticated}<button
        class="danger"
        disabled={busy || !fresh || unsettled}
        onclick={() => (confirmation = 'ring-logout')}>Disconnect…</button
      >{/if}</Row
  >
</div>
{#if showRing}<section class="group form-section">
    {#if challenge}<form class="form" onsubmit={verifyOtp}>
        <h3>Ring verification</h3>
        <p>
          Enter the verification code from Ring. This request expires in {challengeRemaining} seconds.
        </p>
        <label
          >Verification code<input
            bind:value={otp}
            inputmode="numeric"
            autocomplete="one-time-code"
            required
            maxlength="32"
          /></label
        >
        <div class="actions">
          <button type="button" disabled={busy} onclick={cancelChallenge}
            >Cancel verification</button
          ><button class="primary" disabled={busy || !challengeRemaining}>Verify</button>
        </div>
      </form>
    {:else}<form class="form" onsubmit={ringLogin}>
        <label
          >Ring email<input
            type="email"
            bind:value={email}
            autocomplete="username"
            required
            maxlength="254"
          /></label
        ><label
          >Ring password<input
            type="password"
            bind:value={ringPassword}
            autocomplete="current-password"
            required
            maxlength="1024"
          /></label
        ><button class="primary" disabled={busy || !fresh}
          >{busy ? 'Connecting…' : 'Connect Ring'}</button
        >
      </form>{/if}
  </section>{/if}
<h3>Web access</h3>
<div class="group">
  <Row title="Owner username" subtitle={OWNER_USERNAME} />
  <Row title="Halo password" subtitle="Separate from your Ring account"
    ><button disabled={busy} onclick={togglePassword}>Change password…</button></Row
  ><Row title="This browser" subtitle="Sign out of Halo web management"
    ><button disabled={busy} onclick={signOut}>Sign out</button></Row
  >
</div>
{#if showPassword}<form class="group form-section form" onsubmit={password}>
    <label
      >Username<input
        name="username"
        value={OWNER_USERNAME}
        autocomplete="username"
        readonly
      /></label
    >
    <label
      >Current Halo password<input
        type="password"
        bind:value={currentPassword}
        autocomplete="current-password"
        minlength={PASSWORD_MIN_LENGTH}
        required
        maxlength={PASSWORD_MAX_LENGTH}
      /></label
    ><label
      >New Halo password<input
        type="password"
        bind:value={newPassword}
        autocomplete="new-password"
        required
        minlength={PASSWORD_MIN_LENGTH}
        maxlength={PASSWORD_MAX_LENGTH}
      /></label
    ><label
      >Repeat new password<input
        type="password"
        bind:value={repeatPassword}
        autocomplete="new-password"
        required
        minlength={PASSWORD_MIN_LENGTH}
        maxlength={PASSWORD_MAX_LENGTH}
      /></label
    >
    <p class="caption muted">
      Use {PASSWORD_MIN_LENGTH}–{PASSWORD_MAX_LENGTH} characters. Changing the password revokes other
      signed-in sessions.
    </p>
    <button class="primary" disabled={busy}>Change password</button>
  </form>{/if}
<h3>Signed-in sessions</h3>
<div class="group">
  {#each sessions as row (row.session_id)}<Row
      title={row.current ? 'This browser' : 'Signed-in browser'}
      subtitle={`Created ${when(row.created_at)}`}
      >{#if !row.current}<button disabled={busy} onclick={() => revoke(row.session_id)}
          >Revoke</button
        >{/if}</Row
    >{:else}<div class="empty">No session details are available.</div>{/each}
</div>
<h3>Server settings</h3>
{#if configuration?.restart_required}<div class="notice">
    Saved settings differ from the running server. Restart Halo when you are ready to apply them.
  </div>{/if}
{#if configuration && draft}<form onsubmit={save}>
    <div class="group">
      <Row
        title="Operating mode"
        subtitle={`Running: ${pretty(configuration.applied.mode)} · Saved revision ${configuration.saved.revision}`}
        ><select aria-label="Operating mode" bind:value={draft.mode} disabled={busy}
          ><option value="homekit">HomeKit</option><option value="ring-only">Ring only</option
          >{#if configuration.saved.mode === 'compatibility' || configuration.saved.mode === 'dual'}<option
              value="compatibility">Compatibility</option
            ><option value="dual">HomeKit + compatibility</option>{/if}</select
        ></Row
      >
      {#if draft.mode === 'compatibility' || draft.mode === 'dual'}<div class="form-grid">
          <label
            >Plugin loopback host<input
              bind:value={draft.plugin_host}
              required
              maxlength="255"
            /></label
          ><label
            >Plugin port<input
              type="number"
              min="0"
              max="65535"
              bind:value={draft.plugin_port}
              required
            /></label
          ><label
            >RTSP loopback host<input
              bind:value={draft.rtsp_host}
              required
              maxlength="255"
            /></label
          ><label
            >RTSP port<input
              type="number"
              min="0"
              max="65535"
              bind:value={draft.rtsp_port}
              required
            /></label
          >
        </div>{/if}
    </div>
    <div class="actions">
      <button class="primary" disabled={!dirty || busy || !fresh || unsettled}>Save settings</button
      ><button
        type="button"
        disabled={busy}
        onclick={() => {
          if (dirty) confirmation = 'discard';
          else void load();
        }}>Reload saved settings</button
      >{#if dirty}<span class="status warning">Unsaved changes</span>{/if}
    </div>
  </form>{:else}<div class="notice">
    Server settings are unavailable while the owner connection is down.
  </div>{/if}
<h3>Recovery</h3>
<div class="group">
  <Row
    title="Restart Halo"
    subtitle={restart?.available
      ? 'Temporarily interrupts camera connections; this web console stays available'
      : `Unavailable: ${pretty(restart?.code ?? 'managed installation could not be verified')}`}
    ><button
      disabled={busy ||
        !restart?.available ||
        restartOperation?.status === 'pending' ||
        restartOperation?.status === 'unknown'}
      onclick={() => (confirmation = 'restart')}>Restart…</button
    ></Row
  >
</div>
{#if restartOperation}<div class="notice" role="status">
    <strong>Restart: {pretty(restartOperation.status)}</strong>
    <p>
      {restartOperation.status === 'unknown'
        ? 'The restart may have started. Check the existing operation; Halo will not resend it.'
        : restartOperation.status === 'completed'
          ? 'The restarted daemon passed its identity checks.'
          : 'The companion tracks this operation independently of the browser.'}
    </p>
    {#if restartOperation.status !== 'completed' && restartOperation.status !== 'rejected'}<div
        class="actions"
      >
        <button disabled={busy} onclick={checkRestart}>Check restart progress</button>
      </div>{/if}
  </div>{/if}
<h3>Recent operations</h3>
<p class="inline-note">
  Inspect an existing receipt after reconnecting or reloading. Checking an outcome never repeats its
  command.
</p>
<div class="group">
  {#each receipts as receipt (receipt.operation_id)}<Row
      title={pretty(receipt.kind)}
      subtitle={`${pretty(receipt.status)} · ${receipt.operation_id}`}
    >
      <button
        disabled={busy || !receipt.operation_id}
        onclick={() => recoverReceipt(receipt.operation_id!)}>Inspect outcome</button
      >
    </Row>{:else}<div class="empty">No recent operation receipts are available.</div>{/each}
</div>
<h3>Local connection</h3>
{#if access?.restart_required}<div class="notice">
    The saved web address or certificate differs from this running companion. The details below
    describe the connection currently in use. Restart the web companion locally to apply the saved
    configuration.
  </div>{/if}
<div class="group">
  <dl class="kv">
    <dt>Canonical address</dt>
    <dd>{String(access?.canonical_url ?? 'Unavailable')}</dd>
    <dt>Certificate fingerprint</dt>
    <dd class="caption">{String(access?.certificate_fingerprint ?? 'Unavailable')}</dd>
    <dt>Certificate expiry</dt>
    <dd>
      {typeof access?.certificate_expires_at === 'number'
        ? when(access.certificate_expires_at)
        : String(access?.certificate_expires_at ?? 'Unavailable')}
    </dd>
    <dt>Local network access</dt>
    <dd>{access?.lan_enabled === true ? 'Enabled' : 'Not enabled'}</dd>
  </dl>
</div>
<p class="inline-note">
  Certificate and listening-address changes are local owner operations. No software installation or
  release activation is available through the web console.
</p>
{#if confirmation === 'restart'}<Confirm
    title="Restart Halo Server?"
    onclose={() => (confirmation = null)}
    ><p>
      Camera streams and HomeKit connections will temporarily stop. The web console remains
      available to report progress. This does not install updates or change service enablement.
    </p>
    {#if dirty}<p class="warning">Your unsaved form changes will not be applied.</p>{/if}
    <div class="actions end">
      <button onclick={() => (confirmation = null)}>Cancel</button><button
        class="primary"
        onclick={restartDaemon}>Restart Halo</button
      >
    </div></Confirm
  >
{:else if confirmation === 'ring-logout'}<Confirm
    title="Disconnect the Ring account?"
    onclose={() => (confirmation = null)}
    ><p>
      Ring devices and their camera streams will become unavailable until an account is connected
      again.
    </p>
    <div class="actions end">
      <button onclick={() => (confirmation = null)}>Cancel</button><button
        class="danger"
        onclick={ringLogout}>Disconnect Ring</button
      >
    </div></Confirm
  >
{:else if confirmation === 'discard'}<Confirm
    title="Discard unsaved settings?"
    onclose={() => (confirmation = null)}
    ><p>Your draft will be replaced by the latest saved configuration.</p>
    <div class="actions end">
      <button onclick={() => (confirmation = null)}>Keep editing</button><button
        onclick={() => {
          confirmation = null;
          void load();
        }}>Reload saved settings</button
      >
    </div></Confirm
  >{/if}
