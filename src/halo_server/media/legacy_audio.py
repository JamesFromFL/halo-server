"""Bounded, serialized legacy normalization; native objects never close on a loop."""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import time
from collections import deque
from dataclasses import dataclass
from fractions import Fraction

from halo_server.media.audio import AudioNormalizationError, NormalizedOpusPacket
from halo_server.media.formats import MAX_NATIVE_CAMERAS

MAX_NATIVE_OWNERS = MAX_NATIVE_CAMERAS
NATIVE_WORKERS = 2
NATIVE_QUEUE_SECONDS = 1.0


class LegacyInitializationError(AudioNormalizationError):
    pass


class LegacyTimingError(AudioNormalizationError):
    pass


class LegacyQueueError(AudioNormalizationError):
    pass


@dataclass(frozen=True)
class LegacyResult:
    payload: bytes
    channels: int
    rate: int
    packets: tuple
    encode_seconds: float
    discontinuity: bool


class _Scheduler:
    """One queued operation per admitted owner, fixed threads and finite owners."""

    def __init__(self):
        self.lock = threading.Condition()
        self.owners = set()
        self.queue = deque()
        self.workers = []

    @property
    def started(self):
        return len(self.workers) == NATIVE_WORKERS

    def register(self, owner):
        with self.lock:
            if len(self.owners) >= MAX_NATIVE_OWNERS:
                raise LegacyQueueError("Legacy normalization capacity exceeded")
            self.owners.add(owner)

    def release(self, owner):
        with self.lock:
            self.owners.discard(owner)

    def submit(self, owner):
        with self.lock:
            while len(self.workers) < NATIVE_WORKERS:
                thread = threading.Thread(
                    target=self._run,
                    name=f"halo-legacy-audio-{len(self.workers)}",
                    daemon=True,
                )
                try:
                    thread.start()
                except BaseException:
                    # Preserve an actually started thread even if start was
                    # interrupted after creation; retry only missing workers.
                    if thread.is_alive():
                        self.workers.append(thread)
                    raise LegacyQueueError("Legacy native worker startup failed") from None
                self.workers.append(thread)
            # Admission guarantees <=256 owners and <=one queued/running job
            # each. No unbounded executor queue or task-per-source-frame.
            if len(self.queue) >= MAX_NATIVE_OWNERS:
                raise LegacyQueueError("Legacy normalization queue capacity exceeded")
            self.queue.append(owner)
            self.lock.notify()

    def cancel(self, owner):
        with self.lock:
            try:
                self.queue.remove(owner)
            except ValueError:
                return False
            return True

    def _run(self):
        while True:
            with self.lock:
                self.lock.wait_for(lambda: bool(self.queue))
                owner = self.queue.popleft()
            try:
                owner._work()
            finally:
                del owner


_SCHEDULER = _Scheduler()


async def wait_native(future, owner=None):
    """Cancellation retains and joins the actual submitted native operation."""
    result = wrapped = done = _pending = None
    try:
        wrapped = asyncio.wrap_future(future)
        cancelled = False
        if owner is not None:
            try:
                done, _pending = await asyncio.wait({wrapped}, timeout=NATIVE_QUEUE_SECONDS)
                if not done:
                    owner.cancel_queued(future)
            except asyncio.CancelledError:
                cancelled = True
                owner.cancel_queued(future)
        while True:
            try:
                result = await asyncio.shield(wrapped)
                break
            except asyncio.CancelledError:
                cancelled = True
                if wrapped.done():
                    result = wrapped.result()
                    break
            except BaseException:
                if cancelled:
                    raise asyncio.CancelledError from None
                raise
        if cancelled:
            raise asyncio.CancelledError
        return result
    finally:
        # Cancelled Task tracebacks must not retain completed PCM results or
        # Future containers after the real native owner has been joined.
        future = wrapped = result = owner = done = _pending = None


