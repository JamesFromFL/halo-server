"""Actual pinned RTP receive, bounded Opus drain, original receipts and samples."""

from types import SimpleNamespace as NS

import pytest
from aiortc.rtcrtpparameters import RTCRtpCodecParameters
from aiortc.rtcrtpreceiver import RTCRtpReceiver
from aiortc.rtp import RtpPacket

from halo_server.media.opus import OriginalOpusBuffer
from halo_server.media.opus_release import install_complete_opus_buffer
from halo_server.media.ring_media import install_encoded_frame_adapter


def receiver(now, *, enabled=True):
    audio = RTCRtpReceiver("audio", NS(state="new"))
    audio._RTCRtpReceiver__codecs[111] = RTCRtpCodecParameters(
        mimeType="audio/opus", clockRate=48000, channels=1, payloadType=111
    )
    release = install_complete_opus_buffer(audio, clock=lambda: now[0]) if enabled else None
    frames = []
    install_encoded_frame_adapter(
        audio,
        "audio",
        lambda payload, pts, **metadata: frames.append((payload, pts, now[0], metadata)),
        forward_to_decoder=False,
        opus_release=release,
    )
    audio._RTCRtpReceiver__decoder_thread = True
    return audio, frames


def packet(index, *, sequence=0, timestamp=0, payload=b"\x58\x01"):
    p = RtpPacket(
        payload_type=111,
        sequence_number=(sequence + index) % 65536,
        timestamp=(timestamp + index * 2880) % 2**32,
    )
    p.ssrc = 123
    p.payload = payload
    return p


async def feed(audio, now, index, **options):
    await audio._handle_rtp_packet(packet(index, **options), round(now[0] * 1000))


@pytest.mark.asyncio
async def test_same_startup_boundary_releases_bounded_complete_original_packets():
    now = [100.0]
    audio, frames = receiver(now)
    old, baseline = receiver(now, enabled=False)
    for index in range(5):
        now[0] = 100 + index * 0.06
        await feed(audio, now, index)
        await feed(old, now, index)
        if index < 4:
            assert frames == baseline == []
    assert len(frames) == 4 and len(baseline) == 1
    assert frames[0][:3] == baseline[0][:3]
    assert [r[1] for r in frames] == [0, 2880, 5760, 8640]
    assert [r[3]["received_at"] for r in frames] == [100, 100.06, 100.12, 100.18]
    assert frames[0][3] == {
        "received_at": 100,
        "startup_at": 100.24,
        "startup_samples": 11520,
    }
    now[0] = 100.3
    await feed(audio, now, 5)
    assert [r[1] for r in frames] == [i * 2880 for i in range(6)]
    assert all(r[0] == b"\x58\x01" for r in frames)
    assert not any(audio._RTCRtpReceiver__jitter_buffer._packets)


@pytest.mark.asyncio
async def test_reorder_missing_duplicate_and_original_receipt_are_not_invented():
    now = [100.0]
    audio, frames = receiver(now)
    for index in range(5):
        now[0] += 0.06
        await feed(audio, now, index)
    for index in (6, 7, 8):
        now[0] += 0.06
        await feed(audio, now, index)
    assert [r[1] for r in frames] == [i * 2880 for i in range(5)]
    receipt6 = audio._RTCRtpReceiver__jitter_buffer._packets[6]._halo_received_at
    now[0] += 0.02
    await feed(audio, now, 6)  # queued duplicate does not replace the first receipt
    now[0] += 0.02
    await feed(audio, now, 5)
    assert [r[1] for r in frames] == [i * 2880 for i in range(9)]
    assert frames[6][3]["received_at"] == receipt6 < frames[5][3]["received_at"]
    before = len(frames)
    await feed(audio, now, 5)  # already delivered duplicate is suppressed
    assert len(frames) == before
    original = OriginalOpusBuffer(clock=lambda: now[0])
    for payload, pts, _, metadata in frames:
        original.append_audio(payload, pts, **metadata)
    saved = original.snapshot().packets
    assert [p.received_at for p in saved] == [r[3]["received_at"] for r in frames]


@pytest.mark.asyncio
async def test_sequence_and_timestamp_wrap_and_absent_packet_remain_bounded():
    now = [100.0]
    audio, frames = receiver(now)
    for index in (*range(7), *range(8, 40)):
        now[0] += 0.06
        before = len(frames)
        await feed(audio, now, index, sequence=65532, timestamp=2**32 - 5760)
        assert len(frames) - before <= 4
        assert sum(p is not None for p in audio._RTCRtpReceiver__jitter_buffer._packets) <= 16
    timestamps = [f[1] for f in frames]
    assert timestamps == sorted(set(timestamps))
    assert 7 * 2880 not in timestamps  # lost content is never synthesized
    assert timestamps[-1] == 39 * 2880


@pytest.mark.asyncio
async def test_malformed_payload_is_rejected_without_refresh_or_unbounded_work():
    now = [100.0]
    audio, frames = receiver(now)
    for index in range(4):
        await feed(audio, now, index)
    await feed(audio, now, 4, payload=b"\xff")
    assert not frames
    await feed(audio, now, 4)
    assert len(frames) == 4


