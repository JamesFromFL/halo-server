"""Image ownership is bounded independently of cancelled/coalesced clients."""

import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest
from test_homekit_controller import configured

from halo_server.homekit.models import HomeKitOperationError
from halo_server.media import snapshots as module
from halo_server.media.snapshots import HomeKitSnapshots


class Worker:
    def __init__(self):
        self.closed = False
        self.fail_close = False
        self.entered = asyncio.Event()
        self.gate = asyncio.Event()
        self.gate.set()

    async def convert(self, data, configuration):
        self.entered.set()
        await self.gate.wait()
        return data + str((configuration.width, configuration.height)).encode()

    async def close(self):
        if self.fail_close:
            raise RuntimeError("Injected native reap failure")
        self.closed = True


async def manager():
    controller, backend, boot, (scope,) = await configured()
    now = [100.0]
    workers = []

    def create():
        worker = Worker()
        workers.append(worker)
        return worker

    images = HomeKitSnapshots(controller, clock=lambda: now[0], worker_factory=create)
    images.ready = True
    return images, controller, backend, boot, scope, workers, now


async def fetch(images, boot, scope, *, reason="periodic", width=640, height=480, identity=None):
    return await images.get(boot, scope, identity or str(uuid4()), width, height, reason)


@pytest.mark.asyncio
async def test_snapshot_cache_checks_policy_first_and_never_claims_camera_capture_age():
    images, controller, backend, boot, scope, workers, now = await manager()
    requests = []
    real = backend.snapshot

    async def observed(scope, fresh, **kwargs):
        requests.append(fresh)
        return await real(scope, fresh, **kwargs)

    backend.snapshot = observed
    try:
        first = await fetch(images, boot, scope)
        assert first.capture_time is None and not first.fresh_requested
        assert first.retrieved_at == 100.0 and requests == [False]
        now[0] += 1
        assert await fetch(images, boot, scope) is first
        assert len(workers) == 1
        # Unknown HAP reason is separate from explicit owner intent.
        unknown = await fetch(images, boot, scope, reason="unknown")
        assert not unknown.fresh_requested and requests == [False, False]
        event = await fetch(images, boot, scope, reason="event")
        assert event.fresh_requested and event.capture_time is None
        explicit = await fetch(images, boot, scope, reason="explicit")
        assert explicit.fresh_requested and requests == [False, False, True, True]
        assert all(worker.closed for worker in workers)
        controller.fence_camera(boot, scope)
        with pytest.raises(HomeKitOperationError):
            await fetch(images, boot, scope)
        assert len(requests) == 4
        images.refresh()
        assert images.cache_bytes == 0
    finally:
        await images.close()
        await controller.detach()


@pytest.mark.asyncio
async def test_one_cancelled_coalesced_client_keeps_the_other_owner_and_image():
    images, controller, backend, boot, scope, workers, _ = await manager()
    backend.gate = asyncio.Event()
    a, b = str(uuid4()), str(uuid4())
    first = asyncio.create_task(fetch(images, boot, scope, identity=a))
    second = asyncio.create_task(fetch(images, boot, scope, identity=b))
    try:
        await backend.entered.wait()
        for _ in range(20):
            if len(images._clients) == 2:
                break
            await asyncio.sleep(0)
        assert images.pending_resources == 1
        images.cancel(boot, replace(scope, privacy_generation=scope.privacy_generation + 1), a)
        assert not first.done()
        images.cancel(boot, scope, a)
        with pytest.raises(HomeKitOperationError) as cancelled:
            await first
        assert cancelled.value.code == "cancelled" and not second.done()
        backend.gate.set()
        result = await second
        assert result.width == 640 and result.height == 480
        assert len(workers) == 1 and workers[0].closed
        assert images.pending_resources == 0
    finally:
        backend.gate.set()
        await asyncio.gather(first, second, return_exceptions=True)
        await images.close()
        await controller.detach()


@pytest.mark.asyncio
async def test_inflight_worker_result_cannot_cross_privacy_epoch():
    images, controller, _, boot, scope, workers, _ = await manager()
    entered, release = asyncio.Event(), asyncio.Event()

    def create():
        worker = Worker()
        worker.entered, worker.gate = entered, release
        workers.append(worker)
        return worker

    images._worker_factory = create
    operation = asyncio.create_task(fetch(images, boot, scope))
    try:
        await entered.wait()
        controller.fence_camera(boot, scope)
        release.set()
        with pytest.raises(HomeKitOperationError):
            await operation
        assert workers[0].closed and images.cache_bytes == 0
    finally:
        release.set()
        await asyncio.gather(operation, return_exceptions=True)
        await images.close()
        await controller.detach()


