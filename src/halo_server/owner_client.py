"""Async client for Halo Server's private local administration socket."""

from __future__ import annotations

import asyncio
import itertools
import json
import re
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

    async def owner_state(self) -> dict[str, Any]:
        """Fetch the bounded management view, without heavy media diagnostics."""
        from halo_server.owner_state import MAX_OWNER_STATE_BYTES

        result = await self._object("owner_state")
        if (
            result.get("schema") != 1
            or not isinstance(result.get("epoch"), str)
            or any(
                not isinstance(result.get(key), dict)
                for key in ("daemon", "ring", "scope", "homekit", "activity")
            )
            or not isinstance(result.get("devices"), list)
            or len(result["devices"]) > 256
            or type(result.get("truncated")) is not bool
            or len(json.dumps(result, ensure_ascii=True, separators=(",", ":")).encode())
            > MAX_OWNER_STATE_BYTES
        ):
            raise AdminClientError("bad_response", "Daemon returned an invalid owner state")
        return result

    async def login(
        self,
        email: str,
        password: str,
        otp_code: str | None = None,
        *,
        expected_epoch: str | None = None,
    ) -> dict[str, Any]:
        result = await self.request(
            "login",
            email=email,
            password=password,
            otp_code=otp_code,
            **({"expected_epoch": expected_epoch} if expected_epoch is not None else {}),
        )
        if not isinstance(result, dict):
            raise AdminClientError("bad_response", "Daemon returned an invalid login result")
        return result

    async def devices_monitoring_set(
        self, *, profile_id: str, account_generation: int, expected_revision: int, enabled: bool
    ) -> dict[str, Any]:
        return await self._object(
            "devices_monitoring_set",
            profile_id=profile_id,
            account_generation=account_generation,
            expected_revision=expected_revision,
            enabled=enabled,
        )

    async def devices_bridge_set(
        self,
        *,
        profile_id: str,
        account_generation: int,
        expected_revision: int,
        enabled: bool,
        auto_import: bool,
        included_devices: list[str],
        excluded_devices: list[str],
        excluded_locations: list[str],
        excluded_kinds: list[str],
        excluded_mappings: list[str],
    ) -> dict[str, Any]:
        return await self._object(
            "devices_bridge_set",
            profile_id=profile_id,
            account_generation=account_generation,
            expected_revision=expected_revision,
            enabled=enabled,
            auto_import=auto_import,
            included_devices=included_devices,
            excluded_devices=excluded_devices,
            excluded_locations=excluded_locations,
            excluded_kinds=excluded_kinds,
            excluded_mappings=excluded_mappings,
        )

    async def devices_bridge_publication_set(self, **params):
        return await self._object("devices_bridge_publication_set", **params)

    async def devices_alarm_policy_set(self, **params):
        return await self._object("devices_alarm_policy_set", **params)

    async def devices_alarm_mode_set(self, **params):
        return await self._object("devices_alarm_mode_set", **params)

    async def devices_bridge_pairing_get(self):
        return await self._object("devices_bridge_pairing_get")

    async def devices_bridge_pairing_reveal(self, **params):
        return await self._object("devices_bridge_pairing_reveal", **params)

    async def devices_bridge_pairing_reset(self, **params):
        return await self._object("devices_bridge_pairing_reset", **params)

    async def logout(self, *, expected_epoch: str | None = None) -> dict[str, Any]:
        result = await self.request(
            "logout", **({"expected_epoch": expected_epoch} if expected_epoch is not None else {})
        )
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

    async def _object(self, method: str, **params: Any) -> dict[str, Any]:
        result = await self.request(method, **params)
        if not isinstance(result, dict):
            raise AdminClientError("bad_response", "Daemon returned an invalid admin result")
        return result

    async def homekit_list(self, *, offset: int = 0, limit: int = 32) -> dict[str, Any]:
        return _camera_page(await self._object("homekit_list", offset=offset, limit=limit), offset)

    async def homekit_cameras(self) -> dict[str, Any]:
        """Read one bounded coherent selection view; never combine changing pages."""
        return await self._camera_pages(self.homekit_list)

    async def _camera_pages(self, fetch, *, revision_key="revision") -> dict[str, Any]:
        first = await fetch(offset=0, limit=32)
        rows = list(first["cameras"])
        keys = ("profile_id", "account_generation", revision_key, "boot_id")
        token = tuple(first.get(key) for key in keys)
        while len(rows) < first["total"]:
            page = await fetch(offset=len(rows), limit=32)
            if (
                tuple(page.get(key) for key in keys) != token
                or page["total"] != first["total"]
                or not page["cameras"]
            ):
                raise AdminClientError("configuration_conflict", "Camera view changed; refresh")
            rows.extend(page["cameras"])
        if len(rows) != first["total"] or len({row["camera_id"] for row in rows}) != len(rows):
            raise AdminClientError("bad_response", "Daemon returned an invalid camera page")
        return first | {"cameras": rows}

    async def homekit_select(
        self,
        profile_id: str,
        account_generation: int,
        camera_ids: list[str],
        expected_revision: int,
    ) -> dict[str, Any]:
        return await self._object(
            "homekit_select",
            profile_id=profile_id,
            account_generation=account_generation,
            camera_ids=camera_ids,
            expected_revision=expected_revision,
        )

    async def homekit_recording_list(self, *, offset: int = 0, limit: int = 32) -> dict[str, Any]:
        return _camera_page(
            await self._object("homekit_recording_list", offset=offset, limit=limit), offset
        )

    async def homekit_recording_cameras(self) -> dict[str, Any]:
        return await self._camera_pages(self.homekit_recording_list)

    async def homekit_recording_warm(
        self,
        profile_id: str,
        account_generation: int,
        camera_id: str,
        expected_revision: int,
        enabled: bool,
    ) -> dict[str, Any]:
        return await self._object(
            "homekit_recording_warm",
            profile_id=profile_id,
            account_generation=account_generation,
            camera_id=camera_id,
            expected_revision=expected_revision,
            enabled=enabled,
        )

    async def configuration_get(self) -> dict[str, Any]:
        return _configuration_result(await self._object("configuration_get"))

    async def configuration_set(
        self, expected_revision: int, configuration: dict[str, Any]
    ) -> dict[str, Any]:
        fields = ("mode", "plugin_host", "plugin_port", "rtsp_host", "rtsp_port")
        return _configuration_result(
            await self._object(
                "configuration_set",
                expected_revision=expected_revision,
                configuration={name: configuration[name] for name in fields},
            )
        )

    async def homekit_pairing_list(self) -> dict[str, Any]:
        async def page(*, offset, limit):
            result = await self._object("homekit_pairing_list", offset=offset, limit=limit)
            _camera_page(result | {"revision": result.get("registry_revision")}, offset)
            return result

        return await self._camera_pages(page, revision_key="registry_revision")

    async def homekit_pairing_reveal(
        self, page: dict[str, Any], camera: dict[str, Any]
    ) -> dict[str, Any]:
        params = _pairing_identity(page, camera)
        result = await self._object("homekit_pairing_reveal", **params)
        identity = params | {
            "registry_revision": params["expected_registry_revision"],
            "pairing_revision": params["expected_pairing_revision"],
        }
        if (
            any(
                result.get(name) != value
                for name, value in identity.items()
                if not name.startswith("expected_")
            )
            or not isinstance(result.get("pin"), str)
            or re.fullmatch(r"[0-9]{3}-[0-9]{2}-[0-9]{3}", result["pin"]) is None
            or not isinstance(result.get("setup_uri"), str)
            or re.fullmatch(r"X-HM://[0-9A-Z]{9,32}", result["setup_uri"]) is None
            or result.get("expires_in_ms") != 60000
        ):
            raise AdminClientError("bad_response", "Daemon returned invalid camera setup data")
        return result

    async def homekit_pairing_reset(
        self, page: dict[str, Any], camera: dict[str, Any], operation_id: str
    ) -> dict[str, Any]:
        result = await self._object(
            "homekit_pairing_reset", **_pairing_identity(page, camera), operation_id=operation_id
        )
        if result.get("operation_id") != operation_id or result.get("status") not in {
            "completed",
            "pending",
        }:
            raise AdminClientError("bad_response", "Daemon returned an invalid reset outcome")
        return result

    async def camera_controls(self) -> dict[str, Any]:
        result = await self._object("camera_control_list")
        if (
            not isinstance(result.get("cameras"), list)
            or len(result["cameras"]) > 256
            or type(result.get("account_generation")) is not int
            or any(
                not isinstance(row, dict)
                or not isinstance(row.get("camera_id"), str)
                or type(row.get("device_generation")) is not int
                for row in result["cameras"]
            )
        ):
            raise AdminClientError("bad_response", "Daemon returned an invalid control view")
        return result

    async def camera_control(
        self,
        profile_id: str,
        account_generation: int,
        camera_id: str,
        device_generation: int,
        operation_id: str,
        control: str,
        enabled: bool,
    ) -> dict[str, Any]:
        result = await self._object(
            "camera_control",
            profile_id=profile_id,
            account_generation=account_generation,
            camera_id=camera_id,
            device_generation=device_generation,
            operation_id=operation_id,
            control=control,
            enabled=enabled,
        )
        if result.get("status") not in {"accepted", "rejected", "unknown"}:
            raise AdminClientError("bad_response", "Daemon returned an invalid control outcome")
        return result

    async def doctor(self, *, probe: bool = False) -> dict[str, Any]:
        return await self._object("doctor", probe=probe)

    async def support_report(self) -> dict[str, Any]:
        return await self._object("support_report")


