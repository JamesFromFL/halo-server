"""Durable sensor identities and import preferences with isolated HAP publication."""

from __future__ import annotations

import asyncio
import re

from halo_server.account_scope import AccountScope
from halo_server.api.admin import AdminProtocolError
from halo_server.devices.bridge_registry import (
    BridgeRegistry,
    BridgeRegistryConflictError,
    BridgeRegistryError,
    BridgeRegistryStore,
    bridge_projection,
)
from halo_server.media.manager import owned_thread, settle_owned

PREFERENCE_FIELDS = frozenset(
    {
        "enabled",
        "auto_import",
        "included_devices",
        "excluded_devices",
        "excluded_locations",
        "excluded_kinds",
        "excluded_mappings",
    }
)
_KEY = re.compile(r"[a-f0-9]{64}\Z")
_KIND = re.compile(r"[a-z][a-z0-9-]{0,63}\Z")
_MAPPINGS = {
    "contact",
    "motion",
    "leak",
    "smoke",
    "carbon_monoxide",
    "security_system",
    "lightbulb",
}


def validate_preferences(value):
    """Bound both local RPC and HTTP inputs before any storage operation."""
    if not isinstance(value, dict) or set(value) != PREFERENCE_FIELDS:
        raise ValueError("Invalid bridge preferences")
    if any(type(value[key]) is not bool for key in ("enabled", "auto_import")):
        raise ValueError("Invalid bridge preferences")
    for key, limit, valid in (
        ("included_devices", 512, lambda item: _KEY.fullmatch(item)),
        ("excluded_devices", 512, lambda item: _KEY.fullmatch(item)),
        ("excluded_locations", 128, lambda item: _KEY.fullmatch(item)),
        ("excluded_kinds", 64, lambda item: _KIND.fullmatch(item)),
        ("excluded_mappings", 7, lambda item: item in _MAPPINGS),
    ):
        items = value[key]
        if (
            not isinstance(items, (list, tuple))
            or len(items) > limit
            or any(not isinstance(item, str) or not valid(item) for item in items)
            or len(set(items)) != len(items)
        ):
            raise ValueError("Invalid bridge preferences")
    included, excluded = value["included_devices"], value["excluded_devices"]
    if len(included) + len(excluded) > 512 or set(included).intersection(excluded):
        raise ValueError("Invalid bridge preferences")
    return {
        key: tuple(sorted(item)) if isinstance(item, (list, tuple)) else item
        for key, item in value.items()
    }


