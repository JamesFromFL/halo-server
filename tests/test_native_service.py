"""Composed native admission against the daemon's real ownership boundaries."""

import asyncio
import concurrent.futures
import threading
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_media_session import SPS_PPS_IDR

from halo_server.account_scope import AccountScope
from halo_server.api.plugin import PluginIntercomOpenError
from halo_server.configuration import ServerConfiguration, ServerMode
from halo_server.homekit.models import CameraPolicy, HomeKitOperationError
from halo_server.media.buffer import EncodedMediaBuffer, MediaStreamKind
from halo_server.media.leases import LeasePurpose
from halo_server.media.session import MediaSessionState
from halo_server.paths import ServerPaths
from halo_server.service import HaloService


class Camera:
    family = "stickup_cams"
    kind = "stickup_cam_mini"
    model = "Indoor Camera"
    subscribed = True
    battery_life = None

    def __init__(self, camera_id="42", *, capabilities=("video", "light")):
        self.id = camera_id
        self.name = "Hall\u202e\nCamera"
        self.capabilities = set(capabilities)
        self.light_values = []
        self.snapshot_calls = 0

    def has_capability(self, capability):
        return capability in self.capabilities

    async def async_set_light(self, on):
        self.light_values.append(on)

    async def async_get_snapshot(self, **kwargs):
        self.snapshot_calls += 1
        return b"\xff\xd8fixture\xff\xd9"

    async def generate_async_webrtc_stream(self): ...
    async def on_webrtc_candidate(self): ...
    async def close_webrtc_stream(self): ...
    async def get_ice_servers(self): ...


class Client:
    is_authenticated = True

    def __init__(self, *cameras):
        self.all_devices = list(cameras)
        self.submissions = 0
        self.snapshot_future = None

    def submit(self, coroutine):
        self.submissions += 1
        return asyncio.run_coroutine_threadsafe(coroutine, asyncio.get_running_loop())

    def submit_snapshot_for_device(self, device, *, fresh=False):
        self.submissions += 1
        if self.snapshot_future is not None:
            return self.snapshot_future
        future = concurrent.futures.Future()
        future.set_result(b"\xff\xd8fixture\xff\xd9")
        return future


class Sessions:
    account_email = "account-display-only"

    def __init__(self, client):
        self.active_client = client
        self.active_scope = AccountScope(str(uuid4()), 1)

    @property
    def active_context(self):
        return (self.active_client, self.active_scope) if self.active_client is not None else None

    def logout(self):
        self.active_client = None

    def shutdown(self):
        self.active_client = None


class Capture:
    def __init__(self, client, camera, *, legacy_audio_enabled=True):
        self.legacy_audio_enabled = legacy_audio_enabled
        self.client, self.camera = client, camera
        self.buffer = EncodedMediaBuffer()
        self.acquired = []
        self.leases = set()
        self.frames = []
        self.acquire_entered = threading.Event()
        self.acquire_release = threading.Event()
        self.acquire_release.set()

    def owns_source(self, client, device):
        return self.client is client and self.camera is device

    def acquire(self, purpose, *, lease_id):
        self.acquire_entered.set()
        assert self.acquire_release.wait(3)
        self.acquired.append(purpose)
        self.leases.add(lease_id)

    def release(self, lease_id):
        self.leases.discard(lease_id)

    def renew(self, lease_id):
        return object() if lease_id in self.leases else None

    def is_lease_active(self, lease_id):
        return lease_id in self.leases

    def intercom_generation(self, lease_id):
        return lease_id if lease_id in self.leases else None

    def push_intercom_generation(self, generation, payload, *, admission=None):
        assert generation in self.leases
        self.frames.append(payload)

    def wait_intercom_generation_ready(self, generation, timeout):
        return generation in self.leases

    def snapshot(self):
        return SimpleNamespace(state=MediaSessionState.IDLE, lease_count=len(self.leases))

    def stop_all(self, reason):
        self.leases.clear()


def service_fixture(tmp_path, *, cameras=None, mode=ServerMode.HOMEKIT):
    client = Client(*(cameras if cameras is not None else [Camera()]))
    sessions = Sessions(client)
    service = HaloService(
        ServerPaths(tmp_path / "state", tmp_path / "run"),
        session_manager=sessions,
        configuration=ServerConfiguration(mode=mode),
        media_session_factory=Capture,
    )
    service._media_client = client
    service._media_scope = sessions.active_scope
    service._account_integrations_ready = True
    if service._homekit_bridge is not None:
        service._homekit_bridge._publication_enabled = False
    return service, client, sessions


