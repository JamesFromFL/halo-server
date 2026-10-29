"""Native phase, source-time PTS and actual independent-IDR regressions."""

import asyncio
from dataclasses import asdict
from fractions import Fraction

import av
import pytest
from test_media_h264 import encode_source

from halo_server.media import transcode
from halo_server.media.cadence import IDR_INTERVAL, SourceCadence
from halo_server.media.h264 import VideoConfiguration, annexb, split_annexb
from halo_server.media.transcode import AccessUnitAdapter, VideoTransformError
from halo_server.media.transcode_worker import open_encoder


@pytest.mark.asyncio
@pytest.mark.parametrize("skip_inflight_output", [False, True])
async def test_keyframe_request_during_feed_applies_to_next_input(
    monkeypatch, skip_inflight_output
):
    source = encode_source(profile="high", level="5.0", frames=3)
    encoded = encode_source(profile="high", frames=3, force=(2,))
    entered, resume = asyncio.Event(), asyncio.Event()
    dispatched = []

    class HeldWorker:
        def __init__(self, _configuration):
            pass

        async def feed(self, _payload, timestamp, *, force_idr):
            index = len(dispatched)
            dispatched.append(force_idr)
            if index == 1:
                entered.set()
                await resume.wait()
                if skip_inflight_output:
                    return ()
            return ((timestamp, encoded[index][0]),)

        async def close(self):
            pass

    monkeypatch.setattr(transcode, "VideoWorker", HeldWorker)
    adapter = AccessUnitAdapter(VideoConfiguration(320, 240, 15, "high", "4.0", 1000))
    try:
        assert (await adapter.feed(*source[0]))[0].is_keyframe
        pending = asyncio.create_task(adapter.feed(*source[1]))
        await asyncio.wait_for(entered.wait(), 1)
        adapter.request_keyframe()
        resume.set()
        output = await pending
        assert len(output) == (0 if skip_inflight_output else 1)
        if output:
            assert not output[0].is_keyframe
        assert (await adapter.feed(*source[2]))[0].is_keyframe
        assert dispatched == [True, False, True]
        assert not adapter._force
    finally:
        resume.set()
        await adapter.close()


def source_with_long_gop(fps, count):
    encoder = av.CodecContext.create("libx264", "w")
    encoder.width, encoder.height = 320, 240
    encoder.pix_fmt = "yuv420p"
    encoder.time_base, encoder.framerate = Fraction(1, fps), Fraction(fps)
    encoder.thread_count, encoder.bit_rate, encoder.max_b_frames = 1, 500000, 0
    encoder.gop_size = fps * 10
    encoder.options = {
        "preset": "veryfast",
        "tune": "zerolatency",
        "profile": "high",
        "level": "4.0",
        "x264-params": f"bframes=0:scenecut=0:repeat-headers=1:keyint={fps * 10}",
    }
    source = []
    for index in range(count):
        frame = av.VideoFrame(320, 240, "yuv420p")
        for number, plane in enumerate(frame.planes):
            plane.update(bytes([80 + index % 60 if number == 0 else 128]) * plane.buffer_size)
        frame.pts, frame.time_base = index, Fraction(1, fps)
        source.extend(bytes(packet) for packet in encoder.encode(frame))
    source.extend(bytes(packet) for packet in encoder.encode(None))
    assert len(source) == count
    return source


@pytest.mark.parametrize("fps", [1, 15, 24, 30])
def test_phase_keeps_nominal_rate_with_one_tick_jitter_without_accumulated_drift(fps):
    cadence = SourceCadence(fps)
    timestamps = [2**32 + round(index * 90000 / fps) - index % 2 for index in range(1000)]
    assert all(cadence.accept(timestamp) for timestamp in timestamps)
    assert not cadence.accept(timestamps[-1])


