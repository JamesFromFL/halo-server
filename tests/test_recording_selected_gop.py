"""Selected HomeKit keyframe intervals constrain actual copy and native output."""

from fractions import Fraction
from io import BytesIO

import av
import pytest
from test_homekit_audio_codec import source_audio
from test_homekit_recording_codec import inspect_recording, parameter_sets, sample
from test_media_h264 import encode_source

from halo_server.media.h264 import VideoConfiguration, split_annexb
from halo_server.media.recording import (
    RecordingAudioConfiguration,
    RecordingConfiguration,
    RecordingError,
    RecordingWorker,
)


async def render(source, configuration):
    sps, pps = parameter_sets(source[0][0])
    worker = RecordingWorker(
        configuration, video_sps=sps, video_pps=pps, capture_epoch=1, config_epoch=1
    )
    output = []
    inputs = [sample(payload, stamp, number) for number, (payload, stamp) in enumerate(source)]
    if configuration.audio is not None:
        duration = Fraction(source[-1][1], 90000) + Fraction(1, configuration.video.fps)
        inputs.extend(
            sample(payload, index * 960, index, kind="audio")
            for index, payload in enumerate(source_audio(count=round(duration * 50)))
        )
    inputs.sort(key=lambda item: (item.received_at, item.kind))
    try:
        for item in inputs:
            output.extend(await worker.feed(item))
        output.extend(await worker.finish())
    finally:
        await worker.close()
    return output


def inspect_video(output):
    movie = av.open(BytesIO(b"".join(item.payload for item in output)))
    keys, vcl = [], []
    for packet in movie.demux(video=0):
        if not packet.size:
            continue
        if packet.is_keyframe:
            keys.append(packet.pts * packet.time_base)
        payload, offset = bytes(packet), 0
        while offset < len(payload):
            count = int.from_bytes(payload[offset : offset + 4], "big")
            offset += 4
            nal = payload[offset : offset + count]
            if nal[0] & 31 in {1, 5}:
                vcl.append(nal)
            offset += count
        assert all(not frame.is_corrupt for frame in packet.decode())
    assert len(keys) >= 3
    return keys, vcl


@pytest.mark.asyncio
async def test_compatible_one_second_GOP_is_copied_exactly():
    source = encode_source(
        width=1280,
        height=720,
        fps=15,
        profile="high",
        level="4.0",
        frames=60,
        force=range(0, 60, 15),
    )
    configuration = RecordingConfiguration(
        VideoConfiguration(1280, 720, 15, "high", "4.0", 2000), i_frame_interval_ms=1000
    )
    output = await render(source, configuration)
    keys, copied = inspect_video(output)
    assert all(b - a <= 1 for a, b in zip(keys, keys[1:], strict=False))
    assert copied == [
        nal for payload, _ in source for nal in split_annexb(payload) if nal[0] & 31 in {1, 5}
    ]
    assert all(item.duration_seconds <= 4 for item in output[1:])


@pytest.mark.asyncio
@pytest.mark.parametrize("fps", [15, 24, 30])
async def test_longer_source_GOP_adapts_to_selected_one_second_interval(fps):
    source = encode_source(
        width=1280, height=720, fps=fps, profile="high", level="4.0", frames=fps * 4
    )
    configuration = RecordingConfiguration(
        VideoConfiguration(1280, 720, fps, "high", "4.0", 2000), i_frame_interval_ms=1000
    )
    output = await render(source, configuration)
    keys, adapted = inspect_video(output)
    assert all(b - a <= 1 for a, b in zip(keys, keys[1:], strict=False))
    assert adapted != [
        nal for payload, _ in source for nal in split_annexb(payload) if nal[0] & 31 in {1, 5}
    ]
    assert all(0 < item.duration_seconds <= 4 for item in output[1:])


@pytest.mark.asyncio
async def test_copy_cannot_later_exceed_the_selected_keyframe_interval():
    source = encode_source(
        width=1280,
        height=720,
        fps=15,
        profile="high",
        level="4.0",
        frames=75,
        force=(15, 30),
    )
    sps, pps = parameter_sets(source[0][0])
    worker = RecordingWorker(
        RecordingConfiguration(
            VideoConfiguration(1280, 720, 15, "high", "4.0", 2000), i_frame_interval_ms=1000
        ),
        video_sps=sps,
        video_pps=pps,
        capture_epoch=1,
        config_epoch=1,
    )
    try:
        for index, (payload, stamp) in enumerate(source[:46]):
            await worker.feed(sample(payload, stamp, index))
        with pytest.raises(RecordingError):
            await worker.feed(sample(source[46][0], source[46][1], 46))
        assert worker.pid is None and not worker.pending_resources
    finally:
        await worker.close()


@pytest.mark.parametrize("interval", [True, 0, 66, 4001])
def test_keyframe_interval_configuration_rejects_unrepresentable_values(interval):
    with pytest.raises(RecordingError):
        RecordingConfiguration(
            VideoConfiguration(1280, 720, 15, "high", "4.0", 2000), i_frame_interval_ms=interval
        )


def test_minimum_interval_allows_exactly_one_picture():
    config = RecordingConfiguration(
        VideoConfiguration(1280, 720, 15, "high", "4.0", 2000), i_frame_interval_ms=67
    )
    assert Fraction(config.i_frame_interval_ms, 1000) >= Fraction(1, config.video.fps)


@pytest.mark.asyncio
@pytest.mark.parametrize("fps", [15, 24, 30])
@pytest.mark.parametrize("audio", [False, True])
async def test_minimum_representable_interval_emits_actual_IDR_every_picture(fps, audio):
    source = encode_source(
        width=1280, height=720, fps=fps, profile="high", level="4.0", frames=fps * 2
    )
    interval = (1000 + fps - 1) // fps
    output = await render(
        source,
        RecordingConfiguration(
            VideoConfiguration(1280, 720, fps, "high", "4.0", 2000),
            RecordingAudioConfiguration() if audio else None,
            i_frame_interval_ms=interval,
        ),
    )
    keys, vcl = inspect_video(output)
    assert len(keys) == fps * 2
    assert vcl and all(nal[0] & 31 == 5 for nal in vcl)
    assert all(b - a <= Fraction(interval, 1000) for a, b in zip(keys, keys[1:], strict=False))
    _, counts = inspect_recording(output, audio=audio, expected_width=1280, expected_height=720)
    assert counts["video"] == fps * 2
    assert counts["audio"] > 0 if audio else counts["audio"] == 0
