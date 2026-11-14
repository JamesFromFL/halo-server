"""Normal alarm controls: explicit grants, scope fences, ownership and outcomes."""

import asyncio
import json
import threading
from dataclasses import replace
from uuid import uuid4

import pytest
from test_device_monitor import fixture, preference, record

from halo_server.account_scope import AccountScope
from halo_server.api.admin import AdminProtocolError
from halo_server.devices.alarm_policy import (
    AlarmPolicy,
    AlarmPolicyConflict,
    AlarmPolicyError,
    AlarmPolicyStore,
)
from halo_server.devices.models import DeviceObservation


def panel(sessions):
    return replace(
        record(sessions, identifier="panel", name="Ring Alarm"),
        kind="security-panel",
        capabilities=("mode",),
        capability_evidence=("ring-hub-device-schema",),
        observations=tuple(
            DeviceObservation(name, value, 100)
            for name, value in (
                ("alarm_mode", "disarmed"),
                ("alarm_state", "idle"),
                ("alarm_signal", "none"),
            )
        ),
    )


def alarm_source(row, **changes):
    return {
        "device_id": row.key,
        "location_id": row.identity.location_key,
        "epoch": 1,
        "revision": 4,
        "mode": "disarmed",
        "phase": "idle",
        "signal": "none",
        "authorization": "allowed",
        "can_set_mode": True,
        "reason": None,
        "pending": False,
        "outcome_unknown": False,
        "requested_mode": None,
        "allowed_targets": ["disarmed", "home", "away"],
        "readback_revision": 1,
        **changes,
    }


async def ready(tmp_path):
    service, client, sessions, monitor, providers = fixture(tmp_path)
    monitor.bridge.publisher.start = monitor.bridge.publisher.notify
    await monitor.set_enabled(**preference(sessions))
    row = panel(sessions)
    providers[0].options["on_snapshot"](
        records=(row,),
        revision=1,
        complete=True,
        observed_at=100,
        alarms=(alarm_source(row),),
    )
    await asyncio.sleep(0)
    return service, client, sessions, monitor, providers[0], row


def policy_request(sessions, monitor, row, **changes):
    return {
        "profile_id": sessions.active_scope.profile_id,
        "account_generation": sessions.active_scope.generation,
        "location_id": row.identity.location_key,
        "expected_revision": monitor.alarm.policy.revision,
        "enabled": True,
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
        "expected_policy_revision": monitor.alarm.policy.revision,
        "target": "away",
        "operation_id": str(uuid4()),
        **changes,
    }


def test_policy_default_private_cas_scope_and_deletion(tmp_path):
    store = AlarmPolicyStore(tmp_path / "alarm.json")
    assert store.load() == AlarmPolicy()
    assert not list(tmp_path.iterdir())
    profile = str(uuid4())
    first = store.configure(profile, "a" * 64, True, expected_revision=0)
    assert first.allows(profile, "a" * 64)
    assert not first.allows(str(uuid4()), "a" * 64)
    assert store.path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(AlarmPolicyConflict):
        store.configure(profile, "a" * 64, False, expected_revision=0)
    assert not store.configure(profile, "a" * 64, False, expected_revision=1).grants
    store.path.unlink()
    assert not store.load().grants


@pytest.mark.parametrize(
    "payload",
    [
        {"version": 1, "revision": 0, "grants": [], "panic": True},
        {"version": True, "revision": 0, "grants": []},
        {
            "version": 1,
            "revision": 0,
            "grants": [{"profile_id": str(uuid4()), "location_id": "raw"}],
        },
    ],
)
def test_invalid_policy_cannot_authorize(tmp_path, payload):
    store = AlarmPolicyStore(tmp_path / "alarm.json")
    store.path.write_text(json.dumps(payload))
    store.path.chmod(0o600)
    with pytest.raises(AlarmPolicyError):
        store.load()


