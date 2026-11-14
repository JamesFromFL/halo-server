import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { duplexPair } from 'node:stream';
import { test } from 'node:test';
import { BulkChannel } from '../src/bulk.js';
import { AdapterSession, type NativeCamera } from '../src/session.js';
import { validateDirection, type CameraPolicy, type Envelope, type JsonObject, type Kind, type MediaScope } from '../src/protocol.js';

const tick = (): Promise<void> => new Promise(resolve => setImmediate(resolve));
const image = Buffer.from([0xff, 0xd8, 0x01, 0xff, 0xd9]);
const policy: CameraPolicy = {synchronized: true, streaming: true, live_audio: false, snapshots: true,
  recording: false, recording_audio: false, talkback: false, controls: false};
const observed = <T>(value: Promise<T>): Promise<T> => {void value.catch(() => {}); return value;};

class Peer {
  readonly boot = randomUUID();
  readonly account = {profile_id: randomUUID(), account_generation: 1};
  readonly cameras: NativeCamera[];
  readonly writes: Envelope[] = [];
  readonly history: Envelope[] = [];
  readonly failures: string[] = [];
  readonly session: AdapterSession;
  readonly hap: BulkChannel;
  readonly producer: BulkChannel;
  revision = 1;

  constructor(count = 7) {
    this.cameras = Array.from({length: count}, (_, index) => ({
      scope: {...this.account, camera_id: String(index), device_generation: 1, privacy_generation: 1},
      name: 'Synthetic camera', family: 'fixture', kind: 'fixture', model: 'fixture',
      capabilities: {video: true, audio: false, snapshot: true, talkback: false, light: false,
        siren: false, doorbell: false, battery: false, evidence: ['halo:video', 'halo:snapshot']},
      connection_state: null, battery_percent: null, observed_format: null,
    }));
    const sockets = duplexPair(), queues = [Promise.resolve(), Promise.resolve()];
    const send = (side: number, kind: Kind, payload: JsonObject, id: string = randomUUID()): Promise<void> => {
      const message: Envelope = {v: 1, boot_id: this.boot, request_id: id, kind, payload};
      queues[side] = queues[side]!.then(async () => {
        if (side === 0) await this.producer.handleControl(message); else await this.session.receive(message);
      });
      void queues[side]!.catch(() => {});
      return Promise.resolve();
    };
    this.hap = new BulkChannel(sockets[0], {bootId: this.boot, role: 'hap', sendControl: (...args) => send(0, ...args)});
    this.producer = new BulkChannel(sockets[1], {bootId: this.boot, role: 'python', sendControl: (...args) => send(1, ...args)});
    this.session = new AdapterSession(this.boot, {bulk: this.hap,
      send: async message => {validateDirection(message, 'hap'); this.writes.push(message); this.history.push(message);},
      onFailure: error => this.failures.push(error.code)});
    this.hap.start(); this.producer.start();
  }
  async request(kind: Kind): Promise<Envelope> {
    for (let turn = 0; turn < 20; turn++) {
      const index = this.writes.findIndex(message => message.kind === kind);
      if (index >= 0) return this.writes.splice(index, 1)[0]!;
      await tick();
    }
    throw new Error(`Missing synthetic ${kind}`);
  }
  async reply(request: Envelope, kind: Kind, payload: JsonObject): Promise<void> {
    await this.session.receive({...request, kind, payload});
  }
  async inventory(): Promise<void> {
    const request = await this.request('inventory_request');
    await this.reply(request, 'inventory_page', {scope: this.account, cursor: null, next_cursor: null,
      revision: this.revision, total: this.cameras.length, complete: true, devices: this.cameras});
    await tick();
  }
  async ready(): Promise<void> {
    await this.session.receive({v: 1, boot_id: this.boot, request_id: randomUUID(), kind: 'inventory_reset',
      payload: {scope: this.account, revision: this.revision}});
    await this.inventory();
    for (const camera of this.cameras) {
      const update = this.session.camera(camera.scope.camera_id).updatePolicy(policy);
      const request = await this.request('policy_update');
      camera.scope.privacy_generation++; this.revision++;
      await this.reply(request, 'policy_ack', {scope: request.payload.scope, accepted: true, code: 'ok'});
      await this.inventory(); await update;
    }
  }
  snapshot(index: number, signal?: AbortSignal): Promise<Buffer> {
    return observed(this.session.camera(String(index)).snapshot(320, 240, 'periodic', signal));
  }
  async reject(request: Envelope): Promise<void> {
    await this.reply(request, 'snapshot_result', {scope: request.payload.scope, job_id: request.payload.job_id,
      status: 'rejected', code: 'source-failed', transfer_id: null});
    await tick();
  }
  async deliver(request: Envelope, gate: Promise<void> = Promise.resolve()): Promise<void> {
    const transfer = randomUUID();
    await this.reply(request, 'snapshot_result', {scope: request.payload.scope, job_id: request.payload.job_id,
      status: 'ready', code: 'ok', transfer_id: transfer});
    await this.producer.send({scope: request.payload.scope as MediaScope, operationId: request.payload.job_id as string,
      transferId: transfer, kind: 'snapshot', totalBytes: image.length,
      source: (async function* () {await gate; yield image;})()});
  }
  get snapshotWrites(): Envelope[] {return this.history.filter(message => message.kind === 'snapshot_request');}
  async close(): Promise<void> {
    this.session.close(); await Promise.allSettled([this.hap.close(), this.producer.close()]);
  }
}

