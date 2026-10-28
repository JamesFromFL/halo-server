import assert from 'node:assert/strict';
import {randomUUID} from 'node:crypto';
import {test} from 'node:test';
import {HAPStatus, ResourceRequestReason} from '@homebridge/hap-nodejs';
import {CameraAccessory} from '../src/camera.js';
import {HapVideoDelegate} from '../src/video.js';
import {AdapterSession, type CameraDelegate, type NativeCamera, type SnapshotReason} from '../src/session.js';
import {validateDirection, type CameraPolicy, type Envelope, type JsonObject, type Kind} from '../src/protocol.js';
const image=Buffer.from([0xff,0xd8,0x01,0xff,0xd9]);
const tick=():Promise<void>=>new Promise(resolve=>setImmediate(resolve));
function fixture() {
  let active=true, invalid=false;
  const info:NativeCamera={scope:{profile_id:'00000000-0000-0000-0000-000000000001',account_generation:1,camera_id:'front',device_generation:1,privacy_generation:1},
    name:'Fixture',family:'fixture',kind:'fixture',model:'Fixture',capabilities:{video:true,audio:false,snapshot:true,talkback:false,light:false,siren:false,doorbell:false,battery:false,evidence:['halo:snapshot']},
    connection_state:null,battery_percent:null,observed_format:null};
  const calls:{width:number;height:number;reason:SnapshotReason|undefined;signal:AbortSignal|undefined}[]=[],policies:CameraPolicy[]=[];
  let handler: (signal:AbortSignal|undefined)=>Promise<Buffer> = async()=>image;
  const native={get info(){if(invalid)throw new Error('retired');return structuredClone(info);},
    ensurePolicy:async(policy:CameraPolicy)=>{policies.push(policy);},
    snapshot:async(width:number,height:number,reason?:SnapshotReason,signal?:AbortSignal)=>{calls.push({width,height,reason,signal});return handler(signal);}} as unknown as CameraDelegate;
  const delegate=new HapVideoDelegate(native,{active:()=>active,forceStop:()=>{},failure:()=>{}});
  return {native,info,delegate,calls,policies,setActive:(value:boolean)=>{active=value;},invalidate:()=>{invalid=true;},
    handler:(value:typeof handler)=>{handler=value;}};
}
const snapshot=(delegate:HapVideoDelegate,reason?:ResourceRequestReason,width=640,height=480):Promise<Buffer>=>
  new Promise((resolve,reject)=>delegate.handleSnapshotRequest({width,height,...(reason===undefined?{}:{reason})},(error,bytes)=>error?reject(new Error(String(error))):resolve(bytes!)));

for (const [reason,expected] of [[ResourceRequestReason.PERIODIC,'periodic'],[ResourceRequestReason.EVENT,'event'],[undefined,'unknown'],[99 as ResourceRequestReason,'unknown']] as const) {
  test(`released public snapshot reason ${String(reason)} maps to ${expected} without inventing explicit requests`,async()=>{
    const f=fixture();assert.deepEqual(await snapshot(f.delegate,reason),image);
    assert.equal(f.calls[0]!.reason,expected);assert.equal(f.policies[0]!.snapshots,true);assert.equal(f.policies[0]!.recording,false);
    await f.delegate.close();
  });
}

test('real public CameraController invokes bounded JPEG delegate without publication',async()=>{
  const f=fixture();const camera=new CameraAccessory({camera_id:'front',accessory_id:'00000000-0000-0000-0000-000000000002',username:'02:11:22:33:44:55',doorbell:false,audio:null},f.info,{native:f.native});
  const bytes=await camera.controller.handleSnapshotRequest(240,320,undefined,ResourceRequestReason.EVENT);
  assert.deepEqual(bytes,image);assert.deepEqual([f.calls[0]!.width,f.calls[0]!.height,f.calls[0]!.reason],[320,240,'event']);
  camera.retire();await camera.closeStreams();
});

