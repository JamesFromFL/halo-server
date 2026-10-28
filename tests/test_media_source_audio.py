"""Separate original Opus capture and actual per-receiver sender-report clocks."""

import threading
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from aiortc.rtcrtpreceiver import RTCRtpReceiver
from aiortc.rtp import RtcpSenderInfo, RtcpSrPacket
from test_media_session import SPS_PPS_IDR, FakeClient, _receiver_kwargs, fake_supervisor

from halo_server.media import ring_media
from halo_server.media.leases import LeasePurpose
from halo_server.media.ring_media import SourceClockObservation, install_source_clock_observer
from halo_server.media.session import CameraMediaSession
from halo_server.media.supervisor import supervise_receiver


def test_original_audio_is_not_legacy_normalized_and_retires_with_capture_epoch():
    client = FakeClient()
    session = CameraMediaSession(
        client, SimpleNamespace(id=42, name="source"), receiver_supervisor=fake_supervisor
    )
    lease = session.acquire(LeasePurpose.HOMEKIT_LIVE)
    callbacks = _receiver_kwargs(client.futures[0])
    assert callbacks["on_encoded_audio"](b"\xf8\xff\xfe", 12345)
    original = session.original_audio_buffer.read(after_sequence=None, timeout=0).packets
    assert len(original) == 1 and original[0].rtp_timestamp == 12345
    assert original[0].payload == b"\xf8\xff\xfe"
    assert not session.buffer.snapshot().packets  # No invented legacy audio or video boundary.
    callbacks["on_encoded_video"](SPS_PPS_IDR, 4321)
    assert not any(
        packet.payload == original[0].payload for packet in session.buffer.snapshot().packets
    )
    observation = SourceClockObservation("audio", 12, 12000, 1 << 40, session._clock())
    callbacks["on_source_clock"](observation)
    assert session.source_clock_snapshot() == (
        replace(observation, capture_epoch=session.buffer.capture_epoch),
    )
    epoch = session.buffer.capture_epoch
    callbacks["on_attempt_end"](1, None)
    assert session.buffer.capture_epoch > epoch
    assert session.original_audio_buffer.capture_epoch == session.buffer.capture_epoch
    assert (
        not session.original_audio_buffer.snapshot().packets and not session.source_clock_snapshot()
    )
    session.release(lease.id)
    assert not callbacks["on_encoded_audio"](b"\xf8\xff\xfe", 99999)
    callbacks["on_source_clock"](observation)
    assert not session.source_clock_snapshot()


def test_external_source_clear_cannot_leave_original_audio_in_old_epoch():
    client = FakeClient()
    session = CameraMediaSession(
        client, SimpleNamespace(id=42, name="source"), receiver_supervisor=fake_supervisor
    )
    lease = session.acquire(LeasePurpose.HOMEKIT_LIVE)
    callbacks = _receiver_kwargs(client.futures[0])
    callbacks["on_encoded_audio"](b"\xf8\xff\xfe", 1)
    session.buffer.clear()
    callbacks["on_encoded_audio"](b"\xf8\xff\xfe", 100)
    packets = session.original_audio_buffer.read(after_sequence=None, timeout=0).packets
    assert [packet.rtp_timestamp for packet in packets] == [100]
    assert packets[0].capture_epoch == session.buffer.capture_epoch
    session.release(lease.id)


def receiver():
    return RTCRtpReceiver("audio", SimpleNamespace(state="new", _stats_id="offline"))


