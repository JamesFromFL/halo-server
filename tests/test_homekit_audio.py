"""Negotiated native UDP audio and generation-gated synthetic speaker tests."""

import asyncio
import socket
import struct
import time
from dataclasses import replace
from datetime import UTC, datetime
from fractions import Fraction
from functools import lru_cache
from types import SimpleNamespace
from uuid import uuid4

import av
import numpy as np
import pylibsrtp
import pytest
from aiortc.rtcrtpreceiver import RTCRtpReceiver
from aiortc.rtp import RtcpPacket, RtcpRrPacket, RtcpSenderInfo, RtcpSrPacket, RtpPacket
from test_homekit_live import (
    CONFIG,
    KEY,
    SALT,
    Controller,
    DeferredAdapter,
    Handle,
    encoded_picture,
)

from halo_server.homekit.models import HomeKitOperationError
from halo_server.media import homekit_audio as audio_module
from halo_server.media.buffer import BufferRead
from halo_server.media.homekit_audio import AudioEndpointRequest
from halo_server.media.homekit_live import HomeKitLiveManager
from halo_server.media.live_timeline import LiveTimeline
from halo_server.media.opus import AudioConfiguration, AudioOutput, parse_opus
from halo_server.media.ring_media import install_source_clock_observer
from halo_server.media.srtp import SecureVideo, SrtpError
from halo_server.media.talkback import SpeechJitter, TalkPacket

AUDIO_KEY, AUDIO_SALT = b"A" * 16, b"S" * 14
AUDIO = AudioConfiguration(24000, 20, "cbr", 24, rtcp_interval=0.1)


def crypto(ssrc):
    return pylibsrtp.Session(
        pylibsrtp.Policy(
            key=AUDIO_KEY + AUDIO_SALT, ssrc_type=pylibsrtp.Policy.SSRC_SPECIFIC, ssrc_value=ssrc
        )
    )


@lru_cache
def tone_packets():
    encoder = av.CodecContext.create("libopus", "w")
    encoder.sample_rate, encoder.layout, encoder.format = 24000, "mono", "s16"
    encoder.bit_rate, encoder.time_base = 24000, Fraction(1, 24000)
    encoder.options = {"application": "audio", "frame_duration": "20", "vbr": "off", "fec": "off"}
    encoder.open()
    result = []
    for index in range(12):
        t = (np.arange(480) + 480 * index) / 24000
        samples = (np.sin(2 * np.pi * 440 * t) * 12000).astype(np.int16).reshape(1, -1)
        frame = av.AudioFrame.from_ndarray(samples, format="s16", layout="mono")
        frame.sample_rate, frame.pts, frame.time_base = 24000, 480 * index, Fraction(1, 24000)
        result.extend(bytes(packet) for packet in encoder.encode(frame))
    assert all(parse_opus(payload).duration_samples == 960 for payload in result)
    return tuple(result)


def opus_decoder():
    decoder = av.CodecContext.create("opus", "r")
    decoder.extradata = b"OpusHead\x01\x01\x00\x00" + struct.pack("<IhB", 48000, 0, 0)
    return decoder


class AudioHandle(Handle):
    def __init__(self):
        super().__init__()
        self.audio = asyncio.Queue()
        self.audio_read_started = asyncio.Event()
        self.source_clocks = ()

    async def read_audio(self, *, timeout):
        try:
            packets = await asyncio.wait_for(self.audio.get(), timeout)
        except TimeoutError:
            packets = ()
        if packets:
            self.audio_read_started.set()
        return BufferRead(packets, False, self.capture_epoch)

    def append(self, payloads, *, epoch=None):
        epoch = self.capture_epoch if epoch is None else epoch
        self.audio.put_nowait(
            tuple(
                SimpleNamespace(
                    payload=payload,
                    rtp_timestamp=index * 960,
                    received_at=time.monotonic(),
                    capture_epoch=epoch,
                )
                for index, payload in enumerate(payloads)
            )
        )


async def feed_continuous_audio(handle, payloads, *, timestamp=960):
    """Finite live source after the first read, with real receipt-time spacing."""
    for payload in payloads:
        duration = parse_opus(payload).duration_samples
        await asyncio.sleep(duration / 48000)
        handle.audio.put_nowait(
            (
                SimpleNamespace(
                    payload=payload,
                    rtp_timestamp=timestamp,
                    received_at=time.monotonic(),
                    capture_epoch=handle.capture_epoch,
                ),
            )
        )
        timestamp += duration


