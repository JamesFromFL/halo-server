import threading
import time

import pytest

from halo_server.media import h264
from halo_server.media.buffer import (
    EncodedMediaBuffer,
    MediaBufferError,
    MediaStreamKind,
    h264_nal_types,
)

SPS = bytes.fromhex("000000016742c00bda0507e840000003004000000300a3c50aa8")
PPS = bytes.fromhex("0000000168ce0fc8")
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


def test_separate_parameter_sets_prefix_the_idr_without_older_media():
    buffer = EncodedMediaBuffer()
    buffer.append_video(SPS, 1, received_at=1)
    buffer.append_audio(b"opus-before-idr", 2, received_at=1.1)
    buffer.append_video(PPS, 3, received_at=1.2)
    buffer.append_video(IDR, 4, received_at=1.3)
    buffer.append_audio(b"opus-after-idr", 5, received_at=1.4)

    snapshot = buffer.snapshot()

    assert [packet.sequence for packet in snapshot.packets] == [4, 5]
    assert snapshot.packets[0].payload == SPS + PPS + IDR
    assert snapshot.packets[0].received_at == 1.3


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
    buffer = EncodedMediaBuffer(max_bytes=2 * len(SPS + PPS) + len(IDR) + len(P_FRAME))
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
    buffer.append_video(P_FRAME, 3, received_at=0.2)
    buffer.append_video(IDR, 4, received_at=0.3)

    resumed = buffer.read(after_sequence=last_sequence, timeout=0)

    assert [packet.sequence for packet in resumed.packets] == [4]
    assert resumed.packets[0].payload == SPS + PPS + IDR
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


def test_contiguous_reader_does_not_need_a_retained_idr():
    buffer = EncodedMediaBuffer(max_duration=1)
    first = buffer.append_video(SPS + PPS + IDR, 0, received_at=0)
    buffer.append_video(P_FRAME, 90000, received_at=1)
    buffer.append_video(P_FRAME, 180000, received_at=2)

    assert not buffer.ready
    assert not buffer.read(after_sequence=None, timeout=0).packets
    live = buffer.read(after_sequence=first.sequence, timeout=0)
    assert [packet.sequence for packet in live.packets] == [2, 3]
    assert not live.resynchronized


@pytest.mark.parametrize("payload", [SPS[:8], b"\0\0\1\x68\0", b"\0\0\1\x65"])
def test_malformed_update_retires_old_configuration(payload):
    buffer = EncodedMediaBuffer()
    first = buffer.append_video(SPS + PPS + IDR, 0, received_at=0)
    with pytest.raises(MediaBufferError):
        buffer.append_video(payload, 1, received_at=1)
    buffer.append_video(IDR, 2, received_at=2)
    assert not buffer.ready
    assert not buffer.read(after_sequence=first.sequence, timeout=0).packets
    assert buffer._sps is buffer._pps is None


def test_unknown_parameter_ids_do_not_reuse_old_decoder_state():
    buffer = EncodedMediaBuffer()
    first = buffer.append_video(SPS + PPS + IDR, 0, received_at=0)
    # PPS 0 names SPS 1, which has never been supplied.
    unknown_sps_pps = b"\0\0\0\1\x68\xa8"
    buffer.append_video(unknown_sps_pps + IDR, 1, received_at=1)
    assert not buffer.ready
    assert not buffer.read(after_sequence=first.sequence, timeout=0).packets
    recovered = buffer.append_video(PPS + IDR, 2, received_at=2)
    batch = buffer.read(after_sequence=first.sequence, timeout=0)
    assert batch.resynchronized
    assert batch.packets[0].sequence == recovered.sequence
    assert batch.packets[0].payload == SPS + PPS + IDR
    # IDR slice names PPS 1 while the cache contains PPS 0.
    buffer.append_video(b"\0\0\0\1\x65\xb5", 3, received_at=3)
    assert not buffer.ready
    assert not buffer.read(after_sequence=recovered.sequence, timeout=0).packets


def test_repeated_parameters_do_not_discard_the_current_window():
    buffer = EncodedMediaBuffer()
    first = buffer.append_video(SPS + PPS + IDR, 0, received_at=0)
    buffer.append_video(P_FRAME, 1, received_at=1)
    buffer.append_video(SPS + PPS + IDR, 2, received_at=2)
    assert buffer.snapshot().decodable_video_sequence == first.sequence
    assert [packet.sequence for packet in buffer.snapshot().packets] == [1, 2, 3]


