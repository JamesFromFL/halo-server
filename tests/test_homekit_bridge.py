"""Real inherited channels, compiled delegates, synthetic Python camera owners."""

import asyncio
import json
import shutil
from pathlib import Path

import pytest
from test_homekit_broker import setup_broker
from test_native_service import service_fixture

from halo_server.homekit.bridge import AdapterResources
from halo_server.homekit.runtime import HomeKitRuntime

NODE = Path(shutil.which("node") or "/missing-node").resolve()
ADAPTER = Path(__file__).resolve().parents[1] / "homekit-adapter/dist/src"


async def wait_until(check, *, timeout=5):
    async with asyncio.timeout(timeout):
        while not check():
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_daemon_missing_adapter_keeps_local_administration_available(tmp_path, monkeypatch):
    from halo_server.homekit import bridge as module

    def unavailable():
        raise FileNotFoundError("synthetic missing build")

    monkeypatch.setattr(module, "development_resources", unavailable)
    service, _, sessions = service_fixture(tmp_path)
    sessions.active_client = None
    sessions.restore = lambda: None
    sessions.has_stored_credentials = lambda: False
    try:
        await service.start()
        await wait_until(
            lambda: service.status()["homekit"]["reason"] == "adapter_resources_unavailable"
        )
        assert service.paths.admin_socket.is_socket()
        assert service.status()["service"] == "running"
        assert not service.status()["homekit"]["transport_ready"]
    finally:
        await service.stop()
    assert service._shutdown_complete


@pytest.mark.asyncio
async def test_daemon_real_compiled_child_is_control_ready_without_publishing(tmp_path):
    resources = AdapterResources(NODE, ADAPTER / "main.js")
    service, _, sessions = service_fixture(tmp_path)
    service._homekit_bridge._resources = resources
    sessions.active_client = None
    sessions.restore = lambda: None
    sessions.has_stored_credentials = lambda: False
    try:
        await service.start()
        await wait_until(lambda: service.status()["homekit"]["transport_ready"], timeout=25)
        status = service.status()["homekit"]
        assert status["state"] == "control_ready"
        assert status["reason"] == "recording_not_implemented"
        assert status["camera_ready"] is False
        runtime = service._homekit_bridge._runtime
        process = runtime._process
        assert process is not None
        assert list(service.paths.homekit_pairing_directory.iterdir()) == []
        assert await runtime.request("ping", {}, expected="pong") == {}
    finally:
        await service.stop()
    assert process.returncode is not None
    assert runtime.pid is None
    assert service._shutdown_complete


SCENARIO = r"""
import {Socket} from 'node:net';
import {randomUUID} from 'node:crypto';
const root = __MODULE_ROOT__;
const {ControlChannel} = await import(`${root}/ipc.js`);
const {BulkChannel} = await import(`${root}/bulk.js`);
const {AdapterSession} = await import(`${root}/session.js`);
const controlSocket = new Socket({
  fd:Number(process.env.HALO_CONTROL_FD),readable:true,writable:true});
const binarySocket = new Socket({fd:Number(process.env.HALO_BULK_FD),readable:true,writable:true});
const channel = new ControlChannel(controlSocket);
let session, binary, boot, ran = false;
const send = (kind,payload,requestId=randomUUID()) =>
  channel.send({v:1,kind,boot_id:boot,request_id:requestId,payload});
async function exercise() {
  try {
    for(let i=0;i<1000 && !session.inventoryComplete;i++) await new Promise(r=>setTimeout(r,2));
    if(!session.inventoryComplete) throw Error('inventory');
    const camera=session.camera('42');
    await camera.updatePolicy({synchronized:true,streaming:true,live_audio:false,snapshots:true,
      recording:false,recording_audio:false,talkback:true,controls:true});
    const live=await camera.acquireLive(3000); await live.renew(3000);
    if(await camera.setControl('light',true)!=='accepted') throw Error('control');
    const speaker=await camera.acquireTalkback();
    for(let i=0;i<3;i++) await speaker.writePcm(Buffer.alloc(1920,i+1));
    await speaker.close(); await live.close();
    let rejected=false;try{await camera.snapshot(640,480);}catch{rejected=true;}
    if(!rejected) throw Error('image');
    await send('status',{state:'ready',reason:'synthetic-pass'});
  } catch {await send('status',{state:'degraded',reason:'synthetic-failed'});}
}
channel.on('message',message=>{void(async()=>{
  if(message.kind==='hello') {
    boot=message.boot_id;
    binary=new BulkChannel(binarySocket,{bootId:boot,role:'hap',sendControl:send});binary.start();
    session=new AdapterSession(boot,{send:m=>channel.send(m),bulk:binary});
    await channel.send({...message,kind:'hello_ack',payload:{
      role:'hap',build_id:message.payload.build_id,
      protocol:1,features:['classic-camera-control-v1','bulk-cancel-v1'],pid:process.pid}});return;
  }
  if(message.kind==='ping'){await channel.send({...message,kind:'pong',payload:{}});return;}
  if(message.kind==='shutdown'){
    await channel.send({...message,kind:'shutdown_ack',payload:{}});
    session.close();await binary.close();channel.close();return;
  }
  await session.receive(message);
  if(message.kind==='inventory_reset' && !ran){ran=true;void exercise();}
})().catch(()=>{channel.close();binarySocket.destroy();});});
channel.on('closed',()=>{session?.close();binarySocket.destroy();});
"""


