import { createHash, createPrivateKey, createPublicKey, randomInt } from "node:crypto";
import { closeSync, constants, fstatSync, openSync, opendirSync, readSync } from "node:fs";
import { TextDecoder } from "node:util";
import { HAPStorage, HAP_STORAGE_INTENT } from "@homebridge/hap-nodejs";
import { ProtocolError, pairingManifestRevision, type PairingManifest } from "./protocol.js";

export const MAX_PAIRING_FILES = 1024;
export const STORAGE_RECOVERY_REQUIRED = "storage_recovery_required";
export const MAX_PAIRING_FILE_BYTES = 1024 * 1024;
export const MAX_PAIRING_BYTES = 32 * 1024 * 1024;
const basename = /^(AccessoryInfo|IdentifierCache|ControllerStorage)\.([0-9A-F]{12})\.json$/;
const temporary = /^\.(AccessoryInfo|IdentifierCache|ControllerStorage)\.[0-9A-F]{12}\.json\.[1-9][0-9]*\.tmp$/;
const record = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);
const integer = (value: unknown, minimum = 0): value is number =>
  typeof value === "number" && Number.isSafeInteger(value) && value >= minimum;
const hex = (value: unknown, length: number): boolean =>
  typeof value === "string" && value.length === length && /^[0-9a-f]+$/i.test(value);
const pin = (value: unknown): value is string => typeof value === "string" && /^\d{3}-\d{2}-\d{3}$/.test(value);
const safeText = (value: unknown, maximum: number): value is string =>
  typeof value === "string" && Array.from(value).length <= maximum && !/[\p{Cc}\p{Cs}]/u.test(value);

export const PAIRING_RECORDS = {accessory_info: "AccessoryInfo", controller_storage: "ControllerStorage", identifier_cache: "IdentifierCache"} as const;
export function pairingRevision(manifest: PairingManifest): string {
  return pairingManifestRevision(manifest);
}
function pairingSuffix(username: string): string {
  if (username.length !== 17 || !/^[0-9A-F][26AE](?::[0-9A-F]{2}){5}$/.test(username)) throw new ProtocolError("invalid-accessory-identity");
  return username.replaceAll(":", "");
}
function privateDirectory(descriptor: number): void {
  const metadata = fstatSync(descriptor);
  if (!metadata.isDirectory() || metadata.uid !== process.getuid?.() || (metadata.mode & 0o077) !== 0) throw new ProtocolError("pairing-store-unsafe");
}
function readPrivateRecord(descriptor: number, name: string): Buffer | null {
  let file: number;
  try { file = openSync(`/proc/self/fd/${descriptor}/${name}`, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK); }
  catch (error) { if ((error as NodeJS.ErrnoException).code === "ENOENT") return null; throw new ProtocolError("pairing-store-unsafe"); }
  try {
    const metadata = fstatSync(file);
    if (!metadata.isFile() || metadata.uid !== process.getuid?.() || metadata.nlink !== 1 || (metadata.mode & 0o077) !== 0 ||
        metadata.size < 1 || metadata.size > MAX_PAIRING_FILE_BYTES) throw new ProtocolError("pairing-store-unsafe");
    const bytes = Buffer.alloc(metadata.size + 1);
    let count = 0, read = 0;
    do {read = readSync(file, bytes, count, bytes.length - count, null); count += read;} while (read > 0 && count < bytes.length);
    if (count !== metadata.size) throw new ProtocolError("pairing-store-unsafe");
    return bytes.subarray(0, count);
  } finally {closeSync(file);}
}
/** Exact files only. Atomic renames during a live read are caught by reset's later CAS. */
export function readPairingManifest(descriptor: number, username: string): PairingManifest {
  privateDirectory(descriptor);
  const suffix = pairingSuffix(username), result = {} as PairingManifest;
  for (const [field, kind] of Object.entries(PAIRING_RECORDS) as [keyof PairingManifest, string][]) {
    const bytes = readPrivateRecord(descriptor, `${kind}.${suffix}.json`);
    if (bytes !== null) validateStoredRecord(kind, parseRecord(bytes));
    result[field] = bytes === null ? null : createHash("sha256").update(bytes).digest("hex");
  }
  return result;
}

