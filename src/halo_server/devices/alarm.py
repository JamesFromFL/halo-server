"""Owner and HomeKit admission for normal Ring Alarm mode changes.

One coordinator owns location-wide commands. The provider owns actual transport
admission and confirmation. Import preferences never imply control permission.
"""

from __future__ import annotations

import asyncio
import threading
from collections import OrderedDict
from uuid import UUID

from halo_server.account_scope import AccountScope
from halo_server.api.admin import AdminProtocolError
from halo_server.devices.alarm_policy import (
    AlarmPolicy,
    AlarmPolicyConflict,
    AlarmPolicyError,
    AlarmPolicyStore,
    valid_device_id,
)
from halo_server.devices.models import valid_generation
from halo_server.media.manager import owned_thread, settle_owned

MODES = frozenset({"disarmed", "home", "away"})
PHASES = frozenset({"idle", "entry-delay", "exit-delay", "alarming"})
SIGNALS = frozenset(
    {"none", "burglar", "fire", "carbon-monoxide", "fire-or-carbon-monoxide", "panic"}
)
TARGET_REASONS = frozenset(
    {
        "source-unavailable",
        "connection-superseded",
        "panel-offline",
        "multiple-panels",
        "control-unavailable",
        "panel-asset-unsupported",
        "panel-stale",
        "outcome-unknown",
        "transition-active",
        "sensors-require-bypass",
        "faulted-device-list-unknown",
        "command-pending",
        "authorization-denied",
        "authorization-unknown",
        "cellular-backup",
        "panel-inventory-incomplete",
        "life-safety-active",
        "mode-unknown",
        "alarm-active",
        "panel-state-unknown",
        "faulted-device-list-invalid",
    }
)
POLICY_FIELDS = frozenset(
    {"profile_id", "account_generation", "location_id", "expected_revision", "enabled"}
)
MODE_FIELDS = frozenset(
    {
        "profile_id",
        "account_generation",
        "device_id",
        "provider_generation",
        "expected_epoch",
        "expected_revision",
        "expected_policy_revision",
        "target",
        "operation_id",
    }
)


