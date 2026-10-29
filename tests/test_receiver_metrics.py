"""Real aiortc assembly and transparent per-instance observation boundaries."""

import asyncio
import json
from types import SimpleNamespace as NS

import aiortc
import pytest
from aiortc.rtcrtpparameters import RTCRtpCodecParameters
from aiortc.rtcrtpreceiver import RTCRtpReceiver
from aiortc.rtp import RtpPacket

from halo_server.media.receiver_metrics import (
    CAPACITY,
    ReceiverMetricsError,
    ReceiverPipeline,
    install_rtp_observer,
)


def receivers(clock):
    transport = NS(state="new")
    audio = RTCRtpReceiver("audio", transport)
    video = RTCRtpReceiver("video", transport)
    return audio, video, ReceiverPipeline(audio, video, clock=lambda: clock[0])


def packet(sequence):
    value = RtpPacket(payload_type=111, sequence_number=sequence, timestamp=sequence * 2880)
    value.ssrc = 123
    value.payload = b"\xf8\xff\xfe"
    return value


def audio_queue(audio, pipeline):
    audio._RTCRtpReceiver__codecs[111] = RTCRtpCodecParameters(
        mimeType="audio/opus", clockRate=48000, channels=1, payloadType=111
    )
    delivered = []

    class Queue:
        def put(self, value):
            delivered.append(value)
            pipeline.frame("audio")

    audio._RTCRtpReceiver__decoder_queue = Queue()
    audio._RTCRtpReceiver__decoder_thread = True
    return delivered


def test_shared_bins_are_relative_bounded_and_reflect_changed_transport():
    now = [1000000.0]
    audio, video, pipeline = receivers(now)
    pipeline._entry("audio")
    now[0] += 0.25
    pipeline.frame("video")
    pipeline._entry("video")
    snapshot = pipeline.snapshot()
    assert snapshot["shared_dtls"] is True
    assert snapshot["bins"][0]["audio_rtp"] == 1
    assert snapshot["bins"][1] == dict(
        elapsed_ms=100, audio_rtp=0, video_rtp=0, audio_frames=0, video_frames=0
    )
    assert snapshot["bins"][2]["video_frames"] == snapshot["bins"][2]["video_rtp"] == 1
    video._RTCRtpReceiver__transport = NS(state="new")
    assert pipeline.snapshot()["shared_dtls"] is False
    video._RTCRtpReceiver__transport = None
    assert pipeline.snapshot()["shared_dtls"] is None
    now[0] += 1000000
    pipeline._entry("audio")
    snapshot = pipeline.snapshot()
    assert len(snapshot["bins"]) == CAPACITY
    assert sum(row["audio_rtp"] for row in snapshot["bins"]) == 1
    snapshot["bins"][-1]["audio_rtp"] = 999
    assert pipeline.snapshot()["bins"][-1]["audio_rtp"] == 1
    encoded = json.dumps(pipeline.snapshot())
    assert all(word not in encoded for word in ("ssrc", "address", "payload", "timestamp"))
    pipeline.fence()
    pipeline.frame("audio")
    pipeline.wake_lag(1)
    assert pipeline.snapshot() is None
    assert pipeline.metrics.snapshot()["counters"]["audio_frames"] == 0


