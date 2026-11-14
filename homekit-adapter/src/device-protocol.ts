/** Private bridge protocol; typed sensor observations and narrowly scoped alarm modes. */
import { TextDecoder } from "node:util";
import {
  ProtocolError,
  pairingManifestRevision,
  FORBIDDEN_TEXT_RANGES,
  type AccountScope,
  type PairingManifest,
} from "./protocol.js";
export const DEVICE_MAX_FRAME_BYTES = 256 * 1024;
export const DEVICE_MAPPINGS = [
  "carbon_monoxide",
  "contact",
  "leak",
  "motion",
  "smoke",
] as const;
export type DeviceMapping = (typeof DEVICE_MAPPINGS)[number];
export const DEVICE_SERVICE_MAPPINGS = [
  ...DEVICE_MAPPINGS,
  "security_system",
  "lightbulb",
] as const;
export type AlarmMode = "disarmed" | "home" | "away";
export interface DeviceAlarmState {
  mode: AlarmMode | null;
  triggered: boolean | null;
  control_enabled: boolean;
}
export interface DeviceAlarmRequest {
  scope: AccountScope;
  registry_revision: number;
  sequence: number;
  accessory_uuid: string;
  device_id: string;
  target: AlarmMode;
}
export interface DeviceAlarmResult extends DeviceAlarmRequest {
  accepted: boolean;
  code: string;
}
export interface DeviceLightState {
  on: boolean | null;
  control_enabled: boolean;
}
export interface DeviceLightRequest extends Omit<DeviceAlarmRequest, "target"> {
  target: boolean;
}
export interface DeviceLightResult extends DeviceLightRequest {
  accepted: boolean;
  code: string;
  observed_on: boolean | null;
}

export interface DeviceChild {
  device_id: string;
  accessory_uuid: string;
  name: string;
  model: string;
  mappings: (DeviceMapping | "security_system" | "lightbulb")[];
  available: boolean;
  values: Record<DeviceMapping, boolean | null>;
  alarm: DeviceAlarmState | null;
  light: DeviceLightState | null;
}
export interface DevicePublication {
  scope: AccountScope;
  registry_revision: number;
  sequence: number;
  expires_at_ms: number;
  bridge: { accessory_uuid: string; username: string; name: string };
  children: DeviceChild[];
}
export interface DevicePairingIdentity {
  scope: AccountScope;
  registry_revision: number;
  accessory_uuid: string;
  username: string;
}
export interface DevicePairingRequest extends DevicePairingIdentity {
  pairing_revision: string;
}
export type DeviceKind =
  | "hello"
  | "hello_ack"
  | "ping"
  | "pong"
  | "publish"
  | "publish_result"
  | "status"
  | "status_result"
  | "reveal"
  | "reveal_result"
  | "reset_prepare"
  | "reset_prepare_result"
  | "shutdown"
  | "shutdown_ack"
  | "alarm_set"
  | "alarm_set_result"
  | "light_set"
  | "light_set_result";
export interface DeviceEnvelope {
  v: 1;
  kind: DeviceKind;
  boot_id: string;
  request_id: string;
  payload: Record<string, unknown>;
}
export const deviceUuid = (value: unknown): value is string =>
  typeof value === "string" &&
  value.length === 36 &&
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(value);
const uuid4 = (v: unknown): v is string =>
  deviceUuid(v) && v[14] === "4" && /[89ab]/.test(v[19]!);
const hash = (v: unknown): v is string =>
  typeof v === "string" && v.length === 64 && /^[0-9a-f]{64}$/.test(v);
const username = (v: unknown): v is string =>
  typeof v === "string" &&
  v.length === 17 &&
  /^[0-9A-F][26AE](?::[0-9A-F]{2}){5}$/.test(v);
const text = (v: unknown, max: number, empty = false): v is string =>
  typeof v === "string" &&
  (empty || v.length > 0) &&
  Array.from(v).length <= max &&
  Array.from(v).every(
    (char) =>
      !FORBIDDEN_TEXT_RANGES.some(
        ([start, end]) =>
          start <= char.codePointAt(0)! && char.codePointAt(0)! <= end,
      ),
  );
