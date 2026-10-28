import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { test } from "node:test";
import { BulkChannel } from "../src/bulk.js";
import { duplexPair } from "node:stream";
import { AdapterSession, type NativeCamera } from "../src/session.js";
import { validateDirection, type CameraPolicy, type Envelope, type JsonObject, type Kind, type MediaScope } from "../src/protocol.js";

const boot = "00000000-0000-0000-0000-000000000001";
const profile = "00000000-0000-0000-0000-000000000002";
const scope: MediaScope = {profile_id: profile, account_generation: 1, camera_id: "front", device_generation: 1, privacy_generation: 1};
const account = {profile_id: profile, account_generation: 1};
const policy: CameraPolicy = {synchronized: true, streaming: true, live_audio: false, snapshots: true,
  recording: false, recording_audio: false, talkback: true, controls: true};
const camera = (id = "front", privacy = 1, generation = 1): NativeCamera => ({
  scope: {...scope, camera_id: id, privacy_generation: privacy, device_generation: generation},
  name: "Synthetic camera", family: "fixture", kind: "fixture", model: "fixture",
  capabilities: {video: true, audio: false, snapshot: true, talkback: true, light: true, siren: false,
    doorbell: false, battery: false, evidence: ["halo:video", "halo:snapshot", "halo:talkback", "halo:light"]},
  connection_state: null, battery_percent: null, observed_format: null,
});
const tick = (): Promise<void> => new Promise(resolve => setImmediate(resolve));
class Peer {
  writes: Envelope[] = [];
  failures: string[] = [];
  session: AdapterSession;
  constructor(options: {timeout?: number; capacity?: number; bulk?: BulkChannel} = {}) {
    this.session = new AdapterSession(boot, {send: async message => { validateDirection(message, "hap"); this.writes.push(message); },
      onFailure: error => this.failures.push(error.code),
      ...(options.timeout === undefined ? {} : {requestTimeoutMs: options.timeout}),
      ...(options.capacity === undefined ? {} : {maximumPendingRequests: options.capacity}),
      ...(options.bulk === undefined ? {} : {bulk: options.bulk}),
    });
  }
  async request(kind: Kind): Promise<Envelope> {
    for (let turn = 0; turn < 10; turn++) {
      const index = this.writes.findIndex(message => message.kind === kind);
      if (index >= 0) return this.writes.splice(index, 1)[0]!;
      await tick();
    }
    throw new Error(`missing synthetic request ${kind}`);
  }
  async reply(request: Envelope, kind: Kind, payload: JsonObject): Promise<void> {
    await this.session.receive({...request, kind, payload});
  }
  async reset(scope: typeof account | null = account, revision = 1): Promise<void> {
    await this.session.receive({v: 1, kind: "inventory_reset", boot_id: boot, request_id: randomUUID(), payload: {scope, revision}});
  }
  async inventory(devices = [camera()], revision = 1, complete = true): Promise<void> {
    const request = await this.request("inventory_request");
    await this.reply(request, "inventory_page", {scope: account, cursor: request.payload.cursor,
      next_cursor: null, revision, total: devices.length, complete, devices});
    await tick();
  }
  async ready(privacy = 9): Promise<ReturnType<AdapterSession["camera"]>> {
    await this.reset(); await this.inventory();
    const delegate = this.session.camera("front");
    assert.equal(delegate.ready, false);
    const update = delegate.updatePolicy(policy);
    const request = await this.request("policy_update");
    await this.reply(request, "policy_ack", {scope: request.payload.scope, accepted: true, code: "ok"});
    await this.inventory([camera("front", privacy)], 2);
    await update;
    assert.equal(delegate.ready, true);
    return delegate;
  }
}

