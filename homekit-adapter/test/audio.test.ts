import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { test } from "node:test";
import {
  AudioBitrate, AudioStreamingCodecType, Characteristic, H264Level, H264Profile,
  Service, SRTPCryptoSuites, StreamRequestTypes, VideoCodecPacketizationMode, VideoCodecType,
  type PrepareStreamRequest, type PrepareStreamResponse, type StartStreamRequest, type StreamingRequest,
} from "@homebridge/hap-nodejs";
import { AudioControls, AUDIO_SETTINGS_TIMEOUT_MS } from "../src/audio.js";
import { AccessoryManager } from "../src/accessories.js";
import { CameraAccessory } from "../src/camera.js";
import type { PairingStore } from "../src/storage.js";
import { AdapterSession, type NativeCamera } from "../src/session.js";
import { validateReply, type AccessoryAudio, type AudioConfiguration, type AudioSettings, type Envelope, type JsonObject, type MediaScope } from "../src/protocol.js";

const boot = "00000000-0000-0000-0000-000000000001";
const account = {profile_id: "00000000-0000-0000-0000-000000000002", account_generation: 1};
const tick = (): Promise<void> => new Promise(resolve => setImmediate(resolve));
const initial = (): AccessoryAudio => ({microphone: true, speaker: true, microphone_muted: false, speaker_muted: false,
  microphone_volume: 100, speaker_volume: 100, revision: 7});
class AudioPeer {
  sent: Envelope[] = [];
  held: Envelope[] = [];
  heldPolicy: Envelope[] = [];
  holdSettings = false;
  holdPolicy = false;
  rejectSettings = false;
  mismatchEndpoint = false;
  onInventory: (() => void) | undefined;
  settings = initial();
  inventoryRevision = 1;
  current: NativeCamera = {scope: {...account, camera_id: "front", device_generation: 1, privacy_generation: 1},
    name: "Fixture", family: "fixture", kind: "fixture", model: "Fixture", capabilities: {video: true, audio: true,
      snapshot: false, talkback: true, light: false, siren: false, doorbell: false, battery: false,
      evidence: ["halo:video", "halo:audio", "halo:talkback"]}, connection_state: null, battery_percent: null, observed_format: null};
  session = new AdapterSession(boot, {send: async message => {
    this.sent.push(message);
    if (message.kind === "audio_settings_update" && this.holdSettings) this.held.push(message);
    else if (message.kind === "policy_update" && this.holdPolicy) this.heldPolicy.push(message);
    else queueMicrotask(() => { void this.answer(message); });
  }, onInventory: () => this.onInventory?.()});
  async ready(): Promise<CameraAccessory> {
    await this.session.receive({v: 1, kind: "inventory_reset", boot_id: boot, request_id: randomUUID(), payload: {scope: account, revision: 1}});
    await tick(); assert.equal(this.session.inventoryComplete, true);
    return new CameraAccessory({camera_id: "front", accessory_id: "00000000-0000-0000-0000-000000000003",
      username: "02:11:22:33:44:55", doorbell: false, audio: this.settings}, this.current, {native: this.session.camera("front")});
  }
  async answer(request: Envelope): Promise<void> {
    const reply = (kind: Envelope["kind"], payload: JsonObject) => this.session.receive({...request, kind, payload});
    if (request.kind === "inventory_request") await reply("inventory_page", {scope: account, revision: this.inventoryRevision,
      cursor: null, next_cursor: null, total: 1, complete: true, devices: [this.current]});
    else if (request.kind === "policy_update") {
      this.current.scope.privacy_generation++; this.inventoryRevision++;
      await reply("policy_ack", {scope: request.payload.scope, accepted: true, code: "ok"});
    } else if (request.kind === "audio_settings_update") {
      this.current.scope.privacy_generation++; this.inventoryRevision++;
      if (!this.rejectSettings) this.settings = {...this.settings, ...request.payload.settings as AudioSettings, revision: this.settings.revision + 1};
      await reply("audio_settings_result", {scope: request.payload.scope, accepted: !this.rejectSettings,
        code: this.rejectSettings ? "conflict" : "ok", revision: this.settings.revision});
    } else if (request.kind === "video_prepare") {
      const audio = request.payload.audio as {key: string; salt: string} | null;
      await reply("video_prepare_result", {scope: request.payload.scope, session_id: request.payload.session_id, status: "accepted", code: "ok",
        endpoint: {address: request.payload.source_address, port: 50000, ssrc: 1234, key: request.payload.key, salt: request.payload.salt,
          audio: audio === null ? null : {port: 50002, ssrc: 5678, key: this.mismatchEndpoint ? Buffer.alloc(16, 15).toString("base64") : audio.key, salt: audio.salt}}});
    } else if (["video_start", "video_reconfigure", "video_stop"].includes(request.kind)) {
      await reply(`${request.kind}_result` as Envelope["kind"], {scope: request.payload.scope,
        session_id: request.payload.session_id, status: "accepted", code: "ok"});
    }
  }
  async close(accessory: CameraAccessory): Promise<void> { accessory.retire(); await accessory.closeStreams(); this.session.close(); }
}
const request = (sessionID = "HAP audio fixture"): PrepareStreamRequest => ({sessionID, sourceAddress: "127.0.0.1", targetAddress: "127.0.0.1", addressVersion: "ipv4",
  video: {port: 50100, srtpCryptoSuite: SRTPCryptoSuites.AES_CM_128_HMAC_SHA1_80, srtp_key: Buffer.alloc(16, 7), srtp_salt: Buffer.alloc(14, 9)},
  audio: {port: 50102, srtpCryptoSuite: SRTPCryptoSuites.AES_CM_128_HMAC_SHA1_80, srtp_key: Buffer.alloc(16, 8), srtp_salt: Buffer.alloc(14, 10)}});
