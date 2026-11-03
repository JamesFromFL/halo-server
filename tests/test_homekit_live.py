"""Native video wire data, source ownership and cancellation without Ring IO."""

import asyncio
import errno
import hashlib
import hmac
import socket
import struct
import time
import tracemalloc
from dataclasses import replace
from fractions import Fraction
from functools import lru_cache
from types import SimpleNamespace
from uuid import uuid4

import av
import numpy as np
import pylibsrtp
import pytest
from aiortc.rtp import RtcpPacket, RtcpPsfbPacket, RtcpRrPacket, RtcpRtpfbPacket
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from halo_server.homekit.models import HomeKitOperationError, MediaScope
from halo_server.media import homekit_live as module
from halo_server.media.buffer import EncodedMediaBuffer
from halo_server.media.h264 import VideoConfiguration
from halo_server.media.homekit_live import HomeKitLiveManager, _SourceChanged
from halo_server.media.rtp import (
    MAX_UDP_PAYLOAD_BYTES,
    H264Packetizer,
    RtpError,
    annexb_nalus,
    parse_feedback,
    sender_report,
)
from halo_server.media.srtp import SecureVideo, SrtpError

KEY = bytes.fromhex("e1f97a0d3e018be0d64fa32c06de4139")
SALT = bytes.fromhex("0ec675ad498afeebb6960b3aabe6")
CONFIG = VideoConfiguration(320, 240, 15, "baseline", "3.1", 300)


def native_crypto(ssrc):
    return pylibsrtp.Session(
        pylibsrtp.Policy(key=KEY + SALT, ssrc_type=pylibsrtp.Policy.SSRC_SPECIFIC, ssrc_value=ssrc)
    )


def reconstruct(packets):
    nalus, fragment = [], None
    for packet in packets:
        payload = packet[12:]
        if payload[0] & 31 == 28:
            if payload[1] & 0x80:
                assert fragment is None
                fragment = bytes(((payload[0] & 0xE0) | (payload[1] & 31),))
            assert fragment is not None
            fragment += payload[2:]
            if payload[1] & 0x40:
                nalus.append(fragment)
                fragment = None
        elif payload[0] & 31 == 24:
            assert fragment is None and len(payload) > 1
            offset = 1
            while offset < len(payload):
                assert offset + 2 <= len(payload)
                count = struct.unpack_from("!H", payload, offset)[0]
                offset += 2
                assert count and offset + count <= len(payload)
                nal = payload[offset : offset + count]
                assert 1 <= nal[0] & 31 <= 23
                nalus.append(nal)
                offset += count
            assert offset == len(payload)
        else:
            assert fragment is None
            nalus.append(payload)
    assert fragment is None
    return tuple(nalus)


def test_rtp_header_known_vector_and_sequence_wrap():
    sender = H264Packetizer(0x12345678, sequence=65535)
    one = sender.packetize(b"\0\0\0\1\x61\x01\x02", 0xABCDEF01, payload_type=99, mtu=128)
    assert one[0].packet.hex() == "80e3ffffabcdef0112345678610102"
    assert sender.sequence == 0
    two = sender.packetize(b"\0\0\1\x61xx", 0, payload_type=96, mtu=1200)
    assert two[0].sequence == 0 and two[0].timestamp == 0


@pytest.mark.parametrize("mtu", [128, 512, 1200, 1228, 1378])
def test_full_idr_fu_a_roundtrip_preserves_each_nal_and_final_datagram_budget(mtu):
    nalus = (b"\x67SPS", b"\x68PPS", b"\x65" + b"\xab" * 200_000)
    source = b"".join(b"\0\0\0\1" + nal for nal in nalus)
    sender = H264Packetizer(123)
    packets = sender.packetize(source, 90000, payload_type=99, mtu=mtu)
    assert len(packets) > 10
    assert all(len(packet.packet) + 10 <= min(mtu, MAX_UDP_PAYLOAD_BYTES) for packet in packets)
    assert all(not packet.packet[1] & 0x80 for packet in packets[:-1])
    assert packets[-1].packet[1] & 0x80
    assert reconstruct([packet.packet for packet in packets]) == nalus


@pytest.mark.parametrize("mtu", [None, True, False, "1378", 1378.0, -1, 0, 127, 1379, 65535])
def test_invalid_peer_mtu_rejects_before_media_access_or_sequence_consumption(mtu):
    sender = H264Packetizer(123, sequence=65535)
    # This deliberately is not media: invalid negotiation must reject before
    # reading/copying an access unit or consuming any RTP/SRTP sequence index.
    with pytest.raises(RtpError, match="Invalid negotiated UDP packet limit"):
        sender.packetize(object(), 90000, payload_type=99, mtu=mtu)
    assert sender.sequence == 65535