async def select(service, **policy):
    service._main_loop = asyncio.get_running_loop()
    boot = str(uuid4())
    source = service._homekit_backend.inventory().cameras[0].scope
    selected = await service.homekit.attach(boot, [source])
    scope = await service.homekit.update_policy(
        boot, selected.cameras[0].scope, CameraPolicy(synchronized=True, **policy)
    )
    return boot, scope


def test_inventory_uses_evidence_and_fresh_generations(tmp_path):
    service, client, sessions = service_fixture(tmp_path)
    backend = service._homekit_backend
    first = backend.inventory()
    camera = first.cameras[0]
    assert first.complete
    assert camera.name == "HallCamera"
    assert camera.connection_state is None  # subscribed is not a reachability signal
    assert camera.observed_format is None
    assert camera.capabilities.video
    assert not camera.capabilities.audio
    assert "sdk:video" in camera.capabilities.evidence
    assert "audio" not in repr(camera.capabilities.evidence)
    assert "account-display-only" not in repr(first)
    client.all_devices[0].name = "Renamed"
    assert backend.inventory().cameras[0].scope == camera.scope
    client.all_devices[0] = Camera()
    replacement = backend.inventory().cameras[0].scope
    assert replacement.device_generation > camera.scope.device_generation
    assert not backend.scope_current(camera.scope)
    client.all_devices.clear()
    assert backend.inventory().complete
    client.all_devices.append(Camera())
    assert backend.inventory().cameras[0].scope.device_generation > replacement.device_generation
    sessions.active_scope = AccountScope(str(uuid4()), 2)
    assert backend.inventory().cameras == ()
    assert not backend.scope_current(replacement)


def test_inventory_conflicts_and_truncation_are_incomplete(tmp_path):
    service, client, _ = service_fixture(tmp_path, cameras=[Camera(), Camera(), Camera("safe")])
    inventory = service._homekit_backend.inventory()
    assert not inventory.complete
    assert [camera.scope.camera_id for camera in inventory.cameras] == ["safe"]
    client.all_devices = [Camera(str(index)) for index in range(257)]
    inventory = service._homekit_backend.inventory()
    assert len(inventory.cameras) == 256
    assert not inventory.complete


def test_halo_override_evidence_is_not_labelled_sdk(tmp_path):
    camera = Camera(capabilities=())
    camera.kind = "stickup_cam_mini_v3"
    service, _, _ = service_fixture(tmp_path, cameras=[camera])
    caps = service._homekit_backend.inventory().cameras[0].capabilities
    assert caps.video and caps.snapshot and caps.siren and caps.talkback
    assert set(caps.evidence) == {"halo:video", "halo:snapshot", "halo:siren", "halo:talkback"}


@pytest.mark.parametrize("mode", [ServerMode.COMPATIBILITY, ServerMode.RING_ONLY])
def test_disabled_native_mode_has_no_inventory_authority(tmp_path, mode):
    service, _, _ = service_fixture(tmp_path, mode=mode)
    assert service._homekit_backend.inventory().cameras == ()


@pytest.mark.parametrize("mode", [ServerMode.HOMEKIT, ServerMode.RING_ONLY])
def test_modes_reject_accidental_compatibility_listeners(tmp_path, mode):
    with pytest.raises(ValueError, match="Compatibility listeners"):
        HaloService(
            ServerPaths(tmp_path / "state", tmp_path / "run"),
            configuration=ServerConfiguration(mode=mode),
            rtsp_host="127.0.0.1",
        )


@pytest.mark.asyncio
async def test_native_and_legacy_share_capture_and_revoke_delivery(tmp_path):
    service, _, _ = service_fixture(tmp_path, mode=ServerMode.DUAL)
    boot, scope = await select(service, streaming=True)
    first = await service.homekit.open_live(boot, scope)
    second = await service.homekit.open_live(boot, scope)
    capture, legacy = await service._acquire_rtsp_consumer("42")
    assert len(service._media_sessions) == 1
    assert capture.acquired.count(LeasePurpose.HOMEKIT_LIVE) == 2
    assert capture.acquired.count(LeasePurpose.SCRYPTED_LIVE) == 1
    capture.buffer.append_video(SPS_PPS_IDR, 0)
    capture.buffer.append_audio(b"audio fixture", 0)
    packets = (await first.read(timeout=0)).packets
    assert packets and all(packet.stream is MediaStreamKind.VIDEO for packet in packets)
    assert not (await first.read(timeout=0)).packets  # filtered audio advances cursor
    with pytest.raises(HomeKitOperationError, match="unavailable") as limited:
        await service.homekit.open_live(boot, scope)
    assert limited.value.code == "busy"
    await service.homekit.update_policy(boot, scope, CameraPolicy(synchronized=True))
    assert len(capture.leases) == 1 and legacy in capture.leases
    with pytest.raises(HomeKitOperationError):
        await second.read(timeout=0)
    await service._release_camera_consumer("42", capture, legacy)
    assert not capture.leases
    assert service.homekit.pending_resources == 0
    await service.homekit.detach()


