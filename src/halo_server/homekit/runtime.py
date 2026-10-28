"""Owned lifecycle and bounded control dispatch for one packaged HAP child."""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from halo_server.homekit.ipc import (
    REPLY_KINDS,
    HomeKitProtocolError,
    read_frame,
    validate_direction,
    validate_reply,
    write_frame,
)
from halo_server.homekit.storage_owner import PairingDirectoryOwner
from halo_server.media.manager import settle_owned

MAX_PENDING_REQUESTS = 32
MAX_CHILD_REQUESTS = 16
if TYPE_CHECKING:
    from halo_server.homekit.bulk import BulkChannel

MAX_REQUEST_HISTORY = 256
MAX_RECENT_REQUESTS = 256
HEARTBEAT_SECONDS = 5.0
REQUEST_SECONDS = 5.0
SNAPSHOT_REQUEST_SECONDS = 16.0
RECORDING_OPEN_SECONDS = 28.0
RECORDING_PULL_SECONDS = 16.0
RECORDING_STOP_SECONDS = 20.0
STOP_SECONDS = 5.0


class HomeKitRuntimeError(RuntimeError):
    """The adapter is unavailable; Ring and local administration remain independent."""


class _AdapterArtifactsUnavailable(HomeKitRuntimeError):
    """An absent packaged executable cannot be repaired by a crash retry."""


@dataclass(frozen=True)
class _PendingRequest:
    envelope: dict[str, Any]
    expected: str
    future: asyncio.Future