class Speaker:
    def __init__(self):
        self.frames = []
        self.closed = False
        self.primed = self.ready = False
        self.ready_entered = asyncio.Event()
        self.ready_release = None
        self.close_entered = asyncio.Event()
        self.close_release = None

    async def wait_ready(self, timeout):
        self.ready_entered.set()
        if not self.primed:
            return False
        if self.ready_release is not None:
            await self.ready_release.wait()
        self.ready = not self.closed
        return self.ready

    async def write(self, frame):
        assert not self.closed and len(frame) == 1920
        if not self.ready:
            assert frame == bytes(1920)
            self.primed = True
            return
        self.frames.append(frame)

    async def renew(self):
        return not self.closed

    async def close(self):
        self.close_entered.set()
        if self.close_release is not None:
            await self.close_release.wait()
        self.closed = True


class AudioController(Controller):
    def __init__(self):
        super().__init__()
        self.handle = AudioHandle()
        self.permissions = {"streaming", "live_audio", "talkback"}
        self.speaker = Speaker()
        self.speaker_opens = 0
        self.speaker_id = None
        self.speaker_entered = asyncio.Event()
        self.speaker_release = None
        self.speaker_busy = False

    def authorize(self, boot, scope, permission):
        if not self.allowed or (boot, scope) != (self.boot, self.scope):
            raise HomeKitOperationError("scope_changed")
        if permission not in self.permissions:
            raise HomeKitOperationError("policy_denied")

    async def open_speaker(self, boot, scope, *, session_id):
        self.authorize(boot, scope, "talkback")
        self.speaker_opens += 1
        self.speaker_id = session_id
        self.speaker_entered.set()
        if self.speaker_busy:
            raise HomeKitOperationError("intercom_busy")
        if self.speaker_release is not None:
            try:
                await self.speaker_release.wait()
            except asyncio.CancelledError:
                await self.speaker_release.wait()
        return self.speaker


class CopyAudio:
    def __init__(self, config):
        self.config, self.closed = config, False
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.release.set()

    async def feed(self, payload, timestamp, *, capture_epoch):
        self.entered.set()
        await self.release.wait()
        return (AudioOutput(payload, timestamp, parse_opus(payload).duration_samples, "copy"),)

    async def close(self):
        self.closed = True


async def prepared_audio(*, controller=None, audio_factory=None, decoder_factory=None):
    controller = controller or AudioController()
    manager = HomeKitLiveManager(
        controller,
        adapter_factory=DeferredAdapter,
        audio_adapter_factory=audio_factory,
        audio_decoder_factory=decoder_factory,
    )
    video, audio = (socket.socket(socket.AF_INET, socket.SOCK_DGRAM) for _ in range(2))
    for sock in (video, audio):
        sock.bind(("127.0.0.1", 0))
        sock.setblocking(False)
    identity = str(uuid4())
    result = await manager.prepare(
        controller.boot,
        controller.scope,
        identity,
        source_address="127.0.0.1",
        target_address="127.0.0.1",
        address_version="ipv4",
        target_port=video.getsockname()[1],
        key=KEY,
        salt=SALT,
        audio=AudioEndpointRequest(audio.getsockname()[1], AUDIO_KEY, AUDIO_SALT),
    )
    return manager, controller, identity, result, video, audio


async def receive_media(sock, peer, count):
    packets = []
    async with asyncio.timeout(3):
        while len(packets) < count:
            protected = await asyncio.get_running_loop().sock_recv(sock, 4096)
            if 192 <= protected[1] <= 223:
                continue
            packets.append(RtpPacket.parse(peer.unprotect(protected)))
    return packets


async def until(predicate, timeout=3):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


@pytest.mark.asyncio
async def test_atomic_prepare_retains_two_real_ports_and_never_wakes_capture_or_speaker():
    manager, controller, identity, result, video, audio = await prepared_audio()
    try:
        assert result.port != result.audio.port and result.ssrc != result.audio.ssrc
        assert controller.opens == controller.speaker_opens == 0
        assert manager.pending_resources == 1
        assert b"AAAAAAAA".decode() not in repr(result)
        session = manager._sessions[identity]
        ports = session.socket.getsockname()[1], session.audio.socket.getsockname()[1]
        assert ports == (result.port, result.audio.port)
        await manager.stop(controller.boot, controller.scope, identity)
        assert session.socket.fileno() == session.audio.socket.fileno() == -1
        assert manager.pending_resources == 0
    finally:
        video.close()
        audio.close()
        await manager.close()