test("inventory discovery grants no policy authority; ACK refresh discovers the assigned privacy generation", async () => {
  const peer = new Peer();
  await peer.reset(); await peer.inventory();
  const delegate = peer.session.camera("front");
  await assert.rejects(delegate.acquireLive(), /policy-denied/);
  const input = structuredClone(policy), update = delegate.updatePolicy(input);
  input.streaming = false;
  const request = await peer.request("policy_update");
  assert.equal((request.payload.policy as CameraPolicy).streaming, true);
  await peer.reply(request, "policy_ack", {scope: request.payload.scope, accepted: true, code: "ok"});
  assert.equal(delegate.ready, false);
  await peer.inventory([camera("front", 713)], 2);
  await update;
  assert.equal(delegate.info.scope.privacy_generation, 713);
  assert.equal(delegate.ready, true);
  const pending = delegate.acquireLive(1000);
  const lease = await peer.request("lease_request");
  assert.equal((lease.payload.scope as MediaScope).privacy_generation, 713);
  await peer.reply(lease, "lease_result", {scope: lease.payload.scope, session_id: lease.payload.session_id, status: "accepted", code: "ok", ttl_ms: 1000});
  await pending;
  peer.session.close();
});

test("concurrent newer policy waiters join the replacement after an earlier policy settles", async () => {
  const peer = new Peer({capacity: 1});
  try {
    await peer.reset(); await peer.inventory();
    const delegate = peer.session.camera("front");
    const initial = delegate.ensurePolicy(policy), first = await peer.request("policy_update");
    const newer = {...policy, live_audio: true};
    const one = delegate.ensurePolicy(newer), two = delegate.ensurePolicy({...newer});
    const results = Promise.allSettled([one, two]);
    await peer.reply(first, "policy_ack", {scope: first.payload.scope, accepted: true, code: "ok"});
    await peer.inventory([camera("front", 2)], 2); await initial;
    const replacement = await peer.request("policy_update");
    assert.deepEqual(replacement.payload.policy, newer);
    assert.equal((replacement.payload.scope as MediaScope).privacy_generation, 2);
    assert.equal(delegate.ready, false);
    await peer.reply(replacement, "policy_ack", {scope: replacement.payload.scope, accepted: true, code: "ok"});
    await peer.inventory([camera("front", 3)], 3);
    assert.deepEqual(await results, [{status: "fulfilled", value: undefined}, {status: "fulfilled", value: undefined}]);
    assert.equal(delegate.ready, true);
    assert.equal(peer.session.pendingRequests, 0);
    assert.equal(peer.writes.some(message => message.kind === "policy_update"), false);
    assert.deepEqual(peer.failures, []);
  } finally { peer.session.close(); }
});

test("queued policy intents freeze inputs and serialize distinct replacements with fresh scopes", async () => {
  const peer = new Peer();
  try {
    await peer.reset(); await peer.inventory();
    const delegate = peer.session.camera("front");
    const initial = delegate.ensurePolicy(policy), first = await peer.request("policy_update");
    const input = {...policy, snapshots: false}, wanted = structuredClone(input);
    const second = delegate.ensurePolicy(input);
    input.streaming = false;
    const last = {...policy, talkback: false}, third = delegate.ensurePolicy(last);
    const results = Promise.allSettled([second, third]);
    await peer.reply(first, "policy_ack", {scope: first.payload.scope, accepted: true, code: "ok"});
    await peer.inventory([camera("front", 2)], 2); await initial;
    const replacement = await peer.request("policy_update");
    assert.deepEqual(replacement.payload.policy, wanted);
    assert.equal((replacement.payload.scope as MediaScope).privacy_generation, 2);
    await peer.reply(replacement, "policy_ack", {scope: replacement.payload.scope, accepted: true, code: "ok"});
    await peer.inventory([camera("front", 3)], 3);
    const final = await peer.request("policy_update");
    assert.deepEqual(final.payload.policy, last);
    assert.equal((final.payload.scope as MediaScope).privacy_generation, 3);
    await peer.reply(final, "policy_ack", {scope: final.payload.scope, accepted: true, code: "ok"});
    await peer.inventory([camera("front", 4)], 4);
    assert.deepEqual(await results, [{status: "fulfilled", value: undefined}, {status: "fulfilled", value: undefined}]);
    await delegate.ensurePolicy(last);
    assert.equal(peer.writes.some(message => message.kind === "policy_update"), false);
    assert.equal(peer.session.pendingRequests, 0);
  } finally { peer.session.close(); }
});

