import asyncio
import threading
from dataclasses import replace
from uuid import uuid4

import pytest

from halo_server.homekit import controller as native
from halo_server.homekit.models import (
    CameraCapabilities,
    CameraPolicy,
    HomeKitOperationError,
    MediaScope,
    NativeCamera,
    NativeInventory,
)
from halo_server.media.buffer import EncodedMediaBuffer, MediaStreamKind

KEYFRAME = b"\x00\x00\x01\x67sps\x00\x00\x01\x68pps\x00\x00\x01\x65idr"
JPEG = b"\xff\xd8image\xff\xd9"
POLICY = CameraPolicy(
    synchronized=True, streaming=True, live_audio=True, snapshots=True, talkback=True, controls=True
)


class Consumer:
    def __init__(self):
        self.buffer = EncodedMediaBuffer()
        self.buffer.append_video(KEYFRAME, 1)
        self.closed = False
        self.closes = 0
        self.close_error = False
        self.close_entered = None
        self.close_release = None

    def is_current(self):
        return not self.closed

    def renew(self):
        return not self.closed

    def close(self):
        self.closes += 1
        if self.close_entered is not None:
            self.close_entered.set()
            assert self.close_release.wait(2)
        if self.close_error:
            raise RuntimeError("sensitive backend details")
        self.closed = True


class Speaker:
    def __init__(self, admission):
        self.admission = admission
        self.payloads = []
        self.closed = False

    async def write(self, payload):
        assert self.admission()
        self.payloads.append(payload)

    async def wait_ready(self, timeout):
        return self.admission()

    async def renew(self):
        return self.admission()

    async def close(self):
        self.closed = True


class Backend:
    def __init__(self, count=1):
        profile = str(uuid4())
        caps = CameraCapabilities(
            video=True,
            audio=True,
            snapshot=True,
            talkback=True,
            light=True,
            siren=True,
            evidence=(
                "sdk:video",
                "halo:audio",
                "halo:snapshot",
                "halo:talkback",
                "sdk:light",
                "sdk:siren",
            ),
        )
        self.cameras = tuple(
            NativeCamera(
                MediaScope(profile, str(i), 1, i, 1),
                f"Camera {i}",
                "doorbots",
                "kind",
                "model",
                caps,
            )
            for i in range(1, count + 1)
        )
        self.profile = profile
        self.resources = []
        self.speakers = []
        self.entered = asyncio.Event()
        self.gate = None
        self.suppress_cancel = False
        self.ignore_admission = False
        self.controls = []
        self.admissions = []
        self.snapshot_result = JPEG

    def inventory(self):
        return NativeInventory(1, self.cameras, True, self.profile, 1)

    def scope_current(self, scope):
        return any(camera.scope.source_key == scope.source_key for camera in self.cameras)

    async def acquire_live(self, scope, *, admission):
        self.admissions.append(admission)
        self.entered.set()
        if self.gate is not None:
            try:
                await self.gate.wait()
            except asyncio.CancelledError:
                if not self.suppress_cancel:
                    raise
                await self.gate.wait()
        if not self.ignore_admission and not admission():
            raise HomeKitOperationError("not_admitted")
        resource = Consumer()
        self.resources.append(resource)
        return resource

    async def snapshot(self, scope, fresh, *, admission):
        self.entered.set()
        if self.gate is not None:
            await self.gate.wait()
        assert admission()
        return self.snapshot_result

    async def open_speaker(self, scope, *, admission):
        assert admission()
        speaker = Speaker(admission)
        self.speakers.append(speaker)
        return speaker

    async def set_control(self, scope, control, on, *, admission):
        if not admission():
            raise HomeKitOperationError("not_admitted")
        self.controls.append((control, on))


async def configured(count=1):
    backend = Backend(count)
    controller = native.HomeKitController(backend)
    boot = str(uuid4())
    await controller.attach(boot, tuple(camera.scope for camera in backend.cameras))
    for camera in controller.inventory().cameras:
        await controller.update_policy(boot, camera.scope, POLICY)
    return (
        controller,
        backend,
        boot,
        tuple(camera.scope for camera in controller.inventory().cameras),
    )


@pytest.mark.asyncio
async def test_attach_defaults_unselected_and_policy_unsynchronized_until_python_sync():
    backend = Backend()
    controller = native.HomeKitController(backend)
    boot = str(uuid4())
    assert (await controller.attach(boot)).cameras == ()
    snapshot = await controller.attach(boot, (backend.cameras[0].scope,))
    scope = snapshot.cameras[0].scope
    with pytest.raises(HomeKitOperationError, match="HomeKit") as error:
        await controller.open_live(boot, scope)
    assert error.value.code == "policy_denied"
    scope = await controller.update_policy(boot, scope, POLICY)
    handle = await controller.open_live(boot, scope)
    assert handle.is_current()
    await controller.close()
    assert backend.resources[0].closed


