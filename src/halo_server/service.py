"""Halo Server daemon lifecycle and local administration methods."""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import logging
import math
import time
import uuid
from collections import deque
from collections.abc import Callable, Coroutine
from typing import Any

from ring_doorbell import AuthenticationError, Requires2FAError

from halo_server import __version__
from halo_server.api.admin import AdminProtocolError, AdminServer
from halo_server.api.events import EventJournal, EventJournalError
from halo_server.api.plugin import (
    CAMERA_CONTROL_TIMEOUT_SECONDS,
    PLUGIN_PROTOCOL_VERSION,
    PluginApiServer,
    PluginCameraOperationError,
    PluginIntercomOpenError,
    PluginIntercomStream,
)
from halo_server.api.tokens import PluginTokenStore, PluginTokenStoreError
from halo_server.configuration import ServerConfiguration
from halo_server.homekit.bridge import HomeKitBridge
from halo_server.homekit.controller import HomeKitController
from halo_server.homekit.controls import CONTROL_STATE_TTL, observed_controls
from halo_server.homekit.models import HomeKitOperationError
from halo_server.homekit.power import mains_confirmed
from halo_server.homekit.recording_settings import (
    RecordingSettingsConflict,
    RecordingSettingsError,
    configuration_wire,
)
from halo_server.homekit.registry import RegistryConflictError, RegistryError
from halo_server.homekit.service_backend import ServiceHomeKitBackend
from halo_server.incidents.coordinator import (
    IncidentAction,
    IncidentActionType,
    IncidentCoordinator,
)
from halo_server.media.consumers import BufferedMediaConsumer
from halo_server.media.leases import LeasePurpose, maximum_session_seconds
from halo_server.media.manager import CaptureRegistry, acquire_owned, owned_thread
from halo_server.media.ring_media import validate_media_compatibility
from halo_server.media.ring_speaker import supports_ring_camera_intercom
from halo_server.media.session import CameraMediaSession, MediaSessionState
from halo_server.paths import ServerPaths
from halo_server.ring.client import RingSessionManager, SessionSupersededError
from halo_server.ring.credentials import CredentialStore, CredentialStoreError
from halo_server.ring.devices import (
    has_ring_camera_capability,
    summarize_cameras,
    supports_ring_camera_snapshot,
)
from halo_server.ring.events import (
    EventConnectionState,
    EventListener,
    NormalizedRingEvent,
    RingEventType,
)

_log = logging.getLogger(__name__)

TICK_INTERVAL = 0.25
RECENT_EVENT_LIMIT = 100
GATEWAY_CALL_TIMEOUT = 10.0
RESTORE_RETRY_INITIAL_SECONDS = 5.0
RESTORE_RETRY_MAX_SECONDS = 300.0
INVENTORY_REFRESH_INTERVAL_SECONDS = 60.0
SIREN_ON_DURATION_SECONDS = 30
RING_CONTROL_TIMEOUT_SECONDS = CAMERA_CONTROL_TIMEOUT_SECONDS - 1.0


class _ServiceIntercomStream(PluginIntercomStream):
    def __init__(
        self,
        service: HaloService,
        camera_id: str,
        lease_id: str,
        intercom_generation: int,
        session: CameraMediaSession,
        admission: Callable[[], bool] | None = None,
    ) -> None:
        self._service = service
        self._camera_id = camera_id
        self._lease_id = lease_id
        self._intercom_generation = intercom_generation
        self._session = session
        self._admission = admission
        self._closed = False

    def _current(self) -> bool:
        return (
            not self._closed
            and (self._admission is None or self._admission())
            and self._service._account_authority_current()
            and self._service._media_sessions.get(self._camera_id) is self._session
            and self._service._camera_source_current(self._camera_id, self._session)
            and self._service._intercom_leases.get(self._camera_id) == self._lease_id
        )

    async def write(self, payload: bytes) -> None:
        if self._closed:
            raise RuntimeError("intercom stream is closed")
        if not self._current():
            raise RuntimeError("intercom account authority was retired")
        self._session.push_intercom_generation(
            self._intercom_generation, payload, admission=self._current
        )

    async def wait_ready(self, timeout: float) -> bool:
        if not self._current():
            return False
        ready = await asyncio.to_thread(
            self._session.wait_intercom_generation_ready,
            self._intercom_generation,
            timeout,
        )
        return ready and self._current()

    async def renew(self) -> bool:
        if not self._current():
            return False
        renewed = await self._service._renew_plugin_intercom(
            self._camera_id,
            self._lease_id,
            self._session,
        )
        return renewed and self._current()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._service._close_plugin_intercom(
            self._camera_id,
            self._lease_id,
            self._session,
        )


