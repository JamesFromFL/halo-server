"""REST lighting groups with observed state and one account lighting owner.

Reports describe the group aggregate. They never overwrite member/camera state.
No retries, local expiry switch-off, rollback, membership edits or brightness.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass

from halo_server.devices.groups import (
    MAX_GROUPS,
    MAX_LOCATIONS,
    MAX_TOTAL_MEMBERS,
    GroupInventory,
    GroupReport,
    GroupSchemaError,
    identifier,
    parse_groups,
    parse_report,
)
from halo_server.devices.light_policy import ON_DURATIONS
from halo_server.devices.models import (
    DeviceIdentity,
    DeviceObservation,
    DeviceRecord,
    valid_generation,
)
from halo_server.ring.json_transport import RingJsonError
from halo_server.ring.lighting import LightingReservation

REPORT_MAX_AGE = 120.0
INVENTORY_MAX_AGE = 600.0
DISCOVERY_TIMEOUT_SECONDS = 30.0
READ_TIMEOUT_SECONDS = 10.0


@dataclass
class _Group:
    inventory: GroupInventory
    location_name: str
    epoch: int
    revision: int
    inventory_at: float
    inventory_valid: bool = True
    authorization: str = "unknown"
    report: GroupReport | None = None
    reported_at: float | None = None
    report_clock: float | None = None
    readback_revision: int = 0
    error: str | None = None
    pending: bool = False
    target: bool | None = None
    uncertain: bool = False
    denied: bool = False
    retry_at: float = 0
    serial: int = 0
    report_lighting_revision: int = -1
    own_lighting_revision: int | None = None


class GroupCommands:
    def __init__(self, provider, gateway, reservation=None, *, timeout=10.0):
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Invalid group command timeout")
        self.provider, self.gateway = provider, gateway
        self.reservation = (
            reservation or getattr(provider, "_lighting_reservation", None) or LightingReservation()
        )
        self.timeout = min(timeout, 10.0)
        self._groups: dict[str, _Group] = {}
        self._next_epoch = 0
        self._reads = {}
        self._tasks = set()
        self._closed = False
        self._poll_cursor = 0
        self._refresh_cursor = 0
        self._list_retry = {}
        self._context_revision = 0
        self._context_ownership = {}
        self._context_locations = None
        self._context_valid = False
        self._refreshing = False
        self.complete = False

    def identity(self, inventory):
        return DeviceIdentity(
            self.provider.profile_id,
            "ring-light-group",
            inventory.location_id,
            "",
            inventory.group_id,
        )

    def has_group(self, location_id, group_id):
        return any(
            row.inventory.location_id == location_id and row.inventory.group_id == group_id
            for row in self._groups.values()
        )

    def _active(self):
        return not self._closed and self.provider._active()

    def _fresh_inventory(self, row):
        return (
            row.inventory_valid
            and 0 <= self.provider._monotonic() - row.inventory_at <= INVENTORY_MAX_AGE
        )

    def _observed(self, row):
        return (
            row.report.on
            if self._active()
            and self.provider._rest_valid
            and self._fresh_inventory(row)
            and row.error is None
            and row.report is not None
            and row.report_clock is not None
            and 0 <= self.provider._monotonic() - row.report_clock <= REPORT_MAX_AGE
            and (
                row.report_lighting_revision == self.reservation.revision
                or row.pending
                and row.own_lighting_revision == self.reservation.revision
            )
            else None
        )

    def update_ownership(self, ownership, *, locations=None, valid=True):
        """Synchronously revoke stale REST authority before the next group poll.

        Call on every discovery receipt/failure, not just the group inventory
        schedule. Unknown/malformed input revokes all group command authority.
        """
        accepted = (
            valid is True
            and type(ownership) is dict
            and len(ownership) <= MAX_TOTAL_MEMBERS
            and (locations is None or type(locations) is dict and len(locations) <= MAX_LOCATIONS)
        )
        if accepted and locations is not None:
            try:
                for location_id in locations:
                    identifier(location_id)
            except GroupSchemaError:
                accepted = False
        normalized = {}
        if accepted:
            for key, value in ownership.items():
                if (
                    type(key) is not tuple
                    or len(key) != 3
                    or any(type(part) is not str or len(part) > 128 for part in key)
                ):
                    accepted = False
                    break
                normalized[key] = value is True
        if not accepted:
            normalized = {}
        location_context = (
            frozenset(locations) if accepted and locations is not None else self._context_locations
        )
        if (
            accepted != self._context_valid
            or normalized != self._context_ownership
            or location_context != self._context_locations
        ):
            self._context_revision += 1
            self._context_valid = accepted
            self._context_ownership = normalized
            self._context_locations = location_context
        for row in self._groups.values():
            present = accepted and (locations is None or row.inventory.location_id in locations)
            self._authorize(row, normalized if present else {})
            if not present and row.inventory_valid:
                row.inventory_valid = False
                row.revision += 1
        if not accepted:
            self.complete = False
        return accepted

    def _reason(self, row, *, pending=True, uncertainty=True):
        if not self._active() or not self.provider._rest_valid:
            return "source-unavailable"
        if not self._fresh_inventory(row):
            return "group-inventory-stale"
        if row.denied:
            return "permission-denied"
        if row.authorization != "allowed":
            return "group-ownership-unverified"
        if self.provider._monotonic() < row.retry_at:
            return "retry-later"
        if self._observed(row) is None:
            return "group-state-unavailable"
        if pending and row.pending:
            return "command-pending"
        if uncertainty and row.uncertain:
            return "outcome-unknown"
        return None

    async def refresh(self, locations, ownership, *, report_limit=4):
        """Only complete validated location/member ownership input is authoritative.

        Ownership keys are (raw location ID, canonical decimal REST ID, exact
        member kind); only literal True qualifies. The caller must omit duplicate
        or ambiguous source rows rather than resolving them by order.
        """
        if not self._active() or self._refreshing:
            return
        self._refreshing = True
        current = asyncio.current_task()
        self._tasks.add(current)
        try:
            if (
                type(locations) is not dict
                or len(locations) > MAX_LOCATIONS
                or type(ownership) is not dict
                or len(ownership) > MAX_TOTAL_MEMBERS
            ):
                raise GroupSchemaError("group-response-limit")
            for location_id, name in locations.items():
                identifier(location_id)
                if type(name) is not str or len(name) > 4096:
                    raise GroupSchemaError("invalid-group-location")
            # Ownership is refreshed even when a subsequent group GET fails.
            self.update_ownership(ownership, locations=locations)
            context_revision = self._context_revision
            ownership = dict(self._context_ownership)
            all_valid = True
            ordered = sorted(locations.items())
            if ordered:
                offset = self._refresh_cursor % len(ordered)
                ordered = ordered[offset:] + ordered[:offset]
            pending = iter(ordered)
            completed, attempted = set(), set()

            async def worker():
                nonlocal all_valid
                while self._active():
                    item = next(pending, None)
                    if item is None:
                        return
                    location_id, name = item
                    attempted.add(location_id)
                    if self.provider._monotonic() < self._list_retry.get(location_id, 0):
                        self._invalidate_location(location_id)
                        all_valid = False
                        completed.add(location_id)
                        continue
                    try:
                        async with asyncio.timeout(READ_TIMEOUT_SECONDS):
                            payload = await self.gateway.read_light_groups(location_id, timeout=10)
                        if context_revision != self._context_revision or not self._active():
                            all_valid = False
                            completed.add(location_id)
                            continue
                        parsed = parse_groups(payload, location_id)
                        retained = [
                            row
                            for row in self._groups.values()
                            if row.inventory.location_id != location_id
                        ]
                        if (
                            len(retained) + len(parsed) > MAX_GROUPS
                            or sum(len(row.inventory.members) for row in retained)
                            + sum(len(row.members) for row in parsed)
                            > MAX_TOTAL_MEMBERS
                        ):
                            raise GroupSchemaError("group-response-limit")
                        self._replace_location(location_id, name, parsed, ownership)
                        self._list_retry.pop(location_id, None)
                    except (GroupSchemaError, RingJsonError, TimeoutError) as error:
                        if context_revision == self._context_revision:
                            self._invalidate_location(location_id)
                            self._list_retry[location_id] = self.provider._monotonic() + _backoff(
                                error
                            )
                        all_valid = False
                    completed.add(location_id)

            try:
                # Two workers share the existing transport budget; one failed
                # location cannot serialize all healthy locations behind it.
                async with (
                    asyncio.timeout(DISCOVERY_TIMEOUT_SECONDS),
                    asyncio.TaskGroup() as workers,
                ):
                    for _ in range(min(2, len(ordered))):
                        workers.create_task(worker())
            except TimeoutError:
                all_valid = False
            finally:
                self._refresh_cursor += len(attempted)
            if context_revision == self._context_revision:
                for location_id in set(locations) - completed:
                    self._invalidate_location(location_id)
                    all_valid = False
                for key, row in tuple(self._groups.items()):
                    if row.inventory.location_id not in locations:
                        self._groups.pop(key)
                self._list_retry = {
                    key: value for key, value in self._list_retry.items() if key in locations
                }
            else:
                all_valid = False
            self.complete = all_valid
            await self.refresh_reports(limit=report_limit)
        except (GroupSchemaError, TimeoutError):
            self.complete = False
            for row in self._groups.values():
                row.inventory_valid = False
                row.revision += 1
        finally:
            self._refreshing = False
            self._tasks.discard(current)

    def _authorize(self, row, ownership):
        allowed = bool(row.inventory.members) and all(
            ownership.get((row.inventory.location_id, member.api_id, member.kind)) is True
            for member in row.inventory.members
        )
        authorization = "allowed" if allowed else "unknown"
        if row.authorization != authorization:
            row.authorization = authorization
            row.revision += 1

    def _replace_location(self, location_id, name, inventories, ownership):
        wanted = {self.identity(inventory).key for inventory in inventories}
        for key, row in tuple(self._groups.items()):
            if row.inventory.location_id == location_id and key not in wanted:
                self._groups.pop(key)
        for inventory in inventories:
            key = self.identity(inventory).key
            row = self._groups.get(key)
            if row is None or row.inventory.membership != inventory.membership:
                self._next_epoch += 1
                row = _Group(inventory, name, self._next_epoch, 1, self.provider._monotonic())
                self._groups[key] = row
            else:
                if (
                    row.inventory != inventory
                    or row.location_name != name
                    or not row.inventory_valid
                ):
                    row.revision += 1
                row.inventory, row.location_name = inventory, name
                row.inventory_at, row.inventory_valid = self.provider._monotonic(), True
            self._authorize(row, ownership)

    def _invalidate_location(self, location_id):
        for row in self._groups.values():
            if row.inventory.location_id == location_id:
                row.inventory_valid = False
                row.revision += 1

    async def refresh_reports(self, *, limit=4):
        if type(limit) is not int or not 0 <= limit <= 16:
            raise ValueError("Invalid group report budget")
        keys = sorted(self._groups)
        if not keys or not self._active():
            return
        candidates = [
            keys[(self._poll_cursor + index) % len(keys)] for index in range(min(limit, len(keys)))
        ]
        self._poll_cursor = (self._poll_cursor + len(candidates)) % len(keys)
        for key in candidates:
            row = self._groups.get(key)
            if (
                row is not None
                and self._fresh_inventory(row)
                and not row.pending
                and self.provider._monotonic() >= row.retry_at
            ):
                await self.read_report(key)

    async def read_report(self, key):
        existing = self._reads.get(key)
        if existing is not None:
            return await asyncio.shield(existing)
        row = self._groups.get(key)
        if row is None or not self._active():
            return False
        task = asyncio.create_task(
            self._read_report(key, row, row.serial, self.reservation.revision)
        )
        self._reads[key] = task

        def retire(done):
            if self._reads.get(key) is done:
                self._reads.pop(key)

        task.add_done_callback(retire)
        try:
            return await asyncio.shield(task)
        finally:
            if task.done() and self._reads.get(key) is task:
                self._reads.pop(key)

    async def _read_report(self, key, row, serial, lighting_revision):
        try:
            payload = await self.gateway.read_light_group_report(
                row.inventory.location_id, row.inventory.group_id, timeout=10
            )
            report = parse_report(payload, row.inventory)
            if (
                not self._active()
                or self._groups.get(key) is not row
                or row.serial != serial
                or self.reservation.revision != lighting_revision
            ):
                return False
            if row.report != report or row.error is not None:
                row.revision += 1
            row.report, row.reported_at = report, self.provider._clock()
            row.report_clock, row.error = self.provider._monotonic(), None
            row.report_lighting_revision = lighting_revision
            if report.on is not None:
                row.readback_revision += 1
                row.uncertain = False
            return True
        except (GroupSchemaError, RingJsonError, TimeoutError) as error:
            if self._groups.get(key) is row and row.serial == serial:
                row.error = "group-report-unavailable"
                row.revision += 1
                row.retry_at = self.provider._monotonic() + _backoff(error)
            return False

    def records(self):
        output = []
        for row in self._groups.values():
            on = self._observed(row)
            observation = DeviceObservation(
                "light_on",
                on,
                row.reported_at if on is not None else None,
                "current" if on is not None else "unknown",
                "ring-group-report",
            )
            output.append(
                DeviceRecord(
                    self.identity(row.inventory),
                    self.provider.account_generation,
                    self.provider.provider_generation,
                    row.inventory.name,
                    "lighting-group",
                    "light-group",
                    model="Ring Light Group",
                    capabilities=("switch",),
                    capability_evidence=("ring-light-group-schema",),
                    observations=(observation,),
                    source_health="online" if on is not None else "stale",
                    unavailable_reason=None if on is not None else "group-state-unavailable",
                    location_name=row.location_name,
                    supported=True,
                )
            )
        return tuple(sorted(output, key=lambda row: row.key))

    def snapshots(self):
        output = []
        for key, row in sorted(self._groups.items()):
            reason = self._reason(row)
            output.append(
                {
                    "device_id": key,
                    "location_id": self.identity(row.inventory).location_key,
                    "epoch": row.epoch,
                    "revision": row.revision,
                    "readback_revision": row.readback_revision,
                    "on": self._observed(row),
                    "authorization": "denied" if row.denied else row.authorization,
                    "can_set": reason is None,
                    "reason": reason,
                    "pending": row.pending,
                    "outcome_unknown": row.uncertain,
                    "requested_on": row.target if row.pending else None,
                }
            )
        return tuple(output)

    def details(self, key):
        row = self._groups.get(key)
        if row is None:
            return None
        # API identifiers are intentionally absent from the owner projection.
        reported = (
            dict(row.report.members) if row.report and self._observed(row) is not None else {}
        )
        return {
            "member_count": len(row.inventory.members),
            "members": [
                {"name": member.name, "kind": member.kind, "on": reported.get(member.api_id)}
                for member in row.inventory.members
            ],
            "aggregate_on": self._observed(row),
            "semantics": "group-reported-state",
        }

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
        def result(status, code, *, sent=False):
            current = self._groups.get(device_id) if type(device_id) is str else None
            return {
                "status": status,
                "code": code,
                "possibly_sent": sent,
                "on": self._observed(current)
                if current and current.report_lighting_revision == self.reservation.revision
                else None,
            }

        if (
            type(device_id) is not str
            or type(target) is not bool
            or not valid_generation(expected_revision)
            or not valid_generation(expected_epoch)
            or (
                target
                and (type(duration_seconds) is not int or duration_seconds not in ON_DURATIONS)
            )
            or (not target and duration_seconds is not None)
        ):
            return result("rejected", "invalid-request")
        row = self._groups.get(device_id)
        if row is None:
            return result("rejected", "light-unavailable")

        def allowed():
            try:
                return callable(admission) and admission() is True
            except Exception:
                return False

        if not allowed():
            return result("rejected", "policy-denied")
        reason = self._reason(row)
        if reason or row.revision != expected_revision or row.epoch != expected_epoch:
            return result("rejected", reason or "state-changed")
        token = self.reservation.try_acquire()
        if token is None:
            return result("busy", "lighting-busy")
        sent = False
        row.pending, row.target = True, target
        row.serial += 1
        current_task = asyncio.current_task()
        self._tasks.add(current_task)

        def check():
            if (
                not allowed()
                or self._groups.get(device_id) is not row
                or row.epoch != expected_epoch
                or not self.reservation.valid(token)
                or self._reason(row, pending=False) is not None
            ):
                raise RingJsonError("policy-denied")
            if not sent and row.revision != expected_revision:
                raise RingJsonError("state-changed")
            return True

        def admitted():
            nonlocal sent
            check()
            if not self.reservation.mark_sent(token):
                raise RingJsonError("policy-denied")
            row.own_lighting_revision = self.reservation.revision
            sent = True

        try:
            async with asyncio.timeout(self.timeout):
                await self.provider._publish()
                receipt = await self.gateway.set_light_group(
                    row.inventory.location_id,
                    row.inventory.group_id,
                    target,
                    duration_seconds=duration_seconds,
                    admission=check,
                    on_admitted=admitted,
                    timeout=self.timeout,
                )
                if not receipt.accepted:
                    if receipt.code == "permission-denied":
                        row.denied = True
                    if type(receipt.retry_after) in (int, float) and math.isfinite(
                        receipt.retry_after
                    ):
                        row.retry_at = self.provider._monotonic() + min(
                            3600, max(0, receipt.retry_after)
                        )
                    row.uncertain = bool(sent and receipt.outcome != "rejected")
                    return result(
                        "uncertain" if row.uncertain else "rejected", receipt.code, sent=sent
                    )
                # Drain a GET begun before this POST; it cannot prove the write.
                existing = self._reads.get(device_id)
                if existing is not None:
                    await asyncio.shield(existing)
                    if self._reads.get(device_id) is existing:
                        self._reads.pop(device_id)
                for _ in range(2):
                    check()
                    before = row.readback_revision
                    await self.read_report(device_id)
                    if (
                        allowed()
                        and self._groups.get(device_id) is row
                        and self.reservation.valid(token)
                        and self._reason(row, pending=False, uncertainty=False) is None
                        and row.readback_revision > before
                        and self._observed(row) is target
                    ):
                        return result("confirmed", "observed", sent=True)
                row.uncertain = True
                return result("uncertain", "outcome-unknown", sent=True)
        except asyncio.CancelledError:
            row.uncertain |= sent
            raise
        except (RingJsonError, TimeoutError):
            row.uncertain |= sent
            return result(
                "uncertain" if sent else "rejected",
                "outcome-unknown" if sent else "source-unavailable",
                sent=sent,
            )
        finally:
            row.pending, row.target = False, None
            row.own_lighting_revision = None
            self.reservation.release(token)
            self._tasks.discard(current_task)
            await self.provider._publish()

    async def close(self):
        self._closed = True
        tasks = (self._tasks | set(self._reads.values())) - {asyncio.current_task()}
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._reads.clear()


def _backoff(error):
    retry = getattr(error, "retry_after", None)
    if type(retry) in (int, float) and math.isfinite(retry) and retry >= 0:
        return min(3600, max(30, retry))
    return (
        300
        if getattr(error, "code", None) in {"permission-denied", "authentication-required"}
        else 30
    )
