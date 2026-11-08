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
    keyframe_callback: Callable[[int], bool] | None = None
    diagnostic_trace: object | None = None

    def __post_init__(self) -> None:
        self._lock = threading.Lock()
        self._renew_lock = threading.Lock()
        self._release_lock = threading.Lock()
        self._closed = False
        self._released = False

    def renew(self) -> bool:
        with self._lock:
            if self._closed:
                return False
        with self._renew_lock:
            if not self.is_current():
                return False
            # The callback can synchronously wait for the service event loop.
            # That loop must remain free to check this consumer's authority.
            renewed = bool(self.renew_callback())
            current = self.is_current()
            return renewed and current

    def request_keyframe(self, capture_epoch: int) -> bool:
        if type(capture_epoch) is not int or capture_epoch < 0:
            return False
        with self._renew_lock:
            if (
                not self.is_current()
                or capture_epoch != self.buffer.capture_epoch
                or self.keyframe_callback is None
            ):
                return False
            attempted = bool(self.keyframe_callback(capture_epoch))
            return attempted and self.is_current() and capture_epoch == self.buffer.capture_epoch

    def is_current(self) -> bool:
        """Check source authority before each private media delivery."""
        with self._lock:
            if self._closed:
                return False
        current = self.validity_callback is None or bool(self.validity_callback())
        with self._lock:
            return not self._closed and current

    def close(self) -> None:
        with self._lock:
            # Delivery is revoked immediately even if a previous release is
            # still running or failed. That fence is not cleanup evidence.
            self._closed = True
        with self._release_lock:
            with self._lock:
                if self._released:
                    return
            # Fence first, then join renewal before actual release. A late
            # renewal must never race behind the final lease release.
            with self._renew_lock:
                self.close_callback()
            with self._lock:
                self._released = True
