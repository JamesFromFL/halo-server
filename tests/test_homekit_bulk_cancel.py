"""Actual socketpair cancellation, framing frontiers and retained native owners."""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid

import pytest
from test_homekit_bulk import chunks, identity, pair

from halo_server.homekit.bulk import MAX_TERMINALS, BulkTransferError
from halo_server.homekit.ipc import BULK_HEADER_BYTES, encode_bulk_header


async def until(predicate):
    async with asyncio.timeout(1):
        while not predicate():
            await asyncio.sleep(0)


async def discard(_):
    pass


async def roundtrip(sender, receiver):
    data = []

    async def sink(chunk):
        data.append(chunk)

    info = identity(size=2)
    handle = receiver.authorize_receive(**info, sink=sink)
    await asyncio.wait_for(sender.send(**info, source=chunks(b"ab")), 1)
    await handle.wait()
    assert data == [b"ab"]


def envelope(kind, info, boot, payload, request_id=None):
    return dict(
        v=1,
        kind=kind,
        boot_id=boot,
        request_id=request_id or str(uuid.uuid4()),
        payload=dict(
            scope=info["scope"],
            operation_id=info["operation_id"],
            transfer_id=info["transfer_id"],
            **payload,
        ),
    )


@pytest.mark.asyncio
async def test_consumer_cancel_drains_two_credited_chunks_and_unblocks_other_transfer():
    async with pair() as (sender, receiver, controls, _):
        entered = asyncio.Event()
        calls = []

        async def sink(chunk):
            calls.append(chunk)
            entered.set()
            await asyncio.Event().wait()

        info = identity(size=3)
        handle = receiver.authorize_receive(**info, sink=sink)
        sending = asyncio.create_task(sender.send(**info, source=chunks(b"a", b"b", b"c")))
        await entered.wait()
        await until(lambda: sender._outgoing[info["transfer_id"]].sent == 2)
        await asyncio.wait_for(handle.cancel(), 1)
        with pytest.raises(BulkTransferError, match="cancelled"):
            await handle.wait()
        with pytest.raises(BulkTransferError, match="cancelled"):
            await sending
        assert calls == [b"a"]
        assert not sender.closed and not receiver.closed
        assert sender.counts == receiver.counts == (0, 0)
        fence = [m for m in controls[0] if m["kind"] == "transfer_fence"][0]
        ack = [m for m in controls[1] if m["kind"] == "transfer_fence_ack"][0]
        assert fence["payload"] == ack["payload"]
        assert fence["request_id"] == ack["request_id"]
        assert fence["payload"]["sequence_count"] == fence["payload"]["sent_bytes"] == 2
        await roundtrip(sender, receiver)
        assert sender._send_slots._value == 2


@pytest.mark.asyncio
async def test_producer_cancel_stubborn_source_retains_cleanup_without_holding_wire():
    async with pair(timeout=0.05) as (sender, receiver, _, _):
        entered, release = asyncio.Event(), asyncio.Event()

        class Producer:
            closed = False

            def __aiter__(self):
                return self

            async def __anext__(self):
                entered.set()
                while not release.is_set():
                    with contextlib.suppress(asyncio.CancelledError):
                        await release.wait()
                raise StopAsyncIteration

            async def aclose(self):
                self.closed = True

        producer = Producer()
        info = identity(size=1)
        handle = receiver.authorize_receive(**info, sink=discard)
        prepared = sender.prepare_send(**info)
        sending = asyncio.create_task(prepared.send(producer))
        await entered.wait()
        await asyncio.wait_for(sender.cancel_transfer(info["transfer_id"]), 0.5)
        with pytest.raises(BulkTransferError):
            await handle.wait()
        with pytest.raises(BulkTransferError):
            await sending
        assert sender.counts == (0, 1) and sender.cleanup_pending == 1
        assert sender.pending_resources == 1 and not producer.closed
        assert not prepared.retirement_complete
        assert not prepared.peer_completed
        assert not sender.closed and not receiver.closed
        await roundtrip(sender, receiver)
        release.set()
        await until(lambda: not sender._owned_tasks)
        await sender.cancel_transfer(info["transfer_id"])
        assert producer.closed and sender.counts == (0, 0) and sender.cleanup_pending == 0
        assert prepared.retirement_complete
        assert not prepared.peer_completed


