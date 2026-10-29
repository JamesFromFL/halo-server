"""No native VFR tail, bounded source-clock calibration, and real-picture ownership."""

import asyncio
import time
from dataclasses import asdict

import av
import pytest
from test_media_h264 import encode_source

from halo_server.media.h264 import VideoConfiguration, parse_sps, split_annexb
from halo_server.media.transcode import AccessUnitAdapter, VideoWorker
from halo_server.media.transcode_worker import (
    RATE_COOLDOWN,
    RATE_SAMPLES,
    Encoder,
    RateCadence,
)

CONFIG = VideoConfiguration(320, 240, 30, "main", "3.1", 1000)


def test_calibration_needs_eight_intervals_and_half_second_without_holding_frames():
    rate = RateCadence(30)
    assert [rate.observe(index * 6000) for index in range(8)] == [30] * 8
    assert rate.observe(48000) == 15
    for index in range(9, 100):
        assert rate.observe(index * 6000) == 15
    assert len(rate.samples) <= RATE_SAMPLES
    assert rate.samples[-1] - rate.samples[0] >= 4 * 90000
    faster = RateCadence(30)
    for index in range(15):
        assert faster.observe(index * 3000) == 30 and not faster.calibrated
    assert faster.observe(45000) == 30 and faster.calibrated


def test_one_true_source_gap_resets_estimation_without_reducing_the_calibrated_rate():
    rate = RateCadence(30)
    for index in range(40):
        rate.observe(index * 6000)
    assert rate.target == 15
    after_gap = 39 * 6000 + 102600  # Actual1.14s source-clock gap, not an arrival gap.
    assert rate.observe(after_gap) == 15 and len(rate.samples) == 1
    for index in range(1, 40):
        assert rate.observe(after_gap + index * 6000) == 15


def test_later_change_requires_sustained_estimate_and_five_seconds_of_source_time():
    rate = RateCadence(30)
    choices = []
    for index in range(46):
        stamp = index * 6000
        choices.append((stamp, rate.observe(stamp)))
    first_change = rate.changed_at
    assert rate.target == 15
    for index in range(1, 230):
        stamp = 270000 + index * 3000
        before = rate.target
        value = rate.observe(stamp)
        if value != before:
            assert stamp - first_change >= RATE_COOLDOWN
            assert value == 30
        choices.append((stamp, value))
    assert rate.target == 30
    assert (
        len([1 for left, right in zip(choices, choices[1:], strict=False) if left[1] != right[1]])
        == 2
    )


def solid_frame():
    frame = av.VideoFrame(320, 240, "yuv420p")
    for index, plane in enumerate(frame.planes):
        plane.update(bytes([80 if index == 0 else 128]) * plane.buffer_size)
    return frame


def test_calibration_waits_for_returned_batch_origins_and_preserves_force_fifo():
    worker = Encoder(asdict(CONFIG))
    for index in range(7):
        assert len(worker.resolve(worker.encode(solid_frame(), index * 6000))) == 1
    packets = list(worker.encode(solid_frame(), 42000))
    packets.extend(worker.encode(solid_frame(), 48000))
    assert worker.rate.target == 15 and worker.nominal_fps == 30
    assert worker.refreshes == 0 and set(worker.pending) == {42000, 48000}
    assert [row[0] for row in worker.resolve(packets)] == [42000, 48000]
    worker.forces.extend((11, 12))
    first = worker.resolve(worker.encode(solid_frame(), 54000))
    assert worker.nominal_fps == 15 and worker.refreshes == 1
    assert first[0][:2] == (54000, 11) and list(worker.forces) == [12]
    second = worker.resolve(worker.encode(solid_frame(), 60000))
    assert second[0][:2] == (60000, 12) and not worker.forces and not worker.pending
    for _pts, _token, data in (*first, *second):
        assert {5, 7, 8} <= {nal[0] & 31 for nal in split_annexb(data)}
        independent = av.CodecContext.create("h264", "r")
        independent.thread_count = 1
        assert len(independent.decode(av.Packet(data))) == 1


