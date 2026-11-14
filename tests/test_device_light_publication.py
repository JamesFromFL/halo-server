"""Lightbulb mapping: observed state, identity continuity and scoped commands."""

import asyncio
from dataclasses import replace

import pytest
from test_device_alarm_publication import payload as alarm_payload
from test_device_light import light, light_source, policy_request
from test_device_publication import ready

from halo_server.devices.bridge_registry import BridgeRegistryStore, bridge_projection
from halo_server.devices.models import DeviceObservation


async def light_ready(tmp_path):
    service, client, sessions, monitor, providers, sensor = await ready(tmp_path)
    row = light(sessions)
    providers[0].options["on_snapshot"](
        records=(sensor, row),
        revision=2,
        complete=True,
        observed_at=100,
        lights=(light_source(row),),
    )
    await asyncio.sleep(0)
    if monitor.bridge._task:
        await monitor.bridge._task
    publisher = monitor.bridge.publisher
    async with publisher._lock:
        await publisher._step()
    return service, sessions, monitor, providers[0], row


def payload(publisher, device):
    return alarm_payload(publisher, device) | {"target": True}


@pytest.mark.asyncio
async def test_read_only_lightbulb_and_unrelated_sensors_remain_observed(tmp_path):
    service, sessions, monitor, provider, row = await light_ready(tmp_path)
    try:
        publisher = monitor.bridge.publisher
        device = next(
            child for child in publisher._last_snapshot["children"] if child["device_id"] == row.key
        )
        assert device["mappings"] == ["lightbulb"]
        assert device["light"] == {"on": False, "control_enabled": False}
        assert device["alarm"] is None and all(value is None for value in device["values"].values())
        assert not (await publisher._light_set(payload(publisher, device), lambda: True))[
            "accepted"
        ]
        assert all(
            child["light"] is None
            for child in publisher._last_snapshot["children"]
            if child["device_id"] != row.key
        )
    finally:
        await monitor.close()