@pytest.mark.asyncio
async def test_observation_does_not_enable_controls_and_optin_does_not_send(tmp_path):
    service, client, sessions, monitor, provider, row = await ready(tmp_path)
    try:
        state = monitor.snapshot()["devices"][0]["alarm"]
        assert state["mode"] == "disarmed" and not state["enabled"]
        assert not state["can_set_mode"]
        assert (await monitor.alarm.set_mode(**command(sessions, monitor, row)))[
            "status"
        ] == "rejected"
        submissions = client.submissions
        await service.handle_admin(
            "devices_alarm_policy_set", policy_request(sessions, monitor, row)
        )
        assert client.submissions == submissions
        state = monitor.snapshot()
        assert state["devices"][0]["alarm"]["can_set_mode"]
        assert state["safety"] == {
            "read_only": False,
            "panic_enabled": False,
            "emergency_dispatch_enabled": False,
        }
    finally:
        await monitor.close()


@pytest.mark.asyncio
async def test_confirmation_duplicate_operation_and_no_optimistic_state(tmp_path):
    service, client, sessions, monitor, provider, row = await ready(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def set_mode(*args, **kwargs):
        calls.append(args)
        assert kwargs["admission"]()
        entered.set()
        await release.wait()
        return {"status": "confirmed", "code": None, "possibly_sent": True, "mode": "away"}

    provider.set_alarm_mode = set_mode
    try:
        await monitor.alarm.configure(**policy_request(sessions, monitor, row))
        params = command(sessions, monitor, row)
        task = asyncio.create_task(service.handle_admin("devices_alarm_mode_set", params))
        await entered.wait()
        state = monitor.snapshot()["devices"][0]["alarm"]
        assert state["pending"] and state["requested_mode"] == "away"
        assert state["mode"] == "disarmed" and not state["can_set_mode"]
        duplicate = asyncio.create_task(monitor.alarm.set_mode(**params))
        with pytest.raises(AdminProtocolError) as error:
            await monitor.alarm.set_mode(**command(sessions, monitor, row, target="home"))
        assert error.value.code == "busy"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        assert (await duplicate)["status"] == "confirmed"
        assert await monitor.alarm.set_mode(**params) == await duplicate
        assert len(calls) == 1
        with pytest.raises(AdminProtocolError):
            await monitor.alarm.set_mode(**(params | {"target": "home"}))
        assert monitor.snapshot()["devices"][0]["alarm"]["mode"] == "disarmed"
    finally:
        release.set()
        await monitor.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"target": "night"},
        {"target": "panic"},
        {"target": "bypass"},
        {"target": []},
        {"expected_revision": True},
        {"expected_epoch": 2},
        {"expected_policy_revision": 0},
        {"provider_generation": 2},
        {"profile_id": str(uuid4())},
        {"bypass": []},
    ],
)
async def test_commands_fail_before_provider_on_bad_or_stale_parameters(tmp_path, change):
    service, client, sessions, monitor, provider, row = await ready(tmp_path)
    provider.set_alarm_mode = lambda *_a, **_k: pytest.fail("must not dispatch")
    try:
        await monitor.alarm.configure(**policy_request(sessions, monitor, row))
        with pytest.raises(AdminProtocolError):
            await service.handle_admin(
                "devices_alarm_mode_set", command(sessions, monitor, row, **change)
            )
    finally:
        await monitor.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source",
    [
        {"authorization": "unknown", "can_set_mode": False, "reason": "authorization-unknown"},
        {"authorization": "denied", "can_set_mode": False, "reason": "authorization-denied"},
        {"outcome_unknown": True, "can_set_mode": False, "reason": "outcome-unknown"},
    ],
)
async def test_optin_does_not_override_source_guard(tmp_path, source):
    service, client, sessions, monitor, provider, row = await ready(tmp_path)
    try:
        await monitor.alarm.configure(**policy_request(sessions, monitor, row))
        monitor.alarm.receive(monitor._binding, (alarm_source(row, **source),))
        result = await monitor.alarm.set_mode(**command(sessions, monitor, row))
        assert result["status"] == "rejected" and result["code"] == source["reason"]
    finally:
        await monitor.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["disable", "account", "expire"])