@pytest.mark.asyncio
async def test_normal_peer_completion_proof_precedes_actual_source_close_retirement():
    async with pair() as (sender, receiver, _, _):
        entered, release = asyncio.Event(), asyncio.Event()

        class Producer:
            sent = False

            def __aiter__(self):
                return self

            async def __anext__(self):
                if self.sent:
                    raise StopAsyncIteration
                self.sent = True
                return b"a"

            async def aclose(self):
                entered.set()
                await release.wait()

        info = identity(size=1)
        received = receiver.authorize_receive(**info, sink=discard)
        prepared = sender.prepare_send(**info)
        sending = asyncio.create_task(prepared.send(Producer()))
        try:
            assert not prepared.peer_completed
            await entered.wait()
            await received.wait()
            assert prepared.peer_completed and not prepared.retirement_complete
            assert not sending.done() and sender.pending_resources == 1
            release.set()
            await sending
            assert prepared.peer_completed and prepared.retirement_complete
        finally:
            release.set()
            await asyncio.gather(sending, return_exceptions=True)


@pytest.mark.asyncio
async def test_stubborn_sink_wire_drains_before_native_cleanup_and_retry():
    async with pair(timeout=0.05) as (sender, receiver, _, _):
        entered, release = asyncio.Event(), asyncio.Event()
        closed = []

        async def sink(_):
            entered.set()
            while not release.is_set():
                with contextlib.suppress(asyncio.CancelledError):
                    await release.wait()

        async def close(code):
            closed.append(code)

        info = identity(size=2)
        handle = receiver.authorize_receive(**info, sink=sink, on_close=close)
        sending = asyncio.create_task(sender.send(**info, source=chunks(b"a", b"b")))
        await entered.wait()
        await handle.cancel()
        await asyncio.gather(sending, return_exceptions=True)
        assert receiver.counts == (1, 0) and receiver.cleanup_pending == 1
        assert not handle.retirement_complete
        assert not closed
        assert not sender.closed and not receiver.closed
        await roundtrip(sender, receiver)
        release.set()
        await until(lambda: not receiver._owned_tasks)
        await handle.cancel()
        assert closed == ["cancelled"]
        assert receiver.counts == (0, 0)
        assert handle.retirement_complete


@pytest.mark.asyncio
async def test_prepared_send_reserves_before_open_and_local_cancel_has_no_wire():
    async with pair() as (sender, receiver, controls, _):
        info = identity(size=1)
        prepared = sender.prepare_send(**info)
        assert sender.counts == (0, 1) and not controls[0]
        assert not prepared.retirement_complete
        with pytest.raises(BulkTransferError, match="conflict"):
            sender.prepare_send(**info)
        await prepared.cancel()
        assert sender.counts == (0, 0) and not controls[0]
        assert prepared.retirement_complete
        with pytest.raises(BulkTransferError, match="cancelled"):
            await prepared.send(chunks(b"a"))
        await roundtrip(sender, receiver)


@pytest.mark.asyncio
async def test_cancel_before_open_waits_exact_authorization_and_grants_zero_credit():
    async with pair(timeout=0.2) as (sender, receiver, controls, _):
        info = identity(size=1)
        handle = receiver.authorize_receive(**info, sink=discard)
        cancelling = asyncio.create_task(handle.cancel())
        await asyncio.sleep(0.01)
        assert not controls[1]
        sending = asyncio.create_task(sender.send(**info, source=chunks(b"a")))
        await asyncio.wait_for(cancelling, 0.5)
        await asyncio.gather(sending, return_exceptions=True)
        assert all(m["kind"] != "transfer_credit" for m in controls[1])
        fence = next(m for m in controls[0] if m["kind"] == "transfer_fence")
        assert fence["payload"]["sequence_count"] == fence["payload"]["sent_bytes"] == 0
        assert not sender.closed and not receiver.closed
        await roundtrip(sender, receiver)


