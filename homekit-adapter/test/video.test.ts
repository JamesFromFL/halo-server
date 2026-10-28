import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { EventEmitter } from "node:events";
import { test } from "node:test";
import {
  encode as tlvEncode, decode as tlvDecode, writeUInt16, writeUInt32, uuid,
  AudioBitrate, AudioStreamingCodecType, Characteristic, H264Level, H264Profile, Service, SRTPCryptoSuites, StreamRequestTypes,
  VideoCodecPacketizationMode, VideoCodecType, type PrepareStreamRequest, type PrepareStreamResponse, type StartStreamRequest, type StreamingRequest,
} from "@homebridge/hap-nodejs";
import { CameraAccessory } from "../src/camera.js";
import { AdapterSession, type NativeCamera } from "../src/session.js";
import { HapVideoDelegate, literalUnicastAddress, PREPARED_VIDEO_TIMEOUT_MS } from "../src/video.js";
import { receiveTerminalCleanup } from "../src/main.js";
import { ProtocolError, type AccessoryAudio, type Envelope, type JsonObject, type MediaScope } from "../src/protocol.js";

const boot = "00000000-0000-0000-0000-000000000001", account = {profile_id: "00000000-0000-0000-0000-000000000002", account_generation: 1};
const tick = (): Promise<void> => new Promise(resolve => setImmediate(resolve));
const scope: MediaScope = {...account, camera_id: "front", device_generation: 1, privacy_generation: 1};
const camera = (): NativeCamera => ({scope: {...scope}, name: "Fixture", family: "fixture", kind: "fixture", model: "Fixture",
  capabilities: {video: true, audio: false, snapshot: false, talkback: false, light: false, siren: false,
    doorbell: false, battery: false, evidence: ["halo:video"]}, connection_state: null, battery_percent: null, observed_format: null});
class Peer {
  sent: Envelope[] = [];
  held: Envelope[] = [];
  failures: string[] = [];
  current = camera();
  revision = 1;
  hold: "video_prepare" | "policy_update" | "video_start" | "video_reconfigure" | "video_stop" | null = null;
  rejectStop = false;
  constructor(readonly audio: AccessoryAudio | null = null) {
    this.current.capabilities.audio = audio !== null;
    this.current.capabilities.talkback = audio?.speaker === true;
    if (audio !== null) this.current.capabilities.evidence.push("halo:audio", "halo:talkback");
  }
  session = new AdapterSession(boot, {send: async message => {
    this.sent.push(message);
    if (message.kind === this.hold) this.held.push(message);
    else queueMicrotask(() => { void this.answer(message); });
  }, onFailure: error => this.failures.push(error.code)});
  async ready(): Promise<void> {
    await this.session.receive({v: 1, kind: "inventory_reset", boot_id: boot, request_id: randomUUID(), payload: {scope: account, revision: 1}});
    await tick(); assert.equal(this.session.inventoryComplete, true);
  }
  async answer(request: Envelope): Promise<void> {
    const reply = (kind: Envelope["kind"], payload: JsonObject) => this.session.receive({...request, kind, payload});
    if (request.kind === "inventory_request") await reply("inventory_page", {scope: account, cursor: null, next_cursor: null,
      revision: this.revision, total: 1, complete: true, devices: [this.current]});
    else if (request.kind === "policy_update") {
      this.current.scope.privacy_generation++; this.revision++;
      await reply("policy_ack", {scope: request.payload.scope, accepted: true, code: "ok"});
    } else if (request.kind === "video_prepare") {
      const audio = request.payload.audio as {key: string; salt: string} | null;
      await reply("video_prepare_result", {
        scope: request.payload.scope, session_id: request.payload.session_id, status: "accepted", code: "ok",
        endpoint: {address: request.payload.source_address, port: 50000, ssrc: 1234, key: request.payload.key, salt: request.payload.salt,
          audio: audio === null ? null : {port: 50002, ssrc: 5678, key: audio.key, salt: audio.salt}},
      });
    }
    else if (["video_start", "video_reconfigure", "video_stop"].includes(request.kind)) await reply(`${request.kind}_result` as Envelope["kind"], {
      scope: request.payload.scope, session_id: request.payload.session_id,
      status: request.kind === "video_stop" && this.rejectStop ? "rejected" : "accepted",
      code: request.kind === "video_stop" && this.rejectStop ? "source-failed" : "ok",
    });
  }
  delegate() {
    let active = true;
    const ended: string[] = [], failures: string[] = [];
    const value = new HapVideoDelegate(this.session.camera("front"), {active: () => active, audio: () => this.audio,
      forceStop: id => ended.push(id), failure: () => failures.push("failed")});
    return {value, ended, failures, setActive: (enabled: boolean) => { active = enabled; }};
  }
}
const prepareRequest = (id = "HAP synthetic ID"): PrepareStreamRequest => ({sessionID: id, sourceAddress: "127.0.0.1", targetAddress: "127.0.0.1", addressVersion: "ipv4",
  video: {port: 50100, srtpCryptoSuite: SRTPCryptoSuites.AES_CM_128_HMAC_SHA1_80, srtp_key: Buffer.alloc(16, 7), srtp_salt: Buffer.alloc(14, 9)},
  audio: {port: 50102, srtpCryptoSuite: SRTPCryptoSuites.AES_CM_128_HMAC_SHA1_80, srtp_key: Buffer.alloc(16, 8), srtp_salt: Buffer.alloc(14, 10)}});
