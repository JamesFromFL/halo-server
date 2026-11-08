"""Finite decoded handoff, real pinned decoder and retained shutdown ownership."""

import asyncio
import json
import threading
import time
from fractions import Fraction
from types import SimpleNamespace

import pytest
from aiortc.jitterbuffer import JitterFrame
from aiortc.mediastreams import MediaStreamError
from aiortc.rtcrtpparameters import RTCRtpCodecParameters
from aiortc.rtcrtpreceiver import RemoteStreamTrack, RTCRtpReceiver, decoder_worker
from av import AudioFrame
from test_ring_opus_decoder import silk_wideband_60ms as silk_wideband_60ms

from halo_server.media.decoded_audio import (
    MAX_PCM_BYTES,
    MAX_PCM_FRAMES,
    MAX_PCM_SAMPLES,
    DecodedAudioError,
    DecodedAudioLane,
)
from halo_server.media.opus import parse_opus, repacketize


def frame(index, *, samples=5760, channels=2):
    value = AudioFrame(format="s16", layout="mono" if channels == 1 else "stereo", samples=samples)
    value.sample_rate = 48000
    value.pts, value.time_base = 2_000_000_000 + index * samples, Fraction(1, 48000)
    return value


def codec():
    return RTCRtpCodecParameters(
        mimeType="audio/opus", clockRate=48000, channels=2, payloadType=111
    )


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.001)


def receiver(thread=None, lane=None):
    return SimpleNamespace(
        _RTCRtpReceiver__decoder_thread=thread,
        _RTCRtpReceiver__decoder_queue=lane.input if lane else None,
    )


@pytest.mark.asyncio
async def test_put_reserves_before_coroutine_and_get_retains_until_actual_release():
    lane = DecodedAudioLane()
    value = frame(0)
    notification = lane.output.put(value)
    try:
        assert lane.snapshot()["queued_frames"] == 1
        assert lane.snapshot()["notification_pending"]
        # No frame argument is retained by a scheduled put coroutine; its frame
        # reservation already belongs to the finite thread-safe deque.
        assert set(notification.cr_frame.f_locals) == {"self"}
        await notification
        before = lane.snapshot()
        assert await lane.output.get() is value
        held = lane.snapshot()
        assert held["queued_frames"] == 0 and held["inflight_frames"] == 1
        assert held["reserved_bytes"] == before["reserved_bytes"]
        with pytest.raises(DecodedAudioError, match="consumer_busy"):
            await lane.output.get()
        lane.fence()
        assert lane.snapshot()["inflight_frames"] == 1
        await lane.retire(receiver())
        assert lane.snapshot()["native_retired"]
        assert lane.snapshot()["reserved_bytes"] >= value.planes[0].buffer_size
        lane.release(value)
        assert lane.snapshot()["reserved_bytes"] == lane.snapshot()["reserved_frames"] == 0
        assert await lane.output.get() is None
    finally:
        lane.fence()
        notification.close()


@pytest.mark.asyncio
async def test_maximum_120ms_stereo_frames_fit_without_evicting_inflight_owner():
    lane = DecodedAudioLane()
    first = frame(0)
    await lane.output.put(first)
    assert await lane.output.get() is first
    try:
        for index in range(1, 101):
            await lane.output.put(frame(index))
            result = lane.snapshot()
            assert result["inflight_frames"] == 1
            assert result["reserved_frames"] <= MAX_PCM_FRAMES
            assert result["reserved_bytes"] <= MAX_PCM_BYTES
            assert result["reserved_samples"] <= MAX_PCM_SAMPLES
        result = lane.snapshot()
        assert result["counters"]["pcm_invalid"] == 0
        assert result["counters"]["pcm_dropped"] > 0 and lane.revision > 0
        assert result["inflight_frames"] == 1
        lane.release(first)
        newest = await lane.output.get()
        assert newest.pts == 2_000_000_000 + 100 * 5760
        lane.release(newest)
        text = json.dumps(lane.snapshot())
        assert "2000000000" not in text and "payload" not in text
        assert len(text) < 1800
    finally:
        lane.fence()
        await lane.retire(receiver())


@pytest.mark.asyncio
async def test_only_one_pending_notification_and_fence_wakes_decoder_thread():
    lane = DecodedAudioLane()
    entered, release = asyncio.Event(), asyncio.Event()
    second_returned = threading.Event()
    first = lane._notify
    futures = []

    async def delayed():
        entered.set()
        await release.wait()
        await first()

    lane._notify = delayed
    loop = asyncio.get_running_loop()

    def produce():
        futures.append(asyncio.run_coroutine_threadsafe(lane.output.put(frame(0)), loop))
        futures.append(asyncio.run_coroutine_threadsafe(lane.output.put(frame(1)), loop))
        second_returned.set()

    thread = threading.Thread(target=produce)
    thread.start()
    try:
        await asyncio.wait_for(entered.wait(), 1)
        # Scheduling the coroutine may precede publishing its future. Wait for
        # the second put to block before inspecting the producer's first result.
        await until(lambda: lane.snapshot()["counters"]["notification_waits"] > 0)
        assert not second_returned.is_set()
        assert len(futures) == 1 and lane.snapshot()["queued_frames"] == 1
        lane.fence()
        await lane.retire(receiver(thread, lane))
        assert second_returned.is_set() and not thread.is_alive()
        assert lane.snapshot()["reserved_frames"] == 0
    finally:
        lane.fence()
        release.set()
        await until(lambda: not thread.is_alive())
        for future in futures:
            await asyncio.wrap_future(future)


