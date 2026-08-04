"""Authenticated versioned HTTP/WebSocket API for Halo plugins."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import ssl
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from aiohttp import WSMsgType, web

from halo_server import __version__
from halo_server.api.events import EventJournal, EventJournalError
from halo_server.api.tokens import (
    PluginIdentity,
    PluginTokenStore,
    PluginTokenStoreError,
)
from halo_server.media.leases import maximum_session_seconds

_log = logging.getLogger(__name__)

PLUGIN_PROTOCOL_VERSION = 2
PROTOCOL_HEADER = "Halo-Protocol-Version"
MAX_REQUEST_BYTES = 16 * 1024
DEFAULT_RATE_LIMIT = 120
RATE_WINDOW_SECONDS = 60.0
EVENT_WAIT_SECONDS = 15.0
MAX_RATE_LIMIT_CLIENTS = 256
MAX_EVENT_CONNECTIONS = 32
MAX_EVENT_CONNECTIONS_PER_CLIENT = 4
INTERCOM_FRAME_BYTES = 1920
INTERCOM_IDLE_TIMEOUT = 15.0
INTERCOM_RENEW_INTERVAL = 10.0
INTERCOM_ACTIVATION_TIMEOUT = 15.0
PLUGIN_IDENTITY_KEY = web.RequestKey("plugin_identity", PluginIdentity)
PLUGIN_TOKEN_KEY = web.RequestKey("plugin_token", str)


class PluginIntercomStream(Protocol):
    async def write(self, payload: bytes) -> None: ...

    async def wait_ready(self, timeout: float) -> bool: ...

    async def renew(self) -> bool: ...

    async def close(self) -> None: ...


class PluginIntercomOpenError(RuntimeError):
    """Stable failure returned before an intercom WebSocket is upgraded."""

    def __init__(self, code: str, message: str, *, status: int) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


class PluginApiServer:
    """Expose capability-limited Halo state to authenticated plugin clients."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        token_store: PluginTokenStore,
        event_journal: EventJournal,
        status_provider: Callable[[], dict[str, Any]],
        media_provider: Callable[[str], dict[str, Any] | None] | None = None,
        intercom_provider: (Callable[[str], Awaitable[PluginIntercomStream]] | None) = None,
        ssl_context: ssl.SSLContext | None = None,
        rate_limit: int = DEFAULT_RATE_LIMIT,
    ) -> None:
        if not _is_loopback_host(host) and ssl_context is None:
            raise ValueError("non-loopback plugin API requires TLS")
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise ValueError("plugin API port is invalid")
        if isinstance(rate_limit, bool) or not isinstance(rate_limit, int) or rate_limit <= 0:
            raise ValueError("plugin API rate limit must be positive")
        self.host = host
        self.port = port
        self.bound_port: int | None = None
        self._token_store = token_store
        self._events = event_journal
        self._status_provider = status_provider
        self._media_provider = media_provider
        self._intercom_provider = intercom_provider
        self._ssl_context = ssl_context
        self._rate_limit = rate_limit
        self._requests: dict[str, deque[float]] = defaultdict(deque)
        self._event_connections: dict[str, int] = {}
        self._event_connection_total = 0
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    async def start(self) -> None:
        if self._runner is not None:
            return
        app = web.Application(
            client_max_size=MAX_REQUEST_BYTES,
            middlewares=[self._error_middleware, self._security_middleware],
        )
        app.add_routes(
            [
                web.get("/v1/health", self._health),
                web.get("/v1/capabilities", self._capabilities),
                web.get("/v1/devices", self._devices),
                web.get("/v1/events", self._event_socket),
                web.get("/v1/intercom/{camera_id}", self._intercom_socket),
            ]
        )
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(
            runner,
            host=self.host,
            port=self.port,
            ssl_context=self._ssl_context,
        )
        try:
            await site.start()
        except BaseException:
            await runner.cleanup()
            raise
        self._runner = runner
        self._site = site
        server = getattr(site, "_server", None)
        sockets = getattr(server, "sockets", None)
        self.bound_port = int(sockets[0].getsockname()[1]) if sockets else self.port

    async def stop(self) -> None:
        runner = self._runner
        self._runner = None
        self._site = None
        self.bound_port = None
        if runner is not None:
            await runner.cleanup()
        self._event_connections.clear()
        self._event_connection_total = 0

    @web.middleware
    async def _error_middleware(self, request, handler):
        try:
            return await handler(request)
        except web.HTTPException as exc:
            code, message = {
                404: ("not_found", "Plugin API route was not found"),
                405: ("method_not_allowed", "Plugin API method is not allowed"),
            }.get(
                exc.status,
                ("request_rejected", "Plugin API request was rejected"),
            )
            return _secure_response(_error_response(code, message, status=exc.status))
        except Exception:
            identity = request.get(PLUGIN_IDENTITY_KEY)
            _log.exception(
                "Unhandled plugin API request method=%s path=%s client_id=%s",
                request.method,
                request.path,
                identity.id if isinstance(identity, PluginIdentity) else "unverified",
            )
            return _secure_response(
                _error_response(
                    "internal_error",
                    "Halo Server could not complete the plugin request",
                    status=500,
                )
            )

    @web.middleware
    async def _security_middleware(self, request, handler):
        if request.headers.get(PROTOCOL_HEADER) != str(PLUGIN_PROTOCOL_VERSION):
            response = _error_response(
                "unsupported_version",
                "Halo plugin protocol version is unsupported",
                status=426,
            )
            response.headers[PROTOCOL_HEADER] = str(PLUGIN_PROTOCOL_VERSION)
            return _secure_response(response)

        authorization = request.headers.get("Authorization", "")
        if not authorization.startswith("Bearer ") or len(authorization) > 256:
            return _secure_response(
                _error_response("unauthorized", "Plugin authentication is required", status=401)
            )
        token = authorization[7:]
        try:
            identity = self._token_store.verify(token)
        except PluginTokenStoreError:
            return _secure_response(
                _error_response(
                    "authentication_unavailable",
                    "Plugin authentication is temporarily unavailable",
                    status=503,
                )
            )
        if identity is None:
            return _secure_response(
                _error_response("unauthorized", "Plugin authentication failed", status=401)
            )
        if not self._allow_request(identity.id):
            return _secure_response(
                _error_response("rate_limited", "Plugin request rate exceeded", status=429)
            )
        request[PLUGIN_IDENTITY_KEY] = identity
        request[PLUGIN_TOKEN_KEY] = token
        response = await handler(request)
        return response if response.prepared else _secure_response(response)

    async def _health(self, _request: web.Request) -> web.Response:
        status = self._status_provider()
        account = status.get("account", {})
        events = status.get("events", {})
        return web.json_response(
            {
                "server_version": __version__,
                "protocol_version": PLUGIN_PROTOCOL_VERSION,
                "service": status.get("service"),
                "ring_authenticated": bool(account.get("authenticated")),
                "event_connection": events.get("connection", "offline"),
                "event_epoch": self._events.epoch,
                "latest_event_sequence": self._events.latest_sequence,
            }
        )

    async def _capabilities(self, _request: web.Request) -> web.Response:
        return web.json_response(
            {
                "protocol_version": PLUGIN_PROTOCOL_VERSION,
                "capabilities": [
                    "devices",
                    "events",
                    "motion",
                    "doorbell",
                    "battery",
                    "online",
                ]
                + (["intercom"] if self._intercom_provider is not None else []),
                "media": {
                    "available": self._media_provider is not None,
                    "transport": "rtsp" if self._media_provider is not None else None,
                    "on_demand": self._media_provider is not None,
                    "two_way_audio": self._intercom_provider is not None,
                    "maximum_session_seconds": maximum_session_seconds(),
                    "reason": (
                        None if self._media_provider is not None else "transport_not_implemented"
                    ),
                },
                "event_epoch": self._events.epoch,
                "latest_event_sequence": self._events.latest_sequence,
            }
        )

    async def _devices(self, _request: web.Request) -> web.Response:
        status = self._status_provider()
        account = status.get("account", {})
        if not isinstance(account, dict):
            account = {}
        if not account.get("authenticated"):
            if account.get("restoring") or account.get("last_error"):
                return _error_response(
                    "ring_unavailable",
                    "Ring device inventory is temporarily unavailable",
                    status=503,
                )
            return _error_response(
                "ring_authentication_required",
                "Ring sign-in is required before device inventory is available",
                status=503,
            )
        if account.get("integrations_ready") is False:
            return _error_response(
                "ring_unavailable",
                "Ring account integrations are still starting",
                status=503,
            )
        devices = status.get("cameras", [])
        incidents = status.get("incidents", {})
        if not isinstance(devices, list):
            devices = []
        if not isinstance(incidents, dict):
            incidents = {}
        states = {
            device["id"]: {
                "motion_detected": bool(incidents.get(device["id"], {}).get("motion_published")),
            }
            for device in devices
            if isinstance(device, dict) and isinstance(device.get("id"), str)
        }
        streams = {}
        if self._media_provider is not None:
            for device in devices:
                if not isinstance(device, dict) or not isinstance(device.get("id"), str):
                    continue
                descriptor = self._media_provider(device["id"])
                if descriptor is not None:
                    published = dict(descriptor)
                    if self._intercom_provider is not None and device.get("has_intercom") is True:
                        published["intercom"] = {
                            "codec": "pcm_s16le",
                            "sample_rate": 48_000,
                            "channels": 1,
                            "frame_duration_ms": 20,
                        }
                    streams[device["id"]] = published
        return web.json_response(
            {
                "event_epoch": self._events.epoch,
                "latest_event_sequence": self._events.latest_sequence,
                "devices": devices,
                "states": states,
                "streams": streams,
            }
        )

    async def _event_socket(self, request: web.Request) -> web.StreamResponse:
        epoch = request.query.get("epoch")
        after_raw = request.query.get("after")
        if (epoch is None) != (after_raw is None):
            return _error_response(
                "bad_request",
                "Event epoch and sequence must be provided together",
                status=400,
            )
        if epoch is None:
            epoch = self._events.epoch
            after_sequence = self._events.latest_sequence
        else:
            try:
                after_sequence = int(after_raw)
            except (TypeError, ValueError):
                return _error_response(
                    "bad_request",
                    "Event sequence is invalid",
                    status=400,
                )
        try:
            replay = self._events.replay(
                epoch=epoch,
                after_sequence=after_sequence,
            )
        except (EventJournalError, ValueError):
            return _error_response(
                "resync_required",
                "Plugin must refresh device state and event position",
                status=409,
            )

        identity = request[PLUGIN_IDENTITY_KEY]
        if not self._claim_event_connection(identity.id):
            return _error_response(
                "connection_limit",
                "Plugin event connection limit reached",
                status=429,
            )
        try:
            return await self._serve_event_socket(
                request,
                epoch=epoch,
                after_sequence=after_sequence,
                replay=replay,
            )
        finally:
            self._release_event_connection(identity.id)

    async def _serve_event_socket(
        self,
        request: web.Request,
        *,
        epoch: str,
        after_sequence: int,
        replay,
    ) -> web.StreamResponse:
        socket = web.WebSocketResponse(heartbeat=30, max_msg_size=MAX_REQUEST_BYTES)
        _secure_response(socket)
        await socket.prepare(request)
        await socket.send_json(
            {
                "type": "hello",
                "protocol_version": PLUGIN_PROTOCOL_VERSION,
                "event_epoch": self._events.epoch,
                "latest_event_sequence": self._events.latest_sequence,
            }
        )
        for event in replay:
            await socket.send_json({"type": "event", "event": event.as_dict()})
            after_sequence = event.sequence

        token = request[PLUGIN_TOKEN_KEY]
        while not socket.closed:
            sent_events = False
            receive_task = asyncio.create_task(socket.receive())
            event_task = asyncio.create_task(
                self._events.wait_for_events(
                    epoch=epoch,
                    after_sequence=after_sequence,
                    timeout=EVENT_WAIT_SECONDS,
                )
            )
            done, pending = await asyncio.wait(
                {receive_task, event_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

            if receive_task in done:
                message = receive_task.result()
                if message.type in {
                    WSMsgType.CLOSE,
                    WSMsgType.CLOSING,
                    WSMsgType.CLOSED,
                    WSMsgType.ERROR,
                }:
                    break
                if message.type in {WSMsgType.TEXT, WSMsgType.BINARY}:
                    await socket.close(code=1008, message=b"client messages are not supported")
                    break

            if event_task in done:
                try:
                    events = event_task.result()
                except EventJournalError:
                    await socket.send_json({"type": "resync_required"})
                    await socket.close(code=1012, message=b"event history gap")
                    break
                for event in events:
                    await socket.send_json({"type": "event", "event": event.as_dict()})
                    after_sequence = event.sequence
                sent_events = bool(events)

            try:
                identity = self._token_store.verify(token)
            except PluginTokenStoreError:
                identity = None
            if identity is None:
                await socket.close(code=4003, message=b"plugin credential revoked")
                break
            if not sent_events:
                await socket.send_json(
                    {
                        "type": "heartbeat",
                        "event_epoch": epoch,
                        "latest_event_sequence": self._events.latest_sequence,
                    }
                )
        return socket

    async def _intercom_socket(self, request: web.Request) -> web.StreamResponse:
        provider = self._intercom_provider
        camera_id = request.match_info.get("camera_id", "")
        if provider is None:
            return _error_response(
                "intercom_unavailable",
                "Halo Server intercom is unavailable",
                status=404,
            )
        if not 1 <= len(camera_id) <= 128:
            return _error_response(
                "bad_request",
                "Camera identifier is invalid",
                status=400,
            )
        status = self._status_provider()
        cameras = status.get("cameras", [])
        if not isinstance(cameras, list):
            cameras = []
        camera = next(
            (
                camera
                for camera in cameras
                if isinstance(camera, dict) and camera.get("id") == camera_id
            ),
            None,
        )
        if camera is None:
            return _error_response(
                "camera_unavailable",
                "Ring camera is unavailable",
                status=404,
            )
        if camera.get("has_intercom") is not True:
            return _error_response(
                "intercom_unsupported",
                "This Ring camera does not support Halo intercom",
                status=404,
            )
        _log.info("Halo intercom WebSocket requested camera_id=%s", camera_id)
        try:
            stream = await provider(camera_id)
        except PluginIntercomOpenError as exc:
            return _error_response(exc.code, str(exc), status=exc.status)

        socket = web.WebSocketResponse(
            heartbeat=15,
            # aiohttp rejects payloads equal to max_msg_size; Halo validates
            # the exact 1,920-byte protocol frame immediately after receipt.
            max_msg_size=INTERCOM_FRAME_BYTES + 1,
        )
        _secure_response(socket)
        prepared = False
        frame_count = 0
        close_reason = "client_closed"
        try:
            await socket.prepare(request)
            prepared = True
            await socket.send_json(
                {
                    "type": "ready",
                    "protocol_version": PLUGIN_PROTOCOL_VERSION,
                }
            )
            _log.info("Halo intercom WebSocket accepted camera_id=%s", camera_id)
            token = request[PLUGIN_TOKEN_KEY]
            next_renewal = time.monotonic() + INTERCOM_RENEW_INTERVAL
            audio_deadline = asyncio.get_running_loop().time() + INTERCOM_IDLE_TIMEOUT
            while not socket.closed:
                try:
                    async with asyncio.timeout_at(audio_deadline):
                        message = await socket.receive()
                except TimeoutError:
                    close_reason = "idle_timeout"
                    await socket.close(code=1008, message=b"intercom audio timed out")
                    break
                if message.type in {WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR}:
                    close_reason = (
                        "socket_error" if message.type is WSMsgType.ERROR else "client_closed"
                    )
                    break
                if message.type is not WSMsgType.BINARY:
                    if message.type not in {WSMsgType.PING, WSMsgType.PONG}:
                        close_reason = "non_binary_frame"
                        await socket.close(code=1003, message=b"binary PCM audio is required")
                        break
                    continue
                payload = bytes(message.data)
                if len(payload) != INTERCOM_FRAME_BYTES:
                    close_reason = "invalid_frame_size"
                    await socket.close(code=1007, message=b"invalid PCM frame size")
                    break
                await stream.write(payload)
                audio_deadline = asyncio.get_running_loop().time() + INTERCOM_IDLE_TIMEOUT
                frame_count += 1
                if frame_count == 1:
                    _log.info(
                        "Halo intercom first PCM frame received camera_id=%s bytes=%d",
                        camera_id,
                        len(payload),
                    )
                    if not await stream.wait_ready(INTERCOM_ACTIVATION_TIMEOUT):
                        close_reason = "speaker_activation_failed"
                        await socket.close(
                            code=1011,
                            message=b"camera speaker activation failed",
                        )
                        break
                    await socket.send_json({"type": "active"})
                    audio_deadline = asyncio.get_running_loop().time() + INTERCOM_IDLE_TIMEOUT
                    _log.info("Halo intercom active camera_id=%s", camera_id)

                now = time.monotonic()
                if now < next_renewal:
                    continue
                try:
                    identity = self._token_store.verify(token)
                except PluginTokenStoreError:
                    identity = None
                if identity is None:
                    close_reason = "credential_revoked"
                    await socket.close(code=4003, message=b"plugin credential revoked")
                    break
                if not await stream.renew():
                    close_reason = "lease_expired"
                    await socket.close(code=1008, message=b"intercom lease expired")
                    break
                next_renewal = now + INTERCOM_RENEW_INTERVAL
        except Exception:
            close_reason = "stream_failure"
            _log.exception("Halo intercom WebSocket failed camera_id=%s", camera_id)
            if not prepared:
                raise
            if not socket.closed:
                await socket.close(code=1011, message=b"intercom stream failed")
        finally:
            with contextlib.suppress(Exception):
                await stream.close()
            _log.info(
                "Halo intercom WebSocket closed camera_id=%s reason=%s frames=%d",
                camera_id,
                close_reason,
                frame_count,
            )
        return socket

    def _allow_request(self, client_id: str) -> bool:
        now = time.monotonic()
        cutoff = now - RATE_WINDOW_SECONDS
        requests = self._requests.get(client_id)
        if requests is None:
            if len(self._requests) >= MAX_RATE_LIMIT_CLIENTS:
                expired = [
                    identity
                    for identity, timestamps in self._requests.items()
                    if not timestamps or timestamps[-1] <= cutoff
                ]
                for identity in expired:
                    self._requests.pop(identity, None)
            if len(self._requests) >= MAX_RATE_LIMIT_CLIENTS:
                oldest = min(
                    self._requests,
                    key=lambda identity: self._requests[identity][-1],
                )
                self._requests.pop(oldest, None)
            requests = deque()
            self._requests[client_id] = requests
        while requests and requests[0] <= cutoff:
            requests.popleft()
        if len(requests) >= self._rate_limit:
            return False
        requests.append(now)
        return True

    def _claim_event_connection(self, client_id: str) -> bool:
        count = self._event_connections.get(client_id, 0)
        if (
            self._event_connection_total >= MAX_EVENT_CONNECTIONS
            or count >= MAX_EVENT_CONNECTIONS_PER_CLIENT
        ):
            return False
        self._event_connections[client_id] = count + 1
        self._event_connection_total += 1
        return True

    def _release_event_connection(self, client_id: str) -> None:
        count = self._event_connections.get(client_id, 0)
        if count <= 1:
            self._event_connections.pop(client_id, None)
        else:
            self._event_connections[client_id] = count - 1
        if count:
            self._event_connection_total -= 1


def _error_response(code: str, message: str, *, status: int) -> web.Response:
    return web.json_response(
        {"error": {"code": code, "message": message}},
        status=status,
    )


def _secure_response(response: web.StreamResponse) -> web.StreamResponse:
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers[PROTOCOL_HEADER] = str(PLUGIN_PROTOCOL_VERSION)
    return response


def _is_loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