const prepare = (delegate: HapVideoDelegate, request = prepareRequest()): Promise<PrepareStreamResponse> =>
  new Promise((resolve, reject) => delegate.prepareStream(request, (error, result) => error ? reject(error) : resolve(result!)));
const handle = (delegate: HapVideoDelegate, request: StreamingRequest): Promise<void> =>
  new Promise((resolve, reject) => delegate.handleStreamRequest(request, error => error ? reject(error) : resolve()));
const startRequest = (id: string, ssrc: number): StartStreamRequest => ({sessionID: id, type: StreamRequestTypes.START,
  video: {codec: VideoCodecType.H264, profile: H264Profile.BASELINE, level: H264Level.LEVEL3_1,
    packetizationMode: VideoCodecPacketizationMode.NON_INTERLEAVED, width: 320, height: 240, fps: 15, pt: 99,
    ssrc, max_bit_rate: 500, rtcp_interval: 0.5, mtu: 1200}, audio: {} as StartStreamRequest["audio"]});
const publicStreamValues = (id: string) => {
  const crypto = tlvEncode(1, 0, 2, Buffer.alloc(16, 7), 3, Buffer.alloc(14, 9));
  const interval = Buffer.alloc(4); interval.writeFloatLE(.5);
  return {
    setup: tlvEncode(1, uuid.write(id), 3,
      tlvEncode(1, 0, 2, "127.0.0.1", 3, writeUInt16(50100), 4, writeUInt16(50102)), 4, crypto, 5, crypto).toString("base64"),
    start: tlvEncode(1, tlvEncode(1, uuid.write(id), 2, 1),
      2, tlvEncode(1, 0, 2, tlvEncode(1, 0, 2, 0, 3, 0),
        3, tlvEncode(1, writeUInt16(320), 2, writeUInt16(240), 3, 15),
        4, tlvEncode(1, 99, 2, writeUInt32(4321), 3, writeUInt16(500), 4, interval, 5, writeUInt16(1200))),
      3, tlvEncode(1, 3, 2, tlvEncode(1, 1, 2, 0, 3, 1, 4, 20),
        3, tlvEncode(1, 110, 2, writeUInt32(9876), 3, writeUInt16(24), 4, interval, 6, 13), 4, 0)).toString("base64"),
    reconfigure: tlvEncode(1, tlvEncode(1, uuid.write(id), 2, 4),
      2, tlvEncode(3, tlvEncode(1, writeUInt16(640), 2, writeUInt16(480), 3, 30),
        4, tlvEncode(3, writeUInt16(1000), 4, interval))).toString("base64"),
    stop: tlvEncode(1, tlvEncode(1, uuid.write(id), 2, 0)).toString("base64"),
  };
};

