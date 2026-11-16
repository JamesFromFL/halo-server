import { randomUUID } from "node:crypto";
import { BlockList, isIP } from "node:net";
import {
  AudioBitrate, AudioStreamingCodecType, ResourceRequestReason, H264Level, H264Profile, HAPStatus, SRTPCryptoSuites, StreamRequestTypes,
  VideoCodecPacketizationMode, VideoCodecType, type CameraStreamingDelegate,
  type PrepareStreamRequest, type PrepareStreamResponse, type ReconfiguredVideoInfo,
  type StreamingRequest, type VideoInfo, type AudioInfo,
} from "@homebridge/hap-nodejs";
import type { AccessoryAudio, AudioConfiguration, CameraPolicy, VideoConfiguration, VideoPrepare } from "./protocol.js";
import { ProtocolError } from "./protocol.js";
import type { CameraDelegate, VideoDelegate } from "./session.js";

export const PREPARED_VIDEO_TIMEOUT_MS = 10_000;
interface OwnedVideo {
  hapId: string; wireId: string; retired: boolean; phase: "preparing" | "prepared" | "starting" | "active" | "reconfiguring";
  transport: VideoDelegate | null; config: VideoConfiguration | null; timer: NodeJS.Timeout | null; closing: Promise<void> | null;
  callbacks: Set<() => void>;
}
export interface VideoCameraOptions {
  active: () => boolean;
  audio?: () => AccessoryAudio | null;
  policy?: () => CameraPolicy;
  snapshotAllowed?: (reason?: ResourceRequestReason) => boolean;
  forceStop: (hapId: string) => void;
  failure: () => void;
  reportError?: (stage: string, code: string) => void;
}
export const VIDEO_DIAGNOSTIC_CODES = [
  "operation-failed", "invalid-video-prepare", "invalid-audio-prepare", "video-retired",
  "invalid-video-endpoint", "unsupported-video-profile", "unsupported-video-level", "unsupported-video-codec",
  "unsupported-audio-format", "video-not-prepared", "video-start-conflict", "audio-start-conflict",
  "video-reconfigure-conflict", "unsupported-video-operation", "video-prepare-rejected", "video-endpoint-mismatch",
  "video-configuration-rejected", "video-stop-rejected", "video-cleanup-failed", "video-capacity",
  "request-timeout", "session-closed", "resource-retired", "snapshot-retired", "snapshot-unavailable",
  "snapshot-cancelled", "snapshot-capacity", "snapshot-rejected", "snapshot-transfer-failed",
  "snapshot-busy", "snapshot-timeout", "snapshot-expired", "snapshot-unsupported", "snapshot-unauthorized",
  "snapshot-stale-scope", "snapshot-policy-denied", "snapshot-conflict", "snapshot-not-ready",
  "snapshot-invalid-request", "snapshot-source-failed",
] as const;
const diagnosticCodes = new Set<string>(VIDEO_DIAGNOSTIC_CODES);
const uint32 = (value: number): boolean => Number.isSafeInteger(value) && value >= 0 && value <= 0xffffffff;
const forbidden = new BlockList();
forbidden.addSubnet("0.0.0.0", 8, "ipv4"); forbidden.addSubnet("224.0.0.0", 4, "ipv4");
forbidden.addSubnet("240.0.0.0", 4, "ipv4"); forbidden.addAddress("::", "ipv6"); forbidden.addSubnet("ff00::", 8, "ipv6");
const linkLocal = new BlockList(); linkLocal.addSubnet("fe80::", 10, "ipv6");
export function literalUnicastAddress(value: string, version: "ipv4" | "ipv6"): boolean {
  if (typeof value !== "string") return false;
  const [base, zone, extra] = value.split("%");
  if (!base || extra !== undefined || isIP(base) !== (version === "ipv4" ? 4 : 6) || forbidden.check(base, version)) return false;
  if (zone !== undefined && (version !== "ipv6" || !linkLocal.check(base, "ipv6") ||
      !/^[A-Za-z0-9_.:-]{1,32}$/.test(zone) || /^\d+$/.test(zone) && (!/^[1-9][0-9]*$/.test(zone) || Number(zone) > 0xffffffff))) return false;
  return true;
}
// HAP exposes the TCP socket's raw localAddress; a dual-stack listener reports
// IPv4 peers as ::ffff:a.b.c.d even when the selected RTP family is IPv4.
// Normalize only that exact mapped representation before the existing unicast
// and family checks. True IPv6 and malformed/multicast values remain rejected.
function mediaSourceAddress(value: string, version: "ipv4" | "ipv6"): string {
  if (version === "ipv4" && typeof value === "string" && /^::ffff:/i.test(value)) {
    const mapped = value.slice(7);
    if (isIP(mapped) === 4) return mapped;
  }
  return value;
}
const streamingPolicy = (enabled: boolean, snapshots: boolean, audio: AccessoryAudio | null, speaker: boolean): CameraPolicy => ({synchronized: true, streaming: enabled,
  live_audio: enabled && audio?.microphone === true && !audio.microphone_muted && audio.microphone_volume > 0,
  snapshots: enabled && snapshots, recording: false, recording_audio: false,
  talkback: enabled && speaker && audio?.speaker === true && !audio.speaker_muted && audio.speaker_volume > 0, controls: false});
