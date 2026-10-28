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

    def __post_init__(self) -> None:
        self._lock = threading.Lock()
        self._closed = False

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
            if self._closed:
                return
            self._closed = True
        self.close_callback()
