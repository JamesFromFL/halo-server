"""Keypad HAP mapping and command lanes; synthetic records and private IPC only."""

import asyncio
import copy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_device_bridge_registry import PROFILE, configure, record
from test_device_bridge_runtime import envelope, snapshot

from halo_server.devices import ipc
from halo_server.devices.bridge_registry import BridgeRegistryStore, bridge_projection
from halo_server.devices.runtime import DeviceBridgeRuntime


def keypad_snapshot(*, available=True, locked=False):
    value = snapshot()
    value["children"][0].update(
        mappings=["keypad_light"],
        available=available,
        values=dict.fromkeys(ipc.MAPPINGS),
        keypad={
            "brightness": 42 if available else None,
            "locked": locked,
            "control_enabled": True,
            "on_brightness": 42,
        },
    )
    return value


def command(value, target):
    child = value["children"][0]
    return {key: value[key] for key in ("scope", "registry_revision", "sequence")} | {
        "device_id": child["device_id"],
        "accessory_uuid": child["accessory_uuid"],
        "target": target,
    }


def test_registry_qualifies_exact_keypad_and_retains_identity_during_motion_enrichment(tmp_path):
    store = BridgeRegistryStore(tmp_path / "registry.json")
    row = record(2, kind="keypad", model="security-keypad", capabilities=())
    contact = record()
    original = configure(store, (contact, row))
    enriched = replace(row, capabilities=("keypad-brightness",))
    current = store.reconcile(PROFILE, (contact, enriched))
    assert [(e.device_id, e.accessory_uuid) for e in current.entries] == [
        (e.device_id, e.accessory_uuid) for e in original.entries
    ]
    entry = next(e for e in current.entries if e.device_id == row.key)
    assert entry.mappings == ("keypad_light",) and entry.selected
    fake_motion = replace(enriched, capabilities=("keypad-brightness", "motion"))
    assert store.reconcile(PROFILE, (contact, fake_motion)) == current
    motion = replace(
        fake_motion, capability_evidence=("ring-hub-device-schema", "ring-keypad-motion-impulse")
    )
    admitted = store.reconcile(PROFILE, (contact, motion))
    next_entry = next(e for e in admitted.entries if e.device_id == row.key)
    assert next_entry.accessory_uuid == entry.accessory_uuid and next_entry.mappings == (
        "keypad_light",
        "motion",
    )
    retained = store.reconcile(PROFILE, (contact, enriched))
    assert retained == admitted
    assert next(
        e
        for e in bridge_projection(retained, PROFILE, (contact, enriched))["devices"]
        if e["id"] == row.key
    )["eligible"]
    bad = replace(enriched, model="unknown-keypad")
    rejected = store.reconcile(PROFILE, (contact, bad))
    assert not next(e for e in rejected.entries if e.device_id == row.key).qualified


@pytest.mark.parametrize("value", [True, -1, 101, 4.5, "42"])
def test_protocol_rejects_invalid_percentage_observation_and_command(value):
    snap = keypad_snapshot()
    snap["children"][0]["keypad"]["brightness"] = value
    with pytest.raises(ipc.DeviceBridgeProtocolError):
        ipc.encode_frame(envelope("publish", snap))
    with pytest.raises(ipc.DeviceBridgeProtocolError):
        ipc.encode_frame(envelope("keypad_set", command(keypad_snapshot(), value)))


def test_keypad_protocol_requires_mapping_state_and_typed_independent_lock():
    snap = keypad_snapshot(available=False, locked=True)
    ipc.encode_frame(envelope("publish", snap))
    lock = envelope("keypad_lock", command(snap, False))
    ipc.validate_envelope(lock, role="hap")
    with pytest.raises(ipc.DeviceBridgeProtocolError):
        ipc.validate_envelope(lock, role="python")
    for target in (1, None, "false"):
        with pytest.raises(ipc.DeviceBridgeProtocolError):
            ipc.encode_frame(envelope("keypad_lock", command(snap, target)))
    for change in ({"keypad": None}, {"mappings": ["contact", "keypad_light"]}):
        bad = copy.deepcopy(snap)
        bad["children"][0].update(change)
        with pytest.raises(ipc.DeviceBridgeProtocolError):
            ipc.encode_frame(envelope("publish", bad))
    ipc.encode_frame(
        envelope(
            "keypad_lock_result",
            command(snap, True) | {"accepted": True, "code": "ok", "observed_locked": True},
        )
    )
    with pytest.raises(ipc.DeviceBridgeProtocolError):
        ipc.encode_frame(
            envelope(
                "keypad_set_result",
                command(snap, 20) | {"accepted": True, "code": "ok", "observed_brightness": None},
            )
        )


