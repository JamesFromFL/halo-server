"""Ring privacy and HAP privacy are independent, revocable admission gates."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_homekit_controller import POLICY, Backend
from test_homekit_snapshots import Worker

from halo_server.homekit.controller import HomeKitController
from halo_server.homekit.models import CameraPolicy, HomeKitOperationError
from halo_server.homekit.service_backend import ServiceHomeKitBackend
from halo_server.media.snapshots import HomeKitSnapshots


async def fixture():
    backend = Backend()
    authority = SimpleNamespace(epoch=1, allowed=True)
    backend.privacy_token = lambda scope: authority.epoch
    backend.permission_allowed = lambda scope, permission: (
        permission == "controls" or authority.allowed
    )

    async def acquire_recording(scope, *, admission, warm=False):
        return await backend.acquire_live(scope, admission=admission)

    backend.acquire_recording = acquire_recording
    controller = HomeKitController(backend)
    boot = str(uuid4())
    inventory = await controller.attach(boot, [backend.cameras[0].scope])
    policy = replace(POLICY, recording=True, recording_audio=True)
    scope = await controller.update_policy(boot, inventory.cameras[0].scope, policy)
    return controller, backend, authority, boot, scope, policy


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "permission",
    ["streaming", "live_audio", "snapshots", "recording", "recording_audio", "talkback"],
)
async def test_ring_denial_is_independent_of_permissive_hap_policy(permission):
    controller, backend, authority, boot, scope, _ = await fixture()
    try:
        authority.allowed = False
        with pytest.raises(HomeKitOperationError) as error:
            controller.authorize(boot, scope, permission)
        assert error.value.code == "policy_denied"
        controller.authorize(boot, scope, "controls")
        assert not backend.resources
    finally:
        await controller.close()


@pytest.mark.asyncio
async def test_upstream_override_cannot_bypass_hap_off_or_disabled_recording():
    controller, backend, authority, boot, scope, _ = await fixture()
    try:
        authority.allowed = True  # Represents the explicitly selected HomeKit owner.
        scope = await controller.update_policy(boot, scope, CameraPolicy(synchronized=True))
        for operation in (
            controller.open_live(boot, scope),
            controller.open_recording(boot, scope),
            controller.open_recording(boot, scope, warm=True),
            controller.snapshot(boot, scope, fresh=False),
            controller.open_speaker(boot, scope),
        ):
            with pytest.raises(HomeKitOperationError) as error:
                await operation
            assert error.value.code == "policy_denied"
        assert not backend.resources
    finally:
        await controller.close()


@pytest.mark.asyncio
async def test_ring_microphone_denial_blocks_audio_without_granting_or_revoking_video():
    controller, backend, _, boot, scope, _ = await fixture()
    backend.permission_allowed = lambda scope, permission: (
        permission not in {"live_audio", "recording_audio"}
    )
    try:
        live = await controller.open_live(boot, scope)
        recording = await controller.open_recording(boot, scope)
        assert live.is_current() and recording.is_current()
        for handle in (live, recording):
            with pytest.raises(HomeKitOperationError) as error:
                await handle.read_audio()
            assert error.value.code == "policy_denied"
            assert handle.is_current()
        controller.authorize(boot, scope, "streaming")
        controller.authorize(boot, scope, "recording")
        controller.authorize(boot, scope, "talkback")
    finally:
        await controller.close()


@pytest.mark.asyncio
async def test_epoch_change_immediately_rejects_cache_and_claims_before_cleanup():
    controller, backend, authority, boot, scope, policy = await fixture()
    snapshots = HomeKitSnapshots(controller, worker_factory=Worker)
    snapshots.ready = True
    try:
        live = await controller.open_live(boot, scope)
        recording = await controller.open_recording(boot, scope)
        warm = await controller.open_recording(boot, scope, warm=True)
        await snapshots.get(boot, scope, str(uuid4()), 320, 240, "periodic")
        assert snapshots.cache_bytes > 0
        authority.epoch += 1
        authority.allowed = False
        assert not any(handle.is_current() for handle in (live, recording, warm))
        with pytest.raises(HomeKitOperationError):
            await snapshots.get(boot, scope, str(uuid4()), 320, 240, "periodic")
        snapshots.refresh()
        assert snapshots.cache_bytes == 0
        inventory = await controller.reconcile()
        assert all(resource.closed for resource in backend.resources)
        next_scope = inventory.cameras[0].scope
        assert next_scope.privacy_generation > scope.privacy_generation
        assert next_scope.source_key == scope.source_key
        with pytest.raises(HomeKitOperationError):
            await controller.open_live(boot, next_scope)

        # Reopening Ring alone cannot resurrect a prior HAP scope or a cached image.
        authority.allowed = True
        authority.epoch += 1
        with pytest.raises(HomeKitOperationError):
            await controller.open_live(boot, scope)
        new_scope = (await controller.reconcile()).cameras[0].scope
        with pytest.raises(HomeKitOperationError):
            await controller.open_live(boot, new_scope)
        new_scope = await controller.update_policy(boot, new_scope, policy)
        assert (await controller.open_live(boot, new_scope)).is_current()
        assert snapshots.cache_bytes == 0
    finally:
        await snapshots.close()
        await controller.close()


@pytest.mark.asyncio
async def test_revoke_then_reopen_before_reconcile_still_retires_old_media():
    controller, backend, authority, boot, scope, _ = await fixture()
    try:
        handle = await controller.open_live(boot, scope)
        authority.epoch += 2  # A complete off/on transition between owner-loop turns.
        assert authority.allowed
        assert not handle.is_current()
        await controller.reconcile()
        assert backend.resources[0].closed
        with pytest.raises(HomeKitOperationError):
            await controller.open_live(boot, scope)
    finally:
        await controller.close()


@pytest.mark.asyncio
async def test_pending_snapshot_cannot_deliver_after_ring_epoch_change():
    controller, backend, authority, boot, scope, _ = await fixture()
    backend.gate = asyncio.Event()
    pending = asyncio.create_task(controller.snapshot(boot, scope, fresh=False))
    try:
        await backend.entered.wait()
        authority.epoch += 1
        backend.gate.set()
        with pytest.raises(HomeKitOperationError):
            await pending
        await controller.reconcile()
        assert controller.pending_resources == 0
    finally:
        backend.gate.set()
        await controller.close()


@pytest.mark.parametrize(
    "permission,purpose",
    [
        ("streaming", "live"),
        ("live_audio", "audio"),
        ("talkback", "talkback"),
        ("snapshots", "snapshot"),
        ("recording", "recording"),
        ("recording_audio", "audio"),
    ],
)
def test_service_backend_explicitly_selects_homekit_privacy_authority(permission, purpose):
    calls = []
    service = SimpleNamespace(
        _camera_privacy_allowed=lambda camera_id, operation, *, homekit: (
            calls.append((camera_id, operation, homekit)) or True
        ),
        _camera_privacy_epoch=lambda camera_id, *, homekit: (camera_id, homekit, 7),
    )
    backend = ServiceHomeKitBackend(service)
    scope = Backend().cameras[0].scope
    assert backend.permission_allowed(scope, permission)
    assert calls == [(scope.camera_id, purpose, True)]
    assert backend.privacy_token(scope) == (scope.camera_id, True, 7)


def test_async_backend_guard_is_not_revived_by_return_to_allowed_state():
    authority = SimpleNamespace(epoch=1, allowed=True)
    service = SimpleNamespace(
        _camera_privacy_allowed=lambda *args, **kwargs: authority.allowed,
        _camera_privacy_epoch=lambda *args, **kwargs: authority.epoch,
    )
    backend = ServiceHomeKitBackend(service)
    backend.scope_current = lambda scope: True
    guard = backend._guard(Backend().cameras[0].scope, lambda: True, "snapshots")
    assert guard()
    authority.epoch += 2
    assert authority.allowed and not guard()
