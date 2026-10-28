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
    motion_deadline: float | None
    motion_known: bool


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
        for duration in (quiet_period, readiness_timeout, dedupe_window):
            if type(duration) not in {int, float} or not math.isfinite(duration) or duration <= 0:
                raise ValueError("incident durations must be finite and positive")
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
        self._motion_deadline: float | None = None
        self._motion_known = False
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
                motion_deadline=self._motion_deadline,
                motion_known=self._motion_known,
            )

    def on_motion(
        self,
        event_id: str | int,
        *,
        is_update: bool = False,
        expires_in: float | None = None,
        received_at: float | None = None,
        now: float | None = None,
    ) -> list[IncidentAction]:
        """Publish source motion immediately, independently of capture readiness."""
        if expires_in is not None and (
            type(expires_in) not in {int, float} or not math.isfinite(expires_in) or expires_in < 0
        ):
            raise ValueError("motion expiry must be finite and non-negative")
        current_time = self._now(now)
        if received_at is not None:
            if type(received_at) not in {int, float} or not math.isfinite(received_at):
                raise ValueError("motion receipt time must be finite")
            expires_in = max(
                0.0,
                min(self._quiet_period, self._quiet_period if expires_in is None else expires_in)
                - max(0.0, current_time - received_at),
            )
        return self._on_signal(
            "motion", str(event_id), is_update=is_update, now=current_time, expires_in=expires_in
        )

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
            self._readiness_deadline = None
            actions.append(
                IncidentAction(
                    IncidentActionType.MEDIA_READY,
                    self._generation,
                    "first_decodable_media",
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
            self._readiness_deadline = None
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
            deadlines = [
                deadline
                for deadline in (
                    self._quiet_deadline,
                    self._readiness_deadline,
                    self._motion_deadline,
                )
                if deadline is not None
            ]
            return min(deadlines) if deadlines else None

    def _on_signal(
        self,
        kind: str,
        event_id: str,
        *,
        is_update: bool,
        now: float | None,
        expires_in: float | None = None,
    ) -> list[IncidentAction]:
        current_time = self._now(now)
        with self._lock:
            actions = self._advance_locked(current_time)
            key = (kind, event_id)
            self._prune_events_locked(current_time)
            if key in self._recent_events:
                return actions
            self._recent_events[key] = current_time
            while len(self._recent_events) > 1024:
                self._recent_events.pop(next(iter(self._recent_events)))
            # SDK updates may describe a press from before attachment. A button
            # edge is never recovered as a fresh press; expired motion cannot
            # wake capture or clear a newer signal either.
            if kind == "doorbell" and is_update or kind == "motion" and expires_in == 0:
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

            self._last_signal_at = current_time
            self._quiet_deadline = current_time + self._quiet_period
            if kind == "motion":
                self._motion_known = True
                self._motion_deadline = current_time + min(
                    self._quiet_period,
                    self._quiet_period if expires_in is None else expires_in,
                )
                if not self._motion_published:
                    self._motion_published = True
                    actions.append(
                        IncidentAction(
                            IncidentActionType.MOTION_ON,
                            self._generation,
                            "ring_update_recovery" if is_update else "ring_push",
                            event_id,
                        )
                    )
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

        actions = []
        if self._motion_deadline is not None and now >= self._motion_deadline:
            self._motion_deadline = None
            if self._motion_published:
                self._motion_published = False
                actions.append(
                    IncidentAction(
                        IncidentActionType.MOTION_OFF, self._generation, "motion_expired"
                    )
                )
        if self._readiness_deadline is not None and now >= self._readiness_deadline:
            self._readiness_deadline = None
            if not self._media_ready:
                self._phase = IncidentPhase.DEGRADED
                self._failure = "readiness_timeout"
                actions.append(
                    IncidentAction(
                        IncidentActionType.MEDIA_FAILED, self._generation, "readiness_timeout"
                    )
                )
        return actions

    def _start_locked(self, now: float) -> None:
        self._generation += 1
        self._phase = IncidentPhase.WARMING
        self._started_at = now
        self._last_signal_at = None
        self._quiet_deadline = None
        self._readiness_deadline = now + self._readiness_timeout
        self._media_ready = False
        self._motion_published = False
        self._motion_deadline = None
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
        self._motion_deadline = None
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
