"""Exercise native RPC authority against the composed Python resource owners."""

import asyncio
from dataclasses import asdict
from uuid import uuid4

import pytest
from test_native_service import Camera, service_fixture

from halo_server.homekit.broker import HomeKitBroker
from halo_server.homekit.ipc import HomeKitProtocolError, encode_frame, validate_reply


class Peer:
    def __init__(self):
        self.messages = []
        self.bulk_channel = None

    async def send(self, kind, payload, request_id=None):
        self.messages.append((kind, payload))


def request(boot, kind, payload):
    return {"v": 1, "kind": kind, "boot_id": boot, "request_id": str(uuid4()), "payload": payload}


async def setup_broker(tmp_path, *, cameras=None, selected=True):
    service, client, sessions = service_fixture(tmp_path, cameras=cameras)
    service._main_loop = asyncio.get_running_loop()
    backend = service._homekit_backend
    broker = HomeKitBroker(
        service.homekit,
        backend,
        selection=lambda: (
            tuple(camera.scope for camera in backend.inventory().cameras) if selected else ()
        ),
    )
    peer = Peer()
    broker.runtime = peer
    boot = str(uuid4())
    await broker.ready(boot)
    return broker, boot, peer, service, client, sessions


async def permit(broker, boot, **flags):
    scope = asdict(broker.controller.inventory().cameras[0].scope)
    policy = dict.fromkeys(
        (
            "synchronized",
            "streaming",
            "live_audio",
            "snapshots",
            "recording",
            "recording_audio",
            "talkback",
            "controls",
        ),
        False,
    )
    policy.update(synchronized=True, **flags)
    envelope = request(boot, "policy_update", {"scope": scope, "policy": policy})
    payload = await broker.request(envelope)
    assert payload["accepted"] is True
    validate_reply(envelope, {**envelope, "kind": "policy_ack", "payload": payload})
    return asdict(broker.controller.inventory().cameras[0].scope)


@pytest.mark.asyncio
async def test_default_selection_never_grants_camera_authority(tmp_path):
    broker, boot, peer, service, client, _ = await setup_broker(tmp_path, selected=False)
    try:
        assert peer.messages[-1][0] == "inventory_reset"
        account = peer.messages[-1][1]["scope"]
        page = await broker.request(
            request(boot, "inventory_request", {"scope": account, "revision": 0, "cursor": None})
        )
        assert page["total"] == 0 and page["complete"]
        assert not service.homekit.inventory().cameras
        assert client.submissions == 0 and not service._media_sessions
    finally:
        await broker.retired(boot)


@pytest.mark.asyncio
async def test_inventory_pages_obey_encoded_bytes_and_retire_cursor_on_change(tmp_path):
    cameras = [Camera(str(index) + "\u4e00" * 120) for index in range(40)]
    for camera in cameras:
        camera.name = "\u4e00" * 256
        camera.model = camera.kind = "\u4e00" * 128
    broker, boot, peer, _, _, _ = await setup_broker(tmp_path, cameras=cameras)
    try:
        account = peer.messages[-1][1]["scope"]
        cursor, revision, seen = None, 0, []
        while True:
            envelope = request(
                boot,
                "inventory_request",
                {"scope": account, "revision": revision, "cursor": cursor},
            )
            payload = await broker.request(envelope)
            frame = {**envelope, "kind": "inventory_page", "payload": payload}
            validate_reply(envelope, frame)
            assert len(encode_frame(frame)) <= 65540
            assert len(payload["devices"]) < 32  # byte cap wins over count cap
            seen += [camera["scope"]["camera_id"] for camera in payload["devices"]]
            cursor, revision = payload["next_cursor"], payload["revision"]
            if cursor is None:
                break
        assert len(set(seen)) == 40
        first = await broker.request(
            request(boot, "inventory_request", {"scope": account, "revision": 0, "cursor": None})
        )
        cameras[0].name = "renamed"
        result = await broker.request(
            request(
                boot,
                "inventory_request",
                {"scope": account, "revision": first["revision"], "cursor": first["next_cursor"]},
            )
        )
        assert result["kind"] == "error" and result["payload"]["code"] == "stale-scope"
    finally:
        await broker.retired(boot)