test("public HAP delegates prepare/start/reconfigure/stop with real scoped RPC and no per-viewer policy reset", async () => {
  const peer = new Peer(); await peer.ready(); const delegate = peer.delegate();
  const [first, second] = await Promise.all([prepare(delegate.value, prepareRequest("first")), prepare(delegate.value, prepareRequest("second"))]);
  assert.equal(peer.sent.filter(message => message.kind === "policy_update").length, 1);
  const prepares = peer.sent.filter(message => message.kind === "video_prepare");
  assert.equal(prepares.length, 2); assert.notEqual(prepares[0]!.payload.session_id, "first");
  assert.notEqual(prepares[0]!.payload.session_id, prepares[1]!.payload.session_id);
  assert.equal((prepares[0]!.payload.scope as MediaScope).privacy_generation, 2);
  assert.equal((first.video as {ssrc: number}).ssrc, 1234);
  assert.equal((first.video as {ssrc: number}).ssrc, (second.video as {ssrc: number}).ssrc);
  await handle(delegate.value, startRequest("first", (first.video as {ssrc: number}).ssrc));
  await handle(delegate.value, {sessionID: "first", type: StreamRequestTypes.RECONFIGURE,
    video: {width: 640, height: 480, fps: 30, max_bit_rate: 1000, rtcp_interval: 1}});
  const reconfigure = peer.sent.find(message => message.kind === "video_reconfigure")!.payload.config as Record<string, unknown>;
  assert.equal(reconfigure.width, 640); assert.equal(reconfigure.payload_type, 99); assert.equal(reconfigure.mtu, 1200);
  await handle(delegate.value, {sessionID: "first", type: StreamRequestTypes.STOP});
  await handle(delegate.value, {sessionID: "first", type: StreamRequestTypes.STOP});
  assert.equal(peer.sent.filter(message => message.kind === "video_stop").length, 1);
  await delegate.value.close(); peer.session.close();
});

test("endpoint addresses reject DNS, multicast, unspecified and malformed scopes without touching native IPC", async () => {
  for (const address of ["example.invalid", "0.0.0.0", "224.1.1.1", "255.255.255.255", "127.00.0.1"]) assert.equal(literalUnicastAddress(address, "ipv4"), false);
  for (const address of ["::", "0:0:0:0:0:0:0:0", "ff02::1", "::ffff:e000:1", "::ffff:0.0.0.0",
    "fe80::1%", "fe80::1%0", "fe80::1%lo/escape", "fe80::1%lo%extra", "::1%lo"]) assert.equal(literalUnicastAddress(address, "ipv6"), false);
  for (const address of ["::1", "fe80::1%lo", "fe80::1%1"]) assert.equal(literalUnicastAddress(address, "ipv6"), true);
  const peer = new Peer(); await peer.ready(); const delegate = peer.delegate();
  const request = prepareRequest(); request.targetAddress = "239.1.1.1";
  await assert.rejects(prepare(delegate.value, request), /camera-stream-unavailable/);
  assert.equal(peer.sent.filter(message => message.kind === "video_prepare").length, 0);
  await delegate.value.close(); peer.session.close();
});

test("prepared timeout maps the original HAP ID back to forceStop and releases only its UUID", async context => {
  context.mock.timers.enable({apis: ["setTimeout"]});
  const peer = new Peer(); await peer.ready(); const delegate = peer.delegate();
  await prepare(delegate.value, prepareRequest("not-a-UUID-HAP-session"));
  context.mock.timers.tick(PREPARED_VIDEO_TIMEOUT_MS); await tick();
  assert.deepEqual(delegate.ended, ["not-a-UUID-HAP-session"]); assert.equal(delegate.value.count, 0);
  assert.equal(peer.sent.filter(message => message.kind === "video_stop").length, 1);
  await delegate.value.close(); peer.session.close();
});

