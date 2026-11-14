"""The web management projection must stay small, truthful, and media-free."""

import asyncio
import json
from dataclasses import replace
from uuid import uuid4

import pytest
from test_native_service import Camera, service_fixture

from halo_server.api.admin import AdminProtocolError
from halo_server.configuration import ServerMode
from halo_server.owner_activity import OwnerActivity
from halo_server.owner_state import MAX_OWNER_STATE_BYTES
from halo_server.ring.events import EventConnectionState


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", list(ServerMode))
async def test_management_read_never_calls_media_or_heavy_status(tmp_path, monkeypatch, mode):
    service, client, _ = service_fixture(tmp_path, mode=mode)

    def forbidden(*args, **kwargs):
        raise AssertionError("Management state acquired media or full diagnostics")

    monkeypatch.setattr(service, "status", forbidden)
    if service._homekit_bridge:
        monkeypatch.setattr(service._homekit_bridge, "status", forbidden)
        monkeypatch.setattr(service._homekit_bridge, "cameras", forbidden)
    for _ in range(10):
        state = await service.handle_admin("owner_state", {})
        assert state["schema"] == 1
        assert state["devices"][0]["observed"]["source_available"] is None
        assert state["devices"][0]["homekit"]["paired"] is None
        assert state["devices"][0]["scope"]["device_generation"] == 1
        assert state["ring"]["authenticated"]
        assert not state["truncated"]
    assert client.submissions == 0 and client.all_devices[0].snapshot_calls == 0
    assert not service._media_sessions
    assert not service.paths.state_directory.exists()


@pytest.mark.asyncio
async def test_activity_records_settled_control_after_client_disconnect(tmp_path):
    service, client, _ = service_fixture(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()

    async def set_light(enabled):
        entered.set()
        await release.wait()

    client.all_devices[0].async_set_light = set_light
    state = await service.handle_admin("owner_state", {})
    camera = state["devices"][0]
    params = camera["scope"] | {
        "camera_id": camera["id"],
        "control": "light",
        "enabled": True,
        "operation_id": str(uuid4()),
    }
    task = asyncio.create_task(service.handle_admin("camera_control", params))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    await asyncio.gather(*service._owner_tasks)
    events = (await service.handle_admin("owner_state", {}))["activity"]["entries"]
    assert events[-1]["action"] == "camera_control"
    assert events[-1]["outcome"] == "accepted"
    assert set(events[-1]) == {"sequence", "at", "category", "action", "outcome", "camera_id"}


@pytest.mark.asyncio
async def test_account_transition_fences_activity_without_waiting_for_login_cleanup(tmp_path):
    service, _, sessions = service_fixture(tmp_path)
    service._set_event_state(service._listener_generation, EventConnectionState.CONNECTED)
    before = await service.handle_admin("owner_state", {})
    assert before["activity"]["entries"][-1]["outcome"] == "connected"
    sessions.active_scope = replace(sessions.active_scope, generation=2)
    after = await service.handle_admin("owner_state", {})
    assert after["epoch"] != before["epoch"]
    assert not after["activity"]["entries"] and not after["devices"]
    assert not after["ring"]["authenticated"]


def test_owner_activity_is_bounded_and_rejects_arbitrary_content():
    activity = OwnerActivity(lambda: None)
    for _ in range(300):
        activity.record("owner", "configuration_set", "accepted")
    snapshot = activity.snapshot()
    assert snapshot["cursor"] == 300
    assert len(snapshot["entries"]) == 256 and snapshot["truncated"]
    with pytest.raises(ValueError):
        activity.record("owner", "password=SECRET", "accepted")


@pytest.mark.asyncio
async def test_activity_does_not_call_uncertain_storage_failure_rejected(tmp_path, monkeypatch):
    service, _, _ = service_fixture(tmp_path)

    async def uncertain(method, params):
        raise AdminProtocolError("configuration_unavailable", "Save outcome requires reload")

    monkeypatch.setattr(service, "_dispatch_admin", uncertain)
    with pytest.raises(AdminProtocolError):
        await service.handle_admin("configuration_set", {})
    assert service._owner_activity.snapshot()["entries"][-1]["outcome"] == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["login", "logout"])
async def test_queued_account_operation_rechecks_epoch_under_session_lock(tmp_path, method):
    service, client, sessions = service_fixture(tmp_path)
    before = await service.handle_admin("owner_state", {})
    params = {"expected_epoch": before["epoch"]}
    if method == "login":
        params.update(email="fixture@example.invalid", password="fixture-password")
    async with service._session_change_lock:
        task = asyncio.create_task(service.handle_admin(method, params))
        await asyncio.sleep(0)
        assert not task.done()
        sessions.active_scope = replace(sessions.active_scope, generation=2)
    with pytest.raises(AdminProtocolError) as error:
        await task
    assert error.value.code == "account_changed"
    assert sessions.active_client is client
    assert service._account_integrations_ready
    assert not service._media_sessions


@pytest.mark.asyncio
async def test_activity_target_cannot_leak_from_retired_inventory(tmp_path, monkeypatch):
    service, _, sessions = service_fixture(tmp_path)
    await service.handle_admin("owner_state", {})
    assert "42" in service._owner_admin.control_backend._present
    sessions.active_scope = replace(sessions.active_scope, generation=2)

    async def reject(method, params):
        raise AdminProtocolError("bad_params", "Old camera cannot be controlled")

    monkeypatch.setattr(service, "_dispatch_admin", reject)
    with pytest.raises(AdminProtocolError):
        await service.handle_admin("camera_control", {"camera_id": "42"})
    entry = service._owner_activity.snapshot()["entries"][-1]
    assert entry["camera_id"] is None
    assert entry["outcome"] == "rejected"


@pytest.mark.asyncio
async def test_many_long_device_names_respect_wire_budget(tmp_path):
    cameras = [Camera(str(index)) for index in range(256)]
    for camera in cameras:
        camera.name = "\u4e00" * 256
    service, client, sessions = service_fixture(tmp_path, cameras=cameras)
    for _ in range(256):
        service._owner_activity.record(
            "owner", "configuration_set", "accepted", expected_scope=sessions.active_scope
        )
    state = await service.handle_admin("owner_state", {})
    assert (
        len(json.dumps(state, ensure_ascii=True, separators=(",", ":")).encode())
        <= MAX_OWNER_STATE_BYTES
    )
    assert state["truncated"]
    assert client.submissions == 0
