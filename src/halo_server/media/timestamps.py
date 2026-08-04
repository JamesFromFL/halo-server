"""Monotonic media timestamp mapping across Ring stream renewal."""

from __future__ import annotations

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402


class EncodedTimestampMapper:
    """Map one encoded RTP clock to a monotonic GStreamer timeline."""

    def __init__(self, *, clock_rate: int, default_duration_ns: int) -> None:
        self._clock_rate = clock_rate
        self._default_duration_ns = default_duration_ns
        self._source_base: int | None = None
        self._source_last: int | None = None
        self._output_base = 0
        self._output_last: int | None = None

    def map(self, timestamp: int) -> tuple[int, bool]:
        discontinuity = self._source_base is None
        if self._source_base is None:
            self._source_base = timestamp
        elif self._source_last is not None and timestamp < self._source_last:
            self._source_base = timestamp
            self._output_base = (self._output_last or 0) + self._default_duration_ns
            discontinuity = True

        relative = max(0, timestamp - self._source_base)
        pts = self._output_base + (relative * Gst.SECOND // self._clock_rate)
        if self._output_last is not None and pts <= self._output_last:
            pts = self._output_last + 1
        self._source_last = timestamp
        self._output_last = pts
        return pts, discontinuity


class SampleTimestampMapper:
    """Build a gap-free audio timeline from the samples actually received."""

    def __init__(self) -> None:
        self._next_pts = 0
        self._first = True

    def map(self, samples: int, rate: int) -> tuple[int, int, bool]:
        if samples <= 0 or rate <= 0:
            raise ValueError("audio samples and rate must be positive")
        pts = self._next_pts
        duration = samples * Gst.SECOND // rate
        self._next_pts += duration
        discontinuity = self._first
        self._first = False
        return pts, duration, discontinuity
