"""Halo Server daemon lifecycle and local administration methods."""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import logging
import time
import uuid
from collections import deque
from collections.abc import Coroutine
from typing import Any

from ring_doorbell import AuthenticationError, Requires2FAError

from halo_server import __version__
from halo_server.api.admin import AdminProtocolError, AdminServer
from halo_server.api.events import EventJournal
from halo_server.api.plugin import (
    PLUGIN_PROTOCOL_VERSION,
    PluginApiServer,
    PluginIntercomOpenError,
    PluginIntercomStream,
)
from halo_server.api.tokens import PluginTokenStore, PluginTokenStoreError
from halo_server.incidents.coordinator import (
    IncidentAction,
    IncidentActionType,
    IncidentCoordinator,
)
from halo_server.media.leases import LeasePurpose
from halo_server.media.ring_speaker import supports_ring_camera_intercom
from halo_server.media.rtsp import BufferedMediaConsumer, RtspMediaGateway
from halo_server.media.session import CameraMediaSession, MediaSessionState
from halo_server.paths import ServerPaths
from halo_server.ring.client import RingSessionManager, SessionSupersededError
from halo_server.ring.credentials import CredentialStore, CredentialStoreError
from halo_server.ring.devices import summarize_cameras
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


class _ServiceIntercomStream(PluginIntercomStream):
    def __init__(
        self,
        service: HaloService,
        camera_id: str,
        lease_id: str,
        intercom_generation: int,
        session: CameraMediaSession,
    ) -> None:
        self._service = service
        self._camera_id = camera_id
        self._lease_id = lease_id
        self._intercom_generation = intercom_generation
        self._session = session
        self._closed = False

    async def write(self, payload: bytes) -> None:
        if self._closed:
            raise RuntimeError("intercom stream is closed")
        self._session.push_intercom_generation(self._intercom_generation, payload)

    async def wait_ready(self, timeout: float) -> bool:
        if self._closed:
            return False
        return await asyncio.to_thread(
            self._session.wait_intercom_generation_ready,
            self._intercom_generation,
            timeout,
        )

    async def renew(self) -> bool:
        if self._closed:
            return False
        return await self._service._renew_plugin_intercom(
            self._camera_id,
            self._lease_id,
            self._session,
        )

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
        listener_factory=EventListener,
        media_session_factory=CameraMediaSession,
        plugin_token_store: PluginTokenStore | None = None,
        event_journal: EventJournal | None = None,
        plugin_host: str | None = None,
        plugin_port: int = 18443,
        plugin_api_factory=PluginApiServer,
        rtsp_host: str | None = None,
        rtsp_port: int = 18554,
        rtsp_gateway_factory=RtspMediaGateway,
        restore_retry_initial: float = RESTORE_RETRY_INITIAL_SECONDS,
        restore_retry_max: float = RESTORE_RETRY_MAX_SECONDS,
    ) -> None:
        self.paths = paths
        self._sessions = session_manager or RingSessionManager(
            CredentialStore(paths.credentials_file)
        )
        self._listener_factory = listener_factory
        self._media_session_factory = media_session_factory
        self._plugin_tokens = plugin_token_store or PluginTokenStore(paths.integration_directory)
        self._plugin_events = event_journal or EventJournal()
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
            )
            if plugin_host is not None
            else None
        )
        self._admin = AdminServer(paths.admin_socket, self.handle_admin)
        self._session_change_lock = asyncio.Lock()
        self._camera_media_locks: dict[str, asyncio.Lock] = {}
        self._media_epoch = 0
        self._listener: EventListener | None = None
        self._listener_generation = 0
        self._accept_events = False
        self._media_client = None
        self._media_sessions: dict[str, CameraMediaSession] = {}
        self._intercom_leases: dict[str, str] = {}
        self._media_tasks: set[asyncio.Task] = set()
        self._event_state = EventConnectionState.OFFLINE
        self._incidents: dict[str, IncidentCoordinator] = {}
        self._recent_events: deque[dict[str, Any]] = deque(maxlen=RECENT_EVENT_LIMIT)
        self._recent_actions: deque[dict[str, Any]] = deque(maxlen=RECENT_EVENT_LIMIT)
        self._main_loop: asyncio.AbstractEventLoop | None = None
        self._tick_task: asyncio.Task | None = None
        self._restore_task: asyncio.Task | None = None
        self._repair_requested = False
        self._stop_requested = asyncio.Event()
        self._started_at: float | None = None
        self._restoring = False
        self._last_account_error: str | None = None
        self._account_integrations_ready = False
        self._restore_retry_initial = max(0.01, restore_retry_initial)
        self._restore_retry_max = max(self._restore_retry_initial, restore_retry_max)

    async def start(self) -> None:
        """Start the admin plane, then restore Ring state in the background loop."""
        if self._started_at is not None:
            return
        self.paths.prepare()
        self._main_loop = asyncio.get_running_loop()
        self._stop_requested.clear()
        self._repair_requested = False
        self._account_integrations_ready = False
        self._started_at = time.monotonic()
        self._restoring = True
        try:
            await self._admin.start()
            if self._rtsp_gateway is not None:
                await asyncio.to_thread(self._rtsp_gateway.start)
            if self._plugin_api is not None:
                await self._plugin_api.start()
        except BaseException:
            if self._plugin_api is not None:
                with contextlib.suppress(Exception):
                    await self._plugin_api.stop()
            if self._rtsp_gateway is not None:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(self._rtsp_gateway.stop)
            with contextlib.suppress(Exception):
                await self._admin.stop()
            self._restoring = False
            self._started_at = None
            self._main_loop = None
            raise
        self._tick_task = asyncio.create_task(self._tick_incidents(), name="incident-timer")
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

        if self._plugin_api is not None:
            await attempt("plugin API", self._plugin_api.stop())
        await attempt("admin API", self._admin.stop())
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
        listener = self._listener
        self._listener = None
        if listener is not None:
            await attempt("Ring event listener", asyncio.to_thread(listener.stop))
        for camera_id, coordinator in self._incidents.items():
            self._handle_incident_actions(camera_id, coordinator.stop())
        await attempt("media tasks", self._drain_media_tasks())
        await attempt("media sessions", self._stop_media_sessions("service_stop"))
        await attempt("Ring session", asyncio.to_thread(self._sessions.shutdown))
        self._event_state = EventConnectionState.OFFLINE
        self._account_integrations_ready = False
        self._started_at = None
        self._main_loop = None
        if failures:
            raise ExceptionGroup("Halo Server shutdown failed", failures)

    async def _try_restore_ring_session(self) -> bool:
        """Attempt a cached login and report whether a later retry is warranted."""
        async with self._session_change_lock:
            active = self._sessions.active_client
            self._restoring = True
            try:
                client = (
                    active
                    if active is not None and active.is_authenticated
                    else await asyncio.to_thread(self._sessions.restore)
                )
                if client is not None:
                    return await self._reconcile_account_integrations(client)

                retry = await asyncio.to_thread(self._sessions.has_stored_credentials)
                self._last_account_error = (
                    "Ring session restore is temporarily unavailable; retrying" if retry else None
                )
                return retry
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
        try:
            await self._sync_rtsp_cameras()
            await self._replace_listener(client)
        except Exception:
            self._last_account_error = "Ring account integrations are unavailable; retrying"
            _log.exception("Ring account integration reconciliation failed")
            return True
        self._account_integrations_ready = True
        self._last_account_error = None
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
        if method == "status":
            _require_only(params, set())
            return self.status()
        if method == "login":
            _require_only(params, {"email", "password", "otp_code"})
            return await self._login(
                _required_string(params, "email", max_length=320),
                _required_string(params, "password", max_length=4096),
                _optional_string(params, "otp_code", max_length=32),
            )
        if method == "logout":
            _require_only(params, set())
            return await self._logout()
        if method == "plugin_pair":
            _require_only(params, {"label"})
            return await self._pair_plugin(_required_string(params, "label", max_length=80))
        if method == "plugin_revoke":
            _require_only(params, {"client_id"})
            return await self._revoke_plugin(_required_string(params, "client_id", max_length=32))
        if method == "plugin_list":
            _require_only(params, set())
            return self._plugin_status()
        if method == "shutdown":
            _require_only(params, set())
            self.request_stop()
            return {"stopping": True}
        raise AdminProtocolError("unknown_method", "Unknown Halo Server admin method")

    def status(self) -> dict[str, Any]:
        client = self._sessions.active_client
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
        return {
            "version": __version__,
            "service": "running" if self._started_at is not None else "stopped",
            "uptime_seconds": uptime,
            "account": {
                "authenticated": bool(client and client.is_authenticated),
                "email": self._sessions.account_email,
                "restoring": self._restoring,
                "integrations_ready": bool(
                    client and client.is_authenticated and self._account_integrations_ready
                ),
                "last_error": self._last_account_error,
            },
            "events": {
                "connection": self._event_state,
                "recent": list(self._recent_events)[-25:],
            },
            "cameras": devices,
            "incidents": incidents,
            "media": {
                "control": "lease_controlled",
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
                "sessions": media_sessions,
            },
            "integrations": self._plugin_status(),
            "recent_actions": list(self._recent_actions)[-25:],
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
            try:
                client = await asyncio.to_thread(
                    self._sessions.login,
                    email,
                    password,
                    otp_code,
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
            self._invalidate_media_access()
            self._accept_events = False
            self._listener_generation += 1
            self._account_integrations_ready = False
            for camera_id, coordinator in self._incidents.items():
                self._handle_incident_actions(
                    camera_id,
                    coordinator.stop("account_replaced"),
                )
            await self._sync_rtsp_cameras([])
            await self._drain_media_tasks()
            await self._stop_media_sessions("account_replaced")
            self._media_client = client
            if await self._reconcile_account_integrations(client):
                self._request_account_repair()
            return self.status()

    async def _logout(self) -> dict[str, Any]:
        async with self._session_change_lock:
            self._invalidate_media_access()
            self._accept_events = False
            self._listener_generation += 1
            listener = self._listener
            self._listener = None
            if listener is not None:
                await asyncio.to_thread(listener.stop)
            for camera_id, coordinator in self._incidents.items():
                self._handle_incident_actions(
                    camera_id,
                    coordinator.stop("logout"),
                )
            await self._sync_rtsp_cameras([])
            await self._drain_media_tasks()
            await self._stop_media_sessions("logout")
            self._media_client = None
            self._repair_requested = False
            self._account_integrations_ready = False
            logout_error: CredentialStoreError | None = None
            try:
                await asyncio.to_thread(self._sessions.logout)
            except CredentialStoreError as exc:
                logout_error = exc
            self._event_state = EventConnectionState.OFFLINE
            self._last_account_error = None
            if logout_error is not None:
                raise AdminProtocolError(
                    "credential_storage_failed",
                    "Ring credentials could not be cleared safely",
                ) from logout_error
            return self.status()

    async def _replace_listener(self, client) -> None:
        self._accept_events = False
        self._listener_generation += 1
        generation = self._listener_generation
        previous = self._listener
        self._listener = None
        if previous is not None:
            await asyncio.to_thread(previous.stop)
        listener = self._listener_factory(
            client,
            lambda event: self._receive_event(generation, event),
            lambda state: self._receive_event_state(generation, state),
        )
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
        loop.call_soon_threadsafe(self._process_listener_event, generation, event)

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
        if generation != self._listener_generation:
            return
        self._event_state = state

    def _process_listener_event(
        self,
        generation: int,
        event: NormalizedRingEvent,
    ) -> None:
        if generation != self._listener_generation or not self._accept_events:
            return
        self._process_event(event)

    def _process_event(self, event: NormalizedRingEvent) -> None:
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
            actions = coordinator.on_motion(event.event_id, is_update=event.is_update)
        elif event.type is RingEventType.DOORBELL:
            actions = coordinator.on_doorbell(event.event_id, is_update=event.is_update)
        else:
            actions = []
        generation = coordinator.snapshot().generation
        self._handle_incident_actions(
            event.camera_id,
            actions,
            ensure_event_generation=(
                generation if event.type in {RingEventType.MOTION, RingEventType.DOORBELL} else None
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

    def _handle_incident_actions(
        self,
        camera_id: str,
        actions: list[IncidentAction],
        *,
        ensure_event_generation: int | None = None,
    ) -> None:
        for action in actions:
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
                self._plugin_events.publish(
                    action.type,
                    camera_id=camera_id,
                    payload={
                        "generation": action.generation,
                        "reason": action.reason,
                        "event_id": action.event_id,
                    },
                )
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
            client = self._media_client
            if client is None or client is not self._sessions.active_client:
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
            try:
                await asyncio.to_thread(
                    session.acquire,
                    LeasePurpose.EVENT,
                    lease_id=_event_lease_id(ensure_event_generation),
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
            if epoch != self._media_epoch or self._media_sessions.get(camera_id) is not session:
                await asyncio.to_thread(
                    session.release,
                    _event_lease_id(ensure_event_generation),
                )

    def _create_media_session(self, camera_id: str) -> CameraMediaSession | None:
        client = self._media_client
        if client is None or client is not self._sessions.active_client:
            return None
        device = self._find_media_device(camera_id)
        if device is None:
            return None
        session = self._media_session_factory(client, device)
        self._media_sessions[camera_id] = session
        return session

    def _find_media_device(self, camera_id: str):
        client = self._media_client
        if client is None or client is not self._sessions.active_client:
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
        sessions = list(self._media_sessions.items())
        self._media_sessions.clear()
        self._intercom_leases.clear()
        if sessions:

            async def stop_session(
                camera_id: str,
                session: CameraMediaSession,
            ) -> None:
                async with self._camera_media_lock(camera_id):
                    await asyncio.to_thread(session.stop_all, reason)

            await asyncio.gather(
                *(stop_session(camera_id, session) for camera_id, session in sessions),
                return_exceptions=True,
            )

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
        )

    async def _acquire_rtsp_consumer(
        self,
        camera_id: str,
    ) -> tuple[CameraMediaSession, str]:
        async with self._camera_media_lock(camera_id):
            epoch = self._media_epoch
            session = self._media_sessions.get(camera_id)
            if session is None:
                session = self._create_media_session(camera_id)
            if session is None:
                raise RuntimeError("camera is unavailable")
            lease_id = f"rtsp-{uuid.uuid4().hex}"
            await asyncio.to_thread(
                session.acquire,
                LeasePurpose.SCRYPTED_LIVE,
                lease_id=lease_id,
            )
            if epoch != self._media_epoch or self._media_sessions.get(camera_id) is not session:
                await asyncio.to_thread(session.release, lease_id)
                raise RuntimeError("camera media ownership changed")
            return session, lease_id

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
            if session is None:
                return False
            lease = await asyncio.to_thread(session.renew, lease_id)
            return bool(
                lease is not None
                and epoch == self._media_epoch
                and self._media_sessions.get(camera_id) is session
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
                await asyncio.to_thread(session.release, lease_id)

    async def _open_plugin_intercom(self, camera_id: str) -> PluginIntercomStream:
        async with self._camera_media_lock(camera_id):
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
            lease_id = f"intercom-{uuid.uuid4().hex}"
            lease_acquired = False
            try:
                await asyncio.to_thread(
                    session.acquire,
                    LeasePurpose.INTERCOM,
                    lease_id=lease_id,
                )
                lease_acquired = True
                intercom_generation = session.intercom_generation(lease_id)
                if intercom_generation is None:
                    raise RuntimeError("intercom lease did not become active")
                if epoch != self._media_epoch or self._media_sessions.get(camera_id) is not session:
                    raise RuntimeError("camera media ownership changed")
            except Exception as exc:
                if lease_acquired:
                    with contextlib.suppress(Exception):
                        await asyncio.to_thread(session.release, lease_id)
                _log.exception("Could not start intercom for Ring camera %s", camera_id)
                raise PluginIntercomOpenError(
                    "intercom_start_failed",
                    "Halo Server could not start camera intercom",
                    status=503,
                ) from exc
            self._intercom_leases[camera_id] = lease_id
            _log.info("Halo intercom service lease active camera_id=%s", camera_id)
            return _ServiceIntercomStream(
                self,
                camera_id,
                lease_id,
                intercom_generation,
                session,
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
            if self._intercom_leases.get(camera_id) == lease_id:
                self._intercom_leases.pop(camera_id, None)
            await asyncio.to_thread(session.release, lease_id)
            _log.info("Halo intercom service lease released camera_id=%s", camera_id)

    def _camera_media_lock(self, camera_id: str) -> asyncio.Lock:
        lock = self._camera_media_locks.get(camera_id)
        if lock is None:
            lock = asyncio.Lock()
            self._camera_media_locks[camera_id] = lock
        return lock

    def _invalidate_media_access(self) -> None:
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
