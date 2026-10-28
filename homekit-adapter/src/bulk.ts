/** Explicitly authorized binary IPC; this does not model HAP/HDS drains. */
import { randomUUID } from 'node:crypto';
import type { Duplex } from 'node:stream';
import { BULK_HEADER_BYTES, BULK_LIMITS, MAX_BULK_CHUNK_BYTES, MAX_BULK_WINDOW_CHUNKS,
  decodeBulkHeader, encodeBulkHeader, validateDirection, validateScope,
  type BulkKind, type Envelope, type MediaScope } from './protocol.js';
export class BulkTransferError extends Error {
  constructor(readonly code = 'protocol-error') { super(`Private binary transfer failed (${code})`); }
}
export type CancelCode = 'cancelled' | 'invalidated' | 'timeout';
interface Deferred { promise: Promise<void>; resolve(): void; reject(error: Error): void }
function deferred(): Deferred {
  let resolve!: () => void, reject!: (error: Error) => void;
  const promise = new Promise<void>((yes, no) => { resolve = yes; reject = no; });
  void promise.catch(() => {}); return { promise, resolve, reject };
}
interface Identity { scope: MediaScope; operationId: string; transferId: string; kind: BulkKind; totalBytes: number; valid?: () => boolean }
export interface ReceiveOptions extends Identity {
  sink(chunk: Buffer, signal: AbortSignal): Promise<void>;
  onClose?(code: string): Promise<void>;
}
export interface SendOptions extends Identity { source: AsyncIterable<Uint8Array> }
interface Fence { requestId: string; count: number; bytes: number; code: CancelCode }
interface Base extends Identity {
  valid: () => boolean; completion: Deferred; wire: Deferred; timer: NodeJS.Timeout | undefined;
  abort: AbortController; cancelled: CancelCode | undefined; terminal: boolean; fenceWaiting: boolean; cleanup?: Promise<void> | undefined;
}
interface Incoming extends Base {
  sink: ReceiveOptions['sink']; onClose: ReceiveOptions['onClose']; consumers: Set<Promise<void>>;
  openId: string | undefined; chunkBytes: number; window: number; granted: number;
  sequence: number; received: number; credited: number; readingBytes: number; end: { requestId: string; count: number } | undefined; finishing: boolean;
  cancelId: string | undefined; fence: Fence | undefined; closingConsumer?: Promise<void> | undefined;
}
interface Outgoing extends Base {
  openId: string; endId: string; available: number; sent: number; sentBytes: number; acknowledged: number;
  initialized: boolean; endSent: boolean; changed: Deferred; fence: Fence | undefined; fencing?: Promise<void>;
  cancelId: string | undefined; openWritten: Deferred;
  iterator?: AsyncIterator<Uint8Array>; pendingNext?: Promise<IteratorResult<Uint8Array>> | undefined;
  iteratorClosing?: Promise<void> | undefined;
}
interface Tombstone {
  scope: MediaScope; operationId: string; transferId: string; kind: BulkKind; totalBytes: number;
  outgoing: boolean; expires: number; openId: string | undefined; endId: string | undefined;
  sequence: number; bytes: number; acknowledged: number; initialized: boolean; endSent: boolean;
  fence: Fence | undefined; cancelId: string | undefined;
}
export interface ReceiveHandle { wait(): Promise<void>; cancel(code?: CancelCode): Promise<void> }
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
  private readonly tombstones = new Map<string, Tombstone>();
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
  private prune(): void {
    for (const [id, value] of this.tombstones) if (value.expires <= performance.now()) this.tombstones.delete(id);
  }
  private remember(state: Incoming | Outgoing, outgoing: boolean): void {
    this.prune();
    // Never discard a still-live correlation to admit more transfers.
    if (this.tombstones.size >= 4096 && !this.tombstones.has(state.transferId)) throw new BulkTransferError('busy');
    // No callbacks, promises, buffers or native source handles enter history.
    const producer = state as Outgoing, consumer = state as Incoming;
    this.tombstones.set(state.transferId, {scope: {...state.scope}, operationId: state.operationId,
      transferId: state.transferId, kind: state.kind, totalBytes: state.totalBytes, outgoing,
      expires: performance.now() + 60_000, openId: state.openId,
      endId: outgoing ? producer.endId : consumer.end?.requestId,
      sequence: outgoing ? producer.sent : consumer.sequence, bytes: outgoing ? producer.sentBytes : consumer.received,
      acknowledged: outgoing ? producer.acknowledged : 0, initialized: outgoing ? producer.initialized : false,
      endSent: outgoing ? producer.endSent : consumer.end !== undefined,
      fence: state.fence ? {...state.fence} : undefined, cancelId: state.cancelId});
  }
  private async terminalControl(message: Envelope, terminal: Tombstone): Promise<void> {
    const payload = message.payload;
    if (!sameScope(terminal.scope, payload.scope as MediaScope) || terminal.operationId !== payload.operation_id) throw new BulkTransferError();
    const send = (kind: Envelope['kind'], extra: Record<string, unknown>, id: string): Promise<void> =>
      this.bounded(this.options.sendControl(kind, {scope: terminal.scope, operation_id: terminal.operationId,
        transfer_id: terminal.transferId, ...extra}, id));
    if (!terminal.outgoing && message.kind === 'transfer_fence') {
      const fence: Fence = {requestId: message.request_id, count: payload.sequence_count as number,
        bytes: payload.sent_bytes as number, code: payload.code as CancelCode};
      if (fence.count !== terminal.sequence || fence.bytes !== terminal.bytes ||
        terminal.fence && JSON.stringify(terminal.fence) !== JSON.stringify(fence)) throw new BulkTransferError();
      terminal.fence = fence;
      await send('transfer_fence_ack', {sequence_count: fence.count, sent_bytes: fence.bytes, code: fence.code}, fence.requestId);
    } else if (!terminal.outgoing && message.kind === 'transfer_end') {
      if (terminal.bytes !== terminal.totalBytes || payload.total_bytes !== terminal.totalBytes ||
        payload.sequence_count !== terminal.sequence || terminal.endId && message.request_id !== terminal.endId) throw new BulkTransferError();
      terminal.endId = message.request_id;
    } else if (terminal.outgoing && message.kind === 'transfer_credit') {
      const sequence = payload.next_sequence as number, credits = payload.credits as number;
      if (message.request_id !== terminal.openId) throw new BulkTransferError();
      if (!terminal.initialized) {
        if (sequence !== 0 || credits !== MAX_BULK_WINDOW_CHUNKS) throw new BulkTransferError(); terminal.initialized = true;
      } else {
        if (sequence - terminal.acknowledged !== credits || sequence > terminal.sequence || sequence <= terminal.acknowledged) throw new BulkTransferError();
        terminal.acknowledged = sequence; // Already released by the terminal ACK.
      }
    } else if (terminal.outgoing && message.kind === 'transfer_result') {
      if (!terminal.endSent || message.request_id !== terminal.endId || payload.status !== 'complete' ||
        payload.code !== 'ok' || terminal.bytes !== terminal.totalBytes) throw new BulkTransferError();
    } else if (terminal.outgoing && message.kind === 'transfer_cancel') {
      if (terminal.cancelId && terminal.cancelId !== message.request_id) throw new BulkTransferError();
      terminal.cancelId = message.request_id;
      terminal.fence ??= {requestId: randomUUID(), count: terminal.sequence, bytes: terminal.bytes,
        code: payload.code === 'timeout' ? 'timeout' : payload.code === 'invalidated' ? 'invalidated' : 'cancelled'};
      const fence = terminal.fence;
      await send('transfer_fence', {sequence_count: fence.count, sent_bytes: fence.bytes, code: fence.code}, fence.requestId);
    } else if (terminal.outgoing && message.kind === 'transfer_fence_ack') {
      const fence = terminal.fence;
      if (!fence || message.request_id !== fence.requestId || payload.sequence_count !== fence.count ||
        payload.sent_bytes !== fence.bytes || payload.code !== fence.code) throw new BulkTransferError();
    } else throw new BulkTransferError();
  }

  private identity(options: Identity, incoming: boolean): () => boolean {
    this.ensureOpen(); this.prune(); validateScope(options.scope);
    encodeBulkHeader({ kind: options.kind, transfer_id: options.transferId, sequence: 0, length: 1 });
    if (!uuid(options.operationId) || !Number.isSafeInteger(options.totalBytes) || options.totalBytes < 1 || options.totalBytes > BULK_LIMITS[options.kind]) throw new BulkTransferError();
    if (((options.kind === 'talkback_pcm' ? 'hap' : 'python') !== this.options.role) !== incoming) throw new BulkTransferError();
    if (this.incoming.has(options.transferId) || this.outgoing.has(options.transferId) || this.tombstones.has(options.transferId)) throw new BulkTransferError('conflict');
    if (this.tombstones.size + this.incoming.size + this.outgoing.size >= 4096) throw new BulkTransferError('busy');
    const valid = options.valid ?? (() => true); if (!valid()) throw new BulkTransferError('invalidated'); return valid;
  }
  private deadline(id: string): NodeJS.Timeout {
    return setTimeout(() => { void this.cancelTransfer(id, 'timeout').catch(() => {}); }, this.timeoutMs);
  }
  authorizeReceive(options: ReceiveOptions): ReceiveHandle {
    const valid = this.identity(options, true);
    if (this.incoming.size >= 4) throw new BulkTransferError('busy');
    const state: Incoming = { ...options, scope: { ...options.scope }, valid, completion: deferred(), wire: deferred(),
      timer: this.deadline(options.transferId), abort: new AbortController(), cancelled: undefined, terminal: false, fenceWaiting: false,
      openId: undefined, chunkBytes: 0, window: 0, granted: 0, sequence: 0, received: 0, credited: 0, readingBytes: 0, end: undefined,
      finishing: false, onClose: options.onClose, consumers: new Set(), cancelId: undefined, fence: undefined };
    this.incoming.set(options.transferId, state);
    return { wait: () => state.completion.promise, cancel: code => this.cancelTransfer(options.transferId, code) };
  }
  private async bounded<T>(promise: Promise<T>, abortable = true, signal?: AbortSignal): Promise<T> {
    let timer: NodeJS.Timeout | undefined, rejectAbort: ((error: Error) => void) | undefined;
    const abort = (): void => rejectAbort?.(new BulkTransferError('cancelled'));
    const interrupted = new Promise<never>((_, no) => {
      rejectAbort = no; timer = setTimeout(() => no(new BulkTransferError('timeout')), this.timeoutMs);
      if (abortable) { this.abort.signal.addEventListener('abort', abort, {once:true}); if (this.abort.signal.aborted) abort(); }
      signal?.addEventListener('abort', abort, {once:true}); if (signal?.aborted) abort();
    });
    try { return await Promise.race([promise, interrupted]); }
    finally { if (timer) clearTimeout(timer); this.abort.signal.removeEventListener('abort', abort); signal?.removeEventListener('abort', abort); }
  }
  private control(kind: Envelope['kind'], state: Base, extra: Record<string, unknown>, requestId: string, cleanup = false): Promise<void> {
    this.ensureOpen(); if (!cleanup && (!state.valid() || state.cancelled)) throw new BulkTransferError('invalidated');
    return this.bounded(this.options.sendControl(kind, {scope: state.scope, operation_id: state.operationId, transfer_id: state.transferId, ...extra}, requestId));
  }
  private cancelState(state: Base, code: CancelCode): void {
    if (state.terminal || state.cancelled) return;
    state.cancelled = code; state.abort.abort(); state.completion.reject(new BulkTransferError(code)); clearTimeout(state.timer);
  }
  /** Synchronously fences delivery; native cleanup remains owned independently of wire draining. */
  async cancelTransfer(id: string, code: CancelCode = 'cancelled'): Promise<void> {
    if (!['cancelled','invalidated','timeout'].includes(code)) throw new BulkTransferError('invalid-request');
    const incoming = this.incoming.get(id), outgoing = this.outgoing.get(id), state = incoming ?? outgoing;
    if (!state) return;
    if (state.terminal) {if (incoming) await this.cleanupIncoming(incoming); else if (outgoing) await this.cleanupOutgoing(outgoing); return;}
    const alreadyCancelled = state.cancelled !== undefined;
    this.cancelState(state, code);
    if (outgoing) { outgoing.changed.resolve(); this.slotChanged.resolve(); void this.fenceOutgoing(outgoing).catch(error => this.fail(error)); }
    else if (incoming?.openId && !incoming.cancelId) {
      incoming.cancelId = randomUUID();
      void this.control('transfer_cancel', incoming, {code: incoming.cancelled}, incoming.cancelId, true).catch(error => this.fail(error));
    }
    // No OPEN means no peer producer yet. Keep the bounded authorization so a late
    // exact OPEN can be cancelled without granting any binary credit.
    if (incoming && !incoming.openId) {
      if (!alreadyCancelled) state.timer = setTimeout(() => { void this.expireUnopened(incoming).catch(error => this.fail(error)); }, 60_000);
      state.timer?.unref();
      await this.cleanupIncoming(incoming); return;
    }
    this.fenceDeadline(state);
    await this.bounded(state.wire.promise);
    if (incoming) await this.cleanupIncoming(incoming);
    else if (outgoing) await this.cleanupOutgoing(outgoing);
  }
  private fenceDeadline(state: Base): void {
    if (state.terminal || state.fenceWaiting) return;
    state.fenceWaiting = true; clearTimeout(state.timer);
    state.timer = setTimeout(() => { void this.fail(new BulkTransferError('timeout')).catch(() => {}); }, this.timeoutMs);
  }
  private async expireUnopened(state: Incoming): Promise<void> {
    if (state.openId || state.terminal) return;
    state.terminal = true; state.wire.resolve(); await this.cleanupIncoming(state);
  }
  private fenceOutgoing(state: Outgoing): Promise<void> {
    if (state.fencing) return state.fencing;
    state.fencing = (async () => {
      // A queued writer checks the synchronous fence before enqueueing. The
      // barrier captures all bytes that really entered the binary socket.
      await this.bounded(state.openWritten.promise);
      await this.bounded(this.writeTail);
      if (state.terminal && !state.cancelled && !state.cancelId) return;
      state.fence ??= {requestId: randomUUID(), count: state.sent, bytes: state.sentBytes, code: state.cancelled ?? 'cancelled'};
      await this.sendFence(state);
    })();
    return state.fencing;
  }
  private sendFence(state: Outgoing): Promise<void> {
    const fence = state.fence!;
    return this.control('transfer_fence', state, {sequence_count: fence.count, sent_bytes: fence.bytes, code: fence.code}, fence.requestId, true);
  }
  async handleControl(envelope: Envelope): Promise<boolean> {
    if (!envelope.kind.startsWith('transfer_')) return false;
    try {
      this.ensureOpen(); validateDirection(envelope, this.options.role === 'python' ? 'hap' : 'python');
      if (envelope.boot_id !== this.options.bootId) throw new BulkTransferError();
      this.prune(); const payload = envelope.payload, id = payload.transfer_id as string;
      const incomingKind = ['transfer_open','transfer_end','transfer_fence'].includes(envelope.kind);
      const outgoingKind = ['transfer_credit','transfer_result','transfer_cancel','transfer_fence_ack'].includes(envelope.kind);
      const tomb = this.tombstones.get(id);
      const state = incomingKind ? this.incoming.get(id) : outgoingKind ? this.outgoing.get(id) : undefined;
      if (!state && tomb) {await this.terminalControl(envelope, tomb); return true;}
      if (!state || !sameScope(state.scope, payload.scope as MediaScope) || state.operationId !== payload.operation_id) throw new BulkTransferError('invalidated');
      if (envelope.kind === 'transfer_open') {
        const incoming = state as Incoming;
        if (incoming.openId !== undefined || incoming.terminal || payload.kind !== incoming.kind || payload.total_bytes !== incoming.totalBytes) throw new BulkTransferError();
        incoming.openId = envelope.request_id; incoming.chunkBytes = payload.chunk_bytes as number; incoming.window = payload.window_chunks as number;
        if (!incoming.valid() && !incoming.cancelled) this.cancelState(incoming, 'invalidated');
        if (incoming.cancelled) {
          this.fenceDeadline(incoming); incoming.cancelId = randomUUID();
          await this.control('transfer_cancel', incoming, {code: incoming.cancelled}, incoming.cancelId, true);
        } else {
          await this.grantCredit(incoming, incoming.window);
        }
      } else if (envelope.kind === 'transfer_credit') {
        const outgoing = state as Outgoing, sequence = payload.next_sequence as number, credits = payload.credits as number;
        if (envelope.request_id !== outgoing.openId) throw new BulkTransferError();
        if (!outgoing.initialized) {
          if (sequence !== 0 || credits !== MAX_BULK_WINDOW_CHUNKS || outgoing.terminal) throw new BulkTransferError();
          outgoing.initialized = true;
        } else {
          const delta = sequence - outgoing.acknowledged;
          if (delta !== credits || delta < 1 || delta > outgoing.sent - outgoing.acknowledged) throw new BulkTransferError();
          if (!outgoing.terminal) this.releaseSlots(delta);
          outgoing.acknowledged = sequence;
        }
        if (!outgoing.cancelled && !outgoing.terminal) {
          if (outgoing.available + credits + outgoing.sent - outgoing.acknowledged > MAX_BULK_WINDOW_CHUNKS) throw new BulkTransferError();
          outgoing.available += credits;
        }
        outgoing.changed.resolve(); outgoing.changed = deferred();
      } else if (envelope.kind === 'transfer_end') {
        const incoming = state as Incoming;
        if (!incoming.openId || incoming.end || payload.total_bytes !== incoming.totalBytes ||
          (payload.sequence_count as number) < Math.ceil(incoming.totalBytes / incoming.chunkBytes) || (payload.sequence_count as number) > incoming.totalBytes) throw new BulkTransferError();
        incoming.end = {requestId: envelope.request_id, count: payload.sequence_count as number};
        if (incoming.cancelled) {
          if (incoming.fence && incoming.end.count > incoming.fence.count) throw new BulkTransferError();
        } else await this.finishIncoming(incoming);
      } else if (envelope.kind === 'transfer_result') {
        const outgoing = state as Outgoing;
        if (envelope.request_id !== outgoing.endId || !outgoing.endSent || payload.status !== 'complete' || payload.code !== 'ok' || outgoing.acknowledged !== outgoing.sent) throw new BulkTransferError();
        // A receiver that committed success before seeing cancellation may finish
        // normally. Cancellation still keeps this caller's delivery fenced.
        if (!outgoing.terminal && !outgoing.cancelled) {
          outgoing.terminal = true; clearTimeout(state.timer); state.wire.resolve(); state.completion.resolve(); outgoing.changed.resolve();
        }
      } else if (envelope.kind === 'transfer_cancel') {
        const outgoing = state as Outgoing;
        if (outgoing.cancelId && outgoing.cancelId !== envelope.request_id) throw new BulkTransferError();
        outgoing.cancelId = envelope.request_id;
        if (!outgoing.terminal) {
          this.cancelState(outgoing, payload.code === 'timeout' ? 'timeout' : payload.code === 'invalidated' ? 'invalidated' : 'cancelled');
          this.fenceDeadline(outgoing); outgoing.changed.resolve(); this.slotChanged.resolve(); await this.fenceOutgoing(outgoing);
        } else {
          outgoing.fence ??= {requestId: randomUUID(), count: outgoing.sent, bytes: outgoing.sentBytes, code: payload.code === 'timeout' ? 'timeout' : payload.code === 'invalidated' ? 'invalidated' : 'cancelled'};
          await this.sendFence(outgoing);
        }
      } else if (envelope.kind === 'transfer_fence') {
        const incoming = state as Incoming, fence: Fence = {requestId: envelope.request_id,
          count: payload.sequence_count as number, bytes: payload.sent_bytes as number, code: payload.code as CancelCode};
        if (incoming.terminal && (fence.count !== incoming.sequence || fence.bytes !== incoming.received)) throw new BulkTransferError();
        if (!incoming.openId || fence.count < incoming.sequence + (incoming.readingBytes ? 1 : 0) || fence.bytes < incoming.received + incoming.readingBytes || fence.bytes > incoming.totalBytes ||
            fence.count - incoming.sequence > incoming.granted + (incoming.readingBytes ? 1 : 0) || (fence.count === 0) !== (fence.bytes === 0) ||
            fence.count > fence.bytes || fence.bytes > fence.count * incoming.chunkBytes ||
            incoming.fence && JSON.stringify(incoming.fence) !== JSON.stringify(fence)) throw new BulkTransferError();
        incoming.fence = fence;
        if (!incoming.terminal) {this.cancelState(incoming, fence.code); this.fenceDeadline(incoming);}
        await this.finishFence(incoming);
      } else if (envelope.kind === 'transfer_fence_ack') {
        const outgoing = state as Outgoing, fence = outgoing.fence;
        if (!fence || envelope.request_id !== fence.requestId || payload.sequence_count !== fence.count || payload.sent_bytes !== fence.bytes || payload.code !== fence.code) throw new BulkTransferError();
        if (!outgoing.terminal) {
          outgoing.terminal = true; clearTimeout(outgoing.timer); this.releaseSlots(outgoing.sent - outgoing.acknowledged);
          outgoing.wire.resolve(); outgoing.changed.resolve();
          void this.cleanupOutgoing(outgoing).catch(() => {});
        }
      }
      return true;
    } catch (error) { await this.fail(error as Error); throw error; }
  }
  private releaseSlots(count: number): void {
    this.slots += count; if (this.slots > MAX_BULK_WINDOW_CHUNKS) throw new BulkTransferError();
    this.slotChanged.resolve(); this.slotChanged = deferred();
  }
  async send(options: SendOptions): Promise<void> {
    const valid = this.identity(options, false);
    if (this.outgoing.size >= 4) throw new BulkTransferError('busy');
    const state: Outgoing = {...options, scope: {...options.scope}, valid, completion: deferred(), wire: deferred(),
      timer: this.deadline(options.transferId), abort: new AbortController(), cancelled: undefined, terminal: false, fenceWaiting: false,
      openId: randomUUID(), endId: randomUUID(), available: 0, sent: 0, sentBytes: 0, acknowledged: 0, initialized: false,
      endSent: false, changed: deferred(), fence: undefined, cancelId: undefined, openWritten: deferred()};
    this.outgoing.set(options.transferId, state); const iterator = options.source[Symbol.asyncIterator](); state.iterator = iterator;
    try {
      try {
        await this.control('transfer_open', state, {kind: options.kind, total_bytes: options.totalBytes, chunk_bytes: Math.min(MAX_BULK_CHUNK_BYTES, options.totalBytes), window_chunks: MAX_BULK_WINDOW_CHUNKS}, state.openId);
        state.openWritten.resolve();
      } catch (error) { state.openWritten.reject(error as Error); await this.fail(error as Error); throw error; }
      while (true) {
        this.checkOutgoing(state);
        while (!state.available) { this.checkOutgoing(state); await this.bounded(state.changed.promise, true, state.abort.signal); }
        while (!this.slots) { this.checkOutgoing(state); await this.bounded(this.slotChanged.promise, true, state.abort.signal); }
        --this.slots; let reserved = true;
        try {
          const pending = iterator.next(); state.pendingNext = pending;
          void pending.finally(() => {if (state.pendingNext === pending) state.pendingNext = undefined;}).catch(() => {});
          const next = await this.bounded(pending, true, state.abort.signal); this.checkOutgoing(state); if (next.done) break;
          const chunk = next.value;
          if (!(chunk instanceof Uint8Array) || chunk.byteLength < 1 || chunk.byteLength > Math.min(MAX_BULK_CHUNK_BYTES, options.totalBytes) || state.sentBytes + chunk.byteLength > options.totalBytes) throw new BulkTransferError();
          const previous = this.writeTail, turn = deferred(); this.writeTail = turn.promise;
          try {
            await this.bounded(previous); this.checkOutgoing(state);
            const header = encodeBulkHeader({kind: options.kind, transfer_id: options.transferId, sequence: state.sent, length: chunk.byteLength});
            --state.available; ++state.sent; state.sentBytes += chunk.byteLength; reserved = false;
            // The frontier is committed exactly when the write is enqueued.
            await this.bounded(new Promise<void>((yes, no) => {this.socket.write(Buffer.concat([header, chunk]), error => error ? no(error) : yes());}));
          } finally {turn.resolve();}
        } finally {if (reserved) this.releaseSlots(1);}
      }
      if (state.sentBytes !== options.totalBytes || !state.sent) throw new BulkTransferError();
      this.checkOutgoing(state); state.endSent = true;
      await this.control('transfer_end', state, {sequence_count: state.sent, total_bytes: options.totalBytes}, state.endId);
      await this.bounded(state.completion.promise, true, state.abort.signal);
    } catch (error) {
      if (!this.closing && !state.cancelled && error instanceof BulkTransferError && ['timeout','invalidated'].includes(error.code)) {
        void this.cancelTransfer(state.transferId, error.code as CancelCode).catch(() => {});
      } else if (!this.closing && !state.cancelled) await this.fail(error as Error);
      throw state.cancelled ? new BulkTransferError(state.cancelled) : error;
    } finally {
      try {await this.cleanupOutgoing(state);}
      catch (error) {if (!state.cancelled && !this.closing) await this.fail(error as Error); throw error;}
    }
  }
  private checkOutgoing(state: Outgoing): void {
    this.ensureOpen(); if (state.cancelled) throw new BulkTransferError(state.cancelled);
    if (!state.valid()) throw new BulkTransferError('invalidated');
  }
  private async readable(idle: boolean): Promise<void> {
    let cleanup = (): void => {};
    const promise = new Promise<void>((yes, no) => {
      const ready = (): void => {cleanup(); yes();}, end = (): void => {cleanup(); no(new BulkTransferError('cancelled'));}, error = (cause: Error): void => {cleanup(); no(cause);};
      cleanup = () => {this.socket.off('readable', ready); this.socket.off('end', end); this.socket.off('close', end); this.socket.off('error', error); this.abort.signal.removeEventListener('abort', end);};
      this.socket.once('readable', ready); this.socket.once('end', end); this.socket.once('close', end); this.socket.once('error', error); this.abort.signal.addEventListener('abort', end, {once:true});
    });
    try {if (idle) await promise; else await this.bounded(promise);} finally {cleanup();}
  }
  private async readExact(length: number, idle = false): Promise<Buffer> {
    const output = Buffer.allocUnsafe(length); let offset = 0;
    while (offset < length) {
      this.ensureOpen();
      // read(n) waits for all n bytes even when a smaller fragment is buffered.
      // Consume that fragment before waiting, otherwise readable events can
      // spin indefinitely on the same insufficient buffered bytes.
      const available = Math.min(length - offset, this.socket.readableLength);
      const chunk = available ? this.socket.read(available) as Buffer | null : null;
      if (chunk) {chunk.copy(output, offset); offset += chunk.length; continue;}
      if (this.socket.readableEnded || this.socket.destroyed) throw new BulkTransferError('cancelled');
      await this.readable(idle && offset === 0);
    }
    return output;
  }
  private async readLoop(): Promise<void> {
    try {
      while (!this.closing) {
        const header = decodeBulkHeader(await this.readExact(BULK_HEADER_BYTES, true)), state = this.incoming.get(header.transfer_id);
        if (!state || !state.openId || state.terminal || header.kind !== state.kind || header.sequence !== state.sequence || state.granted < 1 ||
          header.length > state.chunkBytes || state.received + header.length > state.totalBytes || state.fence &&
          (header.sequence >= state.fence.count || state.received + header.length > state.fence.bytes)) throw new BulkTransferError();
        --state.granted; state.readingBytes = header.length; const chunk = await this.readExact(header.length); state.readingBytes = 0; state.received += chunk.length; ++state.sequence;
        if (!state.valid() && !state.cancelled) void this.cancelTransfer(state.transferId, 'invalidated').catch(() => {});
        if (!state.cancelled) {
          const consumer = Promise.resolve().then(() => state.sink(chunk, state.abort.signal));
          this.consumers.add(consumer); state.consumers.add(consumer);
          void consumer.finally(() => {this.consumers.delete(consumer); state.consumers.delete(consumer);}).catch(() => {});
          try {await this.bounded(consumer, true, state.abort.signal);}
          catch (error) {if (!state.cancelled) throw error;}
          if (!state.valid() && !state.cancelled) void this.cancelTransfer(state.transferId, 'invalidated').catch(() => {});
          if (!state.cancelled) await this.grantCredit(state, 1);
        }
        if (state.cancelled) await this.finishFence(state); else await this.finishIncoming(state);
      }
    } catch (error) {if (!this.closing) await this.fail(error as Error);}
  }
  private async finishFence(state: Incoming): Promise<void> {
    const fence = state.fence; if (!fence || state.sequence < fence.count) return;
    if (state.sequence !== fence.count || state.received !== fence.bytes) throw new BulkTransferError();
    await this.control('transfer_fence_ack', state, {sequence_count: fence.count, sent_bytes: fence.bytes, code: fence.code}, fence.requestId, true);
    if (!state.terminal) {
      state.terminal = true; clearTimeout(state.timer); state.wire.resolve();
      void this.cleanupIncoming(state).catch(() => {});
    }
  }
  private async grantCredit(state: Incoming, credits: number): Promise<void> {
    if (state.cancelled || state.terminal) return;
    if (!state.valid()) {void this.cancelTransfer(state.transferId, 'invalidated').catch(() => {}); return;}
    // Validity and the granted frontier commit synchronously. A later fence
    // drains this already committed credit; it must not retract a partial write.
    state.granted += credits;
    await this.control('transfer_credit', state, {next_sequence: state.sequence, credits}, state.openId!, true);
    state.credited = state.sequence;
  }
  private closeConsumer(state: Incoming, code: string): Promise<void> {
    if (!state.closingConsumer && state.onClose) {
      const callback = state.onClose;
      state.closingConsumer = Promise.resolve().then(() => callback(code)).then(() => {state.onClose = undefined;})
        .catch(error => {state.closingConsumer = undefined; throw error;});
      void state.closingConsumer.catch(() => {});
    }
    return state.closingConsumer ?? Promise.resolve();
  }
  private finishIncoming(state: Incoming): void {
    if (!state.end || state.received < state.totalBytes || state.credited !== state.sequence || state.finishing || state.cancelled || state.terminal) return;
    state.finishing = true;
    if (state.received !== state.totalBytes || state.sequence !== state.end.count) throw new BulkTransferError();
    // Native close may be slow. It belongs to this transfer and cannot occupy
    // the binary reader or prevent a cancellation fence from draining.
    void (async () => {
      try {await this.bounded(this.closeConsumer(state, 'ok'), true, state.abort.signal);}
      catch (error) {
        if (!state.cancelled && !this.closing) void this.cancelTransfer(state.transferId,
          error instanceof BulkTransferError && error.code === 'timeout' ? 'timeout' : 'cancelled').catch(() => {});
        return;
      }
      if (state.cancelled || !state.valid()) {void this.cancelTransfer(state.transferId, 'invalidated').catch(() => {}); return;}
      try {await this.control('transfer_result', state, {status: 'complete', code: 'ok'}, state.end!.requestId);}
      catch (error) {
        if (state.cancelled || error instanceof BulkTransferError && error.code === 'invalidated') {
          void this.cancelTransfer(state.transferId, 'invalidated').catch(() => {}); return;
        }
        throw error;
      }
      state.terminal = true; clearTimeout(state.timer); state.wire.resolve(); state.completion.resolve();
      this.remember(state, false); this.incoming.delete(state.transferId);
    })().catch(error => {void this.fail(error).catch(() => {});});
  }

  private cleanupIncoming(state: Incoming): Promise<void> {
    if (!state.cleanup) {
      state.cleanup = (async () => {
        await this.bounded(Promise.allSettled([...state.consumers]).then(() => {}), false);
        await this.bounded(this.closeConsumer(state, state.cancelled ?? 'cancelled'), false);
        if (state.terminal) {this.remember(state, false); this.incoming.delete(state.transferId);}
      })().finally(() => {state.cleanup = undefined;});
      void state.cleanup.catch(() => {});
    }
    return state.cleanup;
  }
  private cleanupOutgoing(state: Outgoing): Promise<void> {
    if (!state.cleanup) {
      state.cleanup = (async () => {
        await this.bounded(this.closeIterator(state), false);
        if (state.terminal) {this.remember(state, true); this.outgoing.delete(state.transferId);}
      })().finally(() => {state.cleanup = undefined;});
      void state.cleanup.catch(() => {});
    }
    return state.cleanup;
  }
  private async fail(error: Error): Promise<void> {
    if (this.closing) return; this.failureValue = error;
    try {await this.close(error instanceof BulkTransferError ? error.code : 'protocol-error');}
    finally {await this.options.onFailure?.(error);}
  }
  async close(code = 'cancelled'): Promise<void> {
    if (this.closeWork) return this.closeWork;
    this.closing = true; this.abort.abort(); this.socket.destroy();
    for (const state of [...this.incoming.values(), ...this.outgoing.values()]) {
      clearTimeout(state.timer); state.abort.abort(); state.wire.reject(new BulkTransferError(code)); if ('changed' in state) state.changed.resolve();
    }
    this.slotChanged.resolve(); this.closeWork = this.retire(code);
    try {await this.closeWork;} finally {this.closeWork = undefined;}
  }
  private closeIterator(state: Outgoing): Promise<void> {
    if (!state.iteratorClosing) {
      state.iteratorClosing = (async () => {
        const pending = state.pendingNext, returned = state.iterator?.return?.();
        const results = await Promise.allSettled([pending, returned]);
        if (results[1]?.status === 'rejected') throw new BulkTransferError('source-failed');
      })().catch(error => {state.iteratorClosing = undefined; throw error;});
      void state.iteratorClosing.catch(() => {});
    }
    return state.iteratorClosing;
  }
  private async retire(code: string): Promise<void> {
    const states = [...this.incoming.values(), ...this.outgoing.values()]; let failed = false;
    try {
      await this.bounded(Promise.allSettled([...this.consumers]).then(() => {}), false); const failures: Error[] = [];
      for (const [id, state] of this.incoming) {
        try {await this.bounded(this.closeConsumer(state, code), false); this.incoming.delete(id);}
        catch (error) {failures.push(error as Error);}
      }
      for (const [id, state] of this.outgoing) {
        try {await this.bounded(this.closeIterator(state), false); this.outgoing.delete(id);}
        catch (error) {failures.push(error as Error);}
      }
      if (failures.length) throw new BulkTransferError('source-failed');
    } catch (error) {failed = true; throw error;}
    finally {for (const state of states) state.completion.reject(new BulkTransferError(failed ? 'source-failed' : code));}
  }
}
