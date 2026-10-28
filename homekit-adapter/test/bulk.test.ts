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
  const controls: Envelope[][] = [[], []], blocked = new Set<string>();
  let left!: BulkChannel, right!: BulkChannel;
  const tails = [Promise.resolve(), Promise.resolve()];
  const send = async (index: number, kind: Kind, payload: Record<string, unknown>, requestId: string = randomUUID()): Promise<void> => {
    const message: Envelope = { v: 1, kind, payload, request_id: requestId, boot_id: bootId };
    controls[index]!.push(message);
    if (blocked.has(`${index}:${kind}`)) return;
    tails[index] = tails[index]!.then(async () => { await (index === 0 ? right : left).handleControl(message); });
    void tails[index]!.catch(() => {});
  };
  left = new BulkChannel(streams[0], { bootId, role: 'python', timeoutMs, sendControl: (...args) => send(0, ...args) });
  right = new BulkChannel(streams[1], { bootId, role: 'hap', timeoutMs, sendControl: (...args) => send(1, ...args) });
  left.start(); right.start();
  return { left, right, streams, controls, bootId, block: (index:number,kind:Kind) => blocked.add(`${index}:${kind}`), close: async () => { await Promise.allSettled([left.close(), right.close()]); } };
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

test('cancelling one consumer drains its credited chunks and preserves other transfers', async () => {
  const p = pair(), cancelled = identity(6), healthy = identity(6), delivered: string[] = [];
  let entered!: () => void; const started = new Promise<void>(yes => {entered = yes;});
  const handle = p.right.authorizeReceive({...cancelled, sink: async (_, signal) => {
    entered(); await new Promise<void>(yes => signal.addEventListener('abort', () => yes(), {once:true}));
  }});
  const sender = p.left.send({...cancelled, source: source(Buffer.from('abc'), Buffer.from('def'))}); void sender.catch(() => {});
  await started; await handle.cancel(); await assert.rejects(handle.wait(), /cancelled/); await assert.rejects(sender, /cancelled/);
  const next = p.right.authorizeReceive({...healthy, sink: async bytes => {delivered.push(bytes.toString());}});
  await p.left.send({...healthy, source: source(Buffer.from('abcdef'))}); await next.wait();
  assert.deepEqual(delivered, ['abcdef']); assert.equal(p.left.closed, false); assert.equal(p.right.closed, false);
  const fence = p.controls[0]!.find(item => item.kind === 'transfer_fence')!;
  const ack = p.controls[1]!.find(item => item.kind === 'transfer_fence_ack')!;
  assert.equal(ack.request_id, fence.request_id); assert.deepEqual(ack.payload, fence.payload);
  await p.close();
});

test('cancel before OPEN retains only its authorization and grants no initial credit', async () => {
  const p = pair(), info = identity(1); let delivered = false;
  const handle = p.right.authorizeReceive({...info, sink: async () => {delivered = true;}});
  await handle.cancel(); assert.equal(p.controls[1]!.length, 0);
  await assert.rejects(p.left.send({...info, source: source(Buffer.from('a'))}), /cancelled/);
  await assert.rejects(handle.wait(), /cancelled/); await tick();
  assert.equal(delivered, false); assert.equal(p.controls[1]!.some(item => item.kind === 'transfer_credit'), false);
  assert.equal(p.left.closed, false); assert.equal(p.right.closed, false); await p.close();
});

test('uncooperative sink cannot delay wire fence or consume another transfer', async () => {
  const p = pair(30), info = identity(1), nextInfo = identity(1);
  let release!: () => void, entered!: () => void;
  const gate = new Promise<void>(yes => {release = yes;}), started = new Promise<void>(yes => {entered = yes;});
  const handle = p.right.authorizeReceive({...info, sink: async () => {entered(); await gate;}});
  const sending = p.left.send({...info, source: source(Buffer.from('a'))}); void sending.catch(() => {});
  await started; await assert.rejects(handle.cancel(), /timeout/); await assert.rejects(sending);
  assert.equal(p.controls[1]!.some(item => item.kind === 'transfer_fence_ack'), true);
  assert.deepEqual(p.right.counts, [1, 0]); assert.equal(p.right.closed, false);
  const next = p.right.authorizeReceive({...nextInfo, sink: async () => {}});
  await p.left.send({...nextInfo, source: source(Buffer.from('b'))}); await next.wait();
  release(); await tick(); await handle.cancel(); assert.deepEqual(p.right.counts, [0, 0]); await p.close();
});