test("shared replacement timeout rejects all waiters and releases synchronization for retry", async context => {
  context.mock.timers.enable({apis: ["setTimeout"]});
  const peer = new Peer({timeout: 20});
  try {
    await peer.reset(); await peer.inventory();
    const delegate = peer.session.camera("front");
    const initial = delegate.ensurePolicy(policy), first = await peer.request("policy_update");
    const newer = {...policy, live_audio: true};
    const results = Promise.allSettled([delegate.ensurePolicy(newer), delegate.ensurePolicy(newer)]);
    await peer.reply(first, "policy_ack", {scope: first.payload.scope, accepted: true, code: "ok"});
    await peer.inventory([camera("front", 2)], 2); await initial;
    await peer.request("policy_update");
    context.mock.timers.tick(20);
    const settled = await results;
    assert.equal(settled.length, 2);
    for (const result of settled) {
      assert.equal(result.status, "rejected");
      if (result.status === "rejected") assert.match(String(result.reason), /request-timeout/);
    }
    assert.equal(delegate.ready, false); assert.equal(peer.session.pendingRequests, 0);
    const retry = delegate.ensurePolicy(newer), request = await peer.request("policy_update");
    await peer.reply(request, "policy_ack", {scope: request.payload.scope, accepted: true, code: "ok"});
    await peer.inventory([camera("front", 3)], 3); await retry;
    assert.equal(delegate.ready, true); assert.equal(peer.session.isClosed, false);
    assert.deepEqual(peer.failures, []);
  } finally { peer.session.close(); }
});

test("scope replacement and session close never let waiting policy callers regain stale authority", async () => {
  for (const close of [false, true]) {
    const peer = new Peer();
    try {
      await peer.reset(); await peer.inventory();
      const delegate = peer.session.camera("front");
      const initial = delegate.ensurePolicy(policy), request = await peer.request("policy_update");
      const wanted = {...policy, live_audio: true};
      const results = Promise.allSettled([initial, delegate.ensurePolicy(wanted), delegate.ensurePolicy(wanted)]);
      if (close) peer.session.close();
      else {
        await peer.reply(request, "policy_ack", {scope: request.payload.scope, accepted: true, code: "ok"});
        await peer.inventory([camera("front", 2, 2)], 2);
      }
      for (const result of await results) {
        assert.equal(result.status, "rejected");
        if (result.status === "rejected") assert.match(String(result.reason), close ? /session-closed/ : /stale-camera/);
      }
      assert.equal(peer.writes.some(message => message.kind === "policy_update"), false);
      assert.equal(peer.session.pendingRequests, 0); assert.equal(delegate.ready, false);
    } finally { peer.session.close(); }
  }
});

test("typed live, renewal, control, and speaker delegates retain full scope and explicit handle identity", async () => {
  const peer = new Peer(), delegate = await peer.ready();
  const acquiring = delegate.acquireLive(1000), request = await peer.request("lease_request");
  await peer.reply(request, "lease_result", {scope: request.payload.scope, session_id: request.payload.session_id,
    status: "accepted", code: "ok", ttl_ms: 1000});
  const live = await acquiring;
  const renewal = live.renew(1000), renew = await peer.request("lease_renew");
  assert.equal(renew.payload.session_id, live.sessionId);
  await peer.reply(renew, "lease_renew_result", {scope: renew.payload.scope, session_id: live.sessionId, status: "accepted", code: "ok", ttl_ms: 1000});
  await renewal;
  const control = delegate.setControl("light", true), set = await peer.request("control_request");
  await peer.reply(set, "control_result", {scope: set.payload.scope, operation_id: set.payload.operation_id, status: "unknown", code: "timeout"});
  assert.equal(await control, "unknown");
  const speaking = delegate.acquireTalkback(), speaker = await peer.request("talkback_request");
  await peer.reply(speaker, "talkback_result", {scope: speaker.payload.scope, session_id: speaker.payload.session_id, status: "accepted", code: "ok"});
  const talkback = await speaking;
  await assert.rejects(delegate.acquireTalkback(), /speaker-capacity/);
  await assert.rejects(talkback.writePcm(Buffer.alloc(1922)), /invalid-pcm-frame/);
  const stoppingSpeaker = talkback.close(), stopSpeaker = await peer.request("talkback_request");
  assert.equal(stopSpeaker.payload.action, "stop");
  await peer.reply(stopSpeaker, "talkback_result", {scope: stopSpeaker.payload.scope, session_id: talkback.sessionId, status: "stopped", code: "ok"});
  await stoppingSpeaker;
  const stopping = live.close(), stop = await peer.request("lease_stop");
  await peer.reply(stop, "lease_stop_result", {scope: stop.payload.scope, session_id: live.sessionId, status: "stopped", code: "ok"});
  await stopping; await live.close();
  peer.session.close();
});

