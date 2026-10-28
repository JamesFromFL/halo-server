"""Pull-driven recording lifecycle, pre-event claims and final privacy fences."""

import asyncio
from dataclasses import replace
from fractions import Fraction
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_homekit_controller import POLICY, configured

from halo_server.homekit.models import HomeKitOperationError
from halo_server.media import homekit_recording as module
from halo_server.media.h264 import VideoConfiguration
from halo_server.media.homekit_recording import HomeKitRecordingManager
from halo_server.media.recording import RecordingAudioConfiguration, RecordingConfiguration


def configuration(*, audio=False):
    return RecordingConfiguration(
        VideoConfiguration(1280, 720, 15, "main", "4.0", 2000),
        RecordingAudioConfiguration() if audio else None,
    )


def packet(kind, sequence, payload=b"native-output", seconds=2):
    return SimpleNamespace(
        kind=kind, sequence=sequence, payload=payload, duration_seconds=Fraction(seconds)
    )


class Capture:
    def __init__(self, *, ready=True):
        self.retired = self.cleanup_failed = self.closed = False
        self.fail_close = False
        self.snapshot_state = SimpleNamespace(
            ready=ready,
            reduced_reason=None if ready else "cold_reduced_history",
            timing_mode="receipt_estimate",
            samples=("a", "b", "c"),
            cursor=3,
            video_sps=b"sps",
            video_pps=b"pps",
            source_clocks=(),
            capture_epoch=1,
            config_epoch=1,
        )
        self.reads = 0

    @property
    def current_epochs(self):
        return self.snapshot_state.capture_epoch, self.snapshot_state.config_epoch

    @property
    def current_cursor(self):
        return self.snapshot_state.cursor

    def snapshot(self, **kwargs):
        return self.snapshot_state

    async def read_since(self, cursor, **kwargs):
        self.reads += 1
        await asyncio.sleep(0.001)
        return SimpleNamespace(**(vars(self.snapshot_state) | {"samples": ()}))

    async def close(self):
        self.retired = True
        if self.fail_close:
            raise RuntimeError("Injected source cleanup failure")
        self.closed = True


class Captures:
    cleanup_failures = 0

    def __init__(self):
        self.capture = Capture()
        self.opens = 0
        self.entered = asyncio.Event()
        self.gate = None
        self.ignore_cancel = False

    @property
    def pending_resources(self):
        return int(not self.capture.closed and bool(self.opens))

    def reopen(self):
        if self.pending_resources:
            raise HomeKitOperationError("cleanup_pending")

    async def open(self, *args, **kwargs):
        self.opens += 1
        self.entered.set()
        if self.gate is not None:
            try:
                await self.gate.wait()
            except asyncio.CancelledError:
                if not self.ignore_cancel:
                    raise
                await self.gate.wait()
        return self.capture

    async def retired(self, boot):
        if self.opens:
            await self.capture.close()

    async def refresh(self):
        pass

    async def close(self):
        await self.retired(None)


class Worker:
    def __init__(self, *args, valid, **kwargs):
        self.valid = valid
        self.fed = []
        self.closed = False
        self.fail_close = False
        self.feed_hook = None
        self.finish_output = (packet("fragment", 2),)

    async def feed(self, sample):
        self.fed.append(sample)
        if self.feed_hook is not None:
            await self.feed_hook()
        return (
            (packet("init", 0),)
            if len(self.fed) == 1
            else (packet("fragment", 1),)
            if len(self.fed) == 2
            else ()
        )

    async def finish(self):
        return self.finish_output

    async def close(self):
        if self.fail_close:
            raise RuntimeError("Injected reap failure")
        self.closed = True


async def setup(*, count=1, audio=False):
    controller, backend, boot, scopes = await configured(count)
    scopes = tuple(
        [
            await controller.update_policy(
                boot, scope, replace(POLICY, recording=True, recording_audio=audio)
            )
            for scope in scopes
        ]
    )
    captures, workers = Captures(), []

    def create(*args, **kwargs):
        worker = Worker(*args, **kwargs)
        workers.append(worker)
        return worker

    manager = HomeKitRecordingManager(
        controller, capture_manager=captures, worker_factory=create, clock=lambda: 100.0
    )
    manager.ready = True
    return manager, controller, boot, scopes, captures, workers