class DeviceBridge:
    def __init__(self, service):
        self.service = service
        self.store = BridgeRegistryStore(service.paths.homekit_device_registry_file)
        self.registry = BridgeRegistry()
        self._loaded = False
        self._error = False
        self._configuring = False
        self._lock = asyncio.Lock()
        self._records = ()
        self._scope = None
        self._signature = None
        self._pending = None
        self._task = None
        from halo_server.devices.publication import DevicePublisher

        self.publisher = DevicePublisher(self)

    def _context(self):
        client = self.service._sessions.active_client
        scope = self.service._current_account_scope()
        return (
            scope
            if client is not None and client.is_authenticated and isinstance(scope, AccountScope)
            else None
        )

    def _current(self, scope):
        return self._context() == scope and not self.service._stop_requested.is_set()

    async def _load_locked(self):
        if not self._loaded:
            self.registry = await owned_thread(self.store.load)
            self._loaded = True
            self._error = False

    def _failed(self):
        # An fsync failure can follow replacement: do not display cached enabled
        # preferences until disk has been reloaded successfully.
        self._loaded = False
        self._error = True
        self._signature = None
        self.publisher.invalidate()

    async def reconcile(self):
        self.publisher.start()
        async with self._lock:
            try:
                await self._load_locked()
            except Exception:
                self._failed()
        if self._scope is not None and self._current(self._scope) and not self._error:
            self.observe(self._scope, self._records)

    def observe(self, scope, records):
        if not self._current(scope):
            return
        self._scope, self._records = scope, records
        self.publisher.notify()
        # State/freshness updates must not create a file transaction for every
        # sensor event. Only identity, mapping evidence and import filters matter.
        signature = (
            scope,
            tuple(
                sorted(
                    (
                        row.key,
                        row.family,
                        row.kind,
                        row.capabilities,
                        row.capability_evidence,
                        row.supported,
                    )
                    for row in records
                )
            ),
        )
        if signature == self._signature:
            return
        self._pending = scope, records, signature
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._flush(), name="device-bridge-registry")

    async def _flush(self):
        while self._pending is not None:
            async with self._lock:
                pending, self._pending = self._pending, None
                if pending is None:
                    return
                scope, records, signature = pending
                if not self._current(scope):
                    continue
                try:
                    await self._load_locked()
                    self.registry = await owned_thread(
                        self.store.reconcile, scope.profile_id, records
                    )
                    self._signature = signature
                    self._error = False
                    self.publisher.notify()
                except Exception:
                    self._failed()
                    # Retry on a subsequent inventory/reconcile cycle, not in a
                    # tight loop against a broken or full disk.
                    self._pending = None
                    return

    def invalidate(self):
        self.publisher.invalidate()
        self._pending = None
        self._scope = None
        self._records = ()
        self._signature = None

    async def close(self):
        self.invalidate()
        if self._task is not None:
            await settle_owned(self._task)
        await self.publisher.close()

    async def configure(self, *, profile_id, account_generation, expected_revision, **value):
        try:
            scope = AccountScope(profile_id, account_generation)
            if type(expected_revision) is not int or not 0 <= expected_revision < 2**53:
                raise ValueError
            preferences = validate_preferences(value)
        except (ValueError, TypeError) as error:
            raise AdminProtocolError("bad_params", "Bridge preferences are invalid") from error
        async with self.service._session_change_lock, self._lock:
            if self._context() != scope:
                raise AdminProtocolError("account_changed", "The Ring account changed")
            if self.service._stop_requested.is_set():
                raise AdminProtocolError("service_stopping", "Halo Server is stopping")
            self._configuring = True
            try:
                self.publisher.invalidate()
                records = self._records if self._scope == scope else ()
                try:
                    self.registry = await owned_thread(
                        self.store.configure,
                        profile_id,
                        records=records,
                        expected_revision=expected_revision,
                        **preferences,
                    )
                    self._loaded = True
                    self._error = False
                except asyncio.CancelledError:
                    self._failed()
                    raise
                except BridgeRegistryConflictError as error:
                    self._failed()
                    try:
                        await self._load_locked()
                    except Exception:
                        self._failed()
                    raise AdminProtocolError(
                        "configuration_conflict",
                        "Bridge preferences changed; refresh before saving",
                    ) from error
                except BridgeRegistryError as error:
                    self._failed()
                    raise AdminProtocolError(
                        "device_bridge_unavailable", "Bridge preferences need attention"
                    ) from error
                self.publisher.start()
                projection = bridge_projection(self.registry, profile_id, records)
                projection["publication"] = "implemented"
                # The durable web operation journal has a much smaller budget than
                # the inventory. Never put device lists or pairing identity into it.
                return {
                    key: projection[key]
                    for key in (
                        "revision",
                        "configured",
                        "enabled",
                        "auto_import",
                        "selected_count",
                        "overflow_count",
                        "publication",
                    )
                }
            finally:
                self._configuring = False
                self.publisher.notify()

    def snapshot(self):
        scope = self._context()
        # An error must hide old identity/preferences. The file is preserved for
        # repair, never replaced with a freshly generated bridge.
        registry = self.registry if self._loaded and not self._error else BridgeRegistry()
        try:
            projection = bridge_projection(
                registry,
                scope.profile_id if scope else None,
                self._records if self._scope == scope else (),
            )
        except BridgeRegistryError:
            # A future catalog classification may be valid for monitoring but
            # unsupported by this registry schema. Keep the owner interface and
            # camera administration usable while bridge preparation is blocked.
            self._failed()
            projection = bridge_projection(BridgeRegistry(), scope.profile_id if scope else None)
        projection["publication"] = "implemented"
        projection["runtime"] = self.publisher.snapshot()
        projection["status"] = (
            "error"
            if self._error
            else "offline"
            if scope is None
            else "ready"
            if self._loaded
            else "loading"
        )
        projection["truncated"] = len(projection["devices"]) > 256
        projection["devices"] = projection["devices"][:256]
        return projection
