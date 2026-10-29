"""Bounded exact-ciphertext repairs never own the feedback reader."""

import asyncio
import struct
from contextlib import asynccontextmanager
from types import SimpleNamespace as NS

import pytest
from aiortc.rtp import RtcpByePacket, RtcpRtpfbPacket
from test_homekit_live import CONFIG, DeferredAdapter, native_crypto, observe_video_clock, prepared
from test_homekit_video_recovery import controlled, output

from halo_server.homekit.models import HomeKitOperationError
from halo_server.media import homekit_live as module
from halo_server.media.video_repair import (
    MAX_REPAIR_BYTES,
    MAX_REPAIR_PACKETS,
    RepairQueue,
    RepairRate,
)


def cache(now, *, epoch=1, size=1200):
    return now, b"x" * size, size - 22, epoch, now + 1


@pytest.mark.asyncio
async def test_queue_bounds_include_current_ciphertext_after_clear_and_fence():
    queue = RepairQueue()
    originals = [cache(10) for _ in range(MAX_REPAIR_PACKETS + 1)]
    for sequence in range(MAX_REPAIR_PACKETS):
        assert queue.enqueue(sequence, originals[sequence]) == "queued"
    item = await queue.take()
    assert item[1] is originals[0]
    assert queue.enqueue(0, originals[0]) == "duplicate"
    assert queue.enqueue(MAX_REPAIR_PACKETS, originals[-1]) == "full"
    assert queue.snapshot()["pending_bytes"] == MAX_REPAIR_BYTES
    queue.clear()
    assert queue.snapshot()["pending_packets"] == 1
    assert queue.snapshot()["pending_bytes"] == 1200 and queue.snapshot()["in_flight"]
    queue.fence()
    assert queue.enqueue(1, originals[1]) == "closed"
    assert queue.snapshot()["pending_bytes"] == 1200
    queue.finish(item)
    assert queue.snapshot()["pending_packets"] == queue.snapshot()["pending_bytes"] == 0
    assert await queue.take() is None


def test_request_and_sent_rolling_budgets_are_distinct_and_do_not_reset_at_fixed_boundaries():
    requests, sent = RepairRate(), RepairRate()
    for _ in range(64):
        assert requests.permits(0.9, 1200)
        requests.record(0.9, 1200)
    assert sent.permits(0.9, 1200)  # Enqueueing did not pretend a socket send occurred.
    assert not requests.permits(1.01, 1200)
    assert not requests.permits(0.8, 1200)  # A backwards clock cannot restore tokens.
    assert requests.permits(1.900001, 1200)
    assert len(requests._rows) == 0
    assert not requests.permits(1.8, 1200)  # A non-recorded query also advances clock authority.


@pytest.mark.asyncio
async def test_late_nack_after_source_deadline_replays_exact_ciphertext_without_reencryption(
    monkeypatch,
):
    async with controlled(monkeypatch) as (manager, _, session, clock, sink):
        receipt = clock()
        assert await manager._access_unit(
            session, output(), session.capture_epoch, received_at=receipt
        )
        sequence, original = next(iter(session.retransmit.items()))
        first_send = original[0]
        assert original[4] == first_send + 1
        clock.now = first_send + 0.650
        assert clock() > receipt + 0.5
        count = len(sink.sent)
        protects = []
        session.secure.protect = lambda packet: protects.append(packet)
        session.repair_queue.enqueue(sequence, original)
        session.repair_worker = asyncio.create_task(manager._repair_video(session))
        async with asyncio.timeout(1):
            while len(sink.sent) == count:
                await asyncio.sleep(0)
        assert sink.sent[-1][1] is original[1] and not protects
        assert session.retransmit[sequence] is original  # Successful repair never extends age.