@pytest.mark.parametrize("payload", [b"", b"invalid", b"\0\0\1", b"\0\0\1\x80bad", b"\0\0\1\x7cFU"])
def test_invalid_annexb_is_rejected(payload):
    with pytest.raises(RtpError):
        annexb_nalus(payload)


def test_sender_report_ntp_reference_and_counter_wrap():
    report = RtcpPacket.parse(sender_report(123, 0x100000001, 0x100000002, 0x100000003, now=1.5))[0]
    assert report.ssrc == 123
    assert report.sender_info.ntp_timestamp == (2_208_988_801 << 32) | 0x80000000
    assert report.sender_info.rtp_timestamp == 1
    assert report.sender_info.packet_count == 2 and report.sender_info.octet_count == 3


def test_bounded_feedback_handles_empty_rr_pli_nack_and_wrong_media_source():
    payload = bytes(RtcpRrPacket(ssrc=7)) + bytes(RtcpPsfbPacket(fmt=1, ssrc=7, media_ssrc=9))
    payload += b"\x81\xcd\x00\x03" + struct.pack("!IIHH", 7, 9, 65535, 3)
    parsed = parse_feedback(payload, 9)
    assert parsed.pli and parsed.nack == (65535, 0, 1) and parsed.reports == 0
    assert not parse_feedback(payload, 10).pli
    with pytest.raises(RtpError):
        parse_feedback(b"\x80\xc9\xff\xffabcd", 9)


def test_srtp_matches_independent_rfc3711_aes_ctr_hmac_vector():
    # RFC3711 appendix B.3 derived keys, then independent encryption/authentication.
    encryption = bytes.fromhex("c61e7a93744f39ee10734afe3ff7a087")
    authentication = bytes.fromhex("cebe321f6ff7716b6fd4ab49af256a156d38baa4")
    session_salt = bytes.fromhex("30cbbc08863d8c85d49db34a9ae1")
    ssrc, sequence = 0xCAFEBABE, 0x1234
    header = struct.pack("!BBHII", 0x80, 99, sequence, 0xDECAFBAD, ssrc)
    plain = b"\xab" * 32
    iv = int.from_bytes(session_salt + b"\0\0", "big") ^ (ssrc << 64) ^ (sequence << 16)
    cipher = Cipher(algorithms.AES(encryption), modes.CTR(iv.to_bytes(16, "big"))).encryptor()
    encrypted = header + cipher.update(plain) + cipher.finalize()
    expected = (
        encrypted + hmac.new(authentication, encrypted + bytes(4), hashlib.sha1).digest()[:10]
    )
    secure = SecureVideo(KEY, SALT, ssrc)
    try:
        assert secure.protect(header + plain) == expected
    finally:
        secure.close()
    with pytest.raises(SrtpError):
        secure.protect(header + plain)


def test_srtcp_tamper_replay_and_sender_pinning_do_not_accept_bad_authentication():
    secure = SecureVideo(KEY, SALT, 10)
    peer = native_crypto(20)
    packet = peer.protect_rtcp(bytes(RtcpRrPacket(ssrc=20)))
    forged = packet[:-1] + bytes((packet[-1] ^ 1,))
    with pytest.raises(SrtpError):
        secure.unprotect_rtcp(forged)
    assert secure.unprotect_rtcp(packet) == bytes(RtcpRrPacket(ssrc=20))
    with pytest.raises(SrtpError):
        secure.unprotect_rtcp(packet)
    another = native_crypto(30).protect_rtcp(bytes(RtcpRrPacket(ssrc=30)))
    with pytest.raises(SrtpError, match="sender"):
        secure.unprotect_rtcp(another)
    next_packet = peer.protect_rtcp(bytes(RtcpRrPacket(ssrc=20)))
    assert secure.unprotect_rtcp(next_packet) == bytes(RtcpRrPacket(ssrc=20))
    secure.close()


