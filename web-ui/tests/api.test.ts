import { test } from 'node:test';
import assert from 'node:assert/strict';
import { ApiClient, ApiError } from '../src/lib/api.ts';
const ok = (result: unknown) => new Response(JSON.stringify({ ok: true, result }));
const session = {
  authenticated: true,
  setup_required: false,
  csrf_token: 'test-csrf',
  reauthenticated: false,
  session_id: 'test-session',
};
test('mutations require bootstrap and use CSRF, cookies, JSON and no cache', async () => {
  const calls: RequestInit[] = [];
  const client = new ApiClient(async (_url, init) => {
    calls.push(init!);
    return ok(calls.length === 1 ? session : { revoked: true });
  });
  await assert.rejects(client.post('controls', {}));
  assert.equal(calls.length, 0);
  await client.session();
  await client.delete('sessions/00000000-0000-0000-0000-000000000001');
  assert.equal(calls[1].method, 'DELETE');
  assert.equal(calls[1].credentials, 'same-origin');
  assert.equal(calls[1].cache, 'no-store');
  assert.equal((calls[1].headers as Record<string, string>)['X-Halo-CSRF'], session.csrf_token);
  assert.equal(calls[1].body, '{}');
});
test('network uncertainty sends a control exactly once and reports possibly sent', async () => {
  let writes = 0;
  const client = new ApiClient(async (_url, init) => {
    if (init?.method === 'GET') return ok(session);
    writes++;
    throw new TypeError('network gone');
  });
  await client.session();
  await assert.rejects(
    client.post('controls', { operation_id: 'same-id' }),
    (e: unknown) => e instanceof ApiError && e.possiblySent,
  );
  assert.equal(writes, 1);
});
test('conflict preserves classification and never retries', async () => {
  let writes = 0;
  const client = new ApiClient(async (_url, init) => {
    if (init?.method === 'GET') return ok(session);
    writes++;
    return new Response(
      JSON.stringify({
        ok: false,
        error: { code: 'configuration_conflict', message: 'Changed', possibly_sent: false },
      }),
      { status: 409 },
    );
  });
  await client.session();
  await assert.rejects(
    client.post('configuration', {}),
    (e: unknown) => e instanceof ApiError && e.status === 409 && !e.possiblySent,
  );
  assert.equal(writes, 1);
});
test('malformed mutation replies remain uncertain and oversized replies are bounded', async () => {
  const client = new ApiClient(async (_url, init) =>
    init?.method === 'GET' ? ok(session) : new Response('not-json'),
  );
  await client.session();
  await assert.rejects(
    client.post('controls', {}),
    (e: unknown) => e instanceof ApiError && e.possiblySent,
  );
  const large = new ApiClient(async () => new Response('x'.repeat(1024 * 1024 + 1)));
  await assert.rejects(
    large.get('state'),
    (e: unknown) => e instanceof ApiError && e.code === 'response_limit',
  );
});
test('route input cannot become cross-origin or escape API namespace', async () => {
  let calls = 0;
  const client = new ApiClient(async () => {
    calls++;
    return ok({});
  });
  for (const route of ['https://example.com', '../secret', 'state?token=x', '//outside.example'])
    await assert.rejects(client.get(route));
  assert.equal(calls, 0);
});
test('rotated sessions refresh CSRF; clear prevents another mutation', async () => {
  let csrf = 'one';
  let sent = '';
  const client = new ApiClient(async (_url, init) => {
    if (init?.method === 'GET') return ok({ ...session, csrf_token: csrf });
    sent = (init!.headers as Record<string, string>)['X-Halo-CSRF'];
    return ok({});
  });
  await client.session();
  csrf = 'two';
  await client.session();
  await client.post('reauth', { password: 'test-only' });
  assert.equal(sent, 'two');
  client.clear();
  await assert.rejects(client.post('controls', {}));
});

for (const path of ['setup', 'login']) {
  test(`${path} sends owner credentials only in the protected JSON body and does not retry failure`, async () => {
    const credentials = {
      ...(path === 'setup' ? { setup_code: 'test-only-setup-code' } : {}),
      username: 'admin',
      password: 'test-only-owner-password',
    };
    const calls: { url: string; init: RequestInit }[] = [];
    const client = new ApiClient(async (url, init) => {
      if (init?.method === 'GET') return ok({ ...session, authenticated: false });
      calls.push({ url: String(url), init: init! });
      return new Response(
        JSON.stringify({
          ok: false,
          error: { code: 'authentication_failed', message: 'Sign-in failed' },
        }),
        { status: 401 },
      );
    });
    await client.session();
    await assert.rejects(
      client.post(path, credentials),
      (error: unknown) => error instanceof ApiError && error.status === 401 && !error.possiblySent,
    );
    assert.equal(calls.length, 1);
    assert.equal(calls[0].url, `/api/web/v1/${path}`);
    assert.equal(calls[0].init.credentials, 'same-origin');
    assert.equal(calls[0].init.cache, 'no-store');
    assert.equal(calls[0].init.redirect, 'error');
    const headers = calls[0].init.headers as Record<string, string>;
    assert.equal(headers['X-Halo-CSRF'], session.csrf_token);
    assert.equal(headers['Content-Type'], 'application/json');
    assert.deepEqual(JSON.parse(String(calls[0].init.body)), credentials);
  });
}

