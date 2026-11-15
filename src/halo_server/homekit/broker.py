"""Boot-scoped native operations between the HAP child and Python resource owners."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import math
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any
from uuid import uuid4

from halo_server.homekit.controller import HomeKitController
from halo_server.homekit.ipc import (
    BULK_LIMITS,
    MAX_INVENTORY_PAGE,
    HomeKitProtocolError,
    encode_frame,
    validate_direction,
)
from halo_server.homekit.models import CameraPolicy, HomeKitOperationError, MediaScope

MAX_CURSOR_PAGES = 4
CURSOR_SECONDS = 10.0
MAX_WRITE_OPERATIONS = 4096
MAX_SESSION_IDENTITIES = 512
RETIRED_SESSION_SECONDS = 60.0
RECORDING_POSTROLL_SECONDS = 2.0
RECORDING_EVENT_SECONDS = 150.0
RECORDING_TRANSFER_SECONDS = 5.0


@dataclass(frozen=True)
class RecordingEvent:
    generation: int
    event_at: float
    end_at: float

    def __post_init__(self):
        if (
            type(self.generation) is not int
            or self.generation < 1
            or any(
                type(value) not in {int, float} or not math.isfinite(value)
                for value in (self.event_at, self.end_at)
            )
            or not 0 <= self.event_at <= self.end_at
        ):
            raise HomeKitOperationError("invalid_event")


@dataclass
class _Resource:
    scope: MediaScope
    kind: str
    handle: Any
    deadline: float


@dataclass
class _SnapshotDelivery:
    boot: str
    scope: MediaScope
    identity: str
    transfer_id: str
    image: Any = field(default=None, repr=False)
    prepared: Any = field(default=None, repr=False)
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    cancelled: bool = False
    task: asyncio.Task | None = None
    request_task: asyncio.Task | None = None
    cleanup_failed: bool = False


@dataclass
class _RecordingJob:
    boot: str
    scope: MediaScope
    identity: str
    configuration: Any
    privacy_signature: tuple
    event: RecordingEvent
    opened_at: float
    finish_requested: bool = False
    stream: Any = field(default=None, repr=False)
    request_task: asyncio.Task | None = None
    pulling: asyncio.Task | None = None
    delivery: _SnapshotDelivery | None = None
    sequence: int = 0
    cancelled: bool = False
    cleanup_failed: bool = False
    closing: asyncio.Lock = field(default_factory=asyncio.Lock)


class HomeKitBroker:
    """Own private leases, with no accessory publication or implicit selection.

    Selected scopes come from trusted Python configuration (S07), never a child
    inventory query. S06 defaults to no selected cameras. Tests inject explicitly
    selected synthetic devices without changing that production default.
    """

    def __init__(
        self,
        controller: HomeKitController,
        backend,
        *,
        selection: Callable = lambda: (),
        changed: Callable[[], None] = lambda: None,
        live_manager=None,
        snapshot_manager=None,
        audio_settings: Callable | None = None,
        recording_manager=None,
        recording_authority: Callable | None = None,
        recording_policy=None,
        recording_event: Callable | None = None,
    ) -> None:
        self.controller, self.backend = controller, backend
        self._selection, self._changed = selection, changed
        self.live_manager = live_manager
        self.snapshot_manager = snapshot_manager
        self._snapshot_jobs: dict[str, _SnapshotDelivery] = {}
        self._audio_settings = audio_settings
        self.recording_manager = recording_manager
        self._recording_authority = recording_authority
        self.recording_policy = recording_policy
        self._recording_event = recording_event
        self._recording_jobs: dict[str, _RecordingJob] = {}
        self._recording_history: dict[str, tuple[MediaScope, float]] = {}
        self.runtime = None
        self.boot_id: str | None = None
        self._account = None
        self._selection_keys = ()
        self._retiring_boot = None
        self._resources: dict[str, _Resource] = {}
        self._session_ids: dict[str, float | None] = {}
        self._pages: OrderedDict[str, tuple[Any, int, float]] = OrderedDict()
        self._writes: set[str] = set()
        self._reaper: asyncio.Task | None = None
        self._refresh_lock = asyncio.Lock()
        self.cleanup_failed = False

    @property
    def recording_resources(self):
        return len(self._recording_jobs)

    @property
    def recording_cleanup_failures(self):
        return sum(job.cleanup_failed for job in self._recording_jobs.values())

    async def ready(self, boot_id: str) -> None:
        async with self._refresh_lock:
            if (
                self.boot_id is not None
                or self._retiring_boot is not None
                or self._resources
                or self._snapshot_jobs
                or self._recording_jobs
                or self.cleanup_failed
            ):
                raise HomeKitOperationError("cleanup_pending")
            self.boot_id = boot_id
            selection = tuple(self._selection())
            await self.controller.attach(boot_id, selection)
            self._selection_keys = tuple(scope.source_key for scope in selection)
            if self.boot_id != boot_id:
                raise HomeKitOperationError("boot_changed")
            self._account = _account(self.backend.inventory())
            self._reaper = asyncio.create_task(self._expire(), name="homekit-lease-expiry")
            await self._refresh_locked()

    async def refresh(self) -> None:
        async with self._refresh_lock:
            await self._refresh_locked()

    async def _refresh_locked(self) -> None:
        boot = self.boot_id
        if boot is None or self.runtime is None:
            return
        account = _account(self.backend.inventory())
        selection = tuple(self._selection())
        keys = tuple(scope.source_key for scope in selection)
        if (
            account != self._account
            or keys != self._selection_keys
            or self.controller.boot_id != boot
        ):
            self.controller.fence()
            await self.controller.detach()
            if self.boot_id != boot:
                return
            await self._close_resources()
            if self.boot_id != boot:
                return
            await self.controller.attach(boot, selection)
            self._selection_keys = keys
            if self.boot_id != boot:
                return
            self._account = account
        inventory = await self.controller.reconcile()
        self._pages.clear()
        if self.boot_id == boot:
            await self.runtime.send(
                "inventory_reset",
                {
                    "scope": _account(inventory),
                    "revision": inventory.revision,
                },
            )

    async def retired(self, boot_id: str) -> None:
        if boot_id not in (self.boot_id, self._retiring_boot):
            return
        self._retiring_boot = boot_id
        self.boot_id = None  # Fence before waiting for a concurrent refresh.
        async with self._refresh_lock:
            if self._retiring_boot != boot_id or self.boot_id is not None:
                return
            self._pages.clear()
            reaper, self._reaper = self._reaper, None
            if reaper is not None:
                reaper.cancel()
                await asyncio.gather(reaper, return_exceptions=True)
            failures = []
            for operation in (self._close_resources(), self.controller.detach()):
                try:
                    await operation
                except Exception as exc:
                    failures.append(exc)
            self._writes.clear()
            self._account = None
            self.cleanup_failed = bool(failures)
            if failures:
                raise ExceptionGroup("HomeKit resource retirement incomplete", failures)
            self._retiring_boot = None
            self._session_ids.clear()
            self._recording_history.clear()

    async def _close_resources(self) -> None:
        failures = []
        try:
            await self._close_recordings()
        except Exception as exc:
            failures.append(exc)
        try:
            await self._close_snapshots()
        except Exception as exc:
            failures.append(exc)
        for identity, resource in tuple(self._resources.items()):
            try:
                await resource.handle.close()
            except Exception as exc:
                failures.append(exc)
            else:
                self._forget_resource(identity, resource)
        if failures:
            self.cleanup_failed = True
            raise ExceptionGroup("Native resource cleanup failed", failures)

    async def _expire(self) -> None:
        try:
            while True:
                await asyncio.sleep(0.1)
                now = asyncio.get_running_loop().time()
                current = {camera.scope for camera in self.controller.inventory().cameras}
                for job in tuple(self._recording_jobs.values()):
                    if not self._recording_current(job):
                        # A natural terminal pull retires native input before
                        # returning StopAsyncIteration. Keep its EOS reply
                        # owned while teardown joins; privacy remains fenced.
                        if (
                            job.finish_requested
                            and job.pulling is not None
                            and self._recording_authorized(job)
                        ):
                            continue
                        try:
                            await self._stop_recording(job)
                        except Exception:
                            self.cleanup_failed = True
                            self._changed()
                            return
                    elif job.stream is not None and not job.finish_requested:
                        event = self._recording_event(job.scope) if self._recording_event else None
                        if (
                            isinstance(event, RecordingEvent)
                            and event.generation == job.event.generation
                        ):
                            job.event = event
                        if (
                            isinstance(event, RecordingEvent)
                            and event.generation != job.event.generation
                            or now >= job.event.end_at + RECORDING_POSTROLL_SECONDS
                            or now - job.opened_at >= RECORDING_EVENT_SECONDS
                        ):
                            try:
                                job.stream.finish()
                                job.finish_requested = True
                            except HomeKitOperationError:
                                try:
                                    await self._stop_recording(job)
                                except Exception:
                                    self.cleanup_failed = True
                                    self._changed()
                                    return
                for delivery in tuple(self._snapshot_jobs.values()):
                    if not self._snapshot_current(delivery) and not delivery.cancelled:
                        try:
                            await self._cancel_snapshot(delivery)
                        except Exception:
                            self.cleanup_failed = True
                            self._changed()
                            return
                for identity, resource in tuple(self._resources.items()):
                    if now >= resource.deadline or resource.scope not in current:
                        try:
                            await resource.handle.close()
                        except Exception:
                            self.cleanup_failed = True
                            # Retain the claim and terminate this boot; never spin
                            # a failed close or reopen a source behind it.
                            self._changed()
                            return
                        self._forget_resource(identity, resource)
        except asyncio.CancelledError:
            raise

    def _require_boot(self, envelope: dict) -> str:
        validate_direction(envelope, "hap")
        if self.boot_id is None or envelope["boot_id"] != self.boot_id:
            raise HomeKitProtocolError("retired broker boot")
        if self.cleanup_failed:
            raise HomeKitProtocolError("broker cleanup is incomplete")
        return self.boot_id

    async def request(self, envelope: dict) -> dict:
        boot = self._require_boot(envelope)
        kind, data = envelope["kind"], envelope["payload"]
        if kind in {"video_prepare", "video_start", "video_reconfigure", "video_stop"}:
            return await self._video_request(boot, kind, data)
        if kind in {"recording_open", "recording_next", "recording_stop"}:
            return await self._recording_request(boot, kind, data)
        if kind == "inventory_request":
            try:
                return self._inventory(envelope)
            except HomeKitOperationError as exc:
                return {**envelope, "kind": "error", "payload": {"code": _code(exc), "message": ""}}
        if kind == "policy_update":
            try:
                scope, policy = _scope(data), CameraPolicy(**data["policy"])
                self._authorize_audio_policy(scope, policy)
                self._authorize_recording_policy(scope, policy)
                updated = await self.controller.update_policy(boot, scope, policy)
            except HomeKitOperationError as exc:
                return {"scope": data["scope"], "accepted": False, "code": _code(exc)}
            self._pages.clear()
            if updated != scope:
                self._changed()
            return {"scope": data["scope"], "accepted": True, "code": "ok"}
        if kind in {"lease_request", "lease_renew", "lease_stop", "talkback_request"}:
            return await self._resource_request(boot, kind, data)
        if kind == "snapshot_request":
            return await self._snapshot_request(boot, data)
        if kind == "control_request":
            result = {"scope": data["scope"], "operation_id": data["operation_id"]}
            if data["operation_id"] in self._writes:
                return result | {"status": "rejected", "code": "conflict"}
            if len(self._writes) >= MAX_WRITE_OPERATIONS:
                return result | {"status": "rejected", "code": "busy"}
            self._writes.add(data["operation_id"])
            try:
                await self.controller.set_control(
                    boot, _scope(data), data["control"], data["enabled"]
                )
            except HomeKitOperationError as exc:
                return result | {
                    "status": "unknown" if exc.possibly_sent else "rejected",
                    "code": _code(exc),
                }
            return result | {"status": "accepted", "code": "ok"}
        raise HomeKitProtocolError("unsupported child request")

    def _snapshot_current(self, delivery):
        if delivery.cancelled or self.boot_id != delivery.boot or self.cleanup_failed:
            return False
        try:
            self.controller.authorize(delivery.boot, delivery.scope, "snapshots")
        except HomeKitOperationError:
            return False
        return True

    async def _snapshot_request(self, boot, data):
        from halo_server.homekit.bulk import BulkTransferError

        result = {"scope": data["scope"], "job_id": data["job_id"], "transfer_id": None}
        channel = self.runtime.bulk_channel if self.runtime is not None else None
        if self.snapshot_manager is None or not self.snapshot_manager.ready or channel is None:
            return result | {"status": "rejected", "code": "not-ready"}
        if data["job_id"] in self._snapshot_jobs:
            return result | {"status": "rejected", "code": "conflict"}
        if len(self._snapshot_jobs) >= 16:
            return result | {"status": "rejected", "code": "busy"}
        delivery = _SnapshotDelivery(boot, _scope(data), data["job_id"], str(uuid4()))
        delivery.request_task = asyncio.current_task()
        self._snapshot_jobs[delivery.identity] = delivery
        try:
            delivery.image = await self.snapshot_manager.get(
                boot,
                delivery.scope,
                delivery.identity,
                int(data["width"]),
                int(data["height"]),
                data["reason"],
            )
            if not self._snapshot_current(delivery):
                raise HomeKitOperationError("cancelled")
            # Reserve all transfer ownership before accepting the operation.
            # OPEN cannot race ahead of its correlated READY reply.
            delivery.prepared = channel.prepare_send(
                scope=data["scope"],
                operation_id=delivery.identity,
                transfer_id=delivery.transfer_id,
                kind="snapshot",
                total_bytes=len(delivery.image.data),
                valid=lambda: self._snapshot_current(delivery),
            )
            delivery.task = asyncio.create_task(
                self._deliver_snapshot(delivery), name="homekit-snapshot-delivery"
            )
            delivery.task.add_done_callback(self._snapshot_done)
        except HomeKitOperationError as exc:
            return result | {"status": "rejected", "code": _code(exc)}
        except BulkTransferError as exc:
            return result | {"status": "rejected", "code": exc.code}
        finally:
            if delivery.task is None and self._snapshot_jobs.get(delivery.identity) is delivery:
                self._snapshot_jobs.pop(delivery.identity)
        return result | {"status": "ready", "code": "ok", "transfer_id": delivery.transfer_id}

    async def replied(self, message, reply):
        """Called only after the validated operation reply has been flushed."""
        if message["kind"] == "recording_next":
            job = self._recording_jobs.get(message["payload"]["job_id"])
            delivery = job.delivery if job is not None else None
            if (
                delivery is not None
                and delivery.boot == message["boot_id"]
                and delivery.scope == _scope(message["payload"])
                and reply["payload"].get("transfer_id") == delivery.transfer_id
                and reply["payload"].get("sequence") == message["payload"]["sequence"]
            ):
                delivery.ready.set()
            return
        if message["kind"] != "snapshot_request":
            return
        delivery = self._snapshot_jobs.get(message["payload"]["job_id"])
        if (
            delivery is not None
            and delivery.boot == message["boot_id"]
            and delivery.scope == _scope(message["payload"])
            and reply["payload"].get("transfer_id") == delivery.transfer_id
        ):
            delivery.ready.set()

    async def _deliver_snapshot(self, delivery):
        from halo_server.homekit.bulk import BulkTransferError

        async def chunks():
            image = delivery.image.data
            for offset in range(0, len(image), 65536):
                yield memoryview(image)[offset : offset + 65536]

        try:
            await asyncio.wait_for(delivery.ready.wait(), 5.0)
            if not self._snapshot_current(delivery):
                await self._cancel_snapshot(delivery)
                return
            await delivery.prepared.send(chunks())
        except asyncio.CancelledError:
            await self._cancel_snapshot(delivery)
            raise
        except BulkTransferError as exc:
            if exc.code not in {"cancelled", "invalidated", "timeout"}:
                self.cleanup_failed = True
                delivery.cleanup_failed = exc.code == "cleanup_pending"
                self._changed()
        except Exception:
            self.cleanup_failed = True
            self._changed()
            await self._cancel_snapshot(delivery)
        finally:
            if (
                not delivery.cleanup_failed
                and self._snapshot_jobs.get(delivery.identity) is delivery
            ):
                self._snapshot_jobs.pop(delivery.identity)

    def _snapshot_done(self, task):
        if not task.cancelled() and task.exception() is not None:
            self.cleanup_failed = True
            self._changed()

    async def _cancel_snapshot(self, delivery):
        delivery.cancelled = True  # Delivery fence before any transport await.
        delivery.ready.set()
        if self.snapshot_manager is not None:
            self.snapshot_manager.cancel(delivery.boot, delivery.scope, delivery.identity)
        if delivery.prepared is not None:
            try:
                await delivery.prepared.cancel("cancelled")
                if not delivery.prepared.retirement_complete:
                    raise HomeKitOperationError("cleanup_pending")
            except BaseException:
                delivery.cleanup_failed = True
                raise
            delivery.cleanup_failed = False

    async def _close_snapshots(self):
        deliveries = tuple(self._snapshot_jobs.values())
        failures = []
        for delivery in deliveries:
            try:
                await self._cancel_snapshot(delivery)
            except Exception as exc:
                failures.append(exc)
        tasks = {
            task
            for delivery in deliveries
            if (task := delivery.task or delivery.request_task) is not None
            and task is not asyncio.current_task()
        }
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=5.0)
            if pending:
                failures.append(HomeKitOperationError("cleanup_pending"))
        for delivery in deliveries:
            if delivery.task is not None and delivery.task.done() and not delivery.cleanup_failed:
                self._snapshot_jobs.pop(delivery.identity, None)
        if self._snapshot_jobs:
            failures.append(HomeKitOperationError("cleanup_pending"))
        if failures:
            raise ExceptionGroup("Snapshot delivery retirement incomplete", failures)

    def _authorize_audio_policy(self, scope, policy):
        # Production injects durable Python-owned settings. A child cannot
        # unmute a camera by sending a permissive policy before a saved update.
        if self._audio_settings is None:
            return
        row = self._audio_settings(scope)
        if (
            policy.live_audio
            and (
                row is None
                or not row.microphone
                or row.preferences.microphone_muted
                or row.preferences.microphone_volume == 0
            )
            or policy.talkback
            and (
                row is None
                or not row.speaker
                or row.preferences.speaker_muted
                or row.preferences.speaker_volume == 0
            )
        ):
            raise HomeKitOperationError("policy_denied")

    def _authorize_recording_policy(self, scope, policy):
        if self._recording_authority is None:
            return
        state = self._recording_authority(scope)
        if (
            policy.recording
            and (state is None or not state.active or state.configuration is None)
            or policy.recording_audio
            and (state is None or not state.audio_active)
        ):
            raise HomeKitOperationError("policy_denied")

    def _recording_authorized(self, job):
        if (
            job.cancelled
            or self.boot_id != job.boot
            or self.cleanup_failed
            or self._recording_jobs.get(job.identity) is not job
        ):
            return False
        try:
            self.controller.authorize(job.boot, job.scope, "recording")
            if job.configuration.audio is not None:
                self.controller.authorize(job.boot, job.scope, "recording_audio")
            state = self._recording_authority(job.scope) if self._recording_authority else None
            return state is not None and state.capture_signature == job.privacy_signature
        except HomeKitOperationError:
            return False

    def _recording_current(self, job):
        return self._recording_authorized(job) and (job.stream is None or job.stream.is_current())

    def _prune_recording_history(self):
        now = asyncio.get_running_loop().time()
        self._recording_history = {
            identity: value for identity, value in self._recording_history.items() if value[1] > now
        }

    def _forget_recording(self, job):
        if self._recording_jobs.get(job.identity) is job:
            self._recording_jobs.pop(job.identity)
            self._recording_history[job.identity] = (
                job.scope,
                asyncio.get_running_loop().time() + RETIRED_SESSION_SECONDS,
            )

    async def _recording_request(self, boot, kind, data):
        from halo_server.homekit.bulk import BulkTransferError
        from halo_server.media.recording import RecordingError

        scope, identity = _scope(data), data["job_id"]
        result = {"scope": data["scope"], "job_id": identity}
        if kind == "recording_next":
            result |= {
                "transfer_id": None,
                "kind": None,
                "sequence": data["sequence"],
                "total_bytes": 0,
                "duration_ms": 0,
            }
        self._prune_recording_history()
        job = self._recording_jobs.get(identity)
        try:
            if job is not None and (job.boot != boot or job.scope != scope):
                raise HomeKitOperationError("session_conflict")
            if kind == "recording_stop":
                if job is not None:
                    await self._stop_recording(job)
                else:
                    previous = self._recording_history.get(identity)
                    if previous is not None and previous[0] != scope:
                        raise HomeKitOperationError("session_conflict")
                    if previous is None:
                        if (
                            len(self._recording_history) + len(self._recording_jobs)
                            >= MAX_SESSION_IDENTITIES
                        ):
                            raise HomeKitOperationError("busy")
                        self._recording_history[identity] = (
                            scope,
                            asyncio.get_running_loop().time() + RETIRED_SESSION_SECONDS,
                        )
                    # A deny-only tombstone grants no media authority. Privacy
                    # may already have advanced before a cancelled OPEN arrived;
                    # retire the exact older tuple rather than rejecting cleanup.
                    if self.recording_manager is not None:
                        await self.recording_manager.stop(boot, scope, identity)
                return result | {"status": "stopped", "code": "ok"}
            manager = self.recording_manager
            if manager is None or not manager.ready:
                raise HomeKitOperationError("not_ready")
            if kind == "recording_open":
                if identity in self._recording_history or job is not None:
                    raise HomeKitOperationError("session_conflict")
                if (
                    len(self._recording_history) + len(self._recording_jobs)
                    >= MAX_SESSION_IDENTITIES
                    or any(
                        j.scope.source_key == scope.source_key
                        for j in self._recording_jobs.values()
                    )
                ):
                    raise HomeKitOperationError("busy")
                configuration = recording_configuration(data["configuration"])
                state = self._recording_authority(scope) if self._recording_authority else None
                if (
                    state is None
                    or not state.active
                    or state.effective_configuration != configuration
                ):
                    raise HomeKitOperationError("policy_denied")
                self.controller.authorize(boot, scope, "recording")
                if configuration.audio is not None:
                    self.controller.authorize(boot, scope, "recording_audio")
                if self.recording_policy is None:
                    raise HomeKitOperationError("not_ready")
                self.recording_policy.authorize_open(boot, scope, configuration)
                event = self._recording_event(scope) if self._recording_event else None
                now = asyncio.get_running_loop().time()
                if (
                    not isinstance(event, RecordingEvent)
                    or not 0 <= event.event_at <= now
                    or now - event.event_at > 12
                ):
                    raise HomeKitOperationError("not_ready")
                job = _RecordingJob(
                    boot, scope, identity, configuration, state.capture_signature, event, now
                )
                job.request_task = asyncio.current_task()
                self._recording_jobs[identity] = job
                try:
                    job.stream = await manager.open(
                        boot, scope, identity, configuration, event_at=event.event_at
                    )
                    if not self._recording_current(job):
                        raise HomeKitOperationError("cancelled")
                except BaseException:
                    if not job.cancelled:
                        with contextlib.suppress(Exception):
                            await self._stop_recording(job)
                    raise
                finally:
                    if job.request_task is asyncio.current_task():
                        job.request_task = None
                return result | {"status": "accepted", "code": "ok"}
            if job is None or not self._recording_current(job):
                raise HomeKitOperationError("expired")
            if job.pulling is not None:
                raise HomeKitOperationError("busy")
            delivery = job.delivery
            if delivery is not None:
                # The peer may request its next packet immediately after the
                # successful transfer result, before this producer resumes.
                # Its validated normal ACK permits joining this exact tail;
                # actual source retirement still precedes the next pull.
                if (
                    not (delivery.prepared.peer_completed or delivery.prepared.retirement_complete)
                    or delivery.task is None
                ):
                    raise HomeKitOperationError("busy")
                done, _ = await asyncio.wait({delivery.task}, timeout=5.0)
                if not done:
                    raise HomeKitOperationError("cleanup_pending")
                await asyncio.shield(delivery.task)
                if not self._recording_current(job):
                    raise HomeKitOperationError("expired")
                if job.pulling is not None or job.delivery is not None:
                    raise HomeKitOperationError("busy")
            if data["sequence"] != job.sequence:
                raise HomeKitOperationError("session_conflict")
            channel = self.runtime.bulk_channel if self.runtime is not None else None
            if channel is None:
                raise HomeKitOperationError("not_ready")
            job.pulling = asyncio.current_task()
            try:
                packet = await anext(job.stream)
                if not self._recording_current(job):
                    raise HomeKitOperationError("cancelled")
                if (
                    packet.sequence != job.sequence
                    or packet.kind not in {"init", "fragment"}
                    or not isinstance(packet.payload, bytes)
                    or not 0
                    < len(packet.payload)
                    <= BULK_LIMITS[
                        "recording_init" if packet.kind == "init" else "recording_fragment"
                    ]
                    or type(packet.duration_ms) is not int
                    or (packet.kind == "init" and (packet.sequence != 0 or packet.duration_ms != 0))
                    or (packet.kind == "fragment" and not 1 <= packet.duration_ms <= 4000)
                ):
                    raise HomeKitOperationError("invalid_recording_output")
                delivery = _SnapshotDelivery(boot, scope, identity, str(uuid4()), image=packet)
                delivery.prepared = await self._prepare_recording_transfer(
                    job,
                    channel,
                    scope=data["scope"],
                    operation_id=identity,
                    transfer_id=delivery.transfer_id,
                    kind="recording_init" if packet.kind == "init" else "recording_fragment",
                    total_bytes=len(packet.payload),
                    valid=lambda: self._recording_current(job),
                )
                job.delivery = delivery
                job.sequence += 1
                delivery.task = asyncio.create_task(
                    self._deliver_recording(job, delivery), name="homekit-recording-delivery"
                )
                delivery.task.add_done_callback(self._snapshot_done)
                return result | {
                    "status": "ready",
                    "code": "ok",
                    "transfer_id": delivery.transfer_id,
                    "kind": packet.kind,
                    "total_bytes": len(packet.payload),
                    "duration_ms": packet.duration_ms,
                }
            except StopAsyncIteration:
                await self._stop_recording(job)
                return result | {"status": "eos", "code": "ok"}
            except BaseException:
                if not job.cancelled:
                    with contextlib.suppress(Exception):
                        await self._stop_recording(job)
                raise
            finally:
                if job.pulling is asyncio.current_task():
                    job.pulling = None
        except HomeKitOperationError as error:
            return result | {"status": "rejected", "code": _code(error)}
        except (RecordingError, ValueError, TypeError):
            return result | {"status": "rejected", "code": "unsupported"}
        except BulkTransferError as error:
            return result | {"status": "rejected", "code": error.code}

    async def _prepare_recording_transfer(self, job, channel, **parameters):
        """Share the bounded transport window without dropping another camera's clip."""
        from halo_server.homekit.bulk import BulkTransferError

        deadline = asyncio.get_running_loop().time() + RECORDING_TRANSFER_SECONDS
        while True:
            if not self._recording_current(job):
                raise HomeKitOperationError("cancelled")
            try:
                return channel.prepare_send(**parameters)
            except BulkTransferError as error:
                if error.code != "busy" or asyncio.get_running_loop().time() >= deadline:
                    raise
                await asyncio.sleep(0.01)

    async def _deliver_recording(self, job, delivery):
        async def chunks():
            payload = delivery.image.payload
            for offset in range(0, len(payload), 65536):
                yield memoryview(payload)[offset : offset + 65536]

        try:
            await asyncio.wait_for(delivery.ready.wait(), 5.0)
            if not self._recording_current(job):
                raise HomeKitOperationError("cancelled")
            await delivery.prepared.send(chunks())
            if not delivery.prepared.retirement_complete:
                raise HomeKitOperationError("cleanup_pending")
            if job.delivery is delivery:
                job.delivery = None
        except asyncio.CancelledError:
            if not job.cancelled:
                await self._stop_recording(job)
            raise
        except Exception:
            if not job.cancelled:
                await self._stop_recording(job)

    async def _stop_recording(self, job):
        job.cancelled = True  # Privacy/delivery fence before any cleanup await.
        async with job.closing:
            failures = []
            delivery = job.delivery
            if delivery is not None:
                delivery.cancelled = True
                delivery.ready.set()
                try:
                    await delivery.prepared.cancel("cancelled")
                    if not delivery.prepared.retirement_complete:
                        raise HomeKitOperationError("cleanup_pending")
                except BaseException as error:
                    failures.append(error)
            if self.recording_manager is not None:
                try:
                    await self.recording_manager.stop(job.boot, job.scope, job.identity)
                except BaseException as error:
                    failures.append(error)
            tasks = {
                task
                for task in (job.request_task, job.pulling, delivery.task if delivery else None)
                if task is not None and task is not asyncio.current_task() and not task.done()
            }
            for task in tasks:
                task.cancel()
            if tasks:
                _, pending = await asyncio.wait(tasks, timeout=5.0)
                if pending:
                    failures.append(HomeKitOperationError("cleanup_pending"))
            if failures:
                job.cleanup_failed = True
                raise HomeKitOperationError("cleanup_pending")
            job.delivery = job.stream = None
            job.cleanup_failed = False
            self._forget_recording(job)

    async def _close_recordings(self):
        results = await asyncio.gather(
            *(self._stop_recording(job) for job in tuple(self._recording_jobs.values())),
            return_exceptions=True,
        )
        if any(isinstance(result, BaseException) for result in results):
            raise HomeKitOperationError("cleanup_pending")

    async def _video_request(self, boot: str, kind: str, data: dict) -> dict:
        from halo_server.media.h264 import H264Error, VideoConfiguration

        result = {"scope": data["scope"], "session_id": data["session_id"]}
        if kind == "video_prepare":
            result["endpoint"] = None
        if self.live_manager is None or not self.live_manager.ready and kind != "video_stop":
            return result | {"status": "rejected", "code": "not-ready"}
        scope, session_id = _scope(data), data["session_id"]
        try:
            if kind == "video_prepare":
                from halo_server.media.homekit_audio import AudioEndpointRequest

                audio = data["audio"]
                if audio is not None and self._audio_settings is not None:
                    row = self._audio_settings(scope)
                    if row is None or not (row.microphone or row.speaker):
                        raise HomeKitOperationError("policy_denied")
                prepared = await self.live_manager.prepare(
                    boot,
                    scope,
                    session_id,
                    source_address=data["source_address"],
                    target_address=data["target_address"],
                    address_version=data["address_version"],
                    target_port=int(data["target_port"]),
                    key=base64.b64decode(data["key"], validate=True),
                    salt=base64.b64decode(data["salt"], validate=True),
                    audio=(
                        AudioEndpointRequest(
                            target_port=int(audio["target_port"]),
                            key=base64.b64decode(audio["key"], validate=True),
                            salt=base64.b64decode(audio["salt"], validate=True),
                        )
                        if audio is not None
                        else None
                    ),
                )
                result["endpoint"] = {
                    "address": prepared.address,
                    "port": prepared.port,
                    "ssrc": prepared.ssrc,
                    "key": base64.b64encode(prepared.key).decode("ascii"),
                    "salt": base64.b64encode(prepared.salt).decode("ascii"),
                    "audio": (
                        {
                            "port": prepared.audio.port,
                            "ssrc": prepared.audio.ssrc,
                            "key": base64.b64encode(prepared.audio.key).decode("ascii"),
                            "salt": base64.b64encode(prepared.audio.salt).decode("ascii"),
                        }
                        if prepared.audio is not None
                        else None
                    ),
                }
            elif kind in {"video_start", "video_reconfigure"}:
                config = VideoConfiguration(**data["config"])
                if kind == "video_start":
                    from halo_server.media.opus import AudioConfiguration

                    audio = data["audio_config"]
                    row = self._audio_settings(scope) if self._audio_settings else None
                    if (
                        audio is not None
                        and self._audio_settings is not None
                        and (row is None or not (row.microphone or row.speaker))
                    ):
                        raise HomeKitOperationError("policy_denied")
                    await self.live_manager.start(
                        boot,
                        scope,
                        session_id,
                        config,
                        audio_config=AudioConfiguration(**audio) if audio is not None else None,
                        microphone_volume=row.preferences.microphone_volume if row else 100,
                        speaker_volume=row.preferences.speaker_volume if row else 100,
                    )
                else:
                    await self.live_manager.reconfigure(boot, scope, session_id, config)
            else:
                await self.live_manager.stop(boot, scope, session_id)
        except HomeKitOperationError as exc:
            return result | {"status": "rejected", "code": _code(exc)}
        except H264Error:
            return result | {"status": "rejected", "code": "unsupported"}
        except ValueError:
            return result | {
                "status": "rejected",
                "code": "invalid-request" if kind == "video_prepare" else "unsupported",
            }
        except OSError:
            return result | {"status": "rejected", "code": "source-failed"}
        return result | {"status": "accepted", "code": "ok"}

    def _inventory(self, envelope: dict) -> dict:
        data = envelope["payload"]
        current = self.controller.inventory()
        if data["scope"] != _account(current):
            raise HomeKitOperationError("stale_scope")
        now = asyncio.get_running_loop().time()
        for key, (_, _, deadline) in tuple(self._pages.items()):
            if deadline <= now:
                self._pages.pop(key)
        cursor = data["cursor"]
        if cursor is None:
            inventory, offset = current, 0
            if data["revision"] not in (0, inventory.revision):
                raise HomeKitOperationError("stale_scope")
        else:
            stored = self._pages.pop(cursor, None)
            if stored is None:
                raise HomeKitOperationError("stale_scope")
            inventory, offset, _ = stored
            if inventory != current or data["revision"] != inventory.revision:
                raise HomeKitOperationError("stale_scope")
        continuation = str(uuid4())
        payload = {
            "scope": data["scope"],
            "revision": inventory.revision,
            "cursor": cursor,
            "next_cursor": None,
            "total": len(inventory.cameras),
            "complete": inventory.complete,
            "devices": [],
        }
        end = offset
        for camera in inventory.cameras[offset : offset + MAX_INVENTORY_PAGE]:
            candidate = payload | {
                "devices": payload["devices"] + [_device_dict(camera)],
                "next_cursor": continuation if end + 1 < len(inventory.cameras) else None,
            }
            try:
                encode_frame({**envelope, "kind": "inventory_page", "payload": candidate})
            except HomeKitProtocolError:
                if not payload["devices"]:
                    raise
                break
            payload = candidate
            end += 1
        if end < len(inventory.cameras):
            payload["next_cursor"] = continuation
            while len(self._pages) >= MAX_CURSOR_PAGES:
                self._pages.popitem(last=False)
            self._pages[continuation] = inventory, end, now + CURSOR_SECONDS
        return payload

    async def _resource_request(self, boot, kind, data) -> dict:
        identity, scope = data["session_id"], _scope(data)
        result = {"scope": data["scope"], "session_id": identity}
        speaker = kind == "talkback_request"
        stopping = kind == "lease_stop" or speaker and data["action"] == "stop"
        resource = self._resources.get(identity)
        ttl = data.get("ttl_ms", 30000)
        code = "ok"
        try:
            if resource is not None and (
                resource.scope != scope or resource.kind != ("speaker" if speaker else "live")
            ):
                raise HomeKitOperationError("session_conflict")
            if stopping:
                if resource is None and self._session_ids.get(identity, 0) is None:
                    raise HomeKitOperationError("busy")
                if resource is not None:
                    await resource.handle.close()
                    self._forget_resource(identity, resource)
                status = "stopped"
            elif kind == "lease_renew":
                if resource is None or not self._current(resource):
                    raise HomeKitOperationError("expired")
                if not await resource.handle.renew():
                    raise HomeKitOperationError("expired")
                resource.deadline = asyncio.get_running_loop().time() + ttl / 1000
                status = "accepted"
            else:
                if resource is None:
                    now = asyncio.get_running_loop().time()
                    self._session_ids = {
                        key: deadline
                        for key, deadline in self._session_ids.items()
                        if deadline is None or deadline > now
                    }
                    if identity in self._session_ids:
                        raise HomeKitOperationError("session_conflict")
                    if len(self._session_ids) >= MAX_SESSION_IDENTITIES:
                        raise HomeKitOperationError("busy")
                    method = self.controller.open_speaker if speaker else self.controller.open_live
                    self._session_ids[identity] = None  # Reserve before acquisition yields.
                    try:
                        handle = await method(boot, scope, session_id=identity)
                    except BaseException:
                        self._session_ids[identity] = (
                            asyncio.get_running_loop().time() + RETIRED_SESSION_SECONDS
                        )
                        raise
                    resource = _Resource(
                        scope,
                        "speaker" if speaker else "live",
                        handle,
                        asyncio.get_running_loop().time() + ttl / 1000,
                    )
                    self._resources[identity] = resource
                    self._session_ids[identity] = None
                    if self.boot_id != boot or not self._current(resource):
                        await resource.handle.close()
                        self._forget_resource(identity, resource)
                        raise HomeKitOperationError("stale_scope")
                elif not self._current(resource):
                    raise HomeKitOperationError("expired")
                if not speaker:
                    ttl = min(
                        ttl, int((resource.deadline - asyncio.get_running_loop().time()) * 1000)
                    )
                    if ttl <= 0:
                        raise HomeKitOperationError("expired")
                status = "accepted"
        except HomeKitOperationError as exc:
            code, status = _code(exc), "rejected"
        if kind in {"lease_request", "lease_renew"}:
            result["ttl_ms"] = ttl if status == "accepted" else 0
        return result | {"status": status, "code": code}

    async def notice(self, envelope: dict) -> None:
        self._require_boot(envelope)
        if envelope["kind"] == "snapshot_cancel":
            data = envelope["payload"]
            delivery = self._snapshot_jobs.get(data["job_id"])
            if delivery is not None and delivery.scope == _scope(data):
                await self._cancel_snapshot(delivery)
            return
        if envelope["kind"] != "transfer_open":
            raise HomeKitProtocolError("unsupported child notice")
        data = envelope["payload"]
        resource = self._resources.get(data["operation_id"])
        if (
            data["kind"] != "talkback_pcm"
            or data["total_bytes"] != 1920
            or data["chunk_bytes"] != 1920
            or resource is None
            or resource.kind != "speaker"
            or resource.scope != _scope(data)
            or not self._current(resource)
        ):
            raise HomeKitProtocolError("unowned talkback transfer")
        channel = self.runtime.bulk_channel
        if channel is None:
            raise HomeKitProtocolError("bulk unavailable")
        channel.authorize_receive(
            scope=data["scope"],
            operation_id=data["operation_id"],
            transfer_id=data["transfer_id"],
            kind="talkback_pcm",
            total_bytes=1920,
            sink=resource.handle.write,
            valid=lambda: self._current(resource),
        )

    def _forget_resource(self, identity: str, resource: _Resource) -> None:
        if self._resources.get(identity) is resource:
            self._resources.pop(identity)
            # Longer than either control deadline: bound late/replayed traffic
            # without an ever-growing history or a lifetime streaming limit.
            self._session_ids[identity] = (
                asyncio.get_running_loop().time() + RETIRED_SESSION_SECONDS
            )

    def _current(self, resource: _Resource) -> bool:
        return (
            self.boot_id is not None
            and not self.cleanup_failed
            and any(owned is resource for owned in self._resources.values())
            and asyncio.get_running_loop().time() < resource.deadline
            and any(
                camera.scope == resource.scope for camera in self.controller.inventory().cameras
            )
        )


