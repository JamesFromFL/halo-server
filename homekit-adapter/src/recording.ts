import {
  AudioBitrate, AudioRecordingCodecType, AudioRecordingSamplerate, Characteristic, H264Level, H264Profile,
  HAPStatus, MediaContainerType, VideoCodecType, type CameraController, type CameraRecordingConfiguration,
  type CameraRecordingDelegate, type HDSProtocolSpecificErrorReason, type RecordingPacket,
} from "@homebridge/hap-nodejs";
import {
  ProtocolError, recordingConfiguration, type AccessoryRecording, type CameraPolicy, type RecordingConfiguration,
} from "./protocol.js";
import type { CameraDelegate, NativeRecording } from "./session.js";

export const RECORDING_SETTINGS_TIMEOUT_MS = 7_000;
export const RECORDING_STREAM_TIMEOUT_MS = 180_000;
interface Preferences {active: boolean; audio_active: boolean; configuration: RecordingConfiguration | null}
interface OwnedRecording {
  hapId: number; abort: AbortController; retired: boolean; native: NativeRecording | null;
  opening: Promise<NativeRecording> | null; closing: Promise<void> | null; timer: NodeJS.Timeout;
}
export interface RecordingDelegateOptions {
  active: () => boolean;
  policy: () => CameraPolicy;
  closeTransport: () => Promise<void>;
  failure: () => void;
}
const preferences = (value: Preferences): Preferences => ({active: value.active,
  audio_active: value.audio_active, configuration: structuredClone(value.configuration)});
const equal = (left: Preferences, right: Preferences): boolean => JSON.stringify(preferences(left)) === JSON.stringify(preferences(right));
const revokes = (left: Preferences, right: Preferences): boolean => left.active && !right.active ||
  left.configuration !== null && right.configuration === null || left.audio_active && !right.audio_active;
async function abortable<T>(operation: Promise<T>, signal: AbortSignal): Promise<T> {
  if (signal.aborted) throw new ProtocolError("recording-retired");
  let aborted: (() => void) | undefined;
  const cancellation = new Promise<never>((_resolve, reject) => {
    aborted = () => reject(new ProtocolError("recording-retired"));
    signal.addEventListener("abort", aborted, {once: true});
  });
  try { return await Promise.race([operation, cancellation]); }
  finally { if (aborted) signal.removeEventListener("abort", aborted); }
}

/** Convert only the public, selected recording format; RTP settings never enter the MP4 pipeline. */
export function selectedRecordingConfiguration(value: CameraRecordingConfiguration): RecordingConfiguration {
  const video = value.videoCodec, audio = value.audioCodec;
  if (value.prebufferLength !== 4000 || value.mediaContainerConfiguration.type !== MediaContainerType.FRAGMENTED_MP4 ||
      value.mediaContainerConfiguration.fragmentLength !== 4000 || video.type !== VideoCodecType.H264 ||
      audio.type !== AudioRecordingCodecType.AAC_LC || audio.audioChannels !== 1 || audio.bitrateMode !== AudioBitrate.VARIABLE) {
    throw new ProtocolError("unsupported-recording-format");
  }
  const profile = video.parameters.profile === H264Profile.BASELINE ? "baseline" :
    video.parameters.profile === H264Profile.MAIN ? "main" : video.parameters.profile === H264Profile.HIGH ? "high" : null;
  const level = video.parameters.level === H264Level.LEVEL3_1 ? "3.1" :
    video.parameters.level === H264Level.LEVEL3_2 ? "3.2" : video.parameters.level === H264Level.LEVEL4_0 ? "4.0" : null;
  const rate = audio.samplerate === AudioRecordingSamplerate.KHZ_16 ? 16000 :
    audio.samplerate === AudioRecordingSamplerate.KHZ_24 ? 24000 : audio.samplerate === AudioRecordingSamplerate.KHZ_48 ? 48000 : null;
  if (!profile || !level || !rate) throw new ProtocolError("unsupported-recording-format");
  const result: RecordingConfiguration = {video: {width: video.resolution[0], height: video.resolution[1], fps: video.resolution[2],
    profile, level, max_bit_rate: video.parameters.bitRate},
    audio: {sample_rate: rate, bit_rate: audio.bitrate * 1000, channels: 1, bitrate_mode: "vbr"},
    fragment_length_ms: 4000, i_frame_interval_ms: video.parameters.iFrameInterval};
  if (!recordingConfiguration(result)) throw new ProtocolError("unsupported-recording-format");
  return result;
}