test("STOP while prepare waits compensates a late accepted resource without returning an endpoint", async () => {
  const peer = new Peer(); await peer.ready(); peer.hold = "video_prepare"; const delegate = peer.delegate();
  const pending = prepare(delegate.value), rejected = assert.rejects(pending, /camera-stream-unavailable/); await tick();
  assert.equal(peer.held.length, 1);
  await handle(delegate.value, {sessionID: "HAP synthetic ID", type: StreamRequestTypes.STOP});
  await peer.answer(peer.held.shift()!); await rejected;
  assert.equal(delegate.value.count, 0); assert.equal(peer.sent.filter(message => message.kind === "video_stop").length, 1);
  await delegate.value.close(); peer.session.close();
});

test("an Active change fences pending admission before policy completion and does not restore streaming", async () => {
  const peer = new Peer(); await peer.ready(); peer.hold = "policy_update"; const delegate = peer.delegate();
  const pending = prepare(delegate.value), rejected = assert.rejects(pending, /camera-stream-unavailable/); await tick();
  delegate.setActive(false); const disabling = delegate.value.activeChanged();
  peer.hold = null; await peer.answer(peer.held.shift()!); await rejected; await disabling;
  assert.equal(peer.sent.filter(message => message.kind === "video_prepare").length, 0);
  const policies = peer.sent.filter(message => message.kind === "policy_update");
  assert.equal((policies.at(-1)!.payload.policy as Record<string, unknown>).streaming, false);
  await delegate.value.close(); peer.session.close();
});

test("video_ended notices require exact full scope and cannot stop an unrelated HAP stream", async () => {
  const peer = new Peer(); await peer.ready(); const delegate = peer.delegate();
  await prepare(delegate.value, prepareRequest("first")); await prepare(delegate.value, prepareRequest("second"));
  const first = peer.sent.find(message => message.kind === "video_prepare")!;
  const notice = {v: 1 as const, kind: "video_ended" as const, boot_id: boot, request_id: randomUUID(),
    payload: {scope: {...first.payload.scope as MediaScope, privacy_generation: 1}, session_id: first.payload.session_id, code: "expired"}};
  await peer.session.receive(notice); assert.equal(delegate.value.count, 2); assert.deepEqual(delegate.ended, []);
  notice.payload.scope = first.payload.scope as MediaScope;
  await peer.session.receive(notice); assert.equal(delegate.value.count, 1); assert.deepEqual(delegate.ended, ["first"]);
  await delegate.value.close(); peer.session.close();
});

test("public RTP Active characteristics stop all camera video and keep the saved disable policy", async () => {
  const peer = new Peer(); await peer.ready();
  const accessory = new CameraAccessory({camera_id: "front", accessory_id: "00000000-0000-0000-0000-000000000003", username: "02:11:22:33:44:55", doorbell: false},
    peer.current, {native: peer.session.camera("front")});
  await accessory.synchronizePolicy(); await prepare(accessory.streamingDelegate!);
  const service = accessory.accessory.services.find(service => service.UUID === Service.CameraRTPStreamManagement.UUID)!;
  service.updateCharacteristic(Characteristic.Active, false); await tick(); await tick();
  assert.equal(accessory.streamingDelegate!.count, 0);
  await assert.rejects(prepare(accessory.streamingDelegate!), /camera-stream-unavailable/);
  await accessory.synchronizePolicy();
  const policies = peer.sent.filter(message => message.kind === "policy_update");
  assert.equal((policies.at(-1)!.payload.policy as Record<string, unknown>).streaming, false);
  accessory.retire(); await accessory.closeStreams(); peer.session.close();
});

