"""Physical Beams control grants, source fences, observed state and no replay."""

import asyncio
import json
import threading
from dataclasses import replace
from uuid import uuid4

import pytest
from test_device_monitor import fixture, preference, record

from halo_server.api.admin import AdminProtocolError
from halo_server.devices.light_policy import (
    LightPolicy,
    LightPolicyConflict,
    LightPolicyError,
    LightPolicyStore,
)
from halo_server.devices.models import DeviceObservation


def light(sessions, identifier="light"):
    return replace(
        record(sessions, identifier=identifier, name="Path light"),
        kind="beams-light",
        model="switch.beams",
        capabilities=("switch",),
        capability_evidence=("ring-hub-device-schema",),
        observations=(DeviceObservation("light_on", False, 100),),
    )


def light_source(row, **changes):
    return {
        "device_id": row.key,
        "location_id": row.identity.location_key,
        "epoch": 1,
        "revision": 4,
        "readback_revision": 1,
        "on": False,
        "authorization": "allowed",
        "can_set": True,
        "reason": None,
        "pending": False,
        "outcome_unknown": False,
        "requested_on": None,
        **changes,
    }


async def ready(tmp_path):
    service, client, sessions, monitor, providers = fixture(tmp_path)
    monitor.bridge.publisher.start = monitor.bridge.publisher.notify
    await monitor.set_enabled(**preference(sessions))
    row = light(sessions)
    providers[0].options["on_snapshot"](
        records=(row,),
        revision=1,
        complete=True,
        observed_at=100,
        lights=(light_source(row),),
    )
    await asyncio.sleep(0)
    return service, client, sessions, monitor, providers[0], row


def policy_request(sessions, monitor, row, **changes):
    return {
        "profile_id": sessions.active_scope.profile_id,
        "account_generation": sessions.active_scope.generation,
        "device_id": row.key,
        "expected_revision": monitor.light.policy.revision,
        "enabled": True,
        "on_duration_seconds": 60,
        **changes,
    }


def command(sessions, monitor, row, **changes):
    return {
        "profile_id": sessions.active_scope.profile_id,
        "account_generation": sessions.active_scope.generation,
        "device_id": row.key,
        "provider_generation": monitor._binding.generation,
        "expected_epoch": 1,
        "expected_revision": 4,
        "expected_policy_revision": monitor.light.policy.revision,
        "target": True,
        "operation_id": str(uuid4()),
        **changes,
    }


def test_policy_default_private_duration_cas_and_deletion(tmp_path):
    store = LightPolicyStore(tmp_path / "lights.json")
    assert store.load() == LightPolicy() and not store.path.exists()
    profile = str(uuid4())
    first = store.configure(profile, "a" * 64, True, 300, expected_revision=0)
    assert first.allows(profile, "a" * 64)
    assert first.preference(profile, "a" * 64).on_duration_seconds == 300
    assert not first.allows(str(uuid4()), "a" * 64)
    assert store.path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(LightPolicyConflict):
        store.configure(profile, "a" * 64, False, 60, expected_revision=0)
    disabled = store.configure(profile, "a" * 64, False, 300, expected_revision=1)
    assert not disabled.allows(profile, "a" * 64)
    assert disabled.preference(profile, "a" * 64).on_duration_seconds == 300
    store.path.unlink()
    assert not store.load().preferences


@pytest.mark.parametrize("duration", [0, True, 1, 32767, 60.0, "60", None])
def test_unbounded_or_wrong_duration_never_persists(tmp_path, duration):
    store = LightPolicyStore(tmp_path / "lights.json")
    with pytest.raises(LightPolicyError):
        store.configure(str(uuid4()), "a" * 64, True, duration, expected_revision=0)
    assert not store.path.exists()


def test_duplicate_device_preferences_are_invalid(tmp_path):
    store = LightPolicyStore(tmp_path / "lights.json")
    entry = {
        "profile_id": str(uuid4()),
        "device_id": "a" * 64,
        "enabled": True,
        "on_duration_seconds": 60,
    }
    store.path.write_text(
        json.dumps(
            {
                "version": 1,
                "revision": 0,
                "preferences": [entry, entry | {"on_duration_seconds": 300}],
            }
        )
    )
    store.path.chmod(0o600)
    with pytest.raises(LightPolicyError):
        store.load()


