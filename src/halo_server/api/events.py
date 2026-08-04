"""Bounded sequence journal for reconnecting plugin clients."""

from __future__ import annotations

import asyncio
import json
import math
import secrets
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

MAX_EVENT_BYTES = 64 * 1024


class EventJournalError(RuntimeError):
    """The plugin must perform a full state resynchronization."""


@dataclass(frozen=True)
class JournalEvent:
    epoch: str
    sequence: int
    emitted_at: float
    type: str
    camera_id: str | None
    payload: Mapping[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "epoch": self.epoch,
            "sequence": self.sequence,
            "emitted_at": self.emitted_at,
            "type": self.type,
            "camera_id": self.camera_id,
            "payload": _thaw_json(self.payload),
        }


class EventJournal:
    """Retain bounded events and wake same-loop asynchronous subscribers."""

    def __init__(self, *, max_events: int = 1024) -> None:
        if isinstance(max_events, bool) or not isinstance(max_events, int) or max_events <= 0:
            raise ValueError("maximum event count must be a positive integer")
        self.epoch = secrets.token_urlsafe(12)
        self._events: deque[JournalEvent] = deque(maxlen=max_events)
        self._next_sequence = 1
        self._waiters: set[asyncio.Event] = set()

    @property
    def latest_sequence(self) -> int:
        return self._next_sequence - 1

    def publish(
        self,
        event_type: str,
        *,
        camera_id: str | None,
        payload: dict[str, Any],
        emitted_at: float | None = None,
    ) -> JournalEvent:
        if not isinstance(event_type, str) or not event_type or len(event_type) > 64:
            raise ValueError("event type is invalid")
        event_type = str(event_type)
        if camera_id is not None and (
            not isinstance(camera_id, str) or not camera_id or len(camera_id) > 128
        ):
            raise ValueError("event camera id is invalid")
        copied_payload = _bounded_json_copy(payload)
        event_time = time.time() if emitted_at is None else emitted_at
        if (
            not isinstance(event_time, (int, float))
            or isinstance(event_time, bool)
            or not math.isfinite(event_time)
            or event_time < 0
        ):
            raise ValueError("event time must be finite and non-negative")
        event = JournalEvent(
            epoch=self.epoch,
            sequence=self._next_sequence,
            emitted_at=float(event_time),
            type=event_type,
            camera_id=camera_id,
            payload=copied_payload,
        )
        self._next_sequence += 1
        self._events.append(event)
        waiters = tuple(self._waiters)
        self._waiters.clear()
        for waiter in waiters:
            waiter.set()
        return event

    def replay(
        self,
        *,
        epoch: str,
        after_sequence: int,
    ) -> tuple[JournalEvent, ...]:
        if epoch != self.epoch:
            raise EventJournalError("event epoch changed; full resynchronization required")
        if (
            isinstance(after_sequence, bool)
            or not isinstance(after_sequence, int)
            or after_sequence < 0
        ):
            raise ValueError("event sequence must be a non-negative integer")
        latest = self.latest_sequence
        earliest = self._events[0].sequence if self._events else self._next_sequence
        if after_sequence > latest or after_sequence < earliest - 1:
            raise EventJournalError("event history gap; full resynchronization required")
        return tuple(event for event in self._events if event.sequence > after_sequence)

    async def wait_for_events(
        self,
        *,
        epoch: str,
        after_sequence: int,
        timeout: float,
    ) -> tuple[JournalEvent, ...]:
        replay = self.replay(epoch=epoch, after_sequence=after_sequence)
        if replay:
            return replay
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("event wait timeout must be finite and positive")
        waiter = asyncio.Event()
        self._waiters.add(waiter)
        try:
            replay = self.replay(epoch=epoch, after_sequence=after_sequence)
            if replay:
                return replay
            try:
                await asyncio.wait_for(waiter.wait(), timeout=timeout)
            except TimeoutError:
                return ()
            return self.replay(epoch=epoch, after_sequence=after_sequence)
        finally:
            self._waiters.discard(waiter)


def _bounded_json_copy(payload: dict[str, Any]) -> Mapping[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("event payload must be a JSON object")
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    except (TypeError, ValueError) as exc:
        raise ValueError("event payload must contain only finite JSON data") from exc
    if len(encoded) > MAX_EVENT_BYTES:
        raise ValueError("event payload is too large")
    copied = json.loads(encoded)
    if not isinstance(copied, dict):
        raise ValueError("event payload must be a JSON object")
    return _freeze_json(copied)


def _freeze_json(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze_json(nested) for key, nested in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(nested) for nested in value)
    return value


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(nested) for key, nested in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(nested) for nested in value]
    return value
