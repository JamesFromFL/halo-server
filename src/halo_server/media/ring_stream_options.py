"""Attempt-owned activation parity for the pinned Ring WebRTC client."""

from __future__ import annotations

import asyncio
import inspect
import json
import math
from collections.abc import Callable
from importlib import metadata

_VERSIONS = frozenset({"0.9.14"})
_POLL_SECONDS = 0.01
DEFAULT_TIMEOUT = 5.0


class RingStreamOptionsError(RuntimeError):
    """The validated post-activation command could not complete safely."""

    def __init__(self, message, *, code="contract_unavailable"):
        super().__init__(message)
        self.code = (
            code
            if code in {"unsupported", "contract_unavailable", "retired", "timeout", "send_failed"}
            else "contract_unavailable"
        )


def _validate_sdk():
    from ring_doorbell.webrtcstream import RingWebRtcStream

    try:
        version = metadata.version("ring-doorbell")
    except metadata.PackageNotFoundError:
        version = None
    activate = RingWebRtcStream._activate
    code = getattr(activate, "__code__", None)
    names = set(getattr(code, "co_names", ()))
    if (
        version not in _VERSIONS
        or not inspect.iscoroutinefunction(activate)
        or getattr(code, "co_varnames", ())[:1] != ("self",)
        or not {"get_session_message", "send", "ping_task", "pinger"}.issubset(names)
        or "activate_session" not in getattr(code, "co_consts", ())
    ):
        raise RingStreamOptionsError(
            "Ring activation contract is not validated", code="unsupported"
        )
    return RingWebRtcStream


class RingStreamOptions:
    """Reassert the original live-view options once after SDK activation.

    This owner belongs to one receive attempt, never to a lease renewal. It
    creates no task: the caller retains and joins its negotiation coroutine.
    """

    def __init__(self, device, session_id: str, *, current: Callable[[], bool]):
        self._device = device
        self._session_id = session_id
        self._current = current
        self._lock = asyncio.Lock()
        self._closed = False
        self._stream = None
        self._sent = False

    def fence(self) -> None:
        self._closed = True

    def _admitted(self) -> bool:
        if self._closed:
            return False
        try:
            return self._current() is True
        except BaseException:
            return False

    def _check(self, registry, stream) -> None:
        if (
            not self._admitted()
            or getattr(self._device, "_webrtc_streams", None) is not registry
            or registry.get(self._session_id) is not stream
            or not getattr(stream, "is_alive", False)
        ):
            raise RingStreamOptionsError("Ring stream options authority ended", code="retired")

    async def enable(self, *, timeout: float = DEFAULT_TIMEOUT) -> bool:
        """Return whether this call sent the command; failures are content-free."""
        if type(timeout) not in {int, float} or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Stream options timeout must be finite and positive")
        try:
            async with asyncio.timeout(timeout) as deadline:
                async with self._lock:
                    stream_type = _validate_sdk()
                    registry = getattr(self._device, "_webrtc_streams", None)
                    if not isinstance(registry, dict):
                        raise RingStreamOptionsError("Ring stream registry is unavailable")
                    stream = registry.get(self._session_id)
                    if not isinstance(stream, stream_type):
                        raise RingStreamOptionsError("Ring stream is unavailable")
                    if self._stream is not None and self._stream is not stream:
                        raise RingStreamOptionsError(
                            "Ring stream options authority ended", code="retired"
                        )
                    self._stream = stream
                    offered = getattr(stream, "_offered_event", None)
                    answered = getattr(stream, "_sdp_answer_event", None)
                    if not isinstance(offered, asyncio.Event) or not isinstance(
                        answered, asyncio.Event
                    ):
                        raise RingStreamOptionsError("Ring stream options contract changed")
                    while True:
                        self._check(registry, stream)
                        ping = getattr(stream, "ping_task", None)
                        if (
                            offered.is_set()
                            and answered.is_set()
                            and stream.session_id is not None
                            and isinstance(ping, asyncio.Task)
                            and not ping.done()
                        ):
                            break
                        await asyncio.sleep(_POLL_SECONDS)
                    if self._sent:
                        return False
                    socket = getattr(stream, "websocket", None)
                    send = getattr(socket, "send", None)
                    if not callable(send):
                        raise RingStreamOptionsError("Ring signaling socket is unavailable")
                    ring_session_id = stream.session_id
                    message = stream.get_session_message(
                        "stream_options", {"audio_enabled": True, "video_enabled": True}
                    )
                    self._check(registry, stream)
                    try:
                        await send(json.dumps(message, separators=(",", ":")))
                    except Exception:
                        raise RingStreamOptionsError(
                            "Ring stream options command failed", code="send_failed"
                        ) from None
                    self._sent = True
                    if deadline.expired():
                        raise RingStreamOptionsError(
                            "Ring stream options timed out", code="timeout"
                        )
                    self._check(registry, stream)
                    if stream.websocket is not socket or stream.session_id != ring_session_id:
                        raise RingStreamOptionsError(
                            "Ring stream options authority ended", code="retired"
                        )
                    return True
        except TimeoutError:
            self.fence()
            raise RingStreamOptionsError("Ring stream options timed out", code="timeout") from None
        except BaseException:
            # A failed/ambiguous send is never replayed within this attempt.
            self.fence()
            raise