test("graceful ending still consumes correlated cleanup replies and rejects new operation results", async () => {
  const peer = new Peer(); await peer.ready(); const delegate = peer.delegate();
  await prepare(delegate.value); peer.hold = "video_stop";
  const closing = delegate.value.close(); await tick();
  const request = peer.held.shift()!;
  assert.equal(await receiveTerminalCleanup(peer.session, boot, {...request, kind: "video_start_result",
    payload: {...request.payload, status: "accepted", code: "ok"}}), false);
  assert.equal(await receiveTerminalCleanup(peer.session, boot, {...request, kind: "video_stop_result",
    payload: {...request.payload, status: "accepted", code: "ok"}}), true);
  await closing; assert.equal(delegate.value.count, 0); peer.session.close();
});

test("metadata refresh preserves prepared ownership while a confirmed privacy generation change retires it", async () => {
  const peer = new Peer(); await peer.ready(); const delegate = peer.delegate();
  await prepare(delegate.value); peer.current.name = "Renamed"; peer.revision++;
  await peer.session.receive({v: 1, kind: "inventory_reset", boot_id: boot, request_id: randomUUID(),
    payload: {scope: account, revision: peer.revision}});
  await tick(); assert.equal(delegate.value.count, 1);
  assert.equal(peer.sent.filter(message => message.kind === "video_stop").length, 0);
  peer.current.scope.privacy_generation++; peer.revision++;
  await peer.session.receive({v: 1, kind: "inventory_reset", boot_id: boot, request_id: randomUUID(),
    payload: {scope: account, revision: peer.revision}});
  await tick(); assert.equal(delegate.value.count, 0);
  assert.deepEqual(delegate.ended, ["HAP synthetic ID"]);
  await delegate.value.close(); peer.session.close();
});

test("an unadvertised packetization mode retires its prepared resource before reporting failure", async () => {
  const peer = new Peer(); await peer.ready(); const delegate = peer.delegate();
  await prepare(delegate.value);
  const request = startRequest("HAP synthetic ID", 4321);
  request.video.packetizationMode = 1 as VideoCodecPacketizationMode;
  await assert.rejects(handle(delegate.value, request), /camera-stream-unavailable/);
  assert.equal(peer.sent.filter(message => message.kind === "video_start").length, 0);
  assert.equal(peer.sent.filter(message => message.kind === "video_stop").length, 1);
  assert.equal(delegate.value.count, 0);
  await delegate.value.close(); peer.session.close();
});

test("failed START remains charged until exact cleanup completes and then permits an immediate retry", async () => {
  const peer = new Peer(); await peer.ready(); const delegate = peer.delegate();
  await prepare(delegate.value, prepareRequest("first"));
  await prepare(delegate.value, prepareRequest("second"));
  const first = peer.sent.find(message => message.kind === "video_prepare")!;
  const request = startRequest("first", 4321); request.video.codec = 1 as VideoCodecType;
  peer.hold = "video_stop";
  let completed = false;
  const rejected = assert.rejects(handle(delegate.value, request).finally(() => {completed = true;}), /camera-stream-unavailable/);
  await tick();
  assert.equal(completed, false); assert.equal(delegate.value.count, 2);
  await assert.rejects(prepare(delegate.value, prepareRequest("third")), /camera-stream-unavailable/);
  const stop = peer.held.shift()!;
  assert.equal(stop.payload.session_id, first.payload.session_id);
  peer.hold = null; await peer.answer(stop); await rejected;
  assert.equal(delegate.value.count, 1);
  await prepare(delegate.value, prepareRequest("third"));
  assert.equal(delegate.value.count, 2);
  await delegate.value.close(); peer.session.close();
});