test('seven simultaneous camera tiles finish FIFO with at most two complete snapshot operations', async () => {
  const peer = new Peer(); await peer.ready();
  try {
    const calls = peer.cameras.map((_, index) => peer.snapshot(index));
    const first = await peer.request('snapshot_request'), second = await peer.request('snapshot_request');
    assert.deepEqual(peer.snapshotWrites.map(request => (request.payload.scope as MediaScope).camera_id), ['0', '1']);
    let release!: () => void;
    const delivering = peer.deliver(first, new Promise<void>(resolve => {release = resolve;}));
    await tick(); await tick();
    // READY releases the RPC correlation, but the owned image is still transferring.
    assert.equal(peer.snapshotWrites.length, 2);
    release(); await delivering;
    assert.deepEqual(await calls[0], image);
    const pending = [second, await peer.request('snapshot_request')];
    for (let index = 1; index < 7; index++) {
      const request = pending.shift()!;
      assert.equal((request.payload.scope as MediaScope).camera_id, String(index));
      assert.equal(peer.snapshotWrites.length, Math.min(index + 2, 7));
      await peer.deliver(request); assert.deepEqual(await calls[index], image);
      if (index < 5) pending.push(await peer.request('snapshot_request'));
    }
    assert.deepEqual(await Promise.all(calls), Array(7).fill(image));
    assert.equal(peer.session.pendingRequests, 0);
    assert.deepEqual(peer.hap.counts, [0, 0]); assert.deepEqual(peer.producer.counts, [0, 0]);
    assert.deepEqual(peer.failures, []);
  } finally {await peer.close();}
});

test('queued cancellation has no IPC or cleanup tombstone and the next camera keeps its place', async () => {
  const peer = new Peer(); await peer.ready();
  try {
    const first = peer.snapshot(0), second = peer.snapshot(1), abort = new AbortController();
    const cancelled = peer.snapshot(2, abort.signal), fourth = peer.snapshot(3);
    const one = await peer.request('snapshot_request'), two = await peer.request('snapshot_request');
    abort.abort(); await assert.rejects(cancelled, /snapshot-cancelled/);
    assert.equal(peer.history.some(message => message.kind === 'snapshot_cancel'), false);
    assert.equal(peer.snapshotWrites.length, 2);
    await peer.reject(one); await assert.rejects(first, /snapshot-source-failed/);
    const next = await peer.request('snapshot_request');
    assert.equal((next.payload.scope as MediaScope).camera_id, '3');
    await peer.deliver(two); await peer.deliver(next);
    assert.deepEqual(await second, image); assert.deepEqual(await fourth, image);
  } finally {await peer.close();}
});

