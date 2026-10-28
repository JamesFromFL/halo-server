/** Explicitly authorized binary IPC; this does not model HAP/HDS drains. */
import { randomUUID } from 'node:crypto';
import type { Duplex } from 'node:stream';
import { BULK_HEADER_BYTES, BULK_LIMITS, MAX_BULK_CHUNK_BYTES, MAX_BULK_WINDOW_CHUNKS,
  decodeBulkHeader, encodeBulkHeader, validateDirection, validateScope,
  type BulkKind, type Envelope, type MediaScope } from './protocol.js';
export class BulkTransferError extends Error {
  constructor(readonly code = 'protocol-error') { super(`Private binary transfer failed (${code})`); }
}
interface Deferred { promise: Promise<void>; resolve(): void; reject(error: Error): void }
function deferred(): Deferred {
  let resolve!: () => void, reject!: (error: Error) => void;
  const promise = new Promise<void>((yes, no) => { resolve = yes; reject = no; });
  void promise.catch(() => {});
  return { promise, resolve, reject };
}
interface Identity { scope: MediaScope; operationId: string; transferId: string; kind: BulkKind; totalBytes: number; valid?: () => boolean }
export interface ReceiveOptions extends Identity {
  sink(chunk: Buffer, signal: AbortSignal): Promise<void>;
  onClose?(code: string): Promise<void>;
}
export interface SendOptions extends Identity { source: AsyncIterable<Uint8Array> }
interface Base extends Identity { valid: () => boolean; completion: Deferred; timer: NodeJS.Timeout }
interface Incoming extends Base {
  sink: ReceiveOptions['sink']; onClose: ReceiveOptions['onClose'];
  openId: string | undefined; chunkBytes: number; window: number; granted: number;
  sequence: number; received: number; end: { requestId: string; count: number } | undefined; finishing: boolean;
}
interface Outgoing extends Base {
  openId: string; endId: string; available: number; sent: number; acknowledged: number;
  initialized: boolean; endSent: boolean; changed: Deferred;
  iterator?: AsyncIterator<Uint8Array>; pendingNext?: Promise<IteratorResult<Uint8Array>> | undefined;
  iteratorClosing?: Promise<void> | undefined;
}
export interface ReceiveHandle { wait(): Promise<void>; cancel(): Promise<void> }
export interface BulkOptions {
  bootId: string; role: 'python' | 'hap'; timeoutMs?: number;
  sendControl(kind: Envelope['kind'], payload: Record<string, unknown>, requestId?: string): Promise<void>;
  onFailure?(error: Error): void | Promise<void>;
}
const uuid = (value: string): boolean => /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(value);
const sameScope = (a: MediaScope, b: MediaScope): boolean =>
  a.profile_id === b.profile_id && a.camera_id === b.camera_id && a.account_generation === b.account_generation &&
  a.device_generation === b.device_generation && a.privacy_generation === b.privacy_generation;
