"""Actual native bitstreams and bounded SPS/Annex-B admission vectors."""

from dataclasses import replace
from fractions import Fraction

import av
import pytest

from halo_server.media.h264 import (
    MAX_ACCESS_UNIT_BYTES,
    H264Error,
    VideoConfiguration,
    annexb,
    compatible,
    parse_sps,
    pps_sequence_id,
    split_annexb,
)


def encode_source(
    *, width=320, height=240, fps=15, profile="baseline", level="3.1", frames=3, bframes=0, force=()
):
    encoder = av.CodecContext.create("libx264", "w")
    encoder.width, encoder.height = width, height
    encoder.pix_fmt, encoder.time_base, encoder.framerate = (
        "yuv420p",
        Fraction(1, fps),
        Fraction(fps),
    )
    encoder.bit_rate, encoder.thread_count = 1000000, 1
    encoder.gop_size, encoder.max_b_frames = fps * 2, bframes
    encoder.options = {
        "profile": profile,
        "level": level,
        "preset": "veryfast",
        "x264-params": f"bframes={bframes}:scenecut=0:repeat-headers=1",
    }
    if not bframes:
        encoder.options = encoder.options | {"tune": "zerolatency"}
    packets = []
    for index in range(frames):
        frame = av.VideoFrame(width, height, "yuv420p")
        for number, plane in enumerate(frame.planes):
            plane.update(bytes([80 + index if number == 0 else 128]) * plane.buffer_size)
        frame.pts, frame.time_base = index, Fraction(1, fps)
        if index in force:
            frame.pict_type = av.video.frame.PictureType.I
        packets += encoder.encode(frame)
    packets += encoder.encode(None)
    return tuple((bytes(packet), int(packet.pts * packet.time_base * 90000)) for packet in packets)


def source_sps(payload):
    return parse_sps(next(nal for nal in split_annexb(payload) if nal[0] & 31 == 7))


@pytest.mark.parametrize("profile", ["baseline", "main", "high"])
def test_real_native_sps_proves_profile_geometry_level_and_frame_timing(profile):
    payload, _ = encode_source(profile=profile, frames=1)[0]
    info = source_sps(payload)
    assert (info.profile, info.width, info.height, info.level_idc) == (profile, 320, 240, 31)
    assert info.frame_rate == 15 and info.progressive and info.bit_depth == 8
    assert info.max_reorder_frames == 0
    assert compatible(info, VideoConfiguration(320, 240, 15, profile, "3.1", 2000))
    assert not compatible(info, VideoConfiguration(640, 480, 15, profile, "3.1", 2000))
    pps = next(nal for nal in split_annexb(payload) if nal[0] & 31 == 8)
    assert pps_sequence_id(pps) == info.sequence_id


def test_real_bframe_source_is_not_qualified_for_compressed_copy():
    payload, _ = encode_source(profile="high", frames=8, bframes=2)[0]
    info = source_sps(payload)
    assert info.max_reorder_frames == 2
    assert not compatible(info, VideoConfiguration(320, 240, 15, "high", "3.1", 2000))


@pytest.mark.parametrize(
    "change",
    [
        {"width": 1},
        {"width": 321},
        {"height": 1082},
        {"fps": 31},
        {"fps": True},
        {"profile": "extended"},
        {"level": "5.0"},
        {"max_bit_rate": 0},
        {"payload_type": 95},
        {"rtcp_interval": float("nan")},
        {"mtu": 200},
    ],
)
def test_selected_output_rejects_unsafe_or_unimplemented_choices(change):
    with pytest.raises(H264Error):
        replace(VideoConfiguration(320, 240, 15, "baseline", "3.1", 1000), **change)


def test_impossible_hap_cross_product_is_rejected_instead_of_relabelled():
    with pytest.raises(H264Error, match="level"):
        VideoConfiguration(1920, 1080, 30, "high", "3.1", 2000)
    assert VideoConfiguration(1920, 1080, 30, "high", "4.0", 2000)


