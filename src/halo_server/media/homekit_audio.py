"""Negotiated audio endpoints owned by one live camera output session."""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import socket
import struct
import time
from collections import deque
from dataclasses import dataclass, field

from aiortc.rtp import RtpPacket

from halo_server.homekit.models import HomeKitOperationError
from halo_server.media.rtp import RtpError, parse_feedback, sender_report
from halo_server.media.srtp import SecureVideo, SrtpError
from halo_server.media.talkback import Talkback, TalkPacket

MAX_AUDIO_AGE = 0.5
MAX_AUDIO_PACKET = 1200
RECEIVER_SECONDS = 60.0
SEND_SECONDS = 0.25
MAX_DATAGRAM_RATE = 128
MAX_INPUT_SPANS = 512


@dataclass(frozen=True)
class AudioEndpointRequest:
    target_port: int
    key: bytes = field(repr=False)
    salt: bytes = field(repr=False)

    def __post_init__(self):
        if (
            type(self.target_port) is not int
            or not 1 <= self.target_port <= 65535
            or not isinstance(self.key, bytes)
            or len(self.key) != 16
            or not isinstance(self.salt, bytes)
            or len(self.salt) != 14
        ):
            raise HomeKitOperationError("invalid_request")


@dataclass(frozen=True)
class PreparedAudio:
    port: int
    ssrc: int
    key: bytes = field(repr=False)
    salt: bytes = field(repr=False)