@pytest.mark.parametrize("source_fps,selected_fps", [(60, 30), (30, 15), (60, 24)])
def test_phase_selects_one_existing_frame_per_rational_slot(source_fps, selected_fps):
    cadence = SourceCadence(selected_fps)
    timestamps = [round(index * 90000 / source_fps) for index in range(source_fps * 10)]
    selected = [timestamp for timestamp in timestamps if cadence.accept(timestamp)]
    # A finite window may include the nearest phase just beyond its last full
    # nominal period; the quantization envelope is bounded to half a period.
    assert len(selected) == (timestamps[-1] * selected_fps + 44999) // 90000 + 1
    assert selected_fps * 10 <= len(selected) <= selected_fps * 10 + 1
    assert len(selected) == len(set(selected))
    if (source_fps, selected_fps) == (60, 30):
        assert selected == list(range(0, 900000, 3000))


@pytest.mark.parametrize("fps,period", [(30, Fraction(3003)), (24, Fraction(15015, 4))])
def test_fractional_source_rate_below_selected_maximum_keeps_every_source_frame(fps, period):
    cadence = SourceCadence(fps)
    assert all(cadence.accept(round(index * period)) for index in range(1000))


@pytest.mark.parametrize("fps", [True, 0, 31, 29.97])
def test_cadence_rejects_invalid_rate(fps):
    with pytest.raises(ValueError):
        SourceCadence(fps)


@pytest.mark.asyncio
async def test_copy_keeps_jittered_source_bytes_and_does_not_skip_dependent_frames():
    source = encode_source(fps=30, frames=30)
    adapter = AccessUnitAdapter(VideoConfiguration(320, 240, 30, "baseline", "3.1", 2000))
    try:
        for index, (payload, timestamp) in enumerate(source):
            timestamp -= index % 2
            outputs = await adapter.feed(payload, timestamp, capture_epoch=1)
            assert len(outputs) == 1
            assert outputs[0].mode == "copy" and outputs[0].payload == payload
            assert outputs[0].timestamp == timestamp
        assert adapter._worker is None
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_true_repeated_phase_fences_copy_chain_until_an_actual_source_idr():
    source = encode_source(frames=5, force=(4,))
    adapter = AccessUnitAdapter(VideoConfiguration(320, 240, 15, "baseline", "3.1", 2000))
    try:
        assert (await adapter.feed(source[0][0], 0))[0].mode == "copy"
        for index in range(1, 4):
            assert await adapter.feed(source[index][0], index * 3000) == ()
        outputs = await adapter.feed(source[4][0], 12000)
        assert outputs[0].mode == "adapt" and outputs[0].is_keyframe
    finally:
        await adapter.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("source_fps,selected_fps", [(15, 15), (24, 24), (30, 30), (15, 30)])
