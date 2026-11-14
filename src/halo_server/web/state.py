"""One bounded projection reader shared by all browser tabs and SSE clients."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import time
from collections import deque
from uuid import uuid4

from halo_server.web.errors import WebError

MAX_STATE_BYTES = 128 * 1024
POLL_SECONDS = 2.0


class StateHub:
    def __init__(self, client, pool, *, clock=time.monotonic, wall_clock=time.time):
        self.client, self.pool = client, pool
        self.clock, self.wall_clock = clock, wall_clock
        self.boot_id = str(uuid4())
        self.sequence = 0
        self.current = None
        self.last_read = -POLL_SECONDS
        self.task = None
        self.poller = None
        self.closed = False
        self.history = deque(maxlen=256)
        self.subscribers = set()
        self.on_epoch = lambda _epoch: None
        self.on_disconnect = lambda: None
        self.web_status = lambda: {"draining": False, "pending_operations": 0}

    def _snapshot(self, value):
        result = copy.deepcopy(value)
        result["web"].update(self.web_status())
        return result

    async def get(self, *, force=False):
        if self.closed:
            raise WebError("unavailable", "The web console is stopping", status=503)
        if self.task is not None and not self.task.done():
            return self._snapshot(await asyncio.shield(self.task))
        if not force and self.current is not None and self.clock() - self.last_read < POLL_SECONDS:
            return self._snapshot(self.current)
        self.task = asyncio.create_task(self._read(), name="web-shared-state")
        self.task.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
        return self._snapshot(await asyncio.shield(self.task))

    async def _read(self):
        previous = self.current.get("state") if self.current else None
        try:
            async with asyncio.timeout(3):
                result = await self.pool.state(self.client.owner_state)
            if (
                not isinstance(result, dict)
                or result.get("schema") != 1
                or not isinstance(result.get("epoch"), str)
                or len(json.dumps(result, separators=(",", ":")).encode()) > MAX_STATE_BYTES
            ):
                raise ValueError
            observation = result.get("observed_at")
            daemon = {"available": True, "stale": False, "observed_at": observation, "error": None}
        except Exception:
            result = previous
            self.on_disconnect()
            daemon = {
                "available": False,
                "stale": True,
                "observed_at": self.current["daemon"]["observed_at"] if self.current else None,
                "error": "daemon_unavailable",
            }
        epoch = result.get("epoch") if result else None
        changed = previous is not None and epoch != previous.get("epoch")
        if changed:
            self.history.clear()
            for queue in self.subscribers:
                while not queue.empty():
                    queue.get_nowait()
            self.on_epoch(epoch)
        self.sequence += 1
        self.last_read = self.clock()
        self.current = {
            "schema": 1,
            "web": {"boot_id": self.boot_id, "observed_at": self.wall_clock()} | self.web_status(),
            "daemon": daemon,
            "state": result,
            "cursor": f"{self.boot_id}:{self.sequence}",
        }
        event = ("resync" if changed else "state", copy.deepcopy(self.current))
        self.history.append(event)
        for queue in tuple(self.subscribers):
            if queue.full():
                self.subscribers.discard(queue)
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait(None)
            else:
                queue.put_nowait(event)
        return self.current

    async def subscribe(self, cursor=None):
        if len(self.subscribers) >= 8:
            raise WebError("capacity", "Too many live dashboard connections", status=429)
        current = await self.get()
        # More clients may have subscribed while the shared read completed.
        if len(self.subscribers) >= 8:
            raise WebError("capacity", "Too many live dashboard connections", status=429)
        queue = asyncio.Queue(maxsize=16)
        initial = [("state", current)]
        if cursor:
            indexes = [
                index for index, (_, row) in enumerate(self.history) if row["cursor"] == cursor
            ]
            if indexes:
                initial = list(self.history)[indexes[-1] + 1 :]
                if len(initial) > 16:
                    initial = [("resync", current)]
            else:
                initial = [("resync", current)]
        for event in initial:
            queue.put_nowait(event)
        self.subscribers.add(queue)
        if self.poller is None or self.poller.done():
            self.poller = asyncio.create_task(self._poll(), name="web-state-feed")
        return queue

    def unsubscribe(self, queue):
        self.subscribers.discard(queue)

    async def _poll(self):
        while self.subscribers and not self.closed:
            await asyncio.sleep(POLL_SECONDS)
            if self.subscribers and not self.closed:
                await self.get()

    async def close(self):
        self.closed = True
        for queue in self.subscribers:
            while not queue.empty():
                queue.get_nowait()
            queue.put_nowait(None)
        self.subscribers.clear()
        if self.poller:
            self.poller.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.poller
        if self.task and not self.task.done():
            await asyncio.wait((self.task,))
