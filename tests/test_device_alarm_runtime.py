"""Actual private IPC alarm requests remain scoped, bounded, and independently observed."""

import asyncio
import copy
import json
import time
from types import SimpleNamespace

import pytest
from test_device_bridge_runtime import envelope, fake_child, runtime, snapshot, wait_state

from halo_server.devices import ipc
from halo_server.devices.runtime import DeviceBridgeRuntimeError


def alarm_snapshot():
    value = snapshot()
    child = value["children"][0]
    child.update(
        mappings=["security_system"],
        values=dict.fromkeys(ipc.MAPPINGS),
        alarm={"mode": "disarmed", "triggered": False, "control_enabled": True},
    )
    return value


def command(value):
    child = value["children"][0]
    return {
        "scope": value["scope"],
        "registry_revision": value["registry_revision"],
        "sequence": value["sequence"],
        "device_id": child["device_id"],
        "accessory_uuid": child["accessory_uuid"],
        "target": "away",
    }


def command_peer(tmp_path, *, mutate="", count=1, duplicate=False):
    output = tmp_path / "alarm-results.json"
    publish = (
        """
if (!issued) {
 issued = true;
 const child = p.children[0];
 last = {v:1,kind:'alarm_set',boot_id:message.boot_id,
   request_id:require('node:crypto').randomUUID(),payload:{scope:p.scope,
   registry_revision:p.registry_revision,sequence:p.sequence,
   device_id:child.device_id,accessory_uuid:child.accessory_uuid,target:'away'}};
"""
        + mutate
    )
    publish += (
        f"for(let i=0;i<{count};i++) "
        "send({...last,request_id:require('node:crypto').randomUUID()});"
        if not duplicate
        else "send(last);"
    )
    publish += "}\n"
    result = (
        "results.push(message.payload);require('node:fs').writeFileSync("
        f"{json.dumps(str(output))},JSON.stringify(results));"
    )
    if duplicate:
        result += "if(results.length===1)send(last);"
    entry = fake_child(
        tmp_path,
        before="let issued=false,last=null,results=[];",
        publish=publish,
        alarm_result=result,
    )
    return entry, output


async def wait_results(path, count=1):
    async with asyncio.timeout(4):
        while not path.exists() or len(json.loads(path.read_text())) < count:
            await asyncio.sleep(0.005)
    return json.loads(path.read_text())


def test_alarm_protocol_accepts_only_explicit_mode_commands_and_state():
    value = alarm_snapshot()
    ipc.encode_frame(envelope("publish", value))
    request = envelope("alarm_set", command(value))
    ipc.validate_envelope(request, role="hap")
    with pytest.raises(ipc.DeviceBridgeProtocolError):
        ipc.validate_envelope(request, role="python")
    ipc.encode_frame(
        {
            **request,
            "kind": "alarm_set_result",
            "payload": {**request["payload"], "accepted": False, "code": "uncertain"},
        }
    )
    for target in ("night", "panic", "bypass", "arm_away", None, []):
        with pytest.raises(ipc.DeviceBridgeProtocolError):
            ipc.encode_frame(envelope("alarm_set", {**command(value), "target": target}))
    for change in (
        {"mode": "night"},
        {"mode": []},
        {"triggered": 0},
        {"control_enabled": 1},
        {"panic": False},
    ):
        changed = copy.deepcopy(value)
        changed["children"][0]["alarm"].update(change)
        with pytest.raises(ipc.DeviceBridgeProtocolError):
            ipc.encode_frame(envelope("publish", changed))


@pytest.mark.asyncio
async def test_alarm_confirmation_does_not_block_new_observations_or_heartbeat(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    admissions, calls = [], []

    async def handler(payload, admission):
        calls.append(payload)
        admissions.append(admission())
        entered.set()
        await release.wait()
        admissions.append(admission())
        return {"accepted": True}

    entry, output = command_peer(tmp_path)
    child = runtime(tmp_path, entry=entry, on_alarm_set=handler)
    initial = alarm_snapshot()
    try:
        await child.start()
        await child.publish(initial)
        await asyncio.wait_for(entered.wait(), 2)
        assert await child.request("ping", {}) == {}
        observed = copy.deepcopy(initial)
        observed["sequence"] += 1
        observed["children"][0]["alarm"]["mode"] = "away"
        assert (await child.publish(observed))["accepted"]
        release.set()
        results = await wait_results(output)
        assert results[0]["accepted"] is True
        assert admissions == [True, True]
        assert calls == [command(initial)]
    finally:
        release.set()
        await child.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutate",
    [
        "last.payload.sequence+=1;",
        "last.payload.registry_revision+=1;",
        "last.payload.scope.account_generation+=1;",
        "last.payload.accessory_uuid=require('node:crypto').randomUUID();",
        "last.payload.device_id='f'.repeat(64);",
    ],
)
async def test_stale_alarm_authority_never_reaches_provider_handler(tmp_path, mutate):
    calls = []

    async def handler(payload, admission):
        calls.append(payload)
        return {"accepted": True, "code": "ok"}

    entry, output = command_peer(tmp_path, mutate=mutate)
    child = runtime(tmp_path, entry=entry, on_alarm_set=handler)
    try:
        await child.start()
        await child.publish(alarm_snapshot())
        assert (await wait_results(output))[0]["code"] == "policy-denied"
        assert calls == []
    finally:
        await child.stop()


