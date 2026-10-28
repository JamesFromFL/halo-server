"""A queued notification must recheck source authority under its transport lock."""

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest

from halo_server.homekit.runtime import HomeKitRuntime, HomeKitRuntimeError


@pytest.mark.asyncio
async def test_notification_revoked_while_waiting_for_write_lock_never_writes(monkeypatch):
    written = []

    async def write(_writer, envelope):
        written.append(envelope)

    monkeypatch.setattr("halo_server.homekit.runtime.write_frame", write)

    async def retired(_boot):
        pass

    runtime = HomeKitRuntime(
        Path("/usr/bin/node"), Path("/tmp/synthetic.js"), build_id="synthetic", on_retired=retired
    )
    runtime._boot_id = str(uuid4())
    runtime._writer = object()
    valid = [True]
    await runtime._write_lock.acquire()
    queued = asyncio.create_task(
        runtime.send("status", {"state": "ready", "reason": ""}, admission=lambda: valid[0])
    )
    try:
        await asyncio.sleep(0)
        assert not queued.done() and not written
        valid[0] = False
        runtime._write_lock.release()
        with pytest.raises(HomeKitRuntimeError, match="authority was retired"):
            await queued
        assert not written
    finally:
        if runtime._write_lock.locked():
            runtime._write_lock.release()
        await asyncio.gather(queued, return_exceptions=True)
