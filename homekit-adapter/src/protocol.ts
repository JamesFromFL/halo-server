import { TextDecoder } from "node:util";

export const PROTOCOL_VERSION = 1;
export const MAX_FRAME_BYTES = 65_536;
export const MAX_JSON_DEPTH = 32;
export const MAX_FRAMES_PER_CHUNK = 256;
export const FEATURE = "classic-camera-control-v1";

export const KINDS = [
  "hello", "hello_ack", "ping", "pong", "shutdown", "shutdown_ack", "status", "error",
  "inventory_request", "inventory_page", "camera_status", "policy_update", "policy_ack",
  "lease_request", "lease_result", "lease_renew", "lease_renew_result", "lease_stop", "lease_stop_result",
  "snapshot_request", "snapshot_result", "snapshot_cancel", "talkback_request", "talkback_result",
  "control_request", "control_result", "transfer_open", "transfer_credit", "transfer_end", "transfer_cancel", "transfer_result",
] as const;
export type Kind = typeof KINDS[number];
export type JsonObject = { [key: string]: unknown };
export interface Envelope {
  v: 1;
  kind: Kind;
  boot_id: string;
  request_id: string;
  payload: JsonObject;
}

export class ProtocolError extends Error {
  constructor(public readonly code: string) {
    super(code);
    this.name = "ProtocolError";
  }
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const utf8 = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true });

