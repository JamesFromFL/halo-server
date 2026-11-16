"""Privacy DTOs stay typed and account-bound across private owner IPC."""

import copy
from uuid import uuid4

import pytest
from test_native_service import service_fixture

from halo_server.api.admin import AdminServer
from halo_server.owner_client import AdminClient, AdminClientError


def privacy_page():
    return {
        "profile_id": str(uuid4()),
        "account_generation": 3,
        "available": True,
        "cameras": [
            {
                "camera_id": "camera-a",
                "revision": 0,
                "mode": "ring",
                "source_fresh": True,
                "ring_live_allowed": True,
                "ring_recording_allowed": False,
                "ring_snapshot_allowed": False,
                "effective_live_allowed": True,
                "effective_recording_allowed": False,
                "effective_snapshot_allowed": False,
                "ring_audio_allowed": True,
                "effective_audio_allowed": True,
                "reason": "ring_motion_disabled",
            }
        ],
    }


@pytest.mark.asyncio
async def test_privacy_private_ipc_roundtrip(tmp_path):
    page, calls = privacy_page(), []

    async def handle(method, params):
        calls.append((method, params))
        if method == "camera_privacy_list":
            assert params == {}
            return page
        assert method == "camera_privacy_set"
        return {key: value for key, value in params.items() if key != "expected_revision"} | {
            "revision": 1,
            "status": "completed",
        }

    server = AdminServer(tmp_path / "owner.sock", handle)
    await server.start()
    try:
        client = AdminClient(server.socket_path)
        assert await client.camera_privacy_list() == page
        result = await client.camera_privacy_set(page["profile_id"], 3, "camera-a", 0, "homekit")
        assert result["mode"] == "homekit" and result["revision"] == 1
        assert len(calls) == 2
        assert calls[-1][1] == {
            "profile_id": page["profile_id"],
            "account_generation": 3,
            "camera_id": "camera-a",
            "expected_revision": 0,
            "mode": "homekit",
        }
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_real_service_privacy_ipc_changes_only_local_preference(tmp_path):
    service, ring, _ = service_fixture(tmp_path)
    server = AdminServer(tmp_path / "owner.sock", service.handle_admin)
    await server.start()
    try:
        client = AdminClient(server.socket_path)
        page = await client.camera_privacy_list()
        camera = page["cameras"][0]
        assert page["available"] and camera["source_fresh"]
        assert camera["mode"] == "ring" and camera["revision"] == 0
        saved = await client.camera_privacy_set(
            page["profile_id"], page["account_generation"], camera["camera_id"], 0, "homekit"
        )
        assert saved["revision"] == 1 and saved["mode"] == "homekit"
        assert (await client.camera_privacy_list())["cameras"][0]["mode"] == "homekit"
        with pytest.raises(AdminClientError) as error:
            await client.camera_privacy_set(
                page["profile_id"], page["account_generation"], camera["camera_id"], 0, "ring"
            )
        assert error.value.code == "configuration_conflict"
        assert ring.submissions == 0
        assert ring.all_devices[0].light_values == []
        assert not service._media_sessions
    finally:
        await server.stop()
        await service._owner_admin.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"mode": []},
        {"revision": True},
        {"ring_live_allowed": "yes"},
        {"effective_live_allowed": None},
        {"source_fresh": 1},
        {"reason": []},
        {"reason": "arbitrary upstream text"},
        {"camera_id": ""},
    ],
)
async def test_privacy_view_rejects_ambiguous_types(tmp_path, change):
    page = privacy_page()
    page["cameras"][0].update(change)
    client = AdminClient(tmp_path / "unused.sock")

    async def read(*args, **kwargs):
        return copy.deepcopy(page)

    client._object = read
    with pytest.raises(AdminClientError, match="privacy view"):
        await client.camera_privacy_list()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"profile_id": str(uuid4())},
        {"account_generation": 2},
        {"camera_id": "camera-b"},
        {"mode": "ring"},
        {"revision": 9},
        {"revision": True},
        {"status": "pending"},
    ],
)
async def test_privacy_write_rejects_wrong_scope_or_unconfirmed_save(tmp_path, change):
    profile = str(uuid4())
    result = {
        "profile_id": profile,
        "account_generation": 3,
        "camera_id": "camera-a",
        "mode": "homekit",
        "revision": 1,
        "status": "completed",
    } | change
    client = AdminClient(tmp_path / "unused.sock")

    async def read(*args, **kwargs):
        return result

    client._object = read
    with pytest.raises(AdminClientError, match="privacy outcome"):
        await client.camera_privacy_set(profile, 3, "camera-a", 0, "homekit")


@pytest.mark.asyncio
async def test_privacy_write_does_not_confuse_boolean_with_account_generation(tmp_path):
    profile = str(uuid4())
    client = AdminClient(tmp_path / "unused.sock")

    async def read(*args, **kwargs):
        return {
            "profile_id": profile,
            "account_generation": True,
            "camera_id": "camera-a",
            "mode": "homekit",
            "revision": 1,
            "status": "completed",
        }

    client._object = read
    with pytest.raises(AdminClientError, match="privacy outcome"):
        await client.camera_privacy_set(profile, 1, "camera-a", 0, "homekit")
