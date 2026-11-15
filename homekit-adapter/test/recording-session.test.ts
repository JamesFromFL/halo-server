import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { duplexPair } from 'node:stream';
import { test } from 'node:test';
import { BulkChannel } from '../src/bulk.js';
import { AdapterSession, type NativeCamera } from '../src/session.js';
import { validateDirection, type CameraPolicy, type Envelope, type JsonObject, type Kind, type MediaScope, type RecordingConfiguration } from '../src/protocol.js';
const boot = '00000000-0000-0000-0000-000000000001', profile = '00000000-0000-0000-0000-000000000002';
const config: RecordingConfiguration = {
  video: {
    width: 1280, height: 720, fps: 15, profile: 'main', level: '3.1', max_bit_rate: 2000
  }, audio: null, fragment_length_ms: 4000, i_frame_interval_ms: 1000
};
const policy: CameraPolicy = {
  synchronized: true, streaming: false, live_audio: false, snapshots: false, recording: true, recording_audio: true, talkback: false, controls: false
};
const tick = (): Promise<void> => new Promise(resolve => setImmediate(resolve));
class Peer {
  readonly session: AdapterSession;
  readonly receiver: BulkChannel;
  readonly producer: BulkChannel;
  scope: MediaScope = {
    profile_id: profile, account_generation: 1, camera_id: '42', device_generation: 1, privacy_generation: 1
  };
  writes: Envelope[] = [];
  failures: string[] = [];
  queues = [Promise.resolve(), Promise.resolve()];
  revision = 1;
  holdOpen = false;
  holdStop = false;
  constructor(timeout = 2000) {
    const [left, right] = duplexPair();
    this.receiver = new BulkChannel(left, {
      bootId: boot, role: 'hap', timeoutMs: timeout, sendControl: (kind, payload, id) => this.control(0, kind, payload, id)
    });
    this.producer = new BulkChannel(right, {
      bootId: boot, role: 'python', timeoutMs: timeout, sendControl: (kind, payload, id) => this.control(1, kind, payload, id)
    });
    this.session = new AdapterSession(boot, {
      bulk: this.receiver, requestTimeoutMs: timeout, send: async (m) => { validateDirection(m, 'hap'); this.writes.push(m); queueMicrotask(() => { void this.auto(m).catch(e => this.failures.push(String(e))); }); }, onFailure: e => this.failures.push(e.code)
    });
    this.receiver.start();
    this.producer.start();
  }
  camera(): NativeCamera { return {
    scope: { ...this.scope }, name: 'fixture', kind: 'stickup_cam_mini_v2', family: 'stickup_cams', model: 'Indoor Cam', capabilities: {
      video: true, audio: true, snapshot: false, talkback: false, light: false, siren: false, doorbell: false, battery: false, evidence: ['halo:video', 'halo:audio']
    }, connection_state: null, battery_percent: null, observed_format: null
  }; }
  async auto(r: Envelope) {
    const p = r.payload;
    if (r.kind === 'inventory_request')
      await this.reply(r, 'inventory_page', {
        scope: { profile_id: profile, account_generation: 1 }, cursor: p.cursor, next_cursor: null, revision: this.revision, total: 1, complete: true, devices: [this.camera()]
      });
    if (r.kind === 'policy_update') {
      this.scope.privacy_generation++;
      this.revision++;
      await this.reply(r, 'policy_ack', {
        scope: p.scope, accepted: true, code: 'ok'
      });
    }
    if (r.kind === 'recording_open' && !this.holdOpen)
      await this.reply(r, 'recording_open_result', {
        scope: p.scope, job_id: p.job_id, status: 'accepted', code: 'ok'
      });
    if (r.kind === 'recording_stop' && !this.holdStop)
      await this.reply(r, 'recording_stop_result', {
        scope: p.scope, job_id: p.job_id, status: 'stopped', code: 'ok'
      });
    if (r.kind === 'recording_settings_update')
      await this.reply(r, 'recording_settings_result', {
        scope: p.scope, accepted: true, code: 'ok', revision: (p.expected_revision as number) + 1
      });
  }
  control(index: number, kind: Kind, payload: JsonObject, id: string = randomUUID()): Promise<void> {
    const message: Envelope = {
      v: 1, kind, boot_id: boot, request_id: id, payload
    };
    this.queues[index] = this.queues[index]!.then(async () => { if (index === 0)
      await this.producer.handleControl(message);
    else
      await this.session.receive(message); });
    void this.queues[index]!.catch(error => this.failures.push(String(error)));
    return Promise.resolve();
  }
  async ready() { await this.session.receive({
    v: 1, kind: 'inventory_reset', boot_id: boot, request_id: randomUUID(), payload: { scope: { profile_id: profile, account_generation: 1 }, revision: 1 }
  }); for (let i = 0; i < 20 && !this.session.inventoryComplete; i++)
    await tick(); const camera = this.session.camera('42'); await camera.updatePolicy(policy); return camera; }
  reply(r: Envelope, kind: Kind, payload: JsonObject) { return this.session.receive({
    ...r, kind, payload
  }); }
  async take(kind: Kind) { for (let i = 0; i < 30; i++) {
    const n = this.writes.findIndex(m => m.kind === kind);
    if (n >= 0)
      return this.writes.splice(n, 1)[0]!;
    await tick();
  } throw new Error(`missing ${kind}`); }
  async packet(r: Envelope, data: Buffer, kind: 'init' | 'fragment') {
    const transfer_id = randomUUID();
    await this.reply(r, 'recording_next_result', {
      scope: r.payload.scope, job_id: r.payload.job_id, status: 'ready', code: 'ok', transfer_id, kind, sequence: r.payload.sequence, total_bytes: data.length, duration_ms: kind === 'init' ? 0 : 1000
    });
    await this.producer.send({
      scope: r.payload.scope as MediaScope, operationId: r.payload.job_id as string, transferId: transfer_id, kind: kind === 'init' ? 'recording_init' : 'recording_fragment', totalBytes: data.length, source: (async function* () { for (let i = 0; i < data.length; i += 65536)
        yield data.subarray(i, i + 65536); })()
    });
  }
  async close() { this.session.close(); await Promise.all([this.receiver.close(), this.producer.close()]); }
}
test('seven cameras record concurrently with independent policy and exact packet delivery', async () => {
  class CameraPeer extends Peer {
    scopes = Array.from({ length: 7 }, (_, i) => ({ ...this.scope, camera_id: String(42 + i) }));
    override async auto(r: Envelope) {
      if (r.kind === 'inventory_request') {
        await this.reply(r, 'inventory_page', {
          scope: { profile_id: profile, account_generation: 1 }, cursor: r.payload.cursor,
          next_cursor: null, revision: this.revision, total: this.scopes.length, complete: true,
          devices: this.scopes.map(scope => ({ ...this.camera(), scope: { ...scope } })),
        });
      } else if (r.kind === 'policy_update') {
        const scope = this.scopes.find(s => s.camera_id === (r.payload.scope as MediaScope).camera_id)!;
        scope.privacy_generation++;
        this.revision++;
        await this.reply(r, 'policy_ack', { scope: r.payload.scope, accepted: true, code: 'ok' });
      } else {
        await super.auto(r);
      }
    }
  }
  const p = new CameraPeer();
  try {
    await p.ready();
    for (const scope of p.scopes.slice(1))
      await p.session.camera(scope.camera_id).updatePolicy(policy);
    const cameras = p.scopes.map(scope => p.session.camera(scope.camera_id));
    const recordings = await Promise.all(cameras.map(camera => camera.openRecording(config)));
    assert.equal(p.session.activeRecordings, 7);
    await assert.rejects(cameras[0]!.openRecording(config), /recording-capacity/);
    const pulls = recordings.map(recording => recording.next());
    for (let i = 0; i < recordings.length; i++) {
      const request = await p.take('recording_next');
      const bytes = Buffer.from(`camera-${(request.payload.scope as MediaScope).camera_id}`);
      await p.packet(request, bytes, 'init');
    }
    const packets = await Promise.all(pulls);
    for (let i = 0; i < recordings.length; i++)
      assert.deepEqual(packets[i]!.data, Buffer.from(`camera-${recordings[i]!.scope.camera_id}`));
    await recordings[0]!.stop();
    assert.equal(p.session.activeRecordings, 6);
    assert.equal(recordings[0]!.isCurrent(), false);
    assert.ok(recordings.slice(1).every(recording => recording.isCurrent()));
    await Promise.all(recordings.slice(1).map(recording => recording.stop()));
    assert.equal(p.session.activeRecordings, 0);
    assert.deepEqual(p.failures, []);
  } finally {
    await p.close();
  }
});
test('actual recording bulk has exact sequence, one pull, frozen scope and EOS authorization', async () => {
  const p = new Peer();
  try {
    const c = await p.ready(), r = await c.openRecording(config), scope = r.scope;
    scope.privacy_generation = 99;
    assert.equal(r.scope.privacy_generation, 2);
    for (const [kind, data] of [['init', Buffer.alloc(1000, 1)], ['fragment', Buffer.alloc(140000, 2)]] as const) {
      const pending = r.next(), request = await p.take('recording_next');
      await assert.rejects(r.next(), /recording-next-busy/);
      await p.packet(request, data, kind);
      const packet = await pending;
      assert.equal(packet!.kind, kind);
      assert.deepEqual(packet!.data, data);
      assert.equal(packet!.sequence, kind === 'init' ? 0 : 1);
    }
    const pending = r.next(), request = await p.take('recording_next');
    await p.reply(request, 'recording_next_result', {
      scope: request.payload.scope, job_id: r.jobId, status: 'eos', code: 'ok', transfer_id: null, kind: null, sequence: 2, total_bytes: 0, duration_ms: 0
    });
    assert.equal(await pending, null);
    assert.equal(r.isCurrent(), true);
    await r.stop();
    assert.equal(r.isCurrent(), false);
    assert.deepEqual(p.receiver.counts, [0, 0]);
    assert.deepEqual(p.failures, []);
  }
  finally {
    await p.close();
  }
});
test('abort before READY admits only exact canceled transfer cleanup and another job succeeds', async () => {
  const p = new Peer();
  try {
    const c = await p.ready(), r = await c.openRecording(config), abort = new AbortController();
    const pending = r.next(abort.signal);
    void pending.catch(() => { });
    const request = await p.take('recording_next');
    abort.abort();
    await assert.rejects(pending, /recording-retired/);
    await assert.rejects(p.packet(request, Buffer.alloc(1024), 'init'));
    for (let i = 0; i < 20 && p.receiver.counts[0]; i++)
      await tick();
    assert.deepEqual(p.receiver.counts, [0, 0]);
    assert.deepEqual(p.producer.counts, [0, 0]);
    assert.equal(p.session.isClosed, false);
    const next = await c.openRecording(config), pulling = next.next(), other = await p.take('recording_next');
    await p.packet(other, Buffer.from('second'), 'init');
    assert.equal((await pulling)!.data.toString(), 'second');
    await next.stop();
  }
  finally {
    await p.close();
  }
});
test('abort pending OPEN sends STOP and a late accepted reply cannot return a handle', async () => {
  const p = new Peer();
  try {
    const c = await p.ready();
    p.holdOpen = true;
    const abort = new AbortController(), pending = c.openRecording(config, abort.signal);
    void pending.catch(() => { });
    const request = await p.take('recording_open');
    abort.abort();
    await assert.rejects(pending, /recording-retired/);
    await p.reply(request, 'recording_open_result', {
      scope: request.payload.scope, job_id: request.payload.job_id, status: 'accepted', code: 'ok'
    });
    assert.equal(p.session.activeRecordings, 0);
    assert.equal(p.session.isClosed, false);
  }
  finally {
    await p.close();
  }
});
test('normal selected-config changes preserve a stream; audio disable synchronously fences it', async () => {
  const p = new Peer();
  try {
    const c = await p.ready(), audio = structuredClone(config);
    audio.audio = {
      sample_rate: 24000, bit_rate: 32000, channels: 1, bitrate_mode: 'vbr'
    };
    const r = await c.openRecording(audio), replacement = structuredClone(audio);
    replacement.video.max_bit_rate = 3000;
    assert.equal(await c.updateRecordingSettings(0, true, true, replacement), 1);
    assert.equal(r.isCurrent(), true);
    assert.equal(await c.updateRecordingSettings(1, true, false, replacement), 2);
    assert.equal(r.isCurrent(), false);
    await r.stop();
  }
  finally {
    await p.close();
  }
});
test('privacy invalidation rejects pending media before refreshed inventory can grant anything', async () => {
  const p = new Peer();
  try {
    const c = await p.ready(), r = await c.openRecording(config), pending = r.next();
    void pending.catch(() => { });
    await p.take('recording_next');
    p.scope.privacy_generation++;
    p.revision++;
    await p.session.receive({
      v: 1, kind: 'inventory_reset', boot_id: boot, request_id: randomUUID(), payload: { scope: { profile_id: profile, account_generation: 1 }, revision: p.revision }
    });
    assert.equal(r.isCurrent(), false);
    await assert.rejects(pending, /recording-retired/);
  }
  finally {
    await p.close();
  }
});
test('known wrong packet sequence and unknown recording OPEN both fail the session closed', async () => {
  for (const unknown of [false, true]) {
    const p = new Peer();
    try {
      const c = await p.ready(), r = await c.openRecording(config), pending = r.next();
      void pending.catch(() => { });
      const request = await p.take('recording_next');
      if (unknown)
        await assert.rejects(p.session.receive({
          v: 1, kind: 'transfer_open', boot_id: boot, request_id: randomUUID(), payload: {
            scope: r.scope, operation_id: randomUUID(), transfer_id: randomUUID(), kind: 'recording_init', total_bytes: 1, chunk_bytes: 1, window_chunks: 2
          }
        }));
      else
        await assert.rejects(p.reply(request, 'recording_next_result', {
          scope: request.payload.scope, job_id: r.jobId, status: 'ready', code: 'ok', transfer_id: randomUUID(), kind: 'fragment', sequence: 1, total_bytes: 5, duration_ms: 1000
        }));
      assert.equal(p.session.isClosed, true);
      await assert.rejects(pending);
    }
    finally {
      await p.close();
    }
  }
});
test('negative OPEN still compensates; a rejected cleanup ACK retires the entire session', async () => {
  const p = new Peer();
  try {
    const c = await p.ready();
    p.holdOpen = true;
    p.holdStop = true;
    const pending = c.openRecording(config);
    void pending.catch(() => { });
    const request = await p.take('recording_open');
    await p.reply(request, 'recording_open_result', {
      scope: request.payload.scope, job_id: request.payload.job_id, status: 'rejected', code: 'unavailable'
    });
    const stop = await p.take('recording_stop');
    await p.reply(stop, 'recording_stop_result', {
      scope: stop.payload.scope, job_id: stop.payload.job_id, status: 'rejected', code: 'source-failed'
    });
    await assert.rejects(pending, /recording-stop-rejected/);
    assert.equal(p.session.isClosed, true);
  }
  finally {
    await p.close();
  }
});
test('abort clears partially received media synchronously while real producer cleanup joins', async () => {
  const p = new Peer();
  let release!: () => void;
  const gate = new Promise<void>(ok => { release = ok; });
  try {
    const c = await p.ready(), r = await c.openRecording(config), abort = new AbortController();
    const pending = r.next(abort.signal);
    void pending.catch(() => { });
    const request = await p.take('recording_next'), transfer_id = randomUUID();
    await p.reply(request, 'recording_next_result', {
      scope: request.payload.scope, job_id: r.jobId, status: 'ready', code: 'ok', transfer_id, kind: 'init', sequence: 0, total_bytes: 131072, duration_ms: 0
    });
    const sending = p.producer.send({
      scope: r.scope, operationId: r.jobId, transferId: transfer_id, kind: 'recording_init', totalBytes: 131072,
      source: (async function* () { yield Buffer.alloc(65536, 7); await gate; yield Buffer.alloc(65536, 8); })()
    });
    void sending.catch(() => { });
    const jobs = (p.session as unknown as {
      recordings: Map<string, {
        pull: {
          chunks: Buffer[];
          length: number;
          result: Promise<unknown>;
        };
      }>;
    }).recordings;
    for (let i = 0; i < 30 && !jobs.get(r.jobId)!.pull.length; i++)
      await tick();
    assert.equal(jobs.get(r.jobId)!.pull.length, 65536);
    abort.abort();
    assert.equal(jobs.get(r.jobId)!.pull.chunks.length, 0);
    assert.equal(jobs.get(r.jobId)!.pull.length, 0);
    assert.equal(await jobs.get(r.jobId)!.pull.result, null);
    release();
    await assert.rejects(pending, /recording-retired/);
    await assert.rejects(sending);
    for (let i = 0; i < 20 && p.receiver.counts[0]; i++)
      await tick();
    assert.deepEqual(p.receiver.counts, [0, 0]);
  }
  finally {
    release();
    await p.close();
  }
});
test('retired recording capacity stays charged until stopped ACK proves cleanup', async () => {
  const p = new Peer();
  try {
    const c = await p.ready(), r = await c.openRecording(config);
    p.holdStop = true;
    const stopping = r.stop();
    void stopping.catch(() => { });
    const request = await p.take('recording_stop');
    assert.equal(p.session.activeRecordings, 1);
    await assert.rejects(c.openRecording(config), /recording-capacity/);
    await p.reply(request, 'recording_stop_result', {
      scope: request.payload.scope, job_id: r.jobId, status: 'stopped', code: 'ok'
    });
    await stopping;
    assert.equal(p.session.activeRecordings, 0);
    p.holdStop = false;
    const next = await c.openRecording(config);
    await next.stop();
  }
  finally {
    await p.close();
  }
});