def test_native_srtp_sequence_and_timestamp_rollover_rejects_tamper_and_replay():
    sender, receiver = SecureVideo(KEY, SALT, 123), native_crypto(123)
    try:
        encrypted = []
        plaintext = []
        for index, sequence in enumerate((65534, 65535, 0, 1)):
            packet = (
                struct.pack(
                    "!BBHII", 0x80, 99, sequence, (0xFFFFFF00 + index * 3000) & 0xFFFFFFFF, 123
                )
                + bytes([index]) * 32
            )
            plaintext.append(packet)
            encrypted.append(sender.protect(packet))
        assert receiver.unprotect(encrypted[0]) == plaintext[0]
        assert receiver.unprotect(encrypted[1]) == plaintext[1]
        forged = encrypted[2][:-1] + bytes((encrypted[2][-1] ^ 1,))
        with pytest.raises(pylibsrtp.Error):
            receiver.unprotect(forged)
        assert receiver.unprotect(encrypted[2]) == plaintext[2]
        assert receiver.unprotect(encrypted[3]) == plaintext[3]
        with pytest.raises(pylibsrtp.Error):
            receiver.unprotect(encrypted[0])
    finally:
        sender.close()


@lru_cache
def encoded_picture(channel=0):
    encoder = av.CodecContext.create("libx264", "w")
    encoder.width, encoder.height = 320, 240
    encoder.pix_fmt, encoder.time_base, encoder.framerate = "yuv420p", Fraction(1, 15), Fraction(15)
    encoder.options = {
        "profile": "baseline",
        "level": "3.1",
        "preset": "veryfast",
        "tune": "zerolatency",
        "x264-params": "bframes=0:repeat-headers=1:annexb=1",
    }
    encoder.bit_rate = 300000
    encoder.open()
    image = np.zeros((240, 320, 3), dtype=np.uint8)
    image[:, :, channel] = 255
    frame = av.VideoFrame.from_ndarray(image, format="rgb24").reformat(format="yuv420p")
    frame.pts = 0
    return b"".join(bytes(packet) for packet in encoder.encode(frame) + encoder.encode(None))


def wire_idr(size):
    """Real selected headers plus a synthetic large NAL for packet-budget tests."""
    headers = b"".join(
        b"\0\0\1" + nal for nal in annexb_nalus(encoded_picture()) if nal[0] & 31 in {7, 8}
    )
    return headers + b"\0\0\1\x65" + b"\xab" * size


def observe_video_clock(manager, session, timestamp=0, received_at=None):
    from halo_server.media.live_timeline import LiveTimeline

    if session.timeline is None:
        session.timeline = LiveTimeline(
            audio_rate=None, video_origin=session.output_base, clock=manager._clock
        )
    session.capture_epoch = session.handle.capture_epoch
    session.timeline.observe_input(
        "video",
        capture_epoch=session.capture_epoch,
        source_pts=timestamp,
        received_at=manager._clock() if received_at is None else received_at,
        source_clocks=tuple(getattr(session.handle, "source_clocks", ())),
    )


class Handle:
    def __init__(self):
        self.buffer = EncodedMediaBuffer()
        self.cursor = None
        self.closed = False
        self.close_entered = self.close_release = None

    @property
    def capture_epoch(self):
        return self.buffer.capture_epoch

    def is_current(self):
        return not self.closed

    async def read(self, *, timeout):
        result = self.buffer.read(after_sequence=self.cursor, timeout=0)
        if result.packets:
            self.cursor = result.packets[-1].sequence
        else:
            await asyncio.sleep(0.005)
        return result

    async def renew(self):
        return self.is_current()

    async def close(self):
        if self.close_entered is not None:
            self.close_entered.set()
            await self.close_release.wait()
        self.closed = True


class Controller:
    def __init__(self):
        self.boot = str(uuid4())
        self.scope = MediaScope(str(uuid4()), "camera", 1, 1, 1)
        self.allowed = True
        self.opens = 0
        self.handle = Handle()
        self.acquire_entered = asyncio.Event()
        self.acquire_release = None

    def authorize(self, boot, scope, permission):
        if (
            not self.allowed
            or (boot, scope) != (self.boot, self.scope)
            or permission != "streaming"
        ):
            raise HomeKitOperationError("policy_denied")

    async def open_live(self, boot, scope, **_kwargs):
        self.authorize(boot, scope, "streaming")
        self.opens += 1
        self.acquire_entered.set()
        if self.acquire_release is not None:
            try:
                await self.acquire_release.wait()
            except asyncio.CancelledError:
                await self.acquire_release.wait()
        return self.handle


async def prepared(*, adapter_factory=None, address="127.0.0.1"):
    controller = Controller()
    manager = HomeKitLiveManager(controller, adapter_factory=adapter_factory)
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    receiver = socket.socket(family, socket.SOCK_DGRAM)
    try:
        receiver.bind((address, 0))
    except OSError as exc:
        receiver.close()
        if family == socket.AF_INET6 and exc.errno in {errno.EADDRNOTAVAIL, errno.EAFNOSUPPORT}:
            pytest.skip("The qualified test host has no IPv6 loopback address")
        raise
    receiver.setblocking(False)
    identity = str(uuid4())
    result = await manager.prepare(
        controller.boot,
        controller.scope,
        identity,
        source_address=address,
        target_address=address,
        address_version="ipv6" if family == socket.AF_INET6 else "ipv4",
        target_port=receiver.getsockname()[1],
        key=KEY,
        salt=SALT,
    )
    return manager, controller, identity, result, receiver