test("a known mismatched response fails the session; unsolicited and late results never grant resources", async () => {
  const peer = new Peer(), delegate = await peer.ready();
  const pending = delegate.setControl("light", true), request = await peer.request("control_request");
  const rejected = assert.rejects(pending, /session-closed/);
  await assert.rejects(peer.reply(request, "control_result", {scope: {...request.payload.scope as MediaScope, privacy_generation: 1},
    operation_id: request.payload.operation_id, status: "accepted", code: "ok"}), /invalid-reply-ownership/);
  await rejected;
  assert.deepEqual(peer.failures, ["invalid-reply-ownership"]);
  assert.equal(peer.session.isClosed, true);
  assert.equal(await peer.session.receive({...request, kind: "control_result", request_id: randomUUID(),
    payload: {scope: request.payload.scope, operation_id: request.payload.operation_id, status: "accepted", code: "ok"}}), false);
});

test("pending RPC capacity and deadlines retire promises and a late accepted lease cannot revive them", async () => {
  const peer = new Peer({timeout: 20, capacity: 2}), delegate = await peer.ready();
  const first = delegate.setControl("light", true), second = delegate.setControl("light", false);
  const firstRejected = assert.rejects(first, /request-timeout/), secondRejected = assert.rejects(second, /request-timeout/);
  const request = await peer.request("control_request");
  await assert.rejects(delegate.acquireLive(), /request-capacity/);
  assert.equal(peer.session.pendingRequests, 2);
  await Promise.all([firstRejected, secondRejected]);
  assert.equal(peer.session.pendingRequests, 0);
  assert.equal(await peer.session.receive({...request, kind: "control_result", payload: {scope: request.payload.scope,
    operation_id: request.payload.operation_id, status: "accepted", code: "ok"}}), false);
  peer.session.close();
});

test("an inventory reset fences existing handles immediately and replacement devices retire delegates", async () => {
  const peer = new Peer(), delegate = await peer.ready();
  const acquiring = delegate.acquireLive(1000), request = await peer.request("lease_request");
  await peer.reply(request, "lease_result", {scope: request.payload.scope, session_id: request.payload.session_id,
    status: "accepted", code: "ok", ttl_ms: 1000});
  const live = await acquiring;
  await peer.reset(account, 3);
  assert.equal(delegate.ready, false);
  await assert.rejects(live.renew(), /resource-retired/);
  const stop = await peer.request("lease_stop");
  await peer.reply(stop, "lease_stop_result", {scope: stop.payload.scope, session_id: live.sessionId, status: "stopped", code: "ok"});
  await peer.inventory([camera("front", 1, 2)], 3);
  assert.throws(() => delegate.info, /stale-camera/);
  const replacement = peer.session.camera("front");
  assert.equal(replacement.ready, false);
  await peer.reset(null, 4);
  assert.equal(peer.session.inventoryComplete, false);
  await assert.rejects(replacement.setControl("light", true), /stale-camera/);
  peer.session.close();
});

test("pagination rejects repeats across otherwise valid pages and a partial inventory cannot enable operations", async () => {
  const peer = new Peer(); await peer.reset();
  const first = await peer.request("inventory_request"), cursor = randomUUID();
  await peer.reply(first, "inventory_page", {scope: account, revision: 3, cursor: null, next_cursor: cursor,
    total: 2, complete: true, devices: [camera()]});
  const second = await peer.request("inventory_request");
  assert.equal(second.payload.revision, 3);
  await peer.reply(second, "inventory_page", {scope: account, revision: 3, cursor, next_cursor: null,
    total: 2, complete: true, devices: [camera()]});
  await tick();
  assert.deepEqual(peer.failures, ["duplicate-inventory-camera"]);
  assert.equal(peer.session.isClosed, true);
  const partial = new Peer(); await partial.reset(); await partial.inventory([camera()], 1, false);
  assert.equal(partial.session.inventoryComplete, false);
  assert.throws(() => partial.session.camera("front"), /camera-not-ready/);
  partial.session.close();
});

