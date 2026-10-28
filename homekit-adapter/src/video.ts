import { randomUUID } from "node:crypto";
import { BlockList, isIP } from "node:net";
import {
  H264Level, H264Profile, HAPStatus, SRTPCryptoSuites, StreamRequestTypes,
  VideoCodecPacketizationMode, VideoCodecType, type CameraStreamingDelegate,
  type PrepareStreamRequest, type PrepareStreamResponse, type ReconfiguredVideoInfo,
  type StreamingRequest, type VideoInfo,
} from "@homebridge/hap-nodejs";
import type { CameraPolicy, VideoConfiguration } from "./protocol.js";
import { ProtocolError } from "./protocol.js";
import type { CameraDelegate, VideoDelegate } from "./session.js";

export const PREPARED_VIDEO_TIMEOUT_MS = 10_000;
interface OwnedVideo {
  hapId: string; wireId: string; retired: boolean; phase: "preparing" | "prepared" | "starting" | "active" | "reconfiguring";
  transport: VideoDelegate | null; config: VideoConfiguration | null; timer: NodeJS.Timeout | null; closing: Promise<void> | null;
}
export interface VideoCameraOptions {
  active: () => boolean;
  forceStop: (hapId: string) => void;
  failure: () => void;
}
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
const streamingPolicy = (enabled: boolean): CameraPolicy => ({synchronized: true, streaming: enabled,
  live_audio: false, snapshots: false, recording: false, recording_audio: false, talkback: false, controls: false});
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
  constructor(private readonly native: CameraDelegate, private readonly options: VideoCameraOptions) { }
  get count(): number { return this.sessions.size; }

  async synchronizePolicy(): Promise<void> {
    while (!this.closed) {
      const enabled = this.options.active();
      await this.native.ensurePolicy(streamingPolicy(enabled));
      if (enabled === this.options.active()) return;
    }
  }
  handleSnapshotRequest(_request: Parameters<CameraStreamingDelegate["handleSnapshotRequest"]>[0],
    callback: Parameters<CameraStreamingDelegate["handleSnapshotRequest"]>[1]): void { callback(HAPStatus.SERVICE_COMMUNICATION_FAILURE); }
  prepareStream(request: PrepareStreamRequest, callback: Parameters<CameraStreamingDelegate["prepareStream"]>[1]): void {
    void this.prepare(request).then(response => callback(undefined, response), () => callback(new Error("camera-stream-unavailable")));
  }
  handleStreamRequest(request: StreamingRequest, callback: Parameters<CameraStreamingDelegate["handleStreamRequest"]>[1]): void {
    void this.handle(request).then(() => callback(), () => callback(new Error("camera-stream-unavailable")));
  }
  private available(): boolean { return !this.closed && this.options.active(); }
  private current(session: OwnedVideo): void {
    if (!this.available() || session.retired || this.sessions.get(session.hapId) !== session) throw new ProtocolError("video-retired");
  }
  private async prepare(request: PrepareStreamRequest): Promise<PrepareStreamResponse> {
    if (!this.available() || typeof request.sessionID !== "string" || !request.sessionID || request.sessionID.length > 128 ||
        this.sessions.has(request.sessionID) || this.sessions.size >= 2 ||
        !["ipv4", "ipv6"].includes(request.addressVersion) || !literalUnicastAddress(request.sourceAddress, request.addressVersion) ||
        !literalUnicastAddress(request.targetAddress, request.addressVersion) ||
        request.video.srtpCryptoSuite !== SRTPCryptoSuites.AES_CM_128_HMAC_SHA1_80 ||
        !Buffer.isBuffer(request.video.srtp_key) || request.video.srtp_key.length !== 16 ||
        !Buffer.isBuffer(request.video.srtp_salt) || request.video.srtp_salt.length !== 14 ||
        !Number.isSafeInteger(request.video.port) || request.video.port < 1 || request.video.port > 65535) throw new ProtocolError("invalid-video-prepare");
    const session: OwnedVideo = {hapId: request.sessionID, wireId: randomUUID(), retired: false,
      phase: "preparing", transport: null, config: null, timer: null, closing: null};
    this.sessions.set(session.hapId, session);
    const prepared = {source_address: request.sourceAddress, target_address: request.targetAddress, address_version: request.addressVersion,
      target_port: request.video.port, key: request.video.srtp_key.toString("base64"), salt: request.video.srtp_salt.toString("base64")};
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
        srtp_key: Buffer.from(endpoint.key, "base64"), srtp_salt: Buffer.from(endpoint.salt, "base64")}};
    } catch (error) { await this.stop(session); throw error; }
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
  private async handle(request: StreamingRequest): Promise<void> {
    const session = this.sessions.get(request.sessionID);
    if (request.type === StreamRequestTypes.STOP) { if (session) await this.stop(session); return; }
    if (!session) throw new ProtocolError("video-not-prepared");
    this.current(session);
    const transport = session.transport;
    if (!transport) throw new ProtocolError("video-not-prepared");
    if (request.type === StreamRequestTypes.START) {
      if (session.phase !== "prepared" || request.video.ssrc !== transport.endpoint.ssrc) throw new ProtocolError("video-start-conflict");
      const config = this.configuration(request.video);
      session.phase = "starting";
      try {
        await transport.start(config); this.current(session); session.phase = "active"; session.config = config;
        if (session.timer) clearTimeout(session.timer); session.timer = null;
      } catch (error) { await this.stop(session); throw error; }
    } else if (request.type === StreamRequestTypes.RECONFIGURE) {
      if (session.phase !== "active" || !session.config) throw new ProtocolError("video-reconfigure-conflict");
      const config = this.reconfiguration(request.video, session.config); session.phase = "reconfiguring";
      try { await transport.reconfigure(config); this.current(session); session.phase = "active"; session.config = config; }
      catch (error) { await this.stop(session); throw error; }
    } else throw new ProtocolError("unsupported-video-operation");
  }
  private stop(session: OwnedVideo): Promise<void> {
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
    session.retired = true;
    if (session.timer) clearTimeout(session.timer); session.timer = null;
    this.sessions.delete(session.hapId); this.options.forceStop(session.hapId);
  }
  async activeChanged(): Promise<void> {
    if (!this.options.active()) {
      const retiring = [...this.sessions.values()];
      for (const session of retiring) { session.retired = true; this.options.forceStop(session.hapId); }
      await Promise.all(retiring.map(session => this.stop(session)));
    }
    if (!this.closed) await this.synchronizePolicy();
  }
  async close(): Promise<void> {
    this.closed = true;
    const retiring = [...this.sessions.values()];
    for (const session of retiring) { session.retired = true; this.options.forceStop(session.hapId); }
    await Promise.all(retiring.map(session => this.stop(session)));
  }
}
