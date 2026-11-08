"""Original input clocks through owned native audio/video and protected wire."""

import asyncio
import time
from dataclasses import replace
from types import SimpleNamespace as NS

import av
import pytest
from aiortc.rtp import RtcpPacket, RtcpSrPacket, RtpPacket
from test_homekit_audio import AUDIO, crypto, prepared_audio, until
from test_homekit_audio_recovery_native import decode_every_packet, source_packets
from test_homekit_live import (
    CONFIG,
    DeferredAdapter,
    encoded_picture,
    native_crypto,
    observe_video_clock,
    reconstruct,
)
from test_homekit_video_recovery import controlled, output

from halo_server.media.live_timeline import TimelineError


def source_report(kind, pts, receipt, *, offset=0, epoch=1):
    return NS(
        kind=kind,
        rtp_timestamp=pts,
        sender_ntp=(3_900_000_000 << 32) + round(offset * 2**32),
        received_at=receipt,
        capture_epoch=epoch,
        ssrc=11 if kind == "video" else 12,
    )


def signed32(value):
    return ((value + 2**31) & 0xFFFFFFFF) - 2**31


async def receive_track(sock, peer, *, media_count):
    media, report = [], None
    async with asyncio.timeout(4):
        while len(media) < media_count or report is None:
            protected = await asyncio.get_running_loop().sock_recv(sock, 4096)
            if 192 <= protected[1] <= 223:
                report = next(
                    (
                        item
                        for item in RtcpPacket.parse(peer.unprotect_rtcp(protected))
                        if isinstance(item, RtcpSrPacket)
                    ),
                    report,
                )
            else:
                media.append(RtpPacket.parse(peer.unprotect(protected)))
    return media, report


@pytest.mark.asyncio
async def test_same_epoch_reconfigure_keeps_elapsed_source_time_and_srtp_indices(monkeypatch):
    async with controlled(monkeypatch) as (manager, _, session, clock, sink):
        session.state = "active"
        session.adapter = DeferredAdapter(session.config)
        peer = native_crypto(session.prepared.ssrc)
        secure = session.secure
        assert await manager._access_unit(
            session, output(), session.capture_epoch, received_at=clock()
        )
        before = len(sink.sent)
        first = [RtpPacket.parse(peer.unprotect(data)) for _, data in sink.sent]
        clock.now += 2
        await manager.reconfigure(
            session.boot, session.scope, session.identity, replace(session.config, max_bit_rate=400)
        )
        observe_video_clock(manager, session, 180000, clock())
        assert await manager._access_unit(
            session,
            output(
                timestamp=180000,
                configuration_generation=session.adapter.configuration_generation,
            ),
            session.capture_epoch,
            received_at=clock(),
        )
        assert session.pending_reconfiguration is None
        second = [RtpPacket.parse(peer.unprotect(data)) for _, data in sink.sent[before:]]
        assert (second[0].timestamp - first[0].timestamp) & 0xFFFFFFFF == 180000
        assert (second[0].sequence_number - first[-1].sequence_number) & 65535 == 1
        assert second[0].ssrc == first[0].ssrc == session.prepared.ssrc
        assert session.secure is secure and session.timeline.capture_epoch == session.capture_epoch
        assert (
            session.timeline.presentation(
                "video", capture_epoch=session.capture_epoch, source_pts=180000
            )
            == 102
        )


@pytest.mark.asyncio
async def test_original_input_must_be_observed_and_late_old_epoch_cannot_rebase(monkeypatch):
    async with controlled(monkeypatch) as (manager, controller, session, clock, sink):
        assert await manager._access_unit(
            session, output(), session.capture_epoch, received_at=clock()
        )
        old_epoch = session.capture_epoch
        old_packets = len(sink.sent)
        controller.handle.buffer.clear()
        clock.now += 1
        observe_video_clock(manager, session, 0, clock())
        with pytest.raises(TimelineError, match="stale_epoch"):
            session.timeline.observe_input(
                "video", capture_epoch=old_epoch, source_pts=90000, received_at=clock()
            )
        with pytest.raises(TimelineError, match="stale_epoch"):
            session.timeline.report(
                "video", capture_epoch=old_epoch, instant=session.timeline.instant()
            )
        assert len(sink.sent) == old_packets
        assert session.timeline.capture_epoch == controller.handle.capture_epoch
        assert session.timeline.snapshot()["epoch_changes"] == 1


