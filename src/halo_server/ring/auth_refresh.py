# SPDX-License-Identifier: GPL-3.0-or-later
# Adapted from JamesFromFL/halo-gtk c2f99aa3c8da3f75181a23201c53fcb972203309.
"""Single shared SDK token rotation owner for camera and hub requests."""

from __future__ import annotations

import asyncio
import contextlib
from contextvars import ContextVar


def install_serialized_auth_refresh(auth) -> None:
    """Serialize token rotation and deduplicate stale in-flight request refreshes."""
    if getattr(auth, "_halo_refresh_serialized", False):
        return

    original_refresh = getattr(auth, "async_refresh_tokens", None)
    if not callable(original_refresh):
        return
    refresh_lock = asyncio.Lock()
    completed_refreshes = 0
    last_result = None
    request_refresh_generation: ContextVar[int | None] = ContextVar(
        "ring_request_refresh_generation",
        default=None,
    )

    @contextlib.contextmanager
    def request_scope():
        """Capture the token generation before either SDK or bounded image I/O."""
        token = request_refresh_generation.set(completed_refreshes)
        try:
            yield
        finally:
            request_refresh_generation.reset(token)

    async def serialized_refresh():
        nonlocal completed_refreshes, last_result
        observed_refreshes = completed_refreshes
        request_generation = request_refresh_generation.get()
        async with refresh_lock:
            if request_generation is not None and completed_refreshes != request_generation:
                return last_result
            if completed_refreshes != observed_refreshes:
                return last_result
            last_result = await original_refresh()
            completed_refreshes += 1
            return last_result

    auth.async_refresh_tokens = serialized_refresh
    auth._halo_auth_request_scope = request_scope
    original_query = getattr(auth, "async_query", None)
    if callable(original_query):

        async def generation_tracked_query(*args, **kwargs):
            with request_scope():
                return await original_query(*args, **kwargs)

        auth.async_query = generation_tracked_query
    auth._halo_refresh_serialized = True
