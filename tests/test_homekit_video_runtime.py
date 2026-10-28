"""Retirement closes admission while keeping correlated cleanup possible."""

import asyncio
from types import SimpleNamespace

import pytest
from test_homekit_bridge import ADAPTER, NODE
from test_homekit_broker import request, setup_broker
from test_homekit_video_broker import VideoOwner, sample
from test_native_service import select, service_fixture

from halo_server.homekit.ipc import HomeKitProtocolError
from halo_server.homekit.models import HomeKitOperationError
from halo_server.homekit.runtime import HomeKitRuntime


@pytest.mark.asyncio
async def test_already_queued_child_work_cannot_acquire_after_runtime_stop(tmp_path):
    broker, boot, _, _, _, _ = await setup_broker(tmp_path)
    owner = broker.live_manager = VideoOwner()
    runtime = HomeKitRuntime(
        NODE,
        ADAPTER / "main.js",
        build_id="retirement-fixture",
        on_retired=broker.retired,
        on_request=broker.request,
    )
    runtime._stopping.set()
    try:
        # Models a task admitted before stop but not scheduled until after it.
        with pytest.raises(HomeKitProtocolError):
            await runtime._dispatch(request(boot, "video_prepare", sample("video_prepare")))
        assert owner.calls == []
    finally:
        await broker.retired(boot)


@pytest.mark.asyncio
async def test_bridge_stop_revokes_scope_before_awaiting_child_cleanup(tmp_path):
    service, client, _ = service_fixture(tmp_path)
    bridge = service._homekit_bridge
    boot, scope = await select(service, streaming=True, controls=True)
    entered, release = asyncio.Event(), asyncio.Event()

    async def stop():
        entered.set()
        await release.wait()

    bridge._runtime = SimpleNamespace(boot_id=boot, stop=stop)
    stopping = asyncio.create_task(bridge.stop())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        with pytest.raises(HomeKitOperationError):
            await service.homekit.set_control(boot, scope, "light", True)
        assert client.submissions == 0 and service.homekit.boot_id is None
    finally:
        release.set()
        await asyncio.wait_for(stopping, 1)
        await service.homekit.detach()