const profile = (value: H264Profile): VideoConfiguration["profile"] => {
  if (value === H264Profile.BASELINE) return "baseline";
  if (value === H264Profile.MAIN) return "main";
  if (value === H264Profile.HIGH) return "high";
  throw new ProtocolError("unsupported-video-profile");
};
const level = (value: H264Level): VideoConfiguration["level"] => {
  if (value === H264Level.LEVEL3_1) return "3.1";
  if (value === H264Level.LEVEL3_2) return "3.2";
  if (value === H264Level.LEVEL4_0) return "4.0";
  throw new ProtocolError("unsupported-video-level");
};

/** HAP owns authorization/negotiation; Python owns UDP/SRTP/capture resources. */
export class HapVideoDelegate implements CameraStreamingDelegate {
  private readonly sessions = new Map<string, OwnedVideo>();
  private closed = false;
  private snapshotPolicyChange: Promise<void> | null = null;
  private readonly snapshots = new Set<{abort: AbortController; allowed: () => boolean; finish: (error?: Error | HAPStatus, bytes?: Buffer) => void}>();
  constructor(private readonly native: CameraDelegate, private readonly options: VideoCameraOptions) { }
  get count(): number { return this.sessions.size; }

  private policy(): CameraPolicy {
    if (this.options.policy) return this.options.policy();
    const capabilities = this.native.info.capabilities;
    return streamingPolicy(this.options.active(), capabilities.snapshot, this.options.audio?.() ?? null, capabilities.talkback);
  }

