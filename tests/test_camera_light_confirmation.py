"""One light write followed by a scoped, post-acknowledgement Ring GET.

The SDK inventory transport is synthetic; no camera, network or media is used.
"""

import asyncio
import time
from dataclasses import asdict, replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_homekit_broker import request
from test_homekit_selection import selected_bridge
from test_native_service import Camera, service_fixture

from halo_server.homekit.models import HomeKitOperationError
from halo_server.ring.client import RingClient
from halo_server.ring.lighting import client_lighting


def install_inventory(service, client, update, *, capture_allowed=False):
    """Use the actual cache/serialization/receipt implementation with a fake GET."""
    client._ring = SimpleNamespace(
        devices=lambda: SimpleNamespace(all_devices=list(client.all_devices))
    )
    client._inventory_refresh_lock = asyncio.Lock()
    client._last_device_update_at = time.monotonic()
    client._last_device_update_receipt = (
        client._last_device_update_at,
        client_lighting(client).revision,
    )
    client._record_inventory_receipt = RingClient._record_inventory_receipt.__get__(client)
    client._async_refresh_devices = RingClient._async_refresh_devices.__get__(client)

    async def get(ring):
        assert not service._session_change_lock.locked()
        assert capture_allowed or not service._media_sessions
        await update()

    client._async_update_inventory = get
    service._remember_control_observations(client)


@pytest.mark.asyncio
@pytest.mark.parametrize("observed", [True, False, None, "failure"])
async def test_real_hap_control_reply_is_accepted_then_observed_without_optimism(
    tmp_path, observed
):
    bridge, boot, scope, service, client, _ = await selected_bridge(tmp_path)
    camera = client.all_devices[0]
    camera.lights = "off"
    order = []

    async def write(value):
        camera.light_values.append(value)
        order.append("ack")

    async def update():
        assert order == ["ack"]
        order.append("get")
        if observed == "failure":
            raise RuntimeError("synthetic GET failure after accepted write")
        camera.lights = "on" if observed is True else "off" if observed is False else "unknown"

    camera.async_set_light = write
    install_inventory(service, client, update)
    operation = request(
        boot,
        "control_request",
        {"scope": asdict(scope), "operation_id": str(uuid4()), "control": "light", "enabled": True},
    )
    try:
        assert (await bridge._request(operation))["status"] == "accepted"
        state = await bridge._request(
            request(
                boot,
                "control_state_request",
                {"scope": asdict(scope), "operation_id": str(uuid4())},
            )
        )
        expected = observed if type(observed) is bool else None
        assert state["light"] is expected
        assert camera.light_values == [True] and order == ["ack", "get"]
        assert not service._media_sessions
        assert (await bridge._request(operation))["status"] == "rejected"
        assert camera.light_values == [True]  # No replay after failed/mismatched confirmation.
    finally:
        await bridge._broker.retired(boot)


