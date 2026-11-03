"""Recording history and authority fixtures; synthetic sources never contact Ring."""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from uuid import uuid4

import pytest
from test_homekit_bulk_cancel import until
from test_homekit_controller import configured
from test_media_session import FakeClient, FakeClock, fake_supervisor

from halo_server.homekit.models import CameraPolicy, HomeKitOperationError
from halo_server.media.buffer import EncodedPacket, MediaStreamKind
from halo_server.media.consumers import BufferedMediaConsumer
from halo_server.media.leases import LeasePurpose, MediaLeaseCoordinator, maximum_session_seconds
from halo_server.media.opus import OriginalOpusBuffer
from halo_server.media.recording_capture import (
    RecordingCaptureManager,
    RecordingHistory,
    RecordingSourceBuffer,
)
from halo_server.media.session import CameraMediaSession

# Minimal Annex-B metadata fixtures. Worker/composed tests independently verify
# native bitstream decode; these fixtures isolate capture history and ownership.
SPS = bytes.fromhex("6742c00bda0507e840000003004000000300a3c50aa8")
PPS = bytes.fromhex("68ce0fc8")
KEY = b"\0\0\1" + SPS + b"\0\0\1" + PPS + b"\0\0\1\x65\x88\x84"
PFRAME = b"\0\0\1\x41\x9a"
OPUS = b"\xf8\xff\xfe"
POLICY = CameraPolicy(synchronized=True, recording=True, recording_audio=True)


def packet(kind, sequence, stamp, receipt, *, epoch=1, key=False):
    return EncodedPacket(
        sequence,
        MediaStreamKind(kind),
        KEY if key else PFRAME if kind == "video" else OPUS,
        stamp,
        receipt,
        capture_epoch=epoch,
    )


def fill(history, now, *, start=0.0, duration=5.2, flat_video=False, audio=True):
    history.configure((1, 1, SPS, PPS, start))
    count = round(duration * 50)
    for index in range(count + 1):
        now[0] = start + index / 50
        if index % 5 == 0:
            history.append(
                packet(
                    "video",
                    index // 5 + 1,
                    0 if flat_video else index * 1800,
                    now[0],
                    key=index % 50 == 0,
                )
            )
        if audio:
            history.append(packet("audio", index + 1, index * 960, now[0]))


def test_history_requires_real_receipt_and_source_pts_coverage_not_configured_duration():
    now = [0.0]
    history = RecordingHistory(audio=True, clock=lambda: now[0])
    fill(history, now, duration=2)
    initial = history.snapshot(event_at=now[0])
    assert not initial.ready and initial.available_audio_seconds == 2
    assert initial.reduced_reason == "audio_history_short"
    # New independent continuous history, not appending a duplicated timeline.
    history = RecordingHistory(audio=True, clock=lambda: now[0])
    fill(history, now)
    full = history.snapshot(event_at=now[0])
    assert full.ready and full.available_video_seconds >= 4 and full.available_audio_seconds >= 4
    assert full.timing_mode == "receipt_estimate"
    assert full.video_sps == SPS and full.video_pps == PPS
    assert full.samples[0].kind == "video" and b"\x65" in full.samples[0].payload
    assert full.event_at == 5.2


