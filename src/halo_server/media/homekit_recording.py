"""Scoped, pull-driven recording jobs over the shared capture and native muxer.

This owner does not publish HKSV. A consumer must pass the independent HDS
transport gate before using these packets with a real Home controller.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from halo_server.homekit.models import HomeKitOperationError, MediaScope
from halo_server.media.formats import MAX_FRAGMENT_BYTES, MAX_INIT_BYTES, MAX_STAGED_FRAGMENTS

MAX_RECORDINGS = 2
MAX_CAMERA_RECORDINGS = 1
SOURCE_SECONDS = 25.0
IDLE_SECONDS = 5.0
LIFETIME_SECONDS = 180.0
CLEANUP_SECONDS = 5.0


def _uuid(value):
    if not isinstance(value, str):
        raise HomeKitOperationError("invalid_request")
    try:
        if str(UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError) as exc:
        raise HomeKitOperationError("invalid_request") from exc


@dataclass(eq=False)
class _Job:
    boot: str
    scope: MediaScope
    identity: str
    configuration: Any
    event_at: float
    opened_at: float
    allow_reduced: bool
    capture: Any = None
    worker: Any = None
    history: Any = None
    initial: deque = field(default_factory=deque)
    output: deque = field(default_factory=deque)
    cursor: int | None = None
    capture_epoch: int | None = None
    config_epoch: int | None = None
    last_source_at: float = 0
    next_sequence: int = 0
    init_seen: bool = False
    retired: bool = False
    finished: bool = False
    finish_requested: bool = False
    end_cursor: int | None = None
    failure: str | None = None
    acquiring: asyncio.Task | None = None
    pulling: asyncio.Task | None = None
    cleanup: asyncio.Task | None = None
    timer: asyncio.Task | None = None
    cleanup_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class RecordingStream:
    """One finite recording. Pulling a packet is the only way to produce output."""

    def __init__(self, owner, job):
        self._owner, self._job = owner, job

    @property
    def history_ready(self):
        return self._job.history is not None and self._job.history.ready

    @property
    def reduced_reason(self):
        return self._job.history.reduced_reason if self._job.history is not None else "warming"

    @property
    def timing_mode(self):
        return (
            self._job.history.timing_mode if self._job.history is not None else "receipt_estimate"
        )

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self._owner._next(self._job)

    def finish(self):
        """Request a bounded natural EOS; privacy revocation uses close instead."""
        self._owner._finish(self._job)

    async def close(self):
        await self._owner._close_job(self._job)


class HomeKitRecordingManager:
    """Reservations survive failed cleanup; neither packets nor work queue grow.

    Full pre-roll is strict by default. ``allow_reduced`` is a trusted internal
    experimental choice, never inferred from a cold source or an HAP request.
    Warm capture opt-in belongs to the separate trusted capture-policy owner.
    """

    def __init__(
        self, controller, *, capture_manager=None, worker_factory=None, clock=time.monotonic
    ):
        from halo_server.media.recording_capture import RecordingCaptureManager

        self.controller = controller
        self.captures = capture_manager or RecordingCaptureManager(controller)
        self._worker_factory = worker_factory
        self._clock = clock
        self._jobs: dict[str, _Job] = {}
        self._closed = False
        self._lifecycle = 0
        self._qualifying = asyncio.Lock()
        self._closing = asyncio.Lock()
        self.ready = False
        self.capabilities = None

    @property
    def pending_resources(self):
        return len(self._jobs)

    @property
    def cleanup_failures(self):
        return sum(job.failure is not None for job in self._jobs.values())

    async def qualify(self):
        from halo_server.media.recording import RecordingCapabilities, recording_capabilities

        if self._jobs or self.captures.pending_resources or self._closing.locked():
            if self._closed:
                self.ready = False
            return RecordingCapabilities(False, "cleanup_pending" if self._closed else "busy")
        if self._qualifying.locked():
            return RecordingCapabilities(False, "busy")
        async with self._qualifying:
            lifecycle = self._lifecycle
            if self._closed:
                self.ready = False
            capabilities = await recording_capabilities()
            if lifecycle != self._lifecycle or self._closing.locked():
                return RecordingCapabilities(False, "closed")
            if self._jobs or self.captures.pending_resources:
                return RecordingCapabilities(False, "busy")
            if capabilities.ready:
                self.captures.reopen()  # Only after actual empty-owner proof.
                self._closed = False
            self.capabilities = capabilities
            self.ready = capabilities.ready
            return capabilities

    def _current(self, job):
        if self._closed or job.retired or self._jobs.get(job.identity) is not job:
            raise HomeKitOperationError("authority_changed")
        self.controller.authorize(job.boot, job.scope, "recording")
        if job.configuration.audio is not None:
            self.controller.authorize(job.boot, job.scope, "recording_audio")
        if self._clock() - job.opened_at >= LIFETIME_SECONDS:
            raise HomeKitOperationError("session_expired")
        if job.capture is not None:
            if job.capture.retired:
                raise HomeKitOperationError("authority_changed")
            if job.capture_epoch is not None and job.capture.current_epochs != (
                job.capture_epoch,
                job.config_epoch,
            ):
                raise HomeKitOperationError("source_changed")

    def _valid(self, job):
        try:
            self._current(job)
            return True
        except HomeKitOperationError:
            return False

    async def open(self, boot, scope, job_id, configuration, *, event_at=None, allow_reduced=False):
        from halo_server.media.recording import RecordingConfiguration

        _uuid(boot)
        _uuid(job_id)
        if (
            not isinstance(scope, MediaScope)
            or not isinstance(configuration, RecordingConfiguration)
            or type(allow_reduced) is not bool
        ):
            raise HomeKitOperationError("invalid_request")
        now = self._clock()
        if event_at is None:
            event_at = now
        if type(event_at) not in {int, float} or not 0 <= event_at <= now or now - event_at > 12:
            raise HomeKitOperationError("invalid_request")
        if self._closed:
            raise HomeKitOperationError("closed")
        if not self.ready:
            raise HomeKitOperationError("not_ready")
        if self._qualifying.locked():
            raise HomeKitOperationError("busy")
        self.controller.authorize(boot, scope, "recording")
        if configuration.audio is not None:
            self.controller.authorize(boot, scope, "recording_audio")
        if job_id in self._jobs:
            raise HomeKitOperationError("session_conflict")
        if (
            len(self._jobs) >= MAX_RECORDINGS
            or sum(j.scope.source_key == scope.source_key for j in self._jobs.values())
            >= MAX_CAMERA_RECORDINGS
        ):
            raise HomeKitOperationError("capacity")
        job = _Job(boot, scope, job_id, configuration, float(event_at), now, allow_reduced)
        self._jobs[job_id] = job
        job.timer = asyncio.create_task(self._expire(job), name="homekit-recording-expiry")
        job.acquiring = asyncio.create_task(self._acquire(job), name="homekit-recording-capture")
        try:
            done, _ = await asyncio.wait({job.acquiring}, timeout=SOURCE_SECONDS)
            if not done:
                raise HomeKitOperationError("source_timeout")
            await job.acquiring
            self._current(job)
            return RecordingStream(self, job)
        except BaseException:
            job.retired = True
            with contextlib.suppress(Exception):
                await self._close_job(job)
            raise

    async def _acquire(self, job):
        job.capture = await self.captures.open(
            job.boot, job.scope, job.identity, audio=job.configuration.audio is not None
        )
        self._current(job)
        history = job.capture.snapshot(event_at=job.event_at)
        # The strict mode must fail at this frozen event boundary. Waiting four
        # seconds after the event cannot manufacture pre-event footage.
        if not history.ready and not job.allow_reduced:
            raise HomeKitOperationError("prebuffer_unavailable")
        while not history.samples or not history.video_sps or not history.video_pps:
            self._current(job)
            if self._clock() - job.opened_at >= SOURCE_SECONDS:
                raise HomeKitOperationError("source_timeout")
            await job.capture.read_since(history.cursor, timeout=0.25)
            history = job.capture.snapshot(event_at=job.event_at)
        job.history = history
        job.initial.extend(history.samples)
        job.cursor = history.cursor
        job.capture_epoch, job.config_epoch = history.capture_epoch, history.config_epoch
        job.last_source_at = self._clock()
        from halo_server.media.recording import RecordingWorker

        factory = self._worker_factory or RecordingWorker
        self._current(job)
        job.worker = factory(
            job.configuration,
            video_sps=history.video_sps,
            video_pps=history.video_pps,
            source_clocks=history.source_clocks,
            capture_epoch=history.capture_epoch,
            config_epoch=history.config_epoch,
            valid=lambda: self._valid(job),
        )
        self._current(job)

    def _finish(self, job):
        self._current(job)
        if not job.finish_requested and not job.finished:
            # Freeze exactly the source tail already admitted at EOS. Future
            # camera samples cannot extend a finished event indefinitely.
            job.end_cursor = job.capture.current_cursor
            job.finish_requested = True

    async def _next(self, job):
        if job.retired:
            raise StopAsyncIteration
        if job.finished and not job.output:
            await self._close_job(job)
            raise StopAsyncIteration
        try:
            self._current(job)
        except HomeKitOperationError:
            with contextlib.suppress(Exception):
                await self._close_job(job)
            raise
        if job.pulling is not None:
            raise HomeKitOperationError("busy")
        task = asyncio.create_task(self._pull(job), name="homekit-recording-pull")
        job.pulling = task
        try:
            packet = await asyncio.shield(task)
            self._current(job)  # Final dequeue fence, including a paused consumer.
            return packet
        except BaseException:
            job.retired = True
            with contextlib.suppress(Exception):
                await self._close_job(job)
            raise
        finally:
            if job.pulling is task and task.done():
                job.pulling = None

    def _stage(self, job, packets):
        if not isinstance(packets, tuple) or len(packets) > MAX_STAGED_FRAGMENTS:
            raise HomeKitOperationError("invalid_recording_output")
        self._current(job)
        for packet in packets:
            if (
                packet.kind not in {"init", "fragment"}
                or not isinstance(packet.payload, bytes)
                or not packet.payload
                or packet.sequence != job.next_sequence
            ):
                raise HomeKitOperationError("invalid_recording_output")
            if packet.kind == "init":
                if job.init_seen or packet.sequence != 0 or len(packet.payload) > MAX_INIT_BYTES:
                    raise HomeKitOperationError("invalid_recording_output")
                job.init_seen = True
            elif (
                not job.init_seen
                or len(packet.payload) > MAX_FRAGMENT_BYTES
                or not 0 < packet.duration_seconds <= job.configuration.fragment_length_ms / 1000
            ):
                raise HomeKitOperationError("invalid_recording_output")
            if len(job.output) >= MAX_STAGED_FRAGMENTS:
                raise HomeKitOperationError("invalid_recording_output")
            job.next_sequence += 1
            job.output.append(packet)

    async def _pull(self, job):
        while True:
            self._current(job)
            if job.output:
                return job.output.popleft()
            if job.finished:
                raise StopAsyncIteration
            if job.initial:
                self._stage(job, await job.worker.feed(job.initial.popleft()))
                continue
            if job.finish_requested and job.cursor >= job.end_cursor:
                self._stage(job, await job.worker.finish())
                job.finish_requested = False
                job.finished = True
                if job.output:
                    return job.output.popleft()
                raise StopAsyncIteration
            snapshot = await job.capture.read_since(job.cursor, timeout=0.25)
            self._current(job)
            if (snapshot.capture_epoch, snapshot.config_epoch) != (
                job.capture_epoch,
                job.config_epoch,
            ):
                raise HomeKitOperationError("source_changed")
            job.cursor = snapshot.cursor
            samples = snapshot.samples
            if job.finish_requested:
                samples = tuple(sample for sample in samples if sample.sequence <= job.end_cursor)
            if samples:
                job.last_source_at = self._clock()
                job.initial.extend(samples)
            elif self._clock() - job.last_source_at >= IDLE_SECONDS:
                raise HomeKitOperationError("source_timeout")

    async def _expire(self, job):
        await asyncio.sleep(LIFETIME_SECONDS)
        with contextlib.suppress(Exception):
            await self._close_job(job)

    async def _close_job(self, job):
        job.retired = True  # Install the delivery fence before any await.
        job.initial.clear()
        job.output.clear()
        for task in (job.acquiring, job.pulling, job.timer):
            if task is not None and task is not asyncio.current_task() and not task.done():
                task.cancel()
        if job.cleanup is None or job.cleanup.done():
            job.cleanup = asyncio.create_task(self._cleanup(job), name="homekit-recording-cleanup")
            job.cleanup.add_done_callback(_observed)
        done, _ = await asyncio.wait({job.cleanup}, timeout=CLEANUP_SECONDS)
        if not done:
            job.failure = "cleanup_pending"
            raise HomeKitOperationError("cleanup_pending")
        await asyncio.shield(job.cleanup)

    async def _cleanup(self, job):
        async with job.cleanup_lock:
            failures, attempted = [], set()

            async def release_resources():
                for name in ("worker", "capture"):
                    resource = getattr(job, name)
                    if resource is None or id(resource) in attempted:
                        continue
                    attempted.add(id(resource))
                    try:
                        await resource.close()
                    except (Exception, asyncio.CancelledError) as exc:
                        failures.append(exc)
                    else:
                        setattr(job, name, None)

            # Closing native input/output unblocks an in-flight pull. Waiting
            # for that pull before stopping the native owner can deadlock.
            await release_resources()
            tasks = [task for task in (job.acquiring, job.pulling) if task is not None]
            if tasks:
                done, pending = await asyncio.wait(tasks, timeout=CLEANUP_SECONDS)
                for task in done:
                    with contextlib.suppress(BaseException):
                        task.result()
                if pending:
                    failures.append(HomeKitOperationError("cleanup_pending"))
            # A cancelled acquisition may return a late resource. It remains
            # charged until this owner has joined and closed it too.
            await release_resources()
            if failures:
                job.failure = "cleanup_pending"
                raise HomeKitOperationError("cleanup_pending")
            job.history = None
            job.failure = None
            if self._jobs.get(job.identity) is job:
                self._jobs.pop(job.identity)

    async def retired(self, boot):
        jobs = [job for job in self._jobs.values() if job.boot == boot]
        for job in jobs:
            job.retired = True
        results = await asyncio.gather(
            *(self._close_job(job) for job in jobs), return_exceptions=True
        )
        await self.captures.retired(boot)
        if any(isinstance(result, BaseException) for result in results):
            raise HomeKitOperationError("cleanup_pending")

    async def refresh(self):
        jobs = [job for job in self._jobs.values() if not self._valid(job)]
        results = await asyncio.gather(
            *(self._close_job(job) for job in jobs), return_exceptions=True
        )
        await self.captures.refresh()
        if any(isinstance(result, BaseException) for result in results):
            raise HomeKitOperationError("cleanup_pending")

    async def close(self):
        self._closed = True
        self.ready = False
        self._lifecycle += 1  # A late probe must never undo this stop fence.
        async with self._closing:
            results = await asyncio.gather(
                *(self._close_job(job) for job in tuple(self._jobs.values())),
                return_exceptions=True,
            )
            await self.captures.close()
            if any(isinstance(result, BaseException) for result in results):
                raise HomeKitOperationError("cleanup_pending")


def _observed(task):
    if not task.cancelled():
        task.exception()