def test_actual_native_cadence_change_keeps_decoder_warm_and_force_acknowledgments_exact():
    source = encode_source(fps=25, frames=170, profile="high", level="5.0") * 2
    worker = Encoder(asdict(CONFIG))
    decoder = worker.decoder
    output_decoder = av.CodecContext.create("h264", "r")
    output_decoder.thread_count = 1
    refreshes, acknowledgments = [], []
    token = 0
    for index, (data, _declared_pts) in enumerate(source):
        pts = index * 6000 if index < 45 else 270000 + (index - 45) * 3000
        request = index in {5, 8, 50, 135}
        if request:
            token += 1
        before = worker.refreshes
        result = worker.feed(data, pts, token if request else 0)
        assert len(result) == 1 and result[0][0] == pts
        assert not worker.pending and worker.decoder is decoder
        if worker.refreshes != before:
            refreshes.append((pts, worker.nominal_fps))
            assert {5, 7, 8} <= {nal[0] & 31 for nal in split_annexb(result[0][2])}
        if request:
            assert result[0][1] == token
            acknowledgments.append((pts, token))
        else:
            assert result[0][1] == 0
        assert len(output_decoder.decode(av.Packet(result[0][2]))) == 1
    assert worker.nominal_fps == 30 and worker.refreshes == 2
    assert refreshes[0] == (48000, 15)
    assert refreshes[1][1] == 30 and refreshes[1][0] - refreshes[0][0] >= RATE_COOLDOWN
    assert acknowledgments == [(30000, 1), (48000, 2), (285000, 3), (540000, 4)]


@pytest.mark.asyncio
@pytest.mark.parametrize("actual_fps", (15, 30))
async def test_false_sps25_does_not_choose_nominal_and_every_real_picture_is_immediate(actual_fps):
    # Declare25fps in the real bitstream while supplying actual15/30 presentation PTS.
    source = encode_source(fps=25, frames=70, profile="high", level="5.0")
    worker = VideoWorker(CONFIG)
    decoder = av.CodecContext.create("h264", "r")
    decoder.thread_count = 1
    rates, idrs = [], []
    try:
        for index, (data, _declared_pts) in enumerate(source):
            pts = index * 90000 // actual_fps
            result = await worker.feed(data, pts)
            assert len(result) == 1 and result[0][0] == pts
            assert worker.last_pending_frames == 0
            nals = split_annexb(result[0][1])
            if any(nal[0] & 31 == 5 for nal in nals):
                idrs.append(pts)
                rates.append(parse_sps(next(nal for nal in nals if nal[0] & 31 == 7)).frame_rate)
            frames = decoder.decode(av.Packet(result[0][1]))
            assert len(frames) == 1
        assert rates[0] == 30 and rates[-1] == actual_fps
        assert len(set(rates)) == (2 if actual_fps == 15 else 1)
        assert len(idrs) == (4 if actual_fps == 15 else 2)
    finally:
        await worker.close()
    assert worker.pid is None


@pytest.mark.asyncio
async def test_irregular_source_pts_and_original_receipts_survive_calibration_and_epoch_reset():
    source = encode_source(fps=25, frames=50, profile="high", level="5.0")
    adapter = AccessUnitAdapter(CONFIG)
    steps = (3600, 5400, 7200, 5400, 9000, 5400)  # 40–100ms, average15fps.
    pts = 0
    try:
        for index, (data, _declared_pts) in enumerate(source):
            receipt = 100 + index * 0.012
            result = await adapter.feed(data, pts, capture_epoch=8, received_at=receipt)
            assert len(result) == 1
            assert (result[0].timestamp, result[0].received_at, result[0].capture_epoch) == (
                pts,
                receipt,
                8,
            )
            assert adapter._worker.last_pending_frames == 0 and not adapter._source_receipts
            pts += steps[index % len(steps)]
        retired = adapter._worker
        reset = await adapter.feed(source[0][0], 0, capture_epoch=9, received_at=200)
        assert retired.pid is None and adapter._worker is not retired
        assert reset[0].is_keyframe and reset[0].timestamp == 0
        assert reset[0].capture_epoch == 9 and reset[0].received_at == 200
    finally:
        await adapter.close()
    assert adapter._worker is None


@pytest.mark.asyncio
async def test_repeated_eight_hundred_ms_arrival_gaps_do_not_age_a_native_tail():
    source = encode_source(fps=25, frames=90, profile="high", level="5.0")
    worker = VideoWorker(CONFIG)
    await worker.start()
    started = time.monotonic()
    outputs = []
    try:
        for index, (data, _declared_pts) in enumerate(source):
            source_time = index / 15
            arrival = (int(source_time // 2) + 1) * 2 if source_time % 2 >= 1.25 else source_time
            await asyncio.sleep(max(0, started + arrival - time.monotonic()))
            receipt = time.monotonic()
            result = await worker.feed(data, index * 6000)
            assert len(result) == 1 and result[0][0] == index * 6000
            assert time.monotonic() - receipt < 0.500
            assert worker.last_pending_frames == 0
            outputs.extend(result)
        assert len(outputs) == len(source)
    finally:
        await worker.close()
    assert worker.pid is None