@pytest.mark.asyncio
async def test_import_observes_and_optin_never_sends(tmp_path):
    service, client, sessions, monitor, provider, row = await ready(tmp_path)
    try:
        state = monitor.snapshot()["devices"][0]["light"]
        assert state["on"] is False and not state["enabled"] and not state["can_set"]
        assert (await monitor.light.set_light(**command(sessions, monitor, row)))[
            "status"
        ] == "rejected"
        before = client.submissions
        await service.handle_admin(
            "devices_light_policy_set", policy_request(sessions, monitor, row)
        )
        assert client.submissions == before
        assert monitor.snapshot()["devices"][0]["light"]["can_set"]
        assert monitor.snapshot()["safety"] == {
            "read_only": False,
            "panic_enabled": False,
            "emergency_dispatch_enabled": False,
        }
        with pytest.raises(AdminProtocolError):
            await service.handle_admin(
                "devices_light_set", command(sessions, monitor, row) | {"duration_seconds": 10000}
            )
    finally:
        await monitor.close()


@pytest.mark.asyncio
async def test_saved_duration_no_optimism_duplicate_and_conflicting_request(tmp_path):
    service, client, sessions, monitor, provider, row = await ready(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def set_light(device, target, **params):
        calls.append((device, target, params["duration_seconds"]))
        assert params["admission"]()
        entered.set()
        await release.wait()
        return {"status": "confirmed", "code": None, "possibly_sent": True, "on": target}

    provider.set_light = set_light
    try:
        await monitor.light.configure(
            **policy_request(sessions, monitor, row, on_duration_seconds=300)
        )
        request = command(sessions, monitor, row)
        task = asyncio.create_task(monitor.light.set_light(**request))
        await entered.wait()
        assert monitor.snapshot()["devices"][0]["light"]["on"] is False
        duplicate = asyncio.create_task(monitor.light.set_light(**request))
        with pytest.raises(AdminProtocolError) as error:
            await monitor.light.set_light(**command(sessions, monitor, row, target=False))
        assert error.value.code == "busy"
        with pytest.raises(AdminProtocolError) as error:
            await monitor.light.set_light(**(request | {"target": False}))
        assert error.value.code == "operation_conflict"
        release.set()
        assert (await task)["status"] == "confirmed"
        assert await duplicate == await monitor.light.set_light(**request)
        assert calls == [(row.key, True, 300)]
        await monitor.light.set_light(**command(sessions, monitor, row, target=False))
        assert calls[-1] == (row.key, False, None)
    finally:
        release.set()
        await monitor.close()


@pytest.mark.asyncio
async def test_disable_then_reenable_never_revives_queued_send(tmp_path):
    service, client, sessions, monitor, provider, row = await ready(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    admissions = []

    async def set_light(*args, **params):
        entered.set()
        await release.wait()
        admissions.append(params["admission"]())
        return {"status": "rejected", "code": "superseded", "possibly_sent": False, "on": False}

    provider.set_light = set_light
    try:
        await monitor.light.configure(**policy_request(sessions, monitor, row))
        task = asyncio.create_task(monitor.light.set_light(**command(sessions, monitor, row)))
        await entered.wait()
        await monitor.light.configure(**policy_request(sessions, monitor, row, enabled=False))
        await monitor.light.configure(**policy_request(sessions, monitor, row))
        release.set()
        assert (await task)["status"] == "rejected" and admissions == [False]
    finally:
        release.set()
        await monitor.close()


@pytest.mark.asyncio
async def test_bad_batch_revokes_captured_lease_even_after_recovery(tmp_path):
    service, client, sessions, monitor, provider, row = await ready(tmp_path)
    try:
        await monitor.light.configure(**policy_request(sessions, monitor, row))
        lease = monitor.light._lease
        monitor.light.receive(monitor._binding, (light_source(row, on=1),))
        assert not lease.is_set()
        assert monitor.snapshot()["devices"][0]["light"]["on"] is None
        monitor.light.receive(monitor._binding, (light_source(row),))
        assert not lease.is_set() and monitor.light._lease.is_set()
    finally:
        await monitor.close()


@pytest.mark.asyncio
async def test_lost_result_blocks_until_full_readback_not_unrelated_state(tmp_path):
    service, client, sessions, monitor, provider, row = await ready(tmp_path)

    async def set_light(*args, **params):
        raise RuntimeError("lost result")

    provider.set_light = set_light
    try:
        await monitor.light.configure(**policy_request(sessions, monitor, row))
        result = await monitor.light.set_light(**command(sessions, monitor, row))
        assert result["status"] == "uncertain"
        assert monitor.snapshot()["devices"][0]["light"]["outcome_unknown"]
        monitor.light.receive(monitor._binding, (light_source(row, revision=5),))
        assert monitor.snapshot()["devices"][0]["light"]["outcome_unknown"]
        monitor.light.receive(
            monitor._binding, (light_source(row, revision=6, readback_revision=2),)
        )
        assert not monitor.snapshot()["devices"][0]["light"]["outcome_unknown"]
        assert monitor.snapshot()["devices"][0]["light"]["on"] is False
    finally:
        await monitor.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", [{"target": 1}, {"target": "on"}, {"target": None}, {"expected_revision": True}]
)
async def test_invalid_command_types_never_dispatch(tmp_path, change):
    service, client, sessions, monitor, provider, row = await ready(tmp_path)
    try:
        before = client.submissions
        with pytest.raises(AdminProtocolError):
            await monitor.light.set_light(**command(sessions, monitor, row, **change))
        assert client.submissions == before
    finally:
        await monitor.close()


@pytest.mark.asyncio
async def test_expired_illumination_unknown_but_old_battery_does_not_hide_fresh_light(tmp_path):
    service, client, sessions, monitor, provider, row = await ready(tmp_path)
    try:
        await monitor.light.configure(**policy_request(sessions, monitor, row))
        row = replace(
            row,
            observations=(
                *row.observations,
                DeviceObservation("battery_percent", 80, 0, status="stale"),
            ),
        )
        provider.options["on_snapshot"](
            records=(row,), revision=2, complete=True, observed_at=100, lights=(light_source(row),)
        )
        await asyncio.sleep(0)
        assert monitor.snapshot()["devices"][0]["light"]["can_set"]
        monitor._clock = lambda: 1000
        state = monitor.snapshot()["devices"][0]["light"]
        assert state["on"] is None and not state["can_set"]
    finally:
        await monitor.close()


@pytest.mark.asyncio
async def test_disable_revokes_before_policy_disk_write(tmp_path, monkeypatch):
    service, client, sessions, monitor, provider, row = await ready(tmp_path)
    writing, release = threading.Event(), threading.Event()
    try:
        await monitor.light.configure(**policy_request(sessions, monitor, row))
        lease = monitor.light._lease
        original = monitor.light.store.configure

        def delayed(*args, **kwargs):
            writing.set()
            assert release.wait(5)
            return original(*args, **kwargs)

        monkeypatch.setattr(monitor.light.store, "configure", delayed)
        task = asyncio.create_task(
            monitor.light.configure(**policy_request(sessions, monitor, row, enabled=False))
        )
        assert await asyncio.to_thread(writing.wait, 5)
        assert not lease.is_set()
        assert not monitor.snapshot()["devices"][0]["light"]["can_set"]
        release.set()
        await task
    finally:
        release.set()
        await monitor.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("family", ["light", "alarm"])
async def test_cancelled_ring_result_is_uncertain_and_retirement_still_completes(tmp_path, family):
    from test_device_alarm import command as alarm_command
    from test_device_alarm import policy_request as alarm_policy
    from test_device_alarm import ready as alarm_ready

    setup, configure, request, method = (
        (ready, policy_request, command, "set_light")
        if family == "light"
        else (alarm_ready, alarm_policy, alarm_command, "set_mode")
    )
    service, client, sessions, monitor, provider, row = await setup(tmp_path)

    async def cancelled(*args, **kwargs):
        raise asyncio.CancelledError

    setattr(provider, "set_light" if family == "light" else "set_alarm_mode", cancelled)
    coordinator = getattr(monitor, family)
    try:
        await coordinator.configure(**configure(sessions, monitor, row))
        params = request(sessions, monitor, row)
        result = await asyncio.wait_for(getattr(coordinator, method)(**params), 2)
        assert result["status"] == "uncertain" and result["possibly_sent"]
        assert monitor.snapshot()["devices"][0][family]["outcome_unknown"]
        assert await getattr(coordinator, method)(**params) == result
    finally:
        await monitor.close()
    assert provider.closed


@pytest.mark.asyncio
async def test_failed_command_retirement_cannot_skip_bridge_cleanup(tmp_path, monkeypatch):
    service, client, sessions, monitor, provider, row = await ready(tmp_path)
    closed = []

    async def failed():
        closed.append("alarm")
        raise asyncio.CancelledError

    async def light_closed():
        closed.append("light")

    async def bridge_closed():
        closed.append("bridge")

    monkeypatch.setattr(monitor.alarm, "close", failed)
    monkeypatch.setattr(monitor.light, "close", light_closed)
    monkeypatch.setattr(monitor.bridge, "close", bridge_closed)
    with pytest.raises(asyncio.CancelledError):
        await monitor.close()
    assert closed == ["alarm", "light", "bridge"] and provider.closed
