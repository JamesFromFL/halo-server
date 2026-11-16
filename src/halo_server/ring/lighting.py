"""One exclusive lighting reservation per authenticated Ring client.

Camera, hub and group writes share this object across the daemon and Ring loops.
The owner retains its token until the actual operation retires, even when its
consumer cancels. A retired token can never release a newer operation.
"""

from __future__ import annotations

import asyncio
import math
import threading
from dataclasses import dataclass

MAX_LIGHTING_TARGETS = 256
MAX_LIGHTING_WAITERS = 64


class LightingRetiredError(RuntimeError):
    """The authenticated client owning this reservation has retired."""


@dataclass(eq=False)
class LightingToken:
    sent: bool = False
    acknowledged_revision: int | None = None


class LightingReservation:
    def __init__(self):
        self._lock = threading.Lock()
        self._active = None
        self._retired = False
        self._revision = 0
        self._global_revision = 0
        self._camera_revisions = {}
        self._active_camera_id = None
        self._waiters = set()

    def try_acquire(self, camera_id=None):
        if camera_id is not None and (type(camera_id) is not str or not 0 < len(camera_id) <= 128):
            raise ValueError("Invalid lighting target")
        with self._lock:
            if self._retired or self._active is not None:
                return None
            self._active = LightingToken()
            self._active_camera_id = camera_id
            return self._active

    def valid(self, token):
        with self._lock:
            return not self._retired and token is not None and self._active is token

    def mark_sent(self, token):
        with self._lock:
            if self._retired or token is None or self._active is not token:
                return False
            if not token.sent:
                token.sent = True
                self._mutated_locked()
            return True

    def acknowledge(self, token):
        """Fence inventory reads begun before this active write completed.

        Call only after a successful setter, before releasing its token. The
        second revision distinguishes a GET during the write from one begun
        after its acknowledgement; neither revision counts physical writes.
        """
        with self._lock:
            if self._retired or token is None or self._active is not token or not token.sent:
                return None
            if token.acknowledged_revision is None:
                self._mutated_locked()
                token.acknowledged_revision = self._revision
            return token.acknowledged_revision

    def _mutated_locked(self):
        self._revision += 1
        camera_id = self._active_camera_id
        if camera_id is None or (
            camera_id not in self._camera_revisions
            and len(self._camera_revisions) >= MAX_LIGHTING_TARGETS
        ):
            # Never evict a target and accidentally resurrect an old receipt.
            # A bounded-history reset conservatively invalidates every camera.
            self._global_revision = self._revision
            self._camera_revisions.clear()
        else:
            self._camera_revisions[camera_id] = self._revision

    def observation_current(self, revision, camera_id):
        """Validate a global GET stamp for one target without retaining history."""
        if type(revision) is not int or type(camera_id) is not str or not 0 < len(camera_id) <= 128:
            return False
        with self._lock:
            return (
                not self._retired
                and 0 <= revision <= self._revision
                and self._global_revision <= revision
                and self._camera_revisions.get(camera_id, 0) <= revision
            )

    async def wait_available(self, timeout):
        """Wait for a chance to acquire, never receive ownership implicitly.

        Callers must retry try_acquire with their own deadline and fresh guards.
        Cancellation/timeout only removes a notification: no command is queued
        here, and no token can be stranded by a canceled or closed event loop.
        """
        if (
            type(timeout) not in (int, float)
            or not 0 < timeout <= 2**53 - 1
            or not math.isfinite(timeout)
        ):
            return False
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        waiter = loop, future
        with self._lock:
            if self._retired:
                raise LightingRetiredError("Lighting client has retired")
            if self._active is None:
                return True
            if len(self._waiters) >= MAX_LIGHTING_WAITERS:
                return False
            self._waiters.add(waiter)
        try:
            try:
                async with asyncio.timeout(timeout):
                    await future
            except TimeoutError:
                return False
            with self._lock:
                if self._retired:
                    raise LightingRetiredError("Lighting client has retired")
            return True
        finally:
            with self._lock:
                self._waiters.discard(waiter)

    def _wake(self, waiters):
        for loop, future in waiters:

            def notify(target=future):
                if not target.done():
                    target.set_result(None)

            try:
                loop.call_soon_threadsafe(notify)
            except RuntimeError:
                # A closed consumer loop cannot own a reservation token.
                with self._lock:
                    self._waiters.discard((loop, future))

    @property
    def revision(self):
        with self._lock:
            return self._revision

    def release(self, token):
        waiters = ()
        with self._lock:
            if token is not None and self._active is token:
                self._active = None
                self._active_camera_id = None
                waiters = tuple(self._waiters)
        self._wake(waiters)

    def retire(self):
        with self._lock:
            self._retired = True
            waiters = tuple(self._waiters)
        self._wake(waiters)


_CLIENT_LOCK = threading.Lock()


def client_lighting(client):
    """Lazy attachment also supports injected session clients used by tests."""
    with _CLIENT_LOCK:
        reservation = getattr(client, "lighting_reservation", None)
        if reservation is None:
            reservation = LightingReservation()
            client.lighting_reservation = reservation
        if not isinstance(reservation, LightingReservation):
            raise TypeError("Invalid client lighting reservation")
        return reservation
