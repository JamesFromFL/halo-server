import type { Session } from './types.ts';
export const API = '/api/web/v1';
const MAX_RESPONSE = 1024 * 1024;
export class ApiError extends Error {
  code: string;
  possiblySent: boolean;
  status: number;
  constructor(code: string, message: string, possiblySent = false, status = 0) {
    super(message);
    this.name = 'ApiError';
    this.code = code;
    this.possiblySent = possiblySent;
    this.status = status;
  }
}
export class ApiClient {
  private csrf = '';
  private transport: typeof fetch;
  constructor(transport: typeof fetch = globalThis.fetch.bind(globalThis)) {
    this.transport = transport;
  }
  clear() {
    this.csrf = '';
  }
  private rememberSession(value: Session): Session {
    if (typeof value.csrf_token !== 'string' || typeof value.authenticated !== 'boolean')
      throw new ApiError('bad_response', 'Halo returned an invalid session.');
    this.csrf = value.csrf_token;
    return value;
  }
  async session(signal?: AbortSignal): Promise<Session> {
    const value = await this.get<Session>('session', signal);
    if (signal?.aborted)
      throw new ApiError('session_changed', 'This session refresh is no longer current.');
    return this.rememberSession(value);
  }
  async reauthenticate(password: string): Promise<Session> {
    return this.rememberSession(await this.post<Session>('reauth', { password }));
  }
  get<T>(path: string, signal?: AbortSignal): Promise<T> {
    return this.request<T>(path, 'GET', undefined, signal);
  }
  post<T>(path: string, body: unknown): Promise<T> {
    return this.request<T>(path, 'POST', body);
  }
  delete<T>(path: string): Promise<T> {
    return this.request<T>(path, 'DELETE', {});
  }
  async request<T>(path: string, method: string, body?: unknown, signal?: AbortSignal): Promise<T> {
    if (!/^[A-Za-z0-9_/-]+$/.test(path) || path.includes('..'))
      throw new ApiError('invalid_route', 'Invalid Halo route.');
    const mutation = method !== 'GET';
    if (mutation && !this.csrf)
      throw new ApiError('session_required', 'Refresh the session before making changes.');
    const abort = new AbortController();
    const stop = () => abort.abort();
    signal?.addEventListener('abort', stop, { once: true });
    if (signal?.aborted) abort.abort();
    const timeout = setTimeout(stop, 45_000);
    try {
      const response = await this.transport(`${API}/${path}`, {
        method,
        credentials: 'same-origin',
        cache: 'no-store',
        redirect: 'error',
        headers: mutation
          ? { 'Content-Type': 'application/json', 'X-Halo-CSRF': this.csrf }
          : { Accept: 'application/json' },
        body: mutation ? JSON.stringify(body) : undefined,
        signal: abort.signal,
      });
      if (Number(response.headers.get('content-length')) > MAX_RESPONSE)
        throw new ApiError('response_limit', 'Halo response exceeded the display limit.', mutation);
      const reader = response.body?.getReader();
      let size = 0;
      const chunks: Uint8Array[] = [];
      if (reader) {
        try {
          for (;;) {
            const { done, value } = await reader.read();
            if (done) break;
            size += value.byteLength;
            if (size > MAX_RESPONSE) {
              await reader.cancel();
              throw new ApiError(
                'response_limit',
                'Halo response exceeded the display limit.',
                mutation,
              );
            }
            chunks.push(value);
          }
        } finally {
          reader.releaseLock();
        }
      }
      const bytes = new Uint8Array(size);
      let offset = 0;
      for (const chunk of chunks) {
        bytes.set(chunk, offset);
        offset += chunk.byteLength;
      }
      let payload: {
        ok?: boolean;
        result?: T;
        error?: { code?: string; message?: string; possibly_sent?: boolean };
      };
      try {
        payload = JSON.parse(new TextDecoder().decode(bytes));
      } catch {
        throw new ApiError(
          'bad_response',
          'Halo returned an unreadable response.',
          mutation,
          response.status,
        );
      }
      if (!response.ok || payload.ok !== true)
        throw new ApiError(
          payload.error?.code ?? 'request_failed',
          payload.error?.message ?? 'Halo could not complete the request.',
          payload.error?.possibly_sent === true,
          response.status,
        );
      return payload.result as T;
    } catch (error) {
      if (error instanceof ApiError) throw error;
      throw new ApiError(
        'connection_lost',
        mutation
          ? 'Connection interrupted. This action may have reached Halo; check its outcome before trying again.'
          : 'Cannot reach the Halo web service.',
        mutation,
      );
    } finally {
      clearTimeout(timeout);
      signal?.removeEventListener('abort', stop);
    }
  }
}
export const api = new ApiClient();
export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : 'Halo could not complete this action.';
}
export function operationId(): string {
  return crypto.randomUUID();
}