async def test_revocation_and_expiry_fence_commands(tmp_path, action):
    service, client, sessions, monitor, provider, row = await ready(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()

    async def set_mode(*args, **kwargs):
        entered.set()
        await release.wait()
        return {
            "status": "rejected" if not kwargs["admission"]() else "confirmed",
            "code": "superseded",
            "possibly_sent": False,
            "mode": None,
        }

    provider.set_alarm_mode = set_mode
    try:
        await monitor.alarm.configure(**policy_request(sessions, monitor, row))
        if action == "expire":
            monitor._clock = lambda: 1000
            result = await monitor.alarm.set_mode(**command(sessions, monitor, row))
            assert result["status"] == "rejected" and not entered.is_set()
            assert monitor.snapshot()["devices"][0]["alarm"]["mode"] is None
        else:
            task = asyncio.create_task(monitor.alarm.set_mode(**command(sessions, monitor, row)))
            await entered.wait()
            if action == "disable":
                await monitor.alarm.configure(
                    **policy_request(sessions, monitor, row, enabled=False)
                )
            else:
                sessions.active_scope = AccountScope(str(uuid4()), 2)
                monitor.invalidate()
            release.set()
            assert (await task)["status"] != "confirmed"
    finally:
        release.set()
        await monitor.close()


@pytest.mark.asyncio
async def test_alarm_policy_damage_does_not_stop_sensor_observation(tmp_path):
    service, client, sessions, monitor, providers = fixture(tmp_path)
    monitor.bridge.publisher.start = monitor.bridge.publisher.notify
    monitor.alarm.store.path.parent.mkdir(parents=True, mode=0o700)
    monitor.alarm.store.path.write_text("broken")
    monitor.alarm.store.path.chmod(0o600)
    try:
        await monitor.set_enabled(**preference(sessions))
        providers[0].publish((record(sessions),))
        await asyncio.sleep(0)
        assert monitor.snapshot()["devices"][0]["observed"]["contact_open"] is True
        assert monitor.alarm._error and not monitor.alarm._lease.is_set()
    finally:
        await monitor.close()


@pytest.mark.asyncio
async def test_lost_result_requires_new_full_panel_readback_not_any_revision(tmp_path):
    service, client, sessions, monitor, provider, row = await ready(tmp_path)

    async def failed(*args, **kwargs):
        raise RuntimeError("result lost")

    provider.set_alarm_mode = failed
    try:
        await monitor.alarm.configure(**policy_request(sessions, monitor, row))
        result = await monitor.alarm.set_mode(**command(sessions, monitor, row))
        assert result["status"] == "uncertain"
        monitor.alarm.receive(monitor._binding, (alarm_source(row, revision=5),))
        state = monitor.snapshot()["devices"][0]["alarm"]
        assert state["outcome_unknown"] and not state["can_set_mode"]
        assert state["requested_mode"] == "away"
        monitor.alarm.receive(
            monitor._binding, (alarm_source(row, revision=6, readback_revision=2),)
        )
        state = monitor.snapshot()["devices"][0]["alarm"]
        assert not state["outcome_unknown"] and state["can_set_mode"]
    finally:
        await monitor.close()


@pytest.mark.asyncio
async def test_malformed_batch_revokes_commands_until_valid_source_resync(tmp_path):
    service, client, sessions, monitor, provider, row = await ready(tmp_path)
    try:
        await monitor.alarm.configure(**policy_request(sessions, monitor, row))
        monitor._receive(
            monitor._binding,
            {
                "records": "bad",
                "revision": 2,
                "complete": True,
                "observed_at": 100,
                "alarms": (alarm_source(row),),
            },
        )
        assert not monitor.snapshot()["devices"][0]["alarm"]["can_set_mode"]
        monitor._receive(
            monitor._binding,
            {
                "records": (row,),
                "revision": 3,
                "complete": True,
                "observed_at": 100,
                "alarms": (alarm_source(row),),
            },
        )
        assert monitor.snapshot()["devices"][0]["alarm"]["can_set_mode"]
        monitor.alarm.receive(monitor._binding, (alarm_source(row, mode=[]),))
        assert not monitor.snapshot()["devices"][0]["alarm"]["can_set_mode"]
    finally:
        await monitor.close()


@pytest.mark.asyncio
async def test_target_specific_guard_rejects_arming_but_preserves_disarm(tmp_path):
    service, client, sessions, monitor, provider, row = await ready(tmp_path)
    try:
        await monitor.alarm.configure(**policy_request(sessions, monitor, row))
        monitor.alarm.receive(monitor._binding, (alarm_source(row, allowed_targets=["disarmed"]),))
        state = monitor.snapshot()["devices"][0]["alarm"]
        assert state["can_set_mode"] and state["allowed_targets"] == ["disarmed"]
        assert (await monitor.alarm.set_mode(**command(sessions, monitor, row)))[
            "code"
        ] == "target-unavailable"
    finally:
        await monitor.close()


@pytest.mark.asyncio
async def test_monitor_disable_revokes_before_policy_write_completes(tmp_path):
    service, client, sessions, monitor, provider, row = await ready(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    saving, finish_save = threading.Event(), threading.Event()
    original = monitor.store.set_enabled

    async def set_mode(*args, **kwargs):
        entered.set()
        await release.wait()
        return {
            "status": "confirmed" if kwargs["admission"]() else "rejected",
            "code": "superseded",
            "possibly_sent": False,
            "mode": None,
        }

    def save(*args, **kwargs):
        saving.set()
        assert finish_save.wait(5)
        return original(*args, **kwargs)

    provider.set_alarm_mode = set_mode
    try:
        await monitor.alarm.configure(**policy_request(sessions, monitor, row))
        task = asyncio.create_task(monitor.alarm.set_mode(**command(sessions, monitor, row)))
        await entered.wait()
        monitor.store.set_enabled = save
        disabling = asyncio.create_task(
            monitor.set_enabled(**preference(sessions, enabled=False, revision=1))
        )
        assert await asyncio.to_thread(saving.wait, 5)
        release.set()
        assert (await task)["status"] != "confirmed"
        assert not monitor._binding.active.is_set()
        finish_save.set()
        await disabling
    finally:
        release.set()
        finish_save.set()
        await monitor.close()


@pytest.mark.asyncio
async def test_malformed_then_valid_batch_cannot_revive_queued_command_lease(tmp_path):
    service, client, sessions, monitor, provider, row = await ready(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()

    async def set_mode(*args, **kwargs):
        entered.set()
        await release.wait()
        assert not kwargs["admission"]()
        return {"status": "rejected", "code": "superseded", "possibly_sent": False, "mode": None}

    provider.set_alarm_mode = set_mode
    try:
        await monitor.alarm.configure(**policy_request(sessions, monitor, row))
        task = asyncio.create_task(monitor.alarm.set_mode(**command(sessions, monitor, row)))
        await entered.wait()
        monitor.alarm.receive(monitor._binding, (alarm_source(row, mode=[]),))
        monitor.alarm.receive(monitor._binding, (alarm_source(row),))
        release.set()
        assert (await task)["status"] == "rejected"
        assert monitor.snapshot()["devices"][0]["alarm"]["can_set_mode"]
    finally:
        release.set()
        await monitor.close()


@pytest.mark.asyncio
async def test_completed_stale_receipts_do_not_exhaust_long_running_session(tmp_path):
    service, client, sessions, monitor, provider, row = await ready(tmp_path)
    calls = []

    async def set_mode(*args, **kwargs):
        calls.append(args)
        return {"status": "confirmed", "code": None, "mode": "away", "possibly_sent": True}

    provider.set_alarm_mode = set_mode
    try:
        await monitor.alarm.configure(**policy_request(sessions, monitor, row))
        first = command(sessions, monitor, row)
        await monitor.alarm.set_mode(**first)
        for _ in range(255):
            await monitor.alarm.set_mode(**command(sessions, monitor, row))
        with pytest.raises(AdminProtocolError) as error:
            await monitor.alarm.set_mode(**command(sessions, monitor, row))
        assert error.value.code == "busy"
        monitor.alarm.receive(monitor._binding, (alarm_source(row, revision=5),))
        await monitor.alarm.set_mode(**command(sessions, monitor, row, expected_revision=5))
        assert len(monitor.alarm._operations) == 1
        with pytest.raises(AdminProtocolError):
            await monitor.alarm.set_mode(**first)
        assert len(calls) == 257
    finally:
        await monitor.close()
