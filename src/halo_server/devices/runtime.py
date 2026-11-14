"""Independent supervision of the metadata-only non-camera HomeKit process."""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from halo_server.devices.ipc import (
    MAX_CONTROL_BYTES,
    REPLIES,
    DeviceBridgeProtocolError,
    encode_frame,
    read_frame,
    validate_envelope,
    validate_reply,
)
from halo_server.homekit.storage_owner import PairingDirectoryOwner
from halo_server.homekit.text import literal_text
from halo_server.media.manager import settle_owned
from halo_server.release_guard import inherited_guard_environment, inherited_guard_fds

MAX_PENDING_REQUESTS = 8
REQUEST_SECONDS = 5.0
HEARTBEAT_SECONDS = 5.0
STOP_SECONDS = 5.0


class DeviceBridgeRuntimeError(RuntimeError):
    """The isolated bridge is unavailable; camera processes remain independent."""


class _ArtifactsUnavailable(DeviceBridgeRuntimeError):
    pass


@dataclass(frozen=True)
class _Pending:
    envelope: dict[str, Any]
    future: asyncio.Future


def device_resources():
    """Resolve the same verified release as cameras, with a separate entrypoint.

    Managed resource resolution verifies the complete immutable manifest before
    returning any path. Development uses the already built checkout and never
    installs Node or builds JavaScript during ordinary daemon launch.
    """
    from halo_server.homekit.bridge import AdapterResources, development_resources

    camera = development_resources()
    return AdapterResources(
        camera.node, camera.entry.with_name("device-main.js"), build_id=camera.build_id
    )