async def start(manager, boot, scope, **kwargs):
    return await manager.open(boot, scope, str(uuid4()), configuration(**kwargs))


@pytest.mark.asyncio
async def test_recording_is_pull_driven_and_normal_eos_retires_every_owner():
    manager, controller, boot, (scope,), captures, workers = await setup()
    try:
        stream = await start(manager, boot, scope)
        assert stream.history_ready and stream.timing_mode == "receipt_estimate"
        assert workers[0].fed == []
        assert (await anext(stream)).kind == "init"
        await asyncio.sleep(0)
        assert workers[0].fed == ["a"]  # No producer task or fragment queue.
        assert (await anext(stream)).sequence == 1
        stream.finish()
        assert (await anext(stream)).sequence == 2
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
        assert not manager.pending_resources
        assert workers[0].closed and captures.capture.closed
    finally:
        await manager.close()
        await controller.detach()


@pytest.mark.asyncio
async def test_full_preroll_never_waits_after_event_to_invent_past_footage():
    manager, controller, boot, (scope,), captures, workers = await setup()
    captures.capture.snapshot_state.ready = False
    captures.capture.snapshot_state.reduced_reason = "cold_reduced_history"
    try:
        with pytest.raises(HomeKitOperationError, match="unavailable") as error:
            await start(manager, boot, scope)
        assert error.value.code == "prebuffer_unavailable"
        assert not workers and captures.capture.reads == 0
        assert not manager.pending_resources
    finally:
        await manager.close()
        await controller.detach()


@pytest.mark.asyncio
async def test_reduced_capture_requires_explicit_trusted_opt_in():
    manager, controller, boot, (scope,), captures, workers = await setup()
    captures.capture.snapshot_state.ready = False
    captures.capture.snapshot_state.reduced_reason = "cold_reduced_history"
    try:
        stream = await manager.open(boot, scope, str(uuid4()), configuration(), allow_reduced=True)
        assert not stream.history_ready and stream.reduced_reason == "cold_reduced_history"
        assert (await anext(stream)).kind == "init"
        await stream.close()
    finally:
        await manager.close()
        await controller.detach()


@pytest.mark.asyncio
async def test_recording_audio_permission_is_independent_of_live_audio():
    manager, controller, boot, (scope,), captures, workers = await setup()
    try:
        with pytest.raises(HomeKitOperationError) as error:
            await start(manager, boot, scope, audio=True)
        assert error.value.code == "policy_denied"
        assert not captures.opens and not workers
    finally:
        await manager.close()
        await controller.detach()


@pytest.mark.asyncio
async def test_privacy_change_between_native_output_and_dequeue_rejects_bytes():
    manager, controller, boot, (scope,), captures, workers = await setup()
    try:
        stream = await start(manager, boot, scope)

        async def revoke():
            controller.fence_camera(boot, scope)

        workers[0].feed_hook = revoke
        with pytest.raises(HomeKitOperationError):
            await anext(stream)
        assert not manager.pending_resources and workers[0].closed
    finally:
        await manager.close()
        await controller.detach()


@pytest.mark.asyncio
async def test_failed_worker_retirement_retains_capacity_until_real_retry():
    manager, controller, boot, (scope,), captures, workers = await setup()
    stream = await start(manager, boot, scope)
    workers[0].fail_close = True
    try:
        with pytest.raises(HomeKitOperationError) as error:
            await stream.close()
        assert error.value.code == "cleanup_pending"
        assert manager.pending_resources == manager.cleanup_failures == 1
        with pytest.raises(HomeKitOperationError) as error:
            await start(manager, boot, scope)
        assert error.value.code == "capacity"
        workers[0].fail_close = False
        await stream.close()
        assert not manager.pending_resources and workers[0].closed
    finally:
        workers[0].fail_close = False
        await manager.close()
        await controller.detach()


