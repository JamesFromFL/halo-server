"""Synthetic publication races: no real Ring, HomeKit, alarm, or device actions."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_device_alarm import alarm_source
from test_device_alarm_publication import alarm_ready, delivered_alarm, observe_alarm

from halo_server.devices.models import DeviceObservation


def observe_panels(monitor, rows, *, states, epochs=(1, 1), offline=()):
    changed = []
    alarms = []
    for index, row in enumerate(rows):
        active = states[index]
        phase, signal = ("alarming", "burglar") if active else ("idle", "none")
        item = replace(
            row,
            source_health="offline" if index in offline else "online",
            observations=tuple(
                DeviceObservation(name, value, 100, status="current")
                for name, value in (
                    ("alarm_mode", "disarmed"),
                    ("alarm_state", phase),
                    ("alarm_signal", signal),
                )
            ),
        )
        changed.append(item)
        alarms.append(alarm_source(item, phase=phase, signal=signal, epoch=epochs[index]))
    row_keys = {row.key for row in rows}
    others = tuple(
        record
        for record in monitor._catalog.records(present_only=True)
        if record.key not in row_keys
    )
    monitor._receive(
        monitor._binding,
        {
            "records": (*others, *changed),
            "revision": monitor._catalog.revision + 1,
            "complete": True,
            "observed_at": 100,
            "alarms": tuple(alarms),
        },
    )


async def two_panels(tmp_path):
    _service, _sessions, monitor, provider, first = await alarm_ready(tmp_path)
    second = replace(
        first,
        identity=replace(first.identity, location_id="second-home", device_id="second-panel"),
        name="Second panel",
    )

    async def forbidden(*args, **kwargs):
        pytest.fail("Observation delivery must never invoke a Ring alarm command")

    provider.set_alarm_mode = forbidden
    observe_panels(monitor, (first, second), states=(False, False))
    await asyncio.sleep(0)
    if monitor.bridge._task:
        await monitor.bridge._task
    await monitor.bridge.publisher._step()
    monitor.bridge.publisher.runtime.snapshots.clear()
    return monitor, (first, second)


def alarm_pairs(runtime, rows):
    result = []
    for snapshot in runtime.snapshots:
        alarms = {child["device_id"]: child["alarm"] for child in snapshot["children"]}
        result.append(tuple(alarms[row.key]["triggered"] for row in rows))
    return result


@pytest.mark.asyncio
async def test_two_locations_keep_each_observed_alarm_in_chronological_order(tmp_path):
    monitor, rows = await two_panels(tmp_path)
    publisher = monitor.bridge.publisher
    try:
        expected = [(True, False), (True, True), (False, True), (False, False)]
        for states in expected:
            observe_panels(monitor, rows, states=states)
        await publisher._step()
        assert alarm_pairs(publisher.runtime, rows) == [*expected, (False, False)]
        for snapshot in publisher.runtime.snapshots[:-1]:
            assert all(
                not child["alarm"]["control_enabled"]
                for child in snapshot["children"]
                if child["alarm"] is not None
            )
    finally:
        await monitor.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("retirement", ["offline", "epoch"])
async def test_retiring_one_location_preserves_other_locations_pending_alarm(tmp_path, retirement):
    monitor, rows = await two_panels(tmp_path)
    publisher = monitor.bridge.publisher
    try:
        observe_panels(monitor, rows, states=(True, False))
        observe_panels(monitor, rows, states=(False, False))
        observe_panels(
            monitor,
            rows,
            states=(False, False),
            epochs=(1, 2) if retirement == "epoch" else (1, 1),
            offline=(1,) if retirement == "offline" else (),
        )
        await publisher._step()
        assert [
            state["triggered"] for state in delivered_alarm(publisher.runtime, rows[0].key)
        ] == [True, False, False]
        if retirement == "offline":
            assert all(
                state["triggered"] is None
                for state in delivered_alarm(publisher.runtime, rows[1].key)
            )
    finally:
        await monitor.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("retirement", ["epoch", "offline", "expiry"])
async def test_history_admission_is_rechecked_after_waiting_to_send(
    tmp_path, monkeypatch, retirement
):
    import halo_server.devices.publication as publication

    clock = [1000.0]
    monkeypatch.setattr(publication, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monitor, rows = await two_panels(tmp_path)
    publisher = monitor.bridge.publisher
    runtime = publisher.runtime
    entered, release = asyncio.Event(), asyncio.Event()
    original = runtime.publish
    first = True

    async def blocked(snapshot, *, admission):
        nonlocal first
        if first:
            first = False
            entered.set()
            await release.wait()
        if not admission():
            return {"accepted": False}
        return await original(snapshot, admission=admission)

    runtime.publish = blocked
    observe_panels(monitor, rows, states=(True, False))
    observe_panels(monitor, rows, states=(False, False))
    task = asyncio.create_task(publisher._step())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        if retirement == "expiry":
            clock[0] += publication.ALARM_EVENT_TTL + 1
        else:
            observe_panels(
                monitor,
                rows,
                states=(False, False),
                epochs=(2, 1) if retirement == "epoch" else (1, 1),
                offline=(0,) if retirement == "offline" else (),
            )
        release.set()
        await asyncio.wait_for(task, 2)
        await publisher._step()
        assert not any(
            state["triggered"] is True for state in delivered_alarm(runtime, rows[0].key)
        )
        assert publisher.snapshot()["alarm_events_dropped"] > 0
    finally:
        release.set()
        await task
        await monitor.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["rejected", "exception"])
async def test_failed_history_delivery_is_counted_instead_of_silently_lost(tmp_path, failure):
    _service, _sessions, monitor, _provider, row = await alarm_ready(tmp_path)
    publisher = monitor.bridge.publisher
    original = publisher.runtime.publish
    first = True

    async def fail_once(snapshot, *, admission):
        nonlocal first
        if first:
            first = False
            if failure == "exception":
                raise RuntimeError("Synthetic delivery failure")
            return {"accepted": False}
        return await original(snapshot, admission=admission)

    publisher.runtime.publish = fail_once
    try:
        observe_alarm(monitor, row, phase="alarming", signal="burglar")
        observe_alarm(monitor, row)
        await publisher._step()
        assert publisher.snapshot()["alarm_events_dropped"] >= 1
        await publisher._step()
        assert delivered_alarm(publisher.runtime, row.key)[-1]["triggered"] is False
    finally:
        await monitor.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("retirement", ["offline", "epoch"])
async def test_proven_unsent_batch_preserves_other_locations_alarm_after_retirement(
    tmp_path, retirement
):
    from halo_server.devices.runtime import DeviceBridgeAdmissionRejected

    monitor, rows = await two_panels(tmp_path)
    publisher = monitor.bridge.publisher
    runtime = publisher.runtime
    entered, release = asyncio.Event(), asyncio.Event()
    original = runtime.publish
    first = True

    async def blocked(snapshot, *, admission):
        nonlocal first
        if first:
            first = False
            entered.set()
            await release.wait()
        if not admission():
            # This dedicated exception guarantees no send took place. A generic
            # timeout/error cannot safely authorize a replay of a prior alarm.
            raise DeviceBridgeAdmissionRejected("Synthetic unsent admission rejection")
        return await original(snapshot, admission=admission)

    runtime.publish = blocked
    observe_panels(monitor, rows, states=(True, False))
    observe_panels(monitor, rows, states=(False, False))
    task = asyncio.create_task(publisher._step())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        observe_panels(
            monitor,
            rows,
            states=(False, False),
            epochs=(1, 2) if retirement == "epoch" else (1, 1),
            offline=(1,) if retirement == "offline" else (),
        )
        release.set()
        await asyncio.wait_for(task, 2)
        await publisher._step()
        states = [state["triggered"] for state in delivered_alarm(runtime, rows[0].key)]
        assert states[0] is True
        assert all(state is False for state in states[1:])
        if retirement == "offline":
            assert all(
                state["triggered"] is None for state in delivered_alarm(runtime, rows[1].key)
            )
    finally:
        release.set()
        await task
        await monitor.close()
