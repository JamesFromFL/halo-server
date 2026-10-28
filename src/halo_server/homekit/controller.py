"""One private admission facade over the server's existing Ring resource owners.

This module neither owns an SDK session nor publishes HAP or sends RTP. Calls
are made on one owner loop; admission callbacks may run on the Ring loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any, Protocol
from uuid import UUID, uuid4

from halo_server.homekit.models import (
    CameraPolicy,
    HomeKitOperationError,
    MediaScope,
    NativeCamera,
    NativeInventory,
)
from halo_server.media.buffer import BufferRead, MediaStreamKind
from halo_server.media.formats import MAX_SNAPSHOT_BYTES, MAX_TALKBACK_PCM_FRAME_BYTES
from halo_server.media.manager import owned_thread, settle_owned

MAX_LIVE_PER_CAMERA = 2
MAX_LIVE_TOTAL = 8
MAX_SNAPSHOTS = 2
MAX_CONTROLS = 2
MAX_SPEAKERS = 1
ACQUIRE_SECONDS = 20.0
SNAPSHOT_SECONDS = 8.0
CONTROL_SECONDS = 30.0
CLEANUP_SECONDS = 5.0
MAX_READ_SECONDS = 1.0
MAX_HANDLE_WORK = 2


class HomeKitBackend(Protocol):
    def inventory(self) -> NativeInventory: ...
    def scope_current(self, scope: MediaScope) -> bool: ...
    async def acquire_live(self, scope: MediaScope, *, admission: Callable[[], bool]): ...
    async def snapshot(
        self, scope: MediaScope, fresh: bool, *, admission: Callable[[], bool]
    ) -> bytes: ...
    async def open_speaker(self, scope: MediaScope, *, admission: Callable[[], bool]): ...
    async def set_control(
        self, scope: MediaScope, control: str, on: bool, *, admission: Callable[[], bool]
    ) -> None: ...


@dataclass
class _Camera:
    scope: MediaScope
    policy: CameraPolicy = field(default_factory=CameraPolicy)


@dataclass(eq=False)
class _Claim:
    session_id: str
    boot_id: str
    scope: MediaScope
    kind: str
    deadline: float
    task: asyncio.Task | None = None
    resource: Any = None
    handle: Any = None
    retired: bool = False
    cleanup: asyncio.Task | None = None
    failure: str | None = None
    waiters: int = 0
    possibly_sent: bool = False
    work: dict[asyncio.Task, asyncio.Event] = field(default_factory=dict)


class HomeKitController:
    def __init__(self, backend: HomeKitBackend) -> None:
        self._backend = backend
        self._lock = threading.RLock()
        self._boot: str | None = None
        self._closed = False
        self._cameras: dict[tuple, _Camera] = {}
        self._claims: dict[str, _Claim] = {}
        self._privacy_generation = 0
        self._revision = 0
        self._inventory_signature = None
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def boot_id(self) -> str | None:
        with self._lock:
            return self._boot

    @property
    def pending_resources(self) -> int:
        with self._lock:
            return len(self._claims)

    @property
    def cleanup_failures(self) -> int:
        with self._lock:
            return sum(claim.failure is not None for claim in self._claims.values())

    def _owner(self) -> asyncio.AbstractEventLoop:
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise RuntimeError("HomeKit controller requires one owner loop")
        return loop

    def _next_privacy(self) -> int:
        self._privacy_generation += 1
        if self._privacy_generation >= 2**53:
            raise HomeKitOperationError("generation_exhausted")
        return self._privacy_generation

    async def attach(self, boot_id: str, selected_scopes=()) -> NativeInventory:
        self._owner()
        _identifier(boot_id)
        selected = tuple(selected_scopes)
        inventory = self._backend.inventory()
        known = {camera.scope.source_key for camera in inventory.cameras}
        if len(selected) > 256 or any(not isinstance(scope, MediaScope) for scope in selected):
            raise HomeKitOperationError("invalid_selection")
        if len({scope.source_key for scope in selected}) != len(selected):
            raise HomeKitOperationError("invalid_selection")
        if any(
            scope.source_key not in known or not self._backend.scope_current(scope)
            for scope in selected
        ):
            raise HomeKitOperationError("scope_changed")
        with self._lock:
            if self._closed:
                raise HomeKitOperationError("closed")
            if self._boot == boot_id and set(self._cameras) == {
                scope.source_key for scope in selected
            }:
                return self.inventory()
            claims = tuple(self._claims.values())
            for claim in claims:
                claim.retired = True
            self._boot = boot_id
            self._cameras = {
                scope.source_key: _Camera(replace(scope, privacy_generation=self._next_privacy()))
                for scope in selected
            }
        await self._retire(claims)
        if self.boot_id != boot_id:
            raise HomeKitOperationError("boot_changed")
        return self.inventory()

    def inventory(self) -> NativeInventory:
        source = self._backend.inventory()
        with self._lock:
            cameras = tuple(
                replace(camera, scope=self._cameras[camera.scope.source_key].scope)
                for camera in source.cameras
                if self._boot is not None
                and camera.scope.source_key in self._cameras
                and self._backend.scope_current(camera.scope)
            )
            signature = (self._boot, source, cameras)
            if signature != self._inventory_signature:
                self._revision += 1
                self._inventory_signature = signature
            return NativeInventory(
                self._revision,
                cameras,
                source.complete,
                source.profile_id,
                source.account_generation,
            )

    async def update_policy(
        self, boot_id: str, scope: MediaScope, policy: CameraPolicy
    ) -> MediaScope:
        self._owner()
        if not isinstance(policy, CameraPolicy):
            raise HomeKitOperationError("invalid_policy")
        with self._lock:
            entry = self._entry(boot_id, scope)
            if entry.policy == policy:
                return entry.scope
            entry.scope = replace(scope, privacy_generation=self._next_privacy())
            entry.policy = policy
            claims = tuple(
                claim
                for claim in self._claims.values()
                if claim.scope.source_key == scope.source_key
            )
            for claim in claims:
                claim.retired = True
            updated = entry.scope
        await self._retire(claims)
        with self._lock:
            self._entry(boot_id, updated)
        return updated

    async def reconcile(self) -> NativeInventory:
        self._owner()
        with self._lock:
            claims = tuple(claim for claim in self._claims.values() if not self._valid(claim))
            for claim in claims:
                claim.retired = True
        await self._retire(claims)
        return self.inventory()

    def _entry(self, boot_id: str, scope: MediaScope) -> _Camera:
        if self._closed or self._boot is None or self._boot != boot_id:
            raise HomeKitOperationError("boot_changed")
        entry = self._cameras.get(scope.source_key)
        if entry is None or entry.scope != scope or not self._backend.scope_current(scope):
            raise HomeKitOperationError("scope_changed")
        return entry

    def authorize(self, boot_id: str, scope: MediaScope, permission: str) -> None:
        """Recheck selected source/privacy authority without acquiring a resource."""
        with self._lock:
            if not self._entry(boot_id, scope).policy.permits(permission):
                raise HomeKitOperationError("policy_denied")

    def validate_selected_scope(self, boot_id: str, scope: MediaScope) -> None:
        with self._lock:
            self._entry(boot_id, scope)

    def fence_camera(self, boot_id: str, scope: MediaScope) -> None:
        """Revoke one camera before awaiting a durable settings operation."""
        with self._lock:
            entry = self._entry(boot_id, scope)
            entry.scope = replace(scope, privacy_generation=self._next_privacy())
            entry.policy = CameraPolicy()
            for claim in self._claims.values():
                if claim.scope.source_key == scope.source_key:
                    claim.retired = True

    def _valid(self, claim: _Claim) -> bool:
        with self._lock:
            try:
                entry = self._entry(claim.boot_id, claim.scope)
            except HomeKitOperationError:
                return False
            return (
                self._claims.get(claim.session_id) is claim
                and not claim.retired
                and entry.policy.permits(_permission(claim.kind))
            )

    def _admission(self, claim: _Claim) -> bool:
        with self._lock:
            allowed = self._valid(claim)
            # Reading the loop's monotonic clock is safe in SDK callbacks.
            allowed = (
                allowed
                and self._loop is not None
                and (claim.handle is not None or self._loop.time() < claim.deadline)
            )
            if allowed and claim.kind == "control":
                claim.possibly_sent = True
            return bool(allowed)

    def _camera(self, scope: MediaScope) -> NativeCamera:
        camera = next(
            (
                camera
                for camera in self._backend.inventory().cameras
                if camera.scope.source_key == scope.source_key
            ),
            None,
        )
        if camera is None:
            raise HomeKitOperationError("scope_changed")
        return camera

    def _reserve(self, boot_id, scope, kind, session_id, timeout) -> tuple[_Claim, bool]:
        loop = self._owner()
        session_id = str(uuid4()) if session_id is None else _identifier(session_id)
        with self._lock:
            entry = self._entry(boot_id, scope)
            if not entry.policy.permits(_permission(kind)):
                raise HomeKitOperationError("policy_denied")
            existing = self._claims.get(session_id)
            if existing is not None:
                if (
                    existing.boot_id == boot_id
                    and existing.scope == scope
                    and existing.kind == kind
                    and not existing.retired
                ):
                    return existing, False
                raise HomeKitOperationError("session_conflict")
            claims = tuple(self._claims.values())
            if any(
                claim.retired and claim.scope.source_key == scope.source_key for claim in claims
            ):
                raise HomeKitOperationError("cleanup_pending")
            same_kind = [claim for claim in claims if claim.kind == kind]
            if kind == "live" and (
                len(same_kind) >= MAX_LIVE_TOTAL
                or sum(claim.scope.source_key == scope.source_key for claim in same_kind)
                >= MAX_LIVE_PER_CAMERA
            ):
                raise HomeKitOperationError("busy")
            if kind == "snapshot" and len(same_kind) >= MAX_SNAPSHOTS:
                raise HomeKitOperationError("busy")
            if kind == "control" and len(same_kind) >= MAX_CONTROLS:
                raise HomeKitOperationError("busy")
            if kind == "speaker" and len(same_kind) >= MAX_SPEAKERS:
                raise HomeKitOperationError("busy")
            claim = _Claim(session_id, boot_id, scope, kind, loop.time() + timeout)
            self._claims[session_id] = claim
            return claim, True

    async def open_live(self, boot_id: str, scope: MediaScope, *, session_id: str | None = None):
        if not self._camera(scope).capabilities.video:
            raise HomeKitOperationError("capability_unsupported")
        return await self._open(boot_id, scope, "live", session_id)

    async def open_speaker(self, boot_id: str, scope: MediaScope, *, session_id: str | None = None):
        if not self._camera(scope).capabilities.talkback:
            raise HomeKitOperationError("capability_unsupported")
        return await self._open(boot_id, scope, "speaker", session_id)

    async def _open(self, boot_id, scope, kind, session_id):
        claim, created = self._reserve(boot_id, scope, kind, session_id, ACQUIRE_SECONDS)
        if created:
            claim.task = asyncio.create_task(self._acquire(claim))
            claim.task.add_done_callback(lambda task: self._claim_done(claim, task))
        with self._lock:
            claim.waiters += 1
        try:
            return await asyncio.shield(claim.task)
        except asyncio.CancelledError:
            with self._lock:
                if claim.waiters == 1:
                    claim.retired = True
                    if claim.task.done() and claim.resource is not None:
                        self._start_cleanup(claim)
                    else:
                        claim.task.cancel()
            raise
        finally:
            with self._lock:
                claim.waiters -= 1

    async def _acquire(self, claim):
        try:
            async with asyncio.timeout(ACQUIRE_SECONDS):
                method = (
                    self._backend.acquire_live
                    if claim.kind == "live"
                    else self._backend.open_speaker
                )
                claim.resource = await method(claim.scope, admission=lambda: self._admission(claim))
            if not self._admission(claim):
                raise HomeKitOperationError("authority_changed")
            claim.handle = (
                NativeLiveHandle(self, claim)
                if claim.kind == "live"
                else NativeSpeakerHandle(self, claim)
            )
            return claim.handle
        except BaseException as exc:
            claim.retired = True
            if claim.resource is not None:
                await settle_owned(self._start_cleanup(claim))
            else:
                self._forget(claim)
            if isinstance(exc, (asyncio.CancelledError, HomeKitOperationError)):
                raise
            if isinstance(exc, TimeoutError):
                raise HomeKitOperationError("timeout") from None
            raise HomeKitOperationError("backend_failed") from None

    async def snapshot(self, boot_id: str, scope: MediaScope, fresh: bool = False) -> bytes:
        if type(fresh) is not bool or not self._camera(scope).capabilities.snapshot:
            raise HomeKitOperationError("capability_unsupported")
        claim, _ = self._reserve(boot_id, scope, "snapshot", None, SNAPSHOT_SECONDS)
        return await self._once(
            claim,
            lambda: self._backend.snapshot(
                scope, fresh=fresh, admission=lambda: self._admission(claim)
            ),
        )

    async def set_control(self, boot_id: str, scope: MediaScope, control: str, on: bool) -> None:
        if (
            not isinstance(control, str)
            or control not in {"light", "siren"}
            or type(on) is not bool
        ):
            raise HomeKitOperationError("invalid_control")
        if not getattr(self._camera(scope).capabilities, control):
            raise HomeKitOperationError("capability_unsupported")
        claim, _ = self._reserve(boot_id, scope, "control", None, CONTROL_SECONDS)
        await self._once(
            claim,
            lambda: self._backend.set_control(
                scope, control, on, admission=lambda: self._admission(claim)
            ),
        )

    async def _once(self, claim, operation):
        async def run():
            try:
                async with asyncio.timeout(max(0, claim.deadline - self._owner().time())):
                    result = await operation()
                if not self._admission(claim):
                    raise HomeKitOperationError(
                        "authority_changed", possibly_sent=claim.possibly_sent
                    )
                if claim.kind == "snapshot" and (
                    not isinstance(result, bytes) or not 0 < len(result) <= MAX_SNAPSHOT_BYTES
                ):
                    raise HomeKitOperationError("invalid_snapshot")
                return result
            except asyncio.CancelledError:
                raise
            except HomeKitOperationError as exc:
                raise HomeKitOperationError(
                    exc.code, str(exc), possibly_sent=exc.possibly_sent or claim.possibly_sent
                ) from None
            except Exception as exc:
                raise HomeKitOperationError(
                    "timeout" if isinstance(exc, TimeoutError) else "backend_failed",
                    possibly_sent=claim.possibly_sent,
                ) from None
            finally:
                self._forget(claim)

        claim.task = asyncio.create_task(run())
        claim.task.add_done_callback(lambda task: self._claim_done(claim, task))
        try:
            return await asyncio.shield(claim.task)
        except asyncio.CancelledError:
            claim.retired = True
            claim.task.cancel()
            raise

    def _claim_done(self, claim, task):
        # A task cancelled before its first step never enters its try/finally.
        # It cannot own a backend resource yet, so retire that reservation here.
        _observe(task)
        if claim.resource is None:
            self._forget(claim)

    def _begin_work(self, claim):
        task = asyncio.current_task()
        with self._lock:
            if not self._valid(claim):
                raise HomeKitOperationError("authority_changed")
            if task in claim.work or len(claim.work) >= MAX_HANDLE_WORK:
                raise HomeKitOperationError("busy")
            claim.work[task] = asyncio.Event()
        return task

    def _finish_work(self, claim, task):
        with self._lock:
            finished = claim.work.pop(task, None)
            if finished is not None:
                finished.set()

    def _forget(self, claim):
        with self._lock:
            if self._claims.get(claim.session_id) is claim:
                self._claims.pop(claim.session_id)

    def _start_cleanup(self, claim) -> asyncio.Task:
        with self._lock:
            if claim.cleanup is None:
                claim.retired = True
                claim.cleanup = asyncio.create_task(self._cleanup(claim))
                claim.cleanup.add_done_callback(_observe)
            return claim.cleanup

    async def _cleanup(self, claim):
        try:
            work = tuple(
                (task, finished)
                for task, finished in claim.work.items()
                if task is not asyncio.current_task()
            )
            for task, _ in work:
                task.cancel()
            if work:
                # Join the owned method, not the caller's entire task: its outer
                # finally block may itself await handle.close().
                await settle_owned(asyncio.gather(*(finished.wait() for _, finished in work)))
            if claim.resource is not None:
                if claim.kind == "live":
                    await owned_thread(claim.resource.close)
                else:
                    await claim.resource.close()
        except BaseException:
            claim.failure = "cleanup_failed"
            raise HomeKitOperationError("cleanup_failed") from None
        self._forget(claim)

    async def _retire(self, claims):
        tasks = []
        for claim in claims:
            claim.retired = True
            if claim.resource is not None and claim.task is not None and claim.task.done():
                tasks.append(self._start_cleanup(claim))
            elif claim.task is not None:
                claim.task.cancel()
                tasks.append(claim.task)
        if tasks:
            waiter = asyncio.create_task(_join(tasks))
            try:
                done, _ = await asyncio.wait((waiter,), timeout=CLEANUP_SECONDS)
                if not done:
                    waiter.add_done_callback(_observe)
                    raise HomeKitOperationError("cleanup_pending")
                errors = waiter.result()
                if any(
                    isinstance(error, HomeKitOperationError) and error.code == "cleanup_failed"
                    for error in errors
                ):
                    raise HomeKitOperationError("cleanup_failed")
            except asyncio.CancelledError:
                waiter.add_done_callback(_observe)
                raise

    def fence(self) -> None:
        """Synchronously revoke local selection before waiting for persistence/cleanup."""
        self._fence()

    def _fence(self, boot_id: str | None = None):
        with self._lock:
            if boot_id is not None and boot_id != self._boot:
                raise HomeKitOperationError("boot_changed")
            self._boot = None
            self._cameras.clear()
            claims = tuple(self._claims.values())
            for claim in claims:
                claim.retired = True
            return claims

    async def detach(self, boot_id: str | None = None) -> None:
        self._owner()
        await self._retire(self._fence(boot_id))

    async def close(self) -> None:
        with self._lock:
            self._closed = True
        await self.detach()


class NativeLiveHandle:
    def __init__(self, controller: HomeKitController, claim: _Claim):
        self._controller, self._claim = controller, claim
        self._cursor = None
        self._capture_epoch = None
        self._reading = asyncio.Lock()
        self._audio_cursor = self._audio_epoch = None
        self._audio_reading = asyncio.Lock()

    @property
    def capture_epoch(self):
        resource = self._claim.resource
        return resource.buffer.capture_epoch if resource is not None else None

    @property
    def session_id(self):
        return self._claim.session_id

    @property
    def scope(self):
        return self._claim.scope

    def is_current(self):
        resource = self._claim.resource
        return self._controller._valid(self._claim) and bool(resource.is_current())

    @property
    def source_clocks(self):
        if not self.is_current():
            raise HomeKitOperationError("authority_changed")
        callback = getattr(self._claim.resource, "source_clock_callback", None)
        clocks = callback() if callback is not None else ()
        if not self.is_current():
            raise HomeKitOperationError("authority_changed")
        return tuple(item for item in clocks if item.capture_epoch == self.capture_epoch)

    async def read_audio(self, *, timeout: float = 0.25) -> BufferRead:
        if type(timeout) not in {int, float} or not 0 <= timeout <= MAX_READ_SECONDS:
            raise HomeKitOperationError("invalid_timeout")
        if self._audio_reading.locked():
            raise HomeKitOperationError("busy")
        task = self._controller._begin_work(self._claim)
        try:
            async with self._audio_reading:
                self._controller.authorize(self._claim.boot_id, self.scope, "live_audio")
                if not self.is_current():
                    raise HomeKitOperationError("authority_changed")
                buffer = getattr(self._claim.resource, "original_audio_buffer", None)
                if buffer is None:
                    raise HomeKitOperationError("audio_unavailable")
                batch = await owned_thread(
                    buffer.read, after_sequence=self._audio_cursor, timeout=timeout
                )
                self._controller.authorize(self._claim.boot_id, self.scope, "live_audio")
                if not self.is_current():
                    raise HomeKitOperationError("authority_changed")
                epoch = self.capture_epoch
                if batch.capture_epoch != epoch:
                    self._audio_cursor, self._audio_epoch = None, epoch
                    return BufferRead((), True, epoch)
                changed = self._audio_epoch is not None and self._audio_epoch != epoch
                self._audio_epoch = epoch
                if batch.packets:
                    self._audio_cursor = batch.packets[-1].sequence
                return BufferRead(batch.packets, batch.resynchronized or changed, epoch)
        finally:
            self._controller._finish_work(self._claim, task)

    async def read(self, *, timeout: float = 0.25) -> BufferRead:
        if type(timeout) not in {int, float} or not 0 <= timeout <= MAX_READ_SECONDS:
            raise HomeKitOperationError("invalid_timeout")
        if self._reading.locked():
            raise HomeKitOperationError("busy")
        task = self._controller._begin_work(self._claim)
        try:
            async with self._reading:
                if not self.is_current():
                    raise HomeKitOperationError("authority_changed")
                batch = await owned_thread(
                    self._claim.resource.buffer.read, after_sequence=self._cursor, timeout=timeout
                )
                if not self.is_current():
                    raise HomeKitOperationError("authority_changed")
                epoch_changed = (
                    self._capture_epoch is not None and self._capture_epoch != batch.capture_epoch
                )
                self._capture_epoch = batch.capture_epoch
                if epoch_changed:
                    self._cursor = None
                if batch.packets:
                    self._cursor = batch.packets[-1].sequence
                entry = self._controller._cameras[self.scope.source_key]
                audio = (
                    entry.policy.permits("live_audio")
                    and self._controller._camera(self.scope).capabilities.audio
                )
                return BufferRead(
                    tuple(
                        packet
                        for packet in batch.packets
                        if audio or packet.stream is MediaStreamKind.VIDEO
                    ),
                    batch.resynchronized or epoch_changed,
                    batch.capture_epoch,
                )
        finally:
            self._controller._finish_work(self._claim, task)

    async def renew(self) -> bool:
        return await _resource_call(self._controller, self._claim, "renew", threaded=True)

    async def close(self) -> None:
        await self._controller._retire((self._claim,))


class NativeSpeakerHandle:
    def __init__(self, controller: HomeKitController, claim: _Claim):
        self._controller, self._claim = controller, claim

    @property
    def session_id(self):
        return self._claim.session_id

    @property
    def scope(self):
        return self._claim.scope

    async def write(self, payload: bytes) -> None:
        if not isinstance(payload, bytes) or len(payload) != MAX_TALKBACK_PCM_FRAME_BYTES:
            raise HomeKitOperationError("invalid_pcm")
        await _resource_call(self._controller, self._claim, "write", payload)

    async def wait_ready(self, timeout: float) -> bool:
        if type(timeout) not in {int, float} or not 0 < timeout <= ACQUIRE_SECONDS:
            raise HomeKitOperationError("invalid_timeout")
        return await _resource_call(self._controller, self._claim, "wait_ready", timeout)

    async def renew(self) -> bool:
        return await _resource_call(self._controller, self._claim, "renew")

    async def close(self) -> None:
        await self._controller._retire((self._claim,))


async def _resource_call(controller, claim, method, *args, threaded=False):
    task = controller._begin_work(claim)
    try:
        if not controller._valid(claim):
            raise HomeKitOperationError("authority_changed")
        call = getattr(claim.resource, method)
        result = await owned_thread(call, *args) if threaded else await call(*args)
        if not controller._valid(claim):
            raise HomeKitOperationError("authority_changed")
        return result
    finally:
        controller._finish_work(claim, task)


async def _join(tasks):
    return await asyncio.gather(*tasks, return_exceptions=True)


def _observe(task):
    if not task.cancelled():
        with contextlib.suppress(asyncio.CancelledError, Exception):
            task.exception()


def _identifier(value):
    try:
        if isinstance(value, str) and str(UUID(value)) == value:
            return value
    except ValueError:
        pass
    raise HomeKitOperationError("invalid_session")


def _permission(kind):
    return {
        "live": "streaming",
        "snapshot": "snapshots",
        "speaker": "talkback",
        "control": "controls",
    }[kind]