@pytest.mark.asyncio
@pytest.mark.parametrize("address", ["127.0.0.1", "::1"])
async def test_prepare_owns_real_bound_port_without_capturing_and_stop_is_idempotent(address):
    manager, controller, identity, result, receiver = await prepared(address=address)
    try:
        assert controller.opens == 0 and result.port > 0
        assert result.key == KEY and result.salt == SALT
        assert KEY.hex() not in repr(result) and SALT.hex() not in repr(result)
        assert manager.pending_resources == 1
        await manager.stop(controller.boot, controller.scope, identity)
        await manager.stop(controller.boot, controller.scope, identity)
        assert manager.pending_resources == 0 and controller.opens == 0
        with pytest.raises(HomeKitOperationError, match="unavailable"):
            await manager.prepare(
                controller.boot,
                controller.scope,
                identity,
                source_address=address,
                target_address=address,
                address_version="ipv6" if ":" in address else "ipv4",
                target_port=receiver.getsockname()[1],
                key=KEY,
                salt=SALT,
            )
    finally:
        receiver.close()
        await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mtu", [1200, 1228, 1378])
async def test_real_udp_srtp_reconstructs_and_decodes_picture_and_timer_sender_report(mtu):
    manager, controller, identity, result, receiver = await prepared()
    payload = encoded_picture()
    controller.handle.buffer.append_video(payload, 90000)
    crypto = native_crypto(result.ssrc)
    clear_packets = []
    config = replace(CONFIG, mtu=mtu)
    try:
        await manager.start(controller.boot, controller.scope, identity, config)
        async with asyncio.timeout(5):
            while True:
                protected = await asyncio.get_running_loop().sock_recv(receiver, 2048)
                assert len(protected) <= min(config.mtu, MAX_UDP_PAYLOAD_BYTES)
                packet = crypto.unprotect(protected)
                clear_packets.append(packet)
                if packet[1] & 0x80:
                    break
        recovered = reconstruct(clear_packets)
        assert recovered == annexb_nalus(payload)
        picture = b"".join(b"\0\0\0\1" + nal for nal in recovered)
        decoded = av.CodecContext.create("h264", "r").decode(av.Packet(picture))
        assert len(decoded) == 1 and (decoded[0].width, decoded[0].height) == (320, 240)
        rgb = decoded[0].to_ndarray(format="rgb24").mean(axis=(0, 1))
        assert rgb[0] > 220 and rgb[1] < 30 and rgb[2] < 30
        async with asyncio.timeout(3):
            report = await asyncio.get_running_loop().sock_recv(receiver, 2048)
        parsed, description = RtcpPacket.parse(crypto.unprotect_rtcp(report))
        assert parsed.ssrc == result.ssrc and parsed.sender_info.packet_count == len(clear_packets)
        assert description.chunks[0].ssrc == result.ssrc
        assert description.chunks[0].items == [(1, manager._sessions[identity].timeline.cname)]
        assert controller.opens == 1
    finally:
        await manager.stop(controller.boot, controller.scope, identity)
        await manager.close()
        receiver.close()
    assert controller.handle.closed and manager.pending_resources == 0


@pytest.mark.asyncio
async def test_policy_revocation_fences_before_buffered_or_native_work_can_send():
    manager, controller, identity, _, receiver = await prepared()
    try:
        controller.allowed = False
        await manager.refresh()
        async with asyncio.timeout(1):
            while manager.pending_resources:
                await asyncio.sleep(0.005)
        assert controller.opens == 0
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.get_running_loop().sock_recv(receiver, 2048), 0.03)
    finally:
        receiver.close()
        await manager.close()


@pytest.mark.asyncio
async def test_stop_during_late_start_retains_lease_until_compensating_close():
    manager, controller, identity, _, receiver = await prepared()
    controller.acquire_release = asyncio.Event()
    starting = asyncio.create_task(
        manager.start(controller.boot, controller.scope, identity, CONFIG)
    )
    await controller.acquire_entered.wait()
    stopping = asyncio.create_task(manager.stop(controller.boot, controller.scope, identity))
    await asyncio.sleep(0)
    assert manager.pending_resources == 1 and not stopping.done()
    controller.acquire_release.set()
    try:
        await asyncio.wait_for(stopping, 2)
        await asyncio.gather(starting, return_exceptions=True)
        assert controller.handle.closed and manager.pending_resources == 0
    finally:
        controller.acquire_release.set()
        receiver.close()
        await manager.close()


