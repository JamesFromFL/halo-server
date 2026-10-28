"""Scoped bounded HomeKit snapshots, independent of event publication."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import OrderedDict
from dataclasses import dataclass, field

from halo_server.homekit.models import HomeKitOperationError, MediaScope
from halo_server.media.manager import settle_owned

MAX_JOBS = 2
MAX_CLIENTS = 16
MAX_CLIENTS_PER_JOB = 8
MAX_HISTORY = 4096
HISTORY_SECONDS = 60.0
CACHE_BYTES = 16 * 1024 * 1024
CACHE_ENTRIES = 16
IMAGE_SECONDS = 12.0
CLIENT_SECONDS = 14.0
RETIRE_SECONDS = 3.0
CACHE_TTL = {"periodic": 5.0, "unknown": 2.0, "event": 1.0, "explicit": 0.0}


@dataclass(frozen=True)
class SnapshotImage:
    data: bytes = field(repr=False)
    width: int
    height: int
    retrieved_at: float
    reason: str
    fresh_requested: bool
    # The SDK may fall back to its cached image even when fresh=True. Retrieval
    # time is deliberately not represented as a camera capture timestamp.
    capture_time: float | None = None


@dataclass
class _Job:
    key: tuple
    task: asyncio.Task | None = None
    worker: object | None = None
    clients: int = 0
    retired: bool = False
    cleanup_failed: bool = False


@dataclass
class _Client:
    scope: MediaScope
    job: _Job
    cancelled: asyncio.Future


class HomeKitSnapshots:
    """Single-flight only within the exact boot, privacy, reason and dimensions.

    Cancelling a coalesced caller closes only its delivery token. The image job
    remains owned while other callers need it, and retains failed worker cleanup.
    """

    def __init__(self, controller, *, clock=time.monotonic, worker_factory=None):
        self.controller, self._clock = controller, clock
        self._worker_factory = worker_factory
        self.ready = False
        self.capabilities = None
        self._jobs: dict[tuple, _Job] = {}
        self._clients: dict[tuple, _Client] = {}
        self._history: OrderedDict[tuple, float] = OrderedDict()
        self._cache: OrderedDict[tuple, SnapshotImage] = OrderedDict()
        self._cache_bytes = 0
        self._closed = False

    @property
    def pending_resources(self):
        return len(self._jobs)

    @property
    def cleanup_failures(self):
        return sum(job.cleanup_failed for job in self._jobs.values())

    @property
    def cache_bytes(self):
        return self._cache_bytes

    async def qualify(self):
        from halo_server.media.snapshot_codec import snapshot_capabilities

        if self._jobs:
            raise HomeKitOperationError("cleanup_pending")
        self._closed = False
        self.capabilities = await snapshot_capabilities()
        self.ready = self.capabilities.ready
        return self.capabilities

    def _authorize(self, boot, scope):
        if self._closed:
            raise HomeKitOperationError("closed")
        self.controller.authorize(boot, scope, "snapshots")

    def _valid(self, key):
        try:
            self._authorize(key[0], key[1])
        except HomeKitOperationError:
            return False
        return True

    def _prune(self):
        now = self._clock()
        self._history = OrderedDict(
            (identity, deadline) for identity, deadline in self._history.items() if deadline > now
        )
        for key, image in tuple(self._cache.items()):
            if now - image.retrieved_at > CACHE_TTL[image.reason] or not self._valid(key):
                self._cache_bytes -= len(self._cache.pop(key).data)

    def _cache_image(self, key, image):
        if CACHE_TTL[image.reason] == 0:
            return
        previous = self._cache.pop(key, None)
        if previous is not None:
            self._cache_bytes -= len(previous.data)
        while self._cache and (
            len(self._cache) >= CACHE_ENTRIES or self._cache_bytes + len(image.data) > CACHE_BYTES
        ):
            _, old = self._cache.popitem(last=False)
            self._cache_bytes -= len(old.data)
        if len(image.data) <= CACHE_BYTES:
            self._cache[key] = image
            self._cache_bytes += len(image.data)

    async def get(self, boot, scope, job_id, width, height, reason):
        from halo_server.media.jpeg import SnapshotConfiguration

        try:
            configuration = SnapshotConfiguration(width, height)
        except ValueError:
            raise HomeKitOperationError("invalid_request") from None
        if reason not in CACHE_TTL:
            raise HomeKitOperationError("invalid_request")
        self._authorize(boot, scope)  # Always before cache or shared-job lookup.
        if not self.ready:
            raise HomeKitOperationError("not_ready")
        self._prune()
        identity = boot, job_id
        if identity in self._clients or identity in self._history:
            raise HomeKitOperationError("session_conflict")
        if len(self._clients) >= MAX_CLIENTS or len(self._history) >= MAX_HISTORY:
            raise HomeKitOperationError("busy")
        self._history[identity] = self._clock() + HISTORY_SECONDS
        key = boot, scope, width, height, reason
        image = self._cache.get(key)
        if image is not None:
            self._authorize(boot, scope)
            self._cache.move_to_end(key)
            return image
        job = self._jobs.get(key)
        if job is None:
            if len(self._jobs) >= MAX_JOBS:
                raise HomeKitOperationError("busy")
            job = _Job(key)
            self._jobs[key] = job
            job.task = asyncio.create_task(
                self._capture(job, configuration), name="homekit-snapshot-image"
            )
            job.task.add_done_callback(lambda task: self._settled(job, task))
        elif job.retired or job.cleanup_failed:
            raise HomeKitOperationError("cleanup_pending")
        if job.clients >= MAX_CLIENTS_PER_JOB:
            raise HomeKitOperationError("busy")
        client = _Client(scope, job, asyncio.get_running_loop().create_future())
        self._clients[identity] = client
        job.clients += 1
        try:
            done, _ = await asyncio.wait(
                (job.task, client.cancelled),
                timeout=CLIENT_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                raise HomeKitOperationError("timeout")
            if client.cancelled.done():
                raise HomeKitOperationError("cancelled")
            image = job.task.result()
            self._authorize(boot, scope)
            return image
        finally:
            self._clients.pop(identity, None)
            job.clients -= 1
            if not client.cancelled.done():
                client.cancelled.cancel()
            if job.clients == 0 and job.task is not None and not job.task.done():
                job.retired = True
                job.task.cancel()

    async def _capture(self, job, configuration):
        from halo_server.media.snapshot_codec import SnapshotCodecError, SnapshotWorker

        boot, scope, _, _, reason = job.key
        try:
            async with asyncio.timeout(IMAGE_SECONDS):
                self._authorize(boot, scope)
                raw = await self.controller.snapshot(
                    boot, scope, fresh=reason in {"event", "explicit"}
                )
                self._authorize(boot, scope)
                if job.retired:
                    raise HomeKitOperationError("cancelled")
                job.worker = (self._worker_factory or SnapshotWorker)()
                try:
                    data = await job.worker.convert(raw, configuration)
                finally:
                    await self._close_worker(job)
                self._authorize(boot, scope)
                if job.retired:
                    raise HomeKitOperationError("cancelled")
                image = SnapshotImage(
                    data,
                    configuration.width,
                    configuration.height,
                    self._clock(),
                    reason,
                    reason in {"event", "explicit"},
                )
                self._cache_image(job.key, image)
                return image
        except SnapshotCodecError as exc:
            raise HomeKitOperationError(exc.code) from None
        except TimeoutError:
            raise HomeKitOperationError("timeout") from None
        finally:
            if job.worker is not None:
                await self._close_worker(job)

    async def _close_worker(self, job):
        try:
            await job.worker.close()
        except BaseException:
            job.cleanup_failed = True
            raise
        job.worker = None
        job.cleanup_failed = False

    def _settled(self, job, task):
        if not task.cancelled():
            with contextlib.suppress(Exception):
                task.exception()
        if job.worker is None:
            job.cleanup_failed = False
        if not job.cleanup_failed and self._jobs.get(job.key) is job:
            self._jobs.pop(job.key, None)

    def cancel(self, boot, scope, job_id):
        client = self._clients.get((boot, job_id))
        if client is not None and client.scope == scope and not client.cancelled.done():
            client.cancelled.set_result(None)

    def refresh(self):
        self._prune()
        for job in tuple(self._jobs.values()):
            if not self._valid(job.key):
                job.retired = True
                if job.task is not None and not job.task.done():
                    job.task.cancel()
        for (boot, job_id), client in tuple(self._clients.items()):
            if not self._valid((boot, client.scope)):
                self.cancel(boot, client.scope, job_id)

    async def retired(self, boot):
        task = asyncio.create_task(self._retire_owned(boot), name="homekit-snapshot-retire")
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await settle_owned(task)
            raise

    async def _retire_owned(self, boot):
        jobs = tuple(job for job in self._jobs.values() if boot is None or job.key[0] == boot)
        for (owner_boot, job_id), client in tuple(self._clients.items()):
            if boot is None or owner_boot == boot:
                self.cancel(owner_boot, client.scope, job_id)
        for job in jobs:
            job.retired = True
            if job.task is not None and not job.task.done():
                job.task.cancel()
        failures = []
        tasks = tuple(job.task for job in jobs if job.task is not None)
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=RETIRE_SECONDS)
        else:
            pending = set()
        for job in jobs:
            if job.task in pending:
                job.cleanup_failed = True
                failures.append(HomeKitOperationError("cleanup_pending"))
                continue
            if job.worker is not None:
                try:
                    await self._close_worker(job)
                except Exception as exc:
                    failures.append(exc)
                    continue
            if self._jobs.get(job.key) is job:
                self._jobs.pop(job.key)
        for key in tuple(self._cache):
            if boot is None or key[0] == boot:
                self._cache_bytes -= len(self._cache.pop(key).data)
        for key in tuple(self._history):
            if boot is None or key[0] == boot:
                self._history.pop(key)
        if failures:
            raise ExceptionGroup("Snapshot retirement incomplete", failures)

    async def close(self):
        self._closed = True
        await self.retired(None)
