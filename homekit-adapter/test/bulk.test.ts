import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { duplexPair } from 'node:stream';
import { test } from 'node:test';
import { BulkChannel, BulkTransferError, type SendOptions } from '../src/bulk.js';
import { encodeBulkHeader, type Envelope, type Kind } from '../src/protocol.js';
const tick = (): Promise<void> => new Promise(resolve => setImmediate(resolve));
const identity = (totalBytes = 6, kind: SendOptions['kind'] = 'snapshot') => ({
  scope: { profile_id: randomUUID(), camera_id: 'camera', account_generation: 1, device_generation: 1, privacy_generation: 1 },
  operationId: randomUUID(), transferId: randomUUID(), kind, totalBytes,
});
async function* source(...values: Buffer[]): AsyncGenerator<Buffer> { for (const value of values) yield value; }
function pair(timeoutMs = 1000) {
  const streams = duplexPair({ highWaterMark: 65536 }), bootId = randomUUID();
  const controls: Envelope[][] = [[], []];
  let left!: BulkChannel, right!: BulkChannel;
  const tails = [Promise.resolve(), Promise.resolve()];
  const send = async (index: number, kind: Kind, payload: Record<string, unknown>, requestId: string = randomUUID()): Promise<void> => {
    const message: Envelope = { v: 1, kind, payload, request_id: requestId, boot_id: bootId };
    controls[index]!.push(message);
    tails[index] = tails[index]!.then(async () => { await (index === 0 ? right : left).handleControl(message); });
    void tails[index]!.catch(() => {});
  };
  left = new BulkChannel(streams[0], { bootId, role: 'python', timeoutMs, sendControl: (...args) => send(0, ...args) });
  right = new BulkChannel(streams[1], { bootId, role: 'hap', timeoutMs, sendControl: (...args) => send(1, ...args) });
  left.start(); right.start();
  return { left, right, streams, controls, bootId, close: async () => { await Promise.allSettled([left.close(), right.close()]); } };
}

test('binary transfer waits for sink consumption before credits and completion', async () => {
  const p = pair(), info = identity(), output: Buffer[] = [];
  let release!: () => void, entered!: () => void;
  const gate = new Promise<void>(yes => { release = yes; }), started = new Promise<void>(yes => { entered = yes; });
  const handle = p.right.authorizeReceive({ ...info, sink: async bytes => { entered(); await gate; output.push(bytes); } });
  const sending = p.left.send({ ...info, source: source(Buffer.from('abc'), Buffer.from('def')) });
  await started; assert.equal(p.controls[1]!.length, 1); release();
  await Promise.all([sending, handle.wait()]);
  assert.equal(Buffer.concat(output).toString(), 'abcdef');
  assert.deepEqual(p.left.counts, [0, 0]); assert.deepEqual(p.right.counts, [0, 0]);
  await p.close();
});

test('two global outstanding chunks stop producer prefetch', async () => {
  const p = pair(), info = identity(6 * 65536), produced: number[] = [];
  let release!: () => void, entered!: () => void;
  const gate = new Promise<void>(yes => { release = yes; }), started = new Promise<void>(yes => { entered = yes; });
  p.right.authorizeReceive({ ...info, sink: async () => { entered(); await gate; } });
  async function* chunks(): AsyncGenerator<Buffer> { for (let i = 0; i < 6; i++) { produced.push(i); yield Buffer.alloc(65536, i); } }
  const sending = p.left.send({ ...info, source: chunks() });
  await started; await tick(); assert.deepEqual(produced, [0, 1]); release(); await sending; await p.close();
});

test('reverse-direction PCM is limited to a single finite frame', async () => {
  const p = pair(), info = identity(1920, 'talkback_pcm'), output: Buffer[] = [];
  const handle = p.left.authorizeReceive({ ...info, sink: async bytes => { output.push(bytes); } });
  await p.right.send({ ...info, source: source(Buffer.alloc(1920, 4)) }); await handle.wait();
  assert.equal(output[0]!.length, 1920);
  assert.throws(() => p.left.authorizeReceive({ ...identity(1921, 'talkback_pcm'), sink: async () => {} }), BulkTransferError);
  await p.close();
});
for (const mutation of ['boot', 'scope', 'operation', 'size', 'kind', 'id']) {
  test(`metadata cannot authorize incoming bytes: ${mutation}`, async () => {
    const p = pair(), info = identity();
    p.right.authorizeReceive({ ...info, sink: async () => assert.fail('unauthorized delivery') });
    const payload = { scope: { ...info.scope }, operation_id: info.operationId, transfer_id: info.transferId,
      kind: 'snapshot', total_bytes: 6, chunk_bytes: 6, window_chunks: 2 };
    let bootId = p.bootId;
    if (mutation === 'boot') bootId = randomUUID();
    else if (mutation === 'scope') payload.scope.privacy_generation++;
    else if (mutation === 'operation') payload.operation_id = randomUUID();
    else if (mutation === 'size') payload.total_bytes++;
    else if (mutation === 'kind') payload.kind = 'recording_init';
    else payload.transfer_id = randomUUID();
    await assert.rejects(p.right.handleControl({ v: 1, kind: 'transfer_open', payload, boot_id: bootId, request_id: randomUUID() }));
    assert.equal(p.right.closed, true); await p.close();
  });
}

