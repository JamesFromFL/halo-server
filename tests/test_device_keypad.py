"""Offline keypad policy, honest observations, and lock-versus-send races."""

import asyncio
import json
import threading
from dataclasses import replace
from uuid import uuid4

import pytest
from test_device_monitor import fixture, preference, record

from halo_server.account_scope import AccountScope
from halo_server.api.admin import AdminProtocolError
from halo_server.devices.keypad import KeypadCoordinator
from halo_server.devices.keypad_policy import (
    KeypadPolicy,
    KeypadPolicyConflict,
    KeypadPolicyError,
    KeypadPolicyStore,
)
from halo_server.devices.models import DeviceObservation


def keypad(sessions, brightness=42, **changes):
    return replace(
        record(sessions, identifier="keypad", name="Hall keypad"),
        kind="keypad",
        model="security-keypad",
        capabilities=("keypad-brightness",),
        capability_evidence=("ring-hub-device-schema",),
        observations=(DeviceObservation("keypad_brightness", brightness, 100),),
        **changes,
    )


def source(row, **changes):
    return {
        "device_id": row.key,
        "location_id": row.identity.location_key,
        "epoch": 1,
        "revision": 4,
        "readback_revision": 1,
        "brightness": 42,
        "authorization": "allowed",
        "can_set": True,
        "reason": None,
        "pending": False,
        "outcome_unknown": False,
        "requested_brightness": None,
        **changes,
    }


async def ready(tmp_path):
    service, client, sessions, monitor, providers = fixture(tmp_path)
    monitor.bridge.publisher.start = monitor.bridge.publisher.notify
    await monitor.set_enabled(**preference(sessions))
    row = keypad(sessions)
    providers[0].options["on_snapshot"](
        records=(row,), revision=1, complete=True, observed_at=100, keypads=(source(row),)
    )
    await asyncio.sleep(0)
    return service, client, sessions, monitor, providers[0], row


def policy_request(sessions, monitor, row, **changes):
    return {
        "profile_id": sessions.active_scope.profile_id,
        "account_generation": sessions.active_scope.generation,
        "device_id": row.key,
        "expected_revision": monitor.keypad.policy.revision,
        "enabled": True,
        "locked": False,
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
        "expected_policy_revision": monitor.keypad.policy.revision,
        "target": 25,
        "operation_id": str(uuid4()),
        **changes,
    }


def lock_request(sessions, monitor, row, **changes):
    return {
        "profile_id": sessions.active_scope.profile_id,
        "account_generation": sessions.active_scope.generation,
        "device_id": row.key,
        "expected_policy_revision": monitor.keypad.policy.revision,
        "locked": True,
        **changes,
    }


def view(monitor):
    return monitor.snapshot()["devices"][0]["keypad"]


def test_policy_defaults_persistence_private_permissions_and_cas(tmp_path):
    store = KeypadPolicyStore(tmp_path / "keypad.json")
    profile, device = str(uuid4()), "a" * 64
    assert store.load() == KeypadPolicy() and not store.path.exists()
    default = store.load().preference(profile, device)
    assert not default.enabled and default.locked
    saved = store.configure(profile, device, True, True, expected_revision=0)
    assert saved.preference(profile, device).enabled and saved.preference(profile, device).locked
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert KeypadPolicyStore(store.path).load() == saved
    with pytest.raises(KeypadPolicyConflict):
        store.configure(profile, device, True, False, expected_revision=0)
    assert not saved.allows(str(uuid4()), device)
    saved = store.configure(profile, device, True, False, expected_revision=1)
    assert not KeypadPolicyStore(store.path).load().preference(profile, device).locked
    store.path.unlink()
    assert not store.load().allows(profile, device)
    assert store.load().preference(profile, device).locked


@pytest.mark.parametrize("value", [None, 0, 1, "true", 1.0])
def test_nonboolean_lock_never_persists(tmp_path, value):
    store = KeypadPolicyStore(tmp_path / "keypad.json")
    with pytest.raises(KeypadPolicyError):
        store.configure(str(uuid4()), "a" * 64, True, value, expected_revision=0)
    assert not store.path.exists()


def test_duplicate_and_unexpected_policy_fields_rejected(tmp_path):
    store = KeypadPolicyStore(tmp_path / "keypad.json")
    entry = {"profile_id": str(uuid4()), "device_id": "a" * 64, "enabled": True, "locked": False}
    for entries in ([entry, entry], [entry | {"brightness": 50}]):
        store.path.write_text(json.dumps({"version": 1, "revision": 0, "preferences": entries}))
        store.path.chmod(0o600)
        with pytest.raises(KeypadPolicyError):
            store.load()


