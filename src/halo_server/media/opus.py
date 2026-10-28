"""Finite RFC 6716 packet framing; no native codec or dynamic-library dependency."""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from dataclasses import dataclass

MAX_OPUS_PACKET = 65535
MAX_OPUS_FRAME = 1275
MAX_OPUS_FRAMES = 48
MAX_OPUS_SAMPLES = 5760
MAX_AUDIO_PAYLOAD = 1178  # 1200 UDP cap less RTP and SRTP authentication.


class OpusError(ValueError):
    """Malformed or unsupported bounded audio input."""


@dataclass(frozen=True)
class AudioConfiguration:
    sample_rate: int
    packet_time: int
    bitrate_mode: str
    max_bit_rate: int
    payload_type: int = 110
    rtcp_interval: float = 0.5
    channels: int = 1
    gain: int = 100
    clock_mode: str = "classic_hap"

    def __post_init__(self):
        for value in (
            self.sample_rate,
            self.packet_time,
            self.max_bit_rate,
            self.payload_type,
            self.channels,
            self.gain,
        ):
            if type(value) is not int:
                raise OpusError("Invalid audio configuration")
        if (
            self.sample_rate not in {8000, 16000, 24000}
            or self.packet_time not in {20, 30, 40, 60}
            or self.bitrate_mode not in {"cbr", "vbr"}
            or not 6 <= self.max_bit_rate <= 64
            or not 96 <= self.payload_type <= 127
            or self.channels != 1
            or not 0 <= self.gain <= 100
            or self.clock_mode != "classic_hap"
            or isinstance(self.rtcp_interval, bool)
            or not isinstance(self.rtcp_interval, (int, float))
            or not math.isfinite(self.rtcp_interval)
            or not 0.1 <= self.rtcp_interval <= 10
        ):
            raise OpusError("Invalid audio configuration")


@dataclass(frozen=True)
class OpusPacket:
    toc: int
    frames: tuple[bytes, ...]
    frame_samples: int
    channels: int
    bandwidth_rate: int

    @property
    def duration_samples(self):
        return len(self.frames) * self.frame_samples

    @property
    def samples(self):
        return self.duration_samples


@dataclass(frozen=True)
class AudioOutput:
    payload: bytes
    timestamp: int
    duration: int
    mode: str


def _length(data, offset, end):
    if offset >= end:
        raise OpusError("Truncated Opus frame size")
    size = data[offset]
    offset += 1
    if size >= 252:
        if offset >= end:
            raise OpusError("Truncated Opus frame size")
        size += 4 * data[offset]
        offset += 1
    if size > MAX_OPUS_FRAME:
        raise OpusError("Opus frame exceeds limit")
    return size, offset