test("terminal active phase errors retire only their owner even when HAP sends no STOP", async () => {
  for (const failure of ["duplicate-start", "invalid-reconfigure"] as const) {
    const peer = new Peer(); await peer.ready(); const delegate = peer.delegate();
    await prepare(delegate.value, prepareRequest("first"));
    await prepare(delegate.value, prepareRequest("second"));
    await handle(delegate.value, startRequest("first", 4321));
    const first = peer.sent.find(message => message.kind === "video_prepare")!;
    const request: StreamingRequest = failure === "duplicate-start" ? startRequest("first", 4321) : {
      sessionID: "first", type: StreamRequestTypes.RECONFIGURE,
      video: {width: 0, height: 240, fps: 15, max_bit_rate: 500, rtcp_interval: .5},
    };
    await assert.rejects(handle(delegate.value, request), /camera-stream-unavailable/);
    assert.equal(delegate.value.count, 1);
    const stops = peer.sent.filter(message => message.kind === "video_stop");
    assert.equal(stops.length, 1); assert.equal(stops[0]!.payload.session_id, first.payload.session_id);
    await assert.rejects(handle(delegate.value, startRequest("unknown", 4321)), /camera-stream-unavailable/);
    assert.equal(peer.sent.filter(message => message.kind === "video_stop").length, 1);
    await prepare(delegate.value, prepareRequest("retry"));
    assert.equal(delegate.value.count, 2);
    await delegate.value.close(); peer.session.close();
  }
});

test("selected video SSRC is an independent uint32 and malformed values retire before native START", async () => {
  for (const ssrc of [0, 0xffffffff, -1, .5, 0x1_0000_0000, NaN, Infinity]) {
    const peer = new Peer(); await peer.ready(); const delegate = peer.delegate();
    try {
      await prepare(delegate.value);
      if (ssrc === 0 || ssrc === 0xffffffff) {
        await handle(delegate.value, startRequest("HAP synthetic ID", ssrc));
        assert.equal(peer.sent.filter(message => message.kind === "video_start").length, 1);
      } else {
        await assert.rejects(handle(delegate.value, startRequest("HAP synthetic ID", ssrc)), /camera-stream-unavailable/);
        assert.equal(peer.sent.filter(message => message.kind === "video_start").length, 0);
        assert.equal(delegate.value.count, 0);
        assert.equal(peer.sent.filter(message => message.kind === "video_stop").length, 1);
      }
    } finally {
      await delegate.value.close(); peer.session.close();
    }
  }
});

test("failed terminal cleanup escalates the channel and cannot admit another native session", async () => {
  const peer = new Peer(); await peer.ready(); const delegate = peer.delegate();
  await prepare(delegate.value); peer.rejectStop = true;
  const request = startRequest("HAP synthetic ID", 4321);
  request.video.packetizationMode = 1 as VideoCodecPacketizationMode;
  await assert.rejects(handle(delegate.value, request), /camera-stream-unavailable/);
  assert.deepEqual(delegate.failures, ["failed"]);
  assert.deepEqual(peer.failures, ["video-stop-rejected"]);
  await assert.rejects(prepare(delegate.value, prepareRequest("retry")), /camera-stream-unavailable/);
  assert.equal(peer.sent.filter(message => message.kind === "video_prepare").length, 1);
  await delegate.value.close(); peer.session.close();
});

test("selected audio SSRC is independent and malformed values retire its exact native reservation", async () => {
  for (const ssrc of [0, 0xffffffff, -1, .5, 0x1_0000_0000, NaN]) {
    const peer = new Peer({microphone: true, speaker: true, microphone_muted: false, speaker_muted: false,
      microphone_volume: 100, speaker_volume: 100, revision: 1});
    await peer.ready(); const delegate = peer.delegate();
    try {
      await prepare(delegate.value);
      const request = startRequest("HAP synthetic ID", 4321);
      request.audio = {codec: AudioStreamingCodecType.OPUS, channel: 1, bit_rate: AudioBitrate.VARIABLE, sample_rate: 24,
        packet_time: 20, pt: 110, ssrc, max_bit_rate: 24, rtcp_interval: .5, comfort_pt: 13, comfortNoiseEnabled: false};
      if (ssrc === 0 || ssrc === 0xffffffff) {
        await handle(delegate.value, request);
        assert.equal(peer.sent.filter(message => message.kind === "video_start").length, 1);
      } else {
        await assert.rejects(handle(delegate.value, request), /camera-stream-unavailable/);
        assert.equal(peer.sent.filter(message => message.kind === "video_start").length, 0);
        assert.equal(delegate.value.count, 0);
        assert.equal(peer.sent.filter(message => message.kind === "video_stop").length, 1);
      }
    } finally {
      await delegate.value.close(); peer.session.close();
    }
  }
});