class DeviceBridgeRuntime:
    """One owned child, private control socket, and separate pairing lock.

    A failed or ambiguous request retires this process. There are no child
    requests, Ring credentials, inherited public sockets, or media channels.
    The caller coalesces observations into cumulative bounded snapshots.
    """

    def __init__(
        self,
        node: Path,
        entry: Path,
        *,
        build_id: str,
        pairing_directory: Path | None = None,
        on_ready: Callable[[str], Awaitable[None]] | None = None,
        on_retired: Callable[[str], Awaitable[None]] | None = None,
        max_restarts: int = 3,
    ):
        if not node.is_absolute() or not entry.is_absolute():
            raise ValueError("Device bridge executable paths must be absolute")
        if not literal_text(build_id, 128, empty=False):
            raise ValueError("Device bridge build identity is invalid")
        if pairing_directory is not None and not pairing_directory.is_absolute():
            raise ValueError("Device bridge pairing directory must be absolute")
        if type(max_restarts) is not int or not 0 <= max_restarts <= 10:
            raise ValueError("Device bridge restart budget is invalid")
        self._command = str(node), str(entry)
        self._build_id = build_id
        self._pairing_directory = pairing_directory
        self._pairing_owner: PairingDirectoryOwner | None = None
        self._on_ready, self._on_retired = on_ready, on_retired
        self._max_restarts = max_restarts
        self._supervisor: asyncio.Task | None = None
        self._first_ready: asyncio.Future | None = None
        self._reader_task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._pending: dict[str, _Pending] = {}
        self._boot_id: str | None = None
        self._handshake_complete = False
        self._stopping = asyncio.Event()
        self._write_lock = asyncio.Lock()
        self._stop_lock = asyncio.Lock()
        self._terminate_lock = asyncio.Lock()
        self.state = "stopped"
        self.reason: str | None = None
        self.restart_count = 0

    @property
    def pid(self):
        return self._process.pid if self._process is not None else None

    @property
    def boot_id(self):
        return self._boot_id

    @property
    def retirement_pending(self):
        return self._boot_id is not None or self._pairing_owner is not None

    async def start(self):
        if self._supervisor is None:
            if self.retirement_pending:
                raise DeviceBridgeRuntimeError("Previous device bridge retirement is incomplete")
            self._stopping.clear()
            self.restart_count = 0
            self._first_ready = asyncio.get_running_loop().create_future()
            self._first_ready.add_done_callback(_retrieve_future_error)
            self._supervisor = asyncio.create_task(self._run(), name="device-hap-supervisor")
        assert self._first_ready is not None
        try:
            await asyncio.shield(self._first_ready)
            if self.state != "ready" or self._stopping.is_set():
                raise DeviceBridgeRuntimeError("Device bridge is not ready")
        except asyncio.CancelledError:
            await self.stop()
            raise

    def invalidate(self):
        """Immediately fence this account's child before awaiting its retirement."""
        self._stopping.set()
        self._handshake_complete = False
        self._fail_pending("Device bridge account was retired")
        if self._writer is not None:
            self._writer.close()
        if self._process is not None and self._process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self._process.terminate()

    async def stop(self):
        task = asyncio.create_task(self._stop_serially(), name="device-hap-stop")
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await settle_owned(task)
            raise

    async def _stop_serially(self):
        async with self._stop_lock:
            self._stopping.set()
            if self._heartbeat_task is not None:
                self._heartbeat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._heartbeat_task
                self._heartbeat_task = None
            if self._writer is not None and self._handshake_complete:
                with contextlib.suppress(Exception):
                    await self.request("shutdown", {})
                    if self._process is not None:
                        await asyncio.wait_for(self._process.wait(), 1)
            await self._terminate()
            if self._supervisor is not None:
                await settle_owned(self._supervisor)
                self._supervisor = None
            if self.retirement_pending:
                await self._cleanup()
            self.state = "stopped"

    async def publish(self, snapshot, *, admission=None):
        return await self.request("publish", snapshot, admission=admission)

    async def status(self, identity, *, admission=None):
        return await self.request("status", identity, admission=admission)

    async def reveal(self, identity, *, admission=None):
        return await self.request("reveal", identity, admission=admission)

    async def prepare_reset(self, identity, *, admission=None):
        return await self.request("reset_prepare", identity, admission=admission)

    async def request(self, kind, payload, *, expected=None, admission=None):
        if kind not in REPLIES or expected is not None and expected != REPLIES[kind]:
            raise DeviceBridgeProtocolError("Invalid device bridge request")
        boot = self._boot_id
        if boot is None or self._writer is None:
            raise DeviceBridgeRuntimeError("Device bridge is unavailable")
        if self._stopping.is_set() and kind != "shutdown":
            raise DeviceBridgeRuntimeError("Device bridge is stopping")
        if kind != "hello" and not self._handshake_complete:
            raise DeviceBridgeRuntimeError("Device bridge handshake is incomplete")
        if len(self._pending) >= MAX_PENDING_REQUESTS:
            raise DeviceBridgeRuntimeError("Device bridge control queue is full")
        envelope = {
            "v": 1,
            "kind": kind,
            "boot_id": boot,
            "request_id": str(uuid.uuid4()),
            "payload": payload,
        }
        validate_envelope(envelope, role="python")
        frame = encode_frame(envelope)
        # Validate/freeze before the first await; caller-owned dictionaries must
        # not be able to change the correlation of an already queued request.
        from halo_server.devices.ipc import decode_body

        frozen = decode_body(frame[4:])
        future = asyncio.get_running_loop().create_future()
        future.add_done_callback(_retrieve_future_error)
        request_id = envelope["request_id"]
        self._pending[request_id] = _Pending(frozen, future)
        sent = False
        try:
            async with asyncio.timeout(REQUEST_SECONDS):
                async with self._write_lock:
                    writer = self._writer
                    if (
                        writer is None
                        or boot != self._boot_id
                        or self._stopping.is_set()
                        and kind != "shutdown"
                        or admission is not None
                        and not admission()
                    ):
                        raise DeviceBridgeRuntimeError("Device bridge request was retired")
                    sent = True
                    writer.write(frame)
                    await writer.drain()
                return (await asyncio.shield(future))["payload"]
        except (TimeoutError, asyncio.CancelledError):
            if sent:
                # Delivery might have completed. Retire the bounded child so a
                # late publish/reset cannot grant authority after cancellation.
                await self._terminate()
            raise
        except (ConnectionError, OSError) as error:
            await self._terminate()
            raise DeviceBridgeRuntimeError("Device bridge channel is unavailable") from error
        finally:
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()

    async def _run(self):
        try:
            while not self._stopping.is_set():
                self.state, self.reason = "starting", None
                retirement_failed = False
                artifacts_missing = False
                try:
                    await self._launch()
                    if self._stopping.is_set():
                        break
                    self.state = "ready"
                    if self._first_ready is not None and not self._first_ready.done():
                        self._first_ready.set_result(None)
                    assert self._process is not None
                    await self._process.wait()
                    self.reason = "device_adapter_exited"
                except _ArtifactsUnavailable:
                    self.reason = "device_adapter_artifacts_unavailable"
                    artifacts_missing = True
                except Exception:
                    self.reason = "device_adapter_start_or_transport_failed"
                finally:
                    try:
                        await self._cleanup()
                    except Exception:
                        self.reason = "device_adapter_retirement_failed"
                        retirement_failed = True
                if self._stopping.is_set():
                    break
                if (
                    artifacts_missing
                    or retirement_failed
                    or self.restart_count >= self._max_restarts
                ):
                    self.state = "failed"
                    break
                self.state = "degraded"
                delay = min(2**self.restart_count, 30)
                self.restart_count += 1
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stopping.wait(), delay)
        finally:
            if self._first_ready is not None and not self._first_ready.done():
                self._first_ready.set_exception(
                    DeviceBridgeRuntimeError("Device bridge did not start")
                )

    async def _launch(self):
        if not all(Path(path).is_file() for path in self._command):
            raise _ArtifactsUnavailable("Bundled device bridge artifacts are unavailable")
        guard_fds = inherited_guard_fds()
        guard_environment = inherited_guard_environment()
        if self._pairing_directory is not None:
            if self._pairing_owner is not None:
                raise DeviceBridgeRuntimeError("Previous pairing store ownership is incomplete")
            self._pairing_owner = PairingDirectoryOwner.acquire(self._pairing_directory)
        parent, child = socket.socketpair()
        self._boot_id = str(uuid.uuid4())
        self._handshake_complete = False
        env = {key: os.environ[key] for key in ("LANG", "LC_ALL", "TZ") if key in os.environ}
        env.update(guard_environment)
        env.update(HALO_DEVICE_CONTROL_FD=str(child.fileno()), HALO_PARENT_PID=str(os.getpid()))
        inherited = child.fileno(), *guard_fds
        if self._pairing_owner is not None:
            env["HALO_PAIRING_FD"] = str(self._pairing_owner.directory_fd)
            env["HALO_PAIRING_LOCK_FD"] = str(self._pairing_owner.lock_fd)
            inherited += self._pairing_owner.directory_fd, self._pairing_owner.lock_fd
        spawn = asyncio.create_task(
            asyncio.create_subprocess_exec(
                *self._command,
                pass_fds=inherited,
                umask=0o077,
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        )
        try:
            try:
                self._process = await asyncio.shield(spawn)
            except asyncio.CancelledError:
                self._process = await settle_owned(spawn)
                raise
            if self._stopping.is_set():
                raise DeviceBridgeRuntimeError("Device bridge startup was retired")
            reader, self._writer = await asyncio.open_connection(
                sock=parent, limit=MAX_CONTROL_BYTES + 4
            )
            parent = None
        finally:
            child.close()
            if parent is not None:
                parent.close()
        self._reader_task = asyncio.create_task(self._read(reader), name="device-hap-control")
        reply = await self.request(
            "hello", {"role": "python", "build_id": self._build_id, "protocol": 1}
        )
        if reply["build_id"] != self._build_id or reply["pid"] != self.pid:
            raise DeviceBridgeProtocolError("Device bridge build or process identity mismatch")
        self._handshake_complete = True
        if self._on_ready is not None:
            async with asyncio.timeout(REQUEST_SECONDS):
                await self._on_ready(self._boot_id)
        self._heartbeat_task = asyncio.create_task(self._heartbeat(), name="device-hap-heartbeat")

    async def _read(self, reader):
        try:
            while True:
                first = await reader.readexactly(1)
                message = await read_frame(reader, first_byte=first)
                validate_envelope(message, role="hap")
                if message["boot_id"] != self._boot_id:
                    raise DeviceBridgeProtocolError("Retired device bridge boot identity")
                record = self._pending.get(message["request_id"])
                if record is None or record.future.done():
                    raise DeviceBridgeProtocolError("Unowned or duplicate device bridge response")
                validate_reply(record.envelope, message)
                record.future.set_result(message)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._fail_pending("Device bridge channel closed")
            await self._terminate()

    async def _heartbeat(self):
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_SECONDS)
                await self.request("ping", {})
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._terminate()

    async def _terminate(self):
        async with self._terminate_lock:
            process = self._process
            if process is None or process.returncode is not None:
                return
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), STOP_SECONDS)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                await process.wait()

    def _fail_pending(self, reason):
        for pending in tuple(self._pending.values()):
            if not pending.future.done():
                pending.future.set_exception(DeviceBridgeRuntimeError(reason))

    async def _cleanup(self):
        await self._terminate()
        for task in (self._reader_task, self._heartbeat_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._reader_task = self._heartbeat_task = None
        self._handshake_complete = False
        self._fail_pending("Device bridge was retired")
        if self._writer is not None:
            self._writer.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._writer.wait_closed(), 1)
            self._writer = None
        if self._boot_id is not None and self._on_retired is not None:
            async with asyncio.timeout(REQUEST_SECONDS):
                await self._on_retired(self._boot_id)
        if self._pairing_owner is not None:
            self._pairing_owner.close()
            self._pairing_owner = None
        self._boot_id = None
        self._process = None


def _retrieve_future_error(future):
    if not future.cancelled():
        future.exception()