async def test_native_jitter_and_long_source_gop_keep_frames_and_force_source_time_idrs(
    source_fps, selected_fps
):
    source = source_with_long_gop(source_fps, source_fps * 6 + 1)
    # The source has only the initial IDR; the managed encoder must create its own.
    assert sum(any(nal[0] & 31 == 5 for nal in split_annexb(data)) for data in source) == 1
    adapter = AccessUnitAdapter(VideoConfiguration(320, 240, selected_fps, "main", "3.1", 1000))
    outputs = []
    timestamps = []
    try:
        for index, payload in enumerate(source):
            timestamp = 2**32 - 9000 + round(index * 90000 / source_fps) - index % 2
            timestamps.append(timestamp)
            outputs.extend(await adapter.feed(payload, timestamp, capture_epoch=1))
        assert [output.timestamp for output in outputs] == timestamps
        assert all(output.mode == "adapt" for output in outputs)
        idrs = [output for output in outputs if output.is_keyframe]
        assert len(idrs) >= 3
        # If no input is exactly on the deadline, the next available source frame wins.
        assert all(
            right.timestamp - left.timestamp <= IDR_INTERVAL + 90000 / source_fps + 1
            for left, right in zip(idrs, idrs[1:], strict=False)
        )
        for output in idrs:
            assert {5, 7, 8} <= {nal[0] & 31 for nal in split_annexb(output.payload)}
            decoder = av.CodecContext.create("h264", "r")
            decoder.thread_count = 1
            decoded = decoder.decode(av.Packet(output.payload))
            assert len(decoded) == 1 and (decoded[0].width, decoded[0].height) == (320, 240)
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_native_faster_input_is_decimated_by_phase_without_retimestamping():
    source = source_with_long_gop(60, 120)
    adapter = AccessUnitAdapter(VideoConfiguration(320, 240, 30, "main", "3.1", 1000))
    try:
        outputs = []
        for index, payload in enumerate(source):
            outputs.extend(await adapter.feed(payload, index * 1500))
        assert [output.timestamp for output in outputs] == list(range(0, 180000, 3000))
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_discontinuity_and_new_capture_reset_phase_and_require_a_real_idr():
    source = encode_source(profile="high", level="5.0", frames=4, force=(3,))
    adapter = AccessUnitAdapter(VideoConfiguration(320, 240, 15, "main", "3.1", 1000))
    try:
        assert (await adapter.feed(source[0][0], 90000, capture_epoch=1))[0].is_keyframe
        assert await adapter.feed(source[1][0], 96000, capture_epoch=1)
        assert await adapter.feed(source[2][0], 6000, capture_epoch=1) == ()
        restarted = await adapter.feed(source[3][0], 12000, capture_epoch=1)
        assert restarted[0].timestamp == 12000 and restarted[0].is_keyframe
        assert await adapter.feed(source[2][0], 6000, capture_epoch=2) == ()
        fresh = await adapter.feed(source[3][0], 12000, capture_epoch=2)
        assert fresh[0].timestamp == 12000 and fresh[0].is_keyframe
    finally:
        await adapter.close()


def test_fixed_encoder_uses_actual_source_precision_without_vfr_buffering():
    selected = VideoConfiguration(320, 240, 30, "main", "3.1", 1000)
    encoder = open_encoder(asdict(selected))
    assert encoder.time_base == Fraction(1, 90000)
    assert encoder.framerate == 30
    assert not encoder.options
    for timestamp in (2**32 - 2999, 2**32 + 3001, 2**32 + 9000):
        frame = av.VideoFrame(320, 240, "yuv420p")
        for index, plane in enumerate(frame.planes):
            plane.update(bytes([80 if index == 0 else 128]) * plane.buffer_size)
        frame.pts, frame.time_base = timestamp, Fraction(1, 90000)
        packets = encoder.encode(frame)
        assert len(packets) == 1
        assert packets[0].pts == timestamp and packets[0].time_base == Fraction(1, 90000)
    assert not encoder.encode(None)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["deadline", "not-idr", "missing-pps"])
async def test_forced_worker_output_must_contain_actual_idr_and_matching_parameters(
    monkeypatch, failure
):
    source = encode_source(profile="high", level="5.0", frames=1)
    output = encode_source(profile="high", frames=2)

    class WrongWorker:
        def __init__(self, _configuration):
            pass

        async def feed(self, *_args, **_kwargs):
            if failure == "deadline":
                return ((0, output[0][0]), (180000, output[1][0]))
            nals = split_annexb(output[0][0])
            if failure == "not-idr":
                return ((0, annexb([nal for nal in nals if nal[0] & 31 != 5]) + output[1][0]),)
            return ((0, annexb([nal for nal in nals if nal[0] & 31 != 8])),)

        async def close(self):
            pass

    monkeypatch.setattr(transcode, "VideoWorker", WrongWorker)
    adapter = AccessUnitAdapter(VideoConfiguration(320, 240, 15, "high", "3.1", 2000))
    try:
        # A worker claiming a P frame at the source-time deadline must fail even
        # when its initial output was a genuine independently decodable IDR.
        with pytest.raises(VideoTransformError, match="source-time IDR|current parameters"):
            await adapter.feed(source[0][0], 0, capture_epoch=0)
    finally:
        await adapter.close()