test('unsolicited header fails before advertised payload allocation', async () => {
  const p = pair();
  p.streams[0].write(encodeBulkHeader({ kind: 'snapshot', transfer_id: randomUUID(), sequence: 0, length: 65536 }));
  for (let i = 0; i < 20 && !p.right.closed; i++) await tick();
  assert.equal(p.right.closed, true); assert.deepEqual(p.right.counts, [0, 0]); await p.close();
});
for (const [chunks, size] of [[[Buffer.alloc(65537)], 65537], [[Buffer.alloc(0)], 1], [[Buffer.from('aa')], 1], [[Buffer.from('a')], 2]] as const) {
  test(`producer size mismatch fails (${chunks[0]!.length}/${size})`, async () => {
    const p = pair(), info = identity(size); p.right.authorizeReceive({ ...info, sink: async () => {} });
    await assert.rejects(p.left.send({ ...info, source: source(...chunks) }));
    assert.equal(p.left.closed, true); await p.close();
  });
}

test('authorization capacity and whole-transfer timeout stay bounded', async () => {
  const p = pair(20), handles = Array.from({ length: 4 }, () => p.right.authorizeReceive({ ...identity(), sink: async () => {} }));
  assert.throws(() => p.right.authorizeReceive({ ...identity(), sink: async () => {} }), /busy/);
  await assert.rejects(handles[0]!.wait(), /timeout/); await p.right.close();
  assert.deepEqual(p.right.counts, [0, 0]); await p.close();
});

test('failed owned cleanup remains reserved and is retryable', async () => {
  const p = pair(), codes: string[] = [];
  p.right.authorizeReceive({ ...identity(), sink: async () => {}, onClose: async code => {
    codes.push(code); if (codes.length === 1) throw new Error('synthetic failure');
  } });
  await assert.rejects(p.right.close(), /source-failed/); assert.deepEqual(p.right.counts, [1, 0]);
  await p.right.close(); assert.deepEqual(p.right.counts, [0, 0]); assert.deepEqual(codes, ['cancelled', 'cancelled']); await p.close();
});

test('close aborts and joins a cooperative consumer before resolving', async () => {
  const p = pair(), info = identity(1), states: string[] = [];
  let entered!: () => void; const started = new Promise<void>(yes => { entered = yes; });
  p.right.authorizeReceive({ ...info, sink: async (_, signal) => {
    entered(); await new Promise<void>(yes => { signal.addEventListener('abort', () => { states.push('aborted'); yes(); }, { once: true }); });
  }, onClose: async () => { states.push('closed'); } });
  const sending = p.left.send({ ...info, source: source(Buffer.from('a')) }); void sending.catch(() => {});
  await started; await p.right.close(); assert.deepEqual(states, ['aborted', 'closed']);
  await assert.rejects(sending); await p.close();
});

for (const mutation of ['sequence', 'kind', 'chunk-size', 'total-size']) {
  test(`registered transfer rejects bad binary header: ${mutation}`, async () => {
    const streams = duplexPair(), bootId = randomUUID(), info = identity(), writes: string[] = [];
    const receiver = new BulkChannel(streams[1], { bootId, role: 'hap', sendControl: async kind => { writes.push(kind); } });
    receiver.start(); receiver.authorizeReceive({ ...info, sink: async () => assert.fail('bad payload delivered') });
    await receiver.handleControl({ v: 1, kind: 'transfer_open', boot_id: bootId, request_id: randomUUID(),
      payload: { scope: info.scope, operation_id: info.operationId, transfer_id: info.transferId, kind: 'snapshot', total_bytes: 6, chunk_bytes: 3, window_chunks: 2 } });
    streams[0].write(encodeBulkHeader({ kind: mutation === 'kind' ? 'recording_init' : 'snapshot', transfer_id: info.transferId,
      sequence: mutation === 'sequence' ? 1 : 0, length: mutation === 'chunk-size' ? 4 : mutation === 'total-size' ? 7 : 3 }));
    for (let i = 0; i < 20 && !receiver.closed; i++) await tick();
    assert.equal(receiver.closed, true); assert.deepEqual(writes, ['transfer_credit']); await receiver.close(); streams[0].destroy();
  });
}

