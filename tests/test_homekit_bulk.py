"""Exercise actual private socket streams; no Ring or HomeKit publication."""

from __future__ import annotations

import asyncio
import socket
import uuid
from contextlib import asynccontextmanager

import pytest

from halo_server.homekit.bulk import BulkChannel, BulkTransferError
from halo_server.homekit.ipc import encode_bulk_header


def identity(kind="snapshot", size=6):
    return dict(
        scope=dict(
            profile_id=str(uuid.uuid4()),
            camera_id="camera",
            account_generation=1,
            device_generation=1,
            privacy_generation=1,
        ),
        operation_id=str(uuid.uuid4()),
        transfer_id=str(uuid.uuid4()),
        kind=kind,
        total_bytes=size,
    )


async def chunks(*values):
    for value in values:
        yield value


@asynccontextmanager
async def pair(timeout=2):
    a, b = socket.socketpair()
    left_streams = await asyncio.open_connection(sock=a, limit=131072)
    right_streams = await asyncio.open_connection(sock=b, limit=131072)
    boot_id = str(uuid.uuid4())
    queues = [asyncio.Queue(), asyncio.Queue()]
    controls = [[], []]

    async def send(index, kind, payload, request_id=None):
        envelope = dict(
            v=1,
            kind=kind,
            payload=payload,
            boot_id=boot_id,
            request_id=request_id or str(uuid.uuid4()),
        )
        controls[index].append(envelope)
        await queues[1 - index].put(envelope)

    left = BulkChannel(
        *left_streams,
        boot_id=boot_id,
        role="python",
        send_control=lambda *args: send(0, *args),
        timeout=timeout,
    )
    right = BulkChannel(
        *right_streams,
        boot_id=boot_id,
        role="hap",
        send_control=lambda *args: send(1, *args),
        timeout=timeout,
    )

    async def route(channel, queue):
        while True:
            message = await queue.get()
            await channel.handle_control(message)

    tasks = [
        asyncio.create_task(route(left, queues[0])),
        asyncio.create_task(route(right, queues[1])),
    ]
    left.start()
    right.start()
    try:
        yield left, right, controls, boot_id
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(left.close(), right.close(), return_exceptions=True)


@pytest.mark.asyncio
async def test_round_trip_with_end_control_ahead_of_binary_consumer():
    async with pair() as (sender, receiver, controls, _):
        entered, release = asyncio.Event(), asyncio.Event()
        delivered = []

        async def sink(chunk):
            entered.set()
            await release.wait()
            delivered.append(chunk)

        info = identity()
        handle = receiver.authorize_receive(**info, sink=sink)
        task = asyncio.create_task(sender.send(**info, source=chunks(b"abc", b"def")))
        await entered.wait()
        # Receiver has only its initial credits until the actual sink completes.
        assert len(controls[1]) == 1
        assert not handle._state.future.done()
        release.set()
        await asyncio.wait_for(asyncio.gather(task, handle.wait()), 1)
        assert delivered == [b"abc", b"def"]
        assert sender.counts == receiver.counts == (0, 0)
        assert [m["kind"] for m in controls[1]] == ["transfer_credit"] * 3 + ["transfer_result"]


@pytest.mark.asyncio
async def test_global_window_blocks_third_chunk_and_source_prefetch():
    async with pair() as (sender, receiver, _, _):
        release, entered = asyncio.Event(), asyncio.Event()
        produced = []

        async def source():
            for i in range(6):
                produced.append(i)
                yield bytes([i]) * 65536

        async def sink(_):
            entered.set()
            await release.wait()

        info = identity(size=6 * 65536)
        receiver.authorize_receive(**info, sink=sink)
        task = asyncio.create_task(sender.send(**info, source=source()))
        await entered.wait()
        await asyncio.sleep(0.02)
        assert produced == [0, 1]
        assert sender._send_slots.locked()
        release.set()
        await asyncio.wait_for(task, 1)


