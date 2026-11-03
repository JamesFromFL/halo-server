"""Real H.264 depayload/receiver controls plus bounded adversarial assembly."""

import base64
import importlib.util
import weakref
from pathlib import Path
from types import SimpleNamespace

import av
import pytest
from aiortc.codecs.h264 import H264PayloadDescriptor
from aiortc.rtcrtpparameters import RTCRtpCodecParameters
from aiortc.rtcrtpreceiver import RTCRtpReceiver
from aiortc.rtp import RtpPacket

from halo_server.media.ring_media import install_encoded_frame_adapter
from halo_server.media.video_jitter import H264JitterBuffer, VideoJitterBuffer


def packet(sequence, timestamp, payload=b"\x65\x80\x11", *, marker=True, ssrc=7, pt=96):
    value = RtpPacket(payload_type=pt, sequence_number=sequence % 65536, timestamp=timestamp)
    value.ssrc, value.marker, value.payload = ssrc, marker, payload
    value._data = H264PayloadDescriptor.parse(payload)[1]
    return value


def test_marker_releases_complete_au_without_next_timestamp():
    jitter = H264JitterBuffer()
    pli, frame = jitter.add(packet(10, 100))
    assert not pli and frame.timestamp == 100
    assert frame.data == b"\0\0\0\1\x65\x80\x11"
    assert jitter.buffered_packets == jitter.buffered_bytes == 0
    assert not jitter.waiting_for_idr


def test_missing_markers_use_contiguous_timestamp_fallback():
    jitter = H264JitterBuffer()
    assert jitter.add(packet(1, 100, marker=False))[1] is None
    first = jitter.add(packet(2, 200, b"\x61\x80\x22", marker=False))[1]
    second = jitter.add(packet(3, 300, b"\x61\x80\x33", marker=False))[1]
    assert [first.timestamp, second.timestamp] == [100, 200]


def test_fu_reorder_duplicate_sequence_wrap_and_timestamp_wrap():
    jitter = H264JitterBuffer()
    start = packet(65534, 0xFFFFFF00, b"\x7c\x85\x80a", marker=False)
    middle = packet(65535, 0xFFFFFF00, b"\x7c\x05b", marker=False)
    end = packet(0, 0xFFFFFF00, b"\x7c\x45c")
    assert jitter.add(start)[1] is None
    assert jitter.add(end)[1] is None
    assert jitter.add(end)[1] is None
    complete = jitter.add(middle)[1]
    assert complete.data == b"\0\0\0\1\x65\x80abc"
    assert jitter.add(middle)[1] is None
    assert jitter.add(packet(1, 10, b"\x61\x80x"))[1].timestamp == 10


@pytest.mark.parametrize("limit", ["time", "horizon"])
def test_missing_fragment_recovers_with_pli_and_gates_dependents(limit):
    now = [0.0]
    jitter = H264JitterBuffer(clock=lambda: now[0])
    assert jitter.add(packet(1, 100, b"\x7c\x85\x80a", marker=False))[1] is None
    assert jitter.add(packet(3, 100, b"\x7c\x45c"))[1] is None
    if limit == "time":
        now[0] = 0.101
    assert jitter.add(packet(4, 200, b"\x61\x80p"))[1] is None
    assert jitter.add(packet(5, 300, b"\x61\x80q"))[1] is None
    assert jitter.snapshot()["recovery_requests"] == 1
    assert jitter.waiting_for_idr
    assert jitter.add(packet(6, 400))[1].timestamp == 400
    assert jitter.add(packet(7, 500, b"\x61\x80p"))[1].timestamp == 500
    assert not jitter.waiting_for_idr


def test_lost_pli_retries_on_arrivals_with_one_second_cooldown():
    now = [0.0]
    jitter = H264JitterBuffer(clock=lambda: now[0])
    flags = []
    for seq, when in enumerate((0, 0.2, 0.9, 1.01, 1.2, 2.02)):
        now[0] = when
        flags.append(jitter.add(packet(seq, seq * 100, b"\x61\x80p"))[0])
    assert flags == [True, False, False, True, False, True]
    assert jitter.snapshot()["loss_events"] == 1


@pytest.mark.parametrize("bound", ["packets", "bytes"])
def test_bounds_never_emit_a_truncated_idr(bound):
    jitter = H264JitterBuffer(
        capacity=4 if bound == "packets" else 512, max_bytes=45 if bound == "bytes" else 1024
    )
    outputs = []
    for seq in range(9):
        header = 0x85 if seq == 0 else 0x45 if seq == 8 else 0x05
        _, result = jitter.add(packet(seq, 100, bytes([0x7C, header, 0x80, 0x11]), marker=seq == 8))
        outputs.extend([result] if result else [])
        assert jitter.buffered_packets <= jitter.capacity
        assert jitter.buffered_bytes <= jitter.max_bytes
    assert not outputs
    assert jitter.waiting_for_idr
    assert jitter.add(packet(9, 200))[1] is not None