class AudioTrack:
    """No source lease: both directions use their parent session's live handle."""

    def __init__(
        self,
        request,
        *,
        bind,
        endpoint,
        family,
        excluded_ssrc,
        check,
        epoch,
        controller,
        boot,
        scope,
        failure,
        presentation,
        touch_feedback,
        last_feedback,
        clock=time.monotonic,
        adapter_factory=None,
        decoder_factory=None,
    ):
        self.request = request
        self._check, self._epoch, self._failure = check, epoch, failure
        self._clock, self._presentation = clock, presentation
        self._touch_feedback, self._last_seen = touch_feedback, last_feedback
        self._adapter_factory, self._decoder_factory = adapter_factory, decoder_factory
        self._controller, self._boot, self._scope = controller, boot, scope
        self._retired = False
        self.socket = socket.socket(family, socket.SOCK_DGRAM)
        self.secure = None
        try:
            self.socket.setblocking(False)
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 64 * 1024)
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 64 * 1024)
            self.socket.bind(bind)
            self.socket.connect(endpoint)
            ssrc = secrets.randbelow(0xFFFFFFFF) + 1
            while ssrc == excluded_ssrc:
                ssrc = secrets.randbelow(0xFFFFFFFF) + 1
            self.secure = SecureVideo(request.key, request.salt, ssrc)
            self.prepared = PreparedAudio(
                self.socket.getsockname()[1], ssrc, request.key, request.salt
            )
        except BaseException:
            self.socket.close()
            if self.secure is not None:
                self.secure.close()
            raise
        self._tasks = []
        self._adapter = None
        self.talkback = None
        self.config = None
        self.handle = None
        self._sequence = secrets.randbelow(65536)
        self._base = secrets.randbits(32)
        self._source_base = self._source_last = None
        self._last_timestamp = None
        self._last_duration = 0
        self._last_presentation = self._clock()
        self._last_feedback = self._clock()
        self._packets = self._octets = 0
        self._next_packet_at = 0.0
        self._capture_epoch = None
        self._input_spans = deque()
        self._input_end = None
        self.failure = None

    def start(self, handle, config, *, speaker_volume):
        from halo_server.media.homekit_audio_codec import AudioAdapter

        self.handle, self.config = handle, config
        self._adapter = (self._adapter_factory or AudioAdapter)(config)
        self.talkback = Talkback(
            self._controller,
            self._boot,
            self._scope,
            check=self._check,
            epoch=self._epoch,
            gain=speaker_volume,
            decoder_factory=self._decoder_factory,
            clock=self._clock,
        )
        self._last_feedback = self._clock()
        self._tasks = [
            asyncio.create_task(self._send_audio(), name="homekit-audio-send"),
            asyncio.create_task(self._receive(), name="homekit-audio-receive"),
            asyncio.create_task(self._reports(), name="homekit-audio-sr"),
        ]
        for task in self._tasks:
            task.add_done_callback(_observed)

    @property
    def cleanup_failed(self):
        return self.talkback is not None and self.talkback.cleanup_failed

    def _may(self, permission):
        if self._retired:
            raise HomeKitOperationError("authority_changed")
        try:
            self._check(permission)
        except HomeKitOperationError as exc:
            if exc.code == "policy_denied":
                return False
            raise
        return True

    async def _send(self, packet, *, permission=None, epoch=None, fresh_until=None):
        deadline = self._clock() + SEND_SECONDS
        while True:
            if self._retired:
                raise HomeKitOperationError("authority_changed")
            self._check(permission)
            if epoch is not None and epoch != self._epoch():
                return False
            if fresh_until is not None and self._clock() > fresh_until:
                return False
            try:
                if self.socket.send(packet) != len(packet):
                    raise HomeKitOperationError("media_failed")
                return True
            except BlockingIOError:
                if self._clock() >= deadline:
                    raise HomeKitOperationError("media_saturated") from None
                await asyncio.sleep(0.002)

    def _reset(self, epoch):
        self._capture_epoch = epoch
        self._source_base = self._source_last = None
        if self._last_timestamp is not None:
            self._base = (self._last_timestamp + self._last_duration) & 0xFFFFFFFF
        self._next_packet_at = 0
        self._input_spans.clear()
        self._input_end = None

    async def _discard_expired_prefix(self):
        if self._input_spans and self._clock() - self._input_spans[0][2] > MAX_AUDIO_AGE:
            from halo_server.media.homekit_audio_codec import AudioAdapter

            await self._adapter.close()
            self._check("live_audio")
            if self._retired:
                raise HomeKitOperationError("authority_changed")
            self._adapter = (self._adapter_factory or AudioAdapter)(self.config)
            self._input_spans.clear()
            self._input_end = None

    def _output_receipt(self, output, fallback):
        start, end = output.timestamp, output.timestamp + output.duration
        receipts = [
            received for first, last, received in self._input_spans if last > start and first < end
        ]
        # Native encoder lookahead can precede the first submitted timestamp.
        # Conservatively retain the oldest receipt if no overlap is provable.
        received = (
            min(receipts)
            if receipts
            else (self._input_spans[0][2] if self._input_spans else fallback)
        )
        while self._input_spans and self._input_spans[0][1] <= end:
            self._input_spans.popleft()
        return received

    def _timestamp(self, timestamp):
        if self._source_base is None:
            self._source_base = timestamp
        if self._source_last is not None and timestamp < self._source_last:
            self._source_base = timestamp
            self._base = ((self._last_timestamp or self._base) + self._last_duration) & 0xFFFFFFFF
        self._source_last = timestamp
        return (
            self._base + (timestamp - self._source_base) * self.config.sample_rate // 48000
        ) & 0xFFFFFFFF

    async def _send_audio(self):
        from halo_server.media.opus import parse_opus

        try:
            while not self._retired:
                self._check(None)
                if not self._may("live_audio"):
                    await asyncio.sleep(0.05)
                    continue
                try:
                    batch = await self.handle.read_audio(timeout=0.25)
                except HomeKitOperationError as exc:
                    if exc.code != "policy_denied":
                        raise
                    await asyncio.sleep(0.05)
                    continue
                if batch.capture_epoch != self._capture_epoch:
                    self._reset(batch.capture_epoch)
                if not self._may("live_audio"):
                    continue
                for packet in batch.packets:
                    if packet.capture_epoch != self._epoch():
                        continue
                    if self._clock() - packet.received_at > MAX_AUDIO_AGE:
                        continue
                    await self._discard_expired_prefix()
                    if packet.capture_epoch != self._epoch():
                        break
                    if self._clock() - packet.received_at > MAX_AUDIO_AGE:
                        continue
                    if self._input_end is not None and packet.rtp_timestamp != self._input_end:
                        self._input_spans.clear()
                    self._input_end = (
                        packet.rtp_timestamp + parse_opus(packet.payload).duration_samples
                    )
                    if len(self._input_spans) >= MAX_INPUT_SPANS:
                        raise HomeKitOperationError("media_saturated")
                    self._input_spans.append(
                        (
                            packet.rtp_timestamp,
                            self._input_end,
                            packet.received_at,
                        )
                    )
                    outputs = await self._adapter.feed(
                        packet.payload, packet.rtp_timestamp, capture_epoch=packet.capture_epoch
                    )
                    for output in outputs:
                        received_at = self._output_receipt(output, packet.received_at)
                        if packet.capture_epoch != self._epoch():
                            break
                        if self._clock() - received_at > MAX_AUDIO_AGE:
                            continue
                        target = max(self._clock(), self._next_packet_at)
                        if target > self._clock():
                            await asyncio.sleep(target - self._clock())
                        if self._clock() - received_at > MAX_AUDIO_AGE:
                            continue
                        if len(output.payload) + 22 > MAX_AUDIO_PACKET:
                            raise HomeKitOperationError("media_saturated")
                        timestamp = self._timestamp(output.timestamp)
                        plain = (
                            struct.pack(
                                "!BBHII",
                                0x80,
                                self.config.payload_type,
                                self._sequence,
                                timestamp,
                                self.prepared.ssrc,
                            )
                            + output.payload
                        )
                        protected = self.secure.protect(plain)
                        # Native SRTP consumed this index even if an epoch fence
                        # now rejects delivery. Never encrypt different PCM at it.
                        self._sequence = (self._sequence + 1) & 65535
                        if not await self._send(
                            protected,
                            permission="live_audio",
                            epoch=packet.capture_epoch,
                            fresh_until=received_at + MAX_AUDIO_AGE,
                        ):
                            break
                        self._last_timestamp = timestamp
                        self._last_duration = output.duration * self.config.sample_rate // 48000
                        self._last_presentation = self._presentation(
                            "audio", output.timestamp, received_at
                        )
                        self._packets += 1
                        self._octets += len(output.payload)
                        self._next_packet_at = target + output.duration / 48000
        except asyncio.CancelledError:
            raise
        except Exception:
            self.failure = "audio_output_failed"
            self._failure(self.failure)

    async def _receive(self):
        from halo_server.media.opus import parse_opus

        try:
            loop = asyncio.get_running_loop()
            window, count = self._clock(), 0
            while not self._retired:
                protected = await loop.sock_recv(self.socket, 4096)
                self._check(None)
                now = self._clock()
                if now - window >= 1:
                    window, count = now, 0
                count += 1
                if count > MAX_DATAGRAM_RATE:
                    await asyncio.sleep(0.01)
                    continue
                try:
                    if len(protected) >= 2 and 192 <= protected[1] <= 223:
                        feedback = parse_feedback(
                            self.secure.unprotect_rtcp(protected), self.prepared.ssrc
                        )
                        self._last_feedback = now
                        self._touch_feedback(now)
                        if feedback.bye:
                            self._failure("receiver_closed")
                            return
                        continue
                    clear = self.secure.unprotect(protected)
                    packet = RtpPacket.parse(clear)
                    if packet.payload_type != self.config.payload_type:
                        continue
                    parsed = parse_opus(packet.payload)
                    if parsed.channels != self.config.channels or parsed.duration_samples > 5760:
                        continue
                except (SrtpError, RtpError, ValueError, RuntimeError):
                    continue
                if not self._may("talkback"):
                    continue
                self._last_feedback = now
                self._touch_feedback(now)
                self.talkback.submit(
                    TalkPacket(
                        packet.payload, packet.sequence_number, packet.timestamp, now, self._epoch()
                    )
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            self.failure = "audio_feedback_failed"
            self._failure(self.failure)

    async def _reports(self):
        try:
            while not self._retired:
                await asyncio.sleep(self.config.rtcp_interval)
                self._check(None)
                if self._clock() - max(self._last_feedback, self._last_seen()) > RECEIVER_SECONDS:
                    self._failure("receiver_timeout")
                    return
                if (
                    self._last_timestamp is not None
                    and self._capture_epoch == self._epoch()
                    and self._may("live_audio")
                ):
                    timestamp = (
                        self._last_timestamp
                        + int(
                            max(0, self._clock() - self._last_presentation)
                            * self.config.sample_rate
                        )
                    ) & 0xFFFFFFFF
                    report = sender_report(
                        self.prepared.ssrc, timestamp, self._packets, self._octets
                    )
                    await self._send(
                        self.secure.protect_rtcp(report),
                        permission="live_audio",
                        epoch=self._capture_epoch,
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            self.failure = "audio_report_failed"
            self._failure(self.failure)

    def fence(self):
        self._retired = True
        descriptor = self.socket.fileno()
        if descriptor >= 0:
            loop = asyncio.get_running_loop()
            loop.remove_reader(descriptor)
            loop.remove_writer(descriptor)
            self.socket.close()
        if self.talkback is not None:
            self.talkback.fence()

    async def close(self):
        self.fence()
        for task in self._tasks:
            if not task.done():
                task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if self.talkback is not None:
            await self.talkback.close()
        if self._adapter is not None:
            await self._adapter.close()
        self.secure.close()
        self._input_spans.clear()


def _observed(task):
    if not task.cancelled():
        with contextlib.suppress(Exception):
            task.exception()
