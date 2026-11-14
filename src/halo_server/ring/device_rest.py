"""Narrow non-camera REST controls sharing the monitor's authenticated core.

Ring SDK/GTK-compatible bodies are intentionally minimal. These methods never
infer model support, ownership, subscription entitlement, or physical success.
"""

from __future__ import annotations

import asyncio

from halo_server.ring.control_routes import device_url, group_url, settings_url
from halo_server.ring.json_transport import RingJsonError, _write_admission

INVENTORY_ENDPOINT = "https://api.ring.com/clients_api/ring_devices"
LOCATIONS_ENDPOINT = "https://api.ring.com/devices/v1/locations"


class RingDeviceRestGateway:
    """Own feature requests, never the injected transport or underlying Auth."""

    def __init__(self, auth, *, transport, admission=None):
        if transport._auth is not auth:
            raise ValueError("Shared transport belongs to another Auth")
        if admission is not None and not callable(admission):
            raise ValueError("Admission must be a synchronous callback")
        self._transport = transport
        self._admission = admission
        self._closed = False
        self._requests = set()

    def _check_open(self):
        if self._closed:
            raise RingJsonError("closed")
        if self._admission is not None:
            _write_admission(self._admission)
        return True

    async def read_inventory(self, *, timeout=None):
        return await self._read(INVENTORY_ENDPOINT, timeout=timeout)

    async def read_locations(self, *, timeout=None):
        return await self._read(LOCATIONS_ENDPOINT, timeout=timeout)

    async def read_chime_health(self, api_id, *, timeout=None):
        return await self._read(_url(device_url, api_id) + "health", timeout=timeout)

    async def read_chime_settings(self, api_id, *, timeout=None):
        return await self._read(_url(settings_url, api_id), timeout=timeout)

    async def read_light_groups(self, location_id, *, timeout=None):
        return await self._read(_url(group_url, location_id), timeout=timeout)

    async def read_light_group_report(self, location_id, group_id, *, timeout=None):
        if group_id is None:
            raise RingJsonError("invalid-request")
        return await self._read(_url(group_url, location_id, group_id), timeout=timeout)

    async def set_chime_volume(self, api_id, volume, *, admission, on_admitted=None, timeout=None):
        return await self._write(
            _url(device_url, api_id),
            "PUT",
            {"chime": {"settings": {"volume": volume}}},
            admission=admission,
            on_admitted=on_admitted,
            timeout=timeout,
        )

    async def test_chime(self, api_id, kind, *, admission, on_admitted=None, timeout=None):
        return await self._write(
            _url(device_url, api_id) + "play_sound",
            "POST",
            {"kind": kind},
            admission=admission,
            on_admitted=on_admitted,
            timeout=timeout,
        )

    async def snooze_chime(self, api_id, minutes, *, admission, on_admitted=None, timeout=None):
        return await self._write(
            _url(device_url, api_id) + "do_not_disturb",
            "POST",
            None if minutes is None else {"time": minutes},
            admission=admission,
            on_admitted=on_admitted,
            timeout=timeout,
        )

    async def set_chime_nightlight(
        self, api_id, enabled, *, admission, on_admitted=None, timeout=None
    ):
        # This configures the ambient-light sensor, not instantaneous lamp power.
        return await self._write(
            _url(settings_url, api_id),
            "PATCH",
            {"night_light_settings": {"light_sensor_enabled": enabled}},
            admission=admission,
            on_admitted=on_admitted,
            timeout=timeout,
        )

    async def set_light_group(
        self,
        location_id,
        group_id,
        on,
        *,
        duration_seconds=None,
        admission,
        on_admitted=None,
        timeout=None,
    ):
        if group_id is None:
            raise RingJsonError("invalid-request")
        settings = {"enabled": on}
        if duration_seconds is not None:
            settings["duration_seconds"] = duration_seconds
        return await self._write(
            _url(group_url, location_id, group_id),
            "POST",
            {"lights_on": settings},
            admission=admission,
            on_admitted=on_admitted,
            timeout=timeout,
        )

    async def _read(self, url, *, timeout):
        self._check_open()
        current = asyncio.current_task()
        self._requests.add(current)
        try:
            return await self._transport.request_json(
                url, timeout=timeout, recovery=False, admission=self._check_open
            )
        finally:
            self._requests.discard(current)

    async def _write(self, url, method, payload, *, admission, on_admitted, timeout):
        if not callable(admission):
            raise RingJsonError("invalid-admission")
        current = asyncio.current_task()
        self._requests.add(current)

        def validate():
            self._check_open()
            _write_admission(admission)
            return True

        try:
            return await self._transport.request_write(
                url,
                method=method,
                json=payload,
                timeout=timeout,
                admission=validate,
                on_admitted=on_admitted,
            )
        finally:
            self._requests.discard(current)

    async def close(self):
        self._closed = True
        tasks = self._requests - {asyncio.current_task()}
        for task in tasks:
            if not task.cancelling():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


def _url(builder, *args):
    try:
        return builder(*args)
    except (ValueError, UnicodeError):
        raise RingJsonError("invalid-request") from None
