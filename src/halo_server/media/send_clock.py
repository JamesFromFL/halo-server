"""Bounded relative timing at authenticated RTP entry, never an absolute clock.

The negotiated abs-send-time extension is a 24-bit 6.18 clock with a 64s wrap.
Its stamping endpoint may be a relay, not the camera. Differences describe that
stamp-to-callback boundary; they are not wire timestamps or end-to-end latency.
"""

from __future__ import annotations

import math
import threading

from halo_server.media.telemetry import StreamMetrics

KINDS = ("audio", "video")
TICKS = 1 << 18
MODULUS = 1 << 24
MAX_INTERVAL = 10
GAP_SECONDS = 0.5
COUNTERS = tuple(
    f"{kind}_{field}"
    for kind in KINDS
    for field in (
        "present",
        "absent",
        "invalid",
        "excluded",
        "sequence_discontinuity",
        "clock_reset",
        "compared",
        "gap_compared",
    )
)
TIMINGS = tuple(
    f"{kind}_{prefix}{field}"
    for kind in KINDS
    for prefix in ("", "gap_")
    for field in ("arrival_interval", "send_interval", "arrival_excess", "send_excess")
)


class SendClock:
    """Two previous numeric references; fixed metrics; no packets retained."""

    def __init__(self):
        self._lock = threading.Lock()
        self._previous = {}
        self._fenced = False
        self.metrics = StreamMetrics(counters=COUNTERS, timings=TIMINGS)

    def observe_receiver(self, kind, receiver, packet, now):
        codecs = getattr(receiver, "_RTCRtpReceiver__codecs", None)
        payload_type = getattr(packet, "payload_type", None)
        codec = (
            codecs.get(payload_type) if type(codecs) is dict and type(payload_type) is int else None
        )
        mime = getattr(codec, "mimeType", None)
        allowed = {
            "audio": ("audio/opus", "audio/g722", "audio/pcmu", "audio/pcma"),
            "video": ("video/h264", "video/vp8"),
        }
        if kind not in KINDS:
            return
        if type(mime) is not str or mime.lower() not in allowed[kind]:
            with self._lock:
                if not self._fenced:
                    self._previous.pop(kind, None)
                    self.metrics.count(f"{kind}_excluded")
            return
        self.observe(kind, packet, now)

    def observe(self, kind, packet, now):
        if kind not in KINDS or type(now) not in (int, float) or not math.isfinite(now):
            return
        stamp = getattr(getattr(packet, "extensions", None), "abs_send_time", None)
        sequence = getattr(packet, "sequence_number", None)
        source = getattr(packet, "ssrc", None)
        with self._lock:
            if self._fenced:
                return
            if stamp is None:
                self.metrics.count(f"{kind}_absent")
                self._previous.pop(kind, None)
                return
            if (
                type(stamp) is not int
                or not 0 <= stamp < MODULUS
                or type(sequence) is not int
                or not 0 <= sequence < 65536
                or type(source) is not int
                or not 0 <= source < 2**32
            ):
                self.metrics.count(f"{kind}_invalid")
                self._previous.pop(kind, None)
                return
            self.metrics.count(f"{kind}_present")
            previous = self._previous.get(kind)
            self._previous[kind] = (source, sequence, stamp, now)
            if previous is None:
                return
            previous_source, previous_sequence, previous_stamp, previous_now = previous
            # Compare consecutive primary packets only: loss, reordering and
            # SSRC changes must not masquerade as a batching observation.
            if source != previous_source or (sequence - previous_sequence) % 65536 != 1:
                self.metrics.count(f"{kind}_sequence_discontinuity")
                return
            arrival = now - previous_now
            elapsed_ticks = (stamp - previous_stamp) % MODULUS
            if not 0 <= arrival <= MAX_INTERVAL or elapsed_ticks > MAX_INTERVAL * TICKS:
                self.metrics.count(f"{kind}_clock_reset")
                return
            sent = elapsed_ticks / TICKS
            self.metrics.count(f"{kind}_compared")
            self._intervals(kind, "", arrival, sent)
            if arrival >= GAP_SECONDS:
                self.metrics.count(f"{kind}_gap_compared")
                self._intervals(kind, "gap_", arrival, sent)

    def _intervals(self, kind, prefix, arrival, sent):
        for name, seconds in (
            ("arrival_interval", arrival),
            ("send_interval", sent),
            ("arrival_excess", max(0, arrival - sent)),
            ("send_excess", max(0, sent - arrival)),
        ):
            self.metrics.observe(f"{kind}_{prefix}{name}", seconds)

    def snapshot(self):
        with self._lock:
            return None if self._fenced else self.metrics.snapshot()

    def fence(self):
        with self._lock:
            self._fenced = True
            self._previous.clear()


def safe_send_clock(value):
    """Allow only fixed numeric histograms across the status trust boundary."""
    if not isinstance(value, dict):
        return None
    bounds = StreamMetrics().snapshot()["bucket_bounds_us"]
    if value.get("bucket_bounds_us") != bounds or type(value.get("overflow")) is not bool:
        return None

    def number(item):
        if type(item) is not int or not 0 <= item < 2**53:
            raise ValueError
        return item

    try:
        counters = {key: number(value["counters"][key]) for key in COUNTERS}
        timings = {}
        for key in TIMINGS:
            item = value["timings"][key]
            if type(item["buckets"]) is not list or len(item["buckets"]) != len(bounds) + 1:
                return None
            timings[key] = {
                **{field: number(item[field]) for field in ("count", "sum_us", "max_us")},
                "buckets": [number(n) for n in item["buckets"]],
            }
            if sum(timings[key]["buckets"]) != timings[key]["count"] and not value["overflow"]:
                return None
        return {
            "counters": counters,
            "maxima": {},
            "timings": timings,
            "bucket_bounds_us": bounds,
            "overflow": value["overflow"],
        }
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