def _account(inventory):
    return (
        None
        if inventory.profile_id is None
        else {
            "profile_id": inventory.profile_id,
            "account_generation": inventory.account_generation,
        }
    )


def _scope(data: dict) -> MediaScope:
    return MediaScope(**data["scope"])


def recording_configuration(value):
    """Construct only recording codec choices, never peer RTP/session settings."""
    from halo_server.media.h264 import VideoConfiguration
    from halo_server.media.recording import RecordingAudioConfiguration, RecordingConfiguration

    if (
        not isinstance(value, dict)
        or set(value) != {"video", "audio", "fragment_length_ms", "i_frame_interval_ms"}
        or not isinstance(value["video"], dict)
        or set(value["video"]) != {"width", "height", "fps", "profile", "level", "max_bit_rate"}
        or value["audio"] is not None
        and (
            not isinstance(value["audio"], dict)
            or set(value["audio"]) != {"sample_rate", "bit_rate", "channels", "bitrate_mode"}
        )
    ):
        raise HomeKitOperationError("invalid_request")
    return RecordingConfiguration(
        VideoConfiguration(**value["video"]),
        RecordingAudioConfiguration(**value["audio"]) if value["audio"] is not None else None,
        fragment_length_ms=value["fragment_length_ms"],
        i_frame_interval_ms=value["i_frame_interval_ms"],
    )


def _code(error: HomeKitOperationError) -> str:
    if error.code in {"busy", "timeout", "expired", "cancelled", "unsupported", "unauthorized"}:
        return error.code
    if error.code in {"scope_changed", "stale_scope", "boot_changed", "authority_changed"}:
        return "stale-scope"
    if error.code in {"policy_denied"}:
        return "policy-denied"
    if error.code in {"session_conflict"}:
        return "conflict"
    if error.code in {"capability_unsupported", "snapshot_unsupported", "intercom_unsupported"}:
        return "unsupported"
    if error.code in {
        "cleanup_failed",
        "cleanup_pending",
        "closed",
        "not_ready",
        "prebuffer_unavailable",
    }:
        return "not-ready"
    if error.code in {"invalid_request", "invalid_dimensions"}:
        return "invalid-request"
    return "source-failed"


def _device_dict(camera):
    value = asdict(camera)
    value["capabilities"]["evidence"] = list(value["capabilities"]["evidence"])
    return value