@pytest.mark.parametrize("payload", [b"\x7c\xc5\x80", b"\x7c\x45\x80", b"\x65\x40x"])
def test_malformed_or_joined_mid_idr_never_becomes_a_complete_au(payload):
    now = [0.0]
    jitter = H264JitterBuffer(clock=lambda: now[0])
    assert jitter.add(packet(1, 100, payload))[1] is None
    now[0] = 0.11
    pli, frame = jitter.add(packet(2, 200, b"\x61\x80p"))
    assert frame is None
    assert jitter.waiting_for_idr
    assert jitter.snapshot()["recovery_requests"] >= 1


def test_conflicting_duplicate_fences_that_au_and_earlier_dependent_data():
    jitter = H264JitterBuffer()
    original = packet(1, 100, b"\x7c\x85\x80a", marker=False)
    assert jitter.add(original)[1] is None
    assert jitter.add(packet(1, 100, b"\x7c\x85\x80b", marker=False))[0]
    assert jitter.add(packet(2, 100, b"\x7c\x45c"))[1] is None
    assert jitter.add(packet(3, 200))[1] is not None


def test_source_change_forward_gap_and_backward_restart_need_idr():
    jitter = H264JitterBuffer()
    assert jitter.add(packet(1000, 100))[1]
    assert jitter.add(packet(1001, 200, b"\x61\x80p", ssrc=8))[1] is None
    assert jitter.add(packet(1002, 300, ssrc=8))[1]
    assert jitter.add(packet(3000, 400, b"\x61\x80p", ssrc=8))[1] is None
    assert jitter.add(packet(3001, 500, ssrc=8))[1]
    pli, frame = jitter.add(packet(10, 600, ssrc=8))
    assert pli and frame
    assert jitter.add(packet(3000, 400, ssrc=8))[1] is None


def test_stap_a_accepts_config_and_single_byte_end_nal():
    jitter = H264JitterBuffer()
    # STAP contains an IDR slice and valid one-byte end-of-sequence NAL.
    payload = b"\x78\x00\x03\x65\x80\x11\x00\x01\x0a"
    assert jitter.add(packet(1, 100, payload))[1].data.endswith(b"\0\0\0\1\x0a")


def test_single_oversized_packet_is_rejected_without_retained_payload():
    jitter = H264JitterBuffer()
    pli, frame = jitter.add(packet(1, 100, b"\x65\x80" + b"x" * (1024 * 1024)))
    assert pli and frame is None
    assert jitter.buffered_bytes == jitter.buffered_packets == 0
    assert jitter.add(packet(2, 100))[1] is None
    assert jitter.add(packet(3, 200))[1]


def test_late_repair_cannot_reintroduce_expired_fragmented_idr():
    now = [0.0]
    jitter = H264JitterBuffer(clock=lambda: now[0])
    assert jitter.add(packet(1, 100, b"\x7c\x85\x80a", marker=False))[1] is None
    assert jitter.add(packet(3, 100, b"\x7c\x45c"))[1] is None
    now[0] = 0.2
    assert jitter.add(packet(4, 200, b"\x61\x80p"))[1] is None
    assert jitter.add(packet(2, 100, b"\x7c\x05b", marker=False))[1] is None
    assert jitter.add(packet(5, 300))[1]


@pytest.mark.parametrize("repair_at,accepted", [(0.05, True), (0.101, False), (10.0, False)])
def test_first_arrival_after_hole_deadline_cannot_repair_expired_idr(repair_at, accepted):
    now = [0.0]
    jitter = H264JitterBuffer(clock=lambda: now[0])
    assert jitter.add(packet(1, 100, b"\x7c\x85\x80a", marker=False))[1] is None
    assert jitter.add(packet(3, 100, b"\x7c\x45c"))[1] is None
    now[0] = repair_at
    pli, frame = jitter.add(packet(2, 100, b"\x7c\x05b", marker=False))
    assert bool(frame) is accepted
    assert pli is not accepted
    assert jitter.snapshot()["reorder_timeouts"] == int(not accepted)
    if accepted:
        assert frame.data == b"\0\0\0\1\x65\x80abc"
    else:
        assert jitter.buffered_packets == 0
        assert jitter.add(packet(4, 200, b"\x61\x80p"))[1] is None
        assert jitter.add(packet(5, 300))[1]


