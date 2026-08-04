import threading
import time

import pytest

from halo_server.media.buffer import (
    EncodedMediaBuffer,
    MediaBufferError,
    MediaStreamKind,
    h264_nal_types,
)

SPS = b"\x00\x00\x00\x01\x67\x64\x00\x32"
PPS = b"\x00\x00\x00\x01\x68\xee\x3c\x80"
IDR = b"\x00\x00\x00\x01\x65\x88\x84"
P_FRAME = b"\x00\x00\x00\x01\x41\x9a"


def test_h264_nal_parser_handles_three_and_four_byte_annex_b_codes():
    payload = b"\x00\x00\x00\x01\x67x\x00\x00\x01\x68y\x00\x00\x01\x65z"

    assert h264_nal_types(payload) == frozenset({5, 7, 8})


def test_snapshot_is_empty_until_sps_pps_and_idr_are_retained():
    buffer = EncodedMediaBuffer()
    buffer.append_audio(b"opus", 1, received_at=0)
    buffer.append_video(P_FRAME, 2, received_at=0.1)

    assert buffer.snapshot().ready is False
    assert buffer.snapshot().packets == ()

    buffer.append_video(SPS + PPS + IDR, 3, received_at=0.2)
    snapshot = buffer.snapshot()

    assert snapshot.ready is True
    assert [packet.sequence for packet in snapshot.packets] == [3]
    assert snapshot.packets[0].h264_nal_types == frozenset({5, 7, 8})


def test_separate_parameter_sets_are_kept_before_idr_and_audio():
    buffer = EncodedMediaBuffer()
    buffer.append_video(SPS, 1, received_at=1)
    buffer.append_audio(b"opus-before-idr", 2, received_at=1.1)
    buffer.append_video(PPS, 3, received_at=1.2)
    buffer.append_video(IDR, 4, received_at=1.3)
    buffer.append_audio(b"opus-after-idr", 5, received_at=1.4)

    snapshot = buffer.snapshot()

    assert [packet.sequence for packet in snapshot.packets] == [1, 2, 3, 4, 5]
    assert snapshot.packets[0].payload == SPS


def test_duration_pruning_requires_a_new_decodable_boundary():
    buffer = EncodedMediaBuffer(max_duration=2)
    buffer.append_video(SPS + PPS + IDR, 1, received_at=0)
    buffer.append_audio(b"old-audio", 2, received_at=1)
    buffer.append_video(P_FRAME, 3, received_at=3)

    assert buffer.snapshot().ready is False

    buffer.append_video(SPS + PPS + IDR, 4, received_at=3.1)

    assert buffer.snapshot().ready is True
    assert buffer.snapshot().decodable_video_sequence == 4


def test_byte_limit_never_returns_packets_before_a_surviving_boundary():
    buffer = EncodedMediaBuffer(max_bytes=len(SPS + PPS + IDR) + len(P_FRAME))
    buffer.append_video(SPS + PPS + IDR, 1, received_at=0)
    buffer.append_video(P_FRAME, 2, received_at=0.1)
    buffer.append_video(P_FRAME, 3, received_at=0.2)

    assert buffer.snapshot().ready is False


def test_packet_limit_bounds_zero_duration_stream():
    buffer = EncodedMediaBuffer(max_packets=2)
    buffer.append_video(SPS + PPS + IDR, 1, received_at=0)
    buffer.append_audio(b"a", 2, received_at=0)
    buffer.append_audio(b"b", 3, received_at=0)

    assert buffer.snapshot().ready is False


def test_wait_until_ready_is_released_by_first_keyframe():
    buffer = EncodedMediaBuffer()
    result = []

    waiter = threading.Thread(target=lambda: result.append(buffer.wait_until_ready(1)))
    waiter.start()
    time.sleep(0.01)
    buffer.append_video(SPS + PPS + IDR, 1)
    waiter.join(timeout=1)

    assert result == [True]