class HaloService:
    """Long-running owner of Ring sessions, events, and incident state."""

    def __init__(
        self,
        paths: ServerPaths,
        *,
        session_manager: RingSessionManager | None = None,
        configuration: ServerConfiguration | None = None,
        homekit_resources=None,
        homekit_bridge_factory=HomeKitBridge,
        listener_factory=EventListener,
        media_session_factory=CameraMediaSession,
        plugin_token_store: PluginTokenStore | None = None,
        event_journal: EventJournal | None = None,
        plugin_host: str | None = None,
        plugin_port: int = 18443,
        plugin_api_factory=PluginApiServer,
        rtsp_host: str | None = None,
        rtsp_port: int = 18554,
        rtsp_gateway_factory=None,
        restore_retry_initial: float = RESTORE_RETRY_INITIAL_SECONDS,
        restore_retry_max: float = RESTORE_RETRY_MAX_SECONDS,
    ) -> None:
        self.paths = paths
        self.configuration = configuration or ServerConfiguration()
        from halo_server.release_execution import validate_operating_mode

        validate_operating_mode(self.configuration)
        from halo_server.owner_admin import OwnerAdministration

        self._owner_admin = OwnerAdministration(self)
        if not self.configuration.compatibility_enabled and (
            plugin_host is not None or rtsp_host is not None
        ):
            raise ValueError("Compatibility listeners require compatibility or dual mode")
        self._sessions = session_manager or RingSessionManager(
            CredentialStore(paths.credentials_file)
        )
        self._listener_factory = listener_factory
        self._media_session_factory = media_session_factory
        self._plugin_tokens = plugin_token_store or PluginTokenStore(paths.integration_directory)
        self._plugin_events = event_journal or EventJournal()
        if rtsp_host is not None and rtsp_gateway_factory is None:
            from halo_server.media.rtsp import RtspMediaGateway

            rtsp_gateway_factory = RtspMediaGateway
        self._rtsp_gateway = (
            rtsp_gateway_factory(
                host=rtsp_host,
                port=rtsp_port,
                open_consumer=self._open_rtsp_consumer,
            )
            if rtsp_host is not None
            else None
        )
        self._plugin_api = (
            plugin_api_factory(
                host=plugin_host,
                port=plugin_port,
                token_store=self._plugin_tokens,
                event_journal=self._plugin_events,
                status_provider=self.status,
                media_provider=(
                    self._rtsp_gateway.stream_descriptor if self._rtsp_gateway is not None else None
                ),
                intercom_provider=self._open_plugin_intercom,
                control_provider=self._set_plugin_camera_control,
                snapshot_provider=self._get_plugin_snapshot,
                provider_admission=True,
            )
            if plugin_host is not None
            else None
        )
        self._admin = AdminServer(paths.admin_socket, self.handle_admin)
        self._session_change_lock = asyncio.Lock()
        self._camera_media_locks: dict[str, asyncio.Lock] = {}
        self._camera_control_locks: dict[str, asyncio.Lock] = {}
        self._media_epoch = 0
        self._control_observations = None
        self._listener: EventListener | None = None
        self._listener_generation = 0
        self._accept_events = False
        self._media_client = None
        self._media_scope = None
        self._captures = CaptureRegistry()
        self._media_sessions: dict[str, CameraMediaSession] = self._captures.active
        self._intercom_leases: dict[str, str] = {}
        self._media_tasks: set[asyncio.Task] = set()
        self._account_tasks: set[asyncio.Task] = set()
        self._event_state = EventConnectionState.OFFLINE
        self._incidents: dict[str, IncidentCoordinator] = {}
        self._recent_events: deque[dict[str, Any]] = deque(maxlen=RECENT_EVENT_LIMIT)
        self._recent_actions: deque[dict[str, Any]] = deque(maxlen=RECENT_EVENT_LIMIT)
        self._main_loop: asyncio.AbstractEventLoop | None = None
        self._tick_task: asyncio.Task | None = None
        self._inventory_task: asyncio.Task | None = None
        self._restore_task: asyncio.Task | None = None
        self._initial_restore_task: asyncio.Task | None = None
        self._repair_requested = False
        self._stop_requested = asyncio.Event()
        self._started_at: float | None = None
        self._restoring = False
        self._last_account_error: str | None = None
        self._account_integrations_ready = False
        self._shutdown_complete = True
        self._source_guard_lease = None
        self._restore_retry_initial = max(0.01, restore_retry_initial)
        self._restore_retry_max = max(self._restore_retry_initial, restore_retry_max)
        self._plugin_events.bind_scope(self._current_account_scope)
        self._homekit_backend = ServiceHomeKitBackend(self)
        self.homekit = HomeKitController(self._homekit_backend)
        self._homekit_bridge = (
            homekit_bridge_factory(
                self.homekit,
                self._homekit_backend,
                resources=homekit_resources,
                paths=paths,
                journal=self._plugin_events,
                incidents=lambda: {key: value.snapshot() for key, value in self._incidents.items()},
                source_state=lambda: str(self._event_state),
            )
            if self.configuration.homekit_enabled
            else None
        )

    def _current_account_scope(self) -> object:
        if hasattr(self._sessions, "active_scope"):
            return self._sessions.active_scope
        client = self._sessions.active_client
        return id(client) if client is not None else None

    def _account_authority_current(self) -> bool:
        return self._media_client is self._sessions.active_client and (
            not hasattr(self._sessions, "active_scope")
            or self._media_scope == self._current_account_scope()
        )

    def _account_admission_options(self) -> dict[str, Any]:
        if getattr(self._sessions, "supports_account_admission", False):
            return {"admission": lambda: not self._stop_requested.is_set()}
        return {}

    def _retire_account_observations(self) -> None:
        self._incidents.clear()
        self._recent_events.clear()
        self._recent_actions.clear()
        self._plugin_events.reset()
        self._event_state = EventConnectionState.OFFLINE

    async def start(self) -> None:
        """Start the admin plane, then restore Ring state in the background loop."""
        if self._started_at is not None:
            return
        from halo_server.release_execution import validate_runtime_state

        if self._source_guard_lease is not None:
            raise RuntimeError("Previous source release retirement is incomplete")
        for camera_id in tuple(self._captures.retiring):
            self._captures.can_admit(camera_id)
        if self._captures.retiring:
            raise RuntimeError("Previous Ring camera teardown is still pending")
        validate_media_compatibility()
        self.paths.prepare()
        from halo_server.release_guard import acquire_source_guard

        lease = acquire_source_guard(self.paths.state_directory)
        self._source_guard_lease = lease
        try:
            validate_runtime_state(self.paths.state_directory)
            if self.configuration.homekit_enabled:
                self.paths.prepare_homekit()
        except BaseException:
            lease.close()
            self._source_guard_lease = None
            raise
        self._main_loop = asyncio.get_running_loop()
        self._stop_requested.clear()
        self._repair_requested = False
        self._account_integrations_ready = False
        self._started_at = time.monotonic()
        self._shutdown_complete = False
        self._restoring = True
        try:
            await self._admin.start()
            if self._rtsp_gateway is not None:
                await asyncio.to_thread(self._rtsp_gateway.start)
            if self._plugin_api is not None:
                await self._plugin_api.start()
        except BaseException:
            cleanup_failed = False
            if self._plugin_api is not None:
                try:
                    await self._plugin_api.stop()
                except BaseException:
                    cleanup_failed = True
            if self._rtsp_gateway is not None:
                try:
                    await asyncio.to_thread(self._rtsp_gateway.stop)
                except BaseException:
                    cleanup_failed = True
            try:
                await self._admin.stop()
            except BaseException:
                cleanup_failed = True
            self._restoring = False
            self._started_at = None
            self._main_loop = None
            self._shutdown_complete = not cleanup_failed
            if not cleanup_failed:
                lease.close()
                self._source_guard_lease = None
            raise
        self._tick_task = asyncio.create_task(self._tick_incidents(), name="incident-timer")
        self._inventory_task = asyncio.create_task(
            self._refresh_inventory_loop(),
            name="ring-inventory-refresh",
        )
        self._initial_restore_task = asyncio.create_task(
            self._initial_restore(), name="initial-ring-session-restore"
        )
        if self._homekit_bridge is not None:
            self._homekit_bridge.start()

    async def _initial_restore(self) -> None:
        if await self._try_restore_ring_session():
            self._request_account_repair()

    async def stop(self) -> None:
        """Stop accepting work and release every daemon-owned resource."""
        failures: list[Exception] = []
        self._stop_requested.set()
        self._repair_requested = False

        async def attempt(label: str, operation: Coroutine[Any, Any, Any]) -> None:
            try:
                await operation
            except Exception as exc:
                failures.append(exc)
                _log.exception("Failed to stop %s", label)

        cancel_pending = getattr(self._sessions, "cancel_pending_operations", None)
        if callable(cancel_pending):
            await attempt("pending Ring authentication", owned_thread(cancel_pending))
        if self._homekit_bridge is not None:
            await attempt("HomeKit adapter", self._homekit_bridge.stop())
        await attempt("HomeKit resources", self.homekit.detach())

        if self._plugin_api is not None:
            await attempt("plugin API", self._plugin_api.stop())
        await attempt("admin API", self._admin.stop())
        await attempt("owner administration", self._owner_admin.close())
        initial_restore = self._initial_restore_task
        self._initial_restore_task = None
        if initial_restore is not None:
            initial_restore.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await initial_restore
        if self._account_tasks:
            await asyncio.gather(*tuple(self._account_tasks), return_exceptions=True)
        if self._rtsp_gateway is not None:
            await attempt("RTSP gateway", asyncio.to_thread(self._rtsp_gateway.stop))
        self._accept_events = False
        self._listener_generation += 1
        tick_task = self._tick_task
        self._tick_task = None
        if tick_task is not None:
            tick_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await tick_task
        restore_task = self._restore_task
        self._restore_task = None
        if restore_task is not None:
            restore_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await restore_task
        inventory_task = self._inventory_task
        self._inventory_task = None
        if inventory_task is not None:
            inventory_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await inventory_task
        listener = self._listener
        self._listener = None
        if listener is not None:
            await attempt("Ring event listener", asyncio.to_thread(listener.stop))
        for camera_id, coordinator in self._incidents.items():
            self._handle_incident_actions(camera_id, coordinator.stop())
        await attempt("media tasks", self._drain_media_tasks())
        await attempt("media sessions", self._stop_media_sessions("service_stop"))
        await attempt("Ring session", asyncio.to_thread(self._sessions.shutdown))
        for camera_id, session in tuple(self._captures.retiring.items()):
            self._captures.finish_retirement(camera_id, session)
        if self._captures.retiring:
            failures.append(RuntimeError("Ring camera receiver teardown is still pending"))
        self._retire_account_observations()
        self._event_state = EventConnectionState.OFFLINE
        self._account_integrations_ready = False
        self._started_at = None
        self._main_loop = None
        self._shutdown_complete = not failures
        if failures:
            raise ExceptionGroup("Halo Server shutdown failed", failures)
        if self._source_guard_lease is not None:
            self._source_guard_lease.close()
            self._source_guard_lease = None

    async def _try_restore_ring_session(self) -> bool:
        """Attempt a cached login and report whether a later retry is warranted."""
        async with self._session_change_lock:
            if self._stop_requested.is_set():
                self._restoring = False
                return False
            active = self._sessions.active_client
            self._restoring = True
            try:
                client = (
                    active
                    if active is not None and active.is_authenticated
                    else await owned_thread(
                        self._sessions.restore, **self._account_admission_options()
                    )
                )
                if client is not None:
                    return await self._reconcile_account_integrations(client)

                retry = await asyncio.to_thread(self._sessions.has_stored_credentials)
                self._last_account_error = (
                    "Ring session restore is temporarily unavailable; retrying" if retry else None
                )
                return retry
            except SessionSupersededError:
                return False
            except CredentialStoreError:
                self._last_account_error = "Stored Ring credentials could not be read safely"
                _log.exception("Ring credential restore failed")
                return False
            except Exception:
                self._last_account_error = "Ring session restore failed; retrying"
                _log.exception("Ring session restore failed")
                return True
            finally:
                self._restoring = False

    async def _reconcile_account_integrations(self, client) -> bool:
        """Bring media registration and Ring events in line with an authenticated client."""
        self._account_integrations_ready = False
        if self._media_client is not client:
            self._media_epoch += 1
        self._media_client = client
        self._media_scope = self._current_account_scope()
        try:
            await self._sync_rtsp_cameras()
            await self._replace_listener(client)
        except Exception:
            self._last_account_error = "Ring account integrations are unavailable; retrying"
            _log.exception("Ring account integration reconciliation failed")
            return True
        self._account_integrations_ready = True
        self._remember_control_observations(client)
        self._last_account_error = None
        await self.homekit.reconcile()
        self._notify_homekit_inventory()
        return False

    def _request_account_repair(self) -> None:
        if self._started_at is None or self._stop_requested.is_set():
            return
        self._repair_requested = True
        task = self._restore_task
        if task is not None and not task.done():
            return
        self._restore_task = asyncio.create_task(
            self._retry_ring_session_restore(),
            name="ring-session-restore",
        )

    async def _retry_ring_session_restore(self) -> None:
        delay = self._restore_retry_initial
        try:
            while not self._stop_requested.is_set():
                try:
                    await asyncio.wait_for(self._stop_requested.wait(), timeout=delay)
                    return
                except TimeoutError:
                    pass

                self._repair_requested = False
                retry = await self._try_restore_ring_session()
                if not retry and not self._repair_requested:
                    return
                delay = min(delay * 2, self._restore_retry_max)
        finally:
            if self._restore_task is asyncio.current_task():
                self._restore_task = None
            if self._repair_requested and not self._stop_requested.is_set():
                self._request_account_repair()

    async def wait_for_stop(self) -> None:
        await self._stop_requested.wait()

    def request_stop(self) -> None:
        self._stop_requested.set()

    async def handle_admin(self, method: str, params: dict[str, Any]) -> Any:
        """Dispatch one authenticated local admin request."""
        if method in self._owner_admin.methods:
            return await self._owner_admin.handle(method, params)
        if method == "status":
            _require_only(params, set())
            return self.status()
        if method == "login":
            _require_only(params, {"email", "password", "otp_code"})
            return await self._owned_account_operation(
                self._login(
                    _required_string(params, "email", max_length=320),
                    _required_string(params, "password", max_length=4096),
                    _optional_string(params, "otp_code", max_length=32),
                )
            )
        if method == "logout":
            _require_only(params, set())
            return await self._owned_account_operation(self._logout())
        if method == "plugin_pair":
            _require_only(params, {"label"})
            return await self._pair_plugin(_required_string(params, "label", max_length=80))
        if method == "plugin_revoke":
            _require_only(params, {"client_id"})
            return await self._revoke_plugin(_required_string(params, "client_id", max_length=32))
        if method == "plugin_list":
            _require_only(params, set())
            return self._plugin_status()
        if method in {"homekit_recording_list", "homekit_recording_warm"}:
            bridge = self._homekit_bridge
            if bridge is None:
                raise AdminProtocolError(
                    "homekit_disabled", "Start a native HomeKit operating mode first"
                )
            try:
                if method == "homekit_recording_list":
                    _require_only(params, {"offset", "limit"})
                    offset, limit = params.get("offset", 0), params.get("limit", 32)
                    if (
                        type(offset) is not int
                        or not 0 <= offset <= 256
                        or type(limit) is not int
                        or not 1 <= limit <= 32
                    ):
                        raise AdminProtocolError("bad_params", "Recording page bounds are invalid")
                    registry, inventory = await bridge.cameras()
                    cameras = {camera.scope.camera_id: camera for camera in inventory.cameras}
                    rows = registry.selected(inventory.profile_id) if inventory.profile_id else ()
                    selected = {
                        camera.scope.camera_id: camera
                        for camera in bridge._broker.controller.inventory().cameras
                    }
                    return {
                        "profile_id": inventory.profile_id,
                        "account_generation": inventory.account_generation,
                        "revision": bridge._recording_settings.revision,
                        "available": bridge._recording_available,
                        "offset": offset,
                        "total": len(rows),
                        "cameras": [
                            {
                                "camera_id": row.camera_id,
                                "owner_warm": settings.owner_warm if settings else False,
                                "mains_confirmed": mains_confirmed(cameras.get(row.camera_id)),
                                "active": settings.preferences.active if settings else False,
                                "audio_active": settings.preferences.audio_active
                                if settings
                                else False,
                                "configuration": configuration_wire(
                                    settings.preferences.configuration
                                )
                                if settings
                                else None,
                                "warm_state": bridge._recording_policy.status(
                                    selected[row.camera_id].scope
                                ).state
                                if row.camera_id in selected
                                else "disabled",
                            }
                            for row in rows[offset : offset + limit]
                            for settings in (
                                bridge._recording_settings.camera(row.profile_id, row.camera_id),
                            )
                        ],
                    }
                _require_only(
                    params,
                    {
                        "profile_id",
                        "account_generation",
                        "camera_id",
                        "expected_revision",
                        "enabled",
                    },
                )
                generation, revision, enabled = (
                    params.get("account_generation"),
                    params.get("expected_revision"),
                    params.get("enabled"),
                )
                if (
                    type(generation) is not int
                    or not 1 <= generation < 2**53
                    or type(revision) is not int
                    or not 0 <= revision < 2**53
                    or type(enabled) is not bool
                ):
                    raise AdminProtocolError(
                        "bad_params", "Recording opt-in parameters are invalid"
                    )
                saved = await bridge.set_recording_owner_warm(
                    _required_string(params, "profile_id", max_length=36),
                    generation,
                    _required_string(params, "camera_id", max_length=128),
                    revision,
                    enabled,
                )
                return {"saved": True, "revision": saved.revision}
            except RecordingSettingsConflict as error:
                raise AdminProtocolError(
                    "configuration_conflict", "Recording settings changed; reload before saving"
                ) from error
            except RecordingSettingsError as error:
                raise AdminProtocolError(
                    "configuration_unavailable",
                    "Recording preferences are unavailable; reload before retrying",
                ) from error
            except HomeKitOperationError as error:
                raise AdminProtocolError(
                    "unsupported" if error.code == "capability_unsupported" else "account_changed",
                    "Recording opt-in requires a selected, current, confirmed mains-only camera",
                ) from error
        if method in {"homekit_list", "homekit_select"}:
            if self._homekit_bridge is None:
                raise AdminProtocolError(
                    "homekit_disabled", "Start a native HomeKit operating mode first"
                )
            try:
                if method == "homekit_list":
                    _require_only(params, {"offset", "limit"})
                    offset, limit = params.get("offset", 0), params.get("limit", 32)
                    if (
                        type(offset) is not int
                        or not 0 <= offset <= 256
                        or type(limit) is not int
                        or not 1 <= limit <= 32
                    ):
                        raise AdminProtocolError("bad_params", "Camera page bounds are invalid")
                    registry, inventory = await self._homekit_bridge.cameras()
                    current = {camera.scope.camera_id: camera for camera in inventory.cameras}
                    rows = [
                        row for row in registry.entries if row.profile_id == inventory.profile_id
                    ]
                    return {
                        "profile_id": inventory.profile_id,
                        "account_generation": inventory.account_generation,
                        "revision": registry.revision,
                        "offset": offset,
                        "total": len(rows),
                        "cameras": [
                            {
                                "camera_id": row.camera_id,
                                "accessory_id": row.accessory_uuid,
                                "username": row.username,
                                "doorbell": row.doorbell,
                                "selected": row.selected,
                                "available": row.camera_id in current,
                                "name": current[row.camera_id].name
                                if row.camera_id in current
                                else "",
                            }
                            for row in rows[offset : offset + limit]
                        ],
                    }
                _require_only(
                    params, {"profile_id", "account_generation", "camera_ids", "expected_revision"}
                )
                identifiers = params.get("camera_ids")
                generation, revision = (
                    params.get("account_generation"),
                    params.get("expected_revision"),
                )
                if (
                    not isinstance(identifiers, list)
                    or len(identifiers) > 256
                    or any(
                        not isinstance(value, str) or not 1 <= len(value) <= 128
                        for value in identifiers
                    )
                    or type(generation) is not int
                    or generation < 1
                    or type(revision) is not int
                    or revision < 0
                ):
                    raise AdminProtocolError(
                        "bad_params", "Camera selection parameters are invalid"
                    )
                registry = await self._homekit_bridge.select(
                    _required_string(params, "profile_id", max_length=36),
                    generation,
                    identifiers,
                    revision,
                )
                return {"saved": True, "revision": registry.revision, "camera_ready": False}
            except RegistryConflictError as exc:
                raise AdminProtocolError(
                    "configuration_conflict",
                    "Camera selection changed; reload the current revision",
                ) from exc
            except RegistryError as exc:
                raise AdminProtocolError(
                    "configuration_unavailable",
                    "Camera selection could not be saved safely; reload before retrying",
                ) from exc
            except HomeKitOperationError as exc:
                raise AdminProtocolError(
                    "account_changed",
                    "Account authority changed; reload the current camera selection",
                ) from exc
        if method == "shutdown":
            _require_only(params, set())
            self.request_stop()
            return {"stopping": True}
        raise AdminProtocolError("unknown_method", "Unknown Halo Server admin method")

    async def _owned_account_operation(self, operation: Coroutine) -> Any:
        task = asyncio.create_task(operation, name="ring-account-operation")
        self._account_tasks.add(task)

        def completed(done: asyncio.Task) -> None:
            self._account_tasks.discard(done)
            if not done.cancelled():
                done.exception()  # Retrieve a failed operation after a client disconnects.

        task.add_done_callback(completed)
        return await asyncio.shield(task)

    def status(self) -> dict[str, Any]:
        client = self._sessions.active_client
        authority_current = self._account_authority_current()
        devices = summarize_cameras(client.all_devices if client is not None else [])
        incidents = {
            camera_id: {
                "phase": snapshot.phase,
                "generation": snapshot.generation,
                "media_ready": snapshot.media_ready,
                "motion_published": snapshot.motion_published,
                "failure": snapshot.failure,
            }
            for camera_id, coordinator in sorted(self._incidents.items())
            if authority_current
            if (snapshot := coordinator.snapshot()).phase != "idle"
        }
        uptime = None
        if self._started_at is not None:
            uptime = max(0.0, time.monotonic() - self._started_at)
        media_sessions = {
            camera_id: {
                "state": snapshot.state,
                "generation": snapshot.generation,
                "lease_count": snapshot.lease_count,
                "lease_purposes": snapshot.lease_purposes,
                "buffer_ready": snapshot.buffer_ready,
                "buffer_packets": snapshot.buffer_packets,
                "buffer_bytes": snapshot.buffer_bytes,
                "opus_frame_duration_ms": snapshot.opus_frame_duration_ms,
                "failure": snapshot.failure,
            }
            for camera_id, session in sorted(self._media_sessions.items())
            if (snapshot := session.snapshot()).lease_count > 0
            or snapshot.state is not MediaSessionState.IDLE
        }
        from halo_server.release_execution import release_status

        return {
            "release": release_status(),
            "version": __version__,
            "service": "running" if self._started_at is not None else "stopped",
            "shutdown_complete": self._shutdown_complete,
            "configuration": {
                "mode": self.configuration.mode.value,
                "revision": self.configuration.revision,
            },
            "homekit": self._homekit_bridge.status()
            if self._homekit_bridge is not None
            else {
                "requested": False,
                "state": "disabled",
                "reason": None,
                "transport_ready": False,
                "camera_ready": False,
            },
            "uptime_seconds": uptime,
            "account": {
                "authenticated": bool(client and client.is_authenticated),
                "email": self._sessions.account_email,
                "restoring": self._restoring,
                "integrations_ready": bool(
                    client
                    and client.is_authenticated
                    and self._account_integrations_ready
                    and authority_current
                ),
                "last_error": self._last_account_error,
            },
            "events": {
                "connection": self._event_state
                if authority_current
                else EventConnectionState.OFFLINE,
                "recent": list(self._recent_events)[-25:] if authority_current else [],
            },
            "cameras": devices,
            "incidents": incidents,
            "media": {
                "control": "lease_controlled",
                "retiring_upstreams": len(self._captures.retiring),
                "maximum_session_seconds": maximum_session_seconds(),
                "gateway": {
                    "enabled": self._rtsp_gateway is not None,
                    "host": (self._rtsp_gateway.host if self._rtsp_gateway is not None else None),
                    "port": (
                        self._rtsp_gateway.bound_port if self._rtsp_gateway is not None else None
                    ),
                },
                "active_upstreams": sum(
                    snapshot["state"] in {MediaSessionState.STARTING, MediaSessionState.READY}
                    for snapshot in media_sessions.values()
                ),
                "sessions": media_sessions if authority_current else {},
            },
            "integrations": self._plugin_status(),
            "recent_actions": list(self._recent_actions)[-25:] if authority_current else [],
        }

    async def _pair_plugin(self, label: str) -> dict[str, Any]:
        try:
            issued = await asyncio.to_thread(self._plugin_tokens.create, label)
        except (PluginTokenStoreError, ValueError) as exc:
            raise AdminProtocolError(
                "plugin_pairing_failed",
                "Plugin credentials could not be created safely",
            ) from exc
        return {
            "client": issued.identity.as_dict(),
            "token": issued.token,
        }

    async def _revoke_plugin(self, client_id: str) -> dict[str, Any]:
        try:
            revoked = await asyncio.to_thread(self._plugin_tokens.revoke, client_id)
        except (PluginTokenStoreError, ValueError) as exc:
            raise AdminProtocolError(
                "plugin_revocation_failed",
                "Plugin credentials could not be revoked safely",
            ) from exc
        if revoked and self._plugin_api is not None:
            await self._plugin_api.revoke_client(client_id)
        if revoked and self._rtsp_gateway is not None:
            try:
                await asyncio.to_thread(self._rtsp_gateway.rotate_credentials)
            except Exception as exc:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(self._rtsp_gateway.stop)
                raise AdminProtocolError(
                    "plugin_revocation_failed",
                    "Plugin media credentials could not be revoked safely; "
                    "the media gateway was stopped",
                ) from exc
        return {
            "client_id": client_id,
            "revoked": revoked,
        }

    def _plugin_status(self) -> dict[str, Any]:
        try:
            clients = [identity.as_dict() for identity in self._plugin_tokens.list_identities()]
            error = None
        except PluginTokenStoreError:
            clients = []
            error = "Plugin credentials could not be read safely"
        return {
            "protocol_version": PLUGIN_PROTOCOL_VERSION,
            "event_epoch": self._plugin_events.epoch,
            "latest_event_sequence": self._plugin_events.latest_sequence,
            "clients": clients,
            "error": error,
            "api": {
                "enabled": self._plugin_api is not None,
                "host": self._plugin_api.host if self._plugin_api is not None else None,
                "port": (self._plugin_api.bound_port if self._plugin_api is not None else None),
            },
        }

    async def _login(
        self,
        email: str,
        password: str,
        otp_code: str | None,
    ) -> dict[str, Any]:
        async with self._session_change_lock:
            if self._stop_requested.is_set():
                raise AdminProtocolError("service_stopping", "Halo Server is stopping")
            try:
                client = await owned_thread(
                    self._sessions.login,
                    email,
                    password,
                    otp_code,
                    **self._account_admission_options(),
                )
            except Requires2FAError as exc:
                raise AdminProtocolError(
                    "two_factor_required",
                    "Ring requires a two-factor code",
                ) from exc
            except AuthenticationError as exc:
                raise AdminProtocolError(
                    "authentication_failed",
                    "Ring rejected the email, password, or two-factor code",
                ) from exc
            except SessionSupersededError as exc:
                raise AdminProtocolError(
                    "session_superseded",
                    "A newer Ring account operation replaced this login",
                ) from exc
            except CredentialStoreError as exc:
                raise AdminProtocolError(
                    "credential_storage_failed",
                    "The Ring session was valid but could not be stored safely",
                ) from exc
            except Exception as exc:
                raise AdminProtocolError(
                    "ring_unavailable",
                    "Ring login could not be completed",
                ) from exc
            if self._stop_requested.is_set() or self._sessions.active_client is not client:
                await owned_thread(self._sessions.shutdown)
                raise AdminProtocolError("service_stopping", "Halo Server is stopping")
            self._invalidate_media_access()
            self._accept_events = False
            self._listener_generation += 1
            self._account_integrations_ready = False
            native_error = None
            try:
                await self.homekit.detach()
            except Exception as exc:
                native_error = exc
                _log.exception("Native resources could not retire after account replacement")
            for camera_id, coordinator in self._incidents.items():
                self._handle_incident_actions(
                    camera_id,
                    coordinator.stop("account_replaced"),
                )
            await self._sync_rtsp_cameras([])
            await self._drain_media_tasks()
            await self._stop_media_sessions("account_replaced")
            self._retire_account_observations()
            self._media_client = client
            self._media_scope = self._current_account_scope()
            if native_error is not None:
                self._last_account_error = "Previous native media cleanup is still pending"
                raise AdminProtocolError(
                    "media_cleanup_failed",
                    "Ring login changed accounts, but previous media cleanup needs repair",
                ) from native_error
            if await self._reconcile_account_integrations(client):
                self._request_account_repair()
            return self.status()

    async def _logout(self) -> dict[str, Any]:
        async with self._session_change_lock:
            self._invalidate_media_access()
            self._accept_events = False
            self._listener_generation += 1
            self._account_integrations_ready = False
            self._repair_requested = False
            failures = []

            async def retire(operation):
                try:
                    await operation
                except Exception as exc:
                    failures.append(exc)
                    _log.exception("Account resource cleanup failed during logout")

            await retire(self.homekit.detach())
            listener = self._listener
            self._listener = None
            if listener is not None:
                await retire(asyncio.to_thread(listener.stop))
            for camera_id, coordinator in self._incidents.items():
                self._handle_incident_actions(
                    camera_id,
                    coordinator.stop("logout"),
                )
            await retire(self._sync_rtsp_cameras([]))
            await retire(self._drain_media_tasks())
            await retire(self._stop_media_sessions("logout"))
            self._retire_account_observations()
            self._media_client = None
            self._media_scope = None
            self._repair_requested = False
            self._account_integrations_ready = False
            logout_error: CredentialStoreError | None = None
            try:
                await owned_thread(self._sessions.logout)
            except CredentialStoreError as exc:
                logout_error = exc
            self._event_state = EventConnectionState.OFFLINE
            self._last_account_error = None
            if logout_error is not None:
                raise AdminProtocolError(
                    "credential_storage_failed",
                    "Ring credentials could not be cleared safely",
                ) from logout_error
            if failures:
                self._last_account_error = "Ring signed out; previous media cleanup needs repair"
                raise AdminProtocolError(
                    "media_cleanup_failed",
                    "Ring signed out, but previous media cleanup needs repair",
                ) from ExceptionGroup("Account cleanup failed", failures)
            return self.status()

    async def _replace_listener(self, client) -> None:
        self._accept_events = False
        self._listener_generation += 1
        generation = self._listener_generation
        previous = self._listener
        self._listener = None
        if previous is not None:
            await asyncio.to_thread(previous.stop)
        listener_args = (
            client,
            lambda event: self._receive_event(generation, event),
            lambda state: self._receive_event_state(generation, state),
        )
        prepare_credentials = getattr(
            self._sessions,
            "prepare_event_listener_credentials",
            None,
        )
        if callable(prepare_credentials):
            credentials, credentials_updated_callback = await asyncio.to_thread(
                prepare_credentials,
                client,
            )
            listener = self._listener_factory(
                *listener_args,
                credentials=credentials,
                credentials_updated_callback=credentials_updated_callback,
            )
        else:
            listener = self._listener_factory(*listener_args)
        try:
            future = listener.start()
            wait_until_started = getattr(listener, "wait_until_started", None)
            if callable(wait_until_started):
                await asyncio.to_thread(wait_until_started)
        except Exception:
            self._event_state = EventConnectionState.OFFLINE
            with contextlib.suppress(Exception):
                await asyncio.to_thread(listener.stop)
            raise
        self._listener = listener
        self._accept_events = True
        add_done_callback = getattr(future, "add_done_callback", None)
        if callable(add_done_callback):
            add_done_callback(
                lambda completed: self._receive_listener_completion(
                    generation,
                    listener,
                    completed,
                )
            )

    def _receive_listener_completion(self, generation: int, listener, future) -> None:
        try:
            error = future.exception()
        except (asyncio.CancelledError, concurrent.futures.CancelledError):
            error = None
        except BaseException as exc:
            error = exc
        loop = self._main_loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(
            self._listener_completed,
            generation,
            listener,
            error,
        )

    def _listener_completed(self, generation: int, listener, error: BaseException | None) -> None:
        if (
            generation != self._listener_generation
            or listener is not self._listener
            or not self._accept_events
            or self._stop_requested.is_set()
        ):
            return
        self._listener = None
        self._accept_events = False
        self._event_state = EventConnectionState.OFFLINE
        self._account_integrations_ready = False
        self._last_account_error = "Ring event listener stopped; retrying"
        self._notify_homekit_inventory()
        if error is None:
            _log.warning("Ring event listener stopped unexpectedly; repairing")
        else:
            _log.error(
                "Ring event listener terminated; repairing: %s",
                error,
                exc_info=(type(error), error, error.__traceback__),
            )
        self._request_account_repair()

    def _receive_event(
        self,
        generation: int,
        event: NormalizedRingEvent,
    ) -> None:
        loop = self._main_loop
        if (
            loop is None
            or loop.is_closed()
            or generation != self._listener_generation
            or not self._accept_events
        ):
            return
        source_device = self._find_media_device(event.camera_id)
        if source_device is None:
            return
        loop.call_soon_threadsafe(self._process_listener_event, generation, event, source_device)

    def _receive_event_state(
        self,
        generation: int,
        state: EventConnectionState,
    ) -> None:
        loop = self._main_loop
        if loop is None or loop.is_closed() or generation != self._listener_generation:
            return
        loop.call_soon_threadsafe(self._set_event_state, generation, state)

    def _set_event_state(
        self,
        generation: int,
        state: EventConnectionState,
    ) -> None:
        if generation != self._listener_generation or not self._account_authority_current():
            return
        self._event_state = state
        self._notify_homekit_inventory()

    def _process_listener_event(
        self,
        generation: int,
        event: NormalizedRingEvent,
        source_device=None,
    ) -> None:
        if (
            generation != self._listener_generation
            or not self._accept_events
            or not self._account_authority_current()
            or source_device is None
            or self._find_media_device(event.camera_id) is not source_device
        ):
            return
        self._process_event(event, source_received_at=event.received_at)

    def _process_event(
        self, event: NormalizedRingEvent, *, source_received_at: float | None = None
    ) -> None:
        self._recent_events.append(
            {
                "event_id": event.event_id,
                "camera_id": event.camera_id,
                "camera_name": event.camera_name,
                "type": event.type,
                "source_kind": event.source_kind,
                "occurred_at": event.occurred_at,
                "received_at": event.received_at,
                "is_update": event.is_update,
            }
        )
        coordinator = self._incidents.setdefault(event.camera_id, IncidentCoordinator())
        if event.type is RingEventType.MOTION:
            actions = coordinator.on_motion(
                event.event_id,
                is_update=event.is_update,
                expires_in=event.source_expires_in,
                received_at=source_received_at,
            )
        elif event.type is RingEventType.DOORBELL:
            actions = coordinator.on_doorbell(event.event_id, is_update=event.is_update)
        else:
            actions = []
        generation = coordinator.snapshot().generation
        self._handle_incident_actions(
            event.camera_id,
            actions,
            source_received_at=source_received_at,
            ensure_event_generation=(
                generation
                if event.type in {RingEventType.MOTION, RingEventType.DOORBELL}
                and coordinator.snapshot().phase != "idle"
                else None
            ),
        )

    async def _tick_incidents(self) -> None:
        while True:
            await asyncio.sleep(TICK_INTERVAL)
            for camera_id, coordinator in list(self._incidents.items()):
                self._handle_incident_actions(camera_id, coordinator.tick())
            for session in list(self._media_sessions.values()):
                await asyncio.to_thread(session.expire)
            self._reconcile_media_state()

    async def _refresh_inventory_loop(self) -> None:
        while not self._stop_requested.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_requested.wait(),
                    timeout=INVENTORY_REFRESH_INTERVAL_SECONDS,
                )
                return
            except TimeoutError:
                pass
            await self._refresh_active_inventory()

    async def _refresh_active_inventory(self) -> None:
        client = self._sessions.active_client
        refresh = getattr(client, "refresh_devices", None)
        if (
            client is None
            or not client.is_authenticated
            or self._media_client is not client
            or not callable(refresh)
        ):
            return
        epoch = self._media_epoch
        try:
            devices = await asyncio.to_thread(refresh)
        except asyncio.CancelledError:
            raise
        except Exception:
            if self._sessions.active_client is client:
                _log.exception("Ring camera inventory refresh failed")
            return

        async with self._session_change_lock:
            if (
                epoch != self._media_epoch
                or self._sessions.active_client is not client
                or self._media_client is not client
            ):
                return
            camera_ids = [device["id"] for device in summarize_cameras(devices)]
            try:
                await self._sync_rtsp_cameras(camera_ids)
                await self._reconcile_refreshed_media(camera_ids)
                await self.homekit.reconcile()
                self._remember_control_observations(
                    client, refreshed_at=getattr(client, "_last_device_update_at", time.monotonic())
                )
                self._notify_homekit_inventory()
            except asyncio.CancelledError:
                raise
            except Exception:
                if self._sessions.active_client is client:
                    self._account_integrations_ready = False
                    self._last_account_error = (
                        "Ring camera inventory integrations are unavailable; retrying"
                    )
                    _log.exception("Ring camera inventory integration failed")
                    self._request_account_repair()

    async def _reconcile_refreshed_media(self, camera_ids: list[str]) -> None:
        available = set(camera_ids)

        def source_current(camera_id: str, session: CameraMediaSession) -> bool:
            owns_source = getattr(session, "owns_source", None)
            return camera_id in available and (
                not callable(owns_source)
                or owns_source(self._media_client, self._find_media_device(camera_id))
            )

        async def reconcile(camera_id: str, session: CameraMediaSession) -> None:
            snapshot = session.snapshot()
            if source_current(camera_id, session) and (
                snapshot.lease_count > 0 or snapshot.state is not MediaSessionState.IDLE
            ):
                return
            async with self._camera_media_lock(camera_id):
                if self._media_sessions.get(camera_id) is not session:
                    return
                snapshot = session.snapshot()
                if source_current(camera_id, session) and (
                    snapshot.lease_count > 0 or snapshot.state is not MediaSessionState.IDLE
                ):
                    return
                self._captures.retire(camera_id, session)
                self._intercom_leases.pop(camera_id, None)
                await owned_thread(session.stop_all, "inventory_refreshed")
                self._captures.finish_retirement(camera_id, session)

        await asyncio.gather(
            *(
                reconcile(camera_id, session)
                for camera_id, session in list(self._media_sessions.items())
            )
        )

    def _handle_incident_actions(
        self,
        camera_id: str,
        actions: list[IncidentAction],
        *,
        ensure_event_generation: int | None = None,
        source_received_at: float | None = None,
    ) -> None:
        for action in actions:
            if not self._account_authority_current():
                continue
            self._recent_actions.append(
                {
                    "camera_id": camera_id,
                    "type": action.type,
                    "generation": action.generation,
                    "reason": action.reason,
                    "event_id": action.event_id,
                }
            )
            if action.type in {
                IncidentActionType.MEDIA_READY,
                IncidentActionType.MEDIA_FAILED,
                IncidentActionType.MOTION_ON,
                IncidentActionType.MOTION_OFF,
                IncidentActionType.DOORBELL,
            }:
                scope = (
                    self._media_scope
                    if hasattr(self._sessions, "active_scope")
                    else id(self._media_client)
                    if self._media_client is not None
                    else None
                )
                try:
                    published = self._plugin_events.publish(
                        action.type,
                        camera_id=camera_id,
                        payload={
                            "generation": action.generation,
                            "reason": action.reason,
                            "event_id": action.event_id,
                        },
                        expected_scope=scope,
                    )
                    if self._homekit_bridge is not None:
                        self._homekit_bridge.notify_event(published, received_at=source_received_at)
                except EventJournalError:
                    # A manager thread can publish a replacement while this
                    # event-loop callback serializes an old source fact.
                    continue
        if ensure_event_generation is not None or any(
            action.type is IncidentActionType.STOP_CAPTURE for action in actions
        ):
            self._spawn_media_actions(
                camera_id,
                actions,
                ensure_event_generation,
            )

    def _spawn_media_actions(
        self,
        camera_id: str,
        actions: list[IncidentAction],
        ensure_event_generation: int | None,
    ) -> None:
        loop = self._main_loop
        if loop is None or loop.is_closed():
            return
        task = loop.create_task(
            self._execute_media_actions(
                camera_id,
                actions,
                ensure_event_generation,
            ),
            name=f"media-actions-{camera_id}",
        )
        self._media_tasks.add(task)
        task.add_done_callback(self._media_task_done)

    async def _execute_media_actions(
        self,
        camera_id: str,
        actions: list[IncidentAction],
        ensure_event_generation: int | None,
    ) -> None:
        async with self._camera_media_lock(camera_id):
            epoch = self._media_epoch
            session = self._media_sessions.get(camera_id)
            for action in actions:
                if action.type is IncidentActionType.STOP_CAPTURE and session is not None:
                    await asyncio.to_thread(
                        session.release,
                        _event_lease_id(action.generation),
                    )
            if ensure_event_generation is None:
                return
            if self.configuration.homekit_enabled and not self.configuration.compatibility_enabled:
                # Direct-mode recording demand has its own scoped policy and
                # lease owner. An unsolicited event must not bypass HomeKit-off
                # by acquiring the compatibility prewarm lease.
                return
            client = self._media_client
            if client is None or not self._account_authority_current():
                return
            if session is None:
                session = self._create_media_session(camera_id)
                if session is None:
                    self._media_failed(
                        camera_id,
                        ensure_event_generation,
                        "camera_unavailable",
                    )
                    return
            if not self._camera_source_current(camera_id, session):
                self._media_failed(camera_id, ensure_event_generation, "camera_unavailable")
                return
            try:
                await acquire_owned(
                    session,
                    LeasePurpose.EVENT,
                    _event_lease_id(ensure_event_generation),
                )
            except Exception:
                if epoch != self._media_epoch or self._media_sessions.get(camera_id) is not session:
                    return
                _log.exception("Could not start event media for Ring camera %s", camera_id)
                self._media_failed(
                    camera_id,
                    ensure_event_generation,
                    "receiver_start_failed",
                )
                return
            if (
                epoch != self._media_epoch
                or self._media_sessions.get(camera_id) is not session
                or not self._account_authority_current()
                or not self._camera_source_current(camera_id, session)
            ):
                await owned_thread(
                    session.release,
                    _event_lease_id(ensure_event_generation),
                )

    def _create_media_session(self, camera_id: str) -> CameraMediaSession | None:
        if not self._captures.can_admit(camera_id):
            return None
        client = self._media_client
        if client is None or not self._account_authority_current():
            return None
        device = self._find_media_device(camera_id)
        if device is None:
            return None
        session = self._media_session_factory(client, device)
        self._media_sessions[camera_id] = session
        return session

    def _camera_source_current(self, camera_id: str, session: CameraMediaSession) -> bool:
        owns_source = getattr(session, "owns_source", None)
        return self._account_authority_current() and (
            not callable(owns_source)
            or owns_source(self._media_client, self._find_media_device(camera_id))
        )

    def _find_media_device(self, camera_id: str):
        client = self._media_client
        if client is None or not self._account_authority_current():
            return None
        return next(
            (
                candidate
                for candidate in client.all_devices
                if str(getattr(candidate, "id", "")) == camera_id
            ),
            None,
        )

    def _reconcile_media_state(self) -> None:
        for camera_id, session in self._media_sessions.items():
            coordinator = self._incidents.get(camera_id)
            if coordinator is None:
                continue
            incident = coordinator.snapshot()
            if incident.phase == "idle":
                continue
            media = session.snapshot()
            if media.state is MediaSessionState.READY and not incident.media_ready:
                self._handle_incident_actions(
                    camera_id,
                    coordinator.on_media_ready(incident.generation),
                )
            elif (
                media.state is MediaSessionState.FAILED
                and media.failure
                and media.failure != incident.failure
            ):
                self._media_failed(camera_id, incident.generation, media.failure)

    def _media_failed(self, camera_id: str, generation: int, failure: str) -> None:
        coordinator = self._incidents.get(camera_id)
        if coordinator is None:
            return
        self._handle_incident_actions(
            camera_id,
            coordinator.on_media_failed(generation, failure),
        )

    async def _drain_media_tasks(self) -> None:
        while self._media_tasks:
            await asyncio.gather(*list(self._media_tasks), return_exceptions=True)

    def _media_task_done(self, task: asyncio.Task) -> None:
        self._media_tasks.discard(task)
        if task.cancelled():
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        if error is not None:
            _log.error(
                "Background media action failed: %s",
                error,
                exc_info=(type(error), error, error.__traceback__),
            )

    async def _stop_media_sessions(self, reason: str) -> None:
        self._invalidate_media_access()
        sessions = list((self._captures.retiring | self._media_sessions).items())
        for camera_id, session in sessions:
            self._captures.retire(camera_id, session)
        self._intercom_leases.clear()
        if sessions:

            async def stop_session(
                camera_id: str,
                session: CameraMediaSession,
            ) -> None:
                async with self._camera_media_lock(camera_id):
                    await owned_thread(session.stop_all, reason)
                    self._captures.finish_retirement(camera_id, session)

            results = await asyncio.gather(
                *(stop_session(camera_id, session) for camera_id, session in sessions),
                return_exceptions=True,
            )
            failures = [result for result in results if isinstance(result, Exception)]
            if failures:
                raise ExceptionGroup("Halo media session shutdown failed", failures)

    async def _sync_rtsp_cameras(self, camera_ids: list[str] | None = None) -> None:
        gateway = self._rtsp_gateway
        if gateway is None:
            return
        if camera_ids is None:
            client = self._media_client
            devices = summarize_cameras(client.all_devices if client is not None else [])
            camera_ids = [device["id"] for device in devices]
        await asyncio.to_thread(gateway.sync_cameras, camera_ids)

    def _open_rtsp_consumer(self, camera_id: str) -> BufferedMediaConsumer:
        session, lease_id = self._run_gateway_call(self._acquire_rtsp_consumer(camera_id))
        return BufferedMediaConsumer(
            buffer=session.buffer,
            renew_callback=lambda: self._renew_rtsp_consumer(camera_id, lease_id),
            close_callback=lambda: self._close_rtsp_consumer(camera_id, lease_id),
            validity_callback=lambda: (
                self._account_authority_current()
                and self._media_sessions.get(camera_id) is session
                and self._camera_source_current(camera_id, session)
                and (
                    not callable(getattr(session, "is_lease_active", None))
                    or session.is_lease_active(lease_id)
                )
            ),
        )

    async def _acquire_rtsp_consumer(
        self,
        camera_id: str,
    ) -> tuple[CameraMediaSession, str]:
        return await self._acquire_camera_consumer(camera_id, LeasePurpose.SCRYPTED_LIVE)

    async def _acquire_camera_consumer(
        self,
        camera_id: str,
        purpose: LeasePurpose,
        *,
        admission: Callable[[], bool] | None = None,
    ) -> tuple[CameraMediaSession, str]:
        prefixes = {
            LeasePurpose.HOMEKIT_LIVE: "homekit",
            LeasePurpose.SCRYPTED_LIVE: "rtsp",
            LeasePurpose.HOMEKIT_RECORDING: "homekit-recording",
            LeasePurpose.HOMEKIT_RECORDING_WARM: "homekit-recording-warm",
        }
        if purpose not in prefixes:
            raise ValueError(
                "camera consumer purpose must be a supported native or live integration"
            )
        async with self._camera_media_lock(camera_id):
            _require_source_admission(admission)
            epoch = self._media_epoch
            if self._media_client is None or not self._account_authority_current():
                raise RuntimeError("camera account authority is unavailable")
            session = self._media_sessions.get(camera_id)
            if session is None:
                session = self._create_media_session(camera_id)
            if session is None:
                raise RuntimeError("camera is unavailable")
            if not self._camera_source_current(camera_id, session):
                raise RuntimeError("camera source reconciliation is pending")
            lease_id = f"{prefixes[purpose]}-{uuid.uuid4().hex}"
            await acquire_owned(session, purpose, lease_id)
            if (
                epoch != self._media_epoch
                or self._media_sessions.get(camera_id) is not session
                or not self._account_authority_current()
                or (admission is not None and not admission())
                or not self._camera_source_current(camera_id, session)
            ):
                await owned_thread(session.release, lease_id)
                raise RuntimeError("camera media ownership changed")
            return session, lease_id

    async def _renew_camera_consumer(
        self,
        camera_id: str,
        session: CameraMediaSession,
        lease_id: str,
        *,
        admission: Callable[[], bool],
    ) -> bool:
        async with self._camera_media_lock(camera_id):
            if (
                self._media_sessions.get(camera_id) is not session
                or not self._camera_source_current(camera_id, session)
                or not admission()
            ):
                return False
            lease = await owned_thread(session.renew, lease_id)
            return (
                lease is not None
                and self._media_sessions.get(camera_id) is session
                and self._camera_source_current(camera_id, session)
                and admission()
            )

    async def _release_camera_consumer(
        self,
        camera_id: str,
        session: CameraMediaSession,
        lease_id: str,
    ) -> None:
        async with self._camera_media_lock(camera_id):
            await owned_thread(session.release, lease_id)
            self._captures.finish_retirement(camera_id, session)

    def _renew_rtsp_consumer(self, camera_id: str, lease_id: str) -> bool:
        try:
            return bool(
                self._run_gateway_call(self._renew_rtsp_consumer_async(camera_id, lease_id))
            )
        except Exception:
            _log.exception("Could not renew RTSP media lease for camera %s", camera_id)
            return False

    async def _renew_rtsp_consumer_async(self, camera_id: str, lease_id: str) -> bool:
        async with self._camera_media_lock(camera_id):
            epoch = self._media_epoch
            session = self._media_sessions.get(camera_id)
            if session is None or not self._account_authority_current():
                return False
            if not self._camera_source_current(camera_id, session):
                return False
            lease = await asyncio.to_thread(session.renew, lease_id)
            return bool(
                lease is not None
                and epoch == self._media_epoch
                and self._media_sessions.get(camera_id) is session
                and self._camera_source_current(camera_id, session)
            )

    def _close_rtsp_consumer(self, camera_id: str, lease_id: str) -> None:
        try:
            self._run_gateway_call(self._close_rtsp_consumer_async(camera_id, lease_id))
        except Exception:
            _log.exception("Could not release RTSP media lease for camera %s", camera_id)

    async def _close_rtsp_consumer_async(self, camera_id: str, lease_id: str) -> None:
        async with self._camera_media_lock(camera_id):
            session = self._media_sessions.get(camera_id)
            if session is not None:
                await owned_thread(session.release, lease_id)

    async def _open_plugin_intercom(
        self, camera_id: str, *, admission: Callable[[], bool] | None = None
    ) -> PluginIntercomStream:
        return await self._open_camera_speaker(camera_id, admission=admission)

    async def _open_camera_speaker(
        self, camera_id: str, *, admission: Callable[[], bool] | None = None
    ) -> PluginIntercomStream:
        async with self._camera_media_lock(camera_id):
            _require_plugin_admission(admission, intercom=True)
            epoch = self._media_epoch
            if camera_id in self._intercom_leases:
                raise PluginIntercomOpenError(
                    "intercom_busy",
                    "Another intercom session is already active for this camera",
                    status=409,
                )
            device = self._find_media_device(camera_id)
            if device is None:
                raise PluginIntercomOpenError(
                    "camera_unavailable",
                    "Ring camera is unavailable",
                    status=404,
                )
            if not supports_ring_camera_intercom(device):
                raise PluginIntercomOpenError(
                    "intercom_unsupported",
                    "This Ring camera does not support Halo intercom",
                    status=404,
                )
            session = self._media_sessions.get(camera_id)
            if session is None:
                session = self._create_media_session(camera_id)
            if session is None:
                raise PluginIntercomOpenError(
                    "camera_unavailable",
                    "Ring camera is unavailable",
                    status=404,
                )
            if not self._camera_source_current(camera_id, session):
                raise PluginIntercomOpenError(
                    "camera_unavailable",
                    "Camera source reconciliation is pending",
                    status=503,
                )
            lease_id = f"intercom-{uuid.uuid4().hex}"
            self._intercom_leases[camera_id] = lease_id
            try:
                await acquire_owned(session, LeasePurpose.INTERCOM, lease_id)
                _require_plugin_admission(admission, intercom=True)
                intercom_generation = session.intercom_generation(lease_id)
                if intercom_generation is None:
                    raise RuntimeError("intercom lease did not become active")
                if (
                    epoch != self._media_epoch
                    or self._media_sessions.get(camera_id) is not session
                    or not self._account_authority_current()
                    or not self._camera_source_current(camera_id, session)
                ):
                    raise RuntimeError("camera media ownership changed")
            except BaseException as exc:
                await owned_thread(session.release, lease_id)
                if self._intercom_leases.get(camera_id) == lease_id:
                    self._intercom_leases.pop(camera_id, None)
                if isinstance(exc, (asyncio.CancelledError, PluginIntercomOpenError)):
                    raise
                _log.exception("Could not start intercom for Ring camera %s", camera_id)
                raise PluginIntercomOpenError(
                    "intercom_start_failed",
                    "Halo Server could not start camera intercom",
                    status=503,
                ) from exc
            _log.info("Halo intercom service lease active camera_id=%s", camera_id)
            return _ServiceIntercomStream(
                self,
                camera_id,
                lease_id,
                intercom_generation,
                session,
                admission,
            )

    async def _renew_plugin_intercom(
        self,
        camera_id: str,
        lease_id: str,
        session: CameraMediaSession,
    ) -> bool:
        async with self._camera_media_lock(camera_id):
            epoch = self._media_epoch
            if (
                self._intercom_leases.get(camera_id) != lease_id
                or self._media_sessions.get(camera_id) is not session
                or not self._account_authority_current()
                or not self._camera_source_current(camera_id, session)
            ):
                return False
            lease = await asyncio.to_thread(session.renew, lease_id)
            if (
                lease is None
                or epoch != self._media_epoch
                or self._media_sessions.get(camera_id) is not session
            ):
                self._intercom_leases.pop(camera_id, None)
                return False
            return True

    async def _close_plugin_intercom(
        self,
        camera_id: str,
        lease_id: str,
        session: CameraMediaSession,
    ) -> None:
        async with self._camera_media_lock(camera_id):
            await owned_thread(session.release, lease_id)
            if self._intercom_leases.get(camera_id) == lease_id:
                self._intercom_leases.pop(camera_id, None)
            _log.info("Halo intercom service lease released camera_id=%s", camera_id)

    def _remember_control_observations(self, client, *, refreshed_at=None):
        if client is not self._media_client or not self._account_authority_current():
            self._control_observations = None
            return
        if refreshed_at is None:
            refreshed_at = getattr(client, "_last_device_update_at", None)
        if (
            type(refreshed_at) not in {int, float}
            or not math.isfinite(refreshed_at)
            or not 0 <= time.monotonic() - refreshed_at <= CONTROL_STATE_TTL
        ):
            self._control_observations = None
            return
        try:
            devices = tuple(client.all_devices)
            rows = {}
            duplicates = set()
            for device in devices:
                identifier = getattr(device, "id", None)
                if type(identifier) not in {str, int}:
                    continue
                identifier = str(identifier).strip()
                if identifier in rows:
                    duplicates.add(identifier)
                rows[identifier] = (device, *observed_controls(device))
            for identifier in duplicates:
                rows.pop(identifier, None)
            self._control_observations = (
                client,
                self._current_account_scope(),
                refreshed_at,
                rows,
            )
        except Exception:
            self._control_observations = None

    def _observed_camera_controls(self, camera_id, device):
        proof = self._control_observations
        if (
            proof is None
            or not self._account_authority_current()
            or not self._account_integrations_ready
        ):
            return None, None
        client, account, received_at, rows = proof
        row = rows.get(camera_id)
        if (
            client is not self._media_client
            or account != self._current_account_scope()
            or not 0 <= time.monotonic() - received_at <= CONTROL_STATE_TTL
            or row is None
            or row[0] is not device
        ):
            return None, None
        return row[1], row[2]

    async def _set_plugin_camera_control(
        self,
        camera_id: str,
        control: str,
        on: bool,
        *,
        admission: Callable[[], bool] | None = None,
    ) -> None:
        await self._set_camera_control(camera_id, control, on, admission=admission)

    async def _set_camera_control(
        self,
        camera_id: str,
        control: str,
        on: bool,
        *,
        admission: Callable[[], bool] | None = None,
    ) -> None:
        async with self._camera_control_lock(camera_id):
            async with self._session_change_lock:
                client, device = self._active_ring_camera(camera_id)
                epoch = self._media_epoch
                if control not in {"light", "siren"} or not has_ring_camera_capability(
                    device, control
                ):
                    raise PluginCameraOperationError(
                        f"{control}_unsupported",
                        "This Ring camera does not support that control",
                        status=404,
                    )
                method_name = "async_set_light" if control == "light" else "async_set_siren"
                setter = getattr(device, method_name, None)
                if not callable(setter):
                    raise PluginCameraOperationError(
                        f"{control}_unsupported",
                        "This Ring camera does not support that control",
                        status=404,
                    )
                value = on if control == "light" else SIREN_ON_DURATION_SECONDS if on else 0
                _require_plugin_admission(admission)

                async def guarded_control():
                    _require_plugin_admission(
                        lambda: (
                            epoch == self._media_epoch
                            and self._media_client is client
                            and self._account_authority_current()
                            and self._find_media_device(camera_id) is device
                            and (admission is None or admission())
                        )
                    )
                    return await setter(value)

                try:
                    future = _submit_ring_coroutine(client, guarded_control())
                except Exception as exc:
                    _log.exception(
                        "Ring camera control submission failed camera_id=%s control=%s",
                        camera_id,
                        control,
                    )
                    raise PluginCameraOperationError(
                        "control_failed",
                        "Ring camera control could not be completed",
                        status=502,
                    ) from exc
            try:
                async with asyncio.timeout(RING_CONTROL_TIMEOUT_SECONDS):
                    await _await_concurrent_future(future)
            except TimeoutError as exc:
                raise PluginCameraOperationError(
                    "control_timeout",
                    "Ring camera control request timed out",
                    status=504,
                ) from exc
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _log.exception(
                    "Ring camera control failed camera_id=%s control=%s",
                    camera_id,
                    control,
                )
                raise PluginCameraOperationError(
                    "control_failed",
                    "Ring camera control could not be completed",
                    status=502,
                ) from exc
            async with self._session_change_lock:
                if (
                    epoch != self._media_epoch
                    or self._sessions.active_client is not client
                    or self._media_client is not client
                    or not self._account_integrations_ready
                ):
                    raise PluginCameraOperationError(
                        "ring_unavailable",
                        "Ring camera operations are temporarily unavailable",
                        status=503,
                    )

    async def _get_plugin_snapshot(
        self,
        camera_id: str,
        fresh: bool,
        *,
        admission: Callable[[], bool] | None = None,
    ) -> bytes:
        return await self._get_camera_snapshot(camera_id, fresh, admission=admission)

    async def _get_camera_snapshot(
        self,
        camera_id: str,
        fresh: bool,
        *,
        admission: Callable[[], bool] | None = None,
    ) -> bytes:
        async with self._session_change_lock:
            client, device = self._active_ring_camera(camera_id)
            epoch = self._media_epoch
            if not supports_ring_camera_snapshot(device):
                raise PluginCameraOperationError(
                    "snapshot_unsupported",
                    "This Ring camera does not support snapshots",
                    status=404,
                )
            submit = getattr(client, "submit_snapshot_for_device", None)
            if not callable(submit):
                raise PluginCameraOperationError(
                    "snapshot_unavailable",
                    "Ring snapshots are unavailable",
                    status=503,
                )
            _require_plugin_admission(admission)
            options = {"fresh": fresh}
            if getattr(client, "supports_snapshot_admission", False):
                options["admission"] = lambda: (
                    epoch == self._media_epoch
                    and self._media_client is client
                    and self._account_authority_current()
                    and self._find_media_device(camera_id) is device
                    and (admission is None or admission())
                )
            try:
                future = submit(device, **options)
            except Exception as exc:
                _log.exception("Ring snapshot submission failed camera_id=%s", camera_id)
                raise PluginCameraOperationError(
                    "snapshot_failed",
                    "Ring snapshot could not be completed",
                    status=502,
                ) from exc
        try:
            snapshot = await _await_concurrent_future(future)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _log.exception("Ring snapshot failed camera_id=%s fresh=%s", camera_id, fresh)
            raise PluginCameraOperationError(
                "snapshot_failed",
                "Ring snapshot could not be completed",
                status=502,
            ) from exc
        async with self._session_change_lock:
            if (
                epoch != self._media_epoch
                or self._sessions.active_client is not client
                or self._media_client is not client
                or not self._account_integrations_ready
            ):
                raise PluginCameraOperationError(
                    "ring_unavailable",
                    "Ring camera operations are temporarily unavailable",
                    status=503,
                )
        if not snapshot:
            raise PluginCameraOperationError(
                "snapshot_unavailable",
                "Ring did not return a snapshot image",
                status=502,
            )
        return bytes(snapshot)

    def _active_ring_camera(self, camera_id: str):
        client = self._sessions.active_client
        if (
            client is None
            or not client.is_authenticated
            or client is not self._media_client
            or not self._account_integrations_ready
            or not self._account_authority_current()
        ):
            raise PluginCameraOperationError(
                "ring_unavailable",
                "Ring camera operations are temporarily unavailable",
                status=503,
            )
        device = next(
            (
                candidate
                for candidate in client.all_devices
                if str(getattr(candidate, "id", "")) == camera_id
            ),
            None,
        )
        if device is None:
            raise PluginCameraOperationError(
                "camera_unavailable",
                "Ring camera is unavailable",
                status=404,
            )
        return client, device

    def _camera_control_lock(self, camera_id: str) -> asyncio.Lock:
        lock = self._camera_control_locks.get(camera_id)
        if lock is None:
            lock = asyncio.Lock()
            self._camera_control_locks[camera_id] = lock
        return lock

    def _camera_media_lock(self, camera_id: str) -> asyncio.Lock:
        lock = self._camera_media_locks.get(camera_id)
        if lock is None:
            lock = asyncio.Lock()
            self._camera_media_locks[camera_id] = lock
        return lock

    def _notify_homekit_inventory(self) -> None:
        if self._homekit_bridge is not None:
            self._homekit_bridge.notify_inventory()
            self._homekit_bridge.notify_event()

    def _invalidate_media_access(self) -> None:
        self._notify_homekit_inventory()
        self._media_epoch += 1
        self._media_client = None

    def _run_gateway_call(self, coroutine: Coroutine[Any, Any, Any]) -> Any:
        loop = self._main_loop
        if loop is None or loop.is_closed():
            coroutine.close()
            raise RuntimeError("Halo Server event loop is unavailable")
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        if running_loop is loop:
            coroutine.close()
            raise RuntimeError("RTSP gateway call cannot block the Halo Server event loop")
        future = asyncio.run_coroutine_threadsafe(coroutine, loop)
        try:
            return future.result(timeout=GATEWAY_CALL_TIMEOUT)
        except BaseException:
            future.cancel()
            raise


