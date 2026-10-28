import assert from "node:assert/strict";
import { test } from "node:test";
import { AudioBitrate, AudioRecordingCodecType, AudioRecordingSamplerate, Characteristic, H264Level, H264Profile,
  MediaContainerType, Service, VideoCodecType, type CameraRecordingConfiguration } from "@homebridge/hap-nodejs";
import { CameraAccessory, type AccessoryIdentity } from "../src/camera.js";
import type { CameraDelegate, NativeCamera } from "../src/session.js";
import type { AccessoryAudio, AccessoryRecording, CameraPolicy } from "../src/protocol.js";

const recording: AccessoryRecording = {enabled: true, active: false, audio_active: false, configuration: null, revision: 0};
const audio: AccessoryAudio = {microphone: true, speaker: true, microphone_muted: false, speaker_muted: false,
  microphone_volume: 100, speaker_volume: 100, revision: 0};
const identity: AccessoryIdentity = {camera_id: "front", accessory_id: "00000000-0000-0000-0000-000000000003",
  username: "02:11:22:33:44:55", doorbell: false, audio, recording};
const camera: NativeCamera = {scope: {profile_id: "00000000-0000-0000-0000-000000000002", account_generation: 1,
  camera_id: "front", device_generation: 1, privacy_generation: 1}, name: "Camera", family: "fixture", model: "fixture", kind: "camera",
  capabilities: {video: true, audio: true, snapshot: true, talkback: true, light: false, siren: false, doorbell: false,
    battery: false, evidence: ["fixture"]}, battery_percent: null, connection_state: null, observed_format: null};
const selected = (): CameraRecordingConfiguration => ({prebufferLength: 4000, eventTriggerTypes: [1],
  mediaContainerConfiguration: {type: MediaContainerType.FRAGMENTED_MP4, fragmentLength: 4000},
  videoCodec: {type: VideoCodecType.H264, parameters: {profile: H264Profile.MAIN, level: H264Level.LEVEL3_1,
    bitRate: 1000, iFrameInterval: 1000}, resolution: [1280, 720, 15]},
  audioCodec: {type: AudioRecordingCodecType.AAC_LC, audioChannels: 1, bitrateMode: AudioBitrate.VARIABLE,
    samplerate: AudioRecordingSamplerate.KHZ_24, bitrate: 32}});
const tick = (): Promise<void> => new Promise(resolve => setImmediate(resolve));
function harness() {
  const policies: CameraPolicy[] = []; let revision = 0;
  const native = {info: structuredClone(camera), ensurePolicy: async (value: CameraPolicy) => {policies.push(structuredClone(value));},
    updateRecordingSettings: async (expected: number) => {assert.equal(expected, revision); return ++revision;},
    updateAudioSettings: async () => 1, stopVideo: async () => {}} as unknown as CameraDelegate;
  const graph = new CameraAccessory(identity, camera, {native});
  const activate = async () => {
    graph.recordingDelegate!.updateRecordingConfiguration(selected()); graph.recordingDelegate!.updateRecordingActive(true);
    await graph.recordingDelegate!.setAudioActive(true); await graph.synchronizePolicy();
  };
  return {graph, policies, activate};
}

test("recording services require explicit graph enrollment and preserve camera identity", async () => {
  const plain = new CameraAccessory({...identity, recording: null}, camera), enabled = new CameraAccessory(identity, camera);
  assert.equal(plain.accessory.UUID, enabled.accessory.UUID);
  assert.equal(plain.accessory.getService(Service.CameraRecordingManagement), undefined);
  assert.ok(enabled.accessory.getService(Service.CameraRecordingManagement));
  assert.ok(enabled.accessory.getService(Service.CameraOperatingMode));
  enabled.update({...camera, name: "Renamed"}); assert.ok(enabled.accessory.getService(Service.CameraRecordingManagement));
  plain.retire(); enabled.retire(); await Promise.all([plain.closeStreams(), enabled.closeStreams()]);
});

test("one policy preserves recording when live streaming is disabled", async () => {
  const {graph, policies, activate} = harness();
  try {
    await activate(); assert.equal(graph.policy.recording, true); assert.equal(graph.policy.recording_audio, true);
    const stream = graph.accessory.services.find(value => value.UUID === Service.CameraRTPStreamManagement.UUID)!;
    stream.updateCharacteristic(Characteristic.Active, false); await tick(); await graph.synchronizePolicy();
    assert.equal(graph.policy.streaming, false); assert.equal(graph.policy.snapshots, false); assert.equal(graph.policy.recording, true);
    assert.equal(policies.at(-1)!.recording, true); assert.equal(policies.at(-1)!.streaming, false);
  } finally {graph.retire(); await graph.closeStreams();}
});

test("public HomeKitCameraActive false fences live and recording together", async () => {
  const {graph, policies, activate} = harness();
  try {
    await activate();
    const operating = graph.controller.recordingManagement!.operatingModeService;
    graph.applyState({motion: true, synchronized: true, source_state: "connected"});
    operating.updateCharacteristic(Characteristic.HomeKitCameraActive, false); await tick(); await graph.synchronizePolicy();
    graph.applyState({motion: true, synchronized: true, source_state: "connected"});
    assert.equal(graph.controller.motionService!.getCharacteristic(Characteristic.MotionDetected).value, false);
    assert.equal(graph.controller.motionService!.getCharacteristic(Characteristic.StatusActive).value, false);
    for (const field of ["streaming", "snapshots", "recording", "recording_audio", "live_audio", "talkback"] as const) {
      assert.equal(graph.policy[field], false); assert.equal(policies.at(-1)![field], false);
    }
    operating.updateCharacteristic(Characteristic.HomeKitCameraActive, true); await tick(); await graph.synchronizePolicy();
    assert.equal(graph.policy.recording, true); assert.equal(graph.policy.streaming, true);
  } finally {graph.retire(); await graph.closeStreams();}
});

test("live microphone preference refresh keeps independent recording-audio intent", async () => {
  const {graph, activate} = harness();
  try {
    await activate(); await graph.updateAudio({...audio, microphone_muted: true, revision: 1}); await graph.synchronizePolicy();
    assert.equal(graph.policy.live_audio, false); assert.equal(graph.policy.recording_audio, true);
    assert.equal(graph.recordingDelegate!.current.audio_active, true);
  } finally {graph.retire(); await graph.closeStreams();}
});