test("owned negative inventory errors fail closed, while an unowned error does not create authority", async () => {
  const peer = new Peer(); await peer.reset();
  const request = await peer.request("inventory_request");
  await peer.reply(request, "error", {code: "stale-scope", message: ""});
  await tick();
  assert.equal(peer.session.inventoryComplete, false);
  assert.equal(peer.session.isClosed, false);
  assert.equal(await peer.session.receive({...request, request_id: randomUUID(), kind: "error", payload: {code: "ok", message: ""}}), false);
  peer.session.close();
});

test("snapshot bytes require the explicitly owned job and transfer and resolve only after bounded consumption", async () => {
  let authorized: Parameters<BulkChannel["authorizeReceive"]>[0] | null = null;
  let finish!: () => void;
  const completed = new Promise<void>(resolve => { finish = resolve; });
  const bulk = {authorizeReceive: (operation: Parameters<BulkChannel["authorizeReceive"]>[0]) => {
    authorized = operation; return {wait: () => completed, cancel: async () => {}};
  }, handleControl: async () => true, close: async () => {}} as unknown as BulkChannel;
  const peer = new Peer({bulk}), delegate = await peer.ready();
  const snapshot = delegate.snapshot(640, 480), request = await peer.request("snapshot_request"), transferId = randomUUID();
  await peer.reply(request, "snapshot_result", {scope: request.payload.scope, job_id: request.payload.job_id,
    status: "ready", code: "ok", transfer_id: transferId});
  await peer.session.receive({v: 1, boot_id: boot, request_id: randomUUID(), kind: "transfer_open",
    payload: {scope: request.payload.scope, operation_id: request.payload.job_id, transfer_id: transferId,
      kind: "snapshot", total_bytes: 4, chunk_bytes: 4, window_chunks: 1}});
  assert.notEqual(authorized, null);
  const operation = authorized as unknown as Parameters<BulkChannel["authorizeReceive"]>[0];
  assert.equal(operation.valid?.(), true);
  await operation.sink(Buffer.from("test"), new AbortController().signal);
  await operation.onClose?.("ok"); finish();
  assert.deepEqual(await snapshot, Buffer.from("test"));
  peer.session.close();
});

test("a valid but unsolicited transfer fails closed before any payload is accepted", async () => {
  let called = false;
  const peer = new Peer({bulk: {authorizeReceive: () => { called = true; }, handleControl: async () => true, close: async () => {}} as unknown as BulkChannel});
  await assert.rejects(peer.session.receive({v: 1, boot_id: boot, request_id: randomUUID(), kind: "transfer_open",
    payload: {scope, operation_id: randomUUID(), transfer_id: randomUUID(), kind: "snapshot", total_bytes: 4, chunk_bytes: 4, window_chunks: 1}}), /unsolicited-transfer/);
  assert.equal(called, false); assert.equal(peer.session.isClosed, true);
});


