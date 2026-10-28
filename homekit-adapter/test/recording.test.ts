import assert from "node:assert/strict";
import { test } from "node:test";
import {
  AudioBitrate, AudioRecordingCodecType, AudioRecordingSamplerate, H264Level, H264Profile,
  MediaContainerType, VideoCodecType, type CameraRecordingConfiguration,
} from "@homebridge/hap-nodejs";
import { HapRecordingDelegate, RECORDING_SETTINGS_TIMEOUT_MS, selectedRecordingConfiguration } from "../src/recording.js";
import type { CameraDelegate, NativeRecording } from "../src/session.js";
import type { AccessoryRecording, CameraPolicy, RecordingConfiguration } from "../src/protocol.js";

const tick = (): Promise<void> => new Promise(resolve => setImmediate(resolve));
const configuration = (): CameraRecordingConfiguration => ({prebufferLength: 4000, eventTriggerTypes: [1],
  mediaContainerConfiguration: {type: MediaContainerType.FRAGMENTED_MP4, fragmentLength: 4000},
  videoCodec: {type: VideoCodecType.H264, parameters: {profile: H264Profile.MAIN, level: H264Level.LEVEL3_1,
    bitRate: 1000, iFrameInterval: 1000}, resolution: [1280, 720, 15]},
  audioCodec: {type: AudioRecordingCodecType.AAC_LC, audioChannels: 1, bitrateMode: AudioBitrate.VARIABLE,
    samplerate: AudioRecordingSamplerate.KHZ_24, bitrate: 32}});
type Packet = NonNullable<Awaited<ReturnType<NativeRecording["next"]>>>;
class Source {
  packets: (Packet | null)[] = [
    {kind: "init", sequence: 0, data: Buffer.from("init"), durationMs: 0},
    {kind: "fragment", sequence: 1, data: Buffer.from("first"), durationMs: 2000},
    {kind: "fragment", sequence: 2, data: Buffer.from("tail"), durationMs: 500}, null,
  ];
  current = true;
  pulls = 0;
  stops = 0;
  hold = false;
  rejectStop = false;
  isCurrent(): boolean { return this.current; }
  async next(signal?: AbortSignal): Promise<Packet | null> {
    this.pulls++;
    if (signal?.aborted) throw new Error("aborted");
    if (this.hold) await new Promise<never>((_resolve, reject) => {
      signal!.addEventListener("abort", () => reject(new Error("aborted")), {once: true});
    });
    return this.packets.shift() ?? null;
  }
  async stop(): Promise<void> { this.stops++; if (this.rejectStop) throw new Error("retained-cleanup"); this.current = false; }
}
function harness() {
  const saved: AccessoryRecording = {revision: 0, enabled: true, active: false, audio_active: false, configuration: null};
  const updates: {expected: number; active: boolean; audio: boolean; config: RecordingConfiguration | null}[] = [];
  const policies: CameraPolicy[] = [], opened: RecordingConfiguration[] = [], sources: Source[] = [];
  let active = true, revision = 0, fail = 0, transportCloses = 0;
  let holdWrite: Promise<void> | null = null, holdOpen: Promise<void> | null = null, holdPolicy: Promise<void> | null = null;
  let nextSource = new Source();
  let delegate: HapRecordingDelegate;
  const policy = (): CameraPolicy => ({synchronized: true, streaming: active, live_audio: false, snapshots: active,
    recording: delegate.permissions.recording, recording_audio: delegate.permissions.recording_audio, talkback: false, controls: false});
  const native = {
    ensurePolicy: async (value: CameraPolicy) => { policies.push(structuredClone(value)); if (holdPolicy) await holdPolicy; },
    updateRecordingSettings: async (expected: number, enabled: boolean, audio: boolean, config: RecordingConfiguration | null) => {
      updates.push({expected, active: enabled, audio, config: structuredClone(config)});
      if (holdWrite) await holdWrite;
      assert.equal(expected, revision); return ++revision;
    },
    openRecording: async (config: RecordingConfiguration, signal: AbortSignal) => {
      opened.push(structuredClone(config)); const source = nextSource; sources.push(source);
      if (holdOpen) await holdOpen;
      if (signal.aborted) { await source.stop(); throw new Error("aborted"); }
      return source as unknown as NativeRecording;
    },
  } as unknown as CameraDelegate;
  delegate = new HapRecordingDelegate(native, saved, {active: () => active, policy,
    closeTransport: async () => { transportCloses++; }, failure: () => { fail++; }});
  const activate = async (audio = true) => {
    delegate.updateRecordingConfiguration(configuration()); delegate.updateRecordingActive(true);
    await delegate.setAudioActive(audio);
  };
  return {delegate, updates, policies, opened, sources, activate,
    get failures() { return fail; }, get transportCloses() { return transportCloses; },
    get source() { return nextSource; }, set source(value: Source) { nextSource = value; },
    setActive(value: boolean) { active = value; }, setHoldWrite(value: Promise<void> | null) { holdWrite = value; },
    setHoldOpen(value: Promise<void> | null) { holdOpen = value; },
    setHoldPolicy(value: Promise<void> | null) { holdPolicy = value; }};
}

