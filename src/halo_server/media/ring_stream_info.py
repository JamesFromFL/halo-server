"""Content-free, attempt-owned observation of the pinned Ring signal reader."""

from __future__ import annotations

import asyncio
import inspect
import json
import math
import time
from importlib import metadata

MAX_MESSAGE = 4096
MAX_COUNTER = 2**53 - 1
POLL_SECONDS = 0.01
DEFAULT_TIMEOUT = 20.0


def _clock_value(clock):
    value = clock()
    return value if type(value) in {int, float} and math.isfinite(value) and value >= 0 else None


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate signal field")
        result[key] = value
    return result


def _stream_type():
    from ring_doorbell.webrtcstream import RingWebRtcStream

    method = RingWebRtcStream.handle_message
    code = getattr(method, "__code__", None)
    if (
        metadata.version("ring-doorbell") != "0.9.14"
        or not inspect.iscoroutinefunction(method)
        or getattr(code, "co_varnames", ())[:2] != ("self", "message_str")
        or not {"json_loads", "handle_answer_message", "handle_ice_message"}.issubset(
            set(getattr(code, "co_names", ()))
        )
    ):
        return None
    return RingWebRtcStream


class RingStreamInfo:
    """Observe one SDK instance without changing signaling or media policy.

    The caller retains and joins ``install`` with its existing receive attempt.
    No original message, parsed body, identifier or exception is stored here.
    """

    def __init__(self, device, session_id, *, current, clock=time.monotonic):
        self._device, self._session_id = device, session_id
        self._current, self._clock = current, clock
        self._closed = self._attempted = False
        self._registry = self._stream = self._wrapper = None
        self._state = "absent"
        self._before_reader = None
        self._transcoding = self._reason = self._received_at = None
        self._observations = self._invalid = self._oversized = 0

    def _admitted(self):
        try:
            return not self._closed and self._current() is True
        except BaseException:
            return False

    def _owned(self):
        try:
            return (
                self._admitted()
                and getattr(self._device, "_webrtc_streams", None) is self._registry
                and self._registry.get(self._session_id) is self._stream
                and self._stream.is_alive is True
                and self._stream.__dict__.get("handle_message") is self._wrapper
            )
        except BaseException:
            return False

    async def install(self, *, timeout=DEFAULT_TIMEOUT):
        if type(timeout) not in {int, float} or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Signal observation timeout must be finite and positive")
        if self._attempted:
            return self._wrapper is not None and not self._closed
        self._attempted = True
        try:
            stream_type = _stream_type()
            registry = getattr(self._device, "_webrtc_streams", None)
            if stream_type is None or type(registry) is not dict:
                self._state = "unavailable"
                return False
            async with asyncio.timeout(timeout):
                while self._admitted():
                    if getattr(self._device, "_webrtc_streams", None) is not registry:
                        self._state = "unavailable"
                        return False
                    stream = registry.get(self._session_id)
                    if stream is not None:
                        if type(stream) is not stream_type or "handle_message" in stream.__dict__:
                            self._state = "unavailable"
                            return False
                        if stream.is_alive is not True:
                            return False
                        reader = stream.read_task
                        if reader is None:
                            before = True
                        elif isinstance(reader, asyncio.Task):
                            before = (
                                inspect.getcoroutinestate(reader.get_coro()) == inspect.CORO_CREATED
                            )
                        else:
                            self._state = "unavailable"
                            return False
                        original = stream.handle_message
                        self._registry, self._stream = registry, stream

                        async def observed(message, _original=original):
                            try:
                                result = await _original(message)
                                # Original exception/cancellation/return semantics
                                # are untouched; observation cannot fail signaling.
                                try:
                                    if self._owned():
                                        self._observe(message)
                                except BaseException:
                                    pass
                                return result
                            finally:
                                message = None

                        self._wrapper = observed
                        stream.handle_message = observed
                        self._before_reader, self._state = before, "not_seen"
                        return True
                    await asyncio.sleep(POLL_SECONDS)
            return False
        except asyncio.CancelledError:
            self.fence()
            raise
        except TimeoutError:
            return False  # An absent stream is unknown, never transcoding=False.
        except BaseException:
            self._state = "unavailable"
            self._restore()
            return False

    def _observe(self, message):
        if type(message) is not str:
            self._invalid = min(MAX_COUNTER, self._invalid + 1)
            return
        if (
            len(message) > MAX_MESSAGE
            or len(message.encode("utf-8", "surrogatepass")) > MAX_MESSAGE
        ):
            self._oversized = min(MAX_COUNTER, self._oversized + 1)
            return
        try:
            parsed = json.loads(message, object_pairs_hook=_unique_pairs)
        except (ValueError, RecursionError):
            self._invalid = min(MAX_COUNTER, self._invalid + 1)
            return
        if type(parsed) is not dict or parsed.get("method") != "stream_info":
            return
        body = parsed.get("body")
        stream = self._stream
        if (
            type(body) is not dict
            or type(body.get("doorbot_id")) is not int
            or body.get("doorbot_id") != stream.device_api_id
            or type(body.get("session_id")) is not str
            or type(stream.session_id) is not str
            or body.get("session_id") != stream.session_id
            or type(body.get("transcoding")) is not bool
        ):
            self._invalid = min(MAX_COUNTER, self._invalid + 1)
            return
        self._state = "observed"
        self._transcoding = body["transcoding"]
        reason = body.get("transcoding_reason")
        self._reason = (
            "codec_mismatch"
            if reason == "codec_mismatch"
            else "other"
            if type(reason) is str and 0 < len(reason) <= 256
            else "unknown"
        )
        self._received_at = _clock_value(self._clock)
        self._observations = min(MAX_COUNTER, self._observations + 1)

    def snapshot(self):
        if not self._admitted():
            self.fence()
            return None
        if self._stream is not None and not self._owned():
            self.fence()
            return None
        try:
            now = _clock_value(self._clock)
            age = (
                min(MAX_COUNTER, round((now - self._received_at) * 1000))
                if now is not None and self._received_at is not None and now >= self._received_at
                else None
            )
            return {
                "state": self._state,
                "installed_before_reader": self._before_reader,
                "transcoding": self._transcoding,
                "reason": self._reason,
                "observations": self._observations,
                "invalid_messages": self._invalid,
                "oversized_messages": self._oversized,
                "age_ms": age,
            }
        except BaseException:
            return None

    def fence(self):
        self._closed = True
        self._restore()

    def _restore(self):
        try:
            if (
                self._stream is not None
                and self._stream.__dict__.get("handle_message") is self._wrapper
            ):
                del self._stream.handle_message
        except BaseException:
            pass
        self._registry = self._stream = self._wrapper = None
