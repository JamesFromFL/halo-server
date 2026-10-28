import { TextDecoder } from "node:util";

export const PROTOCOL_VERSION = 1;
export const MAX_FRAME_BYTES = 65_536;
export const MAX_JSON_DEPTH = 32;
export const MAX_FRAMES_PER_CHUNK = 256;
export const FEATURE = "classic-camera-control-v1";

export const KINDS = [
  "hello", "hello_ack", "ping", "pong", "shutdown", "shutdown_ack", "status", "error",
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

export function validateEnvelope(value: unknown): Envelope {
  if (!record(value)) throw new ProtocolError("invalid-envelope");
  fields(value, ["v", "kind", "boot_id", "request_id", "payload"]);
  if (value.v !== 1 || !KINDS.includes(value.kind as Kind) ||
      typeof value.boot_id !== "string" || !UUID.test(value.boot_id) ||
      typeof value.request_id !== "string" || !UUID.test(value.request_id) ||
      !record(value.payload)) {
    throw new ProtocolError("invalid-envelope");
  }
  const payload = value.payload;
  switch (value.kind) {
    case "hello":
      fields(payload, ["role", "build_id", "protocol"]);
      if (payload.role !== "python" || !text(payload.build_id, 128, true) || payload.protocol !== 1) {
        throw new ProtocolError("invalid-hello");
      }
      break;
    case "hello_ack":
      fields(payload, ["role", "build_id", "protocol", "features", "pid"]);
      if (payload.role !== "hap" || !text(payload.build_id, 128, true) || payload.protocol !== 1 ||
          !Array.isArray(payload.features) || payload.features.length !== 1 ||
          payload.features[0] !== FEATURE || !Number.isSafeInteger(payload.pid) ||
          (payload.pid as number) <= 0) {
        throw new ProtocolError("invalid-hello-ack");
      }
      break;
    case "status":
      fields(payload, ["state", "reason"]);
      if (!["ready", "degraded"].includes(payload.state as string) || !text(payload.reason, 256)) {
        throw new ProtocolError("invalid-status");
      }
      break;
    case "error":
      fields(payload, ["code", "message"]);
      if (!text(payload.code, 64, true) || !text(payload.message, 256)) {
        throw new ProtocolError("invalid-error");
      }
      break;
    default:
      fields(payload, []);
  }
  return value as unknown as Envelope;
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
