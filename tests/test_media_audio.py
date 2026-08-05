import numpy as np
import pytest

from halo_server.media.audio import (
    OPUS_FRAME_SAMPLES,
    AudioNormalizationError,
    NormalizedOpusEncoder,
    opus_frame_duration_ms,
)


def test_normalizer_downmixes_stereo_to_fixed_20ms_mono_opus():
    encoder = NormalizedOpusEncoder()
    stereo = np.zeros((OPUS_FRAME_SAMPLES, 2), dtype="<i2").tobytes()

    packets = encoder.encode(stereo, 2, 48_000)

    assert len(packets) == 1
    assert packets[0].rtp_timestamp == 0
    assert opus_frame_duration_ms(packets[0].payload) == 20


def test_normalizer_accumulates_partial_pcm_into_complete_opus_frames():
    encoder = NormalizedOpusEncoder()
    half_frame = np.zeros(OPUS_FRAME_SAMPLES // 2, dtype="<i2").tobytes()

    assert encoder.encode(half_frame, 1, 48_000) == ()
    packets = encoder.encode(half_frame, 1, 48_000)

    assert len(packets) == 1
    assert packets[0].rtp_timestamp == 0


def test_normalizer_resamples_supported_pcm_rate():
    encoder = NormalizedOpusEncoder()
    pcm = np.zeros(320, dtype="<i2").tobytes()

    first = encoder.encode(pcm, 1, 16_000)
    second = encoder.encode(pcm, 1, 16_000)

    assert len(first) + len(second) >= 1
    assert all(opus_frame_duration_ms(packet.payload) == 20 for packet in first + second)


@pytest.mark.parametrize(
    ("payload", "channels", "rate", "message"),
    [
        (b"", 1, 48_000, "non-empty"),
        (b"\x00", 1, 48_000, "aligned"),
        (b"\x00\x00", 3, 48_000, "one or two"),
        (b"\x00\x00", 1, 0, "rate"),
    ],
)
def test_normalizer_rejects_invalid_pcm(payload, channels, rate, message):
    encoder = NormalizedOpusEncoder()

    with pytest.raises(AudioNormalizationError, match=message):
        encoder.encode(payload, channels, rate)


def test_normalizer_rejects_mid_session_format_change():
    encoder = NormalizedOpusEncoder()
    encoder.encode(np.zeros(960, dtype="<i2").tobytes(), 1, 48_000)

    with pytest.raises(AudioNormalizationError, match="changed"):
        encoder.encode(np.zeros(320, dtype="<i2").tobytes(), 1, 16_000)


@pytest.mark.parametrize(
    ("packet", "expected"),
    [
        (b"\xbb\x03", 20.0),
        (b"\x58", 60.0),
        (b"\xfc", 20.0),
        (b"", None),
    ],
)
def test_opus_frame_duration_from_toc(packet, expected):
    assert opus_frame_duration_ms(packet) == expected