@pytest.mark.asyncio
async def test_policy_echo_discovers_new_generation_and_control_has_no_replay(tmp_path):
    broker, boot, _, _, client, _ = await setup_broker(tmp_path)
    try:
        old = broker.controller.inventory().cameras[0].scope
        scope = await permit(broker, boot, controls=True)
        assert scope["privacy_generation"] > old.privacy_generation
        operation = request(
            boot,
            "control_request",
            {"scope": scope, "operation_id": str(uuid4()), "control": "light", "enabled": True},
        )
        assert (await broker.request(operation))["status"] == "accepted"
        replay = await broker.request({**operation, "request_id": str(uuid4())})
        assert replay["status"] == "rejected" and replay["code"] == "conflict"
        assert client.all_devices[0].light_values == [True]
    finally:
        await broker.retired(boot)


@pytest.mark.asyncio
async def test_live_deadline_releases_capture_without_waiting_for_peer(tmp_path):
    broker, boot, _, service, _, _ = await setup_broker(tmp_path)
    try:
        scope = await permit(broker, boot, streaming=True)
        identity = str(uuid4())
        payload = await broker.request(
            request(
                boot,
                "lease_request",
                {"scope": scope, "session_id": identity, "purpose": "homekit_live", "ttl_ms": 20},
            )
        )
        assert payload["status"] == "accepted"
        capture = service._media_sessions["42"]
        for _ in range(30):
            if not capture.leases:
                break
            await asyncio.sleep(0.01)
        assert not capture.leases
        renewal = await broker.request(
            request(boot, "lease_renew", {"scope": scope, "session_id": identity, "ttl_ms": 30000})
        )
        assert renewal["status"] == "rejected"
    finally:
        await broker.retired(boot)


@pytest.mark.asyncio
async def test_old_boot_and_replaced_account_never_enter_ring(tmp_path):
    broker, boot, _, _, client, sessions = await setup_broker(tmp_path)
    scope = await permit(broker, boot, controls=True)
    operation = request(
        str(uuid4()),
        "control_request",
        {"scope": scope, "operation_id": str(uuid4()), "control": "light", "enabled": True},
    )
    try:
        with pytest.raises(HomeKitProtocolError):
            await broker.request(operation)
        sessions.active_client = None
        result = await broker.request({**operation, "boot_id": boot})
        assert result["status"] == "rejected" and result["code"] == "stale-scope"
        assert client.submissions == 0
    finally:
        await broker.retired(boot)


@pytest.mark.asyncio
async def test_snapshot_dimensions_never_silently_return_unprocessed_ring_image(tmp_path):
    broker, boot, _, _, client, _ = await setup_broker(tmp_path)
    try:
        scope = await permit(broker, boot, snapshots=True)
        result = await broker.request(
            request(
                boot,
                "snapshot_request",
                {
                    "scope": scope,
                    "job_id": str(uuid4()),
                    "width": 640,
                    "height": 480,
                    "reason": "explicit",
                },
            )
        )
        assert result["status"] == "rejected" and result["code"] == "not-ready"
        assert result["transfer_id"] is None and client.submissions == 0
    finally:
        await broker.retired(boot)


@pytest.mark.asyncio
async def test_duplicate_lease_cannot_reaccept_revoked_handle_or_overstate_ttl(tmp_path):
    from halo_server.homekit.models import CameraPolicy

    broker, boot, _, _, _, _ = await setup_broker(tmp_path)
    try:
        scope = await permit(broker, boot, streaming=True)
        operation = request(
            boot,
            "lease_request",
            {
                "scope": scope,
                "session_id": str(uuid4()),
                "purpose": "homekit_live",
                "ttl_ms": 30000,
            },
        )
        assert (await broker.request(operation))["status"] == "accepted"
        resource = broker._resources[operation["payload"]["session_id"]]
        resource.deadline = asyncio.get_running_loop().time() + 0.2
        duplicate = await broker.request({**operation, "request_id": str(uuid4())})
        assert duplicate["status"] == "accepted" and 0 < duplicate["ttl_ms"] <= 200
        await broker.controller.update_policy(boot, resource.scope, CameraPolicy(synchronized=True))
        duplicate = await broker.request({**operation, "request_id": str(uuid4())})
        assert duplicate["status"] == "rejected"
        broker._resources.pop(operation["payload"]["session_id"])
        assert not broker._current(resource)
    finally:
        await broker.retired(boot)


