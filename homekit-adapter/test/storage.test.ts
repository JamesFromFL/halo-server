import assert from "node:assert/strict";
import {
  chmodSync, closeSync, constants, existsSync, fstatSync, ftruncateSync, linkSync, mkdirSync, mkdtempSync,
  openSync, readFileSync, renameSync, rmSync, symlinkSync, writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { HAPStorage } from "@homebridge/hap-nodejs";
import { MAX_PAIRING_FILE_BYTES, PairingStore } from "../src/storage.js";
import { pairingDescriptors } from "../src/main.js";

const username = "02:11:22:33:44:55", suffix = "021122334455.json";
// RFC8032 public test vector, never an account or an actual HomeKit accessory.
const publicKey = "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a";
const information = () => ({displayName: "Fixture", category: 18, pincode: "192-83-746",
  signSk: "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60" + publicKey,
  signPk: publicKey, pairedClients: {}, pairedClientsPermission: {}, configVersion: 1, configHash: "",
  setupID: "ABCD", lastFirmwareVersion: "2.2.3"});
function directory(context: {after: (callback: () => void) => void}): {root: string; descriptor: number; write: (name: string, value: string | Buffer) => void} {
  const root = mkdtempSync(join(tmpdir(), "halo-hap-store-")); chmodSync(root, 0o700);
  const descriptor = openSync(root, constants.O_RDONLY | constants.O_DIRECTORY | constants.O_NOFOLLOW);
  context.after(() => { try { closeSync(descriptor); } catch { /* PairingStore owns a successful descriptor. */ }
    rmSync(root, {recursive: true, force: true}); });
  return {root, descriptor, write: (name, value) => writeFileSync(join(root, name), value, {mode: 0o600})};
}

test("pairing preflight validates before HAP initialization and reuses a private Node-owned PIN", context => {
  const value = directory(context);
  value.write(`AccessoryInfo.${suffix}`, JSON.stringify(information()));
  value.write(`IdentifierCache.${suffix}`, JSON.stringify({cache: {"|nextAID": 2}}));
  value.write(`ControllerStorage.${suffix}`, JSON.stringify({accessories: {}}));
  const storageCalls: string[] = [];
  context.mock.method(HAPStorage, "setCustomStoragePath", (path: string) => storageCalls.push(path));
  const store = new PairingStore(value.descriptor);
  assert.deepEqual(storageCalls, []);
  assert.equal(store.pincode(username), information().pincode);
  const before = process.umask();
  try { store.initialize(); store.initialize(); } finally { process.umask(before); }
  assert.deepEqual(storageCalls, [`/proc/self/fd/${value.descriptor}`]);
  const generated = store.pincode("02:11:22:33:44:56");
  assert.match(generated, /^\d{3}-\d{2}-\d{3}$/);
  assert.equal(store.pincode("02:11:22:33:44:56"), generated);
  assert.throws(() => store.pincode("../escape"), /invalid-accessory-identity/);
  store.close(); store.close(); assert.equal(fstatSync(value.descriptor).isDirectory(), true);
  assert.throws(() => store.preflight(), /pairing-store-unavailable/);
});

for (const [name, content] of [
  ["invalid JSON", "{"], ["duplicate key", '{"signSk":"aa","signSk":"bb"}'], ["primitive", "null"],
  ["invalid UTF8", Buffer.from([0xff])], ["nonfinite", '{"number":1e9999}'],
  ["BOM", "\uFEFF" + JSON.stringify(information())],
  ["prototype key", '{"__proto__":{}}'],
  ["missing keys", JSON.stringify({signSk: "aa"})],
  ["mismatched signing key", JSON.stringify({...information(), signPk: "aa".repeat(32)})],
  ["deep JSON", '{"nested":' + "[".repeat(33) + "0" + "]".repeat(33) + "}"],
] as const) test(`existing pairing ${name} fails visibly without altering files or touching HAP`, context => {
  const value = directory(context), file = `AccessoryInfo.${suffix}`;
  value.write(file, content);
  let initialized = false;
  context.mock.method(HAPStorage, "setCustomStoragePath", () => { initialized = true; });
  const before = readFileSync(join(value.root, file));
  assert.throws(() => new PairingStore(value.descriptor), /pairing-store-(corrupt|unsafe)/);
  assert.equal(initialized, false); assert.deepEqual(readFileSync(join(value.root, file)), before);
});

test("pairing preflight rejects public directories/files, unexpected entries, links and oversized files", async context => {
  for (const variant of ["directory", "file", "unknown", "hidden", "symlink", "hardlink", "oversize"] as const) {
    await context.test(variant, subcontext => {
      const value = directory(subcontext), file = join(value.root, `AccessoryInfo.${suffix}`);
      if (variant === "directory") chmodSync(value.root, 0o755);
      else if (variant === "unknown" || variant === "hidden") value.write(variant === "hidden" ? ".unknown" : "unknown.json", "{}");
      else if (variant === "symlink") symlinkSync("/dev/null", file);
      else {
        value.write(`AccessoryInfo.${suffix}`, JSON.stringify(information()));
        if (variant === "file") chmodSync(file, 0o644);
        if (variant === "hardlink") linkSync(file, join(value.root, `AccessoryInfo.021122334456.json`));
        if (variant === "oversize") {
          const descriptor = openSync(file, constants.O_WRONLY); ftruncateSync(descriptor, MAX_PAIRING_FILE_BYTES + 1); closeSync(descriptor);
        }
      }
      assert.throws(() => new PairingStore(value.descriptor), /pairing-store-unsafe/);
      assert.equal(existsSync(value.root), true);
    });
  }
});

test("paired identities never silently regain default permissions or lose their identifier cache", async context => {
  for (const variant of ["missing-permissions", "no-admin", "missing-identifiers", "orphan-cache"] as const) {
    await context.test(variant, subcontext => {
      const value = directory(subcontext);
      const info = {...information(), pairedClients: {controller: "cc".repeat(32)}, pairedClientsPermission: {controller: 1}};
      if (variant === "missing-permissions") info.pairedClientsPermission = {} as {controller: number};
      if (variant === "no-admin") info.pairedClientsPermission.controller = 0;
      if (variant !== "orphan-cache") value.write(`AccessoryInfo.${suffix}`, JSON.stringify(info));
      if (variant !== "missing-identifiers") value.write(`IdentifierCache.${suffix}`, JSON.stringify({cache: {"|nextAID": 2}}));
      assert.throws(() => new PairingStore(value.descriptor), /pairing-store-(corrupt|unsafe)/);
    });
  }
});

test("directory pin survives path replacement and interrupted temporary writes remain untouched", context => {
  const root = mkdtempSync(join(tmpdir(), "halo-hap-pin-")), original = join(root, "original"), moved = join(root, "moved");
  mkdirSync(original, {mode: 0o700});
  const descriptor = openSync(original, constants.O_RDONLY | constants.O_DIRECTORY | constants.O_NOFOLLOW);
  context.after(() => { try { closeSync(descriptor); } catch { /* Owned by PairingStore. */ } rmSync(root, {recursive: true, force: true}); });
  const temporary = `.AccessoryInfo.${suffix}.123.tmp`;
  writeFileSync(join(original, temporary), "{", {mode: 0o600});
  renameSync(original, moved); symlinkSync("/dev/null", original);
  const store = new PairingStore(descriptor);
  assert.equal(store.path, `/proc/self/fd/${descriptor}`);
  assert.equal(readFileSync(join(moved, temporary), "utf8"), "{");
  store.close();
});

test("malformed controller accessory UUIDs fail before HAP can load controller state", context => {
  const value = directory(context);
  value.write(`AccessoryInfo.${suffix}`, JSON.stringify(information()));
  value.write(`ControllerStorage.${suffix}`, JSON.stringify({accessories: {"------------------------------------": []}}));
  assert.throws(() => new PairingStore(value.descriptor), /pairing-store-corrupt/);
});

test("pairing bootstrap requires a distinct inherited private lock and retains its descriptor", context => {
  const value = directory(context), lockPath = join(value.root, "owner.lock");
  const lock = openSync(lockPath, constants.O_CREAT | constants.O_RDWR | constants.O_NOFOLLOW, 0o600);
  context.after(() => closeSync(lock));
  const environment = {HALO_PAIRING_FD: String(value.descriptor), HALO_PAIRING_LOCK_FD: String(lock)};
  assert.deepEqual(pairingDescriptors({}, 9000, 9001), {});
  assert.deepEqual(pairingDescriptors(environment, 9000, 9001), {pairing: value.descriptor, pairingLock: lock});
  assert.equal(fstatSync(lock).isFile(), true);
  for (const invalid of [
    {HALO_PAIRING_FD: String(value.descriptor)}, {HALO_PAIRING_LOCK_FD: String(lock)},
    {...environment, HALO_PAIRING_LOCK_FD: String(value.descriptor)},
    {...environment, HALO_PAIRING_LOCK_FD: "09000"},
    {...environment, HALO_PAIRING_LOCK_FD: "9000"},
  ]) assert.throws(() => pairingDescriptors(invalid, 9000, 9001));
  chmodSync(lockPath, 0o644); assert.throws(() => pairingDescriptors(environment, 9000, 9001), /invalid-bootstrap/);
  chmodSync(lockPath, 0o600); linkSync(lockPath, join(value.root, "owner-copy.lock"));
  assert.throws(() => pairingDescriptors(environment, 9000, 9001), /invalid-bootstrap/);
});