@pytest.mark.asyncio
async def test_actual_sender_report_is_mapped_into_encoded_domain_preserving_original_processing():
    source = receiver()
    observations = []
    now = [10.0]
    observe = install_source_clock_observer(
        source, "audio", observations.append, clock=lambda: now[0]
    )
    mapper = source._RTCRtpReceiver__timestamp_mapper
    assert mapper.map(4_000_000_000) == 0
    source._RTCRtpReceiver__remote_streams[77] = object()
    source._RTCRtpReceiver__active_ssrc[77] = datetime.now(UTC)
    report = RtcpSrPacket(77, RtcpSenderInfo((3_900_000_000 << 32), 4_000_048_000, 1, 2))
    await source._handle_rtcp_packet(report)
    observe(960)
    assert len(observations) == 1
    assert observations[0].rtp_timestamp == 48000
    assert observations[0].sender_ntp == report.sender_info.ntp_timestamp
    assert source._RTCRtpReceiver__lsr[77] == (report.sender_info.ntp_timestamp >> 16) & 0xFFFFFFFF
    assert len(source._RTCRtpReceiver__stats) == 1
    now[0] += 31
    observe(960)
    assert len(observations) == 2 and observations[-1].sender_ntp == 0


@pytest.mark.asyncio
async def test_unknown_or_invalid_sender_and_absent_origin_have_no_clock_authority():
    source = receiver()
    observations = []
    observe = install_source_clock_observer(source, "audio", observations.append)
    await source._handle_rtcp_packet(RtcpSrPacket(7, RtcpSenderInfo(1 << 40, 500, 1, 2)))
    observe(0)
    assert not observations
    source._RTCRtpReceiver__remote_streams[7] = object()
    source._RTCRtpReceiver__active_ssrc[7] = datetime.now(UTC)
    await source._handle_rtcp_packet(RtcpSrPacket(7, RtcpSenderInfo(1 << 40, 500, 1, 2)))
    observe(0)
    assert not observations  # Real encoded origin has not been observed.


@pytest.mark.asyncio
async def test_source_clock_mapper_handles_rtp_wrap_without_raw_timestamp_relabelling():
    source = receiver()
    observations = []
    observe = install_source_clock_observer(source, "audio", observations.append)
    mapper = source._RTCRtpReceiver__timestamp_mapper
    mapper.map(0xFFFFFFF0)
    timestamp = mapper.map(0x30)
    source._RTCRtpReceiver__remote_streams[7] = object()
    source._RTCRtpReceiver__active_ssrc[7] = datetime.now(UTC)
    await source._handle_rtcp_packet(RtcpSrPacket(7, RtcpSenderInfo(1 << 40, 0x20, 1, 2)))
    observe(timestamp)
    assert timestamp == 64 and observations[0].rtp_timestamp == 48


@pytest.mark.asyncio
async def test_old_known_ssrc_report_cannot_anchor_current_new_rtp_source():
    from datetime import timedelta

    source = receiver()
    observations = []
    observe = install_source_clock_observer(source, "audio", observations.append)
    source._RTCRtpReceiver__timestamp_mapper.map(100)
    now = datetime.now(UTC)
    source._RTCRtpReceiver__remote_streams.update({7: object(), 8: object()})
    source._RTCRtpReceiver__active_ssrc.update({7: now, 8: now + timedelta(seconds=1)})
    await source._handle_rtcp_packet(RtcpSrPacket(7, RtcpSenderInfo(1 << 40, 500, 1, 2)))
    observe(1000)
    assert not observations


@pytest.mark.asyncio
async def test_previously_published_clock_is_invalidated_when_primary_source_becomes_ambiguous():
    source = receiver()
    observations = []
    observe = install_source_clock_observer(source, "audio", observations.append)
    source._RTCRtpReceiver__timestamp_mapper.map(100)
    source._RTCRtpReceiver__remote_streams[7] = object()
    source._RTCRtpReceiver__active_ssrc[7] = datetime.now(UTC)
    await source._handle_rtcp_packet(RtcpSrPacket(7, RtcpSenderInfo(1 << 40, 500, 1, 2)))
    observe(1000)
    assert observations[-1].sender_ntp > 0
    source._RTCRtpReceiver__remote_streams[8] = object()
    source._RTCRtpReceiver__active_ssrc[8] = datetime.now(UTC)
    observe(1000)
    assert len(observations) == 2 and observations[-1].sender_ntp == 0