@pytest.mark.asyncio
async def test_compiled_camera_delegates_drive_real_broker_and_pcm_channel(tmp_path):
    broker, boot, _, service, client, _ = await setup_broker(tmp_path)
    await broker.retired(boot)
    entry = tmp_path / "scenario.mjs"
    entry.write_text(SCENARIO.replace("__MODULE_ROOT__", json.dumps(ADAPTER.as_uri())))
    outcome = asyncio.get_running_loop().create_future()

    async def notice(message):
        if message["kind"] == "status":
            if not outcome.done():
                outcome.set_result(message["payload"]["reason"])
        else:
            await broker.notice(message)

    runtime = HomeKitRuntime(
        NODE,
        entry,
        build_id="synthetic-cross-language",
        on_ready=broker.ready,
        on_request=broker.request,
        on_notice=notice,
        on_retired=broker.retired,
        max_restarts=0,
    )
    broker.runtime = runtime
    try:
        await asyncio.wait_for(runtime.start(), 4)
        assert await asyncio.wait_for(outcome, 8) == "synthetic-pass"
        assert client.all_devices[0].light_values == [True]
        capture = service._media_sessions["42"]
        assert capture.frames == [bytes([index]) * 1920 for index in (1, 2, 3)]
        assert not capture.leases
    finally:
        await asyncio.wait_for(runtime.stop(), 5)
    assert runtime.pid is None and broker.boot_id is None
    assert service.homekit.pending_resources == 0


@pytest.mark.asyncio
async def test_bridge_cannot_overwrite_runtime_with_unfinished_retirement(tmp_path):
    from types import SimpleNamespace

    service, _, _ = service_fixture(tmp_path)
    bridge = service._homekit_bridge
    retained = SimpleNamespace(boot_id="retiring-boot")
    bridge._runtime = retained
    with pytest.raises(RuntimeError, match="retirement"):
        bridge.start()
    assert bridge._runtime is retained and bridge._task is None


@pytest.mark.asyncio
async def test_cancelled_bridge_stop_still_joins_owned_runtime_cleanup(tmp_path):
    service, _, _ = service_fixture(tmp_path)
    bridge = service._homekit_bridge
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class RetiringRuntime:
        async def stop(self):
            entered.set()
            await release.wait()
            finished.set()

    bridge._runtime = RetiringRuntime()
    stopping = asyncio.create_task(bridge.stop())
    await asyncio.wait_for(entered.wait(), 1)
    stopping.cancel()
    await asyncio.sleep(0)
    assert not stopping.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(stopping, 1)
    assert finished.is_set() and bridge.state == "stopped"
