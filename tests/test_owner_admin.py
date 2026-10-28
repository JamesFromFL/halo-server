"""Owner-only settings and verified controls across operating modes."""

import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest
from test_native_service import Camera, service_fixture

from halo_server.api.admin import AdminProtocolError
from halo_server.configuration import ConfigurationStore, ServerMode


@pytest.mark.asyncio
async def test_saved_configuration_cas_does_not_pretend_runtime_changed(tmp_path):
    service, client, _ = service_fixture(tmp_path)
    first = await service.handle_admin("configuration_get", {})
    assert first["applied"]["mode"] == "homekit"
    value = {
        key: value for key, value in first["saved"].items() if key not in {"version", "revision"}
    }
    value["mode"] = "ring-only"
    saved = await service.handle_admin(
        "configuration_set",
        {"expected_revision": first["saved"]["revision"], "configuration": value},
    )
    assert saved["saved"]["mode"] == "ring-only" and saved["restart_required"]
    assert service.configuration.mode == ServerMode.HOMEKIT and client.submissions == 0
    assert ConfigurationStore(service.paths.configuration_file).load().mode == ServerMode.RING_ONLY
    with pytest.raises(AdminProtocolError) as error:
        await service.handle_admin(
            "configuration_set",
            {"expected_revision": first["saved"]["revision"], "configuration": value},
        )
    assert error.value.code == "configuration_conflict"


def control(row, page, **changes):
    return {
        "profile_id": page["profile_id"],
        "account_generation": page["account_generation"],
        "camera_id": row["camera_id"],
        "device_generation": row["device_generation"],
        "operation_id": str(uuid4()),
        "control": "light",
        "enabled": True,
    } | changes


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", list(ServerMode))
async def test_verified_owner_controls_work_in_every_mode_without_homekit_selection(tmp_path, mode):
    service, client, _ = service_fixture(tmp_path, mode=mode)
    page = await service.handle_admin("camera_control_list", {})
    row = page["cameras"][0]
    assert row["has_light"] and not row["has_siren"] and row["light"] is None
    request = control(row, page)
    result = await service.handle_admin("camera_control", request)
    assert result == {"status": "accepted", "code": "ok"}
    assert client.all_devices[0].light_values == [True]
    assert await service.handle_admin("camera_control", request) == result
    assert client.all_devices[0].light_values == [True]
    with pytest.raises(AdminProtocolError) as error:
        await service.handle_admin("camera_control", request | {"enabled": False})
    assert error.value.code == "operation_conflict"


@pytest.mark.asyncio
async def test_replaced_device_and_account_cannot_inherit_owner_control_scope(tmp_path):
    service, client, sessions = service_fixture(tmp_path)
    page = await service.handle_admin("camera_control_list", {})
    request = control(page["cameras"][0], page)
    client.all_devices[:] = [Camera()]
    assert (await service.handle_admin("camera_control", request))["status"] == "rejected"
    assert client.submissions == 0
    page = await service.handle_admin("camera_control_list", {})
    request = control(page["cameras"][0], page)
    sessions.active_scope = replace(sessions.active_scope, generation=2)
    service._media_scope = sessions.active_scope
    assert (await service.handle_admin("camera_control", request))["status"] == "rejected"
    assert client.submissions == 0


@pytest.mark.asyncio
async def test_owner_control_disconnect_does_not_repeat_sent_command(tmp_path):
    service, client, _ = service_fixture(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()

    async def pause(enabled):
        entered.set()
        await release.wait()
        client.all_devices[0].light_values.append(enabled)

    client.all_devices[0].async_set_light = pause
    page = await service.handle_admin("camera_control_list", {})
    request = control(page["cameras"][0], page)
    waiter = asyncio.create_task(service.handle_admin("camera_control", request))
    await entered.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    release.set()
    assert (await service.handle_admin("camera_control", request))["status"] == "accepted"
    assert client.submissions == 1 and client.all_devices[0].light_values == [True]
