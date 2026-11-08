"""Explicit local-owner capture controls; never start a camera or change Home policy."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import threading
from uuid import UUID

from halo_server.api.admin import AdminProtocolError
from halo_server.media.manager import owned_thread
from halo_server.paths import open_private_directory

MAX_RETAINED_CAPTURES = 3
MIN_FREE_BYTES = 384 * 1024 * 1024


class ScopedTrace:
    """Fence recording immediately when the camera/account owner is replaced."""

    def __init__(self, capture, current):
        self.capture = capture
        self.current = current
        self.revoked = threading.Event()

    def _allowed(self):
        if self.closed:
            return False
        try:
            current = self.current()
            if current is None:
                self.capture.note_authority_contention()
                return False
            if current:
                return True
        except Exception:
            pass
        self.revoked.set()
        return False

    @property
    def closed(self):
        return self.revoked.is_set() or self.capture.closed

    def event(self, stage, track, **fields):
        if self._allowed():
            with contextlib.suppress(Exception):
                return self.capture.event(stage, track, **fields)
        return False

    def media(self, stage, track, payload, **fields):
        if self._allowed():
            with contextlib.suppress(Exception):
                return self.capture.media(stage, track, payload, **fields)
        return False

    def trigger(self, reason="manual"):
        if self._allowed():
            with contextlib.suppress(Exception):
                return self.capture.trigger(reason)
        return False

    def allocate_stream(self):
        if self._allowed():
            with contextlib.suppress(Exception):
                return self.capture.allocate_stream()
        return 0


class OwnerCapture:
    def __init__(self, administration):
        self.owner = administration
        self.lock = asyncio.Lock()
        self.capture = self.trace = self.scope = self.request = self.path = None

    def trace_for(self, camera_id):
        try:
            if (
                self.scope is not None
                and self.scope.camera_id == camera_id
                and self.trace is not None
                and not self.trace.closed
                and self.owner.control_backend.scope_current(self.scope)
                and self.capture.status().get("accepting", False)
            ):
                return self.trace
        except Exception:
            if self.trace is not None:
                self.trace.revoked.set()
            if self.capture is not None:
                with contextlib.suppress(Exception):
                    self.capture.fence()
        return None

    def status(self):
        if self.capture is None:
            return {"state": "idle", "accepting": False, "capture_id": None}
        return self.capture.status() | {
            "capture_id": self.request["capture_id"],
            "directory": str(self.path),
            "authority_current": self.owner.control_backend.scope_current(self.scope),
        }

    async def handle(self, method, params):
        async with self.lock:
            if method == "stream_capture_start":
                return await self._start(params)
            if method == "stream_capture_status":
                _only(params, set())
                return self.status()
            _only(params, {"capture_id"})
            if self.request is None or params.get("capture_id") != self.request["capture_id"]:
                raise AdminProtocolError("stale_capture", "This capture is not the current owner")
            if method == "stream_capture_mark":
                self.trace.trigger("manual")
            elif method == "stream_capture_stop":
                await self._close()
            else:
                raise AdminProtocolError("unknown_method", "Unknown capture operation")
            return self.status()

    async def _start(self, params):
        _only(params, {"capture_id", "camera_id", "seconds", "record_media"})
        identifier, camera_id = params.get("capture_id"), params.get("camera_id")
        seconds = params.get("seconds", 240)
        try:
            valid_id = type(identifier) is str and str(UUID(identifier)) == identifier
        except (ValueError, AttributeError):
            valid_id = False
        if (
            not valid_id
            or type(camera_id) is not str
            or not 1 <= len(camera_id) <= 128
            or type(seconds) is not int
            or not 30 <= seconds <= 300
            or params.get("record_media") is not True
        ):
            raise AdminProtocolError(
                "bad_params", "Capture needs an ID, camera, 30–300 seconds and record_media=true"
            )
        request = dict(params, seconds=seconds)
        if self.request is not None and identifier == self.request["capture_id"]:
            if request != self.request:
                raise AdminProtocolError("capture_conflict", "Capture ID has different parameters")
            return self.status()
        if self.capture is not None and not self.capture.status().get("finished", False):
            raise AdminProtocolError("busy", "The preceding diagnostic writer has not finished")
        service, backend = self.owner.service, self.owner.control_backend
        inventory = backend.inventory()
        camera = next((row for row in inventory.cameras if row.scope.camera_id == camera_id), None)
        if not inventory.complete or camera is None or not camera.capabilities.video:
            raise AdminProtocolError("camera_unavailable", "Camera authority is unavailable")
        scope = camera.scope
        async with service._camera_media_lock(camera_id):
            if not backend.scope_current(scope):
                raise AdminProtocolError("stale_scope", "Camera authority changed")
            if service._captures.retiring:
                raise AdminProtocolError("busy", "Wait for camera cleanup before capture")
            for session in service._media_sessions.values():
                snapshot = session.snapshot()
                if snapshot.lease_count or snapshot.state != "idle":
                    raise AdminProtocolError("busy", "Close live views before starting capture")
            native = service._homekit_bridge.status() if service._homekit_bridge else {}
            if any(
                native.get(key, 0)
                for key in (
                    "live_video_resources",
                    "recording_resources",
                    "recording_capture_resources",
                    "recording_warm_resources",
                    "snapshot_resources",
                    "bulk_resources",
                )
            ):
                raise AdminProtocolError("busy", "Wait for media consumers to finish")
            # Publishing in the owned worker also retains the capture if the admin caller leaves.
            try:
                await owned_thread(self._create, request, scope)
            except BaseException:
                if self.request == request:
                    await self._close()
                raise
            if not backend.scope_current(scope):
                await self._close()
                raise AdminProtocolError("stale_scope", "Camera authority changed during start")
            existing = service._media_sessions.get(camera_id)
            if existing is not None:
                try:
                    attached = await owned_thread(existing.set_diagnostic_trace, self.trace)
                    if not attached:
                        raise RuntimeError("Diagnostic source is not idle")
                except Exception as error:
                    await self._close()
                    raise AdminProtocolError(
                        "busy", "Camera source has not fully retired"
                    ) from error
            return self.status()

    def _create(self, request, scope):
        from halo_server.media.diagnostic_capture import DiagnosticCapture
        from halo_server.release_execution import release_status

        root = self.owner.service.paths.state_directory / "diagnostic-captures"
        descriptor = open_private_directory(root, create=True)
        try:
            with os.scandir(descriptor) as entries:
                for index, _ in enumerate(entries):
                    if index + 1 >= MAX_RETAINED_CAPTURES:
                        raise AdminProtocolError(
                            "capture_capacity", "Archive prior diagnostic captures first"
                        )
            if shutil.disk_usage(root).free < MIN_FREE_BYTES:
                raise AdminProtocolError(
                    "capture_capacity", "Insufficient diagnostic capture space"
                )
            path = root / request["capture_id"]
            capture = DiagnosticCapture(path, duration_seconds=request["seconds"])
            self.capture, self.path, self.scope, self.request = capture, path, scope, request
            self.trace = ScopedTrace(
                capture, lambda: self.owner.control_backend.diagnostic_scope_current(scope)
            )
            try:
                leaf = open_private_directory(path)
                try:
                    fd = os.open(
                        "owner.json",
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o600,
                        dir_fd=leaf,
                    )
                    with os.fdopen(fd, "w") as output:
                        json.dump(
                            {
                                "schema": 1,
                                "release": release_status(),
                                "capture_id": request["capture_id"],
                                "duration_seconds": request["seconds"],
                                "record_media": True,
                            },
                            output,
                        )
                        output.write("\n")
                finally:
                    os.close(leaf)
            except BaseException:
                self.trace.revoked.set()
                capture.close()
                raise
        finally:
            os.close(descriptor)

    async def _close(self):
        if self.capture is not None:
            self.trace.revoked.set()
            self.capture.fence()
            await owned_thread(self.capture.close)

    async def reconcile(self):
        if self.trace is not None and (
            self.trace.closed or not self.owner.control_backend.scope_current(self.scope)
        ):
            async with self.lock:
                if not self.capture.status().get("finished", False):
                    self.trace.revoked.set()
                    self.capture.fence()

    async def close(self):
        async with self.lock:
            await self._close()
            if self.capture is not None and not self.capture.status().get("finished", False):
                raise RuntimeError("Diagnostic capture cleanup is pending")


def _only(params, allowed):
    if not isinstance(params, dict) or set(params) - allowed:
        raise AdminProtocolError("bad_params", "Unexpected diagnostic capture parameters")
