import pytest

from halo_server.media.profiles import (
    EncodedMediaProfile,
    TransportMode,
    opus_frame_duration_ms,
    select_transport,
)


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


def test_auto_falls_back_when_encoded_probe_fails():
    assert select_transport(TransportMode.AUTO, None) is TransportMode.TRANSCODE
    assert select_transport(TransportMode.PASSTHROUGH, None) is None
    assert select_transport(TransportMode.HYBRID, None) is None


def test_auto_selects_passthrough_for_native_20_ms_opus():
    profile = EncodedMediaProfile(opus_frame_duration_ms=20)

    assert select_transport(TransportMode.AUTO, profile) is TransportMode.PASSTHROUGH


def test_auto_normalizes_non_20_ms_opus_without_transcoding_video():
    profile = EncodedMediaProfile(opus_frame_duration_ms=60)

    assert select_transport(TransportMode.AUTO, profile) is TransportMode.HYBRID
    assert select_transport(TransportMode.HYBRID, profile) is TransportMode.HYBRID