@pytest.mark.asyncio
async def test_cancelled_unopened_authorization_expires_locally_and_late_open_is_unowned():
    async with pair(timeout=0.02) as (_, receiver, controls, boot):
        info = identity(size=1)
        handle = receiver.authorize_receive(**info, sink=discard)
        await handle.cancel()
        assert not receiver.closed and not controls[1] and receiver.counts == (0, 0)
        with pytest.raises(BulkTransferError):
            await receiver.handle_control(
                envelope(
                    "transfer_open",
                    info,
                    boot,
                    dict(kind="snapshot", total_bytes=1, chunk_bytes=1, window_chunks=2),
                )
            )
        assert receiver.closed


@pytest.mark.asyncio
async def test_fragmented_frame_cancellation_preserves_next_frame_boundary():
    async with pair() as (sender, receiver, controls, _):
        info = identity(size=6)
        delivered = []
        handle = receiver.authorize_receive(**info, sink=lambda data: _append(delivered, data))
        # Faithful raw producer: preserve its owned outgoing state and OPEN, then
        # feed a deliberately fragmented frame through the actual socket.
        prepared = sender.prepare_send(**info)
        state = prepared._state
        state.open_task = asyncio.create_task(
            sender._control(
                "transfer_open",
                state,
                dict(kind="snapshot", total_bytes=6, chunk_bytes=6, window_chunks=2),
                state.open_id,
            )
        )
        await state.open_task
        await until(lambda: state.initialized)
        await sender._send_slots.acquire()
        state.sent, state.sent_bytes, state.available = 1, 6, 1
        frame = encode_bulk_header("snapshot", info["transfer_id"], 0, 6) + b"secret"
        sender.writer.write(frame[: BULK_HEADER_BYTES + 2])
        await sender.writer.drain()
        await until(lambda: handle._state.granted == 1)
        cancelling = asyncio.create_task(handle.cancel())
        await until(lambda: any(m["kind"] == "transfer_fence" for m in controls[0]))
        assert not cancelling.done()
        sender.writer.write(frame[BULK_HEADER_BYTES + 2 :])
        await sender.writer.drain()
        await cancelling
        assert not delivered and not receiver.closed and not sender.closed
        await roundtrip(sender, receiver)


async def _append(target, data):
    target.append(data)


@pytest.mark.asyncio
async def test_cancel_waiting_metadata_open_cannot_emit_fence_before_open():
    async with pair() as (sender, receiver, controls, _):
        entered, release = asyncio.Event(), asyncio.Event()
        original = sender._send_control

        async def delayed(kind, *args):
            if kind == "transfer_open":
                entered.set()
                await release.wait()
            await original(kind, *args)

        sender._send_control = delayed
        info = identity(size=1)
        receiver.authorize_receive(**info, sink=discard)
        sending = asyncio.create_task(sender.send(**info, source=chunks(b"a")))
        await entered.wait()
        stopping = asyncio.create_task(sender.cancel_transfer(info["transfer_id"]))
        await asyncio.sleep(0.01)
        assert not controls[0]
        release.set()
        await stopping
        await asyncio.gather(sending, return_exceptions=True)
        assert [m["kind"] for m in controls[0]] == ["transfer_open", "transfer_fence"]
        assert not sender.closed and not receiver.closed


@pytest.mark.asyncio
async def test_simultaneous_cancel_uses_one_stable_producer_frontier():
    async with pair() as (sender, receiver, controls, _):
        entered = asyncio.Event()

        async def sink(_):
            entered.set()
            await asyncio.Event().wait()

        info = identity(size=3)
        handle = receiver.authorize_receive(**info, sink=sink)
        sending = asyncio.create_task(sender.send(**info, source=chunks(b"a", b"b", b"c")))
        await entered.wait()
        await asyncio.gather(handle.cancel(), sender.cancel_transfer(info["transfer_id"]))
        await asyncio.gather(sending, return_exceptions=True)
        assert len([m for m in controls[0] if m["kind"] == "transfer_fence"]) == 1
        assert not sender.closed and not receiver.closed
        await roundtrip(sender, receiver)


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["count", "bytes", "scope", "request"])
async def test_bad_frontier_or_ack_is_fatal(mutation):
    async with pair() as (sender, receiver, controls, _):
        entered = asyncio.Event()

        async def sink(_):
            entered.set()
            await asyncio.Event().wait()

        original = receiver._send_control

        async def bad_ack(kind, payload, request):
            if kind == "transfer_fence_ack":
                payload = {**payload, "scope": dict(payload["scope"])}
                if mutation == "count":
                    payload["sequence_count"] += 1
                elif mutation == "bytes":
                    payload["sent_bytes"] += 1
                elif mutation == "scope":
                    payload["scope"]["privacy_generation"] += 1
                else:
                    request = str(uuid.uuid4())
            await original(kind, payload, request)

        receiver._send_control = bad_ack
        info = identity(size=3)
        handle = receiver.authorize_receive(**info, sink=sink)
        sending = asyncio.create_task(sender.send(**info, source=chunks(b"a", b"b", b"c")))
        await entered.wait()
        await asyncio.gather(handle.cancel(), return_exceptions=True)
        await asyncio.gather(sending, return_exceptions=True)
        await until(lambda: sender.closed)
        assert sender.failure is not None
        assert any(m["kind"] == "transfer_fence_ack" for m in controls[1])