def test_reader_returns_decodable_window_then_only_live_packets():
    buffer = EncodedMediaBuffer()
    buffer.append_video(SPS + PPS + IDR, 1, received_at=0)
    buffer.append_audio(b"audio", 2, received_at=0.1)

    initial = buffer.read(after_sequence=None, timeout=0)
    followup = buffer.read(after_sequence=initial.packets[-1].sequence, timeout=0)
    buffer.append_video(P_FRAME, 3, received_at=0.2)
    live = buffer.read(after_sequence=initial.packets[-1].sequence, timeout=0)

    assert [packet.sequence for packet in initial.packets] == [1, 2]
    assert initial.resynchronized is False
    assert followup.packets == ()
    assert [packet.sequence for packet in live.packets] == [3]
    assert live.resynchronized is False


def test_reader_resynchronizes_at_next_retained_keyframe_after_gap():
    buffer = EncodedMediaBuffer(max_packets=2)
    buffer.append_video(SPS + PPS + IDR, 1, received_at=0)
    last_sequence = buffer.read(after_sequence=None, timeout=0).packets[-1].sequence
    buffer.append_video(P_FRAME, 2, received_at=0.1)
    buffer.append_video(SPS + PPS + IDR, 3, received_at=0.2)

    resumed = buffer.read(after_sequence=last_sequence, timeout=0)

    assert [packet.sequence for packet in resumed.packets] == [3]
    assert resumed.resynchronized is True


def test_reader_waits_for_live_packet_without_busy_polling():
    buffer = EncodedMediaBuffer()
    buffer.append_video(SPS + PPS + IDR, 1)
    last_sequence = buffer.read(after_sequence=None, timeout=0).packets[-1].sequence
    result = []
    reader = threading.Thread(
        target=lambda: result.append(buffer.read(after_sequence=last_sequence, timeout=1))
    )
    reader.start()
    time.sleep(0.01)
    buffer.append_audio(b"audio", 2)
    reader.join(timeout=1)

    assert [packet.payload for packet in result[0].packets] == [b"audio"]


def test_snapshot_packets_are_immutable_copies():
    buffer = EncodedMediaBuffer()
    source = bytearray(SPS + PPS + IDR)

    with pytest.raises(MediaBufferError, match="bytes"):
        buffer.append_video(source, 1)

    buffer.append_video(bytes(source), 1)
    source[-1] = 0
    assert buffer.snapshot().packets[0].payload.endswith(b"\x84")


@pytest.mark.parametrize(
    ("operation", "message"),
    [
        (lambda buffer: buffer.append_audio(b"", 1), "non-empty"),
        (lambda buffer: buffer.append_audio(b"x", True), "non-negative integer"),
        (lambda buffer: buffer.append_audio(b"x", -1), "non-negative integer"),
        (
            lambda buffer: buffer.append_audio(b"x", 1, received_at=float("nan")),
            "finite",
        ),
    ],
)
def test_buffer_rejects_invalid_packets(operation, message):
    with pytest.raises(MediaBufferError, match=message):
        operation(EncodedMediaBuffer())


def test_buffer_rejects_backwards_receipt_time():
    buffer = EncodedMediaBuffer()
    buffer.append_audio(b"a", 1, received_at=2)

    with pytest.raises(MediaBufferError, match="backwards"):
        buffer.append_audio(b"b", 2, received_at=1)


@pytest.mark.parametrize("after_sequence", [True, -1, 1.5])
def test_reader_rejects_invalid_sequence(after_sequence):
    with pytest.raises(ValueError, match="sequence"):
        EncodedMediaBuffer().read(after_sequence=after_sequence, timeout=0)


def test_packet_stream_kind_is_explicit():
    buffer = EncodedMediaBuffer()
    buffer.append_video(SPS + PPS + IDR, 1, received_at=0)
    buffer.append_audio(b"opus", 2, received_at=0.1)

    assert [packet.stream for packet in buffer.snapshot().packets] == [
        MediaStreamKind.VIDEO,
        MediaStreamKind.AUDIO,
    ]
