"""Pinned per-receiver routed-RTP observations, never packets or media policy."""

from __future__ import annotations

import contextlib
import inspect
import math
import threading
import time

import aiortc

from halo_server.media.telemetry import StreamMetrics

PERIOD_MS = 100
CAPACITY = 64
_LIMIT = 2**53 - 1
_KINDS = ("audio", "video")
_FIELDS = ("audio_rtp", "video_rtp", "audio_frames", "video_frames")


class ReceiverMetricsError(RuntimeError):
    """The pinned observation boundary cannot be installed safely."""


def _safe(callback, *args):
    with contextlib.suppress(BaseException):
        return callback(*args)
    return None


class ReceiverPipeline:
    def __init__(self, audio_receiver, video_receiver, *, clock=time.monotonic):
        self.metrics = StreamMetrics(
            counters=("audio_rtp", "video_rtp", "audio_frames", "video_frames"),
            timings=(
                "audio_rtp_gap",
                "video_rtp_gap",
                "audio_frame_gap",
                "video_frame_gap",
                "audio_handler",
                "video_handler",
                "wakeup_lag",
            ),
        )
        self._clock = clock
        self._started = clock()
        self._lock = threading.Lock()
        self._fenced = False
        self._receivers = (audio_receiver, video_receiver)
        self._bins = [None] * CAPACITY
        self._previous = {}

    def _now(self):
        with contextlib.suppress(BaseException):
            now = self._clock()
            return now if type(now) in (int, float) and math.isfinite(now) else None
        return None

    def _metric(self, method, *args):
        with contextlib.suppress(BaseException):
            getattr(self.metrics, method)(*args)

    def _tick(self, now):
        elapsed = max(0, now - self._started)
        if elapsed >= _LIMIT / 1000:
            return _LIMIT // PERIOD_MS
        return round(elapsed * 1_000_000) // 100_000

    def _elapsed_ms(self, now):
        elapsed = max(0, now - self._started)
        return _LIMIT if elapsed >= _LIMIT / 1000 else round(elapsed * 1000)

    def _record(self, kind, category, now):
        if kind not in _KINDS or now is None:
            return
        name = f"{kind}_{category}"
        tick = self._tick(now)
        with self._lock:
            if self._fenced:
                return
            item = self._bins[tick % CAPACITY]
            if item is None or item[0] != tick:
                item = self._bins[tick % CAPACITY] = (tick, dict.fromkeys(_FIELDS, 0))
            item[1][name] = min(_LIMIT, item[1][name] + 1)
            previous = self._previous.get(name)
            self._previous[name] = now
        self._metric("count", name)
        if previous is not None:
            self._metric(
                "observe",
                f"{kind}_{'rtp' if category == 'rtp' else 'frame'}_gap",
                max(0, now - previous),
            )

    def _entry(self, kind):
        now = self._now()
        self._record(kind, "rtp", now)
        return now

    def _finish(self, kind, entered):
        now = self._now()
        with self._lock:
            active = not self._fenced
        if active and now is not None and entered is not None:
            self._metric("observe", f"{kind}_handler", max(0, now - entered))

    def frame(self, kind, entered=None):
        # The optional encoded-ingress timestamp uses the same monotonic axis.
        _safe(self._record, kind, "frames", self._now() if entered is None else entered)

    def wake_lag(self, seconds):
        with self._lock:
            active = not self._fenced
        if active:
            self._metric("observe", "wakeup_lag", seconds)

    def snapshot(self):
        now = self._now()
        if now is None:
            return None
        tick = self._tick(now)
        with self._lock:
            if self._fenced:
                return None
            bins = []
            for index in range(max(0, tick - CAPACITY + 1), tick + 1):
                item = self._bins[index % CAPACITY]
                counts = (
                    dict(item[1])
                    if item is not None and item[0] == index
                    else dict.fromkeys(_FIELDS, 0)
                )
                bins.append({"elapsed_ms": index * PERIOD_MS, **counts})
            receivers = self._receivers
        shared = None
        with contextlib.suppress(BaseException):
            audio, video = (getattr(receiver, "transport", None) for receiver in receivers)
            if audio is not None and video is not None:
                shared = audio is video
        metrics = self.metrics.snapshot()
        with self._lock:
            if self._fenced:
                return None
        return {
            "period_ms": PERIOD_MS,
            "capacity": CAPACITY,
            "end_elapsed_ms": self._elapsed_ms(now),
            "bins": bins,
            "shared_dtls": shared,
            "metrics": metrics,
        }

    def fence(self):
        with self._lock:
            self._fenced = True
            self._receivers = ()
            self._previous.clear()


def install_rtp_observer(receiver, kind, pipeline):
    """Wrap one instance before receive; preserve return, failure and cancellation."""
    handler = getattr(type(receiver), "_handle_rtp_packet", None)
    code = getattr(handler, "__code__", None)
    original = getattr(receiver, "_handle_rtp_packet", None)
    required = {"_RTCRtpReceiver__jitter_buffer", "add", "_RTCRtpReceiver__decoder_queue"}
    if (
        str(getattr(aiortc, "__version__", "")) != "1.15.0"
        or kind not in _KINDS
        or not isinstance(pipeline, ReceiverPipeline)
        or not inspect.iscoroutinefunction(handler)
        or not callable(original)
        or code is None
        or code.co_argcount != 3
        or code.co_varnames[:3] != ("self", "packet", "arrival_time_ms")
        or not required.issubset(code.co_names)
        or "_handle_rtp_packet" in receiver.__dict__
    ):
        raise ReceiverMetricsError("Routed RTP observation contract changed")

    async def observed(packet, arrival_time_ms):
        entered = _safe(pipeline._entry, kind)
        try:
            return await original(packet, arrival_time_ms)
        finally:
            _safe(pipeline._finish, kind, entered)

    receiver._handle_rtp_packet = observed