@pytest.mark.asyncio
async def test_revocation_blocks_control_queued_behind_daemon_lock(tmp_path):
    service, client, _ = service_fixture(tmp_path)
    boot, scope = await select(service, controls=True)
    lock = service._camera_control_lock("42")
    await lock.acquire()
    operation = asyncio.create_task(service.homekit.set_control(boot, scope, "light", True))
    try:
        for _ in range(20):
            await asyncio.sleep(0)
            if service.homekit.pending_resources:
                break
        await service.homekit.update_policy(boot, scope, CameraPolicy(synchronized=True))
    finally:
        lock.release()
    await asyncio.gather(operation, return_exceptions=True)
    assert client.submissions == 0
    assert client.all_devices[0].light_values == []
    assert service.homekit.pending_resources == 0


@pytest.mark.asyncio
async def test_snapshot_cannot_cross_device_replacement(tmp_path):
    service, client, _ = service_fixture(tmp_path)
    client.snapshot_future = concurrent.futures.Future()
    boot, scope = await select(service, snapshots=True)
    operation = asyncio.create_task(service.homekit.snapshot(boot, scope))
    for _ in range(20):
        await asyncio.sleep(0)
        if client.submissions:
            break
    assert client.submissions == 1
    client.all_devices[0] = Camera()
    client.snapshot_future.set_result(b"\xff\xd8old camera\xff\xd9")
    with pytest.raises(HomeKitOperationError):
        await operation
    assert service.homekit.pending_resources == 0


@pytest.mark.asyncio
async def test_speaker_exclusion_and_backend_write_admission(tmp_path):
    service, _, _ = service_fixture(tmp_path)
    boot, scope = await select(service, talkback=True)
    speaker = await service.homekit.open_speaker(boot, scope)
    with pytest.raises(PluginIntercomOpenError) as busy:
        await service._open_plugin_intercom("42")
    assert busy.value.code == "intercom_busy"
    assert await speaker.wait_ready(1)
    await speaker.write(bytes(1920))
    capture = service._media_sessions["42"]
    assert len(capture.frames) == 1
    await service.homekit.update_policy(boot, scope, CameraPolicy(synchronized=True))
    with pytest.raises(HomeKitOperationError):
        await speaker.write(bytes(1920))
    assert len(capture.frames) == 1
    assert not capture.leases and not service._intercom_leases
    allowed = True
    raw = await service._open_camera_speaker("42", admission=lambda: allowed)
    allowed = False
    with pytest.raises(RuntimeError, match="authority"):
        await raw.write(bytes(1920))
    assert not await raw.wait_ready(1)
    assert not await raw.renew()
    await raw.close()


@pytest.mark.asyncio
async def test_cancelled_native_acquisition_releases_late_daemon_lease(tmp_path):
    service, _, _ = service_fixture(tmp_path)
    boot, scope = await select(service, streaming=True)
    capture = service._create_media_session("42")
    capture.acquire_release.clear()
    operation = asyncio.create_task(service.homekit.open_live(boot, scope))
    assert await asyncio.to_thread(capture.acquire_entered.wait, 2)
    operation.cancel()
    await asyncio.gather(operation, return_exceptions=True)
    capture.acquire_release.set()
    await service.homekit.detach()
    assert capture.acquired == [LeasePurpose.HOMEKIT_LIVE]
    assert not capture.leases
    assert service.homekit.pending_resources == 0


@pytest.mark.asyncio
async def test_logout_retires_native_handles_and_allows_later_attach(tmp_path):
    service, client, sessions = service_fixture(tmp_path)
    boot, scope = await select(service, streaming=True)
    handle = await service.homekit.open_live(boot, scope)
    capture = service._media_sessions["42"]
    await service._logout()
    assert service.homekit.boot_id is None
    assert not handle.is_current() and not capture.leases
    assert service.homekit.pending_resources == 0
    sessions.active_client = client
    sessions.active_scope = AccountScope(sessions.active_scope.profile_id, 2)
    service._media_client = client
    service._media_scope = sessions.active_scope
    service._account_integrations_ready = True
    new_boot, new_scope = await select(service, streaming=True)
    assert new_boot != boot and new_scope.account_generation == 2
    new_handle = await service.homekit.open_live(new_boot, new_scope)
    await new_handle.close()
    await service.homekit.detach()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["control", "snapshot"])