test('reauthentication installs rotated CSRF directly before the sensitive action', async () => {
  const calls: { url: string; init: RequestInit }[] = [];
  const rotated = {
    ...session,
    csrf_token: 'rotated-csrf',
    session_id: 'rotated-session',
    reauthenticated: true,
  };
  const client = new ApiClient(async (url, init) => {
    calls.push({ url: String(url), init: init! });
    if (String(url).endsWith('/session')) return ok(session);
    if (String(url).endsWith('/reauth')) return ok(rotated);
    return ok({});
  });
  await client.session();
  assert.deepEqual(await client.reauthenticate('test-only-password'), rotated);
  await client.post('pairing/reveal', {});
  assert.deepEqual(
    calls.map((call) => call.url),
    ['/api/web/v1/session', '/api/web/v1/reauth', '/api/web/v1/pairing/reveal'],
  );
  assert.equal(
    (calls[1].init.headers as Record<string, string>)['X-Halo-CSRF'],
    session.csrf_token,
  );
  assert.equal(
    (calls[2].init.headers as Record<string, string>)['X-Halo-CSRF'],
    rotated.csrf_token,
  );
  assert.deepEqual(JSON.parse(String(calls[1].init.body)), { password: 'test-only-password' });
});

test('retiring a delayed reconnect cannot overwrite the rotated session token', async () => {
  let release!: (value: Response) => void;
  let reads = 0;
  let sent = '';
  const client = new ApiClient(async (url, init) => {
    if (String(url).endsWith('/session')) {
      if (++reads === 1) return ok(session);
      // Deliberately ignore AbortSignal, as if the old response already arrived.
      return new Promise<Response>((resolve) => {
        release = resolve;
      });
    }
    if (String(url).endsWith('/reauth')) return ok({ ...session, csrf_token: 'new-csrf' });
    sent = (init!.headers as Record<string, string>)['X-Halo-CSRF'];
    return ok({});
  });
  await client.session();
  const reconnect = new AbortController();
  const pending = client.session(reconnect.signal);
  reconnect.abort();
  await client.reauthenticate('test-only-password');
  release(ok(session));
  await assert.rejects(
    pending,
    (error: unknown) => error instanceof ApiError && error.code === 'session_changed',
  );
  await client.post('pairing/reveal', {});
  assert.equal(sent, 'new-csrf');
});

test('a late session read cannot restore authority after sign-out or password rotation', async () => {
  for (const action of ['clear', 'password'] as const) {
    let reads = 0;
    let release!: (response: Response) => void;
    let sent = '';
    const client = new ApiClient(async (url, init) => {
      if (String(url).endsWith('/session')) {
        if (++reads === 1) return ok(session);
        return new Promise<Response>((resolve) => {
          release = resolve;
        });
      }
      if (String(url).endsWith('/password'))
        return ok({ ...session, csrf_token: 'changed-password-csrf' });
      sent = (init!.headers as Record<string, string>)['X-Halo-CSRF'];
      return ok({});
    });
    await client.session();
    const pending = client.session();
    if (action === 'clear') client.clear();
    else await client.changePassword('synthetic-old-password', 'synthetic-new-password');
    release(ok(session));
    await assert.rejects(
      pending,
      (error: unknown) => error instanceof ApiError && error.code === 'session_changed',
    );
    if (action === 'clear') await assert.rejects(client.post('controls', {}));
    else {
      await client.post('controls', {});
      assert.equal(sent, 'changed-password-csrf');
    }
  }
});

test('concurrent session reads accept only the latest request', async () => {
  let release!: (response: Response) => void;
  let reads = 0;
  let sent = '';
  const client = new ApiClient(async (_url, init) => {
    if (init?.method === 'GET') {
      if (++reads === 1)
        return new Promise<Response>((resolve) => {
          release = resolve;
        });
      return ok({ ...session, csrf_token: 'newest-csrf' });
    }
    sent = (init!.headers as Record<string, string>)['X-Halo-CSRF'];
    return ok({});
  });
  const pending = client.session();
  await client.session();
  release(ok(session));
  await assert.rejects(
    pending,
    (error: unknown) => error instanceof ApiError && error.code === 'session_changed',
  );
  await client.post('controls', {});
  assert.equal(sent, 'newest-csrf');
});

test('malformed sessions and success envelopes report invalid data without granting authority', async () => {
  for (const value of [
    null,
    {},
    { ...session, csrf_token: '' },
    { ...session, setup_required: null },
  ]) {
    const client = new ApiClient(async () => ok(value));
    await assert.rejects(
      client.session(),
      (error: unknown) => error instanceof ApiError && error.code === 'bad_response',
    );
    await assert.rejects(client.post('controls', {}));
  }
  for (const value of [null, [], {}, { ok: false }, { ok: true }]) {
    let reads = 0;
    const client = new ApiClient(async () =>
      ++reads === 1 ? ok(session) : new Response(JSON.stringify(value)),
    );
    await client.session();
    await assert.rejects(
      client.post('controls', {}),
      (error: unknown) =>
        error instanceof ApiError && error.code === 'bad_response' && error.possiblySent,
    );
  }
});
