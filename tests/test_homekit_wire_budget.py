"""Actual manager/SRTP ownership with separate codec and local wire policies."""

import asyncio
import struct
from dataclasses import replace
from fractions import Fraction
from types import SimpleNamespace as NS

import av
import numpy as np
import pytest
from aiortc.rtp import RtcpRtpfbPacket
from test_homekit_live import (
    CONFIG,
    DeferredAdapter,
    encoded_picture,
    native_crypto,
    prepared,
    reconstruct,
    wire_idr,
)
from test_homekit_video_recovery import controlled, output

from halo_server.homekit.models import HomeKitOperationError
from halo_server.media import homekit_live as module
from halo_server.media.h264 import parse_sps, split_annexb
from halo_server.media.video_budget import VideoWirePolicy


@pytest.mark.asyncio
@pytest.mark.parametrize("selected,size", [(132, 10000), (299, 19000)])
async def test_idr_rejected_by_codec_serialization_now_fits_finite_independent_wire_peak(
    monkeypatch, selected, size
):
    async with controlled(monkeypatch, bitrate=selected) as (manager, _, session, clock, sink):
        receipt = clock()
        payload = wire_idr(size)
        assert len(payload) * 8 / (selected * 1000) > 0.5
        assert await manager._access_unit(
            session, output(payload), session.capture_epoch, received_at=receipt
        )
        assert sink.sent and not session.awaiting_idr
        peak = VideoWirePolicy(selected).peak_bps
        for (previous_at, previous), (current_at, _) in zip(sink.sent, sink.sent[1:], strict=False):
            assert current_at >= previous_at + len(previous) * 8 / peak - 1e-12
        assert session.next_packet_at <= receipt + 0.5


@pytest.mark.asyncio
async def test_oversize_count_and_deadline_reject_before_sequence_or_crypto_mutation(monkeypatch):
    async with controlled(monkeypatch, bitrate=132) as (manager, _, session, clock, sink):
        protects = []
        original = session.secure.protect
        session.secure.protect = lambda packet: protects.append(packet) or original(packet)
        for payload, mtu in ((wire_idr(150000), 1200), (wire_idr(1500000), 256)):
            session.config = replace(session.config, mtu=mtu)
            sequence = session.packetizer.sequence
            assert not await manager._access_unit(
                session, output(payload), session.capture_epoch, received_at=clock()
            )
            assert session.packetizer.sequence == sequence
        assert not sink.sent and not protects
        assert session.recovery_attempts == 2


@pytest.mark.asyncio
async def test_wire_oversleep_and_busy_socket_still_have_absolute_original_deadline(monkeypatch):
    async with controlled(monkeypatch) as (manager, _, session, clock, sink):
        deadline = clock() + 0.5
        session.next_packet_at = clock() + 0.490
        clock.overshoot = 0.007
        with pytest.raises(module._VideoDeadline):
            await manager._paced_send(
                session, b"p" * 1000, epoch=session.capture_epoch, deadline=deadline
            )
        assert not sink.sent and clock() < deadline
        assert clock() + 1000 * 8 / 2000000 > deadline

        clock.overshoot = 0
        session.next_packet_at = clock()

        def busy():
            raise BlockingIOError

        sink.before = busy
        deadline = clock() + 0.020
        with pytest.raises(module._VideoDeadline):
            await manager._paced_send(
                session, b"p" * 1000, epoch=session.capture_epoch, deadline=deadline
            )
        assert not sink.sent and clock() < deadline
        assert clock() + 1000 * 8 / 2000000 > deadline


@pytest.mark.asyncio
async def test_complete_au_remains_reserved_and_scope_revocation_fences_after_wait(monkeypatch):
    async with controlled(monkeypatch) as (manager, controller, session, clock, sink):
        pending = []

        def revoke():
            pending.append(True)
            controller.allowed = False

        clock.after_sleep = revoke
        with pytest.raises(HomeKitOperationError):
            await manager._access_unit(
                session, output(wire_idr(19000)), session.capture_epoch, received_at=clock()
            )
        assert pending and len(sink.sent) == 1
        assert not session.retransmit or len(session.retransmit) == 1


@pytest.mark.asyncio
async def test_codec_observation_cannot_drop_idr_or_confuse_actual_with_selected_sps(monkeypatch):
    async with controlled(monkeypatch, bitrate=1) as (manager, _, session, clock, sink):
        payload = encoded_picture()
        info = parse_sps(next(nal for nal in split_annexb(payload) if nal[0] & 31 == 7))
        session.adapter = NS(
            source_format=info,
            output_format=info,
            close=lambda: asyncio.sleep(0),
            configuration_generation=1,
        )
        unit = output(payload)
        unit.capture_epoch, unit.configuration_generation = session.capture_epoch, 1
        manager._observe_outputs(session, (unit,))
        assert await manager._access_unit(session, unit, session.capture_epoch, received_at=clock())
        report = manager._diagnostics(session)
        assert report["codec_source_time_window"]["above_selected_windows"] == 1
        assert report["codec_source_time_window"]["window_coverage"] == "partial"
        assert report["actual_output_format"]["width"] == info.width
        assert report["actual_source_format"]["profile"] == info.profile
        assert report["video_format"]["bitrate_kbps"] == 1
        assert report["local_wire_peak_bps"] == 2000000
        assert sink.sent and session.recovery_attempts == 0


