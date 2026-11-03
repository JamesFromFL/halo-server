"""Bounded, view-owned source/presentation/RTP correlation; no media or I/O."""

from __future__ import annotations

import math
import secrets
import time
from dataclasses import dataclass, field

_NS = 1_000_000_000
_NTP = 1 << 32
_LIMIT = (1 << 53) - 1
_SR_AGE_NS = 30 * _NS
_PAIR_ERROR_NS = 2 * _NS
_INITIAL_ERROR_NS = 5 * _NS
_COUNTERS = (
    "accepted",
    "duplicate",
    "reordered",
    "invalid",
    "wrong_epoch",
    "identity_changed",
    "incoherent",
    "late_ignored",
)


class TimelineError(ValueError):
    """A fixed error code, never media, source identities or raw clock values."""


def _integer(value, low=-_LIMIT, high=_LIMIT):
    return type(value) is int and low <= value <= high


def _ns(value):
    if type(value) not in {int, float} or not math.isfinite(value) or not 0 <= value <= _LIMIT:
        raise TimelineError("invalid_time")
    return round(value * _NS)


def _rescale(value, numerator, denominator):
    # Nearest integer; exact ties choose the lower integer, including negatives.
    result, remainder = divmod(value * numerator, denominator)
    return result + (2 * remainder > denominator)


@dataclass(frozen=True)
class ReportInstant:
    monotonic_ns: int
    wall_ns: int
    _owner: object = field(repr=False)


@dataclass(frozen=True)
class ReportClock:
    rtp_timestamp: int
    wall_time: float


@dataclass(frozen=True)
class _Clock:
    pts: int
    ntp: int
    received: int
    source: int


@dataclass
class _Track:
    source_rate: int
    output_rate: int
    origin: int
    first_pts: int | None = None
    first_receipt: int | None = None
    frozen: _Clock | None = None
    latest: _Clock | None = None
    time_origin: int | None = None
    last_sent_time: int | None = None
    sent_epoch: int | None = None
    counters: dict = field(default_factory=lambda: dict.fromkeys(_COUNTERS, 0))
    residual_max: int = 0
    rejected_clock: bool = False

    def count(self, name):
        self.counters[name] = min(_LIMIT, self.counters[name] + 1)