@pytest.mark.asyncio
async def test_talkback_uses_reverse_direction_and_frame_limit():
    async with pair() as (receiver, sender, _, _):
        data = []

        async def sink(chunk):
            data.append(chunk)

        info = identity("talkback_pcm", 1920)
        handle = receiver.authorize_receive(**info, sink=sink)
        await sender.send(**info, source=chunks(b"x" * 1920))
        await handle.wait()
        assert data == [b"x" * 1920]
        with pytest.raises(BulkTransferError):
            receiver.authorize_receive(**identity("talkback_pcm", 1921), sink=sink)


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["boot", "scope", "operation", "size", "kind", "id"])
async def test_incoming_metadata_never_creates_authority(mutation):
    async with pair() as (_, receiver, _, boot):

        async def sink(_):
            pytest.fail("unauthorized payload delivered")

        info = identity()
        receiver.authorize_receive(**info, sink=sink)
        payload = dict(
            scope=dict(info["scope"]),
            operation_id=info["operation_id"],
            transfer_id=info["transfer_id"],
            kind="snapshot",
            total_bytes=6,
            chunk_bytes=6,
            window_chunks=2,
        )
        if mutation == "boot":
            boot = str(uuid.uuid4())
        elif mutation == "scope":
            payload["scope"]["privacy_generation"] = 2
        elif mutation == "operation":
            payload["operation_id"] = str(uuid.uuid4())
        elif mutation == "size":
            payload["total_bytes"] = 7
        elif mutation == "kind":
            payload["kind"] = "recording_init"
        else:
            payload["transfer_id"] = str(uuid.uuid4())
        with pytest.raises(BulkTransferError):
            await receiver.handle_control(
                dict(
                    v=1,
                    kind="transfer_open",
                    boot_id=boot,
                    request_id=str(uuid.uuid4()),
                    payload=payload,
                )
            )
        assert receiver.closed


@pytest.mark.asyncio
async def test_unsolicited_binary_header_retires_before_reading_body():
    async with pair() as (sender, receiver, _, _):
        sender.writer.write(encode_bulk_header("snapshot", str(uuid.uuid4()), 0, 65536))
        await sender.writer.drain()
        for _ in range(100):
            if receiver.closed:
                break
            await asyncio.sleep(0.001)
        assert receiver.closed
        assert receiver.counts == (0, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "values,size", [([b"x" * 65537], 65537), ([b""], 1), ([b"ab"], 1), ([b"a"], 2)]
)
async def test_invalid_producer_sizes_fail_channel(values, size):
    async with pair() as (sender, receiver, _, _):

        async def sink(_):
            pass

        info = identity(size=size)
        receiver.authorize_receive(**info, sink=sink)
        with pytest.raises((BulkTransferError, asyncio.CancelledError)):
            await sender.send(**info, source=chunks(*values))
        assert sender.closed


@pytest.mark.asyncio
async def test_registration_count_and_whole_operation_deadline():
    async with pair(timeout=0.03) as (_, receiver, _, _):

        async def sink(_):
            pass

        handles = [receiver.authorize_receive(**identity(), sink=sink) for _ in range(4)]
        with pytest.raises(BulkTransferError, match="busy"):
            receiver.authorize_receive(**identity(), sink=sink)
        with pytest.raises(BulkTransferError, match="timeout"):
            await handles[0].wait()
        assert not receiver.closed
        assert receiver.counts == (0, 0)


@pytest.mark.asyncio
async def test_policy_revoked_while_consumer_waits_prevents_success_credit():
    async with pair() as (sender, receiver, controls, _):
        entered, release = asyncio.Event(), asyncio.Event()
        current = True

        async def sink(_):
            entered.set()
            await release.wait()

        info = identity(size=1)
        handle = receiver.authorize_receive(**info, sink=sink, valid=lambda: current)
        task = asyncio.create_task(sender.send(**info, source=chunks(b"a")))
        await entered.wait()
        current = False
        release.set()
        with pytest.raises(BulkTransferError):
            await handle.wait()
        await asyncio.gather(task, return_exceptions=True)
        assert [message["kind"] for message in controls[1]] == [
            "transfer_credit",
            "transfer_cancel",
            "transfer_fence_ack",
        ]
        assert not sender.closed and not receiver.closed