test("selected HKSV format uses recording enums and exactly one kbps-to-bps conversion", () => {
  const wire = selectedRecordingConfiguration(configuration());
  assert.deepEqual(wire, {video: {width: 1280, height: 720, fps: 15, profile: "main", level: "3.1", max_bit_rate: 1000},
    audio: {sample_rate: 24000, bit_rate: 32000, channels: 1, bitrate_mode: "vbr"},
    fragment_length_ms: 4000, i_frame_interval_ms: 1000});
  for (const [key, value] of [["prebufferLength", 0]] as const) {
    assert.throws(() => selectedRecordingConfiguration({...configuration(), [key]: value}), /unsupported-recording-format/);
  }
  for (const mutate of [
    (value: CameraRecordingConfiguration) => { value.videoCodec.resolution = [1920, 1080, 30]; },
    (value: CameraRecordingConfiguration) => { value.videoCodec.parameters.iFrameInterval = 1; },
    (value: CameraRecordingConfiguration) => { value.audioCodec.type = AudioRecordingCodecType.AAC_ELD; },
    (value: CameraRecordingConfiguration) => { value.audioCodec.bitrateMode = AudioBitrate.CONSTANT; },
    (value: CameraRecordingConfiguration) => { value.audioCodec.bitrate = 32000; },
    (value: CameraRecordingConfiguration) => { value.mediaContainerConfiguration.fragmentLength = 5000; },
  ]) {
    const value = configuration(); mutate(value);
    assert.throws(() => selectedRecordingConfiguration(value), /unsupported-recording-format/);
  }
});

test("startup saved intent grants no recording until public HAP selection and durable CAS synchronize", async () => {
  const value = harness(), signal = new AbortController();
  assert.equal(value.delegate.permissions.recording, false);
  await assert.rejects(value.delegate.handleRecordingStreamRequest(1, signal.signal).next(), /recording-unavailable/);
  assert.equal(value.opened.length, 0);
  await value.activate();
  assert.equal(value.updates.length, 1); assert.equal(value.updates[0]!.expected, 0);
  assert.equal(value.delegate.permissions.recording_audio, true);
  assert.equal(value.policies.at(-1)!.recording, true);
  await value.delegate.close();
});

test("one-fragment lookahead delivers initialization, full prebuffer and final tail with a truthful EOS", async () => {
  const value = harness(); await value.activate(); const signal = new AbortController();
  const stream = value.delegate.handleRecordingStreamRequest(7, signal.signal);
  const init = await stream.next(); assert.equal(init.value!.data.toString(), "init"); assert.equal(init.value!.isLast, false);
  assert.equal(value.source.pulls, 1);
  const first = await stream.next(); assert.equal(first.value!.data.toString(), "first"); assert.equal(first.value!.isLast, false);
  assert.equal(value.source.pulls, 3);
  const tail = await stream.next(); assert.equal(tail.value!.data.toString(), "tail"); assert.equal(tail.value!.isLast, true);
  assert.equal((await stream.next()).done, true); assert.equal(value.delegate.count, 0); assert.equal(value.source.stops, 1);
  assert.equal(value.failures, 0); await value.delegate.close();
});

test("ordinary quality changes affect the next recording and preserve the active format and source", async () => {
  const value = harness(); await value.activate(); const signal = new AbortController();
  const stream = value.delegate.handleRecordingStreamRequest(8, signal.signal); await stream.next();
  const changed = configuration(); changed.videoCodec.parameters.bitRate = 1500;
  value.delegate.updateRecordingConfiguration(changed); await value.delegate.changing;
  assert.equal(value.source.stops, 0); assert.equal(value.delegate.permissions.recording, true);
  assert.equal(value.opened[0]!.video.max_bit_rate, 1000);
  assert.equal((await stream.next()).value!.data.toString(), "first");
  await stream.return(undefined);
  value.source = new Source(); const next = value.delegate.handleRecordingStreamRequest(8, new AbortController().signal);
  await next.next(); assert.equal(value.opened[1]!.video.max_bit_rate, 1500); await next.return(undefined);
  await value.delegate.close();
});

test("RecordingAudioActive off fences immediately and new initialization omits audio without losing selected AAC", async () => {
  const value = harness(); await value.activate(); const stream = value.delegate.handleRecordingStreamRequest(1, new AbortController().signal);
  await stream.next(); const disabling = value.delegate.setAudioActive(false);
  assert.equal(value.delegate.permissions.recording, false); assert.equal(value.source.stops, 1);
  await disabling; await assert.rejects(stream.next(), /recording-retired|aborted/);
  assert.equal(value.delegate.current.configuration!.audio!.sample_rate, 24000);
  value.source = new Source(); const silent = value.delegate.handleRecordingStreamRequest(1, new AbortController().signal);
  await silent.next(); assert.equal(value.opened.at(-1)!.audio, null); await silent.return(undefined);
  await value.delegate.close();
});

