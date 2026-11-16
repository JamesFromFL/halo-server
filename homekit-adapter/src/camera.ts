import {
  Accessory, AudioRecordingCodecType, AudioRecordingSamplerate, MediaContainerType, PhysicalConnectionBudget, VideoCodecType, AudioBitrate, AudioStreamingCodecType, AudioStreamingSamplerate, CameraController, Categories, Characteristic, DoorbellController,
  H264Level, H264Profile, HAPStatus, ResourceRequestReason, Service, SRTPCryptoSuites, StreamRequestTypes,
  type CameraRecordingDelegate, type CameraStreamingDelegate,
} from "@homebridge/hap-nodejs";
import { ProtocolError, type AccessoryAudio, type AccessoryRecording, type CameraPolicy, type MediaScope } from "./protocol.js";
import type { NativeCamera, CameraDelegate } from "./session.js";
import { HapVideoDelegate } from "./video.js";
import { AudioControls } from "./audio.js";
import { HapRecordingDelegate } from "./recording.js";
import { CameraControls } from "./controls.js";

const recordingConnections = new PhysicalConnectionBudget(16);

export interface AccessoryIdentity { camera_id: string; accessory_id: string; username: string; doorbell: boolean; audio?: AccessoryAudio | null; recording?: AccessoryRecording | null }
export interface MotionState { motion: boolean | null; synchronized: boolean; source_state: "connected" | "connecting" | "offline" }

/** These delegates intentionally expose no RTP endpoints or synthetic image bytes. */
export function unavailableStreamingDelegate(): CameraStreamingDelegate {
  return {
    handleSnapshotRequest(_request, callback) { callback(HAPStatus.SERVICE_COMMUNICATION_FAILURE); },
    prepareStream(_request, callback) { callback(new Error("media-not-ready")); },
    handleStreamRequest(request, callback) {
      if (request.type === StreamRequestTypes.STOP) callback();
      else callback(new Error("media-not-ready"));
    },
  };
}

function unavailableRecordingDelegate(): CameraRecordingDelegate {
  return {updateRecordingActive() {}, updateRecordingConfiguration() {},
    async *handleRecordingStreamRequest() { throw new Error("recording-not-ready"); }, closeRecordingStream() {}};
}

/** Independent camera graph; Python gates publication on its native output preflight. */
export class CameraAccessory {
  readonly accessory: Accessory;
  readonly controller: CameraController;
  private current: NativeCamera;
  private retired = false;
  private lastState: MotionState = {motion: null, synchronized: false, source_state: "connecting"};
  private audioControls: AudioControls | null = null;
  private controls: CameraControls | null = null;
  readonly streamingDelegate: HapVideoDelegate | null;
  readonly recordingDelegate: HapRecordingDelegate | null;