@pytest.mark.asyncio
async def test_large_idr_is_not_burst_to_escape_selected_bitrate_budget():
    manager, controller, identity, _, receiver = await prepared()
    session = manager._sessions[identity]
    session.config = CONFIG
    session.handle = controller.handle
    output = SimpleNamespace(
        payload=wire_idr(1024 * 1024), timestamp=0, mode="copy", is_keyframe=True
    )
    sequence = session.packetizer.sequence
    try:
        assert not await manager._access_unit(session, output, controller.handle.capture_epoch)
        assert session.packetizer.sequence == sequence and session.packets == 0
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.get_running_loop().sock_recv(receiver, 2048), 0.03)
    finally:
        receiver.close()
        await manager.close()


@pytest.mark.asyncio
async def test_capture_epoch_change_between_packetization_and_udp_write_rejects_old_bytes():
    manager, controller, identity, _, receiver = await prepared()
    session = manager._sessions[identity]
    session.handle = controller.handle
    old = controller.handle.capture_epoch
    controller.handle.buffer.clear()
    try:
        with pytest.raises(_SourceChanged):
            await manager._send(session, b"old encrypted bytes", epoch=old)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.get_running_loop().sock_recv(receiver, 2048), 0.03)
    finally:
        receiver.close()
        await manager.close()


@pytest.mark.asyncio
async def test_packet_pacing_respects_finite_wire_peak_without_clamping_idr_duration():
    manager, controller, identity, _, receiver = await prepared()
    session = manager._sessions[identity]
    session.config, session.handle = CONFIG, controller.handle
    observe_video_clock(manager, session)
    output = SimpleNamespace(payload=wire_idr(10_000), timestamp=0, mode="copy", is_keyframe=True)
    try:
        began = time.monotonic()
        assert await manager._access_unit(session, output, controller.handle.capture_epoch)
        elapsed = time.monotonic() - began
        # The first UDP packet may leave immediately; every later packet waits
        # for its byte budget, carrying debt across access units.
        from halo_server.media.video_budget import VideoWirePolicy

        assert elapsed >= (10_000 - CONFIG.mtu) * 8 / VideoWirePolicy(CONFIG.max_bit_rate).peak_bps
        assert elapsed < 1.0
        assert (
            session.next_packet_at
            > began + 10_000 * 8 / VideoWirePolicy(CONFIG.max_bit_rate).peak_bps
        )
    finally:
        receiver.close()
        await manager.close()


@pytest.mark.asyncio
async def test_capture_retry_during_idr_pacing_stops_remaining_old_attempt_datagrams():
    manager, controller, identity, _, receiver = await prepared()
    session = manager._sessions[identity]
    session.config, session.handle = CONFIG, controller.handle
    observe_video_clock(manager, session)
    output = SimpleNamespace(payload=wire_idr(10_000), timestamp=0, mode="copy", is_keyframe=True)
    sending = asyncio.create_task(
        manager._access_unit(session, output, controller.handle.capture_epoch)
    )
    try:
        await asyncio.wait_for(asyncio.get_running_loop().sock_recv(receiver, 2048), 1)
        controller.handle.buffer.clear()
        with pytest.raises(_SourceChanged):
            await sending
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.get_running_loop().sock_recv(receiver, 2048), 0.08)
        assert session.packets == 1
    finally:
        await asyncio.gather(sending, return_exceptions=True)
        receiver.close()
        await manager.close()


class DeferredAdapter:
    def __init__(self, config):
        self.config = config
        self.configuration_generation = 1
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.release.set()
        self.closed = False
        self.close_entered = self.close_release = None
        self.epochs = []
        self.keyframe_requests = 0

    async def feed(self, payload, timestamp, *, capture_epoch, received_at=None):
        generation = self.configuration_generation
        self.entered.set()
        self.epochs.append(capture_epoch)
        await self.release.wait()
        return (
            SimpleNamespace(
                payload=payload,
                timestamp=timestamp,
                is_keyframe=True,
                mode="copy",
                capture_epoch=capture_epoch,
                configuration_generation=generation,
                received_at=received_at,
            ),
        )

    def request_keyframe(self):
        self.keyframe_requests += 1

    def force_adaptation(self):
        pass

    async def reconfigure(self, config):
        if self.config != config:
            self.configuration_generation += 1
        self.config = config

    async def close(self):
        if self.close_entered is not None:
            self.close_entered.set()
            await self.close_release.wait()
        self.closed = True