@pytest.mark.asyncio
@pytest.mark.parametrize("invalidate", ["expired", "epoch", "policy", "identity", "reset"])
async def test_queued_repair_is_revalidated_after_wait(monkeypatch, invalidate):
    async with controlled(monkeypatch) as (manager, controller, session, clock, sink):
        original = cache(clock(), epoch=session.capture_epoch)
        session.retransmit[1], session.retransmit_bytes = original, len(original[1])
        session.repair_queue.enqueue(1, original)
        await session.send_lock.acquire()
        session.repair_worker = asyncio.create_task(manager._repair_video(session))
        await asyncio.sleep(0)
        assert session.repair_queue.snapshot()["in_flight"]
        if invalidate == "expired":
            clock.now += 1.001
        elif invalidate == "epoch":
            controller.handle.buffer.clear()
        elif invalidate == "policy":
            controller.allowed = False
        elif invalidate == "identity":
            session.retransmit[1] = cache(clock(), epoch=session.capture_epoch)
        else:
            manager._need_idr(session)
        session.send_lock.release()
        async with asyncio.timeout(1):
            while session.repair_queue.snapshot()["in_flight"]:
                await asyncio.sleep(0)
        assert not sink.sent


@pytest.mark.asyncio
async def test_repair_waiting_for_au_lock_expires_without_closing_healthy_view(monkeypatch):
    async with controlled(monkeypatch) as (manager, _, session, clock, sink):
        original = cache(clock() - 0.980, epoch=session.capture_epoch)
        session.retransmit[1], session.retransmit_bytes = original, len(original[1])
        session.repair_queue.enqueue(1, original)
        await session.send_lock.acquire()
        session.repair_worker = asyncio.create_task(manager._repair_video(session))
        await until(lambda: session.metrics.snapshot()["counters"]["repair_deadline_rejected"] == 1)
        assert not sink.sent and not session.retired
        assert session.repair_queue.snapshot()["pending_packets"] == 0
        session.send_lock.release()


@asynccontextmanager
async def feedback_owner():
    manager, controller, identity, prepared_video, receiver = await prepared()
    manager._reaper.cancel()
    await asyncio.gather(manager._reaper, return_exceptions=True)
    session = manager._sessions[identity]
    session.config, session.handle = CONFIG, controller.handle
    session.capture_epoch, session.state = controller.handle.capture_epoch, "active"
    session.adapter = NS(close=lambda: asyncio.sleep(0), request_keyframe=lambda: None)
    session.receiver = asyncio.create_task(manager._receive_feedback(session))
    session.repair_worker = asyncio.create_task(manager._repair_video(session))
    peer = native_crypto(500)
    try:
        yield manager, controller, session, prepared_video, receiver, peer
    finally:
        receiver.close()
        await manager.close()


async def seed(manager, session, receiver, count=1):
    result = []
    for sequence in range(count):
        clear = (
            struct.pack("!BBHII", 0x80, 99, sequence, 90000, session.prepared.ssrc) + b"\x41\x88"
        )
        protected = session.secure.protect(clear)
        await manager._send(session, protected)
        sent_at = manager._clock()
        cached = sent_at, protected, 2, session.capture_epoch, sent_at + 1
        session.retransmit[sequence] = cached
        session.retransmit_bytes += len(protected)
        assert (
            await asyncio.wait_for(asyncio.get_running_loop().sock_recv(receiver, 2048), 1)
            == protected
        )
        result.append(cached)
    session.packetizer.sequence = count
    return result


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.001)