class AlarmCoordinator:
    def __init__(self, monitor):
        self.monitor, self.service = monitor, monitor.service
        self.store = AlarmPolicyStore(self.service.paths.state_directory / "devices" / "alarm.json")
        self.policy = AlarmPolicy()
        self._loaded = False
        self._error = False
        self._configuring = False
        self._lock = asyncio.Lock()
        self._lease = threading.Event()
        self._rows = {}
        self._binding = None
        self._pending = {}
        self._operations = OrderedDict()
        self._uncertain = {}

    async def load(self):
        if self._loaded:
            return
        self._lease.clear()
        try:
            self.policy = await owned_thread(self.store.load)
            self._loaded, self._error = True, False
            self._lease = threading.Event()
            self._lease.set()
        except AlarmPolicyError:
            self._error = True

    def invalidate(self):
        self._lease.clear()
        self._rows = {}
        self._binding = None
        self._uncertain = {}

    def _revoke_source(self):
        self._lease.clear()
        self._rows = {}

    def receive(self, binding, rows):
        if not self.monitor._current(binding):
            return
        if not isinstance(rows, (tuple, list)) or len(rows) > 128:
            self._revoke_source()
            return
        indexed = {}
        for row in rows:
            if (
                not isinstance(row, dict)
                or not valid_device_id(row.get("device_id"))
                or not valid_device_id(row.get("location_id"))
                or row["device_id"] in indexed
                or any(
                    not valid_generation(row.get(key), zero=True) for key in ("epoch", "revision")
                )
                or any(
                    value is not None and (not isinstance(value, str) or value not in allowed)
                    for value, allowed in (
                        (row.get("mode"), MODES),
                        (row.get("requested_mode"), MODES),
                        (row.get("phase"), PHASES),
                        (row.get("signal"), SIGNALS),
                    )
                )
                or not isinstance(row.get("authorization"), str)
                or row["authorization"] not in {"allowed", "denied", "unknown"}
                or row.get("reason") is not None
                and (not isinstance(row["reason"], str) or not 1 <= len(row["reason"]) <= 128)
                or any(
                    type(row.get(key)) is not bool
                    for key in ("can_set_mode", "pending", "outcome_unknown")
                )
                or not valid_generation(row.get("readback_revision"), zero=True)
                or not isinstance(row.get("allowed_targets"), (list, tuple))
                or len(row["allowed_targets"]) > 3
                or any(
                    not isinstance(target, str) or target not in MODES
                    for target in row["allowed_targets"]
                )
                or len(set(row["allowed_targets"])) != len(row["allowed_targets"])
                or "target_reasons" in row
                and (
                    not isinstance(row["target_reasons"], dict)
                    or len(row["target_reasons"]) > 3
                    or any(
                        target not in MODES
                        or target in row["allowed_targets"]
                        or not isinstance(reason, str)
                        or reason not in TARGET_REASONS
                        for target, reason in row["target_reasons"].items()
                    )
                )
            ):
                self._revoke_source()
                return
            indexed[row["device_id"]] = dict(row)
            indexed[row["device_id"]]["target_reasons"] = dict(row.get("target_reasons", {}))
        if not indexed or any(
            identity not in indexed
            or indexed[identity]["epoch"] != old["epoch"]
            or old["authorization"] == "allowed"
            and indexed[identity]["authorization"] != "allowed"
            for identity, old in self._rows.items()
        ):
            self._lease.clear()
        if self._binding is not binding:
            self._operations = OrderedDict(
                (key, value) for key, value in self._operations.items() if not value[1].done()
            )
            self._binding = binding
        if (
            indexed
            and not self._lease.is_set()
            and self._loaded
            and not self._error
            and not self._configuring
        ):
            # Old captured leases stay revoked even after a valid resync.
            self._lease = threading.Event()
            self._lease.set()
        self._rows = indexed
        for source in indexed.values():
            key = (binding.scope, source["location_id"])
            uncertain = self._uncertain.get(key)
            if (
                uncertain
                and source["readback_revision"] > uncertain[0]
                and not source["outcome_unknown"]
            ):
                self._uncertain.pop(key, None)

    def _prune_operations(self):
        binding = self.monitor._binding
        for identifier, (fingerprint, task) in tuple(self._operations.items()):
            if not task.done() or binding is None:
                continue
            scope, device, generation, epoch, revision, _policy, _target = fingerprint
            source = self._rows.get(device)
            if (
                scope != binding.scope
                or generation != binding.generation
                or source is not None
                and (source["epoch"], source["revision"]) != (epoch, revision)
            ):
                # Provider state tokens never repeat within a binding. An old
                # request still fails CAS after its completed receipt is evicted.
                self._operations.pop(identifier, None)

    def allows(self, profile_id, location_id):
        return bool(
            self._loaded
            and not self._error
            and not self._configuring
            and self.policy.allows(profile_id, location_id)
        )

    def view(self, row):
        if row.get("kind") != "security-panel":
            return None
        binding = self.monitor._binding
        source = self._rows.get(row["id"], {})
        current = bool(
            binding is not None
            and binding is self._binding
            and self.monitor._current(binding)
            and source.get("location_id") == row["location_id"]
        )
        enabled = bool(binding and self.allows(binding.scope.profile_id, row["location_id"]))
        details = row.get("observation_details", {})
        fields = {
            target: source.get(target)
            if current
            and row.get("source_health") == "online"
            and details.get(name, {}).get("status") == "current"
            and details[name].get("value") is not None
            else None
            for target, name in (
                ("mode", "alarm_mode"),
                ("phase", "alarm_state"),
                ("signal", "alarm_signal"),
            )
        }
        fresh = all(value is not None for value in fields.values())
        key = (binding.scope if binding else None, row["location_id"])
        pending = self._pending.get(key)
        uncertain = self._uncertain.get(key)
        reason = (
            "alarm-policy-unavailable"
            if self._error
            else "controls-disabled"
            if not enabled
            else "observation-stale"
            if not fresh
            else "command-pending"
            if pending or source.get("pending")
            else "outcome-unknown"
            if uncertain
            else "authorization-unknown"
            if source.get("authorization") != "allowed" and not source.get("reason")
            else source.get("reason")
        )
        can_set = bool(
            enabled
            and self._lease.is_set()
            and fresh
            and not pending
            and not uncertain
            and source.get("authorization") == "allowed"
            and source.get("can_set_mode")
            and not source.get("pending")
            and not source.get("outcome_unknown")
            and source.get("allowed_targets")
        )
        return {
            "policy_revision": self.policy.revision,
            "enabled": enabled,
            "provider_generation": binding.generation if binding else 0,
            "epoch": source.get("epoch", 0) if current else 0,
            "observed_revision": source.get("revision", 0) if current else 0,
            **fields,
            "authorization": source.get("authorization", "unknown") if current else "unknown",
            "can_set_mode": can_set,
            "allowed_targets": list(source["allowed_targets"]) if can_set else [],
            "target_reasons": dict(source.get("target_reasons", {})) if current and fresh else {},
            "reason": reason,
            "pending": bool(pending or current and source.get("pending")),
            "outcome_unknown": bool(uncertain or current and source.get("outcome_unknown")),
            "requested_mode": pending["target"]
            if pending
            else uncertain[1]
            if uncertain
            else source.get("requested_mode")
            if current
            else None,
        }

    def _row(self, device_id):
        catalog = self.monitor._catalog
        if catalog is None:
            return None
        return next(
            (
                row
                for row in catalog.snapshot(now=self.monitor._clock(), max_age=360)["devices"]
                if row["id"] == device_id
            ),
            None,
        )

    def _scope(self, profile_id, account_generation):
        try:
            scope = AccountScope(profile_id, account_generation)
        except (TypeError, ValueError) as error:
            raise AdminProtocolError("bad_params", "Alarm account identity is invalid") from error
        if self.service._current_account_scope() != scope or self.monitor._context() is None:
            raise AdminProtocolError("account_changed", "The Ring account changed")
        if self.service._stop_requested.is_set():
            raise AdminProtocolError("service_stopping", "Halo Server is stopping")
        return scope

    async def configure(
        self, *, profile_id, account_generation, location_id, expected_revision, enabled
    ):
        if (
            not valid_device_id(location_id)
            or not valid_generation(expected_revision, zero=True)
            or type(enabled) is not bool
        ):
            raise AdminProtocolError("bad_params", "Alarm preferences are invalid")
        async with self.service._session_change_lock, self._lock:
            scope = self._scope(profile_id, account_generation)
            await self.load()
            if not self._loaded or self._error:
                raise AdminProtocolError(
                    "alarm_policy_unavailable", "Alarm preferences need repair"
                )
            if enabled and not any(
                record.identity.location_key == location_id
                and record.kind == "security-panel"
                and record.identity.provider == "ring-hub"
                for record in (
                    self.monitor._catalog.records(present_only=True)
                    if self.monitor._catalog
                    else ()
                )
            ):
                raise AdminProtocolError("unsupported", "A current Ring Alarm panel is required")
            self._configuring = True
            self._lease.clear()
            self.monitor.bridge.publisher.notify()
            try:
                self.policy = await owned_thread(
                    self.store.configure,
                    scope.profile_id,
                    location_id,
                    enabled,
                    expected_revision=expected_revision,
                )
                self._loaded, self._error = True, False
            except AlarmPolicyConflict as error:
                self._loaded = False
                await self.load()
                raise AdminProtocolError(
                    "configuration_conflict", "Alarm preferences changed"
                ) from error
            except (AlarmPolicyError, asyncio.CancelledError) as error:
                self._loaded = False
                self._error = True
                if isinstance(error, asyncio.CancelledError):
                    raise
                raise AdminProtocolError(
                    "alarm_policy_unavailable", "Reload alarm preferences before retrying"
                ) from error
            finally:
                self._configuring = False
                if self._loaded and not self._error:
                    self._lease = threading.Event()
                    self._lease.set()
                self.monitor.bridge.publisher.notify()
            return {
                "enabled": self.policy.allows(profile_id, location_id),
                "policy_revision": self.policy.revision,
            }

    async def set_mode(
        self,
        *,
        profile_id,
        account_generation,
        device_id,
        provider_generation,
        expected_epoch,
        expected_revision,
        expected_policy_revision,
        target,
        operation_id,
        admission=lambda: True,
    ):
        try:
            valid_operation = (
                isinstance(operation_id, str) and str(UUID(operation_id)) == operation_id
            )
        except ValueError:
            valid_operation = False
        if (
            not valid_operation
            or not valid_device_id(device_id)
            or not isinstance(target, str)
            or target not in MODES
            or not valid_generation(provider_generation)
            or any(
                not valid_generation(value, zero=True)
                for value in (expected_epoch, expected_revision, expected_policy_revision)
            )
        ):
            raise AdminProtocolError("bad_params", "Alarm mode request is invalid")
        scope = self._scope(profile_id, account_generation)
        fingerprint = (
            scope,
            device_id,
            provider_generation,
            expected_epoch,
            expected_revision,
            expected_policy_revision,
            target,
        )
        old = self._operations.get(operation_id)
        if old:
            if old[0] != fingerprint:
                raise AdminProtocolError(
                    "operation_conflict", "Alarm operation ID was already used"
                )
            return await asyncio.shield(old[1])
        self._prune_operations()
        if len(self._operations) >= 256:
            raise AdminProtocolError("busy", "Alarm operation history is full for this session")
        binding = self.monitor._binding
        row = self._row(device_id)
        view = self.view(row) if row else None
        if (
            binding is None
            or not self.monitor._current(binding)
            or binding.scope != scope
            or binding.generation != provider_generation
            or view is None
            or self.policy.revision != expected_policy_revision
            or (view["epoch"], view["observed_revision"]) != (expected_epoch, expected_revision)
            or not admission()
        ):
            raise AdminProtocolError(
                "configuration_conflict", "Alarm state changed; refresh before trying again"
            )
        key = (scope, row["location_id"])
        if key in self._pending:
            raise AdminProtocolError("busy", "Another alarm command is pending at this location")
        if not view["can_set_mode"]:
            return {
                "status": "rejected",
                "code": view["reason"] or "controls-unavailable",
                "possibly_sent": False,
                "mode": view["mode"],
            }
        if target not in view["allowed_targets"]:
            return {
                "status": "rejected",
                "code": "target-unavailable",
                "possibly_sent": False,
                "mode": view["mode"],
            }
        lease = self._lease

        def admitted():
            return bool(
                lease.is_set()
                and self.monitor._current(binding)
                and admission()
                and self.policy.revision == expected_policy_revision
            )

        async def execute():
            try:

                async def dispatch():
                    if not admitted():
                        return {
                            "status": "rejected",
                            "code": "superseded",
                            "possibly_sent": False,
                            "mode": None,
                        }
                    return await binding.provider.set_alarm_mode(
                        device_id,
                        target,
                        expected_revision=expected_revision,
                        expected_epoch=expected_epoch,
                        admission=admitted,
                    )

                result = await asyncio.wrap_future(binding.client.submit(dispatch()))
                if not self.monitor._current(binding):
                    return {
                        "status": "uncertain",
                        "code": "account-changed",
                        "possibly_sent": result.get("possibly_sent", True),
                        "mode": None,
                    }
                return result
            except (Exception, asyncio.CancelledError):
                # This owned task is shielded from its caller. Cancellation of
                # the Ring-loop future is a lost result, not proof of no send.
                if self.monitor._current(binding):
                    source = self._rows.get(device_id, {})
                    self._uncertain[key] = (
                        source.get("readback_revision", 0),
                        target,
                    )
                return {
                    "status": "uncertain",
                    "code": "result-unavailable",
                    "possibly_sent": True,
                    "mode": None,
                }
            finally:
                self._pending.pop(key, None)
                self.monitor.bridge.publisher.notify()

        self._pending[key] = {"target": target, "operation_id": operation_id}
        task = asyncio.create_task(execute(), name="alarm-mode-operation")
        self._operations[operation_id] = (fingerprint, task)
        self.monitor.bridge.publisher.notify()
        return await asyncio.shield(task)

    async def close(self):
        self.invalidate()
        for _fingerprint, task in tuple(self._operations.values()):
            await settle_owned(task)