@pytest.mark.parametrize("kind", ["video", "audio"])
def test_fresh_receipts_with_flat_rtp_cannot_claim_history(kind):
    now = [0.0]
    history = RecordingHistory(audio=kind == "audio", clock=lambda: now[0])
    fill(history, now, flat_video=kind == "video", audio=kind == "audio")
    if kind == "audio":
        history = RecordingHistory(audio=True, clock=lambda: now[0])
        history.configure((1, 1, SPS, PPS))
        for index in range(261):
            now[0] = index / 50
            if index % 5 == 0:
                history.append(
                    packet("video", index // 5 + 1, index * 1800, now[0], key=index % 50 == 0)
                )
            history.append(packet("audio", index + 1, 0, now[0]))
    result = history.snapshot(event_at=now[0])
    assert not result.ready
    assert result.available_video_seconds < 4 or result.available_audio_seconds < 4


def test_config_and_capture_reset_clear_samples_and_advance_identity_without_cursor_rewind():
    now = [0.0]
    history = RecordingHistory(audio=True, clock=lambda: now[0])
    fill(history, now)
    cursor, config = history.cursor, history.config_epoch
    assert history.configure((1, 2, SPS, PPS + b"changed", 5.3))
    result = history.snapshot(event_at=5.3)
    assert not result.samples and result.config_epoch > config and result.cursor == cursor
    # Samples captured before the current config receipt floor are never relabelled.
    history.append(packet("video", 1000, 500000, 5.2, key=True))
    assert not history.snapshot(event_at=5.3).samples
    history.configure((2, 1, SPS, PPS, 5.4))
    history.append(packet("video", 1001, 500000, 5.4, epoch=1, key=True))
    assert not history.snapshot(event_at=5.4).samples
    assert history.capture_epoch == 2


def test_raw_since_preserves_unconsumed_pframes_and_reports_pressure_loss():
    now = [0.0]
    history = RecordingHistory(clock=lambda: now[0])
    fill(history, now, duration=1, audio=False)
    snapshot = history.snapshot(event_at=now[0])
    cursor = snapshot.cursor
    for index in range(11, 21):
        now[0] = index / 10
        history.append(packet("video", index + 1, index * 9000, now[0], key=index == 20))
    new = history.raw_since(cursor)
    assert len(new) == 10 and all(sample.sequence > cursor for sample in new)
    assert new[0].payload == PFRAME and new[-1].payload == KEY
    now[0] = 100
    with pytest.raises(HomeKitOperationError) as lost:
        history.raw_since(cursor)
    assert lost.value.code == "history_lost"


def test_seed_cannot_copy_audio_into_video_only_recording():
    now = [0.0]
    warm = RecordingHistory(audio=True, clock=lambda: now[0])
    fill(warm, now)
    cold = RecordingHistory(audio=False, clock=lambda: now[0])
    cold.seed(warm)
    assert cold.snapshot(event_at=now[0]).ready
    assert all(sample.kind == "video" for sample in cold.snapshot(event_at=now[0]).samples)
    assert not cold._audio and cold._audio_bytes == 0


def test_source_raw_video_survives_header_expiry_and_mixed_sequences_are_not_resync():
    now = [0.0]
    source = RecordingSourceBuffer(clock=lambda: now[0])
    source.append(packet("video", 1, 0, 0, key=True))
    first = source.read(after_sequence=None, timeout=0)
    source.append(packet("video", 3, 9000, 0.1))  # sequence2 belongs to legacy audio
    assert not source.read(after_sequence=first.packets[-1].sequence, timeout=0).resynchronized
    now[0] = 13
    source.append(packet("video", 5, 1170000, 13, key=True))
    result = source.read(after_sequence=1, timeout=0)
    assert result.resynchronized and result.packets[-1].sequence == 5
    source.clear(capture_epoch=2)
    assert source.read(after_sequence=None, timeout=0).capture_epoch == 2


def test_recording_leases_have_independent_limits_and_legacy_public_keys():
    leases = MediaLeaseCoordinator()
    live = [leases.acquire(LeasePurpose.HOMEKIT_LIVE)[0] for _ in range(2)]
    recording, _ = leases.acquire(LeasePurpose.HOMEKIT_RECORDING)
    warm, _ = leases.acquire(LeasePurpose.HOMEKIT_RECORDING_WARM)
    with pytest.raises(RuntimeError):
        leases.acquire(LeasePurpose.HOMEKIT_RECORDING)
    with pytest.raises(RuntimeError):
        leases.acquire(LeasePurpose.HOMEKIT_RECORDING_WARM)
    assert leases.release(recording.id) == []
    assert {lease.id for lease in leases.snapshot().leases} == {
        warm.id,
        *(lease.id for lease in live),
    }
    assert set(maximum_session_seconds()) == {
        "event",
        "homekit_live",
        "scrypted_live",
        "snapshot",
        "intercom",
    }
    assert maximum_session_seconds(internal=True)["homekit_recording"] == 180


class Source:
    def __init__(self, now):
        self.now = now
        self.video = RecordingSourceBuffer(clock=lambda: now[0])
        self.audio = OriginalOpusBuffer(clock=lambda: now[0])
        self.configuration = (1, 1, SPS, PPS, now[0])
        self.closes = 0
        self.fail_close = False
        self.admissions = []
        self.consumers = []

    async def acquire(self, scope, *, admission, warm=False):
        assert admission()
        self.admissions.append((scope, warm))

        def close():
            self.closes += 1
            if self.fail_close:
                raise RuntimeError("Injected native release failure")

        resource = BufferedMediaConsumer(
            self.video,
            lambda: admission(),
            close,
            validity_callback=admission,
            original_audio_buffer=self.audio,
            source_clock_callback=lambda: (),
        )
        resource.video_configuration_callback = lambda: self.configuration
        self.consumers.append(resource)
        return resource

    def append(self, index):
        self.now[0] = self.configuration[4] + index / 10
        self.video.append(
            packet("video", index + 1, index * 9000, self.now[0], key=index % 10 == 0)
        )
        for audio_index in range(index * 5, index * 5 + 5):
            self.audio.append_audio(
                OPUS, audio_index * 960, received_at=self.configuration[4] + audio_index / 50
            )
        self.now[0] = self.configuration[4] + (index * 5 + 4) / 50


async def setup(count=1):
    controller, backend, boot, scopes = await configured(count)
    scopes = tuple([await controller.update_policy(boot, scope, POLICY) for scope in scopes])
    now = [time.monotonic()]
    source = Source(now)
    backend.acquire_recording = source.acquire
    manager = RecordingCaptureManager(controller, clock=lambda: now[0])
    return controller, backend, boot, scopes, now, source, manager


@pytest.mark.asyncio
async def test_recording_floor_does_not_grant_live_or_replay_old_frames():
    controller, _, boot, (scope,), now, source, manager = await setup()
    floor = controller.recording_floor(boot, scope)
    source.video.append(packet("video", 1, 0, floor - 1, key=True))
    source.audio.append_audio(OPUS, 0, received_at=floor - 1)
    handle = await controller.open_recording(boot, scope)
    try:
        assert not (await handle.read(timeout=0)).packets
        assert not (await handle.read_audio(timeout=0)).packets
        with pytest.raises(HomeKitOperationError) as denied:
            await controller.open_live(boot, scope)
        assert denied.value.code == "policy_denied"
        source.video.append(packet("video", 2, 9000, now[0], key=True))
        source.audio.append_audio(OPUS, 960, received_at=now[0])
        assert len((await handle.read(timeout=0)).packets) == 1
        assert len((await handle.read_audio(timeout=0)).packets) == 1
        assert handle.video_configuration[:4] == (1, 1, SPS, PPS)
        changed = await controller.update_policy(
            boot, scope, replace(POLICY, recording_audio=False)
        )
        with pytest.raises(HomeKitOperationError):
            await handle.read_audio(timeout=0)
        video_only = await controller.open_recording(boot, changed)
        try:
            with pytest.raises(HomeKitOperationError) as denied:
                await video_only.read_audio(timeout=0)
            assert denied.value.code == "policy_denied"
        finally:
            await video_only.close()
    finally:
        await manager.close()
        await controller.close()


@pytest.mark.asyncio
async def test_warm_continuously_accumulates_real_audio_then_cold_lease_survives_warm_stop():
    controller, _, boot, (scope,), now, source, manager = await setup()
    warm = await manager.open(
        boot, scope, str(uuid4()), audio=True, warm=True, owner_opt_in=True, mains_confirmed=True
    )
    try:
        for index in range(53):
            previous = warm.history.cursor
            source.append(index)
            await until(lambda previous=previous: warm.history.cursor >= previous + 6)
        snapshot = warm.snapshot()
        assert snapshot.ready and snapshot.available_audio_seconds >= 4
        assert source.audio.snapshot().duration_seconds <= 2
        assert warm.history._audio[-1].received_at - warm.history._audio[0].received_at > 4
        job = await manager.open(boot, scope, str(uuid4()), audio=True)
        assert job.snapshot().ready
        assert manager.pending_resources == 2
        await warm.close()
        assert manager.pending_resources == 1 and job.handle.is_current()
        before = job.current_cursor
        source.append(53)
        await until(lambda: job.current_cursor > before)
        new = await job.read_since(before, timeout=0)
        assert new.samples and all(sample.sequence > before for sample in new.samples)
        await job.close()
        assert manager.pending_resources == controller.pending_resources == 0
        assert source.closes == 2
    finally:
        await manager.close()
        await controller.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("owner,mains", [(False, False), (True, False), (False, True)])
async def test_warm_requires_explicit_owner_and_positive_mains_evidence(owner, mains):
    controller, _, boot, (scope,), _, source, manager = await setup()
    try:
        with pytest.raises(HomeKitOperationError) as denied:
            await manager.open(
                boot, scope, str(uuid4()), warm=True, owner_opt_in=owner, mains_confirmed=mains
            )
        assert denied.value.code == "warm_not_authorized"
        assert not source.admissions and manager.pending_resources == 0
    finally:
        await manager.close()
        await controller.close()


@pytest.mark.asyncio
async def test_manager_admits_independent_budgets_before_pending_acquisitions_allocate():
    controller, backend, boot, scopes, _, source, manager = await setup(3)
    entered, release = asyncio.Event(), asyncio.Event()
    original = backend.acquire_recording

    async def blocked(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    backend.acquire_recording = blocked
    tasks = [asyncio.create_task(manager.open(boot, scope, str(uuid4()))) for scope in scopes[:2]]
    try:
        await entered.wait()
        assert manager.pending_resources == 2
        with pytest.raises(HomeKitOperationError) as busy:
            await manager.open(boot, scopes[2], str(uuid4()))
        assert busy.value.code == "busy"
        assert manager.pending_resources == 2
        release.set()
        await asyncio.gather(*tasks)
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await manager.close()
        await controller.close()


@pytest.mark.asyncio
async def test_actual_source_epoch_and_configuration_changes_fence_before_collector_runs():
    controller, _, boot, (scope,), _, source, manager = await setup()
    capture = await manager.open(boot, scope, str(uuid4()))
    try:
        source.append(0)
        await until(lambda: capture.history.cursor > 0)
        old = capture.current_epochs
        source.video.clear(capture_epoch=2)
        assert capture.current_epochs != old  # no collector scheduling point
        hidden = capture.snapshot()
        assert not hidden.samples and not hidden.ready and hidden.video_sps is None
        assert hidden.reduced_reason == "source_changed"
        source.configuration = (2, 2, SPS, PPS + b"new", source.now[0])
        assert capture.current_epochs != old
    finally:
        await manager.close()
        await controller.close()


@pytest.mark.asyncio
async def test_failed_recording_release_retains_budget_then_explicit_retry_proves_cleanup():
    controller, _, boot, (scope,), _, source, manager = await setup()
    capture = await manager.open(boot, scope, str(uuid4()))
    source.fail_close = True
    with pytest.raises(HomeKitOperationError):
        await capture.close()
    assert manager.pending_resources == manager.cleanup_failures == 1
    assert controller.pending_resources == 1
    source.fail_close = False
    await capture.close()
    assert source.closes == 2 and manager.pending_resources == controller.pending_resources == 0
    await manager.close()
    await controller.close()


@pytest.mark.asyncio
async def test_clean_capture_owner_reopens_and_preserves_retired_identity_history():
    controller, _, boot, (scope,), _, source, manager = await setup()
    identity = str(uuid4())
    try:
        await manager.open(boot, scope, identity)
        await manager.close()
        assert not manager.pending_resources and source.closes == 1
        with pytest.raises(HomeKitOperationError):
            await manager.open(boot, scope, str(uuid4()))
        manager.reopen()
        with pytest.raises(HomeKitOperationError) as replay:
            await manager.open(boot, scope, identity)
        assert replay.value.code == "session_conflict"
        capture = await manager.open(boot, scope, str(uuid4()))
        assert capture.handle.is_current() and manager.pending_resources == 1
        await capture.close()
        assert source.closes == 2 and not manager.pending_resources
    finally:
        await manager.close()
        await controller.close()


@pytest.mark.asyncio
async def test_capture_reopen_refuses_failed_cleanup_until_actual_retry_retires_owner():
    controller, _, boot, (scope,), _, source, manager = await setup()
    try:
        capture = await manager.open(boot, scope, str(uuid4()))
        source.fail_close = True
        with pytest.raises(ExceptionGroup):
            await manager.close()
        assert manager.pending_resources == manager.cleanup_failures == 1
        with pytest.raises(HomeKitOperationError) as blocked:
            manager.reopen()
        assert blocked.value.code == "cleanup_pending" and manager._closed
        assert controller.pending_resources == 1
        source.fail_close = False
        await capture.close()
        assert not manager.pending_resources and not controller.pending_resources
        manager.reopen()
        replacement = await manager.open(boot, scope, str(uuid4()))
        assert replacement.handle.is_current() and manager.pending_resources == 1
    finally:
        source.fail_close = False
        await manager.close()
        await controller.close()


def test_session_frozen_headers_outlive_rolling_packet_ages_but_not_capture_retry():
    from types import SimpleNamespace

    clock = FakeClock()
    client = FakeClient()
    session = CameraMediaSession(
        client,
        SimpleNamespace(id="camera", name="Camera"),
        clock=clock,
        receiver_supervisor=fake_supervisor,
    )
    session.acquire(LeasePurpose.HOMEKIT_RECORDING)
    # The Session's actual receiver callback contract keeps generation binding.
    generation = session.snapshot().generation
    try:
        assert session._encoded_video(generation, KEY, 0)
        for second in range(1, 14):
            clock.value = second
            assert session._encoded_video(generation, b"\0\0\1\x65\x88\x84", second * 90000)
        assert session.buffer.ready
        metadata = session.video_configuration_snapshot()
        assert metadata[:4] == (session.buffer.capture_epoch, 1, SPS, PPS)
        assert session.recording_video_buffer.read(after_sequence=None, timeout=0).packets
        session._attempt_started(generation, 2)
        assert session.video_configuration_snapshot() is None
        assert not session.recording_video_buffer.read(after_sequence=None, timeout=0).packets
        assert not session._encoded_video(generation, PFRAME, 14 * 90000)
    finally:
        session.stop_all()


def test_native_sps_bounded_b_frame_reorder_retains_real_advancing_history():
    from fractions import Fraction

    import av

    from halo_server.media.h264 import parse_sps, split_annexb

    encoder = av.CodecContext.create("libx264", "w")
    encoder.width, encoder.height, encoder.pix_fmt = 320, 240, "yuv420p"
    encoder.time_base, encoder.framerate = Fraction(1, 30), Fraction(30)
    encoder.options = {
        "preset": "fast",
        "x264-params": "keyint=60:min-keyint=60:scenecut=0:bframes=2:force-cfr=1",
    }
    encoded = []
    for index in range(160):
        frame = av.VideoFrame(320, 240, "yuv420p")
        for plane in frame.planes:
            plane.update(bytes([16 if plane is frame.planes[0] else 128]) * plane.buffer_size)
        frame.pts = index
        encoded.extend(encoder.encode(frame))
    encoded.extend(encoder.encode(None))
    nals = split_annexb(bytes(encoded[0]))
    sps = next(nal for nal in nals if nal[0] & 31 == 7)
    pps = next(nal for nal in nals if nal[0] & 31 == 8)
    info = parse_sps(sps)
    assert info.max_reorder_frames == 2 and info.frame_rate == 30
    assert any(encoded[i].pts < encoded[i - 1].pts for i in range(1, len(encoded)))
    now = [0.0]
    history = RecordingHistory(clock=lambda: now[0])
    history.configure((1, 1, sps, pps))
    for index, output in enumerate(encoded):
        now[0] = index / 30
        history.append(
            EncodedPacket(
                index + 1,
                MediaStreamKind.VIDEO,
                bytes(output),
                output.pts * 3000,
                now[0],
                capture_epoch=1,
            )
        )
    result = history.snapshot(event_at=now[0])
    assert result.ready and result.available_video_seconds >= 4
    assert result.config_epoch == 1  # legitimate PTS reorder never reset/config relabelled


@pytest.mark.asyncio
async def test_recording_audio_false_to_true_has_new_floor_for_old_source_audio():
    controller, _, boot, (scope,), now, source, manager = await setup()
    try:
        scope = await controller.update_policy(boot, scope, replace(POLICY, recording_audio=False))
        old = time.monotonic()
        source.audio.append_audio(OPUS, 0, received_at=old)
        await asyncio.sleep(0.001)
        scope = await controller.update_policy(boot, scope, POLICY)
        floor = controller.recording_floor(boot, scope, audio=True)
        assert floor > old
        handle = await controller.open_recording(boot, scope)
        assert not (await handle.read_audio(timeout=0)).packets
        source.audio.append_audio(OPUS, 960, received_at=max(floor, now[0]) + 0.000001)
        assert len((await handle.read_audio(timeout=0)).packets) == 1
        await handle.close()
    finally:
        await manager.close()
        await controller.close()


@pytest.mark.asyncio
async def test_recording_and_warm_claim_budgets_do_not_consume_live_slots():
    controller, backend, boot, scopes, _, source, manager = await setup(3)
    handles = []
    try:
        scopes = tuple(
            [
                await controller.update_policy(boot, scope, replace(POLICY, streaming=True))
                for scope in scopes
            ]
        )
        handles.extend([await controller.open_live(boot, scopes[0]) for _ in range(2)])
        handles.append(await controller.open_recording(boot, scopes[0]))
        handles.append(await controller.open_recording(boot, scopes[0], warm=True))
        handles.append(await controller.open_recording(boot, scopes[1]))
        handles.append(await controller.open_recording(boot, scopes[1], warm=True))
        for warm in (False, True):
            with pytest.raises(HomeKitOperationError) as busy:
                await controller.open_recording(boot, scopes[2], warm=warm)
            assert busy.value.code == "busy"
        await handles[2].close()
        assert handles[0].is_current() and handles[1].is_current()
        assert handles[3].is_current() and len(backend.resources) == 2
    finally:
        for handle in handles:
            await handle.close()
        await manager.close()
        await controller.close()


@pytest.mark.asyncio
async def test_cancelled_late_acquire_stays_reserved_until_compensating_release(monkeypatch):
    import contextlib

    from halo_server.homekit import controller as native
    from halo_server.media import recording_capture as module

    monkeypatch.setattr(native, "CLEANUP_SECONDS", 0.03)
    monkeypatch.setattr(module, "STOP_SECONDS", 0.03)
    controller, backend, boot, (scope,), _, source, manager = await setup()
    entered, release = asyncio.Event(), asyncio.Event()

    async def stubborn(scope, *, admission, warm=False):
        entered.set()
        while not release.is_set():
            with contextlib.suppress(asyncio.CancelledError):
                await release.wait()
        # Deliberately uncooperative backend: creates a resource even after
        # controller admission is synchronously revoked. Controller compensates.
        resource = await source.acquire(scope, admission=lambda: True, warm=warm)
        resource.validity_callback = admission
        return resource

    backend.acquire_recording = stubborn
    opening = asyncio.create_task(manager.open(boot, scope, str(uuid4())))
    try:
        await entered.wait()
        opening.cancel()
        with pytest.raises(asyncio.CancelledError):
            await opening
        assert manager.pending_resources == controller.pending_resources == 1
        assert manager.cleanup_failures == 1 and not source.consumers
        with pytest.raises(HomeKitOperationError):
            await manager.open(boot, scope, str(uuid4()))
        release.set()
        await until(lambda: controller.pending_resources == 0)
        assert source.closes == 1 and not source.consumers[0].is_current()
        await manager.refresh()
        assert manager.pending_resources == manager.cleanup_failures == 0
    finally:
        release.set()
        await asyncio.gather(opening, return_exceptions=True)
        await manager.close()
        await controller.close()


def test_history_duration_packet_and_byte_caps_never_promise_lost_history(monkeypatch):
    from halo_server.media import recording_capture as module

    monkeypatch.setattr(module, "HISTORY_PACKETS", 8)
    monkeypatch.setattr(module, "VIDEO_BYTES", 80)
    monkeypatch.setattr(module, "AUDIO_BYTES", 20)
    now = [0.0]
    history = RecordingHistory(audio=True, clock=lambda: now[0])
    fill(history, now)
    assert len(history._video) <= 8 and history._video_bytes <= 80
    assert len(history._audio) <= 8 and history._audio_bytes <= 20
    assert not history.snapshot(event_at=now[0]).ready
    with pytest.raises(HomeKitOperationError) as lost:
        history.raw_since(0)
    assert lost.value.code == "history_lost"


def test_recording_warm_live_share_one_receiver_and_stop_only_after_last_lease():
    from types import SimpleNamespace

    client = FakeClient()
    session = CameraMediaSession(
        client, SimpleNamespace(id="camera", name="Camera"), receiver_supervisor=fake_supervisor
    )
    live = session.acquire(LeasePurpose.HOMEKIT_LIVE)
    recording = session.acquire(LeasePurpose.HOMEKIT_RECORDING)
    warm = session.acquire(LeasePurpose.HOMEKIT_RECORDING_WARM)
    assert len(client.futures) == 1 and session.snapshot().lease_count == 3
    session.release(warm.id)
    session.release(recording.id)
    assert session.is_lease_active(live.id) and session.snapshot().lease_count == 1
    assert not client.futures[0].finished
    session.release(live.id)
    assert client.futures[0].finished and session.receiver_retired


@pytest.mark.asyncio
async def test_real_service_backend_uses_distinct_recording_purposes_and_shared_capture(tmp_path):
    from test_native_service import select, service_fixture

    from halo_server.configuration import ServerMode

    service, _, _ = service_fixture(tmp_path, mode=ServerMode.DUAL)
    boot, scope = await select(service, streaming=True, recording=True)
    live = await service.homekit.open_live(boot, scope)
    recording = await service.homekit.open_recording(boot, scope)
    warm = await service.homekit.open_recording(boot, scope, warm=True)
    try:
        capture = service._media_sessions["42"]
        assert len(service._media_sessions) == 1 and len(capture.leases) == 3
        assert capture.acquired == [
            LeasePurpose.HOMEKIT_LIVE,
            LeasePurpose.HOMEKIT_RECORDING,
            LeasePurpose.HOMEKIT_RECORDING_WARM,
        ]
        await recording.close()
        await warm.close()
        assert len(capture.leases) == 1 and live.is_current()
    finally:
        await live.close()
        await recording.close()
        await warm.close()
        await service.homekit.detach()


@pytest.mark.asyncio
async def test_recording_read_discards_batch_if_source_clears_during_owned_thread():
    controller, _, boot, (scope,), _, source, manager = await setup()
    handle = await controller.open_recording(boot, scope)
    try:
        source.append(0)
        original = source.video.read

        def racing_read(**kwargs):
            batch = original(**kwargs)
            source.video.clear(capture_epoch=2)
            return batch

        source.video.read = racing_read
        result = await handle.read(timeout=0)
        assert not result.packets and result.resynchronized and result.capture_epoch == 2
        source.video.append(packet("video", 100, 90000, source.now[0], epoch=1, key=True))
        assert not original(after_sequence=None, timeout=0).packets
    finally:
        await handle.close()
        await manager.close()
        await controller.close()


@pytest.mark.asyncio
async def test_epoch_reset_between_identity_getters_cannot_return_old_history_identity():
    controller, _, boot, (scope,), _, source, manager = await setup()
    capture = await manager.open(boot, scope, str(uuid4()))
    try:
        source.append(0)
        await until(lambda: capture.history.cursor > 0)
        old = capture.current_epochs
        triggered = False

        def racing_configuration():
            nonlocal triggered
            if not triggered:
                triggered = True
                source.video.clear(capture_epoch=2)
                source.configuration = (2, 1, SPS, PPS, source.now[0])
            return source.configuration

        source.consumers[0].video_configuration_callback = racing_configuration
        current = capture.current_epochs
        assert current[0] == 2 and current != old
        assert not capture.snapshot().samples
    finally:
        await manager.close()
        await controller.close()


@pytest.mark.asyncio
async def test_cold_open_during_pending_warm_acquire_succeeds_without_invented_seed():
    controller, backend, boot, (scope,), _, source, manager = await setup()
    entered, release = asyncio.Event(), asyncio.Event()
    original = backend.acquire_recording

    async def delayed(*args, warm=False, **kwargs):
        if warm:
            entered.set()
            await release.wait()
        return await original(*args, warm=warm, **kwargs)

    backend.acquire_recording = delayed
    warming = asyncio.create_task(
        manager.open(boot, scope, str(uuid4()), warm=True, owner_opt_in=True, mains_confirmed=True)
    )
    try:
        await entered.wait()
        cold = await manager.open(boot, scope, str(uuid4()))
        assert not cold.snapshot().ready and not cold.snapshot().samples
        assert manager.pending_resources == 2
        release.set()
        warm = await warming
        assert warm is not cold and len(source.admissions) == 2
    finally:
        release.set()
        await asyncio.gather(warming, return_exceptions=True)
        await manager.close()
        await controller.close()


def test_expired_recording_claim_cannot_extend_headers_once_useful_video_evidence():
    from types import SimpleNamespace

    clock = FakeClock()
    client = FakeClient()
    session = CameraMediaSession(
        client,
        SimpleNamespace(id="camera", name="Camera"),
        clock=clock,
        receiver_supervisor=fake_supervisor,
    )
    live = session.acquire(LeasePurpose.HOMEKIT_LIVE, ttl=50)
    recording = session.acquire(LeasePurpose.HOMEKIT_RECORDING)
    generation = session.snapshot().generation
    try:
        assert session._encoded_video(generation, KEY, 0)
        clock.value = 13
        assert session._encoded_video(generation, b"\0\0\1\x65\x88\x84", 13 * 90000)
        assert session.buffer.ready and session.is_lease_active(recording.id)
        clock.value = 31
        assert session.is_lease_active(live.id) and not session.is_lease_active(recording.id)
        assert not session._encoded_video(generation, PFRAME, 31 * 90000)
    finally:
        session.stop_all()
