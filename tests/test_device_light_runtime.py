"""Real private-socket lighting requests share bounded, revocable device ownership."""

import asyncio
import copy
from uuid import uuid4

import pytest
from test_device_alarm_runtime import wait_results
from test_device_bridge_runtime import (
    envelope,
    fake_child,
    publish_json_result,
    runtime,
    snapshot,
    wait_state,
)

from halo_server.devices import ipc


def light_snapshot():
    value = snapshot()
    value["children"][0].update(
        mappings=["lightbulb"],
        values=dict.fromkeys(ipc.MAPPINGS),
        light={"on": True, "control_enabled": True},
    )
    return value


def light_command(value):
    child = value["children"][0]
    return {
        "scope": value["scope"],
        "registry_revision": value["registry_revision"],
        "sequence": value["sequence"],
        "device_id": child["device_id"],
        "accessory_uuid": child["accessory_uuid"],
        "target": False,
    }


def light_peer(tmp_path, *, mutate="", all_children=False, duplicate=False):
    output = tmp_path / "light-results.json"
    publish = """
if (!issued) {
  issued = true;
  for (const child of CHILDREN) {
    last = {v:1,kind:child.alarm ? 'alarm_set' : 'light_set',boot_id:message.boot_id,
      request_id:require('node:crypto').randomUUID(),payload:{scope:p.scope,
      registry_revision:p.registry_revision,sequence:p.sequence,device_id:child.device_id,
      accessory_uuid:child.accessory_uuid,target:child.alarm ? 'away' : false}};
    MUTATE
    send(last);
  }
}
""".replace("CHILDREN", "p.children" if all_children else "[p.children[0]]").replace(
        "MUTATE", mutate
    )
    result = "results.push(message.payload);" + publish_json_result(output, "results")
    if duplicate:
        result += "if(results.length===1)send(last);"
    return fake_child(
        tmp_path,
        before="let issued=false,last=null,results=[];",
        publish=publish,
        command_result=result,
    ), output


def test_light_protocol_requires_actual_binary_observations_and_narrow_controls():
    value = light_snapshot()
    ipc.encode_frame(envelope("publish", value))
    command = envelope("light_set", light_command(value))
    ipc.validate_envelope(command, role="hap")
    with pytest.raises(ipc.DeviceBridgeProtocolError):
        ipc.validate_envelope(command, role="python")
    for target in (None, 1, "on", [], "default"):
        with pytest.raises(ipc.DeviceBridgeProtocolError):
            ipc.encode_frame(envelope("light_set", {**light_command(value), "target": target}))
    for result in (
        {"accepted": True, "code": "ok", "observed_on": None},
        {"accepted": True, "code": "ok", "observed_on": 1},
        {"accepted": False, "code": "uncertain", "observed_on": True},
    ):
        with pytest.raises(ipc.DeviceBridgeProtocolError):
            ipc.encode_frame(envelope("light_set_result", {**light_command(value), **result}))


@pytest.mark.asyncio
async def test_off_request_can_return_real_on_readback_while_observations_continue(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    admissions = []

    async def handler(payload, admission):
        assert payload["target"] is False
        admissions.append(admission())
        entered.set()
        await release.wait()
        admissions.append(admission())
        return {"accepted": True, "observed_on": True}

    entry, output = light_peer(tmp_path)
    child = runtime(tmp_path, entry=entry, on_light_set=handler)
    value = light_snapshot()
    try:
        await child.start()
        await child.publish(value)
        await asyncio.wait_for(entered.wait(), 2)
        assert await child.request("ping", {}) == {}
        await child.publish({**value, "sequence": 2})
        release.set()
        result = (await wait_results(output))[0]
        assert result["accepted"] and result["observed_on"] is True and result["code"] == "ok"
        assert admissions == [True, True]
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
        "last.payload.device_id='f'.repeat(64);",
        "last.payload.accessory_uuid=require('node:crypto').randomUUID();",
    ],
)
async def test_light_authority_rejection_never_dispatches(tmp_path, mutate):
    calls = []

    async def handler(payload, admission):
        calls.append(payload)
        return {"accepted": True, "observed_on": False}

    entry, output = light_peer(tmp_path, mutate=mutate)
    child = runtime(tmp_path, entry=entry, on_light_set=handler)
    try:
        await child.start()
        await child.publish(light_snapshot())
        result = (await wait_results(output))[0]
        assert result["code"] == "policy-denied" and result["observed_on"] is None
        assert calls == []
    finally:
        await child.stop()