  async synchronizePolicy(): Promise<void> {
    while (!this.closed) {
      const policy = this.policy();
      await this.native.ensurePolicy(policy);
      if (JSON.stringify(policy) === JSON.stringify(this.policy())) return;
    }
  }
  handleSnapshotRequest(request: Parameters<CameraStreamingDelegate["handleSnapshotRequest"]>[0],
    callback: Parameters<CameraStreamingDelegate["handleSnapshotRequest"]>[1]): void {
    const allowed = (): boolean => this.available() && (this.options.snapshotAllowed?.(request.reason) ?? true);
    let supported = false;
    try {supported = allowed() && this.native.info.capabilities.snapshot;} catch {}
    if (!supported || this.snapshots.size >= 2 ||
      !Number.isSafeInteger(request.width) || request.width < 1 || request.width > 4096 ||
      !Number.isSafeInteger(request.height) || request.height < 1 || request.height > 4096 || request.width * request.height > 8_000_000) {
      callback(HAPStatus.SERVICE_COMMUNICATION_FAILURE); return;
    }
    const abort = new AbortController(); let completed = false;
    const job = {abort, allowed, finish: (error?: Error | HAPStatus, bytes?: Buffer): void => {
      if (completed) return; completed = true; clearTimeout(timer); this.snapshots.delete(job); callback(error, bytes);
    }};
    const timer = setTimeout(() => {abort.abort(); job.finish(HAPStatus.OPERATION_TIMED_OUT);}, 20_000);
    this.snapshots.add(job);
    // Only released public enum members have event/periodic meaning. Absence
    // and future/unknown values never become an explicit fresh camera wakeup.
    const reason = request.reason === ResourceRequestReason.PERIODIC ? "periodic" : request.reason === ResourceRequestReason.EVENT ? "event" : "unknown";
    void (async () => {
      await this.snapshotPolicyChange;
      await this.synchronizePolicy();
      if (!allowed() || abort.signal.aborted) throw new ProtocolError("snapshot-retired");
      const scope = this.native.info.scope;
      const bytes = await this.native.snapshot(request.width, request.height, reason, abort.signal);
      if (!allowed() || abort.signal.aborted || JSON.stringify(scope) !== JSON.stringify(this.native.info.scope) ||
        !Buffer.isBuffer(bytes) || bytes.length < 4 || bytes.length > 8 * 1024 * 1024 ||
        bytes[0] !== 0xff || bytes[1] !== 0xd8 || bytes.at(-2) !== 0xff || bytes.at(-1) !== 0xd9) throw new ProtocolError("snapshot-unavailable");
      job.finish(undefined, bytes);
    })().catch(error => {this.report("snapshot", error); job.finish(HAPStatus.SERVICE_COMMUNICATION_FAILURE);});
  }
  private report(stage: string, error: unknown): void {
    const code = error instanceof ProtocolError && diagnosticCodes.has(error.code) ? error.code : "operation-failed";
    // Diagnostics may never change admission, callback completion or cleanup.
    try { this.options.reportError?.(stage, code); } catch {}
  }
  prepareStream(request: PrepareStreamRequest, callback: Parameters<CameraStreamingDelegate["prepareStream"]>[1]): void {
    let owner: OwnedVideo | undefined, completed = false;
    const finish = (error?: unknown, response?: PrepareStreamResponse, quiet = false): void => {
      if (completed) return;
      completed = true; owner?.callbacks.delete(cancel);
      if (error !== undefined) {
        if (!quiet) this.report("video-prepare", error);
        callback(new Error("camera-stream-unavailable"));
      } else callback(undefined, response);
    };
    const cancel = (): void => finish(new ProtocolError("video-retired"), undefined, true);
    void this.prepare(request, session => {owner = session; session.callbacks.add(cancel);}).then(
      response => finish(undefined, response), error => {finish(error); if (owner) this.cancelCallbacks(owner);});
  }
  handleStreamRequest(request: StreamingRequest, callback: Parameters<CameraStreamingDelegate["handleStreamRequest"]>[1]): void {
    const owner = request.type === StreamRequestTypes.STOP ? undefined : this.sessions.get(request.sessionID);
    let completed = false;
    const finish = (error?: unknown, quiet = false): void => {
      if (completed) return;
      completed = true; owner?.callbacks.delete(cancel);
      if (error !== undefined) {
        const stage = request.type === StreamRequestTypes.STOP ? "video-stop" :
          request.type === StreamRequestTypes.RECONFIGURE ? "video-reconfigure" : "video-start";
        if (!quiet) this.report(stage, error);
        callback(new Error("camera-stream-unavailable"));
      } else callback();
    };
    const cancel = (): void => finish(new ProtocolError("video-retired"), true);
    if (owner?.closing) {
      this.cancelCallbacks(owner);
      finish(new ProtocolError("video-retired"));
      return;
    }
    owner?.callbacks.add(cancel);
    void this.handle(request).then(() => finish(), error => {finish(error); if (owner) this.cancelCallbacks(owner);});
  }
  private available(): boolean { return !this.closed && this.options.active(); }
  private current(session: OwnedVideo): void {
    if (!this.available() || session.retired || this.sessions.get(session.hapId) !== session) throw new ProtocolError("video-retired");
  }
  private async prepare(request: PrepareStreamRequest, owned: (session: OwnedVideo) => void): Promise<PrepareStreamResponse> {
    const sourceAddress = mediaSourceAddress(request.sourceAddress, request.addressVersion);
    if (!this.available() || typeof request.sessionID !== "string" || !request.sessionID || request.sessionID.length > 128 ||
        this.sessions.has(request.sessionID) || this.sessions.size >= 2 ||
        !["ipv4", "ipv6"].includes(request.addressVersion) || !literalUnicastAddress(sourceAddress, request.addressVersion) ||
        !literalUnicastAddress(request.targetAddress, request.addressVersion) ||
        request.video.srtpCryptoSuite !== SRTPCryptoSuites.AES_CM_128_HMAC_SHA1_80 ||
        !Buffer.isBuffer(request.video.srtp_key) || request.video.srtp_key.length !== 16 ||
        !Buffer.isBuffer(request.video.srtp_salt) || request.video.srtp_salt.length !== 14 ||
        !Number.isSafeInteger(request.video.port) || request.video.port < 1 || request.video.port > 65535) throw new ProtocolError("invalid-video-prepare");
    const session: OwnedVideo = {hapId: request.sessionID, wireId: randomUUID(), retired: false,
      phase: "preparing", transport: null, config: null, timer: null, closing: null, callbacks: new Set()};
    this.sessions.set(session.hapId, session);
    owned(session);
    const audio = this.options.audio?.();
    const withAudio = audio?.microphone === true || audio?.speaker === true;
    if (withAudio && (!request.audio || request.audio.srtpCryptoSuite !== SRTPCryptoSuites.AES_CM_128_HMAC_SHA1_80 ||
      !Buffer.isBuffer(request.audio.srtp_key) || request.audio.srtp_key.length !== 16 ||
      !Buffer.isBuffer(request.audio.srtp_salt) || request.audio.srtp_salt.length !== 14 ||
      !Number.isSafeInteger(request.audio.port) || request.audio.port < 1 || request.audio.port > 65535)) {
      this.sessions.delete(session.hapId); throw new ProtocolError("invalid-audio-prepare");
    }
    const prepared: VideoPrepare = {source_address: sourceAddress, target_address: request.targetAddress, address_version: request.addressVersion,
      target_port: request.video.port, key: request.video.srtp_key.toString("base64"), salt: request.video.srtp_salt.toString("base64"),
      audio: withAudio ? {target_port: request.audio.port, key: request.audio.srtp_key.toString("base64"), salt: request.audio.srtp_salt.toString("base64")} : null};
    session.timer = setTimeout(() => {
      void this.stop(session).then(() => this.options.forceStop(session.hapId)).catch(() => this.options.failure());
    }, PREPARED_VIDEO_TIMEOUT_MS);
    try {
      await this.synchronizePolicy(); this.current(session);
      const transport = await this.native.prepareVideo(session.wireId, prepared, () => this.ended(session));
      session.transport = transport; this.current(session); session.phase = "prepared";
      const endpoint = transport.endpoint;
      if (!literalUnicastAddress(endpoint.address, prepared.address_version)) throw new ProtocolError("invalid-video-endpoint");
      return {addressOverride: endpoint.address, video: {port: endpoint.port, ssrc: endpoint.ssrc,
        srtp_key: Buffer.from(endpoint.key, "base64"), srtp_salt: Buffer.from(endpoint.salt, "base64")},
        ...(endpoint.audio === null ? {} : {audio: {port: endpoint.audio.port, ssrc: endpoint.audio.ssrc,
          srtp_key: Buffer.from(endpoint.audio.key, "base64"), srtp_salt: Buffer.from(endpoint.audio.salt, "base64")}})};
    } catch (error) { await this.stop(session, false); throw error; }
  }
  private configuration(video: VideoInfo): VideoConfiguration {
    if (video.codec !== VideoCodecType.H264 || video.packetizationMode !== VideoCodecPacketizationMode.NON_INTERLEAVED) {
      throw new ProtocolError("unsupported-video-codec");
    }
    return {width: video.width, height: video.height, fps: video.fps, profile: profile(video.profile), level: level(video.level),
      max_bit_rate: video.max_bit_rate, payload_type: video.pt, rtcp_interval: video.rtcp_interval, mtu: video.mtu};
  }
  private reconfiguration(video: ReconfiguredVideoInfo, current: VideoConfiguration): VideoConfiguration {
    return {...current, width: video.width, height: video.height, fps: video.fps,
      max_bit_rate: video.max_bit_rate, rtcp_interval: video.rtcp_interval};
  }
  private audioConfiguration(audio: AudioInfo): AudioConfiguration {
    if (audio.codec !== AudioStreamingCodecType.OPUS || audio.channel !== 1 ||
      ![8,16,24].includes(audio.sample_rate) || ![20,30,40,60].includes(audio.packet_time) ||
      ![AudioBitrate.CONSTANT, AudioBitrate.VARIABLE].includes(audio.bit_rate) || audio.comfortNoiseEnabled !== false ||
      !Number.isSafeInteger(audio.comfort_pt) || audio.comfort_pt < 0 || audio.comfort_pt > 127) throw new ProtocolError("unsupported-audio-format");
    return {sample_rate: audio.sample_rate * 1000 as AudioConfiguration["sample_rate"], packet_time: audio.packet_time as AudioConfiguration["packet_time"],
      bitrate_mode: audio.bit_rate === AudioBitrate.CONSTANT ? "cbr" : "vbr", max_bit_rate: audio.max_bit_rate,
      payload_type: audio.pt, rtcp_interval: audio.rtcp_interval, channels: 1, clock_mode: "classic_hap"};
  }
  private async handle(request: StreamingRequest): Promise<void> {
    const session = this.sessions.get(request.sessionID);
    if (request.type === StreamRequestTypes.STOP) { if (session) await this.stop(session); return; }
    if (!session) throw new ProtocolError("video-not-prepared");
    try {
      this.current(session);
      const transport = session.transport;
      if (!transport) throw new ProtocolError("video-not-prepared");
      if (request.type === StreamRequestTypes.START) {
        if (session.phase !== "prepared" || !uint32(request.video.ssrc)) throw new ProtocolError("video-start-conflict");
        // SelectedRTP contains the controller's independently selected SSRCs.
        // Python retains the server SSRCs returned by SetupEndpoints; selected
        // values neither replace those sender identities nor authorize media.
        const config = this.configuration(request.video);
        if (transport.endpoint.audio !== null && !uint32(request.audio.ssrc)) throw new ProtocolError("audio-start-conflict");
        const audio = transport.endpoint.audio === null ? null : this.audioConfiguration(request.audio);
        session.phase = "starting";
        await transport.start(config, audio); this.current(session); session.phase = "active"; session.config = config;
        if (session.timer) clearTimeout(session.timer); session.timer = null;
      } else if (request.type === StreamRequestTypes.RECONFIGURE) {
        if (session.phase !== "active" || !session.config) throw new ProtocolError("video-reconfigure-conflict");
        const config = this.reconfiguration(request.video, session.config); session.phase = "reconfiguring";
        await transport.reconfigure(config); this.current(session); session.phase = "active"; session.config = config;
      } else throw new ProtocolError("unsupported-video-operation");
    } catch (error) {
      // HAP resets its stream management session after a callback error and
      // does not send a compensating STOP. Retire this exact owner first,
      // including validation/phase failures before any native START request.
      await this.stop(session, false);
      throw error;
    }
  }
  private cancelCallbacks(session: OwnedVideo): void {
    for (const cancel of [...session.callbacks]) cancel();
  }
  private stop(session: OwnedVideo, cancelPending = true): Promise<void> {
    // Public HAP END resets its slot before invoking STOP. Settle this owner's
    // callbacks synchronously so their late native replies cannot reset a new
    // slot. Error-driven cleanup retains its callback until native STOP joins.
    if (cancelPending) this.cancelCallbacks(session);
    if (session.closing) return session.closing;
    session.retired = true;
    if (session.timer) clearTimeout(session.timer); session.timer = null;
    session.closing = this.native.stopVideo(session.wireId).then(() => {
      if (this.sessions.get(session.hapId) === session) this.sessions.delete(session.hapId);
    }).catch(error => { this.options.failure(); throw error; });
    return session.closing;
  }
  private ended(session: OwnedVideo): void {
    if (this.sessions.get(session.hapId) !== session) return;
    const notify = !session.retired;
    session.retired = true;
    this.cancelCallbacks(session);
    if (session.timer) clearTimeout(session.timer); session.timer = null;
    this.sessions.delete(session.hapId); if (notify) this.options.forceStop(session.hapId);
  }
  /** Synchronous local retirement precedes persistence, cleanup, and policy awaits. */
  fence(): Promise<void> {
    for (const job of [...this.snapshots]) {job.abort.abort(); job.finish(HAPStatus.SERVICE_COMMUNICATION_FAILURE);}
    const retiring = [...this.sessions.values()];
    for (const session of retiring) { session.retired = true; this.options.forceStop(session.hapId); }
    return Promise.all(retiring.map(session => this.stop(session))).then(() => {});
  }
  async activeChanged(): Promise<void> {
    if (!this.options.active()) await this.fence();
    if (!this.closed) await this.synchronizePolicy();
  }
  snapshotPolicyChanged(): Promise<void> {
    // The wire policy has one aggregate snapshot permission. Cross a denied
    // parent epoch even if the other reason stays enabled, so no cached image
    // from before this privacy change can be reused after a quick off/on.
    for (const job of [...this.snapshots]) {
      job.abort.abort(); job.finish(HAPStatus.SERVICE_COMMUNICATION_FAILURE);
    }
    const changing = (this.snapshotPolicyChange ?? Promise.resolve()).then(async () => {
      if (this.closed) return;
      await this.native.ensurePolicy({...this.policy(), snapshots: false});
      if (!this.closed) await this.synchronizePolicy();
    });
    this.snapshotPolicyChange = changing;
    void changing.then(() => {
      if (this.snapshotPolicyChange === changing) this.snapshotPolicyChange = null;
    }, () => {});
    return changing;
  }
  async close(): Promise<void> {
    this.closed = true;
    await this.fence();
  }
}
