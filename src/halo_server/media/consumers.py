"""Integration-neutral claims on one shared encoded camera buffer."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass

from halo_server.media.buffer import EncodedMediaBuffer


@dataclass
class BufferedMediaConsumer:
    """One downstream claim, with idempotent close and serialized renewal."""

    buffer: EncodedMediaBuffer
    renew_callback: Callable[[], bool]
    close_callback: Callable[[], None]
    validity_callback: Callable[[], bool] | None = None
    original_audio_buffer: object | None = None
    source_clock_callback: Callable[[], tuple] | None = None

    def __post_init__(self) -> None:
        self._lock = threading.Lock()
        self._release_lock = threading.Lock()
        self._closed = False
        self._released = False

    def renew(self) -> bool:
        with self._lock:
            if self._closed or (
                self.validity_callback is not None and not self.validity_callback()
            ):
                return False
            return bool(self.renew_callback())

    def is_current(self) -> bool:
        """Check source authority before each private media delivery."""
        with self._lock:
            return not self._closed and (
                self.validity_callback is None or bool(self.validity_callback())
            )

    def close(self) -> None:
        with self._lock:
            # Delivery is revoked immediately even if a previous release is
            # still running or failed. That fence is not cleanup evidence.
            self._closed = True
        with self._release_lock:
            with self._lock:
                if self._released:
                    return
            self.close_callback()
            with self._lock:
                self._released = True