test('pending producer next cannot delay fence and remains owned until cleanup succeeds', async () => {
  const p = pair(30), info = identity(1); let finishNext!: () => void, finishReturn!: () => void;
  const iterator: AsyncIterable<Buffer> = {[Symbol.asyncIterator]() {return {
    next: () => new Promise<IteratorResult<Buffer>>(yes => {finishNext = () => yes({done:true,value:undefined});}),
    return: () => new Promise<IteratorResult<Buffer>>(yes => {finishReturn = () => yes({done:true,value:undefined});}),
  };}};
  const handle = p.right.authorizeReceive({...info, sink: async () => assert.fail('cancelled source delivered')});
  const sending = p.left.send({...info, source: iterator}); void sending.catch(() => {});
  while (!finishNext) await tick();
  const cancel = p.left.cancelTransfer(info.transferId); void cancel.catch(() => {});
  for (let index=0; index<50 && !p.controls[1]!.some(item => item.kind === 'transfer_fence_ack');index++) await tick();
  assert.equal(p.controls[1]!.some(item => item.kind === 'transfer_fence_ack'), true);
  await assert.rejects(cancel, /timeout/); await assert.rejects(sending, /timeout/);
  assert.deepEqual(p.left.counts, [0,1]); assert.equal(p.left.closed, false);
  finishNext(); finishReturn(); await tick(); await p.left.cancelTransfer(info.transferId);
  assert.deepEqual(p.left.counts,[0,0]); await assert.rejects(handle.wait()); await p.close();
});

for (const field of ['request_id','sequence_count','sent_bytes','code','scope','operation_id']) {
  test(`fence ACK must exactly echo its owned producer fence: ${field}`, async () => {
    const p=pair(), info=identity(1); let entered!:()=>void;
    const started=new Promise<void>(yes=>{entered=yes;}); p.block(1,'transfer_fence_ack');
    p.right.authorizeReceive({...info,sink:async(_,signal)=>{entered();await new Promise<void>(yes=>signal.addEventListener('abort',()=>yes(),{once:true}));}});
    const sending=p.left.send({...info,source:source(Buffer.from('a'))});void sending.catch(()=>{});await started;
    const cancel=p.left.cancelTransfer(info.transferId);void cancel.catch(()=>{});
    while(!p.controls[1]!.some(item=>item.kind==='transfer_fence_ack'))await tick();
    const ack=structuredClone(p.controls[1]!.find(item=>item.kind==='transfer_fence_ack')!);
    if(field==='request_id')ack.request_id=randomUUID();
    else if(field==='scope')(ack.payload.scope as typeof info.scope).privacy_generation++;
    else if(field==='operation_id')ack.payload.operation_id=randomUUID();
    else if(field==='code')ack.payload.code='timeout';
    else ack.payload[field]=(ack.payload[field] as number)+1;
    await assert.rejects(p.left.handleControl(ack));
    assert.equal(p.left.closed,true);await assert.rejects(cancel);await assert.rejects(sending);await p.close();
  });
}

test('unknown remote cancellation cannot create a producer or suppress a future transfer', async () => {
  const p=pair(),info=identity();
  await p.left.cancelTransfer(info.transferId);assert.equal(p.controls[0]!.length,0);
  await assert.rejects(p.left.handleControl({v:1,kind:'transfer_cancel',boot_id:p.bootId,request_id:randomUUID(),payload:{
    scope:info.scope,operation_id:info.operationId,transfer_id:info.transferId,code:'cancelled'}}));
  assert.equal(p.left.closed,true);await p.close();
});

test('completed tombstones acknowledge an exact late cancellation without releasing wire slots twice',async()=>{
  const p=pair(),info=identity(1),handle=p.right.authorizeReceive({...info,sink:async()=>{}});
  await p.left.send({...info,source:source(Buffer.from('a'))});await handle.wait();
  const cancel:Envelope={v:1,kind:'transfer_cancel',boot_id:p.bootId,request_id:randomUUID(),payload:{scope:info.scope,operation_id:info.operationId,transfer_id:info.transferId,code:'cancelled'}};
  await p.left.handleControl(cancel);
  while(!p.controls[1]!.some(message=>message.kind==='transfer_fence_ack'))await tick();
  await p.left.handleControl(p.controls[1]!.find(message=>message.kind==='transfer_fence_ack')!);
  const next=identity(1),nextHandle=p.right.authorizeReceive({...next,sink:async()=>{}});
  await p.left.send({...next,source:source(Buffer.from('b'))});await nextHandle.wait();
  assert.equal(p.left.closed,false);assert.equal(p.right.closed,false);await p.close();
});

test('normal result racing cancellation cannot skip the owned fence ACK handshake',async()=>{
  const p=pair(),info=identity(1);p.block(1,'transfer_result');p.block(1,'transfer_fence_ack');
  const handle=p.right.authorizeReceive({...info,sink:async()=>{}});
  const sending=p.left.send({...info,source:source(Buffer.from('a'))});void sending.catch(()=>{});
  while(!p.controls[1]!.some(message=>message.kind==='transfer_result'))await tick();await handle.wait();
  let resolved=false;const cancel=p.left.cancelTransfer(info.transferId).then(()=>{resolved=true;});void cancel.catch(()=>{});
  while(!p.controls[1]!.some(message=>message.kind==='transfer_fence_ack'))await tick();
  await p.left.handleControl(p.controls[1]!.find(message=>message.kind==='transfer_result')!);await tick();assert.equal(resolved,false);
  await p.left.handleControl(p.controls[1]!.find(message=>message.kind==='transfer_fence_ack')!);await cancel;await assert.rejects(sending,/cancelled/);
  assert.equal(p.left.closed,false);assert.equal(p.right.closed,false);await p.close();
});