/** HAP callbacks describe intent. Parent CAS, current policy and actual warm history grant delivery. */
export class HapRecordingDelegate implements CameraRecordingDelegate {
  private state: AccessoryRecording;
  // HAP's documented startup state is inactive with no selected configuration.
  private desired: Preferences = {active: false, audio_active: false, configuration: null};
  private blocked = false;
  private retired = false;
  private cleanupFailed = false;
  private settingsFailure: string | null = null;
  private pending: Promise<void> | null = null;
  private job: OwnedRecording | null = null;
  private fencing: Promise<void> | null = null;
  private audioCharacteristic: Characteristic | null = null;
  private retirement = new AbortController();

  constructor(private readonly native: CameraDelegate, initial: AccessoryRecording,
    private readonly options: RecordingDelegateOptions) { this.state = structuredClone(initial); }
  get current(): AccessoryRecording { return structuredClone(this.state); }
  get changing(): Promise<void> | null { return this.pending; }
  get count(): number { return this.job === null ? 0 : 1; }
  get failureReason(): string | null { return this.cleanupFailed ? "recording-cleanup-failed" : this.settingsFailure; }
  get available(): boolean { return !this.retired && !this.blocked && !this.cleanupFailed && this.state.enabled; }
  get permissions(): {recording: boolean; recording_audio: boolean} {
    const recording = this.available && this.options.active() && this.state.active && this.desired.active &&
      this.state.configuration !== null && this.desired.configuration !== null;
    return {recording, recording_audio: recording && this.state.audio_active && this.desired.audio_active &&
      this.state.configuration?.audio !== null && this.desired.configuration?.audio !== null};
  }

  attach(controller: CameraController): void {
    const management = controller.recordingManagement;
    if (!management || this.audioCharacteristic) throw new ProtocolError("recording-graph-unavailable");
    const characteristic = management.recordingManagementService.getCharacteristic(Characteristic.RecordingAudioActive);
    this.audioCharacteristic = characteristic;
    this.desired.audio_active = characteristic.value === true || characteristic.value === 1;
    // The released library has no audio delegate callback. This public handler
    // delays a successful audio write until its durable parent acknowledgement.
    characteristic.onSet(async value => {
      if (typeof value !== "boolean" && value !== 0 && value !== 1) throw HAPStatus.INVALID_VALUE_IN_REQUEST;
      try { await this.setAudioActive(value === true || value === 1); }
      catch { throw HAPStatus.SERVICE_COMMUNICATION_FAILURE; }
    });
    characteristic.on("change", change => {
      const active = change.newValue === true || change.newValue === 1;
      if (active !== this.desired.audio_active) void this.setAudioActive(active).catch(() => {});
    });
  }

  updateRecordingActive(active: boolean): void {
    if (this.retired) return;
    if (typeof active !== "boolean") { this.settingsFailed("invalid-recording-active"); return; }
    void this.change({...this.desired, active}).catch(() => {});
  }
  updateRecordingConfiguration(configuration: CameraRecordingConfiguration | undefined): void {
    if (this.retired) return;
    let selected: RecordingConfiguration | null = null;
    try { if (configuration !== undefined) selected = selectedRecordingConfiguration(configuration); }
    catch {
      void this.change({...this.desired, configuration: null}).catch(() => {});
      this.settingsFailed("unsupported-recording-format"); return;
    }
    void this.change({...this.desired, configuration: selected}).catch(() => {});
  }
  setAudioActive(active: boolean): Promise<void> {
    if (typeof active !== "boolean") return Promise.reject(new ProtocolError("invalid-recording-audio"));
    return this.change({...this.desired, audio_active: active});
  }

