"""Real receiver, native audio worker, HomeKit pacer and independent SRTP decode."""

import asyncio
import socket
import time
from types import SimpleNamespace as NS

import pylibsrtp
import pytest
from aiortc.rtcrtpparameters import RTCRtpCodecParameters
from aiortc.rtcrtpreceiver import RTCRtpReceiver
from aiortc.rtp import RtpPacket
from test_homekit_audio_recovery_native import decode_every_packet, source_packets

from halo_server.media.homekit_audio import AudioEndpointRequest, AudioTrack
from halo_server.media.homekit_audio_codec import AudioAdapter
from halo_server.media.live_timeline import LiveTimeline
from halo_server.media.opus import AudioConfiguration, OriginalOpusBuffer
from halo_server.media.opus_release import install_complete_opus_buffer
from halo_server.media.ring_media import install_encoded_frame_adapter


async def run_schedule(schedule, *, enabled=True):
    """One fresh source/view; schedule entries are (delay, seq, RTPts, Opus)."""
    pending = asyncio.Queue()
    original = OriginalOpusBuffer()
    audio = RTCRtpReceiver("audio", NS(state="new"))
    audio._RTCRtpReceiver__codecs[111] = RTCRtpCodecParameters(
        mimeType="audio/opus", clockRate=48000, channels=1, payloadType=111
    )
    release = install_complete_opus_buffer(audio) if enabled else None
    frames, batch, inputs, outputs, failures, sent = [], [], [], [], [], []

    def received(payload, timestamp, **metadata):
        packet = original.append_audio(payload, timestamp, **metadata)
        batch.append(packet)
        frames.append(packet)

    install_encoded_frame_adapter(
        audio, "audio", received, forward_to_decoder=False, opus_release=release
    )
    audio._RTCRtpReceiver__decoder_thread = True

    class MeasuredAdapter(AudioAdapter):
        async def feed(self, payload, timestamp, *, capture_epoch):
            inputs.append((payload, timestamp, capture_epoch))
            result = await super().feed(payload, timestamp, capture_epoch=capture_epoch)
            outputs.extend(result)
            return result

    class Handle:
        async def read_audio(self, *, timeout):
            try:
                packets = await asyncio.wait_for(pending.get(), timeout)
            except TimeoutError:
                packets = ()
            return NS(packets=packets, capture_epoch=1, resynchronized=False)

    # All networking remains loopback. SRTP is independently authenticated and
    # decoded here, including every output packet and exact 24k RTP stamps.
    sink = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sink.bind(("127.0.0.1", 0))
    sink.setblocking(False)
    track = AudioTrack(
        AudioEndpointRequest(sink.getsockname()[1], b"A" * 16, b"S" * 14),
        bind=("127.0.0.1", 0),
        endpoint=sink.getsockname(),
        family=socket.AF_INET,
        excluded_ssrc=1,
        check=lambda _: None,
        epoch=lambda: 1,
        controller=None,
        boot=None,
        scope=None,
        failure=failures.append,
        touch_feedback=lambda _: None,
        last_feedback=time.monotonic,
        adapter_factory=MeasuredAdapter,
    )
    decoder = pylibsrtp.Session(
        pylibsrtp.Policy(
            key=b"A" * 16 + b"S" * 14,
            ssrc_type=pylibsrtp.Policy.SSRC_SPECIFIC,
            ssrc_value=track.prepared.ssrc,
        )
    )

    async def read_udp():
        while True:
            data = await asyncio.get_running_loop().sock_recv(sink, 4096)
            if not 192 <= data[1] <= 223:
                sent.append((time.monotonic(), RtpPacket.parse(decoder.unprotect(data))))
            else:
                decoder.unprotect_rtcp(data)

    drainer = asyncio.create_task(read_udp())
    authorized = time.monotonic()
    timeline = LiveTimeline(audio_rate=24000, video_origin=10000, audio_origin=20000)
    timeline.observe_input("video", capture_epoch=1, source_pts=0, received_at=authorized)
    timeline.sent("video", capture_epoch=1, source_pts=0)
    started = False
    try:
        for delay, sequence, timestamp, payload in schedule:
            await asyncio.sleep(max(0, authorized + delay - time.monotonic()))
            packet = RtpPacket(payload_type=111, sequence_number=sequence, timestamp=timestamp)
            packet.ssrc, packet.payload = 123, payload
            batch.clear()
            await audio._handle_rtp_packet(packet, round(time.monotonic() * 1000))
            if batch:
                pending.put_nowait(tuple(batch))
                if not started:
                    # Real manager order: source readiness precedes track.start.
                    track.start(
                        Handle(),
                        AudioConfiguration(24000, 20, "vbr", 24),
                        speaker_volume=100,
                        authorized_at=authorized,
                        timeline=timeline,
                    )
                    started = True
        await asyncio.sleep(0.7)
        assert not failures and track.failure is None
        diagnostics = track.diagnostics()
        retained = track._retained_start
        instant = timeline.instant()
        reports = {
            kind: timeline.report(kind, capture_epoch=1, instant=instant)
            for kind in ("video", "audio")
        }
    finally:
        await track.close()
        drainer.cancel()
        await asyncio.gather(drainer, return_exceptions=True)
        sink.close()
    return NS(
        frames=frames,
        inputs=inputs,
        outputs=outputs,
        sent=sent,
        diagnostics=diagnostics,
        retained=retained,
        authorized=authorized,
        timeline=timeline,
        reports=reports,
    )