@pytest.mark.asyncio
async def test_failed_cleanup_keeps_reservation_and_retries():
    async with pair() as (_, receiver, _, _):
        attempts = []

        async def sink(_):
            pass

        async def cleanup(code):
            attempts.append(code)
            if len(attempts) == 1:
                raise RuntimeError("synthetic cleanup failure")

        receiver.authorize_receive(**identity(), sink=sink, on_close=cleanup)
        with pytest.raises(BulkTransferError, match="source-failed"):
            await receiver.close()
        assert receiver.counts == (1, 0)
        await receiver.close()
        assert receiver.counts == (0, 0)
        assert attempts == ["cancelled", "cancelled"]


@pytest.mark.asyncio
async def test_cancellation_joins_sink_and_source_cleanup():
    async with pair() as (sender, receiver, _, _):
        entered = asyncio.Event()
        closed = []

        async def sink(_):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                closed.append("sink")

        async def source():
            try:
                yield b"a"
            finally:
                closed.append("source")

        info = identity(size=1)
        receiver.authorize_receive(**info, sink=sink)
        task = asyncio.create_task(sender.send(**info, source=source()))
        await entered.wait()
        await receiver.close()
        await asyncio.gather(task, return_exceptions=True)
        assert set(closed) == {"sink", "source"}
        assert receiver.counts == (0, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["sequence", "kind", "chunk-size", "total-size"])
async def test_registered_transfer_rejects_malformed_binary_header_without_body(mutation):
    async with pair() as (sender, receiver, _, boot):

        async def sink(_):
            pytest.fail("bad header reached sink")

        info = identity(size=6)
        receiver.authorize_receive(**info, sink=sink)
        payload = dict(
            scope=info["scope"],
            operation_id=info["operation_id"],
            transfer_id=info["transfer_id"],
            kind="snapshot",
            total_bytes=6,
            chunk_bytes=3,
            window_chunks=2,
        )
        # The genuine sender is absent for this adversarial raw peer.
        receiver._send_control = lambda *args: asyncio.sleep(0)
        await receiver.handle_control(
            dict(
                v=1,
                kind="transfer_open",
                boot_id=boot,
                request_id=str(uuid.uuid4()),
                payload=payload,
            )
        )
        sequence = 1 if mutation == "sequence" else 0
        kind = "recording_init" if mutation == "kind" else "snapshot"
        length = 4 if mutation == "chunk-size" else 7 if mutation == "total-size" else 3
        sender.writer.write(encode_bulk_header(kind, info["transfer_id"], sequence, length))
        await sender.writer.drain()
        for _ in range(100):
            if receiver.closed:
                break
            await asyncio.sleep(0.001)
        assert receiver.closed


@pytest.mark.asyncio
async def test_scope_revoked_during_success_cleanup_does_not_emit_completion():
    async with pair() as (sender, receiver, controls, _):
        current = True

        async def sink(_):
            pass

        async def cleanup(_):
            nonlocal current
            current = False

        info = identity(size=1)
        handle = receiver.authorize_receive(
            **info, sink=sink, on_close=cleanup, valid=lambda: current
        )
        task = asyncio.create_task(sender.send(**info, source=chunks(b"a")))
        with pytest.raises(BulkTransferError):
            await handle.wait()
        await asyncio.gather(task, return_exceptions=True)
        assert all(message["kind"] != "transfer_result" for message in controls[1])


@pytest.mark.asyncio
async def test_outgoing_source_cleanup_failure_is_not_swallowed():
    async with pair() as (sender, receiver, _, _):

        async def sink(_):
            pass

        class Producer:
            def __init__(self):
                self.read = 0
                self.closed = 0

            def __aiter__(self):
                return self

            async def __anext__(self):
                self.read += 1
                if self.read == 1:
                    return b"a"
                raise StopAsyncIteration

            async def aclose(self):
                self.closed += 1
                raise RuntimeError("synthetic producer cleanup failure")

        producer = Producer()
        info = identity(size=1)
        receiver.authorize_receive(**info, sink=sink)
        with pytest.raises((RuntimeError, BulkTransferError)):
            await sender.send(**info, source=producer)
        assert not sender.closed
        assert sender.counts == (0, 1)
        assert sender.cleanup_pending == 1
        assert producer.closed >= 1
        producer.aclose = lambda: asyncio.sleep(0)
        await sender.close()
        assert sender.counts == (0, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["replay", "future-ack", "request-id"])