function keyPairValid(secret: unknown, publicKey: unknown): boolean {
  if (!hex(secret, 128) || !hex(publicKey, 64)) return false;
  try {
    const secretBytes = Buffer.from(secret as string, "hex"), publicBytes = Buffer.from(publicKey as string, "hex");
    const key = createPrivateKey({key: Buffer.concat([Buffer.from("302e020100300506032b657004220420", "hex"), secretBytes.subarray(0, 32)]),
      format: "der", type: "pkcs8"});
    const derived = createPublicKey(key).export({format: "der", type: "spki"});
    return secretBytes.subarray(32).equals(publicBytes) && derived.subarray(-32).equals(publicBytes);
  } catch { return false; }
}

/** HAP swallows malformed JSON. Refuse it before the library can regenerate identities. */
function parseRecord(bytes: Buffer): Record<string, unknown> {
  try {
    const text = new TextDecoder("utf-8", {fatal: true, ignoreBOM: true}).decode(bytes);
    const value: unknown = JSON.parse(text);
    if (!record(value)) throw new Error();
    const stack: {keys: Set<string> | null; keyNext: boolean}[] = [];
    for (let index = 0; index < text.length; index++) {
      const char = text[index];
      if (char === '"') {
        const start = index;
        while (++index < text.length) {
          if (text[index] === "\\") index++;
          else if (text[index] === '"') break;
        }
        const frame = stack.at(-1);
        if (frame?.keys && frame.keyNext) {
          const key = JSON.parse(text.slice(start, index + 1)) as string;
          if (frame.keys.has(key) || ["__proto__", "constructor", "prototype"].includes(key)) throw new Error();
          frame.keys.add(key); frame.keyNext = false;
        }
      } else if (char === "{" || char === "[") {
        stack.push({keys: char === "{" ? new Set() : null, keyNext: true});
        if (stack.length > 32) throw new Error();
      } else if (char === "}" || char === "]") stack.pop();
      else if (char === "," && stack.at(-1)?.keys) stack.at(-1)!.keyNext = true;
    }
    const pending: unknown[] = [value];
    let nodes = 0;
    while (pending.length) {
      const next = pending.pop();
      if (++nodes > 100_000 || typeof next === "number" && !Number.isFinite(next)) throw new Error();
      if (next !== null && typeof next === "object") pending.push(...Object.values(next));
    }
    return value;
  } catch { throw new ProtocolError("pairing-store-corrupt"); }
}

function validateStoredRecord(kind: string, value: Record<string, unknown>): void {
  if (kind === "AccessoryInfo") {
    const clients = value.pairedClients, permissions = value.pairedClientsPermission;
    if (!keyPairValid(value.signSk, value.signPk) || !pin(value.pincode) ||
        !safeText(value.displayName, 256) || !integer(value.category, 1) || value.category > 255 ||
        !integer(value.configVersion, 1) || value.configVersion > 65535 ||
        !safeText(value.configHash, 256) || typeof value.setupID !== "string" || !/^[A-Z0-9]{4}$/.test(value.setupID) ||
        !safeText(value.lastFirmwareVersion, 128) || !record(clients) || !record(permissions) ||
        Object.keys(clients).length > 256 || Object.keys(clients).length !== Object.keys(permissions).length ||
        Object.entries(clients).some(([id, key]) => !safeText(id, 128) || !id || !hex(key, 64) ||
          !Object.hasOwn(permissions, id) || ![0, 1].includes(permissions[id] as number)) ||
        Object.keys(permissions).some(id => !Object.hasOwn(clients, id)) ||
        Object.keys(clients).length > 0 && !Object.values(permissions).includes(1)) {
      throw new ProtocolError("pairing-store-corrupt");
    }
  } else if (kind === "IdentifierCache") {
    if (!record(value.cache) || Object.keys(value.cache).length > 8192 ||
        Object.entries(value.cache).some(([key, id]) => !safeText(key, 512) || !integer(id, 1))) {
      throw new ProtocolError("pairing-store-corrupt");
    }
  } else if (kind === "ControllerStorage") {
    if (!record(value.accessories) || Object.keys(value.accessories).length > 256 ||
        Object.entries(value.accessories).some(([id, entries]) => !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(id) ||
          !Array.isArray(entries) || entries.length > 32 || entries.some(entry => !record(entry) ||
            !safeText(entry.type, 128) || !record(entry.controllerData)))) {
      throw new ProtocolError("pairing-store-corrupt");
    }
  }
}

/** One inherited, pinned Linux directory. Python holds its exclusive lock. */
export class PairingStore {
  readonly path: string;
  private initialized = false;
  private closed = false;
  private readonly pins = new Map<string, string>();

