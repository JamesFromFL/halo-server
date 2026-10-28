import { randomUUID } from "node:crypto";
import type { BulkChannel } from "./bulk.js";
import {
  BULK_LIMITS, MAX_INVENTORY_DEVICES, MAX_INVENTORY_PAGE, ProtocolError, REPLY_KINDS,
  validateDirection, validateReply, type AccountScope, type CameraPolicy, type Envelope,
  type JsonObject, type Kind, type MediaScope, type VideoConfiguration, type VideoPrepare, type VideoEndpoint,
} from "./protocol.js";

export const MAX_SESSION_REQUESTS = 16;
export const REQUEST_TIMEOUT_MS = 30_000;
const MAX_SNAPSHOT_REQUESTS = 2;
const MAX_LIVE_SESSIONS = 8;

export interface NativeCamera {
  scope: MediaScope;
  name: string; family: string; kind: string; model: string;
  capabilities: {video: boolean; audio: boolean; snapshot: boolean; talkback: boolean;
    light: boolean; siren: boolean; doorbell: boolean; battery: boolean; evidence: string[]};
  connection_state: boolean | null; battery_percent: number | null;
  observed_format: JsonObject | null;
}
export interface SessionOptions {
  send: (message: Envelope) => Promise<void>;
  bulk?: BulkChannel;
  requestTimeoutMs?: number;
  maximumPendingRequests?: number;
  onFailure?: (error: ProtocolError) => void;
  onInventory?: () => void;
  onInvalidate?: () => void;
}
interface Pending {
  request: Envelope; timer: NodeJS.Timeout;
  resolve: (message: Envelope) => void; reject: (error: Error) => void;
  observe?: (message: Envelope) => void;
}
interface CameraEntry { camera: NativeCamera; synchronized: boolean; policy: CameraPolicy | null }
interface Resource { scope: MediaScope; expires: number }
interface VideoResource { scope: MediaScope; retired: boolean; endpoint: VideoEndpoint | null;
  ended: () => void; stopping: Promise<void> | null }
interface SnapshotJob {
  scope: MediaScope; transferId: string | null; cancelled: boolean;
  resolve: (value: Buffer) => void; reject: (error: Error) => void;
  result: Promise<Buffer>; timer: NodeJS.Timeout;
}
const sourceKey = (scope: MediaScope): string => JSON.stringify([
  scope.profile_id, scope.account_generation, scope.camera_id, scope.device_generation,
]);
const sameAccount = (left: AccountScope | null, right: AccountScope | null): boolean =>
  left !== null && right !== null && left.profile_id === right.profile_id &&
  left.account_generation === right.account_generation;
const sameScope = (left: MediaScope, right: MediaScope): boolean =>
  sourceKey(left) === sourceKey(right) && left.privacy_generation === right.privacy_generation;

/** Private camera delegates only. No HAP accessory, storage or network publication is created. */
export class AdapterSession {
  private readonly pending = new Map<string, Pending>();
  private readonly entries = new Map<string, CameraEntry>();
  private readonly live = new Map<string, Resource>();
  private readonly speakers = new Map<string, Resource>();
  private readonly snapshots = new Map<string, SnapshotJob>();
  private readonly policyChanges = new Set<string>();
  private readonly policySynchronization = new Map<string, Promise<void>>();
  private readonly videos = new Map<string, VideoResource>();
  private account: AccountScope | null = null;
  private revision = 0;
  private noticeEpoch = 0;
  private complete = false;
  private closed = false;
  private refreshing: Promise<void> | null = null;
  private refreshAgain = false;
  private readonly timeout: number;
  private readonly maximumPending: number;