@pytest.mark.asyncio
async def test_authenticated_nack_storm_has_inclusive_queue_and_separate_request_send_limits():
    async with feedback_owner() as (manager, _, session, prepared_video, receiver, peer):
        originals = await seed(manager, session, receiver, 80)
        await session.send_lock.acquire()
        nack = bytes(RtcpRtpfbPacket(1, 500, prepared_video.ssrc, list(range(80))))
        receiver.sendto(peer.protect_rtcp(nack), ("127.0.0.1", prepared_video.port))
        await until(lambda: session.nack_count == 64)
        assert session.repair_queue.snapshot()["pending_packets"] == 64
        assert session.metrics.snapshot()["counters"]["retransmit_packets"] == 0
        assert len(session.repair_sends._rows) == 0 and len(session.repair_requests._rows) == 64
        assert session.metrics.snapshot()["counters"]["nack_rate_limited"] == 16
        session.send_lock.release()
        await until(lambda: session.metrics.snapshot()["counters"]["retransmit_packets"] == 64)
        assert session.repair_queue.snapshot()["pending_packets"] == 0
        for original in originals[:64]:
            assert (
                await asyncio.wait_for(asyncio.get_running_loop().sock_recv(receiver, 2048), 1)
                == original[1]
            )
        assert len(session.repair_sends._rows) == 64


@pytest.mark.asyncio
async def test_feedback_bye_does_not_wait_for_repair_au_lock():
    async with feedback_owner() as (manager, _, session, prepared_video, receiver, peer):
        await seed(manager, session, receiver)
        await session.send_lock.acquire()
        nack = bytes(RtcpRtpfbPacket(1, 500, prepared_video.ssrc, [0]))
        receiver.sendto(peer.protect_rtcp(nack), ("127.0.0.1", prepared_video.port))
        await until(lambda: session.repair_queue.snapshot()["in_flight"])
        bye = bytes(RtcpByePacket(sources=[500]))
        receiver.sendto(peer.protect_rtcp(bye), ("127.0.0.1", prepared_video.port))
        await until(lambda: session.retired)
        assert session.repair_queue.snapshot()["closed"] and session.socket.fileno() == -1
        await until(lambda: session.repair_worker.done())
        assert session.repair_queue.snapshot()["pending_packets"] == 0
        session.send_lock.release()


@pytest.mark.asyncio
async def test_native_authenticated_late_nack_uses_one_second_cache_without_extending_it():
    async with feedback_owner() as (manager, controller, session, prepared_video, receiver, peer):
        original = (await seed(manager, session, receiver))[0]
        await asyncio.sleep(0.650)
        assert manager._clock() - original[0] > 0.5
        nack = bytes(RtcpRtpfbPacket(1, 500, prepared_video.ssrc, [0]))
        receiver.sendto(peer.protect_rtcp(nack), ("127.0.0.1", prepared_video.port))
        assert (
            await asyncio.wait_for(asyncio.get_running_loop().sock_recv(receiver, 2048), 0.25)
            == original[1]
        )
        assert session.retransmit[0] is original
        await asyncio.sleep(max(0, original[4] - manager._clock()) + 0.010)
        receiver.sendto(peer.protect_rtcp(nack), ("127.0.0.1", prepared_video.port))
        await until(lambda: session.metrics.snapshot()["counters"]["nack_cache_misses"] == 1)
        assert session.metrics.snapshot()["counters"]["retransmit_packets"] == 1
        assert controller.opens == 0  # Repairs never acquire or renew the source.


