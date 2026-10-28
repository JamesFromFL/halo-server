"""Bounded, explicitly authorized binary transfers on the private per-boot channel.

Only the current broker can authorize an incoming operation. Control metadata is
never authority. This is IPC flow control, not a claim about HAP/HDS socket drains.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import math
import uuid
from collections.abc import AsyncIterable, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .ipc import (
    BULK_HEADER_BYTES,
    BULK_LIMITS,
    MAX_BULK_CHUNK_BYTES,
    MAX_BULK_WINDOW_CHUNKS,
    HomeKitProtocolError,
    decode_bulk_header,
    encode_bulk_header,
    validate_direction,
    validate_scope,
)

MAX_TRANSFERS = 4


class BulkTransferError(HomeKitProtocolError):
    """A transfer failed; the shared channel has retired to prevent desynchronization."""

    def __init__(self, code: str = "protocol-error") -> None:
        super().__init__(f"Private binary transfer failed ({code})")
        self.code = code


def _observed(future: asyncio.Future) -> None:
    if not future.cancelled():
        future.exception()


@dataclass
class _Incoming:
    scope: dict[str, Any]
    operation_id: str
    transfer_id: str
    kind: str
    total: int
    sink: Callable[[bytes], Awaitable[None]]
    valid: Callable[[], bool]
    on_close: Callable[[str], Awaitable[None]] | None
    future: asyncio.Future
    open_id: str | None = None
    chunk_bytes: int = 0
    window: int = 0
    granted: int = 0
    sequence: int = 0
    received: int = 0
    end: dict[str, Any] | None = None
    timer: asyncio.Task | None = None
    finishing: bool = False
    owned: set[asyncio.Task] = field(default_factory=set)


@dataclass
class _Outgoing:
    scope: dict[str, Any]
    operation_id: str
    transfer_id: str
    kind: str
    total: int
    open_id: str
    end_id: str
    valid: Callable[[], bool]
    future: asyncio.Future
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    available: int = 0
    sent: int = 0
    acknowledged: int = 0
    initialized: bool = False
    end_sent: bool = False
    source: Any = None
    source_closed: bool = False
    pending_next: asyncio.Task | None = None
    owned: set[asyncio.Task] = field(default_factory=set)
    timer: asyncio.Task | None = None


class ReceiveHandle:
    """A finite authorization, retained until its consumer and cleanup finish."""

    def __init__(self, channel: BulkChannel, state: _Incoming) -> None:
        self._channel, self._state = channel, state

    async def wait(self) -> None:
        try:
            await asyncio.shield(self._state.future)
        except asyncio.CancelledError:
            await self._channel.close("cancelled")
            raise

    async def cancel(self) -> None:
        if not self._state.future.done():
            await self._channel.close("cancelled")


class BulkChannel:
    """One reader, one serialized writer, at most two unacknowledged chunks globally.

    ``send_control`` writes a one-way metadata envelope; it must not wait for an
    RPC response. Its optional third argument is the exact correlation UUID.
    One sink callback is owned and awaited at a time, without a detached queue.
    Failed cancellation remains a visible reservation until an explicit retry.
    """

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        boot_id: str,
        role: str = "python",
        send_control: Callable[..., Awaitable[None]],
        timeout: float = 5.0,
        on_failure: Callable[[Exception], Awaitable[None]] | None = None,
    ) -> None:
        if role not in {"python", "hap"} or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Invalid binary channel configuration")
        if str(uuid.UUID(boot_id)) != boot_id:
            raise ValueError("Invalid boot identity")
        self.reader, self.writer = reader, writer
        self.boot_id, self.role = boot_id, role
        self._send_control, self.timeout, self._on_failure = send_control, timeout, on_failure
        self._incoming: dict[str, _Incoming] = {}
        self._outgoing: dict[str, _Outgoing] = {}
        self._write_lock = asyncio.Lock()
        self._send_slots = asyncio.Semaphore(MAX_BULK_WINDOW_CHUNKS)
        self._reader_task: asyncio.Task | None = None
        self._closing = False
        self._close_lock = asyncio.Lock()
        self._close_owner: asyncio.Task | None = None
        self._send_tasks: set[asyncio.Task] = set()
        self._owned_tasks: set[asyncio.Task] = set()
        self._failure: Exception | None = None
        # Bound asyncio's own buffered output as well as our application frames.
        transport = getattr(writer, "transport", None)
        if transport is not None:
            transport.set_write_buffer_limits(high=MAX_BULK_CHUNK_BYTES, low=0)

    @property
    def closed(self) -> bool:
        return self._closing

    @property
    def failure(self) -> Exception | None:
        return self._failure

    @property
    def counts(self) -> tuple[int, int]:
        return len(self._incoming), len(self._outgoing)

    def start(self) -> None:
        self._ensure_open()
        if self._reader_task is None:
            self._reader_task = asyncio.create_task(self._read_loop(), name="homekit-bulk-reader")
            self._reader_task.add_done_callback(_observed)

    def _ensure_open(self) -> None:
        if self._closing:
            raise BulkTransferError("cancelled")

    def _identity(
        self,
        scope: dict[str, Any],
        operation_id: str,
        transfer_id: str,
        kind: str,
        total: int,
        *,
        incoming: bool,
    ) -> dict[str, Any]:
        self._ensure_open()
        validate_scope(scope)
        encode_bulk_header(kind, transfer_id, 0, 1)
        if (
            str(uuid.UUID(operation_id)) != operation_id
            or type(total) is not int
            or not 1 <= total <= BULK_LIMITS[kind]
        ):
            raise BulkTransferError()
        producer = "hap" if kind == "talkback_pcm" else "python"
        if (producer != self.role) != incoming:
            raise BulkTransferError()
        if transfer_id in self._incoming or transfer_id in self._outgoing:
            raise BulkTransferError("conflict")
        return dict(scope)

    def authorize_receive(
        self,
        *,
        scope: dict[str, Any],
        operation_id: str,
        transfer_id: str,
        kind: str,
        total_bytes: int,
        sink: Callable[[bytes], Awaitable[None]],
        valid: Callable[[], bool] = lambda: True,
        on_close: Callable[[str], Awaitable[None]] | None = None,
    ) -> ReceiveHandle:
        copied = self._identity(scope, operation_id, transfer_id, kind, total_bytes, incoming=True)
        if len(self._incoming) >= MAX_TRANSFERS:
            raise BulkTransferError("busy")
        if not callable(sink) or not callable(valid) or not valid():
            raise BulkTransferError("invalidated")
        future = asyncio.get_running_loop().create_future()
        future.add_done_callback(_observed)
        state = _Incoming(
            copied, operation_id, transfer_id, kind, total_bytes, sink, valid, on_close, future
        )
        self._incoming[transfer_id] = state
        state.timer = asyncio.create_task(self._deadline(), name="homekit-bulk-deadline")
        state.timer.add_done_callback(_observed)
        return ReceiveHandle(self, state)

    async def _deadline(self) -> None:
        await asyncio.sleep(self.timeout)
        await self._fail(BulkTransferError("timeout"))

    async def _control(
        self, kind: str, state: _Incoming | _Outgoing, extra: dict[str, Any], request_id: str
    ) -> None:
        self._ensure_open()
        if not state.valid():
            raise BulkTransferError("invalidated")
        payload = {
            "scope": state.scope,
            "operation_id": state.operation_id,
            "transfer_id": state.transfer_id,
            **extra,
        }
        await self._owned_call(self._send_control(kind, payload, request_id), owner=state)

    @staticmethod
    def _matches(state: _Incoming | _Outgoing, payload: dict[str, Any]) -> bool:
        return state.scope == payload["scope"] and state.operation_id == payload["operation_id"]

    async def handle_control(self, envelope: dict[str, Any]) -> bool:
        if not isinstance(envelope, dict) or not str(envelope.get("kind", "")).startswith(
            "transfer_"
        ):
            return False
        try:
            self._ensure_open()
            validate_direction(envelope, "hap" if self.role == "python" else "python")
            if envelope["boot_id"] != self.boot_id:
                raise BulkTransferError()
            payload, request_id, kind = (
                envelope["payload"],
                envelope["request_id"],
                envelope["kind"],
            )
            transfer_id = payload["transfer_id"]
            if kind in {"transfer_open", "transfer_end"}:
                state = self._incoming.get(transfer_id)
            elif kind in {"transfer_credit", "transfer_result"}:
                state = self._outgoing.get(transfer_id)
            else:
                state = self._incoming.get(transfer_id) or self._outgoing.get(transfer_id)
            if state is None or not self._matches(state, payload) or not state.valid():
                raise BulkTransferError("invalidated")
            if kind == "transfer_open":
                assert isinstance(state, _Incoming)
                if (
                    state.open_id is not None
                    or payload["kind"] != state.kind
                    or payload["total_bytes"] != state.total
                ):
                    raise BulkTransferError()
                state.open_id, state.chunk_bytes, state.window = (
                    request_id,
                    payload["chunk_bytes"],
                    payload["window_chunks"],
                )
                state.granted = state.window
                await self._control(
                    "transfer_credit",
                    state,
                    {"next_sequence": 0, "credits": state.window},
                    request_id,
                )
            elif kind == "transfer_credit":
                assert isinstance(state, _Outgoing)
                if request_id != state.open_id:
                    raise BulkTransferError()
                sequence, credits = payload["next_sequence"], payload["credits"]
                if not state.initialized:
                    if sequence != 0 or credits != MAX_BULK_WINDOW_CHUNKS:
                        raise BulkTransferError()
                    state.initialized = True
                else:
                    delta = sequence - state.acknowledged
                    if delta != credits or not 0 < delta <= state.sent - state.acknowledged:
                        raise BulkTransferError()
                    for _ in range(delta):
                        self._send_slots.release()
                    state.acknowledged = sequence
                if (
                    state.available + credits + state.sent - state.acknowledged
                    > MAX_BULK_WINDOW_CHUNKS
                ):
                    raise BulkTransferError()
                state.available += credits
                state.changed.set()
            elif kind == "transfer_end":
                assert isinstance(state, _Incoming)
                if (
                    state.open_id is None
                    or state.end is not None
                    or payload["total_bytes"] != state.total
                    or not math.ceil(state.total / state.chunk_bytes)
                    <= payload["sequence_count"]
                    <= state.total
                ):
                    raise BulkTransferError()
                state.end = {**payload, "request_id": request_id}
                await self._finish_incoming(state)
            elif kind == "transfer_result":
                assert isinstance(state, _Outgoing)
                if (
                    request_id != state.end_id
                    or not state.end_sent
                    or payload["status"] != "complete"
                    or payload["code"] != "ok"
                    or state.acknowledged != state.sent
                ):
                    raise BulkTransferError()
                if state.timer is not None:
                    state.timer.cancel()
                state.future.set_result(None)
                state.changed.set()
            else:
                raise BulkTransferError(payload["code"])
            return True
        except Exception as error:
            await self._fail(error)
            raise

    async def send(
        self,
        *,
        scope: dict[str, Any],
        operation_id: str,
        transfer_id: str,
        kind: str,
        total_bytes: int,
        source: AsyncIterable[bytes],
        valid: Callable[[], bool] = lambda: True,
    ) -> None:
        copied = self._identity(scope, operation_id, transfer_id, kind, total_bytes, incoming=False)
        if len(self._outgoing) >= MAX_TRANSFERS:
            raise BulkTransferError("busy")
        if not callable(valid) or not valid():
            raise BulkTransferError("invalidated")
        future = asyncio.get_running_loop().create_future()
        future.add_done_callback(_observed)
        state = _Outgoing(
            copied,
            operation_id,
            transfer_id,
            kind,
            total_bytes,
            str(uuid.uuid4()),
            str(uuid.uuid4()),
            valid,
            future,
        )
        self._outgoing[transfer_id] = state
        state.timer = asyncio.create_task(self._deadline(), name="homekit-bulk-deadline")
        state.timer.add_done_callback(_observed)
        iterator = source.__aiter__()
        state.source = iterator
        owner = asyncio.current_task()
        assert owner is not None
        self._send_tasks.add(owner)
        try:
            await self._control(
                "transfer_open",
                state,
                {
                    "kind": kind,
                    "total_bytes": total_bytes,
                    "chunk_bytes": min(MAX_BULK_CHUNK_BYTES, total_bytes),
                    "window_chunks": MAX_BULK_WINDOW_CHUNKS,
                },
                state.open_id,
            )
            sent_bytes = 0
            while True:
                while state.available == 0:
                    state.changed.clear()
                    if state.future.done():
                        await state.future
                    await asyncio.wait_for(state.changed.wait(), self.timeout)
                await asyncio.wait_for(self._send_slots.acquire(), self.timeout)
                reserved = True
                try:
                    try:
                        state.pending_next = asyncio.ensure_future(anext(iterator))
                        chunk = await self._owned_call(state.pending_next, owner=state)
                    except StopAsyncIteration:
                        break
                    if (
                        not isinstance(chunk, (bytes, bytearray, memoryview))
                        or not 0 < len(chunk) <= min(MAX_BULK_CHUNK_BYTES, total_bytes)
                        or sent_bytes + len(chunk) > total_bytes
                    ):
                        raise BulkTransferError()
                    if not valid() or self._closing:
                        raise BulkTransferError("invalidated")
                    async with self._write_lock:
                        if not valid() or self._closing:
                            raise BulkTransferError("invalidated")
                        frame = encode_bulk_header(
                            kind, transfer_id, state.sent, len(chunk)
                        ) + bytes(chunk)
                        state.available -= 1
                        state.sent += 1
                        reserved = False  # the peer acknowledgement now owns this slot
                        self.writer.write(frame)
                        await asyncio.wait_for(self.writer.drain(), self.timeout)
                    sent_bytes += len(chunk)
                finally:
                    if reserved:
                        self._send_slots.release()
            if sent_bytes != total_bytes or state.sent == 0 or not valid():
                raise BulkTransferError()
            state.end_sent = True
            await self._control(
                "transfer_end",
                state,
                {"sequence_count": state.sent, "total_bytes": total_bytes},
                state.end_id,
            )
            await asyncio.shield(future)
        except asyncio.CancelledError:
            if not self._closing:
                await self.close("cancelled")
            raise
        except Exception as error:
            await self._fail(error)
            raise
        finally:
            try:
                await self._close_source(state)
            except Exception as error:
                if not self._closing:
                    await self._fail(error)
                raise
            else:
                if not any(not task.done() for task in state.owned):
                    self._outgoing.pop(transfer_id, None)
            finally:
                self._send_tasks.discard(owner)

    async def _close_source(self, state: _Outgoing) -> None:
        if state.source_closed:
            return
        if state.pending_next is not None and not state.pending_next.done():
            raise BulkTransferError("source-failed")

        async def finish() -> None:
            closer = getattr(state.source, "aclose", None)
            if closer is not None:
                await closer()
            state.source_closed = True

        await self._owned_call(finish(), owner=state)

    async def _close_consumer(self, state: _Incoming, code: str) -> None:
        if state.on_close is None:
            return

        async def finish() -> None:
            if state.on_close is not None:
                await state.on_close(code)
                state.on_close = None

        await self._owned_call(finish(), owner=state)

    async def _owned_call(self, awaitable: Awaitable, *, owner: _Incoming | _Outgoing) -> Any:
        # asyncio.wait_for can exceed its timeout while a callback suppresses
        # cancellation. Keep that task visible and let close report a failed
        # retirement instead of silently abandoning it or hanging indefinitely.
        task = asyncio.ensure_future(awaitable)
        self._owned_tasks.add(task)
        owner.owned.add(task)

        def finished(done: asyncio.Future) -> None:
            self._owned_tasks.discard(done)
            owner.owned.discard(done)
            _observed(done)

        task.add_done_callback(finished)
        try:
            done, _ = await asyncio.wait({task}, timeout=self.timeout)
            if not done:
                task.cancel()
                raise BulkTransferError("timeout")
            return task.result()
        except asyncio.CancelledError:
            task.cancel()
            raise

    async def _read_loop(self) -> None:
        try:
            while True:
                header = decode_bulk_header(await self.reader.readexactly(BULK_HEADER_BYTES))
                state = self._incoming.get(header["transfer_id"])
                if (
                    state is None
                    or state.open_id is None
                    or not state.valid()
                    or header["kind"] != state.kind
                    or header["sequence"] != state.sequence
                    or state.granted < 1
                    or header["length"] > state.chunk_bytes
                    or state.received + header["length"] > state.total
                ):
                    raise BulkTransferError()
                state.granted -= 1
                chunk = await asyncio.wait_for(
                    self.reader.readexactly(header["length"]), self.timeout
                )
                if not state.valid():
                    raise BulkTransferError("invalidated")
                await self._owned_call(state.sink(chunk), owner=state)
                if not state.valid():
                    raise BulkTransferError("invalidated")
                state.received += len(chunk)
                state.sequence += 1
                state.granted += 1
                await self._control(
                    "transfer_credit",
                    state,
                    {"next_sequence": state.sequence, "credits": 1},
                    state.open_id,
                )
                await self._finish_incoming(state)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await self._fail(error)

    async def _finish_incoming(self, state: _Incoming) -> None:
        if state.end is None or state.received < state.total or state.finishing:
            return
        state.finishing = True
        if (
            state.received != state.total
            or state.end["sequence_count"] != state.sequence
            or not state.valid()
        ):
            raise BulkTransferError()
        # Keep the reservation until cleanup has completed, including async cleanup.
        await self._close_consumer(state, "ok")
        await self._control(
            "transfer_result", state, {"status": "complete", "code": "ok"}, state.end["request_id"]
        )
        self._incoming.pop(state.transfer_id)
        if state.timer is not None:
            state.timer.cancel()
        state.future.set_result(None)

    async def _fail(self, error: Exception) -> None:
        if self._closing:
            return
        self._failure = error
        try:
            await self.close(getattr(error, "code", "protocol-error"))
        finally:
            if self._on_failure is not None:
                result = self._on_failure(error)
                if inspect.isawaitable(result):
                    await result

    async def close(self, code: str = "cancelled") -> None:
        current = asyncio.current_task()
        if self._close_owner is current:
            return
        self._closing = True
        async with self._close_lock:
            self._close_owner = current
            try:
                await self._retire(code, current)
            finally:
                self._close_owner = None

    async def _retire(self, code: str, current: asyncio.Task | None) -> None:
        states = [*self._incoming.values(), *self._outgoing.values()]
        tasks = {
            state.timer
            for state in states
            if state.timer is not None and state.timer is not current
        }
        tasks.update(task for task in self._send_tasks | self._owned_tasks if task is not current)
        if self._reader_task is not None and self._reader_task is not current:
            tasks.add(self._reader_task)
        for task in tasks:
            task.cancel()
        for state in states:
            if isinstance(state, _Outgoing):
                state.changed.set()
        self.writer.close()
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=self.timeout)
        else:
            pending = set()
        pending.update(
            task for task in self._owned_tasks if task is not current and not task.done()
        )
        if pending:
            for state in states:
                if not state.future.done():
                    state.future.set_exception(BulkTransferError("source-failed"))
            # All source/consumer reservations remain visible until these owned
            # tasks finish and an explicit close retry can prove retirement.
            raise BulkTransferError("source-failed")
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.writer.wait_closed(), self.timeout)
        errors = []
        for identity, state in list(self._incoming.items()):
            try:
                await self._close_consumer(state, code)
                self._incoming.pop(identity, None)
            except Exception as error:
                errors.append(error)
        for identity, state in list(self._outgoing.items()):
            try:
                await self._close_source(state)
                self._outgoing.pop(identity, None)
            except Exception as error:
                errors.append(error)
        for state in states:
            if not state.future.done():
                state.future.set_exception(BulkTransferError("source-failed" if errors else code))
        if errors:
            # Failed cleanup remains reserved and is retried by the next close.
            raise BulkTransferError("source-failed") from errors[0]