const prepare = (accessory: CameraAccessory, value = request()): Promise<PrepareStreamResponse> => new Promise((resolve, reject) =>
  accessory.streamingDelegate!.prepareStream(value, (error, response) => error ? reject(error) : resolve(response!)));
const handle = (accessory: CameraAccessory, value: StreamingRequest): Promise<void> => new Promise((resolve, reject) =>
  accessory.streamingDelegate!.handleStreamRequest(value, error => error ? reject(error) : resolve()));
const start = (sessionID = "HAP audio fixture"): StartStreamRequest => ({sessionID, type: StreamRequestTypes.START,
  video: {codec: VideoCodecType.H264, profile: H264Profile.BASELINE, level: H264Level.LEVEL3_1, packetizationMode: VideoCodecPacketizationMode.NON_INTERLEAVED,
    width: 320, height: 240, fps: 15, pt: 99, ssrc: 1234, max_bit_rate: 500, rtcp_interval: 0.5, mtu: 1200},
  audio: {codec: AudioStreamingCodecType.OPUS, channel: 1, bit_rate: AudioBitrate.VARIABLE, sample_rate: 24,
    packet_time: 20, pt: 110, ssrc: 5678, max_bit_rate: 24, rtcp_interval: 0.5, comfort_pt: 13, comfortNoiseEnabled: false}});
const write = (accessory: CameraAccessory, type: typeof Service.Microphone | typeof Service.Speaker,
  characteristic: typeof Characteristic.Mute | typeof Characteristic.Volume, value: boolean | number): Promise<void> => new Promise((resolve, reject) =>
    accessory.accessory.getService(type)!.getCharacteristic(characteristic).setValue(value, (error: unknown) => error ? reject(error) : resolve()));

test("durable mute is applied before policy synchronization and the born audio graph survives capability drift", async () => {
  const peer = new AudioPeer(); peer.settings.microphone_muted = true; peer.settings.speaker_muted = true;
  const accessory = await peer.ready();
  assert.equal(accessory.accessory.getService(Service.Microphone)!.getCharacteristic(Characteristic.Mute).value, true);
  assert.equal(accessory.accessory.getService(Service.Speaker)!.getCharacteristic(Characteristic.Mute).value, true);
  await accessory.synchronizePolicy();
  const policy = peer.sent.find(message => message.kind === "policy_update")!.payload.policy as Record<string, boolean>;
  assert.equal(policy.streaming, true); assert.equal(policy.live_audio, false); assert.equal(policy.talkback, false);
  peer.current.capabilities.audio = false; peer.current.capabilities.talkback = false;
  accessory.update(peer.current); await accessory.synchronizePolicy();
  assert.ok(accessory.accessory.getService(Service.Microphone)); assert.ok(accessory.accessory.getService(Service.Speaker));
  assert.equal(accessory.audio!.microphone_muted, true);
  await peer.close(accessory);
});