  constructor(readonly bootId: string, private readonly options: SessionOptions) {
    // Validate even synthetic entry points before accepting an authority token.
    validateDirection({v: 1, kind: "inventory_reset", boot_id: bootId,
      request_id: randomUUID(), payload: {scope: null, revision: 0}}, "python");
    this.timeout = options.requestTimeoutMs ?? REQUEST_TIMEOUT_MS;
    this.maximumPending = options.maximumPendingRequests ?? MAX_SESSION_REQUESTS;
    if (!Number.isSafeInteger(this.timeout) || this.timeout < 1 || this.timeout > REQUEST_TIMEOUT_MS ||
        !Number.isSafeInteger(this.maximumPending) || this.maximumPending < 1 || this.maximumPending > MAX_SESSION_REQUESTS) {
      throw new ProtocolError("invalid-session-options");
    }
  }

  get isClosed(): boolean { return this.closed; }
  get pendingRequests(): number { return this.pending.size; }
  get inventoryComplete(): boolean { return this.complete; }
  get inventoryRevision(): number { return this.revision; }
  get accountScope(): AccountScope | null { return this.account === null ? null : {...this.account}; }
  synchronizeEvents(scope: AccountScope | null, epoch: string | null, sequence: number): Promise<JsonObject> {
    return this.request("event_sync", {scope, epoch, sequence}).then(reply => reply.payload);
  }
  cameras(): NativeCamera[] { return [...this.entries.values()].map(entry => structuredClone(entry.camera)); }
  camera(cameraId: string): CameraDelegate {
    const entry = this.entries.get(cameraId);
    if (!entry || !this.complete || this.closed) throw new ProtocolError("camera-not-ready");
    return new CameraDelegate(this, cameraId, sourceKey(entry.camera.scope));
  }

  /** Return false only for an unowned late result; it cannot create a resource. */
  async receive(value: Envelope): Promise<boolean> {
    if (this.closed) return false;
    const message = validateDirection(value, "python");
    const pending = this.pending.get(message.request_id);
    if (message.boot_id !== this.bootId) {
      if (pending) this.fail(new ProtocolError("invalid-reply-correlation"));
      return false;
    }
    if (message.kind === "inventory_reset") {
      if (this.complete && sameAccount(this.account, message.payload.scope as AccountScope | null) &&
          this.revision === message.payload.revision) return true;
      this.noticeEpoch += 1;
      this.account = structuredClone(message.payload.scope) as AccountScope | null;
      this.revision = message.payload.revision as number;
      this.invalidate();
      if (this.account !== null) void this.refresh().catch(() => {});
      return true;
    }
    if (message.kind === "camera_status") {
      const scope = message.payload.scope as MediaScope;
      const entry = this.entries.get(scope.camera_id);
      if (this.complete && entry && sameScope(entry.camera.scope, scope) &&
          message.payload.revision === this.revision) {
        entry.camera.connection_state = message.payload.state === "unknown" ? null : message.payload.state === "online";
      }
      return true;
    }
    if (message.kind === "video_ended") {
      const resource = this.videos.get(message.payload.session_id as string);
      if (resource && sameScope(resource.scope, message.payload.scope as MediaScope)) {
        resource.retired = true; this.videos.delete(message.payload.session_id as string); resource.ended();
      }
      return true;
    }
    if (message.kind.startsWith("transfer_")) {
      if (!this.options.bulk) throw this.fail(new ProtocolError("bulk-unavailable"));
      if (message.kind === "transfer_open") this.authorizeSnapshot(message);
      return this.options.bulk.handleControl(message);
    }
    if (!pending) return false;
    if (message.kind === "error") {
      // A correlated negative inventory reply carries no authority or arbitrary error text.
      if (pending.request.kind !== "inventory_request") throw this.fail(new ProtocolError("unexpected-error-reply"));
      this.retirePending(message.request_id);
      pending.reject(new ProtocolError("inventory-unavailable"));
      return true;
    }
    try {
      validateReply(pending.request, message);
      pending.observe?.(message);
    } catch (error) {
      throw this.fail(error instanceof ProtocolError ? error : new ProtocolError("invalid-reply"));
    }
    this.retirePending(message.request_id);
    pending.resolve(message);
    return true;
  }

