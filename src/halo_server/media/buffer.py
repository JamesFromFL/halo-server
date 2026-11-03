"""Thread-safe bounded encoded media prebuffer."""

from __future__ import annotations

import math
import re
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum

from halo_server.media import h264

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
    capture_epoch: int = 0


@dataclass(frozen=True)
class BufferSnapshot:
    packets: tuple[EncodedPacket, ...]
    payload_bytes: int
    duration_seconds: float
    decodable_video_sequence: int | None
    capture_epoch: int = 0

    @property
    def ready(self) -> bool:
        return self.decodable_video_sequence is not None


@dataclass(frozen=True)
class BufferRead:
    packets: tuple[EncodedPacket, ...]
    resynchronized: bool
    capture_epoch: int = 0


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
        self._capture_epoch = 1
        self._last_received_at: float | None = None
        # Only the current configuration survives media-window eviction. Its
        # two bounded NALs count against the same byte budget as packet history.
        self._sps: bytes | None = None
        self._pps: bytes | None = None
        self._sps_id: int | None = None
        self._pps_id: int | None = None
        self._pps_sps_id: int | None = None
        self._parameter_bytes = 0
        self._video_start_sequence: int | None = None
        self._decodable_boundaries: deque[int] = deque()

    @property
    def capture_epoch(self) -> int:
        with self._condition:
            return self._capture_epoch

    @property
    def ready(self) -> bool:
        with self._condition:
            return self._first_retained_boundary_locked() is not None

    @property
    def video_continuity(self) -> bool:
        """A retained IDR established this configuration for continuing readers."""
        with self._condition:
            return self._video_start_sequence is not None

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
                return BufferSnapshot((), 0, 0.0, None, self._capture_epoch)
            packets = self._initial_packets_locked(boundary)
            payload_bytes = sum(len(packet.payload) for packet in packets)
            duration = (
                max(0.0, packets[-1].received_at - packets[0].received_at)
                if len(packets) > 1
                else 0.0
            )
            return BufferSnapshot(packets, payload_bytes, duration, boundary, self._capture_epoch)

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

            def contiguous() -> bool:
                return (
                    after_sequence is not None
                    and self._video_start_sequence is not None
                    and after_sequence >= self._video_start_sequence
                    and bool(self._packets)
                    and self._packets[0].sequence <= after_sequence + 1
                )

            def available() -> bool:
                if not self._packets:
                    return False
                if contiguous():
                    return self._packets[-1].sequence > after_sequence
                boundary = self._first_retained_boundary_locked()
                return boundary is not None and (
                    after_sequence is None or boundary > after_sequence
                )

            if not self._condition.wait_for(available, timeout=timeout):
                return BufferRead((), False, self._capture_epoch)
            if contiguous():
                return BufferRead(
                    tuple(packet for packet in self._packets if packet.sequence > after_sequence),
                    False,
                    self._capture_epoch,
                )
            boundary = self._first_retained_boundary_locked()
            return BufferRead(
                self._initial_packets_locked(boundary),
                after_sequence is not None,
                self._capture_epoch,
            )

    def clear(self) -> None:
        with self._condition:
            self._capture_epoch += 1
            self._retire_video_locked(parameters=True)
            self._last_received_at = None
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
            decodable_idr = False
            if stream is MediaStreamKind.VIDEO:
                try:
                    decodable_idr = self._update_parameters_locked(payload)
                except h264.H264Error as exc:
                    # A rejected update must not leave stale parameters usable
                    # by a later IDR that reuses the same parameter IDs.
                    self._retire_video_locked(parameters=True)
                    self._condition.notify_all()
                    raise MediaBufferError(str(exc)) from exc
            sequence = self._next_sequence
            self._next_sequence += 1
            packet = EncodedPacket(
                sequence=sequence,
                stream=stream,
                payload=bytes(payload),
                rtp_timestamp=rtp_timestamp,
                received_at=normalized_time,
                h264_nal_types=nal_types,
                capture_epoch=self._capture_epoch,
            )
            self._packets.append(packet)
            self._payload_bytes += len(packet.payload)
            self._last_received_at = normalized_time

            if decodable_idr:
                self._decodable_boundaries.append(sequence)
                if self._video_start_sequence is None:
                    self._video_start_sequence = sequence

            self._prune_locked(normalized_time)
            self._condition.notify_all()
            return packet

    def _prune_locked(self, newest_time: float) -> None:
        cutoff = newest_time - self._max_duration
        while self._packets and (
            self._packets[0].received_at < cutoff
            or self._payload_bytes + self._parameter_bytes > self._max_bytes
            or len(self._packets) > self._max_packets
        ):
            removed = self._packets.popleft()
            self._payload_bytes -= len(removed.payload)
        first_sequence = self._packets[0].sequence if self._packets else self._next_sequence
        while self._decodable_boundaries and self._decodable_boundaries[0] < first_sequence:
            self._decodable_boundaries.popleft()
        if not self._packets:
            # An access unit that cannot fit never establishes a usable start,
            # and losing the entire retained window breaks reader continuity.
            self._video_start_sequence = None

    def _retire_video_locked(self, *, parameters: bool = False) -> None:
        self._packets.clear()
        self._payload_bytes = 0
        self._decodable_boundaries.clear()
        self._video_start_sequence = None
        if parameters:
            self._sps = self._pps = None
            self._sps_id = self._pps_id = self._pps_sps_id = None
            self._parameter_bytes = 0

    def _update_parameters_locked(self, payload: bytes) -> bool:
        nals = h264.split_annexb(payload)
        changed = False
        slices: list[tuple[int, int]] = []
        for nal in nals:
            kind = nal[0] & 31
            if kind in {7, 8} and slices:
                raise h264.H264Error("H.264 parameters follow picture slices")
            if kind == 7:
                source = h264.parse_sps(nal)
                if nal != self._sps:
                    # Reusing an SPS ID with different content requires a fresh
                    # PPS, even if the old PPS happened to name that same ID.
                    if self._sps_id == source.sequence_id or (
                        self._pps_sps_id != source.sequence_id
                    ):
                        self._pps = None
                        self._pps_id = self._pps_sps_id = None
                    self._sps, self._sps_id = nal, source.sequence_id
                    changed = True
            elif kind == 8:
                picture_id, sequence_id = h264.pps_ids(nal)
                if nal != self._pps:
                    self._pps, self._pps_id, self._pps_sps_id = nal, picture_id, sequence_id
                    changed = True
            elif kind in {1, 5}:
                slices.append((kind, h264.slice_pps_id(nal)))
        self._parameter_bytes = sum(
            len(nal) + 4 for nal in (self._sps, self._pps) if nal is not None
        )
        if self._parameter_bytes > self._max_bytes:
            raise h264.H264Error("H.264 parameters exceed the buffer byte limit")
        matching = (
            self._sps is not None
            and self._pps is not None
            and self._pps_sps_id == self._sps_id
            and all(picture_id == self._pps_id for _, picture_id in slices)
        )
        if changed or (slices and not matching):
            self._retire_video_locked()
        # Mixed IDR/non-IDR slices cannot establish one random-access picture.
        decodable_idr = bool(slices) and matching and all(kind == 5 for kind, _ in slices)
        if decodable_idr and not {7, 8}.issubset(nal[0] & 31 for nal in nals):
            prefixed_size = self._parameter_bytes + sum(
                len(nal) + 4 for nal in nals if nal[0] & 31 not in {7, 8}
            )
            if prefixed_size > min(self._max_bytes, h264.MAX_ACCESS_UNIT_BYTES):
                raise h264.H264Error("Prefixed H.264 access unit exceeds byte limit")
        return decodable_idr

    def _initial_packets_locked(self, boundary: int) -> tuple[EncodedPacket, ...]:
        packets = tuple(packet for packet in self._packets if packet.sequence >= boundary)
        first = packets[0]
        if not {7, 8}.issubset(first.h264_nal_types):
            # Preserve the IDR's sequence, source timestamp, receipt time and
            # epoch: only its missing decoder configuration is synthesized.
            nals = h264.split_annexb(first.payload)
            payload = h264.annexb(
                (self._sps, self._pps, *(nal for nal in nals if nal[0] & 31 not in {7, 8}))
            )
            first = replace(first, payload=payload, h264_nal_types=first.h264_nal_types | {7, 8})
        return (first, *packets[1:])

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