test("the real HAP delegate sends separate audio SRTP endpoints and every negotiated classic audio format", async () => {
  const peer = new AudioPeer(), accessory = await peer.ready();
  for (const rate of [8,16,24] as const) for (const duration of [20,30,40,60]) for (const bitrate of [AudioBitrate.VARIABLE,AudioBitrate.CONSTANT]) {
    const id = `${rate}/${duration}/${bitrate}`, response = await prepare(accessory, request(id));
    assert.equal((response.audio as {port: number}).port, 50002);
    assert.deepEqual((response.audio as {srtp_key: Buffer}).srtp_key, Buffer.alloc(16, 8));
    const selected = start(id); selected.audio.sample_rate = rate; selected.audio.packet_time = duration; selected.audio.bit_rate = bitrate;
    await handle(accessory, selected);
    const config = peer.sent.filter(message => message.kind === "video_start").at(-1)!.payload.audio_config as AudioConfiguration;
    assert.deepEqual(config, {sample_rate: rate * 1000, packet_time: duration, bitrate_mode: bitrate === AudioBitrate.CONSTANT ? "cbr" : "vbr",
      max_bit_rate: 24, payload_type: 110, rtcp_interval: 0.5, channels: 1, clock_mode: "classic_hap"});
    await handle(accessory, {sessionID: id, type: StreamRequestTypes.RECONFIGURE, video: {width: 640, height: 480, fps: 30, max_bit_rate: 1000, rtcp_interval: 1}});
    assert.equal(Object.hasOwn(peer.sent.at(-1)!.payload, "audio_config"), false);
    await handle(accessory, {sessionID: id, type: StreamRequestTypes.STOP});
  }
  assert.equal(peer.sent.filter(message => message.kind === "policy_update").length, 1);
  assert.equal(peer.sent.some(message => message.kind === "talkback_request"), false);
  await peer.close(accessory);
});

test("cold unknown source audio does not prevent a saved microphone graph from negotiating its first validated packets", async () => {
  const peer = new AudioPeer(); peer.current.capabilities.audio = false; peer.current.capabilities.evidence = ["halo:video", "halo:talkback"];
  const accessory = await peer.ready(); await prepare(accessory); await handle(accessory, start());
  const policy = peer.sent.find(message => message.kind === "policy_update")!.payload.policy as Record<string, boolean>;
  assert.equal(policy.live_audio, true); assert.equal(peer.current.capabilities.audio, false); assert.equal(peer.current.observed_format, null);
  await peer.close(accessory);
});

test("zero microphone and speaker volume independently revoke their delivery permissions", async () => {
  const peer = new AudioPeer(), accessory = await peer.ready(); await accessory.synchronizePolicy();
  await write(accessory, Service.Microphone, Characteristic.Volume, 0);
  let policy = peer.sent.filter(message => message.kind === "policy_update").at(-1)!.payload.policy as Record<string, boolean>;
  assert.equal(policy.streaming, true); assert.equal(policy.live_audio, false); assert.equal(policy.talkback, true);
  await write(accessory, Service.Speaker, Characteristic.Volume, 0);
  policy = peer.sent.filter(message => message.kind === "policy_update").at(-1)!.payload.policy as Record<string, boolean>;
  assert.equal(policy.streaming, true); assert.equal(policy.live_audio, false); assert.equal(policy.talkback, false);
  await peer.close(accessory);
});

