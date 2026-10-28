"""Ownership barriers shared by every integration using Ring capture."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from typing import Any, TypeVar

from halo_server.media.leases import LeasePurpose

_T = TypeVar("_T")


async def settle_owned(task: asyncio.Task[_T]) -> _T:
    """Join an already owned operation despite cancellation of its caller.

    Canceling an asyncio wrapper cannot stop a running worker thread. The
    admission lock must outlive that worker and its compensating release.
    """
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                return task.result()


async def owned_thread(call: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
    """Keep a synchronous resource mutation joined before releasing its lock."""
    task = asyncio.create_task(asyncio.to_thread(call, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        with contextlib.suppress(Exception):
            await settle_owned(task)
        raise


async def acquire_owned(session: Any, purpose: LeasePurpose, lease_id: str) -> Any:
    """Release a late or partially failed lease before admission can reopen."""
    task = asyncio.create_task(asyncio.to_thread(session.acquire, purpose, lease_id=lease_id))
    try:
        return await asyncio.shield(task)
    except BaseException:
        with contextlib.suppress(Exception):
            await settle_owned(task)
        release = asyncio.create_task(asyncio.to_thread(session.release, lease_id))
        await settle_owned(release)
        raise


class CaptureRegistry:
    """Retain retiring receivers so a timed out stop cannot admit a duplicate."""

    def __init__(self) -> None:
        self.active: dict[str, Any] = {}
        self.retiring: dict[str, Any] = {}

    def can_admit(self, camera_id: str) -> bool:
        previous = self.retiring.get(camera_id)
        if previous is None:
            return True
        if getattr(previous, "receiver_retired", False):
            self.retiring.pop(camera_id)
            return True
        return False

    def retire(self, camera_id: str, session: Any) -> None:
        previous = self.retiring.get(camera_id)
        if previous is not None and previous is not session:
            raise RuntimeError("camera already has a retiring capture")
        if self.active.get(camera_id) is session:
            self.active.pop(camera_id)
        self.retiring[camera_id] = session

    def finish_retirement(self, camera_id: str, session: Any) -> None:
        if self.retiring.get(camera_id) is not session:
            return
        retired = getattr(session, "receiver_retired", None)
        # Compatibility test doubles have no receiver future. Production
        # sessions must explicitly attest that teardown actually completed.
        if retired is True or (retired is None and session.snapshot().state == "idle"):
            self.retiring.pop(camera_id)