@pytest.mark.asyncio
async def test_prefetched_native_audio_uses_existing_pacer_across_periodic_arrival_holes():
    payloads = source_packets(61)
    # A 200ms source callback hole repeats at two-second boundaries. The
    # current four-packet retention already covers it; no new reservoir exists.
    schedule = []
    for index, payload in enumerate(payloads):
        delay = index * 0.06
        if 1.3 <= delay % 2 < 1.5:
            delay = int(delay // 2) * 2 + 1.5
        schedule.append((delay, index, index * 2880, payload))
    result = await run_schedule(schedule)
    assert result.retained
    assert [p.payload for p in result.frames] == payloads
    assert [row[0] for row in result.inputs] == payloads
    assert len(result.sent) == len(result.outputs) == 61 * 3 - 1
    counters = result.diagnostics["metrics"]["counters"]
    assert counters["source_initial_skipped_samples"] == 0
    assert counters["source_stale_discarded_samples"] == counters["output_stale_samples"] == 0
    assert counters["adapter_recreates"] == 0
    assert result.diagnostics["metrics"]["maxima"]["retained_output_packets"] <= 32
    assert result.diagnostics["metrics"]["maxima"]["retained_output_bytes"] <= 40 * 1024
    wire = [packet for _, packet in result.sent]
    assert all(
        (b.timestamp - a.timestamp) % 2**32 == 480 for a, b in zip(wire, wire[1:], strict=False)
    )
    intervals = [b[0] - a[0] for a, b in zip(result.sent, result.sent[1:], strict=False)]
    # Real event-loop/worker test, allowing 60ms scheduling noise while
    # detecting the source's deliberate recurring 200ms stalls.
    assert max(intervals) < 0.08
    assert result.sent[-1][0] - result.sent[0][0] >= (len(wire) - 1) * 0.02 - 0.025
    pcm = decode_every_packet([p.payload for p in wire])
    assert len(pcm) == len(wire) * 960
    # Audio's actual source samples and the shared video SR project onto one
    # wall instant. Prefetch/send delay cannot silently become an A/V phase.
    audio, video = result.reports["audio"], result.reports["video"]
    assert audio.wall_time == video.wall_time
    original_offset = result.frames[0].received_at - result.authorized
    phase = ((video.rtp_timestamp - 10000) % 2**32) / 90000 - (
        (audio.rtp_timestamp - 20000) % 2**32
    ) / 24000
    assert phase == pytest.approx(original_offset, abs=1 / 24000)
    first = result.outputs[0]
    assert wire[0].timestamp == (20000 + round(first.timestamp / 2)) % 2**32
    # Viewing delay is measurable and distinct from original-media phase.
    first_presentation = result.timeline.presentation(
        "audio", capture_epoch=1, source_pts=first.timestamp
    )
    assert 0.20 < result.sent[0][0] - first_presentation < 0.6