def test_inbound_srtp_tamper_does_not_pin_sender_and_replay_or_second_sender_is_rejected():
    secure = SecureVideo(AUDIO_KEY, AUDIO_SALT, 900)
    peer = crypto(500)
    plain = struct.pack("!BBHII", 0x80, 110, 65535, 0xFFFFFF00, 500) + tone_packets()[0]
    protected = peer.protect(plain)
    try:
        with pytest.raises(SrtpError):
            secure.unprotect(protected[:-1] + bytes((protected[-1] ^ 1,)))
        assert secure.unprotect(protected) == plain
        with pytest.raises(SrtpError):
            secure.unprotect(protected)
        other = crypto(501).protect(plain[:8] + struct.pack("!I", 501) + plain[12:])
        with pytest.raises(SrtpError):
            secure.unprotect(other)
        next_plain = struct.pack("!BBHII", 0x80, 110, 0, 224, 500) + tone_packets()[1]
        assert secure.unprotect(peer.protect(next_plain)) == next_plain
        assert secure.unprotect_rtcp(
            crypto(600).protect_rtcp(bytes(RtcpRrPacket(ssrc=600)))
        ) == bytes(RtcpRrPacket(ssrc=600))
    finally:
        secure.close()


@pytest.mark.asyncio
async def test_native_audio_udp_decrypts_decodes_tone_with_actual_classic_hap_clock_and_sr():
    manager, controller, identity, result, video, audio = await prepared_audio()
    controller.handle.append(tone_packets()[:1])
    peer = crypto(result.audio.ssrc)
    feeder = None
    try:
        await manager.start(controller.boot, controller.scope, identity, CONFIG, audio_config=AUDIO)
        # Start-edge selection has its own regressions. This finite source
        # continues supplying every tone packet after the initial live read.
        await asyncio.wait_for(controller.handle.audio_read_started.wait(), 1)
        feeder = asyncio.create_task(feed_continuous_audio(controller.handle, tone_packets()[1:8]))
        packets = await receive_media(audio, peer, 8)
        await feeder
        assert all(
            (b.timestamp - a.timestamp) & 0xFFFFFFFF == 480
            for a, b in zip(packets, packets[1:], strict=False)
        )
        assert all(
            (b.sequence_number - a.sequence_number) & 65535 == 1
            for a, b in zip(packets, packets[1:], strict=False)
        )
        assert all(
            packet.payload_type == 110 and packet.ssrc == result.audio.ssrc for packet in packets
        )
        decoder = opus_decoder()
        frames = [
            frame for packet in packets for frame in decoder.decode(av.Packet(packet.payload))
        ]
        samples = np.concatenate([frame.to_ndarray().reshape(-1) for frame in frames])[960:]
        frequency = np.fft.rfftfreq(samples.size, 1 / 48000)[np.argmax(abs(np.fft.rfft(samples)))]
        assert 420 < frequency < 460 and np.max(abs(samples)) > 0.1
        assert controller.opens == 1 and controller.speaker_opens == 0
        async with asyncio.timeout(1):
            while True:
                protected = await asyncio.get_running_loop().sock_recv(audio, 4096)
                if 192 <= protected[1] <= 223:
                    report = RtcpPacket.parse(peer.unprotect_rtcp(protected))[0]
                    assert report.sender_info.packet_count == 8
                    break
    finally:
        if feeder is not None:
            feeder.cancel()
            await asyncio.gather(feeder, return_exceptions=True)
        video.close()
        audio.close()
        await manager.close()


@pytest.mark.asyncio
async def test_source_epoch_change_during_audio_worker_emits_only_new_attempt_packets():
    adapter = CopyAudio(AUDIO)
    adapter.release.clear()
    manager, controller, identity, result, video, audio = await prepared_audio(
        audio_factory=lambda _: adapter
    )
    controller.handle.append(tone_packets()[:1])
    try:
        await manager.start(controller.boot, controller.scope, identity, CONFIG, audio_config=AUDIO)
        await adapter.entered.wait()
        controller.handle.buffer.clear()
        adapter.release.set()
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.get_running_loop().sock_recv(audio, 4096), 0.08)
        controller.handle.append(tone_packets()[1:2])
        packets = await receive_media(audio, crypto(result.audio.ssrc), 1)
        assert packets[0].payload == tone_packets()[1]
    finally:
        adapter.release.set()
        video.close()
        audio.close()
        await manager.close()


def test_speech_jitter_reorders_wrap_and_ignores_duplicates_and_large_jumps():
    jitter = SpeechJitter()

    def packet(sequence):
        return TalkPacket(b"x", sequence, sequence, 0, 1)

    jitter.put(packet(65535))
    jitter.put(packet(1))
    jitter.put(packet(0))
    assert jitter.pop(0.02) is None
    assert [jitter.pop(0.05).sequence for _ in range(3)] == [65535, 0, 1]
    jitter.put(packet(0))
    jitter.put(packet(4000))
    assert jitter.pop(1) is None