@pytest.mark.asyncio
async def test_waiting_new_access_unit_is_not_starved_by_queued_repairs():
    async with feedback_owner() as (manager, _, session, _, receiver, _):
        originals = await seed(manager, session, receiver, 64)
        observe_video_clock(manager, session)
        await session.send_lock.acquire()
        for sequence, original in enumerate(originals):
            session.repair_queue.enqueue(sequence, original)
        await until(lambda: session.repair_queue.snapshot()["in_flight"])
        fresh = asyncio.create_task(
            manager._access_unit(
                session, output(), session.capture_epoch, received_at=manager._clock()
            )
        )
        await asyncio.sleep(0)
        session.send_lock.release()
        assert await asyncio.wait_for(fresh, 1)
        assert session.metrics.snapshot()["counters"]["sent_access_units"] == 1
        assert session.repair_queue.snapshot()["pending_packets"] > 0


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["bye", "stop"])
async def test_blocked_udp_repair_does_not_block_feedback_or_stop(terminal):
    async with feedback_owner() as (manager, controller, session, prepared_video, receiver, peer):
        original = (await seed(manager, session, receiver))[0]
        blocked = asyncio.Event()
        socket = session.socket

        class BusySocket:
            def __getattr__(self, name):
                return getattr(socket, name)

            def send(self, _packet):
                blocked.set()
                raise BlockingIOError

        session.socket = BusySocket()
        session.repair_queue.enqueue(0, original)
        await asyncio.wait_for(blocked.wait(), 1)
        if terminal == "bye":
            receiver.sendto(
                peer.protect_rtcp(bytes(RtcpByePacket(sources=[500]))),
                ("127.0.0.1", prepared_video.port),
            )
        else:
            await manager.stop(controller.boot, controller.scope, session.identity)
        await until(lambda: session.retired and session.repair_worker.done())
        assert session.repair_queue.snapshot()["pending_packets"] == 0
        assert session.metrics.snapshot()["counters"]["retransmit_packets"] == 0


@pytest.mark.asyncio
async def test_stop_with_blocked_repair_retains_actual_owner_until_join(monkeypatch):
    monkeypatch.setattr(module, "CLEANUP_SECONDS", 0.020)
    async with feedback_owner() as (manager, controller, session, _, receiver, _):
        original = (await seed(manager, session, receiver))[0]
        entered, release = asyncio.Event(), asyncio.Event()

        async def delayed(*_args, **_kwargs):
            entered.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                await release.wait()
                raise

        monkeypatch.setattr(manager, "_paced_send", delayed)
        session.repair_queue.enqueue(0, original)
        await entered.wait()
        try:
            with pytest.raises(HomeKitOperationError) as failure:
                await manager.stop(controller.boot, controller.scope, session.identity)
            assert failure.value.code == "cleanup_pending"
            assert manager.pending_resources == 1
            assert session.repair_queue.snapshot()["pending_packets"] == 1
            assert session.repair_queue.snapshot()["in_flight"]
        finally:
            release.set()
        await until(lambda: session.repair_worker.done())
        await manager.stop(controller.boot, controller.scope, session.identity)
        assert manager.pending_resources == 0
        assert session.repair_queue.snapshot()["pending_packets"] == 0


@pytest.mark.asyncio
async def test_prepared_stop_fences_empty_repair_owner_without_source_acquisition():
    manager, controller, identity, _, receiver = await prepared()
    session = manager._sessions[identity]
    try:
        assert session.repair_worker is None
        await manager.stop(controller.boot, controller.scope, identity)
        assert controller.opens == 0 and manager.pending_resources == 0
        assert session.repair_queue.snapshot()["closed"]
        assert session.repair_queue.snapshot()["pending_packets"] == 0
    finally:
        receiver.close()
        await manager.close()


@pytest.mark.asyncio
async def test_partial_start_task_failure_joins_all_created_media_tasks(monkeypatch):
    adapter = DeferredAdapter(CONFIG)
    manager, controller, identity, _, receiver = await prepared(adapter_factory=lambda _: adapter)
    session = manager._sessions[identity]
    original = module.asyncio.create_task

    def fail_repair_start(coroutine, **kwargs):
        if kwargs.get("name") == "homekit-video-repair":
            coroutine.close()
            raise RuntimeError("synthetic repair task admission failed")
        return original(coroutine, **kwargs)

    monkeypatch.setattr(module.asyncio, "create_task", fail_repair_start)
    try:
        with pytest.raises(RuntimeError, match="synthetic repair task admission failed"):
            await manager.start(controller.boot, controller.scope, identity, CONFIG)
        assert manager.pending_resources == 0 and controller.handle.closed and adapter.closed
        assert all(task.done() for task in (session.sender, session.receiver, session.reports))
        assert session.repair_worker is None and session.repair_queue.snapshot()["closed"]
    finally:
        receiver.close()
        await manager.close()
