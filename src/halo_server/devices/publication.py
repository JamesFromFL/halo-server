"""Account-fenced sensor/alarm publication and explicit bridge pairing lifecycle."""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from uuid import uuid4

from halo_server.account_scope import AccountScope, valid_profile_id
from halo_server.api.admin import AdminProtocolError
from halo_server.devices.publication_policy import (
    BridgeReset,
    PublicationConflict,
    PublicationPolicy,
    PublicationStore,
)
from halo_server.devices.runtime import DeviceBridgeRuntime, device_resources
from halo_server.homekit.pairing_maintenance import MaintenanceOwner
from halo_server.media.manager import owned_thread, settle_owned

SOURCES = {
    "contact": "contact_open",
    "motion": "motion_detected",
    "leak": "leak_detected",
    "smoke": "smoke_detected",
    "carbon_monoxide": "co_detected",
}
PAIRING_FIELDS = frozenset(
    {
        "profile_id",
        "account_generation",
        "registry_revision",
        "accessory_id",
        "boot_id",
        "pairing_revision",
    }
)


class DeviceMaintenanceOwner(MaintenanceOwner):
    helper_name = "device-pairing-reset.js"


def _display(value, default):
    # Provider names are display-only and never a storage or HomeKit identity.
    from halo_server.homekit.text import safe_display_text

    value = safe_display_text(value, 64).strip()
    return value or default