test("session snapshot delegate composes with both real bounded binary transports", async () => {
  const [hapSocket, pythonSocket] = duplexPair();
  let peer!: Peer, python!: BulkChannel;
  const hap = new BulkChannel(hapSocket, {bootId: boot, role: "hap", sendControl: async (kind, payload, requestId) => {
    await python.handleControl({v: 1, kind, boot_id: boot, request_id: requestId ?? randomUUID(), payload});
  }});
  python = new BulkChannel(pythonSocket, {bootId: boot, role: "python", sendControl: async (kind, payload, requestId) => {
    await peer.session.receive({v: 1, kind, boot_id: boot, request_id: requestId ?? randomUUID(), payload});
  }});
  peer = new Peer({bulk: hap}); hap.start(); python.start();
  try {
    const delegate = await peer.ready(), image = Buffer.alloc(100_000, 0x65);
    const snapshot = delegate.snapshot(640, 480), request = await peer.request("snapshot_request"), transferId = randomUUID();
    await peer.reply(request, "snapshot_result", {scope: request.payload.scope, job_id: request.payload.job_id,
      status: "ready", code: "ok", transfer_id: transferId});
    const sending = python.send({scope: request.payload.scope as MediaScope, operationId: request.payload.job_id as string,
      transferId, kind: "snapshot", totalBytes: image.length,
      source: (async function* () { yield image.subarray(0, 65_536); yield image.subarray(65_536); })()});
    assert.deepEqual(await snapshot, image); await sending;
    assert.deepEqual(hap.counts, [0, 0]); assert.deepEqual(python.counts, [0, 0]);
  } finally { peer.session.close(); await Promise.all([hap.close(), python.close()]); }
});


test("contradictory positive status with a failure code never grants policy or live authority", async () => {
  const peer = new Peer(); await peer.reset(); await peer.inventory();
  const delegate = peer.session.camera("front"), update = delegate.updatePolicy(policy);
  const request = await peer.request("policy_update");
  await peer.reply(request, "policy_ack", {scope: request.payload.scope, accepted: true, code: "policy-denied"});
  await assert.rejects(update, /policy-rejected/); assert.equal(delegate.ready, false);
  peer.session.close();
  const ready = new Peer(), camera = await ready.ready();
  const live = camera.acquireLive(1000), lease = await ready.request("lease_request");
  await ready.reply(lease, "lease_result", {scope: lease.payload.scope, session_id: lease.payload.session_id,
    status: "accepted", ttl_ms: 1000, code: "busy"});
  await assert.rejects(live, /lease-rejected/); ready.session.close();
});

test("inventory invalidation restores acknowledged policy only after complete unchanged-scope refresh", async () => {
  const peer = new Peer(), delegate = await peer.ready(37);
  await peer.reset(account, 3);
  assert.equal(delegate.ready, false);
  await assert.rejects(delegate.acquireLive(), /stale-camera/);
  await peer.inventory([camera("front", 37)], 3);
  assert.equal(delegate.ready, true);
  await peer.reset(account, 4);
  assert.equal(delegate.ready, false);
  await peer.inventory([camera("front", 38)], 4);
  assert.equal(delegate.info.scope.privacy_generation, 38);
  assert.equal(delegate.ready, false);
  await assert.rejects(delegate.acquireLive(), /policy-denied/);
  peer.session.close();
});


test("unchanged authoritative inventory notice preserves live ownership and its negotiated TTL", async () => {
  const peer = new Peer(), delegate = await peer.ready(37);
  const acquiring = delegate.acquireLive(1000), request = await peer.request("lease_request");
  await peer.reply(request, "lease_result", {scope: request.payload.scope, session_id: request.payload.session_id,
    status: "accepted", code: "ok", ttl_ms: 1000});
  const live = await acquiring;
  assert.equal(peer.session.inventoryRevision, 2);
  await peer.reset(account, 2);
  assert.equal(delegate.ready, true); assert.equal(peer.session.inventoryComplete, true);
  assert.equal(peer.writes.some(message => message.kind === "lease_stop" || message.kind === "inventory_request"), false);
  const renewal = live.renew(1000), renew = await peer.request("lease_renew");
  assert.equal(renew.payload.session_id, live.sessionId);
  assert.deepEqual(renew.payload.scope, request.payload.scope);
  assert.equal(renew.payload.ttl_ms, 1000);
  await peer.reply(renew, "lease_renew_result", {scope: renew.payload.scope, session_id: live.sessionId,
    status: "accepted", code: "ok", ttl_ms: 1000});
  await renewal;
  const stopping = live.close(), stop = await peer.request("lease_stop");
  await peer.reply(stop, "lease_stop_result", {scope: stop.payload.scope, session_id: live.sessionId, status: "stopped", code: "ok"});
  await stopping; peer.session.close();
});

