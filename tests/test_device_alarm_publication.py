"""SecuritySystem mapping and composed HomeKit-to-provider command admission."""

import asyncio

import pytest
from test_device_alarm import alarm_source, panel, policy_request
from test_device_publication import ready


async def alarm_ready(tmp_path):
    service, client, sessions, monitor, providers, sensor = await ready(tmp_path)
    row = panel(sessions)
    providers[0].options["on_snapshot"](
        records=(sensor, row),
        revision=2,
        complete=True,
        observed_at=100,
        alarms=(alarm_source(row),),
    )
    await asyncio.sleep(0)
    if monitor.bridge._task:
        await monitor.bridge._task
    publisher = monitor.bridge.publisher
    async with publisher._lock:
        await publisher._step()
    return service, sessions, monitor, providers[0], row


def payload(publisher, device):
    snapshot = publisher._last_snapshot
    return {
        "scope": snapshot["scope"],
        "registry_revision": snapshot["registry_revision"],
        "sequence": snapshot["sequence"],
        "device_id": device["device_id"],
        "accessory_uuid": device["accessory_uuid"],
        "target": "home",
    }


@pytest.mark.asyncio
async def test_alarm_autoimport_observes_but_cannot_actuate_without_separate_grant(tmp_path):
    service, sessions, monitor, provider, row = await alarm_ready(tmp_path)
    try:
        publisher = monitor.bridge.publisher
        snapshot = publisher._last_snapshot
        device = next(child for child in snapshot["children"] if child["device_id"] == row.key)
        assert device["mappings"] == ["security_system"]
        assert device["alarm"] == {"mode": "disarmed", "triggered": False, "control_enabled": False}
        assert all(value is None for value in device["values"].values())
        result = await publisher._alarm_set(payload(publisher, device), lambda: True)
        assert result == {"accepted": False, "code": "policy-denied"}
        assert (
            next(child for child in snapshot["children"] if child["device_id"] != row.key)["alarm"]
            is None
        )
    finally:
        await monitor.close()


@pytest.mark.asyncio
async def test_hap_request_bound_to_current_boot_graph_and_policy(tmp_path):
    service, sessions, monitor, provider, row = await alarm_ready(tmp_path)
    calls = []

    async def set_mode(device, target, **kwargs):
        assert kwargs["admission"]()
        calls.append((device, target))
        return {"status": "confirmed", "code": None, "mode": target, "possibly_sent": True}

    provider.set_alarm_mode = set_mode
    try:
        await monitor.alarm.configure(**policy_request(sessions, monitor, row))
        publisher = monitor.bridge.publisher
        async with publisher._lock:
            await publisher._step()
        device = next(
            child for child in publisher._last_snapshot["children"] if child["device_id"] == row.key
        )
        request = payload(publisher, device)
        for changed in (
            {"sequence": request["sequence"] - 1},
            {"registry_revision": 0},
            {"device_id": "a" * 64},
        ):
            assert not (await publisher._alarm_set(request | changed, lambda: True))["accepted"]
        assert not (await publisher._alarm_set(request, lambda: False))["accepted"]
        assert (await publisher._alarm_set(request, lambda: True))["accepted"]
        assert calls == [(row.key, "home")]
        # A saved disable revokes an already-delivered HAP graph immediately.
        await monitor.alarm.configure(**policy_request(sessions, monitor, row, enabled=False))
        assert not (await publisher._alarm_set(request, lambda: True))["accepted"]
        assert len(calls) == 1
    finally:
        await monitor.close()


@pytest.mark.asyncio
async def test_unknown_alarm_state_stays_unknown_and_outcome_is_not_success(tmp_path):
    service, sessions, monitor, provider, row = await alarm_ready(tmp_path)

    async def set_mode(*args, **kwargs):
        return {
            "status": "uncertain",
            "code": "outcome-unknown",
            "mode": None,
            "possibly_sent": True,
        }

    provider.set_alarm_mode = set_mode
    try:
        await monitor.alarm.configure(**policy_request(sessions, monitor, row))
        publisher = monitor.bridge.publisher
        async with publisher._lock:
            await publisher._step()
        device = next(
            child for child in publisher._last_snapshot["children"] if child["device_id"] == row.key
        )
        assert await publisher._alarm_set(payload(publisher, device), lambda: True) == {
            "accepted": False,
            "code": "uncertain",
        }
        monitor._clock = lambda: 1000
        snapshot = publisher._snapshot(
            sessions.active_scope,
            monitor.bridge.registry.profile(sessions.active_scope.profile_id),
            99,
        )
        device = next(child for child in snapshot["children"] if child["device_id"] == row.key)
        assert device["alarm"] == {"mode": None, "triggered": None, "control_enabled": False}
    finally:
        await monitor.close()


@pytest.mark.asyncio
async def test_pending_observation_does_not_revoke_its_own_hap_request(tmp_path):
    service, sessions, monitor, provider, row = await alarm_ready(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()

    async def set_mode(*args, **kwargs):
        entered.set()
        await release.wait()
        assert kwargs["admission"]()
        return {"status": "confirmed", "code": None, "mode": "home", "possibly_sent": True}

    provider.set_alarm_mode = set_mode
    try:
        await monitor.alarm.configure(**policy_request(sessions, monitor, row))
        publisher = monitor.bridge.publisher
        async with publisher._lock:
            await publisher._step()
        device = next(
            child for child in publisher._last_snapshot["children"] if child["device_id"] == row.key
        )
        task = asyncio.create_task(publisher._alarm_set(payload(publisher, device), lambda: True))
        await entered.wait()
        monitor.alarm.receive(
            monitor._binding,
            (
                alarm_source(
                    row,
                    pending=True,
                    can_set_mode=False,
                    requested_mode="home",
                    reason="command-pending",
                ),
            ),
        )
        async with publisher._lock:
            await publisher._step()
        current = next(
            child for child in publisher._last_snapshot["children"] if child["device_id"] == row.key
        )
        assert current["alarm"]["control_enabled"]
        assert not monitor.snapshot()["devices"][
            1 if monitor.snapshot()["devices"][0]["id"] != row.key else 0
        ]["alarm"]["can_set_mode"]
        release.set()
        assert (await task)["accepted"]
    finally:
        release.set()
        await monitor.close()
