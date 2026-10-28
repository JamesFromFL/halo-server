"""Compiled Node and Python cancel individual jobs over actual private sockets."""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from test_homekit_bridge import ADAPTER, NODE
from test_homekit_bulk import chunks, identity
from test_homekit_bulk_cancel import until

from halo_server.homekit.bulk import BulkTransferError
from halo_server.homekit.runtime import HomeKitRuntime

SCENARIO = r"""
import {Socket} from 'node:net';
import {randomUUID} from 'node:crypto';
import {writeFile} from 'node:fs/promises';
const {ControlChannel}=await import(`${__ROOT__}/ipc.js`);
const {BulkChannel}=await import(`${__ROOT__}/bulk.js`);
const wire=new Socket({fd:Number(process.env.HALO_CONTROL_FD),readable:true,writable:true});
const binary=new Socket({fd:Number(process.env.HALO_BULK_FD),readable:true,writable:true});
const control=new ControlChannel(wire);
const mode=__MODE__,bad=__BAD__,good=__GOOD__;
let bulk,boot,handle,badTask,sourceRelease;
const sent=[],delivered=[];
const send=(kind,payload,request_id=randomUUID())=>{
 if(kind.startsWith('transfer_'))sent.push({kind,payload,request_id});
 return control.send({v:1,kind,boot_id:boot,request_id,payload});
};
const status=reason=>send('status',{state:'ready',reason});
const options=value=>({scope:value.scope,operationId:value.operation_id,
 transferId:value.transfer_id,
 kind:value.kind,totalBytes:value.total_bytes});
async function command(revision){
 if(revision===1){
  if(mode.startsWith('python-')){
   handle=bulk.authorizeReceive({...options(bad),sink:async(chunk,signal)=>{
    delivered.push(chunk.toString('hex'));await status('sink-entered');
    if(!signal.aborted)await new Promise(resolve=>signal.addEventListener('abort',resolve,
 {once:true}));
   }});
   if(mode.endsWith('empty'))await handle.cancel();
   await status('ready');
  }else{
   const source=mode.endsWith('empty')?{
    [Symbol.asyncIterator](){return this;},
    next(){void status('source-entered');
 return new Promise(resolve=>{sourceRelease=()=>resolve({done:true});});},
    async return(){return {done:true};}
   }:{async *[Symbol.asyncIterator](){yield Buffer.from('a');yield Buffer.from('b');
 yield Buffer.from('c');}};
   badTask=bulk.send({...options(bad),source}).then(()=>status('unexpected-complete'),
 error=>status(`source-${error.code??'failed'}`));
   await status('ready');
  }
 }else if(revision===2){
  await handle.cancel();await status('cancelled');
 }else if(revision===3){
  if(mode.startsWith('python-')){
   const received=[];
   const healthy=bulk.authorizeReceive({...options(good),
 sink:async(chunk)=>{received.push(chunk.toString('hex'));}});
   void healthy.wait().then(async()=>{if(received.join('')!=='6f6b')throw Error('payload');
 await status('good');})
    .catch(()=>status('good-failed'));
   await status('good-ready');
  }else{
   await bulk.send({...options(good),
 source:{async *[Symbol.asyncIterator](){yield Buffer.from('ok');}}});
   await status('good');
  }
 }else if(revision===4){
  sourceRelease?.();await badTask;await bulk.cancelTransfer(bad.transfer_id);
 await status('released');
 }
}
control.on('message',message=>{void(async()=>{
 if(message.kind==='hello'){
  boot=message.boot_id;bulk=new BulkChannel(binary,{bootId:boot,role:'hap',timeoutMs:2000,
 sendControl:send});bulk.start();
  await control.send({...message,kind:'hello_ack',payload:{role:'hap',
 build_id:message.payload.build_id,
   protocol:1,features:['classic-camera-control-v1','bulk-cancel-v1','hksv-recording-v1','owner-admin-v1'],pid:process.pid}});return;
 }
 if(message.kind==='ping'){await control.send({...message,kind:'pong',payload:{}});return;}
 if(message.kind==='shutdown'){
  sourceRelease?.();await badTask;await bulk.close();
  await writeFile(__OUTPUT__,JSON.stringify({sent,delivered,counts:bulk.counts}),{mode:0o600});
  await control.send({...message,kind:'shutdown_ack',payload:{}});control.close();return;
 }
 if(message.kind==='inventory_reset'){await command(message.payload.revision);return;}
 if(message.kind.startsWith('transfer_')){await bulk.handleControl(message);return;}
 throw Error('unexpected-control');
})().catch(async error=>{await status(`failed-${error.code??error.name}`).catch(()=>{});
 control.close();binary.destroy();});});
control.on('closed',()=>{sourceRelease?.();binary.destroy();});"""


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["python-empty", "python-partial", "node-empty", "node-partial"])
async def test_actual_private_peers_cancel_frontier_then_complete_unrelated_transfer(
    tmp_path, mode
):
    python_producer = mode.startswith("python-")
    bad = identity("snapshot" if python_producer else "talkback_pcm", 3)
    good = identity(bad["kind"], 2)
    good["scope"] = dict(bad["scope"])
    output = tmp_path / "node-evidence.json"
    entry = tmp_path / "bulk-cancel.mjs"
    script = SCENARIO
    for key, value in {
        "__ROOT__": ADAPTER.as_uri(),
        "__MODE__": mode,
        "__BAD__": bad,
        "__GOOD__": good,
        "__OUTPUT__": str(output),
    }.items():
        script = script.replace(key, json.dumps(value))
    entry.write_text(script)
    statuses = []
    changed = asyncio.Event()

    async def notice(message):
        if message["kind"] == "status":
            statuses.append(message["payload"]["reason"])
            changed.set()
        else:
            assert message["kind"] == "transfer_open"

    async def wait_status(expected):
        async with asyncio.timeout(4):
            while expected not in statuses:
                assert not any(value.startswith("failed-") for value in statuses), statuses
                changed.clear()
                if expected not in statuses:
                    await changed.wait()

    runtime = HomeKitRuntime(
        NODE,
        entry,
        build_id=f"bulk-cancel-{uuid.uuid4()}",
        on_notice=notice,
        on_retired=lambda boot: asyncio.sleep(0),
        max_restarts=0,
    )
    sending = None
    release = asyncio.Event()
    metadata = []
    try:
        await asyncio.wait_for(runtime.start(), 4)
        bulk = runtime.bulk_channel
        assert bulk is not None
        original_send = bulk._send_control
        original_receive = bulk.handle_control

        async def send(kind, payload, request):
            metadata.append(dict(kind=kind, payload=payload, request_id=request))
            await original_send(kind, payload, request)

        async def receive(message):
            metadata.append(message)
            return await original_receive(message)

        bulk._send_control, bulk.handle_control = send, receive

        async def command(revision):
            await runtime.send("inventory_reset", {"scope": None, "revision": revision})

        if python_producer:
            await command(1)
            await wait_status("ready")
            sending = asyncio.create_task(bulk.send(**bad, source=chunks(b"a", b"b", b"c")))
            if mode.endswith("partial"):
                await wait_status("sink-entered")
                await until(lambda: bulk._outgoing[bad["transfer_id"]].sent == 2)
                await command(2)
                await wait_status("cancelled")
            with pytest.raises(BulkTransferError, match="cancelled"):
                await asyncio.wait_for(sending, 3)
        else:
            entered = asyncio.Event()
            calls = []

            async def sink(chunk):
                calls.append(chunk)
                entered.set()
                await release.wait()

            handle = bulk.authorize_receive(**bad, sink=sink)
            await command(1)
            await wait_status("ready")
            if mode.endswith("empty"):
                await wait_status("source-entered")
            else:
                await entered.wait()
                await until(lambda: handle._state.sequence == 1 and handle._state.granted == 1)
                # The second credited body is already enqueued by Node while
                # Python waits for its first sink. The fence drains it as well.
                await asyncio.sleep(0.02)
            await asyncio.wait_for(handle.cancel(), 3)
            with pytest.raises(BulkTransferError, match="cancelled"):
                await handle.wait()
            assert calls == ([] if mode.endswith("empty") else [b"a"])

        # No whole-channel failure/restart, and no callback for drained data.
        assert not bulk.closed and runtime.state == "ready" and runtime.restart_count == 0
        fences = [m for m in metadata if m["kind"] == "transfer_fence"]
        acks = [m for m in metadata if m["kind"] == "transfer_fence_ack"]
        assert len(fences) == len(acks) == 1
        assert fences[0]["payload"] == acks[0]["payload"]
        assert fences[0]["request_id"] == acks[0]["request_id"]
        assert fences[0]["payload"]["sequence_count"] == (0 if mode.endswith("empty") else 2)
        assert fences[0]["payload"]["sent_bytes"] == (0 if mode.endswith("empty") else 2)

        # A second owned transfer succeeds on the SAME channels while cancelled
        # producer cleanup may still be waiting for its old iterator result.
        if python_producer:
            await command(3)
            await wait_status("good-ready")
            await bulk.send(**good, source=chunks(b"ok"))
        else:
            received = []

            async def good_sink(chunk):
                received.append(chunk)

            healthy = bulk.authorize_receive(**good, sink=good_sink)
            await command(3)
            await healthy.wait()
            assert received == [b"ok"]
        await wait_status("good")
        if not python_producer:
            await command(4)
            await wait_status("released")
        assert not bulk.closed and bulk.counts == (0, 0)
        assert bulk._send_slots._value == 2
        process = runtime._process
        await asyncio.wait_for(runtime.stop(), 4)
        assert process.returncode == 0
        evidence = json.loads(output.read_text())
        assert evidence["counts"] == [0, 0]
        if python_producer:
            assert evidence["delivered"] == ([] if mode.endswith("empty") else ["61"])
    finally:
        release.set()
        if sending is not None:
            await asyncio.gather(sending, return_exceptions=True)
        await runtime.stop()