@pytest.mark.asyncio
async def test_cleanup_failure_fences_new_rpc_instead_of_only_reporting_status(tmp_path):
    broker, boot, _, _, _, _ = await setup_broker(tmp_path)
    try:
        broker.cleanup_failed = True
        with pytest.raises(HomeKitProtocolError, match="cleanup"):
            await broker.request(
                request(
                    boot,
                    "inventory_request",
                    {
                        "scope": broker._account,
                        "revision": 0,
                        "cursor": None,
                    },
                )
            )
    finally:
        broker.cleanup_failed = False
        await broker.retired(boot)


@pytest.mark.asyncio
async def test_stopped_speaker_id_cannot_be_reused_by_late_pcm_operation(tmp_path):
    broker, boot, _, service, _, _ = await setup_broker(tmp_path)
    try:
        scope = await permit(broker, boot, talkback=True)
        identity = str(uuid4())
        acquire = request(
            boot, "talkback_request", {"scope": scope, "session_id": identity, "action": "acquire"}
        )
        assert (await broker.request(acquire))["status"] == "accepted"
        stop = request(
            boot, "talkback_request", {"scope": scope, "session_id": identity, "action": "stop"}
        )
        assert (await broker.request(stop))["status"] == "stopped"
        repeat = await broker.request({**acquire, "request_id": str(uuid4())})
        assert repeat["status"] == "rejected" and repeat["code"] == "conflict"
        assert not service._media_sessions["42"].leases
    finally:
        await broker.retired(boot)


@pytest.mark.asyncio
async def test_refresh_cannot_reattach_retired_boot_over_a_new_adapter(tmp_path):
    from halo_server.account_scope import AccountScope

    broker, old_boot, _, service, _, sessions = await setup_broker(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    original_close = broker._close_resources

    async def delayed_close():
        entered.set()
        await release.wait()
        await original_close()

    broker._close_resources = delayed_close
    sessions.active_scope = AccountScope(str(uuid4()), 2)
    service._media_scope = sessions.active_scope
    refresh = asyncio.create_task(broker.refresh())
    await asyncio.wait_for(entered.wait(), 1)
    retire = asyncio.create_task(broker.retired(old_boot))
    await asyncio.sleep(0)
    assert broker.boot_id is None
    new_boot = str(uuid4())
    ready = asyncio.create_task(broker.ready(new_boot))
    release.set()
    await asyncio.wait_for(asyncio.gather(refresh, retire, ready), 3)
    try:
        assert broker.boot_id == broker.controller.boot_id == new_boot
        assert broker.controller.inventory().profile_id == sessions.active_scope.profile_id
        assert broker.controller.inventory().account_generation == 2
    finally:
        await broker.retired(new_boot)


@pytest.mark.asyncio
async def test_duplicate_pending_acquire_cannot_close_the_first_owners_handle(tmp_path):
    broker, boot, _, service, _, _ = await setup_broker(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    backend = service._homekit_backend
    original = backend.acquire_live

    async def delayed(scope, *, admission):
        entered.set()
        await release.wait()
        return await original(scope, admission=admission)

    backend.acquire_live = delayed
    try:
        scope = await permit(broker, boot, streaming=True)
        operation = request(
            boot,
            "lease_request",
            {
                "scope": scope,
                "session_id": str(uuid4()),
                "purpose": "homekit_live",
                "ttl_ms": 30000,
            },
        )
        first = asyncio.create_task(broker.request(operation))
        await asyncio.wait_for(entered.wait(), 1)
        duplicate = await broker.request({**operation, "request_id": str(uuid4())})
        assert duplicate["status"] == "rejected"
        release.set()
        assert (await first)["status"] == "accepted"
        resource = broker._resources[operation["payload"]["session_id"]]
        assert broker._current(resource) and resource.handle.is_current()
        assert len(service._media_sessions["42"].leases) == 1
    finally:
        release.set()
        await broker.retired(boot)
