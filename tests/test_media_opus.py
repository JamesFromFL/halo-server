"""RFC packet framing cross-checked with the independent official libopus API."""

import ctypes
import ctypes.util
import random
from dataclasses import replace

import pytest

from halo_server.media.opus import (
    AudioConfiguration,
    OpusError,
    OriginalOpusBuffer,
    parse_opus,
    repacketize,
)


def oracle():
    name = ctypes.util.find_library("opus")
    if not name:
        pytest.skip("Independent libopus packet parser is unavailable")
    lib = ctypes.CDLL(name)
    lib.opus_packet_parse.argtypes = [
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_ubyte),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_int16),
        ctypes.POINTER(ctypes.c_int),
    ]
    lib.opus_packet_parse.restype = ctypes.c_int

    def inspect(payload):
        toc, sizes, pointers, offset = (
            ctypes.c_ubyte(),
            (ctypes.c_int16 * 48)(),
            (ctypes.c_void_p * 48)(),
            ctypes.c_int(),
        )
        count = lib.opus_packet_parse(
            payload, len(payload), ctypes.byref(toc), pointers, sizes, ctypes.byref(offset)
        )
        if count < 0:
            return None
        return toc.value & 252, tuple(
            ctypes.string_at(pointers[index], sizes[index]) for index in range(count)
        )

    return inspect


@pytest.mark.parametrize(
    "config,expected", [(0, 480), (3, 2880), (12, 480), (15, 960), (16, 120), (31, 960)]
)
def test_all_toc_duration_families(config, expected):
    parsed = parse_opus(bytes((config << 3,)) + b"frame")
    assert parsed.frame_samples == expected and parsed.duration_samples == expected


def test_rfc_parser_matches_official_parser_for_vbr_long_sizes_padding_and_random_shapes():
    inspect = oracle()
    packets = [
        b"\xf8\xff\xfe",
        b"\xfb\x43\xff\x00" + b"ab" * 3 + bytes(254),
        b"\xfa\xfc\x02" + b"a" * 260 + b"last",
        b"\xfb\x83\x01\x02abbccc",
        b"\x83\x30" + b"a" * 48,
    ]
    rng = random.Random(1984)
    packets.extend(rng.randbytes(rng.randrange(1, 150)) for _ in range(2500))
    for payload in packets:
        expected = inspect(payload)
        try:
            parsed = parse_opus(payload)
        except OpusError:
            assert expected is None
        else:
            assert expected == (parsed.toc, parsed.frames)


@pytest.mark.parametrize("lengths", [(1,), (4, 4), (4, 8), (3, 3, 3), (1, 260, 1275)])
def test_repacketization_preserves_actual_frame_bytes_and_matches_official_parser(lengths):
    inputs = [bytes((0x90,)) + bytes((index,)) * length for index, length in enumerate(lengths)]
    payload = repacketize(inputs)
    parsed = parse_opus(payload)
    assert parsed.frames == tuple(data[1:] for data in inputs)
    assert oracle()(payload) == (parsed.toc, parsed.frames)
    assert parsed.duration_samples == len(lengths) * 480


@pytest.mark.parametrize(
    "payload",
    [b"", b"\xfb", b"\xfb\x00", b"\xf9a", b"\xfa\xff", b"\xfb\x3f", b"\x18" + b"a" * 1276],
)
def test_malformed_packets_fail(payload):
    with pytest.raises(OpusError):
        parse_opus(payload)


def test_repacketization_never_concatenates_incompatible_toc_or_excess_duration():
    with pytest.raises(OpusError):
        repacketize([b"\xf8a", b"\xd8b"])
    with pytest.raises(OpusError):
        repacketize([b"\xf8a"] * 7)
    with pytest.raises(OpusError):
        parse_opus(b"\xfb\x41" + b"\xff" * 65533)


@pytest.mark.parametrize(
    "field,value",
    [
        ("sample_rate", 48000),
        ("packet_time", 10),
        ("max_bit_rate", 5),
        ("gain", 101),
        ("channels", 2),
        ("clock_mode", "rfc7587"),
        ("payload_type", 95),
        ("rtcp_interval", float("nan")),
    ],
)
def test_unqualified_configuration_is_rejected(field, value):
    with pytest.raises(OpusError):
        replace(AudioConfiguration(16000, 20, "vbr", 24), **{field: value})


def test_original_lane_is_audio_only_bounded_fresh_and_epoch_scoped():
    now = [0.0]
    buffer = OriginalOpusBuffer(clock=lambda: now[0])
    first = buffer.append_audio(b"\xf8\xff\xfe", 42)
    read = buffer.read(after_sequence=None, timeout=0)
    assert read.packets == (first,) and first.payload == b"\xf8\xff\xfe"
    for index in range(600):
        now[0] += 0.001
        buffer.append_audio(b"\xf8\xff\xfe", index * 960)
    assert len(buffer.snapshot().packets) == 512
    assert buffer.read(after_sequence=first.sequence, timeout=0).resynchronized
    buffer.clear(capture_epoch=17)
    assert buffer.read(after_sequence=first.sequence, timeout=0).capture_epoch == 17
    assert not buffer.snapshot().packets
    packet = buffer.append_audio(b"\xf8\xff\xfe", 123)
    assert packet.capture_epoch == 17
    now[0] += 2.001
    assert not buffer.read(after_sequence=None, timeout=0).packets


def test_original_lane_accounts_large_valid_packets_before_retaining_them():
    buffer = OriginalOpusBuffer(clock=lambda: 0)
    # 48 x 2.5ms frames, each1275bytes: valid Opus framing, never directly RTP-sized.
    packet = b"\x83\x30" + b"a" * (48 * 1275)
    for index in range(25):
        buffer.append_audio(packet, index * 5760)
    assert buffer.snapshot().payload_bytes <= 1024 * 1024
