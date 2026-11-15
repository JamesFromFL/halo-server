"""Ring inventory, per-location hub monitoring, and guarded normal Alarm modes.

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

from halo_server.devices.alarm_commands import AlarmCommands
from halo_server.devices.chime_commands import ChimeCommands, chime_values
from halo_server.devices.group_commands import GroupCommands
from halo_server.devices.groups import (
    MAX_LOCATIONS as MAX_GROUP_LOCATIONS,
)
from halo_server.devices.groups import (
    GroupSchemaError,
    api_id,
    require_complete_inventory,
)
from halo_server.devices.groups import (
    identifier as group_identifier,
)
from halo_server.devices.keypad_commands import KeypadCommands
from halo_server.devices.light_commands import BEAMS_TYPES, LightCommands
from halo_server.devices.models import DeviceIdentity, DeviceObservation, DeviceRecord
from halo_server.homekit.text import literal_text
from halo_server.ring.device_rest import RingDeviceRestGateway
from halo_server.ring.hub.models import (
    AlarmConnectionStatus,
    AlarmDeviceKind,
    AlarmInventoryStatus,
    AlarmPhase,
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
from halo_server.ring.hub.store import AlarmStateStore, panel_inventory_baseline
from halo_server.ring.hub.transport import AlarmTransportError, RingAlarmTransport
from halo_server.ring.lighting import LightingReservation

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


_KEYPAD_MOTION_SECONDS = 20.0


@dataclass
class _KeypadMotion:
    runtime: object
    epoch: int
    deadline: float
    observed_at: float
    active: bool = True
    source: str = "hub-keypad-motion-impulse"
    task: asyncio.Task | None = None


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
        command_timeout: float = 10.0,
        lighting_reservation: LightingReservation | None = None,
    ):
        for value in (refresh_interval, health_interval, command_timeout):
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
        self._auth = auth
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
        self._alarm_commands = AlarmCommands(self, timeout=command_timeout)
        self._beams_owners: dict[tuple[str, str], bool] = {}
        self._lighting_reservation = lighting_reservation or LightingReservation()
        self._light_commands = LightCommands(self, timeout=command_timeout)
        self._keypad_commands = KeypadCommands(self, timeout=command_timeout)
        self._keypad_motion: dict[tuple[str, str, str], _KeypadMotion] = {}
        self._keypad_motion_tasks: set[asyncio.Task] = set()
        self._device_gateway = None
        self._chime_commands = ChimeCommands(self, timeout=command_timeout)
        self._discovery_retry_at = 0.0
        self._discovery_sequence = 0
        self._command_timeout = command_timeout
        self._groups: GroupCommands | None = None
        self._group_task: asyncio.Task | None = None
        self._group_wake = asyncio.Event()
        self._group_locations: dict[str, str] = {}
        self._group_ownership: dict[tuple[str, str, str], bool] = {}
        self._group_context_valid = False
        self._group_discovery_at = 0.0
        self._group_report_at = 0.0

    def _get_device_gateway(self):
        if self._device_gateway is None:
            core = getattr(self._gateway, "shared_transport", None)
            if core is None:
                return None
            self._device_gateway = RingDeviceRestGateway(
                self._auth, transport=core, admission=self._active
            )
        return self._device_gateway

    def _update_group_context(self, devices=None, locations=None):
        # Authority must change synchronously, before any transport cleanup or
        # callback yields. A crossed group GET cannot restore an earlier owner.
        try:
            names, ownership = _group_context(devices, locations)
            valid = self._rest_valid
        except (ValueError, TypeError):
            names, ownership, valid = {}, {}, False
        if names.keys() != self._group_locations.keys() or valid and not self._group_context_valid:
            self._group_discovery_at = 0.0
        self._group_locations, self._group_ownership = names, ownership
        self._group_context_valid = valid
        if self._groups is None:
            gateway = self._get_device_gateway()
            if gateway is not None and all(
                callable(getattr(gateway, method, None))
                for method in ("read_light_groups", "read_light_group_report", "set_light_group")
            ):
                self._groups = GroupCommands(self, gateway, timeout=self._command_timeout)
        if self._groups is not None:
            self._groups.update_ownership(ownership, locations=names, valid=valid)
        self._group_wake.set()

    async def _refresh_groups(self):
        if not self._active() or self._groups is None or not self._group_context_valid:
            return
        now = self._monotonic()
        if now >= self._group_discovery_at:
            self._group_discovery_at = now + 300
            # Copies fence this poll's location and owner context from a later
            # REST receipt. The manager additionally checks its context token.
            await self._groups.refresh(
                dict(self._group_locations), dict(self._group_ownership), report_limit=4
            )
            self._group_report_at = self._monotonic() + 30
        elif now >= self._group_report_at:
            self._group_report_at = now + 30
            await self._groups.refresh_reports(limit=4)
        await self._publish()

    async def _group_loop(self):
        while self._active():
            self._group_wake.clear()
            try:
                await self._refresh_groups()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Keep the independent monitor alive after an invalid source;
                # no failure may preserve command authority from an old poll.
                if self._groups is not None:
                    self._groups.update_ownership({}, valid=False)
                await self._publish()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._group_wake.wait(), timeout=30)

    async def set_chime(
        self, device_id, action, value, *, expected_revision, expected_epoch, admission=None
    ):
        return await self._chime_commands.request(
            device_id,
            action,
            value,
            expected_revision=expected_revision,
            expected_epoch=expected_epoch,
            admission=admission,
        )

    async def start(self):
        if self._closed:
            raise RuntimeError("Provider is closed")
        if self._task is None:
            self._task = asyncio.create_task(self._discovery_loop(), name="ring-device-discovery")
            self._group_task = asyncio.create_task(self._group_loop(), name="ring-light-groups")

    def snapshot(self) -> tuple[DeviceRecord, ...]:
        return self._records

    def status(self) -> dict:
        return {
            "read_only": True,
            "discovery_complete": self._rest_valid
            and (self._groups is None or self._groups.complete),
            "locations": len(self._runtimes),
            "error": self._error,
        }

    async def execute(self, *_args, **_kwargs):
        raise ReadOnlyDeviceError()

    async def set_alarm_mode(
        self, device_id, target, *, expected_revision, expected_epoch, admission=None
    ):
        """Apply an explicitly admitted normal mode; all other controls stay disabled."""
        return await self._alarm_commands.request(
            device_id,
            target,
            expected_revision=expected_revision,
            expected_epoch=expected_epoch,
            admission=admission,
        )

    def _active(self):
        return not self._closed and self._admission() is True

    async def set_light(
        self,
        device_id,
        target,
        *,
        expected_revision,
        expected_epoch,
        duration_seconds,
        admission=None,
    ):
        """Dispatch only exact qualified physical-light or canonical group IDs."""
        commands = (
            self._groups
            if self._groups is not None
            and any(record.key == device_id for record in self._groups.records())
            else self._light_commands
        )
        return await commands.request(
            device_id,
            target,
            expected_revision=expected_revision,
            expected_epoch=expected_epoch,
            duration_seconds=duration_seconds,
            admission=admission,
        )

    async def set_keypad_brightness(
        self, device_id, target, *, expected_revision, expected_epoch, admission=None
    ):
        """Change only a qualified keypad LED brightness under owner admission."""
        return await self._keypad_commands.request(
            device_id,
            target,
            expected_revision=expected_revision,
            expected_epoch=expected_epoch,
            admission=admission,
        )

    async def _discovery_loop(self):
        while self._active():
            await self.refresh()
            interval = (
                min(self._refresh_interval, 20)
                if any(record.family == "chimes" for record in self._rest_records)
                else self._refresh_interval
            )
            delay = max(interval, self._discovery_retry_at - self._monotonic())
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=delay)

    async def refresh(self):
        async with self._refresh_lock:
            if not self._active():
                return
            if self._monotonic() < self._discovery_retry_at:
                return
            started = self._monotonic()
            self._discovery_sequence += 1
            sequence = self._discovery_sequence
            try:
                devices, locations = await asyncio.gather(
                    self._gateway.request_json(DEVICES_ENDPOINT),
                    self._gateway.request_json(LOCATIONS_ENDPOINT),
                    return_exceptions=True,
                )
                for result in (devices, locations):
                    if isinstance(result, BaseException):
                        raise result
                rows, seeds, beams_owners = self._discover(devices, locations)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not self._active():
                    return
                self._rest_valid = False
                self._chime_commands.invalidate()
                self._update_group_context()
                retry = getattr(exc, "retry_after", None)
                if type(retry) in (int, float) and math.isfinite(retry) and retry > 0:
                    self._discovery_retry_at = self._monotonic() + min(3600, retry)
                elif getattr(exc, "code", None) == "rate-limited":
                    self._discovery_retry_at = self._monotonic() + 60
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
            self._beams_owners = beams_owners
            self._update_group_context(devices, locations)
            self._discovery_retry_at = 0.0
            self._chime_commands.observe(devices, started=started, sequence=sequence)
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
                    self._alarm_commands.invalidate(location_id)
                    self._light_commands.invalidate(location_id)
                    self._keypad_commands.invalidate(location_id)
                    await self._stop_runtime(runtime)
                    self._observation_times = {
                        key: value
                        for key, value in self._observation_times.items()
                        if key[0] != location_id
                    }
            for seed in seeds:
                runtime = self._runtimes.get(seed.location_id)
                if runtime is None:
                    self._store.register_location(
                        seed.location_id, seed.name, write_authorization=seed.write_authorization
                    )
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
                        write_authorization=seed.write_authorization,
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
        alarm_ownership: dict[str, list[bool]] = {}
        beams_owners = {}
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
                values = chime_values(row) if family == "chimes" else {"connected": connected}
                connected = values["connected"]
                observations = tuple(
                    DeviceObservation(
                        name,
                        value,
                        now if value is not None else None,
                        status="current" if value is not None else "unknown",
                        source="rest",
                    )
                    for name, value in values.items()
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
                if family == "base_stations":
                    alarm_ownership.setdefault(location, []).append(row.get("owned") is True)
                if family == "beams_bridges":
                    # REST role belongs to this bridge, not every asset at its
                    # location. Ticket doorbotId establishes the exact match.
                    beams_owners[(location, device_id)] = row.get("role") == "owner"
        seeds = location_seeds_from_base_stations(hub_rows, raw_locations=location_rows)
        # Explicit owner role is documented authority for ordinary Alarm modes.
        # It does not prove subscription entitlement. Ring enforces entitlement
        # on each admitted request; shared/unknown roles remain read-only.
        return (
            tuple(records),
            tuple(
                replace(
                    seed,
                    write_authorization=(
                        AlarmWriteAuthorization.ALLOWED
                        if alarm_ownership.get(seed.location_id)
                        and all(alarm_ownership[seed.location_id])
                        else AlarmWriteAuthorization.UNKNOWN
                    ),
                )
                for seed in seeds
            ),
            beams_owners,
        )

    async def _supervise(self, runtime):
        backoff = 1.0
        while self._active() and self._runtimes.get(runtime.seed.location_id) is runtime:
            runtime.epoch = self._store.begin_epoch(runtime.seed.location_id)
            self._alarm_commands.invalidate(runtime.seed.location_id)
            self._light_commands.invalidate(runtime.seed.location_id)
            self._keypad_commands.invalidate(runtime.seed.location_id)
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
            self._alarm_commands.invalidate(runtime.seed.location_id)
            self._light_commands.invalidate(runtime.seed.location_id)
            self._keypad_commands.invalidate(runtime.seed.location_id)
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
            write_authorization=runtime.seed.write_authorization,
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
        lighting_revision = self._lighting_reservation.revision
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
        self._alarm_commands.observe(runtime, epoch, message)
        self._light_commands.observe(runtime, epoch, message, lighting_revision=lighting_revision)
        self._keypad_commands.observe(runtime, epoch, message)
        self._record_keypad_motion(runtime, epoch, message, previous)
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

    def _keypad_motion_current(self, key, event):
        location = self._store.snapshot.find_location(key[0])
        asset = location.find_asset(key[1]) if location else None
        device = asset.find_device(key[2]) if asset else None
        return bool(
            self._valid_runtime(event.runtime, event.epoch)
            and event.runtime.transport.connected
            and location is not None
            and location.epoch == event.epoch
            and asset is not None
            and asset.connection is AlarmConnectionStatus.ONLINE
            and not asset.stale
            and asset.inventory is AlarmInventoryStatus.COMPLETE
            and asset.inventory_monotonic is not None
            and 0 <= self._monotonic() - asset.inventory_monotonic <= 120
            and device is not None
            and device.raw_type == "security-keypad"
            and not device.stale
            and device.connection is AlarmConnectionStatus.ONLINE
        )

    def _prune_keypad_motion(self):
        for key, event in tuple(self._keypad_motion.items()):
            if not self._keypad_motion_current(key, event):
                self._keypad_motion.pop(key)
                if event.task is not None and event.task is not asyncio.current_task():
                    event.task.cancel()

    def _record_keypad_motion(self, runtime, epoch, message, previous):
        self._prune_keypad_motion()
        if not isinstance(message, DeviceInfoDocUpdate):
            return
        previous_asset = previous.find_asset(message.asset_id) if previous else None
        for document in message.devices:
            previous_device = previous_asset.find_device(document.zid) if previous_asset else None
            if (
                not document.keypad_motion
                or previous_device is None
                or previous_device.raw_type != "security-keypad"
            ):
                continue
            key = (runtime.seed.location_id, message.asset_id, document.zid)
            event = _KeypadMotion(
                runtime, epoch, self._monotonic() + _KEYPAD_MOTION_SECONDS, self._clock()
            )
            if not self._keypad_motion_current(key, event):
                continue
            old = self._keypad_motion.get(key)
            if old is not None and old.task is not None:
                old.task.cancel()
            self._keypad_motion[key] = event
            event.task = asyncio.create_task(
                self._expire_keypad_motion(key, event), name="ring-keypad-proximity"
            )
            self._keypad_motion_tasks.add(event.task)
            event.task.add_done_callback(self._keypad_motion_tasks.discard)

    async def _expire_keypad_motion(self, key, event):
        while self._keypad_motion.get(key) is event and self._keypad_motion_current(key, event):
            remaining = event.deadline - self._monotonic()
            if remaining > 0:
                await asyncio.sleep(remaining)
                continue
            # Ring reports an impulse, not a clear. Label the timeout honestly
            # while retaining the proven capability for this connection epoch.
            event.active = False
            event.source = "hub-keypad-motion-expiry"
            await self._publish()
            return
        # Source freshness may expire while the timer sleeps. Publish the lost
        # evidence now, rather than leaving a last active value until another
        # device happens to report. Never retire a newer replacement event.
        if self._keypad_motion.get(key) is event:
            self._keypad_motion.pop(key)
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
            if isinstance(message, DeviceInfoDocList) and panel_inventory_baseline(document.data):
                # A complete panel document reports normal state by omitting the
                # optional alarm fields. A sparse mode/health patch cannot do so.
                receipts["alarm_state"] = now
                receipts["alarm_signal"] = now
            elif "transitionDelayEndTimestamp" in document.data:
                location = self._store.snapshot.find_location(location_id)
                asset = location.find_asset(message.asset_id) if location else None
                device = asset.find_device(document.zid) if asset else None
                if (
                    device is not None
                    and device.kind is AlarmDeviceKind.SECURITY_PANEL
                    and device.phase in {AlarmPhase.IDLE, AlarmPhase.EXIT_DELAY}
                ):
                    # A transition-only patch observes that phase, not a retained
                    # entry/alarm/unknown state and not the separate alarm signal.
                    receipts["alarm_state"] = now

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
        self._prune_keypad_motion()
        records = list(self._rest_records)
        complete = self._rest_valid
        group_records = self._groups.records() if self._groups is not None else ()
        if self._groups is not None:
            complete &= self._groups.complete
            records.extend(group_records)
        group_keys = {record.key for record in group_records}
        suppressed = set()
        for location in self._store.snapshot.locations:
            runtime = self._runtimes.get(location.location_id)
            complete &= location.inventory is AlarmInventoryStatus.COMPLETE
            for device in location.devices:
                if (
                    self._groups is not None
                    and device.raw_type == "group.light-group.beams"
                    and device.group_id is not None
                    and self._groups.has_group(location.location_id, device.group_id)
                ):
                    suppressed.add(
                        DeviceIdentity(
                            self.profile_id,
                            "ring-hub",
                            device.location_id,
                            device.asset_id,
                            device.zid,
                        ).key
                    )
                    continue
                try:
                    records.append(
                        self._device_record(
                            device, location.name, runtime.error if runtime else "unavailable"
                        )
                    )
                except ValueError:
                    complete = False
                    self._error = "invalid-device-state"
        # A healthy group list can prove absence while an unrelated hub is
        # offline. Emit an unavailable tombstone in that partial batch instead
        # of letting the catalog retain the removed group's last current value.
        for previous in self._records:
            if (
                previous.identity.provider == "ring-light-group"
                and previous.key not in group_keys
                or previous.key in suppressed
            ):
                self._retired_records[previous.key] = replace(
                    _stale(
                        previous, "group-alias" if previous.key in suppressed else "device-removed"
                    ),
                    source_health="unavailable",
                )
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
        alarms = self._alarm_commands.snapshots()
        lights = self._light_commands.snapshots()
        if self._groups is not None:
            lights += self._groups.snapshots()
        if self._error == "device-limit-exceeded":
            # No current write capability may escape a rejected inventory.
            lights = ()
        if self._callback is not None:
            result = self._callback(
                records=self._records,
                complete=bool(complete),
                revision=self._revision,
                observed_at=self._clock(),
                alarms=alarms,
                lights=lights,
                chimes=self._chime_commands.snapshots(),
                keypads=self._keypad_commands.snapshots()
                if self._error != "device-limit-exceeded"
                else (),
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
            "keypad_brightness": "keypad_brightness",
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
        keypad_motion = self._keypad_motion.get((device.location_id, device.asset_id, device.zid))
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
            source = "hub"
            if name == "motion_detected" and device.raw_type == "security-keypad":
                value = keypad_motion.active if keypad_motion else None
                observed = (
                    keypad_motion.observed_at
                    if keypad_motion and keypad_motion.active
                    else self._clock()
                    if keypad_motion
                    else None
                )
                # An ended impulse window is a current derived "no recent
                # detection" value only while its qualified connection survives.
                # This is not a fresh Ring clear; preserve both its distinct
                # provenance and the genuine last event time in the event state.
                source = keypad_motion.source if keypad_motion else "hub"
            status = (
                "unknown"
                if value is None
                else "stale"
                if device.stale
                or observed is None
                or (
                    name == "light_on"
                    and device.raw_type in BEAMS_TYPES
                    and not self._light_commands.evidence_current(device)
                )
                else "current"
            )
            observations.append(
                DeviceObservation(
                    name,
                    value,
                    observed if value is not None else None,
                    status=status,
                    source=source,
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
        capability_values = {str(capability) for capability in device.capabilities}
        evidence = ["ring-hub-device-schema"] if capability_values else []
        if keypad_motion is not None:
            capability_values.add("motion")
            evidence.append("ring-keypad-motion-impulse")
        capabilities = tuple(sorted(capability_values))
        return DeviceRecord(
            identity=DeviceIdentity(
                self.profile_id, "ring-hub", device.location_id, device.asset_id, device.zid
            ),
            account_generation=self.account_generation,
            provider_generation=self.provider_generation,
            name=device.name or str(device.kind),
            family="hub",
            kind="beams-light" if device.raw_type in BEAMS_TYPES else str(device.kind),
            model=device.raw_type,
            location_name=location_name,
            capabilities=capabilities,
            capability_evidence=tuple(evidence),
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
            for location_id in self._runtimes:
                self._alarm_commands.invalidate(location_id)
                self._light_commands.invalidate(location_id)
                self._keypad_commands.invalidate(location_id)
            self._close_task = asyncio.create_task(self._close_impl(), name="ring-device-close")
        await asyncio.shield(self._close_task)

    async def _close_impl(self):
        self._stop.set()
        motion_tasks = tuple(self._keypad_motion_tasks)
        self._keypad_motion.clear()
        for task in motion_tasks:
            task.cancel()
        await asyncio.gather(*motion_tasks, return_exceptions=True)
        if self._task is not None:
            self._task.cancel()
        if self._group_task is not None:
            self._group_task.cancel()
        if self._groups is not None:
            await self._groups.close()
        if self._group_task is not None:
            await asyncio.gather(self._group_task, return_exceptions=True)
        await self._chime_commands.close()
        if self._device_gateway is not None:
            await self._device_gateway.close()
        await asyncio.gather(*(self._stop_runtime(runtime) for runtime in self._runtimes.values()))
        self._runtimes.clear()
        await self._gateway.close()
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)


def _group_context(devices, locations):
    """Exact REST member ownership; never infer it from a location or hub."""
    if not isinstance(devices, Mapping) or not isinstance(locations, Mapping):
        raise ValueError("invalid-group-context")
    rows = locations.get("user_locations")
    if type(rows) is not list or len(rows) > MAX_GROUP_LOCATIONS:
        raise ValueError("invalid-group-context")
    require_complete_inventory(locations, len(rows))
    names = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("invalid-group-context")
        location = group_identifier(row.get("location_id", row.get("locationId")))
        if location in names:
            raise ValueError("invalid-group-context")
        names[location] = _text(row.get("name"), 256)
    candidates, seen, ambiguous = {}, set(), set()
    count = 0
    for family, rows in devices.items():
        if not isinstance(family, str) or not isinstance(rows, (list, dict)):
            raise ValueError("invalid-group-context")
        for row in rows.values() if isinstance(rows, dict) else rows:
            count += 1
            if count > _MAX_RECORDS or not isinstance(row, Mapping):
                raise ValueError("invalid-group-context")
            # Validate cameras too: the noncamera inventory intentionally skips
            # their projection, but they can still be members of a REST group.
            location = group_identifier(row.get("location_id", row.get("locationId")))
            if location not in names:
                raise ValueError("invalid-group-context")
            try:
                raw_id = row.get("id")
                device_id = api_id(raw_id, report=type(raw_id) is str)
            except GroupSchemaError:
                # Reject ambiguous/noncanonical source identity, including
                # camera rows omitted by the noncamera record projection.
                raise ValueError("invalid-group-member-context") from None
            key = (location, device_id)
            if key in seen:
                ambiguous.add(key)
            seen.add(key)
            kind = row.get("kind")
            if family in _HUB_FAMILIES or not literal_text(kind, 128, empty=False):
                continue
            # Only the exact physical row's authority counts. A contradictory
            # explicit ownership field cannot be overridden by its role label.
            owned = row.get("owned") is True or ("owned" not in row and row.get("role") == "owner")
            candidates[(location, device_id, kind)] = owned
    return names, {key: value for key, value in candidates.items() if key[:2] not in ambiguous}


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
    "keypad_brightness": {"brightness"},
    "ac_power": {"acStatus"},
    "siren_active": {"siren"},
    "bypassed": {"bypassed", "bypassStatus"},
    "faulted": {"faulted"},
    "glass_break_detected": {"faulted"},
    "tilt_detected": {"faulted"},
    "valve_open": {"valveState"},
    "connected": {"commStatus"},
}