def test_parameter_cache_is_counted_in_byte_budget_and_survives_history_eviction():
    limit = len(SPS + PPS) + len(IDR) + len(P_FRAME)
    buffer = EncodedMediaBuffer(max_bytes=limit)
    buffer.append_video(SPS + PPS, 0, received_at=0)
    for index in range(1, 100):
        buffer.append_video(IDR, index, received_at=index)
        snapshot = buffer.snapshot()
        assert snapshot.ready
        assert snapshot.payload_bytes <= limit
        assert buffer._payload_bytes + buffer._parameter_bytes <= limit
        assert len(buffer._packets) <= 2
    assert buffer._parameter_bytes == len(SPS + PPS)
    assert buffer._packets[0].sequence > 1


def test_clear_retires_parameter_cache_and_never_reuses_it_across_epochs():
    buffer = EncodedMediaBuffer()
    first = buffer.append_video(SPS + PPS + IDR, 0)
    buffer.clear()
    assert buffer._sps is buffer._pps is None
    assert buffer._parameter_bytes == 0
    buffer.append_video(IDR, 1)
    assert not buffer.ready
    assert not buffer.read(after_sequence=first.sequence, timeout=0).packets
    recovered = buffer.append_video(SPS + PPS + IDR, 2)
    batch = buffer.read(after_sequence=first.sequence, timeout=0)
    assert batch.resynchronized and batch.capture_epoch == first.capture_epoch + 1
    assert batch.packets[0].sequence == recovered.sequence


def test_parameter_sizes_are_bounded_even_for_separately_received_headers():
    buffer = EncodedMediaBuffer(max_bytes=len(SPS) + len(PPS) - 1)
    buffer.append_video(SPS, 0)
    with pytest.raises(MediaBufferError, match="parameters exceed"):
        buffer.append_video(PPS, 1)
    assert buffer._parameter_bytes == 0
    oversized = PPS + b"x" * (64 * 1024)
    with pytest.raises(MediaBufferError, match="picture parameters exceed"):
        EncodedMediaBuffer().append_video(oversized, 0)


@pytest.mark.parametrize("bound", ["access_unit", "buffer"])
def test_synthesized_idr_obeys_access_unit_and_packet_byte_bounds(monkeypatch, bound):
    if bound == "access_unit":
        monkeypatch.setattr(h264, "MAX_ACCESS_UNIT_BYTES", 128)
    buffer = EncodedMediaBuffer(max_bytes=128 if bound == "buffer" else 1024)
    buffer.append_video(SPS + PPS, 0)
    large_idr = IDR + b"x" * (128 - len(IDR))
    with pytest.raises(MediaBufferError, match="Prefixed.*byte limit"):
        buffer.append_video(large_idr, 1)
    assert not buffer.ready
    assert not buffer.snapshot().packets
    assert buffer._parameter_bytes == 0


def test_missing_sps_timing_does_not_prevent_decoder_configuration_retention():
    sps = bytes.fromhex("000000016742001fda0507e4")
    assert h264.parse_sps(sps[4:]).frame_rate is None
    buffer = EncodedMediaBuffer(max_duration=1)
    buffer.append_video(sps + PPS + IDR, 0, received_at=0)
    packet = buffer.append_video(IDR, 180000, received_at=2)
    initial = buffer.read(after_sequence=None, timeout=0)
    assert initial.packets[0].sequence == packet.sequence
    assert initial.packets[0].payload == sps + PPS + IDR


def _golomb_fields(*values):
    fields = []
    for value in values:
        bits = f"{value + 1:b}"
        fields.append("0" * (len(bits) - 1) + bits)
    bits = "".join(fields) + "1000"
    bits += "0" * (-len(bits) % 8)
    return int(bits, 2).to_bytes(len(bits) // 8, "big")


@pytest.mark.parametrize(
    "payload",
    [
        b"\0\0\1\x68" + _golomb_fields(256, 0),
        b"\0\0\1\x68" + _golomb_fields(0, 32),
        b"\0\0\1\x65" + _golomb_fields(0, 2, 256),
    ],
)
def test_out_of_range_parameter_ids_are_rejected_and_retired(payload):
    buffer = EncodedMediaBuffer()
    buffer.append_video(SPS + PPS + IDR, 0)
    with pytest.raises(MediaBufferError, match="exceeds limit"):
        buffer.append_video(payload, 1)
    assert buffer._parameter_bytes == 0
    assert not buffer.ready