class LegacyAudioOwner:
    """One exact capture's encoder, its caller, and its real retirement proof."""

    def __init__(self, factory, *, retired=lambda _owner: None):
        self.factory = factory
        self._retired_callback = retired
        self._lock = threading.Lock()
        self._encoder = None
        self._job = None
        self._claimed = False
        self._fenced = False
        self._closed = threading.Event()
        self._cleanup_failed = False
        self._expected_pts = None
        self._offset = 0
        self._source_mode = None
        self._active_thread = None
        self._queued_at = None
        self._registered = False

    @property
    def closed(self):
        return self._closed.is_set()

    @property
    def cleanup_failed(self):
        with self._lock:
            return self._cleanup_failed

    @property
    def active_thread(self):
        with self._lock:
            return self._active_thread

    def diagnostics(self):
        with self._lock:
            queued = self._job is not None and self._active_thread is None
            return {
                "claimed": self._claimed,
                "queued": queued,
                "queue_age_ms": (
                    min(2**53 - 1, round(max(0, time.monotonic() - self._queued_at) * 1000))
                    if queued and self._queued_at is not None
                    else None
                ),
                "native_inflight": self._active_thread is not None,
                "initialized": self._encoder is not None,
                "fenced": self._fenced,
                "closed": self.closed,
                "cleanup_failed": self._cleanup_failed,
            }

    def claim(self):
        with self._lock:
            if self._fenced or self._claimed or self._job is not None:
                return False
            self._claimed = True
            return True

    def submit(self, payload=None, channels=None, rate=None, *, frame=None):
        future = concurrent.futures.Future()
        if frame is None and (not isinstance(payload, bytes) or not 0 < len(payload) <= 92160):
            future.set_exception(AudioNormalizationError("Invalid legacy PCM input"))
            return future
        with self._lock:
            if not self._claimed or self._job is not None or self._fenced:
                future.set_exception(AudioNormalizationError("Legacy owner is retired or busy"))
                return future
            if not self._registered:
                try:
                    _SCHEDULER.register(self)
                except AudioNormalizationError:
                    future.set_exception(LegacyQueueError("Legacy normalization capacity exceeded"))
                    return future
                self._registered = True
            self._job = (future, payload, channels, rate, frame)
            self._queued_at = time.monotonic()
        try:
            _SCHEDULER.submit(self)
        except LegacyQueueError:
            with self._lock:
                self._job = None
                self._queued_at = None
            future.set_exception(LegacyQueueError("Legacy native worker admission failed"))
        return future

    def cancel_queued(self, future):
        with self._lock:
            if self._job is None or self._job[0] is not future or self._active_thread is not None:
                return False
            if not _SCHEDULER.cancel(self):
                return False
            self._job = None  # Drop all queued frame/PCM references now.
            self._queued_at = None
        if not future.done():
            future.set_exception(
                LegacyQueueError("Legacy normalization queue timed out or retired")
            )
        self._schedule_retirement()
        return True

    def release(self):
        with self._lock:
            self._claimed = False
        self._schedule_retirement()

    def fence(self):
        with self._lock:
            self._fenced = True
            future = self._job[0] if self._job is not None else None
        if future is not None:
            self.cancel_queued(future)
        self._schedule_retirement()

    def _schedule_retirement(self):
        notify = schedule = False
        with self._lock:
            if self.closed or not self._fenced or self._claimed or self._job is not None:
                return
            if self._encoder is None:
                self._closed.set()
                notify = True
            else:
                self._job = (None, None, None, None, None)
                self._queued_at = time.monotonic()
                schedule = True
        if notify:
            self._notify()
        elif schedule:
            try:
                _SCHEDULER.submit(self)
            except LegacyQueueError:
                with self._lock:
                    self._job = None
                    self._queued_at = None
                    self._cleanup_failed = True

    def _notify(self):
        _SCHEDULER.release(self)
        self._retired_callback(self)

    def wait_closed(self, timeout):
        return self._closed.wait(timeout)

    def _close_encoder(self):
        # Keep the only production native reference here until actual close.
        encoder = self._encoder
        if encoder is not None:
            close = getattr(encoder, "close", None)
            if callable(close):
                close()
            self._encoder = None

    @staticmethod
    def _source_metadata(frame):
        pts, base = getattr(frame, "pts", None), getattr(frame, "time_base", None)
        samples, rate = getattr(frame, "samples", None), getattr(frame, "sample_rate", None)
        if type(pts) is not int or type(samples) is not int or type(rate) is not int:
            raise LegacyTimingError("Decoded source timing unavailable")
        if not 0 < samples <= 23040 or not 8000 <= rate <= 192000 or base is None:
            raise LegacyTimingError("Decoded source timing unavailable")
        timestamp, duration = pts * Fraction(base) * 48000, Fraction(samples * 48000, rate)
        if timestamp.denominator != 1 or duration.denominator != 1:
            raise LegacyTimingError("Decoded source timing unavailable")
        if not 0 < duration <= 5760 or not 0 <= timestamp < timestamp + duration < 2**53:
            raise LegacyTimingError("Decoded source timing unavailable")
        return int(timestamp), int(duration)

    def _encode(self, payload, channels, rate, frame):
        self._check_fence()
        source_pts = duration = None
        if frame is not None:
            from halo_server.media.ring_media import audio_frame_to_s16le_interleaved

            source_pts, duration = self._source_metadata(frame)
            payload, channels, rate, _samples = audio_frame_to_s16le_interleaved(frame)
        self._check_fence()
        mode = source_pts is not None
        if self._source_mode is not None and self._source_mode != mode:
            raise LegacyTimingError("Decoded source timing mode changed")
        if mode and self._expected_pts is not None and source_pts < self._expected_pts:
            raise LegacyTimingError("Decoded source timing moved backwards")
        discontinuity = False
        if mode and self._expected_pts is not None and source_pts != self._expected_pts:
            discontinuity = True
        if discontinuity:
            self._close_encoder()
        self._check_fence()
        if self._encoder is None:
            try:
                self._encoder = self.factory()
            except BaseException as exc:
                raise LegacyInitializationError("Legacy encoder initialization failed") from exc
            self._offset = source_pts if mode else 0
        self._check_fence()
        self._source_mode = mode
        if mode:
            self._expected_pts = source_pts + duration
        began = time.monotonic()
        packets = self._encoder.encode(payload, channels, rate)
        elapsed = time.monotonic() - began
        if (
            not isinstance(packets, (list, tuple))
            or len(packets) > 16
            or any(
                not isinstance(packet.payload, bytes) or not 0 < len(packet.payload) <= 1275
                for packet in packets
            )
        ):
            raise AudioNormalizationError("Legacy encoder output exceeded capacity")
        if mode:
            packets = tuple(
                NormalizedOpusPacket(packet.payload, packet.rtp_timestamp + self._offset)
                for packet in packets
            )
        return LegacyResult(payload, channels, rate, tuple(packets), elapsed, discontinuity)

    def _check_fence(self):
        with self._lock:
            if self._fenced:
                raise AudioNormalizationError("Legacy owner retired")

    def _work(self):
        with self._lock:
            job = self._job
            self._active_thread = threading.get_ident()
            fenced = self._fenced
        future, payload, channels, rate, frame = job
        error = result = None
        try:
            if future is None:
                self._close_encoder()
            elif not fenced:
                result = self._encode(payload, channels, rate, frame)
        except BaseException as exc:
            # Native tracebacks can retain a whole PCM/frame or caller closure.
            # Only a fixed typed result crosses back to the loop.
            kind = (
                LegacyInitializationError
                if isinstance(exc, LegacyInitializationError)
                else LegacyTimingError
                if isinstance(exc, LegacyTimingError)
                else AudioNormalizationError
            )
            error = kind("Legacy native operation failed")
        finally:
            job = payload = frame = None
            with self._lock:
                self._job = None
                self._queued_at = None
                self._active_thread = None
                if future is None:
                    self._cleanup_failed = error is not None
                    if error is None:
                        self._closed.set()
            if future is None:
                if error is None:
                    self._notify()
            else:
                if future.done():
                    pass
                elif error is None:
                    future.set_result(result)
                else:
                    future.set_exception(error)
                self._schedule_retirement()
