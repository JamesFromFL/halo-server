"""Finite per-receiver legacy PCM handoff for the pinned aiortc decoder.

The original encoded Opus observer is independent. Only this optional decoded
lane drops work under pressure; it never blocks the Ring loop on queue space.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from fractions import Fraction

import aiortc
from av import AudioFrame

from halo_server.media.opus import MAX_OPUS_PACKET, MAX_OPUS_SAMPLES, parse_opus

_PINNED_VERSION = "1.15.0"
_THREAD_ATTRIBUTE = "_RTCRtpReceiver__decoder_thread"
_COUNTER_MAX = 2**53 - 1
MAX_INPUT_PACKETS = 4
MAX_PCM_FRAMES = 64
MAX_PCM_SAMPLES = 48_000
MAX_PCM_BYTES = 256 * 1024
MAX_FRAME_BYTES = MAX_OPUS_SAMPLES * 4 + 512
# One legal120ms packet can contain48 real frames. Charge plane alignment for
# every one, plus a previous decoder-loop local which may survive its next get.
_NATIVE_FRAMES = 48
_NATIVE_BYTES = MAX_OPUS_SAMPLES * 4 + _NATIVE_FRAMES * 512
# Current conversion may own ndarray/raw/native-frame copies, finite normalized
# results, and fixed FIFO/lookahead. This is a reservation, not an RSS claim.
_CONVERSION_BYTES = 128 * 1024


class DecodedAudioError(RuntimeError):
    """The isolated legacy lane no longer matches its validated contract."""


@dataclass(eq=False)
class _Input:
    item: object = field(repr=False)
    size: int
    samples: int


@dataclass(eq=False)
class _Pcm:
    frame: AudioFrame = field(repr=False)
    size: int
    samples: int


class _DecoderInput(queue.Queue):
    def __init__(self, lane):
        super().__init__()
        self._lane = lane

    def put(self, item, block=True, timeout=None):
        # The receive loop must never wait for a legacy decoder. Original Opus
        # observation has already happened before this bounded forwarding sink.
        self._lane._put_input(item)

    def put_nowait(self, item):
        self.put(item, block=False)

    def get(self, block=True, timeout=None):
        return self._lane._get_input(block=block, timeout=timeout)

    def get_nowait(self):
        return self.get(block=False)

    def qsize(self):
        with self._lane._condition:
            return len(self._lane._inputs) + int(
                self._lane._closed and not self._lane._terminal_consumed
            )

    def empty(self):
        return self.qsize() == 0

    def full(self):
        with self._lane._condition:
            return len(self._lane._inputs) >= MAX_INPUT_PACKETS


class _DecodedOutput:
    def __init__(self, lane):
        self._lane = lane

    def put(self, frame):
        # An ordinary method is essential: aiortc calls this on its decoder
        # thread BEFORE run_coroutine_threadsafe. Reserving only inside an
        # async put would permit unlimited scheduled coroutines retaining PCM.
        return self._lane._put_frame(frame)

    async def get(self):
        return await self._lane._get_frame()

    def qsize(self):
        with self._lane._condition:
            return len(self._lane._frames)

    def empty(self):
        return self.qsize() == 0


class DecodedAudioLane:
    """One decoder thread, one notified handoff and one retained PCM consumer."""

    def __init__(self, loop=None, on_discontinuity=None):
        if str(getattr(aiortc, "__version__", "unknown")) != _PINNED_VERSION:
            raise DecodedAudioError("unsupported_aiortc")
        if loop is None:
            with contextlib.suppress(RuntimeError):
                loop = asyncio.get_running_loop()
        self._loop, self._on_discontinuity = loop, on_discontinuity
        self._condition = threading.Condition(threading.RLock())
        self._available = asyncio.Event()
        self._inputs, self._frames = deque(), deque()
        self._current_input = self._inflight = None
        self._owned_bytes = self._owned_samples = 0
        self._notification_pending = self._wake_pending = False
        self._closed = self._terminal_consumed = self._native_retired = False
        self._decoder_seen = False
        self._revision = 0
        self._counters = dict.fromkeys(
            (
                "input_accepted",
                "input_dropped",
                "input_dropped_samples",
                "input_invalid",
                "pcm_accepted",
                "pcm_dropped",
                "pcm_dropped_samples",
                "pcm_invalid",
                "pcm_released",
                "notification_waits",
            ),
            0,
        )
        self._maxima = dict.fromkeys(("frames", "bytes", "samples", "input_packets"), 0)
        self.input, self.output = _DecoderInput(self), _DecodedOutput(self)

    @property
    def revision(self):
        with self._condition:
            return self._revision

    def _count(self, name, amount=1):
        self._counters[name] = min(_COUNTER_MAX, self._counters[name] + amount)

    def _changed(self):
        self._revision = min(_COUNTER_MAX, self._revision + 1)

    def _observe_change(self):
        if self._on_discontinuity is not None:
            with contextlib.suppress(BaseException):
                self._on_discontinuity()

    def _budget(self):
        count = len(self._frames) + int(self._inflight is not None)
        size, samples = self._owned_bytes, self._owned_samples
        if not self._native_retired:
            count += _NATIVE_FRAMES + 1
            size += _NATIVE_BYTES + MAX_FRAME_BYTES
            samples += MAX_OPUS_SAMPLES * 2
        if not self._native_retired or self._inflight is not None:
            size += _CONVERSION_BYTES
        return count, size, samples

    def _fits(self, node):
        count, size, samples = self._budget()
        return (
            count + 1 <= MAX_PCM_FRAMES
            and size + node.size <= MAX_PCM_BYTES
            and samples + node.samples <= MAX_PCM_SAMPLES
        )

    def _record_peak(self):
        count, size, samples = self._budget()
        for key, value in (("frames", count), ("bytes", size), ("samples", samples)):
            self._maxima[key] = max(self._maxima[key], value)
        self._maxima["input_packets"] = max(self._maxima["input_packets"], len(self._inputs))

    def _drop_frame(self, node):
        self._owned_bytes -= node.size
        self._owned_samples -= node.samples
        self._count("pcm_dropped")
        self._count("pcm_dropped_samples", node.samples)
        self._changed()

    def _put_input(self, item):
        if item is None:
            self.fence()
            return
        try:
            if not isinstance(item, tuple) or len(item) != 2:
                raise ValueError
            codec, encoded = item
            if str(getattr(codec, "name", "")).lower() != "opus":
                raise ValueError
            if not isinstance(encoded.data, bytes) or len(encoded.data) > MAX_OPUS_PACKET:
                raise ValueError
            parsed = parse_opus(encoded.data)
            node = _Input(item, len(encoded.data), parsed.duration_samples)
        except Exception:
            with self._condition:
                self._count("input_invalid")
                self._changed()
            self._observe_change()
            return
        changed = False
        with self._condition:
            if self._closed:
                return
            if len(self._inputs) >= MAX_INPUT_PACKETS:
                old = self._inputs.popleft()
                self._count("input_dropped")
                self._count("input_dropped_samples", old.samples)
                self._changed()
                changed = True
            self._inputs.append(node)
            self._count("input_accepted")
            self._record_peak()
            self._condition.notify_all()
        if changed:
            self._observe_change()

    def _get_input(self, *, block=True, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            # A new get is the pinned worker's actual previous-batch boundary,
            # not the first output frame. Keep its final decoder-local reserve.
            while not self._closed and (not self._inputs or self._notification_pending):
                if not block:
                    raise queue.Empty
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise queue.Empty
                self._condition.wait(remaining)
            if self._closed:
                self._terminal_consumed = True
                return None
            self._current_input = self._inputs.popleft()
            self._decoder_seen = True
            return self._current_input.item

    @staticmethod
    def _pcm(frame):
        if (
            not isinstance(frame, AudioFrame)
            or frame.sample_rate != 48000
            or frame.format.name != "s16"
            or len(frame.layout.channels) not in {1, 2}
            or not 0 < frame.samples <= MAX_OPUS_SAMPLES
            or len(frame.planes) != 1
            or frame.planes[0].buffer_size > MAX_FRAME_BYTES
            or type(frame.pts) is not int
            or frame.time_base is None
        ):
            raise ValueError
        timestamp = Fraction(frame.pts) * frame.time_base * 48000
        if timestamp.denominator != 1 or not -(2**53) < timestamp < 2**53:
            raise ValueError
        return _Pcm(frame, frame.planes[0].buffer_size, frame.samples)

    def _put_frame(self, frame):
        if frame is None:
            self.fence()
            return self._noop()
        with self._condition:
            while self._notification_pending and not self._closed:
                try:
                    current = asyncio.get_running_loop()
                except RuntimeError:
                    current = None
                if current is not None and current is self._loop:
                    raise DecodedAudioError("notification_not_awaited")
                self._count("notification_waits")
                self._condition.wait()
            if self._closed:
                # Only the finite current native batch can reach here; input
                # quiescence prevents another decode. No PCM enters a coroutine.
                return self._noop()
            self._notification_pending = True
        try:
            node = self._pcm(frame)
        except Exception:
            with self._condition:
                self._count("pcm_invalid")
                self._changed()
            self._observe_change()
            return self._notify()
        changed = False
        with self._condition:
            if self._closed:
                return self._notify()
            if (self._inflight is not None and self._inflight.frame is frame) or any(
                old.frame is frame for old in self._frames
            ):
                self._count("pcm_invalid")
                self._changed()
                return self._notify()
            while self._frames and not self._fits(node):
                self._drop_frame(self._frames.popleft())
                changed = True
            if not self._fits(node):
                # The fixed reserves permit any legal maximum frame alongside
                # one maximum in-flight frame. Never evict that active owner.
                self._count("pcm_invalid")
                self._changed()
                return self._notify()
            self._frames.append(node)
            self._owned_bytes += node.size
            self._owned_samples += node.samples
            self._count("pcm_accepted")
            self._record_peak()
        if changed:
            self._observe_change()
        # This coroutine captures only the lane, not a frame/array/raw payload.
        return self._notify()

    async def _noop(self):
        return None

    async def _notify(self):
        loop = asyncio.get_running_loop()
        with self._condition:
            if self._loop is None:
                self._loop = loop
            if self._loop is not loop:
                self.fence()
                raise DecodedAudioError("loop_changed")
            self._available.set()
            self._notification_pending = False
            self._condition.notify_all()

    async def _get_frame(self):
        loop = asyncio.get_running_loop()
        while True:
            with self._condition:
                if self._loop is None:
                    self._loop = loop
                if self._loop is not loop:
                    raise DecodedAudioError("loop_changed")
                if self._inflight is not None:
                    raise DecodedAudioError("consumer_busy")
                if self._frames:
                    self._inflight = self._frames.popleft()
                    return self._inflight.frame
                if self._closed:
                    return None
                self._available.clear()
            await self._available.wait()

    def release(self, frame):
        with self._condition:
            if self._inflight is None or self._inflight.frame is not frame:
                raise DecodedAudioError("unknown_frame")
            self._owned_bytes -= self._inflight.size
            self._owned_samples -= self._inflight.samples
            self._inflight = None
            self._count("pcm_released")
            self._condition.notify_all()

    def attach_track(self, track, receiver=None):
        if receiver is not None and not hasattr(receiver, _THREAD_ATTRIBUTE):
            raise DecodedAudioError("thread_contract_changed")
        if receiver is not None and getattr(receiver, _THREAD_ATTRIBUTE, None) is not None:
            raise DecodedAudioError("decoder_already_started")
        if receiver is not None and hasattr(receiver, "_track") and receiver._track is not track:
            raise DecodedAudioError("track_contract_changed")
        current = getattr(track, "_queue", None)
        if current is self.output:
            return
        if (
            getattr(track, "kind", None) != "audio"
            or not isinstance(current, asyncio.Queue)
            or not current.empty()
        ):
            raise DecodedAudioError("track_contract_changed")
        track._queue = self.output

    def _wake(self):
        with self._condition:
            self._wake_pending = False
            self._available.set()

    def fence(self):
        with self._condition:
            self._closed = True
            while self._frames:
                self._drop_frame(self._frames.popleft())
            while self._inputs:
                node = self._inputs.popleft()
                self._count("input_dropped")
                self._count("input_dropped_samples", node.samples)
            self._condition.notify_all()
            if self._loop is not None and not self._wake_pending:
                self._wake_pending = True
                try:
                    self._loop.call_soon_threadsafe(self._wake)
                except RuntimeError:
                    self._wake_pending = False

    async def retire(self, receiver, timeout=2.0):
        """Timeout/cancellation leaves all native/consumer claims retained."""
        self.fence()
        if type(timeout) not in {int, float} or not math.isfinite(timeout) or timeout < 0:
            raise DecodedAudioError("invalid_retirement_timeout")
        if (receiver is not None and not hasattr(receiver, _THREAD_ATTRIBUTE)) or (
            receiver is None and self._decoder_seen
        ):
            raise DecodedAudioError("thread_contract_changed")
        thread = getattr(receiver, _THREAD_ATTRIBUTE, None)
        if thread is not None and not isinstance(thread, threading.Thread):
            raise DecodedAudioError("thread_contract_changed")
        if thread is threading.current_thread():
            raise DecodedAudioError("decoder_self_join")
        deadline = asyncio.get_running_loop().time() + timeout
        while thread is not None and thread.is_alive():
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError("decoded_audio_cleanup_pending")
            await asyncio.sleep(0.005)
        with self._condition:
            self._native_retired = True
            self._current_input = None
        return True

    def snapshot(self):
        with self._condition:
            frames, size, samples = self._budget()
            current = self._current_input
            # A decoder-local encoded item can survive entry to the next get;
            # retain a fixed maximum allowance until actual thread retirement.
            old = int(self._decoder_seen and not self._native_retired)
            return {
                "revision": self._revision,
                "closed": self._closed,
                "native_retired": self._native_retired,
                "queued_frames": len(self._frames),
                "inflight_frames": int(self._inflight is not None),
                "reserved_frames": frames,
                "reserved_bytes": size,
                "reserved_samples": samples,
                "input_queued": len(self._inputs),
                "input_reserved_packets": len(self._inputs) + int(current is not None) + old,
                "input_reserved_bytes": sum(node.size for node in self._inputs)
                + (current.size if current else 0)
                + old * MAX_OPUS_PACKET,
                "input_reserved_samples": sum(node.samples for node in self._inputs)
                + (current.samples if current else 0)
                + old * MAX_OPUS_SAMPLES,
                "notification_pending": self._notification_pending,
                "counters": dict(self._counters),
                "maxima": dict(self._maxima),
            }