test('snapshot fence aborts native work and invokes each callback once even if a late JPEG arrives',async()=>{
  const f=fixture();let release!:(bytes:Buffer)=>void;
  f.handler(()=>new Promise<Buffer>(yes=>{release=yes;}));const answers:(Error|HAPStatus|undefined)[]=[];
  f.delegate.handleSnapshotRequest({width:640,height:480},error=>{answers.push(error);});await tick();
  assert.equal(f.calls.length,1);await f.delegate.fence();assert.equal(f.calls[0]!.signal!.aborted,true);
  assert.deepEqual(answers,[HAPStatus.SERVICE_COMMUNICATION_FAILURE]);release(image);await tick();assert.equal(answers.length,1);await f.delegate.close();
});

test('whole public snapshot deadline includes policy waiting and cannot return a late result',async context=>{
  context.mock.timers.enable({apis:['setTimeout']});const f=fixture();let release!:(bytes:Buffer)=>void;
  f.handler(()=>new Promise<Buffer>(yes=>{release=yes;}));let calls=0,error:Error|HAPStatus|undefined;
  f.delegate.handleSnapshotRequest({width:640,height:480},value=>{calls++;error=value;});await tick();
  context.mock.timers.tick(20_000);await tick();assert.equal(error,HAPStatus.OPERATION_TIMED_OUT);assert.equal(calls,1);assert.equal(f.calls[0]!.signal!.aborted,true);
  release(image);await tick();assert.equal(calls,1);await f.delegate.close();
});

test('full privacy scope change discards a completed snapshot before its callback',async()=>{
  const f=fixture();let release!:(bytes:Buffer)=>void;f.handler(()=>new Promise<Buffer>(yes=>{release=yes;}));
  const pending=snapshot(f.delegate);const rejected=assert.rejects(pending);await tick();f.info.scope.privacy_generation++;release(image);await rejected;await f.delegate.close();
});

test('snapshot callback rejects invalid sizes, stale delegates, disabled cameras and non-JPEG output',async()=>{
  for(const [width,height] of [[0,480],[4097,1],[4096,4096],[1.5,1]]) {const f=fixture();await assert.rejects(snapshot(f.delegate,undefined,width,height));assert.equal(f.calls.length,0);await f.delegate.close();}
  const stale=fixture();stale.invalidate();await assert.rejects(snapshot(stale.delegate));await stale.delegate.close();
  const inactive=fixture();inactive.setActive(false);await assert.rejects(snapshot(inactive.delegate));assert.equal(inactive.calls.length,0);await inactive.delegate.close();
  const bad=fixture();bad.handler(async()=>Buffer.from('not a JPEG'));await assert.rejects(snapshot(bad.delegate));await bad.delegate.close();
});

test('two snapshot callers remain separately owned and one completion does not retire the other',async()=>{
  const f=fixture(),releases:((bytes:Buffer)=>void)[]=[];f.handler(()=>new Promise<Buffer>(yes=>{releases.push(yes);}));
  const first=snapshot(f.delegate,ResourceRequestReason.EVENT),second=snapshot(f.delegate,ResourceRequestReason.EVENT);await tick();
  await assert.rejects(snapshot(f.delegate));assert.equal(f.calls.length,2);
  releases[0]!(image);assert.deepEqual(await first,image);assert.equal(f.calls[1]!.signal!.aborted,false);
  releases[1]!(image);assert.deepEqual(await second,image);await f.delegate.close();
});

test('whole public deadline also fences an unresolved policy synchronization before image acquisition',async context=>{
  context.mock.timers.enable({apis:['setTimeout']});const f=fixture();let release!:()=>void;
  (f.native as unknown as {ensurePolicy:(policy:CameraPolicy)=>Promise<void>}).ensurePolicy=()=>new Promise<void>(yes=>{release=yes;});
  let calls=0;f.delegate.handleSnapshotRequest({width:640,height:480},()=>{calls++;});await tick();
  context.mock.timers.tick(20_000);await tick();assert.equal(calls,1);assert.equal(f.calls.length,0);
  release();await tick();assert.equal(calls,1);assert.equal(f.calls.length,0);await f.delegate.close();
});