@pytest.mark.asyncio
async def test_reason_and_geometry_singleflight_are_exact_and_capacity_is_retained():
    images, controller, backend, boot, scope, _, _ = await manager()
    backend.gate = asyncio.Event()
    first = asyncio.create_task(fetch(images, boot, scope, reason="event"))
    second = asyncio.create_task(fetch(images, boot, scope, width=320))
    try:
        await backend.entered.wait()
        for _ in range(20):
            if images.pending_resources == 2:
                break
            await asyncio.sleep(0)
        with pytest.raises(HomeKitOperationError) as busy:
            await fetch(images, boot, scope, reason="explicit")
        assert busy.value.code == "busy" and images.pending_resources == 2
        backend.gate.set()
        a, b = await asyncio.gather(first, second)
        assert a.reason == "event" and b.reason == "periodic" and b.width == 320
    finally:
        backend.gate.set()
        await asyncio.gather(first, second, return_exceptions=True)
        await images.close()
        await controller.detach()


@pytest.mark.asyncio
async def test_failed_worker_close_retains_reservation_until_explicit_recovery():
    images, controller, _, boot, scope, workers, _ = await manager()

    def create():
        worker = Worker()
        worker.fail_close = True
        workers.append(worker)
        return worker

    images._worker_factory = create
    try:
        with pytest.raises(RuntimeError, match="Injected"):
            await fetch(images, boot, scope)
        assert images.pending_resources == images.cleanup_failures == 1
        assert images.cache_bytes == 0
        with pytest.raises(HomeKitOperationError) as pending:
            await fetch(images, boot, scope)
        assert pending.value.code == "cleanup_pending"
        with pytest.raises(ExceptionGroup):
            await images.retired(boot)
        workers[0].fail_close = False
        await images.retired(boot)
        assert images.pending_resources == images.cleanup_failures == 0
        assert workers[0].closed
    finally:
        for worker in workers:
            worker.fail_close = False
        await images.close()
        await controller.detach()


@pytest.mark.asyncio
async def test_replay_history_and_cache_age_are_bounded(monkeypatch):
    images, controller, _, boot, scope, workers, now = await manager()
    monkeypatch.setattr(module, "MAX_HISTORY", 2)
    identity = str(uuid4())
    try:
        first = await fetch(images, boot, scope, identity=identity)
        with pytest.raises(HomeKitOperationError) as replay:
            await fetch(images, boot, scope, identity=identity)
        assert replay.value.code == "session_conflict"
        assert await fetch(images, boot, scope) is first
        with pytest.raises(HomeKitOperationError) as busy:
            await fetch(images, boot, scope)
        assert busy.value.code == "busy"
        now[0] += module.HISTORY_SECONDS + 1
        second = await fetch(images, boot, scope)
        assert second is not first and len(workers) == 2
    finally:
        await images.close()
        await controller.detach()


@pytest.mark.asyncio
async def test_stubborn_fetch_cannot_extend_client_deadline_or_lose_cleanup_owner(monkeypatch):
    images, controller, _, boot, scope, workers, _ = await manager()
    monkeypatch.setattr(module, "IMAGE_SECONDS", 0.01)
    monkeypatch.setattr(module, "CLIENT_SECONDS", 0.03)
    monkeypatch.setattr(module, "RETIRE_SECONDS", 0.01)
    entered, release = asyncio.Event(), asyncio.Event()

    async def stubborn(*args, **kwargs):
        entered.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue
        return b"late image"

    monkeypatch.setattr(controller, "snapshot", stubborn)
    request = asyncio.create_task(fetch(images, boot, scope))
    try:
        await entered.wait()
        with pytest.raises(HomeKitOperationError) as timeout:
            await asyncio.wait_for(request, 0.3)
        assert timeout.value.code == "timeout" and images.pending_resources == 1
        with pytest.raises(ExceptionGroup):
            await asyncio.wait_for(images.retired(boot), 0.3)
        assert images.cleanup_failures == 1 and not workers
        release.set()
        for _ in range(100):
            if images.pending_resources == 0:
                break
            await asyncio.sleep(0)
        assert images.pending_resources == images.cleanup_failures == 0
        assert not workers and images.cache_bytes == 0
    finally:
        release.set()
        await asyncio.gather(request, return_exceptions=True)
        await images.close()
        await controller.detach()
