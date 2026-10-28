"""Bounded, explicitly authorized binary transfers on the private per-boot channel.

Only the current broker can authorize an incoming operation. Control metadata is
never authority. This is IPC flow control, not a claim about HAP/HDS socket drains.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import math
import time
import uuid
from collections import OrderedDict
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
MAX_TERMINALS = 4096
TERMINAL_SECONDS = 60.0


class BulkTransferError(HomeKitProtocolError):
    """A bounded transfer or its shared framing could not complete safely."""

    def __init__(self, code: str = "protocol-error") -> None:
        super().__init__(f"Private binary transfer failed ({code})")
        self.code = code


def _observed(future: asyncio.Future) -> None:
    if not future.cancelled():
        future.exception()


def _future():
    future = asyncio.get_running_loop().create_future()
    future.add_done_callback(_observed)
    return future


@dataclass(kw_only=True)
class _Lifecycle:
    cancelled: str | None = None
    abort: asyncio.Event = field(default_factory=asyncio.Event)
    wire_done: asyncio.Future = field(default_factory=_future)
    cancel_task: asyncio.Task | None = None
    cleanup_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    cleanup_failed: bool = False
    fence_id: str | None = None
    fence: dict | None = None
    terminal_committed: bool = False


@dataclass
class _Incoming(_Lifecycle):
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
    reading_bytes: int = 0
    end: dict[str, Any] | None = None
    timer: asyncio.Task | None = None
    finishing: bool = False
    owned: set[asyncio.Task] = field(default_factory=set)
    opened: asyncio.Event = field(default_factory=asyncio.Event)
    finish_task: asyncio.Task | None = None


@dataclass
class _Outgoing(_Lifecycle):
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
    sent_bytes: int = 0
    open_task: asyncio.Task | None = None
    open_enqueued: bool = False
    normal_result: bool = False


@dataclass
class _Terminal:
    scope: dict
    operation_id: str
    transfer_id: str
    kind: str
    total: int
    incoming: bool
    sequence: int
    received: int
    open_id: str | None
    end_id: str | None
    fence_id: str | None
    fence: dict | None
    complete: bool
    expires: float


class ReceiveHandle:
    """A finite authorization, retained until its consumer and cleanup finish."""

    def __init__(self, channel: BulkChannel, state: _Incoming) -> None:
        self._channel, self._state = channel, state

    @property
    def retirement_complete(self) -> bool:
        """Wire and actual consumer ownership have both left the channel."""
        return (
            self._channel._incoming.get(self._state.transfer_id) is not self._state
            and not self._state.cleanup_failed
        )

    async def wait(self) -> None:
        try:
            await asyncio.shield(self._state.future)
        except asyncio.CancelledError:
            await self.cancel()
            raise

    async def cancel(self, code="cancelled") -> None:
        await self._channel.cancel_transfer(self._state.transfer_id, code)


class SendHandle:
    """A bounded producer reservation; OPEN starts only when send is invoked."""

    def __init__(self, channel: BulkChannel, state: _Outgoing):
        self._channel, self._state = channel, state

    @property
    def peer_completed(self) -> bool:
        """An exact normal result was validated; native cleanup may remain.

        A cancellation/fence acknowledgement never supplies this proof. Callers
        must still join actual retirement before reusing source ownership.
        """
        return (
            self._state.normal_result
            and self._state.wire_done.done()
            and self._state.cancelled is None
        )

    @property
    def retirement_complete(self) -> bool:
        """The source has actually retired; a fence ACK alone is insufficient."""
        return (
            self._channel._outgoing.get(self._state.transfer_id) is not self._state
            and not self._state.cleanup_failed
        )

    async def send(self, source: AsyncIterable[bytes]) -> None:
        await self._channel._send(self._state, source)

    async def cancel(self, code: str = "cancelled") -> None:
        await self._channel.cancel_transfer(self._state.transfer_id, code)


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
        self._terminals: OrderedDict[str, _Terminal] = OrderedDict()
        self._cancel_tasks: set[asyncio.Task] = set()
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

    @property
    def pending_resources(self):
        return len(self._incoming) + len(self._outgoing)

    @property
    def cleanup_pending(self):
        return sum(
            state.cleanup_failed for state in (*self._incoming.values(), *self._outgoing.values())
        )

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
        self._prune_terminals()
        if transfer_id in self._terminals:
            raise BulkTransferError("conflict")
        if len(self._terminals) + self.pending_resources >= MAX_TERMINALS:
            raise BulkTransferError("busy")
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
        state.timer = asyncio.create_task(self._deadline(state), name="homekit-bulk-deadline")
        state.timer.add_done_callback(_observed)
        return ReceiveHandle(self, state)

    async def _deadline(self, state) -> None:
        await asyncio.sleep(self.timeout)
        self._begin_cancel(state, "timeout")

    def _prune_terminals(self) -> None:
        now = time.monotonic()
        for identity, terminal in list(self._terminals.items()):
            if terminal.expires <= now:
                self._terminals.pop(identity)

    def _remember(self, state: _Incoming | _Outgoing) -> None:
        incoming = isinstance(state, _Incoming)
        self._prune_terminals()
        self._terminals[state.transfer_id] = _Terminal(
            dict(state.scope),
            state.operation_id,
            state.transfer_id,
            state.kind,
            state.total,
            incoming,
            state.sequence if incoming else state.sent,
            state.received if incoming else state.sent_bytes,
            state.open_id,
            state.end["request_id"]
            if incoming and state.end
            else state.end_id
            if not incoming and state.end_sent
            else None,
            state.fence_id,
            dict(state.fence) if state.fence else None,
            state.terminal_committed,
            time.monotonic() + TERMINAL_SECONDS,
        )

    @staticmethod
    def _payload(state, extra: dict) -> dict:
        return {
            "scope": state.scope,
            "operation_id": state.operation_id,
            "transfer_id": state.transfer_id,
            **extra,
        }

    async def _control(self, kind: str, state, extra: dict, request_id: str) -> None:
        self._ensure_open()
        if kind == "transfer_end" and (not state.valid() or state.cancelled is not None):
            raise BulkTransferError("invalidated")
        await self._owned_call(
            self._send_control(kind, self._payload(state, extra), request_id),
            owner=state,
            native=False,
            shield_cancel=True,
        )

    @staticmethod
    def _matches(state, payload: dict[str, Any]) -> bool:
        return state.scope == payload["scope"] and state.operation_id == payload["operation_id"]

    async def _terminal_control(self, state: _Terminal, kind: str, payload: dict, request_id: str):
        if not self._matches(state, payload):
            raise BulkTransferError("invalidated")
        if kind == "transfer_cancel" and not state.incoming and state.open_id is not None:
            if state.fence is None:
                state.fence_id = str(uuid.uuid4())
                state.fence = self._payload(
                    state,
                    {
                        "sequence_count": state.sequence,
                        "sent_bytes": state.received,
                        "code": payload["code"],
                    },
                )
            await self._send_control("transfer_fence", state.fence, state.fence_id)
        elif kind == "transfer_fence" and state.incoming and state.open_id is not None:
            if (
                payload["sequence_count"] != state.sequence
                or payload["sent_bytes"] != state.received
                or (
                    state.fence is not None
                    and (state.fence != payload or state.fence_id != request_id)
                )
            ):
                raise BulkTransferError()
            state.fence, state.fence_id = dict(payload), request_id
            await self._send_control("transfer_fence_ack", payload, request_id)
        elif kind == "transfer_fence_ack" and not state.incoming:
            if state.fence != payload or state.fence_id != request_id:
                raise BulkTransferError()
        elif kind == "transfer_result" and not state.incoming:
            if (
                request_id != state.end_id
                or payload["status"] != "complete"
                or payload["code"] != "ok"
            ):
                raise BulkTransferError()
        elif kind == "transfer_credit" and not state.incoming:
            if (
                request_id != state.open_id
                or payload["next_sequence"] > state.sequence
                or not 1 <= payload["credits"] <= MAX_BULK_WINDOW_CHUNKS
            ):
                raise BulkTransferError()
        elif kind == "transfer_end" and state.incoming:
            if (
                request_id != state.end_id
                or payload["total_bytes"] != state.total
                or payload["sequence_count"] != state.sequence
            ):
                raise BulkTransferError()
        else:
            raise BulkTransferError()

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
            identity = payload["transfer_id"]
            if kind in {"transfer_open", "transfer_end", "transfer_fence"}:
                state = self._incoming.get(identity)
            elif kind in {
                "transfer_credit",
                "transfer_result",
                "transfer_cancel",
                "transfer_fence_ack",
            }:
                state = self._outgoing.get(identity)
            else:
                raise BulkTransferError()
            if state is None:
                self._prune_terminals()
                terminal = self._terminals.get(identity)
                if terminal is None:
                    raise BulkTransferError("invalidated")
                await self._terminal_control(terminal, kind, payload, request_id)
                return True
            if not self._matches(state, payload):
                raise BulkTransferError("invalidated")
            if kind == "transfer_open":
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
                state.opened.set()
                if not state.valid():
                    self._begin_cancel(state, "invalidated")
                if state.cancelled is None:
                    await self._grant_credit(state, state.window)
            elif kind == "transfer_credit":
                if request_id != state.open_id:
                    raise BulkTransferError()
                sequence, credits = payload["next_sequence"], payload["credits"]
                if state.wire_done.done():
                    if sequence > state.sent:
                        raise BulkTransferError()
                    return True
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
                self._finish_incoming(state)
            elif kind == "transfer_result":
                if (
                    request_id != state.end_id
                    or not state.end_sent
                    or payload["status"] != "complete"
                    or payload["code"] != "ok"
                    or state.acknowledged != state.sent
                ):
                    raise BulkTransferError()
                state.normal_result = state.terminal_committed = True
                if state.cancelled is None:
                    self._wire_complete(state)
                    if not state.future.done():
                        state.future.set_result(None)
                state.changed.set()
            elif kind == "transfer_cancel":
                self._begin_cancel(state, payload["code"])
                if state.wire_done.done() and state.fence is None:
                    # Completion won the race, but a consumer may still need its
                    # cancellation acknowledged after the normal result crossed.
                    state.fence_id = str(uuid.uuid4())
                    state.fence = self._payload(
                        state,
                        {
                            "sequence_count": state.sent,
                            "sent_bytes": state.sent_bytes,
                            "code": payload["code"],
                        },
                    )
                    await self._control(
                        "transfer_fence",
                        state,
                        {k: state.fence[k] for k in ("sequence_count", "sent_bytes", "code")},
                        state.fence_id,
                    )
            elif kind == "transfer_fence":
                if (
                    state.open_id is None
                    or payload["sequence_count"] < state.sequence
                    or payload["sequence_count"]
                    > state.sequence + state.granted + bool(state.reading_bytes)
                    or payload["sent_bytes"] < state.received
                    or payload["sent_bytes"] > state.total
                    or (
                        (payload["sequence_count"] == state.sequence)
                        != (payload["sent_bytes"] == state.received)
                    )
                    or payload["sent_bytes"] - state.received
                    < max(payload["sequence_count"] - state.sequence, state.reading_bytes)
                    or payload["sent_bytes"] - state.received
                    > (payload["sequence_count"] - state.sequence) * state.chunk_bytes
                    or (
                        state.fence is not None
                        and (state.fence != payload or state.fence_id != request_id)
                    )
                ):
                    raise BulkTransferError()
                state.fence, state.fence_id = dict(payload), request_id
                self._begin_cancel(state, payload["code"])
                await self._maybe_ack_fence(state)
            elif kind == "transfer_fence_ack":
                if state.fence != payload or state.fence_id != request_id:
                    raise BulkTransferError()
                if not state.wire_done.done():
                    for _ in range(state.sent - state.acknowledged):
                        self._send_slots.release()
                    state.acknowledged = state.sent
                    self._wire_complete(state)
            return True
        except Exception as error:
            await self._fail(error)
            raise

    def prepare_send(
        self,
        *,
        scope: dict[str, Any],
        operation_id: str,
        transfer_id: str,
        kind: str,
        total_bytes: int,
        valid: Callable[[], bool] = lambda: True,
    ):
        copied = self._identity(scope, operation_id, transfer_id, kind, total_bytes, incoming=False)
        if len(self._outgoing) >= MAX_TRANSFERS:
            raise BulkTransferError("busy")
        if not callable(valid) or not valid():
            raise BulkTransferError("invalidated")
        state = _Outgoing(
            copied,
            operation_id,
            transfer_id,
            kind,
            total_bytes,
            str(uuid.uuid4()),
            str(uuid.uuid4()),
            valid,
            _future(),
        )
        self._outgoing[transfer_id] = state
        state.timer = asyncio.create_task(self._deadline(state), name="homekit-bulk-deadline")
        state.timer.add_done_callback(_observed)
        return SendHandle(self, state)

    async def send(self, *, source: AsyncIterable[bytes], **identity) -> None:
        await self.prepare_send(**identity).send(source)

    async def _send(self, state: _Outgoing, source: AsyncIterable[bytes]) -> None:
        if state.source is not None or state.cancelled is not None or state.wire_done.done():
            raise BulkTransferError(state.cancelled or "conflict")
        state.source = source.__aiter__()
        owner = asyncio.current_task()
        assert owner is not None
        self._send_tasks.add(owner)
        try:
            self._check_live(state)
            state.open_task = asyncio.create_task(
                self._control(
                    "transfer_open",
                    state,
                    {
                        "kind": state.kind,
                        "total_bytes": state.total,
                        "chunk_bytes": min(MAX_BULK_CHUNK_BYTES, state.total),
                        "window_chunks": MAX_BULK_WINDOW_CHUNKS,
                    },
                    state.open_id,
                )
            )
            state.open_task.add_done_callback(_observed)
            await asyncio.shield(state.open_task)
            state.open_enqueued = True
            while True:
                self._check_live(state)
                while state.available == 0:
                    state.changed.clear()
                    self._check_live(state)
                    await self._wait_abort(state.changed.wait(), state)
                await self._acquire_slot(state)
                reserved = True
                try:
                    self._check_live(state)
                    state.pending_next = asyncio.ensure_future(anext(state.source))
                    try:
                        chunk = await self._owned_call(
                            state.pending_next, owner=state, interruptible=True
                        )
                    except StopAsyncIteration:
                        break
                    if (
                        not isinstance(chunk, (bytes, bytearray, memoryview))
                        or not 0 < len(chunk) <= min(MAX_BULK_CHUNK_BYTES, state.total)
                        or state.sent_bytes + len(chunk) > state.total
                    ):
                        raise BulkTransferError()
                    async with self._write_lock:
                        self._check_live(state)
                        frame = encode_bulk_header(
                            state.kind, state.transfer_id, state.sent, len(chunk)
                        ) + bytes(chunk)
                        state.available -= 1
                        state.sent += 1
                        state.sent_bytes += len(chunk)
                        reserved = False
                        self.writer.write(frame)
                        # A cancelled drain cannot erase an already enqueued frame.
                        await asyncio.wait_for(self.writer.drain(), self.timeout)
                finally:
                    if reserved:
                        self._send_slots.release()
            if state.sent_bytes != state.total or state.sent == 0:
                raise BulkTransferError()
            self._check_live(state)
            state.end_sent = True
            await self._control(
                "transfer_end",
                state,
                {"sequence_count": state.sent, "total_bytes": state.total},
                state.end_id,
            )
            await asyncio.shield(state.future)
        except asyncio.CancelledError:
            if not self._closing:
                await self.cancel_transfer(state.transfer_id)
            raise
        except BulkTransferError as error:
            if state.cancelled is not None or error.code in {"invalidated", "timeout"}:
                self._begin_cancel(state, state.cancelled or error.code)
                if state.cancel_task is not None:
                    await asyncio.shield(state.cancel_task)
                raise BulkTransferError(state.cancelled or error.code) from error
            await self._fail(error)
            raise
        except Exception as error:
            await self._fail(error)
            raise
        finally:
            self._send_tasks.discard(owner)
            if not self._closing and state.cancelled is None and state.wire_done.done():
                await self._cleanup(state)
                if state.cleanup_failed:
                    raise BulkTransferError("source-failed")

    def _check_live(self, state) -> None:
        if self._closing or state.cancelled is not None:
            raise BulkTransferError(state.cancelled or "cancelled")
        if not state.valid():
            self._begin_cancel(state, "invalidated")
            raise BulkTransferError("invalidated")

    async def _wait_abort(self, awaitable, state):
        return await self._owned_call(awaitable, owner=state, native=False, interruptible=True)

    async def _acquire_slot(self, state: _Outgoing) -> None:
        acquire = asyncio.create_task(self._send_slots.acquire())
        try:
            await self._wait_abort(acquire, state)
        except BaseException:
            # Cancellation may be delivered after the acquire task took the
            # slot but before this caller resumes and installs its reservation.
            # Semaphore.acquire itself compensates cancellation before success.
            if acquire.done() and not acquire.cancelled() and acquire.exception() is None:
                if acquire.result():
                    self._send_slots.release()
            else:
                acquire.cancel()
            raise

    def _begin_cancel(self, state, code: str):
        if code not in {"cancelled", "invalidated", "timeout"}:
            raise ValueError("Invalid transfer cancellation code")
        if state.terminal_committed and state.cancelled is None:
            return None
        if state.cancelled is None:
            state.cancelled = code
            state.abort.set()
            if isinstance(state, _Outgoing):
                state.changed.set()
        if state.timer is not None and state.timer is not asyncio.current_task():
            state.timer.cancel()
        if state.cancel_task is None:
            state.cancel_task = asyncio.create_task(self._cancel(state), name="homekit-bulk-cancel")
            self._cancel_tasks.add(state.cancel_task)
            state.cancel_task.add_done_callback(self._cancel_tasks.discard)
            state.cancel_task.add_done_callback(_observed)
        return state.cancel_task

    async def cancel_transfer(self, transfer_id: str, code: str = "cancelled") -> None:
        if code not in {"cancelled", "invalidated", "timeout"}:
            raise ValueError("Invalid transfer cancellation code")
        state = self._incoming.get(transfer_id) or self._outgoing.get(transfer_id)
        if state is None:
            return
        task = self._begin_cancel(state, code)
        if task is not None:
            await asyncio.shield(task)
            if state.wire_done.done() and state.cleanup_failed:
                await self._cleanup(state)
        elif state.wire_done.done():
            await self._cleanup(state)

    async def _cancel(self, state) -> None:
        try:
            if isinstance(state, _Outgoing):
                if state.open_task is None:
                    self._wire_complete(state)
                else:
                    await asyncio.wait_for(asyncio.shield(state.open_task), self.timeout)
                    state.open_enqueued = True
                    # Holding the writer barrier proves no producer frame can be
                    # enqueued after this exact frontier, even a cancelled drain.
                    async with self._write_lock:
                        state.fence_id = str(uuid.uuid4())
                        state.fence = self._payload(
                            state,
                            {
                                "sequence_count": state.sent,
                                "sent_bytes": state.sent_bytes,
                                "code": state.cancelled,
                            },
                        )
                    await self._control(
                        "transfer_fence",
                        state,
                        {k: state.fence[k] for k in ("sequence_count", "sent_bytes", "code")},
                        state.fence_id,
                    )
                    await asyncio.wait_for(asyncio.shield(state.wire_done), self.timeout)
            else:
                if state.open_id is None:
                    try:
                        await asyncio.wait_for(state.opened.wait(), self.timeout)
                    except TimeoutError:
                        # A trusted authorization never opened; no remote state
                        # exists and no unowned cancellation metadata is sent.
                        self._wire_complete(state)
                if not state.wire_done.done():
                    if state.fence is None:
                        await self._control(
                            "transfer_cancel", state, {"code": state.cancelled}, str(uuid.uuid4())
                        )
                    await asyncio.wait_for(asyncio.shield(state.wire_done), self.timeout)
            if not state.future.done():
                state.future.set_exception(BulkTransferError(state.cancelled))
            await self._cleanup(state)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await self._fail(error)

    def _wire_complete(self, state) -> None:
        if state.timer is not None:
            state.timer.cancel()
        if not state.wire_done.done():
            state.wire_done.set_result(None)

    async def _maybe_ack_fence(self, state: _Incoming) -> None:
        if (
            state.fence is None
            or state.wire_done.done()
            or state.sequence != state.fence["sequence_count"]
        ):
            return
        if state.received != state.fence["sent_bytes"]:
            raise BulkTransferError()
        await self._control(
            "transfer_fence_ack",
            state,
            {k: state.fence[k] for k in ("sequence_count", "sent_bytes", "code")},
            state.fence_id,
        )
        self._wire_complete(state)

    async def _grant_credit(self, state: _Incoming, credits: int) -> None:
        # Validate and account before the first await. A later privacy fence
        # still drains this already committed grant without invoking the sink.
        if state.cancelled is not None:
            return
        if not state.valid():
            self._begin_cancel(state, "invalidated")
            return
        state.granted += credits
        await self._control(
            "transfer_credit",
            state,
            {"next_sequence": state.sequence, "credits": credits},
            state.open_id,
        )

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

    async def _cleanup(self, state) -> None:
        async with state.cleanup_lock:
            pending = {task for task in state.owned if not task.done()}
            for task in pending:
                task.cancel()
            if pending:
                _, pending = await asyncio.wait(pending, timeout=self.timeout)
            if pending:
                state.cleanup_failed = True
                return
            try:
                if isinstance(state, _Incoming):
                    await self._close_consumer(state, state.cancelled or "ok")
                    self._incoming.pop(state.transfer_id, None)
                else:
                    await self._close_source(state)
                    self._outgoing.pop(state.transfer_id, None)
            except Exception:
                state.cleanup_failed = True
                return
            state.cleanup_failed = False
            self._remember(state)

    async def _owned_call(
        self,
        awaitable: Awaitable,
        *,
        owner,
        native=True,
        interruptible=False,
        shield_cancel=False,
    ) -> Any:
        task = asyncio.ensure_future(awaitable)
        self._owned_tasks.add(task)
        if native:
            owner.owned.add(task)

        def finished(done):
            self._owned_tasks.discard(done)
            owner.owned.discard(done)
            _observed(done)

        task.add_done_callback(finished)
        abort = asyncio.create_task(owner.abort.wait()) if interruptible else None
        try:
            done, _ = await asyncio.wait(
                {task, abort} if abort else {task},
                timeout=self.timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if task in done:
                if task.cancelled():
                    raise BulkTransferError(owner.cancelled or "source-failed")
                return task.result()
            if abort is not None and abort in done:
                task.cancel()
                raise BulkTransferError(owner.cancelled or "cancelled")
            task.cancel()
            raise BulkTransferError("timeout")
        except asyncio.CancelledError:
            if shield_cancel and not self._closing:
                # A control envelope may already be partially written. Ordinary
                # job cancellation must settle it before a terminal envelope.
                done, _ = await asyncio.wait({task}, timeout=self.timeout)
                if not done:
                    task.cancel()
                    raise BulkTransferError("timeout") from None
                task.result()
            else:
                task.cancel()
            raise
        finally:
            if abort is not None:
                abort.cancel()
                await asyncio.gather(abort, return_exceptions=True)

    async def _read_loop(self) -> None:
        try:
            while True:
                header = decode_bulk_header(await self.reader.readexactly(BULK_HEADER_BYTES))
                state = self._incoming.get(header["transfer_id"])
                if (
                    state is None
                    or state.open_id is None
                    or state.wire_done.done()
                    or header["kind"] != state.kind
                    or header["sequence"] != state.sequence
                    or state.granted < 1
                    or header["length"] > state.chunk_bytes
                    or state.received + header["length"] > state.total
                    or (
                        state.fence is not None
                        and header["sequence"] >= state.fence["sequence_count"]
                    )
                ):
                    raise BulkTransferError()
                if not state.valid():
                    self._begin_cancel(state, "invalidated")
                state.granted -= 1
                state.reading_bytes = header["length"]
                chunk = await asyncio.wait_for(
                    self.reader.readexactly(header["length"]), self.timeout
                )
                # These count physical complete frames, not sink completion.
                state.received += len(chunk)
                state.sequence += 1
                state.reading_bytes = 0
                if not state.valid():
                    self._begin_cancel(state, "invalidated")
                if state.cancelled is None:
                    try:
                        await self._owned_call(state.sink(chunk), owner=state, interruptible=True)
                    except BulkTransferError as error:
                        self._begin_cancel(
                            state,
                            error.code if error.code in {"invalidated", "timeout"} else "cancelled",
                        )
                    except Exception:
                        self._begin_cancel(state, "cancelled")
                if state.cancelled is None and not state.valid():
                    self._begin_cancel(state, "invalidated")
                if state.cancelled is None:
                    await self._grant_credit(state, 1)
                    self._finish_incoming(state)
                if state.cancelled is not None:
                    await self._maybe_ack_fence(state)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await self._fail(error)

    def _finish_incoming(self, state: _Incoming) -> None:
        if state.end is None or state.received < state.total or state.finishing or state.cancelled:
            return
        if state.received != state.total or state.end["sequence_count"] != state.sequence:
            raise BulkTransferError()
        state.finishing = True
        state.finish_task = asyncio.create_task(self._finish(state), name="homekit-bulk-complete")
        self._cancel_tasks.add(state.finish_task)
        state.finish_task.add_done_callback(self._cancel_tasks.discard)
        state.finish_task.add_done_callback(_observed)

    async def _finish(self, state: _Incoming) -> None:
        try:
            await self._owned_call(
                self._close_consumer(state, "ok"), owner=state, native=False, interruptible=True
            )
            self._check_live(state)
            # Commit before starting metadata: cancellation cannot interrupt a
            # partially sent normal result, and an exact later fence is replayed.
            state.terminal_committed = True
            await self._control(
                "transfer_result",
                state,
                {"status": "complete", "code": "ok"},
                state.end["request_id"],
            )
            self._wire_complete(state)
            if not state.future.done():
                state.future.set_result(None)
            await self._cleanup(state)
        except BulkTransferError as error:
            self._begin_cancel(
                state, error.code if error.code in {"invalidated", "timeout"} else "cancelled"
            )
        except Exception as error:
            await self._fail(error)

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
        tasks.update(
            task
            for task in self._send_tasks | self._owned_tasks | self._cancel_tasks
            if task is not current
        )
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
                state.cleanup_failed = True
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
                state.cleanup_failed = False
            except Exception as error:
                state.cleanup_failed = True
                errors.append(error)
        for identity, state in list(self._outgoing.items()):
            try:
                await self._close_source(state)
                self._outgoing.pop(identity, None)
                state.cleanup_failed = False
            except Exception as error:
                state.cleanup_failed = True
                errors.append(error)
        for state in states:
            if not state.future.done():
                state.future.set_exception(BulkTransferError("source-failed" if errors else code))
        if errors:
            # Failed cleanup remains reserved and is retried by the next close.
            raise BulkTransferError("source-failed") from errors[0]