class HomeKitRuntime:
    """Supervise a bundled child without downloads or public listening sockets.

    Control readiness is independent of accessory publication and media support.
    Every request and binary stream belongs to exactly one process boot. Pending
    handlers remain owned until their cancellation and compensating work finish.
    """

    def __init__(
        self,
        node: Path,
        entry: Path,
        *,
        build_id: str,
        on_retired: Callable[[str], Awaitable[None]],
        on_ready: Callable[[str], Awaitable[None]] | None = None,
        on_request: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,
        on_notice: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        on_replied: Callable[[dict[str, Any], dict[str, Any]], Awaitable[None]] | None = None,
        max_restarts: int = 3,
        pairing_directory: Path | None = None,
    ) -> None:
        if not node.is_absolute() or not entry.is_absolute():
            raise ValueError("adapter executable and entry paths must be absolute")
        if not build_id or len(build_id) > 128:
            raise ValueError("adapter build identity is invalid")
        if type(max_restarts) is not int or not 0 <= max_restarts <= 10:
            raise ValueError("adapter restart budget is invalid")
        if pairing_directory is not None and not pairing_directory.is_absolute():
            raise ValueError("pairing directory must be absolute")
        self._pairing_directory = pairing_directory
        self._pairing_owner: PairingDirectoryOwner | None = None
        self._command = (str(node), str(entry))
        self._build_id = build_id
        self._on_retired = on_retired
        self._on_ready = on_ready
        self._on_request = on_request
        self._on_notice = on_notice
        self._on_replied = on_replied
        self._max_restarts = max_restarts
        self._stopping = asyncio.Event()
        self._supervisor: asyncio.Task | None = None
        self._first_ready: asyncio.Future | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._bulk: BulkChannel | None = None
        self._reader_task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._child_tasks: set[asyncio.Task] = set()
        self._cancelling_tasks: set[asyncio.Task] = set()
        self._failure_tasks: set[asyncio.Task] = set()
        self._pending: dict[str, _PendingRequest] = {}
        self._retired_requests: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._child_history: OrderedDict[str, None] = OrderedDict()
        self._active_child_ids: set[str] = set()
        self._write_lock = asyncio.Lock()
        self._stop_lock = asyncio.Lock()
        self._boot_id: str | None = None
        self._handshake_complete = False
        self.state = "stopped"
        self.reason: str | None = None
        self.restart_count = 0

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process is not None else None

    @property
    def boot_id(self) -> str | None:
        return self._boot_id

    @property
    def retirement_pending(self) -> bool:
        return self._boot_id is not None or self._pairing_owner is not None

    @property
    def bulk_channel(self) -> BulkChannel | None:
        """The private binary transport for the current boot, never a raw socket."""
        return self._bulk

    async def start(self) -> None:
        if self._supervisor is None:
            if self.retirement_pending:
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
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._heartbeat_task
            self._heartbeat_task = None
        if self._writer is not None and self.state == "ready":
            try:
                await self.request("shutdown", {}, expected="shutdown_ack")
            except Exception:
                pass
            else:
                process = self._process
                if process is not None and process.returncode is None:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(process.wait(), 1.0)
        await self._terminate()
        if self._supervisor is not None:
            await settle_owned(self._supervisor)
            self._supervisor = None
        if self.retirement_pending:
            await self._cleanup()
        self.state = "stopped"

    async def send(
        self,
        kind: str,
        payload: dict[str, Any],
        request_id: str | None = None,
        *,
        admission: Callable[[], bool] | None = None,
    ) -> None:
        """Send metadata only; bulk transfers own their separate correlation."""
        boot_id = self._boot_id
        if boot_id is None:
            raise HomeKitRuntimeError("HomeKit adapter is unavailable")
        await self._send_envelope(
            {
                "v": 1,
                "kind": kind,
                "boot_id": boot_id,
                "request_id": str(uuid.uuid4()) if request_id is None else request_id,
                "payload": payload,
            },
            admission=admission,
        )

    async def _send_envelope(
        self,
        envelope: dict[str, Any],
        *,
        admission: Callable[[], bool] | None = None,
        retirement_reply: bool = False,
        retirement_bulk: bool = False,
    ) -> None:
        validate_direction(envelope, "python")
        async with self._write_lock:
            writer = self._writer
            if writer is None or envelope["boot_id"] != self._boot_id:
                raise HomeKitRuntimeError("HomeKit adapter is unavailable")
            # A stopped reader must never acquire new ownership through a late
            # handler's successful response. Shutdown itself remains writable.
            cleanup_reply = (
                retirement_reply
                and envelope["kind"]
                in {"video_stop_result", "lease_stop_result", "recording_stop_result"}
                and envelope["request_id"] in self._active_child_ids
            )
            cleanup_bulk = retirement_bulk and envelope["kind"] in {
                "transfer_cancel",
                "transfer_fence",
                "transfer_fence_ack",
                "transfer_result",
            }
            if (
                self._stopping.is_set()
                and envelope["kind"] != "shutdown"
                and not cleanup_reply
                and not cleanup_bulk
            ):
                raise HomeKitRuntimeError("HomeKit adapter is stopping")
            if admission is not None and not admission():
                raise HomeKitRuntimeError("HomeKit notification authority was retired")
            async with asyncio.timeout(REQUEST_SECONDS):
                await write_frame(writer, envelope)

    async def _send_bulk_control(self, kind, payload, request_id=None):
        # Only the owned BulkChannel calls this transport callback, after its
        # exact scope/identity/frontier checks. Shutdown must still drain old
        # transfers; OPEN, credits and fresh media admission remain fenced.
        await self._send_envelope(
            {
                "v": 1,
                "kind": kind,
                "boot_id": self._boot_id,
                "request_id": str(uuid.uuid4()) if request_id is None else request_id,
                "payload": payload,
            },
            retirement_bulk=True,
        )

    async def request(self, kind: str, payload: dict, *, expected: str) -> dict[str, Any]:
        boot_id = self._boot_id
        if self._writer is None or boot_id is None or len(self._pending) >= MAX_PENDING_REQUESTS:
            raise HomeKitRuntimeError("HomeKit adapter is unavailable or busy")
        if REPLY_KINDS.get(kind) != expected:
            raise HomeKitProtocolError("unexpected HomeKit adapter response contract")
        request_id = str(uuid.uuid4())
        envelope = {
            "v": 1,
            "kind": kind,
            "boot_id": boot_id,
            "request_id": request_id,
            "payload": payload,
        }
        validate_direction(envelope, "python")
        future = asyncio.get_running_loop().create_future()
        future.add_done_callback(_retrieve_future_error)
        record = _PendingRequest(envelope, expected, future)
        self._pending[request_id] = record
        try:
            async with asyncio.timeout(REQUEST_SECONDS):
                await self._send_envelope(envelope)
                reply = await future
            return reply["payload"]
        finally:
            self._pending.pop(request_id, None)
            if self._boot_id == boot_id:
                self._retired_requests[request_id] = envelope
                while len(self._retired_requests) > MAX_RECENT_REQUESTS:
                    self._retired_requests.popitem(last=False)
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
                except _AdapterArtifactsUnavailable:
                    self.state, self.reason = "failed", "adapter_artifacts_unavailable"
                    break
                except Exception:
                    self.reason = "adapter_start_or_transport_failed"
                finally:
                    try:
                        await self._cleanup()
                    except Exception:
                        self.state, self.reason = "failed", "adapter_retirement_failed"
                        retirement_failed = True
                if retirement_failed or self._stopping.is_set():
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
        from halo_server.homekit.bulk import BulkChannel

        if not Path(self._command[0]).is_file() or not Path(self._command[1]).is_file():
            raise _AdapterArtifactsUnavailable("Bundled HomeKit adapter artifacts are unavailable")
        if self._pairing_directory is not None:
            if self._pairing_owner is not None:
                raise HomeKitRuntimeError("Previous pairing store ownership is incomplete")
            self._pairing_owner = PairingDirectoryOwner.acquire(self._pairing_directory)
        parent, child = socket.socketpair()
        try:
            bulk_parent, bulk_child = socket.socketpair()
        except BaseException:
            parent.close()
            child.close()
            raise
        self._boot_id = str(uuid.uuid4())
        self._handshake_complete = False
        self._retired_requests.clear()
        self._child_history.clear()
        # Do not forward Ring credentials, user import paths, Node options or
        # runtime injection from the daemon's environment into the child.
        env = {name: os.environ[name] for name in ("LANG", "LC_ALL", "TZ") if name in os.environ}
        env.update(
            {
                "HALO_CONTROL_FD": str(child.fileno()),
                "HALO_BULK_FD": str(bulk_child.fileno()),
                "HALO_PARENT_PID": str(os.getpid()),
            }
        )
        inherited = (child.fileno(), bulk_child.fileno())
        if self._pairing_owner is not None:
            env["HALO_PAIRING_FD"] = str(self._pairing_owner.directory_fd)
            env["HALO_PAIRING_LOCK_FD"] = str(self._pairing_owner.lock_fd)
            inherited += (self._pairing_owner.directory_fd, self._pairing_owner.lock_fd)
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
                raise HomeKitRuntimeError("HomeKit adapter startup was retired")
            reader, self._writer = await asyncio.open_connection(sock=parent, limit=65540)
            parent = None
            bulk_reader, bulk_writer = await asyncio.open_connection(
                sock=bulk_parent, limit=128 * 1024
            )
            bulk_parent = None
            self._bulk = BulkChannel(
                bulk_reader,
                bulk_writer,
                boot_id=self._boot_id,
                role="python",
                send_control=self._send_bulk_control,
                on_failure=self._bulk_failed,
            )
            self._bulk.start()
        finally:
            child.close()
            bulk_child.close()
            if parent is not None:
                parent.close()
            if bulk_parent is not None:
                bulk_parent.close()
        self._reader_task = asyncio.create_task(self._read(reader), name="homekit-control")
        reply = await self.request(
            "hello",
            {"role": "python", "build_id": self._build_id, "protocol": 1},
            expected="hello_ack",
        )
        if reply["build_id"] != self._build_id or reply["pid"] != self._process.pid:
            raise HomeKitProtocolError("HomeKit adapter build or process identity mismatch")
        self._handshake_complete = True
        if self._on_ready is not None:
            async with asyncio.timeout(REQUEST_SECONDS):
                await self._on_ready(self._boot_id)
        self._heartbeat_task = asyncio.create_task(self._heartbeat(), name="homekit-heartbeat")

    async def _read(self, reader: asyncio.StreamReader) -> None:
        try:
            while True:
                # Heartbeat bounds total idle time; a partial frame has its own
                # complete-read deadline inside read_frame.
                first = await reader.readexactly(1)
                message = await read_frame(reader, first_byte=first)
                validate_direction(message, "hap")
                if message["boot_id"] != self._boot_id:
                    raise HomeKitProtocolError("retired adapter boot identity")
                request_id = message["request_id"]
                record = self._pending.get(request_id)
                if record is not None:
                    validate_reply(record.envelope, message)
                    if not record.future.done():
                        record.future.set_result(message)
                    continue
                retired = self._retired_requests.get(request_id)
                if retired is not None:
                    # A timed-out/cancelled request can receive one late reply;
                    # it may not resurrect ownership or alter another scope.
                    validate_reply(retired, message)
                    continue
                if not self._handshake_complete:
                    raise HomeKitProtocolError("HomeKit adapter handshake is incomplete")
                if message["kind"].startswith("transfer_"):
                    if message["kind"] == "transfer_open":
                        self._start_child_task(message, self._dispatch_transfer_open(message))
                    elif self._bulk is None or not await self._bulk.handle_control(message):
                        raise HomeKitProtocolError("unowned bulk transfer metadata")
                    continue
                if message["kind"] in REPLY_KINDS.values():
                    # A result outside both bounded ownership maps cannot grant
                    # any authority. It may be a very late timed-out reply.
                    continue
                self._start_child_task(message, self._dispatch(message))
        except asyncio.CancelledError:
            raise
        except Exception:
            self._fail_pending("HomeKit control channel closed")
            if not self._stopping.is_set():
                await self._terminate()

    def _start_child_task(self, message: dict[str, Any], operation: Awaitable[None]) -> None:
        request_id = message["request_id"]
        try:
            if self._stopping.is_set() and message["kind"] not in {
                "video_stop",
                "lease_stop",
                "snapshot_cancel",
                "recording_stop",
                "transfer_cancel",
            }:
                raise HomeKitProtocolError("HomeKit adapter is stopping")
            if request_id in self._child_history or request_id in self._active_child_ids:
                raise HomeKitProtocolError("duplicate HomeKit adapter request")
            if len(self._child_tasks) >= MAX_CHILD_REQUESTS:
                raise HomeKitProtocolError("too many HomeKit adapter requests")
            # Recent wire IDs catch duplicate delivery without eventually
            # exhausting a daemon through ordinary polling, renewals or audio.
            # Resource/actuator operation IDs retain their separate broker-owned
            # lifetime barriers; this cache never grants operation authority.
            self._child_history[request_id] = None
            while len(self._child_history) > MAX_REQUEST_HISTORY:
                self._child_history.popitem(last=False)
            task = asyncio.create_task(operation, name="homekit-request")
            self._active_child_ids.add(request_id)
        except BaseException:
            # The caller constructed this coroutine before admission. Do not
            # leak an unawaited object if a duplicate or capacity check fails.
            if hasattr(operation, "close"):
                operation.close()
            raise
        self._child_tasks.add(task)
        task.add_done_callback(lambda completed: self._child_done(completed, request_id))

    async def _dispatch_transfer_open(self, message: dict[str, Any]) -> None:
        async with asyncio.timeout(REQUEST_SECONDS):
            if self._on_notice is not None:
                await self._on_notice(message)
            if self._bulk is None or not await self._bulk.handle_control(message):
                raise HomeKitProtocolError("unowned bulk transfer metadata")

    async def _dispatch(self, message: dict[str, Any]) -> None:
        deadline = {
            "snapshot_request": SNAPSHOT_REQUEST_SECONDS,
            "recording_open": RECORDING_OPEN_SECONDS,
            "recording_next": RECORDING_PULL_SECONDS,
            "recording_stop": RECORDING_STOP_SECONDS,
        }.get(message["kind"], REQUEST_SECONDS)
        async with asyncio.timeout(deadline):
            if self._stopping.is_set() and message["kind"] not in {
                "video_stop",
                "lease_stop",
                "snapshot_cancel",
                "recording_stop",
                "transfer_cancel",
            }:
                raise HomeKitProtocolError("HomeKit adapter is stopping")
            if message["kind"] not in REPLY_KINDS:
                if self._on_notice is None:
                    if message["kind"] in {"status", "error"}:
                        return
                    raise HomeKitProtocolError("unsupported HomeKit adapter notice")
                await self._on_notice(message)
                return
            if self._on_request is None:
                raise HomeKitProtocolError("unsupported HomeKit adapter request")
            result = await self._on_request(message)
            if not isinstance(result, dict):
                raise HomeKitProtocolError("invalid HomeKit request handler result")
            if set(result) == {"v", "kind", "boot_id", "request_id", "payload"}:
                reply = result
            else:
                reply = {**message, "kind": REPLY_KINDS[message["kind"]], "payload": result}
            if reply["kind"] == "error":
                validate_direction(reply, "python")
                if (
                    reply["boot_id"] != message["boot_id"]
                    or reply["request_id"] != message["request_id"]
                ):
                    raise HomeKitProtocolError("invalid HomeKit error correlation")
            else:
                validate_reply(message, reply)
            await self._send_envelope(
                reply,
                retirement_reply=message["kind"] in {"video_stop", "lease_stop", "recording_stop"},
            )
            if self._on_replied is not None:
                await self._on_replied(message, reply)

    def _child_done(self, task: asyncio.Task, request_id: str) -> None:
        self._active_child_ids.discard(request_id)
        self._child_tasks.discard(task)
        self._cancelling_tasks.discard(task)
        if task.cancelled():
            return
        if task.exception() is not None:
            # Do not block control reads behind an operation, including its
            # failure path. The supervisor owns teardown and compensation.
            self._handshake_complete = False
            failure = asyncio.create_task(self._terminate(), name="homekit-request-failed")
            self._failure_tasks.add(failure)
            failure.add_done_callback(self._failure_done)

    def _failure_done(self, task: asyncio.Task) -> None:
        self._failure_tasks.discard(task)
        _retrieve_future_error(task)

    async def _bulk_failed(self, _error: Exception) -> None:
        if self._stopping.is_set():
            return  # A retiring peer may close bulk before its control shutdown ACK.
        self._fail_pending("HomeKit binary channel closed")
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

    def _fail_pending(self, reason: str) -> None:
        for record in tuple(self._pending.values()):
            if not record.future.done():
                record.future.set_exception(HomeKitRuntimeError(reason))

    async def _cleanup(self) -> None:
        await self._terminate()
        for task in (self._reader_task, self._heartbeat_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._reader_task = self._heartbeat_task = None
        self._fail_pending("HomeKit adapter was retired")
        self._handshake_complete = False
        if self._writer is not None:
            self._writer.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._writer.wait_closed(), 1)
        self._writer = None
        # Reader admission is closed before cancelling operations. A cancelled
        # handler may still own a thread/late acquisition; keep this boot barrier
        # until that task's compensation has actually joined.
        children = tuple(self._child_tasks)
        for task in children:
            if task not in self._cancelling_tasks:
                self._cancelling_tasks.add(task)
                task.cancel()
        if children:
            _done, pending = await asyncio.wait(children, timeout=STOP_SECONDS)
            if pending:
                raise HomeKitRuntimeError("HomeKit request retirement is incomplete")
        failures = tuple(self._failure_tasks)
        if failures:
            await asyncio.gather(*failures, return_exceptions=True)
        if self._bulk is not None:
            await self._bulk.close()
            self._bulk = None
        if self._boot_id is not None:
            boot_id = self._boot_id
            await self._on_retired(boot_id)
        if self._pairing_owner is not None:
            self._pairing_owner.close()
            self._pairing_owner = None
        self._boot_id = None
        self._retired_requests.clear()
        self._child_history.clear()
        self._active_child_ids.clear()
        self._process = None


def _retrieve_future_error(future: asyncio.Future) -> None:
    if not future.cancelled():
        future.exception()
