"""Public HAP snapshots -> real private channels -> isolated JPEG processing."""

import asyncio
import concurrent.futures
import json
from pathlib import Path

import pytest
from test_homekit_bridge import ADAPTER, NODE
from test_homekit_broker import setup_broker
from test_homekit_snapshot_codec import jpeg_source, rgb

from halo_server.homekit.runtime import HomeKitRuntime
from halo_server.media.jpeg import inspect_jpeg
from halo_server.media.snapshots import HomeKitSnapshots

SCENARIO = r"""
import {Socket} from 'node:net';
import {randomUUID} from 'node:crypto';
import {writeFile} from 'node:fs/promises';
const root=__ROOT__;
const {ControlChannel}=await import(`${root}/ipc.js`);
const {BulkChannel}=await import(`${root}/bulk.js`);
const {AdapterSession}=await import(`${root}/session.js`);
const {CameraAccessory}=await import(`${root}/camera.js`);
const hap=await import(__HAP__);
const stream=new Socket({fd:Number(process.env.HALO_CONTROL_FD),readable:true,writable:true});
const binary=new Socket({fd:Number(process.env.HALO_BULK_FD),readable:true,writable:true});
const channel=new ControlChannel(stream);
let session,bulk,camera,boot,ran=false;
const send=(kind,payload,request_id=randomUUID())=>
 channel.send({v:1,kind,boot_id:boot,request_id,payload});
async function exercise(){
 try {
  for(let i=0;i<1000&&!session.inventoryComplete;i++)await new Promise(r=>setTimeout(r,2));
  const native=session.camera('42');
  camera=new CameraAccessory({camera_id:'42',accessory_id:'00000000-0000-0000-0000-000000000003',
    username:'02:11:22:33:44:55',doorbell:false,audio:null},
    native.info,{native});
  const cases=[[320,240,hap.ResourceRequestReason.EVENT],
    [333,187,hap.ResourceRequestReason.PERIODIC],[161,91,undefined],[640,360,undefined]];
  for(let i=0;i<cases.length;i++){
   const [width,height,reason]=cases[i];
   const image=await camera.controller.handleSnapshotRequest(height,width,undefined,reason);
   await writeFile(`${__OUTPUT__}/snapshot-${i}.jpg`,image,{mode:0o600});
  }
  await send('status',{state:'ready',reason:'snapshots-pass'});
 }catch{await send('status',{state:'degraded',reason:'snapshots-failed'});}
}
channel.on('message',message=>{void(async()=>{
 if(message.kind==='hello'){
  boot=message.boot_id;
  bulk=new BulkChannel(binary,{bootId:boot,role:'hap',sendControl:send});bulk.start();
  session=new AdapterSession(boot,{send:m=>channel.send(m),bulk});
  await channel.send({...message,kind:'hello_ack',payload:{
   role:'hap',build_id:message.payload.build_id,
   protocol:1,features:['classic-camera-control-v1','bulk-cancel-v1','hksv-recording-v1','owner-admin-v1'],pid:process.pid}});return;
 }
 if(message.kind==='ping'){await channel.send({...message,kind:'pong',payload:{}});return;}
 if(message.kind==='shutdown'){
  await camera?.closeStreams();session.close();await bulk.close();
  await channel.send({...message,kind:'shutdown_ack',payload:{}});channel.close();return;
 }
 await session.receive(message);
 if(message.kind==='inventory_reset'&&!ran){ran=true;void exercise();}
})().catch(()=>{channel.close();binary.destroy();});});
channel.on('closed',()=>{session?.close();binary.destroy();});
"""


@pytest.mark.asyncio
async def test_public_hap_snapshots_reach_isolated_worker_exact_size_and_graceful_exit(tmp_path):
    broker, old_boot, _, service, client, _ = await setup_broker(tmp_path)
    await broker.retired(old_boot)
    images = HomeKitSnapshots(service.homekit)
    await images.qualify()
    assert images.ready
    broker.snapshot_manager = images
    source = jpeg_source(640, 360)
    requests = []

    def submit(device, *, fresh=False):
        assert device is client.all_devices[0]
        requests.append(fresh)
        client.submissions += 1
        result = concurrent.futures.Future()
        result.set_result(source)
        return result

    client.submit_snapshot_for_device = submit
    hap_entry = (
        Path(__file__).resolve().parents[1]
        / "homekit-adapter/node_modules/@homebridge/hap-nodejs/dist/index.js"
    )
    script = (
        SCENARIO.replace("__ROOT__", json.dumps(ADAPTER.as_uri()))
        .replace("__HAP__", json.dumps(hap_entry.resolve().as_uri()))
        .replace("__OUTPUT__", json.dumps(str(tmp_path)))
    )
    entry = tmp_path / "snapshot.mjs"
    entry.write_text(script)
    outcome = asyncio.get_running_loop().create_future()

    async def notice(message):
        if message["kind"] == "status":
            if not outcome.done():
                outcome.set_result(message["payload"]["reason"])
        else:
            await broker.notice(message)

    async def retired(boot):
        service.homekit.fence()
        await images.retired(boot)
        await broker.retired(boot)

    runtime = HomeKitRuntime(
        NODE,
        entry,
        build_id="native-snapshot-fixture",
        on_ready=broker.ready,
        on_request=broker.request,
        on_replied=broker.replied,
        on_notice=notice,
        on_retired=retired,
        max_restarts=0,
    )
    broker.runtime = runtime
    try:
        await asyncio.wait_for(runtime.start(), 4)
        assert await asyncio.wait_for(outcome, 10) == "snapshots-pass"
        for index, dimensions in enumerate(((320, 240), (333, 187), (161, 91), (640, 360))):
            image = (tmp_path / f"snapshot-{index}.jpg").read_bytes()
            if dimensions == (640, 360):
                assert image == source  # A verified matching baseline stays byte-identical.
            info = inspect_jpeg(image)
            assert (info.width, info.height) == dimensions
            pixels = rgb(image)  # Independent decoder checks image content too.
            assert pixels.shape[:2] == dimensions[::-1]
            # Requested geometry is letterboxed without distorting the source.
            height, width = pixels.shape[:2]
            centre = pixels[height // 4 : height * 3 // 4, width // 4 : width * 3 // 4]
            assert abs(float(centre[..., 0].mean()) - 190) < 10
            assert abs(float(centre[..., 1].mean()) - 50) < 10
            assert abs(float(centre[..., 2].mean()) - 20) < 10
        assert requests == [True, False, False, False]
        assert client.submissions == 4
        assert images.pending_resources == 0 and images.cache_bytes > 0
        process = runtime._process
        await asyncio.wait_for(runtime.stop(), 4)
        assert process.returncode == 0
        assert images.pending_resources == images.cache_bytes == 0
        assert not broker._snapshot_jobs and service.homekit.pending_resources == 0
    finally:
        await runtime.stop()
        await images.close()
