"""Owner admission for Chime settings and explicit sound tests.

One coordinator owns per-device commands; the provider owns transport admission
and confirmation. Chimes do not expose synthetic HomeKit action switches.
"""

from __future__ import annotations

import asyncio
import threading
from collections import OrderedDict
from uuid import UUID

from halo_server.account_scope import AccountScope
from halo_server.api.admin import AdminProtocolError
from halo_server.devices.chime_policy import (
    ChimePolicy,
    ChimePolicyConflict,
    ChimePolicyError,
    ChimePolicyStore,
    qualified_chime,
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
        "sound_tests_enabled",
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
        "action",
        "value",
        "operation_id",
    }
)


def valid_action(action, value):
    return bool(
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


class ChimeCoordinator:
    def __init__(self, monitor):
        self.monitor, self.service = monitor, monitor.service
        self.store = ChimePolicyStore(self.service.paths.state_directory / "devices" / "chime.json")
        self.policy = ChimePolicy()
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
        except ChimePolicyError:
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
                or row.get("volume") is not None
                and (type(row["volume"]) is not int or not 0 <= row["volume"] <= 11)
                or row.get("snooze_seconds_left") is not None
                and (
                    type(row["snooze_seconds_left"]) is not int
                    or not 0 <= row["snooze_seconds_left"] <= 2_147_483_647
                )
                or row.get("automatic_nightlight") is not None
                and type(row["automatic_nightlight"]) is not bool
                or row.get("requested_action") is not None
                and (
                    not isinstance(row["requested_action"], str)
                    or row["requested_action"]
                    not in {"volume", "test", "snooze", "nightlight", "sound_stopped"}
                )
                or not isinstance(row.get("authorization"), str)
                or row["authorization"] not in {"allowed", "denied", "unknown"}
                or row.get("reason") is not None
                and (not isinstance(row["reason"], str) or not 1 <= len(row["reason"]) <= 128)
                or any(
                    type(row.get(key)) is not bool
                    for key in ("can_set", "pending", "outcome_unknown", "sound_active")
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
            scope, device, generation, epoch, revision, _policy, _action, _value = fingerprint
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
        if row.get("kind") != "chime":
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
        fresh = bool(
            current
            and row.get("source_health") == "online"
            and details.get("connected", {}).get("status") == "current"
            and details["connected"].get("value") is True
        )
        key = (binding.scope if binding else None, row["id"])
        pending = self._pending.get(key)
        uncertain = self._uncertain.get(key)
        reason = (
            "chime-policy-unavailable"
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
            "volume": source.get("volume") if fresh else None,
            "snooze_seconds_left": source.get("snooze_seconds_left") if fresh else None,
            "automatic_nightlight": source.get("automatic_nightlight") if fresh else None,
            "sound_active": bool(current and source.get("sound_active")),
            "sound_tests_enabled": bool(
                binding
                and self.policy.preference(binding.scope.profile_id, row["id"]).sound_tests_enabled
            ),
            "authorization": source.get("authorization", "unknown") if current else "unknown",
            "can_set": can_set,
            "reason": reason,
            "pending": bool(pending or current and source.get("pending")),
            "outcome_unknown": bool(uncertain or current and source.get("outcome_unknown")),
            "requested_action": pending["action"]
            if pending
            else uncertain[1]
            if uncertain
            else source.get("requested_action")
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
            raise AdminProtocolError("bad_params", "Chime account identity is invalid") from error
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
        sound_tests_enabled,
    ):
        if (
            not valid_device_id(device_id)
            or not valid_generation(expected_revision, zero=True)
            or type(enabled) is not bool
            or type(sound_tests_enabled) is not bool
            or sound_tests_enabled
            and not enabled
        ):
            raise AdminProtocolError("bad_params", "Chime preferences are invalid")
        async with self.service._session_change_lock, self._lock:
            scope = self._scope(profile_id, account_generation)
            await self.load()
            if not self._loaded or self._error:
                raise AdminProtocolError(
                    "chime_policy_unavailable", "Chime preferences need repair"
                )
            if enabled and not any(
                record.key == device_id and qualified_chime(record)
                for record in (
                    self.monitor._catalog.records(present_only=True)
                    if self.monitor._catalog
                    else ()
                )
            ):
                raise AdminProtocolError(
                    "unsupported", "A qualified current Ring Chime is required"
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
                    sound_tests_enabled,
                    expected_revision=expected_revision,
                )
                self._loaded, self._error = True, False
            except ChimePolicyConflict as error:
                self._loaded = False
                await self.load()
                raise AdminProtocolError(
                    "configuration_conflict", "Chime preferences changed"
                ) from error
            except (ChimePolicyError, asyncio.CancelledError) as error:
                self._loaded = False
                self._error = True
                if isinstance(error, asyncio.CancelledError):
                    raise
                raise AdminProtocolError(
                    "chime_policy_unavailable", "Reload chime preferences before retrying"
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
                "sound_tests_enabled": self.policy.preference(
                    profile_id, device_id
                ).sound_tests_enabled,
            }

    async def set_chime(
        self,
        *,
        profile_id,
        account_generation,
        device_id,
        provider_generation,
        expected_epoch,
        expected_revision,
        expected_policy_revision,
        action,
        value,
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
            or not valid_action(action, value)
            or not valid_generation(provider_generation)
            or any(
                not valid_generation(value, zero=True)
                for value in (expected_epoch, expected_revision, expected_policy_revision)
            )
        ):
            raise AdminProtocolError("bad_params", "Chime request is invalid")
        scope = self._scope(profile_id, account_generation)
        fingerprint = (
            scope,
            device_id,
            provider_generation,
            expected_epoch,
            expected_revision,
            expected_policy_revision,
            action,
            value,
        )
        old = self._operations.get(operation_id)
        if old:
            if old[0] != fingerprint:
                raise AdminProtocolError(
                    "operation_conflict", "Chime operation ID was already used"
                )
            return await asyncio.shield(old[1])
        self._prune_operations()
        if len(self._operations) >= 256:
            raise AdminProtocolError("busy", "Chime operation history is full for this session")
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
                "configuration_conflict", "Chime state changed; refresh before trying again"
            )
        key = (scope, device_id)
        if key in self._pending:
            raise AdminProtocolError("busy", "Another chime command is pending for this device")
        acknowledging = bool(
            action == "sound_stopped"
            and view["sound_active"]
            and view["enabled"]
            and view["authorization"] == "allowed"
            and not view["pending"]
            and self._lease.is_set()
        )
        if (
            not view["can_set"]
            and not acknowledging
            or action == "test"
            and not view["sound_tests_enabled"]
        ):
            return {
                "status": "rejected",
                "code": view["reason"] or "controls-unavailable",
                "possibly_sent": False,
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
                        }
                    return await binding.provider.set_chime(
                        device_id,
                        action,
                        value,
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
                    }
                return result
            except (Exception, asyncio.CancelledError):
                # This owned task is shielded from its caller. Cancellation of
                # the Ring-loop future is a lost result, not proof of no send.
                if self.monitor._current(binding):
                    source = self._rows.get(device_id, {})
                    self._uncertain[key] = (
                        source.get("readback_revision", 0),
                        action,
                        value,
                    )
                return {
                    "status": "uncertain",
                    "code": "result-unavailable",
                    "possibly_sent": True,
                }
            finally:
                self._pending.pop(key, None)
                self.monitor.bridge.publisher.notify()

        self._pending[key] = {"action": action, "operation_id": operation_id}
        task = asyncio.create_task(execute(), name="chime-operation")
        self._operations[operation_id] = (fingerprint, task)
        self.monitor.bridge.publisher.notify()
        return await asyncio.shield(task)

    async def close(self):
        self.invalidate()
        for _fingerprint, task in tuple(self._operations.values()):
            await settle_owned(task)