@pytest.mark.parametrize("value", [-1, 101, True, 4.0, "4"])
def test_brightness_observation_has_strict_range(value):
    with pytest.raises(ValueError):
        DeviceObservation("keypad_brightness", value, 100)


@pytest.mark.asyncio
async def test_owner_optin_lock_and_restart_never_send_ring_commands(tmp_path):
    service, client, sessions, monitor, provider, row = await ready(tmp_path)
    try:
        initial = view(monitor)
        assert initial["brightness"] == 42 and initial["on_brightness"] == 42
        assert initial["locked"] and not initial["enabled"] and not initial["can_set_lock"]
        before = client.submissions
        with pytest.raises(AdminProtocolError, match="disabled"):
            await service.handle_admin(
                "devices_keypad_lock_set", lock_request(sessions, monitor, row)
            )
        saved = await service.handle_admin(
            "devices_keypad_policy_set", policy_request(sessions, monitor, row, locked=True)
        )
        assert saved == {"enabled": True, "locked": True, "policy_revision": 1}
        state = view(monitor)
        assert (
            state["can_set_lock"]
            and not state["can_set"]
            and state["reason"] == "brightness-locked"
        )
        for target in (0, 100, 42):
            result = await service.handle_admin(
                "devices_keypad_brightness_set", command(sessions, monitor, row, target=target)
            )
            assert result["status"] == "rejected" and not result["possibly_sent"]
        assert client.submissions == before
        restarted = KeypadCoordinator(monitor)
        await restarted.load()
        assert restarted.policy.preference(sessions.active_scope.profile_id, row.key).locked
        assert restarted.policy.preference(sessions.active_scope.profile_id, row.key).enabled
        unlocked = await service.handle_admin(
            "devices_keypad_lock_set", lock_request(sessions, monitor, row, locked=False)
        )
        assert unlocked == {"enabled": True, "locked": False, "policy_revision": 2}
        assert view(monitor)["can_set"] and client.submissions == before
    finally:
        await monitor.close()