  async refresh(): Promise<void> {
    if (this.closed || this.account === null) throw new ProtocolError("inventory-unavailable");
    this.refreshAgain = true;
    if (this.refreshing) return this.refreshing;
    this.refreshing = (async () => {
      while (this.refreshAgain && !this.closed && this.account !== null) {
        this.refreshAgain = false;
        const epoch = this.noticeEpoch, account = {...this.account};
        try { await this.fetchInventory(account, epoch); }
        catch (error) {
          if (epoch !== this.noticeEpoch && this.account !== null) { this.refreshAgain = true; continue; }
          this.complete = false;
          throw error;
        }
      }
    })().finally(() => { this.refreshing = null; });
    return this.refreshing;
  }

  private async fetchInventory(account: AccountScope, epoch: number): Promise<void> {
    const collected: NativeCamera[] = [], cursors = new Set<string>();
    let cursor: string | null = null, revision = 0, total: number | null = null, complete: boolean | null = null;
    for (let pageCount = 0; pageCount <= MAX_INVENTORY_DEVICES; pageCount++) {
      const reply = await this.request("inventory_request", {scope: account, revision, cursor});
      if (this.closed || epoch !== this.noticeEpoch || !sameAccount(account, this.account)) return;
      const page = reply.payload;
      if (total === null) { total = page.total as number; complete = page.complete as boolean; revision = page.revision as number; }
      if (page.total !== total || page.complete !== complete || page.revision !== revision ||
          !sameAccount(page.scope as AccountScope, account)) throw this.fail(new ProtocolError("invalid-inventory-pagination"));
      const devices = page.devices as NativeCamera[];
      if (devices.length > MAX_INVENTORY_PAGE || collected.length + devices.length > total) {
        throw this.fail(new ProtocolError("invalid-inventory-pagination"));
      }
      const names = new Set(collected.map(camera => camera.scope.camera_id));
      for (const camera of devices) {
        if (names.has(camera.scope.camera_id)) throw this.fail(new ProtocolError("duplicate-inventory-camera"));
        names.add(camera.scope.camera_id); collected.push(structuredClone(camera));
      }
      const next = page.next_cursor as string | null;
      if (next === null) {
        if (collected.length !== total) throw this.fail(new ProtocolError("invalid-inventory-total"));
        const previous = new Map(this.entries);
        this.entries.clear();
        for (const camera of collected) {
          const old = previous.get(camera.scope.camera_id);
          const unchanged = old !== undefined && sameScope(old.camera.scope, camera.scope);
          this.entries.set(camera.scope.camera_id, {camera, synchronized: Boolean(complete && unchanged && old.synchronized),
            policy: unchanged ? old.policy : null});
        }
        this.revision = revision; this.complete = complete === true;
        if (this.complete) for (const [id, video] of this.videos) {
          const current = this.entries.get(video.scope.camera_id);
          if (!current || !sameScope(current.camera.scope, video.scope)) this.retireVideo(id, video);
        }
        this.options.onInventory?.();
        return;
      }
      if (cursors.has(next) || devices.length === 0 || collected.length >= total) {
        throw this.fail(new ProtocolError("invalid-inventory-cursor"));
      }
      cursors.add(next); cursor = next;
    }
    throw this.fail(new ProtocolError("inventory-page-capacity"));
  }

