"""One bounded, inclusive ciphertext-identity repair queue per live owner."""

from __future__ import annotations

import asyncio
import math
from collections import OrderedDict, deque

MAX_REPAIR_PACKETS = 64
MAX_REPAIR_BYTES = MAX_REPAIR_PACKETS * 1200
REPAIR_WINDOW_SECONDS = 1.0


class RepairRate:
    """Independent rolling request/successful-send budgets; metadata only."""

    def __init__(self):
        self._rows = deque()
        self._bytes = 0
        self._last = 0.0

    def permits(self, now, size):
        if (
            type(now) not in {int, float}
            or not 0 <= now <= 2**48
            or not math.isfinite(now)
            or now < self._last
            or type(size) is not int
            or not 1 <= size <= 1200
        ):
            return False
        self._last = now
        while self._rows and self._rows[0][0] <= now - REPAIR_WINDOW_SECONDS:
            self._bytes -= self._rows.popleft()[1]
        return len(self._rows) < MAX_REPAIR_PACKETS and self._bytes + size <= MAX_REPAIR_BYTES

    def record(self, now, size):
        # Same-loop code calls permits immediately before admission, with no
        # intervening await. Success is recorded only after the actual syscall.
        if not self.permits(now, size):
            raise RuntimeError("Repair budget admission is no longer current")
        self._last = now
        self._rows.append((now, size))
        self._bytes += size


class RepairQueue:
    """Cache objects are shared, never copied or re-encrypted here.

    The current item stays charged until finish, even across clear/fence. Only
    this owner loop uses the queue; no per-NACK task or cross-thread callback.
    """

    def __init__(self):
        self._entries = OrderedDict()
        self._ready = deque()
        self._current = None
        self._bytes = 0
        self._closed = False
        self._wake = asyncio.Event()

    def enqueue(self, sequence, cached):
        if self._closed:
            return "closed"
        previous = self._entries.get(sequence)
        if previous is not None:
            return "duplicate" if previous is cached else "full"
        size = len(cached[1])
        if (
            len(self._entries) >= MAX_REPAIR_PACKETS
            or not 1 <= size <= 1200
            or self._bytes + size > MAX_REPAIR_BYTES
        ):
            return "full"
        self._entries[sequence] = cached
        self._bytes += size
        self._ready.append(sequence)
        self._wake.set()
        return "queued"

    async def take(self):
        if self._current is not None:
            raise RuntimeError("Repair queue already owns an in-flight item")
        while not self._closed:
            if self._ready:
                sequence = self._ready.popleft()
                cached = self._entries[sequence]
                self._current = sequence, cached
                return self._current
            self._wake.clear()
            await self._wake.wait()
        return None

    def finish(self, item):
        if self._current is not item:
            return
        sequence, cached = item
        self._current = None
        if self._entries.get(sequence) is cached:
            del self._entries[sequence]
            self._bytes -= len(cached[1])

    def clear(self):
        for sequence in self._ready:
            cached = self._entries.pop(sequence)
            self._bytes -= len(cached[1])
        self._ready.clear()

    def fence(self):
        self._closed = True
        self.clear()
        self._wake.set()

    def snapshot(self):
        return {
            "pending_packets": len(self._entries),
            "pending_bytes": self._bytes,
            "in_flight": self._current is not None,
            "closed": self._closed,
            "maximum_packets": MAX_REPAIR_PACKETS,
            "maximum_bytes": MAX_REPAIR_BYTES,
        }
