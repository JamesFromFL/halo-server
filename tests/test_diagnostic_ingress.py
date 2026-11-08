"""The optional capture observes real missing audio packets without fixing them."""

import asyncio
import json
import threading
from datetime import UTC, datetime
from types import SimpleNamespace as NS

import pytest
from aiortc.jitterbuffer import JitterFrame
from aiortc.rtcrtpparameters import RTCRtpCodecParameters
from aiortc.rtcrtpreceiver import RTCRtpReceiver
from aiortc.rtp import RtcpSenderInfo, RtcpSrPacket, RtpPacket
from test_media_session import FakeClient, _receiver_kwargs, fake_supervisor

from halo_server.media.diagnostic_capture import DiagnosticCapture
from halo_server.media.diagnostic_ingress import (
    ScopedDiagnosticTrace,
    install_audio_jitter_trace,
    trace_frame,
    trace_rtp,
)
from halo_server.media.leases import LeasePurpose
from halo_server.media.receiver_metrics import ReceiverPipeline, install_rtp_observer
from halo_server.media.ring_media import (
    install_encoded_frame_adapter,
    install_source_clock_observer,
)
from halo_server.media.session import CameraMediaSession
from halo_server.media.supervisor import supervise_receiver


class Trace:
    closed = False

    def __init__(self):
        self.events = []
        self.media_records = []
        self.triggers = []

    def event(self, stage, track, **fields):
        self.events.append((stage, track, fields))
        return True

    def media(self, stage, track, payload, **fields):
        self.media_records.append((stage, track, payload, fields))
        return True

    def trigger(self, reason):
        self.triggers.append(reason)
        return True


def rtp(sequence):
    packet = RtpPacket(
        payload_type=111, sequence_number=sequence, timestamp=sequence * 2880, marker=1
    )
    packet.ssrc = 777
    packet.payload = b"\xf8\xff\xfe"
    return packet


def receiver(trace, now):
    transport = NS(state="new", _stats_id="private")
    audio = RTCRtpReceiver("audio", transport)
    video = RTCRtpReceiver("video", transport)
    audio._RTCRtpReceiver__codecs[111] = RTCRtpCodecParameters(
        mimeType="audio/opus", clockRate=48000, channels=1, payloadType=111
    )
    pipeline = ReceiverPipeline(audio, video, clock=lambda: now[0], diagnostic_trace=trace)
    delivered = []

    def frame(payload, timestamp):
        delivered.append((payload, timestamp))
        pipeline.frame("audio")

    install_encoded_frame_adapter(
        audio, "audio", frame, forward_to_decoder=False, diagnostic_trace=pipeline._diagnostic_trace
    )
    audio._RTCRtpReceiver__decoder_thread = True
    install_rtp_observer(audio, "audio", pipeline)
    return audio, pipeline, delivered


