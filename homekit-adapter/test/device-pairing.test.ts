import assert from "node:assert/strict";
import {
  chmodSync,
  closeSync,
  constants,
  mkdtempSync,
  openSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test, type TestContext } from "node:test";
import { HAPFileStorage, HAPStorage } from "@homebridge/hap-nodejs";
import { DeviceBridgeManager } from "../src/device-bridge.js";
import {
  validateDeviceEnvelope,
  type DevicePairingIdentity,
} from "../src/device-protocol.js";
import { PairingStore } from "../src/storage.js";
import {
  deviceBridgeUuid,
  deviceProfile,
  devicePublication,
  DeviceFakePublisher,
} from "./device-fixtures.js";
const username = "02:11:22:33:44:55",
  suffix = "021122334455.json";
const identity: DevicePairingIdentity = {
  scope: { profile_id: deviceProfile, account_generation: 1 },
  registry_revision: 1,
  accessory_uuid: deviceBridgeUuid,
  username,
};
const publicKey =
  "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a";
const info = (paired = false) => ({
  displayName: "Halo Bridge",
  category: 2,
  pincode: "192-83-746",
  signSk:
    "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60" +
    publicKey,
  signPk: publicKey,
  pairedClients: paired ? { controller: "cc".repeat(32) } : {},
  pairedClientsPermission: paired ? { controller: 1 } : {},
  configVersion: 1,
  configHash: "",
  setupID: "ABCD",
  lastFirmwareVersion: "2.3.0",
});
async function fixture(context: TestContext, paired = false) {
  const root = mkdtempSync(join(tmpdir(), "halo-device-pairing-"));
  chmodSync(root, 0o700);
  const descriptor = openSync(
    root,
    constants.O_RDONLY | constants.O_DIRECTORY | constants.O_NOFOLLOW,
  );
  const write = (kind: string, value: unknown): void =>
    writeFileSync(join(root, `${kind}.${suffix}`), JSON.stringify(value), {
      mode: 0o600,
    });
  write("AccessoryInfo", info(paired));
  write("IdentifierCache", { cache: { "|nextAID": 2 } });
  write("ControllerStorage", { accessories: {} });
  const storage = new HAPFileStorage();
  context.mock.method(HAPStorage, "setCustomStoragePath", (path: string) =>
    storage.initSync({ dir: path }),
  );
  context.mock.method(HAPStorage, "storage", () => storage);
  const store = new PairingStore(descriptor),
    publisher = new DeviceFakePublisher(),
    manager = new DeviceBridgeManager({
      store,
      publisher,
      monotonicNow: () => 1,
    });
  context.after(async () => {
    await manager.close();
    closeSync(descriptor);
    rmSync(root, { recursive: true, force: true });
  });
  await manager.publish(devicePublication());
  context.mock.method(
    manager.accessory!,
    "setupURI",
    () => "X-HM://000000000ABCD",
  );
  return { manager, publisher, write };
}
const validates = (
  kind: "status_result" | "reveal_result" | "reset_prepare_result",
  payload: object,
): void => {
  assert.doesNotThrow(() =>
    validateDeviceEnvelope(
      {
        v: 1,
        kind,
        boot_id: deviceProfile,
        request_id: deviceBridgeUuid,
        payload,
      },
      "device-hap",
    ),
  );
};
test("device pairing status excludes secrets and reveal matches exact published account and pairing revision", async (context) => {
  const value = await fixture(context),
    status = value.manager.status(identity);
  assert.equal(status.accepted, true);
  assert.equal(status.published, true);
  assert.equal(status.paired, false);
  assert.ok(!Object.hasOwn(status, "pincode"));
  validates("status_result", status);
  const reveal = value.manager.reveal({
    ...identity,
    pairing_revision: status.pairing_revision!,
  });
  assert.equal(reveal.pincode, "192-83-746");
  assert.equal(reveal.setup_uri, "X-HM://000000000ABCD");
  validates("reveal_result", reveal);
  const rejected = value.manager.reveal({
    ...identity,
    pairing_revision: "a".repeat(64),
  });
  assert.equal(rejected.code, "conflict");
  assert.equal(rejected.pincode, null);
  validates("reveal_result", rejected);
  for (const patch of [
    { registry_revision: 2 },
    { accessory_uuid: "00000000-0000-4000-8000-000000000099" },
    { username: "02:11:22:33:44:56" },
    { scope: { ...identity.scope, account_generation: 2 } },
  ]) {
    const rejected = value.manager.status({ ...identity, ...patch });
    assert.equal(rejected.accepted, false);
    assert.equal(rejected.pairing_manifest, null);
    validates("status_result", rejected);
  }
});
test("paired bridges deny revealing reusable setup credentials", async (context) => {
  const value = await fixture(context, true),
    status = value.manager.status(identity);
  assert.equal(status.paired, true);
  assert.equal(status.paired_clients, 1);
  const rejected = value.manager.reveal({
    ...identity,
    pairing_revision: status.pairing_revision!,
  });
  assert.equal(rejected.accepted, false);
  assert.equal(rejected.code, "policy-denied");
  assert.equal(rejected.pincode, null);
  validates("reveal_result", rejected);
});
test("prepare reset quiesces before cleanup and remains fenced until process retirement", async (context) => {
  const value = await fixture(context),
    status = value.manager.status(identity);
  let fenced = false;
  value.publisher.unpublish = async () => {
    fenced = !value.manager.published;
  };
  const prepared = await value.manager.prepareReset({
    ...identity,
    pairing_revision: status.pairing_revision!,
  });
  assert.equal(fenced, true);
  assert.equal(prepared.accepted, true);
  assert.equal(prepared.quiesced, true);
  assert.deepEqual(prepared.pairing_manifest, status.pairing_manifest);
  validates("reset_prepare_result", prepared);
  await assert.rejects(
    value.manager.publish(devicePublication({ sequence: 2 })),
    /not-ready/,
  );
  assert.equal(value.manager.status(identity).accepted, false);
});
test("pairing reset conflicts before cleanup and detects manifest change during unpublish", async (context) => {
  const value = await fixture(context),
    status = value.manager.status(identity);
  const early = await value.manager.prepareReset({
    ...identity,
    pairing_revision: "b".repeat(64),
  });
  assert.equal(early.accepted, false);
  assert.equal(early.quiesced, false);
  assert.equal(value.publisher.unpublished.length, 0);
  validates("reset_prepare_result", early);
  value.publisher.unpublish = async () =>
    value.write("AccessoryInfo", { ...info(), configVersion: 2 });
  const late = await value.manager.prepareReset({
    ...identity,
    pairing_revision: status.pairing_revision!,
  });
  assert.equal(late.accepted, false);
  assert.equal(late.quiesced, true);
  assert.equal(late.code, "conflict");
  assert.equal(late.pairing_manifest, null);
  validates("reset_prepare_result", late);
});
test("malformed pairing success cannot become IPC authority", () => {
  const base = {
    ...identity,
    accepted: true,
    code: "ok",
    published: true,
    paired: false,
    paired_clients: 0,
    pairing_revision: "a".repeat(64),
  };
  for (const payload of [
    { ...base, pairing_manifest: null },
    { ...base, paired: null, paired_clients: null, pairing_manifest: null },
  ])
    assert.throws(() =>
      validateDeviceEnvelope({
        v: 1,
        kind: "status_result",
        boot_id: deviceProfile,
        request_id: deviceBridgeUuid,
        payload,
      }),
    );
  for (const patch of [
    { pincode: null },
    { setup_uri: "X-HM://A" },
    { ttl_ms: 0 },
    { paired: true, paired_clients: 1 },
  ])
    assert.throws(() =>
      validateDeviceEnvelope({
        v: 1,
        kind: "reveal_result",
        boot_id: deviceProfile,
        request_id: deviceBridgeUuid,
        payload: {
          ...base,
          pincode: "192-83-746",
          setup_uri: "X-HM://000000000ABCD",
          ttl_ms: 60000,
          ...patch,
        },
      }),
    );
});