async def test_credits_cannot_inflate_sender_window(mutation):
    async with pair() as (sender, receiver, controls, _):
        entered = asyncio.Event()

        async def sink(_):
            entered.set()
            await asyncio.Event().wait()

        info = identity(size=6)
        receiver.authorize_receive(**info, sink=sink)
        sending = asyncio.create_task(sender.send(**info, source=chunks(b"abc", b"def")))
        await entered.wait()
        credit = {**controls[1][0], "payload": dict(controls[1][0]["payload"])}
        if mutation == "future-ack":
            credit["payload"].update(next_sequence=100, credits=1)
        elif mutation == "request-id":
            credit["request_id"] = str(uuid.uuid4())
        with pytest.raises(BulkTransferError):
            await sender.handle_control(credit)
        await asyncio.gather(sending, return_exceptions=True)
        assert sender.closed


@pytest.mark.asyncio
async def test_two_outgoing_transfers_share_the_same_global_window():
    async with pair() as (sender, receiver, _, _):
        entered, release = asyncio.Event(), asyncio.Event()
        produced = []

        async def sink(_):
            entered.set()
            await release.wait()

        async def source(label):
            for i in range(3):
                produced.append((label, i))
                yield b"a" * 65536

        infos = [identity(size=3 * 65536), identity(size=3 * 65536)]
        for info in infos:
            receiver.authorize_receive(**info, sink=sink)
        tasks = [
            asyncio.create_task(sender.send(**info, source=source(i)))
            for i, info in enumerate(infos)
        ]
        await entered.wait()
        await asyncio.sleep(0.02)
        assert len(produced) == 2
        release.set()
        await asyncio.gather(*tasks)


@pytest.mark.asyncio
async def test_stubborn_consumer_has_bounded_failed_close_and_visible_retry():
    async with pair(timeout=0.03) as (sender, receiver, _, _):
        entered, release = asyncio.Event(), asyncio.Event()

        async def sink(_):
            entered.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue

        info = identity(size=1)
        receiver.authorize_receive(**info, sink=sink)
        sending = asyncio.create_task(sender.send(**info, source=chunks(b"a")))
        await entered.wait()
        with pytest.raises(BulkTransferError, match="source-failed"):
            await asyncio.wait_for(receiver.close(), 0.2)
        assert receiver.counts == (1, 0)
        assert receiver._owned_tasks
        release.set()
        await asyncio.sleep(0)
        await receiver.close()
        assert receiver.counts == (0, 0)
        await asyncio.gather(sending, return_exceptions=True)


@pytest.mark.asyncio
async def test_stubborn_producer_retains_its_operation_until_cleanup_retry():
    async with pair(timeout=0.03) as (sender, receiver, _, _):
        entered, release = asyncio.Event(), asyncio.Event()

        class Producer:
            def __init__(self):
                self.closed = False

            def __aiter__(self):
                return self

            async def __anext__(self):
                entered.set()
                while not release.is_set():
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        continue
                raise StopAsyncIteration

            async def aclose(self):
                self.closed = True

        producer = Producer()

        async def sink(_):
            pass

        info = identity(size=1)
        receiver.authorize_receive(**info, sink=sink)
        sending = asyncio.create_task(sender.send(**info, source=producer))
        await entered.wait()
        with pytest.raises(BulkTransferError, match="source-failed"):
            await asyncio.wait_for(sender.close(), 0.2)
        assert sender.counts == (0, 1)
        assert not producer.closed
        release.set()
        await asyncio.gather(sending, return_exceptions=True)
        await sender.close()
        assert producer.closed
        assert sender.counts == (0, 0)
