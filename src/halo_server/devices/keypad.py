"""Shared owner/HomeKit admission for keypad brightness and local policy locks.

Observed levels stay separate from requests. A persisted lock revokes unsent
brightness commands without waiting for a pending physical operation.
"""

from __future__ import annotations

import asyncio
import threading
from collections import OrderedDict
from uuid import UUID

from halo_server.account_scope import AccountScope
from halo_server.api.admin import AdminProtocolError
from halo_server.devices.keypad_policy import (
    KeypadPolicy,
    KeypadPolicyConflict,
    KeypadPolicyError,
    KeypadPolicyStore,
    qualified_keypad,
    valid_device_id,
)
from halo_server.devices.models import valid_generation
from halo_server.media.manager import owned_thread, settle_owned

POLICY_FIELDS = frozenset(
    {"profile_id", "account_generation", "device_id", "expected_revision", "enabled", "locked"}
)
LOCK_FIELDS = frozenset(
    {"profile_id", "account_generation", "device_id", "expected_policy_revision", "locked"}
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


def valid_brightness(value):
    return type(value) is int and 0 <= value <= 100


class KeypadCoordinator:
    def __init__(self, monitor):
        self.monitor, self.service = monitor, monitor.service
        self.store = KeypadPolicyStore(
            self.service.paths.state_directory / "devices" / "keypad.json"
        )
        self.policy = KeypadPolicy()
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
        self._nonzero = {}

    async def load(self):
        if self._loaded:
            return
        self._lease.clear()
        try:
            self.policy = await owned_thread(self.store.load)
            self._loaded, self._error = True, False
            self._lease = threading.Event()
            self._lease.set()
        except KeypadPolicyError:
            self._error = True

    def invalidate(self):
        self._lease.clear()
        self._rows = {}
        self._binding = None
        self._uncertain = {}
        self._nonzero = {}

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
                    row.get(key) is not None and not valid_brightness(row[key])
                    for key in ("brightness", "requested_brightness")
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
            self._nonzero = {}
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
            observed = self._row(source["device_id"])
            detail = (observed or {}).get("observation_details", {}).get("keypad_brightness", {})
            if (
                observed
                and observed.get("source_health") == "online"
                and detail.get("status") == "current"
                and valid_brightness(source.get("brightness"))
                and source["brightness"] > 0
                and detail.get("value") == source["brightness"]
            ):
                self._nonzero[source["device_id"]] = source["brightness"]
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
        if row.get("kind") != "keypad":
            return None
        binding = self.monitor._binding
        source = self._rows.get(row["id"], {})
        current = bool(
            binding is not None
            and binding is self._binding
            and self.monitor._current(binding)
            and source.get("location_id") == row["location_id"]
        )
        preference = (
            self.policy.preference(binding.scope.profile_id, row["id"]) if binding else None
        )
        # Stable authority must not disappear while its own lock write persists.
        enabled = bool(preference and preference.enabled and self._loaded and not self._error)
        locked = preference.locked if preference and self._loaded and not self._error else True
        detail = row.get("observation_details", {}).get("keypad_brightness", {})
        brightness = (
            source.get("brightness")
            if (
                current
                and row.get("source_health") == "online"
                and detail.get("status") == "current"
                and valid_brightness(detail.get("value"))
                and source.get("brightness") == detail.get("value")
            )
            else None
        )
        fresh = valid_brightness(brightness)
        qualified = self._qualified(row["id"])
        key = (binding.scope if binding else None, row["id"])
        pending, uncertain = self._pending.get(key), self._uncertain.get(key)
        reason = (
            "keypad-policy-unavailable"
            if self._error
            else "controls-disabled"
            if not enabled
            else "unsupported-keypad"
            if not qualified
            else "brightness-locked"
            if locked
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
            and qualified
            and not locked
            and self._lease.is_set()
            and not self._configuring
            and fresh
            and not pending
            and not uncertain
            and source.get("authorization") == "allowed"
            and source.get("can_set")
            and not source.get("pending")
            and not source.get("outcome_unknown")
        )
        can_set_lock = bool(enabled and binding and self.monitor._current(binding) and qualified)
        return {
            "policy_revision": self.policy.revision,
            "enabled": enabled,
            "locked": locked,
            "provider_generation": binding.generation if binding else 0,
            "epoch": source.get("epoch", 0) if current else 0,
            "observed_revision": source.get("revision", 0) if current else 0,
            "brightness": brightness,
            # This is a requested On target, never an observation of illumination.
            "on_brightness": brightness
            if fresh and brightness > 0
            else self._nonzero.get(row["id"], 100),
            "authorization": source.get("authorization", "unknown") if current else "unknown",
            "can_set": can_set,
            "can_set_lock": can_set_lock,
            "reason": reason,
            "pending": bool(pending or current and source.get("pending")),
            "outcome_unknown": bool(uncertain or current and source.get("outcome_unknown")),
            "requested_brightness": pending["target"]
            if pending
            else uncertain[1]
            if uncertain
            else source.get("requested_brightness")
            if current
            else None,
        }

    def _qualified(self, device_id):
        return any(
            record.key == device_id and qualified_keypad(record)
            for record in (
                self.monitor._catalog.records(present_only=True) if self.monitor._catalog else ()
            )
        )

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
            raise AdminProtocolError("bad_params", "Keypad account identity is invalid") from error
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
        locked,
    ):
        if type(enabled) is not bool:
            raise AdminProtocolError("bad_params", "Keypad permission must be a boolean")
        return await self._configure(
            profile_id=profile_id,
            account_generation=account_generation,
            device_id=device_id,
            expected_revision=expected_revision,
            enabled=enabled,
            locked=locked,
        )

    async def set_lock(
        self,
        *,
        profile_id,
        account_generation,
        device_id,
        expected_policy_revision,
        locked,
        admission=lambda: True,
    ):
        return await self._configure(
            profile_id=profile_id,
            account_generation=account_generation,
            device_id=device_id,
            expected_revision=expected_policy_revision,
            enabled=None,
            locked=locked,
            admission=admission,
        )

    async def _configure(
        self,
        *,
        profile_id,
        account_generation,
        device_id,
        expected_revision,
        enabled,
        locked,
        admission=lambda: True,
    ):
        if (
            not valid_device_id(device_id)
            or not valid_generation(expected_revision, zero=True)
            or enabled is not None
            and type(enabled) is not bool
            or type(locked) is not bool
        ):
            raise AdminProtocolError("bad_params", "Keypad preferences are invalid")
        async with self.service._session_change_lock, self._lock:
            scope = self._scope(profile_id, account_generation)
            await self.load()
            if not self._loaded or self._error:
                raise AdminProtocolError(
                    "keypad_policy_unavailable", "Keypad preferences need repair"
                )
            if self.policy.revision != expected_revision or not admission():
                raise AdminProtocolError("configuration_conflict", "Keypad preferences changed")
            if enabled is None:
                row = self._row(device_id)
                view = self.view(row) if row else None
                if not view or not view["can_set_lock"]:
                    raise AdminProtocolError("policy_denied", "Keypad controls are disabled")
                enabled = True
            if enabled and not self._qualified(device_id):
                raise AdminProtocolError(
                    "unsupported", "A qualified current Ring keypad is required"
                )
            self._configuring = True
            # No await between revoking an already captured lease and scheduling persistence.
            # Lock changes never acquire or wait for the physical command lane.
            self._lease.clear()
            self.monitor.bridge.publisher.notify()
            try:
                self.policy = await owned_thread(
                    self.store.configure,
                    scope.profile_id,
                    device_id,
                    enabled,
                    locked,
                    expected_revision=expected_revision,
                )
                self._loaded, self._error = True, False
            except KeypadPolicyConflict as error:
                self._loaded = False
                await self.load()
                raise AdminProtocolError(
                    "configuration_conflict", "Keypad preferences changed"
                ) from error
            except (KeypadPolicyError, asyncio.CancelledError) as error:
                self._loaded = False
                self._error = True
                if isinstance(error, asyncio.CancelledError):
                    raise
                raise AdminProtocolError(
                    "keypad_policy_unavailable", "Reload keypad preferences before retrying"
                ) from error
            finally:
                self._configuring = False
                if self._loaded and not self._error:
                    self._lease = threading.Event()
                    self._lease.set()
                self.monitor.bridge.publisher.notify()
            preference = self.policy.preference(profile_id, device_id)
            return {
                "enabled": preference.enabled,
                "locked": preference.locked,
                "policy_revision": self.policy.revision,
            }

    async def set_brightness(
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
            or not valid_brightness(target)
            or not valid_generation(provider_generation)
            or any(
                not valid_generation(value, zero=True)
                for value in (expected_epoch, expected_revision, expected_policy_revision)
            )
        ):
            raise AdminProtocolError("bad_params", "Keypad request is invalid")
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
                    "operation_conflict", "Keypad operation ID was already used"
                )
            return await asyncio.shield(old[1])
        self._prune_operations()
        if len(self._operations) >= 256:
            raise AdminProtocolError("busy", "Keypad operation history is full for this session")
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
                "configuration_conflict", "Keypad state changed; refresh before trying again"
            )
        key = (scope, device_id)
        if key in self._pending:
            raise AdminProtocolError("busy", "Another keypad command is pending for this device")
        if not view["can_set"]:
            return {
                "status": "rejected",
                "code": view["reason"] or "controls-unavailable",
                "possibly_sent": False,
                "brightness": view["brightness"],
            }
        lease = self._lease

        def admitted():
            return bool(
                lease.is_set()
                and self.monitor._current(binding)
                and admission()
                and self.policy.revision == expected_policy_revision
                and self.allows(scope.profile_id, device_id)
                and not self.policy.preference(scope.profile_id, device_id).locked
            )

        async def execute():
            try:

                async def dispatch():
                    if not admitted():
                        return {
                            "status": "rejected",
                            "code": "superseded",
                            "possibly_sent": False,
                            "brightness": None,
                        }
                    return await binding.provider.set_keypad_brightness(
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
                        "brightness": None,
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
                    "brightness": None,
                }
            finally:
                self._pending.pop(key, None)
                self.monitor.bridge.publisher.notify()

        self._pending[key] = {"target": target, "operation_id": operation_id}
        task = asyncio.create_task(execute(), name="keypad-operation")
        self._operations[operation_id] = (fingerprint, task)
        self.monitor.bridge.publisher.notify()
        return await asyncio.shield(task)

    async def close(self):
        self.invalidate()
        for _fingerprint, task in tuple(self._operations.values()):
            await settle_owned(task)
