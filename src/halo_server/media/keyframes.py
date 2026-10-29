"""One bounded, attempt-owned PLI requester over the existing Ring receiver."""

from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
from collections.abc import Callable
from datetime import UTC, datetime

import aiortc
from aiortc.rtcrtpreceiver import RTCRtpReceiver

KEYFRAME_REQUEST_SECONDS = 1.0
KEYFRAME_COOLDOWN_SECONDS = 0.5
MAX_SOURCE_IDENTITIES = 16
_LIMIT = 2**53 - 1
_ACTIVE = "_RTCRtpReceiver__active_ssrc"
_REMOTE = "_RTCRtpReceiver__remote_streams"
_RTX = "_RTCRtpReceiver__rtx_ssrc"
_LOCAL = "_RTCRtpReceiver__rtcp_ssrc"


def _contract(receiver):
    method = getattr(type(receiver), "_send_rtcp_pli", None)
    code = getattr(method, "__code__", None)
    return (
        aiortc.__version__ == "1.15.0"
        and type(receiver) is RTCRtpReceiver
        and getattr(receiver, "_RTCRtpReceiver__kind", None) == "video"
        and inspect.iscoroutinefunction(method)
        and code is not None
        and code.co_argcount == 2
        and {_LOCAL, "RtcpPsfbPacket", "_send_rtcp"}.issubset(code.co_names)
        and "_send_rtcp_pli" not in receiver.__dict__
        and all(type(getattr(receiver, name, None)) is dict for name in (_ACTIVE, _REMOTE, _RTX))
    )


def _sources(receiver):
    active, remote, rtx = (getattr(receiver, name) for name in (_ACTIVE, _REMOTE, _RTX))
    if any(len(items) > MAX_SOURCE_IDENTITIES for items in (active, remote, rtx)):
        return ()
    local = getattr(receiver, _LOCAL, None)
    if type(local) is not int or not 0 <= local <= 0xFFFFFFFF:
        return ()
    now = datetime.now(UTC)
    result = []
    for source, observed in active.items():
        if (
            type(source) is int
            and 0 <= source <= 0xFFFFFFFF
            and source in remote
            and source not in rtx
            and isinstance(observed, datetime)
            and observed.tzinfo is not None
            and 0 <= (now - observed).total_seconds() <= 10
        ):
            result.append(source)
    return tuple(sorted(result))


class SourceKeyframes:
    """One actual send task, without a media queue or a new receiver.

    Cross-loop caller waiters remain bounded by existing controller claims.
    """

    def __init__(self, receiver, *, current: Callable[[], bool], spawn):
        self._receiver, self._current, self._spawn = receiver, current, spawn
        self._loop = asyncio.get_running_loop()
        self.supported = _contract(receiver)
        self._fenced = False
        self._pending = None
        self._next_at = 0.0
        self._attempted = self._coalesced = self._rejected = 0

    def _allowed(self, admission):
        try:
            return not self._fenced and self._current() and admission()
        except Exception:
            return False

    def submit(self, admission):
        # Bound to this existing attempt's loop; never ask RingClient to create
        # a loop or start another source on behalf of a stale callback.
        if self._fenced or self._loop.is_closed():
            future = concurrent.futures.Future()
            future.set_result(False)
            return future
        coroutine = self.request(admission)
        try:
            return asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        except BaseException:
            coroutine.close()
            raise

    async def request(self, admission):
        loop = asyncio.get_running_loop()
        if not self.supported or not self._allowed(admission):
            self._rejected = min(_LIMIT, self._rejected + 1)
            return False
        if self._pending is not None or loop.time() < self._next_at:
            self._coalesced = min(_LIMIT, self._coalesced + 1)
            return False
        if not _sources(self._receiver):
            self._rejected = min(_LIMIT, self._rejected + 1)
            return False
        self._next_at = loop.time() + KEYFRAME_COOLDOWN_SECONDS
        task = self._spawn(self._send(admission), "upstream keyframe request")
        self._pending = task

        def finished(completed):
            if self._pending is completed:
                self._pending = None

        task.add_done_callback(finished)
        # Cancellation fences the caller, not the source owner's actual task.
        # Receiver cleanup still owns and joins that task through close().
        return await asyncio.shield(task)

    async def _send(self, admission):
        try:
            async with asyncio.timeout(KEYFRAME_REQUEST_SECONDS):
                attempted = False
                for source in _sources(self._receiver):
                    if not self._allowed(admission) or source not in _sources(self._receiver):
                        return False
                    self._attempted = min(_LIMIT, self._attempted + 1)
                    attempted = True
                    await self._receiver._send_rtcp_pli(source)
                return attempted and self._allowed(admission)
        except Exception:
            return False

    def fence(self):
        self._fenced = True

    async def close(self):
        self.fence()
        task = self._pending
        if task is None:
            return
        task.cancel()
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if not task.done():
                    cancelled = True
            except Exception:
                break
        if self._pending is task:
            self._pending = None
        if cancelled:
            raise asyncio.CancelledError

    def snapshot(self):
        return {
            "available": self.supported and not self._fenced,
            "pending": self._pending is not None,
            "attempted": min(self._attempted, 2**53 - 1),
            "coalesced": min(self._coalesced, 2**53 - 1),
            "rejected": min(self._rejected, 2**53 - 1),
        }