@pytest.mark.asyncio
async def test_completed_tombstones_ack_late_cancel_without_credit_inflation():
    async with pair() as (sender, receiver, controls, boot):
        info = identity(size=1)
        handle = receiver.authorize_receive(**info, sink=discard)
        await sender.send(**info, source=chunks(b"a"))
        await handle.wait()
        await sender.handle_control(envelope("transfer_cancel", info, boot, dict(code="cancelled")))
        await until(lambda: any(m["kind"] == "transfer_fence_ack" for m in controls[1]))
        assert not sender.closed and not receiver.closed and sender._send_slots._value == 2
        ack = next(m for m in controls[1] if m["kind"] == "transfer_fence_ack")
        await sender.handle_control(ack)
        # An exact late credit is recognized but cannot release a slot twice.
        for credit in [m for m in controls[1] if m["kind"] == "transfer_credit"]:
            await sender.handle_control(credit)
        assert sender._send_slots._value == 2
        await roundtrip(sender, receiver)


@pytest.mark.asyncio
async def test_unknown_remote_cancel_has_no_authority_or_terminal_allocation():
    async with pair() as (sender, _, _, boot):
        with pytest.raises(BulkTransferError):
            await sender.handle_control(
                envelope("transfer_cancel", identity(), boot, dict(code="cancelled"))
            )
        assert sender.closed and not sender._terminals


@pytest.mark.asyncio
async def test_terminal_admission_bound_and_expiry_without_identity_reuse():
    async with pair() as (sender, _, _, _):
        first = None
        for _ in range(MAX_TERMINALS):
            info = identity(size=1)
            first = first or info
            await sender.prepare_send(**info).cancel()
        assert len(sender._terminals) == MAX_TERMINALS
        with pytest.raises(BulkTransferError, match="busy"):
            sender.prepare_send(**identity(size=1))
        with pytest.raises(BulkTransferError, match="conflict"):
            sender.prepare_send(**first)
        for terminal in sender._terminals.values():
            terminal.expires = time.monotonic() - 1
        prepared = sender.prepare_send(**identity(size=1))
        assert not sender._terminals
        await prepared.cancel()


@pytest.mark.asyncio
async def test_second_credited_partial_body_remains_in_frontier_budget():
    async with pair() as (sender, receiver, controls, _):
        info = identity(size=6)
        handle = receiver.authorize_receive(**info, sink=discard)
        state = sender.prepare_send(**info)._state
        state.open_task = asyncio.create_task(
            sender._control(
                "transfer_open",
                state,
                dict(kind="snapshot", total_bytes=6, chunk_bytes=3, window_chunks=2),
                state.open_id,
            )
        )
        await state.open_task
        await until(lambda: state.initialized)
        # Withhold replenishment so the second body consumes the final credit.
        original = receiver._send_control

        async def withhold(kind, payload, request):
            if kind != "transfer_credit":
                await original(kind, payload, request)

        receiver._send_control = withhold
        for _ in range(2):
            await sender._send_slots.acquire()
        state.sent, state.sent_bytes, state.available = 2, 6, 0
        first = encode_bulk_header("snapshot", info["transfer_id"], 0, 3) + b"abc"
        second = encode_bulk_header("snapshot", info["transfer_id"], 1, 3) + b"def"
        sender.writer.write(first + second[: BULK_HEADER_BYTES + 1])
        await sender.writer.drain()
        await until(lambda: handle._state.reading_bytes == 3 and handle._state.sequence == 1)
        stopping = asyncio.create_task(handle.cancel())
        await until(lambda: any(m["kind"] == "transfer_fence" for m in controls[0]))
        assert not receiver.closed and not stopping.done()
        sender.writer.write(second[BULK_HEADER_BYTES + 1 :])
        await sender.writer.drain()
        await stopping
        receiver._send_control = original
        assert not sender.closed and not receiver.closed and sender._send_slots._value == 2
        await roundtrip(sender, receiver)