@pytest.mark.asyncio
async def test_session_id_coalesces_matching_live_but_rejects_other_kind_or_boot():
    controller, backend, boot, (scope,) = await configured()
    session = str(uuid4())
    first = await controller.open_live(boot, scope, session_id=session)
    assert await controller.open_live(boot, scope, session_id=session) is first
    assert len(backend.resources) == 1
    with pytest.raises(HomeKitOperationError) as error:
        await controller.open_speaker(boot, scope, session_id=session)
    assert error.value.code == "session_conflict"
    await controller.attach(str(uuid4()), (scope,))
    assert not first.is_current()
    assert backend.resources[0].closed
    with pytest.raises(HomeKitOperationError):
        await controller.open_live(boot, scope)
    await controller.close()


@pytest.mark.asyncio
async def test_pending_live_claims_count_toward_per_camera_cap_before_backend_returns():
    controller, backend, boot, (scope,) = await configured()
    backend.gate = asyncio.Event()
    calls = [asyncio.create_task(controller.open_live(boot, scope)) for _ in range(2)]
    await backend.entered.wait()
    await asyncio.sleep(0)
    with pytest.raises(HomeKitOperationError) as error:
        await controller.open_live(boot, scope)
    assert error.value.code == "busy"
    assert controller.pending_resources == 2
    backend.gate.set()
    await asyncio.gather(*calls)
    await controller.close()
    assert controller.pending_resources == 0


@pytest.mark.asyncio
async def test_global_live_cap_is_shared_across_selected_cameras():
    controller, backend, boot, scopes = await configured(5)
    for scope in scopes[:4]:
        await controller.open_live(boot, scope)
        await controller.open_live(boot, scope)
    with pytest.raises(HomeKitOperationError) as error:
        await controller.open_live(boot, scopes[-1])
    assert error.value.code == "busy"
    assert len(backend.resources) == 8
    await controller.close()


@pytest.mark.asyncio
async def test_cancelled_late_acquire_remains_owned_then_is_compensated():
    controller, backend, boot, (scope,) = await configured()
    backend.gate = asyncio.Event()
    backend.suppress_cancel = backend.ignore_admission = True
    task = asyncio.create_task(controller.open_live(boot, scope))
    await backend.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert controller.pending_resources == 1
    backend.gate.set()
    for _ in range(100):
        if controller.pending_resources == 0:
            break
        await asyncio.sleep(0.001)
    assert backend.resources[0].closed
    assert controller.pending_resources == 0
    await controller.close()


@pytest.mark.asyncio
async def test_one_cancelled_coalesced_waiter_does_not_cancel_another_session_waiter():
    controller, backend, boot, (scope,) = await configured()
    backend.gate = asyncio.Event()
    session = str(uuid4())
    first = asyncio.create_task(controller.open_live(boot, scope, session_id=session))
    await backend.entered.wait()
    second = asyncio.create_task(controller.open_live(boot, scope, session_id=session))
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    backend.gate.set()
    handle = await second
    assert handle.is_current()
    assert len(backend.resources) == 1
    await controller.close()


@pytest.mark.asyncio
async def test_privacy_revokes_delivery_before_slow_cleanup_completes(monkeypatch):
    controller, backend, boot, (scope,) = await configured()
    handle = await controller.open_live(boot, scope)
    resource = backend.resources[0]
    resource.close_entered, resource.close_release = threading.Event(), threading.Event()
    monkeypatch.setattr(native, "CLEANUP_SECONDS", 0.02)
    update = asyncio.create_task(
        controller.update_policy(boot, scope, CameraPolicy(synchronized=True))
    )
    assert await asyncio.to_thread(resource.close_entered.wait, 1)
    assert not handle.is_current()
    with pytest.raises(HomeKitOperationError) as error:
        await update
    assert error.value.code == "cleanup_pending"
    assert controller.pending_resources == 1
    resource.close_release.set()
    await controller.close()
    assert controller.pending_resources == 0


@pytest.mark.asyncio
async def test_failed_close_retains_reservation_and_blocks_replacement():
    controller, backend, boot, (scope,) = await configured()
    handle = await controller.open_live(boot, scope)
    backend.resources[0].close_error = True
    with pytest.raises(HomeKitOperationError) as error:
        await handle.close()
    assert error.value.code == "cleanup_failed"
    assert controller.pending_resources == controller.cleanup_failures == 1
    with pytest.raises(HomeKitOperationError) as error:
        await controller.open_live(boot, scope)
    assert error.value.code == "cleanup_pending"
    assert backend.resources[0].closes == 1
    with pytest.raises(HomeKitOperationError):
        await controller.close()