def test_compressed_input_nonblocking_bounded_drop_preserves_latest_and_control():
    lane = DecodedAudioLane()
    for index in range(100):
        lane.input.put((codec(), JitterFrame(b"\x58\x01", index * 2880)))
    result = lane.snapshot()
    assert result["input_queued"] == 4
    assert result["counters"]["input_accepted"] == 100
    assert result["counters"]["input_dropped"] == 96
    assert result["counters"]["input_dropped_samples"] == 96 * 2880
    assert lane.input.get_nowait()[1].timestamp == 96 * 2880
    result = lane.snapshot()
    assert result["input_reserved_packets"] <= 6
    assert result["input_reserved_samples"] <= 6 * 5760
    assert result["input_reserved_bytes"] <= 6 * 65535
    lane.input.put(None)
    assert lane.input.get_nowait() is None
    assert lane.input.empty() and lane.snapshot()["closed"]


@pytest.mark.asyncio
async def test_quiesce_wakes_blocked_input_and_original_private_join_is_then_immediate():
    lane = DecodedAudioLane()
    items = []
    thread = threading.Thread(target=lambda: items.append(lane.input.get()))
    thread.start()
    owner = receiver(thread, lane)
    try:
        await lane.retire(owner)
        assert items == [None] and not thread.is_alive()
        began = time.monotonic()
        RTCRtpReceiver._RTCRtpReceiver__stop_decoder(owner)
        assert time.monotonic() - began < 0.05
        assert owner._RTCRtpReceiver__decoder_thread is None
    finally:
        lane.fence()
        await until(lambda: not thread.is_alive())


@pytest.mark.asyncio
async def test_retirement_timeout_and_cancel_keep_exact_thread_claim_until_actual_exit():
    lane = DecodedAudioLane()
    entered, release = threading.Event(), threading.Event()

    def blocked_native():
        entered.set()
        release.wait()

    thread = threading.Thread(target=blocked_native)
    thread.start()
    owner = receiver(thread, lane)
    pending = None
    try:
        await until(entered.is_set)
        with pytest.raises(TimeoutError):
            await lane.retire(owner, timeout=0.01)
        assert thread.is_alive() and not lane.snapshot()["native_retired"]
        assert lane.snapshot()["reserved_bytes"] > 0
        pending = asyncio.create_task(lane.retire(owner))
        await asyncio.sleep(0)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert thread.is_alive() and not lane.snapshot()["native_retired"]
        release.set()
        assert await lane.retire(owner)
        assert lane.snapshot()["reserved_bytes"] == 0
    finally:
        release.set()
        await until(lambda: not thread.is_alive())
        if pending is not None:
            await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["pts", "timebase", "channels", "format", "oversized"])
async def test_unknown_or_unsupported_pcm_is_only_legacy_drop_with_finite_notification(invalid):
    lane = DecodedAudioLane()
    value = frame(0)
    if invalid == "pts":
        value.pts = None
    elif invalid == "timebase":
        value.time_base = Fraction(1, 90001)
        value.pts = 1
    elif invalid == "channels":
        value = AudioFrame(format="s16", layout="5.1", samples=960)
    elif invalid == "format":
        value = AudioFrame(format="flt", layout="stereo", samples=960)
    else:
        value = frame(0, samples=5761)
    notification = lane.output.put(value)
    assert lane.snapshot()["notification_pending"]
    await notification
    assert not lane.snapshot()["notification_pending"]
    assert lane.output.empty() and lane.snapshot()["counters"]["pcm_invalid"] == 1
    lane.fence()
    await lane.retire(receiver())


@pytest.mark.asyncio
async def test_attachment_is_per_track_before_start_and_preserves_public_recv_contract():
    lane = DecodedAudioLane()
    track = RemoteStreamTrack("audio")
    lane.attach_track(track, receiver=receiver())
    lane.attach_track(track, receiver=receiver())
    await lane.output.put(frame(0, samples=960))
    value = await track.recv()
    assert value.samples == 960
    lane.release(value)
    lane.fence()
    with pytest.raises(MediaStreamError):
        await track.recv()
    with pytest.raises(DecodedAudioError, match="decoder_already_started"):
        lane.attach_track(RemoteStreamTrack("audio"), receiver=receiver(threading.Thread()))
    with pytest.raises(DecodedAudioError, match="track_contract_changed"):
        lane.attach_track(RemoteStreamTrack("video"))
    wrong = receiver()
    wrong._track = RemoteStreamTrack("audio")
    with pytest.raises(DecodedAudioError, match="track_contract_changed"):
        lane.attach_track(RemoteStreamTrack("audio"), receiver=wrong)
    await lane.retire(receiver())