async def test_ring_loop_queue_rechecks_source_and_privacy_before_sdk_entry(tmp_path, kind):
    from halo_server.ring.client import RingClient

    service, client, _ = service_fixture(tmp_path)
    client.supports_snapshot_admission = True
    client._ring = object()
    client._snapshot_reader = SimpleNamespace(fetch=client.all_devices[0].async_get_snapshot)
    client._async_snapshot_bytes = RingClient._async_snapshot_bytes.__get__(client)
    client.submit_snapshot_for_device = RingClient.submit_snapshot_for_device.__get__(client)
    queued = asyncio.Event()
    jobs = []

    def submit(coroutine):
        future = concurrent.futures.Future()
        jobs.append((coroutine, future))
        queued.set()
        return future

    client.submit = submit
    backend = service._homekit_backend
    scope = backend.inventory().cameras[0].scope
    permitted = True

    def admission():
        return permitted

    operation = (
        backend.set_control(scope, "light", True, admission=admission)
        if kind == "control"
        else backend.snapshot(scope, True, admission=admission)
    )
    task = asyncio.create_task(operation)
    await asyncio.wait_for(queued.wait(), 1)
    permitted = False
    for coroutine, future in jobs:
        try:
            result = await coroutine
        except Exception as exc:
            future.set_exception(exc)
        else:
            future.set_result(result)
    with pytest.raises(HomeKitOperationError):
        await task
    assert client.all_devices[0].light_values == []
    assert client.all_devices[0].snapshot_calls == 0


@pytest.mark.asyncio
async def test_native_cleanup_failure_cannot_prevent_logout(tmp_path):
    from halo_server.api.admin import AdminProtocolError

    service, _, sessions = service_fixture(tmp_path)

    async def failed_detach():
        raise HomeKitOperationError("cleanup_failed")

    service.homekit.detach = failed_detach
    service._recent_events.append({"private": "old account"})
    with pytest.raises(AdminProtocolError) as failure:
        await service._logout()
    assert failure.value.code == "media_cleanup_failed"
    assert sessions.active_client is None
    assert service._media_client is None and service._media_scope is None
    assert not service._recent_events
    assert not service._account_integrations_ready


@pytest.mark.asyncio
async def test_new_device_scope_cannot_reuse_old_shared_capture_before_reconciliation(tmp_path):
    service, client, _ = service_fixture(tmp_path)
    boot, scope = await select(service, streaming=True, talkback=True)
    live = await service.homekit.open_live(boot, scope)
    capture = service._media_sessions["42"]
    client.all_devices[0] = Camera()
    new_boot, new_scope = await select(service, streaming=True, talkback=True)
    assert new_scope.device_generation != scope.device_generation
    assert not live.is_current()
    with pytest.raises(HomeKitOperationError):
        await service.homekit.open_live(new_boot, new_scope)
    with pytest.raises(HomeKitOperationError):
        await service.homekit.open_speaker(new_boot, new_scope)
    with pytest.raises(RuntimeError, match="reconciliation"):
        await service._acquire_rtsp_consumer("42")
    assert capture.acquired == [LeasePurpose.HOMEKIT_LIVE]
    assert not capture.leases
    await service.homekit.detach()


def test_scope_delivery_guard_checks_live_identity_without_rebuilding_metadata(tmp_path):
    service, client, sessions = service_fixture(tmp_path)
    backend = service._homekit_backend
    scope = backend.inventory().cameras[0].scope
    calls = []
    camera = client.all_devices[0]
    original = camera.has_capability
    camera.has_capability = lambda capability: calls.append(capability) or original(capability)
    assert all(backend.scope_current(scope) for _ in range(20))
    assert not calls
    client.is_authenticated = False
    assert not backend.scope_current(scope)
    client.is_authenticated = True
    sessions.active_scope = type(sessions.active_scope)(str(uuid4()), 2)
    service._media_scope = sessions.active_scope
    from dataclasses import replace

    fabricated = replace(scope, profile_id=sessions.active_scope.profile_id, account_generation=2)
    assert not backend.scope_current(fabricated)
