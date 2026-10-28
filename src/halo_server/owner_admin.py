"""Local owner operations; never exported through the compatibility plugin."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import replace
from uuid import UUID

from halo_server.api.admin import AdminProtocolError
from halo_server.configuration import (
    ConfigurationConflictError,
    ConfigurationError,
    ConfigurationStore,
)
from halo_server.homekit.models import HomeKitOperationError, MediaScope
from halo_server.homekit.service_backend import ServiceHomeKitBackend
from halo_server.media.manager import owned_thread, settle_owned


class OwnerAdministration:
    methods = frozenset(
        {
            "configuration_get",
            "configuration_set",
            "camera_control_list",
            "camera_control",
            "homekit_pairing_list",
            "homekit_pairing_reveal",
            "homekit_pairing_reset",
            "doctor",
            "support_report",
        }
    )

    def __init__(self, service):
        self.service = service
        self.configuration = ConfigurationStore(service.paths.configuration_file)
        self.configuration_lock = asyncio.Lock()
        self.probe_lock = asyncio.Lock()
        self.control_backend = ServiceHomeKitBackend(service, owner_controls=True)
        self.controls = OrderedDict()

    async def handle(self, method, params):
        if method.startswith("configuration_"):
            return await self._configuration(method, params)
        if method == "camera_control_list":
            _only(params, set())
            inventory = self.control_backend.inventory()
            cameras = []
            for camera in inventory.cameras:
                source = self.control_backend._present.get(camera.scope.camera_id)
                device = source[0] if source is not None else None
                light, siren = self.service._observed_camera_controls(
                    camera.scope.camera_id, device
                )
                cameras.append(
                    {
                        "camera_id": camera.scope.camera_id,
                        "device_generation": camera.scope.device_generation,
                        "name": camera.name,
                        "has_light": camera.capabilities.light,
                        "has_siren": camera.capabilities.siren,
                        "light": light,
                        "siren": siren,
                    }
                )
            return {
                "profile_id": inventory.profile_id,
                "account_generation": inventory.account_generation,
                "cameras": cameras,
            }
        if method == "camera_control":
            return await self._control(params)
        if method.startswith("homekit_pairing_"):
            bridge = self.service._homekit_bridge
            if bridge is None:
                raise AdminProtocolError("homekit_disabled", "Start a native HomeKit mode first")
            return await bridge.owner_pairing(method, params)
        if method in {"doctor", "support_report"}:
            return await self._diagnostics(method, params)
        raise AdminProtocolError("unknown_method", "Unknown owner method")

    async def close(self):
        tasks = tuple(task for _, task in self.controls.values() if not task.done())
        failures = []
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=5)
            if pending:
                failures.append(RuntimeError("Camera owner control retirement is pending"))
        bridge = self.service._homekit_bridge
        if bridge is not None:
            try:
                await bridge._pairing_admin.close()
            except Exception as error:
                failures.append(error)
        if failures:
            raise ExceptionGroup("Owner administration retirement incomplete", failures)

    async def _diagnostics(self, method, params):
        from halo_server.diagnostics import local_doctor

        _only(params, {"probe"} if method == "doctor" else set())
        probe = params.get("probe", False)
        if type(probe) is not bool:
            raise AdminProtocolError("bad_params", "Diagnostic probe must be a boolean")
        bridge = self.service._homekit_bridge
        if probe:
            if self.probe_lock.locked():
                raise AdminProtocolError("busy", "A diagnostic probe is already running")
            async with self.probe_lock:
                local = await local_doctor(
                    self.service.paths,
                    probe=True,
                    resources=bridge._resources if bridge is not None else None,
                )
        else:
            local = await local_doctor(
                self.service.paths, resources=bridge._resources if bridge is not None else None
            )
        # Build a new whitelist; never sanitize arbitrary status/store/log data.
        native = bridge.status() if bridge is not None else {}
        boolean_keys = (
            "transport_ready",
            "live_video_ready",
            "live_audio_ready",
            "snapshots_ready",
            "recording_samples_ready",
            "recording_delivery_ready",
            "pairing_maintenance_available",
        )
        count_keys = (
            "selected_cameras",
            "live_video_resources",
            "live_video_cleanup_failures",
            "snapshot_resources",
            "snapshot_cleanup_failures",
            "recording_resources",
            "recording_cleanup_failures",
            "recording_capture_resources",
            "recording_capture_cleanup_failures",
            "recording_broker_resources",
            "recording_warm_resources",
            "recording_warm_cleanup_failures",
            "bulk_resources",
            "bulk_cleanup_pending",
            "pairing_maintenance_pending",
        )
        runtime = {key: native[key] for key in boolean_keys if type(native.get(key)) is bool}
        runtime.update(
            {
                key: native[key]
                for key in count_keys
                if type(native.get(key)) is int and native[key] >= 0
            }
        )
        try:
            saved = await self._configuration("configuration_get", {})
            runtime["configuration_available"] = True
        except AdminProtocolError:
            saved = {"saved": {"mode": "unavailable"}, "restart_required": True}
            runtime["configuration_available"] = False
        runtime.update(
            {
                "mode": self.service.configuration.mode.value,
                "saved_mode": saved["saved"]["mode"],
                "restart_required": saved["restart_required"],
                "account_authenticated": self.service._sessions.active_client is not None
                and self.service._account_authority_current(),
                "event_connected": str(self.service._event_state) == "connected",
            }
        )
        return {
            "schema": 1,
            "kind": "daemon" if method == "doctor" else "support",
            "local": local,
            "runtime": runtime,
            "limitations": ["real-ring-apple-not-qualified"],
        }

    async def _configuration(self, method, params):
        allowed = set() if method == "configuration_get" else {"expected_revision", "configuration"}
        _only(params, allowed)
        async with self.configuration_lock:
            try:
                saved = await owned_thread(self.configuration.load)
                if method == "configuration_set":
                    revision, value = params.get("expected_revision"), params.get("configuration")
                    if (
                        type(revision) is not int
                        or not 0 <= revision < 2**53
                        or not isinstance(value, dict)
                        or set(value)
                        != {"mode", "plugin_host", "plugin_port", "rtsp_host", "rtsp_port"}
                    ):
                        raise AdminProtocolError(
                            "bad_params", "Server configuration parameters are invalid"
                        )
                    proposed = replace(saved, **value)
                    task = asyncio.create_task(
                        owned_thread(self.configuration.save, proposed, expected_revision=revision),
                        name="owner-configuration-save",
                    )
                    try:
                        saved = await asyncio.shield(task)
                    except asyncio.CancelledError:
                        await settle_owned(task)
                        raise
            except ConfigurationConflictError as error:
                raise AdminProtocolError(
                    "configuration_conflict", "Server configuration changed; reload before saving"
                ) from error
            except ConfigurationError as error:
                raise AdminProtocolError(
                    "configuration_unavailable",
                    "Server configuration could not be read or saved safely",
                ) from error
            applied = self.service.configuration

            def comparable(row):
                return {
                    key: value
                    for key, value in row.as_dict().items()
                    if key not in {"revision", "version"}
                }

            return {
                "saved": saved.as_dict(),
                "applied": applied.as_dict(),
                "restart_required": comparable(saved) != comparable(applied),
            }

    async def _control(self, params):
        _only(
            params,
            {
                "profile_id",
                "account_generation",
                "camera_id",
                "device_generation",
                "operation_id",
                "control",
                "enabled",
            },
        )
        try:
            operation = params["operation_id"]
            if not isinstance(operation, str) or str(UUID(operation)) != operation:
                raise ValueError
            scope = MediaScope(
                params["profile_id"],
                params["camera_id"],
                params["account_generation"],
                params["device_generation"],
                1,
            )
            control, enabled = params["control"], params["enabled"]
            if control not in {"light", "siren"} or type(enabled) is not bool:
                raise ValueError
        except (KeyError, TypeError, ValueError) as error:
            raise AdminProtocolError(
                "bad_params", "Camera control parameters are invalid"
            ) from error
        identity = scope, control, enabled
        existing = self.controls.get(operation)
        if existing is not None:
            if existing[0] != identity:
                raise AdminProtocolError("operation_conflict", "Camera operation identity changed")
            return await asyncio.shield(existing[1])
        # Never evict a running operation or replay a possibly-sent command.
        if len(self.controls) >= 4096:
            raise AdminProtocolError(
                "capacity", "Camera operation history is full; restart before retrying"
            )
        self.control_backend.inventory()

        async def apply():
            try:
                await self.control_backend.set_control(
                    scope,
                    control,
                    enabled,
                    admission=lambda: not self.service._stop_requested.is_set(),
                )
            except HomeKitOperationError as error:
                return {
                    "status": "unknown" if error.possibly_sent else "rejected",
                    "code": error.code,
                }
            return {"status": "accepted", "code": "ok"}

        task = asyncio.create_task(apply(), name="owner-camera-control")
        task.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
        self.controls[operation] = identity, task
        return await asyncio.shield(task)


def _only(params, allowed):
    if not isinstance(params, dict) or set(params) - allowed:
        raise AdminProtocolError("bad_params", "Owner parameters are invalid")
