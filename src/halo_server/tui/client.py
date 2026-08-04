"""Async client for Halo Server's private local administration socket."""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
from pathlib import Path
from typing import Any

from halo_server.api.admin import ADMIN_PROTOCOL_VERSION, MAX_REQUEST_BYTES

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

        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(self.socket_path),
                timeout=self.timeout,
            )
        except (OSError, TimeoutError) as exc:
            raise AdminClientError(
                "daemon_unavailable",
                "Halo Server daemon is not available",
            ) from exc

        try:
            writer.write(encoded)
            await asyncio.wait_for(writer.drain(), timeout=self.timeout)
            response_line = await asyncio.wait_for(reader.readline(), timeout=self.timeout)
        except (OSError, TimeoutError) as exc:
            raise AdminClientError("request_failed", "Admin request did not complete") from exc
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

        if not response_line or len(response_line) > MAX_REQUEST_BYTES:
            raise AdminClientError("bad_response", "Daemon returned an invalid response")
        try:
            response = json.loads(response_line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AdminClientError("bad_response", "Daemon returned invalid JSON") from exc
        if (
            not isinstance(response, dict)
            or response.get("version") != ADMIN_PROTOCOL_VERSION
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