test('scope revoked during success cleanup cannot emit complete result', async () => {
  const p = pair(), info = identity(1); let current = true;
  const handle = p.right.authorizeReceive({ ...info, valid: () => current, sink: async () => {}, onClose: async () => { current = false; } });
  const sending = p.left.send({ ...info, source: source(Buffer.from('a')) }); void sending.catch(() => {});
  await assert.rejects(handle.wait()); await assert.rejects(sending);
  assert.equal(p.controls[1]!.some(message => message.kind === 'transfer_result'), false); await p.close();
});

test('unresolved producer promises keep a visible failed retirement reservation', async () => {
  const p = pair(20), info = identity(1); let finishNext!: () => void, finishReturn!: () => void;
  const producer: AsyncIterable<Buffer> = { [Symbol.asyncIterator]() { return {
    next: () => new Promise<IteratorResult<Buffer>>(yes => { finishNext = () => yes({ done: true, value: undefined }); }),
    return: () => new Promise<IteratorResult<Buffer>>(yes => { finishReturn = () => yes({ done: true, value: undefined }); }),
  }; } };
  p.right.authorizeReceive({ ...info, sink: async () => {} });
  const sending = p.left.send({ ...info, source: producer }); void sending.catch(() => {});
  while (!finishNext) await tick();
  await assert.rejects(p.left.close(), /timeout|source-failed/); assert.deepEqual(p.left.counts, [0, 1]);
  finishNext(); finishReturn(); await assert.rejects(sending);
  await p.left.close(); assert.deepEqual(p.left.counts, [0, 0]); await p.close();
});

for (const mutation of ['replay', 'future-ack', 'request-id']) {
  test(`credits cannot inflate sender window: ${mutation}`, async () => {
    const p = pair(), info = identity(); let entered!: () => void;
    const started = new Promise<void>(yes => { entered = yes; });
    p.right.authorizeReceive({ ...info, sink: async (_, signal) => {
      entered(); await new Promise<void>(yes => signal.addEventListener('abort', () => yes(), { once: true }));
    } });
    const sending = p.left.send({ ...info, source: source(Buffer.from('abc'), Buffer.from('def')) }); void sending.catch(() => {});
    await started; const credit = structuredClone(p.controls[1]![0]!);
    if (mutation === 'future-ack') { credit.payload.next_sequence = 100; credit.payload.credits = 1; }
    else if (mutation === 'request-id') credit.request_id = randomUUID();
    await assert.rejects(p.left.handleControl(credit)); await assert.rejects(sending); assert.equal(p.left.closed, true); await p.close();
  });
}

test('two outgoing transfers share one global outstanding window', async () => {
  const p = pair(), infos = [identity(3 * 65536), identity(3 * 65536)], produced: number[] = [];
  let entered!: () => void, release!: () => void;
  const started = new Promise<void>(yes => { entered = yes; }), gate = new Promise<void>(yes => { release = yes; });
  async function* chunks(label: number): AsyncGenerator<Buffer> { for (let i = 0; i < 3; i++) { produced.push(label); yield Buffer.alloc(65536); } }
  for (const info of infos) p.right.authorizeReceive({ ...info, sink: async () => { entered(); await gate; } });
  const tasks = infos.map((info, index) => p.left.send({ ...info, source: chunks(index) }));
  await started; await tick(); assert.equal(produced.length, 2); release(); await Promise.all(tasks); await p.close();
});

test('consumer ignoring abort has a finite failed close and retained reservation', async () => {
  const p = pair(20), info = identity(1); let entered!: () => void, release!: () => void;
  const started = new Promise<void>(yes => { entered = yes; }), gate = new Promise<void>(yes => { release = yes; });
  p.right.authorizeReceive({ ...info, sink: async () => { entered(); await gate; } });
  const sending = p.left.send({ ...info, source: source(Buffer.from('a')) }); void sending.catch(() => {});
  await started; await assert.rejects(p.right.close(), /timeout|source-failed/); assert.deepEqual(p.right.counts, [1, 0]);
  release(); await tick(); await p.right.close(); assert.deepEqual(p.right.counts, [0, 0]); await assert.rejects(sending); await p.close();
});
