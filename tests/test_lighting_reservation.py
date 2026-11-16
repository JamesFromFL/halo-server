"""Shared account lighting ownership across the daemon and Ring event loops."""

import asyncio
import time

import pytest
from test_native_service import service_fixture

from halo_server.api.plugin import PluginCameraOperationError
from halo_server.ring.lighting import LightingReservation, client_lighting


@pytest.mark.asyncio
async def test_group_reservation_defers_camera_until_exclusive_owner_releases(tmp_path):
    service, client, _ = service_fixture(tmp_path)
    reservation = client_lighting(client)
    token = reservation.try_acquire()
    command = asyncio.create_task(service._set_camera_control("42", "light", True))
    try:
        await asyncio.sleep(0)
        assert not command.done()
        assert client.submissions == 0 and not client.all_devices[0].light_values
        assert reservation.valid(token)
        reservation.release(token)
        await command
        assert client.all_devices[0].light_values == [True]
        assert reservation.revision == 2
    finally:
        reservation.release(token)
        if not command.done():
            command.cancel()
        await asyncio.gather(command, return_exceptions=True)


def test_acknowledgement_is_idempotent_and_requires_active_sent_token():
    reservation = LightingReservation()
    first = reservation.try_acquire()
    assert reservation.acknowledge(None) is None
    assert reservation.acknowledge(first) is None and reservation.revision == 0
    assert reservation.mark_sent(first) and reservation.revision == 1
    assert reservation.acknowledge(first) == 2
    assert reservation.acknowledge(first) == 2 and reservation.revision == 2
    reservation.release(first)
    second = reservation.try_acquire()
    assert reservation.acknowledge(first) is None and reservation.revision == 2
    assert reservation.mark_sent(second) and reservation.revision == 3
    reservation.retire()
    assert reservation.acknowledge(second) is None and reservation.revision == 3


@pytest.mark.asyncio
async def test_failed_camera_setter_never_records_ack_or_retries(tmp_path):
    service, client, _ = service_fixture(tmp_path)
    camera = client.all_devices[0]

    async def failed(value):
        camera.light_values.append(value)
        raise RuntimeError("synthetic setter failure")

    camera.async_set_light = failed
    with pytest.raises(PluginCameraOperationError) as error:
        await service._set_camera_control("42", "light", True)
    assert error.value.code == "control_failed"
    reservation = client_lighting(client)
    assert camera.light_values == [True] and reservation.revision == 1
    token = reservation.try_acquire()
    assert token is not None
    reservation.release(token)


@pytest.mark.asyncio
async def test_cancelled_camera_keeps_reservation_until_actual_sdk_cleanup(tmp_path):
    service, client, _ = service_fixture(tmp_path)
    reservation = client_lighting(client)
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def setter(on):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cleaning.set()
            await release.wait()
            raise

    client.all_devices[0].async_set_light = setter
    command = asyncio.create_task(service._set_camera_control("42", "light", True))
    await entered.wait()
    command.cancel()
    with pytest.raises(asyncio.CancelledError):
        await command
    await cleaning.wait()
    assert reservation.try_acquire() is None
    release.set()
    for _ in range(20):
        await asyncio.sleep(0)
        token = reservation.try_acquire()
        if token:
            break
    assert token is not None
    reservation.release(token)


@pytest.mark.asyncio
async def test_camera_cancelled_before_ring_admission_releases_without_late_actuation(tmp_path):
    service, client, _ = service_fixture(tmp_path)
    reservation = client_lighting(client)
    lock = service._camera_control_lock("42")
    await lock.acquire()
    command = asyncio.create_task(service._set_camera_control("42", "light", True))
    await asyncio.sleep(0)
    # Waiting for this camera's FIFO lock must not block other cameras/groups.
    other_owner = reservation.try_acquire()
    assert other_owner is not None
    reservation.release(other_owner)
    command.cancel()
    with pytest.raises(asyncio.CancelledError):
        await command
    lock.release()
    token = reservation.try_acquire()
    assert token is not None and not client.all_devices[0].light_values
    reservation.release(token)


@pytest.mark.asyncio
async def test_shared_mutation_invalidates_camera_light_observation(tmp_path):
    service, client, _ = service_fixture(tmp_path)
    camera = client.all_devices[0]
    camera.lights = "on"
    service._remember_control_observations(
        client, refreshed_at=time.monotonic(), lighting_revision=client_lighting(client).revision
    )
    before = service._observed_camera_controls("42", camera)
    reservation = client_lighting(client)
    token = reservation.try_acquire()
    reservation.mark_sent(token)
    reservation.release(token)
    after = service._observed_camera_controls("42", camera)
    assert after[0] is None and after[1] == before[1]
    service._remember_control_observations(
        client, refreshed_at=time.monotonic(), lighting_revision=client_lighting(client).revision
    )
    assert service._observed_camera_controls("42", camera) == before
