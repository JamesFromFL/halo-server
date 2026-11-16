"""Bind the native facade to the daemon's existing Ring and capture owners."""

from __future__ import annotations

import threading
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from halo_server.api.plugin import PluginCameraOperationError, PluginIntercomOpenError
from halo_server.homekit.models import (
    CameraCapabilities,
    HomeKitOperationError,
    MediaScope,
    NativeCamera,
    NativeInventory,
    safe_display_text,
)
from halo_server.media.consumers import BufferedMediaConsumer
from halo_server.media.formats import MAX_NATIVE_CAMERAS
from halo_server.media.leases import LeasePurpose
from halo_server.media.ring_speaker import (
    VALIDATED_INTERCOM_VIDEO_KINDS,
    supports_ring_camera_intercom,
)
from halo_server.ring.devices import (
    INFERRED_KIND_CAPABILITY_FALLBACKS,
    summarize_device,
    supports_ring_camera_snapshot,
)

if TYPE_CHECKING:
    from halo_server.service import HaloService

_DIAGNOSTIC_SCOPE_ONLY = object()


@dataclass
class _RecordingMediaConsumer(BufferedMediaConsumer):
    video_configuration_callback: Callable | None = None


class ServiceHomeKitBackend:
    """Expose allowlisted evidence, never SDK objects or a second Ring login.

    Device generations describe this runtime's source ownership. They are not
    durable Home accessory identities. Removed/replaced wrappers receive fresh
    generations, even if Ring recycles an API ID.
    """

    def __init__(self, service: HaloService, *, owner_controls: bool = False) -> None:
        self._service = service
        self._owner_controls = owner_controls
        self._lock = threading.RLock()
        self._account: tuple[str, int] | None = None
        self._present: dict[str, tuple[object, int]] = {}
        self._next_device_generation = 0
        self._snapshot = NativeInventory(0, (), False)
        self._valid = False

    def inventory(self) -> NativeInventory:
        with self._lock:
            return self._inventory_locked()

    def _inventory_locked(self) -> NativeInventory:
        service = self._service
        context = getattr(service._sessions, "active_context", None)
        if (
            (not self._owner_controls and not service.configuration.homekit_enabled)
            or service._stop_requested.is_set()
            or context is None
        ):
            self._account = None
            self._present.clear()
            self._valid = False
            return self._publish((), False)
        client, account = context
        key = account.profile_id, account.generation
        if key != self._account:
            self._account = key
            self._present.clear()
            self._next_device_generation = 0
        if (
            not client.is_authenticated
            or service._media_client is not client
            or not service._account_authority_current()
        ):
            self._valid = False
            return self._publish((), False)
        try:
            devices = tuple(client.all_devices)
            rows = [
                (device, summary) for device in devices if (summary := summarize_device(device))
            ]
        except Exception:
            self._valid = False
            return self._publish((), False, key)
        counts = Counter(summary.id for _, summary in rows)
        unique = [(device, summary) for device, summary in rows if counts[summary.id] == 1]
        unique.sort(key=lambda item: item[1].id)
        complete = len(unique) <= MAX_NATIVE_CAMERAS and len(unique) == len(rows)
        current = {}
        cameras = []
        for device, summary in unique[:MAX_NATIVE_CAMERAS]:
            previous = self._present.get(summary.id)
            if previous is not None and previous[0] is device:
                generation = previous[1]
            else:
                self._next_device_generation += 1
                generation = self._next_device_generation
            try:
                source = MediaScope(key[0], summary.id, key[1], generation, 1)
                camera = NativeCamera(
                    scope=source,
                    name=safe_display_text(summary.name),
                    family=safe_display_text(summary.family, 128),
                    kind=safe_display_text(summary.kind, 128),
                    model=safe_display_text(summary.model, 128),
                    capabilities=_capabilities(device),
                    connection_state=_connection_state(device),
                    battery_percent=summary.battery_percent,
                )
            except (ValueError, TypeError):
                complete = False
                continue
            current[summary.id] = device, generation
            cameras.append(camera)
        # A manager thread may publish a replacement while metadata is copied.
        # Never relabel the old account's inventory with the new publication.
        if (
            getattr(service._sessions, "active_context", None) != context
            or not service._account_authority_current()
        ):
            self._valid = False
            return self._publish((), False)
        self._present = current
        self._valid = service._account_integrations_ready
        return self._publish(tuple(cameras), complete and self._valid, key)

    def _publish(
        self,
        cameras: tuple[NativeCamera, ...],
        complete: bool,
        account: tuple[str, int] | None = None,
    ) -> NativeInventory:
        candidate = NativeInventory(
            self._snapshot.revision,
            cameras,
            complete,
            account[0] if account is not None else None,
            account[1] if account is not None else None,
        )
        if candidate != self._snapshot:
            candidate = NativeInventory(
                self._snapshot.revision + 1,
                cameras,
                complete,
                candidate.profile_id,
                candidate.account_generation,
            )
        self._snapshot = candidate
        return candidate

    def scope_current(self, scope: MediaScope) -> bool:
        if not isinstance(scope, MediaScope):
            return False
        with self._lock:
            service = self._service
            context = getattr(service._sessions, "active_context", None)
            present = self._present.get(scope.camera_id)
            if (
                not self._valid
                or present is None
                or context is None
                or service._stop_requested.is_set()
                or (not self._owner_controls and not service.configuration.homekit_enabled)
                or not service._account_integrations_ready
            ):
                return False
            client, account = context
            if (
                (account.profile_id, account.generation)
                != (scope.profile_id, scope.account_generation)
                or service._media_client is not client
                or not client.is_authenticated
                or not service._account_authority_current()
                or present[1] != scope.device_generation
                or self._account != (scope.profile_id, scope.account_generation)
            ):
                return False
            # Do not rebuild every camera DTO for every delivery/PCM frame.
            # Check exact current SDK ownership, including duplicate IDs, while
            # leaving generation allocation and metadata serialization in inventory().
            try:
                matches = [
                    device
                    for device in client.all_devices
                    if type(identifier := getattr(device, "id", None)) in {str, int}
                    and str(identifier).strip() == scope.camera_id
                ]
            except Exception:
                return False
            return (
                len(matches) == 1
                and matches[0] is present[0]
                and (
                    getattr(service._sessions, "active_context", None) == context
                    and service._account_authority_current()
                )
            )

    def diagnostic_scope_current(
        self, scope: MediaScope, *, privacy_token=_DIAGNOSTIC_SCOPE_ONLY
    ) -> bool | None:
        """Check current ownership without making observation wait on inventory.

        None means lock contention, not revoked authority. The diagnostic owner
        must skip and count that observation; it must not admit it using a cached
        permission. Account properties and optional privacy checks also take
        RLocks; each must be available before invoking the ownership checks.
        """
        if not isinstance(scope, MediaScope):
            return False
        if not self._lock.acquire(blocking=False):
            return None
        account_lock = privacy_lock = None
        try:
            candidate = getattr(self._service._sessions, "_lock", None)
            if candidate is not None:
                # Production RingSessionManager uses the same RLock type. Do
                # not re-enter an unfamiliar injected, non-reentrant lock.
                if not isinstance(candidate, type(self._lock)):
                    return None
                if not candidate.acquire(blocking=False):
                    return None
                account_lock = candidate
            if privacy_token is not _DIAGNOSTIC_SCOPE_ONLY:
                candidate = self._service._camera_privacy._lock
                if not isinstance(candidate, type(self._lock)):
                    return None
                if not candidate.acquire(blocking=False):
                    return None
                privacy_lock = candidate
            if not self.scope_current(scope):
                return False
            if privacy_token is _DIAGNOSTIC_SCOPE_ONLY:
                return True
            # Diagnostic media is a separate local recorder, not a HAP viewer.
            # A HomeKit-only override therefore cannot authorize this capture.
            return bool(
                self._service._camera_privacy_epoch(scope.camera_id) == privacy_token
                and self._service._camera_privacy_allowed(scope.camera_id, "live")
                and self._service._camera_privacy_allowed(scope.camera_id, "audio")
            )
        except Exception:
            return False
        finally:
            if privacy_lock is not None:
                privacy_lock.release()
            if account_lock is not None:
                account_lock.release()
            self._lock.release()

    def privacy_token(self, scope: MediaScope):
        """Material Ring/owner authority changes fence already admitted media."""
        return self._service._camera_privacy_epoch(scope.camera_id, homekit=True)

    def permission_allowed(self, scope: MediaScope, permission: str) -> bool:
        if permission == "controls":
            return True
        purpose = {
            "streaming": "live",
            "live_audio": "audio",
            "talkback": "talkback",
            "snapshots": "snapshot",
            "recording": "recording",
            "recording_audio": "audio",
        }.get(permission)
        if purpose is None:
            return False
        return self._service._camera_privacy_allowed(scope.camera_id, purpose, homekit=True)

    def _guard(
        self, scope: MediaScope, admission: Callable[[], bool], permission: str | None = None
    ) -> Callable[[], bool]:
        token = self.privacy_token(scope) if permission is not None else None

        def current() -> bool:
            return (
                self.scope_current(scope)
                and bool(admission())
                and (
                    permission is None
                    or self.privacy_token(scope) == token
                    and self.permission_allowed(scope, permission)
                )
            )

        if not current():
            raise HomeKitOperationError("stale_scope")
        return current

    async def acquire_live(
        self, scope: MediaScope, *, admission: Callable[[], bool]
    ) -> BufferedMediaConsumer:
        return await self._acquire_buffered(scope, LeasePurpose.HOMEKIT_LIVE, admission)

    async def acquire_recording(self, scope, *, admission, warm=False):
        if type(warm) is not bool:
            raise HomeKitOperationError("invalid_request")
        purpose = LeasePurpose.HOMEKIT_RECORDING_WARM if warm else LeasePurpose.HOMEKIT_RECORDING
        return await self._acquire_buffered(scope, purpose, admission, recording=True)

    async def _acquire_buffered(self, scope, purpose, admission, recording=False):
        guard = self._guard(scope, admission, "recording" if recording else "streaming")
        service = self._service
        try:
            session, lease_id = await service._acquire_camera_consumer(
                scope.camera_id,
                purpose,
                admission=guard,
                homekit=True,
            )
        except RuntimeError as exc:
            raise HomeKitOperationError("camera_unavailable") from exc
        consumer_type = _RecordingMediaConsumer if recording else BufferedMediaConsumer
        request_keyframe = getattr(session, "request_keyframe", None)
        consumer = consumer_type(
            buffer=(
                getattr(session, "recording_video_buffer", session.buffer)
                if recording
                else session.buffer
            ),
            original_audio_buffer=getattr(session, "original_audio_buffer", None),
            source_clock_callback=getattr(session, "source_clock_snapshot", None),
            diagnostic_trace=None if recording else getattr(session, "diagnostic_trace", None),
            keyframe_callback=(lambda epoch: request_keyframe(lease_id, epoch, admission=guard))
            if not recording and callable(request_keyframe)
            else None,
            renew_callback=lambda: service._run_gateway_call(
                service._renew_camera_consumer(
                    scope.camera_id,
                    session,
                    lease_id,
                    admission=guard,
                )
            ),
            close_callback=lambda: service._run_gateway_call(
                service._release_camera_consumer(
                    scope.camera_id,
                    session,
                    lease_id,
                )
            ),
            validity_callback=lambda: (
                guard()
                and service._media_sessions.get(scope.camera_id) is session
                and service._camera_source_current(scope.camera_id, session)
                and session.is_lease_active(lease_id)
            ),
        )

        if recording:
            consumer.video_configuration_callback = getattr(
                session, "video_configuration_snapshot", None
            )
        return consumer

    async def snapshot(
        self, scope: MediaScope, fresh: bool, *, admission: Callable[[], bool]
    ) -> bytes:
        if type(fresh) is not bool:
            raise HomeKitOperationError("invalid_request")
        guard = self._guard(scope, admission, "snapshots")
        try:
            image = await self._service._get_camera_snapshot(
                scope.camera_id, fresh, admission=guard, homekit=True
            )
        except PluginCameraOperationError as exc:
            raise HomeKitOperationError(exc.code) from exc
        if not guard():
            raise HomeKitOperationError("stale_scope")
        return image

    async def open_speaker(self, scope: MediaScope, *, admission: Callable[[], bool]):
        guard = self._guard(scope, admission, "talkback")
        try:
            return await self._service._open_camera_speaker(
                scope.camera_id, admission=guard, homekit=True
            )
        except PluginIntercomOpenError as exc:
            raise HomeKitOperationError(exc.code) from exc

    def control_state(self, scope: MediaScope, *, admission: Callable[[], bool]):
        guard = self._guard(scope, admission)
        with self._lock:
            present = self._present.get(scope.camera_id)
            if present is None or present[1] != scope.device_generation:
                raise HomeKitOperationError("stale_scope")
            light, siren = self._service._observed_camera_controls(scope.camera_id, present[0])
        if not guard():
            raise HomeKitOperationError("stale_scope")
        return light, siren

    async def set_control(
        self, scope: MediaScope, control: str, on: bool, *, admission: Callable[[], bool]
    ) -> None:
        if (
            not isinstance(control, str)
            or control not in {"light", "siren"}
            or type(on) is not bool
        ):
            raise HomeKitOperationError("invalid_request")
        guard = self._guard(scope, admission)
        try:
            await self._service._set_camera_control(scope.camera_id, control, on, admission=guard)
        except PluginCameraOperationError as exc:
            raise HomeKitOperationError(
                exc.code,
                possibly_sent=exc.code in {"control_failed", "control_timeout", "ring_unavailable"},
            ) from exc
        if not guard():
            raise HomeKitOperationError("stale_scope", possibly_sent=True)


