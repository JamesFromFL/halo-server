"""Parallel HomeKit light scenes with synthetic SDK writes and inventory GETs."""

import asyncio
import concurrent.futures
import time
from dataclasses import asdict, replace
from uuid import uuid4

import pytest
from test_camera_light_confirmation import install_inventory
from test_homekit_broker import request
from test_homekit_selection import selected_bridge
from test_native_service import Camera, service_fixture

import halo_server.service as service_module
from halo_server.api.plugin import PluginCameraOperationError
from halo_server.homekit.models import CameraPolicy, HomeKitOperationError
from halo_server.ring.lighting import client_lighting


async def settled_until(predicate):
    async with asyncio.timeout(1):
        while not predicate():
            await asyncio.sleep(0)


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [2, 7])
async def test_parallel_scene_accepts_and_confirms_each_camera_after_all_acks(tmp_path, count):
    bridge, boot, _, service, client, _ = await selected_bridge(tmp_path, count=count)
    scopes = []
    for camera in service.homekit.inventory().cameras:
        scopes.append(
            await service.homekit.update_policy(
                boot, camera.scope, CameraPolicy(synchronized=True, streaming=True, controls=True)
            )
        )
    active, peak = 0, 0

    for camera in client.all_devices:
        camera.lights = "off"

        async def write(value, camera=camera):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            camera.light_values.append(value)
            try:
                await asyncio.sleep(0)
                camera.lights = "on" if value else "off"
            finally:
                active -= 1

        camera.async_set_light = write

    async def update():
        await asyncio.sleep(0)

    install_inventory(service, client, update)

    async def state(scope):
        return await bridge._request(
            request(
                boot,
                "control_state_request",
                {"scope": asdict(scope), "operation_id": str(uuid4())},
            )
        )

    async def turn_on(scope):
        response = await bridge._request(
            request(
                boot,
                "control_request",
                {
                    "scope": asdict(scope),
                    "operation_id": str(uuid4()),
                    "control": "light",
                    "enabled": True,
                },
            )
        )
        assert response["status"] == "accepted"
        assert (await state(scope))["light"] is True

    try:
        await asyncio.gather(*(turn_on(scope) for scope in scopes))
        assert [
            response["light"]
            for response in await asyncio.gather(*(state(scope) for scope in scopes))
        ] == [True] * count
        assert all(camera.light_values == [True] for camera in client.all_devices)
        assert peak == 1 and service._camera_light_pending == 0
        assert not service._media_sessions
    finally:
        await bridge._broker.retired(boot)


@pytest.mark.asyncio
async def test_same_camera_commands_keep_fifo_order_through_wait_and_readback(tmp_path):
    service, client, _ = service_fixture(tmp_path)
    camera = client.all_devices[0]
    camera.lights = "off"
    entered, release = asyncio.Event(), asyncio.Event()

    async def write(value):
        camera.light_values.append(value)
        if len(camera.light_values) == 1:
            entered.set()
            await release.wait()
        camera.lights = "on" if value else "off"

    async def update():
        await asyncio.sleep(0)

    camera.async_set_light = write
    install_inventory(service, client, update)
    first = asyncio.create_task(service._set_camera_control("42", "light", True))
    await entered.wait()
    second = asyncio.create_task(service._set_camera_control("42", "light", False))
    await settled_until(lambda: service._camera_light_pending == 2)
    third = asyncio.create_task(service._set_camera_control("42", "light", True))
    await settled_until(lambda: service._camera_light_pending == 3)
    release.set()
    await asyncio.gather(first, second, third)
    assert camera.light_values == [True, False, True]
    assert service._observed_camera_controls("42", camera)[0] is True
    assert service._camera_light_pending == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["release", "cancel", "timeout", "retire"])