test("unadvertised audio codec, stereo, comfort noise, SSRC and packet durations cannot emit START", async () => {
  const peer = new AudioPeer(), accessory = await peer.ready();
  const mutations: ((value: StartStreamRequest) => void)[] = [value => { value.audio.channel = 2; },
    value => { value.audio.codec = AudioStreamingCodecType.AAC_ELD; }, value => { value.audio.comfortNoiseEnabled = true; },
    value => { value.audio.ssrc = 4321; }, value => { value.audio.packet_time = 10; }, value => { value.audio.bit_rate = 3; },
    value => { value.audio.sample_rate = 48 as StartStreamRequest["audio"]["sample_rate"]; }];
  for (const [index, mutate] of mutations.entries()) {
    const id = `invalid-${index}`; await prepare(accessory, request(id)); const selected = start(id); mutate(selected);
    await assert.rejects(handle(accessory, selected), /camera-stream-unavailable/);
    await handle(accessory, {sessionID: id, type: StreamRequestTypes.STOP});
  }
  assert.equal(peer.sent.some(message => message.kind === "video_start"), false);
  const malformed = request(); malformed.audio.srtp_key = Buffer.alloc(15);
  const before = peer.sent.filter(message => message.kind === "video_prepare").length;
  await assert.rejects(prepare(accessory, malformed));
  assert.equal(peer.sent.filter(message => message.kind === "video_prepare").length, before);
  assert.equal(accessory.streamingDelegate!.count, 0);
  await peer.close(accessory);
});

test("a microphone mute fences immediately and its public write callback waits for durable acknowledgement", async () => {
  const peer = new AudioPeer(), accessory = await peer.ready(); await prepare(accessory);
  peer.holdSettings = true; let complete = false;
  const muting = write(accessory, Service.Microphone, Characteristic.Mute, true).then(() => { complete = true; });
  await tick(); assert.equal(complete, false);
  assert.equal(peer.sent.filter(message => message.kind === "video_stop").length, 1);
  await assert.rejects(prepare(accessory), /camera-stream-unavailable/);
  assert.equal(accessory.accessory.getService(Service.Microphone)!.getCharacteristic(Characteristic.Mute).value, false);
  await peer.answer(peer.held.shift()!); await muting;
  assert.equal(complete, true); assert.equal(accessory.audio!.microphone_muted, true);
  const policy = peer.sent.filter(message => message.kind === "policy_update").at(-1)!.payload.policy as Record<string, boolean>;
  assert.equal(policy.live_audio, false); assert.equal(policy.talkback, true);
  assert.equal((peer.sent.find(message => message.kind === "audio_settings_update")!.payload.scope as MediaScope).privacy_generation, 2);
  await peer.close(accessory);
});

test("volume writes persist the full independent settings and retire existing views", async () => {
  const peer = new AudioPeer(), accessory = await peer.ready(); await prepare(accessory);
  await write(accessory, Service.Speaker, Characteristic.Volume, 37);
  const sent = peer.sent.find(message => message.kind === "audio_settings_update")!;
  assert.deepEqual(sent.payload.settings, {microphone_muted: false, speaker_muted: false, microphone_volume: 100, speaker_volume: 37});
  assert.equal(accessory.audio!.speaker_volume, 37); assert.equal(accessory.audio!.microphone_volume, 100);
  assert.equal(peer.sent.filter(message => message.kind === "video_stop").length, 1);
  await peer.close(accessory);
});

test("one public Home multi-write coalesces four independent audio fields into one durable CAS", async () => {
  const peer = new AudioPeer(), accessory = await peer.ready(); await accessory.synchronizePolicy();
  peer.holdSettings = true; let callbacks = 0;
  const writes = [write(accessory, Service.Microphone, Characteristic.Mute, true),
    write(accessory, Service.Speaker, Characteristic.Mute, true),
    write(accessory, Service.Microphone, Characteristic.Volume, 17),
    write(accessory, Service.Speaker, Characteristic.Volume, 37)].map(value => value.then(() => {callbacks++;}));
  assert.equal(accessory.policy.streaming, false);
  await tick(); assert.equal(callbacks, 0); assert.equal(peer.held.length, 1);
  assert.deepEqual(peer.held[0]!.payload.settings,
    {microphone_muted: true, speaker_muted: true, microphone_volume: 17, speaker_volume: 37});
  await peer.answer(peer.held.shift()!); await Promise.all(writes);
  assert.equal(callbacks, 4); assert.equal(peer.sent.filter(value => value.kind === "audio_settings_update").length, 1);
  assert.equal(accessory.audio!.microphone_muted, true); assert.equal(accessory.audio!.speaker_muted, true);
  assert.equal(accessory.audio!.microphone_volume, 17); assert.equal(accessory.audio!.speaker_volume, 37);
  await peer.close(accessory);
});

