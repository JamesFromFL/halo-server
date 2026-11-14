<script lang="ts">
  import { OWNER_USERNAME } from './lib/auth.ts';
  import { PASSWORD_MIN_LENGTH, PASSWORD_MAX_LENGTH } from './lib/password.ts';
  import { onMount, tick } from 'svelte';
  import { API, api, ApiError, errorMessage } from './lib/api.ts';
  import { authority, isFresh, readAppearance, validState, when } from './lib/model.ts';
  import type { Page, Session, StateEnvelope } from './lib/types.ts';
  import Icon from './components/Icon.svelte';
  import Confirm from './components/Confirm.svelte';
  import SignIn from './pages/SignIn.svelte';
  import Overview from './pages/Overview.svelte';
  import Devices from './pages/Devices.svelte';
  import Integrations from './pages/Integrations.svelte';
  import Activity from './pages/Activity.svelte';
  import Diagnostics from './pages/Diagnostics.svelte';
  import Settings from './pages/Settings.svelte';
  const pages: { key: Page; title: string; icon: string }[] = [
    { key: 'overview', title: 'Overview', icon: 'overview' },
    { key: 'devices', title: 'Devices', icon: 'devices' },
    { key: 'integrations', title: 'Integrations', icon: 'integrations' },
    { key: 'activity', title: 'Activity', icon: 'activity' },
    { key: 'diagnostics', title: 'Diagnostics', icon: 'diagnostics' },
    { key: 'settings', title: 'Settings', icon: 'settings' },
  ];
  let page = $state<Page>('overview');
  let deviceId = $state('');
  let sidebar = $state(false);
  let navElement = $state<HTMLElement>();
  let menuButton = $state<HTMLButtonElement>();
  let session = $state<Session | null>(null);
  let snapshot = $state<StateEnvelope | null>(null);
  let connected = $state(false);
  let starting = $state(true);
  let problem = $state('');
  let now = $state(Date.now());
  let toast = $state('');
  let stream: EventSource | null = null;
  let reconnect: ReturnType<typeof setTimeout> | undefined;
  let toastTimer: ReturnType<typeof setTimeout> | undefined;
  let disposed = false;
  let feedGeneration = 0;
  let feedSession: AbortController | undefined;
  let stateRequest: AbortController | undefined;
  let stateRead = 0;
  let refreshing = $state(false);
  let feedReady: ((ready: boolean) => void) | undefined;
  let feedTimeout: ReturnType<typeof setTimeout> | undefined;
  let reauthOpen = $state(false);
  let reauthPassword = $state('');
  let reauthInput = $state<HTMLInputElement>();
  let reauthError = $state('');
  let reauthBusy = $state(false);
  let reauthResolve: ((v: boolean) => void) | undefined;
  const fresh = $derived(isFresh(snapshot, connected, now));
  const generation = $derived(authority(snapshot));
  let navigationGuard: (() => boolean) | null = null;
  let previousHash = location.hash;
  function setNavigationGuard(guard: (() => boolean) | null) {
    navigationGuard = guard;
  }
  function route() {
    if (navigationGuard && !navigationGuard()) {
      history.replaceState(null, '', previousHash || '#/settings');
      return;
    }
    closeReauth(false);
    previousHash = location.hash;
    const [name, id] = location.hash.replace(/^#\/?/, '').split('/');
    page = pages.some((v) => v.key === name) ? (name as Page) : 'overview';
    try {
      deviceId = page === 'devices' && id ? decodeURIComponent(id) : '';
    } catch {
      deviceId = '';
    }
    sidebar = false;
    void tick().then(() => document.getElementById('page-title')?.focus());
  }
  function notify(message: string) {
    toast = message;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => (toast = ''), 6000);
  }
  function finishFeed(ready: boolean) {
    clearTimeout(feedTimeout);
    feedReady?.(ready);
    feedReady = undefined;
  }
  function stopFeed() {
    cancelStateRead();
    finishFeed(false);
    feedSession?.abort();
    feedSession = undefined;
    feedGeneration++;
    stream?.close();
    stream = null;
    connected = false;
    clearTimeout(reconnect);
  }
  function cancelStateRead() {
    stateRead++;
    stateRequest?.abort();
    stateRequest = undefined;
    refreshing = false;
  }
  function accept(value: unknown, fromFeed = false) {
    if (!validState(value)) {
      connected = false;
      problem = 'Halo returned an invalid state. Refresh to try again.';
      return false;
    }
    // A pushed observation supersedes any read begun before it. An old HTTP
    // response must not roll back the current account, policies or device state.
    if (fromFeed) cancelStateRead();
    snapshot = value;
    connected = !document.hidden;
    problem = '';
    return isFresh(value, connected);
  }
  async function refresh() {
    if (disposed || !session?.authenticated) return;
    cancelStateRead();
    const expected = stateRead;
    const request = new AbortController();
    stateRequest = request;
    refreshing = true;
    try {
      const value = await api.get<StateEnvelope>('state', request.signal);
      if (!disposed && expected === stateRead && session?.authenticated) accept(value);
    } catch (error) {
      if (disposed || expected !== stateRead) return;
      if (error instanceof ApiError && error.status === 401) {
        stopFeed();
        snapshot = null;
        session = null;
        api.clear();
        await bootstrap();
        return;
      }
      connected = false;
      problem = errorMessage(error);
    } finally {
      if (stateRequest === request) {
        stateRequest = undefined;
        refreshing = false;
      }
    }
  }
  function startFeed(): Promise<boolean> {
    stopFeed();
    if (!session?.authenticated || document.hidden || disposed) return Promise.resolve(false);
    const expected = feedGeneration;
    const ready = new Promise<boolean>((resolve) => {
      feedReady = resolve;
      feedTimeout = setTimeout(() => finishFeed(false), 15_000);
    });
    stream = new EventSource(`${API}/events`);
    for (const name of ['state', 'resync'])
      stream.addEventListener(name, (event) => {
        if (expected !== feedGeneration) return;
        try {
          if (accept(JSON.parse((event as MessageEvent).data), true)) finishFeed(true);
        } catch {
          connected = false;
          problem = 'State updates were interrupted.';
        }
      });
    stream.onerror = () => {
      if (expected !== feedGeneration) return;
      stopFeed();
      problem = 'Connection interrupted. Displayed observations may be out of date.';
      const recoveryGeneration = feedGeneration;
      reconnect = setTimeout(async () => {
        if (disposed || recoveryGeneration !== feedGeneration) return;
        const request = new AbortController();
        feedSession = request;
        try {
          const current = await api.session(request.signal);
          if (disposed || recoveryGeneration !== feedGeneration) return;
          if (!current.authenticated) {
            session = current;
            snapshot = null;
            closeReauth(false);
            return;
          }
          session = current;
          startFeed();
        } catch {
          if (!disposed && recoveryGeneration === feedGeneration) void startFeed();
        } finally {
          if (feedSession === request) feedSession = undefined;
        }
      }, 5000);
    };
    return ready;
  }
  async function bootstrap() {
    stopFeed();
    starting = true;
    problem = '';
    try {
      session = await api.session();
      if (session.authenticated) {
        void startFeed();
        void refresh();
      } else {
        snapshot = null;
      }
    } catch (error) {
      problem = errorMessage(error);
    } finally {
      starting = false;
    }
  }
  async function signedIn(current: Session) {
    session = current;
    snapshot = null;
    problem = '';
    void startFeed();
    void refresh();
  }
  async function changePassword(currentPassword: string, password: string) {
    stopFeed();
    try {
      session = await api.changePassword(currentPassword, password);
    } finally {
      // A successful change rotates this browser's cookie just like reauth.
      // Reconnect even on rejection; do not replay a password change.
      if (!disposed) void startFeed();
    }
  }
  async function logout() {
    stopFeed();
    try {
      await api.post('logout', {});
      stopFeed();
      snapshot = null;
      api.clear();
      closeReauth(false);
      await bootstrap();
    } catch (error) {
      problem = errorMessage(error);
      if (!disposed) void startFeed();
    }
  }
  function reauthenticate(): Promise<boolean> {
    return new Promise((resolve) => {
      reauthResolve?.(false);
      reauthResolve = resolve;
      reauthPassword = '';
      reauthError = '';
      reauthOpen = true;
      void tick().then(() => {
        if (reauthOpen) reauthInput?.focus();
      });
    });
  }
  function closeReauth(ok: boolean) {
    reauthOpen = false;
    reauthPassword = '';
    reauthError = '';
    reauthResolve?.(ok);
    reauthResolve = undefined;
  }
  async function doReauth(event: SubmitEvent) {
    event.preventDefault();
    if (reauthBusy || !reauthResolve) return;
    const request = reauthResolve;
    reauthBusy = true;
    // The password check rotates the cookie. Retire its old SSE connection
    // before rotation, then require fresh state on the replacement connection.
    stopFeed();
    try {
      const current = await api.reauthenticate(reauthPassword);
      if (disposed) return;
      session = current;
      const ready = await startFeed();
      await tick();
      if (reauthResolve !== request) return;
      if (!ready || !fresh)
        throw new Error(
          'Halo could not refresh its live status. Wait for it to reconnect, then confirm again.',
        );
      closeReauth(true);
    } catch (error) {
      if (!disposed && reauthResolve === request) reauthError = errorMessage(error);
    } finally {
      if (reauthResolve === request) reauthPassword = '';
      reauthBusy = false;
      // A rejected password retains the original session and must restore its feed.
      if (!disposed && !stream) void startFeed();
    }
  }
  async function closeDrawer() {
    sidebar = false;
    await tick();
    menuButton?.focus();
  }
  function drawerKey(event: KeyboardEvent) {
    if (!sidebar) return;
    if (event.key === 'Escape') {
      event.preventDefault();
      closeDrawer();
    }
    if (event.key === 'Tab') {
      const focusable = [...(navElement?.querySelectorAll<HTMLElement>('a,button') ?? [])];
      const first = focusable[0],
        last = focusable.at(-1);
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last?.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first?.focus();
      }
    }
  }
  async function openDrawer() {
    sidebar = true;
    await tick();
    navElement?.querySelector<HTMLElement>('button,a')?.focus();
  }
  onMount(() => {
    document.documentElement.dataset.theme = readAppearance();
    route();
    void bootstrap();
    const interval = setInterval(() => (now = Date.now()), 1000);
    const visibility = () => {
      if (document.hidden) {
        stopFeed();
        closeReauth(false);
      } else if (session?.authenticated) {
        void startFeed();
        void refresh();
      }
    };
    const desktop = window.matchMedia('(min-width: 900px)');
    const resize = () => {
      if (desktop.matches) sidebar = false;
    };
    desktop.addEventListener('change', resize);
    window.addEventListener('hashchange', route);
    document.addEventListener('visibilitychange', visibility);
    return () => {
      disposed = true;
      stopFeed();
      clearInterval(interval);
      clearTimeout(toastTimer);
      closeReauth(false);
      window.removeEventListener('hashchange', route);
      document.removeEventListener('visibilitychange', visibility);
      desktop.removeEventListener('change', resize);
    };
  });
