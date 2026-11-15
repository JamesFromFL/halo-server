"""All discovered cameras can record; transport pressure must not discard clips."""

import asyncio
from dataclasses import asdict, replace
from uuid import uuid4

import pytest
from test_homekit_broker import setup_broker
from test_homekit_controller import POLICY
from test_homekit_recording_broker import (
    Channel,
    Manager,
    Policy,
    configured,
    rpc,
    wire,
)
from test_homekit_recording_manager import Capture, configuration
from test_homekit_recording_manager import setup as manager_setup
from test_native_service import Camera
from test_recording_capture import setup as capture_setup

from halo_server.homekit.broker import RecordingEvent
from halo_server.homekit.bulk import BulkTransferError
from halo_server.homekit.models import HomeKitOperationError
from halo_server.media.recording_policy import RecordingAuthority


@pytest.mark.asyncio
async def test_seven_camera_controller_and_capture_admission_keep_per_camera_exclusivity():
    controller, _, boot, scopes, _, _, captures = await capture_setup(7)
    opened = []
    try:
        for scope in scopes:
            opened.append(
                await captures.open(
                    boot,
                    scope,
                    str(uuid4()),
                    warm=True,
                    owner_opt_in=True,
                    recording_supported=True,
                )
            )
            opened.append(await captures.open(boot, scope, str(uuid4())))
        assert len(opened) == 14
        assert all(not capture.retired for capture in opened)
        for warm in (False, True):
            with pytest.raises(HomeKitOperationError) as error:
                await captures.open(
                    boot,
                    scopes[0],
                    str(uuid4()),
                    warm=warm,
                    owner_opt_in=True,
                    recording_supported=True,
                )
            assert error.value.code == "busy"
        changed = await controller.update_policy(boot, scopes[0], replace(POLICY, recording=False))
        assert changed != scopes[0]
        with pytest.raises(HomeKitOperationError):
            opened[0].snapshot()
        assert all(not capture.retired for capture in opened[2:])
    finally:
        await captures.close()
        await controller.close()


@pytest.mark.asyncio
async def test_seven_camera_mux_jobs_have_independent_capture_and_cleanup():
    manager, controller, boot, scopes, captures, workers = await manager_setup(count=7)
    source_captures = []

    async def independent_capture(*args, **kwargs):
        capture = Capture()
        source_captures.append(capture)
        return capture

    captures.open = independent_capture
    streams = []
    try:
        streams = await asyncio.gather(
            *(
                manager.open(boot, scope, str(uuid4()), configuration(), event_at=100)
                for scope in scopes
            )
        )
        assert len(workers) == len(source_captures) == 7
        assert all(stream.is_current() for stream in streams)
        with pytest.raises(HomeKitOperationError) as error:
            await manager.open(boot, scopes[0], str(uuid4()), configuration(), event_at=100)
        assert error.value.code == "capacity"
        await streams[0].close()
        assert source_captures[0].closed
        assert all(stream.is_current() for stream in streams[1:])
    finally:
        await manager.close()
        await controller.close()
    assert all(capture.closed for capture in source_captures)
    assert all(worker.closed for worker in workers)


@pytest.mark.asyncio
async def test_seven_broker_recordings_share_bounded_transfer_window_without_losing_packets(
    tmp_path,
):
    broker, boot, peer, _, _, _ = await setup_broker(
        tmp_path, cameras=[Camera(str(index)) for index in range(7)]
    )
    config = configuration()
    broker._recording_authority = lambda scope: RecordingAuthority(True, False, config, True, True)
    manager = broker.recording_manager = Manager()
    broker.recording_policy = Policy()
    now = asyncio.get_running_loop().time()
    broker._recording_event = lambda scope: RecordingEvent(1, now - 0.001, now + 30)

    class BoundedChannel(Channel):
        def prepare_send(self, **parameters):
            if sum(not handle.retirement_complete for handle in self.handles) >= 4:
                raise BulkTransferError("busy")
            return super().prepare_send(**parameters)

    channel = peer.bulk_channel = BoundedChannel()
    data = []
    pulls = []
    try:
        for camera in broker.controller.inventory().cameras:
            scope = await broker.controller.update_policy(
                boot, camera.scope, replace(POLICY, recording=True)
            )
            data.append(
                {"scope": asdict(scope), "job_id": str(uuid4()), "configuration": wire(config)}
            )
        for value in data:
            _, reply = await rpc(broker, boot, "recording_open", value)
            assert reply["payload"]["status"] == "accepted"
        assert len(manager.opens) == 7
        for value in data:
            pulls.append(
                asyncio.create_task(
                    rpc(
                        broker,
                        boot,
                        "recording_next",
                        {
                            "scope": value["scope"],
                            "job_id": value["job_id"],
                            "sequence": 0,
                        },
                    )
                )
            )
        for _ in range(50):
            if len(channel.handles) == 4:
                break
            await asyncio.sleep(0.002)
        assert len(channel.handles) == 4
        assert sum(task.done() for task in pulls) == 4
        # Complete transfer batches as capacity becomes available. Waiting jobs
        # retain their original init packet and sequence rather than reading again.
        for task in pulls:
            envelope, reply = await asyncio.wait_for(task, 2)
            assert reply["payload"]["status"] == "ready"
            assert reply["payload"]["kind"] == "init"
            assert reply["payload"]["sequence"] == 0
            await broker.replied(envelope, reply)
        for _ in range(50):
            if all(handle.success for handle in channel.handles):
                break
            await asyncio.sleep(0.002)
        assert len(channel.handles) == 7 and all(handle.success for handle in channel.handles)
        assert all(stream.packets[0].kind == "fragment" for stream in manager.streams.values())
    finally:
        await broker.retired(boot)
        await asyncio.gather(*pulls, return_exceptions=True)