test("writes during persistence retain only the latest intent and every callback waits for its final revision", async () => {
  const peer = new AudioPeer(), accessory = await peer.ready(); await accessory.synchronizePolicy();
  peer.holdSettings = true; let callbacks = 0;
  const writes = [write(accessory, Service.Microphone, Characteristic.Mute, true).then(() => {callbacks++;})];
  await tick(); const first = peer.held.shift()!;
  writes.push(write(accessory, Service.Speaker, Characteristic.Volume, 37).then(() => {callbacks++;}));
  await peer.answer(first); await tick();
  assert.equal(callbacks, 0); assert.equal(peer.held.length, 1);
  assert.equal(peer.held[0]!.payload.expected_revision, 8);
  writes.push(write(accessory, Service.Microphone, Characteristic.Mute, false).then(() => {callbacks++;}));
  writes.push(write(accessory, Service.Microphone, Characteristic.Mute, true).then(() => {callbacks++;}));
  assert.equal(accessory.policy.streaming, false);
  await peer.answer(peer.held.shift()!); await Promise.all(writes);
  assert.equal(callbacks, 4); assert.equal(accessory.audio!.revision, 9);
  assert.equal(accessory.audio!.microphone_muted, true); assert.equal(accessory.audio!.speaker_volume, 37);
  assert.equal(peer.sent.filter(value => value.kind === "audio_settings_update").length, 2);
  await peer.close(accessory);
});

test("a mute superseding an in-flight unmute stays fenced until the final revocation is durable", async () => {
  const peer = new AudioPeer(); peer.settings.microphone_muted = true;
  const accessory = await peer.ready(); await accessory.synchronizePolicy(); peer.holdSettings = true;
  let callbacks = 0;
  const grant = write(accessory, Service.Microphone, Characteristic.Mute, false).then(() => {callbacks++;});
  await tick(); const first = peer.held.shift()!;
  const revoke = write(accessory, Service.Microphone, Characteristic.Mute, true).then(() => {callbacks++;});
  await peer.answer(first); await tick();
  assert.equal(callbacks, 0); assert.equal(accessory.policy.streaming, false);
  assert.equal(peer.held.length, 1);
  assert.equal((peer.held[0]!.payload.settings as AudioSettings).microphone_muted, true);
  await peer.answer(peer.held.shift()!); await Promise.all([grant, revoke]);
  assert.equal(callbacks, 2); assert.equal(accessory.audio!.microphone_muted, true);
  const policy = peer.sent.filter(value => value.kind === "policy_update").at(-1)!.payload.policy as Record<string, boolean>;
  assert.equal(policy.live_audio, false);
  await peer.close(accessory);
});

test("a disconnected public multi-write fails every callback without replaying a partially saved intent", async () => {
  const peer = new AudioPeer(), accessory = await peer.ready(); await accessory.synchronizePolicy(); peer.holdSettings = true;
  let callbacks = 0;
  const writes = [write(accessory, Service.Microphone, Characteristic.Mute, true),
    write(accessory, Service.Speaker, Characteristic.Mute, true)].map(value => value.then(
      () => {throw new Error("a disconnected write must not succeed");}, () => {callbacks++;}));
  await tick(); peer.session.close(); await Promise.all(writes);
  assert.equal(callbacks, 2); assert.equal(accessory.policy.streaming, false);
  assert.equal(accessory.audio!.microphone_muted, false); assert.equal(accessory.audio!.speaker_muted, false);
  assert.equal(peer.sent.filter(value => value.kind === "audio_settings_update").length, 1);
  accessory.retire(); await accessory.closeStreams();
});