@pytest.mark.asyncio
async def test_only_authenticated_valid_selected_audio_starts_speaker_and_native_pcm_is_tone():
    manager, controller, identity, result, video, audio = await prepared_audio()
    peer = crypto(500)
    try:
        await manager.start(controller.boot, controller.scope, identity, CONFIG, audio_config=AUDIO)
        destination = ("127.0.0.1", result.audio.port)
        audio.sendto(b"not authenticated", destination)
        audio.sendto(peer.protect_rtcp(bytes(RtcpRrPacket(ssrc=500))), destination)
        wrong = struct.pack("!BBHII", 0x80, 111, 0, 0, 500) + tone_packets()[0]
        audio.sendto(peer.protect(wrong), destination)
        malformed = struct.pack("!BBHII", 0x80, 110, 1, 480, 500) + b"\x03"
        audio.sendto(peer.protect(malformed), destination)
        await asyncio.sleep(0.07)
        assert controller.speaker_opens == 0
        for index, payload in enumerate(tone_packets()[:6], start=2):
            plain = struct.pack("!BBHII", 0x80, 110, index, index * 480, 500) + payload
            audio.sendto(peer.protect(plain), destination)
            await asyncio.sleep(0.02)
        await until(lambda: len(controller.speaker.frames) == 6)
        assert controller.speaker_opens == 1 and controller.speaker_id != identity
        assert controller.speaker.primed and controller.speaker.ready
        samples = np.frombuffer(b"".join(controller.speaker.frames), dtype="<i2")
        assert max(abs(samples)) > 1000
        frequency = np.fft.rfftfreq(samples.size, 1 / 48000)[np.argmax(abs(np.fft.rfft(samples)))]
        assert 420 < frequency < 460
        assert samples.size == 6 * 960
        await manager.stop(controller.boot, controller.scope, identity)
        assert controller.speaker.closed and controller.handle.closed
    finally:
        video.close()
        audio.close()
        await manager.close()


@pytest.mark.asyncio
async def test_speaker_busy_keeps_live_view_and_does_not_retry_unbounded_activation():
    controller = AudioController()
    controller.speaker_busy = True
    manager, controller, identity, result, video, audio = await prepared_audio(
        controller=controller
    )
    try:
        await manager.start(controller.boot, controller.scope, identity, CONFIG, audio_config=AUDIO)
        peer = crypto(500)
        for index in range(3):
            plain = struct.pack("!BBHII", 0x80, 110, index, index * 480, 500) + tone_packets()[0]
            audio.sendto(peer.protect(plain), ("127.0.0.1", result.audio.port))
        await until(lambda: manager._sessions[identity].audio.talkback.failure is not None)
        assert controller.speaker_opens == 1 and manager.pending_resources == 1
        assert manager._sessions[identity].audio.talkback.failure == "intercom_busy"
    finally:
        video.close()
        audio.close()
        await manager.close()


@pytest.mark.asyncio
async def test_stop_waits_for_late_speaker_acquisition_then_closes_it_without_any_pcm():
    controller = AudioController()
    controller.speaker_release = asyncio.Event()
    manager, controller, identity, result, video, audio = await prepared_audio(
        controller=controller
    )
    stopping = None
    try:
        await manager.start(controller.boot, controller.scope, identity, CONFIG, audio_config=AUDIO)
        peer = crypto(500)
        plain = struct.pack("!BBHII", 0x80, 110, 0, 0, 500) + tone_packets()[0]
        audio.sendto(peer.protect(plain), ("127.0.0.1", result.audio.port))
        await controller.speaker_entered.wait()
        stopping = asyncio.create_task(manager.stop(controller.boot, controller.scope, identity))
        await asyncio.sleep(0.03)
        assert not stopping.done() and not controller.speaker.frames
        controller.speaker_release.set()
        await asyncio.wait_for(stopping, 1)
        assert controller.speaker.closed and controller.handle.closed
        assert manager.pending_resources == 0
    finally:
        controller.speaker_release.set()
        if stopping is not None:
            await asyncio.gather(stopping, return_exceptions=True)
        video.close()
        audio.close()
        await manager.close()


