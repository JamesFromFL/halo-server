"""Daemon integration for the supervised native adapter, independent of Ring IO."""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path

from halo_server.homekit.broker import HomeKitBroker
from halo_server.homekit.events import EventPump
from halo_server.homekit.models import HomeKitOperationError
from halo_server.homekit.registry import CameraRegistry, RegistryError, RegistryStore
from halo_server.homekit.runtime import HomeKitRuntime
from halo_server.media.manager import owned_thread, settle_owned


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
    """Resolve an already-built source tree; never install or build on startup.

    Release resource manifests and immutable installed runtime selection are S14.
    A wheel without bundled adapter resources reports degraded local status.
    """
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
        publication_enabled=True,
    ):
        from halo_server.media.homekit_live import HomeKitLiveManager

        self._paths, self._backend = paths, backend
        self._publication_enabled = publication_enabled
        self._live = live_manager or HomeKitLiveManager(controller, on_ended=self._video_ended)
        self._store = RegistryStore(paths.homekit_registry_file) if paths is not None else None
        self._registry = CameraRegistry()
        self._registry_lock = asyncio.Lock()
        self._config_generation = 0
        self._config_signature = None
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
            selection=lambda: tuple(
                row.camera.scope for row in self._registry.resolve(backend.inventory())
            ),
        )
        self._runtime = None
        self._task: asyncio.Task | None = None
        self._updates = asyncio.Event()
        self._stopping = False
        self._stop_lock = asyncio.Lock()
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

    async def _load_registry(self):
        if self._store is not None:
            inventory = self._backend.inventory()
            async with self._registry_lock:
                self._registry = await owned_thread(self._store.reconcile, inventory)

    async def _ready(self, boot):
        await self._load_registry()
        if self._events is not None:
            self._events.start(boot)
        await self._broker.ready(boot)
        self._config_signature = None
        await self._configure()

    async def _retired(self, boot):
        from halo_server.media.transcode import retire_video_workers

        failures = []
        if self._broker.controller.boot_id == boot:
            self._broker.controller.fence()
        try:
            await self._live.retired(boot)
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
        if failures:
            raise ExceptionGroup("Native bridge retirement incomplete", failures)
        self._config_signature = None

    async def _request(self, message):
        if message["kind"] == "event_sync" and self._events is not None:
            return await self._events.sync(message["boot_id"], message["payload"])
        return await self._broker.request(message)

    async def _notice(self, message):
        if message["kind"] in {"status", "error"}:
            return  # Never elevate media readiness from a peer's generic status.
        await self._broker.notice(message)

    async def _configure(self):
        runtime, boot = self._runtime, self._broker.boot_id
        if runtime is None or boot is None:
            return
        inventory = self._backend.inventory()
        registry = self._registry
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
                }
                for row in registry.selected(inventory.profile_id)
            ]
        )
        media_ready = self._publication_enabled and self._live.ready
        signature = (scope, registry.revision, entries, media_ready)
        if signature == self._config_signature:
            return
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
        if registry != self._registry or (current.profile_id, current.account_generation) != (
            inventory.profile_id,
            inventory.account_generation,
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

    def status(self) -> dict:
        runtime = self._runtime
        profile = self._backend.inventory().profile_id
        transport_ready = runtime is not None and runtime.state == "ready"
        reason = self.reason
        if runtime is not None and not transport_ready:
            reason = runtime.reason or "adapter_not_ready"
        if self._broker.cleanup_failed:
            reason = "native_cleanup_failed"
        if self._live.cleanup_failures:
            reason = "native_output_cleanup_failed"
        if self._events is not None and self._events.state == "failed":
            reason = "event_delivery_failed"
        if (
            not reason
            and transport_ready
            and self._live.capabilities is not None
            and not self._live.ready
        ):
            reason = "video_output_unavailable"
        return {
            "requested": True,
            "state": "degraded" if reason else "control_ready" if transport_ready else self.state,
            "reason": reason
            or (
                "audio_and_snapshots_not_implemented"
                if transport_ready and self._live.ready
                else "media_not_implemented"
                if transport_ready
                else None
            ),
            "transport_ready": transport_ready,
            "camera_ready": False,
            "live_video_ready": self._live.ready,
            "live_video_resources": self._live.pending_resources,
            "live_video_cleanup_failures": self._live.cleanup_failures,
            "restart_count": runtime.restart_count if runtime is not None else 0,
            "selected_cameras": len(self._registry.selected(profile)) if profile else 0,
            "registry_revision": self._registry.revision,
            "event_state": self._events.state if self._events is not None else "disabled",
        }

    async def _run(self) -> None:
        try:
            resources = self._resources or development_resources()
            await self._live.qualify()
            self._runtime = self._runtime_factory(
                resources.node,
                resources.entry,
                build_id=resources.build_id,
                on_ready=self._ready,
                on_retired=self._retired,
                on_request=self._request,
                on_notice=self._notice,
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
                if self._runtime.state == "ready":
                    await self._load_registry()
                    await self._live.refresh()
                    await self._broker.refresh()
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
                await retire_video_workers()
            except Exception as exc:
                failures.append(exc)
            if failures:
                raise ExceptionGroup("Native adapter/output retirement incomplete", failures)
            self.state = "stopped"