@pytest.mark.asyncio
async def test_capture_retry_during_codec_worker_discards_old_result_before_udp_delivery():
    adapter = DeferredAdapter(CONFIG)
    adapter.release.clear()
    manager, controller, identity, result, receiver = await prepared(
        adapter_factory=lambda _: adapter
    )
    controller.handle.buffer.append_video(encoded_picture(0), 0)
    crypto = native_crypto(result.ssrc)
    try:
        await manager.start(controller.boot, controller.scope, identity, CONFIG)
        await asyncio.wait_for(adapter.entered.wait(), 1)
        controller.handle.buffer.clear()
        controller.handle.buffer.append_video(encoded_picture(2), 0)
        adapter.release.set()
        clear = []
        async with asyncio.timeout(2):
            while True:
                packet = crypto.unprotect(
                    await asyncio.get_running_loop().sock_recv(receiver, 2048)
                )
                clear.append(packet)
                if packet[1] & 0x80:
                    break
        payload = b"".join(b"\0\0\0\1" + nal for nal in reconstruct(clear))
        decoded = av.CodecContext.create("h264", "r").decode(av.Packet(payload))[0]
        rgb = decoded.to_ndarray(format="rgb24").mean(axis=(0, 1))
        assert rgb[2] > 220 and rgb[0] < 30
        assert len(set(adapter.epochs)) == 2
    finally:
        adapter.release.set()
        await manager.stop(controller.boot, controller.scope, identity)
        await manager.close()
        receiver.close()


@pytest.mark.asyncio
async def test_cancelled_stop_still_joins_codec_cleanup_and_releases_socket_and_capture():
    adapter = DeferredAdapter(CONFIG)
    adapter.close_entered, adapter.close_release = asyncio.Event(), asyncio.Event()
    manager, controller, identity, _, receiver = await prepared(adapter_factory=lambda _: adapter)
    await manager.start(controller.boot, controller.scope, identity, CONFIG)
    stopping = asyncio.create_task(manager.stop(controller.boot, controller.scope, identity))
    try:
        await asyncio.wait_for(adapter.close_entered.wait(), 1)
        stopping.cancel()
        await asyncio.sleep(0)
        assert not stopping.done() and manager.pending_resources == 1
        adapter.close_release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(stopping, 1)
        assert adapter.closed and controller.handle.closed and manager.pending_resources == 0
    finally:
        adapter.close_release.set()
        await asyncio.gather(stopping, return_exceptions=True)
        receiver.close()
        await manager.close()


@pytest.mark.asyncio
async def test_failed_capture_cleanup_retains_reservation_and_blocks_overlapping_prepare(
    monkeypatch,
):
    manager, controller, identity, _, receiver = await prepared(adapter_factory=DeferredAdapter)
    await manager.start(controller.boot, controller.scope, identity, CONFIG)
    real_close = controller.handle.close

    async def failed():
        raise RuntimeError("Synthetic native cleanup failure")

    monkeypatch.setattr(controller.handle, "close", failed)
    try:
        with pytest.raises(RuntimeError, match="cleanup"):
            await manager.stop(controller.boot, controller.scope, identity)
        assert manager.pending_resources == 1 and not controller.handle.closed
        with pytest.raises(HomeKitOperationError) as raised:
            await manager.prepare(
                controller.boot,
                controller.scope,
                str(uuid4()),
                source_address="127.0.0.1",
                target_address="127.0.0.1",
                address_version="ipv4",
                target_port=receiver.getsockname()[1],
                key=KEY,
                salt=SALT,
            )
        assert raised.value.code == "cleanup_pending"
    finally:
        monkeypatch.setattr(controller.handle, "close", real_close)
        await manager.stop(controller.boot, controller.scope, identity)
        receiver.close()
        await manager.close()
    assert controller.handle.closed and manager.pending_resources == 0


@pytest.mark.asyncio
async def test_reconfigure_preserves_udp_port_ssrc_and_crypto_sequence_state():
    adapter = DeferredAdapter(CONFIG)
    manager, controller, identity, result, receiver = await prepared(
        adapter_factory=lambda _: adapter
    )
    try:
        await manager.start(controller.boot, controller.scope, identity, CONFIG)
        session = manager._sessions[identity]
        original = session.socket, session.secure, session.packetizer.sequence
        changed = replace(CONFIG, max_bit_rate=400)
        await manager.reconfigure(controller.boot, controller.scope, identity, changed)
        assert (session.socket, session.secure, session.packetizer.sequence) == original
        assert session.prepared == result and adapter.config == changed
        assert controller.opens == 1
    finally:
        receiver.close()
        await manager.close()