@pytest.mark.asyncio
async def test_capture_epoch_change_cannot_join_two_clips_under_one_init():
    manager, controller, boot, (scope,), captures, workers = await setup()
    try:
        stream = await start(manager, boot, scope)
        await anext(stream)
        await anext(stream)
        captures.capture.snapshot_state.capture_epoch = 2
        with pytest.raises(HomeKitOperationError) as error:
            await anext(stream)
        assert error.value.code == "source_changed"
        assert not manager.pending_resources
    finally:
        await manager.close()
        await controller.detach()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad", [packet("fragment", 0), packet("init", 4), packet("fragment", 1, seconds=5)]
)
async def test_invalid_fragment_contract_never_reaches_consumer(bad):
    manager, controller, boot, (scope,), captures, workers = await setup()
    try:
        stream = await start(manager, boot, scope)
        if bad.sequence == 1:
            await anext(stream)

        async def corrupt(sample):
            return (bad,)

        workers[0].feed = corrupt
        with pytest.raises(HomeKitOperationError) as error:
            await anext(stream)
        assert error.value.code == "invalid_recording_output"
        assert not manager.pending_resources
    finally:
        await manager.close()
        await controller.detach()


@pytest.mark.asyncio
async def test_cancelled_open_keeps_late_source_owned_until_it_really_closes(monkeypatch):
    monkeypatch.setattr(module, "CLEANUP_SECONDS", 0.02)
    manager, controller, boot, (scope,), captures, workers = await setup()
    captures.gate = asyncio.Event()
    captures.ignore_cancel = True
    task = asyncio.create_task(start(manager, boot, scope))
    await captures.entered.wait()
    task.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await task
        assert manager.pending_resources == 1 and manager.cleanup_failures == 1
        captures.gate.set()
        for _ in range(30):
            if all(job.acquiring.done() for job in manager._jobs.values()):
                break
            await asyncio.sleep(0.001)
        await manager.refresh()
        assert captures.capture.closed and not manager.pending_resources
        assert not workers
    finally:
        captures.gate.set()
        await manager.close()
        await controller.detach()


@pytest.mark.asyncio
async def test_lifetime_reaper_retires_paused_consumer_without_more_pulls(monkeypatch):
    monkeypatch.setattr(module, "LIFETIME_SECONDS", 0.02)
    manager, controller, boot, (scope,), captures, workers = await setup()
    try:
        await start(manager, boot, scope)
        async with asyncio.timeout(1):
            while manager.pending_resources:
                await asyncio.sleep(0.005)
        assert captures.capture.closed and workers[0].closed
    finally:
        await manager.close()
        await controller.detach()


@pytest.mark.asyncio
async def test_failed_native_qualification_rejects_before_any_capture():
    manager, controller, boot, (scope,), captures, workers = await setup()
    manager.ready = False
    try:
        with pytest.raises(HomeKitOperationError) as error:
            await start(manager, boot, scope)
        assert error.value.code == "not_ready"
        assert not captures.opens and not workers and not manager.pending_resources
    finally:
        await manager.close()
        await controller.detach()


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["capture_epoch", "config_epoch"])
async def test_source_change_fences_already_staged_fragment(field):
    manager, controller, boot, (scope,), captures, workers = await setup()
    try:
        stream = await start(manager, boot, scope)
        await anext(stream)
        job = stream._job
        job.output.append(packet("fragment", 1))
        setattr(captures.capture.snapshot_state, field, 2)
        with pytest.raises(HomeKitOperationError) as error:
            await anext(stream)
        assert error.value.code == "source_changed"
        assert not manager.pending_resources and workers[0].closed
    finally:
        await manager.close()
        await controller.detach()