function record(value: unknown): value is JsonObject {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function fields(value: JsonObject, expected: readonly string[]): void {
  const keys = Object.keys(value);
  if (keys.length !== expected.length || keys.some(key => !expected.includes(key))) {
    throw new ProtocolError("invalid-fields");
  }
}

function text(value: unknown, maximum: number, nonempty = false): value is string {
  if (typeof value !== "string" || (nonempty && !value.length) || /[\u0000-\u001f]/.test(value)) {
    return false;
  }
  const points = Array.from(value);
  return points.length <= maximum && points.every(char =>
    char.length === 2 || char.charCodeAt(0) < 0xd800 || char.charCodeAt(0) > 0xdfff,
  );
}

export const MAX_INVENTORY_DEVICES = 256;
export const MAX_INVENTORY_PAGE = 32;
export const MAX_BULK_CHUNK_BYTES = 65_536;
export const MAX_BULK_WINDOW_CHUNKS = 2;
export const MAX_BULK_WINDOW_BYTES = 131_072;
export const BULK_HEADER_BYTES = 32;
export const BULK_LIMITS = {
  snapshot: 8 * 1024 * 1024, recording_init: 1024 * 1024,
  recording_fragment: 8 * 1024 * 1024, talkback_pcm: 1920,
} as const;
export type BulkKind = keyof typeof BULK_LIMITS;
const bulkKinds = Object.keys(BULK_LIMITS) as BulkKind[];
export const POLICY_FIELDS = [
  "synchronized", "streaming", "live_audio", "snapshots", "recording", "recording_audio", "talkback", "controls",
] as const;
export const CAPABILITY_FIELDS = ["video", "audio", "snapshot", "talkback", "light", "siren", "doorbell", "battery"] as const;
export const RESULT_CODES = [
  "ok", "unavailable", "unsupported", "unauthorized", "stale-scope", "policy-denied", "busy", "timeout",
  "cancelled", "invalid-request", "not-found", "conflict", "source-failed", "expired", "protocol-error", "invalidated", "not-ready",
] as const;
export interface AccountScope { profile_id: string; account_generation: number }
export interface MediaScope extends AccountScope { camera_id: string; device_generation: number; privacy_generation: number }
export type CameraPolicy = Record<typeof POLICY_FIELDS[number], boolean>;
export interface BulkHeader { kind: BulkKind; transfer_id: string; sequence: number; length: number }

type Check = (value: unknown) => boolean;
const integer = (minimum = 1, maximum = Number.MAX_SAFE_INTEGER): Check => value =>
  typeof value === "number" && Number.isSafeInteger(value) && value >= minimum && value <= maximum;
const choice = (...values: readonly string[]): Check => value => typeof value === "string" && values.includes(value);
const nullable = (check: Check): Check => value => value === null || check(value);
const object = (shape: Record<string, Check>): Check => value => record(value) &&
  Object.keys(value).length === Object.keys(shape).length && Object.entries(shape).every(([key, check]) =>
    Object.hasOwn(value, key) && check(value[key]));
const bool: Check = value => typeof value === "boolean";
const uuid: Check = value => typeof value === "string" && value.length === 36 && UUID.test(value);
// Frozen Unicode16 Cc/Cf/Cs ranges: do not depend on the Node Unicode database.
export const FORBIDDEN_TEXT_RANGES: readonly (readonly [number, number])[] = [
  [0x0, 0x1f],
  [0x7f, 0x9f],
  [0xad, 0xad],
  [0x600, 0x605],
  [0x61c, 0x61c],
  [0x6dd, 0x6dd],
  [0x70f, 0x70f],
  [0x890, 0x891],
  [0x8e2, 0x8e2],
  [0x180e, 0x180e],
  [0x200b, 0x200f],
  [0x202a, 0x202e],
  [0x2060, 0x2064],
  [0x2066, 0x206f],
  [0xd800, 0xdfff],
  [0xfeff, 0xfeff],
  [0xfff9, 0xfffb],
  [0x110bd, 0x110bd],
  [0x110cd, 0x110cd],
  [0x13430, 0x1343f],
  [0x1bca0, 0x1bca3],
  [0x1d173, 0x1d17a],
  [0xe0001, 0xe0001],
  [0xe0020, 0xe007f],
];
const display = (maximum: number, nonempty = true): Check => value => text(value, maximum, nonempty) &&
  Array.from(value).every(char => !FORBIDDEN_TEXT_RANGES.some(([start, end]) => {
    const point = char.codePointAt(0)!; return start <= point && point <= end;
  }));
const positive = integer();
const revision = integer(0);
const code = choice(...RESULT_CODES);
const account = object({ profile_id: uuid, account_generation: positive });
const scope = object({ profile_id: uuid, camera_id: display(128), account_generation: positive, device_generation: positive, privacy_generation: positive });
const policyFields = object(Object.fromEntries(POLICY_FIELDS.map(key => [key, bool])));
const policy: Check = value => policyFields(value) && record(value) && (!value.recording_audio || value.recording === true);
const labels = new Set(CAPABILITY_FIELDS.flatMap(name => [`sdk:${name}`, `halo:${name}`]));
const capabilitiesFields = object({ ...Object.fromEntries(CAPABILITY_FIELDS.map(key => [key, bool])), evidence: () => true });
const capabilities: Check = value => {
  if (!capabilitiesFields(value) || !record(value) || !Array.isArray(value.evidence)) return false;
  const evidence: unknown[] = value.evidence;
  return evidence.length <= 16 && evidence.every(label => typeof label === "string" && labels.has(label)) &&
    new Set(evidence).size === evidence.length && CAPABILITY_FIELDS.every(name =>
      !value[name] || ["sdk", "halo"].some(source => evidence.includes(`${source}:${name}`)));
};
const ascii32: Check = value => typeof value === "string" && /^[\x20-\x7e]{1,32}$/.test(value) && !/[^\x20-\x7e]/.test(value);
const videoFields = object({codec: choice("h264"), width: nullable(integer(1, 8192)), height: nullable(integer(1, 8192)),
  profile: nullable(ascii32), level: nullable(ascii32), frame_rate_num: nullable(integer(1, 1_000_000)), frame_rate_den: nullable(integer(1, 1_000_000))});
const video: Check = value => videoFields(value) && record(value) && (
  (value.frame_rate_num === null && value.frame_rate_den === null) ||
  (value.frame_rate_num !== null && value.frame_rate_den !== null && (value.frame_rate_num as number) <= 240 * (value.frame_rate_den as number)));
const packetTime: Check = value => typeof value === "number" && Number.isFinite(value) && value > 0 && value <= 120;
const audio = object({codec: choice("opus", "aac", "pcm_s16le"), sample_rate: nullable(integer(1, 192000)), channels: nullable(integer(1, 2)),
  rtp_clock_rate: nullable(integer(1, 192000)), packet_time_ms: nullable(packetTime)});
const sourceFormat = nullable(object({video: nullable(video), audio: nullable(audio), capture_attempt: nullable(positive), config_generation: nullable(positive)}));
const device = object({
  scope, name: display(256, false), family: display(128, false), kind: display(128, false), model: display(128, false), capabilities,
  connection_state: nullable(bool), battery_percent: nullable(integer(0, 100)), observed_format: sourceFormat,
});
const inventoryFields = object({
  scope: account, revision, cursor: nullable(uuid), next_cursor: nullable(uuid), total: integer(0, MAX_INVENTORY_DEVICES), complete: bool,
  devices: value => Array.isArray(value) && value.length <= MAX_INVENTORY_PAGE && value.every(device),
});
const inventory: Check = value => {
  if (!inventoryFields(value) || !record(value)) return false;
  const devices = value.devices as {scope: MediaScope}[];
  const accountScope = value.scope as AccountScope;
  return devices.length <= (value.total as number) && new Set(devices.map(item => item.scope.camera_id)).size === devices.length &&
    devices.every(item => item.scope.profile_id === accountScope.profile_id && item.scope.account_generation === accountScope.account_generation) &&
    (value.next_cursor === null || (devices.length > 0 && value.next_cursor !== value.cursor));
};
const snapshotRequestFields = object({scope, job_id: uuid, width: integer(1, 4096), height: integer(1, 4096), reason: choice("periodic", "event", "explicit")});
const snapshotRequest: Check = value => snapshotRequestFields(value) && record(value) && (value.width as number) * (value.height as number) <= 8_000_000;
const snapshotResultFields = object({scope, job_id: uuid, status: choice("ready", "rejected", "cancelled"), code, transfer_id: nullable(uuid)});
const snapshotResult: Check = value => snapshotResultFields(value) && record(value) && ((value.status === "ready") === (value.transfer_id !== null));
const resource = {scope, session_id: uuid};
const transfer = {scope, operation_id: uuid, transfer_id: uuid};
const leaseResultFields = object({...resource, status: choice("accepted", "rejected", "expired"), code, ttl_ms: integer(0, 30_000)});
const leaseResult: Check = value => leaseResultFields(value) && record(value) && ((value.status === "accepted") === ((value.ttl_ms as number) > 0));
const transferOpenFields = object({...transfer, kind: choice(...bulkKinds), total_bytes: integer(1, 8 * 1024 * 1024), chunk_bytes: integer(1, MAX_BULK_CHUNK_BYTES), window_chunks: integer(1, MAX_BULK_WINDOW_CHUNKS)});
const transferOpen: Check = value => transferOpenFields(value) && record(value) &&
  (value.total_bytes as number) <= BULK_LIMITS[value.kind as BulkKind] &&
  (value.chunk_bytes as number) <= Math.min(MAX_BULK_CHUNK_BYTES, value.total_bytes as number) &&
  (value.chunk_bytes as number) * (value.window_chunks as number) <= MAX_BULK_WINDOW_BYTES;
const payloadValidators: Record<Kind, Check> = {
  hello: object({role: choice("python"), build_id: value => text(value, 128, true), protocol: integer(1, 1)}),
  hello_ack: object({role: choice("hap"), build_id: value => text(value, 128, true), protocol: integer(1, 1),
    features: value => Array.isArray(value) && value.length === 1 && value[0] === FEATURE, pid: positive}),
  ping: object({}), pong: object({}), shutdown: object({}), shutdown_ack: object({}),
  status: object({state: choice("ready", "degraded"), reason: value => text(value, 256)}),
  error: object({code: value => text(value, 64, true), message: value => text(value, 256)}),
  inventory_request: object({scope: account, revision, cursor: nullable(uuid)}), inventory_page: inventory,
  camera_status: object({scope, revision, state: choice("online", "offline", "unknown"), code}),
  policy_update: object({scope, policy}), policy_ack: object({scope, accepted: bool, code}),
  lease_request: object({...resource, purpose: choice("homekit_live"), ttl_ms: integer(1, 30_000)}), lease_result: leaseResult,
  lease_renew: object({...resource, ttl_ms: integer(1, 30_000)}), lease_renew_result: leaseResult,
  lease_stop: object(resource), lease_stop_result: object({...resource, status: choice("stopped", "rejected"), code}),
  snapshot_request: snapshotRequest, snapshot_result: snapshotResult, snapshot_cancel: object({scope, job_id: uuid}),
  talkback_request: object({...resource, action: choice("acquire", "stop")}),
  talkback_result: object({...resource, status: choice("accepted", "stopped", "rejected"), code}),
  control_request: object({scope, operation_id: uuid, control: choice("light", "siren"), enabled: bool}),
  control_result: object({scope, operation_id: uuid, status: choice("accepted", "rejected", "unknown"), code}),
  transfer_open: transferOpen,
  transfer_credit: object({...transfer, next_sequence: integer(0, 2 ** 32 - 1), credits: integer(1, MAX_BULK_WINDOW_CHUNKS)}),
  transfer_end: object({...transfer, sequence_count: integer(1, 2 ** 32 - 1), total_bytes: integer(1, 8 * 1024 * 1024)}),
  transfer_cancel: object({...transfer, code: choice("cancelled", "invalidated", "timeout", "protocol-error")}),
  transfer_result: object({...transfer, status: choice("complete", "cancelled", "rejected"), code}),
};
const pythonKinds: readonly Kind[] = ["hello", "ping", "shutdown", "inventory_page", "camera_status", "policy_ack", "lease_result", "lease_renew_result", "lease_stop_result", "snapshot_result", "talkback_result", "control_result"];
const hapKinds: readonly Kind[] = ["hello_ack", "pong", "shutdown_ack", "inventory_request", "policy_update", "lease_request", "lease_renew", "lease_stop", "snapshot_request", "snapshot_cancel", "talkback_request", "control_request"];
export function validateScope(value: unknown): MediaScope {
  if (!scope(value)) throw new ProtocolError("invalid-scope");
  return value as MediaScope;
}
export function validateEnvelope(value: unknown): Envelope {
  if (!record(value)) throw new ProtocolError("invalid-envelope");
  fields(value, ["v", "kind", "boot_id", "request_id", "payload"]);
  if (value.v !== 1 || !KINDS.includes(value.kind as Kind) || !uuid(value.boot_id) || !uuid(value.request_id)) {
    throw new ProtocolError("invalid-envelope");
  }
  if (!payloadValidators[value.kind as Kind](value.payload)) throw new ProtocolError("invalid-payload");
  return value as unknown as Envelope;
}
export function validateDirection(value: unknown, sender: "python" | "hap"): Envelope {
  const message = validateEnvelope(value);
  const allowed = sender === "python" ? pythonKinds : sender === "hap" ? hapKinds : null;
  if (allowed === null || (!allowed.includes(message.kind) && (pythonKinds.includes(message.kind) || hapKinds.includes(message.kind)))) {
    throw new ProtocolError("invalid-direction");
  }
  if (message.kind === "transfer_open") {
    const producer = message.payload.kind === "talkback_pcm" ? "hap" : "python";
    if (sender !== producer) throw new ProtocolError("invalid-bulk-direction");
  }
  return message;
}
export const REPLY_KINDS: Partial<Record<Kind, Kind>> = {
  hello: "hello_ack", ping: "pong", shutdown: "shutdown_ack", inventory_request: "inventory_page",
  policy_update: "policy_ack", lease_request: "lease_result", lease_renew: "lease_renew_result",
  lease_stop: "lease_stop_result", snapshot_request: "snapshot_result", talkback_request: "talkback_result",
  control_request: "control_result", transfer_open: "transfer_credit", transfer_end: "transfer_result",
};
export function validateReply(requestValue: unknown, replyValue: unknown): Envelope {
  const request = validateEnvelope(requestValue), reply = validateEnvelope(replyValue);
  if (REPLY_KINDS[request.kind] !== reply.kind || request.boot_id !== reply.boot_id || request.request_id !== reply.request_id) {
    throw new ProtocolError("invalid-reply-correlation");
  }
  for (const key of ["scope", "session_id", "job_id", "operation_id", "transfer_id"]) {
    if (!Object.hasOwn(request.payload, key)) continue;
    const sent = request.payload[key], received = reply.payload[key];
    const equal = key === "scope" ? record(sent) && record(received) &&
      Object.keys(sent).length === Object.keys(received).length && Object.entries(sent).every(([name, value]) => received[name] === value) : sent === received;
    if (!equal) throw new ProtocolError("invalid-reply-ownership");
  }
  if (request.kind === "inventory_request" && (request.payload.cursor !== reply.payload.cursor ||
      (request.payload.revision !== 0 && request.payload.revision !== reply.payload.revision))) {
    throw new ProtocolError("invalid-inventory-revision");
  }
  return reply;
}
export function encodeBulkHeader(header: BulkHeader): Buffer {
  if (!record(header) || !object({kind: choice(...bulkKinds), transfer_id: uuid, sequence: integer(0, 2 ** 32 - 1), length: integer(1, MAX_BULK_CHUNK_BYTES)})(header) ||
      header.length > BULK_LIMITS[header.kind]) throw new ProtocolError("invalid-bulk-header");
  const bytes = Buffer.alloc(BULK_HEADER_BYTES);
  bytes.write("HBLK", 0, "ascii"); bytes[4] = 1; bytes[5] = bulkKinds.indexOf(header.kind) + 1;
  Buffer.from(header.transfer_id.replaceAll("-", ""), "hex").copy(bytes, 8);
  bytes.writeUInt32BE(header.sequence, 24); bytes.writeUInt32BE(header.length, 28);
  return bytes;
}
export function decodeBulkHeader(bytes: Uint8Array): BulkHeader {
  if (bytes.byteLength !== BULK_HEADER_BYTES) throw new ProtocolError("invalid-bulk-length");
  const body = Buffer.from(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const kind = bulkKinds[body[5]! - 1];
  if (!body.subarray(0, 4).equals(Buffer.from("HBLK", "ascii")) || body[4] !== 1 || body.readUInt16BE(6) !== 0 || kind === undefined) {
    throw new ProtocolError("invalid-bulk-header");
  }
  const hex = body.subarray(8, 24).toString("hex");
  const header = {kind, transfer_id: `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`, sequence: body.readUInt32BE(24), length: body.readUInt32BE(28)};
  encodeBulkHeader(header);
  return header;
}

// JSON.parse validates syntax. This pass additionally rejects duplicate object keys,
// excessive nesting and nonfinite numeric values before an envelope becomes authority.
function validateJson(textValue: string, value: unknown): void {
  const stack: { keys: Set<string> | null; keyNext: boolean }[] = [];
  for (let i = 0; i < textValue.length; i++) {
    const char = textValue[i];
    if (char === '"') {
      const start = i;
      while (++i < textValue.length) {
        if (textValue[i] === "\\") i++;
        else if (textValue[i] === '"') break;
      }
      const frame = stack.at(-1);
      if (frame?.keys && frame.keyNext) {
        const key = JSON.parse(textValue.slice(start, i + 1)) as string;
        if (frame.keys.has(key)) throw new ProtocolError("duplicate-key");
        frame.keys.add(key);
        frame.keyNext = false;
      }
    } else if (char === "{" || char === "[") {
      stack.push({ keys: char === "{" ? new Set() : null, keyNext: true });
      if (stack.length > MAX_JSON_DEPTH) throw new ProtocolError("json-depth");
    } else if (char === "}" || char === "]") {
      stack.pop();
    } else if (char === "," && stack.at(-1)?.keys) {
      stack.at(-1)!.keyNext = true;
    }
  }
  const pending = [value];
  while (pending.length) {
    const next = pending.pop();
    if (typeof next === "number" && !Number.isFinite(next)) {
      throw new ProtocolError("nonfinite-number");
    }
    if (next !== null && typeof next === "object") pending.push(...Object.values(next));
  }
}

export function decodeEnvelope(body: Uint8Array): Envelope {
  if (body.byteLength < 1 || body.byteLength > MAX_FRAME_BYTES) {
    throw new ProtocolError("frame-length");
  }
  try {
    const decoded = utf8.decode(body);
    const value: unknown = JSON.parse(decoded);
    validateJson(decoded, value);
    return validateEnvelope(value);
  } catch (error) {
    if (error instanceof ProtocolError) throw error;
    throw new ProtocolError("invalid-json");
  }
}

export function encodeEnvelope(value: unknown): Buffer {
  const envelope = validateEnvelope(value);
  const body = Buffer.from(JSON.stringify(envelope), "utf8");
  if (body.length < 1 || body.length > MAX_FRAME_BYTES) throw new ProtocolError("frame-length");
  // Also rejects values JSON.stringify silently rewrites (e.g. custom toJSON output).
  decodeEnvelope(body);
  const frame = Buffer.allocUnsafe(body.length + 4);
  frame.writeUInt32BE(body.length);
  body.copy(frame, 4);
  return frame;
}

export class FrameDecoder {
  private prefix = Buffer.alloc(4);
  private prefixBytes = 0;
  private body: Buffer | null = null;
  private bodyBytes = 0;
  private failed = false;

  get partial(): boolean { return this.prefixBytes > 0 || this.body !== null; }

  feed(chunk: Uint8Array): Envelope[] {
    if (this.failed) throw new ProtocolError("decoder-closed");
    const messages: Envelope[] = [];
    let offset = 0;
    try {
      while (offset < chunk.length) {
        if (this.body === null) {
          const count = Math.min(4 - this.prefixBytes, chunk.length - offset);
          this.prefix.set(chunk.subarray(offset, offset + count), this.prefixBytes);
          this.prefixBytes += count;
          offset += count;
          if (this.prefixBytes !== 4) continue;
          const length = this.prefix.readUInt32BE();
          if (length < 1 || length > MAX_FRAME_BYTES) throw new ProtocolError("frame-length");
          this.body = Buffer.allocUnsafe(length);
          this.prefixBytes = 0;
        }
        const count = Math.min(this.body.length - this.bodyBytes, chunk.length - offset);
        this.body.set(chunk.subarray(offset, offset + count), this.bodyBytes);
        this.bodyBytes += count;
        offset += count;
        if (this.bodyBytes === this.body.length) {
          messages.push(decodeEnvelope(this.body));
          this.body = null;
          this.bodyBytes = 0;
          if (messages.length > MAX_FRAMES_PER_CHUNK) throw new ProtocolError("frame-burst");
        }
      }
      return messages;
    } catch (error) {
      this.failed = true;
      this.body = null;
      throw error;
    }
  }

  end(): void {
    if (this.partial) {
      this.failed = true;
      this.body = null;
      throw new ProtocolError("truncated-frame");
    }
  }
}