@pytest.mark.asyncio
async def test_runtime_lock_separate_lane_revokes_dimmer_but_accepts_own_persistence():
    entered, release = asyncio.Event(), asyncio.Event()
    results = []
    admissions = []

    async def dimmer(payload, admission):
        entered.set()
        await release.wait()
        admissions.append(admission())
        return {"accepted": True, "observed_brightness": 20}

    async def lock(payload, admission):
        updated = copy.deepcopy(child._publication)
        updated["sequence"] += 1
        updated["children"][0]["keypad"]["locked"] = payload["target"]
        child._accept_publication(updated)
        assert admission()
        return {"accepted": True, "observed_locked": True}

    child = DeviceBridgeRuntime(
        Path("/synthetic/node"),
        Path("/synthetic/entry"),
        build_id="test",
        on_keypad_set=dimmer,
        on_keypad_lock=lock,
    )
    child._boot_id = str(uuid4())
    child._handshake_complete = True
    child._writer = SimpleNamespace()

    async def send(message, result):
        results.append((message["kind"], result))

    child._send_command_result = send
    snap = keypad_snapshot()
    child._accept_publication(snap)

    async def dispatch(kind, target):
        request = envelope(kind, command(snap, target))
        request["boot_id"] = child._boot_id
        await child._dispatch_command(request)

    await dispatch("keypad_set", 20)
    await entered.wait()
    await dispatch("keypad_lock", True)
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(*tuple(child._command_tasks.values()))
    assert admissions == [False]
    assert dict(results)["keypad_lock"]["accepted"] is True
    assert dict(results)["keypad_set"] == {"accepted": False, "code": "uncertain"}
    assert not child._command_devices


@pytest.mark.asyncio
async def test_runtime_offline_lock_is_allowed_while_brightness_never_dispatches():
    calls = []
    results = []

    async def handler(payload, admission):
        assert admission()
        calls.append(payload["target"])
        return {"accepted": True, "observed_locked": False}

    child = DeviceBridgeRuntime(
        Path("/synthetic/node"),
        Path("/synthetic/entry"),
        build_id="test",
        on_keypad_lock=handler,
        on_keypad_set=handler,
    )
    child._boot_id = str(uuid4())
    child._handshake_complete = True
    child._writer = SimpleNamespace()

    async def send(message, result):
        results.append((message["kind"], result))

    child._send_command_result = send
    snap = keypad_snapshot(available=False, locked=True)
    child._accept_publication(snap)
    for kind, target in [("keypad_set", 20), ("keypad_lock", False)]:
        request = envelope(kind, command(snap, target))
        request["boot_id"] = child._boot_id
        await child._dispatch_command(request)
    await asyncio.gather(*tuple(child._command_tasks.values()))
    assert calls == [False]
    assert dict(results)["keypad_set"]["code"] == "policy-denied"
    assert dict(results)["keypad_lock"]["accepted"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,target,observed",
    [("keypad_set", 25, "observed_brightness"), ("keypad_lock", True, "observed_locked")],
)
async def test_real_private_socket_carries_keypad_request_and_correlated_readback(
    tmp_path, kind, target, observed
):
    import json

    from test_device_alarm_runtime import wait_results
    from test_device_bridge_runtime import fake_child, runtime

    output = tmp_path / "result.json"
    peer = fake_child(
        tmp_path,
        before="let sent=false;",
        publish="""
if (!sent) { sent=true; const child=p.children[0]; send({v:1,kind:KIND,boot_id:message.boot_id,
request_id:require('node:crypto').randomUUID(),payload:{scope:p.scope,registry_revision:p.registry_revision,
sequence:p.sequence,device_id:child.device_id,
accessory_uuid:child.accessory_uuid,target:TARGET}}); }
""".replace("KIND", json.dumps(kind)).replace("TARGET", json.dumps(target)),
        command_result='require("node:fs").writeFileSync('
        + json.dumps(str(output))
        + ",JSON.stringify([message.payload]));",
    )
    peer.write_text(
        peer.read_text().replace(
            "['alarm_set_result','light_set_result']",
            "['alarm_set_result','light_set_result','keypad_set_result','keypad_lock_result']",
        )
    )

    async def handler(payload, admission):
        assert admission() and payload["target"] == target
        return {"accepted": True, observed: target}

    child = runtime(tmp_path, entry=peer, **{"on_" + kind: handler})
    try:
        await child.start()
        await child.publish(keypad_snapshot())
        result = (await wait_results(output))[0]
        assert result["accepted"] and result[observed] == target and result["target"] == target
    finally:
        await child.stop()
