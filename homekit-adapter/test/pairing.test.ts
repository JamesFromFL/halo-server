import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { chmodSync, closeSync, constants, existsSync, fsyncSync, mkdtempSync, openSync, readFileSync, rmSync, unlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test, type TestContext } from "node:test";
import { HAPFileStorage, HAPStorage } from "@homebridge/hap-nodejs";
import { AccessoryManager, type AccessoryPublisher } from "../src/accessories.js";
import { decodeResetInput, resetPairing, type ResetInput } from "../src/pairing-reset.js";
import { PairingStore, pairingRevision, readPairingManifest } from "../src/storage.js";
import { pairingSnapshot } from "../src/pairing.js";
import { validateReply, validateEnvelope, type PairingIdentity } from "../src/protocol.js";
import type { NativeCamera } from "../src/session.js";

const username = "02:11:22:33:44:55", suffix = "021122334455.json", id = "00000000-0000-0000-0000-000000000003";
const operation = "00000000-0000-0000-0000-000000000004";
const publicKey = "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a";
const info = (paired = false) => ({displayName: "Fixture", category: 18, pincode: "192-83-746",
  signSk: "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60" + publicKey, signPk: publicKey,
  pairedClients: paired ? {controller: "cc".repeat(32)} : {}, pairedClientsPermission: paired ? {controller: 1} : {},
  configVersion: 1, configHash: "", setupID: "ABCD", lastFirmwareVersion: "2.2.3"});
function fixture(context: TestContext, paired = false) {
  const root = mkdtempSync(join(tmpdir(), "halo-pairing-owner-"));chmodSync(root, 0o700);
  const descriptor = openSync(root, constants.O_RDONLY | constants.O_DIRECTORY | constants.O_NOFOLLOW);
  context.after(() => {closeSync(descriptor);rmSync(root, {recursive:true,force:true});});
  const write = (kind: string, value: unknown, target = suffix): void => writeFileSync(join(root, `${kind}.${target}`), JSON.stringify(value), {mode:0o600});
  write("AccessoryInfo",info(paired));write("IdentifierCache",{cache:{"|nextAID":2}});write("ControllerStorage",{accessories:{}});
  const storage = new HAPFileStorage();
  context.mock.method(HAPStorage,"setCustomStoragePath",(path:string)=>storage.initSync({dir:path}));
  context.mock.method(HAPStorage,"storage",()=>storage);
  const store = new PairingStore(descriptor);
  return {root,descriptor,store,write,manifest:()=>readPairingManifest(descriptor,username)};
}
const owner: PairingIdentity = {scope:{profile_id:"00000000-0000-0000-0000-000000000002",account_generation:1,camera_id:"front",device_generation:1,privacy_generation:1},
  registry_revision:1,accessory_id:id,username};
const camera = (): NativeCamera => ({scope:{...owner.scope},name:"Front",family:"fixture",kind:"fixture",model:"Fixture",
  capabilities:{video:true,audio:false,snapshot:false,talkback:false,light:false,siren:false,doorbell:false,battery:false,evidence:["halo:video"]},
  connection_state:null,battery_percent:null,observed_format:null});
async function managerFixture(context: TestContext, paired=false) {
  const value=fixture(context,paired);let quiesced=0;
  const publisher:AccessoryPublisher={mediaReady:true,async publish(){},async unpublish(){}};
  const session={cameras:()=>[camera()],inventoryComplete:true,accountScope:{profile_id:owner.scope.profile_id,account_generation:1},quiesce:()=>{quiesced++;}};
  const manager=new AccessoryManager({session,store:value.store,publisher,syncEvents:async()=>({scope:session.accountScope,epoch:"e",sequence:0,accepted:true,code:"ok"})});
  await manager.configure({scope:session.accountScope,revision:1,generation:1,page_index:0,total_pages:1,entries:[{camera_id:"front",accessory_id:id,username,doorbell:false,audio:null}],media_ready:true});
  context.after(async()=>{await manager.close().catch(()=>{});});
  return {...value,manager,publisher,session,get quiesced(){return quiesced;}};
}

test("pairing manifest uses the frozen sorted-key domain-separated digest",context=>{
  const value=fixture(context),manifest=value.manifest();
  const expected=createHash("sha256").update("halo-pairing-manifest-v1\0").update(JSON.stringify({accessory_info:manifest.accessory_info,
    controller_storage:manifest.controller_storage,identifier_cache:manifest.identifier_cache})).digest("hex");
  assert.equal(pairingRevision(manifest),expected);
  const snapshot=pairingSnapshot(value.store,username);assert.equal(snapshot.paired,false);assert.equal(snapshot.paired_clients,0);
  assert.equal(snapshot.pairing_revision,expected);value.store.close();
});

test("status excludes secrets and reveal requires exact published identity and unchanged pairing state",async context=>{
  const value=await managerFixture(context);const status=value.manager.pairingStatus(owner);
  assert.equal(status.accepted,true);assert.equal(status.published,true);assert.equal(Object.hasOwn(status,"pincode"),false);
  assert.equal(Object.hasOwn(status,"setup_uri"),false);
  context.mock.method(value.manager.accessories()[0]!.accessory,"setupURI",()=>"X-HM://000000000ABCD");
  const reveal=value.manager.pairingReveal({...owner,pairing_revision:status.pairing_revision!});
  assert.equal(reveal.accepted,true);assert.equal(reveal.pincode,"192-83-746");assert.equal(reveal.ttl_ms,60000);
  assert.equal(value.manager.pairingReveal({...owner,pairing_revision:"a".repeat(64)}).code,"conflict");
  for(const change of [{registry_revision:2},{accessory_id:operation},{username:"02:11:22:33:44:56"},{scope:{...owner.scope,privacy_generation:2}}]) {
    const rejected=value.manager.pairingReveal({...owner,...change,pairing_revision:status.pairing_revision!});
    assert.equal(rejected.accepted,false);assert.equal(rejected.pincode,null);assert.equal(rejected.setup_uri,null);
  }
});