test("IPv4 media prepare accepts the IPv4-mapped source address passed by a dual-stack HAP connection", async () => {
  const peer = new Peer(); await peer.ready(); const delegate = peer.delegate();
  const request = prepareRequest("dual-stack mapped source");
  request.sourceAddress = "::ffff:127.0.0.1";
  try {
    const result = await prepare(delegate.value, request);
    assert.equal(result.addressOverride, "127.0.0.1");
    const prepared = peer.sent.find(message => message.kind === "video_prepare");
    assert.equal(prepared?.payload.source_address, "127.0.0.1");
  } finally {
    await delegate.value.close(); peer.session.close();
  }
});


for (const withAudio of [false, true]) test(`released HAP accepts independent controller SSRCs through mapped SetupEndpoints and SelectedRTP characteristics (audio=${withAudio})`, async () => {
  const audio: AccessoryAudio | null = withAudio ? {microphone: true, speaker: true, microphone_muted: false, speaker_muted: false,
    microphone_volume: 100, speaker_volume: 100, revision: 1} : null;
  const peer = new Peer(audio); await peer.ready();
  const accessory = new CameraAccessory({camera_id: "front", accessory_id: randomUUID(), username: "02:11:22:33:44:56", doorbell: false, audio},
    peer.current, {native: peer.session.camera("front")});
  const connection = Object.assign(new EventEmitter(), {
    localAddress: "::ffff:127.0.0.1", getLocalAddress: () => "127.0.0.1",
  });
  const id = randomUUID();
  const values = publicStreamValues(id);
  const service = accessory.accessory.services.find(item => item.UUID === Service.CameraRTPStreamManagement.UUID)!;
  try {
    const setup = service.getCharacteristic(Characteristic.SetupEndpoints);
    await setup.handleSetRequest(values.setup, connection as never);
    const response = tlvDecode(Buffer.from(await setup.handleGetRequest(connection as never) as string, "base64"));
    assert.equal(response[2]![0], 0);
    assert.equal(tlvDecode(response[3]!)[2]!.toString(), "127.0.0.1");
    assert.equal(peer.sent.find(item => item.kind === "video_prepare")!.payload.source_address, "127.0.0.1");
    await service.getCharacteristic(Characteristic.SelectedRTPStreamConfiguration)
      .handleSetRequest(values.start, connection as never);
    assert.equal(response[6]!.readUInt32LE(), 1234);
    if (withAudio) assert.equal(response[7]!.readUInt32LE(), 5678);
    assert.equal(peer.sent.filter(item => item.kind === "video_start").length, 1);
    const start = peer.sent.find(item => item.kind === "video_start")!;
    assert.equal(Object.hasOwn(start.payload.config as object, "ssrc"), false);
    if (withAudio) {
      assert.equal((start.payload.audio_config as JsonObject).payload_type, 110);
      assert.equal(Object.hasOwn(start.payload.audio_config as object, "ssrc"), false);
    } else assert.equal(start.payload.audio_config, null);
  } finally {
    connection.emit("closed");
    accessory.retire(); await accessory.closeStreams(); peer.session.close();
  }
});

