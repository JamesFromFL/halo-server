"""One non-queued lighting reservation per authenticated Ring client.

Camera, hub and group writes share this object across the daemon and Ring loops.
The owner retains its token until the actual operation retires, even when its
consumer cancels. A retired token can never release a newer operation.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass


@dataclass(eq=False)
class LightingToken:
    sent: bool = False


class LightingReservation:
    def __init__(self):
        self._lock = threading.Lock()
        self._active = None
        self._retired = False
        self._revision = 0

    def try_acquire(self):
        with self._lock:
            if self._retired or self._active is not None:
                return None
            self._active = LightingToken()
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
                self._revision += 1
            return True

    @property
    def revision(self):
        with self._lock:
            return self._revision

    def release(self, token):
        with self._lock:
            if token is not None and self._active is token:
                self._active = None

    def retire(self):
        with self._lock:
            self._retired = True


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