@pytest.mark.asyncio
async def test_caller_cancel_during_enqueued_writer_drain_counts_frame_once():
    async with pair() as (sender, receiver, controls, _):
        entered = asyncio.Event()
        original = sender.writer.drain
        calls = 0

        async def drain():
            nonlocal calls
            calls += 1
            if calls == 1:
                entered.set()
                await asyncio.Event().wait()
            await original()

        sender.writer.drain = drain
        info = identity(size=2)
        handle = receiver.authorize_receive(**info, sink=discard)
        sending = asyncio.create_task(sender.send(**info, source=chunks(b"a", b"b")))
        await entered.wait()
        sending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sending
        with pytest.raises(BulkTransferError):
            await handle.wait()
        fence = next(m for m in controls[0] if m["kind"] == "transfer_fence")
        assert fence["payload"]["sequence_count"] == fence["payload"]["sent_bytes"] == 1
        assert sender._send_slots._value == 2
        assert not sender.closed and not receiver.closed
        await roundtrip(sender, receiver)


@pytest.mark.asyncio
async def test_active_whole_operation_timeout_is_isolated():
    async with pair(timeout=0.03) as (sender, receiver, _, _):

        async def sink(_):
            await asyncio.Event().wait()

        info = identity(size=2)
        handle = receiver.authorize_receive(**info, sink=sink)
        sending = asyncio.create_task(sender.send(**info, source=chunks(b"a", b"b")))
        with pytest.raises(BulkTransferError, match="timeout"):
            await handle.wait()
        with pytest.raises(BulkTransferError, match="timeout"):
            await sending
        assert not sender.closed and not receiver.closed
        await roundtrip(sender, receiver)


@pytest.mark.asyncio
async def test_caller_cancel_waits_partial_end_metadata_before_fence():
    async with pair() as (sender, receiver, controls, _):
        entered, release = asyncio.Event(), asyncio.Event()
        original = sender._send_control

        async def delayed(kind, *args):
            if kind == "transfer_end":
                entered.set()
                await release.wait()
            await original(kind, *args)

        sender._send_control = delayed
        info = identity(size=1)
        handle = receiver.authorize_receive(**info, sink=discard)
        sending = asyncio.create_task(sender.send(**info, source=chunks(b"a")))
        await entered.wait()
        sending.cancel()
        await asyncio.sleep(0.01)
        assert all(m["kind"] != "transfer_fence" for m in controls[0])
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await sending
        await asyncio.gather(handle.wait(), return_exceptions=True)
        kinds = [m["kind"] for m in controls[0]]
        assert kinds.index("transfer_end") < kinds.index("transfer_fence")
        assert not sender.closed and not receiver.closed
        await roundtrip(sender, receiver)


@pytest.mark.asyncio
async def test_complete_result_crosses_cancellation_frontier_without_rejecting_late_result():
    async with pair() as (sender, receiver, controls, _):
        entered, release = asyncio.Event(), asyncio.Event()
        original = receiver._send_control

        async def delayed(kind, *args):
            if kind == "transfer_result":
                entered.set()
                await release.wait()
            await original(kind, *args)

        receiver._send_control = delayed
        info = identity(size=1)
        handle = receiver.authorize_receive(**info, sink=discard)
        sending = asyncio.create_task(sender.send(**info, source=chunks(b"a")))
        await entered.wait()
        await sender.cancel_transfer(info["transfer_id"])
        with pytest.raises(BulkTransferError, match="cancelled"):
            await sending
        release.set()
        await handle.wait()
        await until(lambda: any(m["kind"] == "transfer_result" for m in controls[1]))
        await asyncio.sleep(0)
        assert not sender.closed and not receiver.closed and sender._send_slots._value == 2
        await roundtrip(sender, receiver)


