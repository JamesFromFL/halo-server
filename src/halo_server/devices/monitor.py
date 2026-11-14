"""Daemon ownership of opt-in, read-only non-camera observations.

All public state lives on the daemon loop. Ring I/O stays on the existing client's
loop; callbacks are fenced by account, client, and provider generation.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from dataclasses import dataclass, field

from halo_server.account_scope import AccountScope
from halo_server.api.admin import AdminProtocolError
from halo_server.devices.catalog import CatalogError, DeviceCatalog
from halo_server.devices.policy import (
    DevicePolicy,
    DevicePolicyConflictError,
    DevicePolicyError,
    DevicePolicyStore,
    safety_snapshot,
)
from halo_server.media.manager import owned_thread


@dataclass
class _Binding:
    client: object
    scope: AccountScope
    generation: int
    active: threading.Event = field(default_factory=threading.Event)
    provider: object = None


class DeviceMonitor:
    def __init__(self, service, *, provider_factory=None, clock=time.time):
        self.service = service
        self.store = DevicePolicyStore(service.paths.state_directory / "devices" / "policy.json")
        self.policy = DevicePolicy()
        self._loaded = False
        self._factory = provider_factory
        self._clock = clock
        self._lock = asyncio.Lock()
        self._binding = None
        self._catalog = None
        self._generation = 0
        self._state = "disabled"

    def _context(self):
        client = self.service._sessions.active_client
        scope = self.service._current_account_scope()
        if client is None or not client.is_authenticated or not isinstance(scope, AccountScope):
            return None
        return client, scope

    def _current(self, binding):
        return (
            binding is self._binding
            and binding.active.is_set()
            and self.policy.enabled
            and not self.service._stop_requested.is_set()
            and self._context() == (binding.client, binding.scope)
        )

    def invalidate(self):
        """Fence queued callbacks immediately; async retirement follows separately."""
        if self._binding is not None:
            self._binding.active.clear()

    async def reconcile(self):
        async with self._lock:
            try:
                if not self._loaded:
                    self.policy = await owned_thread(self.store.load)
                    self._loaded = True
                await self._reconcile_locked()
            except Exception:
                # Provider failure must not prevent camera setup. The owner sees
                # a finite error state, never raw provider or credential text.
                self.invalidate()
                self._state = "error"
                with contextlib.suppress(Exception):
                    await self._retire()

    async def _reconcile_locked(self):
        context = self._context()
        if not self.policy.enabled or context is None or self.service._stop_requested.is_set():
            await self._retire()
            self._state = "disabled" if not self.policy.enabled else "offline"
            return
        if self._binding is not None and self._current(self._binding):
            return
        await self._retire()
        client, scope = context
        self._generation += 1
        binding = _Binding(client, scope, self._generation)
        binding.active.set()
        self._binding = binding
        self._catalog = DeviceCatalog(scope.profile_id, scope.generation, binding.generation)
        self._state = "starting"
        loop = asyncio.get_running_loop()

        def receive(**batch):
            if binding.active.is_set():
                with contextlib.suppress(RuntimeError):
                    loop.call_soon_threadsafe(self._receive, binding, batch)

        async def start():
            if not self._current(binding):
                return
            from halo_server.devices.provider import NoncameraProvider

            provider = (self._factory or NoncameraProvider)(
                client.ring.auth,
                profile_id=scope.profile_id,
                account_generation=scope.generation,
                provider_generation=binding.generation,
                on_snapshot=receive,
                admission=lambda: self._current(binding),
            )
            binding.provider = provider
            client.register_session_resource(provider)
            try:
                await provider.start()
            except BaseException:
                await provider.close()
                client.unregister_session_resource(provider)
                raise

        async with asyncio.timeout(10):
            await asyncio.wrap_future(client.submit(start()))

    def _receive(self, binding, batch):
        if not self._current(binding):
            return
        try:
            self._catalog.apply(**batch)
        except (CatalogError, TypeError, ValueError):
            self._state = "error"
            return
        self._state = "ready" if batch["complete"] else "partial"

    async def _retire(self):
        binding = self._binding
        if binding is None:
            return
        binding.active.clear()
        self._catalog = None

        async def close():
            if binding.provider is not None:
                await binding.provider.close()
                binding.client.unregister_session_resource(binding.provider)

        # RingClient itself owns retirement when login replaces or stops it.
        if binding.client.is_authenticated:
            async with asyncio.timeout(10):
                await asyncio.wrap_future(binding.client.submit(close()))
        self._binding = None

    async def close(self):
        self.invalidate()
        async with self._lock:
            await self._retire()
            self._state = "disabled" if not self.policy.enabled else "offline"

    async def set_enabled(self, *, profile_id, account_generation, expected_revision, enabled):
        if (
            type(enabled) is not bool
            or type(expected_revision) is not int
            or not 0 <= expected_revision < 2**53
            or type(account_generation) is not int
            or account_generation < 1
        ):
            raise AdminProtocolError("bad_params", "Device monitoring preference is invalid")
        async with self.service._session_change_lock, self._lock:
            context = self._context()
            if context is None or (context[1].profile_id, context[1].generation) != (
                profile_id,
                account_generation,
            ):
                raise AdminProtocolError("account_changed", "The Ring account changed")
            if self.service._stop_requested.is_set():
                raise AdminProtocolError("service_stopping", "Halo Server is stopping")
            try:
                self.policy = await owned_thread(
                    self.store.set_enabled, enabled, expected_revision=expected_revision
                )
                self._loaded = True
            except DevicePolicyConflictError as error:
                try:
                    self.policy = await owned_thread(self.store.load)
                    self._loaded = True
                    await self._reconcile_locked()
                except Exception:
                    self._loaded = False
                    self.invalidate()
                    self._state = "error"
                raise AdminProtocolError(
                    "configuration_conflict", "Device policy changed"
                ) from error
            except DevicePolicyError as error:
                # A replace/fsync failure can mean the new preference reached
                # disk. Reload before any later admission; never restart from
                # the previous in-memory enabled value after an uncertain save.
                self._loaded = False
                self.invalidate()
                self._state = "error"
                with contextlib.suppress(Exception):
                    await self._retire()
                raise AdminProtocolError(
                    "device_policy_unavailable", "Device policy needs repair"
                ) from error
            try:
                await self._reconcile_locked()
            except Exception as error:
                self.invalidate()
                self._state = "error"
                raise AdminProtocolError(
                    "device_monitoring_unavailable", "Preference saved; monitoring needs repair"
                ) from error
            # Never put a whole inventory into the bounded mutation journal.
            return {
                "enabled": self.policy.enabled,
                "policy_revision": self.policy.revision,
                "state": self._state,
                "safety": safety_snapshot(),
            }

    def snapshot(self):
        context = self._context()
        scope = context[1] if context is not None else None
        state = self._state
        inventory = {"revision": 0, "complete": False, "observed_at": None, "devices": []}
        if self._binding is not None and self._current(self._binding) and self._catalog:
            inventory = self._catalog.snapshot(now=self._clock(), max_age=360.0)
            if any(row["stale"] for row in inventory["devices"]):
                inventory["complete"] = False
                state = "partial"
        elif state != "error":
            state = "disabled" if not self.policy.enabled else "offline"
        truncated = len(inventory["devices"]) > 256
        inventory["devices"] = inventory["devices"][:256]
        if truncated:
            inventory["complete"] = False
        return {
            "schema": 1,
            **inventory,
            "policy_revision": self.policy.revision,
            "enabled": self.policy.enabled,
            "state": state,
            "truncated": truncated,
            "scope": {
                "profile_id": scope.profile_id if scope else None,
                "account_generation": scope.generation if scope else None,
            },
            "safety": safety_snapshot(),
        }