class DevicePublisher:
    def __init__(self, bridge, *, resources=device_resources, runtime_factory=DeviceBridgeRuntime):
        self.bridge, self.service = bridge, bridge.service
        self.store = PublicationStore(self.service.paths.homekit_device_publication_file)
        self.policy = PublicationPolicy()
        self._loaded = False
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._task = None
        self._closed = False
        self._token = 0
        self._runtime_token = None
        self.runtime = None
        self._resources = resources
        self._factory = runtime_factory
        self._published_boot = None
        self._sequence = 0
        self._scope = None
        self._state = "disabled"
        self._reason = None
        self._last_snapshot = None

    def notify(self):
        self._wake.set()

    def start(self):
        # Logout joins close(), but the daemon reuses this monitor on next login.
        # Only reopen after the previous loop has finished, never during shutdown.
        if self._closed and not self.service._stop_requested.is_set():
            if self._task is not None and not self._task.done():
                return
            self._closed = False
        if not self._closed and (self._task is None or self._task.done()):
            self._task = asyncio.create_task(self._run(), name="device-bridge-publication")
        self.notify()

    def invalidate(self):
        self._token += 1
        self._published_boot = None
        self._last_snapshot = None
        if self.runtime is not None:
            self.runtime.invalidate()
        self._state, self._reason = "offline", "account-or-policy-changed"
        self.notify()

    async def close(self):
        self._closed = True
        self.invalidate()
        if self._task is not None:
            await settle_owned(self._task)
        async with self._lock:
            await self._retire()

    async def _load(self):
        if not self._loaded:
            self.policy = await owned_thread(self.store.load)
            self._loaded = True

    def _profile(self):
        scope = self.bridge._context()
        profile = self.bridge.registry.profile(scope.profile_id) if scope else None
        return scope, profile

    def _eligible(self):
        scope, profile = self._profile()
        monitor = getattr(self.service, "_device_monitor", None)
        saved = self.policy.profile(scope.profile_id) if scope else None
        binding = getattr(monitor, "_binding", None)
        return bool(
            not self._closed
            and self._loaded
            and self.bridge._loaded
            and not self.bridge._error
            and not self.bridge._configuring
            and scope
            and self.bridge._current(scope)
            and profile
            and profile.enabled
            and saved
            and saved.enabled
            and saved.accessory_id == profile.accessory_uuid
            and (saved.reset is None or saved.reset.phase == "completed")
            and self.service.configuration.homekit_enabled
            and binding is not None
            and monitor._current(binding)
        )

    async def _retire(self):
        if self.runtime is not None:
            await self.runtime.stop()
            self.runtime = None
        self._published_boot = None
        self._runtime_token = None
        self._scope = None

    async def _run(self):
        while not self._closed:
            self._wake.clear()
            try:
                async with self._lock:
                    await self._step()
            except Exception:
                self._loaded = False
                self._state, self._reason = "error", "bridge-unavailable"
                with contextlib.suppress(Exception):
                    async with self._lock:
                        await self._retire()
            if not self._closed:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), 5)

    async def _step(self):
        await self._load()
        scope, profile = self._profile()
        if not self._eligible():
            await self._retire()
            saved = self.policy.profile(scope.profile_id) if scope else None
            pending = saved and saved.reset and saved.reset.phase != "completed"
            self._state = "reset_pending" if pending else "disabled"
            self._reason = "reset-needs-attention" if pending else None
            return
        if self.runtime is not None and (
            self._scope != scope or self._runtime_token != self._token
        ):
            await self._retire()
        token = self._token
        if self.runtime is None:
            resources = self._resources()
            await owned_thread(self.service.paths.prepare_homekit_devices)
            if token != self._token or not self._eligible():
                return

            async def changed(_boot):
                self.notify()

            self.runtime = self._factory(
                resources.node,
                resources.entry,
                build_id=resources.build_id,
                pairing_directory=self.service.paths.homekit_device_pairing_directory,
                on_ready=changed,
                on_retired=changed,
                on_alarm_set=self._alarm_set,
                on_light_set=self._light_set,
            )
            self._runtime_token, self._scope = token, scope
            self._state = "starting"
            # Keep a failed runtime object: its bounded restart budget may not be
            # replenished by the periodic observation refresh loop.
            try:
                await self.runtime.start()
            except Exception:
                self._state, self._reason = "error", "bridge-start-failed"
                return
        if self.runtime.state != "ready":
            self._state = "error" if self.runtime.state in {"failed", "stopped"} else "starting"
            self._reason = "bridge-unavailable"
            return
        revision = self.bridge.registry.revision
        runtime = self.runtime
        boot = runtime.boot_id

        def admitted():
            return self._admitted(scope, revision, runtime, token, boot)

        if not admitted():
            return
        self._sequence += 1
        snapshot = self._snapshot(scope, profile, self._sequence)
        try:
            result = await runtime.publish(snapshot, admission=admitted)
            if result.get("accepted") is True and admitted():
                self._last_snapshot = snapshot
            status = (
                await runtime.status(self._identity(scope, profile), admission=admitted)
                if result.get("accepted") is True and admitted()
                else {}
            )
        except Exception:
            self._published_boot = None
            self._state, self._reason = "offline", "bridge-unavailable"
            return
        if admitted() and status.get("accepted") is True and status.get("published") is True:
            self._published_boot = runtime.boot_id
            self._state, self._reason = "ready", None
        else:
            self._published_boot = None
            self._state, self._reason = "offline", "publication-rejected"

    def _admitted(self, scope, revision, runtime, token, boot):
        return (
            self._eligible()
            and self.bridge._context() == scope
            and self.bridge.registry.revision == revision
            and self.runtime is runtime
            and self._scope == scope
            and self._runtime_token == token
            and self._token == token
            and runtime.state == "ready"
            and runtime.boot_id == boot
        )

    def _identity(self, scope, profile):
        return {
            "scope": {"profile_id": scope.profile_id, "account_generation": scope.generation},
            "registry_revision": self.bridge.registry.revision,
            "accessory_uuid": profile.accessory_uuid,
            "username": profile.username,
        }

    def _snapshot(self, scope, profile, sequence):
        from halo_server.devices.bridge_registry import bridge_projection

        monitor = self.service._device_monitor
        catalog = monitor._catalog
        observations = {}
        if catalog is not None and (catalog.profile_id, catalog.account_generation) == (
            scope.profile_id,
            scope.generation,
        ):
            observations = {
                row["id"]: row
                for row in catalog.snapshot(now=monitor._clock(), max_age=360)["devices"]
            }
        projection = bridge_projection(self.bridge.registry, scope.profile_id, self.bridge._records)
        children = []
        for entry in projection["devices"]:
            if not entry["selected"] or not entry["mappings"]:
                continue
            row = observations.get(entry["id"], {})
            available = (
                row.get("source_health") == "online" and entry["eligible"] and not entry["excluded"]
            )
            values = {
                key: row.get("observation_details", {}).get(source, {}).get("value")
                if available and key in entry["mappings"]
                else None
                for key, source in SOURCES.items()
            }
            values = {key: value if type(value) is bool else None for key, value in values.items()}
            alarm = None
            if entry["mappings"] == ["security_system"]:
                state = monitor.alarm.view(row) if row else None
                mode = state.get("mode") if state and available else None
                signal = state.get("signal") if state and available else None
                phase = state.get("phase") if state and available else None
                triggered = (
                    True
                    if phase == "alarming" or signal not in {None, "none"}
                    else False
                    if phase in {"idle", "entry-delay", "exit-delay"} and signal == "none"
                    else None
                )
                alarm = {
                    "mode": mode,
                    "triggered": triggered,
                    # This is authority, not transient readiness. Publishing
                    # pending/busy as a revoked grant would invalidate the very
                    # request whose readback is currently being awaited.
                    "control_enabled": bool(
                        state
                        and state["enabled"]
                        and state["authorization"] == "allowed"
                        and mode is not None
                        and triggered is not None
                        and available
                    ),
                }
            light = None
            if entry["mappings"] == ["lightbulb"]:
                state = monitor.light.view(row) if row else None
                on = state.get("on") if state and available else None
                light = {
                    "on": on,
                    "control_enabled": bool(
                        state
                        and state["enabled"]
                        and state["authorization"] == "allowed"
                        and type(on) is bool
                        and available
                    ),
                }
            children.append(
                {
                    "device_id": entry["id"],
                    "accessory_uuid": entry["accessory_uuid"],
                    "name": _display(row.get("name", ""), "Ring sensor"),
                    "model": _display(row.get("model", ""), "Ring sensor"),
                    "mappings": sorted(entry["mappings"]),
                    "available": available,
                    "values": values,
                    "alarm": alarm,
                    "light": light,
                }
            )
        return {
            "scope": self._identity(scope, profile)["scope"],
            "registry_revision": self.bridge.registry.revision,
            "sequence": sequence,
            "expires_at_ms": int(time.monotonic() * 1000) + 15000,
            "bridge": {
                "accessory_uuid": profile.accessory_uuid,
                "username": profile.username,
                "name": "Halo Bridge",
            },
            "children": children,
        }

    async def _alarm_set(self, payload, admission):
        """A HAP request has no Ring authority until the daemon admits it."""
        scope, profile = self._profile()
        runtime, token = self.runtime, self._token
        boot = runtime.boot_id if runtime else None
        revision = self.bridge.registry.revision
        snapshot = self._last_snapshot
        if (
            scope is None
            or profile is None
            or runtime is None
            or snapshot is None
            or not admission()
            or not self._admitted(scope, revision, runtime, token, boot)
            or self._published_boot != boot
            or payload["scope"] != snapshot["scope"]
            or payload["registry_revision"] != revision
            or payload["sequence"] != snapshot["sequence"]
            or snapshot["expires_at_ms"] <= time.monotonic() * 1000
        ):
            return {"accepted": False, "code": "conflict"}
        child = next(
            (
                row
                for row in snapshot["children"]
                if row["device_id"] == payload["device_id"]
                and row["accessory_uuid"] == payload["accessory_uuid"]
            ),
            None,
        )
        if not child or not child.get("alarm") or not child["alarm"]["control_enabled"]:
            return {"accepted": False, "code": "policy-denied"}
        monitor = self.service._device_monitor
        row = monitor.alarm._row(payload["device_id"])
        state = monitor.alarm.view(row) if row else None
        if not state or not state["can_set_mode"]:
            return {"accepted": False, "code": "policy-denied"}

        def current():
            # Observation sequence may advance while waiting for readback.
            return admission() and self._admitted(scope, revision, runtime, token, boot)

        try:
            result = await monitor.alarm.set_mode(
                profile_id=scope.profile_id,
                account_generation=scope.generation,
                device_id=payload["device_id"],
                provider_generation=state["provider_generation"],
                expected_epoch=state["epoch"],
                expected_revision=state["observed_revision"],
                expected_policy_revision=state["policy_revision"],
                target=payload["target"],
                operation_id=str(uuid4()),
                admission=current,
            )
        except AdminProtocolError as error:
            return {"accepted": False, "code": "busy" if error.code == "busy" else "conflict"}
        if result.get("status") == "confirmed" and current():
            return {"accepted": True}
        return {
            "accepted": False,
            "code": "uncertain" if result.get("possibly_sent") else "policy-denied",
        }

    async def _light_set(self, payload, admission):
        """A HAP request has no Ring authority until the daemon admits it."""
        scope, profile = self._profile()
        runtime, token = self.runtime, self._token
        boot = runtime.boot_id if runtime else None
        revision = self.bridge.registry.revision
        snapshot = self._last_snapshot
        if (
            scope is None
            or profile is None
            or runtime is None
            or snapshot is None
            or not admission()
            or not self._admitted(scope, revision, runtime, token, boot)
            or self._published_boot != boot
            or payload["scope"] != snapshot["scope"]
            or payload["registry_revision"] != revision
            or payload["sequence"] != snapshot["sequence"]
            or snapshot["expires_at_ms"] <= time.monotonic() * 1000
        ):
            return {"accepted": False, "code": "conflict", "observed_on": None}
        child = next(
            (
                row
                for row in snapshot["children"]
                if row["device_id"] == payload["device_id"]
                and row["accessory_uuid"] == payload["accessory_uuid"]
            ),
            None,
        )
        if not child or not child.get("light") or not child["light"]["control_enabled"]:
            return {"accepted": False, "code": "policy-denied", "observed_on": None}
        monitor = self.service._device_monitor
        row = monitor.light._row(payload["device_id"])
        state = monitor.light.view(row) if row else None
        if not state or not state["can_set"]:
            return {"accepted": False, "code": "policy-denied", "observed_on": None}

        def current():
            # Observation sequence may advance while waiting for readback.
            return admission() and self._admitted(scope, revision, runtime, token, boot)

        try:
            result = await monitor.light.set_light(
                profile_id=scope.profile_id,
                account_generation=scope.generation,
                device_id=payload["device_id"],
                provider_generation=state["provider_generation"],
                expected_epoch=state["epoch"],
                expected_revision=state["observed_revision"],
                expected_policy_revision=state["policy_revision"],
                target=payload["target"],
                operation_id=str(uuid4()),
                admission=current,
            )
        except AdminProtocolError as error:
            return {
                "accepted": False,
                "code": "busy" if error.code == "busy" else "conflict",
                "observed_on": None,
            }
        if result.get("status") == "confirmed" and type(result.get("on")) is bool and current():
            # A newer source event can reach the daemon after the provider
            # produced its result. Do not overwrite it with an older reply.
            latest_row = monitor.light._row(payload["device_id"])
            latest = monitor.light.view(latest_row) if latest_row else None
            if not latest or latest["on"] is None or latest["authorization"] != "allowed":
                return {"accepted": False, "code": "uncertain", "observed_on": None}
            observed = (
                latest["on"]
                if latest["observed_revision"] > state["observed_revision"]
                else result["on"]
            )
            return {"accepted": True, "code": "ok", "observed_on": observed}
        return {
            "accepted": False,
            "code": "uncertain" if result.get("possibly_sent") else "policy-denied",
            "observed_on": None,
        }

    def snapshot(self):
        scope, _profile = self._profile()
        saved = self.policy.profile(scope.profile_id) if scope and self._loaded else None
        pending = bool(saved and saved.reset and saved.reset.phase != "completed")
        published = bool(
            self.runtime
            and self._eligible()
            and self._scope == scope
            and self._runtime_token == self._token
            and self._published_boot == self.runtime.boot_id
            and self.runtime.state == "ready"
        )
        return {
            "state": "reset_pending" if pending else self._state,
            "reason": self._reason,
            "boot_id": self.runtime.boot_id if published else None,
            "published": published,
            "publication_enabled": bool(saved and saved.enabled),
            "publication_revision": self.policy.revision if self._loaded else 0,
            "reset_pending": pending,
            "reset_operation_id": saved.reset.operation_id if pending else None,
        }

    def _scope_check(self, profile_id, account_generation):
        try:
            scope = AccountScope(profile_id, account_generation)
        except (TypeError, ValueError) as error:
            raise AdminProtocolError("bad_params", "Bridge account identity is invalid") from error
        current, profile = self._profile()
        if scope != current or not self.bridge._current(scope):
            raise AdminProtocolError("account_changed", "The Ring account changed")
        if profile is None or not self.bridge._loaded or self.bridge._error:
            raise AdminProtocolError("device_bridge_unavailable", "Prepare Halo Bridge first")
        return scope, profile

    async def configure(
        self,
        *,
        profile_id,
        account_generation,
        expected_registry_revision,
        expected_revision,
        enabled,
    ):
        if (
            any(
                type(value) is not int or not 0 <= value < 2**53
                for value in (expected_registry_revision, expected_revision)
            )
            or type(enabled) is not bool
        ):
            raise AdminProtocolError("bad_params", "Bridge publication preferences are invalid")
        async with self.service._session_change_lock, self._lock:
            scope, profile = self._scope_check(profile_id, account_generation)
            if self.bridge.registry.revision != expected_registry_revision:
                raise AdminProtocolError("configuration_conflict", "Bridge preferences changed")
            monitor = self.service._device_monitor
            if enabled and (
                not self.service.configuration.homekit_enabled
                or not profile.enabled
                or monitor._binding is None
                or not monitor._current(monitor._binding)
            ):
                raise AdminProtocolError(
                    "configuration_conflict", "Enable HomeKit, monitoring and bridge imports first"
                )
            self.invalidate()
            await self._retire()
            try:
                self.policy = await owned_thread(
                    self.store.set_enabled,
                    scope.profile_id,
                    profile.accessory_uuid,
                    enabled,
                    expected_revision=expected_revision,
                )
                self._loaded = True
            except asyncio.CancelledError:
                self._loaded = False
                self.notify()
                raise
            except PublicationConflict as error:
                self._loaded = False
                raise AdminProtocolError(
                    "configuration_conflict", "Bridge publication changed; refresh before saving"
                ) from error
            except Exception as error:
                self._loaded = False
                raise AdminProtocolError(
                    "device_bridge_unavailable", "Bridge publication needs attention"
                ) from error
            self.start()
            return {"publication_revision": self.policy.revision, "publication_enabled": enabled}

    async def pairing(self):
        async with self._lock:
            return await self._pairing()

    async def _pairing(self):
        await self._load()
        scope, profile = self._profile()
        saved = self.policy.profile(scope.profile_id) if scope else None
        pending = (
            saved.reset if saved and saved.reset and saved.reset.phase != "completed" else None
        )
        result = {
            "profile_id": scope.profile_id if scope else None,
            "account_generation": scope.generation if scope else None,
            "registry_revision": self.bridge.registry.revision,
            "publication_revision": self.policy.revision,
            "accessory_id": profile.accessory_uuid if profile else None,
            "username": profile.username if profile else None,
            "boot_id": None,
            "available": False,
            "published": False,
            "paired_known": False,
            "paired": False,
            "paired_clients": 0,
            "pairing_revision": pending.pairing_revision if pending else None,
            "pending_reset": pending is not None,
            "reset_operation_id": pending.operation_id if pending else None,
        }
        if pending or not self.snapshot()["published"]:
            return result
        runtime, token, revision = self.runtime, self._token, self.bridge.registry.revision
        boot = runtime.boot_id

        def admitted():
            return self._admitted(scope, revision, runtime, token, boot)

        try:
            status = await runtime.status(self._identity(scope, profile), admission=admitted)
        except Exception:
            return result
        if admitted() and status.get("accepted") is True:
            result.update(
                {
                    key: status[key]
                    for key in ("published", "paired", "paired_clients", "pairing_revision")
                }
            )
            result.update(available=True, paired_known=True, boot_id=runtime.boot_id)
        return result

    def _validate_pairing(self, params, *, reset=False):
        expected = PAIRING_FIELDS | ({"operation_id"} if reset else set())
        if (
            set(params) != expected
            or not valid_profile_id(params.get("accessory_id"))
            or type(params.get("registry_revision")) is not int
            or not 0 <= params["registry_revision"] < 2**53
            or not isinstance(params.get("pairing_revision"), str)
            or re.fullmatch(r"[a-f0-9]{64}", params["pairing_revision"]) is None
            or (params.get("boot_id") is not None and not valid_profile_id(params["boot_id"]))
            or (not reset and params.get("boot_id") is None)
        ):
            raise AdminProtocolError("bad_params", "Bridge pairing identity is invalid")
        if reset and not valid_profile_id(params["operation_id"]):
            raise AdminProtocolError("bad_params", "Bridge reset operation is invalid")
        scope, profile = self._scope_check(params["profile_id"], params["account_generation"])
        if (
            profile.accessory_uuid != params["accessory_id"]
            or self.bridge.registry.revision != params["registry_revision"]
        ):
            raise AdminProtocolError("configuration_conflict", "Bridge identity changed")
        return scope, profile

    async def reveal(self, **params):
        async with self.service._session_change_lock, self._lock:
            scope, profile = self._validate_pairing(params)
            page = await self._pairing()
            if not page["available"] or any(
                page[key] != params[key] for key in ("boot_id", "pairing_revision")
            ):
                raise AdminProtocolError("configuration_conflict", "Bridge pairing changed")
            runtime, token, revision = self.runtime, self._token, self.bridge.registry.revision
            boot = runtime.boot_id

            def admitted():
                return self._admitted(scope, revision, runtime, token, boot)

            reply = await runtime.reveal(
                self._identity(scope, profile) | {"pairing_revision": params["pairing_revision"]},
                admission=admitted,
            )
            if not admitted():
                raise AdminProtocolError(
                    "device_bridge_unavailable", "Bridge pairing is unavailable"
                )
            if reply.get("accepted") is not True:
                if reply.get("code") == "policy-denied":
                    raise AdminProtocolError(
                        "pairing_not_available", "Pairing code is unavailable for Halo Bridge"
                    )
                if reply.get("code") == "conflict":
                    raise AdminProtocolError("configuration_conflict", "Bridge pairing changed")
                raise AdminProtocolError(
                    "device_bridge_unavailable", "Bridge pairing is unavailable"
                )
            return {
                **params,
                "pin": reply["pincode"],
                "setup_uri": reply["setup_uri"],
                "expires_in_ms": min(30000, reply["ttl_ms"]),
            }

    async def reset(self, **params):
        async with self.service._session_change_lock, self._lock:
            scope, profile = self._validate_pairing(params, reset=True)
            await self._load()
            saved = self.policy.profile(scope.profile_id)
            previous = saved.reset if saved else None
            owner = None
            try:
                resources = self._resources()
                if previous and previous.operation_id == params["operation_id"]:
                    if previous.pairing_revision != params["pairing_revision"]:
                        raise AdminProtocolError(
                            "configuration_conflict", "Bridge reset identity changed"
                        )
                    row = previous
                    if row.phase == "completed":
                        return {"completed": True, "publication_enabled": False}
                else:
                    if previous and previous.phase != "completed":
                        raise AdminProtocolError(
                            "configuration_conflict", "Resume the pending bridge reset"
                        )
                    page = await self._pairing()
                    if not page["available"] or any(
                        page[key] != params[key] for key in ("boot_id", "pairing_revision")
                    ):
                        raise AdminProtocolError("configuration_conflict", "Bridge pairing changed")
                    runtime, token, revision = (
                        self.runtime,
                        self._token,
                        self.bridge.registry.revision,
                    )

                    boot = runtime.boot_id

                    def admitted():
                        return self._admitted(scope, revision, runtime, token, boot)

                    status = await runtime.status(
                        self._identity(scope, profile), admission=admitted
                    )
                    if (
                        not admitted()
                        or status.get("pairing_revision") != params["pairing_revision"]
                        or not status.get("accepted")
                    ):
                        raise AdminProtocolError("configuration_conflict", "Bridge pairing changed")
                    row = BridgeReset(
                        params["operation_id"],
                        scope.profile_id,
                        profile.accessory_uuid,
                        profile.username,
                        params["pairing_revision"],
                        tuple(sorted(status["pairing_manifest"].items())),
                    )
                    self.policy = await owned_thread(self.store.begin_reset, row)
                    # Denial is durable before quiescence or any destructive helper.
                owner = DeviceMaintenanceOwner.capture(
                    self.runtime, self.service.paths.homekit_device_pairing_directory
                )
                if (
                    row.phase == "denied"
                    and self.runtime is not None
                    and self.runtime.state == "ready"
                ):
                    reset_runtime, reset_boot = self.runtime, self.runtime.boot_id
                    frozen = await self.runtime.prepare_reset(
                        self._identity(scope, profile) | {"pairing_revision": row.pairing_revision},
                        admission=lambda: (
                            self.bridge._current(scope)
                            and self.runtime is reset_runtime
                            and reset_runtime.boot_id == reset_boot
                        ),
                    )
                    if (
                        self.runtime is not reset_runtime
                        or reset_runtime.boot_id != reset_boot
                        or not frozen.get("accepted")
                        or not frozen.get("quiesced")
                        or frozen.get("pairing_manifest") != dict(row.manifest)
                    ):
                        raise RuntimeError("Bridge reset manifest changed")
                self.invalidate()
                await self._retire()
                if row.phase == "denied":
                    self.policy = await owned_thread(
                        self.store.advance, scope.profile_id, row.operation_id, "quiesced"
                    )
                    row = self.policy.profile(scope.profile_id).reset
                resume = row.phase == "cleanup_started"
                if row.phase == "quiesced":
                    self.policy = await owned_thread(
                        self.store.advance, scope.profile_id, row.operation_id, "cleanup_started"
                    )
                    row = self.policy.profile(scope.profile_id).reset
                await owner.cleanup(resources, row, resume=resume)
                self.policy = await owned_thread(
                    self.store.advance, scope.profile_id, row.operation_id, "completed"
                )
                self._state, self._reason = "disabled", None
                return {"completed": True, "publication_enabled": False}
            except asyncio.CancelledError:
                self.invalidate()
                self._loaded = False
                await self._retire()
                raise
            except AdminProtocolError:
                raise
            except Exception as error:
                self.invalidate()
                self._loaded = False
                self._state, self._reason = "reset_pending", "reset-needs-attention"
                raise AdminProtocolError(
                    "device_bridge_unavailable",
                    "Bridge reset needs attention; refresh to resume the same operation",
                ) from error
            finally:
                if owner is not None:
                    owner.close()
                self.notify()
