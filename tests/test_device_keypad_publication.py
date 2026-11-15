"""Composed HAP keypad publication uses persisted policy and actual readback only."""

import asyncio
from dataclasses import replace

import pytest
from test_device_alarm_publication import payload
from test_device_keypad import keypad, policy_request, source
from test_device_publication import ready

from halo_server.devices.models import DeviceObservation


async def fixture(tmp_path):
    service, client, sessions, monitor, providers, sensor = await ready(tmp_path)
    row = keypad(sessions)
    provider = providers[0]
    provider.options["on_snapshot"](
        records=(sensor, row), revision=2, complete=True, observed_at=100, keypads=(source(row),)
    )
    await asyncio.sleep(0)
    if monitor.bridge._task:
        await monitor.bridge._task
    publisher = monitor.bridge.publisher
    await monitor.keypad.configure(**policy_request(sessions, monitor, row))
    async with publisher._lock:
        await publisher._step()
    return sessions, monitor, provider, row


def child(publisher, row):
    return next(c for c in publisher._last_snapshot["children"] if c["device_id"] == row.key)


@pytest.mark.asyncio
@pytest.mark.parametrize("newer", [False, True])
async def test_confirmed_readback_wins_baseline_but_newer_observation_wins_result(tmp_path, newer):
    sessions, monitor, provider, row = await fixture(tmp_path)
    publisher = monitor.bridge.publisher

    async def set_brightness(device, target, **params):
        assert params["admission"]()
        if newer:
            updated = replace(row, observations=(DeviceObservation("keypad_brightness", 33, 100),))
            provider.options["on_snapshot"](
                records=(updated,),
                revision=3,
                complete=True,
                observed_at=100,
                keypads=(source(updated, brightness=33, revision=5),),
            )
            await asyncio.sleep(0)
        return {"status": "confirmed", "code": None, "brightness": 25, "possibly_sent": True}

    provider.set_keypad_brightness = set_brightness
    try:
        request = payload(publisher, child(publisher, row)) | {"target": 25}
        result = await publisher._keypad_set(request, lambda: True)
        assert result == {
            "accepted": True,
            "code": "ok",
            "observed_brightness": 33 if newer else 25,
        }
    finally:
        await monitor.close()


@pytest.mark.asyncio
async def test_local_lock_persistence_is_published_without_provider_command(tmp_path):
    sessions, monitor, provider, row = await fixture(tmp_path)
    publisher = monitor.bridge.publisher
    calls = []

    async def set_brightness(*args, **kwargs):
        calls.append(args)
        raise AssertionError("No physical command permitted")

    provider.set_keypad_brightness = set_brightness
    try:
        request = payload(publisher, child(publisher, row)) | {"target": True}
        assert await publisher._keypad_lock(request, lambda: True) == {
            "accepted": True,
            "code": "ok",
            "observed_locked": True,
        }
        async with publisher._lock:
            await publisher._step()
        assert child(publisher, row)["keypad"]["locked"] is True
        assert not (
            await publisher._keypad_set(
                payload(publisher, child(publisher, row)) | {"target": 20}, lambda: True
            )
        )["accepted"]
        assert calls == []
        # Removing observations never invents an unlocked state for a retained identity.
        provider.options["on_snapshot"](
            records=(), revision=3, complete=True, observed_at=100, keypads=()
        )
        await asyncio.sleep(0)
        if monitor.bridge._task:
            await monitor.bridge._task
        async with publisher._lock:
            await publisher._step()
        assert child(publisher, row)["keypad"]["locked"] is True
        assert child(publisher, row)["keypad"]["brightness"] is None
    finally:
        await monitor.close()