@pytest.mark.asyncio
async def test_real_high_detail_idr_uses_actual_manager_srtp_and_independent_decoder(monkeypatch):
    # An actual high-detail picture at a full selected codec target. The burst
    # is admitted by its finite local wire cost, rather than bitrate labels.
    encoder = av.CodecContext.create("libx264", "w")
    encoder.width, encoder.height = 1280, 720
    encoder.pix_fmt = "yuv420p"
    encoder.time_base, encoder.framerate = Fraction(1, 90000), Fraction(30)
    encoder.bit_rate, encoder.thread_count = 299000, 1
    encoder.options = {
        "preset": "veryfast",
        "tune": "zerolatency",
        "profile": "high",
        "level": "3.1",
        "x264-params": "threads=1:bframes=0:repeat-headers=1:vbv-maxrate=299:vbv-bufsize=299",
    }
    encoder.open()
    yy, xx = np.indices((720, 1280))
    image = np.stack((xx % 256, yy % 256, ((xx // 12 + yy // 12) % 2) * 200), axis=2)
    frame = av.VideoFrame.from_ndarray(image.astype(np.uint8), format="rgb24")
    frame = frame.reformat(format="yuv420p", threads=1)
    frame.pts, frame.time_base = 0, Fraction(1, 90000)
    packets = encoder.encode(frame)
    assert len(packets) == 1 and not encoder.encode(None)
    payload = bytes(packets[0])
    async with controlled(monkeypatch, bitrate=299) as (manager, _, session, clock, sink):
        session.config = replace(CONFIG, width=1280, height=720, profile="high", max_bit_rate=299)
        original = clock()
        clock.now += 0.250
        assert await manager._access_unit(
            session, output(payload), session.capture_epoch, received_at=original
        )
        assert session.next_packet_at <= original + 0.5
        crypto = native_crypto(session.prepared.ssrc)
        clear = [crypto.unprotect(packet) for _, packet in sink.sent]
        delivered = b"".join(b"\0\0\0\1" + nal for nal in reconstruct(clear))
        decoder = av.CodecContext.create("h264", "r")
        decoded = decoder.decode(av.Packet(delivered))
        assert len(decoded) == 1 and (decoded[0].width, decoded[0].height) == (1280, 720)


@pytest.mark.asyncio
async def test_authenticated_duplicate_repair_flood_is_coalesced_and_ciphertext_bounded():
    adapter = DeferredAdapter(CONFIG)
    manager, controller, identity, result, receiver = await prepared(
        adapter_factory=lambda _: adapter
    )
    controller.handle.buffer.append_video(encoded_picture(), 0)
    crypto, peer = native_crypto(result.ssrc), native_crypto(500)
    try:
        await manager.start(controller.boot, controller.scope, identity, CONFIG)
        async with asyncio.timeout(2):
            original = await asyncio.get_running_loop().sock_recv(receiver, 2048)
            clear = crypto.unprotect(original)
            sequence = struct.unpack_from("!H", clear, 2)[0]
            while not clear[1] & 0x80:
                clear = crypto.unprotect(await asyncio.get_running_loop().sock_recv(receiver, 2048))
            request = bytes(RtcpRtpfbPacket(1, 500, result.ssrc, [sequence]))
            for _ in range(80):
                receiver.sendto(peer.protect_rtcp(request), ("127.0.0.1", result.port))
            session = manager._sessions[identity]
            while session.metrics.snapshot()["counters"]["feedback_datagrams"] < 80:
                await asyncio.sleep(0.001)
        counters = session.metrics.snapshot()["counters"]
        async with asyncio.timeout(1):
            while counters["retransmit_packets"] == 0:
                await asyncio.sleep(0.001)
                counters = session.metrics.snapshot()["counters"]
        assert session.nack_count == counters["retransmit_packets"] == 1
        assert counters["repair_duplicate_requests"] == 79
        assert counters["nack_rate_limited"] == 0
        assert len(session.retransmit) <= module.MAX_RETRANSMIT_PACKETS
        assert session.retransmit_bytes <= module.MAX_RETRANSMIT_BYTES
        for _ in range(1):
            assert (
                await asyncio.wait_for(asyncio.get_running_loop().sock_recv(receiver, 2048), 1)
                == original
            )
    finally:
        receiver.close()
        await manager.close()
