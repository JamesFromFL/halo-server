import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { AccessoryInfo, DataStreamParser, DataStreamReader, DataStreamWriter, HAPEncryption,
  HAPFileStorage, HAPStorage } from "@homebridge/hap-nodejs";
// Qualification of the installed artifact's crypto implementation; production uses public HAP APIs.
import { ed25519Sign, ed25519Verify, layerDecrypt, layerEncrypt } from "@homebridge/hap-nodejs/dist/lib/util/hapCrypto.js";

const publicKey = "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a";
const seed = "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60";

test("2.3 native crypto signs with the existing 2.2 pairing-store secret without rewriting it", context => {
  const directory = mkdtempSync(join(tmpdir(), "halo-hap230-pairing-"));
  context.after(() => rmSync(directory, { recursive: true, force: true }));
  const storage = new HAPFileStorage(); storage.initSync({ dir: directory });
  storage.setItemSync("AccessoryInfo.021122334455.json", {
    displayName: "RFC8032 fixture", category: 18, pincode: "192-83-746",
    signSk: seed + publicKey, signPk: publicKey, pairedClients: {}, pairedClientsPermission: {},
    configVersion: 1, configHash: "", setupID: "ABCD", lastFirmwareVersion: "2.2.3",
  });
  context.mock.method(HAPStorage, "storage", () => storage);
  const file = join(directory, "AccessoryInfo.021122334455.json"), original = readFileSync(file);
  const info = AccessoryInfo.load("02:11:22:33:44:55"); assert.ok(info);
  const message = Buffer.alloc(0), signature = ed25519Sign(message, info.signSk);
  assert.equal(signature.toString("hex"), "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b");
  assert.equal(ed25519Verify(message, signature, info.signPk), true);
  assert.equal(ed25519Verify(Buffer.from("altered"), signature, info.signPk), false);
  assert.equal(ed25519Verify(message, signature.subarray(1), info.signPk), false);
  assert.throws(() => ed25519Sign(message, info.signSk.subarray(0, 32)));
  assert.deepEqual(readFileSync(file), original);
});

test("installed HDS parser keeps corrected short binary and tag-only back-reference semantics", () => {
  for (const length of [0, 1, 3, 32]) {
    const data = { streamId: 1, data: Buffer.alloc(length, 0xab), type: "repeat", again: "repeat" };
    const writer = new DataStreamWriter(128); DataStreamParser.encode(data, writer);
    assert.deepEqual(DataStreamParser.decode(new DataStreamReader(writer.getData())), data);
  }
  assert.deepEqual(DataStreamParser.decode(new DataStreamReader(Buffer.from("e24161014162a1", "hex"))), { a: true, b: "b" });
  assert.throws(() => DataStreamParser.encode({ data: Buffer.alloc(100) }, new DataStreamWriter(8)), /budget/);
});

test("installed HAP encrypted stream retains a split length header and the next frame nonce", () => {
  const empty = Buffer.alloc(0), key = Buffer.alloc(32, 7);
  const sender = new HAPEncryption(empty, empty, empty, empty, empty);
  const receiver = new HAPEncryption(empty, empty, empty, empty, empty);
  sender.accessoryToControllerKey = receiver.controllerToAccessoryKey = key;
  const first = layerEncrypt(Buffer.from("first control response"), sender);
  assert.equal(layerDecrypt(first.subarray(0, 1), receiver).length, 0);
  assert.equal(layerDecrypt(first.subarray(1), receiver).toString(), "first control response");
  const second = layerEncrypt(Buffer.from("second control response"), sender);
  assert.equal(layerDecrypt(second, receiver).toString(), "second control response");
});