test("a new mute during policy synchronization fences immediately and cannot receive an early successful callback", async () => {
  const peer = new AudioPeer(), accessory = await peer.ready(); await accessory.synchronizePolicy();
  peer.holdPolicy = true; let callbacks = 0;
  const first = write(accessory, Service.Microphone, Characteristic.Mute, true).then(() => {callbacks++;});
  await tick(); assert.equal(peer.heldPolicy.length, 1);
  const second = write(accessory, Service.Speaker, Characteristic.Mute, true).then(() => {callbacks++;});
  assert.equal(accessory.policy.streaming, false); assert.equal(callbacks, 0);
  await peer.answer(peer.heldPolicy.shift()!); await tick();
  assert.equal(callbacks, 0); assert.equal(peer.heldPolicy.length, 1);
  // The old policy synchronizer first observes the new synchronous deny.
  // Only after that fence does the writer install the second durable intent.
  await peer.answer(peer.heldPolicy.shift()!); await tick();
  assert.equal(callbacks, 0); assert.equal(peer.heldPolicy.length, 1);
  await peer.answer(peer.heldPolicy.shift()!); await Promise.all([first, second]);
  assert.equal(callbacks, 2); assert.equal(accessory.audio!.speaker_muted, true);
  const policy = peer.sent.filter(value => value.kind === "policy_update").at(-1)!.payload.policy as Record<string, boolean>;
  assert.equal(policy.live_audio, false); assert.equal(policy.talkback, false);
  await peer.close(accessory);
});

test("a burst of public writes uses one persistence slot and one final audio snapshot", async () => {
  const peer = new AudioPeer(), accessory = await peer.ready(); await accessory.synchronizePolicy(); peer.holdSettings = true;
  const writes = Array.from({length: 1000}, (_, index) => write(accessory, Service.Speaker, Characteristic.Volume, index % 101));
  await tick(); assert.equal(peer.held.length, 1);
  assert.equal((peer.held[0]!.payload.settings as AudioSettings).speaker_volume, 999 % 101);
  await peer.answer(peer.held.shift()!); await Promise.all(writes);
  assert.equal(peer.sent.filter(value => value.kind === "audio_settings_update").length, 1);
  assert.equal(accessory.audio!.speaker_volume, 999 % 101);
  await peer.close(accessory);
});

test("timed-out owned persistence prevents a second task and late completion cannot reopen audio", async context => {
  context.mock.timers.enable({apis: ["setTimeout"]});
  const peer = new AudioPeer(), accessory = await peer.ready();
  let release!: (revision: number) => void, persists = 0;
  const diagnostics: [string, string][] = [];
  const controls = new AudioControls(accessory.accessory, accessory.controller, initial(), {fence: async () => {},
    persist: async () => {persists++; return new Promise<number>(resolve => {release = resolve;});},
    synchronize: async () => {}, reportError: (stage, code) => diagnostics.push([stage, code])});
  const first = write(accessory, Service.Microphone, Characteristic.Mute, true), rejected = assert.rejects(first);
  await tick(); context.mock.timers.tick(AUDIO_SETTINGS_TIMEOUT_MS); await rejected;
  await assert.rejects(write(accessory, Service.Speaker, Characteristic.Mute, true));
  assert.equal(persists, 1); assert.equal(controls.available, false);
  release(8); await tick();
  assert.equal(controls.available, false); assert.equal(controls.current.microphone_muted, false);
  assert.deepEqual(diagnostics, [["audio-settings", "timeout"]]);
  controls.retire(); await peer.close(accessory);
});

test("even a cleanup rejection without an Error payload cannot grant a successful audio write", async () => {
  const peer = new AudioPeer(), accessory = await peer.ready();
  let policies = 0;
  const diagnostics: [string, string][] = [];
  const controls = new AudioControls(accessory.accessory, accessory.controller, initial(), {
    fence: async () => {throw null;}, persist: async () => 8,
    synchronize: async () => {policies++;}, reportError: (stage, code) => diagnostics.push([stage, code])});
  await assert.rejects(write(accessory, Service.Microphone, Characteristic.Mute, true));
  assert.equal(controls.available, false); assert.equal(policies, 0);
  assert.deepEqual(diagnostics, [["audio-settings", "cleanup-failed"]]);
  controls.retire(); await peer.close(accessory);
});