@pytest.mark.asyncio
async def test_no_optimism_uuid_idempotence_busy_and_operation_conflict(tmp_path):
    service, client, sessions, monitor, provider, row = await ready(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def set_brightness(device, target, **params):
        calls.append((device, target))
        assert params["admission"]()
        entered.set()
        await release.wait()
        return {"status": "confirmed", "code": None, "possibly_sent": True, "brightness": target}

    provider.set_keypad_brightness = set_brightness
    try:
        await monitor.keypad.configure(**policy_request(sessions, monitor, row))
        request = command(sessions, monitor, row)
        task = asyncio.create_task(monitor.keypad.set_brightness(**request))
        await entered.wait()
        assert view(monitor)["brightness"] == 42 and view(monitor)["requested_brightness"] == 25
        duplicate = asyncio.create_task(monitor.keypad.set_brightness(**request))
        with pytest.raises(AdminProtocolError, match="pending"):
            await monitor.keypad.set_brightness(**command(sessions, monitor, row, target=0))
        with pytest.raises(AdminProtocolError) as error:
            await monitor.keypad.set_brightness(**(request | {"target": 0}))
        assert error.value.code == "operation_conflict"
        release.set()
        result = await task
        assert result["brightness"] == 25
        assert await duplicate == await monitor.keypad.set_brightness(**request) == result
        assert calls == [(row.key, 25)] and view(monitor)["brightness"] == 42
    finally:
        release.set()
        await monitor.close()


@pytest.mark.asyncio
async def test_lock_persists_without_waiting_for_dimmer_and_old_lease_never_revives(tmp_path):
    service, client, sessions, monitor, provider, row = await ready(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    admissions = []

    async def set_brightness(device, target, **params):
        entered.set()
        await release.wait()
        admissions.append(params["admission"]())
        return {
            "status": "rejected",
            "code": "superseded",
            "possibly_sent": False,
            "brightness": 42,
        }

    provider.set_keypad_brightness = set_brightness
    try:
        await monitor.keypad.configure(**policy_request(sessions, monitor, row))
        task = asyncio.create_task(monitor.keypad.set_brightness(**command(sessions, monitor, row)))
        await entered.wait()
        result = await asyncio.wait_for(
            monitor.keypad.set_lock(**lock_request(sessions, monitor, row)), 2
        )
        assert result["locked"] and not task.done()
        await monitor.keypad.set_lock(**lock_request(sessions, monitor, row, locked=False))
        release.set()
        assert (await task)["status"] == "rejected" and admissions == [False]
        assert not view(monitor)["locked"]
    finally:
        release.set()
        await monitor.close()


@pytest.mark.asyncio
async def test_lock_revokes_before_disk_write_but_keeps_own_grant_stable(tmp_path, monkeypatch):
    service, client, sessions, monitor, provider, row = await ready(tmp_path)
    writing, release = threading.Event(), threading.Event()
    try:
        await monitor.keypad.configure(**policy_request(sessions, monitor, row))
        captured = monitor.keypad._lease
        original = monitor.keypad.store.configure

        def delayed(*args, **kwargs):
            writing.set()
            assert release.wait(5)
            return original(*args, **kwargs)

        monkeypatch.setattr(monitor.keypad.store, "configure", delayed)
        task = asyncio.create_task(monitor.keypad.set_lock(**lock_request(sessions, monitor, row)))
        assert await asyncio.to_thread(writing.wait, 5)
        assert not captured.is_set() and not view(monitor)["can_set"]
        assert view(monitor)["enabled"] and view(monitor)["can_set_lock"]
        assert not view(monitor)["locked"]  # no optimistic acknowledgement
        release.set()
        await task
        assert view(monitor)["locked"] and not captured.is_set()
    finally:
        release.set()
        await monitor.close()


@pytest.mark.asyncio
async def test_policy_write_failure_fails_closed_without_false_lock_ack(tmp_path, monkeypatch):
    service, client, sessions, monitor, provider, row = await ready(tmp_path)
    try:
        await monitor.keypad.configure(**policy_request(sessions, monitor, row))
        before = client.submissions

        def failed(*args, **kwargs):
            raise KeypadPolicyError("failed")

        monkeypatch.setattr(monitor.keypad.store, "configure", failed)
        with pytest.raises(AdminProtocolError) as error:
            await monitor.keypad.set_lock(**lock_request(sessions, monitor, row))
        assert error.value.code == "keypad_policy_unavailable"
        assert not view(monitor)["can_set"] and not view(monitor)["can_set_lock"]
        assert client.submissions == before
    finally:
        await monitor.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("exception", [RuntimeError, asyncio.CancelledError])
async def test_unknown_result_is_not_replayed_and_requires_fresh_readback(tmp_path, exception):
    service, client, sessions, monitor, provider, row = await ready(tmp_path)
    calls = []

    async def failed(*args, **params):
        calls.append(True)
        raise exception()

    provider.set_keypad_brightness = failed
    try:
        await monitor.keypad.configure(**policy_request(sessions, monitor, row))
        request = command(sessions, monitor, row)
        result = await asyncio.wait_for(monitor.keypad.set_brightness(**request), 2)
        assert result["status"] == "uncertain" and result["possibly_sent"]
        assert view(monitor)["outcome_unknown"]
        assert await monitor.keypad.set_brightness(**request) == result and calls == [True]
        monitor.keypad.receive(monitor._binding, (source(row, revision=5),))
        assert view(monitor)["outcome_unknown"]
        monitor.keypad.receive(monitor._binding, (source(row, revision=6, readback_revision=2),))
        assert not view(monitor)["outcome_unknown"] and view(monitor)["brightness"] == 42
    finally:
        await monitor.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"target": True},
        {"target": -1},
        {"target": 101},
        {"target": 2.0},
        {"expected_revision": True},
        {"operation_id": "x"},
    ],
)
async def test_invalid_brightness_commands_never_dispatch(tmp_path, changes):
    service, client, sessions, monitor, provider, row = await ready(tmp_path)
    try:
        before = client.submissions
        with pytest.raises(AdminProtocolError):
            await monitor.keypad.set_brightness(**command(sessions, monitor, row, **changes))
        assert client.submissions == before
    finally:
        await monitor.close()


@pytest.mark.asyncio
async def test_stale_readings_are_unknown_and_local_lock_remains_available(tmp_path):
    service, client, sessions, monitor, provider, row = await ready(tmp_path)
    try:
        await monitor.keypad.configure(**policy_request(sessions, monitor, row))
        monitor._clock = lambda: 1000
        assert view(monitor)["brightness"] is None and not view(monitor)["can_set"]
        assert view(monitor)["can_set_lock"]
        before = client.submissions
        assert (await monitor.keypad.set_lock(**lock_request(sessions, monitor, row)))["locked"]
        assert client.submissions == before
    finally:
        await monitor.close()


@pytest.mark.asyncio
async def test_nonzero_on_target_is_observed_history_and_reset_is_explicit_fallback(tmp_path):
    service, client, sessions, monitor, provider, row = await ready(tmp_path)
    try:
        off = keypad(sessions, brightness=0)
        provider.options["on_snapshot"](
            records=(off,),
            revision=2,
            complete=True,
            observed_at=100,
            keypads=(source(row, brightness=0, revision=5),),
        )
        await asyncio.sleep(0)
        assert view(monitor)["brightness"] == 0 and view(monitor)["on_brightness"] == 42
        restarted = KeypadCoordinator(monitor)
        await restarted.load()
        restarted.receive(monitor._binding, (source(row, brightness=0),))
        assert restarted.view(restarted._row(row.key))["on_brightness"] == 100
        assert restarted.view(restarted._row(row.key))["brightness"] == 0
    finally:
        await monitor.close()


