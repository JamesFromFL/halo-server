"""Alarm history uses current authority; all providers and transports are inert."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from test_device_alarm import alarm_source
from test_device_alarm_event_fences import two_panels
from test_device_alarm_publication import payload

from halo_server.devices.models import DeviceObservation
from halo_server.devices.runtime import DeviceBridgeRuntime


def observe(monitor, rows, *, modes=("disarmed", "disarmed"), triggered=(False, False)):
    changed, sources = [], []
    for row, mode, active in zip(rows, modes, triggered, strict=True):
        phase, signal = ("alarming", "burglar") if active else ("idle", "none")
        item = replace(
            row,
            observations=tuple(
                DeviceObservation(name, value, 100, status="current")
                for name, value in (
                    ("alarm_mode", mode),
                    ("alarm_state", phase),
                    ("alarm_signal", signal),
                )
            ),
        )
        changed.append(item)
        sources.append(alarm_source(item, mode=mode, phase=phase, signal=signal))
    keys = {row.key for row in rows}
    others = tuple(
        row for row in monitor._catalog.records(present_only=True) if row.key not in keys
    )
    monitor._receive(
        monitor._binding,
        {
            "records": (*others, *changed),
            "revision": monitor._catalog.revision + 1,
            "complete": True,
            "observed_at": 100,
            "alarms": tuple(sources),
        },
    )


async def grant(monitor, row, enabled):
    scope = monitor._binding.scope
    await monitor.alarm.configure(
        profile_id=scope.profile_id,
        account_generation=scope.generation,
        location_id=row.identity.location_key,
        expected_revision=monitor.alarm.policy.revision,
        enabled=enabled,
    )


def child(snapshot, row):
    return next(item for item in snapshot["children"] if item["device_id"] == row.key)


def actual_admission_runtime(publisher):
    """Use the real epoch/command code without starting a process or opening a socket."""
    runtime = DeviceBridgeRuntime(
        Path("/synthetic-unused-node"),
        Path("/synthetic-unused-entry"),
        build_id="synthetic-alarm-history",
        on_alarm_set=publisher._alarm_set,
    )
    runtime._boot_id = publisher.runtime.boot_id
    runtime._handshake_complete = True
    runtime._writer = object()
    runtime._accept_publication(deepcopy(publisher._last_snapshot))
    return runtime


@pytest.mark.asyncio
async def test_readback_and_other_location_history_do_not_revoke_pending_normal_command(tmp_path):
    monitor, rows = await two_panels(tmp_path)
    publisher = monitor.bridge.publisher
    entered, release = asyncio.Event(), asyncio.Event()
    calls, results, history_rejections = [], [], []
    tasks = []

    async def provider_stub(device, target, *, admission, **kwargs):
        calls.append((device, target))
        assert admission()
        entered.set()
        await release.wait()
        assert admission(), "Observed readback must not retire its own pending command"
        return {"status": "confirmed", "mode": target, "code": None, "possibly_sent": True}

    monitor._binding.provider.set_alarm_mode = provider_stub
    try:
        await grant(monitor, rows[0], True)
        await publisher._step()
        transport = publisher.runtime
        transport.snapshots.clear()
        runtime = actual_admission_runtime(publisher)
        initial_epoch = runtime._command_epochs[rows[0].key]
        request = payload(publisher, child(publisher._last_snapshot, rows[0]))

        async def collect_result(message, result):
            results.append(result)

        runtime._send_command_result = collect_result
        original_publish = transport.publish

        async def publish(snapshot, *, admission):
            result = await original_publish(snapshot, admission=admission)
            runtime._accept_publication(deepcopy(snapshot))
            # A newly arrived command carrying this history sequence can pass
            # child-level authority but must fail the parent's current sequence.
            if publisher._last_snapshot["sequence"] != snapshot["sequence"]:
                candidate = request | {"sequence": snapshot["sequence"]}
                history_rejections.append(await publisher._alarm_set(candidate, lambda: True))
            return result

        transport.publish = publish
        request_id = str(uuid4())
        await runtime._dispatch_command(
            {
                "kind": "alarm_set",
                "boot_id": runtime.boot_id,
                "request_id": request_id,
                "payload": request,
            }
        )
        tasks.append(runtime._command_tasks[request_id])
        await asyncio.wait_for(entered.wait(), 2)
        observe(monitor, rows, triggered=(False, True))
        observe(monitor, rows, modes=("home", "disarmed"), triggered=(False, True))
        assert len(publisher._alarm_events) == 2
        await publisher._step()

        assert [child(snapshot, rows[0])["alarm"]["mode"] for snapshot in transport.snapshots] == [
            "disarmed",
            "home",
            "home",
        ]
        assert all(
            child(snapshot, rows[0])["alarm"]["control_enabled"] for snapshot in transport.snapshots
        )
        assert all(
            not child(snapshot, rows[1])["alarm"]["control_enabled"]
            for snapshot in transport.snapshots
        )
        assert runtime._command_epochs[rows[0].key] == initial_epoch
        assert runtime._command_current(request, runtime.boot_id, epoch=initial_epoch)
        assert history_rejections and all(
            result == {"accepted": False, "code": "conflict"} for result in history_rejections
        )
        release.set()
        await asyncio.wait_for(tasks[0], 2)
        assert results == [{"accepted": True}]
        assert calls == [(rows[0].key, "home")]
    finally:
        release.set()
        if tasks:
            await asyncio.gather(*tasks)
        await monitor.close()


@pytest.mark.asyncio
async def test_queued_history_cannot_restore_a_revoked_owner_grant(tmp_path):
    monitor, rows = await two_panels(tmp_path)
    publisher = monitor.bridge.publisher
    try:
        await grant(monitor, rows[0], True)
        await publisher._step()
        transport = publisher.runtime
        transport.snapshots.clear()
        runtime = actual_admission_runtime(publisher)
        request = payload(publisher, child(publisher._last_snapshot, rows[0]))
        initial_epoch = runtime._command_epochs[rows[0].key]
        original_publish = transport.publish

        async def publish(snapshot, *, admission):
            result = await original_publish(snapshot, admission=admission)
            runtime._accept_publication(deepcopy(snapshot))
            return result

        transport.publish = publish
        observe(monitor, rows, triggered=(True, False))
        observe(monitor, rows)
        assert len(publisher._alarm_events) == 2
        for _fences, _expiry, observations in publisher._alarm_events:
            assert all(set(value) == {"mode", "triggered"} for value in observations.values())
        await grant(monitor, rows[0], False)
        await publisher._step()

        assert [
            child(snapshot, rows[0])["alarm"]["triggered"] for snapshot in transport.snapshots
        ] == [
            True,
            False,
            False,
        ]
        assert all(
            not child(snapshot, rows[0])["alarm"]["control_enabled"]
            for snapshot in transport.snapshots
        )
        assert runtime._command_epochs[rows[0].key] > initial_epoch
        assert not runtime._command_current(request, runtime.boot_id, epoch=initial_epoch)
        current = payload(publisher, child(publisher._last_snapshot, rows[0]))
        assert await publisher._alarm_set(current, lambda: True) == {
            "accepted": False,
            "code": "policy-denied",
        }
    finally:
        await monitor.close()