@pytest.mark.asyncio
async def test_real_native_60ms_audio_outputs_and_video_reports_retain_known_av_offset():
    manager, controller, identity, prepared, video, audio = await prepared_audio()
    held = DeferredAdapter(CONFIG)
    held.release.clear()
    manager._adapter_factory = lambda _config: held
    source = source_packets(4)
    video_pts, audio_pts = 90000, 2_000_000_000
    now = time.monotonic()
    controller.handle.source_clocks = (
        source_report("video", video_pts, now),
        source_report("audio", audio_pts, now, offset=0.1),
    )
    controller.handle.buffer.append_video(encoded_picture(), video_pts)
    controller.handle.audio.put_nowait(
        (NS(payload=source[0], rtp_timestamp=audio_pts, received_at=now, capture_epoch=1),)
    )
    feeder = None
    try:
        await manager.start(
            controller.boot,
            controller.scope,
            identity,
            replace(CONFIG, rtcp_interval=0.1),
            audio_config=replace(AUDIO, bitrate_mode="vbr"),
        )
        session = manager._sessions[identity]
        await held.entered.wait()
        await until(lambda: session.timeline.snapshot()["tracks"]["audio"]["initialized"])
        held.release.set()

        async def feed():
            for index, payload in enumerate(source[1:], 1):
                await asyncio.sleep(0.06)
                controller.handle.audio.put_nowait(
                    (
                        NS(
                            payload=payload,
                            rtp_timestamp=audio_pts + index * 2880,
                            received_at=time.monotonic(),
                            capture_epoch=1,
                        ),
                    )
                )

        feeder = asyncio.create_task(feed())
        (pictures, video_sr), (speech, audio_sr) = await asyncio.gather(
            receive_track(video, native_crypto(prepared.ssrc), media_count=1),
            receive_track(audio, crypto(prepared.audio.ssrc), media_count=6),
        )
        assert session.timeline.mode == "source_sender_report"
        assert all(
            (b.timestamp - a.timestamp) & 0xFFFFFFFF == 480
            for a, b in zip(speech, speech[1:], strict=False)
        )
        assert decode_every_packet([packet.payload for packet in speech]).size == len(speech) * 960
        clear = [packet.serialize() for packet in pictures]
        picture = b"".join(b"\0\0\0\1" + nal for nal in reconstruct(clear))
        assert len(av.CodecContext.create("h264", "r").decode(av.Packet(picture))) == 1
        video_info, audio_info = video_sr.sender_info, audio_sr.sender_info
        video_sample = (
            video_info.ntp_timestamp / 2**32
            + signed32(pictures[0].timestamp - video_info.rtp_timestamp) / 90000
        )
        audio_sample = (
            audio_info.ntp_timestamp / 2**32
            + signed32(speech[0].timestamp - audio_info.rtp_timestamp) / 24000
        )
        # libopus lookahead places the first native20ms output 2.5ms before
        # its first60ms input. Original SRs say audio begins100ms after video.
        assert audio_sample - video_sample == pytest.approx(0.0975, abs=0.0001)
        frozen = session.timeline.presentation("audio", capture_epoch=1, source_pts=audio_pts)
        controller.handle.source_clocks = (
            source_report("video", video_pts + 90000, time.monotonic(), offset=1),
            source_report("audio", audio_pts + 48000, time.monotonic(), offset=2.1),
        )
        # A later incoherent source report cannot change the frozen sample map.
        session.timeline.observe_input(
            "audio",
            capture_epoch=1,
            source_pts=audio_pts + 48000,
            received_at=time.monotonic(),
            source_clocks=controller.handle.source_clocks,
        )
        assert (
            session.timeline.presentation("audio", capture_epoch=1, source_pts=audio_pts) == frozen
        )
        assert session.audio.failure is None and not session.retired
    finally:
        held.release.set()
        if feeder is not None:
            feeder.cancel()
            await asyncio.gather(feeder, return_exceptions=True)
        video.close()
        audio.close()
        await manager.close()
    assert manager.pending_resources == 0 and controller.handle.closed