  private request(kind: Kind, payload: JsonObject, observe?: Pending["observe"]): Promise<Envelope> {
    if (this.closed) return Promise.reject(new ProtocolError("session-closed"));
    if (!REPLY_KINDS[kind]) return Promise.reject(new ProtocolError("request-not-supported"));
    if (this.pending.size >= this.maximumPending) return Promise.reject(new ProtocolError("request-capacity"));
    const message = structuredClone(validateDirection({v: 1, kind, boot_id: this.bootId, request_id: randomUUID(), payload}, "hap"));
    return new Promise<Envelope>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.retirePending(message.request_id);
        reject(new ProtocolError("request-timeout"));
      }, this.timeout);
      const item: Pending = {request: message, timer, resolve, reject};
      if (observe) item.observe = observe;
      this.pending.set(message.request_id, item);
      void this.options.send(structuredClone(message)).catch(() => {
        if (this.pending.get(message.request_id) !== item) return;
        this.retirePending(message.request_id); reject(new ProtocolError("control-send-failed"));
      });
    });
  }
  private retirePending(id: string): void {
    const item = this.pending.get(id);
    if (item) clearTimeout(item.timer);
    this.pending.delete(id);
  }
  private entry(cameraId: string, identity: string, permission?: keyof CameraPolicy): CameraEntry {
    const entry = this.entries.get(cameraId);
    if (this.closed || !this.complete || !entry || sourceKey(entry.camera.scope) !== identity) throw new ProtocolError("stale-camera");
    if (permission && (!entry.synchronized || !entry.policy?.synchronized || !entry.policy[permission])) throw new ProtocolError("policy-denied");
    return entry;
  }
  cameraInfo(cameraId: string, identity: string): NativeCamera { return structuredClone(this.entry(cameraId, identity).camera); }
  cameraReady(cameraId: string, identity: string): boolean {
    try { return this.entry(cameraId, identity).synchronized; } catch { return false; }
  }

  async updatePolicy(cameraId: string, identity: string, policy: CameraPolicy): Promise<void> {
    const entry = this.entry(cameraId, identity);
    if (this.policyChanges.has(identity)) throw new ProtocolError("policy-update-busy");
    const scope = {...entry.camera.scope};
    policy = structuredClone(policy);
    // Full replacement and validation occur before any request can escape.
    validateDirection({v: 1, kind: "policy_update", boot_id: this.bootId,
      request_id: randomUUID(), payload: {scope, policy}}, "hap");
    this.policyChanges.add(identity);
    entry.synchronized = false; entry.policy = null;
    this.retireCamera(scope);
    try {
      const reply = await this.request("policy_update", {scope, policy});
      if (reply.payload.accepted !== true || reply.payload.code !== "ok") throw new ProtocolError("policy-rejected");
      await this.refresh();
      const current = this.entry(cameraId, identity);
      current.policy = structuredClone(policy); current.synchronized = policy.synchronized;
    } finally { this.policyChanges.delete(identity); }
  }

  async ensurePolicy(cameraId: string, identity: string, policy: CameraPolicy): Promise<void> {
    const pending = this.policySynchronization.get(identity);
    if (pending) await pending;
    const entry = this.entry(cameraId, identity);
    if (entry.synchronized && entry.policy && Object.keys(policy).every(name =>
      entry.policy![name as keyof CameraPolicy] === policy[name as keyof CameraPolicy])) return;
    const operation = this.updatePolicy(cameraId, identity, policy);
    this.policySynchronization.set(identity, operation);
    try { await operation; }
    finally { if (this.policySynchronization.get(identity) === operation) this.policySynchronization.delete(identity); }
  }

  async prepareVideo(cameraId: string, identity: string, sessionId: string, options: VideoPrepare, ended: () => void): Promise<VideoDelegate> {
    const scope = {...this.entry(cameraId, identity, "streaming").camera.scope};
    if (this.videos.has(sessionId) || this.videos.size + this.live.size >= MAX_LIVE_SESSIONS ||
        [...this.videos.values()].filter(video => video.scope.camera_id === cameraId).length >= 2) throw new ProtocolError("video-capacity");
    const resource: VideoResource = {scope, retired: false, endpoint: null, ended, stopping: null};
    this.videos.set(sessionId, resource);
    try {
      const reply = await this.request("video_prepare", {scope, session_id: sessionId, ...options});
      this.video(sessionId);
      if (reply.payload.status !== "accepted" || reply.payload.code !== "ok") throw new ProtocolError("video-prepare-rejected");
      const endpoint = structuredClone(reply.payload.endpoint as VideoEndpoint);
      if (endpoint.key !== options.key || endpoint.salt !== options.salt) throw new ProtocolError("video-endpoint-mismatch");
      resource.endpoint = endpoint;
      return new VideoDelegate(this, sessionId, endpoint);
    } catch (error) {
      await this.stopVideo(sessionId);
      throw error;
    }
  }
  private video(sessionId: string): VideoResource {
    const resource = this.videos.get(sessionId);
    if (!resource || resource.retired) throw new ProtocolError("video-retired");
    const current = this.entry(resource.scope.camera_id, sourceKey(resource.scope), "streaming");
    if (!sameScope(current.camera.scope, resource.scope)) throw new ProtocolError("video-retired");
    return resource;
  }
  async startVideo(sessionId: string, config: VideoConfiguration, reconfigure = false): Promise<void> {
    const resource = this.video(sessionId);
    const reply = await this.request(reconfigure ? "video_reconfigure" : "video_start", {scope: resource.scope, session_id: sessionId, config});
    this.video(sessionId);
    if (reply.payload.status !== "accepted" || reply.payload.code !== "ok") throw new ProtocolError("video-configuration-rejected");
  }
  stopVideo(sessionId: string): Promise<void> {
    const resource = this.videos.get(sessionId);
    if (!resource) return Promise.resolve();
    if (resource.stopping) return resource.stopping;
    resource.retired = true;
    resource.stopping = this.request("video_stop", {scope: resource.scope, session_id: sessionId}).then(reply => {
      if (reply.payload.status !== "accepted" || reply.payload.code !== "ok") throw new ProtocolError("video-stop-rejected");
      if (this.videos.get(sessionId) === resource) this.videos.delete(sessionId);
    }).catch(error => { throw this.fail(error instanceof ProtocolError ? error : new ProtocolError("video-cleanup-failed")); });
    return resource.stopping;
  }
  private retireVideo(id: string, video: VideoResource): void {
    video.retired = true; video.ended();
    void this.stopVideo(id).catch(() => {});
  }

  async acquireLive(cameraId: string, identity: string, ttlMs = 30_000): Promise<LiveDelegate> {
    const entry = this.entry(cameraId, identity, "streaming"), scope = {...entry.camera.scope};
    if (this.live.size + this.videos.size >= MAX_LIVE_SESSIONS) throw new ProtocolError("live-capacity");
    const sessionId = randomUUID(); this.live.set(sessionId, {scope, expires: 0});
    try {
      const reply = await this.request("lease_request", {scope, session_id: sessionId, purpose: "homekit_live", ttl_ms: ttlMs});
      this.verifyResource(this.live, sessionId, "streaming", false);
      if (reply.payload.status !== "accepted" || reply.payload.code !== "ok" || (reply.payload.ttl_ms as number) > ttlMs) throw new ProtocolError("lease-rejected");
      this.live.set(sessionId, {scope, expires: performance.now() + (reply.payload.ttl_ms as number)});
      return new LiveDelegate(this, sessionId);
    } catch (error) { this.live.delete(sessionId); throw error; }
  }
  async renewLive(sessionId: string, ttlMs = 30_000): Promise<void> {
    const resource = this.verifyResource(this.live, sessionId, "streaming");
    const reply = await this.request("lease_renew", {scope: resource.scope, session_id: sessionId, ttl_ms: ttlMs});
    this.verifyResource(this.live, sessionId, "streaming");
    if (reply.payload.status !== "accepted" || reply.payload.code !== "ok" || (reply.payload.ttl_ms as number) > ttlMs) {
      this.live.delete(sessionId); throw new ProtocolError("lease-rejected");
    }
    resource.expires = performance.now() + (reply.payload.ttl_ms as number);
  }
  async stopLive(sessionId: string): Promise<void> {
    const resource = this.live.get(sessionId);
    if (!resource) return;
    this.live.delete(sessionId);
    const reply = await this.request("lease_stop", {scope: resource.scope, session_id: sessionId});
    if (reply.payload.status !== "stopped" || reply.payload.code !== "ok") throw new ProtocolError("lease-stop-rejected");
  }
  private verifyResource(resources: Map<string, Resource>, id: string, permission: keyof CameraPolicy, checkExpiry = true): Resource {
    const resource = resources.get(id);
    if (!resource) throw new ProtocolError("resource-retired");
    const entry = this.entry(resource.scope.camera_id, sourceKey(resource.scope), permission);
    if (!sameScope(entry.camera.scope, resource.scope) || (checkExpiry && resource.expires <= performance.now())) {
      resources.delete(id); throw new ProtocolError("resource-retired");
    }
    return resource;
  }

  async setControl(cameraId: string, identity: string, control: "light" | "siren", enabled: boolean): Promise<"accepted" | "unknown"> {
    const scope = {...this.entry(cameraId, identity, "controls").camera.scope};
    const reply = await this.request("control_request", {scope, operation_id: randomUUID(), control, enabled});
    if (!sameScope(this.entry(cameraId, identity, "controls").camera.scope, scope)) throw new ProtocolError("resource-retired");
    if (reply.payload.status === "rejected" || (reply.payload.status === "accepted" && reply.payload.code !== "ok")) throw new ProtocolError("control-rejected");
    return reply.payload.status as "accepted" | "unknown";
  }

  async snapshot(cameraId: string, identity: string, width: number, height: number,
    reason: "periodic" | "event" | "explicit" = "explicit"): Promise<Buffer> {
    const scope = {...this.entry(cameraId, identity, "snapshots").camera.scope};
    if (this.snapshots.size >= MAX_SNAPSHOT_REQUESTS) throw new ProtocolError("snapshot-capacity");
    const jobId = randomUUID();
    let resolve!: (value: Buffer) => void, reject!: (error: Error) => void;
    const result = new Promise<Buffer>((ok, no) => { resolve = ok; reject = no; });
    // The rejection can precede the operation reply; keep it observed until awaited.
    void result.catch(() => {});
    const timer = setTimeout(() => this.cancelSnapshot(jobId, "snapshot-timeout"), this.timeout);
    const job: SnapshotJob = {scope, transferId: null, cancelled: false, result, resolve, reject, timer};
    this.snapshots.set(jobId, job);
    try {
      const reply = await this.request("snapshot_request", {scope, job_id: jobId, width, height, reason}, message => {
        if (message.payload.status === "ready" && message.payload.code === "ok") job.transferId = message.payload.transfer_id as string;
      });
      if (reply.payload.status !== "ready" || reply.payload.code !== "ok") throw new ProtocolError("snapshot-rejected");
      const bytes = await result;
      if (!sameScope(this.entry(cameraId, identity, "snapshots").camera.scope, scope)) throw new ProtocolError("resource-retired");
      return bytes;
    } finally { clearTimeout(timer); this.snapshots.delete(jobId); }
  }
  private authorizeSnapshot(message: Envelope): void {
    const payload = message.payload, job = this.snapshots.get(payload.operation_id as string);
    if (!job || job.cancelled || job.transferId !== payload.transfer_id || payload.kind !== "snapshot" ||
        !sameScope(job.scope, payload.scope as MediaScope) || !this.options.bulk ||
        (payload.total_bytes as number) > BULK_LIMITS.snapshot) throw this.fail(new ProtocolError("unsolicited-transfer"));
    const chunks: Buffer[] = []; let length = 0;
    const valid = (): boolean => {
      try { return !job.cancelled && this.snapshots.get(payload.operation_id as string) === job &&
        sameScope(this.entry(job.scope.camera_id, sourceKey(job.scope), "snapshots").camera.scope, job.scope); }
      catch { return false; }
    };
    if (!valid()) throw this.fail(new ProtocolError("stale-transfer"));
    const receiver = this.options.bulk.authorizeReceive({scope: job.scope, operationId: payload.operation_id as string,
      transferId: payload.transfer_id as string, kind: "snapshot", totalBytes: payload.total_bytes as number, valid,
      sink: async chunk => {
        length += chunk.length;
        if (length > BULK_LIMITS.snapshot) throw new ProtocolError("snapshot-capacity");
        chunks.push(Buffer.from(chunk));
      }, onClose: async code => { if (code !== "ok") job.reject(new ProtocolError("snapshot-transfer-failed")); }});
    void receiver.wait().then(() => {
      if (valid()) job.resolve(Buffer.concat(chunks, length));
      else job.reject(new ProtocolError("resource-retired"));
    }).catch(() => job.reject(new ProtocolError("snapshot-transfer-failed")));
  }
  private cancelSnapshot(id: string, code: string): void {
    const job = this.snapshots.get(id);
    if (!job) return;
    job.cancelled = true; clearTimeout(job.timer); job.reject(new ProtocolError(code));
    this.snapshots.delete(id);
    void this.sendNotice("snapshot_cancel", {scope: job.scope, job_id: id}).catch(() => {});
  }

  async acquireTalkback(cameraId: string, identity: string): Promise<TalkbackDelegate> {
    const scope = {...this.entry(cameraId, identity, "talkback").camera.scope};
    if (this.speakers.size) throw new ProtocolError("speaker-capacity");
    const sessionId = randomUUID(); this.speakers.set(sessionId, {scope, expires: Number.POSITIVE_INFINITY});
    try {
      const reply = await this.request("talkback_request", {scope, session_id: sessionId, action: "acquire"});
      this.verifyResource(this.speakers, sessionId, "talkback");
      if (reply.payload.status !== "accepted" || reply.payload.code !== "ok") throw new ProtocolError("talkback-rejected");
      return new TalkbackDelegate(this, sessionId);
    } catch (error) { this.speakers.delete(sessionId); throw error; }
  }
  async stopTalkback(sessionId: string): Promise<void> {
    const resource = this.speakers.get(sessionId);
    if (!resource) return;
    this.speakers.delete(sessionId);
    const reply = await this.request("talkback_request", {scope: resource.scope, session_id: sessionId, action: "stop"});
    if (reply.payload.status !== "stopped" || reply.payload.code !== "ok") throw new ProtocolError("talkback-stop-rejected");
  }
  async sendPcm(sessionId: string, pcm: Buffer): Promise<void> {
    const resource = this.verifyResource(this.speakers, sessionId, "talkback");
    if (!Buffer.isBuffer(pcm) || pcm.length < 2 || pcm.length > BULK_LIMITS.talkback_pcm || pcm.length % 2) throw new ProtocolError("invalid-pcm-frame");
    if (!this.options.bulk) throw new ProtocolError("bulk-unavailable");
    const frozen = Buffer.from(pcm), scope = {...resource.scope};
    await this.options.bulk.send({scope, operationId: sessionId, transferId: randomUUID(), kind: "talkback_pcm", totalBytes: frozen.length,
      source: (async function* () { yield frozen; })(), valid: () => {
        try { return sameScope(this.verifyResource(this.speakers, sessionId, "talkback").scope, scope); }
        catch { return false; }
      }});
  }
  private async sendNotice(kind: Kind, payload: JsonObject): Promise<void> {
    if (this.closed) return;
    await this.options.send(validateDirection({v: 1, kind, boot_id: this.bootId, request_id: randomUUID(), payload}, "hap"));
  }
  private retireCamera(scope: MediaScope, includeVideos = true): void {
    if (includeVideos) for (const [id, video] of this.videos) if (sourceKey(video.scope) === sourceKey(scope)) this.retireVideo(id, video);
    for (const [id, resource] of this.live) if (sourceKey(resource.scope) === sourceKey(scope)) {
      this.live.delete(id); void this.request("lease_stop", {scope: resource.scope, session_id: id}).catch(() => {});
    }
    for (const [id, resource] of this.speakers) if (sourceKey(resource.scope) === sourceKey(scope)) {
      this.speakers.delete(id); void this.request("talkback_request", {scope: resource.scope, session_id: id, action: "stop"}).catch(() => {});
    }
    for (const [id, job] of this.snapshots) if (sourceKey(job.scope) === sourceKey(scope)) this.cancelSnapshot(id, "resource-retired");
  }
  private invalidate(): void {
    this.complete = false;
    this.options.onInvalidate?.();
    for (const [id, video] of this.videos) if (!sameAccount(video.scope, this.account)) this.retireVideo(id, video);
    for (const entry of this.entries.values()) this.retireCamera(entry.camera.scope, false);
  }
  private fail(error: ProtocolError): ProtocolError {
    if (!this.closed) { this.close(); this.options.onFailure?.(error); }
    return error;
  }
  close(): void {
    if (this.closed) return;
    this.closed = true; this.complete = false; this.noticeEpoch += 1;
    this.options.onInvalidate?.();
    for (const item of this.pending.values()) { clearTimeout(item.timer); item.reject(new ProtocolError("session-closed")); }
    this.pending.clear();
    for (const video of this.videos.values()) { video.retired = true; video.ended(); }
    this.videos.clear(); this.policySynchronization.clear();
    for (const [id] of this.snapshots) this.cancelSnapshot(id, "session-closed");
    this.entries.clear(); this.live.clear(); this.speakers.clear(); this.policyChanges.clear();
    void this.options.bulk?.close("cancelled").catch(() => this.options.onFailure?.(new ProtocolError("bulk-cleanup-failed")));
  }
}

