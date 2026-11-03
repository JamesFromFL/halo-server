"""Real H.264 decoder regressions for the rolling live buffer's header cache."""

from fractions import Fraction

import av
import pytest

from halo_server.media.buffer import EncodedMediaBuffer
from halo_server.media.h264 import annexb, split_annexb


def encode_source(*, count=32, width=320, height=240, repeat_parameters=False, keyint=2):
    encoder = av.CodecContext.create("libx264", "w")
    encoder.width, encoder.height, encoder.pix_fmt = width, height, "yuv420p"
    encoder.time_base, encoder.framerate = Fraction(1, 90000), Fraction(1)
    encoder.thread_count, encoder.max_b_frames = 1, 0
    encoder.options = {
        "preset": "ultrafast",
        "tune": "zerolatency",
        "x264-params": f"keyint={keyint}:scenecut=0:repeat-headers=1:bframes=0",
    }
    encoder.open()
    payloads = []
    for index in range(count):
        frame = av.VideoFrame(width, height, "yuv420p")
        for number, plane in enumerate(frame.planes):
            plane.update(bytes([70 + index if number == 0 else 128]) * plane.buffer_size)
        frame.pts, frame.time_base = index * 90000, Fraction(1, 90000)
        encoded = encoder.encode(frame)
        assert len(encoded) == 1
        nals = split_annexb(bytes(encoded[0]))
        if index and not repeat_parameters:
            nals = tuple(nal for nal in nals if nal[0] & 31 not in {7, 8})
        payloads.append(annexb(nals))
    assert not encoder.encode(None)
    return payloads


def decoder():
    result = av.CodecContext.create("h264", "r")
    result.thread_count = 1
    return result


def decode_packets(codec, packets):
    return [frame for packet in packets for frame in codec.decode(av.Packet(packet.payload))]


@pytest.mark.parametrize("repeat_parameters", [False, True])
def test_new_and_continuing_readers_decode_after_original_headers_expire(repeat_parameters):
    source = encode_source(repeat_parameters=repeat_parameters)
    buffer = EncodedMediaBuffer()
    continuous, reference = decoder(), decoder()
    cursor = None
    for second, payload in enumerate(source):
        expected = reference.decode(av.Packet(payload))
        packet = buffer.append_video(payload, second * 90000, received_at=100 + second)
        batch = buffer.read(after_sequence=cursor, timeout=0)
        assert [item.sequence for item in batch.packets] == [packet.sequence]
        assert not batch.resynchronized
        actual = decode_packets(continuous, batch.packets)
        assert len(actual) == len(expected) == 1
        assert actual[0].to_ndarray().tobytes() == expected[0].to_ndarray().tobytes()
        cursor = packet.sequence
        if second >= 13:
            assert all(item.sequence > 1 for item in buffer._packets)
            assert buffer.ready
            initial = buffer.read(after_sequence=None, timeout=0)
            snapshot = buffer.snapshot()
            assert snapshot.packets == initial.packets
            assert snapshot.duration_seconds <= 12
            fresh = decode_packets(decoder(), initial.packets)
            assert len(fresh) == len(initial.packets)
            assert fresh[-1].to_ndarray().tobytes() == expected[0].to_ndarray().tobytes()
            first = initial.packets[0]
            assert first.h264_nal_types >= {5, 7, 8}
            assert first.received_at == 100 + first.rtp_timestamp / 90000
            assert first.capture_epoch == packet.capture_epoch
    assert not continuous.decode(None)
    assert not reference.decode(None)


def test_decoder_continuity_without_any_retained_random_access_picture_and_gap_recovery():
    source = encode_source(count=20, keyint=8)
    buffer = EncodedMediaBuffer(max_duration=2)
    continuous = decoder()
    cursor = None
    stale_cursor = None
    for second, payload in enumerate(source):
        packet = buffer.append_video(payload, second * 90000, received_at=second)
        batch = buffer.read(after_sequence=cursor, timeout=0)
        assert len(decode_packets(continuous, batch.packets)) == 1
        assert not batch.resynchronized
        cursor = packet.sequence
        if second == 0:
            stale_cursor = cursor
        if second == 7:
            assert not buffer.ready
            assert not buffer.read(after_sequence=None, timeout=0).packets
        if second == 8:
            resumed = buffer.read(after_sequence=stale_cursor, timeout=0)
            assert resumed.resynchronized
            assert resumed.packets[0].sequence == packet.sequence
            assert len(decode_packets(decoder(), resumed.packets)) == 1


def test_replaced_sps_waits_for_fresh_pps_then_decodes_the_new_configuration():
    old = encode_source(count=2)
    new = encode_source(count=2, width=160, height=120)
    buffer = EncodedMediaBuffer()
    continuous = decoder()
    buffer.append_video(old[0], 0, received_at=0)
    first = buffer.read(after_sequence=None, timeout=0)
    assert decode_packets(continuous, first.packets)[0].width == 320
    nals = split_annexb(new[0])
    sps = annexb(nal for nal in nals if nal[0] & 31 == 7)
    pps = annexb(nal for nal in nals if nal[0] & 31 == 8)
    picture = annexb(nal for nal in nals if nal[0] & 31 not in {7, 8})
    buffer.append_video(sps + picture, 90000, received_at=1)
    assert not buffer.ready
    assert not buffer.read(after_sequence=first.packets[-1].sequence, timeout=0).packets
    recovered = buffer.append_video(pps + picture, 180000, received_at=2)
    batch = buffer.read(after_sequence=first.packets[-1].sequence, timeout=0)
    assert batch.resynchronized
    assert batch.packets[0].sequence == recovered.sequence
    assert decode_packets(continuous, batch.packets)[0].width == 160
    assert decode_packets(decoder(), batch.packets)[0].width == 160
    buffer.append_video(new[1], 270000, received_at=3)
    live = buffer.read(after_sequence=recovered.sequence, timeout=0)
    assert not live.resynchronized
    assert decode_packets(continuous, live.packets)[0].width == 160