test('active cancellation holds its slot until a correlated rejection proves cleanup', async () => {
  const peer = new Peer(); await peer.ready();
  try {
    const abort = new AbortController(), first = peer.snapshot(0, abort.signal), second = peer.snapshot(1), third = peer.snapshot(2);
    const one = await peer.request('snapshot_request'), two = await peer.request('snapshot_request');
    abort.abort(); await assert.rejects(first, /snapshot-cancelled/); await tick();
    assert.equal(peer.snapshotWrites.length, 2);
    assert.equal(peer.history.filter(message => message.kind === 'snapshot_cancel').length, 1);
    await peer.reject(one);
    const next = await peer.request('snapshot_request');
    assert.equal((next.payload.scope as MediaScope).camera_id, '2');
    await peer.deliver(two); await peer.deliver(next);
    assert.deepEqual(await second, image); assert.deepEqual(await third, image);
  } finally {await peer.close();}
});

test('the original total deadline includes time queued and never restarts on dispatch', async context => {
  context.mock.timers.enable({apis: ['setTimeout']});
  const peer = new Peer(); await peer.ready();
  try {
    const first = peer.snapshot(0), second = peer.snapshot(1), third = peer.snapshot(2);
    const one = await peer.request('snapshot_request'); await peer.request('snapshot_request');
    context.mock.timers.tick(15_000); await peer.reject(one); await assert.rejects(first);
    await peer.request('snapshot_request');
    context.mock.timers.tick(5_000); await tick();
    await assert.rejects(third, /snapshot-timeout/); await assert.rejects(second);
    assert.equal(peer.snapshotWrites.length, 3);
  } finally {await peer.close();}
});

test('an expired queued call cannot dispatch when completion runs before overdue timer callbacks', async context => {
  let now = 0; context.mock.method(performance, 'now', () => now);
  const peer = new Peer(); await peer.ready();
  try {
    const first = peer.snapshot(0), second = peer.snapshot(1), third = peer.snapshot(2);
    const one = await peer.request('snapshot_request'), two = await peer.request('snapshot_request');
    now = 20_001;
    await peer.reject(one); await assert.rejects(first); await assert.rejects(third, /snapshot-timeout/);
    assert.equal(peer.snapshotWrites.length, 2);
    assert.equal(peer.history.some(message => message.kind === 'snapshot_cancel'), false);
    await peer.reject(two); await assert.rejects(second);
  } finally {await peer.close();}
});

test('a queued image rechecks its original privacy scope after inventory refresh', async () => {
  const peer = new Peer(); await peer.ready();
  try {
    const first = peer.snapshot(0), second = peer.snapshot(1), third = peer.snapshot(2);
    const one = await peer.request('snapshot_request'), two = await peer.request('snapshot_request');
    peer.cameras[2]!.scope.privacy_generation++; peer.revision++;
    const refresh = peer.session.refresh(); await peer.inventory(); await refresh;
    await peer.reject(one); await assert.rejects(first); await assert.rejects(third, /policy-denied|resource-retired/);
    assert.equal(peer.snapshotWrites.length, 2);
    assert.equal(peer.history.some(message => message.kind === 'snapshot_cancel'), false);
    await peer.deliver(two); assert.deepEqual(await second, image);
  } finally {await peer.close();}
});

test('queued metadata and cancelled active cleanup owners share the finite sixteen-call bound', async () => {
  const peer = new Peer(); await peer.ready();
  try {
    const abort = new AbortController();
    const calls = Array.from({length: 16}, (_, index) => peer.snapshot(index % 7, index === 0 ? abort.signal : undefined));
    await peer.request('snapshot_request'); await peer.request('snapshot_request');
    await assert.rejects(peer.snapshot(0), /snapshot-capacity/);
    abort.abort(); await assert.rejects(calls[0]!, /snapshot-cancelled/);
    await assert.rejects(peer.snapshot(0), /snapshot-capacity/);
    assert.equal(peer.snapshotWrites.length, 2);
    peer.session.close(); await Promise.allSettled(calls); await tick();
    assert.equal(peer.snapshotWrites.length, 2);
    assert.equal(peer.history.filter(message => message.kind === 'snapshot_cancel').length, 1);
  } finally {await peer.close();}
});