@pytest.mark.asyncio
async def test_actual_audio_hold_captures_exact_missing_slot_and_preserves_results():
    now = [10.0]
    trace = Trace()
    traced, pipeline, observed = receiver(trace, now)
    plain, _, expected = receiver(None, now)
    sequences = (0, 1, 2, 4, 5, 6, 7, 8, 9, 10)
    for sequence in sequences:
        now[0] = 10 + sequence * 0.06
        assert await traced._handle_rtp_packet(rtp(sequence), round(now[0] * 1000)) is None
        await plain._handle_rtp_packet(rtp(sequence), round(now[0] * 1000))
    assert observed == expected == []
    assert trace.triggers == ["audio_assembly_hold"]
    before = [fields for stage, _, fields in trace.events if stage == "audio_jitter_before"]
    after = [fields for stage, _, fields in trace.events if stage == "audio_jitter_after"]
    assert len(before) == len(after) == len(sequences)
    assert after[-1] == dict(
        seq=10,
        frame_returned=False,
        pli=False,
        hold_ms=600,
        waiting_packets=10,
        origin=0,
        capacity=16,
        prefetch=4,
        occupancy_mask=(1 << 11) - 1 - (1 << 3),
        contiguous_packets=3,
        contiguous_frames=2,
        first_gap_offset=3,
        incoming_offset=10,
    )
    assert "add" not in plain._RTCRtpReceiver__jitter_buffer.__dict__
    now[0] = 10.7
    await traced._handle_rtp_packet(rtp(3), 10700)
    await plain._handle_rtp_packet(rtp(3), 10700)
    assert observed == expected == [(b"\xf8\xff\xfe", 0)]
    assert [row[2] for row in trace.media_records if row[0] == "source_frame"] == [observed[0][0]]
    fields = [row[3] for row in trace.media_records if row[0] == "source_rtp"]
    assert fields[-1] == dict(
        seq=3, rtp_timestamp=8640, payload_type=111, marker=True, payload_bytes=3, codec="opus"
    )
    serialized = json.dumps([trace.events, fields])
    assert all(secret not in serialized for secret in ("ssrc", "777", "camera", "sdp", "address"))
    count = len(trace.events)
    pipeline.fence()
    await traced._handle_rtp_packet(rtp(11), 10760)
    assert len(trace.events) == count


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("private"), asyncio.CancelledError("private")])
async def test_observer_exceptions_never_change_actual_frame_results(failure):
    class Broken:
        def __getattr__(self, name):
            raise failure

    now = [10.0]
    traced, _, observed = receiver(Broken(), now)
    plain, _, expected = receiver(None, now)
    for sequence in range(20):
        now[0] += 0.06
        await traced._handle_rtp_packet(rtp(sequence), round(now[0] * 1000))
        await plain._handle_rtp_packet(rtp(sequence), round(now[0] * 1000))
    assert observed == expected and len(observed) == 16


@pytest.mark.asyncio
async def test_established_audio_hold_reports_prefetch_gap_without_repairing_it():
    trace, now = Trace(), [20.0]
    traced, _, observed = receiver(trace, now)
    plain, _, expected = receiver(None, now)
    for sequence in (*range(11), *range(12, 23)):
        now[0] = 20 + sequence * 0.06
        await traced._handle_rtp_packet(rtp(sequence), round(now[0] * 1000))
        await plain._handle_rtp_packet(rtp(sequence), round(now[0] * 1000))
    assert observed == expected and len(observed) == 7
    last = [fields for stage, _, fields in trace.events if stage == "audio_jitter_after"][-1]
    assert last["origin"] == 7
    assert last["first_gap_offset"] == 4
    assert last["contiguous_frames"] == 3
    assert last["hold_ms"] == 720
    assert trace.triggers == ["audio_assembly_hold"]
    now[0] = 21.4
    await traced._handle_rtp_packet(rtp(11), 21400)
    await plain._handle_rtp_packet(rtp(11), 21400)
    assert observed == expected and len(observed) == 8


@pytest.mark.asyncio
async def test_hold_trigger_retries_when_first_recorder_offer_loses_contention():
    class Contended(Trace):
        def trigger(self, reason):
            self.triggers.append(reason)
            return len(self.triggers) > 1

    trace, now = Contended(), [10.0]
    audio, _, delivered = receiver(trace, now)
    for sequence in (0, 1, 2, 4, 5, 6, 7, 8, 9, 10):
        now[0] = 10 + sequence * 0.06
        await audio._handle_rtp_packet(rtp(sequence), round(now[0] * 1000))
    assert not delivered
    assert trace.triggers == ["audio_assembly_hold", "audio_assembly_hold"]


def test_jitter_wrapper_preserves_original_exception_identity():
    audio = RTCRtpReceiver("audio", NS(state="new"))
    assert install_audio_jitter_trace(audio, Trace())
    failure = ValueError("original receiver error")

    class InvalidPacket:
        @property
        def sequence_number(self):
            raise failure

    with pytest.raises(ValueError) as caught:
        audio._RTCRtpReceiver__jitter_buffer.add(InvalidPacket())
    assert caught.value is failure