def test_session_never_exposes_expired_or_explicitly_invalidated_source_clock():
    client = FakeClient()
    now = [0.0]
    session = CameraMediaSession(
        client,
        SimpleNamespace(id=42, name="source"),
        receiver_supervisor=fake_supervisor,
        clock=lambda: now[0],
    )
    lease = session.acquire(LeasePurpose.HOMEKIT_LIVE)
    callbacks = _receiver_kwargs(client.futures[0])
    callbacks["on_source_clock"](SourceClockObservation("audio", 7, 0, 1 << 40, 0))
    assert session.source_clock_snapshot()
    now[0] = 31
    assert not session.source_clock_snapshot()
    callbacks["on_source_clock"](SourceClockObservation("audio", 0, 0, 0, 31))
    assert not session._source_clocks
    session.release(lease.id)


def test_source_observer_fails_closed_on_dependency_or_layout_drift(monkeypatch):
    source = receiver()
    monkeypatch.setattr(ring_media.importlib_metadata, "version", lambda _: "other")
    import aiortc

    monkeypatch.setattr(aiortc, "__version__", "other")
    with pytest.raises(ring_media.EncodedFrameAdapterError):
        install_source_clock_observer(source, "audio", lambda _: None)


@pytest.mark.asyncio
async def test_source_clock_callback_cannot_cross_a_supervisor_attempt_boundary():
    stop = threading.Event()
    saved = []
    observed = []

    class Receiver:
        def __init__(self, *_args, on_source_clock, **_kwargs):
            self.clock = on_source_clock

        async def receive(self, _stop):
            saved.append(self.clock)
            if len(saved) == 1:
                self.clock("first")
                raise RuntimeError("restart")
            assert saved[0]("stale") is False
            self.clock("second")
            stop.set()

    await supervise_receiver(
        object(),
        None,
        None,
        stop,
        receiver_factory=Receiver,
        receiver_kwargs={"on_source_clock": observed.append},
        retry_min=0,
        retry_max=0,
    )
    assert observed == ["first", "second"]


def test_real_microphone_consumption_drops_expired_and_revoked_queued_frames():
    now = [0.0]
    allowed = [True]
    source = ring_media.MicrophonePcmBuffer(clock=lambda: now[0])
    generation = source.begin_intercom()
    source.push_intercom(generation, b"\x01" * 1920, admission=lambda: allowed[0])
    now[0] = 0.3
    source.set_speaker_ready(generation, True)
    assert source.pull() is None  # readiness does not replay the old syllable.
    source.push_intercom(generation, b"\x02" * 1920, admission=lambda: allowed[0])
    allowed[0] = False
    assert source.pull() is None
    assert source.snapshot().dropped_frames == 2 and source.snapshot().consumed_frames == 0


def test_microphone_final_admission_runs_outside_lock_and_generation_is_rechecked():
    source = ring_media.MicrophonePcmBuffer()
    generation = source.begin_intercom()
    source.set_speaker_ready(generation, True)

    def admission():
        assert source._lock.acquire(blocking=False)
        source._lock.release()
        source.end_intercom()
        replacement = source.begin_intercom()
        source.set_speaker_ready(replacement, True)
        return True

    source.push_intercom(generation, b"\x01" * 1920, admission=admission)
    assert source.pull() is None
    assert source.snapshot().consumed_frames == 0


def test_microphone_does_not_admit_frame_aged_during_callback():
    now = [0.0]
    source = ring_media.MicrophonePcmBuffer(clock=lambda: now[0])
    generation = source.begin_intercom()
    source.set_speaker_ready(generation, True)

    def admission():
        now[0] = 1.0
        return True

    source.push_intercom(generation, b"\x01" * 1920, admission=admission)
    assert source.pull() is None


def test_microphone_pull_has_a_finite_retry_budget_even_if_denied_callback_requeues():
    source = ring_media.MicrophonePcmBuffer(max_frames=2)
    generation = source.begin_intercom()
    source.set_speaker_ready(generation, True)
    calls = []

    def deny():
        calls.append(True)
        source.push_intercom(generation, b"\x02" * 1920, admission=deny)
        return False

    source.push_intercom(generation, b"\x01" * 1920, admission=deny)
    assert source.pull() is None and len(calls) == 2
