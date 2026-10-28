"""Real isolated child process exercises supervised dispatch without Ring or HAP publication."""

import asyncio
import json
import shutil
from pathlib import Path

import pytest

from halo_server.homekit.runtime import HomeKitRuntime, HomeKitRuntimeError

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node executable is required")
ACCOUNT = {"profile_id": "00000000-0000-0000-0000-000000000010", "account_generation": 1}
INVENTORY = {"scope": ACCOUNT, "revision": 0, "cursor": None}
PAGE = {
    "scope": ACCOUNT,
    "revision": 1,
    "cursor": None,
    "next_cursor": None,
    "total": 0,
    "complete": True,
    "devices": [],
}


def _child(tmp_path, action="", received=""):
    entry = tmp_path / "dispatch.cjs"
    entry.write_text(
        """
const { Socket } = require('node:net');
const control = new Socket({fd:Number(process.env.HALO_CONTROL_FD),readable:true,writable:true});
const bulk = new Socket({fd:Number(process.env.HALO_BULK_FD),readable:true,writable:true});
let input=Buffer.alloc(0), boot=null, triggered=false;
function send(message) {
  const body=Buffer.from(JSON.stringify(message));
  const frame=Buffer.alloc(body.length+4);frame.writeUInt32BE(body.length);body.copy(frame,4);
  control.write(frame);
}
function request(index, kind='inventory_request', payload=INVENTORY) {
  send({v:1,kind,boot_id:boot,request_id:'00000000-0000-0000-0000-'+String(index).padStart(12,'0'),payload});
}
const INVENTORY=INVENTORY_JSON;
control.on('data', chunk=> {
  input=Buffer.concat([input,chunk]);
  while(input.length>=4 && input.length>=input.readUInt32BE(0)+4) {
    const size=input.readUInt32BE(0), message=JSON.parse(input.subarray(4,size+4).toString());
    input=input.subarray(size+4);
    if(message.kind==='hello') {
      boot=message.boot_id;
      send({...message,kind:'hello_ack',payload:{role:'hap',build_id:message.payload.build_id,
        protocol:1,features:['classic-camera-control-v1','bulk-cancel-v1','hksv-recording-v1','owner-admin-v1'],pid:process.pid}});
    } else if(message.kind==='shutdown') {
      send({...message,kind:'shutdown_ack',payload:{}});
      control.end();bulk.destroy();
    } else if(message.kind==='ping') {
      ACTION
    } else { RECEIVED }
  }
});
control.on('error',()=>{});bulk.on('error',()=>{});
""".replace("INVENTORY_JSON", json.dumps(INVENTORY))
        .replace("ACTION", action or "send({...message,kind:'pong',payload:{}});")
        .replace("RECEIVED", received)
    )
    return entry


async def _wait_state(runtime, state):
    async with asyncio.timeout(3):
        while runtime.state != state:
            await asyncio.sleep(0.005)


async def _noop(_boot):
    pass


@pytest.mark.asyncio
async def test_ready_callback_has_current_boot_binary_channel_and_live_control(tmp_path):
    observed = []
    runtime = None

    async def ready(boot):
        observed.append((boot, runtime.boot_id, runtime.state, runtime.bulk_channel is not None))
        assert await runtime.request("ping", {}, expected="pong") == {}

    runtime = HomeKitRuntime(
        Path(NODE), _child(tmp_path), build_id="ready-test", on_ready=ready, on_retired=_noop
    )
    try:
        await runtime.start()
        assert observed == [(runtime.boot_id, runtime.boot_id, "starting", True)]
        assert runtime.state == "ready"
    finally:
        await runtime.stop()
    assert runtime.bulk_channel is None and runtime.boot_id is None