@pytest.mark.asyncio
async def test_variable_opus_packet_durations_and_dtx_preserve_exact_samples():
    now = [100.0]
    audio, frames = receiver(now)
    # Complete 10,20,40,60,120ms packets; valid framing, no decoded claim.
    payloads = [b"\x40\x01", b"\x48\x01", b"\x50\x01", b"\x58\x01", b"\x59\x01\x01"]
    durations = [480, 960, 1920, 2880, 5760]
    pts = 0
    for index, (payload, duration) in enumerate(zip(payloads * 2, durations * 2, strict=True)):
        if index == 6:
            pts += 48000  # DTX: omitted source time stays omitted.
        p = packet(index, payload=payload)
        p.timestamp = pts
        await audio._handle_rtp_packet(p, round(now[0] * 1000))
        now[0] += duration / 48000
        pts += duration
    assert [f[0] for f in frames] == payloads * 2
    assert frames[0][3]["startup_samples"] == sum(durations[:4])
    assert frames[6][1] - frames[5][1] == 48000 + 480


@pytest.mark.asyncio
async def test_codec_transition_never_relabels_foreign_payload_or_loses_receipt():
    now = [100.0]
    audio, frames = receiver(now)
    audio._RTCRtpReceiver__codecs[0] = RTCRtpCodecParameters(
        mimeType="audio/PCMU", clockRate=8000, channels=1, payloadType=0
    )
    for index in range(5):
        await feed(audio, now, index)
    p = packet(5, payload=b"foreign")
    p.payload_type = 0
    await audio._handle_rtp_packet(p, 100000)
    count = len(frames)
    for index in range(6, 10):
        await feed(audio, now, index)
        assert len(frames) == count
    await feed(audio, now, 10)
    assert len(frames) == count + 4
    assert all(f[0] == b"\x58\x01" and "received_at" in f[3] for f in frames)


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_ring_opt_in_retains_legacy_callback_contract_and_retires(monkeypatch, enabled):
    import threading

    from test_ring_media import FakeDevice, install_media_fakes

    from halo_server.media.ring_media import RingStreamReceiver

    peer = install_media_fakes(monkeypatch)
    stop, received, receivers = threading.Event(), [], {}

    def transceiver(track, **_kwargs):
        kind = track if isinstance(track, str) else track.kind
        receiver = RTCRtpReceiver(kind, NS(state="new"))
        if kind == "audio":
            receiver._RTCRtpReceiver__codecs[111] = RTCRtpCodecParameters(
                mimeType="audio/opus", clockRate=48000, channels=1, payloadType=111
            )
        receivers[kind] = receiver
        return NS(kind=kind, receiver=receiver)

    peer.addTransceiver = transceiver

    async def generated(_session, _callback):
        receivers["audio"]._RTCRtpReceiver__decoder_thread = True
        for index in range(5):
            await receivers["audio"]._handle_rtp_packet(packet(index), index * 60)
        stop.set()

    def legacy(payload, timestamp):
        received.append((payload, timestamp, {}))

    def complete(payload, timestamp, **metadata):
        received.append((payload, timestamp, metadata))

    receiver = RingStreamReceiver(
        FakeDevice(on_generate=generated),
        None,
        on_encoded_audio=complete if enabled else legacy,
        decode_audio=False,
        complete_audio=enabled,
    )
    await receiver.receive(stop)
    assert len(received) == (4 if enabled else 1)
    assert bool(received[0][2]) is enabled
    assert peer.closed


@pytest.mark.asyncio
async def test_real_recorder_observes_complete_release_and_generation_fence(tmp_path):
    from halo_server.media.diagnostic_capture import DiagnosticCapture
    from halo_server.media.receiver_metrics import ReceiverPipeline, install_rtp_observer

    now = [100.0]
    audio, frames = receiver(now)
    video = RTCRtpReceiver("video", NS(state="new"))
    capture = DiagnosticCapture(tmp_path / "capture", duration_seconds=5)
    pipeline = ReceiverPipeline(audio, video, clock=lambda: now[0], diagnostic_trace=capture)
    try:
        install_rtp_observer(audio, "audio", pipeline)
        for index in range(6):
            now[0] += 0.06
            await feed(audio, now, index)
        assert len(frames) == 6
        pipeline.fence()
        before = capture.status()["counters"]["accepted_events"]
        await feed(audio, now, 6)
        assert capture.status()["counters"]["accepted_events"] == before
    finally:
        status = capture.close()
    assert status["finalized"] and status["counters"]["invalid_records"] == 0
    assert status["counters"]["accepted_events"] >= 18


@pytest.mark.asyncio
async def test_complete_release_forwards_each_original_frame_once_to_legacy_decoder():
    now = [100.0]
    audio = RTCRtpReceiver("audio", NS(state="new"))
    codec = RTCRtpCodecParameters(
        mimeType="audio/opus", clockRate=48000, channels=1, payloadType=111
    )
    audio._RTCRtpReceiver__codecs[111] = codec
    release = install_complete_opus_buffer(audio, clock=lambda: now[0])
    observed = []
    adapter = install_encoded_frame_adapter(
        audio,
        "audio",
        lambda payload, pts, **_: observed.append((payload, pts)),
        forward_to_decoder=True,
        opus_release=release,
    )
    audio._RTCRtpReceiver__decoder_thread = True
    for index in range(5):
        now[0] += 0.06
        await feed(audio, now, index)
    forwarded = [adapter.get_nowait() for _ in range(4)]
    assert adapter.empty()
    assert all(item[0] is codec for item in forwarded)
    assert [(frame.data, frame.timestamp) for _, frame in forwarded] == observed
    assert len(observed) == 4
    adapter.put(None)
    assert adapter.get_nowait() is None
