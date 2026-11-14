"""Read-only Ring inventory and per-location hub monitoring.

The authenticated SDK session remains owned by RingClient. This provider owns
only bounded GET requests and ticket sockets; it never opens a second login.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import math
import random
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace

from halo_server.devices.models import DeviceIdentity, DeviceObservation, DeviceRecord
from halo_server.ring.hub.models import (
    AlarmConnectionStatus,
    AlarmDeviceKind,
    AlarmInventoryStatus,
    AlarmWriteAuthorization,
    TriState,
)
from halo_server.ring.hub.protocol import (
    AssetSessionInfo,
    DeviceInfoDocList,
    DeviceInfoDocUpdate,
    SessionInfo,
)
from halo_server.ring.hub.provider import AlarmLocationSeed, location_seeds_from_base_stations
from halo_server.ring.hub.rest import DEVICES_ENDPOINT, LOCATIONS_ENDPOINT, RingAlarmRestGateway
from halo_server.ring.hub.store import AlarmStateStore
from halo_server.ring.hub.transport import AlarmTransportError, RingAlarmTransport

_CAMERA_FAMILIES = frozenset({"doorbots", "authorized_doorbots", "stickup_cams"})
_HUB_FAMILIES = frozenset({"base_stations", "beams_bridges"})
_MAX_RECORDS = 1024
_MAX_LOCATIONS = 128
_ALLOWED_ERRORS = frozenset(
    {
        "authentication-required",
        "permission-denied",
        "rate-limited",
        "invalid-response",
        "invalid-discovery",
        "timeout",
        "network-unavailable",
        "no-supported-assets",
        "unsupported-auth-contract",
        "authentication-coordination-unavailable",
        "device-limit-exceeded",
        "asset-limit-exceeded",
    }
)


class ReadOnlyDeviceError(RuntimeError):
    """No device control is available in the monitoring wave."""

    def __init__(self):
        super().__init__("device-controls-disabled")
        self.code = "device-controls-disabled"


@dataclass
class _LocationRuntime:
    seed: AlarmLocationSeed
    transport: object
    epoch: int = 0
    task: asyncio.Task | None = None
    maintenance: asyncio.Task | None = None
    error: str | None = None
    assets: frozenset[str] = frozenset()


class NoncameraProvider:
    """Run on RingClient's event loop; callbacks contain normalized data only."""

    def __init__(
        self,
        auth,
        *,
        profile_id: str,
        account_generation: int,
        provider_generation: int = 1,
        on_snapshot: Callable | None = None,
        admission: Callable[[], bool] = lambda: True,
        refresh_interval: float = 300.0,
        gateway=None,
        transport_factory=RingAlarmTransport,
        clock=time.time,
        monotonic_clock=time.monotonic,
        health_interval: float = 60.0,
    ):
        for value in (refresh_interval, health_interval):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError("Provider intervals must be positive finite numbers")
        # Validate scope even when the account has no non-camera devices.
        DeviceIdentity(profile_id, "ring-rest", "scope", "", "scope")
        if any(
            type(value) is not int or not 1 <= value < 2**53
            for value in (account_generation, provider_generation)
        ):
            raise ValueError("Provider generations must be positive safe integers")
        self.profile_id = profile_id
        self.account_generation = account_generation
        self.provider_generation = provider_generation
        self._gateway = (
            gateway if gateway is not None else RingAlarmRestGateway(auth, admission=self._active)
        )
        self._transport_factory = (
            (lambda gateway, seed: RingAlarmTransport(gateway, seed, admission=self._active))
            if transport_factory is RingAlarmTransport
            else transport_factory
        )
        self._admission = admission
        self._callback = on_snapshot
        self._clock = clock
        self._monotonic = monotonic_clock
        self._refresh_interval = refresh_interval
        self._health_interval = health_interval
        self._store = AlarmStateStore(
            provider_generation, clock=clock, monotonic_clock=monotonic_clock
        )
        self._runtimes: dict[str, _LocationRuntime] = {}
        self._rest_records: tuple[DeviceRecord, ...] = ()
        self._records: tuple[DeviceRecord, ...] = ()
        self._retired_records: dict[str, DeviceRecord] = {}
        self._observation_times: dict[tuple[str, str, str], dict[str, float]] = {}
        self._revision = 0
        self._rest_valid = False
        self._closed = False
        self._stop = asyncio.Event()
        self._refresh_lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._close_task: asyncio.Task | None = None
        self._error: str | None = None

    async def start(self):
        if self._closed:
            raise RuntimeError("Provider is closed")
        if self._task is None:
            self._task = asyncio.create_task(self._discovery_loop(), name="ring-device-discovery")

    def snapshot(self) -> tuple[DeviceRecord, ...]:
        return self._records

    def status(self) -> dict:
        return {
            "read_only": True,
            "discovery_complete": self._rest_valid,
            "locations": len(self._runtimes),
            "error": self._error,
        }

    async def execute(self, *_args, **_kwargs):
        raise ReadOnlyDeviceError()

    def _active(self):
        return not self._closed and self._admission() is True

    async def _discovery_loop(self):
        while self._active():
            await self.refresh()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self._refresh_interval)

    async def refresh(self):
        async with self._refresh_lock:
            if not self._active():
                return
            try:
                devices, locations = await asyncio.gather(
                    self._gateway.request_json(DEVICES_ENDPOINT),
                    self._gateway.request_json(LOCATIONS_ENDPOINT),
                    return_exceptions=True,
                )
                for result in (devices, locations):
                    if isinstance(result, BaseException):
                        raise result
                rows, seeds = self._discover(devices, locations)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not self._active():
                    return
                self._rest_valid = False
                self._error = _error_code(exc)
                self._rest_records = tuple(
                    _stale(record, self._error) for record in self._rest_records
                )
                await self._publish()
                return
            if not self._active():
                return
            self._error = None
            self._rest_valid = True
            self._rest_records = rows
            current = {seed.location_id: seed for seed in seeds}
            rest_keys = {record.key for record in rows}
            # An offline location makes the aggregate inventory partial. Still
            # publish explicit unavailability for removals this REST read proved;
            # otherwise the catalog would keep another location's old values
            # current until its age limit expired.
            for record in self._records:
                rest_removed = (
                    record.identity.provider == "ring-rest" and record.key not in rest_keys
                )
                hub_removed = (
                    record.identity.provider == "ring-hub"
                    and record.identity.location_id not in current
                )
                if rest_removed or hub_removed:
                    self._retired_records[record.key] = replace(
                        _stale(record, "source-removed" if hub_removed else "device-removed"),
                        source_health="unavailable",
                    )
            for location_id, runtime in tuple(self._runtimes.items()):
                if location_id not in current:
                    # Retire the projection before awaiting socket cleanup so
                    # another location's callback cannot revive this source.
                    self._runtimes.pop(location_id, None)
                    self._store.remove_location(location_id)
                    await self._stop_runtime(runtime)
                    self._observation_times = {
                        key: value
                        for key, value in self._observation_times.items()
                        if key[0] != location_id
                    }
            for seed in seeds:
                runtime = self._runtimes.get(seed.location_id)
                if runtime is None:
                    self._store.register_location(seed.location_id, seed.name)
                    runtime = _LocationRuntime(seed, self._transport_factory(self._gateway, seed))
                    self._runtimes[seed.location_id] = runtime
                    runtime.task = asyncio.create_task(
                        self._supervise(runtime), name="ring-device-location"
                    )
                    runtime.maintenance = asyncio.create_task(
                        self._maintain(runtime), name="ring-device-health"
                    )
                else:
                    runtime.seed = seed
                    location = self._store.snapshot.find_location(seed.location_id)
                    self._store.register_location(
                        seed.location_id,
                        seed.name,
                        ((asset.asset_id, asset.kind) for asset in location.assets)
                        if location
                        else (),
                    )
            await self._publish()

    def _discover(self, devices, locations):
        if not isinstance(devices, Mapping) or not devices or not isinstance(locations, Mapping):
            raise ValueError("invalid-discovery")
        location_rows = locations.get("user_locations")
        if not isinstance(location_rows, list) or len(location_rows) > _MAX_LOCATIONS:
            raise ValueError("invalid-discovery")
        names = {}
        for row in location_rows:
            if not isinstance(row, Mapping):
                raise ValueError("invalid-discovery")
            location = _identifier(row.get("location_id", row.get("locationId")))
            if location in names:
                raise ValueError("invalid-discovery")
            names[location] = _text(row.get("name"), 256)
        records = []
        hub_rows = []
        seen = set()
        count = 0
        now = self._clock()
        for family, rows in devices.items():
            if not isinstance(family, str) or len(family) > 128:
                raise ValueError("invalid-discovery")
            if not isinstance(rows, (list, dict)):
                raise ValueError("invalid-discovery")
            rows = rows.values() if isinstance(rows, dict) else rows
            for row in rows:
                count += 1
                if count > _MAX_RECORDS or not isinstance(row, Mapping):
                    raise ValueError("invalid-discovery")
                if family in _CAMERA_FAMILIES:
                    continue
                device_id = _identifier(row.get("id"))
                location = _identifier(row.get("location_id", row.get("locationId")))
                if location not in names:
                    raise ValueError("invalid-discovery")
                key = (family, device_id)
                if key in seen:
                    raise ValueError("invalid-discovery")
                seen.add(key)
                kind = {
                    "base_stations": "base-station",
                    "beams_bridges": "lighting-bridge",
                    "chimes": "chime",
                    "intercoms": "intercom",
                }.get(family, "unknown")
                raw_kind = _text(row.get("kind"), 128)
                if raw_kind.startswith("intercom"):
                    kind = "intercom"
                supported = kind != "unknown"
                capabilities = ("device-health",) if supported else ()
                connection = row.get("connection_status")
                if connection is None:
                    connection = row.get("online")
                connected = (
                    connection
                    if type(connection) is bool
                    else (
                        True
                        if connection == "online"
                        else False
                        if connection == "offline"
                        else None
                    )
                )
                observations = (
                    DeviceObservation(
                        "connected",
                        connected,
                        now if connected is not None else None,
                        status="current" if connected is not None else "unknown",
                        source="rest",
                    ),
                )
                records.append(
                    DeviceRecord(
                        identity=DeviceIdentity(
                            self.profile_id, "ring-rest", location, family, device_id
                        ),
                        account_generation=self.account_generation,
                        provider_generation=self.provider_generation,
                        name=_text(row.get("description") or row.get("name"), 256) or kind,
                        family=family,
                        kind=kind,
                        model=raw_kind,
                        location_name=names[location],
                        capabilities=capabilities,
                        capability_evidence=("ring-rest-schema",) if capabilities else (),
                        observations=observations,
                        source_health=(
                            "online"
                            if connected is True
                            else "offline"
                            if connected is False
                            else "unknown"
                        ),
                        supported=supported,
                        unsupported_reason=None if supported else "unrecognized-ring-device",
                    )
                )
                if family in _HUB_FAMILIES:
                    hub_rows.append(row)
        seeds = location_seeds_from_base_stations(hub_rows, raw_locations=location_rows)
        # Monitoring never treats ownership as authority to arm or actuate.
        return tuple(records), tuple(
            replace(seed, write_authorization=AlarmWriteAuthorization.UNKNOWN) for seed in seeds
        )

    async def _supervise(self, runtime):
        backoff = 1.0
        while self._active() and self._runtimes.get(runtime.seed.location_id) is runtime:
            runtime.epoch = self._store.begin_epoch(runtime.seed.location_id)
            epoch = runtime.epoch
            began = self._monotonic()
            await self._publish()
            try:
                await runtime.transport.connect_once(
                    epoch=epoch,
                    stop_event=self._stop,
                    on_open=lambda assets, epoch=epoch: self._opened(runtime, epoch, assets),
                    on_message=lambda message, epoch=epoch: self._message(runtime, epoch, message),
                )
                error = AlarmTransportError("websocket-closed")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                error = exc
            if not self._valid_runtime(runtime, epoch):
                return
            runtime.error = _error_code(error)
            self._store.mark_location_stale(runtime.seed.location_id, epoch)
            await self._publish()
            if self._monotonic() - began >= 60:
                backoff = 1.0
            retry = getattr(error, "retry_after", None)
            delay = (
                min(300.0, retry)
                if isinstance(retry, (int, float)) and math.isfinite(retry) and retry >= 0
                else random.uniform(backoff * 0.8, backoff * 1.2)
            )
            backoff = min(60.0, backoff * 2)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=delay)

    def _valid_runtime(self, runtime, epoch):
        return (
            self._active()
            and self._runtimes.get(runtime.seed.location_id) is runtime
            and runtime.epoch == epoch
        )

    async def _opened(self, runtime, epoch, assets):
        if not self._valid_runtime(runtime, epoch):
            return
        other_assets = sum(
            len(location.assets)
            for location in self._store.snapshot.locations
            if location.location_id != runtime.seed.location_id
        )
        if other_assets + len(assets) > _MAX_RECORDS:
            raise AlarmTransportError("asset-limit-exceeded")
        runtime.error = None
        runtime.assets = frozenset(asset.asset_id for asset in assets)
        self._store.register_location(
            runtime.seed.location_id,
            runtime.seed.name,
            ((asset.asset_id, asset.kind) for asset in assets),
        )
        sessions = tuple(
            AssetSessionInfo(
                asset.asset_id,
                AlarmConnectionStatus.ONLINE
                if asset.status == "online"
                else AlarmConnectionStatus.OFFLINE
                if asset.status == "offline"
                else AlarmConnectionStatus.UNKNOWN,
                asset.kind,
            )
            for asset in assets
        )
        self._store.apply_message(runtime.seed.location_id, SessionInfo(sessions), epoch)
        for asset in assets:
            self._store.record_ticket_power(
                runtime.seed.location_id, asset.asset_id, epoch, asset.on_battery
            )
        await self._publish()

    async def _message(self, runtime, epoch, message):
        if not self._valid_runtime(runtime, epoch):
            return
        if (
            isinstance(message, (DeviceInfoDocList, DeviceInfoDocUpdate))
            and message.asset_id not in runtime.assets
        ):
            return
        if isinstance(message, SessionInfo):
            message = replace(
                message,
                sessions=tuple(
                    item for item in message.sessions if item.asset_id in runtime.assets
                ),
            )
        previous = self._store.snapshot.find_location(runtime.seed.location_id)
        if isinstance(message, (DeviceInfoDocList, DeviceInfoDocUpdate)):
            existing = previous.find_asset(message.asset_id) if previous else None
            incoming = {document.zid for document in message.devices}
            if isinstance(message, DeviceInfoDocUpdate) and existing is not None:
                incoming.update(device.zid for device in existing.devices)
            other_devices = sum(
                len(asset.devices)
                for location in self._store.snapshot.locations
                for asset in location.assets
                if (location.location_id, asset.asset_id)
                != (runtime.seed.location_id, message.asset_id)
            )
            if len(self._rest_records) + other_devices + len(incoming) > _MAX_RECORDS:
                raise AlarmTransportError("device-limit-exceeded")
        self._store.apply_message(runtime.seed.location_id, message, epoch)
        if isinstance(message, (DeviceInfoDocList, DeviceInfoDocUpdate)):
            self._record_receipts(runtime.seed.location_id, message)
        if isinstance(message, SessionInfo):
            for session in message.sessions:
                asset = previous.find_asset(session.asset_id) if previous else None
                if session.connection is AlarmConnectionStatus.ONLINE and (
                    asset is None
                    or asset.stale
                    or asset.connection is not AlarmConnectionStatus.ONLINE
                ):
                    self._store.mark_asset_stale(
                        runtime.seed.location_id,
                        session.asset_id,
                        epoch,
                        connection=AlarmConnectionStatus.ONLINE,
                    )
                    await runtime.transport.request_inventory(session.asset_id, epoch=epoch)
        await self._publish()

    def _record_receipts(self, location_id, message):
        if isinstance(message, DeviceInfoDocList):
            self._observation_times = {
                key: value
                for key, value in self._observation_times.items()
                if key[:2] != (location_id, message.asset_id)
            }
        now = self._clock()
        for document in message.devices:
            key = (location_id, message.asset_id, document.zid)
            receipts = self._observation_times.setdefault(key, {})
            for name, source_fields in _OBSERVATION_FIELDS.items():
                if source_fields.intersection(document.data):
                    receipts[name] = now

    async def _maintain(self, runtime):
        while self._active():
            await asyncio.sleep(self._health_interval)
            if not self._valid_runtime(runtime, runtime.epoch):
                return
            location = self._store.snapshot.find_location(runtime.seed.location_id)
            if location is None:
                continue
            for asset in location.assets:
                if (
                    asset.inventory_monotonic is None
                    or self._monotonic() - asset.inventory_monotonic > self._health_interval * 2
                ):
                    self._store.mark_asset_stale(
                        location.location_id,
                        asset.asset_id,
                        runtime.epoch,
                        connection=asset.connection,
                    )
                if runtime.transport.connected:
                    with contextlib.suppress(AlarmTransportError):
                        await runtime.transport.request_inventory(
                            asset.asset_id, epoch=runtime.epoch
                        )
            await self._publish()

    async def _publish(self):
        if not self._active():
            return
        records = list(self._rest_records)
        complete = self._rest_valid
        for location in self._store.snapshot.locations:
            runtime = self._runtimes.get(location.location_id)
            complete &= location.inventory is AlarmInventoryStatus.COMPLETE
            for device in location.devices:
                try:
                    records.append(
                        self._device_record(
                            device, location.name, runtime.error if runtime else "unavailable"
                        )
                    )
                except ValueError:
                    complete = False
                    self._error = "invalid-device-state"
        live_keys = {record.key for record in records}
        self._retired_records = {
            key: record for key, record in self._retired_records.items() if key not in live_keys
        }
        if complete:
            # The catalog can now reconcile absence normally.
            self._retired_records.clear()
        else:
            records.extend(self._retired_records.values())
        if len(records) > _MAX_RECORDS:
            records = [_stale(record, "device-limit-exceeded") for record in self._records]
            complete = False
            self._error = "device-limit-exceeded"
        self._records = tuple(records)
        self._revision += 1
        if self._callback is not None:
            result = self._callback(
                records=self._records,
                complete=bool(complete),
                revision=self._revision,
                observed_at=self._clock(),
            )
            if inspect.isawaitable(result):
                await result

    def _device_record(self, device, location_name, error):
        fields = {
            "contact": "contact_open",
            "motion": "motion_detected",
            "battery_level": "battery_percent",
            "battery_low": "battery_low",
            "tamper": "tampered",
            "mode": "alarm_mode",
            "phase": "alarm_state",
            "signal": "alarm_signal",
            "switch_on": "light_on",
            "flood": "leak_detected",
            "freeze": "freeze_detected",
            "smoke": "smoke_detected",
            "carbon_monoxide": "co_detected",
            "lock_state": "lock_state",
            "temperature_celsius": "temperature_celsius",
            "ac_power": "ac_power",
            "siren_on": "siren_active",
            "bypassed": "bypassed",
            "faulted": "faulted",
            "glass_break": "glass_break_detected",
            "tilt": "tilt_detected",
            "valve_open": "valve_open",
        }
        observations = []
        receipts = self._observation_times.get(
            (device.location_id, device.asset_id, device.zid), {}
        )
        for attribute, name in fields.items():
            value = getattr(device, attribute)
            if isinstance(value, TriState):
                value = (
                    True
                    if value is TriState.ACTIVE
                    else False
                    if value is TriState.INACTIVE
                    else None
                )
            elif isinstance(value, str):
                value = None if value == "unknown" else str(value)
            observed = receipts.get(name)
            status = (
                "unknown"
                if value is None
                else "stale"
                if device.stale or observed is None
                else "current"
            )
            observations.append(
                DeviceObservation(
                    name,
                    value,
                    observed if value is not None else None,
                    status=status,
                    source="hub",
                )
            )
        connected = (
            True
            if device.connection is AlarmConnectionStatus.ONLINE
            else False
            if device.connection is AlarmConnectionStatus.OFFLINE
            else None
        )
        observations.append(
            DeviceObservation(
                "connected",
                connected,
                receipts.get("connected") if connected is not None else None,
                status="unknown"
                if connected is None
                else "stale"
                if device.stale or "connected" not in receipts
                else "current",
                source="hub",
            )
        )
        supported = device.kind is not AlarmDeviceKind.UNKNOWN
        capabilities = tuple(sorted(str(capability) for capability in device.capabilities))
        return DeviceRecord(
            identity=DeviceIdentity(
                self.profile_id, "ring-hub", device.location_id, device.asset_id, device.zid
            ),
            account_generation=self.account_generation,
            provider_generation=self.provider_generation,
            name=device.name or str(device.kind),
            family="hub",
            kind=str(device.kind),
            model=device.raw_type,
            location_name=location_name,
            capabilities=capabilities,
            capability_evidence=("ring-hub-device-schema",) if capabilities else (),
            observations=tuple(observations),
            source_health="stale"
            if device.stale
            else "online"
            if device.connection is AlarmConnectionStatus.ONLINE
            else "offline"
            if device.connection is AlarmConnectionStatus.OFFLINE
            else "unknown",
            unavailable_reason=error if device.stale else None,
            supported=supported,
            unsupported_reason=None if supported else "unrecognized-ring-device",
        )

    async def _stop_runtime(self, runtime):
        tasks = tuple(task for task in (runtime.task, runtime.maintenance) if task is not None)
        for task in tasks:
            task.cancel()
        await runtime.transport.close()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def close(self):
        if self._close_task is None:
            self._closed = True
            self._close_task = asyncio.create_task(self._close_impl(), name="ring-device-close")
        await asyncio.shield(self._close_task)

    async def _close_impl(self):
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
        await asyncio.gather(*(self._stop_runtime(runtime) for runtime in self._runtimes.values()))
        self._runtimes.clear()
        await self._gateway.close()
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)


