import assert from "node:assert/strict";
import { test, type TestContext } from "node:test";
import { Accessory, Characteristic, Service, type CharacteristicGetHandler } from "@homebridge/hap-nodejs";
import { CameraControls } from "../src/controls.js";
import { CameraAccessory } from "../src/camera.js";
import type { CameraDelegate, NativeCamera } from "../src/session.js";
const id="00000000-0000-0000-0000-000000000003";
const camera=(light=true,siren=true):NativeCamera=>({scope:{profile_id:"00000000-0000-0000-0000-000000000002",account_generation:1,camera_id:"front",device_generation:1,privacy_generation:1},
  name:"Fixture",family:"fixture",kind:"fixture",model:"Fixture",capabilities:{video:true,audio:false,snapshot:false,talkback:false,light,siren,doorbell:false,battery:false,
    evidence:["halo:video",...(light?["sdk:light"]:[]),...(siren?["sdk:siren"]:[])]},connection_state:null,battery_percent:null,observed_format:null});
const write=(service:Service,value:boolean):Promise<void>=>new Promise((resolve,reject)=>service.getCharacteristic(Characteristic.On).setValue(value,(error:unknown)=>error?reject(error):resolve()));

function fixture(context:TestContext) {
  const getters=new Map<Characteristic,CharacteristicGetHandler>();
  const onGet=Characteristic.prototype.onGet;
  context.mock.method(Characteristic.prototype,"onGet",function(this:Characteristic,handler:CharacteristicGetHandler){getters.set(this,handler);return onGet.call(this,handler);});
  const read=(service:Service):Promise<unknown>=>Promise.resolve().then(()=>getters.get(service.getCharacteristic(Characteristic.On))!(undefined));
  const accessory=new Accessory("Fixture",id);let active=true,sets=0,gets=0,syncs=0;
  let state:{light:boolean|null;siren:boolean|null}={light:false,siren:false},accepted=true;
  let gate:Promise<void>|null=null;
  const native={async controlState(){gets++;return {...state};},async setControl(control:"light"|"siren",enabled:boolean){sets++;if(accepted)state[control]=enabled;return accepted?"accepted":"unknown";}} as CameraDelegate;
  const controls=new CameraControls(accessory,camera(),native,()=>active,async()=>{syncs++;await gate;});
  return {accessory,controls,read,get sets(){return sets;},get gets(){return gets;},get syncs(){return syncs;},
    setState:(value:typeof state)=>{state=value;},setActive:(value:boolean)=>{active=value;},setAccepted:(value:boolean)=>{accepted=value;},setGate:(value:Promise<void>)=>{gate=value;}};
}
test("capability services construct and update without issuing control operations",context=>{
  const value=fixture(context);assert.ok(value.accessory.getService(Service.Lightbulb));assert.ok(value.accessory.getService(Service.Switch));
  value.controls.update(camera());assert.equal(value.sets,0);assert.equal(value.gets,0);assert.equal(value.syncs,0);
  value.controls.update(camera(false,false));assert.equal(value.accessory.getService(Service.Lightbulb),undefined);assert.equal(value.accessory.getService(Service.Switch),undefined);
  const unsupported=camera();unsupported.capabilities.evidence=["halo:video"];
  value.controls.update(unsupported);assert.equal(value.controls.available,false);
});
test("GET preserves unknown state as a fault instead of claiming the light is off",async context=>{
  const value=fixture(context);value.setState({light:null,siren:null});const light=value.accessory.getService(Service.Lightbulb)!;
  await assert.rejects(value.read(light));assert.equal(value.sets,0);
  assert.equal(light.getCharacteristic(Characteristic.StatusFault).value,Characteristic.StatusFault.GENERAL_FAULT);
  value.setState({light:true,siren:false});assert.equal(await value.read(light),true);assert.equal(value.sets,0);
});
test("explicit SET requires accepted actuation plus a current observed state",async context=>{
  const value=fixture(context),siren=value.accessory.getService(Service.Switch)!;
  await write(siren,true);assert.equal(value.sets,1);assert.equal(await value.read(siren),true);
  value.setAccepted(false);await assert.rejects(write(siren,false));assert.equal(value.sets,2);
  value.controls.retire();await assert.rejects(write(siren,true));assert.equal(value.sets,2);
});
test("a privacy change while policy synchronization waits prevents actuation",async context=>{
  const value=fixture(context);let release!:()=>void;value.setGate(new Promise<void>(resolve=>{release=resolve;}));
  const setting=write(value.accessory.getService(Service.Lightbulb)!,true),rejected=assert.rejects(setting);
  value.setActive(false);release();await rejected;assert.equal(value.sets,0);
});
test("camera policy keeps controls independent of RTP Active and denies them on retirement",()=>{
  const accessory=new CameraAccessory({camera_id:"front",accessory_id:id,username:"02:11:22:33:44:55",doorbell:false},camera());
  assert.equal(accessory.policy.controls,true);
  for(const service of accessory.accessory.services.filter(service=>service.UUID===Service.CameraRTPStreamManagement.UUID))
    service.updateCharacteristic(Characteristic.Active,false);
  assert.equal(accessory.policy.controls,true);assert.equal(accessory.policy.streaming,false);
  accessory.retire();assert.equal(accessory.policy.controls,false);
});
