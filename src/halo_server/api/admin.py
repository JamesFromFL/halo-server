"""Owner-only Unix-socket administration protocol."""

from __future__ import annotations

import asyncio
import ctypes
import json
import logging
import os
import socket
import stat
import struct
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from halo_server.paths import ensure_private_directory

_log = logging.getLogger(__name__)

ADMIN_PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_SOCKET_PATH_BYTES = 100
MAX_CONNECTIONS = 16
READ_TIMEOUT = 5.0
HANDLER_TIMEOUT = 90.0
WRITE_TIMEOUT = 5.0
CLOSE_TIMEOUT = 1.0
STOP_TIMEOUT = 5.0

AdminHandler = Callable[[str, dict[str, Any]], Awaitable[Any]]


class AdminProtocolError(RuntimeError):
    """A request violates the local administration protocol."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class AdminServer:
    """Serve one bounded JSON request per owner-authenticated connection."""

    def __init__(
        self,
        socket_path: Path,
        handler: AdminHandler,
        *,
        max_connections: int = MAX_CONNECTIONS,
        read_timeout: float = READ_TIMEOUT,
        handler_timeout: float = HANDLER_TIMEOUT,
        write_timeout: float = WRITE_TIMEOUT,
        close_timeout: float = CLOSE_TIMEOUT,
        stop_timeout: float = STOP_TIMEOUT,
    ) -> None:
        self.socket_path = socket_path
        self._handler = handler
        self._server: asyncio.Server | None = None
        if max_connections < 1:
            raise ValueError("Admin connection limit must be positive")
        self._max_connections = max_connections
        self._read_timeout = read_timeout
        self._handler_timeout = handler_timeout
        self._write_timeout = write_timeout
        self._close_timeout = close_timeout
        self._stop_timeout = stop_timeout
        self._connections: dict[asyncio.Task, asyncio.StreamWriter] = {}
        self._stopping = False
        self._stop_task: asyncio.Task | None = None
        self._socket_identity: tuple[int, int] | None = None

    async def start(self) -> None:
        if self._server is not None:
            return
        if self._stop_task is not None and not self._stop_task.done():
            await asyncio.shield(self._stop_task)
        if self._connections:
            raise RuntimeError("Previous admin connections have not stopped")
        ensure_private_directory(self.socket_path.parent)
        if len(os.fsencode(self.socket_path)) > MAX_SOCKET_PATH_BYTES:
            raise RuntimeError("Halo Server admin socket path is too long")
        await _remove_stale_socket(self.socket_path)
        self._stopping = False
        self._server = await asyncio.start_unix_server(
            self._accept_connection,
            path=self.socket_path,
            limit=MAX_REQUEST_BYTES + 1,
        )
        self.socket_path.chmod(0o600)
        path_stat = self.socket_path.lstat()
        self._socket_identity = path_stat.st_dev, path_stat.st_ino

    async def stop(self) -> None:
        if self._stop_task is None or self._stop_task.done():
            self._stop_task = asyncio.create_task(self._stop(), name="admin-stop")
        await asyncio.shield(self._stop_task)

    async def _stop(self) -> None:
        self._stopping = True
        server = self._server
        self._server = None
        if server is not None:
            server.close()
        tasks = tuple(self._connections)
        for task in tasks:
            self._connections[task].close()
            task.cancel()
        pending = set()
        if tasks:
            _done, pending = await asyncio.wait(tasks, timeout=self._stop_timeout)
        for task in pending:
            if writer := self._connections.get(task):
                writer.transport.abort()
        if server is not None:
            async with asyncio.timeout(self._close_timeout):
                await server.wait_closed()
        try:
            path_stat = self.socket_path.lstat()
            if (
                stat.S_ISSOCK(path_stat.st_mode)
                and path_stat.st_uid == os.getuid()
                and (path_stat.st_dev, path_stat.st_ino) == self._socket_identity
            ):
                self.socket_path.unlink()
        except FileNotFoundError:
            pass
        if pending:
            # Keep ownership until a cancellation-delaying service operation settles.
            raise RuntimeError("Admin connections are still stopping")

    def _accept_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        if self._stopping or len(self._connections) >= self._max_connections:
            writer.close()
            writer.transport.abort()
            return
        task = asyncio.create_task(self._handle_connection(reader, writer), name="admin-request")
        self._connections[task] = writer
        task.add_done_callback(self._connection_finished)

    def _connection_finished(self, task: asyncio.Task) -> None:
        self._connections.pop(task, None)
        if not task.cancelled():
            exception = task.exception()
            if exception is not None:
                _log.error(
                    "Admin connection failed",
                    exc_info=(type(exception), exception, exception.__traceback__),
                )

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            await self._respond(reader, writer)
        finally:
            await _close_writer(writer, self._close_timeout)

    async def _respond(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        request_id: str | int | None = None
        method: str | None = None
        try:
            _require_owner_peer(writer)
            try:
                async with asyncio.timeout(self._read_timeout):
                    line = await reader.readline()
            except ValueError as exc:
                raise AdminProtocolError("request_too_large", "Admin request is too large") from exc
            if len(line) > MAX_REQUEST_BYTES:
                raise AdminProtocolError("request_too_large", "Admin request is too large")
            if not line or not line.endswith(b"\n"):
                raise AdminProtocolError("bad_request", "Admin request must be one JSON line")
            try:
                request = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise AdminProtocolError("bad_json", "Admin request is not valid JSON") from exc
            request_id, method, params = _validate_request(request)
            deadline = asyncio.get_running_loop().time() + self._handler_timeout
            async with asyncio.timeout(self._handler_timeout):
                result = await self._handler(method, params)
            if asyncio.get_running_loop().time() > deadline:
                raise TimeoutError
            response = {
                "version": ADMIN_PROTOCOL_VERSION,
                "id": request_id,
                "ok": True,
                "result": result,
            }
        except AdminProtocolError as exc:
            response = _error_response(request_id, exc.code, str(exc))
        except TimeoutError:
            response = _error_response(request_id, "request_timeout", "Admin request timed out")
        except Exception:
            _log.exception(
                "Unhandled admin request failure method=%s request_id=%r",
                method or "unknown",
                request_id,
            )
            response = _error_response(
                request_id,
                "internal_error",
                "Halo Server could not complete the admin request",
            )
        try:
            if not self._stopping:
                try:
                    encoded = _encode_response(response)
                except AdminProtocolError:
                    encoded = _encode_response(
                        _error_response(
                            request_id, "response_too_large", "Admin response is too large"
                        )
                    )
                except (TypeError, ValueError, RecursionError):
                    _log.exception("Cannot serialize admin response method=%s", method or "unknown")
                    encoded = _encode_response(
                        _error_response(request_id, "internal_error", "Admin response is invalid")
                    )
                writer.write(encoded)
                async with asyncio.timeout(self._write_timeout):
                    await writer.drain()
        except (OSError, TimeoutError):
            pass


def _validate_request(request: Any) -> tuple[str | int, str, dict[str, Any]]:
    if not isinstance(request, dict):
        raise AdminProtocolError("bad_request", "Admin request must be a JSON object")
    if type(request.get("version")) is not int or request["version"] != ADMIN_PROTOCOL_VERSION:
        raise AdminProtocolError("unsupported_version", "Admin protocol version is unsupported")
    request_id = request.get("id")
    if isinstance(request_id, bool) or not isinstance(request_id, (str, int)):
        raise AdminProtocolError("bad_request", "Admin request id must be a string or integer")
    method = request.get("method")
    if not isinstance(method, str) or not method:
        raise AdminProtocolError("bad_request", "Admin method is missing or invalid")
    params = request.get("params", {})
    if not isinstance(params, dict):
        raise AdminProtocolError("bad_request", "Admin params must be a JSON object")
    return request_id, method, params


def _require_owner_peer(writer: asyncio.StreamWriter) -> None:
    peer_socket = writer.get_extra_info("socket")
    peer_uid = _peer_uid(peer_socket) if peer_socket is not None else None
    if peer_uid is None:
        raise AdminProtocolError("peer_rejected", "Cannot verify admin client identity")
    if peer_uid != os.getuid():
        raise AdminProtocolError("peer_rejected", "Admin client is not the daemon owner")


def _peer_uid(peer_socket) -> int | None:
    """Return a verified Unix-domain peer uid on Linux or macOS."""
    if hasattr(socket, "SO_PEERCRED"):
        try:
            credentials = peer_socket.getsockopt(
                socket.SOL_SOCKET,
                socket.SO_PEERCRED,
                struct.calcsize("3i"),
            )
            _pid, peer_uid, _gid = struct.unpack("3i", credentials)
            return peer_uid
        except (OSError, struct.error):
            pass
    if sys.platform == "darwin":
        try:
            return _darwin_peer_uid(peer_socket.fileno())
        except (AttributeError, OSError, TypeError, ValueError):
            return None
    return None


def _darwin_peer_uid(descriptor: int) -> int:
    """Read a Unix-domain peer uid through macOS libc ``getpeereid``."""
    libc = ctypes.CDLL(None, use_errno=True)
    getpeereid = libc.getpeereid
    getpeereid.argtypes = [
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_uint),
        ctypes.POINTER(ctypes.c_uint),
    ]
    getpeereid.restype = ctypes.c_int
    peer_uid = ctypes.c_uint()
    peer_gid = ctypes.c_uint()
    if getpeereid(descriptor, ctypes.byref(peer_uid), ctypes.byref(peer_gid)) != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))
    return peer_uid.value


async def _remove_stale_socket(path: Path) -> None:
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(path_stat.st_mode) or path_stat.st_uid != os.getuid():
        raise RuntimeError("Refusing to replace an unsafe admin socket path")

    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(path),
            timeout=0.25,
        )
    except (OSError, TimeoutError):
        path.unlink()
        return
    await _close_writer(writer, CLOSE_TIMEOUT)
    raise RuntimeError("Halo Server is already running")


def _error_response(
    request_id: str | int | None,
    code: str,
    message: str,
) -> dict[str, Any]:
    return {
        "version": ADMIN_PROTOCOL_VERSION,
        "id": request_id,
        "ok": False,
        "error": {"code": code, "message": message},
    }


def _encode_response(response: dict[str, Any]) -> bytes:
    encoded = bytearray()
    encoder = json.JSONEncoder(ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    for chunk in encoder.iterencode(response):
        part = chunk.encode()
        if len(encoded) + len(part) + 1 > MAX_RESPONSE_BYTES:
            raise AdminProtocolError("response_too_large", "Admin response is too large")
        encoded.extend(part)
    encoded.extend(b"\n")
    return bytes(encoded)


async def _close_writer(writer: asyncio.StreamWriter, timeout: float) -> None:
    writer.close()
    try:
        async with asyncio.timeout(timeout):
            await writer.wait_closed()
    except asyncio.CancelledError:
        writer.transport.abort()
        raise
    except (OSError, TimeoutError):
        writer.transport.abort()
