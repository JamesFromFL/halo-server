"""Fresh, bounded native event delivery from the existing Ring incident authority."""

from __future__ import annotations

import asyncio
import contextlib
import math
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from halo_server.api.events import EventJournal, EventJournalError, JournalEvent
from halo_server.homekit.models import HomeKitOperationError, MediaScope
from halo_server.incidents.coordinator import IncidentSnapshot
from halo_server.media.manager import settle_owned

MAX_STATES = 256
MAX_PRESSES = 16
PRESS_SECONDS = 2.0
POLL_SECONDS = 0.5
STOP_SECONDS = 5.0


@dataclass(frozen=True)
class _Press:
    scope: MediaScope
    epoch: str
    sequence: int
    event_id: str
    deadline: float


class EventPump:
    """One owned sender; resynchronization restores state and never replays presses.

    ``send`` accepts the runtime's optional ``admission`` callback. It must check
    that callback after acquiring the transport write lock, before writing bytes.
    Source health describes the Ring event subscription, never camera reachability.
    ``incidents`` reads snapshots from the service's existing coordinator map.
    """

    def __init__(
        self,
        journal: EventJournal,
        controller,
        send,
        *,
        incidents: Callable[[], Mapping[str, IncidentSnapshot]] = dict,
        source_state: Callable[[], str] = lambda: "offline",
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._journal, self._controller, self._send = journal, controller, send
        self._incidents, self._source_state, self._clock = incidents, source_state, clock
        self._boot: str | None = None
        self._task: asyncio.Task | None = None
        self._cancelling = False
        self._wake = asyncio.Event()
        self._stop_lock = asyncio.Lock()
        self._synchronized = False
        self._account: dict[str, Any] | None = None
        self._epoch = ""
        self._cursor = 0
        self._baseline = 0
        self._last_press = 0
        self._scopes: tuple[MediaScope, ...] = ()
        self._presses: deque[_Press] = deque(maxlen=MAX_PRESSES)
        self._sent: dict[MediaScope, tuple] = {}
        self.state, self.reason = "stopped", None
        self.dropped_presses = 0

    @property
    def boot_id(self) -> str | None:
        return self._boot

    @property
    def synchronized(self) -> bool:
        return self._synchronized

    def start(self, boot_id: str) -> None:
        if self._boot is not None or self._task is not None:
            raise HomeKitOperationError("cleanup_pending")
        self._boot = boot_id
        self._cancelling = False
        self._synchronized = False
        self._epoch, self._cursor = self._journal.epoch, self._journal.latest_sequence
        self._baseline = self._last_press = self._cursor
        self._scopes, self._account = (), None
        self._sent.clear()
        self._presses.clear()
        self.state, self.reason = "waiting_for_sync", None
        self._task = asyncio.create_task(self._run(), name="homekit-events")
        self._task.add_done_callback(_observed)
        self._wake.set()

    def notify(
        self, event: JournalEvent | None = None, *, received_at: float | None = None
    ) -> None:
        """Called after journal publication, before capture work is scheduled.

        Only this live notification can create a doorbell edge. Journal replay
        is used for cursor/gap tracking, never to recreate a button press.
        """
        if self._boot is None:
            return
        if (
            event is not None
            and event.type == "doorbell"
            and self._synchronized
            and event.epoch == self._epoch
            and event.sequence > max(self._baseline, self._last_press)
        ):
            inventory = self._controller.inventory()
            camera = next(
                (item for item in inventory.cameras if item.scope.camera_id == event.camera_id),
                None,
            )
            if (
                inventory.complete
                and _account(inventory) == self._account
                and camera is not None
                and camera.scope in self._scopes
            ):
                self._last_press = event.sequence
                if len(self._presses) == MAX_PRESSES:
                    self.dropped_presses += 1
                self._presses.append(
                    _Press(
                        camera.scope,
                        event.epoch,
                        event.sequence,
                        str(uuid4()),
                        min(self._clock(), received_at) + PRESS_SECONDS
                        if received_at is not None
                        else self._clock() + PRESS_SECONDS,
                    )
                )
        self._wake.set()

    async def sync(self, boot_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Start at current state/cursor, even if a peer requests an older cursor.

        No await follows this state commit: the request owner can queue its ACK
        before the newly awakened sender queues snapshots or live notifications.
        """
        if self._boot != boot_id or self._task is None or self._task.done():
            raise HomeKitOperationError("boot_changed")
        inventory = self._controller.inventory()
        account = _account(inventory)
        epoch, sequence = self._journal.epoch, self._journal.latest_sequence
        requested = payload["scope"]
        accepted = requested == account and inventory.complete
        self._synchronized = accepted
        self._account, self._epoch = account, epoch
        self._cursor = self._baseline = self._last_press = sequence
        self._scopes = tuple(camera.scope for camera in inventory.cameras[:MAX_STATES])
        self._presses.clear()
        self._sent.clear()
        self.state, self.reason = ("synchronized", None) if accepted else ("waiting_for_sync", None)
        self._wake.set()
        return {
            "scope": requested,
            "epoch": epoch,
            "sequence": sequence,
            "accepted": accepted,
            "code": "ok" if accepted else "stale-scope" if requested != account else "not-ready",
        }

    def _invalidate(self, inventory) -> None:
        self._synchronized = False
        self._account = _account(inventory)
        self._epoch, self._cursor = self._journal.epoch, self._journal.latest_sequence
        self._baseline = self._last_press = self._cursor
        self._scopes = tuple(camera.scope for camera in inventory.cameras[:MAX_STATES])
        self._presses.clear()
        self._sent.clear()
        self.state = "waiting_for_sync"

    def _current(self, scope: MediaScope, boot: str, epoch: str, synchronized: bool) -> bool:
        if self._boot != boot or self._epoch != epoch or self._cancelling:
            return False
        if synchronized and not self._synchronized:
            return False
        if self._journal.epoch != epoch:
            return False
        inventory = self._controller.inventory()
        return (
            inventory.complete
            and _account(inventory) == self._account
            and any(camera.scope == scope for camera in inventory.cameras)
        )

    def _motion(
        self, scope: MediaScope, snapshots, now, source
    ) -> tuple[bool | None, int, float | None]:
        snapshot = snapshots.get(scope.camera_id)
        if not self._synchronized or snapshot is None:
            return None, 0, None
        deadline = snapshot.motion_deadline
        if snapshot.motion_published and deadline is not None and deadline > now:
            return True, min(30000, max(1, math.ceil((deadline - now) * 1000))), deadline
        known = snapshot.motion_known and source == "connected"
        return False if known else None, 0, None

    def _source(self) -> str:
        source = str(self._source_state())
        return source if source in {"connected", "connecting", "offline"} else "offline"

    def _admit_state(self, scope, boot, epoch, synchronized, signature, payload) -> bool:
        if not self._current(scope, boot, epoch, synchronized):
            return False
        source = self._source()
        motion, ttl, deadline = self._motion(scope, self._incidents(), self._clock(), source)
        if (motion, deadline, source, self._synchronized) != signature:
            return False
        # Recompute the relative lifetime under the actual transport write lock;
        # queueing time must not extend an already observed source deadline.
        payload["ttl_ms"] = ttl
        payload["expires_at_ms"] = math.floor(deadline * 1000) if motion is True else 0
        return True

    def _admit_press(self, press, boot, payload) -> bool:
        remaining = press.deadline - self._clock()
        if remaining <= 0 or not self._current(press.scope, boot, press.epoch, True):
            return False
        payload["ttl_ms"] = min(2000, max(1, math.ceil(remaining * 1000)))
        payload["expires_at_ms"] = math.floor(press.deadline * 1000)
        return True

    async def _run(self) -> None:
        try:
            while self._boot is not None:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), POLL_SECONDS)
                self._wake.clear()
                inventory = self._controller.inventory()
                scopes = tuple(camera.scope for camera in inventory.cameras[:MAX_STATES])
                if (
                    self._journal.epoch != self._epoch
                    or _account(inventory) != self._account
                    or scopes != self._scopes
                    or not inventory.complete
                    and self._synchronized
                ):
                    self._invalidate(inventory)
                if self._synchronized:
                    try:
                        replay = self._journal.replay(
                            epoch=self._epoch, after_sequence=self._cursor
                        )
                    except EventJournalError:
                        self._invalidate(inventory)
                    else:
                        if replay:
                            self._cursor = replay[-1].sequence
                source = self._source()
                now, snapshots = self._clock(), self._incidents()
                boot, epoch = self._boot, self._epoch
                for scope in self._scopes:
                    motion, ttl, deadline = self._motion(scope, snapshots, now, source)
                    signature = (motion, deadline, source, self._synchronized)
                    if self._sent.get(scope) == signature:
                        continue
                    synchronized = self._synchronized
                    payload = {
                        "scope": _scope(scope),
                        "epoch": epoch,
                        "sequence": self._cursor,
                        "motion": motion,
                        "ttl_ms": ttl,
                        "expires_at_ms": math.floor(deadline * 1000) if motion is True else 0,
                        "source_state": source,
                        "synchronized": synchronized,
                    }

                    def admitted(
                        scope=scope,
                        synchronized=synchronized,
                        boot=boot,
                        epoch=epoch,
                        signature=signature,
                        payload=payload,
                    ):
                        return self._admit_state(
                            scope, boot, epoch, synchronized, signature, payload
                        )

                    if not admitted():
                        continue
                    try:
                        await self._send("event_state", payload, admission=admitted)
                    except Exception:
                        if admitted():
                            raise
                        self._wake.set()
                        break
                    self._sent[scope] = signature
                while self._presses and self._synchronized:
                    press = self._presses.popleft()
                    remaining = press.deadline - self._clock()
                    if remaining <= 0:
                        self.dropped_presses += 1
                        continue

                    payload = {
                        "scope": _scope(press.scope),
                        "epoch": press.epoch,
                        "sequence": press.sequence,
                        "event_id": press.event_id,
                        "type": "doorbell",
                        "ttl_ms": min(2000, max(1, math.ceil(remaining * 1000))),
                        "expires_at_ms": math.floor(press.deadline * 1000),
                    }

                    def admitted(press=press, boot=boot, payload=payload):
                        return self._admit_press(press, boot, payload)

                    if not admitted():
                        continue
                    try:
                        await self._send("camera_event", payload, admission=admitted)
                    except Exception:
                        if admitted():
                            raise
                        self._wake.set()
        except asyncio.CancelledError:
            raise
        except Exception:
            self._synchronized = False
            self.state, self.reason = "failed", "event_delivery_failed"

    async def stop(self, boot_id: str | None = None) -> None:
        task = asyncio.create_task(self._stop_owned(boot_id), name="homekit-events-stop")
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await settle_owned(task)
            raise

    async def _stop_owned(self, boot_id: str | None) -> None:
        async with self._stop_lock:
            if boot_id is not None and self._boot != boot_id:
                return
            self._synchronized = False
            self._presses.clear()
            task = self._task
            if task is not None:
                if not self._cancelling:
                    self._cancelling = True
                    task.cancel()
                _done, pending = await asyncio.wait({task}, timeout=STOP_SECONDS)
                if pending:
                    self.state, self.reason = "failed", "event_cleanup_pending"
                    raise HomeKitOperationError("cleanup_pending")
                await asyncio.gather(task, return_exceptions=True)
            self._task = None
            self._boot = None
            self._sent.clear()
            self._scopes = ()
            self.state, self.reason = "stopped", None


def _account(inventory):
    return (
        None
        if inventory.profile_id is None
        else {
            "profile_id": inventory.profile_id,
            "account_generation": inventory.account_generation,
        }
    )


def _scope(scope: MediaScope) -> dict[str, Any]:
    return {
        "profile_id": scope.profile_id,
        "camera_id": scope.camera_id,
        "account_generation": scope.account_generation,
        "device_generation": scope.device_generation,
        "privacy_generation": scope.privacy_generation,
    }


def _observed(task: asyncio.Task) -> None:
    if not task.cancelled():
        task.exception()