def test_hole_grace_begins_when_gap_is_observed_not_at_first_fragment():
    now = [0.0]
    jitter = H264JitterBuffer(clock=lambda: now[0])
    assert jitter.add(packet(1, 100, b"\x7c\x85\x80a", marker=False))[1] is None
    now[0] = 0.45
    assert jitter.add(packet(3, 100, b"\x7c\x45c"))[1] is None
    now[0] = 0.5
    assert jitter.add(packet(2, 100, b"\x7c\x05b", marker=False))[1]
    assert jitter.snapshot()["loss_events"] == 0


def test_whole_au_age_is_separate_bounded_limit_for_contiguous_fragments():
    now = [0.0]
    jitter = H264JitterBuffer(clock=lambda: now[0])
    assert jitter.add(packet(1, 100, b"\x7c\x85\x80a", marker=False))[1] is None
    now[0] = 0.8
    assert jitter.add(packet(2, 100, b"\x7c\x05b", marker=False))[1] is None
    now[0] = 1.01
    pli, frame = jitter.add(packet(3, 100, b"\x7c\x45c"))
    assert pli and frame is None
    assert jitter.snapshot()["assembly_timeouts"] == 1
    assert jitter.snapshot()["reorder_timeouts"] == 0
    assert jitter.buffered_bytes == jitter.buffered_packets == 0


def test_repaired_head_does_not_leave_gap_timeout_on_complete_queued_au():
    now = [0.0]
    jitter = H264JitterBuffer(clock=lambda: now[0])
    jitter.add(packet(1, 100, b"\x7c\x85\x80a", marker=False))
    jitter.add(packet(3, 100, b"\x7c\x45c"))
    jitter.add(packet(4, 200, b"\x61\x80p"))
    now[0] = 0.05
    assert jitter.add(packet(2, 100, b"\x7c\x05b", marker=False))[1].timestamp == 100
    now[0] = 0.15
    assert jitter.add(packet(5, 300, b"\x61\x80q"))[1].timestamp == 200
    assert jitter.snapshot()["loss_events"] == 0


@pytest.fixture(scope="module")
def synthetic():
    path = Path(__file__).resolve().parents[1] / "tools/receiver_comparison/run.py"
    spec = importlib.util.spec_from_file_location("comparison_for_jitter", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def receive_trace(trace, *, rtx_repair=False):
    receiver = RTCRtpReceiver("video", SimpleNamespace(state="new"))
    assert isinstance(receiver._RTCRtpReceiver__jitter_buffer, VideoJitterBuffer)
    clock = [0.0]
    receiver._RTCRtpReceiver__jitter_buffer._h264.clock = lambda: clock[0]
    outputs, feedback = [], []
    install_encoded_frame_adapter(
        receiver, "video", lambda data, ts: outputs.append((data, ts)), forward_to_decoder=False
    )
    receiver._RTCRtpReceiver__decoder_thread = True
    receiver._set_rtcp_ssrc(999)
    receiver._RTCRtpReceiver__codecs[96] = RTCRtpCodecParameters(
        mimeType="video/H264", clockRate=90000, payloadType=96
    )
    receiver._RTCRtpReceiver__codecs[97] = RTCRtpCodecParameters(
        mimeType="video/rtx", clockRate=90000, payloadType=97, parameters={"apt": 96}
    )
    receiver._RTCRtpReceiver__rtx_ssrc[0x12345679] = 0x12345678

    async def send_rtcp(value):
        feedback.append(value)

    receiver._send_rtcp = send_rtcp
    withheld = None
    for index, item in enumerate(trace["packets"]):
        clock[0] = item["due_ms"] / 1000
        value = RtpPacket.parse(base64.b64decode(item["wire"]))
        if rtx_repair and index == 4:
            withheld = value
            continue
        await receiver._handle_rtp_packet(value, int(clock[0] * 1000))
        if withheld and index == 6:
            original_sequence = withheld.sequence_number
            withheld.payload_type = 97
            withheld.ssrc = 0x12345679
            withheld.sequence_number = 77
            withheld.payload = original_sequence.to_bytes(2, "big") + withheld.payload
            await receiver._handle_rtp_packet(withheld, int(clock[0] * 1000))
            withheld = None
    receiver._RTCRtpReceiver__decoder_thread = None
    return outputs, feedback, receiver._RTCRtpReceiver__jitter_buffer.snapshot()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["regular", "reorder", "large_idr", "wrap", "delayed_boundary"])