@pytest.mark.asyncio
async def test_waiting_transfer_rechecks_revoked_recording_authority(tmp_path):
    broker, boot, state, _, channel, _, data = await configured(tmp_path)
    task = None
    try:
        await rpc(broker, boot, "recording_open", data)
        channel.busy = True
        task = asyncio.create_task(
            rpc(
                broker,
                boot,
                "recording_next",
                {
                    "scope": data["scope"],
                    "job_id": data["job_id"],
                    "sequence": 0,
                },
            )
        )
        await asyncio.sleep(0.025)
        assert not task.done()
        state[0] = replace(state[0], owner_opt_in=False)
        _, reply = await asyncio.wait_for(task, 1)
        assert reply["payload"]["status"] == "rejected"
        assert not channel.handles
    finally:
        await broker.retired(boot)
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)


async def two_recordings_with_busy_transport(tmp_path):
    broker, boot, peer, _, _, _ = await setup_broker(tmp_path, cameras=[Camera("42"), Camera("43")])
    config = configuration()
    broker._recording_authority = lambda scope: RecordingAuthority(True, False, config, True, True)
    manager = broker.recording_manager = Manager()
    broker.recording_policy = Policy()
    now = asyncio.get_running_loop().time()
    broker._recording_event = lambda scope: RecordingEvent(1, now - 0.001, now + 30)

    class BusyChannel(Channel):
        def __init__(self):
            super().__init__()
            self.busy = True
            self.attempted = asyncio.Event()

        def prepare_send(self, **parameters):
            self.attempted.set()
            return super().prepare_send(**parameters)

    channel = peer.bulk_channel = BusyChannel()
    data = []
    for camera in broker.controller.inventory().cameras:
        scope = await broker.controller.update_policy(
            boot, camera.scope, replace(POLICY, recording=True)
        )
        value = {"scope": asdict(scope), "job_id": str(uuid4()), "configuration": wire(config)}
        data.append(value)
        _, reply = await rpc(broker, boot, "recording_open", value)
        assert reply["payload"]["status"] == "accepted"
    return broker, boot, manager, channel, data


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["stop", "cancel"])
async def test_stop_or_cancel_while_waiting_retires_only_exact_recording(tmp_path, action):
    broker, boot, manager, channel, data = await two_recordings_with_busy_transport(tmp_path)
    task = None
    try:
        task = asyncio.create_task(
            rpc(
                broker,
                boot,
                "recording_next",
                {
                    "scope": data[0]["scope"],
                    "job_id": data[0]["job_id"],
                    "sequence": 0,
                },
            )
        )
        await asyncio.wait_for(channel.attempted.wait(), 1)
        assert not task.done() and not channel.handles
        if action == "stop":
            _, reply = await asyncio.wait_for(
                rpc(
                    broker,
                    boot,
                    "recording_stop",
                    {
                        "scope": data[0]["scope"],
                        "job_id": data[0]["job_id"],
                    },
                ),
                1,
            )
            assert reply["payload"]["status"] == "stopped"
        else:
            task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        assert not channel.handles
        assert broker.recording_resources == 1 and not broker.recording_cleanup_failures
        assert set(manager.streams) == {data[1]["job_id"]}
        assert manager.streams[data[1]["job_id"]].is_current()
        channel.busy = False
        envelope, reply = await rpc(
            broker,
            boot,
            "recording_next",
            {
                "scope": data[1]["scope"],
                "job_id": data[1]["job_id"],
                "sequence": 0,
            },
        )
        assert reply["payload"]["status"] == "ready" and reply["payload"]["kind"] == "init"
        await broker.replied(envelope, reply)
    finally:
        await broker.retired(boot)
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_busy_transfer_deadline_retires_only_waiting_recording(tmp_path, monkeypatch):
    from halo_server.homekit import broker as broker_module

    monkeypatch.setattr(broker_module, "RECORDING_TRANSFER_SECONDS", 0.03)
    broker, boot, manager, channel, data = await two_recordings_with_busy_transport(tmp_path)
    try:
        _, reply = await asyncio.wait_for(
            rpc(
                broker,
                boot,
                "recording_next",
                {
                    "scope": data[0]["scope"],
                    "job_id": data[0]["job_id"],
                    "sequence": 0,
                },
            ),
            1,
        )
        assert reply["payload"]["status"] == "rejected" and reply["payload"]["code"] == "busy"
        assert channel.attempted.is_set() and not channel.handles
        assert broker.recording_resources == 1 and not broker.recording_cleanup_failures
        assert set(manager.streams) == {data[1]["job_id"]}
        assert manager.streams[data[1]["job_id"]].is_current()
        channel.busy = False
        envelope, reply = await rpc(
            broker,
            boot,
            "recording_next",
            {
                "scope": data[1]["scope"],
                "job_id": data[1]["job_id"],
                "sequence": 0,
            },
        )
        assert reply["payload"]["status"] == "ready" and reply["payload"]["sequence"] == 0
        await broker.replied(envelope, reply)
    finally:
        await broker.retired(boot)