@pytest.mark.asyncio
async def test_mixed_alarm_light_commands_share_one_global_capacity(tmp_path):
    release = asyncio.Event()
    calls = []

    async def handler(payload, admission):
        calls.append(payload)
        await release.wait()
        return {
            "accepted": True,
            **({"observed_on": False} if type(payload["target"]) is bool else {}),
        }

    entry, output = light_peer(tmp_path, all_children=True)
    child = runtime(tmp_path, entry=entry, on_light_set=handler, on_alarm_set=handler)
    value = light_snapshot()
    template = value["children"][0]
    value["children"] = [copy.deepcopy(template) for _ in range(5)]
    for index, row in enumerate(value["children"]):
        row.update(device_id=f"{index:064x}", accessory_uuid=str(uuid4()))
        if index % 2:
            row.update(
                mappings=["security_system"],
                light=None,
                alarm={"mode": "disarmed", "triggered": False, "control_enabled": True},
            )
    try:
        await child.start()
        await child.publish(value)
        assert (await wait_results(output))[0]["code"] == "busy"
        assert len(calls) == 4 and len(child._command_tasks) == 4
        release.set()
        results = await wait_results(output, 5)
        assert sum(row["accepted"] for row in results) == 4
    finally:
        release.set()
        await child.stop()


@pytest.mark.asyncio
async def test_light_policy_revocation_is_permanent_for_pending_command(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()

    async def handler(payload, admission):
        entered.set()
        await release.wait()
        assert not admission()
        return {"accepted": True, "observed_on": False}

    entry, output = light_peer(tmp_path)
    child = runtime(tmp_path, entry=entry, on_light_set=handler)
    value = light_snapshot()
    try:
        await child.start()
        await child.publish(value)
        await asyncio.wait_for(entered.wait(), 2)
        revoked = copy.deepcopy(value)
        revoked["sequence"] = 2
        revoked["children"][0]["light"]["control_enabled"] = False
        await child.publish(revoked)
        await child.publish({**value, "sequence": 3})
        release.set()
        result = (await wait_results(output))[0]
        assert result["code"] == "uncertain" and result["observed_on"] is None
    finally:
        release.set()
        await child.stop()


@pytest.mark.asyncio
async def test_duplicate_light_result_and_disconnect_do_not_repeat_actuation(tmp_path):
    calls = []

    async def handler(payload, admission):
        calls.append(payload)
        return {"accepted": True, "observed_on": False}

    entry, _ = light_peer(tmp_path, duplicate=True)
    child = runtime(tmp_path, entry=entry, on_light_set=handler, max_restarts=0)
    try:
        await child.start()
        await child.publish(light_snapshot())
        await wait_state(child, "failed")
        assert len(calls) == 1 and not child.retirement_pending
    finally:
        await child.stop()


@pytest.mark.asyncio
async def test_invalid_or_missing_light_readback_is_uncertain(tmp_path):
    async def handler(payload, admission):
        return {"accepted": True}

    entry, output = light_peer(tmp_path)
    child = runtime(tmp_path, entry=entry, on_light_set=handler)
    try:
        await child.start()
        await child.publish(light_snapshot())
        result = (await wait_results(output))[0]
        assert result["code"] == "uncertain" and result["observed_on"] is None
    finally:
        await child.stop()