async function snapshotPeer() {
  const boot = randomUUID(), info = fixture().info;
  info.capabilities.evidence.push('halo:video');
  const account = {profile_id: info.scope.profile_id, account_generation: info.scope.account_generation};
  const sent: Envelope[] = [], failures: string[] = [];
  const session = new AdapterSession(boot, {send: async message => {
    validateDirection(message, 'hap'); sent.push(message);
  }, onFailure: error => failures.push(error.code)});
  const request = async (kind: Kind): Promise<Envelope> => {
    for (let turn = 0; turn < 10; turn++) {
      const index = sent.findIndex(message => message.kind === kind);
      if (index >= 0) return sent.splice(index, 1)[0]!;
      await tick();
    }
    throw new Error('missing synthetic snapshot request');
  };
  const reply = (message: Envelope, kind: Kind, payload: JsonObject): Promise<boolean> =>
    session.receive({...message, kind, payload});
  const inventory = async (revision: number): Promise<void> => {
    const message = await request('inventory_request');
    await reply(message, 'inventory_page', {scope: account, cursor: message.payload.cursor,
      next_cursor: null, revision, total: 1, complete: true, devices: [info]});
    await tick();
  };
  await session.receive({v: 1, kind: 'inventory_reset', boot_id: boot,
    request_id: randomUUID(), payload: {scope: account, revision: 1}});
  await inventory(1);
  const native = session.camera('front');
  const policy: CameraPolicy = {synchronized: true, streaming: true, live_audio: false, snapshots: true,
    recording: false, recording_audio: false, talkback: false, controls: false};
  const update = native.updatePolicy(policy), changing = await request('policy_update');
  await reply(changing, 'policy_ack', {scope: changing.payload.scope, accepted: true, code: 'ok'});
  info.scope.privacy_generation++;
  await inventory(2); await update;
  const diagnostics: [string, string][] = [];
  const delegate = new HapVideoDelegate(native, {active: () => true, policy: () => policy,
    forceStop: () => {}, failure: () => {}, reportError: (stage, code) => diagnostics.push([stage, code])});
  return {session, native, delegate, request, reply, sent, failures, diagnostics};
}

test('snapshot parent failures retain only fixed reasons through the public HAP callback', async () => {
  const peer = await snapshotPeer();
  try {
    for (const [code, expected] of [['source-failed', 'snapshot-source-failed'], ['busy', 'snapshot-busy'],
      ['stale-scope', 'snapshot-stale-scope'], ['not-found', 'snapshot-rejected']] as const) {
      const pending = snapshot(peer.delegate, ResourceRequestReason.PERIODIC);
      const rejected = assert.rejects(pending, /-70402/);
      const message = await peer.request('snapshot_request');
      await peer.reply(message, 'snapshot_result', {scope: message.payload.scope, job_id: message.payload.job_id,
        status: 'rejected', code, transfer_id: null});
      await rejected;
      assert.deepEqual(peer.diagnostics.at(-1), ['snapshot', expected]);
      assert.equal(peer.session.pendingRequests, 0);
      assert.equal(peer.sent.some(value => value.kind === 'snapshot_cancel'), false);
    }
    assert.deepEqual(peer.failures, []);
  } finally { await peer.delegate.close(); peer.session.close(); }
});

test('arbitrary parent error text cannot become a snapshot diagnostic or a successful image', async () => {
  const peer = await snapshotPeer();
  try {
    const pending = snapshot(peer.delegate, ResourceRequestReason.PERIODIC), rejected = assert.rejects(pending, /-70402/);
    const message = await peer.request('snapshot_request');
    await assert.rejects(peer.reply(message, 'snapshot_result', {scope: message.payload.scope, job_id: message.payload.job_id,
      status: 'rejected', code: 'untrusted-secret-text', transfer_id: null}), /invalid-payload/);
    peer.session.close(); await rejected;
    assert.equal(JSON.stringify(peer.diagnostics).includes('untrusted-secret-text'), false);
    assert.equal(peer.session.pendingRequests, 0);
  } finally { await peer.delegate.close(); peer.session.close(); }
});