@pytest.mark.asyncio
async def test_older_inflight_get_cannot_satisfy_post_ack_confirmation(tmp_path):
    service, client, _ = service_fixture(tmp_path)
    camera = client.all_devices[0]
    camera.lights = "off"
    first_entered, first_release, second_entered, second_release = (
        asyncio.Event() for _ in range(4)
    )
    order = []

    async def update():
        if not first_entered.is_set():
            order.append("old-get")
            first_entered.set()
            await first_release.wait()
            camera.lights = "off"
        else:
            order.append("new-get")
            second_entered.set()
            await second_release.wait()
            camera.lights = "on"

    async def write(value):
        camera.light_values.append(value)
        order.append("ack")

    camera.async_set_light = write
    install_inventory(service, client, update)
    older = asyncio.create_task(client._async_refresh_devices(None, -1))
    await first_entered.wait()
    command = asyncio.create_task(service._set_camera_control("42", "light", True))
    try:
        while not camera.light_values:
            await asyncio.sleep(0)
        first_release.set()
        await older
        await second_entered.wait()
        assert order == ["old-get", "ack", "new-get"]
        assert not command.done()
        assert service._observed_camera_controls("42", camera)[0] is None
        second_release.set()
        await command
        assert service._observed_camera_controls("42", camera)[0] is True
        assert camera.light_values == [True]
    finally:
        first_release.set()
        second_release.set()
        await asyncio.gather(older, command, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("publish_after_ack", [False, True])
async def test_get_begun_during_write_cannot_confirm_when_post_ack_get_fails(
    tmp_path, publish_after_ack
):
    service, client, _ = service_fixture(tmp_path)
    camera = client.all_devices[0]
    camera.lights = "off"
    sent, acknowledge, get_entered, finish_get = (asyncio.Event() for _ in range(4))
    reads = []

    async def write(value):
        camera.light_values.append(value)
        sent.set()
        await acknowledge.wait()

    async def update():
        reads.append(client_lighting(client).revision)
        if len(reads) == 2:
            raise RuntimeError("synthetic post-ACK GET failure")
        get_entered.set()
        await finish_get.wait()
        camera.lights = "on"

    async def normal_inventory_refresh():
        await client._async_refresh_devices(None, -1)
        # Model normal inventory publication independently from confirmation.
        service._remember_control_observations(client)

    camera.async_set_light = write
    install_inventory(service, client, update)
    command = asyncio.create_task(service._set_camera_control("42", "light", True))
    await sent.wait()
    normal_refresh = asyncio.create_task(normal_inventory_refresh())
    try:
        await get_entered.wait()
        if publish_after_ack:
            acknowledge.set()
            while client_lighting(client).revision != 2:
                await asyncio.sleep(0)
            finish_get.set()
            await normal_refresh
        else:
            finish_get.set()
            await normal_refresh
            assert service._observed_camera_controls("42", camera)[0] is True
            acknowledge.set()
        await command
        assert reads == [1, 2]
        assert client_lighting(client).revision == 2
        assert service._control_lighting_revision == 1
        assert service._observed_camera_controls("42", camera)[0] is None
        assert camera.light_values == [True] and not service._media_sessions
    finally:
        acknowledge.set()
        finish_get.set()
        await asyncio.gather(normal_refresh, command, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["account", "client", "device", "admission", "capability", "lighting"]
)
async def test_crossed_authority_never_publishes_confirmation_or_retries_write(tmp_path, change):
    service, client, sessions = service_fixture(tmp_path)
    camera = client.all_devices[0]
    camera.lights = "off"
    entered, release = asyncio.Event(), asyncio.Event()
    permitted = True

    async def update():
        entered.set()
        await release.wait()
        camera.lights = "on"

    install_inventory(service, client, update)
    backend = service._owner_admin.control_backend
    scope = backend.inventory().cameras[0].scope
    command = asyncio.create_task(
        backend.set_control(scope, "light", True, admission=lambda: permitted)
    )
    try:
        await entered.wait()
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
            reservation = client_lighting(client)
            token = reservation.try_acquire()
            assert token is not None and reservation.mark_sent(token)
            reservation.release(token)
        release.set()
        if change in {"account", "client", "device", "admission"}:
            with pytest.raises(HomeKitOperationError) as error:
                await command
            assert error.value.possibly_sent
        else:
            await command  # Accepted original write; confirmation is unavailable.
        assert service._observed_camera_controls("42", camera)[0] is None
        assert camera.light_values == [True] and not service._media_sessions
    finally:
        release.set()
        await asyncio.gather(command, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_confirmation_timeout_or_cancellation_has_no_late_publication(
    tmp_path, monkeypatch, cancel
):
    import halo_server.service as service_module

    service, client, _ = service_fixture(tmp_path)
    camera = client.all_devices[0]
    camera.lights = "off"
    entered, cleaning, release, finished = (asyncio.Event() for _ in range(4))

    async def update():
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cleaning.set()
            await release.wait()
            camera.lights = "on"
        finally:
            finished.set()

    install_inventory(service, client, update)
    monkeypatch.setattr(service_module, "CONTROL_CONFIRMATION_TIMEOUT_SECONDS", 0.01)
    command = asyncio.create_task(service._set_camera_control("42", "light", True))
    try:
        await entered.wait()
        if cancel:
            command.cancel()
            with pytest.raises(asyncio.CancelledError):
                await command
        else:
            await command  # A timed-out GET does not turn ACK into safely-unsent failure.
        await cleaning.wait()
        release.set()
        await finished.wait()
        await asyncio.sleep(0)
        assert camera.light_values == [True]
        assert service._observed_camera_controls("42", camera)[0] is None
    finally:
        release.set()
        await asyncio.gather(command, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("newer_observation", [False, True])
async def test_copied_get_values_are_not_relabelled_and_newer_external_off_wins(
    tmp_path, newer_observation
):
    service, client, _ = service_fixture(tmp_path)
    camera = client.all_devices[0]
    camera.lights = "off"
    entered, release, copied = (asyncio.Event() for _ in range(3))

    async def update():
        entered.set()
        await release.wait()
        camera.lights = "on"

    install_inventory(service, client, update)
    submit = client.submit

    def submitted(coroutine):
        future = submit(coroutine)
        if client.submissions == 2:
            future.add_done_callback(lambda _: copied.set())
        return future

    client.submit = submitted
    command = asyncio.create_task(service._set_camera_control("42", "light", True))
    try:
        await entered.wait()
        async with service._session_change_lock:
            release.set()
            await copied.wait()
            camera.lights = "off"
            if newer_observation:
                service._remember_control_observations(
                    client,
                    refreshed_at=time.monotonic(),
                    lighting_revision=client_lighting(client).revision,
                )
        await command
        assert service._observed_camera_controls("42", camera)[0] is (not newer_observation)
        assert camera.light_values == [True]
    finally:
        release.set()
        await asyncio.gather(command, return_exceptions=True)


@pytest.mark.asyncio
async def test_control_get_also_fences_new_ring_privacy_revocation(tmp_path):
    bridge, boot, scope, service, client, _ = await selected_bridge(tmp_path)
    camera = client.all_devices[0]
    camera.lights = "off"
    handle = await service.homekit.open_live(boot, scope)
    capture = service._media_sessions[scope.camera_id]

    async def update():
        camera.lights = "on"
        camera._attrs["settings"]["live_view_disabled"] = True

    install_inventory(service, client, update, capture_allowed=True)
    try:
        result = await bridge._request(
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
        assert result["status"] == "unknown"  # Sent; old HAP authority was revoked by the GET.
        assert camera.light_values == [True] and not handle.is_current()
        assert not service._media_sessions
        await service._drain_media_tasks()
        assert not capture.leases
        assert service._observed_camera_controls(scope.camera_id, camera)[0] is True
        assert not service._camera_privacy_allowed(scope.camera_id, "live", homekit=True)
    finally:
        await bridge._broker.retired(boot)


@pytest.mark.asyncio
@pytest.mark.parametrize("receipt", ["cached", "wrong-revision", "future", "missing"])
async def test_read_without_valid_post_ack_receipt_cannot_confirm(tmp_path, receipt):
    service, client, _ = service_fixture(tmp_path)
    camera = client.all_devices[0]
    camera.lights = "off"

    async def update():
        camera.lights = "on"

    install_inventory(service, client, update)
    original = client._last_device_update_receipt

    def record(revision):
        client._last_device_update_receipt = {
            "cached": original,
            "wrong-revision": (time.monotonic(), revision - 1),
            "future": (time.monotonic() + 10, revision),
            "missing": None,
        }[receipt]

    client._record_inventory_receipt = record
    await service._set_camera_control("42", "light", True)
    assert camera.light_values == [True]
    assert service._observed_camera_controls("42", camera)[0] is None
