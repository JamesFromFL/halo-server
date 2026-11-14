"""Trusted recording demand and measured warm-history admission.

HomeKit-selected codecs describe output. Only durable Python owner settings,
positive mains evidence, and the current camera policy can request warm input.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, replace
from uuid import uuid4

from halo_server.homekit.models import HomeKitOperationError
from halo_server.media.recording import RecordingConfiguration

MAX_WARM = 2
WARM_SECONDS = 570.0
MAINTENANCE_SECONDS = 0.5
CLEANUP_SECONDS = 5.0


@dataclass(frozen=True)
class RecordingAuthority:
    active: bool
    audio_active: bool
    configuration: RecordingConfiguration | None
    owner_opt_in: bool = False
    mains_confirmed: bool = False

    def __post_init__(self):
        if (
            any(
                type(value) is not bool
                for value in (
                    self.active,
                    self.audio_active,
                    self.owner_opt_in,
                    self.mains_confirmed,
                )
            )
            or self.configuration is not None
            and not isinstance(self.configuration, RecordingConfiguration)
        ):
            raise HomeKitOperationError("invalid_configuration")

    @property
    def effective_configuration(self):
        if self.configuration is None:
            return None
        return self.configuration if self.audio_active else replace(self.configuration, audio=None)

    @property
    def capture_signature(self):
        # Raw source history is independent of the selected output quality/GOP.
        return (
            self.active,
            self.audio_active,
            self.owner_opt_in,
            self.mains_confirmed,
            self.configuration is not None,
        )

    @property
    def warm_allowed(self):
        return (
            self.active
            and self.owner_opt_in
            and self.mains_confirmed
            and self.configuration is not None
        )


@dataclass(frozen=True)
class RecordingWarmStatus:
    state: str
    history_ready: bool = False
    video_seconds: float = 0.0
    audio_seconds: float = 0.0


@dataclass(eq=False)
class _Warm:
    boot: str
    scope: object
    identity: str
    signature: tuple
    opened_at: float
    capture: object = None
    acquiring: asyncio.Task | None = None
    retiring: bool = False
    failure: bool = False


class RecordingPolicyOwner:
    """At most two warm reservations, including acquisition and failed cleanup."""

    def __init__(
        self, controller, captures, authority, *, clock=time.monotonic, changed=lambda: None
    ):
        self.controller, self.captures, self._authority = controller, captures, authority
        self._changed = changed
        self._status_signature = None
        self._clock = clock
        self._boot = None
        self._retiring_boot = None
        self._entries = {}
        self._maintenance = None
        self._refreshing = asyncio.Lock()
        self._closed = False

    @property
    def warm_budget(self):
        """Read reserved slots without starting capture or probing its history.

        Acquisitions and retained cleanup own capacity too; this count is not
        the number of active streams or of owner preferences switched on.
        """
        return {"capacity": MAX_WARM, "reserved": len(self._entries)}

    @property
    def pending_resources(self):
        return len(self._entries) + int(
            self._retiring_boot is not None and self._maintenance is not None
        )

    @property
    def cleanup_failures(self):
        return sum(entry.failure for entry in self._entries.values()) + int(
            self._retiring_boot is not None and self._maintenance is not None
        )

    def _state(self, scope):
        state = self._authority(scope)
        return state if isinstance(state, RecordingAuthority) else None

    def _allowed(self, boot, scope):
        if self._closed or self._boot != boot:
            return None
        state = self._state(scope)
        if state is None or not state.warm_allowed:
            return None
        try:
            self.controller.authorize(boot, scope, "recording")
            if state.audio_active:
                self.controller.authorize(boot, scope, "recording_audio")
        except HomeKitOperationError:
            return None
        return state

    def _current(self, entry):
        state = self._allowed(entry.boot, entry.scope)
        return (
            not entry.retiring
            and self._entries.get(entry.scope.source_key) is entry
            and state is not None
            and state.capture_signature == entry.signature
            and self._clock() - entry.opened_at < WARM_SECONDS
        )

    def _notify(self):
        statuses = tuple(
            (camera.scope, self.status(camera.scope).state)
            for camera in self.controller.inventory().cameras
        )
        signature = self._boot, statuses, self.pending_resources, self.cleanup_failures
        if signature != self._status_signature:
            self._status_signature = signature
            self._changed()

    async def ready(self, boot):
        if self._entries or self._maintenance is not None or self._retiring_boot is not None:
            raise HomeKitOperationError("cleanup_pending")
        self._closed = False
        self._boot = boot
        self._maintenance = asyncio.create_task(self._maintain(boot), name="recording-warm-policy")
        self._maintenance.add_done_callback(_observed)
        await self.refresh(boot)

    async def _maintain(self, boot):
        while self._boot == boot and not self._closed:
            await asyncio.sleep(MAINTENANCE_SECONDS)
            with contextlib.suppress(HomeKitOperationError, ExceptionGroup):
                await self.refresh(boot)

    async def refresh(self, boot):
        async with self._refreshing:
            if self._closed or self._boot != boot:
                return
            failures = []
            for entry in tuple(self._entries.values()):
                if not self._current(entry) or entry.capture is not None and entry.capture.retired:
                    try:
                        await self._stop(entry)
                    except (Exception, asyncio.CancelledError) as error:
                        if isinstance(error, asyncio.CancelledError):
                            raise
                        failures.append(error)
            if self._closed or self._boot != boot:
                return
            for camera in self.controller.inventory().cameras:
                state = self._allowed(boot, camera.scope)
                if (
                    state is None
                    or camera.scope.source_key in self._entries
                    or len(self._entries) >= MAX_WARM
                ):
                    continue
                entry = _Warm(
                    boot, camera.scope, str(uuid4()), state.capture_signature, self._clock()
                )
                self._entries[camera.scope.source_key] = entry
                entry.acquiring = asyncio.create_task(
                    self._acquire(entry), name="recording-warm-open"
                )
                entry.acquiring.add_done_callback(_observed)
            self._notify()
            if failures:
                raise ExceptionGroup("Warm recording cleanup pending", failures)

    async def _acquire(self, entry):
        try:
            entry.capture = await self.captures.open(
                entry.boot,
                entry.scope,
                entry.identity,
                audio=entry.signature[1],
                warm=True,
                owner_opt_in=True,
                mains_confirmed=True,
            )
            if not self._current(entry):
                await entry.capture.close()
                entry.retiring = True
        except asyncio.CancelledError:
            raise
        except Exception:
            # Even an open that raised may still retain native cleanup. Keep
            # its exact identity until captures.stop proves actual retirement.
            entry.failure = True
            entry.retiring = True
        finally:
            self._notify()

    async def _stop(self, entry):
        entry.retiring = True
        task = entry.acquiring
        if task is not None and not task.done() and task is not asyncio.current_task():
            if not task.cancelling():
                task.cancel()
            done, _ = await asyncio.wait({task}, timeout=CLEANUP_SECONDS)
            if not done:
                entry.failure = True
                raise HomeKitOperationError("cleanup_pending")
            with contextlib.suppress(BaseException):
                task.result()
        try:
            await self.captures.stop(entry.boot, entry.scope, entry.identity)
        except BaseException:
            entry.failure = True
            raise
        entry.capture = None
        entry.failure = False
        if self._entries.get(entry.scope.source_key) is entry:
            self._entries.pop(entry.scope.source_key)

    def status(self, scope):
        entry = self._entries.get(scope.source_key)
        if entry is not None and entry.failure:
            return RecordingWarmStatus("cleanup_pending")
        state = self._allowed(self._boot, scope)
        if state is None:
            return RecordingWarmStatus("disabled")
        if entry is None and len(self._entries) >= MAX_WARM:
            return RecordingWarmStatus("capacity")
        if entry is None or entry.capture is None or not self._current(entry):
            return RecordingWarmStatus("warming")
        try:
            snapshot = entry.capture.snapshot()
        except HomeKitOperationError:
            return RecordingWarmStatus("warming")
        return RecordingWarmStatus(
            "ready" if snapshot.ready else "warming",
            snapshot.ready,
            snapshot.available_video_seconds,
            snapshot.available_audio_seconds,
        )

    def authorize_open(self, boot, scope, configuration):
        state = self._allowed(boot, scope)
        if state is None or state.effective_configuration != configuration:
            raise HomeKitOperationError("policy_denied")
        if not self.status(scope).history_ready:
            raise HomeKitOperationError("prebuffer_unavailable")

    async def retired(self, boot):
        if self._boot not in {None, boot}:
            return
        if self._retiring_boot not in {None, boot}:
            return
        self._retiring_boot = boot
        if self._boot == boot:
            self._boot = None
        maintenance = self._maintenance
        if maintenance is not None and maintenance is not asyncio.current_task():
            if not maintenance.done() and not maintenance.cancelling():
                maintenance.cancel()
            done, _ = await asyncio.wait({maintenance}, timeout=CLEANUP_SECONDS)
            if not done:
                raise HomeKitOperationError("cleanup_pending")
            with contextlib.suppress(BaseException):
                maintenance.result()
            if self._maintenance is maintenance:
                self._maintenance = None
        try:
            await asyncio.wait_for(self._refreshing.acquire(), CLEANUP_SECONDS)
        except TimeoutError:
            raise HomeKitOperationError("cleanup_pending") from None
        try:
            results = await asyncio.gather(
                *(
                    self._stop(entry)
                    for entry in tuple(self._entries.values())
                    if entry.boot == boot
                ),
                return_exceptions=True,
            )
        finally:
            self._refreshing.release()
        failures = [result for result in results if isinstance(result, Exception)]
        self._notify()
        if failures:
            raise ExceptionGroup("Warm recording retirement incomplete", failures)
        self._retiring_boot = None

    async def close(self):
        self._closed = True
        boots = {entry.boot for entry in self._entries.values()}
        if self._boot is not None:
            boots.add(self._boot)
        if self._retiring_boot is not None:
            boots.add(self._retiring_boot)
        failures = []
        for boot in boots:
            try:
                await self.retired(boot)
            except Exception as error:
                failures.append(error)
        if failures:
            raise ExceptionGroup("Warm recording close incomplete", failures)


def _observed(task):
    if not task.cancelled():
        task.exception()
