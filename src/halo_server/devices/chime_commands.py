"""Qualified legacy Chime controls over the provider's shared authenticated REST.

Inventory is evidence, not an acknowledgement of a command. Sound tests retain
an explicit owner-acknowledged latch because Ring offers no playback completion.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
from collections.abc import Mapping
from dataclasses import dataclass

from halo_server.devices.chime_policy import CHIME_TYPES
from halo_server.devices.models import DeviceIdentity, valid_generation
from halo_server.homekit.text import literal_text
from halo_server.ring.json_transport import RingJsonError, RingWriteCancelled


@dataclass(frozen=True)
class ChimeState:
    identity: DeviceIdentity
    api_id: int | None
    kind: str
    hardware_id: str | None
    owned: bool | None
    online: bool | None
    volume: int | None
    snooze: int | None
    nightlight: bool | None


def _mapping(value):
    return value if isinstance(value, Mapping) else {}


def _integer(value, minimum, maximum):
    return value if type(value) is int and minimum <= value <= maximum else None


def chime_values(row):
    settings = _mapping(row.get("settings"))
    connection = _mapping(row.get("alerts")).get("connection")
    nightlight = _mapping(settings.get("night_light_settings")).get("light_sensor_enabled")
    return {
        "connected": True if connection == "online" else False if connection == "offline" else None,
        "volume": _integer(settings.get("volume"), 0, 11),
        "snooze_seconds_left": _integer(
            _mapping(row.get("do_not_disturb")).get("seconds_left"), 0, 2_147_483_647
        ),
        "automatic_nightlight": nightlight if type(nightlight) is bool else None,
    }


def valid_action(action, value):
    return bool(
        type(action) is str
        and (
            action == "volume"
            and type(value) is int
            and 0 <= value <= 11
            or action == "test"
            and type(value) is str
            and value in {"ding", "motion"}
            or action == "snooze"
            and type(value) is int
            and 0 <= value <= 1440
            or action == "nightlight"
            and type(value) is bool
            or action == "sound_stopped"
            and value is None
        )
    )


@dataclass
class _Pending:
    action: str
    sent: bool = False
    started: float = 0.0
    sequence: int = 0


class ChimeCommands:
    def __init__(self, provider, *, timeout=10.0):
        self.provider = provider
        self.timeout = min(timeout, 10.0)
        self._states = {}
        self._epochs = {}
        self._tokens = {}
        self._counter = 0
        self._readback = 0
        self._readbacks = {}
        self._received = None
        self._started = None
        self._sequence = 0
        self._nightlight_seen = set()
        self._valid = False
        self._pending = {}
        self._uncertain = {}
        self._sound_active = set()
        self._retry_at = {}
        self._requests = set()

    def _next(self):
        self._counter += 1
        return self._counter

    def invalidate(self):
        if self._valid:
            for key in self._states:
                self._epochs[key] = self._next()
                self._tokens[key] = self._next()
        self._valid = False

    def observe(self, devices, *, started, sequence):
        """Called only after the provider validates the entire inventory/locations."""
        rows = devices.get("chimes", ())
        rows = rows.values() if isinstance(rows, Mapping) else rows
        states = {}
        for row in rows:
            values = chime_values(row)
            hardware = row.get("device_id")
            valid_hardware = hardware is None or (
                type(hardware) is str and bool(hardware) and literal_text(hardware, 128)
            )
            identity = DeviceIdentity(
                self.provider.profile_id,
                "ring-rest",
                str(row.get("location_id", row.get("locationId"))),
                "chimes",
                str(row["id"]),
            )
            states[identity.key] = ChimeState(
                identity,
                _integer(row.get("id"), 1, 2**63 - 1) if valid_hardware else None,
                row.get("kind") if type(row.get("kind")) is str else "",
                hardware if valid_hardware else None,
                row.get("owned") if type(row.get("owned")) is bool else None,
                values["connected"],
                values["volume"],
                values["snooze_seconds_left"],
                values["automatic_nightlight"],
            )
        for key, state in states.items():
            old = self._states.get(key)
            if old is None or (old.api_id, old.kind, old.hardware_id, old.owned, old.online) != (
                state.api_id,
                state.kind,
                state.hardware_id,
                state.owned,
                state.online,
            ):
                self._epochs[key] = self._next()
            if not self._valid or self._states.get(key) != state:
                self._tokens[key] = self._next()
            if state.nightlight is not None:
                self._nightlight_seen.add(key)
            if (
                state.online is True
                and state.volume is not None
                and state.snooze is not None
                and (key not in self._nightlight_seen or state.nightlight is not None)
            ):
                self._readback += 1
                self._readbacks[key] = self._readback
            unknown = self._uncertain.get(key)
            if (
                unknown is not None
                and unknown[0] != "test"
                and sequence > unknown[1]
                and state.online is True
                and self._value(state, unknown[0]) is not None
                and key not in self._pending
            ):
                self._uncertain.pop(key, None)
        self._states = states
        self._received = self.provider._monotonic()
        self._started = started
        self._sequence = sequence
        self._valid = True
        for values in (self._tokens, self._epochs, self._readbacks, self._retry_at):
            for key in values.keys() - states.keys():
                values.pop(key, None)
        self._nightlight_seen.intersection_update(
            states.keys() | self._uncertain.keys() | self._pending.keys()
        )
        # Keep uncertainty/sound latches for a temporarily missing device until
        # provider retirement; the same identity must not regain a repeat test.

    @staticmethod
    def _value(state, action):
        return getattr(
            state,
            {"volume": "volume", "snooze": "snooze", "nightlight": "nightlight"}.get(
                action, "volume"
            ),
        )

    @staticmethod
    def _authorization(state):
        return "allowed" if state.owned is True else "unknown"

    def _reason(self, key, *, acknowledging=False):
        state = self._states.get(key)
        if not self.provider._active() or not self.provider._rest_valid or not self._valid:
            return "source-unavailable"
        if state is None:
            return "chime-unavailable"
        if state.kind not in CHIME_TYPES or state.api_id is None:
            return "chime-unsupported"
        if self._authorization(state) != "allowed":
            return "authorization-unknown"
        if acknowledging:
            return None if key in self._sound_active else "sound-not-active"
        if self.provider._monotonic() < self._retry_at.get(key, 0):
            return "rate-limited"
        if self._received is None or not 0 <= self.provider._monotonic() - self._received <= 30:
            return "chime-stale"
        if state.online is not True:
            return "chime-offline"
        if key in self._uncertain:
            return "outcome-unknown"
        if key in self._sound_active:
            return "sound-active"
        return None

    def snapshots(self):
        result = []
        for key, state in self._states.items():
            pending = self._pending.get(key)
            reason = "command-pending" if pending else self._reason(key)
            result.append(
                {
                    "device_id": key,
                    "location_id": state.identity.location_key,
                    "epoch": self._epochs[key],
                    "revision": self._tokens[key],
                    "readback_revision": self._readbacks.get(key, 0),
                    "authorization": self._authorization(state),
                    "can_set": reason is None,
                    "reason": reason,
                    "pending": pending is not None,
                    "outcome_unknown": key in self._uncertain,
                    "requested_action": pending.action
                    if pending
                    else self._uncertain.get(key, (None,))[0],
                    "volume": state.volume,
                    "snooze_seconds_left": state.snooze,
                    "automatic_nightlight": state.nightlight,
                    "sound_active": key in self._sound_active,
                }
            )
        return tuple(result)

    @staticmethod
    def _result(status, code, sent=False):
        return {"status": status, "code": code, "possibly_sent": sent}

    async def request(
        self, device_id, action, value, *, expected_revision, expected_epoch, admission=None
    ):
        if (
            type(device_id) is not str
            or len(device_id) != 64
            or not valid_action(action, value)
            or not valid_generation(expected_revision)
            or not valid_generation(expected_epoch)
        ):
            return self._result("rejected", "invalid-request")
        if not callable(admission):
            return self._result("rejected", "policy-denied")
        if device_id in self._pending:
            return self._result("busy", "command-pending")
        acknowledging = action == "sound_stopped"

        def check(*, sent=False):
            if (
                device_id not in self._states
                or self._epochs.get(device_id) != expected_epoch
                or (not sent and self._tokens.get(device_id) != expected_revision)
            ):
                return "state-changed"
            try:
                permitted = admission() is True
            except Exception:
                permitted = False
            if not permitted:
                return "policy-denied"
            reason = self._reason(device_id, acknowledging=acknowledging)
            pending = self._pending.get(device_id)
            if reason == "sound-active" and pending and pending.sent and pending.action == "test":
                reason = None
            if sent and reason in {"sound-active", "outcome-unknown"}:
                reason = None
            return reason

        reason = check()
        if reason:
            return self._result("rejected", reason)
        if acknowledging:
            self._sound_active.discard(device_id)
            if self._uncertain.get(device_id, (None,))[0] == "test":
                self._uncertain.pop(device_id, None)
            self._tokens[device_id] = self._next()
            async with asyncio.timeout(min(self.timeout, 1.0)):
                await self.provider._publish()
            return self._result("confirmed", "sound-stop-acknowledged")
        state = self._states[device_id]
        retained = self._sound_active | self._uncertain.keys()
        if device_id not in retained and len(retained) >= 1024:
            return self._result("busy", "command-history-full")
        if action == "nightlight" and state.nightlight is None:
            return self._result("rejected", "nightlight-unsupported")
        gateway = self.provider._get_device_gateway()
        if gateway is None:
            return self._result("rejected", "transport-unavailable")
        pending = _Pending(action)
        self._pending[device_id] = pending
        current = asyncio.current_task()
        self._requests.add(current)

        def admitted():
            reason = check()
            if reason:
                raise RingJsonError("admission-denied")
            return True

        def marker():
            pending.sent = True
            pending.started = self.provider._monotonic()
            pending.sequence = self.provider._discovery_sequence
            if action == "test":
                self._sound_active.add(device_id)

        def unknown(code):
            self._uncertain[device_id] = (action, self.provider._discovery_sequence)
            return self._result("uncertain", code, True)

        try:
            async with asyncio.timeout(self.timeout):
                await self.provider._publish()
                # Always perform a fresh operation-specific write. Ring volume
                # and automatic-nightlight data are never optimistically changed.
                common = {"admission": admitted, "on_admitted": marker, "timeout": self.timeout}
                if action == "volume":
                    receipt = await gateway.set_chime_volume(state.api_id, value, **common)
                elif action == "test":
                    receipt = await gateway.test_chime(state.api_id, value, **common)
                elif action == "snooze":
                    receipt = await gateway.snooze_chime(state.api_id, value or None, **common)
                else:
                    receipt = await gateway.set_chime_nightlight(state.api_id, value, **common)
                pending.sent |= receipt.possibly_sent
                if receipt.code == "rate-limited":
                    retry = receipt.retry_after
                    delay = (
                        min(3600, retry)
                        if type(retry) in (int, float) and math.isfinite(retry) and retry > 0
                        else 60
                    )
                    self._retry_at[device_id] = self.provider._monotonic() + delay
                    self.provider._discovery_retry_at = max(
                        self.provider._discovery_retry_at, self._retry_at[device_id]
                    )
                if receipt.outcome == "rejected":
                    if action == "test":
                        self._sound_active.discard(device_id)
                    return self._result("rejected", receipt.code, pending.sent)
                if not receipt.accepted:
                    return unknown(receipt.code)
                reason = check(sent=True)
                if reason:
                    return unknown(reason)
                if action == "test":
                    return self._result("accepted", "sound-request-accepted", True)
                # The provider lock starts the inventory request inside refresh;
                # only a full read begun after this command may confirm it.
                for attempt in range(3):
                    await self.provider.refresh()
                    reason = check(sent=True)
                    if reason:
                        return unknown(reason)
                    current_state = self._states[device_id]
                    if self._sequence > pending.sequence:
                        actual = self._value(current_state, action)
                        if action == "snooze" and value:
                            elapsed = max(0, self.provider._monotonic() - pending.started)
                            matched = (
                                type(actual) is int
                                and max(0, value * 60 - math.ceil(elapsed) - 2)
                                <= actual
                                <= value * 60
                            )
                        else:
                            matched = actual is not None and actual == value
                        if matched:
                            return self._result("confirmed", "state-confirmed", True)
                    if attempt < 2:
                        await asyncio.sleep(0.2)
                return unknown("confirmation-unavailable")
        except asyncio.CancelledError as error:
            pending.sent |= isinstance(error, RingWriteCancelled) and error.possibly_sent
            if pending.sent:
                unknown("cancelled")
            raise
        except Exception:
            return (
                unknown("result-unavailable")
                if pending.sent
                else self._result("rejected", "request-failed")
            )
        finally:
            self._pending.pop(device_id, None)
            self._requests.discard(current)
            if device_id in self._states:
                self._tokens[device_id] = self._next()
            with contextlib.suppress(Exception):
                async with asyncio.timeout(min(self.timeout, 1.0)):
                    await self.provider._publish()

    async def close(self):
        self.invalidate()
        tasks = self._requests - {asyncio.current_task()}
        for task in tasks:
            if not task.cancelling():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