  constructor(readonly identity: AccessoryIdentity, camera: NativeCamera, options: {native?: CameraDelegate; failure?: () => void; reportError?: (stage: string, code: string) => void} = {}) {
    this.current = structuredClone(camera);
    const name = camera.name || (identity.doorbell ? "Ring Doorbell" : "Ring Camera");
    this.accessory = new Accessory(name, identity.accessory_id);
    this.accessory.category = identity.doorbell ? Categories.VIDEO_DOORBELL : Categories.IP_CAMERA;
    this.accessory.getService(Service.AccessoryInformation)!
      .setCharacteristic(Characteristic.Manufacturer, "Ring via Halo")
      .setCharacteristic(Characteristic.Model, camera.model || "Ring Camera")
      .setCharacteristic(Characteristic.SerialNumber, identity.accessory_id)
      .setCharacteristic(Characteristic.FirmwareRevision, "0.1.0");
    this.accessory.on("identify", (_paired, callback) => callback());
    this.recordingDelegate = options.native && identity.recording?.enabled ? new HapRecordingDelegate(options.native, identity.recording, {
      active: () => !this.retired && this.homeKitActive() && this.audioControls?.available !== false,
      policy: () => this.policy,
      closeTransport: () => this.controller.closeRecordingStreams(),
      failure: options.failure ?? (() => {}),
    }) : null;
    this.streamingDelegate = options.native ? new HapVideoDelegate(options.native, {
      active: () => !this.retired && this.homeKitActive() && this.streamingActive() && this.audioControls?.available !== false,
      policy: () => this.policy,
      audio: () => this.audioControls?.current ?? identity.audio ?? null,
      snapshotAllowed: reason => this.snapshotAllowed(reason),
      forceStop: hapId => this.controller.forceStopStreamingSession(hapId),
      ...(options.reportError === undefined ? {} : {reportError: options.reportError}),
      failure: options.failure ?? (() => {}),
    }) : null;
    const controllerOptions = {
      cameraStreamCount: 2,
      delegate: this.streamingDelegate ?? unavailableStreamingDelegate(),
      streamingOptions: {
        supportedCryptoSuites: [SRTPCryptoSuites.AES_CM_128_HMAC_SHA1_80],
        video: {codec: {profiles: [H264Profile.BASELINE, H264Profile.MAIN, H264Profile.HIGH],
          levels: [H264Level.LEVEL3_1, H264Level.LEVEL3_2, H264Level.LEVEL4_0]},
          resolutions: [[320, 240, 15], [640, 360, 30], [640, 480, 30], [1280, 720, 30], [1920, 1080, 30]] as [number, number, number][]},
        ...(identity.audio?.microphone ? {audio: {codecs: [AudioBitrate.VARIABLE, AudioBitrate.CONSTANT].map(bitrate => ({
          type: AudioStreamingCodecType.OPUS, audioChannels: 1, bitrate,
          samplerate: [AudioStreamingSamplerate.KHZ_8, AudioStreamingSamplerate.KHZ_16, AudioStreamingSamplerate.KHZ_24],
        })), twoWayAudio: identity.audio.speaker, comfort_noise: false}} : {}),
      },
      ...(identity.recording?.enabled ? {recording: {delegate: this.recordingDelegate ?? unavailableRecordingDelegate(), options: {
        prebufferLength: 4000, mediaContainerConfiguration: {type: MediaContainerType.FRAGMENTED_MP4, fragmentLength: 4000},
        video: {type: VideoCodecType.H264, parameters: {profiles: [H264Profile.BASELINE, H264Profile.MAIN, H264Profile.HIGH],
          levels: [H264Level.LEVEL3_1, H264Level.LEVEL3_2, H264Level.LEVEL4_0]},
          resolutions: [15, 24, 30].flatMap(fps => [[1280, 720, fps], [1920, 1080, fps]] as [number, number, number][])},
        audio: {codecs: [{type: AudioRecordingCodecType.AAC_LC, audioChannels: 1, bitrateMode: AudioBitrate.VARIABLE,
          samplerate: [AudioRecordingSamplerate.KHZ_16, AudioRecordingSamplerate.KHZ_24, AudioRecordingSamplerate.KHZ_48]}]},
        dataStreamTransport: {connectionBudget: recordingConnections, maximumConnections: 4, maximumPreparedSessions: 16,
          writerLimits: {maximumFrameBytes: 266240, maximumPendingFrames: 2, maximumPendingBytes: 532480,
            writeTimeoutMs: 5000, closeGraceMs: 1000, closeTimeoutMs: 1000}},
      }}} : {}),
      sensors: {motion: true},
    };
    this.controller = identity.doorbell ? new DoorbellController({...controllerOptions, name}) : new CameraController(controllerOptions);
    this.accessory.configureController(this.controller);
    this.recordingDelegate?.attach(this.controller);
    this.controls = new CameraControls(this.accessory, camera, options.native,
      () => !this.retired && this.homeKitActive(), () => this.synchronizePolicy());
    if (identity.audio !== undefined && identity.audio !== null) {
      this.audioControls = new AudioControls(this.accessory, this.controller, identity.audio, {
        ...(options.reportError === undefined ? {} : {reportError: options.reportError}),
        fence: () => this.fence(),
        persist: (revision, settings) => options.native?.updateAudioSettings(revision, settings) ?? Promise.reject(new ProtocolError("audio-settings-unavailable")),
        synchronize: () => this.streamingDelegate?.synchronizePolicy() ?? Promise.resolve(),
      });
    }
    for (const service of this.accessory.services.filter(service => service.UUID === Service.CameraRTPStreamManagement.UUID)) {
      service.getCharacteristic(Characteristic.Active).on("change", () => {
        void this.streamingDelegate?.activeChanged().catch(() => options.failure?.());
      });
    }
    const operating = this.controller.recordingManagement?.operatingModeService;
    operating?.getCharacteristic(Characteristic.HomeKitCameraActive).on("change", () => {
      this.applyState(this.lastState);
      void Promise.all([this.streamingDelegate?.activeChanged(), this.recordingDelegate?.activeChanged()]).catch(() => options.failure?.());
    });
    for (const type of [Characteristic.EventSnapshotsActive, Characteristic.PeriodicSnapshotsActive]) {
      operating?.getCharacteristic(type).on("change", () => {
        void this.streamingDelegate?.snapshotPolicyChanged().catch(() => options.failure?.());
      });
    }
    this.applyState({motion: null, synchronized: false, source_state: "connecting"});
  }

