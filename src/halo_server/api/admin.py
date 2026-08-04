"""Owner-only Unix-socket administration protocol."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import stat
import struct
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from halo_server.paths import ensure_private_directory

_log = logging.getLogger(__name__)

ADMIN_PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 64 * 1024
MAX_SOCKET_PATH_BYTES = 100

AdminHandler = Callable[[str, dict[str, Any]], Awaitable[Any]]


class AdminProtocolError(RuntimeError):
    """A request violates the local administration protocol."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class AdminServer:
    """Serve one bounded JSON request per owner-authenticated connection."""

    def __init__(self, socket_path: Path, handler: AdminHandler) -> None:
        self.socket_path = socket_path
        self._handler = handler
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        if self._server is not None:
            return
        ensure_private_directory(self.socket_path.parent)
        if len(os.fsencode(self.socket_path)) > MAX_SOCKET_PATH_BYTES:
            raise RuntimeError("Halo Server admin socket path is too long")
        await _remove_stale_socket(self.socket_path)
        self._server = await asyncio.start_unix_server(
            self._handle_connection,
            path=self.socket_path,
            limit=MAX_REQUEST_BYTES + 1,
        )
        self.socket_path.chmod(0o600)

    async def stop(self) -> None:
        server = self._server
        self._server = None
        if server is not None:
            server.close()
            await server.wait_closed()
        try:
            path_stat = self.socket_path.lstat()
            if stat.S_ISSOCK(path_stat.st_mode) and path_stat.st_uid == os.getuid():
                self.socket_path.unlink()
        except FileNotFoundError:
            pass

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        request_id: str | int | None = None
        method: str | None = None
        try:
            _require_owner_peer(writer)
            try:
                line = await reader.readline()
            except ValueError as exc:
                raise AdminProtocolError("request_too_large", "Admin request is too large") from exc
            if not line or len(line) > MAX_REQUEST_BYTES or not line.endswith(b"\n"):
                raise AdminProtocolError("bad_request", "Admin request must be one JSON line")
            try:
                request = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise AdminProtocolError("bad_json", "Admin request is not valid JSON") from exc
            request_id, method, params = _validate_request(request)
            result = await self._handler(method, params)
            response = {
                "version": ADMIN_PROTOCOL_VERSION,
                "id": request_id,
                "ok": True,
                "result": result,
            }
        except AdminProtocolError as exc:
            response = _error_response(request_id, exc.code, str(exc))
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
        writer.write(_encode_response(response))
        try:
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()


def _validate_request(request: Any) -> tuple[str | int, str, dict[str, Any]]:
    if not isinstance(request, dict):
        raise AdminProtocolError("bad_request", "Admin request must be a JSON object")
    if request.get("version") != ADMIN_PROTOCOL_VERSION:
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
    if peer_socket is None or not hasattr(socket, "SO_PEERCRED"):
        raise AdminProtocolError("peer_rejected", "Cannot verify admin client identity")
    credentials = peer_socket.getsockopt(
        socket.SOL_SOCKET,
        socket.SO_PEERCRED,
        struct.calcsize("3i"),
    )
    _pid, peer_uid, _gid = struct.unpack("3i", credentials)
    if peer_uid != os.getuid():
        raise AdminProtocolError("peer_rejected", "Admin client is not the daemon owner")


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
    writer.close()
    await writer.wait_closed()
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
    return (
        json.dumps(response, ensure_ascii=True, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode()
