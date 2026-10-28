"""Public HAP delegates -> real IPC -> native SRTP -> independent picture decode."""

import asyncio
import json
import socket
import struct
from pathlib import Path

import av
import pylibsrtp
import pytest
from test_homekit_bridge import ADAPTER, NODE
from test_homekit_broker import setup_broker

from halo_server.homekit.runtime import HomeKitRuntime
from halo_server.media.h264 import VideoConfiguration
from halo_server.media.homekit_live import HomeKitLiveManager
from halo_server.media.transcode import VideoWorker

SCENARIO = r"""
import {Socket} from 'node:net';
import {randomUUID} from 'node:crypto';
const root=__ROOT__;
const {ControlChannel}=await import(`${root}/ipc.js`);
const {BulkChannel}=await import(`${root}/bulk.js`);
const {AdapterSession}=await import(`${root}/session.js`);
const {HapVideoDelegate}=await import(`${root}/video.js`);
const hap=await import(__HAP__);
const stream=new Socket({fd:Number(process.env.HALO_CONTROL_FD),readable:true,writable:true});
const binary=new Socket({fd:Number(process.env.HALO_BULK_FD),readable:true,writable:true});
const channel=new ControlChannel(stream);
let session,bulk,delegate,boot,ran=false;
const send=(kind,payload,request_id=randomUUID())=>
 channel.send({v:1,kind,boot_id:boot,request_id,payload});
const invoke=(method,value)=>new Promise((resolve,reject)=>
 method(value,(error,response)=>error?reject(error):resolve(response)));
async function exercise(){
 try {
  for(let i=0;i<1000&&!session.inventoryComplete;i++)await new Promise(r=>setTimeout(r,2));
  delegate=new HapVideoDelegate(session.camera('42'),{
   active:()=>true,forceStop:()=>{},failure:()=>{throw Error('delegate')}});
  const source={port:__PORT__,srtpCryptoSuite:hap.SRTPCryptoSuites.AES_CM_128_HMAC_SHA1_80,
   srtp_key:Buffer.from(__KEY__,'hex'),srtp_salt:Buffer.from(__SALT__,'hex')};
  const response=await invoke(delegate.prepareStream.bind(delegate),{sessionID:'actual-hap-session',
   sourceAddress:'127.0.0.1',targetAddress:'127.0.0.1',addressVersion:'ipv4',video:source,audio:source});
  await invoke(delegate.handleStreamRequest.bind(delegate),{
   sessionID:'actual-hap-session',type:hap.StreamRequestTypes.START,
   video:{codec:hap.VideoCodecType.H264,profile:hap.H264Profile.BASELINE,level:hap.H264Level.LEVEL3_1,
    packetizationMode:hap.VideoCodecPacketizationMode.NON_INTERLEAVED,width:320,height:240,fps:15,pt:99,
    ssrc:response.video.ssrc,max_bit_rate:300,rtcp_interval:.5,mtu:1200},audio:{}});
  await send('status',{state:'ready',reason:`started-${response.video.ssrc}`});
 }catch{await send('status',{state:'degraded',reason:'failed'});}
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
  await delegate?.close();session.close();await bulk.close();
  await channel.send({...message,kind:'shutdown_ack',payload:{}});channel.close();return;
 }
 await session.receive(message);
 if(message.kind==='inventory_reset'&&!ran){ran=true;void exercise();}
})().catch(()=>{channel.close();binary.destroy();});});
channel.on('closed',()=>{session?.close();binary.destroy();});
"""


@pytest.mark.asyncio
async def test_public_hap_delegate_reaches_native_udp_picture_and_graceful_cleanup(tmp_path):
    broker, old_boot, _, service, client, _ = await setup_broker(tmp_path)
    await broker.retired(old_boot)
    live = HomeKitLiveManager(service.homekit)
    await live.qualify()
    assert live.ready
    broker.live_manager = live
    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    receiver.bind(("127.0.0.1", 0))
    receiver.setblocking(False)
    key, salt = bytes(range(16)), bytes(range(14))
    hap_entry = (
        Path(__file__).resolve().parents[1]
        / "homekit-adapter/node_modules/@homebridge/hap-nodejs/dist/index.js"
    )
    script = (
        SCENARIO.replace("__ROOT__", json.dumps(ADAPTER.as_uri()))
        .replace("__HAP__", json.dumps(hap_entry.resolve().as_uri()))
        .replace("__PORT__", str(receiver.getsockname()[1]))
        .replace("__KEY__", json.dumps(key.hex()))
        .replace("__SALT__", json.dumps(salt.hex()))
    )
    entry = tmp_path / "video.mjs"
    entry.write_text(script)
    started = asyncio.get_running_loop().create_future()

    async def notice(message):
        if message["kind"] == "status":
            if not started.done():
                started.set_result(message["payload"]["reason"])
        else:
            await broker.notice(message)

    async def retired(boot):
        service.homekit.fence()
        await live.retired(boot)
        await broker.retired(boot)

    runtime = HomeKitRuntime(
        NODE,
        entry,
        build_id="native-video-fixture",
        on_ready=broker.ready,
        on_request=broker.request,
        on_notice=notice,
        on_retired=retired,
        max_restarts=0,
    )
    broker.runtime = runtime
    worker = VideoWorker(VideoConfiguration(320, 240, 15, "baseline", "3.1", 300))
    try:
        await asyncio.wait_for(runtime.start(), 4)
        result = await asyncio.wait_for(started, 4)
        assert result.startswith("started-"), result
        ssrc = int(result.removeprefix("started-"))
        capture = service._media_sessions["42"]
        picture = (await worker.probe())[0][1]
        await worker.close()
        capture.buffer.append_video(picture, 0)
        decoder = pylibsrtp.Session(
            pylibsrtp.Policy(
                key=key + salt, ssrc_type=pylibsrtp.Policy.SSRC_SPECIFIC, ssrc_value=ssrc
            )
        )
        nals, fragment = [], bytearray()
        async with asyncio.timeout(4):
            while True:
                protected = await asyncio.get_running_loop().sock_recv(receiver, 2048)
                assert len(protected) <= 1200
                packet = decoder.unprotect(protected)
                assert packet[1] & 0x7F == 99 and struct.unpack_from("!I", packet, 8)[0] == ssrc
                payload = packet[12:]
                if payload[0] & 31 == 28:
                    if payload[1] & 0x80:
                        fragment = bytearray([(payload[0] & 0xE0) | (payload[1] & 31)])
                    fragment.extend(payload[2:])
                    if payload[1] & 0x40:
                        nals.append(bytes(fragment))
                else:
                    nals.append(payload)
                if packet[1] & 0x80:
                    break
        recovered = b"".join(b"\x00\x00\x00\x01" + nal for nal in nals)
        decoded = av.CodecContext.create("h264", "r").decode(av.Packet(recovered))
        assert len(decoded) == 1 and (decoded[0].width, decoded[0].height) == (320, 240)
        assert abs(decoded[0].to_ndarray(format="gray").mean()) < 2
        # Copy mode proves compressed NAL preservation separately from picture decode.
        from halo_server.media.h264 import split_annexb

        assert tuple(nals) == split_annexb(picture)
        assert client.submissions == 0 and len(capture.leases) == 1
        process = runtime._process
        await asyncio.wait_for(runtime.stop(), 4)
        assert process.returncode == 0  # Cleanup RPC replies remain admitted during shutdown.
        assert not capture.leases and live.pending_resources == 0
        assert service.homekit.pending_resources == 0
    finally:
        receiver.close()
        await worker.close()
        await runtime.stop()
        await live.retired(runtime.boot_id)
