"""Daemon integration for the supervised native adapter, independent of Ring IO."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import stat
from dataclasses import dataclass, replace
from pathlib import Path

from halo_server.homekit.broker import HomeKitBroker, RecordingEvent
from halo_server.homekit.events import EventPump
from halo_server.homekit.models import HomeKitOperationError, MediaScope
from halo_server.homekit.pairing_admin import PairingAdministration
from halo_server.homekit.power import mains_confirmed
from halo_server.homekit.recording_settings import (
    RecordingPreferences,
    RecordingSettings,
    RecordingSettingsConflict,
    RecordingSettingsError,
    RecordingSettingsStore,
    configuration_from_wire,
    privacy_signature,
)
from halo_server.homekit.registry import CameraRegistry, RegistryError, RegistryStore
from halo_server.homekit.runtime import HomeKitRuntime
from halo_server.homekit.settings import (
    AudioPreferences,
    AudioSettings,
    AudioSettingsConflict,
    AudioSettingsError,
    AudioSettingsStore,
)
from halo_server.media.manager import owned_thread, settle_owned
from halo_server.media.recording_policy import RecordingAuthority, RecordingPolicyOwner
from halo_server.media.telemetry import safe_diagnostics
from halo_server.paths import PathSecurityError, open_private_directory

_LOGGER = logging.getLogger(__name__)
_INTEROP_STAGES = frozenset(
    {
        "snapshot",
        "video-prepare",
        "video-start",
        "video-reconfigure",
        "video-stop",
        "video-request",
        "audio-settings",
    }
)
_INTEROP_CODES = frozenset(
    {
        "operation-failed",
        "invalid-video-prepare",
        "invalid-audio-prepare",
        "video-retired",
        "invalid-video-endpoint",
        "unsupported-video-profile",
        "unsupported-video-level",
        "unsupported-video-codec",
        "unsupported-audio-format",
        "video-not-prepared",
        "video-start-conflict",
        "audio-start-conflict",
        "video-reconfigure-conflict",
        "unsupported-video-operation",
        "video-prepare-rejected",
        "video-endpoint-mismatch",
        "video-configuration-rejected",
        "video-stop-rejected",
        "video-cleanup-failed",
        "video-capacity",
        "request-timeout",
        "session-closed",
        "resource-retired",
        "snapshot-retired",
        "snapshot-unavailable",
        "snapshot-cancelled",
        "snapshot-capacity",
        "snapshot-rejected",
        "snapshot-transfer-failed",
        "snapshot-busy",
        "snapshot-timeout",
        "snapshot-expired",
        "snapshot-unsupported",
        "snapshot-unauthorized",
        "snapshot-stale-scope",
        "snapshot-policy-denied",
        "snapshot-conflict",
        "snapshot-not-ready",
        "snapshot-invalid-request",
        "snapshot-source-failed",
        "invalid-value",
        "retired",
        "timeout",
        "conflict",
        "cleanup-failed",
        "persistence-failed",
        "policy-failed",
        "policy-update-busy",
        "policy-denied",
        "not-ready",
        "busy",
        "stale-scope",
        "source-lost",
        "unavailable",
    }
)


@dataclass(frozen=True)
class AdapterResources:
    node: Path
    entry: Path
    build_id: str = "halo-homekit-development-v1"

    def __post_init__(self):
        if not self.node.is_absolute() or not self.entry.is_absolute():
            raise ValueError("HomeKit resource paths must be absolute")
        for path in (self.node, self.entry):
            if not stat.S_ISREG(path.stat().st_mode):
                raise ValueError("HomeKit resource is not a regular file")
        if not os.access(self.node, os.X_OK):
            raise ValueError("HomeKit Node runtime is not executable")


def development_resources() -> AdapterResources:
    """Resolve verified installed resources or an already-built development tree.

    Ordinary launch never downloads, builds, installs or resolves npm packages.
    A standalone Python wheel without the managed adapter remains unavailable.
    """
    from halo_server.release_execution import managed_context

    release = managed_context()
    if release is not None:
        return AdapterResources(release.node, release.entry, build_id=release.build_id)
    source = Path(__file__).resolve().parents[3]
    if not (source / "pyproject.toml").is_file():
        raise FileNotFoundError("HomeKit adapter resources are not installed")
    node = shutil.which("node")
    if node is None:
        raise FileNotFoundError("HomeKit Node runtime is unavailable")
    return AdapterResources(Path(node).resolve(), source / "homekit-adapter/dist/src/main.js")


class HomeKitBridge:
    """One adapter owner with coalesced inventory updates and honest health."""

    def __init__(
        self,
        controller,
        backend,
        *,
        resources=None,
        runtime_factory=HomeKitRuntime,
        paths=None,
        journal=None,
        incidents=dict,
        source_state=lambda: "offline",
        live_manager=None,
        snapshot_manager=None,
        recording_manager=None,
        publication_enabled=True,
    ):
        from halo_server.media.homekit_live import HomeKitLiveManager
        from halo_server.media.homekit_recording import HomeKitRecordingManager
        from halo_server.media.snapshots import HomeKitSnapshots

        self._paths, self._backend = paths, backend
        self._recording_incidents, self._recording_source_state = incidents, source_state
        self._publication_enabled = publication_enabled
        self._live = live_manager or HomeKitLiveManager(controller, on_ended=self._video_ended)
        self._snapshots = snapshot_manager or HomeKitSnapshots(controller)
        # S11 prepares bounded native samples without publishing HKSV or
        # starting capture. Recording policy/HDS transport are separate gates.
        self._recording = recording_manager or HomeKitRecordingManager(controller)
        self._recording_store = (
            RecordingSettingsStore(paths.homekit_recording_settings_file)
            if paths is not None
            else None
        )
        self._recording_settings = RecordingSettings()
        self._recording_available = False
        self._recording_lock = asyncio.Lock()
        self._recording_power = {}
        self._recording_policy = RecordingPolicyOwner(
            controller,
            self._recording.captures,
            self._recording_for_scope,
            changed=self.notify_inventory,
        )
        self._store = RegistryStore(paths.homekit_registry_file) if paths is not None else None
        self._registry = CameraRegistry()
        self._audio_store = (
            AudioSettingsStore(paths.homekit_audio_settings_file) if paths is not None else None
        )
        self._audio_settings = AudioSettings()
        self._audio_available = False
        self._audio_lock = asyncio.Lock()
        self._registry_lock = asyncio.Lock()
        self._config_generation = 0
        self._config_signature = None
        self._publication_status = None
        self._diagnostic_last = {}
        self._events = (
            EventPump(
                journal,
                controller,
                self._send_event,
                incidents=incidents,
                source_state=source_state,
            )
            if journal is not None
            else None
        )
        self._resources = resources
        self._runtime_factory = runtime_factory
        self._broker = HomeKitBroker(
            controller,
            backend,
            changed=self.notify_inventory,
            live_manager=self._live,
            snapshot_manager=self._snapshots,
            audio_settings=self._audio_for_scope,
            recording_manager=self._recording,
            recording_authority=self._recording_for_scope,
            recording_policy=self._recording_policy,
            recording_event=self._recording_event_for_scope,
            selection=lambda: tuple(
                row.camera.scope
                for row in self._registry.resolve(backend.inventory())
                if not self._pairing_admin.denied(*row.identity.key)
            ),
        )
        self._pairing_admin = PairingAdministration(self, paths)
        self._runtime = None
        self._task: asyncio.Task | None = None
        self._updates = asyncio.Event()
        self._stopping = False
        self._stop_lock = asyncio.Lock()
        self._inventory_apply_lock = asyncio.Lock()
        self.state, self.reason = "stopped", None

    def start(self) -> None:
        if self._task is None:
            if self._runtime is not None and (
                self._runtime.boot_id is not None
                or getattr(self._runtime, "retirement_pending", False)
            ):
                raise RuntimeError("Previous HomeKit adapter retirement is incomplete")
            if self._broker.cleanup_failed:
                raise RuntimeError("Previous native source retirement is incomplete")
            self._stopping = False
            self.state, self.reason = "starting", None
            self._task = asyncio.create_task(self._run(), name="homekit-service")

    def notify_inventory(self) -> None:
        self._updates.set()

    def notify_event(self, event=None, *, received_at=None) -> None:
        if self._events is not None:
            self._events.notify(event, received_at=received_at)

    async def _send_event(self, kind, payload, *, admission=None):
        if self._runtime is None:
            raise HomeKitOperationError("closed")
        await self._runtime.send(kind, payload, admission=admission)

    async def _video_ended(self, boot, scope, session_id, code):
        from dataclasses import asdict

        from halo_server.homekit.broker import _code

        runtime = self._runtime
        if runtime is not None and runtime.boot_id == boot and runtime.state == "ready":
            await runtime.send(
                "video_ended",
                {
                    "scope": asdict(scope),
                    "session_id": session_id,
                    "code": _code(HomeKitOperationError(code)),
                },
            )

    async def owner_pairing(self, method, params):
        return await self._pairing_admin.handle(method, params)

    async def _load_registry(self):
        await self._pairing_admin.load()
        if self._store is not None:
            inventory = self._backend.inventory()
            self._observe_recording_power(inventory)
            async with self._registry_lock:
                self._registry = await owned_thread(self._store.reconcile, inventory)
                if self._audio_store is not None:
                    async with self._audio_lock:
                        try:
                            saved = await owned_thread(
                                self._audio_store.reconcile, self._registry, inventory
                            )
                            self._install_audio_settings(saved)
                        except AudioSettingsError:
                            self._audio_available = False
                            self._fence_audio_settings()
                            raise
                        self._audio_available = True
                if self._recording_store is not None:
                    async with self._recording_lock:
                        try:
                            saved = await owned_thread(
                                self._recording_store.reconcile, self._registry, inventory
                            )
                            self._install_recording_settings(saved)
                        except RecordingSettingsError:
                            self._recording_available = False
                            self._fence_recording_settings()
                        else:
                            self._recording_available = True

    def _observe_recording_power(self, inventory):
        observed = {
            camera.scope.source_key: mains_confirmed(camera) for camera in inventory.cameras
        }
        boot = self._broker.boot_id
        if boot is not None:
            for camera in self._broker.controller.inventory().cameras:
                key = camera.scope.source_key
                if self._recording_power.get(key) and not observed.get(key, False):
                    self._broker.controller.fence_camera(boot, camera.scope)
        self._recording_power = observed

    def _recording_row(self, scope):
        if not self._recording_available:
            return None
        row = self._recording_settings.camera(scope.profile_id, scope.camera_id)
        identity = next(
            (
                row
                for row in self._registry.entries
                if row.key == (scope.profile_id, scope.camera_id)
            ),
            None,
        )
        return (
            row
            if row is not None
            and identity is not None
            and row.accessory_id == identity.accessory_uuid
            else None
        )

    def _recording_for_scope(self, scope):
        row = self._recording_row(scope)
        if row is None:
            return None
        camera = next(
            (
                camera
                for camera in self._backend.inventory().cameras
                if camera.scope.source_key == scope.source_key
            ),
            None,
        )
        return RecordingAuthority(
            row.preferences.active,
            row.preferences.audio_active,
            row.preferences.configuration,
            row.owner_warm,
            mains_confirmed(camera),
        )

    def _recording_event_for_scope(self, scope):
        if str(self._recording_source_state()) != "connected" or not self._backend.scope_current(
            scope
        ):
            return None
        incident = self._recording_incidents().get(scope.camera_id)
        if incident is None or incident.started_at is None or incident.quiet_deadline is None:
            return None
        return RecordingEvent(incident.generation, incident.started_at, incident.quiet_deadline)

    def _fence_recording_settings(self, saved=None):
        boot = self._broker.boot_id
        if boot is not None:
            for camera in self._broker.controller.inventory().cameras:
                key = camera.scope.profile_id, camera.scope.camera_id
                if saved is None or privacy_signature(
                    self._recording_settings.camera(*key)
                ) != privacy_signature(saved.camera(*key)):
                    self._broker.controller.fence_camera(boot, camera.scope)

    def _install_recording_settings(self, saved):
        self._fence_recording_settings(saved)
        self._recording_settings = saved

    def _fence_audio_settings(self, saved=None):
        # Legitimate local store writers can race cached HAP writes. Durable
        # truth is authoritative even when a child ignores refreshed config.
        # Revoke old delivery synchronously before exposing changed preferences.
        controller = self._broker.controller
        boot = self._broker.boot_id
        if boot is not None:
            for camera in controller.inventory().cameras:
                key = camera.scope.profile_id, camera.scope.camera_id
                if saved is None or self._audio_settings.camera(*key) != saved.camera(*key):
                    controller.fence_camera(boot, camera.scope)

    def _install_audio_settings(self, saved):
        self._fence_audio_settings(saved)
        self._audio_settings = saved

    def _audio_for_scope(self, scope):
        if not self._audio_available:
            return None
        row = self._audio_settings.camera(scope.profile_id, scope.camera_id)
        identity = next(
            (
                row
                for row in self._registry.entries
                if row.key == (scope.profile_id, scope.camera_id)
            ),
            None,
        )
        return (
            row
            if row is not None
            and identity is not None
            and row.accessory_id == identity.accessory_uuid
            else None
        )

    async def _ready(self, boot):
        await self._load_registry()
        if self._events is not None:
            self._events.start(boot)
        await self._broker.ready(boot)
        await self._recording_policy.ready(boot)
        self._config_signature = None
        self._publication_status = None
        await self._configure()

    async def _retired(self, boot):
        self._publication_status = None
        from halo_server.media.homekit_audio_codec import retire_audio_workers
        from halo_server.media.recording import retire_recording_workers
        from halo_server.media.snapshot_codec import retire_snapshot_workers
        from halo_server.media.transcode import retire_video_workers

        failures = []
        if self._broker.controller.boot_id == boot:
            self._broker.controller.fence()
        try:
            await self._recording_policy.retired(boot)
        except Exception as exc:
            failures.append(exc)
        try:
            await self._live.retired(boot)
        except Exception as exc:
            failures.append(exc)
        try:
            await self._snapshots.retired(boot)
        except Exception as exc:
            failures.append(exc)
        try:
            await self._recording.retired(boot)
        except Exception as exc:
            failures.append(exc)
        if self._events is not None:
            try:
                await self._events.stop(boot)
            except Exception as exc:
                failures.append(exc)
        try:
            await self._broker.retired(boot)
        except Exception as exc:
            failures.append(exc)
        try:
            await retire_video_workers()
        except Exception as exc:
            failures.append(exc)
        try:
            await retire_audio_workers()
        except Exception as exc:
            failures.append(exc)
        try:
            await retire_snapshot_workers()
        except Exception as exc:
            failures.append(exc)
        try:
            await retire_recording_workers()
        except Exception as exc:
            failures.append(exc)
        if failures:
            raise ExceptionGroup("Native bridge retirement incomplete", failures)
        self._config_signature = None

    async def _request(self, message):
        if message["kind"] == "control_state_request":
            data, boot = message["payload"], self._broker._require_boot(message)
            scope = MediaScope(**data["scope"])
            result = {
                "scope": data["scope"],
                "operation_id": data["operation_id"],
                "light": None,
                "siren": None,
            }
            try:
                self._broker.controller.authorize(boot, scope, "controls")
                light, siren = self._backend.control_state(
                    scope, admission=lambda: self._control_current(boot, scope)
                )
                self._broker.controller.authorize(boot, scope, "controls")
            except HomeKitOperationError:
                return result | {"status": "rejected", "code": "stale-scope"}
            return result | {"status": "accepted", "code": "ok", "light": light, "siren": siren}
        if message["kind"] == "recording_settings_update":
            data, boot = message["payload"], self._broker._require_boot(message)
            result = {"scope": data["scope"]}
            scope = MediaScope(**data["scope"])

            def camera_revision():
                row = self._recording_settings.camera(scope.profile_id, scope.camera_id)
                return row.preferences_revision if row is not None else 0

            try:
                saved = await self.set_recording_settings(
                    boot,
                    MediaScope(**data["scope"]),
                    data["expected_revision"],
                    RecordingPreferences(
                        data["active"],
                        data["audio_active"],
                        configuration_from_wire(data["configuration"]),
                    ),
                )
            except RecordingSettingsConflict:
                return result | {
                    "accepted": False,
                    "code": "conflict",
                    "revision": camera_revision(),
                }
            except RecordingSettingsError:
                return result | {
                    "accepted": False,
                    "code": "not-ready",
                    "revision": camera_revision(),
                }
            except HomeKitOperationError:
                return result | {
                    "accepted": False,
                    "code": "stale-scope",
                    "revision": camera_revision(),
                }
            return result | {
                "accepted": True,
                "code": "ok",
                "revision": saved.camera(scope.profile_id, scope.camera_id).preferences_revision,
            }
        if message["kind"] == "audio_settings_update":
            data, boot = message["payload"], self._broker._require_boot(message)
            result = {"scope": data["scope"]}
            try:
                saved = await self.set_audio_settings(
                    boot,
                    MediaScope(**data["scope"]),
                    data["expected_revision"],
                    AudioPreferences(**data["settings"]),
                )
            except AudioSettingsConflict:
                return result | {
                    "accepted": False,
                    "code": "conflict",
                    "revision": self._audio_settings.revision,
                }
            except AudioSettingsError:
                return result | {
                    "accepted": False,
                    "code": "not-ready",
                    "revision": self._audio_settings.revision,
                }
            except HomeKitOperationError:
                return result | {
                    "accepted": False,
                    "code": "stale-scope",
                    "revision": self._audio_settings.revision,
                }
            return result | {"accepted": True, "code": "ok", "revision": saved.revision}
        if message["kind"] == "event_sync" and self._events is not None:
            return await self._events.sync(message["boot_id"], message["payload"])
        return await self._broker.request(message)

    def _control_current(self, boot, scope):
        try:
            self._broker.controller.authorize(boot, scope, "controls")
        except HomeKitOperationError:
            return False
        return True

    async def _replied(self, message, reply):
        await self._broker.replied(message, reply)

    async def _notice(self, message):
        if message["kind"] == "accessory_status":
            data = message["payload"]
            inventory = self._backend.inventory()
            scope = (
                None
                if inventory.profile_id is None
                else {
                    "profile_id": inventory.profile_id,
                    "account_generation": inventory.account_generation,
                }
            )
            if (
                message["boot_id"] == self._broker.boot_id
                and data["scope"] == scope
                and data["revision"] == self._registry.revision
                and data["generation"] == self._config_generation
            ):
                self._publication_status = (message["boot_id"], dict(data))
            return
        if message["kind"] == "error":
            # Existing private notices carry only an allowlisted operation/code.
            # Never log a peer message, exception or negotiation payload verbatim.
            data = message["payload"]
            stage = data["code"].removeprefix("interop-")
            code = data["message"]
            if (
                self._broker.boot_id is not None
                and message["boot_id"] == self._broker.boot_id
                and data["code"].startswith("interop-")
                and stage in _INTEROP_STAGES
                and code in _INTEROP_CODES
            ):
                now = asyncio.get_running_loop().time()
                if now - self._diagnostic_last.get(stage, float("-inf")) >= 5:
                    self._diagnostic_last[stage] = now
                    _LOGGER.warning("HomeKit operation %s failed: %s", stage, code)
            return
        if message["kind"] == "status":
            return  # Never elevate media readiness from a peer's generic status.
        await self._broker.notice(message)

    async def _configure(self):
        runtime, boot = self._runtime, self._broker.boot_id
        if runtime is None or boot is None:
            return
        inventory = self._backend.inventory()
        registry = self._registry
        audio_settings = self._audio_settings
        recording_settings = self._recording_settings
        scope = (
            None
            if inventory.profile_id is None
            else {
                "profile_id": inventory.profile_id,
                "account_generation": inventory.account_generation,
            }
        )
        entries = (
            []
            if scope is None
            else [
                {
                    "camera_id": row.camera_id,
                    "accessory_id": row.accessory_uuid,
                    "username": row.username,
                    "doorbell": row.doorbell,
                    "audio": (
                        audio.wire(audio_settings.revision)
                        if (audio := audio_settings.camera(row.profile_id, row.camera_id))
                        is not None
                        and audio.accessory_id == row.accessory_uuid
                        else None
                    ),
                }
                for row in registry.selected(inventory.profile_id)
                if not self._pairing_admin.denied(*row.key)
            ]
        )
        current = {camera.scope.camera_id: camera for camera in inventory.cameras}
        for entry in entries:
            row = recording_settings.camera(inventory.profile_id, entry["camera_id"])
            entry["recording"] = (
                row.wire(
                    enabled=(
                        self._publication_enabled
                        and self._recording.ready
                        and self._recording_available
                        and row.owner_warm
                        and mains_confirmed(current.get(entry["camera_id"]))
                    ),
                )
                if row is not None and row.accessory_id == entry["accessory_id"]
                else {
                    "revision": 0,
                    "enabled": False,
                    "active": False,
                    "audio_active": False,
                    "configuration": None,
                }
            )
        media_ready = (
            self._publication_enabled
            and self._snapshots.ready
            and self._live.ready
            and self._audio_available
            and (
                not any(row["audio"] and row["audio"]["microphone"] for row in entries)
                or self._live.audio_ready
            )
        )
        signature = (scope, registry.revision, entries, media_ready)
        if signature == self._config_signature:
            return
        self._publication_status = None
        self._config_signature = None
        self._config_generation += 1
        generation = self._config_generation
        pages = [entries[index : index + 32] for index in range(0, len(entries), 32)] or [[]]
        for index, page in enumerate(pages):
            if self._broker.boot_id != boot:
                raise HomeKitOperationError("boot_changed")
            payload = {
                "scope": scope,
                "revision": registry.revision,
                "generation": generation,
                "page_index": index,
                "total_pages": len(pages),
                "entries": page,
                "media_ready": media_ready,
            }
            reply = await runtime.request(
                "accessory_config", payload, expected="accessory_config_ack"
            )
            if reply["accepted"] is not True or reply["code"] != "ok":
                raise HomeKitOperationError("configuration_rejected")
        if self._broker.boot_id != boot:
            raise HomeKitOperationError("boot_changed")
        self._config_signature = signature
        current = self._backend.inventory()
        if (
            registry != self._registry
            or audio_settings != self._audio_settings
            or recording_settings != self._recording_settings
            or (current.profile_id, current.account_generation)
            != (
                inventory.profile_id,
                inventory.account_generation,
            )
        ):
            self.notify_inventory()

    async def cameras(self):
        await self._load_registry()
        inventory = self._backend.inventory()
        return self._registry, inventory

    async def select(self, profile_id, account_generation, camera_ids, expected_revision):
        task = asyncio.create_task(
            self._select_owned(profile_id, account_generation, camera_ids, expected_revision)
        )
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await settle_owned(task)
            raise

    async def _select_owned(self, profile_id, account_generation, camera_ids, expected_revision):
        inventory = self._backend.inventory()
        if (inventory.profile_id, inventory.account_generation) != (profile_id, account_generation):
            raise HomeKitOperationError("stale_scope")
        if self._store is None:
            raise RegistryError("Native selection store is unavailable")
        async with self._registry_lock:
            try:
                registry = await owned_thread(
                    self._store.set_selected,
                    profile_id,
                    camera_ids,
                    expected_revision=expected_revision,
                )
            except RegistryError as exc:
                if exc.possibly_saved:
                    self._broker.controller.fence()
                    try:
                        self._registry = await owned_thread(self._store.load)
                    finally:
                        self.notify_inventory()
                raise
            changed = registry != self._registry
            self._registry = registry
            if changed:
                self._broker.controller.fence()
            self.notify_inventory()
        current = self._backend.inventory()
        if (current.profile_id, current.account_generation) != (profile_id, account_generation):
            raise HomeKitOperationError("stale_scope", possibly_sent=True)
        return self._registry

    async def set_audio_settings(self, boot, scope, revision, preferences):
        task = asyncio.create_task(self._set_audio_owned(boot, scope, revision, preferences))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await settle_owned(task)
            raise

    async def _set_audio_owned(self, boot, scope, revision, preferences):
        async with self._audio_lock:
            if boot != self._broker.boot_id:
                raise HomeKitOperationError("boot_changed")
            self._broker.controller.validate_selected_scope(boot, scope)
            row = self._audio_for_scope(scope)
            if row is None or self._audio_store is None:
                raise AudioSettingsError("Trusted audio preferences are unavailable")
            if revision != self._audio_settings.revision:
                self.notify_inventory()
                raise AudioSettingsConflict("Audio settings changed; reload before saving")
            changed = preferences != row.preferences
            if changed:
                self._broker.controller.fence_camera(boot, scope)
            try:
                saved = await owned_thread(
                    self._audio_store.set_preferences,
                    row.profile_id,
                    row.camera_id,
                    row.accessory_id,
                    preferences,
                    expected_revision=revision,
                )
            except AudioSettingsError:
                try:
                    recovered = await owned_thread(self._audio_store.load)
                    self._install_audio_settings(recovered)
                except AudioSettingsError:
                    self._audio_available = False
                    self._fence_audio_settings()
                finally:
                    self.notify_inventory()
                raise
            self._install_audio_settings(saved)
            self.notify_inventory()
            if boot != self._broker.boot_id or not self._backend.scope_current(scope):
                raise HomeKitOperationError("stale_scope", possibly_sent=changed)
            return self._audio_settings

    async def set_recording_settings(self, boot, scope, revision, preferences):
        task = asyncio.create_task(
            self._set_recording_owned(boot, scope, revision, preferences=preferences),
            name="homekit-recording-settings",
        )
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await settle_owned(task)
            raise

    async def set_recording_owner_warm(
        self, profile_id, account_generation, camera_id, expected_revision, enabled
    ):
        """Trusted local owner opt-in; no HAP message can invoke this setter."""
        task = asyncio.create_task(
            self._set_recording_owner_owned(
                profile_id, account_generation, camera_id, expected_revision, enabled
            ),
            name="homekit-recording-owner",
        )
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await settle_owned(task)
            raise

    async def _set_recording_owner_owned(self, profile, generation, camera_id, revision, enabled):
        if type(enabled) is not bool:
            raise RecordingSettingsError("Recording opt-in must be a boolean")
        inventory = self._backend.inventory()
        if (inventory.profile_id, inventory.account_generation) != (profile, generation):
            raise HomeKitOperationError("stale_scope")
        if enabled and not inventory.complete:
            raise HomeKitOperationError("stale_scope")
        row = self._recording_settings.camera(profile, camera_id)
        identity = next(
            (row for row in self._registry.selected(profile) if row.camera_id == camera_id), None
        )
        if row is None or identity is None or row.accessory_id != identity.accessory_uuid:
            raise RecordingSettingsError("Select the recording camera before changing its opt-in")
        camera = next(
            (camera for camera in inventory.cameras if camera.scope.camera_id == camera_id), None
        )
        if enabled and not mains_confirmed(camera):
            raise HomeKitOperationError("capability_unsupported")
        scope = next(
            (
                camera.scope
                for camera in self._broker.controller.inventory().cameras
                if camera.scope.profile_id == profile and camera.scope.camera_id == camera_id
            ),
            None,
        )
        return await self._set_recording_owned(
            self._broker.boot_id,
            scope,
            revision,
            owner_enabled=enabled,
            identity=(profile, generation, camera_id),
        )

    async def _set_recording_owned(
        self, boot, scope, revision, *, preferences=None, owner_enabled=None, identity=None
    ):
        async with self._recording_lock:
            if identity is None:
                if boot != self._broker.boot_id:
                    raise HomeKitOperationError("boot_changed")
                self._broker.controller.validate_selected_scope(boot, scope)
                profile, camera_id = scope.profile_id, scope.camera_id
            else:
                profile, generation, camera_id = identity
                current = self._backend.inventory()
                if (current.profile_id, current.account_generation) != (profile, generation):
                    raise HomeKitOperationError("stale_scope")
            row = self._recording_settings.camera(profile, camera_id)
            if not self._recording_available or row is None or self._recording_store is None:
                raise RecordingSettingsError("Trusted recording preferences are unavailable")
            current_revision = (
                row.preferences_revision
                if owner_enabled is None
                else self._recording_settings.revision
            )
            if revision != current_revision:
                self.notify_inventory()
                raise RecordingSettingsConflict("Recording settings changed; reload before saving")
            proposed = (
                replace(row, preferences=preferences)
                if owner_enabled is None
                else replace(row, owner_warm=owner_enabled)
            )
            changed = proposed != row
            if scope is not None and privacy_signature(proposed) != privacy_signature(row):
                self._broker.controller.fence_camera(boot, scope)
            try:
                method, argument = (
                    (self._recording_store.set_hap_preferences, preferences)
                    if owner_enabled is None
                    else (self._recording_store.set_owner_warm, owner_enabled)
                )
                saved = await owned_thread(
                    method,
                    profile,
                    camera_id,
                    row.accessory_id,
                    argument,
                    **(
                        {"expected_preferences_revision": revision}
                        if owner_enabled is None
                        else {"expected_revision": revision}
                    ),
                )
            except RecordingSettingsError:
                try:
                    recovered = await owned_thread(self._recording_store.load)
                    self._install_recording_settings(recovered)
                except RecordingSettingsError:
                    self._recording_available = False
                    self._fence_recording_settings()
                finally:
                    self.notify_inventory()
                raise
            self._install_recording_settings(saved)
            self.notify_inventory()
            current = self._backend.inventory()
            if identity is None:
                current_valid = boot == self._broker.boot_id and self._backend.scope_current(scope)
            else:
                current_valid = (current.profile_id, current.account_generation) == (
                    profile,
                    generation,
                )
            if not current_valid:
                raise HomeKitOperationError("stale_scope", possibly_sent=changed)
            return self._recording_settings

    def owner_summary(self) -> dict:
        """Read cached authority/publication facts without media or storage probes."""
        inventory = self._backend.inventory()
        runtime = self._runtime
        ready = runtime is not None and runtime.state == "ready"
        publication = None
        if self._config_signature is not None and self._publication_status is not None:
            notice_boot, notice = self._publication_status
            expected_scope, revision, entries, media = self._config_signature
            scope = (
                {
                    "profile_id": inventory.profile_id,
                    "account_generation": inventory.account_generation,
                }
                if inventory.profile_id is not None
                else None
            )
            if (
                ready
                and notice_boot == runtime.boot_id == self._broker.boot_id
                and notice["scope"] == expected_scope == scope
                and notice["revision"] == revision == self._registry.revision
                and notice["generation"] == self._config_generation
                and self._publication_enabled
                and media
            ):
                publication = notice
        selected = len(self._registry.selected(inventory.profile_id)) if inventory.profile_id else 0
        published = publication["published"] if publication is not None else None
        return {
            "enabled": True,
            "adapter_boot_id": runtime.boot_id if runtime else None,
            "state": (
                "published"
                if publication is not None
                and publication["configured"]
                == publication["published"]
                == publication["advertised"]
                == selected
                else "ready"
                if ready
                else self.state
            ),
            "registry_revision": self._registry.revision,
            "selected_count": selected,
            "published_count": published,
            "recording_revision": self._recording_settings.revision,
        }

    def status(self) -> dict:
        runtime = self._runtime
        bulk = getattr(runtime, "bulk_channel", None) if runtime is not None else None
        profile = self._backend.inventory().profile_id
        transport_ready = runtime is not None and runtime.state == "ready"
        reason = self.reason
        if not reason and not self._pairing_admin.available:
            reason = "pairing_maintenance_unavailable"
        if not reason and any(
            row.phase != "completed" for row in self._pairing_admin.journal.entries
        ):
            reason = "pairing_reset_pending"
        if runtime is not None and not transport_ready:
            reason = runtime.reason or "adapter_not_ready"
        if self._broker.cleanup_failed:
            reason = "native_cleanup_failed"
        if bulk is not None and bulk.cleanup_pending:
            reason = "bulk_cleanup_pending"
        if self._live.cleanup_failures:
            reason = "native_output_cleanup_failed"
        if self._snapshots.cleanup_failures:
            reason = "snapshot_cleanup_failed"
        if self._recording.cleanup_failures or self._recording.captures.cleanup_failures:
            reason = "recording_cleanup_failed"
        if self._recording_policy.cleanup_failures or self._broker.recording_cleanup_failures:
            reason = "recording_cleanup_failed"
        if self._recording_store is not None and not self._recording_available and transport_ready:
            reason = "recording_settings_unavailable"
        if self._audio_store is not None and not self._audio_available and transport_ready:
            reason = "audio_settings_unavailable"
        if self._events is not None and self._events.state == "failed":
            reason = "event_delivery_failed"
        if (
            not reason
            and transport_ready
            and self._live.capabilities is not None
            and not self._live.ready
        ):
            reason = "video_output_unavailable"
        if not reason and transport_ready and self._live.ready and not self._live.audio_ready:
            reason = "audio_output_unavailable"
        if not reason and transport_ready and not self._snapshots.ready:
            reason = "snapshot_output_unavailable"
        storage_recovery = False
        if self._paths is not None:
            try:
                directory = open_private_directory(self._paths.homekit_pairing_directory)
            except (PathSecurityError, OSError):
                pass
            else:
                try:
                    os.stat(".hap-storage-intent", dir_fd=directory, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                except OSError:
                    reason = "pairing_storage_unavailable"
                else:
                    storage_recovery = True
                    reason = "storage_recovery_required"
                finally:
                    os.close(directory)
        selected = self._broker.controller.inventory().cameras
        warm = tuple(self._recording_policy.status(camera.scope) for camera in selected)
        recording_enabled = any(
            state is not None and state.owner_opt_in and state.mains_confirmed
            for camera in selected
            if (state := self._recording_for_scope(camera.scope)) is not None
        )
        recording_delivery = (
            transport_ready
            and self._recording.ready
            and self._recording_available
            and any(state.history_ready for state in warm)
        )
        fallback = (
            None
            if not transport_ready or recording_delivery
            else "media_not_implemented"
            if not self._live.ready
            else "recording_output_unavailable"
            if not self._recording.ready
            else "recording_prebuffer_warming"
            if recording_enabled
            else "recording_disabled"
        )
        publication = None
        if self._config_signature is not None and self._publication_status is not None:
            notice_boot, notice = self._publication_status
            expected_scope, expected_revision, expected_entries, expected_media = (
                self._config_signature
            )
            inventory = self._backend.inventory()
            current_scope = (
                None
                if inventory.profile_id is None
                else {
                    "profile_id": inventory.profile_id,
                    "account_generation": inventory.account_generation,
                }
            )
            if (
                transport_ready
                and notice_boot == runtime.boot_id == self._broker.boot_id
                and notice["scope"] == expected_scope == current_scope
                and notice["revision"] == expected_revision == self._registry.revision
                and notice["generation"] == self._config_generation
            ):
                publication = notice
        else:
            expected_entries, expected_media = [], False
        camera_ready = bool(
            publication is not None
            and self._publication_enabled
            and expected_media
            and expected_entries
            and not reason
            and publication["configured"]
            == publication["published"]
            == publication["advertised"]
            == len(expected_entries)
        )
        return {
            "requested": True,
            "boot_id": runtime.boot_id if runtime is not None else None,
            "pairing_storage_recovery_required": storage_recovery,
            "pairing_maintenance_pending": sum(
                row.phase != "completed" for row in self._pairing_admin.journal.entries
            ),
            "pairing_maintenance_available": self._pairing_admin.available,
            "state": "degraded" if reason else "control_ready" if transport_ready else self.state,
            "reason": reason or fallback,
            "transport_ready": transport_ready,
            "camera_ready": camera_ready,
            "publication_configured": publication["configured"] if publication is not None else 0,
            "publication_listening": publication["published"] if publication is not None else 0,
            "publication_advertised": publication["advertised"] if publication is not None else 0,
            "real_ring_apple_qualified": False,
            "live_stream_diagnostics": safe_diagnostics(self._live),
            "live_video_ready": self._live.ready,
            "live_video_resources": self._live.pending_resources,
            "live_video_cleanup_failures": self._live.cleanup_failures,
            "live_audio_ready": self._live.audio_ready,
            "recording_samples_ready": self._recording.ready,
            "recording_delivery_ready": recording_delivery,
            "recording_resources": self._recording.pending_resources,
            "recording_capture_resources": self._recording.captures.pending_resources,
            "recording_cleanup_failures": self._recording.cleanup_failures,
            "recording_capture_cleanup_failures": self._recording.captures.cleanup_failures,
            "recording_broker_resources": self._broker.recording_resources,
            "recording_warm_resources": self._recording_policy.pending_resources,
            "recording_warm_cleanup_failures": self._recording_policy.cleanup_failures,
            "recording_settings_revision": self._recording_settings.revision,
            "recording_warm_ready": sum(state.history_ready for state in warm),
            "snapshots_ready": self._snapshots.ready,
            "snapshot_resources": self._snapshots.pending_resources,
            "snapshot_cleanup_failures": self._snapshots.cleanup_failures,
            "snapshot_cache_bytes": self._snapshots.cache_bytes,
            "bulk_resources": bulk.pending_resources if bulk is not None else 0,
            "bulk_cleanup_pending": bulk.cleanup_pending if bulk is not None else 0,
            "audio_settings_revision": self._audio_settings.revision,
            "restart_count": runtime.restart_count if runtime is not None else 0,
            "selected_cameras": len(self._registry.selected(profile)) if profile else 0,
            "registry_revision": self._registry.revision,
            "event_state": self._events.state if self._events is not None else "disabled",
        }

    async def _run(self) -> None:
        try:
            resources = self._resources or development_resources()
            await self._live.qualify()
            await self._snapshots.qualify()
            await self._recording.qualify()
            self._runtime = self._runtime_factory(
                resources.node,
                resources.entry,
                build_id=resources.build_id,
                on_ready=self._ready,
                on_retired=self._retired,
                on_request=self._request,
                on_notice=self._notice,
                on_replied=self._replied,
                pairing_directory=self._paths.homekit_pairing_directory
                if self._paths is not None
                else None,
            )
            self._broker.runtime = self._runtime
            await self._runtime.start()
            self.state, self.reason = "control_ready", None
            while not self._stopping:
                await self._updates.wait()
                self._updates.clear()
                if self._broker.cleanup_failed:
                    await self._runtime.stop()
                    self.state, self.reason = "degraded", "native_cleanup_failed"
                    return
                async with self._inventory_apply_lock:
                    if self._runtime.state == "ready":
                        await self._load_registry()
                        await self._live.refresh()
                        self._snapshots.refresh()
                        await self._recording.refresh()
                        await self._broker.refresh()
                        await self._recording_policy.refresh(self._broker.boot_id)
                        await self._configure()
                        self.notify_event()
        except asyncio.CancelledError:
            raise
        except (OSError, ValueError):
            self.state, self.reason = "degraded", "adapter_resources_unavailable"
        except Exception:
            self.state, self.reason = "degraded", "adapter_control_failed"
        finally:
            if self._runtime is not None and self.state == "degraded":
                if self._broker.controller.boot_id == self._runtime.boot_id:
                    self._broker.controller.fence()
                with contextlib.suppress(Exception):
                    await self._runtime.stop()

    async def stop(self) -> None:
        task = asyncio.create_task(self._stop_owned(), name="homekit-service-stop")
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await settle_owned(task)
            raise

    async def _stop_owned(self) -> None:
        from halo_server.media.homekit_audio_codec import retire_audio_workers
        from halo_server.media.recording import retire_recording_workers
        from halo_server.media.snapshot_codec import retire_snapshot_workers
        from halo_server.media.transcode import retire_video_workers

        async with self._stop_lock:
            self._stopping = True
            boot = getattr(self._runtime, "boot_id", None)
            if boot is not None and self._broker.controller.boot_id == boot:
                self._broker.controller.fence()
            task = self._task
            if task is not None:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                self._task = None
            failures = []
            if self._runtime is not None:
                try:
                    await self._runtime.stop()
                except Exception as exc:
                    failures.append(exc)
            try:
                await self._recording_policy.close()
            except Exception as exc:
                failures.append(exc)
            try:
                await retire_video_workers()
            except Exception as exc:
                failures.append(exc)
            try:
                await retire_audio_workers()
            except Exception as exc:
                failures.append(exc)
            try:
                await self._snapshots.close()
                await retire_snapshot_workers()
            except Exception as exc:
                failures.append(exc)
            try:
                await self._recording.close()
            except Exception as exc:
                failures.append(exc)
            try:
                await retire_recording_workers()
            except Exception as exc:
                failures.append(exc)
            if failures:
                raise ExceptionGroup("Native adapter/output retirement incomplete", failures)
            self.state = "stopped"