@pytest.mark.asyncio
async def test_reconcile_retires_changed_source_but_preserves_unchanged_policy():
    controller, backend, boot, (first_scope, second_scope) = await configured(2)
    first = await controller.open_live(boot, first_scope)
    second = await controller.open_live(boot, second_scope)
    backend.cameras = (
        replace(backend.cameras[0], scope=replace(first_scope, device_generation=99)),
        backend.cameras[1],
    )
    assert not first.is_current()
    assert second.is_current()
    await controller.reconcile()
    assert backend.resources[0].closed
    assert not backend.resources[1].closed
    third = await controller.open_live(boot, second_scope)
    assert third.is_current()
    await controller.close()


@pytest.mark.asyncio
async def test_live_audio_is_filtered_and_read_cursor_advances_past_filtered_packets():
    controller, backend, boot, (scope,) = await configured()
    scope = await controller.update_policy(boot, scope, replace(POLICY, live_audio=False))
    handle = await controller.open_live(boot, scope)
    buffer = backend.resources[0].buffer
    buffer.append_audio(b"opus", 2)
    first = await handle.read(timeout=0)
    assert all(packet.stream is MediaStreamKind.VIDEO for packet in first.packets)
    assert handle._cursor == 2
    assert (await handle.read(timeout=0)).packets == ()
    await controller.close()


@pytest.mark.asyncio
async def test_read_rechecks_scope_after_blocking_buffer_returns():
    controller, backend, boot, (scope,) = await configured()
    handle = await controller.open_live(boot, scope)
    read_entered, read_release = threading.Event(), threading.Event()
    original = backend.resources[0].buffer.read

    def read(**kwargs):
        read_entered.set()
        assert read_release.wait(2)
        return original(**kwargs)

    backend.resources[0].buffer.read = read
    reading = asyncio.create_task(handle.read(timeout=0))
    assert await asyncio.to_thread(read_entered.wait, 1)
    backend.cameras = ()
    read_release.set()
    with pytest.raises(HomeKitOperationError) as error:
        await reading
    assert error.value.code == "authority_changed"
    await controller.close()


@pytest.mark.asyncio
async def test_snapshot_pending_limit_and_privacy_cancellation_drop_bytes():
    controller, backend, boot, (scope,) = await configured()
    backend.gate = asyncio.Event()
    tasks = [asyncio.create_task(controller.snapshot(boot, scope)) for _ in range(2)]
    await backend.entered.wait()
    await asyncio.sleep(0)
    with pytest.raises(HomeKitOperationError) as error:
        await controller.snapshot(boot, scope)
    assert error.value.code == "busy"
    await controller.update_policy(boot, scope, replace(POLICY, snapshots=False))
    result = await asyncio.gather(*tasks, return_exceptions=True)
    assert all(isinstance(item, asyncio.CancelledError) for item in result)
    assert controller.pending_resources == 0
    await controller.close()


@pytest.mark.asyncio
async def test_speaker_pcm_and_retained_admission_reject_privacy_generation_change():
    controller, backend, boot, (scope,) = await configured()
    speaker = await controller.open_speaker(boot, scope)
    assert await speaker.wait_ready(1)
    with pytest.raises(HomeKitOperationError):
        await speaker.write(b"short")
    await speaker.write(b"\x00" * 1920)
    assert len(backend.speakers[0].payloads) == 1
    await controller.update_policy(boot, scope, replace(POLICY, talkback=False))
    assert backend.speakers[0].closed
    assert not backend.speakers[0].admission()
    with pytest.raises(HomeKitOperationError):
        await speaker.write(b"\x00" * 1920)
    await controller.close()


@pytest.mark.asyncio
async def test_control_backend_failure_is_sanitized_uncertain_and_never_replayed():
    controller, backend, boot, (scope,) = await configured()

    async def fail(scope, control, on, *, admission):
        assert admission()
        backend.controls.append((control, on))
        raise HomeKitOperationError("remote_unknown", "safe message")

    backend.set_control = fail
    with pytest.raises(HomeKitOperationError) as error:
        await controller.set_control(boot, scope, "light", True)
    assert error.value.code == "remote_unknown"
    assert error.value.possibly_sent
    assert backend.controls == [("light", True)]
    assert controller.pending_resources == 0
    await controller.close()


@pytest.mark.asyncio
async def test_expired_acquisition_cannot_publish_after_backend_suppresses_timeout(monkeypatch):
    controller, backend, boot, (scope,) = await configured()
    backend.gate = asyncio.Event()
    backend.ignore_admission = backend.suppress_cancel = True
    monkeypatch.setattr(native, "ACQUIRE_SECONDS", 0.01)
    task = asyncio.create_task(controller.open_live(boot, scope))
    await backend.entered.wait()
    await asyncio.sleep(0.02)
    backend.gate.set()
    with pytest.raises(HomeKitOperationError):
        await task
    assert backend.resources[0].closed
    assert controller.pending_resources == 0
    await controller.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["live", "speaker", "snapshot", "control"])