test("a durable config received before the settings result cannot reopen pending admission", async () => {
  const peer = new AudioPeer(), accessory = await peer.ready(); await accessory.synchronizePolicy(); peer.holdSettings = true;
  const muting = write(accessory, Service.Speaker, Characteristic.Mute, true);
  await tick(); await accessory.updateAudio({...initial(), speaker_muted: true, revision: 8});
  await assert.rejects(prepare(accessory));
  await peer.answer(peer.held.shift()!); await muting;
  assert.equal(accessory.audio!.speaker_muted, true);
  const policy = peer.sent.filter(message => message.kind === "policy_update").at(-1)!.payload.policy as Record<string, boolean>;
  assert.equal(policy.live_audio, true); assert.equal(policy.talkback, false);
  await peer.close(accessory);
});

test("an external durable mute during policy synchronization conflicts instead of rebasing stale unmute fields", async () => {
  const peer = new AudioPeer(), accessory = await peer.ready(); await accessory.synchronizePolicy(); peer.holdPolicy = true;
  const muting = write(accessory, Service.Microphone, Characteristic.Mute, true), rejected = assert.rejects(muting);
  await tick(); assert.equal(peer.heldPolicy.length, 1);
  peer.settings = {...peer.settings, speaker_muted: true, revision: 9};
  await accessory.updateAudio(peer.settings); await rejected;
  assert.equal(accessory.policy.streaming, false);
  for (let index = 0; index < 4; index++) {
    await tick(); const request = peer.heldPolicy.shift(); if (request) await peer.answer(request);
  }
  assert.equal(peer.sent.filter(value => value.kind === "audio_settings_update").length, 1);
  assert.equal(accessory.audio!.microphone_muted, true); assert.equal(accessory.audio!.speaker_muted, true);
  assert.equal(accessory.audio!.revision, 9);
  await peer.close(accessory);
});

test("a newer page with identical settings preserves a saved write without replaying it", async () => {
  const peer = new AudioPeer(), accessory = await peer.ready(); await accessory.synchronizePolicy(); peer.holdPolicy = true;
  const muting = write(accessory, Service.Microphone, Characteristic.Mute, true);
  await tick(); assert.equal(peer.heldPolicy.length, 1);
  peer.settings = {...peer.settings, revision: 9};
  await accessory.updateAudio(peer.settings);
  await peer.answer(peer.heldPolicy.shift()!); await muting;
  assert.equal(accessory.audio!.revision, 9); assert.equal(accessory.audio!.microphone_muted, true);
  assert.equal(peer.sent.filter(value => value.kind === "audio_settings_update").length, 1);
  await peer.close(accessory);
});

test("a changed external page cannot masquerade as the pending write's own CAS page", async () => {
  const peer = new AudioPeer(), accessory = await peer.ready(); await accessory.synchronizePolicy(); peer.holdSettings = true;
  const muting = write(accessory, Service.Microphone, Characteristic.Mute, true), rejected = assert.rejects(muting);
  await tick();
  await accessory.updateAudio({...initial(), speaker_muted: true, revision: 8}); await rejected;
  assert.equal(accessory.policy.streaming, false);
  await peer.answer(peer.held.shift()!); await tick();
  assert.equal(peer.sent.filter(value => value.kind === "audio_settings_update").length, 1);
  assert.equal(accessory.audio!.microphone_muted, false); assert.equal(accessory.audio!.speaker_muted, true);
  await peer.close(accessory);
});

test("a rejected settings CAS keeps admission closed until a fresh durable config is restored", async () => {
  const peer = new AudioPeer(), accessory = await peer.ready(); await accessory.synchronizePolicy(); peer.rejectSettings = true;
  await assert.rejects(write(accessory, Service.Microphone, Characteristic.Mute, true));
  await assert.rejects(prepare(accessory)); assert.equal(accessory.audio!.microphone_muted, false);
  await accessory.updateAudio(peer.settings); await accessory.synchronizePolicy();
  await prepare(accessory); await peer.close(accessory);
});

test("a stalled settings write has a finite HAP deadline and a late result cannot unmute a retired graph", async context => {
  context.mock.timers.enable({apis: ["setTimeout"]});
  const peer = new AudioPeer(), accessory = await peer.ready(); await accessory.synchronizePolicy(); peer.holdSettings = true;
  const pending = write(accessory, Service.Microphone, Characteristic.Mute, true), rejected = assert.rejects(pending);
  await tick(); context.mock.timers.tick(AUDIO_SETTINGS_TIMEOUT_MS); await rejected;
  await assert.rejects(prepare(accessory));
  accessory.retire(); await peer.answer(peer.held.shift()!); await tick();
  assert.equal(accessory.audio!.microphone_muted, false);
  await peer.close(accessory);
});