def test_frame_capture_preserves_h264_bytes_and_mapped_timestamp():
    trace = Trace()
    payload = bytes.fromhex("00000001658884")
    trace_frame(trace, "video", JitterFrame(payload, 81000))
    assert trace.media_records == [
        (
            "source_frame",
            "video",
            payload,
            dict(timestamp=81000, payload_bytes=len(payload), codec="h264"),
        )
    ]
    trace_rtp(trace, "video", NS(payload=b"\x65", sequence_number=1))
    assert len(trace.media_records) == 1  # Partial diagnostic objects are non-authoritative.


def test_audio_jitter_trace_is_bounded_contract_checked_and_never_reinstalled():
    audio = RTCRtpReceiver("audio", NS(state="new"))
    buffer = audio._RTCRtpReceiver__jitter_buffer
    original = buffer.add
    assert install_audio_jitter_trace(audio, None) is False
    assert buffer.add == original
    assert install_audio_jitter_trace(audio, Trace()) is True
    wrapped = buffer.add
    assert install_audio_jitter_trace(audio, Trace()) is False
    assert buffer.add is wrapped
    other = RTCRtpReceiver("video", NS(state="new"))
    assert install_audio_jitter_trace(other, Trace()) is False


def test_scope_fences_retained_events_media_and_trigger():
    trace = Trace()
    valid = [True]
    scoped = ScopedDiagnosticTrace(trace, lambda: valid[0], source_generation=4)
    nested = ScopedDiagnosticTrace(scoped, lambda: True, attempt=2)
    assert nested.event("source_frame", "audio", timestamp=1)
    assert trace.events[-1][2] == dict(timestamp=1, source_generation=4, attempt=2)
    valid[0] = False
    assert nested.closed
    assert not nested.event("source_frame", "audio", timestamp=2)
    assert not nested.media("source_frame", "audio", b"x")
    assert not nested.trigger("audio_assembly_hold")
    valid[0] = True
    scoped.fence()
    assert not nested.event("source_frame", "audio", timestamp=3)
    assert len(trace.events) == 1 and trace.media_records == trace.triggers == []


def test_session_trace_binding_requires_idle_and_retired_generation():
    first, second = Trace(), Trace()
    client = FakeClient()
    session = CameraMediaSession(
        client,
        NS(id=42, name="private"),
        receiver_supervisor=fake_supervisor,
        legacy_audio_enabled=False,
    )
    assert session.set_diagnostic_trace(first)
    assert session.diagnostic_trace is first
    session.acquire(LeasePurpose.HOMEKIT_LIVE)
    scoped = _receiver_kwargs(client.futures[0])["diagnostic_trace"]
    scoped.event("source_frame", "audio", timestamp=1)
    assert first.events[-1][2]["source_generation"] == 1
    assert not session.set_diagnostic_trace(second)
    session.stop_all()
    assert session.set_diagnostic_trace(second)
    scoped.event("source_frame", "audio", timestamp=2)
    assert len(first.events) == 1
    assert session.set_diagnostic_trace(None)
    assert session.diagnostic_trace is None


def test_source_attempt_mapping_links_capture_epoch_without_source_identifiers():
    trace, client = Trace(), FakeClient()
    session = CameraMediaSession(
        client,
        NS(id=42, name="private"),
        receiver_supervisor=fake_supervisor,
        legacy_audio_enabled=False,
        diagnostic_trace=trace,
    )
    session.acquire(LeasePurpose.HOMEKIT_LIVE)
    callbacks = _receiver_kwargs(client.futures[0])
    callbacks["on_attempt"](1)
    first = session.buffer.capture_epoch
    callbacks["on_attempt_end"](1, None)
    callbacks["on_attempt"](2)
    second = session.buffer.capture_epoch
    assert second > first
    assert trace.events == [
        (
            "lifecycle",
            "session",
            dict(reason="start", source_generation=1, attempt=1, capture_epoch=first),
        ),
        (
            "lifecycle",
            "session",
            dict(reason="start", source_generation=1, attempt=2, capture_epoch=second),
        ),
    ]
    session.stop_all()


