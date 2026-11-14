"""Finite physical Beams lighting commands on the existing monitor socket.

``default`` restores Ring automatic behavior; it cannot suppress motion lighting.
An accepted socket write is never treated as proof of the physical lamp state.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass

from halo_server.devices.models import DeviceIdentity, valid_generation
from halo_server.ring.hub.models import AlarmConnectionStatus, AlarmInventoryStatus, TriState
from halo_server.ring.hub.protocol import DeviceInfoDocList, DeviceInfoDocUpdate
from halo_server.ring.hub.provider import AlarmCommandSupersededError

BEAMS_TYPES = frozenset({"switch.beams", "switch.multilevel.beams", "switch.transformer.beams"})
ON_DURATIONS = frozenset({30, 60, 300, 900})


@dataclass
class _Pending:
    runtime: object
    epoch: int
    asset_id: str
    zid: str
    target: bool
    future: asyncio.Future
    admission: object
    admitted: bool = False
    receipt: int = 0


class LightCommands:
    """One provider loop owns all state; busy writes are rejected, never queued."""

    def __init__(self, provider, *, timeout=10.0):
        self.provider = provider
        self.timeout = min(timeout, 10.0)
        self._pending: dict[str, _Pending] = {}
        self._uncertain: dict[str, int] = {}
        self._tokens: dict[str, tuple[object, int]] = {}
        self._next_token = 0
        self._receipt = 0
        self._field_receipts: dict[tuple[str, str, str], dict[str, float]] = {}
        self._readback_counter = 0
        self._readbacks: dict[str, tuple[int, int]] = {}

    def _identity(self, light):
        return DeviceIdentity(
            self.provider.profile_id, "ring-hub", light.location_id, light.asset_id, light.zid
        )

    def _authorization(self, location, light):
        runtime = self.provider._runtimes.get(location.location_id)
        assets = getattr(runtime.transport, "assets", ()) if runtime else ()
        matches = [asset for asset in assets if asset.asset_id == light.asset_id]
        if len(matches) != 1:
            return "unknown"
        asset = matches[0]
        doorbot_id = getattr(asset, "doorbot_id", None)
        if (
            asset.kind != "beams_bridge_v1"
            or type(doorbot_id) is not int
            or not 0 < doorbot_id < 2**53
            or sum(getattr(item, "doorbot_id", None) == doorbot_id for item in assets) != 1
        ):
            return "unknown"
        return (
            "allowed"
            if self.provider._beams_owners.get((location.location_id, str(doorbot_id))) is True
            else "unknown"
        )

    def _revision(self, location, light):
        identity = self._identity(light).key
        asset = location.find_asset(light.asset_id)
        fingerprint = (
            location.epoch,
            light,
            self._authorization(location, light),
            (asset.kind, asset.connection, asset.stale, asset.inventory) if asset else None,
        )
        previous = self._tokens.get(identity)
        if previous is None or previous[0] != fingerprint:
            self._next_token += 1
            self._tokens[identity] = (fingerprint, self._next_token)
        return self._tokens[identity][1]

    def _fresh(self, location, light):
        asset = location.find_asset(light.asset_id)
        fields = self._field_receipts.get((location.location_id, light.asset_id, light.zid), {})
        now = self.provider._monotonic()
        return (
            asset is not None
            and asset.kind == "beams_bridge_v1"
            and asset.connection is AlarmConnectionStatus.ONLINE
            and not asset.stale
            and asset.inventory is AlarmInventoryStatus.COMPLETE
            and asset.inventory_monotonic is not None
            and 0 <= now - asset.inventory_monotonic <= 120
            and not light.stale
            and light.connection is AlarmConnectionStatus.ONLINE
            and light.switch_on is not TriState.UNKNOWN
            and all(
                name in fields and 0 <= now - fields[name] <= 120 for name in ("on", "commStatus")
            )
        )

    def _reason(self, location, light):
        provider = self.provider
        runtime = provider._runtimes.get(location.location_id)
        if not provider._active() or not provider._rest_valid:
            return "source-unavailable"
        if runtime is None or not provider._valid_runtime(runtime, location.epoch):
            return "connection-superseded"
        if not runtime.transport.connected or light.connection is not AlarmConnectionStatus.ONLINE:
            return "light-offline"
        if light.raw_type not in BEAMS_TYPES:
            return "light-unsupported"
        if self._authorization(location, light) != "allowed":
            return "authorization-unknown"
        if not self._fresh(location, light):
            return "light-stale"
        if self._identity(light).key in self._uncertain:
            return "outcome-unknown"
        return None

    def _find(self, device_id):
        for location in self.provider._store.snapshot.locations:
            for light in location.devices:
                if light.raw_type in BEAMS_TYPES and self._identity(light).key == device_id:
                    return location, light
        return None, None

    def snapshots(self):
        output = []
        present = set()
        source_keys = set()
        for location in self.provider._store.snapshot.locations:
            for light in location.devices:
                if light.raw_type not in BEAMS_TYPES:
                    continue
                identity = self._identity(light)
                present.add(identity.key)
                source_keys.add((location.location_id, light.asset_id, light.zid))
                pending = self._pending.get(identity.key)
                epoch, readback = self._readbacks.get(identity.key, (0, 0))
                reason = "command-pending" if pending else self._reason(location, light)
                output.append(
                    {
                        "device_id": identity.key,
                        "location_id": identity.location_key,
                        "epoch": location.epoch,
                        "revision": self._revision(location, light),
                        "readback_revision": readback if epoch == location.epoch else 0,
                        "on": None
                        if light.switch_on is TriState.UNKNOWN
                        else light.switch_on is TriState.ACTIVE,
                        "authorization": self._authorization(location, light),
                        "can_set": reason is None,
                        "reason": reason,
                        "pending": pending is not None,
                        "outcome_unknown": identity.key in self._uncertain,
                        "requested_on": pending.target if pending else None,
                    }
                )
        self._tokens = {key: value for key, value in self._tokens.items() if key in present}
        self._readbacks = {key: value for key, value in self._readbacks.items() if key in present}
        self._field_receipts = {
            key: value for key, value in self._field_receipts.items() if key in source_keys
        }
        self._uncertain = {
            key: value
            for key, value in self._uncertain.items()
            if key in present or key in self._pending
        }
        return tuple(output)

    @staticmethod
    def _result(status, code, *, sent=False, on=None):
        return {"status": status, "code": code, "possibly_sent": sent, "on": on}

    def _observed(self, device_id):
        location, light = self._find(device_id)
        runtime = self.provider._runtimes.get(location.location_id) if location else None
        if (
            location is None
            or runtime is None
            or not self.provider._rest_valid
            or not self.provider._valid_runtime(runtime, location.epoch)
            or not runtime.transport.connected
            or not self._fresh(location, light)
        ):
            return None
        return light.switch_on is TriState.ACTIVE

    async def request(
        self,
        device_id,
        target,
        *,
        expected_revision,
        expected_epoch,
        duration_seconds,
        admission=None,
    ):
        if (
            type(device_id) is not str
            or len(device_id) != 64
            or type(target) is not bool
            or not valid_generation(expected_revision)
            or not valid_generation(expected_epoch)
            or (
                target
                and (type(duration_seconds) is not int or duration_seconds not in ON_DURATIONS)
            )
            or (not target and duration_seconds is not None)
        ):
            return self._result("rejected", "invalid-request")
        if not callable(admission):
            return self._result("rejected", "policy-denied")
        location, light = self._find(device_id)
        if location is None:
            return self._result("rejected", "light-unavailable")

        def finish(status, code, *, sent=False):
            return self._result(status, code, sent=sent, on=self._observed(device_id))

        if device_id in self._pending:
            return finish("busy", "command-pending")
        runtime = self.provider._runtimes.get(location.location_id)

        def check():
            current, current_light = self._find(device_id)
            if (
                current is None
                or current.epoch != expected_epoch
                or self.provider._runtimes.get(location.location_id) is not runtime
                or self._revision(current, current_light) != expected_revision
            ):
                return "state-changed"
            try:
                allowed = admission() is True
            except Exception:
                allowed = False
            return self._reason(current, current_light) if allowed else "policy-denied"

        reason = check()
        if reason:
            return finish("rejected", reason)
        pending = _Pending(
            runtime,
            expected_epoch,
            light.asset_id,
            light.zid,
            target,
            asyncio.get_running_loop().create_future(),
            admission,
        )
        self._pending[device_id] = pending
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout
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
                # Always send: On renews a finite override even when already
                # lit; Off restores automatic mode even when already dark.
                await asyncio.wait_for(
                    runtime.transport.send_light(
                        light.asset_id,
                        light.zid,
                        target,
                        duration_seconds=duration_seconds,
                        epoch=expected_epoch,
                        admission=guard,
                    ),
                    max(0.0, observation_deadline - loop.time()),
                )
            except AlarmCommandSupersededError:
                return finish("rejected", "state-changed")
            except asyncio.CancelledError:
                raise
            except Exception:
                if not pending.admitted:
                    return finish("rejected", "connection-unavailable")
                if not pending.future.done():
                    pending.future.set_result(False)
            try:
                confirmed = await asyncio.wait_for(
                    asyncio.shield(pending.future), max(0.0, observation_deadline - loop.time())
                )
            except TimeoutError:
                confirmed = False
            # A later packet can supersede the confirmation before this task
            # resumes. Return today's observation, never the cached intention.
            current, current_light = self._find(device_id)
            try:
                still_admitted = admission() is True
            except Exception:
                still_admitted = False
            if (
                confirmed
                and still_admitted
                and current is not None
                and self._reason(current, current_light) is None
                and self._observed(device_id) is target
            ):
                return finish("confirmed", "observed", sent=True)
            if not pending.future.done():
                pending.future.set_result(False)
            self._uncertain[device_id] = self._receipt
            await self._refresh(runtime, light.asset_id, expected_epoch, deadline)
            return finish("uncertain", "outcome-unknown", sent=True)
        except asyncio.CancelledError:
            if pending.admitted:
                self._uncertain[device_id] = self._receipt
            raise
        finally:
            if pending.admitted and not pending.future.done():
                self._uncertain.setdefault(device_id, self._receipt)
            if not pending.future.done():
                pending.future.cancel()
            if self._pending.get(device_id) is pending:
                self._pending.pop(device_id)
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
        if location is None or location.epoch != epoch:
            return
        if isinstance(message, DeviceInfoDocList):
            self._field_receipts = {
                key: value
                for key, value in self._field_receipts.items()
                if key[:2] != (location_id, message.asset_id)
            }
        for document in message.devices:
            key = (location_id, message.asset_id, document.zid)
            fields = self._field_receipts.setdefault(key, {})
            for name in ("on", "commStatus"):
                if name in document.data:
                    fields[name] = self.provider._monotonic()
        for light in location.devices:
            if light.raw_type not in BEAMS_TYPES or light.asset_id != message.asset_id:
                continue
            identity = self._identity(light).key
            document = next((doc for doc in message.devices if doc.zid == light.zid), None)
            if (
                document is None
                or type(document.data.get("on")) is not bool
                or not self._fresh(location, light)
            ):
                continue
            if isinstance(message, DeviceInfoDocList):
                self._readback_counter = min(self._readback_counter + 1, 2**53 - 1)
                self._readbacks[identity] = (epoch, self._readback_counter)
                if self._receipt > self._uncertain.get(identity, self._receipt):
                    self._uncertain.pop(identity, None)
            pending = self._pending.get(identity)
            if (
                pending is not None
                and pending.admitted
                and pending.runtime is runtime
                and pending.epoch == epoch
                and self._receipt > pending.receipt
                and document.data["on"] is pending.target
                and not pending.future.done()
            ):
                try:
                    allowed = pending.admission() is True
                except Exception:
                    allowed = False
                pending.future.set_result(allowed and self._reason(location, light) is None)
        self.invalidate(location_id, only_if_unavailable=True)

    def invalidate(self, location_id, *, only_if_unavailable=False):
        for device_id, pending in self._pending.items():
            if pending.runtime.seed.location_id != location_id:
                continue
            if only_if_unavailable:
                location, light = self._find(device_id)
                if location is not None and self._reason(location, light) is None:
                    continue
            if pending.admitted:
                self._uncertain[device_id] = self._receipt
            if not pending.future.done():
                pending.future.set_result(False)