@pytest.mark.asyncio
async def test_microphone_and_speaker_policy_are_independent():
    controller = AudioController()
    controller.permissions.remove("live_audio")
    manager, controller, identity, result, video, audio = await prepared_audio(
        controller=controller
    )
    controller.handle.append(tone_packets()[:2])
    try:
        await manager.start(controller.boot, controller.scope, identity, CONFIG, audio_config=AUDIO)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.get_running_loop().sock_recv(audio, 4096), 0.07)
        peer = crypto(500)
        plain = struct.pack("!BBHII", 0x80, 110, 0, 0, 500) + tone_packets()[0]
        audio.sendto(peer.protect(plain), ("127.0.0.1", result.audio.port))
        await until(lambda: bool(controller.speaker.frames))
        assert manager.pending_resources == 1
    finally:
        video.close()
        audio.close()
        await manager.close()


@pytest.mark.asyncio
async def test_common_sender_report_anchor_aligns_audio_video_without_resetting_rtp_origins():
    manager, controller, identity, _, video, audio = await prepared_audio()
    session = manager._sessions[identity]
    session.handle = controller.handle
    now = time.monotonic()
    manager._clock = lambda: now + 1
    ntp = 2_300_000_000 << 32
    controller.handle.source_clocks = (
        SimpleNamespace(
            kind="video",
            ssrc=11,
            rtp_timestamp=90000,
            sender_ntp=ntp,
            received_at=now,
            capture_epoch=1,
        ),
        SimpleNamespace(
            kind="audio",
            ssrc=12,
            rtp_timestamp=48000,
            sender_ntp=ntp + (1 << 31),
            received_at=now + 0.03,
            capture_epoch=1,
        ),
    )
    session.timeline = LiveTimeline(
        audio_rate=AUDIO.sample_rate,
        video_origin=session.output_base,
        audio_origin=session.audio.rtp_origin,
        clock=manager._clock,
    )
    for kind, pts, receipt in (("video", 135000, now + 0.5), ("audio", 48000, now + 0.53)):
        session.timeline.observe_input(
            kind,
            capture_epoch=1,
            source_pts=pts,
            received_at=receipt,
            source_clocks=controller.handle.source_clocks,
        )
    try:
        # The same source instant is video+0.5s, audio+0s; receive times differ.
        v = manager._presentation_time(session, "video", 135000, now + 0.5)
        a = manager._presentation_time(session, "audio", 48000, now + 0.53)
        assert abs(v - a) < 1e-6 and session.timing_mode == "source_sender_report"
        base = session.output_base
        controller.handle.buffer.clear()
        session.timeline.observe_input(
            "audio",
            capture_epoch=controller.handle.capture_epoch,
            source_pts=48000,
            received_at=now,
        )
        assert manager._presentation_time(session, "audio", 48000, now) == now
        assert session.timing_mode == "receiver_estimated" and session.output_base == base
    finally:
        video.close()
        audio.close()
        await manager.close()


@pytest.mark.asyncio
async def test_second_socket_bind_failure_closes_both_partial_endpoints(monkeypatch):
    manager = HomeKitLiveManager(AudioController())
    controller = manager.controller
    created = []
    real_socket = socket.socket

    class TrackedSocket(real_socket):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created.append(self)

        def bind(self, endpoint):
            if len(created) == 2:
                raise OSError("Synthetic second-track bind failure")
            return super().bind(endpoint)

    monkeypatch.setattr(socket, "socket", TrackedSocket)
    try:
        with pytest.raises(OSError, match="second-track"):
            await manager.prepare(
                controller.boot,
                controller.scope,
                str(uuid4()),
                source_address="127.0.0.1",
                target_address="127.0.0.1",
                address_version="ipv4",
                target_port=9998,
                key=KEY,
                salt=SALT,
                audio=AudioEndpointRequest(9999, AUDIO_KEY, AUDIO_SALT),
            )
        assert len(created) == 2 and all(sock.fileno() == -1 for sock in created)
        assert manager.pending_resources == controller.opens == 0
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_audio_adapter_initialization_failure_compensates_entire_start():
    def unavailable(_config):
        raise RuntimeError("Synthetic audio converter failure")

    manager, controller, identity, _, video, audio = await prepared_audio(audio_factory=unavailable)
    session = manager._sessions[identity]
    try:
        with pytest.raises(RuntimeError, match="converter"):
            await manager.start(
                controller.boot, controller.scope, identity, CONFIG, audio_config=AUDIO
            )
        assert session.socket.fileno() == session.audio.socket.fileno() == -1
        assert session.adapter.closed and controller.handle.closed
        assert manager.pending_resources == manager.cleanup_failures == 0
    finally:
        video.close()
        audio.close()
        await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("retirement", ["capture_retry", "stop"])