</script>

<svelte:window onkeydown={drawerKey} />
{#if starting}<main class="loading" aria-live="polite">
    <span class="spinner"></span>
    <p>Connecting to Halo…</p>
  </main>
{:else if !session}<main class="auth-page">
    <section class="auth-card">
      <h1>Halo is unavailable</h1>
      <p role="alert">{problem}</p>
      <button onclick={bootstrap}>Try again</button>
    </section>
  </main>
{:else if !session.authenticated}<SignIn {session} onauthenticated={signedIn} />
{:else}
  <a
    href="#main-content"
    class="skip-link"
    onclick={(event) => {
      event.preventDefault();
      document.getElementById('main-content')?.focus();
    }}>Skip to content</a
  >
  <div class="app">
    {#if sidebar}<button
        class="drawer-shade"
        aria-label="Close navigation"
        tabindex="-1"
        onclick={closeDrawer}
      ></button>{/if}
    <aside
      class:open={sidebar}
      class="sidebar"
      bind:this={navElement}
      aria-label="Primary navigation"
    >
      <div class="brand">
        <img src="/halo.png" alt="" />
        <div><strong>Halo</strong><small>Server</small></div>
        <button class="icon-button drawer-close" aria-label="Close navigation" onclick={closeDrawer}
          ><Icon name="close" /></button
        >
      </div>
      <nav class="navigation">
        {#each pages.filter((v) => v.key !== 'settings') as item}<a
            href={`#/${item.key}`}
            aria-current={page === item.key ? 'page' : undefined}
            ><Icon name={item.icon} />{item.title}</a
          >{/each}
      </nav>
      <div class="sidebar-bottom">
        <a
          class="settings-link"
          href="#/settings"
          aria-current={page === 'settings' ? 'page' : undefined}
          ><Icon name="settings" />Settings</a
        ><small class="wrap">{location.host}</small>
      </div>
    </aside>
    <div class="main" inert={sidebar}>
      <header class="header">
        <button
          bind:this={menuButton}
          class="icon-button mobile-menu"
          aria-label="Open navigation"
          aria-expanded={sidebar}
          onclick={openDrawer}><Icon name="menu" /></button
        >
        <h1 id="page-title" tabindex="-1">{pages.find((v) => v.key === page)?.title}</h1>
        <button
          class="icon-button"
          aria-label="Refresh server state"
          aria-busy={refreshing}
          disabled={refreshing}
          onclick={refresh}><Icon name="refresh" /></button
        >
      </header>
      {#if snapshot?.web.draining}<div class="banner" role="status">
          <strong>Release maintenance paused browser changes.</strong>
          {#if (snapshot.web.pending_operations ?? 0) > 0}Wait for {snapshot.web.pending_operations} pending
            operations to finish.
          {:else}The local owner can retry the release or restart the web companion to resume
            changes.{/if}
        </div>{/if}
      {#if !fresh}<div class="banner" role="status">
          {problem ||
            (snapshot?.daemon.available
              ? 'Checking the latest server state…'
              : 'The Halo daemon is unavailable. Recovery tools remain accessible.')}
        </div>{/if}
      <main id="main-content" class="content" tabindex="-1">
        {#key `${page}:${deviceId}`}
          <svelte:boundary>
            {#if page === 'overview'}<Overview {snapshot} {fresh} />
            {:else if page === 'devices'}<Devices
                {snapshot}
                {fresh}
                {deviceId}
                {generation}
                {reauthenticate}
                {notify}
                {refresh}
              />
            {:else if page === 'integrations'}<Integrations
                {snapshot}
                {fresh}
                {generation}
                {reauthenticate}
                {notify}
                {refresh}
              />
            {:else if page === 'activity'}<Activity {snapshot} />
            {:else if page === 'diagnostics'}<Diagnostics {snapshot} {fresh} {notify} />
            {:else if page === 'settings'}<Settings
                {snapshot}
                {fresh}
                {session}
                {reauthenticate}
                {notify}
                {refresh}
                {logout}
                {changePassword}
                {setNavigationGuard}
              />{/if}
            {#snippet failed(_error, reset)}
              <section class="notice error" role="alert">
                <h2>This page could not be displayed</h2>
                <p>
                  Your server keeps running. Try opening the page again, or reload the interface.
                </p>
                <div class="actions">
                  <button onclick={reset}>Try this page again</button>
                  <button onclick={() => location.reload()}>Reload interface</button>
                </div>
              </section>
            {/snippet}
          </svelte:boundary>
        {/key}
      </main>
      <footer class="footer">
        <span
          >{snapshot?.state?.daemon.version
            ? `Halo ${snapshot.state.daemon.version}`
            : 'Halo Server'}</span
        ><span>{fresh ? 'Updated' : 'Last observation'}: {when(snapshot?.state?.observed_at)}</span>
      </footer>
    </div>
  </div>
{/if}
{#if toast}<div class="toast" role="status">{toast}</div>{/if}
{#if reauthOpen}<Confirm title="Confirm your Halo password" onclose={() => closeReauth(false)}
    ><p>Recent authentication is required for this sensitive action.</p>
    {#if reauthError}<div class="notice error" role="alert">{reauthError}</div>{/if}
    <form class="form" onsubmit={doReauth}>
      <label
        >Username<input
          name="username"
          value={OWNER_USERNAME}
          autocomplete="username"
          readonly
        /></label
      >
      <label
        >Password<input
          bind:this={reauthInput}
          type="password"
          bind:value={reauthPassword}
          autocomplete="current-password"
          minlength={PASSWORD_MIN_LENGTH}
          required
          maxlength={PASSWORD_MAX_LENGTH}
        /></label
      >
      <div class="actions end">
        <button type="button" disabled={reauthBusy} onclick={() => closeReauth(false)}
          >Cancel</button
        ><button class="primary" disabled={reauthBusy}>Confirm</button>
      </div>
    </form></Confirm
  >{/if}
