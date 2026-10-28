import assert from "node:assert/strict";
import { closeSync, constants, mkdtempSync, openSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { HAPStorage, HAPStorageError, HAP_STORAGE_INTENT, type HAPStorageFailureHandler } from "@homebridge/hap-nodejs";
import { enablePairingDurability } from "../src/storage-boundary.js";
import { PairingStore } from "../src/storage.js";

test("public durability boundary enables the guard and fences before immediate process retirement",context=>{
  let callback:HAPStorageFailureHandler|undefined;const actions:string[]=[];
  context.mock.method(HAPStorage,"setDurabilityGuard",(enabled:boolean)=>{assert.equal(enabled,true);actions.push("guard");});
  context.mock.method(HAPStorage,"setFailureHandler",(handler:HAPStorageFailureHandler)=>{callback=handler;actions.push("handler");});
  context.mock.method(process.stderr,"write",(value:unknown)=>{assert.equal(value,"Halo HAP adapter: storage_recovery_required\n");actions.push("code");return true;});
  const stopped=new Error("synthetic process exit");
  context.mock.method(process,"exit",(code?:number|string|null)=>{assert.equal(code,1);actions.push("exit");throw stopped;});
  enablePairingDurability(()=>{actions.push("fence");});
  assert.throws(()=>callback!(new HAPStorageError("write",true)),stopped);
  assert.deepEqual(actions,["guard","handler","fence","code","exit"]);
});

test("a throwing local fence still retires the process and cannot continue cached authority",context=>{
  let callback:HAPStorageFailureHandler|undefined,exited=false;
  context.mock.method(HAPStorage,"setDurabilityGuard",()=>{});
  context.mock.method(HAPStorage,"setFailureHandler",(handler:HAPStorageFailureHandler)=>{callback=handler;});
  context.mock.method(process.stderr,"write",()=>true);
  context.mock.method(process,"exit",()=>{exited=true;throw new Error("exit");});
  enablePairingDurability(()=>{throw new Error("fence");});
  assert.throws(()=>callback!(new HAPStorageError("remove",true)),/exit/);assert.equal(exited,true);
});

test("retained intent is preserved and refused before HAP reads storage",context=>{
  const directory=mkdtempSync(join(tmpdir(),"halo-intent-preflight-")),file=join(directory,HAP_STORAGE_INTENT);
  const fd=openSync(directory,constants.O_RDONLY|constants.O_DIRECTORY|constants.O_NOFOLLOW);
  context.after(()=>{closeSync(fd);rmSync(directory,{recursive:true,force:true});});
  writeFileSync(file,'{"version":1,"operation":"remove"}',{mode:0o600});const before=readFileSync(file);
  let initialized=false;context.mock.method(HAPStorage,"setCustomStoragePath",()=>{initialized=true;});
  assert.throws(()=>new PairingStore(fd),/storage_recovery_required/);
  assert.equal(initialized,false);assert.deepEqual(readFileSync(file),before);
});

test("diagnostic output failure cannot bypass immediate process retirement",context=>{
  let callback:HAPStorageFailureHandler|undefined,exited=false;
  context.mock.method(HAPStorage,"setDurabilityGuard",()=>{});
  context.mock.method(HAPStorage,"setFailureHandler",(handler:HAPStorageFailureHandler)=>{callback=handler;});
  context.mock.method(process.stderr,"write",()=>{throw new Error("closed stderr");});
  context.mock.method(process,"exit",()=>{exited=true;throw new Error("exit");});
  enablePairingDurability(()=>{});
  assert.throws(()=>callback!(new HAPStorageError("write",true)),/exit/);assert.equal(exited,true);
});