  private change(value: Preferences): Promise<void> {
    if (this.retired) return Promise.reject(new ProtocolError("recording-retired"));
    const revoke = revokes(this.desired, value);
    this.desired = preferences(value);
    if (revoke) {
      this.blocked = true;
      // Local packet and encrypted-transport fences precede disk/IPC awaits.
      void this.fence().catch(() => this.fail());
    }
    if (!this.pending) {
      this.pending = Promise.resolve().then(() => this.flush()).catch(error => {
        if (!this.retired) this.settingsFailed("recording-settings-unavailable"); throw error;
      })
        .finally(() => {
          this.pending = null;
          // Also cover an intent arriving between flush resolution and this
          // finalizer; coalesce it into one new operation, never a waiter queue.
          if (!this.retired && this.settingsFailure === null && !equal(this.desired, this.state)) return this.change(this.desired);
        });
      // HAP's active/configuration callbacks intentionally return void. Failure
      // remains local and fail-closed; actual cleanup failure escalates.
      void this.pending.catch(() => {});
    }
    return this.pending;
  }
  private async flush(): Promise<void> {
    while (true) {
      while (!this.retired && !equal(this.desired, this.state)) {
        const next = preferences(this.desired), expected = this.state.revision;
        let timer: NodeJS.Timeout | undefined;
        let abort: (() => void) | undefined;
        const deadline = new Promise<never>((_resolve, reject) => {
          timer = setTimeout(() => reject(new ProtocolError("recording-settings-timeout")), RECORDING_SETTINGS_TIMEOUT_MS);
          abort = () => reject(new ProtocolError("recording-retired"));
          this.retirement.signal.addEventListener("abort", abort, {once: true});
        });
        try {
          const revision = await Promise.race([
            this.native.updateRecordingSettings(expected, next.active, next.audio_active, next.configuration), deadline,
          ]);
          if (this.retired || revision < this.state.revision || revision === this.state.revision && !equal(next, this.state)) {
            throw new ProtocolError("recording-settings-conflict");
          }
          this.state = {...this.state, ...next, revision};
        } finally {
          if (timer) clearTimeout(timer);
          if (abort) this.retirement.signal.removeEventListener("abort", abort);
        }
      }
      if (this.retired) throw new ProtocolError("recording-retired");
      if (this.fencing) await this.fencing;
      if (!this.cleanupFailed) { this.blocked = false; this.settingsFailure = null; }
      await this.synchronizePolicy();
      // Public HAP callbacks can arrive while policy synchronization awaits IPC.
      // Their promises must cover the latest durable intent as well.
      if (equal(this.desired, this.state)) return;
    }
  }
  private fail(): void { this.blocked = true; this.options.failure(); }
  private settingsFailed(reason: string): void {
    this.blocked = true; this.settingsFailure = reason;
    // A selection/CAS error belongs to this camera. Fence recording and send
    // the aggregate deny policy without disrupting unrelated accessories.
    void this.fence().then(() => this.synchronizePolicy()).catch(() => {
      if (!this.retired) this.fail();
    });
  }

  /** Complete parent pages are the authority after an uncertain write or owner opt-out. */
  async update(value: AccessoryRecording): Promise<void> {
    if (this.retired || value.revision < this.state.revision) return;
    if (value.enabled !== this.state.enabled || value.revision === this.state.revision && !equal(value, this.state)) {
      throw new ProtocolError("recording-settings-conflict");
    }
    const cleanup = revokes(this.state, value) ? this.fence() : Promise.resolve();
    this.state = structuredClone(value);
    await cleanup;
    if (!this.pending && equal(this.desired, value) && !this.cleanupFailed) {
      this.blocked = false; this.settingsFailure = null;
    }
  }
  async synchronizePolicy(signal: AbortSignal = this.retirement.signal): Promise<void> {
    while (!this.retired) {
      const policy = this.options.policy();
      await abortable(this.native.ensurePolicy(policy), signal);
      if (JSON.stringify(policy) === JSON.stringify(this.options.policy())) return;
    }
  }

