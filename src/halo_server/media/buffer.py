"""Thread-safe bounded encoded media prebuffer."""

from __future__ import annotations

import math
import re
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

_ANNEX_B_START_CODE = re.compile(b"\x00\x00\x01")


class MediaStreamKind(StrEnum):
    VIDEO = "video"
    AUDIO = "audio"


class MediaBufferError(RuntimeError):
    """The encoded media buffer rejected unsafe input."""


@dataclass(frozen=True)
class EncodedPacket:
    sequence: int
    stream: MediaStreamKind
    payload: bytes
    rtp_timestamp: int
    received_at: float
    h264_nal_types: frozenset[int] = frozenset()


@dataclass(frozen=True)
class BufferSnapshot:
    packets: tuple[EncodedPacket, ...]
    payload_bytes: int
    duration_seconds: float
    decodable_video_sequence: int | None

    @property
    def ready(self) -> bool:
        return self.decodable_video_sequence is not None


@dataclass(frozen=True)
class BufferRead:
    packets: tuple[EncodedPacket, ...]
    resynchronized: bool


class EncodedMediaBuffer:
    """Retain a bounded H.264/Opus window beginning at a decodable frame."""

    def __init__(
        self,
        *,
        max_duration: float = 12.0,
        max_bytes: int = 32 * 1024 * 1024,
        max_packets: int = 4096,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not math.isfinite(max_duration) or max_duration <= 0:
            raise ValueError("max duration must be finite and positive")
        if max_bytes <= 0:
            raise ValueError("max bytes must be positive")
        if max_packets <= 0:
            raise ValueError("max packets must be positive")
        self._max_duration = max_duration
        self._max_bytes = max_bytes
        self._max_packets = max_packets
        self._clock = clock
        self._condition = threading.Condition(threading.RLock())
        self._packets: deque[EncodedPacket] = deque()
        self._payload_bytes = 0
        self._next_sequence = 1
        self._last_received_at: float | None = None
        self._latest_sps_sequence: int | None = None
        self._latest_pps_sequence: int | None = None
        self._decodable_boundaries: deque[int] = deque()

    @property
    def ready(self) -> bool:
        with self._condition:
            return self._first_retained_boundary_locked() is not None

    def append_video(
        self,
        payload: bytes,
        rtp_timestamp: int,
        *,
        received_at: float | None = None,
    ) -> EncodedPacket:
        if not isinstance(payload, bytes) or not payload:
            raise MediaBufferError("encoded media payload must be non-empty bytes")
        nal_types = h264_nal_types(payload)
        return self._append(
            MediaStreamKind.VIDEO,
            payload,
            rtp_timestamp,
            received_at=received_at,
            nal_types=nal_types,
        )

    def append_audio(
        self,
        payload: bytes,
        rtp_timestamp: int,
        *,
        received_at: float | None = None,
    ) -> EncodedPacket:
        return self._append(
            MediaStreamKind.AUDIO,
            payload,
            rtp_timestamp,
            received_at=received_at,
            nal_types=frozenset(),
        )

    def snapshot(self) -> BufferSnapshot:
        with self._condition:
            boundary = self._first_retained_boundary_locked()
            if boundary is None:
                return BufferSnapshot((), 0, 0.0, None)
            packets = tuple(packet for packet in self._packets if packet.sequence >= boundary)
            payload_bytes = sum(len(packet.payload) for packet in packets)
            duration = (
                max(0.0, packets[-1].received_at - packets[0].received_at)
                if len(packets) > 1
                else 0.0
            )
            return BufferSnapshot(packets, payload_bytes, duration, boundary)

    def wait_until_ready(self, timeout: float | None = None) -> bool:
        if timeout is not None and (not math.isfinite(timeout) or timeout < 0):
            raise ValueError("timeout must be finite and non-negative")
        with self._condition:
            return self._condition.wait_for(
                lambda: self._first_retained_boundary_locked() is not None,
                timeout=timeout,
            )

    def read(
        self,
        *,
        after_sequence: int | None,
        timeout: float | None = None,
    ) -> BufferRead:
        """Read a decodable initial window or packets following a prior read."""
        if after_sequence is not None and (
            isinstance(after_sequence, bool)
            or not isinstance(after_sequence, int)
            or after_sequence < 0
        ):
            raise ValueError("packet sequence must be a non-negative integer")
        if timeout is not None and (not math.isfinite(timeout) or timeout < 0):
            raise ValueError("timeout must be finite and non-negative")
        with self._condition:

            def available() -> bool:
                boundary = self._first_retained_boundary_locked()
                if boundary is None:
                    return False
                if after_sequence is None:
                    return True
                return boundary > after_sequence + 1 or self._packets[-1].sequence > after_sequence

            if not self._condition.wait_for(available, timeout=timeout):
                return BufferRead((), False)
            boundary = self._first_retained_boundary_locked()
            if boundary is None:
                return BufferRead((), False)
            resynchronized = after_sequence is not None and boundary > after_sequence + 1
            first_sequence = (
                boundary if after_sequence is None or resynchronized else after_sequence + 1
            )
            return BufferRead(
                tuple(packet for packet in self._packets if packet.sequence >= first_sequence),
                resynchronized,
            )

    def clear(self) -> None:
        with self._condition:
            self._packets.clear()
            self._payload_bytes = 0
            self._last_received_at = None
            self._latest_sps_sequence = None
            self._latest_pps_sequence = None
            self._decodable_boundaries.clear()
            self._condition.notify_all()

    def _append(
        self,
        stream: MediaStreamKind,
        payload: bytes,
        rtp_timestamp: int,
        *,
        received_at: float | None,
        nal_types: frozenset[int],
    ) -> EncodedPacket:
        if not isinstance(payload, bytes) or not payload:
            raise MediaBufferError("encoded media payload must be non-empty bytes")
        if len(payload) > self._max_bytes:
            raise MediaBufferError("encoded media packet exceeds the buffer byte limit")
        if (
            isinstance(rtp_timestamp, bool)
            or not isinstance(rtp_timestamp, int)
            or rtp_timestamp < 0
        ):
            raise MediaBufferError("RTP timestamp must be a non-negative integer")
        with self._condition:
            timestamp = self._clock() if received_at is None else received_at
            if not isinstance(timestamp, (int, float)) or isinstance(timestamp, bool):
                raise MediaBufferError("receipt time must be numeric")
            normalized_time = float(timestamp)
            if not math.isfinite(normalized_time) or normalized_time < 0:
                raise MediaBufferError("receipt time must be finite and non-negative")
            if self._last_received_at is not None and normalized_time < self._last_received_at:
                raise MediaBufferError("encoded media receipt time moved backwards")
            sequence = self._next_sequence
            self._next_sequence += 1
            packet = EncodedPacket(
                sequence=sequence,
                stream=stream,
                payload=bytes(payload),
                rtp_timestamp=rtp_timestamp,
                received_at=normalized_time,
                h264_nal_types=nal_types,
            )
            self._packets.append(packet)
            self._payload_bytes += len(packet.payload)
            self._last_received_at = normalized_time

            if stream is MediaStreamKind.VIDEO:
                if 7 in nal_types:
                    self._latest_sps_sequence = sequence
                if 8 in nal_types:
                    self._latest_pps_sequence = sequence
                if (
                    5 in nal_types
                    and self._latest_sps_sequence is not None
                    and self._latest_pps_sequence is not None
                ):
                    self._decodable_boundaries.append(
                        min(
                            self._latest_sps_sequence,
                            self._latest_pps_sequence,
                            sequence,
                        )
                    )

            self._prune_locked(normalized_time)
            if self._first_retained_boundary_locked() is not None:
                self._drop_before_first_boundary_locked()
            self._condition.notify_all()
            return packet

    def _prune_locked(self, newest_time: float) -> None:
        cutoff = newest_time - self._max_duration
        while self._packets and (
            self._packets[0].received_at < cutoff
            or self._payload_bytes > self._max_bytes
            or len(self._packets) > self._max_packets
        ):
            removed = self._packets.popleft()
            self._payload_bytes -= len(removed.payload)
        first_sequence = self._packets[0].sequence if self._packets else self._next_sequence
        while self._decodable_boundaries and self._decodable_boundaries[0] < first_sequence:
            self._decodable_boundaries.popleft()
        if self._latest_sps_sequence is not None and self._latest_sps_sequence < first_sequence:
            self._latest_sps_sequence = None
        if self._latest_pps_sequence is not None and self._latest_pps_sequence < first_sequence:
            self._latest_pps_sequence = None

    def _drop_before_first_boundary_locked(self) -> None:
        boundary = self._first_retained_boundary_locked()
        while boundary is not None and self._packets[0].sequence < boundary:
            removed = self._packets.popleft()
            self._payload_bytes -= len(removed.payload)

    def _first_retained_boundary_locked(self) -> int | None:
        if not self._packets or not self._decodable_boundaries:
            return None
        first_sequence = self._packets[0].sequence
        while self._decodable_boundaries and self._decodable_boundaries[0] < first_sequence:
            self._decodable_boundaries.popleft()
        return self._decodable_boundaries[0] if self._decodable_boundaries else None


def h264_nal_types(payload: bytes) -> frozenset[int]:
    """Return Annex-B NAL unit types present in one encoded access unit."""
    if not isinstance(payload, bytes):
        raise TypeError("H.264 payload must be bytes")
    types: set[int] = set()
    for match in _ANNEX_B_START_CODE.finditer(payload):
        header_index = match.end()
        if header_index < len(payload):
            types.add(payload[header_index] & 0x1F)
    return frozenset(types)