def _camera_page(value: dict[str, Any], offset: int) -> dict[str, Any]:
    if (
        value.get("profile_id") is not None
        and not isinstance(value.get("profile_id"), str)
        or type(value.get("account_generation")) is not int
        or type(value.get("revision")) is not int
        or value["revision"] < 0
        or type(value.get("total")) is not int
        or not 0 <= value["total"] <= 256
        or value.get("offset") != offset
        or not isinstance(value.get("cameras"), list)
        or len(value["cameras"]) > 32
        or any(
            not isinstance(row, dict)
            or not isinstance(row.get("camera_id"), str)
            or not 1 <= len(row["camera_id"]) <= 128
            for row in value["cameras"]
        )
    ):
        raise AdminClientError("bad_response", "Daemon returned an invalid camera page")
    return value


def _configuration_result(result):
    from halo_server.configuration import ConfigurationError, ServerConfiguration

    try:
        for key in ("saved", "applied"):
            if not isinstance(result.get(key), dict):
                raise TypeError
            ServerConfiguration(**result[key])
        if type(result.get("restart_required")) is not bool:
            raise TypeError
    except (ConfigurationError, TypeError) as error:
        raise AdminClientError(
            "bad_response", "Daemon returned invalid operating settings"
        ) from error
    return result


def _pairing_identity(page, camera):
    try:
        return {
            "profile_id": page["profile_id"],
            "account_generation": page["account_generation"],
            "camera_id": camera["camera_id"],
            "accessory_id": camera["accessory_id"],
            "expected_registry_revision": page["registry_revision"],
            "expected_pairing_revision": camera["pairing_revision"],
            "boot_id": page["boot_id"],
        }
    except KeyError as error:
        raise AdminClientError(
            "bad_response", "Daemon returned invalid pairing identity"
        ) from error