@pytest.mark.asyncio
async def test_hap_light_fences_and_pending_readback_do_not_self_revoke(tmp_path):
    service, sessions, monitor, provider, row = await light_ready(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def set_light(device, target, **params):
        calls.append((device, target, params["duration_seconds"]))
        entered.set()
        await release.wait()
        assert params["admission"]()
        return {"status": "confirmed", "code": None, "on": True, "possibly_sent": True}

    provider.set_light = set_light
    try:
        await monitor.light.configure(
            **policy_request(sessions, monitor, row, on_duration_seconds=900)
        )
        publisher = monitor.bridge.publisher
        async with publisher._lock:
            await publisher._step()
        device = next(
            child for child in publisher._last_snapshot["children"] if child["device_id"] == row.key
        )
        request = payload(publisher, device)
        for change in ({"sequence": 0}, {"registry_revision": 0}, {"device_id": "f" * 64}):
            assert not (await publisher._light_set(request | change, lambda: True))["accepted"]
        assert not (await publisher._light_set(request, lambda: False))["accepted"]
        task = asyncio.create_task(publisher._light_set(request, lambda: True))
        await entered.wait()
        monitor.light.receive(
            monitor._binding, (light_source(row, pending=True, can_set=False, requested_on=True),)
        )
        async with publisher._lock:
            await publisher._step()
        device = next(
            child for child in publisher._last_snapshot["children"] if child["device_id"] == row.key
        )
        assert device["light"] == {"on": False, "control_enabled": True}
        release.set()
        assert await task == {"accepted": True, "code": "ok", "observed_on": True}
        assert calls == [(row.key, True, 900)]
        await monitor.light.configure(**policy_request(sessions, monitor, row, enabled=False))
        assert not (await publisher._light_set(payload(publisher, device), lambda: True))[
            "accepted"
        ]
    finally:
        release.set()
        await monitor.close()


@pytest.mark.asyncio
async def test_off_uncertain_does_not_invent_off_and_expiry_becomes_unknown(tmp_path):
    service, sessions, monitor, provider, row = await light_ready(tmp_path)

    async def set_light(*args, **params):
        return {
            "status": "uncertain",
            "code": "readback-timeout",
            "on": True,
            "possibly_sent": True,
        }

    provider.set_light = set_light
    try:
        await monitor.light.configure(**policy_request(sessions, monitor, row))
        row = replace(row, observations=(DeviceObservation("light_on", True, 100),))
        provider.options["on_snapshot"](
            records=(row,),
            revision=3,
            complete=True,
            observed_at=100,
            lights=(light_source(row, on=True),),
        )
        await asyncio.sleep(0)
        if monitor.bridge._task:
            await monitor.bridge._task
        publisher = monitor.bridge.publisher
        async with publisher._lock:
            await publisher._step()
        device = next(
            child for child in publisher._last_snapshot["children"] if child["device_id"] == row.key
        )
        result = await publisher._light_set(
            payload(publisher, device) | {"target": False}, lambda: True
        )
        assert not result["accepted"] and result["code"] == "uncertain"
        assert device["light"]["on"] is True
        monitor._clock = lambda: 1000
        current = publisher._snapshot(
            sessions.active_scope,
            monitor.bridge.registry.profile(sessions.active_scope.profile_id),
            99,
        )
        device = next(child for child in current["children"] if child["device_id"] == row.key)
        assert device["light"] == {"on": None, "control_enabled": False}
    finally:
        await monitor.close()


@pytest.mark.asyncio
async def test_generic_switch_identity_enriches_without_changing_uuid(tmp_path):
    service, sessions, monitor, provider, row = await light_ready(tmp_path)
    try:
        store = BridgeRegistryStore(tmp_path / "old-registry.json")
        profile = sessions.active_scope.profile_id
        old = replace(row, kind="switch")
        registry = store.configure(
            profile,
            expected_revision=0,
            enabled=True,
            auto_import=True,
            records=(old,),
            excluded_kinds=("switch",),
        )
        before = next(entry for entry in registry.entries if entry.device_id == row.key)
        assert not before.mappings
        registry = store.reconcile(profile, (row,))
        after = next(entry for entry in registry.entries if entry.device_id == row.key)
        assert after.accessory_uuid == before.accessory_uuid
        assert after.mappings == ("lightbulb",) and after.kind == "beams-light"
        assert not after.selected  # The old switch-family exclusion still applies.
        for wrong in (replace(row, model="group.light-group.beams"), replace(row, model="switch")):
            assert not bridge_projection(registry, profile, (wrong,))["devices"][0]["eligible"]
    finally:
        await monitor.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", [{"supported": False}, {"family": "unknown"}, {"model": "group.light-group.beams"}]
)
async def test_unqualified_enrichment_does_not_break_registry_reconciliation(tmp_path, change):
    service, sessions, monitor, provider, row = await light_ready(tmp_path)
    try:
        store = BridgeRegistryStore(tmp_path / "old-registry.json")
        profile = sessions.active_scope.profile_id
        registry = store.configure(
            profile,
            expected_revision=0,
            enabled=True,
            auto_import=True,
            records=(replace(row, kind="switch"),),
        )
        original = registry.entries[0]
        result = store.reconcile(profile, (replace(row, **change),))
        assert result.entries[0].accessory_uuid == original.accessory_uuid
        assert result.entries[0].kind == "switch" and not result.entries[0].mappings
    finally:
        await monitor.close()


@pytest.mark.asyncio
async def test_newer_source_state_wins_over_older_confirmed_callback(tmp_path):
    service, sessions, monitor, provider, row = await light_ready(tmp_path)

    async def set_light(*args, **params):
        monitor.light.receive(monitor._binding, (light_source(row, revision=5, on=False),))
        return {"status": "confirmed", "code": None, "on": True, "possibly_sent": True}

    provider.set_light = set_light
    try:
        await monitor.light.configure(**policy_request(sessions, monitor, row))
        publisher = monitor.bridge.publisher
        async with publisher._lock:
            await publisher._step()
        device = next(
            child for child in publisher._last_snapshot["children"] if child["device_id"] == row.key
        )
        result = await publisher._light_set(payload(publisher, device), lambda: True)
        assert result == {"accepted": True, "code": "ok", "observed_on": False}
    finally:
        await monitor.close()