@pytest.mark.parametrize(
    "payload",
    [b"", b"bare-nal", b"\0\0\1", b"\0\0\1\xe7bad", b"\0\0\1\x78bad", b"junk\0\0\1\x65good"],
)
def test_annexb_refuses_missing_empty_forbidden_and_aggregated_nals(payload):
    with pytest.raises(H264Error):
        split_annexb(payload)


def test_annexb_count_and_byte_limits_are_finite():
    with pytest.raises(H264Error):
        split_annexb(b"\0\0\1\x09\x80" * 257)
    with pytest.raises(H264Error):
        split_annexb(b"\0\0\1\x65" + b"a" * MAX_ACCESS_UNIT_BYTES)


def test_annexb_roundtrip_preserves_the_compressed_nal_bytes():
    payload, _ = encode_source(frames=1)[0]
    nals = split_annexb(payload)
    assert split_annexb(annexb(nals)) == nals


def _ue(value):
    bits = bin(value + 1)[2:]
    return "0" * (len(bits) - 1) + bits


def synthetic_sps(*, width_mbs=20, height_mbs=15, crop=(0, 0, 0, 0), high_scaling=False):
    bits = f"{100 if high_scaling else 66:08b}" + "00000000" + "00011111" + _ue(0)
    if high_scaling:
        bits += _ue(1) + _ue(0) + _ue(0) + "01" + "1" + "1" * 16 + "0" * 7
    bits += _ue(0) + _ue(0) + _ue(0) + _ue(1) + "0"
    bits += _ue(width_mbs - 1) + _ue(height_mbs - 1) + "11"
    bits += "1" + "".join(_ue(value) for value in crop)
    bits += "01"
    bits += "0" * ((-len(bits)) % 8)
    rbsp = int(bits, 2).to_bytes(len(bits) // 8, "big")
    escaped = bytearray()
    zeros = 0
    for byte in rbsp:
        if zeros >= 2 and byte <= 3:
            escaped.append(3)
            zeros = 0
        escaped.append(byte)
        zeros = zeros + 1 if byte == 0 else 0
    return b"\x67" + bytes(escaped)


def test_sps_cropping_uses_chroma_crop_units():
    info = parse_sps(synthetic_sps(crop=(1, 1, 1, 1)))
    assert (info.width, info.height) == (316, 236)
    assert info.frame_rate is None


def test_high_profile_scaling_lists_are_parsed_with_finite_bounds():
    info = parse_sps(synthetic_sps(width_mbs=8, height_mbs=8, high_scaling=True))
    assert (info.profile, info.width, info.height) == ("high", 128, 128)


@pytest.mark.parametrize(
    "payload",
    [
        b"\x67\x42\x00\x1f" + b"\x00" * 8,
        b"\x67\x42\x00\x1f\x00\x00\x03",
        b"\x67\x42\x00\x1f\x00\x00\x03\x04",
    ],
)
def test_sps_truncation_exp_golomb_and_emulation_abuse_are_rejected(payload):
    with pytest.raises(H264Error):
        parse_sps(payload)


def test_sps_pixel_bomb_and_impossible_crop_fail_before_native_decode():
    with pytest.raises(H264Error, match="geometry"):
        parse_sps(synthetic_sps(width_mbs=512, height_mbs=512))
    with pytest.raises(H264Error, match="geometry"):
        parse_sps(synthetic_sps(crop=(1000, 0, 0, 0)))


def test_start_code_flood_is_rejected_without_materializing_millions_of_matches():
    import tracemalloc

    payload = b"\x00\x00\x01\x65" * (MAX_ACCESS_UNIT_BYTES // 4)
    tracemalloc.start()
    try:
        with pytest.raises(H264Error, match="NAL count"):
            split_annexb(payload)
        _, peak = tracemalloc.get_traced_memory()
        assert peak < 512 * 1024
    finally:
        tracemalloc.stop()
