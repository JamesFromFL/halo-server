"""Selection persistence and native admission remain one owned operation."""

import asyncio
import copy
import threading
from dataclasses import asdict
from uuid import uuid4

import pytest
from test_homekit_broker import request
from test_native_service import Camera, service_fixture

from halo_server.homekit.ipc import validate_direction
from halo_server.homekit.models import CameraPolicy
from halo_server.homekit.registry import RegistryError


class Peer:
    def __init__(self):
        self.messages = []
        self.bulk_channel = None

    async def send(self, kind, payload, **_kwargs):
        self.messages.append((kind, copy.deepcopy(payload)))


async def selected_bridge(tmp_path, *, count=1):
    cameras = [Camera(str(index)) for index in range(count)]
    service, client, sessions = service_fixture(tmp_path, cameras=cameras)
    service._main_loop = asyncio.get_running_loop()
    bridge = service._homekit_bridge
    await bridge._load_registry()
    source = service._homekit_backend.inventory()
    registry = bridge._store.set_selected(
        source.profile_id,
        [camera.scope.camera_id for camera in source.cameras],
        expected_revision=bridge._registry.revision,
    )
    bridge._registry = registry
    bridge._updates.clear()
    boot = str(uuid4())
    bridge._broker.runtime = Peer()
    await bridge._broker.ready(boot)
    scope = await service.homekit.update_policy(
        boot,
        service.homekit.inventory().cameras[0].scope,
        CameraPolicy(synchronized=True, streaming=True, controls=True),
    )
    return bridge, boot, scope, service, client, sessions


@pytest.mark.asyncio
async def test_cancelled_selection_joins_disk_commit_cache_update_and_authority_fence(
    tmp_path, monkeypatch
):
    bridge, boot, _, service, client, _ = await selected_bridge(tmp_path)
    before = bridge._registry
    inventory = bridge._backend.inventory()
    entered, release = threading.Event(), threading.Event()
    real_select = bridge._store.set_selected

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return real_select(*args, **kwargs)

    monkeypatch.setattr(bridge._store, "set_selected", blocked)
    selecting = asyncio.create_task(
        bridge.select(inventory.profile_id, inventory.account_generation, [], before.revision)
    )
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        selecting.cancel()
        await asyncio.sleep(0)
        assert not selecting.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(selecting, 2)
        saved = bridge._store.load()
        assert saved.revision == before.revision + 1
        assert saved.selected(inventory.profile_id) == ()
        assert bridge._registry == saved and bridge._updates.is_set()
        assert service.homekit.boot_id is None
        assert service.homekit.inventory().cameras == ()
        assert client.submissions == 0
    finally:
        release.set()
        await asyncio.gather(selecting, return_exceptions=True)
        await bridge._broker.retired(boot)


@pytest.mark.asyncio
async def test_uncertain_post_replace_selection_save_reloads_and_fences_cached_authority(
    tmp_path, monkeypatch
):
    bridge, boot, _, service, client, _ = await selected_bridge(tmp_path)
    before = bridge._registry
    inventory = bridge._backend.inventory()
    real_select = bridge._store.set_selected

    def saved_then_failed(*args, **kwargs):
        real_select(*args, **kwargs)
        raise RegistryError("Injected directory fsync failure", possibly_saved=True)

    monkeypatch.setattr(bridge._store, "set_selected", saved_then_failed)
    try:
        with pytest.raises(RegistryError) as raised:
            await bridge.select(
                inventory.profile_id, inventory.account_generation, [], before.revision
            )
        assert raised.value.possibly_saved
        saved = bridge._store.load()
        assert saved.revision == before.revision + 1
        assert saved.selected(inventory.profile_id) == ()
        assert bridge._registry == saved and bridge._updates.is_set()
        assert service.homekit.boot_id is None
        assert service.homekit.inventory().cameras == ()
        assert client.submissions == 0
    finally:
        await bridge._broker.retired(boot)


@pytest.mark.asyncio
async def test_configuration_pages_use_one_immutable_registry_snapshot(tmp_path):
    bridge, boot, _, _, _, _ = await selected_bridge(tmp_path, count=33)
    original = bridge._registry
    inventory = bridge._backend.inventory()

    class ChangingPeer:
        def __init__(self):
            self.pages = []

        async def request(self, kind, payload, *, expected):
            assert kind == "accessory_config" and expected == "accessory_config_ack"
            validate_direction(
                {
                    "v": 1,
                    "kind": kind,
                    "boot_id": boot,
                    "request_id": str(uuid4()),
                    "payload": payload,
                },
                "python",
            )
            self.pages.append(copy.deepcopy(payload))
            if len(self.pages) == 1:
                bridge._registry = bridge._store.set_selected(
                    inventory.profile_id,
                    [inventory.cameras[0].scope.camera_id],
                    expected_revision=original.revision,
                )
                await asyncio.sleep(0)
            return {"accepted": True, "code": "ok"}

    peer = ChangingPeer()
    bridge._runtime = peer
    try:
        await bridge._configure()
        assert len(peer.pages) == 2
        assert {page["revision"] for page in peer.pages} == {original.revision}
        assert {page["generation"] for page in peer.pages} == {1}
        assert [page["page_index"] for page in peer.pages] == [0, 1]
        assert {page["total_pages"] for page in peer.pages} == {2}
        entries = [entry for page in peer.pages for entry in page["entries"]]
        assert {entry["accessory_id"] for entry in entries} == {
            entry.accessory_uuid for entry in original.entries
        }
        assert bridge._updates.is_set()
        bridge._updates.clear()
        await bridge._configure()
        assert len(peer.pages) == 3
        latest = peer.pages[-1]
        assert latest["generation"] == 2 and latest["page_index"] == 0
        assert latest["total_pages"] == 1 and len(latest["entries"]) == 1
        assert latest["revision"] == bridge._registry.revision == original.revision + 1
    finally:
        await bridge._broker.retired(boot)


@pytest.mark.asyncio
async def test_deselection_fences_source_writes_before_slow_lease_cleanup(tmp_path, monkeypatch):
    bridge, boot, scope, service, client, _ = await selected_bridge(tmp_path)
    broker = bridge._broker
    session_id = str(uuid4())
    result = await broker.request(
        request(
            boot,
            "lease_request",
            {
                "scope": asdict(scope),
                "session_id": session_id,
                "purpose": "homekit_live",
                "ttl_ms": 3000,
            },
        )
    )
    assert result["status"] == "accepted"
    capture = service._media_sessions[scope.camera_id]
    entered, release = threading.Event(), threading.Event()
    real_release = capture.release

    def blocked_release(lease_id):
        entered.set()
        assert release.wait(3)
        real_release(lease_id)

    monkeypatch.setattr(capture, "release", blocked_release)
    bridge._registry = bridge._store.set_selected(
        scope.profile_id, [], expected_revision=bridge._registry.revision
    )
    refreshing = asyncio.create_task(broker.refresh())
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        assert service.homekit.boot_id is None
        denied = await broker.request(
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
        assert denied["status"] == "rejected"
        assert client.submissions == 0 and client.all_devices[0].light_values == []
        assert not refreshing.done()
        release.set()
        await asyncio.wait_for(refreshing, 2)
        assert not service.homekit.inventory().cameras
        assert not capture.leases
    finally:
        release.set()
        await asyncio.gather(refreshing, return_exceptions=True)
        await broker.retired(boot)
