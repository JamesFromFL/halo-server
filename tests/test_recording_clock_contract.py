"""Actual source sender reports preserve a common A/V origin across native mux."""

from dataclasses import replace
from fractions import Fraction
from io import BytesIO

import av
import pytest
from test_homekit_audio_codec import source_audio
from test_homekit_recording_codec import native_video, parameter_sets, sample

from halo_server.media.h264 import VideoConfiguration
from halo_server.media.recording import (
    RecordingAudioConfiguration,
    RecordingConfiguration,
    RecordingError,
    RecordingWorker,
)
from halo_server.media.ring_media import SourceClockObservation


def source_clocks(offset):
    base = 3_900_000_000 * 2**32
    return [
        SourceClockObservation("video", 11, 0, base, 10.0, 1),
        SourceClockObservation("audio", 22, 0, base + round(offset * 2**32), 10.0, 1),
    ]


@pytest.mark.asyncio
async def test_source_sender_reports_preserve_known_offset_and_freeze_late_anchor_updates():
    video = native_video(1280, 720, 15, seconds=4)
    audio = source_audio(channels=2, count=200)
    sps, pps = parameter_sets(video[0][0])
    observations = source_clocks(0.1)
    worker = RecordingWorker(
        RecordingConfiguration(
            VideoConfiguration(1280, 720, 15, "high", "4.0", 2000), RecordingAudioConfiguration()
        ),
        video_sps=sps,
        video_pps=pps,
        source_clocks=observations,
        capture_epoch=1,
        config_epoch=1,
    )
    # Later receiver observations cannot rebase the origin of an active movie.
    observations[1] = replace(observations[1], sender_ntp=observations[1].sender_ntp + 20 * 2**32)
    inputs = [sample(payload, stamp, index + 1) for index, (payload, stamp) in enumerate(video)]
    inputs.extend(
        sample(payload, index * 960, index + 1, kind="audio") for index, payload in enumerate(audio)
    )
    output = []
    try:
        # Receipts deliberately imply no offset; source clocks must win.
        for item in sorted(inputs, key=lambda item: (item.received_at, item.kind)):
            output.extend(await worker.feed(item))
        output.extend(await worker.finish())
    finally:
        await worker.close()
    movie_bytes = b"".join(packet.payload for packet in output)
    with av.open(BytesIO(movie_bytes)) as movie:
        first_video = next(movie.decode(video=0))
        video_start = first_video.pts * first_video.time_base
    with av.open(BytesIO(movie_bytes)) as movie:
        frames = list(movie.decode(audio=0))
        assert frames
        audio_start = frames[0].pts * frames[0].time_base
    assert abs((audio_start - video_start) - Fraction(1, 10)) <= Fraction(1, 24000)


@pytest.mark.asyncio
async def test_impossible_cross_track_clock_offset_is_rejected_without_retained_worker():
    video = native_video(1280, 720, 15, seconds=1)
    sps, pps = parameter_sets(video[0][0])
    worker = RecordingWorker(
        RecordingConfiguration(
            VideoConfiguration(1280, 720, 15, "high", "4.0", 2000), RecordingAudioConfiguration()
        ),
        video_sps=sps,
        video_pps=pps,
        source_clocks=source_clocks(20),
        capture_epoch=1,
        config_epoch=1,
    )
    try:
        await worker.feed(sample(video[0][0], video[0][1], 1))
        with pytest.raises(RecordingError):
            await worker.feed(sample(source_audio(count=1)[0], 0, 1, kind="audio"))
    finally:
        await worker.close()
    assert worker.pid is None and not worker.pending_resources