@pytest.mark.asyncio
async def test_concurrent_pull_rejects_extra_work_and_cancel_retires_blocked_input():
    manager, controller, boot, (scope,), captures, workers = await setup()
    entered, release = asyncio.Event(), asyncio.Event()
    try:
        stream = await start(manager, boot, scope)

        async def blocked():
            entered.set()
            await release.wait()

        workers[0].feed_hook = blocked
        pulling = asyncio.create_task(anext(stream))
        await entered.wait()
        with pytest.raises(HomeKitOperationError) as error:
            await anext(stream)
        assert error.value.code == "busy"
        pulling.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pulling
        assert workers[0].closed and captures.capture.closed
        assert not manager.pending_resources
    finally:
        release.set()
        await manager.close()
        await controller.detach()


@pytest.mark.asyncio
async def test_clean_stop_can_requalify_and_rearm_without_replacing_capture_owner(monkeypatch):
    import halo_server.media.recording as codec

    async def qualified():
        return codec.RecordingCapabilities(True)

    monkeypatch.setattr(codec, "recording_capabilities", qualified)
    manager, controller, boot, (scope,), captures, workers = await setup()
    try:
        stream = await start(manager, boot, scope)
        await stream.close()
        await manager.close()
        assert not manager.ready
        assert (await manager.qualify()).ready
        assert manager.captures is captures
        captures.capture = Capture()
        stream = await start(manager, boot, scope)
        assert (await anext(stream)).kind == "init"
        await stream.close()
    finally:
        await manager.close()
        await controller.detach()


@pytest.mark.asyncio
async def test_failed_cleanup_cannot_be_rearmed_or_bypassed_by_probe(monkeypatch):
    import halo_server.media.recording as codec

    calls = []

    async def qualified():
        calls.append(True)
        return codec.RecordingCapabilities(True)

    monkeypatch.setattr(codec, "recording_capabilities", qualified)
    manager, controller, boot, (scope,), captures, workers = await setup()
    await start(manager, boot, scope)
    workers[0].fail_close = True
    try:
        with pytest.raises(HomeKitOperationError):
            await manager.close()
        result = await manager.qualify()
        assert not result.ready and result.reason == "cleanup_pending"
        assert not manager.ready and manager.pending_resources == 1
        assert not calls
        with pytest.raises(HomeKitOperationError) as error:
            await start(manager, boot, scope)
        assert error.value.code == "closed"
        workers[0].fail_close = False
        await manager.close()
        assert (await manager.qualify()).ready
    finally:
        workers[0].fail_close = False
        await manager.close()
        await controller.detach()


@pytest.mark.asyncio
async def test_stop_during_native_probe_cannot_reopen_closed_owner(monkeypatch):
    import halo_server.media.recording as codec

    entered, release = asyncio.Event(), asyncio.Event()

    async def qualified():
        entered.set()
        await release.wait()
        return codec.RecordingCapabilities(True)

    monkeypatch.setattr(codec, "recording_capabilities", qualified)
    manager, controller, boot, (scope,), captures, workers = await setup()
    probing = asyncio.create_task(manager.qualify())
    try:
        await entered.wait()
        await manager.close()
        release.set()
        result = await probing
        assert not result.ready and result.reason == "closed"
        assert not manager.ready
        with pytest.raises(HomeKitOperationError) as error:
            await start(manager, boot, scope)
        assert error.value.code == "closed"
    finally:
        release.set()
        await asyncio.gather(probing, return_exceptions=True)
        await manager.close()
        await controller.detach()


@pytest.mark.asyncio
async def test_requalification_reserves_probe_before_admitting_new_recordings(monkeypatch):
    import halo_server.media.recording as codec

    entered, release = asyncio.Event(), asyncio.Event()

    async def qualified():
        entered.set()
        await release.wait()
        return codec.RecordingCapabilities(True)

    monkeypatch.setattr(codec, "recording_capabilities", qualified)
    manager, controller, boot, (scope,), captures, workers = await setup()
    probing = asyncio.create_task(manager.qualify())
    try:
        await entered.wait()
        with pytest.raises(HomeKitOperationError) as error:
            await start(manager, boot, scope)
        assert error.value.code == "busy" and not captures.opens
        release.set()
        assert (await probing).ready
    finally:
        release.set()
        await asyncio.gather(probing, return_exceptions=True)
        await manager.close()
        await controller.detach()
