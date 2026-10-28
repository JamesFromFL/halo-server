from dataclasses import replace

import pytest

from halo_server.media.formats import AudioFormat, SourceFormat, VideoFormat


def test_source_format_leaves_unmeasured_properties_unknown():
    assert SourceFormat().video is None
    assert SourceFormat().audio is None
    assert VideoFormat("h264").width is None
    assert AudioFormat("opus").rtp_clock_rate is None
    assert AudioFormat("opus", sample_rate=24000, rtp_clock_rate=48000).sample_rate == 24000


@pytest.mark.parametrize(
    "kwargs",
    [
        {"width": True},
        {"height": 8193},
        {"frame_rate_num": 30},
        {"frame_rate_num": 241, "frame_rate_den": 1},
        {"profile": "unsafe\n"},
    ],
)
def test_video_format_rejects_invalid_or_unbounded_observations(kwargs):
    with pytest.raises(ValueError):
        VideoFormat("h264", **kwargs)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"channels": 3},
        {"sample_rate": True},
        {"rtp_clock_rate": 192001},
        {"packet_time_ms": float("nan")},
        {"packet_time_ms": 121},
        {"packet_time_ms": True},
    ],
)
def test_audio_format_rejects_invalid_or_unbounded_observations(kwargs):
    with pytest.raises(ValueError):
        AudioFormat("opus", **kwargs)


@pytest.mark.parametrize("field", ["capture_attempt", "config_generation"])
@pytest.mark.parametrize("value", [0, True, 2**53])
def test_attempt_and_codec_generation_are_safe_positive_integers(field, value):
    with pytest.raises(ValueError):
        replace(SourceFormat(), **{field: value})