class LiveTimeline:
    """One asyncio-owner clock, with fixed-size metadata for at most two tracks.

    Input observation precedes conversion. Mode freezes on the first mapping,
    not on construction or the first observation, and never promotes mid-epoch.
    Receipts passed here remain untouched; this owner does not grant freshness.
    """

    def __init__(
        self,
        *,
        audio_rate: int | None,
        video_origin: int,
        audio_origin: int | None = None,
        clock=time.monotonic,
        wall_clock=time.time,
    ):
        if not _integer(video_origin, 0, 0xFFFFFFFF):
            raise TimelineError("invalid_origin")
        if audio_rate is not None and (
            type(audio_rate) is not int
            or audio_rate not in {8000, 16000, 24000}
            or not _integer(audio_origin, 0, 0xFFFFFFFF)
        ):
            raise TimelineError("invalid_audio_clock")
        if audio_rate is None and audio_origin is not None:
            raise TimelineError("invalid_audio_clock")
        self._clock = clock
        before, wall, after = _ns(clock()), _ns(wall_clock()), _ns(clock())
        if after < before:
            raise TimelineError("monotonic_regressed")
        self._mono, self._wall = (before + after) // 2, wall
        self._sample_span = after - before
        self._token = object()
        # Shared by this view's audio/video SRs, stable through reconfigure and
        # source recovery. Never derived from a device/account or put in logs.
        self.cname = secrets.token_hex(16).encode("ascii")
        self._tracks = {"video": _Track(90000, 90000, video_origin)}
        if audio_rate is not None:
            self._tracks["audio"] = _Track(48000, audio_rate, audio_origin)
        self._epoch = None
        self._mode = "pending"
        self._reference = None
        self._epoch_changes = 0

    @property
    def mode(self):
        return self._mode

    @property
    def capture_epoch(self):
        return self._epoch

    def _track(self, kind, epoch):
        if kind not in self._tracks:
            raise TimelineError("disabled_track")
        if not _integer(epoch, 0) or epoch != self._epoch:
            raise TimelineError("stale_epoch")
        return self._tracks[kind]

    def observe_input(self, kind, *, capture_epoch, source_pts, received_at, source_clocks=()):
        if kind not in self._tracks or not _integer(capture_epoch, 0) or not _integer(source_pts):
            raise TimelineError("invalid_input")
        received, now = _ns(received_at), _ns(self._clock())
        if received > now or not isinstance(source_clocks, tuple) or len(source_clocks) > 2:
            raise TimelineError("invalid_input")
        if self._epoch is not None and capture_epoch < self._epoch:
            raise TimelineError("stale_epoch")
        if capture_epoch != self._epoch:
            self._epoch_changes = min(_LIMIT, self._epoch_changes + (self._epoch is not None))
            self._epoch, self._mode, self._reference = capture_epoch, "pending", None
            for track in self._tracks.values():
                track.first_pts = track.first_receipt = None
                track.frozen = track.latest = None
                track.rejected_clock = False
        track = self._tracks[kind]
        if track.first_pts is None:
            track.first_pts, track.first_receipt = source_pts, received
        for observation in source_clocks:
            self._observe_clock(observation, now)

    def _observe_clock(self, observation, now):
        kind = getattr(observation, "kind", None)
        if kind not in self._tracks:
            return
        track = self._tracks[kind]
        epoch = getattr(observation, "capture_epoch", None)
        if not _integer(epoch, 0):
            track.count("invalid")
            track.rejected_clock = True
            return
        if epoch != self._epoch:
            track.count("wrong_epoch")
            return
        pts, ntp = (
            getattr(observation, "rtp_timestamp", None),
            getattr(observation, "sender_ntp", None),
        )
        source = getattr(observation, "ssrc", None)
        try:
            received = _ns(getattr(observation, "received_at", None))
        except TimelineError:
            track.count("invalid")
            track.rejected_clock = True
            return
        if not (
            _integer(pts)
            and _integer(ntp, 1, (1 << 64) - 1)
            and _integer(source, 1, 0xFFFFFFFF)
            and 0 <= now - received <= _SR_AGE_NS
        ):
            track.count("invalid")
            track.rejected_clock = True
            return
        candidate = _Clock(pts, ntp, received, source)
        old = track.latest
        if candidate == old:
            return  # Repeated snapshots are not new SR observations.
        if old is not None:
            if source != old.source:
                track.count("identity_changed")
                track.rejected_clock = True
                return
            if (pts, ntp) == (old.pts, old.ntp):
                track.count("duplicate")
                return
            if pts <= old.pts or ntp <= old.ntp or received < old.received:
                track.count("reordered")
                track.rejected_clock = True
                return
            media_delta = _rescale(pts - old.pts, _NS, track.source_rate)
            clock_delta = _rescale(ntp - old.ntp, _NS, _NTP)
            residual = abs(media_delta - clock_delta)
            track.residual_max = min(_LIMIT, max(track.residual_max, residual))
            # A guard, not a drift-correction or clock-quality guarantee.
            if residual > min(250_000_000, 50_000_000 + clock_delta // 500):
                track.count("incoherent")
                track.rejected_clock = True
                return
            if track.frozen is not None:
                frozen = track.frozen
                elapsed = _rescale(ntp - frozen.ntp, _NS, _NTP)
                total_residual = abs(_rescale(pts - frozen.pts, _NS, track.source_rate) - elapsed)
                track.residual_max = min(_LIMIT, max(track.residual_max, total_residual))
                if total_residual > min(250_000_000, 50_000_000 + elapsed // 500):
                    track.count("incoherent")
                    track.rejected_clock = True
                    return
        track.latest = candidate
        track.rejected_clock = False
        track.count("accepted")
        if self._mode != "pending":
            track.count("late_ignored")

    def _freeze(self):
        if self._mode != "pending":
            return
        candidates = [track.latest for track in self._tracks.values()]
        now = _ns(self._clock())
        ready = all(
            item is not None and 0 <= now - item.received <= _SR_AGE_NS for item in candidates
        ) and all(track.first_pts is not None for track in self._tracks.values())
        if ready:
            first = min(candidates, key=lambda item: item.received)
            ready = all(
                abs(_rescale(item.ntp - first.ntp, _NS, _NTP) - (item.received - first.received))
                <= _PAIR_ERROR_NS
                for item in candidates
            )
            for track in self._tracks.values():
                ready &= abs(track.first_pts - track.latest.pts) <= track.source_rate * 60
                initial = (
                    first.received
                    + _rescale(track.latest.ntp - first.ntp, _NS, _NTP)
                    + _rescale(track.first_pts - track.latest.pts, _NS, track.source_rate)
                )
                ready &= abs(initial - track.first_receipt) <= _INITIAL_ERROR_NS
        if ready:
            self._mode, self._reference = "source_sender_report", first
            for track in self._tracks.values():
                track.frozen = track.latest
        else:
            self._mode = "receiver_estimated"

    def _presentation_ns(self, track, pts):
        if not _integer(pts) or track.first_pts is None:
            raise TimelineError("unobserved_track")
        self._freeze()
        if self._mode == "source_sender_report":
            anchor, reference = track.frozen, self._reference
            return (
                reference.received
                + _rescale(anchor.ntp - reference.ntp, _NS, _NTP)
                + _rescale(pts - anchor.pts, _NS, track.source_rate)
            )
        return track.first_receipt + _rescale(pts - track.first_pts, _NS, track.source_rate)

    def presentation(self, kind, *, capture_epoch, source_pts):
        return self._presentation_ns(self._track(kind, capture_epoch), source_pts) / _NS

    def _wire(self, track, pts):
        presentation = self._presentation_ns(track, pts)
        if track.time_origin is None:
            track.time_origin = self._presentation_ns(track, track.first_pts)
        if track.last_sent_time is not None and presentation < track.last_sent_time:
            raise TimelineError("output_regressed")
        return (
            track.origin + _rescale(presentation - track.time_origin, track.output_rate, _NS)
        ) & 0xFFFFFFFF

    def rtp_timestamp(self, kind, *, capture_epoch, source_pts):
        return self._wire(self._track(kind, capture_epoch), source_pts)

    def sent(self, kind, *, capture_epoch, source_pts):
        track = self._track(kind, capture_epoch)
        self._wire(track, source_pts)
        track.last_sent_time = self._presentation_ns(track, source_pts)
        track.sent_epoch = capture_epoch

    def instant(self):
        now = _ns(self._clock())
        if now < self._mono:
            raise TimelineError("monotonic_regressed")
        return ReportInstant(now, self._wall + now - self._mono, self._token)

    def report(self, kind, *, capture_epoch, instant):
        track = self._track(kind, capture_epoch)
        if not isinstance(instant, ReportInstant) or instant._owner is not self._token:
            raise TimelineError("invalid_instant")
        if track.time_origin is None or track.sent_epoch != capture_epoch:
            return None
        rtp = track.origin + _rescale(
            instant.monotonic_ns - track.time_origin, track.output_rate, _NS
        )
        return ReportClock(rtp & 0xFFFFFFFF, instant.wall_ns / _NS)

    def snapshot(self):
        now = _ns(self._clock())
        tracks = {}
        for kind, track in self._tracks.items():
            age = None if track.latest is None else max(0, now - track.latest.received)
            confidence = "unobserved" if track.first_pts is None else "estimated"
            if self._mode == "source_sender_report":
                confidence = (
                    "source_recent" if age is not None and age <= _SR_AGE_NS else "source_stale"
                )
                if confidence == "source_recent" and track.rejected_clock:
                    confidence = "source_held_over"
            tracks[kind] = {
                "initialized": track.first_pts is not None,
                "confidence": confidence,
                "sr_age_ms": None if age is None else min(3_600_000, age // 1_000_000),
                "sr_residual_us_max": track.residual_max // 1000,
                "counters": dict(track.counters),
            }
        return {
            "mode": self._mode,
            "epoch_changes": self._epoch_changes,
            "clock_sample_span_us": self._sample_span // 1000,
            "tracks": tracks,
        }
