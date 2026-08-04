"""Deterministic per-camera Ring incident coordination."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum


class IncidentPhase(StrEnum):
    """Externally observable lifecycle of one camera incident."""

    IDLE = "idle"
    WARMING = "warming"
    ACTIVE = "active"
    DEGRADED = "degraded"


class IncidentActionType(StrEnum):
    """Side effects requested by the coordinator."""

    START_CAPTURE = "start_capture"
    STOP_CAPTURE = "stop_capture"
    MEDIA_READY = "media_ready"
    MEDIA_FAILED = "media_failed"
    MOTION_ON = "motion_on"
    MOTION_OFF = "motion_off"
    DOORBELL = "doorbell"


@dataclass(frozen=True)
class IncidentAction:
    """One ordered side effect for the service layer to execute."""

    type: IncidentActionType
    generation: int
    reason: str
    event_id: str | None = None


@dataclass(frozen=True)
class IncidentSnapshot:
    """Immutable coordinator state for APIs and diagnostics."""

    phase: IncidentPhase
    generation: int
    started_at: float | None
    last_signal_at: float | None
    quiet_deadline: float | None
    readiness_deadline: float | None
    media_ready: bool
    motion_published: bool
    failure: str | None


class IncidentCoordinator:
    """Coordinate one camera's capture and downstream event lifecycle.

    Ring push notifications are treated as distinct positive signals. Every new
    signal extends the incident quiet deadline. Duplicate updates for the same
    Ring event do not extend it.
    """

    def __init__(
        self,
        *,
        quiet_period: float = 30.0,
        readiness_timeout: float = 2.0,
        dedupe_window: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if quiet_period <= 0:
            raise ValueError("quiet period must be positive")
        if readiness_timeout <= 0:
            raise ValueError("readiness timeout must be positive")
        if dedupe_window <= 0:
            raise ValueError("dedupe window must be positive")
        self._quiet_period = quiet_period
        self._readiness_timeout = readiness_timeout
        self._dedupe_window = dedupe_window
        self._clock = clock
        self._lock = threading.RLock()
        self._phase = IncidentPhase.IDLE
        self._generation = 0
        self._started_at: float | None = None
        self._last_signal_at: float | None = None
        self._quiet_deadline: float | None = None
        self._readiness_deadline: float | None = None
        self._media_ready = False
        self._motion_published = False
        self._failure: str | None = None
        self._recent_events: dict[tuple[str, str], float] = {}

    def snapshot(self) -> IncidentSnapshot:
        with self._lock:
            return IncidentSnapshot(
                phase=self._phase,
                generation=self._generation,
                started_at=self._started_at,
                last_signal_at=self._last_signal_at,
                quiet_deadline=self._quiet_deadline,
                readiness_deadline=self._readiness_deadline,
                media_ready=self._media_ready,
                motion_published=self._motion_published,
                failure=self._failure,
            )

    def on_motion(
        self,
        event_id: str | int,
        *,
        is_update: bool = False,
        now: float | None = None,
    ) -> list[IncidentAction]:
        """Accept one Ring motion signal and extend the active incident."""
        return self._on_signal("motion", str(event_id), is_update=is_update, now=now)

    def on_doorbell(
        self,
        event_id: str | int,
        *,
        is_update: bool = False,
        now: float | None = None,
    ) -> list[IncidentAction]:
        """Accept a doorbell signal and publish it without waiting for media."""
        return self._on_signal("doorbell", str(event_id), is_update=is_update, now=now)

    def on_media_ready(
        self,
        generation: int,
        *,
        now: float | None = None,
    ) -> list[IncidentAction]:
        """Mark media ready if it belongs to the current incident generation."""
        current_time = self._now(now)
        with self._lock:
            actions = self._advance_locked(current_time)
            if generation != self._generation or self._phase is IncidentPhase.IDLE:
                return actions
            if self._media_ready:
                return actions

            self._media_ready = True
            self._failure = None
            self._phase = IncidentPhase.ACTIVE
            actions.append(
                IncidentAction(
                    IncidentActionType.MEDIA_READY,
                    self._generation,
                    "first_decodable_media",
                )
            )
            if not self._motion_published:
                self._motion_published = True
                actions.append(
                    IncidentAction(
                        IncidentActionType.MOTION_ON,
                        self._generation,
                        "media_ready",
                    )
                )
            return actions

    def on_media_failed(
        self,
        generation: int,
        failure: str,
        *,
        now: float | None = None,
    ) -> list[IncidentAction]:
        """Record a current capture failure while preserving the incident."""
        current_time = self._now(now)
        with self._lock:
            actions = self._advance_locked(current_time)
            if generation != self._generation or self._phase is IncidentPhase.IDLE:
                return actions
            self._failure = failure
            self._phase = IncidentPhase.DEGRADED
            actions.append(
                IncidentAction(
                    IncidentActionType.MEDIA_FAILED,
                    self._generation,
                    failure,
                )
            )
            return actions

    def tick(self, *, now: float | None = None) -> list[IncidentAction]:
        """Advance readiness and quiet deadlines."""
        with self._lock:
            return self._advance_locked(self._now(now))

    def stop(self, reason: str = "service_stop") -> list[IncidentAction]:
        """Stop the current incident explicitly."""
        with self._lock:
            if self._phase is IncidentPhase.IDLE:
                return []
            actions = self._stop_actions_locked(reason)
            self._reset_locked()
            return actions

    def next_deadline(self) -> float | None:
        """Return the next monotonic time at which ``tick`` may produce work."""
        with self._lock:
            if self._phase is IncidentPhase.IDLE:
                return None
            deadlines = [deadline for deadline in (self._quiet_deadline,) if deadline is not None]
            if not self._motion_published and self._readiness_deadline is not None:
                deadlines.append(self._readiness_deadline)
            return min(deadlines) if deadlines else None

    def _on_signal(
        self,
        kind: str,
        event_id: str,
        *,
        is_update: bool,
        now: float | None,
    ) -> list[IncidentAction]:
        current_time = self._now(now)
        with self._lock:
            actions = self._advance_locked(current_time)
            key = (kind, event_id)
            self._prune_events_locked(current_time)
            if key in self._recent_events:
                return actions

            if self._phase is IncidentPhase.IDLE:
                self._start_locked(current_time)
                actions.append(
                    IncidentAction(
                        IncidentActionType.START_CAPTURE,
                        self._generation,
                        "ring_update_recovery" if is_update else kind,
                        event_id,
                    )
                )

            self._recent_events[key] = current_time
            self._last_signal_at = current_time
            self._quiet_deadline = current_time + self._quiet_period
            if kind == "doorbell":
                actions.append(
                    IncidentAction(
                        IncidentActionType.DOORBELL,
                        self._generation,
                        "ring_update_recovery" if is_update else "ring_push",
                        event_id,
                    )
                )
            return actions

    def _advance_locked(self, now: float) -> list[IncidentAction]:
        if self._phase is IncidentPhase.IDLE:
            return []

        if self._quiet_deadline is not None and now >= self._quiet_deadline:
            actions = self._stop_actions_locked("quiet_period_elapsed")
            self._reset_locked()
            return actions

        if (
            not self._motion_published
            and self._readiness_deadline is not None
            and now >= self._readiness_deadline
        ):
            self._motion_published = True
            if not self._media_ready:
                self._phase = IncidentPhase.DEGRADED
            return [
                IncidentAction(
                    IncidentActionType.MOTION_ON,
                    self._generation,
                    "readiness_timeout",
                )
            ]
        return []

    def _start_locked(self, now: float) -> None:
        self._generation += 1
        self._phase = IncidentPhase.WARMING
        self._started_at = now
        self._last_signal_at = None
        self._quiet_deadline = None
        self._readiness_deadline = now + self._readiness_timeout
        self._media_ready = False
        self._motion_published = False
        self._failure = None

    def _stop_actions_locked(self, reason: str) -> list[IncidentAction]:
        actions: list[IncidentAction] = []
        if self._motion_published:
            actions.append(
                IncidentAction(
                    IncidentActionType.MOTION_OFF,
                    self._generation,
                    reason,
                )
            )
        actions.append(
            IncidentAction(
                IncidentActionType.STOP_CAPTURE,
                self._generation,
                reason,
            )
        )
        return actions

    def _reset_locked(self) -> None:
        self._phase = IncidentPhase.IDLE
        self._started_at = None
        self._last_signal_at = None
        self._quiet_deadline = None
        self._readiness_deadline = None
        self._media_ready = False
        self._motion_published = False
        self._failure = None

    def _prune_events_locked(self, now: float) -> None:
        cutoff = now - self._dedupe_window
        self._recent_events = {
            key: seen_at for key, seen_at in self._recent_events.items() if seen_at >= cutoff
        }

    def _now(self, supplied: float | None) -> float:
        value = self._clock() if supplied is None else supplied
        if not math.isfinite(value) or value < 0:
            raise ValueError("monotonic time must be finite and non-negative")
        return value