def _event_lease_id(incident_generation: int) -> str:
    return f"incident-{incident_generation}"


def _require_source_admission(admission: Callable[[], bool] | None) -> None:
    if admission is not None and not admission():
        raise RuntimeError("camera consumer authority is no longer current")


def _require_plugin_admission(
    admission: Callable[[], bool] | None, *, intercom: bool = False
) -> None:
    if admission is not None and not admission():
        error_type = PluginIntercomOpenError if intercom else PluginCameraOperationError
        raise error_type("unauthorized", "Plugin authentication failed", status=401)


def _submit_ring_coroutine(
    client,
    coroutine: Coroutine[Any, Any, Any],
) -> concurrent.futures.Future:
    try:
        return client.submit(coroutine)
    except BaseException:
        coroutine.close()
        raise


async def _await_concurrent_future(future: concurrent.futures.Future) -> Any:
    try:
        return await asyncio.wrap_future(future)
    except asyncio.CancelledError:
        future.cancel()
        raise


def _require_only(params: dict[str, Any], allowed: set[str]) -> None:
    if unknown := set(params) - allowed:
        raise AdminProtocolError(
            "bad_params",
            f"Unknown admin parameter: {sorted(unknown)[0]}",
        )


def _required_string(
    params: dict[str, Any],
    name: str,
    *,
    max_length: int,
) -> str:
    value = params.get(name)
    if not isinstance(value, str) or not value or len(value) > max_length:
        raise AdminProtocolError("bad_params", f"Admin parameter {name} is invalid")
    return value


def _optional_string(
    params: dict[str, Any],
    name: str,
    *,
    max_length: int,
) -> str | None:
    value = params.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > max_length:
        raise AdminProtocolError("bad_params", f"Admin parameter {name} is invalid")
    return value
