"""Guarded, observation-confirmed normal Alarm commands on the monitor socket.

No subscription inference, bypass, panic, siren, or optimistic state belongs in
this boundary. A caller must opt in to each request and remain admitted until
the write starts. Ring remains authoritative about whether it accepts a write.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass

from halo_server.devices.models import DeviceIdentity, valid_generation
from halo_server.ring.hub.models import (
    AlarmCapability,
    AlarmConnectionStatus,
    AlarmDeviceKind,
    AlarmInventoryStatus,
    AlarmMode,
    AlarmPhase,
    AlarmWriteAuthorization,
    TriState,
)
from halo_server.ring.hub.protocol import DeviceInfoDocList, DeviceInfoDocUpdate
from halo_server.ring.hub.provider import AlarmCommandSupersededError

_MODES = (AlarmMode.DISARMED, AlarmMode.HOME, AlarmMode.AWAY)
_WIRE_MODES = {AlarmMode.DISARMED: "none", AlarmMode.HOME: "some", AlarmMode.AWAY: "all"}
# Ring inventories include credential-management records alongside physical
# sensors. Homebridge also excludes these exact types from accessory handling.
# Only their diagnostic-only shape may be ignored by the sensor safety check.
_ADMINISTRATIVE_TYPES = frozenset({"access-code", "access-code.vault"})
_ADMINISTRATIVE_CAPABILITIES = frozenset(
    {AlarmCapability.BATTERY, AlarmCapability.AC_POWER, AlarmCapability.TAMPER}
)
_BYPASS_KINDS = frozenset(
    {
        AlarmDeviceKind.CONTACT_SENSOR,
        AlarmDeviceKind.MOTION_SENSOR,
        AlarmDeviceKind.GLASS_BREAK_SENSOR,
        AlarmDeviceKind.TILT_SENSOR,
        AlarmDeviceKind.RETROFIT_ZONE,
    }
)


@dataclass
class _Pending:
    runtime: object
    epoch: int
    revision: int
    asset_id: str
    panel_id: str
    target: AlarmMode
    future: asyncio.Future
    admitted: bool = False
    receipt: int = 0


class AlarmCommands:
    """Owned by one provider event loop; no transport, account, or task of its own."""

    def __init__(self, provider, *, timeout=10.0):
        self.provider = provider
        self.timeout = timeout
        self._pending: dict[str, _Pending] = {}
        self._uncertain: dict[str, int] = {}
        self._tokens: dict[str, tuple[object, int]] = {}
        self._next_token = 0
        self._receipt = 0
        self._mode_receipts: dict[tuple[str, str, str], float] = {}
        self._readback_counter = 0
        self._readbacks: dict[tuple[str, str, str], tuple[int, int]] = {}

    def _identity(self, panel):
        return DeviceIdentity(
            self.provider.profile_id, "ring-hub", panel.location_id, panel.asset_id, panel.zid
        )

    def _revision(self, location, panel):
        identity = self._identity(panel).key
        # Tokens never repeat during this provider lifetime, even if a panel is
        # removed and reappears in the same socket epoch with revision one.
        fingerprint = (
            location.epoch,
            panel,
            location.write_authorization,
            location.can_set_mode,
            location.command_unavailable_reason,
            tuple((a.asset_id, a.connection, a.stale, a.inventory) for a in location.assets),
        )
        previous = self._tokens.get(identity)
        if previous is None or previous[0] != fingerprint:
            self._next_token += 1
            self._tokens[identity] = (fingerprint, self._next_token)
        return self._tokens[identity][1]

    def _reason(self, location, panel, target=None):
        provider = self.provider
        runtime = provider._runtimes.get(location.location_id)
        if not provider._active() or not provider._rest_valid:
            return "source-unavailable"
        if runtime is None or not provider._valid_runtime(runtime, location.epoch):
            return "connection-superseded"
        if not runtime.transport.connected:
            return "panel-offline"
        if location.security_panel is None or location.security_panel != panel:
            return "multiple-panels"
        if panel.connection is not AlarmConnectionStatus.ONLINE:
            return "panel-offline"
        if not location.can_set_mode:
            return location.command_unavailable_reason or "control-unavailable"
        asset = location.find_asset(panel.asset_id)
        if asset is None or not asset.kind.startswith("base_station"):
            return "panel-asset-unsupported"
        now = provider._monotonic()
        mode_at = self._mode_receipts.get((location.location_id, panel.asset_id, panel.zid))
        if (
            panel.stale
            or asset is None
            or asset.inventory_monotonic is None
            or not 0 <= now - asset.inventory_monotonic <= 120
            or mode_at is None
            or not 0 <= now - mode_at <= 120
        ):
            return "panel-stale"
        if location.location_id in self._uncertain:
            return "outcome-unknown"
        if target is not None and target is not AlarmMode.DISARMED:
            if panel.phase in (AlarmPhase.ENTRY_DELAY, AlarmPhase.EXIT_DELAY):
                return "transition-active"
            if panel.faulted_device_ids is None:
                return self._sensor_fallback(location, asset)
            if panel.faulted_device_ids:
                return "sensors-require-bypass"
        return None

    def _sensor_fallback(self, location, asset):
        """Missing optional fault list is safe only with complete sensor evidence."""
        sensors = []
        for device in asset.devices:
            if device.raw_type in _ADMINISTRATIVE_TYPES:
                if (
                    device.kind is AlarmDeviceKind.UNKNOWN
                    and device.capabilities <= _ADMINISTRATIVE_CAPABILITIES
                ):
                    continue
                return "faulted-device-list-unknown"
            if device.kind is AlarmDeviceKind.UNKNOWN:
                return "faulted-device-list-unknown"
            if AlarmCapability.BYPASS not in device.capabilities:
                continue
            if device.kind not in _BYPASS_KINDS:
                return "faulted-device-list-unknown"
            sensors.append(device)
        if not sensors:
            return "faulted-device-list-unknown"
        now = self.provider._clock()
        for sensor in sensors:
            observed = self.provider._observation_times.get(
                (location.location_id, asset.asset_id, sensor.zid), {}
            ).get("faulted")
            if (
                sensor.stale
                or sensor.connection is not AlarmConnectionStatus.ONLINE
                or observed is None
                or not 0 <= now - observed <= 120
                or sensor.faulted is TriState.UNKNOWN
            ):
                return "faulted-device-list-unknown"
            if sensor.faulted is TriState.ACTIVE:
                return "sensors-require-bypass"
        return None

    def snapshots(self):
        output = []
        present = set()
        panel_keys = set()
        locations = set()
        for location in self.provider._store.snapshot.locations:
            locations.add(location.location_id)
            for panel in location.devices:
                if panel.kind is not AlarmDeviceKind.SECURITY_PANEL:
                    continue
                identity = self._identity(panel)
                present.add(identity.key)
                panel_key = (location.location_id, panel.asset_id, panel.zid)
                panel_keys.add(panel_key)
                readback_epoch, readback_revision = self._readbacks.get(panel_key, (0, 0))
                pending = self._pending.get(location.location_id)
                reason = self._reason(location, panel)
                if pending:
                    reason = "command-pending"
                target_reasons = {
                    str(mode): "command-pending" if pending else self._reason(location, panel, mode)
                    for mode in _MODES
                }
                output.append(
                    {
                        "device_id": identity.key,
                        "location_id": identity.location_key,
                        "epoch": location.epoch,
                        "revision": self._revision(location, panel),
                        "readback_revision": readback_revision
                        if readback_epoch == location.epoch
                        else 0,
                        "mode": None if panel.mode is AlarmMode.UNKNOWN else str(panel.mode),
                        "phase": None if panel.phase is AlarmPhase.UNKNOWN else str(panel.phase),
                        "signal": None if str(panel.signal) == "unknown" else str(panel.signal),
                        "authorization": str(location.write_authorization),
                        "authorization_evidence": "ring-base-station-owner"
                        if location.write_authorization is AlarmWriteAuthorization.ALLOWED
                        else None,
                        "can_set_mode": reason is None,
                        "reason": reason,
                        "allowed_targets": [
                            target for target, blocked in target_reasons.items() if blocked is None
                        ],
                        "target_reasons": {
                            target: blocked
                            for target, blocked in target_reasons.items()
                            if blocked is not None
                        },
                        "pending": pending is not None,
                        "outcome_unknown": location.location_id in self._uncertain,
                        "requested_mode": str(pending.target) if pending else None,
                    }
                )
        self._tokens = {key: value for key, value in self._tokens.items() if key in present}
        self._mode_receipts = {
            key: value for key, value in self._mode_receipts.items() if key in panel_keys
        }
        self._readbacks = {
            key: value for key, value in self._readbacks.items() if key in panel_keys
        }
        self._uncertain = {
            key: value
            for key, value in self._uncertain.items()
            if key in locations or key in self._pending
        }
        return tuple(output)

    def _find(self, device_id):
        for location in self.provider._store.snapshot.locations:
            for panel in location.devices:
                if (
                    panel.kind is AlarmDeviceKind.SECURITY_PANEL
                    and self._identity(panel).key == device_id
                ):
                    return location, panel
        return None, None

    @staticmethod
    def _result(status, code, *, sent=False, mode=None):
        return {"status": status, "code": code, "possibly_sent": sent, "mode": mode}

    async def request(
        self, device_id, target, *, expected_revision, expected_epoch, admission=None
    ):
        if (
            type(device_id) is not str
            or len(device_id) != 64
            or type(target) is not str
            or target not in {str(mode) for mode in _MODES}
            or not valid_generation(expected_revision)
            or not valid_generation(expected_epoch)
        ):
            return self._result("rejected", "invalid-request")
        if not callable(admission):
            return self._result("rejected", "policy-denied")
        target = AlarmMode(target)
        location, panel = self._find(device_id)
        if location is None:
            return self._result("rejected", "panel-unavailable")
        location_id = location.location_id
        if location_id in self._pending:
            return self._result("busy", "command-pending")
        runtime = self.provider._runtimes.get(location_id)

        def check():
            current, current_panel = self._find(device_id)
            if (
                current is None
                or current.epoch != expected_epoch
                or self.provider._runtimes.get(location_id) is not runtime
                or self._revision(current, current_panel) != expected_revision
            ):
                return "state-changed"
            try:
                admitted = admission() is True
            except Exception:
                admitted = False
            return self._reason(current, current_panel, target) if admitted else "policy-denied"

        reason = check()
        if reason:
            return self._result("rejected", reason)
        if panel.mode is target:
            return self._result("confirmed", "already-set", mode=str(target))
        pending = _Pending(
            runtime,
            expected_epoch,
            expected_revision,
            panel.asset_id,
            panel.zid,
            target,
            asyncio.get_running_loop().create_future(),
        )
        self._pending[location_id] = pending
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout
        # Reserve part of the single operation budget for a read-only refresh.
        # Timeout is never restarted independently for send and confirmation.
        observation_deadline = deadline - min(2.0, self.timeout * 0.2)

        @contextlib.contextmanager
        def guard():
            reason = check()
            if reason:
                raise AlarmCommandSupersededError(reason)
            pending.admitted = True
            pending.receipt = self._receipt
            yield

        try:
            await self.provider._publish()
            try:
                # Bound time spent waiting for the transport lock as well as
                # the socket write. There is never a queued second command.
                await asyncio.wait_for(
                    runtime.transport.send_mode(
                        panel.asset_id, panel.zid, target, epoch=expected_epoch, admission=guard
                    ),
                    timeout=max(0.0, observation_deadline - loop.time()),
                )
            except AlarmCommandSupersededError:
                return self._result("rejected", "state-changed")
            except asyncio.CancelledError:
                raise
            except Exception:
                if not pending.admitted:
                    return self._result("rejected", "connection-unavailable")
                if not pending.future.done():
                    pending.future.set_result(False)
                self._uncertain[location_id] = self._receipt
                await self._refresh(runtime, panel.asset_id, expected_epoch, deadline)
                return self._result("uncertain", "outcome-unknown", sent=True)
            try:
                confirmed = await asyncio.wait_for(
                    asyncio.shield(pending.future), max(0.0, observation_deadline - loop.time())
                )
            except TimeoutError:
                confirmed = False
            if confirmed:
                return self._result("confirmed", "observed", sent=True, mode=str(target))
            if not pending.future.done():
                pending.future.set_result(False)
            self._uncertain[location_id] = self._receipt
            await self._refresh(runtime, panel.asset_id, expected_epoch, deadline)
            return self._result("uncertain", "outcome-unknown", sent=True)
        except asyncio.CancelledError:
            if pending.admitted:
                self._uncertain[location_id] = self._receipt
            raise
        finally:
            # A callback failure must never leave an admitted command reusable.
            if pending.admitted and not pending.future.done():
                self._uncertain.setdefault(location_id, self._receipt)
            if not pending.future.done():
                pending.future.cancel()
            if self._pending.get(location_id) is pending:
                self._pending.pop(location_id)
            await self.provider._publish()

    async def _refresh(self, runtime, asset_id, epoch, deadline):
        remaining = deadline - asyncio.get_running_loop().time()
        if (
            remaining > 0
            and self.provider._valid_runtime(runtime, epoch)
            and runtime.transport.connected
        ):
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    runtime.transport.request_inventory(asset_id, epoch=epoch), remaining
                )

    def observe(self, runtime, epoch, message):
        self._receipt += 1
        location_id = runtime.seed.location_id
        if not isinstance(message, (DeviceInfoDocList, DeviceInfoDocUpdate)):
            self.invalidate(location_id, only_if_unavailable=True)
            return
        location = self.provider._store.snapshot.find_location(location_id)
        panel = location.security_panel if location else None
        if isinstance(message, DeviceInfoDocList):
            self._mode_receipts = {
                key: value
                for key, value in self._mode_receipts.items()
                if key[:2] != (location_id, message.asset_id)
            }
        if panel is None or panel.asset_id != message.asset_id:
            return
        document = next((doc for doc in message.devices if doc.zid == panel.zid), None)
        if document is None or "mode" not in document.data:
            return
        self._mode_receipts[(location_id, panel.asset_id, panel.zid)] = self.provider._monotonic()
        asset = location.find_asset(panel.asset_id)
        fresh = (
            location.epoch == epoch
            and not panel.stale
            and panel.connection is AlarmConnectionStatus.ONLINE
            and asset is not None
            and not asset.stale
            and asset.connection is AlarmConnectionStatus.ONLINE
            and asset.inventory is AlarmInventoryStatus.COMPLETE
            and panel.mode in _MODES
        )
        if not fresh:
            return
        if isinstance(message, DeviceInfoDocList):
            # A separate provider-lifetime sequence proves a new full readback.
            # Panel revisions also change for battery/name patches and cannot
            # resolve the daemon's lost-result uncertainty on their own.
            self._readback_counter = min(self._readback_counter + 1, 2**53 - 1)
            self._readbacks[(location_id, panel.asset_id, panel.zid)] = (
                epoch,
                self._readback_counter,
            )
        if isinstance(message, DeviceInfoDocList) and self._receipt > self._uncertain.get(
            location_id, self._receipt
        ):
            self._uncertain.pop(location_id, None)
        pending = self._pending.get(location_id)
        if (
            pending is not None
            and pending.admitted
            and pending.runtime is runtime
            and pending.epoch == epoch
            and pending.asset_id == panel.asset_id
            and pending.panel_id == panel.zid
            and self._receipt > pending.receipt
            and document.data.get("mode") == _WIRE_MODES[pending.target]
            and panel.mode is pending.target
            and not pending.future.done()
        ):
            pending.future.set_result(True)

    def invalidate(self, location_id, *, only_if_unavailable=False):
        pending = self._pending.get(location_id)
        if pending is None:
            return
        if only_if_unavailable:
            location = self.provider._store.snapshot.find_location(location_id)
            panel = location.security_panel if location else None
            if panel is not None and self._reason(location, panel) is None:
                return
        if pending.admitted:
            self._uncertain[location_id] = self._receipt
        if not pending.future.done():
            pending.future.set_result(False)