test("HDS unique signal closes a blocked pull; reused numeric callbacks cannot cancel a newer connection", async () => {
  const value = harness(); await value.activate(); const signal = new AbortController();
  const old = value.delegate.handleRecordingStreamRequest(3, signal.signal); await old.next();
  value.source.hold = true; const pulling = old.next(), rejected = assert.rejects(pulling, /aborted|recording-retired/);
  await tick(); signal.abort(); await rejected;
  assert.equal(value.delegate.count, 0);
  value.source = new Source(); const next = value.delegate.handleRecordingStreamRequest(3, new AbortController().signal);
  await next.next(); value.delegate.closeRecordingStream(3, undefined); value.delegate.acknowledgeStream(3);
  assert.equal(value.source.stops, 0); assert.equal((await next.next()).value!.data.toString(), "first");
  await next.return(undefined); await value.delegate.close();
});

test("privacy off during OPEN joins late source cleanup and never yields initialization", async () => {
  const value = harness(); await value.activate(); let release!: () => void;
  value.setHoldOpen(new Promise(resolve => { release = resolve; }));
  const stream = value.delegate.handleRecordingStreamRequest(1, new AbortController().signal);
  const opening = stream.next(), rejected = assert.rejects(opening, /aborted|recording-retired/); await tick();
  value.delegate.updateRecordingActive(false); assert.equal(value.delegate.permissions.recording, false);
  release(); await rejected; await value.delegate.changing;
  assert.equal(value.delegate.count, 0); assert.equal(value.source.stops, 1); await value.delegate.close();
});

test("HomeKit camera disable fences recording and preserves unrelated live policy fields", async () => {
  const value = harness(); await value.activate(); const stream = value.delegate.handleRecordingStreamRequest(1, new AbortController().signal);
  await stream.next(); value.setActive(false); const disabling = value.delegate.activeChanged();
  assert.equal(value.source.stops, 1); await disabling; await assert.rejects(stream.next(), /recording-retired|aborted/);
  assert.equal(value.policies.at(-1)!.streaming, false); assert.equal(value.policies.at(-1)!.recording, false);
  await value.delegate.close();
});

test("a failed native close retains capacity and keeps later admission denied", async () => {
  const value = harness(); await value.activate(); const stream = value.delegate.handleRecordingStreamRequest(1, new AbortController().signal);
  await stream.next(); value.source.rejectStop = true;
  await assert.rejects(value.delegate.fence(), /retained-cleanup/); assert.equal(value.delegate.count, 1);
  await assert.rejects(value.delegate.handleRecordingStreamRequest(2, new AbortController().signal).next(), /recording-unavailable/);
  await assert.rejects(stream.return(undefined), /retained-cleanup/); await assert.rejects(value.delegate.close(), /retained-cleanup/);
});

test("uncertain durable recording writes remain denied after the bounded deadline", async context => {
  context.mock.timers.enable({apis: ["setTimeout"]});
  const value = harness(); value.setHoldWrite(new Promise(() => {}));
  const activating = value.activate(), rejected = assert.rejects(activating, /recording-settings-timeout/);
  await tick(); context.mock.timers.tick(RECORDING_SETTINGS_TIMEOUT_MS); await rejected;
  assert.equal(value.delegate.permissions.recording, false); assert.equal(value.opened.length, 0);
  assert.equal(value.failures, 0); assert.equal(value.delegate.failureReason, "recording-settings-unavailable");
  await tick();
  assert.equal(value.policies.at(-1)!.recording, false); assert.equal(value.policies.at(-1)!.streaming, true);
  await value.delegate.close();
});

test("a privacy change during final policy synchronization is durably drained before either waiter completes", async () => {
  const value = harness(); await value.activate(); let release!: () => void;
  value.setHoldPolicy(new Promise(resolve => { release = resolve; }));
  const changed = configuration(); changed.videoCodec.parameters.bitRate = 1500;
  value.delegate.updateRecordingConfiguration(changed); const first = value.delegate.changing!;
  await tick(); assert.equal(value.updates.length, 2);
  const second = value.delegate.setAudioActive(false);
  release(); await Promise.all([first, second]);
  assert.equal(value.updates.length, 3); assert.equal(value.updates[2]!.audio, false);
  assert.equal(value.delegate.current.audio_active, false); assert.equal(value.delegate.permissions.recording_audio, false);
  assert.equal(value.delegate.current.configuration!.video.max_bit_rate, 1500);
  assert.equal(value.delegate.changing, null); assert.equal(value.failures, 0);
  await value.delegate.close();
});

test("retiring during an unresolved policy synchronization promptly fences the generator before OPEN", async () => {
  const value = harness(); await value.activate(); value.setHoldPolicy(new Promise(() => {}));
  const signal = new AbortController(), stream = value.delegate.handleRecordingStreamRequest(1, signal.signal);
  const pulling = stream.next(), rejected = assert.rejects(pulling, /recording-retired/);
  await tick(); signal.abort(); await rejected;
  assert.equal(value.opened.length, 0); assert.equal(value.delegate.count, 0);
  await value.delegate.close();
});