async def test_revocation_releases_task_cancelled_before_first_step(kind):
    controller, backend, boot, (scope,) = await configured()
    operations = {
        "live": lambda: controller.open_live(boot, scope),
        "speaker": lambda: controller.open_speaker(boot, scope),
        "snapshot": lambda: controller.snapshot(boot, scope),
        "control": lambda: controller.set_control(boot, scope, "light", True),
    }
    pending = asyncio.create_task(operations[kind]())
    # The outer operation reserves its claim and schedules the backend task.
    # Revoke before that newly scheduled task gets its first execution step.
    await asyncio.sleep(0)
    assert controller.pending_resources == 1
    assert not backend.resources and not backend.speakers and not backend.controls
    assert not backend.entered.is_set()
    await controller.update_policy(boot, scope, CameraPolicy())
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert controller.pending_resources == 0
    assert not backend.resources and not backend.speakers and not backend.controls
    assert not backend.entered.is_set()
    await controller.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("control", [[], {}, None, 1, True])
async def test_control_rejects_untyped_values_before_reserving_or_backend_call(control):
    controller, backend, boot, (scope,) = await configured()
    with pytest.raises(HomeKitOperationError) as error:
        await controller.set_control(boot, scope, control, True)
    assert error.value.code == "invalid_control"
    assert not backend.controls and controller.pending_resources == 0
    await controller.close()


@pytest.mark.asyncio
async def test_handle_work_is_bounded_before_await_and_close_drains_owned_calls():
    controller, backend, boot, (scope,) = await configured()
    speaker = await controller.open_speaker(boot, scope)
    entered = asyncio.Event()
    gate = asyncio.Event()

    async def delayed_ready(timeout):
        entered.set()
        await gate.wait()
        return True

    backend.speakers[0].wait_ready = delayed_ready
    pending = [asyncio.create_task(speaker.wait_ready(1)) for _ in range(2)]
    await entered.wait()
    await asyncio.sleep(0)
    with pytest.raises(HomeKitOperationError) as error:
        await speaker.wait_ready(1)
    assert error.value.code == "busy"
    await speaker.close()
    results = await asyncio.gather(*pending, return_exceptions=True)
    assert all(isinstance(result, asyncio.CancelledError) for result in results)
    assert backend.speakers[0].closed and controller.pending_resources == 0
    await controller.close()


@pytest.mark.asyncio
async def test_published_speaker_admission_outlives_initial_acquisition_deadline(monkeypatch):
    controller, backend, boot, (scope,) = await configured()
    monkeypatch.setattr(native, "ACQUIRE_SECONDS", 0.01)
    speaker = await controller.open_speaker(boot, scope)
    await asyncio.sleep(0.02)
    assert backend.speakers[0].admission()
    await speaker.write(b"\x00" * 1920)
    assert len(backend.speakers[0].payloads) == 1
    await controller.close()


@pytest.mark.asyncio
async def test_revocation_drains_method_without_joining_caller_finally_close(monkeypatch):
    controller, backend, boot, (scope,) = await configured()
    speaker = await controller.open_speaker(boot, scope)
    entered = asyncio.Event()

    async def delayed_ready(timeout):
        entered.set()
        await asyncio.Event().wait()

    backend.speakers[0].wait_ready = delayed_ready

    async def caller():
        try:
            await speaker.wait_ready(1)
        finally:
            await speaker.close()

    pending = asyncio.create_task(caller())
    await entered.wait()
    monkeypatch.setattr(native, "CLEANUP_SECONDS", 0.05)
    await asyncio.wait_for(
        controller.update_policy(boot, scope, replace(POLICY, talkback=False)), 0.2
    )
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert backend.speakers[0].closed and controller.pending_resources == 0
    await controller.close()


@pytest.mark.asyncio
async def test_failed_close_blocks_same_source_after_boot_rotation():
    controller, backend, boot, (scope,) = await configured()
    handle = await controller.open_live(boot, scope)
    backend.resources[0].close_error = True
    with pytest.raises(HomeKitOperationError):
        await handle.close()
    replacement_boot = str(uuid4())
    with pytest.raises(HomeKitOperationError) as error:
        await controller.attach(replacement_boot, (scope,))
    assert error.value.code == "cleanup_failed"
    current = controller.inventory().cameras[0].scope
    with pytest.raises(HomeKitOperationError) as error:
        await controller.update_policy(replacement_boot, current, POLICY)
    assert error.value.code == "cleanup_failed"
    current = controller.inventory().cameras[0].scope
    with pytest.raises(HomeKitOperationError) as error:
        await controller.open_live(replacement_boot, current)
    assert error.value.code == "cleanup_pending"
    assert len(backend.resources) == backend.resources[0].closes == 1
    with pytest.raises(HomeKitOperationError):
        await controller.close()
