"""Existing source prefetch is paced once, without refreshing original sound."""

import asyncio
import struct
from types import SimpleNamespace as NS

import pytest
from test_homekit_audio_live_edge import drain, setup, source
from test_live_timeline import Clock, observe, presentation, sr

from halo_server.homekit.models import HomeKitOperationError
from halo_server.media.opus import AudioOutput, OriginalOpusBuffer


def retained_batch(*, start=100, epoch=1):
    packets = tuple(source(i, start + i * 0.06, epoch=epoch) for i in range(4))
    packets[0].startup_at = start + 0.24
    packets[0].startup_samples = 11520
    return NS(packets=packets, capture_epoch=epoch, resynchronized=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("rejection", [None, "prewarm", "permission", "old", "resync"])
async def test_retained_start_is_authorized_fresh_and_paced_only_once(monkeypatch, rejection):
    context = setup(monkeypatch, cold=0)
    track = context.track
    track._view_started_at = 100
    context.now[0] = 100.24
    batch = retained_batch()
    if rejection == "prewarm":
        track._view_started_at = 100.1
    elif rejection == "permission":
        track._audio_allowed_after = 100.1
    elif rejection == "old":
        context.now[0] = 100.5
    elif rejection == "resync":
        batch.resynchronized = True
    try:
        await track._audio_batch(batch)
        assert track._retained_start is (rejection is None)
        assert [pts for pts, _ in context.calls] == (
            [0, 2880, 5760, 8640] if rejection is None else [8640]
        )
        await drain(track)
        if rejection is None:
            assert len(context.sent) == 12
            gaps = [b[0] - a[0] for a, b in zip(context.sent, context.sent[1:], strict=False)]
            assert gaps == pytest.approx([0.02] * 11)
            # Timing remains original 48k source PTS -> negotiated 24k RTP,
            # including the converter's actual signed lookahead offset.
            rtp = [struct.unpack("!I", data[4:8])[0] for _, data in context.sent]
            assert [(b - a) % 2**32 for a, b in zip(rtp, rtp[1:], strict=False)] == [480] * 11
            assert track._timeline.presentation("audio", capture_epoch=1, source_pts=0) == 100
            track._pause_delivery()
            assert not track._retained_start and track._retained_anchor is None
    finally:
        await track.close()


@pytest.mark.asyncio
async def test_authorized_acquisition_precedes_delayed_start_and_epoch_fences(monkeypatch):
    context = setup(monkeypatch, cold=0)
    track = context.track
    context.now[0] = 100.24
    # Avoid starting unrelated read/feedback owners: this specifically checks
    # start's authorization boundary plus the real batch/pacer underneath it.
    created = []

    def create(coro, **_kwargs):
        coro.close()
        future = asyncio.get_running_loop().create_future()
        future.set_result(None)
        created.append(future)
        return future

    monkeypatch.setattr("halo_server.media.homekit_audio.asyncio.create_task", create)
    try:
        track.start(NS(), track.config, speaker_volume=100, authorized_at=100)
        await track._audio_batch(retained_batch())
        assert track._retained_start and len(context.calls) == 4
        context.epoch[0] = 2
        await drain(track)
        assert not context.sent
        track._reset(2)
        assert not track._retained_start and track._retained_anchor is None
    finally:
        await track.close()
    assert created


@pytest.mark.parametrize("value", [True, -1, float("nan"), float("inf"), 101])
def test_invalid_authorized_boundary_rejected(monkeypatch, value):
    context = setup(monkeypatch)
    try:
        with pytest.raises(HomeKitOperationError) as exc:
            context.track.start(NS(), context.track.config, speaker_volume=100, authorized_at=value)
        assert exc.value.code == "invalid_configuration"
    finally:
        context.track.socket.close()
        context.track.secure.close()


def test_reordered_receipt_expiration_removes_old_interior_without_refresh():
    now = [101.0]
    buffer = OriginalOpusBuffer(clock=lambda: now[0])
    for index, receipt in enumerate((100.8, 100, 100.9)):
        buffer.append_audio(b"\x58\x01", index * 2880, received_at=receipt)
    now[0] = 102.1
    batch = buffer.read(after_sequence=0, timeout=0)
    assert [p.sequence for p in batch.packets] == [1, 3]
    assert batch.resynchronized
    assert [p.received_at for p in batch.packets] == [100.8, 100.9]
    assert sum(len(p.payload) for p in buffer.snapshot().packets) == 4


@pytest.mark.asyncio
async def test_output_discontinuity_reanchors_without_permanent_budget_mute(monkeypatch):
    context = setup(monkeypatch, cold=0)
    track = context.track
    context.now[0] = 100.24
    try:
        await track._audio_batch(retained_batch())
        await drain(track)
        first_count = len(context.sent)
        # Same-epoch source reset or DTX gap: preserve discontinuity, do not
        # schedule the source's new numeric PTS offset thousands of seconds out.
        packet = source(0, context.now[0])
        await track._audio_outputs((AudioOutput(b"\xd8\x01", 2**32, 960, "adapt"),), packet)
        await drain(track)
        assert len(context.sent) == first_count + 1
        assert track._retained_anchor[0] == 2**32
        assert track._timing.values["sent_packets"] == first_count + 1
    finally:
        await track.close()


def test_native_prefix_expiry_and_lookahead_use_oldest_reordered_receipt(monkeypatch):
    context = setup(monkeypatch)
    track = context.track
    context.now[0] = 101.3
    track._input_spans.extend(((0, 960, 101), (960, 1920, 100)))
    assert track._expired_prefix()
    assert track._output_receipt(AudioOutput(b"\xd8\x01", -120, 120, "adapt"), 101.3) == 100
    track.socket.close()
    track.secure.close()


@pytest.mark.parametrize("sender_clock", [False, True])
def test_delayed_audio_admission_preserves_original_av_clock_relationship(sender_clock):
    clock = Clock()
    timeline = clock.timeline()
    clocks = (sr("video"), sr("audio", offset=0.02)) if sender_clock else ()
    observe(timeline, "video", receipt=100, clocks=clocks)
    # The complete audio packet arrived 20ms later, then spent 240ms in
    # existing source prefetch. Admission must not turn this into 260ms.
    clock.now = 100.26
    observe(timeline, "audio", receipt=100.02, clocks=clocks)
    assert presentation(timeline, "audio", 0) - presentation(timeline, "video", 0) == pytest.approx(
        0.02
    )
    assert presentation(timeline, "audio", 48000) - presentation(
        timeline, "video", 90000
    ) == pytest.approx(0.02)