async def test_audio_decode_result_cannot_write_pcm_after_epoch_or_stop_fence(retirement):
    entered, release = asyncio.Event(), asyncio.Event()
    decoders = []

    class DelayedDecoder:
        def __init__(self, **_kwargs):
            self.closed = False
            decoders.append(self)

        async def feed(self, _payload, *, capture_epoch):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            return (b"x" * 1920,)

        async def close(self):
            self.closed = True

    manager, controller, identity, result, video, audio = await prepared_audio(
        decoder_factory=DelayedDecoder
    )
    stopping = None
    try:
        await manager.start(controller.boot, controller.scope, identity, CONFIG, audio_config=AUDIO)
        plain = struct.pack("!BBHII", 0x80, 110, 0, 0, 500) + tone_packets()[0]
        audio.sendto(crypto(500).protect(plain), ("127.0.0.1", result.audio.port))
        await entered.wait()
        if retirement == "capture_retry":
            controller.handle.buffer.clear()
        else:
            stopping = asyncio.create_task(
                manager.stop(controller.boot, controller.scope, identity)
            )
            await asyncio.sleep(0.02)
            assert not stopping.done()
        release.set()
        await until(lambda: bool(decoders) and decoders[0].closed)
        assert controller.speaker_opens == 0 or controller.speaker.closed
        assert controller.speaker.frames == []
        if stopping is not None:
            await stopping
            assert manager.pending_resources == 0
    finally:
        release.set()
        if stopping is not None:
            await asyncio.gather(stopping, return_exceptions=True)
        video.close()
        audio.close()
        await manager.close()


@pytest.mark.asyncio
async def test_failed_speaker_cleanup_retains_entire_av_reservation_and_health(monkeypatch):
    manager, controller, identity, result, video, audio = await prepared_audio()
    original_close = controller.speaker.close

    async def failed():
        raise RuntimeError("Synthetic speaker cleanup failure")

    try:
        await manager.start(controller.boot, controller.scope, identity, CONFIG, audio_config=AUDIO)
        plain = struct.pack("!BBHII", 0x80, 110, 0, 0, 500) + tone_packets()[0]
        audio.sendto(crypto(500).protect(plain), ("127.0.0.1", result.audio.port))
        await until(lambda: bool(controller.speaker.frames))
        monkeypatch.setattr(controller.speaker, "close", failed)
        with pytest.raises(RuntimeError, match="cleanup"):
            await manager.stop(controller.boot, controller.scope, identity)
        assert manager.pending_resources == manager.cleanup_failures == 1
        assert not controller.handle.closed and not controller.speaker.closed
        monkeypatch.setattr(controller.speaker, "close", original_close)
        await manager.stop(controller.boot, controller.scope, identity)
        assert manager.pending_resources == 0 and controller.speaker.closed
    finally:
        monkeypatch.setattr(controller.speaker, "close", original_close)
        video.close()
        audio.close()
        await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("rate", [8000, 16000, 24000])
@pytest.mark.parametrize("duration", [20, 30, 40, 60])
async def test_negotiated_native_udp_packet_duration_and_classic_clock_matrix(rate, duration):
    manager, controller, identity, result, video, audio = await prepared_audio()
    selected = AudioConfiguration(rate, duration, "vbr", 24)
    controller.handle.append(tone_packets()[:1])
    feeder = None
    try:
        await manager.start(
            controller.boot, controller.scope, identity, CONFIG, audio_config=selected
        )
        await asyncio.wait_for(controller.handle.audio_read_started.wait(), 1)
        feeder = asyncio.create_task(feed_continuous_audio(controller.handle, tone_packets()[1:]))
        packets = await receive_media(audio, crypto(result.audio.ssrc), 2)
        assert (packets[1].timestamp - packets[0].timestamp) & 0xFFFFFFFF == rate * duration // 1000
        assert all(
            parse_opus(packet.payload).duration_samples == duration * 48 for packet in packets
        )
        assert all(parse_opus(packet.payload).bandwidth_rate <= rate for packet in packets)
        decoder = opus_decoder()
        decoded = [
            frame for packet in packets for frame in decoder.decode(av.Packet(packet.payload))
        ]
        assert sum(frame.samples for frame in decoded) == 2 * duration * 48
    finally:
        if feeder is not None:
            feeder.cancel()
            await asyncio.gather(feeder, return_exceptions=True)
        video.close()
        audio.close()
        await manager.close()