@pytest.mark.asyncio
async def test_prepared_expiry_emits_one_terminal_notice_and_releases_reservation(monkeypatch):
    monkeypatch.setattr(module, "PREPARE_SECONDS", 0)
    manager, controller, identity, _, receiver = await prepared()
    notices = []

    async def ended(*args):
        notices.append(args)

    manager._on_ended = ended
    try:
        await manager.refresh()
        async with asyncio.timeout(1):
            while manager.pending_resources or not notices:
                await asyncio.sleep(0.005)
        await manager.refresh()
        assert notices == [(controller.boot, controller.scope, identity, "prepare_expired")]
        assert controller.opens == 0
    finally:
        receiver.close()
        await manager.close()


@pytest.mark.asyncio
async def test_authenticated_pli_nack_and_empty_rr_only_affect_their_current_session():
    adapter = DeferredAdapter(CONFIG)
    manager, controller, identity, result, receiver = await prepared(
        adapter_factory=lambda _: adapter
    )
    controller.handle.buffer.append_video(encoded_picture(), 0)
    crypto = native_crypto(result.ssrc)
    peer_crypto = native_crypto(500)
    try:
        await manager.start(controller.boot, controller.scope, identity, CONFIG)
        original = await asyncio.wait_for(asyncio.get_running_loop().sock_recv(receiver, 2048), 1)
        clear = crypto.unprotect(original)
        sequence = struct.unpack_from("!H", clear, 2)[0]
        async with asyncio.timeout(1):
            while not clear[1] & 0x80:
                clear = crypto.unprotect(await asyncio.get_running_loop().sock_recv(receiver, 2048))
        session = manager._sessions[identity]
        feedback_at = session.last_feedback
        plain = bytes(RtcpPsfbPacket(fmt=1, ssrc=500, media_ssrc=result.ssrc))
        receiver.sendto(plain, ("127.0.0.1", result.port))
        await asyncio.sleep(0.02)
        assert adapter.keyframe_requests == 0 and session.last_feedback == feedback_at
        valid = peer_crypto.protect_rtcp(plain)
        receiver.sendto(valid, ("127.0.0.1", result.port))
        async with asyncio.timeout(1):
            while adapter.keyframe_requests == 0:
                await asyncio.sleep(0.005)
        assert adapter.keyframe_requests == 1 and session.last_feedback > feedback_at
        receiver.sendto(valid, ("127.0.0.1", result.port))
        await asyncio.sleep(0.02)
        assert adapter.keyframe_requests == 1
        nack = bytes(RtcpRtpfbPacket(fmt=1, ssrc=500, media_ssrc=result.ssrc, lost=[sequence]))
        receiver.sendto(peer_crypto.protect_rtcp(nack), ("127.0.0.1", result.port))
        retransmit = await asyncio.wait_for(asyncio.get_running_loop().sock_recv(receiver, 2048), 1)
        assert retransmit == original
        # Identical ciphertext is resent, not altered/re-encrypted at a reused SRTP index.
        before_empty = session.last_feedback
        receiver.sendto(
            peer_crypto.protect_rtcp(bytes(RtcpRrPacket(ssrc=500))), ("127.0.0.1", result.port)
        )
        async with asyncio.timeout(1):
            while session.last_feedback <= before_empty:
                await asyncio.sleep(0.005)
        assert manager.pending_resources == 1
    finally:
        receiver.close()
        await manager.close()


@pytest.mark.asyncio
async def test_unknown_stop_prevents_late_prepare_without_allocating_camera_or_udp():
    controller = Controller()
    manager = HomeKitLiveManager(controller)
    identity = str(uuid4())
    try:
        await manager.stop(controller.boot, controller.scope, identity)
        with pytest.raises(HomeKitOperationError) as raised:
            await manager.prepare(
                controller.boot,
                controller.scope,
                identity,
                source_address="127.0.0.1",
                target_address="127.0.0.1",
                address_version="ipv4",
                target_port=9999,
                key=KEY,
                salt=SALT,
            )
        assert raised.value.code == "session_conflict"
        assert manager.pending_resources == 0 and controller.opens == 0
    finally:
        await manager.close()