def parse_opus(payload: bytes) -> OpusPacket:
    if not isinstance(payload, bytes) or not 1 <= len(payload) <= MAX_OPUS_PACKET:
        raise OpusError("Invalid Opus packet size")
    config, code = payload[0] >> 3, payload[0] & 3
    if config < 12:
        frame_samples = (480, 960, 1920, 2880)[config % 4]
        bandwidth = (8000, 12000, 16000)[config // 4]
    elif config < 16:
        frame_samples = (480, 960)[config % 2]
        bandwidth = 24000 if config < 14 else 48000
    else:
        frame_samples = (120, 240, 480, 960)[config % 4]
        bandwidth = (8000, 16000, 24000, 48000)[(config - 16) // 4]
    start, end = 1, len(payload)
    if code == 0:
        sizes = [end - start]
    elif code == 1:
        if (end - start) % 2:
            raise OpusError("Unaligned equal Opus frames")
        sizes = [(end - start) // 2] * 2
    elif code == 2:
        size, start = _length(payload, start, end)
        sizes = [size, end - start - size]
    else:
        if start >= end:
            raise OpusError("Truncated Opus frame count")
        control = payload[start]
        start += 1
        count = control & 63
        if not 1 <= count <= MAX_OPUS_FRAMES or count * frame_samples > MAX_OPUS_SAMPLES:
            raise OpusError("Invalid Opus duration")
        padding = 0
        if control & 64:
            while True:
                if start >= end:
                    raise OpusError("Truncated Opus padding")
                value = payload[start]
                start += 1
                padding += 254 if value == 255 else value
                if padding > end - start:
                    raise OpusError("Invalid Opus padding")
                if value != 255:
                    break
        end -= padding
        if control & 128:
            sizes = []
            for _ in range(count - 1):
                size, start = _length(payload, start, end)
                sizes.append(size)
            sizes.append(end - start - sum(sizes))
        else:
            if (end - start) % count:
                raise OpusError("Unaligned equal Opus frames")
            sizes = [(end - start) // count] * count
    if (
        len(sizes) * frame_samples > MAX_OPUS_SAMPLES
        or any(not 0 <= size <= MAX_OPUS_FRAME for size in sizes)
        or start + sum(sizes) != end
    ):
        raise OpusError("Invalid Opus frame lengths")
    frames = []
    for size in sizes:
        frames.append(payload[start : start + size])
        start += size
    return OpusPacket(
        payload[0] & 252, tuple(frames), frame_samples, 2 if payload[0] & 4 else 1, bandwidth
    )


def _encode_length(size):
    if size < 252:
        return bytes((size,))
    first = 252 + (size - 252) % 4
    return bytes((first, (size - first) // 4))


def repacketize(packets) -> bytes:
    parsed = []
    frames = []
    for payload in packets:
        packet = parse_opus(payload)
        if parsed and packet.toc != parsed[0].toc:
            raise OpusError("Incompatible Opus configurations")
        parsed.append(packet)
        frames.extend(packet.frames)
        if len(frames) > MAX_OPUS_FRAMES or len(frames) * packet.frame_samples > MAX_OPUS_SAMPLES:
            raise OpusError("Opus duration exceeds limit")
    if not parsed:
        raise OpusError("No Opus frames")
    toc = parsed[0].toc
    if len(frames) == 1:
        return bytes((toc,)) + frames[0]
    equal = len({len(frame) for frame in frames}) == 1
    if len(frames) == 2:
        return (
            bytes((toc | 1,)) + b"".join(frames)
            if equal
            else bytes((toc | 2,)) + _encode_length(len(frames[0])) + b"".join(frames)
        )
    header = bytes((toc | 3, len(frames) | (0 if equal else 128)))
    if not equal:
        header += b"".join(_encode_length(len(frame)) for frame in frames[:-1])
    return header + b"".join(frames)


class OriginalOpusBuffer:
    """Original Ring packets only, with no video-boundary dependency or normalization."""

    def __init__(self, *, clock=time.monotonic):
        self._clock = clock
        self._condition = threading.Condition(threading.RLock())
        self._packets = deque()
        self._bytes = 0
        self._sequence = 0
        self._epoch = 1

    @property
    def capture_epoch(self):
        with self._condition:
            return self._epoch

    def clear(self, *, capture_epoch=None):
        if capture_epoch is not None and (type(capture_epoch) is not int or capture_epoch < 1):
            raise OpusError("Invalid capture epoch")
        with self._condition:
            self._epoch = self._epoch + 1 if capture_epoch is None else capture_epoch
            self._packets.clear()
            self._bytes = 0
            self._condition.notify_all()

    def _expire(self, now):
        while self._packets and (
            len(self._packets) > 512
            or self._bytes > 1024 * 1024
            or now - self._packets[0].received_at > 2
        ):
            self._bytes -= len(self._packets.popleft().payload)

    def append_audio(self, payload, rtp_timestamp, *, received_at=None):
        from halo_server.media.buffer import EncodedPacket, MediaStreamKind

        parse_opus(payload)
        if type(rtp_timestamp) is not int or rtp_timestamp < 0:
            raise OpusError("Invalid source audio timestamp")
        now = self._clock() if received_at is None else received_at
        if (
            isinstance(now, bool)
            or not isinstance(now, (int, float))
            or not math.isfinite(now)
            or now < 0
        ):
            raise OpusError("Invalid audio receipt time")
        with self._condition:
            if self._packets and now < self._packets[-1].received_at:
                raise OpusError("Audio receipt time moved backwards")
            self._sequence += 1
            packet = EncodedPacket(
                self._sequence,
                MediaStreamKind.AUDIO,
                payload,
                rtp_timestamp,
                float(now),
                capture_epoch=self._epoch,
            )
            self._packets.append(packet)
            self._bytes += len(payload)
            self._expire(now)
            self._condition.notify_all()
            return packet

    def read(self, *, after_sequence, timeout=None):
        from halo_server.media.buffer import BufferRead

        if after_sequence is not None and (type(after_sequence) is not int or after_sequence < 0):
            raise OpusError("Invalid audio sequence")
        if timeout is not None and (not math.isfinite(timeout) or timeout < 0):
            raise OpusError("Invalid audio wait")
        with self._condition:
            self._expire(self._clock())
            self._condition.wait_for(
                lambda: (
                    bool(self._packets)
                    and (after_sequence is None or self._packets[-1].sequence > after_sequence)
                ),
                timeout=timeout,
            )
            self._expire(self._clock())
            if not self._packets:
                return BufferRead((), False, self._epoch)
            resync = after_sequence is not None and self._packets[0].sequence > after_sequence + 1
            return BufferRead(
                tuple(
                    packet
                    for packet in self._packets
                    if after_sequence is None or resync or packet.sequence > after_sequence
                ),
                resync,
                self._epoch,
            )

    def snapshot(self):
        from halo_server.media.buffer import BufferSnapshot

        with self._condition:
            self._expire(self._clock())
            packets = tuple(self._packets)
            duration = packets[-1].received_at - packets[0].received_at if len(packets) > 1 else 0.0
            return BufferSnapshot(packets, self._bytes, duration, None, self._epoch)