@pytest.mark.asyncio
async def test_supervisor_attempt_scope_expires_before_successor_runs():
    trace, stop, retained = Trace(), threading.Event(), []

    class Receiver:
        def __init__(self, _device, _video, _audio, *, diagnostic_trace):
            self.trace = diagnostic_trace
            retained.append(self.trace)

        async def receive(self, _stop):
            if len(retained) == 2:
                retained[0].event("source_frame", "audio", timestamp=900)
            self.trace.event("source_frame", "audio", timestamp=10)
            if len(retained) == 2:
                stop.set()

    await supervise_receiver(
        NS(name="private"),
        None,
        None,
        stop,
        receiver_factory=Receiver,
        receiver_kwargs={"diagnostic_trace": trace},
        retry_min=0,
    )
    assert [fields for _, _, fields in trace.events] == [
        dict(timestamp=10, attempt=1),
        dict(timestamp=10, attempt=2),
    ]
    assert all(item.closed for item in retained)


@pytest.mark.asyncio
async def test_real_recorder_accepts_ingress_schema_without_invalid_records(tmp_path):
    capture = DiagnosticCapture(tmp_path / "capture", duration_seconds=5)
    now = [10.0]
    audio, _, observed = receiver(capture, now)
    try:
        for sequence in (0, 1, 2, 4, 5, 6, 7, 8, 3):
            now[0] += 0.06
            await audio._handle_rtp_packet(rtp(sequence), round(now[0] * 1000))
        install_source_clock_observer(
            audio,
            "audio",
            lambda _observation: None,
            clock=lambda: now[0],
            diagnostic_trace=capture,
        )
        await audio._handle_rtcp_packet(
            RtcpSrPacket(777, RtcpSenderInfo(3_900_000_000 << 32, 48_000, 1, 2))
        )
    finally:
        status = capture.close()
    assert observed == [(b"\xf8\xff\xfe", 0)]
    assert status["finalized"] and status["counters"]["invalid_records"] == 0
    assert status["counters"]["accepted_events"] > 0
    assert status["counters"]["accepted_media"] > 0


@pytest.mark.asyncio
async def test_source_sender_report_trace_retains_mapping_without_identity_or_behavior_change():
    trace, observations = Trace(), []
    source = RTCRtpReceiver("audio", NS(state="new", _stats_id="private"))
    guard = ScopedDiagnosticTrace(trace, lambda: True, source_generation=2, attempt=1)
    observe = install_source_clock_observer(
        source, "audio", observations.append, clock=lambda: 10.0, diagnostic_trace=guard
    )
    mapper = source._RTCRtpReceiver__timestamp_mapper
    mapper.map(4_000_000_000)
    source._RTCRtpReceiver__remote_streams[77] = object()
    source._RTCRtpReceiver__active_ssrc[77] = datetime.now(UTC)
    report = RtcpSrPacket(77, RtcpSenderInfo(3_900_000_000 << 32, 4_000_048_000, 1, 2))
    await source._handle_rtcp_packet(report)
    observe(960)
    assert observations[0].rtp_timestamp == 48000
    assert trace.events == [
        (
            "source_sender_report",
            "audio",
            {
                "rtp_timestamp": 4_000_048_000,
                "ntp_seconds": 3_900_000_000,
                "ntp_fraction": 0,
                "received_ns": 10_000_000_000,
                "origin": 4_000_000_000,
                "source_generation": 2,
                "attempt": 1,
            },
        )
    ]
    assert len(source._RTCRtpReceiver__stats) == 1
    guard.fence()
    await source._handle_rtcp_packet(report)
    assert len(trace.events) == 1