test("same inventory notice retries when the previously fetched revision was incomplete", async () => {
  const peer = new Peer(); await peer.reset(account, 1); await peer.inventory([camera()], 1, false);
  assert.equal(peer.session.inventoryComplete, false);
  await peer.reset(account, 1); await peer.inventory([camera()], 1, true);
  assert.equal(peer.session.inventoryComplete, true);
  assert.equal(peer.session.camera("front").ready, false);
  peer.session.close();
});

test('cancelled snapshot keeps exact late READY/OPEN drain authority and another image still completes', async () => {
  const sockets=duplexPair(),queues=[Promise.resolve(),Promise.resolve()];let peer!:Peer,python!:BulkChannel;
  const controls:Envelope[]=[];
  const send=(index:number,kind:Kind,payload:JsonObject,id:string=randomUUID()):Promise<void>=>{
    const message:Envelope={v:1,kind,payload,boot_id:boot,request_id:id};controls.push(message);
    queues[index]=queues[index]!.then(async()=>{if(index===0)await python.handleControl(message);else await peer.session.receive(message);});void queues[index]!.catch(()=>{});return Promise.resolve();
  };
  const hap=new BulkChannel(sockets[0],{bootId:boot,role:'hap',sendControl:(...args)=>send(0,...args)});
  python=new BulkChannel(sockets[1],{bootId:boot,role:'python',sendControl:(...args)=>send(1,...args)});
  peer=new Peer({bulk:hap});hap.start();python.start();
  try {
    const delegate=await peer.ready(),abort=new AbortController();
    const first=delegate.snapshot(320,240,'event',abort.signal),firstRejected=assert.rejects(first,/snapshot-cancelled/),request=await peer.request('snapshot_request');
    abort.abort();await firstRejected;
    const transferId=randomUUID();await peer.reply(request,'snapshot_result',{scope:request.payload.scope,job_id:request.payload.job_id,status:'ready',code:'ok',transfer_id:transferId});
    await assert.rejects(python.send({scope:request.payload.scope as MediaScope,operationId:request.payload.job_id as string,transferId,kind:'snapshot',totalBytes:1,source:(async function*(){yield Buffer.from('a');})()}),/cancelled/);
    assert.equal(controls.some(message=>message.kind==='transfer_credit'&&message.payload.transfer_id===transferId),false);
    assert.equal(peer.session.isClosed,false);assert.equal(hap.closed,false);assert.equal(python.closed,false);
    const second=delegate.snapshot(320,240,'event'),next=await peer.request('snapshot_request'),nextId=randomUUID();
    await peer.reply(next,'snapshot_result',{scope:next.payload.scope,job_id:next.payload.job_id,status:'ready',code:'ok',transfer_id:nextId});
    await python.send({scope:next.payload.scope as MediaScope,operationId:next.payload.job_id as string,transferId:nextId,kind:'snapshot',totalBytes:4,source:(async function*(){yield Buffer.from('test');})()});
    assert.deepEqual(await second,Buffer.from('test'));assert.equal(peer.session.isClosed,false);
  } finally {peer.session.close();await Promise.allSettled([hap.close(),python.close()]);}
});

test('aborting one concurrent snapshot leaves the second owned caller deliverable',async()=>{
  const jobs:Parameters<BulkChannel['authorizeReceive']>[0][]=[];const resolvers:(()=>void)[]=[];
  const bulk={authorizeReceive:(operation:Parameters<BulkChannel['authorizeReceive']>[0])=>{
    jobs.push(operation);let resolve!:()=>void,reject!:(error:Error)=>void;const promise=new Promise<void>((yes,no)=>{resolve=yes;reject=no;});resolvers.push(resolve);
    return {wait:()=>promise,cancel:async()=>{reject(new Error('cancelled'));}};
  },handleControl:async()=>true,close:async()=>{}} as unknown as BulkChannel;
  const peer=new Peer({bulk}),delegate=await peer.ready(),abort=new AbortController();
  const first=delegate.snapshot(640,480,'event',abort.signal),rejected=assert.rejects(first),one=await peer.request('snapshot_request');
  const second=delegate.snapshot(640,480,'event'),two=await peer.request('snapshot_request');
  const transferIds=[randomUUID(),randomUUID()];
  for(const [index,request] of [one,two].entries()) {
    await peer.reply(request,'snapshot_result',{scope:request.payload.scope,job_id:request.payload.job_id,status:'ready',code:'ok',transfer_id:transferIds[index]});
    await peer.session.receive({v:1,kind:'transfer_open',boot_id:boot,request_id:randomUUID(),payload:{scope:request.payload.scope,operation_id:request.payload.job_id,transfer_id:transferIds[index],kind:'snapshot',total_bytes:4,chunk_bytes:4,window_chunks:1}});
  }
  abort.abort();await rejected;assert.equal(jobs[1]!.valid!(),true);
  await jobs[1]!.sink(Buffer.from('test'),new AbortController().signal);resolvers[1]!();assert.deepEqual(await second,Buffer.from('test'));
  assert.equal(peer.session.isClosed,false);peer.session.close();
});