  private assertCurrent(job: OwnedRecording): void {
    if (this.job !== job || job.retired || job.abort.signal.aborted || !this.permissions.recording ||
        job.native !== null && !job.native.isCurrent()) throw new ProtocolError("recording-retired");
  }
  async *handleRecordingStreamRequest(streamId: number, signal?: AbortSignal): AsyncGenerator<RecordingPacket> {
    // A numerical stream ID has meaning only on its HDS connection. The approved
    // transport supplies the unique signal which owns all local cancellation.
    if (!Number.isSafeInteger(streamId) || streamId < 0 || !(signal instanceof AbortSignal) || signal.aborted || this.job) {
      throw new ProtocolError("recording-unavailable");
    }
    if (this.pending) await this.pending;
    if (this.job || signal.aborted || !this.permissions.recording || !equal(this.desired, this.state)) throw new ProtocolError("recording-unavailable");
    const configuration = structuredClone(this.state.configuration!);
    if (!this.permissions.recording_audio) configuration.audio = null;
    const abort = new AbortController();
    const timer = setTimeout(() => { abort.abort(); void this.fence().catch(() => this.fail()); }, RECORDING_STREAM_TIMEOUT_MS);
    const job: OwnedRecording = {hapId: streamId, abort, retired: false, native: null, opening: null, closing: null, timer};
    this.job = job;
    const closed = () => { void this.stop(job).catch(() => this.fail()); };
    signal.addEventListener("abort", closed, {once: true});
    if (signal.aborted) closed();
    try {
      await this.synchronizePolicy(abort.signal); this.assertCurrent(job);
      job.opening = this.native.openRecording(configuration, abort.signal);
      job.native = await job.opening; this.assertCurrent(job);
      let packet = await job.native.next(abort.signal); this.assertCurrent(job);
      if (!packet || packet.kind !== "init") throw new ProtocolError("recording-init-required");
      yield {data: packet.data, isLast: false};
      packet = await job.native.next(abort.signal); this.assertCurrent(job);
      if (!packet || packet.kind !== "fragment") throw new ProtocolError("recording-fragment-required");
      // One-fragment lookahead supplies a truthful isLast without discarding a
      // tail or retaining an unbounded movie. HDS awaits every yielded write.
      while (true) {
        const following = await job.native.next(abort.signal); this.assertCurrent(job);
        if (following !== null && following.kind !== "fragment") throw new ProtocolError("recording-fragment-required");
        this.assertCurrent(job);
        yield {data: packet.data, isLast: following === null};
        if (following === null) return;
        packet = following;
      }
    } finally {
      signal.removeEventListener("abort", closed);
      await this.stop(job);
    }
  }
  // HAP guarantees its AbortSignal is fenced before either callback. Never use
  // the reusable numerical ID as authority to cancel a possibly newer job.
  acknowledgeStream(_streamId: number): void { }
  closeRecordingStream(_streamId: number, _reason: HDSProtocolSpecificErrorReason | undefined): void { }

  private stop(job: OwnedRecording): Promise<void> {
    job.retired = true; job.abort.abort(); clearTimeout(job.timer);
    if (job.closing) return job.closing;
    job.closing = (async () => {
      const owner = job.native ?? await job.opening?.catch(() => null);
      await owner?.stop();
      if (this.job === job) this.job = null;
    })().catch(error => { this.cleanupFailed = true; this.fail(); throw error; });
    return job.closing;
  }
  fence(): Promise<void> {
    if (this.fencing) return this.fencing;
    const native = this.job === null ? Promise.resolve() : this.stop(this.job);
    const transport = this.options.closeTransport();
    this.fencing = Promise.all([native, transport]).then(() => {}).catch(error => {
      this.cleanupFailed = true; this.fail(); throw error;
    }).finally(() => { this.fencing = null; });
    return this.fencing;
  }
  async activeChanged(): Promise<void> {
    if (!this.options.active()) await this.fence();
    await this.synchronizePolicy();
  }
  retire(): void {
    this.retired = true; this.blocked = true; this.retirement.abort();
    if (this.job) { this.job.retired = true; this.job.abort.abort(); }
  }
  settle(): Promise<void> { return this.pending?.catch(() => {}) ?? Promise.resolve(); }
  async close(): Promise<void> { this.retire(); await Promise.all([this.settle(), this.fence()]); }
}