@pytest.mark.asyncio
async def test_bad_batches_and_account_changes_fence_brightness_and_lock(tmp_path):
    service, client, sessions, monitor, provider, row = await ready(tmp_path)
    try:
        await monitor.keypad.configure(**policy_request(sessions, monitor, row))
        lease = monitor.keypad._lease
        monitor.keypad.receive(monitor._binding, (source(row, brightness=True),))
        assert not lease.is_set() and view(monitor)["brightness"] is None
        monitor.keypad.receive(monitor._binding, (source(row),))
        assert not lease.is_set() and monitor.keypad._lease.is_set()
        stale_lock = lock_request(sessions, monitor, row)
        sessions.active_scope = AccountScope(str(uuid4()), 2)
        with pytest.raises(AdminProtocolError) as error:
            await monitor.keypad.set_lock(**stale_lock)
        assert error.value.code == "account_changed"
    finally:
        await monitor.close()


@pytest.mark.asyncio
async def test_unknown_model_or_policy_types_cannot_enable_controls(tmp_path):
    service, client, sessions, monitor, provider, row = await ready(tmp_path)
    try:
        for enabled in (None, 1, "true"):
            with pytest.raises(AdminProtocolError):
                await monitor.keypad.configure(
                    **policy_request(sessions, monitor, row, enabled=enabled)
                )
        unknown = replace(row, model="security-keypad-unknown")
        provider.options["on_snapshot"](
            records=(unknown,), revision=2, complete=True, observed_at=100, keypads=(source(row),)
        )
        await asyncio.sleep(0)
        with pytest.raises(AdminProtocolError) as error:
            await monitor.keypad.configure(**policy_request(sessions, monitor, row))
        assert error.value.code == "unsupported"
        assert not monitor.keypad.policy.preferences
    finally:
        await monitor.close()


@pytest.mark.asyncio
async def test_old_grant_cannot_control_reclassified_device_or_revoked_ring_role(tmp_path):
    service, client, sessions, monitor, provider, row = await ready(tmp_path)
    try:
        await monitor.keypad.configure(**policy_request(sessions, monitor, row))
        before = client.submissions
        monitor.keypad.receive(
            monitor._binding, (source(row, authorization="denied", can_set=False),)
        )
        result = await monitor.keypad.set_brightness(**command(sessions, monitor, row))
        assert result["status"] == "rejected" and not result["possibly_sent"]
        assert view(monitor)["can_set_lock"]
        unknown = replace(row, model="security-keypad-unknown")
        provider.options["on_snapshot"](
            records=(unknown,), revision=2, complete=True, observed_at=100, keypads=(source(row),)
        )
        await asyncio.sleep(0)
        assert not view(monitor)["can_set"] and not view(monitor)["can_set_lock"]
        result = await monitor.keypad.set_brightness(**command(sessions, monitor, row))
        assert result["code"] == "unsupported-keypad" and not result["possibly_sent"]
        assert client.submissions == before
    finally:
        await monitor.close()


@pytest.mark.asyncio
async def test_lock_cas_and_parent_admission_reject_without_policy_write(tmp_path):
    service, client, sessions, monitor, provider, row = await ready(tmp_path)
    try:
        await monitor.keypad.configure(**policy_request(sessions, monitor, row))
        original = monitor.keypad.store.path.read_bytes()
        for changes in ({"expected_policy_revision": 0}, {"admission": lambda: False}):
            with pytest.raises(AdminProtocolError) as error:
                await monitor.keypad.set_lock(**lock_request(sessions, monitor, row, **changes))
            assert error.value.code == "configuration_conflict"
            assert monitor.keypad.store.path.read_bytes() == original
            assert not view(monitor)["locked"]
    finally:
        await monitor.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field",
    ["provider_generation", "expected_epoch", "expected_revision", "expected_policy_revision"],
)
async def test_brightness_rejects_stale_authority_without_provider_call(tmp_path, field):
    service, client, sessions, monitor, provider, row = await ready(tmp_path)
    try:
        await monitor.keypad.configure(**policy_request(sessions, monitor, row))
        before = client.submissions
        with pytest.raises(AdminProtocolError) as error:
            await monitor.keypad.set_brightness(**command(sessions, monitor, row, **{field: 999}))
        assert error.value.code == "configuration_conflict" and client.submissions == before
    finally:
        await monitor.close()
