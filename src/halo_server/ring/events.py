"""Normalized, supervised Ring push-event delivery."""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import logging
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

_log = logging.getLogger(__name__)

RECONNECT_MIN = 1.0
RECONNECT_MAX = 30.0
MONITOR_INTERVAL = 2.0
STOP_TIMEOUT = 5.0
START_TIMEOUT = 5.0


class EventConnectionState(StrEnum):
    """Connection state exposed to the daemon API and TUI."""

    OFFLINE = "offline"
    CONNECTING = "connecting"
    CONNECTED = "connected"


class RingEventType(StrEnum):
    """Stable Halo event names independent of Ring's wire terminology."""

    MOTION = "motion"
    DOORBELL = "doorbell"
    OTHER = "other"


class EventNormalizationError(ValueError):
    """Raised when an incoming Ring event lacks its required identity."""


@dataclass(frozen=True)
class NormalizedRingEvent:
    """Serializable Ring event passed into Halo's incident layer."""

    event_id: str
    camera_id: str
    camera_name: str
    camera_kind: str
    type: RingEventType
    source_kind: str
    source_state: str
    occurred_at: float
    received_at: float
    source_expires_in: float | None
    is_update: bool


def normalize_ring_event(
    event: Any,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> NormalizedRingEvent:
    """Convert a ring-doorbell event into Halo's stable internal contract."""
    event_id = _required_identifier(event, "id", "event")
    camera_id = _required_identifier(event, "doorbot_id", "camera")
    source_kind = _required_text(event, "kind").lower()
    occurred_at = _finite_number(getattr(event, "now", None), "event timestamp")
    received_at = _finite_number(clock(), "receipt timestamp")
    expires = getattr(event, "expires_in", None)
    source_expires_in = (
        None if expires is None else _finite_number(expires, "source expiry duration")
    )
    if source_expires_in is not None and source_expires_in < 0:
        raise EventNormalizationError("source expiry duration must be non-negative")

    if source_kind == "motion":
        event_type = RingEventType.MOTION
    elif source_kind == "ding":
        event_type = RingEventType.DOORBELL
    else:
        event_type = RingEventType.OTHER

    return NormalizedRingEvent(
        event_id=event_id,
        camera_id=camera_id,
        camera_name=str(getattr(event, "device_name", "") or ""),
        camera_kind=str(getattr(event, "device_kind", "") or ""),
        type=event_type,
        source_kind=source_kind,
        source_state=str(getattr(event, "state", "") or ""),
        occurred_at=occurred_at,
        received_at=received_at,
        source_expires_in=source_expires_in,
        is_update=bool(getattr(event, "is_update", False)),
    )


class EventListener:
    """Supervise Ring FCM events on the authenticated client's event loop."""

    def __init__(
        self,
        client,
        on_event: Callable[[NormalizedRingEvent], None],
        on_state: Callable[[EventConnectionState], None] | None = None,
    ) -> None:
        self._client = client
        self._on_event = on_event
        self._on_state = on_state
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._future: concurrent.futures.Future | None = None
        self._state = EventConnectionState.OFFLINE
        self._startup_ready = threading.Event()
        self._startup_error: BaseException | None = None

    @property
    def state(self) -> EventConnectionState:
        with self._lock:
            return self._state

    @property
    def running(self) -> bool:
        with self._lock:
            return self._future is not None and not self._future.done()

    def start(self) -> concurrent.futures.Future:
        """Start listening without blocking; repeated calls are idempotent."""
        with self._lock:
            if self._future is not None and not self._future.done():
                return self._future
            self._stop.clear()
            self._startup_ready.clear()
            self._startup_error = None
            coroutine = self._run()
            try:
                self._future = self._client.submit(coroutine)
            except BaseException as exc:
                coroutine.close()
                self._startup_error = exc
                self._startup_ready.set()
                raise
            return self._future

    def wait_until_started(self, timeout: float = START_TIMEOUT) -> None:
        """Wait until the supervisor has initialized or surface its startup failure."""
        ready = self._startup_ready.wait(timeout)
        with self._lock:
            future = self._future
            startup_error = self._startup_error
        if not ready:
            if future is not None and future.done():
                future.result()
            raise TimeoutError("Ring event listener did not start in time")
        if startup_error is not None:
            raise RuntimeError(str(startup_error)) from startup_error
        if future is None:
            raise RuntimeError("Ring event listener was not submitted")
        if future.done():
            future.result()
            if not self._stop.is_set():
                raise RuntimeError("Ring event listener stopped during startup")

    def stop(self) -> None:
        """Request listener teardown and wait briefly outside its loop thread."""
        self._stop.set()
        with self._lock:
            future = self._future
        if future is None:
            self._set_state(EventConnectionState.OFFLINE)
            return

        loop_thread = getattr(self._client, "_loop_thread", None)
        if threading.current_thread() is loop_thread:
            return
        try:
            future.result(timeout=STOP_TIMEOUT)
        except concurrent.futures.TimeoutError:
            future.cancel()
        except (concurrent.futures.CancelledError, Exception) as exc:
            _log.debug("Ring event listener stopped with an error: %s", exc)
        finally:
            with self._lock:
                if self._future is future:
                    self._future = None
            self._set_state(EventConnectionState.OFFLINE)

    def _emit(self, event: Any) -> None:
        try:
            normalized = normalize_ring_event(event)
        except EventNormalizationError as exc:
            _log.warning("Ignoring malformed Ring event: %s", exc)
            return
        try:
            self._on_event(normalized)
        except Exception as exc:
            _log.exception("Ring event callback failed: %s", exc)

    def _set_state(self, state: EventConnectionState) -> None:
        callback = None
        with self._lock:
            if state is self._state:
                return
            self._state = state
            callback = self._on_state
        if callback is not None:
            try:
                callback(state)
            except Exception as exc:
                _log.debug("Ring connection-state callback failed: %s", exc)

    async def _run(self) -> None:
        try:
            from ring_doorbell import RingEventListener, RingEventListenerConfig

            ring = self._client.ring
            if ring is None:
                raise RuntimeError("Cannot start Ring events without an authenticated session")

            config = RingEventListenerConfig.default_config()
            config.connection_retry_count = 1_000_000
            config.abort_on_sequential_error_count = None
            backoff = RECONNECT_MIN
            while not self._stop.is_set():
                self._set_state(EventConnectionState.CONNECTING)
                listener = RingEventListener(ring, config=config)
                listener.add_notification_callback(self._emit)
                self._signal_startup()
                try:
                    try:
                        started = await listener.start()
                    except Exception as exc:
                        _log.info("Ring event listener start failed; retrying: %s", exc)
                        started = False

                    if not started:
                        await self._sleep_until_stopped(backoff)
                        backoff = min(backoff * 2, RECONNECT_MAX)
                        continue

                    backoff = RECONNECT_MIN
                    self._set_state(EventConnectionState.CONNECTED)
                    while not self._stop.is_set():
                        await self._sleep_until_stopped(MONITOR_INTERVAL)
                        if self._stop.is_set():
                            break
                        run_state = _listener_run_state(listener)
                        if run_state == "STOPPED":
                            _log.warning("Ring event listener dropped; reconnecting")
                            break
                        if run_state == "STARTED":
                            self._set_state(EventConnectionState.CONNECTED)
                        elif run_state is not None:
                            self._set_state(EventConnectionState.CONNECTING)
                finally:
                    with contextlib.suppress(Exception):
                        await listener.stop()
        except BaseException as exc:
            self._signal_startup(exc)
            raise
        finally:
            self._signal_startup()
            self._set_state(EventConnectionState.OFFLINE)

    def _signal_startup(self, error: BaseException | None = None) -> None:
        with self._lock:
            if self._startup_ready.is_set():
                return
            self._startup_error = error
            self._startup_ready.set()

    async def _sleep_until_stopped(self, duration: float) -> None:
        deadline = asyncio.get_running_loop().time() + duration
        while not self._stop.is_set():
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return
            await asyncio.sleep(min(remaining, 0.25))


def _listener_run_state(listener: Any) -> str | None:
    receiver = getattr(listener, "_receiver", None)
    state = getattr(receiver, "run_state", None)
    name = getattr(state, "name", None)
    return name if isinstance(name, str) else None


def _required_identifier(event: Any, attribute: str, label: str) -> str:
    value = getattr(event, attribute, None)
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise EventNormalizationError(f"Ring {label} identifier is missing or invalid")
    normalized = str(value).strip()
    if not normalized:
        raise EventNormalizationError(f"Ring {label} identifier is missing or invalid")
    return normalized


def _required_text(event: Any, attribute: str) -> str:
    value = getattr(event, attribute, None)
    if not isinstance(value, str) or not value.strip():
        raise EventNormalizationError(f"Ring event {attribute} is missing or invalid")
    return value.strip()


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EventNormalizationError(f"Ring {label} is missing or invalid")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized < 0:
        raise EventNormalizationError(f"Ring {label} must be finite and non-negative")
    return normalized