@pytest.mark.parametrize(
    "target,version",
    [
        ("0.0.0.0", "ipv4"),
        ("224.0.0.1", "ipv4"),
        ("255.255.255.255", "ipv4"),
        ("camera.local", "ipv4"),
        ("127.0.0.1\n", "ipv4"),
        ("::", "ipv6"),
        ("ff02::1", "ipv6"),
        ("127.0.0.1", "ipv6"),
        ("::1", "ipv4"),
        ("fe80::1%nonexistent_halo_interface", "ipv6"),
    ],
)
def test_destination_validation_rejects_nonunicast_hostname_and_wrong_family(target, version):
    with pytest.raises(HomeKitOperationError):
        module._address(target, version)


@pytest.mark.asyncio
async def test_cancelled_reconfiguration_retires_partially_changed_codec_and_source():
    adapter = DeferredAdapter(CONFIG)
    manager, controller, identity, _, receiver = await prepared(adapter_factory=lambda _: adapter)
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked(config):
        adapter.config = config
        entered.set()
        await release.wait()

    adapter.reconfigure = blocked
    await manager.start(controller.boot, controller.scope, identity, CONFIG)
    changing = asyncio.create_task(
        manager.reconfigure(
            controller.boot, controller.scope, identity, replace(CONFIG, max_bit_rate=400)
        )
    )
    try:
        await entered.wait()
        changing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(changing, 1)
        assert adapter.closed and controller.handle.closed and manager.pending_resources == 0
    finally:
        release.set()
        await asyncio.gather(changing, return_exceptions=True)
        receiver.close()
        await manager.close()


def test_annexb_marker_flood_rejects_before_allocating_a_match_per_input_marker():
    payload = b"\0\0\1\x65" * (512 * 1024)
    tracemalloc.start()
    try:
        with pytest.raises(RtpError):
            annexb_nalus(payload)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 256 * 1024


@pytest.mark.asyncio
async def test_stop_of_owned_session_succeeds_when_unknown_stop_tombstones_are_full(monkeypatch):
    manager, controller, identity, _, receiver = await prepared()
    monkeypatch.setattr(module, "MAX_STOP_TOMBSTONES", 1)
    try:
        await manager.stop(controller.boot, controller.scope, str(uuid4()))
        await manager.stop(controller.boot, controller.scope, identity)
        assert manager.pending_resources == 0
        assert len(manager._stopped) == 2
        with pytest.raises(HomeKitOperationError) as raised:
            await manager.prepare(
                controller.boot,
                controller.scope,
                str(uuid4()),
                source_address="127.0.0.1",
                target_address="127.0.0.1",
                address_version="ipv4",
                target_port=receiver.getsockname()[1],
                key=KEY,
                salt=SALT,
            )
        assert raised.value.code == "busy"
    finally:
        receiver.close()
        await manager.close()


@pytest.mark.asyncio
async def test_blocked_codec_cleanup_is_bounded_and_retains_claim_until_actual_completion(
    monkeypatch,
):
    monkeypatch.setattr(module, "CLEANUP_SECONDS", 0.03)
    adapter = DeferredAdapter(CONFIG)
    adapter.close_entered, adapter.close_release = asyncio.Event(), asyncio.Event()
    manager, controller, identity, _, receiver = await prepared(adapter_factory=lambda _: adapter)
    await manager.start(controller.boot, controller.scope, identity, CONFIG)
    session = manager._sessions[identity]
    try:
        with pytest.raises(HomeKitOperationError) as raised:
            await asyncio.wait_for(manager.stop(controller.boot, controller.scope, identity), 0.5)
        assert raised.value.code == "cleanup_pending"
        assert session.socket.fileno() == -1 and not session.cleanup.done()
        assert manager.pending_resources == 1 and manager.cleanup_failures == 1
        assert not controller.handle.closed
        adapter.close_release.set()
        await asyncio.wait_for(asyncio.shield(session.cleanup), 1)
        assert adapter.closed and controller.handle.closed
        assert manager.pending_resources == manager.cleanup_failures == 0
    finally:
        adapter.close_release.set()
        receiver.close()
        await manager.close()


@pytest.mark.asyncio
async def test_retransmissions_share_the_bounded_local_wire_peak():
    manager, controller, identity, _, receiver = await prepared()
    session = manager._sessions[identity]
    session.config, session.handle = CONFIG, controller.handle
    try:
        packet = b"x" * 1000
        began = time.monotonic()
        for _ in range(5):
            await manager._paced_send(session, packet, epoch=controller.handle.capture_epoch)
        from halo_server.media.video_budget import VideoWirePolicy

        assert (
            time.monotonic() - began
            >= 4 * len(packet) * 8 / VideoWirePolicy(CONFIG.max_bit_rate).peak_bps
        )
    finally:
        receiver.close()
        await manager.close()