test("an already paired accessory does not reveal a reusable setup secret",async context=>{
  const value=await managerFixture(context,true);const status=value.manager.pairingStatus(owner);
  assert.equal(status.paired,true);assert.equal(status.paired_clients,1);
  assert.equal(value.manager.pairingReveal({...owner,pairing_revision:status.pairing_revision!}).code,"policy-denied");
});

test("prepare reset quiesces permanently before unpublish and rejects a pairing change during cleanup",async context=>{
  const value=await managerFixture(context);const before=value.manager.pairingStatus(owner);
  value.publisher.unpublish=async()=>{assert.equal(value.quiesced,1);value.write("AccessoryInfo",{...info(),configVersion:2});};
  const result=await value.manager.preparePairingReset({...owner,pairing_revision:before.pairing_revision!});
  assert.equal(result.quiesced,true);assert.equal(result.accepted,false);assert.equal(result.code,"conflict");
  assert.equal(result.pairing_manifest,null);assert.equal(value.manager.pairingStatus(owner).accepted,false);
});

test("prepare reset preserves files and returns the exact manifest after successful quiescence",async context=>{
  const value=await managerFixture(context);const status=value.manager.pairingStatus(owner);
  const result=await value.manager.preparePairingReset({...owner,pairing_revision:status.pairing_revision!});
  assert.equal(result.accepted,true);assert.equal(result.quiesced,true);assert.equal(value.quiesced,1);
  assert.deepEqual(result.pairing_manifest,status.pairing_manifest);assert.equal(result.quiesced_revision,status.pairing_revision);
  assert.equal(existsSync(join(value.root,`AccessoryInfo.${suffix}`)),true);
});
function input(manifest:ReturnType<typeof readPairingManifest>):ResetInput {
  return {v:1,operation_id:operation,accessory_id:id,username,pairing_revision:pairingRevision(manifest),pairing_manifest:manifest,resume_cleanup:false};
}
test("offline exact reset uses public targeted cleanup and preserves another accessory byte-for-byte",context=>{
  const value=fixture(context),other="021122334456.json";
  value.write("AccessoryInfo",info(),other);value.write("IdentifierCache",{cache:{}},other);
  const before=readFileSync(join(value.root,`AccessoryInfo.${other}`)),request=input(value.manifest());
  assert.equal(resetPairing(value.descriptor,request).accepted,true);
  assert.ok(Object.values(value.manifest()).every(item=>item===null));assert.deepEqual(readFileSync(join(value.root,`AccessoryInfo.${other}`)),before);
  fsyncSync(value.descriptor);
});
test("offline reset rejects changed bytes and permits only original-or-absent crash recovery",context=>{
  const value=fixture(context),request=input(value.manifest());
  value.write("AccessoryInfo",{...info(),configVersion:2});assert.equal(resetPairing(value.descriptor,request).code,"conflict");
  value.write("AccessoryInfo",info());unlinkSync(join(value.root,`IdentifierCache.${suffix}`));
  assert.equal(resetPairing(value.descriptor,request).code,"conflict");
  assert.equal(resetPairing(value.descriptor,{...request,resume_cleanup:true}).accepted,true);
});
test("helper rejects duplicate fields, malformed manifests and noncanonical or oversized input",()=>{
  const request=input({accessory_info:null,controller_storage:null,identifier_cache:null});
  const text=JSON.stringify(request)+"\n";assert.deepEqual(decodeResetInput(Buffer.from(text)),request);
  for(const bad of [text.replace('"v":1','"v":1,"v":1'),text.replace('"v":1','"v": 1'),text.replace(request.pairing_revision,"a".repeat(64)),"x".repeat(4097)])
    assert.throws(()=>decodeResetInput(Buffer.from(bad)));
});
test("pairing replies bind stable identity, registry revision, expected digest and full scope",()=>{
  const envelope={v:1,boot_id:id,request_id:operation,kind:"pairing_status_request",payload:owner};
  const manifest={accessory_info:null,controller_storage:null,identifier_cache:null};
  const reply={...envelope,kind:"pairing_status_result",payload:{...owner,accepted:true,code:"ok",published:false,paired:false,paired_clients:0,
    pairing_revision:pairingRevision(manifest),pairing_manifest:manifest}};
  assert.doesNotThrow(()=>validateReply(envelope,reply));
  for(const change of [{registry_revision:2},{username:"02:11:22:33:44:56"},{scope:{...owner.scope,privacy_generation:2}}])
    assert.throws(()=>validateReply(envelope,{...reply,payload:{...reply.payload,...change}}));
  assert.throws(()=>validateEnvelope({...reply,payload:{...reply.payload,pairing_revision:"a".repeat(64)}}));
  assert.throws(()=>validateEnvelope({...reply,payload:{...reply.payload,pincode:"192-83-746"}}));
});
