import { randomUUID } from "node:crypto";
import type { BulkChannel, ReceiveHandle } from "./bulk.js";
import {
  BULK_LIMITS, MAX_INVENTORY_DEVICES, MAX_INVENTORY_PAGE, ProtocolError, REPLY_KINDS,
  validateDirection, validateReply, type AccountScope, type CameraPolicy, type Envelope,
  type JsonObject, type Kind, type MediaScope, type VideoConfiguration, type VideoPrepare, type VideoEndpoint,
  type AudioConfiguration, type AudioSettings, type RecordingConfiguration,
} from "./protocol.js";

export const MAX_SESSION_REQUESTS = 16;
export const REQUEST_TIMEOUT_MS = 30_000;
const MAX_SNAPSHOT_REQUESTS = 2;
export const SNAPSHOT_TIMEOUT_MS = 20_000;
export const SNAPSHOT_REPLY_TIMEOUT_MS = 17_000;
export type SnapshotReason = "periodic" | "event" | "explicit" | "unknown";
const MAX_LIVE_SESSIONS = 8;
const MAX_RECORDINGS = 2;
const RECORDING_CLEANUP_MS = 60_000;
export const RECORDING_LIFETIME_MS = 180_000;

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
  result: Promise<Buffer>; timer: NodeJS.Timeout; receiver: ReceiveHandle | null; cleanupTimer: NodeJS.Timeout | null;
}
export interface NativeRecordingPacket {
  kind: "init" | "fragment";
  data: Buffer;
  sequence: number;
  durationMs: number;
}
interface RecordingPull {
  sequence: number;
  transferId: string | null;
  kind: "init" | "fragment" | null;
  totalBytes: number;
  durationMs: number;
  receiver: ReceiveHandle | null;
  chunks: Buffer[];
  length: number;
  result: Promise<NativeRecordingPacket | null>;
  resolve: (packet: NativeRecordingPacket | null) => void;
  reject: (error: Error) => void;
}
interface RecordingJob {
  scope: MediaScope;
  configuration: RecordingConfiguration;
  sequence: number;
  retired: boolean;
  finished: boolean;
  cleanupProven: boolean;
  pull: RecordingPull | null;
  stopping: Promise<void> | null;
  failure: Promise<never>;
  reject: (error: Error) => void;
  signal: AbortSignal | undefined;
  abort: (() => void) | null;
  cleanupTimer: NodeJS.Timeout | null;
  timer: NodeJS.Timeout;
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
  private readonly recordings = new Map<string, RecordingJob>();
  private readonly policyChanges = new Set<string>();
  private readonly policySynchronization = new Map<string, Promise<void>>();
  private readonly videos = new Map<string, VideoResource>();
  private account: AccountScope | null = null;
  private revision = 0;
  private noticeEpoch = 0;
  private complete = false;
  private closed = false;
  private quiesced = false;
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
  get activeRecordings(): number {return [...this.recordings.values()].filter(job=>!job.finished&&(!job.retired||!job.cleanupProven)).length;}
  get inventoryComplete(): boolean { return this.complete; }
  get inventoryRevision(): number { return this.revision; }
  get accountScope(): AccountScope | null { return this.account === null ? null : {...this.account}; }
  synchronizeEvents(scope: AccountScope | null, epoch: string | null, sequence: number): Promise<JsonObject> {
    return this.request("event_sync", {scope, epoch, sequence}).then(reply => reply.payload);
  }
  cameras(): NativeCamera[] { return [...this.entries.values()].map(entry => structuredClone(entry.camera)); }
  camera(cameraId: string): CameraDelegate {
    const entry = this.entries.get(cameraId);
    if (!entry || !this.complete || this.closed || this.quiesced) throw new ProtocolError("camera-not-ready");
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
      if (message.kind === "transfer_open" && message.payload.kind === "snapshot") this.authorizeSnapshot(message);
      try{return await this.options.bulk.handleControl(message);}
      catch{throw this.fail(new ProtocolError("bulk-protocol-failed"));}
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
        if (this.complete) for(const [id,job] of this.recordings) {
          const current=this.entries.get(job.scope.camera_id);
          if(!current||!sameScope(current.camera.scope,job.scope))void this.stopRecording(id).catch(()=>{});
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

  private request(kind: Kind, payload: JsonObject, observe?: Pending["observe"], timeout = this.timeout): Promise<Envelope> {
    if (this.closed) return Promise.reject(new ProtocolError("session-closed"));
    if (!REPLY_KINDS[kind]) return Promise.reject(new ProtocolError("request-not-supported"));
    if (this.pending.size >= this.maximumPending) return Promise.reject(new ProtocolError("request-capacity"));
    const message = structuredClone(validateDirection({v: 1, kind, boot_id: this.bootId, request_id: randomUUID(), payload}, "hap"));
    return new Promise<Envelope>((resolve, reject) => {
      const timer = setTimeout(() => {
        if (kind === "snapshot_request" || kind === "recording_next" || kind === "recording_open") {
          // A cancelled image can still have a READY already queued by the
          // parent. Keep only this bounded correlation for cleanup admission.
          item.timer = setTimeout(() => this.retirePending(message.request_id), 60_000); item.timer.unref();
        } else this.retirePending(message.request_id);
        reject(new ProtocolError("request-timeout"));
      }, timeout);
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
    if (this.closed || this.quiesced || !this.complete || !entry || sourceKey(entry.camera.scope) !== identity) throw new ProtocolError("stale-camera");
    if (permission && (!entry.synchronized || !entry.policy?.synchronized || !entry.policy[permission])) throw new ProtocolError("policy-denied");
    return entry;
  }
  cameraInfo(cameraId: string, identity: string): NativeCamera { return structuredClone(this.entry(cameraId, identity).camera); }
  cameraReady(cameraId: string, identity: string): boolean {
    try { return this.entry(cameraId, identity).synchronized; } catch { return false; }
  }
  /** Keep cleanup correlation alive while permanently closing all acquisition. */
  quiesce(): void {
    if (this.quiesced) return;
    this.quiesced = true;
    for (const entry of this.entries.values()) {
      entry.synchronized = false; entry.policy = null;
      this.retireCamera(entry.camera.scope);
    }
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
    policy = structuredClone(policy);
    while (true) {
      const pending = this.policySynchronization.get(identity);
      // A preceding waiter may have installed the next update while this one settled.
      if (pending) { await pending; continue; }
      const entry = this.entry(cameraId, identity);
      if (entry.synchronized && entry.policy && Object.keys(policy).every(name =>
        entry.policy![name as keyof CameraPolicy] === policy[name as keyof CameraPolicy])) return;
      const operation = this.updatePolicy(cameraId, identity, policy);
      this.policySynchronization.set(identity, operation);
      try { await operation; return; }
      finally { if (this.policySynchronization.get(identity) === operation) this.policySynchronization.delete(identity); }
    }
  }

  async updateAudioSettings(cameraId: string, identity: string, expectedRevision: number, settings: AudioSettings): Promise<number> {
    const entry = this.entry(cameraId, identity), scope = {...entry.camera.scope};
    settings = structuredClone(settings);
    validateDirection({v: 1, kind: "audio_settings_update", boot_id: this.bootId, request_id: randomUUID(),
      payload: {scope, expected_revision: expectedRevision, settings}}, "hap");
    entry.synchronized = false; entry.policy = null; this.retireCamera(scope);
    const reply = await this.request("audio_settings_update", {scope, expected_revision: expectedRevision, settings});
    await this.refresh(); this.entry(cameraId, identity);
    if (reply.payload.accepted !== true || reply.payload.code !== "ok") throw new ProtocolError("audio-settings-rejected");
    return reply.payload.revision as number;
  }

  async updateRecordingSettings(scope: MediaScope, expectedRevision: number, active: boolean, audioActive: boolean, configuration: RecordingConfiguration | null): Promise<JsonObject> {
    const current = this.entry(scope.camera_id, sourceKey(scope));
    if (!sameScope(current.camera.scope, scope))
      throw new ProtocolError("stale-camera");
    const payload = { scope: { ...scope }, expected_revision: expectedRevision, active, audio_active: audioActive,
      configuration: structuredClone(configuration) };
    validateDirection({ v: 1, kind: "recording_settings_update", boot_id: this.bootId, request_id: randomUUID(), payload }, "hap");
    for (const [id, job] of this.recordings)
      if (sameScope(job.scope, scope) &&
        (!active || configuration === null || !audioActive && job.configuration.audio !== null))
        void this.stopRecording(id).catch(() => { });
    const reply = await this.request("recording_settings_update", payload);
    await this.refresh();
    this.entry(scope.camera_id, sourceKey(scope));
    if (reply.payload.accepted !== true || reply.payload.code !== "ok")
      throw new ProtocolError("recording-settings-rejected");
    return reply.payload;
  }

  async openRecording(scope: MediaScope, configuration: RecordingConfiguration, signal?: AbortSignal): Promise<NativeRecording> {
    const current = this.entry(scope.camera_id, sourceKey(scope), "recording");
    if (!sameScope(current.camera.scope, scope))
      throw new ProtocolError("recording-retired");
    if (configuration.audio !== null)
      this.entry(scope.camera_id, sourceKey(scope), "recording_audio");
    if (signal?.aborted)
      throw new ProtocolError("recording-cancelled");
    if (!this.options.bulk)
      throw new ProtocolError("bulk-unavailable");
    if (this.activeRecordings >= MAX_RECORDINGS || this.recordings.size >= MAX_SESSION_REQUESTS ||
      [...this.recordings.values()].some(job => !job.finished && (!job.retired || !job.cleanupProven) && sameScope(job.scope, scope)))
      throw new ProtocolError("recording-capacity");
    const jobId = randomUUID(), payload = { scope: { ...scope }, job_id: jobId, configuration: structuredClone(configuration) };
    validateDirection({ v: 1, kind: "recording_open", boot_id: this.bootId, request_id: randomUUID(), payload }, "hap");
    let reject!: (error: Error) => void;
    const failure = new Promise<never>((_, no) => { reject = no; });
    void failure.catch(() => { });
    const job: RecordingJob = { scope: { ...scope }, configuration: structuredClone(configuration), sequence: 0, retired: false,
      finished: false, cleanupProven: false, pull: null, stopping: null, failure, reject, signal, abort: null, cleanupTimer: null,
      timer: setTimeout(() => { void this.stopRecording(jobId).catch(() => { }); }, RECORDING_LIFETIME_MS) };
    this.recordings.set(jobId, job);
    if (signal) {
      job.abort = () => { void this.stopRecording(jobId).catch(() => { }); };
      signal.addEventListener("abort", job.abort, { once: true });
    }
    try {
      const reply = await Promise.race([this.request("recording_open", payload), failure]);
      this.recording(jobId);
      if (reply.payload.status !== "accepted" || reply.payload.code !== "ok")
        throw new ProtocolError("recording-open-rejected");
      return new NativeRecording(this, jobId, scope);
    }
    catch (error) {
      await this.stopRecording(jobId);
      throw error;
    }
  }
  private recording(id: string): RecordingJob {
    const job = this.recordings.get(id);
    if (!job || job.retired)
      throw new ProtocolError("recording-retired");
    const current = this.entry(job.scope.camera_id, sourceKey(job.scope), "recording");
    if (!sameScope(current.camera.scope, job.scope))
      throw new ProtocolError("recording-retired");
    if (job.configuration.audio !== null)
      this.entry(job.scope.camera_id, sourceKey(job.scope), "recording_audio");
    return job;
  }
  recordingCurrent(id: string): boolean {
    try {
      this.recording(id);
      return true;
    } catch {
      return false;
    }
  }
  async nextRecording(id: string, signal?: AbortSignal): Promise<NativeRecordingPacket | null> {
    const job = this.recording(id);
    if (signal?.aborted) {
      await this.stopRecording(id);
      throw new ProtocolError("recording-cancelled");
    }
    if (job.finished)
      return null;
    if (job.pull)
      throw new ProtocolError("recording-next-busy");
    let resolve!: (packet: NativeRecordingPacket | null) => void, reject!: (error: Error) => void;
    const result = new Promise<NativeRecordingPacket | null>((ok, no) => { resolve = ok; reject = no; });
    void result.catch(() => { });
    const pull: RecordingPull = { sequence: job.sequence, transferId: null, kind: null, totalBytes: 0, durationMs: 0,
      receiver: null, chunks: [], length: 0, result, resolve, reject };
    job.pull = pull;
    const abort = (): void => { void this.stopRecording(id).catch(() => { }); };
    signal?.addEventListener("abort", abort, { once: true });
    const request = this.request("recording_next", { scope: job.scope, job_id: id, sequence: pull.sequence }, message => { if (message.payload.status === "ready")
      this.authorizeRecording(id, job, pull, message); });
    void request.then(reply => {
      if (reply.payload.status === "eos") {
        job.finished = true;
        pull.resolve(null);
      }
      else if (reply.payload.status !== "ready") {
        pull.reject(new ProtocolError("recording-next-rejected"));
      }
    }).catch(error => pull.reject(error as Error));
    try {
      const packet = await result;
      this.recording(id);
      if (signal?.aborted)
        throw new ProtocolError("recording-cancelled");
      if (packet !== null)
        job.sequence += 1;
      if (job.pull === pull)
        job.pull = null;
      return packet;
    }
    catch (error) {
      await this.stopRecording(id);
      throw error;
    }
    finally {
      signal?.removeEventListener("abort", abort);
    }
  }
  private authorizeRecording(id: string, job: RecordingJob, pull: RecordingPull, message: Envelope): void {
    const payload = message.payload;
    if (this.recordings.get(id) !== job || job.pull !== pull || pull.transferId !== null || !this.options.bulk)
      throw new ProtocolError("unsolicited-recording-ready");
    pull.transferId = payload.transfer_id as string;
    pull.kind = payload.kind as "init" | "fragment";
    pull.totalBytes = payload.total_bytes as number;
    pull.durationMs = payload.duration_ms as number;
    const valid = (): boolean => !job.retired && job.pull === pull && this.recordingCurrent(id);
    if (!valid() && !job.retired)
      this.retireRecording(id, job, new ProtocolError("recording-retired"));
    const receiver = this.options.bulk.authorizeReceive({ scope: job.scope, operationId: id, transferId: pull.transferId,
      kind: pull.kind === "init" ? "recording_init" : "recording_fragment", totalBytes: pull.totalBytes,
      valid: job.retired ? () => true : valid,
      sink: async (chunk) => {
        if (!valid())
          throw new ProtocolError("recording-retired");
        pull.length += chunk.length;
        if (pull.length > pull.totalBytes)
          throw new ProtocolError("recording-capacity");
        pull.chunks.push(Buffer.from(chunk));
      },
      onClose: async (code) => { if (code !== "ok") {
        pull.chunks.length = 0;
        pull.length = 0;
        pull.reject(new ProtocolError("recording-transfer-failed"));
      } } });
    pull.receiver = receiver;
    if (job.retired)
      void receiver.cancel("invalidated").catch(() => { this.fail(new ProtocolError("recording-cleanup-failed")); });
    void receiver.wait().then(() => {
      if (!valid())
        throw new ProtocolError("recording-retired");
      const data = Buffer.concat(pull.chunks, pull.length);
      pull.chunks.length = 0;
      pull.length = 0;
      pull.resolve({ kind: pull.kind!, sequence: pull.sequence, durationMs: pull.durationMs, data });
    }).catch(() => pull.reject(new ProtocolError("recording-transfer-failed"))).finally(() => { pull.chunks.length = 0; pull.length = 0; });
  }
  private retireRecording(id: string, job: RecordingJob, error: Error): void {
    if (job.retired)
      return;
    job.retired = true;
    job.reject(error);
    clearTimeout(job.timer);
    for (const [requestId, pending] of this.pending)
      if (pending.request.payload.job_id === id &&
        (pending.request.kind === "recording_open" || pending.request.kind === "recording_next")) {
        clearTimeout(pending.timer);
        pending.reject(error);
        pending.timer = setTimeout(() => this.retirePending(requestId), RECORDING_CLEANUP_MS);
        pending.timer.unref();
      }
    if (job.signal && job.abort)
      job.signal.removeEventListener("abort", job.abort);
    job.abort = null;
    job.signal = undefined;
    if (job.pull) {
      const pull = job.pull;
      pull.reject(error);
      pull.chunks.length = 0;
      pull.length = 0;
      // Deny-only late READY correlation retains identity/counters, never media
      // through a settled promise or its resolver closures.
      pull.result = Promise.resolve(null);
      pull.resolve = () => { };
      pull.reject = () => { };
    }
    job.cleanupTimer = setTimeout(() => this.finishRecording(id, job), RECORDING_CLEANUP_MS);
    job.cleanupTimer.unref();
  }
  private finishRecording(id: string, job: RecordingJob): void {
    if (this.recordings.get(id) !== job)
      return;
    clearTimeout(job.timer);
    if (job.cleanupTimer)
      clearTimeout(job.cleanupTimer);
    if (job.signal && job.abort)
      job.signal.removeEventListener("abort", job.abort);
    job.pull = null;
    this.recordings.delete(id);
  }
  stopRecording(id: string): Promise<void> {
    const job = this.recordings.get(id);
    if (!job)
      return Promise.resolve();
    if (job.stopping)
      return job.stopping;
    this.retireRecording(id, job, new ProtocolError("recording-retired"));
    const receiver = job.pull?.receiver;
    if (this.closed) {
      if (receiver)
        void receiver.cancel("invalidated").catch(() => { });
      return Promise.resolve();
    }
    job.stopping = Promise.all([
      this.request("recording_stop", { scope: job.scope, job_id: id }, undefined, Math.min(this.timeout, 20000)).then(reply => {
        if (reply.payload.status !== "stopped" || reply.payload.code !== "ok")
          throw new ProtocolError("recording-stop-rejected");
      }),
      receiver?.cancel("invalidated") ?? Promise.resolve(),
    ]).then(() => { job.cleanupProven = true; }).catch(error => { throw this.fail(error instanceof ProtocolError ? error : new ProtocolError("recording-cleanup-failed")); });
    void job.stopping.catch(() => { });
    return job.stopping;
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
      if (endpoint.key !== options.key || endpoint.salt !== options.salt ||
        (options.audio === null) !== (endpoint.audio === null) || options.audio !== null &&
        (endpoint.audio!.key !== options.audio.key || endpoint.audio!.salt !== options.audio.salt)) throw new ProtocolError("video-endpoint-mismatch");
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
  async startVideo(sessionId: string, config: VideoConfiguration, reconfigure = false, audio: AudioConfiguration | null = null): Promise<void> {
    const resource = this.video(sessionId);
    const reply = await this.request(reconfigure ? "video_reconfigure" : "video_start", {scope: resource.scope, session_id: sessionId, config,
      ...(reconfigure ? {} : {audio_config: audio})});
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
  async controlState(cameraId: string, identity: string): Promise<{light: boolean | null; siren: boolean | null}> {
    const scope = {...this.entry(cameraId, identity, "controls").camera.scope};
    const reply = await this.request("control_state_request", {scope, operation_id: randomUUID()});
    if (!sameScope(this.entry(cameraId, identity, "controls").camera.scope, scope)) throw new ProtocolError("resource-retired");
    if (reply.payload.status !== "accepted" || reply.payload.code !== "ok") throw new ProtocolError("control-state-unavailable");
    return {light: reply.payload.light as boolean | null, siren: reply.payload.siren as boolean | null};
  }

  async snapshot(cameraId: string, identity: string, width: number, height: number,
    reason: SnapshotReason = "explicit", signal?: AbortSignal): Promise<Buffer> {
    const scope = {...this.entry(cameraId, identity, "snapshots").camera.scope};
    if (signal?.aborted) throw new ProtocolError("snapshot-cancelled");
    if ([...this.snapshots.values()].filter(job => !job.cancelled).length >= MAX_SNAPSHOT_REQUESTS ||
        this.snapshots.size >= MAX_SESSION_REQUESTS) throw new ProtocolError("snapshot-capacity");
    const jobId = randomUUID();
    validateDirection({v: 1, boot_id: this.bootId, request_id: randomUUID(), kind: "snapshot_request",
      payload: {scope, job_id: jobId, width, height, reason}}, "hap");
    let resolve!: (value: Buffer) => void, reject!: (error: Error) => void;
    const result = new Promise<Buffer>((ok, no) => {resolve = ok; reject = no;}); void result.catch(() => {});
    const timer = setTimeout(() => this.cancelSnapshot(jobId, "snapshot-timeout"), Math.min(this.timeout, SNAPSHOT_TIMEOUT_MS));
    const job: SnapshotJob = {scope, transferId: null, cancelled: false, result, resolve, reject, timer, receiver: null, cleanupTimer: null};
    this.snapshots.set(jobId, job);
    const abort = (): void => this.cancelSnapshot(jobId, "snapshot-cancelled");
    signal?.addEventListener("abort", abort, {once:true});
    // Keep the owned RPC correlation after caller cancellation. Its exact late
    // READY/OPEN may authorize draining, but can never invoke the caller again.
    const request = this.request("snapshot_request", {scope, job_id: jobId, width, height, reason}, message => {
      if (message.payload.status === "ready" && message.payload.code === "ok") job.transferId = message.payload.transfer_id as string;
    }, Math.min(this.timeout, SNAPSHOT_REPLY_TIMEOUT_MS));
    void request.then(reply => {
      if (reply.payload.status !== "ready" || reply.payload.code !== "ok") {
        job.reject(new ProtocolError("snapshot-rejected")); this.finishSnapshot(jobId, job);
      }
    }).catch(error => {job.reject(error as Error); this.cancelSnapshot(jobId, "snapshot-cancelled");});
    try {
      const bytes = await result;
      if (signal?.aborted || job.cancelled || !sameScope(this.entry(cameraId, identity, "snapshots").camera.scope, scope)) throw new ProtocolError("resource-retired");
      return bytes;
    } catch (error) {this.cancelSnapshot(jobId, "snapshot-cancelled"); throw error;}
    finally {
      clearTimeout(timer); signal?.removeEventListener("abort", abort);
      if (!job.cancelled) this.finishSnapshot(jobId, job);
    }
  }
  private finishSnapshot(id: string, job: SnapshotJob): void {
    if (this.snapshots.get(id) !== job) return;
    clearTimeout(job.timer); if (job.cleanupTimer) clearTimeout(job.cleanupTimer); this.snapshots.delete(id);
  }
  private authorizeSnapshot(message: Envelope): void {
    const payload = message.payload, id = payload.operation_id as string, job = this.snapshots.get(id);
    if (!job || job.receiver || job.transferId !== payload.transfer_id || payload.kind !== "snapshot" ||
        !sameScope(job.scope, payload.scope as MediaScope) || !this.options.bulk ||
        (payload.total_bytes as number) > BULK_LIMITS.snapshot) throw this.fail(new ProtocolError("unsolicited-transfer"));
    const chunks: Buffer[] = []; let length = 0;
    const valid = (): boolean => {
      try {return !job.cancelled && this.snapshots.get(id) === job &&
        sameScope(this.entry(job.scope.camera_id, sourceKey(job.scope), "snapshots").camera.scope, job.scope);}
      catch {return false;}
    };
    if (!valid() && !job.cancelled) this.cancelSnapshot(id, "resource-retired");
    // A cancelled late OPEN is admitted only as the exact previously owned
    // READY transfer, immediately fenced before OPEN handling grants credits.
    const receiver = this.options.bulk.authorizeReceive({scope: job.scope, operationId: id,
      transferId: payload.transfer_id as string, kind: "snapshot", totalBytes: payload.total_bytes as number,
      valid: job.cancelled ? () => true : valid,
      sink: async chunk => {
        if (!valid()) throw new ProtocolError("resource-retired");
        length += chunk.length; if (length > BULK_LIMITS.snapshot) throw new ProtocolError("snapshot-capacity"); chunks.push(Buffer.from(chunk));
      }, onClose: async code => {if (code !== "ok") job.reject(new ProtocolError("snapshot-transfer-failed"));}});
    job.receiver = receiver;
    if (job.cancelled) void receiver.cancel("cancelled").catch(() => {});
    void receiver.wait().then(() => {
      if (valid()) job.resolve(Buffer.concat(chunks, length)); else job.reject(new ProtocolError("resource-retired"));
    }).catch(() => job.reject(new ProtocolError("snapshot-transfer-failed"))).finally(() => {
      chunks.length = 0; this.finishSnapshot(id, job);
    });
  }
  private cancelSnapshot(id: string, code: string): void {
    const job = this.snapshots.get(id);
    if (!job || job.cancelled) return;
    job.cancelled = true; clearTimeout(job.timer); job.reject(new ProtocolError(code));
    if (job.receiver) void job.receiver.cancel(code === "snapshot-timeout" ? "timeout" : "invalidated").catch(() => {});
    // A finite deny-only owner is retained for a ready reply/OPEN already in
    // flight. It cannot grant credits or accept a different transfer identity.
    job.cleanupTimer = setTimeout(() => this.finishSnapshot(id, job), 60_000); job.cleanupTimer.unref();
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
    for(const [id,job] of this.recordings)if(sourceKey(job.scope)===sourceKey(scope))void this.stopRecording(id).catch(()=>{});
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
    for (const [id, job] of this.snapshots) this.finishSnapshot(id, job);
    for(const [id,job] of this.recordings){this.retireRecording(id,job,new ProtocolError("session-closed"));this.finishRecording(id,job);}
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
  updateAudioSettings(expectedRevision: number, settings: AudioSettings): Promise<number> {
    return this.session.updateAudioSettings(this.cameraId, this.identity, expectedRevision, settings);
  }
  async updateRecordingSettings(expectedRevision: number, active: boolean, audioActive: boolean, configuration: RecordingConfiguration | null): Promise<number> {
    return (await this.session.updateRecordingSettings(this.info.scope, expectedRevision, active, audioActive, configuration)).revision as number;
  }
  openRecording(configuration: RecordingConfiguration, signal?: AbortSignal): Promise<NativeRecording> {
    return this.session.openRecording(this.info.scope, configuration, signal);
  }
  prepareVideo(sessionId: string, options: VideoPrepare, ended: () => void): Promise<VideoDelegate> {
    return this.session.prepareVideo(this.cameraId, this.identity, sessionId, options, ended);
  }
  stopVideo(sessionId: string): Promise<void> { return this.session.stopVideo(sessionId); }
  acquireLive(ttlMs?: number): Promise<LiveDelegate> { return this.session.acquireLive(this.cameraId, this.identity, ttlMs); }
  snapshot(width: number, height: number, reason?: SnapshotReason, signal?: AbortSignal): Promise<Buffer> {
    return this.session.snapshot(this.cameraId, this.identity, width, height, reason, signal);
  }
  setControl(control: "light" | "siren", enabled: boolean): Promise<"accepted" | "unknown"> {
    return this.session.setControl(this.cameraId, this.identity, control, enabled);
  }
  controlState(): Promise<{light: boolean | null; siren: boolean | null}> {
    return this.session.controlState(this.cameraId, this.identity);
  }
  acquireTalkback(): Promise<TalkbackDelegate> { return this.session.acquireTalkback(this.cameraId, this.identity); }
}
export class VideoDelegate {
  private readonly preparedEndpoint: VideoEndpoint;
  constructor(private readonly session: AdapterSession, readonly sessionId: string, endpoint: VideoEndpoint) {
    this.preparedEndpoint = structuredClone(endpoint);
  }
  get endpoint(): VideoEndpoint { return structuredClone(this.preparedEndpoint); }
  start(config: VideoConfiguration, audio: AudioConfiguration | null = null): Promise<void> { return this.session.startVideo(this.sessionId, config, false, audio); }
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

export class NativeRecording {
  private readonly frozenScope: MediaScope;
  constructor(private readonly session: AdapterSession, readonly jobId: string, scope: MediaScope) { this.frozenScope = { ...scope }; }
  get scope(): MediaScope { return { ...this.frozenScope }; }
  isCurrent(): boolean { return this.session.recordingCurrent(this.jobId); }
  next(signal?: AbortSignal): Promise<NativeRecordingPacket | null> { return this.session.nextRecording(this.jobId, signal); }
  stop(): Promise<void> { return this.session.stopRecording(this.jobId); }
}