  constructor(private readonly descriptor: number, private readonly resettingUsername: string | null = null) {
    if (process.platform !== "linux" || !integer(descriptor, 3)) throw new ProtocolError("pairing-store-unavailable");
    this.path = `/proc/self/fd/${descriptor}`;
    if (resettingUsername !== null) pairingSuffix(resettingUsername);
    this.preflight();
  }

  preflight(): void {
    if (this.closed) throw new ProtocolError("pairing-store-unavailable");
    const uid = process.getuid?.();
    if (uid === undefined) throw new ProtocolError("pairing-store-unavailable");
    let total = 0;
    const loaded = new Map<string, Record<string, unknown>>();
    try {
      const directory = fstatSync(this.descriptor);
      if (!directory.isDirectory() || directory.uid !== uid || (directory.mode & 0o077) !== 0) throw new Error();
      const entries: string[] = [];
      const listing = opendirSync(this.path);
      try {
        let entry;
        while ((entry = listing.readSync()) !== null) {
          if (entries.length >= MAX_PAIRING_FILES) throw new Error();
          entries.push(entry.name);
        }
      } finally { listing.closeSync(); }
      for (const name of entries) {
        if (name === HAP_STORAGE_INTENT) throw new ProtocolError(STORAGE_RECOVERY_REQUIRED);
        const match = basename.exec(name);
        if (!match && !temporary.test(name)) throw new Error();
        const descriptor = openSync(`${this.path}/${name}`, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
        try {
          const metadata = fstatSync(descriptor);
          if (!metadata.isFile() || metadata.uid !== uid || metadata.nlink !== 1 || (metadata.mode & 0o077) !== 0 ||
              metadata.size < 1 || metadata.size > MAX_PAIRING_FILE_BYTES || (total += metadata.size) > MAX_PAIRING_BYTES) throw new Error();
          if (match) {
            // Bounded read of a pinned regular file; a concurrent growth cannot allocate arbitrarily.
            const allocation = Buffer.alloc(metadata.size + 1);
            let count = 0, read = 0;
            do { read = readSync(descriptor, allocation, count, allocation.length - count, null); count += read; }
            while (read > 0 && count < allocation.length);
            if (count !== metadata.size) throw new Error();
            const bytes = allocation.subarray(0, count);
            const value = parseRecord(bytes);
            validateStoredRecord(match[1]!, value); loaded.set(name, value);
          }
        } finally { closeSync(descriptor); }
      }
      for (const [name, value] of loaded) {
        const match = basename.exec(name)!;
        const identity = match[2]!;
        const resetting = this.resettingUsername !== null && identity === pairingSuffix(this.resettingUsername);
        if (!resetting && match[1] !== "AccessoryInfo" && !loaded.has(`AccessoryInfo.${identity}.json`)) throw new Error();
        if (match[1] === "AccessoryInfo") {
          if (!resetting && Object.keys(value.pairedClients as object).length && !loaded.has(`IdentifierCache.${identity}.json`)) throw new Error();
          this.pins.set(identity, value.pincode as string);
        }
      }
    } catch (error) {
      if (error instanceof ProtocolError) throw error;
      throw new ProtocolError("pairing-store-unsafe");
    }
  }

  initialize(): void {
    if (this.initialized) return;
    this.preflight();
    process.umask(0o077);
    HAPStorage.setCustomStoragePath(this.path);
    this.initialized = true;
  }

  pincode(username: string): string {
    if (!/^(?:[0-9A-F]{2}:){5}[0-9A-F]{2}$/.test(username)) throw new ProtocolError("invalid-accessory-identity");
    const key = username.replaceAll(":", "");
    let value = this.pins.get(key);
    if (value) return value;
    do {
      const digits = randomInt(100_000_000).toString().padStart(8, "0");
      if (/^(\d)\1{7}$/.test(digits) || ["12345678", "87654321"].includes(digits)) continue;
      value = `${digits.slice(0, 3)}-${digits.slice(3, 5)}-${digits.slice(5)}`;
    } while (!value);
    this.pins.set(key, value);
    return value;
  }

  manifest(username: string): PairingManifest {
    this.preflight();
    return readPairingManifest(this.descriptor, username);
  }

  close(): void {
    if (this.closed) return;
    this.closed = true; this.pins.clear();
    // HAP may still own a delayed store write after unpublish. Keep the pinned
    // directory and inherited flock descriptor alive until the child exits.
  }
}
