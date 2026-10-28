"""Real child failure boundaries with private sockets and synthetic HAP storage only."""

import asyncio
import json
import os
import socket
import sys
from pathlib import Path

import pytest
from test_homekit_bridge import ADAPTER, NODE
from test_homekit_storage_owner import pairing_directory

from halo_server.homekit.pairing_maintenance import manifest_revision
from halo_server.homekit.runtime import HomeKitRuntime, HomeKitRuntimeError
from halo_server.homekit.storage_owner import PairingDirectoryOwner

ROOT = Path(__file__).resolve().parents[1]
ENTRY = ADAPTER / "main.js"
HELPER = ADAPTER / "pairing-reset.js"
MARKER = ".hap-storage-intent"
pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or not NODE.is_file() or not ENTRY.is_file(),
    reason="qualified Linux Node and compiled adapter required",
)


async def boot_with_marker(directory):
    owner = PairingDirectoryOwner.acquire(directory)
    control, child = socket.socketpair()
    bulk, bulk_child = socket.socketpair()
    try:
        process = await asyncio.create_subprocess_exec(
            str(NODE),
            str(ENTRY),
            env={
                "HALO_PARENT_PID": str(os.getpid()),
                "HALO_CONTROL_FD": str(child.fileno()),
                "HALO_BULK_FD": str(bulk_child.fileno()),
                "HALO_PAIRING_FD": str(owner.directory_fd),
                "HALO_PAIRING_LOCK_FD": str(owner.lock_fd),
            },
            pass_fds=(child.fileno(), bulk_child.fileno(), owner.directory_fd, owner.lock_fd),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        child.close()
        bulk_child.close()
        output, error = await asyncio.wait_for(process.communicate(), 3)
        assert process.returncode == 1 and output == b""
        assert error == b"Halo HAP adapter: storage_recovery_required\n"
    finally:
        control.close()
        child.close()
        bulk.close()
        bulk_child.close()
        owner.close()


@pytest.mark.asyncio
async def test_actual_adapter_refuses_retained_intent_without_loading_or_erasing_it(tmp_path):
    directory = pairing_directory(tmp_path)
    marker = directory / MARKER
    marker.write_bytes(b'{"version":1,"operation":"remove"}')
    marker.chmod(0o600)
    before = marker.read_bytes()
    await boot_with_marker(directory)
    assert marker.read_bytes() == before
    assert list(directory.iterdir()) == [marker]


SCENARIO = r"""
import fs from 'node:fs';
import {Socket} from 'node:net';
const root=__ROOT__;
const {ControlChannel}=await import(`${root}/ipc.js`);
const {AdapterSession}=await import(`${root}/session.js`);
const {CameraAccessory}=await import(`${root}/camera.js`);
const {PairingStore}=await import(`${root}/storage.js`);
const {enablePairingDurability}=await import(`${root}/storage-boundary.js`);
const {FEATURES}=await import(`${root}/protocol.js`);
const hap=await import(__HAP__);
const channel=new ControlChannel(new Socket({
 fd:Number(process.env.HALO_CONTROL_FD),readable:true,writable:true}));
let session,camera,native,ran=false;
enablePairingDurability(()=>{
 session?.quiesce();camera?.retire();
 fs.writeFileSync(__WITNESS__,JSON.stringify({
  quiesced:native?.ready===false,retired:camera?.isRetired===true}),{mode:0o600});
});
const store=new PairingStore(Number(process.env.HALO_PAIRING_FD));store.initialize();
function exercise(){
 if(ran)return;ran=true;native=session.camera('front');
 camera=new CameraAccessory({camera_id:'front',
  accessory_id:'00000000-0000-0000-0000-000000000003',
  username:'02:11:22:33:44:55',doorbell:false,audio:null},native.info,{native});
 camera.streamingDelegate.prepareStream({sessionID:'pending-real-private-rpc',
  sourceAddress:'127.0.0.1',targetAddress:'127.0.0.1',addressVersion:'ipv4',
  video:{port:50100,srtpCryptoSuite:hap.SRTPCryptoSuites.AES_CM_128_HMAC_SHA1_80,
   srtp_key:Buffer.alloc(16,7),srtp_salt:Buffer.alloc(14,9)}},()=>{});
}
function fault(){
 const sync=fs.fsyncSync;let directories=0;
 fs.fsyncSync=fd=>{
  if(fs.fstatSync(fd).isDirectory()&&++directories===2)throw Error('private-error');
  return sync(fd);
 };
 hap.HAPStorage.storage().setItemSync('IdentifierCache.021122334455.json',{cache:{}});
 fs.writeFileSync(__AFTER__,'must-not-continue');
}
channel.on('message',message=>{void(async()=>{
 if(message.kind==='hello'){
  session=new AdapterSession(message.boot_id,{
   send:value=>channel.send(value),onInventory:exercise});
  await channel.send({...message,kind:'hello_ack',payload:{role:'hap',
   protocol:1,build_id:message.payload.build_id,features:[...FEATURES],pid:process.pid}});
 }else if(message.kind==='ping'){fault();}
 else await session.receive(message);
})().catch(()=>process.exit(9));});
"""


@pytest.mark.asyncio
async def test_actual_storage_failure_exits_before_cached_authority_or_pending_media_can_continue(
    tmp_path,
):
    directory = pairing_directory(tmp_path)
    witness, after = tmp_path / "fenced.json", tmp_path / "after"
    hap = ROOT / "homekit-adapter/node_modules/@homebridge/hap-nodejs/dist/index.js"
    script = (
        SCENARIO.replace("__ROOT__", json.dumps(ADAPTER.as_uri()))
        .replace("__HAP__", json.dumps(hap.as_uri()))
        .replace("__WITNESS__", json.dumps(str(witness)))
        .replace("__AFTER__", json.dumps(str(after)))
    )
    entry = tmp_path / "storage-failure.mjs"
    entry.write_text(script)
    scope = {
        "profile_id": "00000000-0000-0000-0000-000000000002",
        "account_generation": 1,
        "camera_id": "front",
        "device_generation": 1,
        "privacy_generation": 1,
    }
    account = {key: scope[key] for key in ("profile_id", "account_generation")}
    capabilities = {
        key: key == "video"
        for key in (
            "video",
            "audio",
            "snapshot",
            "talkback",
            "light",
            "siren",
            "doorbell",
            "battery",
        )
    } | {"evidence": ["halo:video"]}
    device = {
        "scope": scope,
        "name": "Fixture",
        "family": "fixture",
        "kind": "fixture",
        "model": "Fixture",
        "capabilities": capabilities,
        "connection_state": None,
        "battery_percent": None,
        "observed_format": None,
    }
    prepare, release, retired = asyncio.Event(), asyncio.Event(), []
    runtime = None

    async def ready(_boot):
        await runtime.send("inventory_reset", {"scope": account, "revision": 1})

    async def request(message):
        payload = message["payload"]
        if message["kind"] == "inventory_request":
            return {
                "scope": account,
                "revision": 1,
                "cursor": payload["cursor"],
                "next_cursor": None,
                "total": 1,
                "complete": True,
                "devices": [device],
            }
        if message["kind"] == "policy_update":
            return {"scope": payload["scope"], "accepted": True, "code": "ok"}
        if message["kind"] == "video_prepare":
            prepare.set()
            await release.wait()
            return payload | {"status": "rejected", "code": "cancelled", "endpoint": None}
        assert message["kind"] == "video_stop"
        return payload | {"status": "accepted", "code": "ok"}

    async def retire(boot):
        assert runtime._process.returncode == 1 and prepare.is_set()
        retired.append(boot)

    runtime = HomeKitRuntime(
        NODE,
        entry,
        build_id="storage-fatal-test",
        pairing_directory=directory,
        on_ready=ready,
        on_request=request,
        on_retired=retire,
        max_restarts=0,
    )
    try:
        await asyncio.wait_for(runtime.start(), 4)
        await asyncio.wait_for(prepare.wait(), 4)
        with pytest.raises(HomeKitRuntimeError):
            await runtime.request("ping", {}, expected="pong")
        async with asyncio.timeout(4):
            while runtime.state != "failed":
                await asyncio.sleep(0.01)
        assert len(retired) == 1 and runtime.pid is None
        assert json.loads(witness.read_text()) == {"quiesced": True, "retired": True}
        assert not after.exists()
        marker = directory / MARKER
        assert json.loads(marker.read_bytes()) == {"version": 1, "operation": "write"}
    finally:
        release.set()
        await runtime.stop()
    before = {path.name: path.read_bytes() for path in directory.iterdir()}
    await boot_with_marker(directory)
    assert {path.name: path.read_bytes() for path in directory.iterdir()} == before


@pytest.mark.asyncio
async def test_actual_reset_helper_enables_guard_and_emits_no_success_after_storage_failure(
    tmp_path,
):
    directory = pairing_directory(tmp_path)
    owner = PairingDirectoryOwner.acquire(directory)
    wrapper = tmp_path / "helper-failure.mjs"
    wrapper.write_text(
        "import fs from 'node:fs';\n"
        "const remove=fs.rmSync;fs.rmSync=(...args)=>{remove(...args);throw Error('private');};\n"
        f"process.argv[1]={json.dumps(str(HELPER))};await import({json.dumps(HELPER.as_uri())});\n"
    )
    manifest = dict.fromkeys(("accessory_info", "controller_storage", "identifier_cache"))
    request = {
        "v": 1,
        "operation_id": "00000000-0000-0000-0000-000000000004",
        "accessory_id": "00000000-0000-0000-0000-000000000003",
        "username": "02:11:22:33:44:55",
        "pairing_revision": manifest_revision(manifest),
        "pairing_manifest": manifest,
        "resume_cleanup": False,
    }
    try:
        process = await asyncio.create_subprocess_exec(
            str(NODE),
            str(wrapper),
            env={
                "HALO_PARENT_PID": str(os.getpid()),
                "HALO_PAIRING_FD": str(owner.directory_fd),
                "HALO_PAIRING_LOCK_FD": str(owner.lock_fd),
            },
            pass_fds=(owner.directory_fd, owner.lock_fd),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        body = (json.dumps(request, separators=(",", ":")) + "\n").encode("ascii")
        output, error = await asyncio.wait_for(process.communicate(body), 4)
        assert process.returncode == 1 and output == b""
        assert error == b"Halo HAP adapter: storage_recovery_required\n"
        assert json.loads((directory / MARKER).read_bytes())["operation"] == "remove"
    finally:
        owner.close()