def _capabilities(device) -> CameraCapabilities:
    def sdk_capability(name: str) -> bool:
        try:
            return device.has_capability(name) is True
        except Exception:
            return False

    sdk = {name: sdk_capability(name) for name in ("video", "light", "siren", "battery")}
    sdk["doorbell"] = sdk_capability("ding")
    try:
        kind = getattr(device, "kind", None)
        video_override = kind in VALIDATED_INTERCOM_VIDEO_KINDS
        inferred = INFERRED_KIND_CAPABILITY_FALLBACKS.get(kind, frozenset())
    except Exception:
        video_override = False
        inferred = frozenset()
    flags = sdk | {
        "video": sdk["video"] or video_override,
        "light": sdk["light"] or "light" in inferred,
        "siren": sdk["siren"] or "siren" in inferred,
        "audio": False,  # Source audio format has not been measured here.
        "snapshot": (sdk["video"] or video_override) and supports_ring_camera_snapshot(device),
        "talkback": (sdk["video"] or video_override) and supports_ring_camera_intercom(device),
    }
    evidence = tuple(
        sorted(
            f"{'sdk' if sdk.get(name) else 'halo'}:{name}"
            for name, enabled in flags.items()
            if enabled
        )
    )
    return CameraCapabilities(**flags, evidence=evidence)


def _connection_state(device) -> bool | None:
    try:
        state = getattr(device, "connection_status", None)
    except Exception:
        return None
    # The legacy SDK `subscribed` flag is not proof of current reachability.
    return True if state == "online" else False if state == "offline" else None