@pytest.mark.asyncio
async def test_readonly_or_expired_alarm_is_denied_without_handler(tmp_path):
    entry, output = command_peer(tmp_path)
    child = runtime(tmp_path, entry=entry)
    value = alarm_snapshot()
    value["children"][0]["alarm"]["control_enabled"] = False
    try:
        await child.start()
        await child.publish(value)
        assert (await wait_results(output))[0]["accepted"] is False
        expired = alarm_snapshot()
        expired["expires_at_ms"] = int(time.monotonic() * 1000) - 1
        child._accept_publication(expired)
        assert not child._alarm_current(command(expired), child.boot_id, initial=True)
    finally:
        await child.stop()


@pytest.mark.asyncio
async def test_alarm_requests_are_one_per_device_and_duplicate_ids_never_reexecute(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def handler(payload, admission):
        calls.append(payload)
        entered.set()
        await release.wait()
        return {"accepted": True, "code": "ok"}

    entry, output = command_peer(tmp_path, count=2)
    child = runtime(tmp_path, entry=entry, on_alarm_set=handler)
    try:
        await child.start()
        await child.publish(alarm_snapshot())
        await asyncio.wait_for(entered.wait(), 2)
        assert (await wait_results(output))[0]["code"] == "busy"
        release.set()
        assert len(await wait_results(output, 2)) == 2
        assert len(calls) == 1
    finally:
        release.set()
        await child.stop()
    entry, output = command_peer(tmp_path, duplicate=True)
    child = runtime(tmp_path, entry=entry, on_alarm_set=handler, max_restarts=0)
    try:
        await child.start()
        await child.publish(alarm_snapshot())
        await wait_state(child, "failed")
        assert len(calls) == 2
        assert not child.retirement_pending
    finally:
        await child.stop()


@pytest.mark.asyncio
async def test_control_revocation_cannot_be_undone_during_readback(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    final_admission = []

    async def handler(payload, admission):
        entered.set()
        await release.wait()
        final_admission.append(admission())
        return {"accepted": True, "code": "ok"}

    entry, output = command_peer(tmp_path)
    child = runtime(tmp_path, entry=entry, on_alarm_set=handler)
    initial = alarm_snapshot()
    try:
        await child.start()
        await child.publish(initial)
        await asyncio.wait_for(entered.wait(), 2)
        disabled = copy.deepcopy(initial)
        disabled["sequence"] = 2
        disabled["children"][0]["alarm"]["control_enabled"] = False
        await child.publish(disabled)
        enabled = copy.deepcopy(initial)
        enabled["sequence"] = 3
        await child.publish(enabled)
        release.set()
        assert (await wait_results(output))[0]["code"] == "uncertain"
        assert final_admission == [False]
    finally:
        release.set()
        await child.stop()


@pytest.mark.asyncio
async def test_fresh_observation_cannot_revive_expired_command(tmp_path, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    now = [time.monotonic()]
    monkeypatch.setattr(
        "halo_server.devices.runtime.time", SimpleNamespace(monotonic=lambda: now[0])
    )

    async def handler(payload, admission):
        entered.set()
        await release.wait()
        assert not admission()
        return {"accepted": True}

    entry, output = command_peer(tmp_path)
    child = runtime(tmp_path, entry=entry, on_alarm_set=handler)
    initial = alarm_snapshot()
    try:
        await child.start()
        await child.publish(initial)
        await asyncio.wait_for(entered.wait(), 2)
        now[0] += 20
        refreshed = copy.deepcopy(initial)
        refreshed["sequence"] = 2
        refreshed["expires_at_ms"] = int(now[0] * 1000) + 15000
        await child.publish(refreshed)
        release.set()
        assert (await wait_results(output))[0]["code"] == "uncertain"
    finally:
        release.set()
        await child.stop()


@pytest.mark.asyncio
async def test_disconnect_reaps_hap_but_does_not_cancel_owned_alarm_confirmation(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    completed, cancelled = [], []

    async def handler(payload, admission):
        entered.set()
        try:
            await release.wait()
            completed.append(admission())
            return {"accepted": False, "code": "uncertain"}
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    entry, _ = command_peer(tmp_path)
    child = runtime(tmp_path, entry=entry, on_alarm_set=handler)
    try:
        await child.start()
        await child.publish(alarm_snapshot())
        await asyncio.wait_for(entered.wait(), 2)
        process = child._process
        child.invalidate()
        stopped = asyncio.create_task(child.stop())
        await asyncio.sleep(0.05)
        assert not stopped.done()
        assert process.returncode is not None
        assert child.retirement_pending
        release.set()
        await stopped
        assert completed == [False] and cancelled == []
        assert not child.retirement_pending and not child._alarm_tasks
    finally:
        release.set()
        await child.stop()


@pytest.mark.asyncio
async def test_stuck_callback_keeps_retirement_owned_until_later_join(tmp_path, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr("halo_server.devices.runtime.ALARM_RETIRE_SECONDS", 0.01)

    async def handler(payload, admission):
        entered.set()
        await release.wait()
        return {"accepted": False, "code": "uncertain"}

    entry, _ = command_peer(tmp_path)
    child = runtime(tmp_path, entry=entry, on_alarm_set=handler, max_restarts=0)
    try:
        await child.start()
        await child.publish(alarm_snapshot())
        await asyncio.wait_for(entered.wait(), 2)
        child.invalidate()
        with pytest.raises(DeviceBridgeRuntimeError, match="still owned"):
            await child.stop()
        assert child.retirement_pending
        with pytest.raises(DeviceBridgeRuntimeError):
            await child.start()
        release.set()
        await child.stop()
        assert not child.retirement_pending
    finally:
        release.set()
        await child.stop()
