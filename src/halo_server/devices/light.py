"""Owner and HomeKit admission for individual Ring Smart Lighting commands.

One coordinator owns per-device commands. The provider owns actual transport
admission and confirmation. Import preferences never imply control permission.
"""

from __future__ import annotations

import asyncio
import threading
from collections import OrderedDict
from uuid import UUID

from halo_server.account_scope import AccountScope
from halo_server.api.admin import AdminProtocolError
from halo_server.devices.light_policy import (
    ON_DURATIONS,
    LightPolicy,
    LightPolicyConflict,
    LightPolicyError,
    LightPolicyStore,
    qualified_light,
    valid_device_id,
)
from halo_server.devices.models import valid_generation
from halo_server.media.manager import owned_thread, settle_owned

POLICY_FIELDS = frozenset(
    {
        "profile_id",
        "account_generation",
        "device_id",
        "expected_revision",
        "enabled",
        "on_duration_seconds",
    }
)
SET_FIELDS = frozenset(
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


class LightCoordinator:
    def __init__(self, monitor):
        self.monitor, self.service = monitor, monitor.service
        self.store = LightPolicyStore(self.service.paths.state_directory / "devices" / "light.json")
        self.policy = LightPolicy()
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
        except LightPolicyError:
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
        if not isinstance(rows, (tuple, list)) or len(rows) > 1024:
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
                    row.get(key) is not None and type(row[key]) is not bool
                    for key in ("on", "requested_on")
                )
                or not isinstance(row.get("authorization"), str)
                or row["authorization"] not in {"allowed", "denied", "unknown"}
                or row.get("reason") is not None
                and (not isinstance(row["reason"], str) or not 1 <= len(row["reason"]) <= 128)
                or any(
                    type(row.get(key)) is not bool
                    for key in ("can_set", "pending", "outcome_unknown")
                )
                or not valid_generation(row.get("readback_revision"), zero=True)
            ):
                self._revoke_source()
                return
            indexed[row["device_id"]] = dict(row)
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
            key = (binding.scope, source["device_id"])
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

    def allows(self, profile_id, device_id):
        return bool(
            self._loaded
            and not self._error
            and not self._configuring
            and self.policy.allows(profile_id, device_id)
        )

    def view(self, row):
        if row.get("kind") not in {"beams-light", "light-group"}:
            return None
        binding = self.monitor._binding
        source = self._rows.get(row["id"], {})
        current = bool(
            binding is not None
            and binding is self._binding
            and self.monitor._current(binding)
            and source.get("location_id") == row["location_id"]
        )
        enabled = bool(binding and self.allows(binding.scope.profile_id, row["id"]))
        details = row.get("observation_details", {})
        on = (
            source.get("on")
            if (
                current
                and row.get("source_health") == "online"
                and details.get("light_on", {}).get("status") == "current"
                and type(details["light_on"].get("value")) is bool
            )
            else None
        )
        fresh = type(on) is bool
        key = (binding.scope if binding else None, row["id"])
        pending = self._pending.get(key)
        uncertain = self._uncertain.get(key)
        reason = (
            "light-policy-unavailable"
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
            and source.get("can_set")
            and not source.get("pending")
            and not source.get("outcome_unknown")
        )
        return {
            "policy_revision": self.policy.revision,
            "enabled": enabled,
            "provider_generation": binding.generation if binding else 0,
            "epoch": source.get("epoch", 0) if current else 0,
            "observed_revision": source.get("revision", 0) if current else 0,
            "on": on,
            "on_duration_seconds": self.policy.preference(
                binding.scope.profile_id, row["id"]
            ).on_duration_seconds
            if binding
            else 60,
            "authorization": source.get("authorization", "unknown") if current else "unknown",
            "can_set": can_set,
            "reason": reason,
            "pending": bool(pending or current and source.get("pending")),
            "outcome_unknown": bool(uncertain or current and source.get("outcome_unknown")),
            "requested_on": pending["target"]
            if pending
            else uncertain[1]
            if uncertain
            else source.get("requested_on")
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
            raise AdminProtocolError("bad_params", "Light account identity is invalid") from error
        if self.service._current_account_scope() != scope or self.monitor._context() is None:
            raise AdminProtocolError("account_changed", "The Ring account changed")
        if self.service._stop_requested.is_set():
            raise AdminProtocolError("service_stopping", "Halo Server is stopping")
        return scope

    async def configure(
        self,
        *,
        profile_id,
        account_generation,
        device_id,
        expected_revision,
        enabled,
        on_duration_seconds,
    ):
        if (
            not valid_device_id(device_id)
            or not valid_generation(expected_revision, zero=True)
            or type(enabled) is not bool
            or type(on_duration_seconds) is not int
            or on_duration_seconds not in ON_DURATIONS
        ):
            raise AdminProtocolError("bad_params", "Light preferences are invalid")
        async with self.service._session_change_lock, self._lock:
            scope = self._scope(profile_id, account_generation)
            await self.load()
            if not self._loaded or self._error:
                raise AdminProtocolError(
                    "light_policy_unavailable", "Light preferences need repair"
                )
            if enabled and not any(
                record.key == device_id and qualified_light(record)
                for record in (
                    self.monitor._catalog.records(present_only=True)
                    if self.monitor._catalog
                    else ()
                )
            ):
                raise AdminProtocolError(
                    "unsupported", "A qualified current Ring Smart Lighting device is required"
                )
            self._configuring = True
            self._lease.clear()
            self.monitor.bridge.publisher.notify()
            try:
                self.policy = await owned_thread(
                    self.store.configure,
                    scope.profile_id,
                    device_id,
                    enabled,
                    on_duration_seconds,
                    expected_revision=expected_revision,
                )
                self._loaded, self._error = True, False
            except LightPolicyConflict as error:
                self._loaded = False
                await self.load()
                raise AdminProtocolError(
                    "configuration_conflict", "Light preferences changed"
                ) from error
            except (LightPolicyError, asyncio.CancelledError) as error:
                self._loaded = False
                self._error = True
                if isinstance(error, asyncio.CancelledError):
                    raise
                raise AdminProtocolError(
                    "light_policy_unavailable", "Reload light preferences before retrying"
                ) from error
            finally:
                self._configuring = False
                if self._loaded and not self._error:
                    self._lease = threading.Event()
                    self._lease.set()
                self.monitor.bridge.publisher.notify()
            return {
                "enabled": self.policy.allows(profile_id, device_id),
                "policy_revision": self.policy.revision,
                "on_duration_seconds": self.policy.preference(
                    profile_id, device_id
                ).on_duration_seconds,
            }

    async def set_light(
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
            or type(target) is not bool
            or not valid_generation(provider_generation)
            or any(
                not valid_generation(value, zero=True)
                for value in (expected_epoch, expected_revision, expected_policy_revision)
            )
        ):
            raise AdminProtocolError("bad_params", "Light request is invalid")
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
                    "operation_conflict", "Light operation ID was already used"
                )
            return await asyncio.shield(old[1])
        self._prune_operations()
        if len(self._operations) >= 256:
            raise AdminProtocolError("busy", "Light operation history is full for this session")
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
                "configuration_conflict", "Light state changed; refresh before trying again"
            )
        key = (scope, device_id)
        if key in self._pending:
            raise AdminProtocolError("busy", "Another light command is pending for this device")
        if not view["can_set"]:
            return {
                "status": "rejected",
                "code": view["reason"] or "controls-unavailable",
                "possibly_sent": False,
                "on": view["on"],
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
                            "on": None,
                        }
                    return await binding.provider.set_light(
                        device_id,
                        target,
                        expected_revision=expected_revision,
                        expected_epoch=expected_epoch,
                        duration_seconds=view["on_duration_seconds"] if target else None,
                        admission=admitted,
                    )

                result = await asyncio.wrap_future(binding.client.submit(dispatch()))
                if not self.monitor._current(binding):
                    return {
                        "status": "uncertain",
                        "code": "account-changed",
                        "possibly_sent": result.get("possibly_sent", True),
                        "on": None,
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
                    "on": None,
                }
            finally:
                self._pending.pop(key, None)
                self.monitor.bridge.publisher.notify()

        self._pending[key] = {"target": target, "operation_id": operation_id}
        task = asyncio.create_task(execute(), name="light-operation")
        self._operations[operation_id] = (fingerprint, task)
        self.monitor.bridge.publisher.notify()
        return await asyncio.shield(task)

    async def close(self):
        self.invalidate()
        for _fingerprint, task in tuple(self._operations.values()):
            await settle_owned(task)
