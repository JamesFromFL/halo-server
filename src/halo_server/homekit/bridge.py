"""Daemon integration for the supervised native adapter, independent of Ring IO."""

from __future__ import annotations

import asyncio
import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path

from halo_server.homekit.broker import HomeKitBroker
from halo_server.homekit.runtime import HomeKitRuntime
from halo_server.media.manager import settle_owned


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

    def __init__(self, controller, backend, *, resources=None, runtime_factory=HomeKitRuntime):
        self._resources = resources
        self._runtime_factory = runtime_factory
        self._broker = HomeKitBroker(controller, backend, changed=self.notify_inventory)
        self._runtime = None
        self._task: asyncio.Task | None = None
        self._updates = asyncio.Event()
        self._stopping = False
        self._stop_lock = asyncio.Lock()
        self.state, self.reason = "stopped", None

    def start(self) -> None:
        if self._task is None:
            if self._runtime is not None and self._runtime.boot_id is not None:
                raise RuntimeError("Previous HomeKit adapter retirement is incomplete")
            if self._broker.cleanup_failed:
                raise RuntimeError("Previous native source retirement is incomplete")
            self._stopping = False
            self.state, self.reason = "starting", None
            self._task = asyncio.create_task(self._run(), name="homekit-service")

    def notify_inventory(self) -> None:
        self._updates.set()

    def status(self) -> dict:
        runtime = self._runtime
        transport_ready = runtime is not None and runtime.state == "ready"
        reason = self.reason
        if runtime is not None and not transport_ready:
            reason = runtime.reason or "adapter_not_ready"
        if self._broker.cleanup_failed:
            reason = "native_cleanup_failed"
        return {
            "requested": True,
            "state": "degraded" if reason else "control_ready" if transport_ready else self.state,
            "reason": reason or ("accessories_not_implemented" if transport_ready else None),
            "transport_ready": transport_ready,
            "camera_ready": False,
            "restart_count": runtime.restart_count if runtime is not None else 0,
        }

    async def _run(self) -> None:
        try:
            resources = self._resources or development_resources()
            self._runtime = self._runtime_factory(
                resources.node,
                resources.entry,
                build_id=resources.build_id,
                on_ready=self._broker.ready,
                on_retired=self._broker.retired,
                on_request=self._broker.request,
                on_notice=self._broker.notice,
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
                    await self._broker.refresh()
        except asyncio.CancelledError:
            raise
        except (OSError, ValueError):
            self.state, self.reason = "degraded", "adapter_resources_unavailable"
        except Exception:
            self.state, self.reason = "degraded", "adapter_control_failed"

    async def stop(self) -> None:
        task = asyncio.create_task(self._stop_owned(), name="homekit-service-stop")
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await settle_owned(task)
            raise

    async def _stop_owned(self) -> None:
        async with self._stop_lock:
            self._stopping = True
            task = self._task
            if task is not None:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                self._task = None
            if self._runtime is not None:
                await self._runtime.stop()
            self.state = "stopped"