@pytest.mark.asyncio
async def test_child_request_does_not_block_parent_heartbeat_or_control_reader(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    seen = []

    async def handler(envelope):
        seen.append(envelope)
        entered.set()
        await release.wait()
        return PAGE

    entry = _child(
        tmp_path,
        "if(!triggered){triggered=true;request(1);}send({...message,kind:'pong',payload:{}});",
    )
    runtime = HomeKitRuntime(
        Path(NODE), entry, build_id="parallel-test", on_request=handler, on_retired=_noop
    )
    try:
        await runtime.start()
        assert await runtime.request("ping", {}, expected="pong") == {}
        await asyncio.wait_for(entered.wait(), 1)
        assert await runtime.request("ping", {}, expected="pong") == {}
        assert len(seen) == 1 and seen[0]["payload"] == INVENTORY
        release.set()
        async with asyncio.timeout(1):
            while runtime._child_tasks:
                await asyncio.sleep(0)
    finally:
        release.set()
        await runtime.stop()


@pytest.mark.asyncio
async def test_owned_negative_error_reply_is_delivered_without_granting_authority(tmp_path):
    observed = asyncio.Event()

    async def handler(envelope):
        return {**envelope, "kind": "error", "payload": {"code": "not-ready", "message": ""}}

    async def notice(envelope):
        assert envelope["kind"] == "status"
        assert envelope["payload"]["reason"] == "negative-received"
        observed.set()

    entry = _child(
        tmp_path,
        "if(!triggered){triggered=true;request(1);}send({...message,kind:'pong',payload:{}});",
        "if(message.kind==='error')request(2,'status',{state:'degraded',reason:'negative-received'});",
    )
    runtime = HomeKitRuntime(
        Path(NODE),
        entry,
        build_id="negative-test",
        on_request=handler,
        on_notice=notice,
        on_retired=_noop,
    )
    try:
        await runtime.start()
        await runtime.request("ping", {}, expected="pong")
        await asyncio.wait_for(observed.wait(), 1)
        assert runtime.state == "ready"
    finally:
        await runtime.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed", [False, True])
async def test_late_known_reply_cannot_resurrect_request_or_change_reply_contract(
    tmp_path, monkeypatch, malformed
):
    monkeypatch.setattr("halo_server.homekit.runtime.REQUEST_SECONDS", 0.05)
    kind = "shutdown_ack" if malformed else "pong"
    entry = _child(
        tmp_path,
        "if(!triggered){triggered=true;setTimeout(()=>send({...message,kind:"
        + json.dumps(kind)
        + ",payload:{}}),100);}else send({...message,kind:'pong',payload:{}});",
    )
    runtime = HomeKitRuntime(
        Path(NODE), entry, build_id="late-test", on_retired=_noop, max_restarts=0
    )
    try:
        await runtime.start()
        with pytest.raises(TimeoutError):
            await runtime.request("ping", {}, expected="pong")
        assert not runtime._pending
        if malformed:
            await _wait_state(runtime, "failed")
        else:
            await asyncio.sleep(0.12)
            assert await runtime.request("ping", {}, expected="pong") == {}
            assert runtime.state == "ready"
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_unsolicited_result_has_no_authority_and_does_not_complete_owned_request(tmp_path):
    entry = _child(
        tmp_path,
        "request(999,'pong',{});send({...message,kind:'pong',payload:{}});",
    )
    runtime = HomeKitRuntime(Path(NODE), entry, build_id="unowned-test", on_retired=_noop)
    try:
        await runtime.start()
        assert await runtime.request("ping", {}, expected="pong") == {}
        assert runtime.state == "ready"
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_duplicate_child_request_is_not_replayed(tmp_path):
    entered = asyncio.Event()
    invocations = []

    async def handler(envelope):
        invocations.append(envelope)
        entered.set()
        await asyncio.Event().wait()

    entry = _child(
        tmp_path,
        "if(!triggered){triggered=true;request(1);setTimeout(()=>request(1),20);}"
        "send({...message,kind:'pong',payload:{}});",
    )
    runtime = HomeKitRuntime(
        Path(NODE),
        entry,
        build_id="duplicate-test",
        on_request=handler,
        on_retired=_noop,
        max_restarts=0,
    )
    try:
        await runtime.start()
        await runtime.request("ping", {}, expected="pong")
        await asyncio.wait_for(entered.wait(), 1)
        await _wait_state(runtime, "failed")
        assert len(invocations) == 1
        assert not runtime._child_tasks
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_child_dispatch_capacity_is_bounded_and_retires_all_admitted_tasks(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("halo_server.homekit.runtime.MAX_CHILD_REQUESTS", 2)
    invocations = []

    async def handler(envelope):
        invocations.append(envelope)
        await asyncio.Event().wait()

    entry = _child(
        tmp_path,
        "if(!triggered){triggered=true;request(1);request(2);request(3);}"
        "send({...message,kind:'pong',payload:{}});",
    )
    runtime = HomeKitRuntime(
        Path(NODE),
        entry,
        build_id="capacity-test",
        on_request=handler,
        on_retired=_noop,
        max_restarts=0,
    )
    try:
        await runtime.start()
        with pytest.raises(HomeKitRuntimeError):
            await runtime.request("ping", {}, expected="pong")
        await _wait_state(runtime, "failed")
        assert len(invocations) <= 2
        assert not runtime._child_tasks
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_stop_retains_boot_until_handler_compensation_joins_without_recancelling(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("halo_server.homekit.runtime.STOP_SECONDS", 0.03)
    entered, compensating, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    order = []

    async def handler(_envelope):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            compensating.set()
            await release.wait()
            order.append("compensated")
            raise

    async def retired(boot):
        assert order == ["compensated"]
        order.append(boot)

    entry = _child(
        tmp_path,
        "if(!triggered){triggered=true;request(1);}send({...message,kind:'pong',payload:{}});",
    )
    runtime = HomeKitRuntime(
        Path(NODE),
        entry,
        build_id="compensation-test",
        on_request=handler,
        on_retired=retired,
        max_restarts=1,
    )
    try:
        await runtime.start()
        await runtime.request("ping", {}, expected="pong")
        await asyncio.wait_for(entered.wait(), 1)
        boot = runtime.boot_id
        with pytest.raises(HomeKitRuntimeError, match="retirement is incomplete"):
            await runtime.stop()
        assert compensating.is_set()
        assert runtime.boot_id == boot and runtime.state == "failed"
        assert not order
        assert runtime.restart_count == 0
        with pytest.raises(HomeKitRuntimeError):
            await runtime.start()
        release.set()
        await asyncio.sleep(0)
        await runtime.stop()
        assert order == ["compensated", boot]
    finally:
        release.set()
        await runtime.stop()


@pytest.mark.asyncio
async def test_missing_artifacts_fail_before_boot_allocation_or_retirement_callback(tmp_path):
    called = []

    async def retired(boot):
        called.append(boot)

    runtime = HomeKitRuntime(
        Path(NODE),
        tmp_path / "missing.js",
        build_id="missing-test",
        on_retired=retired,
    )
    try:
        with pytest.raises(HomeKitRuntimeError):
            await asyncio.wait_for(runtime.start(), 1)
        assert runtime.state == "failed" and runtime.boot_id is None
        assert runtime.reason == "adapter_artifacts_unavailable"
        assert runtime.restart_count == 0
        assert not called
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_normal_parent_heartbeats_have_bounded_recent_history(tmp_path, monkeypatch):
    monkeypatch.setattr("halo_server.homekit.runtime.MAX_RECENT_REQUESTS", 4)
    runtime = HomeKitRuntime(
        Path(NODE), _child(tmp_path), build_id="history-test", on_retired=_noop
    )
    try:
        await runtime.start()
        for _ in range(20):
            assert await runtime.request("ping", {}, expected="pong") == {}
        assert len(runtime._retired_requests) == 4
        assert not runtime._pending
        assert runtime.state == "ready"
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_normal_child_inventory_polling_does_not_exhaust_operation_replay_budget(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("halo_server.homekit.runtime.MAX_RECENT_REQUESTS", 4)
    monkeypatch.setattr("halo_server.homekit.runtime.MAX_REQUEST_HISTORY", 2)
    complete = asyncio.Event()
    calls = []

    async def handler(envelope):
        calls.append(envelope)
        return PAGE

    async def notice(_envelope):
        complete.set()

    entry = _child(
        tmp_path,
        "if(!triggered){triggered=true;request(1);}send({...message,kind:'pong',payload:{}});",
        "if(message.kind==='inventory_page'){const next=Number(message.request_id.slice(-12))+1;"
        "if(next<=20)request(next);else request(99,'status',{state:'ready',reason:''});}",
    )
    runtime = HomeKitRuntime(
        Path(NODE),
        entry,
        build_id="polling-test",
        on_request=handler,
        on_notice=notice,
        on_retired=_noop,
    )
    try:
        await runtime.start()
        await runtime.request("ping", {}, expected="pong")
        await asyncio.wait_for(complete.wait(), 2)
        assert len(calls) == 20
        assert len(runtime._child_history) == 2
        assert runtime.state == "ready"
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_binary_eof_retires_child_and_exact_boot_even_if_control_is_healthy(tmp_path):
    retired = []

    async def retirement(boot):
        retired.append(boot)

    entry = _child(
        tmp_path,
        "send({...message,kind:'pong',payload:{}});if(!triggered){triggered=true;bulk.end();}",
    )
    runtime = HomeKitRuntime(
        Path(NODE), entry, build_id="bulk-eof-test", on_retired=retirement, max_restarts=0
    )
    try:
        await runtime.start()
        boot = runtime.boot_id
        await runtime.request("ping", {}, expected="pong")
        await _wait_state(runtime, "failed")
        assert retired == [boot]
        assert runtime.pid is None and runtime.bulk_channel is None
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_child_cannot_send_a_python_only_message(tmp_path):
    entry = _child(
        tmp_path,
        "request(99,'hello',{role:'python',build_id:'bad-direction',protocol:1});"
        "send({...message,kind:'pong',payload:{}});",
    )
    runtime = HomeKitRuntime(
        Path(NODE), entry, build_id="direction-test", on_retired=_noop, max_restarts=0
    )
    try:
        await runtime.start()
        with pytest.raises(HomeKitRuntimeError):
            await runtime.request("ping", {}, expected="pong")
        await _wait_state(runtime, "failed")
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_handler_cannot_reply_to_another_request_even_with_a_valid_error(tmp_path):
    async def handler(envelope):
        return {
            **envelope,
            "request_id": "00000000-0000-0000-0000-000000000999",
            "kind": "error",
            "payload": {"code": "not-ready", "message": ""},
        }

    entry = _child(
        tmp_path,
        "if(!triggered){triggered=true;request(1);}send({...message,kind:'pong',payload:{}});",
    )
    runtime = HomeKitRuntime(
        Path(NODE),
        entry,
        build_id="handler-correlation-test",
        on_request=handler,
        on_retired=_noop,
        max_restarts=0,
    )
    try:
        await runtime.start()
        await runtime.request("ping", {}, expected="pong")
        await _wait_state(runtime, "failed")
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_bulk_open_registers_operation_before_credit_and_keeps_control_responsive(tmp_path):
    from halo_server.homekit.ipc import encode_bulk_header

    scope = {
        **ACCOUNT,
        "camera_id": "front-door",
        "device_generation": 1,
        "privacy_generation": 1,
    }
    operation_id = "00000000-0000-0000-0000-000000000030"
    transfer_id = "00000000-0000-0000-0000-000000000040"
    identity = {"scope": scope, "operation_id": operation_id, "transfer_id": transfer_id}
    opened = {
        **identity,
        "kind": "talkback_pcm",
        "total_bytes": 1920,
        "chunk_bytes": 1920,
        "window_chunks": 1,
    }
    ended = {**identity, "sequence_count": 1, "total_bytes": 1920}
    binary = encode_bulk_header("talkback_pcm", transfer_id, 0, 1920) + b"a" * 1920
    registered, allow = asyncio.Event(), asyncio.Event()
    handles, received = [], []
    runtime = None

    async def sink(data):
        received.append(data)

    async def notice(envelope):
        assert envelope["kind"] == "transfer_open"
        registered.set()
        await allow.wait()
        handles.append(
            runtime.bulk_channel.authorize_receive(
                **identity, kind="talkback_pcm", total_bytes=1920, sink=sink
            )
        )

    entry = _child(
        tmp_path,
        "if(!triggered){triggered=true;request(5,'transfer_open',"
        + json.dumps(opened)
        + ");}send({...message,kind:'pong',payload:{}});",
        "if(message.kind==='transfer_credit' && message.payload.next_sequence===0){"
        "bulk.write(Buffer.from("
        + json.dumps(binary.hex())
        + ",'hex'));request(6,'transfer_end',"
        + json.dumps(ended)
        + ");}",
    )
    runtime = HomeKitRuntime(
        Path(NODE), entry, build_id="bulk-admission-test", on_notice=notice, on_retired=_noop
    )
    try:
        await runtime.start()
        await runtime.request("ping", {}, expected="pong")
        await asyncio.wait_for(registered.wait(), 1)
        assert not handles and not received
        assert await runtime.request("ping", {}, expected="pong") == {}
        allow.set()
        async with asyncio.timeout(2):
            while not handles:
                await asyncio.sleep(0)
            await handles[0].wait()
        assert received == [b"a" * 1920]
        assert runtime.bulk_channel.counts == (0, 0)
        assert runtime.state == "ready"
    finally:
        allow.set()
        await runtime.stop()


@pytest.mark.asyncio
async def test_recent_cache_eviction_never_admits_duplicate_of_active_request(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("halo_server.homekit.runtime.MAX_REQUEST_HISTORY", 2)
    calls = []

    async def handler(envelope):
        calls.append(envelope["request_id"])
        if envelope["request_id"].endswith("000001"):
            await asyncio.Event().wait()
        return PAGE

    entry = _child(
        tmp_path,
        "if(!triggered){triggered=true;request(1);request(2);}"
        "send({...message,kind:'pong',payload:{}});",
        "if(message.kind==='inventory_page'){"
        "if(message.request_id.endsWith('000002'))request(3);else request(1);}",
    )
    runtime = HomeKitRuntime(
        Path(NODE),
        entry,
        build_id="active-replay-test",
        on_request=handler,
        on_retired=_noop,
        max_restarts=0,
    )
    try:
        await runtime.start()
        await runtime.request("ping", {}, expected="pong")
        await _wait_state(runtime, "failed")
        assert len(calls) == 3
        assert len(set(calls)) == 3
        assert not runtime._active_child_ids and not runtime._child_tasks
    finally:
        await runtime.stop()