def _identifier(value):
    if type(value) is int and value > 0:
        value = str(value)
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 128
        or any(ord(char) <= 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError("invalid-discovery")
    return value


def _text(value, limit):
    if not isinstance(value, str):
        return ""
    return "".join(char for char in value if ord(char) >= 32 and ord(char) != 127)[:limit]


def _error_code(error):
    code = getattr(error, "code", None)
    return (
        code
        if code in _ALLOWED_ERRORS
        else "invalid-discovery"
        if isinstance(error, ValueError)
        else "source-unavailable"
    )


def _stale(record, reason):
    return replace(
        record,
        source_health="stale",
        unavailable_reason=reason,
        observations=tuple(
            replace(observation, status="stale") if observation.value is not None else observation
            for observation in record.observations
        ),
    )


# Receipt freshness follows fields actually reported, not arbitrary device patches.
_OBSERVATION_FIELDS = {
    "contact_open": {"faulted"},
    "motion_detected": {"motionStatus", "faulted"},
    "battery_percent": {"batteryLevel"},
    "battery_low": {"batteryStatus"},
    "tampered": {"tamperStatus"},
    "alarm_mode": {"mode"},
    "alarm_state": {"alarmStatus", "alarmInfo"},
    "alarm_signal": {"alarmStatus", "alarmInfo", "smoke", "co", "components"},
    "light_on": {"on"},
    "leak_detected": {"flood"},
    "freeze_detected": {"freeze"},
    "smoke_detected": {"smoke", "components", "alarmStatus"},
    "co_detected": {"co", "components", "alarmStatus"},
    "lock_state": {"locked"},
    "temperature_celsius": {"celsius"},
    "ac_power": {"acStatus"},
    "siren_active": {"siren"},
    "bypassed": {"bypassed", "bypassStatus"},
    "faulted": {"faulted"},
    "glass_break_detected": {"faulted"},
    "tilt_detected": {"faulted"},
    "valve_open": {"valveState"},
    "connected": {"commStatus"},
}