@pytest.mark.asyncio
async def test_extra_binary_after_acked_frontier_fails_before_sink():
    async with pair() as (sender, receiver, _, _):
        info = identity(size=1)
        calls = []
        handle = receiver.authorize_receive(**info, sink=lambda data: _append(calls, data))
        sending = asyncio.create_task(sender.send(**info, source=chunks(b"a")))
        await until(
            lambda: (
                sender._outgoing.get(info["transfer_id"]) is not None
                and sender._outgoing[info["transfer_id"]].open_enqueued
            )
        )
        await handle.cancel()
        await asyncio.gather(sending, return_exceptions=True)
        before = list(calls)
        sender.writer.write(encode_bulk_header("snapshot", info["transfer_id"], 1, 1) + b"x")
        await sender.writer.drain()
        await until(lambda: receiver.closed)
        assert calls == before


@pytest.mark.asyncio
async def test_terminal_records_do_not_hold_native_source_or_sink_references():
    async with pair() as (sender, receiver, _, _):
        await roundtrip(sender, receiver)
        for terminal in (*sender._terminals.values(), *receiver._terminals.values()):
            assert set(vars(terminal)) == {
                "scope",
                "operation_id",
                "transfer_id",
                "kind",
                "total",
                "incoming",
                "sequence",
                "received",
                "open_id",
                "end_id",
                "fence_id",
                "fence",
                "complete",
                "expires",
            }


@pytest.mark.asyncio
async def test_cancel_exactly_after_global_slot_acquire_compensates_unassigned_reservation():
    async with pair() as (sender, receiver, controls, _):
        original = sender._send_slots.acquire
        sending = None
        consumed = []

        async def acquire():
            result = await original()
            asyncio.get_running_loop().call_soon(sending.cancel)
            return result

        async def source():
            consumed.append(True)
            yield b"a"

        sender._send_slots.acquire = acquire
        info = identity(size=1)
        handle = receiver.authorize_receive(**info, sink=discard)
        sending = asyncio.create_task(sender.send(**info, source=source()))
        with pytest.raises(asyncio.CancelledError):
            await sending
        with pytest.raises(BulkTransferError):
            await handle.wait()
        assert not consumed and sender._send_slots._value == 2
        assert not sender.closed and not receiver.closed
        fence = next(m for m in controls[0] if m["kind"] == "transfer_fence")
        assert fence["payload"]["sequence_count"] == 0
        sender._send_slots.acquire = original
        await roundtrip(sender, receiver)


@pytest.mark.asyncio
async def test_minute_of_legacy_pcm_rate_does_not_exhaust_terminal_admission():
    async with pair() as (receiver, sender, _, _):
        # Fifty 20ms PCM operations/sec: retain a full minute of exact replay
        # metadata without evicting still-live tombstones or retaining audio.
        for _ in range(3000):
            info = identity("talkback_pcm", 1920)
            handle = receiver.authorize_receive(**info, sink=discard)
            await sender.send(**info, source=chunks(b"\0" * 1920))
            await handle.wait()
        assert len(sender._terminals) == len(receiver._terminals) == 3000
        assert not sender.closed and not receiver.closed
        assert sender._send_slots._value == 2
        assert sender.counts == receiver.counts == (0, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("initial", [True, False])
async def test_scope_revoked_at_credit_commit_is_local_and_never_grants_new_capacity(initial):
    async with pair() as (sender, receiver, controls, _):
        current = True
        original = receiver._grant_credit

        async def racing_grant(state, credits):
            nonlocal current
            if (state.sequence == 0) == initial:
                current = False
            await original(state, credits)

        receiver._grant_credit = racing_grant
        info = identity(size=1)
        handle = receiver.authorize_receive(**info, sink=discard, valid=lambda: current)
        sending = asyncio.create_task(sender.send(**info, source=chunks(b"a")))
        with pytest.raises(BulkTransferError, match="invalidated"):
            await handle.wait()
        await asyncio.gather(sending, return_exceptions=True)
        assert len([m for m in controls[1] if m["kind"] == "transfer_credit"]) == (
            0 if initial else 1
        )
        assert not sender.closed and not receiver.closed
        receiver._grant_credit = original
        await roundtrip(sender, receiver)
