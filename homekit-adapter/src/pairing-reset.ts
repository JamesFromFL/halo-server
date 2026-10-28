import { Accessory } from "@homebridge/hap-nodejs";
import { fsyncSync } from "node:fs";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { pairingDescriptors } from "./main.js";
import { PairingStore, pairingRevision, readPairingManifest } from "./storage.js";
import { verifyPublicApi } from "./public-api.js";
import { enablePairingDurability } from "./storage-boundary.js";
import type { PairingManifest } from "./protocol.js";

export interface ResetInput {
  v: 1; operation_id: string; accessory_id: string; username: string; pairing_revision: string;
  pairing_manifest: PairingManifest; resume_cleanup: boolean;
}
export interface ResetOutput {
  v: 1; operation_id: string; accessory_id: string; username: string; pairing_revision: string;
  accepted: boolean; code: "ok" | "conflict" | "invalid-request" | "unavailable";
}
const uuid = (value: unknown): value is string => typeof value === "string" && value.length === 36 && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(value);
const sha = (value: unknown): value is string => typeof value === "string" && value.length === 64 && /^[0-9a-f]{64}$/.test(value);
const keys = (value: unknown, expected: string[]): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value) && Object.keys(value).length === expected.length &&
  expected.every(key => Object.hasOwn(value, key));
export function decodeResetInput(bytes: Buffer): ResetInput {
  if (bytes.length < 1 || bytes.length > 4096 || bytes.some(value => value > 127)) throw new Error("invalid-request");
  const text = bytes.toString("ascii"), value: unknown = JSON.parse(text);
  // The parent emits one canonical ASCII record. Equality also rejects duplicate keys.
  if (JSON.stringify(value) + "\n" !== text || !keys(value, ["v", "operation_id", "accessory_id", "username", "pairing_revision", "pairing_manifest", "resume_cleanup"]) ||
      value.v !== 1 || !uuid(value.operation_id) || !uuid(value.accessory_id) || typeof value.username !== "string" || value.username.length !== 17 ||
      !/^[0-9A-F][26AE](?::[0-9A-F]{2}){5}$/.test(value.username) || !sha(value.pairing_revision) ||
      typeof value.resume_cleanup !== "boolean" || !keys(value.pairing_manifest, ["accessory_info", "identifier_cache", "controller_storage"]) ||
      !Object.values(value.pairing_manifest).every(item => item === null || sha(item))) throw new Error("invalid-request");
  const result = value as unknown as ResetInput;
  if (pairingRevision(result.pairing_manifest) !== result.pairing_revision) throw new Error("invalid-request");
  return result;
}
/** Caller owns the exclusive inherited lock and has reaped every prior HAP child. */
export function resetPairing(descriptor: number, input: ResetInput): ResetOutput {
  const result: ResetOutput = {v: 1, operation_id: input.operation_id, accessory_id: input.accessory_id,
    username: input.username, pairing_revision: input.pairing_revision, accepted: false, code: "unavailable"};
  try {
    // Validate direct test/library callers just as strictly as stdin.
    decodeResetInput(Buffer.from(JSON.stringify(input) + "\n"));
    const current = readPairingManifest(descriptor, input.username);
    if (Object.keys(current).some(field => {
      const name = field as keyof PairingManifest;
      return current[name] !== input.pairing_manifest[name] && !(input.resume_cleanup && current[name] === null);
    })) return {...result, code: "conflict"};
    const store = new PairingStore(descriptor, input.username);
    try {
      store.initialize();
      Accessory.cleanupAccessoryData(input.username);
      fsyncSync(descriptor);
      if (Object.values(readPairingManifest(descriptor, input.username)).some(value => value !== null)) return result;
      return {...result, accepted: true, code: "ok"};
    } finally {store.close();}
  } catch {return result;}
}
async function run(): Promise<number> {
  if (process.version !== "v26.12.1" || process.platform !== "linux" || process.env.NODE_OPTIONS || process.env.NODE_PATH ||
      process.env.HALO_PARENT_PID !== String(process.ppid)) throw new Error("invalid-bootstrap");
  const descriptors = pairingDescriptors(process.env, 0, 1);
  if (descriptors.pairing === undefined || descriptors.pairingLock === undefined) throw new Error("invalid-bootstrap");
  verifyPublicApi();
  // This helper has no media/model authority; its parent already reaped the adapter.
  enablePairingDurability(() => {});
  const chunks: Buffer[] = [];let length = 0;
  const deadline = setTimeout(() => {process.stdin.destroy(new Error("timeout"));}, 5000);
  let input: ResetInput;
  try {
    for await (const chunk of process.stdin) {
      const bytes = Buffer.from(chunk as Uint8Array);length += bytes.length;
      if (length > 4096) throw new Error("invalid-request");chunks.push(bytes);
    }
    input = decodeResetInput(Buffer.concat(chunks));
  } finally {clearTimeout(deadline);}
  const result = resetPairing(descriptors.pairing, input);
  await new Promise<void>((resolve, reject) => process.stdout.write(JSON.stringify(result) + "\n", error => error ? reject(error) : resolve()));
  return result.accepted ? 0 : 1;
}
if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  void run().then(code => {process.exitCode = code;}).catch(() => {process.exitCode = 1;});
}
