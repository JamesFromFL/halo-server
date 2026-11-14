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