async def test_group_reservation_wait_is_bounded_cancelable_and_unsent(
    tmp_path, monkeypatch, action
):
    service, client, _ = service_fixture(tmp_path)
    camera = client.all_devices[0]
    reservation = client_lighting(client)
    group = reservation.try_acquire()
    assert group is not None
    if action == "timeout":
        monkeypatch.setattr(service_module, "RING_CONTROL_TIMEOUT_SECONDS", 0.03)
    backend = service._owner_admin.control_backend
    scope = backend.inventory().cameras[0].scope
    command = asyncio.create_task(backend.set_control(scope, "light", True, admission=lambda: True))
    await settled_until(lambda: bool(reservation._waiters))
    assert client.submissions == 0 and camera.light_values == []
    if action == "release":
        reservation.release(group)
        await command
        assert camera.light_values == [True]
    elif action == "cancel":
        command.cancel()
        with pytest.raises(asyncio.CancelledError):
            await command
    else:
        if action == "retire":
            reservation.retire()
        with pytest.raises(HomeKitOperationError) as error:
            await command
        assert not error.value.possibly_sent
        assert error.value.code == (
            "lighting_timeout" if action == "timeout" else "control_superseded"
        )
    reservation.release(group)
    await asyncio.sleep(0)
    assert camera.light_values == ([True] if action == "release" else [])
    assert not reservation._waiters and service._camera_light_pending == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["account", "client", "device", "admission", "capability", "readiness"]
)
async def test_queue_revalidates_original_authority_before_send(tmp_path, change):
    service, client, sessions = service_fixture(tmp_path)
    camera = client.all_devices[0]
    reservation = client_lighting(client)
    group = reservation.try_acquire()
    permitted = True
    command = asyncio.create_task(
        service._set_camera_control("42", "light", True, admission=lambda: permitted)
    )
    await settled_until(lambda: bool(reservation._waiters))
    if change == "account":
        sessions.active_scope = replace(sessions.active_scope, generation=2)
    elif change == "client":
        sessions.active_client = None
    elif change == "device":
        client.all_devices[:] = [Camera()]
    elif change == "admission":
        permitted = False
    elif change == "capability":
        camera.capabilities.remove("light")
    else:
        service._account_integrations_ready = False
    reservation.release(group)
    with pytest.raises(PluginCameraOperationError) as error:
        await command
    assert error.value.code == "control_superseded"
    assert not camera.light_values and client.submissions == 0
    assert service._camera_light_pending == 0