export class BulkChannel {
  private readonly incoming = new Map<string, Incoming>();
  private readonly outgoing = new Map<string, Outgoing>();
  private readonly abort = new AbortController();
  private readonly timeoutMs: number;
  private reader: Promise<void> | undefined;
  private closing = false;
  private closeWork: Promise<void> | undefined;
  private failureValue: Error | undefined;
  private writeTail: Promise<void> = Promise.resolve();
  private slots = MAX_BULK_WINDOW_CHUNKS;
  private slotChanged = deferred();
  private readonly consumers = new Set<Promise<void>>();
  constructor(private readonly socket: Duplex, private readonly options: BulkOptions) {
    this.timeoutMs = options.timeoutMs ?? 5000;
    if (!uuid(options.bootId) || !['python', 'hap'].includes(options.role) || !Number.isFinite(this.timeoutMs) || this.timeoutMs <= 0)
      throw new BulkTransferError('invalid-request');
    socket.pause(); socket.on('error', this.socketError);
  }
  private readonly socketError = (error: Error): void => { void this.fail(error).catch(() => {}); };
  get closed(): boolean { return this.closing; }
  get failure(): Error | undefined { return this.failureValue; }
  get counts(): readonly [number, number] { return [this.incoming.size, this.outgoing.size]; }
  start(): void { this.ensureOpen(); if (!this.reader) { this.reader = this.readLoop(); void this.reader.catch(() => {}); } }
  private ensureOpen(): void { if (this.closing) throw new BulkTransferError('cancelled'); }
  private identity(options: Identity, incoming: boolean): () => boolean {
    this.ensureOpen(); validateScope(options.scope);
    encodeBulkHeader({ kind: options.kind, transfer_id: options.transferId, sequence: 0, length: 1 });
    if (!uuid(options.operationId) || !Number.isSafeInteger(options.totalBytes) || options.totalBytes < 1 || options.totalBytes > BULK_LIMITS[options.kind]) throw new BulkTransferError();
    if (((options.kind === 'talkback_pcm' ? 'hap' : 'python') !== this.options.role) !== incoming) throw new BulkTransferError();
    if (this.incoming.has(options.transferId) || this.outgoing.has(options.transferId)) throw new BulkTransferError('conflict');
    const valid = options.valid ?? (() => true);
    if (!valid()) throw new BulkTransferError('invalidated');
    return valid;
  }
  private deadline(): NodeJS.Timeout {
    return setTimeout(() => { void this.fail(new BulkTransferError('timeout')).catch(() => {}); }, this.timeoutMs);
  }
  authorizeReceive(options: ReceiveOptions): ReceiveHandle {
    const valid = this.identity(options, true);
    if (this.incoming.size >= 4) throw new BulkTransferError('busy');
    const state: Incoming = { ...options, scope: { ...options.scope }, valid, completion: deferred(), timer: this.deadline(),
      openId: undefined, chunkBytes: 0, window: 0, granted: 0, sequence: 0, received: 0, end: undefined, finishing: false, onClose: options.onClose };
    this.incoming.set(options.transferId, state);
    return { wait: () => state.completion.promise, cancel: async () => { if (this.incoming.has(options.transferId)) await this.close(); } };
  }
  private async bounded<T>(promise: Promise<T>, abortable = true): Promise<T> {
    let timer: NodeJS.Timeout | undefined;
    let rejectAbort: ((error: Error) => void) | undefined;
    const abort = (): void => rejectAbort?.(new BulkTransferError('cancelled'));
    const interrupted = new Promise<never>((_, no) => {
      rejectAbort = no; timer = setTimeout(() => no(new BulkTransferError('timeout')), this.timeoutMs);
      if (abortable) { this.abort.signal.addEventListener('abort', abort, { once: true }); if (this.abort.signal.aborted) abort(); }
    });
    try { return await Promise.race([promise, interrupted]); }
    finally { if (timer) clearTimeout(timer); this.abort.signal.removeEventListener('abort', abort); }
  }
  private control(kind: Envelope['kind'], state: Base, extra: Record<string, unknown>, requestId: string): Promise<void> {
    this.ensureOpen(); if (!state.valid()) throw new BulkTransferError('invalidated');
    return this.bounded(this.options.sendControl(kind, { scope: state.scope, operation_id: state.operationId, transfer_id: state.transferId, ...extra }, requestId));
  }
  async handleControl(envelope: Envelope): Promise<boolean> {
    if (!envelope.kind.startsWith('transfer_')) return false;
    try {
      this.ensureOpen(); validateDirection(envelope, this.options.role === 'python' ? 'hap' : 'python');
      if (envelope.boot_id !== this.options.bootId) throw new BulkTransferError();
      const payload = envelope.payload, transferId = payload.transfer_id as string;
      const state = ['transfer_open', 'transfer_end'].includes(envelope.kind) ? this.incoming.get(transferId) :
        ['transfer_credit', 'transfer_result'].includes(envelope.kind) ? this.outgoing.get(transferId) : this.incoming.get(transferId) ?? this.outgoing.get(transferId);
      if (!state || !sameScope(state.scope, payload.scope as MediaScope) || state.operationId !== payload.operation_id || !state.valid()) throw new BulkTransferError('invalidated');
      if (envelope.kind === 'transfer_open') {
        const incoming = state as Incoming;
        if (incoming.openId !== undefined || payload.kind !== incoming.kind || payload.total_bytes !== incoming.totalBytes) throw new BulkTransferError();
        incoming.openId = envelope.request_id; incoming.chunkBytes = payload.chunk_bytes as number;
        incoming.window = payload.window_chunks as number; incoming.granted = incoming.window;
        await this.control('transfer_credit', state, { next_sequence: 0, credits: incoming.window }, envelope.request_id);
      } else if (envelope.kind === 'transfer_credit') {
        const outgoing = state as Outgoing, sequence = payload.next_sequence as number, credits = payload.credits as number;
        if (envelope.request_id !== outgoing.openId) throw new BulkTransferError();
        if (!outgoing.initialized) {
          if (sequence !== 0 || credits !== MAX_BULK_WINDOW_CHUNKS) throw new BulkTransferError();
          outgoing.initialized = true;
        } else {
          const delta = sequence - outgoing.acknowledged;
          if (delta !== credits || delta < 1 || delta > outgoing.sent - outgoing.acknowledged) throw new BulkTransferError();
          this.slots += delta; this.slotChanged.resolve(); this.slotChanged = deferred(); outgoing.acknowledged = sequence;
        }
        if (outgoing.available + credits + outgoing.sent - outgoing.acknowledged > MAX_BULK_WINDOW_CHUNKS) throw new BulkTransferError();
        outgoing.available += credits; outgoing.changed.resolve(); outgoing.changed = deferred();
      } else if (envelope.kind === 'transfer_end') {
        const incoming = state as Incoming;
        if (!incoming.openId || incoming.end || payload.total_bytes !== incoming.totalBytes ||
            (payload.sequence_count as number) < Math.ceil(incoming.totalBytes / incoming.chunkBytes) ||
            (payload.sequence_count as number) > incoming.totalBytes) throw new BulkTransferError();
        incoming.end = { requestId: envelope.request_id, count: payload.sequence_count as number }; await this.finishIncoming(incoming);
      } else if (envelope.kind === 'transfer_result') {
        const outgoing = state as Outgoing;
        if (envelope.request_id !== outgoing.endId || !outgoing.endSent || payload.status !== 'complete' || payload.code !== 'ok' || outgoing.acknowledged !== outgoing.sent) throw new BulkTransferError();
        clearTimeout(state.timer); state.completion.resolve(); outgoing.changed.resolve();
      } else throw new BulkTransferError(payload.code as string);
      return true;
    } catch (error) { await this.fail(error as Error); throw error; }
  }
  async send(options: SendOptions): Promise<void> {
    const valid = this.identity(options, false);
    if (this.outgoing.size >= 4) throw new BulkTransferError('busy');
    const state: Outgoing = { ...options, scope: { ...options.scope }, valid, completion: deferred(), timer: this.deadline(),
      openId: randomUUID(), endId: randomUUID(), available: 0, sent: 0, acknowledged: 0, initialized: false, endSent: false, changed: deferred() };
    this.outgoing.set(options.transferId, state);
    const iterator = options.source[Symbol.asyncIterator](); state.iterator = iterator;
    try {
      await this.control('transfer_open', state, { kind: options.kind, total_bytes: options.totalBytes, chunk_bytes: Math.min(MAX_BULK_CHUNK_BYTES, options.totalBytes), window_chunks: MAX_BULK_WINDOW_CHUNKS }, state.openId);
      let sentBytes = 0;
      while (true) {
        while (!state.available) { this.ensureOpen(); await this.bounded(state.changed.promise); }
        while (!this.slots) { this.ensureOpen(); await this.bounded(this.slotChanged.promise); }
        --this.slots;
        let reserved = true;
        try {
          const pending = iterator.next(); state.pendingNext = pending;
          void pending.finally(() => { if (state.pendingNext === pending) state.pendingNext = undefined; }).catch(() => {});
          const next = await this.bounded(pending); if (next.done) break;
          const chunk = next.value;
          if (!(chunk instanceof Uint8Array) || chunk.byteLength < 1 || chunk.byteLength > Math.min(MAX_BULK_CHUNK_BYTES, options.totalBytes) || sentBytes + chunk.byteLength > options.totalBytes) throw new BulkTransferError();
          const previous = this.writeTail, turn = deferred(); this.writeTail = turn.promise;
          try {
            await this.bounded(previous); this.ensureOpen(); if (!valid()) throw new BulkTransferError('invalidated');
            const header = encodeBulkHeader({ kind: options.kind, transfer_id: options.transferId, sequence: state.sent, length: chunk.byteLength });
            --state.available; ++state.sent; reserved = false;
            await this.bounded(new Promise<void>((yes, no) => {
              this.socket.write(Buffer.concat([header, chunk]), error => error ? no(error) : yes());
            }));
          } finally { turn.resolve(); }
          sentBytes += chunk.byteLength;
        } finally { if (reserved) { ++this.slots; this.slotChanged.resolve(); this.slotChanged = deferred(); } }
      }
      if (sentBytes !== options.totalBytes || !state.sent || !valid()) throw new BulkTransferError();
      state.endSent = true;
      await this.control('transfer_end', state, { sequence_count: state.sent, total_bytes: options.totalBytes }, state.endId);
      await this.bounded(state.completion.promise);
    } catch (error) { await this.fail(error as Error); throw error; }
    finally {
      try { await this.bounded(this.closeIterator(state), false); this.outgoing.delete(options.transferId); }
      catch (error) { if (!this.closing) await this.fail(error as Error); throw error; }
    }
  }
  private async readable(idle: boolean): Promise<void> {
    // Idle headers wait for supervised socket closure, not a short transfer timer.
    let cleanup = (): void => {};
    const promise = new Promise<void>((yes, no) => {
      const ready = (): void => { cleanup(); yes(); };
      const end = (): void => { cleanup(); no(new BulkTransferError('cancelled')); };
      const error = (cause: Error): void => { cleanup(); no(cause); };
      cleanup = () => { this.socket.off('readable', ready); this.socket.off('end', end); this.socket.off('close', end); this.socket.off('error', error); this.abort.signal.removeEventListener('abort', end); };
      this.socket.once('readable', ready); this.socket.once('end', end); this.socket.once('close', end); this.socket.once('error', error);
      this.abort.signal.addEventListener('abort', end, { once: true });
    });
    try { if (idle) await promise; else await this.bounded(promise); } finally { cleanup(); }
  }
  private async readExact(length: number, idle = false): Promise<Buffer> {
    const output = Buffer.allocUnsafe(length); let offset = 0;
    while (offset < length) {
      this.ensureOpen(); const chunk = this.socket.read(length - offset) as Buffer | null;
      if (chunk) { chunk.copy(output, offset); offset += chunk.length; continue; }
      if (this.socket.readableEnded || this.socket.destroyed) throw new BulkTransferError('cancelled');
      await this.readable(idle && offset === 0);
    }
    return output;
  }
  private async readLoop(): Promise<void> {
    try {
      while (!this.closing) {
        const header = decodeBulkHeader(await this.readExact(BULK_HEADER_BYTES, true)), state = this.incoming.get(header.transfer_id);
        if (!state || !state.openId || !state.valid() || header.kind !== state.kind || header.sequence !== state.sequence || state.granted < 1 || header.length > state.chunkBytes || state.received + header.length > state.totalBytes) throw new BulkTransferError();
        --state.granted; const chunk = await this.readExact(header.length);
        if (!state.valid()) throw new BulkTransferError('invalidated');
        const consumer = state.sink(chunk, this.abort.signal); this.consumers.add(consumer);
        void consumer.finally(() => this.consumers.delete(consumer)).catch(() => {});
        await this.bounded(consumer);
        if (!state.valid()) throw new BulkTransferError('invalidated');
        state.received += chunk.length; ++state.sequence; ++state.granted;
        await this.control('transfer_credit', state, { next_sequence: state.sequence, credits: 1 }, state.openId);
        await this.finishIncoming(state);
      }
    } catch (error) { if (!this.closing) await this.fail(error as Error); }
  }
  private async finishIncoming(state: Incoming): Promise<void> {
    if (!state.end || state.received < state.totalBytes || state.finishing) return;
    state.finishing = true;
    if (state.received !== state.totalBytes || state.sequence !== state.end.count || !state.valid()) throw new BulkTransferError();
    if (state.onClose) { await this.bounded(state.onClose('ok')); state.onClose = undefined; }
    await this.control('transfer_result', state, { status: 'complete', code: 'ok' }, state.end.requestId);
    this.incoming.delete(state.transferId); clearTimeout(state.timer); state.completion.resolve();
  }
  private async fail(error: Error): Promise<void> {
    if (this.closing) return;
    this.failureValue = error;
    try { await this.close(error instanceof BulkTransferError ? error.code : 'protocol-error'); }
    finally { await this.options.onFailure?.(error); }
  }
  async close(code = 'cancelled'): Promise<void> {
    if (this.closeWork) return this.closeWork;
    this.closing = true; this.abort.abort(); this.socket.destroy();
    for (const state of [...this.incoming.values(), ...this.outgoing.values()]) {
      clearTimeout(state.timer); if ('changed' in state) state.changed.resolve();
    }
    this.slotChanged.resolve();
    this.closeWork = this.retire(code);
    try { await this.closeWork; } finally { this.closeWork = undefined; }
  }
  private closeIterator(state: Outgoing): Promise<void> {
    if (!state.iteratorClosing) {
      state.iteratorClosing = (async () => {
        const pending = state.pendingNext;
        const returned = state.iterator?.return?.();
        const results = await Promise.allSettled([pending, returned]);
        if (results[1]?.status === 'rejected') throw new BulkTransferError('source-failed');
      })().catch(error => { state.iteratorClosing = undefined; throw error; });
      void state.iteratorClosing.catch(() => {});
    }
    return state.iteratorClosing;
  }
  private async retire(code: string): Promise<void> {
    const states = [...this.incoming.values(), ...this.outgoing.values()];
    let failed = false;
    try {
      await this.bounded(Promise.allSettled([...this.consumers]).then(() => {}), false);
      const failures: Error[] = [];
      for (const [id, state] of this.incoming) {
        try { if (state.onClose) { await this.bounded(state.onClose(code), false); state.onClose = undefined; } this.incoming.delete(id); }
        catch (error) { failures.push(error as Error); }
      }
      for (const [id, state] of this.outgoing) {
        try { await this.bounded(this.closeIterator(state), false); this.outgoing.delete(id); }
        catch (error) { failures.push(error as Error); }
      }
      if (failures.length) throw new BulkTransferError('source-failed');
    } catch (error) { failed = true; throw error; }
    finally { for (const state of states) state.completion.reject(new BulkTransferError(failed ? 'source-failed' : code)); }
  }
}