export class CameraDelegate {
  constructor(private readonly session: AdapterSession, readonly cameraId: string, private readonly identity: string) {}
  get info(): NativeCamera { return this.session.cameraInfo(this.cameraId, this.identity); }
  get ready(): boolean { return this.session.cameraReady(this.cameraId, this.identity); }
  updatePolicy(policy: CameraPolicy): Promise<void> { return this.session.updatePolicy(this.cameraId, this.identity, policy); }
  ensurePolicy(policy: CameraPolicy): Promise<void> { return this.session.ensurePolicy(this.cameraId, this.identity, policy); }
  prepareVideo(sessionId: string, options: VideoPrepare, ended: () => void): Promise<VideoDelegate> {
    return this.session.prepareVideo(this.cameraId, this.identity, sessionId, options, ended);
  }
  stopVideo(sessionId: string): Promise<void> { return this.session.stopVideo(sessionId); }
  acquireLive(ttlMs?: number): Promise<LiveDelegate> { return this.session.acquireLive(this.cameraId, this.identity, ttlMs); }
  snapshot(width: number, height: number, reason?: "periodic" | "event" | "explicit"): Promise<Buffer> {
    return this.session.snapshot(this.cameraId, this.identity, width, height, reason);
  }
  setControl(control: "light" | "siren", enabled: boolean): Promise<"accepted" | "unknown"> {
    return this.session.setControl(this.cameraId, this.identity, control, enabled);
  }
  acquireTalkback(): Promise<TalkbackDelegate> { return this.session.acquireTalkback(this.cameraId, this.identity); }
}
export class VideoDelegate {
  private readonly preparedEndpoint: VideoEndpoint;
  constructor(private readonly session: AdapterSession, readonly sessionId: string, endpoint: VideoEndpoint) {
    this.preparedEndpoint = structuredClone(endpoint);
  }
  get endpoint(): VideoEndpoint { return structuredClone(this.preparedEndpoint); }
  start(config: VideoConfiguration): Promise<void> { return this.session.startVideo(this.sessionId, config); }
  reconfigure(config: VideoConfiguration): Promise<void> { return this.session.startVideo(this.sessionId, config, true); }
  close(): Promise<void> { return this.session.stopVideo(this.sessionId); }
}
export class LiveDelegate {
  constructor(private readonly session: AdapterSession, readonly sessionId: string) {}
  renew(ttlMs?: number): Promise<void> { return this.session.renewLive(this.sessionId, ttlMs); }
  close(): Promise<void> { return this.session.stopLive(this.sessionId); }
}
export class TalkbackDelegate {
  constructor(private readonly session: AdapterSession, readonly sessionId: string) {}
  writePcm(pcm: Buffer): Promise<void> { return this.session.sendPcm(this.sessionId, pcm); }
  close(): Promise<void> { return this.session.stopTalkback(this.sessionId); }
}