for (const heldKind of ["video_prepare", "video_start", "video_reconfigure"] as const) {
  test(`released HAP END settles an old ${heldKind} callback before replacement SetupEndpoints`, async () => {
    const peer = new Peer(); await peer.ready();
    const accessory = new CameraAccessory({camera_id: "front", accessory_id: randomUUID(), username: "02:11:22:33:44:56", doorbell: false},
      peer.current, {native: peer.session.camera("front")});
    const connection = Object.assign(new EventEmitter(), {localAddress: "::ffff:127.0.0.1", getLocalAddress: () => "127.0.0.1"});
    const service = accessory.accessory.services.find(item => item.UUID === Service.CameraRTPStreamManagement.UUID)!;
    const setup = service.getCharacteristic(Characteristic.SetupEndpoints);
    const selected = service.getCharacteristic(Characteristic.SelectedRTPStreamConfiguration);
    const old = publicStreamValues(randomUUID()), replacement = publicStreamValues(randomUUID());
    try {
      let pending: Promise<unknown>;
      if (heldKind === "video_prepare") {
        peer.hold = heldKind;
        pending = setup.handleSetRequest(old.setup, connection as never);
      } else {
        await setup.handleSetRequest(old.setup, connection as never);
        if (heldKind === "video_reconfigure") await selected.handleSetRequest(old.start, connection as never);
        peer.hold = heldKind;
        pending = selected.handleSetRequest(heldKind === "video_start" ? old.start : old.reconfigure, connection as never);
      }
      let settled = false;
      const rejected = assert.rejects(pending).then(() => {settled = true;});
      await tick(); assert.equal(peer.held.length, 1);
      const late = peer.held.shift()!;
      await selected.handleSetRequest(old.stop, connection as never);
      await tick(); assert.equal(settled, true);
      peer.hold = null;
      await setup.handleSetRequest(replacement.setup, connection as never);
      const response = tlvDecode(Buffer.from(await setup.handleGetRequest(connection as never) as string, "base64"));
      assert.equal(response[2]![0], 0); assert.equal(accessory.streamingDelegate!.count, 1);
      await peer.answer(late); await rejected; await tick();
      await selected.handleSetRequest(replacement.start, connection as never);
      assert.equal(accessory.streamingDelegate!.count, 1);
      assert.equal(peer.sent.filter(item => item.kind === "video_stop").length, 1);
    } finally {
      connection.emit("closed"); accessory.retire(); await accessory.closeStreams(); peer.session.close();
    }
  });
}


test("mapped source normalization preserves unicast and negotiated-family rejection before IPC", async () => {
  const peer = new Peer(); await peer.ready(); const delegate = peer.delegate();
  try {
    for (const sourceAddress of ["::ffff:224.1.1.1", "::ffff:0.0.0.0", "::ffff:255.255.255.255",
      "::ffff:240.1.1.1", "::ffff:127.00.0.1", "::1", "example.invalid"]) {
      await assert.rejects(prepare(delegate.value, {...prepareRequest(sourceAddress), sourceAddress}), /camera-stream-unavailable/);
    }
    assert.equal(peer.sent.filter(item => item.kind === "video_prepare").length, 0);
  } finally {
    await delegate.value.close(); peer.session.close();
  }
});

test("prepare diagnostics emit only fixed protocol codes and cannot change callback behavior", async context => {
  const peer = new Peer(); await peer.ready();
  const reported: [string, string][] = [];
  const delegate = new HapVideoDelegate(peer.session.camera("front"), {active: () => true,
    forceStop: () => {}, failure: () => {}, reportError: (stage, code) => {reported.push([stage, code]); throw Error("callback ignored");}});
  try {
    await assert.rejects(prepare(delegate, {...prepareRequest(), targetAddress: "239.1.1.1"}), /camera-stream-unavailable/);
    context.mock.method(peer.session, "prepareVideo", async () => {throw new ProtocolError("untrusted secret 192.0.2.4 key=private");});
    await assert.rejects(prepare(delegate), /camera-stream-unavailable/);
    assert.deepEqual(reported, [["video-prepare", "invalid-video-prepare"], ["video-prepare", "operation-failed"]]);
    assert.equal(JSON.stringify(reported).includes("private"), false);
  } finally {
    await delegate.close(); peer.session.close();
  }
});