@pytest.mark.asyncio
async def test_real_pinned_decoder_accepts_120ms_silk_and_retires_before_peer_join(
    silk_wideband_60ms,
):
    lane = DecodedAudioLane()
    track = RemoteStreamTrack("audio")
    owner = receiver(lane=lane)
    lane.attach_track(track, receiver=owner)
    payload = repacketize(silk_wideband_60ms[:2])
    assert parse_opus(payload).duration_samples == 5760
    thread = threading.Thread(
        target=decoder_worker, args=(asyncio.get_running_loop(), lane.input, lane.output)
    )
    owner._RTCRtpReceiver__decoder_thread = thread
    thread.start()
    try:
        lane.input.put((codec(), JitterFrame(payload, 2_000_000_000)))
        try:
            value = await asyncio.wait_for(track.recv(), 2)
        except TimeoutError:
            pytest.fail(f"Native decoded lane did not deliver: {lane.snapshot()}")
        assert value.format.name == "s16" and value.sample_rate == 48000
        assert value.samples == 5760 and len(value.layout.channels) == 2
        assert value.pts == 2_000_000_000
        result = lane.snapshot()
        assert result["reserved_bytes"] <= MAX_PCM_BYTES
        assert result["reserved_frames"] <= MAX_PCM_FRAMES
        assert result["reserved_samples"] <= MAX_PCM_SAMPLES
        lane.release(value)
        await lane.retire(owner)
        assert not thread.is_alive() and lane.snapshot()["reserved_bytes"] == 0
        RTCRtpReceiver._RTCRtpReceiver__stop_decoder(owner)
        with pytest.raises(MediaStreamError):
            await track.recv()
    finally:
        lane.fence()
        await lane.retire(owner)


@pytest.mark.asyncio
async def test_thread_flood_keeps_loop_responsive_and_never_drops_active_pcm():
    lane = DecodedAudioLane()
    held = frame(0)
    await lane.output.put(held)
    assert await lane.output.get() is held
    loop = asyncio.get_running_loop()
    futures, unfinished = [], []

    def produce():
        for index in range(1, 101):
            future = asyncio.run_coroutine_threadsafe(lane.output.put(frame(index)), loop)
            futures.append(future)
            unfinished.append(sum(not item.done() for item in futures))

    thread = threading.Thread(target=produce)
    thread.start()
    heartbeats = 0
    try:
        async with asyncio.timeout(2):
            while thread.is_alive():
                heartbeats += 1
                await asyncio.sleep(0)
        for future in futures:
            await asyncio.wrap_future(future)
        result = lane.snapshot()
        assert heartbeats > 10
        # One logically pending notification plus its just-completed Future
        # awaiting the standard asyncio completion callback, never100 put jobs.
        assert max(unfinished) <= 2
        assert result["inflight_frames"] == 1
        assert result["counters"]["pcm_invalid"] == 0
        assert result["counters"]["pcm_dropped"] >= 90
        assert result["maxima"]["frames"] <= MAX_PCM_FRAMES
        assert result["maxima"]["bytes"] <= MAX_PCM_BYTES
        assert result["maxima"]["samples"] <= MAX_PCM_SAMPLES
        assert held.pts == 2_000_000_000
        lane.release(held)
        newest = await lane.output.get()
        assert newest.pts > held.pts + held.samples
        lane.release(newest)
    finally:
        lane.fence()
        await lane.retire(receiver(thread, lane))


@pytest.mark.asyncio
async def test_reused_frame_cannot_create_a_second_claim_for_the_active_owner():
    lane = DecodedAudioLane()
    value = frame(0)
    await lane.output.put(value)
    assert await lane.output.get() is value
    await lane.output.put(value)
    assert lane.output.empty() and lane.snapshot()["inflight_frames"] == 1
    assert lane.snapshot()["counters"]["pcm_invalid"] == 1
    lane.release(value)
    await lane.retire(receiver())


def test_unpinned_version_and_unknown_queue_contract_fail_before_attach(monkeypatch):
    from halo_server.media import decoded_audio

    lane = DecodedAudioLane()
    with pytest.raises(DecodedAudioError, match="thread_contract_changed"):
        lane.attach_track(RemoteStreamTrack("audio"), receiver=object())
    monkeypatch.setattr(decoded_audio.aiortc, "__version__", "future-unqualified")
    with pytest.raises(DecodedAudioError, match="unsupported_aiortc"):
        DecodedAudioLane()
