"""Guarded keypad LED settings; no alarm, siren, volume or display-mode writes."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass

from halo_server.devices.models import DeviceIdentity, valid_generation
from halo_server.ring.hub.models import AlarmConnectionStatus, AlarmInventoryStatus
from halo_server.ring.hub.protocol import DeviceInfoDocList, DeviceInfoDocUpdate
from halo_server.ring.hub.provider import AlarmCommandSupersededError
from halo_server.ring.hub.store import keypad_brightness


@dataclass
class _Pending:
    runtime: object
    epoch: int
    target: int
    future: asyncio.Future
    admission: object
    admitted: bool = False
    receipt: int = 0


class KeypadCommands:
    """One command per keypad; only fresh post-send readings confirm it."""

    def __init__(self, provider, *, timeout=10.0):
        self.provider = provider
        self.timeout = min(timeout, 10.0)
        self._pending: dict[str, _Pending] = {}
        self._uncertain: dict[str, int] = {}
        self._tokens: dict[str, tuple[object, int]] = {}
        self._next_token = 0
        self._receipt = 0
        self._fields: dict[tuple[str, str, str], dict[str, float]] = {}
        self._readback_counter = 0
        self._readbacks: dict[str, tuple[int, int]] = {}

    def _identity(self, keypad):
        return DeviceIdentity(
            self.provider.profile_id, "ring-hub", keypad.location_id, keypad.asset_id, keypad.zid
        )

    def _authorization(self, location, keypad):
        runtime = self.provider._runtimes.get(location.location_id)
        matches = (
            [
                asset
                for asset in getattr(runtime.transport, "assets", ())
                if asset.asset_id == keypad.asset_id
            ]
            if runtime
            else []
        )
        return (
            str(location.write_authorization)
            if len(matches) == 1 and matches[0].kind.startswith("base_station")
            else "unknown"
        )

    def _revision(self, location, keypad):
        identity = self._identity(keypad).key
        asset = location.find_asset(keypad.asset_id)
        fingerprint = (
            location.epoch,
            keypad,
            self._authorization(location, keypad),
            (asset.kind, asset.connection, asset.stale, asset.inventory) if asset else None,
        )
        previous = self._tokens.get(identity)
        if previous is None or previous[0] != fingerprint:
            self._next_token += 1
            self._tokens[identity] = (fingerprint, self._next_token)
        return self._tokens[identity][1]

    def _fresh(self, location, keypad):
        asset = location.find_asset(keypad.asset_id)
        fields = self._fields.get((location.location_id, keypad.asset_id, keypad.zid), {})
        now = self.provider._monotonic()
        return (
            asset is not None
            and asset.kind.startswith("base_station")
            and asset.connection is AlarmConnectionStatus.ONLINE
            and not asset.stale
            and asset.inventory is AlarmInventoryStatus.COMPLETE
            and asset.inventory_monotonic is not None
            and 0 <= now - asset.inventory_monotonic <= 120
            and not keypad.stale
            and keypad.connection is AlarmConnectionStatus.ONLINE
            and keypad.keypad_brightness is not None
            and all(
                name in fields and 0 <= now - fields[name] <= 120
                for name in ("brightness", "commStatus")
            )
        )

    def _reason(self, location, keypad):
        provider = self.provider
        runtime = provider._runtimes.get(location.location_id)
        if not provider._active() or not provider._rest_valid:
            return "source-unavailable"
        if runtime is None or not provider._valid_runtime(runtime, location.epoch):
            return "connection-superseded"
        if not runtime.transport.connected or keypad.connection is not AlarmConnectionStatus.ONLINE:
            return "keypad-offline"
        if keypad.raw_type != "security-keypad" or keypad.keypad_brightness is None:
            return "keypad-unsupported"
        if self._authorization(location, keypad) != "allowed":
            return "authorization-unknown"
        if not self._fresh(location, keypad):
            return "keypad-stale"
        if self._identity(keypad).key in self._uncertain:
            return "outcome-unknown"
        return None

    def _find(self, device_id):
        for location in self.provider._store.snapshot.locations:
            for keypad in location.devices:
                if keypad.raw_type == "security-keypad" and self._identity(keypad).key == device_id:
                    return location, keypad
        return None, None

    def snapshots(self):
        output, present, keys = [], set(), set()
        for location in self.provider._store.snapshot.locations:
            for keypad in location.devices:
                if keypad.raw_type != "security-keypad":
                    continue
                identity = self._identity(keypad)
                present.add(identity.key)
                keys.add((location.location_id, keypad.asset_id, keypad.zid))
                pending = self._pending.get(identity.key)
                epoch, readback = self._readbacks.get(identity.key, (0, 0))
                reason = "command-pending" if pending else self._reason(location, keypad)
                output.append(
                    {
                        "device_id": identity.key,
                        "location_id": identity.location_key,
                        "epoch": location.epoch,
                        "revision": self._revision(location, keypad),
                        "readback_revision": readback if epoch == location.epoch else 0,
                        "brightness": keypad.keypad_brightness,
                        "authorization": self._authorization(location, keypad),
                        "can_set": reason is None,
                        "reason": reason,
                        "pending": pending is not None,
                        "outcome_unknown": identity.key in self._uncertain,
                        "requested_brightness": pending.target if pending else None,
                    }
                )
        self._tokens = {key: value for key, value in self._tokens.items() if key in present}
        self._readbacks = {key: value for key, value in self._readbacks.items() if key in present}
        self._fields = {key: value for key, value in self._fields.items() if key in keys}
        self._uncertain = {
            key: value
            for key, value in self._uncertain.items()
            if key in present or key in self._pending
        }
        return tuple(output)

    @staticmethod
    def _result(status, code, *, sent=False, brightness=None):
        return {"status": status, "code": code, "possibly_sent": sent, "brightness": brightness}

    async def request(
        self, device_id, target, *, expected_revision, expected_epoch, admission=None
    ):
        if (
            type(device_id) is not str
            or len(device_id) != 64
            or type(target) is not int
            or not 0 <= target <= 100
            or not valid_generation(expected_revision)
            or not valid_generation(expected_epoch)
        ):
            return self._result("rejected", "invalid-request")
        if not callable(admission):
            return self._result("rejected", "policy-denied")
        location, keypad = self._find(device_id)
        if location is None:
            return self._result("rejected", "keypad-unavailable")
        if device_id in self._pending:
            return self._result("busy", "command-pending")
        runtime = self.provider._runtimes.get(location.location_id)

        def allowed():
            try:
                return admission() is True
            except Exception:
                return False

        def check():
            current, current_keypad = self._find(device_id)
            if (
                current is None
                or current.epoch != expected_epoch
                or self.provider._runtimes.get(location.location_id) is not runtime
                or self._revision(current, current_keypad) != expected_revision
            ):
                return "state-changed"
            return self._reason(current, current_keypad) if allowed() else "policy-denied"

        reason = check()
        if reason:
            return self._result("rejected", reason)
        pending = _Pending(
            runtime, expected_epoch, target, asyncio.get_running_loop().create_future(), admission
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
                await asyncio.wait_for(
                    runtime.transport.send_keypad_brightness(
                        keypad.asset_id, keypad.zid, target, epoch=expected_epoch, admission=guard
                    ),
                    max(0.0, observation_deadline - loop.time()),
                )
            except AlarmCommandSupersededError:
                if not pending.admitted:
                    return self._result("rejected", "state-changed")
                # Only pre-handoff rejection proves nothing was sent. A custom
                # transport must not turn a later error into a safe retry.
                if not pending.future.done():
                    pending.future.set_result(False)
            except asyncio.CancelledError:
                raise
            except Exception:
                if not pending.admitted:
                    return self._result("rejected", "connection-unavailable")
                if not pending.future.done():
                    pending.future.set_result(False)
            try:
                confirmed = await asyncio.wait_for(
                    asyncio.shield(pending.future), max(0.0, observation_deadline - loop.time())
                )
            except TimeoutError:
                confirmed = False
            current, current_keypad = self._find(device_id)
            if (
                confirmed
                and allowed()
                and current is not None
                and current.epoch == expected_epoch
                and self._reason(current, current_keypad) is None
                and current_keypad.keypad_brightness == target
            ):
                return self._result("confirmed", "observed", sent=True, brightness=target)
            self._uncertain[device_id] = self._receipt
            remaining = deadline - loop.time()
            if (
                remaining > 0
                and self.provider._valid_runtime(runtime, expected_epoch)
                and runtime.transport.connected
            ):
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(
                        runtime.transport.request_inventory(keypad.asset_id, epoch=expected_epoch),
                        remaining,
                    )
            return self._result("uncertain", "outcome-unknown", sent=True)
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
            self._fields = {
                key: value
                for key, value in self._fields.items()
                if key[:2] != (location_id, message.asset_id)
            }
        for document in message.devices:
            key = (location_id, message.asset_id, document.zid)
            fields = self._fields.setdefault(key, {})
            for name in ("brightness", "commStatus"):
                if name in document.data:
                    fields[name] = self.provider._monotonic()
        for keypad in location.devices:
            if keypad.raw_type != "security-keypad" or keypad.asset_id != message.asset_id:
                continue
            identity = self._identity(keypad).key
            document = next((doc for doc in message.devices if doc.zid == keypad.zid), None)
            if (
                document is None
                or keypad_brightness(document.data.get("brightness")) is None
                or not self._fresh(location, keypad)
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
                and keypad.keypad_brightness == pending.target
                and not pending.future.done()
            ):
                try:
                    allowed = pending.admission() is True
                except Exception:
                    allowed = False
                pending.future.set_result(allowed and self._reason(location, keypad) is None)
        self.invalidate(location_id, only_if_unavailable=True)

    def invalidate(self, location_id, *, only_if_unavailable=False):
        if not only_if_unavailable:
            self._fields = {
                key: value for key, value in self._fields.items() if key[0] != location_id
            }
        for device_id, pending in self._pending.items():
            if pending.runtime.seed.location_id != location_id:
                continue
            if only_if_unavailable:
                location, keypad = self._find(device_id)
                if location is not None and self._reason(location, keypad) is None:
                    continue
            if pending.admitted:
                self._uncertain[device_id] = self._receipt
            if not pending.future.done():
                pending.future.set_result(False)