test("a mismatched audio endpoint is compensated through the common STOP", async () => {
  const peer = new AudioPeer(), accessory = await peer.ready(); peer.mismatchEndpoint = true;
  await assert.rejects(prepare(accessory));
  assert.equal(peer.sent.filter(message => message.kind === "video_stop").length, 1);
  assert.equal(accessory.streamingDelegate!.count, 0); await peer.close(accessory);
});

test("accessory retirement cancels a pending public settings write without waiting for its deadline", async () => {
  const peer = new AudioPeer(), accessory = await peer.ready(); await accessory.synchronizePolicy(); peer.holdSettings = true;
  const pending = write(accessory, Service.Microphone, Characteristic.Mute, true), rejected = assert.rejects(pending);
  await tick(); accessory.retire(); await accessory.closeStreams(); await rejected;
  assert.equal(accessory.audio!.microphone_muted, false);
  peer.session.close(); assert.equal(peer.session.pendingRequests, 0);
});

test("paged graph reconciliation and inventory callbacks preserve the audio object during a pending durable write", async () => {
  const peer = new AudioPeer(); const unused = await peer.ready(); unused.retire(); await unused.closeStreams();
  let published = 0, unpublished = 0;
  const failures: string[] = [];
  const manager = new AccessoryManager({session: peer.session,
    store: {preflight: () => {}, close: () => {}} as unknown as PairingStore,
    publisher: {mediaReady: true, publish: async camera => {
      assert.equal(camera.accessory.getService(Service.Microphone)!.getCharacteristic(Characteristic.Mute).value, false); published++;
    }, unpublish: async () => { unpublished++; }},
    syncEvents: async () => ({scope: account, epoch: "audio-fixture", sequence: 0, accepted: true, code: "ok"}),
    onFailure: error => failures.push(error.code)});
  peer.onInventory = () => { void manager.reconcile().catch(error => failures.push(String(error))); };
  const identity = {camera_id: "front", accessory_id: "00000000-0000-0000-0000-000000000003", username: "02:11:22:33:44:55", doorbell: false};
  const config = {scope: account, revision: 1, generation: 1, page_index: 0, total_pages: 1,
    entries: [{...identity, audio: initial()}], media_ready: true};
  await manager.configure(config); const accessory = manager.accessories()[0]!; await prepare(accessory);
  peer.holdSettings = true;
  const muting = write(accessory, Service.Microphone, Characteristic.Mute, true); await tick();
  const configuring = manager.configure({...config, generation: 2,
    entries: [{...identity, audio: {...initial(), microphone_muted: true, revision: 8}}]});
  await tick(); await assert.rejects(prepare(accessory));
  await peer.answer(peer.held.shift()!); await muting; await configuring; await manager.reconcile();
  assert.equal(manager.accessories()[0], accessory); assert.equal(published, 1); assert.equal(unpublished, 0);
  assert.equal(accessory.audio!.microphone_muted, true); assert.deepEqual(failures, []);
  await manager.close(); peer.session.close(); assert.equal(unpublished, 1);
});

test("audio settings acknowledgement is correlated to exact scope and its bounded CAS revision", () => {
  const request: Envelope = {v: 1, kind: "audio_settings_update", boot_id: boot, request_id: randomUUID(),
    payload: {scope: {...account, camera_id: "front", device_generation: 1, privacy_generation: 1}, expected_revision: 7,
      settings: {microphone_muted: true, speaker_muted: false, microphone_volume: 100, speaker_volume: 100}}};
  const reply: Envelope = {...request, kind: "audio_settings_result", payload: {scope: request.payload.scope, accepted: true, code: "ok", revision: 8}};
  validateReply(request, reply); reply.payload.revision = 7; validateReply(request, reply);
  reply.payload.revision = 9; assert.throws(() => validateReply(request, reply));
  reply.payload.revision = 8; reply.payload.scope = {...request.payload.scope as MediaScope, privacy_generation: 2};
  assert.throws(() => validateReply(request, reply));
});