test('snapshot RPC expiry preserves only correlated late READY cleanup admission',async context=>{
  context.mock.timers.enable({apis:['setTimeout']});let cancelled=0,authorized=0;
  const bulk={authorizeReceive:()=>{authorized++;return {wait:()=>Promise.reject(new Error('cancelled')),cancel:async()=>{cancelled++;}};},
    handleControl:async()=>true,close:async()=>{}} as unknown as BulkChannel;
  const peer=new Peer({bulk,timeout:20}),delegate=await peer.ready(),abort=new AbortController();
  const snapshot=delegate.snapshot(640,480,'event',abort.signal),rejected=assert.rejects(snapshot),request=await peer.request('snapshot_request');
  abort.abort();await rejected;context.mock.timers.tick(20);await tick();assert.equal(peer.session.pendingRequests,1);
  const transferId=randomUUID();await peer.reply(request,'snapshot_result',{scope:request.payload.scope,job_id:request.payload.job_id,status:'ready',code:'ok',transfer_id:transferId});
  await peer.session.receive({v:1,kind:'transfer_open',boot_id:boot,request_id:randomUUID(),payload:{scope:request.payload.scope,operation_id:request.payload.job_id,transfer_id:transferId,kind:'snapshot',total_bytes:4,chunk_bytes:4,window_chunks:1}});
  await tick();assert.equal(authorized,1);assert.equal(cancelled,1);assert.equal(peer.session.pendingRequests,0);assert.equal(peer.session.isClosed,false);peer.session.close();
});

test("control state keeps nullable observations and validates the full request scope",async()=>{
  const peer=new Peer(),delegate=await peer.ready();
  const state=delegate.controlState(),request=await peer.request("control_state_request");
  await peer.reply(request,"control_state_result",{scope:request.payload.scope,operation_id:request.payload.operation_id,status:"accepted",code:"ok",light:null,siren:null});
  assert.deepEqual(await state,{light:null,siren:null});
  const pending=delegate.controlState(),rejected=assert.rejects(pending),other=await peer.request("control_state_request");
  await assert.rejects(peer.reply(other,"control_state_result",{scope:{...other.payload.scope as MediaScope,privacy_generation:100},operation_id:other.payload.operation_id,
    status:"accepted",code:"ok",light:false,siren:null}));await rejected;peer.session.close();
});
test("reset quiescence closes admission synchronously while owned stop correlation remains live",async()=>{
  const peer=new Peer(),delegate=await peer.ready();
  const pending=delegate.acquireLive(),request=await peer.request("lease_request");
  await peer.reply(request,"lease_result",{scope:request.payload.scope,session_id:request.payload.session_id,status:"accepted",code:"ok",ttl_ms:10000});
  await pending;peer.session.quiesce();
  assert.throws(()=>peer.session.camera("front"),/camera-not-ready/);
  await assert.rejects(delegate.controlState(),/stale-camera/);await assert.rejects(delegate.setControl("light",true),/stale-camera/);
  const stop=await peer.request("lease_stop");await peer.reply(stop,"lease_stop_result",{scope:stop.payload.scope,session_id:stop.payload.session_id,status:"stopped",code:"ok"});
  assert.equal(peer.session.isClosed,false);peer.session.close();
});