async def test_real_receiver_outputs_every_valid_au_and_independent_decoder_accepts(
    synthetic, case
):
    frames = synthetic.source_frames(case == "large_idr")
    trace = synthetic.make_trace(case, frames)
    outputs, _, stats = await receive_trace(trace)
    assert len(outputs) == len(frames) == stats["completed_aus"] == 12
    expected = [au["annexb_sha256"] for au in trace["aus"]]
    assert [synthetic.digest(data) for data, _ in outputs] == expected
    decoder = av.CodecContext.create("h264", "r")
    assert sum(len(decoder.decode(av.Packet(data))) for data, _ in outputs) == 12


@pytest.mark.asyncio
async def test_real_receiver_loss_recovers_only_at_full_idr_and_decodes_following_pictures(
    synthetic,
):
    trace = synthetic.make_trace("loss", synthetic.source_frames())
    outputs, feedback, stats = await receive_trace(trace)
    assert [synthetic.digest(data) for data, _ in outputs] == [
        au["annexb_sha256"] for au in trace["aus"][6:]
    ]
    assert any(
        getattr(value, "fmt", None) == 1 and type(value).__name__ == "RtcpPsfbPacket"
        for value in feedback
    )
    assert stats["loss_events"] == 1 and not stats["waiting_for_idr"]
    decoder = av.CodecContext.create("h264", "r")
    assert sum(len(decoder.decode(av.Packet(data))) for data, _ in outputs) == 6


@pytest.mark.asyncio
async def test_real_rtx_unwrap_repairs_missing_fu_before_deadline(synthetic):
    trace = synthetic.make_trace("regular", synthetic.source_frames())
    outputs, _, stats = await receive_trace(trace, rtx_repair=True)
    assert len(outputs) == 12
    assert [synthetic.digest(data) for data, _ in outputs] == [
        au["annexb_sha256"] for au in trace["aus"]
    ]
    assert stats["loss_events"] == 0


def test_other_codec_reaches_original_generic_video_jitter_boundary():
    receiver = SimpleNamespace(_RTCRtpReceiver__codecs={96: SimpleNamespace(mimeType="video/VP8")})
    jitter = VideoJitterBuffer(receiver)
    first = packet(1, 100)
    second = packet(2, 200)
    assert jitter.add(first)[1] is None
    assert jitter.add(second)[1].timestamp == 100
    assert jitter.snapshot() is None


@pytest.mark.asyncio
async def test_real_vp8_handler_still_reaches_incompatible_codec_callback():
    receiver = RTCRtpReceiver("video", SimpleNamespace(state="new"))
    incompatible = []
    install_encoded_frame_adapter(
        receiver,
        "video",
        lambda *_: pytest.fail("H264 callback"),
        forward_to_decoder=False,
        on_incompatible=lambda *args: incompatible.append(args),
    )
    receiver._RTCRtpReceiver__decoder_thread = True
    receiver._RTCRtpReceiver__codecs[96] = RTCRtpCodecParameters(
        mimeType="video/VP8", clockRate=90000, payloadType=96
    )
    for seq in (1, 2):
        value = packet(seq, seq * 3000)
        value.payload = b"\x10vp8fixture"
        await receiver._handle_rtp_packet(value, seq * 33)
    receiver._RTCRtpReceiver__decoder_thread = None
    assert incompatible == [("video", "video/vp8")]


@pytest.mark.asyncio
async def test_real_large_idrs_paced_over_half_second_remain_complete_and_decodable(synthetic):
    trace = synthetic.make_trace("large_idr", synthetic.source_frames(True))
    start = 0.0
    for au in trace["aus"]:
        packets = [item for item in trace["packets"] if item["au"] == au["index"]]
        duration = 500.0 if len(packets) > 128 else (len(packets) - 1) * 0.015
        for index, item in enumerate(packets):
            item["due_ms"] = start + duration * index / max(1, len(packets) - 1)
        start += duration + 33.333
    outputs, _, stats = await receive_trace(trace)
    assert [synthetic.digest(data) for data, _ in outputs] == [
        au["annexb_sha256"] for au in trace["aus"]
    ]
    assert stats["loss_events"] == stats["reorder_timeouts"] == stats["assembly_timeouts"] == 0
    decoder = av.CodecContext.create("h264", "r")
    assert sum(len(decoder.decode(av.Packet(data))) for data, _ in outputs) == 12


def test_codec_dispatch_observes_in_place_negotiation_without_retaining_receiver():
    class Receiver:
        pass

    receiver = Receiver()
    codecs = receiver._RTCRtpReceiver__codecs = {}
    jitter = VideoJitterBuffer(receiver)
    owner = weakref.ref(receiver)
    del receiver
    assert owner() is None
    codecs[96] = SimpleNamespace(mimeType="video/H264")
    assert jitter.add(packet(1, 100))[1].timestamp == 100
