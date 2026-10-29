"""Separate local protected-wire pacing from the selected codec ceiling.

The peak is a finite local egress policy, never an estimate of path capacity.
The codec observer counts original encoded access-unit bytes in source time;
RTP/SRTP overhead, retransmissions and delivery bursts cannot inflate that rate.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

MAX_AU_PACKETS = 4096
MAX_AU_WIRE_BYTES = 2 * 1024 * 1024 + 128 * 1024
MAX_DATAGRAM_BYTES = 1200
MIN_PEAK_BPS = 2_000_000
MAX_PEAK_BPS = 64_000_000
MAX_CODEC_ROWS = 128
SOURCE_SECOND = 90000
_LIMIT = 2**53 - 1


class VideoBudgetError(ValueError):
    """Invalid or excessive metadata, without any media in the error."""


def _time(value):
    if type(value) not in {int, float} or not 0 <= value <= 2**48 or not math.isfinite(value):
        raise VideoBudgetError("Invalid local egress time")
    return value


@dataclass(frozen=True)
class WireReservation:
    packets: int
    protected_bytes: int
    start: float
    finish: float


@dataclass(frozen=True)
class VideoWirePolicy:
    codec_kbps: int

    def __post_init__(self):
        if type(self.codec_kbps) is not int or not 1 <= self.codec_kbps <= 20000:
            raise VideoBudgetError("Invalid selected codec ceiling")

    @property
    def peak_bps(self):
        return min(MAX_PEAK_BPS, max(MIN_PEAK_BPS, 4 * self.codec_kbps * 1000))

    def service(self, protected_bytes):
        if type(protected_bytes) is not int or not 1 <= protected_bytes <= MAX_DATAGRAM_BYTES:
            raise VideoBudgetError("Invalid protected datagram size")
        return protected_bytes * 8 / self.peak_bps

    def reserve(self, lengths, *, now, next_packet_at, deadline):
        """Dry-run a complete AU before consuming RTP or cryptographic indexes.

        No token is banked while idle. Existing work and every byte of this AU
        must fit the original source deadline at the finite peak. The caller
        owns the one-AU lock and rechecks each actual send after every await.
        """
        now, next_packet_at, deadline = map(_time, (now, next_packet_at, deadline))
        if not isinstance(lengths, tuple) or not 1 <= len(lengths) <= MAX_AU_PACKETS:
            raise VideoBudgetError("Access unit exceeds datagram count limit")
        total = 0
        for length in lengths:
            self.service(length)
            total += length
            if total > MAX_AU_WIRE_BYTES:
                raise VideoBudgetError("Access unit exceeds protected byte limit")
        start = max(now, next_packet_at)
        finish = start + total * 8 / self.peak_bps
        if finish > deadline:
            return None
        return WireReservation(len(lengths), total, start, finish)


class CodecRateMonitor:
    """Bounded read-side one-second codec validation, never a dropping pacer.

    Windows are (source_pts-90000, source_pts], including partial startup. Full
    coverage is reported only after an unbroken observed source second. A
    backwards PTS or metadata overflow invalidates coverage rather than
    pretending an incomplete ledger proves conformance. Same-PTS bytes add.
    Configuration/epoch changes create a new measurement domain explicitly.
    """

    def __init__(self):
        self._rows = deque()
        self._domain = None
        self._first = self._last = None
        self._bytes = self._current = self._peak = 0
        self._samples = self._complete = self._exceedances = 0
        self._resets = self._unknown = 0
        self._selected = None

    def observe(self, *, epoch, generation, selected_kbps, source_pts, encoded_bytes):
        if (
            type(epoch) is not int
            or not 0 <= epoch <= _LIMIT
            or type(generation) is not int
            or not 1 <= generation <= _LIMIT
            or type(source_pts) is not int
            or not -(2**63) <= source_pts < 2**63
            or type(encoded_bytes) is not int
            or not 1 <= encoded_bytes <= 2 * 1024 * 1024
        ):
            raise VideoBudgetError("Invalid codec rate metadata")
        VideoWirePolicy(selected_kbps)
        domain = epoch, generation, selected_kbps
        if domain != self._domain:
            if self._domain is not None:
                self._resets = min(_LIMIT, self._resets + 1)
            self._rows.clear()
            self._first = self._last = None
            self._bytes = self._current = self._peak = 0
            self._samples = self._complete = self._exceedances = 0
            self._domain, self._selected = domain, selected_kbps
        if self._last is not None and source_pts < self._last:
            self._rows.clear()
            self._first = None
            self._bytes = 0
            self._unknown = min(_LIMIT, self._unknown + 1)
        if self._first is None:
            self._first = source_pts
        self._last = source_pts
        while self._rows and self._rows[0][0] <= source_pts - SOURCE_SECOND:
            self._bytes -= self._rows.popleft()[1]
        if self._rows and self._rows[-1][0] == source_pts:
            timestamp, previous = self._rows.pop()
            updated = min(_LIMIT // 8, previous + encoded_bytes)
            encoded_bytes = updated - previous
            self._rows.append((timestamp, updated))
        else:
            if len(self._rows) == MAX_CODEC_ROWS:
                self._rows.clear()
                self._bytes = 0
                self._first = source_pts
                self._unknown = min(_LIMIT, self._unknown + 1)
            self._rows.append((source_pts, encoded_bytes))
        self._bytes += encoded_bytes
        self._current = min(_LIMIT, self._bytes * 8)
        self._peak = max(self._peak, self._current)
        self._samples = min(_LIMIT, self._samples + 1)
        if source_pts - self._first >= SOURCE_SECOND:
            self._complete = min(_LIMIT, self._complete + 1)
        if self._current > selected_kbps * 1000:
            self._exceedances = min(_LIMIT, self._exceedances + 1)

    def snapshot(self):
        return {
            "selected_bps": self._selected * 1000 if self._selected is not None else None,
            "window_payload_bits": self._current if self._domain is not None else None,
            "peak_window_payload_bits": self._peak if self._domain is not None else None,
            "samples": self._samples,
            "window_coverage": (
                "unknown"
                if self._first is None
                else "complete"
                if self._last - self._first >= SOURCE_SECOND
                else "partial"
            ),
            "complete_windows": self._complete,
            "above_selected_windows": self._exceedances,
            "measurement_resets": self._resets,
            "unknown_windows": self._unknown,
        }