  get scope(): MediaScope { return {...this.current.scope}; }
  get isRetired(): boolean { return this.retired; }
  private streamingActive(): boolean {
    const services = this.accessory.services.filter(service => service.UUID === Service.CameraRTPStreamManagement.UUID);
    return services.length > 0 && services.every(service => service.getCharacteristic(Characteristic.Active).value === true ||
      service.getCharacteristic(Characteristic.Active).value === 1);
  }
  private homeKitActive(): boolean {
    const operating = this.controller.recordingManagement?.operatingModeService;
    if (!operating) return true;
    const value = operating.getCharacteristic(Characteristic.HomeKitCameraActive).value;
    return value === true || value === 1;
  }
  private snapshotAllowed(reason?: ResourceRequestReason): boolean {
    if (this.retired || !this.homeKitActive() || !this.streamingActive()) return false;
    const operating = this.controller.recordingManagement?.operatingModeService;
    if (!operating) return true;
    const enabled = (type: typeof Characteristic.EventSnapshotsActive | typeof Characteristic.PeriodicSnapshotsActive): boolean => {
      const value = operating.getCharacteristic(type).value;
      return value === true || value === 1;
    };
    const event = enabled(Characteristic.EventSnapshotsActive), periodic = enabled(Characteristic.PeriodicSnapshotsActive);
    return reason === ResourceRequestReason.EVENT ? event : reason === ResourceRequestReason.PERIODIC ? periodic : event && periodic;
  }
  get policy(): CameraPolicy {
    const active = !this.retired && this.homeKitActive() && this.audioControls?.available !== false;
    const streaming = active && this.streamingActive(), audio = this.audioControls?.current ?? this.identity.audio ?? null;
    const recording = this.recordingDelegate?.permissions ?? {recording: false, recording_audio: false};
    return {synchronized: true, streaming, snapshots: streaming && this.current.capabilities.snapshot &&
      (this.snapshotAllowed(ResourceRequestReason.EVENT) || this.snapshotAllowed(ResourceRequestReason.PERIODIC)),
      live_audio: streaming && audio?.microphone === true && !audio.microphone_muted && audio.microphone_volume > 0,
      talkback: streaming && this.current.capabilities.talkback && audio?.speaker === true && !audio.speaker_muted && audio.speaker_volume > 0,
      controls: active && this.controls?.available === true, recording: active && recording.recording, recording_audio: active && recording.recording_audio};
  }
  private fence(): Promise<void> {
    return Promise.all([this.streamingDelegate?.fence(), this.recordingDelegate?.fence()]).then(() => {});
  }
  get audio(): AccessoryAudio | null { return this.audioControls?.current ?? null; }
  async updateAudio(value: AccessoryAudio | null): Promise<void> {
    if (value === null ? this.audioControls !== null : this.audioControls === null) throw new ProtocolError("audio-graph-changed");
    if (value !== null) await this.audioControls!.update(value);
  }
  async updateRecording(value: AccessoryRecording | null): Promise<void> {
    if ((value?.enabled === true) !== (this.identity.recording?.enabled === true)) throw new ProtocolError("recording-graph-changed");
    if (value !== null) await this.recordingDelegate?.update(value);
  }
  async synchronizePolicy(): Promise<void> {
    await this.audioControls?.changing?.catch(() => {});
    await this.recordingDelegate?.changing?.catch(() => {});
    await this.streamingDelegate?.synchronizePolicy();
  }
  async closeStreams(): Promise<void> {
    this.controls?.retire();
    this.audioControls?.retire();
    this.recordingDelegate?.retire();
    await Promise.all([this.audioControls?.settle(), this.streamingDelegate?.close(), this.recordingDelegate?.close(),
      this.controller.closeRecordingStreams()]);
  }
  update(camera: NativeCamera): void {
    if (this.retired) return;
    this.current = structuredClone(camera);
    this.controls?.update(camera);
    const name = camera.name || (this.identity.doorbell ? "Ring Doorbell" : "Ring Camera");
    this.accessory.displayName = name;
    this.accessory.getService(Service.AccessoryInformation)!
      .updateCharacteristic(Characteristic.Name, name)
      .updateCharacteristic(Characteristic.Model, camera.model || "Ring Camera");
  }
  applyState(state: MotionState): void {
    this.lastState = {...state};
    const active = !this.retired && this.homeKitActive() && state.synchronized && state.source_state === "connected" && state.motion !== null;
    const sensor = this.controller.motionService!;
    sensor.updateCharacteristic(Characteristic.MotionDetected, active && state.motion === true);
    sensor.updateCharacteristic(Characteristic.StatusActive, active);
    sensor.updateCharacteristic(Characteristic.StatusFault,
      active ? Characteristic.StatusFault.NO_FAULT : Characteristic.StatusFault.GENERAL_FAULT);
  }
  ring(): void {
    if (!this.retired && this.controller instanceof DoorbellController) this.controller.ringDoorbell();
  }
  retire(): void {
    this.retired = true;
    this.controls?.retire();
    this.audioControls?.retire();
    this.recordingDelegate?.retire();
    this.applyState({motion: null, synchronized: false, source_state: "offline"});
  }
}