@pytest.mark.asyncio
async def test_failed_native_decode_cannot_wake_the_ring_speaker():
    closed = []

    class RejectedDecoder:
        def __init__(self, **_kwargs):
            pass

        async def feed(self, _payload, *, capture_epoch):
            raise RuntimeError("Synthetic invalid native audio")

        async def close(self):
            closed.append(True)

    manager, controller, identity, result, video, audio = await prepared_audio(
        decoder_factory=RejectedDecoder
    )
    try:
        await manager.start(controller.boot, controller.scope, identity, CONFIG, audio_config=AUDIO)
        plain = struct.pack("!BBHII", 0x80, 110, 0, 0, 500) + tone_packets()[0]
        audio.sendto(crypto(500).protect(plain), ("127.0.0.1", result.audio.port))
        await until(lambda: bool(closed))
        assert controller.speaker_opens == 0 and manager.pending_resources == 1
    finally:
        video.close()
        audio.close()
        await manager.close()


@pytest.mark.asyncio
async def test_actual_source_sender_reports_align_native_impulse_and_picture_presentation():
    manager, controller, identity, _, video, audio = await prepared_audio()
    session = manager._sessions[identity]
    session.handle = controller.handle
    observations = []
    ntp = 3_900_000_000 << 32
    for kind, rate, origin, ssrc in (("video", 90000, 1000000, 10), ("audio", 48000, 2000000, 11)):
        receiver = RTCRtpReceiver(kind, SimpleNamespace(state="new", _stats_id="offline"))
        observe = install_source_clock_observer(receiver, kind, observations.append)
        assert receiver._RTCRtpReceiver__timestamp_mapper.map(origin) == 0
        receiver._RTCRtpReceiver__remote_streams[ssrc] = object()
        receiver._RTCRtpReceiver__active_ssrc[ssrc] = datetime.now(UTC)
        await receiver._handle_rtcp_packet(
            RtcpSrPacket(ssrc, RtcpSenderInfo(ntp, origin + rate, 1, 100))
        )
        observe(rate // 2)
    controller.handle.source_clocks = tuple(replace(item, capture_epoch=1) for item in observations)
    encoder = av.CodecContext.create("libopus", "w")
    encoder.sample_rate, encoder.layout, encoder.format = 24000, "mono", "s16"
    encoder.bit_rate = 24000
    encoder.options = {"application": "lowdelay", "frame_duration": "20", "vbr": "off"}
    encoder.open()
    samples = np.zeros((1, 480), dtype=np.int16)
    samples[0, 240] = 20000
    impulse = av.AudioFrame.from_ndarray(samples, format="s16", layout="mono")
    impulse.sample_rate = 24000
    encoded = encoder.encode(impulse)
    decoded = opus_decoder().decode(av.Packet(bytes(encoded[0])))[0].to_ndarray().reshape(-1)
    peak_seconds = int(np.argmax(abs(decoded))) / 48000
    picture = av.CodecContext.create("h264", "r").decode(av.Packet(encoded_picture(2)))[0]
    assert picture.to_ndarray(format="rgb24")[120, 160, 2] > 240
    try:
        received_at = time.monotonic() - 0.5
        session.timeline = LiveTimeline(
            audio_rate=AUDIO.sample_rate,
            video_origin=session.output_base,
            audio_origin=session.audio.rtp_origin,
            clock=manager._clock,
        )
        for kind, pts in (("video", 45000), ("audio", 24000)):
            session.timeline.observe_input(
                kind,
                capture_epoch=1,
                source_pts=pts,
                received_at=received_at,
                source_clocks=controller.handle.source_clocks,
            )
        picture_at = manager._presentation_time(session, "video", 45000, received_at)
        impulse_at = manager._presentation_time(session, "audio", 24000, received_at) + peak_seconds
        assert 0 < impulse_at - picture_at < 0.02
        assert session.timing_mode == "source_sender_report"
    finally:
        video.close()
        audio.close()
        await manager.close()


@pytest.mark.asyncio
async def test_muted_audio_without_audio_rr_stays_alive_when_video_receives_authenticated_rr(
    monkeypatch,
):
    monkeypatch.setattr(audio_module, "RECEIVER_SECONDS", 0.05)
    controller = AudioController()
    controller.permissions.remove("live_audio")
    manager, controller, identity, result, video, audio = await prepared_audio(
        controller=controller
    )
    peer = pylibsrtp.Session(
        pylibsrtp.Policy(key=KEY + SALT, ssrc_type=pylibsrtp.Policy.SSRC_SPECIFIC, ssrc_value=700)
    )
    try:
        await manager.start(controller.boot, controller.scope, identity, CONFIG, audio_config=AUDIO)
        for _ in range(12):
            video.sendto(
                peer.protect_rtcp(bytes(RtcpRrPacket(ssrc=700))), ("127.0.0.1", result.port)
            )
            await asyncio.sleep(0.02)
        assert manager.pending_resources == 1
        assert not manager._sessions[identity].retired and controller.speaker_opens == 0
    finally:
        video.close()
        audio.close()
        await manager.close()


@pytest.mark.asyncio
async def test_expired_audio_is_rejected_after_udp_retry_without_new_enqueue():
    manager, controller, identity, _, video, audio = await prepared_audio()
    session = manager._sessions[identity]
    session.handle = controller.handle
    track = session.audio
    now = [time.monotonic()]
    track._clock = lambda: now[0]
    original = track.socket

    class Blocked:
        calls = 0

        def send(self, _packet):
            self.calls += 1
            now[0] += 0.01
            raise BlockingIOError

    blocked = Blocked()
    track.socket = blocked
    try:
        assert not await track._send(
            b"bounded encrypted speech",
            permission="live_audio",
            epoch=controller.handle.capture_epoch,
            fresh_until=now[0] + 0.005,
        )
        assert blocked.calls == 1
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.get_running_loop().sock_recv(audio, 4096), 0.02)
    finally:
        track.socket = original
        video.close()
        audio.close()
        await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["receipt_gap", "conversion_crosses_oldest_deadline"])