@pytest.mark.asyncio
async def test_real_jitter_buffer_can_pause_frames_while_routed_rtp_continues():
    now = [10.0]
    audio, _, pipeline = receivers(now)
    delivered = audio_queue(audio, pipeline)
    install_rtp_observer(audio, "audio", pipeline)
    # Real audio prefetch4 plus a missing sequence: assembly cannot complete
    # while later routed/decrypted entries continue. No network is simulated.
    for sequence in (0, 1, 2, 4, 5, 6, 7, 8, 9, 10):
        now[0] = 10 + sequence * 0.06
        await audio._handle_rtp_packet(packet(sequence), round(now[0] * 1000))
    before = pipeline.snapshot()
    assert len(delivered) == 0
    assert sum(row["audio_rtp"] for row in before["bins"]) == 10
    assert sum(row["audio_frames"] for row in before["bins"]) == 0
    now[0] = 10.7
    await audio._handle_rtp_packet(packet(3), 10700)
    after = pipeline.snapshot()
    assert len(delivered) == 1
    assert delivered[0][1].timestamp == 0
    assert after["metrics"]["counters"] == dict(
        audio_rtp=11, video_rtp=0, audio_frames=1, video_frames=0
    )
    assert after["metrics"]["timings"]["audio_handler"]["count"] == 11


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [RuntimeError("private metrics error"), asyncio.CancelledError("private metrics cancellation")],
)
async def test_metric_failure_never_suppresses_real_original_delivery(failure):
    now = [10.0]
    audio, _, pipeline = receivers(now)
    delivered = audio_queue(audio, pipeline)

    class Broken:
        def __getattr__(self, name):
            raise failure

    pipeline.metrics = Broken()
    install_rtp_observer(audio, "audio", pipeline)
    for sequence in range(5):
        now[0] += 0.06
        assert await audio._handle_rtp_packet(packet(sequence), 10000) is None
    assert len(delivered) == 1 and delivered[0][1].data == b"\xf8\xff\xfe"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "original_error",
    [ValueError("private original error"), asyncio.CancelledError("original cancellation")],
)
async def test_observer_preserves_exact_original_exception_or_cancel(original_error):
    now = [10.0]
    audio, _, pipeline = receivers(now)

    class Enabled:
        def __bool__(self):
            raise original_error

    audio._enabled = Enabled()
    install_rtp_observer(audio, "audio", pipeline)
    with pytest.raises(type(original_error)) as caught:
        await audio._handle_rtp_packet(packet(0), 10000)
    assert caught.value is original_error
    assert pipeline.metrics.snapshot()["timings"]["audio_handler"]["count"] == 1


def test_contract_and_double_install_fail_before_changing_handler(monkeypatch):
    now = [10.0]
    audio, _, pipeline = receivers(now)
    original = audio._handle_rtp_packet
    monkeypatch.setattr(aiortc, "__version__", "unsupported")
    with pytest.raises(ReceiverMetricsError):
        install_rtp_observer(audio, "audio", pipeline)
    assert audio._handle_rtp_packet == original
    monkeypatch.setattr(aiortc, "__version__", "1.15.0")
    install_rtp_observer(audio, "audio", pipeline)
    wrapped = audio._handle_rtp_packet
    with pytest.raises(ReceiverMetricsError):
        install_rtp_observer(audio, "audio", pipeline)
    assert audio._handle_rtp_packet is wrapped

    class Changed:
        async def _handle_rtp_packet(self, packet, arrival_time_ms):
            return "private"

    changed = Changed()
    with pytest.raises(ReceiverMetricsError):
        install_rtp_observer(changed, "audio", pipeline)
    assert "_handle_rtp_packet" not in changed.__dict__


def test_clock_and_frame_observation_failures_are_non_authoritative():
    now = [10.0]
    _, _, pipeline = receivers(now)

    def broken():
        raise asyncio.CancelledError("observer only")

    pipeline._clock = broken
    pipeline.frame("audio")
    pipeline.wake_lag(float("nan"))
    assert pipeline.snapshot() is None


def test_huge_time_jump_stays_bounded_and_snapshot_rechecks_retirement():
    now = [1.0]
    audio, video, pipeline = receivers(now)
    now[0] = 1e300
    pipeline.frame("audio")
    snapshot = pipeline.snapshot()
    assert snapshot["end_elapsed_ms"] == 2**53 - 1
    assert len(snapshot["bins"]) == CAPACITY
    assert snapshot["bins"][-1]["audio_frames"] == 1

    class RetiringTransport:
        @property
        def transport(self):
            pipeline.fence()
            return object()

    pipeline._receivers = (RetiringTransport(), video)
    assert pipeline.snapshot() is None