test('producer fence admits the second credited chunk whose body is still in flight',async()=>{
  const sockets=duplexPair(),bootId=randomUUID(),info=identity(6),messages:Envelope[]=[];
  const receiver=new BulkChannel(sockets[1],{bootId,role:'hap',sendControl:async(kind,payload,request_id=randomUUID())=>{messages.push({v:1,kind,payload,boot_id:bootId,request_id});}});
  const handle=receiver.authorizeReceive({...info,sink:async()=>{}});receiver.start();
  await receiver.handleControl({v:1,kind:'transfer_open',boot_id:bootId,request_id:randomUUID(),payload:{scope:info.scope,operation_id:info.operationId,transfer_id:info.transferId,kind:'snapshot',total_bytes:6,chunk_bytes:3,window_chunks:2}});
  sockets[0].write(Buffer.concat([encodeBulkHeader({kind:'snapshot',transfer_id:info.transferId,sequence:0,length:3}),Buffer.from('abc')]));
  for(let turn=0;turn<100 && messages.filter(message=>message.kind==='transfer_credit').length<2;turn++)await tick();
  assert.equal(messages.filter(message=>message.kind==='transfer_credit').length,2,String(receiver.failure));
  sockets[0].write(Buffer.concat([encodeBulkHeader({kind:'snapshot',transfer_id:info.transferId,sequence:1,length:3}),Buffer.from('d')]));await tick();
  const fence:Envelope={v:1,kind:'transfer_fence',boot_id:bootId,request_id:randomUUID(),payload:{scope:info.scope,operation_id:info.operationId,transfer_id:info.transferId,sequence_count:2,sent_bytes:6,code:'cancelled'}};
  await receiver.handleControl(fence);assert.equal(messages.some(message=>message.kind==='transfer_fence_ack'),false);
  sockets[0].write(Buffer.from('ef'));
  for(let turn=0;turn<100 && !messages.some(message=>message.kind==='transfer_fence_ack');turn++)await tick();
  assert.equal(messages.some(message=>message.kind==='transfer_fence_ack'),true,String(receiver.failure));
  assert.equal(receiver.closed,false);await assert.rejects(handle.wait());await receiver.close();sockets[0].destroy();
});

test('sixty seconds of 50Hz tiny PCM keeps bounded metadata without retaining sink or source closures',async()=>{
  const p=pair();
  for(let index=0;index<3000;index++){
    const info=identity(2,'talkback_pcm'),sink=async()=>{};
    const received=p.left.authorizeReceive({...info,sink});
    await p.right.send({...info,source:source(Buffer.from([0,0]))});await received.wait();
  }
  const terminals=(p.left as unknown as {tombstones:Map<string,Record<string,unknown>>}).tombstones;
  assert.equal(terminals.size,3000);assert.equal((p.right as unknown as {tombstones:Map<string,unknown>}).tombstones.size,3000);
  for(const value of terminals.values())for(const field of ['sink','source','iterator','completion','wire','abort','timer','cleanup','consumers','result'])assert.equal(Object.hasOwn(value,field),false);
  assert.ok(Buffer.byteLength(JSON.stringify([...terminals.values()]))<2_000_000);
  assert.equal(p.left.closed,false);assert.equal(p.right.closed,false);await p.close();
});

test('success cleanup does not block another transfer and cancellation shares its owned callback',async()=>{
  const p=pair(30),info=identity(1),next=identity(1);let entered!:()=>void,release!:()=>void,closed=0;
  const started=new Promise<void>(yes=>{entered=yes;}),gate=new Promise<void>(yes=>{release=yes;});
  const handle=p.right.authorizeReceive({...info,sink:async()=>{},onClose:async()=>{closed++;entered();await gate;}});
  const sending=p.left.send({...info,source:source(Buffer.from('a'))});void sending.catch(()=>{});await started;
  const other=p.right.authorizeReceive({...next,sink:async()=>{}});await p.left.send({...next,source:source(Buffer.from('b'))});await other.wait();
  await assert.rejects(handle.cancel(),/timeout/);await assert.rejects(sending);assert.equal(p.right.closed,false);assert.equal(closed,1);
  release();await tick();await handle.cancel();assert.equal(closed,1);assert.deepEqual(p.right.counts,[0,0]);await p.close();
});

test('revocation at initial credit admission cancels only its owned transfer',async()=>{
  const p=pair(),info=identity(1);let checks=0;
  const handle=p.right.authorizeReceive({...info,valid:()=>++checks<3,sink:async()=>assert.fail('revoked delivery')});
  await assert.rejects(p.left.send({...info,source:source(Buffer.from('a'))}),/invalidated/);await assert.rejects(handle.wait(),/invalidated/);
  assert.equal(p.controls[1]!.some(message=>message.kind==='transfer_credit'),false);assert.equal(p.left.closed,false);assert.equal(p.right.closed,false);await p.close();
});