async def test_grouped_audio_uses_earliest_receipt_and_reuses_fresh_batch_after_reset(
    scenario,
):
    from halo_server.media.homekit_audio import MAX_AUDIO_AGE
    from halo_server.media.opus import repacketize

    instances = []
    now = [time.monotonic()]
    began = now[0]
    first_feed = asyncio.Event()

    class Grouped:
        def __init__(self, config):
            self.closed = False
            self.pending = []
            self.groups = 0
            self.inputs = []
            instances.append(self)

        async def feed(self, payload, timestamp, *, capture_epoch):
            self.inputs.append(timestamp)
            self.pending.append((payload, timestamp))
            first_feed.set()
            if len(self.pending) < 3:
                return ()
            self.groups += 1
            group, self.pending = self.pending, []
            if scenario == "conversion_crosses_oldest_deadline" and self.groups == 1:
                now[0] = began + MAX_AUDIO_AGE + 0.01
            return (
                AudioOutput(
                    repacketize(item[0] for item in group), group[0][1] - 120, 2880, "adapt"
                ),
            )

        async def close(self):
            self.closed = True
            self.pending.clear()

    manager, controller, identity, result, video, audio = await prepared_audio(
        audio_factory=Grouped
    )
    selected = AudioConfiguration(24000, 60, "vbr", 24)

    def append(timestamps, received):
        controller.handle.audio.put_nowait(
            tuple(
                SimpleNamespace(
                    payload=tone_packets()[5],
                    rtp_timestamp=timestamp,
                    received_at=received,
                    capture_epoch=controller.handle.capture_epoch,
                )
                for timestamp in timestamps
            )
        )

    manager._clock = lambda: now[0]
    try:
        await manager.start(
            controller.boot, controller.scope, identity, CONFIG, audio_config=selected
        )
        manager._sessions[identity].audio._clock = lambda: now[0]
        append([0], began)
        await first_feed.wait()
        if scenario == "receipt_gap":
            now[0] = began + MAX_AUDIO_AGE + 0.25
            append([960, 1920, 2880], now[0])
            await until(lambda: len(instances) == 2)
            assert instances[0].closed and instances[1].inputs == [960, 1920, 2880]
            # The source cursor already advanced: reuse this actual fresh read
            # after joined reset instead of discarding all three source units.
        else:
            now[0] = began + MAX_AUDIO_AGE - 0.01
            append([960, 1920], now[0])
            await until(lambda: instances[0].groups == 1)
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.get_running_loop().sock_recv(audio, 4096), 0.03)
            now[0] = began + MAX_AUDIO_AGE + 0.02
            append([2880, 3840, 4800], now[0])
            await until(lambda: instances[0].inputs[-1] == 4800)
        packets = await receive_media(audio, crypto(result.audio.ssrc), 1)
        assert parse_opus(packets[0].payload).duration_samples == 2880
        if scenario == "receipt_gap":
            assert len(instances) == 2 and instances[0].closed
            assert instances[0].inputs == [0] and instances[1].inputs == [960, 1920, 2880]
        else:
            assert len(instances) == 1 and instances[0].groups == 2
            assert instances[0].inputs == [0, 960, 1920, 2880, 3840, 4800]
    finally:
        video.close()
        audio.close()
        await manager.close()