const integer = (v: unknown, min = 0): v is number =>
  Number.isSafeInteger(v) && (v as number) >= min;
function exact(
  v: unknown,
  keys: readonly string[],
): asserts v is Record<string, unknown> {
  if (
    v === null ||
    typeof v !== "object" ||
    Array.isArray(v) ||
    Object.keys(v).length !== keys.length ||
    keys.some((k) => !Object.hasOwn(v, k))
  )
    throw new ProtocolError("invalid-device-payload");
}
export function validateDeviceScope(v: unknown): AccountScope {
  exact(v, ["profile_id", "account_generation"]);
  if (!deviceUuid(v.profile_id) || !integer(v.account_generation, 1))
    throw new ProtocolError("invalid-device-scope");
  return v as unknown as AccountScope;
}
export function validateDevicePublication(v: unknown): DevicePublication {
  exact(v, [
    "scope",
    "registry_revision",
    "sequence",
    "expires_at_ms",
    "bridge",
    "children",
  ]);
  validateDeviceScope(v.scope);
  if (
    !integer(v.registry_revision) ||
    !integer(v.sequence, 1) ||
    !integer(v.expires_at_ms, 1)
  )
    throw new ProtocolError("invalid-device-revision");
  exact(v.bridge, ["accessory_uuid", "username", "name"]);
  if (
    !uuid4(v.bridge.accessory_uuid) ||
    !username(v.bridge.username) ||
    !text(v.bridge.name, 64)
  )
    throw new ProtocolError("invalid-device-identity");
  if (!Array.isArray(v.children) || v.children.length > 149)
    throw new ProtocolError("device-capacity");
  const ids = new Set<string>(),
    uuids = new Set<string>([v.bridge.accessory_uuid]);
  for (const child of v.children) {
    exact(child, [
      "device_id",
      "accessory_uuid",
      "name",
      "model",
      "mappings",
      "available",
      "values",
      "alarm",
      "light",
    ]);
    if (
      !hash(child.device_id) ||
      !uuid4(child.accessory_uuid) ||
      ids.has(child.device_id) ||
      uuids.has(child.accessory_uuid) ||
      !text(child.name, 64) ||
      !text(child.model, 64, true) ||
      typeof child.available !== "boolean" ||
      !Array.isArray(child.mappings) ||
      !child.mappings.length ||
      child.mappings.length > DEVICE_SERVICE_MAPPINGS.length ||
      child.mappings.some((x) => !DEVICE_SERVICE_MAPPINGS.includes(x)) ||
      new Set(child.mappings).size !== child.mappings.length ||
      child.mappings.join() !== [...child.mappings].sort().join()
    )
      throw new ProtocolError("invalid-device-child");
    if (child.mappings.includes("security_system")) {
      if (child.mappings.length !== 1)
        throw new ProtocolError("invalid-alarm-mapping");
      exact(child.alarm, ["mode", "triggered", "control_enabled"]);
      if (
        (child.alarm.mode !== null &&
          !["disarmed", "home", "away"].includes(child.alarm.mode as string)) ||
        (child.alarm.triggered !== null &&
          typeof child.alarm.triggered !== "boolean") ||
        typeof child.alarm.control_enabled !== "boolean" ||
        (!child.available &&
          (child.alarm.mode !== null ||
            child.alarm.triggered !== null ||
            child.alarm.control_enabled))
      )
        throw new ProtocolError("invalid-alarm-state");
    } else if (child.alarm !== null)
      throw new ProtocolError("invalid-alarm-mapping");
    if (child.mappings.includes("lightbulb")) {
      if (child.mappings.length !== 1)
        throw new ProtocolError("invalid-light-mapping");
      exact(child.light, ["on", "control_enabled"]);
      if (
        (child.light.on !== null && typeof child.light.on !== "boolean") ||
        typeof child.light.control_enabled !== "boolean" ||
        (!child.available &&
          (child.light.on !== null || child.light.control_enabled))
      )
        throw new ProtocolError("invalid-light-state");
    } else if (child.light !== null)
      throw new ProtocolError("invalid-light-mapping");
    exact(child.values, DEVICE_MAPPINGS);
    for (const mapping of DEVICE_MAPPINGS) {
      const observed = child.values[mapping];
      if (
        observed !== null &&
        (typeof observed !== "boolean" ||
          !child.available ||
          !child.mappings.includes(mapping))
      )
        throw new ProtocolError("invalid-device-observation");
    }
    ids.add(child.device_id);
    uuids.add(child.accessory_uuid);
  }
  return v as unknown as DevicePublication;
}
export function validateDeviceAlarm(
  value: unknown,
  result = false,
): DeviceAlarmRequest | DeviceAlarmResult {
  exact(value, [
    "scope",
    "registry_revision",
    "sequence",
    "accessory_uuid",
    "device_id",
    "target",
    ...(result ? ["accepted", "code"] : []),
  ]);
  validateDeviceScope(value.scope);
  if (
    !integer(value.registry_revision) ||
    !integer(value.sequence, 1) ||
    !uuid4(value.accessory_uuid) ||
    !hash(value.device_id) ||
    !["disarmed", "home", "away"].includes(value.target as string)
  )
    throw new ProtocolError("invalid-alarm-command");
  if (result) outcome(value);
  return value as unknown as DeviceAlarmRequest;
}
export function validateDeviceLight(
  value: unknown,
  result = false,
): DeviceLightRequest | DeviceLightResult {
  exact(value, [
    "scope",
    "registry_revision",
    "sequence",
    "accessory_uuid",
    "device_id",
    "target",
    ...(result ? ["accepted", "code", "observed_on"] : []),
  ]);
  validateDeviceScope(value.scope);
  if (
    !integer(value.registry_revision) ||
    !integer(value.sequence, 1) ||
    !uuid4(value.accessory_uuid) ||
    !hash(value.device_id) ||
    typeof value.target !== "boolean"
  )
    throw new ProtocolError("invalid-light-command");
  if (result) {
    outcome(value);
    if (
      value.accepted
        ? typeof value.observed_on !== "boolean"
        : value.observed_on !== null
    )
      throw new ProtocolError("invalid-light-result");
  }
  return value as unknown as DeviceLightRequest;
}
export function validateDevicePairing(
  v: unknown,
  revision = false,
): DevicePairingIdentity | DevicePairingRequest {
  exact(v, [
    "scope",
    "registry_revision",
    "accessory_uuid",
    "username",
    ...(revision ? ["pairing_revision"] : []),
  ]);
  validateDeviceScope(v.scope);
  if (
    !integer(v.registry_revision) ||
    !uuid4(v.accessory_uuid) ||
    !username(v.username) ||
    (revision && !hash(v.pairing_revision))
  )
    throw new ProtocolError("invalid-device-pairing");
  return v as unknown as DevicePairingIdentity;
}
const codes = [
  "ok",
  "not-ready",
  "stale-scope",
  "conflict",
  "storage-recovery-required",
  "unavailable",
  "invalid-request",
  "busy",
  "policy-denied",
  "uncertain",
];
function outcome(v: Record<string, unknown>): void {
  if (
    typeof v.accepted !== "boolean" ||
    !codes.includes(v.code as string) ||
    v.accepted !== (v.code === "ok")
  )
    throw new ProtocolError("invalid-device-result");
}
function manifest(v: unknown, revision: unknown): void {
  if (v === null) return;
  exact(v, ["accessory_info", "controller_storage", "identifier_cache"]);
  if (
    Object.values(v).some((x) => x !== null && !hash(x)) ||
    pairingManifestRevision(v as unknown as PairingManifest) !== revision
  )
    throw new ProtocolError("invalid-device-manifest");
}
export function validateDeviceEnvelope(
  v: unknown,
  sender?: "python" | "device-hap",
): DeviceEnvelope {
  exact(v, ["v", "kind", "boot_id", "request_id", "payload"]);
  if (
    v.v !== 1 ||
    !deviceUuid(v.boot_id) ||
    !deviceUuid(v.request_id) ||
    !v.payload ||
    typeof v.payload !== "object" ||
    Array.isArray(v.payload)
  )
    throw new ProtocolError("invalid-device-envelope");
  const requests = [
    "hello",
    "ping",
    "publish",
    "status",
    "reveal",
    "reset_prepare",
    "shutdown",
    "alarm_set_result",
    "light_set_result",
  ];
  const replies = [
    "hello_ack",
    "pong",
    "publish_result",
    "status_result",
    "reveal_result",
    "reset_prepare_result",
    "shutdown_ack",
    "alarm_set",
    "light_set",
  ];
  if (
    !(
      sender === "python"
        ? requests
        : sender === "device-hap"
          ? replies
          : [...requests, ...replies]
    ).includes(v.kind as string)
  )
    throw new ProtocolError("invalid-device-kind");
  const p = v.payload as Record<string, unknown>;
  switch (v.kind) {
    case "alarm_set":
      validateDeviceAlarm(p);
      break;
    case "alarm_set_result":
      validateDeviceAlarm(p, true);
      break;
    case "light_set":
      validateDeviceLight(p);
      break;
    case "light_set_result":
      validateDeviceLight(p, true);
      break;
    case "hello":
    case "hello_ack":
      exact(p, [
        "role",
        "build_id",
        "protocol",
        ...(v.kind === "hello_ack" ? ["pid"] : []),
      ]);
      if (
        p.role !== (v.kind === "hello" ? "python" : "device-hap") ||
        !text(p.build_id, 128) ||
        p.protocol !== 1 ||
        (v.kind === "hello_ack" && !integer(p.pid, 1))
      )
        throw new ProtocolError("invalid-device-hello");
      break;
    case "publish":
      validateDevicePublication(p);
      break;
    case "publish_result":
      exact(p, ["scope", "registry_revision", "sequence", "accepted", "code"]);
      validateDeviceScope(p.scope);
      outcome(p);
      if (!integer(p.registry_revision) || !integer(p.sequence, 1))
        throw new ProtocolError("invalid-device-result");
      break;
    case "status":
      validateDevicePairing(p);
      break;
    case "reveal":
    case "reset_prepare":
      validateDevicePairing(p, true);
      break;
    case "status_result":
    case "reveal_result":
    case "reset_prepare_result": {
      const extra =
        v.kind === "reveal_result"
          ? ["pincode", "setup_uri", "ttl_ms"]
          : [
              "pairing_manifest",
              ...(v.kind === "reset_prepare_result" ? ["quiesced"] : []),
            ];
      exact(p, [
        "scope",
        "registry_revision",
        "accessory_uuid",
        "username",
        "accepted",
        "code",
        "published",
        "paired",
        "paired_clients",
        "pairing_revision",
        ...extra,
      ]);
      validateDevicePairing({
        scope: p.scope,
        registry_revision: p.registry_revision,
        accessory_uuid: p.accessory_uuid,
        username: p.username,
      });
      outcome(p);
      if (
        typeof p.published !== "boolean" ||
        (p.paired !== null && typeof p.paired !== "boolean") ||
        (p.paired_clients !== null &&
          (!integer(p.paired_clients) || p.paired_clients > 256)) ||
        (p.pairing_revision !== null && !hash(p.pairing_revision)) ||
        (p.paired === null) !== (p.paired_clients === null) ||
        (p.paired !== null && p.paired !== (p.paired_clients as number) > 0)
      )
        throw new ProtocolError("invalid-device-result");
      if (p.accepted && (p.paired === null || p.pairing_revision === null))
        throw new ProtocolError("invalid-device-result");
      if (v.kind === "reveal_result") {
        if (
          (p.pincode !== null &&
            (typeof p.pincode !== "string" ||
              p.pincode.length !== 10 ||
              !/^\d{3}-\d{2}-\d{3}$/.test(p.pincode))) ||
          (p.setup_uri !== null &&
            (typeof p.setup_uri !== "string" ||
              p.setup_uri.length !== 20 ||
              !/^X-HM:\/\/[0-9A-Z]{13}$/.test(p.setup_uri))) ||
          !integer(p.ttl_ms) ||
          p.ttl_ms > 60_000 ||
          (p.accepted &&
            (p.paired !== false ||
              p.pincode === null ||
              p.setup_uri === null ||
              p.ttl_ms !== 60_000)) ||
          (!p.accepted &&
            (p.pincode !== null || p.setup_uri !== null || p.ttl_ms !== 0))
        )
          throw new ProtocolError("invalid-device-reveal");
      } else {
        manifest(p.pairing_manifest, p.pairing_revision);
        if (p.accepted === (p.pairing_manifest === null))
          throw new ProtocolError("invalid-device-manifest");
        if (
          v.kind === "reset_prepare_result" &&
          (typeof p.quiesced !== "boolean" || (p.accepted && !p.quiesced))
        )
          throw new ProtocolError("invalid-device-reset");
      }
      break;
    }
    default:
      exact(p, []);
  }
  return v as unknown as DeviceEnvelope;
}
export function decodeDeviceEnvelope(bytes: Uint8Array): DeviceEnvelope {
  if (!bytes.length || bytes.length > DEVICE_MAX_FRAME_BYTES)
    throw new ProtocolError("frame-length");
  try {
    const raw = new TextDecoder("utf-8", {
        fatal: true,
        ignoreBOM: true,
      }).decode(bytes),
      v: unknown = JSON.parse(raw);
    const stack: { keys: Set<string> | null; next: boolean }[] = [];
    for (let i = 0; i < raw.length; i++) {
      const c = raw[i];
      if (c === '"') {
        const start = i;
        while (++i < raw.length) {
          if (raw[i] === "\\") i++;
          else if (raw[i] === '"') break;
        }
        const f = stack.at(-1);
        if (f?.keys && f.next) {
          const key = JSON.parse(raw.slice(start, i + 1)) as string;
          if (
            f.keys.has(key) ||
            ["__proto__", "constructor", "prototype"].includes(key)
          )
            throw new ProtocolError("duplicate-key");
          f.keys.add(key);
          f.next = false;
        }
      } else if (c === "{" || c === "[") {
        stack.push({ keys: c === "{" ? new Set() : null, next: true });
        if (stack.length > 16) throw new ProtocolError("json-depth");
      } else if (c === "}" || c === "]") stack.pop();
      else if (c === "," && stack.at(-1)?.keys) stack.at(-1)!.next = true;
    }
    return validateDeviceEnvelope(v);
  } catch (error) {
    if (error instanceof ProtocolError) throw error;
    throw new ProtocolError("invalid-json");
  }
}
export function encodeDeviceEnvelope(v: unknown): Buffer {
  const body = Buffer.from(JSON.stringify(validateDeviceEnvelope(v)), "utf8");
  decodeDeviceEnvelope(body);
  const frame = Buffer.allocUnsafe(body.length + 4);
  frame.writeUInt32BE(body.length);
  body.copy(frame, 4);
  return frame;
}
export class DeviceFrameDecoder {
  private prefix = Buffer.alloc(4);
  private prefixBytes = 0;
  private body: Buffer | null = null;
  private bodyBytes = 0;
  private failed = false;
  get partial(): boolean {
    return this.prefixBytes > 0 || this.body !== null;
  }
  feed(chunk: Uint8Array): DeviceEnvelope[] {
    if (this.failed) throw new ProtocolError("decoder-closed");
    const messages: DeviceEnvelope[] = [];
    let offset = 0;
    try {
      while (offset < chunk.length) {
        if (this.body === null) {
          const n = Math.min(4 - this.prefixBytes, chunk.length - offset);
          this.prefix.set(chunk.subarray(offset, offset + n), this.prefixBytes);
          this.prefixBytes += n;
          offset += n;
          if (this.prefixBytes !== 4) continue;
          const length = this.prefix.readUInt32BE();
          if (length < 1 || length > DEVICE_MAX_FRAME_BYTES)
            throw new ProtocolError("frame-length");
          this.body = Buffer.allocUnsafe(length);
          this.prefixBytes = 0;
        }
        const n = Math.min(
          this.body.length - this.bodyBytes,
          chunk.length - offset,
        );
        this.body.set(chunk.subarray(offset, offset + n), this.bodyBytes);
        this.bodyBytes += n;
        offset += n;
        if (this.bodyBytes === this.body.length) {
          messages.push(decodeDeviceEnvelope(this.body));
          this.body = null;
          this.bodyBytes = 0;
          if (messages.length > 32) throw new ProtocolError("frame-burst");
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
      throw new ProtocolError("truncated-frame");
    }
  }
}