@pytest.mark.asyncio
async def test_per_camera_lock_wait_uses_original_deadline_and_never_reserves(
    tmp_path, monkeypatch
):
    service, client, _ = service_fixture(tmp_path)
    monkeypatch.setattr(service_module, "RING_CONTROL_TIMEOUT_SECONDS", 0.03)
    lock = service._camera_control_lock("42")
    await lock.acquire()
    began = time.monotonic()
    try:
        with pytest.raises(PluginCameraOperationError) as error:
            await service._set_camera_control("42", "light", True)
        assert error.value.code == "lighting_timeout"
        assert time.monotonic() - began < 0.2
    finally:
        lock.release()
    assert client.submissions == 0 and not client.all_devices[0].light_values
    reservation = client_lighting(client)
    token = reservation.try_acquire()
    assert token is not None and service._camera_light_pending == 0
    reservation.release(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_delayed_ring_handoff_never_sends_after_expiry_or_cancel(
    tmp_path, monkeypatch, cancel
):
    service, client, _ = service_fixture(tmp_path)
    monkeypatch.setattr(service_module, "RING_CONTROL_TIMEOUT_SECONDS", 0.03)
    submitted = []

    def delayed(coroutine):
        future = concurrent.futures.Future()
        submitted.append((coroutine, future))
        return future

    client.submit = delayed
    command = asyncio.create_task(service._set_camera_control("42", "light", True))
    await settled_until(lambda: bool(submitted))
    if cancel:
        command.cancel()
        with pytest.raises(asyncio.CancelledError):
            await command
    else:
        with pytest.raises(PluginCameraOperationError) as error:
            await command
        assert error.value.code == "lighting_timeout"
    coroutine, future = submitted[0]
    assert future.cancelled()
    # Deliberately run the previously submitted coroutine despite cancellation,
    # modeling a Ring thread that could not schedule the cancellation promptly.
    with pytest.raises(PluginCameraOperationError):
        await coroutine
    assert client.all_devices[0].light_values == []
    assert client_lighting(client).revision == 0 and service._camera_light_pending == 0


@pytest.mark.asyncio
async def test_cancelled_sdk_cleanup_keeps_other_camera_unsent_until_actual_release(tmp_path):
    service, client, _ = service_fixture(tmp_path, cameras=[Camera("42"), Camera("43")])
    first, second = client.all_devices
    entered, cleaning, release = (asyncio.Event() for _ in range(3))

    async def write(value):
        first.light_values.append(value)
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cleaning.set()
            await release.wait()
            raise

    first.async_set_light = write
    command = asyncio.create_task(service._set_camera_control("42", "light", True))
    await entered.wait()
    command.cancel()
    with pytest.raises(asyncio.CancelledError):
        await command
    await cleaning.wait()
    waiting = asyncio.create_task(service._set_camera_control("43", "light", True))
    reservation = client_lighting(client)
    await settled_until(lambda: bool(reservation._waiters))
    assert second.light_values == []
    release.set()
    await waiting
    assert first.light_values == second.light_values == [True]
    assert service._camera_light_pending == 0


@pytest.mark.asyncio
async def test_queue_capacity_rejects_without_submission_and_recovers(tmp_path, monkeypatch):
    service, client, _ = service_fixture(tmp_path)
    monkeypatch.setattr(service_module, "CAMERA_LIGHT_PENDING_LIMIT", 1)
    reservation = client_lighting(client)
    group = reservation.try_acquire()
    waiting = asyncio.create_task(service._set_camera_control("42", "light", True))
    await settled_until(lambda: bool(reservation._waiters))
    with pytest.raises(PluginCameraOperationError) as error:
        await service._set_camera_control("42", "light", False)
    assert error.value.code == "lighting_busy" and client.submissions == 0
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    reservation.release(group)
    await service._set_camera_control("42", "light", True)
    assert client.all_devices[0].light_values == [True] and service._camera_light_pending == 0


@pytest.mark.asyncio
async def test_broker_queue_expiry_is_rejected_not_unknown(tmp_path, monkeypatch):
    bridge, boot, scope, service, client, _ = await selected_bridge(tmp_path)
    monkeypatch.setattr(service_module, "RING_CONTROL_TIMEOUT_SECONDS", 0.03)
    reservation = client_lighting(client)
    group = reservation.try_acquire()
    try:
        response = await bridge._request(
            request(
                boot,
                "control_request",
                {
                    "scope": asdict(scope),
                    "operation_id": str(uuid4()),
                    "control": "light",
                    "enabled": True,
                },
            )
        )
        # The existing IPC vocabulary redacts service-specific error names.
        assert response["status"] == "rejected" and response["code"] == "source-failed"
        assert client.submissions == 0 and client.all_devices[0].light_values == []
    finally:
        reservation.release(group)
        await bridge._broker.retired(boot)


@pytest.mark.asyncio
async def test_setter_does_not_receive_a_new_deadline_after_queue_wait(tmp_path, monkeypatch):
    service, client, _ = service_fixture(tmp_path)
    monkeypatch.setattr(service_module, "RING_CONTROL_TIMEOUT_SECONDS", 0.2)
    reservation = client_lighting(client)
    group = reservation.try_acquire()
    entered, canceled = asyncio.Event(), asyncio.Event()
    camera = client.all_devices[0]

    async def write(value):
        camera.light_values.append(value)
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            canceled.set()

    camera.async_set_light = write
    began = time.monotonic()
    command = asyncio.create_task(service._set_camera_control("42", "light", True))
    await settled_until(lambda: bool(reservation._waiters))
    await asyncio.sleep(0.15)
    reservation.release(group)
    await entered.wait()
    with pytest.raises(PluginCameraOperationError) as error:
        await command
    assert error.value.code == "control_timeout"
    assert time.monotonic() - began < 0.3  # A fresh post-queue deadline would exceed 0.35s.
    await canceled.wait()
    assert camera.light_values == [True] and service._camera_light_pending == 0
