"""Async client for Halo Server's private local administration socket."""

from __future__ import annotations

import asyncio
import itertools
import json
from pathlib import Path
from typing import Any

from halo_server.api.admin import (
    ADMIN_PROTOCOL_VERSION,
    CLOSE_TIMEOUT,
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
)

DEFAULT_TIMEOUT = 95.0


class AdminClientError(RuntimeError):
    """An admin request failed locally or was rejected by the daemon."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class AdminClient:
    """Issue isolated requests without retaining credentials or connections."""

    def __init__(self, socket_path: Path, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.socket_path = socket_path
        self.timeout = timeout
        self._ids = itertools.count(1)

    async def request(self, method: str, **params: Any) -> Any:
        request_id = next(self._ids)
        payload = {
            "version": ADMIN_PROTOCOL_VERSION,
            "id": request_id,
            "method": method,
            "params": params,
        }
        encoded = (
            json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True) + "\n"
        ).encode()
        if len(encoded) > MAX_REQUEST_BYTES:
            raise AdminClientError("request_too_large", "Admin request is too large")

        writer = None
        try:
            async with asyncio.timeout(self.timeout):
                try:
                    reader, writer = await asyncio.open_unix_connection(
                        self.socket_path, limit=MAX_RESPONSE_BYTES
                    )
                except OSError as exc:
                    raise AdminClientError(
                        "daemon_unavailable", "Halo Server daemon is not available"
                    ) from exc
                writer.write(encoded)
                await writer.drain()
                try:
                    response_line = await reader.readline()
                except ValueError as exc:
                    raise AdminClientError("bad_response", "Daemon response is too large") from exc
        except (OSError, TimeoutError) as exc:
            raise AdminClientError("request_failed", "Admin request did not complete") from exc
        finally:
            if writer is not None:
                writer.close()
                try:
                    async with asyncio.timeout(min(self.timeout, CLOSE_TIMEOUT)):
                        await writer.wait_closed()
                except asyncio.CancelledError:
                    writer.transport.abort()
                    raise
                except (OSError, TimeoutError):
                    writer.transport.abort()

        if (
            not response_line
            or len(response_line) > MAX_RESPONSE_BYTES
            or not response_line.endswith(b"\n")
        ):
            raise AdminClientError("bad_response", "Daemon returned an invalid response")
        try:
            response = json.loads(response_line)
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
            raise AdminClientError("bad_response", "Daemon returned invalid JSON") from exc
        if (
            not isinstance(response, dict)
            or type(response.get("version")) is not int
            or response["version"] != ADMIN_PROTOCOL_VERSION
            or type(response.get("id")) is not int
            or response.get("id") != request_id
            or not isinstance(response.get("ok"), bool)
        ):
            raise AdminClientError("bad_response", "Daemon returned an invalid response")
        if response["ok"]:
            return response.get("result")

        error = response.get("error")
        if not isinstance(error, dict):
            raise AdminClientError("bad_response", "Daemon returned an invalid error")
        code = error.get("code")
        message = error.get("message")
        if not isinstance(code, str) or not isinstance(message, str):
            raise AdminClientError("bad_response", "Daemon returned an invalid error")
        raise AdminClientError(code, message)

    async def status(self) -> dict[str, Any]:
        result = await self.request("status")
        if not isinstance(result, dict):
            raise AdminClientError("bad_response", "Daemon returned an invalid status")
        return result

    async def login(
        self,
        email: str,
        password: str,
        otp_code: str | None = None,
    ) -> dict[str, Any]:
        result = await self.request(
            "login",
            email=email,
            password=password,
            otp_code=otp_code,
        )
        if not isinstance(result, dict):
            raise AdminClientError("bad_response", "Daemon returned an invalid login result")
        return result

    async def logout(self) -> dict[str, Any]:
        result = await self.request("logout")
        if not isinstance(result, dict):
            raise AdminClientError("bad_response", "Daemon returned an invalid logout result")
        return result

    async def pair_plugin(self, label: str) -> dict[str, Any]:
        result = await self.request("plugin_pair", label=label)
        if not isinstance(result, dict):
            raise AdminClientError("bad_response", "Daemon returned an invalid pairing result")
        return result

    async def revoke_plugin(self, client_id: str) -> dict[str, Any]:
        result = await self.request("plugin_revoke", client_id=client_id)
        if not isinstance(result, dict):
            raise AdminClientError("bad_response", "Daemon returned an invalid revocation result")
        return result
