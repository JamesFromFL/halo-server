"""Owned lifecycle for one bundled HAP child and its inherited private channels."""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from halo_server.homekit.ipc import HomeKitProtocolError, read_frame, write_frame
from halo_server.media.manager import settle_owned

MAX_PENDING_REQUESTS = 32
HEARTBEAT_SECONDS = 5.0
REQUEST_SECONDS = 5.0
STOP_SECONDS = 5.0


class HomeKitRuntimeError(RuntimeError):
    """The adapter is unavailable; Ring and local administration remain independent."""


class HomeKitRuntime:
    """Supervise a packaged child without running npm or opening a TCP listener.

    Control readiness is distinct from published/pairable cameras and working
    media. This initial delegate deliberately publishes no Home accessories.
    """

    def __init__(
        self,
        node: Path,
        entry: Path,
        *,
        build_id: str,
        on_retired: Callable[[str], Awaitable[None]],
        max_restarts: int = 3,
    ) -> None:
        if not node.is_absolute() or not entry.is_absolute():
            raise ValueError("adapter executable and entry paths must be absolute")
        if not build_id or len(build_id) > 128:
            raise ValueError("adapter build identity is invalid")
        if type(max_restarts) is not int or not 0 <= max_restarts <= 10:
            raise ValueError("adapter restart budget is invalid")
        self._command = (str(node), str(entry))
        self._build_id = build_id
        self._on_retired = on_retired
        self._max_restarts = max_restarts
        self._stopping = asyncio.Event()
        self._supervisor: asyncio.Task | None = None
        self._first_ready: asyncio.Future | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._bulk: socket.socket | None = None
        self._reader_task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._write_lock = asyncio.Lock()
        self._stop_lock = asyncio.Lock()
        self._boot_id: str | None = None
        self.state = "stopped"
        self.reason: str | None = None
        self.restart_count = 0

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process is not None else None

    async def start(self) -> None:
        if self._supervisor is None:
            if self._boot_id is not None:
                raise HomeKitRuntimeError("Previous HomeKit adapter retirement is incomplete")
            self._stopping.clear()
            self.restart_count = 0
            self._first_ready = asyncio.get_running_loop().create_future()
            self._first_ready.add_done_callback(_retrieve_future_error)
            self._supervisor = asyncio.create_task(self._run(), name="homekit-adapter")
        assert self._first_ready is not None
        try:
            await asyncio.shield(self._first_ready)
            if self.state != "ready":
                raise HomeKitRuntimeError("HomeKit adapter is not ready")
        except asyncio.CancelledError:
            await self.stop()
            raise

    async def stop(self) -> None:
        task = asyncio.create_task(self._stop_serially(), name="homekit-stop")
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await settle_owned(task)
            raise

    async def _stop_serially(self) -> None:
        async with self._stop_lock:
            await self._stop_owned()

    async def _stop_owned(self) -> None:
        self._stopping.set()
        if self._writer is not None and self.state == "ready":
            with contextlib.suppress(Exception):
                await self.request("shutdown", {}, expected="shutdown_ack")
        await self._terminate()
        if self._supervisor is not None:
            await settle_owned(self._supervisor)
            self._supervisor = None
        if self._boot_id is not None:
            await self._cleanup()
        self.state = "stopped"

    async def request(self, kind: str, payload: dict, *, expected: str) -> dict[str, Any]:
        writer, boot_id = self._writer, self._boot_id
        if writer is None or boot_id is None or len(self._pending) >= MAX_PENDING_REQUESTS:
            raise HomeKitRuntimeError("HomeKit adapter is unavailable or busy")
        request_id = str(uuid.uuid4())
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            async with asyncio.timeout(REQUEST_SECONDS):
                async with self._write_lock:
                    await write_frame(
                        writer,
                        {
                            "v": 1,
                            "kind": kind,
                            "boot_id": boot_id,
                            "request_id": request_id,
                            "payload": payload,
                        },
                    )
                reply = await future
            if reply["kind"] != expected:
                raise HomeKitProtocolError("unexpected HomeKit adapter reply")
            return reply["payload"]
        finally:
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()

    async def _run(self) -> None:
        try:
            while not self._stopping.is_set():
                self.state, self.reason = "starting", None
                retirement_failed = False
                try:
                    await self._launch()
                    if self._stopping.is_set():
                        break
                    self.state = "ready"
                    if self._first_ready is not None and not self._first_ready.done():
                        self._first_ready.set_result(None)
                    assert self._process is not None
                    await self._process.wait()
                    self.reason = "adapter_exited"
                except Exception:
                    self.reason = "adapter_start_or_transport_failed"
                finally:
                    try:
                        await self._cleanup()
                    except Exception:
                        self.state, self.reason = "failed", "adapter_retirement_failed"
                        retirement_failed = True
                if retirement_failed:
                    break
                if self._stopping.is_set():
                    break
                self.state = "degraded"
                if self.restart_count >= self._max_restarts:
                    self.state = "failed"
                    break
                delay = min(2**self.restart_count, 30)
                self.restart_count += 1
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stopping.wait(), delay)
        finally:
            if self._first_ready is not None and not self._first_ready.done():
                self._first_ready.set_exception(
                    HomeKitRuntimeError("HomeKit adapter did not start")
                )

    async def _launch(self) -> None:
        parent, child = socket.socketpair()
        try:
            bulk_parent, bulk_child = socket.socketpair()
        except BaseException:
            parent.close()
            child.close()
            raise
        self._boot_id = str(uuid.uuid4())
        self._bulk = bulk_parent
        # Do not forward Ring/account credentials or runtime injection from the
        # daemon's environment to the less-privileged adapter role.
        env = {name: os.environ[name] for name in ("LANG", "LC_ALL", "TZ") if name in os.environ}
        env.update(
            {
                "HALO_CONTROL_FD": str(child.fileno()),
                "HALO_BULK_FD": str(bulk_child.fileno()),
                "HALO_PARENT_PID": str(os.getpid()),
            }
        )
        spawn = asyncio.create_task(
            asyncio.create_subprocess_exec(
                *self._command,
                pass_fds=(child.fileno(), bulk_child.fileno()),
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
        except BaseException:
            parent.close()
            raise
        finally:
            child.close()
            bulk_child.close()
        if self._stopping.is_set():
            parent.close()
            raise HomeKitRuntimeError("HomeKit adapter startup was retired")
        try:
            reader, self._writer = await asyncio.open_connection(sock=parent, limit=65540)
        except BaseException:
            parent.close()
            raise
        self._reader_task = asyncio.create_task(self._read(reader), name="homekit-control")
        reply = await self.request(
            "hello",
            {
                "role": "python",
                "build_id": self._build_id,
                "protocol": 1,
            },
            expected="hello_ack",
        )
        if reply["build_id"] != self._build_id or reply["pid"] != self._process.pid:
            raise HomeKitProtocolError("HomeKit adapter build or process identity mismatch")
        self._heartbeat_task = asyncio.create_task(self._heartbeat(), name="homekit-heartbeat")

    async def _read(self, reader: asyncio.StreamReader) -> None:
        try:
            while True:
                # Heartbeat supplies the total idle deadline. A frame already
                # started still has a finite complete-read deadline.
                first = await reader.readexactly(1)
                message = await read_frame(reader, first_byte=first)
                if message["boot_id"] != self._boot_id:
                    raise HomeKitProtocolError("retired adapter boot identity")
                future = self._pending.get(message["request_id"])
                if future is not None and not future.done():
                    future.set_result(message)
        except asyncio.CancelledError:
            raise
        except Exception:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(HomeKitRuntimeError("HomeKit control channel closed"))
            await self._terminate()

    async def _heartbeat(self) -> None:
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_SECONDS)
                await self.request("ping", {}, expected="pong")
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._terminate()

    async def _terminate(self) -> None:
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

    async def _cleanup(self) -> None:
        await self._terminate()
        for task in (self._reader_task, self._heartbeat_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._reader_task = self._heartbeat_task = None
        for future in self._pending.values():
            if not future.done():
                future.set_exception(HomeKitRuntimeError("HomeKit adapter was retired"))
        if self._writer is not None:
            self._writer.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._writer.wait_closed(), 1)
        self._writer = None
        if self._bulk is not None:
            self._bulk.close()
            self._bulk = None
        if self._boot_id is not None:
            boot_id = self._boot_id
            await self._on_retired(boot_id)
            self._boot_id = None
        self._process = None


def _retrieve_future_error(future: asyncio.Future) -> None:
    if not future.cancelled():
        future.exception()
